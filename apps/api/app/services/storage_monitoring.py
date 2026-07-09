from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import os
import re
import shutil
import threading
import time

from sqlalchemy.orm import Session, object_session

from app.core.config import settings
from app.core.sanitization import redact_text
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.services.audit_log import create_event
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    VIDEO_EXTENSIONS,
    archive_root_public_status,
    list_archive_roots,
    migration_preview,
    resolve_segment_file_path,
    root_usage,
)
from app.services.storage_contract import storage_contract
from app.services.timezone_contract import format_system_iso, timezone_context

OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
SEGMENT_STATUS_DELETED = "deleted"
COUNTABLE_ARCHIVE_SEGMENT_STATUSES = {"finalized", "ready"}
SCAN_MODE_METADATA_ONLY = "metadata_only"
SCAN_MODE_NAMESPACE_BOUNDED = "namespace_bounded"
MAX_NAMESPACE_FILES = 1000
MAX_NAMESPACE_DIRS = 300
MAX_SCAN_SECONDS = 3.0
MAX_SAMPLE_ITEMS = 50
_STORAGE_AUDIT_LOCK = threading.Lock()
TECHNICAL_DELETED_CAMERA_RE = re.compile(r"__deleted_\d+_\d+$")
_LAST_STORAGE_AUDIT_STATE: dict[str, dict] = {}


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _storage_root_path() -> Path:
    return Path(settings.storage_root)


def _safe_realpath(path: Path) -> str | None:
    try:
        return str(path.resolve())
    except OSError:
        return None


def _safe_relative(target: Path, root: Path) -> tuple[str | None, str | None, Path | None]:
    try:
        resolved_root = root.resolve()
        resolved_target = (resolved_root / target).resolve() if not target.is_absolute() else target.resolve()
        resolved_target.relative_to(resolved_root)
        return resolved_target.relative_to(resolved_root).as_posix(), None, resolved_target
    except ValueError:
        return None, "path_outside_storage", None
    except OSError as exc:
        return None, redact_text(str(exc)) or "path_error", None


def _segment_relative(segment: RecordingSegment, root: Path) -> tuple[str | None, str | None, Path | None]:
    if segment.relative_path:
        return _safe_relative(Path(segment.relative_path), root)
    if not segment.file_path:
        return None, "invalid_path", None
    return _safe_relative(Path(segment.file_path), root)


def _path_checks(root: Path) -> dict:
    exists = root.exists()
    is_dir = root.is_dir() if exists else False
    readable = os.access(root, os.R_OK) if exists else False
    writable = os.access(root, os.W_OK) if exists else False
    executable = os.access(root, os.X_OK) if exists else False
    status = "available" if exists and is_dir else "unavailable"
    error = None
    if not exists:
        error = "storage_root_missing"
    elif not is_dir:
        error = "storage_root_not_directory"
    elif not readable:
        status = "degraded"
        error = "storage_root_not_readable"
    elif not writable:
        status = "degraded"
        error = "storage_root_not_writable"

    return {
        "path_exists": exists,
        "is_dir": is_dir,
        "readable": readable,
        "writable": writable,
        "executable": executable,
        "can_write_test_file": None,
        "can_delete_test_file": None,
        "can_create_folder": None,
        "write_probe_status": "not_run_read_only_status",
        "write_probe_policy": "normal_status_does_not_create_or_delete_probe_files",
        "write_probe_note": "Actual write/delete capability must be validated separately with bounded stage6_test artifacts.",
        "permission_error": error if error and "not_" in error else None,
        "last_error": error,
        "status": status,
        "storage_root_realpath": _safe_realpath(root),
    }


def _storage_audit_state(summary: dict) -> dict:
    checks = summary.get("storage_path_checks") or {}
    return {
        "status": summary.get("status"),
        "available": bool(summary.get("available")),
        "readable": bool(checks.get("readable")),
        "writable": bool(checks.get("writable")),
        "reason": checks.get("last_error"),
        "storage_namespace": summary.get("storage_namespace"),
        "storage_root": summary.get("container_runtime_storage_root"),
    }


def _storage_transition_event(previous: dict | None, current: dict) -> tuple[str | None, str]:
    if previous is None:
        if current["status"] == "unavailable":
            return "storage.unavailable", "error"
        if not current["writable"]:
            return "storage.unwritable", "warning"
        return None, "info"
    if bool(previous.get("available")) != bool(current.get("available")):
        return ("storage.available", "info") if current["available"] else ("storage.unavailable", "error")
    if bool(previous.get("writable")) != bool(current.get("writable")):
        return ("storage.writable", "info") if current["writable"] else ("storage.unwritable", "warning")
    if previous.get("status") != current.get("status"):
        return "storage.status_transition", "warning" if current.get("status") == "degraded" else "info"
    return None, "info"


def _maybe_audit_storage_transition(db: Session, summary: dict, *, actor=None) -> None:
    current = _storage_audit_state(summary)
    state_key = str(current.get("storage_root") or "default")
    with _STORAGE_AUDIT_LOCK:
        previous = _LAST_STORAGE_AUDIT_STATE.get(state_key)
        if previous == current:
            return
        event_type, severity = _storage_transition_event(previous, current)
        _LAST_STORAGE_AUDIT_STATE[state_key] = dict(current)
    if not event_type:
        return
    metadata = {
        "previous_status": previous.get("status") if previous else None,
        "current_status": current.get("status"),
        "previous_writable": previous.get("writable") if previous else None,
        "current_writable": current.get("writable"),
        "readable": current.get("readable"),
        "writable": current.get("writable"),
        "available": current.get("available"),
        "reason": current.get("reason"),
        "storage_namespace": current.get("storage_namespace"),
        "storage_root": current.get("storage_root"),
        "checked_at": summary.get("checked_at"),
        "capacity": summary.get("capacity"),
    }
    create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type=event_type,
        severity=severity,
        message_ru=f"Storage status transition: {metadata['previous_status']} -> {metadata['current_status']}",
        message_en=f"Storage status transition: {metadata['previous_status']} -> {metadata['current_status']}",
        target_type="storage",
        target_id=current.get("storage_namespace"),
        metadata=metadata,
    )


def reset_storage_audit_state() -> None:
    with _STORAGE_AUDIT_LOCK:
        _LAST_STORAGE_AUDIT_STATE.clear()


def _capacity(root: Path) -> tuple[dict, str | None]:
    try:
        usage = shutil.disk_usage(root)
        return (
            {
                "total_bytes": int(usage.total),
                "used_bytes": int(usage.used),
                "free_bytes": int(usage.free),
                "available_bytes": int(usage.free),
                "filesystem_probe_status": "ok",
            },
            None,
        )
    except OSError as exc:
        return (
            {
                "total_bytes": None,
                "used_bytes": None,
                "free_bytes": None,
                "available_bytes": None,
                "filesystem_probe_status": "error",
            },
            redact_text(str(exc)),
        )


def _capacity_percent(numerator: int | None, denominator: int | None) -> float | None:
    if denominator in {None, 0} or numerator is None:
        return None
    try:
        return round((int(numerator) / int(denominator)) * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _is_technical_deleted_camera_label(value: str | None) -> bool:
    return bool(value and TECHNICAL_DELETED_CAMERA_RE.search(str(value)))


def _safe_camera_usage_label(*values: str | None, fallback: str = "Удалённая камера") -> str:
    for value in values:
        text = str(value or "").strip()
        if text and not _is_technical_deleted_camera_label(text):
            return text
    return fallback


def _empty_camera_usage(camera: Camera | None = None) -> dict:
    return {
        "camera_id": camera.id if camera else None,
        "camera_name": _safe_camera_usage_label(camera.name) if camera else None,
        "recording_count": 0,
        "segment_count": 0,
        "existing_file_count": 0,
        "missing_file_count": 0,
        "problem_file_count": 0,
        "size_bytes": 0,
        "newest_recording_at": None,
        "oldest_recording_at": None,
        "container_format_counts": {},
        "file_extension_counts": {},
        "status_counts": {},
        "integrity_status_counts": {},
    }


def _bump_counter_map(row: dict, field: str, value: str | None) -> None:
    key = value or "unknown"
    counts = dict(row.get(field) or {})
    counts[key] = int(counts.get(key) or 0) + 1
    row[field] = counts


def _update_range(row: dict, started_at: datetime | None) -> None:
    if not started_at:
        return
    value = started_at.isoformat()
    if row["oldest_recording_at"] is None or value < row["oldest_recording_at"]:
        row["oldest_recording_at"] = value
    if row["newest_recording_at"] is None or value > row["newest_recording_at"]:
        row["newest_recording_at"] = value


def _is_kmvms_owned(segment: RecordingSegment) -> bool:
    return segment.ownership == OWNERSHIP_KM_VMS and segment.source == RECORDER_SOURCE


def _is_countable_archive_segment(segment: RecordingSegment) -> bool:
    return (
        _is_kmvms_owned(segment)
        and segment.deleted_at is None
        and segment.status in COUNTABLE_ARCHIVE_SEGMENT_STATUSES
    )


def _safe_stat_segment(segment: RecordingSegment, root: Path) -> tuple[str | None, int | None, str | None, Path | None]:
    try:
        db = object_session(segment)
        if db is None:
            return segment.relative_path, None, "db_session_missing", None
        target = resolve_segment_file_path(db, segment)
        rel_path = segment.relative_path
    except FileNotFoundError:
        return segment.relative_path, None, "missing_file", None
    except Exception:
        return segment.relative_path, None, "invalid_path", None
    if not target.exists():
        return rel_path, None, "missing_file", target
    if not target.is_file():
        return rel_path, None, "not_file", target
    try:
        return rel_path, int(target.stat().st_size), None, target
    except OSError as exc:
        return rel_path, None, redact_text(str(exc)) or "stat_error", target


def _observe_namespace(root: Path, owned_paths: set[str]) -> dict:
    namespace_root = root / KMVMS_RECORDINGS_NAMESPACE
    observations = {
        "namespace_root": str(namespace_root),
        "namespace_exists": namespace_root.exists(),
        "scan_mode": SCAN_MODE_NAMESPACE_BOUNDED,
        "scan_limited": False,
        "partial": False,
        "partial_reason": None,
        "max_files": MAX_NAMESPACE_FILES,
        "max_dirs": MAX_NAMESPACE_DIRS,
        "max_seconds": MAX_SCAN_SECONDS,
        "scanned_files": 0,
        "scanned_dirs": 0,
        "orphan_file_count": 0,
        "foreign_unknown_count": 0,
        "samples": [],
        "errors": [],
    }
    if not namespace_root.exists() or not namespace_root.is_dir():
        return observations

    start = time.monotonic()
    try:
        for current_root, dirs, files in os.walk(namespace_root, followlinks=False):
            observations["scanned_dirs"] += 1
            if observations["scanned_dirs"] > MAX_NAMESPACE_DIRS:
                observations["scan_limited"] = True
                observations["partial"] = True
                observations["partial_reason"] = "max_dirs"
                break
            if time.monotonic() - start > MAX_SCAN_SECONDS:
                observations["scan_limited"] = True
                observations["partial"] = True
                observations["partial_reason"] = "max_seconds"
                break

            dirs[:] = [name for name in dirs if not (Path(current_root) / name).is_symlink()]
            for name in files:
                path = Path(current_root) / name
                if path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                observations["scanned_files"] += 1
                if observations["scanned_files"] > MAX_NAMESPACE_FILES:
                    observations["scan_limited"] = True
                    observations["partial"] = True
                    observations["partial_reason"] = "max_files"
                    break
                rel_path, path_error, _target = _safe_relative(path, root)
                if path_error or not rel_path:
                    observations["foreign_unknown_count"] += 1
                    continue
                if rel_path in owned_paths:
                    continue
                observations["orphan_file_count"] += 1
                if len(observations["samples"]) < MAX_SAMPLE_ITEMS:
                    observations["samples"].append(
                        {
                            "relative_path": rel_path,
                            "observation": "file_in_kmvms_namespace_without_owned_metadata",
                        }
                    )
            if observations["scan_limited"]:
                break
    except OSError as exc:
        observations["errors"].append(redact_text(str(exc)))
        observations["partial"] = True
        observations["partial_reason"] = "scan_error"
    return observations


def _safe_last_summary(value: dict | None) -> dict:
    value = value or {}
    return {
        "ok": value.get("ok"),
        "operation": value.get("operation"),
        "requested_count": int(value.get("requested_count") or 0),
        "planned_count": int(value.get("planned_count") or 0),
        "deleted_count": int(value.get("deleted_count") or 0),
        "skipped_count": int(value.get("skipped_count") or 0),
        "failed_count": int(value.get("failed_count") or 0),
        "bytes_freed": int(value.get("bytes_freed") or 0),
        "reason_counts": dict(value.get("reason_counts") or {}),
        "item_reason_counts": dict(value.get("item_reason_counts") or {}),
        "skipped_reason_counts": dict(value.get("skipped_reason_counts") or {}),
        "failed_reason_counts": dict(value.get("failed_reason_counts") or {}),
        "observability": dict(value.get("observability") or {}),
        "warnings": list(value.get("warnings") or [])[:MAX_SAMPLE_ITEMS],
    }


def _safe_camera_usage(rows: list[dict] | None) -> list[dict]:
    safe_rows = []
    for row in rows or []:
        safe_rows.append(
            {
                "camera_id": row.get("camera_id"),
                "camera_name": row.get("camera_name"),
                "recording_count": int(row.get("recording_count") or 0),
                "segment_count": int(row.get("segment_count") or 0),
                "existing_file_count": int(row.get("existing_file_count") or 0),
                "missing_file_count": int(row.get("missing_file_count") or 0),
                "problem_file_count": int(row.get("problem_file_count") or 0),
                "size_bytes": int(row.get("size_bytes") or 0),
                "oldest_recording_at": row.get("oldest_recording_at"),
                "newest_recording_at": row.get("newest_recording_at"),
                "status_counts": dict(row.get("status_counts") or {}),
                "integrity_status_counts": dict(row.get("integrity_status_counts") or {}),
                "reconciliation_status_counts": dict(row.get("reconciliation_status_counts") or {}),
            }
        )
    return safe_rows


def _storage_problem_details(reconciliation: dict, namespace_observations: dict) -> dict:
    labels = {
        "missing_file": "файл отсутствует",
        "orphan_file": "файл без записи в базе",
        "invalid_path": "некорректный путь",
        "path_outside_storage": "путь вне хранилища",
    }
    reasons = {
        "missing_file": "Строка метаданных ссылается на файл, который не виден на диске. Удаление метаданных этим экраном не выполняется.",
        "orphan_file": "Файл не имеет доверенной строки метаданных KM VMS, поэтому его нельзя удалить или присвоить автоматически.",
        "invalid_path": "Путь в метаданных некорректен и требует ручной проверки.",
        "path_outside_storage": "Путь выходит за границы настроенного хранилища и не может исправляться автоматически.",
    }
    category_counts = {
        "missing_file": int(reconciliation.get("missing_file_count") or 0),
        "orphan_file": int(reconciliation.get("orphan_file_count") or 0),
        "invalid_path": int(reconciliation.get("invalid_path_count") or 0),
        "path_outside_storage": int(reconciliation.get("path_outside_storage_count") or 0),
    }
    category_counts = {key: value for key, value in category_counts.items() if value > 0}
    samples = []
    for item in namespace_observations.get("samples") or []:
        if len(samples) >= MAX_SAMPLE_ITEMS:
            break
        relative_path = str(item.get("relative_path") or "")
        samples.append(
            {
                "category": "orphan_file",
                "sample_name": Path(relative_path).name if relative_path else None,
                "relative_path_redacted": bool(relative_path),
                "archive_root_id": item.get("archive_root_id"),
            }
        )
    return {
        "total_problem_count": int(sum(category_counts.values())),
        "category_counts": category_counts,
        "categories": [
            {
                "code": code,
                "label_ru": labels.get(code, code),
                "count": count,
                "safe_action_status": "manual_review_required",
                "reason_no_action_available": reasons.get(code, "Нет безопасного автоматического действия для этой категории."),
            }
            for code, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "samples": samples,
        "safe_action_contract": "Status summary is read-only: no file deletion, import, adoption, or automatic metadata mutation.",
        "raw_absolute_paths_included": False,
    }


def _build_storage_operations_summary(db: Session, summary: dict) -> dict:
    from app.services.recording_retention import automatic_retention_status, auto_free_space_status

    capacity = summary.get("capacity") or {}
    total = capacity.get("total_bytes")
    used = capacity.get("used_bytes")
    free = capacity.get("free_bytes")
    policy = summary.get("auto_free_space_policy") or {}
    retention = automatic_retention_status()
    auto_cleanup = auto_free_space_status()
    reconciliation = summary.get("reconciliation_summary") or {}
    cleanup = summary.get("cleanup_candidates_summary") or {}
    namespace = summary.get("namespace_observations") or {}
    owned = summary.get("owned_archive") or {}
    path_checks = summary.get("storage_path_checks") or {}
    archive_roots = summary.get("archive_roots") or []
    operation_archive_roots = [
        {key: value for key, value in root.items() if key not in {"configured_path", "root_path", "path", "archive_host_path"}}
        for root in archive_roots
    ]
    active_root = next((root for root in operation_archive_roots if root.get("is_active")), None)

    retention_last = _safe_last_summary(retention.get("last_summary"))
    auto_last = _safe_last_summary(auto_cleanup.get("last_summary"))

    return {
        "checked_at": summary.get("checked_at"),
        "status": summary.get("status") or "unknown",
        "capacity": {
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "available_bytes": capacity.get("available_bytes"),
            "usage_percent": _capacity_percent(used, total),
            "free_percent": _capacity_percent(free, total),
            "filesystem_probe_status": capacity.get("filesystem_probe_status"),
        },
        "path_health": {
            "available": bool(summary.get("available")),
            "readable": bool(path_checks.get("readable")),
            "writable": bool(path_checks.get("writable")),
            "filesystem_probe_status": capacity.get("filesystem_probe_status"),
            "status": path_checks.get("status"),
            "reason": path_checks.get("last_error"),
        },
        "archive_roots": operation_archive_roots,
        "active_archive_root": active_root,
        "migration_preview": summary.get("migration_preview"),
        "namespace_health": {
            "storage_namespace": summary.get("storage_namespace"),
            "namespace_exists": namespace.get("namespace_exists"),
            "namespace_status": "available" if bool(namespace.get("namespace_exists")) or not summary.get("available") is False else "unknown",
            "scan_mode": summary.get("scan_mode"),
            "scan_limited": bool(summary.get("scan_limited")),
            "partial": bool(summary.get("partial")),
            "partial_reason": summary.get("partial_reason"),
            "scanned_files": int(namespace.get("scanned_files") or 0),
            "scanned_dirs": int(namespace.get("scanned_dirs") or 0),
        },
        "owned_archive": {
            "size_bytes": int(owned.get("kmvms_owned_archive_size_bytes") or owned.get("kmvms_owned_archive_bytes") or 0),
            "segments_count": int(owned.get("kmvms_owned_segments_count") or 0),
            "existing_file_count": int(owned.get("kmvms_owned_existing_file_count") or 0),
            "missing_file_count": int(owned.get("kmvms_owned_missing_file_count") or 0),
            "problem_file_count": int(owned.get("kmvms_owned_problem_file_count") or 0),
            "skipped_foreign_metadata_rows": int(owned.get("skipped_foreign_metadata_rows") or 0),
            "deleted_metadata_rows_excluded": int(owned.get("deleted_metadata_rows_excluded") or 0),
        },
        "per_camera_usage": _safe_camera_usage(summary.get("camera_usage")),
        "low_disk_policy": {
            "state": policy.get("state") or "unknown",
            "policy_state": "ON" if policy.get("auto_free_space_cleanup_enabled") else "OFF",
            "auto_free_space_cleanup_enabled": bool(policy.get("auto_free_space_cleanup_enabled")),
            "warning_threshold_percent": policy.get("warning_threshold_percent"),
            "cleanup_threshold_percent": policy.get("cleanup_threshold_percent"),
            "critical_threshold_percent": policy.get("critical_threshold_percent"),
            "cleanup_allowed": bool(policy.get("cleanup_allowed")),
            "critical_recording_suspend_required": bool(policy.get("critical_recording_suspend_required")),
            "recording_suspended_by_low_disk": bool(policy.get("recording_suspended_by_low_disk")),
            "free_percent": policy.get("free_percent"),
            "free_bytes": policy.get("free_bytes"),
        },
        "auto_free_space_cleanup": {
            "enabled": bool(auto_cleanup.get("enabled")),
            "running": bool(auto_cleanup.get("running")),
            "last_started_at": auto_cleanup.get("last_started_at"),
            "last_finished_at": auto_cleanup.get("last_finished_at"),
            "last_status": auto_cleanup.get("last_status") or "never_run",
            "last_trigger": auto_cleanup.get("last_trigger"),
            "last_error": redact_text(str(auto_cleanup.get("last_error") or "")) or None,
            "run_count": int(auto_cleanup.get("run_count") or 0),
            "last_summary": auto_last,
        },
        "retention": {
            "enabled": retention.get("enabled"),
            "running": bool(retention.get("running")),
            "last_started_at": retention.get("last_started_at"),
            "last_finished_at": retention.get("last_finished_at"),
            "last_status": retention.get("last_status") or "never_run",
            "last_error": redact_text(str(retention.get("last_error") or "")) or None,
            "run_count": int(retention.get("run_count") or 0),
            "last_summary": retention_last,
        },
        "reconciliation": {
            "status": "problems_found" if any(int(reconciliation.get(key) or 0) for key in ("missing_file_count", "orphan_file_count", "invalid_path_count", "path_outside_storage_count")) else "ok",
            "missing_file_count": int(reconciliation.get("missing_file_count") or 0),
            "orphan_file_count": int(reconciliation.get("orphan_file_count") or 0),
            "invalid_path_count": int(reconciliation.get("invalid_path_count") or 0),
            "path_outside_storage_count": int(reconciliation.get("path_outside_storage_count") or 0),
            "foreign_unknown_count": int(reconciliation.get("foreign_unknown_count") or 0),
            "problem_file_count": int(
                sum(int(reconciliation.get(key) or 0) for key in ("missing_file_count", "orphan_file_count", "invalid_path_count", "path_outside_storage_count"))
            ),
            "cleanup_candidate_count": int(cleanup.get("count") or 0),
            "cleanup_review_only": True,
            "problem_details": reconciliation.get("problem_details") or {},
            "scan_limited": bool(summary.get("scan_limited")),
            "partial": bool(summary.get("partial")),
            "last_checked_at": summary.get("checked_at"),
        },
        "recent_operations": {
            "available": False,
            "items": [],
            "note": "No safe bounded operation history source is exposed in Stage 3; use current retention/reconciliation summaries.",
        },
    }


def build_storage_monitoring_summary(
    db: Session,
    *,
    include_namespace_observations: bool = True,
    write_audit: bool = False,
    audit_actor=None,
) -> dict:
    root = _storage_root_path()
    archive_root_rows = list_archive_roots(db)
    checked_at = _utc_now()
    warnings: list[str] = []
    errors: list[str] = []

    path_checks = _path_checks(root)
    capacity, capacity_error = _capacity(root) if path_checks["path_exists"] and path_checks["is_dir"] else (
        {
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "available_bytes": None,
            "filesystem_probe_status": "storage_unavailable",
        },
        path_checks.get("last_error"),
    )
    if capacity_error:
        errors.append(capacity_error)

    cameras = {camera.id: camera for camera in db.query(Camera).order_by(Camera.id.asc()).all()}
    camera_usage: dict[int, dict] = {
        camera_id: _empty_camera_usage(camera)
        for camera_id, camera in cameras.items()
        if camera.deleted_at is None
    }
    status_counts = Counter()
    integrity_counts = Counter()
    reconciliation_counts = Counter()
    container_counts = Counter()
    extension_counts = Counter()
    owned_paths: set[str] = set()
    invalid_path_count = 0
    path_outside_count = 0
    owned_existing_count = 0
    owned_missing_count = 0
    owned_problem_count = 0
    owned_archive_size = 0
    skipped_foreign_metadata = 0
    deleted_metadata_rows = 0
    archive_roots = []
    for root_row in archive_root_rows:
        root_status = archive_root_public_status(root_row, include_path=True)
        root_status.update(root_usage(db, root_row))
        archive_roots.append(root_status)

    segments = db.query(RecordingSegment).order_by(RecordingSegment.id.asc()).all()
    for segment in segments:
        if not _is_kmvms_owned(segment):
            skipped_foreign_metadata += 1
            continue
        if segment.status == SEGMENT_STATUS_DELETED:
            deleted_metadata_rows += 1
            continue
        if not _is_countable_archive_segment(segment):
            status_counts[segment.status or "unknown"] += 1
            integrity_counts[segment.integrity_status or "unknown"] += 1
            reconciliation_counts[segment.reconciliation_status or "unknown"] += 1
            container_counts[segment.container_format or "unknown"] += 1
            extension_counts[segment.file_extension or "unknown"] += 1
            continue

        row = camera_usage.setdefault(segment.camera_id, _empty_camera_usage(cameras.get(segment.camera_id)))
        if row["camera_id"] is None:
            row["camera_id"] = segment.camera_id
        camera = cameras.get(segment.camera_id)
        if camera and camera.deleted_at is not None:
            row["camera_name"] = _safe_camera_usage_label(
                segment.camera_name_snapshot,
                camera.name,
            )
        elif not row.get("camera_name") or _is_technical_deleted_camera_label(row.get("camera_name")):
            row["camera_name"] = _safe_camera_usage_label(
                segment.camera_name_snapshot,
                camera.name if camera else None,
            )
        row["recording_count"] += 1
        row["segment_count"] += 1
        _update_range(row, segment.started_at)
        _bump_counter_map(row, "status_counts", segment.status)
        _bump_counter_map(row, "integrity_status_counts", segment.integrity_status)
        _bump_counter_map(row, "container_format_counts", segment.container_format)
        _bump_counter_map(row, "file_extension_counts", segment.file_extension)
        status_counts[segment.status or "unknown"] += 1
        integrity_counts[segment.integrity_status or "unknown"] += 1
        reconciliation_counts[segment.reconciliation_status or "unknown"] += 1
        container_counts[segment.container_format or "unknown"] += 1
        extension_counts[segment.file_extension or "unknown"] += 1

        rel_path, size, problem, _target = _safe_stat_segment(segment, root)
        if rel_path:
            owned_paths.add(rel_path)
        if problem is None and size is not None:
            owned_existing_count += 1
            row["existing_file_count"] += 1
            row["size_bytes"] += size
            owned_archive_size += size
        else:
            owned_problem_count += 1
            row["problem_file_count"] += 1
            if problem == "missing_file":
                owned_missing_count += 1
                row["missing_file_count"] += 1
            elif problem == "path_outside_storage":
                path_outside_count += 1
            elif problem == "invalid_path":
                invalid_path_count += 1
            if problem:
                warnings.append(f"segment_{segment.id}:{problem}")

    namespace_observations = {
        "scan_mode": SCAN_MODE_METADATA_ONLY,
        "scan_limited": False,
        "partial": False,
        "partial_reason": None,
        "orphan_file_count": 0,
        "foreign_unknown_count": 0,
        "samples": [],
        "errors": [],
    }
    if include_namespace_observations and path_checks["path_exists"] and path_checks["is_dir"]:
        namespace_observations = _observe_namespace(root, owned_paths)
        errors.extend(namespace_observations.get("errors") or [])

    scan_limited = bool(namespace_observations.get("scan_limited"))
    partial = bool(namespace_observations.get("partial"))
    path_available = bool(path_checks["path_exists"] and path_checks["is_dir"])
    integrity_problem_count = int(owned_problem_count) + int(namespace_observations.get("orphan_file_count") or 0) + int(invalid_path_count) + int(path_outside_count)
    has_storage_problem = bool(errors or integrity_problem_count)
    if not path_available:
        status = "unavailable"
    elif has_storage_problem:
        status = "degraded"
    else:
        status = "available"

    cleanup_candidates_summary = {
        "mode": "read_only_observability",
        "count": int(namespace_observations.get("orphan_file_count") or 0),
        "samples": namespace_observations.get("samples") or [],
        "note": "Not a retention planner, not a deletion dry-run, no files are deleted or auto-owned.",
    }

    summary = {
        "status": status,
        "ok": status == "available",
        "available": path_available,
        "checked_at": checked_at,
        "checked_at_utc": checked_at,
        "checked_at_system": format_system_iso(datetime.fromisoformat(checked_at.removesuffix("Z")), timezone_context(db)),
        "storage_contract": storage_contract(),
        "container_runtime_storage_root": str(root),
        "container_recordings_namespace_root": str(root / KMVMS_RECORDINGS_NAMESPACE),
        "storage_namespace": KMVMS_RECORDINGS_NAMESPACE,
        "storage_root": str(root),
        "kmvms_namespace_root": str(root / KMVMS_RECORDINGS_NAMESPACE),
        "scan_mode": namespace_observations.get("scan_mode") or SCAN_MODE_METADATA_ONLY,
        "scan_limited": scan_limited,
        "partial": partial,
        "partial_reason": namespace_observations.get("partial_reason"),
        "warnings": warnings[:MAX_SAMPLE_ITEMS],
        "errors": errors[:MAX_SAMPLE_ITEMS],
        "capacity": capacity,
        "mount_status": {
            "path_available": path_available,
            "filesystem_probe_status": capacity.get("filesystem_probe_status"),
            "mount_point": path_checks.get("storage_root_realpath"),
        },
        "storage_path_checks": path_checks,
        "owned_archive": {
            "kmvms_owned_archive_bytes": int(owned_archive_size),
            "kmvms_owned_archive_size_bytes": int(owned_archive_size),
            "kmvms_owned_segments_count": int(sum(status_counts.values())),
            "kmvms_owned_existing_file_count": int(owned_existing_count),
            "kmvms_owned_missing_file_count": int(owned_missing_count),
            "kmvms_owned_problem_file_count": int(owned_problem_count),
            "skipped_foreign_metadata_rows": int(skipped_foreign_metadata),
            "deleted_metadata_rows_excluded": int(deleted_metadata_rows),
        },
        "camera_usage": sorted(camera_usage.values(), key=lambda item: item.get("camera_id") or 0),
        "segment_status_counts": dict(status_counts),
        "recording_status_counts": dict(status_counts),
        "integrity_status_counts": dict(integrity_counts),
        "reconciliation_status_counts": dict(reconciliation_counts),
        "container_format_counts": dict(container_counts),
        "file_extension_counts": dict(extension_counts),
        "reconciliation_summary": {
            "missing_file_count": int(owned_missing_count),
            "invalid_path_count": int(invalid_path_count),
            "path_outside_storage_count": int(path_outside_count),
            "orphan_file_count": int(namespace_observations.get("orphan_file_count") or 0),
            "foreign_unknown_count": int(namespace_observations.get("foreign_unknown_count") or 0),
            "deleted_files_count": 0,
            "deleted_product_metadata_count": 0,
        },
        "cleanup_candidates_summary": cleanup_candidates_summary,
        "namespace_observations": namespace_observations,
        "archive_roots": archive_roots,
        "migration_preview": migration_preview(db),
    }
    summary["reconciliation_summary"]["problem_details"] = _storage_problem_details(
        summary["reconciliation_summary"],
        namespace_observations,
    )
    from app.services.recording_retention import low_disk_policy_status

    summary["auto_free_space_policy"] = low_disk_policy_status(db, summary)
    summary["storage_operations"] = _build_storage_operations_summary(db, summary)
    if write_audit:
        _maybe_audit_storage_transition(db, summary, actor=audit_actor)
    return summary
