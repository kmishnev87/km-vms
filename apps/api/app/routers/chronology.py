from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import PERMISSION_VIEW_TIMELINE, user_has_permission
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.user import User
from app.routers.deps import FORBIDDEN_DETAIL, get_db, require_permission
from app.services.media_tokens import create_media_token, media_token_response, validate_media_token

router = APIRouter(prefix="/chronology", tags=["chronology"])

OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
SEGMENT_STATUS_FINALIZED = "finalized"
PROBLEM_INTEGRITY_STATUSES = {
    "missing_file",
    "orphan_metadata",
    "orphan_file",
    "pre_metadata_km_vms_file",
    "legacy_archive_file",
    "foreign_file",
    "unknown_file",
    "zero_size_file",
    "partial_file",
    "corrupted_file",
    "stale_writing_segment",
    "invalid_path",
    "path_outside_storage",
    "unreadable_file",
    "storage_unavailable",
}


def _storage_root() -> Path:
    return Path(settings.storage_root)


def _safe_storage_relative_path(relative_path: str) -> str:
    if not relative_path:
        raise HTTPException(status_code=400, detail="Empty path")

    root = _storage_root().resolve()
    target = (root / relative_path).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")


def _resolve_segment_path(segment: RecordingSegment, require_exists: bool = True) -> Path:
    if not segment.relative_path:
        raise HTTPException(status_code=404, detail="Recording metadata has no file path")

    rel_path = _safe_storage_relative_path(segment.relative_path)
    target = (_storage_root() / rel_path).resolve()
    if require_exists and (not target.exists() or not target.is_file()):
        raise HTTPException(status_code=404, detail="Recording file not found")
    return target


def _segment_media_metadata(segment: RecordingSegment, file_path: Path) -> dict[str, str | None]:
    extension = (segment.file_extension or file_path.suffix or "").lower()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    media_type = segment.mime_type
    if not media_type:
        media_type, _ = mimetypes.guess_type(str(file_path))
    return {
        "container_format": segment.container_format or extension.lstrip(".") or None,
        "file_extension": extension or None,
        "mime_type": media_type or "application/octet-stream",
    }


def _parse_ts(raw: str, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")


def _finalized_segments_query(db: Session):
    return db.query(RecordingSegment).filter(
        RecordingSegment.ownership == OWNERSHIP_KM_VMS,
        RecordingSegment.source == RECORDER_SOURCE,
        RecordingSegment.status == SEGMENT_STATUS_FINALIZED,
        RecordingSegment.relative_path.isnot(None),
        or_(
            RecordingSegment.integrity_status.is_(None),
            ~RecordingSegment.integrity_status.in_(PROBLEM_INTEGRITY_STATUSES),
        ),
    )


def _segment_end(segment: RecordingSegment) -> datetime | None:
    if segment.ended_at and segment.ended_at > segment.started_at:
        return segment.ended_at
    if segment.finalized_at and segment.finalized_at > segment.started_at:
        return segment.finalized_at
    if segment.duration_sec and segment.duration_sec > 0:
        return segment.started_at + timedelta(seconds=int(segment.duration_sec))
    return None


def _segment_interval(segment: RecordingSegment) -> tuple[datetime, datetime] | None:
    if not segment.started_at:
        return None
    end_dt = _segment_end(segment)
    if not end_dt or end_dt <= segment.started_at:
        return None
    return segment.started_at, end_dt


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
        raise HTTPException(status_code=404, detail="Camera not found")

    target_dt = _parse_ts(ts, "timestamp")
    segments = (
        _finalized_segments_query(db)
        .filter(RecordingSegment.camera_id == camera_id, RecordingSegment.started_at <= target_dt)
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
        .all()
    )

    for segment in segments:
        interval = _segment_interval(segment)
        if not interval:
            continue

        start_dt, end_dt = interval
        if not (start_dt <= target_dt < end_dt):
            continue

        try:
            file_path = _resolve_segment_path(segment)
        except HTTPException:
            return {
                "camera_id": camera_id,
                "has_video": False,
                "file_url": None,
                "rel_path": None,
                "offset_sec": 0,
            }

        rel_path = _safe_storage_relative_path(segment.relative_path)
        offset_sec = int((target_dt - start_dt).total_seconds())
        media_metadata = _segment_media_metadata(segment, file_path)
        return {
            "camera_id": camera_id,
            "has_video": True,
            "file_url": f"/api/chronology/file?camera_id={camera_id}&rel_path={rel_path}",
            "rel_path": rel_path,
            "offset_sec": offset_sec,
            "file_start": start_dt.isoformat(),
            "file_end": end_dt.isoformat(),
            "container_format": media_metadata["container_format"],
            "file_extension": media_metadata["file_extension"],
            "mime_type": media_metadata["mime_type"],
        }

    return {
        "camera_id": camera_id,
        "has_video": False,
        "file_url": None,
        "rel_path": None,
        "offset_sec": 0,
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
        raise HTTPException(status_code=400, detail="Invalid camera_ids")

    if not parsed_ids:
        raise HTTPException(status_code=400, detail="camera_ids required")

    range_from = _parse_ts(from_ts, "from")
    range_to = _parse_ts(to_ts, "to")
    if range_to <= range_from:
        raise HTTPException(status_code=400, detail="to must be greater than from")

    cameras = db.query(Camera).filter(Camera.id.in_(parsed_ids)).all()
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

        segments = (
            _finalized_segments_query(db)
            .filter(
                RecordingSegment.camera_id == camera_id,
                RecordingSegment.started_at < range_to,
            )
            .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
            .all()
        )

        clipped = []
        for segment in segments:
            interval = _segment_interval(segment)
            if not interval:
                continue
            try:
                _resolve_segment_path(segment)
            except HTTPException:
                continue

            start_dt, end_dt = interval
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


def _chronology_media_resource(camera_id: int, rel_path: str) -> dict:
    return {"camera_id": int(camera_id), "rel_path": _safe_storage_relative_path(rel_path)}


@router.post("/media-token")
def issue_chronology_media_token(
    camera_id: int = Query(...),
    rel_path: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_timeline")),
):
    normalized_path = _safe_storage_relative_path(rel_path)
    segment = (
        _finalized_segments_query(db)
        .filter(
            RecordingSegment.camera_id == camera_id,
            RecordingSegment.relative_path == normalized_path,
        )
        .order_by(RecordingSegment.started_at.desc(), RecordingSegment.id.desc())
        .first()
    )
    if not segment:
        raise HTTPException(status_code=404, detail="Recording metadata not found")
    _resolve_segment_path(segment)
    token, expires_at = create_media_token(
        user=current_user,
        scope="chronology",
        resource=_chronology_media_resource(camera_id, normalized_path),
    )
    return media_token_response(token, expires_at)


@router.get("/file")
def chronology_file(
    camera_id: int = Query(...),
    rel_path: str = Query(...),
    media_token: str = Query(...),
    db: Session = Depends(get_db),
):
    validate_media_token(
        db,
        token=media_token,
        scope="chronology",
        resource=_chronology_media_resource(camera_id, rel_path),
        permission=PERMISSION_VIEW_TIMELINE,
    )

    normalized_path = _safe_storage_relative_path(rel_path)
    segment = (
        _finalized_segments_query(db)
        .filter(
            RecordingSegment.camera_id == camera_id,
            RecordingSegment.relative_path == normalized_path,
        )
        .order_by(RecordingSegment.started_at.desc(), RecordingSegment.id.desc())
        .first()
    )
    if not segment:
        raise HTTPException(status_code=404, detail="Recording metadata not found")

    file_path = _resolve_segment_path(segment)
    media_metadata = _segment_media_metadata(segment, file_path)
    return FileResponse(file_path, media_type=media_metadata["mime_type"] or "application/octet-stream", filename=file_path.name)
