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
UPDATE_PROGRESS_FILE="${KM_VMS_UPDATE_PROGRESS_FILE:-}"
RELEASE_IDENTITY_HOST_STATUS=""
RELEASE_IDENTITY_API_STATUS=""
RELEASE_IDENTITY_API_VISIBLE=0
RELEASE_IDENTITY_COMMIT_VERIFIED=0
UPDATE_BOOTSTRAP_IMAGE=""
UPDATE_BOOTSTRAP_STAGE_DIR=""
UPDATE_BOOTSTRAP_GATE_PATH=""
UPDATE_HELPER_IMAGE_PREPARED=0
UPDATE_HELPER_REFRESH_SCHEDULED=0
SCHEMA_CANDIDATE_IMAGE=""
SCHEMA_CANDIDATE_OVERRIDE=""
SCHEMA_MIGRATION_REQUIRED=0
SCHEMA_WRITERS_STOPPED=0
SCHEMA_MUTATION_STARTED=0
PRODUCT_SOURCE_DIR=""
SLOT_AWARE_ACTIVATION=1
SLOT_ACTIVATION_RESULT=""
PREVIOUS_SLOT_ID=""
TARGET_SLOT_ID=""

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

progress_step_name() {
  case "$1" in
    init|validate_app_dir|compose_detection|preflight_preservation|permission_preflight|schema_preflight) printf preflight ;;
    acquire) printf acquire_source ;;
    extract) printf extracting ;;
    validate_source_tree) printf validating_source ;;
    overlay) printf overlay ;;
    compose_config) printf compose_config ;;
    helper_bootstrap|rebuild_recreate|schema_update) printf rebuilding ;;
    health_check) printf health_check ;;
    metadata_write|postflight_preservation) printf commit_verification ;;
    cleanup) printf completed ;;
    *) printf '%s' "$1" ;;
  esac
}

write_helper_progress() {
  [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ] || return 0
  [ -n "$UPDATE_PROGRESS_FILE" ] || return 0
  status="${1:-running}"
  message="${2:-}"
  now=$(metadata_time)
  step=$(progress_step_name "$PHASE")
  tmp_progress="$UPDATE_PROGRESS_FILE.tmp.$$"
  mkdir -p "$(dirname "$UPDATE_PROGRESS_FILE")" 2>/dev/null || return 0
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "status": "%s",\n' "$(json_escape "$status")"
    printf '  "phase": "%s",\n' "$(json_escape "$PHASE")"
    printf '  "current_step": "%s",\n' "$(json_escape "$step")"
    printf '  "updated_at": "%s",\n' "$(json_escape "$now")"
    printf '  "request_id": "%s",\n' "$(json_escape "${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-}")"
    printf '  "message": "%s"\n' "$(json_escape "$message")"
    printf '}\n'
  } > "$tmp_progress" 2>/dev/null || return 0
  mv "$tmp_progress" "$UPDATE_PROGRESS_FILE" 2>/dev/null || true
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
    printf '    "postflight_preservation": "%s",\n' "$(if [ "$status" = "success" ]; then printf checked; else printf unknown; fi)"
    printf '    "release_identity_host_metadata_status": "%s",\n' "$(json_escape "$RELEASE_IDENTITY_HOST_STATUS")"
    printf '    "release_identity_api_metadata_status": "%s",\n' "$(json_escape "$RELEASE_IDENTITY_API_STATUS")"
    printf '    "release_identity_api_visible": %s,\n' "$(if [ "$RELEASE_IDENTITY_API_VISIBLE" = "1" ]; then printf true; else printf false; fi)"
    printf '    "release_identity_commit_verified": %s\n' "$(if [ "$RELEASE_IDENTITY_COMMIT_VERIFIED" = "1" ]; then printf true; else printf false; fi)"
    printf '  },\n'
    if [ -n "$error_message" ]; then
      printf '  "error_message": "%s",\n' "$(json_escape "$error_message")"
    else
      printf '  "error_message": null,\n'
    fi
    printf '  "rollback": {\n'
    printf '    "implemented": true,\n'
    printf '    "before_activation": "the active source remains unchanged while the trusted target is prepared",\n'
    printf '    "after_activation": "failed target health or identity restores the exact captured previous release",\n'
    printf '    "operator_guidance": "review the bounded activation result before retrying"\n'
    printf '  }\n'
    printf '}\n'
  } > "$metadata"
}

fail() {
  message="$*"
  refresh_schema_mutation_truth 2>/dev/null || true
  printf 'ERROR [%s]: %s\n' "$PHASE" "$message" >&2
  write_helper_progress "failed" "$message" 2>/dev/null || true
  if [ "$SCHEMA_MUTATION_STARTED" = "1" ] && [ -n "$APP_DIR" ] && [ -f "$APP_DIR/docker-compose.yml" ]; then
    (
      cd "$APP_DIR"
      compose_with_archive_roots stop api recorder >/dev/null 2>&1
    ) || true
  fi
  if [ "$SCHEMA_WRITERS_STOPPED" = "1" ] && [ "$SCHEMA_MUTATION_STARTED" != "1" ]; then
    (
      cd "$APP_DIR"
      compose_with_archive_roots up -d api recorder >/dev/null 2>&1
    ) || true
    SCHEMA_WRITERS_STOPPED=0
  fi
  if [ "$SCHEMA_MUTATION_STARTED" != "1" ] && [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ] && [ -n "$APP_DIR" ] && [ -f "$APP_DIR/docker-compose.yml" ]; then
    case "$PHASE" in
      rebuild_recreate|schema_update|health_check)
        (
          cd "$APP_DIR"
          compose_with_archive_roots up -d postgres redis api recorder web nginx >/dev/null 2>&1
        ) || true
        ;;
    esac
  fi
  if [ "$DRY_RUN" != "1" ] && [ -n "$APP_DIR" ] && [ -d "$APP_DIR" ] && [ -f "$APP_DIR/docker-compose.yml" ]; then
    write_update_metadata "failed" "$message" 2>/dev/null || true
  fi
  exit 1
}

compose_with_archive_roots() {
  source_dir="${PRODUCT_SOURCE_DIR:-$APP_DIR}"
  km_vms_compose_for_source "$APP_DIR" "$source_dir" "$@"
}

archive_roots_compose_file() {
  printf '%s\n' "$APP_DIR/data/install-control/docker-compose.archive-roots.yml"
}

archive_roots_compose_present() {
  [ -f "$(archive_roots_compose_file)" ]
}

apply_generated_archive_roots_compose_if_needed() {
  was_present="$1"
  if [ "$was_present" = "1" ]; then
    return 1
  fi
  archive_roots_compose_present || return 1
  compose_with_archive_roots config >/dev/null
  compose_with_archive_roots up -d --force-recreate api
  return 0
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
  if [ -n "$UPDATE_BOOTSTRAP_STAGE_DIR" ] && [ -d "$UPDATE_BOOTSTRAP_STAGE_DIR" ]; then
    rm -rf "$UPDATE_BOOTSTRAP_STAGE_DIR" 2>/dev/null || true
  fi
  if [ -n "$UPDATE_BOOTSTRAP_IMAGE" ] && command_exists docker; then
    docker image rm -f "$UPDATE_BOOTSTRAP_IMAGE" >/dev/null 2>&1 || true
  fi
  if [ -n "$SCHEMA_CANDIDATE_IMAGE" ] && command_exists docker; then
    docker image rm -f "$SCHEMA_CANDIDATE_IMAGE" >/dev/null 2>&1 || true
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
  write_helper_progress "running" "Validating installed app directory."
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
  write_helper_progress "running" "Detecting Docker Compose."
  # shellcheck disable=SC1090
  . "$APP_DIR/scripts/km-vms-compose-common.sh"
  km_vms_detect_compose "$DOCKER_COMPOSE_BIN" || fail "Docker Compose was not found. Checked KM_VMS_DOCKER_COMPOSE, PATH docker compose/docker-compose, and known NAS vendor paths."
  case "$COMPOSE_BIN" in
    */*)
      compose_bin_dir=$(dirname "$COMPOSE_BIN")
      if [ -x "$compose_bin_dir/docker" ]; then
        PATH="$compose_bin_dir:$PATH"
        export PATH
      fi
      ;;
  esac
  if [ "$DRY_RUN" != "1" ]; then
    command_exists docker ||
      fail "Docker CLI was not found next to Docker Compose or in PATH."
  fi
  KM_VMS_DOCKER_COMPOSE="$COMPOSE_BIN"
  KM_VMS_DOCKER_COMPOSE_KIND="$COMPOSE_KIND"
  export KM_VMS_DOCKER_COMPOSE KM_VMS_DOCKER_COMPOSE_KIND
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
  write_helper_progress "running" "Acquiring trusted source archive."
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
  write_helper_progress "running" "Extracting trusted source archive."
  safe_extract_tarball "$archive" "$TMP_ROOT/source"
  clear_github_token
}

validate_source_tree() {
  PHASE="validate_source_tree"
  write_helper_progress "running" "Validating trusted source tree."
  source="$TMP_ROOT/source"
  [ -f "$source/docker-compose.yml" ] || fail "Source tree is missing docker-compose.yml."
  [ -d "$source/apps/api" ] || fail "Source tree is missing apps/api."
  [ -d "$source/apps/web" ] || fail "Source tree is missing apps/web."
  [ -f "$source/deploy/nginx/default.conf" ] || fail "Source tree is missing deploy/nginx/default.conf."
  [ -f "$source/scripts/install.sh" ] || fail "Source tree is missing scripts/install.sh."
  [ -f "$source/scripts/km-vms-compose-common.sh" ] || fail "Source tree is missing scripts/km-vms-compose-common.sh."
  [ -f "$source/scripts/km-vms-permission-gate.sh" ] || fail "Source tree is missing scripts/km-vms-permission-gate.sh."
  [ -f "$source/scripts/km-vms-update-helper-bridge.py" ] || fail "Source tree is missing scripts/km-vms-update-helper-bridge.py."
  [ -f "$source/scripts/km-vms-release-slots.py" ] || fail "Source tree is missing scripts/km-vms-release-slots.py."
  [ -f "$source/docs/INSTALL.md" ] || fail "Source tree is missing docs/INSTALL.md."
  [ -f "$source/release/km-vms-release.json" ] || fail "Source tree is missing release/km-vms-release.json."
  [ -f "$source/release/km-vms-update-lineage.json" ] || fail "Source tree is missing release/km-vms-update-lineage.json."
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
  write_helper_progress "running" "Checking preservation contract."
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
  write_helper_progress "running" "Verifying preservation contract."
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
    docker-compose.yml|docker-compose.pytest.yml|.dockerignore|.gitignore|.gitattributes|.env.example)
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
  write_helper_progress "running" "Applying product source overlay."
  OVERLAY_STARTED=1
  for relative in apps deploy docs release scripts docker-compose.yml docker-compose.pytest.yml .dockerignore .gitignore .gitattributes .env.example; do
    copy_allowed_path "$relative"
  done
}

helper_host_app_dir() {
  host_app_dir="${KM_VMS_UPDATE_HOST_APP_DIR:-}"
  [ -n "$host_app_dir" ] || fail "KM_VMS_UPDATE_HOST_APP_DIR is required for helper bootstrap."
  case "$host_app_dir" in
    /*) ;;
    *) fail "KM_VMS_UPDATE_HOST_APP_DIR must be an absolute host path." ;;
  esac
  [ -d "$host_app_dir" ] || fail "Host app directory is not mounted inside update-helper: $host_app_dir"
  [ -f "$host_app_dir/docker-compose.yml" ] || fail "Host app directory is missing docker-compose.yml."
  printf '%s\n' "$host_app_dir"
}

prepare_permission_gate_runtime() {
  [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ] || return 0
  [ -n "$UPDATE_BOOTSTRAP_IMAGE" ] && return 0
  PHASE="permission_preflight"
  write_helper_progress "running" "Preparing target permission inspection runtime."
  command_exists docker || fail "Docker CLI is required for helper permission bootstrap."
  docker version >/dev/null 2>&1 || fail "Docker daemon is unavailable for helper permission bootstrap."
  host_app_dir=$(helper_host_app_dir)
  bootstrap_root="$host_app_dir/data/update-control"
  mkdir -p "$bootstrap_root" || fail "Cannot create permission bootstrap root."
  UPDATE_BOOTSTRAP_STAGE_DIR=$(mktemp -d "$bootstrap_root/.permission-bootstrap.XXXXXX") ||
    fail "Cannot create permission bootstrap staging directory."
  cp "$TMP_ROOT/source/scripts/km-vms-permission-gate.sh" "$UPDATE_BOOTSTRAP_STAGE_DIR/km-vms-permission-gate.sh" ||
    fail "Cannot stage trusted target permission gate."
  chmod 0755 "$UPDATE_BOOTSTRAP_STAGE_DIR/km-vms-permission-gate.sh" ||
    fail "Cannot set trusted permission gate mode."
  UPDATE_BOOTSTRAP_GATE_PATH="$UPDATE_BOOTSTRAP_STAGE_DIR/km-vms-permission-gate.sh"
  bootstrap_suffix=$(printf '%s' "${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-run-$$}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_.-')
  [ -n "$bootstrap_suffix" ] || bootstrap_suffix="run-$$"
  UPDATE_BOOTSTRAP_IMAGE="km-vms-update-bootstrap:$bootstrap_suffix"
  docker build -t "$UPDATE_BOOTSTRAP_IMAGE" "$TMP_ROOT/source/apps/update-helper" >/dev/null ||
    fail "Cannot build target permission inspection runtime."
}

run_trusted_permission_gate() {
  contract="$1"
  action="$2"
  trusted_gate="$TMP_ROOT/source/scripts/km-vms-permission-gate.sh"
  [ -f "$trusted_gate" ] || fail "Trusted source permission gate is missing."
  if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ]; then
    prepare_permission_gate_runtime
    host_app_dir=$(helper_host_app_dir)
    if [ "$contract" = "existing" ]; then
      docker run --rm \
        -v "$host_app_dir:$host_app_dir" \
        -w "$host_app_dir" \
        "$UPDATE_BOOTSTRAP_IMAGE" \
        sh "$UPDATE_BOOTSTRAP_GATE_PATH" --preflight-existing "$action" --app-dir "$host_app_dir"
    else
      docker run --rm \
        -v "$host_app_dir:$host_app_dir" \
        -w "$host_app_dir" \
        "$UPDATE_BOOTSTRAP_IMAGE" \
        sh "$UPDATE_BOOTSTRAP_GATE_PATH" "$action" --app-dir "$host_app_dir"
    fi
    return $?
  fi
  if [ "$contract" = "existing" ]; then
    sh "$trusted_gate" --preflight-existing "$action" --app-dir "$APP_DIR"
  else
    sh "$trusted_gate" "$action" --app-dir "$APP_DIR"
  fi
}

preflight_permission_policy() {
  PHASE="permission_preflight"
  write_helper_progress "running" "Validating the existing product tree before target overlay."
  run_trusted_permission_gate existing --fix ||
    fail "Pre-overlay existing-tree permission validation failed."
}

apply_permission_policy() {
  PHASE="overlay"
  write_helper_progress "running" "Hardening and validating the complete target product tree."
  run_trusted_permission_gate target --fix ||
    fail "Post-overlay target-tree permission hardening failed."
}

preflight_target_permission_policy() {
  PHASE="permission_preflight"
  write_helper_progress "running" "Validating the staged target critical permission chain."
  sh "$TMP_ROOT/source/scripts/km-vms-permission-gate.sh" \
    --check --app-dir "$TMP_ROOT/source" ||
    fail "Staged target permission validation failed."
}

prepare_schema_handoff() {
  PHASE="schema_preflight"
  request_id="${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-}"
  printf '%s' "$request_id" |
    grep -Eq '^(update|stage609)-[0-9a-fA-F]{32}$' ||
    fail "A canonical request id is required for schema handoff."
  handoff_output="$TMP_ROOT/slot-handoff.out"
  if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ]; then
    python3 "$TMP_ROOT/source/scripts/km-vms-update-helper-bridge.py" \
      handoff \
      --app-dir "$APP_DIR" \
      --target-source-dir "$TMP_ROOT/source" \
      --request-id "$request_id" \
      --project-name "$PROJECT_NAME" >"$handoff_output" ||
      fail "Cannot prepare the pre-overlay schema handoff."
  else
    version=$(staged_release_descriptor_value version)
    [ -n "$version" ] || fail "Trusted target version is unavailable."
    python3 "$TMP_ROOT/source/scripts/km-vms-update-helper-bridge.py" \
      handoff \
      --app-dir "$APP_DIR" \
      --target-source-dir "$TMP_ROOT/source" \
      --request-id "$request_id" \
      --project-name "$PROJECT_NAME" \
      --terminal \
      --trusted-commit "$SOURCE_COMMIT_SHA" \
      --declared-version "$version" >"$handoff_output" ||
      fail "Cannot prepare the terminal release-slot handoff."
  fi
  PREVIOUS_SLOT_ID=$(
    sed -n 's/^previous_slot=//p' "$handoff_output" | tail -n 1
  )
  printf '%s' "$PREVIOUS_SLOT_ID" |
    grep -Eq '^(release-[0-9a-f]{40}|adopted-[0-9a-f]{64})$' ||
    fail "Schema handoff returned no exact previous release slot."
}

prepare_trusted_target_slot() {
  PHASE="schema_preflight"
  write_helper_progress "staging" "Preparing the trusted target release outside the active source."
  request_id="${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-}"
  version=$(staged_release_descriptor_value version)
  [ -n "$version" ] || fail "Trusted target version is unavailable."
  target_output="$TMP_ROOT/slot-target.out"
  python3 "$TMP_ROOT/source/scripts/km-vms-update-helper-bridge.py" \
    prepare-target \
    --app-dir "$APP_DIR" \
    --target-source-dir "$TMP_ROOT/source" \
    --request-id "$request_id" \
    --trusted-commit "$SOURCE_COMMIT_SHA" \
    --declared-version "$version" \
    --project-name "$PROJECT_NAME" >"$target_output" ||
    fail "Trusted target release could not be prepared before activation."
  TARGET_SLOT_ID=$(
    sed -n 's/^target_slot=//p' "$target_output" | tail -n 1
  )
  [ "$TARGET_SLOT_ID" = "release-$SOURCE_COMMIT_SHA" ] ||
    fail "Prepared target slot does not match the trusted commit."
}

activate_trusted_target_slot() {
  PHASE="activation"
  write_helper_progress "activating" "Activating the prepared target release."
  request_id="${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-}"
  version=$(staged_release_descriptor_value version)
  activation_output="$TMP_ROOT/slot-activation.out"
  if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ]; then
    python3 "$TMP_ROOT/source/scripts/km-vms-update-helper-bridge.py" \
      activate-target \
      --app-dir "$APP_DIR" \
      --project-name "$PROJECT_NAME" \
      --request-id "$request_id" \
      --previous-slot "$PREVIOUS_SLOT_ID" \
      --target-slot "$TARGET_SLOT_ID" \
      --target-commit "$SOURCE_COMMIT_SHA" \
      --target-version "$version" >"$activation_output" ||
      fail "Release-slot activation could not converge safely."
  else
    python3 "$TMP_ROOT/source/scripts/km-vms-update-helper-bridge.py" \
      activate-target \
      --app-dir "$APP_DIR" \
      --project-name "$PROJECT_NAME" \
      --request-id "$request_id" \
      --previous-slot "$PREVIOUS_SLOT_ID" \
      --target-slot "$TARGET_SLOT_ID" \
      --target-commit "$SOURCE_COMMIT_SHA" \
      --target-version "$version" \
      --terminal >"$activation_output" ||
      fail "Terminal release-slot activation could not converge safely."
  fi
  SLOT_ACTIVATION_RESULT=$(
    python3 -c \
      'import json,sys; print(json.loads(sys.stdin.read().splitlines()[-1])["activation"])' \
      <"$activation_output" 2>/dev/null || true
  )
  case "$SLOT_ACTIVATION_RESULT" in
    completed)
      SLOT_AWARE_ACTIVATION=1
      ;;
    failed_rolled_back)
      SLOT_AWARE_ACTIVATION=1
      fail "Target activation failed and the exact previous release was restored."
      ;;
    blocked)
      SLOT_AWARE_ACTIVATION=1
      fail "Target activation stopped because safe convergence could not be proven."
      ;;
    *)
      fail "Release-slot activation returned invalid terminal evidence."
      ;;
  esac
}

ensure_activation_request_id() {
  request_id="${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-}"
  if printf '%s' "$request_id" |
    grep -Eq '^(update|stage609)-[0-9a-fA-F]{32}$'; then
    return 0
  fi
  [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" != "1" ] ||
    fail "The update helper did not provide a canonical request id."
  request_id=$(
    python3 -c 'import uuid; print("update-" + uuid.uuid4().hex)'
  ) || fail "Cannot create a terminal update request id."
  printf '%s' "$request_id" |
    grep -Eq '^update-[0-9a-f]{32}$' ||
    fail "Cannot create a canonical terminal update request id."
  KM_VMS_UPDATE_CONTROL_REQUEST_ID="$request_id"
  export KM_VMS_UPDATE_CONTROL_REQUEST_ID
}

staged_compose_with_archive_roots() {
  if [ -n "$PROJECT_NAME" ]; then
    km_vms_compose_for_source "$APP_DIR" "$TMP_ROOT/source" \
      -p "$PROJECT_NAME" "$@"
  else
    km_vms_compose_for_source "$APP_DIR" "$TMP_ROOT/source" "$@"
  fi
}

staged_compose_config() {
  PHASE="compose_config"
  write_helper_progress "running" "Validating staged target Docker Compose config."
  staged_compose_with_archive_roots config >/dev/null ||
    fail "Staged target Compose config validation failed."
}

staged_release_descriptor_value() {
  key="$1"
  file="$TMP_ROOT/source/release/km-vms-release.json"
  sed -n "s/^[[:space:]]*\"$key\"[[:space:]]*:[[:space:]]*\"\(.*\)\"[[:space:]]*,\{0,1\}[[:space:]]*$/\1/p" "$file" |
    head -n 1
}

write_schema_candidate_identity() {
  candidate_dir="$TMP_ROOT/source/apps/api/.km-vms-candidate"
  mkdir -p "$candidate_dir" ||
    fail "Cannot create the schema candidate metadata directory."
  cp "$TMP_ROOT/source/release/km-vms-update-lineage.json" \
    "$candidate_dir/update-lineage.json" ||
    fail "Cannot stage target update lineage for schema preflight."
  version=$(staged_release_descriptor_value version)
  title=$(staged_release_descriptor_value title)
  summary=$(staged_release_descriptor_value summary)
  channel=$(staged_release_descriptor_value release_channel)
  source_kind=$(staged_release_descriptor_value source_kind)
  source_repo=$(staged_release_descriptor_value source_repo)
  source_ref=$(staged_release_descriptor_value source_ref)
  [ -n "$version" ] && [ -n "$title" ] && [ -n "$summary" ] &&
    [ -n "$channel" ] && [ -n "$source_kind" ] &&
    [ -n "$source_repo" ] && [ -n "$source_ref" ] ||
    fail "Target release descriptor is incomplete."
  [ -n "$SOURCE_COMMIT_SHA" ] ||
    fail "Trusted target commit is missing for schema preflight."
  identity="$candidate_dir/release-identity.json"
  installed_at=$(metadata_time)
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
    printf '  "commit_sha": "%s",\n' "$(json_escape "$SOURCE_COMMIT_SHA")"
    printf '  "installed_at": "%s",\n' "$(json_escape "$installed_at")"
    printf '  "installed_by": "in_app_helper",\n'
    printf '  "metadata_status": "precompose",\n'
    printf '  "metadata_source": "helper"\n'
    printf '}\n'
  } > "$identity" ||
    fail "Cannot write target schema candidate identity."
}

validated_backup_paths() {
  host_app_dir=$(helper_host_app_dir)
  host_backup=$(read_env_value KMVMS_HOST_DB_BACKUP_ROOT)
  container_backup=$(read_env_value KMVMS_DB_BACKUP_ROOT)
  [ -n "$container_backup" ] ||
    container_backup="/storage/backups/db"
  case "$host_backup" in
    "") host_backup="$host_app_dir/data/backups/db" ;;
    /*) ;;
    *)
      case "/$host_backup/" in
        */../*) fail "Configured DB backup root contains parent traversal." ;;
      esac
      host_backup="$host_app_dir/$host_backup"
      ;;
  esac
  case "$host_backup:$container_backup" in
    *\"*|*\\*) fail "Configured DB backup roots contain unsupported characters." ;;
  esac
  case "$container_backup" in
    /*) ;;
    *) fail "Container DB backup root must be absolute." ;;
  esac
  case "/$container_backup/" in
    */../*) fail "Container DB backup root contains parent traversal." ;;
  esac
  printf '%s\n%s\n' "$host_backup" "$container_backup"
}

prepare_schema_candidate_image() {
  [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ] || return 0
  PHASE="schema_preflight"
  write_helper_progress "running" "Building a bounded target schema preflight image."
  command_exists docker ||
    fail "Docker CLI is required for target schema preflight."
  write_schema_candidate_identity
  candidate_suffix=$(printf '%s' "${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-run-$$}" |
    tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_.-')
  [ -n "$candidate_suffix" ] || candidate_suffix="run-$$"
  SCHEMA_CANDIDATE_IMAGE="km-vms-schema-candidate:$candidate_suffix"
  docker build -t "$SCHEMA_CANDIDATE_IMAGE" \
    "$TMP_ROOT/source/apps/api" >/dev/null ||
    fail "Target schema preflight image build failed."

  backup_paths=$(validated_backup_paths)
  host_backup=$(printf '%s\n' "$backup_paths" | sed -n '1p')
  container_backup=$(printf '%s\n' "$backup_paths" | sed -n '2p')
  host_app_dir=$(helper_host_app_dir)
  SCHEMA_CANDIDATE_OVERRIDE="$TMP_ROOT/schema-candidate.yml"
  {
    printf 'services:\n'
    printf '  schema-update:\n'
    printf '    image: "%s"\n' "$SCHEMA_CANDIDATE_IMAGE"
    printf '    environment:\n'
    printf '      KMVMS_RELEASE_IDENTITY_FILE: /app/.km-vms-candidate/release-identity.json\n'
    printf '      KMVMS_UPDATE_LINEAGE_FILE: /app/.km-vms-candidate/update-lineage.json\n'
    printf '      KMVMS_DB_BACKUP_ROOT: "%s"\n' "$container_backup"
    printf '    volumes:\n'
    printf '      - type: bind\n'
    printf '        source: "%s/data/update-control"\n' "$host_app_dir"
    printf '        target: /update-control\n'
    printf '        bind:\n'
    printf '          create_host_path: false\n'
    printf '      - type: bind\n'
    printf '        source: "%s/data/update-control"\n' "$host_app_dir"
    printf '        target: /app/release\n'
    printf '        read_only: true\n'
    printf '        bind:\n'
    printf '          create_host_path: false\n'
    printf '      - type: bind\n'
    printf '        source: "%s/data/update-control/schema-update-request.json"\n' "$host_app_dir"
    printf '        target: /app/.km-vms-release.json\n'
    printf '        read_only: true\n'
    printf '        bind:\n'
    printf '          create_host_path: false\n'
    printf '      - type: bind\n'
    printf '        source: "%s/data/update-control/schema-update-request.json"\n' "$host_app_dir"
    printf '        target: /app/.km-vms-source.json\n'
    printf '        read_only: true\n'
    printf '        bind:\n'
    printf '          create_host_path: false\n'
    printf '      - type: bind\n'
    printf '        source: "%s"\n' "$host_backup"
    printf '        target: "%s"\n' "$container_backup"
    printf '        bind:\n'
    printf '          create_host_path: true\n'
  } > "$SCHEMA_CANDIDATE_OVERRIDE" ||
    fail "Cannot create target schema candidate Compose override."
  schema_candidate_compose config >/dev/null ||
    fail "Target schema candidate Compose config validation failed."
}

schema_candidate_compose() {
  if [ -n "$PROJECT_NAME" ]; then
    km_vms_compose_for_source "$APP_DIR" "$TMP_ROOT/source" \
      -p "$PROJECT_NAME" \
      -f "$SCHEMA_CANDIDATE_OVERRIDE" "$@"
  else
    km_vms_compose_for_source "$APP_DIR" "$TMP_ROOT/source" \
      -f "$SCHEMA_CANDIDATE_OVERRIDE" "$@"
  fi
}

run_schema_preflight() {
  [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ] || return 0
  PHASE="schema_preflight"
  output="$TMP_ROOT/schema-preflight.out"
  if ! schema_candidate_compose run --rm --no-deps schema-update \
    python3 -m app.services.schema_update_pipeline --preflight \
    >"$output" 2>&1; then
    fail "Target schema/history/backup preflight failed before source activation."
  fi
  required=$(sed -n 's/^schema_migration_required=//p' "$output" |
    tail -n 1)
  case "$required" in
    true) SCHEMA_MIGRATION_REQUIRED=1 ;;
    false) SCHEMA_MIGRATION_REQUIRED=0 ;;
    *) fail "Target schema preflight returned no exact migration decision." ;;
  esac
}

refresh_schema_mutation_truth() {
  [ "$SCHEMA_MUTATION_STARTED" = "1" ] && return 0
  marker="$APP_DIR/data/update-control/schema-mutation-state.json"
  [ -f "$marker" ] && [ ! -L "$marker" ] || return 0
  request_id="${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-}"
  grep -Fq "\"request_id\": \"$request_id\"" "$marker" || return 0
  if grep -Fq '"mutation_started": true' "$marker"; then
    SCHEMA_MUTATION_STARTED=1
  fi
}

stop_schema_writers() {
  [ "$SCHEMA_MIGRATION_REQUIRED" = "1" ] || return 0
  PHASE="schema_update"
  write_helper_progress "running" "Pausing database writers immediately before backup and migration."
  (
    cd "$APP_DIR"
    compose_with_archive_roots stop api recorder
  ) || fail "Cannot pause API and recorder before schema migration."
  SCHEMA_WRITERS_STOPPED=1
}

run_schema_migration() {
  [ "$SCHEMA_MIGRATION_REQUIRED" = "1" ] || return 0
  PHASE="schema_update"
  output="$TMP_ROOT/schema-migration.out"
  if ! schema_candidate_compose run --rm --no-deps schema-update \
    python3 -m app.services.schema_update_pipeline --migrate \
    >"$output" 2>&1; then
    refresh_schema_mutation_truth
    fail "Target database backup/migration did not complete."
  fi
  refresh_schema_mutation_truth
  [ "$SCHEMA_MUTATION_STARTED" = "1" ] ||
    fail "Schema migration completed without durable mutation truth."
}

prepare_update_helper_image() {
  [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ] || return 0
  PHASE="helper_bootstrap"
  write_helper_progress "running" "Building the target update-helper image without replacing the active helper."
  (
    cd "$APP_DIR"
    compose_with_archive_roots build update-helper
  ) || fail "Target update-helper image build failed."
  UPDATE_HELPER_IMAGE_PREPARED=1
}

schedule_update_helper_recreate() {
  [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ] || return 0
  [ "$UPDATE_HELPER_IMAGE_PREPARED" = "1" ] || fail "Target update-helper image was not prepared."
  request_id="${KM_VMS_UPDATE_CONTROL_REQUEST_ID:-}"
  printf '%s' "$request_id" | grep -Eq '^update-[0-9a-fA-F]{32}$' ||
    fail "A canonical update request id is required for helper refresh handoff."
  host_app_dir=$(helper_host_app_dir)
  bridge_script="$host_app_dir/scripts/km-vms-update-helper-bridge.py"
  [ -f "$bridge_script" ] || fail "Target update-helper bridge is missing."
  bootstrap_output=$(
    cd "$APP_DIR"
    compose_with_archive_roots run --rm --no-deps update-helper-bootstrap \
      python3 "$bridge_script" bootstrap --require-request-id "$request_id"
  ) || fail "Cannot schedule update-helper refresh handoff."
  printf '%s\n' "$bootstrap_output" | grep -Fq "update_helper_request_id=$request_id" ||
    fail "Update-helper refresh handoff did not acknowledge the active request."
  if printf '%s\n' "$bootstrap_output" | grep -Eq '^update_helper_bootstrap=(PASS|ALREADY_SCHEDULED)$'; then
    UPDATE_HELPER_REFRESH_SCHEDULED=1
    return 0
  fi
  fail "Update-helper refresh handoff was not scheduled."
}

compose_config() {
  PHASE="compose_config"
  write_helper_progress "running" "Validating Docker Compose config."
  (
    cd "$APP_DIR"
    compose_with_archive_roots config >/dev/null
  ) || fail "Compose config validation failed."
}

normalize_legacy_schema_override_service() {
  override="$(archive_roots_compose_file)"
  [ -e "$override" ] || return 0
  [ -f "$override" ] && [ ! -L "$override" ] ||
    fail "Generated archive-roots Compose override is not a regular file."
  legacy_count=$(grep -c '^  operation-recovery:$' "$override" || true)
  target_count=$(grep -c '^  schema-update:$' "$override" || true)
  [ "$legacy_count" -le 1 ] && [ "$target_count" -le 1 ] ||
    fail "Generated archive-roots Compose override has ambiguous schema services."
  if [ "$legacy_count" = "1" ] && [ "$target_count" = "0" ]; then
    tmp_override="$override.tmp.$$"
    sed 's/^  operation-recovery:$/  schema-update:/' "$override" \
      > "$tmp_override" ||
      fail "Cannot normalize the generated schema service override."
    chmod 600 "$tmp_override" 2>/dev/null || true
    mv "$tmp_override" "$override" ||
      fail "Cannot activate the normalized schema service override."
  elif [ "$legacy_count" = "1" ] && [ "$target_count" = "1" ]; then
    fail "Generated archive-roots Compose override has conflicting schema services."
  fi
}

UPDATE_ONE_SHOT_SERVICES="update-helper-bootstrap schema-update"

compose_service_failed() {
  service="$1"
  container_id=$(
    compose_with_archive_roots ps -a -q "$service" 2>/dev/null |
      head -n 1
  )
  [ -n "$container_id" ] || return 1
  container_state=$(
    docker inspect \
      --format '{{.State.Status}}:{{.State.ExitCode}}' \
      "$container_id" 2>/dev/null || true
  )
  case "$container_state" in
    exited:0) return 1 ;;
    exited:*|dead:*) return 0 ;;
    *) return 1 ;;
  esac
}

schema_pipeline_failed() {
  for service in schema-update; do
    if compose_service_failed "$service"; then
      return 0
    fi
  done
  return 1
}

reset_update_one_shots() {
  compose_with_archive_roots rm -f -s $UPDATE_ONE_SHOT_SERVICES \
    >/dev/null ||
    fail "Cannot reset stopped update one-shot containers."
}

reset_failed_update_bootstrap() {
  compose_service_failed update-helper-bootstrap || return 0
  compose_with_archive_roots rm -f -s update-helper-bootstrap \
    >/dev/null ||
    fail "Cannot reset the failed update-helper bootstrap container."
}

rebuild_recreate() {
  PHASE="rebuild_recreate"
  write_helper_progress "running" "Rebuilding and recreating containers."
  ARCHIVE_ROOTS_COMPOSE_WAS_PRESENT=0
  archive_roots_compose_present && ARCHIVE_ROOTS_COMPOSE_WAS_PRESENT=1
  (
    cd "$APP_DIR"
    if [ "${KM_VMS_UPDATE_HELPER_MODE:-0}" = "1" ]; then
      reset_update_one_shots
      compose_with_archive_roots up -d --build postgres redis api recorder web nginx || {
        if schema_pipeline_failed; then
          return 1
        fi
        reset_failed_update_bootstrap
        sleep 5
        compose_with_archive_roots up -d --build postgres redis api recorder web nginx
      }
    else
      compose_with_archive_roots up -d --build
    fi
  ) || {
    if (
      cd "$APP_DIR"
      schema_pipeline_failed
    ); then
      PHASE="schema_update"
      fail "Database schema preparation failed."
    fi
    fail "Compose rebuild/recreate failed."
  }
}

health_check() {
  PHASE="health_check"
  write_helper_progress "running" "Checking API health after update."
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
  write_helper_progress "running" "Writing source metadata."
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
  if [ -f "$identity" ]; then
    # Preserve the bind-mounted file inode so running containers do not keep
    # reading a stale precompose identity after the final complete write.
    cat "$tmp_identity" > "$identity"
    rm -f "$tmp_identity"
  else
    mv "$tmp_identity" "$identity"
  fi
  RELEASE_IDENTITY_HOST_STATUS="$metadata_status"
}

api_visible_release_identity_status() {
  [ -n "$SOURCE_COMMIT_SHA" ] || return 1
  (
    cd "$APP_DIR"
    compose_with_archive_roots exec -T api python -c '
import json
import sys
from pathlib import Path

expected = sys.argv[1]
path = Path("/app/.km-vms-release.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(10)
status = str(data.get("metadata_status") or "")
commit = str(data.get("commit_sha") or "")
if status != "complete":
    print(f"metadata_status={status or 'missing'}", file=sys.stderr)
    raise SystemExit(11)
if commit.lower() != expected.lower():
    print("commit_mismatch", file=sys.stderr)
    raise SystemExit(12)
print(f"complete {commit.lower()}")
' "$SOURCE_COMMIT_SHA"
  )
}

verify_api_visible_release_identity() {
  PHASE="metadata_write"
  write_helper_progress "running" "Verifying API-visible release identity."
  RELEASE_IDENTITY_API_VISIBLE=0
  RELEASE_IDENTITY_COMMIT_VERIFIED=0
  RELEASE_IDENTITY_API_STATUS=""
  [ -n "$SOURCE_COMMIT_SHA" ] || fail "Cannot mark update complete without trusted commit evidence."

  if api_status=$(api_visible_release_identity_status 2>/dev/null); then
    RELEASE_IDENTITY_API_STATUS="$(printf '%s\n' "$api_status" | tail -n 1 | awk '{print $1}')"
  else
    RELEASE_IDENTITY_API_STATUS=""
  fi
  if [ "$RELEASE_IDENTITY_API_STATUS" = "complete" ]; then
    RELEASE_IDENTITY_API_VISIBLE=1
    RELEASE_IDENTITY_COMMIT_VERIFIED=1
    return 0
  fi

  info "API-visible release identity is stale or incomplete; recreating api service to remount final identity."
  (
    cd "$APP_DIR"
    compose_with_archive_roots up -d --force-recreate api
  ) || fail "API recreate after release identity finalization failed."
  health_check
  PHASE="metadata_write"
  if api_status=$(api_visible_release_identity_status 2>/dev/null); then
    RELEASE_IDENTITY_API_STATUS="$(printf '%s\n' "$api_status" | tail -n 1 | awk '{print $1}')"
  else
    RELEASE_IDENTITY_API_STATUS=""
  fi
  if [ "$RELEASE_IDENTITY_API_STATUS" = "complete" ]; then
    RELEASE_IDENTITY_API_VISIBLE=1
    RELEASE_IDENTITY_COMMIT_VERIFIED=1
    return 0
  fi
  fail "API-visible release identity is not complete after final identity write and api recreate."
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
  info "Would prepare: one immutable trusted target release outside the active source"
  info "Would preserve: $PRESERVED_PATHS"
  info "Activation: one atomic active-slot switch after target build and schema gate"
  info "Rollback: exact captured previous runtime on target health or identity failure"
}

PHASE="init"
write_helper_progress "running" "Starting update."
validate_app_dir
PROJECT_NAME="${PROJECT_NAME:-$(read_env_value COMPOSE_PROJECT_NAME)}"
safe_project_name "$PROJECT_NAME"
if [ "$DRY_RUN" != "1" ]; then
  acquire_lock
fi
load_compose_common
PRODUCT_SOURCE_DIR=$(km_vms_resolve_product_source "$APP_DIR")
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
ensure_activation_request_id
preflight_permission_policy
preflight_target_permission_policy
prepare_schema_handoff
normalize_legacy_schema_override_service
staged_compose_config
prepare_trusted_target_slot
activate_trusted_target_slot
PRODUCT_SOURCE_DIR=$(km_vms_resolve_product_source "$APP_DIR")
UPDATED_PATHS="data/update-runtime/slots/$TARGET_SLOT_ID data/update-runtime/active"
RELEASE_IDENTITY_HOST_STATUS="complete"
RELEASE_IDENTITY_API_STATUS="complete"
RELEASE_IDENTITY_API_VISIBLE=1
RELEASE_IDENTITY_COMMIT_VERIFIED=1
postflight_preservation
PHASE="metadata_write"
write_helper_progress "running" "Writing successful update metadata."
write_update_metadata "success" ""
PHASE="cleanup"
write_helper_progress "completed" "Update completed."
info "KM VMS staged activation completed."
info "Active target slot: $TARGET_SLOT_ID"
info "Preserved paths: $PRESERVED_PATHS"
info "Update metadata: $APP_DIR/.km-vms-update.json"
