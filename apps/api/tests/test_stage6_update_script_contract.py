import os
import re
import subprocess
import tempfile
import importlib.util
import json
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _write_update_shell_fixture(tmp_path: Path, *, compose_function: str, commit: str = "b" * 40) -> tuple[Path, Path, Path]:
    script = read("scripts/update.sh")
    app = tmp_path / "app"
    source = tmp_path / "source"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for root in (app, source):
        (root / "apps/api").mkdir(parents=True)
        (root / "apps/web").mkdir(parents=True)
        (root / "deploy/nginx").mkdir(parents=True)
        (root / "scripts").mkdir(parents=True)
        (root / "docs").mkdir(parents=True)
        (root / "release").mkdir(parents=True)
        (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (root / "docker-compose.pytest.yml").write_text("services: {}\n", encoding="utf-8")
        (root / ".dockerignore").write_text("data\n", encoding="utf-8")
        (root / ".gitignore").write_text(".env\n", encoding="utf-8")
        (root / ".env.example").write_text("TZ=UTC\n", encoding="utf-8")
        (root / "deploy/nginx/default.conf").write_text("# nginx\n", encoding="utf-8")
        (root / "scripts/install.sh").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
        (root / "scripts/km-vms-compose-common.sh").write_text(compose_function + "\n", encoding="utf-8")
        (root / "docs/INSTALL.md").write_text("# install\n", encoding="utf-8")
        (root / "release/km-vms-release.json").write_text('{"schema_version":1,"version":"0.7.1"}\n', encoding="utf-8")
    (source / "scripts/update.sh").write_text(script, encoding="utf-8")
    (app / "scripts/update.sh").write_text(script, encoding="utf-8")
    (app / ".env").write_text("HTTP_PORT=18183\nCOMPOSE_PROJECT_NAME=kmvmsfixture\n", encoding="utf-8")
    (app / "data").mkdir()
    tarball = tmp_path / "source.tar.gz"
    subprocess.run(["tar", "-czf", str(tarball), "-C", str(source.parent), source.name], check=True)
    (bin_dir / "curl").write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                "case \"$*\" in */commits/main*) printf '{\\n  \"sha\": \"" + commit + "\"\\n}\\n'; exit 0 ;; esac",
                "for arg in \"$@\"; do",
                "  if [ \"$prev\" = \"-o\" ]; then cp '" + str(tarball) + "' \"$arg\"; exit 0; fi",
                "  prev=\"$arg\"",
                "done",
                "exit 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(bin_dir / "curl", 0o755)
    return app, source, bin_dir


def test_update_script_exists_and_exposes_terminal_contract():
    script_path = ROOT / "scripts" / "update.sh"
    script = script_path.read_text(encoding="utf-8")

    assert script_path.exists()
    help_result = subprocess.run(
        ["sh", str(script_path), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert "KM VMS terminal update" in help_result.stdout

    for required in (
        "--github-repo <repo>",
        "--branch <branch>",
        "--ref <ref>",
        "--github-private",
        "--github-token-file",
        "--github-token-env",
        "--yes",
        "--dry-run",
        "KM_VMS_GITHUB_REPO",
        "KM_VMS_BRANCH",
        "KM_VMS_GITHUB_PRIVATE",
        "KM_VMS_GITHUB_TOKEN",
        "KM_VMS_GITHUB_TOKEN_FILE",
        "KM_VMS_GITHUB_TOKEN_ENV",
        "KM_VMS_DOCKER_COMPOSE",
        "KM_VMS_PROJECT_NAME",
        "KM_VMS_YES",
    ):
        assert required in script


def test_update_script_reuses_compose_helper_and_github_tarball_without_git_requirement():
    script = read("scripts/update.sh")

    assert ". \"$APP_DIR/scripts/km-vms-compose-common.sh\"" in script
    assert "km_vms_detect_compose" in script
    assert "https://api.github.com/repos/$GITHUB_REPO/tarball/$BRANCH" in script
    assert "git clone" not in script
    assert "command_exists git" not in script
    assert "Authorization: Bearer" in script
    assert ".km-vms-source.json" in script
    assert ".km-vms-update.json" in script
    assert ".km-vms-release.json" in script
    assert "kmishnev87/km-vms" in script


def test_update_helper_failure_steps_match_failed_phase():
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_contract", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    def statuses(steps):
        return {step["name"]: step["status"] for step in steps}

    assert statuses(helper.failed_steps("preflight_failed"))["preflight"] == "failed"
    assert statuses(helper.failed_steps("apply_failed"))["overlay"] == "failed"
    assert statuses(helper.failed_steps("health_check_failed"))["health_check"] == "failed"
    assert statuses(helper.failed_steps("docker_build_failed"))["rebuilding"] == "failed"
    assert statuses(helper.failed_steps("compose_config_failed"))["compose_config"] == "failed"


def test_update_helper_requires_mounted_host_app_dir_for_compose(tmp_path):
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_host_dir_contract", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    helper.HOST_APP_DIR = None
    try:
        helper.compose_app_dir()
    except helper.HelperError as exc:
        assert exc.category == "helper_host_app_dir_missing"
    else:
        raise AssertionError("compose_app_dir accepted missing host app dir")

    app_dir = tmp_path / "app"
    (app_dir / "scripts").mkdir(parents=True)
    (app_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (app_dir / "scripts" / "update.sh").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    helper.HOST_APP_DIR = app_dir
    assert helper.compose_app_dir() == app_dir


def test_update_helper_classifies_health_check_failure_from_metadata(tmp_path):
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_failure_contract", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    (tmp_path / ".km-vms-update.json").write_text(json.dumps({"schema_version": 1, "failed_phase": "health_check"}), encoding="utf-8")
    exc = helper.classify_apply_failure(tmp_path, "health stderr")
    assert exc.category == "health_check_failed"
    assert "health stderr" in str(exc)

    (tmp_path / ".km-vms-update.json").write_text(json.dumps({"schema_version": 1, "failed_phase": "rebuild_recreate"}), encoding="utf-8")
    assert helper.classify_apply_failure(tmp_path, "apply stderr").category == "docker_build_failed"


def test_update_helper_uses_trusted_commit_as_apply_ref_and_verifies_metadata(tmp_path, monkeypatch):
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_pin_contract", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    app_dir = tmp_path / "app"
    control_dir = app_dir / "data" / "update-control"
    (app_dir / "scripts").mkdir(parents=True)
    control_dir.mkdir(parents=True)
    (app_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (app_dir / "scripts" / "update.sh").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    helper.HOST_APP_DIR = app_dir
    helper.STATUS_FILE = control_dir / "update-status.json"

    expected = "d" * 40
    commands = []

    def fake_run_child(command, request_arg, update_dir, env, **kwargs):
        commands.append(command)
        if "--dry-run" not in command:
            (app_dir / ".km-vms-update.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "commit_sha": expected,
                        "validation_summary": {
                            "release_identity_host_metadata_status": "complete",
                            "release_identity_api_metadata_status": "complete",
                            "release_identity_api_visible": True,
                            "release_identity_commit_verified": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (app_dir / ".km-vms-source.json").write_text(json.dumps({"schema_version": 1, "commit_sha": expected}), encoding="utf-8")
            (app_dir / ".km-vms-release.json").write_text(json.dumps({"schema_version": 1, "commit_sha": expected, "metadata_status": "complete"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(helper, "run_child_with_progress", fake_run_child)
    request = {
        "schema_version": 1,
        "request_id": "stage609-pin",
        "requested_at": "2026-06-19T00:00:00Z",
        "intent": "apply_update",
        "confirmed": True,
        "source": {
            "kind": "trusted_manifest",
            "source_type": "github_tarball",
            "repo": "owner/repo",
            "ref": "main",
            "commit": expected,
            "apply_ref": expected,
        },
    }

    assert helper.run_update(request) == 0
    assert commands[0][:6] == ["sh", "scripts/update.sh", "--github-repo", "owner/repo", "--branch", expected]
    assert "main" not in commands[0]
    status = json.loads((control_dir / "update-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert status["commit_verified"] is True
    assert status["installed_commit"] == expected
    assert status["release_identity"] == {
        "host_metadata_status": "complete",
        "api_metadata_status": "complete",
        "api_visible": True,
        "commit_verified": True,
    }


def test_update_helper_uses_container_compose_override_not_host_only_path(tmp_path, monkeypatch):
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_compose_env_contract", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    app_dir = tmp_path / "app"
    control_dir = app_dir / "data" / "update-control"
    (app_dir / "scripts").mkdir(parents=True)
    control_dir.mkdir(parents=True)
    (app_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (app_dir / "scripts" / "update.sh").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    helper.HOST_APP_DIR = app_dir
    helper.STATUS_FILE = control_dir / "update-status.json"
    helper.PROGRESS_FILE = control_dir / "update-progress.json"

    expected = "e" * 40
    seen_compose_values = []
    monkeypatch.setenv("KM_VMS_DOCKER_COMPOSE", "/Volume1/@apps/DockerEngine/dockerd/bin/docker-compose")
    monkeypatch.setenv("KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE", "docker-compose")

    def fake_run_child(command, request_arg, update_dir, env, **kwargs):
        seen_compose_values.append(env.get("KM_VMS_DOCKER_COMPOSE"))
        if "--dry-run" not in command:
            (app_dir / ".km-vms-update.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "commit_sha": expected,
                        "validation_summary": {
                            "release_identity_host_metadata_status": "complete",
                            "release_identity_api_metadata_status": "complete",
                            "release_identity_api_visible": True,
                            "release_identity_commit_verified": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (app_dir / ".km-vms-source.json").write_text(json.dumps({"schema_version": 1, "commit_sha": expected}), encoding="utf-8")
            (app_dir / ".km-vms-release.json").write_text(json.dumps({"schema_version": 1, "commit_sha": expected, "metadata_status": "complete"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(helper, "run_child_with_progress", fake_run_child)
    request = {
        "schema_version": 1,
        "request_id": "stage651-compose-env",
        "requested_at": "2026-07-06T00:00:00Z",
        "intent": "apply_update",
        "confirmed": True,
        "source": {
            "kind": "trusted_manifest",
            "source_type": "github_tarball",
            "repo": "owner/repo",
            "ref": expected,
            "commit": expected,
            "apply_ref": expected,
        },
    }

    assert helper.run_update(request) == 0
    assert seen_compose_values == ["docker-compose", "docker-compose"]


def test_update_helper_rejects_commit_mismatch_after_success(tmp_path):
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_commit_mismatch_contract", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    expected = "e" * 40
    installed = "f" * 40
    (tmp_path / ".km-vms-update.json").write_text(json.dumps({"schema_version": 1, "status": "success", "commit_sha": installed}), encoding="utf-8")
    try:
        helper.verify_installed_commit(tmp_path, expected)
    except helper.HelperError as exc:
        assert exc.category == "commit_mismatch"
        assert exc.phase == "commit_verification"
    else:
        raise AssertionError("commit mismatch was accepted")


def test_update_script_forbids_destructive_runtime_data_and_docker_actions():
    script = read("scripts/update.sh")
    compact = re.sub(r"\s+", " ", script)

    forbidden = (
        "down -v",
        "docker system prune",
        "docker volume rm",
        "docker volume prune",
        "volume rm",
        "rm -rf data",
        'rm -rf "$APP_DIR/data"',
        "POSTGRES_PASSWORD=",
        "JWT_SECRET=",
        "ENCRYPTION_KEY=",
        "ADMIN_PASSWORD=",
    )
    for pattern in forbidden:
        assert pattern not in compact

    assert 'compose_cmd --env-file "$APP_DIR/.env" up -d --build' in script
    assert 'KM_VMS_UPDATE_HELPER_MODE' in script
    assert 'up -d --build postgres redis api recorder web nginx' in script
    assert "health_attempts=60" in script
    assert 'health_targets="http://nginx/api/health http://api:8000/health"' in script
    assert "no app source changes are made before the overlay phase" in script
    assert "partially updated" in script


def test_update_overlay_allowlist_and_recursive_denylist_are_present():
    script = read("scripts/update.sh")

    for allowed in (
        "apps",
        "deploy",
        "docs",
        "release",
        "scripts",
        "docker-compose.yml",
        "docker-compose.pytest.yml",
        ".dockerignore",
        ".gitignore",
        ".env.example",
    ):
        assert allowed in script

    for denied in (
        "--exclude=./.git",
        "--exclude=*/.git",
        "--exclude=./.env",
        "--exclude=*/.env",
        "--exclude=./.env.*",
        "--exclude=*/.env.*",
        "--exclude=./data",
        "--exclude=*/data",
        "--exclude=./node_modules",
        "--exclude=*/node_modules",
        "--exclude=./.next",
        "--exclude=*/.next",
        "--exclude=./__pycache__",
        "--exclude=*/__pycache__",
        "--exclude=./.pytest_cache",
        "--exclude=*/.pytest_cache",
        "--exclude=./service-artifacts",
        "--exclude=*/service-artifacts",
        "--exclude=./.ssh",
        "--exclude=*/.ssh",
        "--exclude=id_rsa",
        "--exclude=*/id_rsa",
        "--exclude=id_ed25519",
        "--exclude=*/id_ed25519",
        "--exclude=*.pem",
        "--exclude=*.key",
        "--exclude=*.p12",
        "--exclude=*.pfx",
        "--exclude=*.token",
        "--exclude=*.secret",
        "--exclude=*token.txt",
        "--exclude=*Token.txt",
        "--exclude=*TOKEN.txt",
        "--exclude=*secret.txt",
        "--exclude=*Secret.txt",
        "--exclude=*SECRET.txt",
        "--exclude=*secret.json",
        "--exclude=*Secret.json",
        "--exclude=*SECRET.json",
        "--exclude=github-token*",
        "--exclude=auth-token*",
        "--exclude=authorization-token*",
        "--exclude=*credential*",
        "--exclude=*credentials*",
        "--exclude=*Credentials*",
        "--exclude=*CREDENTIALS*",
        "--exclude=*.dump",
        "--exclude=*.sql",
    ):
        assert denied in script

    assert "--exclude=*token*" not in script.lower()
    assert "--exclude=*secret*" not in script.lower()
    for allowed_product_asset_extension in ("*.png", "*.jpg", "*.jpeg", "*.mp4", "*.mkv", "*.ts", "*.m3u8"):
        assert f"--exclude={allowed_product_asset_extension}" not in script


def test_update_script_validates_source_app_paths_and_rejects_traversal_and_symlinks():
    script = read("scripts/update.sh")

    for marker in (
        "Source tree is missing docker-compose.yml",
        "Source tree is missing apps/api",
        "Source tree is missing apps/web",
        "Source tree is missing deploy/nginx/default.conf",
        "Source tree is missing scripts/install.sh",
        "Source tree is missing scripts/km-vms-compose-common.sh",
        "Source tree is missing docs/INSTALL.md",
        "Source tree is missing release/km-vms-release.json",
        "Source tree includes scripts/update.sh",
        "Source tree does not include scripts/update.sh",
        "Run update.sh from a KM VMS app directory",
        "Refusing dangerous app dir",
        "Refusing unsafe tarball entry path",
        "Source tree contains symlinks",
    ):
        assert marker in script


def test_update_script_locking_preservation_dry_run_and_metadata_contracts():
    script = read("scripts/update.sh")

    for required in (
        "data/update-control",
        "update.lock",
        "preflight_preservation",
        "postflight_preservation",
        'cksum "$APP_DIR/.env"',
        "PREFLIGHT_DATA_PATHS",
        "Preserved data path disappeared during update",
        "update.sh.preserved",
        "Failed to restore preserved update.sh",
        "Dry-run complete. No app source, .env, data, containers, or update metadata were modified.",
        "token mode: enabled via secure input path",
        "schema_version",
        "github_repo",
        "commit_sha",
        ".km-vms-release.json",
        "write_release_identity",
        "updated_paths_summary",
        "preserved_paths_summary",
        "error_message",
        '"implemented": false',
    ):
        assert required in script

    metadata_block = script.split("write_update_metadata()", 1)[1].split("fail()", 1)[0]
    assert "GITHUB_TOKEN" not in metadata_block
    assert "KM_VMS_GITHUB_TOKEN" not in metadata_block

    overlay_i = script.index("overlay_source\n")
    precompose_identity_i = script.index('write_release_identity "precompose"\n')
    compose_config_i = script.index("compose_config\n")
    final_identity_i = script.index("write_release_identity\n")
    assert overlay_i < precompose_identity_i < compose_config_i < final_identity_i
    assert "Release identity path is a directory and cannot be mounted by Docker Compose" in script


def test_update_script_dry_run_preserves_fixture_app_when_source_acquisition_is_stubbed():
    script = read("scripts/update.sh")
    with tempfile.TemporaryDirectory(prefix="kmvms_update_fixture_") as tmp:
        app = Path(tmp) / "app"
        source = Path(tmp) / "source"
        bin_dir = Path(tmp) / "bin"
        app.mkdir()
        source.mkdir()
        bin_dir.mkdir()
        for root in (app, source):
            (root / "apps/api").mkdir(parents=True)
            (root / "apps/web").mkdir(parents=True)
            (root / "deploy/nginx").mkdir(parents=True)
            (root / "scripts").mkdir(parents=True)
            (root / "docs").mkdir(parents=True)
            (root / "release").mkdir(parents=True)
            (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (root / "deploy/nginx/default.conf").write_text("# nginx\n", encoding="utf-8")
            (root / "scripts/install.sh").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
            (root / "scripts/km-vms-compose-common.sh").write_text(
                "\n".join(
                        [
                            "km_vms_detect_compose() { COMPOSE_KIND=stub; COMPOSE_BIN=stub; COMPOSE_SOURCE=stub; }",
                            "km_vms_compose_cmd() { if [ \"$1\" = \"--env-file\" ]; then shift 2; fi; if [ \"$1\" = \"config\" ] && [ ! -f .km-vms-release.json ]; then echo missing release identity before compose config >&2; return 42; fi; if [ \"$1\" = \"exec\" ]; then echo complete; return 0; fi; :; }",
                        ]
                    )
                + "\n",
                encoding="utf-8",
            )
            (root / "docs/INSTALL.md").write_text("# install\n", encoding="utf-8")
            (root / "release/km-vms-release.json").write_text('{"schema_version":1,"version":"0.7.1"}\n', encoding="utf-8")
        (source / "scripts/update.sh").write_text(script, encoding="utf-8")
        (app / "scripts/update.sh").write_text(script, encoding="utf-8")
        (app / ".env").write_text("HTTP_PORT=18181\nCOMPOSE_PROJECT_NAME=kmvmsfixture\n", encoding="utf-8")
        (app / "data/postgres").mkdir(parents=True)
        (app / "data/sentinel.txt").write_text("keep\n", encoding="utf-8")

        tarball = Path(tmp) / "source.tar.gz"
        subprocess.run(["tar", "-czf", str(tarball), "-C", str(source.parent), source.name], check=True)
        (bin_dir / "curl").write_text(
            "#!/usr/bin/env sh\ncp '" + str(tarball) + "' \"$4\"\n",
            encoding="utf-8",
        )
        os.chmod(bin_dir / "curl", 0o755)

        before_env = (app / ".env").read_text(encoding="utf-8")
        before_data = (app / "data/sentinel.txt").read_text(encoding="utf-8")
        result = subprocess.run(
            ["sh", "scripts/update.sh", "--github-repo", "owner/repo", "--branch", "main", "--dry-run"],
            cwd=app,
            env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        assert "Dry-run complete" in result.stdout
        assert (app / ".env").read_text(encoding="utf-8") == before_env
        assert (app / "data/sentinel.txt").read_text(encoding="utf-8") == before_data
        assert not (app / "data/update-control").exists()
        assert not (app / ".km-vms-update.json").exists()
        assert not (app / ".km-vms-source.json").exists()


def test_update_apply_overlay_blocks_secret_artifacts_but_copies_legitimate_source_file():
    script = read("scripts/update.sh")
    with tempfile.TemporaryDirectory(prefix="kmvms_update_overlay_fixture_") as tmp:
        app = Path(tmp) / "app"
        source = Path(tmp) / "source"
        bin_dir = Path(tmp) / "bin"
        app.mkdir()
        source.mkdir()
        bin_dir.mkdir()
        for root in (app, source):
            (root / "apps/api").mkdir(parents=True)
            (root / "apps/web").mkdir(parents=True)
            (root / "deploy/nginx").mkdir(parents=True)
            (root / "scripts").mkdir(parents=True)
            (root / "docs").mkdir(parents=True)
            (root / "release").mkdir(parents=True)
            (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (root / "docker-compose.pytest.yml").write_text("services: {}\n", encoding="utf-8")
            (root / ".dockerignore").write_text("data\n", encoding="utf-8")
            (root / ".gitignore").write_text(".env\n", encoding="utf-8")
            (root / ".env.example").write_text("TZ=UTC\n", encoding="utf-8")
            (root / "deploy/nginx/default.conf").write_text("# nginx\n", encoding="utf-8")
            (root / "scripts/install.sh").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
            (root / "scripts/km-vms-compose-common.sh").write_text(
                "\n".join(
                        [
                            "km_vms_detect_compose() { COMPOSE_KIND=stub; COMPOSE_BIN=stub; COMPOSE_SOURCE=stub; }",
                            "km_vms_compose_cmd() { if [ \"$1\" = \"--env-file\" ]; then shift 2; fi; if [ \"$1\" = \"config\" ] && [ ! -f .km-vms-release.json ]; then echo missing release identity before compose config >&2; return 42; fi; if [ \"$1\" = \"exec\" ]; then echo complete; return 0; fi; :; }",
                        ]
                    )
                + "\n",
                encoding="utf-8",
            )
            (root / "docs/INSTALL.md").write_text("# install\n", encoding="utf-8")
            (root / "release/km-vms-release.json").write_text('{"schema_version":1,"version":"0.7.1"}\n', encoding="utf-8")
        (source / "scripts/update.sh").write_text(script, encoding="utf-8")
        (app / "scripts/update.sh").write_text(script, encoding="utf-8")
        (app / ".env").write_text("HTTP_PORT=18182\nCOMPOSE_PROJECT_NAME=kmvmsfixture\n", encoding="utf-8")
        (app / "data/previews").mkdir(parents=True)
        (app / "data/sentinel.txt").write_text("keep\n", encoding="utf-8")

        dangerous_files = (
            "apps/.env",
            "apps/.ssh/id_rsa",
            "apps/github-token.txt",
            "apps/secret.json",
            "apps/api/auth-token.txt",
            "apps/web/authorization-token.json",
            "docs/private.p12",
            "docs/SECRET.json",
            "scripts/credentials.txt",
            "scripts/id_ed25519",
            "scripts/private.key",
        )
        for relative in dangerous_files:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must-not-copy\n", encoding="utf-8")
        (source / "apps/api/stage607_legitimate_source.py").write_text("VALUE = 'copied'\n", encoding="utf-8")
        (source / "apps/api/token_serializer.py").write_text("VALUE = 'legitimate product token logic'\n", encoding="utf-8")
        (source / "apps/api/secret_policy.py").write_text("VALUE = 'legitimate product secret policy logic'\n", encoding="utf-8")
        product_assets = (
            "apps/web/public/stage607-logo.png",
            "apps/web/public/stage607-icon.jpg",
            "docs/images/stage607-update-flow.png",
            "docs/images/stage607-update-flow.jpg",
        )
        for relative in product_assets:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stage607-product-asset\n")

        tarball = Path(tmp) / "source.tar.gz"
        subprocess.run(["tar", "-czf", str(tarball), "-C", str(source.parent), source.name], check=True)
        (bin_dir / "curl").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env sh",
                    "case \"$*\" in */commits/main*) printf '{\\n  \"sha\": \"" + ("b" * 40) + "\"\\n}\\n'; exit 0 ;; esac",
                    "for arg in \"$@\"; do",
                    "  if [ \"$prev\" = \"-o\" ]; then cp '" + str(tarball) + "' \"$arg\"; exit 0; fi",
                    "  prev=\"$arg\"",
                    "done",
                    "exit 0",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(bin_dir / "curl", 0o755)

        result = subprocess.run(
            ["sh", "scripts/update.sh", "--github-repo", "owner/repo", "--branch", "main", "--yes"],
            cwd=app,
            env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        assert "KM VMS update completed" in result.stdout
        for relative in dangerous_files:
            assert not (app / relative).exists(), relative
        assert (app / "apps/api/stage607_legitimate_source.py").exists()
        assert (app / "apps/api/token_serializer.py").exists()
        assert (app / "apps/api/secret_policy.py").exists()
        for relative in product_assets:
            assert (app / relative).exists(), relative
        assert (app / ".env").read_text(encoding="utf-8").startswith("HTTP_PORT=18182")
        assert (app / "data/sentinel.txt").read_text(encoding="utf-8") == "keep\n"
        assert (app / ".km-vms-release.json").is_file()
        assert json.loads((app / ".km-vms-release.json").read_text(encoding="utf-8"))["metadata_status"] in {"complete", "partial"}
        update_metadata = json.loads((app / ".km-vms-update.json").read_text(encoding="utf-8"))
        assert update_metadata["validation_summary"]["release_identity_api_visible"] is True
        assert update_metadata["validation_summary"]["release_identity_commit_verified"] is True


def test_update_script_final_identity_preserves_bind_mount_inode_and_verifies_api_visibility():
    script = read("scripts/update.sh")

    assert 'cat "$tmp_identity" > "$identity"' in script
    assert 'mv "$tmp_identity" "$identity"' in script
    assert "verify_api_visible_release_identity" in script
    assert "API-visible release identity is stale or incomplete" in script
    assert "up -d --force-recreate api" in script
    assert "release_identity_api_visible" in script
    assert "release_identity_commit_verified" in script


def test_update_script_recreates_api_until_api_visible_identity_is_complete(tmp_path):
    expected = "b" * 40
    compose_function = "\n".join(
        [
            "km_vms_detect_compose() { COMPOSE_KIND=stub; COMPOSE_BIN=stub; COMPOSE_SOURCE=stub; }",
            "km_vms_compose_cmd() {",
            "  if [ \"$1\" = \"--env-file\" ]; then shift 2; fi",
            "  if [ \"$1\" = \"config\" ] && [ ! -f .km-vms-release.json ]; then echo missing release identity before compose config >&2; return 42; fi",
            "  if [ \"$1\" = \"up\" ]; then case \"$*\" in *'--force-recreate api'*) touch data/api-recreated ;; esac; return 0; fi",
            "  if [ \"$1\" = \"exec\" ]; then",
            "    if [ -f data/api-recreated ]; then echo 'complete " + expected + "'; return 0; fi",
            "    return 11",
            "  fi",
            "  :",
            "}",
        ]
    )
    app, _source, bin_dir = _write_update_shell_fixture(tmp_path, compose_function=compose_function, commit=expected)

    result = subprocess.run(
        ["sh", "scripts/update.sh", "--github-repo", "owner/repo", "--branch", "main", "--yes"],
        cwd=app,
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    metadata = json.loads((app / ".km-vms-update.json").read_text(encoding="utf-8"))

    assert "API-visible release identity is stale or incomplete" in result.stdout
    assert (app / "data/api-recreated").is_file()
    assert metadata["status"] == "success"
    assert metadata["validation_summary"]["release_identity_api_metadata_status"] == "complete"
    assert metadata["validation_summary"]["release_identity_api_visible"] is True
    assert metadata["validation_summary"]["release_identity_commit_verified"] is True


def test_update_script_remediates_api_visible_stale_precompose_identity(tmp_path):
    expected = "b" * 40
    compose_function = "\n".join(
        [
            "km_vms_detect_compose() { COMPOSE_KIND=stub; COMPOSE_BIN=stub; COMPOSE_SOURCE=stub; }",
            "km_vms_compose_cmd() {",
            "  if [ \"$1\" = \"--env-file\" ]; then shift 2; fi",
            "  if [ \"$1\" = \"config\" ] && [ ! -f .km-vms-release.json ]; then echo missing release identity before compose config >&2; return 42; fi",
            "  if [ \"$1\" = \"up\" ]; then case \"$*\" in *'--force-recreate api'*) touch data/api-recreated-after-precompose ;; esac; return 0; fi",
            "  if [ \"$1\" = \"exec\" ]; then",
            "    if [ -f data/api-recreated-after-precompose ]; then echo 'complete " + expected + "'; return 0; fi",
            "    echo 'metadata_status=precompose' >&2; return 11",
            "  fi",
            "  :",
            "}",
        ]
    )
    app, _source, bin_dir = _write_update_shell_fixture(tmp_path, compose_function=compose_function, commit=expected)

    result = subprocess.run(
        ["sh", "scripts/update.sh", "--github-repo", "owner/repo", "--branch", "main", "--yes"],
        cwd=app,
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    metadata = json.loads((app / ".km-vms-update.json").read_text(encoding="utf-8"))

    assert "API-visible release identity is stale or incomplete" in result.stdout
    assert (app / "data/api-recreated-after-precompose").is_file()
    assert json.loads((app / ".km-vms-release.json").read_text(encoding="utf-8"))["metadata_status"] == "complete"
    assert metadata["status"] == "success"
    assert metadata["validation_summary"]["release_identity_host_metadata_status"] == "complete"
    assert metadata["validation_summary"]["release_identity_api_metadata_status"] == "complete"
    assert metadata["validation_summary"]["release_identity_api_visible"] is True
    assert metadata["validation_summary"]["release_identity_commit_verified"] is True


def test_update_script_rejects_api_visible_complete_with_wrong_commit_stdout(tmp_path):
    expected = "b" * 40
    wrong = "a" * 40
    compose_function = "\n".join(
        [
            "km_vms_detect_compose() { COMPOSE_KIND=stub; COMPOSE_BIN=stub; COMPOSE_SOURCE=stub; }",
            "km_vms_compose_cmd() {",
            "  if [ \"$1\" = \"--env-file\" ]; then shift 2; fi",
            "  if [ \"$1\" = \"config\" ] && [ ! -f .km-vms-release.json ]; then echo missing release identity before compose config >&2; return 42; fi",
            "  if [ \"$1\" = \"up\" ]; then touch data/api-recreate-attempted; return 0; fi",
            "  if [ \"$1\" = \"exec\" ]; then echo 'complete " + wrong + "'; return 12; fi",
            "  :",
            "}",
        ]
    )
    app, _source, bin_dir = _write_update_shell_fixture(tmp_path, compose_function=compose_function, commit=expected)

    result = subprocess.run(
        ["sh", "scripts/update.sh", "--github-repo", "owner/repo", "--branch", "main", "--yes"],
        cwd=app,
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    metadata = json.loads((app / ".km-vms-update.json").read_text(encoding="utf-8"))

    assert result.returncode != 0
    assert "API-visible release identity is not complete" in result.stderr
    assert (app / "data/api-recreate-attempted").is_file()
    assert metadata["status"] == "failed"
    assert metadata["validation_summary"]["release_identity_api_metadata_status"] == ""
    assert metadata["validation_summary"]["release_identity_api_visible"] is False
    assert metadata["validation_summary"]["release_identity_commit_verified"] is False


def test_update_helper_requires_complete_host_and_api_visible_identity():
    helper = read("scripts/km-vms-update-helper.py")

    assert "release_identity_api_visible" in helper
    assert "release_identity_commit_verified" in helper
    assert "Host release identity is not complete after successful apply." in helper
    assert "API-visible release identity was not confirmed complete after successful apply." in helper
    assert 'completed["release_identity"] = release_identity' in helper


def test_docs_describe_terminal_update_without_future_stage_claims():
    docs = read("docs/INSTALL.md")

    for required in (
        "## Terminal Update",
        "curl -fsSL https://raw.githubusercontent.com/kmishnev87/km-vms/main/scripts/install.sh",
        "sh scripts/update.sh --branch v0.7.2 --yes",
        "sh scripts/update.sh --branch v0.7.2 --dry-run",
        "sh scripts/km-vms-release-cycle.sh --check",
        "git tag -a vX.Y.Z",
        ".km-vms-release.json",
        "--github-private",
        "KM_VMS_GITHUB_TOKEN",
        ".env",
        "data/",
        "rollback is not implemented",
        "bounded in-app apply orchestration",
        "dedicated `update-helper` service",
    ):
        assert required in docs

    assert "down -v" in docs
    assert "docker system prune" in docs
    assert "delete Docker volumes" in docs
    assert "automatically runs database migrations" not in docs
