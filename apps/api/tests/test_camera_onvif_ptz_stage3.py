import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.routers.cameras as cameras_module
import app.routers.camera_onvif_routes as onvif_routes_module
from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.models.camera import Camera
from app.routers.cameras import onvif_ptz_capabilities, onvif_ptz_command
from app.services.onvif_service import (
    execute_onvif_ptz_command,
    get_onvif_ptz_capabilities,
    validate_ptz_command_payload,
)


class FakeQuery:
    def __init__(self, camera):
        self.camera = camera

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.camera


class FakeDb:
    def __init__(self, camera):
        self.camera = camera
        self.events = []

    def query(self, model):
        return FakeQuery(self.camera)

    def add(self, value):
        self.events.append(value)

    def commit(self):
        return None

    def refresh(self, value):
        return None


def user(role="owner"):
    return SimpleNamespace(id=1, username="owner", role=role, is_active=True)


def request():
    return SimpleNamespace(client=None, headers={})


def camera(**overrides):
    data = {
        "id": 31,
        "name": "Stage3",
        "storage_folder_name": "stage3",
        "enabled": True,
        "protocol": "onvif",
        "host": "onvif.example.test",
        "port": 20003,
        "username": "operator",
        "password_encrypted": "encrypted",
        "rtsp_main_url": "/main",
        "rtsp_sub_url": None,
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 10003,
        "rtsp_transport": "tcp",
        "onvif_path": None,
        "onvif_profile_token": "main",
        "onvif_channel_id": None,
        "recording_mode": "always",
        "default_live_stream": "main",
        "default_record_stream": "main",
        "segment_minutes": 5,
        "retention_days": 30,
        "storage_quota_gb": 50,
        "status": "created",
        "last_error": None,
    }
    data.update(overrides)
    return Camera(**data)


def test_ptz_endpoints_are_registered_and_manage_camera_protected():
    rows = {(item.method, item.path): item for item in ENDPOINT_PERMISSIONS}

    caps = rows[("GET", "/cameras/{camera_id}/onvif/ptz/capabilities")]
    command = rows[("POST", "/cameras/{camera_id}/onvif/ptz/command")]

    assert caps.decision == "manage_cameras"
    assert command.decision == "manage_cameras"
    assert caps.allowed_roles == ("owner", "admin")
    assert command.allowed_roles == ("owner", "admin")
    assert "PTZ" in caps.notes
    assert "dry-run" in command.notes


def test_non_onvif_camera_capability_returns_safe_unsupported():
    result = onvif_ptz_capabilities(31, db=FakeDb(camera(protocol="rtsp")), current_user=user())

    assert result["supported"] is False
    assert result["source"] == "not_onvif"
    assert result["raw_secret_exposed"] is False
    assert "password" not in str(result).lower()


def test_deleted_camera_is_rejected_safely():
    with pytest.raises(HTTPException) as exc:
        onvif_ptz_capabilities(404, db=FakeDb(None), current_user=user())

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "camera_not_active"


def test_missing_credentials_are_rejected_safely(monkeypatch):
    monkeypatch.setattr(onvif_routes_module, "decrypt_text", lambda value: None)

    with pytest.raises(HTTPException) as exc:
        onvif_ptz_capabilities(31, db=FakeDb(camera()), current_user=user())

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "camera_onvif_credentials_required"
    assert "encrypted" not in str(exc.value.detail)


def test_fake_onvif_ptz_capabilities_are_sanitized(monkeypatch):
    monkeypatch.setattr(onvif_routes_module, "decrypt_text", lambda value: "camera-pass")
    monkeypatch.setattr(
        onvif_routes_module,
        "get_onvif_ptz_capabilities",
        lambda **kwargs: {
            "ok": True,
            "supported": True,
            "source": "onvif_ptz_service",
            "can_pan_tilt": True,
            "can_zoom": True,
            "can_stop": True,
            "can_presets": False,
            "limits": {"actions": ["stop", "move", "zoom"]},
            "warnings": [],
            "unsupported_reasons": [],
            "raw_secret_exposed": False,
        },
    )

    result = onvif_ptz_capabilities(31, db=FakeDb(camera()), current_user=user())

    assert result["supported"] is True
    assert result["can_pan_tilt"] is True
    assert result["can_zoom"] is True
    assert result["raw_secret_exposed"] is False
    assert "camera-pass" not in str(result)


def test_command_validator_allowlist_and_bounds():
    command = validate_ptz_command_payload({
        "action": "move",
        "direction": "left",
        "speed": 0.2,
        "duration_seconds": 0.5,
        "dry_run": True,
    })

    assert command["action"] == "move"
    assert command["execution_mode"] == "dry_run"
    assert command["pan"] < 0
    assert command["tilt"] == 0

    invalid_payloads = [
        {"action": "preset", "preset": "home"},
        {"action": "move", "direction": "left", "speed": 0.2},
        {"action": "move", "direction": "spin", "speed": 0.2, "duration_seconds": 0.5},
        {"action": "move", "direction": "left", "speed": 5, "duration_seconds": 0.5},
        {"action": "zoom", "direction": "in", "speed": 0.2, "duration_seconds": 30},
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValueError):
            validate_ptz_command_payload(payload)


def test_command_validation_only_never_implies_physical_execution(monkeypatch):
    monkeypatch.setattr(onvif_routes_module, "create_event", lambda **kwargs: None)

    result = onvif_ptz_command(
        31,
        {"action": "stop", "validation_only": True},
        request=request(),
        db=FakeDb(camera(protocol="rtsp")),
        current_user=user(),
    )

    assert result["execution_mode"] == "validation_only"
    assert result["executed"] is False
    assert result["physical_camera_mutated"] is False
    assert result["camera_stopped"] is False


def test_command_rejects_non_onvif_when_execution_is_requested(monkeypatch):
    monkeypatch.setattr(onvif_routes_module, "create_event", lambda **kwargs: None)

    with pytest.raises(HTTPException) as exc:
        onvif_ptz_command(
            31,
            {"action": "stop", "dry_run": False},
            request=request(),
            db=FakeDb(camera(protocol="rtsp")),
            current_user=user(),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "camera_not_onvif"


def test_command_dry_run_response_is_secret_free(monkeypatch):
    monkeypatch.setattr(onvif_routes_module, "decrypt_text", lambda value: "camera-pass")
    monkeypatch.setattr(onvif_routes_module, "create_event", lambda **kwargs: None)
    monkeypatch.setattr(
        onvif_routes_module,
        "execute_onvif_ptz_command",
        lambda **kwargs: {
            "ok": True,
            "action": "move",
            "execution_mode": "dry_run",
            "executed": False,
            "physical_camera_mutated": False,
            "camera_stopped": False,
            "duration_seconds": 0.5,
            "warnings": ["physical_execution_not_requested"],
            "message": "PTZ command validated.",
            "raw_secret_exposed": False,
        },
    )

    result = onvif_ptz_command(
        31,
        {"action": "move", "direction": "up", "speed": 0.1, "duration_seconds": 0.5},
        request=request(),
        db=FakeDb(camera()),
        current_user=user(),
    )

    assert result["execution_mode"] == "dry_run"
    assert result["executed"] is False
    assert result["physical_camera_mutated"] is False
    assert result["raw_secret_exposed"] is False
    assert "camera-pass" not in str(result)


class FakePtz:
    def __init__(self):
        self.calls = []

    def GetNodes(self):
        return [
            SimpleNamespace(
                token="node-token",
                Name="PTZ Node",
                SupportedPTZSpaces=SimpleNamespace(
                    ContinuousPanTiltVelocitySpace=[
                        SimpleNamespace(
                            URI="space",
                            XRange=SimpleNamespace(Min=-1, Max=1),
                            YRange=SimpleNamespace(Min=-1, Max=1),
                        )
                    ],
                    ContinuousZoomVelocitySpace=[
                        SimpleNamespace(URI="zoom", XRange=SimpleNamespace(Min=-1, Max=1))
                    ],
                ),
            )
        ]

    def ContinuousMove(self, payload):
        self.calls.append(("move", payload))

    def Stop(self, payload):
        self.calls.append(("stop", payload))


def test_service_capability_normalization_with_fake_ptz(monkeypatch):
    fake_ptz = FakePtz()
    monkeypatch.setattr(
        "app.services.onvif_service._prepare_ptz_context",
        lambda *args, **kwargs: (object(), fake_ptz, "profile-token"),
    )

    result = get_onvif_ptz_capabilities("host", 80, "user", "secret")

    assert result["supported"] is True
    assert result["source"] == "onvif_ptz_service"
    assert result["can_pan_tilt"] is True
    assert result["can_zoom"] is True
    assert result["can_stop"] is True
    assert result["raw_secret_exposed"] is False
    assert "user:secret" not in str(result)


def test_service_execute_move_sends_stop_after_bounded_movement(monkeypatch):
    fake_ptz = FakePtz()
    monkeypatch.setattr(
        "app.services.onvif_service._prepare_ptz_context",
        lambda *args, **kwargs: (object(), fake_ptz, "profile-token"),
    )
    monkeypatch.setattr("app.services.onvif_service.time.sleep", lambda seconds: None)

    result = execute_onvif_ptz_command(
        "host",
        80,
        "user",
        "secret",
        {"action": "move", "direction": "right", "speed": 0.1, "duration_seconds": 0.2, "dry_run": False},
    )

    assert result["execution_mode"] == "executed"
    assert result["executed"] is True
    assert result["physical_camera_mutated"] is True
    assert result["camera_stopped"] is True
    assert fake_ptz.calls[0][0] == "move"
    assert fake_ptz.calls[-1][0] == "stop"
    assert "user:secret" not in str(result)
