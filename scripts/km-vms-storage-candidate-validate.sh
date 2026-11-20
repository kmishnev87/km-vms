#!/usr/bin/env sh
set -eu

FOLDER_NAME=""
ARCHIVE_HOST_PATH=""
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
    --prepare-archive)
      [ "$#" -ge 2 ] || fail "archive_attachment_target_invalid"
      ARCHIVE_HOST_PATH="$2"
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

probe=$(mktemp "$SELECTED_ROOT/.km-vms-candidate-probe.XXXXXX") || fail "storage_candidate_not_writable"
printf 'km-vms-candidate-probe\n' > "$probe" || fail "storage_candidate_not_writable"
readback=$(cat "$probe" 2>/dev/null || true)
rm -f "$probe" || fail "storage_candidate_probe_cleanup_failed"
[ ! -e "$probe" ] || fail "storage_candidate_probe_cleanup_failed"
[ "$readback" = "km-vms-candidate-probe" ] || fail "storage_candidate_probe_readback_failed"

target="$SELECTED_ROOT/$FOLDER_NAME"
[ ! -L "$target" ] || fail "target_is_symlink"
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

if [ -n "$ARCHIVE_HOST_PATH" ]; then
  case "$ARCHIVE_HOST_PATH" in
    /*/"$FOLDER_NAME") ;;
    *) fail "archive_attachment_target_invalid" ;;
  esac
  printf '%s' "$ARCHIVE_HOST_PATH" | LC_ALL=C grep '[[:cntrl:]"\\]' >/dev/null 2>&1 && fail "archive_attachment_target_invalid"
  umask 027
  [ "$exists" = "true" ] || mkdir -- "$target" || fail "archive_attachment_folder_create_failed"
  [ -d "$target" ] && [ ! -L "$target" ] || fail "archive_attachment_target_invalid"
  for part in "$target/kmvms" "$target/kmvms/recordings" "$target/.km-vms-storage-root.json"; do
    [ ! -L "$part" ] || fail "target_is_symlink"
  done
  [ ! -e "$target/.km-vms-storage-root.json" ] || [ "$has_marker" = "true" ] || fail "archive_attachment_target_invalid"
  mkdir -p -- "$target/kmvms/recordings" || fail "archive_attachment_namespace_create_failed"
  probe=$(mktemp "$target/kmvms/recordings/.km-vms-probe.XXXXXX") || fail "target_not_writable"
  if ! printf 'km-vms\n' > "$probe"; then
    rm -f -- "$probe"
    fail "target_not_writable"
  fi
  rm -f -- "$probe" || fail "storage_candidate_probe_cleanup_failed"
  if [ "$has_marker" != "true" ]; then
    (set -C; printf '{\n  "schema_version": 1,\n  "product": "KM VMS",\n  "selected_host_path": "%s",\n  "container_archive_path": "/storage/archive"\n}\n' "$ARCHIVE_HOST_PATH" > "$target/.km-vms-storage-root.json") ||
      fail "archive_attachment_marker_write_failed"
  fi
fi

printf 'writable=true\n'
printf 'exists=%s\n' "$exists"
printf 'is_empty=%s\n' "$is_empty"
printf 'has_km_vms_marker=%s\n' "$has_marker"
