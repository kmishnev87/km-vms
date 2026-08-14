#!/usr/bin/env sh
set -eu

APP_DIR=""
INITIAL_SETUP=0
RECOVERY_ACTION=""
RECOVERY_REQUEST_ID=""
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-${KMVMS_DOCKER_COMPOSE:-}}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

usage() {
  cat <<'EOF'
KM VMS storage apply helper

Usage:
  sh scripts/km-vms-storage-apply.sh --app-dir <path> [--initial-setup]
  sh scripts/km-vms-storage-apply.sh --app-dir <path> --restore-initial-recovery <request-id>
  sh scripts/km-vms-storage-apply.sh --app-dir <path> --cleanup-initial-recovery <request-id>

Reads data/install-control/storage-selection.control and updates only the
SURVEILLANCE_ROOT line in .env. Does not print .env contents or secrets.
Restart containers after running this helper.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

. "$SCRIPT_DIR/km-vms-compose-common.sh"

safe_realpath() {
  value="$1"
  if command -v realpath >/dev/null 2>&1; then
    if realpath -m "$value" >/dev/null 2>&1; then
      realpath -m "$value"
      return
    fi
    if [ -e "$value" ]; then
      realpath "$value"
      return
    fi
  fi
  if [ -d "$value" ]; then
    (cd "$value" && pwd -P)
    return
  fi
  parent=$(dirname "$value")
  base=$(basename "$value")
  [ -d "$parent" ] || fail "path parent does not exist"
  printf '%s/%s\n' "$(cd "$parent" && pwd -P)" "$base"
}

same_or_child() {
  parent="$1"
  child="$2"
  [ "$parent" = "$child" ] && return 0
  case "$child" in
    "$parent"/*) return 0 ;;
    *) return 1 ;;
  esac
}

reject_sensitive_path() {
  case "$1" in
    /|/bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib64|/lib64/*|/proc|/proc/*|/run|/run/*|/sbin|/sbin/*|/sys|/sys/*|/usr|/usr/*|/var|/var/*|/root|/root/*|/tmp|/tmp/*|*/.git|*/.git/*|*/.env|*/.env/*|*secrets*|*credentials*) fail "selected path is unsafe for archive storage" ;;
    "$APP_DIR"|"$APP_DIR"/*) fail "selected path must not be inside app dir" ;;
  esac
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

read_control_value() {
  file="$1"
  key="$2"
  [ -f "$file" ] || fail "$(basename "$file") not found"
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

validate_recovery_request_id() {
  value="$1"
  [ -n "$value" ] || fail "storage recovery request id is required"
  printf '%s' "$value" | LC_ALL=C grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$' || fail "storage recovery request id is invalid"
}

remove_recovery_directory() {
  recovery_dir="$1"
  rm -f \
    "$recovery_dir/env.previous" \
    "$recovery_dir/manifest.previous" \
    "$recovery_dir/manifest.absent" \
    "$recovery_dir/override.previous" \
    "$recovery_dir/override.absent" \
    "$recovery_dir/request.control"
  rmdir "$recovery_dir" 2>/dev/null || return 1
  rmdir "$RECOVERY_ROOT" 2>/dev/null || true
  return 0
}

create_initial_recovery_set() {
  validate_recovery_request_id "$request_id"
  recovery_dir="$RECOVERY_ROOT/$request_id"
  if [ -d "$recovery_dir" ]; then
    saved_request=$(read_control_value "$recovery_dir/request.control" request_id || true)
    saved_path=$(read_control_value "$recovery_dir/request.control" selected_host_path || true)
    [ "$saved_request" = "$request_id" ] || fail "storage recovery request mismatch"
    [ "$saved_path" = "$selected_path" ] || fail "storage recovery selected path mismatch"
    return 0
  fi

  umask 077
  mkdir -p "$RECOVERY_ROOT"
  chmod 700 "$RECOVERY_ROOT" 2>/dev/null || true
  recovery_tmp="$RECOVERY_ROOT/.new-$request_id-$$"
  mkdir "$recovery_tmp"
  chmod 700 "$recovery_tmp" 2>/dev/null || true
  if ! cp "$ENV_FILE" "$recovery_tmp/env.previous"; then
    remove_recovery_directory "$recovery_tmp" 2>/dev/null || true
    fail "cannot preserve initial storage environment"
  fi
  chmod 600 "$recovery_tmp/env.previous" 2>/dev/null || true
  if [ -f "$ARCHIVE_ROOTS_MANIFEST_FILE" ]; then
    if ! cp "$ARCHIVE_ROOTS_MANIFEST_FILE" "$recovery_tmp/manifest.previous"; then
      remove_recovery_directory "$recovery_tmp" 2>/dev/null || true
      fail "cannot preserve archive roots manifest"
    fi
  else
    : > "$recovery_tmp/manifest.absent"
  fi
  if [ -f "$ARCHIVE_ROOTS_COMPOSE_FILE" ]; then
    if ! cp "$ARCHIVE_ROOTS_COMPOSE_FILE" "$recovery_tmp/override.previous"; then
      remove_recovery_directory "$recovery_tmp" 2>/dev/null || true
      fail "cannot preserve archive roots compose override"
    fi
  else
    : > "$recovery_tmp/override.absent"
  fi
  if ! {
    printf 'schema_version=1\n'
    printf 'request_id=%s\n' "$request_id"
    printf 'selected_host_path=%s\n' "$selected_path"
  } > "$recovery_tmp/request.control"; then
    remove_recovery_directory "$recovery_tmp" 2>/dev/null || true
    fail "cannot publish initial storage recovery identity"
  fi
  chmod 600 "$recovery_tmp"/* 2>/dev/null || true
  if ! mv "$recovery_tmp" "$recovery_dir"; then
    remove_recovery_directory "$recovery_tmp" 2>/dev/null || true
    fail "cannot publish initial storage recovery set"
  fi
}

initial_recovery_set_matches() {
  recovery_dir="$1"
  cmp -s "$recovery_dir/env.previous" "$ENV_FILE" || return 1

  if [ -f "$recovery_dir/manifest.previous" ] && [ ! -e "$recovery_dir/manifest.absent" ]; then
    cmp -s "$recovery_dir/manifest.previous" "$ARCHIVE_ROOTS_MANIFEST_FILE" || return 1
  elif [ -f "$recovery_dir/manifest.absent" ] && [ ! -e "$recovery_dir/manifest.previous" ]; then
    [ ! -e "$ARCHIVE_ROOTS_MANIFEST_FILE" ] && [ ! -L "$ARCHIVE_ROOTS_MANIFEST_FILE" ] || return 1
  else
    return 1
  fi

  if [ -f "$recovery_dir/override.previous" ] && [ ! -e "$recovery_dir/override.absent" ]; then
    cmp -s "$recovery_dir/override.previous" "$ARCHIVE_ROOTS_COMPOSE_FILE" || return 1
  elif [ -f "$recovery_dir/override.absent" ] && [ ! -e "$recovery_dir/override.previous" ]; then
    [ ! -e "$ARCHIVE_ROOTS_COMPOSE_FILE" ] && [ ! -L "$ARCHIVE_ROOTS_COMPOSE_FILE" ] || return 1
  else
    return 1
  fi
  return 0
}

restore_initial_recovery_set() {
  recovery_request="$1"
  validate_recovery_request_id "$recovery_request"
  recovery_dir="$RECOVERY_ROOT/$recovery_request"
  [ -d "$recovery_dir" ] || return 0
  saved_request=$(read_control_value "$recovery_dir/request.control" request_id || true)
  [ "$saved_request" = "$recovery_request" ] || return 1
  [ -f "$recovery_dir/env.previous" ] || return 1
  { [ -f "$recovery_dir/manifest.previous" ] || [ -f "$recovery_dir/manifest.absent" ]; } || return 1
  { [ -f "$recovery_dir/override.previous" ] || [ -f "$recovery_dir/override.absent" ]; } || return 1

  env_restore="$ENV_FILE.restore.$$"
  if ! cp "$recovery_dir/env.previous" "$env_restore"; then
    rm -f "$env_restore"
    return 1
  fi
  chmod --reference="$ENV_FILE" "$env_restore" 2>/dev/null || chmod 600 "$env_restore" 2>/dev/null || true
  if ! mv "$env_restore" "$ENV_FILE"; then
    rm -f "$env_restore"
    return 1
  fi

  if [ -f "$recovery_dir/manifest.previous" ]; then
    manifest_restore="$ARCHIVE_ROOTS_MANIFEST_FILE.restore.$$"
    if ! cp "$recovery_dir/manifest.previous" "$manifest_restore" || ! mv "$manifest_restore" "$ARCHIVE_ROOTS_MANIFEST_FILE"; then
      rm -f "$manifest_restore"
      return 1
    fi
  else
    rm -f "$ARCHIVE_ROOTS_MANIFEST_FILE" || return 1
  fi
  if [ -f "$recovery_dir/override.previous" ]; then
    override_restore="$ARCHIVE_ROOTS_COMPOSE_FILE.restore.$$"
    if ! cp "$recovery_dir/override.previous" "$override_restore" || ! mv "$override_restore" "$ARCHIVE_ROOTS_COMPOSE_FILE"; then
      rm -f "$override_restore"
      return 1
    fi
  else
    rm -f "$ARCHIVE_ROOTS_COMPOSE_FILE" || return 1
  fi
  initial_recovery_set_matches "$recovery_dir" || return 1
  rm -f "$RUNTIME_CONVERGENCE_FILE" "$RUNTIME_CONVERGENCE_CONTROL_FILE" || return 1
  remove_recovery_directory "$recovery_dir"
}

cleanup_initial_recovery_set() {
  recovery_request="$1"
  validate_recovery_request_id "$recovery_request"
  recovery_dir="$RECOVERY_ROOT/$recovery_request"
  [ ! -d "$recovery_dir" ] || remove_recovery_directory "$recovery_dir"
  rm -f "$APP_DIR/.env.stage2-storage.bak"
}

detect_compose() {
  km_vms_detect_compose "$DOCKER_COMPOSE_BIN"
}

compose_config_check() {
  if ! detect_compose; then
    printf 'ERROR: Docker Compose is unavailable; storage Compose configuration was not validated.\n' >&2
    return 1
  fi
  active_pointer="$APP_DIR/data/update-runtime/active"
  if [ -e "$active_pointer" ] || [ -L "$active_pointer" ]; then
    product_source=$(km_vms_resolve_product_source "$APP_DIR")
    (cd "$APP_DIR" && km_vms_compose_for_source "$APP_DIR" "$product_source" config >/dev/null)
    return 0
  fi
  [ "$INITIAL_SETUP" = "1" ] ||
    fail "canonical active release is required for storage Compose validation"
  product_source=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
  [ -f "$product_source/docker-compose.yml" ] ||
    fail "explicit installation source is missing docker-compose.yml"
  (
    cd "$APP_DIR"
    KM_VMS_ALLOW_PREBOOTSTRAP_COMPOSE=1 \
      km_vms_compose_for_source "$APP_DIR" "$product_source" config >/dev/null
  )
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-dir)
      [ "$#" -ge 2 ] || fail "--app-dir requires a value"
      APP_DIR="$2"
      shift 2
      ;;
    --initial-setup)
      INITIAL_SETUP=1
      shift
      ;;
    --restore-initial-recovery)
      [ "$#" -ge 2 ] || fail "--restore-initial-recovery requires a request id"
      RECOVERY_ACTION="restore"
      RECOVERY_REQUEST_ID="$2"
      shift 2
      ;;
    --cleanup-initial-recovery)
      [ "$#" -ge 2 ] || fail "--cleanup-initial-recovery requires a request id"
      RECOVERY_ACTION="cleanup"
      RECOVERY_REQUEST_ID="$2"
      shift 2
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

[ -n "$APP_DIR" ] || fail "--app-dir is required"
ENV_FILE="$APP_DIR/.env"
SELECTION_FILE="$APP_DIR/data/install-control/storage-selection.control"
STATUS_FILE="$APP_DIR/data/install-control/storage-apply-status.json"
ARCHIVE_ROOTS_MANIFEST_FILE="$APP_DIR/data/install-control/archive-roots-runtime.json"
ARCHIVE_ROOTS_COMPOSE_FILE="$APP_DIR/data/install-control/docker-compose.archive-roots.yml"
RUNTIME_CONVERGENCE_FILE="$APP_DIR/data/install-control/storage-runtime-convergence.json"
RUNTIME_CONVERGENCE_CONTROL_FILE="$APP_DIR/data/install-control/storage-runtime-convergence.control"
RECOVERY_ROOT="$APP_DIR/data/install-control/.storage-activation-recovery"
[ -f "$ENV_FILE" ] || fail ".env not found"
[ -z "$RECOVERY_ACTION" ] || {
  if [ "$RECOVERY_ACTION" = "restore" ]; then
    restore_initial_recovery_set "$RECOVERY_REQUEST_ID" || fail "initial storage configuration recovery failed"
  else
    cleanup_initial_recovery_set "$RECOVERY_REQUEST_ID" || fail "initial storage recovery cleanup failed"
  fi
  exit 0
}
[ -f "$SELECTION_FILE" ] || fail "storage-selection.control not found"

selected_path=$(read_control_value "$SELECTION_FILE" selected_host_path || true)
selected_mount=$(read_control_value "$SELECTION_FILE" selected_mount_path || true)
folder_name=$(read_control_value "$SELECTION_FILE" folder_name || true)
apply_status=$(read_control_value "$SELECTION_FILE" apply_status || true)
request_id=$(read_control_value "$SELECTION_FILE" activation_request_id || true)
operation_id=$(read_control_value "$SELECTION_FILE" operation_id || true)
[ -n "$selected_path" ] || fail "selected_host_path not found in selection control file"
[ -n "$selected_mount" ] || fail "selected_mount_path not found in selection control file"
[ -n "$folder_name" ] || fail "folder_name not found in selection control file"
[ -z "$apply_status" ] || [ "$apply_status" = "pending_host_helper_restart_required" ] || [ "$apply_status" = "activation_requested" ] || fail "selection is not pending storage activation"
case "$selected_path" in
  /*) ;;
  *) fail "selected_host_path must be absolute" ;;
esac
case "$selected_mount" in
  /*) ;;
  *) fail "selected_mount_path must be absolute" ;;
esac
case "$folder_name" in
  ''|.|..|*/*|*\\*) fail "folder_name must be a single folder name" ;;
esac
printf '%s' "$folder_name" | LC_ALL=C grep '[[:cntrl:]]' >/dev/null 2>&1 && fail "folder_name contains control characters"
case "$selected_path" in
  "$selected_mount/$folder_name") ;;
  *) fail "selected_host_path must match selected_mount_path/folder_name" ;;
esac
fs_selected_mount="${KM_VMS_SELECTED_MOUNT_CONTAINER:-$selected_mount}"
fs_selected_path="${KM_VMS_SELECTED_PATH_CONTAINER:-$selected_path}"
case "$fs_selected_mount" in
  /*) ;;
  *) fail "filesystem selected mount must be absolute" ;;
esac
case "$fs_selected_path" in
  "$fs_selected_mount/$folder_name") ;;
  *) fail "filesystem selected path must match filesystem mount/folder_name" ;;
esac
selected_mount_real=$(safe_realpath "$fs_selected_mount")
selected_path_real=$(safe_realpath "$fs_selected_path")
reject_sensitive_path "$selected_mount_real"
reject_sensitive_path "$selected_path"
same_or_child "$selected_mount_real" "$selected_path_real" || fail "selected_host_path escapes selected_mount_path"
[ "$selected_mount_real" != "$selected_path_real" ] || fail "selected_host_path must be a child folder"
[ -d "$selected_mount_real" ] || fail "selected_mount_path does not exist"
[ -r "$selected_mount_real" ] || fail "selected_mount_path is not readable"
[ -x "$selected_mount_real" ] || fail "selected_mount_path is not searchable"
[ -w "$selected_mount_real" ] || fail "selected_mount_path is not writable"
[ ! -L "$fs_selected_mount" ] || fail "selected_mount_path must not be a symlink"
if [ -e "$fs_selected_path" ]; then
  [ -d "$fs_selected_path" ] || fail "selected_host_path exists and is not a directory"
  [ ! -L "$fs_selected_path" ] || fail "selected_host_path must not be a symlink"
  [ -r "$fs_selected_path" ] || fail "selected_host_path is not readable"
  [ -x "$fs_selected_path" ] || fail "selected_host_path is not searchable"
  find "$fs_selected_path" -mindepth 1 -maxdepth 1 >/dev/null || fail "cannot list selected_host_path"
  if [ "$(find "$fs_selected_path" -mindepth 1 -maxdepth 1 ! -name '.km-vms-storage-root.json' | wc -l | tr -d ' ')" -gt 0 ] && [ ! -f "$fs_selected_path/.km-vms-storage-root.json" ]; then
    if [ -d "$fs_selected_path/kmvms/recordings" ] && [ ! -L "$fs_selected_path/kmvms" ] && [ ! -L "$fs_selected_path/kmvms/recordings" ]; then
      :
    else
      fail "selected_host_path is non-empty and has no KM VMS marker or recordings namespace"
    fi
  fi
else
  [ -d "$selected_mount_real" ] || fail "selected_mount_path does not exist"
  mkdir "$fs_selected_path" || fail "cannot create selected_host_path"
  chmod 750 "$fs_selected_path" 2>/dev/null || true
fi
[ -r "$fs_selected_path" ] || fail "selected_host_path is not readable"
[ -x "$fs_selected_path" ] || fail "selected_host_path is not searchable"
[ -w "$fs_selected_path" ] || fail "selected_host_path is not writable"
probe="$fs_selected_path/.km-vms-write-test.$$"
printf 'km-vms-storage-write-test\n' > "$probe" || fail "cannot write test file"
if command -v sync >/dev/null 2>&1; then
  sync "$probe" 2>/dev/null || sync 2>/dev/null || true
fi
readback=$(cat "$probe" 2>/dev/null || true)
rm -f "$probe" || fail "cannot remove write test file"
[ ! -e "$probe" ] || fail "write test file was not removed"
[ "$readback" = "km-vms-storage-write-test" ] || fail "write test readback failed"
if [ ! -f "$fs_selected_path/.km-vms-storage-root.json" ]; then
  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "product": "KM VMS",\n'
    printf '  "created_at": "%s",\n' "$created_at"
    printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
    printf '  "container_archive_path": "/storage/archive"\n'
    printf '}\n'
  } > "$fs_selected_path/.km-vms-storage-root.json" || fail "cannot write KM VMS storage marker"
fi
namespace_path="$fs_selected_path/kmvms/recordings"
if [ -e "$fs_selected_path/kmvms" ] && [ ! -d "$fs_selected_path/kmvms" ]; then
  fail "KM VMS namespace parent exists and is not a directory"
fi
if [ -e "$namespace_path" ] && [ ! -d "$namespace_path" ]; then
  fail "KM VMS recordings namespace exists and is not a directory"
fi
[ ! -L "$fs_selected_path/kmvms" ] || fail "KM VMS namespace parent must not be a symlink"
[ ! -L "$namespace_path" ] || fail "KM VMS recordings namespace must not be a symlink"
mkdir -p "$namespace_path" || fail "cannot create KM VMS recordings namespace"
chmod 750 "$fs_selected_path/kmvms" "$namespace_path" 2>/dev/null || true

backup="$ENV_FILE.stage2-storage.bak"
tmp="$ENV_FILE.tmp.$$"
ENV_CHANGED=0

restore_configuration_on_failure() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ "$ENV_CHANGED" = "1" ]; then
    if [ "$INITIAL_SETUP" = "1" ]; then
      restore_initial_recovery_set "$request_id" || true
    elif [ -f "$backup" ]; then
      restore_tmp="$ENV_FILE.restore.$$"
      if cp "$backup" "$restore_tmp"; then
        chmod --reference="$ENV_FILE" "$restore_tmp" 2>/dev/null || chmod 600 "$restore_tmp" 2>/dev/null || true
        mv "$restore_tmp" "$ENV_FILE" || rm -f "$restore_tmp"
      else
        rm -f "$restore_tmp"
      fi
    fi
  fi
  exit "$status"
}

trap restore_configuration_on_failure EXIT
trap 'exit 1' HUP INT TERM
if [ "$INITIAL_SETUP" = "1" ]; then
  create_initial_recovery_set
else
  cp "$ENV_FILE" "$backup"
fi
awk -v value="$selected_path" '
  BEGIN { done = 0 }
  /^SURVEILLANCE_ROOT=/ { print "SURVEILLANCE_ROOT=" value; done = 1; next }
  { print }
  END { if (!done) print "SURVEILLANCE_ROOT=" value }
' "$ENV_FILE" > "$tmp"
chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp" 2>/dev/null || true
mv "$tmp" "$ENV_FILE"
ENV_CHANGED=1
if ! compose_config_check; then
  fail "docker compose config failed after storage path apply"
fi
applied_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
status_tmp="$STATUS_FILE.tmp.$$"
{
  printf '{\n'
  printf '  "schema_version": 1,\n'
  printf '  "status": "applied_restart_required",\n'
  printf '  "request_id": "%s",\n' "$(json_escape "$request_id")"
  printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id")"
  printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
  printf '  "container_archive_path": "/storage/archive",\n'
  printf '  "applied_at": "%s",\n' "$(json_escape "$applied_at")"
  printf '  "next_action": "restart_km_vms_containers"\n'
  printf '}\n'
} > "$status_tmp"
mv "$status_tmp" "$STATUS_FILE"
chmod 600 "$STATUS_FILE" 2>/dev/null || true
ENV_CHANGED=0
trap - EXIT HUP INT TERM
printf 'Storage host path applied. Status: applied_restart_required. Restart KM VMS containers to use the new bind mount.\n'
