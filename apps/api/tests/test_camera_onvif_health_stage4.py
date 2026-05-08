import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.routers.cameras as cameras_module
from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.models.camera import Camera
from app.routers.cameras import onvif_health, onvif_health_check
from app.services.onvif_service import (
    build_onvif_health_contract,
    check_onvif_events_feasibility,
    normalize_ptz_compatibility,
    ptz_validation_response,
    summarize_main_sub_assignment,
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


def user(role="owner"):
    return SimpleNamespace(id=1, username="owner", role=role, is_active=True)


def request():
    return SimpleNamespace(client=None, headers={})


def camera(**overrides):
    data = {
        "id": 41,
        "name": "Stage4",
        "storage_folder_name": "stage4",
        "enabled": True,
        "protocol": "onvif",
        "host": "onvif.example.test",
        "port": 20004,
        "username": "operator",
        "password_encrypted": "encrypted",
        "rtsp_main_url": "/main",
        "rtsp_sub_url": "/sub",
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 10004,
        "rtsp_transport": "tcp",
        "onvif_path": None,
        "onvif_profile_token": "main-token",
        "onvif_channel_id": None,
        "recording_mode": "always",
        "default_live_stream": "sub",
        "default_record_stream": "main",
        "segment_minutes": 5,
        "retention_days": 30,
        "storage_quota_gb": 50,
        "status": "created",
        "last_error": None,
    }
    data.update(overrides)
    return Camera(**data)


def test_health_endpoints_are_registered_and_manage_camera_protected():
    rows = {(item.method, item.path): item for item in ENDPOINT_PERMISSIONS}

    health = rows[("GET", "/cameras/{camera_id}/onvif/health")]
    check = rows[("POST", "/cameras/{camera_id}/onvif/health/check")]

    assert health.decision == "manage_cameras"
    assert check.decision == "manage_cameras"
    assert health.allowed_roles == ("owner", "admin")
    assert check.allowed_roles == ("owner", "admin")
    assert "no camera network probe" in health.notes
    assert "read-only" in check.notes


def test_health_get_non_onvif_returns_safe_unsupported_without_probe():
    result = onvif_health(41, db=FakeDb(camera(protocol="rtsp")), current_user=user())

    assert result["availability"]["onvif_status"] == "unsupported"
    assert result["availability"]["rtsp_status"] == "not_checked"
    assert result["availability"]["recorder_status"] == "not_checked"
    assert result["compatibility_matrix"]["onvif_service"]["status"] == "unsupported"
    assert result["raw_secret_exposed"] is False
    assert "encrypted" not in str(result)


def test_health_deleted_camera_rejected_safely():
    with pytest.raises(HTTPException) as exc:
        onvif_health(404, db=FakeDb(None), current_user=user())

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "camera_not_active"


def test_health_check_missing_credentials_returns_misconfigured(monkeypatch):
    monkeypatch.setattr(cameras_module, "decrypt_text", lambda value: None)
    monkeypatch.setattr(cameras_module, "create_event", lambda **kwargs: None)

    result = onvif_health_check(41, request=request(), db=FakeDb(camera()), current_user=user())

    assert result["availability"]["onvif_status"] == "misconfigured"
    assert result["onvif_misconfigured"] is True
    assert "onvif_credentials_required" in result["warnings"]
    assert result["raw_secret_exposed"] is False
    assert "encrypted" not in str(result)


def test_unreachable_onvif_maps_to_safe_reason_code(monkeypatch):
    monkeypatch.setattr(cameras_module, "decrypt_text", lambda value: "camera-pass")
    monkeypatch.setattr(cameras_module, "create_event", lambda **kwargs: None)
    monkeypatch.setattr(cameras_module, "fetch_onvif_profiles", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("timeout camera-pass raw SOAP")))
    monkeypatch.setattr(cameras_module, "get_onvif_ptz_capabilities", lambda **kwargs: {"supported": False, "unsupported_reasons": ["ptz_check_failed"], "raw_secret_exposed": False})
    monkeypatch.setattr(cameras_module, "check_onvif_events_feasibility", lambda **kwargs: {"events_supported": False, "events_status": "unknown", "reason_codes": ["events_feasibility_check_failed"], "limitations": [], "raw_secret_exposed": False})

    result = onvif_health_check(41, request=request(), db=FakeDb(camera()), current_user=user())

    assert result["availability"]["onvif_status"] == "unreachable"
    assert "timeout" in result["warnings"]
    assert result["raw_secret_exposed"] is False
    assert "camera-pass" not in str(result)
    assert "SOAP" not in str(result)


def test_fake_onvif_profiles_ptz_events_return_normalized_matrix(monkeypatch):
    monkeypatch.setattr(cameras_module, "decrypt_text", lambda value: "camera-pass")
    monkeypatch.setattr(cameras_module, "create_event", lambda **kwargs: None)
    monkeypatch.setattr(
        cameras_module,
        "fetch_onvif_profiles",
        lambda **kwargs: {
            "profiles": [
                {"token": "main-token", "stream_path": "/main", "video": {"codec": "H264"}},
                {"token": "sub-token", "stream_path": "/sub", "video": {"codec": "H264"}},
            ]
        },
    )
    monkeypatch.setattr(
        cameras_module,
        "get_onvif_ptz_capabilities",
        lambda **kwargs: {
            "supported": True,
            "profile_token_available": True,
            "can_pan_tilt": True,
            "can_zoom": False,
            "nodes": [{"can_pan_tilt": True, "can_zoom": False}],
            "raw_secret_exposed": False,
        },
    )
    monkeypatch.setattr(
        cameras_module,
        "check_onvif_events_feasibility",
        lambda **kwargs: {
            "events_supported": True,
            "events_status": "supported",
            "reason_codes": [],
            "limitations": ["feasibility_only_no_subscription_started"],
            "raw_secret_exposed": False,
        },
    )

    result = onvif_health_check(41, request=request(), db=FakeDb(camera()), current_user=user())
    matrix = result["compatibility_matrix"]

    assert matrix["onvif_service"]["status"] == "ok"
    assert matrix["media_profiles"]["profile_count"] == 2
    assert matrix["main_sub_assignment"]["main_confidence"] == "token_exact"
    assert matrix["main_sub_assignment"]["sub_confidence"] == "path_unique"
    assert matrix["ptz"]["status"] == "ok"
    assert matrix["events"]["status"] == "ok"
    assert matrix["redaction"]["raw_secret_exposed"] is False
    assert "camera-pass" not in str(result)


def test_empty_ptz_nodes_do_not_overstate_full_support():
    result = normalize_ptz_compatibility({
        "supported": True,
        "profile_token_available": True,
        "can_pan_tilt": False,
        "can_zoom": False,
        "nodes": [],
    })

    assert result["status"] == "warning"
    assert result["supported"] == "partial"
    assert "ptz_capability_incomplete" in result["reason_codes"]


def test_ptz_validation_only_semantics_are_explicit():
    command = validate_ptz_command_payload({"action": "stop", "validation_only": True})
    result = ptz_validation_response(command)

    assert result["payload_valid"] is True
    assert result["camera_capability_checked"] is False
    assert result["camera_supported"] is None
    assert result["command_executable"] is False
    assert result["executed"] is False


def test_main_sub_path_matching_confidence_edge_cases():
    cam = camera(rtsp_main_url="/same", rtsp_sub_url="/same", onvif_profile_token=None)
    result = summarize_main_sub_assignment(
        cam,
        [
            {"token": "a", "stream_path": "/same"},
            {"token": "b", "stream_path": "/same"},
        ],
    )

    assert result["status"] == "warning"
    assert result["main_confidence"] == "path_ambiguous"
    assert result["sub_confidence"] == "path_ambiguous"
    assert "main_sub_paths_identical" in result["reason_codes"]


class FakeDeviceMgmt:
    def __init__(self, services):
        self.services = services

    def GetServices(self, payload):
        return self.services


class FakeOnvifCamera:
    def __init__(self, host, port, username, password, wsdl):
        self.devicemgmt = FakeDeviceMgmt([
            SimpleNamespace(Namespace="http://www.onvif.org/ver10/events/wsdl", XAddr="http://camera/onvif/events")
        ])


def test_events_feasibility_detects_service_without_subscription(monkeypatch):
    monkeypatch.setattr("app.services.onvif_service.ONVIFCamera", FakeOnvifCamera)
    monkeypatch.setattr("app.services.onvif_service.wsdl_dir", lambda: "/tmp/wsdl")

    result = check_onvif_events_feasibility("host", 80, "user", "camera-pass")

    assert result["events_supported"] is True
    assert result["events_status"] == "supported"
    assert "feasibility_only_no_subscription_started" in result["limitations"]
    assert "camera-pass" not in str(result)


def test_static_health_contract_separates_availability_domains():
    result = build_onvif_health_contract(camera(), check_performed=False)

    assert result["availability"] == {
        "onvif_status": "unknown",
        "rtsp_status": "not_checked",
        "recorder_status": "not_checked",
        "live_status": "not_checked",
    }
    assert result["persisted_last_check"] is False
    assert result["raw_secret_exposed"] is False
