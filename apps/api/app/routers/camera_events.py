from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.core.permissions import user_has_permission
from app.core.security import decrypt_text
from app.db.session import get_db
from app.models.camera import Camera
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.routers.deps import require_permission
from app.services import camera_events as events
from app.services.audit_log import create_event
from app.services.camera_events_worker import receiver_states
from app.services.onvif_events import DeviceClient, EventDeviceError, detector_lock, detector_state, set_detector
from app.services.timezone_contract import format_system_iso, parse_api_timestamp, timezone_context

router = APIRouter(prefix="/camera-events", tags=["camera-events"])
EventKind = Literal["motion", "person", "vehicle", "animal", "intrusion", "line_crossing",
                    "loitering", "tamper", "audio", "wrong_direction", "fire", "other"]


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notifications: StrictBool = True
    camera_ids: list[StrictInt] | None = Field(None, max_length=512)
    kinds: list[EventKind] | None = Field(None, max_length=12)
    hours_enabled: StrictBool = False
    start_minute: int = Field(0, ge=0, le=1439)
    end_minute: int = Field(0, ge=0, le=1439)


class ReadSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[UUID] = Field(max_length=200)


class ReadAll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    through: str = Field(min_length=39, max_length=100)


class DeleteSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[UUID] = Field(min_length=1, max_length=200)


class DetectorChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def _failure(code, status=409):
    raise HTTPException(status_code=status, detail={"code": code, "message": code})


def _camera(db, camera_id):
    camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).first()
    if camera is None:
        _failure("event_camera_not_available", 404)
    return camera


def _client(camera):
    if camera.protocol != "onvif":
        raise EventDeviceError("analytics_unsupported")
    password = decrypt_text(camera.password_encrypted)
    if not password or not camera.username:
        raise EventDeviceError("camera_credentials_unavailable")
    return DeviceClient(camera.host, camera.port, camera.username, password, camera.onvif_path)


def _since(value, db):
    if not value:
        return None
    try:
        return parse_api_timestamp(value, timezone_context(db), field_name="event_date").storage_utc
    except ValueError:
        _failure("invalid_event_date", 422)


@router.get("")
def history(camera_id: int | None = Query(None, ge=1), kind: EventKind | None = None,
            since: str | None = Query(None, max_length=40), until: str | None = Query(None, max_length=40), unread: bool = False,
            limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
            db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    start, end = _since(since, db), _since(until, db)
    if start and end and start > end:
        _failure("invalid_event_date", 422)
    return events.list_camera_events(db, user.id, camera_id=camera_id, kind=kind, since=start, until=end,
                                    unread=unread, limit=limit, offset=offset)


@router.get("/cameras")
def cameras(db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    states = receiver_states()
    return {"items": [{"id": c.id, "name": c.name, "protocol": c.protocol, "enabled": c.enabled, "events_enabled": c.events_enabled,
                        "receiver": {"status": "unsupported"} if c.protocol != "onvif" else
                        {"status": "disabled"} if not c.events_enabled else states.get(str(c.id), {"status": "pending"})}
                       for c in db.query(Camera).filter(Camera.deleted_at.is_(None)).order_by(Camera.name).all()]}


@router.get("/preferences")
def get_preferences(db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    return events.preferences(db, user.id)[0]


@router.put("/preferences")
def put_preferences(payload: Preferences, db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    value = payload.model_dump()
    if value["camera_ids"] is not None and any(item < 1 for item in value["camera_ids"]):
        _failure("invalid_event_camera", 422)
    return events.save_preferences(db, user.id, value)


@router.post("/read")
def mark_read(payload: ReadSelection, db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    return {"marked": events.mark_read(db, user.id, [str(item) for item in payload.ids])}


@router.post("/read-all")
def mark_all_read(payload: ReadAll, db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    parsed, identifier = _snapshot_cursor(payload.through)
    return {"marked": events.mark_all_read(db, user.id, parsed, identifier)}


def _snapshot_cursor(through):
    try:
        stamp, identifier = through.split("|", 1)
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is not None:
            raise ValueError()
        identifier = str(UUID(identifier))
    except (ValueError, TypeError):
        _failure("invalid_event_cursor", 422)
    return parsed, identifier


@router.post("/delete")
def delete_selected(payload: DeleteSelection, db: Session = Depends(get_db), user: User = Depends(require_permission("manage_cameras"))):
    return {"deleted": events.delete_camera_events(db, ids=[str(item) for item in payload.ids], actor=user)}


@router.post("/delete-all")
def delete_history(payload: ReadAll, db: Session = Depends(get_db), user: User = Depends(require_permission("manage_cameras"))):
    return {"deleted": events.delete_camera_events(db, through=_snapshot_cursor(payload.through), actor=user)}


@router.get("/updates")
async def updates(request: Request, after: str | None = Query(None, max_length=100), wait: int = Query(25, ge=0, le=25),
                  db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    if after:
        try:
            stamp, identifier = after.split("|", 1)
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is not None:
                raise ValueError()
            UUID(identifier)
        except (ValueError, TypeError):
            _failure("invalid_event_cursor", 422)
    user_id = user.id
    listener = events.listen(user_id) if after and wait else None
    if after and wait and listener is None:
        _failure("event_watch_limit", 429)
    try:
        result = await run_in_threadpool(events.updates, db, user_id, after)
        if after and not result["items"] and wait:
            # No waiting thread or checked-out database connection per browser tab.
            db.rollback()
            async def disconnected():
                while (await request.receive())["type"] != "http.disconnect":
                    pass
            waiting = asyncio.create_task(listener[1].wait())
            closing = asyncio.create_task(disconnected())
            try:
                done, _ = await asyncio.wait({waiting, closing}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                if closing in done:
                    return Response(status_code=204)
            finally:
                waiting.cancel(); closing.cancel()
                await asyncio.gather(waiting, closing, return_exceptions=True)
            db.expire_all()
            fresh = await run_in_threadpool(db.get, User, user_id)
            if fresh is None or not fresh.is_active or not user_has_permission(fresh.role, "view_live"):
                _failure("event_access_denied", 403)
            result = await run_in_threadpool(events.updates, db, user_id, after)
            result["changed"] = listener[1].is_set()
    finally:
        if listener:
            events.unlisten(listener[0])
    ctx = timezone_context(db)
    result["timezone"] = ctx.name
    for item in result["items"]:
        item["notify"] = events.notifications_allowed(result["preferences"], item, ctx)
    return result


@router.get("/cameras/{camera_id}/detectors")
def read_detectors(camera_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("manage_cameras"))):
    camera = _camera(db, camera_id)
    try:
        state, _ = detector_state(_client(camera), camera.onvif_profile_token)
        return {"supported": state["supported"], "rules": state["rules"]}
    except EventDeviceError as exc:
        if exc.code in {"analytics_unsupported", "analytics_profile_unavailable"}:
            return {"supported": False, "rules": [], "reason": exc.code}
        _failure(exc.code, 503)
    except Exception:
        _failure("detector_read_failed", 503)


@router.get("/markers")
def markers(camera_ids: str = Query(..., max_length=1024), from_ts: str = Query(..., alias="from", max_length=40),
            to_ts: str = Query(..., alias="to", max_length=40), db: Session = Depends(get_db),
            user: User = Depends(require_permission("view_timeline"))):
    try:
        ids = list(dict.fromkeys(int(value) for value in camera_ids.split(",") if value))
        if not 1 <= len(ids) <= 64 or any(value <= 0 for value in ids):
            raise ValueError()
    except ValueError:
        _failure("invalid_event_camera", 422)
    start, end = _since(from_ts, db), _since(to_ts, db)
    if end <= start or (end - start).total_seconds() > 8 * 86400:
        _failure("invalid_event_date", 422)
    rows = events.event_query(db).filter(AuditEvent.target_type == "camera", AuditEvent.target_id.in_([str(v) for v in ids]),
        AuditEvent.created_at >= start - timedelta(minutes=5), AuditEvent.created_at <= end + timedelta(minutes=5)).order_by(AuditEvent.created_at.desc()).limit(5001).all()
    ctx = timezone_context(db)
    result = []
    for row in rows[:5000]:
        event = events.serialize(row)
        instant = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        if start <= instant <= end:
            result.append({"id": row.id, "camera_id": event["camera_id"], "kind": event["kind"], "topic": event["topic"],
                           "timestamp_system": format_system_iso(instant, ctx)[:19]})
    return {"items": result, "truncated": len(rows) > 5000}


@router.put("/cameras/{camera_id}/detectors/{rule_id}")
def change_detector(camera_id: int, rule_id: str, payload: DetectorChange, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("manage_cameras"))):
    if len(rule_id) != 32 or any(c not in "0123456789abcdef" for c in rule_id):
        _failure("invalid_detector_id", 422)
    camera = _camera(db, camera_id)
    try:
        identity = f"{camera.host}:{camera.port}:{camera.onvif_path}"
        with detector_lock(identity):
            state = set_detector(_client(camera), camera.onvif_profile_token, rule_id, payload.enabled, payload.revision)
        create_event(db=db, actor=user, category="cameras", event_type="cameras.detector.changed", severity="info",
                     message_ru="Изменено обнаружение на камере", message_en="Camera detection changed",
                     target_type="camera", target_id=camera.id, target_name=camera.name,
                     metadata={"detector_id": rule_id, "enabled": payload.enabled})
        return {"supported": state["supported"], "rules": state["rules"]}
    except EventDeviceError as exc:
        _failure(exc.code)
    except Exception:
        _failure("detector_change_unconfirmed", 503)


@router.get("/{event_id}/target")
def target(event_id: UUID, mode: Literal["live", "archive"], db: Session = Depends(get_db), user: User = Depends(require_permission("view_live"))):
    row = events.event_query(db).filter_by(id=str(event_id)).first()
    if row is None:
        _failure("event_not_found", 404)
    value = events.serialize(row)
    if not value["camera_id"]:
        _failure("event_source_ambiguous")
    if mode == "live":
        camera = _camera(db, value["camera_id"])
        return {"camera_id": camera.id, "camera_name": camera.name, "url": "/live?camera_event=" + row.id}
    if not user_has_permission(user.role, "view_timeline"):
        _failure("event_access_denied", 403)
    try:
        instant = datetime.fromisoformat(value["occurred_at"].replace("Z", "+00:00"))
    except (ValueError, TypeError):
        _failure("event_time_unavailable")
    from app.routers.chronology import chronology_playback
    requested = instant - timedelta(seconds=5)
    playback = chronology_playback(camera_id=value["camera_id"], ts=requested.isoformat(), db=db, current_user=user)
    if not playback.get("has_video"):
        requested = instant
        playback = chronology_playback(camera_id=value["camera_id"], ts=requested.isoformat(), db=db, current_user=user)
        if playback.get("has_video") and playback.get("file_start_system"):
            earliest = datetime.fromisoformat(playback["file_start_system"])
            requested = max(instant - timedelta(seconds=5), earliest)
    if not playback.get("has_video"):
        _failure("event_archive_unavailable", 404)
    ctx = timezone_context(db)
    return {"camera_id": value["camera_id"], "camera_name": row.target_name, "timestamp_system": format_system_iso(requested.astimezone(timezone.utc).replace(tzinfo=None), ctx)[:19],
            "url": "/chronology?camera_event=" + row.id}
