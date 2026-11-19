from dataclasses import replace
from datetime import timedelta

import pytest

from app.models import AuditEvent, Camera
from app.services import camera_alert_episodes as admission, camera_events as history
from test_ai_recording_foundation import NOW, GROUP, env, camera_signal


def save(client, mode, *, camera=1, revision=0, minutes=2, kinds=None):
    policy = {"mode": mode, "source": "camera", "event_kinds": kinds or ["motion", "vehicle", "person"]}
    data = {"expected_revision":revision, "policy":policy}
    if mode == "events":
        policy.update(profile="full_quality", prebuffer_seconds=30, postrecord_minutes=minutes)
    else:
        data["alert_quiet_minutes"] = minutes
    return client.put(f"/ai-recording/cameras/{camera}", json=data)


@pytest.mark.parametrize("mode", ["continuous", "events"])
def test_any_eligible_kind_slides_only_one_episode_per_lens(env, mode, monkeypatch):
    client, factory = env
    assert save(client, mode).status_code == 200
    assert save(client, mode, camera=2).status_code == 200
    clock = [NOW]
    monkeypatch.setattr(history, "now_utc", lambda: clock[0])
    left, right = GROUP["cameras"][0], {"id":2,"name":"Second"}
    group = {**GROUP, "cameras":[left, right]}

    def event(at, kind="motion", *, lens=left, **changes):
        clock[0] = NOW + timedelta(seconds=at)
        with factory() as db:   # Each pulse simulates a fresh receiver session.
            return history.record_signal(db, group, lens, replace(camera_signal(at), kind=kind, **changes))

    first = event(0)
    assert first
    assert event(60,"vehicle") is None
    assert event(70,"vehicle",active=False) is None
    assert event(165,"animal") is None  # Not selected; cannot reset the quiet interval.
    assert event(166,"person",lens=right)
    assert event(179,"person") is None
    with factory() as db:
        assert history.event_query(db).count() == 2
        item = history.serialize(db.get(AuditEvent, first))
        assert item["kind"] == "motion" and item["active"] is True
        if mode == "events":
            assert db.get(Camera, 1).recording_signal_state["last_activity_at"] == admission.iso(admission.epoch(clock[0]))
        else:
            assert not (db.get(Camera, 1).recording_signal_state or {}).get("last_activity_at")
    assert event(299,"vehicle")  # Exactly 120 seconds after the last eligible pulse.
    with factory() as db:
        item=history.serialize(db.get(AuditEvent, first))
        assert not item["active"] and item["ended_at"] == admission.iso(admission.epoch(clock[0]))
        assert history.event_query(db).count() == 3


def test_deleting_or_muting_one_episode_cannot_rearm_it(env, monkeypatch):
    client, factory = env
    assert save(client,"continuous",minutes=1).status_code==200
    clock=[NOW]
    monkeypatch.setattr(history,"now_utc",lambda:clock[0])
    with factory() as db:
        history.save_preferences(db,1,{"notifications":False})
        first=history.record_signal(db,GROUP,GROUP["cameras"][0],camera_signal())
        assert first and history.marked_read_ids(db,1,[first])=={first}
        history.delete_camera_events(db,ids=[first])
        history.save_preferences(db,1,{"notifications":True})
    with factory() as db:
        clock[0]+=timedelta(seconds=10)
        assert history.record_signal(db,GROUP,GROUP["cameras"][0],camera_signal(10)) is None
        assert history.event_query(db).count()==0
    with factory() as db:
        clock[0]+=timedelta(seconds=60)
        identifier=history.record_signal(db,GROUP,GROUP["cameras"][0],camera_signal(70))
        assert identifier and history.list_camera_events(db,1)["unread_count"]==1


def test_optional_interval_is_preserved_when_older_clients_edit_the_policy(env):
    client, _ = env
    assert save(client,"continuous",minutes=4).json()["alert_quiet_minutes"] == 4
    response=client.put("/ai-recording/cameras/1",json={"expected_revision":1,"policy":{
        "mode":"continuous","source":"camera","postrecord_minutes":1,"event_kinds":["person"]
    }})
    assert response.status_code==200 and response.json()["alert_quiet_minutes"]==4
    assert save(client,"events",revision=2,minutes=2).json()["policy"]["postrecord_minutes"]==2


def test_ai_progress_rearms_only_after_its_last_positive_even_if_history_deleted(env):
    _, factory = env
    start = admission.epoch(NOW)
    with factory() as db:
        camera=db.get(Camera,1)
        assert admission.admit(db,camera,source="ai",at=start,minutes=2,identifier="start").opening
        db.commit()
    with factory() as db:
        camera=db.get(Camera,1)
        assert not admission.admit(db,camera,source="ai",at=start+119,minutes=2).opening
        db.commit()
    with factory() as db:
        camera=db.get(Camera,1)
        assert not admission.admit(db,camera,source="ai",at=start+235,minutes=2,identifier="new-type").opening
        assert admission.admit(db,camera,source="ai",at=start+234,minutes=2,identifier="late") is None
        assert admission.admit(db,camera,source="ai",at=start+355,minutes=2) is None
        assert admission.admit(db,camera,source="ai",at=start+355,minutes=2,identifier="second").opening
