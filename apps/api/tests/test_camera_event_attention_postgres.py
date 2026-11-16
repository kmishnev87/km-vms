"""The new attention contract on an owned disposable PostgreSQL database."""
from datetime import datetime
from types import SimpleNamespace
import uuid

from app.models import AuditEvent, User
from app.services import camera_events as events
from app.services.onvif_events import Signal
from app.services.schema_migrations import execute_migration_plan
from test_format_schema_stage1375_postgres import format_database, business_snapshot, pytestmark


def accept(db):
    camera = {"id": 1, "name": "Preserved camera"}
    return events.record_signal(db, {"device_id": "fixture", "cameras": [camera]}, camera,
        Signal("Motion/" + uuid.uuid4().hex, "motion", True, False, events.now_utc(), None, False, {}))


def test_muted_arrival_markers_survive_reenable_and_never_touch_the_old_backlog(format_database):
    _, db = format_database
    execute_migration_plan(db)
    old = accept(db)
    before = business_snapshot(db)
    events.save_preferences(db, 1, {"notifications": False})
    muted = accept(db)
    events.save_preferences(db, 1, {"notifications": True})
    page = events.list_camera_events(db, 1)
    assert page["total"] == 2 and page["unread_count"] == 1
    assert {v["id"] for v in page["items"] if v["read"]} == {muted}
    assert not events.event_notification_allowed(db, 1, page.get("preferences", {"notifications": True}), events.serialize(db.get(AuditEvent, muted)), events.timezone_context(db))
    assert events.automatic_read_ids(db, 1, [old, muted]) == {muted}
    assert events.list_camera_events(db, 2)["unread_count"] == 2
    assert business_snapshot(db) == before


def test_individual_schedules_round_trip_on_postgres_without_new_schema(format_database):
    _, db = format_database
    execute_migration_plan(db)
    before = business_snapshot(db)
    policy = {"notifications": True, "hours_enabled": True, "schedule_mode": "per_camera", "start_minute": 0, "end_minute": 0,
              "camera_schedules": {"1": {"start_minute": 1140, "end_minute": 420}}}
    events.save_preferences(db, 1, policy)
    saved = events.preferences(db, 1)[0]
    assert saved["camera_schedules"] == policy["camera_schedules"] and saved["schedule_mode"] == "per_camera"
    context = events.timezone_context(db)
    assert not events.notifications_allowed(saved, {"camera_id": 1}, context, at=datetime(2026, 11, 16, 12))
    assert events.notifications_allowed(saved, {"camera_id": 1}, context, at=datetime(2026, 11, 16, 20))
    assert events.notifications_allowed(saved, {"camera_id": 2}, context, at=datetime(2026, 11, 16, 12))
    assert business_snapshot(db) == before
