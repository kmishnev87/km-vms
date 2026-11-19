"""Personal attention is decided on arrival; saving never clears old unread."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import uuid
from unittest.mock import patch

import pytest

from app.models import User, Camera, AuditEvent
from app.services import camera_events as events, mobile_push as push
from app.services.onvif_events import Signal
from test_camera_events_api_stage172 import api
from test_mobile_push import fixture, register


def accept(db, *, camera_id=1, kind="motion", at=None, received=None):
    received = received or events.now_utc()
    instant = at or received
    camera = {"id": camera_id, "name": f"Camera {camera_id}"}
    group = {"device_id": "test-device", "cameras": [camera]}
    with patch.object(events, "now_utc", lambda: received):
        return events.record_signal(db, group, camera, Signal("Motion/" + uuid.uuid4().hex, kind,
            True, False, instant, None, False, {}))


def test_ten_old_unread_survive_disable_new_muted_events_stay_read_after_enable(api):
    client, factory, first, _ = api
    now = events.now_utc()
    with factory() as db:
        for minutes in range(9, 0, -1):
            assert accept(db, received=now-timedelta(minutes=minutes))
    baseline = client.get("/camera-events").json()
    assert baseline["unread_count"] == baseline["total"] == 10
    cursor = baseline["read_cursor"]
    assert client.put("/camera-events/preferences", json={"notifications": False}).status_code == 200
    assert client.get("/camera-events").json()["unread_count"] == 10
    with factory() as db:
        muted = accept(db, received=now)
    assert client.get("/camera-events").json()["unread_count"] == 10
    assert client.put("/camera-events/preferences", json={"notifications": True}).status_code == 200
    values = client.get("/camera-events").json()
    assert values["total"] == 11 and values["unread_count"] == 10
    assert {v["id"] for v in values["items"] if v["read"]} == {muted}
    updates = client.get("/camera-events/updates", params={"wait": 0, "after": cursor}).json()
    assert updates["items"][0]["id"] == muted and updates["items"][0]["read"] and not updates["items"][0]["notify"]
    with factory() as db:
        allowed = accept(db, received=now+timedelta(minutes=1))
    unread = client.get("/camera-events?unread=true").json()
    assert unread["unread_count"] == 11 and allowed in {v["id"] for v in unread["items"]} and muted not in {v["id"] for v in unread["items"]}


@pytest.mark.parametrize("choice", [{"camera_ids": []}, {"notifications": False}])
def test_auto_read_is_per_user_and_does_not_touch_shared_history_or_recorder(api, choice):
    client, factory, first, _ = api
    with factory() as db:
        db.add(User(id=2, username="attention-other", password_hash="test-only", role="viewer", is_active=True)); db.commit()
    assert client.put("/camera-events/preferences", json=choice).status_code == 200
    with factory() as db:
        new = accept(db)
        own = events.list_camera_events(db, 1)
        other = events.list_camera_events(db, 2)
        assert own["total"] == other["total"] == 2
        assert own["unread_count"] == 1 and other["unread_count"] == 2
        assert events.automatic_read_ids(db, 1, [first, new]) == {new}
        assert db.get(Camera, 1).enabled and not db.get(Camera, 1).events_enabled
    assert client.put("/camera-events/preferences", json={"notifications": True, "camera_ids": None, "kinds": None}).status_code == 200
    assert client.get("/camera-events").json()["unread_count"] == 1


def test_per_camera_schedules_use_nas_arrival_time_and_support_midnight(api, monkeypatch):
    client, factory, first, _ = api
    policy = {"hours_enabled": True, "schedule_mode": "per_camera", "start_minute": 0, "end_minute": 0,
              "camera_schedules": {"1": {"start_minute": 19*60, "end_minute": 7*60},
                                   "2": {"start_minute": 0, "end_minute": 6*60}}}
    assert client.put("/camera-events/preferences", json=policy).json()["camera_schedules"] == policy["camera_schedules"]
    monkeypatch.setattr(events, "timezone_context", lambda _: SimpleNamespace(zone=ZoneInfo("Asia/Yekaterinburg"), name="Asia/Yekaterinburg"))
    # 20:00 NAS / 15:00 UTC: outdoor camera alerts, indoor camera is quiet.
    arrival = datetime(2026, 11, 16, 15)
    monkeypatch.setattr(events, "now_utc", lambda: arrival)
    with factory() as db:
        for cid in (2, 3):
            db.add(Camera(id=cid, name=f"Lens {cid}", storage_folder_name=f"lens{cid}",
                          host="192.0.2.2", port=80, protocol="onvif", events_enabled=True))
        db.commit()
        outdoor = accept(db, camera_id=1, at=arrival-timedelta(hours=5))
        indoor = accept(db, camera_id=2)
        fallback = accept(db, camera_id=3)
        assert events.automatic_read_ids(db, 1, [outdoor, indoor, fallback]) == {indoor}
    # Next NAS day 01:00: both ranges allow alerts. Older muted stays read.
    monkeypatch.setattr(events, "now_utc", lambda: arrival+timedelta(hours=5))
    with factory() as db:
        later = accept(db, camera_id=2)
        assert events.automatic_read_ids(db, 1, [indoor, later]) == {indoor}


@pytest.mark.parametrize("minute,allowed", [(18*60+59, False), (19*60, True), (23*60+59, True), (0, True), (7*60-1, True), (7*60, False)])
def test_schedule_exact_boundaries(minute, allowed):
    policy = {"hours_enabled": True, "start_minute": 19*60, "end_minute": 7*60}
    instant = datetime(2026, 11, 16, minute//60, minute%60, tzinfo=timezone.utc)
    assert events.notifications_allowed(policy, {"camera_id": 1}, SimpleNamespace(zone=timezone.utc), at=instant) is allowed


@pytest.mark.parametrize("bad", [{"schedule_mode": "other"}, {"camera_schedules": {"0": {"start_minute": 0, "end_minute": 1}}},
    {"camera_schedules": {"01": {"start_minute": 0, "end_minute": 1}}}, {"camera_schedules": {"1": {"start_minute": -1, "end_minute": 1}}},
    {"camera_schedules": {"1": {"start_minute": 0, "end_minute": 1440}}}, {"camera_schedules": {"1": {"start_minute": True, "end_minute": 1}}},
    {"camera_schedules": {"1": {"start_minute": 0, "end_minute": 1, "enabled": False}}}])
def test_schedule_input_is_strict_and_never_changes_preferences_on_error(api, bad):
    client, _, _, _ = api
    before = client.get("/camera-events/preferences").json()
    assert client.put("/camera-events/preferences", json=bad).status_code == 422
    assert client.get("/camera-events/preferences").json() == before


def test_older_apk_save_preserves_individual_schedule(api):
    client, _, _, _ = api
    custom = {"schedule_mode": "per_camera", "camera_schedules": {"1": {"start_minute": 60, "end_minute": 120}}}
    assert client.put("/camera-events/preferences", json=custom).status_code == 200
    result = client.put("/camera-events/preferences", json={"notifications": False}).json()
    assert result["schedule_mode"] == custom["schedule_mode"] and result["camera_schedules"] == custom["camera_schedules"]


def test_offline_phone_does_not_replay_events_muted_at_arrival(fixture):
    _, factory, _, _ = fixture
    identifier = register(fixture)
    now = events.now_utc()
    with factory() as db:
        events.save_preferences(db, 1, {"notifications": False})
        muted = accept(db, received=now)
        events.save_preferences(db, 1, {"notifications": True})
    sent = []
    assert push.deliver_one(factory, identifier, "attention-worker", sender=lambda *_: sent.append(True))
    assert not sent
    with factory() as db:
        allowed = accept(db, received=now+timedelta(minutes=1))
    assert push.deliver_one(factory, identifier, "attention-worker", sender=lambda *_: sent.append(True))
    assert len(sent) == 1
    with factory() as db:
        assert events.list_camera_events(db, 1)["unread_count"] == 1
        assert events.list_camera_events(db, 2)["unread_count"] == 2


def test_disabling_only_one_phone_does_not_auto_read_the_users_events(fixture):
    _, factory, _, _ = fixture
    register(fixture, enabled=False)
    with factory() as db:
        current = accept(db)
        assert events.list_camera_events(db, 1)["unread_count"] == 1
        assert not events.automatic_read_ids(db, 1, [current])


def test_phone_event_details_use_the_same_personal_read_state_as_history(fixture):
    client, factory, _, _ = fixture
    now = events.now_utc()
    with factory() as db:
        events.save_preferences(db, 1, {"notifications": False})
        muted = accept(db, received=now)
        events.save_preferences(db, 1, {"notifications": True})
        unread = accept(db, received=now+timedelta(minutes=1))
    assert client.get(f"/mobile/push/events/{muted}").json()["event"]["read"] is True
    assert client.get(f"/mobile/push/events/{unread}").json()["event"]["read"] is False
    with factory() as db:
        events.mark_read(db, 1, [unread])
    assert client.get(f"/mobile/push/events/{unread}").json()["event"]["read"] is True


def test_legacy_personal_type_settings_do_not_mark_valid_notifications_read(api):
    client, factory, _, _ = api
    assert client.put("/camera-events/preferences", json={"kinds": ["person"]}).status_code == 200
    with factory() as db:
        camera = {"id": 1, "name": "Front"}
        group = {"device_id": "classified", "cameras": [camera]}
        rule = Signal("RuleEngine/FieldDetector/ObjectsInside", "intrusion", True, False, events.now_utc(), None, False, {}, rule_kind="intrusion")
        muted = events.record_signal(db, group, camera, rule)
        with patch.object(events, "now_utc", lambda: rule.occurred_at+timedelta(minutes=1, seconds=1)):
            person = events.record_signal(db, group, camera, replace(rule, object_kind="person", object_kinds=("person",)))
        assert person is not None and person != muted
        assert events.automatic_read_ids(db, 1, [muted, person]) == set()
        assert events.event_notification_allowed(db, 1, events.preferences(db, 1)[0], events.serialize(db.get(AuditEvent, person)), events.timezone_context(db))
