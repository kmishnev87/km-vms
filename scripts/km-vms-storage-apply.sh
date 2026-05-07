#!/usr/bin/env sh
set -eu

APP_DIR=""
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-${KMVMS_DOCKER_COMPOSE:-}}"

usage() {
  cat <<'EOF'
KM VMS storage apply helper

Usage:
  sh scripts/km-vms-storage-apply.sh --app-dir <path>

Reads data/install-control/storage-selection.json and updates only the
SURVEILLANCE_ROOT line in .env. Does not print .env contents or secrets.
Restart containers after running this helper.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

safe_realpath() {
  value="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath -m "$value"
    return
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

detect_compose() {
  if [ -n "$DOCKER_COMPOSE_BIN" ]; then
    if [ "$DOCKER_COMPOSE_BIN" = "docker compose" ]; then
      command -v docker >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE=\"docker compose\" but docker was not found"
      docker compose version >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE=\"docker compose\" but docker compose is not available"
      COMPOSE_KIND="plugin"
      COMPOSE_BIN="docker"
      return 0
    fi
    case "$DOCKER_COMPOSE_BIN" in
      *[\;\|\&\`\>\<\(\)]*|*'$('*|*'$'*|*" "*|*"	"*) fail "KM_VMS_DOCKER_COMPOSE contains unsafe characters or spaces" ;;
    esac
    if [ "$DOCKER_COMPOSE_BIN" = "docker" ]; then
      command -v docker >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE=docker but docker was not found"
      docker compose version >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE=docker but docker compose is not available"
      COMPOSE_KIND="plugin"
      COMPOSE_BIN="docker"
      return 0
    fi
    if [ "$DOCKER_COMPOSE_BIN" = "docker-compose" ]; then
      command -v docker-compose >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE=docker-compose but docker-compose was not found"
      COMPOSE_KIND="standalone"
      COMPOSE_BIN="docker-compose"
      return 0
    fi
    [ -x "$DOCKER_COMPOSE_BIN" ] || fail "KM_VMS_DOCKER_COMPOSE must be docker, docker-compose, docker compose, or an executable path"
    COMPOSE_KIND="standalone"
    COMPOSE_BIN="$DOCKER_COMPOSE_BIN"
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_KIND="plugin"
    COMPOSE_BIN="docker"
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_KIND="standalone"
    COMPOSE_BIN="docker-compose"
    return 0
  fi
  return 1
}

compose_config_check() {
  [ -f "$APP_DIR/docker-compose.yml" ] || return 0
  if detect_compose; then
    if [ "$COMPOSE_KIND" = "plugin" ]; then
      (cd "$APP_DIR" && docker compose --env-file "$ENV_FILE" config >/dev/null)
    else
      (cd "$APP_DIR" && "$COMPOSE_BIN" --env-file "$ENV_FILE" config >/dev/null)
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
SELECTION_FILE="$APP_DIR/data/install-control/storage-selection.json"
STATUS_FILE="$APP_DIR/data/install-control/storage-apply-status.json"
[ -f "$ENV_FILE" ] || fail ".env not found"
[ -f "$SELECTION_FILE" ] || fail "storage-selection.json not found"

selected_path=$(sed -n 's/.*"selected_host_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SELECTION_FILE" | head -n 1)
selected_mount=$(sed -n 's/.*"selected_mount_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SELECTION_FILE" | head -n 1)
folder_name=$(sed -n 's/.*"folder_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SELECTION_FILE" | head -n 1)
apply_status=$(sed -n 's/.*"apply_status"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SELECTION_FILE" | head -n 1)
[ -n "$selected_path" ] || fail "selected_host_path not found in selection file"
[ -n "$selected_mount" ] || fail "selected_mount_path not found in selection file"
[ -n "$folder_name" ] || fail "folder_name not found in selection file"
[ -z "$apply_status" ] || [ "$apply_status" = "pending_host_helper_restart_required" ] || fail "selection is not pending host helper apply"
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
selected_mount_real=$(safe_realpath "$selected_mount")
selected_path_real=$(safe_realpath "$selected_path")
reject_sensitive_path "$selected_mount_real"
reject_sensitive_path "$selected_path_real"
same_or_child "$selected_mount_real" "$selected_path_real" || fail "selected_host_path escapes selected_mount_path"
[ "$selected_mount_real" != "$selected_path_real" ] || fail "selected_host_path must be a child folder"
[ -d "$selected_mount_real" ] || fail "selected_mount_path does not exist"
[ -r "$selected_mount_real" ] || fail "selected_mount_path is not readable"
[ -x "$selected_mount_real" ] || fail "selected_mount_path is not searchable"
[ -w "$selected_mount_real" ] || fail "selected_mount_path is not writable"
[ ! -L "$selected_mount" ] || fail "selected_mount_path must not be a symlink"
if [ -e "$selected_path" ]; then
  [ -d "$selected_path" ] || fail "selected_host_path exists and is not a directory"
  [ ! -L "$selected_path" ] || fail "selected_host_path must not be a symlink"
  [ -r "$selected_path" ] || fail "selected_host_path is not readable"
  [ -x "$selected_path" ] || fail "selected_host_path is not searchable"
  find "$selected_path" -mindepth 1 -maxdepth 1 >/dev/null || fail "cannot list selected_host_path"
  if [ "$(find "$selected_path" -mindepth 1 -maxdepth 1 ! -name '.km-vms-storage-root.json' | wc -l | tr -d ' ')" -gt 0 ] && [ ! -f "$selected_path/.km-vms-storage-root.json" ]; then
    fail "selected_host_path is non-empty and has no KM VMS marker"
  fi
else
  [ -d "$selected_mount_real" ] || fail "selected_mount_path does not exist"
  mkdir "$selected_path" || fail "cannot create selected_host_path"
  chmod 750 "$selected_path" 2>/dev/null || true
fi
[ -r "$selected_path" ] || fail "selected_host_path is not readable"
[ -x "$selected_path" ] || fail "selected_host_path is not searchable"
[ -w "$selected_path" ] || fail "selected_host_path is not writable"
probe="$selected_path/.km-vms-write-test.$$"
printf 'km-vms-storage-write-test\n' > "$probe" || fail "cannot write test file"
if command -v sync >/dev/null 2>&1; then
  sync "$probe" 2>/dev/null || sync 2>/dev/null || true
fi
readback=$(cat "$probe" 2>/dev/null || true)
rm -f "$probe" || fail "cannot remove write test file"
[ ! -e "$probe" ] || fail "write test file was not removed"
[ "$readback" = "km-vms-storage-write-test" ] || fail "write test readback failed"
if [ ! -f "$selected_path/.km-vms-storage-root.json" ]; then
  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "product": "KM VMS",\n'
    printf '  "created_at": "%s",\n' "$created_at"
    printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
    printf '  "container_archive_path": "/storage/archive"\n'
    printf '}\n'
  } > "$selected_path/.km-vms-storage-root.json" || fail "cannot write KM VMS storage marker"
fi

backup="$ENV_FILE.stage2-storage.bak"
tmp="$ENV_FILE.tmp.$$"
cp "$ENV_FILE" "$backup"
awk -v value="$selected_path" '
  BEGIN { done = 0 }
  /^SURVEILLANCE_ROOT=/ { print "SURVEILLANCE_ROOT=" value; done = 1; next }
  { print }
  END { if (!done) print "SURVEILLANCE_ROOT=" value }
' "$ENV_FILE" > "$tmp"
chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp" 2>/dev/null || true
mv "$tmp" "$ENV_FILE"
compose_config_check || fail "docker compose config failed after storage path apply"
applied_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
{
  printf '{\n'
  printf '  "schema_version": 1,\n'
  printf '  "status": "applied_restart_required",\n'
  printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
  printf '  "container_archive_path": "/storage/archive",\n'
  printf '  "applied_at": "%s",\n' "$(json_escape "$applied_at")"
  printf '  "next_action": "restart_km_vms_containers"\n'
  printf '}\n'
} > "$STATUS_FILE"
chmod 600 "$STATUS_FILE" 2>/dev/null || true
printf 'Storage host path applied. Status: applied_restart_required. Restart KM VMS containers to use the new bind mount.\n'
