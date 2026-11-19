"""Shared owner/admin NAS configuration; no public signal-injection endpoint."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.camera import Camera
from app.models.user import User
from app.routers.deps import require_permission
from app.schemas.ai_recording import EmergencyUpdate, PolicyUpdate
from app.services import ai_recording as domain
from app.services.ai_rule_frame import (
    frame, matching_references, read_editor_image, reference_is_fresh, saved_reference, store_editor_image,
)
from app.services.camera_events_worker import retry_camera_events

router = APIRouter(prefix="/ai-recording", tags=["ai-recording"])
private_frame_headers = {"Cache-Control": "private, no-store"}


def frame_camera(camera):
    if camera is None or camera.deleted_at is not None or camera.status == "deleting":
        raise HTTPException(404, detail={"code": "camera_not_found"})
    return camera


@router.get("/cameras")
def cameras(db: Session = Depends(get_db), actor: User = Depends(require_permission("manage_cameras"))):
    rows = domain.camera_rows(db, db.query(Camera).filter(Camera.deleted_at.is_(None), Camera.status != "deleting").order_by(Camera.id).all())
    return {"schema_version": 1, "runtime_active": any(row["runtime"]["policy_applied"] for row in rows), "cameras": rows}


@router.get("/cameras/{camera_id}")
def camera(camera_id: int, db: Session = Depends(get_db), actor: User = Depends(require_permission("manage_cameras"))):
    row = db.get(Camera, camera_id)
    if row is None or row.deleted_at is not None or row.status == "deleting":
        raise HTTPException(404, detail={"code": "camera_not_found"})
    return domain.camera_rows(db, [row])[0]


@router.put("/cameras/{camera_id}")
def save_camera(camera_id: int, payload: PolicyUpdate, db: Session = Depends(get_db),
                actor: User = Depends(require_permission("manage_cameras"))):
    return domain.save_policy(db, camera_id, payload, actor)


@router.get("/cameras/{camera_id}/rule-frame")
def rule_frame(camera_id: int, profile: Literal["none", "balanced", "full_quality"] = "none",
               db: Session = Depends(get_db), actor: User = Depends(require_permission("manage_cameras"))):
    camera = frame_camera(db.get(Camera, camera_id))
    db.expunge(camera)
    db.rollback()  # No database transaction waits on camera I/O.
    return JSONResponse(frame(camera, profile), headers=private_frame_headers)


@router.post("/cameras/{camera_id}/rule-frame")
def capture_editor_frame(camera_id: int, profile: Literal["none", "balanced", "full_quality"] = "none",
                         db: Session = Depends(get_db), actor: User = Depends(require_permission("manage_cameras"))):
    camera = frame_camera(db.get(Camera, camera_id))
    db.expunge(camera)
    db.rollback()
    snapshot = frame(camera, profile, editor=True)
    locked = frame_camera(db.query(Camera).filter(Camera.id == camera_id).with_for_update().one_or_none())
    if not reference_is_fresh(locked, snapshot["reference"]):
        raise HTTPException(409, detail={"code": "ai_frame_reference_changed"})
    previous = saved_reference(locked)
    snapshot["matching_saved"] = not previous or matching_references(previous, snapshot["reference"])
    try:
        store_editor_image(locked, snapshot)
    except (OSError, ValueError):
        raise HTTPException(409, detail={"code": "ai_frame_storage_unavailable"}) from None
    return JSONResponse(snapshot, headers=private_frame_headers)


@router.get("/cameras/{camera_id}/rule-frame/saved")
def saved_editor_frame(camera_id: int, db: Session = Depends(get_db),
                       actor: User = Depends(require_permission("manage_cameras"))):
    camera = frame_camera(db.get(Camera, camera_id))
    snapshot = read_editor_image(camera, saved_reference(camera))
    if snapshot is None:
        raise HTTPException(404, detail={"code": "ai_frame_not_saved"}, headers=private_frame_headers)
    return JSONResponse({**snapshot, "matching_saved": True}, headers=private_frame_headers)


@router.post("/cameras/{camera_id}/retry")
def retry_source(camera_id: int, source: Literal["ai", "camera"] | None = None, db: Session = Depends(get_db),
                 actor: User = Depends(require_permission("manage_cameras"))):
    camera = db.get(Camera, camera_id)
    if camera is None or camera.deleted_at is not None or camera.status == "deleting":
        raise HTTPException(404, detail={"code": "camera_not_found"})
    if source == "ai" or source is None and domain.ai_policy_supported(camera):
        status = domain.queue_ai_retry(db, camera_id)
    elif camera.protocol != "onvif" or not camera.events_enabled:
        raise HTTPException(409, detail={"code": "camera_event_reception_required"})
    else:
        status = retry_camera_events(camera)
    if status != "queued":
        raise HTTPException(409, detail={"code": status})
    return {"status": status}


@router.get("/emergency")
def emergency(db: Session = Depends(get_db), actor: User = Depends(require_permission("manage_settings"))):
    return domain.emergency_response(db)


@router.put("/emergency")
def save_emergency(payload: EmergencyUpdate, db: Session = Depends(get_db),
                   actor: User = Depends(require_permission("manage_settings"))):
    return domain.save_emergency(db, payload, actor)
