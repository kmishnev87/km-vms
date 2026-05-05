from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.services.audit_log import serialize_event
from app.services.recording_retention import retention_diagnostics
from app.services.recorder_runtime_status import (
    HEARTBEAT_STALE_SECONDS,
    SEGMENT_STATUS_DELETED,
    age_seconds as _age_seconds,
    iso_or_none as _iso,
    list_camera_recording_states as _camera_recording_states,
    read_recorder_heartbeat as _read_heartbeat,
    summarize_recorder_jobs as _job_summary,
    summarize_recorder_segments as _segment_summary,
    utc_now as _utc_now,
)
from app.services.storage_monitoring import build_storage_monitoring_summary
from app.services.storage_contract import recording_format_contract, storage_contract
from app.services.system_settings import get_system_settings


FAILED_JOB_STATES = {"error", "restarting"}
MAX_RECENT_ITEMS = 50


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
