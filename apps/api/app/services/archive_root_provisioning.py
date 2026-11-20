"""Queue an approved root attachment for the existing host setup helper."""
import time
from pathlib import Path
from uuid import uuid4

from app.services.setup_storage import _safe_read_json, _write_control, install_control_dir, validate_folder_name

REQUEST = "storage-root-provision-request.control"
RESULT = "storage-root-provision-result.json"
REQUEST_TTL = 600


def _request() -> dict:
    path = install_control_dir() / REQUEST
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return {}
        return dict(line.split("=", 1) for line in path.read_text("utf-8").splitlines() if "=" in line)
    except (OSError, ValueError):
        return {}


def provision_status(root_id=None):
    request = _request()
    if not request.get("request_id") or (root_id is not None and str(root_id) != request.get("root_id")):
        return None
    result = _safe_read_json(install_control_dir() / RESULT)
    if result.get("request_id") == request["request_id"] and result.get("root_id") == request.get("root_id"):
        if result.get("status") in ("completed", "failed"):
            return {"status": result["status"], "reason": result.get("reason"), "root_id": result["root_id"]}
    try:
        expired = time.time() >= int(request.get("expires_at", ""))
    except ValueError:
        expired = True
    return {"status": "failed" if expired else "provisioning",
            "reason": "archive_attachment_timeout" if expired else None, "root_id": request.get("root_id")}


def queue_provision(root, selected_mount):
    host_path = Path(root.root_path)
    mount = Path(selected_mount)
    folder = validate_folder_name(host_path.name)
    if not mount.is_absolute() or mount == Path("/") or mount / folder != host_path or not root.physical_identity:
        raise ValueError("archive_attachment_target_invalid")
    request_id = uuid4().hex
    _write_control(install_control_dir() / REQUEST, {
        "request_id": request_id,
        "root_id": root.id,
        "mount": str(mount),
        "folder": folder,
        "physical_identity": root.physical_identity,
        "expires_at": int(time.time()) + REQUEST_TTL,
    })
    return provision_status(root.id)
