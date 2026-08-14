#!/usr/bin/env sh
set -eu

APP_DIR=""

usage() {
  cat <<'EOF'
KM VMS canonical terminal update launcher

Usage:
  sh data/update-runtime/bootstrap/current/km-vms-update-launcher.sh \
    --app-dir <path> [update.sh options]

The launcher resolves the exact validated active release without repairing or
resuming activation, then delegates all remaining options to its update.sh.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --app-dir)
    [ "$#" -ge 2 ] || fail "--app-dir requires a value"
    APP_DIR="$2"
    shift 2
    ;;
  *)
    fail "--app-dir must be the first argument"
    ;;
esac

case "$APP_DIR" in
  /*) ;;
  *) fail "--app-dir must be an absolute path" ;;
esac
[ -d "$APP_DIR" ] || fail "KM VMS app directory is unavailable"
APP_DIR=$(CDPATH= cd -- "$APP_DIR" && pwd -P) ||
  fail "KM VMS app directory cannot be resolved"
case "$APP_DIR" in
  /|/bin|/boot|/dev|/etc|/lib|/lib64|/proc|/run|/sbin|/sys|/usr|/var|/root|/tmp)
    fail "Refusing dangerous app directory"
    ;;
esac
[ -f "$APP_DIR/.env" ] || fail "Stable KM VMS .env is unavailable"
[ -d "$APP_DIR/data/update-runtime/slots" ] ||
  fail "Canonical KM VMS release slots are unavailable"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) ||
  fail "Launcher directory cannot be resolved"
CURRENT_BUNDLE="$APP_DIR/data/update-runtime/bootstrap/current"
[ -L "$CURRENT_BUNDLE" ] && [ -d "$CURRENT_BUNDLE" ] ||
  fail "Stable bootstrap bundle is unavailable"
CURRENT_BUNDLE_REAL=$(CDPATH= cd -- "$CURRENT_BUNDLE" && pwd -P) ||
  fail "Stable bootstrap bundle cannot be resolved"
[ "$SCRIPT_DIR" = "$CURRENT_BUNDLE_REAL" ] ||
  fail "Run the update launcher from the current stable bootstrap bundle"

BOOTSTRAP="$CURRENT_BUNDLE/km-vms-bootstrap.py"
[ -f "$BOOTSTRAP" ] && [ ! -L "$BOOTSTRAP" ] ||
  fail "Stable bootstrap authority is unavailable"
command -v python3 >/dev/null 2>&1 ||
  fail "python3 is required for canonical terminal update resolution"

SOURCE_DIR=$(python3 -B "$BOOTSTRAP" resolve-path --app-dir "$APP_DIR") ||
  fail "Canonical active release is unavailable; complete recovery before starting a new update"
case "$SOURCE_DIR" in
  "$APP_DIR"/data/update-runtime/slots/*/source) ;;
  *) fail "Canonical active release escaped the slot layout" ;;
esac
SLOT_ID=${SOURCE_DIR#"$APP_DIR/data/update-runtime/slots/"}
SLOT_ID=${SLOT_ID%%/*}
printf '%s\n' "$SLOT_ID" |
  grep -Eq '^(release-[0-9a-f]{40}|adopted-[0-9a-f]{64}|initial-[0-9a-f]{64})$' ||
  fail "Canonical active release identity is invalid"
SLOTS_REAL=$(CDPATH= cd -- "$APP_DIR/data/update-runtime/slots" && pwd -P) ||
  fail "Canonical release-slot root cannot be resolved"
SOURCE_REAL=$(CDPATH= cd -- "$SOURCE_DIR" && pwd -P) ||
  fail "Canonical active release source cannot be resolved"
[ "$SOURCE_REAL" = "$SLOTS_REAL/$SLOT_ID/source" ] ||
  fail "Canonical active release source escaped the slot layout"

UPDATE_SCRIPT="$SOURCE_REAL/scripts/update.sh"
[ -f "$UPDATE_SCRIPT" ] && [ ! -L "$UPDATE_SCRIPT" ] ||
  fail "Canonical active release updater is unavailable"

KM_VMS_PRODUCT_SOURCE_DIR="$SOURCE_REAL"
export KM_VMS_PRODUCT_SOURCE_DIR
cd "$APP_DIR"
exec sh "$UPDATE_SCRIPT" "$@"
