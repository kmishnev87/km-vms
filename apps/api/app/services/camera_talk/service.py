import asyncio
import audioop
from dataclasses import dataclass, field
import secrets
import time

from .rtsp import RtspTalk
from .tapo import TapoTalk, probe as tapo_probe
from .types import TalkError, TalkSource


def discover(source):
    rtsp = None
    reason = "backchannel_unsupported"
    try:
        rtsp = RtspTalk(source)
        return rtsp.describe()
    except TalkError as error:
        reason = error.code
    except (OSError, ValueError):
        reason = "camera_unavailable"
    finally:
        if rtsp:
            rtsp.close()
    try:
        candidate = tapo_probe(source)
    except (TalkError, OSError, ValueError):
        candidate = None
    if candidate:
        return candidate
    raise TalkError(reason)


@dataclass(repr=False)
class TalkSession:
    id: str
    user_id: int
    candidate: object
    created: float = field(default_factory=time.monotonic)
    transport: object = None
    socket: object = None
    timer: object = None
    closing: bool = False
    resampler: object = None
    audio_budget: float = 0.2
    audio_clock: float = field(default_factory=time.monotonic)

    def encode(self, pcm):
        if len(pcm) != 640:
            raise TalkError("invalid_audio_frame", 400)
        now = time.monotonic()
        self.audio_budget = min(0.2, self.audio_budget + now - self.audio_clock) - 0.02
        self.audio_clock = now
        if self.audio_budget < -0.02:
            raise TalkError("audio_queue_overflow", 429)
        reduced, self.resampler = audioop.ratecv(pcm, 2, 1, 16000, 8000, self.resampler)
        return audioop.lin2alaw(reduced, 2) if self.candidate.codec == "PCMA" else audioop.lin2ulaw(reduced, 2)


class TalkManager:
    def __init__(self):
        self.sessions = {}
        self.cache = {}
        self.lock = asyncio.Lock()
        self.probes = asyncio.Semaphore(4)

    async def capability(self, source):
        cached = self.cache.get(source.camera_id)
        if cached and cached[0] == source.signature and time.monotonic() - cached[1] < 60:
            if isinstance(cached[2], TalkError):
                raise cached[2]
            return cached[2]
        try:
            await asyncio.wait_for(self.probes.acquire(), 1)
        except asyncio.TimeoutError:
            raise TalkError("discovery_busy", 429) from None
        try:
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            try:
                result = await asyncio.to_thread(discover, source)
            except TalkError as error:
                self.cache[source.camera_id] = (source.signature, time.monotonic(), error)
                raise
            if len(self.cache) >= 128:
                self.cache.pop(next(iter(self.cache)))
            self.cache[source.camera_id] = (source.signature, time.monotonic(), result)
            return result
        finally:
            self.probes.release()

    async def reserve(self, source, user_id):
        candidate = await self.capability(source)
        if candidate.public()["status"] == "needs_credentials":
            raise TalkError("talk_credentials_required")
        async with self.lock:
            if any(s.candidate.output_key == candidate.output_key for s in self.sessions.values()):
                raise TalkError("camera_busy")
            if len(self.sessions) >= 4:
                raise TalkError("server_busy", 429)
            session = TalkSession(secrets.token_urlsafe(24), user_id, candidate)
            self.sessions[session.id] = session
            session.timer = asyncio.get_running_loop().call_later(10, lambda: asyncio.create_task(self.close(session.id)))
            return session

    def owned(self, session_id, user_id):
        session = self.sessions.get(session_id)
        if not session or session.user_id != user_id or session.closing:
            raise TalkError("call_not_found", 404)
        return session

    async def start(self, session, websocket):
        if session.socket is not None:
            raise TalkError("camera_busy")
        session.socket = websocket
        session.timer.cancel()
        session.timer = asyncio.get_running_loop().call_later(600, lambda: asyncio.create_task(self.close(session.id)))
        def connect():
            transport = TapoTalk(session.candidate.source) if session.candidate.adapter == "tapo" else RtspTalk(session.candidate.source)
            session.transport = transport
            if session.closing:
                transport.close()
                raise TalkError("call_ended")
            transport.start(session.candidate)
            transport.wire.timeout = 1
            transport.wire.sock.settimeout(1)
        # Socket creation and authentication must not block the API event loop.
        # The router sends a safe failure reason before its finally closes WS.
        await asyncio.to_thread(connect)

    async def close(self, session_id):
        session = self.sessions.get(session_id)
        if not session or session.closing:
            return
        session.closing = True
        if session.timer:
            session.timer.cancel()
        try:
            if session.transport:
                await asyncio.to_thread(session.transport.close)
            if session.socket:
                try:
                    await session.socket.close(code=1000)
                except Exception:
                    pass
        finally:
            self.sessions.pop(session_id, None)

    async def shutdown(self):
        await asyncio.gather(*(self.close(key) for key in list(self.sessions)))


manager = TalkManager()
