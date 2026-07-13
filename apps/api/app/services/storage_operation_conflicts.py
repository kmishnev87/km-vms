from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.storage_operation import StorageOperation
from app.services.storage_operations_foundation import (
    ACTIVE_OPERATION_STATUSES,
    OperationHandle,
    OPERATION_MAX_LIST_ITEMS,
    StorageOperationContractError,
    TERMINAL_OPERATION_STATUSES,
    acquire_worker_lease,
    actor_identity,
    canonical_operation_scope,
    claim_operation,
    database_now,
    finish_operation,
    normalize_operation_scope,
    operation_effective_status,
    reclaim_operation,
    release_worker_lease,
    renew_worker_lease,
    request_fingerprint,
    safe_reason_code,
)


ACTIVE_JOB_STATES = frozenset({"starting", "recording", "stopping", "restarting"})
ACTIVE_SEGMENT_STATES = frozenset({"starting", "writing", "stopping", "restarting"})
COORDINATOR_WORKER_KEY = "storage-operation-claim-coordinator"
COORDINATOR_LEASE_SECONDS = 15
COORDINATOR_WAIT_SECONDS = 2.0
MAX_ACTIVE_OPERATION_SCAN = 256

OPERATION_TYPES = frozenset(
    {
        "manual_single_delete",
        "manual_bulk_delete",
        "manual_delete_by_camera",
        "manual_delete_all",
        "camera_delete_with_files",
        "retention_run",
        "retention_auto_run",
        "retention_auto_free_space",
        "integrity_metadata_repair",
        "orphan_file_cleanup",
        "archive_migration_apply",
        "archive_root_delete",
        "archive_root_activation",
    }
)
DELETION_EXECUTION_TYPES = frozenset(
    {
        "manual_single_delete",
        "manual_bulk_delete",
        "manual_delete_by_camera",
        "manual_delete_all",
        "camera_delete_with_files",
        "retention_run",
        "retention_auto_run",
        "retention_auto_free_space",
    }
)

ROOT_EXCLUSIVE_TYPES = frozenset(
    {
        "archive_root_delete",
        "archive_root_activation",
        "archive_migration_apply",
        "orphan_file_cleanup",
    }
)
EXACT_ITEM_TYPES = DELETION_EXECUTION_TYPES - {"manual_delete_by_camera", "manual_delete_all", "camera_delete_with_files"}
ACTIVE_WRITE_ROOT_BLOCKED_TYPES = frozenset(
    {
        "archive_root_delete",
        "archive_migration_apply",
        "integrity_metadata_repair",
        "orphan_file_cleanup",
    }
)
ACTIVE_WRITE_CAMERA_BLOCKED_TYPES = frozenset({"camera_delete_with_files"})
ACTIVE_WRITE_EXACT_TYPES = frozenset(
    {
        "manual_single_delete",
        "manual_bulk_delete",
        "manual_delete_by_camera",
        "manual_delete_all",
        "retention_run",
        "retention_auto_run",
        "retention_auto_free_space",
    }
)
ACTIVE_WRITE_GLOBAL_BLOCKED_TYPES = frozenset({"integrity_metadata_repair"})


class StorageOperationConflict(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail.get("reason_code") or "storage_operation_conflict"))


def _claim_operation_checked(db: Session, **kwargs) -> dict[str, Any]:
    try:
        return claim_operation(db, **kwargs)
    except StorageOperationContractError as exc:
        if str(exc) in {
            "operation_identity_mismatch",
            "operation_idempotency_identity_mismatch",
            "operation_retry_parent_invalid",
        }:
            raise StorageOperationConflict(
                {
                    "reason_code": "storage_operation_identity_mismatch",
                    "retryable": False,
                }
            ) from exc
        raise


@dataclass(frozen=True)
class ActiveRecorderWriteGuard:
    camera_ids: frozenset[int]
    root_ids: frozenset[str]
    segment_ids: frozenset[int]

    @property
    def active(self) -> bool:
        return bool(self.camera_ids or self.root_ids or self.segment_ids)


class CoordinatorLeaseSession:
    def __init__(self, bind: Any, handle, *, lease_seconds: int = COORDINATOR_LEASE_SECONDS):
        self.bind = bind
        self.handle = handle
        self.lease_seconds = max(5, int(lease_seconds))
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.assert_owned()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"storage-operation-coordinator-{self.handle.fencing_token}",
            daemon=True,
        )
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        interval = max(1.0, self.lease_seconds / 3.0)
        while not self._stop.wait(interval):
            try:
                with Session(bind=self.bind) as heartbeat_db:
                    renew_worker_lease(
                        heartbeat_db,
                        self.handle,
                        lease_seconds=self.lease_seconds,
                    )
            except BaseException as exc:
                self._error = exc
                self._lost.set()
                return

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise StorageOperationConflict(
                {"reason_code": "storage_operation_coordinator_lease_lost", "retryable": True}
            ) from self._error
        try:
            with Session(bind=self.bind) as heartbeat_db:
                renew_worker_lease(
                    heartbeat_db,
                    self.handle,
                    lease_seconds=self.lease_seconds,
                )
        except Exception as exc:
            self._error = exc
            self._lost.set()
            raise StorageOperationConflict(
                {"reason_code": "storage_operation_coordinator_lease_lost", "retryable": True}
            ) from exc

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.lease_seconds / 2.0))
        try:
            with Session(bind=self.bind) as release_db:
                release_worker_lease(release_db, self.handle)
        except Exception:
            pass


class StorageOperationLifecycle:
    """Guarantee one terminal outer state unless authority is durably handed to an async worker."""

    def __init__(self, db: Session, handle: OperationHandle, *, failure_reason: str):
        self.db = db
        self.handle = handle
        self.failure_reason = safe_reason_code(failure_reason, fallback="storage_operation_internal_failure")
        self.terminalized = False
        self.deferred = False
        self.inner_persisted = False
        self.inner_result: dict[str, Any] = {}

    def __enter__(self) -> "StorageOperationLifecycle":
        return self

    def finish(
        self,
        *,
        status: str,
        result: dict | None = None,
        progress: dict | None = None,
        reason_code: str | None = None,
        next_action: str | None = None,
        retry_mode: str | None = None,
        retry_allowed: bool = False,
    ) -> dict:
        if self.terminalized or self.deferred:
            raise StorageOperationContractError("storage_operation_lifecycle_already_closed")
        terminal = finish_operation(
            self.db,
            self.handle,
            status=status,
            result=result,
            progress=progress,
            reason_code=safe_reason_code(reason_code),
            next_action=next_action,
            retry_mode=retry_mode,
            retry_allowed=retry_allowed,
        )
        self.terminalized = True
        return terminal

    def finish_result(
        self,
        result: dict,
        *,
        progress: dict | None = None,
        reason_code: str | None = None,
        retry_mode: str | None = None,
        retry_allowed: bool | None = None,
    ) -> dict:
        self.mark_inner_persisted(result)
        return self.finish(
            status=terminal_status_for_result(result),
            result=terminal_result_summary(result),
            progress=progress,
            reason_code=reason_code,
            retry_mode=retry_mode,
            retry_allowed=bool(result.get("retryable")) if retry_allowed is None else retry_allowed,
        )

    def mark_inner_persisted(self, result: dict | None = None) -> None:
        self.inner_persisted = True
        self.inner_result = terminal_result_summary(result)

    def block(self, reason_code: str, *, retry_allowed: bool = True, retry_mode: str = "immediate") -> dict:
        return self.finish(
            status="blocked",
            result={"status": "blocked"},
            reason_code=reason_code,
            retry_allowed=retry_allowed,
            retry_mode=retry_mode if retry_allowed else None,
        )

    def defer_to_async_worker(self) -> None:
        if self.terminalized:
            raise StorageOperationContractError("storage_operation_lifecycle_already_closed")
        self.deferred = True

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if self.terminalized or self.deferred:
            return False
        self.db.rollback()
        reason = self.failure_reason
        if isinstance(exc, StorageOperationConflict):
            reason = safe_reason_code(exc.detail, fallback=reason)
        try:
            self.finish(
                status="partial" if self.inner_persisted else "failed",
                result=self.inner_result or {"status": "failed"},
                reason_code=reason,
                retry_allowed=True,
                retry_mode="immediate",
            )
        except Exception as terminal_exc:
            self.db.rollback()
            raise StorageOperationContractError("operation_terminal_persistence_failed") from terminal_exc
        if exc is None:
            raise StorageOperationContractError("storage_operation_exit_without_terminal")
        return False


def claim_state_detail(claimed: dict[str, Any]) -> dict[str, Any]:
    state = str(claimed.get("state") or "unknown")
    if state == "interrupted":
        return {
            "reason_code": "storage_operation_interrupted",
            "retryable": True,
            "retry_mode": "refresh",
        }
    if state == "queued":
        return {"reason_code": "storage_operation_queued", "retryable": True}
    return {"reason_code": "storage_operation_already_running", "retryable": True}


def terminal_replay_result(claimed: dict[str, Any]) -> dict[str, Any]:
    if claimed.get("state") != "terminal":
        raise StorageOperationContractError("storage_operation_terminal_replay_required")
    operation = dict(claimed.get("operation") or {})
    result = terminal_result_summary(claimed.get("terminal_result"))
    result.setdefault("status", operation.get("status") or "failed")
    for field in ("reason_code", "next_action", "retry_mode", "retry_allowed", "cancel_allowed"):
        result.setdefault(field, operation.get(field))
    result["operation_id"] = operation.get("operation_id")
    result["replayed"] = True
    return result


def operation_instance_id(prefix: str = "api") -> str:
    return f"{prefix}:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex[:12]}"


def scope_with_physical_volumes(db: Session, scope: dict) -> dict:
    raw = dict(scope or {})
    root_ids = sorted({str(value) for value in raw.get("root_ids") or [] if str(value)})
    if len(root_ids) > OPERATION_MAX_LIST_ITEMS:
        return normalize_operation_scope({**raw, "global": True, "root_ids": []})
    volume_ids = {str(value) for value in raw.get("physical_volume_ids") or [] if str(value)}
    if root_ids:
        rows = (
            db.query(ArchiveRoot.id, ArchiveRoot.physical_identity)
            .filter(ArchiveRoot.id.in_(root_ids))
            .order_by(ArchiveRoot.id.asc())
            .limit(OPERATION_MAX_LIST_ITEMS)
            .all()
        )
        resolved_root_ids = {str(row.id) for row in rows}
        if resolved_root_ids != set(root_ids) or any(not row.physical_identity for row in rows):
            return normalize_operation_scope(
                {
                    **raw,
                    "global": True,
                    "root_ids": [],
                    "physical_volume_ids": [],
                }
            )
        volume_ids.update(str(row.physical_identity) for row in rows if row.physical_identity)
    return normalize_operation_scope(
        {
            **raw,
            "root_ids": root_ids,
            "physical_volume_ids": sorted(volume_ids),
        }
    )


def active_recorder_write_guard(db: Session) -> ActiveRecorderWriteGuard:
    camera_ids = {
        int(value)
        for (value,) in db.query(RecordingJob.camera_id)
        .filter(RecordingJob.state.in_(tuple(ACTIVE_JOB_STATES)))
        .distinct()
        .all()
        if value is not None
    }
    segment_rows = (
        db.query(RecordingSegment.id, RecordingSegment.camera_id, RecordingSegment.archive_root_id)
        .filter(
            RecordingSegment.deleted_at.is_(None),
            RecordingSegment.status.in_(tuple(ACTIVE_SEGMENT_STATES)),
        )
        .all()
    )
    return ActiveRecorderWriteGuard(
        camera_ids=frozenset(camera_ids | {int(row.camera_id) for row in segment_rows if row.camera_id is not None}),
        root_ids=frozenset(str(row.archive_root_id) for row in segment_rows if row.archive_root_id),
        segment_ids=frozenset(int(row.id) for row in segment_rows if row.id is not None),
    )


def _set(scope: dict, field: str) -> set:
    return set(scope.get(field) or [])


def _conflict_active_filter(now):
    pristine_queue = and_(
        StorageOperation.status == "queued",
        StorageOperation.fencing_token == 0,
        StorageOperation.started_at.is_(None),
        StorageOperation.heartbeat_at.is_(None),
        StorageOperation.owner_token_hash.is_(None),
        StorageOperation.owner_instance_id.is_(None),
    )
    live_lease = and_(
        StorageOperation.status.in_(("queued", "running", "cancel_requested")),
        StorageOperation.lease_expires_at.isnot(None),
        StorageOperation.lease_expires_at > now,
    )
    return or_(pristine_queue, live_lease)


def scopes_overlap(left: dict, right: dict) -> bool:
    if bool(left.get("global")) or bool(right.get("global")):
        return True
    dimensions = ("physical_volume_ids", "root_ids", "camera_ids", "segment_ids")
    return any(_set(left, field) & _set(right, field) for field in dimensions)


def operations_conflict(left_type: str, left_scope: dict, right_type: str, right_scope: dict) -> bool:
    if left_type not in OPERATION_TYPES or right_type not in OPERATION_TYPES:
        return True
    if bool(left_scope.get("global")) or bool(right_scope.get("global")):
        return True

    left_roots = _set(left_scope, "root_ids")
    right_roots = _set(right_scope, "root_ids")
    left_volumes = _set(left_scope, "physical_volume_ids")
    right_volumes = _set(right_scope, "physical_volume_ids")
    if left_type in ROOT_EXCLUSIVE_TYPES or right_type in ROOT_EXCLUSIVE_TYPES:
        return bool((left_roots & right_roots) or (left_volumes & right_volumes))

    left_segments = _set(left_scope, "segment_ids")
    right_segments = _set(right_scope, "segment_ids")
    if left_type in EXACT_ITEM_TYPES and right_type in EXACT_ITEM_TYPES and left_segments and right_segments:
        return bool(left_segments & right_segments)

    left_cameras = _set(left_scope, "camera_ids")
    right_cameras = _set(right_scope, "camera_ids")
    if left_cameras and right_cameras:
        return bool(left_cameras & right_cameras)
    if left_segments and right_segments:
        return bool(left_segments & right_segments)
    return bool((left_roots & right_roots) or (left_volumes & right_volumes))


def _audit_conflict(
    db: Session,
    *,
    actor: Any,
    operation_type: str,
    detail: dict[str, Any],
) -> None:
    try:
        from app.services.audit_log import create_event

        create_event(
            db=db,
            actor=actor,
            category="storage",
            event_type="storage_operation.conflict",
            severity="warning",
            message_ru="Storage operation was blocked by a coordination conflict",
            message_en="Storage operation was blocked by a coordination conflict",
            target_type="storage_operation",
            metadata={
                "operation_type": operation_type,
                "reason_code": detail.get("reason_code"),
                "conflicting_operation_type": detail.get("conflicting_operation_type"),
                "conflict_scope": detail.get("conflict_scope"),
                "retryable": bool(detail.get("retryable")),
            },
        )
    except Exception:
        db.rollback()


def active_write_conflict(operation_type: str, scope: dict, guard: ActiveRecorderWriteGuard) -> dict[str, Any] | None:
    if not guard.active or operation_type == "archive_root_activation":
        return None
    if operation_type in ACTIVE_WRITE_ROOT_BLOCKED_TYPES and (
        bool(scope.get("global")) or bool(_set(scope, "root_ids") & set(guard.root_ids))
    ):
        return {
            "reason_code": "active_recorder_write_conflict",
            "conflict_scope": "archive_root",
            "retryable": True,
        }
    if operation_type in ACTIVE_WRITE_CAMERA_BLOCKED_TYPES and (
        bool(scope.get("global")) or bool(_set(scope, "camera_ids") & set(guard.camera_ids))
    ):
        return {
            "reason_code": "active_recorder_write_conflict",
            "conflict_scope": "camera",
            "retryable": True,
        }
    if operation_type in ACTIVE_WRITE_GLOBAL_BLOCKED_TYPES:
        return {
            "reason_code": "active_recorder_write_conflict",
            "conflict_scope": "global",
            "retryable": True,
        }
    if operation_type in ACTIVE_WRITE_EXACT_TYPES and bool(_set(scope, "segment_ids") & set(guard.segment_ids)):
        return {
            "reason_code": "active_recorder_write_conflict",
            "conflict_scope": "segment",
            "retryable": True,
        }
    return None


def _coordinator_lease(db: Session, owner_instance_id: str) -> CoordinatorLeaseSession:
    deadline = time.monotonic() + COORDINATOR_WAIT_SECONDS
    while True:
        with Session(bind=db.get_bind()) as coordinator_db:
            handle = acquire_worker_lease(
                coordinator_db,
                worker_key=COORDINATOR_WORKER_KEY,
                owner_instance_id=owner_instance_id,
                lease_seconds=COORDINATOR_LEASE_SECONDS,
            )
        if handle is not None:
            session = CoordinatorLeaseSession(
                db.get_bind(),
                handle,
                lease_seconds=COORDINATOR_LEASE_SECONDS,
            )
            try:
                session.start()
            except Exception:
                session.close()
                raise
            return session
        if time.monotonic() >= deadline:
            raise StorageOperationConflict(
                {
                    "reason_code": "storage_operation_coordinator_busy",
                    "retryable": True,
                }
            )
        time.sleep(0.025)


def claim_operation_with_conflicts(
    db: Session,
    *,
    operation_type: str,
    scope: dict,
    request_identity: Any,
    actor: Any = None,
    system_owner: str | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
    parent_operation_id: str | None = None,
    owner_instance_id: str | None = None,
    initial_progress: dict | None = None,
) -> dict[str, Any]:
    if operation_type not in OPERATION_TYPES:
        raise StorageOperationContractError("storage_operation_type_unsupported")
    normalized_scope = scope_with_physical_volumes(db, scope)
    if not any(
        [
            normalized_scope.get("global"),
            normalized_scope.get("physical_volume_ids"),
            normalized_scope.get("root_ids"),
            normalized_scope.get("camera_ids"),
            normalized_scope.get("segment_ids"),
        ]
    ):
        raise StorageOperationContractError("storage_operation_scope_empty")
    instance = owner_instance_id or operation_instance_id()
    _actor_kind, actor_key, _actor_user_id, _system_owner = actor_identity(actor, system_owner=system_owner)
    fingerprint = request_fingerprint(request_identity)
    idem = str(idempotency_key or fingerprint).lower()
    existing_query = db.query(StorageOperation).filter(
        StorageOperation.actor_key == actor_key,
        StorageOperation.operation_type == operation_type,
        StorageOperation.idempotency_key == idem,
    )
    if operation_id is not None:
        existing_query = existing_query.filter(StorageOperation.id == str(operation_id))
    existing = existing_query.first()
    if existing is not None:
        now = database_now(db)
        effective = operation_effective_status(existing, now)
        if existing.status in TERMINAL_OPERATION_STATUSES or effective in ACTIVE_OPERATION_STATUSES | {"interrupted"}:
            return _claim_operation_checked(
                db,
                operation_type=operation_type,
                scope=canonical_operation_scope(existing.scope),
                request_identity=request_identity,
                actor=actor,
                system_owner=system_owner,
                operation_id=str(existing.id),
                idempotency_key=idem,
                parent_operation_id=existing.parent_operation_id,
                owner_instance_id=instance,
                scope_is_canonical=True,
                initial_progress=initial_progress,
            )
    coordinator = _coordinator_lease(db, instance)
    try:
        coordinator.assert_owned()
        now = database_now(db)
        active_rows = (
            db.query(StorageOperation)
            .filter(
                _conflict_active_filter(now)
            )
            .order_by(StorageOperation.created_at.asc(), StorageOperation.id.asc())
            .limit(MAX_ACTIVE_OPERATION_SCAN + 1)
            .all()
        )
        if len(active_rows) > MAX_ACTIVE_OPERATION_SCAN:
            raise StorageOperationConflict(
                {
                    "reason_code": "storage_operation_conflict_set_unbounded",
                    "retryable": True,
                }
            )
        for active in active_rows:
            same_identity = bool(
                active.actor_key == actor_key
                and active.operation_type == operation_type
                and active.idempotency_key == idem
                and active.request_fingerprint == fingerprint
            )
            if same_identity:
                continue
            if operations_conflict(
                operation_type,
                normalized_scope,
                str(active.operation_type),
                canonical_operation_scope(active.scope),
            ):
                detail = {
                    "reason_code": "storage_operation_scope_conflict",
                    "conflicting_operation_type": str(active.operation_type),
                    "retryable": True,
                }
                _audit_conflict(db, actor=actor, operation_type=operation_type, detail=detail)
                raise StorageOperationConflict(detail)
        recorder_conflict = active_write_conflict(operation_type, normalized_scope, active_recorder_write_guard(db))
        if recorder_conflict:
            _audit_conflict(db, actor=actor, operation_type=operation_type, detail=recorder_conflict)
            raise StorageOperationConflict(recorder_conflict)
        coordinator.assert_owned()
        claimed = _claim_operation_checked(
            db,
            operation_type=operation_type,
            scope=normalized_scope,
            request_identity=request_identity,
            actor=actor,
            system_owner=system_owner,
            operation_id=operation_id,
            idempotency_key=idem,
            parent_operation_id=parent_operation_id,
            owner_instance_id=instance,
            scope_is_canonical=True,
            initial_progress=initial_progress,
        )
        try:
            coordinator.assert_owned()
        except StorageOperationConflict:
            if claimed.get("state") == "claimed":
                try:
                    finish_operation(
                        db,
                        claimed["handle"],
                        status="failed",
                        result={"status": "failed"},
                        reason_code="storage_operation_coordinator_lease_lost",
                        retry_allowed=True,
                        retry_mode="immediate",
                    )
                except Exception:
                    db.rollback()
            raise
        return claimed
    finally:
        coordinator.close()


def reclaim_operation_with_conflicts(
    db: Session,
    *,
    operation_id: str,
    operation_type: str,
    request_identity: Any,
    idempotency_key: str,
    owner_instance_id: str | None = None,
) -> dict[str, Any]:
    if operation_type not in OPERATION_TYPES:
        raise StorageOperationContractError("storage_operation_type_unsupported")
    instance = owner_instance_id or operation_instance_id("recovery")
    coordinator = _coordinator_lease(db, instance)
    try:
        coordinator.assert_owned()
        row = db.get(StorageOperation, str(operation_id))
        fingerprint = request_fingerprint(request_identity)
        normalized_idempotency = str(idempotency_key).strip().lower()
        if row is None:
            raise StorageOperationContractError("operation_recovery_not_found")
        if not (
            row.operation_type == operation_type
            and row.idempotency_key == normalized_idempotency
            and row.request_fingerprint == fingerprint
        ):
            raise StorageOperationContractError("operation_recovery_identity_mismatch")
        normalized_scope = canonical_operation_scope(row.scope)
        now = database_now(db)
        active_rows = (
            db.query(StorageOperation)
            .filter(
                StorageOperation.id != row.id,
                _conflict_active_filter(now),
            )
            .order_by(StorageOperation.created_at.asc(), StorageOperation.id.asc())
            .limit(MAX_ACTIVE_OPERATION_SCAN + 1)
            .all()
        )
        if len(active_rows) > MAX_ACTIVE_OPERATION_SCAN:
            raise StorageOperationConflict(
                {"reason_code": "storage_operation_conflict_set_unbounded", "retryable": True}
            )
        for active in active_rows:
            if operations_conflict(
                operation_type,
                normalized_scope,
                str(active.operation_type),
                canonical_operation_scope(active.scope),
            ):
                raise StorageOperationConflict(
                    {
                        "reason_code": "storage_operation_scope_conflict",
                        "conflicting_operation_type": str(active.operation_type),
                        "retryable": True,
                    }
                )
        recorder_conflict = active_write_conflict(operation_type, normalized_scope, active_recorder_write_guard(db))
        if recorder_conflict:
            raise StorageOperationConflict(recorder_conflict)
        coordinator.assert_owned()
        claimed = reclaim_operation(
            db,
            operation_id=operation_id,
            operation_type=operation_type,
            request_identity=request_identity,
            idempotency_key=idempotency_key,
            owner_instance_id=instance,
        )
        try:
            coordinator.assert_owned()
        except StorageOperationConflict:
            if claimed.get("state") == "claimed":
                try:
                    finish_operation(
                        db,
                        claimed["handle"],
                        status="failed",
                        result={"status": "failed"},
                        reason_code="storage_operation_coordinator_lease_lost",
                        retry_allowed=True,
                        retry_mode="immediate",
                    )
                except Exception:
                    db.rollback()
            raise
        return claimed
    finally:
        coordinator.close()


def terminal_result_summary(result: dict | None) -> dict:
    source = dict(result or {})
    scalar_keys = (
        "ok",
        "status",
        "camera_delete_status",
        "camera_removed",
        "camera_id",
        "camera_name",
        "camera_preview_deleted",
        "delete_files",
        "requested_count",
        "planned_count",
        "deleted_count",
        "executed_count",
        "skipped_count",
        "failed_count",
        "bytes_freed",
        "cleanup_pending",
        "cleanup_status",
        "source_preserved",
        "total_rows",
        "updated_count",
        "segments_deleted",
        "files_deleted",
        "root_directory_removed",
        "directory_preserved",
        "metadata_recovered_count",
        "remaining_count",
        "executed_bytes",
        "target_root_id",
        "plan_id",
        "finalization_pending",
        "retry_available",
    )
    summary = {key: source.get(key) for key in scalar_keys if source.get(key) is not None}
    if "executed_count" not in summary and isinstance(source.get("executed"), list):
        summary["executed_count"] = len(source["executed"])
    if "failed_count" not in summary and isinstance(source.get("failed"), list):
        summary["failed_count"] = len(source["failed"])
    if isinstance(source.get("blockers"), list):
        summary["blocker_count"] = len(source["blockers"])
    reason_counts = source.get("reason_counts")
    if isinstance(reason_counts, dict):
        safe_counts: dict[str, int] = {}
        for key, value in sorted(reason_counts.items(), key=lambda item: str(item[0]))[:32]:
            safe_key = safe_reason_code(key, fallback="other")
            safe_counts[safe_key] = safe_counts.get(safe_key, 0) + int(value or 0)
        summary["reason_counts"] = safe_counts
    warnings = source.get("warnings")
    if isinstance(warnings, list):
        summary["warnings"] = [
            safe_reason_code(item, fallback="other")
            for item in warnings[:32]
        ]
    return summary


def terminal_status_for_result(result: dict | None) -> str:
    source = dict(result or {})
    status = str(source.get("status") or "").lower()
    if status in {"completed", "partial", "blocked", "failed", "cancelled"}:
        return status
    if source.get("ok") is True and not int(source.get("failed_count") or 0) and not int(source.get("skipped_count") or 0):
        return "completed"
    if int(source.get("deleted_count") or source.get("executed_count") or 0) > 0:
        return "partial"
    return "failed"
