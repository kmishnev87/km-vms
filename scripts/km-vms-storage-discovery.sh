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

emit_candidate() {
  mount="$1"
  fstype="$2"
  total="$3"
  used="$4"
  free="$5"
  [ -n "$mount" ] || return 0
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
  id=$(printf '%s' "$mount" | cksum | awk '{print "mount-" $1}')
  if [ "$first" = "1" ]; then
    first=0
  else
    printf ',\n' >> "$CANDIDATES_TMP"
  fi
  printf '    {"id":"%s","path":"%s","label":"%s","filesystem_type":"%s","total_bytes":%s,"used_bytes":%s,"free_bytes":%s,"writable":%s,"safety_status":"%s","reason":"%s","recommended":%s}' \
    "$(json_escape "$id")" "$(json_escape "$mount")" "$(json_escape "$mount")" "$(json_escape "$fstype")" \
    "${total:-0}" "${used:-0}" "${free:-0}" "$writable" "$safety" "$(json_escape "$reason")" false >> "$CANDIDATES_TMP"
}

read_findmnt_json() {
  command -v findmnt >/dev/null 2>&1 || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  findmnt --json -b -o TARGET,FSTYPE,SIZE,USED,AVAIL 2>/dev/null | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)

def walk(items):
    for item in items or []:
        yield item
        yield from walk(item.get("children") or [])

for item in walk(payload.get("filesystems") or []):
    target = item.get("target") or ""
    if not target:
        continue
    fstype = item.get("fstype") or ""
    total = int(item.get("size") or 0)
    used = int(item.get("used") or 0)
    free = int(item.get("avail") or 0)
    print(f"{target}\t{fstype}\t{total}\t{used}\t{free}")
'
}

first=1
DISCOVERY_SOURCE="host-helper-df-proc-mounts"
if read_findmnt_json > "$OUT.findmnt.$$"; then
  DISCOVERY_SOURCE="host-helper-findmnt-json"
  while IFS='	' read -r mount fstype total used free; do
    emit_candidate "$mount" "$fstype" "$total" "$used" "$free"
  done < "$OUT.findmnt.$$"
else
  df -Pk 2>/dev/null | awk 'NR > 1 {total=$2*1024; used=$3*1024; free=$4*1024; mount=$0; sub(/^([^ ]+[ ]+){5}/, "", mount); print mount "\t" total "\t" used "\t" free}' | while IFS='	' read -r mount total used free; do
  [ -n "$mount" ] || continue
  fstype=""
  if [ -r /proc/mounts ]; then
    fstype=$(awk -v m="$mount" '$2 == m {print $3; exit}' /proc/mounts 2>/dev/null || true)
  fi
  emit_candidate "$mount" "$fstype" "$total" "$used" "$free"
  done
fi
rm -f "$OUT.findmnt.$$"

created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
{
  printf '{\n'
  printf '  "schema_version": 1,\n'
  printf '  "created_at": "%s",\n' "$(json_escape "$created_at")"
  printf '  "discovery_source": "%s",\n' "$DISCOVERY_SOURCE"
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
