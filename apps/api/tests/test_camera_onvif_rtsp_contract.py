import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers.cameras import assemble_rtsp_url, build_test_url, safe_onvif_error
from app.services.onvif_service import _field_meta, rtsp_display_uri, rtsp_path_from_uri


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


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("contract_tests_ok")
