from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session, object_session

from app.core.config import settings
from app.core.sanitization import redact_text
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.user import User
from app.services.audit_log import create_event
from app.services.recording_operations import (
    DestructiveScopeConflict,
    LeaseHeartbeat,
    ScopeLease,
    destructive_scope_guard,
    new_operation_id,
    operation_scope_mutation_guard,
    scope_for_segments,
)
from app.services.recording_storage import (
    archive_root_for_segment,
    archive_root_runtime_access_state,
    is_kmvms_namespace_relative,
    resolve_segment_file_path,
    segment_has_resolved_archive_root,
    segment_relative_path,
)
from app.services.timezone_contract import retention_cutoff_storage, timezone_context, utc_now_storage
from app.services.storage_operation_conflicts import (
    DELETION_EXECUTION_TYPES,
    StorageOperationLifecycle,
    StorageOperationConflict,
    claim_state_detail,
    claim_operation_with_conflicts,
    operation_instance_id,
    terminal_replay_result,
)
from app.services.storage_operations_foundation import (
    OperationHeartbeatController,
    OperationHandle as StorageOperationHandle,
    safe_reason_code,
)

OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
SEGMENT_STATUS_FINALIZED = "finalized"
SEGMENT_STATUS_DELETED = "deleted"
RETENTION_LOCK_NAME = ".kmvms_retention_apply.lock"
RETENTION_LOCK_STALE_AFTER_SECONDS = 60 * 60
ACTIVE_JOB_STATES = {"starting", "recording", "stopping", "restarting"}
DEFAULT_MAX_CANDIDATES = 100
HARD_MAX_CANDIDATES = 1000
DEFAULT_MANUAL_MAX_BYTES = 10 * 1024 * 1024 * 1024
HARD_MANUAL_MAX_BYTES = 100 * 1024 * 1024 * 1024
EXECUTION_POLICY_AUTOMATIC_BOUNDED = "automatic_bounded"
EXECUTION_POLICY_MANUAL_BOUNDED = "manual_bounded"
EXECUTION_POLICY_MANUAL_COMPLETE = "manual_complete"
MANUAL_BATCH_SIZE = 100
MAX_RESULT_SAMPLES = 100
PROBLEM_INTEGRITY_STATUSES = {
    "missing_file",
    "orphan_metadata",
    "orphan_file",
    "pre_metadata_km_vms_file",
    "legacy_archive_file",
    "foreign_file",
    "unknown_file",
    "zero_size_file",
    "partial_file",
    "corrupted_file",
    "stale_writing_segment",
    "invalid_path",
    "path_outside_storage",
    "unreadable_file",
    "storage_unavailable",
}


def _foundation_scope(scope: dict, segments: list[RecordingSegment]) -> dict:
    scope_type = str(scope.get("type") or "segments")
    return {
        "global": scope_type == "all",
        "root_ids": list(scope.get("root_ids") or [segment.archive_root_id for segment in segments if segment.archive_root_id]),
        "camera_ids": list(scope.get("camera_ids") or [segment.camera_id for segment in segments]),
        "segment_ids": list(scope.get("segment_ids") or [segment.id for segment in segments]),
        "physical_volume_ids": [],
    }


def _now() -> datetime:
    return utc_now_storage()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _storage_root() -> Path:
    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_rel(segment: RecordingSegment) -> tuple[str | None, Path | None, str | None]:
    if not segment.relative_path:
        return None, None, "missing_relative_path"
    if not segment_has_resolved_archive_root(segment):
        return segment.relative_path.replace("\\", "/").lstrip("/"), None, "root_unresolved"
    try:
        db = object_session(segment)
        if db is None:
            return None, None, "db_session_missing"
        root = archive_root_for_segment(db, segment)
        access = archive_root_runtime_access_state(root)
        if access.get("read_access_state") != "available":
            return segment.relative_path.replace("\\", "/").lstrip("/"), None, "storage_unavailable"
        target = resolve_segment_file_path(db, segment)
        rel_path = segment_relative_path(db, segment)
    except ValueError as exc:
        error = str(exc) or "path_escape_attempt"
        if error == "path_outside_archive_root":
            error = "path_outside_storage"
        return None, None, error
    except FileNotFoundError:
        return segment.relative_path.replace("\\", "/").lstrip("/"), None, "file_missing"
    return (rel_path or segment.relative_path).replace("\\", "/").lstrip("/"), target, None


def _active_job_ids(db: Session) -> set[str]:
    return {
        str(job_id)
        for (job_id,) in db.query(RecordingJob.id)
        .filter(RecordingJob.state.in_(ACTIVE_JOB_STATES))
        .all()
        if job_id
    }


def _base_result(
    operation: str,
    *,
    dry_run: bool,
    operation_id: str | None = None,
    scope: dict | None = None,
) -> dict:
    started_at = _now()
    return {
        "ok": False,
        "operation": operation,
        "operation_id": operation_id,
        "scope": scope,
        "dry_run": dry_run,
        "status": "running",
        "requested_count": 0,
        "planned_count": 0,
        "processed_count": 0,
        "deleted_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "not_found_count": 0,
        "bytes_freed": 0,
        "candidates_count": 0,
        "limit_applied": None,
        "limit_exceeded": False,
        "batch_count": 0,
        "started_at": _iso(started_at),
        "finished_at": None,
        "generated_at": _iso(started_at),
        "items": [],
        "sample_eligible_count": 0,
        "sample_truncated": False,
        "per_camera": {},
        "reason_counts": {},
        "skipped_reason_counts": {},
        "failed_reason_counts": {},
        "warnings": [],
        "retryable": False,
    }


def _finish(result: dict) -> dict:
    result["finished_at"] = _iso(_now())
    deleted = int(result.get("deleted_count") or 0)
    skipped = int(result.get("skipped_count") or 0)
    failed = int(result.get("failed_count") or 0)
    result["processed_count"] = (
        int(result.get("planned_count") or 0) + skipped + failed
        if result.get("dry_run")
        else deleted + skipped + failed
    )
    if failed:
        result["status"] = "partial" if deleted else "failed"
    elif skipped or result.get("limit_exceeded"):
        result["status"] = "partial" if deleted else "blocked"
    else:
        result["status"] = "completed"
    result["ok"] = bool(
        result["status"] == "completed"
        and failed == 0
        and skipped == 0
        and not result.get("limit_exceeded", False)
        and result["processed_count"] >= int(result.get("requested_count") or 0)
    )
    return result


def _item(
    segment: RecordingSegment | None,
    *,
    action: str,
    reason: str,
    error: str | None = None,
    size_bytes: int = 0,
) -> dict:
    path_name = None
    if segment and segment.relative_path:
        path_name = Path(str(segment.relative_path).replace("\\", "/")).name[:160]
    return {
        "segment_id": segment.id if segment else None,
        "camera_id": segment.camera_id if segment else None,
        "path_name": path_name,
        "action": action,
        "reason": reason,
        "error": redact_text(error) if error else None,
        "size_bytes": int(size_bytes or 0),
    }


def _segment_audit_ref(segment: RecordingSegment | None) -> dict:
    if segment is None:
        return {"segment_id": None, "camera_id": None, "path_name": None}
    path_name = None
    if segment.relative_path:
        path_name = Path(str(segment.relative_path).replace("\\", "/")).name[:160]
    return {
        "segment_id": segment.id,
        "camera_id": segment.camera_id,
        "path_name": path_name,
    }


def _append_sample(result: dict, item: dict) -> None:
    result["sample_eligible_count"] = int(result.get("sample_eligible_count") or 0) + 1
    if len(result["items"]) < MAX_RESULT_SAMPLES:
        result["items"].append(item)
    else:
        result["sample_truncated"] = True


def _add_item(result: dict, item: dict) -> None:
    camera_key = str(item.get("camera_id") or "unknown")
    row = result["per_camera"].setdefault(
        camera_key,
        {"camera_id": item.get("camera_id"), "deleted_count": 0, "skipped_count": 0, "failed_count": 0, "bytes_freed": 0},
    )
    action = item.get("action")
    reason = str(item.get("reason") or "unknown")
    _append_sample(result, item)
    _add_reason_count(result, reason)
    if action == "deleted":
        result["deleted_count"] += 1
        result["bytes_freed"] += int(item.get("size_bytes") or 0)
        row["deleted_count"] += 1
        row["bytes_freed"] += int(item.get("size_bytes") or 0)
    elif action == "failed":
        result["failed_count"] += 1
        row["failed_count"] += 1
        counts = result.setdefault("failed_reason_counts", {})
        counts[reason] = int(counts.get(reason) or 0) + 1
    else:
        result["skipped_count"] += 1
        row["skipped_count"] += 1
        counts = result.setdefault("skipped_reason_counts", {})
        counts[reason] = int(counts.get(reason) or 0) + 1
        if item.get("reason") in {"metadata_not_found", "file_missing"}:
            result["not_found_count"] += 1


def _add_reason_count(result: dict, reason: str) -> None:
    reason_counts = result.setdefault("reason_counts", {})
    reason_counts[reason] = int(reason_counts.get(reason) or 0) + 1


def _segment_size(segment: RecordingSegment, file_path: Path | None) -> int:
    if file_path and file_path.exists() and file_path.is_file():
        try:
            return int(file_path.stat().st_size)
        except OSError:
            return int(segment.size_bytes or 0)
    return int(segment.size_bytes or 0)


def validate_segment_for_deletion(
    segment: RecordingSegment,
    *,
    active_job_ids: set[str],
    require_file: bool = True,
    block_active_job: bool = False,
    allowed_integrity_statuses: set[str] | frozenset[str] | None = None,
) -> tuple[bool, str, Path | None, int]:
    if segment.ownership != OWNERSHIP_KM_VMS:
        return False, "unowned", None, 0
    if segment.source != RECORDER_SOURCE:
        return False, "foreign_source", None, 0
    if segment.status == SEGMENT_STATUS_DELETED:
        return False, "already_deleted", None, 0
    if block_active_job and segment.job_id and str(segment.job_id) in active_job_ids:
        return False, "active_job", None, 0
    if segment.status != SEGMENT_STATUS_FINALIZED:
        return False, "not_finalized", None, 0
    allowed_integrity = set(allowed_integrity_statuses or ())
    if segment.integrity_status in PROBLEM_INTEGRITY_STATUSES and segment.integrity_status not in allowed_integrity:
        return False, "integrity_problem", None, 0

    rel_path, file_path, path_error = _safe_rel(segment)
    if path_error or not rel_path or file_path is None:
        return False, path_error or "missing_relative_path", None, 0
    if not is_kmvms_namespace_relative(rel_path):
        return False, "outside_kmvms_namespace", file_path, 0
    if require_file and not file_path.exists():
        return False, "file_missing", file_path, 0
    if require_file and not file_path.is_file():
        return False, "not_file", file_path, 0
    return True, "eligible", file_path, _segment_size(segment, file_path)


def _matches_expected_identity(db: Session, segment: RecordingSegment, expected: dict | None) -> bool:
    if not expected:
        return True
    try:
        return bool(
            int(segment.id) == int(expected.get("segment_id") or 0)
            and int(segment.camera_id) == int(expected.get("camera_id") or 0)
            and str(segment.archive_root_id or "") == str(expected.get("archive_root_id") or "")
            and str(segment_relative_path(db, segment) or "") == str(expected.get("relative_path") or "")
            and int(segment.size_bytes or 0) == int(expected.get("size_bytes") or 0)
        )
    except (TypeError, ValueError):
        return False


def _fresh_segment(db: Session, segment_id: int) -> RecordingSegment | None:
    return (
        db.query(RecordingSegment)
        .populate_existing()
        .filter(RecordingSegment.id == int(segment_id))
        .first()
    )


def _eligible_segments_query(db: Session):
    return (
        db.query(RecordingSegment)
        .filter(
            RecordingSegment.ownership == OWNERSHIP_KM_VMS,
            RecordingSegment.source == RECORDER_SOURCE,
            RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
            RecordingSegment.relative_path.isnot(None),
            or_(
                RecordingSegment.integrity_status.is_(None),
                ~RecordingSegment.integrity_status.in_(PROBLEM_INTEGRITY_STATUSES),
            ),
        )
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
    )


def _all_owned_segments_query(db: Session):
    return db.query(RecordingSegment).filter(
        RecordingSegment.ownership == OWNERSHIP_KM_VMS,
        RecordingSegment.source == RECORDER_SOURCE,
    )


def _retention_observability_counts(
    db: Session,
    *,
    camera_id: int | None,
    candidate_ids: set[int],
    active_job_ids: set[str],
) -> dict:
    counts = {
        "candidate_count": len(candidate_ids),
        "retention_not_due_count": 0,
        "non_finalized_count": 0,
        "active_or_writing_count": 0,
        "outside_namespace_count": 0,
        "root_unavailable_count": 0,
        "root_unresolved_count": 0,
        "integrity_problem_count": 0,
        "missing_file_count": 0,
        "foreign_or_unowned_count": 0,
    }

    query = db.query(RecordingSegment)
    if camera_id is not None:
        query = query.filter(RecordingSegment.camera_id == camera_id)

    for segment in query.all():
        if segment.ownership != OWNERSHIP_KM_VMS or segment.source != RECORDER_SOURCE:
            counts["foreign_or_unowned_count"] += 1
            continue
        if segment.job_id and str(segment.job_id) in active_job_ids:
            counts["active_or_writing_count"] += 1
        if segment.status != SEGMENT_STATUS_FINALIZED:
            counts["non_finalized_count"] += 1
            if segment.status in {"writing", "starting", "stopping", "restarting"}:
                counts["active_or_writing_count"] += 1
            continue
        if segment.integrity_status in PROBLEM_INTEGRITY_STATUSES:
            counts["integrity_problem_count"] += 1

        rel_path, file_path, path_error = _safe_rel(segment)
        if path_error == "storage_unavailable":
            counts["root_unavailable_count"] += 1
        elif path_error == "root_unresolved":
            counts["root_unresolved_count"] += 1
        elif path_error or not rel_path or file_path is None or not is_kmvms_namespace_relative(rel_path):
            counts["outside_namespace_count"] += 1
        elif not file_path.exists():
            counts["missing_file_count"] += 1

        if segment.id not in candidate_ids:
            counts["retention_not_due_count"] += 1
    return counts


def _policy_candidates(db: Session, *, camera_id: int | None = None) -> tuple[list[tuple[RecordingSegment, str]], dict]:
    ctx = timezone_context(db)
    cameras = {camera.id: camera for camera in db.query(Camera).order_by(Camera.id.asc()).all()}
    query = _eligible_segments_query(db)
    if camera_id is not None:
        query = query.filter(RecordingSegment.camera_id == camera_id)
    segments = query.all()
    by_segment: dict[int, tuple[RecordingSegment, set[str]]] = {}
    policy_summary = {"days": {}, "quota": {}, "unsupported": {"storage_pressure": "not_configured"}}

    for segment in segments:
        camera = cameras.get(segment.camera_id)
        if not camera:
            continue
        retention_days = int(camera.retention_days or 0)
        if retention_days > 0:
            storage_cutoff, local_compat_cutoff = retention_cutoff_storage(retention_days, ctx)
        if retention_days > 0 and segment.started_at and (
            segment.started_at < storage_cutoff or segment.started_at < local_compat_cutoff
        ):
            by_segment.setdefault(segment.id, (segment, set()))[1].add("retention_days")
            policy_summary["days"][str(camera.id)] = {
                "retention_days": retention_days,
                "timezone": ctx.name,
                "cutoff_storage_utc": storage_cutoff.isoformat(),
                "cutoff_local_compat": local_compat_cutoff.isoformat(),
                "boundary": "system_timezone_calendar_day",
            }

    per_camera_segments: dict[int, list[RecordingSegment]] = defaultdict(list)
    for segment in segments:
        per_camera_segments[segment.camera_id].append(segment)

    active_job_ids = _active_job_ids(db)
    for current_camera_id, camera_segments in per_camera_segments.items():
        camera = cameras.get(current_camera_id)
        if not camera:
            continue
        quota_gb = int(camera.storage_quota_gb or 0)
        if quota_gb <= 0:
            continue
        quota_bytes = quota_gb * 1024 * 1024 * 1024
        sized_rows: list[tuple[RecordingSegment, int]] = []
        total = 0
        for segment in camera_segments:
            ok, _reason, file_path, size = validate_segment_for_deletion(segment, active_job_ids=active_job_ids)
            if ok:
                sized_rows.append((segment, size))
                total += size
        policy_summary["quota"][str(current_camera_id)] = {
            "storage_quota_gb": quota_gb,
            "quota_bytes": quota_bytes,
            "owned_finalized_bytes": total,
        }
        while total > quota_bytes and sized_rows:
            segment, size = sized_rows.pop(0)
            by_segment.setdefault(segment.id, (segment, set()))[1].add("storage_quota")
            total -= size

    candidates: list[tuple[RecordingSegment, str]] = []
    for segment, reasons in by_segment.values():
        candidates.append((segment, "+".join(sorted(reasons))))
    candidates.sort(key=lambda row: (row[0].started_at or row[0].created_at, row[0].id))
    return candidates, policy_summary


def build_retention_plan(
    db: Session,
    *,
    camera_id: int | None = None,
    actor: User | None = None,
    write_audit: bool = False,
) -> dict:
    if write_audit:
        _audit(
            db,
            actor,
            event_type="retention.dry_run_started",
            message="Retention dry-run started",
            metadata={"camera_id": camera_id},
        )
    result = _base_result("retention_dry_run", dry_run=True)
    candidates, policy_summary = _policy_candidates(db, camera_id=camera_id)
    active_job_ids = _active_job_ids(db)
    candidate_ids = {segment.id for segment, _reason in candidates}
    result["policy"] = policy_summary
    result["observability"] = _retention_observability_counts(
        db,
        camera_id=camera_id,
        candidate_ids=candidate_ids,
        active_job_ids=active_job_ids,
    )
    result["requested_count"] = len(candidates)
    for segment, policy_reason in candidates:
        ok, reason, _file_path, size = validate_segment_for_deletion(segment, active_job_ids=active_job_ids)
        if ok:
            result["planned_count"] += 1
            result["candidates_count"] += 1
            item = _item(segment, action="candidate", reason=policy_reason, size_bytes=size)
            _append_sample(result, item)
            camera_key = str(segment.camera_id)
            row = result["per_camera"].setdefault(
                camera_key,
                {"camera_id": segment.camera_id, "deleted_count": 0, "skipped_count": 0, "failed_count": 0, "bytes_freed": 0},
            )
            row["bytes_freed"] += size
            result["bytes_freed"] += size
        else:
            _add_item(result, _item(segment, action="skipped", reason=reason, size_bytes=size))
    result["estimated_freed_bytes"] = result["bytes_freed"]
    finished = _finish(result)
    if write_audit:
        _audit(
            db,
            actor,
            event_type="retention.dry_run_completed",
            severity="warning" if finished.get("failed_count") or finished.get("warnings") else "info",
            message="Retention dry-run completed",
            metadata=_retention_summary(finished),
        )
    return finished


class RetentionApplyLock:
    def __init__(self) -> None:
        self.path = _storage_root() / RETENTION_LOCK_NAME
        self.fd: int | None = None

    def __enter__(self):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self.fd = os.open(self.path, flags, 0o600)
            os.write(self.fd, f"{os.getpid()} {int(time.time())}\n".encode("ascii"))
        except FileExistsError as exc:
            try:
                age_seconds = max(0, int(time.time() - self.path.stat().st_mtime))
            except OSError:
                age_seconds = None
            if age_seconds is not None and age_seconds > RETENTION_LOCK_STALE_AFTER_SECONDS:
                raise RuntimeError("concurrency_lock_stale_manual_recovery_required") from exc
            raise RuntimeError("concurrency_lock") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _mark_deleted(
    db: Session,
    segment: RecordingSegment,
    *,
    actor: User | None,
    reason: str,
    source: str,
) -> None:
    segment.status = SEGMENT_STATUS_DELETED
    segment.deleted_at = _now()
    segment.deletion_reason = reason
    segment.deleted_by = getattr(actor, "username", None)
    segment.deletion_source = source
    segment.updated_at = _now()
    db.add(segment)


def _audit(
    db: Session,
    actor: User | None,
    *,
    event_type: str,
    severity: str = "info",
    message: str,
    segment: RecordingSegment | None = None,
    metadata: dict | None = None,
) -> None:
    category = "retention" if event_type.startswith("retention.") else "records"
    create_event(
        db=db,
        actor=actor,
        category=category,
        event_type=event_type,
        severity=severity,
        message_ru=message,
        message_en=message,
        target_type="recording_segment" if segment else "recording_retention",
        target_id=segment.id if segment else None,
        target_name=f"segment:{segment.id}" if segment else None,
        metadata=metadata or {},
    )


def _recover_deleted_segment_metadata(
    db: Session,
    segment: RecordingSegment,
    *,
    actor: User | None,
    reason: str,
    source: str,
    error: str,
) -> bool:
    try:
        db.rollback()
        fresh = db.get(RecordingSegment, segment.id)
        if fresh is None:
            return False
        _mark_deleted(db, fresh, actor=actor, reason=f"{reason}:metadata_recovery_after_file_delete", source=source)
        db.commit()
        _audit(
            db,
            actor,
            event_type="recordings.metadata_recovered_after_file_delete" if source.startswith("manual") else "retention.metadata_recovered_after_file_delete",
            severity="warning",
            message=f"Recording metadata recovered after file deletion for segment {fresh.id}",
            segment=fresh,
            metadata={
                **_segment_audit_ref(fresh),
                "reason": "metadata_update_failed_recovered",
                "error": error,
            },
        )
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


def execute_segments(
    db: Session,
    segments: Iterable[RecordingSegment],
    *,
    actor: User | None,
    operation: str,
    reason: str,
    max_candidates: int | None = None,
    max_bytes: int | None = None,
    policy: str = EXECUTION_POLICY_AUTOMATIC_BOUNDED,
    operation_id: str | None = None,
    scope: dict | None = None,
    scope_lease: ScopeLease | None = None,
    initial_items: Iterable[dict] | None = None,
    write_terminal_audit: bool = True,
    write_item_audit: bool = True,
    operation_heartbeat: Callable[[], None] | None = None,
    operation_owner_token: str | None = None,
    expected_identities: dict[int, dict] | None = None,
    outer_operation_handle: StorageOperationHandle | None = None,
    manage_outer_operation: bool = True,
    allowed_integrity_statuses: set[str] | frozenset[str] | None = None,
) -> dict:
    if policy not in {
        EXECUTION_POLICY_AUTOMATIC_BOUNDED,
        EXECUTION_POLICY_MANUAL_BOUNDED,
        EXECUTION_POLICY_MANUAL_COMPLETE,
    }:
        raise ValueError("invalid_recording_deletion_policy")
    segment_list = list(segments)
    operation_id = operation_id or new_operation_id(operation[:32])
    normalized_scope = scope or scope_for_segments(segment_list)
    result = _base_result(operation, dry_run=False, operation_id=operation_id, scope=normalized_scope)
    for item in initial_items or []:
        result["requested_count"] += 1
        _add_item(result, dict(item))

    common_scope = _foundation_scope(normalized_scope, segment_list)
    has_common_scope = bool(
        common_scope.get("global")
        or common_scope.get("root_ids")
        or common_scope.get("camera_ids")
        or common_scope.get("segment_ids")
    )
    if manage_outer_operation and outer_operation_handle is None and has_common_scope:
        try:
            coordinator_operation = operation if operation in DELETION_EXECUTION_TYPES else "retention_run"
            claimed_outer = claim_operation_with_conflicts(
                db,
                operation_type=coordinator_operation,
                scope=common_scope,
                request_identity={"operation_id": operation_id, "scope": normalized_scope},
                actor=actor,
                system_owner=None if actor is not None else coordinator_operation,
                operation_id=operation_id,
                idempotency_key=operation_id,
                owner_instance_id=operation_instance_id(coordinator_operation),
            )
        except StorageOperationConflict as exc:
            _add_item(result, _item(None, action="skipped", reason=str(exc.detail.get("reason_code") or "storage_operation_conflict")))
            result["retryable"] = bool(exc.detail.get("retryable", True))
            result["conflict"] = dict(exc.detail)
            finished = _finish(result)
            _write_terminal_execution_audit(db, actor, finished, write_terminal_audit=write_terminal_audit)
            return finished
        if claimed_outer.get("state") == "terminal":
            return terminal_replay_result(claimed_outer)
        if claimed_outer.get("state") != "claimed":
            detail = claim_state_detail(claimed_outer)
            _add_item(result, _item(None, action="skipped", reason=str(detail["reason_code"])))
            result["retryable"] = True
            finished = _finish(result)
            _write_terminal_execution_audit(db, actor, finished, write_terminal_audit=write_terminal_audit)
            return finished
        managed_lifecycle = StorageOperationLifecycle(
            db,
            claimed_outer["handle"],
            failure_reason="storage_deletion_execution_failed",
        )
        managed_handle = managed_lifecycle.handle
        try:
            finished = execute_segments(
                db,
                segment_list,
                actor=actor,
                operation=operation,
                reason=reason,
                max_candidates=max_candidates,
                max_bytes=max_bytes,
                policy=policy,
                operation_id=operation_id,
                scope=normalized_scope,
                scope_lease=scope_lease,
                initial_items=initial_items,
                write_terminal_audit=write_terminal_audit,
                write_item_audit=write_item_audit,
                operation_heartbeat=operation_heartbeat,
                operation_owner_token=operation_owner_token,
                expected_identities=expected_identities,
                outer_operation_handle=managed_handle,
                manage_outer_operation=False,
                allowed_integrity_statuses=allowed_integrity_statuses,
            )
            managed_lifecycle.finish_result(
                finished,
                progress={
                    "planned_count": int(finished.get("planned_count") or 0),
                    "completed_count": int(finished.get("deleted_count") or 0),
                    "failed_count": int(finished.get("failed_count") or 0),
                    "skipped_count": int(finished.get("skipped_count") or 0),
                    "completed_bytes": int(finished.get("bytes_freed") or 0),
                },
                reason_code=safe_reason_code((finished.get("warnings") or [None])[0]),
                retry_allowed=bool(finished.get("retryable")),
                retry_mode="immediate" if finished.get("retryable") else None,
            )
            return finished
        except Exception as exc:
            if not managed_lifecycle.terminalized:
                managed_lifecycle.__exit__(type(exc), exc, exc.__traceback__)
            raise

    outer_heartbeat = (
        OperationHeartbeatController(db.get_bind(), outer_operation_handle)
        if outer_operation_handle is not None
        else None
    )

    def touch_outer() -> None:
        if outer_heartbeat is not None:
            outer_heartbeat.touch(force=True)

    def combined_heartbeat(callback: Callable[[], None] | None = None) -> None:
        if callback is not None:
            callback()
        touch_outer()

    if scope_lease is None:
        try:
            with destructive_scope_guard(operation_id, normalized_scope, purpose=operation) as owned_lease:
                if operation_owner_token:
                    with LeaseHeartbeat(
                        scope_lease=owned_lease,
                        operation_id=operation_id,
                        owner_token=operation_owner_token,
                    ) as heartbeat:
                        return _execute_segments_with_lease(
                            db,
                            segment_list,
                            actor=actor,
                            operation=operation,
                            operation_id=operation_id,
                            reason=reason,
                            max_candidates=max_candidates,
                            max_bytes=max_bytes,
                            policy=policy,
                            result=result,
                            scope_lease=owned_lease,
                            write_terminal_audit=write_terminal_audit,
                            write_item_audit=write_item_audit,
                            operation_heartbeat=lambda: combined_heartbeat(heartbeat.progress),
                            operation_owner_token=operation_owner_token,
                            expected_identities=expected_identities,
                            allowed_integrity_statuses=allowed_integrity_statuses,
                        )
                return _execute_segments_with_lease(
                    db,
                    segment_list,
                    actor=actor,
                    operation=operation,
                    operation_id=operation_id,
                    reason=reason,
                    max_candidates=max_candidates,
                    max_bytes=max_bytes,
                    policy=policy,
                    result=result,
                    scope_lease=owned_lease,
                    write_terminal_audit=write_terminal_audit,
                    write_item_audit=write_item_audit,
                    operation_heartbeat=lambda: combined_heartbeat(operation_heartbeat),
                    operation_owner_token=None,
                    expected_identities=expected_identities,
                    allowed_integrity_statuses=allowed_integrity_statuses,
                )
        except DestructiveScopeConflict as exc:
            _add_item(
                result,
                _item(None, action="skipped", reason=str(exc.detail.get("reason") or "destructive_scope_conflict")),
            )
            result["retryable"] = bool(exc.detail.get("retryable", True))
            result["conflict"] = {
                "reason": exc.detail.get("reason"),
                "conflicting_operation_id": exc.detail.get("conflicting_operation_id"),
            }
            finished = _finish(result)
            _write_terminal_execution_audit(db, actor, finished, write_terminal_audit=write_terminal_audit)
            return finished

    return _execute_segments_with_lease(
        db,
        segment_list,
        actor=actor,
        operation=operation,
        operation_id=operation_id,
        reason=reason,
        max_candidates=max_candidates,
        max_bytes=max_bytes,
        policy=policy,
        result=result,
        scope_lease=scope_lease,
        write_terminal_audit=write_terminal_audit,
        write_item_audit=write_item_audit,
        operation_heartbeat=lambda: combined_heartbeat(operation_heartbeat),
        operation_owner_token=operation_owner_token,
        expected_identities=expected_identities,
        allowed_integrity_statuses=allowed_integrity_statuses,
    )


def _execute_segments_with_lease(
    db: Session,
    segments: list[RecordingSegment],
    *,
    actor: User | None,
    operation: str,
    operation_id: str,
    reason: str,
    max_candidates: int | None,
    max_bytes: int | None,
    policy: str,
    result: dict,
    scope_lease: ScopeLease,
    write_terminal_audit: bool,
    write_item_audit: bool,
    operation_heartbeat: Callable[[], None] | None,
    operation_owner_token: str | None,
    expected_identities: dict[int, dict] | None,
    allowed_integrity_statuses: set[str] | frozenset[str] | None,
) -> dict:
    if policy == EXECUTION_POLICY_AUTOMATIC_BOUNDED:
        max_candidates = min(int(max_candidates or DEFAULT_MAX_CANDIDATES), HARD_MAX_CANDIDATES)
        max_bytes = None
        result["limit_applied"] = {"max_candidates": max_candidates, "max_bytes": None, "policy": policy}
    elif policy == EXECUTION_POLICY_MANUAL_BOUNDED:
        max_candidates = min(int(max_candidates or DEFAULT_MAX_CANDIDATES), HARD_MAX_CANDIDATES)
        max_bytes = min(int(max_bytes or DEFAULT_MANUAL_MAX_BYTES), HARD_MANUAL_MAX_BYTES)
        result["limit_applied"] = {
            "max_candidates": max_candidates,
            "max_bytes": max_bytes,
            "policy": policy,
        }
    else:
        max_candidates = None
        max_bytes = None
        result["limit_applied"] = {"max_candidates": None, "max_bytes": None, "batch_size": MANUAL_BATCH_SIZE, "policy": policy}

    active_job_ids = _active_job_ids(db)
    expected_identities = expected_identities or {}
    planned: list[tuple[RecordingSegment, Path, int, dict | None]] = []

    for index, segment in enumerate(segments):
        if policy == EXECUTION_POLICY_MANUAL_COMPLETE and index % MANUAL_BATCH_SIZE == 0:
            if operation_heartbeat is not None:
                operation_heartbeat()
            scope_lease.touch()
            scope_lease.assert_owned()
        result["requested_count"] += 1
        expected = expected_identities.get(int(segment.id))
        if expected is not None and not _matches_expected_identity(db, segment, expected):
            _add_item(result, _item(segment, action="skipped", reason="deletion_plan_item_changed", size_bytes=0))
            continue
        ok, item_reason, file_path, size = validate_segment_for_deletion(
            segment,
            active_job_ids=active_job_ids,
            block_active_job=operation.startswith("camera_delete"),
            allowed_integrity_statuses=allowed_integrity_statuses,
        )
        if not ok or file_path is None:
            _add_item(result, _item(segment, action="skipped", reason=item_reason, size_bytes=size))
            continue
        planned.append((segment, file_path, size, expected))

    planned_bytes = sum(size for _segment, _path, size, _expected in planned)
    limit_exceeded = bool(
        policy == EXECUTION_POLICY_AUTOMATIC_BOUNDED
        and len(planned) > int(max_candidates or 0)
    ) or bool(
        policy == EXECUTION_POLICY_MANUAL_BOUNDED
        and (
            len(planned) > int(max_candidates or 0)
            or planned_bytes > int(max_bytes or 0)
        )
    )
    if limit_exceeded:
        result["limit_exceeded"] = True
        result["planned_count"] = len(planned)
        result["candidates_count"] = len(planned)
        result["warnings"].append("limit_exceeded")
        for segment, _path, size, _expected in planned:
            _add_item(result, _item(segment, action="skipped", reason="limit_exceeded", size_bytes=size))
        finished = _finish(result)
        _write_terminal_execution_audit(db, actor, finished, write_terminal_audit=write_terminal_audit)
        return finished

    result["planned_count"] = len(planned)
    result["candidates_count"] = len(planned)
    batch_size = MANUAL_BATCH_SIZE if policy == EXECUTION_POLICY_MANUAL_COMPLETE else max(1, len(planned))
    for offset in range(0, len(planned), batch_size):
        result["batch_count"] += 1
        if operation_heartbeat is not None:
            operation_heartbeat()
        scope_lease.touch()
        for segment, _file_path, size, expected in planned[offset : offset + batch_size]:
            if operation_heartbeat is not None:
                operation_heartbeat()
            mutation_guard = (
                operation_scope_mutation_guard(operation_id, operation_owner_token, scope_lease)
                if operation_owner_token
                else scope_lease.mutation_guard()
            )
            try:
                with mutation_guard:
                    fresh = _fresh_segment(db, int(segment.id))
                    if fresh is None:
                        _add_item(result, _item(segment, action="skipped", reason="metadata_not_found", size_bytes=0))
                        continue
                    if expected is not None and not _matches_expected_identity(db, fresh, expected):
                        _add_item(
                            result,
                            _item(fresh, action="skipped", reason="deletion_plan_item_changed", size_bytes=0),
                        )
                        continue
                    ok, final_reason, file_path, final_size = validate_segment_for_deletion(
                        fresh,
                        active_job_ids=active_job_ids,
                        block_active_job=operation.startswith("camera_delete"),
                        allowed_integrity_statuses=allowed_integrity_statuses,
                    )
                    if not ok or file_path is None:
                        _add_item(result, _item(fresh, action="skipped", reason=final_reason, size_bytes=final_size))
                        continue
                    file_path.unlink()
                    try:
                        _mark_deleted(db, fresh, actor=actor, reason=reason, source=operation)
                        db.commit()
                    except Exception as exc:
                        recovered = _recover_deleted_segment_metadata(
                            db,
                            fresh,
                            actor=actor,
                            reason=reason,
                            source=operation,
                            error=str(exc),
                        )
                        failure_reason = "metadata_update_failed_recovered" if recovered else "metadata_update_failed"
                        _add_item(
                            result,
                            _item(fresh, action="failed", reason=failure_reason, error=str(exc), size_bytes=final_size),
                        )
                        _write_item_execution_audit(
                            db,
                            actor,
                            operation=operation,
                            segment=fresh,
                            reason=failure_reason,
                            error=str(exc),
                            write_item_audit=write_item_audit,
                        )
                        continue
            except FileNotFoundError:
                db.rollback()
                _add_item(result, _item(segment, action="skipped", reason="file_missing", size_bytes=size))
                continue
            except OSError as exc:
                db.rollback()
                _add_item(result, _item(segment, action="failed", reason="delete_failed", error=str(exc), size_bytes=size))
                _write_item_execution_audit(
                    db,
                    actor,
                    operation=operation,
                    segment=segment,
                    reason="delete_failed",
                    error=str(exc),
                    write_item_audit=write_item_audit,
                )
                continue

            _add_item(result, _item(fresh, action="deleted", reason=reason, size_bytes=final_size))
            if operation.startswith("manual") and write_item_audit:
                _audit(
                    db,
                    actor,
                    event_type="recordings.deleted_segment",
                    message=f"Recording segment deleted: {fresh.id}",
                    segment=fresh,
                    metadata={**_segment_audit_ref(fresh), "reason": reason, "bytes_freed": final_size},
                )

    finished = _finish(result)
    _write_terminal_execution_audit(db, actor, finished, write_terminal_audit=write_terminal_audit)
    return finished


def _write_item_execution_audit(
    db: Session,
    actor: User | None,
    *,
    operation: str,
    segment: RecordingSegment,
    reason: str,
    error: str,
    write_item_audit: bool,
) -> None:
    if not write_item_audit:
        return
    try:
        _audit(
            db,
            actor,
            event_type="recordings.delete_failed" if operation.startswith("manual") else "retention.deletion_failed",
            severity="error",
            message=f"Recording deletion failed for segment {segment.id}",
            segment=segment,
            metadata={**_segment_audit_ref(segment), "reason": reason, "error": error},
        )
    except Exception:
        db.rollback()


def _write_terminal_execution_audit(
    db: Session,
    actor: User | None,
    result: dict,
    *,
    write_terminal_audit: bool,
) -> None:
    if not write_terminal_audit:
        return
    operation = str(result.get("operation") or "recording_delete")
    status = str(result.get("status") or "failed")
    try:
        if operation.startswith("manual"):
            event_type = f"recordings.bulk_delete_{status}"
            category_message = f"{operation} {status}"
        elif operation.startswith("retention_auto"):
            return
        else:
            event_type = f"retention.apply_{status}"
            category_message = f"{operation} {status}"
        _audit(
            db,
            actor,
            event_type=event_type,
            severity="info" if status == "completed" else "warning" if status == "blocked" else "error",
            message=category_message,
            metadata=_retention_summary(result),
        )
    except Exception:
        db.rollback()
        if "audit_write_failed" not in result["warnings"]:
            result["warnings"].append("audit_write_failed")


def begin_manual_execution_result(
    operation: str,
    *,
    operation_id: str,
    scope: dict,
    planned_count: int = 0,
) -> dict:
    result = _base_result(operation, dry_run=False, operation_id=operation_id, scope=scope)
    result["planned_count"] = max(0, int(planned_count or 0))
    result["candidates_count"] = result["planned_count"]
    result["limit_applied"] = {
        "max_candidates": None,
        "max_bytes": None,
        "batch_size": MANUAL_BATCH_SIZE,
        "policy": EXECUTION_POLICY_MANUAL_COMPLETE,
    }
    return result


def merge_execution_result(target: dict, batch: dict) -> dict:
    for key in (
        "requested_count",
        "processed_count",
        "deleted_count",
        "skipped_count",
        "failed_count",
        "not_found_count",
        "bytes_freed",
        "batch_count",
        "sample_eligible_count",
    ):
        target[key] = int(target.get(key) or 0) + int(batch.get(key) or 0)
    target["sample_truncated"] = bool(target.get("sample_truncated") or batch.get("sample_truncated"))
    for item in batch.get("items") or []:
        if len(target["items"]) < MAX_RESULT_SAMPLES:
            target["items"].append(item)
        else:
            target["sample_truncated"] = True
    for field in ("reason_counts", "skipped_reason_counts", "failed_reason_counts"):
        output = target.setdefault(field, {})
        for reason, count in (batch.get(field) or {}).items():
            output[str(reason)] = int(output.get(str(reason)) or 0) + int(count or 0)
    for camera_key, row in (batch.get("per_camera") or {}).items():
        output = target["per_camera"].setdefault(
            str(camera_key),
            {
                "camera_id": row.get("camera_id"),
                "deleted_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "bytes_freed": 0,
            },
        )
        for key in ("deleted_count", "skipped_count", "failed_count", "bytes_freed"):
            output[key] = int(output.get(key) or 0) + int(row.get(key) or 0)
    for warning in batch.get("warnings") or []:
        if warning not in target["warnings"]:
            target["warnings"].append(warning)
    target["retryable"] = bool(target.get("retryable") or batch.get("retryable"))
    return target


def append_execution_issue(
    result: dict,
    *,
    reason: str,
    action: str = "skipped",
    retryable: bool = False,
    count: int = 1,
) -> dict:
    count = max(1, int(count or 1))
    result["requested_count"] = int(result.get("requested_count") or 0) + count
    item = _item(None, action=action, reason=reason)
    _add_item(result, item)
    if count > 1:
        additional = count - 1
        result["sample_eligible_count"] = int(result.get("sample_eligible_count") or 0) + additional
        result["sample_truncated"] = True
        result["reason_counts"][reason] = int(result["reason_counts"].get(reason) or 0) + additional
        row = result["per_camera"]["unknown"]
        if action == "failed":
            result["failed_count"] += additional
            row["failed_count"] += additional
            result["failed_reason_counts"][reason] = int(result["failed_reason_counts"].get(reason) or 0) + additional
        else:
            result["skipped_count"] += additional
            row["skipped_count"] += additional
            result["skipped_reason_counts"][reason] = int(result["skipped_reason_counts"].get(reason) or 0) + additional
    result["retryable"] = bool(result.get("retryable") or retryable)
    return result


def enforce_exact_planned_accounting(result: dict, planned_count: int) -> dict:
    expected = max(0, int(planned_count or 0))
    observed = sum(
        int(result.get(key) or 0)
        for key in ("deleted_count", "skipped_count", "failed_count")
    )
    result["accounting_expected_count"] = expected
    result["accounting_observed_count"] = observed
    if observed != expected:
        append_execution_issue(
            result,
            reason="deletion_plan_accounting_mismatch",
            action="failed",
            retryable=True,
            count=max(1, expected - observed),
        )
    result["requested_count"] = max(int(result.get("requested_count") or 0), expected)
    return result


def finish_manual_execution_result(
    db: Session,
    actor: User | None,
    result: dict,
    *,
    write_terminal_audit: bool = True,
) -> dict:
    finished = _finish(result)
    _write_terminal_execution_audit(db, actor, finished, write_terminal_audit=write_terminal_audit)
    return finished


def preview_segments(
    db: Session,
    segments: Iterable[RecordingSegment],
    *,
    operation: str,
    reason: str,
) -> dict:
    result = _base_result(operation, dry_run=True)
    active_job_ids = _active_job_ids(db)
    for segment in segments:
        result["requested_count"] += 1
        ok, item_reason, _file_path, size = validate_segment_for_deletion(
            segment,
            active_job_ids=active_job_ids,
            block_active_job=operation.startswith("camera_delete"),
        )
        if ok:
            result["planned_count"] += 1
            result["candidates_count"] += 1
            result["bytes_freed"] += int(size or 0)
            result["estimated_freed_bytes"] = result["bytes_freed"]
            _append_sample(result, _item(segment, action="candidate", reason=reason, size_bytes=size))
            camera_key = str(segment.camera_id)
            row = result["per_camera"].setdefault(
                camera_key,
                {"camera_id": segment.camera_id, "deleted_count": 0, "skipped_count": 0, "failed_count": 0, "bytes_freed": 0},
            )
            row["bytes_freed"] += int(size or 0)
        else:
            _add_item(result, _item(segment, action="skipped", reason=item_reason, size_bytes=size))
    result.setdefault("estimated_freed_bytes", result["bytes_freed"])
    return _finish(result)


def run_retention(
    db: Session,
    *,
    actor: User | None,
    camera_id: int | None = None,
    max_candidates: int | None = None,
    max_bytes: int | None = None,
    operation: str = "retention_run",
    reason: str = "retention_policy",
) -> dict:
    try:
        with RetentionApplyLock():
            candidates, _policy_summary = _policy_candidates(db, camera_id=camera_id)
            _audit(
                db,
                actor,
                event_type="retention.apply_started",
                message="Retention apply started",
                metadata={
                    "camera_id": camera_id,
                    "max_candidates": max_candidates,
                    "max_bytes": max_bytes,
                    "candidate_count": len(candidates),
                },
            )
            return execute_segments(
                db,
                (segment for segment, _reason in candidates),
                actor=actor,
                operation=operation,
                reason=reason,
                max_candidates=max_candidates,
                max_bytes=max_bytes,
                policy=EXECUTION_POLICY_MANUAL_BOUNDED,
            )
    except RuntimeError as exc:
        result = _base_result(operation, dry_run=False)
        _add_item(result, _item(None, action="skipped", reason=str(exc) or "concurrency_lock"))
        result["ok"] = False
        finished = _finish(result)
        _audit(
            db,
            actor,
            event_type="retention.apply_failed",
            severity="error",
            message="Retention apply failed",
            metadata=_retention_summary(finished),
        )
        return finished


def _retention_summary(result: dict) -> dict:
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "operation": result.get("operation"),
        "operation_id": result.get("operation_id"),
        "scope": result.get("scope"),
        "requested_count": int(result.get("requested_count") or 0),
        "planned_count": int(result.get("planned_count") or 0),
        "processed_count": int(result.get("processed_count") or 0),
        "deleted_count": int(result.get("deleted_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "bytes_freed": int(result.get("bytes_freed") or 0),
        "batch_count": int(result.get("batch_count") or 0),
        "limit_applied": result.get("limit_applied"),
        "limit_exceeded": bool(result.get("limit_exceeded")),
        "bounded_requested_count": int(result.get("bounded_requested_count") or 0),
        "bounded_executed_count": int(result.get("bounded_executed_count") or 0),
        "bounded_skipped_due_to_limit_count": int(result.get("bounded_skipped_due_to_limit_count") or 0),
        "reason_counts": dict(result.get("reason_counts") or {}),
        "item_reason_counts": dict(result.get("reason_counts") or {}),
        "skipped_reason_counts": dict(result.get("skipped_reason_counts") or {}),
        "failed_reason_counts": dict(result.get("failed_reason_counts") or {}),
        "sample_eligible_count": int(result.get("sample_eligible_count") or 0),
        "sample_truncated": bool(result.get("sample_truncated")),
        "retryable": bool(result.get("retryable")),
        "observability": dict(result.get("observability") or {}),
        "warnings": list(result.get("warnings") or []),
    }


def run_auto_free_space_cleanup_once(
    db: Session,
    *,
    max_candidates: int | None = None,
    operation_heartbeat: Callable[[], None] | None = None,
    **_legacy_options,
) -> dict:
    """Compatibility entry point backed by the durable Stage 4.10.2 coordinator."""
    from app.services.retention_automation import retention_page_size, run_auto_free_pressure_groups

    if operation_heartbeat is not None:
        operation_heartbeat()
    return run_auto_free_pressure_groups(
        db,
        page_size=retention_page_size(max_candidates),
    )


def run_automatic_retention_once(
    db: Session,
    *,
    max_candidates: int | None = None,
    operation_heartbeat: Callable[[], None] | None = None,
    **_legacy_options,
) -> dict:
    """Compatibility entry point backed by the durable coalesced retention signal."""
    from app.services.retention_automation import (
        advance_retention_signal,
        claim_retention_signal,
        ensure_retention_signal,
        retention_page_size,
        run_retention_signal_generation,
    )

    ensure_retention_signal(db)
    advance_retention_signal(db)
    if operation_heartbeat is not None:
        operation_heartbeat()
    handle = claim_retention_signal(
        db,
        owner_instance_id=f"retention-compat:{os.getpid()}",
    )
    if handle is None:
        return {"status": "idle", "deleted_count": 0, "bytes_freed": 0}
    return run_retention_signal_generation(
        db,
        handle,
        page_size=retention_page_size(max_candidates),
    )


def retention_diagnostics(db: Session) -> dict:
    from app.services.retention_automation import (
        auto_free_runtime_status,
        low_disk_policy_status as durable_low_disk_policy_status,
        retention_runtime_status,
    )

    cameras = db.query(Camera).order_by(Camera.id.asc()).all()
    recent = (
        db.query(RecordingSegment)
        .filter(RecordingSegment.status == SEGMENT_STATUS_DELETED)
        .order_by(RecordingSegment.deleted_at.desc().nullslast(), RecordingSegment.id.desc())
        .limit(50)
        .all()
    )
    return {
        "status": "available",
        "metadata_strategy": "status_deleted",
        "dry_run_available": True,
        "execute_available": True,
        "concurrency_guard": {
            "type": "filesystem_lock",
            "lock_name": RETENTION_LOCK_NAME,
            "cross_process": True,
            "stale_after_seconds": RETENTION_LOCK_STALE_AFTER_SECONDS,
            "stale_behavior": "fail_closed_manual_recovery_required",
        },
        "automatic_retention": retention_runtime_status(db),
        "auto_free_space_cleanup": auto_free_runtime_status(db),
        "auto_free_space_policy": durable_low_disk_policy_status(db),
        "policies": [
            {
                "camera_id": camera.id,
                "camera_name": camera.name,
                "retention_days": camera.retention_days,
                "storage_quota_gb": camera.storage_quota_gb,
            }
            for camera in cameras
        ],
        "deleted_segments_recent": [
            {
                "segment_id": segment.id,
                "camera_id": segment.camera_id,
                "relative_path": segment.relative_path,
                "deleted_at": _iso(segment.deleted_at),
                "deletion_reason": segment.deletion_reason,
                "deletion_source": segment.deletion_source,
            }
            for segment in recent
        ],
        "deleted_segments_count": db.query(RecordingSegment).filter(RecordingSegment.status == SEGMENT_STATUS_DELETED).count(),
    }
