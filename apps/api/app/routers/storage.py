from pathlib import Path
import os

from fastapi import APIRouter, Depends
from app.core.config import settings
from app.models.user import User
from app.routers.deps import get_current_user

router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("/status")
def storage_status(current_user: User = Depends(get_current_user)):
    root = Path(settings.storage_root)
    previews = Path(settings.storage_previews)
    exports = Path(settings.storage_exports)

    return {
        "storage_root": str(root),
        "storage_root_exists": root.exists(),
        "storage_root_writable": os.access(root, os.W_OK) if root.exists() else False,
        "storage_previews": str(previews),
        "storage_previews_exists": previews.exists(),
        "storage_exports": str(exports),
        "storage_exports_exists": exports.exists(),
    }
