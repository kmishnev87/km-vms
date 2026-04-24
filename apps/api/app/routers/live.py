from __future__ import annotations

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
from app.services.live_hls import (
    ensure_stream,
    get_playlist_file,
    get_segment_file,
    get_stream_debug,
    list_stream_status,
    stop_all_streams,
    stop_stream,
)

router = APIRouter(prefix="/live", tags=["live"])

StreamKey = Literal["main", "sub", "sub2"]


class LiveStartPayload(BaseModel):
    camera_id: int
    stream: StreamKey = "main"


class LiveStopPayload(BaseModel):
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


@router.post("/start")
def start_live_stream(
    payload: LiveStartPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    camera = _get_camera(db, payload.camera_id)
    result = ensure_stream(camera, payload.stream)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Не удалось запустить live поток")
    return result


@router.post("/stop")
def stop_live_stream(
    payload: LiveStopPayload,
    current_user: User = Depends(get_current_user),
):
    stopped = stop_stream(payload.camera_id, payload.stream)
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
    stopped_count = stop_all_streams()
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
    items = list_stream_status(camera_id=camera_id, stream=stream)
    return {
        "items": items,
        "count": len(items),
    }


@router.get("/{camera_id}/{stream}/index.m3u8")
def live_playlist(
    request: Request,
    camera_id: int,
    stream: StreamKey,
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _authorize_live_request(request, token)

    camera = _get_camera(db, camera_id)
    result = ensure_stream(camera, stream, wait_for_ready=False)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Не удалось запустить live поток")

    playlist = get_playlist_file(camera_id, stream)
    if not playlist.exists() or playlist.stat().st_size <= 0:
        debug = get_stream_debug(camera_id, stream)
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Плейлист HLS пока не готов",
                "debug": debug,
            },
        )

    playlist_text = playlist.read_text(encoding="utf-8")
    lines = []
    for line in playlist_text.splitlines():
        if line.endswith(".ts"):
            line = f"/api/live/{camera_id}/{stream}/{line}?token={token}"
        lines.append(line)

    patched_playlist = "\n".join(lines) + "\n"
    return Response(
        content=patched_playlist,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{camera_id}/{stream}/{filename}")
def live_segment(
    request: Request,
    camera_id: int,
    stream: StreamKey,
    filename: str,
    token: Optional[str] = Query(default=None),
):
    _authorize_live_request(request, token)

    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Недопустимое имя сегмента")

    file_path = get_segment_file(camera_id, stream, filename)
    if not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="Сегмент не найден")

    media_type = "video/mp2t" if filename.endswith(".ts") else "application/octet-stream"
    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{camera_id}/{stream}/debug")
def live_debug(
    request: Request,
    camera_id: int,
    stream: StreamKey,
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    _authorize_live_request(request, token)
    _get_camera(db, camera_id)
    return get_stream_debug(camera_id, stream)
