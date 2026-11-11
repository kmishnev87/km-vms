"""Area zoom must use a declared image scale, never generic motor positions."""
from types import SimpleNamespace as NS

import pytest

from app.services import dahua_area_zoom as native
from app.services import onvif_ptz as runtime
from app.services import onvif_service as sdk
from test_ptz_operator_controls_chapter155 import device, isolated_cache, caps, execute, space, CREDS


def area(**extra):
    return {"action": "area_zoom", "left": .5, "top": .25, "right": 1., "bottom": .5,
            "dry_run": False, **extra}


def advertise(device, uri="http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationSpaceFov", high=float("inf")):
    device.ptz.spaces.RelativePanTiltTranslationSpace.append(space(runtime.FOV_SPACE))
    device.ptz.spaces.RelativeZoomTranslationSpace.append(space(uri, 0, high, y=False))
    return uri


def test_standard_area_combines_image_centre_and_optical_fov_factor(device):
    uri = advertise(device)
    assert caps()["can_area_zoom"] is True
    assert caps()["area_zoom_source"] == "onvif_fov"
    assert execute(area())["executed"]
    assert device.ptz.calls[-1] == ("RelativeMove", {"ProfileToken": "ptz-main", "Translation": {
        "PanTilt": {"space": runtime.FOV_SPACE, "x": .5, "y": .25}, "Zoom": {"space": uri, "x": 2.}}})


def test_milestone_extension_uses_ratio_not_inverse_factor(device):
    uri = advertise(device, "http://www.milestonesys.com/ver10/tptz/ZoomSpaces/TranslationSpaceFov")
    execute(area())
    assert device.ptz.calls[-1][1]["Translation"]["Zoom"] == {"space": uri, "x": .5}


def test_tall_rectangle_preserves_its_whole_extent_and_obeys_device_limits(device):
    advertise(device, high=2)
    execute(area(left=.4, right=.5, top=.1, bottom=.9))
    assert device.ptz.calls[-1][1]["Translation"]["Zoom"]["x"] == pytest.approx(1.25)
    execute(area(left=.4, right=.5, top=.4, bottom=.5))
    assert device.ptz.calls[-1][1]["Translation"]["Zoom"]["x"] == 2


def test_generic_zoom_alone_never_enables_area_positioning(device):
    device.ptz.spaces.RelativePanTiltTranslationSpace.append(space(runtime.FOV_SPACE))
    assert caps()["can_point"] and not caps()["can_area_zoom"]
    with pytest.raises(sdk.OnvifConfigurationError, match="Area positioning"):
        execute(area())
    assert not any(name.endswith("Move") for name, _ in device.ptz.calls)


@pytest.mark.parametrize("field,value", [("left", None), ("top", True), ("right", float("nan")),
    ("bottom", float("inf")), ("left", -.1), ("right", 1.01), ("left", 1.),
    ("top", .5), ("right", .500001), ("hold", True)])
def test_invalid_rectangles_cannot_reach_discovery_or_actuation(monkeypatch, field, value):
    monkeypatch.setattr(sdk, "_prepare_ptz_context", lambda *a, **kw: pytest.fail("unexpected device access"))
    with pytest.raises(ValueError):
        execute(area(**{field: value}))


def test_dry_run_and_fixed_lens_cannot_actuate_area_zoom(device):
    assert not execute(area(dry_run=True))["executed"]
    assert not device.prepares
    with pytest.raises(sdk.OnvifConfigurationError):
        execute(area(), profile="panorama-main")
    assert not any(name.endswith("Move") for name, _ in device.ptz.calls)


def test_stop_tombstone_cancels_area_before_and_after_slow_command(device, monkeypatch):
    advertise(device)
    execute({"action": "stop", "control_id": "area-cancelled-001", "dry_run": False})
    assert execute(area(control_id="area-cancelled-001"))["ignored_stale_control"]
    assert not any(name == "RelativeMove" for name, _ in device.ptz.calls)
    original = device.ptz.RelativeMove
    def delayed(payload):
        original(payload)
        execute({"action": "stop", "control_id": "area-cancelled-002", "dry_run": False})
    monkeypatch.setattr(device.ptz, "RelativeMove", delayed)
    result = execute(area(control_id="area-cancelled-002"))
    assert result["camera_stopped"] and result["ignored_stale_control"]


def test_finite_area_can_be_stopped_and_never_renewed_as_a_hold(device):
    advertise(device)
    execute(area(control_id="area-finite-001", sequence=1))
    result = execute({"action": "stop", "control_id": "area-finite-001", "sequence": 2, "dry_run": False})
    assert result["camera_stopped"]
    assert device.ptz.calls[-1][0] == "Stop"


def test_native_adapter_is_optional_profile_bound_and_not_replayed_after_failure(device, monkeypatch):
    calls = []
    driver = NS(move=lambda payload: calls.append(payload))
    monkeypatch.setattr(native, "discover", lambda cam, selected, *args: driver if selected == "ptz-main" else None)
    assert caps()["area_zoom_source"] == "dahua_cgi"
    execute(area())
    assert len(calls) == 1
    def failed(payload):
        calls.append(payload)
        raise sdk.OnvifConfigurationError("ptz_area_zoom_failed", "No confirmation")
    driver.move = failed
    with pytest.raises(sdk.OnvifConfigurationError):
        execute(area())
    assert len(calls) == 2 and device.ptz.calls[-1][0] == "Stop"
    assert not caps("panorama-main")["can_area_zoom"]


def test_transient_optional_probe_never_disables_ptz_or_delays_ordinary_commands(device, monkeypatch):
    attempts = []
    def probe(*args):
        attempts.append(True)
        return False if len(attempts) == 1 else NS(move=lambda payload: None)
    monkeypatch.setattr(native, "discover", probe)
    assert caps()["supported"] and not caps()["can_area_zoom"]
    assert len(attempts) == 1
    now = runtime.time.monotonic()
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now + 11)
    execute({"action": "move", "direction": "left", "speed": .1, "duration_seconds": .1, "dry_run": False})
    assert len(attempts) == 1
    assert caps()["can_area_zoom"] and len(attempts) == 2
