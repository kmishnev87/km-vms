from pathlib import Path
import shutil
import subprocess
import json
from uuid import uuid4
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import user_has_permission
from app.core.security import encrypt_text, decrypt_text
from app.db.session import get_db
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.user import User
from app.routers.deps import require_permission
from app.schemas.camera import CameraCreate, CameraResponse, CameraUpdate
from app.services.audit_log import create_event, redact_text, request_ip, request_user_agent
from app.services.storage import build_unique_folder_name, ensure_camera_folder
from app.services.onvif_service import (
    fetch_onvif_profiles,
    get_onvif_profile_config,
    update_onvif_profile,
)
from app.services.recording_retention import execute_segments, preview_segments

router = APIRouter(prefix="/cameras", tags=["cameras"])
viewer_router = APIRouter(prefix="/viewer/cameras", tags=["viewer-cameras"])

CAMERA_DELETE_FILES_REASON = "camera_delete_with_files"
CAMERA_DELETE_NO_FILES_BLOCK_REASON = "recordings_exist_delete_files_false_requires_safe_policy"
CAMERA_DELETE_UNSAFE_WITH_FILES_REASON = "camera_delete_with_files_requires_all_segments_safe"


def safe_onvif_error(exc: Exception) -> str:
    text = redact_text(str(exc) if exc else "")
    lower = text.lower()
    if "auth" in lower or "401" in lower or "forbidden" in lower:
        return "ONVIF service is reachable, but authentication failed. Check camera ONVIF credentials."
    if "connection refused" in lower or "failed to establish" in lower or "newconnectionerror" in lower:
        return "ONVIF service is not reachable on the selected host and port."
    if "timeout" in lower or "timed out" in lower:
        return "ONVIF service did not respond in time. Check ONVIF host, port, and network access."
    if len(text) > 180 or "traceback" in lower or "envelope" in lower or "soap" in lower:
        return "ONVIF operation failed. Check camera ONVIF service, permissions, and profile support."
    return text or "ONVIF operation failed."


def assemble_rtsp_url(
    host: str | None,
    port: int | None,
    username: str | None,
    password: str | None,
    value: str | None,
) -> str | None:
    if not value:
        return None

    value = str(value).strip()
    if not value:
        return None

    if value.lower().startswith("rtsp://"):
        return value

    if not host:
        return value

    path = value if value.startswith("/") else f"/{value}"
    auth = ""
    if username and password:
        auth = f"{quote(str(username), safe='')}:{quote(str(password), safe='')}@"
    elif username:
        auth = f"{quote(str(username), safe='')}@"

    port_part = f":{int(port)}" if port else ""
    return f"rtsp://{auth}{host}{port_part}{path}"


def get_camera_credentials(
    db: Session,
    payload: dict,
):
    camera_id = payload.get("camera_id")
    username = payload.get("username")
    password = payload.get("password")
    host = payload.get("host")
    port = payload.get("port")
    rtsp_host = payload.get("rtsp_host")
    rtsp_port = payload.get("rtsp_port")

    if camera_id:
        camera = db.query(Camera).filter(Camera.id == int(camera_id)).first()
        if camera:
            if not host:
                host = camera.host
            if not port:
                port = camera.port
            if not username:
                username = camera.username
            if not password:
                password = decrypt_text(camera.password_encrypted)
            if not rtsp_host:
                rtsp_host = camera.rtsp_reachable_host
            if not rtsp_port:
                rtsp_port = camera.rtsp_reachable_port

    return {
        "camera_id": camera_id,
        "host": host,
        "port": port,
        "rtsp_host": rtsp_host,
        "rtsp_port": rtsp_port,
        "username": username,
        "password": password,
    }


def build_test_url(payload: dict, db: Session | None = None) -> str | None:
    protocol = str(payload.get("protocol") or "rtsp").lower()
    if protocol == "rtsp":
        host = payload.get("host")
        port = payload.get("port") or 554
    else:
        host = payload.get("rtsp_host") or payload.get("host")
        port = payload.get("rtsp_port") or 554
    username = payload.get("username")
    password = payload.get("password")

    if db is not None:
        creds = get_camera_credentials(db, payload)
        if protocol == "rtsp":
            host = creds["host"] or host
            port = creds["port"] or port
        else:
            host = payload.get("rtsp_host") or creds["rtsp_host"] or host
            port = payload.get("rtsp_port") or creds["rtsp_port"] or port
        username = creds["username"] or username
        password = creds["password"] or password

    return (
        assemble_rtsp_url(
            host,
            port,
            username,
            password,
            payload.get("rtsp_main_url"),
        )
        or
        assemble_rtsp_url(
            host,
            port,
            username,
            password,
            payload.get("rtsp_sub_url"),
        )
    )


@router.post("/onvif/profiles")
def onvif_profiles(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)

    host = creds["host"]
    port = creds["port"] or 80
    username = creds["username"]
    password = creds["password"]

    if not host or not username or not password:
        raise HTTPException(status_code=400, detail="Для ONVIF нужны host, username, password")

    try:
        data = fetch_onvif_profiles(
            host=str(host),
            port=int(port),
            username=str(username),
            password=str(password),
            rtsp_host=str(creds["rtsp_host"] or host),
            rtsp_port=int(creds["rtsp_port"] or 554),
        )
        return {
            "ok": True,
            **data,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=safe_onvif_error(e))


@router.post("/onvif/profile_config")
def onvif_profile_config(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)
    if not creds["host"] or not creds["username"] or not creds["password"] or not payload.get("profile_token"):
        raise HTTPException(status_code=400, detail="ONVIF profile settings require host, credentials, and profile token.")

    try:
        return get_onvif_profile_config(
            host=str(creds["host"]),
            port=int(creds["port"] or 80),
            username=str(creds["username"]),
            password=str(creds["password"]),
            profile_token=str(payload["profile_token"]),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=safe_onvif_error(e))


@router.post("/onvif/update_profile")
def update_onvif_profile_route(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)
    if not creds["host"] or not creds["username"] or not creds["password"] or not payload.get("profile_token"):
        raise HTTPException(status_code=400, detail="ONVIF profile update requires host, credentials, and profile token.")

    try:
        return update_onvif_profile(
            host=str(creds["host"]),
            port=int(creds["port"] or 80),
            username=str(creds["username"]),
            password=str(creds["password"]),
            profile_token=str(payload["profile_token"]),
            config=payload["config"],
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=safe_onvif_error(e))


@router.get("", response_model=list[CameraResponse])
def list_cameras(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    return db.query(Camera).order_by(Camera.name.asc()).all()


@viewer_router.get("")
def list_viewer_cameras(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_live")),
):
    cameras = db.query(Camera).order_by(Camera.name.asc()).all()
    return [
        {
            "id": camera.id,
            "name": camera.name,
            "enabled": bool(camera.enabled),
            "host": camera.host,
            "port": camera.port,
            "status": camera.status,
            "default_live_stream": camera.default_live_stream,
            "rtsp_main_url": bool(camera.rtsp_main_url),
            "rtsp_sub_url": bool(camera.rtsp_sub_url),
        }
        for camera in cameras
    ]


@router.get("/{camera_id}", response_model=CameraResponse)
def get_camera(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    return camera


@router.post("/test")
def test_camera(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    input_url = build_test_url(payload, db=db)
    if not input_url:
        raise HTTPException(status_code=400, detail="Укажите RTSP path или URL для проверки камеры.")

    transport = payload.get("rtsp_transport") or "tcp"

    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", transport,
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        input_url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=400, detail="Камера не ответила вовремя. Проверьте адрес, порт и доступность камеры.")

    if result.returncode != 0:
        raise HTTPException(status_code=400, detail="Не удалось подключиться к камере. Проверьте RTSP path, логин, пароль и сетевой доступ.")

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Камера ответила нестандартно. Проверьте параметры потока.")

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    preview_path, preview_url, preview_token = test_preview_destination(payload)
    preview_ok = capture_camera_preview(input_url, transport, preview_path)

    return {
        "ok": True,
        "display_path": safe_rtsp_display_path(input_url),
        "transport": transport,
        "preview_url": preview_url if preview_ok else None,
        "preview_token": preview_token if preview_ok and preview_token else None,
        "preview_ok": preview_ok,
        "preview_message": None if preview_ok else "Соединение установлено, но кадр превью получить не удалось.",
        "video": {
            "codec": video.get("codec_name") if video else None,
            "profile": video.get("profile") if video else None,
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "fps": video.get("r_frame_rate") if video else None,
            "pix_fmt": video.get("pix_fmt") if video else None,
        } if video else None,
        "audio": {
            "codec": audio.get("codec_name") if audio else None,
            "sample_rate": audio.get("sample_rate") if audio else None,
            "channels": audio.get("channels") if audio else None,
        } if audio else None,
        "format": {
            "format_name": data.get("format", {}).get("format_name"),
            "bit_rate": data.get("format", {}).get("bit_rate"),
        },
    }


@router.post("", response_model=CameraResponse, status_code=status.HTTP_201_CREATED)
def create_camera(
    payload: CameraCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    existing = db.query(Camera).filter(Camera.name == payload.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Камера с таким именем уже существует")

    folder_name = build_unique_folder_name(db, payload.name)
    ensure_camera_folder(folder_name)

    rtsp_host = payload.rtsp_host or payload.host
    rtsp_port = payload.rtsp_port or 554
    if str(payload.protocol or "rtsp").lower() == "rtsp":
        rtsp_host = payload.host
        rtsp_port = payload.port

    rtsp_main_url = assemble_rtsp_url(
        rtsp_host,
        rtsp_port,
        payload.username,
        payload.password,
        payload.rtsp_main_url,
    )
    rtsp_sub_url = assemble_rtsp_url(
        rtsp_host,
        rtsp_port,
        payload.username,
        payload.password,
        payload.rtsp_sub_url,
    )

    camera = Camera(
        name=payload.name,
        storage_folder_name=folder_name,
        enabled=payload.enabled,
        protocol=payload.protocol,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password_encrypted=encrypt_text(payload.password),
        rtsp_main_url=rtsp_main_url,
        rtsp_sub_url=rtsp_sub_url,
        rtsp_transport=payload.rtsp_transport,
        onvif_path=payload.onvif_path,
        onvif_profile_token=payload.onvif_profile_token,
        onvif_channel_id=payload.onvif_channel_id,
        recording_mode=payload.recording_mode,
        default_live_stream=payload.default_live_stream,
        default_record_stream=payload.default_record_stream,
        segment_minutes=payload.segment_minutes,
        retention_days=payload.retention_days,
        storage_quota_gb=max(payload.storage_quota_gb, 50),
        status="created",
        last_error=None,
    )

    db.add(camera)
    db.commit()
    db.refresh(camera)
    attach_test_preview_to_camera(payload.preview_token, camera.id)
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.created",
        message_ru=f"{current_user.username} добавил камеру {camera.name}",
        message_en=f"{current_user.username} added camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        metadata={
            "protocol": camera.protocol,
            "enabled": bool(camera.enabled),
            "default_live_stream": camera.default_live_stream,
            "default_record_stream": camera.default_record_stream,
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return camera


def safe_rtsp_display_path(input_url: str | None) -> str:
    if not input_url:
        return "RTSP path указан"
    try:
        parsed = urlparse(input_url)
        path = f"{parsed.path or ''}{('?' + parsed.query) if parsed.query else ''}"
        return path or "RTSP path указан"
    except Exception:
        return "RTSP path указан"


def safe_preview_token(value: str | None) -> str | None:
    if not value:
        return None
    token = str(value).strip()
    if not token:
        return None
    if all(ch.isalnum() or ch in {"-", "_"} for ch in token):
        return token
    return None


def capture_camera_preview(input_url: str, transport: str, output_path: Path) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-v", "error",
        "-rtsp_transport", transport,
        "-i", input_url,
        "-vf", "scale=640:-2:flags=lanczos,boxblur=0.25",
        "-frames:v", "1",
        "-q:v", "5",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        output_path.unlink(missing_ok=True)
        return False
    return output_path.exists() and output_path.stat().st_size > 0


def test_preview_destination(payload: dict) -> tuple[Path, str, str]:
    camera_id = payload.get("camera_id")
    if camera_id:
        path = settings.camera_preview_path(int(camera_id))
        return path, settings.camera_preview_url(int(camera_id)), ""
    token = uuid4().hex
    path = settings.camera_test_preview_path(token)
    return path, settings.camera_test_preview_url(token), token


def attach_test_preview_to_camera(token: str | None, camera_id: int) -> None:
    safe_token = safe_preview_token(token)
    if not safe_token:
        return
    source = settings.camera_test_preview_path(safe_token)
    if not source.exists():
        return
    destination = settings.camera_preview_path(camera_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    source.unlink(missing_ok=True)


def camera_recording_segments(db: Session, camera_id: int) -> list[RecordingSegment]:
    return (
        db.query(RecordingSegment)
        .filter(RecordingSegment.camera_id == int(camera_id))
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
        .all()
    )


def deletion_reason_counts(result: dict) -> dict:
    counts = {}
    for item in result.get("items") or []:
        reason = item.get("reason") or "unknown"
        counts[reason] = int(counts.get(reason) or 0) + 1
    for reason, value in (result.get("reason_counts") or {}).items():
        counts.setdefault(reason, int(value or 0))
    return counts


def compact_deletion_result(result: dict) -> dict:
    return {
        "ok": bool(result.get("ok")),
        "operation": result.get("operation"),
        "dry_run": bool(result.get("dry_run")),
        "requested_count": int(result.get("requested_count") or 0),
        "planned_count": int(result.get("planned_count") or 0),
        "deleted_count": int(result.get("deleted_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "not_found_count": int(result.get("not_found_count") or 0),
        "bytes_freed": int(result.get("bytes_freed") or 0),
        "estimated_freed_bytes": int(result.get("estimated_freed_bytes") or result.get("bytes_freed") or 0),
        "reason_counts": deletion_reason_counts(result),
        "active_blockers": int(deletion_reason_counts(result).get("active_job") or 0),
        "limit_exceeded": bool(result.get("limit_exceeded")),
        "warnings": list(result.get("warnings") or []),
        "items": [
            {
                "segment_id": item.get("segment_id"),
                "camera_id": item.get("camera_id"),
                "action": item.get("action"),
                "reason": item.get("reason"),
                "size_bytes": int(item.get("size_bytes") or 0),
                "error": item.get("error"),
            }
            for item in (result.get("items") or [])[:100]
        ],
    }


def mark_camera_delete_preview_unsafe(recordings: dict) -> dict:
    if (
        recordings["requested_count"] != recordings["planned_count"]
        or recordings["failed_count"]
        or recordings["skipped_count"]
    ):
        recordings["ok"] = False
        if CAMERA_DELETE_UNSAFE_WITH_FILES_REASON not in recordings["warnings"]:
            recordings["warnings"].append(CAMERA_DELETE_UNSAFE_WITH_FILES_REASON)
    return recordings


def camera_delete_response(
    *,
    camera: Camera,
    delete_files: bool,
    status_value: str,
    recordings: dict,
    preview_deleted: bool = False,
) -> dict:
    return {
        "ok": status_value == "deleted",
        "status": status_value,
        "camera_id": camera.id,
        "camera_name": camera.name,
        "delete_files": bool(delete_files),
        "recordings": recordings,
        "preview_cleanup": {
            "deleted": bool(preview_deleted),
            "scope": "camera_preview_only",
        },
    }


def require_recording_delete_permission_for_camera_delete(current_user: User) -> None:
    if not user_has_permission(getattr(current_user, "role", ""), "delete_recordings"):
        raise HTTPException(status_code=403, detail="delete_files=true requires delete_recordings permission")


def audit_camera_delete(
    db: Session,
    *,
    actor: User,
    request: Request,
    event_type: str,
    severity: str,
    camera: Camera,
    delete_files: bool,
    status_value: str,
    recordings: dict,
) -> None:
    create_event(
        db=db,
        actor=actor,
        category="cameras",
        event_type=event_type,
        severity=severity,
        message_ru=f"{actor.username} запросил удаление камеры {camera.name}: {status_value}",
        message_en=f"{actor.username} requested camera deletion for {camera.name}: {status_value}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        metadata={
            "delete_files": bool(delete_files),
            "status": status_value,
            "recordings": {
                "requested_count": recordings.get("requested_count"),
                "planned_count": recordings.get("planned_count"),
                "deleted_count": recordings.get("deleted_count"),
                "skipped_count": recordings.get("skipped_count"),
                "failed_count": recordings.get("failed_count"),
                "bytes_freed": recordings.get("bytes_freed"),
                "reason_counts": recordings.get("reason_counts"),
                "active_blockers": recordings.get("active_blockers"),
            },
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )


def delete_camera_preview(camera_id: int) -> bool:
    preview_path = settings.camera_preview_path(camera_id)
    existed = preview_path.exists()
    preview_path.unlink(missing_ok=True)
    return existed


@router.put("/{camera_id}", response_model=CameraResponse)
def update_camera(
    camera_id: int,
    payload: CameraUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    data = payload.model_dump(exclude_unset=True)
    preview_token = data.pop("preview_token", None)

    if "name" in data and data["name"] != camera.name:
        existing = db.query(Camera).filter(Camera.name == data["name"]).first()
        if existing and existing.id != camera.id:
            raise HTTPException(status_code=400, detail="Камера с таким именем уже существует")

    if "storage_quota_gb" in data and data["storage_quota_gb"] is not None:
        data["storage_quota_gb"] = max(int(data["storage_quota_gb"]), 50)

    existing_password = decrypt_text(camera.password_encrypted)
    password_for_rtsp = data.get("password") if data.get("password") else existing_password
    username_for_rtsp = data.get("username", camera.username)
    next_protocol = str(data.get("protocol", camera.protocol or "rtsp")).lower()
    if next_protocol == "rtsp":
        data.pop("rtsp_host", None)
        data.pop("rtsp_port", None)
        host_for_rtsp = data.get("host", camera.host)
        port_for_rtsp = data.get("port", camera.port)
    else:
        host_for_rtsp = data.pop("rtsp_host", None) or data.get("host", camera.rtsp_reachable_host or camera.host)
        port_for_rtsp = data.pop("rtsp_port", None) or camera.rtsp_reachable_port or 554

    if "rtsp_main_url" in data and data["rtsp_main_url"] is not None:
        data["rtsp_main_url"] = assemble_rtsp_url(
            host_for_rtsp,
            port_for_rtsp,
            username_for_rtsp,
            password_for_rtsp,
            data["rtsp_main_url"],
        )

    if "rtsp_sub_url" in data and data["rtsp_sub_url"] is not None:
        data["rtsp_sub_url"] = assemble_rtsp_url(
            host_for_rtsp,
            port_for_rtsp,
            username_for_rtsp,
            password_for_rtsp,
            data["rtsp_sub_url"],
        )

    if "password" in data and data["password"]:
        camera.password_encrypted = encrypt_text(data.pop("password"))
    elif "password" in data:
        data.pop("password")

    old_values = {
        "name": camera.name,
        "enabled": bool(camera.enabled),
        "protocol": camera.protocol,
        "host": camera.host,
        "port": camera.port,
        "username": camera.username,
        "rtsp_transport": camera.rtsp_transport,
        "recording_mode": camera.recording_mode,
        "default_live_stream": camera.default_live_stream,
        "default_record_stream": camera.default_record_stream,
        "segment_minutes": camera.segment_minutes,
        "retention_days": camera.retention_days,
        "storage_quota_gb": camera.storage_quota_gb,
    }
    for key, value in data.items():
        setattr(camera, key, value)

    db.add(camera)
    db.commit()
    db.refresh(camera)
    attach_test_preview_to_camera(preview_token, camera.id)
    changed = {}
    for key, old_value in old_values.items():
        new_value = getattr(camera, key, None)
        if old_value != new_value:
            changed[key] = {"old": old_value, "new": new_value}
    credential_changed = bool(payload.password)
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.updated",
        message_ru=f"{current_user.username} изменил камеру {camera.name}",
        message_en=f"{current_user.username} updated camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        metadata={"changed": changed, "credential_changed": credential_changed},
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return camera


@router.post("/{camera_id}/enable", response_model=CameraResponse)
def enable_camera(
    camera_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    camera.enabled = True
    camera.status = "enabled"
    camera.last_error = None
    db.add(camera)
    db.commit()
    db.refresh(camera)
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.enabled",
        message_ru=f"{current_user.username} включил камеру {camera.name}",
        message_en=f"{current_user.username} enabled camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return camera


@router.post("/{camera_id}/disable", response_model=CameraResponse)
def disable_camera(
    camera_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    camera.enabled = False
    camera.status = "disabled"
    camera.last_error = None
    db.add(camera)
    db.commit()
    db.refresh(camera)
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.disabled",
        message_ru=f"{current_user.username} отключил камеру {camera.name}",
        message_en=f"{current_user.username} disabled camera {camera.name}",
        target_type="camera",
        target_id=camera.id,
        target_name=camera.name,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return camera


@router.get("/{camera_id}/delete-preview")
def preview_delete_camera(
    camera_id: int,
    request: Request,
    delete_files: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    segments = camera_recording_segments(db, camera.id)
    if delete_files:
        require_recording_delete_permission_for_camera_delete(current_user)
        recordings = mark_camera_delete_preview_unsafe(
            compact_deletion_result(
                preview_segments(
                    db,
                    segments,
                    operation="camera_delete_preview",
                    reason=CAMERA_DELETE_FILES_REASON,
                )
            )
        )
    else:
        recordings = {
            "ok": len(segments) == 0,
            "operation": "camera_delete_preview",
            "dry_run": True,
            "requested_count": len(segments),
            "planned_count": 0,
            "deleted_count": 0,
            "skipped_count": len(segments),
            "failed_count": 0,
            "not_found_count": 0,
            "bytes_freed": 0,
            "estimated_freed_bytes": 0,
            "reason_counts": {CAMERA_DELETE_NO_FILES_BLOCK_REASON: len(segments)} if segments else {},
            "active_blockers": 0,
            "limit_exceeded": False,
            "warnings": [],
            "items": [
                {
                    "segment_id": segment.id,
                    "camera_id": segment.camera_id,
                    "action": "skipped",
                    "reason": CAMERA_DELETE_NO_FILES_BLOCK_REASON,
                    "size_bytes": int(segment.size_bytes or 0),
                    "error": None,
                }
                for segment in segments[:100]
            ],
        }

    status_value = "preview_safe" if recordings["ok"] else "preview_blocked"
    audit_camera_delete(
        db,
        actor=current_user,
        request=request,
        event_type="cameras.delete_preview",
        severity="info" if recordings["ok"] else "warning",
        camera=camera,
        delete_files=delete_files,
        status_value=status_value,
        recordings=recordings,
    )
    return camera_delete_response(
        camera=camera,
        delete_files=delete_files,
        status_value=status_value,
        recordings=recordings,
    )


@router.delete("/{camera_id}")
def delete_camera(
    camera_id: int,
    request: Request,
    delete_files: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    segments = camera_recording_segments(db, camera.id)

    if not delete_files and segments:
        recordings = {
            "ok": False,
            "operation": "camera_delete_without_files",
            "dry_run": False,
            "requested_count": len(segments),
            "planned_count": 0,
            "deleted_count": 0,
            "skipped_count": len(segments),
            "failed_count": 0,
            "not_found_count": 0,
            "bytes_freed": 0,
            "estimated_freed_bytes": 0,
            "reason_counts": {CAMERA_DELETE_NO_FILES_BLOCK_REASON: len(segments)},
            "active_blockers": 0,
            "limit_exceeded": False,
            "warnings": ["camera_delete_blocked_recordings_exist"],
            "items": [
                {
                    "segment_id": segment.id,
                    "camera_id": segment.camera_id,
                    "action": "skipped",
                    "reason": CAMERA_DELETE_NO_FILES_BLOCK_REASON,
                    "size_bytes": int(segment.size_bytes or 0),
                    "error": None,
                }
                for segment in segments[:100]
            ],
        }
        audit_camera_delete(
            db,
            actor=current_user,
            request=request,
            event_type="cameras.delete_blocked",
            severity="warning",
            camera=camera,
            delete_files=False,
            status_value="blocked",
            recordings=recordings,
        )
        raise HTTPException(
            status_code=409,
            detail=camera_delete_response(
                camera=camera,
                delete_files=False,
                status_value="blocked",
                recordings=recordings,
            ),
        )

    if delete_files:
        require_recording_delete_permission_for_camera_delete(current_user)
        preview = mark_camera_delete_preview_unsafe(
            compact_deletion_result(
                preview_segments(
                    db,
                    segments,
                    operation="camera_delete_preview",
                    reason=CAMERA_DELETE_FILES_REASON,
                )
            )
        )
        if not preview["ok"]:
            audit_camera_delete(
                db,
                actor=current_user,
                request=request,
                event_type="cameras.delete_blocked",
                severity="warning",
                camera=camera,
                delete_files=True,
                status_value="blocked",
                recordings=preview,
            )
            raise HTTPException(
                status_code=409,
                detail=camera_delete_response(
                    camera=camera,
                    delete_files=True,
                    status_value="blocked",
                    recordings=preview,
                ),
            )

        recordings = compact_deletion_result(
            execute_segments(
                db,
                segments,
                actor=current_user,
                operation="camera_delete_with_files",
                reason=CAMERA_DELETE_FILES_REASON,
                max_candidates=max(len(segments), 1),
            )
        )
        if recordings["failed_count"] or recordings["skipped_count"] or recordings["limit_exceeded"]:
            recordings["ok"] = False
            audit_camera_delete(
                db,
                actor=current_user,
                request=request,
                event_type="cameras.delete_failed",
                severity="error",
                camera=camera,
                delete_files=True,
                status_value="blocked",
                recordings=recordings,
            )
            raise HTTPException(
                status_code=409,
                detail=camera_delete_response(
                    camera=camera,
                    delete_files=True,
                    status_value="blocked",
                    recordings=recordings,
                ),
            )
    else:
        recordings = {
            "ok": True,
            "operation": "camera_delete_without_files",
            "dry_run": False,
            "requested_count": 0,
            "planned_count": 0,
            "deleted_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "not_found_count": 0,
            "bytes_freed": 0,
            "estimated_freed_bytes": 0,
            "reason_counts": {},
            "active_blockers": 0,
            "limit_exceeded": False,
            "warnings": [],
            "items": [],
        }

    preview_deleted = delete_camera_preview(camera.id)
    response = camera_delete_response(
        camera=camera,
        delete_files=delete_files,
        status_value="deleted",
        recordings=recordings,
        preview_deleted=preview_deleted,
    )
    db.delete(camera)
    db.commit()
    audit_camera_delete(
        db,
        actor=current_user,
        request=request,
        event_type="cameras.deleted",
        severity="info",
        camera=camera,
        delete_files=delete_files,
        status_value="deleted",
        recordings=recordings,
    )
    return response
