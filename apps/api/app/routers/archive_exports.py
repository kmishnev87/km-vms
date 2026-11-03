from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.permissions import PERMISSION_EXPORT_RECORDINGS
from app.db.session import get_db
from app.models.recording import ArchiveExportJob
from app.models.user import User
from app.routers.deps import require_permission
from app.services.archive_exports import (
    ALLOWED_FORMAT_HINTS,
    EXPORT_STATUSES,
    MAX_ESTIMATED_SOURCE_BYTES,
    MAX_EXPORT_DURATION_SECONDS,
    MAX_SOURCE_SEGMENTS,
    cleanup_archive_export_artifacts,
    create_archive_export_job,
    create_archive_export_manifest,
    generate_archive_export_job,
    prepare_archive_export_download,
    prepare_archive_manifest_download,
    read_archive_export_manifest,
    serialize_archive_export_job,
    archive_export_generation_active,
)
from app.services.media_tokens import create_media_token, media_token_response, validate_media_token

router = APIRouter(prefix="/archive/exports", tags=["archive-exports"])


class ArchiveExportCreateRequest(BaseModel):
    camera_id: int
    start_ts: datetime
    end_ts: datetime
    title: str | None = None
    reason: str | None = None
    note: str | None = None
    format_hint: str | None = None

    class Config:
        extra = "forbid"


class ArchiveExportCleanupRequest(BaseModel):
    dry_run: bool = False
    limit: int = 100

    class Config:
        extra = "forbid"


@router.post("")
def create_export(
    payload: ArchiveExportCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    job = create_archive_export_job(
        db,
        actor=current_user,
        camera_id=payload.camera_id,
        start_ts=payload.start_ts,
        end_ts=payload.end_ts,
        title=payload.title,
        reason=payload.reason,
        note=payload.note,
        format_hint=payload.format_hint,
        request=request,
    )
    return serialize_archive_export_job(job)


@router.get("")
def list_exports(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    query = db.query(ArchiveExportJob)
    if status:
        normalized = status.strip().lower()
        if normalized not in EXPORT_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid export status")
        query = query.filter(ArchiveExportJob.status == normalized)
    jobs = query.order_by(ArchiveExportJob.created_at.desc(), ArchiveExportJob.id.asc()).offset(offset).limit(limit).all()
    return {"items": [serialize_archive_export_job(job) for job in jobs]}


@router.get("/limits")
def get_export_limits(
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    return {
        "max_duration_seconds": MAX_EXPORT_DURATION_SECONDS,
        "max_source_segments": MAX_SOURCE_SEGMENTS,
        "max_estimated_source_bytes": MAX_ESTIMATED_SOURCE_BYTES,
        "format_hints": sorted(ALLOWED_FORMAT_HINTS),
    }


@router.post("/cleanup")
def cleanup_exports(
    payload: ArchiveExportCleanupRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    return cleanup_archive_export_artifacts(
        db,
        actor=current_user,
        dry_run=payload.dry_run,
        limit=payload.limit,
        request=request,
    )


@router.get("/{export_id}")
def get_export(
    export_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    job = db.get(ArchiveExportJob, export_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    return {**serialize_archive_export_job(job), "can_resume": job.status == "running" and not archive_export_generation_active(db, job.id)}


@router.post("/{export_id}/generate")
def generate_export(
    export_id: str,
    request: Request,
    resume: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    job = generate_archive_export_job(db, export_id=export_id, actor=current_user, request=request, resume_interrupted=resume)
    return serialize_archive_export_job(job)


@router.post("/{export_id}/manifest")
def create_export_manifest(
    export_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    return create_archive_export_manifest(db, export_id=export_id, actor=current_user, request=request)


@router.get("/{export_id}/manifest")
def get_export_manifest(
    export_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    return read_archive_export_manifest(db, export_id=export_id)


@router.get("/{export_id}/download")
def download_export(
    export_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    path, filename, media_type, _size = prepare_archive_export_download(
        db,
        export_id=export_id,
        actor=current_user,
        request=request,
    )
    return FileResponse(path, media_type=media_type, filename=filename)


@router.post("/{export_id}/download-token")
def issue_export_download_token(
    export_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    job = db.get(ArchiveExportJob, export_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    metadata = serialize_archive_export_job(job)
    if not metadata["download_ready"]:
        raise HTTPException(status_code=409, detail="manifest_not_ready")
    # The file is fully verified on /file, once per download, not while issuing
    # a small capability. Tokens are per artifact and recheck current user rights.
    token, expires_at = create_media_token(
        user=current_user, scope="archive-export-download",
        resource={"export_id": export_id, "checksum": job.internal_checksum, "action": "download"},
    )
    return {**media_token_response(token, expires_at), "export_id": export_id,
            "filename": metadata["filename"], "size_bytes": metadata["output_size_bytes"]}


@router.get("/{export_id}/file")
def download_export_file(
    export_id: str, request: Request,
    media_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    job = db.get(ArchiveExportJob, export_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    actor = validate_media_token(
        db, token=media_token, scope="archive-export-download",
        resource={"export_id": export_id, "checksum": job.internal_checksum, "action": "download"},
        permission=PERMISSION_EXPORT_RECORDINGS, request=request, media_area="archive-export-download",
    )
    path, filename, media_type, _size = prepare_archive_export_download(
        db, export_id=export_id, actor=actor, request=request,
    )
    return FileResponse(path, media_type=media_type, filename=filename,
                        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


@router.get("/{export_id}/manifest/download")
def download_export_manifest(
    export_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    payload, filename = prepare_archive_manifest_download(
        db,
        export_id=export_id,
        actor=current_user,
        request=request,
    )
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
