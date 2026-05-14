from __future__ import annotations

import mimetypes
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from app.services.recording_storage import resolve_segment_file_path
from app.services.timezone_contract import (
    ParsedTimestamp,
    TimezoneContext,
    format_system_iso,
    parse_api_timestamp,
    timestamp_matches_filename,
    timezone_context,
)

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
    from sqlalchemy.orm import object_session

    db = object_session(segment)
    if db is None:
        raise HTTPException(status_code=500, detail="Recording metadata session unavailable")
    try:
        target = resolve_segment_file_path(db, segment, require_exists=require_exists)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Recording file not found")
    except ValueError:
        raise HTTPException(status_code=404, detail="Recording metadata has no file path")
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


def _safe_download_filename(camera: Camera, segment: RecordingSegment, file_path: Path) -> str:
    raw_camera = str(camera.name or f"camera_{camera.id}")
    camera_label = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_camera).strip("_")[:40]
    camera_label = camera_label or f"camera_{camera.id}"
    stamp = (segment.started_at or datetime.utcnow()).strftime("%Y%m%dT%H%M%S")
    extension = (segment.file_extension or file_path.suffix or ".mkv").lower()
    if not extension.startswith("."):
        extension = f".{extension}"
    extension = re.sub(r"[^A-Za-z0-9.]+", "", extension) or ".mkv"
    return f"km-vms-recording-{camera_label}-{stamp}{extension}"


def _parse_ts(raw: str, field_name: str, ctx: TimezoneContext) -> ParsedTimestamp:
    try:
        return parse_api_timestamp(raw, ctx, field_name=field_name)
    except ValueError:
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


def _segment_uses_local_naive_display(segment: RecordingSegment) -> bool:
    if segment.source != RECORDER_SOURCE:
        return False
    candidates = [
        Path(str(segment.relative_path or "")).name,
        Path(str(segment.file_path or "")).name,
    ]
    return any(timestamp_matches_filename(segment.started_at, candidate) for candidate in candidates)


def _contains_target(segment: RecordingSegment, target: ParsedTimestamp) -> tuple[bool, datetime, bool]:
    interval = _segment_interval(segment)
    if not interval:
        return False, target.storage_utc, False
    start_dt, end_dt = interval
    local_naive_display = _segment_uses_local_naive_display(segment)
    if target.compatibility_local is not None:
        if start_dt <= target.compatibility_local < end_dt:
            return True, target.compatibility_local, True
        if local_naive_display:
            return False, target.compatibility_local, True
    if start_dt <= target.storage_utc < end_dt:
        return True, target.storage_utc, local_naive_display
    return False, target.storage_utc, False


def _max_query_ts(target: ParsedTimestamp) -> datetime:
    values = [target.storage_utc]
    if target.compatibility_local is not None:
        values.append(target.compatibility_local)
    return max(values)


def _segment_covering_timestamp(db: Session, *, camera_id: int, target: ParsedTimestamp) -> RecordingSegment | None:
    segments = (
        _finalized_segments_query(db)
        .filter(RecordingSegment.camera_id == camera_id, RecordingSegment.started_at <= _max_query_ts(target))
        .order_by(RecordingSegment.started_at.desc(), RecordingSegment.id.desc())
        .all()
    )
    for segment in segments:
        ok, _effective_target, _local_naive_display = _contains_target(segment, target)
        if ok:
            return segment
    return None


def _clip_segment_to_range(
    segment: RecordingSegment,
    *,
    range_from: datetime,
    range_to: datetime,
    compat_from: datetime | None,
    compat_to: datetime | None,
) -> tuple[datetime, datetime, bool] | None:
    interval = _segment_interval(segment)
    if not interval:
        return None

    start_dt, end_dt = interval
    local_naive_display = _segment_uses_local_naive_display(segment)
    if local_naive_display and compat_from is not None and compat_to is not None:
        if end_dt <= compat_from or start_dt >= compat_to:
            return None
        return max(start_dt, compat_from), min(end_dt, compat_to), True

    if end_dt <= range_from or start_dt >= range_to:
        return None
    return max(start_dt, range_from), min(end_dt, range_to), local_naive_display


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


def _merge_ranges(ranges: list[tuple[datetime, datetime, bool]], gap_tolerance_sec: int = 2):
    if not ranges:
        return []

    ranges = sorted(ranges, key=lambda x: x[0])
    merged = [ranges[0]]

    for start_dt, end_dt, local_naive_display in ranges[1:]:
        last_start, last_end, last_local_naive_display = merged[-1]
        gap = (start_dt - last_end).total_seconds()

        if gap <= gap_tolerance_sec and local_naive_display == last_local_naive_display:
            merged[-1] = (last_start, max(last_end, end_dt), last_local_naive_display)
        else:
            merged.append((start_dt, end_dt, local_naive_display))

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

    ctx = timezone_context(db)
    target = _parse_ts(ts, "timestamp", ctx)
    segments = (
        _finalized_segments_query(db)
        .filter(RecordingSegment.camera_id == camera_id, RecordingSegment.started_at <= _max_query_ts(target))
        .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
        .all()
    )

    for segment in segments:
        interval = _segment_interval(segment)
        if not interval:
            continue

        start_dt, end_dt = interval
        ok, effective_target, local_naive_display = _contains_target(segment, target)
        if not ok:
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

        rel_path = segment.relative_path.replace("\\", "/").lstrip("/")
        offset_sec = int((effective_target - start_dt).total_seconds())
        if offset_sec < 0:
            continue
        media_metadata = _segment_media_metadata(segment, file_path)
        return {
            "segment_id": segment.id,
            "camera_id": camera_id,
            "has_video": True,
            "file_url": f"/api/chronology/file?camera_id={camera_id}&rel_path={rel_path}",
            "rel_path": rel_path,
            "offset_sec": offset_sec,
            "file_start": start_dt.isoformat(),
            "file_end": end_dt.isoformat(),
            "file_start_system": format_system_iso(start_dt, ctx, local_naive=local_naive_display),
            "file_end_system": format_system_iso(end_dt, ctx, local_naive=local_naive_display),
            "display_timezone": ctx.name,
            "timestamp_display_semantic": "product_local_naive" if local_naive_display else "storage_utc_naive",
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

    ctx = timezone_context(db)
    parsed_from = _parse_ts(from_ts, "from", ctx)
    parsed_to = _parse_ts(to_ts, "to", ctx)
    range_from = parsed_from.storage_utc
    range_to = parsed_to.storage_utc
    compat_from = parsed_from.compatibility_local
    compat_to = parsed_to.compatibility_local
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
                RecordingSegment.started_at < max(range_to, compat_to or range_to),
            )
            .order_by(RecordingSegment.started_at.asc(), RecordingSegment.id.asc())
            .all()
        )

        clipped = []
        for segment in segments:
            try:
                _resolve_segment_path(segment)
            except HTTPException:
                continue

            clipped_segment = _clip_segment_to_range(
                segment,
                range_from=range_from,
                range_to=range_to,
                compat_from=compat_from,
                compat_to=compat_to,
            )
            if clipped_segment is None:
                continue
            clip_start, clip_end, local_naive_display = clipped_segment
            if clip_end > clip_start:
                clipped.append((clip_start, clip_end, local_naive_display))

        merged = _merge_ranges(clipped, gap_tolerance_sec=2)
        result[str(camera_id)] = {
            "camera_id": camera_id,
            "camera_name": camera.name,
            "ranges": [
                {
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "start_system": format_system_iso(start_dt, ctx, local_naive=local_naive_display),
                    "end_system": format_system_iso(end_dt, ctx, local_naive=local_naive_display),
                    "timestamp_display_semantic": "product_local_naive" if local_naive_display else "storage_utc_naive",
                }
                for start_dt, end_dt, local_naive_display in merged
            ],
        }

    return {
        "from": range_from.isoformat(),
        "to": range_to.isoformat(),
        "from_system": format_system_iso(range_from, ctx),
        "to_system": format_system_iso(range_to, ctx),
        "timezone": {
            "id": ctx.name,
            "source": "system_settings.timezone",
            "fallback_used": ctx.fallback_used,
            "storage_semantic": "timestamp_without_time_zone_as_utc_naive_with_local_naive_read_compatibility",
        },
        "items": result,
    }


@router.get("/download")
def chronology_download_current_recording(
    camera_id: int = Query(...),
    ts: str = Query(...),
    media_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
    request: Request = None,
):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Recording is unavailable")

    ctx = timezone_context(db)
    target = _parse_ts(ts, "timestamp", ctx)
    segment = _segment_covering_timestamp(db, camera_id=camera_id, target=target)
    if not segment:
        raise HTTPException(status_code=404, detail="Recording is unavailable")
    validate_media_token(
        db,
        token=media_token,
        scope="chronology-download",
        resource={"camera_id": camera_id, "segment_id": segment.id, "action": "download"},
        permission=PERMISSION_VIEW_TIMELINE,
        request=request,
        media_area="chronology-download",
    )
    try:
        file_path = _resolve_segment_path(segment)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=409, detail="Recording source is unavailable") from exc
        raise
    media_metadata = _segment_media_metadata(segment, file_path)
    return FileResponse(
        file_path,
        media_type=media_metadata["mime_type"] or "application/octet-stream",
        filename=_safe_download_filename(camera, segment, file_path),
    )


@router.post("/download-token")
def issue_chronology_download_token(
    camera_id: int = Query(...),
    ts: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_timeline")),
):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Recording is unavailable")
    ctx = timezone_context(db)
    target = _parse_ts(ts, "timestamp", ctx)
    segment = _segment_covering_timestamp(db, camera_id=camera_id, target=target)
    if not segment:
        raise HTTPException(status_code=404, detail="Recording is unavailable")
    try:
        file_path = _resolve_segment_path(segment)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=409, detail="Recording source is unavailable") from exc
        raise
    token, expires_at = create_media_token(
        user=current_user,
        scope="chronology-download",
        resource={"camera_id": camera_id, "segment_id": segment.id, "action": "download"},
    )
    response = media_token_response(token, expires_at)
    response.update(
        {
            "camera_id": camera_id,
            "segment_id": segment.id,
            "filename": _safe_download_filename(camera, segment, file_path),
        }
    )
    return response


def _chronology_media_resource(camera_id: int, rel_path: str) -> dict:
    return {"camera_id": int(camera_id), "rel_path": rel_path.replace("\\", "/").lstrip("/")}


@router.post("/media-token")
def issue_chronology_media_token(
    camera_id: int = Query(...),
    rel_path: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_timeline")),
):
    normalized_path = rel_path.replace("\\", "/").lstrip("/")
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
    request: Request = None,
):
    validate_media_token(
        db,
        token=media_token,
        scope="chronology",
        resource=_chronology_media_resource(camera_id, rel_path),
        permission=PERMISSION_VIEW_TIMELINE,
        request=request,
        media_area="chronology",
    )

    normalized_path = rel_path.replace("\\", "/").lstrip("/")
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
