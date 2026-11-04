from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import stat
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.permissions import user_has_permission
from app.db.session import SessionLocal
from app.models.archive_migration import ArchiveMigrationItem, ArchiveMigrationPlan
from app.models.audit_event import AuditEvent
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.storage_operation import StorageOperation
from app.models.user import User
from app.services.audit_log import create_event
from app.services.recording_storage import KMVMS_RECORDINGS_NAMESPACE
from app.services.storage_filesystem import (
    StorageFilesystemError,
    archive_roots_overlap,
    assert_root_snapshot,
    create_relative_exclusive,
    is_migration_internal_relative,
    migration_internal_relative,
    normalize_relative_path,
    open_relative_read,
    relative_parent_fd,
    remove_empty_internal_parents,
    rename_relative,
    root_snapshot,
    stat_relative,
    unlink_relative,
    verified_root_fd,
)
from app.services.storage_operation_conflicts import (
    StorageOperationConflict,
    claim_operation_with_conflicts,
    reclaim_operation_with_conflicts,
    run_coordinated_operation_transition,
    scope_with_physical_volumes,
)
from app.services.storage_operations_foundation import (
    ACTIVE_OPERATION_STATUSES,
    MAX_RETRIES_PER_PARENT,
    MAX_RETRY_DEPTH,
    TERMINAL_OPERATION_STATUSES,
    CrossActorRecoveryAuthorization,
    OperationHandle,
    StorageOperationContractError,
    StorageOperationLeaseLost,
    acquire_worker_lease,
    actor_identity,
    canonical_operation_scope,
    database_now,
    ensure_operation_terminal_audit,
    heartbeat_operation,
    lock_operation_owned,
    operation_cancel_requested,
    public_operation_summary,
    release_worker_lease,
    renew_worker_lease,
    request_fingerprint,
    request_operation_cancel,
    safe_reason_code,
    stage_operation_terminal,
)


from app.services.migration_contracts import (
    MIGRATION_OPERATION_TYPE,
    MIGRATION_CLEANUP_CONTINUATION_MODE,
    MIGRATION_PARENT_SNAPSHOT_MAX_BYTES,
    MIGRATION_PLAN_SCHEMA_VERSION,
    PLAN_READY_TTL,
    PLAN_HISTORY_TTL,
    PLAN_BATCH_SIZE,
    PLAN_AUTHORITY_RECHECK_ITEMS,
    ITEM_PAGE_MAX,
    WORKER_POLL_SECONDS,
    WORKER_LEASE_SECONDS,
    WORKER_CANDIDATE_BATCH_SIZE,
    WORKER_CANDIDATE_DIAGNOSTIC_INTERVAL_SECONDS,
    WORKER_CANDIDATE_DIAGNOSTIC_MAX_KEYS,
    COPY_CHUNK_BYTES,
    TEMP_CREATE_MODE,
    THROUGHPUT_MAX_SAMPLES,
    THROUGHPUT_SAMPLE_INTERVAL_SECONDS,
    THROUGHPUT_MIN_SAMPLES,
    THROUGHPUT_MIN_WINDOW_SECONDS,
    THROUGHPUT_MIN_BYTES,
    THROUGHPUT_STALE_SECONDS,
    THROUGHPUT_MAX_BYTES_PER_SECOND,
    THROUGHPUT_MAX_ETA_SECONDS,
    RECENT_WRITE_WINDOW,
    RESERVE_MIN_BYTES,
    RESERVE_PERCENT,
    ACTIVE_JOB_STATES,
    ORPHAN_EXECUTION_AUDIT_EVENTS,
    INITIAL_ORPHAN_EXECUTION_AUDIT_EVENTS,
    ELIGIBLE_SEGMENT_STATUSES,
    PLAN_READY_STATUSES,
    PLAN_TERMINAL_STATUSES,
    ITEM_TERMINAL_PHASES,
    CLEANUP_RECOVERY_ITEM_PHASES,
    MIGRATION_FILESYSTEM_REASON_CODES,
    MigrationRecoveryDecision,
    ArchiveMigrationBlocked,
    _MigrationAuditPersistenceFailed,
    ArchiveMigrationPartial,
    _WorkerCandidateRejected,
    _CopyThroughputSampler,
)
from app.services.migration_manifest import (
    _sha256_json,
    _segment_metadata_fingerprint,
    _item_canonical_payload,
    _item_snapshot,
    _item_manifest_valid,
)
from app.services.migration_plans import (
    MigrationPlanBuilder,
    _active_job_ids,
    _segment_exclusion_reason,
    target_snapshot,
    migration_reserve_bytes,
    migration_required_free_bytes,
)
from app.services.migration_files import MigrationFileExecutor
from app.services.migration_cleanup_retry import MigrationCleanupRetry

logger = logging.getLogger(__name__)


_worker_candidate_state_lock = threading.Lock()
_worker_candidate_cursor: tuple[datetime, str] | None = None
_worker_candidate_diagnostics: dict[tuple[str, str], float] = {}


def _public_migration_reason(exc: Exception, *, fallback: str) -> str:
    if isinstance(exc, ArchiveMigrationBlocked):
        return safe_reason_code(exc.reason_code, fallback=fallback) or fallback
    if isinstance(exc, StorageFilesystemError):
        candidate = str(exc)
        if candidate in MIGRATION_FILESYSTEM_REASON_CODES:
            return candidate
    return safe_reason_code(fallback, fallback="migration_operation_failed") or "migration_operation_failed"








def _hash_descriptor(descriptor: int, *, progress_callback=None) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    progressed = 0
    while True:
        chunk = os.read(descriptor, COPY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        progressed += len(chunk)
        if progress_callback is not None and progressed >= 16 * COPY_CHUNK_BYTES:
            progress_callback()
            progressed = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()




def _plan_identity(plan: ArchiveMigrationPlan) -> dict[str, Any]:
    return {
        "plan_id": str(plan.id),
        "canonical_hash": str(plan.canonical_hash or ""),
        "source_root_id": str(plan.source_root_id),
        "target_root_id": str(plan.target_root_id),
        "schema_version": int(plan.schema_version),
    }


def _operation_domain_ref(plan_id: str) -> str:
    return f"migration-plan:{plan_id}"


def _operation_identity(plan: ArchiveMigrationPlan) -> dict[str, Any]:
    return {"migration": _plan_identity(plan)}


def _safe_label(root: ArchiveRoot | None) -> str | None:
    if root is None:
        return None
    return str(root.label or "Archive")[:255]


def _migration_audit_context(
    *,
    event_type: str,
    plan: ArchiveMigrationPlan,
    operation: StorageOperation | None,
) -> tuple[str, dict[str, Any]]:
    operation_id = str(operation.id) if operation is not None else None
    fencing_token = int(operation.fencing_token or 0) if operation is not None else None
    continuation = _cleanup_continuation_snapshot(operation, plan) if operation is not None else None
    attempt_id = str(continuation["attempt_id"]) if continuation is not None else None
    identity = {
        "event_type": str(event_type),
        "plan_id": str(plan.id),
        "operation_id": operation_id,
        "fencing_token": fencing_token,
        "attempt_id": attempt_id,
    }
    transition_fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    metadata = {
        "migration_transition_fingerprint": transition_fingerprint,
        "plan_id": str(plan.id),
    }
    if operation_id is not None:
        metadata["operation_id"] = operation_id
        metadata["fencing_token"] = fencing_token
    if attempt_id is not None:
        metadata["attempt_id"] = attempt_id
    return transition_fingerprint, metadata


def _strict_migration_audit(
    db: Session,
    *,
    actor: Any = None,
    event_type: str,
    plan: ArchiveMigrationPlan,
    operation: StorageOperation | None = None,
    severity: str = "info",
    metadata: dict[str, Any] | None = None,
    target_type: str = "archive_migration_plan",
    target_id: str | None = None,
    message_ru: str = "Состояние переноса архива изменено",
    message_en: str = "Archive migration state changed",
) -> bool:
    resolved_target_id = str(target_id or plan.id)
    transition_fingerprint, identity_metadata = _migration_audit_context(
        event_type=event_type,
        plan=plan,
        operation=operation,
    )
    try:
        db.flush()
        locked_plan = (
            db.query(ArchiveMigrationPlan.id)
            .filter(ArchiveMigrationPlan.id == str(plan.id))
            .with_for_update()
            .first()
        )
        if locked_plan is None:
            db.rollback()
            raise _MigrationAuditPersistenceFailed()
        fingerprint_value = AuditEvent.event_metadata[
            "migration_transition_fingerprint"
        ].as_string()
        audit_identity = (
            AuditEvent.event_type == event_type,
            AuditEvent.target_type == target_type,
            AuditEvent.target_id == resolved_target_id,
        )
        exact_match = (
            db.query(AuditEvent.id)
            .filter(*audit_identity, fingerprint_value == transition_fingerprint)
            .limit(1)
            .first()
        )
        if exact_match is not None:
            return False
        if operation is None:
            legacy_match = (
                db.query(AuditEvent.id)
                .filter(*audit_identity, fingerprint_value.is_(None))
                .limit(1)
                .first()
            )
            if legacy_match is not None:
                return False
    except _MigrationAuditPersistenceFailed:
        raise
    except Exception as exc:
        db.rollback()
        raise _MigrationAuditPersistenceFailed() from exc
    event = create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type=event_type,
        severity=severity,
        message_ru=message_ru,
        message_en=message_en,
        target_type=target_type,
        target_id=resolved_target_id,
        metadata={
            "status": str(plan.status),
            "phase": str(plan.phase),
            "source_root_id": str(plan.source_root_id),
            "target_root_id": str(plan.target_root_id),
            **identity_metadata,
            **dict(metadata or {}),
        },
        commit=False,
    )
    if event is None:
        raise _MigrationAuditPersistenceFailed()
    return True


def _audit(
    db: Session,
    *,
    actor: Any = None,
    event_type: str,
    plan: ArchiveMigrationPlan,
    operation: StorageOperation | None = None,
    severity: str = "info",
    metadata: dict[str, Any] | None = None,
) -> bool:
    return _strict_migration_audit(
        db,
        actor=actor,
        event_type=event_type,
        plan=plan,
        operation=operation,
        severity=severity,
        metadata=metadata,
    )


def _audit_cleanup_takeover(
    db: Session,
    *,
    actor: User | None,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
    event_type: str,
    severity: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    parent = dict(operation.parent_snapshot or {})
    continuation = _cleanup_continuation_snapshot(operation, plan)
    recovery_actor_key = (
        str(continuation["actor_key"])
        if continuation is not None
        else str(operation.actor_key)
    )
    return _strict_migration_audit(
        db,
        actor=actor,
        event_type=event_type,
        severity=severity,
        plan=plan,
        operation=operation,
        target_type="storage_operation",
        target_id=str(operation.id),
        message_ru="Изменено состояние восстановления переноса архива",
        message_en="Archive migration recovery state changed",
        metadata={
            "parent_operation_id": str(operation.parent_operation_id or ""),
            "original_actor_key": str(parent.get("original_actor_key") or plan.actor_key),
            "recovery_actor_key": recovery_actor_key,
            "recovery_mode": "cleanup_only",
            **dict(metadata or {}),
        },
    )


def _ensure_orphan_cleanup_retry_queue_audit(
    db: Session,
    *,
    actor: User | None,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
) -> None:
    _audit(
        db,
        actor=actor,
        event_type="archive_migration.cleanup_retry_queued",
        plan=plan,
        operation=operation,
        severity="warning",
        metadata={
            "reconstructed_after_binding_crash": True,
            "queued_actor_deleted": operation.actor_user_id is None,
        },
    )


def _ensure_orphan_cleanup_takeover_queue_audit(
    db: Session,
    *,
    actor: User | None,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
) -> None:
    _audit_cleanup_takeover(
        db,
        actor=actor,
        operation=operation,
        plan=plan,
        event_type="archive_migration.cleanup_takeover_queued",
        severity="warning",
        metadata={
            "reconstructed_after_binding_crash": True,
            "queued_actor_deleted": operation.actor_user_id is None,
        },
    )


def _cleanup_attempt_target_id(operation_id: str, attempt_id: str) -> str:
    digest = hashlib.sha256(f"{operation_id}:{attempt_id}".encode("utf-8")).hexdigest()[:32]
    return f"migration-attempt:{digest}"


def _ensure_cleanup_attempt_audit(
    db: Session,
    *,
    actor: User | None,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
    event_type: str,
    status: str,
    severity: str = "warning",
) -> None:
    continuation = _cleanup_continuation_snapshot(operation, plan)
    if continuation is None:
        return
    target_id = _cleanup_attempt_target_id(str(operation.id), str(continuation["attempt_id"]))
    exists = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_type == "storage_operation_attempt",
            AuditEvent.target_id == target_id,
        )
        .first()
    )
    if exists is not None:
        return
    event = create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type=event_type,
        severity=severity,
        message_ru="Изменено состояние попытки восстановления переноса архива",
        message_en="Archive migration recovery attempt state changed",
        target_type="storage_operation_attempt",
        target_id=target_id,
        metadata={
            "operation_id": str(operation.id),
            "plan_id": str(plan.id),
            "attempt": int(continuation["attempt"]),
            "status": str(status),
            "recovery_mode": "cleanup_only",
        },
        commit=False,
    )
    if event is None:
        raise _MigrationAuditPersistenceFailed()


def _canonical_user_operation_actor_id(operation: StorageOperation) -> int | None:
    if str(operation.actor_kind) != "user" or operation.system_owner is not None:
        return None
    actor_key = str(operation.actor_key or "")
    if not actor_key.startswith("user:"):
        return None
    raw_id = actor_key.removeprefix("user:")
    if not raw_id or not raw_id.isascii() or not raw_id.isdigit():
        return None
    actor_id = int(raw_id)
    if actor_id <= 0 or raw_id != str(actor_id):
        return None
    if operation.actor_user_id is not None:
        try:
            if int(operation.actor_user_id) != actor_id:
                return None
        except (TypeError, ValueError):
            return None
    return actor_id


def _ensure_operation_queue_audit(
    db: Session,
    operation: StorageOperation,
    *,
    queued_actor_id: int,
) -> None:
    exists = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == "storage_operation.queued",
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == str(operation.id),
        )
        .first()
    )
    if exists is not None:
        return
    actor = db.get(User, operation.actor_user_id) if operation.actor_user_id else None
    event = create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type="storage_operation.queued",
        severity="info",
        message_ru="Storage operation state changed",
        message_en="Storage operation state changed",
        target_type="storage_operation",
        target_id=str(operation.id),
        metadata={
            "operation_type": str(operation.operation_type),
            "status": "queued",
            "actor_kind": str(operation.actor_kind),
            "queued_actor_user_id": int(queued_actor_id),
            "queued_actor_deleted": operation.actor_user_id is None,
            "reconstructed_after_binding_crash": True,
        },
        commit=False,
    )
    if event is None:
        raise _MigrationAuditPersistenceFailed()


def _ensure_orphan_adoption_audit(
    db: Session,
    *,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
    queued_actor_id: int,
    repair_actor: User | None,
    repair_origin: str,
) -> None:
    if repair_origin not in {"endpoint", "system_worker"}:
        raise _MigrationAuditPersistenceFailed()
    if (repair_origin == "endpoint") != (repair_actor is not None):
        raise _MigrationAuditPersistenceFailed()
    event_type = "archive_migration.retry_child_adopted"
    exists = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == str(operation.id),
        )
        .first()
    )
    if exists is not None:
        return
    event = create_event(
        db=db,
        actor=repair_actor,
        category="storage",
        event_type=event_type,
        severity="warning",
        message_ru="Восстановлена связь операции переноса архива",
        message_en="Archive migration operation binding recovered",
        target_type="storage_operation",
        target_id=str(operation.id),
        metadata={
            "plan_id": str(plan.id),
            "parent_operation_id": str(operation.parent_operation_id or ""),
            "recovery_mode": str(dict(operation.parent_snapshot or {}).get("retry_mode") or ""),
            "repair_origin": repair_origin,
            "queued_actor_kind": str(operation.actor_kind),
            "queued_actor_user_id": int(queued_actor_id),
            "queued_actor_deleted": operation.actor_user_id is None,
        },
        commit=False,
    )
    if event is None:
        raise _MigrationAuditPersistenceFailed()


def _actor_can_read_plan(actor: Any, plan: ArchiveMigrationPlan) -> bool:
    return bool(
        int(getattr(actor, "id", -1)) == int(plan.actor_user_id or -2)
        or str(getattr(actor, "role", "")).lower() in {"owner", "admin"}
    )


def _has_required_permissions(user: User | None, permissions: str) -> bool:
    required = tuple(item.strip() for item in str(permissions or "").split(",") if item.strip())
    return bool(
        user
        and user.is_active
        and required
        and all(user_has_permission(user.role, permission) for permission in required)
    )


def _plan_permission_contract(plan: ArchiveMigrationPlan) -> dict[str, list[str]]:
    def values(field: str) -> list[str]:
        return [item.strip() for item in str(getattr(plan, field, "") or "").split(",") if item.strip()]

    return {
        "read": values("required_read_permission"),
        "prepare": values("required_prepare_permission"),
        "apply": values("required_apply_permissions"),
        "cancel": values("required_cancel_permission"),
        "retry": values("required_retry_permissions"),
    }


def _require_plan_access(actor: Any, plan: ArchiveMigrationPlan) -> None:
    if not _actor_can_read_plan(actor, plan) or not _has_required_permissions(
        actor,
        plan.required_read_permission,
    ):
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)


def _permission_conjunction(user: User | None) -> bool:
    return _has_required_permissions(user, "manage_settings,delete_recordings")


def _actor_matches_plan(actor: Any, plan: ArchiveMigrationPlan) -> bool:
    _actor_kind, actor_key, actor_user_id, _system_owner = actor_identity(actor)
    return bool(
        plan.actor_user_id is not None
        and actor_user_id is not None
        and int(actor_user_id) == int(plan.actor_user_id)
        and str(actor_key) == str(plan.actor_key)
    )


def _is_recovery_administrator(actor: User | None) -> bool:
    return bool(
        actor
        and actor.is_active
        and str(actor.role or "").lower() in {"owner", "admin"}
        and _permission_conjunction(actor)
    )


def _require_initial_apply_authority(actor: User, plan: ArchiveMigrationPlan) -> None:
    if not _actor_matches_plan(actor, plan):
        raise ArchiveMigrationBlocked("migration_plan_actor_mismatch", retry_mode=None)
    if not _permission_conjunction(actor):
        raise ArchiveMigrationBlocked("migration_permission_required", retry_mode=None)


def _cleanup_continuation_snapshot(
    operation: StorageOperation | None,
    plan: ArchiveMigrationPlan,
) -> dict[str, Any] | None:
    if operation is None:
        return None
    raw = dict(operation.parent_snapshot or {}).get("cleanup_continuation")
    if not isinstance(raw, dict):
        return None
    try:
        attempt = int(raw.get("attempt") or 0)
        actor_user_id = int(raw.get("actor_user_id") or 0)
        queued_fencing_token = int(raw.get("queued_fencing_token") or 0)
    except (TypeError, ValueError):
        return None
    actor_key = f"user:{actor_user_id}"
    if not (
        str(raw.get("mode") or "") == MIGRATION_CLEANUP_CONTINUATION_MODE
        and str(operation.domain_ref or "") == _operation_domain_ref(str(plan.id))
        and str(operation.request_fingerprint) == request_fingerprint(_operation_identity(plan))
        and (
            str(operation.retry_mode or plan.retry_mode or "") == "cleanup_only"
            or str(operation.status or "") in TERMINAL_OPERATION_STATUSES
        )
        and attempt > 0
        and actor_user_id > 0
        and len(str(raw.get("attempt_id") or "")) == 32
        and len(str(raw.get("idempotency_fingerprint") or "")) == 64
        and queued_fencing_token > 0
        and queued_fencing_token <= int(operation.fencing_token or 0)
    ):
        return None
    normalized = dict(raw)
    normalized["actor_key"] = actor_key
    normalized["original_actor_key"] = str(plan.actor_key)
    return normalized


def _operation_is_authorized_cleanup_takeover(
    operation: StorageOperation | None,
    plan: ArchiveMigrationPlan,
) -> bool:
    continuation = _cleanup_continuation_snapshot(operation, plan)
    if continuation is not None:
        return str(continuation["actor_key"]) != str(plan.actor_key)
    if operation is None or str(operation.actor_key) == str(plan.actor_key):
        return False
    parent = dict(operation.parent_snapshot or {})
    return bool(
        str(plan.retry_mode or "") == "cleanup_only"
        and str(operation.operation_type) == MIGRATION_OPERATION_TYPE
        and str(operation.domain_ref or "") == _operation_domain_ref(str(plan.id))
        and str(operation.request_fingerprint) == request_fingerprint(_operation_identity(plan))
        and str(parent.get("cross_actor_recovery") or "") == "migration_cleanup_takeover"
        and str(parent.get("original_actor_key") or "") == str(plan.actor_key)
        and str(parent.get("domain_ref") or "") == _operation_domain_ref(str(plan.id))
        and str(parent.get("retry_mode") or "") == "cleanup_only"
        and bool(operation.parent_operation_id)
    )


def _require_prepare_authority(db: Session, plan: ArchiveMigrationPlan) -> User:
    actor = (
        db.query(User)
        .filter(User.id == int(plan.actor_user_id))
        .populate_existing()
        .first()
        if plan.actor_user_id
        else None
    )
    if not _has_required_permissions(actor, plan.required_prepare_permission):
        raise ArchiveMigrationBlocked("migration_prepare_permission_revoked", retry_mode=None)
    return actor


def _require_runtime_authority(db: Session, plan: ArchiveMigrationPlan) -> User:
    operation = (
        db.query(StorageOperation)
        .filter(StorageOperation.id == str(plan.current_operation_id))
        .populate_existing()
        .one_or_none()
        if plan.current_operation_id
        else None
    )
    continuation = _cleanup_continuation_snapshot(operation, plan)
    if continuation is None and isinstance(
        dict(operation.parent_snapshot or {}).get("cleanup_continuation") if operation is not None else None,
        dict,
    ):
        raise ArchiveMigrationBlocked("migration_authority_contract_invalid", retry_mode=None)
    if continuation is not None:
        actor = (
            db.query(User)
            .filter(User.id == int(continuation["actor_user_id"]))
            .populate_existing()
            .one_or_none()
        )
        if str(continuation["actor_key"]) == str(plan.actor_key):
            if (
                actor is None
                or int(actor.id) != int(plan.actor_user_id or -1)
                or not _permission_conjunction(actor)
            ):
                raise ArchiveMigrationBlocked(
                    "migration_permission_revoked",
                    retry_mode=_permission_loss_retry_mode(db, plan),
                )
            return actor
        if not _is_recovery_administrator(actor):
            raise ArchiveMigrationBlocked(
                "migration_recovery_permission_revoked",
                retry_mode=_permission_loss_retry_mode(db, plan),
            )
        return actor
    if _operation_is_authorized_cleanup_takeover(operation, plan):
        actor = (
            db.query(User).filter(User.id == int(operation.actor_user_id)).populate_existing().one_or_none()
            if operation and operation.actor_user_id
            else None
        )
        if not _is_recovery_administrator(actor):
            raise ArchiveMigrationBlocked(
                "migration_recovery_permission_revoked",
                retry_mode=_permission_loss_retry_mode(db, plan),
            )
        return actor
    if operation is not None and str(operation.actor_key) != str(plan.actor_key):
        raise ArchiveMigrationBlocked("migration_authority_contract_invalid", retry_mode=None)
    actor = (
        db.query(User).filter(User.id == int(plan.actor_user_id)).populate_existing().one_or_none()
        if plan.actor_user_id
        else None
    )
    if not _permission_conjunction(actor):
        raise ArchiveMigrationBlocked(
            "migration_permission_revoked",
            retry_mode=_permission_loss_retry_mode(db, plan),
        )
    if operation is not None and (
        operation.actor_user_id is None
        or int(operation.actor_user_id) != int(plan.actor_user_id or -1)
        or str(operation.actor_key) != str(plan.actor_key)
    ):
        raise ArchiveMigrationBlocked("migration_authority_contract_invalid", retry_mode=None)
    return actor


def _permission_loss_retry_mode(db: Session, plan: ArchiveMigrationPlan) -> str:
    if _cleanup_takeover_work_exists(db, plan):
        return "cleanup_only"
    return "after_permission_restore"


def _operation_matches_migration_lineage(
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
    *,
    expected_scope: dict[str, Any] | None = None,
) -> bool:
    try:
        scope = canonical_operation_scope(operation.scope)
    except StorageOperationContractError:
        return False
    expected_root_ids = {str(plan.source_root_id), str(plan.target_root_id)}
    scope_matches_plan = bool(
        not scope.get("global")
        and set(scope.get("root_ids") or []) == expected_root_ids
        and not scope.get("camera_ids")
        and not scope.get("segment_ids")
    )
    return bool(
        str(operation.operation_type) == MIGRATION_OPERATION_TYPE
        and str(operation.domain_ref or "") == _operation_domain_ref(str(plan.id))
        and str(operation.request_fingerprint) == request_fingerprint(_operation_identity(plan))
        and scope_matches_plan
        and (expected_scope is None or scope == expected_scope)
    )


def _lineage_snapshot_matches(
    child: StorageOperation,
    parent: StorageOperation,
    plan: ArchiveMigrationPlan,
) -> bool:
    snapshot = dict(child.parent_snapshot or {})
    try:
        retry_depth_matches = int(snapshot.get("retry_depth")) == int(parent.retry_depth or 0)
    except (TypeError, ValueError):
        return False
    recovery_marker = str(snapshot.get("cross_actor_recovery") or "")
    if str(child.actor_key) != str(plan.actor_key) and recovery_marker != "migration_cleanup_takeover":
        return False
    if recovery_marker and recovery_marker != "migration_cleanup_takeover":
        return False
    return bool(
        str(child.parent_operation_id or "") == str(parent.id)
        and str(snapshot.get("operation_id") or "") == str(parent.id)
        and str(snapshot.get("status") or "") == str(parent.status)
        and retry_depth_matches
        and str(snapshot.get("actor_key") or "") == str(parent.actor_key)
        and str(snapshot.get("original_actor_key") or "") == str(plan.actor_key)
        and str(snapshot.get("domain_ref") or "") == _operation_domain_ref(str(plan.id))
        and str(snapshot.get("retry_mode") or "") == str(parent.retry_mode or "")
    )


def _validated_cleanup_lineage(
    db: Session,
    *,
    plan: ArchiveMigrationPlan,
    current_operation: StorageOperation,
    require_current_terminal: bool,
) -> list[StorageOperation]:
    try:
        current_scope = canonical_operation_scope(current_operation.scope)
    except StorageOperationContractError as exc:
        raise StorageOperationLeaseLost("storage_operation_lease_lost") from exc
    if (
        str(plan.retry_mode or "") != "cleanup_only"
        or not _operation_matches_migration_lineage(
            current_operation,
            plan,
            expected_scope=current_scope,
        )
        or int(current_operation.retry_depth or 0) < 0
        or int(current_operation.retry_depth or 0) > MAX_RETRY_DEPTH
        or (
            require_current_terminal
            and (
                current_operation.status not in TERMINAL_OPERATION_STATUSES
                or str(current_operation.retry_mode or "") != "cleanup_only"
                or not bool(current_operation.retry_allowed)
            )
        )
    ):
        raise StorageOperationLeaseLost("storage_operation_lease_lost")

    lineage = [current_operation]
    if not current_operation.parent_operation_id:
        if int(current_operation.retry_depth or 0) != 0 or str(current_operation.actor_key) != str(plan.actor_key):
            raise StorageOperationLeaseLost("storage_operation_lease_lost")
        return lineage

    if int(current_operation.retry_depth or 0) <= 0:
        raise StorageOperationLeaseLost("storage_operation_lease_lost")

    seen = {str(current_operation.id)}
    child = current_operation
    for _depth in range(1, MAX_RETRY_DEPTH + 1):
        parent_id = str(child.parent_operation_id or "")
        if not parent_id or parent_id in seen:
            raise StorageOperationLeaseLost("storage_operation_lease_lost")
        seen.add(parent_id)
        parent = (
            db.query(StorageOperation)
            .filter(StorageOperation.id == parent_id)
            .populate_existing()
            .one_or_none()
        )
        if (
            parent is None
            or parent.status not in TERMINAL_OPERATION_STATUSES
            or str(parent.retry_mode or "") != "cleanup_only"
            or not bool(parent.retry_allowed)
            or not _operation_matches_migration_lineage(parent, plan, expected_scope=current_scope)
            or not _lineage_snapshot_matches(child, parent, plan)
            or int(child.retry_depth or 0) != int(parent.retry_depth or 0) + 1
            or int(parent.retry_depth or 0) < 0
            or int(child.retry_depth or 0) > MAX_RETRY_DEPTH
        ):
            raise StorageOperationLeaseLost("storage_operation_lease_lost")
        lineage.append(parent)
        if not parent.parent_operation_id:
            if (
                int(parent.retry_depth or 0) != 0
                or str(parent.actor_key) != str(plan.actor_key)
            ):
                raise StorageOperationLeaseLost("storage_operation_lease_lost")
            return lineage
        child = parent
    raise StorageOperationLeaseLost("storage_operation_lease_lost")


def _validated_cleanup_ancestor_operation(
    db: Session,
    *,
    plan: ArchiveMigrationPlan,
    current_operation: StorageOperation,
    bound_operation_id: str,
) -> StorageOperation:
    lineage = _validated_cleanup_lineage(
        db,
        plan=plan,
        current_operation=current_operation,
        require_current_terminal=False,
    )
    for operation in lineage[1:]:
        if str(operation.id) == str(bound_operation_id):
            return operation
    raise StorageOperationLeaseLost("storage_operation_lease_lost")


def _lock_owned_migration_plan(
    db: Session,
    plan: ArchiveMigrationPlan,
    handle: OperationHandle,
    *,
    require_runtime_authority: bool = True,
) -> tuple[StorageOperation, ArchiveMigrationPlan]:
    operation = lock_operation_owned(db, handle)
    locked_plan = (
        db.query(ArchiveMigrationPlan)
        .filter(ArchiveMigrationPlan.id == str(plan.id))
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if (
        locked_plan is None
        or str(locked_plan.current_operation_id or "") != str(handle.operation_id)
        or str(operation.domain_ref or "") != _operation_domain_ref(str(plan.id))
    ):
        raise StorageOperationLeaseLost("storage_operation_lease_lost")
    if require_runtime_authority:
        _require_runtime_authority(db, locked_plan)
    return operation, locked_plan


def _lock_owned_migration_item(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    handle: OperationHandle,
    *,
    require_runtime_authority: bool = True,
    allow_ancestor_adoption: bool = True,
) -> tuple[StorageOperation, ArchiveMigrationPlan, ArchiveMigrationItem]:
    operation, locked_plan = _lock_owned_migration_plan(
        db,
        plan,
        handle,
        require_runtime_authority=require_runtime_authority,
    )
    locked_item = (
        db.query(ArchiveMigrationItem)
        .filter(
            ArchiveMigrationItem.id == str(item.id),
            ArchiveMigrationItem.plan_id == str(plan.id),
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if locked_item is None or (
        str(locked_item.source_root_id) != str(locked_plan.source_root_id)
        or str(locked_item.target_root_id) != str(locked_plan.target_root_id)
    ):
        raise StorageOperationLeaseLost("storage_operation_lease_lost")

    bound_operation_id = str(locked_item.operation_id or "")
    if not bound_operation_id:
        if locked_item.operation_fencing_token is not None:
            raise StorageOperationLeaseLost("storage_operation_lease_lost")
    elif bound_operation_id == str(operation.id):
        if locked_item.operation_fencing_token is None:
            locked_item.operation_fencing_token = int(operation.fencing_token)
            db.add(locked_item)
            db.commit()
            return _lock_owned_migration_item(
                db,
                locked_plan,
                locked_item,
                handle,
                require_runtime_authority=require_runtime_authority,
                allow_ancestor_adoption=False,
            )
        if int(locked_item.operation_fencing_token) <= 0 or int(
            locked_item.operation_fencing_token
        ) > int(operation.fencing_token):
            raise StorageOperationLeaseLost("storage_operation_lease_lost")
    else:
        if not allow_ancestor_adoption or not (
            bool(locked_item.cleanup_pending)
            or str(locked_item.phase) in CLEANUP_RECOVERY_ITEM_PHASES
        ):
            raise StorageOperationLeaseLost("storage_operation_lease_lost")
        bound_operation = _validated_cleanup_ancestor_operation(
            db,
            plan=locked_plan,
            current_operation=operation,
            bound_operation_id=bound_operation_id,
        )
        if (
            locked_item.operation_fencing_token is None
            or int(locked_item.operation_fencing_token) <= 0
            or int(locked_item.operation_fencing_token) > int(bound_operation.fencing_token or 0)
        ):
            raise StorageOperationLeaseLost("storage_operation_lease_lost")
        locked_item.operation_id = str(operation.id)
        locked_item.operation_fencing_token = int(operation.fencing_token)
        db.add(locked_item)
        db.commit()
        return _lock_owned_migration_item(
            db,
            locked_plan,
            locked_item,
            handle,
            require_runtime_authority=require_runtime_authority,
            allow_ancestor_adoption=False,
        )

    if int(operation.fencing_token) != int(handle.fencing_token):
        raise StorageOperationLeaseLost("storage_operation_lease_lost")
    locked_item.operation_id = str(handle.operation_id)
    locked_item.operation_fencing_token = int(handle.fencing_token)
    return operation, locked_plan, locked_item


def _root_by_id(db: Session, root_id: str) -> ArchiveRoot:
    root = db.get(ArchiveRoot, str(root_id))
    if root is None or root.retired_at is not None:
        raise ArchiveMigrationBlocked("archive_root_missing")
    return root


def _snapshot_fields(snapshot: dict[str, Any]) -> dict[str, str]:
    return {
        "physical_identity": str(snapshot["physical_identity"]),
        "snapshot_key": str(snapshot["snapshot_key"]),
        "access_identity": str(snapshot["access_identity"]),
    }


def _snapshot_from_plan(plan: ArchiveMigrationPlan, *, source: bool) -> dict[str, Any]:
    prefix = "source" if source else "target"
    return {
        "root_id": str(getattr(plan, f"{prefix}_root_id")),
        "physical_identity": str(getattr(plan, f"{prefix}_physical_identity")),
        "snapshot_key": str(getattr(plan, f"{prefix}_snapshot_key")),
        "access_identity": str(getattr(plan, f"{prefix}_access_identity")),
    }


def _increment_reason(summary: dict[str, Any], reason: str) -> dict[str, int]:
    result = {str(key): int(value) for key, value in dict(summary or {}).items()}
    result[reason] = result.get(reason, 0) + 1
    return result


def request_migration_plan(
    db: Session,
    *,
    actor: User,
    source_root_id: str,
    target_root_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if not _has_required_permissions(actor, "manage_settings"):
        raise ArchiveMigrationBlocked("migration_prepare_permission_required", retry_mode=None)
    source_id = str(source_root_id or "").strip()
    target_id = str(target_root_id or "").strip()
    if not source_id or not target_id:
        raise ArchiveMigrationBlocked("migration_source_target_required", retry_mode=None)
    if source_id == target_id:
        raise ArchiveMigrationBlocked("migration_source_equals_target", retry_mode=None)
    source = _root_by_id(db, source_id)
    target = _root_by_id(db, target_id)
    try:
        source_snapshot = root_snapshot(source, require_write=True)
        target_snapshot = root_snapshot(target, require_write=True)
    except StorageFilesystemError as exc:
        raise ArchiveMigrationBlocked(
            _public_migration_reason(exc, fallback="migration_plan_preparation_failed"),
            retry_mode="refresh",
        ) from exc
    except Exception as exc:
        raise ArchiveMigrationBlocked("migration_plan_preparation_failed", retry_mode="refresh") from exc
    if archive_roots_overlap(source, target):
        raise ArchiveMigrationBlocked("archive_root_overlap", retry_mode=None)

    actor_kind, actor_key, actor_user_id, _owner = actor_identity(actor)
    request_identity = {
        "source_root_id": source_id,
        "target_root_id": target_id,
        "actor_key": actor_key,
    }
    fingerprint = request_fingerprint(request_identity)
    normalized_idempotency = str(idempotency_key or fingerprint).strip().lower()
    existing = (
        db.query(ArchiveMigrationPlan)
        .filter(
            ArchiveMigrationPlan.actor_key == actor_key,
            ArchiveMigrationPlan.idempotency_key == normalized_idempotency,
        )
        .first()
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ArchiveMigrationBlocked("migration_plan_idempotency_mismatch", retry_mode=None)
        return public_migration_plan(existing, replayed=True)

    source_fields = _snapshot_fields(source_snapshot)
    target_fields = _snapshot_fields(target_snapshot)
    now = database_now(db)
    high_watermark = int(
        db.query(func.coalesce(func.max(RecordingSegment.id), 0))
        .filter(RecordingSegment.archive_root_id == source_id)
        .scalar()
        or 0
    )
    plan = ArchiveMigrationPlan(
        id=str(uuid.uuid4()),
        actor_user_id=actor_user_id,
        actor_key=actor_key,
        idempotency_key=normalized_idempotency,
        request_fingerprint=fingerprint,
        source_root_id=source_id,
        target_root_id=target_id,
        source_label_snapshot=_safe_label(source) or "Archive",
        target_label_snapshot=_safe_label(target) or "Archive",
        source_physical_identity=source_fields["physical_identity"],
        target_physical_identity=target_fields["physical_identity"],
        source_snapshot_key=source_fields["snapshot_key"],
        target_snapshot_key=target_fields["snapshot_key"],
        source_access_identity=source_fields["access_identity"],
        target_access_identity=target_fields["access_identity"],
        schema_version=MIGRATION_PLAN_SCHEMA_VERSION,
        segment_high_watermark=high_watermark,
        inventory_cursor=0,
        same_physical_volume=(
            source_fields["physical_identity"] == target_fields["physical_identity"]
        ),
        capacity_total_bytes=int(target_snapshot["total_bytes"]),
        capacity_free_bytes=int(target_snapshot["free_bytes"]),
        reserve_bytes=migration_reserve_bytes(int(target_snapshot["total_bytes"])),
        status="building",
        phase="inventory",
        required_read_permission="manage_settings",
        required_prepare_permission="manage_settings",
        required_apply_permissions="manage_settings,delete_recordings",
        required_cancel_permission="manage_settings",
        required_retry_permissions="manage_settings,delete_recordings",
        expires_at=now + PLAN_HISTORY_TTL,
        created_at=now,
        updated_at=now,
    )
    db.add(plan)
    _audit(db, actor=actor, event_type="archive_migration.plan_requested", plan=plan)
    db.commit()
    return public_migration_plan(plan)








def _migration_plan_builder() -> MigrationPlanBuilder:
    return MigrationPlanBuilder(
        _require_prepare_authority, _root_by_id, _hash_descriptor, _increment_reason, _snapshot_from_plan, _plan_permission_contract, _audit,
    )


def _migration_file_executor() -> MigrationFileExecutor:
    return MigrationFileExecutor(
        _lock_owned_migration_item, _operation_progress, _root_by_id, _require_runtime_authority, _hash_descriptor,
        heartbeat_operation,
    )


def _migration_cleanup_retry() -> MigrationCleanupRetry:
    return MigrationCleanupRetry(
        _cleanup_source, _remove_owned_target_residue, _lock_owned_migration_item, _root_by_id, _operation_progress, _recount_plan, _terminalize_operation,
        heartbeat_operation,
    )


def _materialize_plan_batch(db: Session, plan: ArchiveMigrationPlan) -> bool:
    return _migration_plan_builder()._materialize_plan_batch(db, plan)




def _canonical_manifest_hash(db: Session, plan: ArchiveMigrationPlan) -> str:
    return _migration_plan_builder()._canonical_manifest_hash(db, plan)


def _manifest_integrity_valid(db: Session, plan: ArchiveMigrationPlan) -> bool:
    return _migration_plan_builder()._manifest_integrity_valid(db, plan)


def _finalize_materialized_plan(
    db: Session,
    plan: ArchiveMigrationPlan,
    target_current: dict[str, Any],
) -> None:
    return _migration_plan_builder()._finalize_materialized_plan(db, plan, target_current)


def _prepare_one_plan(leader=None) -> bool:
    with SessionLocal() as db:
        plan = (
            db.query(ArchiveMigrationPlan)
            .filter(ArchiveMigrationPlan.status == "building")
            .order_by(ArchiveMigrationPlan.created_at.asc(), ArchiveMigrationPlan.id.asc())
            .first()
        )
        if plan is None:
            return False
        try:
            while _materialize_plan_batch(db, plan):
                if leader is not None:
                    leader.assert_owned()
                plan = db.get(ArchiveMigrationPlan, plan.id)
        except _MigrationAuditPersistenceFailed:
            db.rollback()
            return True
        except Exception as exc:
            db.rollback()
            plan = db.get(ArchiveMigrationPlan, plan.id)
            if plan is not None:
                plan.status = "blocked"
                plan.phase = "blocked"
                plan.reason_code = _public_migration_reason(
                    exc,
                    fallback="migration_plan_preparation_failed",
                )
                plan.next_action = "refresh_plan"
                plan.finished_at = database_now(db)
                try:
                    _audit(
                        db,
                        event_type="archive_migration.plan_blocked",
                        plan=plan,
                        severity="warning",
                        metadata={"reason_code": plan.reason_code},
                    )
                    db.commit()
                except _MigrationAuditPersistenceFailed:
                    db.rollback()
        return True


def _refresh_plan_expiry(db: Session, plan: ArchiveMigrationPlan) -> None:
    now = database_now(db)
    if plan.status in PLAN_READY_STATUSES and plan.expires_at and plan.expires_at <= now:
        plan.status = "expired"
        plan.phase = "expired"
        plan.reason_code = "migration_plan_expired"
        plan.next_action = "refresh_plan"
        plan.finished_at = now
        _audit(db, event_type="archive_migration.plan_expired", plan=plan, severity="warning")
        db.commit()


def _expire_one_ready_plan() -> bool:
    with SessionLocal() as db:
        now = database_now(db)
        plan = (
            db.query(ArchiveMigrationPlan)
            .filter(
                ArchiveMigrationPlan.status.in_(tuple(PLAN_READY_STATUSES)),
                ArchiveMigrationPlan.expires_at.isnot(None),
                ArchiveMigrationPlan.expires_at <= now,
            )
            .order_by(ArchiveMigrationPlan.expires_at.asc(), ArchiveMigrationPlan.id.asc())
            .first()
        )
        if plan is None:
            return False
        try:
            _refresh_plan_expiry(db, plan)
            return True
        except _MigrationAuditPersistenceFailed:
            db.rollback()
            return False


def public_migration_plan(plan: ArchiveMigrationPlan, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "plan_id": str(plan.id),
        "status": str(plan.status),
        "phase": str(plan.phase),
        "schema_version": int(plan.schema_version),
        "source_root_id": str(plan.source_root_id),
        "source_label": str(plan.source_label_snapshot),
        "target_root_id": str(plan.target_root_id),
        "target_label": str(plan.target_label_snapshot),
        "same_physical_volume": bool(plan.same_physical_volume),
        "item_count": int(plan.item_count or 0),
        "total_bytes": int(plan.total_bytes or 0),
        "completed_count": int(plan.completed_count or 0),
        "completed_bytes": int(plan.completed_bytes or 0),
        "failed_count": int(plan.failed_count or 0),
        "cancelled_count": int(plan.cancelled_count or 0),
        "excluded_count": int(plan.excluded_count or 0),
        "excluded_summary": dict(plan.excluded_summary or {}),
        "blocker_summary": dict(plan.blocker_summary or {}),
        "capacity_total_bytes": int(plan.capacity_total_bytes or 0),
        "capacity_free_bytes": int(plan.capacity_free_bytes or 0),
        "reserve_bytes": int(plan.reserve_bytes or 0),
        "required_free_bytes": int(plan.required_free_bytes or 0),
        "segment_high_watermark": int(plan.segment_high_watermark or 0),
        "canonical_hash": str(plan.canonical_hash) if plan.canonical_hash else None,
        "operation_id": plan.current_operation_id,
        "cleanup_pending": bool(plan.cleanup_pending),
        "new_after_high_watermark_count": plan.new_after_high_watermark_count,
        "retained_source_count": plan.retained_source_count,
        "reason_code": plan.reason_code,
        "next_action": plan.next_action,
        "retry_mode": plan.retry_mode,
        "ready_at": plan.ready_at.isoformat() if plan.ready_at else None,
        "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
        "started_at": plan.started_at.isoformat() if plan.started_at else None,
        "finished_at": plan.finished_at.isoformat() if plan.finished_at else None,
        "replayed": bool(replayed),
    }


def get_migration_plan(db: Session, *, actor: User, plan_id: str) -> dict[str, Any]:
    plan = db.get(ArchiveMigrationPlan, str(plan_id))
    if plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    _require_plan_access(actor, plan)
    _refresh_plan_expiry(db, plan)
    return public_migration_plan(plan)


def list_migration_items(
    db: Session,
    *,
    actor: User,
    plan_id: str,
    cursor: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    plan = db.get(ArchiveMigrationPlan, str(plan_id))
    if plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    _require_plan_access(actor, plan)
    bounded_limit = max(1, min(ITEM_PAGE_MAX, int(limit)))
    rows = (
        db.query(ArchiveMigrationItem)
        .filter(
            ArchiveMigrationItem.plan_id == str(plan.id),
            ArchiveMigrationItem.item_index > max(0, int(cursor)),
        )
        .order_by(ArchiveMigrationItem.item_index.asc())
        .limit(bounded_limit + 1)
        .all()
    )
    has_more = len(rows) > bounded_limit
    visible = rows[:bounded_limit]
    return {
        "plan_id": str(plan.id),
        "items": [
            {
                "item_index": int(item.item_index),
                "segment_id": int(item.segment_id),
                "camera_id": int(item.camera_id),
                "camera_name": item.camera_name_snapshot,
                "size_bytes": int(item.source_size_bytes),
                "phase": str(item.phase),
                "result_code": item.result_code,
                "cleanup_pending": bool(item.cleanup_pending),
            }
            for item in visible
        ],
        "next_cursor": int(visible[-1].item_index) if has_more and visible else None,
        "has_more": has_more,
    }


def queue_migration_apply(
    db: Session,
    *,
    actor: User,
    plan_id: str,
    expected_hash: str,
    idempotency_key: str,
) -> dict[str, Any]:
    plan = db.get(ArchiveMigrationPlan, str(plan_id))
    if plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    actor_is_plan_owner = _actor_matches_plan(actor, plan)
    if actor_is_plan_owner:
        _require_initial_apply_authority(actor, plan)
    _require_plan_access(actor, plan)
    if not actor_is_plan_owner:
        _require_initial_apply_authority(actor, plan)
    _refresh_plan_expiry(db, plan)
    if plan.status not in PLAN_READY_STATUSES:
        if plan.current_operation_id:
            operation = db.get(StorageOperation, plan.current_operation_id)
            if operation is not None:
                return {
                    "plan": public_migration_plan(plan, replayed=True),
                    "operation": public_operation_summary(operation, now=database_now(db)),
                    "replayed": True,
                }
        raise ArchiveMigrationBlocked(plan.reason_code or "migration_plan_not_ready")
    if not plan.canonical_hash or str(expected_hash or "") != str(plan.canonical_hash):
        raise ArchiveMigrationBlocked("migration_plan_stale_or_tampered", retry_mode="refresh")
    source = _root_by_id(db, plan.source_root_id)
    target = _root_by_id(db, plan.target_root_id)
    try:
        assert_root_snapshot(source, _snapshot_from_plan(plan, source=True), require_write=True)
        assert_root_snapshot(target, _snapshot_from_plan(plan, source=False), require_write=True)
    except StorageFilesystemError as exc:
        raise ArchiveMigrationBlocked(
            _public_migration_reason(exc, fallback="migration_root_revalidation_failed"),
            retry_mode="refresh",
        ) from exc
    except Exception as exc:
        raise ArchiveMigrationBlocked("migration_root_revalidation_failed", retry_mode="refresh") from exc
    initial_orphan = _resolve_exact_initial_migration_child(db, plan=plan)
    if initial_orphan is not None:
        bound_plan, bound_child, _already_bound = _bind_exact_initial_migration_child(
            db,
            plan_id=str(plan.id),
            expected_child_id=str(initial_orphan.id),
            audit_actor=actor,
            repair_origin="endpoint",
        )
        return {
            "plan": public_migration_plan(bound_plan, replayed=True),
            "operation": public_operation_summary(bound_child, now=database_now(db)),
            "replayed": True,
        }
    try:
        claimed = claim_operation_with_conflicts(
            db,
            operation_type=MIGRATION_OPERATION_TYPE,
            scope={"root_ids": [str(plan.source_root_id), str(plan.target_root_id)]},
            request_identity=_operation_identity(plan),
            actor=actor,
            idempotency_key=str(idempotency_key),
            initial_progress=_operation_progress(plan, phase="queued"),
            start_immediately=False,
            cancel_allowed=True,
            domain_ref=_operation_domain_ref(str(plan.id)),
        )
    except StorageOperationConflict:
        db.rollback()
        db.expire_all()
        refreshed_plan = db.get(ArchiveMigrationPlan, str(plan.id))
        if refreshed_plan is None:
            raise
        expected_child = None
        if refreshed_plan.current_operation_id is not None:
            expected_child = db.get(
                StorageOperation,
                str(refreshed_plan.current_operation_id),
            )
        else:
            expected_child = _resolve_exact_initial_migration_child(db, plan=refreshed_plan)
        if expected_child is None:
            raise
        bound_plan, bound_child, _already_bound = _bind_exact_initial_migration_child(
            db,
            plan_id=str(refreshed_plan.id),
            expected_child_id=str(expected_child.id),
            audit_actor=actor,
            repair_origin="endpoint" if refreshed_plan.current_operation_id is None else None,
        )
        return {
            "plan": public_migration_plan(bound_plan, replayed=True),
            "operation": public_operation_summary(bound_child, now=database_now(db)),
            "replayed": True,
        }
    operation = dict(claimed.get("operation") or {})
    if claimed.get("state") == "terminal":
        raise ArchiveMigrationBlocked("migration_initial_child_ambiguous", retry_mode=None)
    operation_row = db.get(StorageOperation, str(operation.get("operation_id") or ""))
    if operation_row is None:
        raise ArchiveMigrationBlocked("migration_operation_identity_invalid", retry_mode=None)
    bound_plan, bound_child, already_bound = _bind_exact_initial_migration_child(
        db,
        plan_id=str(plan.id),
        expected_child_id=str(operation_row.id),
        audit_actor=actor,
        repair_origin=None,
    )
    return {
        "plan": public_migration_plan(bound_plan, replayed=already_bound),
        "operation": public_operation_summary(bound_child, now=database_now(db)),
        "replayed": bool(already_bound),
    }






def _stat_matches_source(item: ArchiveMigrationItem, current: os.stat_result) -> bool:
    return _migration_file_executor()._stat_matches_source(item, current)


def _stat_matches_target_provenance(item: ArchiveMigrationItem, current: os.stat_result) -> bool:
    return _migration_file_executor()._stat_matches_target_provenance(item, current)


def _pending_temp_stat_is_exact(current: os.stat_result) -> bool:
    return _migration_file_executor()._pending_temp_stat_is_exact(current)


def _raise_pending_temp_ambiguous() -> None:
    return _migration_file_executor()._raise_pending_temp_ambiguous()


def _stat_pending_temp(root_fd: int, item: ArchiveMigrationItem) -> os.stat_result:
    return _migration_file_executor()._stat_pending_temp(root_fd, item)


def _open_pending_temp_for_recovery(root_fd: int, item: ArchiveMigrationItem) -> int:
    return _migration_file_executor()._open_pending_temp_for_recovery(root_fd, item)


def _stage_temp_create_intent(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    handle: OperationHandle,
) -> None:
    return _migration_file_executor()._stage_temp_create_intent(db, plan, item, handle)


def _verify_durable_final_target(item: ArchiveMigrationItem, target: ArchiveRoot) -> None:
    return _migration_file_executor()._verify_durable_final_target(item, target)


def _runtime_item_guard(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
) -> tuple[ArchiveRoot, ArchiveRoot, RecordingSegment]:
    _require_runtime_authority(db, plan)
    if not _item_manifest_valid(item):
        raise ArchiveMigrationBlocked("migration_manifest_item_tampered", retry_mode=None)
    source = _root_by_id(db, item.source_root_id)
    target = _root_by_id(db, item.target_root_id)
    assert_root_snapshot(source, _item_snapshot(item, source=True), require_write=True)
    target_current = assert_root_snapshot(target, _item_snapshot(item, source=False), require_write=True)
    segment = db.get(RecordingSegment, int(item.segment_id))
    if segment is None:
        raise ArchiveMigrationBlocked("migration_segment_missing", retry_mode=None)
    if item.phase in {
        "planned",
        "target_temp_create_pending",
        "copying",
        "target_temp_written",
        "target_verified",
        "target_finalized",
    }:
        if (
            str(segment.archive_root_id or "") != str(item.source_root_id)
            or str(segment.relative_path or "") != str(item.source_relative_path)
            or _segment_metadata_fingerprint(segment) != str(item.source_metadata_fingerprint)
            or str(segment.status or "") not in ELIGIBLE_SEGMENT_STATUSES
        ):
            raise ArchiveMigrationBlocked("migration_segment_changed_after_plan", retry_mode="refresh_plan")
        if segment.job_id:
            active = (
                db.query(RecordingJob.id)
                .filter(
                    RecordingJob.id == str(segment.job_id),
                    RecordingJob.state.in_(tuple(ACTIVE_JOB_STATES)),
                )
                .first()
            )
            if active is not None:
                raise ArchiveMigrationBlocked("migration_segment_became_active", retry_mode="refresh_plan")
    durable_target_bytes = 0
    if item.phase in {
        "target_temp_create_pending",
        "copying",
        "target_temp_written",
        "target_verified",
        "target_finalized",
        "metadata_switched",
        "source_cleanup_pending",
        "source_quarantined",
        "source_delete_committing",
    }:
        durable_target_bytes = min(int(item.source_size_bytes), max(0, int(item.transferred_bytes or 0)))
    remaining = max(
        0,
        int(plan.total_bytes or 0) - int(plan.completed_bytes or 0) - durable_target_bytes,
    )
    current_growth = max(0, int(item.source_size_bytes) - durable_target_bytes)
    required = migration_required_free_bytes(
        same_physical_volume=bool(plan.same_physical_volume),
        remaining_not_target_finalized_bytes=remaining,
        largest_next_item_size_bytes=current_growth,
        reserve_bytes=migration_reserve_bytes(int(target_current["total_bytes"])),
    )
    if item.phase in {"planned", "target_temp_create_pending", "copying"} and int(target_current["free_bytes"]) < required:
        raise ArchiveMigrationBlocked("migration_insufficient_target_space", retry_mode="after_free_space")
    return source, target, segment


def _copy_or_resume_temp(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    source: ArchiveRoot,
    target: ArchiveRoot,
    handle: OperationHandle,
) -> None:
    return _migration_file_executor()._copy_or_resume_temp(db, plan, item, source, target, handle)


def _verify_and_finalize_target(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    source: ArchiveRoot,
    target: ArchiveRoot,
    handle: OperationHandle,
) -> None:
    return _migration_file_executor()._verify_and_finalize_target(db, plan, item, source, target, handle)


def _switch_metadata(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    handle: OperationHandle,
) -> None:
    return _migration_file_executor()._switch_metadata(db, plan, item, handle)


def _cleanup_source(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    source: ArchiveRoot,
    handle: OperationHandle,
) -> None:
    return _migration_file_executor()._cleanup_source(db, plan, item, source, handle)


def _process_item(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    handle: OperationHandle,
) -> None:
    source, target, _segment = _runtime_item_guard(db, plan, item)
    if item.phase in {"planned", "target_temp_create_pending", "copying"}:
        _copy_or_resume_temp(db, plan, item, source, target, handle)
        item = db.get(ArchiveMigrationItem, item.id)
    if item.phase in {"target_temp_written", "target_verified"}:
        _verify_and_finalize_target(db, plan, item, source, target, handle)
        item = db.get(ArchiveMigrationItem, item.id)
    if item.phase == "target_finalized":
        _switch_metadata(db, plan, item, handle)
        item = db.get(ArchiveMigrationItem, item.id)
    if item.phase in {"metadata_switched", "source_cleanup_pending", "source_quarantined", "source_delete_committing"}:
        _cleanup_source(db, plan, item, source, handle)
    if db.get(ArchiveMigrationItem, item.id).phase != "completed":
        raise ArchiveMigrationPartial("migration_item_terminal_truth_incomplete", retry_mode="cleanup_only")


def _remove_owned_target_residue(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    target: ArchiveRoot,
    handle: OperationHandle,
    *,
    final: bool,
) -> bool:
    return _migration_file_executor()._remove_owned_target_residue(db, plan, item, target, handle, final=final)


def _cleanup_retry_item(
    db: Session,
    plan: ArchiveMigrationPlan,
    item: ArchiveMigrationItem,
    handle: OperationHandle,
) -> None:
    return _migration_cleanup_retry()._cleanup_retry_item(db, plan, item, handle)


def _execute_cleanup_only_retry(
    db: Session,
    plan: ArchiveMigrationPlan,
    handle: OperationHandle,
) -> None:
    return _migration_cleanup_retry()._execute_cleanup_only_retry(db, plan, handle)


def _operation_progress(
    plan: ArchiveMigrationPlan,
    *,
    phase: str,
    current_item: ArchiveMigrationItem | None = None,
    speed_bytes_per_second: int | None = None,
    eta_seconds: int | None = None,
) -> dict[str, Any]:
    speed = None
    eta = None
    if phase == "copying" and speed_bytes_per_second is not None:
        candidate_speed = int(speed_bytes_per_second)
        if 0 < candidate_speed <= THROUGHPUT_MAX_BYTES_PER_SECOND:
            speed = candidate_speed
    if phase == "copying" and eta_seconds is not None:
        candidate_eta = int(eta_seconds)
        if 0 <= candidate_eta <= THROUGHPUT_MAX_ETA_SECONDS:
            eta = candidate_eta
    return {
        "plan_id": str(plan.id),
        "phase": phase,
        "completed_count": int(plan.completed_count or 0),
        "item_count": int(plan.item_count or 0),
        "completed_bytes": int(plan.completed_bytes or 0),
        "total_bytes": int(plan.total_bytes or 0),
        "current_item_index": int(current_item.item_index) if current_item else None,
        "current_item_bytes": int(current_item.transferred_bytes or 0) if current_item else None,
        "speed_bytes_per_second": int(speed) if speed is not None else None,
        "eta_seconds": int(eta) if eta is not None else None,
        "permission_contract": _plan_permission_contract(plan),
    }


def _recount_plan(
    db: Session,
    plan: ArchiveMigrationPlan,
    *,
    handle: OperationHandle | None = None,
    commit: bool = True,
) -> None:
    if handle is not None:
        _operation, plan = _lock_owned_migration_plan(
            db,
            plan,
            handle,
            require_runtime_authority=False,
        )
    completed_count, completed_bytes = (
        db.query(
            func.count(ArchiveMigrationItem.id),
            func.coalesce(func.sum(ArchiveMigrationItem.source_size_bytes), 0),
        )
        .filter(ArchiveMigrationItem.plan_id == plan.id, ArchiveMigrationItem.phase == "completed")
        .one()
    )
    plan.completed_count = int(completed_count or 0)
    plan.completed_bytes = int(completed_bytes or 0)
    plan.failed_count = int(
        db.query(func.count(ArchiveMigrationItem.id))
        .filter(ArchiveMigrationItem.plan_id == plan.id, ArchiveMigrationItem.phase.in_(("failed", "blocked")))
        .scalar()
        or 0
    )
    plan.cancelled_count = int(
        db.query(func.count(ArchiveMigrationItem.id))
        .filter(ArchiveMigrationItem.plan_id == plan.id, ArchiveMigrationItem.phase == "cancelled")
        .scalar()
        or 0
    )
    plan.cleanup_pending = bool(
        db.query(ArchiveMigrationItem.id)
        .filter(ArchiveMigrationItem.plan_id == plan.id, ArchiveMigrationItem.cleanup_pending.is_(True))
        .first()
    )
    db.add(plan)
    if commit:
        db.commit()


def _final_counts(db: Session, plan: ArchiveMigrationPlan) -> None:
    plan.new_after_high_watermark_count = int(
        db.query(func.count(RecordingSegment.id))
        .filter(
            RecordingSegment.archive_root_id == plan.source_root_id,
            RecordingSegment.id > int(plan.segment_high_watermark),
            RecordingSegment.deleted_at.is_(None),
            RecordingSegment.status != "deleted",
        )
        .scalar()
        or 0
    )
    plan.retained_source_count = int(
        db.query(func.count(RecordingSegment.id))
        .filter(
            RecordingSegment.archive_root_id == plan.source_root_id,
            RecordingSegment.deleted_at.is_(None),
            RecordingSegment.status != "deleted",
        )
        .scalar()
        or 0
    )


def _terminalize_operation(
    db: Session,
    plan: ArchiveMigrationPlan,
    handle: OperationHandle,
    *,
    status: str,
    reason_code: str | None = None,
    retry_mode: str | None = None,
) -> None:
    operation, plan = _lock_owned_migration_plan(
        db,
        plan,
        handle,
        require_runtime_authority=False,
    )
    was_cleanup_retry = str(plan.retry_mode or "") == "cleanup_only"
    was_cleanup_takeover = bool(
        operation is not None and _operation_is_authorized_cleanup_takeover(operation, plan)
    )
    _recount_plan(db, plan, commit=False)
    _final_counts(db, plan)
    plan.status = status
    plan.phase = status
    plan.reason_code = reason_code
    plan.retry_mode = retry_mode
    plan.next_action = (
        "retry_cleanup" if retry_mode == "cleanup_only" else "retry_migration" if retry_mode else None
    )
    plan.finished_at = database_now(db)
    db.add(plan)
    result = {
        "status": status,
        "plan_id": str(plan.id),
        "completed_count": int(plan.completed_count or 0),
        "item_count": int(plan.item_count or 0),
        "completed_bytes": int(plan.completed_bytes or 0),
        "total_bytes": int(plan.total_bytes or 0),
        "failed_count": int(plan.failed_count or 0),
        "cancelled_count": int(plan.cancelled_count or 0),
        "excluded_count": int(plan.excluded_count or 0),
        "cleanup_pending": bool(plan.cleanup_pending),
        "new_after_high_watermark_count": int(plan.new_after_high_watermark_count or 0),
        "retained_source_count": int(plan.retained_source_count or 0),
        "permission_contract": _plan_permission_contract(plan),
    }
    _audit(
        db,
        event_type=f"archive_migration.operation_{status}",
        plan=plan,
        operation=operation,
        severity="info" if status == "completed" else "warning",
        metadata={"reason_code": reason_code, "cleanup_pending": bool(plan.cleanup_pending)},
    )
    if was_cleanup_retry:
        _audit(
            db,
            event_type="archive_migration.cleanup_retry_outcome",
            plan=plan,
            operation=operation,
            severity="info" if status == "completed" else "warning",
            metadata={"status": status, "cleanup_pending": bool(plan.cleanup_pending)},
        )
    if operation is not None and was_cleanup_takeover:
        continuation = _cleanup_continuation_snapshot(operation, plan)
        recovery_actor_user_id = (
            int(continuation["actor_user_id"])
            if continuation is not None
            else int(operation.actor_user_id or 0)
        )
        recovery_actor = db.get(User, recovery_actor_user_id) if recovery_actor_user_id else None
        _audit_cleanup_takeover(
            db,
            actor=recovery_actor,
            operation=operation,
            plan=plan,
            event_type="archive_migration.cleanup_takeover_outcome",
            severity="info" if status == "completed" else "warning",
            metadata={
                "status": status,
                "reason_code": reason_code,
                "cleanup_pending": bool(plan.cleanup_pending),
            },
        )
    if operation is not None:
        continuation = _cleanup_continuation_snapshot(operation, plan)
        if continuation is not None:
            attempt_actor = db.get(User, int(continuation["actor_user_id"]))
            _ensure_cleanup_attempt_audit(
                db,
                actor=attempt_actor,
                operation=operation,
                plan=plan,
                event_type="archive_migration.cleanup_continuation_outcome",
                status=status,
                severity="info" if status == "completed" else "warning",
            )
    stage_operation_terminal(
        db,
        handle,
        status=status,
        result=result,
        progress=_operation_progress(plan, phase=status),
        reason_code=reason_code,
        next_action=plan.next_action,
        retry_mode=retry_mode,
        retry_allowed=bool(retry_mode),
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    terminal_operation = db.get(StorageOperation, str(handle.operation_id))
    ensure_operation_terminal_audit(db, terminal_operation)


def _cancel_planned_items(
    db: Session,
    plan: ArchiveMigrationPlan,
    *,
    handle: OperationHandle | None = None,
) -> int:
    cancelled = 0
    while True:
        rows = (
            db.query(ArchiveMigrationItem)
            .filter(
                ArchiveMigrationItem.plan_id == plan.id,
                ArchiveMigrationItem.phase == "planned",
            )
            .order_by(ArchiveMigrationItem.item_index.asc())
            .limit(PLAN_BATCH_SIZE)
            .all()
        )
        if not rows:
            break
        if handle is not None:
            _operation, plan = _lock_owned_migration_plan(
                db,
                plan,
                handle,
                require_runtime_authority=False,
            )
        now = database_now(db)
        for item in rows:
            item.phase = "cancelled"
            item.result_code = "cancelled_before_start"
            item.cleanup_pending = False
            item.completed_at = now
            db.add(item)
            cancelled += 1
        db.commit()
    return cancelled


def _operation_permission_contract_valid(operation: StorageOperation | None, plan: ArchiveMigrationPlan) -> bool:
    if operation is None:
        return False
    progress = dict(operation.progress or {})
    return progress.get("permission_contract") == _plan_permission_contract(plan)


def _execute_operation(db: Session, plan: ArchiveMigrationPlan, handle: OperationHandle) -> None:
    if plan.status in PLAN_TERMINAL_STATUSES:
        _terminalize_operation(
            db,
            plan,
            handle,
            status=str(plan.status),
            reason_code=plan.reason_code,
            retry_mode=plan.retry_mode,
        )
        return
    operation, plan = _lock_owned_migration_plan(
        db,
        plan,
        handle,
        require_runtime_authority=False,
    )
    if not _operation_permission_contract_valid(operation, plan):
        _terminalize_operation(
            db,
            plan,
            handle,
            status="blocked",
            reason_code="migration_authority_contract_invalid",
            retry_mode=None,
        )
        return
    if not _manifest_integrity_valid(db, plan):
        _terminalize_operation(
            db,
            plan,
            handle,
            status="blocked",
            reason_code="migration_manifest_tampered",
            retry_mode=None,
        )
        return
    try:
        _require_runtime_authority(db, plan)
    except ArchiveMigrationBlocked as exc:
        _terminalize_operation(
            db,
            plan,
            handle,
            status="blocked",
            reason_code=exc.reason_code,
            retry_mode=exc.retry_mode,
        )
        return
    _operation, plan = _lock_owned_migration_plan(db, plan, handle)
    plan.status = "running"
    plan.phase = "running"
    plan.heartbeat_at = database_now(db)
    _audit(
        db,
        event_type="archive_migration.apply_started",
        plan=plan,
        operation=operation,
    )
    heartbeat_operation(db, handle, progress=_operation_progress(plan, phase="running"))
    if plan.retry_mode == "cleanup_only":
        _execute_cleanup_only_retry(db, plan, handle)
        return
    while True:
        item = (
            db.query(ArchiveMigrationItem)
            .filter(
                ArchiveMigrationItem.plan_id == plan.id,
                ArchiveMigrationItem.phase.notin_(tuple(ITEM_TERMINAL_PHASES)),
            )
            .order_by(ArchiveMigrationItem.item_index.asc())
            .first()
        )
        if item is None:
            _recount_plan(db, plan, handle=handle)
            if int(plan.completed_count or 0) == int(plan.item_count or 0) and not plan.cleanup_pending:
                _terminalize_operation(db, plan, handle, status="completed")
            elif plan.cancel_requested and int(plan.cancelled_count or 0) > 0 and not plan.cleanup_pending:
                _terminalize_operation(db, plan, handle, status="cancelled")
            else:
                _terminalize_operation(
                    db,
                    plan,
                    handle,
                    status="partial",
                    reason_code="migration_manifest_incomplete",
                    retry_mode="cleanup_only" if plan.cleanup_pending else "refresh",
                )
            return
        if operation_cancel_requested(db, handle) and item.phase == "planned":
            _cancel_planned_items(db, plan, handle=handle)
            _recount_plan(db, plan, handle=handle)
            if plan.cleanup_pending:
                _terminalize_operation(
                    db,
                    plan,
                    handle,
                    status="partial",
                    reason_code="migration_cancel_cleanup_pending",
                    retry_mode="cleanup_only",
                )
            elif int(plan.completed_count or 0) == int(plan.item_count or 0):
                _terminalize_operation(db, plan, handle, status="completed")
            else:
                _terminalize_operation(db, plan, handle, status="cancelled")
            return
        try:
            _process_item(db, plan, item, handle)
            _recount_plan(db, plan, handle=handle)
            heartbeat_operation(db, handle, progress=_operation_progress(plan, phase="running"))
        except ArchiveMigrationBlocked as exc:
            db.rollback()
            item = db.get(ArchiveMigrationItem, item.id)
            _operation, plan, item = _lock_owned_migration_item(
                db,
                plan,
                item,
                handle,
                require_runtime_authority=False,
            )
            original_phase = str(item.phase)
            item.last_reason_code = exc.reason_code
            item.retry_mode = exc.retry_mode
            side_effect = original_phase not in {"planned"}
            item.result_code = "migration_item_interrupted" if side_effect else "migration_item_blocked"
            item.cleanup_pending = bool(
                item.cleanup_pending
                or original_phase
                in {
                    "target_temp_create_pending",
                    "copying",
                    "target_temp_written",
                    "target_verified",
                    "target_finalized",
                    "metadata_switched",
                    "source_cleanup_pending",
                    "source_quarantined",
                    "source_delete_committing",
                }
            )
            db.add(item)
            db.commit()
            _recount_plan(db, plan, handle=handle)
            status = "partial" if (int(plan.completed_count or 0) > 0 or side_effect or plan.cleanup_pending) else "blocked"
            _terminalize_operation(
                db,
                plan,
                handle,
                status=status,
                reason_code=exc.reason_code,
                retry_mode=exc.retry_mode,
            )
            return
        except (OSError, StorageFilesystemError) as exc:
            db.rollback()
            item = db.get(ArchiveMigrationItem, item.id)
            _operation, plan, item = _lock_owned_migration_item(
                db,
                plan,
                item,
                handle,
                require_runtime_authority=False,
            )
            original_phase = str(item.phase)
            item.last_reason_code = "migration_filesystem_failure"
            item.retry_mode = "cleanup_only" if item.phase != "planned" else "refresh"
            side_effect = original_phase != "planned"
            item.result_code = "migration_filesystem_interrupted"
            item.cleanup_pending = bool(item.cleanup_pending or side_effect)
            db.add(item)
            db.commit()
            _recount_plan(db, plan, handle=handle)
            _terminalize_operation(
                db,
                plan,
                handle,
                status="partial" if side_effect or int(plan.completed_count or 0) else "failed",
                reason_code="migration_filesystem_failure",
                retry_mode=item.retry_mode,
            )
            return


def _reset_worker_candidate_scan_state() -> None:
    global _worker_candidate_cursor
    with _worker_candidate_state_lock:
        _worker_candidate_cursor = None
        _worker_candidate_diagnostics.clear()


def _worker_candidate_cursor_snapshot() -> tuple[datetime, str] | None:
    with _worker_candidate_state_lock:
        return _worker_candidate_cursor


def _set_worker_candidate_cursor(cursor: tuple[datetime, str] | None) -> None:
    global _worker_candidate_cursor
    with _worker_candidate_state_lock:
        _worker_candidate_cursor = cursor


def _worker_candidate_reference(operation_id: Any) -> str:
    value = str(operation_id or "")
    if value and len(value) <= 96 and all(
        char.isascii() and (char.isalnum() or char in "-_.:")
        for char in value
    ):
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"sha256:{digest}"


def _worker_candidate_reason(exc: Exception) -> str:
    if isinstance(exc, (ArchiveMigrationBlocked, _WorkerCandidateRejected)):
        return safe_reason_code(exc.reason_code, fallback="migration_worker_candidate_rejected") or "migration_worker_candidate_rejected"
    if isinstance(exc, StorageOperationConflict):
        return safe_reason_code(exc.detail, fallback="storage_operation_scope_conflict") or "storage_operation_scope_conflict"
    return safe_reason_code(str(exc), fallback="migration_worker_candidate_rejected") or "migration_worker_candidate_rejected"


def _log_worker_candidate_rejection(operation_id: Any, reason_code: str) -> None:
    operation_ref = _worker_candidate_reference(operation_id)
    reason = safe_reason_code(reason_code, fallback="migration_worker_candidate_rejected") or "migration_worker_candidate_rejected"
    key = (operation_ref, reason)
    now = time.monotonic()
    with _worker_candidate_state_lock:
        previous = _worker_candidate_diagnostics.get(key)
        if previous is not None and now - previous < WORKER_CANDIDATE_DIAGNOSTIC_INTERVAL_SECONDS:
            return
        if len(_worker_candidate_diagnostics) >= WORKER_CANDIDATE_DIAGNOSTIC_MAX_KEYS:
            oldest = min(_worker_candidate_diagnostics, key=_worker_candidate_diagnostics.get)
            _worker_candidate_diagnostics.pop(oldest, None)
        _worker_candidate_diagnostics[key] = now
    logger.warning(
        "Archive migration worker skipped candidate operation_id=%s reason_code=%s",
        operation_ref,
        reason,
    )


def _worker_candidate_is_selectable(row: StorageOperation, now: datetime) -> bool:
    return bool(
        str(row.operation_type) == MIGRATION_OPERATION_TYPE
        and str(row.status) in ACTIVE_OPERATION_STATUSES
        and (
            str(row.status) == "queued"
            or int(row.fencing_token or 0) == 0
            or row.lease_expires_at is None
            or row.lease_expires_at <= now
        )
    )


def _load_worker_candidate_batch(
    cursor: tuple[datetime, str] | None,
) -> list[tuple[str, datetime]]:
    with SessionLocal() as selection_db:
        now = database_now(selection_db)
        query = selection_db.query(StorageOperation.id, StorageOperation.queued_at).filter(
            StorageOperation.operation_type == MIGRATION_OPERATION_TYPE,
            StorageOperation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
            or_(
                StorageOperation.status == "queued",
                StorageOperation.fencing_token == 0,
                StorageOperation.lease_expires_at.is_(None),
                StorageOperation.lease_expires_at <= now,
            ),
        )
        if cursor is not None:
            queued_at, operation_id = cursor
            query = query.filter(
                or_(
                    StorageOperation.queued_at > queued_at,
                    and_(
                        StorageOperation.queued_at == queued_at,
                        StorageOperation.id > operation_id,
                    ),
                )
            )
        rows = (
            query.order_by(StorageOperation.queued_at.asc(), StorageOperation.id.asc())
            .limit(WORKER_CANDIDATE_BATCH_SIZE)
            .all()
        )
        return [(str(row.id), row.queued_at) for row in rows]


def _claim_next_operation() -> tuple[Session, ArchiveMigrationPlan, OperationHandle] | None:
    cursor = _worker_candidate_cursor_snapshot()
    try:
        candidates = _load_worker_candidate_batch(cursor)
    except Exception:
        _log_worker_candidate_rejection(
            "worker-poll",
            "migration_worker_candidate_infrastructure_failure",
        )
        return None
    if not candidates:
        if cursor is not None:
            _set_worker_candidate_cursor(None)
        return None

    controlled_errors = (
        ArchiveMigrationBlocked,
        _WorkerCandidateRejected,
        StorageOperationConflict,
        StorageOperationContractError,
        StorageOperationLeaseLost,
    )
    for candidate_id, candidate_queued_at in candidates:
        db = SessionLocal()
        try:
            row = db.get(StorageOperation, candidate_id)
            if row is None:
                raise _WorkerCandidateRejected("migration_worker_candidate_missing")
            now = database_now(db)
            if not _worker_candidate_is_selectable(row, now):
                raise _WorkerCandidateRejected("migration_worker_candidate_stale")
            if not row.domain_ref or not row.domain_ref.startswith("migration-plan:"):
                raise _WorkerCandidateRejected("migration_operation_identity_invalid")
            plan_id = row.domain_ref.split(":", 1)[1]
            plan = db.get(ArchiveMigrationPlan, plan_id)
            if plan is None:
                raise _WorkerCandidateRejected("migration_plan_not_found")
            if str(plan.current_operation_id or "") != str(row.id):
                if row.parent_operation_id is None:
                    plan, row, _already_bound = _bind_exact_initial_migration_child(
                        db,
                        plan_id=str(plan.id),
                        expected_child_id=str(row.id),
                        audit_actor=None,
                        repair_origin="system_worker",
                    )
                else:
                    previous = db.get(StorageOperation, str(row.parent_operation_id))
                    if previous is None or str(plan.current_operation_id or "") != str(previous.id):
                        raise _WorkerCandidateRejected("migration_retry_child_ambiguous")
                    row = _adopt_exact_orphan_migration_child(
                        db,
                        plan=plan,
                        previous=previous,
                        expected_child_id=str(row.id),
                        repair_actor=None,
                        repair_intent="system_worker",
                    )
                plan = db.get(ArchiveMigrationPlan, plan_id)
                if plan is None or str(plan.current_operation_id or "") != str(row.id):
                    raise _WorkerCandidateRejected("migration_retry_child_ambiguous")
            claimed = reclaim_operation_with_conflicts(
                db,
                operation_id=str(row.id),
                operation_type=MIGRATION_OPERATION_TYPE,
                request_identity=_operation_identity(plan),
                idempotency_key=str(row.idempotency_key),
                owner_instance_id=f"archive-migration:{os.getpid()}:{threading.get_ident()}",
            )
            if claimed.get("state") != "claimed":
                raise _WorkerCandidateRejected("migration_worker_candidate_not_claimed")
            _set_worker_candidate_cursor(None)
            return db, plan, claimed["handle"]
        except controlled_errors as exc:
            db.rollback()
            db.close()
            _set_worker_candidate_cursor((candidate_queued_at, candidate_id))
            _log_worker_candidate_rejection(candidate_id, _worker_candidate_reason(exc))
            continue
        except Exception:
            db.rollback()
            db.close()
            _log_worker_candidate_rejection(
                candidate_id,
                "migration_worker_candidate_infrastructure_failure",
            )
            return None
    return None


def _run_one_operation() -> bool:
    claimed = _claim_next_operation()
    if claimed is None:
        return False
    db, plan, handle = claimed
    try:
        _execute_operation(db, plan, handle)
    except _MigrationAuditPersistenceFailed:
        db.rollback()
    except (StorageOperationLeaseLost, StorageOperationConflict):
        db.rollback()
    except Exception:
        db.rollback()
        current = db.get(ArchiveMigrationPlan, plan.id)
        operation = db.get(StorageOperation, handle.operation_id)
        if current is not None and operation is not None and operation.status in ACTIVE_OPERATION_STATUSES:
            try:
                terminal_status = str(current.status) if current.status in PLAN_TERMINAL_STATUSES else None
                cleanup_required = _cleanup_takeover_work_exists(db, current)
                if cleanup_required:
                    _operation, current = _lock_owned_migration_plan(
                        db,
                        current,
                        handle,
                        require_runtime_authority=False,
                    )
                    current.cleanup_pending = True
                    db.add(current)
                    db.commit()
                _terminalize_operation(
                    db,
                    current,
                    handle,
                    status=terminal_status or (
                        "partial" if current.completed_count or current.cleanup_pending or cleanup_required else "failed"
                    ),
                    reason_code=current.reason_code if terminal_status else "migration_worker_failure",
                    retry_mode=current.retry_mode if terminal_status else (
                        "cleanup_only" if current.cleanup_pending or cleanup_required else "refresh"
                    ),
                )
            except Exception:
                db.rollback()
    finally:
        db.close()
    return True


def cancel_migration_operation(db: Session, *, actor: User, operation_id: str) -> dict[str, Any]:
    operation = db.get(StorageOperation, str(operation_id))
    if operation is None or operation.operation_type != MIGRATION_OPERATION_TYPE:
        raise ArchiveMigrationBlocked("migration_operation_not_found", retry_mode=None)
    if not operation.domain_ref or not operation.domain_ref.startswith("migration-plan:"):
        raise ArchiveMigrationBlocked("migration_operation_identity_invalid", retry_mode=None)
    plan = db.get(ArchiveMigrationPlan, operation.domain_ref.split(":", 1)[1])
    if plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    _require_plan_access(actor, plan)
    summary = request_operation_cancel(
        db,
        str(operation.id),
        actor=actor,
        allow_admin_override=True,
    )
    if summary.get("status") == "cancelled":
        _cancel_planned_items(db, plan)
        _recount_plan(db, plan)
        plan.status = "cancelled"
        plan.phase = "cancelled"
        plan.cancel_requested = True
        plan.finished_at = database_now(db)
    else:
        plan.cancel_requested = True
        plan.phase = "cancel_requested"
    _audit(
        db,
        actor=actor,
        event_type="archive_migration.cancel_requested",
        plan=plan,
        operation=operation,
        severity="warning",
    )
    db.commit()
    return {"plan": public_migration_plan(plan), "operation": summary}


def cancel_migration_plan(db: Session, *, actor: User, plan_id: str) -> dict[str, Any]:
    plan = db.get(ArchiveMigrationPlan, str(plan_id))
    if plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    _require_plan_access(actor, plan)
    if plan.current_operation_id:
        return cancel_migration_operation(
            db,
            actor=actor,
            operation_id=str(plan.current_operation_id),
        )
    if plan.status in PLAN_TERMINAL_STATUSES:
        return {"plan": public_migration_plan(plan, replayed=True), "operation": None}
    _cancel_planned_items(db, plan)
    _recount_plan(db, plan)
    plan.cancel_requested = True
    plan.status = "cancelled"
    plan.phase = "cancelled"
    plan.reason_code = "migration_cancelled_before_apply"
    plan.next_action = None
    plan.finished_at = database_now(db)
    _audit(db, actor=actor, event_type="archive_migration.cancel_requested", plan=plan, severity="warning")
    db.commit()
    return {"plan": public_migration_plan(plan), "operation": None}


def _operation_was_never_claimed(
    db: Session,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
) -> bool:
    progress = dict(operation.progress or {})
    if not (
        str(operation.status) == "queued"
        and int(operation.fencing_token or 0) == 0
        and operation.owner_token_hash is None
        and operation.owner_instance_id is None
        and operation.started_at is None
        and operation.heartbeat_at is None
        and operation.finished_at is None
        and operation.result is None
        and operation.reason_code is None
        and operation.next_action is None
        and operation.retry_mode is None
        and not bool(operation.retry_allowed)
        and bool(operation.cancel_allowed)
        and str(progress.get("plan_id") or "") == str(plan.id)
        and str(progress.get("phase") or "") == "queued"
        and progress.get("permission_contract") == _plan_permission_contract(plan)
    ):
        return False
    execution_audit = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == str(operation.id),
            AuditEvent.event_type.in_(tuple(ORPHAN_EXECUTION_AUDIT_EVENTS)),
        )
        .first()
    )
    return execution_audit is None


def _canonical_plan_actor_id(plan: ArchiveMigrationPlan) -> int | None:
    actor_key = str(plan.actor_key or "")
    if not actor_key.startswith("user:"):
        return None
    raw_id = actor_key.removeprefix("user:")
    if not raw_id or not raw_id.isascii() or not raw_id.isdigit():
        return None
    actor_id = int(raw_id)
    if actor_id <= 0 or raw_id != str(actor_id):
        return None
    if plan.actor_user_id is not None:
        try:
            if int(plan.actor_user_id) != actor_id:
                return None
        except (TypeError, ValueError):
            return None
    return actor_id


def _initial_child_has_execution_audit(
    db: Session,
    *,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
) -> bool:
    operation_id_value = AuditEvent.event_metadata["operation_id"].as_string()
    return bool(
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type.in_(tuple(INITIAL_ORPHAN_EXECUTION_AUDIT_EVENTS)),
            AuditEvent.target_type == "archive_migration_plan",
            AuditEvent.target_id == str(plan.id),
            operation_id_value == str(operation.id),
        )
        .limit(1)
        .first()
    )


def _expected_initial_operation_scope(
    db: Session,
    plan: ArchiveMigrationPlan,
) -> dict[str, Any]:
    return canonical_operation_scope(
        scope_with_physical_volumes(
            db,
            {"root_ids": [str(plan.source_root_id), str(plan.target_root_id)]},
        )
    )


def _initial_child_identity_matches(
    db: Session,
    *,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
    expected_scope: dict[str, Any] | None = None,
) -> bool:
    plan_actor_id = _canonical_plan_actor_id(plan)
    operation_actor_id = _canonical_user_operation_actor_id(operation)
    if plan_actor_id is None or operation_actor_id != plan_actor_id:
        return False
    if (plan.actor_user_id is None) != (operation.actor_user_id is None):
        return False
    try:
        operation_scope = canonical_operation_scope(operation.scope)
        canonical_expected_scope = expected_scope or _expected_initial_operation_scope(db, plan)
    except (StorageOperationContractError, StorageOperationConflict):
        return False
    return bool(
        plan.status in PLAN_READY_STATUSES
        and str(plan.phase) == "ready"
        and bool(plan.canonical_hash)
        and plan.current_operation_id is None
        and str(operation.actor_key) == str(plan.actor_key)
        and str(operation.operation_type) == MIGRATION_OPERATION_TYPE
        and str(operation.domain_ref or "") == _operation_domain_ref(str(plan.id))
        and str(operation.request_fingerprint) == request_fingerprint(_operation_identity(plan))
        and operation_scope == canonical_expected_scope
        and operation.parent_operation_id is None
        and int(operation.retry_depth or 0) == 0
        and _operation_was_never_claimed(db, operation, plan)
        and not _initial_child_has_execution_audit(db, operation=operation, plan=plan)
    )


def _resolve_exact_initial_migration_child(
    db: Session,
    *,
    plan: ArchiveMigrationPlan,
    lock: bool = False,
    expected_scope: dict[str, Any] | None = None,
) -> StorageOperation | None:
    if plan.current_operation_id is not None or plan.status not in PLAN_READY_STATUSES:
        return None
    query = db.query(StorageOperation).filter(
        StorageOperation.operation_type == MIGRATION_OPERATION_TYPE,
        StorageOperation.domain_ref == _operation_domain_ref(str(plan.id)),
        StorageOperation.parent_operation_id.is_(None),
        StorageOperation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
    )
    if lock:
        query = query.with_for_update()
    rows = (
        query.order_by(StorageOperation.created_at.asc(), StorageOperation.id.asc())
        .limit(2)
        .all()
    )
    if not rows:
        return None
    if len(rows) != 1 or not _initial_child_identity_matches(
        db,
        operation=rows[0],
        plan=plan,
        expected_scope=expected_scope,
    ):
        raise ArchiveMigrationBlocked("migration_initial_child_ambiguous", retry_mode=None)
    return rows[0]


def _ensure_initial_orphan_adoption_audit(
    db: Session,
    *,
    operation: StorageOperation,
    plan: ArchiveMigrationPlan,
    queued_actor_id: int,
    repair_actor: User | None,
    repair_origin: str,
) -> None:
    if repair_origin not in {"endpoint", "system_worker"}:
        raise _MigrationAuditPersistenceFailed()
    if (repair_origin == "endpoint") != (repair_actor is not None):
        raise _MigrationAuditPersistenceFailed()
    event_type = "archive_migration.initial_child_adopted"
    exists = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == str(operation.id),
        )
        .limit(1)
        .first()
    )
    if exists is not None:
        return
    event = create_event(
        db=db,
        actor=repair_actor,
        category="storage",
        event_type=event_type,
        severity="warning",
        message_ru="Восстановлена связь начальной операции переноса архива",
        message_en="Initial archive migration operation binding recovered",
        target_type="storage_operation",
        target_id=str(operation.id),
        metadata={
            "plan_id": str(plan.id),
            "repair_origin": repair_origin,
            "queued_actor_kind": str(operation.actor_kind),
            "queued_actor_user_id": int(queued_actor_id),
            "queued_actor_deleted": operation.actor_user_id is None,
        },
        commit=False,
    )
    if event is None:
        raise _MigrationAuditPersistenceFailed()


def _bind_exact_initial_migration_child(
    db: Session,
    *,
    plan_id: str,
    expected_child_id: str,
    audit_actor: User | None,
    repair_origin: str | None,
) -> tuple[ArchiveMigrationPlan, StorageOperation, bool]:
    if repair_origin not in {None, "endpoint", "system_worker"}:
        raise ArchiveMigrationBlocked("migration_initial_child_ambiguous", retry_mode=None)

    seed_plan = db.get(ArchiveMigrationPlan, str(plan_id))
    if seed_plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    scope = {"root_ids": [str(seed_plan.source_root_id), str(seed_plan.target_root_id)]}

    def transition(normalized_scope: dict[str, Any]) -> tuple[ArchiveMigrationPlan, StorageOperation, bool]:
        locked_plan = (
            db.query(ArchiveMigrationPlan)
            .filter(ArchiveMigrationPlan.id == str(plan_id))
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if locked_plan is None:
            raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
        if str(locked_plan.current_operation_id or "") == str(expected_child_id):
            bound_child = (
                db.query(StorageOperation)
                .filter(StorageOperation.id == str(expected_child_id))
                .populate_existing()
                .with_for_update()
                .one_or_none()
            )
            if not (
                bound_child is not None
                and str(bound_child.operation_type) == MIGRATION_OPERATION_TYPE
                and str(bound_child.domain_ref or "") == _operation_domain_ref(str(locked_plan.id))
                and str(bound_child.request_fingerprint)
                == request_fingerprint(_operation_identity(locked_plan))
                and str(bound_child.actor_key) == str(locked_plan.actor_key)
                and bound_child.parent_operation_id is None
                and int(bound_child.retry_depth or 0) == 0
            ):
                raise ArchiveMigrationBlocked("migration_initial_child_ambiguous", retry_mode=None)
            db.commit()
            return locked_plan, bound_child, True
        if locked_plan.current_operation_id is not None:
            raise ArchiveMigrationBlocked("migration_initial_child_ambiguous", retry_mode=None)
        child = _resolve_exact_initial_migration_child(
            db,
            plan=locked_plan,
            lock=True,
            expected_scope=canonical_operation_scope(normalized_scope),
        )
        if child is None or str(child.id) != str(expected_child_id):
            raise ArchiveMigrationBlocked("migration_initial_child_ambiguous", retry_mode=None)
        queued_actor_id = _canonical_user_operation_actor_id(child)
        if queued_actor_id is None:
            raise ArchiveMigrationBlocked("migration_initial_child_ambiguous", retry_mode=None)

        locked_plan.status = "queued"
        locked_plan.phase = "queued"
        locked_plan.current_operation_id = str(child.id)
        locked_plan.started_at = locked_plan.started_at or database_now(db)
        locked_plan.reason_code = None
        locked_plan.next_action = None
        locked_plan.retry_mode = None
        db.add(locked_plan)

        _ensure_operation_queue_audit(db, child, queued_actor_id=queued_actor_id)
        _audit(
            db,
            actor=audit_actor,
            event_type="archive_migration.apply_queued",
            plan=locked_plan,
            operation=child,
        )
        if repair_origin is not None:
            _ensure_initial_orphan_adoption_audit(
                db,
                operation=child,
                plan=locked_plan,
                queued_actor_id=queued_actor_id,
                repair_actor=audit_actor,
                repair_origin=repair_origin,
            )
        db.commit()
        return locked_plan, child, False

    return run_coordinated_operation_transition(
        db,
        operation_type=MIGRATION_OPERATION_TYPE,
        scope=scope,
        domain_ref=_operation_domain_ref(str(plan_id)),
        current_operation_id=str(expected_child_id),
        transition=transition,
        actor=audit_actor,
    )


def _exact_orphan_creator_evidence(
    child: StorageOperation,
    parent: StorageOperation,
    plan: ArchiveMigrationPlan,
) -> bool:
    child_actor_id = _canonical_user_operation_actor_id(child)
    parent_actor_id = _canonical_user_operation_actor_id(parent)
    if child_actor_id is None or parent_actor_id is None:
        return False
    plan_actor_key = str(plan.actor_key or "")
    if not plan_actor_key.startswith("user:"):
        return False
    raw_plan_actor_id = plan_actor_key.removeprefix("user:")
    if not raw_plan_actor_id or not raw_plan_actor_id.isascii() or not raw_plan_actor_id.isdigit():
        return False
    plan_actor_id = int(raw_plan_actor_id)
    if plan_actor_id <= 0 or raw_plan_actor_id != str(plan_actor_id):
        return False
    if plan.actor_user_id is not None:
        try:
            if int(plan.actor_user_id) != plan_actor_id:
                return False
        except (TypeError, ValueError):
            return False

    snapshot = dict(child.parent_snapshot or {})
    recovery_marker = str(snapshot.get("cross_actor_recovery") or "")
    cleanup_retry = bool(
        str(parent.retry_mode or "") == "cleanup_only"
        and str(plan.retry_mode or "") == "cleanup_only"
        and str(snapshot.get("retry_mode") or "") == "cleanup_only"
    )
    owner_created = str(child.actor_key) == plan_actor_key
    if owner_created:
        if child_actor_id != plan_actor_id:
            return False
        if child.actor_user_id is None and not cleanup_retry:
            return False
        if not recovery_marker:
            return True
        return bool(
            cleanup_retry
            and recovery_marker == "migration_cleanup_takeover"
            and _operation_is_authorized_cleanup_takeover(parent, plan)
        )
    return bool(
        cleanup_retry
        and recovery_marker == "migration_cleanup_takeover"
        and child_actor_id != plan_actor_id
    )


def _normal_retry_child_available(db: Session, previous: StorageOperation) -> bool:
    if int(previous.retry_depth or 0) >= MAX_RETRY_DEPTH:
        return False
    child_count = int(
        db.query(func.count(StorageOperation.id))
        .filter(StorageOperation.parent_operation_id == str(previous.id))
        .scalar()
        or 0
    )
    return child_count < MAX_RETRIES_PER_PARENT


def _exact_orphan_migration_child(
    db: Session,
    *,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
    lock: bool = False,
) -> StorageOperation | None:
    query = db.query(StorageOperation).filter(
        StorageOperation.operation_type == MIGRATION_OPERATION_TYPE,
        StorageOperation.domain_ref == _operation_domain_ref(str(plan.id)),
        StorageOperation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
    )
    if lock:
        query = query.with_for_update()
    rows = query.order_by(StorageOperation.created_at.asc(), StorageOperation.id.asc()).limit(3).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    child = rows[0]
    try:
        expected_scope = canonical_operation_scope(previous.scope)
    except StorageOperationContractError as exc:
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None) from exc
    if not (
        _operation_was_never_claimed(db, child, plan)
        and previous.status in TERMINAL_OPERATION_STATUSES
        and bool(previous.retry_allowed)
        and str(child.parent_operation_id or "") == str(previous.id)
        and _operation_matches_migration_lineage(child, plan, expected_scope=expected_scope)
        and _lineage_snapshot_matches(child, previous, plan)
        and int(child.retry_depth or 0) == int(previous.retry_depth or 0) + 1
        and int(child.retry_depth or 0) <= MAX_RETRY_DEPTH
        and _exact_orphan_creator_evidence(child, previous, plan)
        and bool(child.cancel_allowed)
    ):
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    return child


def _current_exact_migration_retry_child(
    db: Session,
    *,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
) -> StorageOperation | None:
    current_id = str(plan.current_operation_id or "")
    if not current_id or current_id == str(previous.id):
        return None
    child = db.get(StorageOperation, current_id)
    if child is None:
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    try:
        expected_scope = canonical_operation_scope(previous.scope)
    except StorageOperationContractError as exc:
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None) from exc
    if not (
        str(child.parent_operation_id or "") == str(previous.id)
        and _operation_matches_migration_lineage(child, plan, expected_scope=expected_scope)
        and _lineage_snapshot_matches(child, previous, plan)
        and int(child.retry_depth or 0) == int(previous.retry_depth or 0) + 1
        and int(child.retry_depth or 0) <= MAX_RETRY_DEPTH
    ):
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    return child


def _existing_migration_retry_child(
    db: Session,
    *,
    actor: User,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
    idempotency_key: str,
) -> StorageOperation | None:
    _actor_kind, actor_key, _actor_user_id, _system_owner = actor_identity(actor)
    child = (
        db.query(StorageOperation)
        .filter(
            StorageOperation.actor_key == str(actor_key),
            StorageOperation.operation_type == MIGRATION_OPERATION_TYPE,
            StorageOperation.idempotency_key == str(idempotency_key or "").strip().lower(),
        )
        .one_or_none()
    )
    if child is None:
        return None
    if str(plan.current_operation_id or "") != str(child.id):
        return None
    snapshot = dict(child.parent_snapshot or {})
    retry_depth_matches = False
    try:
        retry_depth_matches = int(child.retry_depth or 0) == int(previous.retry_depth or 0) + 1
    except (TypeError, ValueError):
        pass
    if not (
        str(child.parent_operation_id or "") == str(previous.id)
        and str(child.domain_ref or "") == _operation_domain_ref(str(plan.id))
        and str(child.request_fingerprint) == request_fingerprint(_operation_identity(plan))
        and str(snapshot.get("operation_id") or "") == str(previous.id)
        and str(snapshot.get("actor_key") or "") == str(previous.actor_key)
        and str(snapshot.get("original_actor_key") or "") == str(plan.actor_key)
        and str(snapshot.get("domain_ref") or "") == _operation_domain_ref(str(plan.id))
        and str(snapshot.get("retry_mode") or "") == str(previous.retry_mode or "")
        and retry_depth_matches
        and (
            str(previous.actor_key) == str(actor_key)
            or str(snapshot.get("cross_actor_recovery") or "") == "migration_cleanup_takeover"
        )
    ):
        raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
    return child


def retry_migration_operation(
    db: Session,
    *,
    actor: User,
    operation_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    previous = db.get(StorageOperation, str(operation_id))
    if previous is None or previous.operation_type != MIGRATION_OPERATION_TYPE:
        raise ArchiveMigrationBlocked("migration_operation_not_found", retry_mode=None)
    if not previous.domain_ref or not previous.domain_ref.startswith("migration-plan:"):
        raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
    plan = db.get(ArchiveMigrationPlan, previous.domain_ref.split(":", 1)[1])
    if plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    _require_plan_access(actor, plan)
    if not _actor_matches_plan(actor, plan):
        raise ArchiveMigrationBlocked("migration_plan_actor_mismatch", retry_mode=None)
    if not _permission_conjunction(actor):
        raise ArchiveMigrationBlocked("migration_permission_required", retry_mode=None)
    existing_child = _existing_migration_retry_child(
        db,
        actor=actor,
        plan=plan,
        previous=previous,
        idempotency_key=idempotency_key,
    )
    if existing_child is not None:
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": _public_operation_for_actor(
                db,
                actor=actor,
                plan=plan,
                operation=existing_child,
            ),
            "replayed": True,
        }
    current_child = _current_exact_migration_retry_child(db, plan=plan, previous=previous)
    if current_child is not None:
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": _public_operation_for_actor(
                db,
                actor=actor,
                plan=plan,
                operation=current_child,
            ),
            "replayed": True,
        }
    if _same_operation_continuation_replay(
        actor=actor,
        plan=plan,
        operation=previous,
        idempotency_key=idempotency_key,
    ):
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": _public_operation_for_actor(
                db,
                actor=actor,
                plan=plan,
                operation=previous,
            ),
            "replayed": True,
        }
    decision = _migration_recovery_decision(
        db,
        actor=actor,
        plan=plan,
        previous=previous,
        intent="owner_retry",
    )
    if not decision.allowed:
        raise ArchiveMigrationBlocked(decision.reason_code or "migration_retry_not_allowed", retry_mode=None)
    if decision.action == "adopt_child":
        child = _adopt_exact_orphan_migration_child(
            db,
            plan=plan,
            previous=previous,
            expected_child_id=str(decision.existing_child_id or ""),
            repair_actor=actor,
            repair_intent="owner_retry",
        )
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": _public_operation_for_actor(db, actor=actor, plan=plan, operation=child),
            "replayed": True,
        }
    if decision.action == "continue_same_operation":
        return _continue_same_cleanup_operation(
            db,
            actor=actor,
            plan=plan,
            previous=previous,
            idempotency_key=idempotency_key,
            intent="owner_retry",
        )
    if decision.action != "create_child":
        raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
    requested_retry_mode = str(previous.retry_mode or plan.retry_mode or "refresh")
    cross_actor_recovery = None
    if requested_retry_mode == "cleanup_only":
        lineage = _validated_cleanup_retry_lineage(db, plan=plan, previous=previous)
        if lineage is None:
            raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
        if str(previous.actor_key) != str(plan.actor_key):
            if not _cleanup_lineage_has_typed_recovery(lineage, plan=plan):
                raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
            cross_actor_recovery = _cleanup_recovery_authorization(
                previous=previous,
                plan=plan,
                allow_original_actor_return=True,
            )
    elif str(previous.actor_key) != str(plan.actor_key):
        raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
    if requested_retry_mode != "cleanup_only":
        retryable_phases = ("failed", "blocked")
        rows = (
            db.query(ArchiveMigrationItem)
            .filter(ArchiveMigrationItem.plan_id == plan.id, ArchiveMigrationItem.phase.in_(retryable_phases))
            .order_by(ArchiveMigrationItem.item_index.asc())
            .limit(PLAN_BATCH_SIZE)
            .all()
        )
        while rows:
            for item in rows:
                item.phase = "planned"
                item.last_reason_code = None
                item.result_code = None
                item.retry_mode = None
                item.completed_at = None
                db.add(item)
            db.commit()
            rows = (
                db.query(ArchiveMigrationItem)
                .filter(ArchiveMigrationItem.plan_id == plan.id, ArchiveMigrationItem.phase.in_(retryable_phases))
                .order_by(ArchiveMigrationItem.item_index.asc())
                .limit(PLAN_BATCH_SIZE)
                .all()
            )
    claimed = claim_operation_with_conflicts(
        db,
        operation_type=MIGRATION_OPERATION_TYPE,
        scope={"root_ids": [str(plan.source_root_id), str(plan.target_root_id)]},
        request_identity=_operation_identity(plan),
        actor=actor,
        idempotency_key=str(idempotency_key),
        parent_operation_id=str(previous.id),
        cross_actor_recovery=cross_actor_recovery,
        initial_progress=_operation_progress(plan, phase="queued"),
        start_immediately=False,
        cancel_allowed=True,
        domain_ref=_operation_domain_ref(str(plan.id)),
    )
    plan.current_operation_id = str(claimed["operation"]["operation_id"])
    plan.status = "queued"
    plan.phase = "queued"
    plan.cancel_requested = False
    plan.finished_at = None
    plan.reason_code = None
    plan.retry_mode = requested_retry_mode
    plan.next_action = None
    if requested_retry_mode == "cleanup_only":
        child = db.get(StorageOperation, str(claimed["operation"]["operation_id"]))
        if child is None:
            raise ArchiveMigrationBlocked("migration_operation_identity_invalid", retry_mode=None)
        _audit(
            db,
            actor=actor,
            event_type="archive_migration.cleanup_retry_queued",
            plan=plan,
            operation=child,
            severity="warning",
        )
    db.add(plan)
    db.commit()
    return {"plan": public_migration_plan(plan), "operation": claimed["operation"]}


def _cleanup_takeover_work_exists(db: Session, plan: ArchiveMigrationPlan) -> bool:
    return bool(
        db.query(ArchiveMigrationItem.id)
        .filter(
            ArchiveMigrationItem.plan_id == plan.id,
            or_(
                ArchiveMigrationItem.cleanup_pending.is_(True),
                ArchiveMigrationItem.phase.in_(tuple(CLEANUP_RECOVERY_ITEM_PHASES)),
            ),
        )
        .limit(1)
        .first()
    )


def _validated_cleanup_retry_lineage(
    db: Session,
    *,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
) -> list[StorageOperation] | None:
    if (
        str(plan.current_operation_id or "") != str(previous.id)
        or not _cleanup_takeover_work_exists(db, plan)
    ):
        return None
    try:
        return _validated_cleanup_lineage(
            db,
            plan=plan,
            current_operation=previous,
            require_current_terminal=True,
        )
    except StorageOperationLeaseLost:
        return None


def _cleanup_lineage_has_typed_recovery(
    lineage: list[StorageOperation],
    *,
    plan: ArchiveMigrationPlan,
) -> bool:
    for operation in lineage:
        snapshot = dict(operation.parent_snapshot or {})
        if (
            operation.parent_operation_id
            and str(snapshot.get("cross_actor_recovery") or "") == "migration_cleanup_takeover"
        ):
            return True
        continuation = _cleanup_continuation_snapshot(operation, plan)
        if continuation is not None and (
            str(continuation.get("actor_key") or "") != str(plan.actor_key)
        ):
            return True
        if (
            snapshot.get("cleanup_recovery_established") is True
            and str(snapshot.get("cleanup_recovery_original_actor_key") or "") == str(plan.actor_key)
        ):
            return True
    return False


def _cleanup_recovery_authorization(
    *,
    previous: StorageOperation,
    plan: ArchiveMigrationPlan,
    allow_original_actor_return: bool = False,
) -> CrossActorRecoveryAuthorization:
    return CrossActorRecoveryAuthorization(
        operation_type=MIGRATION_OPERATION_TYPE,
        parent_operation_id=str(previous.id),
        parent_actor_key=str(previous.actor_key),
        original_actor_key=str(
            dict(previous.parent_snapshot or {}).get("original_actor_key")
            or plan.actor_key
        ),
        parent_request_fingerprint=str(previous.request_fingerprint),
        domain_ref=str(previous.domain_ref),
        retry_mode="cleanup_only",
        allow_original_actor_return=bool(allow_original_actor_return),
    )


def _migration_recovery_decision(
    db: Session,
    *,
    actor: User,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
    intent: str,
) -> MigrationRecoveryDecision:
    if intent not in {"owner_retry", "cleanup_takeover"}:
        return MigrationRecoveryDecision(False, None, "migration_retry_not_allowed")
    if (
        not bool(previous.retry_allowed)
        or str(plan.current_operation_id or "") != str(previous.id)
        or previous.status not in TERMINAL_OPERATION_STATUSES
    ):
        return MigrationRecoveryDecision(False, None, "migration_retry_not_allowed")
    raw_continuation = dict(previous.parent_snapshot or {}).get("cleanup_continuation")
    if isinstance(raw_continuation, dict) and _cleanup_continuation_snapshot(previous, plan) is None:
        return MigrationRecoveryDecision(False, None, "migration_authority_contract_invalid")
    if intent == "owner_retry":
        if not _actor_matches_plan(actor, plan) or not _permission_conjunction(actor):
            return MigrationRecoveryDecision(False, None, "migration_retry_not_allowed")
    elif _actor_matches_plan(actor, plan) or not _is_recovery_administrator(actor):
        return MigrationRecoveryDecision(False, None, "migration_cleanup_takeover_not_allowed")

    retry_mode = str(previous.retry_mode or plan.retry_mode or "")
    lineage: list[StorageOperation] | None = None
    if retry_mode == "cleanup_only":
        lineage = _validated_cleanup_retry_lineage(db, plan=plan, previous=previous)
        if lineage is None:
            return MigrationRecoveryDecision(False, None, "migration_retry_not_allowed")
    elif intent != "owner_retry" or str(previous.actor_key) != str(plan.actor_key):
        return MigrationRecoveryDecision(False, None, "migration_retry_not_allowed")

    try:
        orphan = _exact_orphan_migration_child(db, plan=plan, previous=previous)
    except ArchiveMigrationBlocked as exc:
        return MigrationRecoveryDecision(False, None, str(exc.reason_code))

    if retry_mode == "cleanup_only":
        typed_recovery = bool(lineage and _cleanup_lineage_has_typed_recovery(lineage, plan=plan))
        if orphan is not None and str(orphan.actor_key) != str(plan.actor_key):
            typed_recovery = bool(
                str(dict(orphan.parent_snapshot or {}).get("cross_actor_recovery") or "")
                == "migration_cleanup_takeover"
            )
        if intent == "owner_retry" and not (
            str(previous.actor_key) == str(plan.actor_key) or typed_recovery
        ):
            return MigrationRecoveryDecision(False, None, "migration_retry_not_allowed")
        if intent == "cleanup_takeover" and not (
            typed_recovery or _original_plan_actor_unavailable(db, plan)
        ):
            return MigrationRecoveryDecision(False, None, "migration_cleanup_takeover_not_allowed")

    if orphan is not None:
        return MigrationRecoveryDecision(True, "adopt_child", existing_child_id=str(orphan.id))

    if _normal_retry_child_available(db, previous):
        return MigrationRecoveryDecision(True, "create_child")
    if retry_mode == "cleanup_only":
        return MigrationRecoveryDecision(True, "continue_same_operation")
    return MigrationRecoveryDecision(False, None, "migration_retry_capacity_exhausted")


def _owner_retry_allowed(
    db: Session,
    *,
    actor: User,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
) -> bool:
    return _migration_recovery_decision(
        db,
        actor=actor,
        plan=plan,
        previous=previous,
        intent="owner_retry",
    ).allowed


def _original_plan_actor_unavailable(db: Session, plan: ArchiveMigrationPlan) -> bool:
    original = db.get(User, plan.actor_user_id) if plan.actor_user_id else None
    return not _permission_conjunction(original)


def _cleanup_takeover_allowed(
    db: Session,
    *,
    actor: User,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
) -> bool:
    return _migration_recovery_decision(
        db,
        actor=actor,
        plan=plan,
        previous=previous,
        intent="cleanup_takeover",
    ).allowed


def _cleanup_continuation_idempotency_fingerprint(idempotency_key: str) -> str:
    normalized = str(idempotency_key or "").strip().lower()
    if not normalized:
        raise ArchiveMigrationBlocked("migration_idempotency_key_required", retry_mode=None)
    return request_fingerprint({"cleanup_continuation_idempotency_key": normalized})


def _same_operation_continuation_replay(
    *,
    actor: User,
    plan: ArchiveMigrationPlan,
    operation: StorageOperation,
    idempotency_key: str,
) -> bool:
    continuation = _cleanup_continuation_snapshot(operation, plan)
    if continuation is None:
        return False
    _actor_kind, actor_key, actor_user_id, _system_owner = actor_identity(actor)
    return bool(
        actor_user_id is not None
        and int(continuation["actor_user_id"]) == int(actor_user_id)
        and str(continuation["actor_key"]) == str(actor_key)
        and str(continuation["idempotency_fingerprint"])
        == _cleanup_continuation_idempotency_fingerprint(idempotency_key)
    )


def _continue_same_cleanup_operation(
    db: Session,
    *,
    actor: User,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
    idempotency_key: str,
    intent: str,
) -> dict[str, Any]:
    requested_fingerprint = _cleanup_continuation_idempotency_fingerprint(idempotency_key)

    def transition(normalized_scope: dict[str, Any]) -> dict[str, Any]:
        locked_operation = (
            db.query(StorageOperation)
            .filter(StorageOperation.id == str(previous.id))
            .with_for_update()
            .one_or_none()
        )
        locked_plan = (
            db.query(ArchiveMigrationPlan)
            .filter(ArchiveMigrationPlan.id == str(plan.id))
            .with_for_update()
            .one_or_none()
        )
        if locked_operation is None or locked_plan is None:
            raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
        if canonical_operation_scope(locked_operation.scope) != normalized_scope:
            raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
        if _same_operation_continuation_replay(
            actor=actor,
            plan=locked_plan,
            operation=locked_operation,
            idempotency_key=idempotency_key,
        ):
            return {
                "plan": public_migration_plan(locked_plan, replayed=True),
                "operation": _public_operation_for_actor(
                    db,
                    actor=actor,
                    plan=locked_plan,
                    operation=locked_operation,
                ),
                "replayed": True,
            }
        decision = _migration_recovery_decision(
            db,
            actor=actor,
            plan=locked_plan,
            previous=locked_operation,
            intent=intent,
        )
        if not decision.allowed or decision.action != "continue_same_operation":
            raise ArchiveMigrationBlocked(decision.reason_code or "migration_retry_not_allowed", retry_mode=None)

        snapshot = dict(locked_operation.parent_snapshot or {})
        existing_continuation = snapshot.get("cleanup_continuation")
        if existing_continuation is not None and not isinstance(existing_continuation, dict):
            raise ArchiveMigrationBlocked("migration_authority_contract_invalid", retry_mode=None)
        previous_attempt = int(dict(existing_continuation or {}).get("attempt") or 0)
        attempt = previous_attempt + 1
        _actor_kind, actor_key, actor_user_id, _system_owner = actor_identity(actor)
        if actor_user_id is None:
            raise ArchiveMigrationBlocked("migration_retry_not_allowed", retry_mode=None)
        queued_fencing_token = int(locked_operation.fencing_token or 0) + 1
        attempt_id = hashlib.sha256(
            f"{locked_operation.id}:{attempt}:{actor_key}:{requested_fingerprint}".encode("utf-8")
        ).hexdigest()[:32]
        snapshot["cleanup_continuation"] = {
            "mode": MIGRATION_CLEANUP_CONTINUATION_MODE,
            "attempt": attempt,
            "attempt_id": attempt_id,
            "actor_user_id": int(actor_user_id),
            "idempotency_fingerprint": requested_fingerprint,
            "queued_fencing_token": queued_fencing_token,
        }
        if str(actor_key) != str(locked_plan.actor_key):
            snapshot["cleanup_recovery_established"] = True
            snapshot["cleanup_recovery_original_actor_key"] = str(locked_plan.actor_key)
        encoded_snapshot = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded_snapshot) > MIGRATION_PARENT_SNAPSHOT_MAX_BYTES:
            raise ArchiveMigrationBlocked("migration_authority_contract_invalid", retry_mode=None)

        now = database_now(db)
        locked_operation.parent_snapshot = snapshot
        locked_operation.status = "queued"
        locked_operation.progress = _operation_progress(locked_plan, phase="queued")
        locked_operation.result = None
        locked_operation.reason_code = None
        locked_operation.next_action = None
        locked_operation.retry_mode = "cleanup_only"
        locked_operation.retry_allowed = False
        locked_operation.cancel_allowed = True
        locked_operation.owner_token_hash = None
        locked_operation.owner_instance_id = None
        locked_operation.fencing_token = queued_fencing_token
        locked_operation.revision = int(locked_operation.revision or 0) + 1
        locked_operation.lease_expires_at = now + timedelta(seconds=WORKER_LEASE_SECONDS)
        locked_operation.queued_at = now
        locked_operation.started_at = None
        locked_operation.heartbeat_at = None
        locked_operation.finished_at = None
        locked_operation.updated_at = now

        locked_plan.current_operation_id = str(locked_operation.id)
        locked_plan.status = "queued"
        locked_plan.phase = "queued"
        locked_plan.cancel_requested = False
        locked_plan.finished_at = None
        locked_plan.reason_code = None
        locked_plan.retry_mode = "cleanup_only"
        locked_plan.next_action = None
        db.add_all((locked_operation, locked_plan))
        _ensure_cleanup_attempt_audit(
            db,
            actor=actor,
            operation=locked_operation,
            plan=locked_plan,
            event_type="archive_migration.cleanup_continuation_queued",
            status="queued",
        )
        db.commit()
        return {
            "plan": public_migration_plan(locked_plan),
            "operation": _public_operation_for_actor(
                db,
                actor=actor,
                plan=locked_plan,
                operation=locked_operation,
            ),
            "replayed": False,
        }

    return run_coordinated_operation_transition(
        db,
        operation_type=MIGRATION_OPERATION_TYPE,
        scope={"root_ids": [str(plan.source_root_id), str(plan.target_root_id)]},
        domain_ref=_operation_domain_ref(str(plan.id)),
        current_operation_id=str(previous.id),
        transition=transition,
        actor=actor,
    )


def _repair_actor_can_replay_bound_child(
    db: Session,
    *,
    repair_actor: User | None,
    repair_intent: str,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
    child: StorageOperation,
) -> bool:
    if repair_intent == "system_worker":
        return repair_actor is None
    if repair_actor is None:
        return False
    if repair_intent == "owner_retry":
        return bool(_actor_matches_plan(repair_actor, plan) and _permission_conjunction(repair_actor))
    if repair_intent != "cleanup_takeover" or not _is_recovery_administrator(repair_actor):
        return False
    if _actor_matches_plan(repair_actor, plan):
        return False
    typed_recovery = bool(
        str(child.actor_key) != str(plan.actor_key)
        and str(dict(child.parent_snapshot or {}).get("cross_actor_recovery") or "")
        == "migration_cleanup_takeover"
    )
    try:
        lineage = _validated_cleanup_retry_lineage(db, plan=plan, previous=previous)
    except (ArchiveMigrationBlocked, StorageOperationLeaseLost):
        lineage = None
    typed_recovery = bool(
        typed_recovery
        or (lineage and _cleanup_lineage_has_typed_recovery(lineage, plan=plan))
    )
    return bool(typed_recovery or _original_plan_actor_unavailable(db, plan))


def _adopt_exact_orphan_migration_child(
    db: Session,
    *,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
    expected_child_id: str,
    repair_actor: User | None,
    repair_intent: str,
) -> StorageOperation:
    if repair_intent not in {"owner_retry", "cleanup_takeover", "system_worker"}:
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    candidate = (
        db.query(StorageOperation)
        .filter(StorageOperation.id == str(expected_child_id))
        .with_for_update()
        .one_or_none()
    )
    if candidate is None:
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    locked_parent = (
        db.query(StorageOperation)
        .filter(StorageOperation.id == str(previous.id))
        .with_for_update()
        .one_or_none()
    )
    locked_plan = (
        db.query(ArchiveMigrationPlan)
        .filter(ArchiveMigrationPlan.id == str(plan.id))
        .with_for_update()
        .one_or_none()
    )
    if locked_parent is None or locked_plan is None:
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    if repair_intent != "system_worker":
        if repair_actor is None:
            raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
        repair_actor = (
            db.query(User)
            .filter(User.id == int(repair_actor.id))
            .populate_existing()
            .one_or_none()
        )
        if repair_actor is None:
            raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)

    if str(locked_plan.current_operation_id or "") == str(candidate.id):
        current_child = _current_exact_migration_retry_child(
            db,
            plan=locked_plan,
            previous=locked_parent,
        )
        if not (
            current_child is not None
            and str(current_child.id) == str(candidate.id)
            and _exact_orphan_creator_evidence(current_child, locked_parent, locked_plan)
            and _repair_actor_can_replay_bound_child(
                db,
                repair_actor=repair_actor,
                repair_intent=repair_intent,
                plan=locked_plan,
                previous=locked_parent,
                child=current_child,
            )
        ):
            raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
        db.commit()
        return current_child

    if str(locked_plan.current_operation_id or "") != str(locked_parent.id):
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    child = _exact_orphan_migration_child(db, plan=locked_plan, previous=locked_parent, lock=True)
    if child is None or str(child.id) != str(candidate.id):
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    if repair_intent != "system_worker":
        if repair_actor is None:
            raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
        locked_decision = _migration_recovery_decision(
            db,
            actor=repair_actor,
            plan=locked_plan,
            previous=locked_parent,
            intent=repair_intent,
        )
        if not (
            locked_decision.allowed
            and locked_decision.action == "adopt_child"
            and str(locked_decision.existing_child_id or "") == str(child.id)
        ):
            raise ArchiveMigrationBlocked(
                locked_decision.reason_code or "migration_retry_child_ambiguous",
                retry_mode=None,
            )

    retry_mode = str(locked_parent.retry_mode or locked_plan.retry_mode or "refresh")
    locked_plan.current_operation_id = str(child.id)
    locked_plan.status = "queued"
    locked_plan.phase = "queued"
    locked_plan.cancel_requested = False
    locked_plan.finished_at = None
    locked_plan.reason_code = None
    locked_plan.retry_mode = retry_mode
    locked_plan.next_action = None
    db.add(locked_plan)
    queued_actor_id = _canonical_user_operation_actor_id(child)
    if queued_actor_id is None:
        raise ArchiveMigrationBlocked("migration_retry_child_ambiguous", retry_mode=None)
    _ensure_operation_queue_audit(db, child, queued_actor_id=queued_actor_id)
    parent_snapshot = dict(child.parent_snapshot or {})
    child_actor = db.get(User, child.actor_user_id) if child.actor_user_id else None
    if str(parent_snapshot.get("cross_actor_recovery") or "") == "migration_cleanup_takeover":
        _ensure_orphan_cleanup_takeover_queue_audit(
            db,
            actor=child_actor,
            operation=child,
            plan=locked_plan,
        )
    elif retry_mode == "cleanup_only":
        _ensure_orphan_cleanup_retry_queue_audit(
            db,
            actor=child_actor,
            operation=child,
            plan=locked_plan,
        )
    _ensure_orphan_adoption_audit(
        db,
        operation=child,
        plan=locked_plan,
        queued_actor_id=queued_actor_id,
        repair_actor=repair_actor,
        repair_origin="system_worker" if repair_intent == "system_worker" else "endpoint",
    )
    db.commit()
    return child


def _existing_cleanup_takeover_child(
    db: Session,
    *,
    actor: User,
    plan: ArchiveMigrationPlan,
    previous: StorageOperation,
    idempotency_key: str,
) -> StorageOperation | None:
    _actor_kind, actor_key, _actor_user_id, _system_owner = actor_identity(actor)
    child = (
        db.query(StorageOperation)
        .filter(
            StorageOperation.actor_key == str(actor_key),
            StorageOperation.operation_type == MIGRATION_OPERATION_TYPE,
            StorageOperation.idempotency_key == str(idempotency_key or "").strip().lower(),
        )
        .first()
    )
    if child is None:
        return None
    if str(plan.current_operation_id or "") != str(child.id):
        return None
    parent = dict(child.parent_snapshot or {})
    if not (
        _is_recovery_administrator(actor)
        and str(child.parent_operation_id or "") == str(previous.id)
        and str(child.domain_ref or "") == _operation_domain_ref(str(plan.id))
        and str(child.request_fingerprint) == request_fingerprint(_operation_identity(plan))
        and str(parent.get("cross_actor_recovery") or "") == "migration_cleanup_takeover"
        and str(parent.get("operation_id") or "") == str(previous.id)
        and str(parent.get("original_actor_key") or "") == str(plan.actor_key)
        and str(parent.get("domain_ref") or "") == _operation_domain_ref(str(plan.id))
        and str(parent.get("retry_mode") or "") == "cleanup_only"
    ):
        raise ArchiveMigrationBlocked("migration_cleanup_takeover_not_allowed", retry_mode=None)
    return child


def takeover_migration_cleanup(
    db: Session,
    *,
    actor: User,
    operation_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    previous = db.get(StorageOperation, str(operation_id))
    if previous is None or previous.operation_type != MIGRATION_OPERATION_TYPE:
        raise ArchiveMigrationBlocked("migration_operation_not_found", retry_mode=None)
    if not previous.domain_ref or not previous.domain_ref.startswith("migration-plan:"):
        raise ArchiveMigrationBlocked("migration_operation_identity_invalid", retry_mode=None)
    plan = db.get(ArchiveMigrationPlan, previous.domain_ref.split(":", 1)[1])
    if plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    _require_plan_access(actor, plan)
    existing_child = _existing_cleanup_takeover_child(
        db,
        actor=actor,
        plan=plan,
        previous=previous,
        idempotency_key=idempotency_key,
    )
    if existing_child is not None:
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": _public_operation_for_actor(
                db,
                actor=actor,
                plan=plan,
                operation=existing_child,
            ),
            "replayed": True,
        }
    current_child = _current_exact_migration_retry_child(db, plan=plan, previous=previous)
    if current_child is not None:
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": _public_operation_for_actor(
                db,
                actor=actor,
                plan=plan,
                operation=current_child,
            ),
            "replayed": True,
        }
    if _same_operation_continuation_replay(
        actor=actor,
        plan=plan,
        operation=previous,
        idempotency_key=idempotency_key,
    ):
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": _public_operation_for_actor(
                db,
                actor=actor,
                plan=plan,
                operation=previous,
            ),
            "replayed": True,
        }
    decision = _migration_recovery_decision(
        db,
        actor=actor,
        plan=plan,
        previous=previous,
        intent="cleanup_takeover",
    )
    if not decision.allowed:
        raise ArchiveMigrationBlocked(
            decision.reason_code or "migration_cleanup_takeover_not_allowed",
            retry_mode=None,
        )
    if decision.action == "adopt_child":
        child = _adopt_exact_orphan_migration_child(
            db,
            plan=plan,
            previous=previous,
            expected_child_id=str(decision.existing_child_id or ""),
            repair_actor=actor,
            repair_intent="cleanup_takeover",
        )
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": _public_operation_for_actor(db, actor=actor, plan=plan, operation=child),
            "replayed": True,
        }
    if decision.action == "continue_same_operation":
        return _continue_same_cleanup_operation(
            db,
            actor=actor,
            plan=plan,
            previous=previous,
            idempotency_key=idempotency_key,
            intent="cleanup_takeover",
        )
    if decision.action != "create_child":
        raise ArchiveMigrationBlocked("migration_cleanup_takeover_not_allowed", retry_mode=None)

    authorization = _cleanup_recovery_authorization(
        previous=previous,
        plan=plan,
    )
    claimed = claim_operation_with_conflicts(
        db,
        operation_type=MIGRATION_OPERATION_TYPE,
        scope={"root_ids": [str(plan.source_root_id), str(plan.target_root_id)]},
        request_identity=_operation_identity(plan),
        actor=actor,
        idempotency_key=str(idempotency_key),
        parent_operation_id=str(previous.id),
        cross_actor_recovery=authorization,
        initial_progress=_operation_progress(plan, phase="queued"),
        start_immediately=False,
        cancel_allowed=True,
        domain_ref=_operation_domain_ref(str(plan.id)),
    )
    operation = dict(claimed.get("operation") or {})
    if claimed.get("state") in {"terminal", "running", "interrupted"}:
        return {
            "plan": public_migration_plan(plan, replayed=True),
            "operation": operation,
            "replayed": True,
        }
    plan.current_operation_id = str(claimed["operation"]["operation_id"])
    plan.status = "queued"
    plan.phase = "queued"
    plan.cancel_requested = False
    plan.finished_at = None
    plan.reason_code = None
    plan.retry_mode = "cleanup_only"
    plan.next_action = None
    child = db.get(StorageOperation, str(claimed["operation"]["operation_id"]))
    if child is None:
        raise ArchiveMigrationBlocked("migration_operation_identity_invalid", retry_mode=None)
    _audit_cleanup_takeover(
        db,
        actor=actor,
        event_type="archive_migration.cleanup_takeover_queued",
        operation=child,
        plan=plan,
        severity="warning",
        metadata={"recovery_mode": "cleanup_only"},
    )
    db.add(plan)
    db.commit()
    return {"plan": public_migration_plan(plan), "operation": operation, "replayed": False}


def _public_operation_for_actor(
    db: Session,
    *,
    actor: User,
    plan: ArchiveMigrationPlan,
    operation: StorageOperation,
) -> dict[str, Any]:
    public = public_operation_summary(operation, now=database_now(db))
    public["capabilities"] = {
        "owner_retry_allowed": _owner_retry_allowed(
            db,
            actor=actor,
            plan=plan,
            previous=operation,
        ),
        "cleanup_takeover_allowed": _cleanup_takeover_allowed(
            db,
            actor=actor,
            plan=plan,
            previous=operation,
        )
    }
    return public


def active_migration_operation(db: Session, *, actor: User) -> dict[str, Any]:
    row = (
        db.query(StorageOperation)
        .filter(
            StorageOperation.operation_type == MIGRATION_OPERATION_TYPE,
            StorageOperation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
        )
        .order_by(StorageOperation.updated_at.desc(), StorageOperation.id.asc())
        .limit(1)
        .first()
    )
    if row is None:
        return {"active": False, "operation": None, "plan": None}
    plan = None
    if row.domain_ref and row.domain_ref.startswith("migration-plan:"):
        plan = db.get(ArchiveMigrationPlan, row.domain_ref.split(":", 1)[1])
    if plan is None or not _actor_can_read_plan(actor, plan):
        return {"active": False, "operation": None, "plan": None}
    return {
        "active": True,
        "operation": _public_operation_for_actor(db, actor=actor, plan=plan, operation=row),
        "plan": public_migration_plan(plan),
    }


def get_migration_operation(db: Session, *, actor: User, operation_id: str) -> dict[str, Any]:
    row = db.get(StorageOperation, str(operation_id))
    if row is None or row.operation_type != MIGRATION_OPERATION_TYPE:
        raise ArchiveMigrationBlocked("migration_operation_not_found", retry_mode=None)
    if not row.domain_ref or not row.domain_ref.startswith("migration-plan:"):
        raise ArchiveMigrationBlocked("migration_operation_identity_invalid", retry_mode=None)
    plan = db.get(ArchiveMigrationPlan, row.domain_ref.split(":", 1)[1])
    if plan is None:
        raise ArchiveMigrationBlocked("migration_plan_not_found", retry_mode=None)
    _require_plan_access(actor, plan)
    return {
        "operation": _public_operation_for_actor(db, actor=actor, plan=plan, operation=row),
        "plan": public_migration_plan(plan),
    }


def bounded_migration_summary(db: Session) -> dict[str, Any]:
    row = (
        db.query(ArchiveMigrationPlan)
        .order_by(ArchiveMigrationPlan.updated_at.desc(), ArchiveMigrationPlan.id.asc())
        .limit(1)
        .first()
    )
    if row is None:
        return {"available": True, "active": None, "recent": None}
    summary = {
        "plan_id": str(row.id),
        "status": str(row.status),
        "phase": str(row.phase),
        "completed_count": int(row.completed_count or 0),
        "item_count": int(row.item_count or 0),
        "completed_bytes": int(row.completed_bytes or 0),
        "total_bytes": int(row.total_bytes or 0),
        "cleanup_pending": bool(row.cleanup_pending),
        "operation_id": row.current_operation_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    return {
        "available": True,
        "active": summary if row.status in {"building", "queued", "running", "cancel_requested"} else None,
        "recent": summary if row.status in PLAN_TERMINAL_STATUSES else None,
    }


def _recover_terminal_audit_once() -> bool:
    with SessionLocal() as db:
        plans = (
            db.query(ArchiveMigrationPlan)
            .filter(
                ArchiveMigrationPlan.status.in_(tuple(PLAN_TERMINAL_STATUSES)),
                ArchiveMigrationPlan.current_operation_id.isnot(None),
            )
            .order_by(ArchiveMigrationPlan.finished_at.asc(), ArchiveMigrationPlan.id.asc())
            .limit(ITEM_PAGE_MAX)
            .all()
        )
        for plan in plans:
            operation = db.get(StorageOperation, str(plan.current_operation_id))
            if operation is None or str(operation.status) != str(plan.status):
                continue
            event_type = f"archive_migration.operation_{plan.status}"
            try:
                created = _audit(
                    db,
                    event_type=event_type,
                    plan=plan,
                    operation=operation,
                    severity="info" if plan.status == "completed" else "warning",
                    metadata={"recovered_from_terminal_truth": True},
                )
                if not created:
                    continue
                db.commit()
                return True
            except _MigrationAuditPersistenceFailed:
                db.rollback()
                return False
    return False


_worker_stop = threading.Event()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


class _WorkerLeaseHeartbeat:
    def __init__(self, handle):
        self.handle = handle
        self.stop_event = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="archive-migration-leader-heartbeat", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.wait(max(2.0, WORKER_LEASE_SECONDS / 3.0)):
            try:
                with SessionLocal() as db:
                    renew_worker_lease(db, self.handle, lease_seconds=WORKER_LEASE_SECONDS)
            except Exception:
                self.lost.set()
                return

    def assert_owned(self) -> None:
        if self.lost.is_set():
            raise StorageOperationLeaseLost("archive_migration_worker_lease_lost")

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


def _worker_loop() -> None:
    failures = 0
    delay = WORKER_POLL_SECONDS
    while not _worker_stop.wait(delay):
        handle = None
        leader = None
        try:
            with SessionLocal() as lease_db:
                handle = acquire_worker_lease(
                    lease_db,
                    worker_key="archive-migration-worker",
                    owner_instance_id=f"api:{os.getpid()}:{threading.get_ident()}",
                    lease_seconds=WORKER_LEASE_SECONDS,
                )
            if handle is None:
                failures = 0
                delay = WORKER_POLL_SECONDS
                continue
            leader = _WorkerLeaseHeartbeat(handle)
            leader.start()
            did_work = _recover_terminal_audit_once()
            leader.assert_owned()
            did_work = _expire_one_ready_plan() or did_work
            leader.assert_owned()
            did_work = _prepare_one_plan(leader) or did_work
            leader.assert_owned()
            did_work = _run_one_operation() or did_work
            failures = 0
            delay = WORKER_POLL_SECONDS
            if not did_work:
                _worker_stop.wait(0.1)
        except Exception as exc:
            # Sessions roll back on context exit. Durable operation leases fence
            # abandoned attempts; the next cycle reclaims only through that API.
            failures += 1
            delay = min(30.0, max(WORKER_POLL_SECONDS, 2.0) * (2 ** min(failures - 1, 4)))
            logger.warning("Archive migration cycle interrupted; retry in %.1fs; failure_type=%s",
                           delay, type(exc).__name__)
        finally:
            if leader is not None:
                leader.close()
            if handle is not None:
                try:
                    with SessionLocal() as lease_db:
                        release_worker_lease(lease_db, handle)
                except Exception as exc:
                    logger.warning("Archive migration lease release deferred; failure_type=%s", type(exc).__name__)


def start_archive_migration_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_stop.clear()
        _reset_worker_candidate_scan_state()
        _worker_thread = threading.Thread(target=_worker_loop, name="archive-migration-worker", daemon=True)
        _worker_thread.start()


def stop_archive_migration_worker() -> None:
    global _worker_thread
    with _worker_lock:
        _worker_stop.set()
        thread = _worker_thread
        _worker_thread = None
    if thread is not None:
        thread.join(timeout=5)
