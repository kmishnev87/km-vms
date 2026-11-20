from datetime import datetime, timezone
from random import Random
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.ai_recording import AiRules, RecordingPolicy
from app.services.ai_rule_frame import capture_input, make_reference, matching_references, reference_is_fresh
from app.services import ai_rule_frame
from recorder_core.ai_rules import RuleEngine, window_open
from recorder_core.ai_supervisor import AiSupervisor, Frame, Source

ZONE = [[300, 250], [700, 250], [700, 750], [300, 750]]
SIGNED_LINE = [[500, 300], [500, 700]]
GENERATOR = Random(9834)
LUMA = bytes(GENERATOR.randrange(35, 208) for _ in range(240))


def camera(**overrides):
    return SimpleNamespace(**{"id": 4, "rtsp_main_url": "rtsp://192.0.2.1/stream-a",
        "rtsp_sub_url": "rtsp://192.0.2.1/stream-b", "rtsp_transport": "tcp",
        "default_live_stream": "sub", "default_record_stream": "main", **overrides})


def token(*, size=(800, 450), image=LUMA):
    return make_reference(camera(), *size, image)


def config(kind, *, points=(), masks=(), **fields):
    return AiRules(rules=[{"id":"fence", "kind":kind, "points":points, **fields}], exclusions=masks,
                   reference=token() if points or masks else None).model_dump()


def object_at(x, y=.55, *, confidence=.88, kind="person"):
    return {"kind":kind, "confidence":confidence, "box":[x-.033, y-.10, x+.033, y]}


def sample(*objects, image=LUMA, size=(640, 360)):
    return {"phase":"sample", "luma":image.hex(), "size":list(size), "objects":list(objects)}


def run(rule, samples, *, start=1_700_000_000):
    return [rule.process(result, start+i) for i, result in enumerate(samples)]


def test_scene_optional_whole_frame_objects_accept_independent_causes():
    from app.services.ai_event_history import _sensor_identity

    person = object_at(.55)
    settled = [sample(), sample(person), sample(person)]
    assert not any(run(RuleEngine(config("scene"), "UTC"), settled))
    options = config("scene", scene_objects=True, classes=["person"])
    run_result = run(RuleEngine(options, "UTC"), [
        *settled, sample(image=bytes(len(LUMA))),
    ])
    assert run_result[2][0].kind == "presence" and run_result[2][0].started
    assert run_result[2][0].object_kinds == ("person",)
    assert any(hit.kind == "scene" and hit.started for hit in run_result[3])
    camera = SimpleNamespace(recording_policy={"ai_rules": options})
    assert _sensor_identity(camera, {"rule_id": "fence", "condition": "presence", "objects": ["person"]})
    assert not _sensor_identity(camera, {"rule_id": "fence", "condition": "presence", "objects": ["vehicle"]})
    assert _sensor_identity(camera, {"rule_id": "fence", "condition": "scene", "objects": []})
    with pytest.raises(ValidationError, match="ai_rule_classes_required"):
        config("scene", scene_objects=True, classes=[])


def test_strict_geometry_legacy_actions_and_camera_bound_reference():
    with pytest.raises(ValidationError, match="invalid_ai_polygon|self_intersecting_ai_polygon"):
        config("enter", points=[[200,200], [750,750], [750,200], [200,750]])
    with pytest.raises(ValidationError, match="invalid_ai_line"):
        config("crossing", points=[[100,100], [105,105]])
    compatible = config("presence", record=False, notify=False)
    assert compatible["rules"][0]["record"] and compatible["rules"][0]["notify"]
    assert RecordingPolicy(mode="events", source="ai", profile="full_quality", ai_rules=compatible)
    with pytest.raises(ValidationError, match="ai_frame_reference_required"):
        AiRules(rules=[{"id":"gate","kind":"enter","points":ZONE}])
    signed = make_reference(camera(), 800, 450, LUMA, now=1000)
    assert reference_is_fresh(camera(), signed, now=1001)
    assert not reference_is_fresh(camera(id=9), signed, now=1001)
    assert not reference_is_fresh(camera(rtsp_sub_url="rtsp://192.0.2.2/other"), signed, now=1001)
    assert not reference_is_fresh(camera(), signed, now=4000)
    assert not reference_is_fresh(camera(), "a"+signed, now=1001)
    other = camera(default_record_stream="sub", default_live_stream="main")
    assert capture_input(other, "full_quality") == (other.rtsp_sub_url, "record", "sub")
    assert capture_input(other, "balanced") == (other.rtsp_main_url, "live", "main")
    assert not capture_input(camera(rtsp_sub_url=None), "balanced")[0]
    assert matching_references(signed, token())
    assert not matching_references(signed, token(size=(800,600)))


def test_calibration_frame_uses_real_ffmpeg_and_writes_no_capture_file(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    fixture = tmp_path / "frame.mp4"
    subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=2048x1152:rate=5","-frames:v","1",
                    "-c:v","libx264","-threads","1",str(fixture)], check=True)
    real_run = ai_rule_frame.run_ffmpeg
    def run(args, **kw):
        if "-rtsp_transport" in args:
            args = args[2:]
            args[args.index("-i")+1] = str(fixture)
        return real_run(args, **kw)
    monkeypatch.setattr(ai_rule_frame, "run_ffmpeg", run)
    value = ai_rule_frame.frame(camera(), "balanced")
    assert (value["width"], value["height"]) == (800,450)
    assert "role" not in value and len(value["image"]) < 355000
    assert reference_is_fresh(camera(), value["reference"])
    assert not value["reference"].startswith(value["image"])
    editor = ai_rule_frame.frame(camera(), "balanced", editor=True)
    assert (editor["width"], editor["height"]) == (1920,1080)
    assert (editor["role"], editor["track"]) == ("live", "sub")
    assert reference_is_fresh(camera(), editor["reference"])
    assert list(tmp_path.iterdir()) == [fixture]


def test_directional_finite_crossing_and_disappearance_without_teleport():
    movements = [.62,.59,.54,.48,.44,.40,.47,.53,.57,.60]
    rule = RuleEngine(config("crossing", points=SIGNED_LINE, direction="forward"), "UTC")
    hits = run(rule, [sample(object_at(x)) for x in movements])
    assert [i for i, hit in enumerate(hits) if hit] == [4]
    assert hits[4][0].started
    hits = run(RuleEngine(config("crossing", points=SIGNED_LINE), "UTC"),
               [sample(object_at(x, .86)) for x in movements])
    assert not any(hits)
    hits = run(RuleEngine(config("crossing", points=SIGNED_LINE), "UTC"),
               [sample(object_at(.61)), sample(object_at(.59)), sample(object_at(.57)),
                sample(), sample(object_at(.44)), sample(object_at(.41))])
    assert not any(hits)


def test_zone_transition_and_dwell_can_confirm_but_missing_track_is_not_exit():
    frames = [sample(object_at(x)) for x in [.22,.24,.26,.36,.38,.39,.39,.39,.39,.29,.25,.23]]
    inside = run(RuleEngine(config("enter", points=ZONE, confirm_seconds=1, release_seconds=1), "UTC"), frames)
    outside = run(RuleEngine(config("exit", points=ZONE, confirm_seconds=1, release_seconds=1), "UTC"), frames)
    dwell = run(RuleEngine(config("dwell", points=ZONE, dwell_seconds=3, release_seconds=1), "UTC"), frames)
    assert [i for i,h in enumerate(inside) if h] == [4]
    assert [i for i,h in enumerate(outside) if h] == [10]
    assert dwell[6][0].started and dwell[7] and not dwell[11]
    no_exit = run(RuleEngine(config("exit", points=ZONE), "UTC"),
                  [sample(object_at(.45))]*4 + [sample()]*5)
    assert not any(no_exit)


def test_wrong_aspect_ptz_and_unknown_view_suspend_spatial_rules_then_reconfirm():
    rule = RuleEngine(config("presence", points=ZONE), "UTC")
    time = 1_700_000_000
    obj = object_at(.5)
    assert not rule.process(sample(obj), time)
    assert not rule.process(sample(obj), time+1)
    assert rule.process(sample(obj), time+2) and rule.view == "aligned"
    assert not rule.process(sample(obj, size=(640, 480)), time+3)
    assert rule.view == "view_unconfirmed"
    turned = bytes(255-value for value in LUMA)
    assert not rule.process(sample(obj, image=turned), time+4)
    for i in (5,6,7):
        assert not rule.process(sample(obj), time+i)
    assert rule.process(sample(obj), time+8)
    rule.reset()
    assert not rule.process(sample(obj, size=(1280, 720)), time+9)
    assert not rule.process(sample(obj, size=(1280, 720)), time+10)
    assert rule.process(sample(obj, size=(1280, 720)), time+11)


def test_confidence_privacy_overlap_and_ambiguous_teleport_do_not_start_recording():
    privacy = [[400,300], [500,300], [500,700], [400,700]]
    rule = RuleEngine(config("presence", masks=[privacy], confidence=72), "UTC")
    hits = run(rule, [sample(object_at(.52))]*4 + [sample(object_at(.8,confidence=.64))]*4
                  + [sample(object_at(.8,confidence=.92))]*3)
    assert not any(hits[:9])
    assert hits[9] and hits[9][0].started
    line = RuleEngine(config("crossing", points=SIGNED_LINE), "UTC")
    assert not any(run(line, [sample(object_at(x)) for x in [.8,.81,.80,.2,.20,.21]]))


def test_scene_is_explicit_exposure_tolerant_and_reset_does_not_fire():
    rule = RuleEngine(config("scene", notify=True, record=False), "UTC")
    bright = bytes(n+15 for n in LUMA)
    turned = bytes(255-n for n in LUMA)
    hit = run(rule, [sample(), sample(image=bright), sample(image=turned)])
    assert not any(hit[:2])
    assert hit[2][0].started and hit[2][0].kind == "scene"
    rule.reset()
    assert not rule.process(sample(image=bright), 1_700_000_003)
    no_masked_motion = RuleEngine(config("scene", masks=[ZONE]), "UTC")
    changed = bytearray(LUMA)
    for y in range(3,9):
        for x in range(6,14):
            changed[y*20+x] = 255 - changed[y*20+x]
    assert not any(run(no_masked_motion, [sample(),sample(),sample(image=changed)]))


def test_light_noise_one_frame_reflection_and_short_occlusion_cannot_start_a_zone_activity():
    engine = RuleEngine(config("presence", points=ZONE, confidence=65, confirm_seconds=1), "UTC")
    fine_noise = bytes(min(255, max(0, value + (i%13 - 6)*2)) for i,value in enumerate(LUMA))
    frames = [sample(), sample(image=fine_noise), sample(object_at(.5),image=fine_noise), sample(),
              sample(object_at(.5),image=fine_noise), sample()]
    assert not any(run(engine, frames))
    assert engine.view == "aligned"
    no_scene = run(RuleEngine(config("scene"), "UTC"), [sample(), sample(image=fine_noise), sample()])
    assert not any(no_scene)


def test_schedule_uses_nas_local_date_and_previous_day_for_an_overnight_window():
    schedule = {"days":[1], "start":19*60, "end":7*60}
    assert window_open(schedule, datetime(2026,11,16,19,0))
    assert window_open(schedule, datetime(2026,11,17,6,59))
    assert not window_open(schedule, datetime(2026,11,17,7,0))
    assert not window_open(schedule, datetime(2026,11,17,20,0))
    assert window_open({"days":[2],"start":0,"end":0}, datetime(2026,11,17,23,59))
    start = datetime(2026,11,16,14,0,tzinfo=timezone.utc).timestamp()
    assert any(run(RuleEngine(config("presence", schedule=schedule), "Asia/Yekaterinburg"),
                   [sample(object_at(.8))]*2, start=start))
    assert not any(run(RuleEngine(config("presence", schedule=schedule), "UTC"),
                       [sample(object_at(.8))]*2, start=start))


def test_legacy_toggles_do_not_block_activity_and_failed_view_is_not_an_engine_outage(monkeypatch):
    monkeypatch.setattr("recorder_core.ai_supervisor.time.monotonic", lambda: 103.5)
    source = Source("policy", frozenset({"person"}), 100., stream="fixed")
    manager = AiSupervisor()
    manager.sources[1] = source
    manager.backend = {"backend":"cpu"}
    legacy = config("presence", points=ZONE)
    legacy["rules"][0].update(record=False, notify=False)  # Stored before the update, not normalized by the editor.
    source.rules = RuleEngine(legacy, "UTC")
    for i in range(3):
        manager._accept(Frame(1,"policy","fixed","h264",b"",b"",100.+i,1_700_000_000+i,True), sample(object_at(.5)))
    assert manager.observe(1,"policy").phase == "healthy"
    assert manager.observe(1,"policy").activity == 1_700_000_002
    manager._accept(Frame(1,"policy","fixed","h264",b"",b"",103.,1_700_000_003,True), sample(object_at(.5), size=(480,640)))
    evidence = manager.observe(1,"policy")
    assert evidence.phase == "healthy" and evidence.info["rules_view"] == "view_unconfirmed"
    source.completed = 40.
    assert manager.observe(1,"policy").phase == "stalled"
    assert "rules_view" not in manager.observe(1,"policy").info
