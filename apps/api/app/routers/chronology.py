from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import user_has_permission
from app.models.camera import Camera
from app.models.user import User
from app.routers.deps import FORBIDDEN_DETAIL, get_db, require_permission

router = APIRouter(prefix="/chronology", tags=["chronology"])

FILE_TS_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.mp4$", re.IGNORECASE)


def _parse_file_start(path: Path) -> datetime | None:
    match = FILE_TS_RE.search(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d-%H-%M-%S")
    except Exception:
        return None


def _camera_root(camera: Camera) -> Path:
    return Path(settings.storage_root) / camera.storage_folder_name


def _safe_relative_path(root: Path, file_path: Path) -> str:
    return file_path.resolve().relative_to(root.resolve()).as_posix()


def _resolve_camera_file(camera: Camera, rel_path: str) -> Path:
    root = _camera_root(camera).resolve()
    target = (root / rel_path).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=400, detail="Некорректный путь")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return target


def _validate_token(token: str, db: Session):
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Недействительный токен")

    username = payload.get("sub")
    user = db.query(User).filter(User.username == username).first() if username else None
    if not user or not getattr(user, "is_active", True):
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    if not user_has_permission(user.role, "view_timeline"):
        raise HTTPException(status_code=403, detail=FORBIDDEN_DETAIL)


def _build_camera_files(camera: Camera):
    root = _camera_root(camera)
    if not root.exists():
        return []

    files = []
    for path in root.rglob("*.mp4"):
        start_dt = _parse_file_start(path)
        if start_dt is None:
            continue

        try:
            stat = path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)
        except Exception:
            mtime = start_dt

        files.append((start_dt, mtime, path))

    files.sort(key=lambda x: x[0])
    return files


def _build_file_intervals(files):
    intervals = []

    for idx, (start_dt, mtime_dt, path) in enumerate(files):
        next_start = files[idx + 1][0] if idx + 1 < len(files) else None

        end_candidates = []
        if next_start and next_start > start_dt:
            end_candidates.append(next_start)
        if mtime_dt > start_dt:
            end_candidates.append(mtime_dt)

        if end_candidates:
            end_dt = min(end_candidates)
        else:
            end_dt = start_dt

        if end_dt > start_dt:
            intervals.append((start_dt, end_dt, path))

    return intervals


def _merge_ranges(ranges: list[tuple[datetime, datetime]], gap_tolerance_sec: int = 2):
    if not ranges:
        return []

    ranges = sorted(ranges, key=lambda x: x[0])
    merged = [ranges[0]]

    for start_dt, end_dt in ranges[1:]:
        last_start, last_end = merged[-1]
        gap = (start_dt - last_end).total_seconds()

        if gap <= gap_tolerance_sec:
            merged[-1] = (last_start, max(last_end, end_dt))
        else:
            merged.append((start_dt, end_dt))

    return merged


@router.get("/playback")
def chronology_playback(
    camera_id: int = Query(...),
    ts: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_timeline")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    try:
        target_dt = datetime.fromisoformat(ts)
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректная дата/время")

    files = _build_camera_files(camera)
    if not files:
        return {
            "camera_id": camera_id,
            "has_video": False,
            "file_url": None,
            "rel_path": None,
            "offset_sec": 0,
        }

    intervals = _build_file_intervals(files)

    chosen = None
    for start_dt, end_dt, path in intervals:
        if start_dt <= target_dt < end_dt:
            chosen = (start_dt, end_dt, path)
            break

    if not chosen:
        return {
            "camera_id": camera_id,
            "has_video": False,
            "file_url": None,
            "rel_path": None,
            "offset_sec": 0,
        }

    chosen_start, chosen_end, chosen_path = chosen
    offset_sec = int((target_dt - chosen_start).total_seconds())
    rel_path = _safe_relative_path(_camera_root(camera), chosen_path)

    return {
        "camera_id": camera_id,
        "has_video": True,
        "file_url": f"/api/chronology/file?camera_id={camera_id}&rel_path={rel_path}",
        "rel_path": rel_path,
        "offset_sec": offset_sec,
        "file_start": chosen_start.isoformat(),
        "file_end": chosen_end.isoformat(),
    }


@router.get("/ranges")
def chronology_ranges(
    camera_ids: str = Query(..., description="CSV camera ids, example: 9,10,11"),
    from_ts: str = Query(..., alias="from"),
    to_ts: str = Query(..., alias="to"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_timeline")),
):
    try:
        parsed_ids = [int(x.strip()) for x in camera_ids.split(",") if x.strip()]
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный список camera_ids")

    if not parsed_ids:
        raise HTTPException(status_code=400, detail="Не переданы camera_ids")

    try:
        range_from = datetime.fromisoformat(from_ts)
        range_to = datetime.fromisoformat(to_ts)
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный диапазон времени")

    if range_to <= range_from:
        raise HTTPException(status_code=400, detail="Параметр to должен быть больше from")

    cameras = (
        db.query(Camera)
        .filter(Camera.id.in_(parsed_ids))
        .all()
    )
    camera_map = {camera.id: camera for camera in cameras}

    result = {}

    for camera_id in parsed_ids:
        camera = camera_map.get(camera_id)
        if not camera:
          result[str(camera_id)] = {
              "camera_id": camera_id,
              "camera_name": None,
              "ranges": [],
          }
          continue

        files = _build_camera_files(camera)
        intervals = _build_file_intervals(files)

        clipped = []
        for start_dt, end_dt, _path in intervals:
            if end_dt <= range_from or start_dt >= range_to:
                continue

            clip_start = max(start_dt, range_from)
            clip_end = min(end_dt, range_to)

            if clip_end > clip_start:
                clipped.append((clip_start, clip_end))

        merged = _merge_ranges(clipped, gap_tolerance_sec=2)

        result[str(camera_id)] = {
            "camera_id": camera_id,
            "camera_name": camera.name,
            "ranges": [
                {
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                }
                for start_dt, end_dt in merged
            ],
        }

    return {
        "from": range_from.isoformat(),
        "to": range_to.isoformat(),
        "items": result,
    }


@router.get("/file")
def chronology_file(
    camera_id: int = Query(...),
    rel_path: str = Query(...),
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    _validate_token(token, db)

    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")

    file_path = _resolve_camera_file(camera, rel_path)
    return FileResponse(file_path, media_type="video/mp4", filename=file_path.name)
