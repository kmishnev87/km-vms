from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app.core.config import settings

CONTAINER_ARCHIVE_PATH = "/storage/archive"
DISCOVERY_FILE = "storage-discovery.json"
SELECTION_FILE = "storage-selection.json"
MARKER_FILE = ".km-vms-storage-root.json"
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,79}$")
BLOCKED_PATHS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run",
    "/sbin", "/sys", "/usr", "/var", "/root", "/tmp",
}
BLOCKED_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "tmpfs",
    "overlay", "squashfs", "aufs", "debugfs", "tracefs", "securityfs",
    "pstore", "configfs", "mqueue", "hugetlbfs", "fusectl", "autofs",
}


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


def _candidate_id(path: str) -> str:
    import zlib

    return f"mount-{zlib.crc32(path.encode('utf-8')) & 0xffffffff}"


def _path_block_reason(path: str, fstype: str | None = None) -> str:
    value = str(path or "").strip()
    if not value.startswith("/"):
        return "not_absolute"
    candidate_path = Path(value)
    if value == "/" or any(_same_or_child(candidate_path, Path(blocked)) for blocked in BLOCKED_PATHS if blocked != "/"):
        return "dangerous_system_path"
    if value.startswith("/var/lib/docker") or "/overlay" in value:
        return "docker_internal_path"
    parts = set(Path(value).parts)
    if ".git" in parts or ".env" in parts or "secrets" in parts or "credentials" in parts:
        return "sensitive_or_service_path"
    if fstype and fstype in BLOCKED_FSTYPES:
        return "unsupported_pseudo_filesystem"
    return ""


def sanitize_candidate(raw: dict) -> dict:
    path = str(raw.get("path") or "").strip()
    fstype = str(raw.get("filesystem_type") or "").strip()
    reason = _path_block_reason(path, fstype) or str(raw.get("reason") or "")
    writable = bool(raw.get("writable")) and not reason
    safety = "allowed" if writable else "blocked"
    return {
        "id": str(raw.get("id") or _candidate_id(path)),
        "path": path,
        "label": str(raw.get("label") or path),
        "filesystem_type": fstype,
        "total_bytes": int(raw.get("total_bytes") or 0),
        "used_bytes": int(raw.get("used_bytes") or 0),
        "free_bytes": int(raw.get("free_bytes") or 0),
        "writable": writable,
        "safety_status": safety,
        "reason": reason,
        "recommended": bool(raw.get("recommended") and writable),
    }


def discovery_snapshot() -> dict:
    path = install_control_dir() / DISCOVERY_FILE
    payload = _safe_read_json(path)
    candidates = [sanitize_candidate(item) for item in payload.get("candidates") or [] if isinstance(item, dict)]
    return {
        "available": bool(candidates) and bool(payload.get("host_visibility")),
        "discovery_source": payload.get("discovery_source") or "unavailable",
        "host_visibility": bool(payload.get("host_visibility")),
        "created_at": payload.get("created_at"),
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "candidates": candidates,
        "status": "ready" if candidates and payload.get("host_visibility") else "manual_snapshot_required",
    }


def get_candidate(candidate_id: str) -> dict:
    for item in discovery_snapshot()["candidates"]:
        if item["id"] == candidate_id:
            return item
    raise ValueError("storage candidate is not available")


def validate_folder_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("folder_name is required")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("folder_name must be a single folder name")
    if any(ord(char) < 32 for char in name):
        raise ValueError("folder_name contains control characters")
    if not FOLDER_RE.match(name):
        raise ValueError("folder_name contains unsupported characters")
    return name


def build_preview(candidate_id: str, folder_name: str) -> dict:
    candidate = get_candidate(candidate_id)
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
    warnings: list[str] = []
    if exists and not final.is_dir():
        blockers.append("target_exists_not_directory")
    if exists and final.is_symlink():
        blockers.append("target_is_symlink")
    if exists and final.is_dir() and (not is_empty) and not marked:
        blockers.append("non_empty_unmarked_folder")
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
        "warnings": warnings,
        "blockers": blockers,
        "restart_required": True,
        "status": "blocked" if blockers else "ready_to_validate",
    }


def validate_and_mark(candidate_id: str, folder_name: str) -> dict:
    preview = build_preview(candidate_id, folder_name)
    if preview["blockers"]:
        raise ValueError(",".join(preview["blockers"]))
    preview["host_validation_required"] = True
    preview["write_test"] = {"ok": False, "reason": "pending_host_helper"}
    preview["status"] = "pending_host_helper_required"
    return preview


def persist_selection(candidate_id: str, folder_name: str) -> dict:
    result = validate_and_mark(candidate_id, folder_name)
    selection = {
        "schema_version": 1,
        "selected_host_path": result["final_host_path"],
        "selected_mount_path": result["selected_mount_path"],
        "folder_name": result["folder_name"],
        "container_archive_path": CONTAINER_ARCHIVE_PATH,
        "candidate_id": result["candidate_id"],
        "selected_at": datetime.utcnow().isoformat() + "Z",
        "apply_status": "pending_host_helper_restart_required",
        "apply_helper": "scripts/km-vms-storage-apply.sh --app-dir <app-dir>",
    }
    control = install_control_dir()
    control.mkdir(parents=True, exist_ok=True)
    tmp = control / f"{SELECTION_FILE}.tmp"
    tmp.write_text(json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(control / SELECTION_FILE)
    return {**result, **selection}
