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

km_vms_compose_bound_cmd() {
  if [ -n "${KM_VMS_COMPOSE_SLOT_ID:-}" ]; then
    (
      KM_VMS_RELEASE_IMAGE_TAG="$KM_VMS_COMPOSE_SLOT_ID"
      export KM_VMS_RELEASE_IMAGE_TAG
      km_vms_compose_cmd "$@"
    )
    return $?
  fi
  km_vms_compose_cmd "$@"
}

km_vms_resolve_product_source() {
  stable_app_dir="$1"
  [ -n "$stable_app_dir" ] ||
    km_vms_compose_fail "Stable KM VMS app directory is required."
  case "$stable_app_dir" in
    /*) ;;
    *) km_vms_compose_fail "Stable KM VMS app directory must be absolute." ;;
  esac
  active_pointer="$stable_app_dir/data/update-runtime/active"
  if [ ! -e "$active_pointer" ] && [ ! -L "$active_pointer" ]; then
    km_vms_compose_fail "Canonical active KM VMS release pointer is missing."
  fi
  [ -L "$active_pointer" ] ||
    km_vms_compose_fail "Active KM VMS release pointer is not a symlink."
  km_vms_command_exists readlink ||
    km_vms_compose_fail "readlink is required to resolve the active KM VMS release slot."
  pointer_target=$(readlink "$active_pointer") ||
    km_vms_compose_fail "Active KM VMS release pointer cannot be read."
  printf '%s\n' "$pointer_target" |
    grep -Eq '^slots/(release-[0-9a-f]{40}|adopted-[0-9a-f]{64}|initial-[0-9a-f]{64})/source$' ||
    km_vms_compose_fail "Active KM VMS release pointer is outside its bounded slot layout."
  slot_id=$(printf '%s\n' "$pointer_target" | cut -d/ -f2)
  slot_root="$stable_app_dir/data/update-runtime/slots/$slot_id"
  resolved="$slot_root/source"
  [ -d "$slot_root" ] && [ ! -L "$slot_root" ] &&
    [ -d "$resolved" ] && [ ! -L "$resolved" ] &&
    [ -f "$slot_root/slot-manifest.json" ] &&
    [ ! -L "$slot_root/slot-manifest.json" ] ||
    km_vms_compose_fail "Resolved KM VMS release slot is incomplete or unsafe."
  [ -f "$resolved/docker-compose.yml" ] ||
    km_vms_compose_fail "Resolved KM VMS release source is incomplete."
  printf '%s\n' "$resolved"
}

km_vms_lifecycle_override() {
  stable_app_dir="$1"
  bundle="$stable_app_dir/data/update-runtime/bootstrap/current"
  if [ "${KM_VMS_ALLOW_PREBOOTSTRAP_COMPOSE:-0}" = "1" ] &&
     [ ! -e "$bundle" ] && [ ! -L "$bundle" ]; then
    printf '\n'
    return 0
  fi
  [ -L "$bundle" ] && [ -d "$bundle" ] ||
    km_vms_compose_fail "Stable KM VMS bootstrap bundle is unavailable."
  [ -f "$bundle/bootstrap-files.sha256" ] &&
    [ ! -L "$bundle/bootstrap-files.sha256" ] ||
    km_vms_compose_fail "Stable KM VMS bootstrap checksums are unavailable."
  km_vms_command_exists sha256sum ||
    km_vms_compose_fail "sha256sum is required to verify the stable KM VMS bootstrap."
  (cd "$bundle" && sha256sum -c bootstrap-files.sha256 >/dev/null 2>&1) ||
    km_vms_compose_fail "Stable KM VMS bootstrap digest verification failed."
  lifecycle="$bundle/docker-compose.lifecycle.yml"
  [ -f "$lifecycle" ] && [ ! -L "$lifecycle" ] ||
    km_vms_compose_fail "Stable KM VMS lifecycle override is unavailable."
  printf '%s\n' "$lifecycle"
}

km_vms_slot_image_override() {
  stable_app_dir="$1"
  bootstrap="$stable_app_dir/data/update-runtime/bootstrap/current/km-vms-bootstrap.py"
  [ -f "$bootstrap" ] && [ ! -L "$bootstrap" ] ||
    km_vms_compose_fail "Stable KM VMS bootstrap authority is unavailable."
  project_name="${KM_VMS_PROJECT_NAME:-${PROJECT_NAME:-}}"
  if [ -z "$project_name" ]; then
    project_name=$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$stable_app_dir/.env" | tail -n 1)
  fi
  printf '%s\n' "$project_name" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$' ||
    km_vms_compose_fail "Stable KM VMS Compose project identity is invalid."
  if km_vms_command_exists python3; then
    image_override=$(python3 -B "$bootstrap" image-override-path \
      --app-dir "$stable_app_dir" --project-name "$project_name") ||
      km_vms_compose_fail "Immutable slot image override could not be materialized."
  else
    km_vms_command_exists docker ||
      km_vms_compose_fail "Python or Docker is required to materialize immutable slot images."
    helper_ids=$(docker ps -q \
      --filter "label=com.docker.compose.project=$project_name" \
      --filter "label=com.docker.compose.service=update-helper")
    [ "$(printf '%s\n' "$helper_ids" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] ||
      km_vms_compose_fail "Canonical update-helper owner is unavailable."
    helper_id=$(printf '%s\n' "$helper_ids" | sed -n '1p')
    image_override=$(docker exec "$helper_id" python3 -B \
      /host-app/data/update-runtime/bootstrap/current/km-vms-bootstrap.py \
      image-override-path --app-dir /host-app --project-name "$project_name") ||
      km_vms_compose_fail "Immutable slot image override could not be materialized."
  fi
  case "$image_override" in
    "$stable_app_dir"/data/update-runtime/derived-compose/*-images.yml) ;;
    *) km_vms_compose_fail "Immutable slot image override escaped stable runtime." ;;
  esac
  [ -f "$image_override" ] && [ ! -L "$image_override" ] ||
    km_vms_compose_fail "Immutable slot image override is unsafe."
  printf '%s\n' "$image_override"
}

km_vms_compose_for_source() {
  stable_app_dir="$1"
  source_dir="$2"
  shift 2
  [ -f "$stable_app_dir/.env" ] ||
    km_vms_compose_fail "Stable KM VMS .env is unavailable."
  [ -f "$source_dir/docker-compose.yml" ] ||
    km_vms_compose_fail "KM VMS product source has no docker-compose.yml."
  archive_override="$stable_app_dir/data/install-control/docker-compose.archive-roots.yml"
  lifecycle_override=$(km_vms_lifecycle_override "$stable_app_dir")
  slot_runtime_override=""
  slot_image_override=""
  KM_VMS_COMPOSE_SLOT_ID=""
  case "$source_dir" in
    "$stable_app_dir"/data/update-runtime/slots/*/source)
      KM_VMS_COMPOSE_SLOT_ID=${source_dir#"$stable_app_dir/data/update-runtime/slots/"}
      KM_VMS_COMPOSE_SLOT_ID=${KM_VMS_COMPOSE_SLOT_ID%%/*}
      printf '%s\n' "$KM_VMS_COMPOSE_SLOT_ID" |
        grep -Eq '^(release-[0-9a-f]{40}|adopted-[0-9a-f]{64}|initial-[0-9a-f]{64})$' ||
        km_vms_compose_fail "KM VMS release-slot image identity is invalid."
      possible_runtime_override="$(dirname "$source_dir")/docker-compose.runtime-override.yml"
      if [ -e "$possible_runtime_override" ] || [ -L "$possible_runtime_override" ]; then
        [ -f "$possible_runtime_override" ] && [ ! -L "$possible_runtime_override" ] ||
          km_vms_compose_fail "Release-slot runtime Compose override is unsafe."
        slot_runtime_override="$possible_runtime_override"
      fi
      slot_image_override=$(km_vms_slot_image_override "$stable_app_dir")
      ;;
  esac
  if [ -n "$slot_image_override" ] && [ -n "$slot_runtime_override" ] && [ -f "$archive_override" ] && [ -n "$lifecycle_override" ]; then
    km_vms_compose_bound_cmd \
      --env-file "$stable_app_dir/.env" \
      --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" \
      -f "$slot_runtime_override" \
      -f "$slot_image_override" \
      -f "$archive_override" \
      -f "$lifecycle_override" \
      "$@"
  elif [ -n "$slot_image_override" ] && [ -n "$slot_runtime_override" ] && [ -n "$lifecycle_override" ]; then
    km_vms_compose_bound_cmd \
      --env-file "$stable_app_dir/.env" \
      --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" \
      -f "$slot_runtime_override" \
      -f "$slot_image_override" \
      -f "$lifecycle_override" \
      "$@"
  elif [ -n "$slot_image_override" ] && [ -f "$archive_override" ] && [ -n "$lifecycle_override" ]; then
    km_vms_compose_bound_cmd \
      --env-file "$stable_app_dir/.env" \
      --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" \
      -f "$slot_image_override" \
      -f "$archive_override" \
      -f "$lifecycle_override" \
      "$@"
  elif [ -n "$slot_image_override" ] && [ -n "$lifecycle_override" ]; then
    km_vms_compose_bound_cmd \
      --env-file "$stable_app_dir/.env" \
      --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" \
      -f "$slot_image_override" \
      -f "$lifecycle_override" \
      "$@"
  elif [ -n "$slot_runtime_override" ] && [ -f "$archive_override" ] && [ -n "$lifecycle_override" ]; then
    km_vms_compose_bound_cmd \
      --env-file "$stable_app_dir/.env" \
      --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" \
      -f "$slot_runtime_override" \
      -f "$archive_override" \
      -f "$lifecycle_override" \
      "$@"
  elif [ -n "$slot_runtime_override" ] && [ -n "$lifecycle_override" ]; then
    km_vms_compose_bound_cmd \
      --env-file "$stable_app_dir/.env" \
      --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" \
      -f "$slot_runtime_override" \
      -f "$lifecycle_override" \
      "$@"
  elif [ -f "$archive_override" ] && [ -n "$lifecycle_override" ]; then
    km_vms_compose_bound_cmd \
      --env-file "$stable_app_dir/.env" \
      --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" \
      -f "$archive_override" \
      -f "$lifecycle_override" \
      "$@"
  elif [ -n "$lifecycle_override" ]; then
    km_vms_compose_bound_cmd \
      --env-file "$stable_app_dir/.env" \
      --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" \
      -f "$lifecycle_override" \
      "$@"
  elif [ -n "$slot_runtime_override" ] && [ -f "$archive_override" ]; then
    km_vms_compose_bound_cmd --env-file "$stable_app_dir/.env" --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" -f "$slot_runtime_override" -f "$archive_override" "$@"
  elif [ -n "$slot_runtime_override" ]; then
    km_vms_compose_bound_cmd --env-file "$stable_app_dir/.env" --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" -f "$slot_runtime_override" "$@"
  elif [ -f "$archive_override" ]; then
    km_vms_compose_bound_cmd --env-file "$stable_app_dir/.env" --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" -f "$archive_override" "$@"
  else
    km_vms_compose_bound_cmd --env-file "$stable_app_dir/.env" --project-directory "$source_dir" \
      -f "$source_dir/docker-compose.yml" "$@"
  fi
}
