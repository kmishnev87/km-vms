from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.recording import ArchiveRoot, RecordingJob
from app.models.user import User
from app.routers.deps import require_permission
from app.services.audit_log import create_event
from app.services.recording_reconciliation import reconcile_recordings
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    archive_root_public_status,
    list_archive_roots,
    migration_preview,
    sanitize_archive_root_path,
)
from app.services.storage_monitoring import build_storage_monitoring_summary

router = APIRouter(prefix="/storage", tags=["storage"])


class ReconciliationRequest(BaseModel):
    mode: str = "dry_run"


class ArchiveRootValidateRequest(BaseModel):
    root_path: str
    create_namespace: bool = False


class ArchiveRootCreateRequest(BaseModel):
    root_path: str
    label: str | None = None
    make_active: bool = False
    confirm: bool = False


class ArchiveRootActivateRequest(BaseModel):
    confirm: bool = False


class MigrationPreviewRequest(BaseModel):
    target_root_id: str | None = None


@router.get("/status")
def storage_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return build_storage_monitoring_summary(db, write_audit=False, audit_actor=current_user)


@router.get("/archive-roots")
def archive_roots(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return {"items": [archive_root_public_status(root, include_path=True) for root in list_archive_roots(db)]}


@router.post("/archive-roots/validate")
def validate_archive_root(
    payload: ArchiveRootValidateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type="archive_root.validate_started",
        severity="info",
        message_ru="Archive root validation started",
        message_en="Archive root validation started",
        target_type="archive_root",
        metadata={"create_namespace": bool(payload.create_namespace)},
    )
    try:
        root_path = sanitize_archive_root_path(payload.root_path, allow_create=payload.create_namespace)
        result = archive_root_public_status(
            ArchiveRoot(
                id="candidate",
                label="Candidate archive root",
                root_path=str(root_path),
                storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
                is_active=False,
            ),
            include_path=True,
        )
        result["ok"] = bool(result["is_available"] and (result["namespace_exists"] or not payload.create_namespace))
        result["preview_only"] = True
        event_type = "archive_root.validate_completed" if result["ok"] else "archive_root.validate_failed"
        create_event(
            db=db,
            actor=current_user,
            category="storage",
            event_type=event_type,
            severity="info" if result["ok"] else "warning",
            message_ru="Archive root validation completed",
            message_en="Archive root validation completed",
            target_type="archive_root",
            metadata={"ok": result["ok"], "problem": result.get("problem")},
        )
        return result
    except ValueError as exc:
        create_event(
            db=db,
            actor=current_user,
            category="storage",
            event_type="archive_root.validate_failed",
            severity="warning",
            message_ru="Archive root validation failed",
            message_en="Archive root validation failed",
            target_type="archive_root",
            metadata={"error": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/archive-roots")
def create_archive_root(
    payload: ArchiveRootCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    root_path = sanitize_archive_root_path(payload.root_path, allow_create=True)
    existing = db.query(ArchiveRoot).filter(ArchiveRoot.root_path == str(root_path)).first()
    if existing:
        root = existing
    else:
        root = ArchiveRoot(
            id=f"root_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
            label=(payload.label or root_path.name or "Archive root")[:255],
            root_path=str(root_path),
            storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
            is_active=False,
            is_readable=True,
            is_writable=True,
            is_available=True,
            last_seen_at=datetime.utcnow(),
        )
        db.add(root)
        db.commit()
        db.refresh(root)
        create_event(
            db=db,
            actor=current_user,
            category="storage",
            event_type="archive_root.created",
            severity="info",
            message_ru="Archive root created",
            message_en="Archive root created",
            target_type="archive_root",
            target_id=root.id,
            target_name=root.label,
            metadata={"root_id": root.id, "label": root.label},
        )
    if payload.make_active:
        if not payload.confirm:
            raise HTTPException(status_code=409, detail="Archive root activation requires confirm=true")
        _activate_root(db, root, current_user)
    return archive_root_public_status(root, include_path=True)


def _activate_root(db: Session, root: ArchiveRoot, current_user: User) -> None:
    active_jobs = db.query(RecordingJob).filter(RecordingJob.state.in_(("starting", "recording", "stopping", "restarting"))).count()
    if active_jobs:
        raise HTTPException(status_code=409, detail={"error": "active_recording_jobs_block_root_switch", "active_jobs_count": int(active_jobs)})
    status = archive_root_public_status(root, include_path=True)
    if not status["is_available"] or not status["is_writable"] or not status["namespace_exists"]:
        raise HTTPException(status_code=409, detail={"error": "archive_root_not_writable_or_namespace_missing", "status": status})
    previous = db.query(ArchiveRoot).filter(ArchiveRoot.is_active == True).all()  # noqa: E712
    previous_ids = [item.id for item in previous]
    for item in previous:
        item.is_active = False
        item.updated_at = datetime.utcnow()
        db.add(item)
    root.is_active = True
    root.updated_at = datetime.utcnow()
    db.add(root)
    db.commit()
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type="archive_root.activated",
        severity="warning",
        message_ru="Archive root activated",
        message_en="Archive root activated",
        target_type="archive_root",
        target_id=root.id,
        target_name=root.label,
        metadata={"previous_root_ids": previous_ids, "active_root_id": root.id, "active_jobs_blocked": active_jobs},
    )


@router.post("/archive-roots/{root_id}/activate")
def activate_archive_root(
    root_id: str,
    payload: ArchiveRootActivateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    if not payload.confirm:
        raise HTTPException(status_code=409, detail="Archive root activation requires confirm=true")
    root = db.get(ArchiveRoot, root_id)
    if not root:
        raise HTTPException(status_code=404, detail="Archive root not found")
    _activate_root(db, root, current_user)
    return archive_root_public_status(root, include_path=True)


@router.post("/migration/preview")
def storage_migration_preview(
    payload: MigrationPreviewRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type="archive_migration.preview_started",
        severity="info",
        message_ru="Archive migration preview started",
        message_en="Archive migration preview started",
        target_type="archive_migration",
        metadata={"target_root_id": payload.target_root_id if payload else None},
    )
    result = migration_preview(db, target_root_id=payload.target_root_id if payload else None)
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type="archive_migration.preview_completed",
        severity="warning" if result.get("blockers") else "info",
        message_ru="Archive migration preview completed",
        message_en="Archive migration preview completed",
        target_type="archive_migration",
        metadata={
            "target_root_id": result.get("target_root_id"),
            "total_would_move_count": result.get("total_would_move_count"),
            "total_would_move_bytes": result.get("total_would_move_bytes"),
            "blocker_count": len(result.get("blockers") or []),
        },
    )
    return result


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
