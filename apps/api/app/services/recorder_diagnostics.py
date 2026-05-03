from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.services.audit_log import redact_text, serialize_event
from app.services.recording_retention import retention_diagnostics
from app.services.storage_monitoring import build_storage_monitoring_summary
from app.services.storage_contract import recording_format_contract, storage_contract
from app.services.system_settings import get_system_settings


ACTIVE_JOB_STATES = {"starting", "recording", "stopping", "restarting"}
FAILED_JOB_STATES = {"error", "restarting"}
HEALTHY_NEUTRAL_JOB_STATES = {"starting", "recording", "stopping", "stopped", "idle", "disabled"}
SEGMENT_STATUS_DELETED = "deleted"
HEARTBEAT_STALE_SECONDS = 90
MAX_RECENT_ITEMS = 50


def _utc_now() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() + "Z"


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))


def _counter_dict(rows: list[tuple[Any, int]]) -> dict[str, int]:
    return {str(key or "unknown"): int(count or 0) for key, count in rows}


def _compact_error(value: str | None) -> str | None:
    if not value:
        return None
    return redact_text(value)[:500]


def _read_heartbeat(db: Session) -> dict[str, Any]:
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
            "error": _compact_error(str(exc)),
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
        "started_at": _iso(row.get("started_at")),
        "heartbeat_at": _iso(row.get("heartbeat_at")),
        "heartbeat_raw": row.get("heartbeat_at"),
        "active_jobs_count": int(row.get("active_jobs_count") or 0),
        "recording_cameras_count": int(row.get("recording_cameras_count") or 0),
        "failed_cameras_count": int(row.get("failed_cameras_count") or 0),
        "last_error": _compact_error(row.get("last_error")),
        "last_exit_code": row.get("last_exit_code"),
        "updated_at": _iso(row.get("updated_at")),
    }


def _job_summary(db: Session) -> dict[str, Any]:
    jobs = db.query(RecordingJob).order_by(RecordingJob.updated_at.desc().nullslast(), RecordingJob.started_at.desc()).limit(MAX_RECENT_ITEMS).all()
    state_counts = _counter_dict(db.query(RecordingJob.state, func.count(RecordingJob.id)).group_by(RecordingJob.state).all())
    active_jobs = [
        {
            "job_id": job.id,
            "camera_id": job.camera_id,
            "camera_name": job.camera_name_snapshot,
            "state": job.state,
            "source_stream": job.source_stream,
            "recorder_instance_id": job.recorder_instance_id,
            "started_at": _iso(job.started_at),
            "stopped_at": _iso(job.stopped_at),
            "last_error": _compact_error(job.last_error),
            "last_error_type": job.last_error_type,
            "last_exit_code": job.last_exit_code,
            "updated_at": _iso(job.updated_at),
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


def _camera_recording_states(db: Session) -> list[dict[str, Any]]:
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
        job_last_error = _compact_error(job.last_error) if job else None
        camera_last_error = _compact_error(camera.last_error)
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
            "job_updated_at": _iso(job.updated_at) if job else None,
            "last_exit_code": job.last_exit_code if job else None,
            "last_error": job_last_error,
            "current_failure": bool(current_failure),
            "stale_error_ignored": bool((job_last_error or camera_last_error) and not current_failure),
        })
    return rows


def _segment_summary(db: Session, now: datetime) -> dict[str, Any]:
    status_counts = _counter_dict(db.query(RecordingSegment.status, func.count(RecordingSegment.id)).group_by(RecordingSegment.status).all())
    integrity_counts = _counter_dict(
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
        "last_segment_time": _iso(last_time),
        "last_segment_age_seconds": _age_seconds(last_time, now),
        "last_segment": {
            "segment_id": last_segment.id,
            "camera_id": last_segment.camera_id,
            "status": last_segment.status,
            "size_bytes": int(last_segment.size_bytes or 0),
            "started_at": _iso(last_segment.started_at),
            "ended_at": _iso(last_segment.ended_at),
            "finalized_at": _iso(last_segment.finalized_at),
        }
        if last_segment
        else None,
        "deletion_summary": deletion_summary,
    }


def _recent_events(db: Session) -> list[dict[str, Any]]:
    events = (
        db.query(AuditEvent)
        .filter(
            (AuditEvent.category.in_(["records", "storage", "diagnostics"]))
            | (AuditEvent.event_type.like("recording.%"))
            | (AuditEvent.event_type.like("retention.%"))
            | (AuditEvent.event_type.like("diagnostics.%"))
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(MAX_RECENT_ITEMS)
        .all()
    )
    return [serialize_event(event) for event in events]


def _storage_state(storage_summary: dict[str, Any]) -> dict[str, Any]:
    owned_archive = storage_summary.get("owned_archive") or {}
    reconciliation = storage_summary.get("reconciliation_summary") or {}
    capacity = storage_summary.get("capacity") or {}
    return {
        "status": storage_summary.get("status"),
        "ok": bool(storage_summary.get("ok")),
        "mount_status": storage_summary.get("mount_status"),
        "scan_limited": bool(storage_summary.get("scan_limited")),
        "partial": bool(storage_summary.get("partial")),
        "warnings_count": len(storage_summary.get("warnings") or []),
        "errors_count": len(storage_summary.get("errors") or []),
        "free_bytes": capacity.get("free_bytes"),
        "available_bytes": capacity.get("available_bytes"),
        "owned_archive": {
            "segments_count": int(owned_archive.get("kmvms_owned_segments_count") or 0),
            "existing_file_count": int(owned_archive.get("kmvms_owned_existing_file_count") or 0),
            "missing_file_count": int(owned_archive.get("kmvms_owned_missing_file_count") or 0),
            "problem_file_count": int(owned_archive.get("kmvms_owned_problem_file_count") or 0),
        },
        "reconciliation": {
            "missing_file_count": int(reconciliation.get("missing_file_count") or 0),
            "orphan_file_count": int(reconciliation.get("orphan_file_count") or 0),
            "path_outside_storage_count": int(reconciliation.get("path_outside_storage_count") or 0),
        },
    }


def _retention_state(retention_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": retention_summary.get("status"),
        "dry_run_available": bool(retention_summary.get("dry_run_available")),
        "execute_available": bool(retention_summary.get("execute_available")),
        "metadata_strategy": retention_summary.get("metadata_strategy"),
        "deleted_segments_count": int(retention_summary.get("deleted_segments_count") or 0),
        "policy_count": len(retention_summary.get("policies") or []),
        "concurrency_guard": retention_summary.get("concurrency_guard"),
        "automatic_retention": retention_summary.get("automatic_retention"),
    }


def _health_from(
    *,
    heartbeat: dict[str, Any],
    heartbeat_age_seconds: int | None,
    job_summary: dict[str, Any],
    camera_states: list[dict[str, Any]],
    storage_state: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    enabled_always = [item for item in camera_states if item["enabled"] and item["recording_mode"] == "always"]
    failed_cameras = [item for item in camera_states if item.get("current_failure")]

    if not heartbeat.get("available"):
        reasons.append(str(heartbeat.get("reason") or "heartbeat_unavailable"))
    elif heartbeat_age_seconds is None or heartbeat_age_seconds > HEARTBEAT_STALE_SECONDS:
        reasons.append("heartbeat_stale")
    elif heartbeat.get("service_status") in {"error", "degraded"}:
        reasons.append(f"recorder_service_{heartbeat.get('service_status')}")

    if storage_state.get("status") not in {"available", None}:
        reasons.append(f"storage_{storage_state.get('status')}")
    if failed_cameras:
        reasons.append("camera_recording_errors")
    if enabled_always and job_summary.get("active_count", 0) == 0:
        reasons.append("no_active_jobs_for_enabled_always_cameras")

    if any(reason in reasons for reason in ["recorder_runtime_status_unavailable", "no_recorder_heartbeat"]):
        return "unavailable", reasons
    if reasons:
        return "degraded", reasons
    return "healthy", ["all_checks_passed"]


def build_recorder_status(db: Session) -> dict[str, Any]:
    now = _utc_now()
    heartbeat = _read_heartbeat(db)
    heartbeat_age_seconds = _age_seconds(heartbeat.get("heartbeat_raw"), now) if heartbeat.get("available") else None
    if "heartbeat_raw" in heartbeat:
        heartbeat = {key: value for key, value in heartbeat.items() if key != "heartbeat_raw"}
    job_summary = _job_summary(db)
    camera_states = _camera_recording_states(db)
    segment_summary = _segment_summary(db, now)
    storage_summary = build_storage_monitoring_summary(db, include_namespace_observations=True)
    storage_state = _storage_state(storage_summary)
    retention_summary = retention_diagnostics(db)
    retention_state = _retention_state(retention_summary)
    system_settings = get_system_settings(db)
    format_contract = recording_format_contract(system_settings.recording_format)
    health, health_reasons = _health_from(
        heartbeat=heartbeat,
        heartbeat_age_seconds=heartbeat_age_seconds,
        job_summary=job_summary,
        camera_states=camera_states,
        storage_state=storage_state,
    )
    return {
        "generated_at": _iso(now),
        "service_status": heartbeat.get("service_status") if heartbeat.get("available") else heartbeat.get("status"),
        "health": health,
        "health_reasons": health_reasons,
        "liveness_source": {
            "type": "recorder_runtime_status_heartbeat",
            "stale_after_seconds": HEARTBEAT_STALE_SECONDS,
            "status": "fresh" if heartbeat.get("available") and heartbeat_age_seconds is not None and heartbeat_age_seconds <= HEARTBEAT_STALE_SECONDS else "stale_or_unavailable",
        },
        "heartbeat": {
            **heartbeat,
            "age_seconds": heartbeat_age_seconds,
        },
        "storage_contract": storage_contract(db_storage_path=system_settings.storage_path),
        "recording_format_contract": format_contract,
        "effective_recording_format": format_contract["recording_format"],
        "active_jobs": job_summary.get("recent_jobs", []),
        "job_summary": job_summary,
        "camera_recording_states": camera_states,
        "cameras_recording_count": sum(1 for item in camera_states if item.get("job_state") == "recording"),
        "failed_cameras_count": sum(1 for item in camera_states if item.get("current_failure")),
        "retrying_cameras_count": sum(1 for item in camera_states if item.get("job_state") == "restarting"),
        "last_segment_time": segment_summary.get("last_segment_time"),
        "last_segment_age_seconds": segment_summary.get("last_segment_age_seconds"),
        "last_error": heartbeat.get("last_error"),
        "last_ffmpeg_exit_code": heartbeat.get("last_exit_code"),
        "uptime_seconds": _age_seconds(datetime.fromisoformat(heartbeat["started_at"].removesuffix("Z")), now)
        if heartbeat.get("started_at")
        else None,
        "restart_count": {"status": "unavailable", "reason": "restart_counter_not_persisted"},
        "current_output_path": {"status": "unavailable", "reason": "current_segment_path_not_persisted"},
        "storage_state": storage_state,
        "retention_status": retention_state,
        "deletion_summary": segment_summary.get("deletion_summary", []),
        "orphan_pre_metadata_cleanup_summary": (storage_summary.get("cleanup_candidates_summary") or {}),
        "segment_summary": segment_summary,
        "recent_events": _recent_events(db),
        "log_summary": {"status": "unavailable", "reason": "no_safe_product_log_summary_source"},
    }


def _system_runtime_from_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": status["generated_at"],
        "recorder": {
            "health": status["health"],
            "service_status": status["service_status"],
            "health_reasons": status["health_reasons"],
            "heartbeat_age_seconds": status["heartbeat"].get("age_seconds"),
            "active_jobs_count": status["job_summary"].get("active_count", 0),
            "cameras_recording_count": status["cameras_recording_count"],
            "failed_cameras_count": status["failed_cameras_count"],
            "retrying_cameras_count": status["retrying_cameras_count"],
            "last_segment_time": status["last_segment_time"],
            "last_segment_age_seconds": status["last_segment_age_seconds"],
        },
        "storage": status["storage_state"],
        "storage_contract": status.get("storage_contract"),
        "recording_format_contract": status.get("recording_format_contract"),
        "effective_recording_format": status.get("effective_recording_format"),
        "retention": status["retention_status"],
    }


def build_system_runtime_status(db: Session) -> dict[str, Any]:
    now = _utc_now()
    heartbeat = _read_heartbeat(db)
    heartbeat_age_seconds = _age_seconds(heartbeat.get("heartbeat_raw"), now) if heartbeat.get("available") else None
    if "heartbeat_raw" in heartbeat:
        heartbeat = {key: value for key, value in heartbeat.items() if key != "heartbeat_raw"}
    job_summary = _job_summary(db)
    camera_states = _camera_recording_states(db)
    segment_summary = _segment_summary(db, now)
    storage_state = _storage_state(build_storage_monitoring_summary(db, include_namespace_observations=False))
    retention_state = _retention_state(retention_diagnostics(db))
    system_settings = get_system_settings(db)
    format_contract = recording_format_contract(system_settings.recording_format)
    health, health_reasons = _health_from(
        heartbeat=heartbeat,
        heartbeat_age_seconds=heartbeat_age_seconds,
        job_summary=job_summary,
        camera_states=camera_states,
        storage_state=storage_state,
    )
    return _system_runtime_from_status(
        {
            "generated_at": _iso(now),
            "health": health,
            "service_status": heartbeat.get("service_status") if heartbeat.get("available") else heartbeat.get("status"),
            "health_reasons": health_reasons,
            "heartbeat": {"age_seconds": heartbeat_age_seconds},
            "job_summary": job_summary,
            "cameras_recording_count": sum(1 for item in camera_states if item.get("job_state") == "recording"),
        "failed_cameras_count": sum(1 for item in camera_states if item.get("current_failure")),
            "retrying_cameras_count": sum(1 for item in camera_states if item.get("job_state") == "restarting"),
            "last_segment_time": segment_summary.get("last_segment_time"),
            "last_segment_age_seconds": segment_summary.get("last_segment_age_seconds"),
            "storage_state": storage_state,
            "retention_status": retention_state,
            "storage_contract": storage_contract(db_storage_path=system_settings.storage_path),
            "recording_format_contract": format_contract,
            "effective_recording_format": format_contract["recording_format"],
        }
    )


def build_recorder_archive_payloads(db: Session) -> dict[str, Any]:
    status = build_recorder_status(db)
    return {
        "system/runtime_status.json": _system_runtime_from_status(status),
        "recorder/status.json": status,
        "recorder/jobs_summary.json": status["job_summary"],
        "recorder/camera_recording_states.json": {
            "items": status["camera_recording_states"],
            "count": len(status["camera_recording_states"]),
        },
        "recorder/segment_summary.json": status["segment_summary"],
        "recorder/recent_events.json": {
            "items": status["recent_events"],
            "count": len(status["recent_events"]),
        },
        "recorder/log_summary.json": status["log_summary"],
    }
