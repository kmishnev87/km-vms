import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.permissions import user_has_permission
from app.db.session import get_db
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.user import User
from app.routers.deps import require_permission
from app.services.archive_root_activation import (
    finalize_pending_archive_root_activation,
    request_archive_root_activation,
)
from app.services.audit_log import create_event
from app.services.recording_reconciliation import reconcile_recordings
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    apply_storage_migration,
    archive_root_public_status,
    list_archive_roots,
    migration_preview,
    resolve_segment_file_path,
    root_usage,
    sanitize_archive_root_path,
)
from app.services import setup_storage
from app.services.storage_monitoring import build_storage_monitoring_summary

router = APIRouter(prefix="/storage", tags=["storage"])


class ReconciliationRequest(BaseModel):
    mode: str = "dry_run"


class ArchiveRootCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str | None = None
    folder_name: str | None = None
    label: str | None = None
    make_active: bool = False
    confirm: bool = False


class ArchiveRootActivateRequest(BaseModel):
    confirm: bool = False


class ArchiveRootDeleteRequest(BaseModel):
    confirm: bool = False


class MigrationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_root_id: str | None = None


class MigrationApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_root_id: str | None = None
    plan_id: str | None = None
    confirm: bool = False


@router.get("/status")
def storage_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    finalize_pending_archive_root_activation(db)
    return build_storage_monitoring_summary(db, write_audit=False, audit_actor=current_user)


@router.get("/archive-roots")
def archive_roots(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    finalize_pending_archive_root_activation(db)
    return {"items": [archive_root_public_status(root, include_path=True) for root in list_archive_roots(db)]}


@router.get("/archive-roots/discovery")
def archive_root_discovery(
    current_user: User = Depends(require_permission("manage_settings")),
):
    return setup_storage.discovery_snapshot()


def _archive_root_path_from_payload(
    payload: ArchiveRootCreateRequest,
) -> tuple[str, dict | None, str | None]:
    if payload.candidate_id == "manual":
        raise ValueError("manual_archive_root_path_disabled")
    if not payload.candidate_id or not payload.folder_name:
        raise ValueError("archive_root_selection_required")
    preview = setup_storage.build_preview(
        payload.candidate_id,
        payload.folder_name,
        None,
    )
    if preview.get("blockers"):
        raise ValueError(",".join(preview.get("blockers") or []))
    return str(preview["final_host_path"]), preview, str(preview.get("selected_mount_path") or "")


def _archive_root_error_detail(exc: ValueError) -> dict:
    codes = [item.strip() for item in str(exc).split(",") if item.strip()]
    primary = codes[0] if codes else "archive_root_add_failed"
    return {
        "error": primary,
        "blockers": [{"reason": code} for code in codes],
    }


def _archive_root_add_blocked(db: Session, current_user: User, exc: ValueError) -> None:
    detail = _archive_root_error_detail(exc)
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type="archive_root.create_blocked",
        severity="warning",
        message_ru="Archive root create blocked",
        message_en="Archive root create blocked",
        target_type="archive_root",
        metadata=detail,
    )
    raise HTTPException(status_code=422, detail=detail)


@router.post("/archive-roots")
def create_archive_root(
    payload: ArchiveRootCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    try:
        selected_path, preview, allowed_base = _archive_root_path_from_payload(payload)
        root_path = sanitize_archive_root_path(selected_path, allow_create=False, allowed_base=allowed_base)
    except ValueError as exc:
        _archive_root_add_blocked(db, current_user, exc)
    existing = db.query(ArchiveRoot).filter(ArchiveRoot.root_path == str(root_path)).first()
    if existing:
        root = existing
    else:
        root = ArchiveRoot(
            id=f"root_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
            label=(payload.label or (preview or {}).get("folder_name") or root_path.name or "Archive root")[:255],
            root_path=str(root_path),
            storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
            is_active=False,
            is_readable=True,
            is_writable=True,
            is_available=False,
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
        result = request_archive_root_activation(db, root=root, actor=current_user)
        if result.get("status") in {"blocked", "already_running"}:
            raise HTTPException(status_code=409, detail=result)
        return result
    return archive_root_public_status(root, include_path=True)


def _safe_cleanup_empty_dirs(root_path: Path) -> int:
    removed = 0
    if not root_path.exists() or not root_path.is_dir():
        return removed
    for current, dirs, _files in os.walk(root_path, topdown=False):
        for dirname in dirs:
            candidate = Path(current) / dirname
            try:
                candidate.rmdir()
                removed += 1
            except OSError:
                pass
    try:
        root_path.rmdir()
        removed += 1
    except OSError:
        pass
    return removed


def _delete_inactive_root(db: Session, root: ArchiveRoot, current_user: User) -> dict:
    if root.is_active:
        raise HTTPException(status_code=409, detail={"error": "archive_root_delete_active_root_blocked"})
    if not user_has_permission(getattr(current_user, "role", ""), "delete_recordings"):
        raise HTTPException(status_code=403, detail="delete_recordings permission is required")

    writing_count = (
        db.query(RecordingSegment)
        .filter(
            RecordingSegment.archive_root_id == root.id,
            RecordingSegment.status.in_(("writing", "starting")),
            RecordingSegment.deleted_at.is_(None),
        )
        .count()
    )
    if writing_count:
        raise HTTPException(status_code=409, detail={"error": "archive_root_delete_active_writes_blocked", "writing_count": int(writing_count)})

    usage = root_usage(db, root)
    root_path = Path(root.root_path).resolve(strict=False)
    segments = (
        db.query(RecordingSegment)
        .filter(
            RecordingSegment.archive_root_id == root.id,
            RecordingSegment.deleted_at.is_(None),
            RecordingSegment.status != "deleted",
        )
        .order_by(RecordingSegment.id.asc())
        .all()
    )
    deleted_files = 0
    missing_files = 0
    skipped_files = 0
    bytes_freed = 0
    now = datetime.utcnow()
    for segment in segments:
        file_deleted = False
        try:
            path = resolve_segment_file_path(db, segment).resolve(strict=False)
            try:
                path.relative_to(root_path)
            except ValueError:
                skipped_files += 1
                path = None
            if path is not None and path.exists() and path.is_file():
                size = int(path.stat().st_size)
                path.unlink()
                deleted_files += 1
                bytes_freed += size
                file_deleted = True
            elif path is not None:
                missing_files += 1
        except Exception:
            skipped_files += 1
        segment.status = "deleted"
        segment.deleted_at = now
        segment.deletion_reason = "archive_root_deleted"
        segment.deleted_by = getattr(current_user, "username", None)
        segment.deletion_source = "archive_root_delete"
        segment.archive_root_id = None
        segment.updated_at = now
        if file_deleted and not segment.size_bytes:
            segment.size_bytes = 0
        db.add(segment)

    root_id = root.id
    root_label = root.label
    db.delete(root)
    db.commit()
    removed_dirs = _safe_cleanup_empty_dirs(root_path)
    result = {
        "ok": True,
        "root_id": root_id,
        "root_label": root_label,
        "segments_deleted": len(segments),
        "files_deleted": deleted_files,
        "missing_files": missing_files,
        "skipped_files": skipped_files,
        "bytes_freed": bytes_freed,
        "removed_empty_dirs": removed_dirs,
        "previous_usage": usage,
    }
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type="archive_root.deleted",
        severity="warning",
        message_ru="Archive root deleted",
        message_en="Archive root deleted",
        target_type="archive_root",
        target_id=root_id,
        target_name=root_label,
        metadata=result,
    )
    return result


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
    result = request_archive_root_activation(db, root=root, actor=current_user)
    if result.get("status") == "blocked":
        raise HTTPException(status_code=409, detail=result)
    if result.get("status") == "already_running":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.delete("/archive-roots/{root_id}")
def delete_archive_root(
    root_id: str,
    payload: ArchiveRootDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    if not payload.confirm:
        raise HTTPException(status_code=409, detail="Archive root deletion requires confirm=true")
    root = db.get(ArchiveRoot, root_id)
    if not root:
        raise HTTPException(status_code=404, detail="Archive root not found")
    return _delete_inactive_root(db, root, current_user)


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


@router.post("/migration/apply")
def storage_migration_apply(
    payload: MigrationApplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    if not payload.confirm:
        create_event(
            db=db,
            actor=current_user,
            category="storage",
            event_type="archive_migration.apply_blocked",
            severity="warning",
            message_ru="Archive migration apply blocked",
            message_en="Archive migration apply blocked",
            target_type="archive_migration",
            metadata={"reason": "confirm_required"},
        )
        raise HTTPException(status_code=409, detail={"error": "archive_migration_apply_requires_confirm"})
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type="archive_migration.apply_started",
        severity="warning",
        message_ru="Archive migration apply started",
        message_en="Archive migration apply started",
        target_type="archive_migration",
        metadata={"target_root_id": payload.target_root_id, "plan_id": payload.plan_id},
    )
    result = apply_storage_migration(db, target_root_id=payload.target_root_id, expected_plan_id=payload.plan_id)
    event_type = "archive_migration.apply_completed" if result["status"] == "completed" else "archive_migration.apply_blocked" if result["status"] == "blocked" else "archive_migration.apply_failed"
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type=event_type,
        severity="info" if result["status"] == "completed" else "warning",
        message_ru="Archive migration apply finished",
        message_en="Archive migration apply finished",
        target_type="archive_migration",
        metadata={
            "status": result["status"],
            "target_root_id": result.get("target_root_id"),
            "plan_id": result.get("plan_id"),
            "executed_count": len(result.get("executed") or []),
            "blocker_count": len(result.get("blockers") or []),
            "source_preserved": bool(result.get("source_preserved")),
            "cleanup_pending": bool(result.get("cleanup_pending")),
        },
    )
    if result["status"] == "blocked":
        raise HTTPException(status_code=409, detail=result)
    if result["status"] == "failed":
        raise HTTPException(status_code=500, detail=result)
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
