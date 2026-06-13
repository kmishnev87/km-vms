#!/usr/bin/env sh

km_vms_compose_fail() {
  if command -v fail >/dev/null 2>&1; then
    fail "$@"
  fi
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

km_vms_command_exists() {
  command -v "$1" >/dev/null 2>&1
}

km_vms_validate_compose_override() {
  override="$1"
  if [ "$override" = "docker compose" ]; then
    km_vms_command_exists docker || km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE=\"docker compose\" but docker was not found."
    docker compose version >/dev/null 2>&1 || km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE=\"docker compose\" but docker compose is not available."
    COMPOSE_KIND="plugin"
    COMPOSE_BIN="docker"
    COMPOSE_SOURCE="override"
    return 0
  fi
  case "$override" in
    *[\;\|\&\`\>\<\(\)]*|*'$('*|*'$'*|*" "*|*"	"*) km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE contains unsafe characters or spaces." ;;
  esac
  case "$override" in
    docker)
      km_vms_command_exists docker || km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE=docker but docker was not found."
      docker compose version >/dev/null 2>&1 || km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE=docker but docker compose is not available."
      COMPOSE_KIND="plugin"
      COMPOSE_BIN="docker"
      COMPOSE_SOURCE="override"
      return 0
      ;;
    docker-compose)
      km_vms_command_exists docker-compose || km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE=docker-compose but docker-compose was not found."
      docker-compose version >/dev/null 2>&1 || km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE=docker-compose is not usable."
      COMPOSE_KIND="standalone"
      COMPOSE_BIN="docker-compose"
      COMPOSE_SOURCE="override"
      return 0
      ;;
    *)
      [ -x "$override" ] || km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE must be docker, docker-compose, docker compose, or an executable path."
      if "$override" compose version >/dev/null 2>&1; then
        COMPOSE_KIND="plugin"
        COMPOSE_BIN="$override"
        COMPOSE_SOURCE="override"
        return 0
      fi
      case "$(basename "$override")" in
        docker) km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE points to docker, but docker compose is not available for that binary." ;;
      esac
      "$override" version >/dev/null 2>&1 || km_vms_compose_fail "KM_VMS_DOCKER_COMPOSE executable is not a usable compose command."
      COMPOSE_KIND="standalone"
      COMPOSE_BIN="$override"
      COMPOSE_SOURCE="override"
      return 0
      ;;
  esac
}

km_vms_try_compose_candidate() {
  candidate="$1"
  if [ ! -x "$candidate" ]; then
    return 1
  fi
  if "$candidate" compose version >/dev/null 2>&1; then
    COMPOSE_KIND="plugin"
    COMPOSE_BIN="$candidate"
    COMPOSE_SOURCE="vendor-path"
    return 0
  fi
  case "$(basename "$candidate")" in
    docker) return 1 ;;
  esac
  if "$candidate" version >/dev/null 2>&1; then
    COMPOSE_KIND="standalone"
    COMPOSE_BIN="$candidate"
    COMPOSE_SOURCE="vendor-path"
    return 0
  fi
  return 1
}

km_vms_detect_compose() {
  override="${1:-}"
  COMPOSE_KIND=""
  COMPOSE_BIN=""
  COMPOSE_SOURCE=""
  if [ -n "$override" ]; then
    km_vms_validate_compose_override "$override"
    return 0
  fi
  if km_vms_command_exists docker && docker compose version >/dev/null 2>&1; then
    COMPOSE_KIND="plugin"
    COMPOSE_BIN="docker"
    COMPOSE_SOURCE="path"
    return 0
  fi
  if km_vms_command_exists docker-compose && docker-compose version >/dev/null 2>&1; then
    COMPOSE_KIND="standalone"
    COMPOSE_BIN="docker-compose"
    COMPOSE_SOURCE="path"
    return 0
  fi
  for candidate in \
    /Volume*/@apps/DockerEngine/dockerd/bin/docker \
    /Volume*/@apps/DockerEngine/dockerd/bin/docker-compose \
    /var/packages/ContainerManager/target/usr/bin/docker \
    /var/packages/ContainerManager/target/usr/bin/docker-compose \
    /var/packages/Docker/usr/bin/docker \
    /var/packages/Docker/usr/bin/docker-compose \
    /share/*/.qpkg/container-station/bin/docker \
    /share/*/.qpkg/container-station/bin/docker-compose \
    /usr/local/bin/docker \
    /usr/local/bin/docker-compose \
    /usr/bin/docker-compose
  do
    if km_vms_try_compose_candidate "$candidate"; then
      return 0
    fi
  done
  return 1
}

km_vms_compose_cmd() {
  if [ "${COMPOSE_KIND:-}" = "plugin" ]; then
    "$COMPOSE_BIN" compose "$@"
  else
    "$COMPOSE_BIN" "$@"
  fi
}

km_vms_compose_version() {
  if [ "${COMPOSE_KIND:-}" = "plugin" ]; then
    "$COMPOSE_BIN" compose version 2>/dev/null | head -n 1
  else
    "$COMPOSE_BIN" version 2>/dev/null | head -n 1
  fi
}
