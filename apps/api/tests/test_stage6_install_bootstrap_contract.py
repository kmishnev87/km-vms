import os
import shutil
import subprocess
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

    previews = nginx.split("location /previews/", 1)[1].split("}", 1)[0]
    assert "set $api_upstream api:8000;" in nginx
    assert "proxy_pass http://$api_upstream;" in previews
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
    assert "setup_completed_now" in helper
    assert "initial_runtime_verified" in helper
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
    assert (
        "data/update-runtime/active/scripts/"
        "km-vms-setup-activation-helper.sh"
        in helper_section
    )
    assert (
        "scripts/km-vms-setup-activation-helper.sh"
        in helper_section
    )
    assert "SOURCE_CONTAINER_DIR" in helper
    assert (
        'sh "$SOURCE_CONTAINER_DIR/scripts/'
        'km-vms-storage-discovery.sh"'
        in helper
    )
    assert (
        'sh "$SOURCE_CONTAINER_DIR/scripts/'
        'km-vms-storage-root-cleanup.sh"'
        in helper
    )
    assert (
        'sh "$SOURCE_CONTAINER_DIR/scripts/'
        'km-vms-storage-candidate-validate.sh"'
        in helper
    )
    assert (
        'sh "$SOURCE_CONTAINER_DIR/scripts/'
        'km-vms-storage-apply.sh"'
        in helper
    )
    assert (
        'sh "$SOURCE_DIR/scripts/km-vms-restart.sh"'
        in helper
    )
    assert "sh /host-app/scripts/km-vms-" not in helper
    assert "/var/run/docker.sock:/var/run/docker.sock" in update_helper_section
    assert "ports:" not in update_helper_section
    assert "km-vms-update-helper.py" in update_helper_section
    assert "working_dir: /host-app" in update_helper_section
    assert "KM_VMS_UPDATE_APP_DIR: /host-app" in update_helper_section
    assert "KM_VMS_UPDATE_HOST_APP_DIR" in update_helper_section
    assert "- ${KM_VMS_HOST_APP_DIR:-.}:/host-app" in update_helper_section
    assert (
        "- ${KM_VMS_HOST_APP_DIR:-.}:${KM_VMS_HOST_APP_DIR:-/host-app}"
        in update_helper_section
    )
    assert "python3 is required for the release-slot layout foundation" not in script
    assert '"$APP_DIR/data/update-runtime/slots"' in script
    assert "read_control_value" in helper
    assert "read_control_value" in storage_apply
    assert "read_control_value" in restart
    assert 'namespace_path="$fs_selected_path/kmvms/recordings"' in storage_apply
    assert 'mkdir -p "$namespace_path"' in storage_apply
    assert '[ ! -L "$fs_selected_path/kmvms" ]' in storage_apply
    assert '[ ! -L "$namespace_path" ]' in storage_apply
    assert "MAX_INITIAL_ACTIVATION_ATTEMPTS=3" in helper
    assert "initial_configuration_published" in helper
    assert "--restore-initial-recovery" in helper
    assert "--cleanup-initial-recovery" in helper
    assert 'storage_apply_mode="--initial-setup"' in helper
    assert 'restart_mode="--initial-setup"' in helper
    assert "converge-runtime-files" in restart
    assert "prove-runtime" in restart
    assert ".storage-activation-recovery" in storage_apply
    assert "manifest.previous" in storage_apply
    assert "override.previous" in storage_apply
    assert "restore_initial_recovery_set" in storage_apply
    for old_json_sed in (
        '"selected_host_path"[[:space:]]*:',
        '"selected_mount_path"[[:space:]]*:',
        '"folder_name"[[:space:]]*:',
        '"request_id"[[:space:]]*:',
    ):
        assert old_json_sed not in helper
        assert old_json_sed not in storage_apply


def test_initial_storage_recovery_restores_complete_configuration_set(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    control = app_dir / "data" / "install-control"
    request_id = "setup-storage-recovery-test"
    recovery = control / ".storage-activation-recovery" / request_id
    recovery.mkdir(parents=True)
    app_dir.joinpath(".env").write_text(
        "SURVEILLANCE_ROOT=/new/archive\nSECRET=never-print\n",
        encoding="utf-8",
    )
    control.joinpath("archive-roots-runtime.json").write_text(
        "new-manifest\n",
        encoding="utf-8",
    )
    control.joinpath("docker-compose.archive-roots.yml").write_text(
        "new-override\n",
        encoding="utf-8",
    )
    recovery.joinpath("env.previous").write_text(
        "SURVEILLANCE_ROOT=/old/archive\nSECRET=never-print\n",
        encoding="utf-8",
    )
    recovery.joinpath("manifest.previous").write_text(
        "old-manifest\n",
        encoding="utf-8",
    )
    recovery.joinpath("override.previous").write_text(
        "old-override\n",
        encoding="utf-8",
    )
    recovery.joinpath("request.control").write_text(
        f"schema_version=1\nrequest_id={request_id}\nselected_host_path=/old/archive\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "sh",
            str(ROOT / "scripts" / "km-vms-storage-apply.sh"),
            "--app-dir",
            str(app_dir),
            "--restore-initial-recovery",
            request_id,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert app_dir.joinpath(".env").read_text(encoding="utf-8").startswith(
        "SURVEILLANCE_ROOT=/old/archive\n"
    )
    assert control.joinpath("archive-roots-runtime.json").read_text(
        encoding="utf-8"
    ) == "old-manifest\n"
    assert control.joinpath("docker-compose.archive-roots.yml").read_text(
        encoding="utf-8"
    ) == "old-override\n"
    assert not recovery.exists()
    assert "SECRET" not in result.stdout
    assert "SECRET" not in result.stderr


def test_initial_storage_recovery_resumes_after_each_partial_replace_failure(
    tmp_path: Path,
) -> None:
    real_mv = shutil.which("mv")
    assert real_mv
    cases = (
        ("env", ".env", ("new-env\n", "new-manifest\n", "new-override\n")),
        (
            "manifest",
            "data/install-control/archive-roots-runtime.json",
            ("old-env\n", "new-manifest\n", "new-override\n"),
        ),
        (
            "override",
            "data/install-control/docker-compose.archive-roots.yml",
            ("old-env\n", "old-manifest\n", "new-override\n"),
        ),
    )

    for label, failed_relative, expected_after_failure in cases:
        app_dir = tmp_path / label / "app"
        control = app_dir / "data" / "install-control"
        request_id = f"setup-storage-recovery-{label}"
        recovery = control / ".storage-activation-recovery" / request_id
        recovery.mkdir(parents=True)
        app_dir.joinpath(".env").write_text("new-env\n", encoding="utf-8")
        control.joinpath("archive-roots-runtime.json").write_text(
            "new-manifest\n", encoding="utf-8"
        )
        control.joinpath("docker-compose.archive-roots.yml").write_text(
            "new-override\n", encoding="utf-8"
        )
        recovery.joinpath("env.previous").write_text("old-env\n", encoding="utf-8")
        recovery.joinpath("manifest.previous").write_text(
            "old-manifest\n", encoding="utf-8"
        )
        recovery.joinpath("override.previous").write_text(
            "old-override\n", encoding="utf-8"
        )
        recovery.joinpath("request.control").write_text(
            f"schema_version=1\nrequest_id={request_id}\nselected_host_path=/old/archive\n",
            encoding="utf-8",
        )

        shim_dir = tmp_path / label / "bin"
        shim_dir.mkdir()
        mv_shim = shim_dir / "mv"
        mv_shim.write_text(
            "#!/bin/sh\n"
            'if [ "$#" -eq 2 ] && [ "$2" = "$KM_VMS_TEST_FAIL_TARGET" ]; then\n'
            "  exit 73\n"
            "fi\n"
            f'exec "{real_mv}" "$@"\n',
            encoding="utf-8",
        )
        mv_shim.chmod(0o755)
        failed_env = os.environ.copy()
        failed_env["PATH"] = f"{shim_dir}{os.pathsep}{failed_env['PATH']}"
        failed_env["KM_VMS_TEST_FAIL_TARGET"] = str(app_dir / failed_relative)

        first = subprocess.run(
            [
                "sh",
                str(ROOT / "scripts" / "km-vms-storage-apply.sh"),
                "--app-dir",
                str(app_dir),
                "--restore-initial-recovery",
                request_id,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=failed_env,
        )

        assert first.returncode != 0, label
        assert recovery.is_dir(), label
        assert (
            app_dir.joinpath(".env").read_text(encoding="utf-8"),
            control.joinpath("archive-roots-runtime.json").read_text(encoding="utf-8"),
            control.joinpath("docker-compose.archive-roots.yml").read_text(encoding="utf-8"),
        ) == expected_after_failure, label

        second = subprocess.run(
            [
                "sh",
                str(ROOT / "scripts" / "km-vms-storage-apply.sh"),
                "--app-dir",
                str(app_dir),
                "--restore-initial-recovery",
                request_id,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        assert second.returncode == 0, f"{label}: {second.stderr}"
        assert app_dir.joinpath(".env").read_text(encoding="utf-8") == "old-env\n"
        assert control.joinpath("archive-roots-runtime.json").read_text(
            encoding="utf-8"
        ) == "old-manifest\n"
        assert control.joinpath("docker-compose.archive-roots.yml").read_text(
            encoding="utf-8"
        ) == "old-override\n"
        assert not recovery.exists(), label


def test_initial_storage_recovery_failure_stays_nonterminal_until_converged() -> None:
    helper = read("scripts/km-vms-setup-activation-helper.sh")

    assert '"status": "activation_in_progress"' in helper
    assert '"phase": "restoring_previous_configuration"' in helper
    assert helper.count("continue_initial_recovery ") == 3
    recovery_function = helper.split("continue_initial_recovery() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert '"processing"' in recovery_function
    assert '"failed"' not in recovery_function
