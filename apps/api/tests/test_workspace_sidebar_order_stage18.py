from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.camera import Camera
from app.models.user import User
from app.routers.users import get_workspace_layout, put_workspace_layout, sanitize_workspace_layout


def session_with_schema():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def add_user(db, user_id: int, username: str, role: str = "operator"):
    item = User(id=user_id, username=username, password_hash="hash", role=role, is_active=True)
    db.add(item)
    return item


def add_camera(db, camera_id: int, name: str, deleted=False):
    item = Camera(
        id=camera_id,
        name=name,
        storage_folder_name=name.lower(),
        protocol="rtsp",
        host=f"{name}.local",
        port=554,
        deleted_at=datetime.utcnow() if deleted else None,
    )
    db.add(item)
    return item


def test_sidebar_camera_order_persists_per_user_and_workspace():
    db = session_with_schema()
    owner = add_user(db, 1, "owner", "owner")
    operator = add_user(db, 2, "operator")
    add_camera(db, 1, "Alpha")
    add_camera(db, 2, "Beta")
    add_camera(db, 3, "Gamma")
    db.commit()

    live = put_workspace_layout(
        "live",
        {"layout_version": 1, "tiles": [{"cameraId": "1", "stream": "sub"}], "sidebarCameraOrder": ["3", "2", "1"]},
        db=db,
        current_user=owner,
    )
    chronology = put_workspace_layout(
        "chronology",
        {"layout_version": 1, "tiles": [{"cameraId": "2"}], "sidebarCameraOrder": ["1", "3"]},
        db=db,
        current_user=owner,
    )
    put_workspace_layout(
        "live",
        {"layout_version": 1, "tiles": [], "sidebarCameraOrder": ["2", "3"]},
        db=db,
        current_user=operator,
    )

    assert live["sidebarCameraOrder"] == ["3", "2", "1"]
    assert chronology["sidebarCameraOrder"] == ["1", "3"]
    assert get_workspace_layout("live", db=db, current_user=owner)["sidebarCameraOrder"] == ["3", "2", "1"]
    assert get_workspace_layout("chronology", db=db, current_user=owner)["sidebarCameraOrder"] == ["1", "3"]
    assert get_workspace_layout("live", db=db, current_user=operator)["sidebarCameraOrder"] == ["2", "3"]


def test_sidebar_camera_order_sanitizes_duplicate_invalid_deleted_and_oversized_values():
    db = session_with_schema()
    owner = add_user(db, 1, "owner", "owner")
    add_camera(db, 1, "Alpha")
    add_camera(db, 2, "Beta")
    add_camera(db, 4, "Deleted", deleted=True)
    db.commit()

    payload = {
        "layout_version": 1,
        "tiles": [{"cameraId": "1"}, {"cameraId": "2"}],
        "sidebarCameraOrder": ["2", "2", 1, "", None, ["bad"], {"bad": True}, "4", "999", *[str(1000 + i) for i in range(600)]],
    }

    saved = put_workspace_layout("chronology", payload, db=db, current_user=owner)

    assert saved["sidebarCameraOrder"] == ["2", "1"]
    assert [tile["cameraId"] for tile in saved["tiles"]] == ["1", "2"]


def test_sidebar_camera_order_rejects_non_list_without_traceback():
    with pytest.raises(HTTPException) as exc:
        sanitize_workspace_layout("live", {"layout_version": 1, "tiles": [], "sidebarCameraOrder": {"1": 1}}, {"1"})

    assert exc.value.status_code == 422
    assert "traceback" not in str(exc.value.detail).lower()


def test_viewer_permission_for_chronology_remains_forbidden():
    db = session_with_schema()
    viewer = SimpleNamespace(id=3, role="viewer", is_active=True)

    with pytest.raises(HTTPException) as exc:
        put_workspace_layout(
            "chronology",
            {"layout_version": 1, "tiles": [], "sidebarCameraOrder": ["1"]},
            db=db,
            current_user=viewer,
        )

    assert exc.value.status_code == 403
