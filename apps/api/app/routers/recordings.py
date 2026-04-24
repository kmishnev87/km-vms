from __future__ import annotations

import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.camera import Camera
from app.models.user import User
from app.routers.deps import get_current_user

router = APIRouter(prefix="/recordings", tags=["recordings"])

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v"}
CHUNK_SIZE = 1024 * 1024

FILENAME_TS_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})-(?P<time>\d{2}-\d{2}-\d{2})(?=\.[^.]+$)"
)


class BulkDeleteRequest(BaseModel):
    paths: list[str]


def storage_root() -> Path:
    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_resolve_relative(relative_path: str) -> Path:
    if not relative_path:
        raise HTTPException(status_code=400, detail="Пустой путь")

    root = storage_root().resolve()
    target = (root / relative_path).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Недопустимый путь")

    return target


def human_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_bytes)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit in {"GB", "TB"}:
        return f"{value:.2f} {unit}"
    if unit == "MB":
        return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    if unit == "KB":
        return f"{value:.0f} {unit}"
    return f"{int(value)} {unit}"


def format_local_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y, %H:%M:%S")


def parse_created_at_from_filename(filename: str) -> Optional[datetime]:
    match = FILENAME_TS_RE.search(filename)
    if not match:
        return None

    raw = f"{match.group('date')} {match.group('time').replace('-', ':')}"
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def get_file_created_display(file_path: Path) -> str:
    from_name = parse_created_at_from_filename(file_path.name)
    if from_name:
        return format_local_dt(from_name)

    stat = file_path.stat()
    ts = getattr(stat, "st_ctime", stat.st_mtime)
    return format_local_dt(datetime.fromtimestamp(ts))


def get_sort_key(file_path: Path) -> float:
    from_name = parse_created_at_from_filename(file_path.name)
    if from_name:
        return from_name.timestamp()

    stat = file_path.stat()
    return float(getattr(stat, "st_ctime", stat.st_mtime))


def collect_camera_names(db: Session) -> list[str]:
    names = {c.name for c in db.query(Camera).order_by(Camera.name.asc()).all()}
    root = storage_root()
    if root.exists():
        for item in root.iterdir():
            if item.is_dir():
                names.add(item.name)
    return sorted(names)


def collect_recording_files(camera_name: Optional[str] = None) -> list[dict]:
    root = storage_root()
    cameras = [camera_name] if camera_name else [p.name for p in root.iterdir() if p.is_dir()]

    items: list[dict] = []
    for cam in cameras:
        cam_dir = root / cam
        if not cam_dir.exists() or not cam_dir.is_dir():
            continue

        for file_path in cam_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue

            rel = file_path.resolve().relative_to(root.resolve()).as_posix()
            stat = file_path.stat()

            items.append(
                {
                    "camera": cam,
                    "path": rel,
                    "filename": file_path.name,
                    "created_at": get_file_created_display(file_path),
                    "_sort_ts": get_sort_key(file_path),
                    "size_bytes": stat.st_size,
                    "size_human": human_size(stat.st_size),
                }
            )

    items.sort(key=lambda x: x["_sort_ts"], reverse=True)

    for item in items:
        item.pop("_sort_ts", None)

    return items


def stream_video(request: Request, file_path: Path):
    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("range")

    start = 0
    end = file_size - 1

    if range_header:
        try:
            units, range_spec = range_header.split("=", 1)
            if units.strip().lower() != "bytes":
                raise ValueError("Only bytes range supported")
            start_s, end_s = range_spec.split("-", 1)
            if start_s.strip():
                start = int(start_s)
            if end_s.strip():
                end = int(end_s)
        except Exception:
            raise HTTPException(status_code=400, detail="Некорректный Range header")

    if start >= file_size or end >= file_size or start > end:
        raise HTTPException(status_code=416, detail="Range Not Satisfiable")

    media_type, _ = mimetypes.guess_type(str(file_path))
    media_type = media_type or "video/mp4"

    def iterfile():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk_size = min(CHUNK_SIZE, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                yield data
                remaining -= len(data)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Type": media_type,
        "Cache-Control": "no-store",
    }

    status_code = 206 if range_header else 200
    return StreamingResponse(iterfile(), status_code=status_code, headers=headers)


@router.get("/cameras")
def list_recording_cameras(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {"items": collect_camera_names(db)}


@router.get("")
def list_recordings(
    camera: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    items = collect_recording_files(camera_name=camera if camera and camera != "__all__" else None)
    total = sum(item["size_bytes"] for item in items)
    return {
        "items": items,
        "summary": {
            "count": len(items),
            "size_bytes": total,
            "size_human": human_size(total),
        },
    }


@router.get("/download")
def download_recording(
    path: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    file_path = safe_resolve_relative(path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type or "application/octet-stream",
    )


@router.get("/stream")
def stream_recording(
    request: Request,
    path: str = Query(...),
):
    file_path = safe_resolve_relative(path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return stream_video(request, file_path)


@router.delete("")
def delete_recording(
    path: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    file_path = safe_resolve_relative(path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    file_path.unlink(missing_ok=True)
    return {"ok": True}


@router.post("/bulk-delete")
def bulk_delete_recordings(
    payload: BulkDeleteRequest,
    current_user: User = Depends(get_current_user),
):
    deleted = 0
    for rel in payload.paths:
        try:
            file_path = safe_resolve_relative(rel)
            if file_path.exists() and file_path.is_file():
                file_path.unlink(missing_ok=True)
                deleted += 1
        except HTTPException:
            continue

    return {"ok": True, "deleted": deleted}


@router.delete("/by-camera")
def delete_recordings_by_camera(
    camera: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    if not camera:
        raise HTTPException(status_code=400, detail="Камера не указана")

    root = storage_root()
    cam_dir = (root / camera).resolve()

    try:
        cam_dir.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Недопустимое имя камеры")

    if not cam_dir.exists() or not cam_dir.is_dir():
        return {"ok": True, "deleted": 0}

    deleted = 0
    for file_path in cam_dir.rglob("*"):
        if file_path.is_file() and file_path.suffix.lower() in VIDEO_EXTENSIONS:
            file_path.unlink(missing_ok=True)
            deleted += 1

    return {"ok": True, "deleted": deleted}


@router.delete("/all")
def delete_all_recordings(
    current_user: User = Depends(get_current_user),
):
    root = storage_root()
    deleted = 0

    for file_path in root.rglob("*"):
        if file_path.is_file() and file_path.suffix.lower() in VIDEO_EXTENSIONS:
            file_path.unlink(missing_ok=True)
            deleted += 1

    return {"ok": True, "deleted": deleted}
