"""AI delivery, retained snapshots and archive permission contracts on isolated fixtures."""
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.models import AuditEvent, Camera, RecordingSegment, SystemSettings, User
from app.routers import camera_events as routes
from app.routers import chronology
from app.routers.deps import get_current_user
from app.services import ai_event_history as history, camera_events as events, mobile_push
from app.services import recorder_runtime_status

NOW = datetime(2026, 11, 19, 9, 10, 28)
UTC_NOW = NOW.replace(tzinfo=timezone.utc)


@pytest.fixture
def api(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(history.settings, "storage_previews", str(tmp_path))
    monkeypatch.setattr(mobile_push, "wake_local", lambda: None)
    with factory() as db:
        db.add_all([User(id=i, username=role, password_hash="fixture", role=role, is_active=True)
                    for i, role in enumerate(("admin", "viewer", "operator"), 1)])
        db.add(SystemSettings(id=1, system_initialized=True, timezone="Asia/Yekaterinburg", language="ru"))
        db.add(Camera(id=7, name="Въезд", storage_folder_name="entry", host="192.0.2.7", port=554,
                      protocol="rtsp", enabled=True, events_enabled=False, segment_minutes=5,
                      recording_policy={"schema_version": 1, "revision": 2, "mode": "continuous", "profile": "none",
                          "source": "ai", "ai_rules": {"rules": [
                              {"id": "quiet", "kind": "presence", "name": "Ворота тихо", "classes": ["person"], "notify": False, "record": True},
                              {"id": "gate", "kind": "exit", "name": "Зона въезда", "classes": ["vehicle", "person"], "notify": True, "record": False},
                          ]}}))
        db.commit()
    state = {"user": 1}
    app = FastAPI()
    app.include_router(routes.router)

    def database():
        with factory() as db:
            yield db

    def actor():
        with factory() as db:
            return db.get(User, state["user"])

    app.dependency_overrides.update({get_db: database, get_current_user: actor})
    with TestClient(app) as client:
        yield client, factory, state
    engine.dispose()


def offer(api, *, rule="gate", condition="exit", objects=("vehicle",), revision=2, second=0, received=None,
          jpeg=b"", identifier=None, progress=False):
    _, factory, _ = api
    identifier = identifier or uuid.uuid4()
    path = history.root() / "inbox" / (identifier.hex + ".evt")
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = UTC_NOW + timedelta(seconds=second)
    packet = {"v": 2 if progress else 1, "camera_id": 7, "revision": revision, "captured": stamp.timestamp()}
    packet.update({"progress": True} if progress else {"rule_id": rule, "condition": condition, "objects": objects})
    path.write_bytes(json.dumps(packet).encode() + b"\n" + jpeg)
    with factory() as db:
        accepted = history.ingest(db, path, received=received or stamp.replace(tzinfo=None))
    return str(identifier), accepted


def test_events_use_shared_inbox_filters_and_personal_preferences(api):
    client, factory, state = api
    state["user"] = 2
    assert client.put("/camera-events/preferences", json={"notifications": True, "hours_enabled": True,
                      "start_minute": 60, "end_minute": 120}).status_code == 200
    state["user"] = 1
    loud, ok = offer(api, objects=("vehicle","person"))
    assert ok
    assert not offer(api, rule="quiet", condition="presence", objects=("person",), second=5)[1]
    assert not offer(api, revision=1)[1]
    assert not offer(api, objects=("animal",))[1]
    assert not offer(api, second=25, objects=("vehicle","person"))[1]
    assert not offer(api, second=55, objects=("vehicle","person"))[1]
    assert not offer(api, second=57, identifier=uuid.UUID(loud))[1]
    assert not offer(api, progress=True, second=114)[1]  # Same episode, no new alert.
    assert not offer(api, progress=True, second=113)[1]  # A delayed pulse cannot shorten the window.
    assert not offer(api, progress=True, second=174, revision=1)[1]
    quiet, ok = offer(api, rule="quiet", condition="presence", objects=("person",), second=174)
    assert ok
    assert offer(api, second=234, objects=("vehicle","person"))[1]
    page = client.get("/camera-events", params={"source": "ai", "rule": "въезда", "kind": "person",
        "condition": "exit", "until": (UTC_NOW + timedelta(seconds=233)).isoformat()}).json()
    assert [item["id"] for item in page["items"]] == [loud] and page["unread_count"] == 3
    result = client.post("/camera-events/selections", json={"source":"ai", "rule":"Ворота", "condition":"presence",
                         "through":page["read_cursor"], "visible_ids":[quiet]})
    assert result.status_code == 200 and result.json()["visible_ids"] == [quiet] and result.json()["count"] == 1
    assert client.get("/camera-events?source=camera").json()["total"] == 0
    assert not client.get("/camera-events?source=ai&rule=%25").json()["items"]
    state["user"] = 2
    assert client.get("/camera-events?unread=true").json()["total"] == 0
    with factory() as db:
        item = events.serialize(db.get(AuditEvent, loud))
        assert json.loads(mobile_push.event_payload(item))["ai_condition"] == "exit"
        assert "snapshot" not in mobile_push.event_payload(item)
        item = events.serialize(db.get(AuditEvent, quiet))
        assert item["ai"]["notify_requested"] and events.notifications_allowed({}, item, None)
        assert not events.automatic_read_ids(db, 1, [quiet])
        assert events.automatic_read_ids(db, 2, [loud, quiet]) == {loud, quiet}
        assert db.get(AuditEvent, quiet).event_metadata["ai_record"] is False


def test_history_rule_choices_span_pages_and_bulk_selection_matches_exact_camera_and_rule(api):
    client, factory, state = api

    def row(camera, rule, name, seconds, *, source="nas_ai"):
        return AuditEvent(id=str(uuid.uuid4()), category=events.CATEGORY, event_type="camera.person",
            target_type="camera", target_id=str(camera), target_name=f"Camera {camera}", severity="info",
            message_ru="", message_en="", created_at=NOW + timedelta(seconds=seconds),
            event_metadata={"origin": source, "ai_rule_id": rule, "ai_rule_name": name, "ai_condition": "presence"})

    old = row(7, "gate", "Old gate", 0)
    latest = row(7, "gate", "Gate", 1)
    other_camera = row(8, "gate", "Gate", 2)
    with factory() as db:
        db.add_all([old, latest, other_camera, row(7, "expired", "Expired rule", -120),
                    row(7, "camera", "Not AI", 4, source="device"),
                    *(row(7, "other", "Gate", 5 + n) for n in range(26))])
        db.commit()

    state["user"] = 2  # History access does not require managing AI rules.
    since = (UTC_NOW - timedelta(seconds=60)).isoformat()
    response = client.get("/camera-events/ai-rule-options", params={"since": since})
    assert response.status_code == 200
    assert response.json()["items"] == [
        {"ref": "7:gate", "camera_name": "Camera 7", "name": "Gate"},
        {"ref": "7:other", "camera_name": "Camera 7", "name": "Gate"},
        {"ref": "8:gate", "camera_name": "Camera 8", "name": "Gate"},
    ]
    assert len(client.get("/camera-events/ai-rule-options", params={"camera_id": 8, "since": since}).json()["items"]) == 1
    filters = {"source": "ai", "rule_ref": "7:gate", "since": since, "condition": "presence"}
    page = client.get("/camera-events", params={**filters, "limit": 1}).json()
    assert page["total"] == 2 and [item["id"] for item in page["items"]] == [latest.id]
    snapshot = client.post("/camera-events/selections", json={
        **filters, "through": page["read_cursor"], "visible_ids": [latest.id]}).json()
    assert snapshot["count"] == 2
    next_page = client.get("/camera-events", params={**filters, "limit": 1, "offset": 1, "selection_id": snapshot["id"]}).json()
    assert next_page["selection"]["visible_ids"] == [old.id]
    assert client.get("/camera-events", params={"rule_ref": "gate"}).status_code == 422


def test_event_scenario_delivers_legacy_disabled_rule_without_changing_older_read_state(api):
    client, factory, _ = api
    old, ok = offer(api, rule="quiet", condition="presence", objects=("person",))
    assert ok
    with factory() as db:
        older_row = db.get(AuditEvent, old)
        older_row.event_metadata = {**older_row.event_metadata, "ai_notify": False}
        camera = db.get(Camera, 7)
        config = camera.recording_policy["ai_rules"]
        camera.recording_policy = {**camera.recording_policy, "revision": 3, "mode": "events", "profile": "full_quality",
            "prebuffer_seconds": 30, "postrecord_minutes": 2, "event_kinds": ["person"], "ai_rules": config}
        db.commit()
    newer, ok = offer(api, rule="quiet", condition="presence", objects=("person",), second=61, revision=3)
    assert ok
    with factory() as db:
        assert db.get(AuditEvent, newer).event_metadata["ai_record"] is True
        assert db.get(AuditEvent, newer).event_metadata["ai_notify"] is True
    rows = {item["id"]: item for item in client.get("/camera-events").json()["items"]}
    assert rows[old]["read"] and not rows[newer]["read"]


@pytest.fixture
def jpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    return subprocess.check_output(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=navy:size=320x180",
                                    "-frames:v", "1", "-c:v", "mjpeg", "-threads", "1", "-f", "image2pipe", "pipe:1"])


def archive_segment(factory, *, start, end, status="finalized"):
    with factory() as db:
        row = RecordingSegment(camera_id=7, relative_path="entry/fixture.mkv", file_path="entry/fixture.mkv",
            archive_root_id="b92f97a9-8fa3-45c0-8b67-553bb0994c1a",
            started_at=start, ended_at=end, finalized_at=end, status=status, source="recorder", ownership="KM VMS", duration_sec=60)
        db.add(row)
        db.commit()
        return row.id


def test_exact_video_pending_utc_roles_and_snapshot_lifecycle(api, jpeg, monkeypatch, tmp_path):
    client, factory, state = api
    # A segment before the event must never enable either archive or clip.
    archive_segment(factory, start=NOW-timedelta(minutes=1), end=NOW-timedelta(seconds=1))
    current = archive_segment(factory, start=NOW-timedelta(seconds=1), end=None, status="writing")
    item, ok = offer(api, jpeg=jpeg)
    assert ok
    monkeypatch.setattr(events, "now_utc", lambda: NOW)
    monkeypatch.setattr("app.services.ai_event_evidence.now_utc", lambda: NOW)
    monkeypatch.setattr(recorder_runtime_status, "list_camera_recording_states", lambda *_a, **_k: [{
        "heartbeat_current": True, "current_segment_id": current, "stale_current_segment": False}])
    fake_file = tmp_path / "fixture.mkv"
    fake_file.write_bytes(b"isolated chronology metadata fixture")
    monkeypatch.setattr(chronology, "_resolve_segment_path", lambda _: fake_file)
    evidence = f"/camera-events/{item}/evidence"
    assert client.get(evidence).json()["archive"] == {"state":"pending"}
    assert client.get(f"/camera-events/{item}/target?mode=archive").status_code == 404
    with factory() as db:
        row = db.get(RecordingSegment, current)
        row.status, row.ended_at, row.finalized_at = "finalized", NOW + timedelta(seconds=59), NOW + timedelta(minutes=1)
        db.commit()
        history._sweep_frames(db)
        assert db.get(AuditEvent, item).event_metadata["ai_recording_id"] == current
    response = client.get(evidence)
    assert response.status_code == 200, response.json()
    archived = response.json()["archive"]
    assert archived["state"] == "ready" and archived["camera_id"] == 7
    assert archived["timestamp_system"] == "2026-11-19T14:10:27" and archived["timestamp_utc"] == "2026-11-19T09:10:27+00:00"
    assert archived["clip"]["end_ts"] == "2026-11-19T09:10:48+00:00"
    state["user"] = 3
    assert "clip" not in client.get(evidence).json()["archive"]
    state["user"] = 2
    assert client.get(evidence).json()["archive"] == {"state":"forbidden"}
    image = client.get(f"/camera-events/{item}/snapshot")
    assert image.content == jpeg and image.headers["cache-control"] == "private, no-store"
    assert client.post(f"/camera-events/{item}/review", json={"false_positive":True}).status_code == 403
    state["user"] = 1
    response = client.post(f"/camera-events/{item}/review", json={"false_positive":True})
    assert response.status_code == 200 and response.json()["ai"]["false_positive"]
    assert client.get("/camera-events?unread=true").json()["total"] == 1
    with factory() as db:
        row = db.get(RecordingSegment, current)
        row.status, row.deleted_at = "deleted", NOW
        db.commit()
        history._sweep_frames(db)
    assert client.get(evidence).json()["archive"] == {"state":"unavailable"}
    assert client.get(f"/camera-events/{item}/snapshot").status_code == 404
    assert client.get(evidence).json()["event"]["ai"]["false_positive"]


def test_real_keyframe_handoff_off_the_recording_thread(api):
    pytest.importorskip("av")
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    from recorder_core.ai_event_outbox import AiEventOutbox
    data = subprocess.check_output(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=800x450",
                                     "-frames:v", "1", "-c:v", "libx264", "-threads", "1", "-f", "h264", "pipe:1"])
    frame = SimpleNamespace(camera=7, captured=UTC_NOW.timestamp(), codec="h264", extra=b"", data=data)
    outbox = AiEventOutbox(history.root())
    outbox.offer(frame, 2, [("gate", "exit", ("vehicle",))])
    outbox.offer(frame, 2, [("gate", "exit", ("vehicle",))])
    outbox.stop()
    deadline = time.monotonic() + 4
    while not list(outbox.root.glob("*.evt")) and time.monotonic() < deadline:
        time.sleep(.02)
    path, = list(outbox.root.glob("*.evt"))
    header, jpeg = history._read(path)
    assert header["condition"] == "exit" and history.jpeg_size(jpeg, 640, 360) == (640, 360)
    client, factory, _ = api
    with factory() as db:
        assert history.ingest(db, path, received=NOW)
    camera_snapshot = client.get(f"/camera-events/{uuid.UUID(hex=path.stem)}/snapshot")
    assert camera_snapshot.content == jpeg


def test_outbox_coalesces_sustained_positives_and_preserves_first_rule(api, monkeypatch):
    from recorder_core import ai_event_outbox as outbox_module

    monkeypatch.setattr(outbox_module, "isolated_thumbnail", lambda _: b"")
    monkeypatch.setattr(outbox_module, "PROGRESS_INTERVAL", 3600)
    outbox = outbox_module.AiEventOutbox(history.root())
    start = UTC_NOW.timestamp()
    gate = ("gate", "exit", ("vehicle",))
    quiet = ("quiet", "presence", ("person",))

    def send(second, matches):
        outbox.offer(SimpleNamespace(camera=7, captured=start + second), 2, matches)

    send(0, [gate, quiet])
    send(20, [quiet])
    send(40, [])  # Sustained activity without a second rule start.
    deadline = time.monotonic() + 4
    while not list(outbox.root.glob("*.evt")) and time.monotonic() < deadline:
        time.sleep(.02)
    send(100, [quiet])  # Exactly sixty seconds since the last positive.
    outbox.stop()
    paths = list(outbox.root.glob("*.evt"))
    packets = [history._read(path)[0] for path in history._queued_events([(start, path) for path in reversed(paths)])]
    assert [p["captured"] - start for p in packets] == [0, 40, 100]
    assert packets[0]["rule_id"] == "gate" and packets[1]["progress"] is True
    assert packets[2]["rule_id"] == "quiet"


def test_ptz_scene_opener_waits_one_second_and_keeps_the_trigger_time(tmp_path, monkeypatch):
    from recorder_core import ai_event_outbox as module

    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    outbox = module.AiEventOutbox(tmp_path)
    outbox.thread = object()  # Drive only the synchronous admission and deadline paths.

    def frame(at, generation="current", camera=7):
        return SimpleNamespace(camera=camera, generation=generation, arrival=at, captured=1000 + at)

    scene = ("ptz", "scene", ())
    person = ("person", "presence", ("person",))
    trigger = frame(9.9)
    outbox.offer(trigger, 2, [scene], ptz=True)
    assert outbox.queue.empty()
    outbox.follow_frame(frame(10.99), 2)
    outbox.follow_frame(frame(11.0, generation="old"), 2)
    outbox.follow_frame(frame(11.0), 3)
    assert outbox.queue.empty()
    accepted = frame(11.0)
    outbox.follow_frame(accepted, 2)
    outbox.offer(accepted, 2, [person], ptz=True)
    outbox.follow_frame(frame(11.5), 2)
    item = outbox.queue.get_nowait()
    assert item == ((7, 2), trigger.captured, *scene, accepted)
    assert outbox.queue.empty()


def test_person_opener_and_stationary_scene_are_unchanged_and_missing_ptz_frame_has_a_deadline(tmp_path, monkeypatch):
    from recorder_core import ai_event_outbox as module

    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    outbox = module.AiEventOutbox(tmp_path)
    outbox.thread = object()
    person = ("person", "presence", ("person",))
    scene = ("ptz", "scene", ())
    ptz = SimpleNamespace(camera=7, generation="current", captured=1010, arrival=10)
    fixed = SimpleNamespace(camera=8, generation="current", captured=1010, arrival=10)
    later = SimpleNamespace(camera=7, generation="current", captured=1011, arrival=11)
    missing = SimpleNamespace(camera=9, generation="current", captured=1010, arrival=10)
    outbox.offer(ptz, 2, [person], ptz=True)
    outbox.offer(later, 2, [scene], ptz=True)
    outbox.offer(fixed, 2, [scene], ptz=False)
    outbox.offer(missing, 2, [scene], ptz=True)
    assert outbox.queue.get_nowait() == ((7, 2), 1010, *person, ptz)
    assert outbox.queue.get_nowait() == ((8, 2), 1010, *scene, fixed)
    clock[0] = 13.99
    outbox._flush_photo_deadlines()
    assert outbox.queue.empty()
    clock[0] = 14.0
    outbox._flush_photo_deadlines()
    outbox._flush_photo_deadlines()
    assert outbox.queue.get_nowait() == ((9, 2), 1010, *scene, None)
    assert outbox.queue.empty()


def test_old_snapshot_is_linked_if_long_segment_finalizes_late_or_is_already_deleted(api, jpeg):
    client, factory, _ = api
    with factory() as db:
        db.get(Camera, 7).segment_minutes = 60
        db.commit()
    item, ok = offer(api, jpeg=jpeg)
    assert ok
    path = history.frame_path(item)
    age = time.time() - 70 * 60
    os.utime(path, (age, age))
    segment = archive_segment(factory, start=NOW-timedelta(seconds=8), end=NOW+timedelta(minutes=60))
    with factory() as db:
        row = db.get(RecordingSegment, segment)
        row.status, row.deleted_at = "deleted", NOW + timedelta(minutes=70)
        db.commit()
        history._sweep_frames(db)
        assert db.get(AuditEvent, item).event_metadata["ai_snapshot"] is False
    assert not path.exists()
    detail = client.get(f"/camera-events/{item}/evidence").json()["event"]["ai"]
    assert detail["scope"] == "zone" and detail["snapshot_available"] is False
