from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.camera import Camera
from app.routers import chronology, cameras
from app.routers.deps import get_db, get_current_user
from test_stage11_retention_automation import db, add_camera, add_segment
from test_stage14_final_chronology_selection import overlap


def test_catalog_includes_only_retained_archives_and_preserves_snapshot_names(db):
    active = add_camera(db, name="Active")
    old = add_camera(db, name="Old__deleted_123_2")
    empty = add_camera(db, name="Empty__deleted_123_3")
    removed = add_camera(db, name="Gone__deleted_123_4")
    damaged = add_camera(db, name="Damaged__deleted_123_5")
    for camera in (old, empty, removed, damaged):
        camera.deleted_at = datetime(2026, 11, 14)
    recording, _ = add_segment(db, old, name="retained", days_ago=0)
    recording.camera_name_snapshot = "Original yard name"
    deleted, _ = add_segment(db, removed, name="removed", days_ago=0)
    deleted.deleted_at = datetime(2026, 11, 14)
    broken, _ = add_segment(db, damaged, name="damaged", days_ago=0)
    broken.status = "missing"
    db.commit()
    items = chronology.chronology_cameras(db, SimpleNamespace(role="viewer"))
    assert [row["id"] for row in items] == [active.id, old.id]
    assert items[1]["name"] == "Original yard name" and items[1]["deleted"]
    assert not items[1]["enabled"] and not items[1]["host"]
    assert not {"username", "password", "rtsp_main_url", "rtsp_sub_url"}.intersection(items[1])
    live = cameras.list_viewer_cameras(db, SimpleNamespace(role="owner"))
    assert [row["id"] for row in live] == [active.id]


def test_retained_camera_playback_and_download_keep_the_same_scoped_identity(db):
    camera, first, path, _, _, user, ts = overlap(db)
    camera.deleted_at = datetime(2026, 11, 14)
    camera.name = "Old__deleted_123_2"
    first.camera_name_snapshot = "Old_archive_name"
    db.commit()
    assert chronology.chronology_playback(camera.id, ts, db, user)["has_video"]
    token = chronology.issue_chronology_download_token(camera.id, ts, db, user)
    assert token["segment_id"] == first.id
    response = chronology.chronology_download_current_recording(camera.id, ts, token["media_token"], db)
    assert str(response.path) == str(path)
    assert "Old_archive_name" in response.headers["content-disposition"]
    assert "__deleted_" not in response.headers["content-disposition"]


def test_retained_catalog_still_requires_timeline_permission(db):
    app = FastAPI()
    app.include_router(chronology.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, role="unknown")
    with TestClient(app) as client:
        assert client.get("/chronology/cameras").status_code == 403
