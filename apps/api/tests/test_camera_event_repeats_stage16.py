from dataclasses import replace
from datetime import datetime, timedelta

from app.models.audit_event import AuditEvent
from app.services import camera_events as events
from test_camera_events_stage172 import db, group
from test_camera_events_lifecycle_stage16 import signal


def clock(monkeypatch):
    current = [datetime(2026, 11, 14, 12)]
    monkeypatch.setattr(events, "now_utc", lambda: current[0])
    return lambda seconds: current.__setitem__(0, datetime(2026, 11, 14, 12) + timedelta(seconds=seconds))


def record(db, value, **kwargs):
    return events.record_signal(db, group(), group()["cameras"][0], value, **kwargs)


def pulse(db, value=None):
    value = value or signal()
    identifier = record(db, value)
    record(db, replace(value, active=False))
    return identifier


def test_window_slides_from_suppressed_starts_not_the_saved_start(db, monkeypatch):
    at = clock(monkeypatch)
    first = pulse(db)
    for second in [20, 50, 94]:
        at(second)
        assert pulse(db) is None
    assert events.event_query(db).count() == 1
    at(139)  # Exactly 45s since the last received start is a NEW event.
    assert pulse(db) not in {None, first}
    assert events.event_query(db).count() == 2


def test_vehicle_and_motion_have_independent_clocks(db, monkeypatch):
    at = clock(monkeypatch)
    motion = signal()
    vehicle = replace(signal(key="car"), object_kind="vehicle", object_kinds=("vehicle",))
    assert pulse(db, motion)
    at(10)
    assert pulse(db, vehicle)
    at(20)
    assert pulse(db, motion) is None
    at(35)
    assert pulse(db, vehicle) is None
    at(65)
    assert pulse(db, motion)
    assert events.event_query(db).count() == 3


def test_different_cameras_lenses_rules_and_types_are_not_suppressed(db, monkeypatch):
    clock(monkeypatch)
    value = signal()
    assert pulse(db, value)
    assert pulse(db, replace(value, source={"VideoSourceConfigurationToken": "lens1", "Rule": "other-zone"}))
    assert pulse(db, replace(value, topic="RuleEngine/LineDetector/Crossed", kind="line_crossing", rule_kind="line_crossing"))
    camera = {"id": 2, "name": "Second lens"}
    assert events.record_signal(db, group(), camera, value)
    assert events.event_query(db).count() == 4


def test_object_id_is_not_a_new_alert_type_and_cannot_end_the_retained_object(db, monkeypatch):
    at = clock(monkeypatch)
    first = record(db, signal())
    at(10)
    assert record(db, signal(key="second")) is None
    record(db, signal("Deleted", key="second"))
    assert db.get(AuditEvent, first).event_metadata["active"] is True
    record(db, signal("Deleted"))
    assert db.get(AuditEvent, first).event_metadata["active"] is False
    assert events.event_query(db).count() == 1


def test_end_does_not_extend_the_repeat_window_or_invent_an_end(db, monkeypatch):
    at = clock(monkeypatch)
    first = record(db, signal())
    at(40)
    record(db, signal("Deleted"))
    assert db.get(AuditEvent, first).event_metadata["active"] is False
    at(45)
    assert record(db, signal())


def test_new_classification_is_retained_and_one_property_end_closes_both(db, monkeypatch):
    at = clock(monkeypatch)
    first = record(db, signal())
    at(10)
    second = record(db, replace(signal(), object_kind="vehicle", object_kinds=("vehicle",)))
    assert second and second != first
    at(20)
    assert record(db, signal()) is None
    record(db, signal("Deleted"))
    assert all(row.event_metadata["active"] is False for row in events.event_query(db).all())
    assert events.event_query(db).count() == 2


def test_suppressed_starts_do_not_add_unread_or_occurrences_and_survive_new_session(db, monkeypatch):
    at = clock(monkeypatch)
    first = pulse(db)
    at(10)
    diagnostics = {}
    assert record(db, signal(), diagnostics=diagnostics) is None
    assert diagnostics == {"messages_suppressed": 1}
    db.expire_all()  # State comes from the DB, not a volatile receiver clock.
    at(50)
    assert pulse(db) is None
    assert events.event_query(db).count() == 1
    assert db.get(AuditEvent, first).event_metadata["occurrences"] == 1
