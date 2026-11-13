"""Shared event deletion never owns the archive or the administrative audit."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest

from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.user import User
from app.services import camera_events as events
from app.services.onvif_events import Signal
from test_camera_events_api_stage172 import api


def administrator(factory):
    with factory() as db:
        db.get(User, 1).role = "admin"
        db.commit()


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_view_only_users_cannot_delete_shared_history(api, role):
    client, factory, identifier, _ = api
    with factory() as db:
        db.get(User, 1).role = role
        db.commit()
    snapshot = client.get("/camera-events").json()
    assert client.post("/camera-events/delete", json={"ids": [identifier]}).status_code == 403
    assert client.post("/camera-events/delete-all", json={"through": snapshot["read_cursor"]}).status_code == 403
    assert client.get("/camera-events").json()["total"] == 1


def test_selection_deletes_only_camera_episodes_and_all_their_markers(api):
    client, factory, identifier, _ = api
    administrator(factory)
    audit_id = str(uuid.uuid4())
    with factory() as db:
        db.add(User(id=2, username="other-viewer", password_hash="test-only", role="viewer", is_active=True))
        db.add(AuditEvent(id=audit_id, created_at=events.now_utc(), category="system", event_type="system.test",
            severity="info", message_ru="test audit", message_en="test audit", event_metadata={}))
        db.add(RecordingSegment(camera_id=1, file_path="/not-mounted/retained.mkv", relative_path="retained.mkv",
            started_at=events.now_utc(), status="ready", container_format="mkv"))
        db.commit()
        events.mark_read(db, 1, [identifier]); events.mark_read(db, 2, [identifier])
        events.save_preferences(db, 1, {"notifications": False})
    result = client.post("/camera-events/delete", json={"ids": [identifier, identifier, audit_id, str(uuid.uuid4())]})
    assert result.status_code == 200 and result.json()["deleted"] == 1
    assert client.post("/camera-events/delete", json={"ids": [identifier]}).json()["deleted"] == 0
    with factory() as db:
        assert db.get(AuditEvent, audit_id) is not None
        assert db.query(AuditEvent).filter(AuditEvent.category == events.READ_CATEGORY).count() == 0
        assert db.query(AuditEvent).filter(AuditEvent.event_type == "cameras.events.deleted").count() == 1
        assert db.query(RecordingSegment).count() == 1 and db.get(Camera, 1).enabled is True
        assert events.preferences(db, 1)[0]["notifications"] is False
    assert client.get(f"/camera-events/{identifier}/target?mode=live").status_code == 404
    assert client.get("/camera-events").json()["unread_count"] == 0
    # A later real signal remains receivable; deleting history does not disable it.
    with factory() as db:
        group = {"device_id": "device", "cameras": [{"id": 1, "name": "Front", "profile_token": "p"}]}
        assert events.record_signal(db, group, group["cameras"][0],
            Signal("Motion", "motion", True, False, events.now_utc(), None, True, {}))
    assert client.get("/camera-events").json()["total"] == 1


def test_delete_all_uses_full_snapshot_not_filters_or_one_page_and_keeps_newer(api):
    client, factory, identifier, _ = api
    administrator(factory)
    with factory() as db:
        db.add_all([AuditEvent(id=str(uuid.uuid4()), created_at=events.now_utc()-timedelta(days=8),
            category=events.CATEGORY, event_type="camera.other", severity="info", message_ru="", message_en="",
            target_type="camera", target_id="1", target_name="Front", event_metadata={}) for _ in range(1005)])
        db.commit()
        events.mark_read(db, 1, [identifier])
    snapshot = client.get("/camera-events", params={"since": events.now_utc().replace(tzinfo=timezone.utc).isoformat(), "limit": 1}).json()
    assert snapshot["total"] == 0 and snapshot["history_count"] == 1006
    newest = str(uuid.uuid4())
    with factory() as db:
        db.add(AuditEvent(id=newest, created_at=events.now_utc()+timedelta(seconds=1), category=events.CATEGORY,
            event_type="camera.motion", severity="info", message_ru="", message_en="", event_metadata={}))
        db.commit()
    response = client.post("/camera-events/delete-all", json={"through": snapshot["read_cursor"]})
    assert response.status_code == 200 and response.json()["deleted"] == 1006
    remaining = client.get("/camera-events").json()
    assert remaining["history_count"] == 1 and remaining["items"][0]["id"] == newest
    assert client.post("/camera-events/delete-all", json={"through": snapshot["read_cursor"]}).json()["deleted"] == 0


@pytest.mark.parametrize("path,payload", [
    ("delete", {"ids": []}), ("delete", {"ids": [str(uuid.uuid4())]*201}),
    ("delete", {"ids": ["not-a-uuid"]}), ("delete", {"ids": [str(uuid.uuid4())], "all": True}),
    ("delete-all", {}), ("delete-all", {"through": "invalid"}),
    ("delete-all", {"through": "2026-11-13T12:00:00Z|"+str(uuid.uuid4())}),
    ("delete-all", {"through": "2026-11-13T12:00:00|"+str(uuid.uuid4()), "ids": []}),
])
def test_invalid_scope_never_changes_history(api, path, payload):
    client, factory, _, _ = api
    administrator(factory)
    assert client.post("/camera-events/"+path, json=payload).status_code == 422
    assert client.get("/camera-events").json()["total"] == 1


def test_failed_transaction_reports_no_false_success_and_restores_events(api, monkeypatch):
    from app.services import audit_log
    client, factory, identifier, _ = api
    administrator(factory)
    client.post("/camera-events/read", json={"ids": [identifier]})
    monkeypatch.setattr(audit_log, "create_event", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="camera_event_delete_audit_failed"):
        client.post("/camera-events/delete", json={"ids": [identifier]})
    snapshot = client.get("/camera-events").json()
    assert snapshot["total"] == 1 and snapshot["items"][0]["read"] is True
