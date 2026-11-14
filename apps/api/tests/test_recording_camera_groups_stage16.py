from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app.routers import recordings
from test_stage11_retention_automation import db, add_camera, add_segment


def test_grouped_catalog_keeps_active_and_only_retained_deleted_cameras(db):
    active = add_camera(db, name="Current camera")
    old = add_camera(db, name="Old__deleted_12_3")
    empty = add_camera(db, name="Empty__deleted_12_4")
    gone = add_camera(db, name="Gone__deleted_12_5")
    for camera in (old, empty, gone):
        camera.deleted_at = datetime(2026, 11, 15)
    kept, _ = add_segment(db, old, name="retained", days_ago=0)
    kept.camera_name_snapshot = "Original name"
    removed, _ = add_segment(db, gone, name="removed", days_ago=0)
    removed.deleted_at = datetime(2026, 11, 15)
    db.commit()
    data = recordings.list_recording_cameras(db, SimpleNamespace(role="viewer"))
    assert [item["id"] for item in data["camera_items"]] == [str(active.id), str(old.id)]
    assert data["camera_items"][1] == {"id": str(old.id), "name": "Original name", "deleted": True, "value": f"camera:{old.id}"}
    assert "items" in data and "export_items" in data
    assert not any("password" in item or "host" in item for item in data["camera_items"])


def test_same_name_old_and_new_are_filtered_by_identity_not_label(db):
    old = add_camera(db, name="Yard__deleted_12_3")
    old.deleted_at = datetime(2026, 11, 15)
    current = add_camera(db, name="Yard")
    first, _ = add_segment(db, old, name="old", days_ago=0)
    second, _ = add_segment(db, current, name="new", days_ago=0)
    first.camera_name_snapshot = second.camera_name_snapshot = "Yard"
    db.commit()
    result = recordings.list_recordings(db=db, current_user=SimpleNamespace(role="viewer"), camera_id=old.id)
    assert result["total"] == 1
    assert [item["camera_id"] for item in result["items"]] == [old.id]
    assert [item.id for item in recordings._deletion_scope_query(db, scope="camera", camera="Yard", camera_id=old.id)] == [first.id]
    assert recordings._deletion_scope_query(db, scope="camera", camera_id=99999).count() == 0
    with pytest.raises(HTTPException):
        recordings._deletion_scope_query(db, scope="all", camera_id=old.id)


def test_legacy_camera_names_are_not_reinterpreted_as_selector_tokens(db):
    camera = add_camera(db, name="camera:123")
    segment, _ = add_segment(db, camera, name="literal-name", days_ago=0)
    db.commit()
    query = recordings.recordings_query_for_filters(db, camera_name="camera:123")
    assert [item.id for item in query] == [segment.id]


@pytest.mark.parametrize("invalid", [0, -1, True, "not-an-id"])
def test_invalid_identity_cannot_expand_to_all_recordings(db, invalid):
    with pytest.raises(HTTPException):
        recordings.apply_camera_filter(recordings.finalized_segments_query(db), db, None, invalid)
