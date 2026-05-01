from pathlib import Path
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.routers.deps import require_permission
from app.services.recording_reconciliation import reconcile_recordings

router = APIRouter(prefix="/storage", tags=["storage"])


class ReconciliationRequest(BaseModel):
    mode: str = "dry_run"


@router.get("/status")
def storage_status(current_user: User = Depends(require_permission("manage_settings"))):
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


@router.get("/reconciliation/summary")
def storage_reconciliation_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    return reconcile_recordings(db, mode="dry_run", actor=current_user, write_audit=False)


@router.post("/reconcile")
def storage_reconcile(
    payload: ReconciliationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    mode = "apply_safe" if payload.mode == "apply_safe" else "dry_run"
    return reconcile_recordings(db, mode=mode, actor=current_user, write_audit=True)
