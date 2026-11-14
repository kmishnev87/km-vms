from datetime import timedelta, timezone
import uuid

import pytest

from app.models.audit_event import AuditEvent
from app.models.user import User
from app.models.workspace_layout import UserWorkspaceLayout
from app.services import camera_events as events
from app.services import camera_event_selections as selections
from test_camera_events_api_stage172 import api


def seed(factory, count=75, *, camera="1", kind="motion", seconds=0):
    instant = events.now_utc() + timedelta(seconds=seconds)
    ids = [str(uuid.uuid4()) for _ in range(count)]
    with factory() as db:
        db.add_all([AuditEvent(id=value, category=events.CATEGORY, event_type="camera." + kind,
            target_type="camera", target_id=camera, target_name="Selection fixture", created_at=instant,
            severity="info", message_ru="", message_en="", event_metadata={}) for value in ids])
        db.commit()
    return ids


def choose(client, *, kind="motion", camera=1, unread=False):
    page = client.get("/camera-events", params={"camera_id": camera, "kind": kind, "limit": 25, "unread": unread}).json()
    response = client.post("/camera-events/selections", json={"through": page["read_cursor"],
        "camera_id": camera, "kind": kind, "unread": unread, "visible_ids": [row["id"] for row in page["items"]]})
    assert response.status_code == 200, response.text
    return response.json(), page


def admin(factory):
    with factory() as db:
        db.get(User, 1).role = "admin"
        db.commit()


def test_cross_page_read_freezes_filters_new_events_and_deselection(api):
    client, factory, original, _ = api
    ids = seed(factory)
    unrelated = seed(factory, 3, camera="2") + seed(factory, 2, kind="audio")
    snapshot, page = choose(client)
    assert snapshot["count"] == 76 and len(snapshot["visible_ids"]) == 25
    later = seed(factory, 2, seconds=1)
    second = client.get("/camera-events", params={"camera_id": 1, "kind": "motion", "offset": 25,
        "limit": 25, "selection_id": snapshot["id"]}).json()
    assert second["selection"]["count"] == 76
    assert set(second["selection"]["visible_ids"]) <= set(ids + [original])
    excluded = snapshot["visible_ids"][0]
    response = client.post(f'/camera-events/selections/{snapshot["id"]}/read', json={"excluded_ids": [excluded]})
    assert response.status_code == 200 and response.json()["marked"] == 75
    unread = client.get("/camera-events", params={"unread": True, "limit": 100}).json()
    assert set(row["id"] for row in unread["items"]) == set(unrelated + later + [excluded])


def test_cross_page_delete_does_not_re_evaluate_unread_or_object_class_filters(api):
    client, factory, original, _ = api
    admin(factory)
    ids = seed(factory, 75, kind="person")
    other = seed(factory, 1, kind="audio")[0]
    snapshot, _ = choose(client, kind="person", unread=True)
    # A second tab reading an item cannot silently change the frozen selection.
    client.post("/camera-events/read", json={"ids": [ids[0]]})
    with factory() as db:
        db.get(AuditEvent, other).event_metadata = {"object_kind": "person"}
        db.commit()
    later = seed(factory, 1, kind="person", seconds=1)[0]
    response = client.post(f'/camera-events/selections/{snapshot["id"]}/delete', json={})
    assert response.status_code == 200 and response.json()["deleted"] == 75
    remaining = client.get("/camera-events", params={"limit": 100}).json()
    assert {row["id"] for row in remaining["items"]} == {original, other, later}
    with factory() as db:
        assert not db.query(AuditEvent).filter(AuditEvent.category == events.READ_CATEGORY).count()
        assert db.query(AuditEvent).filter(AuditEvent.event_type == "cameras.events.deleted").count() == 1
    assert client.post(f'/camera-events/selections/{snapshot["id"]}/delete', json={}).json()["deleted"] == 0


def test_selection_is_owner_bound_and_has_no_delete_permission_for_a_viewer(api):
    client, factory, _, _ = api
    snapshot, _ = choose(client)
    assert client.post(f'/camera-events/selections/{snapshot["id"]}/delete', json={}).status_code == 403
    with factory() as db:
        db.add(User(id=2, username="other-selection-user", password_hash="test-only", role="viewer", is_active=True))
        db.get(UserWorkspaceLayout, db.query(UserWorkspaceLayout.id).filter(UserWorkspaceLayout.workspace_key == selections.PREFIX + snapshot["id"]).scalar()).user_id = 2
        db.commit()
    assert client.post(f'/camera-events/selections/{snapshot["id"]}/read', json={}).status_code == 409
    assert client.get("/camera-events", params={"selection_id": snapshot["id"]}).status_code == 409
    assert client.get("/camera-events").json()["unread_count"] == 1


def test_expired_selection_never_falls_back_to_all_history(api):
    client, factory, _, _ = api
    admin(factory)
    snapshot, _ = choose(client)
    with factory() as db:
        row = db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.workspace_key == selections.PREFIX + snapshot["id"]).one()
        row.created_at = events.now_utc() - selections.TTL - timedelta(seconds=1)
        db.commit()
    response = client.post(f'/camera-events/selections/{snapshot["id"]}/delete', json={})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "event_selection_expired"
    assert client.get("/camera-events").json()["total"] == 1


def test_snapshot_storage_is_bounded_and_keeps_preferences_and_layouts(api):
    client, factory, _, _ = api
    client.put("/camera-events/preferences", json={"notifications": False})
    snapshots = [choose(client)[0] for _ in range(selections.MAX_SNAPSHOTS + 2)]
    with factory() as db:
        assert selections._query(db, 1).count() == selections.MAX_SNAPSHOTS
    assert client.get("/camera-events/preferences").json()["notifications"] is False
    assert client.post(f'/camera-events/selections/{snapshots[0]["id"]}/read', json={}).status_code == 409
    assert client.post(f'/camera-events/selections/{snapshots[-1]["id"]}/read', json={}).status_code == 200


def test_oversized_selection_fails_explicitly_instead_of_silently_selecting_part(api, monkeypatch):
    client, factory, _, _ = api
    seed(factory, 2)
    monkeypatch.setattr(events, "MAX_EVENTS", 2)
    page = client.get("/camera-events").json()
    response = client.post("/camera-events/selections", json={"through": page["read_cursor"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "event_selection_too_large"


def test_filtered_delete_rolls_back_if_its_audit_cannot_be_saved(api, monkeypatch):
    from app.services import audit_log
    client, factory, _, _ = api
    admin(factory)
    seed(factory, 3)
    snapshot, _ = choose(client)
    monkeypatch.setattr(audit_log, "create_event", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="camera_event_delete_audit_failed"):
        client.post(f'/camera-events/selections/{snapshot["id"]}/delete', json={})
    assert client.get("/camera-events").json()["total"] == 4


@pytest.mark.parametrize("payload", [{"through": "bad"}, {"through": "2026-11-14T00:00:00|" + str(uuid.uuid4()), "all": True},
    {"through": "2026-11-14T00:00:00|" + str(uuid.uuid4()), "unread": "true"}])
def test_malformed_scope_is_rejected_without_mutation(api, payload):
    client, _, _, _ = api
    assert client.post("/camera-events/selections", json=payload).status_code == 422
    assert client.get("/camera-events").json()["total"] == 1
