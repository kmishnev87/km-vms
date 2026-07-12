from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from app.core.config import settings

CONTAINER_ARCHIVE_PATH = "/storage/archive"
DISCOVERY_FILE = "storage-discovery.json"
DISCOVERY_REQUEST_FILE = "storage-discovery-request.json"
DISCOVERY_REQUEST_CONTROL_FILE = "storage-discovery-request.control"
DISCOVERY_RESULT_FILE = "storage-discovery-result.json"
DISCOVERY_LOCK_FILE = "storage-discovery-request.lock"
ROOT_CLEANUP_REQUEST_FILE = "storage-root-cleanup-request.json"
ROOT_CLEANUP_REQUEST_CONTROL_FILE = "storage-root-cleanup-request.control"
ROOT_CLEANUP_RESULT_FILE = "storage-root-cleanup-result.json"
SELECTION_FILE = "storage-selection.json"
SELECTION_CONTROL_FILE = "storage-selection.control"
APPLY_STATUS_FILE = "storage-apply-status.json"
ACTIVATION_REQUEST_FILE = "storage-activation-request.json"
ACTIVATION_REQUEST_CONTROL_FILE = "storage-activation-request.control"
SETUP_COMPLETE_FILE = "setup-complete.json"
MARKER_FILE = ".km-vms-storage-root.json"
ACTIVE_SELECTION_STATUSES = {"active"}
IN_PROGRESS_SELECTION_STATUSES = {"activation_requested", "activation_in_progress", "applied_restart_required"}
FAILED_SELECTION_STATUSES = {"activation_failed", "validation_failed"}
DISCOVERY_CURRENT_SECONDS = 120
DISCOVERY_REQUEST_TIMEOUT_SECONDS = 20
ROOT_CLEANUP_TIMEOUT_SECONDS = 30
DISCOVERY_LOCK_STALE_SECONDS = 60
BLOCKED_PATHS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run",
    "/sbin", "/sys", "/usr", "/var", "/root", "/tmp",
}
BLOCKED_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "tmpfs",
    "overlay", "squashfs", "aufs", "debugfs", "tracefs", "securityfs",
    "pstore", "configfs", "mqueue", "hugetlbfs", "fusectl", "autofs",
}
PRIMARY_ROOT_PATTERNS = (
    re.compile(r"^/Volume\d+$"),
    re.compile(r"^/volume\d+$"),
    re.compile(r"^/share(?:/[^/]+)?$"),
    re.compile(r"^/mnt/(?:user|disk\d+|[^/]+)$"),
    re.compile(r"^/srv(?:/[^/]+)?$"),
    re.compile(r"^/data(?:/[^/]+)?$"),
    re.compile(r"^/media/[^/]+$"),
)
ROOT_CLEANUP_IMMEDIATE_REASONS = {
    "archive_root_cleanup_helper_timeout",
    "archive_root_cleanup_helper_failed",
    "root_directory_remove_failed",
    "metadata_update_failed_after_file_delete",
    "runtime_state_finalize_failed",
    "runtime_manifest_recovery_failed",
    "destructive_scope_conflict",
    "destructive_scope_lease_lost",
}
ROOT_CLEANUP_REFRESH_REASONS = {
    "selected_mount_missing",
    "storage_discovery_refresh_failed",
    "archive_root_cleanup_identity_revalidation_failed",
}
ROOT_CLEANUP_EXTERNAL_FIX_REASONS = {
    "selected_mount_not_readable",
    "selected_mount_not_searchable",
    "selected_mount_not_writable",
    "root_marker_remove_failed",
    "filesystem_delete_failed",
}
ROOT_CLEANUP_COMPLETED_STATUSES = {"completed_removed", "completed_preserved_nonempty"}


def _same_or_child(path: Path, blocked: Path) -> bool:
    try:
        resolved = path.resolve()
        blocked_resolved = blocked.resolve()
    except OSError:
        resolved = path
        blocked_resolved = blocked
    return resolved == blocked_resolved or blocked_resolved in resolved.parents


def install_control_dir() -> Path:
    return Path(settings.storage_install_control)


def archive_root_cleanup_capability(reason: str | None, cleanup_status: str | None = None) -> dict[str, object]:
    normalized_reason = str(reason or "").strip()[:120]
    normalized_status = str(cleanup_status or "").strip()[:64]
    if normalized_status in ROOT_CLEANUP_COMPLETED_STATUSES:
        retry_mode = "none"
        next_action = "close"
    elif normalized_reason in ROOT_CLEANUP_IMMEDIATE_REASONS:
        retry_mode = "immediate"
        next_action = "retry_cleanup"
    elif normalized_reason in ROOT_CLEANUP_REFRESH_REASONS:
        retry_mode = "after_refresh"
        next_action = "refresh_storage_state"
    elif normalized_reason in ROOT_CLEANUP_EXTERNAL_FIX_REASONS:
        retry_mode = "after_external_fix"
        next_action = "correct_storage_access"
    else:
        retry_mode = "none"
        next_action = "close"
    return {
        "retry_mode": retry_mode,
        "next_action": next_action,
        "retry_available": retry_mode == "immediate",
    }


def normalize_archive_root_cleanup_result(result: dict) -> dict:
    normalized = dict(result or {})
    reason = str(normalized.get("reason") or "archive_root_cleanup_unknown")[:120]
    cleanup_status = str(normalized.get("cleanup_status") or "partial_cleanup")[:64]
    normalized["reason"] = reason
    normalized["cleanup_status"] = cleanup_status
    normalized.update(archive_root_cleanup_capability(reason, cleanup_status))
    return normalized


def _safe_read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _control_value(value: object) -> str:
    text = str(value or "")
    if "\n" in text or "\r" in text or "\0" in text:
        raise ValueError("control value contains unsafe characters")
    return text


def _write_control(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    lines = [f"{key}={_control_value(value)}" for key, value in payload.items()]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _candidate_id(path: str) -> str:
    import zlib

    return f"mount-{zlib.crc32(path.encode('utf-8')) & 0xffffffff}"


def _format_label(path: str) -> str:
    value = str(path or "").strip()
    if re.fullmatch(r"/Volume\d+", value) or re.fullmatch(r"/volume\d+", value):
        return value.lstrip("/")
    if value.startswith("/share/"):
        return value.split("/", 3)[2]
    if value.startswith("/mnt/"):
        return value.rsplit("/", 1)[-1]
    if value.startswith("/media/"):
        return value.rsplit("/", 1)[-1]
    return value


def _is_primary_root(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in PRIMARY_ROOT_PATTERNS)


def _path_block_reason(path: str, fstype: str | None = None) -> str:
    value = str(path or "").strip()
    if not value.startswith("/"):
        return "not_absolute"
    candidate_path = Path(value)
    if value == "/" or any(_same_or_child(candidate_path, Path(blocked)) for blocked in BLOCKED_PATHS if blocked != "/"):
        return "dangerous_system_path"
    if value.startswith("/var/lib/docker") or "/overlay" in value or "/containers/" in value:
        return "docker_internal_path"
    parts = set(Path(value).parts)
    if ".git" in parts or ".env" in parts or "secrets" in parts or "credentials" in parts:
        return "sensitive_or_service_path"
    if fstype and fstype in BLOCKED_FSTYPES:
        return "unsupported_pseudo_filesystem"
    return ""


def _candidate_rank(path: str, writable: bool, free_bytes: int) -> tuple[int, int]:
    primary = 1 if _is_primary_root(path) else 0
    return (primary if writable else -1, int(free_bytes or 0))


def sanitize_candidate(raw: dict) -> dict:
    path = str(raw.get("path") or "").strip()
    fstype = str(raw.get("filesystem_type") or "").strip()
    raw_writable = bool(raw.get("writable"))
    reason = _path_block_reason(path, fstype) or str(raw.get("reason") or "")
    writable = raw_writable and not reason
    visibility = "primary" if writable and _is_primary_root(path) else "manual_only"
    if reason:
        visibility = "hidden"
    return {
        "id": str(raw.get("id") or _candidate_id(path)),
        "path": path,
        "label": str(raw.get("label") or _format_label(path)),
        "filesystem_type": fstype,
        "physical_identity": str(raw.get("physical_identity") or "").strip() or None,
        "total_bytes": int(raw.get("total_bytes") or 0),
        "used_bytes": int(raw.get("used_bytes") or 0),
        "free_bytes": int(raw.get("free_bytes") or 0),
        "writable": writable,
        "safety_status": "allowed" if writable else "blocked",
        "reason": reason,
        "ui_visibility": visibility,
        "recommended": False,
    }


def _sorted_candidates(items: list[dict]) -> list[dict]:
    ordered = sorted(
        items,
        key=lambda item: (
            0 if item["ui_visibility"] == "primary" else 1,
            -_candidate_rank(item["path"], item["writable"], item["free_bytes"])[0],
            -int(item["free_bytes"] or 0),
            item["path"],
        ),
    )
    first_primary = next((item for item in ordered if item["ui_visibility"] == "primary" and item["writable"]), None)
    if first_primary:
        first_primary["recommended"] = True
    return ordered


def _public_candidate(item: dict) -> dict:
    return {
        key: value
        for key, value in item.items()
        if key not in {"physical_identity", "filesystem_type"}
    }


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def discovery_snapshot() -> dict:
    path = install_control_dir() / DISCOVERY_FILE
    payload = _safe_read_json(path)
    raw_candidates = [sanitize_candidate(item) for item in payload.get("candidates") or [] if isinstance(item, dict)]
    ordered = _sorted_candidates(raw_candidates)
    primary = [_public_candidate(item) for item in ordered if item["ui_visibility"] == "primary"]
    hidden_count = sum(1 for item in ordered if item["ui_visibility"] == "hidden")
    manual_count = sum(1 for item in ordered if item["ui_visibility"] == "manual_only")
    created_at = _parse_utc(payload.get("created_at"))
    age_seconds = max(0, int((datetime.utcnow() - created_at).total_seconds())) if created_at else None
    freshness = "current" if age_seconds is not None and age_seconds <= DISCOVERY_CURRENT_SECONDS else ("stale" if payload else "unavailable")
    available = bool(primary) and bool(payload.get("host_visibility")) and freshness == "current"
    return {
        "available": available,
        "discovery_source": payload.get("discovery_source") or "unavailable",
        "host_visibility": bool(payload.get("host_visibility")),
        "created_at": payload.get("created_at"),
        "snapshot_id": payload.get("snapshot_id"),
        "age_seconds": age_seconds,
        "freshness": freshness,
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "candidates": primary,
        "manual_path_supported": False,
        "hidden_candidate_count": hidden_count,
        "manual_candidate_count": manual_count,
        "status": "ready" if available else freshness,
    }


def _all_discovered_candidates() -> list[dict]:
    path = install_control_dir() / DISCOVERY_FILE
    payload = _safe_read_json(path)
    return [sanitize_candidate(item) for item in payload.get("candidates") or [] if isinstance(item, dict)]


def _acquire_discovery_lock(request_id: str) -> bool:
    path = install_control_dir() / DISCOVERY_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        if age <= DISCOVERY_LOCK_STALE_SECONDS:
            return False
        stale = path.with_name(f"{path.name}.stale.{uuid.uuid4().hex}")
        try:
            os.replace(path, stale)
        except OSError:
            return False
        try:
            stale.unlink()
        except OSError:
            pass
        return _acquire_discovery_lock(request_id)
    try:
        os.write(descriptor, f"{request_id}\n".encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _release_discovery_lock(request_id: str) -> None:
    path = install_control_dir() / DISCOVERY_LOCK_FILE
    try:
        if path.read_text(encoding="utf-8").strip() == request_id:
            path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _queue_discovery_request(payload: dict) -> None:
    _write_json(install_control_dir() / DISCOVERY_REQUEST_FILE, payload)
    _write_control(
        install_control_dir() / DISCOVERY_REQUEST_CONTROL_FILE,
        {
            "schema_version": payload.get("schema_version", 1),
            "request_id": payload.get("request_id"),
            "mode": payload.get("mode"),
            "candidate_id": payload.get("candidate_id"),
            "expected_snapshot_id": payload.get("expected_snapshot_id"),
            "expected_physical_identity": payload.get("expected_physical_identity"),
            "folder_name": payload.get("folder_name"),
            "status": payload.get("status"),
        },
    )


def _wait_discovery_result(request_id: str, *, timeout_seconds: int = DISCOVERY_REQUEST_TIMEOUT_SECONDS) -> dict:
    deadline = time.monotonic() + timeout_seconds
    result_path = install_control_dir() / DISCOVERY_RESULT_FILE
    while time.monotonic() < deadline:
        result = _safe_read_json(result_path)
        if str(result.get("request_id") or "") == request_id and str(result.get("status") or "") in {"completed", "failed"}:
            return result
        time.sleep(0.25)
    return {"request_id": request_id, "status": "failed", "error": "storage_discovery_refresh_timeout"}


def request_archive_root_cleanup(
    root_row,
    *,
    operation_id: str,
    marker_already_removed: bool = False,
    timeout_seconds: int = ROOT_CLEANUP_TIMEOUT_SECONDS,
) -> dict:
    root_id = str(getattr(root_row, "id", "") or "").strip()
    physical_identity = str(getattr(root_row, "physical_identity", "") or "").strip()
    host_path = Path(str(getattr(root_row, "root_path", "") or "").strip())
    if not root_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", operation_id or ""):
        raise ValueError("archive_root_cleanup_identity_invalid")
    if not physical_identity:
        raise ValueError("archive_root_cleanup_physical_identity_missing")
    if not host_path.is_absolute() or not host_path.name or host_path.name in {".", ".."}:
        raise ValueError("archive_root_cleanup_path_invalid")
    selected_mount = host_path.parent
    if ".." in host_path.parts or len(host_path.parts) < 3 or _path_block_reason(str(selected_mount), ""):
        raise ValueError("archive_root_cleanup_path_outside_discovered_volume")
    folder_name = validate_folder_name(host_path.name)
    if host_path != selected_mount / folder_name:
        raise ValueError("archive_root_cleanup_path_invalid")
    request_id = f"storage-root-cleanup-{uuid.uuid4().hex}"
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "operation_id": operation_id,
        "archive_root_id": root_id,
        "selected_host_path": str(host_path),
        "selected_mount_path": str(selected_mount),
        "folder_name": folder_name,
        "expected_physical_identity": physical_identity,
        "marker_already_removed": bool(marker_already_removed),
        "requested_at": _utc_now(),
        "status": "requested",
    }
    _write_json(install_control_dir() / ROOT_CLEANUP_REQUEST_FILE, payload)
    _write_control(
        install_control_dir() / ROOT_CLEANUP_REQUEST_CONTROL_FILE,
        {
            "schema_version": 1,
            "request_id": request_id,
            "operation_id": operation_id,
            "archive_root_id": root_id,
            "selected_host_path": str(host_path),
            "selected_mount_path": str(selected_mount),
            "folder_name": folder_name,
            "expected_physical_identity": physical_identity,
            "marker_already_removed": "true" if marker_already_removed else "false",
            "status": "requested",
        },
    )
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    result_path = install_control_dir() / ROOT_CLEANUP_RESULT_FILE
    while time.monotonic() < deadline:
        result = _safe_read_json(result_path)
        if (
            str(result.get("request_id") or "") == request_id
            and str(result.get("operation_id") or "") == operation_id
            and str(result.get("archive_root_id") or "") == root_id
            and str(result.get("status") or "") in {"completed", "partial", "failed"}
        ):
            return normalize_archive_root_cleanup_result(result)
        time.sleep(0.25)
    return normalize_archive_root_cleanup_result({
        "schema_version": 1,
        "request_id": request_id,
        "operation_id": operation_id,
        "archive_root_id": root_id,
        "status": "partial",
        "cleanup_status": "partial_cleanup",
        "reason": "archive_root_cleanup_helper_timeout",
        "marker_removed": bool(marker_already_removed),
        "root_directory_removed": False,
    })


def request_discovery_refresh(*, timeout_seconds: int = DISCOVERY_REQUEST_TIMEOUT_SECONDS) -> dict:
    request_id = f"storage-discovery-{uuid.uuid4().hex}"
    if not _acquire_discovery_lock(request_id):
        snapshot = discovery_snapshot()
        return {**snapshot, "refresh_in_progress": True, "status": "refreshing"}
    try:
        _queue_discovery_request(
            {
                "schema_version": 1,
                "request_id": request_id,
                "mode": "refresh",
                "requested_at": _utc_now(),
                "status": "requested",
            }
        )
        result = _wait_discovery_result(request_id, timeout_seconds=timeout_seconds)
        snapshot = discovery_snapshot()
        if result.get("status") != "completed":
            return {
                **snapshot,
                "available": False,
                "refresh_error": str(result.get("error") or "storage_discovery_refresh_failed"),
                "status": "stale" if snapshot.get("snapshot_id") else "unavailable",
                "freshness": "stale" if snapshot.get("snapshot_id") else "unavailable",
            }
        return {**snapshot, "refresh_in_progress": False}
    finally:
        _release_discovery_lock(request_id)


def revalidate_discovery_candidate(
    candidate_id: str,
    snapshot_id: str,
    folder_name: str,
    *,
    timeout_seconds: int = DISCOVERY_REQUEST_TIMEOUT_SECONDS,
) -> dict:
    cached_payload = _safe_read_json(install_control_dir() / DISCOVERY_FILE)
    if not snapshot_id or str(cached_payload.get("snapshot_id") or "") != str(snapshot_id):
        raise ValueError("storage_discovery_snapshot_stale")
    cached_candidates = {
        item["id"]: item
        for item in (sanitize_candidate(raw) for raw in cached_payload.get("candidates") or [] if isinstance(raw, dict))
    }
    cached = cached_candidates.get(candidate_id)
    if not cached or cached.get("safety_status") != "allowed":
        raise ValueError("storage candidate is not available")
    expected_identity = str(cached.get("physical_identity") or "")
    if not expected_identity:
        raise ValueError("storage_candidate_identity_unavailable")
    safe_folder = validate_folder_name(folder_name)
    request_id = f"storage-revalidate-{uuid.uuid4().hex}"
    if not _acquire_discovery_lock(request_id):
        raise ValueError("storage_discovery_refresh_in_progress")
    try:
        _queue_discovery_request(
            {
                "schema_version": 1,
                "request_id": request_id,
                "mode": "candidate_revalidate",
                "candidate_id": candidate_id,
                "expected_snapshot_id": snapshot_id,
                "expected_physical_identity": expected_identity,
                "folder_name": safe_folder,
                "requested_at": _utc_now(),
                "status": "requested",
            }
        )
        result = _wait_discovery_result(request_id, timeout_seconds=timeout_seconds)
        if result.get("status") != "completed":
            raise ValueError(str(result.get("error") or "storage_candidate_revalidation_failed"))
        if str(result.get("candidate_id") or "") != candidate_id:
            raise ValueError("storage_candidate_revalidation_mismatch")
        if str(result.get("physical_identity") or "") != expected_identity:
            raise ValueError("storage_candidate_physical_identity_changed")
        if not result.get("writable"):
            raise ValueError("storage_candidate_not_writable")
        return result
    finally:
        _release_discovery_lock(request_id)


def revalidate_configured_archive_root(root_row, *, timeout_seconds: int = DISCOVERY_REQUEST_TIMEOUT_SECONDS) -> dict:
    if root_row is None:
        raise ValueError("archive_root_missing")
    host_path = Path(str(getattr(root_row, "root_path", "") or ""))
    if host_path.as_posix() == Path(settings.storage_root).as_posix():
        configured = str(os.getenv("STORAGE_HOST_ROOT") or os.getenv("SURVEILLANCE_ROOT") or "").strip()
        host_path = Path(configured) if configured else host_path
    if not host_path.is_absolute() or not host_path.name:
        raise ValueError("archive_root_host_path_invalid")
    expected_identity = str(getattr(root_row, "physical_identity", None) or "")

    refreshed = request_discovery_refresh(timeout_seconds=timeout_seconds)
    if refreshed.get("freshness") != "current" or not refreshed.get("snapshot_id"):
        raise ValueError(str(refreshed.get("refresh_error") or "storage_discovery_current_snapshot_unavailable"))
    candidates = [
        item
        for item in _all_discovered_candidates()
        if item.get("safety_status") == "allowed"
    ]
    matches: list[tuple[int, dict, str]] = []
    for candidate in candidates:
        mount = Path(str(candidate.get("path") or ""))
        try:
            relative = host_path.relative_to(mount)
        except ValueError:
            continue
        if len(relative.parts) != 1:
            continue
        matches.append((len(mount.parts), candidate, relative.parts[0]))
    if not matches:
        raise ValueError("storage_candidate_disappeared_or_changed")
    _depth, candidate, folder_name = sorted(matches, key=lambda item: item[0], reverse=True)[0]
    candidate_identity = str(candidate.get("physical_identity") or "")
    if not candidate_identity:
        raise ValueError("storage_candidate_identity_unavailable")
    if expected_identity and candidate_identity != expected_identity:
        raise ValueError("storage_candidate_physical_identity_changed")
    result = revalidate_discovery_candidate(
        str(candidate["id"]),
        str(refreshed["snapshot_id"]),
        folder_name,
        timeout_seconds=timeout_seconds,
    )
    if Path(str(result.get("final_host_path") or "")).resolve(strict=False) != host_path.resolve(strict=False):
        raise ValueError("storage_candidate_revalidation_path_mismatch")
    return result


def storage_confirmation_status() -> dict:
    selection = _safe_read_json(install_control_dir() / SELECTION_FILE)
    apply_state = _safe_read_json(install_control_dir() / APPLY_STATUS_FILE)
    selected_host_path = str(selection.get("selected_host_path") or "").strip()
    container_archive_path = str(selection.get("container_archive_path") or CONTAINER_ARCHIVE_PATH).strip()
    apply_status = str(apply_state.get("status") or selection.get("apply_status") or "").strip()
    runtime_request_id = str(apply_state.get("request_id") or selection.get("activation_request_id") or "").strip()
    errors: list[str] = []

    if not selection:
        errors.append("storage_selection_missing")
    if not selected_host_path:
        errors.append("selected_host_path_missing")
    elif selected_host_path == CONTAINER_ARCHIVE_PATH:
        errors.append("selected_host_path_must_be_host_path")
    elif not selected_host_path.startswith("/"):
        errors.append("selected_host_path_must_be_absolute")
    if container_archive_path != CONTAINER_ARCHIVE_PATH:
        errors.append("container_archive_path_invalid")
    if apply_status != "active":
        errors.append("storage_apply_status_not_active")

    safe_selection = deepcopy(selection)
    for key in list(safe_selection):
        if any(marker in str(key).lower() for marker in ("password", "secret", "token", "credential", "authorization")):
            safe_selection.pop(key, None)

    next_action = "select_and_validate_storage"
    if apply_status in IN_PROGRESS_SELECTION_STATUSES:
        next_action = "wait_for_storage_activation"
    elif apply_status in FAILED_SELECTION_STATUSES:
        next_action = "resolve_storage_activation_error"
    elif apply_status == "active":
        next_action = "continue_setup"

    return {
        "ready": not errors,
        "status": apply_status if apply_status else ("unavailable" if not selection else "validation_failed"),
        "allowed_statuses": sorted(ACTIVE_SELECTION_STATUSES),
        "selected_host_path": selected_host_path or None,
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "apply_status": apply_status or None,
        "runtime_request_id": runtime_request_id or None,
        "operation_id": apply_state.get("operation_id") or selection.get("operation_id"),
        "apply_state": apply_state if apply_state else None,
        "restart_required": apply_status in IN_PROGRESS_SELECTION_STATUSES,
        "manual_action_required": apply_status in FAILED_SELECTION_STATUSES,
        "next_action": next_action,
        "errors": errors,
        "selection": safe_selection if selection else None,
    }


def require_storage_confirmation() -> dict:
    status = storage_confirmation_status()
    if not status["ready"]:
        raise ValueError(",".join(status["errors"]) or "storage_confirmation_required")
    return status


def get_candidate(candidate_id: str) -> dict:
    for item in _all_discovered_candidates():
        if item["id"] == candidate_id:
            if item["safety_status"] != "allowed":
                raise ValueError("storage candidate is blocked")
            return item
    raise ValueError("storage candidate is not available")


def validate_manual_root_path(value: str) -> Path:
    path = Path(str(value or "").strip())
    if not str(path):
        raise ValueError("manual_root_path is required")
    if not path.is_absolute():
        raise ValueError("manual_root_path must be absolute")
    reason = _path_block_reason(str(path))
    if reason:
        raise ValueError(reason)
    if not path.exists():
        raise ValueError("manual_root_path does not exist")
    if not path.is_dir():
        raise ValueError("manual_root_path is not a directory")
    if path.is_symlink():
        raise ValueError("manual_root_path must not be a symlink")
    if not os.access(path, os.R_OK | os.X_OK):
        raise ValueError("manual_root_path is not readable")
    if not os.access(path, os.W_OK):
        raise ValueError("manual_root_path is not writable")
    return path.resolve()


def resolve_root(candidate_id: str, manual_root_path: str | None = None) -> dict:
    if manual_root_path:
        root = validate_manual_root_path(manual_root_path)
        usage = shutil.disk_usage(root)
        return {
            "id": "manual",
            "path": str(root),
            "label": str(root),
            "filesystem_type": "manual",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "writable": True,
            "safety_status": "allowed",
            "reason": "",
            "ui_visibility": "manual_only",
            "recommended": False,
        }
    return get_candidate(candidate_id)


def validate_folder_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("folder_name is required")
    if len(name) > 80:
        raise ValueError("folder_name is too long")
    if name in {".", ".."} or name.startswith(".") or "/" in name or "\\" in name or '"' in name:
        raise ValueError("folder_name must be a single safe folder name")
    if any(ord(char) < 32 for char in name):
        raise ValueError("folder_name contains control characters")
    return name


def build_preview(candidate_id: str, folder_name: str, manual_root_path: str | None = None) -> dict:
    candidate = resolve_root(candidate_id, manual_root_path)
    if candidate["safety_status"] != "allowed":
        raise ValueError("storage candidate is blocked")
    name = validate_folder_name(folder_name)
    root = Path(candidate["path"]).resolve()
    final = (root / name).resolve()
    if root == final or root not in final.parents:
        raise ValueError("selected folder escapes storage candidate")
    exists = final.exists()
    marker = final / MARKER_FILE
    is_empty = exists and final.is_dir() and not any(final.iterdir())
    marked = marker.exists() and marker.is_file()
    blockers: list[str] = []
    if exists and not final.is_dir():
        blockers.append("target_exists_not_directory")
    if exists and final.is_symlink():
        blockers.append("target_is_symlink")
    if exists and final.is_dir() and (not is_empty) and not marked:
        blockers.append("non_empty_unmarked_folder")
    action = "check_and_select" if exists else "create_and_select"
    return {
        "candidate_id": candidate["id"],
        "selected_mount_path": candidate["path"],
        "selected_mount_label": candidate["label"],
        "folder_name": name,
        "final_host_path": str(final),
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "exists": exists,
        "is_empty": is_empty,
        "has_km_vms_marker": marked,
        "warnings": [],
        "blockers": blockers,
        "action": action,
        "status": "blocked" if blockers else "ready_to_activate",
        "free_bytes": int(candidate.get("free_bytes") or 0),
        "writable": True,
    }


def validate_and_mark(candidate_id: str, folder_name: str, manual_root_path: str | None = None) -> dict:
    preview = build_preview(candidate_id, folder_name, manual_root_path)
    if preview["blockers"]:
        raise ValueError(",".join(preview["blockers"]))
    preview["host_validation_required"] = True
    preview["write_test"] = {"ok": False, "reason": "activation_helper_pending"}
    preview["status"] = "activation_ready"
    return preview


def _write_apply_status(payload: dict) -> None:
    _write_json(install_control_dir() / APPLY_STATUS_FILE, payload)


def _write_activation_request(payload: dict) -> None:
    _write_json(install_control_dir() / ACTIVATION_REQUEST_FILE, payload)


def _write_selection_control(payload: dict) -> None:
    _write_control(
        install_control_dir() / SELECTION_CONTROL_FILE,
        {
            "schema_version": payload.get("schema_version"),
            "selected_host_path": payload.get("selected_host_path"),
            "selected_mount_path": payload.get("selected_mount_path"),
            "folder_name": payload.get("folder_name"),
            "container_archive_path": payload.get("container_archive_path"),
            "candidate_id": payload.get("candidate_id"),
            "apply_status": payload.get("apply_status"),
            "activation_request_id": payload.get("activation_request_id"),
            "operation_id": payload.get("operation_id"),
            "physical_identity": payload.get("physical_identity"),
        },
    )


def _write_activation_request_control(payload: dict) -> None:
    _write_control(
        install_control_dir() / ACTIVATION_REQUEST_CONTROL_FILE,
        {
            "schema_version": payload.get("schema_version"),
            "request_id": payload.get("request_id"),
            "selected_host_path": payload.get("selected_host_path"),
            "container_archive_path": payload.get("container_archive_path"),
            "status": payload.get("status"),
            "operation_id": payload.get("operation_id"),
        },
    )


def persist_selection(candidate_id: str, folder_name: str, manual_root_path: str | None = None) -> dict:
    result = validate_and_mark(candidate_id, folder_name, manual_root_path)
    request_id = f"setup-storage-{int(datetime.utcnow().timestamp())}"
    selection = {
        "schema_version": 2,
        "selected_host_path": result["final_host_path"],
        "selected_mount_path": result["selected_mount_path"],
        "folder_name": result["folder_name"],
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "candidate_id": result["candidate_id"],
        "selected_at": _utc_now(),
        "apply_status": "activation_requested",
        "activation_request_id": request_id,
    }
    _write_json(install_control_dir() / SELECTION_FILE, selection)
    _write_selection_control(selection)
    _write_apply_status(
        {
            "schema_version": 2,
            "status": "activation_requested",
            "selected_host_path": result["final_host_path"],
            "container_archive_path": CONTAINER_ARCHIVE_PATH,
            "requested_at": _utc_now(),
            "next_action": "setup_helper_activation",
        }
    )
    activation_request = {
        "schema_version": 1,
        "request_id": request_id,
        "selected_host_path": result["final_host_path"],
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "requested_at": _utc_now(),
        "status": "requested",
    }
    _write_activation_request(activation_request)
    _write_activation_request_control(activation_request)
    return {**result, **selection, "apply_status": "activation_requested", "storage_confirmation": storage_confirmation_status()}


def queue_runtime_activation(
    selected_host_path: str,
    *,
    request_prefix: str = "runtime-storage",
    operation_id: str | None = None,
    physical_identity: str | None = None,
    runtime_request_id: str | None = None,
) -> dict:
    final = Path(str(selected_host_path or "").strip())
    if not final.is_absolute():
        raise ValueError("selected_host_path_must_be_absolute")
    folder_name = validate_folder_name(final.name)
    selected_mount = final.parent
    if str(selected_mount) in {"", "."} or not selected_mount.is_absolute():
        raise ValueError("selected_mount_path_must_be_absolute")
    if final == selected_mount:
        raise ValueError("selected_host_path_must_be_child_folder")
    request_id = runtime_request_id or f"{request_prefix}-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    selection = {
        "schema_version": 2,
        "selected_host_path": str(final),
        "selected_mount_path": str(selected_mount),
        "folder_name": folder_name,
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "candidate_id": f"existing-root-{folder_name}",
        "selected_at": _utc_now(),
        "apply_status": "activation_requested",
        "activation_request_id": request_id,
        "operation_id": operation_id,
        "physical_identity": physical_identity,
    }
    _write_json(install_control_dir() / SELECTION_FILE, selection)
    _write_selection_control(selection)
    _write_apply_status(
        {
            "schema_version": 2,
            "status": "activation_requested",
            "selected_host_path": str(final),
            "container_archive_path": CONTAINER_ARCHIVE_PATH,
            "requested_at": _utc_now(),
            "next_action": "runtime_storage_activation",
            "request_id": request_id,
            "operation_id": operation_id,
        }
    )
    activation_request = {
        "schema_version": 1,
        "request_id": request_id,
        "selected_host_path": str(final),
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "requested_at": _utc_now(),
        "status": "requested",
        "operation_id": operation_id,
    }
    _write_activation_request(activation_request)
    _write_activation_request_control(activation_request)
    return {**selection, "request_id": request_id, "storage_confirmation": storage_confirmation_status()}


def mark_setup_completed() -> None:
    _write_json(
        install_control_dir() / SETUP_COMPLETE_FILE,
        {
            "schema_version": 1,
            "status": "completed",
            "completed_at": _utc_now(),
        },
    )
