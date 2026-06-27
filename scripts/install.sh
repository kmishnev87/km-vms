#!/usr/bin/env sh
set -eu

GITHUB_REPO_DEFAULT="kmishnev87/km-vms"
BRANCH_DEFAULT="main"
HTTP_PORT_DEFAULT="8088"
API_PORT_DEFAULT="18000"
PROJECT_NAME_DEFAULT="km-vms"
TZ_DEFAULT="Asia/Yekaterinburg"

APP_DIR="${KM_VMS_APP_DIR:-}"
REPO_URL="${KM_VMS_REPO_URL:-}"
GITHUB_REPO="${KM_VMS_GITHUB_REPO:-$GITHUB_REPO_DEFAULT}"
BRANCH="${KM_VMS_BRANCH:-$BRANCH_DEFAULT}"
HTTP_PORT="${KM_VMS_HTTP_PORT:-$HTTP_PORT_DEFAULT}"
API_PORT="${KM_VMS_API_PORT:-}"
PROJECT_NAME="${KM_VMS_PROJECT_NAME:-$PROJECT_NAME_DEFAULT}"
SOURCE_DIR="${KM_VMS_SOURCE_DIR:-}"
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-}"
GITHUB_TOKEN_FILE="${KM_VMS_GITHUB_TOKEN_FILE:-}"
GITHUB_TOKEN_ENV_NAME="${KM_VMS_GITHUB_TOKEN_ENV:-}"
GITHUB_PRIVATE="${KM_VMS_GITHUB_PRIVATE:-0}"
YES="${KM_VMS_YES:-0}"
DRY_RUN=0
APP_DIR_CREATED=0
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_URL_EXPLICIT=0
SOURCE_MODE="github-tarball"
SOURCE_COMMIT_SHA=""
SOURCE_PROVENANCE_KIND="github-tarball"
GITHUB_TOKEN=""
GITHUB_TOKEN_CONFIG=""
GITHUB_TOKEN_SOURCE="none"

usage() {
  cat <<'EOF'
KM VMS installer

Usage:
  sh scripts/install.sh --app-dir <path> [options]

Options:
  --app-dir <path>       Installation directory.
  --github-repo <repo>   GitHub repository as owner/name for tarball acquisition.
  --repo-url <url>       Generic Git URL fallback for git-based acquisition.
  --branch <branch>      Git branch/tag/ref. Default: main.
  --ref <ref>            Alias for --branch.
  --github-private       Require a GitHub token for source acquisition.
  --github-token-file    Read GitHub token from a local file.
  --github-token-env     Read GitHub token from the named environment variable.
  --http-port <port>     Host HTTP port for nginx. Default: 8088.
  --project-name <name>  Compose project/container prefix. Default: km-vms.
  --source-dir <path>    Development/testing mode: copy local product source.
  --yes                  Non-interactive confirmation.
  --dry-run              Validate inputs and print plan without writing.
  --help                 Show this help.

Environment equivalents:
  KM_VMS_APP_DIR, KM_VMS_GITHUB_REPO, KM_VMS_REPO_URL, KM_VMS_BRANCH,
  KM_VMS_GITHUB_PRIVATE=1, KM_VMS_GITHUB_TOKEN, KM_VMS_GITHUB_TOKEN_FILE,
  KM_VMS_GITHUB_TOKEN_ENV, KM_VMS_HTTP_PORT, KM_VMS_API_PORT,
  KM_VMS_PROJECT_NAME, KM_VMS_SOURCE_DIR, KM_VMS_DOCKER_COMPOSE, KM_VMS_YES=1.
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
      REPO_URL_EXPLICIT=1
      shift 2
      ;;
    --github-repo)
      [ "$#" -ge 2 ] || fail "--github-repo requires a value"
      GITHUB_REPO="$2"
      shift 2
      ;;
    --branch)
      [ "$#" -ge 2 ] || fail "--branch requires a value"
      BRANCH="$2"
      shift 2
      ;;
    --ref)
      [ "$#" -ge 2 ] || fail "--ref requires a value"
      BRANCH="$2"
      shift 2
      ;;
    --github-private)
      GITHUB_PRIVATE=1
      shift
      ;;
    --github-token-file)
      [ "$#" -ge 2 ] || fail "--github-token-file requires a value"
      GITHUB_TOKEN_FILE="$2"
      shift 2
      ;;
    --github-token-env)
      [ "$#" -ge 2 ] || fail "--github-token-env requires a value"
      GITHUB_TOKEN_ENV_NAME="$2"
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

km_vms_command_exists() {
  command -v "$1" >/dev/null 2>&1
}

load_compose_common() {
  helper=""
  if [ -f "$SCRIPT_DIR/km-vms-compose-common.sh" ]; then
    helper="$SCRIPT_DIR/km-vms-compose-common.sh"
  elif [ -n "$APP_DIR" ] && [ -f "$APP_DIR/scripts/km-vms-compose-common.sh" ]; then
    helper="$APP_DIR/scripts/km-vms-compose-common.sh"
  fi
  [ -n "$helper" ] || fail "km-vms-compose-common.sh was not found. Local installs must run from the unpacked repo; GitHub installs load it after source acquisition."
  # shellcheck disable=SC1090
  . "$helper"
}

validate_github_repo() {
  value="$(printf '%s' "$1" | tr -d '[:space:]')"
  printf '%s' "$value" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' || fail "GitHub repo must be in owner/name format."
  printf '%s\n' "$value"
}

validate_ref() {
  value="$1"
  [ -n "$value" ] || fail "Git ref must not be empty."
  case "$value" in
    *[\;\|\&\`\>\<\(\)]*|*'$('*|*'$'*|*"	"*) fail "Git ref contains unsafe characters." ;;
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

read_hidden() {
  prompt="$1"
  if ! [ -t 0 ]; then
    fail "$prompt requires an interactive terminal or a secure token env/file source."
  fi
  if km_vms_command_exists stty; then
    old_state=$(stty -g 2>/dev/null || true)
    printf '%s' "$prompt" >&2
    stty -echo 2>/dev/null || true
    IFS= read -r value || true
    stty "$old_state" 2>/dev/null || true
    printf '\n' >&2
  else
    printf '%s' "$prompt" >&2
    IFS= read -r value || true
  fi
  printf '%s' "$value"
}

load_github_token() {
  GITHUB_TOKEN=""
  GITHUB_TOKEN_SOURCE="none"
  if [ -n "${KM_VMS_GITHUB_TOKEN:-}" ]; then
    GITHUB_TOKEN="${KM_VMS_GITHUB_TOKEN}"
    GITHUB_TOKEN_SOURCE="env:KM_VMS_GITHUB_TOKEN"
  elif [ -n "$GITHUB_TOKEN_ENV_NAME" ]; then
    GITHUB_TOKEN=$(printenv "$GITHUB_TOKEN_ENV_NAME" 2>/dev/null || true)
    [ -n "$GITHUB_TOKEN" ] || fail "Environment variable for GitHub token is empty: $GITHUB_TOKEN_ENV_NAME"
    GITHUB_TOKEN_SOURCE="env:$GITHUB_TOKEN_ENV_NAME"
  elif [ -n "$GITHUB_TOKEN_FILE" ]; then
    [ -f "$GITHUB_TOKEN_FILE" ] || fail "GitHub token file does not exist: $GITHUB_TOKEN_FILE"
    GITHUB_TOKEN=$(tr -d '\r\n' < "$GITHUB_TOKEN_FILE")
    [ -n "$GITHUB_TOKEN" ] || fail "GitHub token file is empty: $GITHUB_TOKEN_FILE"
    GITHUB_TOKEN_SOURCE="file"
  elif [ "$GITHUB_PRIVATE" = "1" ]; then
    GITHUB_TOKEN=$(read_hidden 'GitHub token (repo read-only contents) > ')
    [ -n "$GITHUB_TOKEN" ] || fail "GitHub token is required for private repository install."
    GITHUB_TOKEN_SOURCE="interactive"
  fi
}

prepare_github_token_config() {
  [ -n "$GITHUB_TOKEN" ] || return 0
  km_vms_command_exists mktemp || fail "mktemp is required for secure GitHub token handling."
  GITHUB_TOKEN_CONFIG=$(mktemp "${TMPDIR:-/tmp}/km-vms-github-token.XXXXXX")
  chmod 600 "$GITHUB_TOKEN_CONFIG" 2>/dev/null || true
  {
    printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN"
    printf 'header = "Accept: application/vnd.github+json"\n'
    printf 'header = "X-GitHub-Api-Version: 2022-11-28"\n'
  } > "$GITHUB_TOKEN_CONFIG"
}

clear_github_token() {
  GITHUB_TOKEN=""
  unset GITHUB_TOKEN
  if [ -n "$GITHUB_TOKEN_CONFIG" ] && [ -f "$GITHUB_TOKEN_CONFIG" ]; then
    rm -f "$GITHUB_TOKEN_CONFIG"
  fi
  GITHUB_TOKEN_CONFIG=""
}

require_download_client() {
  if km_vms_command_exists curl; then
    DOWNLOAD_CLIENT="curl"
    return 0
  fi
  if km_vms_command_exists wget; then
    DOWNLOAD_CLIENT="wget"
    return 0
  fi
  fail "curl or wget is required for GitHub tarball acquisition."
}

http_download() {
  url="$1"
  output="$2"
  require_download_client
  if [ "$DOWNLOAD_CLIENT" = "curl" ]; then
    if [ -n "$GITHUB_TOKEN_CONFIG" ]; then
      curl -fsSL --config "$GITHUB_TOKEN_CONFIG" "$url" -o "$output" || return 1
    else
      curl -fsSL "$url" -o "$output" || return 1
    fi
    return 0
  fi
  [ -z "$GITHUB_TOKEN_CONFIG" ] || fail "Private GitHub install requires curl for secure token handling."
  wget -qO "$output" "$url" || return 1
}

github_api_text() {
  url="$1"
  require_download_client
  if [ "$DOWNLOAD_CLIENT" = "curl" ]; then
    if [ -n "$GITHUB_TOKEN_CONFIG" ]; then
      curl -fsSL --config "$GITHUB_TOKEN_CONFIG" "$url"
    else
      curl -fsSL "$url"
    fi
    return $?
  fi
  [ -z "$GITHUB_TOKEN_CONFIG" ] || fail "Private GitHub API calls require curl for secure token handling."
  wget -qO- "$url"
}

resolve_github_commit_sha() {
  payload=$(github_api_text "https://api.github.com/repos/$GITHUB_REPO/commits/$BRANCH" 2>/dev/null || true)
  SOURCE_COMMIT_SHA=$(printf '%s\n' "$payload" | sed -n 's/^[[:space:]]*"sha"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]\{40\}\)".*/\1/p' | head -n 1)
}

safe_extract_tarball() {
  archive="$1"
  destination="$2"
  km_vms_command_exists tar || fail "tar is required for GitHub tarball acquisition."
  km_vms_command_exists mktemp || fail "mktemp is required for tarball extraction."
  listing=$(mktemp "${TMPDIR:-/tmp}/km-vms-tar-list.XXXXXX")
  extract_dir=$(mktemp -d "${TMPDIR:-/tmp}/km-vms-extract.XXXXXX")
  tar -tzf "$archive" > "$listing" || acquisition_fail "Cannot inspect GitHub tarball."
  top=""
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    case "$entry" in
      /*|../*|*/../*) acquisition_fail "Refusing unsafe tarball entry path." ;;
    esac
    entry_top=${entry%%/*}
    if [ -z "$top" ]; then
      top="$entry_top"
    elif [ "$entry_top" != "$top" ]; then
      acquisition_fail "GitHub tarball has multiple top-level roots."
    fi
  done < "$listing"
  [ -n "$top" ] || acquisition_fail "GitHub tarball is empty."
  tar -xzf "$archive" -C "$extract_dir" || acquisition_fail "Cannot extract GitHub tarball."
  [ -d "$extract_dir/$top" ] || acquisition_fail "GitHub tarball root is missing after extraction."
  (cd "$extract_dir/$top" && tar -cf - .) | (cd "$destination" && tar -xf -) || acquisition_fail "Cannot populate app dir from GitHub tarball."
  rm -f "$listing"
  rm -rf "$extract_dir"
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
  if km_vms_command_exists ss; then
    ss -ltn 2>/dev/null | grep -q "[.:]$port "
    return $?
  fi
  if km_vms_command_exists netstat; then
    netstat -ltn 2>/dev/null | grep -q "[.:]$port "
    return $?
  fi
  return 1
}

detect_compose() {
  km_vms_detect_compose "$DOCKER_COMPOSE_BIN" || fail "Docker Compose was not found. Checked KM_VMS_DOCKER_COMPOSE, PATH docker compose/docker-compose, and known NAS vendor paths."
}

compose_cmd() {
  km_vms_compose_cmd "$@"
}

check_docker() {
  detect_compose
  if km_vms_command_exists docker; then
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
  if km_vms_command_exists openssl; then
    openssl rand -hex 32
    return
  fi
  if [ -r /dev/urandom ] && km_vms_command_exists od; then
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
  backup_dir="$APP_DIR/data/backups/db"
  mkdir -p "$archive_dir" "$backup_dir" "$APP_DIR/data/previews" "$APP_DIR/data/exports" "$APP_DIR/data/install-control"
  chmod 700 "$backup_dir" 2>/dev/null || true
  chmod 755 "$APP_DIR/data/previews" 2>/dev/null || true
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
    printf 'KMVMS_HOST_DB_BACKUP_ROOT=%s\n' "$backup_dir"
    printf 'KMVMS_DB_BACKUP_ROOT=/storage/backups/db\n'
    printf 'STORAGE_INSTALL_CONTROL=%s\n' "$APP_DIR/data/install-control"
    printf 'HTTP_PORT=%s\n' "$HTTP_PORT"
    printf 'API_PORT=%s\n' "$API_PORT"
    printf 'COMPOSE_PROJECT_NAME=%s\n' "$PROJECT_NAME"
    printf 'KM_VMS_CONTAINER_PREFIX=%s\n' "$PROJECT_NAME"
    printf 'KM_VMS_HOST_APP_DIR=%s\n' "$APP_DIR"
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
  source_summary="$SOURCE_MODE"
  {
    printf '{\n'
    printf '  "app_dir": "%s",\n' "$APP_DIR"
    printf '  "project_name": "%s",\n' "$PROJECT_NAME"
    printf '  "http_port": "%s",\n' "$HTTP_PORT"
    printf '  "compose_command": "%s",\n' "$COMPOSE_KIND"
    printf '  "compose_bin": "%s",\n' "$COMPOSE_BIN"
    printf '  "created_at": "%s",\n' "$created_at"
    printf '  "source_mode": "%s",\n' "$source_summary"
    printf '  "setup_url": "http://localhost:%s/setup"\n' "$HTTP_PORT"
    printf '}\n'
  } > "$metadata"
}

write_source_provenance() {
  provenance="$APP_DIR/.km-vms-source.json"
  created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "recorded_at": "%s",\n' "$created_at"
    printf '  "source_kind": "%s",\n' "$SOURCE_PROVENANCE_KIND"
    if [ "$SOURCE_PROVENANCE_KIND" = "github-tarball" ]; then
      printf '  "github_repo": "%s",\n' "$GITHUB_REPO"
      printf '  "ref": "%s",\n' "$BRANCH"
      if [ -n "$SOURCE_COMMIT_SHA" ]; then
        printf '  "commit_sha": "%s"\n' "$SOURCE_COMMIT_SHA"
      else
        printf '  "commit_sha": null\n'
      fi
    elif [ "$SOURCE_PROVENANCE_KIND" = "git-clone" ]; then
      printf '  "repo_url": "%s",\n' "$REPO_URL"
      printf '  "ref": "%s",\n' "$BRANCH"
      printf '  "commit_sha": null\n'
    else
      printf '  "source_dir": "%s",\n' "$SOURCE_DIR"
      printf '  "commit_sha": null\n'
    fi
    printf '}\n'
  } > "$provenance"
}

release_descriptor_value() {
  key="$1"
  file="$APP_DIR/release/km-vms-release.json"
  [ -f "$file" ] || return 0
  sed -n "s/^[[:space:]]*\"$key\"[[:space:]]*:[[:space:]]*\"\(.*\)\"[[:space:]]*,\{0,1\}[[:space:]]*$/\1/p" "$file" | head -n 1
}

write_release_identity() {
  identity="$APP_DIR/.km-vms-release.json"
  tmp_identity="$identity.tmp.$$"
  installed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
  version=$(release_descriptor_value version)
  title=$(release_descriptor_value title)
  summary=$(release_descriptor_value summary)
  channel=$(release_descriptor_value release_channel)
  source_kind=$(release_descriptor_value source_kind)
  source_repo=$(release_descriptor_value source_repo)
  source_ref=$(release_descriptor_value source_ref)
  [ -n "$version" ] || version="0.7.2"
  [ -n "$title" ] || title="Public GitHub Release Identity and Drift-Proof Update Status"
  [ -n "$summary" ] || summary="Public GitHub install/update identity and update status hardening."
  [ -n "$channel" ] || channel="public-github"
  [ -n "$source_kind" ] || source_kind="github-release"
  [ -n "$source_repo" ] || source_repo="$GITHUB_REPO"
  [ -n "$source_ref" ] || source_ref="$BRANCH"
  [ ! -d "$identity" ] || fail "Release identity path is a directory and cannot be mounted by Docker Compose: $identity"
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "product": "KM VMS",\n'
    printf '  "version": "%s",\n' "$(json_escape "$version")"
    printf '  "title": "%s",\n' "$(json_escape "$title")"
    printf '  "summary": "%s",\n' "$(json_escape "$summary")"
    printf '  "release_channel": "%s",\n' "$(json_escape "$channel")"
    printf '  "source_kind": "%s",\n' "$(json_escape "$source_kind")"
    printf '  "source_repo": "%s",\n' "$(json_escape "$source_repo")"
    printf '  "source_ref": "%s",\n' "$(json_escape "$source_ref")"
    if [ -n "$SOURCE_COMMIT_SHA" ]; then
      printf '  "commit_sha": "%s",\n' "$(json_escape "$SOURCE_COMMIT_SHA")"
    else
      printf '  "commit_sha": null,\n'
    fi
    printf '  "installed_at": "%s",\n' "$(json_escape "$installed_at")"
    printf '  "installed_by": "install",\n'
    printf '  "metadata_status": "%s",\n' "$(if [ -n "$SOURCE_COMMIT_SHA" ]; then printf complete; else printf partial; fi)"
    printf '  "metadata_source": "official_install"\n'
    printf '}\n'
  } > "$tmp_identity"
  mv "$tmp_identity" "$identity"
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
  km_vms_command_exists tar || fail "tar is required for --source-dir copy mode"
}

copy_source_dir() {
  SOURCE_MODE="source-dir"
  SOURCE_PROVENANCE_KIND="source-dir"
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
  km_vms_command_exists git || fail "git is required for repository acquisition."
  SOURCE_MODE="git-clone"
  SOURCE_PROVENANCE_KIND="git-clone"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR" || acquisition_fail "Repository acquisition failed."
}

acquire_github_tarball() {
  [ -n "$GITHUB_REPO" ] || fail "GitHub repo is required for tarball acquisition."
  GITHUB_REPO=$(validate_github_repo "$GITHUB_REPO")
  load_github_token
  prepare_github_token_config
  SOURCE_MODE="github-tarball"
  SOURCE_PROVENANCE_KIND="github-tarball"
  km_vms_command_exists mktemp || fail "mktemp is required for GitHub tarball acquisition."
  archive=$(mktemp "${TMPDIR:-/tmp}/km-vms-source.XXXXXX.tar.gz")
  tarball_url="https://api.github.com/repos/$GITHUB_REPO/tarball/$BRANCH"
  if ! http_download "$tarball_url" "$archive"; then
    clear_github_token
    acquisition_fail "GitHub tarball acquisition failed. If the repository is private, rerun with --github-private and a secure token source."
  fi
  resolve_github_commit_sha
  safe_extract_tarball "$archive" "$APP_DIR"
  rm -f "$archive"
  clear_github_token
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
  if km_vms_command_exists hostname; then
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
  info "Compose: $COMPOSE_KIND via $COMPOSE_BIN"
  version=$(km_vms_compose_version || true)
  [ -n "$version" ] && info "Compose version: $version"
  info "App dir: $APP_DIR"
  info "HTTP port: $HTTP_PORT"
  info "API port: $API_PORT"
  info "Project name: $PROJECT_NAME"
  if [ -n "$SOURCE_DIR" ]; then
    info "Source mode: --source-dir development/testing copy"
  elif [ -n "$REPO_URL" ]; then
    info "Source mode: git clone from configured repo URL"
    info "Repo URL: $REPO_URL"
  else
    info "Source mode: GitHub tarball acquisition"
    info "GitHub repo: $GITHUB_REPO"
    info "Git ref: $BRANCH"
    if [ "$GITHUB_PRIVATE" = "1" ] || [ -n "${KM_VMS_GITHUB_TOKEN:-}" ] || [ -n "$GITHUB_TOKEN_FILE" ] || [ -n "$GITHUB_TOKEN_ENV_NAME" ]; then
      info "GitHub token mode: enabled via secure input path"
    else
      info "GitHub token mode: public/no token"
    fi
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
[ -z "$GITHUB_REPO" ] || GITHUB_REPO=$(validate_github_repo "$GITHUB_REPO")
validate_ref "$BRANCH"
APP_DIR=$(normalize_path "$APP_DIR")
[ -n "$SOURCE_DIR" ] && SOURCE_DIR=$(normalize_path "$SOURCE_DIR")
is_dangerous_app_dir "$APP_DIR" && fail "Refusing dangerous app dir: $APP_DIR"
validate_port "$HTTP_PORT"
derive_api_port
validate_port "$API_PORT"
validate_source_dir

if port_busy "$HTTP_PORT"; then
  fail "HTTP port is already listening: $HTTP_PORT"
fi
if port_busy "$API_PORT"; then
  fail "API port is already listening: $API_PORT"
fi

if [ "$DRY_RUN" = "1" ]; then
  load_compose_common
  check_docker
  print_plan
  info "Dry-run complete. No files were written and compose was not started."
  exit 0
fi

prepare_app_dir
probe_write

if [ ! -f "$APP_DIR/docker-compose.yml" ]; then
  if [ -n "$SOURCE_DIR" ]; then
    copy_source_dir
  elif [ -n "$REPO_URL" ]; then
    clone_repo
  else
    acquire_github_tarball
  fi
fi

[ -f "$APP_DIR/docker-compose.yml" ] || fail "Project acquisition did not produce docker-compose.yml."
[ -f "$APP_DIR/deploy/nginx/default.conf" ] || fail "Project acquisition is incomplete: deploy/nginx/default.conf is missing."
[ -f "$APP_DIR/scripts/km-vms-storage-discovery.sh" ] || fail "Project acquisition is incomplete: scripts/km-vms-storage-discovery.sh is missing."

load_compose_common
check_docker
print_plan

write_env
write_metadata
write_source_provenance
write_release_identity
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
