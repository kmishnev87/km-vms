from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import PERMISSION_VIEW_LIVE
from app.db.session import get_db
from app.models.camera import Camera
from app.models.user import User
from app.routers.deps import require_permission
from app.services.media_tokens import create_media_token, media_token_response, validate_media_token
from app.services.live_engine import manager

router = APIRouter(prefix="/live", tags=["live"])

StreamKey = Literal["main", "sub", "sub2"]
HLS_FILENAME_RE = re.compile(r"^(index\.m3u8|seg_\d+\.ts)$")
SENSITIVE_TEXT_RE = re.compile(
    r"(rtsp://|authorization|bearer\s+|password|credential|secret|token=|access_token|media_token|"
    r"/Volume\d*/|/storage/|/tmp/|/proc/|/dev/|ffmpeg\s|traceback|stack trace)",
    re.IGNORECASE,
)
SAFE_FAILURE_REASONS = {
    "no_rtsp_url",
    "resource_limit",
    "startup_timeout_no_progress",
    "startup_timeout_no_hls",
    "startup_timeout",
    "process_exit",
    "ffmpeg_exit",
    "copy_not_ready",
    "hardware_no_hls",
    "slow_transcode_no_hls",
    "live_start_failed",
    "stream_not_ready",
}
LIVE_STATUS_ALLOWED_FIELDS = (
    "stream_key",
    "camera_id",
    "stream",
    "stream_type",
    "running",
    "ready",
    "status",
    "mode",
    "selected_mode",
    "input_codec",
    "input_resolution",
    "input_fps",
    "output_fps",
    "audio_mode",
    "audio_enabled",
    "audio_available",
    "input_audio_codec",
    "input_audio_channels",
    "input_audio_sample_rate",
    "audio_reason",
    "browser_compatible",
    "reason_for_transcode",
    "high_cpu_risk",
    "resource_limit",
    "viewers",
    "uptime_seconds",
    "idle_seconds",
    "startup_elapsed_seconds",
    "speed_state",
    "last_fps",
    "last_speed",
    "state_changed_at",
    "last_state_transition",
    "playlist_exists",
    "segment_count",
)


class LiveStopPayload(BaseModel):
    camera_id: int
    stream: StreamKey = "main"


class LiveViewerPayload(BaseModel):
    camera_id: int
    stream: StreamKey = "main"


def _get_camera(db: Session, camera_id: int) -> Camera:
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    return camera


def _live_media_resource(camera_id: int, stream: str) -> dict:
    return {"camera_id": int(camera_id), "stream": str(stream)}


def _authorize_live_media_token(media_token: Optional[str], camera_id: int, stream: str, db: Session, request: Request | None = None) -> User:
    return validate_media_token(
        db,
        token=media_token,
        scope="live",
        resource=_live_media_resource(camera_id, stream),
        permission=PERMISSION_VIEW_LIVE,
        request=request,
        media_area="live",
    )


def _validate_hls_filename(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Некорректное имя HLS-файла")
    if not HLS_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Некорректное имя HLS-файла")


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return round(float(value), 3)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if SENSITIVE_TEXT_RE.search(text):
            return None
        return text[:160]
    return None


def _safe_failure_reason(item: dict[str, Any] | None, fallback: str = "live_start_failed") -> str | None:
    if not item:
        return fallback
    for key in ("failure_reason", "stop_reason", "recoverable_start_error", "error_code"):
        value = str(item.get(key) or "").strip().lower()
        if not value:
            continue
        value = re.sub(r"[^a-z0-9_:-]+", "_", value).strip("_")
        if value in SAFE_FAILURE_REASONS:
            return value
        if value.startswith("idle_ttl"):
            return "stream_not_ready"
    status_value = str(item.get("status") or "").strip().lower()
    if status_value == "failed":
        return fallback
    return None


def _serialize_live_status_item(item: dict[str, Any] | None) -> dict[str, Any]:
    item = item or {}
    safe = {field: _safe_scalar(item.get(field)) for field in LIVE_STATUS_ALLOWED_FIELDS}
    safe["camera_id"] = int(item.get("camera_id") or 0)
    safe["stream"] = str(item.get("stream") or item.get("stream_type") or "main")
    safe["stream_type"] = safe["stream"]
    safe["running"] = bool(item.get("running"))
    safe["ready"] = bool(item.get("ready"))
    safe["status"] = str(item.get("status") or ("ready" if safe["ready"] else "starting" if safe["running"] else "stopped"))
    safe["viewers"] = int(item.get("viewers") or 0)
    safe["playlist_exists"] = bool(item.get("playlist_exists"))
    safe["segment_count"] = int(item.get("segment_count") or item.get("segments_count") or 0)
    reason = _safe_failure_reason(item)
    if reason:
        safe["failure_reason"] = reason
        safe["safe_failure_reason"] = reason
    return {key: value for key, value in safe.items() if value is not None}


def _serialize_live_status(items: list[dict[str, Any]]) -> dict[str, Any]:
    safe_items = [_serialize_live_status_item(item) for item in items]
    return {"items": safe_items, "count": len(safe_items)}


def _serialize_live_viewer_result(result: dict[str, Any]) -> dict[str, Any]:
    item = _serialize_live_status_item(result)
    response = {
        "ok": bool(result.get("ok")),
        "viewer_id": _safe_scalar(result.get("viewer_id")),
        "stream_url": _safe_scalar(result.get("stream_url")),
        **item,
    }
    recoverable = _safe_failure_reason(result)
    if result.get("recoverable_start_error") and recoverable:
        response["recoverable_start_error"] = recoverable
    return {key: value for key, value in response.items() if value is not None}


def _serialize_live_debug(debug: dict[str, Any]) -> dict[str, Any]:
    items = [_serialize_live_status_item(item) for item in debug.get("items") or []]
    hardware = debug.get("hardware_capabilities") or {}
    return {
        "items": items,
        "count": len(items),
        "viewers_count": int(debug.get("viewers_count") or 0),
        "hardware_capabilities": {
            "hardware_accel_available": bool(hardware.get("hardware_accel_available")),
            "docker_device_access_ok": bool(hardware.get("docker_device_access_ok")),
            "hardware_misconfigured": bool(hardware.get("hardware_misconfigured")),
            "available_backends": [
                str(item)
                for item in (hardware.get("available_backends") or [])
                if isinstance(item, str) and not SENSITIVE_TEXT_RE.search(item)
            ][:8],
        },
    }


def _live_start_error_detail(result: dict[str, Any]) -> dict[str, Any]:
    reason = _safe_failure_reason(result) or "live_start_failed"
    return {"message": "Live stream could not be started", "code": reason}


def _serve_live_playlist(camera_id: int, stream: str, media_token: str) -> Response:
    playlist = manager.get_playlist_file(camera_id, stream)
    if not playlist.exists() or playlist.stat().st_size <= 0:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "HLS-плейлист еще не готов",
                "code": "stream_not_ready",
                "camera_id": camera_id,
                "stream": stream,
            },
        )

    playlist_text = playlist.read_text(encoding="utf-8")
    lines = []
    for line in playlist_text.splitlines():
        if line.endswith(".ts"):
            line = f"/api/live/{camera_id}/{stream}/{line}?media_token={media_token}"
        lines.append(line)

    return Response(
        content="\n".join(lines) + "\n",
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/stop")
def stop_live_stream(
    payload: LiveStopPayload,
    current_user: User = Depends(require_permission("manage_settings")),
):
    stopped = manager.stop_stream(payload.camera_id, payload.stream)
    return {
        "ok": True,
        "stopped": stopped,
        "camera_id": payload.camera_id,
        "stream": payload.stream,
    }


@router.post("/stop-all")
def stop_all_live_streams(
    current_user: User = Depends(require_permission("manage_settings")),
):
    stopped_count = manager.stop_all_streams()
    return {
        "ok": True,
        "stopped_count": stopped_count,
    }


@router.get("/status")
def live_status(
    camera_id: Optional[int] = Query(default=None),
    stream: Optional[StreamKey] = Query(default=None),
    current_user: User = Depends(require_permission("view_live")),
):
    items = manager.status(camera_id=camera_id, stream=stream)
    return _serialize_live_status(items)


@router.post("/viewers")
def open_live_viewer(
    payload: LiveViewerPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_live")),
):
    camera = _get_camera(db, payload.camera_id)
    result = manager.open_viewer(camera, payload.stream)
    if not result.get("ok"):
        status_code = 503 if result.get("error_code") == "resource_limit" else 400
        raise HTTPException(
            status_code=status_code,
            detail=_live_start_error_detail(result),
        )
    return _serialize_live_viewer_result(result)


@router.delete("/viewers/{viewer_id}")
def close_live_viewer(
    viewer_id: str,
    current_user: User = Depends(require_permission("view_live")),
):
    closed = manager.close_viewer(viewer_id)
    return {
        "ok": True,
        "closed": closed,
    }


@router.post("/viewers/{viewer_id}/touch")
def touch_live_viewer(
    viewer_id: str,
    current_user: User = Depends(require_permission("view_live")),
):
    touched = manager.touch_viewer(viewer_id)
    return {
        "ok": True,
        "touched": touched,
    }


@router.get("/debug")
def live_debug_all(
    camera_id: Optional[int] = Query(default=None),
    stream: Optional[StreamKey] = Query(default=None),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return _serialize_live_debug(manager.debug(camera_id=camera_id, stream=stream))


@router.post("/media-token")
def issue_live_media_token(
    payload: LiveViewerPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_live")),
):
    _get_camera(db, payload.camera_id)
    token, expires_at = create_media_token(
        user=current_user,
        scope="live",
        resource=_live_media_resource(payload.camera_id, payload.stream),
    )
    return media_token_response(token, expires_at)


@router.get("/debug/{camera_id}/{stream}")
def live_debug_stream(
    camera_id: int,
    stream: StreamKey,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    _get_camera(db, camera_id)
    return _serialize_live_debug(manager.debug(camera_id=camera_id, stream=stream))


@router.get("/{camera_id}/{stream}/index.m3u8")
def live_playlist(
    request: Request,
    camera_id: int,
    stream: StreamKey,
    media_token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _authorize_live_media_token(media_token, camera_id, stream, db, request)
    _get_camera(db, camera_id)
    return _serve_live_playlist(camera_id, stream, media_token or "")


@router.get("/{camera_id}/{stream}/{filename}")
def live_segment(
    request: Request,
    camera_id: int,
    stream: StreamKey,
    filename: str,
    media_token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _authorize_live_media_token(media_token, camera_id, stream, db, request)
    _get_camera(db, camera_id)
    _validate_hls_filename(filename)

    if filename == "index.m3u8":
        return _serve_live_playlist(camera_id, stream, media_token or "")

    file_path = manager.get_segment_file(camera_id, stream, filename)
    if not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="Сегмент не найден")

    media_type = "video/mp2t" if filename.endswith(".ts") else "application/octet-stream"
    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )
