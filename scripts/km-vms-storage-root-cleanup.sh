#!/bin/sh
set -eu

SELECTED_ROOT="${KM_VMS_SELECTED_MOUNT_CONTAINER:-/selected-root}"
FOLDER_NAME=""
EXPECTED_HOST_PATH=""
OPERATION_ID=""
ARCHIVE_ROOT_ID=""
ALLOW_MISSING_MARKER="false"

set_retry_contract() {
  retry_reason="$1"
  retry_status="${2:-partial_cleanup}"
  if [ "$retry_status" = "completed_removed" ] || [ "$retry_status" = "completed_preserved_nonempty" ]; then
    RETRY_MODE=none
    NEXT_ACTION=close
  else
    case "$retry_reason" in
      archive_root_cleanup_helper_timeout|archive_root_cleanup_helper_failed|root_directory_remove_failed|metadata_update_failed_after_file_delete|runtime_state_finalize_failed|runtime_manifest_recovery_failed|destructive_scope_conflict|destructive_scope_lease_lost)
        RETRY_MODE=immediate
        NEXT_ACTION=retry_cleanup
        ;;
      selected_mount_missing|storage_discovery_refresh_failed|archive_root_cleanup_identity_revalidation_failed)
        RETRY_MODE=after_refresh
        NEXT_ACTION=refresh_storage_state
        ;;
      selected_mount_not_readable|selected_mount_not_searchable|selected_mount_not_writable|root_marker_remove_failed|filesystem_delete_failed)
        RETRY_MODE=after_external_fix
        NEXT_ACTION=correct_storage_access
        ;;
      *)
        RETRY_MODE=none
        NEXT_ACTION=close
        ;;
    esac
  fi
  if [ "$RETRY_MODE" = "immediate" ]; then
    RETRY_AVAILABLE=true
  else
    RETRY_AVAILABLE=false
  fi
}

print_retry_contract() {
  set_retry_contract "$1" "${2:-partial_cleanup}"
  printf 'retry_mode=%s\n' "$RETRY_MODE"
  printf 'next_action=%s\n' "$NEXT_ACTION"
  printf 'retry_available=%s\n' "$RETRY_AVAILABLE"
}

fail_result() {
  reason="$1"
  marker_removed="${2:-false}"
  printf 'status=partial\n'
  printf 'cleanup_status=partial_cleanup\n'
  printf 'reason=%s\n' "$reason"
  printf 'marker_removed=%s\n' "$marker_removed"
  printf 'root_directory_removed=false\n'
  printf 'root_directory_preserved_reason=\n'
  print_retry_contract "$reason" partial_cleanup
  exit 1
}

json_string_field() {
  key="$1"
  file="$2"
  sed -n 's/^[[:space:]]*"'"$key"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$file" | head -n 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --folder-name)
      [ "$#" -ge 2 ] || fail_result "folder_name_required"
      FOLDER_NAME="$2"
      shift 2
      ;;
    --expected-host-path)
      [ "$#" -ge 2 ] || fail_result "expected_host_path_required"
      EXPECTED_HOST_PATH="$2"
      shift 2
      ;;
    --operation-id)
      [ "$#" -ge 2 ] || fail_result "operation_id_required"
      OPERATION_ID="$2"
      shift 2
      ;;
    --archive-root-id)
      [ "$#" -ge 2 ] || fail_result "archive_root_id_required"
      ARCHIVE_ROOT_ID="$2"
      shift 2
      ;;
    --allow-missing-marker)
      [ "$#" -ge 2 ] || fail_result "allow_missing_marker_value_required"
      ALLOW_MISSING_MARKER="$2"
      shift 2
      ;;
    --help|-h)
      printf 'Usage: sh scripts/km-vms-storage-root-cleanup.sh --folder-name <name> --expected-host-path <path> --operation-id <id> --archive-root-id <id> [--allow-missing-marker true|false]\n'
      exit 0
      ;;
    *)
      fail_result "unknown_option"
      ;;
  esac
done

case "$FOLDER_NAME" in
  ''|.|..|.*|*/*|*\\*|*'"'*) fail_result "folder_name_invalid" ;;
esac
case "$OPERATION_ID" in
  ''|*[!A-Za-z0-9_.-]*) fail_result "operation_id_invalid" ;;
esac
case "$ARCHIVE_ROOT_ID" in
  ''|*[!A-Za-z0-9_.-]*) fail_result "archive_root_id_invalid" ;;
esac
case "$ALLOW_MISSING_MARKER" in
  true|false) ;;
  *) fail_result "allow_missing_marker_invalid" ;;
esac

[ -d "$SELECTED_ROOT" ] || fail_result "selected_mount_missing"
[ ! -L "$SELECTED_ROOT" ] || fail_result "selected_mount_symlink_rejected"
[ -r "$SELECTED_ROOT" ] || fail_result "selected_mount_not_readable"
[ -x "$SELECTED_ROOT" ] || fail_result "selected_mount_not_searchable"
[ -w "$SELECTED_ROOT" ] || fail_result "selected_mount_not_writable"

target="$SELECTED_ROOT/$FOLDER_NAME"
marker="$target/.km-vms-storage-root.json"

if [ ! -e "$target" ]; then
  printf 'status=completed\n'
  printf 'cleanup_status=completed_removed\n'
  printf 'reason=already_absent\n'
  printf 'marker_removed=true\n'
  printf 'root_directory_removed=true\n'
  printf 'root_directory_preserved_reason=\n'
  print_retry_contract already_absent completed_removed
  exit 0
fi

[ -d "$target" ] || fail_result "root_path_not_directory"
[ ! -L "$target" ] || fail_result "root_path_symlink_rejected"

marker_removed=false
if [ -e "$marker" ]; then
  [ -f "$marker" ] || fail_result "root_marker_not_regular_file"
  [ ! -L "$marker" ] || fail_result "root_marker_symlink_rejected"
  product=$(json_string_field product "$marker")
  selected_host_path=$(json_string_field selected_host_path "$marker")
  container_archive_path=$(json_string_field container_archive_path "$marker")
  [ "$product" = "KM VMS" ] || fail_result "root_marker_product_mismatch"
  [ "$selected_host_path" = "$EXPECTED_HOST_PATH" ] || fail_result "root_marker_path_mismatch"
  [ "$container_archive_path" = "/storage/archive" ] || fail_result "root_marker_container_path_mismatch"
elif [ "$ALLOW_MISSING_MARKER" != "true" ]; then
  fail_result "root_marker_missing"
else
  marker_removed=true
fi

if [ -d "$target/kmvms/recordings" ] && [ ! -L "$target/kmvms/recordings" ]; then
  rmdir "$target/kmvms/recordings" 2>/dev/null || true
fi
if [ -d "$target/kmvms" ] && [ ! -L "$target/kmvms" ]; then
  rmdir "$target/kmvms" 2>/dev/null || true
fi

remaining=$(find "$target" -mindepth 1 -maxdepth 1 ! -name '.km-vms-storage-root.json' -print 2>/dev/null | wc -l | tr -d ' ')
if [ "$marker_removed" != "true" ]; then
  rm -f -- "$marker" || fail_result "root_marker_remove_failed" false
  [ ! -e "$marker" ] || fail_result "root_marker_remove_failed" false
  marker_removed=true
fi

if [ "$remaining" -gt 0 ]; then
  printf 'status=completed\n'
  printf 'cleanup_status=completed_preserved_nonempty\n'
  printf 'reason=foreign_or_user_content_preserved\n'
  printf 'marker_removed=true\n'
  printf 'root_directory_removed=false\n'
  printf 'root_directory_preserved_reason=foreign_or_user_content\n'
  print_retry_contract foreign_or_user_content_preserved completed_preserved_nonempty
  exit 0
fi

if rmdir "$target" 2>/dev/null; then
  printf 'status=completed\n'
  printf 'cleanup_status=completed_removed\n'
  printf 'reason=empty_root_removed\n'
  printf 'marker_removed=true\n'
  printf 'root_directory_removed=true\n'
  printf 'root_directory_preserved_reason=\n'
  print_retry_contract empty_root_removed completed_removed
  exit 0
fi

fail_result "root_directory_remove_failed" true
