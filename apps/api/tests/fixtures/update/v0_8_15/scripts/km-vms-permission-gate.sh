#!/usr/bin/env sh
set -eu

ACTION="check"
CONTRACT="source"
APP_DIR=""
ACL_CHECK="mode_only"

usage() {
  cat <<'EOF'
KM VMS privileged-chain permission gate

Usage:
  sh scripts/km-vms-permission-gate.sh --check [--app-dir <path>]
  sh scripts/km-vms-permission-gate.sh --fix [--app-dir <path>]
  sh scripts/km-vms-permission-gate.sh --preflight-existing --check [--app-dir <path>]
  sh scripts/km-vms-permission-gate.sh --preflight-existing --fix [--app-dir <path>]
  sh scripts/km-vms-permission-gate.sh --contract source --check --app-dir <source>
  sh scripts/km-vms-permission-gate.sh --contract stable-prebootstrap --check --app-dir <stable-root>
  sh scripts/km-vms-permission-gate.sh --contract stable-runtime --check --app-dir <stable-root>
  sh scripts/km-vms-permission-gate.sh --contract legacy --check --app-dir <legacy-root>

The gate covers only the host-privileged updater/helper entrypoints, their
parent directories, Docker Compose and the helper image definition. It never
recursively normalizes application source, assets, tests, docs, .env or data.
Runtime identity/mount files are checked when present but are never changed.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check)
      ACTION="check"
      shift
      ;;
    --fix)
      ACTION="fix"
      shift
      ;;
    --preflight-existing)
      CONTRACT="legacy"
      shift
      ;;
    --contract)
      [ "$#" -ge 2 ] || fail "--contract requires a value"
      CONTRACT="$2"
      shift 2
      ;;
    --app-dir)
      [ "$#" -ge 2 ] || fail "--app-dir requires a value"
      APP_DIR="$2"
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

case "$CONTRACT" in
  source|stable-prebootstrap|stable-runtime|legacy) ;;
  *) fail "Unsupported permission contract: $CONTRACT" ;;
esac

[ -n "$APP_DIR" ] || APP_DIR=$(pwd -P)
[ -d "$APP_DIR" ] || fail "App dir does not exist: $APP_DIR"
APP_DIR=$(CDPATH= cd "$APP_DIR" 2>/dev/null && pwd -P) ||
  fail "Cannot resolve app dir: $APP_DIR"

case "$APP_DIR" in
  /|/Volume1|/Volume2|/Volume3|/home|/root|/var|/etc|/tmp)
    fail "Refusing dangerous app dir: $APP_DIR"
    ;;
esac

if [ "$CONTRACT" = "source" ] || [ "$CONTRACT" = "legacy" ]; then
  for relative in apps apps/update-helper scripts; do
    path="$APP_DIR/$relative"
    [ ! -L "$path" ] ||
      fail "Privileged-chain directory must not be a symlink: $relative"
    [ -d "$path" ] ||
      fail "Required privileged-chain directory is missing: $relative"
  done
fi

BASE_PRIVILEGED_FILES="
docker-compose.yml
apps/update-helper/Dockerfile
scripts/install.sh
scripts/update.sh
scripts/km-vms-compose-common.sh
scripts/km-vms-update-helper.py
scripts/km-vms-setup-activation-helper.sh
scripts/km-vms-release-cycle.sh
scripts/km-vms-adopt-release-identity.sh
scripts/km-vms-restart.sh
scripts/km-vms-update-launcher.sh
scripts/km-vms-storage-apply.sh
scripts/km-vms-storage-discovery.sh
scripts/km-vms-bootstrap.py
scripts/km-vms-bootstrap-dispatch.sh
"

TARGET_ONLY_PRIVILEGED_FILES="
release/km-vms-update-lineage.json
scripts/km-vms-permission-gate.sh
scripts/km-vms-release-identity.py
scripts/km-vms-update-helper-bridge.py
scripts/km-vms-release-slots.py
scripts/km-vms-bootstrap.py
scripts/km-vms-bootstrap-dispatch.sh
scripts/km-vms-publish-github-release.sh
scripts/km-vms-storage-candidate-validate.sh
scripts/km-vms-storage-root-cleanup.sh
"

EXECUTABLE_FILES="
scripts/install.sh
scripts/update.sh
scripts/km-vms-compose-common.sh
scripts/km-vms-setup-activation-helper.sh
scripts/km-vms-release-cycle.sh
scripts/km-vms-adopt-release-identity.sh
scripts/km-vms-restart.sh
scripts/km-vms-update-launcher.sh
scripts/km-vms-storage-apply.sh
scripts/km-vms-storage-discovery.sh
scripts/km-vms-permission-gate.sh
scripts/km-vms-release-identity.py
scripts/km-vms-release-slots.py
scripts/km-vms-bootstrap.py
scripts/km-vms-bootstrap-dispatch.sh
scripts/km-vms-publish-github-release.sh
"

PRIVILEGED_FILES=""
if [ "$CONTRACT" = "source" ] || [ "$CONTRACT" = "legacy" ]; then
  for relative in $BASE_PRIVILEGED_FILES; do
    path="$APP_DIR/$relative"
    if [ "$CONTRACT" = "legacy" ] && [ ! -e "$path" ] && [ ! -L "$path" ]; then
      continue
    fi
    [ ! -L "$path" ] ||
      fail "Privileged-chain path must not be a symlink: $relative"
    [ -f "$path" ] ||
      fail "Required privileged-chain file is missing: $relative"
    PRIVILEGED_FILES="$PRIVILEGED_FILES
$relative"
  done
  for relative in $TARGET_ONLY_PRIVILEGED_FILES; do
    path="$APP_DIR/$relative"
    if [ "$CONTRACT" = "legacy" ] && [ ! -e "$path" ] && [ ! -L "$path" ]; then
      continue
    fi
    [ ! -L "$path" ] ||
      fail "Privileged-chain path must not be a symlink: $relative"
    [ -f "$path" ] ||
      fail "Required privileged-chain file is missing: $relative"
    PRIVILEGED_FILES="$PRIVILEGED_FILES
$relative"
  done
fi

stat_mode() {
  if [ "$STAT_STYLE" = "gnu" ]; then
    stat -c '%a' "$1"
  else
    stat -f '%Lp' "$1"
  fi
}

stat_owner_group() {
  if [ "$STAT_STYLE" = "gnu" ]; then
    stat -c '%u:%g' "$1"
  else
    stat -f '%u:%g' "$1"
  fi
}

if stat -c '%a' "$APP_DIR" >/dev/null 2>&1; then
  STAT_STYLE="gnu"
elif stat -f '%Lp' "$APP_DIR" >/dev/null 2>&1; then
  STAT_STYLE="bsd"
else
  fail "A supported stat implementation is required"
fi

command -v stat >/dev/null 2>&1 ||
  fail "stat is required for privileged-chain inspection"
if [ "$ACTION" = "fix" ]; then
  command -v chmod >/dev/null 2>&1 ||
    fail "chmod is required for privileged-chain repair"
fi

ROOT_OWNER_GROUP=$(stat_owner_group "$APP_DIR") ||
  fail "Cannot read app-dir owner/group"
ROOT_UID=$(printf '%s\n' "$ROOT_OWNER_GROUP" | cut -d: -f1)

unsafe_mode() {
  mode="$1"
  case "$mode" in
    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
    *) return 0 ;;
  esac
  [ $((0$mode & 0002)) -eq 0 ] || return 0
  [ $((0$mode & 07000)) -eq 0 ] || return 0
  return 1
}

trusted_owner() {
  owner_group=$(stat_owner_group "$1") || return 1
  owner_uid=$(printf '%s\n' "$owner_group" | cut -d: -f1)
  [ "$owner_uid" = "$ROOT_UID" ] || [ "$owner_uid" = "0" ]
}

acl_permissions_field() {
  acl_record=${1%%#*}
  acl_permissions=${acl_record##*:}
  acl_permissions=${acl_permissions%%[	 ]*}
  case "$acl_permissions" in
    [r-][w-][x-]) printf '%s\n' "$acl_permissions" ;;
    *) return 1 ;;
  esac
}

acl_has_write() {
  case "$1" in
    ?w?) return 0 ;;
    *) return 1 ;;
  esac
}

ACL_TMP=""
if command -v getfacl >/dev/null 2>&1; then
  ACL_TMP=$(mktemp /tmp/km-vms-permission-gate-acl.XXXXXX 2>/dev/null || true)
  if [ -n "$ACL_TMP" ]; then
    ACL_CHECK="available"
    trap 'rm -f "$ACL_TMP"' 0
    trap 'rm -f "$ACL_TMP"; exit 1' 1 2 15
  fi
fi

check_path_acl() {
  path="$1"
  relative="$2"
  [ "$ACL_CHECK" = "available" ] || return 0
  if ! getfacl -cp "$path" >"$ACL_TMP" 2>/dev/null; then
    ACL_CHECK="partial_mode_only"
    return 0
  fi
  while IFS= read -r acl_line; do
    case "$acl_line" in
      ''|'#'*)
        ;;
      user::*|group::*|mask::*|default:user::*|default:group::*|default:mask::*|user:*:*|group:*:*|default:user:*:*|default:group:*:*)
        acl_permissions_field "$acl_line" >/dev/null ||
          fail "Unsupported ACL permissions on privileged path: $relative"
        ;;
      other::*|default:other::*)
        permissions=$(acl_permissions_field "$acl_line") ||
          fail "Unsupported ACL permissions on privileged path: $relative"
        if acl_has_write "$permissions"; then
          fail "ACL grants world write on privileged path: $relative"
        fi
        ;;
      *)
        fail "Unsupported ACL record on privileged path: $relative"
        ;;
    esac
  done <"$ACL_TMP"
}

is_executable_file() {
  candidate="$1"
  for relative in $EXECUTABLE_FILES; do
    [ "$candidate" != "$relative" ] || return 0
  done
  return 1
}

mode_has_bits() {
  mode="$1"
  required="$2"
  [ $((0$mode & 0$required)) -eq $((0$required)) ]
}

preflight_path() {
  path="$1"
  relative="$2"
  expected_type="$3"
  [ ! -L "$path" ] ||
    fail "Privileged-chain path must not be a symlink: $relative"
  case "$expected_type" in
    file) [ -f "$path" ] || fail "Privileged-chain file changed type: $relative" ;;
    dir) [ -d "$path" ] || fail "Privileged-chain directory changed type: $relative" ;;
    *) fail "Internal privileged-chain type is invalid: $relative" ;;
  esac
  trusted_owner "$path" ||
    fail "Privileged-chain owner is not trusted: $relative"
  if [ "$ACTION" != "fix" ]; then
    check_path_acl "$path" "$relative"
  fi
}

check_secure_path() {
  path="$1"
  relative="$2"
  expected_type="$3"
  preflight_path "$path" "$relative" "$expected_type"
  trusted_owner "$path" ||
    fail "Privileged-chain owner is not trusted: $relative"
  mode=$(stat_mode "$path") || fail "Cannot read mode: $relative"
  unsafe_mode "$mode" &&
    fail "Privileged-chain path is world-writable or has special bits: $relative mode=$mode"
  if [ "$expected_type" = "dir" ]; then
    if [ "$CONTRACT" = "source" ]; then
      mode_has_bits "$mode" 500 ||
        fail "Privileged source directory owner must have read/execute access: $relative mode=$mode"
    else
      mode_has_bits "$mode" 700 ||
        fail "Privileged-chain directory owner must have rwx access: $relative mode=$mode"
    fi
  elif is_executable_file "$relative"; then
    mode_has_bits "$mode" 500 ||
      fail "Privileged executable owner must have read/execute access: $relative mode=$mode"
  else
    mode_has_bits "$mode" 400 ||
      fail "Privileged non-executable owner must have read access: $relative mode=$mode"
  fi
}

normalize_path() {
  path="$1"
  relative="$2"
  expected_type="$3"
  if [ "$expected_type" = "dir" ]; then
    if [ "$CONTRACT" = "source" ]; then
      chmod u+rx "$path" ||
        fail "Cannot grant owner read/execute access to privileged source directory: $relative"
    else
      chmod u+rwx "$path" ||
        fail "Cannot grant owner access to privileged-chain directory: $relative"
    fi
  elif is_executable_file "$relative"; then
    chmod u+rx "$path" ||
      fail "Cannot grant owner access to privileged executable: $relative"
  else
    chmod u+r "$path" ||
      fail "Cannot grant owner read access to privileged file: $relative"
  fi
  chmod o-w "$path" ||
    fail "Cannot remove world write from privileged-chain path: $relative"
  chmod u-s,g-s,o-t "$path" ||
    fail "Cannot clear privileged-chain special mode bits: $relative"
}

if [ "$CONTRACT" = "source" ] || [ "$CONTRACT" = "legacy" ]; then
  PRIVILEGED_DIRECTORIES=". apps apps/update-helper scripts"
else
  PRIVILEGED_DIRECTORIES=""
fi
for relative in $PRIVILEGED_DIRECTORIES; do
  if [ "$relative" = "." ]; then
    path="$APP_DIR"
  else
    path="$APP_DIR/$relative"
  fi
  preflight_path "$path" "$relative" dir
done
for relative in $PRIVILEGED_FILES; do
  preflight_path "$APP_DIR/$relative" "$relative" file
done

if [ "$ACTION" = "fix" ]; then
  for relative in $PRIVILEGED_DIRECTORIES; do
    if [ "$relative" = "." ]; then
      path="$APP_DIR"
    else
      path="$APP_DIR/$relative"
    fi
    normalize_path "$path" "$relative" dir
  done
  for relative in $PRIVILEGED_FILES; do
    normalize_path "$APP_DIR/$relative" "$relative" file
  done
fi

for relative in $PRIVILEGED_DIRECTORIES; do
  if [ "$relative" = "." ]; then
    path="$APP_DIR"
  else
    path="$APP_DIR/$relative"
  fi
  check_secure_path "$path" "$relative" dir
done
for relative in $PRIVILEGED_FILES; do
  check_secure_path "$APP_DIR/$relative" "$relative" file
done

if [ "$CONTRACT" = "stable-prebootstrap" ] || [ "$CONTRACT" = "stable-runtime" ]; then
  STABLE_DIRECTORIES=".
data
data/install-control
data/update-control
data/update-runtime
data/update-runtime/slots
data/update-runtime/staging"
  # Historical installations can legitimately predate the installer receipt.
  # The receipt is not a runtime authority, so require only .env here and
  # validate the receipt below when it is present.
  STABLE_FILES=".env"
  for relative in $STABLE_DIRECTORIES; do
    if [ "$relative" = "." ]; then path="$APP_DIR"; else path="$APP_DIR/$relative"; fi
    preflight_path "$path" "$relative" dir
  done
  for relative in $STABLE_FILES; do
    preflight_path "$APP_DIR/$relative" "$relative" file
  done
  if [ "$ACTION" = "fix" ]; then
    for relative in $STABLE_DIRECTORIES; do
      if [ "$relative" = "." ]; then path="$APP_DIR"; else path="$APP_DIR/$relative"; fi
      normalize_path "$path" "$relative" dir
    done
    for relative in $STABLE_FILES; do
      normalize_path "$APP_DIR/$relative" "$relative" file
    done
  fi
  for relative in $STABLE_DIRECTORIES; do
    if [ "$relative" = "." ]; then path="$APP_DIR"; else path="$APP_DIR/$relative"; fi
    check_secure_path "$path" "$relative" dir
  done
  for relative in $STABLE_FILES; do
    check_secure_path "$APP_DIR/$relative" "$relative" file
  done
fi

if [ "$CONTRACT" = "stable-runtime" ]; then
  command -v python3 >/dev/null 2>&1 ||
    fail "python3 is required for stable runtime authority validation"
  bundle="$APP_DIR/data/update-runtime/bootstrap/current"
  [ -L "$bundle" ] && [ -d "$bundle" ] ||
    fail "Stable bootstrap pointer is unavailable or unsafe"
  bundle_real=$(CDPATH= cd "$bundle" 2>/dev/null && pwd -P) ||
    fail "Stable bootstrap bundle cannot be resolved"
  case "$bundle_real" in
    "$APP_DIR"/data/update-runtime/bootstrap/bundles/*) ;;
    *) fail "Stable bootstrap bundle escaped its bounded root" ;;
  esac
  check_secure_path "$bundle_real" "data/update-runtime/bootstrap/current" dir
  for name in km-vms-bootstrap.py km-vms-bootstrap-dispatch.sh km-vms-release-slots.py km-vms-compose-common.sh km-vms-restart.sh km-vms-update-launcher.sh km-vms-storage-apply.sh km-vms-setup-activation-helper.sh bootstrap-manifest.json bootstrap-files.sha256 docker-compose.lifecycle.yml; do
    check_secure_path "$bundle_real/$name" "data/update-runtime/bootstrap/current/$name" file
  done
  python3 -B "$bundle/km-vms-bootstrap.py" validate-bundle --app-dir "$APP_DIR" >/dev/null ||
    fail "Stable bootstrap manifest or lifecycle digest is invalid"
  python3 -B "$bundle/km-vms-release-slots.py" resolve-active --app-dir "$APP_DIR" >/dev/null ||
    fail "Canonical active release slot is invalid"
  python3 -B "$bundle/km-vms-release-slots.py" validate-installed-projection --app-dir "$APP_DIR" >/dev/null ||
    fail "Installed-slot projection is invalid"
fi

RUNTIME_CRITICAL_FILES="
.env
.km-vms-install.json
.km-vms-source.json
.km-vms-release.json
data/install-control/archive-roots-runtime.json
data/install-control/docker-compose.archive-roots.yml
"
for relative in $RUNTIME_CRITICAL_FILES; do
  path="$APP_DIR/$relative"
  [ -e "$path" ] || [ -L "$path" ] || continue
  [ ! -L "$path" ] ||
    fail "Runtime authority path must not be a symlink: $relative"
  [ -f "$path" ] ||
    fail "Runtime authority path is not a regular file: $relative"
  trusted_owner "$path" ||
    fail "Runtime authority owner is not trusted: $relative"
  mode=$(stat_mode "$path") || fail "Cannot read mode: $relative"
  unsafe_mode "$mode" &&
    fail "Runtime authority is world-writable or has special bits: $relative mode=$mode"
  check_path_acl "$path" "$relative"
done

if [ -e "$APP_DIR/data/install-control" ] || [ -L "$APP_DIR/data/install-control" ]; then
  for relative in data data/install-control; do
    path="$APP_DIR/$relative"
    [ ! -L "$path" ] ||
      fail "Runtime authority parent must not be a symlink: $relative"
    [ -d "$path" ] ||
      fail "Runtime authority parent is not a directory: $relative"
    trusted_owner "$path" ||
      fail "Runtime authority parent owner is not trusted: $relative"
    mode=$(stat_mode "$path") || fail "Cannot read mode: $relative"
    unsafe_mode "$mode" &&
      fail "Runtime authority parent is world-writable or has special bits: $relative mode=$mode"
    check_path_acl "$path" "$relative"
  done
fi

printf 'permission_gate=PASS\n'
printf 'permission_action=%s\n' "$ACTION"
printf 'permission_app_dir=%s\n' "$APP_DIR"
printf 'permission_scope=privileged_chain\n'
printf 'permission_acl_check=%s\n' "$ACL_CHECK"
printf 'permission_contract=%s\n' "$CONTRACT"
