from pathlib import Path
import shutil
import subprocess
import json
from uuid import uuid4
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import encrypt_text, decrypt_text
from app.db.session import get_db
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.user import User
from app.routers.deps import require_permission
from app.schemas.camera import CameraCreate, CameraResponse, CameraUpdate
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.storage import build_unique_folder_name, ensure_camera_folder
from app.services.onvif_service import (
    fetch_onvif_profiles,
    get_onvif_profile_config,
    update_onvif_profile,
)

router = APIRouter(prefix="/cameras", tags=["cameras"])
viewer_router = APIRouter(prefix="/viewer/cameras", tags=["viewer-cameras"])


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

    return {
        "camera_id": camera_id,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def build_test_url(payload: dict, db: Session | None = None) -> str | None:
    host = payload.get("host")
    port = payload.get("port") or 554
    username = payload.get("username")
    password = payload.get("password")

    if db is not None:
        creds = get_camera_credentials(db, payload)
        host = creds["host"] or host
        port = creds["port"] or port
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
        )
        return {
            "ok": True,
            **data,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/onvif/profile_config")
def onvif_profile_config(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)

    try:
        return get_onvif_profile_config(
            host=str(creds["host"]),
            port=int(creds["port"]),
            username=str(creds["username"]),
            password=str(creds["password"]),
            profile_token=str(payload["profile_token"]),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/onvif/update_profile")
def update_onvif_profile_route(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_cameras")),
):
    creds = get_camera_credentials(db, payload)

    try:
        return update_onvif_profile(
            host=str(creds["host"]),
            port=int(creds["port"]),
            username=str(creds["username"]),
            password=str(creds["password"]),
            profile_token=str(payload["profile_token"]),
            config=payload["config"],
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


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

    rtsp_main_url = assemble_rtsp_url(
        payload.host,
        payload.port,
        payload.username,
        payload.password,
        payload.rtsp_main_url,
    )
    rtsp_sub_url = assemble_rtsp_url(
        payload.host,
        payload.port,
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
    host_for_rtsp = data.get("host", camera.host)
    port_for_rtsp = data.get("port", camera.port)

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


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
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

    folder_path = Path(settings.storage_root) / camera.storage_folder_name
    preview_path = settings.camera_preview_path(camera.id)
    camera_name = camera.name
    camera_id_value = camera.id

    db.query(RecordingSegment).filter(RecordingSegment.camera_id == camera.id).delete()
    db.delete(camera)
    db.commit()
    create_event(
        db=db,
        actor=current_user,
        category="cameras",
        event_type="cameras.deleted",
        message_ru=f"{current_user.username} удалил камеру {camera_name}",
        message_en=f"{current_user.username} deleted camera {camera_name}",
        target_type="camera",
        target_id=camera_id_value,
        target_name=camera_name,
        metadata={"delete_files": bool(delete_files)},
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )

    if delete_files and folder_path.exists() and folder_path.is_dir():
        shutil.rmtree(folder_path, ignore_errors=True)
    preview_path.unlink(missing_ok=True)
