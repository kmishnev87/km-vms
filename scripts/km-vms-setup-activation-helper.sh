#!/usr/bin/env sh
set -eu

APP_DIR="${KM_VMS_SETUP_APP_DIR:-/host-app}"
CONTROL_DIR="$APP_DIR/data/install-control"
REQUEST_FILE="$CONTROL_DIR/storage-activation-request.json"
REQUEST_CONTROL_FILE="$CONTROL_DIR/storage-activation-request.control"
SELECTION_CONTROL_FILE="$CONTROL_DIR/storage-selection.control"
STATUS_FILE="$CONTROL_DIR/storage-apply-status.json"
SETUP_COMPLETE_FILE="$CONTROL_DIR/setup-complete.json"
SETUP_STATUS_URL="${KM_VMS_SETUP_STATUS_URL:-http://api:8000/system/status}"
APPLY_OUT="/tmp/km-vms-storage-apply.out"
APPLY_ERR="/tmp/km-vms-storage-apply.err"
RESTART_OUT="/tmp/km-vms-storage-restart.out"
RESTART_ERR="/tmp/km-vms-storage-restart.err"

fail_status() {
  message="$1"
  selected_path="${2:-}"
  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  {
    printf '{\n'
    printf '  "schema_version": 2,\n'
    printf '  "status": "activation_failed",\n'
    printf '  "selected_host_path": "%s",\n' "$(printf '%s' "$selected_path" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    printf '  "container_archive_path": "/storage/archive",\n'
    printf '  "updated_at": "%s",\n' "$created_at"
    printf '  "error": "%s"\n' "$(printf '%s' "$message" | tr '\r\n' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g')"
    printf '}\n'
  } > "$STATUS_FILE"
  chmod 600 "$STATUS_FILE" 2>/dev/null || true
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
  tmp="$REQUEST_CONTROL_FILE.tmp.$$"
  {
    printf 'schema_version=1\n'
    printf 'request_id=%s\n' "$request_id_value"
    printf 'selected_host_path=%s\n' "$selected_path_value"
    printf 'container_archive_path=/storage/archive\n'
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

while :; do
  if setup_already_completed; then
    exit 0
  fi

  if [ ! -f "$REQUEST_CONTROL_FILE" ]; then
    sleep 2
    continue
  fi

  status=$(read_control_value "$REQUEST_CONTROL_FILE" status || true)
  request_id=$(read_control_value "$REQUEST_CONTROL_FILE" request_id || true)
  selected_path=$(read_control_value "$REQUEST_CONTROL_FILE" selected_host_path || true)
  selected_mount=$(read_control_value "$SELECTION_CONTROL_FILE" selected_mount_path || true)
  folder_name=$(read_control_value "$SELECTION_CONTROL_FILE" folder_name || true)

  if [ "$status" != "requested" ] || [ -z "$request_id" ] || [ -z "$selected_mount" ] || [ -z "$folder_name" ]; then
    sleep 2
    continue
  fi

  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "request_id": "%s",\n' "$request_id"
    printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
    printf '  "requested_at": "%s",\n' "$created_at"
    printf '  "status": "processing"\n'
    printf '}\n'
  } > "$REQUEST_FILE"
  write_request_control "$request_id" "$selected_path" "processing"

  rm -f "$APPLY_OUT" "$APPLY_ERR" "$RESTART_OUT" "$RESTART_ERR"

  if ! docker run --rm \
    -v "$APP_DIR:/host-app" \
    -v "$selected_mount:/selected-root" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -e KM_VMS_SELECTED_MOUNT_CONTAINER=/selected-root \
    -e "KM_VMS_SELECTED_PATH_CONTAINER=/selected-root/$folder_name" \
    docker:27-cli \
    sh /host-app/scripts/km-vms-storage-apply.sh --app-dir /host-app >"$APPLY_OUT" 2>"$APPLY_ERR"; then
    fail_status "$(cat "$APPLY_ERR" 2>/dev/null || printf 'storage apply failed')" "$selected_path"
    {
      printf '{\n'
      printf '  "schema_version": 1,\n'
      printf '  "request_id": "%s",\n' "$request_id"
      printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
      printf '  "requested_at": "%s",\n' "$created_at"
      printf '  "status": "failed"\n'
      printf '}\n'
    } > "$REQUEST_FILE"
    write_request_control "$request_id" "$selected_path" "failed"
    sleep 2
    continue
  fi

  if ! sh "$APP_DIR/scripts/km-vms-restart.sh" --app-dir "$APP_DIR" --verify-storage-selection >"$RESTART_OUT" 2>"$RESTART_ERR"; then
    fail_status "$(cat "$RESTART_ERR" 2>/dev/null || printf 'storage restart failed')" "$selected_path"
    {
      printf '{\n'
      printf '  "schema_version": 1,\n'
      printf '  "request_id": "%s",\n' "$request_id"
      printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
      printf '  "requested_at": "%s",\n' "$created_at"
      printf '  "status": "failed"\n'
      printf '}\n'
    } > "$REQUEST_FILE"
    write_request_control "$request_id" "$selected_path" "failed"
    sleep 2
    continue
  fi

  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "request_id": "%s",\n' "$request_id"
    printf '  "selected_host_path": "%s",\n' "$(json_escape "$selected_path")"
    printf '  "requested_at": "%s",\n' "$created_at"
    printf '  "status": "completed"\n'
    printf '}\n'
  } > "$REQUEST_FILE"
  write_request_control "$request_id" "$selected_path" "completed"
  rm -f "$APPLY_OUT" "$APPLY_ERR" "$RESTART_OUT" "$RESTART_ERR"
  sleep 2
done
