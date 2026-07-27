#!/usr/bin/env sh
set -eu

APP_DIR="${KM_VMS_APP_DIR:-}"
PROJECT_NAME="${KM_VMS_PROJECT_NAME:-}"
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-}"
VERIFY_STORAGE_SELECTION=0
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

usage() {
  cat <<'EOF'
KM VMS safe restart helper

Usage:
  sh scripts/km-vms-restart.sh --app-dir <path> [--project-name <name>] [--verify-storage-selection] [--help]

Environment equivalents:
  KM_VMS_APP_DIR, KM_VMS_PROJECT_NAME, KM_VMS_DOCKER_COMPOSE.

This helper does not regenerate .env, remove volumes, create users/settings,
or re-run completed schema/update one-shot services. It reconciles only the
persistent services of an existing compose application.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

. "$SCRIPT_DIR/km-vms-compose-common.sh"

safe_project_name() {
  value="$1"
  [ -n "$value" ] || fail "Project name must not be empty."
  case "$value" in
    *[A-Z]*)
      fail "Project name must be lowercase. Use: $(printf '%s' "$value" | tr 'A-Z' 'a-z')"
      ;;
  esac
  if printf '%s' "$value" | grep -Eq '^[a-z][a-z0-9_-]*$'; then
    printf '%s\n' "$value"
    return 0
  fi
  fail "Project name must start with a lowercase letter and contain only lowercase letters, digits, dashes or underscores."
}

normalize_path() {
  value="$1"
  case "$value" in
    /*) ;;
    *) value="$(pwd)/$value" ;;
  esac
  parent=$(dirname "$value")
  base=$(basename "$value")
  [ -d "$parent" ] || fail "Parent directory does not exist: $parent"
  parent_real=$(cd "$parent" && pwd -P)
  if [ "$parent_real" = "/" ]; then
    printf '/%s\n' "$base"
  else
    printf '%s/%s\n' "$parent_real" "$base"
  fi
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

read_control_value() {
  file="$1"
  key="$2"
  [ -f "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key="*)
        printf '%s\n' "${line#*=}"
        return 0
        ;;
    esac
  done < "$file"
  return 1
}

detect_compose() {
  km_vms_detect_compose "$DOCKER_COMPOSE_BIN" || fail "Docker Compose was not found. Checked KM_VMS_DOCKER_COMPOSE, PATH docker compose/docker-compose, and known NAS vendor paths."
}

compose_cmd() {
  km_vms_compose_cmd "$@"
}

compose_with_archive_roots() {
  if [ -f "$ARCHIVE_ROOTS_COMPOSE_FILE" ]; then
    compose_cmd --env-file "$ENV_FILE" -f "$APP_DIR/docker-compose.yml" -f "$ARCHIVE_ROOTS_COMPOSE_FILE" "$@"
  else
    compose_cmd --env-file "$ENV_FILE" -f "$APP_DIR/docker-compose.yml" "$@"
  fi
}

archive_roots_compose_present() {
  [ -f "$ARCHIVE_ROOTS_COMPOSE_FILE" ]
}

wait_for_archive_roots_compose_file() {
  attempt=0
  while [ "$attempt" -lt 30 ]; do
    archive_roots_compose_present && return 0
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
}

apply_generated_archive_roots_compose_if_needed() {
  was_present="$1"
  shift
  if [ "$was_present" = "1" ]; then
    return 0
  fi
  wait_for_archive_roots_compose_file || return 0
  compose_with_archive_roots "$@" config >/dev/null
  compose_with_archive_roots "$@" up -d --no-deps --force-recreate api
}

reconcile_persistent_services() {
  compose_with_archive_roots "$@" up -d --no-deps \
    update-status-reader \
    update-retry-admission \
    api \
    recorder \
    web \
    nginx \
    setup-helper \
    update-helper
}

write_apply_status() {
  status="$1"
  note="$2"
  selected_path="$3"
  request_id_value=$(read_control_value "$SELECTION_CONTROL_FILE" activation_request_id || true)
  operation_id_value=$(read_control_value "$SELECTION_CONTROL_FILE" operation_id || true)
  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  tmp="$STATUS_FILE.tmp.$$"
  {
    printf '{\n'
    printf '  "schema_version": 2,\n'
    printf '  "status": "%s",\n' "$(json_escape "$status")"
    printf '  "request_id": "%s",\n' "$(json_escape "$request_id_value")"
    printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id_value")"
    printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
    printf '  "container_archive_path": "/storage/archive",\n'
    printf '  "updated_at": "%s"' "$(json_escape "$created_at")"
    if [ -n "$note" ]; then
      printf ',\n  "note": "%s"\n' "$(json_escape "$note")"
    else
      printf '\n'
    fi
    printf '}\n'
  } > "$tmp"
  mv "$tmp" "$STATUS_FILE"
  chmod 600 "$STATUS_FILE" 2>/dev/null || true
}

wait_for_marker() {
  service="$1"
  selected_path="$2"
  attempt=0
  while [ "$attempt" -lt 30 ]; do
    marker=$(compose_with_archive_roots exec -T "$service" sh -c 'cat /storage/archive/.km-vms-storage-root.json 2>/dev/null' || true)
    if [ -n "$marker" ] && printf '%s' "$marker" | grep -F "\"selected_host_path\": \"$selected_path\"" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    attempt=$((attempt + 1))
  done
  return 1
}

verify_storage_selection() {
  [ -f "$SELECTION_CONTROL_FILE" ] || fail "storage-selection.control not found for verification"
  selected_path=$(read_control_value "$SELECTION_CONTROL_FILE" selected_host_path || true)
  request_id_value=$(read_control_value "$SELECTION_CONTROL_FILE" activation_request_id || true)
  operation_id_value=$(read_control_value "$SELECTION_CONTROL_FILE" operation_id || true)
  [ -n "$selected_path" ] || fail "selected_host_path missing in storage-selection.control"
  write_apply_status "activation_in_progress" "Waiting for recreated services to expose the selected storage marker." "$selected_path"
  wait_for_marker api "$selected_path" || fail "API service did not expose the selected archive marker after restart"
  wait_for_marker recorder "$selected_path" || fail "Recorder service did not expose the selected archive marker after restart"
  verified_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  tmp="$STATUS_FILE.tmp.$$"
  {
    printf '{\n'
    printf '  "schema_version": 2,\n'
    printf '  "status": "active",\n'
    printf '  "request_id": "%s",\n' "$(json_escape "$request_id_value")"
    printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id_value")"
    printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
    printf '  "container_archive_path": "/storage/archive",\n'
    printf '  "verified_at": "%s",\n' "$(json_escape "$verified_at")"
    printf '  "runtime_proof": {\n'
    printf '    "type": "container_marker_visibility",\n'
    printf '    "services": ["api", "recorder"]\n'
    printf '  }\n'
    printf '}\n'
  } > "$tmp"
  mv "$tmp" "$STATUS_FILE"
  chmod 600 "$STATUS_FILE" 2>/dev/null || true
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-dir)
      [ "$#" -ge 2 ] || fail "--app-dir requires a value"
      APP_DIR="$2"
      shift 2
      ;;
    --project-name)
      [ "$#" -ge 2 ] || fail "--project-name requires a value"
      PROJECT_NAME="$2"
      shift 2
      ;;
    --verify-storage-selection)
      VERIFY_STORAGE_SELECTION=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

[ -n "$APP_DIR" ] || fail "--app-dir or KM_VMS_APP_DIR is required."
APP_DIR=$(normalize_path "$APP_DIR")
[ -f "$APP_DIR/docker-compose.yml" ] || fail "docker-compose.yml not found in app dir: $APP_DIR"
[ -f "$APP_DIR/.env" ] || fail ".env not found in app dir: $APP_DIR"
ENV_FILE="$APP_DIR/.env"
SELECTION_FILE="$APP_DIR/data/install-control/storage-selection.json"
SELECTION_CONTROL_FILE="$APP_DIR/data/install-control/storage-selection.control"
STATUS_FILE="$APP_DIR/data/install-control/storage-apply-status.json"
ARCHIVE_ROOTS_COMPOSE_FILE="$APP_DIR/data/install-control/docker-compose.archive-roots.yml"

detect_compose

set --
if [ -n "$PROJECT_NAME" ]; then
  PROJECT_NAME=$(safe_project_name "$PROJECT_NAME")
  set -- "$@" --project-name "$PROJECT_NAME"
fi

(
  cd "$APP_DIR"
  archive_roots_compose_was_present=0
  archive_roots_compose_present && archive_roots_compose_was_present=1
  compose_with_archive_roots "$@" config >/dev/null
  if [ "$VERIFY_STORAGE_SELECTION" = "1" ]; then
    compose_with_archive_roots "$@" up -d --no-deps --force-recreate api recorder web nginx
  else
    reconcile_persistent_services "$@"
  fi
  apply_generated_archive_roots_compose_if_needed "$archive_roots_compose_was_present" "$@"
)

if [ "$VERIFY_STORAGE_SELECTION" = "1" ]; then
  verify_storage_selection
fi

printf 'KM VMS restart command completed for %s\n' "$APP_DIR"
