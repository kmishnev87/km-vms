#!/usr/bin/env sh
set -eu

GITHUB_REPO="${KM_VMS_GITHUB_REPO:-kmishnev87/km-vms}"
BRANCH="${KM_VMS_BRANCH:-main}"
GITHUB_PRIVATE="${KM_VMS_GITHUB_PRIVATE:-0}"
GITHUB_TOKEN_FILE="${KM_VMS_GITHUB_TOKEN_FILE:-}"
GITHUB_TOKEN_ENV_NAME="${KM_VMS_GITHUB_TOKEN_ENV:-}"
DOCKER_COMPOSE_BIN="${KM_VMS_DOCKER_COMPOSE:-}"
PROJECT_NAME="${KM_VMS_PROJECT_NAME:-}"
YES="${KM_VMS_YES:-0}"
DRY_RUN=0

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR=""
TMP_ROOT=""
LOCK_DIR=""
LOCK_HELD=0
GITHUB_TOKEN=""
GITHUB_TOKEN_CONFIG=""
GITHUB_TOKEN_SOURCE="none"
DOWNLOAD_CLIENT=""
SOURCE_COMMIT_SHA=""
PHASE="init"
OVERLAY_STARTED=0
UPDATED_PATHS=""
PRESERVED_PATHS=".env .env.* data data/postgres data/redis data/previews data/exports data/install-control selected-storage"
PREFLIGHT_ENV_CKSUM=""
POSTFLIGHT_ENV_CKSUM=""
PREFLIGHT_DATA_PATHS=""

usage() {
  cat <<'EOF'
KM VMS terminal update

Usage:
  sh scripts/update.sh --github-repo <owner/name> --branch <branch-or-ref> [options]

Options:
  --github-repo <repo>     GitHub repository as owner/name for tarball acquisition.
  --branch <branch>        Git branch/tag/ref. Default: main.
  --ref <ref>              Alias for --branch.
  --github-private         Require a GitHub token for source acquisition.
  --github-token-file      Read GitHub token from a local file.
  --github-token-env       Read GitHub token from the named environment variable.
  --yes                    Non-interactive confirmation.
  --dry-run                Validate inputs, acquire source and print a plan without modifying app files or containers.
  --help                   Show this help.

Environment equivalents:
  KM_VMS_GITHUB_REPO, KM_VMS_BRANCH, KM_VMS_GITHUB_PRIVATE=1,
  KM_VMS_GITHUB_TOKEN, KM_VMS_GITHUB_TOKEN_FILE, KM_VMS_GITHUB_TOKEN_ENV,
  KM_VMS_DOCKER_COMPOSE, KM_VMS_PROJECT_NAME, KM_VMS_YES=1.
EOF
}

info() {
  printf '%s\n' "$*"
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

metadata_time() {
  date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date
}

write_update_metadata() {
  status="$1"
  error_message="${2:-}"
  metadata="$APP_DIR/.km-vms-update.json"
  finished_at=$(metadata_time)
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "status": "%s",\n' "$(json_escape "$status")"
    printf '  "finished_at": "%s",\n' "$(json_escape "$finished_at")"
    if [ "$status" = "success" ]; then
      printf '  "failed_phase": null,\n'
    else
      printf '  "failed_phase": "%s",\n' "$(json_escape "$PHASE")"
    fi
    printf '  "source_kind": "github-tarball",\n'
    printf '  "github_repo": "%s",\n' "$(json_escape "$GITHUB_REPO")"
    printf '  "ref": "%s",\n' "$(json_escape "$BRANCH")"
    if [ -n "$SOURCE_COMMIT_SHA" ]; then
      printf '  "commit_sha": "%s",\n' "$(json_escape "$SOURCE_COMMIT_SHA")"
    else
      printf '  "commit_sha": null,\n'
    fi
    printf '  "compose_kind": "%s",\n' "$(json_escape "${COMPOSE_KIND:-unknown}")"
    printf '  "compose_source": "%s",\n' "$(json_escape "${COMPOSE_SOURCE:-unknown}")"
    printf '  "updated_paths_summary": "%s",\n' "$(json_escape "$UPDATED_PATHS")"
    printf '  "preserved_paths_summary": "%s",\n' "$(json_escape "$PRESERVED_PATHS")"
    printf '  "validation_summary": {\n'
    printf '    "app_dir": "checked",\n'
    printf '    "source_tree": "checked",\n'
    printf '    "compose_config": "%s",\n' "$(if [ "$status" = "success" ]; then printf checked; else printf unknown; fi)"
    printf '    "postflight_preservation": "%s"\n' "$(if [ "$status" = "success" ]; then printf checked; else printf unknown; fi)"
    printf '  },\n'
    if [ -n "$error_message" ]; then
      printf '  "error_message": "%s",\n' "$(json_escape "$error_message")"
    else
      printf '  "error_message": null,\n'
    fi
    printf '  "rollback": {\n'
    printf '    "implemented": false,\n'
    printf '    "before_overlay": "no app source changes are made before the overlay phase",\n'
    printf '    "after_overlay": "app source may be partially updated if failure occurs after copying files",\n'
    printf '    "operator_guidance": "rerun the same update after fixing the failed phase or restore from an external backup if the app does not recover"\n'
    printf '  }\n'
    printf '}\n'
  } > "$metadata"
}

fail() {
  message="$*"
  printf 'ERROR [%s]: %s\n' "$PHASE" "$message" >&2
  if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ] && [ -n "$APP_DIR" ] && [ -f "$APP_DIR/docker-compose.yml" ]; then
    case "$PHASE" in
      rebuild_recreate|health_check)
        (
          cd "$APP_DIR"
          compose_cmd --env-file "$APP_DIR/.env" up -d postgres redis api recorder web nginx >/dev/null 2>&1
        ) || true
        ;;
    esac
  fi
  if [ "$DRY_RUN" != "1" ] && [ -n "$APP_DIR" ] && [ -d "$APP_DIR" ] && [ -f "$APP_DIR/docker-compose.yml" ]; then
    write_update_metadata "failed" "$message" 2>/dev/null || true
  fi
  exit 1
}

cleanup() {
  clear_github_token 2>/dev/null || true
  if [ "$LOCK_HELD" = "1" ] && [ -n "$LOCK_DIR" ] && [ -d "$LOCK_DIR" ]; then
    rm -f "$LOCK_DIR/pid" "$LOCK_DIR/started_at" 2>/dev/null || true
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  if [ -n "$TMP_ROOT" ] && [ -d "$TMP_ROOT" ]; then
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT HUP INT TERM

confirm() {
  prompt="$1"
  if [ "$YES" = "1" ]; then
    return 0
  fi
  printf '%s [y/N] ' "$prompt"
  IFS= read -r answer || true
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) fail "Cancelled." ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
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

resolve_app_dir() {
  cwd=$(pwd -P)
  if [ -f "$cwd/docker-compose.yml" ] && [ -d "$cwd/apps/api" ] && [ -f "$cwd/scripts/update.sh" ]; then
    APP_DIR="$cwd"
    return
  fi
  parent=$(dirname "$SCRIPT_DIR")
  if [ -f "$parent/docker-compose.yml" ] && [ -d "$parent/apps/api" ]; then
    APP_DIR="$(cd "$parent" && pwd -P)"
    return
  fi
  fail "Run update.sh from a KM VMS app directory or from its scripts directory."
}

is_dangerous_app_dir() {
  case "$1" in
    /|/bin|/boot|/dev|/etc|/lib|/lib64|/proc|/run|/sbin|/sys|/usr|/var|/root|/tmp) return 0 ;;
    *) return 1 ;;
  esac
}

validate_app_dir() {
  PHASE="validate_app_dir"
  resolve_app_dir
  is_dangerous_app_dir "$APP_DIR" && fail "Refusing dangerous app dir: $APP_DIR"
  [ -f "$APP_DIR/docker-compose.yml" ] || fail "Missing docker-compose.yml in app dir."
  [ -d "$APP_DIR/apps/api" ] || fail "Missing apps/api in app dir."
  [ -d "$APP_DIR/apps/web" ] || fail "Missing apps/web in app dir."
  [ -f "$APP_DIR/deploy/nginx/default.conf" ] || fail "Missing deploy/nginx/default.conf in app dir."
  [ -f "$APP_DIR/scripts/km-vms-compose-common.sh" ] || fail "Missing compose helper in app dir."
  [ -f "$APP_DIR/.env" ] || fail "Missing .env; update.sh only updates installed instances."
}

load_compose_common() {
  PHASE="compose_detection"
  # shellcheck disable=SC1090
  . "$APP_DIR/scripts/km-vms-compose-common.sh"
  km_vms_detect_compose "$DOCKER_COMPOSE_BIN" || fail "Docker Compose was not found. Checked KM_VMS_DOCKER_COMPOSE, PATH docker compose/docker-compose, and known NAS vendor paths."
}

compose_cmd() {
  km_vms_compose_cmd "$@"
}

safe_project_name() {
  value="$1"
  [ -n "$value" ] || return 0
  case "$value" in
    *[A-Z]*) fail "Project name must be lowercase." ;;
  esac
  printf '%s' "$value" | grep -Eq '^[a-z][a-z0-9_-]*$' || fail "Project name must start with a lowercase letter and contain only lowercase letters, digits, dashes or underscores."
}

read_env_value() {
  key="$1"
  [ -f "$APP_DIR/.env" ] || return 0
  sed -n "s/^$key=//p" "$APP_DIR/.env" | tail -n 1
}

acquire_lock() {
  PHASE="validate_app_dir"
  lock_root="$APP_DIR/data/update-control"
  mkdir -p "$lock_root"
  LOCK_DIR="$lock_root/update.lock"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    LOCK_HELD=1
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    metadata_time > "$LOCK_DIR/started_at"
    return
  fi
  fail "Another update appears to be running. Lock exists at data/update-control/update.lock. Remove it only after verifying no update process is active."
}

read_hidden() {
  prompt="$1"
  if ! [ -t 0 ]; then
    fail "$prompt requires an interactive terminal or a secure token env/file source."
  fi
  if command_exists stty; then
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
    [ -n "$GITHUB_TOKEN" ] || fail "GitHub token is required for private repository update."
    GITHUB_TOKEN_SOURCE="interactive"
  fi
}

prepare_github_token_config() {
  [ -n "$GITHUB_TOKEN" ] || return 0
  command_exists mktemp || fail "mktemp is required for secure GitHub token handling."
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
  if command_exists curl; then
    DOWNLOAD_CLIENT="curl"
    return 0
  fi
  if command_exists wget; then
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
  [ -z "$GITHUB_TOKEN_CONFIG" ] || fail "Private GitHub update requires curl for secure token handling."
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
  command_exists tar || fail "tar is required for GitHub tarball acquisition."
  listing="$TMP_ROOT/tar-list.txt"
  extract_dir="$TMP_ROOT/extract"
  mkdir -p "$extract_dir"
  tar -tzf "$archive" > "$listing" || fail "Cannot inspect GitHub tarball."
  top=""
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    case "$entry" in
      /*|../*|*/../*) fail "Refusing unsafe tarball entry path." ;;
    esac
    entry_top=${entry%%/*}
    if [ -z "$top" ]; then
      top="$entry_top"
    elif [ "$entry_top" != "$top" ]; then
      fail "GitHub tarball has multiple top-level roots."
    fi
  done < "$listing"
  [ -n "$top" ] || fail "GitHub tarball is empty."
  tar -xzf "$archive" -C "$extract_dir" || fail "Cannot extract GitHub tarball."
  [ -d "$extract_dir/$top" ] || fail "GitHub tarball root is missing after extraction."
  (cd "$extract_dir/$top" && tar -cf - .) | (cd "$destination" && tar -xf -) || fail "Cannot stage source from GitHub tarball."
}

acquire_source() {
  PHASE="acquire"
  [ -n "$GITHUB_REPO" ] || fail "GitHub repo is required. Pass --github-repo owner/name or KM_VMS_GITHUB_REPO."
  GITHUB_REPO=$(validate_github_repo "$GITHUB_REPO")
  validate_ref "$BRANCH"
  command_exists mktemp || fail "mktemp is required."
  TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/km-vms-update.XXXXXX")
  mkdir -p "$TMP_ROOT/source"
  load_github_token
  prepare_github_token_config
  archive="$TMP_ROOT/source.tar.gz"
  tarball_url="https://api.github.com/repos/$GITHUB_REPO/tarball/$BRANCH"
  if ! http_download "$tarball_url" "$archive"; then
    fail "GitHub tarball acquisition failed. If the repository is private, rerun with --github-private and a secure token source."
  fi
  resolve_github_commit_sha
  PHASE="extract"
  safe_extract_tarball "$archive" "$TMP_ROOT/source"
  clear_github_token
}

validate_source_tree() {
  PHASE="validate_source_tree"
  source="$TMP_ROOT/source"
  [ -f "$source/docker-compose.yml" ] || fail "Source tree is missing docker-compose.yml."
  [ -d "$source/apps/api" ] || fail "Source tree is missing apps/api."
  [ -d "$source/apps/web" ] || fail "Source tree is missing apps/web."
  [ -f "$source/deploy/nginx/default.conf" ] || fail "Source tree is missing deploy/nginx/default.conf."
  [ -f "$source/scripts/install.sh" ] || fail "Source tree is missing scripts/install.sh."
  [ -f "$source/scripts/km-vms-compose-common.sh" ] || fail "Source tree is missing scripts/km-vms-compose-common.sh."
  [ -f "$source/docs/INSTALL.md" ] || fail "Source tree is missing docs/INSTALL.md."
  [ -f "$source/release/km-vms-release.json" ] || fail "Source tree is missing release/km-vms-release.json."
  if [ -f "$source/scripts/update.sh" ]; then
    info "Source tree includes scripts/update.sh."
  else
    info "Source tree does not include scripts/update.sh; continuing because update.sh may be absent before Stage 6.0.7 is merged into the selected ref."
  fi
  unsafe_link=$(find "$source" -type l -print | head -n 1)
  [ -z "$unsafe_link" ] || fail "Source tree contains symlinks; refusing update because portable safe symlink handling is not guaranteed."
}

preflight_preservation() {
  PHASE="preflight_preservation"
  PREFLIGHT_ENV_CKSUM=$(cksum "$APP_DIR/.env" 2>/dev/null | awk '{print $1 ":" $2}' || printf unavailable)
  PREFLIGHT_DATA_PATHS=""
  for path in data data/postgres data/redis data/previews data/exports data/install-control; do
    if [ -e "$APP_DIR/$path" ]; then
      if [ -z "$PREFLIGHT_DATA_PATHS" ]; then
        PREFLIGHT_DATA_PATHS="$path"
      else
        PREFLIGHT_DATA_PATHS="$PREFLIGHT_DATA_PATHS $path"
      fi
    fi
  done
}

postflight_preservation() {
  PHASE="postflight_preservation"
  POSTFLIGHT_ENV_CKSUM=$(cksum "$APP_DIR/.env" 2>/dev/null | awk '{print $1 ":" $2}' || printf unavailable)
  [ "$PREFLIGHT_ENV_CKSUM" = "$POSTFLIGHT_ENV_CKSUM" ] || fail ".env checksum changed during update."
  for path in $PREFLIGHT_DATA_PATHS; do
    [ -e "$APP_DIR/$path" ] || fail "Preserved data path disappeared during update: $path"
  done
}

tar_excludes() {
  printf '%s\n' \
    "--exclude=./.git" "--exclude=*/.git" \
    "--exclude=./.env" "--exclude=*/.env" \
    "--exclude=./.env.*" "--exclude=*/.env.*" \
    "--exclude=./data" "--exclude=*/data" \
    "--exclude=./logs" "--exclude=*/logs" \
    "--exclude=./log" "--exclude=*/log" \
    "--exclude=./node_modules" "--exclude=*/node_modules" \
    "--exclude=./.next" "--exclude=*/.next" \
    "--exclude=./__pycache__" "--exclude=*/__pycache__" \
    "--exclude=./.pytest_cache" "--exclude=*/.pytest_cache" \
    "--exclude=./coverage" "--exclude=*/coverage" \
    "--exclude=./dist" "--exclude=*/dist" \
    "--exclude=./build" "--exclude=*/build" \
    "--exclude=./service-artifacts" "--exclude=*/service-artifacts" \
    "--exclude=./service_artifacts" "--exclude=*/service_artifacts" \
    "--exclude=*.zip" "--exclude=*.tar" "--exclude=*.tar.gz" "--exclude=*.tgz" "--exclude=*.rar" "--exclude=*.7z" \
    "--exclude=./.ssh" "--exclude=*/.ssh" \
    "--exclude=id_rsa" "--exclude=*/id_rsa" \
    "--exclude=id_ed25519" "--exclude=*/id_ed25519" \
    "--exclude=*.pem" "--exclude=*.key" "--exclude=*.p12" "--exclude=*.pfx" \
    "--exclude=*.crt" "--exclude=*.csr" \
    "--exclude=*credential*" "--exclude=*Credential*" "--exclude=*CREDENTIAL*" \
    "--exclude=*.token" "--exclude=*.secret" \
    "--exclude=*token.txt" "--exclude=*Token.txt" "--exclude=*TOKEN.txt" \
    "--exclude=*secret.txt" "--exclude=*Secret.txt" "--exclude=*SECRET.txt" \
    "--exclude=*secret.json" "--exclude=*Secret.json" "--exclude=*SECRET.json" \
    "--exclude=github-token*" "--exclude=auth-token*" "--exclude=authorization-token*" \
    "--exclude=*credentials*" "--exclude=*Credentials*" "--exclude=*CREDENTIALS*" \
    "--exclude=*password*" "--exclude=*Password*" "--exclude=*PASSWORD*" \
    "--exclude=*.dump" "--exclude=*.sql" "--exclude=*.sqlite" "--exclude=*.db"
}

copy_allowed_path() {
  relative="$1"
  source="$TMP_ROOT/source"
  [ -e "$source/$relative" ] || return 0
  case "$relative" in
    apps|deploy|docs|release|scripts)
      preserve_update_script=0
      if [ "$relative" = "scripts" ] && [ ! -f "$source/scripts/update.sh" ] && [ -f "$APP_DIR/scripts/update.sh" ]; then
        cp "$APP_DIR/scripts/update.sh" "$TMP_ROOT/update.sh.preserved" || fail "Failed to preserve current update.sh."
        preserve_update_script=1
      fi
      rm -rf "$APP_DIR/$relative"
      mkdir -p "$APP_DIR/$relative"
      (
        cd "$source/$relative"
        # shellcheck disable=SC2046
        tar $(tar_excludes) -cf - .
      ) | (cd "$APP_DIR/$relative" && tar -xf -) || fail "Failed to overlay $relative."
      if [ "$preserve_update_script" = "1" ]; then
        cp "$TMP_ROOT/update.sh.preserved" "$APP_DIR/scripts/update.sh" || fail "Failed to restore preserved update.sh."
      fi
      ;;
    docker-compose.yml|docker-compose.pytest.yml|.dockerignore|.gitignore|.env.example)
      cp "$source/$relative" "$APP_DIR/$relative" || fail "Failed to overlay $relative."
      ;;
    *)
      fail "Internal error: path is not in update allowlist: $relative"
      ;;
  esac
  if [ -z "$UPDATED_PATHS" ]; then
    UPDATED_PATHS="$relative"
  else
    UPDATED_PATHS="$UPDATED_PATHS $relative"
  fi
}

overlay_source() {
  PHASE="overlay"
  OVERLAY_STARTED=1
  for relative in apps deploy docs release scripts docker-compose.yml docker-compose.pytest.yml .dockerignore .gitignore .env.example; do
    copy_allowed_path "$relative"
  done
}

compose_config() {
  PHASE="compose_config"
  (
    cd "$APP_DIR"
    compose_cmd --env-file "$APP_DIR/.env" config >/dev/null
  ) || fail "Compose config validation failed."
}

rebuild_recreate() {
  PHASE="rebuild_recreate"
  (
    cd "$APP_DIR"
    if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ]; then
      compose_cmd --env-file "$APP_DIR/.env" up -d --build postgres redis api recorder web nginx || {
        sleep 5
        compose_cmd --env-file "$APP_DIR/.env" up -d --build postgres redis api recorder web nginx
      }
    else
      compose_cmd --env-file "$APP_DIR/.env" up -d --build
    fi
  ) || fail "Compose rebuild/recreate failed."
}

health_check() {
  PHASE="health_check"
  http_port=$(read_env_value HTTP_PORT)
  [ -n "$http_port" ] || http_port="8088"
  if command_exists curl; then
    health_targets="http://127.0.0.1:$http_port/api/health"
    health_attempts=12
    if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ]; then
      health_targets="http://nginx/api/health http://api:8000/health"
      health_attempts=60
    fi
    health_i=1
    while [ "$health_i" -le "$health_attempts" ]; do
      for health_target in $health_targets; do
        if curl -fsS "$health_target" >/dev/null 2>&1; then
          return 0
        fi
      done
      sleep 5
      health_i=$((health_i + 1))
    done
    fail "Health check did not return healthy at /api/health."
  fi
  info "curl is unavailable; skipped HTTP health check."
}

write_source_provenance() {
  PHASE="metadata_write"
  provenance="$APP_DIR/.km-vms-source.json"
  recorded_at=$(metadata_time)
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "recorded_at": "%s",\n' "$(json_escape "$recorded_at")"
    printf '  "source_kind": "github-tarball",\n'
    printf '  "github_repo": "%s",\n' "$(json_escape "$GITHUB_REPO")"
    printf '  "ref": "%s",\n' "$(json_escape "$BRANCH")"
    if [ -n "$SOURCE_COMMIT_SHA" ]; then
      printf '  "commit_sha": "%s"\n' "$(json_escape "$SOURCE_COMMIT_SHA")"
    else
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
  metadata_status="${1:-}"
  identity="$APP_DIR/.km-vms-release.json"
  tmp_identity="$identity.tmp.$$"
  installed_at=$(metadata_time)
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
  [ -n "$metadata_status" ] || metadata_status="$(if [ -n "$SOURCE_COMMIT_SHA" ]; then printf complete; else printf partial; fi)"
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
    printf '  "installed_by": "%s",\n' "$(if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ]; then printf in_app_helper; else printf terminal_update; fi)"
    printf '  "metadata_status": "%s",\n' "$(json_escape "$metadata_status")"
    printf '  "metadata_source": "%s"\n' "$(if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ]; then printf helper; else printf official_update; fi)"
    printf '}\n'
  } > "$tmp_identity"
  mv "$tmp_identity" "$identity"
}

print_plan() {
  info "KM VMS update plan"
  info "App dir: $APP_DIR"
  info "Source mode: GitHub tarball acquisition"
  info "GitHub repo: $GITHUB_REPO"
  info "Git ref: $BRANCH"
  if [ "$GITHUB_PRIVATE" = "1" ] || [ -n "${KM_VMS_GITHUB_TOKEN:-}" ] || [ -n "$GITHUB_TOKEN_FILE" ] || [ -n "$GITHUB_TOKEN_ENV_NAME" ]; then
    info "GitHub token mode: enabled via secure input path"
  else
    info "GitHub token mode: public/no token"
  fi
  info "Compose: ${COMPOSE_KIND:-unknown} via ${COMPOSE_BIN:-unknown}"
  info "Would update: apps deploy docs release scripts docker-compose.yml docker-compose.pytest.yml .dockerignore .gitignore .env.example"
  info "Would preserve: $PRESERVED_PATHS"
  info "Rollback: not implemented in Stage 6.0.7; failures after overlay may leave source partially updated."
}

PHASE="init"
validate_app_dir
PROJECT_NAME="${PROJECT_NAME:-$(read_env_value COMPOSE_PROJECT_NAME)}"
safe_project_name "$PROJECT_NAME"
if [ "$DRY_RUN" != "1" ]; then
  acquire_lock
fi
load_compose_common
acquire_source
validate_source_tree
preflight_preservation
print_plan

if [ "$DRY_RUN" = "1" ]; then
  PHASE="cleanup"
  info "Dry-run complete. No app source, .env, data, containers, or update metadata were modified."
  exit 0
fi

confirm "Apply KM VMS update now?"
overlay_source
write_release_identity "precompose"
compose_config
rebuild_recreate
health_check
write_source_provenance
write_release_identity
postflight_preservation
PHASE="metadata_write"
write_update_metadata "success" ""
PHASE="cleanup"
info "KM VMS update completed."
info "Updated paths: $UPDATED_PATHS"
info "Preserved paths: $PRESERVED_PATHS"
info "Update metadata: $APP_DIR/.km-vms-update.json"
