#!/usr/bin/env sh
set -eu

REPO_URL_DEFAULT="https://github.com/kmishnev87/km-vms.git"
BRANCH_DEFAULT="main"
HTTP_PORT_DEFAULT="8088"
API_PORT_DEFAULT="18000"
PROJECT_NAME_DEFAULT="km-vms"
TZ_DEFAULT="Asia/Yekaterinburg"

APP_DIR="${KM_VMS_APP_DIR:-}"
REPO_URL="${KM_VMS_REPO_URL:-$REPO_URL_DEFAULT}"
BRANCH="${KM_VMS_BRANCH:-$BRANCH_DEFAULT}"
HTTP_PORT="${KM_VMS_HTTP_PORT:-$HTTP_PORT_DEFAULT}"
API_PORT="${KM_VMS_API_PORT:-}"
PROJECT_NAME="${KM_VMS_PROJECT_NAME:-$PROJECT_NAME_DEFAULT}"
SOURCE_DIR="${KM_VMS_SOURCE_DIR:-}"
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-}"
YES="${KM_VMS_YES:-0}"
DRY_RUN=0
APP_DIR_CREATED=0

usage() {
  cat <<'EOF'
KM VMS installer

Usage:
  sh scripts/install.sh --app-dir <path> [options]

Options:
  --app-dir <path>       Installation directory.
  --repo-url <url>       Git repository URL. Default: public KM VMS repo.
  --branch <branch>      Git branch/tag to clone. Default: main.
  --http-port <port>     Host HTTP port for nginx. Default: 8088.
  --project-name <name>  Compose project/container prefix. Default: km-vms.
  --source-dir <path>    Development/testing mode: copy local product source.
  --yes                  Non-interactive confirmation.
  --dry-run              Validate inputs and print plan without writing.
  --help                 Show this help.

Environment equivalents:
  KM_VMS_APP_DIR, KM_VMS_REPO_URL, KM_VMS_BRANCH, KM_VMS_HTTP_PORT,
  KM_VMS_API_PORT, KM_VMS_PROJECT_NAME, KM_VMS_SOURCE_DIR,
  KM_VMS_DOCKER_COMPOSE, KM_VMS_YES=1.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

acquisition_fail() {
  printf 'ERROR: %s\n' "$*" >&2
  if [ "$APP_DIR_CREATED" = "1" ]; then
    printf 'No .env was generated and compose was not started. Safe cleanup for this failed run: rm -rf %s\n' "$APP_DIR" >&2
  else
    printf 'No .env was generated and compose was not started.\n' >&2
  fi
  exit 1
}

info() {
  printf '%s\n' "$*"
}

confirm() {
  prompt="$1"
  if [ "$YES" = "1" ]; then
    return 0
  fi
  printf '%s [y/N] ' "$prompt"
  read answer
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) fail "Cancelled." ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-dir)
      [ "$#" -ge 2 ] || fail "--app-dir requires a value"
      APP_DIR="$2"
      shift 2
      ;;
    --repo-url)
      [ "$#" -ge 2 ] || fail "--repo-url requires a value"
      REPO_URL="$2"
      shift 2
      ;;
    --branch)
      [ "$#" -ge 2 ] || fail "--branch requires a value"
      BRANCH="$2"
      shift 2
      ;;
    --http-port)
      [ "$#" -ge 2 ] || fail "--http-port requires a value"
      HTTP_PORT="$2"
      shift 2
      ;;
    --project-name)
      [ "$#" -ge 2 ] || fail "--project-name requires a value"
      PROJECT_NAME="$2"
      shift 2
      ;;
    --source-dir)
      [ "$#" -ge 2 ] || fail "--source-dir requires a value"
      SOURCE_DIR="$2"
      shift 2
      ;;
    --yes)
      YES=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
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
  [ "$value" = "/" ] && {
    printf '/\n'
    return
  }
  parent=$(dirname "$value")
  base=$(basename "$value")
  if [ -d "$parent" ]; then
    parent_real=$(cd "$parent" && pwd -P)
  else
    parent_parent=$(dirname "$parent")
    parent_base=$(basename "$parent")
    [ -d "$parent_parent" ] || fail "Parent directory does not exist: $parent_parent"
    parent_real="$(cd "$parent_parent" && pwd -P)/$parent_base"
  fi
  if [ "$parent_real" = "/" ]; then
    printf '/%s\n' "$base"
  else
    printf '%s/%s\n' "$parent_real" "$base"
  fi
}

is_dangerous_app_dir() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/lib|/lib64|/proc|/run|/sbin|/sys|/usr|/var|/root) return 0 ;;
    /tmp) [ -n "$SOURCE_DIR" ] && return 1; return 0 ;;
    *) return 1 ;;
  esac
}

is_dangerous_source_dir() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/lib|/lib64|/proc|/run|/sbin|/sys|/usr|/var|/root|/tmp) return 0 ;;
    *) return 1 ;;
  esac
}

path_equal_or_nested() {
  parent="$1"
  child="$2"
  [ "$parent" = "$child" ] && return 0
  case "$child" in
    "$parent"/*) return 0 ;;
    *) return 1 ;;
  esac
}

validate_port() {
  port="$1"
  case "$port" in
    ''|*[!0-9]*) fail "HTTP port must be numeric: $port" ;;
  esac
  [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || fail "HTTP port must be in 1..65535"
  if [ "$port" -lt 1024 ]; then
    confirm "Port $port is privileged and may require elevated host privileges. Continue?"
  fi
}

derive_api_port() {
  if [ -n "$API_PORT" ]; then
    return
  fi
  if [ "$HTTP_PORT" -le 55535 ]; then
    API_PORT=$((HTTP_PORT + 10000))
  elif [ "$HTTP_PORT" -gt 10000 ]; then
    API_PORT=$((HTTP_PORT - 10000))
  else
    API_PORT="$API_PORT_DEFAULT"
  fi
}

port_busy() {
  port="$1"
  if command_exists ss; then
    ss -ltn 2>/dev/null | grep -q "[.:]$port "
    return $?
  fi
  if command_exists netstat; then
    netstat -ltn 2>/dev/null | grep -q "[.:]$port "
    return $?
  fi
  return 1
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
  fail "Docker Compose was not found. Install Docker with the compose plugin, or docker-compose."
}

validate_compose_override() {
  override="$1"
  if [ "$override" = "docker compose" ]; then
    command_exists docker || fail "KM_VMS_DOCKER_COMPOSE=\"docker compose\" but docker was not found."
    docker compose version >/dev/null 2>&1 || fail "KM_VMS_DOCKER_COMPOSE=\"docker compose\" but docker compose is not available."
    COMPOSE_KIND="plugin"
    COMPOSE_BIN="docker"
    return 0
  fi
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

compose_cmd() {
  if [ "$COMPOSE_KIND" = "plugin" ]; then
    docker compose "$@"
  else
    "$COMPOSE_BIN" "$@"
  fi
}

check_docker() {
  detect_compose
  if command_exists docker; then
    docker version >/dev/null 2>&1 || fail "Docker is not reachable for the current user."
    docker info >/dev/null 2>&1 || fail "Docker daemon is not reachable for the current user."
  elif [ "$COMPOSE_KIND" != "standalone" ]; then
    fail "Docker was not found. Install Docker before running this installer."
  fi
}

safe_project_name() {
  value="$1"
  [ -n "$value" ] || fail "Project name must not be empty."
  case "$value" in
    *[A-Z]*)
      fail "Project name must be lowercase. Use: $(printf '%s' "$value" | tr 'A-Z' 'a-z')"
      ;;
  esac
  if printf '%s' "$value" | grep -Eq '^[a-z][a-z0-9_-]*$'; then
      printf '%s\n' "$value"
      return 0
  fi
  fail "Project name must start with a lowercase letter and contain only lowercase letters, digits, dashes or underscores."
}

random_secret() {
  if command_exists openssl; then
    openssl rand -hex 32
    return
  fi
  if [ -r /dev/urandom ] && command_exists od; then
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    printf '\n'
    return
  fi
  fail "Cannot generate secure secrets: openssl or /dev/urandom+od is required."
}

write_env() {
  env_file="$APP_DIR/.env"
  [ ! -e "$env_file" ] || fail ".env already exists; refusing to overwrite: $env_file"
  archive_dir="$APP_DIR/data/archive"
  mkdir -p "$archive_dir" "$APP_DIR/data/previews" "$APP_DIR/data/exports" "$APP_DIR/data/install-control"
  pg_secret=$(random_secret)
  jwt_secret=$(random_secret)
  enc_secret=$(random_secret)
  admin_secret=$(random_secret)
  umask 077
  {
    printf 'TZ=%s\n' "$TZ_DEFAULT"
    printf 'POSTGRES_PASSWORD=%s\n' "$pg_secret"
    printf 'JWT_SECRET=%s\n' "$jwt_secret"
    printf 'ENCRYPTION_KEY=%s\n' "$enc_secret"
    printf 'SURVEILLANCE_ROOT=%s\n' "$archive_dir"
    printf 'STORAGE_INSTALL_CONTROL=%s\n' "$APP_DIR/data/install-control"
    printf 'HTTP_PORT=%s\n' "$HTTP_PORT"
    printf 'API_PORT=%s\n' "$API_PORT"
    printf 'COMPOSE_PROJECT_NAME=%s\n' "$PROJECT_NAME"
    printf 'KM_VMS_CONTAINER_PREFIX=%s\n' "$PROJECT_NAME"
    printf 'NEXT_PUBLIC_API_BASE_URL=/api\n'
    printf 'LIVE_HWACCEL_MODE=auto\n'
    printf 'LIVE_HWACCEL_BACKEND=auto\n'
    printf 'LIVE_HWACCEL_DEVICE=/dev/dri/renderD128\n'
    printf 'VIDEO_GID=44\n'
    printf 'RENDER_GID=109\n'
    printf 'ADMIN_USERNAME=admin\n'
    printf 'ADMIN_PASSWORD=%s\n' "$admin_secret"
    printf 'ADMIN_FULL_NAME=Admin\n'
  } > "$env_file"
  chmod 600 "$env_file" 2>/dev/null || true
}

write_metadata() {
  metadata="$APP_DIR/.km-vms-install.json"
  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  source_summary="git"
  [ -n "$SOURCE_DIR" ] && source_summary="source-dir"
  {
    printf '{\n'
    printf '  "app_dir": "%s",\n' "$APP_DIR"
    printf '  "project_name": "%s",\n' "$PROJECT_NAME"
    printf '  "http_port": "%s",\n' "$HTTP_PORT"
    printf '  "compose_command": "%s",\n' "$COMPOSE_KIND"
    printf '  "created_at": "%s",\n' "$created_at"
    printf '  "source_mode": "%s",\n' "$source_summary"
    printf '  "setup_url": "http://localhost:%s/setup"\n' "$HTTP_PORT"
    printf '}\n'
  } > "$metadata"
}

validate_source_dir() {
  [ -n "$SOURCE_DIR" ] || return 0
  is_dangerous_source_dir "$SOURCE_DIR" && fail "Refusing dangerous source dir: $SOURCE_DIR"
  path_equal_or_nested "$SOURCE_DIR" "$APP_DIR" && fail "APP_DIR must be outside SOURCE_DIR for --source-dir mode."
  path_equal_or_nested "$APP_DIR" "$SOURCE_DIR" && fail "SOURCE_DIR must be outside APP_DIR for --source-dir mode."
  [ -d "$SOURCE_DIR" ] || fail "Source directory does not exist: $SOURCE_DIR"
  [ -f "$SOURCE_DIR/docker-compose.yml" ] || fail "Source directory is not a KM VMS source tree: $SOURCE_DIR"
  [ -d "$SOURCE_DIR/apps/api" ] || fail "Source directory is missing apps/api: $SOURCE_DIR"
  [ -d "$SOURCE_DIR/apps/web" ] || fail "Source directory is missing apps/web: $SOURCE_DIR"
  [ -f "$SOURCE_DIR/deploy/nginx/default.conf" ] || fail "Source directory is missing deploy/nginx/default.conf: $SOURCE_DIR"
  command_exists tar || fail "tar is required for --source-dir copy mode"
}

copy_source_dir() {
  info "Copying product source from --source-dir development/testing mode..."
  (
    cd "$SOURCE_DIR"
    tar \
      --exclude='./.git' --exclude='*/.git' \
      --exclude='./.env' --exclude='*/.env' \
      --exclude='./.env.*' --exclude='*/.env.*' \
      --exclude='./node_modules' --exclude='*/node_modules' \
      --exclude='./.next' --exclude='*/.next' \
      --exclude='./dist' --exclude='*/dist' \
      --exclude='./build' --exclude='*/build' \
      --exclude='./coverage' --exclude='*/coverage' \
      --exclude='./__pycache__' --exclude='*/__pycache__' \
      --exclude='*.pyc' --exclude='*.pyo' \
      --exclude='./logs' --exclude='*/logs' \
      --exclude='./log' --exclude='*/log' \
      --exclude='./data' --exclude='*/data' \
      --exclude='./archive' --exclude='*/archive' \
      --exclude='./archives' --exclude='*/archives' \
      --exclude='./recordings' --exclude='*/recordings' \
      --exclude='./videos' --exclude='*/videos' \
      --exclude='./service-artifacts' --exclude='*/service-artifacts' \
      --exclude='./service_artifacts' --exclude='*/service_artifacts' \
      --exclude='./Working folder' --exclude='*/Working folder' \
      --exclude='./Working' --exclude='*/Working' \
      --exclude='./Current Stage' --exclude='*/Current Stage' \
      --exclude='./Current' --exclude='*/Current' \
      --exclude='*.zip' --exclude='*.tar' --exclude='*.tar.gz' --exclude='*.tgz' --exclude='*.rar' --exclude='*.7z' \
      --exclude='*.key' --exclude='*.pem' --exclude='*.crt' --exclude='*.csr' \
      --exclude='./.ssh' --exclude='*/.ssh' \
      --exclude='id_rsa' --exclude='*/id_rsa' \
      --exclude='id_ed25519' --exclude='*/id_ed25519' \
      --exclude='*.p12' --exclude='*.pfx' \
      --exclude='*secret*' --exclude='*Secret*' --exclude='*SECRET*' \
      --exclude='*credential*' --exclude='*Credential*' --exclude='*CREDENTIAL*' \
      --exclude='./credentials' --exclude='*/credentials' \
      --exclude='./credential' --exclude='*/credential' \
      --exclude='./secrets' --exclude='*/secrets' \
      --exclude='./secret' --exclude='*/secret' \
      --exclude='./km-vms-stage1-*' --exclude='*/km-vms-stage1-*' \
      --exclude='./stage1_installer_*' --exclude='*/stage1_installer_*' \
      --exclude='./km-vms-stage1-0-1-*' --exclude='*/km-vms-stage1-0-1-*' \
      -cf - .
  ) | (cd "$APP_DIR" && tar -xf -) || acquisition_fail "Source-dir acquisition failed."
}

clone_repo() {
  command_exists git || fail "git is required for repository acquisition."
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR" || acquisition_fail "Repository acquisition failed."
}

prepare_app_dir() {
  if [ -e "$APP_DIR" ]; then
    [ -d "$APP_DIR" ] || fail "App dir exists and is not a directory: $APP_DIR"
    if [ "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')" -gt 0 ]; then
      if [ -f "$APP_DIR/docker-compose.yml" ] && [ -d "$APP_DIR/apps/api" ]; then
        if [ -d "$APP_DIR/.git" ]; then
          dirty=$(cd "$APP_DIR" && git status --porcelain 2>/dev/null || true)
          [ -z "$dirty" ] || fail "Existing KM VMS repo has local changes; refusing to update."
        fi
        return 0
      fi
      fail "App dir is non-empty and is not recognized as a KM VMS install: $APP_DIR"
    fi
    return 0
  fi
  confirm "Create KM VMS app directory: $APP_DIR?"
  mkdir -p "$APP_DIR"
  APP_DIR_CREATED=1
}

probe_write() {
  probe="$APP_DIR/.km-vms-write-test.$$"
  printf 'ok\n' > "$probe" || fail "Cannot write to app dir: $APP_DIR"
  rm -f "$probe" || fail "Cannot remove write probe from app dir: $APP_DIR"
}

lan_hint() {
  if command_exists hostname; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
    if [ -n "$ip" ]; then
      printf 'LAN setup URL hint: http://%s:%s/setup\n' "$ip" "$HTTP_PORT"
      return
    fi
  fi
  info "LAN IP was not detected automatically. Open http://<NAS-IP>:$HTTP_PORT/setup manually."
}

print_plan() {
  info "KM VMS install plan"
  info "OS: $(uname -s 2>/dev/null || printf unknown)"
  info "Arch: $(uname -m 2>/dev/null || printf unknown)"
  info "User: $(id -un 2>/dev/null || printf unknown)"
  info "Compose: $COMPOSE_KIND"
  info "App dir: $APP_DIR"
  info "HTTP port: $HTTP_PORT"
  info "API port: $API_PORT"
  info "Project name: $PROJECT_NAME"
  if [ -n "$SOURCE_DIR" ]; then
    info "Source mode: --source-dir development/testing copy"
  else
    info "Source mode: git clone from configured repo"
  fi
}

if [ -z "$APP_DIR" ]; then
  if [ "$YES" = "1" ] || [ "$DRY_RUN" = "1" ]; then
    fail "--app-dir or KM_VMS_APP_DIR is required in non-interactive/dry-run mode."
  fi
  [ -n "${HOME:-}" ] || fail "--app-dir is required when HOME is not set."
  APP_DIR="$HOME/km-vms"
fi

PROJECT_NAME=$(safe_project_name "$PROJECT_NAME")
APP_DIR=$(normalize_path "$APP_DIR")
[ -n "$SOURCE_DIR" ] && SOURCE_DIR=$(normalize_path "$SOURCE_DIR")
is_dangerous_app_dir "$APP_DIR" && fail "Refusing dangerous app dir: $APP_DIR"
validate_port "$HTTP_PORT"
derive_api_port
validate_port "$API_PORT"
validate_source_dir
check_docker

if port_busy "$HTTP_PORT"; then
  fail "HTTP port is already listening: $HTTP_PORT"
fi
if port_busy "$API_PORT"; then
  fail "API port is already listening: $API_PORT"
fi

print_plan

if [ "$DRY_RUN" = "1" ]; then
  info "Dry-run complete. No files were written and compose was not started."
  exit 0
fi

prepare_app_dir
probe_write

if [ ! -f "$APP_DIR/docker-compose.yml" ]; then
  if [ -n "$SOURCE_DIR" ]; then
    copy_source_dir
  else
    clone_repo
  fi
fi

[ -f "$APP_DIR/docker-compose.yml" ] || fail "Project acquisition did not produce docker-compose.yml."
[ -f "$APP_DIR/deploy/nginx/default.conf" ] || fail "Project acquisition is incomplete: deploy/nginx/default.conf is missing."
[ -f "$APP_DIR/scripts/km-vms-storage-discovery.sh" ] || fail "Project acquisition is incomplete: scripts/km-vms-storage-discovery.sh is missing."

write_env
write_metadata
sh "$APP_DIR/scripts/km-vms-storage-discovery.sh" --app-dir "$APP_DIR" >/dev/null

(
  cd "$APP_DIR"
  compose_cmd --env-file "$APP_DIR/.env" config >/dev/null
)

(
  cd "$APP_DIR"
  compose_cmd --env-file "$APP_DIR/.env" up -d --build
)

info "KM VMS setup mode is starting."
info "Local setup URL: http://localhost:$HTTP_PORT/setup"
lan_hint
info "Next step: open the setup URL and create the first owner account."
info "Safe restart after setup: sh $APP_DIR/scripts/km-vms-restart.sh --app-dir $APP_DIR"
