"""Bulk acknowledgement on a disposable PostgreSQL DB, never a real archive."""
from datetime import datetime, timedelta
import uuid

from app.models.audit_event import AuditEvent
from app.services import camera_events as events
from app.services.schema_migrations import execute_migration_plan
from test_format_schema_stage1375_postgres import format_database, business_snapshot, pytestmark


def test_useful_rule_class_filters_and_clock_cleanup_on_real_postgres(format_database):
    from app.services import onvif_events as ov
    from test_camera_events_stage172 import notification, group
    from test_camera_events_useful_types import data_items
    _engine, db = format_database
    execute_migration_plan(db)
    before = business_snapshot(db)
    signal = ov.parse_notification(data_items(notification(topic="RuleEngine/LineDetector/Crossed"), ClassTypes="Human Vehicle Animal"))
    identifier = events.record_signal(db, group(), group()["cameras"][0], signal)
    for kind in ("line_crossing", "person", "vehicle", "animal"):
        assert [item["id"] for item in events.list_camera_events(db, 1, kind=kind)["items"]] == [identifier]
    clock = str(uuid.uuid4())
    db.add(AuditEvent(id=clock, created_at=events.now_utc(), category=events.CATEGORY, event_type="camera.other",
        severity="info", message_ru="", message_en="", event_metadata={"topic": ov.SERVICE_HISTORY_TOPICS[0]}))
    db.commit()
    events.mark_read(db, 1, [clock])
    assert events.cleanup_history(db) == 1
    assert db.get(AuditEvent, identifier) is not None
    assert db.query(AuditEvent).filter(AuditEvent.category == events.READ_CATEGORY).count() == 0
    assert business_snapshot(db) == before


def test_bulk_read_batches_are_atomic_per_user_and_preserve_a_new_same_timestamp_event(format_database):
    _engine, db = format_database
    execute_migration_plan(db)
    business_before = business_snapshot(db)
    stamp = datetime(2026, 11, 13, 12)
    def event(identifier, created):
        return AuditEvent(id=identifier, created_at=created, category=events.CATEGORY, event_type="camera.other",
            severity="info", message_ru="", message_en="", target_type="camera", target_id="1", target_name="Front", event_metadata={})
    db.add_all([event(str(uuid.uuid4()), stamp-timedelta(seconds=1)) for _ in range(1005)])
    db.add(event("00000000-0000-0000-0000-000000000001", stamp))
    db.commit()
    snapshot = events.list_camera_events(db, 1, limit=1)
    assert snapshot["total"] == 1006
    db.add(event("00000000-0000-0000-0000-000000000002", stamp))
    db.commit()
    cutoff, identifier = snapshot["read_cursor"].split("|", 1)
    assert events.mark_all_read(db, 1, datetime.fromisoformat(cutoff), identifier) == 1006
    remaining = events.list_camera_events(db, 1, unread=True)
    assert [item["id"] for item in remaining["items"]] == ["00000000-0000-0000-0000-000000000002"]
    assert events.mark_all_read(db, 1, datetime.fromisoformat(cutoff), identifier) == 0
    assert events.list_camera_events(db, 2)["unread_count"] == 1007
    assert business_snapshot(db) == business_before


def test_bulk_delete_preserves_new_same_timestamp_events_and_business_rows(format_database, monkeypatch):
    _engine, db = format_database
    execute_migration_plan(db)
    before = business_snapshot(db)
    stamp = datetime(2026, 11, 13, 12)
    identifiers = [str(uuid.uuid4()) for _ in range(1005)]
    for identifier in identifiers:
        db.add(AuditEvent(id=identifier, created_at=stamp-timedelta(seconds=1), category=events.CATEGORY,
            event_type="camera.motion", severity="info", message_ru="", message_en="", event_metadata={}))
    last = "00000000-0000-0000-0000-000000000001"
    db.add(AuditEvent(id=last, created_at=stamp, category=events.CATEGORY, event_type="camera.motion",
        severity="info", message_ru="", message_en="", event_metadata={}))
    db.commit()
    events.mark_all_read(db, 1, stamp, last)
    events.mark_all_read(db, 2, stamp, last)
    newer = "00000000-0000-0000-0000-000000000002"
    db.add(AuditEvent(id=newer, created_at=stamp, category=events.CATEGORY, event_type="camera.motion",
        severity="info", message_ru="", message_en="", event_metadata={}))
    db.commit()
    # A temporarily exceeded retention cap must not truncate "delete all".
    monkeypatch.setattr(events, "MAX_EVENTS", 1)
    assert events.delete_camera_events(db, through=(stamp, last)) == 1006
    assert events.delete_camera_events(db, through=(stamp, last)) == 0
    assert [row.id for row in events.event_query(db).all()] == [newer]
    assert db.query(AuditEvent).filter(AuditEvent.category == events.READ_CATEGORY).count() == 0
    assert business_snapshot(db) == before


def test_read_racing_with_delete_does_not_recreate_orphan_markers(format_database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import event, text
    from sqlalchemy.orm import Session
    engine, db = format_database
    execute_migration_plan(db)
    identifier = str(uuid.uuid4())
    db.add(AuditEvent(id=identifier, created_at=datetime(2026, 11, 13, 12), category=events.CATEGORY,
        event_type="camera.motion", severity="info", message_ru="", message_en="", event_metadata={}))
    db.commit()
    db.execute(text("SELECT id FROM audit_events WHERE id=:id FOR UPDATE"), {"id": identifier})
    waiting = Event()
    def before_cursor(connection, cursor, statement, parameters, context, many):
        if "FOR UPDATE" in statement:
            waiting.set()
    def read_in_parallel():
        with Session(engine) as reader:
            event.listen(reader.connection(), "before_cursor_execute", before_cursor)
            return events.mark_read(reader, 1, [identifier])
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(read_in_parallel)
        try:
            assert waiting.wait(3), "reader must reach its row-locking SELECT"
            assert not result.done()
            assert events.delete_camera_events(db, ids=[identifier]) == 1
        finally:
            db.rollback()
        assert result.result(timeout=5) == 0
    assert db.query(AuditEvent).filter(AuditEvent.category == events.READ_CATEGORY).count() == 0
