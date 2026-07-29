#!/usr/bin/env sh
set -eu

APP_DIR=$(CDPATH= cd -- "${KM_VMS_SETUP_APP_DIR:-/host-app}" && pwd -P)
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SOURCE_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
case "$SOURCE_DIR" in
  "$APP_DIR") SOURCE_CONTAINER_DIR="/host-app" ;;
  "$APP_DIR"/*)
    SOURCE_CONTAINER_DIR="/host-app/${SOURCE_DIR#"$APP_DIR"/}"
    ;;
  *)
    printf '%s\n' "setup helper source escaped stable APP_DIR" >&2
    exit 1
    ;;
esac
CONTROL_DIR="$APP_DIR/data/install-control"
REQUEST_FILE="$CONTROL_DIR/storage-activation-request.json"
REQUEST_CONTROL_FILE="$CONTROL_DIR/storage-activation-request.control"
SELECTION_CONTROL_FILE="$CONTROL_DIR/storage-selection.control"
STATUS_FILE="$CONTROL_DIR/storage-apply-status.json"
DISCOVERY_REQUEST_CONTROL_FILE="$CONTROL_DIR/storage-discovery-request.control"
DISCOVERY_RESULT_FILE="$CONTROL_DIR/storage-discovery-result.json"
DISCOVERY_CANDIDATES_CONTROL_FILE="$CONTROL_DIR/storage-discovery-candidates.control"
ROOT_CLEANUP_REQUEST_CONTROL_FILE="$CONTROL_DIR/storage-root-cleanup-request.control"
ROOT_CLEANUP_RESULT_FILE="$CONTROL_DIR/storage-root-cleanup-result.json"
SETUP_COMPLETE_FILE="$CONTROL_DIR/setup-complete.json"
SETUP_STATUS_URL="${KM_VMS_SETUP_STATUS_URL:-http://api:8000/system/status}"
APPLY_OUT="/tmp/km-vms-storage-apply.out"
APPLY_ERR="/tmp/km-vms-storage-apply.err"
RESTART_OUT="/tmp/km-vms-storage-restart.out"
RESTART_ERR="/tmp/km-vms-storage-restart.err"
DISCOVERY_OUT="/tmp/km-vms-storage-discovery.out"
DISCOVERY_ERR="/tmp/km-vms-storage-discovery.err"
DISCOVERY_VALIDATION_OUT="/tmp/km-vms-storage-discovery-validation.out"
DISCOVERY_VALIDATION_ERR="/tmp/km-vms-storage-discovery-validation.err"
ROOT_CLEANUP_OUT="/tmp/km-vms-storage-root-cleanup.out"
ROOT_CLEANUP_ERR="/tmp/km-vms-storage-root-cleanup.err"

fail_status() {
  message="$1"
  selected_path="${2:-}"
  request_id_value="${3:-}"
  operation_id_value="${4:-}"
  configuration_consistent="${5:-true}"
  case "$configuration_consistent" in
    true|false) ;;
    *) configuration_consistent=false ;;
  esac
  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  tmp="$STATUS_FILE.tmp.$$"
  {
    printf '{\n'
    printf '  "schema_version": 2,\n'
    printf '  "status": "activation_failed",\n'
    printf '  "request_id": "%s",\n' "$(json_escape "$request_id_value")"
    printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id_value")"
    printf '  "selected_host_path": "%s",\n' "$(printf '%s' "$selected_path" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    printf '  "container_archive_path": "/storage/archive",\n'
    printf '  "configuration_consistent": %s,\n' "$configuration_consistent"
    printf '  "updated_at": "%s",\n' "$created_at"
    printf '  "error": "%s"\n' "$(printf '%s' "$message" | tr '\r\n' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g')"
    printf '}\n'
  } > "$tmp"
  mv "$tmp" "$STATUS_FILE"
  chmod 600 "$STATUS_FILE" 2>/dev/null || true
}

restore_env_backup() {
  env_file="$APP_DIR/.env"
  backup_file="$APP_DIR/.env.stage2-storage.bak"
  restore_tmp="$env_file.restore.$$"
  [ -f "$env_file" ] || return 1
  [ -f "$backup_file" ] || return 1
  if ! cp "$backup_file" "$restore_tmp"; then
    rm -f "$restore_tmp"
    return 1
  fi
  chmod --reference="$env_file" "$restore_tmp" 2>/dev/null || chmod 600 "$restore_tmp" 2>/dev/null || true
  if ! mv "$restore_tmp" "$env_file"; then
    rm -f "$restore_tmp"
    return 1
  fi
  return 0
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

write_request_control() {
  request_id_value="$1"
  selected_path_value="$2"
  status_value="$3"
  operation_id_value="${4:-}"
  tmp="$REQUEST_CONTROL_FILE.tmp.$$"
  {
    printf 'schema_version=1\n'
    printf 'request_id=%s\n' "$request_id_value"
    printf 'selected_host_path=%s\n' "$selected_path_value"
    printf 'container_archive_path=/storage/archive\n'
    printf 'operation_id=%s\n' "$operation_id_value"
    printf 'status=%s\n' "$status_value"
  } > "$tmp"
  mv "$tmp" "$REQUEST_CONTROL_FILE"
  chmod 600 "$REQUEST_CONTROL_FILE" 2>/dev/null || true
}

setup_already_completed() {
  [ -f "$SETUP_COMPLETE_FILE" ] && return 0
  if command -v wget >/dev/null 2>&1; then
    payload=$(wget -qO- "$SETUP_STATUS_URL" 2>/dev/null || true)
  elif command -v curl >/dev/null 2>&1; then
    payload=$(curl -fsSL "$SETUP_STATUS_URL" 2>/dev/null || true)
  else
    payload=""
  fi
  printf '%s' "$payload" | grep -Eq '"initialized"[[:space:]]*:[[:space:]]*true'
}

write_discovery_request_status() {
  request_id_value="$1"
  mode_value="$2"
  candidate_id_value="$3"
  expected_snapshot_id_value="$4"
  expected_identity_value="$5"
  folder_name_value="$6"
  status_value="$7"
  tmp="$DISCOVERY_REQUEST_CONTROL_FILE.tmp.$$"
  {
    printf 'schema_version=1\n'
    printf 'request_id=%s\n' "$request_id_value"
    printf 'mode=%s\n' "$mode_value"
    printf 'candidate_id=%s\n' "$candidate_id_value"
    printf 'expected_snapshot_id=%s\n' "$expected_snapshot_id_value"
    printf 'expected_physical_identity=%s\n' "$expected_identity_value"
    printf 'folder_name=%s\n' "$folder_name_value"
    printf 'status=%s\n' "$status_value"
  } > "$tmp"
  mv "$tmp" "$DISCOVERY_REQUEST_CONTROL_FILE"
  chmod 600 "$DISCOVERY_REQUEST_CONTROL_FILE" 2>/dev/null || true
}

write_discovery_result() {
  request_id_value="$1"
  status_value="$2"
  error_value="$3"
  candidate_id_value="$4"
  snapshot_id_value="$5"
  identity_value="$6"
  mount_value="$7"
  folder_value="$8"
  writable_value="$9"
  shift 9
  exists_value="${1:-false}"
  is_empty_value="${2:-false}"
  marker_value="${3:-false}"
  final_value=""
  if [ -n "$mount_value" ] && [ -n "$folder_value" ]; then
    final_value="$mount_value/$folder_value"
  fi
  updated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  tmp="$DISCOVERY_RESULT_FILE.tmp.$$"
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "request_id": "%s",\n' "$(json_escape "$request_id_value")"
    printf '  "status": "%s",\n' "$(json_escape "$status_value")"
    printf '  "error": "%s",\n' "$(json_escape "$error_value")"
    printf '  "candidate_id": "%s",\n' "$(json_escape "$candidate_id_value")"
    printf '  "snapshot_id": "%s",\n' "$(json_escape "$snapshot_id_value")"
    printf '  "physical_identity": "%s",\n' "$(json_escape "$identity_value")"
    printf '  "selected_mount_path": "%s",\n' "$(json_escape "$mount_value")"
    printf '  "folder_name": "%s",\n' "$(json_escape "$folder_value")"
    printf '  "final_host_path": "%s",\n' "$(json_escape "$final_value")"
    printf '  "writable": %s,\n' "$writable_value"
    printf '  "exists": %s,\n' "$exists_value"
    printf '  "is_empty": %s,\n' "$is_empty_value"
    printf '  "has_km_vms_marker": %s,\n' "$marker_value"
    printf '  "updated_at": "%s"\n' "$(json_escape "$updated_at")"
    printf '}\n'
  } > "$tmp"
  mv "$tmp" "$DISCOVERY_RESULT_FILE"
  chmod 600 "$DISCOVERY_RESULT_FILE" 2>/dev/null || true
}

write_root_cleanup_request_status() {
  request_id_value="$1"
  operation_id_value="$2"
  archive_root_id_value="$3"
  selected_path_value="$4"
  selected_mount_value="$5"
  folder_value="$6"
  identity_value="$7"
  marker_removed_value="$8"
  status_value="$9"
  tmp="$ROOT_CLEANUP_REQUEST_CONTROL_FILE.tmp.$$"
  {
    printf 'schema_version=1\n'
    printf 'request_id=%s\n' "$request_id_value"
    printf 'operation_id=%s\n' "$operation_id_value"
    printf 'archive_root_id=%s\n' "$archive_root_id_value"
    printf 'selected_host_path=%s\n' "$selected_path_value"
    printf 'selected_mount_path=%s\n' "$selected_mount_value"
    printf 'folder_name=%s\n' "$folder_value"
    printf 'expected_physical_identity=%s\n' "$identity_value"
    printf 'marker_already_removed=%s\n' "$marker_removed_value"
    printf 'status=%s\n' "$status_value"
  } > "$tmp"
  mv "$tmp" "$ROOT_CLEANUP_REQUEST_CONTROL_FILE"
  chmod 600 "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" 2>/dev/null || true
}

set_root_cleanup_capability() {
  capability_reason="$1"
  capability_status="$2"
  if [ "$capability_status" = "completed_removed" ] || [ "$capability_status" = "completed_preserved_nonempty" ]; then
    ROOT_CLEANUP_RETRY_MODE=none
    ROOT_CLEANUP_NEXT_ACTION=close
  else
    case "$capability_reason" in
      archive_root_cleanup_helper_timeout|archive_root_cleanup_helper_failed|root_directory_remove_failed|metadata_update_failed_after_file_delete|runtime_state_finalize_failed|runtime_manifest_recovery_failed|destructive_scope_conflict|destructive_scope_lease_lost)
        ROOT_CLEANUP_RETRY_MODE=immediate
        ROOT_CLEANUP_NEXT_ACTION=retry_cleanup
        ;;
      selected_mount_missing|storage_discovery_refresh_failed|archive_root_cleanup_identity_revalidation_failed)
        ROOT_CLEANUP_RETRY_MODE=after_refresh
        ROOT_CLEANUP_NEXT_ACTION=refresh_storage_state
        ;;
      selected_mount_not_readable|selected_mount_not_searchable|selected_mount_not_writable|root_marker_remove_failed|filesystem_delete_failed)
        ROOT_CLEANUP_RETRY_MODE=after_external_fix
        ROOT_CLEANUP_NEXT_ACTION=correct_storage_access
        ;;
      *)
        ROOT_CLEANUP_RETRY_MODE=none
        ROOT_CLEANUP_NEXT_ACTION=close
        ;;
    esac
  fi
  if [ "$ROOT_CLEANUP_RETRY_MODE" = "immediate" ]; then
    ROOT_CLEANUP_RETRY_AVAILABLE=true
  else
    ROOT_CLEANUP_RETRY_AVAILABLE=false
  fi
}

write_root_cleanup_result() {
  request_id_value="$1"
  operation_id_value="$2"
  archive_root_id_value="$3"
  status_value="$4"
  cleanup_status_value="$5"
  reason_value="$6"
  marker_removed_value="$7"
  directory_removed_value="$8"
  preserved_reason_value="$9"
  supplied_retry_available_value="${10}"
  retry_mode_value="${11:-}"
  next_action_value="${12:-}"
  case "$marker_removed_value:$directory_removed_value:$supplied_retry_available_value" in
    true:true:true|true:true:false|true:false:true|true:false:false|false:true:true|false:true:false|false:false:true|false:false:false) ;;
    *) marker_removed_value=false; directory_removed_value=false ;;
  esac
  case "$retry_mode_value:$next_action_value" in
    immediate:retry_cleanup|after_refresh:refresh_storage_state|after_external_fix:correct_storage_access|none:close) ;;
    *)
      set_root_cleanup_capability "$reason_value" "$cleanup_status_value"
      retry_mode_value="$ROOT_CLEANUP_RETRY_MODE"
      next_action_value="$ROOT_CLEANUP_NEXT_ACTION"
      ;;
  esac
  if [ "$retry_mode_value" = "immediate" ]; then
    retry_available_value=true
  else
    retry_available_value=false
  fi
  updated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  tmp="$ROOT_CLEANUP_RESULT_FILE.tmp.$$"
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "request_id": "%s",\n' "$(json_escape "$request_id_value")"
    printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id_value")"
    printf '  "archive_root_id": "%s",\n' "$(json_escape "$archive_root_id_value")"
    printf '  "status": "%s",\n' "$(json_escape "$status_value")"
    printf '  "cleanup_status": "%s",\n' "$(json_escape "$cleanup_status_value")"
    printf '  "reason": "%s",\n' "$(json_escape "$reason_value")"
    printf '  "marker_removed": %s,\n' "$marker_removed_value"
    printf '  "root_directory_removed": %s,\n' "$directory_removed_value"
    printf '  "root_directory_preserved_reason": "%s",\n' "$(json_escape "$preserved_reason_value")"
    printf '  "retry_mode": "%s",\n' "$(json_escape "$retry_mode_value")"
    printf '  "next_action": "%s",\n' "$(json_escape "$next_action_value")"
    printf '  "retry_available": %s,\n' "$retry_available_value"
    printf '  "updated_at": "%s"\n' "$updated_at"
    printf '}\n'
  } > "$tmp"
  mv "$tmp" "$ROOT_CLEANUP_RESULT_FILE"
  chmod 600 "$ROOT_CLEANUP_RESULT_FILE" 2>/dev/null || true
}

discovery_json_value() {
  key="$1"
  file="$2"
  sed -n 's/^[[:space:]]*"'"$key"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$file" | head -n 1
}

process_root_cleanup_request() {
  [ -f "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" ] || return 0
  cleanup_request_status=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" status || true)
  [ "$cleanup_request_status" = "requested" ] || return 0
  cleanup_request_id=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" request_id || true)
  cleanup_operation_id=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" operation_id || true)
  cleanup_root_id=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" archive_root_id || true)
  cleanup_host_path=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" selected_host_path || true)
  cleanup_mount=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" selected_mount_path || true)
  cleanup_folder=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" folder_name || true)
  cleanup_identity=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" expected_physical_identity || true)
  cleanup_marker_removed=$(read_control_value "$ROOT_CLEANUP_REQUEST_CONTROL_FILE" marker_already_removed || printf false)

  case "$cleanup_request_id" in
    ''|*[!A-Za-z0-9_.-]*)
      write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" failed failed_preflight archive_root_cleanup_request_invalid false false "" false
      write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" failed
      return 0
      ;;
  esac
  case "$cleanup_operation_id" in
    ''|*[!A-Za-z0-9_.-]*)
      write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" failed failed_preflight archive_root_cleanup_identity_invalid false false "" false
      write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" failed
      return 0
      ;;
  esac
  case "$cleanup_root_id" in
    ''|*[!A-Za-z0-9_.-]*)
      write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" failed failed_preflight archive_root_cleanup_identity_invalid false false "" false
      write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" failed
      return 0
      ;;
  esac
  case "$cleanup_mount" in
    /*) ;;
    *)
      write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" failed failed_preflight archive_root_cleanup_mount_invalid false false "" false
      write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" failed
      return 0
      ;;
  esac
  case "$cleanup_folder" in
    ''|.|..|.*|*/*|*\\*|*'"'*)
      write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" failed failed_preflight archive_root_cleanup_folder_invalid false false "" false
      write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" failed
      return 0
      ;;
  esac
  [ "$cleanup_host_path" = "$cleanup_mount/$cleanup_folder" ] || {
    write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" failed failed_preflight archive_root_cleanup_path_mismatch false false "" false
    write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" failed
    return 0
  }
  if [ -z "$cleanup_identity" ]; then
    write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" failed failed_preflight archive_root_cleanup_identity_invalid false false "" false
    write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" failed
    return 0
  fi

  write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" processing
  rm -f "$DISCOVERY_OUT" "$DISCOVERY_ERR" "$ROOT_CLEANUP_OUT" "$ROOT_CLEANUP_ERR"
  if ! docker run --rm \
    -v "$APP_DIR:/host-app" \
    -v "/:/host:ro,rslave" \
    docker:27-cli \
    sh "$SOURCE_CONTAINER_DIR/scripts/km-vms-storage-discovery.sh" --app-dir /host-app --host-root /host >"$DISCOVERY_OUT" 2>"$DISCOVERY_ERR"; then
    write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" partial partial_cleanup storage_discovery_refresh_failed "$cleanup_marker_removed" false "" true
    write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" partial
    return 0
  fi

  cleanup_candidate=$(awk -F '\t' -v mount="$cleanup_mount" '$2 == mount {print; exit}' "$DISCOVERY_CANDIDATES_CONTROL_FILE" 2>/dev/null || true)
  cleanup_current_identity=$(printf '%s\n' "$cleanup_candidate" | awk -F '\t' '{print $3}')
  cleanup_writable=$(printf '%s\n' "$cleanup_candidate" | awk -F '\t' '{print $4}')
  cleanup_safety=$(printf '%s\n' "$cleanup_candidate" | awk -F '\t' '{print $5}')
  if [ -z "$cleanup_candidate" ] || [ "$cleanup_current_identity" != "$cleanup_identity" ] || [ "$cleanup_writable" != "true" ] || [ "$cleanup_safety" != "allowed" ]; then
    write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" failed failed_preflight archive_root_cleanup_identity_revalidation_failed false false "" false
    write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$cleanup_marker_removed" failed
    return 0
  fi

  helper_exit=0
  docker run --rm \
    -v "$APP_DIR:/host-app:ro" \
    -v "$cleanup_mount:/selected-root" \
    docker:27-cli \
    sh "$SOURCE_CONTAINER_DIR/scripts/km-vms-storage-root-cleanup.sh" \
      --folder-name "$cleanup_folder" \
      --expected-host-path "$cleanup_host_path" \
      --operation-id "$cleanup_operation_id" \
      --archive-root-id "$cleanup_root_id" \
      --allow-missing-marker "$cleanup_marker_removed" >"$ROOT_CLEANUP_OUT" 2>"$ROOT_CLEANUP_ERR" || helper_exit=$?

  result_status=$(read_control_value "$ROOT_CLEANUP_OUT" status || printf partial)
  cleanup_status=$(read_control_value "$ROOT_CLEANUP_OUT" cleanup_status || printf partial_cleanup)
  cleanup_reason=$(read_control_value "$ROOT_CLEANUP_OUT" reason || printf archive_root_cleanup_helper_failed)
  result_marker_removed=$(read_control_value "$ROOT_CLEANUP_OUT" marker_removed || printf false)
  result_directory_removed=$(read_control_value "$ROOT_CLEANUP_OUT" root_directory_removed || printf false)
  result_preserved_reason=$(read_control_value "$ROOT_CLEANUP_OUT" root_directory_preserved_reason || true)
  result_retry_mode=$(read_control_value "$ROOT_CLEANUP_OUT" retry_mode || true)
  result_next_action=$(read_control_value "$ROOT_CLEANUP_OUT" next_action || true)
  result_retry=$(read_control_value "$ROOT_CLEANUP_OUT" retry_available || printf true)
  if [ "$helper_exit" -ne 0 ] && [ "$result_status" = "completed" ]; then
    result_status=partial
    cleanup_status=partial_cleanup
    cleanup_reason=archive_root_cleanup_helper_failed
    result_retry_mode=immediate
    result_next_action=retry_cleanup
    result_retry=true
  fi
  write_root_cleanup_result "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$result_status" "$cleanup_status" "$cleanup_reason" "$result_marker_removed" "$result_directory_removed" "$result_preserved_reason" "$result_retry" "$result_retry_mode" "$result_next_action"
  write_root_cleanup_request_status "$cleanup_request_id" "$cleanup_operation_id" "$cleanup_root_id" "$cleanup_host_path" "$cleanup_mount" "$cleanup_folder" "$cleanup_identity" "$result_marker_removed" "$result_status"
}

process_discovery_request() {
  [ -f "$DISCOVERY_REQUEST_CONTROL_FILE" ] || return 0
  discovery_status=$(read_control_value "$DISCOVERY_REQUEST_CONTROL_FILE" status || true)
  [ "$discovery_status" = "requested" ] || return 0
  discovery_request_id=$(read_control_value "$DISCOVERY_REQUEST_CONTROL_FILE" request_id || true)
  discovery_mode=$(read_control_value "$DISCOVERY_REQUEST_CONTROL_FILE" mode || true)
  discovery_candidate_id=$(read_control_value "$DISCOVERY_REQUEST_CONTROL_FILE" candidate_id || true)
  discovery_expected_snapshot=$(read_control_value "$DISCOVERY_REQUEST_CONTROL_FILE" expected_snapshot_id || true)
  discovery_expected_identity=$(read_control_value "$DISCOVERY_REQUEST_CONTROL_FILE" expected_physical_identity || true)
  discovery_folder=$(read_control_value "$DISCOVERY_REQUEST_CONTROL_FILE" folder_name || true)
  [ -n "$discovery_request_id" ] || return 0
  write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "processing"

  rm -f "$DISCOVERY_OUT" "$DISCOVERY_ERR" "$DISCOVERY_VALIDATION_OUT" "$DISCOVERY_VALIDATION_ERR"
  if ! docker run --rm \
    -v "$APP_DIR:/host-app" \
    -v "/:/host:ro,rslave" \
    docker:27-cli \
    sh "$SOURCE_CONTAINER_DIR/scripts/km-vms-storage-discovery.sh" --app-dir /host-app --host-root /host >"$DISCOVERY_OUT" 2>"$DISCOVERY_ERR"; then
    write_discovery_result "$discovery_request_id" "failed" "storage_discovery_refresh_failed" "$discovery_candidate_id" "" "" "" "$discovery_folder" false
    write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "failed"
    return 0
  fi

  discovery_snapshot_id=$(discovery_json_value snapshot_id "$CONTROL_DIR/storage-discovery.json")
  if [ "$discovery_mode" = "refresh" ]; then
    write_discovery_result "$discovery_request_id" "completed" "" "" "$discovery_snapshot_id" "" "" "" false
    write_discovery_request_status "$discovery_request_id" "$discovery_mode" "" "$discovery_expected_snapshot" "" "" "completed"
    return 0
  fi

  if [ "$discovery_mode" != "candidate_revalidate" ] || [ -z "$discovery_candidate_id" ] || [ -z "$discovery_expected_identity" ] || [ -z "$discovery_folder" ]; then
    write_discovery_result "$discovery_request_id" "failed" "storage_discovery_request_invalid" "$discovery_candidate_id" "$discovery_snapshot_id" "" "" "$discovery_folder" false
    write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "failed"
    return 0
  fi

  candidate_line=$(awk -F '\t' -v id="$discovery_candidate_id" '$1 == id {print; exit}' "$DISCOVERY_CANDIDATES_CONTROL_FILE" 2>/dev/null || true)
  if [ -z "$candidate_line" ]; then
    write_discovery_result "$discovery_request_id" "failed" "storage_candidate_disappeared" "$discovery_candidate_id" "$discovery_snapshot_id" "" "" "$discovery_folder" false
    write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "failed"
    return 0
  fi
  discovery_mount=$(printf '%s\n' "$candidate_line" | awk -F '\t' '{print $2}')
  discovery_identity=$(printf '%s\n' "$candidate_line" | awk -F '\t' '{print $3}')
  discovery_writable=$(printf '%s\n' "$candidate_line" | awk -F '\t' '{print $4}')
  discovery_safety=$(printf '%s\n' "$candidate_line" | awk -F '\t' '{print $5}')
  if [ "$discovery_identity" != "$discovery_expected_identity" ]; then
    write_discovery_result "$discovery_request_id" "failed" "storage_candidate_physical_identity_changed" "$discovery_candidate_id" "$discovery_snapshot_id" "$discovery_identity" "$discovery_mount" "$discovery_folder" false
    write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "failed"
    return 0
  fi
  if [ "$discovery_writable" != "true" ] || [ "$discovery_safety" != "allowed" ]; then
    write_discovery_result "$discovery_request_id" "failed" "storage_candidate_not_writable" "$discovery_candidate_id" "$discovery_snapshot_id" "$discovery_identity" "$discovery_mount" "$discovery_folder" false
    write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "failed"
    return 0
  fi
  case "$discovery_mount" in
    /Volume[0-9]*|/volume[0-9]*) ;;
    *)
      write_discovery_result "$discovery_request_id" "failed" "storage_candidate_not_allowed" "$discovery_candidate_id" "$discovery_snapshot_id" "$discovery_identity" "$discovery_mount" "$discovery_folder" false
      write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "failed"
      return 0
      ;;
  esac

  if ! docker run --rm \
    -v "$APP_DIR:/host-app:ro" \
    -v "$discovery_mount:/selected-root" \
    docker:27-cli \
    sh "$SOURCE_CONTAINER_DIR/scripts/km-vms-storage-candidate-validate.sh" --folder-name "$discovery_folder" >"$DISCOVERY_VALIDATION_OUT" 2>"$DISCOVERY_VALIDATION_ERR"; then
    discovery_error=$(read_control_value "$DISCOVERY_VALIDATION_OUT" error || true)
    write_discovery_result "$discovery_request_id" "failed" "${discovery_error:-storage_candidate_revalidation_failed}" "$discovery_candidate_id" "$discovery_snapshot_id" "$discovery_identity" "$discovery_mount" "$discovery_folder" false
    write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "failed"
    return 0
  fi
  validation_writable=$(read_control_value "$DISCOVERY_VALIDATION_OUT" writable || printf false)
  validation_exists=$(read_control_value "$DISCOVERY_VALIDATION_OUT" exists || printf false)
  validation_empty=$(read_control_value "$DISCOVERY_VALIDATION_OUT" is_empty || printf false)
  validation_marker=$(read_control_value "$DISCOVERY_VALIDATION_OUT" has_km_vms_marker || printf false)
  write_discovery_result "$discovery_request_id" "completed" "" "$discovery_candidate_id" "$discovery_snapshot_id" "$discovery_identity" "$discovery_mount" "$discovery_folder" "$validation_writable" "$validation_exists" "$validation_empty" "$validation_marker"
  write_discovery_request_status "$discovery_request_id" "$discovery_mode" "$discovery_candidate_id" "$discovery_expected_snapshot" "$discovery_expected_identity" "$discovery_folder" "completed"
}

while :; do
  process_root_cleanup_request
  process_discovery_request
  if [ ! -f "$REQUEST_CONTROL_FILE" ]; then
    sleep 2
    continue
  fi

  status=$(read_control_value "$REQUEST_CONTROL_FILE" status || true)
  request_id=$(read_control_value "$REQUEST_CONTROL_FILE" request_id || true)
  operation_id=$(read_control_value "$REQUEST_CONTROL_FILE" operation_id || true)
  selected_path=$(read_control_value "$REQUEST_CONTROL_FILE" selected_host_path || true)
  selected_mount=$(read_control_value "$SELECTION_CONTROL_FILE" selected_mount_path || true)
  folder_name=$(read_control_value "$SELECTION_CONTROL_FILE" folder_name || true)
  expected_physical_identity=$(read_control_value "$SELECTION_CONTROL_FILE" physical_identity || true)

  if setup_already_completed && [ "$status" != "requested" ]; then
    sleep 5
    continue
  fi

  if [ "$status" != "requested" ] || [ -z "$request_id" ] || [ -z "$selected_mount" ] || [ -z "$folder_name" ]; then
    sleep 2
    continue
  fi

  if setup_already_completed && [ -z "$operation_id" ]; then
    fail_status "archive root activation operation id is required" "$selected_path" "$request_id" "$operation_id"
    write_request_control "$request_id" "$selected_path" "failed" "$operation_id"
    sleep 2
    continue
  fi

  if setup_already_completed && [ -z "$expected_physical_identity" ]; then
    fail_status "storage candidate identity is required" "$selected_path" "$request_id" "$operation_id"
    write_request_control "$request_id" "$selected_path" "failed" "$operation_id"
    sleep 2
    continue
  fi

  if [ -n "$expected_physical_identity" ]; then
    rm -f "$DISCOVERY_OUT" "$DISCOVERY_ERR"
    if ! docker run --rm \
      -v "$APP_DIR:/host-app" \
      -v "/:/host:ro,rslave" \
      docker:27-cli \
      sh "$SOURCE_CONTAINER_DIR/scripts/km-vms-storage-discovery.sh" --app-dir /host-app --host-root /host >"$DISCOVERY_OUT" 2>"$DISCOVERY_ERR"; then
      fail_status "storage candidate refresh failed before activation" "$selected_path" "$request_id" "$operation_id"
      write_request_control "$request_id" "$selected_path" "failed" "$operation_id"
      sleep 2
      continue
    fi
    activation_candidate=$(awk -F '\t' -v mount="$selected_mount" '$2 == mount {print; exit}' "$DISCOVERY_CANDIDATES_CONTROL_FILE" 2>/dev/null || true)
    activation_identity=$(printf '%s\n' "$activation_candidate" | awk -F '\t' '{print $3}')
    activation_writable=$(printf '%s\n' "$activation_candidate" | awk -F '\t' '{print $4}')
    activation_safety=$(printf '%s\n' "$activation_candidate" | awk -F '\t' '{print $5}')
    if [ -z "$activation_candidate" ] || [ "$activation_identity" != "$expected_physical_identity" ] || [ "$activation_writable" != "true" ] || [ "$activation_safety" != "allowed" ]; then
      fail_status "storage candidate changed before activation" "$selected_path" "$request_id" "$operation_id"
      write_request_control "$request_id" "$selected_path" "failed" "$operation_id"
      sleep 2
      continue
    fi
  fi

  rm -f "$DISCOVERY_VALIDATION_OUT" "$DISCOVERY_VALIDATION_ERR"
  if ! docker run --rm \
    -v "$APP_DIR:/host-app:ro" \
    -v "$selected_mount:/selected-root" \
    docker:27-cli \
    sh "$SOURCE_CONTAINER_DIR/scripts/km-vms-storage-candidate-validate.sh" --folder-name "$folder_name" >"$DISCOVERY_VALIDATION_OUT" 2>"$DISCOVERY_VALIDATION_ERR"; then
    validation_error=$(read_control_value "$DISCOVERY_VALIDATION_OUT" error || true)
    fail_status "${validation_error:-storage candidate revalidation failed before activation}" "$selected_path" "$request_id" "$operation_id"
    write_request_control "$request_id" "$selected_path" "failed" "$operation_id"
    sleep 2
    continue
  fi

  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  request_tmp="$REQUEST_FILE.tmp.$$"
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "request_id": "%s",\n' "$request_id"
    printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id")"
    printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
    printf '  "requested_at": "%s",\n' "$created_at"
    printf '  "status": "processing"\n'
    printf '}\n'
  } > "$request_tmp"
  mv "$request_tmp" "$REQUEST_FILE"
  write_request_control "$request_id" "$selected_path" "processing" "$operation_id"

  rm -f "$APPLY_OUT" "$APPLY_ERR" "$RESTART_OUT" "$RESTART_ERR"

  if ! docker run --rm \
    -v "$APP_DIR:/host-app" \
    -v "$selected_mount:/selected-root" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e KM_VMS_SELECTED_MOUNT_CONTAINER=/selected-root \
    -e "KM_VMS_SELECTED_PATH_CONTAINER=/selected-root/$folder_name" \
    docker:27-cli \
    sh "$SOURCE_CONTAINER_DIR/scripts/km-vms-storage-apply.sh" --app-dir /host-app >"$APPLY_OUT" 2>"$APPLY_ERR"; then
    configuration_consistent=false
    restore_env_backup && configuration_consistent=true
    fail_status "$(cat "$APPLY_ERR" 2>/dev/null || printf 'storage apply failed')" "$selected_path" "$request_id" "$operation_id" "$configuration_consistent"
    request_tmp="$REQUEST_FILE.tmp.$$"
    {
      printf '{\n'
      printf '  "schema_version": 1,\n'
      printf '  "request_id": "%s",\n' "$request_id"
      printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id")"
      printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
      printf '  "requested_at": "%s",\n' "$created_at"
      printf '  "status": "failed"\n'
      printf '}\n'
    } > "$request_tmp"
    mv "$request_tmp" "$REQUEST_FILE"
    write_request_control "$request_id" "$selected_path" "failed" "$operation_id"
    sleep 2
    continue
  fi

  if ! sh "$SOURCE_DIR/scripts/km-vms-restart.sh" --app-dir "$APP_DIR" --verify-storage-selection >"$RESTART_OUT" 2>"$RESTART_ERR"; then
    configuration_consistent=false
    restore_env_backup && configuration_consistent=true
    fail_status "$(cat "$RESTART_ERR" 2>/dev/null || printf 'storage restart failed')" "$selected_path" "$request_id" "$operation_id" "$configuration_consistent"
    request_tmp="$REQUEST_FILE.tmp.$$"
    {
      printf '{\n'
      printf '  "schema_version": 1,\n'
      printf '  "request_id": "%s",\n' "$request_id"
      printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id")"
      printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
      printf '  "requested_at": "%s",\n' "$created_at"
      printf '  "status": "failed"\n'
      printf '}\n'
    } > "$request_tmp"
    mv "$request_tmp" "$REQUEST_FILE"
    write_request_control "$request_id" "$selected_path" "failed" "$operation_id"
    sleep 2
    continue
  fi

  request_tmp="$REQUEST_FILE.tmp.$$"
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "request_id": "%s",\n' "$request_id"
    printf '  "operation_id": "%s",\n' "$(json_escape "$operation_id")"
    printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
    printf '  "requested_at": "%s",\n' "$created_at"
    printf '  "status": "completed"\n'
    printf '}\n'
  } > "$request_tmp"
  mv "$request_tmp" "$REQUEST_FILE"
  write_request_control "$request_id" "$selected_path" "completed" "$operation_id"
  rm -f "$APPLY_OUT" "$APPLY_ERR" "$RESTART_OUT" "$RESTART_ERR"
  sleep 2
done
