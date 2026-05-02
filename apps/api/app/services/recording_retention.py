from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.user import User
from app.services.audit_log import create_event, redact_text
from app.services.recording_storage import is_kmvms_namespace_relative, safe_resolve_relative

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
    return datetime.utcnow()


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
        target = safe_resolve_relative(segment.relative_path)
    except ValueError as exc:
        return None, None, str(exc) or "path_escape_attempt"
    return segment.relative_path.replace("\\", "/").lstrip("/"), target, None


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
    return {
        "segment_id": segment.id if segment else None,
        "camera_id": segment.camera_id if segment else None,
        "relative_path": (segment.relative_path if segment else None),
        "action": action,
        "reason": reason,
        "error": redact_text(error) if error else None,
        "size_bytes": int(size_bytes or 0),
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
    if segment.job_id and str(segment.job_id) in active_job_ids:
        return False, "active_job", None, 0
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
    now = _now()
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
        if retention_days > 0 and segment.started_at and segment.started_at < now - timedelta(days=retention_days):
            by_segment.setdefault(segment.id, (segment, set()))[1].add("retention_days")
            policy_summary["days"][str(camera.id)] = {"retention_days": retention_days}

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


def build_retention_plan(db: Session, *, camera_id: int | None = None) -> dict:
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
    return _finish(result)


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
    create_event(
        db=db,
        actor=actor,
        category="records",
        event_type=event_type,
        severity=severity,
        message_ru=message,
        message_en=message,
        target_type="recording_segment" if segment else "recording_retention",
        target_id=segment.id if segment else None,
        target_name=segment.relative_path if segment else None,
        metadata=metadata or {},
    )


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
        return _finish(result)

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
                message=f"Recording deletion failed: {segment.relative_path}",
                segment=segment,
                metadata={"reason": "delete_failed", "relative_path": segment.relative_path, "error": str(exc)},
            )
            continue

        try:
            _mark_deleted(db, segment, actor=actor, reason=reason, source=operation)
            db.commit()
            _add_item(result, _item(segment, action="deleted", reason=reason, size_bytes=size))
            _audit(
                db,
                actor,
                event_type="recordings.deleted_segment" if operation.startswith("manual") else "retention.deleted_segment",
                message=f"Recording segment deleted: {segment.relative_path}",
                segment=segment,
                metadata={"reason": reason, "relative_path": segment.relative_path, "bytes_freed": size},
            )
        except Exception as exc:
            db.rollback()
            _add_item(result, _item(segment, action="failed", reason="metadata_update_failed", error=str(exc), size_bytes=size))
            _audit(
                db,
                actor,
                event_type="recordings.delete_failed" if operation.startswith("manual") else "retention.deletion_failed",
                severity="error",
                message=f"Recording metadata update failed after file deletion: {segment.relative_path}",
                segment=segment,
                metadata={"reason": "metadata_update_failed", "relative_path": segment.relative_path, "error": str(exc)},
            )

    _audit(
        db,
        actor,
        event_type="recordings.bulk_delete_completed" if operation.startswith("manual") else "retention.run_completed",
        message=f"{operation} completed",
        metadata={
            "operation": operation,
            "deleted_count": result["deleted_count"],
            "skipped_count": result["skipped_count"],
            "failed_count": result["failed_count"],
            "bytes_freed": result["bytes_freed"],
        },
    )
    return _finish(result)


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
) -> dict:
    try:
        with RetentionApplyLock():
            candidates, _policy_summary = _policy_candidates(db, camera_id=camera_id)
            _audit(db, actor, event_type="retention.run_started", message="Retention run started")
            return execute_segments(
                db,
                (segment for segment, _reason in candidates),
                actor=actor,
                operation="retention_run",
                reason="retention_policy",
                max_candidates=max_candidates,
                max_bytes=max_bytes,
            )
    except RuntimeError as exc:
        result = _base_result("retention_run", dry_run=False)
        _add_item(result, _item(None, action="skipped", reason=str(exc) or "concurrency_lock"))
        result["ok"] = False
        return _finish(result)


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
