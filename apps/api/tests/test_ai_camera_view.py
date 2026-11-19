from types import SimpleNamespace

import pytest

from app.services import ai_camera_view as facts
from app.services.ai_rule_frame import matching_references
from recorder_core.ai_camera_view import view_kind as recorder_view
from recorder_core.ai_rules import RuleEngine
from recorder_core.ai_supervisor import AiSupervisor
from test_ai_rules_stage8 import LUMA, SIGNED_LINE, ZONE, config, object_at, run, sample, token


def lens(**values):
    return SimpleNamespace(**{"id": 4, "protocol": "onvif", "host": "192.0.2.4", "port": 80,
        "onvif_path": "/onvif/device_service", "onvif_channel_id": "wide",
        "onvif_profile_token": "wide-main", "onvif_sub_profile_token": "wide-sub",
        "rtsp_main_url": "rtsp://fixture:private@192.0.2.4/wide-main",
        "rtsp_sub_url": "rtsp://fixture:private@192.0.2.4/wide-sub", **values})


def fixed(camera):
    return {"supported": False, "profile_token": camera.onvif_profile_token,
            "unsupported_reasons": ["selected_profile_has_no_ptz"]}


def test_exact_profile_facts_are_shared_and_outages_do_not_erase_ptz(monkeypatch, tmp_path):
    monkeypatch.setattr(facts.settings, "storage_previews", str(tmp_path))
    panorama = lens()
    ptz = lens(id=5, onvif_channel_id="turning", onvif_profile_token="turning-main")
    root = tmp_path / ".ai-events"
    assert facts.view_kind(panorama) == recorder_view(panorama, root) == "unknown"
    assert not facts.remember(panorama, fixed(ptz))
    assert facts.remember(panorama, fixed(panorama))
    assert facts.remember(ptz, {"supported": True, "profile_token": "turning-main"})
    assert not facts.remember(ptz, {"supported": None, "source": "unreachable"})
    assert not facts.remember(ptz, {"supported": False, "profile_token": "turning-main",
                                     "unsupported_reasons": ["ptz_capabilities_not_reported"]})
    assert facts.view_kind(panorama) == recorder_view(panorama, root) == "fixed"
    assert facts.view_kind(ptz) == recorder_view(ptz, root) == "ptz"
    assert facts.view_kind(lens(onvif_profile_token="turning-main")) == "unknown"
    assert recorder_view(lens(rtsp_sub_url="rtsp://192.0.2.4/changed"), root) == "unknown"
    assert recorder_view(lens(protocol="rtsp"), None) == "fixed"
    assert "fixture:" not in (root / "view-facts" / "4.json").read_text()


def test_discovery_retries_only_unknown_and_stops_after_bounded_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(facts.settings, "storage_previews", str(tmp_path))
    known, unknown, calls = lens(), lens(id=6), []
    assert facts.remember(known, fixed(known))
    monkeypatch.setattr(facts, "_candidates", lambda: [known, unknown])
    monkeypatch.setattr(facts, "_stop", SimpleNamespace(is_set=lambda: False))
    monkeypatch.setattr(facts, "_discover", lambda camera: calls.append(camera.id) and False)
    pending = {}
    for at, delay in [(0, 20), (1, 19), (20, 120), (140, None), (20000, None)]:
        assert facts._scan(pending, clock=lambda: at) == delay
    assert calls == [6, 6, 6]


@pytest.mark.parametrize("condition,points,positions", [
    ("crossing", SIGNED_LINE, [.62,.59,.54,.48,.44,.40,.47,.53,.57,.60]),
    ("enter", ZONE, [.22,.24,.26,.36,.38,.39,.39,.39,.39,.29,.25,.23]),
])
@pytest.mark.parametrize("lighting", ["day", "ir", "lamp", "day_to_ir"])
def test_one_fixed_reference_keeps_spatial_rules_active(condition, points, positions, lighting):
    ir = bytes(255-n for n in LUMA)
    lamp = bytes(min(255, 30 + n//2) if i%20 < 10 else 244 - n for i,n in enumerate(LUMA))
    frames = [sample(object_at(x), image={"day":LUMA, "ir":ir, "lamp":lamp,
              "day_to_ir": LUMA if index < 3 else ir}[lighting]) for index, x in enumerate(positions)]
    rule = RuleEngine(config(condition, points=points), "UTC", "fixed")
    hits = [hit for row in run(rule, frames) for hit in row]
    assert any(hit.started and hit.kind == condition for hit in hits)
    assert rule.view == "aligned"


def test_fixed_view_still_checks_frame_geometry_and_signs_a_single_setup_image():
    cfg = config("presence", points=ZONE)
    fixed_view = RuleEngine(cfg, "UTC", "fixed")
    frames = [sample(object_at(.5), size=(640,480))]*6
    assert not any(run(fixed_view, frames))
    assert fixed_view.view == "view_unconfirmed"
    # A refreshed drawing frame on the same fixed input may have any light.
    dark = token(image=bytes(255-n for n in LUMA))
    assert not matching_references(cfg["reference"], dark)
    assert matching_references(cfg["reference"], dark, fixed=True)
    assert not matching_references(cfg["reference"], token(size=(640,480)), fixed=True)


def test_ptz_hides_spatial_rules_after_a_real_image_displacement_until_view_returns():
    rule = RuleEngine(config("presence", points=ZONE), "UTC", "ptz")
    turn = bytes(LUMA[i+7] if i%20 < 13 else 110 for i in range(240))
    stamp = 1_700_000_000
    person = sample(object_at(.5))
    assert any(run(rule, [person]*4, start=stamp))
    assert not any(run(rule, [sample(object_at(.5), image=turn)]*4, start=stamp+4))
    assert rule.view == "view_unconfirmed"
    assert any(run(rule, [person]*5, start=stamp+8))
    assert rule.view == "aligned"


def test_type_rediscovery_updates_only_ai_rules_not_the_media_generation():
    cfg = config("presence", points=ZONE)
    manager = AiSupervisor()
    for view in ("unknown", "fixed", "ptz"):
        manager.configure({4: ("stream-generation", frozenset({"person"}), (cfg, "UTC", view))}, gpu_allowed=False)
        source = manager.sources[4]
        assert source.generation == "stream-generation" and source.rules.view_kind == view
        if view == "unknown":
            before = source
            assert not any(run(source.rules, [sample(object_at(.5))]*5))
            assert source.rules.view == "checking_profile"
        else:
            assert source is before
    manager.stop()
