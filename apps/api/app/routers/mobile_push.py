from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.routers.deps import require_permission
from app.services import mobile_push as push
from app.services import mobile_push_store as store
from app.services.mobile_push_transport import PushDeliveryError, PushEndpointUnavailable, vapid_public_key
from app.services import camera_events
from app.services.timezone_contract import timezone_context

router = APIRouter(prefix="/mobile/push", tags=["mobile"])


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1, le=2147483647)
    label: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")
    enabled: StrictBool
    subscription: dict | None = None
    badge_enabled: StrictBool = False


@router.get("/config")
def configuration(badge: bool = False, db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    value = {"protocol": 1, "vapid_public_key": vapid_public_key(), "registration_days": push.REGISTRATION_DAYS, "widget_badge_v1": True}
    if badge:
        from app.services.mobile_badges import snapshot
        value["unread_badge"] = snapshot(db, user.id)
        db.commit()
    return value


@router.get("/devices/{device_id}")
def device_status(device_id: UUID, db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    row = store.by_user(db, user.id, str(device_id))
    if row is None:
        raise HTTPException(404, detail={"code": "mobile_push_device_not_found"})
    return push.status(row)


@router.post("/devices/{device_id}/test")
def test_delivery(device_id: UUID, db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    try:
        return push.send_test(db, user.id, str(device_id))
    except ValueError as exc:
        db.rollback()
        code = str(exc)
        raise HTTPException(429 if code == "mobile_push_test_rate_limit" else 404, detail={"code": code}) from None
    except PushDeliveryError:
        raise HTTPException(502, detail={"code": "mobile_push_test_unavailable"}) from None


@router.get("/events/{event_id}")
def notification_event(event_id: UUID, db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    row = camera_events.event_query(db).filter(camera_events.AuditEvent.id == str(event_id)).first()
    if row is None:
        raise HTTPException(404, detail={"code": "camera_event_not_found"})
    context = timezone_context(db)
    read = row.id in camera_events.marked_read_ids(db, user.id, [row.id])
    return {"event": camera_events.serialize(row, read=read, ctx=context), "timezone": context.name}


@router.put("/devices/{device_id}")
def registration(device_id: UUID, payload: Registration, db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    try:
        return push.register_device(db, user.id, str(device_id), payload.revision, payload.label,
            payload.enabled, payload.subscription, badge_enabled=payload.badge_enabled)
    except PushEndpointUnavailable as exc:
        db.rollback()
        raise HTTPException(503, detail={"code": str(exc)}) from None
    except ValueError as exc:
        db.rollback()
        code = str(exc)
        if code not in {"mobile_push_revision_conflict", "mobile_push_device_limit"}:
            code = "mobile_push_subscription_invalid"
        raise HTTPException(409 if code == "mobile_push_revision_conflict" else 422, detail={"code": code}) from None
