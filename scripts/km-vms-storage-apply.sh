#!/usr/bin/env sh
set -eu

APP_DIR=""
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-${KMVMS_DOCKER_COMPOSE:-}}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

usage() {
  cat <<'EOF'
KM VMS storage apply helper

Usage:
  sh scripts/km-vms-storage-apply.sh --app-dir <path>

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

detect_compose() {
  km_vms_detect_compose "$DOCKER_COMPOSE_BIN"
}

compose_config_check() {
  [ -f "$APP_DIR/docker-compose.yml" ] || return 0
  if detect_compose; then
    if [ -f "$ARCHIVE_ROOTS_COMPOSE_FILE" ]; then
      (cd "$APP_DIR" && km_vms_compose_cmd --env-file "$ENV_FILE" -f "$APP_DIR/docker-compose.yml" -f "$ARCHIVE_ROOTS_COMPOSE_FILE" config >/dev/null)
    else
      (cd "$APP_DIR" && km_vms_compose_cmd --env-file "$ENV_FILE" -f "$APP_DIR/docker-compose.yml" config >/dev/null)
    fi
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-dir)
      [ "$#" -ge 2 ] || fail "--app-dir requires a value"
      APP_DIR="$2"
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
ARCHIVE_ROOTS_COMPOSE_FILE="$APP_DIR/data/install-control/docker-compose.archive-roots.yml"
[ -f "$ENV_FILE" ] || fail ".env not found"
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

restore_env_on_failure() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$status" -ne 0 ] && [ "$ENV_CHANGED" = "1" ] && [ -f "$backup" ]; then
    restore_tmp="$ENV_FILE.restore.$$"
    if cp "$backup" "$restore_tmp"; then
      chmod --reference="$ENV_FILE" "$restore_tmp" 2>/dev/null || chmod 600 "$restore_tmp" 2>/dev/null || true
      mv "$restore_tmp" "$ENV_FILE" || rm -f "$restore_tmp"
    else
      rm -f "$restore_tmp"
    fi
  fi
  exit "$status"
}

trap restore_env_on_failure EXIT
trap 'exit 1' HUP INT TERM
cp "$ENV_FILE" "$backup"
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
