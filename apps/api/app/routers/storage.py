from datetime import datetime
import os
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.permissions import user_has_permission
from app.db.session import get_db
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.user import User
from app.routers.deps import require_permission
from app.services.archive_root_activation import (
    ArchiveRootMutationConflict,
    archive_root_mutation_guard,
    archive_root_activation_public_status,
    read_pending_archive_root_activation,
    request_archive_root_activation,
)
from app.services.audit_log import create_event
from app.services.recording_operations import (
    DestructiveScopeConflict,
    destructive_scope_guard,
    new_operation_id,
)
from app.services.archive_integrity import (
    cancel_integrity_scan,
    get_integrity_scan,
    latest_integrity_scan,
    legacy_reconciliation_summary,
    list_integrity_findings,
    start_integrity_scan,
)
from app.services.archive_integrity_remediation import (
    IntegrityRemediationBlocked,
    apply_remediation_plan,
    create_remediation_plan,
    get_remediation_plan,
)
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    apply_storage_migration,
    archive_root_public_status,
    archive_root_runtime_path,
    list_archive_roots,
    migration_preview,
    resolve_segment_file_path,
    root_usage,
    sanitize_archive_root_path,
    segment_has_resolved_archive_root,
    verify_archive_root_access,
    write_archive_roots_runtime_files,
)
from app.services import setup_storage
from app.services.storage_monitoring import build_lightweight_storage_monitoring_summary
from app.services.storage_operation_conflicts import (
    StorageOperationLifecycle,
    StorageOperationConflict as StorageOuterConflict,
    claim_state_detail,
    claim_operation_with_conflicts,
    operation_instance_id,
    terminal_replay_result,
)
from app.services.storage_operations_foundation import (
    OperationHeartbeatController,
    StorageOperationContractError,
    safe_reason_code,
)

router = APIRouter(prefix="/storage", tags=["storage"])

ROOT_DELETION_RETRY_STATUSES = {
    "deleting",
    "partial_deletion",
    "partial_cleanup",
    "partial_finalization",
}
ROOT_CLEANUP_EVIDENCE_STATUSES = {"partial_cleanup", "partial_finalization"}
OPERATION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$"


class ReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "dry_run"
    operation_id: str | None = Field(default=None, max_length=96, pattern=OPERATION_ID_PATTERN)


class IntegrityScanStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.:-]{7,63}$")


class IntegrityPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_key: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_]{2,63}$")
    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.:-]{7,63}$")


class IntegrityApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    operation_id: str = Field(min_length=8, max_length=96, pattern=OPERATION_ID_PATTERN)


class ArchiveRootCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str | None = None
    discovery_snapshot_id: str | None = None
    folder_name: str | None = None
    label: str | None = None
    make_active: bool = False
    confirm: bool = False


class ArchiveRootActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    recovery: bool = False


class ArchiveRootDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    operation_id: str | None = Field(default=None, max_length=96, pattern=OPERATION_ID_PATTERN)


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
    summary = build_lightweight_storage_monitoring_summary(db)
    summary["archive_root_activation"] = archive_root_activation_public_status()
    if isinstance(summary.get("storage_operations"), dict):
        summary["storage_operations"]["archive_root_activation"] = summary["archive_root_activation"]
    return summary


@router.get("/archive-roots")
def archive_roots(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return {"items": [archive_root_public_status(root, include_path=True) for root in list_archive_roots(db)]}


@router.get("/archive-roots/discovery")
def archive_root_discovery(
    current_user: User = Depends(require_permission("manage_settings")),
):
    return setup_storage.request_discovery_refresh()


def _archive_root_path_from_payload(
    payload: ArchiveRootCreateRequest,
) -> tuple[str, dict | None, str | None]:
    if payload.candidate_id == "manual":
        raise ValueError("manual_archive_root_path_disabled")
    if not payload.candidate_id or not payload.discovery_snapshot_id or not payload.folder_name:
        raise ValueError("archive_root_selection_required")
    preview = setup_storage.revalidate_discovery_candidate(
        payload.candidate_id,
        payload.discovery_snapshot_id,
        payload.folder_name,
    )
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
    if payload.make_active and not payload.confirm:
        raise HTTPException(status_code=409, detail={"error": "archive_root_activation_confirm_required"})
    try:
        with archive_root_mutation_guard("archive_root_create") as mutation_owner:
            try:
                selected_path, preview, allowed_base = _archive_root_path_from_payload(payload)
                root_path = sanitize_archive_root_path(selected_path, allow_create=False, allowed_base=allowed_base)
            except ValueError as exc:
                _archive_root_add_blocked(db, current_user, exc)

            physical_identity = str((preview or {}).get("physical_identity") or "").strip()
            if not physical_identity:
                _archive_root_add_blocked(db, current_user, ValueError("storage_candidate_identity_unavailable"))
            canonical_path = root_path.resolve(strict=False)
            existing = None
            for candidate in db.query(ArchiveRoot).order_by(ArchiveRoot.created_at.asc()).all():
                if Path(str(candidate.root_path)).resolve(strict=False) == canonical_path:
                    existing = candidate
                    break

            if existing and existing.physical_identity and existing.physical_identity != physical_identity:
                _archive_root_add_blocked(db, current_user, ValueError("root_identity_conflict"))
            if existing and existing.retirement_status in {"deleting", "partial_deletion", "partial_cleanup", "partial_finalization"}:
                _archive_root_add_blocked(db, current_user, ValueError("retired_root_partial_deletion_requires_retry"))

            reactivated = bool(existing and existing.retired_at is not None)
            if existing:
                root = existing
                root.physical_identity = physical_identity
                if reactivated:
                    root.retired_at = None
                    root.retirement_status = None
                    root.retirement_problem = None
                    root.retirement_operation_id = None
                    root.retirement_cleanup_status = None
                    root.retirement_cleanup_result = None
                    root.is_active = False
                root.updated_at = datetime.utcnow()
                db.add(root)
            else:
                root = ArchiveRoot(
                    id=f"root_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
                    label=(payload.label or (preview or {}).get("folder_name") or root_path.name or "Archive root")[:255],
                    root_path=str(canonical_path),
                    storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
                    is_active=False,
                    is_readable=bool((preview or {}).get("exists")),
                    is_writable=bool((preview or {}).get("writable")),
                    is_available=bool((preview or {}).get("exists")),
                    last_seen_at=datetime.utcnow(),
                    physical_identity=physical_identity,
                )
                db.add(root)
            db.commit()
            db.refresh(root)
            write_archive_roots_runtime_files(db)
            create_event(
                db=db,
                actor=current_user,
                category="storage",
                event_type="archive_root.reactivated" if reactivated else "archive_root.created",
                severity="info",
                message_ru="Archive root reactivated" if reactivated else "Archive root created",
                message_en="Archive root reactivated" if reactivated else "Archive root created",
                target_type="archive_root",
                target_id=root.id,
                target_name=root.label,
                metadata={"root_id": root.id, "label": root.label, "identity_reused": reactivated},
            )
            db.commit()
            if payload.make_active:
                result = request_archive_root_activation(
                    db,
                    root=root,
                    actor=current_user,
                    mutation_owner=mutation_owner,
                )
                if result.get("status") in {"blocked", "already_running"}:
                    raise HTTPException(status_code=409, detail=result)
                return result
            return archive_root_public_status(root, include_path=True)
    except ArchiveRootMutationConflict as exc:
        raise HTTPException(status_code=409, detail=exc.blocker) from exc


def _safe_cleanup_empty_dirs(root_path: Path) -> int:
    removed = 0
    namespace_path = (root_path / KMVMS_RECORDINGS_NAMESPACE).resolve(strict=False)
    try:
        namespace_path.relative_to(root_path.resolve(strict=False))
    except ValueError:
        return removed
    if not namespace_path.exists() or not namespace_path.is_dir():
        return removed
    for current, dirs, _files in os.walk(namespace_path, topdown=False):
        for dirname in dirs:
            candidate = Path(current) / dirname
            try:
                candidate.rmdir()
                removed += 1
            except OSError:
                pass
    try:
        namespace_path.rmdir()
        removed += 1
    except OSError:
        pass
    kmvms_path = namespace_path.parent
    try:
        kmvms_path.rmdir()
        removed += 1
    except OSError:
        pass
    return removed


def _mark_root_segment_deleted(db: Session, segment: RecordingSegment, current_user: User, *, now: datetime) -> None:
    segment.status = "deleted"
    segment.deleted_at = now
    segment.deletion_reason = "archive_root_retired"
    segment.deleted_by = getattr(current_user, "username", None)
    segment.deletion_source = "archive_root_delete"
    segment.updated_at = now
    db.add(segment)


def _recover_root_segment_metadata_after_file_delete(
    db: Session,
    *,
    segment_id: int,
    current_user: User,
    now: datetime,
) -> bool:
    try:
        db.rollback()
        fresh = db.get(RecordingSegment, segment_id)
        if fresh is None:
            return False
        _mark_root_segment_deleted(db, fresh, current_user, now=now)
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def _delete_inactive_root(
    db: Session,
    root: ArchiveRoot,
    current_user: User,
    *,
    operation_id: str | None = None,
    operation_heartbeat: Callable[[], None] | None = None,
) -> dict:
    if root.is_active:
        raise HTTPException(status_code=409, detail={"error": "archive_root_delete_active_root_blocked"})
    if not user_has_permission(getattr(current_user, "role", ""), "delete_recordings"):
        raise HTTPException(status_code=403, detail="delete_recordings permission is required")
    previous_status = str(getattr(root, "retirement_status", None) or "")
    previous_operation_id = str(getattr(root, "retirement_operation_id", None) or "")
    operation_id = operation_id or (
        previous_operation_id
        if previous_status in ROOT_DELETION_RETRY_STATUSES and previous_operation_id
        else new_operation_id("archive-root-cleanup")
    )
    camera_ids = [
        int(camera_id)
        for (camera_id,) in (
            db.query(RecordingSegment.camera_id)
            .filter(
                RecordingSegment.archive_root_id == root.id,
                RecordingSegment.deleted_at.is_(None),
                RecordingSegment.status != "deleted",
            )
            .distinct()
            .all()
        )
        if camera_id is not None
    ]
    scope = {
        "type": "root",
        "segment_ids": [],
        "camera_ids": camera_ids,
        "root_ids": [root.id],
    }
    try:
        with destructive_scope_guard(operation_id, scope, purpose="archive_root_delete") as scope_lease:
            kwargs = {"operation_id": operation_id, "scope_lease": scope_lease}
            if operation_heartbeat is not None:
                kwargs["operation_heartbeat"] = operation_heartbeat
            return _delete_inactive_root_owned(db, root, current_user, **kwargs)
    except DestructiveScopeConflict as exc:
        reason = str(exc.detail.get("reason") or "destructive_scope_conflict")
        raise HTTPException(
            status_code=409,
            detail={
                "error": reason,
                "status": "blocked",
                "operation_id": operation_id,
                **setup_storage.archive_root_cleanup_capability(reason, "partial_cleanup"),
            },
        ) from exc


def _delete_inactive_root_owned(
    db: Session,
    root: ArchiveRoot,
    current_user: User,
    *,
    operation_id: str,
    scope_lease,
    operation_heartbeat: Callable[[], None] | None = None,
) -> dict:
    if root.is_active:
        raise HTTPException(status_code=409, detail={"error": "archive_root_delete_active_root_blocked"})
    if not user_has_permission(getattr(current_user, "role", ""), "delete_recordings"):
        raise HTTPException(status_code=403, detail="delete_recordings permission is required")

    previous_retirement_status = str(root.retirement_status or "")
    previous_retirement_operation_id = str(root.retirement_operation_id or "")
    previous_cleanup = (
        dict(root.retirement_cleanup_result)
        if (
            previous_retirement_status in ROOT_CLEANUP_EVIDENCE_STATUSES
            and previous_retirement_operation_id == operation_id
            and isinstance(root.retirement_cleanup_result, dict)
            and str(root.retirement_cleanup_result.get("operation_id") or "") == operation_id
        )
        else {}
    )

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
    usage = root_usage(db, root)
    root_path = archive_root_runtime_path(root).resolve(strict=False)
    namespace_path = (root_path / KMVMS_RECORDINGS_NAMESPACE).resolve(strict=False)
    if segments:
        access = verify_archive_root_access(root, require_write=True)
        if not access.get("verified") or access.get("read_access_state") != "available" or access.get("write_access_state") != "available":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "archive_root_delete_unavailable_root_blocked",
                    "problem": access.get("verification_error") or access.get("problem") or "archive_root_unavailable",
                },
            )
        try:
            namespace_path.relative_to(root_path)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"error": "archive_root_namespace_boundary_invalid"}) from exc

    plans: list[dict] = []
    blockers: list[dict] = []
    for segment in segments:
        if segment.ownership != "KM VMS" or segment.source != "recorder":
            blockers.append({"reason": "archive_root_contains_non_kmvms_metadata", "segment_id": int(segment.id)})
            continue
        if not segment_has_resolved_archive_root(segment):
            blockers.append({"reason": "archive_root_segment_unresolved", "segment_id": int(segment.id)})
            continue
        try:
            path = resolve_segment_file_path(db, segment).resolve(strict=False)
            path.relative_to(namespace_path)
            try:
                stat = path.stat()
                if not path.is_file():
                    blockers.append({"reason": "archive_root_segment_not_regular_file", "segment_id": int(segment.id)})
                    continue
                if not os.access(path.parent, os.W_OK):
                    blockers.append({"reason": "archive_root_segment_parent_not_writable", "segment_id": int(segment.id)})
                    continue
                plans.append({"segment": segment, "path": path, "exists": True, "size_bytes": int(stat.st_size)})
            except FileNotFoundError:
                plans.append({"segment": segment, "path": path, "exists": False, "size_bytes": 0})
        except (OSError, ValueError) as exc:
            blockers.append({"reason": "archive_root_segment_preflight_failed", "segment_id": int(segment.id), "type": type(exc).__name__})
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "archive_root_delete_preflight_blocked",
                "blocker_count": len(blockers),
                "blockers": blockers[:20],
                "mutation_performed": False,
            },
        )

    root.retirement_operation_id = operation_id
    root.retirement_status = "deleting"
    root.retirement_problem = None
    db.add(root)
    db.commit()

    deleted_files = 0
    missing_files = 0
    bytes_freed = 0
    metadata_deleted = 0
    metadata_recovered = 0
    failed: list[dict] = []
    now = datetime.utcnow()
    for plan in plans:
        if operation_heartbeat is not None:
            operation_heartbeat()
        segment = plan["segment"]
        scope_lease.touch()
        try:
            with scope_lease.mutation_guard():
                try:
                    if plan["exists"]:
                        try:
                            plan["path"].unlink()
                            deleted_files += 1
                            bytes_freed += int(plan["size_bytes"])
                        except FileNotFoundError:
                            missing_files += 1
                    else:
                        missing_files += 1
                except OSError as exc:
                    db.rollback()
                    failed.append({"segment_id": int(segment.id), "reason": "filesystem_delete_failed", "type": type(exc).__name__})
                    break
                try:
                    _mark_root_segment_deleted(db, segment, current_user, now=now)
                    db.commit()
                    metadata_deleted += 1
                except Exception:
                    if _recover_root_segment_metadata_after_file_delete(
                        db,
                        segment_id=int(segment.id),
                        current_user=current_user,
                        now=now,
                    ):
                        metadata_deleted += 1
                        metadata_recovered += 1
                        continue
                    failed.append({"segment_id": int(segment.id), "reason": "metadata_update_failed_after_file_delete"})
                    break
        except (DestructiveScopeConflict, OSError):
            db.rollback()
            if not failed:
                failed.append(
                    {
                        "segment_id": int(segment.id),
                        "reason": "destructive_scope_lease_lost",
                    }
                )
            break

    root_id = root.id
    root_label = root.label
    if failed:
        failure_reason = str(failed[0].get("reason") or "archive_root_delete_failed")
        retry_capability = setup_storage.archive_root_cleanup_capability(failure_reason, "not_started")
        root.retirement_status = "partial_deletion"
        root.retirement_problem = failure_reason
        root.retirement_cleanup_status = "not_started"
        root.retirement_cleanup_result = {
            "operation_id": operation_id,
            "cleanup_status": "not_started",
            "reason": failure_reason,
            **retry_capability,
        }
        root.updated_at = datetime.utcnow()
        db.add(root)
        db.commit()
        result = {
            "ok": False,
            "status": "partial",
            "operation_id": operation_id,
            "cleanup_status": "not_started",
            "root_id": root_id,
            "segments_deleted": metadata_deleted,
            "metadata_recovered_count": metadata_recovered,
            "files_deleted": deleted_files,
            "confirmed_missing_files": missing_files,
            "failed_count": len(failed),
            "remaining_count": max(0, len(plans) - metadata_deleted),
            "bytes_freed": bytes_freed,
            "reason": failure_reason,
            **retry_capability,
            "failures": failed,
        }
        create_event(
            db=db,
            actor=current_user,
            category="storage",
            event_type="archive_root.delete_partial",
            severity="error",
            message_ru="Archive root deletion completed partially",
            message_en="Archive root deletion completed partially",
            target_type="archive_root",
            target_id=root_id,
            target_name=root_label,
            metadata=result,
            commit=False,
        )
        db.commit()
        return result

    removed_empty_dirs = _safe_cleanup_empty_dirs(root_path) if segments else 0
    scope_lease.touch()
    try:
        cleanup = setup_storage.request_archive_root_cleanup(
            root,
            operation_id=operation_id,
            marker_already_removed=bool(previous_cleanup.get("marker_removed")),
        )
    except ValueError as exc:
        cleanup = {
            "status": "failed",
            "cleanup_status": "failed_preflight",
            "reason": str(exc) or "archive_root_cleanup_preflight_failed",
            "marker_removed": bool(previous_cleanup.get("marker_removed")),
            "root_directory_removed": False,
            "root_directory_preserved_reason": "",
        }
    cleanup = setup_storage.normalize_archive_root_cleanup_result(cleanup)
    cleanup_facts = {
        "operation_id": operation_id,
        "cleanup_status": str(cleanup.get("cleanup_status") or "partial_cleanup"),
        "reason": str(cleanup.get("reason") or "")[:120] or None,
        "marker_removed": bool(cleanup.get("marker_removed")),
        "root_directory_removed": bool(cleanup.get("root_directory_removed")),
        "root_directory_preserved_reason": str(cleanup.get("root_directory_preserved_reason") or "")[:120] or None,
        "retry_mode": str(cleanup.get("retry_mode") or "none")[:32],
        "next_action": str(cleanup.get("next_action") or "close")[:64],
        "retry_available": bool(cleanup.get("retry_available")),
    }
    cleanup_completed = cleanup_facts["cleanup_status"] in {
        "completed_removed",
        "completed_preserved_nonempty",
    }
    root.retirement_cleanup_status = cleanup_facts["cleanup_status"]
    root.retirement_cleanup_result = cleanup_facts
    if not cleanup_completed:
        root.retired_at = None
        root.retirement_status = "partial_cleanup"
        root.retirement_problem = cleanup_facts["reason"] or "archive_root_cleanup_incomplete"
        root.updated_at = datetime.utcnow()
        db.add(root)
        db.commit()
        result = {
            "ok": False,
            "status": "partial",
            "operation_id": operation_id,
            "root_id": root_id,
            "segments_deleted": metadata_deleted,
            "metadata_recovered_count": metadata_recovered,
            "files_deleted": deleted_files,
            "confirmed_missing_files": missing_files,
            "failed_count": 1,
            "remaining_count": 0,
            "bytes_freed": bytes_freed,
            "removed_empty_dirs": removed_empty_dirs,
            **cleanup_facts,
        }
        create_event(
            db=db,
            actor=current_user,
            category="storage",
            event_type="archive_root.delete_partial",
            severity="error",
            message_ru="Archive root host cleanup completed partially",
            message_en="Archive root host cleanup completed partially",
            target_type="archive_root",
            target_id=root_id,
            target_name=root_label,
            metadata=result,
            commit=False,
        )
        db.commit()
        return result

    root.retired_at = datetime.utcnow()
    root.retirement_status = "completed"
    root.retirement_problem = None
    root.is_active = False
    root.updated_at = datetime.utcnow()
    db.add(root)
    result = {
        "ok": True,
        "operation_id": operation_id,
        "root_id": root_id,
        "root_label": root_label,
        "status": "completed",
        "segments_deleted": metadata_deleted,
        "metadata_recovered_count": metadata_recovered,
        "files_deleted": deleted_files,
        "confirmed_missing_files": missing_files,
        "failed_count": 0,
        "bytes_freed": bytes_freed,
        "removed_empty_dirs": removed_empty_dirs,
        "previous_usage": usage,
        "historical_root_identity_preserved": True,
        **cleanup_facts,
    }
    create_event(
        db=db,
        actor=current_user,
        category="storage",
        event_type="archive_root.retired",
        severity="warning",
        message_ru="Archive root retired after recording deletion",
        message_en="Archive root retired after recording deletion",
        target_type="archive_root",
        target_id=root_id,
        target_name=root_label,
        metadata=result,
        commit=False,
    )
    try:
        # Keep DB retirement, audit truth and generated runtime selection in one
        # recoverable finalization boundary. The runtime files are atomic and
        # are generated from this transaction's flushed retirement state.
        db.flush()
        write_archive_roots_runtime_files(db)
        db.commit()
    except Exception as exc:
        db.rollback()
        fresh = db.get(ArchiveRoot, root_id)
        if fresh is None or fresh.retired_at is None:
            if fresh is None:
                raise
            fresh.retired_at = None
            fresh.retirement_status = "partial_finalization"
            fresh.retirement_problem = "runtime_state_finalize_failed"
            fresh.is_active = False
            fresh.updated_at = datetime.utcnow()
            db.add(fresh)
            db.commit()
            runtime_recovery_failed = False
            try:
                write_archive_roots_runtime_files(db)
            except Exception:
                runtime_recovery_failed = True
                fresh.retirement_problem = "runtime_manifest_recovery_failed"
                fresh.updated_at = datetime.utcnow()
                db.add(fresh)
                db.commit()
            partial = {
                **result,
                "ok": False,
                "status": "partial",
                "failed_count": 1,
                "remaining_count": 0,
                **setup_storage.archive_root_cleanup_capability(
                    fresh.retirement_problem,
                    "partial_finalization",
                ),
                "finalization_pending": True,
                "failure": {
                    "reason": fresh.retirement_problem,
                    "type": type(exc).__name__,
                    "runtime_manifest_recovery_failed": runtime_recovery_failed,
                },
            }
            create_event(
                db=db,
                actor=current_user,
                category="storage",
                event_type="archive_root.delete_partial",
                severity="error",
                message_ru="Archive root deletion finalization completed partially",
                message_en="Archive root deletion finalization completed partially",
                target_type="archive_root",
                target_id=root_id,
                target_name=root_label,
                metadata=partial,
                commit=False,
            )
            db.commit()
            return partial
        root = fresh

    return result


@router.post("/archive-roots/{root_id}/activate")
def activate_archive_root(
    root_id: str,
    payload: ArchiveRootActivateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    if not payload.confirm:
        raise HTTPException(status_code=409, detail={"error": "archive_root_activation_confirm_required"})
    root = db.get(ArchiveRoot, root_id)
    if not root:
        raise HTTPException(status_code=404, detail="Archive root not found")
    pending = read_pending_archive_root_activation()
    if pending is not None or payload.recovery:
        try:
            result = request_archive_root_activation(db, root=root, actor=current_user, recovery=payload.recovery)
        except ArchiveRootMutationConflict as exc:
            raise HTTPException(status_code=409, detail=exc.blocker) from exc
        if result.get("status") in {"blocked", "already_running"}:
            raise HTTPException(status_code=409, detail=result)
        return result

    previous_root = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.is_active == True, ArchiveRoot.retired_at.is_(None))  # noqa: E712
        .order_by(ArchiveRoot.updated_at.desc(), ArchiveRoot.id.asc())
        .first()
    )
    if previous_root is not None and str(previous_root.id) == str(root.id):
        return request_archive_root_activation(db, root=root, actor=current_user, recovery=False)

    operation_id = new_operation_id("archive-root-activation")
    try:
        outer_claim = claim_operation_with_conflicts(
            db,
            operation_type="archive_root_activation",
            scope={
                "global": True,
                "physical_volume_ids": [],
                "root_ids": [
                    value
                    for value in (getattr(previous_root, "id", None), root.id)
                    if value is not None
                ],
                "camera_ids": [],
                "segment_ids": [],
            },
            request_identity={
                "operation_id": operation_id,
                "previous_root_id": getattr(previous_root, "id", None),
                "target_root_id": root.id,
            },
            actor=current_user,
            operation_id=operation_id,
            idempotency_key=operation_id,
            owner_instance_id=operation_instance_id("archive-root-activation"),
        )
    except StorageOuterConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    if outer_claim.get("state") == "terminal":
        replay = terminal_replay_result(outer_claim)
        if replay.get("status") in {"blocked", "failed", "partial"}:
            raise HTTPException(status_code=409, detail=replay)
        return replay
    if outer_claim.get("state") != "claimed":
        raise HTTPException(status_code=409, detail=claim_state_detail(outer_claim))
    outer_handle = outer_claim["handle"]
    with StorageOperationLifecycle(
        db,
        outer_handle,
        failure_reason="archive_root_activation_scheduling_failed",
    ) as lifecycle:
        try:
            result = request_archive_root_activation(
                db,
                root=root,
                actor=current_user,
                recovery=False,
                operation_id=operation_id,
                outer_handle=outer_handle,
            )
        except ArchiveRootMutationConflict as exc:
            lifecycle.block(
                safe_reason_code(exc.blocker, fallback="archive_root_mutation_conflict"),
                retry_allowed=True,
            )
            raise HTTPException(status_code=409, detail=exc.blocker) from exc
        if result.get("status") == "blocked":
            lifecycle.block(
                safe_reason_code(result, fallback="archive_root_activation_blocked"),
                retry_allowed=False,
            )
            raise HTTPException(status_code=409, detail=result)
        if result.get("status") == "already_running":
            lifecycle.block("archive_root_activation_already_running", retry_allowed=True)
            raise HTTPException(status_code=409, detail=result)
        if result.get("status") == "already_active":
            lifecycle.finish(status="completed", result={"status": "completed"})
        elif result.get("status") in {"queued", "running"}:
            lifecycle.defer_to_async_worker()
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
    if not root or (root.retired_at is not None and not payload.operation_id):
        raise HTTPException(status_code=404, detail="Archive root not found")
    previous_status = str(root.retirement_status or "")
    operation_id = payload.operation_id or new_operation_id("archive-root-cleanup")
    parent_operation_id = (
        str(root.retirement_operation_id)
        if (
            previous_status in ROOT_DELETION_RETRY_STATUSES
            and root.retirement_operation_id
            and str(root.retirement_operation_id) != operation_id
        )
        else None
    )
    camera_ids = [
        int(value)
        for (value,) in db.query(RecordingSegment.camera_id)
        .filter(RecordingSegment.archive_root_id == root.id, RecordingSegment.deleted_at.is_(None))
        .distinct()
        .all()
        if value is not None
    ]
    try:
        outer_claim = claim_operation_with_conflicts(
            db,
            operation_type="archive_root_delete",
            scope={
                "global": False,
                "physical_volume_ids": [root.physical_identity] if root.physical_identity else [],
                "root_ids": [root.id],
                "camera_ids": camera_ids,
                "segment_ids": [],
            },
            request_identity={"root_id": root.id, "operation_id": operation_id},
            actor=current_user,
            operation_id=operation_id,
            idempotency_key=operation_id,
            parent_operation_id=parent_operation_id,
            owner_instance_id=operation_instance_id("archive-root-delete"),
        )
    except StorageOuterConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    if outer_claim.get("state") == "terminal":
        replay = terminal_replay_result(outer_claim)
        if replay.get("status") in {"blocked", "partial"}:
            raise HTTPException(status_code=409, detail=replay)
        if replay.get("status") == "failed":
            raise HTTPException(status_code=500, detail=replay)
        return replay
    if outer_claim.get("state") != "claimed":
        raise HTTPException(status_code=409, detail=claim_state_detail(outer_claim))
    outer_handle = outer_claim["handle"]
    outer_heartbeat = OperationHeartbeatController(db.get_bind(), outer_handle)
    with StorageOperationLifecycle(db, outer_handle, failure_reason="archive_root_delete_failed") as lifecycle:
        try:
            with archive_root_mutation_guard("archive_root_delete"):
                root = db.get(ArchiveRoot, root_id)
                if not root or root.retired_at is not None:
                    lifecycle.block("archive_root_not_found", retry_allowed=False)
                    raise HTTPException(status_code=404, detail="Archive root not found")
                result = _delete_inactive_root(
                    db,
                    root,
                    current_user,
                    operation_id=operation_id,
                    operation_heartbeat=outer_heartbeat.touch,
                )
                lifecycle.mark_inner_persisted(result)
        except ArchiveRootMutationConflict as exc:
            lifecycle.block(
                safe_reason_code(exc.blocker, fallback="archive_root_mutation_conflict"),
                retry_allowed=True,
            )
            raise HTTPException(status_code=409, detail=exc.blocker) from exc
        except HTTPException as exc:
            lifecycle.block(
                safe_reason_code(exc.detail, fallback="archive_root_delete_blocked"),
                retry_allowed=exc.status_code >= 500,
            )
            raise
        outer_heartbeat.touch(force=True)
        lifecycle.finish_result(
            result,
            progress={
                "planned_count": int(result.get("planned_count") or 0),
                "completed_count": int(result.get("deleted_count") or 0),
                "failed_count": int(result.get("failed_count") or 0),
                "skipped_count": int(result.get("skipped_count") or 0),
                "completed_bytes": int(result.get("bytes_freed") or 0),
            },
            reason_code=safe_reason_code(result.get("reason_code") or result.get("error")),
            retry_allowed=bool(result.get("retryable")),
            retry_mode=result.get("retry_mode"),
        )
        if result.get("status") == "partial":
            raise HTTPException(status_code=409, detail=result)
        return result


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
    roots = db.query(ArchiveRoot).filter(ArchiveRoot.retired_at.is_(None)).all()
    try:
        outer_claim = claim_operation_with_conflicts(
            db,
            operation_type="archive_migration_apply",
            scope={
                "global": False,
                "physical_volume_ids": [root.physical_identity for root in roots if root.physical_identity],
                "root_ids": [root.id for root in roots],
                "camera_ids": [],
                "segment_ids": [],
            },
            request_identity={"target_root_id": payload.target_root_id, "plan_id": payload.plan_id},
            actor=current_user,
            idempotency_key=payload.plan_id,
            owner_instance_id=operation_instance_id("archive-migration"),
        )
    except StorageOuterConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    if outer_claim.get("state") == "terminal":
        replay = terminal_replay_result(outer_claim)
        if replay.get("status") == "blocked":
            raise HTTPException(status_code=409, detail=replay)
        if replay.get("status") == "failed":
            raise HTTPException(status_code=500, detail=replay)
        return replay
    if outer_claim.get("state") != "claimed":
        raise HTTPException(status_code=409, detail=claim_state_detail(outer_claim))
    outer_handle = outer_claim["handle"]
    outer_heartbeat = OperationHeartbeatController(db.get_bind(), outer_handle)
    with StorageOperationLifecycle(db, outer_handle, failure_reason="archive_migration_apply_failed") as lifecycle:
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
        try:
            with archive_root_mutation_guard("archive_migration_apply"):
                result = apply_storage_migration(
                    db,
                    target_root_id=payload.target_root_id,
                    expected_plan_id=payload.plan_id,
                    progress_callback=outer_heartbeat.touch,
                )
                lifecycle.mark_inner_persisted(result)
        except ArchiveRootMutationConflict as exc:
            lifecycle.block(
                safe_reason_code(exc.blocker, fallback="archive_root_mutation_conflict"),
                retry_allowed=True,
            )
            raise HTTPException(status_code=409, detail=exc.blocker) from exc
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
        outer_heartbeat.touch(force=True)
        lifecycle.finish_result(
            result,
            progress={
                "planned_count": int(result.get("planned_count") or result.get("total_would_move_count") or 0),
                "completed_count": len(result.get("executed") or []),
                "failed_count": len(result.get("failed") or []),
                "completed_bytes": int(result.get("executed_bytes") or 0),
            },
            reason_code=safe_reason_code(
                (result.get("blockers") or [None])[0] if result.get("blockers") else None,
                fallback="archive_migration_apply_incomplete" if result.get("status") != "completed" else None,
            ),
            retry_allowed=result.get("status") in {"blocked", "failed"},
            retry_mode="refresh" if result.get("status") in {"blocked", "failed"} else None,
        )
        if result["status"] == "blocked":
            raise HTTPException(status_code=409, detail=result)
        if result["status"] == "failed":
            raise HTTPException(status_code=500, detail=result)
        return result


@router.post("/integrity/scans")
def storage_integrity_scan_start(
    payload: IntegrityScanStartRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    try:
        return start_integrity_scan(
            db,
            actor=current_user,
            idempotency_key=payload.idempotency_key if payload else None,
        )
    except StorageOuterConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except (StorageOperationContractError, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"reason_code": safe_reason_code(str(exc), fallback="archive_integrity_scan_start_blocked")}) from exc


@router.get("/integrity/scans/latest")
def storage_integrity_scan_latest(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    return latest_integrity_scan(db)


@router.get("/integrity/scans/{scan_id}")
def storage_integrity_scan_status(
    scan_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    result = get_integrity_scan(db, scan_id)
    if result is None:
        raise HTTPException(status_code=404, detail={"reason_code": "archive_integrity_scan_not_found"})
    return result


@router.post("/integrity/scans/{scan_id}/cancel")
def storage_integrity_scan_cancel(
    scan_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    try:
        return cancel_integrity_scan(db, scan_id, actor=current_user)
    except StorageOperationContractError as exc:
        reason = safe_reason_code(str(exc), fallback="archive_integrity_scan_cancel_blocked")
        status_code = 404 if reason == "archive_integrity_scan_not_found" else 409
        raise HTTPException(status_code=status_code, detail={"reason_code": reason}) from exc


@router.get("/integrity/scans/{scan_id}/findings")
def storage_integrity_findings(
    scan_id: str,
    cursor: str | None = None,
    limit: int = 50,
    category: str | None = None,
    impact: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    try:
        return list_integrity_findings(
            db,
            scan_id,
            role=current_user.role,
            cursor=cursor,
            limit=limit,
            category=category,
            impact=impact,
        )
    except StorageOperationContractError as exc:
        raise HTTPException(status_code=404, detail={"reason_code": safe_reason_code(str(exc), fallback="archive_integrity_scan_not_found")}) from exc


def _create_integrity_plan(
    *,
    finding_id: str,
    payload: IntegrityPlanRequest,
    db: Session,
    current_user: User,
    allowed_actions: set[str],
):
    if payload.action_key not in allowed_actions:
        raise HTTPException(status_code=409, detail={"reason_code": "archive_integrity_action_permission_mismatch"})
    try:
        return create_remediation_plan(
            db,
            finding_id=finding_id,
            action_key=payload.action_key,
            actor=current_user,
            idempotency_key=payload.idempotency_key,
        )
    except IntegrityRemediationBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason_code": exc.reason_code, "retry_mode": exc.retry_mode},
        ) from exc
    except StorageOperationContractError as exc:
        raise HTTPException(status_code=409, detail={"reason_code": safe_reason_code(str(exc), fallback="archive_integrity_plan_blocked")}) from exc


@router.post("/integrity/findings/{finding_id}/metadata-plan")
def storage_integrity_metadata_plan(
    finding_id: str,
    payload: IntegrityPlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return _create_integrity_plan(
        finding_id=finding_id,
        payload=payload,
        db=db,
        current_user=current_user,
        allowed_actions={"mark_stale_recording"},
    )


@router.post("/integrity/findings/{finding_id}/deletion-plan")
def storage_integrity_deletion_plan(
    finding_id: str,
    payload: IntegrityPlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    return _create_integrity_plan(
        finding_id=finding_id,
        payload=payload,
        db=db,
        current_user=current_user,
        allowed_actions={"retire_missing_recording", "delete_unusable_recording", "delete_proven_orphan"},
    )


@router.get("/integrity/remediation-plans/{plan_id}")
def storage_integrity_plan_status(
    plan_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    try:
        return get_remediation_plan(db, plan_id, actor=current_user)
    except StorageOperationContractError as exc:
        raise HTTPException(status_code=404, detail={"reason_code": safe_reason_code(str(exc), fallback="archive_integrity_plan_not_found")}) from exc


def _apply_integrity_plan(
    *,
    plan_id: str,
    payload: IntegrityApplyRequest,
    db: Session,
    current_user: User,
):
    try:
        return apply_remediation_plan(
            db,
            plan_id=plan_id,
            actor=current_user,
            confirm=payload.confirm,
            operation_id=payload.operation_id,
        )
    except IntegrityRemediationBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason_code": exc.reason_code, "retry_mode": exc.retry_mode},
        ) from exc
    except StorageOperationContractError as exc:
        raise HTTPException(status_code=409, detail={"reason_code": safe_reason_code(str(exc), fallback="archive_integrity_apply_blocked")}) from exc


@router.post("/integrity/remediation-plans/{plan_id}/apply-metadata")
def storage_integrity_apply_metadata(
    plan_id: str,
    payload: IntegrityApplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return _apply_integrity_plan(plan_id=plan_id, payload=payload, db=db, current_user=current_user)


@router.post("/integrity/remediation-plans/{plan_id}/apply-deletion")
def storage_integrity_apply_deletion(
    plan_id: str,
    payload: IntegrityApplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    return _apply_integrity_plan(plan_id=plan_id, payload=payload, db=db, current_user=current_user)


@router.get("/reconciliation/summary")
def storage_reconciliation_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    return legacy_reconciliation_summary(db)


@router.post("/reconcile")
def storage_reconcile(
    payload: ReconciliationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    if payload.mode == "apply_safe":
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "archive_integrity_broad_apply_removed",
                "next_action": "use_exact_finding_remediation",
            },
        )
    return start_integrity_scan(
        db,
        actor=current_user,
        idempotency_key=payload.operation_id.lower() if payload.operation_id else None,
    )
