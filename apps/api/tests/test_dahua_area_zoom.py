from types import SimpleNamespace as NS

import pytest

from app.services import dahua_area_zoom as area
from app.services.onvif_service import OnvifConfigurationError


def camera(uri="rtsp://device.test/cam/realmonitor?channel=2&subtype=0", maker="Dahua"):
    requests = []
    def stream(payload):
        requests.append(payload)
        return NS(Uri=uri)
    return NS(devicemgmt=NS(GetDeviceInformation=lambda: NS(Manufacturer=maker)),
              _km_ptz_media=NS(GetStreamUri=stream)), requests


CREDS = {"host": "device.test", "port": 80, "username": "operator", "password": "private-test-password"}
CAPS = "caps.Pan=true\ncaps.Zoom=true\ncaps.MoveDirectly.Version=V3"


def discover(cam):
    return area.discover(cam, "opaque-profile-token", **CREDS)


def test_native_channel_comes_only_from_selected_onvif_stream_not_profile_name(monkeypatch):
    calls = []
    monkeypatch.setattr(area, "_request", lambda *args: calls.append(args) or CAPS)
    cam, reads = camera()
    driver = discover(cam)
    assert reads[0]["ProfileToken"] == "opaque-profile-token"
    assert driver.channel == 2
    assert calls[0][-1] == {"action": "getCurrentProtocolCaps", "channel": 2}
    assert "private-test-password" not in repr(driver)


@pytest.mark.parametrize("uri", ["rtsp://device.test/other?channel=2", "rtsp://device.test/cam/realmonitor",
    "rtsp://device.test/cam/realmonitor?channel=1&channel=2", "rtsp://device.test/cam/realmonitor?channel=0",
    "rtsp://device.test/cam/realmonitor?channel=-1", "rtsp://device.test/cam/realmonitor?channel=257"])
def test_ambiguous_or_missing_channel_is_not_guessed(monkeypatch, uri):
    monkeypatch.setattr(area, "_request", lambda *args: pytest.fail("unexpected CGI request"))
    assert discover(camera(uri)[0]) is None


def test_non_dahua_and_fixed_lens_do_not_gain_native_control(monkeypatch):
    monkeypatch.setattr(area, "_request", lambda *args: pytest.fail("unexpected CGI request"))
    assert discover(camera(maker="Other vendor")[0]) is None
    monkeypatch.setattr(area, "_request", lambda *args: "caps.Pan=false\ncaps.Zoom=false\ncaps.MoveDirectly.Version=V3")
    assert discover(camera()[0]) is None


def test_native_move_uses_documented_rectangle_only_and_requires_exact_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(area, "_request", lambda *args: calls.append(args) or "OK")
    driver = area.DahuaAreaZoom("http://device.test:80", 2, "operator", "private-test-password")
    driver.move({"left": .25, "top": .125, "right": .75, "bottom": .875})
    assert calls[0][-2:] == ("/cgi-bin/ptzBase.cgi", {"action": "moveDirectly", "channel": 2,
        "startPoint[0]": 2048, "startPoint[1]": 1024, "endPoint[0]": 6144, "endPoint[1]": 7168})
    calls.clear()
    monkeypatch.setattr(area, "_request", lambda *args: calls.append(args) or "ERROR")
    with pytest.raises(OnvifConfigurationError) as exc:
        driver.move({"left": .25, "top": .125, "right": .75, "bottom": .875})
    assert len(calls) == 1 and "private-test-password" not in str(exc.value)


def test_read_failure_is_retryable_not_a_permanent_no_capability(monkeypatch):
    def fail(*args):
        raise TimeoutError("private transport details")
    monkeypatch.setattr(area, "_request", fail)
    assert discover(camera()[0]) is False


def test_native_auth_denial_is_not_automatically_retried_as_a_network_failure(monkeypatch):
    def denied(*args):
        raise area._Unavailable()
    monkeypatch.setattr(area, "_request", denied)
    assert discover(camera()[0]) is None


def test_coordinate_rounding_cannot_turn_a_narrow_rectangle_into_a_point(monkeypatch):
    monkeypatch.setattr(area, "_request", lambda *args: pytest.fail("degenerate CGI request"))
    driver = area.DahuaAreaZoom("http://device.test:80", 2, "operator", "private-test-password")
    with pytest.raises(OnvifConfigurationError):
        driver.move({"left": 1.5 / 8192, "right": 2.5 / 8192, "top": .1, "bottom": .2})
