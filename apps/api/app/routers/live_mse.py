"""The Live router's same-origin, scoped-cookie WebSocket media adapter."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, WebSocket, WebSocketDisconnect
import jwt
from anyio import CancelScope

from app.core.config import settings
from app.core.live_cookies import live_browser_cookie
from app.core.permissions import PERMISSION_VIEW_LIVE
from app.db.session import SessionLocal
from app.services.live_engine import manager
from app.services.live_engine.stream_instance import _camera_source
from app.services.media_tokens import validate_media_token


def same_origin(websocket):
    try:
        origin = urlsplit(websocket.headers.get("origin", ""))
        return (origin.scheme in {"http", "https"} and bool(origin.netloc)
                and origin.netloc.lower() == websocket.headers.get("host", "").lower()
                and not origin.username and not origin.password)
    except ValueError:
        return False


async def serve_mse(websocket: WebSocket, camera_id: int, stream: str):
    from app.routers.live import _get_camera, _live_media_cookie_name, _live_media_resource

    if not same_origin(websocket):
        await websocket.close(code=1008)
        return
    browser = websocket.cookies.get(live_browser_cookie())
    token = websocket.cookies.get(_live_media_cookie_name(camera_id, stream))
    if not browser or not token:
        await websocket.close(code=1008)
        return

    def authorize(credential):
        with SessionLocal() as db:
            user = validate_media_token(db, token=credential, scope="live",
                resource={**_live_media_resource(camera_id, stream), "browser": browser},
                permission=PERMISSION_VIEW_LIVE, request=websocket, media_area="live")
            source = _camera_source(_get_camera(db, camera_id))
            expires = jwt.decode(credential, settings.jwt_secret, algorithms=["HS256"])["exp"]
            return user.id, source, expires

    item = peer = None
    accepted = False
    tasks = []
    try:
        owner_id, source, expires_at = await asyncio.to_thread(authorize, token)
        source_signature = manager.mse.signature(source, stream)[1]
        await websocket.accept()
        accepted = True
        item, peer = await manager.mse.subscribe(source, stream)
        next_check = min(time.time() + 15, expires_at)

        async def revalidate(credential):
            nonlocal token, expires_at, next_check
            user_id, current, expires = await asyncio.to_thread(authorize, credential)
            if user_id != owner_id or manager.mse.signature(current, stream)[1] != source_signature:
                raise HTTPException(status_code=403, detail="Live authority changed")
            token, expires_at = credential, expires
            next_check = min(time.time() + 15, expires)

        async def send_media():
            while True:
                if time.time() >= next_check:
                    await revalidate(token)
                try:
                    packet = await peer.receive(timeout=max(.1, min(15, next_check - time.time())))
                except asyncio.TimeoutError:
                    # This timeout wakes the authority check; it is not proof
                    # that media failed. Clock rounding or a concurrent token
                    # refresh can leave the new deadline ahead of this wakeup.
                    # The source reader owns media timeout/EOF and queues errors.
                    continue
                if time.time() >= next_check:
                    await revalidate(token)
                if isinstance(packet, bytes):
                    await asyncio.wait_for(websocket.send_bytes(packet), 2)
                else:
                    await asyncio.wait_for(websocket.send_json(packet), 2)
                    if packet.get("type") == "error":
                        return

        async def receive_control():
            while True:
                message = await websocket.receive_text()
                if len(message) > 4096:
                    raise ValueError("mse_invalid_message")
                try:
                    value = json.loads(message)
                except (TypeError, ValueError):
                    raise ValueError("mse_invalid_message") from None
                if not isinstance(value, dict) or value.get("type") != "refresh" or not isinstance(value.get("media_token"), str):
                    raise ValueError("mse_invalid_message")
                await revalidate(value["media_token"])

        tasks = [asyncio.create_task(send_media()), asyncio.create_task(receive_control())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except HTTPException:
        if accepted:
            with suppress(Exception):
                await websocket.send_json({"type": "error", "code": "mse_access_denied"})
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        if accepted:
            with suppress(Exception):
                await websocket.send_json({"type": "error", "code": "mse_stream_failed"})
    finally:
        for task in tasks:
            task.cancel()
        # ASGI can cancel the scope while an authority refresh is in flight.
        # Cleanup must still detach this peer and release the last RTSP source.
        with CancelScope(shield=True):
            if item is not None:
                await manager.mse.unsubscribe(item, peer)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(Exception):
                await websocket.close(code=1000 if accepted else 1008)
