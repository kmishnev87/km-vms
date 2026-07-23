from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_install_script_exposes_private_github_no_git_contract():
    script = read("scripts/install.sh")
    helper = read("scripts/km-vms-compose-common.sh")

    for required in (
        "--github-repo <repo>",
        "--github-private",
        "--github-token-file",
        "--github-token-env",
        "https://api.github.com/repos/$GITHUB_REPO/tarball/$BRANCH",
        ".km-vms-source.json",
    ):
        assert required in script

    for required in (
        "/Volume*/@apps/DockerEngine/dockerd/bin/docker",
        "/Volume*/@apps/DockerEngine/dockerd/bin/docker-compose",
        "/var/packages/ContainerManager/target/usr/bin/docker",
    ):
        assert required in helper


def test_install_docs_include_public_pipe_install_without_token_requirement():
    docs = read("docs/INSTALL.md")

    assert "curl -fsSL https://raw.githubusercontent.com/kmishnev87/km-vms/main/scripts/install.sh | sh -s --" in docs
    public_install_section = docs.split("Public one-line install:", 1)[1].split("Private GitHub repository install without `git`:", 1)[0]
    assert "--github-token" not in public_install_section
    assert "Private GitHub repository install without `git`" in docs
    assert "The operator does not run `km-vms-storage-apply.sh` or `km-vms-restart.sh` manually" in docs
    assert "setup-complete.json" in docs


def test_install_script_creates_nginx_readable_preview_root():
    script = read("scripts/install.sh")

    assert 'mkdir -p "$archive_dir" "$backup_dir" "$APP_DIR/data/previews" "$APP_DIR/data/exports" "$APP_DIR/data/install-control"' in script
    assert 'chmod 755 "$APP_DIR/data/previews" 2>/dev/null || true' in script


def test_previews_are_proxied_to_api_not_read_directly_by_nginx():
    compose = read("docker-compose.yml")
    nginx = read("deploy/nginx/default.conf")
    nginx_section = compose.split("  nginx:", 1)[1].split("networks:", 1)[0]

    assert "proxy_pass http://api:8000/previews/;" in nginx
    assert "alias /var/www/previews/;" not in nginx
    assert "./data/previews:/var/www/previews" not in nginx_section


def test_setup_activation_helper_is_bounded_runtime_storage_helper():
    helper = read("scripts/km-vms-setup-activation-helper.sh")
    compose = read("docker-compose.yml")
    script = read("scripts/install.sh")
    storage_apply = read("scripts/km-vms-storage-apply.sh")
    restart = read("scripts/km-vms-restart.sh")

    assert "storage-activation-request.json" in helper
    assert "storage-activation-request.control" in helper
    assert "storage-selection.control" in helper
    assert "setup-complete.json" in helper
    assert "/system/status" in helper
    assert '"initialized"[[:space:]]*:[[:space:]]*true' in helper
    assert "KM_VMS_HOST_APP_DIR" in compose
    assert "KMVMS_UPDATE_MANIFEST_PATH" in compose
    assert "KMVMS_UPDATE_CHANNEL_ID" in compose
    assert "KM_VMS_HOST_APP_DIR" in script
    assert 'setup_already_completed && [ "$status" != "requested" ]' in helper
    assert "km-vms-storage-apply.sh" in helper
    assert "km-vms-restart.sh" in helper
    assert "docker run --rm" in helper
    assert "$selected_mount:/selected-root" in helper
    assert "KM_VMS_SELECTED_PATH_CONTAINER=/selected-root/$folder_name" in helper
    assert "down -v" not in helper
    assert "docker system prune" not in helper
    assert "setup-helper:" in compose
    assert compose.count("/var/run/docker.sock:/var/run/docker.sock") == 3
    bootstrap_section = compose.split("  update-helper-bootstrap:", 1)[1].split("  api:", 1)[0]
    api_section = compose.split("  api:", 1)[1].split("  setup-helper:", 1)[0]
    helper_section = compose.split("  setup-helper:", 1)[1].split("  update-helper:", 1)[0]
    update_helper_section = compose.split("  update-helper:", 1)[1].split("  recorder:", 1)[0]
    assert "/var/run/docker.sock:/var/run/docker.sock" in bootstrap_section
    assert 'restart: "no"' in bootstrap_section
    assert "km-vms-update-helper-bridge.py" in bootstrap_section
    assert "network_mode: none" in bootstrap_section
    assert "KM_VMS_BOOTSTRAP_HELPER_IMAGE" in bootstrap_section
    assert "update-helper-bootstrap" in api_section
    assert "service_completed_successfully" in api_section
    assert "/var/run/docker.sock:/var/run/docker.sock" not in api_section
    assert "restart: unless-stopped" in helper_section
    assert "/var/run/docker.sock:/var/run/docker.sock" in helper_section
    assert "/var/run/docker.sock:/var/run/docker.sock" in update_helper_section
    assert "ports:" not in update_helper_section
    assert "km-vms-update-helper.py" in update_helper_section
    assert "working_dir: /host-app" in update_helper_section
    assert "KM_VMS_UPDATE_APP_DIR: /host-app" in update_helper_section
    assert "KM_VMS_UPDATE_HOST_APP_DIR" in update_helper_section
    assert "- ./:/host-app" in update_helper_section
    assert "- ./:${KM_VMS_HOST_APP_DIR:-/host-app}" in update_helper_section
    assert "read_control_value" in helper
    assert "read_control_value" in storage_apply
    assert "read_control_value" in restart
    assert 'namespace_path="$fs_selected_path/kmvms/recordings"' in storage_apply
    assert 'mkdir -p "$namespace_path"' in storage_apply
    assert '[ ! -L "$fs_selected_path/kmvms" ]' in storage_apply
    assert '[ ! -L "$namespace_path" ]' in storage_apply
    for old_json_sed in (
        '"selected_host_path"[[:space:]]*:',
        '"selected_mount_path"[[:space:]]*:',
        '"folder_name"[[:space:]]*:',
        '"request_id"[[:space:]]*:',
    ):
        assert old_json_sed not in helper
        assert old_json_sed not in storage_apply
