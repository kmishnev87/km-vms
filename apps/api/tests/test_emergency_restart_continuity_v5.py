from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import schema_update_control, update_check


REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, relative: str):
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load("emergency_v5_bootstrap", "scripts/km-vms-bootstrap.py")
bridge = _load(
    "emergency_v5_bridge",
    "scripts/km-vms-update-helper-bridge.py",
)


def _restore_request(*, state: str = "claimed", terminal=None) -> dict:
    return {
        "schema": bootstrap.RESTORE_REQUEST_SCHEMA,
        "operation_id": "restore-" + ("a" * 32),
        "submission_id": "00000000-0000-4000-8000-000000000001",
        "intent": "restore_current_database",
        "requested_at": "2026-08-13T00:00:00Z",
        "updated_at": "2026-08-13T00:00:01Z",
        "requested_by": {},
        "artifact": {},
        "confirmed": True,
        "confirmation_phrase": "RESTORE KM VMS",
        "state": state,
        "claimed_at": "2026-08-13T00:00:01Z" if state != "admitted" else None,
        "terminal": terminal,
        "video_archive_scope": "excluded",
        "migration_auto_apply": False,
    }


def _restore_journal(phase: str) -> dict:
    return {
        "schema_version": 1,
        "operation_id": "restore-" + ("a" * 32),
        "submission_id": "00000000-0000-4000-8000-000000000001",
        "phase": phase,
        "recorded_at": "2026-08-13T00:00:02Z",
        "source_artifact_id": "kmvms-db-20260813T000000Z-aaaaaaaaaaaa",
        "pre_restore_backup_id": None,
        "destructive_started": phase != "writers_paused",
        "terminal_result": None,
        "reason_code": None,
        "video_archive_modified": False,
    }


def test_lifecycle_override_has_one_definition_per_service() -> None:
    rendered = bootstrap.render_lifecycle_override().decode("utf-8")
    for service in (*bootstrap.PERSISTENT_SERVICES, *bootstrap.ONE_SHOT_SERVICES):
        assert rendered.count(f"  {service}:\n") == 1
    assert rendered.count("    restart: always\n") == len(
        bootstrap.PERSISTENT_SERVICES
    )
    assert rendered.count('    restart: "no"\n') == len(
        bootstrap.ONE_SHOT_SERVICES
    )
    assert "bootstrap/current/km-vms-bootstrap.py" in rendered
    assert "bootstrap/current/km-vms-bootstrap-dispatch.sh" in rendered


def test_bootstrap_drops_only_unavailable_legacy_compose_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KM_VMS_DOCKER_COMPOSE",
        "/Volume1/@apps/DockerEngine/dockerd/bin/docker-compose",
    )
    monkeypatch.setenv(
        "KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE",
        "docker-compose",
    )
    monkeypatch.setattr(
        bootstrap.shutil,
        "which",
        lambda command: "/usr/local/bin/docker" if command == "docker" else None,
    )

    bootstrap.normalize_update_helper_compose_environment()

    assert "KM_VMS_DOCKER_COMPOSE" not in bootstrap.os.environ
    assert "KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE" not in bootstrap.os.environ


def test_bootstrap_preserves_available_container_compose_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE", "docker")
    monkeypatch.setattr(
        bootstrap.shutil,
        "which",
        lambda command: "/usr/local/bin/docker" if command == "docker" else None,
    )

    bootstrap.normalize_update_helper_compose_environment()

    assert bootstrap.os.environ["KM_VMS_UPDATE_HELPER_DOCKER_COMPOSE"] == "docker"


def test_helper_maps_only_project_directory_to_host_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "host-app"
    source = app / "data/update-runtime/slots/release-" / "source"
    source.mkdir(parents=True)
    host_root = "/Volume1/docker/km-vms"
    (app / ".env").write_text(
        f"KM_VMS_HOST_APP_DIR={host_root}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KM_VMS_UPDATE_HOST_APP_DIR", host_root)

    mapped = bridge.mapped_compose_project_directory(app, source)

    assert mapped == Path(host_root) / source.relative_to(app)


def test_compose_digest_is_namespace_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "host-app"
    source = app / "data/update-runtime/slots/release-a/source"
    source.mkdir(parents=True)
    host_root = "/Volume1/docker/km-vms"
    (app / ".env").write_text(
        f"KM_VMS_HOST_APP_DIR={host_root}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KM_VMS_UPDATE_HOST_APP_DIR", host_root)
    local_render = f"source: {source}\nstable: {app}\n"
    host_render = (
        f"source: {Path(host_root) / source.relative_to(app)}\n"
        f"stable: {host_root}\n"
    )

    assert bridge._normalized_compose_digest(
        local_render,
        app_dir=app,
        source_dir=source,
    ) == bridge._normalized_compose_digest(
        host_render,
        app_dir=app,
        source_dir=source,
    )


def test_reconcile_cleans_interrupted_duplicates_after_unhealthy_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {
        "compose_evidence": {
            "services": ["update-retry-admission", "api"],
        }
    }
    cleanup_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(bridge, "writer_policy_fence_active", lambda _app: False)
    monkeypatch.setattr(
        bridge,
        "slot_compose",
        lambda *_a, **_k: (["compose"], {}, tmp_path, manifest),
    )
    monkeypatch.setattr(
        bridge,
        "cleanup_interrupted_compose_recreates",
        lambda _app, _project, services: cleanup_calls.append(tuple(services)) or 0,
    )
    monkeypatch.setattr(
        bridge,
        "run_command",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""),
    )

    def readiness(_compose, service, **_kwargs):
        if service == "api":
            raise bridge.BridgeError(
                "slot_runtime_unhealthy",
                "synthetic unhealthy target",
            )

    monkeypatch.setattr(bridge, "wait_compose_service_ready", readiness)

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.reconcile_slot_runtime(
            tmp_path,
            "fixture",
            "release-" + ("a" * 40),
            engine=object(),
        )

    assert captured.value.code == "slot_runtime_unhealthy"
    assert cleanup_calls[-1] == ("update-retry-admission", "api")


def test_activation_entry_is_serialized_by_one_convergence_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    events: list[object] = []
    monkeypatch.setattr(bridge, "require_app_dir", lambda _value: app)
    monkeypatch.setattr(
        bridge,
        "_activate_or_resume",
        lambda _args: events.append("handler") or 17,
    )
    monkeypatch.setattr(
        bridge.fcntl,
        "flock",
        lambda _fd, mode: events.append(mode),
    )
    args = type("Args", (), {"app_dir": str(app)})()

    assert bridge.activate_or_resume(args) == 17
    assert events == [
        bridge.fcntl.LOCK_EX,
        "handler",
        bridge.fcntl.LOCK_UN,
    ]
    assert (app / "data/update-control/activation-convergence.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("phase", "migration_required", "migration_invoked", "expected"),
    [
        ("target_prepared", True, False, False),
        ("quiescing", True, False, True),
        ("verifying_target", True, True, True),
        ("verifying_target", False, False, False),
        ("blocked", True, True, True),
        ("completed", True, True, False),
    ],
)
def test_activation_writer_fence_is_derived_from_validated_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    migration_required: bool,
    migration_invoked: bool,
    expected: bool,
) -> None:
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    journal = {
        "phase": phase,
        "schema": {
            "migration_required": migration_required,
            "migration_invoked": migration_invoked,
        },
    }
    engine = type("Engine", (), {"read_activation_journal": lambda *_a, **_k: journal})()
    monkeypatch.setattr(bootstrap, "validate_current_bundle", lambda _app: (app, {}))
    monkeypatch.setattr(bootstrap, "load_slot_engine", lambda _bundle: engine)
    monkeypatch.setattr(bootstrap, "read_json", lambda *_a, **_k: None)
    assert bootstrap.writer_isolation_active(app) is expected


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("preflight", False),
        ("pre_restore_backup", False),
        ("writers_paused", True),
        ("restore_running", True),
        ("post_restore_check", True),
    ],
)
def test_restore_writer_fence_uses_existing_request_and_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected: bool,
) -> None:
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    engine = type("Engine", (), {"read_activation_journal": lambda *_a, **_k: None})()
    request = _restore_request()
    journal = _restore_journal(phase)

    def fake_read(path: Path, *, missing_ok: bool = False):
        del missing_ok
        if path.name == "restore-request.json":
            return request
        if path.name == "restore-journal.json":
            return journal
        return None

    monkeypatch.setattr(bootstrap, "validate_current_bundle", lambda _app: (app, {}))
    monkeypatch.setattr(bootstrap, "load_slot_engine", lambda _bundle: engine)
    monkeypatch.setattr(bootstrap, "read_json", fake_read)
    assert bootstrap.writer_isolation_active(app) is expected


def test_failed_recovery_required_remains_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    engine = type("Engine", (), {"read_activation_journal": lambda *_a, **_k: None})()
    request = _restore_request(
        state="terminal",
        terminal={"status": "failed_recovery_required"},
    )
    monkeypatch.setattr(bootstrap, "validate_current_bundle", lambda _app: (app, {}))
    monkeypatch.setattr(bootstrap, "load_slot_engine", lambda _bundle: engine)
    monkeypatch.setattr(
        bootstrap,
        "read_json",
        lambda path, **_k: request if path.name == "restore-request.json" else None,
    )
    assert bootstrap.writer_isolation_active(app) is True


def test_bridge_fences_exact_writers_before_schema_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        bridge,
        "set_writer_policy_fence",
        lambda *_a, **_k: events.append("fence"),
    )
    monkeypatch.setattr(
        bridge,
        "slot_compose",
        lambda *_a, **_k: (["compose"], {}, tmp_path, {}),
    )

    def fake_run(command, **_kwargs):
        if command[:2] == ["compose", "stop"]:
            events.append("stop")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(bridge, "run_command", fake_run)
    bridge.stop_slot_schema_writers(
        tmp_path,
        "fixture",
        "release-" + ("a" * 40),
        engine=object(),
    )
    assert events == ["fence", "stop"]


def test_runtime_callers_do_not_fallback_to_stable_root_source() -> None:
    common = (REPO_ROOT / "scripts/km-vms-compose-common.sh").read_text()
    storage = (REPO_ROOT / "scripts/km-vms-storage-apply.sh").read_text()
    helper = (REPO_ROOT / "scripts/km-vms-update-helper.py").read_text()
    restart = (REPO_ROOT / "scripts/km-vms-restart.sh").read_text()
    assert 'printf \'%s\\n\' "$stable_app_dir"' not in common
    assert '"$APP_DIR/docker-compose.yml"' not in storage
    assert "bootstrap/current/km-vms-bootstrap.py" in helper
    assert "resolve-path" in restart and "--repair" in restart
    assert 'KM_VMS_RELEASE_IMAGE_TAG="$KM_VMS_COMPOSE_SLOT_ID"' in common
    assert 'initial-[0-9a-f]{64}' in common


def test_stable_setup_dispatch_uses_stable_operator_scripts() -> None:
    dispatch = (REPO_ROOT / "scripts/km-vms-bootstrap-dispatch.sh").read_text()
    setup = (REPO_ROOT / "scripts/km-vms-setup-activation-helper.sh").read_text()
    assert 'helper="$BUNDLE/km-vms-setup-activation-helper.sh"' in dispatch
    assert "KM_VMS_OPERATOR_SCRIPTS_DIR" in dispatch
    assert 'sh "$OPERATOR_SCRIPTS_DIR/km-vms-restart.sh"' in setup
    assert 'sh "$OPERATOR_SCRIPTS_DIR/km-vms-storage-apply.sh"' in setup


def _write_manual_update_launcher_fixture(tmp_path: Path) -> dict[str, Path]:
    app = tmp_path / "stable-app"
    bundle = app / "data/update-runtime/bootstrap/bundles/bundle-fixture"
    bundle.mkdir(parents=True)
    current = app / "data/update-runtime/bootstrap/current"
    current.symlink_to("bundles/bundle-fixture")
    app.joinpath(".env").write_text("COMPOSE_PROJECT_NAME=fixture\n", encoding="utf-8")

    launcher = bundle / "km-vms-update-launcher.sh"
    launcher.write_text(
        (REPO_ROOT / "scripts/km-vms-update-launcher.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    resolver_log = tmp_path / "resolver.log"
    repair_marker = tmp_path / "repair-called"
    bundle.joinpath("km-vms-bootstrap.py").write_text(
        """import os
import pathlib
import sys

pathlib.Path(os.environ["KM_VMS_TEST_RESOLVER_LOG"]).write_text(
    "\\n".join(sys.argv[1:]) + "\\n", encoding="utf-8"
)
if "--repair" in sys.argv:
    pathlib.Path(os.environ["KM_VMS_TEST_REPAIR_MARKER"]).write_text("called\\n")
    raise SystemExit(90)
if os.environ.get("KM_VMS_TEST_RESOLVER_MODE") != "valid":
    raise SystemExit(75)
print(os.environ["KM_VMS_TEST_ACTIVE_SOURCE"])
""",
        encoding="utf-8",
    )

    slot_id = "release-" + ("a" * 40)
    source = app / "data/update-runtime/slots" / slot_id / "source"
    source.joinpath("scripts").mkdir(parents=True)
    update_marker = tmp_path / "active-update.json"
    source.joinpath("scripts/update.sh").write_text(
        """#!/usr/bin/env sh
set -eu
{
  printf 'cwd=%s\\n' "$PWD"
  printf 'source=%s\\n' "$KM_VMS_PRODUCT_SOURCE_DIR"
  if [ -n "${KM_VMS_GITHUB_TOKEN:-}" ]; then printf 'token=present\\n'; fi
  for argument in "$@"; do printf 'arg=%s\\n' "$argument"; done
} > "$KM_VMS_TEST_UPDATE_MARKER"
""",
        encoding="utf-8",
    )
    app.joinpath("scripts").mkdir()
    root_marker = tmp_path / "root-update-called"
    app.joinpath("scripts/update.sh").write_text(
        '#!/usr/bin/env sh\nprintf "called\\n" > "$KM_VMS_TEST_ROOT_MARKER"\n',
        encoding="utf-8",
    )

    active = app / "data/update-runtime/active"
    active.symlink_to(f"slots/{slot_id}/source")
    journal = app / "data/update-control/activation-journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"phase":"completed"}\n', encoding="utf-8")
    projection = app / "data/update-runtime/installed-projection/installed-slot.json"
    projection.parent.mkdir(parents=True)
    projection.write_text('{"slot":"fixture"}\n', encoding="utf-8")
    return {
        "app": app,
        "launcher": current / "km-vms-update-launcher.sh",
        "source": source,
        "resolver_log": resolver_log,
        "repair_marker": repair_marker,
        "update_marker": update_marker,
        "root_marker": root_marker,
        "journal": journal,
        "projection": projection,
        "active": active,
    }


def _manual_update_environment(fixture: dict[str, Path], mode: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "KM_VMS_TEST_RESOLVER_LOG": str(fixture["resolver_log"]),
            "KM_VMS_TEST_REPAIR_MARKER": str(fixture["repair_marker"]),
            "KM_VMS_TEST_RESOLVER_MODE": mode,
            "KM_VMS_TEST_ACTIVE_SOURCE": str(fixture["source"]),
            "KM_VMS_TEST_UPDATE_MARKER": str(fixture["update_marker"]),
            "KM_VMS_TEST_ROOT_MARKER": str(fixture["root_marker"]),
            "KM_VMS_GITHUB_TOKEN": "fixture-token-never-print",
        }
    )
    return environment


def test_manual_update_launcher_dispatches_only_to_read_only_active_source(
    tmp_path: Path,
) -> None:
    fixture = _write_manual_update_launcher_fixture(tmp_path)
    environment = _manual_update_environment(fixture, "valid")
    active_before = fixture["active"].readlink()
    journal_before = fixture["journal"].read_bytes()
    projection_before = fixture["projection"].read_bytes()

    help_result = subprocess.run(
        ["sh", str(fixture["launcher"]), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert help_result.returncode == 0
    assert not fixture["resolver_log"].exists()

    result = subprocess.run(
        [
            "sh",
            str(fixture["launcher"]),
            "--app-dir",
            str(fixture["app"]),
            "--branch",
            "v9.9.9",
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert fixture["resolver_log"].read_text(encoding="utf-8").splitlines() == [
        "resolve-path",
        "--app-dir",
        str(fixture["app"]),
    ]
    assert not fixture["repair_marker"].exists()
    assert not fixture["root_marker"].exists()
    assert fixture["update_marker"].read_text(encoding="utf-8").splitlines() == [
        f'cwd={fixture["app"]}',
        f'source={fixture["source"]}',
        "token=present",
        "arg=--branch",
        "arg=v9.9.9",
        "arg=--dry-run",
    ]
    assert "fixture-token-never-print" not in result.stdout
    assert "fixture-token-never-print" not in result.stderr
    assert fixture["active"].readlink() == active_before
    assert fixture["journal"].read_bytes() == journal_before
    assert fixture["projection"].read_bytes() == projection_before


@pytest.mark.parametrize("mode", ("missing", "conflict", "nonterminal", "blocked"))
def test_manual_update_launcher_fails_closed_without_read_only_authority(
    tmp_path: Path,
    mode: str,
) -> None:
    fixture = _write_manual_update_launcher_fixture(tmp_path)
    environment = _manual_update_environment(fixture, mode)
    active_before = fixture["active"].readlink()
    journal_before = fixture["journal"].read_bytes()
    projection_before = fixture["projection"].read_bytes()

    result = subprocess.run(
        [
            "sh",
            str(fixture["launcher"]),
            "--app-dir",
            str(fixture["app"]),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "--repair" not in fixture["resolver_log"].read_text(encoding="utf-8")
    assert not fixture["repair_marker"].exists()
    assert not fixture["update_marker"].exists()
    assert not fixture["root_marker"].exists()
    assert fixture["active"].readlink() == active_before
    assert fixture["journal"].read_bytes() == journal_before
    assert fixture["projection"].read_bytes() == projection_before


def test_stable_bootstrap_bundle_covers_manual_update_launcher(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    app.joinpath(".env").write_text("COMPOSE_PROJECT_NAME=fixture\n", encoding="utf-8")
    app.joinpath("data").mkdir()
    source = tmp_path / "source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    for name in bootstrap.SOURCE_FILES:
        scripts.joinpath(name).write_bytes(REPO_ROOT.joinpath("scripts", name).read_bytes())

    installed = bootstrap.install_bundle(app, source)
    bundle = Path(installed["bundle_path"])
    assert "km-vms-update-launcher.sh" in installed["manifest"]["files"]
    bootstrap.validate_current_bundle(app)

    launcher = bundle / "km-vms-update-launcher.sh"
    original = launcher.read_bytes()
    launcher.chmod(0o755)
    launcher.write_bytes(original + b"# tampered\n")
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.validate_current_bundle(app)
    launcher.write_bytes(original)
    launcher.chmod(0o555)
    bootstrap.validate_current_bundle(app)
    launcher.unlink()
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.validate_current_bundle(app)


def test_fresh_install_request_identity_is_accepted_by_bridge() -> None:
    install = (REPO_ROOT / "scripts/install.sh").read_text()
    bridge_source = (
        REPO_ROOT / "scripts/km-vms-update-helper-bridge.py"
    ).read_text()

    assert 'print("terminal-" + uuid.uuid4().hex)' in install
    assert 'r"^(?:update|stage609|terminal)-[0-9a-f]{32}$"' in bridge_source


def test_vendor_compose_yaml_image_plan_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = """name: fixture
services:
  api:
    image: fixture-api:initial-abc
  postgres:
    image: postgres:16-alpine
networks:
  default: {}
"""
    monkeypatch.setattr(
        bridge,
        "run_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=rendered,
            stderr="",
        ),
    )

    assert bridge._compose_image_refs(["compose"], env={}) == {
        "api": "fixture-api:initial-abc",
        "postgres": "postgres:16-alpine",
    }


def test_frozen_source_contract_accepts_read_execute_directories() -> None:
    gate = (REPO_ROOT / "scripts/km-vms-permission-gate.sh").read_text()
    assert "scripts/km-vms-update-launcher.sh" in gate
    assert "km-vms-update-launcher.sh km-vms-storage-apply.sh" in gate
    assert 'if [ "$CONTRACT" = "source" ]; then' in gate
    assert 'mode_has_bits "$mode" 500' in gate
    assert (
        "Privileged source directory owner must have read/execute access"
        in gate
    )
    assert 'mode_has_bits "$mode" 700' in gate
    assert 'if [ "$ACTION" != "fix" ]; then\n    check_path_acl' in gate
    assert 'trusted_owner "$path" ||' in gate


def _legacy_stable_permission_fixture(root: Path) -> Path:
    app = root / "legacy-app"
    for relative in (
        "data/install-control",
        "data/update-control",
        "data/update-runtime/slots",
        "data/update-runtime/staging",
    ):
        (app / relative).mkdir(parents=True, exist_ok=True)
    (app / ".env").write_text("COMPOSE_PROJECT_NAME=fixture\n", encoding="utf-8")
    return app


def test_stable_prebootstrap_accepts_legacy_root_without_installer_receipt(
    tmp_path: Path,
) -> None:
    app = _legacy_stable_permission_fixture(tmp_path)
    result = subprocess.run(
        [
            "sh",
            str(REPO_ROOT / "scripts/km-vms-permission-gate.sh"),
            "--contract",
            "stable-prebootstrap",
            "--check",
            "--app-dir",
            str(app),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "permission_gate=PASS" in result.stdout


def test_optional_installer_receipt_is_still_permission_checked(
    tmp_path: Path,
) -> None:
    app = _legacy_stable_permission_fixture(tmp_path)
    receipt = app / ".km-vms-install.json"
    receipt.write_text("{}\n", encoding="utf-8")
    receipt.chmod(0o666)
    result = subprocess.run(
        [
            "sh",
            str(REPO_ROOT / "scripts/km-vms-permission-gate.sh"),
            "--contract",
            "stable-prebootstrap",
            "--check",
            "--app-dir",
            str(app),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode != 0
    assert "Runtime authority is world-writable" in result.stderr


def test_vendor_compose_yaml_security_contract_remains_exact_and_path_neutral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    candidate = app / "data/update-runtime/staging/update-" / "source"
    final = app / "data/update-runtime/slots/adopted-" / "source"
    request_id = "terminal-" + ("a" * 32)
    current_yaml = (
        "name: fixture\nservices:\n  api:\n"
        f"    command: [apply, {request_id}]\n"
        f"    volumes:\n      - type: bind\n        source: {app}/release\n"
    )
    adopted_yaml = (
        "name: fixture\nservices:\n  api:\n"
        f"    command: [apply, {request_id}]\n"
        f"    volumes:\n      - type: bind\n        source: {final}/release\n"
    )
    outputs = iter((current_yaml, adopted_yaml))
    monkeypatch.setattr(
        bridge,
        "run_command",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, next(outputs), ""
        ),
    )
    current = bridge._current_compose_security_contract(
        ["compose"],
        app_dir=app,
        source_dir=app,
        request_id=request_id,
    )
    adopted = bridge._current_compose_security_contract(
        ["compose"],
        app_dir=app,
        source_dir=candidate,
        request_id=request_id,
        adopted_final_source=final,
    )
    assert current == adopted
    assert current["format"] == "canonical_yaml_sha256"


def test_slot_http_probe_runs_inside_compose_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        bridge,
        "run_command",
        lambda command, **kwargs: (
            calls.append((list(command), kwargs))
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    bridge.probe_slot_http(["compose"], env={"SAFE": "1"})
    command, kwargs = calls[0]
    assert command[:6] == [
        "compose",
        "exec",
        "-T",
        "api",
        "python3",
        "-c",
    ]
    assert "http://nginx/api/health" in command[-1]
    assert kwargs["env"] == {"SAFE": "1"}
    assert kwargs["check"] is False


def test_bootstrap_materializes_exact_slot_image_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / ".env").write_text("COMPOSE_PROJECT_NAME=fixture\n", encoding="utf-8")
    image_id = "sha256:" + ("a" * 64)
    image_ref = "km-vms-fixture-slot-api:adopted-" + ("b" * 64)
    manifest = {
        "compose_evidence": {"services": ["api", "postgres"]},
        "image_evidence": {
            "services": {
                "api": {
                    "image_id": image_id,
                    "immutable_image_ref": image_ref,
                }
            }
        },
    }
    monkeypatch.setattr(
        bootstrap,
        "resolve_authority",
        lambda *_a, **_k: (
            "adopted-" + ("b" * 64),
            app / "source",
            manifest,
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "_docker",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, image_id + "\n", ""
        ),
    )
    path, slot_id = bootstrap.materialize_slot_image_override(
        app,
        "fixture",
    )
    assert slot_id == "adopted-" + ("b" * 64)
    assert path.parent == app / "data/update-runtime/derived-compose"
    assert image_ref in path.read_text(encoding="utf-8")
    assert "postgres:" not in path.read_text(encoding="utf-8")


def test_common_compose_includes_immutable_images_before_runtime_start() -> None:
    common = (REPO_ROOT / "scripts/km-vms-compose-common.sh").read_text()
    assert "image-override-path" in common
    assert "km_vms_slot_image_override" in common
    slot_branch = common.split(
        'if [ -n "$slot_image_override" ] && [ -n "$slot_runtime_override" ]',
        1,
    )[1].split("elif", 1)[0]
    compose_arguments = slot_branch.split("km_vms_compose_bound_cmd", 1)[1]
    assert compose_arguments.index('-f "$slot_runtime_override"') < compose_arguments.index(
        '-f "$slot_image_override"'
    )
    assert compose_arguments.index('-f "$slot_image_override"') < compose_arguments.index(
        '-f "$archive_override"'
    )
    assert compose_arguments.index('-f "$archive_override"') < compose_arguments.index(
        '-f "$lifecycle_override"'
    )


def test_common_compose_accepts_explicit_project_for_legacy_env() -> None:
    common = (REPO_ROOT / "scripts/km-vms-compose-common.sh").read_text()
    resolver = common.split("km_vms_slot_image_override()", 1)[1].split(
        "km_vms_compose_for_source()",
        1,
    )[0]
    assert 'project_name="${KM_VMS_PROJECT_NAME:-${PROJECT_NAME:-}}"' in resolver
    assert resolver.index('project_name="${KM_VMS_PROJECT_NAME:-${PROJECT_NAME:-}}"') < resolver.index(
        "COMPOSE_PROJECT_NAME="
    )


def test_inventory_bound_schema_token_is_not_a_fake_target_commit() -> None:
    slot_id = "initial-" + ("a" * 64)
    inventory = "b" * 64
    token = bridge.inventory_bound_source_token(slot_id, inventory)
    assert token == schema_update_control.inventory_bound_source_token(
        slot_id,
        inventory,
    )
    assert len(token) == 40
    assert token != "c" * 40


def test_inventory_bound_initial_can_be_exact_previous_for_first_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot_id = "initial-" + ("a" * 64)
    inventory = "b" * 64
    target_commit = "c" * 40
    token = bridge.inventory_bound_source_token(slot_id, inventory)
    monkeypatch.setattr(
        schema_update_control,
        "_source_identity_payload",
        lambda: {
            "request_id": "update-" + ("d" * 32),
            "installed_version": "0.8.14",
            "installed_commit": token,
            "identity_mode": "inventory_bound",
            "slot_id": slot_id,
            "inventory_sha256": inventory,
        },
    )
    version, installed, schema, shapes = (
        schema_update_control.expected_source_lineage(
            request_id="update-" + ("d" * 32),
            target_release="0.8.14",
            target_commit=target_commit,
        )
    )
    assert (version, installed) == ("0.8.14", token)
    assert schema == schema_update_control.TARGET_SCHEMA_VERSION
    assert shapes == schema_update_control.TARGET_SHAPE_FINGERPRINTS


def test_inventory_bound_same_version_is_not_offered_as_an_update() -> None:
    installed = SimpleNamespace(
        status="installed",
        installed_version="0.8.14",
        installed_commit="a" * 40,
        git_head=None,
        identity_validity="inventory_bound",
    )
    latest = SimpleNamespace(
        version="0.8.14",
        commit="b" * 40,
        minimum_current_version=None,
        requires_backup=False,
        requires_manual_action=False,
        requires_migration=False,
    )
    status, blockers, warnings = update_check._compare(installed, latest)
    assert status == "current_or_unknown"
    assert blockers == []
    assert [item.code for item in warnings] == [
        "inventory_bound_same_version_not_canonicalized"
    ]


def test_inventory_identity_is_persisted_in_each_schema_attempt() -> None:
    slot_id = "initial-" + ("a" * 64)
    inventory = "b" * 64
    token = schema_update_control.inventory_bound_source_token(
        slot_id,
        inventory,
    )

    class FixtureDb:
        added = None

        def get(self, *_args):
            return None

        def add(self, value):
            self.added = value

        def flush(self):
            return None

    db = FixtureDb()
    context = SimpleNamespace(
        request_id="update-" + ("c" * 32),
        admission_attempt_id="migration-attempt-" + ("d" * 32),
        target_release="0.8.15",
        target_commit="e" * 40,
        registry_fingerprint="f" * 64,
        plan_fingerprint="1" * 64,
        installed_version="0.8.14",
        installed_commit=token,
        source_identity_mode="inventory_bound",
        source_slot_id=slot_id,
        source_inventory_sha256=inventory,
    )
    schema_update_control.start_attempt(
        db,
        context=context,
        generation=1,
        transition_id="fixture_schema_transition",
        previous_version=8,
        target_version=9,
        definition_fingerprint="2" * 64,
        before_shape_fingerprint="3" * 64,
    )
    assert db.added.details == {
        "source_identity": {
            "identity_mode": "inventory_bound",
            "slot_id": slot_id,
            "inventory_sha256": inventory,
        }
    }


def test_handoff_retry_keeps_same_request_despite_new_observation_time() -> None:
    source = bridge.capture_installed_source_identity.__code__
    assert "requested_at" in source.co_consts
    bridge_source = (
        REPO_ROOT / "scripts/km-vms-update-helper-bridge.py"
    ).read_text()
    retry_guard = bridge_source.split(
        'request_path = control_dir / "schema-update-request.json"',
        1,
    )[1].split("return identity", 1)[0]
    assert 'if field != "requested_at"' in retry_guard
    assert 'existing_request.get("request_id") == request_id' in retry_guard


def test_nonterminal_journal_is_not_bypassed_by_existing_active_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    target_slot = "release-" + ("a" * 40)
    target_source = app / "data/update-runtime/slots" / target_slot / "source"
    target_source.mkdir(parents=True)
    target_binding = {"slot_id": target_slot}

    class Engine:
        @staticmethod
        def read_active_slot(_app):
            return target_slot, target_source

        @staticmethod
        def read_activation_journal(_app, *, missing_ok=False):
            del missing_ok
            return {
                "phase": "verifying_target",
                "target": target_binding,
            }

    monkeypatch.setattr(bootstrap, "validate_current_bundle", lambda _app: (app, {}))
    monkeypatch.setattr(bootstrap, "load_slot_engine", lambda _bundle: Engine())
    with pytest.raises(bootstrap.BootstrapError) as captured:
        bootstrap.resolve_authority(app, project_name="fixture", repair=False)
    assert captured.value.code == "activation_in_progress"


def test_read_only_resolution_does_not_repair_terminal_pointer_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    previous_slot = "release-" + ("b" * 40)
    target_slot = "release-" + ("a" * 40)
    previous_source = app / "data/update-runtime/slots" / previous_slot / "source"
    previous_source.mkdir(parents=True)
    target_binding = {"slot_id": target_slot}
    mutation_calls: list[str] = []

    class Engine:
        @staticmethod
        def read_active_slot(_app):
            return previous_slot, previous_source

        @staticmethod
        def read_activation_journal(_app, *, missing_ok=False):
            del missing_ok
            return {"phase": "completed", "target": target_binding}

        @staticmethod
        def atomic_switch_pointer(_app, _slot_id):
            mutation_calls.append("switch")

        @staticmethod
        def publish_installed_slot_projection(_app, *, binding):
            del binding
            mutation_calls.append("projection")

    monkeypatch.setattr(bootstrap, "validate_current_bundle", lambda _app: (app, {}))
    monkeypatch.setattr(bootstrap, "load_slot_engine", lambda _bundle: Engine())
    monkeypatch.setattr(bootstrap, "_binding_matches", lambda *_args: True)
    with pytest.raises(bootstrap.BootstrapError) as captured:
        bootstrap.resolve_authority(app, project_name="fixture", repair=False)
    assert captured.value.code == "active_pointer_conflict"
    assert mutation_calls == []


def test_interrupted_compose_cleanup_is_project_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text("KM_VMS_CONTAINER_PREFIX=fixture\n")
    calls: list[list[str]] = []
    rows = [
        {
            "Id": "a" * 64,
            "Name": "/fixture-api",
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "fixture-project",
                    "com.docker.compose.service": "api",
                }
            },
            "State": {"Running": False},
        },
        {
            "Id": "b" * 64,
            "Name": "/aaaaaaaaaaaa_fixture-api",
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "fixture-project",
                    "com.docker.compose.service": "api",
                }
            },
            "State": {"Running": False},
        },
    ]

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        if command[1:3] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=("a" * 64) + "\n" + ("b" * 64) + "\n",
                stderr="",
            )
        if command[1] == "inspect":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(rows),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(bridge, "run_command", fake_run)
    removed = bridge.cleanup_interrupted_compose_recreates(
        app,
        "fixture-project",
        ["api"],
    )
    assert removed == 1
    assert calls[-1] == ["docker", "rm", "b" * 64]
    assert "label=com.docker.compose.project=fixture-project" in calls[0]
    assert "label=com.docker.compose.service=api" in calls[0]


def test_interrupted_compose_cleanup_refuses_running_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text("KM_VMS_CONTAINER_PREFIX=fixture\n")
    row = {
        "Id": "b" * 64,
        "Name": "/aaaaaaaaaaaa_fixture-api",
        "Config": {
            "Labels": {
                "com.docker.compose.project": "fixture-project",
                "com.docker.compose.service": "api",
            }
        },
        "State": {"Running": True},
    }

    def fake_run(command, **_kwargs):
        stdout = ("b" * 64) + "\n" if command[1] == "ps" else json.dumps([row])
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(bridge, "run_command", fake_run)
    with pytest.raises(bridge.BridgeError) as captured:
        bridge.cleanup_interrupted_compose_recreates(
            app,
            "fixture-project",
            ["api"],
        )
    assert captured.value.code == "compose_recreate_evidence_invalid"


def test_activation_runtime_reconciliation_is_dependency_ordered() -> None:
    assert bridge.ACTIVATION_RUNTIME_SERVICES[:2] == (
        "update-retry-admission",
        "update-status-reader",
    )
    source = (
        REPO_ROOT / "scripts/km-vms-update-helper-bridge.py"
    ).read_text()
    assert "for service in services:" in source
    assert '"--force-recreate",\n                service,' in source
    assert "wait_compose_service_ready(" in source


def test_service_readiness_accepts_running_service_without_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_compose_container_id", lambda *_a, **_k: "a" * 64)
    monkeypatch.setattr(
        bridge,
        "run_command",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                [{"State": {"Running": True, "Status": "running"}}]
            ),
            stderr="",
        ),
    )
    bridge.wait_compose_service_ready(
        ["compose"],
        "web",
        env={},
        timeout_seconds=1,
    )
