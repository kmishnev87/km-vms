from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.core.config import settings
from app.routers.previews import camera_preview, camera_test_preview


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
