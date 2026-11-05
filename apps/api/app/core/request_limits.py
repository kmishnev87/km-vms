"""Bound request input before FastAPI parses it; response/media size is unrelated."""
from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse

MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_LOGIN_BODY_BYTES = 16 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 30


class RequestBodyLimitMiddleware:
    def __init__(self, app, *, max_bytes=MAX_REQUEST_BODY_BYTES, login_bytes=MAX_LOGIN_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes
        self.login_bytes = login_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.login_bytes if scope.get("path", "").rstrip("/") == "/auth/login" else self.max_bytes

        async def reject(code, detail):
            await JSONResponse({"detail": detail}, status_code=code)(scope, receive, send)

        lengths = [value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"]
        if lengths:
            if any(not value.isdigit() or len(value) > 20 for value in lengths) or len(set(lengths)) != 1:
                return await reject(400, "Invalid request length")
            if int(lengths[0]) > limit:
                return await reject(413, "Request body is too large")

        body = bytearray()
        try:
            async with asyncio.timeout(REQUEST_BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > limit:
                        return await reject(413, "Request body is too large")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "Request body timed out")

        replayed = False

        async def bounded_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # Preserve real disconnect delivery for StreamingResponse; never spin
            # an endless sequence of synthetic empty request messages.
            return await receive()

        await self.app(scope, bounded_receive, send)
