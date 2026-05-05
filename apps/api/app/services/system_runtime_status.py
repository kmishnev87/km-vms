from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.services.live_engine_v2 import manager as live_manager
from app.services.recording_retention import automatic_retention_status
from app.services.recorder_diagnostics import (
    ACTIVE_JOB_STATES,
    HEARTBEAT_STALE_SECONDS,
    SEGMENT_STATUS_DELETED,
    _age_seconds,
    _camera_recording_states,
    _iso,
    _job_summary,
    _read_heartbeat,
    _segment_summary,
    _utc_now,
)
from app.services.storage_monitoring import build_storage_monitoring_summary

SEVERITIES = ("ok", "warning", "error", "unknown")
SAFE_REASON_CODES = {
    "disabled",
    "no_evidence",
    "recorder_heartbeat_stale",
    "recording_failed",
    "recording_stale",
    "live_failed",
    "live_starting",
    "stream_unavailable",
    "unknown_status",
    "camera_unreachable",
    "not_applicable",
    "storage_unavailable",
    "storage_unwritable",
    "storage_unreadable",
    "storage_low_space",
    "storage_unknown",
    "retention_never_run",
    "retention_failed",
    "retention_completed_with_warnings",
    "retention_unknown",
    "retention_policy_risk",
    "reconciliation_never_run",
    "reconciliation_failed",
    "reconciliation_problems_found",
    "cleanup_candidates_present",
    "reconciliation_unknown",
    "not_implemented",
}
RECORDING_STALE_GRACE_SECONDS = 60
LOW_SPACE_WARNING_PERCENT = 10.0
RETENTION_SUCCESS_STATUSES = {"ok", "completed", "success", "completed_successfully", "succeeded"}
RETENTION_WARNING_STATUSES = {"completed_with_warnings", "skipped_concurrent", "warning", "warnings"}
RETENTION_FAILURE_STATUSES = {"failed", "error"}
RETENTION_NO_EVIDENCE_STATUSES = {"", "never_run", "not_run", "none", "null"}


def _severity_rank(value: str) -> int:
    return {"ok": 0, "unknown": 1, "warning": 2, "error": 3}.get(value, 1)


def _rollup(values: list[str]) -> str:
    if not values:
        return "unknown"
    return max(values, key=_severity_rank)


def _count_by_severity(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(item.get("severity") or "unknown") for item in items)
    return {f"{severity}_count": int(counts.get(severity, 0)) for severity in SEVERITIES}


def _safe_reason_from_error(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.lower()
    if "rtsp" in normalized or "connect" in normalized or "timeout" in normalized or "unreachable" in normalized:
        return "camera_unreachable"
    if "stream" in normalized:
        return "stream_unavailable"
    return "unknown_status"


def _append_reason(reasons: list[str], reason: str | None) -> None:
    if reason and reason in SAFE_REASON_CODES and reason not in reasons:
        reasons.append(reason)


def _recording_stale_threshold(camera: Camera) -> int:
    segment_minutes = int(camera.segment_minutes or 5)
    return max((segment_minutes * 2 * 60) + RECORDING_STALE_GRACE_SECONDS, HEARTBEAT_STALE_SECONDS)


def _latest_segment_by_camera(db: Session, now: datetime) -> dict[int, dict[str, Any]]:
    rows = (
        db.query(RecordingSegment)
        .filter(RecordingSegment.status != SEGMENT_STATUS_DELETED)
        .order_by(
            RecordingSegment.camera_id.asc(),
            RecordingSegment.finalized_at.desc().nullslast(),
            RecordingSegment.ended_at.desc().nullslast(),
            RecordingSegment.started_at.desc(),
        )
        .all()
    )
    result: dict[int, dict[str, Any]] = {}
    for segment in rows:
        if segment.camera_id in result:
            continue
        last_time = segment.finalized_at or segment.ended_at or segment.started_at
        result[segment.camera_id] = {
            "last_segment_time": _iso(last_time),
            "last_segment_age_seconds": _age_seconds(last_time, now),
        }
    return result


def _safe_live_items() -> list[dict[str, Any]]:
    return live_manager.status()


def _live_reason_and_severity(item: dict[str, Any] | None, expected: bool) -> tuple[str, list[str], str | None]:
    if item is None:
        if expected:
            return "unknown", ["no_evidence"], None
        return "ok", ["not_applicable"], None

    status = str(item.get("status") or "unknown")
    running = bool(item.get("running"))
    ready = bool(item.get("ready"))
    failure = item.get("failure_reason") or item.get("fallback_reason") or item.get("last_error")
    safe_failure_reason = _safe_reason_from_error(str(failure)) if failure else None
    reasons: list[str] = []

    if ready and running:
        return "ok", [], None
    if status in {"starting", "restarting"} or (running and not ready):
        _append_reason(reasons, "live_starting")
        return "warning", reasons, safe_failure_reason
    if status in {"failed", "error"} or failure:
        _append_reason(reasons, "live_failed")
        _append_reason(reasons, safe_failure_reason)
        return "error", reasons, safe_failure_reason
    _append_reason(reasons, "unknown_status")
    return "unknown", reasons, safe_failure_reason


def _build_live_domain(cameras: list[Camera]) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    raw_items = _safe_live_items()
    by_key = {(int(item.get("camera_id")), str(item.get("stream") or "")): item for item in raw_items if item.get("camera_id") is not None}
    expected_keys = {(camera.id, camera.default_live_stream or "sub") for camera in cameras if camera.enabled}
    keys = sorted(expected_keys | set(by_key.keys()))
    items: list[dict[str, Any]] = []
    by_camera: dict[int, dict[str, Any]] = {}

    for camera_id, stream in keys:
        item = by_key.get((camera_id, stream))
        severity, reasons, safe_failure_reason = _live_reason_and_severity(item, (camera_id, stream) in expected_keys)
        safe_item = {
            "camera_id": camera_id,
            "stream": stream,
            "state": str(item.get("status") or "unknown") if item else "unknown",
            "ready": bool(item.get("ready")) if item else False,
            "running": bool(item.get("running")) if item else False,
            "viewer_count": int(item.get("viewers") or 0) if item else 0,
            "severity": severity,
            "reason_codes": reasons,
            "safe_failure_reason": safe_failure_reason,
            "startup_age_seconds": item.get("startup_elapsed_seconds") if item else None,
        }
        items.append(safe_item)
        current = by_camera.get(camera_id)
        if current is None or _severity_rank(safe_item["severity"]) > _severity_rank(current["severity"]):
            by_camera[camera_id] = safe_item

    counts = _count_by_severity(items)
    summary = {
        "active_streams_count": sum(1 for item in items if item.get("running")),
        "ready_streams_count": sum(1 for item in items if item.get("ready")),
        "failed_streams_count": counts["error_count"],
        "starting_streams_count": sum(1 for item in items if "live_starting" in item.get("reason_codes", [])),
        "unknown_streams_count": counts["unknown_count"],
        "viewer_count": sum(int(item.get("viewer_count") or 0) for item in items),
        **counts,
    }
    return {"severity": _rollup([item["severity"] for item in items]), "summary": summary, "items": items}, by_camera


def _build_recorder_domain(db: Session, now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, dict[str, Any]]]:
    heartbeat = _read_heartbeat(db)
    heartbeat_age_seconds = _age_seconds(heartbeat.get("heartbeat_raw"), now) if heartbeat.get("available") else None
    if "heartbeat_raw" in heartbeat:
        heartbeat = {key: value for key, value in heartbeat.items() if key != "heartbeat_raw"}
    job_summary = _job_summary(db)
    camera_states = _camera_recording_states(db)
    segment_summary = _segment_summary(db, now)

    reasons: list[str] = []
    if not heartbeat.get("available"):
        _append_reason(reasons, "no_evidence")
        severity = "unknown"
    elif heartbeat_age_seconds is None or heartbeat_age_seconds > HEARTBEAT_STALE_SECONDS:
        _append_reason(reasons, "recorder_heartbeat_stale")
        severity = "error" if int(job_summary.get("active_count") or 0) > 0 else "unknown"
    elif heartbeat.get("service_status") in {"error", "failed", "unavailable"}:
        _append_reason(reasons, "recording_failed")
        severity = "error"
    elif heartbeat.get("service_status") == "degraded" or int(job_summary.get("failed_count") or 0) > 0:
        _append_reason(reasons, "recording_failed")
        severity = "warning"
    else:
        severity = "ok"

    states_by_camera = {int(item["camera_id"]): item for item in camera_states}
    summary = {
        "running_jobs": int(job_summary.get("active_count") or 0),
        "active_jobs_count": int(job_summary.get("active_count") or 0),
        "recording_camera_count": sum(1 for item in camera_states if item.get("job_state") == "recording"),
        "failed_camera_count": sum(1 for item in camera_states if item.get("current_failure")),
        "retrying_camera_count": sum(1 for item in camera_states if item.get("job_state") == "restarting"),
        "stale_camera_count": 0,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "stale_after_seconds": HEARTBEAT_STALE_SECONDS,
        "last_segment_time": segment_summary.get("last_segment_time"),
        "last_segment_age_seconds": segment_summary.get("last_segment_age_seconds"),
    }
    return {
        "health": "healthy" if severity == "ok" else severity,
        "severity": severity,
        "service_status": heartbeat.get("service_status") if heartbeat.get("available") else heartbeat.get("status"),
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "stale_after_seconds": HEARTBEAT_STALE_SECONDS,
        "summary": summary,
        "safe_reason_codes": reasons,
    }, camera_states, states_by_camera


def _camera_configured_state(camera: Camera) -> str:
    if not camera.enabled:
        return "disabled"
    has_stream = bool(camera.rtsp_main_url or camera.rtsp_sub_url or camera.onvif_profile_token or camera.host)
    return "configured" if has_stream else "unknown"


def _build_camera_domain(
    db: Session,
    now: datetime,
    cameras: list[Camera],
    recorder_states: dict[int, dict[str, Any]],
    live_by_camera: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    segments_by_camera = _latest_segment_by_camera(db, now)
    items: list[dict[str, Any]] = []

    for camera in cameras:
        recorder_state = recorder_states.get(camera.id) or {}
        live_state = live_by_camera.get(camera.id)
        segment_state = segments_by_camera.get(camera.id) or {}
        reasons: list[str] = []
        configured_state = _camera_configured_state(camera)
        recording_mode = camera.recording_mode or "unknown"
        recording_enabled = bool(camera.enabled and recording_mode == "always")
        job_state = recorder_state.get("job_state")
        current_failure = bool(recorder_state.get("current_failure"))
        last_segment_age = segment_state.get("last_segment_age_seconds")
        stale_threshold = _recording_stale_threshold(camera)

        if not camera.enabled:
            severity = "ok"
            _append_reason(reasons, "disabled")
        elif configured_state != "configured":
            severity = "unknown"
            _append_reason(reasons, "no_evidence")
        elif current_failure:
            severity = "error"
            _append_reason(reasons, "recording_failed")
            _append_reason(reasons, _safe_reason_from_error(recorder_state.get("last_error") or recorder_state.get("camera_last_error")))
        elif recording_enabled and job_state not in ACTIVE_JOB_STATES:
            severity = "warning"
            _append_reason(reasons, "no_evidence")
        elif recording_enabled and last_segment_age is not None and last_segment_age > stale_threshold:
            severity = "warning"
            _append_reason(reasons, "recording_stale")
        elif live_state and live_state.get("severity") == "error":
            severity = "error"
            _append_reason(reasons, "live_failed")
        elif live_state and live_state.get("severity") in {"warning", "unknown"}:
            severity = str(live_state.get("severity"))
            for reason in live_state.get("reason_codes") or []:
                _append_reason(reasons, reason)
        else:
            severity = "ok"

        items.append(
            {
                "camera_id": camera.id,
                "name": camera.name,
                "enabled": bool(camera.enabled),
                "configured_state": configured_state,
                "recording_mode": recording_mode,
                "recording_enabled": recording_enabled,
                "recording_state": job_state or "unknown",
                "live_state": (live_state or {}).get("state", "unknown"),
                "severity": severity,
                "reason_codes": reasons,
                "last_safe_error_code": _safe_reason_from_error(recorder_state.get("last_error") or recorder_state.get("camera_last_error")),
                "last_segment_time": segment_state.get("last_segment_time"),
                "last_segment_age_seconds": last_segment_age,
                "stale_after_seconds": stale_threshold if recording_enabled else None,
            }
        )

    counts = _count_by_severity(items)
    summary = {
        "total_count": len(items),
        "enabled_count": sum(1 for camera in cameras if camera.enabled),
        "disabled_count": sum(1 for camera in cameras if not camera.enabled),
        **counts,
    }
    return {"severity": _rollup([item["severity"] for item in items]), "summary": summary, "items": items}


def _usage_percent(capacity: dict[str, Any]) -> float | None:
    total = capacity.get("total_bytes")
    used = capacity.get("used_bytes")
    if not total or used is None:
        return None
    try:
        return round((int(used) / int(total)) * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _storage_severity_and_reasons(summary: dict[str, Any], usage_percent: float | None) -> tuple[str, list[str]]:
    checks = summary.get("storage_path_checks") or {}
    reasons: list[str] = []
    status = str(summary.get("status") or "unknown")
    available = bool(summary.get("available"))
    readable = bool(checks.get("readable"))
    writable = bool(checks.get("writable"))
    capacity = summary.get("capacity") or {}
    free_percent = None if usage_percent is None else max(0.0, 100.0 - usage_percent)

    if status == "unavailable" or not available:
        _append_reason(reasons, "storage_unavailable")
        return "error", reasons
    if not readable:
        _append_reason(reasons, "storage_unreadable")
    if not writable:
        _append_reason(reasons, "storage_unwritable")
    if free_percent is not None and free_percent < LOW_SPACE_WARNING_PERCENT:
        _append_reason(reasons, "storage_low_space")
    if capacity.get("filesystem_probe_status") not in {None, "ok"}:
        _append_reason(reasons, "storage_unknown")

    if "storage_unreadable" in reasons:
        return "error", reasons
    if reasons or status == "degraded":
        return "warning", reasons
    if status == "available":
        return "ok", reasons
    _append_reason(reasons, "storage_unknown")
    return "unknown", reasons


def _build_storage_domain(storage_summary: dict[str, Any]) -> dict[str, Any]:
    checks = storage_summary.get("storage_path_checks") or {}
    capacity = storage_summary.get("capacity") or {}
    owned = storage_summary.get("owned_archive") or {}
    reconciliation = storage_summary.get("reconciliation_summary") or {}
    usage_percent = _usage_percent(capacity)
    severity, reasons = _storage_severity_and_reasons(storage_summary, usage_percent)
    problem_counts = {
        "missing_file_count": int(reconciliation.get("missing_file_count") or 0),
        "invalid_path_count": int(reconciliation.get("invalid_path_count") or 0),
        "path_outside_storage_count": int(reconciliation.get("path_outside_storage_count") or 0),
        "problem_file_count": int(owned.get("kmvms_owned_problem_file_count") or 0),
    }
    if any(problem_counts.values()) and severity == "ok":
        severity = "warning"

    return {
        "status": str(storage_summary.get("status") or "unknown"),
        "severity": severity,
        "available": bool(storage_summary.get("available")),
        "readable": bool(checks.get("readable")),
        "writable": bool(checks.get("writable")),
        "capacity": {
            "total_bytes": capacity.get("total_bytes"),
            "used_bytes": capacity.get("used_bytes"),
            "free_bytes": capacity.get("free_bytes"),
            "available_bytes": capacity.get("available_bytes"),
            "usage_percent": usage_percent,
        },
        "namespace_status": "available" if bool(storage_summary.get("available")) else "unknown",
        "problem_counts": problem_counts,
        "summary": {
            "segments_count": int(owned.get("kmvms_owned_segments_count") or 0),
            "existing_file_count": int(owned.get("kmvms_owned_existing_file_count") or 0),
            "missing_file_count": problem_counts["missing_file_count"],
            "problem_file_count": problem_counts["problem_file_count"],
        },
        "reason_codes": reasons,
        "evidence_status": "fresh",
        "source": "storage_monitoring_metadata_summary",
        "last_checked_at": storage_summary.get("checked_at"),
    }


def _retention_last_run_age_seconds(state: dict[str, Any], now: datetime) -> int | None:
    value = state.get("last_finished_at") or state.get("last_started_at")
    if not value:
        return None
    return _age_seconds(value, now)


def _normalize_retention_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in RETENTION_SUCCESS_STATUSES:
        return "success"
    if normalized in RETENTION_WARNING_STATUSES:
        return "warning"
    if normalized in RETENTION_FAILURE_STATUSES:
        return "failed"
    if normalized in RETENTION_NO_EVIDENCE_STATUSES:
        return "no_evidence"
    return "unknown"


def _build_retention_domain(db: Session, now: datetime) -> dict[str, Any]:
    state = automatic_retention_status()
    policy_count = db.query(Camera).filter(Camera.retention_days.isnot(None)).count()
    deleted_segments_count = db.query(RecordingSegment).filter(RecordingSegment.status == SEGMENT_STATUS_DELETED).count()
    last_status = state.get("last_status")
    normalized_status = _normalize_retention_status(last_status)
    running = bool(state.get("running"))
    enabled = state.get("enabled")
    reasons: list[str] = []

    if running:
        severity = "ok"
    elif normalized_status == "failed":
        severity = "error"
        _append_reason(reasons, "retention_failed")
    elif normalized_status == "warning":
        severity = "warning"
        _append_reason(reasons, "retention_completed_with_warnings")
    elif normalized_status == "success":
        severity = "ok"
    elif normalized_status == "no_evidence" and policy_count:
        severity = "unknown"
        _append_reason(reasons, "retention_never_run")
    else:
        severity = "unknown"
        _append_reason(reasons, "retention_unknown")

    if policy_count <= 0:
        _append_reason(reasons, "retention_policy_risk")

    return {
        "status": str(last_status or ("running" if running else "unknown")),
        "severity": severity,
        "enabled": enabled if enabled is not None else None,
        "running": running,
        "last_status": last_status,
        "last_started_at": state.get("last_started_at"),
        "last_finished_at": state.get("last_finished_at"),
        "last_run_age_seconds": _retention_last_run_age_seconds(state, now),
        "policy_count": int(policy_count),
        "deleted_segments_count": int(deleted_segments_count),
        "summary": {
            "run_count": int(state.get("run_count") or 0),
            "failed_count": int((state.get("last_summary") or {}).get("failed_count") or 0),
            "skipped_count": int((state.get("last_summary") or {}).get("skipped_count") or 0),
            "deleted_count": int((state.get("last_summary") or {}).get("deleted_count") or 0),
        },
        "reason_codes": reasons,
        "evidence_status": "fresh" if running or normalized_status in {"success", "warning", "failed"} else "missing",
        "source": "automatic_retention_status_memory",
    }


def _build_reconciliation_domain(storage_summary: dict[str, Any]) -> dict[str, Any]:
    raw_reconciliation = storage_summary.get("reconciliation_summary")
    raw_cleanup = storage_summary.get("cleanup_candidates_summary")
    has_reconciliation_evidence = isinstance(raw_reconciliation, dict)
    has_cleanup_evidence = isinstance(raw_cleanup, dict)
    has_status_friendly_evidence = has_reconciliation_evidence and has_cleanup_evidence
    reconciliation = raw_reconciliation if has_reconciliation_evidence else {}
    cleanup = raw_cleanup if has_cleanup_evidence else {}
    missing = int(reconciliation.get("missing_file_count") or 0)
    orphan = int(reconciliation.get("orphan_file_count") or 0)
    path_outside = int(reconciliation.get("path_outside_storage_count") or 0)
    invalid = int(reconciliation.get("invalid_path_count") or 0)
    cleanup_count = int(cleanup.get("count") or 0)
    scan_limited = bool(storage_summary.get("scan_limited"))
    partial = bool(storage_summary.get("partial"))
    reasons: list[str] = []

    if not has_status_friendly_evidence:
        severity = "unknown"
        _append_reason(reasons, "no_evidence")
        _append_reason(reasons, "reconciliation_unknown")
    elif missing or orphan or path_outside or invalid:
        severity = "error" if path_outside else "warning"
        _append_reason(reasons, "reconciliation_problems_found")
    elif cleanup_count:
        severity = "warning"
        _append_reason(reasons, "cleanup_candidates_present")
    elif partial or scan_limited:
        severity = "warning"
        _append_reason(reasons, "reconciliation_unknown")
    else:
        severity = "ok"

    return {
        "status": "no_evidence" if not has_status_friendly_evidence else ("problems_found" if reasons else "ok"),
        "severity": severity,
        "missing_file_count": missing,
        "orphan_file_count": orphan,
        "path_outside_storage_count": path_outside,
        "problem_file_count": missing + orphan + path_outside + invalid,
        "cleanup_candidate_count": cleanup_count,
        "scan_limited": scan_limited,
        "partial": partial,
        "reason_codes": reasons,
        "evidence_status": "fresh" if has_status_friendly_evidence else "missing",
        "source": "storage_monitoring_metadata_reconciliation_counts" if has_status_friendly_evidence else "reconciliation_evidence_missing",
        "last_checked_at": storage_summary.get("checked_at"),
    }


def build_operator_runtime_status(db: Session) -> dict[str, Any]:
    now = _utc_now()
    cameras = db.query(Camera).order_by(Camera.id.asc()).all()
    recorder_domain, _camera_states, recorder_states = _build_recorder_domain(db, now)
    live_domain, live_by_camera = _build_live_domain(cameras)
    cameras_domain = _build_camera_domain(db, now, cameras, recorder_states, live_by_camera)
    storage_summary = build_storage_monitoring_summary(db, include_namespace_observations=False, write_audit=False)
    storage_domain = _build_storage_domain(storage_summary)
    retention_domain = _build_retention_domain(db, now)
    reconciliation_domain = _build_reconciliation_domain(storage_summary)
    domain_severities = [
        cameras_domain["severity"],
        live_domain["severity"],
        recorder_domain["severity"],
        storage_domain["severity"],
        retention_domain["severity"],
        reconciliation_domain["severity"],
    ]
    severity = _rollup(domain_severities)
    problem_count = sum(
        domain["summary"].get("error_count", 0)
        for domain in (cameras_domain, live_domain)
    ) + sum(1 for domain in (recorder_domain, storage_domain, retention_domain, reconciliation_domain) if domain["severity"] == "error")
    warning_count = sum(
        domain["summary"].get("warning_count", 0)
        for domain in (cameras_domain, live_domain)
    ) + sum(1 for domain in (recorder_domain, storage_domain, retention_domain, reconciliation_domain) if domain["severity"] == "warning")
    unknown_count = sum(
        domain["summary"].get("unknown_count", 0)
        for domain in (cameras_domain, live_domain)
    ) + sum(1 for domain in (recorder_domain, storage_domain, retention_domain, reconciliation_domain) if domain["severity"] == "unknown")

    return {
        "generated_at": _iso(now),
        "status": severity,
        "severity": severity,
        "summary": {
            "headline": f"Camera/live/recorder status is {severity}",
            "problem_count": int(problem_count),
            "warning_count": int(warning_count),
            "unknown_count": int(unknown_count),
        },
        "reason_codes": sorted(
            {
                reason
                for domain in (cameras_domain, live_domain)
                for item in domain.get("items", [])
                for reason in item.get("reason_codes", [])
            }
            | set(recorder_domain.get("safe_reason_codes") or [])
            | set(storage_domain.get("reason_codes") or [])
            | set(retention_domain.get("reason_codes") or [])
            | set(reconciliation_domain.get("reason_codes") or [])
        ),
        "domains": {
            "cameras": cameras_domain,
            "live": live_domain,
            "recorder": recorder_domain,
            "storage": storage_domain,
            "retention": retention_domain,
            "reconciliation": reconciliation_domain,
        },
    }
