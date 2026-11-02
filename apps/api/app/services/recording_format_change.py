"""Desired recording format and read-only convergence, not a second job queue."""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.recording import RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.services.recorder_runtime_status import list_camera_recording_states
from app.services.system_settings import active_recording_jobs_count

APPLY_MODES = frozenset({"after_segment", "immediate"})
FORMAT_FINISH_REASONS = frozenset({
    "recording_format_change",
    "recording_format_finish_timeout",
    "recording_format_finalization_failed",
})
SAFE_FAILURE_REASONS = frozenset({
    "auth_failed", "network_timeout", "invalid_rtsp", "storage_unavailable",
    "permission_denied", "ffmpeg_not_found", "process_crashed",
    "process_start_failed", "recording_media_stalled", "recording_segment_not_rotating",
})


def prepare_format_change(
    db: Session,
    system: SystemSettings,
    data: dict,
    *,
    apply_mode: str | None,
    expected_format: str | None,
) -> bool:
    """Called with the existing settings row lock; rejection changes no settings."""
    target = data.get("recording_format")
    if target is None:
        if apply_mode is not None:
            raise HTTPException(422, detail={"error": "recording_format_target_required"})
        return False
    changed = target != system.recording_format
    if not changed and apply_mode is None:
        return False
    if expected_format is not None and expected_format != system.recording_format:
        raise HTTPException(409, detail={
            "error": "recording_format_settings_changed",
            "current_format": system.recording_format,
            "retryable": True,
        })
    count = active_recording_jobs_count(db)
    if changed and count and apply_mode is None:
        raise HTTPException(409, detail={
            "error": "recording_format_change_requires_choice",
            "active_recording_jobs_count": count,
            "apply_modes": sorted(APPLY_MODES),
            "retryable": True,
        })
    if apply_mode is not None and (apply_mode not in APPLY_MODES or expected_format is None):
        raise HTTPException(422, detail={"error": "recording_format_choice_invalid"})
    mode = apply_mode or "after_segment"
    if changed or mode != system.recording_format_apply_mode:
        system.recording_format_apply_mode = mode
        system.recording_format_requested_at = datetime.utcnow()
        db.add(system)
        return True
    return False


def format_change_status(db: Session, system: SystemSettings) -> dict:
    requested_at = getattr(system, "recording_format_requested_at", None)
    result = {
        "state": "idle" if requested_at is None else "applied",
        "target_format": system.recording_format,
        "apply_mode": getattr(system, "recording_format_apply_mode", None) or "after_segment",
        "requested_at": requested_at,
        "items": [],
        "applied_count": 0,
        "pending_count": 0,
        "failed_count": 0,
    }
    if requested_at is None:
        return result
    rows = [row for row in list_camera_recording_states(db) if row["enabled"] and row["recording_mode"] == "always"]
    job_ids = [row["job_id"] for row in rows if row.get("job_id")]
    jobs = {job.id: job for job in db.query(RecordingJob).filter(RecordingJob.id.in_(job_ids)).all()} if job_ids else {}
    unfinalized = set(camera_id for camera_id, in (
        db.query(RecordingJob.camera_id)
        .join(RecordingSegment, RecordingSegment.job_id == RecordingJob.id)
        .filter(
            RecordingJob.stop_reason.in_(FORMAT_FINISH_REASONS),
            RecordingJob.stopped_at >= requested_at,
            RecordingSegment.status.in_({"writing", "failed", "stale_writing"}),
        ).distinct().all()
    ))
    for row in rows:
        job = jobs.get(row.get("job_id"))
        reason = None
        actual = row.get("recording_container_format")
        if row["camera_id"] in unfinalized:
            state, reason = "failed", "previous_segment_not_finalized"
        elif job is not None and job.stop_reason == "recording_format_finish_timeout":
            state, reason = "failed", "finish_timeout"
        elif row.get("current_failure") or row.get("job_state") == "restarting" or row.get("stale_current_segment"):
            state = "failed"
            code = job.last_error_type if job is not None else None
            reason = code if code in SAFE_FAILURE_REASONS else "recording_not_ready"
        elif actual == system.recording_format and row.get("confirmed_recording"):
            state = "applied"
        elif getattr(system, "recording_suspended_by_low_disk", False):
            state, reason = "waiting", "low_disk_suspended"
        elif not row.get("heartbeat_current"):
            state, reason = "waiting", "recorder_unavailable"
        elif actual in {"mkv", "mp4"} and actual != system.recording_format:
            state = "waiting_segment" if result["apply_mode"] == "after_segment" else "switching"
        else:
            state = "starting"
        result["items"].append({
            "camera_id": row["camera_id"], "camera_name": row["camera_name"],
            "state": state, "current_format": actual, "reason_code": reason,
            "recording_confirmed": bool(row.get("confirmed_recording")),
        })
        count_key = "applied_count" if state == "applied" else "failed_count" if state == "failed" else "pending_count"
        result[count_key] += 1
    if result["failed_count"]:
        result["state"] = "failed"
    elif result["pending_count"]:
        result["state"] = "pending"
    return result
