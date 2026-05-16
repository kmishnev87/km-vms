from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.camera import Camera
from app.models.user import User
from app.models.workspace_layout import UserWorkspaceLayout
from app.routers.users import get_workspace_layout, put_workspace_layout, sanitize_workspace_layout


def session_with_schema():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def user(user_id: int, role: str):
    return SimpleNamespace(id=user_id, role=role, is_active=True)


def test_live_workspace_layout_is_sanitized_and_persisted_per_user():
    db = session_with_schema()
    owner = User(id=1, username="owner", password_hash="hash", role="owner", is_active=True)
    operator = User(id=2, username="operator", password_hash="hash", role="operator", is_active=True)
    db.add_all([owner, operator])
    db.add_all([
        Camera(id=10, name="A", storage_folder_name="a", protocol="rtsp", host="a", port=554),
        Camera(id=20, name="B", storage_folder_name="b", protocol="rtsp", host="b", port=554),
    ])
    db.commit()

    payload = {
        "layout_version": 1,
        "tiles": [
            {
                "id": "tile-1",
                "cameraId": "10",
                "stream": "sub",
                "xPct": -1,
                "yPct": 2,
                "wPct": 2,
                "hPct": 0.01,
                "z": 50000,
                "password": "must-not-persist",
                "rtsp_main_url": "rtsp://secret@example/live",
            },
            {"id": "duplicate", "cameraId": "10", "stream": "main"},
        ],
        "sidebarCameraOrder": ["20", "10", "20", "", {"bad": True}, "999"],
    }

    saved = put_workspace_layout("live", payload, db=db, current_user=owner)
    assert saved["workspace_key"] == "live"
    assert saved["layout_version"] == 1
    assert len(saved["tiles"]) == 1
    assert saved["tiles"][0] == {
        "id": "tile-1",
        "cameraId": "10",
        "xPct": 0.0,
        "yPct": 0.95,
        "wPct": 1.0,
        "hPct": 0.05,
        "z": 10000,
        "stream": "sub",
    }
    assert saved["sidebarCameraOrder"] == ["20", "10"]
    rendered = str(db.query(UserWorkspaceLayout).first().layout)
    assert "must-not-persist" not in rendered
    assert "rtsp://" not in rendered

    put_workspace_layout(
        "live",
        {"layout_version": 1, "tiles": [{"id": "other", "cameraId": "20", "stream": "main"}]},
        db=db,
        current_user=operator,
    )
    assert get_workspace_layout("live", db=db, current_user=owner)["tiles"][0]["cameraId"] == "10"
    assert get_workspace_layout("live", db=db, current_user=operator)["tiles"][0]["cameraId"] == "20"
    assert get_workspace_layout("live", db=db, current_user=owner)["sidebarCameraOrder"] == ["20", "10"]
    assert get_workspace_layout("live", db=db, current_user=operator)["sidebarCameraOrder"] == []


def test_workspace_layout_permissions_follow_workspace_key():
    db = session_with_schema()
    viewer = user(3, "viewer")
    operator = user(4, "operator")
    unknown = user(5, "unknown")

    assert get_workspace_layout("live", db=db, current_user=viewer)["tiles"] == []
    assert get_workspace_layout("chronology", db=db, current_user=operator)["tiles"] == []

    with pytest.raises(HTTPException) as exc:
        get_workspace_layout("chronology", db=db, current_user=viewer)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        get_workspace_layout("live", db=db, current_user=unknown)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        get_workspace_layout("unknown", db=db, current_user=operator)
    assert exc.value.status_code == 404


def test_workspace_layout_rejects_oversized_tile_payload():
    payload = {"layout_version": 1, "tiles": [{"cameraId": str(index)} for index in range(65)]}

    with pytest.raises(HTTPException) as exc:
        sanitize_workspace_layout("chronology", payload)

    assert exc.value.status_code == 422
