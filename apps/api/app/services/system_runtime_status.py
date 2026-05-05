from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.services.live_engine_v2 import manager as live_manager
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
}
RECORDING_STALE_GRACE_SECONDS = 60


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


def build_operator_runtime_status(db: Session) -> dict[str, Any]:
    now = _utc_now()
    cameras = db.query(Camera).order_by(Camera.id.asc()).all()
    recorder_domain, _camera_states, recorder_states = _build_recorder_domain(db, now)
    live_domain, live_by_camera = _build_live_domain(cameras)
    cameras_domain = _build_camera_domain(db, now, cameras, recorder_states, live_by_camera)
    domain_severities = [cameras_domain["severity"], live_domain["severity"], recorder_domain["severity"]]
    severity = _rollup(domain_severities)
    problem_count = sum(
        domain["summary"].get("error_count", 0)
        for domain in (cameras_domain, live_domain)
    ) + (1 if recorder_domain["severity"] == "error" else 0)
    warning_count = sum(
        domain["summary"].get("warning_count", 0)
        for domain in (cameras_domain, live_domain)
    ) + (1 if recorder_domain["severity"] == "warning" else 0)
    unknown_count = sum(
        domain["summary"].get("unknown_count", 0)
        for domain in (cameras_domain, live_domain)
    ) + (1 if recorder_domain["severity"] == "unknown" else 0)

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
        ),
        "domains": {
            "cameras": cameras_domain,
            "live": live_domain,
            "recorder": recorder_domain,
        },
    }
