from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

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
from app.services.live_engine_v2 import manager

router = APIRouter(prefix="/live", tags=["live"])

StreamKey = Literal["main", "sub", "sub2"]
HLS_FILENAME_RE = re.compile(r"^(index\.m3u8|seg_\d+\.ts)$")


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


def _hls_debug_payload(camera_id: int, stream: str) -> dict:
    debug = manager.debug(camera_id=camera_id, stream=stream)
    item = (debug.get("items") or [{}])[0]
    return {
        "camera_id": camera_id,
        "stream": stream,
        "status": item.get("status"),
        "running": item.get("running"),
        "ready": item.get("ready"),
        "pid": item.get("pid"),
        "pid_exists": item.get("pid_exists"),
        "pid_cmdline": item.get("pid_cmdline"),
        "process_poll": item.get("process_poll"),
        "is_zombie": item.get("is_zombie"),
        "running_verified": item.get("running_verified"),
        "process_started_at": item.get("process_started_at"),
        "process_age_seconds": item.get("process_age_seconds"),
        "mode": item.get("mode"),
        "requested_mode": item.get("requested_mode"),
        "selected_mode": item.get("selected_mode"),
        "input_codec": item.get("input_codec"),
        "input_resolution": item.get("input_resolution"),
        "input_fps": item.get("input_fps"),
        "copy_eligible": item.get("copy_eligible"),
        "browser_compatible": item.get("browser_compatible"),
        "reason_for_transcode": item.get("reason_for_transcode"),
        "high_cpu_risk": item.get("high_cpu_risk"),
        "resource_limit": item.get("resource_limit"),
        "hardware_accel_available": item.get("hardware_accel_available"),
        "hw_backend": item.get("hw_backend"),
        "hw_device": item.get("hw_device"),
        "hwaccel_mode": item.get("hwaccel_mode"),
        "selected_pipeline": item.get("selected_pipeline"),
        "selected_backend": item.get("selected_backend"),
        "configured_backend": item.get("configured_backend"),
        "effective_backend": item.get("effective_backend"),
        "decision_source": item.get("decision_source"),
        "decision_reason": item.get("decision_reason"),
        "copy_safe": item.get("copy_safe"),
        "heavy_stream": item.get("heavy_stream"),
        "hardware_candidates": item.get("hardware_candidates"),
        "attempted_backends": item.get("attempted_backends"),
        "failed_backends": item.get("failed_backends"),
        "hw_decode": item.get("hw_decode"),
        "hw_encode": item.get("hw_encode"),
        "fallback_to_cpu": item.get("fallback_to_cpu"),
        "hw_failure_reason": item.get("hw_failure_reason"),
        "docker_device_access_ok": item.get("docker_device_access_ok"),
        "hardware_misconfigured": item.get("hardware_misconfigured"),
        "playlist_path": item.get("playlist_path"),
        "playlist_exists": item.get("playlist_exists"),
        "segment_count": item.get("segment_count"),
        "exit_code": item.get("exit_code"),
        "failure_reason": item.get("failure_reason"),
        "last_error": item.get("last_error"),
        "stderr_tail": item.get("stderr_tail"),
        "startup_elapsed_seconds": item.get("startup_elapsed_seconds"),
        "startup_deadline_seconds": item.get("startup_deadline_seconds"),
        "startup_hard_deadline_seconds": item.get("startup_hard_deadline_seconds"),
        "last_ffmpeg_progress_at": item.get("last_ffmpeg_progress_at"),
        "ffmpeg_progress_detected": item.get("ffmpeg_progress_detected"),
        "hardware_progress_detected": item.get("hardware_progress_detected"),
        "hardware_readiness_elapsed": item.get("hardware_readiness_elapsed"),
        "last_fps": item.get("last_fps"),
        "last_speed": item.get("last_speed"),
        "speed_state": item.get("speed_state"),
        "stop_reason": item.get("stop_reason"),
        "stopped_by_backend": item.get("stopped_by_backend"),
        "state_changed_at": item.get("state_changed_at"),
        "last_state_transition": item.get("last_state_transition"),
    }


def _serve_live_playlist(camera_id: int, stream: str, media_token: str) -> Response:
    playlist = manager.get_playlist_file(camera_id, stream)
    if not playlist.exists() or playlist.stat().st_size <= 0:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "HLS-плейлист еще не готов",
                "debug": _hls_debug_payload(camera_id, stream),
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
    return {
        "items": items,
        "count": len(items),
    }


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
            detail={
                "message": result.get("error") or "Не удалось открыть live viewer",
                "debug": result,
            },
        )
    return result


@router.delete("/viewers/{viewer_id}")
def close_live_viewer(
    viewer_id: str,
    current_user: User = Depends(require_permission("view_live")),
):
    closed = manager.close_viewer(viewer_id)
    return {
        "ok": True,
        "closed": closed,
        "viewer_id": viewer_id,
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
        "viewer_id": viewer_id,
    }


@router.get("/debug")
def live_debug_all(
    camera_id: Optional[int] = Query(default=None),
    stream: Optional[StreamKey] = Query(default=None),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return manager.debug(camera_id=camera_id, stream=stream)


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
    return manager.debug(camera_id=camera_id, stream=stream)


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
