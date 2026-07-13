from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, time as datetime_time, timedelta
from typing import Callable

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.storage_operation import StorageOperation, StorageWorkSignal
from app.models.system_settings import SystemSettings
from app.services.audit_log import create_event
from app.services.recording_retention import (
    EXECUTION_POLICY_AUTOMATIC_BOUNDED,
    OWNERSHIP_KM_VMS,
    RECORDER_SOURCE,
    SEGMENT_STATUS_FINALIZED,
    _active_job_ids,
    execute_segments,
    validate_segment_for_deletion,
)
from app.services.recording_storage import (
    archive_root_physical_volume_id,
    archive_root_runtime_access_state,
    archive_root_runtime_path,
    segment_relative_path,
)
from app.services.storage_operation_conflicts import (
    StorageOperationConflict,
    claim_operation_with_conflicts,
    operation_instance_id,
    reclaim_operation_with_conflicts,
    scope_with_physical_volumes,
    terminal_result_summary,
)
from app.services.storage_operations_foundation import (
    ACTIVE_OPERATION_STATUSES,
    TERMINAL_OPERATION_STATUSES,
    StorageOperationContractError,
    StorageOperationLeaseLost,
    WorkSignalHandle,
    acknowledge_work_signal,
    advance_work_signal,
    canonical_operation_scope,
    claim_work_signal,
    database_now,
    finish_operation,
    heartbeat_operation,
    heartbeat_work_signal,
    operation_effective_status,
    publish_work_signal,
    safe_reason_code,
    work_signal_scope_key,
)
from app.services.system_settings import (
    AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT,
    AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT,
    AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT,
    AUTO_FREE_SPACE_TERMS_VERSION,
    AUTO_FREE_SPACE_WARNING_THRESHOLD_PERCENT,
    get_system_settings,
    get_system_settings_read_only,
)
from app.services.timezone_contract import (
    retention_cutoff_storage,
    storage_naive_utc_to_aware_utc,
    system_local_naive_to_storage_utc,
    timezone_context,
)


RETENTION_SIGNAL_TYPE = "retention_evaluate"
RETENTION_SIGNAL_SCOPE = {
    "global": True,
    "physical_volume_ids": [],
    "root_ids": [],
    "camera_ids": [],
    "segment_ids": [],
}
AUTOMATIC_PAGE_SIZE_DEFAULT = 25
AUTOMATIC_PAGE_SIZE_MAX = 250
STATUS_OPERATION_LIMIT = 20
STATUS_VOLUME_LIMIT = 32
REASON_COUNT_LIMIT = 16
AUTO_FREE_RETRY_SECONDS = 300


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _bounded_counts(values: Counter | dict | None, limit: int = REASON_COUNT_LIMIT) -> dict[str, int]:
    source = Counter(values or {})
    return {
        str(key)[:96]: int(count)
        for key, count in source.most_common(limit)
        if int(count or 0) > 0
    }


def retention_signal_scope_key() -> str:
    return work_signal_scope_key(RETENTION_SIGNAL_SCOPE)


def ensure_retention_signal(db: Session) -> dict:
    return publish_work_signal(
        db,
        signal_type=RETENTION_SIGNAL_TYPE,
        scope=RETENTION_SIGNAL_SCOPE,
        watermark=0,
    )


def advance_retention_signal(db: Session, *, commit: bool = True) -> dict:
    return advance_work_signal(
        db,
        signal_type=RETENTION_SIGNAL_TYPE,
        scope=RETENTION_SIGNAL_SCOPE,
        commit=commit,
    )


def retention_signal_status(db: Session) -> dict:
    row = (
        db.query(StorageWorkSignal)
        .filter(
            StorageWorkSignal.signal_type == RETENTION_SIGNAL_TYPE,
            StorageWorkSignal.scope_key == retention_signal_scope_key(),
        )
        .first()
    )
    if row is None:
        return {
            "status": "missing",
            "requested_watermark": 0,
            "consumed_watermark": 0,
            "pending": False,
            "updated_at": None,
        }
    requested = int(row.requested_watermark or 0)
    consumed = int(row.consumed_watermark or 0)
    return {
        "status": str(row.status or "unknown"),
        "requested_watermark": requested,
        "consumed_watermark": consumed,
        "pending": requested > consumed,
        "updated_at": _iso(row.updated_at),
    }


def claim_retention_signal(db: Session, *, owner_instance_id: str) -> WorkSignalHandle | None:
    return claim_work_signal(
        db,
        signal_type=RETENTION_SIGNAL_TYPE,
        scope_key=retention_signal_scope_key(),
        owner_instance_id=owner_instance_id,
    )


def _segment_due_at(segment_started_at: datetime, retention_days: int, db: Session) -> datetime:
    ctx = timezone_context(db)
    storage_local_date = storage_naive_utc_to_aware_utc(segment_started_at).astimezone(ctx.zone).date()
    compatibility_local_date = segment_started_at.date()
    due_dates = {
        storage_local_date + timedelta(days=int(retention_days) + 1),
        compatibility_local_date + timedelta(days=int(retention_days) + 1),
    }
    return min(
        system_local_naive_to_storage_utc(datetime.combine(value, datetime_time.min), ctx)
        for value in due_dates
    )


def earliest_retention_due_at(db: Session) -> datetime | None:
    rows = (
        db.query(
            Camera.id,
            Camera.retention_days,
            func.min(RecordingSegment.started_at).label("oldest_started_at"),
        )
        .join(RecordingSegment, RecordingSegment.camera_id == Camera.id)
        .filter(
            RecordingSegment.ownership == OWNERSHIP_KM_VMS,
            RecordingSegment.source == RECORDER_SOURCE,
            RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
            RecordingSegment.deleted_at.is_(None),
        )
        .group_by(Camera.id, Camera.retention_days)
        .order_by(Camera.id.asc())
        .all()
    )
    due_values = [
        _segment_due_at(row.oldest_started_at, int(row.retention_days), db)
        for row in rows
        if row.oldest_started_at is not None and int(row.retention_days or 0) > 0
    ]
    return min(due_values) if due_values else None


def publish_due_retention_signal(db: Session, *, now: datetime | None = None) -> dict | None:
    due_at = earliest_retention_due_at(db)
    current = now or database_now(db)
    if due_at is None or due_at > current:
        return None
    due_generation = max(1, int(due_at.timestamp()))
    return publish_work_signal(
        db,
        signal_type=RETENTION_SIGNAL_TYPE,
        scope=RETENTION_SIGNAL_SCOPE,
        watermark=due_generation,
    )


def retention_page_size(configured: int | None = None) -> int:
    value = int(configured or AUTOMATIC_PAGE_SIZE_DEFAULT)
    return max(1, min(value, AUTOMATIC_PAGE_SIZE_MAX))


def _policy_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _max_finalized_segment_id(db: Session) -> int:
    return int(
        db.query(func.max(RecordingSegment.id))
        .filter(
            RecordingSegment.ownership == OWNERSHIP_KM_VMS,
            RecordingSegment.source == RECORDER_SOURCE,
            RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
            RecordingSegment.deleted_at.is_(None),
        )
        .scalar()
        or 0
    )


def _retained_camera_ids(db: Session, *, high_watermark: int) -> list[int]:
    return [
        int(row[0])
        for row in (
            db.query(RecordingSegment.camera_id)
            .filter(
                RecordingSegment.ownership == OWNERSHIP_KM_VMS,
                RecordingSegment.source == RECORDER_SOURCE,
                RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
                RecordingSegment.deleted_at.is_(None),
                RecordingSegment.id <= int(high_watermark),
            )
            .distinct()
            .order_by(RecordingSegment.camera_id.asc())
            .all()
        )
        if row[0] is not None
    ]


def camera_policy_snapshot(
    camera: Camera,
    *,
    signal_watermark: int,
    high_watermark: int,
    evaluation_at: datetime,
) -> dict:
    base = {
        "schema_version": 1,
        "camera_id": int(camera.id),
        "policy_version": int(getattr(camera, "retention_policy_version", 1) or 1),
        "retention_days": int(camera.retention_days or 0),
        "storage_quota_gb": int(camera.storage_quota_gb or 0),
        "signal_watermark": int(signal_watermark),
        "segment_high_watermark": int(high_watermark),
        "evaluation_at": _iso(evaluation_at),
        "camera_state": "soft_deleted" if camera.deleted_at is not None else ("enabled" if camera.enabled else "disabled"),
    }
    return {**base, "policy_hash": _policy_hash(base)}


def _authoritative_camera_query(db: Session, snapshot: dict):
    return db.query(RecordingSegment).filter(
        RecordingSegment.camera_id == int(snapshot["camera_id"]),
        RecordingSegment.ownership == OWNERSHIP_KM_VMS,
        RecordingSegment.source == RECORDER_SOURCE,
        RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
        RecordingSegment.deleted_at.is_(None),
        RecordingSegment.id <= int(snapshot["segment_high_watermark"]),
    )


def measure_camera_retention(db: Session, snapshot: dict) -> dict:
    evaluation_at = datetime.fromisoformat(str(snapshot["evaluation_at"]).replace("Z", "+00:00")).replace(tzinfo=None)
    retention_days = int(snapshot.get("retention_days") or 0)
    quota_gb = int(snapshot.get("storage_quota_gb") or 0)
    quota_bytes = quota_gb * 1024 * 1024 * 1024 if quota_gb > 0 else 0
    aggregate = (
        _authoritative_camera_query(db, snapshot)
        .with_entities(
            func.count(RecordingSegment.id),
            func.coalesce(
                func.sum(
                    case(
                        (RecordingSegment.size_bytes > 0, RecordingSegment.size_bytes),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            or_(RecordingSegment.size_bytes.is_(None), RecordingSegment.size_bytes <= 0),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .one()
    )
    owned_count = int(aggregate[0] or 0)
    owned_bytes = int(aggregate[1] or 0)
    unknown_size_count = int(aggregate[2] or 0)
    age_due_count = 0
    cutoff_storage = None
    cutoff_compat = None
    if retention_days > 0:
        cutoff_storage, cutoff_compat = retention_cutoff_storage(
            retention_days,
            timezone_context(db),
            now_utc=evaluation_at,
        )
        age_due_count = int(
            _authoritative_camera_query(db, snapshot)
            .filter(
                or_(
                    RecordingSegment.started_at < cutoff_storage,
                    RecordingSegment.started_at < cutoff_compat,
                )
            )
            .count()
        )
    quota_overage_bytes = max(0, owned_bytes - quota_bytes) if quota_bytes > 0 else 0
    quota_state = (
        "not_configured"
        if quota_gb <= 0
        else "violating"
        if quota_overage_bytes > 0
        else "unknown"
        if unknown_size_count > 0
        else "compliant"
    )
    missing_rule_count = int(retention_days <= 0) + int(quota_gb <= 0)
    violation = bool(age_due_count > 0 or quota_state in {"violating", "unknown"})
    measurement_confidence = "unknown" if quota_state == "unknown" else "confirmed"
    return {
        "retention_days": retention_days,
        "storage_quota_gb": quota_gb,
        "quota_bytes": quota_bytes,
        "owned_finalized_count": owned_count,
        "owned_finalized_bytes": owned_bytes,
        "age_due_count": age_due_count,
        "quota_overage_bytes": quota_overage_bytes,
        "quota_state": quota_state,
        "unknown_size_count": unknown_size_count,
        "missing_rule_count": missing_rule_count,
        "missing_rules": missing_rule_count > 0,
        "violation": violation,
        "measurement_confidence": measurement_confidence,
        "cutoff_storage": _iso(cutoff_storage),
        "_cutoff_storage": cutoff_storage,
        "_cutoff_compat": cutoff_compat,
    }


def select_camera_retention_candidates(
    db: Session,
    snapshot: dict,
    measurement: dict,
    *,
    limit: int,
    cursor: tuple[datetime, int] | None = None,
) -> dict:
    quota_remaining = int(measurement.get("quota_overage_bytes") or 0)
    cutoff_storage = measurement.get("_cutoff_storage")
    cutoff_compat = measurement.get("_cutoff_compat")
    active_job_ids = _active_job_ids(db)
    selected: list[RecordingSegment] = []
    expected_identities: dict[int, dict] = {}
    blocker_counts: Counter = Counter()
    if measurement.get("quota_state") == "unknown":
        blocker_counts["retention_size_unknown"] += int(measurement.get("unknown_size_count") or 1)
    selected_bytes = 0
    scan_page = max(25, min(AUTOMATIC_PAGE_SIZE_MAX, int(limit) * 4))
    query = _authoritative_camera_query(db, snapshot)
    if cursor is not None:
        cursor_started_at, cursor_id = cursor
        query = query.filter(
            or_(
                RecordingSegment.started_at > cursor_started_at,
                and_(
                    RecordingSegment.started_at == cursor_started_at,
                    RecordingSegment.id > int(cursor_id),
                ),
            )
        )
    rows = query.order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc()).limit(scan_page).all()
    for row in rows:
        due_for_age = bool(
            row.started_at is not None
            and cutoff_storage is not None
            and cutoff_compat is not None
            and (row.started_at < cutoff_storage or row.started_at < cutoff_compat)
        )
        due_for_quota = quota_remaining > 0
        if not due_for_age and not due_for_quota:
            break
        authoritative_size = max(0, int(row.size_bytes or 0))
        if due_for_quota and authoritative_size <= 0 and not due_for_age:
            blocker_counts["retention_size_unknown"] += 1
            continue
        ok, reason, _path, actual_size = validate_segment_for_deletion(row, active_job_ids=active_job_ids)
        if not ok:
            blocker_counts[str(reason or "retention_candidate_blocked")] += 1
            continue
        if len(selected) >= int(limit):
            break
        selected.append(row)
        selected_bytes += int(actual_size or authoritative_size)
        quota_remaining = max(0, quota_remaining - authoritative_size)
        expected_identities[int(row.id)] = {
            "segment_id": int(row.id),
            "camera_id": int(row.camera_id),
            "archive_root_id": str(row.archive_root_id or ""),
            "relative_path": str(segment_relative_path(db, row) or ""),
            "size_bytes": int(row.size_bytes or 0),
        }
    next_cursor = None
    if rows:
        last = rows[-1]
        next_cursor = (last.started_at, int(last.id))
    return {
        "segments": selected,
        "expected_identities": expected_identities,
        "selected_bytes": selected_bytes,
        "blocker_counts": _bounded_counts(blocker_counts),
        "quota_remaining_after_selection": quota_remaining,
        "scan_complete": len(rows) < scan_page,
        "next_cursor": next_cursor,
    }


def _camera_operation_id(camera_id: int, signal_watermark: int) -> str:
    return f"ret-auto-c{int(camera_id)}-w{int(signal_watermark)}"


def _camera_operation_scope(db: Session, snapshot: dict) -> dict:
    root_ids = [
        str(row[0])
        for row in (
            db.query(RecordingSegment.archive_root_id)
            .filter(
                RecordingSegment.camera_id == int(snapshot["camera_id"]),
                RecordingSegment.id <= int(snapshot["segment_high_watermark"]),
                RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
                RecordingSegment.deleted_at.is_(None),
                RecordingSegment.archive_root_id.isnot(None),
            )
            .distinct()
            .order_by(RecordingSegment.archive_root_id.asc())
            .all()
        )
        if row[0]
    ]
    return {
        "global": False,
        "root_ids": root_ids,
        "physical_volume_ids": [],
        "camera_ids": [int(snapshot["camera_id"])],
        "segment_ids": [],
    }


def _claim_camera_operation(db: Session, snapshot: dict) -> dict:
    operation_id = _camera_operation_id(snapshot["camera_id"], snapshot["signal_watermark"])
    existing = db.get(StorageOperation, operation_id)
    if existing is not None and existing.progress:
        stored_snapshot = dict(existing.progress.get("policy_snapshot") or {})
        if stored_snapshot:
            snapshot = stored_snapshot
    request_identity = {"policy_snapshot": snapshot}
    idempotency_key = operation_id
    if existing is not None and operation_effective_status(existing, database_now(db)) == "interrupted":
        return reclaim_operation_with_conflicts(
            db,
            operation_id=operation_id,
            operation_type="retention_auto_run",
            request_identity=request_identity,
            idempotency_key=idempotency_key,
            owner_instance_id=operation_instance_id("retention-auto-recovery"),
        )
    return claim_operation_with_conflicts(
        db,
        operation_type="retention_auto_run",
        scope=_camera_operation_scope(db, snapshot),
        request_identity=request_identity,
        system_owner="automatic-retention",
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        owner_instance_id=operation_instance_id("retention-auto"),
        initial_progress={
            "policy_snapshot": snapshot,
            "planned_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "completed_bytes": 0,
        },
    )


def _operation_progress(snapshot: dict, totals: dict, measurement: dict) -> dict:
    return {
        "policy_snapshot": snapshot,
        "planned_count": int(totals.get("planned_count") or 0),
        "completed_count": int(totals.get("deleted_count") or 0),
        "failed_count": int(totals.get("failed_count") or 0),
        "skipped_count": int(totals.get("skipped_count") or 0),
        "completed_bytes": int(totals.get("bytes_freed") or 0),
        "remaining_count": int(measurement.get("age_due_count") or 0),
        "remaining_bytes": int(measurement.get("quota_overage_bytes") or 0),
        "measurement_confidence": str(measurement.get("measurement_confidence") or "unknown"),
        "reason_counts": _bounded_counts(totals.get("reason_counts") or {}),
    }


def _camera_terminal_result(snapshot: dict, totals: dict, measurement: dict, *, status: str) -> dict:
    return {
        "status": status,
        "camera_id": int(snapshot["camera_id"]),
        "policy_version": int(snapshot["policy_version"]),
        "policy_hash": str(snapshot["policy_hash"]),
        "signal_watermark": int(snapshot["signal_watermark"]),
        "segment_high_watermark": int(snapshot["segment_high_watermark"]),
        "owned_finalized_count": int(measurement.get("owned_finalized_count") or 0),
        "owned_finalized_bytes": int(measurement.get("owned_finalized_bytes") or 0),
        "age_due_count": int(measurement.get("age_due_count") or 0),
        "quota_overage_bytes": int(measurement.get("quota_overage_bytes") or 0),
        "quota_state": str(measurement.get("quota_state") or "unknown"),
        "unknown_size_count": int(measurement.get("unknown_size_count") or 0),
        "missing_rule_count": int(measurement.get("missing_rule_count") or 0),
        "measurement_confidence": str(measurement.get("measurement_confidence") or "unknown"),
        "planned_count": int(totals.get("planned_count") or 0),
        "deleted_count": int(totals.get("deleted_count") or 0),
        "skipped_count": int(totals.get("skipped_count") or 0),
        "failed_count": int(totals.get("failed_count") or 0),
        "bytes_freed": int(totals.get("bytes_freed") or 0),
        "reason_counts": _bounded_counts(totals.get("reason_counts") or {}),
    }


def run_camera_retention_operation(
    db: Session,
    snapshot: dict,
    *,
    page_size: int,
    should_preempt: Callable[[], bool] | None = None,
) -> dict:
    try:
        claim = _claim_camera_operation(db, snapshot)
    except StorageOperationConflict as exc:
        db.rollback()
        return {
            "status": "blocked",
            "camera_id": int(snapshot["camera_id"]),
            "reason_code": safe_reason_code(exc.detail, fallback="storage_operation_conflict"),
            "retryable": True,
        }
    if claim.get("state") == "terminal":
        result = terminal_result_summary(claim.get("terminal_result"))
        return {**result, "replayed": True}
    if claim.get("state") != "claimed":
        return {
            "status": str((claim.get("operation") or {}).get("status") or claim.get("state") or "blocked"),
            "camera_id": int(snapshot["camera_id"]),
            "reason_code": "retention_operation_not_claimed",
            "retryable": True,
        }
    handle = claim["handle"]
    stored = db.get(StorageOperation, handle.operation_id)
    stored_progress = dict(stored.progress or {})
    snapshot = dict(stored_progress.get("policy_snapshot") or snapshot)
    totals = {
        "planned_count": int(stored_progress.get("planned_count") or 0),
        "deleted_count": int(stored_progress.get("completed_count") or 0),
        "skipped_count": int(stored_progress.get("skipped_count") or 0),
        "failed_count": int(stored_progress.get("failed_count") or 0),
        "bytes_freed": int(stored_progress.get("completed_bytes") or 0),
        "reason_counts": Counter(stored_progress.get("reason_counts") or {}),
    }
    scan_cursor: tuple[datetime, int] | None = None
    try:
        while True:
            measurement = measure_camera_retention(db, snapshot)
            if not measurement["violation"]:
                result = _camera_terminal_result(snapshot, totals, measurement, status="compliant")
                finish_operation(
                    db,
                    handle,
                    status="completed",
                    result=result,
                    progress=_operation_progress(snapshot, totals, measurement),
                )
                return result
            selected = select_camera_retention_candidates(
                db,
                snapshot,
                measurement,
                limit=page_size,
                cursor=scan_cursor,
            )
            totals["reason_counts"].update(selected["blocker_counts"])
            if not selected["segments"]:
                if not selected["scan_complete"] and selected["next_cursor"] is not None:
                    scan_cursor = selected["next_cursor"]
                    heartbeat_operation(db, handle, progress=_operation_progress(snapshot, totals, measurement))
                    continue
                terminal = "partial" if totals["deleted_count"] else "blocked"
                result = _camera_terminal_result(snapshot, totals, measurement, status="no_safe_candidate")
                finish_operation(
                    db,
                    handle,
                    status=terminal,
                    result=result,
                    progress=_operation_progress(snapshot, totals, measurement),
                    reason_code="retention_no_safe_candidates",
                    next_action="review_storage_problems",
                    retry_allowed=True,
                    retry_mode="scheduled",
                )
                return result
            scan_cursor = None
            execution = execute_segments(
                db,
                selected["segments"],
                actor=None,
                operation="retention_auto_run",
                reason="automatic_retention_policy",
                max_candidates=page_size,
                policy=EXECUTION_POLICY_AUTOMATIC_BOUNDED,
                operation_id=handle.operation_id,
                scope={
                    "type": "camera",
                    "camera_ids": [int(snapshot["camera_id"])],
                    "root_ids": sorted({str(row.archive_root_id) for row in selected["segments"] if row.archive_root_id}),
                    "segment_ids": [int(row.id) for row in selected["segments"]],
                },
                expected_identities=selected["expected_identities"],
                outer_operation_handle=handle,
                manage_outer_operation=False,
                write_terminal_audit=False,
            )
            totals["planned_count"] += int(execution.get("planned_count") or 0)
            totals["deleted_count"] += int(execution.get("deleted_count") or 0)
            totals["skipped_count"] += int(execution.get("skipped_count") or 0)
            totals["failed_count"] += int(execution.get("failed_count") or 0)
            totals["bytes_freed"] += int(execution.get("bytes_freed") or 0)
            totals["reason_counts"].update(execution.get("reason_counts") or {})
            measurement = measure_camera_retention(db, snapshot)
            heartbeat_operation(db, handle, progress=_operation_progress(snapshot, totals, measurement))
            if int(execution.get("deleted_count") or 0) <= 0:
                terminal = "partial" if totals["deleted_count"] else "blocked"
                result = _camera_terminal_result(snapshot, totals, measurement, status="no_progress")
                finish_operation(
                    db,
                    handle,
                    status=terminal,
                    result=result,
                    progress=_operation_progress(snapshot, totals, measurement),
                    reason_code="retention_no_progress",
                    next_action="review_storage_problems",
                    retry_allowed=True,
                    retry_mode="scheduled",
                )
                return result
            if should_preempt is not None and should_preempt():
                result = _camera_terminal_result(snapshot, totals, measurement, status="preempted_by_storage_pressure")
                finish_operation(
                    db,
                    handle,
                    status="partial",
                    result=result,
                    progress=_operation_progress(snapshot, totals, measurement),
                    reason_code="retention_preempted_by_storage_pressure",
                    next_action="resume_after_storage_pressure",
                    retry_allowed=True,
                    retry_mode="immediate",
                )
                advance_retention_signal(db)
                return result
    except Exception as exc:
        db.rollback()
        try:
            finish_operation(
                db,
                handle,
                status="failed",
                result={
                    "status": "failed",
                    "camera_id": int(snapshot["camera_id"]),
                    "policy_version": int(snapshot["policy_version"]),
                },
                progress=_operation_progress(snapshot, totals, measure_camera_retention(db, snapshot)),
                reason_code="automatic_retention_failed",
                next_action="retry_operation",
                retry_allowed=True,
                retry_mode="scheduled",
            )
        except Exception:
            db.rollback()
        raise


def run_retention_signal_generation(
    db: Session,
    handle: WorkSignalHandle,
    *,
    page_size: int,
    should_preempt: Callable[[], bool] | None = None,
) -> dict:
    def pressure_preemption_requested() -> bool:
        heartbeat_work_signal(db, handle)
        if should_preempt is not None:
            return bool(should_preempt())
        return retention_slice_preemption_required(db)

    evaluation_at = database_now(db)
    high_watermark = _max_finalized_segment_id(db)
    camera_ids = _retained_camera_ids(db, high_watermark=high_watermark)
    status_counts: Counter = Counter()
    deleted_count = 0
    bytes_freed = 0
    processed = 0
    preempted = False
    for camera_id in camera_ids:
        camera = db.get(Camera, camera_id)
        if camera is None:
            status_counts["camera_missing"] += 1
            continue
        snapshot = camera_policy_snapshot(
            camera,
            signal_watermark=handle.claimed_watermark,
            high_watermark=high_watermark,
            evaluation_at=evaluation_at,
        )
        if snapshot["retention_days"] <= 0 and snapshot["storage_quota_gb"] <= 0:
            status_counts["rules_missing"] += 1
            continue
        result = run_camera_retention_operation(
            db,
            snapshot,
            page_size=page_size,
            should_preempt=pressure_preemption_requested,
        )
        processed += 1
        status_counts[str(result.get("status") or "unknown")] += 1
        deleted_count += int(result.get("deleted_count") or 0)
        bytes_freed += int(result.get("bytes_freed") or 0)
        if result.get("retryable"):
            advance_retention_signal(db)
        heartbeat_work_signal(db, handle)
        if pressure_preemption_requested():
            preempted = True
            advance_retention_signal(db)
            break
    signal = acknowledge_work_signal(db, handle)
    return {
        "status": "preempted" if preempted else "completed",
        "claimed_watermark": int(handle.claimed_watermark),
        "segment_high_watermark": high_watermark,
        "camera_count": len(camera_ids),
        "processed_camera_count": processed,
        "deleted_count": deleted_count,
        "bytes_freed": bytes_freed,
        "status_counts": _bounded_counts(status_counts),
        "signal": signal,
    }


def auto_free_acknowledgement_state(system: SystemSettings) -> dict:
    acknowledged_version = str(getattr(system, "auto_free_space_acknowledged_terms_version", "") or "")
    configured_enabled = bool(getattr(system, "auto_free_space_cleanup_enabled", False))
    acknowledged = acknowledged_version == AUTO_FREE_SPACE_TERMS_VERSION
    return {
        "configured_enabled": configured_enabled,
        "effective_enabled": bool(configured_enabled and acknowledged),
        "acknowledgement_required": not acknowledged,
        "terms_version": AUTO_FREE_SPACE_TERMS_VERSION,
        "acknowledged_terms_version": acknowledged_version or None,
        "acknowledged_at": _iso(getattr(system, "auto_free_space_acknowledged_at", None)),
    }


def _canonical_volume_id(db: Session, physical_identity: str) -> str:
    scope = scope_with_physical_volumes(
        db,
        {
            "global": False,
            "physical_volume_ids": [str(physical_identity)],
            "root_ids": [],
            "camera_ids": [],
            "segment_ids": [],
        },
    )
    values = list(scope.get("physical_volume_ids") or [])
    if len(values) != 1:
        raise StorageOperationContractError("physical_volume_identity_unknown")
    return str(values[0])


def _safe_canonical_volume_id(db: Session, physical_identity: str | None) -> str | None:
    if not physical_identity:
        return None
    try:
        return _canonical_volume_id(db, str(physical_identity))
    except StorageOperationContractError:
        return None


def storage_volume_groups(db: Session) -> list[dict]:
    roots = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.retired_at.is_(None))
        .order_by(ArchiveRoot.is_active.desc(), ArchiveRoot.created_at.asc(), ArchiveRoot.id.asc())
        .all()
    )
    grouped: dict[str, dict] = {}
    unknown_roots: list[ArchiveRoot] = []
    for root in roots:
        physical_identity = str(root.physical_identity or "").strip()
        if not physical_identity:
            unknown_roots.append(root)
            continue
        group = grouped.setdefault(
            physical_identity,
            {
                "_physical_identity": physical_identity,
                "physical_volume_id": _safe_canonical_volume_id(db, physical_identity),
                "display_label": archive_root_physical_volume_id(root).lstrip("/") or "Storage",
                "root_ids": [],
                "root_count": 0,
                "scope_bounded": True,
                "active_write_target": False,
                "capacity": {
                    "total_bytes": None,
                    "used_bytes": None,
                    "free_bytes": None,
                    "free_percent": None,
                    "filesystem_probe_status": "unknown",
                },
                "root_access_problem_count": 0,
            },
        )
        group["root_count"] += 1
        if len(group["root_ids"]) < 64:
            group["root_ids"].append(str(root.id))
        else:
            group["scope_bounded"] = False
        group["active_write_target"] = bool(group["active_write_target"] or root.is_active)
        access = archive_root_runtime_access_state(root)
        if access.get("read_access_state") != "available":
            group["root_access_problem_count"] += 1
            continue
        if group["capacity"]["filesystem_probe_status"] == "ok":
            continue
        try:
            usage = shutil.disk_usage(archive_root_runtime_path(root))
            free_percent = round((int(usage.free) / int(usage.total)) * 100, 4) if usage.total else None
            group["capacity"] = {
                "total_bytes": int(usage.total),
                "used_bytes": int(usage.used),
                "free_bytes": int(usage.free),
                "free_percent": free_percent,
                "filesystem_probe_status": "ok",
            }
        except OSError:
            group["capacity"]["filesystem_probe_status"] = "error"
    result = list(grouped.values())
    if unknown_roots:
        result.append(
            {
                "_physical_identity": None,
                "physical_volume_id": None,
                "display_label": "Storage",
                "root_ids": [str(root.id) for root in unknown_roots[:64]],
                "root_count": len(unknown_roots),
                "scope_bounded": len(unknown_roots) <= 64,
                "active_write_target": any(bool(root.is_active) for root in unknown_roots),
                "capacity": {
                    "total_bytes": None,
                    "used_bytes": None,
                    "free_bytes": None,
                    "free_percent": None,
                    "filesystem_probe_status": "identity_unknown",
                },
                "root_access_problem_count": len(unknown_roots),
            }
        )
    return sorted(
        result,
        key=lambda item: (not bool(item.get("active_write_target")), str(item.get("physical_volume_id") or "~")),
    )


def _volume_state(free_percent: float | None) -> str:
    if free_percent is None:
        return "capacity_unknown"
    if free_percent < AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT:
        return "critical"
    if free_percent < AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT:
        return "cleanup_threshold"
    if free_percent < AUTO_FREE_SPACE_WARNING_THRESHOLD_PERCENT:
        return "warning"
    return "ok"


def low_disk_policy_status(db: Session, groups: list[dict] | None = None) -> dict:
    system = get_system_settings_read_only(db)
    acknowledgement = auto_free_acknowledgement_state(system)
    volume_groups = groups if groups is not None else storage_volume_groups(db)
    public_groups = []
    for group in volume_groups[:STATUS_VOLUME_LIMIT]:
        capacity = dict(group.get("capacity") or {})
        free_percent = capacity.get("free_percent")
        public_groups.append(
            {
                "physical_volume_id": group.get("physical_volume_id"),
                "display_label": group.get("display_label"),
                "root_count": int(group.get("root_count") or len(group.get("root_ids") or [])),
                "active_write_target": bool(group.get("active_write_target")),
                "state": (
                    _volume_state(free_percent)
                    if group.get("physical_volume_id") and group.get("scope_bounded", True)
                    else "identity_unknown"
                ),
                "free_percent": free_percent,
                "free_bytes": capacity.get("free_bytes"),
                "total_bytes": capacity.get("total_bytes"),
                "filesystem_probe_status": capacity.get("filesystem_probe_status"),
                "cleanup_allowed": bool(
                    acknowledgement["effective_enabled"]
                    and bool(group.get("physical_volume_id"))
                    and bool(group.get("scope_bounded", True))
                    and free_percent is not None
                    and free_percent < AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT
                ),
            }
        )
    active = next((item for item in public_groups if item["active_write_target"]), None)
    return {
        "state": (active or {}).get("state") or "capacity_unknown",
        "policy_state": (
            "ON"
            if acknowledgement["effective_enabled"]
            else "CONFIRMATION_REQUIRED"
            if acknowledgement["configured_enabled"] and acknowledgement["acknowledgement_required"]
            else "OFF"
        ),
        "auto_free_space_cleanup_enabled": acknowledgement["configured_enabled"],
        "auto_free_space_cleanup_effective": acknowledgement["effective_enabled"],
        "acknowledgement_required": acknowledgement["acknowledgement_required"],
        "terms_version": acknowledgement["terms_version"],
        "acknowledged_terms_version": acknowledgement["acknowledged_terms_version"],
        "warning_threshold_percent": AUTO_FREE_SPACE_WARNING_THRESHOLD_PERCENT,
        "cleanup_threshold_percent": AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT,
        "recovery_threshold_percent": AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT,
        "critical_threshold_percent": AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT,
        "cleanup_allowed": bool((active or {}).get("cleanup_allowed")),
        "critical_recording_suspend_required": bool((active or {}).get("state") == "critical"),
        "recording_suspended_by_low_disk": bool(getattr(system, "recording_suspended_by_low_disk", False)),
        "suspended_physical_volume_id": (
            _safe_canonical_volume_id(db, system.low_disk_suspended_physical_volume_id)
            if getattr(system, "low_disk_suspended_physical_volume_id", None)
            else None
        ),
        "suspended_at": _iso(getattr(system, "low_disk_suspended_at", None)),
        "free_percent": (active or {}).get("free_percent"),
        "free_bytes": (active or {}).get("free_bytes"),
        "total_bytes": (active or {}).get("total_bytes"),
        "volume_groups": public_groups,
    }


def _critical_audit(
    db: Session,
    *,
    suspended: bool,
    volume_id: str | None,
    free_percent: float | None,
    reason_code: str,
) -> None:
    create_event(
        db=db,
        actor=None,
        category="storage",
        event_type=(
            "storage.critical_low_disk_recording_suspended"
            if suspended
            else "storage.critical_low_disk_recording_resumed"
        ),
        severity="error" if suspended else "info",
        message_ru=(
            "Запись приостановлена из-за критически малого свободного места"
            if suspended
            else "Запись возобновлена после восстановления свободного места"
        ),
        message_en=(
            "Recording suspended by critical low disk protection"
            if suspended
            else "Recording resumed after low disk recovery"
        ),
        target_type="storage",
        metadata={
            "reason_code": reason_code,
            "physical_volume_id": volume_id,
            "free_percent": free_percent,
            "critical_threshold_percent": AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT,
            "resume_threshold_percent": AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT,
        },
    )


def apply_critical_recording_protection(db: Session, groups: list[dict] | None = None) -> dict:
    volume_groups = groups if groups is not None else storage_volume_groups(db)
    system = get_system_settings(db)
    active = next((item for item in volume_groups if item.get("active_write_target")), None)
    active_free = (active or {}).get("capacity", {}).get("free_percent")
    active_identity = (active or {}).get("_physical_identity")
    currently_suspended = bool(getattr(system, "recording_suspended_by_low_disk", False))
    stored_identity = str(getattr(system, "low_disk_suspended_physical_volume_id", "") or "") or None
    suspended_free = None
    changed = False
    transition_reason = "critical_low_disk"
    if not currently_suspended and active_free is not None and active_free < AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT:
        system.recording_suspended_by_low_disk = True
        system.low_disk_suspended_physical_volume_id = active_identity
        system.low_disk_suspended_at = database_now(db)
        currently_suspended = True
        stored_identity = active_identity
        suspended_free = active_free
        changed = True
    elif currently_suspended:
        suspended_group = next(
            (item for item in volume_groups if item.get("_physical_identity") == stored_identity),
            None,
        )
        suspended_free = (suspended_group or {}).get("capacity", {}).get("free_percent")
        if suspended_free is not None and suspended_free >= AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT:
            system.recording_suspended_by_low_disk = False
            system.low_disk_suspended_physical_volume_id = None
            system.low_disk_suspended_at = None
            currently_suspended = False
            changed = True
            transition_reason = "free_space_recovered"
            active_free = suspended_free
    if changed:
        system.updated_at = database_now(db)
        db.add(system)
        db.commit()
        _critical_audit(
            db,
            suspended=currently_suspended,
            volume_id=(
                _safe_canonical_volume_id(db, stored_identity)
                if stored_identity
                else (active or {}).get("physical_volume_id")
            ),
            free_percent=active_free,
            reason_code=transition_reason,
        )
    return {
        "recording_suspended_by_low_disk": currently_suspended,
        "changed": changed,
        "suspended_physical_volume_id": (
            _safe_canonical_volume_id(db, stored_identity) if currently_suspended and stored_identity else None
        ),
        "active_free_percent": active_free,
        "suspended_free_percent": suspended_free if currently_suspended else None,
        "resume_threshold_percent": AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT,
    }


def _probe_group_by_roots(
    db: Session,
    root_ids: list[str],
    *,
    expected_physical_volume_id: str | None = None,
    expected_runtime_device_id: str | None = None,
) -> dict:
    expected = sorted({str(value) for value in root_ids if value})
    rows = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.id.in_(expected), ArchiveRoot.retired_at.is_(None))
        .order_by(ArchiveRoot.id.asc())
        .all()
    )
    if len(rows) != len(expected):
        return {"status": "root_set_changed", "free_percent": None, "root_ids": expected}
    identities = {str(row.physical_identity or "").strip() for row in rows}
    if len(identities) != 1 or not next(iter(identities), ""):
        return {"status": "physical_volume_identity_unknown", "free_percent": None, "root_ids": expected}
    identity = next(iter(identities))
    canonical_identity = _canonical_volume_id(db, identity)
    if expected_physical_volume_id and canonical_identity != str(expected_physical_volume_id):
        return {
            "status": "physical_volume_identity_changed",
            "free_percent": None,
            "root_ids": expected,
        }
    runtime_devices: set[str] = set()
    measured_usage = None
    for row in rows:
        access = archive_root_runtime_access_state(row)
        if access.get("read_access_state") != "available":
            continue
        try:
            runtime_device = _runtime_device_id(row)
            usage = shutil.disk_usage(archive_root_runtime_path(row))
            runtime_devices.add(runtime_device)
            measured_usage = usage
        except OSError:
            continue
    if not runtime_devices or measured_usage is None:
        return {
            "status": "capacity_unknown",
            "physical_identity": identity,
            "physical_volume_id": canonical_identity,
            "root_ids": expected,
            "free_percent": None,
        }
    if len(runtime_devices) != 1 or (
        expected_runtime_device_id and next(iter(runtime_devices)) != str(expected_runtime_device_id)
    ):
        return {
            "status": "physical_volume_runtime_identity_changed",
            "physical_volume_id": canonical_identity,
            "root_ids": expected,
            "free_percent": None,
        }
    return {
        "status": "ok",
        "physical_identity": identity,
        "physical_volume_id": canonical_identity,
        "root_ids": expected,
        "total_bytes": int(measured_usage.total),
        "free_bytes": int(measured_usage.free),
        "free_percent": (
            round((int(measured_usage.free) / int(measured_usage.total)) * 100, 4)
            if measured_usage.total
            else None
        ),
    }


def _max_group_segment_id(db: Session, root_ids: list[str]) -> int:
    return int(
        db.query(func.max(RecordingSegment.id))
        .filter(
            RecordingSegment.archive_root_id.in_(root_ids),
            RecordingSegment.ownership == OWNERSHIP_KM_VMS,
            RecordingSegment.source == RECORDER_SOURCE,
            RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
            RecordingSegment.deleted_at.is_(None),
        )
        .scalar()
        or 0
    )


def _runtime_device_id(root: ArchiveRoot) -> str:
    device = int(archive_root_runtime_path(root).stat().st_dev)
    return f"rv1:{hashlib.sha256(str(device).encode('ascii')).hexdigest()[:32]}"


def _revalidate_pressure_group(db: Session, root_ids: list[str], expected_identity: str) -> dict:
    from app.services.setup_storage import revalidate_configured_archive_root

    rows = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.id.in_(root_ids), ArchiveRoot.retired_at.is_(None))
        .order_by(ArchiveRoot.id.asc())
        .all()
    )
    if len(rows) != len(root_ids):
        raise StorageOperationContractError("auto_free_root_set_changed")
    runtime_devices: set[str] = set()
    for root in rows:
        if str(root.physical_identity or "") != str(expected_identity):
            raise StorageOperationContractError("physical_volume_identity_unknown")
        evidence = revalidate_configured_archive_root(root)
        if str(evidence.get("physical_identity") or "") != str(expected_identity):
            raise StorageOperationContractError("physical_volume_identity_changed")
        try:
            runtime_devices.add(_runtime_device_id(root))
        except OSError as exc:
            raise StorageOperationContractError("archive_root_runtime_unavailable") from exc
    if len(runtime_devices) != 1:
        raise StorageOperationContractError("physical_volume_runtime_identity_ambiguous")
    return {
        "physical_identity": expected_identity,
        "runtime_device_id": next(iter(runtime_devices)),
    }


def _auto_free_operation_id(group: dict, max_segment_id: int, attempt_at: datetime) -> str:
    raw = f"{group.get('physical_volume_id')}:{int(max_segment_id)}:{attempt_at.isoformat()}"
    return f"auto-free-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _latest_volume_operation(db: Session, canonical_volume: str) -> StorageOperation | None:
    rows = (
        db.query(StorageOperation)
        .filter(StorageOperation.operation_type == "retention_auto_free_space")
        .order_by(StorageOperation.updated_at.desc(), StorageOperation.id.desc())
        .limit(64)
        .all()
    )
    return next(
        (
            row
            for row in rows
            if canonical_volume in set(canonical_operation_scope(row.scope).get("physical_volume_ids") or [])
        ),
        None,
    )


def _auto_free_retry_suppressed(
    db: Session,
    *,
    canonical_volume: str,
    max_segment_id: int,
    current_free_percent: float | None,
) -> bool:
    latest = _latest_volume_operation(db, canonical_volume)
    if latest is None or latest.status not in {"blocked", "partial", "failed"} or latest.finished_at is None:
        return False
    age_seconds = max(0.0, (database_now(db) - latest.finished_at).total_seconds())
    if age_seconds >= AUTO_FREE_RETRY_SECONDS:
        return False
    snapshot = dict((latest.progress or {}).get("pressure_snapshot") or {})
    if int(snapshot.get("max_segment_id") or 0) != int(max_segment_id):
        return False
    last_free = (latest.result or {}).get("final_free_percent")
    if current_free_percent is None or last_free is None:
        return True
    return abs(float(current_free_percent) - float(last_free)) < 0.05


def _claim_auto_free_operation(db: Session, group: dict) -> dict:
    root_ids = sorted({str(value) for value in group.get("root_ids") or [] if value})
    max_segment_id = _max_group_segment_id(db, root_ids)
    canonical_volume = str(group.get("physical_volume_id") or "")
    if not canonical_volume or not bool(group.get("scope_bounded", True)):
        return {"state": "blocked", "reason_code": "physical_volume_identity_unknown"}
    active_rows = (
        db.query(StorageOperation)
        .filter(
            StorageOperation.operation_type == "retention_auto_free_space",
            StorageOperation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
        )
        .order_by(StorageOperation.created_at.desc(), StorageOperation.id.desc())
        .limit(32)
        .all()
    )
    existing = next(
        (
            row
            for row in active_rows
            if canonical_volume in set(canonical_operation_scope(row.scope).get("physical_volume_ids") or [])
        ),
        None,
    )
    if existing is not None and operation_effective_status(existing, database_now(db)) == "interrupted":
        snapshot = dict((existing.progress or {}).get("pressure_snapshot") or {})
        reclaimed = reclaim_operation_with_conflicts(
            db,
            operation_id=str(existing.id),
            operation_type="retention_auto_free_space",
            request_identity={"pressure_snapshot": snapshot},
            idempotency_key=str(existing.id),
            owner_instance_id=operation_instance_id("auto-free-recovery"),
        )
        if reclaimed.get("state") == "claimed":
            try:
                revalidated = _revalidate_pressure_group(
                    db,
                    list(snapshot.get("root_ids") or []),
                    str(group.get("_physical_identity") or ""),
                )
                if revalidated.get("runtime_device_id") != snapshot.get("runtime_device_id"):
                    raise StorageOperationContractError("physical_volume_runtime_identity_changed")
            except StorageOperationContractError as exc:
                db.rollback()
                reason = safe_reason_code(str(exc), fallback="physical_volume_identity_unknown")
                result = {
                    "status": "blocked",
                    "physical_volume_id": canonical_volume,
                    "deleted_count": int((existing.progress or {}).get("completed_count") or 0),
                    "bytes_freed": int((existing.progress or {}).get("completed_bytes") or 0),
                    "reason_counts": {reason: 1},
                }
                finish_operation(
                    db,
                    reclaimed["handle"],
                    status="partial" if result["deleted_count"] else "blocked",
                    result=result,
                    progress=dict(existing.progress or {}),
                    reason_code=reason,
                    next_action="check_storage_access",
                    retry_allowed=True,
                    retry_mode="scheduled",
                )
                return {"state": "terminal", "terminal_result": result}
        return reclaimed
    current_free_percent = group.get("capacity", {}).get("free_percent")
    if _auto_free_retry_suppressed(
        db,
        canonical_volume=canonical_volume,
        max_segment_id=max_segment_id,
        current_free_percent=current_free_percent,
    ):
        return {"state": "retry_suppressed", "reason_code": "auto_free_retry_scheduled"}
    revalidation_reason = None
    try:
        revalidated = _revalidate_pressure_group(
            db,
            root_ids,
            str(group.get("_physical_identity") or ""),
        )
    except StorageOperationContractError as exc:
        db.rollback()
        revalidation_reason = safe_reason_code(str(exc), fallback="physical_volume_identity_unknown")
        revalidated = {"runtime_device_id": None}
    attempt_at = database_now(db)
    operation_id = _auto_free_operation_id(group, max_segment_id, attempt_at)
    snapshot = {
        "schema_version": 1,
        "physical_volume_id": canonical_volume,
        "runtime_device_id": revalidated["runtime_device_id"],
        "revalidation_reason": revalidation_reason,
        "root_ids": root_ids,
        "max_segment_id": max_segment_id,
        "initial_free_percent": group.get("capacity", {}).get("free_percent"),
        "started_at": _iso(attempt_at),
        "trigger_percent": AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT,
        "target_percent": AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT,
    }
    claimed = claim_operation_with_conflicts(
        db,
        operation_type="retention_auto_free_space",
        scope={
            "global": False,
            "physical_volume_ids": [group.get("_physical_identity")],
            "root_ids": root_ids,
            "camera_ids": [],
            "segment_ids": [],
        },
        request_identity={"pressure_snapshot": snapshot},
        system_owner="auto-free-space",
        operation_id=operation_id,
        idempotency_key=operation_id,
        owner_instance_id=operation_instance_id("auto-free"),
        initial_progress={
            "pressure_snapshot": snapshot,
            "planned_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "completed_bytes": 0,
        },
    )
    if revalidation_reason and claimed.get("state") == "claimed":
        result = {
            "status": "blocked",
            "physical_volume_id": canonical_volume,
            "deleted_count": 0,
            "bytes_freed": 0,
            "reason_counts": {revalidation_reason: 1},
        }
        finish_operation(
            db,
            claimed["handle"],
            status="blocked",
            result=result,
            progress={
                "pressure_snapshot": snapshot,
                "planned_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "completed_bytes": 0,
            },
            reason_code=revalidation_reason,
            next_action="check_storage_access",
            retry_allowed=True,
            retry_mode="scheduled",
        )
        return {"state": "terminal", "terminal_result": result}
    return claimed


def _select_group_candidates(
    db: Session,
    root_ids: list[str],
    *,
    limit: int,
    cursor: tuple[datetime, int] | None = None,
) -> dict:
    selected: list[RecordingSegment] = []
    expected: dict[int, dict] = {}
    blocker_counts: Counter = Counter()
    active_job_ids = _active_job_ids(db)
    scan_page = max(25, min(AUTOMATIC_PAGE_SIZE_MAX, int(limit) * 4))
    query = db.query(RecordingSegment).filter(
        RecordingSegment.archive_root_id.in_(root_ids),
        RecordingSegment.ownership == OWNERSHIP_KM_VMS,
        RecordingSegment.source == RECORDER_SOURCE,
        RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
        RecordingSegment.deleted_at.is_(None),
    )
    if cursor is not None:
        cursor_started_at, cursor_id = cursor
        query = query.filter(
            or_(
                RecordingSegment.started_at > cursor_started_at,
                and_(
                    RecordingSegment.started_at == cursor_started_at,
                    RecordingSegment.id > int(cursor_id),
                ),
            )
        )
    rows = query.order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc()).limit(scan_page).all()
    for row in rows:
        ok, reason, _path, _size = validate_segment_for_deletion(row, active_job_ids=active_job_ids)
        if not ok:
            blocker_counts[str(reason or "auto_free_candidate_blocked")] += 1
            continue
        selected.append(row)
        expected[int(row.id)] = {
            "segment_id": int(row.id),
            "camera_id": int(row.camera_id),
            "archive_root_id": str(row.archive_root_id or ""),
            "relative_path": str(segment_relative_path(db, row) or ""),
            "size_bytes": int(row.size_bytes or 0),
        }
        if len(selected) >= int(limit):
            break
    next_cursor = None
    if rows:
        last = rows[-1]
        next_cursor = (last.started_at, int(last.id))
    return {
        "segments": selected[: int(limit)],
        "expected_identities": {int(row.id): expected[int(row.id)] for row in selected[: int(limit)]},
        "blocker_counts": _bounded_counts(blocker_counts),
        "scan_complete": len(rows) < scan_page,
        "next_cursor": next_cursor,
    }


def _auto_free_progress(context: dict, probe: dict) -> dict:
    totals = context["totals"]
    scan_cursor = context.get("scan_cursor")
    return {
        "pressure_snapshot": context["snapshot"],
        "planned_count": int(totals.get("planned_count") or 0),
        "completed_count": int(totals.get("deleted_count") or 0),
        "failed_count": int(totals.get("failed_count") or 0),
        "skipped_count": int(totals.get("skipped_count") or 0),
        "completed_bytes": int(totals.get("bytes_freed") or 0),
        "current_free_percent": probe.get("free_percent"),
        "reason_counts": _bounded_counts(totals.get("reason_counts") or {}),
        "scan_cursor_started_at": _iso(scan_cursor[0]) if scan_cursor else None,
        "scan_cursor_id": int(scan_cursor[1]) if scan_cursor else None,
    }


def _finish_auto_free_context(
    db: Session,
    context: dict,
    probe: dict,
    *,
    operation_status: str,
    result_status: str,
    reason_code: str | None = None,
    next_action: str | None = None,
    retry_allowed: bool = False,
) -> dict:
    totals = context["totals"]
    result = {
        "status": result_status,
        "physical_volume_id": context["snapshot"].get("physical_volume_id"),
        "initial_free_percent": context["snapshot"].get("initial_free_percent"),
        "final_free_percent": probe.get("free_percent"),
        "target_percent": AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT,
        "planned_count": int(totals.get("planned_count") or 0),
        "deleted_count": int(totals.get("deleted_count") or 0),
        "skipped_count": int(totals.get("skipped_count") or 0),
        "failed_count": int(totals.get("failed_count") or 0),
        "bytes_freed": int(totals.get("bytes_freed") or 0),
        "reason_counts": _bounded_counts(totals.get("reason_counts") or {}),
    }
    finish_operation(
        db,
        context["handle"],
        status=operation_status,
        result=result,
        progress=_auto_free_progress(context, probe),
        reason_code=reason_code,
        next_action=next_action,
        retry_allowed=retry_allowed,
        retry_mode="scheduled" if retry_allowed else None,
    )
    context["terminal"] = True
    context["result"] = result
    return result


def prepare_auto_free_context(db: Session, group: dict) -> dict | None:
    claim = _claim_auto_free_operation(db, group)
    if claim.get("state") == "terminal":
        return None
    if claim.get("state") != "claimed":
        return None
    stored = db.get(StorageOperation, claim["handle"].operation_id)
    progress = dict(stored.progress or {})
    snapshot = dict(progress.get("pressure_snapshot") or {})
    cursor = None
    if progress.get("scan_cursor_started_at") and progress.get("scan_cursor_id") is not None:
        cursor = (
            datetime.fromisoformat(str(progress["scan_cursor_started_at"]).replace("Z", "+00:00")).replace(tzinfo=None),
            int(progress["scan_cursor_id"]),
        )
    return {
        "handle": claim["handle"],
        "snapshot": snapshot,
        "totals": {
            "planned_count": int(progress.get("planned_count") or 0),
            "deleted_count": int(progress.get("completed_count") or 0),
            "skipped_count": int(progress.get("skipped_count") or 0),
            "failed_count": int(progress.get("failed_count") or 0),
            "bytes_freed": int(progress.get("completed_bytes") or 0),
            "reason_counts": Counter(progress.get("reason_counts") or {}),
        },
        "scan_cursor": cursor,
        "terminal": False,
        "result": None,
    }


def _probe_auto_free_context(db: Session, context: dict) -> dict:
    snapshot = context["snapshot"]
    return _probe_group_by_roots(
        db,
        list(snapshot.get("root_ids") or []),
        expected_physical_volume_id=str(snapshot.get("physical_volume_id") or "") or None,
        expected_runtime_device_id=str(snapshot.get("runtime_device_id") or "") or None,
    )


def run_auto_free_slice(db: Session, context: dict, *, page_size: int) -> dict:
    apply_critical_recording_protection(db)
    probe = _probe_auto_free_context(db, context)
    if probe.get("free_percent") is None:
        return _finish_auto_free_context(
            db,
            context,
            probe,
            operation_status="blocked",
            result_status="capacity_unknown",
            reason_code=str(probe.get("status") or "capacity_unknown"),
            next_action="check_storage_access",
            retry_allowed=True,
        )
    if float(probe["free_percent"]) >= AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT:
        return _finish_auto_free_context(
            db,
            context,
            probe,
            operation_status="completed",
            result_status="target_reached",
        )
    policy = auto_free_acknowledgement_state(get_system_settings(db))
    if not policy["effective_enabled"]:
        terminal = "partial" if context["totals"]["deleted_count"] else "blocked"
        return _finish_auto_free_context(
            db,
            context,
            probe,
            operation_status=terminal,
            result_status="policy_not_effective",
            reason_code="auto_free_space_policy_not_effective",
            next_action="review_and_confirm_auto_free_terms",
        )
    selected = _select_group_candidates(
        db,
        probe["root_ids"],
        limit=page_size,
        cursor=context.get("scan_cursor"),
    )
    context["totals"]["reason_counts"].update(selected["blocker_counts"])
    if not selected["segments"]:
        if not selected["scan_complete"] and selected["next_cursor"] is not None:
            context["scan_cursor"] = selected["next_cursor"]
            heartbeat_operation(db, context["handle"], progress=_auto_free_progress(context, probe))
            return {"status": "running", "free_percent": probe.get("free_percent")}
        terminal = "partial" if context["totals"]["deleted_count"] else "blocked"
        return _finish_auto_free_context(
            db,
            context,
            probe,
            operation_status=terminal,
            result_status="no_safe_candidate",
            reason_code="auto_free_no_safe_candidates",
            next_action="review_storage_problems",
            retry_allowed=True,
        )
    context["scan_cursor"] = None
    execution = execute_segments(
        db,
        selected["segments"],
        actor=None,
        operation="retention_auto_free_space",
        reason="low_disk",
        max_candidates=page_size,
        policy=EXECUTION_POLICY_AUTOMATIC_BOUNDED,
        operation_id=context["handle"].operation_id,
        scope={
            "type": "root",
            "root_ids": probe["root_ids"],
            "camera_ids": [],
            "segment_ids": [int(row.id) for row in selected["segments"]],
        },
        expected_identities=selected["expected_identities"],
        outer_operation_handle=context["handle"],
        manage_outer_operation=False,
        write_terminal_audit=False,
    )
    totals = context["totals"]
    totals["planned_count"] += int(execution.get("planned_count") or 0)
    totals["deleted_count"] += int(execution.get("deleted_count") or 0)
    totals["skipped_count"] += int(execution.get("skipped_count") or 0)
    totals["failed_count"] += int(execution.get("failed_count") or 0)
    totals["bytes_freed"] += int(execution.get("bytes_freed") or 0)
    totals["reason_counts"].update(execution.get("reason_counts") or {})
    probe = _probe_auto_free_context(db, context)
    heartbeat_operation(db, context["handle"], progress=_auto_free_progress(context, probe))
    apply_critical_recording_protection(db)
    if (
        probe.get("free_percent") is not None
        and float(probe["free_percent"]) >= AUTO_FREE_SPACE_RECOVERY_THRESHOLD_PERCENT
    ):
        return _finish_auto_free_context(
            db,
            context,
            probe,
            operation_status="completed",
            result_status="target_reached",
        )
    if int(execution.get("deleted_count") or 0) <= 0:
        terminal = "partial" if totals["deleted_count"] else "blocked"
        return _finish_auto_free_context(
            db,
            context,
            probe,
            operation_status=terminal,
            result_status="no_progress",
            reason_code="auto_free_no_progress",
            next_action="review_storage_problems",
            retry_allowed=True,
        )
    return {"status": "running", "free_percent": probe.get("free_percent")}


def run_auto_free_pressure_groups(db: Session, *, page_size: int) -> dict:
    groups = storage_volume_groups(db)
    critical = apply_critical_recording_protection(db, groups)
    acknowledgement = auto_free_acknowledgement_state(get_system_settings(db))
    pressured = [
        group
        for group in groups
        if group.get("physical_volume_id")
        and group.get("capacity", {}).get("free_percent") is not None
        and float(group["capacity"]["free_percent"]) < AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT
    ]
    if not acknowledgement["effective_enabled"]:
        return {
            "status": "policy_not_effective",
            "pressure_count": len(pressured),
            "operation_count": 0,
            "critical": critical,
        }
    contexts = []
    results: list[dict] = []
    for group in pressured:
        try:
            context = prepare_auto_free_context(db, group)
        except (StorageOperationConflict, StorageOperationContractError) as exc:
            db.rollback()
            results.append(
                {
                    "status": "blocked",
                    "physical_volume_id": group.get("physical_volume_id"),
                    "reason_counts": {
                        safe_reason_code(
                            getattr(exc, "detail", None) or str(exc),
                            fallback="storage_operation_conflict",
                        ): 1
                    },
                    "deleted_count": 0,
                    "bytes_freed": 0,
                }
            )
            continue
        if context is not None:
            contexts.append(context)
    while contexts:
        next_round = []
        for context in contexts:
            try:
                result = run_auto_free_slice(db, context, page_size=page_size)
            except StorageOperationLeaseLost:
                db.rollback()
                context["terminal"] = True
                context["result"] = {
                    "status": "interrupted",
                    "physical_volume_id": context["snapshot"].get("physical_volume_id"),
                    "deleted_count": int(context["totals"].get("deleted_count") or 0),
                    "bytes_freed": int(context["totals"].get("bytes_freed") or 0),
                    "reason_counts": {"storage_operation_lease_lost": 1},
                }
                result = context["result"]
            except Exception:
                db.rollback()
                probe = _probe_auto_free_context(db, context)
                try:
                    result = _finish_auto_free_context(
                        db,
                        context,
                        probe,
                        operation_status="partial" if context["totals"]["deleted_count"] else "failed",
                        result_status="failed",
                        reason_code="automatic_auto_free_failed",
                        next_action="retry_operation",
                        retry_allowed=True,
                    )
                except Exception:
                    db.rollback()
                    context["terminal"] = True
                    context["result"] = {
                        "status": "interrupted",
                        "physical_volume_id": context["snapshot"].get("physical_volume_id"),
                        "deleted_count": int(context["totals"].get("deleted_count") or 0),
                        "bytes_freed": int(context["totals"].get("bytes_freed") or 0),
                        "reason_counts": {"operation_terminal_persistence_failed": 1},
                    }
                    result = context["result"]
            if context["terminal"]:
                results.append(dict(context["result"] or result))
            else:
                next_round.append(context)
        contexts = next_round
    return {
        "status": "completed",
        "pressure_count": len(pressured),
        "operation_count": len(results),
        "deleted_count": sum(int(item.get("deleted_count") or 0) for item in results),
        "bytes_freed": sum(int(item.get("bytes_freed") or 0) for item in results),
        "result_status_counts": _bounded_counts(Counter(str(item.get("status") or "unknown") for item in results)),
        "critical": critical,
    }


def storage_pressure_present(db: Session, groups: list[dict] | None = None) -> bool:
    policy = auto_free_acknowledgement_state(get_system_settings_read_only(db))
    if not policy["effective_enabled"]:
        return False
    return any(
        group.get("capacity", {}).get("free_percent") is not None
        and float(group["capacity"]["free_percent"]) < AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT
        for group in (groups if groups is not None else storage_volume_groups(db))
        if group.get("physical_volume_id")
    )


def retention_slice_preemption_required(db: Session) -> bool:
    groups = storage_volume_groups(db)
    apply_critical_recording_protection(db, groups)
    return storage_pressure_present(db, groups)


def _latest_operations(db: Session, operation_type: str) -> list[StorageOperation]:
    return (
        db.query(StorageOperation)
        .filter(StorageOperation.operation_type == operation_type)
        .order_by(StorageOperation.updated_at.desc(), StorageOperation.id.desc())
        .limit(STATUS_OPERATION_LIMIT)
        .all()
    )


def retention_runtime_status(db: Session) -> dict:
    signal = retention_signal_status(db)
    rows = _latest_operations(db, "retention_auto_run")
    now = database_now(db)
    active = next((row for row in rows if operation_effective_status(row, now) in ACTIVE_OPERATION_STATUSES), None)
    interrupted = next((row for row in rows if operation_effective_status(row, now) == "interrupted"), None)
    latest_terminal = next((row for row in rows if row.status in TERMINAL_OPERATION_STATUSES), None)
    last_result = dict((latest_terminal.result if latest_terminal else None) or {})
    camera_rows = (
        db.query(
            Camera.deleted_at,
            Camera.enabled,
            Camera.retention_days,
            Camera.storage_quota_gb,
            func.count(RecordingSegment.id),
        )
        .join(RecordingSegment, RecordingSegment.camera_id == Camera.id)
        .filter(
            RecordingSegment.ownership == OWNERSHIP_KM_VMS,
            RecordingSegment.source == RECORDER_SOURCE,
            RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
            RecordingSegment.deleted_at.is_(None),
        )
        .group_by(
            Camera.id,
            Camera.deleted_at,
            Camera.enabled,
            Camera.retention_days,
            Camera.storage_quota_gb,
        )
        .all()
    )
    next_due = earliest_retention_due_at(db)
    last_result_state = str(last_result.get("status") or (latest_terminal.status if latest_terminal else "idle"))
    state = (
        "running"
        if active
        else "pending"
        if signal["pending"]
        else "interrupted"
        if interrupted
        else last_result_state
    )
    latest_evidence = active or interrupted or latest_terminal
    meaningful_rule_count = sum(
        1
        for _deleted_at, _enabled, days, quota, _count in camera_rows
        if int(days or 0) > 0 or int(quota or 0) > 0
    )
    return {
        "enabled": True,
        "state": state,
        "running": active is not None,
        "pending": bool(signal["pending"]),
        "signal": signal,
        "configured_camera_count": len(camera_rows),
        "active_camera_count": sum(
            1 for deleted_at, enabled, _days, _quota, _count in camera_rows if deleted_at is None and enabled
        ),
        "disabled_camera_count": sum(
            1 for deleted_at, enabled, _days, _quota, _count in camera_rows if deleted_at is None and not enabled
        ),
        "retained_deleted_camera_count": sum(
            1 for deleted_at, _enabled, _days, _quota, _count in camera_rows if deleted_at is not None
        ),
        "meaningful_rule_camera_count": meaningful_rule_count,
        "missing_or_invalid_rule_camera_count": max(0, len(camera_rows) - meaningful_rule_count),
        "pending_new_policy": bool(signal["pending"]),
        "next_due_at": _iso(next_due),
        "last_started_at": _iso(latest_evidence.started_at) if latest_evidence else None,
        "last_finished_at": _iso(latest_terminal.finished_at) if latest_terminal else None,
        "last_status": (
            "running"
            if active
            else "interrupted"
            if interrupted
            else str(latest_terminal.status)
            if latest_terminal
            else "never_run"
        ),
        "last_error": (
            "storage_operation_interrupted"
            if interrupted
            else latest_terminal.reason_code
            if latest_terminal
            else None
        ),
        "last_summary": last_result or None,
        "recent_count": len(rows),
    }


def auto_free_runtime_status(
    db: Session,
    groups: list[dict] | None = None,
    *,
    policy: dict | None = None,
) -> dict:
    policy = dict(policy or low_disk_policy_status(db, groups))
    rows = _latest_operations(db, "retention_auto_free_space")
    now = database_now(db)
    active = [row for row in rows if operation_effective_status(row, now) in ACTIVE_OPERATION_STATUSES]
    interrupted = [row for row in rows if operation_effective_status(row, now) == "interrupted"]
    latest_terminal = next((row for row in rows if row.status in TERMINAL_OPERATION_STATUSES), None)
    latest_evidence = (active or interrupted or ([latest_terminal] if latest_terminal else []))
    latest_evidence_row = latest_evidence[0] if latest_evidence else None
    volume_groups = []
    for volume in policy["volume_groups"][:STATUS_VOLUME_LIMIT]:
        volume_id = volume.get("physical_volume_id")
        matching = next(
            (
                row
                for row in rows
                if volume_id
                and volume_id in set(canonical_operation_scope(row.scope).get("physical_volume_ids") or [])
            ),
            None,
        )
        matching_effective = operation_effective_status(matching, now) if matching is not None else None
        volume_groups.append(
            {
                **volume,
                "cleanup_state": matching_effective or "idle",
                "last_cleanup_status": str(matching.status) if matching is not None else "never_run",
                "last_cleanup_reason": matching.reason_code if matching is not None else None,
                "last_cleanup_finished_at": _iso(matching.finished_at) if matching is not None else None,
                "last_cleanup_summary": dict((matching.result if matching is not None else None) or {}) or None,
            }
        )
    return {
        "enabled": bool(policy["auto_free_space_cleanup_enabled"]),
        "effective_enabled": bool(policy["auto_free_space_cleanup_effective"]),
        "acknowledgement_required": bool(policy["acknowledgement_required"]),
        "terms_version": policy["terms_version"],
        "running": bool(active),
        "active_operation_count": len(active),
        "last_started_at": _iso(latest_evidence_row.started_at) if latest_evidence_row else None,
        "last_finished_at": _iso(latest_terminal.finished_at) if latest_terminal else None,
        "last_status": (
            "running"
            if active
            else "interrupted"
            if interrupted
            else str(latest_terminal.status)
            if latest_terminal
            else "never_run"
        ),
        "last_error": (
            "storage_operation_interrupted"
            if interrupted
            else latest_terminal.reason_code
            if latest_terminal
            else None
        ),
        "last_trigger": "physical_volume_pressure" if latest_terminal else None,
        "last_summary": dict((latest_terminal.result if latest_terminal else None) or {}) or None,
        "run_count": len(rows),
        "volume_groups": volume_groups,
    }
