from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.permissions import ROLE_OPERATOR, ROLE_OWNER
from app.core.security import create_access_token
from app.db.session import Base, get_db
from app.main import app
from app.models.user import User
from app.routers.previews import camera_preview, camera_test_preview


def _auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.username)}"}


@pytest.fixture
def preview_client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'preview-auth.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    owner = User(username="preview_owner", full_name="owner", password_hash="hash", role=ROLE_OWNER, is_active=True)
    operator = User(username="preview_operator", full_name="operator", password_hash="hash", role=ROLE_OPERATOR, is_active=True)
    db.add_all([owner, operator])
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    original_storage_previews = settings.storage_previews
    settings.storage_previews = str(tmp_path / "previews")
    try:
        yield TestClient(app), owner, operator
    finally:
        settings.storage_previews = original_storage_previews
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_camera_preview_is_served_by_api_from_storage_previews(tmp_path):
    original_storage_previews = settings.storage_previews
    settings.storage_previews = str(tmp_path / "previews")
    preview = settings.camera_preview_path(7)
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"jpg")

    try:
        response = camera_preview(7)
        assert isinstance(response, FileResponse)
        assert Path(response.path) == preview.resolve()
        assert response.media_type == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
    finally:
        settings.storage_previews = original_storage_previews


def test_camera_test_preview_rejects_path_like_tokens(tmp_path):
    original_storage_previews = settings.storage_previews
    settings.storage_previews = str(tmp_path / "previews")

    try:
        with pytest.raises(HTTPException) as exc:
            camera_test_preview("../secret")
        assert exc.value.status_code == 404
    finally:
        settings.storage_previews = original_storage_previews


def test_camera_test_preview_is_served_by_api_from_storage_previews(tmp_path):
    original_storage_previews = settings.storage_previews
    settings.storage_previews = str(tmp_path / "previews")
    preview = settings.camera_test_preview_path("safe-token_123")
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"jpg")

    try:
        response = camera_test_preview("safe-token_123")
        assert isinstance(response, FileResponse)
        assert Path(response.path) == preview.resolve()
    finally:
        settings.storage_previews = original_storage_previews


def test_preview_routes_require_manage_cameras_and_keep_image_contract(preview_client):
    client, owner, operator = preview_client
    persisted = settings.camera_preview_path(7)
    persisted.parent.mkdir(parents=True)
    persisted.write_bytes(b"jpg")
    tested = settings.camera_test_preview_path("safe-token")
    tested.parent.mkdir(parents=True, exist_ok=True)
    tested.write_bytes(b"jpg")

    for path in ("/previews/camera-previews/7.jpg", "/previews/camera-tests/safe-token.jpg"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=_auth_headers(operator)).status_code == 403
        response = client.get(path, headers=_auth_headers(owner))
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
