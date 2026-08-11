#!/bin/sh
set -eu

DEFAULT_DOCKER_BIN="/Volume1/@apps/DockerEngine/dockerd/bin/docker"
DEFAULT_IMAGE="km-vms-playwright-tools:1.44.1"
STAGE_TMP_PREFIX="/tmp/km-vms-stage-13-5-7-0-"

fail_config() {
  printf '%s\n' "CONFIG_FAIL $1" >&2
  exit 2
}

print_help() {
  cat <<'EOF'
Usage: KMVMS_USERNAME=... KMVMS_PASSWORD=... sh scripts/km-vms-browser-smoke.sh [--validate-config]

Runs the safe KM VMS core browser smoke on the active working-NAS loopback origin.
Optional: DOCKER_BIN, PLAYWRIGHT_IMAGE, KMVMS_BASE_URL, KMVMS_SMOKE_OUT_DIR.
EOF
}

MODE="run"
case "${1:-}" in
  "") ;;
  --help|-h)
    print_help
    exit 0
    ;;
  --validate-config)
    MODE="validate"
    ;;
  *)
    fail_config "unsupported_argument"
    ;;
esac
[ "$#" -le 1 ] || fail_config "too_many_arguments"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PRODUCT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
SCENARIO_DIR="$SCRIPT_DIR/browser-smoke"
SCENARIO_PATH="$SCENARIO_DIR/core-smoke.js"
DOCKER_BIN="${DOCKER_BIN:-$DEFAULT_DOCKER_BIN}"
IMAGE="${PLAYWRIGHT_IMAGE:-$DEFAULT_IMAGE}"

case "$DOCKER_BIN" in
  /*) ;;
  *) fail_config "docker_bin_not_absolute" ;;
esac
[ -x "$DOCKER_BIN" ] || fail_config "docker_bin_unavailable"
[ -f "$SCENARIO_PATH" ] || fail_config "scenario_missing"

case "$IMAGE" in
  ""|-*|*[!A-Za-z0-9._/:@-]*) fail_config "invalid_image_reference" ;;
esac

nginx_ids="$("$DOCKER_BIN" ps \
  --filter "label=com.docker.compose.project.working_dir=$PRODUCT_ROOT" \
  --filter "label=com.docker.compose.service=nginx" \
  --format '{{.ID}}')"
set -- $nginx_ids
[ "$#" -eq 1 ] || fail_config "working_nas_nginx_not_unique"
nginx_id="$1"

approved_port="$("$DOCKER_BIN" inspect \
  --format '{{(index (index .NetworkSettings.Ports "80/tcp") 0).HostPort}}' \
  "$nginx_id")"
case "$approved_port" in
  ""|*[!0-9]*) fail_config "invalid_published_http_port" ;;
esac
[ "$approved_port" -ge 1 ] 2>/dev/null || fail_config "invalid_published_http_port"
[ "$approved_port" -le 65535 ] 2>/dev/null || fail_config "invalid_published_http_port"

BASE_URL="${KMVMS_BASE_URL:-http://127.0.0.1:$approved_port}"
case "$BASE_URL" in
  "http://127.0.0.1:$approved_port"|"http://localhost:$approved_port"|"http://[::1]:$approved_port") ;;
  *) fail_config "base_url_not_approved_working_nas_origin" ;;
esac

KMVMS_USERNAME="${KMVMS_USERNAME:-}"
KMVMS_PASSWORD="${KMVMS_PASSWORD:-}"
[ -n "$KMVMS_USERNAME" ] || fail_config "username_required"
[ -n "$KMVMS_PASSWORD" ] || fail_config "password_required"

if [ -n "${KMVMS_SMOKE_OUT_DIR:-}" ]; then
  OUT_DIR="$KMVMS_SMOKE_OUT_DIR"
else
  stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  OUT_DIR="${STAGE_TMP_PREFIX}${stamp}/smoke"
fi

case "$OUT_DIR" in
  /tmp/km-vms-stage-13-5-7-0-*/smoke) ;;
  *) fail_config "output_not_stage_scoped" ;;
esac
RUN_ROOT="$(dirname -- "$OUT_DIR")"
[ "$(dirname -- "$RUN_ROOT")" = "/tmp" ] || fail_config "output_not_direct_tmp_child"
[ "$(basename -- "$OUT_DIR")" = "smoke" ] || fail_config "output_leaf_invalid"
case "$(basename -- "$RUN_ROOT")" in
  km-vms-stage-13-5-7-0-*) ;;
  *) fail_config "output_run_root_invalid" ;;
esac

if [ "$MODE" = "validate" ]; then
  printf '%s\n' "CONFIG_PASS $BASE_URL"
  exit 0
fi

"$DOCKER_BIN" image inspect "$IMAGE" >/dev/null 2>&1 || fail_config "playwright_image_unavailable"

[ ! -L "$RUN_ROOT" ] || fail_config "output_run_root_symlink"
[ ! -L "$OUT_DIR" ] || fail_config "output_symlink"
mkdir -p "$OUT_DIR"
canonical_out="$(CDPATH= cd -P -- "$OUT_DIR" && pwd)"
[ "$canonical_out" = "$OUT_DIR" ] || fail_config "output_canonical_path_mismatch"

export KMVMS_USERNAME KMVMS_PASSWORD

set +e
"$DOCKER_BIN" run --rm \
  --network host \
  -e KMVMS_BASE_URL="$BASE_URL" \
  -e KMVMS_APPROVED_ORIGIN="$BASE_URL" \
  -e KMVMS_APPROVED_HTTP_PORT="$approved_port" \
  -e KMVMS_USERNAME \
  -e KMVMS_PASSWORD \
  -e KMVMS_SMOKE_OUT_DIR="/artifacts" \
  -e NODE_PATH="/usr/lib/node_modules" \
  -v "$SCENARIO_DIR:/work:ro" \
  -v "$OUT_DIR:/artifacts" \
  "$IMAGE" \
  node /work/core-smoke.js >/dev/null 2>&1
smoke_status=$?
set -e

unset KMVMS_USERNAME KMVMS_PASSWORD

if [ "$smoke_status" -eq 0 ]; then
  printf '%s\n' "SMOKE_PASS $OUT_DIR"
  exit 0
fi

printf '%s\n' "SMOKE_FAIL $OUT_DIR" >&2
exit "$smoke_status"
