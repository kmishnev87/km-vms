#!/usr/bin/env sh
set -eu

APP_DIR=""
HOST_ROOT=""

usage() {
  cat <<'EOF'
KM VMS storage discovery snapshot helper

Usage:
  sh scripts/km-vms-storage-discovery.sh --app-dir <path> [--host-root <mounted-host-root>]

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
    --host-root)
      [ "$#" -ge 2 ] || fail "--host-root requires a value"
      HOST_ROOT="$2"
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
CANDIDATES_CONTROL="$CONTROL_DIR/storage-discovery-candidates.control"
CANDIDATES_CONTROL_TMP="$CANDIDATES_CONTROL.tmp.$$"
MOUNTINFO_FILE="${KM_VMS_MOUNTINFO_FILE:-/proc/self/mountinfo}"
mkdir -p "$CONTROL_DIR"
trap 'rm -f "$TMP" "$CANDIDATES_TMP" "$CANDIDATES_CONTROL_TMP" "$OUT.findmnt.$$"' EXIT
: > "$CANDIDATES_CONTROL_TMP"

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
  actual_mount="$2"
  fstype="$3"
  total="$4"
  used="$5"
  free="$6"
  source="$7"
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
  elif [ ! -w "$actual_mount" ]; then
    safety="blocked"
    reason="not_writable_by_installer_user"
  else
    writable=true
  fi
  id=$(printf '%s' "$mount" | cksum | awk '{print "mount-" $1}')
  physical_identity=$(printf '%s' "$source|$fstype" | cksum | awk '{print "fs-" $1}')
  label=${mount#/}
  if [ "$first" = "1" ]; then
    first=0
  else
    printf ',\n' >> "$CANDIDATES_TMP"
  fi
  printf '    {"id":"%s","path":"%s","label":"%s","filesystem_type":"%s","physical_identity":"%s","total_bytes":%s,"used_bytes":%s,"free_bytes":%s,"writable":%s,"safety_status":"%s","reason":"%s","recommended":%s}' \
    "$(json_escape "$id")" "$(json_escape "$mount")" "$(json_escape "$label")" "$(json_escape "$fstype")" \
    "$(json_escape "$physical_identity")" "${total:-0}" "${used:-0}" "${free:-0}" "$writable" "$safety" "$(json_escape "$reason")" false >> "$CANDIDATES_TMP"
  printf '%s\t%s\t%s\t%s\t%s\n' "$id" "$mount" "$physical_identity" "$writable" "$safety" >> "$CANDIDATES_CONTROL_TMP"
}

read_findmnt_json() {
  command -v findmnt >/dev/null 2>&1 || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  findmnt --json -b -o TARGET,SOURCE,FSTYPE,SIZE,USED,AVAIL 2>/dev/null | python3 -c '
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
    source = item.get("source") or ""
    fstype = item.get("fstype") or ""
    total = int(item.get("size") or 0)
    used = int(item.get("used") or 0)
    free = int(item.get("avail") or 0)
    print(f"{target}\t{source}\t{fstype}\t{total}\t{used}\t{free}")
'
}

first=1
DISCOVERY_SOURCE="host-helper-df-proc-mounts"
if [ -n "$HOST_ROOT" ]; then
  [ -d "$HOST_ROOT" ] || fail "host root does not exist"
  DISCOVERY_SOURCE="setup-helper-host-root-volume-scan"
  for actual_mount in "$HOST_ROOT"/Volume[0-9]* "$HOST_ROOT"/volume[0-9]*; do
    [ -d "$actual_mount" ] || continue
    mount=${actual_mount#"$HOST_ROOT"}
    df_line=$(df -Pk "$actual_mount" 2>/dev/null | tail -n 1 || true)
    [ -n "$df_line" ] || continue
    if [ -r "$MOUNTINFO_FILE" ]; then
      awk -v target="$actual_mount" '$5 == target { found=1 } END { exit found ? 0 : 1 }' "$MOUNTINFO_FILE" || continue
    else
      df_mount=$(printf '%s\n' "$df_line" | awk '{print $NF}')
      [ "$df_mount" = "$actual_mount" ] || continue
    fi
    source=$(printf '%s\n' "$df_line" | awk '{print $1}')
    total=$(printf '%s\n' "$df_line" | awk '{print $2 * 1024}')
    used=$(printf '%s\n' "$df_line" | awk '{print $3 * 1024}')
    free=$(printf '%s\n' "$df_line" | awk '{print $4 * 1024}')
    fstype=$(stat -f -c '%T' "$actual_mount" 2>/dev/null || printf '')
    emit_candidate "$mount" "$actual_mount" "$fstype" "$total" "$used" "$free" "$source"
  done
elif read_findmnt_json > "$OUT.findmnt.$$"; then
  DISCOVERY_SOURCE="host-helper-findmnt-json"
  while IFS='	' read -r mount source fstype total used free; do
    emit_candidate "$mount" "$mount" "$fstype" "$total" "$used" "$free" "$source"
  done < "$OUT.findmnt.$$"
else
  df -Pk 2>/dev/null | awk 'NR > 1 {source=$1; total=$2*1024; used=$3*1024; free=$4*1024; mount=$0; sub(/^([^ ]+[ ]+){5}/, "", mount); print mount "\t" source "\t" total "\t" used "\t" free}' | while IFS='	' read -r mount source total used free; do
  [ -n "$mount" ] || continue
  fstype=""
  if [ -r /proc/mounts ]; then
    fstype=$(awk -v m="$mount" '$2 == m {print $3; exit}' /proc/mounts 2>/dev/null || true)
  fi
  emit_candidate "$mount" "$mount" "$fstype" "$total" "$used" "$free" "$source"
  done
fi
rm -f "$OUT.findmnt.$$"

created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
snapshot_seed=$(cksum "$CANDIDATES_CONTROL_TMP" 2>/dev/null | awk '{print $1}' || printf '0')
snapshot_id="snapshot-${snapshot_seed}-$$"
{
  printf '{\n'
  printf '  "schema_version": 2,\n'
  printf '  "snapshot_id": "%s",\n' "$(json_escape "$snapshot_id")"
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
mv "$CANDIDATES_CONTROL_TMP" "$CANDIDATES_CONTROL"
chmod 600 "$OUT" 2>/dev/null || true
chmod 600 "$CANDIDATES_CONTROL" 2>/dev/null || true
printf 'Storage discovery snapshot written: %s\n' "$OUT"
