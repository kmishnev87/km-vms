from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

from app.models.camera import Camera
from app.services import camera_events_worker as receiver, camera_source_health as health

NOW = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)


def camera(cid, **values):
    return Camera(id=cid, **{"name": f"Camera {cid}", "enabled": True, "protocol": "onvif",
                            "host": "192.0.2.1", "port": 80, "username": "test", "onvif_path": "/onvif/device_service",
                            "events_enabled": True, "status": "ready", **values})


def proof(*, ai_phase=None, request=None, job=None, linked_id=None, source_status=None, failed_at=None):
    ai = {"phase": ai_phase, "model": "not-for-public-output"}
    if failed_at:
        ai.update(retry_token="private-retry-token", incident={"last_failure_at": failed_at.isoformat()})
    return {"heartbeat_current": True, "confirmed_recording": bool(job), "job_id": job,
            "linked_policy_configured": linked_id is not None,
            "event_control": {"instance": "test-recorder", "request": request, "job_id": job, "ai": ai,
                              "source_status": source_status,
                              "link": {"origin_camera_id": linked_id, "source_event_status": source_status}}}


def test_source_health_separates_ai_from_camera_subscription_and_keeps_writer_truth(monkeypatch):
    scene = camera(1, recording_policy={"schema_version": 1, "revision": 1, "mode": "continuous", "profile": "none", "source": "ai"})
    source = camera(2)
    inactive = camera(3, enabled=False, events_enabled=False, recording_policy=scene.recording_policy)
    linked = camera(4)
    dependent = camera(5)
    monkeypatch.setattr(health, "receiver_fault_snapshots", lambda cameras: {
        2: {"group": "one-device", "phase": "exhausted", "retry_available": True},
        4: {"group": "one-device", "phase": "exhausted", "retry_available": True},
    })
    issues = health.build_source_issues([scene, source, inactive, linked, dependent], {
        1: proof(ai_phase="exhausted", request="continuous", job="writer-ai", failed_at=NOW - timedelta(minutes=2)),
        2: proof(request="emergency", job="writer-events"),
        3: proof(ai_phase="exhausted", failed_at=NOW - timedelta(minutes=5)),
        4: proof(),
        5: proof(linked_id=2, source_status="exhausted"),
    }, NOW)
    ai, events = issues
    assert (ai["kind"], ai["retry_available"]) == ("ai", True)
    assert ai["cameras"] == [{"camera_id": 1, "name": "Camera 1", "recording_state": "recording"}]
    assert (events["kind"], events["retry_available"], events["camera_id"]) == ("camera", True, 2)
    assert [(c["camera_id"], c["recording_state"]) for c in events["cameras"]] == [(2, "emergency"), (4, "stopped"), (5, "stopped")]
    assert "not-for-public-output" not in str(issues) and "private-retry-token" not in str(issues)


def test_silence_manual_pause_and_intentional_event_opt_out_are_not_incidents(monkeypatch):
    monkeypatch.setattr(health, "receiver_fault_snapshots", lambda cameras: {})
    camera_policy = {"schema_version": 1, "revision": 1, "mode": "events", "source": "camera", "profile": "economical", "postrecord_minutes": 1}
    cams = [camera(1, enabled=False, recording_policy=camera_policy), camera(2, events_enabled=False, recording_policy=camera_policy),
            camera(3, recording_policy=camera_policy)]
    assert health.build_source_issues(cams, {1: proof(source_status="failed"), 2: proof(source_status="inactive"),
                                            3: proof(source_status="healthy")}, NOW) == []
    issues = health.build_source_issues(cams, {3: proof(source_status="stalled")}, NOW, storage_suspended=True)
    assert len(issues) == 1 and issues[0]["cameras"][0]["recording_state"] == "storage_suspended"


def test_shared_ai_process_is_one_incident_but_lost_camera_frames_are_separate(monkeypatch):
    monkeypatch.setattr(health, "receiver_fault_snapshots", lambda cameras: {})
    policy = {"schema_version": 1, "revision": 1, "mode": "continuous", "profile": "none", "source": "ai"}
    cams = [camera(i, recording_policy=policy) for i in (1, 2, 3)] + [camera(4)]
    reports = {1: proof(ai_phase="failed", failed_at=NOW - timedelta(seconds=1), job="ongoing"),
               2: proof(ai_phase="exhausted", failed_at=NOW - timedelta(seconds=45)),
               3: proof(ai_phase="stalled", failed_at=NOW - timedelta(seconds=45)),
               4: proof(linked_id=2, source_status="exhausted", job="emergency-linked", request="emergency")}
    shared, local = health.build_source_issues(cams, reports, NOW)
    assert shared["camera_id"] == 2 and shared["retry_available"]
    assert [(row["camera_id"], row["recording_state"]) for row in shared["cameras"]] == [
        (1, "recording"), (2, "stopped"), (4, "emergency")]
    assert [row["camera_id"] for row in local["cameras"]] == [3]
    assert not local["retry_available"]


def test_receiver_health_reads_current_worker_only_and_does_not_probe(monkeypatch):
    source = camera(1, enabled=False)
    job = receiver.Receiver({"cameras": [{"id": source.id, "revision": receiver.camera_receiving_revision(source), "events_enabled": True}]})
    job.recovery.incident_id = "test-incident"
    job.manual_ready_at = 80
    job.thread = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(receiver, "_stop", Event())
    monkeypatch.setattr(receiver, "_jobs", {receiver.connection_id(source): job})
    monkeypatch.setattr(receiver, "_states", {"1": {"status": "retry_exhausted", "incident": {"id": "test-incident"}}})
    monkeypatch.setattr(receiver.time, "monotonic", lambda: 100)
    assert receiver.receiver_fault_snapshots([source])[1]["retry_available"] is True
    job.retry_requested.set()
    assert receiver.receiver_fault_snapshots([source])[1]["retry_available"] is False
    receiver._states["1"]["status"] = "waiting"
    assert receiver.receiver_fault_snapshots([source]) == {}
    receiver._states["1"]["status"] = "reconnecting"
    source.onvif_profile_token = "other-profile"
    assert receiver.receiver_fault_snapshots([source]) == {}


def test_multiple_confirmed_offline_cameras_are_grouped_but_unknown_evidence_is_not(monkeypatch):
    failed_at = (NOW - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(health, "receiver_fault_snapshots", lambda cameras: {
        1: {"group": "one", "phase": "failed", "reachability": "network_failure", "failed_at": failed_at, "retry_available": True},
        2: {"group": "two", "phase": "failed", "reachability": "network_failure", "failed_at": failed_at},
        3: {"group": "three", "phase": "failed", "reachability": "network_failure", "failed_at": failed_at},
        4: {"group": "four", "phase": "failed", "reachability": "reachable", "failed_at": failed_at},
        5: {"group": "five", "phase": "failed", "reachability": "network_failure",
            "failed_at": (NOW - timedelta(minutes=20)).isoformat()},
    })
    fresh_failure = {"heartbeat_current": True, "recorder_instance_id": "current", "current_recorder_instance_id": "current",
                     "recording_health": "retrying", "job_updated_at": failed_at, "last_error": "Connection refused"}
    issues = health.build_source_issues([camera(i, enabled=i != 3) for i in range(1, 6)], {2: fresh_failure},
        NOW, live_evidence={1: {"network_failure"}, 5: {"network_failure"}})
    offline, paused, answering, stale = issues
    assert offline["cause"] == "unreachable" and not offline["retry_available"]
    assert [item["camera_id"] for item in offline["cameras"]] == [1, 2]
    assert paused["cause"] == "no_response" and paused["cameras"][0]["recording_state"] == "paused"
    assert answering["cause"] == "subscription" and answering["camera_id"] == 4
    assert stale["cause"] == "no_response"


def test_typed_control_errors_do_not_confuse_camera_response_with_network_loss():
    import requests
    from zeep.exceptions import Fault
    assert receiver.event_link_evidence(requests.exceptions.ConnectionError()) == "network_failure"
    assert receiver.event_link_evidence(requests.exceptions.ReadTimeout()) == "network_failure"
    assert receiver.event_link_evidence(Fault("unsupported operation")) == "reachable"
    assert receiver.event_link_evidence(OSError("could be local disk")) is None
