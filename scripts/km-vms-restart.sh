#!/usr/bin/env sh
set -eu

APP_DIR="${KM_VMS_APP_DIR:-}"
PROJECT_NAME="${KM_VMS_PROJECT_NAME:-}"
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-}"

usage() {
  cat <<'EOF'
KM VMS safe restart helper

Usage:
  sh scripts/km-vms-restart.sh --app-dir <path> [--project-name <name>] [--help]

Environment equivalents:
  KM_VMS_APP_DIR, KM_VMS_PROJECT_NAME, KM_VMS_DOCKER_COMPOSE.

This helper does not regenerate .env, does not remove volumes, and does not
create users/settings. It only restarts the existing compose application.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

validate_compose_override() {
  override="$1"
  case "$override" in
    *[\;\|\&\`\>\<\(\)]*|*'$('*|*'$'*|*" "*|*"	"*) fail "KM_VMS_DOCKER_COMPOSE contains unsafe characters or spaces." ;;
  esac
  case "$override" in
    docker)
      command_exists docker || fail "KM_VMS_DOCKER_COMPOSE=docker but docker was not found."
      docker compose version >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE=docker but docker compose is not available."
      COMPOSE_KIND="plugin"
      COMPOSE_BIN="docker"
      return 0
      ;;
    docker-compose)
      command_exists docker-compose || fail "KM_VMS_DOCKER_COMPOSE=docker-compose but docker-compose was not found."
      docker-compose version >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE=docker-compose is not usable."
      COMPOSE_KIND="standalone"
      COMPOSE_BIN="docker-compose"
      return 0
      ;;
    *)
      [ -x "$override" ] || fail "KM_VMS_DOCKER_COMPOSE must be docker, docker-compose, or an executable path."
      "$override" version >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE executable is not a usable compose command."
      COMPOSE_KIND="standalone"
      COMPOSE_BIN="$override"
      return 0
      ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-dir)
      [ "$#" -ge 2 ] || fail "--app-dir requires a value"
      APP_DIR="$2"
      shift 2
      ;;
    --project-name)
      [ "$#" -ge 2 ] || fail "--project-name requires a value"
      PROJECT_NAME="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

normalize_path() {
  value="$1"
  case "$value" in
    /*) ;;
    *) value="$(pwd)/$value" ;;
  esac
  parent=$(dirname "$value")
  base=$(basename "$value")
  [ -d "$parent" ] || fail "Parent directory does not exist: $parent"
  parent_real=$(cd "$parent" && pwd -P)
  if [ "$parent_real" = "/" ]; then
    printf '/%s\n' "$base"
  else
    printf '%s/%s\n' "$parent_real" "$base"
  fi
}

detect_compose() {
  if [ -n "$DOCKER_COMPOSE_BIN" ]; then
    validate_compose_override "$DOCKER_COMPOSE_BIN"
    return 0
  fi
  if command_exists docker && docker compose version >/dev/null 2>&1; then
    COMPOSE_KIND="plugin"
    COMPOSE_BIN="docker"
    return 0
  fi
  if command_exists docker-compose && docker-compose version >/dev/null 2>&1; then
    COMPOSE_KIND="standalone"
    COMPOSE_BIN="docker-compose"
    return 0
  fi
  fail "Docker Compose was not found."
}

compose_cmd() {
  if [ "$COMPOSE_KIND" = "plugin" ]; then
    docker compose "$@"
  else
    "$COMPOSE_BIN" "$@"
  fi
}

[ -n "$APP_DIR" ] || fail "--app-dir or KM_VMS_APP_DIR is required."
APP_DIR=$(normalize_path "$APP_DIR")
[ -f "$APP_DIR/docker-compose.yml" ] || fail "docker-compose.yml not found in app dir: $APP_DIR"
[ -f "$APP_DIR/.env" ] || fail ".env not found in app dir: $APP_DIR"

detect_compose

set -- --env-file "$APP_DIR/.env"
if [ -n "$PROJECT_NAME" ]; then
  set -- "$@" --project-name "$PROJECT_NAME"
fi

(
  cd "$APP_DIR"
  compose_cmd "$@" config >/dev/null
  compose_cmd "$@" up -d
)

printf 'KM VMS restart command completed for %s\n' "$APP_DIR"
