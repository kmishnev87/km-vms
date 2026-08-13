import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.routers.cameras as cameras_module
import app.routers.camera_onvif_routes as onvif_routes_module
from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.models.camera import Camera
from app.schemas.camera import (
    CameraResponse,
    restore_rtsp_management_value,
    safe_rtsp_management_value,
)
from app.routers.cameras import (
    has_valid_onboarding_proof,
    onvif_discover,
    onvif_error_code,
    onvif_probe,
    register_onvif_probe_proof,
    register_rtsp_test_proof,
    require_save_gate,
    safe_onvif_error,
    update_camera,
)


class FakeQuery:
    def __init__(self, camera=None):
        self.camera = camera

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.camera


class FakeDb:
    def __init__(self, camera=None):
        self.camera = camera
        self.added = []
        self.committed = False

    def query(self, model):
        return FakeQuery(self.camera)

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.committed = True

    def refresh(self, value):
        return None


def user():
    return SimpleNamespace(id=1, username="owner", role="owner", is_active=True)


def request():
    return SimpleNamespace(client=None, headers={})


def camera(**overrides):
    rtsp_scheme = "rtsp"
    data = {
        "id": 7,
        "name": "Existing",
        "storage_folder_name": "existing",
        "enabled": True,
        "protocol": "onvif",
        "host": "onvif.example.test",
        "port": 20003,
        "username": "operator",
        "password_encrypted": None,
        "rtsp_main_url": f"{rtsp_scheme}://" + "operator" + ":" + "old-pass" + "@rtsp.example.test:10003/live",
        "rtsp_sub_url": None,
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 10003,
        "rtsp_transport": "tcp",
        "onvif_path": None,
        "onvif_profile_token": "main",
        "onvif_sub_profile_token": None,
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


def test_discovery_endpoint_returns_stable_sanitized_shape(monkeypatch):
    def fake_discover(timeout_seconds=5):
        return {
            "ok": True,
            "discovery_supported": True,
            "code": "ok",
            "message": "done",
            "candidates": [{"id": "abc", "host": "192.0.2.10", "port": 80, "source": "ws_discovery"}],
            "warnings": [],
            "limitations": ["no_broad_subnet_scan"],
            "timeout_seconds": timeout_seconds,
        }

    monkeypatch.setattr(onvif_routes_module, "discover_onvif_devices", fake_discover)
    result = onvif_discover({"timeout_seconds": 3}, current_user=user())

    assert result["ok"] is True
    assert result["timeout_seconds"] == 3
    assert result["candidates"][0]["host"] == "192.0.2.10"
    assert "password" not in str(result).lower()


def test_discovery_endpoint_handles_unsupported_runtime(monkeypatch):
    monkeypatch.setattr(
        onvif_routes_module,
        "discover_onvif_devices",
        lambda timeout_seconds=5: {
            "ok": True,
            "discovery_supported": False,
            "code": "discovery_not_supported",
            "message": "not available",
            "candidates": [],
            "warnings": [],
            "limitations": ["manual_onvif_probe_available", "no_broad_subnet_scan"],
            "timeout_seconds": timeout_seconds,
        },
    )

    result = onvif_discover({}, current_user=user())

    assert result["discovery_supported"] is False
    assert result["code"] == "discovery_not_supported"


def test_probe_success_returns_safe_summary_and_registers_proof(monkeypatch):
    def fake_probe(**kwargs):
        return {
            "ok": True,
            "code": "ok",
            "device": {"manufacturer": "Vendor", "model": "Model"},
            "onvif": {"host": kwargs["host"], "port": kwargs["port"], "status": "reachable"},
            "rtsp_reachable": {"host": kwargs["rtsp_host"], "port": kwargs["rtsp_port"], "source": "user_override"},
            "media": {"profile_count": 1, "stream_uri_status": "ok"},
            "profiles": [{"token": "main", "rtsp_ready": True}],
        }

    monkeypatch.setattr(onvif_routes_module, "probe_onvif_device", fake_probe)
    payload = {
        "host": "onvif.example.test",
        "port": 20003,
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 10003,
        "username": "operator",
        "password": "camera-" + "pass",
    }
    result = onvif_probe(payload, current_user=user())

    assert result["ok"] is True
    assert result["device"]["manufacturer"] == "Vendor"
    assert result["rtsp_reachable"]["host"] == "rtsp.example.test"
    assert result["onvif_probe_token"]
    assert "camera-" + "pass" not in str(result)
    assert has_valid_onboarding_proof({**payload, "protocol": "onvif", "onvif_probe_token": result["onvif_probe_token"]}) is False


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("401 Unauthorized authentication failed", "wrong_credentials"),
        ("Connection refused", "wrong_port_or_service_unavailable"),
        ("Failed to establish a new connection", "wrong_ip_or_unreachable"),
        ("profiles_unavailable", "profiles_unavailable"),
        ("stream_uri_unavailable", "stream_uri_unavailable"),
        ("ONVIF WSDL missing", "unsupported_onvif"),
        ("timed out", "timeout"),
    ],
)
def test_probe_error_mapping_is_safe(message, code):
    assert onvif_error_code(Exception(message)) == code
    rtsp_scheme = "rtsp"
    secret_uri = f"{rtsp_scheme}://" + "user" + ":" + "secret" + "@example/live"
    token_pair = "token" + "=abc"
    detail = safe_onvif_error(Exception(f"{message} {secret_uri} {token_pair}"))
    assert "secret" not in detail
    assert secret_uri not in detail
    assert "traceback" not in detail.lower()


def test_save_gate_requires_validation_or_manual_confirmation():
    with pytest.raises(HTTPException) as exc:
        require_save_gate({"protocol": "onvif", "host": "host", "port": 80}, connection_sensitive_change=True)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert exc.value.detail["code"] == "camera_validation_required"
    assert require_save_gate(
        {"protocol": "onvif", "host": "host", "port": 80, "manual_confirm_unverified": True},
        connection_sensitive_change=True,
    ) is True


def test_rtsp_host_and_port_fallback_from_legacy_urls():
    rtsp_scheme = "rtsp"
    legacy_url = f"{rtsp_scheme}://" + "operator" + ":" + "pass" + "@legacy.example.test:1554/live"
    cam = camera(rtsp_host=None, rtsp_port=None, rtsp_main_url=legacy_url)

    assert cam.rtsp_reachable_host == "legacy.example.test"
    assert cam.rtsp_reachable_port == 1554


def test_camera_management_response_strips_rtsp_authority_and_query_secrets():
    cam = camera(
        rtsp_main_url="rtsp://operator:sentinel-pass@rtsp.example.test:1554/main?channel=1&token=sentinel-token",
        rtsp_sub_url="rtsp://operator:sentinel-pass@rtsp.example.test:1554/sub?channel=1&subtype=1",
    )
    cam.created_at = datetime.utcnow()
    cam.updated_at = datetime.utcnow()

    payload = CameraResponse.model_validate(cam).model_dump()

    rendered = str(payload)
    assert payload["rtsp_main_url"] == "/main?channel=1&token=redacted"
    assert payload["rtsp_sub_url"] == "/sub?channel=1&subtype=1"
    assert "sentinel-pass" not in rendered
    assert "sentinel-token" not in rendered
    assert "operator@" not in rendered


def test_safe_rtsp_projection_redacts_relative_query_and_restores_it_for_edit_save():
    stored = "rtsp://operator:sentinel-pass@rtsp.example.test:1554/main?channel=1&token=sentinel-token"
    projected = safe_rtsp_management_value(stored)

    assert projected == "/main?channel=1&token=redacted"
    assert restore_rtsp_management_value(projected, stored) == stored
    changed = restore_rtsp_management_value(
        "/main?channel=2&token=redacted",
        stored,
    )
    assert changed == "/main?channel=2&token=sentinel-token"
    assert safe_rtsp_management_value("/sub?secret=sentinel&channel=3") == (
        "/sub?secret=redacted&channel=3"
    )


def test_exact_camera_test_uses_resolved_transport_and_returns_secret_safe_identity(monkeypatch):
    cam = camera(
        rtsp_transport="udp",
        rtsp_main_url=(
            "rtsp://operator:old-pass@rtsp.example.test:10003/live"
            "?channel=1&token=sentinel-token"
        ),
    )
    observed = {}

    def fake_run(command, **_kwargs):
        observed["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_type":"video","codec_name":"h264"}],"format":{}}',
        )

    monkeypatch.setattr(cameras_module.subprocess, "run", fake_run)
    monkeypatch.setattr(cameras_module, "capture_camera_preview", lambda *_args: False)

    result = cameras_module.test_camera(
        {
            "camera_id": cam.id,
            "stream_role": "main",
            "password": "old-pass",
        },
        db=FakeDb(cam),
        current_user=user(),
    )

    assert result["tested_role"] == "main"
    assert result["transport"] == "udp"
    assert result["stream_identity"] == {
        "role": "main",
        "path": "/live?channel=1&token=redacted",
    }
    assert result["display_path"] == "/live?channel=1&token=redacted"
    assert "sentinel-token" not in str(result)
    assert observed["command"][observed["command"].index("-rtsp_transport") + 1] == "udp"


def test_update_non_connection_change_does_not_require_validation(monkeypatch):
    cam = camera()
    events = []
    monkeypatch.setattr(cameras_module, "create_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(cameras_module, "decrypt_text", lambda value: "old-pass")

    payload = cameras_module.CameraUpdate(name="Renamed")
    result = update_camera(cam.id, payload, request=request(), db=FakeDb(cam), current_user=user())

    assert result.name == "Renamed"
    assert events[0]["metadata"]["validation_state"] == "unchanged"


def test_update_connection_change_can_be_saved_as_manual_unverified(monkeypatch):
    cam = camera()
    events = []
    monkeypatch.setattr(cameras_module, "create_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(cameras_module, "decrypt_text", lambda value: "old-pass")

    payload = cameras_module.CameraUpdate(rtsp_host="new-rtsp.example.test", manual_confirm_unverified=True)
    result = update_camera(cam.id, payload, request=request(), db=FakeDb(cam), current_user=user())

    assert result.rtsp_host == "new-rtsp.example.test"
    assert result.status == "manual_unverified"
    assert result.last_error == "updated_unverified"
    assert events[0]["metadata"]["validation_state"] == "manual_unverified"


def test_update_onvif_final_payload_validation_token_allows_verified_save(monkeypatch):
    cam = camera()
    events = []
    monkeypatch.setattr(cameras_module, "create_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(cameras_module, "decrypt_text", lambda value: "old-pass")

    final_payload = {
        "protocol": "onvif",
        "host": "onvif.example.test",
        "port": 20003,
        "username": "operator",
        "password": "old-pass",
        "rtsp_main_url": "/main-final",
        "rtsp_sub_url": "/sub-final",
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 10003,
        "rtsp_transport": "tcp",
        "onvif_path": "",
        "onvif_profile_token": "main-final",
        "onvif_sub_profile_token": "sub-final",
        "onvif_channel_id": "",
        "default_live_stream": "sub",
        "default_record_stream": "main",
    }
    main_token = register_rtsp_test_proof(final_payload, "main")
    sub_token = register_rtsp_test_proof(final_payload, "sub")
    probe_token = register_onvif_probe_proof(final_payload)

    payload = cameras_module.CameraUpdate(
        **{key: value for key, value in final_payload.items() if key != "password"},
        main_validation_token=main_token,
        sub_validation_token=sub_token,
        onvif_probe_token=probe_token,
        manual_confirm_unverified=False,
    )
    result = update_camera(cam.id, payload, request=request(), db=FakeDb(cam), current_user=user())

    assert result.onvif_profile_token == "main-final"
    assert result.status == "created"
    assert events[0]["metadata"]["validation_state"] == "verified"


def test_update_onvif_changed_after_test_requires_new_proof(monkeypatch):
    cam = camera()
    monkeypatch.setattr(cameras_module, "create_event", lambda **kwargs: None)
    monkeypatch.setattr(cameras_module, "decrypt_text", lambda value: "old-pass")

    final_payload = {
        "protocol": "onvif",
        "host": "onvif.example.test",
        "port": 20003,
        "username": "operator",
        "password": "old-pass",
        "rtsp_main_url": "/main-final",
        "rtsp_sub_url": "/sub-final",
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 10003,
        "rtsp_transport": "tcp",
        "onvif_path": "",
        "onvif_profile_token": "main-final",
        "onvif_sub_profile_token": "sub-final",
        "onvif_channel_id": "",
        "default_live_stream": "sub",
        "default_record_stream": "main",
    }
    main_token = register_rtsp_test_proof(final_payload, "main")
    sub_token = register_rtsp_test_proof(final_payload, "sub")
    probe_token = register_onvif_probe_proof(final_payload)

    changed_payload = {key: value for key, value in final_payload.items() if key != "password"}
    changed_payload["rtsp_main_url"] = "/changed-after-test"
    payload = cameras_module.CameraUpdate(
        **changed_payload,
        main_validation_token=main_token,
        sub_validation_token=sub_token,
        onvif_probe_token=probe_token,
    )
    with pytest.raises(HTTPException) as exc:
        update_camera(cam.id, payload, request=request(), db=FakeDb(cam), current_user=user())

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert exc.value.detail["code"] == "camera_validation_required"


def test_validation_proof_covers_stream_transport_and_onvif_fields():
    payload = {
        "protocol": "onvif",
        "host": "onvif.example.test",
        "port": 20003,
        "username": "operator",
        "password": "camera-" + "pass",
        "rtsp_main_url": "/main",
        "rtsp_sub_url": "/sub",
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 10003,
        "rtsp_transport": "tcp",
        "onvif_path": "/onvif/device_service",
        "onvif_profile_token": "main",
        "onvif_sub_profile_token": "sub",
        "onvif_channel_id": "channel-a",
        "default_live_stream": "sub",
        "default_record_stream": "main",
    }
    main_token = register_rtsp_test_proof(payload, "main")
    sub_token = register_rtsp_test_proof(payload, "sub")
    probe_token = register_onvif_probe_proof(payload)
    proofs = {
        "main_validation_token": main_token,
        "sub_validation_token": sub_token,
        "onvif_probe_token": probe_token,
    }

    assert has_valid_onboarding_proof({**payload, **proofs}) is True
    assert has_valid_onboarding_proof({**payload, **proofs, "name": "metadata-only"}) is True
    assert has_valid_onboarding_proof({**payload, **proofs, "rtsp_main_url": "/changed"}) is False
    assert has_valid_onboarding_proof({**payload, **proofs, "rtsp_host": "other.example.test"}) is False
    assert has_valid_onboarding_proof({**payload, **proofs, "rtsp_port": 10004}) is False
    assert has_valid_onboarding_proof({**payload, **proofs, "rtsp_transport": "udp"}) is False
    assert has_valid_onboarding_proof({**payload, **proofs, "onvif_path": "/other"}) is False
    assert has_valid_onboarding_proof({**payload, **proofs, "onvif_profile_token": "changed-main"}) is False
    assert has_valid_onboarding_proof({**payload, **proofs, "onvif_sub_profile_token": "changed-sub"}) is False
    assert has_valid_onboarding_proof({**payload, **proofs, "onvif_channel_id": "channel-b"}) is False


def test_preview_token_file_alone_does_not_satisfy_save_gate():
    assert has_valid_onboarding_proof({"preview_token": "preview-only", "protocol": "rtsp", "host": "cam"}) is False


def test_invalid_discovery_probe_numeric_inputs_return_4xx():
    with pytest.raises(HTTPException) as exc:
        onvif_discover({"timeout_seconds": "bad"}, current_user=user())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_timeout_seconds"

    with pytest.raises(HTTPException) as exc:
        onvif_probe({"host": "onvif.example.test", "port": "bad"}, current_user=user())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_port"

    with pytest.raises(HTTPException) as exc:
        onvif_probe({"host": "onvif.example.test", "rtsp_port": 0}, current_user=user())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "rtsp_port_out_of_range"


def test_audit_metadata_redacts_connection_secret_values(monkeypatch):
    cam = camera()
    events = []
    monkeypatch.setattr(cameras_module, "create_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(cameras_module, "decrypt_text", lambda value: "old-pass")

    payload = cameras_module.CameraUpdate(rtsp_main_url="/changed", manual_confirm_unverified=True)
    update_camera(cam.id, payload, request=request(), db=FakeDb(cam), current_user=user())

    changed = events[0]["metadata"]["changed"]
    assert changed["rtsp_main_url"]["value_redacted"] is True
    assert "/changed" not in str(events[0]["metadata"])


def test_new_onvif_endpoints_are_registered_with_manage_cameras():
    decisions = {(item.method, item.path): item.decision for item in ENDPOINT_PERMISSIONS}

    assert decisions[("POST", "/cameras/onvif/discover")] == "manage_cameras"
    assert decisions[("POST", "/cameras/onvif/probe")] == "manage_cameras"
