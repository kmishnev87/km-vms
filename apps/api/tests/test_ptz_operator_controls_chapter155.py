from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from app.services import onvif_ptz as runtime
from app.services import onvif_service as sdk


CREDS = dict(host="camera.example.test", port=80, username="operator", password="private-test-password")


def space(uri, low=-1, high=1, y=True):
    return NS(URI=uri, XRange=NS(Min=low, Max=high),
              **({"YRange": NS(Min=low, Max=high)} if y else {}))


def all_spaces():
    return NS(ContinuousPanTiltVelocitySpace=[space("pt/VelocityGenericSpace")],
              ContinuousZoomVelocitySpace=[space("zoom/VelocityGenericSpace", y=False)],
              RelativePanTiltTranslationSpace=[space("pt/TranslationGenericSpace")],
              RelativeZoomTranslationSpace=[space("zoom/TranslationGenericSpace", y=False)],
              AbsolutePanTiltPositionSpace=[space("pt/PositionGenericSpace")],
              AbsoluteZoomPositionSpace=[space("zoom/PositionGenericSpace", 0, 1, y=False)])


class Ptz:
    def __init__(self):
        self.calls = []
        self.spaces = all_spaces()
        self.node = NS(token="node-ptz", Name="Moving lens", SupportedPTZSpaces=self.spaces,
                       MaximumNumberOfPresets=8)
        self.options = NS(Spaces=self.spaces, PTZTimeout=NS(Min=timedelta(seconds=1), Max=timedelta(seconds=250)))
        self.saved = [NS(token="home", Name="Дом", PTZPosition=NS()), NS(token="gate", Name="Калитка", PTZPosition=NS())]
        self.on_move = None

    def GetNode(self, request):
        self.calls.append(("GetNode", request))
        assert request["NodeToken"] == "node-ptz"
        return self.node

    def GetNodes(self):
        return [NS(token="unrelated-node", SupportedPTZSpaces=all_spaces()), self.node]

    def GetConfigurationOptions(self, request):
        self.calls.append(("GetConfigurationOptions", request))
        assert request["ConfigurationToken"] == "cfg-ptz"
        return self.options

    def GetStatus(self, request):
        self.calls.append(("GetStatus", request))
        return NS(Position=NS(PanTilt=NS(x=.2, y=.1, space="pt/PositionGenericSpace"),
                              Zoom=NS(x=.25, space="zoom/PositionGenericSpace")))

    def ContinuousMove(self, request):
        self.calls.append(("ContinuousMove", request))
        if self.on_move:
            self.on_move()

    def RelativeMove(self, request):
        self.calls.append(("RelativeMove", request))

    def AbsoluteMove(self, request):
        self.calls.append(("AbsoluteMove", request))

    def Stop(self, request):
        self.calls.append(("Stop", request))

    def GetPresets(self, request):
        self.calls.append(("GetPresets", request))
        return self.saved

    def GotoPreset(self, request):
        self.calls.append(("GotoPreset", request))


@pytest.fixture(autouse=True)
def isolated_cache():
    runtime.reset_runtime_cache()
    yield
    runtime.reset_runtime_cache()


@pytest.fixture
def device(monkeypatch):
    ptz = Ptz()
    configuration = NS(token="cfg-ptz", NodeToken="node-ptz")
    profiles = [NS(token="panorama-main", VideoSourceConfiguration=NS(SourceToken="fixed"), PTZConfiguration=None),
                NS(token="ptz-main", VideoSourceConfiguration=NS(SourceToken="moving"), PTZConfiguration=configuration),
                NS(token="ptz-sub", VideoSourceConfiguration=NS(SourceToken="moving"), PTZConfiguration=configuration)]
    cam = NS(create_ptz_service=lambda: ptz)
    calls = []

    def prepare(*args, **kwargs):
        calls.append((args, kwargs))
        return cam, []

    monkeypatch.setattr(sdk, "_prepare_camera", prepare)
    monkeypatch.setattr(sdk, "_get_media_service", lambda *args: (NS(GetProfiles=lambda: profiles), None))
    monkeypatch.setattr(runtime.time, "sleep", lambda *args: None)
    return NS(ptz=ptz, cam=cam, profiles=profiles, prepares=calls)


def caps(profile="ptz-main", **kwargs):
    return sdk.get_onvif_ptz_capabilities(**CREDS, profile_token=profile, **kwargs)


def command(action="move", direction="right", **extra):
    return {"action": action, "direction": direction, "speed": .1, "duration_seconds": .5,
            "dry_run": False, **extra}


def execute(payload, profile="ptz-main", owner=1):
    return sdk.execute_onvif_ptz_command(**CREDS, profile_token=profile, control_owner=owner, payload=payload)


def hold(control_id="gesture-0001", sequence=1, **extra):
    return command(hold=True, control_id=control_id, sequence=sequence, **extra)


def test_capabilities_are_bound_to_saved_lens_not_first_profile(device):
    fixed = caps("panorama-main")
    moving = caps()
    assert fixed["supported"] is False
    assert not fixed["can_pan_tilt"] and not fixed["can_zoom"]
    assert moving["supported"] is True
    assert moving["profile_token"] == "ptz-main"
    assert moving["node_token"] == "node-ptz"
    assert moving["can_pan_tilt"] and moving["can_zoom"]
    assert moving["can_presets"]
    assert moving["lease_seconds"] == 1.5
    assert "private-test-password" not in str(moving)


def test_unknown_saved_profile_never_falls_back_to_first_lens(device):
    with pytest.raises(sdk.OnvifConfigurationError, match="no longer available"):
        caps("missing-profile")
    assert not device.ptz.calls


def test_multi_lens_without_profile_is_not_guessed(device):
    with pytest.raises(sdk.OnvifConfigurationError, match="correct lens"):
        caps(None)


def test_single_lens_legacy_camera_can_choose_its_bound_profile(device):
    device.profiles.pop(0)
    assert caps(None)["supported"]
    assert caps(None)["profile_token"] == "ptz-main"


def test_unrelated_node_does_not_grant_other_functions(device):
    device.ptz.spaces.ContinuousZoomVelocitySpace = []
    device.ptz.spaces.RelativeZoomTranslationSpace = []
    device.ptz.spaces.AbsoluteZoomPositionSpace = []
    assert caps()["can_zoom"] is False
    with pytest.raises(sdk.OnvifConfigurationError, match="movement axis"):
        execute(command("zoom", "in"))
    assert not any(name.endswith("Move") for name, _ in device.ptz.calls)


def test_discovery_is_cached_and_invalidates_on_profile_or_credentials(device):
    caps(); caps(); caps()
    assert len(device.prepares) == 1
    caps("ptz-sub")
    assert len(device.prepares) == 2
    sdk.get_onvif_ptz_capabilities(**{**CREDS, "password": "different"}, profile_token="ptz-main")
    assert len(device.prepares) == 3


def test_invalidated_context_is_reloaded_on_request_without_polling(device, monkeypatch):
    caps()
    for context in runtime._contexts.values():
        context.expires = 0
    caps()
    assert len(device.prepares) == 2


def test_idle_time_never_expires_verified_binding_or_delays_stop(device, monkeypatch):
    caps()
    now = runtime.time.monotonic()
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now + 3600)
    execute(command("stop"))
    execute(hold())
    caps()
    assert len(device.prepares) == 1


def test_stop_uses_last_binding_after_failure_without_replaying_move(device, monkeypatch):
    caps()
    monkeypatch.setattr(device.ptz, "ContinuousMove", lambda _: (_ for _ in ()).throw(RuntimeError("lost response")))
    with pytest.raises(RuntimeError):
        execute(hold())
    assert len(device.prepares) == 1
    execute(command("stop"))
    assert len(device.prepares) == 1
    caps()
    assert len(device.prepares) == 2


def test_slow_preset_read_never_holds_stop_lock(device, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    caps()
    entered, release = threading.Event(), threading.Event()
    def slow(_):
        entered.set()
        assert release.wait(3)
        return device.ptz.saved
    monkeypatch.setattr(device.ptz, "GetPresets", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(sdk.get_onvif_ptz_presets, **CREDS, profile_token="ptz-main")
        try:
            assert entered.wait(1)
            stopped = pool.submit(execute, command("stop")).result(timeout=1)
            assert stopped["camera_stopped"]
        finally:
            release.set()
        assert read.result()["presets"]


def test_point_requires_advertised_fov_space_and_does_not_touch_zoom(device):
    assert not caps()["can_point"]
    with pytest.raises(sdk.OnvifConfigurationError):
        execute({"action": "point", "x": .75, "y": .25, "dry_run": False})
    runtime.reset_runtime_cache()
    device.ptz.spaces.RelativePanTiltTranslationSpace.append(space(runtime.FOV_SPACE))
    assert caps()["can_point"]
    result = execute({"action": "point", "x": .75, "y": .25, "dry_run": False})
    assert result["executed"]
    move = device.ptz.calls[-1]
    assert move == ("RelativeMove", {"ProfileToken": "ptz-main", "Translation": {
        "PanTilt": {"space": runtime.FOV_SPACE, "x": .5, "y": .5}}})


@pytest.mark.parametrize("coordinate", [None, True, float("nan"), float("inf"), -.01, 1.01])
def test_point_coordinates_are_bounded_and_finite(coordinate):
    with pytest.raises(ValueError):
        sdk.validate_ptz_command_payload({"action": "point", "x": coordinate, "y": .5})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_speed_and_duration_are_rejected(value):
    for field in ("speed", "duration_seconds"):
        with pytest.raises(ValueError):
            sdk.validate_ptz_command_payload(command(**{field: value}))


@pytest.mark.parametrize("payload", [hold(control_id="short"), hold(sequence=-1), hold(sequence=True),
                                     command(hold=True), command("goto_preset", preset_token=""),
                                     command("goto_preset", preset_token="bad\nname")])
def test_invalid_controls_and_preset_tokens_are_rejected(payload):
    with pytest.raises(ValueError):
        sdk.validate_ptz_command_payload(payload)


def test_continuous_move_uses_profile_space_and_device_timeout(device):
    result = execute(command())
    move = next(payload for name, payload in device.ptz.calls if name == "ContinuousMove")
    assert move["ProfileToken"] == "ptz-main"
    assert move["Timeout"] == timedelta(seconds=1.5)
    assert move["Velocity"]["PanTilt"]["space"] == "pt/VelocityGenericSpace"
    assert result["camera_stopped"]
    assert device.ptz.calls[-1][0] == "Stop"


def test_hold_returns_without_sleep_then_stop_finishes_same_profile(device, monkeypatch):
    monkeypatch.setattr(runtime.time, "sleep", lambda *_: pytest.fail("hold must not block a worker"))
    result = execute(hold())
    assert result["executed"] and not result["camera_stopped"]
    assert result["lease_seconds"] == 1.5
    stopped = execute(command("stop", control_id="gesture-0001", sequence=2))
    assert stopped["camera_stopped"]
    assert device.ptz.calls[-1] == ("Stop", {"ProfileToken": "ptz-main", "PanTilt": True, "Zoom": True})


def test_stop_before_delayed_start_tombstones_the_control(device):
    execute(command("stop", control_id="gesture-0001"))
    result = execute(hold())
    assert result["ignored_stale_control"] and not result["executed"]
    assert not any(name == "ContinuousMove" for name, _ in device.ptz.calls)


def test_old_sequence_and_cancelled_renewal_cannot_restart_movement(device):
    execute(hold(sequence=1))
    renewal = execute(hold(sequence=3))
    stale = execute(hold(sequence=2))
    assert renewal["lease_renewal"] and not renewal["audit_event"]
    assert stale["ignored_stale_control"]
    execute(command("stop", control_id="gesture-0001", sequence=4))
    stale = execute(hold(sequence=5))
    assert stale["ignored_stale_control"]
    assert sum(name == "ContinuousMove" for name, _ in device.ptz.calls) == 2


def test_new_controller_closes_old_controller_and_old_stop_does_not_stop_new(device):
    execute(hold())
    execute(hold("gesture-0002"), owner=2)
    calls = len(device.ptz.calls)
    assert execute(hold(sequence=2))["ignored_stale_control"]
    assert execute(command("stop", control_id="gesture-0001"))["ignored_stale_control"]
    assert len(device.ptz.calls) == calls


def test_release_while_device_start_is_in_flight_stops_late_movement(device):
    device.ptz.on_move = lambda: execute(command("stop", control_id="gesture-0001"))
    result = execute(hold())
    assert result["camera_stopped"] and result["ignored_stale_control"]
    assert device.ptz.calls[-1][0] == "Stop"


def test_failed_hold_start_also_attempts_stop(device):
    def failed():
        raise TimeoutError("lost device reply")
    device.ptz.on_move = failed
    with pytest.raises(TimeoutError):
        execute(hold())
    assert device.ptz.calls[-1][0] == "Stop"


def test_relative_only_device_uses_finite_translation_not_continuous_move(device):
    device.ptz.spaces.ContinuousPanTiltVelocitySpace = []
    assert caps()["movement_modes"]["pan_tilt"] == "relative"
    execute(command())
    move = next(payload for name, payload in device.ptz.calls if name == "RelativeMove")
    assert 0 < move["Translation"]["PanTilt"]["x"] <= .02
    assert not any(name == "ContinuousMove" for name, _ in device.ptz.calls)


def test_absolute_only_device_reads_position_and_uses_small_bounded_step(device):
    device.ptz.spaces.ContinuousPanTiltVelocitySpace = []
    device.ptz.spaces.RelativePanTiltTranslationSpace = []
    assert caps()["movement_modes"]["pan_tilt"] == "absolute"
    execute(command())
    move = next(payload for name, payload in device.ptz.calls if name == "AbsoluteMove")
    assert .2 < move["Position"]["PanTilt"]["x"] <= .25
    assert move["Position"]["PanTilt"]["y"] == .1


def test_invalid_continuous_timeout_uses_finite_fallback(device):
    device.ptz.options.PTZTimeout = NS(Min=timedelta(seconds=100), Max=timedelta(seconds=250))
    assert caps()["movement_modes"]["pan_tilt"] == "relative"


def test_options_failure_and_no_node_is_unknown_not_permanent_unsupported(device):
    device.ptz.GetNode = lambda *_: (_ for _ in ()).throw(TimeoutError())
    device.ptz.GetNodes = lambda: (_ for _ in ()).throw(TimeoutError())
    device.ptz.GetConfigurationOptions = lambda *_: (_ for _ in ()).throw(TimeoutError())
    assert caps()["supported"] is None


def test_saved_positions_are_read_without_any_actuation(device):
    result = sdk.get_onvif_ptz_presets(**CREDS, profile_token="ptz-main")
    assert result["presets"] == [{"token": "home", "name": "Дом"}, {"token": "gate", "name": "Калитка"}]
    assert not any(name in ("ContinuousMove", "GotoPreset", "Stop") for name, _ in device.ptz.calls)


def test_goto_uses_selected_profile_and_existing_saved_position(device):
    result = execute(command("goto_preset", preset_token="gate"))
    assert result["executed"] and not result["camera_stopped"]
    assert device.ptz.calls[-1] == ("GotoPreset", {"ProfileToken": "ptz-main", "PresetToken": "gate"})


def test_missing_or_deleted_saved_position_does_not_trigger_movement(device):
    with pytest.raises(sdk.OnvifConfigurationError, match="no longer available"):
        execute(command("goto_preset", preset_token="missing"))
    assert not any(name == "GotoPreset" for name, _ in device.ptz.calls)


def test_panorama_cannot_be_moved_even_with_valid_payload(device):
    with pytest.raises(sdk.OnvifConfigurationError, match="selected camera profile"):
        execute(command(), profile="panorama-main")
    assert not any(name.endswith("Move") for name, _ in device.ptz.calls)


def test_dry_run_never_discovers_or_moves_the_camera(monkeypatch):
    monkeypatch.setattr(sdk, "_prepare_ptz_context", lambda *_args, **_kwargs: pytest.fail("unexpected network access"))
    result = sdk.execute_onvif_ptz_command(**CREDS, payload=command(dry_run=True))
    assert not result["executed"] and result["camera_supported"] is None
