from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.storage_operation import StorageOperation, StorageWorkerLease, StorageWorkSignal


OPERATION_SCOPE_MAX_BYTES = 4096
OPERATION_PROGRESS_MAX_BYTES = 4096
OPERATION_RESULT_MAX_BYTES = 8192
OPERATION_MAX_KEYS = 32
OPERATION_MAX_LIST_ITEMS = 64
OPERATION_MAX_DEPTH = 4
OPERATION_MAX_STRING_BYTES = 512
OPERATION_LEASE_SECONDS = 180
WORKER_LEASE_SECONDS = 180
SIGNAL_LEASE_SECONDS = 180
WORK_SIGNAL_TYPES = frozenset({"retention_evaluate"})
WORK_SIGNAL_MAX_ROWS = 512
WORK_SIGNAL_IDLE_MAX_ROWS = 256
WORK_SIGNAL_NAMESPACE_LEASE_SECONDS = 15
ACTIVE_SUMMARY_LIMIT = 8
INTERRUPTED_SUMMARY_LIMIT = 8
RECENT_SUMMARY_LIMIT = 20
TERMINAL_HISTORY_DAYS = 30
TERMINAL_HISTORY_MAX_ROWS = 500
TERMINAL_CLEANUP_BATCH = 500
MAX_RETRY_DEPTH = 4
MAX_RETRIES_PER_PARENT = 8

TERMINAL_OPERATION_STATUSES = frozenset({"completed", "partial", "blocked", "failed", "cancelled"})
ACTIVE_OPERATION_STATUSES = frozenset({"queued", "running", "cancel_requested"})
ALL_OPERATION_STATUSES = ACTIVE_OPERATION_STATUSES | TERMINAL_OPERATION_STATUSES
PUBLIC_OPERATION_STATUSES = ALL_OPERATION_STATUSES | {"interrupted"}
INTERRUPTED_RECOVERABLE_TYPES = frozenset(
    {
        "archive_root_activation",
        "manual_single_delete",
        "manual_bulk_delete",
        "manual_delete_by_camera",
        "manual_delete_all",
        "retention_auto_run",
        "retention_auto_free_space",
        "integrity_scan",
        "integrity_metadata_repair",
        "integrity_catalog_retirement",
        "integrity_recording_delete",
        "orphan_file_cleanup",
    }
)
SIGNAL_STATUSES = frozenset({"idle", "pending", "running"})
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
CANONICAL_VOLUME_RE = re.compile(r"^pv1:[0-9a-f]{32}$")
LEGACY_CANONICAL_VOLUME_RE = re.compile(r"^[0-9a-f]{32}$")
ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")
SENSITIVE_KEYS = frozenset(
    {
        "absolute_path",
        "authorization",
        "cookie",
        "credentials",
        "owner_token",
        "password",
        "raw_path",
        "secret",
        "token",
        "traceback",
    }
)


class StorageOperationContractError(ValueError):
    pass


class StorageOperationLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationHandle:
    operation_id: str
    owner_token: str
    fencing_token: int
    operation_type: str


@dataclass(frozen=True)
class WorkerLeaseHandle:
    worker_key: str
    owner_token: str
    owner_instance_id: str
    fencing_token: int


@dataclass(frozen=True)
class WorkSignalHandle:
    signal_type: str
    scope_key: str
    owner_token: str
    owner_instance_id: str
    fencing_token: int
    claimed_watermark: int


class OperationHeartbeatController:
    """Refresh an operation lease in an independent transaction when callers report progress."""

    def __init__(self, bind: Any, handle: OperationHandle, *, interval_seconds: float = 45.0):
        self.bind = bind
        self.handle = handle
        self.interval_seconds = max(1.0, float(interval_seconds))
        self._last_heartbeat = time.monotonic()

    def touch(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_heartbeat < self.interval_seconds:
            return
        with Session(bind=self.bind) as heartbeat_db:
            heartbeat_operation(heartbeat_db, self.handle)
        self._last_heartbeat = now


def _db_now(db: Session) -> datetime:
    value = db.execute(select(func.current_timestamp())).scalar_one()
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    raise StorageOperationContractError("database_time_unavailable")


def database_now(db: Session) -> datetime:
    return _db_now(db)


def _code(value: str, *, field: str, max_length: int = 96) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) > max_length or not CODE_RE.fullmatch(normalized):
        raise StorageOperationContractError(f"invalid_{field}")
    return normalized


def safe_reason_code(value: Any, *, fallback: str | None = None) -> str | None:
    candidate = value
    if isinstance(candidate, dict):
        candidate = next(
            (
                candidate.get(key)
                for key in ("reason_code", "reason", "error", "code")
                if candidate.get(key) is not None
            ),
            None,
        )
    if isinstance(candidate, (list, tuple)):
        candidate = candidate[0] if candidate else None
    if candidate is not None:
        try:
            return _code(str(candidate), field="reason_code")
        except StorageOperationContractError:
            pass
    return _code(fallback, field="reason_code") if fallback else None


def _operation_id(value: str | None, *, prefix: str) -> str:
    if value is None:
        return _opaque_id(prefix)
    normalized = str(value).strip()
    if not OPAQUE_ID_RE.fullmatch(normalized):
        raise StorageOperationContractError("invalid_operation_id")
    return normalized


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _opaque_id(prefix: str) -> str:
    safe_prefix = re.sub(r"[^a-z0-9-]", "-", str(prefix).lower()).strip("-")[:32] or "storage-op"
    return f"{safe_prefix}-{uuid.uuid4().hex}"


def request_fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_value(value: Any, *, depth: int = 0) -> None:
    if depth > OPERATION_MAX_DEPTH:
        raise StorageOperationContractError("operation_payload_depth_exceeded")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StorageOperationContractError("operation_payload_number_invalid")
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > OPERATION_MAX_STRING_BYTES:
            raise StorageOperationContractError("operation_payload_string_too_large")
        if ABSOLUTE_PATH_RE.match(value.strip()):
            raise StorageOperationContractError("operation_payload_absolute_path_forbidden")
        return
    if isinstance(value, list):
        if len(value) > OPERATION_MAX_LIST_ITEMS:
            raise StorageOperationContractError("operation_payload_list_too_large")
        for item in value:
            _validate_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > OPERATION_MAX_KEYS:
            raise StorageOperationContractError("operation_payload_too_many_keys")
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise StorageOperationContractError("operation_payload_key_type_unsupported")
            key = raw_key
            if len(key.encode("utf-8")) > 96:
                raise StorageOperationContractError("operation_payload_key_too_large")
            normalized_key = key.strip().lower()
            if normalized_key in SENSITIVE_KEYS or any(
                marker in normalized_key
                for marker in ("password", "secret", "credential", "authorization", "cookie", "traceback", "raw_path", "absolute_path", "owner_token")
            ):
                raise StorageOperationContractError("operation_payload_sensitive_key")
            _validate_value(item, depth=depth + 1)
        return
    raise StorageOperationContractError("operation_payload_type_unsupported")


def bounded_payload(value: dict | None, *, max_bytes: int, field: str) -> dict:
    payload = dict(value or {})
    _validate_value(payload)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise StorageOperationContractError(f"{field}_payload_too_large")
    return payload


def _hashed_resource(value: Any) -> str:
    candidate = str(value).strip().lower()
    if CANONICAL_VOLUME_RE.fullmatch(candidate):
        return candidate
    return f"pv1:{hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:32]}"


def _normalized_scope(source: dict, *, trusted_stored: bool) -> dict:
    allowed = {"global", "physical_volume_ids", "root_ids", "camera_ids", "segment_ids", "scope_escalated"}
    if set(source) - allowed:
        raise StorageOperationContractError("operation_scope_field_unsupported")

    global_scope = bool(source.get("global"))
    roots = sorted({str(item)[:96] for item in source.get("root_ids") or [] if str(item)})
    cameras = sorted({int(item) for item in source.get("camera_ids") or []})
    segments = sorted({int(item) for item in source.get("segment_ids") or []})
    volumes: set[str] = set()
    for item in source.get("physical_volume_ids") or []:
        candidate = str(item).strip().lower()
        if not candidate:
            continue
        if trusted_stored:
            if CANONICAL_VOLUME_RE.fullmatch(candidate):
                volumes.add(candidate)
            elif LEGACY_CANONICAL_VOLUME_RE.fullmatch(candidate):
                volumes.add(f"pv1:{candidate}")
            else:
                raise StorageOperationContractError("operation_stored_scope_volume_invalid")
        else:
            volumes.add(_hashed_resource(item))
    normalized_volumes = sorted(volumes)
    escalated = bool(source.get("scope_escalated"))

    if len(segments) > OPERATION_MAX_LIST_ITEMS:
        if cameras or roots:
            segments = []
            escalated = True
        else:
            global_scope = True
            segments = []
            escalated = True
    if (
        len(cameras) > OPERATION_MAX_LIST_ITEMS
        or len(roots) > OPERATION_MAX_LIST_ITEMS
        or len(normalized_volumes) > OPERATION_MAX_LIST_ITEMS
    ):
        global_scope = True
        roots = []
        cameras = []
        segments = []
        normalized_volumes = []
        escalated = True

    scope = {
        "global": global_scope,
        "physical_volume_ids": normalized_volumes,
        "root_ids": roots,
        "camera_ids": cameras,
        "segment_ids": segments,
        "scope_escalated": escalated,
    }
    return bounded_payload(scope, max_bytes=OPERATION_SCOPE_MAX_BYTES, field="scope")


def normalize_operation_scope(value: dict | None) -> dict:
    return _normalized_scope(dict(value or {}), trusted_stored=False)


def canonical_operation_scope(value: dict | None) -> dict:
    return _normalized_scope(dict(value or {}), trusted_stored=True)


def operation_has_prior_ownership(row: StorageOperation) -> bool:
    return bool(
        int(row.fencing_token or 0) > 0
        or row.started_at is not None
        or row.heartbeat_at is not None
        or row.owner_token_hash
        or row.owner_instance_id
    )


def operation_effective_status(row: StorageOperation, now: datetime) -> str:
    lifecycle = str(row.status)
    if lifecycle not in ACTIVE_OPERATION_STATUSES:
        return lifecycle if lifecycle in TERMINAL_OPERATION_STATUSES else "unknown"
    if lifecycle == "queued" and not operation_has_prior_ownership(row):
        return "queued"
    if row.lease_expires_at is not None and row.lease_expires_at > now:
        return lifecycle
    return "interrupted"


def actor_identity(actor: Any = None, *, system_owner: str | None = None) -> tuple[str, str, int | None, str | None]:
    user_id = getattr(actor, "id", None)
    if user_id is not None:
        return "user", f"user:{int(user_id)}", int(user_id), None
    owner = _code(system_owner or "system", field="system_owner", max_length=64)
    return "system", f"system:{owner}", None, owner


def _write_operation_audit(
    db: Session,
    row: StorageOperation,
    *,
    actor: Any = None,
    event_type: str,
    severity: str = "info",
    metadata: dict | None = None,
) -> None:
    try:
        from app.services.audit_log import create_event

        create_event(
            db=db,
            actor=actor,
            category="storage",
            event_type=event_type,
            severity=severity,
            message_ru="Storage operation state changed",
            message_en="Storage operation state changed",
            target_type="storage_operation",
            target_id=row.id,
            metadata={
                "operation_type": row.operation_type,
                "status": row.status,
                "actor_kind": row.actor_kind,
                **dict(metadata or {}),
            },
        )
    except Exception:
        db.rollback()


def _owner_matches(row: StorageOperation, token: str, fencing_token: int, now: datetime) -> bool:
    return bool(
        row.status in {"running", "cancel_requested"}
        and row.owner_token_hash == _token_hash(token)
        and int(row.fencing_token or 0) == int(fencing_token)
        and row.lease_expires_at is not None
        and row.lease_expires_at > now
    )


def claim_operation(
    db: Session,
    *,
    operation_type: str,
    scope: dict,
    request_identity: Any,
    actor: Any = None,
    system_owner: str | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
    owner_instance_id: str,
    lease_seconds: int = OPERATION_LEASE_SECONDS,
    parent_operation_id: str | None = None,
    start_immediately: bool = True,
    cancel_allowed: bool = False,
    scope_is_canonical: bool = False,
    initial_progress: dict | None = None,
) -> dict[str, Any]:
    op_type = _code(operation_type, field="operation_type", max_length=64)
    normalized_scope = (
        canonical_operation_scope(scope)
        if scope_is_canonical
        else normalize_operation_scope(scope)
    )
    fingerprint = request_fingerprint(request_identity)
    bounded_initial_progress = bounded_payload(
        initial_progress,
        max_bytes=OPERATION_PROGRESS_MAX_BYTES,
        field="progress",
    )
    idem = _code(idempotency_key or fingerprint, field="idempotency_key", max_length=64)
    actor_kind, actor_key, actor_user_id, normalized_system_owner = actor_identity(actor, system_owner=system_owner)
    now = _db_now(db)
    requested_operation_id = _operation_id(operation_id, prefix=op_type) if operation_id is not None else None
    normalized_parent_id = _operation_id(parent_operation_id, prefix=op_type) if parent_operation_id else None
    parent_snapshot = None
    retry_depth = 0
    if normalized_parent_id is not None:
        parent = db.get(StorageOperation, normalized_parent_id)
        if (
            parent is None
            or parent.actor_key != actor_key
            or parent.operation_type != op_type
            or parent.status not in TERMINAL_OPERATION_STATUSES
        ):
            db.rollback()
            raise StorageOperationContractError("operation_retry_parent_invalid")
        retry_depth = int(parent.retry_depth or 0) + 1
        if retry_depth > MAX_RETRY_DEPTH:
            db.rollback()
            raise StorageOperationContractError("operation_retry_depth_exceeded")
        retry_count = int(
            db.query(func.count(StorageOperation.id))
            .filter(StorageOperation.parent_operation_id == normalized_parent_id)
            .scalar()
            or 0
        )
        if retry_count >= MAX_RETRIES_PER_PARENT:
            db.rollback()
            raise StorageOperationContractError("operation_retry_parent_limit_reached")
        parent_snapshot = bounded_payload(
            {
                "operation_id": str(parent.id),
                "status": str(parent.status),
                "reason_code": parent.reason_code,
                "finished_at": parent.finished_at.isoformat() if parent.finished_at else None,
                "retry_depth": int(parent.retry_depth or 0),
            },
            max_bytes=1024,
            field="parent_snapshot",
        )

    row = None
    if requested_operation_id is not None:
        row = (
            db.query(StorageOperation)
            .filter(StorageOperation.id == requested_operation_id)
            .with_for_update()
            .first()
        )
        if row is not None and not (
            row.actor_key == actor_key
            and row.operation_type == op_type
            and row.idempotency_key == idem
            and row.request_fingerprint == fingerprint
            and canonical_operation_scope(row.scope) == normalized_scope
            and row.parent_operation_id == normalized_parent_id
        ):
            db.rollback()
            raise StorageOperationContractError("operation_identity_mismatch")
    if row is None:
        row = (
            db.query(StorageOperation)
            .filter(
                StorageOperation.actor_key == actor_key,
                StorageOperation.operation_type == op_type,
                StorageOperation.idempotency_key == idem,
            )
            .with_for_update()
            .first()
        )
    created = row is None
    if row is None:
        row = StorageOperation(
            id=requested_operation_id or _operation_id(None, prefix=op_type),
            operation_type=op_type,
            actor_kind=actor_kind,
            actor_key=actor_key,
            actor_user_id=actor_user_id,
            system_owner=normalized_system_owner,
            idempotency_key=idem,
            request_fingerprint=fingerprint,
            status="queued",
            scope=normalized_scope,
            progress=bounded_initial_progress,
            parent_operation_id=normalized_parent_id,
            parent_snapshot=parent_snapshot,
            retry_depth=retry_depth,
            queued_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return claim_operation(
                db,
                operation_type=op_type,
                scope=scope,
                request_identity=request_identity,
                actor=actor,
                system_owner=system_owner,
                operation_id=operation_id,
                idempotency_key=idem,
                owner_instance_id=owner_instance_id,
                lease_seconds=lease_seconds,
                parent_operation_id=parent_operation_id,
                start_immediately=start_immediately,
                cancel_allowed=cancel_allowed,
                scope_is_canonical=scope_is_canonical,
                initial_progress=bounded_initial_progress,
            )
    elif (
        row.request_fingerprint != fingerprint
        or canonical_operation_scope(row.scope) != normalized_scope
        or row.parent_operation_id != normalized_parent_id
    ):
        db.rollback()
        raise StorageOperationContractError("operation_idempotency_identity_mismatch")

    if row.status in TERMINAL_OPERATION_STATUSES:
        db.commit()
        return {
            "state": "terminal",
            "operation": public_operation_summary(row, now=now),
            "terminal_result": bounded_payload(row.result, max_bytes=OPERATION_RESULT_MAX_BYTES, field="result"),
        }
    if row.status in {"running", "cancel_requested"} and row.lease_expires_at and row.lease_expires_at > now:
        db.commit()
        return {"state": "running", "operation": public_operation_summary(row, now=now)}
    if not created and operation_effective_status(row, now) == "interrupted":
        db.commit()
        return {"state": "interrupted", "operation": public_operation_summary(row, now=now)}
    if not start_immediately:
        if row.status != "queued":
            db.rollback()
            raise StorageOperationContractError("operation_queue_transition_invalid")
        row.cancel_allowed = bool(cancel_allowed)
        row.lease_expires_at = now + timedelta(seconds=max(5, int(lease_seconds)))
        row.updated_at = now
        db.add(row)
        db.commit()
        if created:
            _write_operation_audit(
                db,
                row,
                actor=actor,
                event_type="storage_operation.queued",
            )
        return {"state": "queued", "operation": public_operation_summary(row, now=now)}

    takeover = not created and int(row.fencing_token or 0) > 0
    token = secrets.token_urlsafe(32)
    row.status = "cancel_requested" if row.status == "cancel_requested" else "running"
    row.owner_token_hash = _token_hash(token)
    row.owner_instance_id = str(owner_instance_id)[:128]
    row.fencing_token = int(row.fencing_token or 0) + 1
    row.revision = int(row.revision or 0) + 1
    row.started_at = row.started_at or now
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(5, int(lease_seconds)))
    row.updated_at = now
    row.cancel_allowed = bool(cancel_allowed)
    db.add(row)
    db.commit()
    _write_operation_audit(
        db,
        row,
        actor=actor,
        event_type="storage_operation.taken_over" if takeover else "storage_operation.started",
        severity="warning" if takeover else "info",
        metadata={"takeover": takeover},
    )
    return {
        "state": "claimed",
        "operation": public_operation_summary(row, now=now),
        "handle": OperationHandle(row.id, token, int(row.fencing_token), row.operation_type),
    }


def reclaim_operation(
    db: Session,
    *,
    operation_id: str,
    operation_type: str,
    request_identity: Any,
    idempotency_key: str,
    owner_instance_id: str,
    lease_seconds: int = OPERATION_LEASE_SECONDS,
) -> dict[str, Any]:
    """Internally reclaim an existing durable operation without changing its actor identity."""
    normalized_id = _operation_id(operation_id, prefix=operation_type)
    normalized_type = _code(operation_type, field="operation_type", max_length=64)
    fingerprint = request_fingerprint(request_identity)
    normalized_idempotency = _code(idempotency_key, field="idempotency_key", max_length=64)
    now = _db_now(db)
    row = (
        db.query(StorageOperation)
        .filter(StorageOperation.id == normalized_id)
        .with_for_update()
        .first()
    )
    if row is None:
        db.rollback()
        raise StorageOperationContractError("operation_recovery_not_found")
    if not (
        row.operation_type == normalized_type
        and row.idempotency_key == normalized_idempotency
        and row.request_fingerprint == fingerprint
    ):
        db.rollback()
        raise StorageOperationContractError("operation_recovery_identity_mismatch")
    if row.status in TERMINAL_OPERATION_STATUSES:
        db.commit()
        return {
            "state": "terminal",
            "operation": public_operation_summary(row, now=now),
            "terminal_result": bounded_payload(row.result, max_bytes=OPERATION_RESULT_MAX_BYTES, field="result"),
        }
    if row.status in {"running", "cancel_requested"} and row.lease_expires_at and row.lease_expires_at > now:
        db.commit()
        return {"state": "running", "operation": public_operation_summary(row, now=now)}
    if row.status not in ACTIVE_OPERATION_STATUSES:
        db.rollback()
        raise StorageOperationContractError("operation_recovery_transition_invalid")

    takeover = int(row.fencing_token or 0) > 0
    token = secrets.token_urlsafe(32)
    row.status = "cancel_requested" if row.status == "cancel_requested" else "running"
    row.owner_token_hash = _token_hash(token)
    row.owner_instance_id = str(owner_instance_id)[:128]
    row.fencing_token = int(row.fencing_token or 0) + 1
    row.revision = int(row.revision or 0) + 1
    row.started_at = row.started_at or now
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(5, int(lease_seconds)))
    row.updated_at = now
    db.add(row)
    db.commit()
    _write_operation_audit(
        db,
        row,
        event_type="storage_operation.taken_over" if takeover else "storage_operation.started",
        severity="warning" if takeover else "info",
        metadata={"takeover": takeover, "recovery": True},
    )
    return {
        "state": "claimed",
        "operation": public_operation_summary(row, now=now),
        "handle": OperationHandle(row.id, token, int(row.fencing_token), row.operation_type),
    }


def create_operation(
    db: Session,
    *,
    operation_type: str,
    scope: dict,
    request_identity: Any,
    actor: Any = None,
    system_owner: str | None = None,
    operation_id: str | None = None,
    idempotency_key: str | None = None,
    owner_instance_id: str = "queued",
    lease_seconds: int = OPERATION_LEASE_SECONDS,
    parent_operation_id: str | None = None,
    cancel_allowed: bool = False,
    initial_progress: dict | None = None,
) -> dict[str, Any]:
    return claim_operation(
        db,
        operation_type=operation_type,
        scope=scope,
        request_identity=request_identity,
        actor=actor,
        system_owner=system_owner,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        owner_instance_id=owner_instance_id,
        lease_seconds=lease_seconds,
        parent_operation_id=parent_operation_id,
        start_immediately=False,
        cancel_allowed=cancel_allowed,
        initial_progress=initial_progress,
    )


def assert_operation_owned(db: Session, handle: OperationHandle) -> StorageOperation:
    now = _db_now(db)
    row = db.get(StorageOperation, handle.operation_id)
    if row is None or not _owner_matches(row, handle.owner_token, handle.fencing_token, now):
        raise StorageOperationLeaseLost("storage_operation_lease_lost")
    return row


def request_operation_cancel(
    db: Session,
    operation_id: str,
    *,
    actor: Any = None,
    system_owner: str | None = None,
) -> dict:
    _actor_kind, actor_key, _actor_user_id, _normalized_system_owner = actor_identity(
        actor,
        system_owner=system_owner,
    )
    now = _db_now(db)
    row = (
        db.query(StorageOperation)
        .filter(StorageOperation.id == _operation_id(operation_id, prefix="storage-operation"))
        .with_for_update()
        .first()
    )
    if row is None:
        db.rollback()
        raise StorageOperationContractError("storage_operation_not_found")
    if row.actor_key != actor_key:
        db.rollback()
        raise StorageOperationContractError("storage_operation_actor_mismatch")
    if row.status in TERMINAL_OPERATION_STATUSES:
        db.commit()
        return public_operation_summary(row, now=now)
    if not row.cancel_allowed:
        db.rollback()
        raise StorageOperationContractError("storage_operation_cancel_not_allowed")
    if row.status not in {"queued", "running", "cancel_requested"}:
        db.rollback()
        raise StorageOperationContractError("storage_operation_cancel_transition_invalid")
    if row.status == "queued":
        row.status = "cancelled"
        row.finished_at = now
        row.lease_expires_at = None
        row.cancel_allowed = False
    else:
        row.status = "cancel_requested"
    row.updated_at = now
    row.revision = int(row.revision or 0) + 1
    db.add(row)
    db.commit()
    _write_operation_audit(
        db,
        row,
        actor=actor,
        event_type="storage_operation.cancelled" if row.status == "cancelled" else "storage_operation.cancel_requested",
        severity="warning",
    )
    return public_operation_summary(row, now=now)


def operation_cancel_requested(db: Session, handle: OperationHandle) -> bool:
    return assert_operation_owned(db, handle).status == "cancel_requested"


def heartbeat_operation(
    db: Session,
    handle: OperationHandle,
    *,
    progress: dict | None = None,
    lease_seconds: int = OPERATION_LEASE_SECONDS,
) -> dict:
    bounded = bounded_payload(progress, max_bytes=OPERATION_PROGRESS_MAX_BYTES, field="progress") if progress is not None else None
    now = _db_now(db)
    values: dict[Any, Any] = {
        StorageOperation.heartbeat_at: now,
        StorageOperation.lease_expires_at: now + timedelta(seconds=max(5, int(lease_seconds))),
        StorageOperation.revision: StorageOperation.revision + 1,
        StorageOperation.updated_at: now,
    }
    if bounded is not None:
        values[StorageOperation.progress] = bounded
    updated = (
        db.query(StorageOperation)
        .filter(
            StorageOperation.id == handle.operation_id,
            StorageOperation.status.in_(("running", "cancel_requested")),
            StorageOperation.owner_token_hash == _token_hash(handle.owner_token),
            StorageOperation.fencing_token == handle.fencing_token,
            StorageOperation.lease_expires_at.isnot(None),
            StorageOperation.lease_expires_at > now,
        )
        .update(values, synchronize_session=False)
    )
    if updated != 1:
        db.rollback()
        raise StorageOperationLeaseLost("storage_operation_lease_lost")
    db.commit()
    row = db.get(StorageOperation, handle.operation_id)
    return public_operation_summary(row, now=now)


def finish_operation(
    db: Session,
    handle: OperationHandle,
    *,
    status: str,
    result: dict | None = None,
    progress: dict | None = None,
    reason_code: str | None = None,
    next_action: str | None = None,
    retry_mode: str | None = None,
    retry_allowed: bool = False,
) -> dict:
    try:
        now = stage_operation_terminal(
            db,
            handle,
            status=status,
            result=result,
            progress=progress,
            reason_code=reason_code,
            next_action=next_action,
            retry_mode=retry_mode,
            retry_allowed=retry_allowed,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = db.get(StorageOperation, handle.operation_id)
    ensure_operation_terminal_audit(db, row)
    return public_operation_summary(row, now=now)


def stage_operation_terminal(
    db: Session,
    handle: OperationHandle,
    *,
    status: str,
    result: dict | None = None,
    progress: dict | None = None,
    reason_code: str | None = None,
    next_action: str | None = None,
    retry_mode: str | None = None,
    retry_allowed: bool = False,
) -> datetime:
    """Stage a fenced terminal update in the caller's current transaction."""
    terminal = _code(status, field="terminal_status", max_length=32)
    if terminal not in TERMINAL_OPERATION_STATUSES:
        raise StorageOperationContractError("operation_terminal_status_required")
    bounded_result = bounded_payload(result, max_bytes=OPERATION_RESULT_MAX_BYTES, field="result")
    bounded_progress = (
        bounded_payload(progress, max_bytes=OPERATION_PROGRESS_MAX_BYTES, field="progress")
        if progress is not None
        else None
    )
    now = _db_now(db)
    values = {
        StorageOperation.status: terminal,
        StorageOperation.result: bounded_result,
        StorageOperation.reason_code: safe_reason_code(reason_code),
        StorageOperation.next_action: _code(next_action, field="next_action") if next_action else None,
        StorageOperation.retry_mode: _code(retry_mode, field="retry_mode", max_length=32) if retry_mode else None,
        StorageOperation.retry_allowed: bool(retry_allowed),
        StorageOperation.cancel_allowed: False,
        StorageOperation.finished_at: now,
        StorageOperation.heartbeat_at: now,
        StorageOperation.lease_expires_at: None,
        StorageOperation.owner_token_hash: None,
        StorageOperation.owner_instance_id: None,
        StorageOperation.revision: StorageOperation.revision + 1,
        StorageOperation.updated_at: now,
    }
    if bounded_progress is not None:
        values[StorageOperation.progress] = bounded_progress
    updated = (
        db.query(StorageOperation)
        .filter(
            StorageOperation.id == handle.operation_id,
            StorageOperation.status.in_(("running", "cancel_requested")),
            StorageOperation.owner_token_hash == _token_hash(handle.owner_token),
            StorageOperation.fencing_token == handle.fencing_token,
            StorageOperation.lease_expires_at.isnot(None),
            StorageOperation.lease_expires_at > now,
        )
        .update(values, synchronize_session=False)
    )
    if updated != 1:
        raise StorageOperationLeaseLost("storage_operation_lease_lost")
    return now


def ensure_operation_terminal_audit(db: Session, row: StorageOperation | None) -> None:
    if row is None or row.status not in TERMINAL_OPERATION_STATUSES:
        return
    from app.models.audit_event import AuditEvent

    exists = (
        db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == "storage_operation.finished",
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == str(row.id),
        )
        .first()
    )
    if exists is not None:
        return
    _write_operation_audit(
        db,
        row,
        event_type="storage_operation.finished",
        severity="info" if row.status == "completed" else "warning",
        metadata={"reason_code": row.reason_code, "retry_allowed": bool(row.retry_allowed)},
    )


def public_operation_summary(row: StorageOperation, *, now: datetime) -> dict:
    progress = bounded_payload(row.progress, max_bytes=OPERATION_PROGRESS_MAX_BYTES, field="progress")
    effective_status = operation_effective_status(row, now)
    return {
        "operation_id": str(row.id),
        "operation_type": str(row.operation_type),
        "status": effective_status if effective_status in PUBLIC_OPERATION_STATUSES else "unknown",
        "interrupted": effective_status == "interrupted",
        "recoverable": effective_status == "interrupted" and str(row.operation_type) in INTERRUPTED_RECOVERABLE_TYPES,
        "progress": progress,
        "reason_code": row.reason_code,
        "next_action": row.next_action,
        "retry_mode": row.retry_mode,
        "retry_allowed": bool(row.retry_allowed),
        "cancel_allowed": bool(row.cancel_allowed),
        "queued_at": row.queued_at.isoformat() if row.queued_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def operation_summaries(db: Session) -> dict:
    now = _db_now(db)
    prior_ownership = or_(
        StorageOperation.fencing_token > 0,
        StorageOperation.started_at.isnot(None),
        StorageOperation.heartbeat_at.isnot(None),
        StorageOperation.owner_token_hash.isnot(None),
        StorageOperation.owner_instance_id.isnot(None),
    )
    pristine_queue = and_(StorageOperation.status == "queued", ~prior_ownership)
    live_lease = and_(
        StorageOperation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
        StorageOperation.lease_expires_at.isnot(None),
        StorageOperation.lease_expires_at > now,
    )
    active = (
        db.query(StorageOperation)
        .filter(or_(pristine_queue, live_lease))
        .order_by(StorageOperation.updated_at.desc(), StorageOperation.id.asc())
        .limit(ACTIVE_SUMMARY_LIMIT)
        .all()
    )
    interrupted = (
        db.query(StorageOperation)
        .filter(
            StorageOperation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
            prior_ownership,
            or_(
                StorageOperation.lease_expires_at.is_(None),
                StorageOperation.lease_expires_at <= now,
            ),
        )
        .order_by(StorageOperation.updated_at.desc(), StorageOperation.id.asc())
        .limit(INTERRUPTED_SUMMARY_LIMIT)
        .all()
    )
    recent = (
        db.query(StorageOperation)
        .filter(StorageOperation.status.in_(tuple(TERMINAL_OPERATION_STATUSES)))
        .order_by(StorageOperation.finished_at.desc(), StorageOperation.id.asc())
        .limit(RECENT_SUMMARY_LIMIT)
        .all()
    )
    return {
        "available": True,
        "active": [public_operation_summary(item, now=now) for item in active],
        "interrupted": [public_operation_summary(item, now=now) for item in interrupted],
        "recent": [public_operation_summary(item, now=now) for item in recent],
    }


def cleanup_terminal_operations(db: Session, *, now: datetime | None = None) -> int:
    current = now or _db_now(db)
    cutoff = current - timedelta(days=TERMINAL_HISTORY_DAYS)
    terminal_query = db.query(StorageOperation).filter(StorageOperation.status.in_(tuple(TERMINAL_OPERATION_STATUSES)))
    keep_ids = [
        item[0]
        for item in terminal_query.with_entities(StorageOperation.id)
        .order_by(StorageOperation.finished_at.desc(), StorageOperation.id.asc())
        .limit(TERMINAL_HISTORY_MAX_ROWS)
        .all()
    ]
    delete_conditions = [StorageOperation.finished_at < cutoff]
    if keep_ids:
        delete_conditions.append(~StorageOperation.id.in_(keep_ids))
    delete_query = terminal_query.filter(
        StorageOperation.finished_at.isnot(None),
        or_(*delete_conditions),
    )
    delete_rows = (
        delete_query.order_by(StorageOperation.finished_at.asc(), StorageOperation.id.asc())
        .with_for_update()
        .limit(TERMINAL_CLEANUP_BATCH)
        .all()
    )
    delete_ids = [str(row.id) for row in delete_rows]
    if not delete_ids:
        db.commit()
        return 0
    child_limit = TERMINAL_CLEANUP_BATCH * MAX_RETRIES_PER_PARENT
    children = (
        db.query(StorageOperation)
        .filter(
            StorageOperation.parent_operation_id.in_(delete_ids),
            ~StorageOperation.id.in_(delete_ids),
        )
        .order_by(StorageOperation.id.asc())
        .with_for_update()
        .limit(child_limit + 1)
        .all()
    )
    if len(children) > child_limit:
        db.rollback()
        raise StorageOperationContractError("operation_lineage_cleanup_bound_exceeded")
    parent_by_id = {str(row.id): row for row in delete_rows}
    for child in children:
        parent = parent_by_id.get(str(child.parent_operation_id))
        if child.parent_snapshot is None and parent is not None:
            child.parent_snapshot = bounded_payload(
                {
                    "operation_id": str(parent.id),
                    "status": str(parent.status),
                    "reason_code": parent.reason_code,
                    "finished_at": parent.finished_at.isoformat() if parent.finished_at else None,
                    "retry_depth": int(parent.retry_depth or 0),
                },
                max_bytes=1024,
                field="parent_snapshot",
            )
        child.parent_operation_id = None
        db.add(child)
    deleted = int(
        db.query(StorageOperation)
        .filter(StorageOperation.id.in_(delete_ids))
        .delete(synchronize_session=False)
        or 0
    )
    db.commit()
    return deleted


def acquire_worker_lease(
    db: Session,
    *,
    worker_key: str,
    owner_instance_id: str,
    lease_seconds: int = WORKER_LEASE_SECONDS,
) -> WorkerLeaseHandle | None:
    key = _code(worker_key, field="worker_key")
    now = _db_now(db)
    row = (
        db.query(StorageWorkerLease)
        .filter(StorageWorkerLease.worker_key == key)
        .with_for_update()
        .first()
    )
    if row is not None and row.lease_expires_at > now:
        db.rollback()
        return None
    token = secrets.token_urlsafe(32)
    if row is None:
        row = StorageWorkerLease(
            worker_key=key,
            owner_token_hash=_token_hash(token),
            owner_instance_id=str(owner_instance_id)[:128],
            fencing_token=1,
            lease_expires_at=now + timedelta(seconds=max(5, int(lease_seconds))),
            heartbeat_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
    else:
        row.owner_token_hash = _token_hash(token)
        row.owner_instance_id = str(owner_instance_id)[:128]
        row.fencing_token = int(row.fencing_token or 0) + 1
        row.heartbeat_at = now
        row.lease_expires_at = now + timedelta(seconds=max(5, int(lease_seconds)))
        row.updated_at = now
        db.add(row)
        db.commit()
    return WorkerLeaseHandle(key, token, str(owner_instance_id)[:128], int(row.fencing_token))


def renew_worker_lease(
    db: Session,
    handle: WorkerLeaseHandle,
    *,
    lease_seconds: int = WORKER_LEASE_SECONDS,
) -> None:
    now = _db_now(db)
    updated = (
        db.query(StorageWorkerLease)
        .filter(
            StorageWorkerLease.worker_key == handle.worker_key,
            StorageWorkerLease.owner_token_hash == _token_hash(handle.owner_token),
            StorageWorkerLease.owner_instance_id == handle.owner_instance_id,
            StorageWorkerLease.fencing_token == handle.fencing_token,
            StorageWorkerLease.lease_expires_at > now,
        )
        .update(
            {
                StorageWorkerLease.heartbeat_at: now,
                StorageWorkerLease.lease_expires_at: now + timedelta(seconds=max(5, int(lease_seconds))),
                StorageWorkerLease.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise StorageOperationLeaseLost("storage_worker_lease_lost")
    db.commit()


def release_worker_lease(db: Session, handle: WorkerLeaseHandle) -> bool:
    now = _db_now(db)
    updated = (
        db.query(StorageWorkerLease)
        .filter(
            StorageWorkerLease.worker_key == handle.worker_key,
            StorageWorkerLease.owner_token_hash == _token_hash(handle.owner_token),
            StorageWorkerLease.owner_instance_id == handle.owner_instance_id,
            StorageWorkerLease.fencing_token == handle.fencing_token,
        )
        .update(
            {
                StorageWorkerLease.lease_expires_at: now,
                StorageWorkerLease.heartbeat_at: now,
                StorageWorkerLease.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


def normalize_work_signal_scope(scope: dict) -> dict:
    normalized = normalize_operation_scope(scope)
    if normalized.get("segment_ids"):
        raise StorageOperationContractError("work_signal_segment_scope_forbidden")
    if not any(
        (
            normalized.get("global"),
            normalized.get("physical_volume_ids"),
            normalized.get("root_ids"),
            normalized.get("camera_ids"),
        )
    ):
        raise StorageOperationContractError("work_signal_scope_empty")
    return normalized


def work_signal_scope_key(scope: dict) -> str:
    normalized = normalize_work_signal_scope(scope)
    return canonical_work_signal_scope_key(normalized)


def canonical_work_signal_scope_key(scope: dict) -> str:
    normalized = canonical_operation_scope(scope)
    if normalized.get("segment_ids"):
        raise StorageOperationContractError("work_signal_segment_scope_forbidden")
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return f"scope:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def cleanup_idle_work_signals(db: Session) -> int:
    keep_ids = [
        row[0]
        for row in db.query(StorageWorkSignal.id)
        .filter(StorageWorkSignal.status == "idle")
        .order_by(StorageWorkSignal.updated_at.desc(), StorageWorkSignal.id.desc())
        .limit(WORK_SIGNAL_IDLE_MAX_ROWS)
        .all()
    ]
    query = db.query(StorageWorkSignal).filter(StorageWorkSignal.status == "idle")
    if keep_ids:
        query = query.filter(~StorageWorkSignal.id.in_(keep_ids))
    deleted = int(query.delete(synchronize_session=False) or 0)
    db.commit()
    return deleted


def publish_work_signal(
    db: Session,
    *,
    signal_type: str,
    scope: dict,
    watermark: int,
) -> dict:
    signal = _code(signal_type, field="signal_type", max_length=64)
    if signal not in WORK_SIGNAL_TYPES:
        raise StorageOperationContractError("work_signal_type_unsupported")
    normalized_scope = normalize_work_signal_scope(scope)
    normalized_key = canonical_work_signal_scope_key(normalized_scope)
    requested = max(0, int(watermark))
    now = _db_now(db)
    row = (
        db.query(StorageWorkSignal)
        .filter(StorageWorkSignal.signal_type == signal, StorageWorkSignal.scope_key == normalized_key)
        .with_for_update()
        .first()
    )
    if row is None:
        db.rollback()
        namespace_lease = acquire_worker_lease(
            db,
            worker_key=f"work-signal-namespace:{signal}",
            owner_instance_id=f"publisher:{uuid.uuid4().hex}",
            lease_seconds=WORK_SIGNAL_NAMESPACE_LEASE_SECONDS,
        )
        if namespace_lease is None:
            raise StorageOperationLeaseLost("storage_work_signal_namespace_busy")
        try:
            row = (
                db.query(StorageWorkSignal)
                .filter(StorageWorkSignal.signal_type == signal, StorageWorkSignal.scope_key == normalized_key)
                .with_for_update()
                .first()
            )
            if row is None:
                cleanup_idle_work_signals(db)
                if db.query(StorageWorkSignal.id).count() >= WORK_SIGNAL_MAX_ROWS:
                    db.rollback()
                    raise StorageOperationContractError("work_signal_row_limit_reached")
                now = _db_now(db)
                row = StorageWorkSignal(
                    signal_type=signal,
                    scope_key=normalized_key,
                    scope=normalized_scope,
                    status="pending" if requested > 0 else "idle",
                    requested_watermark=requested,
                    consumed_watermark=0,
                    fencing_token=0,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.commit()
            else:
                if canonical_operation_scope(row.scope) != normalized_scope:
                    db.rollback()
                    raise StorageOperationContractError("work_signal_scope_mismatch")
                row.requested_watermark = max(int(row.requested_watermark or 0), requested)
                if row.status != "running" and row.requested_watermark > int(row.consumed_watermark or 0):
                    row.status = "pending"
                row.revision = int(row.revision or 0) + 1
                row.updated_at = _db_now(db)
                db.add(row)
                db.commit()
        finally:
            try:
                release_worker_lease(db, namespace_lease)
            except Exception:
                db.rollback()
    else:
        if canonical_operation_scope(row.scope) != normalized_scope:
            db.rollback()
            raise StorageOperationContractError("work_signal_scope_mismatch")
        row.requested_watermark = max(int(row.requested_watermark or 0), requested)
        if row.status != "running" and row.requested_watermark > int(row.consumed_watermark or 0):
            row.status = "pending"
        row.revision = int(row.revision or 0) + 1
        row.updated_at = now
        db.add(row)
        db.commit()
    return public_signal_summary(row)


def advance_work_signal(
    db: Session,
    *,
    signal_type: str,
    scope: dict,
    commit: bool = True,
) -> dict:
    """Atomically advance one coalesced work generation in the caller transaction."""
    signal = _code(signal_type, field="signal_type", max_length=64)
    if signal not in WORK_SIGNAL_TYPES:
        raise StorageOperationContractError("work_signal_type_unsupported")
    normalized_scope = normalize_work_signal_scope(scope)
    normalized_key = canonical_work_signal_scope_key(normalized_scope)
    now = _db_now(db)
    row = (
        db.query(StorageWorkSignal)
        .filter(StorageWorkSignal.signal_type == signal, StorageWorkSignal.scope_key == normalized_key)
        .with_for_update()
        .first()
    )
    if row is None:
        created = False
        try:
            with db.begin_nested():
                row = StorageWorkSignal(
                    signal_type=signal,
                    scope_key=normalized_key,
                    scope=normalized_scope,
                    status="pending",
                    requested_watermark=1,
                    consumed_watermark=0,
                    fencing_token=0,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                created = True
        except IntegrityError:
            row = (
                db.query(StorageWorkSignal)
                .filter(StorageWorkSignal.signal_type == signal, StorageWorkSignal.scope_key == normalized_key)
                .with_for_update()
                .one()
            )
        if not created:
            if canonical_operation_scope(row.scope) != normalized_scope:
                raise StorageOperationContractError("work_signal_scope_mismatch")
            row.requested_watermark = int(row.requested_watermark or 0) + 1
            if row.status != "running":
                row.status = "pending"
            row.revision = int(row.revision or 0) + 1
            row.updated_at = now
            db.add(row)
            db.flush()
    else:
        if canonical_operation_scope(row.scope) != normalized_scope:
            raise StorageOperationContractError("work_signal_scope_mismatch")
        row.requested_watermark = int(row.requested_watermark or 0) + 1
        if row.status != "running":
            row.status = "pending"
        row.revision = int(row.revision or 0) + 1
        row.updated_at = now
        db.add(row)
        db.flush()
    if commit:
        db.commit()
    return public_signal_summary(row)


def claim_work_signal(
    db: Session,
    *,
    signal_type: str,
    scope_key: str,
    owner_instance_id: str,
    lease_seconds: int = SIGNAL_LEASE_SECONDS,
) -> WorkSignalHandle | None:
    signal = _code(signal_type, field="signal_type", max_length=64)
    if signal not in WORK_SIGNAL_TYPES:
        raise StorageOperationContractError("work_signal_type_unsupported")
    normalized_key = _code(scope_key, field="scope_key")
    now = _db_now(db)
    row = (
        db.query(StorageWorkSignal)
        .filter(StorageWorkSignal.signal_type == signal, StorageWorkSignal.scope_key == normalized_key)
        .with_for_update()
        .first()
    )
    if row is None or int(row.requested_watermark or 0) <= int(row.consumed_watermark or 0):
        db.rollback()
        return None
    if row.status == "running" and row.lease_expires_at and row.lease_expires_at > now:
        db.rollback()
        return None
    token = secrets.token_urlsafe(32)
    row.status = "running"
    row.claimed_watermark = int(row.requested_watermark or 0)
    row.owner_token_hash = _token_hash(token)
    row.owner_instance_id = str(owner_instance_id)[:128]
    row.fencing_token = int(row.fencing_token or 0) + 1
    row.revision = int(row.revision or 0) + 1
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(5, int(lease_seconds)))
    row.updated_at = now
    db.add(row)
    db.commit()
    return WorkSignalHandle(signal, normalized_key, token, str(owner_instance_id)[:128], int(row.fencing_token), int(row.claimed_watermark))


def acknowledge_work_signal(db: Session, handle: WorkSignalHandle) -> dict:
    now = _db_now(db)
    row = (
        db.query(StorageWorkSignal)
        .filter(StorageWorkSignal.signal_type == handle.signal_type, StorageWorkSignal.scope_key == handle.scope_key)
        .with_for_update()
        .first()
    )
    if (
        row is None
        or row.status != "running"
        or row.owner_token_hash != _token_hash(handle.owner_token)
        or row.owner_instance_id != handle.owner_instance_id
        or int(row.fencing_token or 0) != int(handle.fencing_token)
        or row.lease_expires_at is None
        or row.lease_expires_at <= now
    ):
        db.rollback()
        raise StorageOperationLeaseLost("storage_work_signal_lease_lost")
    row.consumed_watermark = max(int(row.consumed_watermark or 0), int(handle.claimed_watermark))
    row.status = "pending" if int(row.requested_watermark or 0) > int(row.consumed_watermark or 0) else "idle"
    row.claimed_watermark = None
    row.owner_token_hash = None
    row.owner_instance_id = None
    row.lease_expires_at = None
    row.heartbeat_at = now
    row.revision = int(row.revision or 0) + 1
    row.updated_at = now
    db.add(row)
    db.commit()
    return public_signal_summary(row)


def heartbeat_work_signal(
    db: Session,
    handle: WorkSignalHandle,
    *,
    lease_seconds: int = SIGNAL_LEASE_SECONDS,
) -> dict:
    now = _db_now(db)
    updated = (
        db.query(StorageWorkSignal)
        .filter(
            StorageWorkSignal.signal_type == handle.signal_type,
            StorageWorkSignal.scope_key == handle.scope_key,
            StorageWorkSignal.status == "running",
            StorageWorkSignal.owner_token_hash == _token_hash(handle.owner_token),
            StorageWorkSignal.owner_instance_id == handle.owner_instance_id,
            StorageWorkSignal.fencing_token == handle.fencing_token,
            StorageWorkSignal.claimed_watermark == handle.claimed_watermark,
            StorageWorkSignal.lease_expires_at.isnot(None),
            StorageWorkSignal.lease_expires_at > now,
        )
        .update(
            {
                StorageWorkSignal.heartbeat_at: now,
                StorageWorkSignal.lease_expires_at: now + timedelta(seconds=max(5, int(lease_seconds))),
                StorageWorkSignal.revision: StorageWorkSignal.revision + 1,
                StorageWorkSignal.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise StorageOperationLeaseLost("storage_work_signal_lease_lost")
    db.commit()
    row = (
        db.query(StorageWorkSignal)
        .filter(StorageWorkSignal.signal_type == handle.signal_type, StorageWorkSignal.scope_key == handle.scope_key)
        .one()
    )
    return public_signal_summary(row)


def public_signal_summary(row: StorageWorkSignal) -> dict:
    return {
        "signal_type": row.signal_type,
        "scope_key": row.scope_key,
        "status": row.status if row.status in SIGNAL_STATUSES else "unknown",
        "requested_watermark": int(row.requested_watermark or 0),
        "consumed_watermark": int(row.consumed_watermark or 0),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
