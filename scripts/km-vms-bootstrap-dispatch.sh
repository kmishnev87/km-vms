#!/usr/bin/env sh
set -eu

ROLE="${1:-}"
APP_DIR="${2:-}"

case "$ROLE" in
  setup-helper) ;;
  *) printf 'ERROR: unsupported bootstrap role\n' >&2; exit 1 ;;
esac
case "$APP_DIR" in
  /*) ;;
  *) printf 'ERROR: stable APP_DIR must be absolute\n' >&2; exit 1 ;;
esac

BUNDLE="$APP_DIR/data/update-runtime/bootstrap/current"
last_reason=""
delay=5
while :; do
  reason=""
  if [ ! -d "$BUNDLE" ] || [ ! -f "$BUNDLE/bootstrap-files.sha256" ]; then
    reason="bootstrap_bundle_missing"
  elif ! command -v sha256sum >/dev/null 2>&1; then
    reason="bootstrap_digest_tool_missing"
  elif ! (cd "$BUNDLE" && sha256sum -c bootstrap-files.sha256 >/dev/null 2>&1); then
    reason="bootstrap_digest_mismatch"
  else
    active="$APP_DIR/data/update-runtime/active"
    if [ ! -L "$active" ] || ! command -v readlink >/dev/null 2>&1; then
      reason="active_pointer_missing"
    else
      target=$(readlink "$active" 2>/dev/null || true)
      case "$target" in
        slots/release-????????????????????????????????????????/source|slots/adopted-????????????????????????????????????????????????????????????????/source|slots/initial-????????????????????????????????????????????????????????????????/source)
          product_source="$active"
          helper="$BUNDLE/km-vms-setup-activation-helper.sh"
          if [ -f "$helper" ] && [ ! -L "$helper" ]; then
            KM_VMS_SETUP_PRODUCT_SOURCE_DIR="$product_source" \
            KM_VMS_OPERATOR_SCRIPTS_DIR="$BUNDLE" \
              exec sh "$helper"
          fi
          reason="setup_helper_missing"
          ;;
        *) reason="active_pointer_invalid" ;;
      esac
    fi
  fi
  if [ "$reason" != "$last_reason" ]; then
    printf 'bootstrap_degraded=%s\n' "$reason" >&2
    last_reason="$reason"
  fi
  sleep "$delay"
  if [ "$delay" -lt 60 ]; then
    delay=$((delay * 2))
    [ "$delay" -le 60 ] || delay=60
  fi
done
