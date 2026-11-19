"""Source replay and old-client safety under the shared per-camera episode."""
from dataclasses import replace
from datetime import datetime, timedelta

from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.services import camera_events as events
from test_camera_events_stage172 import db, group
from test_camera_events_lifecycle_stage16 import signal


def clock(monkeypatch):
    start = datetime(2026, 11, 14, 12)
    current = [start]
    monkeypatch.setattr(events, "now_utc", lambda: current[0])
    return lambda seconds: current.__setitem__(0, start + timedelta(seconds=seconds))


def record(db, value, *, generation=1, **kwargs):
    device = {**group(), "recording_generation": str(generation)}
    return events.record_signal(db, device, device["cameras"][0], value, **kwargs)


def test_exact_transport_replay_does_not_extend_on_reconnect(db, monkeypatch):
    at = clock(monkeypatch)
    value = signal()
    identifier = record(db, value)
    assert identifier
    at(20)
    assert record(db, value, generation=2) is None
    at(60)
    assert record(db, signal()) not in {None, identifier}
    assert events.event_query(db).count() == 2


def test_lens_with_unknown_source_has_independent_device_scope(db, monkeypatch):
    at = clock(monkeypatch)
    value = signal()
    assert record(db, value)
    at(5)
    device_id = events.record_signal(db, group(), None, value)
    assert device_id and events.serialize(db.get(AuditEvent, device_id))["source_ambiguous"]
    assert events.event_query(db).count() == 2


def test_unregistered_or_deleted_camera_does_not_open_an_episode(db, monkeypatch):
    clock(monkeypatch)
    value = signal()
    assert events.record_signal(db, group(), {"id": 99, "name": "Removed"}, value) is None
    camera = db.get(Camera, 1)
    camera.deleted_at = events.now_utc()
    db.commit()
    assert record(db, value) is None
    assert events.event_query(db).count() == 0


def test_suppressed_starts_do_not_add_unread_or_occurrences(db, monkeypatch):
    at = clock(monkeypatch)
    first = record(db, signal())
    at(10)
    diagnostics = {}
    assert record(db, replace(signal(), object_kind="vehicle"), diagnostics=diagnostics) is None
    assert diagnostics == {"messages_suppressed": 1}
    db.expire_all()
    at(65)
    assert record(db, signal(key="new rule")) is None
    assert events.event_query(db).count() == 1
    assert events.list_camera_events(db, 1)["unread_count"] == 1
    assert db.get(AuditEvent, first).event_metadata["occurrences"] == 1
