from __future__ import annotations

import mimetypes
import os
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, asc, desc, func, or_
from sqlalchemy.orm import Session

from app.core.permissions import PERMISSION_DELETE_RECORDINGS, PERMISSION_VIEW_RECORDINGS, user_has_permission
from app.db.session import get_db
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.user import User
from app.routers.deps import require_permission
from app.services.media_tokens import create_media_token, media_token_response, validate_media_token
from app.services.recording_operations import (
    DestructiveScopeConflict,
    LeaseHeartbeat,
    ManifestValidationError,
    MANIFEST_STREAM_BATCH_SIZE,
    OperationStateConflict,
    cancel_deletion_plan,
    claim_deletion_plan,
    claim_exact_operation,
    create_deletion_plan,
    destructive_scope_guard,
    finish_operation,
    operation_fingerprint,
    open_verified_deletion_manifest,
    scope_for_segments,
    touch_operation,
)
from app.services.recording_retention import (
    EXECUTION_POLICY_MANUAL_COMPLETE,
    MANUAL_BATCH_SIZE,
    append_execution_issue,
    begin_manual_execution_result,
    build_retention_plan,
    enforce_exact_planned_accounting,
    execute_segments,
    finish_manual_execution_result,
    merge_execution_result,
    preview_segments,
    run_retention,
)
from app.services.recording_storage import (
    archive_root_for_segment,
    archive_root_runtime_access_state,
    resolve_segment_file_path,
    segment_archive_root_resolution,
    segment_has_resolved_archive_root,
    segment_relative_path as root_segment_relative_path,
)
from app.services.timezone_contract import (
    TimezoneContext,
    format_system_display,
    format_system_iso,
    local_day_storage_bounds,
    parse_api_timestamp,
    parse_local_date,
    timestamp_matches_filename,
    timezone_context,
)

router = APIRouter(prefix="/recordings", tags=["recordings"])
logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
DEFAULT_RECORDINGS_PAGE_SIZE = 30
MAX_RECORDINGS_PAGE_SIZE = 100
SUPPORTED_RECORDINGS_PAGE_SIZES = (15, 30, 50, 100)
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
}
TECHNICAL_DELETED_CAMERA_RE = re.compile(r"__deleted_\d+_\d+$")


class BulkDeleteRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)
    items: list[dict] = Field(default_factory=list)
    operation_id: str | None = None


class RecordingDeletionPlanRequest(BaseModel):
    scope: Literal["camera", "all"]
    camera: str | None = None


class RecordingDeletionExecuteRequest(BaseModel):
    confirm: bool = False


class RetentionDryRunRequest(BaseModel):
    camera_id: int | None = None


class RetentionRunRequest(BaseModel):
    confirm: bool = False
    camera_id: int | None = None
    max_candidates: int | None = None
    max_bytes: int | None = None


class RecordingMediaTokenRequest(BaseModel):
    path: str | None = None
    segment_id: int | None = None
    archive_root_id: str | None = None
    recording_ref: str | None = None
    action: str = "stream"


def segment_relative_path(segment: RecordingSegment) -> str | None:
    return segment.relative_path.replace("\\", "/").lstrip("/") if segment.relative_path else None


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
    from sqlalchemy.orm import object_session

    db = object_session(segment)
    if db is None:
        raise HTTPException(status_code=500, detail="Recording metadata session unavailable")
    try:
        root = archive_root_for_segment(db, segment)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "recording_archive_root_unresolved", "category": segment_archive_root_resolution(segment)},
        ) from exc
    access = archive_root_runtime_access_state(root)
    if access.get("read_access_state") != "available":
        raise HTTPException(status_code=409, detail={"error": "recording_archive_root_unavailable", "problem": access.get("problem")})
    try:
        file_path = resolve_segment_file_path(db, segment, require_exists=require_exists)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Recording file not found")
    except ValueError:
        raise HTTPException(status_code=404, detail="Recording metadata has no file path")
    return file_path


def segment_file_resolution(segment: RecordingSegment) -> tuple[Path | None, bool, str | None]:
    from sqlalchemy.orm import object_session

    db = object_session(segment)
    if db is None:
        return None, False, "metadata_unavailable"
    try:
        root = archive_root_for_segment(db, segment)
    except ValueError:
        return None, False, "root_unresolved"
    access = archive_root_runtime_access_state(root)
    if access.get("read_access_state") != "available":
        return None, False, "root_unavailable"
    try:
        file_path = resolve_segment_file_path(db, segment, require_exists=False)
    except ValueError:
        return None, False, "invalid_metadata"
    try:
        exists = file_path.exists() and file_path.is_file()
    except OSError:
        return file_path, False, "verification_error"
    return file_path, exists, None if exists else "missing_file"


def segment_metadata_path(segment: RecordingSegment) -> tuple[Path | None, str | None]:
    from sqlalchemy.orm import object_session

    db = object_session(segment)
    if db is None:
        return None, "metadata_unavailable"
    try:
        root = archive_root_for_segment(db, segment)
    except ValueError:
        return None, "root_unresolved"
    access = archive_root_runtime_access_state(root)
    if access.get("read_access_state") != "available":
        return None, "root_unavailable"
    try:
        return resolve_segment_file_path(db, segment, require_exists=False), None
    except ValueError:
        return None, "invalid_metadata"


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
    normalized_path = relative_path.replace("\\", "/").lstrip("/")
    matches = (
        finalized_segments_query(db)
        .filter(RecordingSegment.relative_path == normalized_path)
        .order_by(RecordingSegment.started_at.desc(), RecordingSegment.id.desc())
        .limit(2)
        .all()
    )
    if not matches:
        raise HTTPException(status_code=404, detail="Recording metadata not found")
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail={"error": "recording_path_ambiguous", "path": normalized_path})
    return matches[0]


def _segment_root_id(segment: RecordingSegment) -> str:
    if not segment_has_resolved_archive_root(segment) or not segment.archive_root_id:
        raise HTTPException(
            status_code=409,
            detail={"error": "recording_archive_root_unresolved", "category": segment_archive_root_resolution(segment)},
        )
    return str(segment.archive_root_id)


def recording_ref(segment: RecordingSegment) -> str:
    return f"segment:{int(segment.id)}:root:{_segment_root_id(segment)}"


def _segment_from_recording_ref(db: Session, ref: str) -> RecordingSegment:
    match = re.match(r"^segment:(\d+):root:([A-Za-z0-9_.-]+)$", str(ref or ""))
    if not match:
        raise HTTPException(status_code=400, detail="Invalid recording reference")
    return get_finalized_segment_by_identity(db, segment_id=int(match.group(1)), archive_root_id=match.group(2))


def get_finalized_segment_by_identity(
    db: Session,
    *,
    segment_id: int | None = None,
    archive_root_id: str | None = None,
    recording_ref_value: str | None = None,
    path: str | None = None,
) -> RecordingSegment:
    segment_id = segment_id if isinstance(segment_id, int) and not isinstance(segment_id, bool) else None
    archive_root_id = archive_root_id if isinstance(archive_root_id, str) and archive_root_id else None
    recording_ref_value = recording_ref_value if isinstance(recording_ref_value, str) and recording_ref_value else None
    path = path if isinstance(path, str) and path else None
    if recording_ref_value:
        return _segment_from_recording_ref(db, recording_ref_value)
    if segment_id is not None:
        segment = finalized_segments_query(db).filter(RecordingSegment.id == int(segment_id)).first()
        if not segment:
            raise HTTPException(status_code=404, detail="Recording metadata not found")
        segment_root_id = _segment_root_id(segment)
        if archive_root_id and segment_root_id != str(archive_root_id):
            raise HTTPException(status_code=404, detail="Recording root mismatch")
        return segment
    if path:
        return get_finalized_segment_by_path(db, path)
    raise HTTPException(status_code=400, detail="Recording identity required")


def is_technical_deleted_camera_label(value: str | None) -> bool:
    return bool(value and TECHNICAL_DELETED_CAMERA_RE.search(str(value)))


def safe_recording_camera_label(value: str | None, fallback: str = "Удалённая камера") -> str:
    text = str(value or "").strip()
    if not text or is_technical_deleted_camera_label(text):
        return fallback
    return text


def apply_camera_filter(query, db: Session, camera_name: str | None):
    if not camera_name or camera_name == "__all__":
        return query
    if is_technical_deleted_camera_label(camera_name):
        return query

    cameras = (
        db.query(Camera)
        .filter(Camera.deleted_at.is_(None), or_(Camera.name == camera_name, Camera.storage_folder_name == camera_name))
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


def _deletion_scope_query(db: Session, *, scope: str, camera: str | None = None):
    query = finalized_segments_query(db)
    if scope == "all":
        return query
    camera_label = str(camera or "").strip()
    if not camera_label or camera_label == "__all__" or is_technical_deleted_camera_label(camera_label):
        raise HTTPException(status_code=400, detail={"error": "recording_deletion_camera_required"})
    return apply_camera_filter(query, db, camera_label)


def _current_deletion_actor(db: Session, current_user: User) -> User:
    user_id = int(getattr(current_user, "id", 0) or 0)
    fresh = (
        db.query(User)
        .populate_existing()
        .filter(User.id == user_id)
        .first()
        if user_id > 0
        else None
    )
    if (
        fresh is None
        or not bool(getattr(fresh, "is_active", False))
        or not user_has_permission(getattr(fresh, "role", ""), PERMISSION_DELETE_RECORDINGS)
    ):
        raise HTTPException(status_code=403, detail="Раздел недоступен. Ограничены права пользователя.")
    return fresh


def _deletion_manifest_items(query):
    rows = (
        query.with_entities(
            RecordingSegment.id,
            RecordingSegment.archive_root_id,
            RecordingSegment.relative_path,
            RecordingSegment.size_bytes,
            RecordingSegment.camera_id,
        )
        .order_by(RecordingSegment.id.asc())
        .execution_options(stream_results=True, max_row_buffer=MANIFEST_STREAM_BATCH_SIZE)
        .yield_per(MANIFEST_STREAM_BATCH_SIZE)
    )
    for row in rows:
        yield {
            "segment_id": int(row[0]),
            "archive_root_id": str(row[1] or ""),
            "relative_path": str(row[2] or ""),
            "size_bytes": int(row[3] or 0),
            "camera_id": int(row[4] or 0),
        }


def _segment_matches_manifest_item(segment: RecordingSegment, item: dict) -> bool:
    try:
        return bool(
            int(segment.id) == int(item.get("segment_id") or 0)
            and int(segment.camera_id) == int(item.get("camera_id") or 0)
            and str(segment.archive_root_id or "") == str(item.get("archive_root_id") or "")
            and str(segment_relative_path(segment) or "") == str(item.get("relative_path") or "")
            and int(segment.size_bytes or 0) == int(item.get("size_bytes") or 0)
        )
    except (TypeError, ValueError):
        return False


def _expected_identity_for_segment(segment: RecordingSegment) -> dict:
    return {
        "segment_id": int(segment.id),
        "camera_id": int(segment.camera_id),
        "archive_root_id": _segment_root_id(segment),
        "relative_path": str(segment_relative_path(segment) or ""),
        "size_bytes": int(segment.size_bytes or 0),
    }


def _manifest_batch_segments(db: Session, items: list[dict]) -> tuple[list[RecordingSegment], dict[int, dict], bool]:
    expected = {int(item["segment_id"]): item for item in items}
    if len(expected) != len(items):
        return [], expected, False
    segments = (
        finalized_segments_query(db)
        .populate_existing()
        .filter(RecordingSegment.id.in_(list(expected)))
        .all()
    )
    by_id = {int(segment.id): segment for segment in segments}
    ordered: list[RecordingSegment] = []
    for item in items:
        segment = by_id.get(int(item["segment_id"]))
        if segment is None or not _segment_matches_manifest_item(segment, item):
            return [], expected, False
        ordered.append(segment)
    return ordered, expected, True


def _public_deletion_plan(record: dict) -> dict:
    return {
        "ok": True,
        "status": "ready",
        "plan_id": record.get("operation_id"),
        "scope": (record.get("scope") or {}).get("type"),
        "camera": record.get("camera_label"),
        "planned_count": int(record.get("planned_count") or 0),
        "planned_bytes": int(record.get("planned_bytes") or 0),
        "cutoff_segment_id": int(record.get("cutoff_segment_id") or 0),
        "expires_in_seconds": 10 * 60,
    }


def _blocked_operation_result(
    db: Session,
    actor: User,
    operation_id: str,
    reason: str,
    *,
    operation: str,
    scope: dict,
    retryable: bool,
    planned_count: int = 0,
) -> dict:
    result = begin_manual_execution_result(
        operation,
        operation_id=operation_id,
        scope=scope,
        planned_count=planned_count,
    )
    append_execution_issue(result, reason=reason, retryable=retryable)
    return finish_manual_execution_result(db, actor, result)


def recording_media_resource_for_segment(segment: RecordingSegment, action: str) -> dict:
    return {
        "segment_id": int(segment.id),
        "archive_root_id": _segment_root_id(segment),
        "action": action,
    }


def _bulk_lookup_failure(exc: HTTPException) -> tuple[str, bool]:
    if exc.status_code == 404:
        return "metadata_not_found", True
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    reason = str(detail.get("error") or "")
    if reason in {
        "recording_path_ambiguous",
        "recording_archive_root_unresolved",
    }:
        return reason, False
    if exc.status_code == 409:
        return "recording_identity_conflict", False
    return "recording_identity_invalid", False


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


def format_local_dt(dt: datetime, ctx: TimezoneContext) -> str:
    return format_system_display(dt, ctx) or dt.strftime("%d.%m.%Y, %H:%M:%S")


def segment_uses_local_naive_display(segment: RecordingSegment, filename: str | None = None) -> bool:
    if segment.source != RECORDER_SOURCE:
        return False
    candidates = [
        filename,
        Path(str(segment.relative_path or "")).name,
        Path(str(segment.file_path or "")).name,
    ]
    return any(timestamp_matches_filename(segment.started_at, candidate) for candidate in candidates)


def segment_camera_name(segment: RecordingSegment, camera: Camera | None) -> str:
    if segment.camera_name_snapshot:
        return safe_recording_camera_label(segment.camera_name_snapshot)
    if camera:
        return safe_recording_camera_label(camera.name)
    return str(segment.camera_id)


def collect_camera_names(db: Session) -> list[str]:
    names = {
        safe_recording_camera_label(camera.name)
        for camera in db.query(Camera).filter(Camera.deleted_at.is_(None)).order_by(Camera.name.asc()).all()
        if camera.name and not is_technical_deleted_camera_label(camera.name)
    }
    rows = (
        finalized_segments_query(db)
        .filter(RecordingSegment.camera_name_snapshot.isnot(None))
        .with_entities(RecordingSegment.camera_name_snapshot)
        .distinct()
        .all()
    )
    names.update(
        name for (name,) in rows
        if name and not is_technical_deleted_camera_label(name)
    )
    return sorted(names)


def apply_time_filter(query, db: Session, *, date_value: str | None = None, from_value: str | None = None, to_value: str | None = None):
    ctx = timezone_context(db)
    storage_from = storage_to = compat_from = compat_to = None
    if date_value:
        local_date = parse_local_date(date_value)
        storage_from, storage_to, compat_from, compat_to = local_day_storage_bounds(local_date, ctx)
    else:
        if from_value:
            parsed = parse_api_timestamp(from_value, ctx, field_name="from")
            storage_from = parsed.storage_utc
            compat_from = parsed.compatibility_local
        if to_value:
            parsed = parse_api_timestamp(to_value, ctx, field_name="to")
            storage_to = parsed.storage_utc
            compat_to = parsed.compatibility_local

    if storage_from is not None or storage_to is not None:
        storage_filters = []
        compat_filters = []
        if storage_from is not None:
            storage_filters.append(RecordingSegment.started_at >= storage_from)
            compat_filters.append(RecordingSegment.started_at >= (compat_from or storage_from))
        if storage_to is not None:
            storage_filters.append(RecordingSegment.started_at < storage_to)
            compat_filters.append(RecordingSegment.started_at < (compat_to or storage_to))
        query = query.filter(or_(and_(*storage_filters), and_(*compat_filters)))
    return query


def collect_recording_files(
    db: Session,
    camera_name: Optional[str] = None,
    *,
    date_value: str | None = None,
    from_value: str | None = None,
    to_value: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    verify_files: bool = False,
) -> list[dict]:
    ctx = timezone_context(db)
    query = apply_camera_filter(finalized_segments_query(db), db, camera_name)
    query = apply_time_filter(query, db, date_value=date_value, from_value=from_value, to_value=to_value)
    query = order_recordings_query(query, sort_by=sort_by, sort_dir=sort_dir)
    if limit is not None:
        query = query.offset(max(0, int(offset))).limit(max(0, int(limit)))
    segments = query.all()
    camera_ids = {segment.camera_id for segment in segments}
    cameras = (
        {camera.id: camera for camera in db.query(Camera).filter(Camera.id.in_(camera_ids)).all()}
        if camera_ids
        else {}
    )

    items: list[dict] = []
    for segment in segments:
        if verify_files:
            file_path, file_exists, unavailable_reason = segment_file_resolution(segment)
            if file_exists:
                availability_status = "available"
            elif unavailable_reason == "missing_file":
                availability_status = "missing"
            elif unavailable_reason == "root_unavailable":
                availability_status = "root_unavailable"
            elif unavailable_reason == "root_unresolved":
                availability_status = "root_unresolved"
            else:
                availability_status = "error"
        else:
            file_path, unavailable_reason = segment_metadata_path(segment)
            file_exists = None
            availability_status = "not_checked" if file_path is not None else unavailable_reason
        rel_path = segment_relative_path(segment)
        if not rel_path:
            continue
        display_file_path = file_path if file_path is not None else Path(rel_path)

        size_bytes = int(segment.size_bytes or 0)
        started_at = segment.started_at or segment.created_at
        media_metadata = segment_media_metadata(segment, display_file_path)
        local_naive_display = segment_uses_local_naive_display(segment, display_file_path.name)
        resolved_root = bool(segment_has_resolved_archive_root(segment) and segment.archive_root_id)
        items.append(
            {
                "segment_id": segment.id,
                "archive_root_id": str(segment.archive_root_id) if resolved_root else None,
                "recording_ref": recording_ref(segment) if resolved_root else None,
                "camera_id": segment.camera_id,
                "camera": segment_camera_name(segment, cameras.get(segment.camera_id)),
                "path": rel_path,
                "filename": display_file_path.name,
                "created_at": format_system_display(started_at, ctx, local_naive=local_naive_display) or format_local_dt(started_at, ctx),
                "started_at": segment.started_at.isoformat() if segment.started_at else None,
                "ended_at": segment.ended_at.isoformat() if segment.ended_at else None,
                "started_at_system": format_system_iso(segment.started_at, ctx, local_naive=local_naive_display),
                "ended_at_system": format_system_iso(segment.ended_at, ctx, local_naive=local_naive_display),
                "display_timezone": ctx.name,
                "timestamp_display_semantic": "product_local_naive" if local_naive_display else "storage_utc_naive",
                "_sort_ts": started_at.timestamp(),
                "size_bytes": size_bytes,
                "size_human": human_size(size_bytes),
                "status": segment.status,
                "ownership": segment.ownership,
                "source": segment.source,
                "container_format": media_metadata["container_format"],
                "file_extension": media_metadata["file_extension"],
                "mime_type": media_metadata["mime_type"],
                "available": file_exists,
                "file_exists": file_exists,
                "playback_available": file_exists,
                "download_available": file_exists,
                "availability_status": availability_status,
            }
        )

    for item in items:
        item.pop("_sort_ts", None)
    return items


def clamp_recordings_pagination(limit: int | None, offset: int | None) -> tuple[int, int]:
    try:
        requested = DEFAULT_RECORDINGS_PAGE_SIZE if limit is None else int(limit)
    except (TypeError, ValueError):
        requested = DEFAULT_RECORDINGS_PAGE_SIZE
    requested = max(1, min(MAX_RECORDINGS_PAGE_SIZE, requested))
    page_size = next((size for size in SUPPORTED_RECORDINGS_PAGE_SIZES if requested <= size), MAX_RECORDINGS_PAGE_SIZE)
    try:
        page_offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        page_offset = 0
    return page_size, page_offset


def order_recordings_query(query, *, sort_by: str = "created_at", sort_dir: str = "desc"):
    direction = asc if sort_dir == "asc" else desc
    if sort_by == "size_bytes":
        return query.order_by(direction(RecordingSegment.size_bytes), desc(RecordingSegment.started_at), desc(RecordingSegment.id))
    if sort_by == "camera":
        return query.order_by(direction(RecordingSegment.camera_name_snapshot), desc(RecordingSegment.started_at), desc(RecordingSegment.id))
    return query.order_by(direction(RecordingSegment.started_at), direction(RecordingSegment.id))


def recording_summary(db: Session, query) -> dict:
    count = query.count()
    size_bytes = int(query.with_entities(func.coalesce(func.sum(RecordingSegment.size_bytes), 0)).scalar() or 0)
    return {
        "count": count,
        "size_bytes": size_bytes,
        "size_human": human_size(size_bytes),
    }


def collect_recording_camera_options(db: Session, query) -> list[dict]:
    rows = query.with_entities(RecordingSegment.camera_id, RecordingSegment.camera_name_snapshot).distinct().all()
    camera_ids = {camera_id for camera_id, _name in rows if camera_id is not None}
    cameras = (
        {camera.id: camera for camera in db.query(Camera).filter(Camera.id.in_(camera_ids)).all()}
        if camera_ids
        else {}
    )
    options = []
    seen = set()
    for camera_id, snapshot in rows:
        camera = cameras.get(camera_id)
        name = safe_recording_camera_label(snapshot or (camera.name if camera else None), fallback=str(camera_id))
        key = str(camera_id)
        if not camera_id or key in seen:
            continue
        seen.add(key)
        options.append({"id": key, "name": name})
    return sorted(options, key=lambda item: item["name"].lower())


def recordings_query_for_filters(
    db: Session,
    *,
    camera_name: str | None = None,
    date_value: str | None = None,
    from_value: str | None = None,
    to_value: str | None = None,
):
    query = apply_camera_filter(finalized_segments_query(db), db, camera_name)
    return apply_time_filter(query, db, date_value=date_value, from_value=from_value, to_value=to_value)


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
    export_query = finalized_segments_query(db).join(Camera, Camera.id == RecordingSegment.camera_id).filter(Camera.deleted_at.is_(None))
    return {
        "items": collect_camera_names(db),
        "export_items": collect_recording_camera_options(db, export_query),
    }


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
    date: Optional[str] = Query(default=None),
    from_ts: Optional[str] = Query(default=None, alias="from"),
    to_ts: Optional[str] = Query(default=None, alias="to"),
    limit: int | None = Query(default=DEFAULT_RECORDINGS_PAGE_SIZE, ge=1),
    offset: int | None = Query(default=0, ge=0),
    sort_by: str = Query(default="created_at"),
    sort_dir: str = Query(default="desc"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("view_recordings")),
):
    if not isinstance(camera, str):
        camera = None
    if not isinstance(date, str):
        date = None
    if not isinstance(from_ts, str):
        from_ts = None
    if not isinstance(to_ts, str):
        to_ts = None
    page_limit, page_offset = clamp_recordings_pagination(limit, offset)
    if not isinstance(sort_by, str):
        sort_by = "created_at"
    if not isinstance(sort_dir, str):
        sort_dir = "desc"
    if sort_by not in {"created_at", "size_bytes", "camera"}:
        raise HTTPException(status_code=400, detail="Unsupported recordings sort field")
    if sort_dir not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="Unsupported recordings sort direction")
    try:
        base_query = recordings_query_for_filters(
            db,
            camera_name=camera if camera and camera != "__all__" else None,
            date_value=date,
            from_value=from_ts,
            to_value=to_ts,
        )
        items = collect_recording_files(
            db,
            camera_name=camera if camera and camera != "__all__" else None,
            date_value=date,
            from_value=from_ts,
            to_value=to_ts,
            limit=page_limit,
            offset=page_offset,
            sort_by=sort_by,
            sort_dir=sort_dir,
            verify_files=True,
        )
        summary = recording_summary(db, base_query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctx = timezone_context(db)
    return {
        "items": items,
        "total": summary["count"],
        "pagination": {
            "limit": page_limit,
            "offset": page_offset,
            "returned_count": len(items),
            "total_count": summary["count"],
            "has_more": page_offset + len(items) < summary["count"],
        },
        "summary": summary,
        "timezone": {
            "id": ctx.name,
            "source": "system_settings.timezone",
            "fallback_used": ctx.fallback_used,
            "storage_semantic": "timestamp_without_time_zone_as_utc_naive_with_local_naive_read_compatibility",
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
    segment = get_finalized_segment_by_identity(
        db,
        segment_id=payload.segment_id,
        archive_root_id=payload.archive_root_id,
        recording_ref_value=payload.recording_ref,
        path=payload.path,
    )
    resolve_segment_file(segment)
    token, expires_at = create_media_token(
        user=current_user,
        scope="recording",
        resource=recording_media_resource_for_segment(segment, action),
    )
    return media_token_response(token, expires_at)


@router.get("/download")
def download_recording(
    request: Request,
    path: str | None = Query(default=None),
    segment_id: int | None = Query(default=None),
    archive_root_id: str | None = Query(default=None),
    recording_ref: str | None = Query(default=None),
    media_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    segment = get_finalized_segment_by_identity(
        db,
        segment_id=segment_id,
        archive_root_id=archive_root_id,
        recording_ref_value=recording_ref,
        path=path,
    )
    validate_media_token(
        db,
        token=media_token,
        scope="recording",
        resource=recording_media_resource_for_segment(segment, "download"),
        permission=PERMISSION_VIEW_RECORDINGS,
        request=request,
        media_area="recordings",
    )
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
    path: str | None = Query(default=None),
    segment_id: int | None = Query(default=None),
    archive_root_id: str | None = Query(default=None),
    recording_ref: str | None = Query(default=None),
    media_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    segment = get_finalized_segment_by_identity(
        db,
        segment_id=segment_id,
        archive_root_id=archive_root_id,
        recording_ref_value=recording_ref,
        path=path,
    )
    validate_media_token(
        db,
        token=media_token,
        scope="recording",
        resource=recording_media_resource_for_segment(segment, "stream"),
        permission=PERMISSION_VIEW_RECORDINGS,
        request=request,
        media_area="recordings",
    )
    file_path = resolve_segment_file(segment)
    media_metadata = segment_media_metadata(segment, file_path)
    return stream_video(request, file_path, media_metadata["mime_type"])


@router.post("/deletion-plans")
def create_recording_deletion_plan(
    payload: RecordingDeletionPlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    current_user = _current_deletion_actor(db, current_user)
    query = _deletion_scope_query(db, scope=payload.scope, camera=payload.camera)
    try:
        record = create_deletion_plan(
            actor=current_user,
            scope_type=payload.scope,
            planned_items=_deletion_manifest_items(query),
            camera_label=payload.camera if payload.scope == "camera" else None,
        )
    except ManifestValidationError as exc:
        raise HTTPException(status_code=409, detail={"error": exc.reason}) from exc
    return _public_deletion_plan(record)


def _execute_recording_deletion_plan(
    plan_id: str,
    *,
    confirm: bool,
    db: Session,
    current_user: User,
) -> dict:
    if not confirm:
        raise HTTPException(status_code=409, detail={"error": "recording_deletion_confirmation_required"})
    current_user = _current_deletion_actor(db, current_user)
    try:
        claimed = claim_deletion_plan(plan_id, actor=current_user)
    except (OperationStateConflict, ValueError) as exc:
        detail = getattr(exc, "detail", {"reason": str(exc) or "deletion_plan_invalid"})
        raise HTTPException(status_code=409, detail={"error": detail.get("reason"), **detail}) from exc
    if claimed["state"] == "terminal":
        return claimed.get("result") or {}
    record = claimed["record"]
    scope = record.get("scope") or {}
    operation = "manual_delete_all" if str(scope.get("type") or "") == "all" else "manual_delete_by_camera"
    if claimed["state"] == "running":
        blocked = _blocked_operation_result(
            db,
            current_user,
            plan_id,
            "destructive_operation_already_running",
            operation=operation,
            scope=scope,
            retryable=True,
            planned_count=int(record.get("planned_count") or 0),
        )
        raise HTTPException(
            status_code=409,
            detail=blocked,
        )
    if claimed["state"] == "expired":
        expired = _blocked_operation_result(
            db,
            current_user,
            plan_id,
            "deletion_plan_expired",
            operation=operation,
            scope=scope,
            retryable=False,
            planned_count=int(record.get("planned_count") or 0),
        )
        finish_operation(plan_id, claimed["owner_token"], expired)
        return expired

    owner_token = claimed["owner_token"]
    scope_type = str(scope.get("type") or "")
    planned_count = int(record.get("planned_count") or 0)
    aggregate = begin_manual_execution_result(
        "manual_delete_all" if scope_type == "all" else "manual_delete_by_camera",
        operation_id=plan_id,
        scope=scope,
        planned_count=planned_count,
    )

    scope_validated = False
    try:
        with destructive_scope_guard(plan_id, scope, purpose=aggregate["operation"]) as scope_lease:
            with LeaseHeartbeat(
                scope_lease=scope_lease,
                operation_id=plan_id,
                owner_token=owner_token,
            ) as heartbeat:
                with open_verified_deletion_manifest(record, progress=heartbeat.progress) as manifest:
                    scope_validated = True
                    for items in manifest.iter_batches(
                        batch_size=MANUAL_BATCH_SIZE,
                        progress=heartbeat.progress,
                    ):
                        heartbeat.progress()
                        _segments, _expected, valid = _manifest_batch_segments(db, items)
                        if not valid:
                            scope_validated = False
                            append_execution_issue(
                                aggregate,
                                reason="deletion_plan_item_changed",
                                retryable=False,
                            )
                            break

                    if scope_validated:
                        for items in manifest.iter_batches(
                            batch_size=MANUAL_BATCH_SIZE,
                            progress=heartbeat.progress,
                        ):
                            heartbeat.progress()
                            batch, expected, valid = _manifest_batch_segments(db, items)
                            if not valid:
                                append_execution_issue(
                                    aggregate,
                                    reason="deletion_plan_item_changed",
                                    retryable=False,
                                )
                                break
                            batch_result = execute_segments(
                                db,
                                batch,
                                actor=current_user,
                                operation=aggregate["operation"],
                                reason=aggregate["operation"],
                                policy=EXECUTION_POLICY_MANUAL_COMPLETE,
                                operation_id=plan_id,
                                scope=scope,
                                scope_lease=scope_lease,
                                write_terminal_audit=False,
                                write_item_audit=False,
                                operation_heartbeat=heartbeat.progress,
                                operation_owner_token=owner_token,
                                expected_identities=expected,
                            )
                            merge_execution_result(aggregate, batch_result)
    except ManifestValidationError as exc:
        append_execution_issue(
            aggregate,
            reason=exc.reason,
            retryable=False,
        )
    except DestructiveScopeConflict as exc:
        append_execution_issue(
            aggregate,
            reason=str(exc.detail.get("reason") or "destructive_scope_conflict"),
            retryable=bool(exc.detail.get("retryable", True)),
        )
    except OperationStateConflict as exc:
        append_execution_issue(
            aggregate,
            reason=str(exc.detail.get("reason") or "operation_lease_lost"),
            action="failed",
            retryable=bool(exc.detail.get("retryable", True)),
        )
    except Exception:
        logger.exception("Recording deletion plan execution failed")
        db.rollback()
        append_execution_issue(aggregate, reason="recording_deletion_internal_failure", action="failed", retryable=True)

    if scope_validated:
        enforce_exact_planned_accounting(aggregate, planned_count)
    finished = finish_manual_execution_result(db, current_user, aggregate)
    try:
        finish_operation(plan_id, owner_token, finished)
    except OperationStateConflict:
        pass
    return finished


@router.post("/deletion-plans/{plan_id}/execute")
def execute_recording_deletion_plan(
    plan_id: str,
    payload: RecordingDeletionExecuteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    return _execute_recording_deletion_plan(
        plan_id,
        confirm=payload.confirm,
        db=db,
        current_user=current_user,
    )


@router.delete("/deletion-plans/{plan_id}")
def cancel_recording_deletion_plan(
    plan_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    current_user = _current_deletion_actor(db, current_user)
    try:
        return cancel_deletion_plan(plan_id, actor=current_user)
    except (OperationStateConflict, ValueError) as exc:
        detail = getattr(exc, "detail", {"reason": str(exc) or "deletion_plan_invalid"})
        raise HTTPException(status_code=409, detail={"error": detail.get("reason"), **detail}) from exc


def _claim_exact_or_return(
    operation_id: str | None,
    *,
    actor: User,
    operation_type: str,
    request_payload: dict,
) -> dict:
    try:
        return claim_exact_operation(
            operation_id,
            actor=actor,
            operation_type=operation_type,
            request_fingerprint=operation_fingerprint(request_payload),
        )
    except (OperationStateConflict, ValueError) as exc:
        detail = getattr(exc, "detail", {"reason": str(exc) or "operation_identity_invalid"})
        raise HTTPException(status_code=409, detail={"error": detail.get("reason"), **detail}) from exc


def _resolve_bulk_delete_segments(
    db: Session,
    payload: BulkDeleteRequest,
    *,
    heartbeat,
) -> tuple[list[RecordingSegment], list[dict]]:
    segments: list[RecordingSegment] = []
    lookup_failures: list[dict] = []
    requested = [
        ("identity", item)
        for item in payload.items
    ] + [
        ("path", rel)
        for rel in payload.paths
    ]
    for index, (kind, value) in enumerate(requested):
        if index % MANUAL_BATCH_SIZE == 0:
            heartbeat()
        try:
            if kind == "identity":
                item = value
                segments.append(
                    get_finalized_segment_by_identity(
                        db,
                        segment_id=item.get("segment_id"),
                        archive_root_id=item.get("archive_root_id"),
                        recording_ref_value=item.get("recording_ref"),
                        path=item.get("path"),
                    )
                )
            else:
                segments.append(get_finalized_segment_by_path(db, value))
        except HTTPException as exc:
            reason, _not_found = _bulk_lookup_failure(exc)
            lookup_failures.append(
                {"segment_id": None, "camera_id": None, "action": "skipped", "reason": reason, "error": None, "size_bytes": 0}
            )
    return list({int(segment.id): segment for segment in segments}.values()), lookup_failures


@router.delete("")
def delete_recording(
    path: str | None = Query(default=None),
    segment_id: int | None = Query(default=None),
    archive_root_id: str | None = Query(default=None),
    recording_ref: str | None = Query(default=None),
    operation_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    current_user = _current_deletion_actor(db, current_user)
    path = path if isinstance(path, str) else None
    segment_id = segment_id if isinstance(segment_id, int) and not isinstance(segment_id, bool) else None
    archive_root_id = archive_root_id if isinstance(archive_root_id, str) else None
    recording_ref = recording_ref if isinstance(recording_ref, str) else None
    operation_id = operation_id if isinstance(operation_id, str) else None
    request_identity = {
        "path": path,
        "segment_id": segment_id,
        "archive_root_id": archive_root_id,
        "recording_ref": recording_ref,
    }
    claimed = _claim_exact_or_return(
        operation_id,
        actor=current_user,
        operation_type="manual_single_delete",
        request_payload=request_identity,
    )
    if claimed["state"] == "terminal":
        terminal = claimed.get("result") or {}
        if terminal.get("ok") is not True:
            raise HTTPException(status_code=409, detail=terminal)
        return terminal
    if claimed["state"] == "running":
        blocked = _blocked_operation_result(
            db,
            current_user,
            str(claimed["record"].get("operation_id") or operation_id or "recording-operation"),
            "destructive_operation_already_running",
            operation="manual_single_delete",
            scope={"type": "segments", "segment_ids": [], "camera_ids": [], "root_ids": []},
            retryable=True,
        )
        raise HTTPException(status_code=409, detail=blocked)
    operation_id = claimed["record"]["operation_id"]
    owner_token = claimed["owner_token"]
    try:
        segment = get_finalized_segment_by_identity(
            db,
            segment_id=segment_id,
            archive_root_id=archive_root_id,
            recording_ref_value=recording_ref,
            path=path,
        )
        result = execute_segments(
            db,
            [segment],
            actor=current_user,
            operation="manual_single_delete",
            reason="manual_delete",
            policy=EXECUTION_POLICY_MANUAL_COMPLETE,
            operation_id=operation_id,
            operation_owner_token=owner_token,
            expected_identities={int(segment.id): _expected_identity_for_segment(segment)},
        )
    except HTTPException as exc:
        reason, _not_found = _bulk_lookup_failure(exc)
        result = begin_manual_execution_result(
            "manual_single_delete",
            operation_id=operation_id,
            scope={"type": "segments", "segment_ids": [], "camera_ids": [], "root_ids": []},
        )
        append_execution_issue(result, reason=reason, retryable=False)
        result = finish_manual_execution_result(db, current_user, result)
    except (OperationStateConflict, DestructiveScopeConflict):
        db.rollback()
        result = begin_manual_execution_result(
            "manual_single_delete",
            operation_id=operation_id,
            scope={"type": "segments", "segment_ids": [], "camera_ids": [], "root_ids": []},
        )
        append_execution_issue(result, reason="operation_lease_lost", action="failed", retryable=True)
        result = finish_manual_execution_result(db, current_user, result)
    except Exception:
        db.rollback()
        result = begin_manual_execution_result(
            "manual_single_delete",
            operation_id=operation_id,
            scope={"type": "segments", "segment_ids": [], "camera_ids": [], "root_ids": []},
        )
        append_execution_issue(result, reason="recording_deletion_internal_failure", action="failed", retryable=True)
        result = finish_manual_execution_result(db, current_user, result)
    finish_operation(operation_id, owner_token, result)
    if result.get("ok") is not True:
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/bulk-delete")
def bulk_delete_recordings(
    payload: BulkDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    current_user = _current_deletion_actor(db, current_user)
    stable_items = sorted(
        [
            {
                "segment_id": item.get("segment_id"),
                "archive_root_id": item.get("archive_root_id"),
                "recording_ref": item.get("recording_ref"),
                "path": item.get("path"),
            }
            for item in payload.items
        ],
        key=lambda item: str(sorted(item.items())),
    )
    claimed = _claim_exact_or_return(
        payload.operation_id,
        actor=current_user,
        operation_type="manual_bulk_delete",
        request_payload={"items": stable_items, "paths": sorted(payload.paths)},
    )
    if claimed["state"] == "terminal":
        return claimed.get("result") or {}
    if claimed["state"] == "running":
        blocked = _blocked_operation_result(
            db,
            current_user,
            str(claimed["record"].get("operation_id") or payload.operation_id or "recording-operation"),
            "destructive_operation_already_running",
            operation="manual_bulk_delete",
            scope={"type": "segments", "segment_ids": [], "camera_ids": [], "root_ids": []},
            retryable=True,
        )
        raise HTTPException(status_code=409, detail=blocked)
    operation_id = claimed["record"]["operation_id"]
    owner_token = claimed["owner_token"]
    unique_segments: list[RecordingSegment] = []
    try:
        unique_segments, lookup_failures = _resolve_bulk_delete_segments(
            db,
            payload,
            heartbeat=lambda: touch_operation(operation_id, owner_token),
        )
        result = execute_segments(
            db,
            unique_segments,
            actor=current_user,
            operation="manual_bulk_delete",
            reason="manual_bulk_delete",
            policy=EXECUTION_POLICY_MANUAL_COMPLETE,
            operation_id=operation_id,
            initial_items=lookup_failures,
            operation_owner_token=owner_token,
            expected_identities={
                int(segment.id): _expected_identity_for_segment(segment)
                for segment in unique_segments
            },
        )
    except (OperationStateConflict, DestructiveScopeConflict):
        db.rollback()
        result = begin_manual_execution_result(
            "manual_bulk_delete",
            operation_id=operation_id,
            scope=scope_for_segments(unique_segments),
        )
        append_execution_issue(result, reason="operation_lease_lost", action="failed", retryable=True)
        result = finish_manual_execution_result(db, current_user, result)
    except Exception:
        db.rollback()
        result = begin_manual_execution_result(
            "manual_bulk_delete",
            operation_id=operation_id,
            scope=scope_for_segments(unique_segments),
        )
        append_execution_issue(result, reason="recording_deletion_internal_failure", action="failed", retryable=True)
        result = finish_manual_execution_result(db, current_user, result)
    finish_operation(operation_id, owner_token, result)
    return result


@router.delete("/by-camera")
def delete_recordings_by_camera(
    camera: str = Query(...),
    plan_id: str | None = Query(default=None),
    confirm: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    if not plan_id:
        raise HTTPException(status_code=409, detail={"error": "recording_deletion_plan_required"})
    return _execute_recording_deletion_plan(plan_id, confirm=confirm, db=db, current_user=current_user)


@router.delete("/all")
def delete_all_recordings(
    dry_run: bool = Query(False),
    confirm: bool = Query(False),
    confirmation_text: str | None = Query(default=None),
    plan_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("delete_recordings")),
):
    if dry_run:
        return create_recording_deletion_plan(
            RecordingDeletionPlanRequest(scope="all"),
            db=db,
            current_user=current_user,
        )
    if not plan_id:
        raise HTTPException(status_code=409, detail={"error": "recording_deletion_plan_required"})
    if not confirm or confirmation_text != DELETE_ALL_CONFIRMATION_TEXT:
        raise HTTPException(status_code=409, detail={"error": "recording_deletion_confirmation_required"})
    return _execute_recording_deletion_plan(plan_id, confirm=True, db=db, current_user=current_user)
