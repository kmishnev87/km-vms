import os
import re
import subprocess
import sys
import tempfile
import importlib.util
import json
import pytest
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _write_stable_runtime_fixture(root: Path) -> None:
    if not (root / ".env").exists():
        (root / ".env").write_text(
            "COMPOSE_PROJECT_NAME=kmvmsfixture\n",
            encoding="utf-8",
        )
    bootstrap = (
        root
        / "data/update-runtime/bootstrap/current/km-vms-bootstrap.py"
    )
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text("# stable bootstrap fixture\n", encoding="utf-8")


PERMISSION_EXECUTABLE_FIXTURE_FILES = (
    "scripts/install.sh",
    "scripts/km-vms-adopt-release-identity.sh",
    "scripts/km-vms-compose-common.sh",
    "scripts/km-vms-permission-gate.sh",
    "scripts/km-vms-release-slots.py",
    "scripts/km-vms-publish-github-release.sh",
    "scripts/km-vms-release-cycle.sh",
    "scripts/km-vms-restart.sh",
    "scripts/km-vms-update-launcher.sh",
    "scripts/km-vms-setup-activation-helper.sh",
    "scripts/km-vms-storage-apply.sh",
    "scripts/km-vms-storage-discovery.sh",
    "scripts/run_backend_tests.sh",
    "scripts/update.sh",
)


def _write_safe_getfacl(bin_dir: Path) -> None:
    tool = bin_dir / "getfacl"
    tool.write_text(
        "#!/usr/bin/env sh\n"
        "printf 'user::rwx\ngroup::r-x\nother::r-x\n'\n",
        encoding="utf-8",
    )
    os.chmod(tool, 0o755)


def _write_permission_chain_fixture(root: Path) -> None:
    (root / "apps/update-helper").mkdir(parents=True, exist_ok=True)
    (root / "deploy").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "release").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    dockerfile = root / "apps/update-helper/Dockerfile"
    if not dockerfile.exists():
        dockerfile.write_text("FROM docker:27-cli\n", encoding="utf-8")
    helper = root / "scripts/km-vms-update-helper.py"
    if not helper.exists():
        helper.write_text("# fixture\n", encoding="utf-8")
    bridge = root / "scripts/km-vms-update-helper-bridge.py"
    if not bridge.exists():
        bridge.write_text(read("scripts/km-vms-update-helper-bridge.py"), encoding="utf-8")
    os.chmod(bridge, 0o644)
    for relative in (
        "scripts/km-vms-storage-candidate-validate.sh",
        "scripts/km-vms-storage-root-cleanup.sh",
    ):
        path = root / relative
        if not path.exists():
            path.write_text("#!/usr/bin/env sh\n", encoding="utf-8")
        os.chmod(path, 0o644)
    for relative in PERMISSION_EXECUTABLE_FIXTURE_FILES:
        path = root / relative
        if relative == "scripts/km-vms-permission-gate.sh":
            path.write_text(read(relative), encoding="utf-8")
        elif not path.exists():
            path.write_text("#!/usr/bin/env sh\n", encoding="utf-8")
        os.chmod(path, 0o755)


def _write_update_shell_fixture(tmp_path: Path, *, compose_function: str, commit: str = "b" * 40) -> tuple[Path, Path, Path]:
    script = read("scripts/update.sh")
    app = tmp_path / "app"
    source = tmp_path / "source"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_safe_getfacl(bin_dir)
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
            compose_function
            + "\n"
            + "km_vms_resolve_product_source() { printf '%s\\n' \"$1\"; }\n"
            + "km_vms_compose_for_source() { shift 2; km_vms_compose_cmd \"$@\"; }\n",
            encoding="utf-8",
        )
        (root / "docs/INSTALL.md").write_text("# install\n", encoding="utf-8")
        (root / "release/km-vms-release.json").write_text('{"schema_version":1,"version":"0.7.1"}\n', encoding="utf-8")
        (root / "release/km-vms-update-lineage.json").write_text(
            read("release/km-vms-update-lineage.json"),
            encoding="utf-8",
        )
        _write_permission_chain_fixture(root)
    (source / "scripts/update.sh").write_text(script, encoding="utf-8")
    (app / "scripts/update.sh").write_text(script, encoding="utf-8")
    (app / ".env").write_text("HTTP_PORT=18183\nCOMPOSE_PROJECT_NAME=kmvmsfixture\n", encoding="utf-8")
    (app / "data").mkdir()
    _write_stable_runtime_fixture(app)
    tarball = tmp_path / "source.tar.gz"
    subprocess.run(["tar", "-czf", str(tarball), "-C", str(source.parent), source.name], check=True)
    (bin_dir / "curl").write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                "case \"$*\" in */commits/main*) printf '{\\n  \"sha\": \"" + commit + "\"\\n}\\n'; exit 0 ;; esac",
                'output=""',
                'headers=""',
                "for arg in \"$@\"; do",
                '  [ "$prev" = "-o" ] && output="$arg"',
                '  [ "$prev" = "--dump-header" ] && headers="$arg"',
                "  prev=\"$arg\"",
                "done",
                "cp '" + str(tarball) + "' \"$output\"",
                "printf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > \"$headers\"",
                "exit 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(bin_dir / "curl", 0o755)
    return app, source, bin_dir


def _write_acquisition_curl_fixture(
    bin_dir: Path,
    tarball: Path,
    state_dir: Path,
) -> None:
    curl = bin_dir / "curl"
    curl.write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                f"state_dir='{state_dir}'",
                f"tarball='{tarball}'",
                'count_file="$state_dir/count"',
                'count=$(cat "$count_file" 2>/dev/null || printf 0)',
                'count=$((count + 1))',
                'printf "%s\\n" "$count" > "$count_file"',
                'output=""',
                'headers=""',
                'url=""',
                'previous=""',
                'for argument in "$@"; do',
                '  [ "$previous" = "-o" ] && output="$argument"',
                '  [ "$previous" = "--dump-header" ] && headers="$argument"',
                '  case "$argument" in https://*) url="$argument" ;; esac',
                '  previous="$argument"',
                'done',
                'printf "%s\\n" "$url" >> "$state_dir/urls"',
                'if [ "$count" -gt 1 ]; then',
                '  [ -e "$output" ] && printf dirty > "$state_dir/partial-state" || printf clean > "$state_dir/partial-state"',
                'fi',
                'mode=${FAKE_CURL_MODE:-success}',
                'case "$mode:$count" in',
                '  transient_then_success:1)',
                '    printf partial > "$output"',
                "    printf 'HTTP/1.1 503 Service Unavailable\\r\\nRetry-After: 0\\r\\n\\r\\n' > \"$headers\"",
                '    exit 22',
                '    ;;',
                '  rate_limit_then_success:1)',
                '    printf limited > "$output"',
                "    printf 'HTTP/1.1 403 Forbidden\\r\\nRetry-After: 0\\r\\nX-RateLimit-Remaining: 0\\r\\n\\r\\n' > \"$headers\"",
                '    exit 22',
                '    ;;',
                '  exhausted:*)',
                '    printf partial > "$output"',
                "    printf 'HTTP/1.1 503 Service Unavailable\\r\\nRetry-After: 0\\r\\n\\r\\n' > \"$headers\"",
                '    exit 22',
                '    ;;',
                '  permanent:*)',
                "    printf 'HTTP/1.1 404 Not Found\\r\\n\\r\\n' > \"$headers\"",
                '    exit 22',
                '    ;;',
                '  long_rate_limit:*)',
                "    printf 'HTTP/1.1 403 Forbidden\\r\\nRetry-After: 60\\r\\nX-RateLimit-Remaining: 0\\r\\n\\r\\n' > \"$headers\"",
                '    exit 22',
                '    ;;',
                '  secondary_rate_limit_body:*)',
                "    printf '%s' '{\"message\":\"You have exceeded a secondary rate limit. Please wait before retrying.\",\"documentation_url\":\"https://docs.github.com/rest/using-the-rest-api/rate-limits-for-the-rest-api\"}' > \"$output\"",
                "    printf 'HTTP/1.1 403 Forbidden\\r\\nX-RateLimit-Remaining: 42\\r\\nContent-Type: application/json\\r\\n\\r\\n' > \"$headers\"",
                '    exit 0',
                '    ;;',
                '  ordinary_403_body:*)',
                "    printf '%s' '{\"message\":\"Resource not accessible by integration\"}' > \"$output\"",
                "    printf 'HTTP/1.1 403 Forbidden\\r\\nX-RateLimit-Remaining: 42\\r\\nContent-Type: application/json\\r\\n\\r\\n' > \"$headers\"",
                '    exit 0',
                '    ;;',
                'esac',
                "printf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > \"$headers\"",
                'cp "$tarball" "$output"',
                'exit 0',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(curl, 0o755)
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    os.chmod(docker, 0o755)


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
    assert (
        "data/update-runtime/bootstrap/current/km-vms-update-launcher.sh"
        in help_result.stdout
    )
    assert "sh scripts/update.sh" not in help_result.stdout

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


def test_operator_help_uses_canonical_stable_bootstrap_entries():
    restart_result = subprocess.run(
        ["sh", str(ROOT / "scripts/km-vms-restart.sh"), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert (
        "data/update-runtime/bootstrap/current/km-vms-restart.sh"
        in restart_result.stdout
    )
    assert "sh scripts/km-vms-restart.sh" not in restart_result.stdout


def test_canonical_update_launcher_help_does_not_advertise_root_entry(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    bundle = app / "data/update-runtime/bootstrap/bundles/fixture"
    slot_id = "release-" + ("a" * 40)
    source = app / "data/update-runtime/slots" / slot_id / "source"
    bundle.mkdir(parents=True)
    source.joinpath("scripts").mkdir(parents=True)
    (app / ".env").write_text(
        "COMPOSE_PROJECT_NAME=fixture\n",
        encoding="utf-8",
    )
    launcher = bundle / "km-vms-update-launcher.sh"
    launcher.write_text(
        read("scripts/km-vms-update-launcher.sh"),
        encoding="utf-8",
    )
    (bundle / "km-vms-bootstrap.py").write_text(
        "import os\nprint(os.environ['KM_VMS_TEST_ACTIVE_SOURCE'])\n",
        encoding="utf-8",
    )
    source.joinpath("scripts/update.sh").write_text(
        read("scripts/update.sh"),
        encoding="utf-8",
    )
    current = app / "data/update-runtime/bootstrap/current"
    current.symlink_to("bundles/fixture")
    environment = os.environ.copy()
    environment["KM_VMS_TEST_ACTIVE_SOURCE"] = str(source)
    result = subprocess.run(
        ["sh", str(current / launcher.name), "--app-dir", str(app), "--help"],
        cwd=app,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        "data/update-runtime/bootstrap/current/km-vms-update-launcher.sh"
        in result.stdout
    )
    assert "sh scripts/update.sh" not in result.stdout


def test_restart_exposes_detected_vendor_docker_to_stable_bootstrap(
    tmp_path: Path,
) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    for name in ("docker", "docker-compose"):
        executable = vendor / name
        executable.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        os.chmod(executable, 0o755)
    restart = read("scripts/km-vms-restart.sh")
    detect_body = restart.split("detect_compose() {", 1)[1].split(
        "\n}\n",
        1,
    )[0]
    probe = (
        "set -eu\n"
        "DOCKER_COMPOSE_BIN=$1\n"
        "fail() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        "km_vms_detect_compose() { COMPOSE_BIN=$1; return 0; }\n"
        f"detect_compose() {{{detect_body}\n}}\n"
        "detect_compose\n"
        '"$KM_VMS_TEST_PYTHON" -c \'import subprocess; '
        "subprocess.run([\"docker\", \"version\"], check=True)'\n"
    )
    result = subprocess.run(
        ["sh", "-c", probe, "vendor-probe", str(vendor / "docker-compose")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": "/usr/bin:/bin",
            "KM_VMS_TEST_PYTHON": sys.executable,
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_update_script_reuses_compose_helper_and_github_tarball_without_git_requirement():
    script = read("scripts/update.sh")

    assert '. "$product_source/scripts/km-vms-compose-common.sh"' in script
    assert '. "$APP_DIR/scripts/km-vms-compose-common.sh"' not in script
    assert "km_vms_detect_compose" in script
    assert 'if [ -x "$compose_bin_dir/docker" ]; then' in script
    assert 'PATH="$compose_bin_dir:$PATH"' in script
    assert "https://api.github.com/repos/$GITHUB_REPO/tarball/$BRANCH" in script
    assert "git clone" not in script
    assert "command_exists git" not in script
    assert "Authorization: Bearer" in script
    assert ".km-vms-source.json" in script
    assert ".km-vms-update.json" in script
    assert ".km-vms-release.json" in script
    assert "kmishnev87/km-vms" in script


def test_update_helper_steps_match_current_failed_phase():
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_contract", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    def statuses(steps):
        return {step["name"]: step["status"] for step in steps}

    for phase, expected_step in (
        ("preflight", "preflight"),
        ("overlay", "applying"),
        ("compose_config", "applying"),
        ("rebuilding", "applying"),
        ("health_check", "health_check"),
        ("commit_verification", "commit_verification"),
    ):
        assert statuses(helper.steps_for(phase, failed=True))[
            expected_step
        ] == "failed"


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
    _write_stable_runtime_fixture(app_dir)
    helper.HOST_APP_DIR = app_dir
    assert helper.compose_app_dir() == app_dir


def test_update_helper_resolves_update_script_from_canonical_active_source(tmp_path, monkeypatch):
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location(
        "km_vms_update_helper_active_source_contract",
        helper_path,
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    app_dir = tmp_path / "app"
    scripts_dir = app_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "km-vms-compose-common.sh").write_text(
        read("scripts/km-vms-compose-common.sh"),
        encoding="utf-8",
    )
    (scripts_dir / "update.sh").write_text(
        "#!/usr/bin/env sh\n",
        encoding="utf-8",
    )
    (app_dir / "docker-compose.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    _write_stable_runtime_fixture(app_dir)

    slot_id = f"release-{'a' * 40}"
    slot_root = app_dir / "data" / "update-runtime" / "slots" / slot_id
    source_dir = slot_root / "source"
    (source_dir / "scripts").mkdir(parents=True)
    (source_dir / "scripts" / "update.sh").write_text(
        "#!/usr/bin/env sh\n",
        encoding="utf-8",
    )
    (source_dir / "docker-compose.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (slot_root / "slot-manifest.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    active = app_dir / "data" / "update-runtime" / "active"
    active.symlink_to(f"slots/{slot_id}/source")

    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"source_path": str(source_dir)}) + "\n",
            stderr="",
        ),
    )

    assert helper.resolve_update_source_dir(app_dir) == source_dir


def test_update_helper_classifies_health_check_failure_from_metadata(tmp_path):
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location("km_vms_update_helper_failure_contract", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    (tmp_path / ".km-vms-update.json").write_text(json.dumps({"schema_version": 1, "failed_phase": "health_check"}), encoding="utf-8")
    exc = helper.classify_apply_failure(tmp_path, "health stderr")
    assert exc.category == "health_check_failed"
    assert str(exc) == "Update health check failed."
    assert "health stderr" not in str(exc)

    (tmp_path / ".km-vms-update.json").write_text(json.dumps({"schema_version": 1, "failed_phase": "rebuild_recreate"}), encoding="utf-8")
    assert helper.classify_apply_failure(tmp_path, "apply stderr").category == "docker_build_failed"
    (tmp_path / ".km-vms-update.json").write_text(
        json.dumps({"schema_version": 1, "failed_phase": "schema_update"}),
        encoding="utf-8",
    )
    schema_failure = helper.classify_apply_failure(tmp_path, "apply stderr")
    assert schema_failure.category == "schema_update_failed"
    assert schema_failure.phase == "schema_update_failed"
    (tmp_path / ".km-vms-update.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "failed_phase": "schema_preflight",
            }
        ),
        encoding="utf-8",
    )
    preflight_failure = helper.classify_apply_failure(
        tmp_path,
        "/volume/private/raw-preflight-error",
    )
    assert preflight_failure.category == "preflight_failed"
    assert "/volume/private" not in str(preflight_failure)
    (tmp_path / ".km-vms-update.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "failed_phase": "schema_preflight",
                "error_category": "slot_adoption_conflict",
            }
        ),
        encoding="utf-8",
    )
    adoption_conflict = helper.classify_apply_failure(
        tmp_path,
        "/volume/private/raw-preflight-error",
    )
    assert adoption_conflict.category == "slot_adoption_conflict"
    assert "/volume/private" not in str(adoption_conflict)
    assert helper.classify_preflight_failure(
        "ERROR [slot_adoption_conflict]: hidden detail"
    ).category == "slot_adoption_conflict"
    unknown_preflight = helper.classify_preflight_failure(
        "ERROR [arbitrary_internal_code]: /volume/private/raw"
    )
    assert unknown_preflight.category == "preflight_failed"
    assert "/volume/private" not in str(unknown_preflight)
    (tmp_path / ".km-vms-update.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "failed_phase": "schema_preflight",
                "error_category": "arbitrary_internal_code",
            }
        ),
        encoding="utf-8",
    )
    assert helper.classify_apply_failure(
        tmp_path,
        "ERROR [arbitrary_internal_code]: /volume/private/raw",
    ).category == "preflight_failed"
    (tmp_path / ".km-vms-update.json").unlink()
    generic = helper.classify_apply_failure(tmp_path, "/volume/private/raw-stderr-marker")
    assert generic.category == "apply_failed"
    assert str(generic) == "Update apply failed."
    assert "/volume/private/raw-stderr-marker" not in json.dumps(
        helper.error_payload("helper_exception")
    )


def test_update_progress_schema_accepts_only_exact_bounded_values(
    tmp_path: Path,
) -> None:
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location(
        "km_vms_update_helper_progress_contract",
        helper_path,
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.PROGRESS_FILE = tmp_path / "update-progress.json"
    request_id = "update-" + ("a" * 32)

    def read(values: dict) -> dict:
        helper.PROGRESS_FILE.write_text(
            json.dumps(
                {
                    "status": "running",
                    "phase": "acquire",
                    "current_step": "acquire_source",
                    "request_id": request_id,
                    **values,
                }
            ),
            encoding="utf-8",
        )
        return helper.read_progress(request_id)

    for percent in (0, 42, 100):
        progress = read(
            {
                "progress_percent": percent,
                "progress_current": percent,
                "progress_total": 100,
                "progress_unit": "bytes",
            }
        )
        assert progress["progress_percent"] == percent

    assert read({})["progress_percent"] is None
    for invalid in (
        {
            "progress_percent": -1,
            "progress_current": 0,
            "progress_total": 100,
            "progress_unit": "bytes",
        },
        {
            "progress_percent": 101,
            "progress_current": 100,
            "progress_total": 100,
            "progress_unit": "bytes",
        },
        {
            "progress_percent": float("inf"),
            "progress_current": 1,
            "progress_total": 2,
            "progress_unit": "bytes",
        },
        {
            "progress_percent": 50,
            "progress_current": 42,
            "progress_total": 100,
            "progress_unit": "bytes",
        },
    ):
        progress = read(invalid)
        assert progress["progress_percent"] is None
        assert progress["progress_current"] is None


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
    _write_stable_runtime_fixture(app_dir)
    helper.HOST_APP_DIR = app_dir
    helper.STATUS_FILE = control_dir / "update-status.json"
    active_source_dir = (
        app_dir
        / "data"
        / "update-runtime"
        / "slots"
        / f"release-{'d' * 40}"
        / "source"
    )
    (active_source_dir / "scripts").mkdir(parents=True)
    (active_source_dir / "scripts" / "update.sh").write_text(
        "#!/usr/bin/env sh\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        helper,
        "resolve_update_source_dir",
        lambda stable_app_dir: active_source_dir,
    )

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
    published = []
    monkeypatch.setattr(helper, "publish_terminal", lambda request_arg, status: published.append((request_arg, status)))
    request = {
        "schema_version": 1,
        "request_id": "stage609-pin",
        "requested_at": "2026-06-19T00:00:00Z",
        "intent": "apply_update",
        "confirmed": True,
        "source": {
            "kind": "trusted_manifest",
            "channel": "stable",
            "version": "0.8.0",
            "source_type": "github_tarball",
            "repo": "owner/repo",
            "ref": "main",
            "commit": expected,
            "apply_ref": expected,
        },
    }

    assert helper.run_update(request) == 0
    assert published and published[0][1]["status"] == "completed"
    assert len(commands) == 1
    assert "--dry-run" not in commands[0]
    assert commands[0][:6] == [
        "sh",
        str(active_source_dir / "scripts" / "update.sh"),
        "--github-repo",
        "owner/repo",
        "--branch",
        expected,
    ]
    assert "main" not in commands[0]
    trusted_index = commands[0].index("--trusted-commit")
    assert commands[0][trusted_index + 1] == expected
    status = published[0][1]
    assert status["status"] == "completed"
    assert status["commit_verified"] is True
    assert status["installed_commit"] == expected
    assert status["release_identity"] == {
        "host_metadata_status": "complete",
        "api_metadata_status": "complete",
        "api_visible": True,
        "commit_verified": True,
    }


@pytest.mark.parametrize(
    ("mode", "expected_returncode", "expected_attempts", "expected_category"),
    [
        ("transient_then_success", 0, 2, None),
        ("rate_limit_then_success", 0, 2, None),
        ("exhausted", 1, 3, "source_acquisition_failed"),
        ("permanent", 1, 1, "source_acquisition_failed"),
        ("long_rate_limit", 1, 1, "source_temporarily_unavailable"),
        ("secondary_rate_limit_body", 1, 1, "source_temporarily_unavailable"),
        ("ordinary_403_body", 1, 1, "source_acquisition_failed"),
        ("success", 0, 1, None),
    ],
)
def test_in_app_exact_commit_acquisition_is_bounded_and_sanitized(
    tmp_path: Path,
    mode: str,
    expected_returncode: int,
    expected_attempts: int,
    expected_category: str | None,
) -> None:
    commit = "c" * 40
    compose_function = (
        "km_vms_detect_compose() { COMPOSE_KIND=stub; COMPOSE_BIN=stub; COMPOSE_SOURCE=stub; }\n"
        "km_vms_compose_cmd() { :; }"
    )
    app, _source, bin_dir = _write_update_shell_fixture(
        tmp_path,
        compose_function=compose_function,
        commit=commit,
    )
    state_dir = tmp_path / "curl-state"
    state_dir.mkdir()
    _write_acquisition_curl_fixture(
        bin_dir,
        tmp_path / "source.tar.gz",
        state_dir,
    )
    command = [
        "sh",
        "scripts/update.sh",
        "--github-repo",
        "owner/repo",
        "--branch",
        commit,
        "--trusted-commit",
        commit,
    ]
    if expected_returncode == 0:
        command.append("--dry-run")
    result = subprocess.run(
        command,
        cwd=app,
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "FAKE_CURL_MODE": mode,
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == expected_returncode
    assert int((state_dir / "count").read_text().strip()) == expected_attempts
    urls = (state_dir / "urls").read_text(encoding="utf-8").splitlines()
    assert len(urls) == expected_attempts
    assert all("/commits/" not in url for url in urls)
    assert all(url.endswith(f"/tarball/{commit}") for url in urls)
    assert "Authorization:" not in result.stdout + result.stderr
    assert "partial" not in result.stdout + result.stderr
    assert "secondary rate limit" not in (result.stdout + result.stderr).casefold()
    if expected_attempts > 1:
        assert (state_dir / "partial-state").read_text() == "clean"

    metadata_path = app / ".km-vms-update.json"
    if expected_category is None:
        assert not metadata_path.exists()
        assert "Dry-run complete" in result.stdout
    else:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["failed_phase"] == "acquire"
        assert metadata["error_category"] == expected_category
        assert metadata["source_acquisition_attempts"] == expected_attempts
        assert metadata["error_message"] in {
            f"Trusted source acquisition failed after {expected_attempts} bounded attempt(s). Verify source access before retrying.",
            f"Trusted source is temporarily unavailable after {expected_attempts} bounded attempt(s). Retry later.",
        }


def test_update_helper_preserves_safe_acquisition_failure_categories(tmp_path: Path) -> None:
    helper_path = ROOT / "scripts" / "km-vms-update-helper.py"
    spec = importlib.util.spec_from_file_location(
        "km_vms_update_helper_acquisition_contract",
        helper_path,
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    for category in (
        "source_acquisition_failed",
        "source_temporarily_unavailable",
    ):
        (tmp_path / ".km-vms-update.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "failed_phase": "acquire",
                    "error_category": category,
                    "source_acquisition_attempts": 3,
                    "error_message": "/private/raw/body",
                }
            ),
            encoding="utf-8",
        )
        failure = helper.classify_apply_failure(
            tmp_path,
            "/private/raw/stderr",
        )
        assert failure.category == category
        assert failure.phase == "acquire_source"
        assert failure.diagnostics["source_acquisition_attempts"] == 3
        assert "/private" not in str(failure)


def test_update_helper_drops_unavailable_container_and_host_compose_overrides(tmp_path, monkeypatch):
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
    _write_stable_runtime_fixture(app_dir)
    helper.HOST_APP_DIR = app_dir
    helper.STATUS_FILE = control_dir / "update-status.json"
    helper.PROGRESS_FILE = control_dir / "update-progress.json"
    monkeypatch.setattr(
        helper,
        "resolve_update_source_dir",
        lambda stable_app_dir: stable_app_dir,
    )

    expected = "e" * 40
    seen_compose_values = []
    monkeypatch.setenv("KM_VMS_DOCKER_COMPOSE", "/Volume1/@apps/DockerEngine/dockerd/bin/docker-compose")
    monkeypatch.setenv("KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE", "docker-compose")
    monkeypatch.setattr(helper.shutil, "which", lambda _command: None)

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
    published = []
    monkeypatch.setattr(helper, "publish_terminal", lambda request_arg, status: published.append((request_arg, status)))
    request = {
        "schema_version": 1,
        "request_id": "stage651-compose-env",
        "requested_at": "2026-07-06T00:00:00Z",
        "intent": "apply_update",
        "confirmed": True,
        "source": {
            "kind": "trusted_manifest",
            "channel": "stable",
            "version": "0.8.0",
            "source_type": "github_tarball",
            "repo": "owner/repo",
            "ref": expected,
            "commit": expected,
            "apply_ref": expected,
        },
    }

    assert helper.run_update(request) == 0
    assert published and published[0][1]["status"] == "completed"
    assert seen_compose_values == [None]


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

    assert 'compose_with_archive_roots up -d --build' in script
    assert 'archive_roots_compose_present || return 1' in script
    assert 'KM_VMS_UPDATE_HELPER_MODE' in script
    assert 'up -d --build postgres redis api recorder web nginx' in script
    assert "health_attempts=60" in script
    assert 'health_targets="http://nginx/api/health http://api:8000/health"' in script
    assert "the active source remains unchanged" in script
    assert "restores the exact captured previous release" in script


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
        ".gitattributes",
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
        "Source tree is missing scripts/km-vms-permission-gate.sh",
        "Source tree is missing scripts/km-vms-update-helper-bridge.py",
        "Source tree is missing scripts/km-vms-release-slots.py",
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
        "error_category",
        "capture_safe_bridge_failure_category",
        "slot_adoption_conflict",
        '"implemented": true',
    ):
        assert required in script

    metadata_block = script.split("write_update_metadata()", 1)[1].split("fail()", 1)[0]
    assert "GITHUB_TOKEN" not in metadata_block
    assert "KM_VMS_GITHUB_TOKEN" not in metadata_block

    main = script[script.index('confirm "Apply KM VMS update now?"') :]
    permission_preflight_i = main.index(
        "\npreflight_permission_policy\n"
    )
    handoff_i = main.index("\nprepare_schema_handoff\n")
    target_i = main.index("\nprepare_trusted_target_slot\n")
    activation_i = main.index("\nactivate_trusted_target_slot\n")
    postflight_i = main.index("\npostflight_preservation\n")
    assert (
        permission_preflight_i
        < handoff_i
        < target_i
        < activation_i
        < postflight_i
    )
    assert "overlay_source\n" not in main
    assert "rebuild_recreate\n" not in main
    assert "prepare_schema_candidate_image\n" not in main
    assert "run_schema_migration\n" not in main
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
        _write_safe_getfacl(bin_dir)
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
                                "km_vms_resolve_product_source() { printf '%s\\n' \"$1\"; }",
                                "km_vms_compose_for_source() { shift 2; km_vms_compose_cmd \"$@\"; }",
                        ]
                    )
                + "\n",
                encoding="utf-8",
            )
            (root / "docs/INSTALL.md").write_text("# install\n", encoding="utf-8")
            (root / "release/km-vms-release.json").write_text('{"schema_version":1,"version":"0.7.1"}\n', encoding="utf-8")
            (root / "release/km-vms-update-lineage.json").write_text(
                read("release/km-vms-update-lineage.json"),
                encoding="utf-8",
            )
            _write_permission_chain_fixture(root)
        (source / "scripts/update.sh").write_text(script, encoding="utf-8")
        (app / "scripts/update.sh").write_text(script, encoding="utf-8")
        (app / ".env").write_text("HTTP_PORT=18181\nCOMPOSE_PROJECT_NAME=kmvmsfixture\n", encoding="utf-8")
        (app / "data/postgres").mkdir(parents=True)
        (app / "data/sentinel.txt").write_text("keep\n", encoding="utf-8")
        _write_stable_runtime_fixture(app)

        tarball = Path(tmp) / "source.tar.gz"
        subprocess.run(["tar", "-czf", str(tarball), "-C", str(source.parent), source.name], check=True)
        (bin_dir / "curl").write_text(
            "#!/usr/bin/env sh\ncp '" + str(tarball) + "' \"$4\"\nprintf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > \"$6\"\n",
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


def test_update_dry_run_never_copies_acquired_source_into_stable_app():
    script = read("scripts/update.sh")
    with tempfile.TemporaryDirectory(prefix="kmvms_update_overlay_fixture_") as tmp:
        app = Path(tmp) / "app"
        source = Path(tmp) / "source"
        bin_dir = Path(tmp) / "bin"
        app.mkdir()
        source.mkdir()
        bin_dir.mkdir()
        _write_safe_getfacl(bin_dir)
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
                                "km_vms_resolve_product_source() { printf '%s\\n' \"$1\"; }",
                                "km_vms_compose_for_source() { shift 2; km_vms_compose_cmd \"$@\"; }",
                        ]
                    )
                + "\n",
                encoding="utf-8",
            )
            (root / "docs/INSTALL.md").write_text("# install\n", encoding="utf-8")
            (root / "release/km-vms-release.json").write_text('{"schema_version":1,"version":"0.7.1"}\n', encoding="utf-8")
            (root / "release/km-vms-update-lineage.json").write_text(
                read("release/km-vms-update-lineage.json"),
                encoding="utf-8",
            )
            _write_permission_chain_fixture(root)
        (source / "scripts/update.sh").write_text(script, encoding="utf-8")
        (app / "scripts/update.sh").write_text(script, encoding="utf-8")
        (app / ".env").write_text("HTTP_PORT=18182\nCOMPOSE_PROJECT_NAME=kmvmsfixture\n", encoding="utf-8")
        (app / "data/previews").mkdir(parents=True)
        (app / "data/sentinel.txt").write_text("keep\n", encoding="utf-8")
        _write_stable_runtime_fixture(app)

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
                    'output=""',
                    'headers=""',
                    "for arg in \"$@\"; do",
                    '  [ "$prev" = "-o" ] && output="$arg"',
                    '  [ "$prev" = "--dump-header" ] && headers="$arg"',
                    "  prev=\"$arg\"",
                    "done",
                    "cp '" + str(tarball) + "' \"$output\"",
                    "printf 'HTTP/1.1 200 OK\\r\\n\\r\\n' > \"$headers\"",
                    "exit 0",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(bin_dir / "curl", 0o755)

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
        for relative in dangerous_files:
            assert not (app / relative).exists(), relative
        assert not (app / "apps/api/stage607_legitimate_source.py").exists()
        assert not (app / "apps/api/token_serializer.py").exists()
        assert not (app / "apps/api/secret_policy.py").exists()
        for relative in product_assets:
            assert not (app / relative).exists(), relative
        assert (app / ".env").read_text(encoding="utf-8").startswith("HTTP_PORT=18182")
        assert (app / "data/sentinel.txt").read_text(encoding="utf-8") == "keep\n"
        assert not (app / ".km-vms-update.json").exists()


def test_update_script_final_identity_preserves_bind_mount_inode_and_verifies_api_visibility():
    script = read("scripts/update.sh")

    assert 'cat "$tmp_identity" > "$identity"' in script
    assert 'mv "$tmp_identity" "$identity"' in script
    assert "verify_api_visible_release_identity" in script
    assert "API-visible release identity is stale or incomplete" in script
    assert "up -d --force-recreate api" in script
    assert "release_identity_api_visible" in script
    assert "release_identity_commit_verified" in script


def test_update_slot_runtime_verifies_api_identity_before_commit():
    bridge = read("scripts/km-vms-update-helper-bridge.py")

    verify_block = bridge[
        bridge.index("def capture_slot_runtime_binding") :
        bridge.index("def verify_slot_runtime")
    ]
    commit_block = bridge[
        bridge.index('        if phase == "committing_target":') :
        bridge.index('        if phase == "rolling_back":')
    ]
    assert "_api_visible_release_identity(" in verify_block
    assert "_api_visible_identity_digest(" in verify_block
    assert '"metadata_status": "complete"' in verify_block
    assert '"identity_validity": (' in verify_block
    assert '"inventory_bound" if initial_identity else "valid"' in verify_block
    assert "verify_slot_runtime(" in commit_block
    assert '"completed"' in commit_block


def test_update_slot_identity_failure_uses_rollback_path():
    bridge = read("scripts/km-vms-update-helper-bridge.py")
    verifying = bridge[
        bridge.index('        if phase == "verifying_target":') :
        bridge.index('        if phase == "committing_target":')
    ]

    assert '"target_identity_mismatch"' in verifying
    assert "rollback_activation(" in verifying
    assert "target_health_failed" in verifying


def test_update_slot_identity_comparison_requires_exact_commit_and_version():
    bridge = read("scripts/km-vms-update-helper-bridge.py")
    identity = bridge[
        bridge.index("def capture_slot_runtime_binding") :
        bridge.index("def verify_slot_runtime")
    ]

    assert '"version": binding["version"]' in identity
    assert '"commit": binding["commit"]' in identity
    assert "slot_runtime_identity_mismatch" in identity


def test_update_helper_requires_complete_host_and_api_visible_identity():
    helper = read("scripts/km-vms-update-helper.py")

    assert "release_identity_api_visible" in helper
    assert "release_identity_commit_verified" in helper
    assert '"metadata_invalid"' in helper
    assert '"Release identity verification is incomplete."' in helper
    assert 'completed["release_identity"] = release_identity' in helper


def test_docs_describe_terminal_update_without_future_stage_claims():
    docs = read("docs/INSTALL.md")

    for required in (
        "## Terminal Update",
        "curl -fsSL https://raw.githubusercontent.com/kmishnev87/km-vms/main/scripts/install.sh",
        "bootstrap/current/km-vms-update-launcher.sh",
        '--app-dir "$HOME/km-vms" --branch vX.Y.Z --yes',
        '--app-dir "$HOME/km-vms" --branch vX.Y.Z --dry-run',
        "sh scripts/km-vms-release-cycle.sh --check",
        "git tag -a vX.Y.Z",
        ".km-vms-release.json",
        "--github-private",
        "KM_VMS_GITHUB_TOKEN",
        ".env",
        "data/",
        "activation returns to the exact captured previous slot",
        "bounded in-app apply orchestration",
        "dedicated `update-helper` service",
    ):
        assert required in docs

    assert "down -v" in docs
    assert "docker system prune" in docs
    assert "delete Docker volumes" in docs
    assert "automatically runs database migrations" not in docs
    assert "sh scripts/update.sh" not in docs
    assert "sh scripts/km-vms-restart.sh" not in docs


def test_update_permission_bridge_separates_existing_and_target_gates_without_state_reset():
    script = read("scripts/update.sh")
    bridge = read("scripts/km-vms-update-helper-bridge.py")

    assert '--contract source --check --app-dir "$PRODUCT_SOURCE_DIR"' in script
    assert '--contract stable-runtime --fix --app-dir "$APP_DIR"' in script
    assert '--contract source --check --app-dir "$TMP_ROOT/source"' in script
    assert "install_stable_bootstrap_for_handoff" in bridge
    assert "stable-prebootstrap" in bridge
    assert "stable-runtime" in bridge
    assert "--preflight-existing" not in script[script.index("preflight_permission_policy()") : script.index("apply_permission_policy()")]
    assert script.count('UPDATE_HELPER_IMAGE_PREPARED=0') == 1
    assert script.count('UPDATE_HELPER_REFRESH_SCHEDULED=0') == 1

    main = script[script.index('confirm "Apply KM VMS update now?"') :]
    preflight_i = main.index("\npreflight_permission_policy\n")
    handoff_i = main.index("\nprepare_schema_handoff\n")
    target_i = main.index("\nprepare_trusted_target_slot\n")
    activation_i = main.index("\nactivate_trusted_target_slot\n")
    success_metadata_i = main.index(
        '\nwrite_update_metadata "success" ""\n'
    )
    assert (
        preflight_i
        < handoff_i
        < target_i
        < activation_i
        < success_metadata_i
    )


def test_current_update_path_uses_target_prepare_before_atomic_activation():
    script = read("scripts/update.sh")
    main = script[script.index('confirm "Apply KM VMS update now?"') :]

    handoff_i = main.index("prepare_schema_handoff\n")
    staged_compose_i = main.index("staged_compose_config\n")
    target_i = main.index("prepare_trusted_target_slot\n")
    activation_i = main.index("activate_trusted_target_slot\n")
    assert (
        handoff_i
        < staged_compose_i
        < target_i
        < activation_i
    )

    for obsolete_call in (
        "prepare_schema_candidate_image\n",
        "run_schema_preflight\n",
        "stop_schema_writers\n",
        "run_schema_migration\n",
        "overlay_source\n",
        "rebuild_recreate\n",
    ):
        assert obsolete_call not in main
    assert "--terminal" in script
    assert "ensure_activation_request_id" in main
    assert "docker image rm -f \"$SCHEMA_CANDIDATE_IMAGE\"" in script


def test_schema_candidate_preserves_backup_and_multi_root_mount_contract():
    script = read("scripts/update.sh")
    compose = read("docker-compose.yml")
    compose_common = read("scripts/km-vms-compose-common.sh")

    assert "KMVMS_HOST_DB_BACKUP_ROOT" in script
    assert "KMVMS_DB_BACKUP_ROOT" in script
    assert "archive_roots_compose_file" in script
    assert "km_vms_compose_for_source" in script
    assert '-f "$archive_override"' in compose_common
    schema_service = compose.split("  schema-update:", 1)[1].split(
        "\n  api:",
        1,
    )[0]
    assert "KMVMS_DB_BACKUP_ROOT:" in schema_service
    assert "KMVMS_HOST_DB_BACKUP_ROOT" in schema_service
    assert "/storage/backups/db" in schema_service


def test_permission_gate_splits_source_stable_and_bounded_legacy_contracts():
    gate = read("scripts/km-vms-permission-gate.sh")

    assert 'CONTRACT="source"' in gate
    assert "--preflight-existing" in gate
    assert 'CONTRACT="legacy"' in gate
    assert "source|stable-prebootstrap|stable-runtime|legacy" in gate
    assert "TARGET_ONLY_PRIVILEGED_FILES=" in gate
    assert "scripts/km-vms-permission-gate.sh" in gate
    assert "scripts/km-vms-update-helper-bridge.py" in gate
    assert "scripts/km-vms-release-slots.py" in gate
    assert 'if [ "$CONTRACT" = "source" ] || [ "$CONTRACT" = "legacy" ]' in gate
    assert 'if [ "$CONTRACT" = "stable-runtime" ]' in gate
    assert "permission_contract=%s" in gate


def test_update_helper_bridge_uses_bounded_json_and_exact_target_image_activation():
    bridge = read("scripts/km-vms-update-helper-bridge.py")
    compose = read("docker-compose.yml")

    assert "MAX_CONTROL_BYTES = 64 * 1024" in bridge
    assert "path.stat().st_size > MAX_CONTROL_BYTES" in bridge
    assert "payload = json.loads(path.read_text" in bridge
    assert "Update control data must be a JSON object." in bridge
    assert "validate_completed_status(payload, request_id)" in bridge
    assert 'payload.get("status") != "completed"' in bridge
    assert 'payload.get("commit_verified") is not True' in bridge
    assert "expected_commit.lower() != installed_commit.lower()" in bridge
    assert '"--force-recreate",' in bridge
    assert '"update-helper",' in bridge
    assert '"--no-build",' in bridge
    assert "getfacl --version" not in bridge
    assert "DEFAULT_TIMEOUT_SECONDS = 7800" in bridge
    assert "docker:27-cli" not in bridge
    assert '"scripts/km-vms-permission-gate.sh"' in bridge
    assert 'def run_target_permission_gate(app_dir: Path) -> None:' in bridge
    gate_call = bridge.index("    run_target_permission_gate(app_dir)\n")
    image_call = bridge.index("    expected_image_id = docker_image_id(helper_image)\n")
    receipt_call = bridge.index("    validate_receipt_binding(receipt_file, request_id, expected_image_id)\n")
    schedule_call = bridge.index("    result = schedule_refresh(\n")
    assert gate_call < image_call < receipt_call < schedule_call
    assert 'check=False,' in bridge[bridge.index("def run_target_permission_gate"):schedule_call]
    assert '"permission_gate=PASS"' in bridge
    assert (
        "/host-app/data/update-runtime/bootstrap/current/"
        "km-vms-bootstrap.py"
    ) in compose
    assert "run-role update-helper" in compose
    assert (
        "/data/update-runtime/active/scripts/"
        "km-vms-update-helper-bridge.py"
    ) not in compose
    assert "run-role update-helper-bootstrap" in compose
    assert "km-vms-bootstrap-dispatch.sh" in compose
    assert (
        'expected_image_id,\n        "-B",\n        str(script_path),'
        in bridge
    )


def test_rebuild_resets_one_shots_and_does_not_retry_terminal_schema_failure():
    script = read("scripts/update.sh")
    rebuild = script[
        script.index("rebuild_recreate() {"):
        script.index("\nhealth_check() {")
    ]

    assert (
        'UPDATE_ONE_SHOT_SERVICES="update-helper-bootstrap '
        'schema-update"'
        in script
    )
    assert "compose_service_failed()" in script
    assert "schema_pipeline_failed()" in script
    assert 'PHASE="schema_update"' in rebuild
    assert 'fail "Database schema preparation failed."' in rebuild
    assert "normalize_legacy_schema_override_service()" in script
    assert "reset_update_one_shots()" in script
    assert "reset_failed_update_bootstrap()" in script
    assert "compose_with_archive_roots ps -a -q" in script
    assert "docker inspect" in script

    reset_i = rebuild.index("      reset_update_one_shots\n")
    first_up_i = rebuild.index(
        "      compose_with_archive_roots up -d --build "
        "postgres redis api recorder web nginx || {"
    )
    terminal_i = rebuild.index(
        "        if schema_pipeline_failed; then\n"
    )
    bootstrap_i = rebuild.index(
        "        reset_failed_update_bootstrap\n"
    )
    retry_i = rebuild.rindex(
        "        compose_with_archive_roots up -d --build "
        "postgres redis api recorder web nginx"
    )
    assert reset_i < first_up_i < terminal_i < bootstrap_i < retry_i

    normalize_i = script.index(
        "normalize_legacy_schema_override_service\n"
    )
    compose_config_i = script.rindex("compose_config\n")
    assert normalize_i < compose_config_i
