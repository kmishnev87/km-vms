#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SCRIPT_DIR="$ROOT_DIR/scripts"
cd "$ROOT_DIR"

. "$SCRIPT_DIR/km-vms-compose-common.sh"

if [ "$#" -eq 0 ]; then
  set -- apps/api/tests
fi

OVERRIDE="${KMVMS_DOCKER_COMPOSE:-${KM_VMS_DOCKER_COMPOSE:-}}"
km_vms_detect_compose "$OVERRIDE" || {
  echo "Docker Compose command not found. Checked KMVMS_DOCKER_COMPOSE/KM_VMS_DOCKER_COMPOSE, PATH docker compose/docker-compose, and known NAS vendor paths." >&2
  exit 127
}

km_vms_compose_cmd -f docker-compose.pytest.yml run --rm backend-pytest pytest "$@"
