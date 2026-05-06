#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ "$#" -eq 0 ]; then
  set -- apps/api/tests
fi

if [ -n "${KMVMS_DOCKER_COMPOSE:-}" ]; then
  set -- "$KMVMS_DOCKER_COMPOSE" -f docker-compose.pytest.yml run --rm backend-pytest pytest "$@"
  exec "$@"
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  exec docker compose -f docker-compose.pytest.yml run --rm backend-pytest pytest "$@"
fi

if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f docker-compose.pytest.yml run --rm backend-pytest pytest "$@"
fi

if [ -x /Volume1/@apps/DockerEngine/dockerd/bin/docker-compose ]; then
  exec /Volume1/@apps/DockerEngine/dockerd/bin/docker-compose -f docker-compose.pytest.yml run --rm backend-pytest pytest "$@"
fi

echo "Docker Compose command not found. Set KMVMS_DOCKER_COMPOSE to the compose binary path." >&2
exit 127
