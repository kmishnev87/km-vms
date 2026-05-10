from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core.sanitization import redact_text
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.services.timezone_contract import TimezoneContext, format_system_iso, timestamp_matches_filename, timezone_context, utc_now_storage

ACTIVE_JOB_STATES = {"starting", "recording", "stopping", "restarting"}
FAILED_JOB_STATES = {"error", "restarting"}
HEALTHY_NEUTRAL_JOB_STATES = {"starting", "recording", "stopping", "stopped", "idle", "disabled"}
SEGMENT_STATUS_DELETED = "deleted"
RECORDER_SOURCE = "recorder"
HEARTBEAT_STALE_SECONDS = 90
MAX_RECENT_ITEMS = 50


def utc_now() -> datetime:
    return utc_now_storage()


def iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.replace(" ", "T")
        return normalized if normalized.endswith("Z") else normalized + "Z"
    return value.isoformat() + "Z"


def system_iso_or_none(value: datetime | str | None, ctx: TimezoneContext) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.removesuffix("Z"))
    return format_system_iso(value, ctx)


def timestamp_fields(name: str, value: datetime | str | None, ctx: TimezoneContext) -> dict[str, str | None]:
    return {
        name: iso_or_none(value),
        f"{name}_utc": iso_or_none(value),
        f"{name}_system": system_iso_or_none(value, ctx),
    }


def segment_uses_local_naive_display(segment: RecordingSegment | None) -> bool:
    if segment is None or segment.source != RECORDER_SOURCE:
        return False
    candidates = [
        Path(str(segment.relative_path or "")).name,
        Path(str(segment.file_path or "")).name,
    ]
    return any(timestamp_matches_filename(segment.started_at, candidate) for candidate in candidates)


def segment_timestamp_fields(
    name: str,
    value: datetime | str | None,
    ctx: TimezoneContext,
    segment: RecordingSegment | None,
) -> dict[str, str | None]:
    local_naive = segment_uses_local_naive_display(segment)
    return {
        name: iso_or_none(value),
        f"{name}_utc": iso_or_none(value),
        f"{name}_system": format_system_iso(value, ctx, local_naive=local_naive) if not isinstance(value, str) else system_iso_or_none(value, ctx),
        f"{name}_display_semantic": "product_local_naive" if local_naive else "storage_utc_naive",
    }


def age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.removesuffix("Z"))
    return max(0, int((now - value).total_seconds()))


def counter_dict(rows: list[tuple[Any, int]]) -> dict[str, int]:
    return {str(key or "unknown"): int(count or 0) for key, count in rows}


def compact_error(value: str | None) -> str | None:
    if not value:
        return None
    return redact_text(value)[:500]


def read_recorder_heartbeat(db: Session) -> dict[str, Any]:
    ctx = timezone_context(db)
    try:
        row = (
            db.execute(
                text(
                    """
                    SELECT recorder_instance_id,
                           service_status,
                           loop_state,
                           started_at,
                           heartbeat_at,
                           active_jobs_count,
                           recording_cameras_count,
                           failed_cameras_count,
                           last_error,
                           last_exit_code,
                           updated_at
                    FROM recorder_runtime_status
                    ORDER BY heartbeat_at DESC
                    LIMIT 1
                    """
                )
            )
            .mappings()
            .first()
        )
    except Exception as exc:
        return {
            "available": False,
            "status": "unavailable",
            "reason": "recorder_runtime_status_unavailable",
            "error": compact_error(str(exc)),
        }

    if row is None:
        return {
            "available": False,
            "status": "missing",
            "reason": "no_recorder_heartbeat",
        }
    return {
        "available": True,
        "recorder_instance_id": row.get("recorder_instance_id"),
        "service_status": row.get("service_status"),
        "loop_state": row.get("loop_state"),
        **timestamp_fields("started_at", row.get("started_at"), ctx),
        **timestamp_fields("heartbeat_at", row.get("heartbeat_at"), ctx),
        "heartbeat_raw": row.get("heartbeat_at"),
        "active_jobs_count": int(row.get("active_jobs_count") or 0),
        "recording_cameras_count": int(row.get("recording_cameras_count") or 0),
        "failed_cameras_count": int(row.get("failed_cameras_count") or 0),
        "last_error": compact_error(row.get("last_error")),
        "last_exit_code": row.get("last_exit_code"),
        **timestamp_fields("updated_at", row.get("updated_at"), ctx),
    }


def summarize_recorder_jobs(db: Session) -> dict[str, Any]:
    ctx = timezone_context(db)
    jobs = db.query(RecordingJob).order_by(RecordingJob.updated_at.desc().nullslast(), RecordingJob.started_at.desc()).limit(MAX_RECENT_ITEMS).all()
    state_counts = counter_dict(db.query(RecordingJob.state, func.count(RecordingJob.id)).group_by(RecordingJob.state).all())
    active_jobs = [
        {
            "job_id": job.id,
            "camera_id": job.camera_id,
            "camera_name": job.camera_name_snapshot,
            "state": job.state,
            "source_stream": job.source_stream,
            "recorder_instance_id": job.recorder_instance_id,
            **timestamp_fields("started_at", job.started_at, ctx),
            **timestamp_fields("stopped_at", job.stopped_at, ctx),
            "last_error": compact_error(job.last_error),
            "last_error_type": job.last_error_type,
            "last_exit_code": job.last_exit_code,
            **timestamp_fields("updated_at", job.updated_at, ctx),
        }
        for job in jobs
        if job.state in ACTIVE_JOB_STATES or job.last_error
    ]
    return {
        "state_counts": state_counts,
        "active_count": sum(state_counts.get(state, 0) for state in ACTIVE_JOB_STATES),
        "failed_count": sum(state_counts.get(state, 0) for state in FAILED_JOB_STATES),
        "recent_jobs": active_jobs[:MAX_RECENT_ITEMS],
    }


def list_camera_recording_states(db: Session) -> list[dict[str, Any]]:
    ctx = timezone_context(db)
    cameras = db.query(Camera).order_by(Camera.id.asc()).all()
    latest_jobs: dict[int, RecordingJob] = {}

    def job_rank(job: RecordingJob) -> tuple[int, datetime]:
        state = str(job.state or "")
        if state in {"starting", "recording", "stopping", "restarting"}:
            priority = 3
        elif state == "error":
            priority = 2
        else:
            priority = 1
        timestamp = job.updated_at or job.started_at or job.created_at or datetime.min
        return priority, timestamp

    for job in db.query(RecordingJob).order_by(RecordingJob.camera_id.asc()).all():
        current = latest_jobs.get(job.camera_id)
        if current is None or job_rank(job) >= job_rank(current):
            latest_jobs[job.camera_id] = job

    rows = []
    for camera in cameras:
        job = latest_jobs.get(camera.id)
        job_state = job.state if job else None
        job_last_error = compact_error(job.last_error) if job else None
        camera_last_error = compact_error(camera.last_error)
        has_healthy_current_job = job_state in HEALTHY_NEUTRAL_JOB_STATES
        current_failure = (
            job_state in FAILED_JOB_STATES
            or (job_state not in HEALTHY_NEUTRAL_JOB_STATES and bool(job_last_error))
            or (not has_healthy_current_job and (camera.status == "error" or bool(camera_last_error)))
        )
        rows.append({
            "camera_id": camera.id,
            "camera_name": camera.name,
            "enabled": bool(camera.enabled),
            "recording_mode": camera.recording_mode,
            "camera_status": camera.status,
            "camera_last_error": camera_last_error,
            "job_state": job_state,
            "job_id": job.id if job else None,
            **timestamp_fields("job_updated_at", job.updated_at if job else None, ctx),
            "last_exit_code": job.last_exit_code if job else None,
            "last_error": job_last_error,
            "current_failure": bool(current_failure),
            "stale_error_ignored": bool((job_last_error or camera_last_error) and not current_failure),
        })
    return rows


def summarize_recorder_segments(db: Session, now: datetime) -> dict[str, Any]:
    ctx = timezone_context(db)
    status_counts = counter_dict(db.query(RecordingSegment.status, func.count(RecordingSegment.id)).group_by(RecordingSegment.status).all())
    integrity_counts = counter_dict(
        db.query(RecordingSegment.integrity_status, func.count(RecordingSegment.id)).group_by(RecordingSegment.integrity_status).all()
    )
    last_segment = (
        db.query(RecordingSegment)
        .filter(RecordingSegment.status != SEGMENT_STATUS_DELETED)
        .order_by(
            RecordingSegment.finalized_at.desc().nullslast(),
            RecordingSegment.ended_at.desc().nullslast(),
            RecordingSegment.started_at.desc(),
        )
        .first()
    )
    deletion_rows = (
        db.query(RecordingSegment.deletion_source, RecordingSegment.deletion_reason, func.count(RecordingSegment.id))
        .filter(RecordingSegment.status == SEGMENT_STATUS_DELETED)
        .group_by(RecordingSegment.deletion_source, RecordingSegment.deletion_reason)
        .all()
    )
    deletion_summary = [
        {
            "deletion_source": source or "unknown",
            "deletion_reason": reason or "unknown",
            "count": int(count or 0),
        }
        for source, reason, count in deletion_rows
    ]
    last_time = None
    if last_segment:
        last_time = last_segment.finalized_at or last_segment.ended_at or last_segment.started_at
    return {
        "status_counts": status_counts,
        "integrity_status_counts": integrity_counts,
        **segment_timestamp_fields("last_segment_time", last_time, ctx, last_segment),
        "last_segment_age_seconds": age_seconds(last_time, now),
        "last_segment": {
            "segment_id": last_segment.id,
            "camera_id": last_segment.camera_id,
            "status": last_segment.status,
            "size_bytes": int(last_segment.size_bytes or 0),
            "timestamp_display_semantic": "product_local_naive" if segment_uses_local_naive_display(last_segment) else "storage_utc_naive",
            **segment_timestamp_fields("started_at", last_segment.started_at, ctx, last_segment),
            **segment_timestamp_fields("ended_at", last_segment.ended_at, ctx, last_segment),
            **segment_timestamp_fields("finalized_at", last_segment.finalized_at, ctx, last_segment),
        }
        if last_segment
        else None,
        "deletion_summary": deletion_summary,
    }
