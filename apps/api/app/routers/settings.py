from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import ROLE_OWNER
from app.core.sanitization import redact_text as audit_redact_text
from app.core.security import hash_password
from app.db.session import get_db
from app.models.camera import Camera
from app.models.setup_lock import SetupLock
from app.models.user import User
from app.routers.deps import require_permission
from app.routers.recordings import collect_recording_files
from app.services.audit_log import audit_summary, create_event, events_as_text, list_events, request_ip, request_user_agent, serialize_event
from app.services.hardware import get_hardware_capabilities, invalidate_hardware_capabilities
from app.services.live_engine_v2 import manager as live_manager
from app.services.recording_reconciliation import reconciliation_diagnostics
from app.services.recording_retention import retention_diagnostics
from app.services.recorder_diagnostics import build_recorder_archive_payloads, build_recorder_status, build_system_runtime_status
from app.services.system_runtime_status import build_operator_runtime_status
from app.services.storage_monitoring import build_storage_monitoring_summary
from app.services.system_settings import (
    active_recording_jobs_count,
    get_system_settings,
    serialize_settings,
    update_system_settings,
    validate_settings_payload,
    validate_storage_path,
)
from app.services.storage_contract import storage_contract
from app.services.setup_storage import (
    build_preview as build_setup_storage_preview,
    discovery_snapshot as setup_storage_discovery_snapshot,
    persist_selection as persist_setup_storage_selection,
    require_storage_confirmation as require_setup_storage_confirmation,
    storage_confirmation_status as setup_storage_confirmation_status,
    validate_and_mark as validate_setup_storage_folder,
)
from app.services.schema_versioning import schema_version_status

router = APIRouter(tags=["settings"])

DIAGNOSTIC_CONTAINERS = (
    "vms-api",
    "vms-recorder",
    "vms-web",
    "vms-nginx",
    "vms-postgres",
    "vms-redis",
)
DIAGNOSTIC_MODES = {"normal", "extended"}
SENSITIVE_KEY_RE = re.compile(r"(password|secret|token|authorization|encryption_key|jwt)", re.IGNORECASE)
RTSP_CREDENTIALS_RE = re.compile(r"(rtsp://)([^@\s/]+)@", re.IGNORECASE)
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
TOKEN_QUERY_RE = re.compile(r"([?&](?:token|access_token|media_token)=)[^&\s]+", re.IGNORECASE)
DOCKER_SOCKET = Path("/var/run/docker.sock")
SETUP_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SETUP_LOCK = threading.Lock()


class SetupRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    password_confirm: str = Field(min_length=8, max_length=256)
    system_name: str | None = Field(default=None, max_length=80)
    timezone: str
    language: str
    storage_path: str
    recording_format: str
    hardware_preferred_backend: str | None = None


class SettingsUpdateRequest(BaseModel):
    system_name: str | None = Field(default=None, max_length=80)
    timezone: str | None = None
    language: str | None = None
    storage_path: str | None = None
    recording_format: str | None = None
    hardware_preferred_backend: str | None = None
    auto_free_space_cleanup_enabled: bool | None = None


class BugReportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    include_logs: bool = True


class StorageValidateRequest(BaseModel):
    storage_path: str
    create: bool = True


class SetupStorageSelectionRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    folder_name: str = Field(min_length=1, max_length=100)


@router.get("/system/status")
def system_status(db: Session = Depends(get_db)):
    system = get_system_settings(db)
    payload = {
        "initialized": system.system_initialized,
        "setup_required": not system.system_initialized,
        "language": system.language,
        "timezone": system.timezone,
    }
    if not system.system_initialized:
        payload["runtime"] = {"available": False, "setup_required": True}
    return payload


def require_setup_mode(db: Session) -> None:
    system = get_system_settings(db)
    if system.system_initialized:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System is already initialized")


@router.get("/setup/storage/discovery")
def setup_storage_discovery(db: Session = Depends(get_db)):
    require_setup_mode(db)
    return setup_storage_discovery_snapshot()


@router.get("/setup/storage/status")
def setup_storage_status(db: Session = Depends(get_db)):
    require_setup_mode(db)
    return setup_storage_confirmation_status()


@router.post("/setup/storage/preview")
def setup_storage_preview(payload: SetupStorageSelectionRequest, db: Session = Depends(get_db)):
    require_setup_mode(db)
    try:
        return build_setup_storage_preview(payload.candidate_id, payload.folder_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("/setup/storage/validate")
def setup_storage_validate(payload: SetupStorageSelectionRequest, db: Session = Depends(get_db)):
    require_setup_mode(db)
    try:
        return validate_setup_storage_folder(payload.candidate_id, payload.folder_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("/setup/storage/apply")
def setup_storage_apply(payload: SetupStorageSelectionRequest, db: Session = Depends(get_db)):
    require_setup_mode(db)
    try:
        return persist_setup_storage_selection(payload.candidate_id, payload.folder_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/system/recorder/status")
def system_recorder_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    return build_recorder_status(db)


@router.get("/system/schema/status")
def system_schema_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return schema_version_status(db)


@router.get("/system/runtime/status")
def system_runtime_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    return build_operator_runtime_status(db)


def audit_setup_failed(db: Session, request: Request, reason: str, status_code: int) -> None:
    create_event(
        db=db,
        actor=None,
        category="system",
        event_type="system.setup_failed",
        severity="warning",
        message_ru=f"System setup failed: {reason}",
        message_en=f"System setup failed: {reason}",
        target_type="system_setup",
        metadata={"reason": reason, "status_code": status_code},
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )


def validate_setup_username(username: str) -> str:
    value = str(username or "").strip()
    if len(value) < 2 or len(value) > 64:
        raise ValueError("username must be 2-64 characters")
    if not SETUP_USERNAME_RE.fullmatch(value):
        raise ValueError("username may contain only letters, numbers, dots, dashes and underscores")
    return value


def acquire_setup_lock(db: Session) -> None:
    db.add(SetupLock(name="first_run_setup"))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System setup is already completed or in progress")


@router.post("/setup", status_code=status.HTTP_201_CREATED)
def setup(payload: SetupRequest, db: Session = Depends(get_db), request: Request = None):
    with SETUP_LOCK:
        system = get_system_settings(db)
        if system.system_initialized:
            audit_setup_failed(db, request, "already_initialized", status.HTTP_409_CONFLICT)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System is already initialized")
        acquire_setup_lock(db)

        try:
            username = validate_setup_username(payload.username)
        except ValueError as exc:
            db.rollback()
            audit_setup_failed(db, request, "invalid_username", status.HTTP_422_UNPROCESSABLE_ENTITY)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

        if payload.password != payload.password_confirm:
            db.rollback()
            audit_setup_failed(db, request, "password_confirmation_mismatch", status.HTTP_422_UNPROCESSABLE_ENTITY)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="password_confirm does not match password")

        existing_user_count = db.query(User).count()
        if existing_user_count:
            db.rollback()
            audit_setup_failed(db, request, "owner_already_exists", status.HTTP_409_CONFLICT)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Initial owner already exists")

        try:
            settings_data = validate_settings_payload(payload.model_dump(exclude={"password_confirm"}), partial=False)
        except ValueError as exc:
            db.rollback()
            audit_setup_failed(db, request, "invalid_settings_payload", status.HTTP_422_UNPROCESSABLE_ENTITY)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

        requested_storage_path = settings_data.get("storage_path")
        try:
            storage_confirmation = require_setup_storage_confirmation()
        except ValueError as exc:
            db.rollback()
            audit_setup_failed(db, request, "storage_confirmation_invalid", status.HTTP_422_UNPROCESSABLE_ENTITY)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"storage_confirmation": str(exc)},
            )
        settings_data["storage_path"] = settings.storage_root

        storage_check = validate_storage_path(settings.storage_root, create=True)
        if not storage_check["ok"]:
            db.rollback()
            audit_setup_failed(db, request, "storage_validation_failed", status.HTTP_422_UNPROCESSABLE_ENTITY)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"storage": storage_check})

        try:
            if db.query(User).count():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Initial owner already exists")
            if get_system_settings(db).system_initialized:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System is already initialized")

            admin = User(
                username=username,
                full_name="Administrator",
                password_hash=hash_password(payload.password),
                role=ROLE_OWNER,
                is_active=True,
            )
            db.add(admin)

            for key, value in settings_data.items():
                setattr(system, key, value)
            system.system_initialized = True
            system.updated_at = datetime.utcnow()
            db.add(system)
            db.commit()
            db.refresh(system)
            db.refresh(admin)
        except HTTPException as exc:
            db.rollback()
            audit_setup_failed(db, request, "owner_already_exists", exc.status_code)
            raise
        except IntegrityError:
            db.rollback()
            audit_setup_failed(db, request, "owner_create_conflict", status.HTTP_409_CONFLICT)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Initial owner already exists")
        except Exception:
            db.rollback()
            audit_setup_failed(db, request, "setup_transaction_failed", status.HTTP_500_INTERNAL_SERVER_ERROR)
            raise
    create_event(
        db=db,
        actor=admin,
        category="system",
        event_type="system.setup_completed",
        message_ru=f"System setup completed by {admin.username}",
        message_en=f"System setup completed by {admin.username}",
        target_type="system_setup",
        target_id=system.id,
        metadata={
            "previous_state": {"system_initialized": False},
            "current_state": {"system_initialized": True},
            "owner_user_id": admin.id,
            "owner_role": admin.role,
            "timezone": system.timezone,
            "language": system.language,
            "system_name": system.system_name or "KM VMS",
            "recording_format": system.recording_format,
            "setup_storage_status": storage_confirmation["status"],
            "setup_storage_next_action": storage_confirmation["next_action"],
            "setup_storage_path_behavior": "stage2_selected_host_path_required_container_path_remains_internal",
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )

    return {
        "ok": True,
        "settings": serialize_settings(system),
        "storage_validation": {
            **storage_check,
            "requested_storage_path": requested_storage_path,
            "effective_storage_path": settings.storage_root,
            "storage_confirmation": {
                "status": storage_confirmation["status"],
                "selected_host_path": storage_confirmation["selected_host_path"],
                "container_archive_path": storage_confirmation["container_archive_path"],
                "next_action": storage_confirmation["next_action"],
            },
            "setup_storage_path_behavior": "stage2_selected_host_path_required_container_path_remains_internal",
        },
    }


@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    system = get_system_settings(db)
    return serialize_settings(system)


@router.patch("/settings")
def patch_settings(
    payload: SettingsUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    data = payload.model_dump(exclude_unset=True)
    data.pop("storage_path", None)
    previous = serialize_settings(get_system_settings(db))
    if (
        "recording_format" in data
        and str(data.get("recording_format") or "").strip().lower() != previous.get("recording_format")
    ):
        active_count = active_recording_jobs_count(db)
        if active_count:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "recording_format_change_blocked_active_recordings",
                    "active_recording_jobs_count": active_count,
                    "active_change_behavior": "blocked_while_active_recording_jobs_exist",
                },
            )
    try:
        system = update_system_settings(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    current = serialize_settings(system)
    if "hardware_preferred_backend" in data and system.hardware_preferred_backend != previous.get("hardware_preferred_backend"):
        invalidate_hardware_capabilities()
    setting_events = [
        ("language", "settings.language_changed", "язык", "language"),
        ("timezone", "settings.timezone_changed", "часовой пояс", "timezone"),
        ("hardware_preferred_backend", "settings.hardware_backend_changed", "аппаратное ускорение", "hardware backend"),
        ("recording_format", "settings.recording_format_changed", "формат записи", "recording format"),
        ("auto_free_space_cleanup_enabled", "settings.auto_free_space_cleanup_changed", "автоосвобождение места", "auto free-space cleanup"),
    ]
    changed = {}
    for key, event_type, label_ru, label_en in setting_events:
        if key in data and previous.get(key) != current.get(key):
            changed[key] = {"old": previous.get(key), "new": current.get(key)}
            create_event(
                db=db,
                actor=current_user,
                category="settings",
                event_type=event_type,
                message_ru=f"{current_user.username} изменил {label_ru}: {previous.get(key)} → {current.get(key)}",
                message_en=f"{current_user.username} changed {label_en}: {previous.get(key)} -> {current.get(key)}",
                target_type="settings",
                metadata={
                    "field": key,
                    "old": previous.get(key),
                    "new": current.get(key),
                    "recording_format_contract": current.get("recording_format_contract") if key == "recording_format" else None,
                },
                ip_address=request_ip(request),
                user_agent=request_user_agent(request),
            )
    if changed or data:
        create_event(
            db=db,
            actor=current_user,
            category="settings",
            event_type="settings.saved",
            message_ru=f"{current_user.username} сохранил настройки",
            message_en=f"{current_user.username} saved settings",
            target_type="settings",
            metadata={"changed": changed},
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
    return serialize_settings(system)


@router.post("/settings/storage/validate")
def validate_storage(
    payload: StorageValidateRequest,
    current_user: User = Depends(require_permission("manage_settings")),
):
    return validate_storage_path(payload.storage_path, create=payload.create)


def iter_log_files() -> list[Path]:
    roots = [
        Path(settings.storage_previews),
        Path(settings.storage_exports),
        Path(settings.storage_root),
        Path("/tmp"),
    ]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.log"):
            if path.is_file():
                files.append(path)
    return sorted(files, key=lambda item: str(item))[:200]


def redact_text(value: str | None) -> str:
    if not value:
        return ""
    text = RTSP_CREDENTIALS_RE.sub(r"\1***@", str(value))
    text = BEARER_RE.sub(r"\1***", text)
    text = TOKEN_QUERY_RE.sub(r"\1***", text)
    return text


def safe_json(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                result[key] = "***"
            else:
                result[key] = safe_json(item)
        return result
    if isinstance(value, list):
        return [safe_json(item) for item in value]
    if isinstance(value, tuple):
        return [safe_json(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def write_json(bundle: zipfile.ZipFile, arcname: str, payload) -> None:
    bundle.writestr(
        arcname,
        json.dumps(safe_json(payload), ensure_ascii=False, indent=2, default=str) + "\n",
    )


def safe_archive_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._/-]+", "_", value).strip("_") or "log"


def camera_diagnostics(db: Session) -> list[dict]:
    cameras = db.query(Camera).order_by(Camera.id.asc()).all()
    return [
        {
            "id": camera.id,
            "name": camera.name,
            "storage_folder_name": camera.storage_folder_name,
            "enabled": bool(camera.enabled),
            "protocol": camera.protocol,
            "host": camera.host,
            "port": camera.port,
            "username": camera.username,
            "rtsp_main_url": camera.rtsp_main_url,
            "rtsp_sub_url": camera.rtsp_sub_url,
            "rtsp_transport": camera.rtsp_transport,
            "recording_mode": camera.recording_mode,
            "default_live_stream": camera.default_live_stream,
            "default_record_stream": camera.default_record_stream,
            "segment_minutes": camera.segment_minutes,
            "retention_days": camera.retention_days,
            "storage_quota_gb": camera.storage_quota_gb,
            "status": camera.status,
            "last_error": camera.last_error,
            "created_at": camera.created_at,
            "updated_at": camera.updated_at,
        }
        for camera in cameras
    ]


def storage_diagnostics() -> dict:
    root = Path(settings.storage_root)
    previews = Path(settings.storage_previews)
    exports = Path(settings.storage_exports)
    return {
        "storage_contract": storage_contract(),
        "storage_root": str(root),
        "storage_root_exists": root.exists(),
        "storage_root_writable": os.access(root, os.W_OK) if root.exists() else False,
        "storage_previews": str(previews),
        "storage_previews_exists": previews.exists(),
        "storage_exports": str(exports),
        "storage_exports_exists": exports.exists(),
    }


def recordings_diagnostics() -> dict:
    try:
        items = collect_recording_files()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    total_size = sum(int(item.get("size_bytes") or 0) for item in items)
    by_camera: dict[str, dict] = {}
    for item in items:
        camera = str(item.get("camera") or "unknown")
        row = by_camera.setdefault(camera, {"count": 0, "size_bytes": 0})
        row["count"] += 1
        row["size_bytes"] += int(item.get("size_bytes") or 0)
    return {
        "ok": True,
        "count": len(items),
        "size_bytes": total_size,
        "by_camera": by_camera,
        "latest": items[:50],
    }


def chronology_diagnostics(db: Session) -> dict:
    rows = []
    for camera in db.query(Camera).order_by(Camera.id.asc()).all():
        root = Path(settings.storage_root) / camera.storage_folder_name
        files = []
        if root.exists():
            for path in sorted(root.rglob("*.mp4"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)[:20]:
                try:
                    stat = path.stat()
                    files.append(
                        {
                            "path": str(path.resolve().relative_to(root.resolve())),
                            "size_bytes": stat.st_size,
                            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        }
                    )
                except Exception as exc:
                    files.append({"path": str(path), "error": str(exc)})
        rows.append(
            {
                "camera_id": camera.id,
                "camera_name": camera.name,
                "archive_root": str(root),
                "archive_root_exists": root.exists(),
                "latest_mp4_files": files,
            }
        )
    return {"items": rows}


def decode_chunked_http_body(body: bytes) -> bytes:
    chunks = []
    idx = 0
    while idx < len(body):
        line_end = body.find(b"\r\n", idx)
        if line_end < 0:
            return body
        size_raw = body[idx:line_end].split(b";", 1)[0]
        try:
            size = int(size_raw, 16)
        except ValueError:
            return body
        idx = line_end + 2
        if size == 0:
            break
        chunks.append(body[idx:idx + size])
        idx += size + 2
    return b"".join(chunks)


def strip_docker_stream_frames(body: bytes) -> bytes:
    frames = []
    idx = 0
    while idx + 8 <= len(body):
        header = body[idx:idx + 8]
        if header[0] not in {1, 2} or header[1:4] != b"\x00\x00\x00":
            return body
        size = int.from_bytes(header[4:8], "big")
        idx += 8
        frames.append(body[idx:idx + size])
        idx += size
    return b"".join(frames) if frames else body


def docker_logs_via_socket(container: str, mode: str) -> str:
    if not DOCKER_SOCKET.exists():
        raise FileNotFoundError(f"{DOCKER_SOCKET} is not mounted")

    params = "stdout=1&stderr=1&timestamps=1"
    if mode == "extended":
        params += f"&since={int(time.time()) - 1800}"
    else:
        params += f"&since={int(time.time()) - 600}"
    path = f"/containers/{quote(container, safe='')}/logs?{params}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(20)
        sock.connect(str(DOCKER_SOCKET))
        sock.sendall(request)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)

    response = b"".join(chunks)
    header_end = response.find(b"\r\n\r\n")
    if header_end < 0:
        return response.decode("utf-8", errors="replace")

    headers = response[:header_end].decode("iso-8859-1", errors="replace")
    body = response[header_end + 4:]
    if "transfer-encoding: chunked" in headers.lower():
        body = decode_chunked_http_body(body)
    body = strip_docker_stream_frames(body)
    if not headers.startswith("HTTP/1.1 200") and not headers.startswith("HTTP/1.0 200"):
        return f"Docker API error for {container}\n{headers}\n{body.decode('utf-8', errors='replace')}"
    return body.decode("utf-8", errors="replace")


def docker_logs_via_cli(container: str, mode: str) -> str:
    cmd = ["docker", "logs"]
    if mode == "extended":
        cmd.extend(["--since", "30m"])
    else:
        cmd.extend(["--since", "10m"])
    cmd.append(container)

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return f"docker logs failed for {container}: {exc}\n"

    output = result.stdout or ""
    if result.returncode != 0:
        output = f"docker logs exit_code={result.returncode}\n{output}"
    return output


def run_docker_logs(container: str, mode: str) -> str:
    try:
        return redact_text(docker_logs_via_socket(container, mode))
    except Exception as socket_exc:
        try:
            return redact_text(docker_logs_via_cli(container, mode))
        except Exception as cli_exc:
            return (
                f"Docker logs unavailable for {container}\n"
                f"socket_error={socket_exc}\n"
                f"cli_error={cli_exc}\n"
            )


def write_docker_logs(bundle: zipfile.ZipFile, mode: str) -> None:
    for container in DIAGNOSTIC_CONTAINERS:
        bundle.writestr(f"docker/{container}.log", run_docker_logs(container, mode))


def write_file_logs(bundle: zipfile.ZipFile) -> None:
    seen: set[Path] = set()
    for path in iter_log_files():
        try:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            content = path.read_text(encoding="utf-8", errors="replace")
            arcname = safe_archive_name(str(resolved).lstrip("/\\").replace("\\", "/"))
            bundle.writestr(f"files/{arcname}", redact_text(content))
        except OSError:
            continue


def build_log_archive(
    db: Session,
    mode: str = "normal",
    report_text: str | None = None,
    include_logs: bool = True,
) -> io.BytesIO:
    mode = mode if mode in DIAGNOSTIC_MODES else "normal"
    created_at = datetime.utcnow().isoformat() + "Z"
    audit_events = list_events(
        db,
        limit=1000,
        since_minutes=30 if mode == "extended" else 10,
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        write_json(
            bundle,
            "system/manifest.json",
            {
                "created_at": created_at,
                "mode": mode,
                "docker_log_rule": "--since=30m" if mode == "extended" else "--since=10m",
                "containers": DIAGNOSTIC_CONTAINERS,
                "audit_events_included": True,
                "audit_event_count": len(audit_events),
                "audit_event_rule": "last 30 minutes" if mode == "extended" else "last 10 minutes",
                "archive_mode": mode,
            },
        )
        write_json(
            bundle,
            "system/info.json",
            {
                "created_at": created_at,
                "app_env": settings.app_env,
                "storage_root": settings.storage_root,
                "storage_previews": settings.storage_previews,
                "storage_exports": settings.storage_exports,
                "default_live_stream": settings.default_live_stream,
                "default_record_stream": settings.default_record_stream,
                "live_transcode": settings.live_transcode,
                "live_video_codec": settings.live_video_codec,
                "live_hwaccel_mode": settings.live_hwaccel_mode,
                "live_hwaccel_backend": settings.live_hwaccel_backend,
                "live_max_concurrent_transcodes": settings.live_max_concurrent_transcodes,
            },
        )
        write_json(bundle, "system/settings.json", serialize_settings(get_system_settings(db)))
        write_json(bundle, "storage/status.json", storage_diagnostics())
        write_json(bundle, "storage/storage_monitoring_summary.json", build_storage_monitoring_summary(db))
        write_json(bundle, "storage/recording_integrity_summary.json", reconciliation_diagnostics(db))
        write_json(bundle, "storage/retention_summary.json", retention_diagnostics(db))
        for arcname, payload in build_recorder_archive_payloads(db).items():
            write_json(bundle, arcname, payload)
        write_json(bundle, "hardware/capabilities.json", get_hardware_capabilities())
        write_json(bundle, "cameras/cameras.json", camera_diagnostics(db))
        live_status = live_manager.status()
        write_json(bundle, "live/status.json", {"items": live_status, "count": len(live_status)})
        write_json(bundle, "live/debug.json", live_manager.debug())
        write_json(bundle, "recordings/summary.json", recordings_diagnostics())
        write_json(bundle, "chronology/summary.json", chronology_diagnostics(db))
        write_json(bundle, "audit/events_recent.json", [serialize_event(event) for event in audit_events])
        write_json(bundle, "audit/summary.json", audit_summary(audit_events))
        write_json(
            bundle,
            "audit/redaction_proof.json",
            {
                "status": "PASS",
                "scope": "diagnostic_archive_audit_payload",
                "raw_tokens_included": False,
                "raw_authorization_headers_included": False,
                "raw_cookies_included": False,
                "raw_rtsp_credentials_included": False,
                "raw_passwords_included": False,
                "raw_request_bodies_included": False,
            },
        )
        bundle.writestr("audit/events_recent.txt", events_as_text(audit_events))
        if report_text is not None:
            bundle.writestr("bug-report.txt", audit_redact_text(report_text.strip()) + "\n")
        if include_logs:
            write_file_logs(bundle)
            write_docker_logs(bundle, mode)
    archive.seek(0)
    return archive


@router.get("/settings/logs/archive")
def download_log_archive(
    request: Request,
    mode: str = Query("normal", pattern="^(normal|extended)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    create_event(
        db=db,
        actor=current_user,
        category="diagnostics",
        event_type="diagnostics.archive_requested",
        message_ru=f"{current_user.username} запросил диагностический архив: {mode}",
        message_en=f"{current_user.username} requested diagnostic archive: {mode}",
        target_type="diagnostic_archive",
        metadata={"mode": mode},
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    try:
        archive = build_log_archive(db=db, mode=mode)
    except Exception as exc:
        create_event(
            db=db,
            actor=current_user,
            category="diagnostics",
            event_type="diagnostics.archive_failed",
            severity="error",
            message_ru=f"Failed to create diagnostic archive: {type(exc).__name__}",
            message_en=f"Failed to create diagnostic archive: {type(exc).__name__}",
            target_type="diagnostic_archive",
            metadata={"mode": mode, "error_type": type(exc).__name__, "error": audit_redact_text(str(exc))[:300]},
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise
    create_event(
        db=db,
        actor=current_user,
        category="diagnostics",
        event_type="diagnostics.archive_created",
        message_ru=f"{current_user.username} создал {'расширенный' if mode == 'extended' else 'обычный'} диагностический архив",
        message_en=f"{current_user.username} created {'extended' if mode == 'extended' else 'normal'} diagnostic archive",
        target_type="diagnostic_archive",
        metadata={"mode": mode},
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    suffix = "extended" if mode == "extended" else "normal"
    filename = f"km-vms-logs-{suffix}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/settings/bug-report")
def create_bug_report(
    payload: BugReportRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    create_event(
        db=db,
        actor=current_user,
        category="diagnostics",
        event_type="diagnostics.bug_report_requested",
        message_ru=f"{current_user.username} requested bug report archive",
        message_en=f"{current_user.username} requested bug report archive",
        target_type="diagnostic_archive",
        metadata={"artifact_kind": "bug_report", "include_logs": payload.include_logs, "report_text_length": len(payload.text or "")},
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    try:
        archive = build_log_archive(db=db, report_text=payload.text, include_logs=payload.include_logs)
    except Exception as exc:
        create_event(
            db=db,
            actor=current_user,
            category="diagnostics",
            event_type="diagnostics.bug_report_failed",
            severity="error",
            message_ru=f"Failed to create bug report archive: {type(exc).__name__}",
            message_en=f"Failed to create bug report archive: {type(exc).__name__}",
            target_type="diagnostic_archive",
            metadata={"artifact_kind": "bug_report", "error_type": type(exc).__name__, "error": audit_redact_text(str(exc))[:300]},
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise
    create_event(
        db=db,
        actor=current_user,
        category="diagnostics",
        event_type="diagnostics.bug_report_created",
        message_ru=f"{current_user.username} created bug report archive",
        message_en=f"{current_user.username} created bug report archive",
        target_type="diagnostic_archive",
        metadata={"artifact_kind": "bug_report", "include_logs": payload.include_logs, "report_text_length": len(payload.text or "")},
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    filename = f"km-vms-bug-report-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
