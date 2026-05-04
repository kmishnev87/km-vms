from __future__ import annotations

import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import PERMISSION_VIEW_RECORDINGS
from app.db.session import get_db
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.user import User
from app.routers.deps import require_permission
from app.services.media_tokens import create_media_token, media_token_response, validate_media_token
from app.services.recording_retention import build_retention_plan, execute_segments, preview_segments, run_retention

router = APIRouter(prefix="/recordings", tags=["recordings"])

CHUNK_SIZE = 1024 * 1024
OWNERSHIP_KM_VMS = "KM VMS"
RECORDER_SOURCE = "recorder"
SEGMENT_STATUS_FINALIZED = "finalized"
DELETE_ALL_CONFIRMATION_TEXT = "DELETE_ALL_RECORDINGS"
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


class BulkDeleteRequest(BaseModel):
    paths: list[str]


class RetentionDryRunRequest(BaseModel):
    camera_id: int | None = None


class RetentionRunRequest(BaseModel):
    confirm: bool = False
    camera_id: int | None = None
    max_candidates: int | None = None
    max_bytes: int | None = None


class RecordingMediaTokenRequest(BaseModel):
    path: str
    action: str = "stream"


def storage_root() -> Path:
    root = Path(settings.storage_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_resolve_relative(relative_path: str) -> Path:
    if not relative_path:
        raise HTTPException(status_code=400, detail="Empty path")

    root = storage_root().resolve()
    target = (root / relative_path).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    return target


def relative_to_storage(path: Path) -> str:
    root = storage_root().resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid recording path")


def segment_relative_path(segment: RecordingSegment) -> str | None:
    if segment.relative_path:
        return relative_to_storage(safe_resolve_relative(segment.relative_path))

    if segment.file_path:
        file_path = Path(segment.file_path)
        if not file_path.is_absolute():
            file_path = storage_root() / file_path
        return relative_to_storage(file_path)

    return None


def segment_media_metadata(segment: RecordingSegment, file_path: Path) -> dict[str, str | None]:
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


def resolve_segment_file(segment: RecordingSegment, require_exists: bool = True) -> Path:
    rel_path = segment_relative_path(segment)
    if not rel_path:
        raise HTTPException(status_code=404, detail="Recording metadata has no file path")

    file_path = safe_resolve_relative(rel_path)
    if require_exists and (not file_path.exists() or not file_path.is_file()):
        raise HTTPException(status_code=404, detail="Recording file not found")
    return file_path


def finalized_segments_query(db: Session):
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


def get_finalized_segment_by_path(db: Session, relative_path: str) -> RecordingSegment:
    normalized_path = relative_to_storage(safe_resolve_relative(relative_path))
    segment = (
        finalized_segments_query(db)
        .filter(RecordingSegment.relative_path == normalized_path)
        .order_by(RecordingSegment.started_at.desc(), RecordingSegment.id.desc())
        .first()
    )
    if not segment:
        raise HTTPException(status_code=404, detail="Recording metadata not found")
    return segment


def apply_camera_filter(query, db: Session, camera_name: str | None):
    if not camera_name or camera_name == "__all__":
        return query

    cameras = (
        db.query(Camera)
        .filter(or_(Camera.name == camera_name, Camera.storage_folder_name == camera_name))
        .all()
    )
    camera_ids = [camera.id for camera in cameras]
    filters = [
        RecordingSegment.camera_name_snapshot == camera_name,
        RecordingSegment.camera_folder_snapshot == camera_name,
    ]
    if camera_ids:
        filters.append(RecordingSegment.camera_id.in_(camera_ids))
    return query.filter(or_(*filters))


def recording_media_resource(path: str, action: str) -> dict:
    return {"path": relative_to_storage(safe_resolve_relative(path)), "action": action}


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


def segment_camera_name(segment: RecordingSegment, camera: Camera | None) -> str:
    if segment.camera_name_snapshot:
        return segment.camera_name_snapshot
    if camera:
        return camera.name
    return str(segment.camera_id)


def collect_camera_names(db: Session) -> list[str]:
    names = {camera.name for camera in db.query(Camera).order_by(Camera.name.asc()).all()}
    rows = (
        finalized_segments_query(db)
        .filter(RecordingSegment.camera_name_snapshot.isnot(None))
        .with_entities(RecordingSegment.camera_name_snapshot)
        .distinct()
        .all()
    )
    names.update(name for (name,) in rows if name)
    return sorted(names)


def collect_recording_files(db: Session, camera_name: Optional[str] = None) -> list[dict]:
    query = apply_camera_filter(finalized_segments_query(db), db, camera_name)
    segments = query.order_by(RecordingSegment.started_at.desc(), RecordingSegment.id.desc()).all()
    camera_ids = {segment.camera_id for segment in segments}
    cameras = (
        {camera.id: camera for camera in db.query(Camera).filter(Camera.id.in_(camera_ids)).all()}
        if camera_ids
        else {}
    )

    items: list[dict] = []
    for segment in segments:
        try:
            file_path = resolve_segment_file(segment)
            rel_path = segment_relative_path(segment)
        except HTTPException:
            continue
        if not rel_path:
            continue

        size_bytes = int(segment.size_bytes or file_path.stat().st_size)
        started_at = segment.started_at or segment.created_at
        media_metadata = segment_media_metadata(segment, file_path)
        items.append(
            {
                "camera": segment_camera_name(segment, cameras.get(segment.camera_id)),
                "path": rel_path,
                "filename": file_path.name,
                "created_at": format_local_dt(started_at),
                "_sort_ts": started_at.timestamp(),
                "size_bytes": size_bytes,
                "size_human": human_size(size_bytes),
                "status": segment.status,
                "ownership": segment.ownership,
                "source": segment.source,
                "container_format": media_metadata["container_format"],
                "file_extension": media_metadata["file_extension"],
                "mime_type": media_metadata["mime_type"],
            }
        )

    items.sort(key=lambda x: x["_sort_ts"], reverse=True)
    for item in items:
        item.pop("_sort_ts", None)
    return items


def stream_video(request: Request, file_path: Path, media_type_override: str | None = None):
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
            raise HTTPException(status_code=400, detail="Invalid Range header")

    if start >= file_size or end >= file_size or start > end:
        raise HTTPException(status_code=416, detail="Range Not Satisfiable")

    media_type = media_type_override
    if not media_type:
        media_type, _ = mimetypes.guess_type(str(file_path))
    media_type = media_type or "application/octet-stream"

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
    current_user: User = Depends(require_permission("view_recordings")),
):
    return {"items": collect_camera_names(db)}


@router.post("/retention/dry-run")
def retention_dry_run(
    payload: RetentionDryRunRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    camera_id = payload.camera_id if payload else None
    return build_retention_plan(db, camera_id=camera_id, actor=current_user, write_audit=True)


@router.get("/retention/plan")
def retention_plan(
    camera_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    return build_retention_plan(db, camera_id=camera_id)


@router.post("/retention/run")
def retention_run(
    payload: RetentionRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    if not payload.confirm:
        raise HTTPException(status_code=409, detail="Retention run requires confirm=true")
    return run_retention(
        db,
        actor=current_user,
        camera_id=payload.camera_id,
        max_candidates=payload.max_candidates,
        max_bytes=payload.max_bytes,
    )


@router.get("")
def list_recordings(
    camera: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_recordings")),
):
    items = collect_recording_files(db, camera_name=camera if camera and camera != "__all__" else None)
    total = sum(item["size_bytes"] for item in items)
    return {
        "items": items,
        "summary": {
            "count": len(items),
            "size_bytes": total,
            "size_human": human_size(total),
        },
    }


@router.post("/media-token")
def issue_recording_media_token(
    payload: RecordingMediaTokenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_recordings")),
):
    action = str(payload.action or "stream").strip().lower()
    if action not in {"stream", "download"}:
        raise HTTPException(status_code=422, detail="Unsupported recording media action")
    segment = get_finalized_segment_by_path(db, payload.path)
    resolve_segment_file(segment)
    token, expires_at = create_media_token(
        user=current_user,
        scope="recording",
        resource=recording_media_resource(payload.path, action),
    )
    return media_token_response(token, expires_at)


@router.get("/download")
def download_recording(
    request: Request,
    path: str = Query(...),
    media_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    validate_media_token(
        db,
        token=media_token,
        scope="recording",
        resource=recording_media_resource(path, "download"),
        permission=PERMISSION_VIEW_RECORDINGS,
        request=request,
        media_area="recordings",
    )
    segment = get_finalized_segment_by_path(db, path)
    file_path = resolve_segment_file(segment)
    media_metadata = segment_media_metadata(segment, file_path)

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_metadata["mime_type"] or "application/octet-stream",
    )


@router.get("/stream")
def stream_recording(
    request: Request,
    path: str = Query(...),
    media_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    validate_media_token(
        db,
        token=media_token,
        scope="recording",
        resource=recording_media_resource(path, "stream"),
        permission=PERMISSION_VIEW_RECORDINGS,
        request=request,
        media_area="recordings",
    )
    segment = get_finalized_segment_by_path(db, path)
    file_path = resolve_segment_file(segment)
    media_metadata = segment_media_metadata(segment, file_path)
    return stream_video(request, file_path, media_metadata["mime_type"])


@router.delete("")
def delete_recording(
    path: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    segment = get_finalized_segment_by_path(db, path)
    result = execute_segments(
        db,
        [segment],
        actor=current_user,
        operation="manual_single_delete",
        reason="manual_delete",
        max_candidates=1,
    )
    if result["failed_count"]:
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/bulk-delete")
def bulk_delete_recordings(
    payload: BulkDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    segments: list[RecordingSegment] = []
    result_not_found = 0
    for rel in payload.paths:
        try:
            segments.append(get_finalized_segment_by_path(db, rel))
        except HTTPException:
            result_not_found += 1

    result = execute_segments(
        db,
        segments,
        actor=current_user,
        operation="manual_bulk_delete",
        reason="manual_bulk_delete",
        max_candidates=max(len(payload.paths), 1),
    )
    result["requested_count"] = len(payload.paths)
    result["not_found_count"] += result_not_found
    result["skipped_count"] += result_not_found
    for _ in range(result_not_found):
        result["items"].append({"segment_id": None, "camera_id": None, "relative_path": None, "action": "skipped", "reason": "metadata_not_found", "error": None, "size_bytes": 0})

    return result


@router.delete("/by-camera")
def delete_recordings_by_camera(
    camera: str = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    if not camera:
        raise HTTPException(status_code=400, detail="Camera is required")

    segments = apply_camera_filter(finalized_segments_query(db), db, camera).all()
    return execute_segments(
        db,
        segments,
        actor=current_user,
        operation="manual_delete_by_camera",
        reason="manual_delete_by_camera",
        max_candidates=max(len(segments), 1),
    )


@router.delete("/all")
def delete_all_recordings(
    dry_run: bool = Query(False),
    confirm: bool = Query(False),
    confirmation_text: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    segments = finalized_segments_query(db).all()
    if dry_run:
        return preview_segments(
            db,
            segments,
            operation="manual_delete_all_preview",
            reason="manual_delete_all",
        )
    if not confirm or confirmation_text != DELETE_ALL_CONFIRMATION_TEXT:
        raise HTTPException(status_code=409, detail=f"Delete all recordings requires confirm=true and confirmation_text={DELETE_ALL_CONFIRMATION_TEXT}")
    return execute_segments(
        db,
        segments,
        actor=current_user,
        operation="manual_delete_all",
        reason="manual_delete_all",
        max_candidates=max(len(segments), 1),
    )
