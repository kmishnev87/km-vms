from __future__ import annotations

import json
import os
import re
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from app.core.config import settings

CONTAINER_ARCHIVE_PATH = "/storage/archive"
DISCOVERY_FILE = "storage-discovery.json"
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


def _safe_read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _control_value(value: object) -> str:
    text = str(value or "")
    if "\n" in text or "\r" in text or "\0" in text:
        raise ValueError("control value contains unsafe characters")
    return text


def _write_control(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
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
        return value
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


def discovery_snapshot() -> dict:
    path = install_control_dir() / DISCOVERY_FILE
    payload = _safe_read_json(path)
    raw_candidates = [sanitize_candidate(item) for item in payload.get("candidates") or [] if isinstance(item, dict)]
    ordered = _sorted_candidates(raw_candidates)
    primary = [item for item in ordered if item["ui_visibility"] == "primary"]
    hidden_count = sum(1 for item in ordered if item["ui_visibility"] == "hidden")
    manual_count = sum(1 for item in ordered if item["ui_visibility"] == "manual_only")
    return {
        "available": bool(primary) and bool(payload.get("host_visibility")),
        "discovery_source": payload.get("discovery_source") or "unavailable",
        "host_visibility": bool(payload.get("host_visibility")),
        "created_at": payload.get("created_at"),
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "candidates": primary,
        "manual_path_supported": True,
        "hidden_candidate_count": hidden_count,
        "manual_candidate_count": manual_count,
        "status": "ready" if primary and payload.get("host_visibility") else "manual_snapshot_required",
    }


def _all_discovered_candidates() -> list[dict]:
    path = install_control_dir() / DISCOVERY_FILE
    payload = _safe_read_json(path)
    return [sanitize_candidate(item) for item in payload.get("candidates") or [] if isinstance(item, dict)]


def storage_confirmation_status() -> dict:
    selection = _safe_read_json(install_control_dir() / SELECTION_FILE)
    apply_state = _safe_read_json(install_control_dir() / APPLY_STATUS_FILE)
    selected_host_path = str(selection.get("selected_host_path") or "").strip()
    container_archive_path = str(selection.get("container_archive_path") or CONTAINER_ARCHIVE_PATH).strip()
    apply_status = str(apply_state.get("status") or selection.get("apply_status") or "").strip()
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


def queue_runtime_activation(selected_host_path: str, *, request_prefix: str = "runtime-storage") -> dict:
    final = Path(str(selected_host_path or "").strip())
    if not final.is_absolute():
        raise ValueError("selected_host_path_must_be_absolute")
    folder_name = validate_folder_name(final.name)
    selected_mount = final.parent
    if str(selected_mount) in {"", "."} or not selected_mount.is_absolute():
        raise ValueError("selected_mount_path_must_be_absolute")
    if final == selected_mount:
        raise ValueError("selected_host_path_must_be_child_folder")
    request_id = f"{request_prefix}-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
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
        }
    )
    activation_request = {
        "schema_version": 1,
        "request_id": request_id,
        "selected_host_path": str(final),
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "requested_at": _utc_now(),
        "status": "requested",
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
