"""Parallel personal choices on an owned disposable PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from sqlalchemy.orm import Session
from app.models import Camera
from app.models.workspace_layout import UserWorkspaceLayout
from app.routers import camera_events as routes
from app.services import camera_events as events
from app.services.schema_migrations import execute_migration_plan
from test_format_schema_stage1375_postgres import format_database, business_snapshot, pytestmark


def test_first_concurrent_camera_choices_merge_without_losing_either_change(format_database):
    engine, db = format_database
    execute_migration_plan(db)
    db.add(Camera(id=2, name="Second", storage_folder_name="second", host="192.0.2.2", port=554, protocol="rtsp", enabled=False))
    db.commit()
    before = business_snapshot(db)
    for _ in range(3):
        db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.user_id == 1,
            UserWorkspaceLayout.workspace_key == events.PREFERENCE_KEY).delete(synchronize_session=False)
        db.commit()
        gate = Barrier(2)
        def choose(camera_id):
            with Session(engine) as own:
                gate.wait(timeout=10)
                return routes.choose_notification(routes.NotificationCameraChoice(camera_id=camera_id, enabled=False),
                    db=own, user=SimpleNamespace(id=1))
        with ThreadPoolExecutor(max_workers=2) as executor:
            values = list(executor.map(choose, (1, 2)))
        db.expire_all()
        final = events.preferences(db, 1)[0]
        assert final["camera_ids"] == [] and final["notifications"]
        assert db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.user_id == 1,
            UserWorkspaceLayout.workspace_key == events.PREFERENCE_KEY).count() == 1
    assert business_snapshot(db) == before


def test_concurrent_master_and_camera_keep_individual_hours_and_other_user(format_database):
    engine, db = format_database
    execute_migration_plan(db)
    policy = {**events.preference_value({}), "hours_enabled": True, "schedule_mode": "per_camera",
              "kinds": ["person"], "camera_schedules": {"1": {"start_minute": 1140, "end_minute": 420}}}
    events.save_preferences(db, 1, policy)
    before = business_snapshot(db)
    gate = Barrier(2)
    def choose(payload):
        with Session(engine) as own:
            gate.wait(timeout=10)
            return routes.choose_notification(payload, db=own, user=SimpleNamespace(id=1))
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(choose, (routes.NotificationMasterChoice(notifications=False),
            routes.NotificationCameraChoice(camera_id=1, enabled=False))))
    db.expire_all()
    assert events.preferences(db, 1)[0] == {**policy, "notifications": False, "camera_ids": []}
    assert events.preferences(db, 2)[0] == events.preference_value({})
    assert business_snapshot(db) == before
