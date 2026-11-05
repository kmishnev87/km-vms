"""A detached refresh must never inherit the running helper's Compose identity."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_bridge():
    spec = importlib.util.spec_from_file_location("stage14_coordinator_labels_bridge", ROOT / "scripts/km-vms-update-helper-bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


@pytest.mark.parametrize("target_slot", [None, "release-" + "a" * 40])
def test_refresh_overrides_inherited_compose_identity_without_changing_target_authority(monkeypatch, tmp_path, target_slot):
    bridge = load_bridge()
    script = tmp_path / "scripts/km-vms-update-helper-bridge.py"
    script.parent.mkdir()
    script.write_text("# fixture bridge\n")
    request_id = "update-" + "b" * 32
    image_id = "sha256:" + "c" * 64
    project = "installed-product"
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 60
        return SimpleNamespace(returncode=0, stdout="d" * 64, stderr="")

    monkeypatch.setattr(bridge, "run_command", run)
    monkeypatch.setattr(bridge, "inspected_container_image", lambda _: None)
    assert bridge.schedule_refresh(app_dir=tmp_path, project_name=project,
                                   helper_image="immutable-helper:target", request_id=request_id,
                                   expected_image_id=image_id, timeout_seconds=120,
                                   bridge_script=script, target_slot=target_slot) == "scheduled"
    assert len(commands) == 1
    command = commands[0]
    inherited = {"com.docker.compose.project": project,
                 "com.docker.compose.service": "update-helper",
                 "com.docker.compose.version": "2.33.0"}
    for index, item in enumerate(command):
        if item == "--label":
            key, value = command[index + 1].split("=", 1)
            inherited[key] = value
    assert inherited["com.docker.compose.project"] == "km-vms-helper-refresh-" + "b" * 32
    assert inherited["com.docker.compose.project"] != project
    assert inherited["com.docker.compose.service"] == "update-helper-refresh"
    assert inherited["com.docker.compose.oneoff"] == "True"
    assert inherited["org.km-vms.update.project"] == project
    assert "--rm" in command
    assert image_id in command
    assert command[command.index("--project-name") + 1] == project
    assert command[command.index("--request-id") + 1] == request_id
    assert command[command.index("--expected-image-id") + 1] == image_id
    if target_slot:
        assert command[command.index("--target-slot") + 1] == target_slot
