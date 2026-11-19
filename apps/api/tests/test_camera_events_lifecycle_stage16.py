"""ONVIF property parsing and safe closure of pre-upgrade property history."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import uuid

from lxml import etree

from app.models.audit_event import AuditEvent
from app.services import camera_events as events
from app.services import onvif_events as onvif
from test_camera_events_stage172 import db, group, notification, register_fixture_cameras


def signal(operation="Changed", key="first", active="true", topic="RuleEngine/FieldDetector/ObjectsInside"):
    message = notification(topic=topic, active=active)
    root = message["Message"]["_value_1"]
    root.set("PropertyOperation", operation)
    data = root.find("{*}Data")
    for item in data:
        item.set("Name", "IsInside")
    keys = etree.SubElement(root, "{http://www.onvif.org/ver10/schema}Key")
    etree.SubElement(keys, "{http://www.onvif.org/ver10/schema}SimpleItem", Name="ObjectId", Value=key)
    if operation == "Deleted":
        root.remove(data)
    return onvif.parse_notification(message)


def record(db, value):
    return events.record_signal(db, group(), group()["cameras"][0], value)


def legacy_open(db, value):
    identity = [group()["device_id"], value.topic, value.source]
    if value.key:
        identity.append(value.key)
    identifier = str(uuid.uuid4())
    db.add(AuditEvent(id=identifier, category=events.CATEGORY, event_type="camera." + value.kind, severity="info",
        message_ru="", message_en="", target_type="camera", target_id="1", target_name="Front",
        event_metadata={"episode": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(), "active": True}))
    db.commit()
    return identifier


def test_deleted_without_data_ends_the_matching_object_only(db):
    first = legacy_open(db, signal())
    second = legacy_open(db, replace(signal(key="second"), object_kind="vehicle", object_kinds=("vehicle",)))
    assert first != second
    ending = signal("Deleted")
    assert ending.active is False and ending.key == {"ObjectId": "first"}
    assert record(db, ending) is None
    assert db.get(AuditEvent, first).event_metadata["active"] is False
    assert db.get(AuditEvent, first).event_metadata["ended_at"]
    assert db.get(AuditEvent, second).event_metadata["active"] is True


def test_end_after_disconnect_reconciles_the_old_episode(db):
    identifier = legacy_open(db, signal())
    events.source_disconnected(db, group())
    record(db, signal(active="false"))
    value = events.serialize(db.get(AuditEvent, identifier))
    assert value["active"] is False and not value["state_unconfirmed"]
    assert value["ended_at"] and events.event_query(db).count() == 1


def test_initialization_never_extends_or_alerts_after_reconnect(db):
    identifier = record(db, signal())
    deadline = db.get(AuditEvent, identifier).event_metadata["alert_episode_until"]
    events.source_disconnected(db, group())
    assert record(db, signal("Initialized")) is None
    value = events.serialize(db.get(AuditEvent, identifier))
    assert value["active"] is True and not value["state_unconfirmed"]
    assert value["occurrences"] == 1 and events.event_query(db).count() == 1
    record(db, signal("Initialized", active="false"))
    assert db.get(AuditEvent, identifier).event_metadata["alert_episode_until"] == deadline


def test_property_snapshots_cannot_start_an_episode(db):
    assert record(db, signal("Initialized", active="false")) is None
    assert record(db, signal("Initialized")) is None
    assert events.event_query(db).count() == 0


def test_a_long_event_can_end_after_more_than_a_day(db):
    identifier = legacy_open(db, signal())
    row = db.get(AuditEvent, identifier)
    row.created_at -= timedelta(days=3)
    db.commit()
    record(db, signal("Deleted"))
    assert db.get(AuditEvent, identifier).event_metadata["active"] is False
    assert events.event_query(db).count() == 1


def test_data_only_kind_can_still_be_closed_by_a_deleted_without_data(db):
    start = replace(signal(topic="Smart/Alarm"), kind="person")
    identifier = legacy_open(db, start)
    assert record(db, replace(start, active=False, operation="Deleted", kind="other")) is None
    assert db.get(AuditEvent, identifier).event_metadata["active"] is False
    assert record(db, replace(start, key={"ObjectId": "absent"}, active=False, kind="other")) is None
    assert events.event_query(db).count() == 1


def test_disconnect_invalidates_all_open_rows_not_just_the_first_batch(db):
    template = signal()
    for index in range(270):
        legacy_open(db, replace(template, key={"ObjectId": str(index)}, source={"VideoSourceConfigurationToken": "lens1", "Rule": str(index)}))
    events.source_disconnected(db, group())
    assert all(row.event_metadata["active"] is None and row.event_metadata["state_unconfirmed"]
               for row in events.event_query(db).all())


def test_receiver_diagnostics_explain_initialized_ends_and_observed_capabilities(monkeypatch, tmp_path):
    import threading
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db.session import Base
    from app.services import camera_events_worker as worker
    engine = create_engine("sqlite:///" + str(tmp_path / "receiver.sqlite3"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as session:
        register_fixture_cameras(session)
    current_group = {**group(), "host": "192.0.2.1", "port": 80, "username": "fixture", "password": "fixture-only", "onvif_path": "/onvif/device_service"}
    current_group["cameras"][0]["events_enabled"] = True
    started = notification(topic="Smart/Alarm")
    started["Message"]["_value_1"].find("{*}Data")[0].set("Name", "IsPeople")
    ended = notification(topic="Smart/Alarm", active=None)
    ended["Message"]["_value_1"].set("PropertyOperation", "Deleted")
    unknown = notification(topic="Device/Notice")
    unknown["Message"]["_value_1"].find("{*}Data")[0].set("Name", "Value")
    unknown["Message"]["_value_1"].find("{*}Data")[0].set("Value", "7.5")
    class Client:
        def __init__(self, *_): pass
        def topics(self): return []
        def profiles(self): return []
        def subscribe(self): pass
        def close(self): pass
        def pull(self): return [notification(topic="Device/Heartbeat"), notification(initialized=True, active="false"), started, ended, unknown]
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "DeviceClient", Client)
    monkeypatch.setattr(worker, "acquire_worker_lease", lambda *a, **k: object())
    monkeypatch.setattr(worker, "renew_worker_lease", lambda *a, **k: None)
    monkeypatch.setattr(worker, "release_worker_lease", lambda *a, **k: None)
    monkeypatch.setattr(worker, "group_is_current", lambda *a: True)
    monkeypatch.setattr(worker, "profile_identities", lambda _: [{"profile": "p", "source": "lens1", "configuration": "lens1", "analytics": None}])
    monkeypatch.setattr(worker, "_stop", threading.Event())
    receiver, result = worker.Receiver(current_group), {}
    def capture(group, status, **fields):
        if status == "connected":
            result.update(fields)
            receiver.stop.set()
    monkeypatch.setattr(worker, "_state", capture)
    try:
        receiver.run()
        assert result["messages_received"] == 5 and result["messages_parsed"] == 4
        assert result["messages_initialized"] == 1 and result["messages_inactive"] == 2
        assert result["messages_deleted"] == 1 and result["messages_ignored"] == 1
        assert result["messages_unsupported"] == 1 and result["messages_unclassified_end"] == 1
        assert result["messages_without_state"] == 1 and result["events_saved"] == 1
        assert result["advertised_kinds"] == [] and result["observed_kinds"] == ["motion", "person"]
        assert result["kinds"] == ["motion", "person"]
        with factory() as session:
            assert events.event_query(session).count() == 1
            assert events.serialize(events.event_query(session).one())["active"] is True
    finally:
        engine.dispose()
