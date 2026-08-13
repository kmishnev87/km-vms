import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.core.config import settings
from app.models.user import User
from app.routers.deps import require_permission

router = APIRouter(prefix="/previews", tags=["previews"])

_TEST_PREVIEW_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _preview_file_response(path: Path) -> FileResponse:
    root = Path(settings.storage_previews).resolve()
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Preview not found")

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Preview not found")

    return FileResponse(
        resolved,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/camera-previews/{camera_id}.jpg")
def camera_preview(
    camera_id: int,
    current_user: User = Depends(require_permission("manage_cameras")),
):
    return _preview_file_response(settings.camera_preview_path(camera_id))


@router.get("/camera-tests/{token}.jpg")
def camera_test_preview(
    token: str,
    current_user: User = Depends(require_permission("manage_cameras")),
):
    if not _TEST_PREVIEW_RE.fullmatch(token):
        raise HTTPException(status_code=404, detail="Preview not found")
    return _preview_file_response(settings.camera_test_preview_path(token))
