"""Step-2 contracts: prepared policies, bounded signals, independent attention."""
import dataclasses
import json
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db
from app.models import AuditEvent, Camera, RecordingSegment, SystemSettings, User
from app.routers import ai_recording as editor_router
from app.routers.ai_recording import router
from app.routers.deps import get_current_user
from app.schemas.ai_recording import DetectionEvidence, RecordingPolicy, RecordingSignal
from app.services import ai_recording as domain, camera_events as history
from app.services import ai_rule_frame as rule_frame
from app.services.onvif_events import Signal

NOW = datetime(2026, 11, 18, 12)
GROUP = {"device_id": "test-device", "fingerprint": "generation-1", "cameras": [{"id": 1, "name": "First", "events_enabled": True}]}


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    with factory() as db:
        db.add(User(id=1, username="test-admin", password_hash="fixture-only", role="admin", is_active=True))
        db.add(SystemSettings(id=1, system_initialized=True, timezone="UTC", language="en", storage_path="/not-mounted/archive"))
        for cid in (1, 2, 3):
            db.add(Camera(id=cid, name=f"Camera {cid}", storage_folder_name=f"cam{cid}", host="192.0.2.1", port=80,
                protocol="onvif", enabled=False, events_enabled=True, default_record_stream="sub", default_live_stream="main"))
        db.commit()
    app = FastAPI(); app.include_router(router)
    def database():
        with factory() as db:
            yield db
    def actor():
        with factory() as db:
            return db.get(User, 1)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = actor
    with TestClient(app) as client:
        yield client, factory
    engine.dispose()


def event_policy(source="camera", **kwargs):
    return {"mode": "events", "source": source, "profile": "balanced", "prebuffer_seconds": 30, **kwargs}


def prepare(client, cid=1, policy=None, revision=0):
    return client.put(f"/ai-recording/cameras/{cid}", json={"expected_revision": revision, "policy": policy or event_policy()})


def camera_signal(seconds=0, **kwargs):
    stamp = NOW + timedelta(seconds=seconds)
    return Signal("RuleEngine/CellMotionDetector/Motion", "motion", True, False, stamp, stamp, False,
                  source={"VideoSourceConfigurationToken": "lens1"}, **kwargs)


def test_read_defaults_are_nonmutating_and_keep_pause_and_logical_streams(env):
    client, factory = env
    value = client.get("/ai-recording/cameras").json()
    assert value["runtime_active"] is False
    row = value["cameras"][0]
    assert row["policy"]["mode"] == "continuous" and row["policy"]["source"] == "camera"
    assert row["policy"]["revision"] == 0 and row["recording_enabled"] is False
    assert row["record_stream"] == "sub" and row["live_stream"] == "main"
    assert row["runtime"]["status"] == "prepared_not_active"
    assert row["runtime"]["actual_status"] == "paused"
    assert row["protocol"] == "onvif"
    assert client.get("/ai-recording/emergency").json()["enabled"] is False
    with factory() as db:
        assert db.get(Camera, 1).recording_policy is None
        assert db.get(Camera, 1).recording_mode == "always"
        assert db.query(AuditEvent).count() == 0


@pytest.mark.parametrize("role,status", [("owner", 200), ("admin", 200), ("operator", 403), ("viewer", 403)])
def test_role_enforcement_is_server_side(env, role, status):
    client, factory = env
    with factory() as db:
        db.get(User, 1).role = role; db.commit()
    assert client.get("/ai-recording/cameras").status_code == status
    assert client.get("/ai-recording/emergency").status_code == status
    assert prepare(client).status_code == status
    base = "/ai-recording/cameras/1/rule-frame"
    if status == 403:
        assert client.post(base).status_code == 403
        assert client.get(base + "/saved").status_code == 403
        assert client.post("/ai-recording/cameras/1/retry?source=camera").status_code == 403
    assert client.put("/ai-recording/emergency", json={"expected_enabled": False, "enabled": True}).status_code == status


def test_retry_uses_explicit_source_on_camera_with_both_ai_and_subscription(env, monkeypatch):
    client, _ = env
    assert prepare(client, policy={"mode": "continuous", "source": "ai", "profile": "none"}).status_code == 200
    calls = []
    monkeypatch.setattr(editor_router, "retry_camera_events", lambda camera: calls.append(("camera", camera.id)) or "queued")
    monkeypatch.setattr(domain, "queue_ai_retry", lambda db, cid: calls.append(("ai", cid)) or "queued")
    path = "/ai-recording/cameras/1/retry"
    assert client.post(path + "?source=camera").json() == {"status": "queued"}
    assert client.post(path + "?source=ai").json() == {"status": "queued"}
    assert client.post(path).json() == {"status": "queued"}  # previously installed client
    assert calls == [("camera", 1), ("ai", 1), ("ai", 1)]


def test_web_rule_frame_is_saved_with_policy_and_reopens_without_camera_io(env, monkeypatch, tmp_path):
    client, factory = env
    monkeypatch.setattr(rule_frame.settings, "storage_previews", str(tmp_path))
    captures = []
    luma = bytes((i * 27) % 256 for i in range(240))

    def capture(camera, profile, *, editor):
        assert editor and profile == "full_quality"
        token = rule_frame.make_reference(camera, 1600, 900, luma, editor=True)
        captures.append(token)
        return {"image": "YWJj", "reference": token, "width": 1600, "height": 900, "role": "record", "track": "sub"}

    monkeypatch.setattr(editor_router, "frame", capture)
    base = "/ai-recording/cameras/1/rule-frame"
    assert client.get(base + "/saved").status_code == 404
    first = client.post(base + "?profile=full_quality").json()
    latest = client.post(base + "?profile=full_quality").json()
    assert client.get(base + "/saved").status_code == 404  # a preview is not a saved policy
    rule = {"id": "gate", "kind": "enter", "points": [[200, 200], [800, 200], [800, 800]]}
    policy = event_policy("ai", profile="full_quality", ai_rules={"rules": [rule], "reference": first["reference"]})
    assert prepare(client, policy=policy).status_code == 409  # superseded unstored draft
    policy["ai_rules"]["reference"] = latest["reference"]
    assert prepare(client, policy=policy).status_code == 200
    restored = client.get(base + "/saved")
    assert restored.json() == latest
    assert restored.headers["Cache-Control"] == "private, no-store"
    assert len(captures) == 2
    assert prepare(client, cid=2, policy=policy).status_code == 409
    assert client.get("/ai-recording/cameras/2/rule-frame/saved").status_code == 404
    client.post(base + "?profile=full_quality").raise_for_status()
    client.post(base + "?profile=full_quality").raise_for_status()
    assert len(list(rule_frame.editor_directory(1).glob("*.json"))) == 2
    assert client.get(base + "/saved").json() == latest  # a newer candidate didn't replace it
    assert len(captures) == 4
    monkeypatch.setattr(rule_frame, "FRAME_LIFETIME", -1)
    rule["points"][0] = [150, 200]
    assert prepare(client, policy=policy, revision=1).status_code == 200  # still editable on the exact saved frame
    assert client.get(base + "/saved").json() == latest
    with factory() as db:
        assert "image" not in db.get(Camera, 1).recording_policy["ai_rules"]


@pytest.mark.parametrize("policy", [
    event_policy("none"), event_policy("ai", profile="economical", prebuffer_seconds=0),
    event_policy(profile="economical"), event_policy(postrecord_minutes=0), event_policy(postrecord_minutes=6),
    event_policy(prebuffer_seconds=121), event_policy(prebuffer_seconds=True),
    event_policy(event_kinds=[]), event_policy(event_kinds=["motion", "motion"]),
    {"mode": "linked", "source": "none", "profile": "balanced"},
    {"mode": "continuous", "profile": "balanced"}, event_policy(password="must-not-be-accepted"),
])
def test_invalid_combinations_never_write(env, policy):
    client, factory = env
    assert prepare(client, policy=policy).status_code == 422
    with factory() as db:
        assert db.get(Camera, 1).recording_policy is None


def test_policy_cas_shared_readback_and_no_recorder_activation(env):
    client, factory = env
    assert prepare(client).json()["policy"]["revision"] == 1
    assert prepare(client).status_code == 409
    assert prepare(client, revision=1).json()["policy"]["revision"] == 1  # no-op
    with factory() as db:
        camera = db.get(Camera, 1)
        assert not camera.enabled and camera.recording_mode == "always" and camera.events_enabled
        assert camera.default_record_stream == "sub"
        assert db.query(AuditEvent).count() == 1


def test_explicit_continuous_choice_preserves_pause_without_migrating_old_settings(env):
    client, factory = env
    with factory() as db:
        db.get(Camera, 1).recording_mode = "manual"
        db.commit()
    assert client.get("/ai-recording/cameras/1").json()["runtime"]["legacy_recording_mode"] == "manual"
    response = prepare(client, policy={"mode": "continuous", "source": "camera"})
    assert response.status_code == 200
    assert response.json()["runtime"]["actual_status"] == "paused"
    assert response.json()["runtime"]["legacy_recording_mode"] == "always"
    with factory() as db:
        assert not db.get(Camera, 1).enabled
        assert db.get(Camera, 1).events_enabled


def test_wait_start_and_emergency_use_current_writer_and_recording_gates(env):
    client, factory = env
    assert prepare(client).status_code == 200
    with factory() as db:
        camera = db.get(Camera, 1)
        camera.enabled = True
        control = {"status": "waiting", "source_status": "available", "job_id": None}
        status = lambda **values: domain.camera_row(camera, {"event_control": control, **values})["runtime"]
        assert status(current_failure=True)["actual_status"] == "waiting"  # prior job's failure is not a new incident
        control.update(source_status="exhausted")
        assert status()["actual_status"] == "source_fault"
        control.update(status="recording", request="emergency", job_id="new")
        assert status(job_id="old", confirmed_recording=True)["actual_status"] == "starting"
        assert not status(job_id="old", confirmed_recording=True)["emergency_recording"]
        assert status(job_id="new", confirmed_recording=True)["emergency_recording"]
        suspended = domain.camera_row(camera, {"event_control": control, "job_id": "new", "confirmed_recording": True},
                                      storage_suspended=True)["runtime"]
        assert suspended["actual_status"] == "storage_suspended" and not suspended["emergency_recording"]


def test_link_diagnostic_and_retry_are_for_the_event_source(env, monkeypatch):
    from app.services import recorder_runtime_status as recorder_status
    client, factory = env
    assert prepare(client).status_code == 200
    assert prepare(client, cid=2, policy={"mode": "linked", "profile": "economical", "linked_camera_id": 1}).status_code == 200
    with factory() as db:
        db.get(Camera, 1).enabled = True
        db.get(Camera, 2).enabled = True
        db.get(Camera, 1).recording_signal_state = {"channel": {"policy_revision": 1,
            "phase": "exhausted", "incident": {"reason": "timeout", "failed_attempts": 5}}}
        db.commit()
    def state_rows(db, *, camera_ids, include_finalized_history):
        assert not include_finalized_history
        return [{"camera_id": 2, "event_control": {"status": "linked_source_fault", "link": {
            "origin_camera_id": 1, "source_event_status": "exhausted",
        }}}] if 2 in camera_ids else []
    monkeypatch.setattr(recorder_status, "list_camera_recording_states", state_rows)
    view = client.get("/ai-recording/cameras/2").json()["runtime"]
    assert view["actual_status"] == "linked_source_fault"
    assert view["retry_camera_id"] == 1 and view["incident"]["failed_attempts"] == 5


def test_link_graph_rejects_missing_self_and_indirect_cycles(env):
    client, _ = env
    linked = lambda cid: {"mode": "linked", "source": "none", "profile": "full_quality", "linked_camera_id": cid}
    assert prepare(client, policy=linked(1)).status_code == 422
    assert prepare(client, policy=linked(99)).status_code == 422
    assert prepare(client, policy=linked(2)).status_code == 200
    assert prepare(client, cid=2, policy=linked(3)).status_code == 200
    assert prepare(client, cid=3, policy=linked(1)).status_code == 422


def test_deleted_camera_is_not_a_configuration_or_link_target(env):
    client, factory = env
    with factory() as db:
        db.get(Camera, 2).deleted_at = NOW; db.commit()
    assert client.get("/ai-recording/cameras/2").status_code == 404
    assert prepare(client, cid=2).status_code == 404
    assert len(client.get("/ai-recording/cameras").json()["cameras"]) == 2


def test_emergency_is_separate_opt_in_cas_and_no_activation(env):
    client, factory = env
    value = client.put("/ai-recording/emergency", json={"expected_enabled": False, "enabled": True}).json()
    assert value["enabled"] and not value["runtime_active"]
    assert not value["overrides_manual_pause"] and not value["overrides_storage_gates"]
    assert client.put("/ai-recording/emergency", json={"expected_enabled": False, "enabled": False}).status_code == 409
    assert client.put("/ai-recording/emergency", json={"expected_enabled": True, "enabled": "false"}).status_code == 422
    with factory() as db:
        assert all(not camera.enabled and camera.recording_mode == "always" for camera in db.query(Camera))


def test_history_suppression_and_muted_attention_do_not_suppress_activity(env, monkeypatch):
    client, factory = env
    assert prepare(client).status_code == 200
    clock = [NOW]
    monkeypatch.setattr(history, "now_utc", lambda: clock[0])
    with factory() as db:
        history.save_preferences(db, 1, {"notifications": False})
        first = history.record_signal(db, GROUP, GROUP["cameras"][0], camera_signal())
        assert first
        assert history.marked_read_ids(db, 1, [first]) == {first}
        for second in (10, 30, 44):
            clock[0] = NOW + timedelta(seconds=second)
            assert history.record_signal(db, GROUP, GROUP["cameras"][0], camera_signal(second)) is None
        state = db.get(Camera, 1).recording_signal_state
        assert state["last_activity_at"] == domain.iso(clock[0])
        assert history.event_query(db).count() == 1
        history.delete_camera_events(db, ids=[first])
        assert db.get(Camera, 1).recording_signal_state["last_activity_at"] == domain.iso(clock[0])


def test_history_budget_never_discards_control_signal(env, monkeypatch):
    client, factory = env
    prepare(client)
    monkeypatch.setattr(history, "now_utc", lambda: NOW)
    with factory() as db:
        assert history.record_signal(db, GROUP, GROUP["cameras"][0], camera_signal(), history_allowed=False) is None
        assert history.event_query(db).count() == 0
        assert db.get(Camera, 1).recording_signal_state["last_activity_at"] == domain.iso(NOW)


def test_source_opt_out_retires_old_history_after_worker_configuration_changes(env, monkeypatch):
    from app.services import camera_events_worker as worker
    _, factory = env
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "renew_worker_lease", lambda *a, **k: None)
    monkeypatch.setattr(history, "now_utc", lambda: NOW)
    with factory() as db:
        camera = db.get(Camera, 1)
        lens = {**GROUP["cameras"][0], "revision": worker.camera_receiving_revision(camera)}
        group = {**GROUP, "cameras": [lens]}
        event = history.record_signal(db, group, lens, camera_signal())
        assert db.get(AuditEvent, event).event_metadata["active"] is True
        camera.events_enabled = False
        db.commit()
    worker.Receiver(group)._disconnect(SimpleNamespace())
    with factory() as db:
        metadata = db.get(AuditEvent, event).event_metadata
        assert metadata["active"] is None and metadata["state_unconfirmed"]


def test_actual_receiver_passes_activity_after_120_history_messages(env, monkeypatch):
    from app.services import camera_events_worker as worker
    client, factory = env
    prepare(client)
    clock = [NOW]
    monkeypatch.setattr(history, "now_utc", lambda: clock[0])
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "_stop", threading.Event())
    monkeypatch.setattr(worker, "_states", {})
    lease = SimpleNamespace(worker_key="fixture-events", owner_instance_id="fixture-owner", fencing_token=1)
    monkeypatch.setattr(worker, "acquire_worker_lease", lambda *a, **k: lease)
    monkeypatch.setattr(worker, "renew_worker_lease", lambda *a, **k: None)
    monkeypatch.setattr(worker, "release_worker_lease", lambda *a, **k: None)
    monkeypatch.setattr(worker, "group_is_current", lambda *a: True)
    monkeypatch.setattr(worker, "profile_identities", lambda _: [])
    monkeypatch.setattr(worker, "signal_camera", lambda *a: (GROUP["cameras"][0], True))
    def parse(value):
        clock[0] = value.occurred_at
        return value
    monkeypatch.setattr(worker, "parse_notification", parse)
    group = {**GROUP, "host": "192.0.2.1", "port": 80, "username": "fixture", "password": "fixture", "onvif_path": None}
    receiver = worker.Receiver(group)
    class Device:
        batch = 0
        def __init__(self, *args): pass
        def topics(self): return ["RuleEngine/CellMotionDetector/Motion"]
        def profiles(self): return []
        def subscribe(self): pass
        def close(self): pass
        def pull(self):
            self.batch += 1
            if self.batch > 2:
                receiver.stop.set()
                return []
            return [camera_signal(index / 10) for index in range((self.batch - 1) * 64, self.batch * 64)]
    monkeypatch.setattr(worker, "DeviceClient", Device)
    receiver.run()
    with factory() as db:
        assert history.event_query(db).count() == 1
        state = db.get(Camera, 1).recording_signal_state
        assert state["last_activity_at"] == domain.iso(NOW + timedelta(seconds=12.7))
        assert state["source_confirmed"] is False
    assert worker.receiver_states()["1"]["dropped"] == 8


def test_initialized_stale_replayed_unknown_and_ambiguous_signals_cannot_extend(env):
    client, factory = env
    prepare(client)
    with factory() as db:
        call = lambda value, resolved=GROUP["cameras"][0]: domain.observe_camera_signal(db, GROUP, resolved, value, received=NOW)
        assert not call(dataclasses.replace(camera_signal(), initialized=True))
        assert not call(camera_signal(-61))
        assert not call(camera_signal(11))
        assert not call(dataclasses.replace(camera_signal(), kind="other"))
        assert not call(camera_signal(), None)
        assert call(camera_signal())
        assert not call(camera_signal())
        assert not call(camera_signal(-1))
        db.commit()


def test_end_keeps_deadline_other_objects_and_camera_updated_timestamp(env):
    client, factory = env
    prepare(client)
    with factory() as db:
        camera = db.get(Camera, 1); updated = camera.updated_at
        for value, second in ((camera_signal(), 0), (dataclasses.replace(camera_signal(1), key={"ObjectId": "second"}), 1),
                              (dataclasses.replace(camera_signal(2), active=False), 2)):
            assert domain.observe_camera_signal(db, GROUP, GROUP["cameras"][0], value, received=NOW + timedelta(seconds=second))
        db.commit(); db.refresh(camera)
        assert camera.updated_at == updated
        state = camera.recording_signal_state
        assert state["last_activity_at"] == domain.iso(NOW + timedelta(seconds=1))
        assert {value["active"] for value in state["observations"].values()} == {True, False}
        assert not camera.enabled  # Observation is independent of manual pause.


def test_same_second_end_and_reconnect_do_not_leave_confirmed_stale_state(env):
    client, factory = env
    prepare(client)
    with factory() as db:
        value = camera_signal()
        assert domain.observe_camera_signal(db, GROUP, GROUP["cameras"][0], value, received=NOW)
        assert domain.observe_camera_signal(db, GROUP, GROUP["cameras"][0], dataclasses.replace(value, active=False), received=NOW)
        history.source_disconnected(db, GROUP)
        assert db.get(Camera, 1).recording_signal_state["source_confirmed"] is False
        next_group = {**GROUP, "recording_generation": "next-subscription"}
        assert domain.observe_camera_signal(db, next_group, GROUP["cameras"][0], camera_signal(1), received=NOW + timedelta(seconds=1))
        assert len(db.get(Camera, 1).recording_signal_state["observations"]) == 1
        assert db.get(Camera, 1).recording_signal_state["source_confirmed"] is True


def test_unclassified_or_delayed_end_closes_only_its_own_known_observation(env):
    client, factory = env
    prepare(client)
    with factory() as db:
        first = dataclasses.replace(camera_signal(), kind="person", key={"ObjectId": "first"})
        second = dataclasses.replace(first, key={"ObjectId": "second"})
        for value in (first, second):
            assert domain.observe_camera_signal(db, GROUP, GROUP["cameras"][0], value, received=NOW)
        ending = dataclasses.replace(first, active=False, kind="other", occurred_at=NOW + timedelta(seconds=1))
        later = NOW + timedelta(minutes=3)
        assert domain.observe_camera_signal(db, GROUP, GROUP["cameras"][0], ending, received=later)
        state = db.get(Camera, 1).recording_signal_state
        assert {value["active"] for value in state["observations"].values()} == {True, False}
        assert state["last_activity_at"] == domain.iso(NOW)
        assert not domain.observe_camera_signal(db, GROUP, GROUP["cameras"][0],
            dataclasses.replace(ending, key={"ObjectId": "unknown"}), received=later)
        assert not domain.observe_camera_signal(db, {**GROUP, "recording_generation": "next-subscription"},
            GROUP["cameras"][0], ending, received=later)


def test_state_is_bounded_and_policy_change_invalidates_old_activity(env):
    client, factory = env
    prepare(client)
    with factory() as db:
        for index in range(130):
            value = dataclasses.replace(camera_signal(), key={"ObjectId": str(index)})
            assert domain.observe_camera_signal(db, GROUP, GROUP["cameras"][0], value, received=NOW)
        db.commit()
        state = db.get(Camera, 1).recording_signal_state
        assert len(state["observations"]) == 64 and state["capacity_limited"]
        assert len(json.dumps(state).encode()) < domain.MAX_STATE_BYTES
    assert prepare(client, policy=event_policy("ai"), revision=1).status_code == 200
    with factory() as db:
        assert db.get(Camera, 1).recording_signal_state is None


def test_same_internal_signal_contract_accepts_ai_not_camera_source(env):
    client, factory = env
    prepare(client, policy=event_policy("ai", rule_ids=["yard-line"]))
    value = RecordingSignal(source="ai", source_generation="model-v1.rules-1", identity="a" * 64,
        kind="person", rule_id="yard-line", active=True, occurred_at=NOW)
    with factory() as db:
        assert domain.observe_recording_signal(db, 1, value, received=NOW)
        assert not domain.observe_recording_signal(db, 1, value.model_copy(update={"source": "camera"}), received=NOW)
        assert not domain.observe_recording_signal(db, 1, value.model_copy(update={"rule_id": "other"}), received=NOW)
        db.commit()


def test_detection_evidence_is_bounded_secret_free_and_references_real_camera_recording(env):
    _, factory = env
    fields = dict(model_id="yolox-tiny", model_version="v1", rule_id="yard", rule_version=1,
                  occurred_at=NOW.replace(tzinfo=timezone.utc), objects=[{"kind": "person", "confidence": .9, "box": [0, 0, 1, 1]}])
    evidence = DetectionEvidence(**fields)
    with pytest.raises(ValidationError): DetectionEvidence(**fields, rtsp_url="rtsp://fixture")
    with pytest.raises(ValidationError): DetectionEvidence(**{**fields, "objects": fields["objects"] * 33})
    with pytest.raises(ValidationError): DetectionEvidence(**{**fields, "occurred_at": NOW})
    with factory() as db:
        assert domain.detection_metadata(db, 1, evidence)["model_id"] == "yolox-tiny"
        with pytest.raises(ValueError): domain.detection_metadata(db, 1, evidence.model_copy(update={"recording_id": 99}))
        segment = RecordingSegment(camera_id=1, file_path="/not-mounted/fixture.mkv", started_at=NOW - timedelta(seconds=1),
            ended_at=NOW + timedelta(seconds=1), finalized_at=NOW, status="finalized")
        db.add(segment); db.flush()
        assert domain.detection_metadata(db, 1, evidence.model_copy(update={"recording_id": segment.id}))["recording_id"] == segment.id
        with pytest.raises(ValueError): domain.detection_metadata(db, 2, evidence.model_copy(update={"recording_id": segment.id}))
