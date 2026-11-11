"""On-demand, shared copy/remux streams. No service, polling worker or disk files.

One FFmpeg per camera/source is shared by WebSocket viewers. The last disconnect
terminates it. Slow viewers disconnect instead of building seconds of latency.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import subprocess
import time
from anyio import CancelScope

from app.core.config import settings
from .ffmpeg import choose_input_url
from .fragmented_mp4 import build_mse_command, initialization_info, read_box

MAX_STREAMS = 16
MAX_PEERS = 64
MAX_QUEUE_BYTES = 16 * 1024 * 1024


class MsePeer:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=16)
        self.bytes = 0
        self.closed = False

    def push(self, value):
        if self.closed:
            return
        size = len(value) if isinstance(value, bytes) else 0
        if self.queue.full() or self.bytes + size > MAX_QUEUE_BYTES:
            self.fail("mse_slow_viewer")
            return
        self.bytes += size
        self.queue.put_nowait(value)

    def fail(self, code):
        if self.closed:
            return
        self.closed = True
        while not self.queue.empty():
            self.queue.get_nowait()
        self.bytes = 0
        self.queue.put_nowait({"type": "error", "code": code})

    async def receive(self, timeout=15.0):
        value = await asyncio.wait_for(self.queue.get(), timeout)
        if isinstance(value, bytes):
            self.bytes -= len(value)
        return value


class MseStream:
    def __init__(self, camera, stream, url, signature):
        self.camera_id, self.stream = int(camera.id), stream
        self.signature = signature
        self.command = build_mse_command(camera, stream, url, str(settings.live_audio_mode or "none").lower())
        self.peers = set()
        self.initialization = None
        self.info = None
        self.proc = None
        self.task = None
        self.closed = False
        self.started = time.time()
        self.failure = None

    def subscribe(self):
        peer = MsePeer()
        if self.failure:
            peer.fail(self.failure)
        else:
            if self.info:
                peer.push(self.info)
                peer.push(self.initialization)
            self.peers.add(peer)
        return peer

    def broadcast(self, value):
        for peer in tuple(self.peers):
            peer.push(value)

    async def run(self):
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command, stdin=subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=subprocess.DEVNULL, limit=256 * 1024,
            )
            ftyp = b""
            fragment = b""
            while not self.closed:
                kind, data = await asyncio.wait_for(read_box(self.proc.stdout), 15)
                if kind == b"ftyp":
                    ftyp = data
                elif kind == b"moov":
                    self.info = initialization_info(data)
                    audio_mode = str(settings.live_audio_mode or "none").lower()
                    self.info.update(audio_mode=audio_mode,
                                     audio_enabled=audio_mode not in {"none", "off", "disable", "disabled"})
                    self.initialization = ftyp + data
                    self.broadcast(self.info)
                    self.broadcast(self.initialization)
                elif kind == b"moof":
                    fragment = data
                elif kind == b"mdat" and fragment:
                    self.broadcast(fragment + data)
                    fragment = b""
        except asyncio.CancelledError:
            raise
        except Exception:
            # Device diagnostics/credentials/FFmpeg command lines never cross
            # the media boundary. Ordinary HLS remains the user's fallback.
            self.failure = "mse_stream_failed"
            for peer in tuple(self.peers):
                peer.fail(self.failure)
        finally:
            with CancelScope(shield=True):
                await self.terminate()

    async def terminate(self):
        if self.proc and self.proc.returncode is None:
            async def discard_output():
                while await self.proc.stdout.read(65536):
                    pass
            # Once the media reader is cancelled, drain its pipe during shutdown;
            # otherwise an unwatched full stdout pipe can delay process.wait().
            drain = asyncio.create_task(discard_output())
            with suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                try:
                    await asyncio.wait_for(self.proc.wait(), 2)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        self.proc.kill()
                    await asyncio.wait_for(self.proc.wait(), 2)
            finally:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)

    async def stop(self):
        self.closed = True
        for peer in tuple(self.peers):
            peer.fail("mse_stream_closed")
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def snapshot(self):
        audio_mode = str(settings.live_audio_mode or "none").lower()
        audio_enabled = audio_mode not in {"none", "off", "disable", "disabled"}
        return {"camera_id": self.camera_id, "stream": self.stream, "stream_type": self.stream,
                "stream_key": f"{self.camera_id}:{self.stream}:mse", "transport": "mse",
                "status": "failed" if self.failure else "ready" if self.info else "starting",
                "running": bool(self.proc and self.proc.returncode is None), "ready": bool(self.info and not self.failure),
                "mode": "copy", "viewers": sum(not peer.closed for peer in tuple(self.peers)),
                "uptime_seconds": round(time.time() - self.started, 2), "high_cpu_risk": False,
                "input_codec": (self.info or {}).get("input_codec"), "audio_mode": audio_mode,
                "audio_enabled": audio_enabled, "audio_available": bool((self.info or {}).get("audio_available")),
                "failure_reason": self.failure}


class MseRegistry:
    def __init__(self):
        self.streams = {}
        self.lock = asyncio.Lock()
        self.loop = None

    @staticmethod
    def signature(camera, stream):
        url = choose_input_url(camera, stream)
        if not url:
            raise ValueError("no_rtsp_url")
        value = f"{url}|{camera.rtsp_transport}|{settings.live_audio_mode}"
        return url, hashlib.sha256(value.encode()).hexdigest()

    async def subscribe(self, camera, stream):
        self.loop = asyncio.get_running_loop()
        key = int(camera.id), stream
        url, signature = self.signature(camera, stream)
        async with self.lock:
            if sum(len(item.peers) for item in self.streams.values()) >= MAX_PEERS:
                raise ValueError("resource_limit")
            item = self.streams.get(key)
            if item and (item.signature != signature or item.failure or item.closed):
                await item.stop()
                self.streams.pop(key, None)
                item = None
            if item is None:
                if len(self.streams) >= MAX_STREAMS:
                    raise ValueError("resource_limit")
                item = MseStream(camera, stream, url, signature)
                self.streams[key] = item
                item.task = asyncio.create_task(item.run(), name=f"live-mse-{key[0]}-{stream}")
            return item, item.subscribe()

    async def unsubscribe(self, item, peer):
        async with self.lock:
            item.peers.discard(peer)
            peer.closed = True
            if not item.peers:
                key = item.camera_id, item.stream
                if self.streams.get(key) is item:
                    self.streams.pop(key, None)
                await item.stop()

    async def stop(self, camera_id=None, stream=None):
        async with self.lock:
            items = [item for item in self.streams.values()
                     if (camera_id is None or item.camera_id == camera_id)
                     and (stream is None or item.stream == stream)]
            for item in items:
                self.streams.pop((item.camera_id, item.stream), None)
            await asyncio.gather(*(item.stop() for item in items))
            return len(items)

    def stop_sync(self, camera_id=None, stream=None):
        if not self.loop or not self.loop.is_running():
            return 0
        operation = self.stop(camera_id, stream)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.loop.create_task(operation)
            return 0
        return asyncio.run_coroutine_threadsafe(operation, self.loop).result(timeout=8)

    def snapshots(self, camera_id=None, stream=None):
        return [item.snapshot() for item in list(self.streams.values())
                if (camera_id is None or item.camera_id == camera_id) and (stream is None or item.stream == stream)]
