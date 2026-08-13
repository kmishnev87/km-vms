#!/usr/bin/env sh
set -eu

APP_DIR="${KM_VMS_APP_DIR:-}"
PROJECT_NAME="${KM_VMS_PROJECT_NAME:-}"
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-}"
VERIFY_STORAGE_SELECTION=0
INITIAL_SETUP=0
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

usage() {
  cat <<'EOF'
KM VMS safe restart helper

Usage:
  sh <app-dir>/data/update-runtime/bootstrap/current/km-vms-restart.sh \
    --app-dir <app-dir> [--project-name <name>] [--verify-storage-selection] [--initial-setup] [--help]

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

read_env_value() {
  key="$1"
  sed -n "s/^$key=//p" "$APP_DIR/.env" | tail -n 1
}

bootstrap_command() {
  bootstrap="$APP_DIR/data/update-runtime/bootstrap/current/km-vms-bootstrap.py"
  [ -f "$bootstrap" ] || fail "Stable bootstrap authority is unavailable."
  if command -v python3 >/dev/null 2>&1; then
    python3 -B "$bootstrap" "$@"
    return $?
  fi
  command -v docker >/dev/null 2>&1 ||
    fail "Canonical maintenance check is unavailable."
  helper_id=$(docker ps -q \
    --filter "label=com.docker.compose.project=$PROJECT_NAME" \
    --filter "label=com.docker.compose.service=update-helper" | head -n 2)
  [ "$(printf '%s\n' "$helper_id" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] ||
    fail "Canonical maintenance check owner is unavailable."
  docker exec "$helper_id" python3 -B "$bootstrap" "$@"
}

writer_isolation_state() {
  set +e
  output=$(bootstrap_command writer-isolation --app-dir "$APP_DIR" 2>/dev/null)
  status=$?
  set -e
  if [ "$status" = "0" ] && [ "$output" = "inactive" ]; then
    printf 'inactive\n'
    return 0
  fi
  if [ "$status" = "75" ] && [ "$output" = "active" ]; then
    printf 'active\n'
    return 0
  fi
  fail "Canonical maintenance writer-isolation evidence is unavailable."
}

detect_compose() {
  km_vms_detect_compose "$DOCKER_COMPOSE_BIN" || fail "Docker Compose was not found. Checked KM_VMS_DOCKER_COMPOSE, PATH docker compose/docker-compose, and known NAS vendor paths."
  case "$COMPOSE_BIN" in
    */*)
      compose_bin_dir=$(dirname "$COMPOSE_BIN")
      if [ -x "$compose_bin_dir/docker" ]; then
        PATH="$compose_bin_dir:$PATH"
        export PATH
      fi
      ;;
  esac
}

compose_cmd() {
  km_vms_compose_cmd "$@"
}

compose_with_archive_roots() {
  km_vms_compose_for_source "$APP_DIR" "$SOURCE_DIR" "$@"
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
  [ "$WRITER_ISOLATION" != "active" ] ||
    fail "maintenance_writer_isolation_active"
  compose_with_archive_roots "$@" config >/dev/null
  compose_with_archive_roots "$@" up -d --no-deps --force-recreate api
}

reconcile_persistent_services() {
  if [ "$WRITER_ISOLATION" = "active" ]; then
    compose_with_archive_roots "$@" up -d --no-deps \
      postgres \
      redis \
      update-status-reader \
      update-retry-admission \
      web \
      nginx \
      setup-helper \
      update-helper
    return 0
  fi
  compose_with_archive_roots "$@" up -d --no-deps \
    postgres \
    redis \
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

prepare_initial_storage_configuration() {
  selected_path=$(read_control_value "$SELECTION_CONTROL_FILE" selected_host_path || true)
  request_id_value=$(read_control_value "$SELECTION_CONTROL_FILE" activation_request_id || true)
  operation_id_value=$(read_control_value "$SELECTION_CONTROL_FILE" operation_id || true)
  [ -n "$selected_path" ] || fail "selected_host_path missing in storage-selection.control"
  [ -n "$request_id_value" ] || fail "activation_request_id missing in storage-selection.control"
  [ -z "$operation_id_value" ] || fail "initial storage convergence cannot process runtime activation"
  write_apply_status "activation_in_progress" "Publishing the selected initial storage binding." "$selected_path"
  compose_with_archive_roots exec -T api \
    python3 -m app.services.setup_storage \
    converge-runtime-files \
    --selected-host-path "$selected_path" \
    --request-id "$request_id_value" >/dev/null
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
  if [ "$INITIAL_SETUP" = "1" ]; then
    compose_with_archive_roots exec -T api \
      python3 -m app.services.setup_storage \
      prove-runtime \
      --selected-host-path "$selected_path" \
      --request-id "$request_id_value" >/dev/null || fail "API default archive runtime proof failed after restart"
  fi
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
    printf '    "api_canonical_marker": true,\n'
    printf '    "recorder_canonical_marker": true,\n'
    if [ "$INITIAL_SETUP" = "1" ]; then
      printf '    "api_default_runtime_marker": true,\n'
      printf '    "api_default_runtime_namespace": true,\n'
      printf '    "api_default_runtime_read_write": true\n'
    else
      printf '    "api_default_runtime_marker": null,\n'
      printf '    "api_default_runtime_namespace": null,\n'
      printf '    "api_default_runtime_read_write": null\n'
    fi
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
    --initial-setup)
      INITIAL_SETUP=1
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
[ "$INITIAL_SETUP" = "0" ] || [ "$VERIFY_STORAGE_SELECTION" = "1" ] || fail "--initial-setup requires --verify-storage-selection"
APP_DIR=$(normalize_path "$APP_DIR")
[ -f "$APP_DIR/.env" ] || fail ".env not found in app dir: $APP_DIR"
SELECTION_FILE="$APP_DIR/data/install-control/storage-selection.json"
SELECTION_CONTROL_FILE="$APP_DIR/data/install-control/storage-selection.control"
STATUS_FILE="$APP_DIR/data/install-control/storage-apply-status.json"
ARCHIVE_ROOTS_COMPOSE_FILE="$APP_DIR/data/install-control/docker-compose.archive-roots.yml"

detect_compose

set --
if [ -n "$PROJECT_NAME" ]; then
  PROJECT_NAME=$(safe_project_name "$PROJECT_NAME")
  set -- "$@" --project-name "$PROJECT_NAME"
else
  PROJECT_NAME=$(safe_project_name "$(read_env_value COMPOSE_PROJECT_NAME)")
fi

SOURCE_DIR=$(bootstrap_command resolve-path \
  --app-dir "$APP_DIR" \
  --project-name "$PROJECT_NAME" \
  --repair) || fail "Canonical release authority could not be resolved."
case "$SOURCE_DIR" in
  "$APP_DIR"/data/update-runtime/slots/*/source) ;;
  *) fail "Canonical release authority escaped the slot layout." ;;
esac

WRITER_ISOLATION=$(writer_isolation_state)
bootstrap_command reconcile-policies \
  --app-dir "$APP_DIR" --project-name "$PROJECT_NAME" >/dev/null ||
  fail "Container restart policies could not be reconciled."
[ "$WRITER_ISOLATION" != "active" ] || [ "$VERIFY_STORAGE_SELECTION" = "0" ] ||
  fail "maintenance_writer_isolation_active"

(
  cd "$APP_DIR"
  archive_roots_compose_was_present=0
  archive_roots_compose_present && archive_roots_compose_was_present=1
  if [ "$VERIFY_STORAGE_SELECTION" = "1" ] && [ "$INITIAL_SETUP" = "1" ]; then
    prepare_initial_storage_configuration
  fi
  compose_with_archive_roots "$@" config >/dev/null
  if [ "$VERIFY_STORAGE_SELECTION" = "1" ]; then
    compose_with_archive_roots "$@" up -d --no-deps --force-recreate api recorder
  else
    reconcile_persistent_services "$@"
    apply_generated_archive_roots_compose_if_needed "$archive_roots_compose_was_present" "$@"
  fi
)

bootstrap_command reconcile-policies \
  --app-dir "$APP_DIR" --project-name "$PROJECT_NAME" >/dev/null ||
  fail "Container restart policies could not be verified."

if [ "$VERIFY_STORAGE_SELECTION" = "1" ]; then
  verify_storage_selection
fi

printf 'KM VMS restart command completed for %s\n' "$APP_DIR"
