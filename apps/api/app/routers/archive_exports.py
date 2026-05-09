from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.permissions import PERMISSION_EXPORT_RECORDINGS
from app.db.session import get_db
from app.models.recording import ArchiveExportJob
from app.models.user import User
from app.routers.deps import require_permission
from app.services.archive_exports import (
    EXPORT_STATUSES,
    create_archive_export_job,
    serialize_archive_export_job,
)

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


@router.get("/{export_id}")
def get_export(
    export_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission(PERMISSION_EXPORT_RECORDINGS)),
):
    job = db.get(ArchiveExportJob, export_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    return serialize_archive_export_job(job)
