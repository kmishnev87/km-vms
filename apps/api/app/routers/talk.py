"""Native camera calls. Audio frames are relayed in memory, never recorded."""
import asyncio
import json
import time
from dataclasses import replace

from anyio import CancelScope

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.permissions import PERMISSION_TALK_CAMERAS
from app.core.security import encrypt_text
from app.db.session import SessionLocal, get_db
from app.models.camera import Camera
from app.models.user import User
from app.routers.deps import require_permission
from app.services.camera_talk.service import manager
from app.services.camera_talk.tapo import verify_credential
from app.services.camera_talk.types import TalkError, TalkSource
from app.services.media_tokens import create_media_token, media_token_response, validate_media_token

router = APIRouter(prefix="/talk", tags=["camera-talk"])
require_talk = require_permission(PERMISSION_TALK_CAMERAS)
SERVER_HEARTBEAT_SECONDS = 2


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: int = Field(gt=0)


class TalkCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str | None = Field(default=None, min_length=1, max_length=255, repr=False)


def source_for(db, camera_id):
    camera = db.get(Camera, camera_id)
    if not camera or camera.deleted_at is not None:
        raise TalkError("camera_unavailable", 404)
    return TalkSource.from_camera(camera)


def http_error(error):
    return HTTPException(error.status, detail=error.code)


def resource(session):
    return {"session_id": session.id, "camera_id": session.candidate.source.camera_id, "action": "talk"}


def token_for(session, user):
    token, expiry = create_media_token(user=user, scope="talk", resource=resource(session))
    return media_token_response(token, expiry)


@router.get("/cameras/{camera_id}")
async def capability(camera_id: int, user: User = Depends(require_talk), db: Session = Depends(get_db)):
    try:
        source = source_for(db, camera_id)
        db.rollback()  # Do not hold a database transaction while probing a device.
        return (await manager.capability(source)).public()
    except TalkError as error:
        if error.status == 404:
            raise http_error(error) from None
        unsupported = error.code in {"backchannel_unsupported", "codec_unsupported", "transport_unsupported"}
        return {"status": "unsupported" if unsupported else "unavailable", "reason": error.code}


@router.put("/cameras/{camera_id}/credentials")
async def save_credential(camera_id: int, body: TalkCredential, user: User = Depends(require_talk), db: Session = Depends(get_db)):
    try:
        original = source_for(db, camera_id)
        db.rollback()
        if body.password is not None:
            try:
                await asyncio.wait_for(manager.probes.acquire(), 1)
            except asyncio.TimeoutError:
                raise TalkError("discovery_busy", 429) from None
            try:
                # Authentication only: neither the microphone nor the camera's
                # speaker is activated just to validate a password.
                await asyncio.to_thread(verify_credential, replace(original, talk_password=body.password))
            except (OSError, ValueError):
                raise TalkError("camera_unavailable") from None
            finally:
                manager.probes.release()
        db.expire_all()
        camera = db.query(Camera).filter(Camera.id == camera_id, Camera.deleted_at.is_(None)).with_for_update().first()
        if not camera:
            raise TalkError("camera_unavailable", 404)
        if TalkSource.from_camera(camera).signature != original.signature:
            raise TalkError("camera_configuration_changed")
    except TalkError as error:
        raise http_error(error) from None
    camera.talk_password_encrypted = encrypt_text(body.password) if body.password else None
    db.commit()
    manager.cache.pop(camera_id, None)
    return {"saved": bool(body.password)}


def forget_rejected_credential(session):
    """Discard only the exact saved Tapo secret rejected by this call.

    A concurrent password replacement must survive an old session's failure.
    Network errors and access-policy denials never erase credentials.
    """
    candidate = session.candidate
    if candidate.adapter != "tapo" or not candidate.source.talk_password:
        return
    with SessionLocal() as db:
        camera = db.query(Camera).filter(Camera.id == candidate.source.camera_id).with_for_update().first()
        if camera and camera.deleted_at is None and TalkSource.from_camera(camera).signature == candidate.source.signature:
            camera.talk_password_encrypted = None
            db.commit()
            manager.cache.pop(camera.id, None)


@router.post("/sessions")
async def create_call(body: CallRequest, user: User = Depends(require_talk), db: Session = Depends(get_db)):
    try:
        user_id = user.id
        source = source_for(db, body.camera_id)
        db.rollback()
        candidate = await manager.capability(source)
        if candidate.public()["status"] == "needs_credentials":
            raise TalkError("talk_credentials_required")
        session = await manager.reserve(source, user_id)
        return {"session_id": session.id, "ws_path": f"api/talk/sessions/{session.id}/audio",
                "sample_rate": 16000, "channels": 1, "format": "pcm_s16le", "frame_bytes": 640,
                **candidate.public(), **token_for(session, user)}
    except TalkError as error:
        raise http_error(error) from None


@router.post("/sessions/{session_id}/token")
def renew_call(session_id: str, user: User = Depends(require_talk)):
    try:
        return token_for(manager.owned(session_id, user.id), user)
    except TalkError as error:
        raise http_error(error) from None


@router.delete("/sessions/{session_id}")
async def end_call(session_id: str, user: User = Depends(require_talk)):
    try:
        manager.owned(session_id, user.id)
    except TalkError:
        # Idempotent and does not reveal another actor's session.
        return {"ended": True}
    await manager.close(session_id)
    return {"ended": True}


def authorize_socket(session, token):
    with SessionLocal() as db:
        actor = validate_media_token(db, token=token, scope="talk", resource=resource(session), permission=PERMISSION_TALK_CAMERAS)
        if actor.id != session.user_id:
            raise TalkError("call_not_found", 404)
        if source_for(db, session.candidate.source.camera_id).signature != session.candidate.source.signature:
            raise TalkError("camera_configuration_changed")


@router.websocket("/sessions/{session_id}/audio")
async def audio_socket(websocket: WebSocket, session_id: str):
    session = manager.sessions.get(session_id)
    # No browser capture in this release; never accept cross-origin browser sockets.
    if not session or session.closing or websocket.headers.get("origin"):
        await websocket.close(code=1008)
        return
    header = websocket.headers.get("authorization", "")
    token = header[7:] if header.startswith("Bearer ") else None
    accepted = False
    try:
        authorize_socket(session, token)
        if session.socket is not None:
            raise TalkError("camera_busy")
        await websocket.accept()
        accepted = True
        await manager.start(session, websocket)
        await websocket.send_json({"type": "ready", **session.candidate.public()})
        checked = time.monotonic()
        pinged = checked
        replied = checked
        while not session.closing:
            message = await asyncio.wait_for(websocket.receive(), 3)
            if message["type"] == "websocket.disconnect":
                break
            if time.monotonic() - checked > 5:
                authorize_socket(session, token)
                checked = time.monotonic()
            if time.monotonic() - pinged > 20:
                await asyncio.to_thread(session.transport.keepalive)
                pinged = time.monotonic()
            if message.get("bytes") is not None:
                audio = session.encode(message["bytes"])
                await asyncio.wait_for(asyncio.to_thread(session.transport.send_audio, audio), 2)
            elif message.get("text") is not None:
                if len(message["text"]) > 2048:
                    raise TalkError("invalid_control")
                control = json.loads(message["text"])
                if control.get("type") == "authorization":
                    replacement = control.get("media_token")
                    authorize_socket(session, replacement)
                    token = replacement
                elif control.get("type") != "keepalive":
                    raise TalkError("invalid_control")
            else:
                raise TalkError("invalid_control")
            # Client-to-server audio does not reset nginx's upstream read
            # timeout. A bounded reply also lets a muted native caller detect
            # a dead connection instead of silently keeping its microphone UI.
            if time.monotonic() - replied >= SERVER_HEARTBEAT_SECONDS:
                await asyncio.wait_for(websocket.send_json({"type": "keepalive"}), 2)
                replied = time.monotonic()
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as error:
        if accepted:
            code = error.code if isinstance(error, TalkError) else "call_failed"
            if code == "talk_authentication_failed":
                try:
                    await asyncio.to_thread(forget_rejected_credential, session)
                except Exception:
                    # Deliver the original safe failure even if persistence is
                    # temporarily unavailable; the native UI still offers edit.
                    pass
            try:
                await websocket.send_json({"type": "error", "reason": code})
            except Exception:
                pass
    finally:
        if accepted and session.socket is websocket:
            # A disconnect may cancel the ASGI scope while the blocking camera
            # transport is being closed. Finish release before leaving that
            # scope so the next attempt does not inherit a busy speaker.
            with CancelScope(shield=True):
                await manager.close(session_id)
        elif not accepted:
            await websocket.close(code=1008)
