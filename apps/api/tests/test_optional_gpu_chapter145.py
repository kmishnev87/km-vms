from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gpu = load("optional_gpu_chapter145", "scripts/km-vms-optional-gpu.py")
bridge = load("optional_gpu_bridge_chapter145", "scripts/km-vms-update-helper-bridge.py")


def source_fixture(tmp_path):
    source = tmp_path / "source"
    override = source / gpu.OVERRIDE
    override.parent.mkdir(parents=True)
    override.write_text("services:\n  api:\n    devices: [/dev/dri:/dev/dri]\n")
    (source / "scripts").mkdir()
    (source / "scripts/km-vms-optional-gpu.py").touch()
    (source / "docker-compose.yml").write_text("services: {}\n")
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text("LIVE_HWACCEL_MODE=auto\n")
    return app, source, override


@pytest.mark.parametrize("in_container", [False, True])
@pytest.mark.parametrize("available", [False, True])
def test_selection_uses_the_correct_host(tmp_path, monkeypatch, in_container, available):
    app, source, override = source_fixture(tmp_path)
    calls = []
    monkeypatch.setattr(gpu, "container_context", lambda: in_container)
    monkeypatch.setattr(gpu, "local_drm_devices", lambda: calls.append("local") or available)
    monkeypatch.setattr(gpu, "docker_host_drm_devices", lambda: calls.append("docker") or available)
    assert gpu.select_override(app, source) == (override if available else None)
    assert calls == (["docker"] if in_container else ["local"])


@pytest.mark.parametrize("mode", ["off", "disabled", "false", "0", '"off"', "'OFF'"])
def test_explicit_cpu_mode_never_starts_a_probe(tmp_path, monkeypatch, mode):
    app, source, _ = source_fixture(tmp_path)
    (app / ".env").write_text(f"LIVE_HWACCEL_MODE={mode}\n")
    monkeypatch.setattr(gpu, "container_context", lambda: pytest.fail("Unexpected GPU probe"))
    assert gpu.select_override(app, source) is None


def test_historical_source_is_not_reinterpreted_and_symlinks_are_rejected(tmp_path):
    app, source, override = source_fixture(tmp_path)
    override.unlink()
    assert gpu.select_override(app, source) is None
    override.symlink_to(source / "docker-compose.yml")
    with pytest.raises(ValueError, match="unsafe"):
        gpu.select_override(app, source)


@pytest.mark.parametrize("probe_returncode", [0, 125])
def test_docker_probe_is_bounded_local_networkless_and_always_reclaimed(monkeypatch, probe_returncode):
    calls = []
    monkeypatch.setattr(gpu.shutil, "which", lambda _: "/usr/bin/docker")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "inspect" in args:
            return subprocess.CompletedProcess(args, 0, "sha256:" + "a" * 64 + "\n", "")
        return subprocess.CompletedProcess(args, probe_returncode, "", "")

    monkeypatch.setattr(gpu.subprocess, "run", run)
    assert gpu.docker_host_drm_devices() is (probe_returncode == 0)
    args, options = calls[1]
    for flag in ("--rm", "--pull=never", "--no-healthcheck", "--read-only"):
        assert flag in args
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--device") + 1] == "/dev/dri:/dev/dri"
    assert "--volume" not in args and "-v" not in args and "--privileged" not in args
    assert options["timeout"] == 15
    assert calls[-1][0][1:3] == ["rm", "--force"]
    assert calls[-1][0][-1] == args[args.index("--name") + 1]


def test_probe_timeout_falls_back_to_cpu_and_cleans_up(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu.shutil, "which", lambda _: "/usr/bin/docker")

    def run(args, **kwargs):
        calls.append(args)
        if "inspect" in args:
            return subprocess.CompletedProcess(args, 0, "sha256:" + "b" * 64, "")
        if "run" in args:
            raise subprocess.TimeoutExpired(args, 15)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(gpu.subprocess, "run", run)
    assert gpu.docker_host_drm_devices() is False
    assert calls[-1][1:3] == ["rm", "--force"]


@pytest.mark.parametrize("selection", ["cpu", "gpu", "escaped"])
def test_update_bridge_uses_same_selector_without_changing_old_sources(tmp_path, monkeypatch, selection):
    app, source, override = source_fixture(tmp_path)
    monkeypatch.setenv("KM_VMS_ALLOW_PREBOOTSTRAP_COMPOSE", "1")
    monkeypatch.setattr(bridge, "protected_archive_override", lambda *args: None)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        selected = {"cpu": "", "gpu": str(override), "escaped": "/tmp/foreign.yml"}[selection]
        return subprocess.CompletedProcess(args, 0, selected, "")

    monkeypatch.setattr(bridge, "run_command", run)
    if selection == "escaped":
        with pytest.raises(bridge.BridgeError, match="escaped"):
            bridge.compose_base(app, "fixture", source_dir=source)
        return
    command = bridge.compose_base(app, "fixture", source_dir=source)
    assert (str(override) in command) is (selection == "gpu")
    assert calls[0][-4:] == ["--app-dir", str(app), "--source-dir", str(source)]
    override.unlink()
    calls.clear()
    bridge.compose_base(app, "fixture", source_dir=source)
    assert calls == []


@pytest.mark.parametrize("available", [False, True])
def test_shell_install_restart_and_storage_use_the_same_selector(tmp_path, available):
    app, source, override = source_fixture(tmp_path)
    (source / "scripts/km-vms-optional-gpu.py").write_text(
        f"print({str(override)!r})\n" if available else "pass\n"
    )
    shell = r'''
set -eu
. "$1"
km_vms_resolve_project_name() { printf 'fixture\n'; }
km_vms_archive_override() { printf '\n'; }
km_vms_lifecycle_override() { printf '\n'; }
km_vms_compose_cmd() { printf '%s\n' "$@"; }
km_vms_compose_for_source "$2" "$3" config
'''
    result = subprocess.run(
        ["sh", "-c", shell, "optional-gpu-test", str(ROOT / "scripts/km-vms-compose-common.sh"), str(app), str(source)],
        env={**os.environ, "KM_VMS_COMPOSE_GPU_OVERRIDE": "/tmp/must-not-leak.yml"},
        capture_output=True, text=True, check=True,
    )
    assert (str(override) in result.stdout.splitlines()) is available
    assert "must-not-leak" not in result.stdout


def test_base_compose_no_longer_requires_a_gpu_and_installer_honors_cpu_mode():
    compose = (ROOT / "docker-compose.yml").read_text()
    api = compose.split("\n  api:", 1)[1].split("\n  restore-executor:", 1)[0]
    assert "\n    devices:" not in api
    assert "/dev/dri:/dev/dri" not in api
    assert "/dev/dri:/dev/dri" in (ROOT / gpu.OVERRIDE).read_text()
    installer = (ROOT / "scripts/install.sh").read_text()
    assert "hardware_mode=${LIVE_HWACCEL_MODE:-auto}" in installer
    assert "printf 'LIVE_HWACCEL_MODE=%s\\n'" in installer
