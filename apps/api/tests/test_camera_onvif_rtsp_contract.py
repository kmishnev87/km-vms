import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.routers.cameras as cameras_module
import app.routers.camera_onvif_routes as onvif_routes_module
from app.models.camera import Camera
from app.routers.cameras import assemble_rtsp_url, build_test_url, onvif_profiles, safe_onvif_error
from app.services.onvif_service import (
    _field_meta,
    _supported_video_fields,
    _validate_profile_config_request,
    rtsp_display_uri,
    rtsp_path_from_uri,
)


def test_onvif_stream_uri_contributes_path_not_internal_host():
    camera_uri = "rtsp://internal:secret@192.168.1.50:554/cam/realmonitor?channel=1&subtype=0"

    assert rtsp_path_from_uri(camera_uri) == "/cam/realmonitor?channel=1&subtype=0"
    assert (
        rtsp_display_uri(camera_uri, override_host="public.example.test", override_port=15540)
        == "rtsp://public.example.test:15540/cam/realmonitor?channel=1&subtype=0"
    )


def test_build_test_url_prefers_explicit_rtsp_reachable_endpoint():
    payload = {
        "protocol": "onvif",
        "host": "onvif.example.test",
        "port": 18080,
        "rtsp_host": "rtsp.example.test",
        "rtsp_port": 15540,
        "username": "operator",
        "password": "camera-pass",
        "rtsp_main_url": "/cam/realmonitor?channel=1&subtype=0",
    }

    assert (
        build_test_url(payload)
        == "rtsp://operator:camera-pass@rtsp.example.test:15540/cam/realmonitor?channel=1&subtype=0"
    )


def test_onvif_without_explicit_rtsp_port_uses_rtsp_default_not_onvif_port():
    payload = {
        "protocol": "onvif",
        "host": "external.example.test",
        "port": 20004,
        "rtsp_host": "",
        "rtsp_port": None,
        "username": None,
        "password": None,
        "rtsp_main_url": "/cam/realmonitor?channel=1&subtype=0",
    }

    assert (
        build_test_url(payload)
        == "rtsp://external.example.test:554/cam/realmonitor?channel=1&subtype=0"
    )


def test_rtsp_only_uses_camera_port_not_default_reachable_port():
    payload = {
        "protocol": "rtsp",
        "host": "camera.local",
        "port": 8554,
        "rtsp_host": "",
        "rtsp_port": 554,
        "username": "operator",
        "password": "camera-pass",
        "rtsp_main_url": "/stream1",
    }

    assert build_test_url(payload) == "rtsp://operator:camera-pass@camera.local:8554/stream1"


def test_assemble_rtsp_url_preserves_full_user_url():
    full_url = "rtsp://operator:camera-pass@rtsp.example.test:15540/live"

    assert assemble_rtsp_url("onvif.example.test", 18080, "operator", "new-pass", full_url) == full_url


def test_safe_onvif_error_redacts_credentials_and_long_soap():
    detail = safe_onvif_error(
        Exception("SOAP failure rtsp://operator:camera-pass@192.168.1.50/live " + ("x" * 220))
    )

    assert "camera-pass" not in detail
    assert "rtsp://operator:camera-pass" not in detail
    assert detail == "ONVIF operation failed. Check camera ONVIF service, permissions, and profile support."


def test_onvif_readable_field_is_not_writable_without_options():
    meta = _field_meta("resolution", "1920x1080", readable=True, writable=False)

    assert meta["readable"] is True
    assert meta["writable"] is False
    assert meta["options"] == []


def test_codec_current_value_is_not_writable_without_supported_options():
    class Config:
        token = "profile-token"
        Encoding = "H264"
        RateControl = None
        Resolution = None
        Quality = None

    supported = _supported_video_fields(media=None, cfg=Config())

    assert supported["codec"]["readable"] is True
    assert supported["codec"]["value"] == "H264"
    assert supported["codec"]["writable"] is False
    assert supported["codec"]["options"] == []


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

    def query(self, model):
        return FakeQuery(self.camera)


def test_onvif_profiles_uses_saved_rtsp_reachable_endpoint_without_payload_values():
    camera = Camera(
        id=7,
        name="Saved ONVIF",
        storage_folder_name="saved-onvif",
        enabled=True,
        protocol="onvif",
        host="onvif.example.test",
        port=20001,
        username="operator",
        password_encrypted=None,
        rtsp_main_url="rtsp://saved-rtsp.example.test:10001/cam/realmonitor?channel=1&subtype=0",
    )
    captured = {}

    def fake_fetch_onvif_profiles(**kwargs):
        captured.update(kwargs)
        return {"profiles": [], "rtsp_reachable": {"host": kwargs["rtsp_host"], "port": kwargs["rtsp_port"]}}

    original = onvif_routes_module.fetch_onvif_profiles
    onvif_routes_module.fetch_onvif_profiles = fake_fetch_onvif_profiles
    try:
        result = onvif_profiles(
            {"camera_id": camera.id, "username": "operator", "password": "camera-pass"},
            db=FakeDb(camera),
            current_user=object(),
        )
    finally:
        onvif_routes_module.fetch_onvif_profiles = original

    assert result["ok"] is True
    assert captured["host"] == "onvif.example.test"
    assert captured["port"] == 20001
    assert captured["rtsp_host"] == "saved-rtsp.example.test"
    assert captured["rtsp_port"] == 10001


def test_profile_config_validation_rejects_non_writable_field():
    supported = {
        "resolution": _field_meta("resolution", "1920x1080", readable=True, writable=False),
    }

    try:
        _validate_profile_config_request({"resolution": "1280x720"}, supported)
    except ValueError as exc:
        assert "not writable" in str(exc)
    else:
        raise AssertionError("non-writable setting was accepted")


def test_profile_config_validation_rejects_out_of_range_value():
    supported = {
        "fps": _field_meta("fps", 25, readable=True, writable=True, value_range={"min": 1, "max": 30}),
    }

    try:
        _validate_profile_config_request({"fps": 60}, supported)
    except ValueError as exc:
        assert "above supported range" in str(exc)
    else:
        raise AssertionError("out-of-range setting was accepted")


def test_profile_config_validation_rejects_unknown_field():
    supported = {
        "fps": _field_meta("fps", 25, readable=True, writable=True, value_range={"min": 1, "max": 30}),
    }

    try:
        _validate_profile_config_request({"bitrate_type": "VBR"}, supported)
    except ValueError as exc:
        assert "Unsupported ONVIF setting" in str(exc)
    else:
        raise AssertionError("unknown setting was accepted")


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("contract_tests_ok")
