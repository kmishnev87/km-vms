"""Executable archive trust boundary tests; synthetic trees, no Docker socket."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("stage14_archive_policy", ROOT / "scripts/km-vms-archive-policy.py")
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def intent(*paths):
    return {"schema_version": 1, "runtime_base": "/storage/archive-roots", "compose_override_file": "docker-compose.archive-roots.yml", "raw_runtime_paths_user_visible": False,
            "items": [{"root_id": f"root_{i}", "user_display_path": path, "backend_runtime_path": f"/storage/archive-roots/root_{i}", "physical_volume_id": "/Volume3", "storage_namespace": "kmvms/recordings", "active_write_target": i == 0} for i, path in enumerate(paths)]}


@pytest.fixture
def host(tmp_path):
    host = tmp_path / "host"
    for path in ("Volume3/app/data/archive", "Volume3/archive", "Volume3/new", "Volume3/personal", "Volume3/app/private", "Volume1/docker-data", "etc", "tmp"):
        (host / path).mkdir(parents=True)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"1 0 0:1 / {host}/Volume3 rw - ext4 /dev/test rw\n")
    return host, mountinfo


def probe(host, paths, *, trusted=("/Volume3/archive",)):
    tree, mounts = host
    items = policy.parse_intent(intent(*paths))
    return policy._host_probe({"app_dir": "/Volume3/app", "docker_root": "/Volume1/docker-data", "trusted": list(trusted), "items": items}, str(tree), str(mounts))


@pytest.mark.parametrize("path", ["/", "/etc", "/tmp", "/Volume3", "/Volume3/app", "/Volume3/app/private", "/Volume1/docker-data", "/Volume1", "/Volume3/archive/inside"])
def test_protected_and_overlapping_paths_are_rejected(host, path):
    (host[0] / "Volume3/archive/inside").mkdir(exist_ok=True)
    with pytest.raises((ValueError, FileNotFoundError)):
        probe(host, [path])


def test_installer_exact_default_and_historical_mixed_root_survive(host):
    (host[0] / "Volume3/archive/legacy-camera-file").write_text("synthetic")
    result = probe(host, ["/Volume3/app/data/archive", "/Volume3/archive"])
    assert [item["source"] for item in result] == ["/Volume3/app/data/archive", "/Volume3/archive"]


def test_absolute_host_symlink_cannot_escape_and_safe_alias_is_canonical(host):
    (host[0] / "Volume3/escape").symlink_to("/etc", target_is_directory=True)
    with pytest.raises(ValueError):
        probe(host, ["/Volume3/escape"])
    (host[0] / "Volume3/alias").symlink_to("/Volume3/archive", target_is_directory=True)
    assert probe(host, ["/Volume3/alias"])[0]["source"] == "/Volume3/archive"


def test_relative_escape_and_symlink_loop_are_bounded(host):
    (host[0] / "Volume3/escape").symlink_to("../etc", target_is_directory=True)
    (host[0] / "Volume3/loop").symlink_to("loop", target_is_directory=True)
    for path in ("/Volume3/escape", "/Volume3/loop"):
        with pytest.raises(ValueError):
            probe(host, [path])


def test_marker_alone_does_not_authorize_personal_data(host):
    directory = host[0] / "Volume3/personal"
    (directory / ".km-vms-storage-root.json").write_text('{"schema_version":1,"product":"KM VMS"}')
    (directory / "personal-file").write_text("synthetic")
    with pytest.raises(ValueError):
        probe(host, ["/Volume3/personal"])


def test_new_empty_and_complete_kmvms_namespace_are_accepted(host):
    assert probe(host, ["/Volume3/new"])[0]["source"] == "/Volume3/new"
    directory = host[0] / "Volume3/new"
    for child in ("kmvms/recordings", ".km-vms-internal/migration", ".km-vms-internal-quarantine"):
        (directory / child).mkdir(parents=True)
    (directory / ".kmvms_retention_apply.lock").write_text("")
    (directory / ".km-vms-storage-root.json").write_text('{"schema_version":1,"product":"KM VMS"}')
    assert probe(host, ["/Volume3/new"])[0]["source"] == "/Volume3/new"


def test_absent_inactive_intent_stays_pending_without_losing_existing_mount(host):
    assert len(probe(host, ["/Volume3/archive", "/Volume3/not-created"])) == 1
    with pytest.raises(FileNotFoundError):
        probe(host, ["/Volume3/not-created"])
    (host[0] / "Volume3/not-created").mkdir()
    assert len(probe(host, ["/Volume3/archive", "/Volume3/not-created"])) == 2


@pytest.mark.parametrize("target", ["/storage/archive-roots/.", "/storage/archive-roots/..", "/storage/archive-roots/x/y", "/var/run/docker.sock"])
def test_targets_cannot_escape(target):
    value = intent("/Volume3/new")
    value["items"][0]["backend_runtime_path"] = target
    with pytest.raises(policy.ArchivePolicyError):
        policy.parse_intent(value)


def test_duplicate_keys_targets_and_oversize_or_symlink_json_are_rejected(tmp_path):
    path = tmp_path / "intent.json"
    path.write_text('{"items":[],"items":[]}')
    with pytest.raises(policy.ArchivePolicyError):
        policy.read_json(path)
    path.write_text(" " * (policy.MAX_JSON + 1))
    with pytest.raises(policy.ArchivePolicyError):
        policy.read_json(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(policy.ArchivePolicyError):
        policy.read_json(link)
    value = intent("/Volume3/archive", "/Volume3/new")
    value["items"][1]["backend_runtime_path"] = value["items"][0]["backend_runtime_path"]
    with pytest.raises(policy.ArchivePolicyError):
        policy.parse_intent(value)


def test_materializer_ignores_injected_yaml_and_prunes_registry(host, tmp_path, monkeypatch):
    app = tmp_path / "app"
    control = app / "data/install-control"
    control.mkdir(parents=True)
    (app / ".env").write_text("KM_VMS_HOST_APP_DIR=/Volume3/app\nSURVEILLANCE_ROOT=/Volume3/archive\n")
    (control / "archive-roots-runtime.json").write_text(json.dumps(intent("/Volume3/archive", "/Volume3/new")))
    injected = control / "docker-compose.archive-roots.yml"
    injected.write_text("services:\n  api:\n    privileged: true\n    command: untrusted\n    volumes: [/:/host]\n")
    monkeypatch.setattr(policy, "installed_sources", lambda _: {"/Volume3/archive"})
    calls = []

    def docker(args, *, data=None, timeout=20):
        calls.append(args)
        if args[0] == "info":
            return "/Volume1/docker-data"
        if args[0] == "image":
            return "sha256:" + "a" * 64
        assert "--read-only" in args and "none" in args and "--pull=never" in args
        assert "--privileged" not in args and "docker.sock" not in " ".join(args)
        return json.dumps(policy._host_probe(json.loads(data), str(host[0]), str(host[1])))

    monkeypatch.setattr(policy, "docker_command", docker)
    target = policy.materialize(app, "fixture")
    text = target.read_text()
    assert "privileged:" not in text and "untrusted" not in text
    assert all(f"  {service}:\n" in text for service in ("api", "schema-update", "recorder"))
    # Both public runtime mounts and the installer primary bind must be
    # derived from the same validated plan.
    assert text.count("create_host_path: false") == 9
    assert target.stat().st_mode & 0o777 == 0o400
    assert "privileged: true" in injected.read_text()  # Not parsed or rewritten.
    (control / "archive-roots-runtime.json").write_text(json.dumps(intent("/Volume3/archive")))
    assert policy.materialize(app, "fixture") == target
    assert json.loads((target.parent / "approved.json").read_text())["sources"] == ["/Volume3/archive"]
    assert len(list(target.parent.glob("*.yml"))) == 1


def test_literal_dollar_is_escaped_for_compose_and_env_is_not_interpolated(host, tmp_path, monkeypatch):
    app = tmp_path / "app"
    control = app / "data/install-control"
    control.mkdir(parents=True)
    directory = host[0] / "Volume3/price$HOME"
    directory.mkdir()
    (app / ".env").write_text("KM_VMS_HOST_APP_DIR=/Volume3/app\nSURVEILLANCE_ROOT='/Volume3/price$HOME'\n")
    (control / "archive-roots-runtime.json").write_text(json.dumps(intent("/Volume3/price$HOME")))
    monkeypatch.setattr(policy, "installed_sources", lambda _: set())
    monkeypatch.setattr(policy, "docker_command", lambda args, **kw: "/Volume1/docker-data" if args[0] == "info" else "sha256:" + "a" * 64 if args[0] == "image" else json.dumps(policy._host_probe(json.loads(kw["data"]), str(host[0]), str(host[1]))))
    assert "price$$HOME" in policy.materialize(app, "fixture").read_text()
    (app / ".env").write_text("SURVEILLANCE_ROOT=/Volume3/price$HOME\n")
    with pytest.raises(policy.ArchivePolicyError, match="literal quoting"):
        policy.installer_paths(app)


def test_missing_manifest_prebootstrap_needs_no_docker(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"KM_VMS_HOST_APP_DIR={tmp_path}\nSURVEILLANCE_ROOT={tmp_path}/data/archive\n")
    monkeypatch.setattr(policy, "docker_command", lambda *_a, **_k: pytest.fail("no Docker before intent"))
    assert policy.materialize(tmp_path, "fixture") is None


def test_missing_intent_cannot_disable_primary_validation(host, tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text("KM_VMS_HOST_APP_DIR=/Volume3/app\nSURVEILLANCE_ROOT=/etc\n")
    monkeypatch.setattr(policy, "installed_sources", lambda _: set())
    monkeypatch.setattr(policy, "docker_command", lambda args, **kw: "/Volume1/docker-data" if args[0] == "info" else "sha256:" + "a" * 64 if args[0] == "image" else json.dumps(policy._host_probe(json.loads(kw["data"]), str(host[0]), str(host[1]))))
    with pytest.raises(ValueError, match="protected host"):
        policy.materialize(app, "fixture")


def test_changed_intent_during_probe_is_not_published(host, tmp_path, monkeypatch):
    app = tmp_path / "app"
    control = app / "data/install-control"
    control.mkdir(parents=True)
    (app / ".env").write_text("KM_VMS_HOST_APP_DIR=/Volume3/app\nSURVEILLANCE_ROOT=/Volume3/archive\n")
    path = control / "archive-roots-runtime.json"
    path.write_text(json.dumps(intent("/Volume3/archive")))
    monkeypatch.setattr(policy, "installed_sources", lambda _: {"/Volume3/archive"})

    def docker(args, **kw):
        if args[0] == "info":
            return "/Volume1/docker-data"
        if args[0] == "image":
            return "sha256:" + "a" * 64
        result = policy._host_probe(json.loads(kw["data"]), str(host[0]), str(host[1]))
        path.write_text(json.dumps(intent("/Volume3/new")))
        return json.dumps(result)

    monkeypatch.setattr(policy, "docker_command", docker)
    with pytest.raises(policy.ArchivePolicyError, match="changed during"):
        policy.materialize(app, "fixture")
    assert not (app / "data/update-runtime/archive-authority/approved.json").exists()


def test_privileged_writer_preserves_installer_owner(tmp_path):
    import os
    if os.geteuid() != 0:
        pytest.skip("mixed-principal ownership requires root in the isolated test container")
    directory = tmp_path / "protected"
    policy._protected_directory(directory, (12345, 12345))
    policy._atomic(directory / "authority", b"synthetic", 0o400, (12345, 12345))
    assert directory.stat().st_uid == 12345
    assert (directory / "authority").stat().st_uid == 12345
    assert (directory / "authority").stat().st_mode & 0o777 == 0o400


def test_common_old_slot_uses_new_bundle_policy_at_runtime(tmp_path):
    app, old_source = tmp_path / "app", tmp_path / "old-source"
    bundle = app / "data/update-runtime/bootstrap/current"
    protected = app / "data/update-runtime/archive-authority/docker-compose.archive-roots.yml"
    bundle.mkdir(parents=True)
    old_source.mkdir()
    protected.parent.mkdir(parents=True)
    protected.write_text("services: {}\n")
    (bundle / "km-vms-archive-policy.py").write_text(f"print({str(protected)!r})\n")
    command = '. "$1"; km_vms_archive_override "$2" fixture "$3"'
    result = subprocess.run(["sh", "-c", command, "sh", str(ROOT / "scripts/km-vms-compose-common.sh"), str(app), str(old_source)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(protected)


def test_new_common_uses_protected_policy_for_old_slot_and_pythonless_helper():
    common = (ROOT / "scripts/km-vms-compose-common.sh").read_text()
    bootstrap = (ROOT / "scripts/km-vms-bootstrap.py").read_text()
    bridge = (ROOT / "scripts/km-vms-update-helper-bridge.py").read_text()
    assert 'archive_app_dir/data/update-runtime/bootstrap/current/km-vms-archive-policy.py' in common
    assert 'docker exec "$archive_helper_id" python3 -B' in common
    assert '"km-vms-archive-policy.py"' in bootstrap
    assert 'archive_override = protected_archive_override(app_dir, project_name)' in bridge
    assert 'archive_override="$stable_app_dir/data/install-control/docker-compose.archive-roots.yml"' not in common
    assert 'app_dir / "data/install-control/docker-compose.archive-roots.yml"' not in bridge


def _cold_fixture(host, tmp_path, monkeypatch):
    app = tmp_path / "app"
    control = app / "data/install-control"
    control.mkdir(parents=True)
    tree, mountinfo = host
    (tree / "Volume2/secondary").mkdir(parents=True)
    with mountinfo.open("a") as stream:
        stream.write(f"2 0 0:2 / {tree}/Volume2 rw - ext4 /dev/test2 rw\n")
    (app / ".env").write_text("KM_VMS_HOST_APP_DIR=/Volume3/app\nSURVEILLANCE_ROOT=/Volume3/archive\n")
    (control / "archive-roots-runtime.json").write_text(json.dumps(intent("/Volume3/archive", "/Volume2/secondary")))
    for path in ("Volume3/archive", "Volume2/secondary"):
        (tree / path / "kmvms/recordings").mkdir(parents=True)
    monkeypatch.setattr(policy, "installed_sources", lambda _: {"/Volume3/archive", "/Volume2/secondary"})

    def docker(args, *, data=None, timeout=20):
        if args[0] == "info":
            return "/Volume1/docker-data"
        if args[0] == "image":
            return "sha256:" + "a" * 64
        return json.dumps(policy._host_probe(json.loads(data), str(tree), str(mountinfo)))

    monkeypatch.setattr(policy, "docker_command", docker)
    original = policy.materialize(app, "fixture")
    assert set(item["target"] for item in policy.mount_evidence(app)) == {
        "/storage/archive", "/storage/archive-roots/root_0", "/storage/archive-roots/root_1",
    }
    return app, tree, mountinfo, original


@pytest.mark.parametrize("unplug", ["primary", "secondary", "both"])
def test_cold_start_omits_only_missing_approved_mounts_and_restores_them(host, tmp_path, monkeypatch, unplug):
    app, tree, _, override = _cold_fixture(host, tmp_path, monkeypatch)
    existing = override.read_bytes()
    missing = []
    for name, relative in (("primary", "Volume3/archive"), ("secondary", "Volume2/secondary")):
        if unplug in {name, "both"}:
            path = tree / relative
            held = path.with_name(path.name + "-held")
            path.rename(held)
            missing.append((held, path))
    policy.materialize(app, "fixture")
    plan = override.read_text()
    assert ("/Volume3/app/data/update-runtime/archive-authority/unavailable-primary" in plan) == (
        unplug in {"primary", "both"}
    )
    assert ('source: "/Volume2/secondary"' in plan) == (unplug == "primary")
    assert ('source: "/Volume3/archive"' in plan) == (unplug == "secondary")
    assert ("read_only: true" in plan) == (unplug != "secondary")
    assert all(not path.exists() for _, path in missing)
    for held, path in missing:
        held.rename(path)
    assert policy.materialize(app, "fixture").read_bytes() == existing


def test_same_path_on_wrong_or_unmounted_filesystem_is_not_reattached(host, tmp_path, monkeypatch):
    app, tree, mountinfo, override = _cold_fixture(host, tmp_path, monkeypatch)
    original = mountinfo.read_text()
    # An unmounted NAS path can still look like a real directory in the
    # underlying system FS. Directory existence is not a mount.
    mountinfo.write_text("\n".join(line for line in original.splitlines() if "/Volume2 " not in line) + "\n")
    policy.materialize(app, "fixture")
    assert 'source: "/Volume2/secondary"' not in override.read_text()
    path = tree / "Volume2/secondary"
    path.rename(path.with_name("original"))
    (path / "kmvms/recordings").mkdir(parents=True)
    mountinfo.write_text(original)
    policy.materialize(app, "fixture")
    assert 'source: "/Volume2/secondary"' not in override.read_text()
    assert not any((path / "kmvms/recordings").iterdir())
    assert {entry["target"] for entry in policy.mount_evidence(app) if not entry["available"]} == {
        "/storage/archive-roots/root_1"
    }


def test_returning_disk_uses_superblock_readiness_not_readonly_probe_mount_flag(host, tmp_path, monkeypatch):
    app, tree, mountinfo, override = _cold_fixture(host, tmp_path, monkeypatch)
    healthy = override.read_bytes()
    original = mountinfo.read_text()
    disk = tree / "Volume2/secondary"
    held = disk.with_name("held")
    disk.rename(held)
    policy.materialize(app, "fixture")
    held.rename(disk)
    mountinfo.write_text(original.replace("/Volume2 rw - ext4 /dev/test2 rw", "/Volume2 ro - ext4 /dev/test2 ro"))
    policy.materialize(app, "fixture")
    assert 'source: "/Volume2/secondary"' not in override.read_text()
    mountinfo.write_text(original.replace("/Volume2 rw - ext4", "/Volume2 ro - ext4"))
    assert policy.materialize(app, "fixture").read_bytes() == healthy
