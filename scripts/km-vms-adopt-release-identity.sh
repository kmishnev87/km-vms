#!/usr/bin/env sh
set -eu

APP_DIR="${KM_VMS_APP_DIR:-}"
APPLY=0

usage() {
  cat <<'EOF'
KM VMS release identity adoption

Usage:
  sh scripts/km-vms-adopt-release-identity.sh [--app-dir <path>] [--apply]

Without --apply the command is a dry-run. It writes .km-vms-release.json only
when --apply is provided and the current source can be matched safely.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-dir)
      [ "$#" -ge 2 ] || fail "--app-dir requires a value"
      APP_DIR="$2"
      shift 2
      ;;
    --apply)
      APPLY=1
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

if [ -z "$APP_DIR" ]; then
  cwd=$(pwd -P)
  if [ -f "$cwd/docker-compose.yml" ] && [ -d "$cwd/apps/api" ]; then
    APP_DIR="$cwd"
  else
    parent=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
    APP_DIR="$parent"
  fi
fi

[ -f "$APP_DIR/docker-compose.yml" ] || fail "App dir is not a KM VMS installation: $APP_DIR"

descriptor="$APP_DIR/release/km-vms-release.json"
[ -f "$descriptor" ] || fail "Release descriptor is missing: release/km-vms-release.json"

value() {
  key="$1"
  sed -n "s/^[[:space:]]*\"$key\"[[:space:]]*:[[:space:]]*\"\(.*\)\"[[:space:]]*,\{0,1\}[[:space:]]*$/\1/p" "$descriptor" | head -n 1
}

commit=""
if [ -d "$APP_DIR/.git" ] && command -v git >/dev/null 2>&1; then
  commit=$(cd "$APP_DIR" && git rev-parse HEAD 2>/dev/null || true)
elif [ -f "$APP_DIR/.km-vms-source.json" ]; then
  commit=$(sed -n 's/^[[:space:]]*"commit_sha"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]\{40\}\)".*/\1/p' "$APP_DIR/.km-vms-source.json" | head -n 1)
fi

[ -n "$commit" ] || fail "Cannot prove current source commit; adoption is blocked."

version=$(value version)
title=$(value title)
summary=$(value summary)
channel=$(value release_channel)
source_kind=$(value source_kind)
source_repo=$(value source_repo)
source_ref=$(value source_ref)
[ -n "$version" ] || fail "Release descriptor has no version."
[ -n "$title" ] || title="KM VMS release"
[ -n "$summary" ] || summary="KM VMS release identity."
[ -n "$channel" ] || channel="public-github"
[ -n "$source_kind" ] || source_kind="github-release"
[ -n "$source_repo" ] || source_repo="kmishnev87/km-vms"
[ -n "$source_ref" ] || source_ref="main"

printf 'KM VMS release identity adoption\n'
printf 'App dir: %s\n' "$APP_DIR"
printf 'Version: %s\n' "$version"
printf 'Source repo: %s\n' "$source_repo"
printf 'Source ref: %s\n' "$source_ref"
printf 'Commit evidence: %s\n' "$commit"

if [ "$APPLY" != "1" ]; then
  printf 'Dry-run complete. No files were written.\n'
  exit 0
fi

installed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
identity="$APP_DIR/.km-vms-release.json"
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
  printf '  "commit_sha": "%s",\n' "$(json_escape "$commit")"
  printf '  "installed_at": "%s",\n' "$(json_escape "$installed_at")"
  printf '  "installed_by": "adoption",\n'
  printf '  "metadata_status": "adopted",\n'
  printf '  "metadata_source": "adoption"\n'
  printf '}\n'
} > "$identity"
printf 'Release identity written: %s\n' "$identity"
