#!/usr/bin/env sh
set -eu

APP_DIR=""

usage() {
  cat <<'EOF'
KM VMS storage discovery snapshot helper

Usage:
  sh scripts/km-vms-storage-discovery.sh --app-dir <path>

Writes a non-secret storage-discovery.json snapshot under:
  <app-dir>/data/install-control/storage-discovery.json
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
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
[ -d "$APP_DIR" ] || fail "App dir does not exist: $APP_DIR"

CONTROL_DIR="$APP_DIR/data/install-control"
OUT="$CONTROL_DIR/storage-discovery.json"
TMP="$OUT.tmp.$$"
CANDIDATES_TMP="$OUT.candidates.$$"
mkdir -p "$CONTROL_DIR"
trap 'rm -f "$TMP" "$CANDIDATES_TMP"' EXIT

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

is_blocked_path() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/lib|/lib64|/proc|/run|/sbin|/sys|/usr|/var|/root|/tmp|/var/lib/docker|/var/lib/docker/*) return 0 ;;
    "$APP_DIR"|"$APP_DIR"/*) return 0 ;;
    *) return 1 ;;
  esac
}

is_blocked_fstype() {
  case "$1" in
    proc|sysfs|devtmpfs|devpts|cgroup|cgroup2|tmpfs|overlay|squashfs|aufs|debugfs|tracefs|securityfs|pstore|configfs|mqueue|hugetlbfs|fusectl|autofs) return 0 ;;
    *) return 1 ;;
  esac
}

first=1
df -Pk 2>/dev/null | awk 'NR > 1 {print $6 "|" $2 "|" $3 "|" $4}' | while IFS='|' read -r mount total_k used_k free_k; do
  [ -n "$mount" ] || continue
  fstype=""
  if [ -r /proc/mounts ]; then
    fstype=$(awk -v m="$mount" '$2 == m {print $3; exit}' /proc/mounts 2>/dev/null || true)
  fi
  safety="allowed"
  reason=""
  writable=false
  if is_blocked_path "$mount"; then
    safety="blocked"
    reason="dangerous_or_internal_path"
  elif is_blocked_fstype "$fstype"; then
    safety="blocked"
    reason="unsupported_pseudo_filesystem"
  elif [ ! -w "$mount" ]; then
    safety="blocked"
    reason="not_writable_by_installer_user"
  else
    writable=true
  fi
  total=$((total_k * 1024))
  used=$((used_k * 1024))
  free=$((free_k * 1024))
  id=$(printf '%s' "$mount" | cksum | awk '{print "mount-" $1}')
  if [ "$first" = "1" ]; then
    first=0
  else
    printf ',\n' >> "$CANDIDATES_TMP"
  fi
  printf '    {"id":"%s","path":"%s","label":"%s","filesystem_type":"%s","total_bytes":%s,"used_bytes":%s,"free_bytes":%s,"writable":%s,"safety_status":"%s","reason":"%s","recommended":%s}' \
    "$(json_escape "$id")" "$(json_escape "$mount")" "$(json_escape "$mount")" "$(json_escape "$fstype")" \
    "$total" "$used" "$free" "$writable" "$safety" "$(json_escape "$reason")" false >> "$CANDIDATES_TMP"
done

created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
{
  printf '{\n'
  printf '  "schema_version": 1,\n'
  printf '  "created_at": "%s",\n' "$(json_escape "$created_at")"
  printf '  "discovery_source": "host-helper-df-proc-mounts",\n'
  printf '  "host_visibility": true,\n'
  printf '  "candidates": [\n'
  if [ -f "$CANDIDATES_TMP" ]; then
    cat "$CANDIDATES_TMP"
  fi
  printf '\n  ]\n'
  printf '}\n'
} > "$TMP"
mv "$TMP" "$OUT"
chmod 600 "$OUT" 2>/dev/null || true
printf 'Storage discovery snapshot written: %s\n' "$OUT"
