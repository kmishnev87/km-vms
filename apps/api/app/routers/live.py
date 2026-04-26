from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.camera import Camera
from app.models.user import User
from app.routers.deps import get_current_user
from app.services.live_engine_v2 import manager

router = APIRouter(prefix="/live", tags=["live"])

StreamKey = Literal["main", "sub", "sub2"]
HLS_FILENAME_RE = re.compile(r"^(index\.m3u8|seg_\d+\.ts)$")


class LiveStartPayload(BaseModel):
    camera_id: int
    stream: StreamKey = "main"


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


def _authorize_live_request(request: Request, token: Optional[str]) -> str:
    raw_token = token

    if not raw_token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            raw_token = auth[7:].strip()

    if not raw_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = jwt.decode(raw_token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token")

    return username


def _validate_hls_filename(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid HLS filename")
    if not HLS_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid HLS filename")


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
        "mode": item.get("mode"),
        "playlist_path": item.get("playlist_path"),
        "playlist_exists": item.get("playlist_exists"),
        "segment_count": item.get("segment_count"),
        "exit_code": item.get("exit_code"),
        "failure_reason": item.get("failure_reason"),
        "last_error": item.get("last_error"),
        "stderr_tail": item.get("stderr_tail"),
    }


def _serve_live_playlist(camera_id: int, stream: str, token: Optional[str]) -> Response:
    playlist = manager.get_playlist_file(camera_id, stream)
    if not playlist.exists() or playlist.stat().st_size <= 0:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "HLS playlist is not ready",
                "debug": _hls_debug_payload(camera_id, stream),
            },
        )

    playlist_text = playlist.read_text(encoding="utf-8")
    lines = []
    for line in playlist_text.splitlines():
        if line.endswith(".ts"):
            line = f"/api/live/{camera_id}/{stream}/{line}?token={token or ''}"
        lines.append(line)

    return Response(
        content="\n".join(lines) + "\n",
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/start")
def start_live_stream(
    payload: LiveStartPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    camera = _get_camera(db, payload.camera_id)
    result = manager.ensure_stream(camera, payload.stream, wait_for_ready=False)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Не удалось запустить live поток")
    return result


@router.post("/stop")
def stop_live_stream(
    payload: LiveStopPayload,
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
):
    camera = _get_camera(db, payload.camera_id)
    result = manager.open_viewer(camera, payload.stream)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": result.get("error") or "Не удалось открыть live viewer",
                "debug": result,
            },
        )
    return result


@router.delete("/viewers/{viewer_id}")
def close_live_viewer(
    viewer_id: str,
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
):
    return manager.debug(camera_id=camera_id, stream=stream)


@router.get("/debug/{camera_id}/{stream}")
def live_debug_stream(
    camera_id: int,
    stream: StreamKey,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_camera(db, camera_id)
    return manager.debug(camera_id=camera_id, stream=stream)


@router.get("/{camera_id}/{stream}/index.m3u8")
def live_playlist(
    request: Request,
    camera_id: int,
    stream: StreamKey,
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _authorize_live_request(request, token)
    _get_camera(db, camera_id)
    return _serve_live_playlist(camera_id, stream, token)


@router.get("/{camera_id}/{stream}/{filename}")
def live_segment(
    request: Request,
    camera_id: int,
    stream: StreamKey,
    filename: str,
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _authorize_live_request(request, token)
    _get_camera(db, camera_id)
    _validate_hls_filename(filename)

    if filename == "index.m3u8":
        return _serve_live_playlist(camera_id, stream, token)

    file_path = manager.get_segment_file(camera_id, stream, filename)
    if not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="Сегмент не найден")

    media_type = "video/mp2t" if filename.endswith(".ts") else "application/octet-stream"
    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )
