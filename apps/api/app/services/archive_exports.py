from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.recording import ArchiveExportJob, RecordingSegment
from app.models.user import User
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.recording_storage import resolve_segment_file_path

OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
SEGMENT_STATUS_FINALIZED = "finalized"
EXPORT_STATUS_QUEUED = "queued"
EXPORT_STATUS_RUNNING = "running"
EXPORT_STATUS_DONE = "done"
EXPORT_STATUS_FAILED = "failed"
EXPORT_STATUS_EXPIRED = "expired"
EXPORT_STATUSES = frozenset(
    {
        EXPORT_STATUS_QUEUED,
        EXPORT_STATUS_RUNNING,
        EXPORT_STATUS_DONE,
        EXPORT_STATUS_FAILED,
        EXPORT_STATUS_EXPIRED,
    }
)

MAX_EXPORT_DURATION_SECONDS = 30 * 60
MAX_SOURCE_SEGMENTS = 120
MAX_ESTIMATED_SOURCE_BYTES = 4 * 1024 * 1024 * 1024
MAX_ACTIVE_JOBS_PER_USER = 3
MAX_NOTE_LENGTH = 500
MAX_TITLE_LENGTH = 200
EXPORT_JOB_EXPIRES_AFTER = timedelta(hours=24)
FUTURE_RANGE_TOLERANCE = timedelta(hours=24)
GAP_TOLERANCE_SECONDS = 2
ALLOWED_FORMAT_HINTS = frozenset({"mkv", "mp4"})
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
TECHNICAL_DELETED_CAMERA_RE = re.compile(r"__deleted_\d+_\d+$")


@dataclass(frozen=True)
class ExportPreflight:
    segments: list[RecordingSegment]
    estimated_source_bytes: int
    gap_warnings: list[dict[str, Any]]


def _safe_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_export_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise _safe_error(422, f"Invalid {field_name}")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _segment_end(segment: RecordingSegment) -> datetime | None:
    if segment.ended_at and segment.ended_at > segment.started_at:
        return segment.ended_at
    if segment.finalized_at and segment.finalized_at > segment.started_at:
        return segment.finalized_at
    if segment.duration_sec and segment.duration_sec > 0:
        return segment.started_at + timedelta(seconds=int(segment.duration_sec))
    return None


def _segment_interval(segment: RecordingSegment) -> tuple[datetime, datetime] | None:
    if not segment.started_at:
        return None
    ended = _segment_end(segment)
    if not ended or ended <= segment.started_at:
        return None
    return segment.started_at, ended


def safe_camera_label(camera: Camera) -> str:
    text = str(getattr(camera, "name", "") or "").strip()
    if not text or TECHNICAL_DELETED_CAMERA_RE.search(text):
        return f"Camera {camera.id}"
    return text[:255]


def clean_optional_text(value: str | None, *, max_length: int, field_name: str) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_length:
        raise _safe_error(422, f"{field_name} is too long")
    return text or None


def finalized_export_segments_query(db: Session):
    return db.query(RecordingSegment).filter(
        RecordingSegment.ownership == OWNERSHIP_KM_VMS,
        RecordingSegment.source == RECORDER_SOURCE,
        RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
        RecordingSegment.relative_path.isnot(None),
        RecordingSegment.deleted_at.is_(None),
        or_(
            RecordingSegment.integrity_status.is_(None),
            ~RecordingSegment.integrity_status.in_(PROBLEM_INTEGRITY_STATUSES),
        ),
    )


def _gap_warnings(intervals: list[tuple[datetime, datetime]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    previous_end: datetime | None = None
    for start_dt, end_dt in sorted(intervals, key=lambda item: item[0]):
        if previous_end is not None:
            gap = int((start_dt - previous_end).total_seconds())
            if gap > GAP_TOLERANCE_SECONDS:
                warnings.append({"type": "source_gap", "gap_seconds": gap})
        previous_end = max(previous_end or end_dt, end_dt)
    return warnings[:20]


def preflight_source_segments(
    db: Session,
    *,
    camera_id: int,
    start_ts: datetime,
    end_ts: datetime,
) -> ExportPreflight:
    candidates = (
        finalized_export_segments_query(db)
        .filter(RecordingSegment.camera_id == camera_id, RecordingSegment.started_at < end_ts)
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
        .all()
    )

    selected: list[RecordingSegment] = []
    intervals: list[tuple[datetime, datetime]] = []
    for segment in candidates:
        interval = _segment_interval(segment)
        if not interval:
            continue
        segment_start, segment_end = interval
        if segment_end <= start_ts or segment_start >= end_ts:
            continue
        selected.append(segment)
        intervals.append((max(segment_start, start_ts), min(segment_end, end_ts)))

    if not selected:
        raise _safe_error(404, "No exportable source segments found")
    if len(selected) > MAX_SOURCE_SEGMENTS:
        raise _safe_error(413, "Too many source segments for export")

    estimated_bytes = 0
    for segment in selected:
        try:
            file_path = resolve_segment_file_path(db, segment, require_exists=True)
        except (FileNotFoundError, ValueError):
            raise _safe_error(409, "Source recording file unavailable")
        try:
            file_size = int(Path(file_path).stat().st_size)
        except OSError:
            raise _safe_error(409, "Source recording file unavailable")
        estimated_bytes += max(file_size, int(segment.size_bytes or 0))
        if estimated_bytes > MAX_ESTIMATED_SOURCE_BYTES:
            raise _safe_error(413, "Estimated export source size is too large")

    return ExportPreflight(
        segments=selected,
        estimated_source_bytes=estimated_bytes,
        gap_warnings=_gap_warnings(intervals),
    )


def validate_export_range(start_ts: datetime, end_ts: datetime) -> tuple[datetime, datetime, int]:
    start_dt = normalize_export_datetime(start_ts, "start_ts")
    end_dt = normalize_export_datetime(end_ts, "end_ts")
    if end_dt <= start_dt:
        raise _safe_error(422, "end_ts must be greater than start_ts")
    duration = int((end_dt - start_dt).total_seconds())
    if duration > MAX_EXPORT_DURATION_SECONDS:
        raise _safe_error(413, "Export range is too long")
    if start_dt > _utcnow_naive() + FUTURE_RANGE_TOLERANCE:
        raise _safe_error(422, "Export range is too far in the future")
    return start_dt, end_dt, duration


def assert_active_job_limit(db: Session, actor: User) -> None:
    active_count = (
        db.query(ArchiveExportJob)
        .filter(
            ArchiveExportJob.actor_user_id == getattr(actor, "id", None),
            ArchiveExportJob.status.in_((EXPORT_STATUS_QUEUED, EXPORT_STATUS_RUNNING)),
        )
        .count()
    )
    if active_count >= MAX_ACTIVE_JOBS_PER_USER:
        raise _safe_error(429, "Too many active export jobs")


def create_archive_export_job(
    db: Session,
    *,
    actor: User,
    camera_id: int,
    start_ts: datetime,
    end_ts: datetime,
    title: str | None = None,
    reason: str | None = None,
    note: str | None = None,
    format_hint: str | None = None,
    request=None,
) -> ArchiveExportJob:
    start_dt, end_dt, duration = validate_export_range(start_ts, end_ts)
    title_text = clean_optional_text(title, max_length=MAX_TITLE_LENGTH, field_name="title")
    reason_text = clean_optional_text(reason or note, max_length=MAX_NOTE_LENGTH, field_name="reason")
    hint = str(format_hint or "").strip().lower() or None
    if hint and hint not in ALLOWED_FORMAT_HINTS:
        raise _safe_error(422, "Unsupported format_hint")

    camera = db.query(Camera).filter(Camera.id == int(camera_id), Camera.deleted_at.is_(None)).first()
    if not camera:
        raise _safe_error(404, "Camera not found")

    assert_active_job_limit(db, actor)
    preflight = preflight_source_segments(db, camera_id=camera.id, start_ts=start_dt, end_ts=end_dt)
    now = _utcnow_naive()
    job = ArchiveExportJob(
        id=str(uuid.uuid4()),
        actor_user_id=getattr(actor, "id", None),
        camera_id=camera.id,
        camera_label_snapshot=safe_camera_label(camera),
        start_ts=start_dt,
        end_ts=end_dt,
        duration_seconds=duration,
        status=EXPORT_STATUS_QUEUED,
        progress_percent=0,
        title=title_text,
        reason=reason_text,
        format_hint=hint,
        source_segment_ids=[segment.id for segment in preflight.segments],
        source_segment_count=len(preflight.segments),
        estimated_source_bytes=preflight.estimated_source_bytes,
        gap_warnings=preflight.gap_warnings,
        created_at=now,
        updated_at=now,
        expires_at=now + EXPORT_JOB_EXPIRES_AFTER,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    create_event(
        db=db,
        actor=actor,
        category="archive",
        event_type="archive_export_requested",
        severity="info",
        message_ru="Archive export requested",
        message_en="Archive export requested",
        target_type="archive_export_job",
        target_id=job.id,
        target_name=job.camera_label_snapshot,
        metadata={
            "export_job_id": job.id,
            "camera_id": job.camera_id,
            "camera_label": job.camera_label_snapshot,
            "start_ts": job.start_ts,
            "end_ts": job.end_ts,
            "duration_seconds": job.duration_seconds,
            "source_segment_count": job.source_segment_count,
            "estimated_source_bytes": job.estimated_source_bytes,
            "status": job.status,
            "gap_warning_count": len(job.gap_warnings or []),
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return job


def serialize_archive_export_job(job: ArchiveExportJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status if job.status in EXPORT_STATUSES else EXPORT_STATUS_FAILED,
        "camera": {
            "id": job.camera_id,
            "label": job.camera_label_snapshot,
        },
        "start_ts": job.start_ts.isoformat() if job.start_ts else None,
        "end_ts": job.end_ts.isoformat() if job.end_ts else None,
        "duration_seconds": job.duration_seconds,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "progress_percent": job.progress_percent,
        "title": job.title,
        "reason": job.reason,
        "format_hint": job.format_hint,
        "source_segment_count": job.source_segment_count,
        "estimated_source_bytes": job.estimated_source_bytes,
        "gap_warnings": job.gap_warnings or [],
        "error_code": job.error_code,
        "error_message": job.sanitized_error_message,
    }
