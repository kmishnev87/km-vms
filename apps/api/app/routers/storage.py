from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.routers.deps import require_permission
from app.services.recording_reconciliation import reconcile_recordings
from app.services.storage_monitoring import build_storage_monitoring_summary

router = APIRouter(prefix="/storage", tags=["storage"])


class ReconciliationRequest(BaseModel):
    mode: str = "dry_run"


@router.get("/status")
def storage_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return build_storage_monitoring_summary(db, write_audit=False, audit_actor=current_user)


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
