#!/usr/bin/env sh
set -eu

ACTION="check"
CONTRACT="target"
APP_DIR=""

usage() {
  cat <<'EOF'
KM VMS product-source permission gate

Usage:
  sh scripts/km-vms-permission-gate.sh --check [--app-dir <path>]
  sh scripts/km-vms-permission-gate.sh --fix [--app-dir <path>]
  sh scripts/km-vms-permission-gate.sh --preflight-existing --check [--app-dir <path>]
  sh scripts/km-vms-permission-gate.sh --preflight-existing --fix [--app-dir <path>]

The gate is intentionally limited to KM VMS product source:
  apps, deploy, docs, release, scripts and selected top-level product files.

It never changes .env, data, .git, build caches, working folders or service
artifacts. --fix assigns 0755 to product directories, 0644 to regular product
files and 0755 only to the explicit executable-script allowlist.

--preflight-existing validates an already installed older tree before overlay.
It does not require target-only privileged files that the overlay has not added yet.
The default target contract requires the complete target privileged chain.
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
      CONTRACT="existing"
      shift
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

if [ -z "$APP_DIR" ]; then
  APP_DIR=$(pwd -P)
fi
[ -d "$APP_DIR" ] || fail "App dir does not exist: $APP_DIR"
APP_DIR=$(CDPATH= cd "$APP_DIR" 2>/dev/null && pwd -P) || fail "Cannot resolve app dir: $APP_DIR"

case "$APP_DIR" in
  /|/Volume1|/Volume2|/Volume3|/home|/root|/var|/etc|/tmp)
    fail "Refusing dangerous app dir: $APP_DIR"
    ;;
esac

for relative in apps deploy docs release scripts; do
  [ -d "$APP_DIR/$relative" ] || fail "Required product directory is missing: $relative"
done

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
scripts/km-vms-storage-apply.sh
scripts/km-vms-storage-discovery.sh
"

TARGET_ONLY_PRIVILEGED_FILES="
scripts/km-vms-permission-gate.sh
scripts/km-vms-update-helper-bridge.py
"

for relative in $BASE_PRIVILEGED_FILES; do
  [ -f "$APP_DIR/$relative" ] || fail "Required privileged-chain file is missing: $relative"
  [ ! -L "$APP_DIR/$relative" ] || fail "Privileged-chain path must not be a symlink: $relative"
done

for relative in $TARGET_ONLY_PRIVILEGED_FILES; do
  if [ "$CONTRACT" = "target" ]; then
    [ -f "$APP_DIR/$relative" ] || fail "Required privileged-chain file is missing: $relative"
  elif [ ! -e "$APP_DIR/$relative" ]; then
    continue
  fi
  [ -f "$APP_DIR/$relative" ] || fail "Privileged-chain path is not a regular file: $relative"
  [ ! -L "$APP_DIR/$relative" ] || fail "Privileged-chain path must not be a symlink: $relative"
done

PRODUCT_TOP_FILES="
.dockerignore
.env.example
.gitattributes
.gitignore
docker-compose.pytest.yml
docker-compose.yml
"

EXECUTABLE_FILES="
scripts/install.sh
scripts/km-vms-adopt-release-identity.sh
scripts/km-vms-compose-common.sh
scripts/km-vms-publish-github-release.sh
scripts/km-vms-release-cycle.sh
scripts/km-vms-restart.sh
scripts/km-vms-setup-activation-helper.sh
scripts/km-vms-storage-apply.sh
scripts/km-vms-storage-discovery.sh
scripts/run_backend_tests.sh
scripts/update.sh
"

if [ "$CONTRACT" = "target" ] || [ -f "$APP_DIR/scripts/km-vms-permission-gate.sh" ]; then
  EXECUTABLE_FILES="$EXECUTABLE_FILES
scripts/km-vms-permission-gate.sh"
fi

CRITICAL_NON_EXECUTABLE_FILES="
docker-compose.yml
apps/update-helper/Dockerfile
scripts/km-vms-update-helper.py
"

if [ "$CONTRACT" = "target" ] || [ -f "$APP_DIR/scripts/km-vms-update-helper-bridge.py" ]; then
  CRITICAL_NON_EXECUTABLE_FILES="$CRITICAL_NON_EXECUTABLE_FILES
scripts/km-vms-update-helper-bridge.py"
fi

stat_mode() {
  if [ "${STAT_STYLE:-}" = "gnu" ]; then
    stat -c '%a' "$1"
  else
    stat -f '%Lp' "$1"
  fi
}

stat_owner_group() {
  if [ "${STAT_STYLE:-}" = "gnu" ]; then
    stat -c '%u:%g' "$1"
  else
    stat -f '%u:%g' "$1"
  fi
}

stat_identity() {
  if [ "${STAT_STYLE:-}" = "gnu" ]; then
    stat -c '%d:%i' "$1"
  else
    stat -f '%d:%i' "$1"
  fi
}

if stat -c '%a' "$APP_DIR" >/dev/null 2>&1; then
  STAT_STYLE="gnu"
elif stat -f '%Lp' "$APP_DIR" >/dev/null 2>&1; then
  STAT_STYLE="bsd"
else
  fail "A supported stat implementation is required"
fi

command -v getfacl >/dev/null 2>&1 ||
  fail "getfacl is required; critical ACL state must not be skipped"
for required_command in find stat chmod mktemp sort cmp rm; do
  command -v "$required_command" >/dev/null 2>&1 ||
    fail "$required_command is required for complete permission inspection"
done
GATE_TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/km-vms-permission-gate.XXXXXX") ||
  fail "Cannot create permission-gate temporary directory"
cleanup_gate_tmp() {
  rm -rf "$GATE_TMP_DIR"
}
trap cleanup_gate_tmp 0
trap 'cleanup_gate_tmp; exit 1' 1 2 15

unsafe_write_mode() {
  mode="$1"
  case "$mode" in
    [0-7][0-7][0-7]) ;;
    [0-7][0-7][0-7][0-7])
      special=${mode%???}
      [ "$special" = "0" ] || return 0
      mode=${mode#?}
      ;;
    *)
      return 0
      ;;
  esac
  last_two=${mode#?}
  group_digit=${last_two%?}
  other_digit=${last_two#?}
  case "$group_digit" in
    2|3|6|7) return 0 ;;
  esac
  case "$other_digit" in
    2|3|6|7) return 0 ;;
  esac
  return 1
}

check_mode_not_shared_writable() {
  path="$1"
  relative=${path#"$APP_DIR"/}
  [ "$relative" != "$path" ] || relative="."
  mode=$(stat_mode "$path") || fail "Cannot read mode: $relative"
  if unsafe_write_mode "$mode"; then
    fail "Group/world-writable product path is forbidden: $relative mode=$mode"
  fi
}

ROOT_OWNER_GROUP=$(stat_owner_group "$APP_DIR") || fail "Cannot read app-dir owner/group"

check_owner_group() {
  path="$1"
  relative=${path#"$APP_DIR"/}
  [ "$relative" != "$path" ] || relative="."
  owner_group=$(stat_owner_group "$path") || fail "Cannot read owner/group: $relative"
  [ "$owner_group" = "$ROOT_OWNER_GROUP" ] || {
    fail "Product path owner/group differs from app dir: $relative owner_group=$owner_group expected=$ROOT_OWNER_GROUP"
  }
}

check_exact_critical_modes() {
  for relative in $CRITICAL_NON_EXECUTABLE_FILES; do
    mode=$(stat_mode "$APP_DIR/$relative") || fail "Cannot read mode: $relative"
    case "$mode" in
      640|644) ;;
      *) fail "Critical non-executable mode must be 0640 or 0644: $relative mode=$mode" ;;
    esac
  done
  for relative in $EXECUTABLE_FILES; do
    mode=$(stat_mode "$APP_DIR/$relative") || fail "Cannot read mode: $relative"
    case "$mode" in
      750|755) ;;
      *) fail "Critical executable mode must be 0750 or 0755: $relative mode=$mode" ;;
    esac
  done
  for relative in . apps apps/update-helper deploy docs release scripts; do
    mode=$(stat_mode "$APP_DIR/$relative") || fail "Cannot read mode: $relative"
    case "$mode" in
      700|750|755) ;;
      *) fail "Product directory mode must be 0700, 0750 or 0755: $relative mode=$mode" ;;
    esac
  done
}

path_from_relative() {
  relative="$1"
  if [ "$relative" = "." ]; then
    printf '%s\n' "$APP_DIR"
  else
    printf '%s/%s\n' "$APP_DIR" "$relative"
  fi
}

is_executable_file() {
  relative="$1"
  for executable_relative in $EXECUTABLE_FILES; do
    [ "$executable_relative" != "$relative" ] || return 0
  done
  return 1
}

build_inventory() {
  inventory_file="$1"
  inventory_tag="$2"
  raw_file="$GATE_TMP_DIR/$inventory_tag.raw"
  : >"$raw_file" || fail "Cannot initialize product inventory"
  printf '.\n' >>"$raw_file" || fail "Cannot inventory app-dir"

  scope_index=0
  for scope_relative in apps deploy docs release scripts; do
    scope_index=$((scope_index + 1))
    scope_file="$GATE_TMP_DIR/$inventory_tag.scope-$scope_index"
    if ! find "$APP_DIR/$scope_relative" -print >"$scope_file"; then
      fail "Cannot enumerate product tree: $scope_relative"
    fi
    while IFS= read -r path; do
      [ -n "$path" ] || continue
      case "$path" in
        "$APP_DIR"/*) relative=${path#"$APP_DIR"/} ;;
        *) fail "Product inventory escaped app-dir: $path" ;;
      esac
      [ ! -L "$path" ] || fail "Product source symlink is forbidden: $relative"
      if [ ! -d "$path" ] && [ ! -f "$path" ]; then
        fail "Unsupported product-source path type: $relative"
      fi
      printf '%s\n' "$relative" >>"$raw_file" || fail "Cannot append product inventory"
    done <"$scope_file"
  done

  for relative in $PRODUCT_TOP_FILES; do
    path="$APP_DIR/$relative"
    if [ -e "$path" ] || [ -L "$path" ]; then
      [ ! -L "$path" ] || fail "Top-level product symlink is forbidden: $relative"
      [ -f "$path" ] || fail "Top-level product path is not a regular file: $relative"
      printf '%s\n' "$relative" >>"$raw_file" || fail "Cannot append top-level product inventory"
    fi
  done

  if ! LC_ALL=C sort -u "$raw_file" >"$inventory_file"; then
    fail "Cannot finalize product inventory"
  fi
  [ -s "$inventory_file" ] || fail "Product inventory is empty"
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

acl_permissions_has_write() {
  case "$1" in
    ?w?) return 0 ;;
    *) return 1 ;;
  esac
}

check_path_acl() {
  path="$1"
  relative="$2"
  acl_policy="$3"
  acl_index=$((acl_index + 1))
  acl_file="$GATE_TMP_DIR/acl-$acl_index.txt"
  acl_error="$GATE_TMP_DIR/acl-$acl_index.err"

  if ! getfacl -cp "$path" >"$acl_file" 2>"$acl_error"; then
    fail "Cannot read ACL: $relative"
  fi

  while IFS= read -r acl_line; do
    case "$acl_line" in
      ''|'#'*)
        ;;
      user::*|default:user::*|mask::*|default:mask::*)
        ;;
      group::*|other::*)
        acl_permissions=$(acl_permissions_field "$acl_line") ||
          fail "Unsupported ACL permissions field on a product path: $relative: $acl_line"
        if [ "$acl_policy" = "strict" ] && acl_permissions_has_write "$acl_permissions"; then
          fail "ACL grants non-owner write on a product path: $relative: $acl_line"
        fi
        ;;
      user:*:*|group:*:*|default:user:*:*|default:group::*|default:group:*:*|default:other::*)
        acl_permissions=$(acl_permissions_field "$acl_line") ||
          fail "Unsupported ACL permissions field on a product path: $relative: $acl_line"
        if acl_permissions_has_write "$acl_permissions"; then
          fail "ACL grants non-owner write on a product path: $relative: $acl_line"
        fi
        ;;
      *)
        fail "Unsupported ACL record on a product path: $relative: $acl_line"
        ;;
    esac
  done <"$acl_file"
}

prepare_verified_manifest() {
  inventory_file="$1"
  manifest_file="$2"
  mode_policy="$3"
  acl_policy="$4"
  : >"$manifest_file" || fail "Cannot initialize verified product manifest"

  while IFS= read -r relative; do
    [ -n "$relative" ] || continue
    path=$(path_from_relative "$relative") || fail "Cannot resolve product inventory path"
    [ ! -L "$path" ] || fail "Product source symlink is forbidden: $relative"
    if [ -d "$path" ]; then
      path_type="dir"
      desired_mode="755"
    elif [ -f "$path" ]; then
      path_type="file"
      desired_mode="644"
      if is_executable_file "$relative"; then
        desired_mode="755"
      fi
    else
      fail "Product inventory path disappeared or changed type: $relative"
    fi

    current_mode=$(stat_mode "$path") || fail "Cannot read mode: $relative"
    if [ "$mode_policy" = "safe" ] && unsafe_write_mode "$current_mode"; then
      fail "Group/world-writable product path is forbidden: $relative mode=$current_mode"
    fi
    check_owner_group "$path"
    path_identity=$(stat_identity "$path") || fail "Cannot read filesystem identity: $relative"
    check_path_acl "$path" "$relative" "$acl_policy"
    printf '%s\t%s\t%s\t%s\n' "$desired_mode" "$path_type" "$path_identity" "$relative" >>"$manifest_file" ||
      fail "Cannot append verified product manifest"
  done <"$inventory_file"

  [ -s "$manifest_file" ] || fail "Verified product manifest is empty"
}

verify_manifest_stable() {
  manifest_file="$1"
  tab=$(printf '\t')
  while IFS="$tab" read -r desired_mode path_type expected_identity relative; do
    [ -n "$relative" ] || continue
    path=$(path_from_relative "$relative") || fail "Cannot resolve verified product path"
    [ ! -L "$path" ] || fail "Product source symlink is forbidden: $relative"
    case "$path_type" in
      dir) [ -d "$path" ] || fail "Verified product directory changed type: $relative" ;;
      file) [ -f "$path" ] || fail "Verified product file changed type: $relative" ;;
      *) fail "Verified product manifest has an invalid path type: $relative" ;;
    esac
    check_owner_group "$path"
    current_identity=$(stat_identity "$path") || fail "Cannot read filesystem identity: $relative"
    [ "$current_identity" = "$expected_identity" ] ||
      fail "Product inventory identity changed before permission mutation: $relative"
  done <"$manifest_file"
}

apply_verified_manifest() {
  manifest_file="$1"
  tab=$(printf '\t')
  for apply_type in dir file; do
    while IFS="$tab" read -r desired_mode path_type expected_identity relative; do
      [ -n "$relative" ] || continue
      [ "$path_type" = "$apply_type" ] || continue
      path=$(path_from_relative "$relative") || fail "Cannot resolve product path for permission mutation"
      chmod "$desired_mode" "$path" || fail "Cannot set product mode: $relative"
      # GNU chmod intentionally preserves setuid/setgid on directories when a
      # three-digit numeric mode is used. Clear all special bits explicitly;
      # BusyBox and BSD-compatible chmod accept the same symbolic operation.
      chmod u-s,g-s,o-t "$path" || fail "Cannot clear product special mode bits: $relative"
    done <"$manifest_file"
  done
}

verify_manifest_strict() {
  manifest_file="$1"
  tab=$(printf '\t')
  while IFS="$tab" read -r desired_mode path_type expected_identity relative; do
    [ -n "$relative" ] || continue
    path=$(path_from_relative "$relative") || fail "Cannot resolve product path for strict verification"
    [ ! -L "$path" ] || fail "Product source symlink is forbidden: $relative"
    case "$path_type" in
      dir) [ -d "$path" ] || fail "Product directory changed type after permission mutation: $relative" ;;
      file) [ -f "$path" ] || fail "Product file changed type after permission mutation: $relative" ;;
      *) fail "Verified product manifest has an invalid path type: $relative" ;;
    esac
    check_owner_group "$path"
    current_identity=$(stat_identity "$path") || fail "Cannot read filesystem identity: $relative"
    [ "$current_identity" = "$expected_identity" ] ||
      fail "Product inventory identity changed during permission mutation: $relative"
    current_mode=$(stat_mode "$path") || fail "Cannot read mode: $relative"
    [ "$current_mode" = "$desired_mode" ] ||
      fail "Normalized product mode mismatch: $relative mode=$current_mode expected=$desired_mode"
    check_path_acl "$path" "$relative" strict
  done <"$manifest_file"
}

acl_index=0
PREFLIGHT_INVENTORY="$GATE_TMP_DIR/preflight.inventory"
PREFLIGHT_MANIFEST="$GATE_TMP_DIR/preflight.manifest"
build_inventory "$PREFLIGHT_INVENTORY" preflight

if [ "$ACTION" = "fix" ]; then
  # The complete source tree is enumerated and inspected before the first chmod.
  # Base group/other write bits are repairable POSIX modes; writable named or
  # default ACL entries remain fail-closed because chmod cannot safely remove them.
  prepare_verified_manifest "$PREFLIGHT_INVENTORY" "$PREFLIGHT_MANIFEST" any fix-preflight
  verify_manifest_stable "$PREFLIGHT_MANIFEST"
  apply_verified_manifest "$PREFLIGHT_MANIFEST"

  POST_INVENTORY="$GATE_TMP_DIR/post.inventory"
  build_inventory "$POST_INVENTORY" post
  if ! cmp -s "$PREFLIGHT_INVENTORY" "$POST_INVENTORY"; then
    fail "Product inventory changed during permission mutation"
  fi
  verify_manifest_strict "$PREFLIGHT_MANIFEST"
else
  prepare_verified_manifest "$PREFLIGHT_INVENTORY" "$PREFLIGHT_MANIFEST" safe strict
  check_exact_critical_modes
fi

printf 'permission_gate=PASS\n'
printf 'permission_action=%s\n' "$ACTION"
printf 'permission_app_dir=%s\n' "$APP_DIR"
printf 'permission_owner_group=%s\n' "$ROOT_OWNER_GROUP"
printf 'permission_contract=%s\n' "$CONTRACT"
