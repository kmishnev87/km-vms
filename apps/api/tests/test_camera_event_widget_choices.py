"""Widget choices update one personal field, not reception, hours or history."""
from datetime import datetime
import pytest
from app.models import Camera, User
from app.services import camera_events as events
from test_camera_events_api_stage172 import api


def test_widget_camera_choice_preserves_other_preferences_history_and_recording(api):
    client, factory, _, _ = api
    with factory() as db:
        db.add(Camera(id=2, name="Other", storage_folder_name="other", host="192.0.2.2", port=554, protocol="rtsp", enabled=True))
        db.commit()
    policy = {"notifications": True, "kinds": ["person"], "hours_enabled": True,
              "schedule_mode": "per_camera", "start_minute": 1140, "end_minute": 420,
              "camera_schedules": {"1": {"start_minute": 1380, "end_minute": 360}}}
    baseline = client.put("/camera-events/preferences", json=policy).json()
    before = client.get("/camera-events").json()
    off = client.patch("/camera-events/preferences", json={"camera_id": 1, "enabled": False})
    assert off.status_code == 200 and off.json() == {**baseline, "camera_ids": [2]}
    on = client.patch("/camera-events/preferences", json={"camera_id": 1, "enabled": True})
    assert on.status_code == 200 and on.json() == {**baseline, "camera_ids": [1, 2]}
    after = client.get("/camera-events").json()
    assert before["unread_count"] == after["unread_count"] == 1
    assert before["items"] == after["items"]
    with factory() as db:
        assert db.get(Camera, 1).enabled and not db.get(Camera, 1).events_enabled
        assert events.preferences(db, 2)[0]["camera_ids"] is None


def test_widget_master_off_on_restores_camera_selection_and_does_not_clear_old_unread(api):
    client, _, _, _ = api
    initial = client.put("/camera-events/preferences", json={"camera_ids": [], "hours_enabled": True,
        "start_minute": 100, "end_minute": 200}).json()
    assert client.patch("/camera-events/preferences", json={"notifications": False}).json() == {**initial, "notifications": False}
    assert client.patch("/camera-events/preferences", json={"notifications": True}).json() == initial
    assert client.get("/camera-events").json()["unread_count"] == 1


@pytest.mark.parametrize("invalid", [{}, {"enabled": True}, {"camera_id": 1}, {"camera_id": True, "enabled": True},
    {"camera_id": 0, "enabled": True}, {"camera_id": 2147483648, "enabled": True}, {"notifications": 1},
    {"notifications": True, "camera_id": 1, "enabled": True}, {"notifications": True, "hours_enabled": False},
    {"camera_id": 1, "enabled": "true"}, {"camera_id": 1, "enabled": True, "events_enabled": False}])
def test_widget_rejects_ambiguous_or_out_of_scope_changes(api, invalid):
    client, _, _, _ = api
    before = client.get("/camera-events/preferences").json()
    assert client.patch("/camera-events/preferences", json=invalid).status_code == 422
    assert client.get("/camera-events/preferences").json() == before


def test_widget_cannot_select_missing_or_deleted_camera(api):
    client, factory, _, _ = api
    assert client.patch("/camera-events/preferences", json={"camera_id": 99, "enabled": True}).status_code == 404
    with factory() as db:
        db.get(Camera, 1).deleted_at = datetime.utcnow(); db.commit()
    assert client.patch("/camera-events/preferences", json={"camera_id": 1, "enabled": True}).status_code == 404


def test_widget_requires_existing_live_permission(api):
    client, factory, _, _ = api
    with factory() as db:
        user = db.get(User, 1)
        user.role = "disabled-test-role"
        db.commit()
    assert client.patch("/camera-events/preferences", json={"notifications": False}).status_code == 403
