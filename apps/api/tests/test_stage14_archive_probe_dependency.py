"""Cold update prerequisites, bounded failure and retry without a Docker socket."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("stage14_probe_dependency", "scripts/km-vms-archive-policy.py")


def test_cached_probe_has_no_registry_dependency(monkeypatch):
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        assert args == ["image", "inspect", "--format", "{{.Id}}", policy.PYTHON_IMAGE]
        return "sha256:" + "a" * 64

    monkeypatch.setattr(policy, "docker_command", docker)
    policy.ensure_probe_image()
    assert len(calls) == 1


def test_absent_probe_is_pulled_by_digest_then_verified(monkeypatch):
    calls = []

    def docker(args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise policy.ArchivePolicyError("not cached")
        return "sha256:" + "a" * 64

    monkeypatch.setattr(policy, "docker_command", docker)
    policy.ensure_probe_image()
    assert [args[0] for args, _ in calls] == ["image", "pull", "image"]
    assert calls[1] == (["pull", "--quiet", policy.PYTHON_IMAGE], {"timeout": 120})
    assert calls[0][0] == calls[2][0]


@pytest.mark.parametrize("failure_at", ["pull", "verification"])
def test_failed_provision_is_specific_and_does_not_run_probe(monkeypatch, failure_at):
    calls = []

    def docker(args, **kwargs):
        calls.append(args[0])
        if len(calls) == 1 or args[0] == failure_at or (failure_at == "verification" and len(calls) == 3):
            raise policy.ArchivePolicyError("private daemon diagnostics")
        return ""

    monkeypatch.setattr(policy, "docker_command", docker)
    with pytest.raises(policy.ArchiveProbeImageError) as error:
        policy.ensure_probe_image()
    assert error.value.code == "archive_probe_image_unavailable"
    assert "private" not in str(error.value)
    assert "run" not in calls
    assert calls.count("pull") == 1


def test_docker_timeout_is_bounded_and_sanitized(monkeypatch):
    monkeypatch.setattr(policy.shutil, "which", lambda _: "/fixture/docker")

    def timeout(args, **kwargs):
        assert kwargs["timeout"] == 120
        raise subprocess.TimeoutExpired(args, 120)

    monkeypatch.setattr(policy.subprocess, "run", timeout)
    with pytest.raises(policy.ArchivePolicyError, match="temporarily unavailable"):
        policy.docker_command(["pull", "--quiet", policy.PYTHON_IMAGE], timeout=120)


def test_failed_cold_materialize_can_retry_without_changing_existing_data(tmp_path, monkeypatch):
    app = tmp_path / "app"
    control = app / "data/install-control"
    control.mkdir(parents=True)
    env = app / ".env"
    env.write_text("KM_VMS_HOST_APP_DIR=/Volume3/app\nSURVEILLANCE_ROOT=/Volume3/archive\n")
    intent = control / "archive-roots-runtime.json"
    original = {"schema_version": 1, "runtime_base": "/storage/archive-roots", "compose_override_file": "docker-compose.archive-roots.yml", "raw_runtime_paths_user_visible": False, "items": [{"root_id": "root_a", "user_display_path": "/Volume3/archive", "backend_runtime_path": "/storage/archive-roots/root_a", "physical_volume_id": "/Volume3", "storage_namespace": "kmvms/recordings", "active_write_target": True}]}
    intent.write_text(json.dumps(original))
    existing = app / "data/existing-video"
    existing.write_bytes(b"synthetic archive sentinel")
    before = {p: p.read_bytes() for p in (env, intent, existing)}
    state = {"online": False, "cached": False}
    calls = []
    monkeypatch.setattr(policy, "installed_sources", lambda _: {"/Volume3/archive"})

    def docker(args, *, data=None, timeout=20):
        calls.append(args)
        if args[0] == "info":
            return "/Volume1/docker-data"
        if args[0] == "image":
            if not state["cached"]:
                raise policy.ArchivePolicyError("not cached")
            return "sha256:" + "a" * 64
        if args[0] == "pull":
            assert timeout == 120 and args[-1] == policy.PYTHON_IMAGE
            if not state["online"]:
                raise policy.ArchivePolicyError("registry unavailable")
            state["cached"] = True
            return ""
        assert args[0] == "run" and state["cached"]
        assert "--pull=never" in args and "--read-only" in args
        assert args[args.index("--network") + 1] == "none"
        assert "--privileged" not in args and "docker.sock" not in " ".join(args)
        assert "type=bind,source=/,target=/host,readonly,bind-propagation=rslave" in args
        return json.dumps([{"source": "/Volume3/archive", "target": "/storage/archive-roots/root_a"}])

    monkeypatch.setattr(policy, "docker_command", docker)
    with pytest.raises(policy.ArchiveProbeImageError):
        policy.materialize(app, "fixture")
    protected = app / "data/update-runtime/archive-authority"
    assert not (protected / "approved.json").exists()
    assert not (protected / "docker-compose.archive-roots.yml").exists()
    assert "run" not in [args[0] for args in calls]
    assert all(p.read_bytes() == value for p, value in before.items())
    state["online"] = True
    target = policy.materialize(app, "fixture")
    assert target.is_file() and target.stat().st_mode & 0o777 == 0o400
    assert json.loads((protected / "approved.json").read_text())["sources"] == ["/Volume3/archive"]
    assert all(p.read_bytes() == value for p, value in before.items())
    pull_count = sum(args[0] == "pull" for args in calls)
    policy.materialize(app, "fixture")
    assert sum(args[0] == "pull" for args in calls) == pull_count


def test_bridge_preserves_specific_safe_dependency_failure(monkeypatch, tmp_path):
    bridge = load("stage14_probe_bridge", "scripts/km-vms-update-helper-bridge.py")

    def load_policy(spec, module):
        def unavailable(*args):
            raise policy.ArchiveProbeImageError("private details")
        module.materialize = unavailable

    monkeypatch.setattr(bridge, "_exec_module_without_bytecode", load_policy)
    with pytest.raises(bridge.BridgeError) as error:
        bridge.protected_archive_override(tmp_path, "fixture")
    assert error.value.code == "archive_probe_image_unavailable"
    assert "private" not in str(error.value)


def test_dependency_error_survives_shell_helper_and_api_projection(tmp_path):
    category = "archive_probe_image_unavailable"
    helper = load("stage14_probe_helper", "scripts/km-vms-update-helper.py")
    from app.services.update_apply import _public_error_for_category
    metadata = {"schema_version": 1, "failed_phase": "schema_preflight", "error_category": category}
    (tmp_path / ".km-vms-update.json").write_text(json.dumps(metadata))
    error = helper.classify_apply_failure(tmp_path, "private raw diagnostic")
    assert error.category == category
    assert "private" not in str(error)
    assert helper.classify_preflight_failure(f"ERROR [{category}]: private raw diagnostic").category == category
    public = _public_error_for_category(category)
    assert public["message"] == helper.error_payload(category)["message"]
    assert public["operator_action"] == helper.error_payload(category)["operator_action"]
    function = (ROOT / "scripts/update.sh").read_text().split("capture_safe_bridge_failure_category() {", 1)[1].split("\n}", 1)[0]
    diagnostic = tmp_path / "failure.txt"
    diagnostic.write_text(f"ERROR [{category}]: private raw diagnostic\n")
    script = "capture_safe_bridge_failure_category() {" + function + "\n}\n" + 'capture_safe_bridge_failure_category "$1"\nprintf "%s" "$FAILURE_CATEGORY"\n'
    result = subprocess.run(["sh", "-c", script, "fixture", str(diagnostic)], capture_output=True, text=True, check=True)
    assert result.stdout == category


def test_probe_digest_matches_both_product_python_images():
    for relative in ("apps/api/Dockerfile", "apps/recorder/Dockerfile"):
        assert (ROOT / relative).read_text().splitlines()[0] == "FROM " + policy.PYTHON_IMAGE
