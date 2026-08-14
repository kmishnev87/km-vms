from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
FROZEN_FIXTURE = REPO_ROOT / "apps/api/tests/fixtures/update/v0_8_15"
FROZEN_TAG = "v0.8.15"
FROZEN_COMMIT = "84206805ecc78043585388a39996cd732b373642"


def _load(name: str, relative: str):
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load("stage13722_bootstrap", "scripts/km-vms-bootstrap.py")
bridge = _load(
    "stage13722_bridge",
    "scripts/km-vms-update-helper-bridge.py",
)


def _active_project_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_name: str = "fixture-a",
) -> tuple[Path, str]:
    app = tmp_path / "app"
    (app / "data/update-runtime/slots").mkdir(parents=True)
    (app / ".env").write_text("TZ=UTC\n", encoding="utf-8")
    slot_id = "release-" + ("a" * 40)
    slot = app / "data/update-runtime/slots" / slot_id
    (slot / "source").mkdir(parents=True)
    manifest = {
        "slot_id": slot_id,
        "compose_evidence": {"project_name": project_name},
    }
    (slot / "slot-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (app / "data/update-runtime/active").symlink_to(
        f"slots/{slot_id}/source"
    )

    class Engine:
        @staticmethod
        def validate_manifest(value, *, expected_slot_id=None):
            assert value["slot_id"] == expected_slot_id
            return value

    monkeypatch.setattr(bootstrap, "load_slot_engine", lambda: Engine())
    return app, slot_id


def test_project_identity_resolves_from_validated_active_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _slot_id = _active_project_fixture(tmp_path, monkeypatch)

    assert bootstrap.read_project_name(app) == "fixture-a"


def test_project_identity_requires_consensus_across_present_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _slot_id = _active_project_fixture(tmp_path, monkeypatch)
    (app / ".env").write_text(
        "COMPOSE_PROJECT_NAME=fixture-b\n",
        encoding="utf-8",
    )

    with pytest.raises(bootstrap.BootstrapError) as captured:
        bootstrap.read_project_name(app, "fixture-a")

    assert captured.value.code == "project_identity_conflict"


def test_project_identity_accepts_permission_checked_matching_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _slot_id = _active_project_fixture(tmp_path, monkeypatch)
    receipt = app / ".km-vms-install.json"
    receipt.write_text(
        json.dumps(
            {
                "app_dir": str(app.resolve()),
                "project_name": "fixture-a",
            }
        ),
        encoding="utf-8",
    )
    receipt.chmod(0o644)

    assert bootstrap.read_project_name(app) == "fixture-a"


def test_project_identity_missing_without_any_trusted_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "data").mkdir()
    (app / ".env").write_text("TZ=UTC\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_legacy_docker_project_name", lambda: None)

    with pytest.raises(bootstrap.BootstrapError) as captured:
        bootstrap.read_project_name(app)

    assert captured.value.code == "project_identity_missing"


def test_project_identity_rejects_ambiguous_complete_legacy_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "data").mkdir()
    (app / ".env").write_text("TZ=UTC\n", encoding="utf-8")
    services = ("api", "web", "recorder", "postgres", "redis", "nginx")
    output = "".join(
        f"{project}\t{service}\n"
        for project in ("fixture-a", "fixture-b")
        for service in services
    )
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout=output,
            stderr="",
        ),
    )

    with pytest.raises(bootstrap.BootstrapError) as captured:
        bootstrap.read_project_name(app)

    assert captured.value.code == "project_identity_ambiguous"


def test_pre_contract_slot_does_not_run_writable_legacy_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.joinpath("data").mkdir(parents=True)
    active_source = tmp_path / "active-source"
    active_source.joinpath("scripts").mkdir(parents=True)
    old_gate = active_source / "scripts/km-vms-permission-gate.sh"
    old_gate.write_text("#!/bin/sh\necho permission_gate=PASS\n", encoding="utf-8")
    active_source.chmod(0o555)
    old_gate.chmod(0o555)
    target = tmp_path / "target"
    target.joinpath("scripts").mkdir(parents=True)
    target_gate = target / "scripts/km-vms-permission-gate.sh"
    target_gate.write_text("#!/bin/sh\n# --contract\n", encoding="utf-8")
    calls: list[tuple[Path, str]] = []

    monkeypatch.setattr(
        bridge,
        "_run_permission_contract",
        lambda gate, _root, *, contract, action, compatibility=False: calls.append(
            (gate, contract)
        ),
    )
    monkeypatch.setattr(
        bridge,
        "load_source_bootstrap",
        lambda _source: SimpleNamespace(install_bundle=lambda *_args: None),
    )
    engine = SimpleNamespace(ensure_layout=lambda _app: None)
    before = (active_source.stat().st_mode, old_gate.read_bytes())

    bridge.install_stable_bootstrap_for_handoff(
        app,
        target,
        engine=engine,
        active_source=active_source,
        contract_family="pre_contract_slot",
    )

    assert all(gate != old_gate for gate, _contract in calls)
    assert (active_source.stat().st_mode, old_gate.read_bytes()) == before
    assert [contract for _gate, contract in calls] == [
        "source",
        "stable-prebootstrap",
    ]


def test_current_contract_requires_its_published_projection(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.joinpath("data").mkdir(parents=True)
    source = tmp_path / "source"
    source.joinpath("scripts").mkdir(parents=True)
    source.joinpath("scripts/km-vms-permission-gate.sh").write_text(
        "#!/bin/sh\n# --contract\n",
        encoding="utf-8",
    )

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.classify_installed_update_contract(
            app,
            ("release-" + ("b" * 40), source),
            engine=SimpleNamespace(),
        )

    assert captured.value.code == "installed_projection_missing"


def test_contract_family_classification_keeps_legacy_and_pre_contract_bounded(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    app.joinpath("data").mkdir(parents=True)
    source = tmp_path / "source"
    source.joinpath("scripts").mkdir(parents=True)
    source.joinpath("scripts/km-vms-permission-gate.sh").write_text(
        "#!/bin/sh\necho permission_gate=PASS\n",
        encoding="utf-8",
    )

    assert bridge.classify_installed_update_contract(
        app,
        None,
        engine=SimpleNamespace(),
    ) == "legacy_root"
    assert bridge.classify_installed_update_contract(
        app,
        ("release-" + ("b" * 40), source),
        engine=SimpleNamespace(),
    ) == "pre_contract_slot"


def test_frozen_entry_without_project_return_channel_stops_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "app"
    app.joinpath("data").mkdir(parents=True)
    app.joinpath("scripts").mkdir()
    app.joinpath("scripts/update.sh").write_text(
        "#!/bin/sh\nPROJECT_NAME=${KM_VMS_PROJECT_NAME:-}\n",
        encoding="utf-8",
    )
    target = tmp_path / "target"
    target.joinpath("release").mkdir(parents=True)
    target.joinpath("scripts").mkdir()
    target.joinpath("release/km-vms-release.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    target.joinpath("scripts/km-vms-bootstrap.py").write_text(
        "# fixture\n",
        encoding="utf-8",
    )
    mutated = False

    def mutation_guard(*_args, **_kwargs):
        nonlocal mutated
        mutated = True
        raise AssertionError("handoff must stop before mutation")

    monkeypatch.setattr(bridge, "require_app_dir", lambda _value: app)
    monkeypatch.setattr(bridge, "load_source_bootstrap", mutation_guard)

    with pytest.raises(bridge.BridgeError) as captured:
        bridge.handoff(
            SimpleNamespace(
                app_dir=str(app),
                target_source_dir=str(target),
                request_id="update-" + ("c" * 32),
                project_name="",
                terminal=False,
            )
        )

    assert captured.value.code == "project_identity_recovery_required"
    assert mutated is False


def test_update_orchestration_reaches_handoff_before_installed_runtime_gates() -> None:
    script = (REPO_ROOT / "scripts/update.sh").read_text(encoding="utf-8")
    main = script[script.index('confirm "Apply KM VMS update now?"') :]
    assert '--contract source --check --app-dir "$PRODUCT_SOURCE_DIR"' not in script
    assert '--contract stable-runtime --fix --app-dir "$APP_DIR"' not in script
    assert main.index("preflight_permission_policy\n") < main.index(
        "prepare_schema_handoff\n"
    )
    assert script.index("preflight_target_permission_policy\n", script.index("preflight_permission_policy()")) < script.index(
        "prepare_schema_handoff()"
    )
    assert "resolved_project_name=" in script


def test_project_identity_consumers_have_no_guessed_product_fallback() -> None:
    paths = (
        "scripts/update.sh",
        "scripts/km-vms-update-helper-bridge.py",
        "scripts/km-vms-bootstrap.py",
        "scripts/km-vms-update-launcher.sh",
        "scripts/km-vms-update-helper.py",
        "scripts/km-vms-restart.sh",
        "scripts/km-vms-compose-common.sh",
    )
    for relative in paths:
        assert 'or "tnas-vms"' not in (
            REPO_ROOT / relative
        ).read_text(encoding="utf-8")


def test_adopted_development_owner_accepts_only_unique_buildable_services() -> None:
    assert bridge._adopted_development_build_services(["web", "api"]) == [
        "api",
        "web",
    ]
    for invalid in ([], ["api", "api"], ["nginx"]):
        with pytest.raises(bridge.BridgeError) as captured:
            bridge._adopted_development_build_services(invalid)
        assert captured.value.code == "adopted_development_services_invalid"


def test_adopted_development_cli_is_one_existing_slot_contract_wrapper() -> None:
    source = (REPO_ROOT / "scripts/km-vms-update-helper-bridge.py").read_text(
        encoding="utf-8"
    )
    block = source[
        source.index("def prepare_adopted_development_slot("):
        source.index("def bind_legacy_adopted_slot_as_active(")
    ]
    assert '"stage-adopted"' in block
    assert '"prepare-adopted-runtime"' in block
    assert '"finalize"' in block
    assert block.index('"stage-adopted"') < block.index(
        '"prepare-adopted-runtime"'
    ) < block.index('"finalize"')
    assert "preserve_slot_images(" in block
    assert "capture_pre_update_slot_evidence(" in block


@pytest.mark.parametrize("authority", [bootstrap, bridge])
def test_runtime_authority_module_load_does_not_write_bytecode(
    tmp_path: Path,
    authority,
) -> None:
    source_dir = tmp_path / authority.__name__
    source_dir.mkdir()
    module_path = source_dir / "authority.py"
    module_path.write_text("VALUE = 42\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        f"{authority.__name__}_fixture",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)

    authority._exec_module_without_bytecode(spec, module)

    assert module.VALUE == 42
    assert not (source_dir / "__pycache__").exists()


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _verified_frozen_manifest() -> dict:
    manifest_path = FROZEN_FIXTURE / "fixture-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "document_type": "km_vms_frozen_update_entry_fixture",
        "files": manifest["files"],
        "release_commit": FROZEN_COMMIT,
        "release_tag": FROZEN_TAG,
        "schema_version": 1,
    }
    assert len(manifest["files"]) == 12
    assert len({item["original_path"] for item in manifest["files"]}) == 12
    for item in manifest["files"]:
        relative = item["fixture_path"]
        assert relative == item["original_path"]
        path = FROZEN_FIXTURE / relative
        data = path.read_bytes()
        assert item["size"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["git_blob"] == _git_blob_sha1(data)
        assert item["git_mode"] in {"100644", "100755"}
        assert item["role"]
    return manifest


def _shell_variable_paths(script: str, variable: str) -> list[str]:
    match = re.search(rf'{re.escape(variable)}="\n(.*?)\n"', script, re.S)
    assert match, f"missing shell list {variable}"
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def _write_fixture_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)


def _copy_frozen_files(destination: Path, manifest: dict) -> None:
    for item in manifest["files"]:
        source = FROZEN_FIXTURE / item["fixture_path"]
        target = destination / item["fixture_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if item["git_mode"] == "100755" else 0o644)


def _complete_frozen_source(source: Path, manifest: dict) -> None:
    _copy_frozen_files(source, manifest)
    for relative, content in (
        ("apps/api/app.py", "# frozen fixture API\n"),
        ("apps/web/page.js", "// frozen fixture web\n"),
        ("apps/recorder/main.py", "# frozen fixture recorder\n"),
        ("apps/update-helper/Dockerfile", "FROM scratch\n"),
        ("deploy/nginx/default.conf", "events {}\nhttp {}\n"),
        ("docs/INSTALL.md", "# Frozen fixture\n"),
        ("docker-compose.yml", "services: {}\n"),
    ):
        _write_fixture_file(source / relative, content)

    gate = (source / "scripts/km-vms-permission-gate.sh").read_text(
        encoding="utf-8"
    )
    executable = set(_shell_variable_paths(gate, "EXECUTABLE_FILES"))
    required = {
        *_shell_variable_paths(gate, "BASE_PRIVILEGED_FILES"),
        *_shell_variable_paths(gate, "TARGET_ONLY_PRIVILEGED_FILES"),
    }
    for relative in sorted(required):
        path = source / relative
        if path.exists():
            continue
        suffix = Path(relative).suffix
        content = (
            "#!/bin/sh\nexit 0\n"
            if suffix == ".sh"
            else "#!/usr/bin/env python3\n"
            if suffix == ".py"
            else "FROM scratch\n"
            if Path(relative).name == "Dockerfile"
            else "fixture\n"
        )
        _write_fixture_file(path, content, executable=relative in executable)


def _compose_evidence(slots, *, project_name: str) -> dict:
    services = sorted(
        {
            "api",
            "nginx",
            "postgres",
            "recorder",
            "redis",
            "setup-helper",
            "update-helper",
            "web",
            "schema-update",
            "update-helper-bootstrap",
            "update-retry-admission",
            "update-status-reader",
        }
    )
    return {
        "schema_version": 1,
        "project_name": project_name,
        "project_directory": "source",
        "captured_plan_sha256": "a" * 64,
        "slot_plan_sha256": "b" * 64,
        "archive_override_attached": True,
        "archive_override_sha256": "c" * 64,
        "runtime_override_sha256": None,
        "shared_root_contract": "stable_app_dir_v1",
        "services": services,
    }


def _image_evidence(slots, slot_id: str) -> dict:
    services = {}
    for index, service in enumerate(
        sorted(slots.TARGET_REQUIRED_IMAGE_SERVICES), start=1
    ):
        source_ref = (
            f"fixture/{service}:{slot_id}"
            if service in slots.TARGET_BUILT_IMAGE_SERVICES
            else f"fixture/{service}:current"
        )
        services[service] = {
            "image_id": "sha256:" + f"{index:064x}",
            "source_image_ref": source_ref,
            "immutable_image_ref": f"km-vms-fixture-{service}:{slot_id}",
        }
    return {"schema_version": 1, "services": services}


def _prepare_frozen_app(root: Path, *, project_name: str) -> tuple[Path, Path, str]:
    manifest = _verified_frozen_manifest()
    app = root / "app"
    source = root / "frozen-source"
    (app / "data/install-control").mkdir(parents=True)
    (app / "data/update-control").mkdir(parents=True)
    (app / "data/postgres").mkdir(parents=True)
    (app / "data/redis").mkdir(parents=True)
    (app / "data/postgres/fixture.sentinel").write_text(
        "database-unchanged\n", encoding="utf-8"
    )
    (app / "data/fixture.sentinel").write_text("data-unchanged\n", encoding="utf-8")
    (app / ".env").write_text(
        f"COMPOSE_PROJECT_NAME={project_name}\n" if project_name else "TZ=UTC\n",
        encoding="utf-8",
    )
    _complete_frozen_source(source, manifest)

    frozen_slots = _load_file(
        f"frozen_slots_{root.name}",
        FROZEN_FIXTURE / "scripts/km-vms-release-slots.py",
    )
    frozen_bootstrap = _load_file(
        f"frozen_bootstrap_{root.name}",
        FROZEN_FIXTURE / "scripts/km-vms-bootstrap.py",
    )
    request_id = "update-" + "1" * 32
    staged = frozen_slots.stage_target(
        app,
        source,
        request_id=request_id,
        trusted_commit=FROZEN_COMMIT,
        declared_version="0.8.15",
    )
    finalized = frozen_slots.finalize_candidate(
        app,
        request_id=request_id,
        compose_evidence=_compose_evidence(
            frozen_slots,
            project_name="fixture-a",
        ),
        image_evidence=_image_evidence(frozen_slots, staged["slot_id"]),
    )
    slot_id = finalized["manifest"]["slot_id"]
    frozen_slots.atomic_switch_pointer(app, slot_id)
    binding = frozen_slots.build_activation_slot_binding(app, slot_id)
    frozen_slots.publish_installed_slot_projection(app, binding=binding)
    active_source = app / "data/update-runtime/slots" / slot_id / "source"
    frozen_bootstrap.install_bundle(app, active_source)

    for relative in (
        "docker-compose.yml",
        "scripts/km-vms-permission-gate.sh",
        "scripts/km-vms-update-helper-bridge.py",
    ):
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, target)
    return app, active_source, slot_id


def _copy_current_target_file(target_root: Path, relative: str) -> None:
    source = REPO_ROOT / relative
    assert source.is_file(), relative
    target = target_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _build_current_target_archive(root: Path) -> Path:
    target_root = root / "target-tree/km-vms-target"
    for directory in ("apps/api", "apps/web", "apps/recorder", "apps/update-helper"):
        (target_root / directory).mkdir(parents=True, exist_ok=True)
    gate_text = (REPO_ROOT / "scripts/km-vms-permission-gate.sh").read_text(
        encoding="utf-8"
    )
    required = {
        *_shell_variable_paths(gate_text, "BASE_PRIVILEGED_FILES"),
        *_shell_variable_paths(gate_text, "TARGET_ONLY_PRIVILEGED_FILES"),
        "deploy/nginx/default.conf",
        "docs/INSTALL.md",
        "release/km-vms-release.json",
        "release/km-vms-update-lineage.json",
    }
    for relative in sorted(required):
        _copy_current_target_file(target_root, relative)
    archive = root / "current-target.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(target_root, arcname="km-vms-target", recursive=True)
    return archive


PYTHON_BRIDGE_HARNESS = r'''from __future__ import annotations
import contextlib
import io
import importlib.util
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
bridge_path = Path(sys.argv[1])
trace_path = Path(os.environ["KM_VMS_TEST_TRACE"])

def trace(message: str) -> None:
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")

spec = importlib.util.spec_from_file_location("stage13722_target_bridge", bridge_path)
if spec is None or spec.loader is None:
    raise SystemExit(98)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
project = os.getenv("KM_VMS_PROJECT_NAME", "")
trace("current_target_handoff_entry project=" + (project or "<empty>"))

original_load_bootstrap = bridge.load_source_bootstrap
def load_bootstrap(source_dir):
    bootstrap = original_load_bootstrap(source_dir)
    original_read = bootstrap.read_project_name
    def read_project_name(app_dir, supplied=None):
        resolved = original_read(app_dir, supplied)
        trace("current_target_project_resolved=" + resolved)
        return resolved
    bootstrap.read_project_name = read_project_name
    return bootstrap
bridge.load_source_bootstrap = load_bootstrap

def stop_before_mutation(_app_dir, _target_source_dir, *, engine, active_source, contract_family):
    del engine, active_source
    trace("current_target_handoff_pre_mutation family=" + contract_family)
    raise bridge.BridgeError(
        "fixture_handoff_reached",
        "The isolated fixture reached the current target handoff boundary.",
    )
bridge.install_stable_bootstrap_for_handoff = stop_before_mutation
error_output = io.StringIO()
with contextlib.redirect_stderr(error_output):
    exit_code = bridge.main(sys.argv[2:])
captured_error = error_output.getvalue()
sys.stderr.write(captured_error)
for line in captured_error.splitlines():
    if line.startswith("ERROR [") and "]:" in line:
        category, message = line[7:].split("]: ", 1)
        trace("current_target_error=" + category)
        trace("current_target_error_message=" + message)
raise SystemExit(exit_code)
'''


def _make_execution_stubs(root: Path, target_archive: Path, trace: Path) -> Path:
    stubs = root / "stubs"
    stubs.mkdir()
    harness = root / "bridge-harness.py"
    harness.write_text(PYTHON_BRIDGE_HARNESS, encoding="utf-8")
    real_python = sys.executable
    scripts = {
        "python3": f'''#!/bin/sh
case "${{1:-}}:${{2:-}}" in
  */km-vms-update-helper-bridge.py:handoff)
    exec "{real_python}" -B "{harness}" "$@"
    ;;
esac
exec "{real_python}" "$@"
''',
        "curl": '''#!/bin/sh
out=""
headers=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --dump-header) headers="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$out" ] && [ -n "$headers" ] || exit 64
cp "$KM_VMS_TEST_TARGET_ARCHIVE" "$out"
size=$(wc -c < "$out")
printf 'HTTP/1.1 200 OK\r\nContent-Length: %s\r\n\r\n' "$size" > "$headers"
printf '%s\n' 'frozen_update_local_target_acquired' >> "$KM_VMS_TEST_TRACE"
''',
        "docker-compose": '''#!/bin/sh
printf 'compose_stub:%s\n' "$*" >> "$KM_VMS_TEST_TRACE"
case "$*" in
  *version*) printf '%s\n' 'Docker Compose version v2.99.0'; exit 0 ;;
  *) exit 97 ;;
esac
''',
        "docker": '''#!/bin/sh
printf 'docker_stub:%s\n' "$*" >> "$KM_VMS_TEST_TRACE"
exit 97
''',
    }
    for name, content in scripts.items():
        path = stubs / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    assert target_archive.is_file()
    trace.write_text("", encoding="utf-8")
    return stubs


def _entry_environment(
    *,
    stubs: Path,
    archive: Path,
    trace: Path,
    project_name: str,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{stubs}:{env.get('PATH', '')}",
            "KM_VMS_DOCKER_COMPOSE": str(stubs / "docker-compose"),
            "KM_VMS_TEST_TARGET_ARCHIVE": str(archive),
            "KM_VMS_TEST_TRACE": str(trace),
            "KM_VMS_UPDATE_CONTROL_REQUEST_ID": "update-" + "2" * 32,
        }
    )
    if project_name:
        env["KM_VMS_PROJECT_NAME"] = project_name
    else:
        env.pop("KM_VMS_PROJECT_NAME", None)
    return env


def _entry_snapshot(app: Path, active_source: Path) -> dict[str, object]:
    manifest = _verified_frozen_manifest()
    return {
        "active": os.readlink(app / "data/update-runtime/active"),
        "projection": (
            app / "data/update-runtime/installed-projection/installed-slot.json"
        ).read_bytes(),
        "env": (app / ".env").read_bytes(),
        "data": (app / "data/fixture.sentinel").read_bytes(),
        "database": (app / "data/postgres/fixture.sentinel").read_bytes(),
        "frozen": {
            item["fixture_path"]: hashlib.sha256(
                (active_source / item["fixture_path"]).read_bytes()
            ).hexdigest()
            for item in manifest["files"]
        },
    }


def _assert_entry_snapshot(app: Path, active_source: Path, before: dict) -> None:
    assert _entry_snapshot(app, active_source) == before


def _run_terminal_entry(root: Path, *, project_name: str) -> tuple[list[str], dict]:
    app, active_source, _slot_id = _prepare_frozen_app(
        root,
        project_name=project_name,
    )
    archive = _build_current_target_archive(root)
    trace_path = root / "ordered-trace.txt"
    stubs = _make_execution_stubs(root, archive, trace_path)
    env = _entry_environment(
        stubs=stubs,
        archive=archive,
        trace=trace_path,
        project_name=project_name,
    )
    before = _entry_snapshot(app, active_source)
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write("terminal_frozen_launcher_start\n")
    launcher = (
        app
        / "data/update-runtime/bootstrap/current/km-vms-update-launcher.sh"
    )
    result = subprocess.run(
        [
            "sh",
            str(launcher),
            "--app-dir",
            str(app),
            "--github-repo",
            "fixture/km-vms",
            "--branch",
            FROZEN_COMMIT,
            "--trusted-commit",
            FROZEN_COMMIT,
            "--yes",
        ],
        cwd=app,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
        check=False,
    )
    trace = trace_path.read_text(encoding="utf-8").splitlines()
    metadata = json.loads((app / ".km-vms-update.json").read_text(encoding="utf-8"))
    assert result.returncode != 0
    _assert_entry_snapshot(app, active_source, before)
    assert all(
        "version" in line
        for line in trace
        if line.startswith("compose_stub:")
    )
    assert not any(line.startswith("docker_stub:") for line in trace)
    return trace, metadata


def _run_in_app_entry(root: Path, *, project_name: str) -> tuple[list[str], dict]:
    app, active_source, _slot_id = _prepare_frozen_app(
        root,
        project_name=project_name,
    )
    archive = _build_current_target_archive(root)
    trace_path = root / "ordered-trace.txt"
    stubs = _make_execution_stubs(root, archive, trace_path)
    env = _entry_environment(
        stubs=stubs,
        archive=archive,
        trace=trace_path,
        project_name=project_name,
    )
    before = _entry_snapshot(app, active_source)
    previous = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        os.environ["KM_VMS_UPDATE_APP_DIR"] = str(app)
        os.environ["KM_VMS_UPDATE_HOST_APP_DIR"] = str(app)
        helper = _load_file(
            f"frozen_helper_{root.name}",
            FROZEN_FIXTURE / "scripts/km-vms-update-helper.py",
        )
        helper.APP_DIR = app
        helper.HOST_APP_DIR = app
        helper.CONTROL_DIR = app / "data/update-control"
        helper.STATUS_FILE = helper.CONTROL_DIR / "update-status.json"
        helper.PROGRESS_FILE = helper.CONTROL_DIR / "update-progress.json"
        helper.ACTIVATION_JOURNAL_FILE = helper.CONTROL_DIR / "activation-journal.json"
        observed_commands: list[list[str]] = []

        def run_child(command, _request, update_dir, child_env, **_kwargs):
            observed_commands.append(list(command))
            with trace_path.open("a", encoding="utf-8") as handle:
                handle.write("in_app_direct_frozen_update_start\n")
            return subprocess.run(
                command,
                cwd=update_dir,
                env=child_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=45,
                check=False,
            )

        helper.run_child_with_progress = run_child
        helper.activation_journal = lambda _request_id: None
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write("in_app_frozen_helper_run_update_start\n")
        request = {
            "schema_version": 3,
            "request_id": "update-" + "2" * 32,
            "requested_at": "2026-08-14T00:00:00Z",
            "intent": "apply_update",
            "confirmed": True,
            "source": {
                "kind": "trusted_manifest",
                "channel": "stable",
                "version": "0.8.15",
                "source_type": "github_tarball",
                "repo": "fixture/km-vms",
                "ref": FROZEN_COMMIT,
                "commit": FROZEN_COMMIT,
                "apply_ref": FROZEN_COMMIT,
            },
        }
        with pytest.raises(helper.HelperError):
            helper.run_update(request)
        assert len(observed_commands) == 1
        assert observed_commands[0][:2] == [
            "sh",
            str(active_source / "scripts/update.sh"),
        ]
        assert "km-vms-update-launcher.sh" not in " ".join(observed_commands[0])
    finally:
        os.environ.clear()
        os.environ.update(previous)
    trace = trace_path.read_text(encoding="utf-8").splitlines()
    metadata = json.loads((app / ".km-vms-update.json").read_text(encoding="utf-8"))
    _assert_entry_snapshot(app, active_source, before)
    assert all(
        "version" in line
        for line in trace
        if line.startswith("compose_stub:")
    )
    assert not any(line.startswith("docker_stub:") for line in trace)
    return trace, metadata


def test_frozen_v0_8_15_fixture_manifest_is_byte_exact() -> None:
    _verified_frozen_manifest()


@pytest.mark.parametrize("surface", ["terminal", "in_app"])
def test_frozen_v0_8_15_known_project_reaches_current_target_handoff(
    tmp_path: Path,
    surface: str,
) -> None:
    run = _run_terminal_entry if surface == "terminal" else _run_in_app_entry
    layout = tmp_path / f"{surface}-layout"
    with tempfile.TemporaryDirectory(dir=tmp_path, prefix=f"{surface}-") as temp:
        layout = Path(temp)
        trace, _metadata = run(layout, project_name="fixture-a")
        assert trace[0] == (
            "terminal_frozen_launcher_start"
            if surface == "terminal"
            else "in_app_frozen_helper_run_update_start"
        )
        assert "frozen_update_local_target_acquired" in trace
        assert "current_target_handoff_entry project=fixture-a" in trace
        assert "current_target_project_resolved=fixture-a" in trace
        assert (
            "current_target_handoff_pre_mutation family=current_contract_slot"
            in trace
        )
        assert not any("tnas-vms" in line for line in trace)
    assert not layout.exists()


@pytest.mark.parametrize("surface", ["terminal", "in_app"])
def test_frozen_v0_8_15_blank_project_stops_recovery_only_before_mutation(
    tmp_path: Path,
    surface: str,
) -> None:
    run = _run_terminal_entry if surface == "terminal" else _run_in_app_entry
    layout = tmp_path / f"{surface}-blank-layout"
    with tempfile.TemporaryDirectory(dir=tmp_path, prefix=f"{surface}-blank-") as temp:
        layout = Path(temp)
        trace, metadata = run(layout, project_name="")
        assert trace[0] == (
            "terminal_frozen_launcher_start"
            if surface == "terminal"
            else "in_app_frozen_helper_run_update_start"
        )
        assert "frozen_update_local_target_acquired" in trace
        assert "current_target_handoff_entry project=<empty>" in trace
        assert not any(
            line.startswith("current_target_handoff_pre_mutation") for line in trace
        )
        assert "current_target_error=project_identity_recovery_required" in trace
        assert any(
            line.startswith("current_target_error_message=")
            and "cannot receive a resolved Compose project identity" in line
            for line in trace
        )
        # The immutable 0.8.15 parent records terminal bridge failures as
        # ``failed`` and does not preserve newer typed categories.  The
        # current bridge trace above is the authoritative typed boundary.
        assert metadata["status"] == "failed"
        assert metadata["error_category"] is None
        assert not any("tnas-vms" in line for line in trace)
    assert not layout.exists()
