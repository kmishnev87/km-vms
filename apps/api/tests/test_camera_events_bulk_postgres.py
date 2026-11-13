"""Bulk acknowledgement on a disposable PostgreSQL DB, never a real archive."""
from datetime import datetime, timedelta
import uuid

from app.models.audit_event import AuditEvent
from app.services import camera_events as events
from app.services.schema_migrations import execute_migration_plan
from test_format_schema_stage1375_postgres import format_database, business_snapshot, pytestmark


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
