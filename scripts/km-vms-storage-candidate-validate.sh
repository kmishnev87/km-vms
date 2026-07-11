#!/usr/bin/env sh
set -eu

FOLDER_NAME=""
SELECTED_ROOT="${KM_VMS_SELECTED_MOUNT_CONTAINER:-/selected-root}"

fail() {
  printf 'error=%s\n' "$1"
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --folder-name)
      [ "$#" -ge 2 ] || fail "folder_name_required"
      FOLDER_NAME="$2"
      shift 2
      ;;
    --help|-h)
      printf 'Usage: sh scripts/km-vms-storage-candidate-validate.sh --folder-name <name>\n'
      exit 0
      ;;
    *)
      fail "unknown_option"
      ;;
  esac
done

case "$FOLDER_NAME" in
  ''|.|..|.*|*/*|*\\*|*'"'*) fail "folder_name_invalid" ;;
esac
printf '%s' "$FOLDER_NAME" | LC_ALL=C grep '[[:cntrl:]]' >/dev/null 2>&1 && fail "folder_name_invalid"

[ -d "$SELECTED_ROOT" ] || fail "storage_candidate_disappeared"
[ -r "$SELECTED_ROOT" ] || fail "storage_candidate_not_readable"
[ -x "$SELECTED_ROOT" ] || fail "storage_candidate_not_searchable"
[ -w "$SELECTED_ROOT" ] || fail "storage_candidate_not_writable"

probe="$SELECTED_ROOT/.km-vms-candidate-probe.$$"
printf 'km-vms-candidate-probe\n' > "$probe" || fail "storage_candidate_not_writable"
readback=$(cat "$probe" 2>/dev/null || true)
rm -f "$probe" || fail "storage_candidate_probe_cleanup_failed"
[ ! -e "$probe" ] || fail "storage_candidate_probe_cleanup_failed"
[ "$readback" = "km-vms-candidate-probe" ] || fail "storage_candidate_probe_readback_failed"

target="$SELECTED_ROOT/$FOLDER_NAME"
exists=false
is_empty=false
has_marker=false
if [ -e "$target" ]; then
  exists=true
  [ -d "$target" ] || fail "target_exists_not_directory"
  [ ! -L "$target" ] || fail "target_is_symlink"
  [ -r "$target" ] || fail "target_not_readable"
  [ -x "$target" ] || fail "target_not_searchable"
  if [ -f "$target/.km-vms-storage-root.json" ]; then
    has_marker=true
  fi
  if [ "$(find "$target" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" -eq 0 ]; then
    is_empty=true
  elif [ "$has_marker" != "true" ] && [ ! -d "$target/kmvms/recordings" ]; then
    fail "non_empty_unmarked_folder"
  fi
fi

printf 'writable=true\n'
printf 'exists=%s\n' "$exists"
printf 'is_empty=%s\n' "$is_empty"
printf 'has_km_vms_marker=%s\n' "$has_marker"
