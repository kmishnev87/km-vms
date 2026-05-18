from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session, object_session

from app.core.config import settings
from app.core.sanitization import redact_text
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.user import User
from app.services.audit_log import create_event
from app.services.recording_storage import is_kmvms_namespace_relative, resolve_segment_file_path, segment_relative_path
from app.services.system_settings import (
    AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT,
    AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT,
    AUTO_FREE_SPACE_WARNING_THRESHOLD_PERCENT,
    get_system_settings,
)
from app.services.timezone_contract import retention_cutoff_storage, timezone_context, utc_now_storage

OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
SEGMENT_STATUS_FINALIZED = "finalized"
SEGMENT_STATUS_DELETED = "deleted"
RETENTION_LOCK_NAME = ".kmvms_retention_apply.lock"
RETENTION_LOCK_STALE_AFTER_SECONDS = 60 * 60
ACTIVE_JOB_STATES = {"starting", "recording", "stopping", "restarting"}
DEFAULT_MAX_CANDIDATES = 100
DEFAULT_MAX_BYTES = 10 * 1024 * 1024 * 1024
HARD_MAX_CANDIDATES = 1000
HARD_MAX_BYTES = 100 * 1024 * 1024 * 1024
AUTO_RETENTION_DEFAULT_MAX_CANDIDATES = 25
AUTO_RETENTION_DEFAULT_MAX_BYTES = 1 * 1024 * 1024 * 1024
AUTO_RETENTION_STATE_LOCK = threading.Lock()
AUTO_RETENTION_STATE: dict = {
    "enabled": True,
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_status": "never_run",
    "last_error": None,
    "last_summary": None,
    "run_count": 0,
}
AUTO_FREE_SPACE_STATE_LOCK = threading.Lock()
AUTO_FREE_SPACE_STATE: dict = {
    "enabled": False,
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_status": "never_run",
    "last_trigger": None,
    "last_error": None,
    "last_summary": None,
    "run_count": 0,
}
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
    try:
        db = object_session(segment)
        if db is None:
            return None, None, "db_session_missing"
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


def _base_result(operation: str, *, dry_run: bool) -> dict:
    started_at = _now()
    return {
        "ok": True,
        "operation": operation,
        "dry_run": dry_run,
        "requested_count": 0,
        "planned_count": 0,
        "deleted_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "not_found_count": 0,
        "bytes_freed": 0,
        "candidates_count": 0,
        "limit_applied": None,
        "limit_exceeded": False,
        "started_at": _iso(started_at),
        "finished_at": None,
        "generated_at": _iso(started_at),
        "items": [],
        "per_camera": {},
        "warnings": [],
    }


def _finish(result: dict) -> dict:
    result["finished_at"] = _iso(_now())
    result["ok"] = result.get("failed_count", 0) == 0 and not result.get("limit_exceeded", False)
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


def _add_item(result: dict, item: dict) -> None:
    result["items"].append(item)
    camera_key = str(item.get("camera_id") or "unknown")
    row = result["per_camera"].setdefault(
        camera_key,
        {"camera_id": item.get("camera_id"), "deleted_count": 0, "skipped_count": 0, "failed_count": 0, "bytes_freed": 0},
    )
    action = item.get("action")
    if action == "deleted":
        result["deleted_count"] += 1
        result["bytes_freed"] += int(item.get("size_bytes") or 0)
        row["deleted_count"] += 1
        row["bytes_freed"] += int(item.get("size_bytes") or 0)
    elif action == "failed":
        result["failed_count"] += 1
        row["failed_count"] += 1
    else:
        result["skipped_count"] += 1
        row["skipped_count"] += 1
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


def _free_percent(capacity: dict | None) -> float | None:
    capacity = capacity or {}
    total = capacity.get("total_bytes")
    free = capacity.get("free_bytes")
    if not total or free is None:
        return None
    try:
        return round((int(free) / int(total)) * 100, 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def low_disk_policy_status(db: Session, storage_summary: dict | None = None) -> dict:
    if storage_summary is None:
        from app.services.storage_monitoring import build_storage_monitoring_summary

        storage_summary = build_storage_monitoring_summary(db, include_namespace_observations=False, write_audit=False)
    system = get_system_settings(db)
    capacity = storage_summary.get("capacity") or {}
    free_percent = _free_percent(capacity)
    cleanup_enabled = bool(getattr(system, "auto_free_space_cleanup_enabled", False))
    if free_percent is None:
        state = "capacity_unknown"
    elif free_percent < AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT:
        state = "critical"
    elif free_percent < AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT:
        state = "cleanup_threshold"
    elif free_percent < AUTO_FREE_SPACE_WARNING_THRESHOLD_PERCENT:
        state = "warning"
    else:
        state = "ok"
    return {
        "state": state,
        "free_percent": free_percent,
        "free_bytes": capacity.get("free_bytes"),
        "total_bytes": capacity.get("total_bytes"),
        "warning_threshold_percent": AUTO_FREE_SPACE_WARNING_THRESHOLD_PERCENT,
        "cleanup_threshold_percent": AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT,
        "critical_threshold_percent": AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT,
        "auto_free_space_cleanup_enabled": cleanup_enabled,
        "cleanup_allowed": bool(cleanup_enabled and free_percent is not None and free_percent < AUTO_FREE_SPACE_CLEANUP_THRESHOLD_PERCENT),
        "critical_recording_suspend_required": bool(free_percent is not None and free_percent < AUTO_FREE_SPACE_CRITICAL_THRESHOLD_PERCENT),
        "recording_suspended_by_low_disk": bool(getattr(system, "recording_suspended_by_low_disk", False)),
    }


def update_low_disk_recording_suspend(
    db: Session,
    *,
    should_suspend: bool,
    actor: User | None = None,
    reason: str,
    policy: dict,
) -> bool:
    system = get_system_settings(db)
    current = bool(getattr(system, "recording_suspended_by_low_disk", False))
    if current == should_suspend:
        return False
    system.recording_suspended_by_low_disk = should_suspend
    system.updated_at = _now()
    db.add(system)
    db.commit()
    create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type="storage.critical_low_disk_recording_suspended" if should_suspend else "storage.critical_low_disk_recording_resumed",
        severity="error" if should_suspend else "info",
        message_ru="Recording suspended by critical low disk protection" if should_suspend else "Recording resumed after critical low disk protection",
        message_en="Recording suspended by critical low disk protection" if should_suspend else "Recording resumed after critical low disk protection",
        target_type="storage",
        metadata={
            "reason": reason,
            "free_percent": policy.get("free_percent"),
            "free_bytes": policy.get("free_bytes"),
            "total_bytes": policy.get("total_bytes"),
            "critical_threshold_percent": policy.get("critical_threshold_percent"),
            "auto_free_space_cleanup_enabled": policy.get("auto_free_space_cleanup_enabled"),
        },
    )
    return True


def apply_critical_low_disk_protection(db: Session, storage_summary: dict | None = None, *, actor: User | None = None) -> dict:
    policy = low_disk_policy_status(db, storage_summary)
    if policy["free_percent"] is None:
        policy["recording_suspend_changed"] = False
        return policy
    should_suspend = bool(policy["critical_recording_suspend_required"])
    changed = update_low_disk_recording_suspend(
        db,
        should_suspend=should_suspend,
        actor=actor,
        reason="critical_low_disk" if should_suspend else "free_space_recovered",
        policy=policy,
    )
    policy["recording_suspend_changed"] = changed
    policy["recording_suspended_by_low_disk"] = should_suspend
    return policy


def validate_segment_for_deletion(
    segment: RecordingSegment,
    *,
    active_job_ids: set[str],
    require_file: bool = True,
) -> tuple[bool, str, Path | None, int]:
    if segment.ownership != OWNERSHIP_KM_VMS:
        return False, "unowned", None, 0
    if segment.source != RECORDER_SOURCE:
        return False, "foreign_source", None, 0
    if segment.status == SEGMENT_STATUS_DELETED:
        return False, "already_deleted", None, 0
    if segment.status != SEGMENT_STATUS_FINALIZED:
        return False, "not_finalized", None, 0
    if segment.integrity_status in PROBLEM_INTEGRITY_STATUSES:
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
        if path_error or not rel_path or file_path is None or not is_kmvms_namespace_relative(rel_path):
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
            result["items"].append(item)
            camera_key = str(segment.camera_id)
            row = result["per_camera"].setdefault(
                camera_key,
                {"camera_id": segment.camera_id, "deleted_count": 0, "skipped_count": 0, "failed_count": 0, "bytes_freed": 0},
            )
            row["bytes_freed"] += size
            result["bytes_freed"] += size
        else:
            _add_reason_count(result, reason)
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
) -> dict:
    result = _base_result(operation, dry_run=False)
    max_candidates = min(int(max_candidates or DEFAULT_MAX_CANDIDATES), HARD_MAX_CANDIDATES)
    max_bytes = min(int(max_bytes or DEFAULT_MAX_BYTES), HARD_MAX_BYTES)
    result["limit_applied"] = {"max_candidates": max_candidates, "max_bytes": max_bytes}
    active_job_ids = _active_job_ids(db)
    planned: list[tuple[RecordingSegment, Path, int]] = []

    for segment in segments:
        result["requested_count"] += 1
        ok, item_reason, file_path, size = validate_segment_for_deletion(segment, active_job_ids=active_job_ids)
        if not ok or file_path is None:
            _add_item(result, _item(segment, action="skipped", reason=item_reason, size_bytes=size))
            continue
        planned.append((segment, file_path, size))

    planned_bytes = sum(size for _segment, _path, size in planned)
    if len(planned) > max_candidates or planned_bytes > max_bytes:
        result["limit_exceeded"] = True
        result["planned_count"] = len(planned)
        result["candidates_count"] = len(planned)
        result["warnings"].append("limit_exceeded")
        for segment, _path, size in planned:
            _add_item(result, _item(segment, action="skipped", reason="limit_exceeded", size_bytes=size))
        finished = _finish(result)
        if not operation.startswith("manual") and not operation.startswith("retention_auto"):
            _audit(
                db,
                actor,
                event_type="retention.apply_completed",
                severity="warning",
                message=f"{operation} completed with retention limits",
                metadata=_retention_summary(finished),
            )
        return finished

    result["planned_count"] = len(planned)
    result["candidates_count"] = len(planned)
    for segment, file_path, size in planned:
        try:
            file_path.unlink()
        except OSError as exc:
            db.rollback()
            _add_item(result, _item(segment, action="failed", reason="delete_failed", error=str(exc), size_bytes=size))
            _audit(
                db,
                actor,
                event_type="recordings.delete_failed" if operation.startswith("manual") else "retention.deletion_failed",
                severity="error",
                message=f"Recording deletion failed for segment {segment.id}",
                segment=segment,
                metadata={**_segment_audit_ref(segment), "reason": "delete_failed", "error": str(exc)},
            )
            continue

        try:
            _mark_deleted(db, segment, actor=actor, reason=reason, source=operation)
            db.commit()
            _add_item(result, _item(segment, action="deleted", reason=reason, size_bytes=size))
            if operation.startswith("manual"):
                _audit(
                    db,
                    actor,
                    event_type="recordings.deleted_segment",
                    message=f"Recording segment deleted: {segment.id}",
                    segment=segment,
                    metadata={**_segment_audit_ref(segment), "reason": reason, "bytes_freed": size},
                )
        except Exception as exc:
            recovered = _recover_deleted_segment_metadata(
                db,
                segment,
                actor=actor,
                reason=reason,
                source=operation,
                error=str(exc),
            )
            failure_reason = "metadata_update_failed_recovered" if recovered else "metadata_update_failed"
            _add_item(result, _item(segment, action="failed", reason=failure_reason, error=str(exc), size_bytes=size))
            _audit(
                db,
                actor,
                event_type="recordings.delete_failed" if operation.startswith("manual") else "retention.deletion_failed",
                severity="error",
                message=f"Recording metadata update failed after file deletion for segment {segment.id}",
                segment=segment,
                metadata={**_segment_audit_ref(segment), "reason": failure_reason, "error": str(exc)},
            )

    finished = _finish(result)
    if operation.startswith("manual"):
        _audit(
            db,
            actor,
            event_type="recordings.bulk_delete_completed",
            message=f"{operation} completed",
            metadata={
                "operation": operation,
                "deleted_count": finished["deleted_count"],
                "skipped_count": finished["skipped_count"],
                "failed_count": finished["failed_count"],
                "bytes_freed": finished["bytes_freed"],
            },
        )
    elif not operation.startswith("retention_auto"):
        _audit(
            db,
            actor,
            event_type="retention.apply_completed",
            severity="error" if finished.get("failed_count") else "info",
            message=f"{operation} completed",
            metadata=_retention_summary(finished),
        )
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
        ok, item_reason, _file_path, size = validate_segment_for_deletion(segment, active_job_ids=active_job_ids)
        if ok:
            result["planned_count"] += 1
            result["candidates_count"] += 1
            result["bytes_freed"] += int(size or 0)
            result["estimated_freed_bytes"] = result["bytes_freed"]
            result["items"].append(_item(segment, action="candidate", reason=reason, size_bytes=size))
            camera_key = str(segment.camera_id)
            row = result["per_camera"].setdefault(
                camera_key,
                {"camera_id": segment.camera_id, "deleted_count": 0, "skipped_count": 0, "failed_count": 0, "bytes_freed": 0},
            )
            row["bytes_freed"] += int(size or 0)
        else:
            _add_reason_count(result, item_reason)
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
    item_reason_counts: dict[str, int] = {}
    skipped_reason_counts: dict[str, int] = {}
    failed_reason_counts: dict[str, int] = {}
    for item in result.get("items") or []:
        reason = str(item.get("reason") or "unknown")
        action = str(item.get("action") or "unknown")
        item_reason_counts[reason] = int(item_reason_counts.get(reason) or 0) + 1
        if action == "skipped":
            skipped_reason_counts[reason] = int(skipped_reason_counts.get(reason) or 0) + 1
        if action == "failed":
            failed_reason_counts[reason] = int(failed_reason_counts.get(reason) or 0) + 1
    return {
        "ok": bool(result.get("ok")),
        "operation": result.get("operation"),
        "requested_count": int(result.get("requested_count") or 0),
        "planned_count": int(result.get("planned_count") or 0),
        "deleted_count": int(result.get("deleted_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "bytes_freed": int(result.get("bytes_freed") or 0),
        "limit_applied": result.get("limit_applied"),
        "limit_exceeded": bool(result.get("limit_exceeded")),
        "bounded_requested_count": int(result.get("bounded_requested_count") or 0),
        "bounded_executed_count": int(result.get("bounded_executed_count") or 0),
        "bounded_skipped_due_to_limit_count": int(result.get("bounded_skipped_due_to_limit_count") or 0),
        "oversized_single_segment_progress": bool(result.get("oversized_single_segment_progress")),
        "reason_counts": dict(result.get("reason_counts") or {}),
        "item_reason_counts": item_reason_counts,
        "skipped_reason_counts": skipped_reason_counts,
        "failed_reason_counts": failed_reason_counts,
        "observability": dict(result.get("observability") or {}),
        "warnings": list(result.get("warnings") or []),
    }


def automatic_retention_status() -> dict:
    with AUTO_RETENTION_STATE_LOCK:
        return dict(AUTO_RETENTION_STATE)


def auto_free_space_status() -> dict:
    with AUTO_FREE_SPACE_STATE_LOCK:
        return dict(AUTO_FREE_SPACE_STATE)


def _set_automatic_retention_state(**updates) -> None:
    with AUTO_RETENTION_STATE_LOCK:
        AUTO_RETENTION_STATE.update(updates)


def _automatic_retention_bounded_subset(
    db: Session,
    *,
    max_candidates: int,
    max_bytes: int,
) -> tuple[list[RecordingSegment], dict]:
    candidates, _policy_summary = _policy_candidates(db)
    active_job_ids = _active_job_ids(db)
    executable: list[tuple[RecordingSegment, int]] = []
    skipped_items: list[dict] = []
    for segment, _policy_reason in candidates:
        ok, item_reason, file_path, size = validate_segment_for_deletion(segment, active_job_ids=active_job_ids)
        if ok and file_path is not None:
            executable.append((segment, size))
        else:
            skipped_items.append(_item(segment, action="skipped", reason=item_reason, size_bytes=size))

    selected: list[RecordingSegment] = []
    selected_bytes = 0
    oversized_single_segment_progress = False
    for segment, size in executable:
        if len(selected) >= max_candidates:
            break
        next_bytes = selected_bytes + int(size or 0)
        if next_bytes <= max_bytes:
            selected.append(segment)
            selected_bytes = next_bytes
            continue
        if not selected:
            selected.append(segment)
            selected_bytes = next_bytes
            oversized_single_segment_progress = True
        break

    metadata = {
        "bounded_requested_count": len(executable),
        "bounded_executed_count": len(selected),
        "bounded_skipped_due_to_limit_count": max(0, len(executable) - len(selected)),
        "bounded_selected_bytes": selected_bytes,
        "oversized_single_segment_progress": oversized_single_segment_progress,
        "bounded_safety_skipped_items": skipped_items,
    }
    return selected, metadata


def _auto_free_space_bounded_subset(
    db: Session,
    *,
    max_candidates: int,
    max_bytes: int,
    target_bytes: int | None,
) -> tuple[list[RecordingSegment], dict]:
    active_job_ids = _active_job_ids(db)
    executable: list[tuple[RecordingSegment, int]] = []
    skipped_items: list[dict] = []
    for segment in _eligible_segments_query(db).all():
        ok, item_reason, file_path, size = validate_segment_for_deletion(segment, active_job_ids=active_job_ids)
        if ok and file_path is not None:
            executable.append((segment, int(size or 0)))
        else:
            skipped_items.append(_item(segment, action="skipped", reason=item_reason, size_bytes=size))

    selected: list[RecordingSegment] = []
    selected_bytes = 0
    oversized_single_segment_progress = False
    target_bytes = max(0, int(target_bytes or 0))
    for segment, size in executable:
        if len(selected) >= max_candidates:
            break
        next_bytes = selected_bytes + int(size or 0)
        if next_bytes <= max_bytes:
            selected.append(segment)
            selected_bytes = next_bytes
        elif not selected:
            selected.append(segment)
            selected_bytes = next_bytes
            oversized_single_segment_progress = True
        else:
            break
        if target_bytes and selected_bytes >= target_bytes:
            break

    metadata = {
        "bounded_requested_count": len(executable),
        "bounded_executed_count": len(selected),
        "bounded_skipped_due_to_limit_count": max(0, len(executable) - len(selected)),
        "bounded_selected_bytes": selected_bytes,
        "target_bytes": target_bytes,
        "oversized_single_segment_progress": oversized_single_segment_progress,
        "bounded_safety_skipped_items": skipped_items,
    }
    return selected, metadata


def _set_auto_free_space_state(**updates) -> None:
    with AUTO_FREE_SPACE_STATE_LOCK:
        AUTO_FREE_SPACE_STATE.update(updates)


def _target_bytes_for_cleanup(policy: dict) -> int | None:
    total = policy.get("total_bytes")
    free = policy.get("free_bytes")
    if not total or free is None:
        return None
    try:
        threshold_bytes = int((float(policy["cleanup_threshold_percent"]) / 100.0) * int(total))
        return max(0, threshold_bytes - int(free))
    except (TypeError, ValueError):
        return None


def run_auto_free_space_cleanup_once(
    db: Session,
    *,
    storage_summary: dict | None = None,
    actor: User | None = None,
    max_candidates: int | None = None,
    max_bytes: int | None = None,
) -> dict:
    max_candidates = min(int(max_candidates or AUTO_RETENTION_DEFAULT_MAX_CANDIDATES), HARD_MAX_CANDIDATES)
    max_bytes = min(int(max_bytes or AUTO_RETENTION_DEFAULT_MAX_BYTES), HARD_MAX_BYTES)
    if storage_summary is None:
        from app.services.storage_monitoring import build_storage_monitoring_summary

        storage_summary = build_storage_monitoring_summary(db, include_namespace_observations=False, write_audit=False)
    policy = apply_critical_low_disk_protection(db, storage_summary, actor=actor)
    trigger = "critical_low_disk" if policy["state"] == "critical" else "low_disk"
    started_at = _now()

    with AUTO_FREE_SPACE_STATE_LOCK:
        if AUTO_FREE_SPACE_STATE.get("running"):
            result = _base_result("retention_auto_free_space", dry_run=False)
            _add_item(result, _item(None, action="skipped", reason="auto_free_space_already_running"))
            result["ok"] = False
            result["low_disk_policy"] = policy
            result = _finish(result)
            result["ok"] = False
            AUTO_FREE_SPACE_STATE["last_status"] = "skipped_concurrent"
            AUTO_FREE_SPACE_STATE["last_summary"] = _retention_summary(result)
            return result
        AUTO_FREE_SPACE_STATE.update(
            {
                "enabled": bool(policy["auto_free_space_cleanup_enabled"]),
                "running": True,
                "last_started_at": _iso(started_at),
                "last_trigger": trigger,
                "last_error": None,
            }
        )

    result = _base_result("retention_auto_free_space", dry_run=False)
    result["low_disk_policy"] = policy
    result["limit_applied"] = {"max_candidates": max_candidates, "max_bytes": max_bytes}
    try:
        if policy["free_percent"] is None:
            _add_item(result, _item(None, action="skipped", reason="capacity_unknown"))
            result["ok"] = False
            return_result = _finish(result)
        elif not policy["auto_free_space_cleanup_enabled"]:
            _add_item(result, _item(None, action="skipped", reason="auto_free_space_cleanup_disabled"))
            return_result = _finish(result)
        elif not policy["cleanup_allowed"]:
            _add_item(result, _item(None, action="skipped", reason="cleanup_threshold_not_reached"))
            return_result = _finish(result)
        else:
            _audit(
                db,
                actor,
                event_type="retention.auto_free_space_started",
                severity="error" if trigger == "critical_low_disk" else "warning",
                message="Automatic free-space cleanup started",
                metadata={
                    "trigger": trigger,
                    "free_percent": policy.get("free_percent"),
                    "free_bytes": policy.get("free_bytes"),
                    "total_bytes": policy.get("total_bytes"),
                    "cleanup_threshold_percent": policy.get("cleanup_threshold_percent"),
                    "critical_threshold_percent": policy.get("critical_threshold_percent"),
                    "max_candidates": max_candidates,
                    "max_bytes": max_bytes,
                },
            )
            with RetentionApplyLock():
                selected, bounded = _auto_free_space_bounded_subset(
                    db,
                    max_candidates=max_candidates,
                    max_bytes=max_bytes,
                    target_bytes=_target_bytes_for_cleanup(policy),
                )
                if not selected:
                    _add_item(result, _item(None, action="skipped", reason="no_safe_cleanup_candidates"))
                    return_result = _finish(result)
                else:
                    effective_max_bytes = max_bytes
                    if bounded["oversized_single_segment_progress"]:
                        effective_max_bytes = max(max_bytes, int(bounded["bounded_selected_bytes"] or 0))
                    return_result = execute_segments(
                        db,
                        selected,
                        actor=actor,
                        operation="retention_auto_free_space",
                        reason=trigger,
                        max_candidates=max_candidates,
                        max_bytes=effective_max_bytes,
                    )
                    safety_skipped_items = bounded.pop("bounded_safety_skipped_items", [])
                    return_result.update(bounded)
                    return_result["low_disk_policy"] = policy
                    for item in safety_skipped_items:
                        _add_item(return_result, item)
                    if bounded["oversized_single_segment_progress"]:
                        return_result["warnings"].append("oversized_single_segment_progress")
                    if bounded["bounded_skipped_due_to_limit_count"]:
                        return_result["warnings"].append("bounded_progress_remaining_candidates")
                    return_result["limit_applied"] = {
                        "max_candidates": max_candidates,
                        "max_bytes": max_bytes,
                        "effective_max_bytes": effective_max_bytes,
                    }
            _audit(
                db,
                actor,
                event_type="retention.auto_free_space_completed",
                severity="error" if return_result.get("failed_count") else ("warning" if trigger == "low_disk" else "error"),
                message="Automatic free-space cleanup completed",
                metadata={**_retention_summary(return_result), "trigger": trigger, "low_disk_policy": policy},
            )

        summary = _retention_summary(return_result)
        _set_auto_free_space_state(
            enabled=bool(policy["auto_free_space_cleanup_enabled"]),
            running=False,
            last_finished_at=_iso(_now()),
            last_status="ok" if return_result.get("ok") else "completed_with_warnings",
            last_error=None,
            last_summary=summary,
            last_trigger=trigger,
            run_count=int(auto_free_space_status().get("run_count") or 0) + 1,
        )
        return return_result
    except Exception as exc:
        db.rollback()
        error = redact_text(str(exc))[:1000]
        result = _base_result("retention_auto_free_space", dry_run=False)
        result["low_disk_policy"] = policy
        _add_item(result, _item(None, action="failed", reason="auto_free_space_exception", error=error))
        result["ok"] = False
        result = _finish(result)
        _audit(
            db,
            actor,
            event_type="retention.auto_free_space_failed",
            severity="error",
            message="Automatic free-space cleanup failed",
            metadata={"error": error, "trigger": trigger},
        )
        _set_auto_free_space_state(
            enabled=bool(policy["auto_free_space_cleanup_enabled"]),
            running=False,
            last_finished_at=_iso(_now()),
            last_status="failed",
            last_error=error,
            last_summary=_retention_summary(result),
            last_trigger=trigger,
            run_count=int(auto_free_space_status().get("run_count") or 0) + 1,
        )
        return result


def run_automatic_retention_once(
    db: Session,
    *,
    max_candidates: int | None = None,
    max_bytes: int | None = None,
) -> dict:
    max_candidates = min(int(max_candidates or AUTO_RETENTION_DEFAULT_MAX_CANDIDATES), HARD_MAX_CANDIDATES)
    max_bytes = min(int(max_bytes or AUTO_RETENTION_DEFAULT_MAX_BYTES), HARD_MAX_BYTES)
    started_at = _now()
    with AUTO_RETENTION_STATE_LOCK:
        if AUTO_RETENTION_STATE.get("running"):
            result = _base_result("retention_auto_run", dry_run=False)
            _add_item(result, _item(None, action="skipped", reason="automatic_retention_already_running"))
            result["ok"] = False
            result = _finish(result)
            result["ok"] = False
            AUTO_RETENTION_STATE["last_status"] = "skipped_concurrent"
            AUTO_RETENTION_STATE["last_summary"] = _retention_summary(result)
            return result
        AUTO_RETENTION_STATE.update(
            {
                "enabled": True,
                "running": True,
                "last_started_at": _iso(started_at),
                "last_error": None,
            }
        )

    try:
        _audit(
            db,
            None,
            event_type="retention.auto_run_started",
            message="Automatic retention run started",
            metadata={"max_candidates": max_candidates, "max_bytes": max_bytes},
        )
        try:
            with RetentionApplyLock():
                selected, bounded = _automatic_retention_bounded_subset(
                    db,
                    max_candidates=max_candidates,
                    max_bytes=max_bytes,
                )
                effective_max_bytes = max_bytes
                if bounded["oversized_single_segment_progress"]:
                    effective_max_bytes = max(max_bytes, int(bounded["bounded_selected_bytes"] or 0))
                result = execute_segments(
                    db,
                    selected,
                    actor=None,
                    operation="retention_auto_run",
                    reason="automatic_retention_policy",
                    max_candidates=max_candidates,
                    max_bytes=effective_max_bytes,
                )
                safety_skipped_items = bounded.pop("bounded_safety_skipped_items", [])
                result.update(bounded)
                for item in safety_skipped_items:
                    _add_item(result, item)
                if bounded["oversized_single_segment_progress"]:
                    result["warnings"].append("oversized_single_segment_progress")
                if bounded["bounded_skipped_due_to_limit_count"]:
                    result["warnings"].append("bounded_progress_remaining_candidates")
                result["limit_applied"] = {
                    "max_candidates": max_candidates,
                    "max_bytes": max_bytes,
                    "effective_max_bytes": effective_max_bytes,
                }
        except RuntimeError as exc:
            result = _base_result("retention_auto_run", dry_run=False)
            _add_item(result, _item(None, action="skipped", reason=str(exc) or "concurrency_lock"))
            result["ok"] = False
            result = _finish(result)
            result["ok"] = False
        summary = _retention_summary(result)
        _audit(
            db,
            None,
            event_type="retention.auto_run_completed",
            severity="error" if result.get("failed_count") else "info",
            message="Automatic retention run completed",
            metadata=summary,
        )
        _set_automatic_retention_state(
            running=False,
            last_finished_at=_iso(_now()),
            last_status="ok" if result.get("ok") else "completed_with_warnings",
            last_error=None,
            last_summary=summary,
            run_count=int(automatic_retention_status().get("run_count") or 0) + 1,
        )
        return result
    except Exception as exc:
        db.rollback()
        error = redact_text(str(exc))[:1000]
        result = _base_result("retention_auto_run", dry_run=False)
        _add_item(result, _item(None, action="failed", reason="automatic_retention_exception", error=error))
        result["ok"] = False
        result = _finish(result)
        _audit(
            db,
            None,
            event_type="retention.auto_run_failed",
            severity="error",
            message="Automatic retention run failed",
            metadata={"error": error},
        )
        _set_automatic_retention_state(
            running=False,
            last_finished_at=_iso(_now()),
            last_status="failed",
            last_error=error,
            last_summary=_retention_summary(result),
            run_count=int(automatic_retention_status().get("run_count") or 0) + 1,
        )
        return result


def retention_diagnostics(db: Session) -> dict:
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
        "automatic_retention": automatic_retention_status(),
        "auto_free_space_cleanup": auto_free_space_status(),
        "auto_free_space_policy": low_disk_policy_status(db),
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
