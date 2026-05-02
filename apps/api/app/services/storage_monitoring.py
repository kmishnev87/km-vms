from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import os
import shutil
import time

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.services.audit_log import redact_text
from app.services.recording_storage import KMVMS_RECORDINGS_NAMESPACE, VIDEO_EXTENSIONS

OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
SEGMENT_STATUS_DELETED = "deleted"
SCAN_MODE_METADATA_ONLY = "metadata_only"
SCAN_MODE_NAMESPACE_BOUNDED = "namespace_bounded"
MAX_NAMESPACE_FILES = 1000
MAX_NAMESPACE_DIRS = 300
MAX_SCAN_SECONDS = 3.0
MAX_SAMPLE_ITEMS = 50


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


def _empty_camera_usage(camera: Camera | None = None) -> dict:
    return {
        "camera_id": camera.id if camera else None,
        "camera_name": camera.name if camera else None,
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


def _safe_stat_segment(segment: RecordingSegment, root: Path) -> tuple[str | None, int | None, str | None, Path | None]:
    rel_path, path_error, target = _segment_relative(segment, root)
    if path_error:
        return rel_path, None, path_error, target
    if target is None:
        return rel_path, None, "invalid_path", target
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


def build_storage_monitoring_summary(db: Session, *, include_namespace_observations: bool = True) -> dict:
    root = _storage_root_path()
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
    camera_usage: dict[int, dict] = {camera_id: _empty_camera_usage(camera) for camera_id, camera in cameras.items()}
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

    segments = db.query(RecordingSegment).order_by(RecordingSegment.id.asc()).all()
    for segment in segments:
        if not _is_kmvms_owned(segment):
            skipped_foreign_metadata += 1
            continue
        if segment.status == SEGMENT_STATUS_DELETED:
            deleted_metadata_rows += 1
            continue

        row = camera_usage.setdefault(segment.camera_id, _empty_camera_usage(cameras.get(segment.camera_id)))
        if row["camera_id"] is None:
            row["camera_id"] = segment.camera_id
            row["camera_name"] = segment.camera_name_snapshot
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
    if errors or partial or owned_problem_count or not path_checks["path_exists"] or not path_checks["is_dir"]:
        status = "degraded" if path_checks["path_exists"] and path_checks["is_dir"] else "unavailable"
    else:
        status = "available"

    cleanup_candidates_summary = {
        "mode": "read_only_observability",
        "count": int(namespace_observations.get("orphan_file_count") or 0),
        "samples": namespace_observations.get("samples") or [],
        "note": "Not a retention planner, not a deletion dry-run, no files are deleted or auto-owned.",
    }

    return {
        "status": status,
        "ok": status == "available",
        "available": status == "available",
        "checked_at": checked_at,
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
            "path_available": bool(path_checks["path_exists"] and path_checks["is_dir"]),
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
    }
