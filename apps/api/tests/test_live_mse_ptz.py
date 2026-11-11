import asyncio
from types import SimpleNamespace as NS
import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.live_cookies import live_browser_cookie
from app.routers import live, live_mse
from app.services import media_tokens
from app.services.live_engine.fragmented_mp4 import (
    MAX_BOX_BYTES, mp4_box_size, initialization_info, read_box, build_mse_command,
)
from app.services.live_engine.mse import MsePeer, MseRegistry, MseStream


def box(kind, data=b""):
    return (len(data) + 8).to_bytes(4, "big") + kind + data


def test_fmp4_codec_is_taken_from_initialization_not_guessed():
    assert initialization_info(box(b"moov", box(b"avcC", bytes([1, 100, 0, 31]))))["mime"] == 'video/mp4; codecs="avc1.64001F"'
    hevc = bytes([1, 1, 0x60, 0, 0, 0, 0xA0, 0, 0, 0, 0, 0, 123])
    info = initialization_info(box(b"moov", b"hev1" + box(b"hvcC", hevc) + b"mp4a"))
    assert info["mime"] == 'video/mp4; codecs="hev1.1.6.L123.A0,mp4a.40.2"'
    assert info["audio_available"]
    with pytest.raises(ValueError):
        initialization_info(box(b"moov", b"unknown"))


@pytest.mark.parametrize("size", [0, 7, MAX_BOX_BYTES + 1])
def test_mp4_framing_rejects_unbounded_sizes(size):
    with pytest.raises(ValueError):
        mp4_box_size(size.to_bytes(4, "big") + b"mdat")


def test_mp4_reader_accepts_fragmented_headers_and_rejects_truncation():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(box(b"mdat", b"unit-test")); reader.feed_eof()
        kind, data = await read_box(reader)
        assert kind == b"mdat" and data.endswith(b"unit-test")
        with pytest.raises(asyncio.IncompleteReadError):
            await read_box(reader)
    asyncio.run(run())


def test_live_mse_uses_copy_and_short_fragments_without_hls_or_gpu():
    command = build_mse_command(NS(rtsp_transport="tcp"), "sub", "rtsp://unit.invalid/video", "none")
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-frag_duration") + 1] == "100000"
    assert "-an" in command and "hls" not in command and "libx264" not in command


def test_slow_peer_is_disconnected_and_memory_is_released():
    async def run():
        peer = MsePeer()
        for _ in range(17):
            peer.push(b"fragment")
        assert peer.closed and peer.bytes == 0 and peer.queue.qsize() == 1
        assert (await peer.receive())["code"] == "mse_slow_viewer"
    asyncio.run(run())


def test_one_process_owner_is_shared_and_last_viewer_releases_it(monkeypatch):
    async def parked(self):
        await asyncio.Future()
    monkeypatch.setattr(MseStream, "run", parked)
    source = NS(id=1, rtsp_main_url="rtsp://unit.invalid/main", rtsp_sub_url="rtsp://unit.invalid/sub", rtsp_transport="tcp")
    async def run():
        registry = MseRegistry()
        first, a = await registry.subscribe(source, "sub")
        second, b = await registry.subscribe(source, "sub")
        assert first is second and len(registry.streams) == 1
        first.broadcast(b"frame")
        assert await a.receive() == await b.receive() == b"frame"
        await registry.unsubscribe(first, a)
        assert len(registry.streams) == 1 and not first.closed
        await registry.unsubscribe(second, b)
        assert registry.streams == {} and first.closed and first.task.done()
    asyncio.run(run())


@pytest.fixture
def socket_harness(monkeypatch):
    user = NS(id=19, username="unit-viewer", role="viewer", is_active=True)
    camera = NS(id=7, name="PTZ", deleted_at=None, rtsp_main_url="rtsp://unit.invalid/main",
                rtsp_sub_url="rtsp://unit.invalid/sub", rtsp_transport="tcp")
    users = {19: user}
    denied = []
    class Db:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def query(self, _): return self
        def filter(self, condition): self.id = condition.right.value; return self
        def first(self): return users.get(self.id)
    monkeypatch.setattr(live_mse, "SessionLocal", Db)
    monkeypatch.setattr(live, "_get_camera", lambda *_: camera)
    monkeypatch.setattr(media_tokens, "audit_security_denied", lambda **kw: denied.append(kw.get("reason")))
    class Registry:
        count = 0
        released = 0
        @staticmethod
        def signature(source, stream): return "unused", source.rtsp_main_url + stream
        async def subscribe(self, *_):
            self.count += 1
            peer = MsePeer()
            peer.push({"type": "init", "mime": 'video/mp4; codecs="avc1.64001F"'})
            peer.push(b"init")
            return object(), peer
        async def unsubscribe(self, item, peer):
            self.released += 1
    registry = Registry()
    monkeypatch.setattr(live.manager, "mse", registry)
    app = FastAPI(); app.include_router(live.router)
    client = TestClient(app)
    def token(**overrides):
        resource = {"camera_id": 7, "stream": "sub", "browser": "unit-browser", **overrides}
        return media_tokens.create_media_token(user=user, scope="live", resource=resource)[0]
    def headers(credential=None, browser="unit-browser", origin="http://testserver"):
        return {"origin": origin, "cookie": f"{live_browser_cookie()}={browser}; {live._live_media_cookie_name(7,'sub')}={credential or token()}"}
    return NS(client=client, registry=registry, token=token, headers=headers, user=user, camera=camera, denied=denied)


def test_websocket_uses_scoped_cookie_and_refreshes_without_reconnecting(socket_harness):
    h = socket_harness
    with h.client.websocket_connect("/live/7/sub/mse", headers=h.headers()) as ws:
        assert ws.receive_json()["type"] == "init"
        assert ws.receive_bytes() == b"init"
        ws.send_json({"type": "refresh", "media_token": h.token()})
    assert h.registry.count == h.registry.released == 1


@pytest.mark.parametrize("case", ["cross-origin", "wrong-camera", "wrong-stream", "wrong-browser", "expired", "missing"])
def test_websocket_denies_before_starting_a_camera(socket_harness, case):
    h = socket_harness
    headers = h.headers()
    if case == "cross-origin": headers = h.headers(origin="http://other.invalid")
    if case == "wrong-camera": headers = h.headers(h.token(camera_id=8))
    if case == "wrong-stream": headers = h.headers(h.token(stream="main"))
    if case == "wrong-browser": headers = h.headers(browser="other-browser")
    if case == "missing": headers = {"origin": "http://testserver"}
    if case == "expired":
        payload = jwt.decode(h.token(), settings.jwt_secret, algorithms=["HS256"])
        payload["exp"] = int(time.time()) - 1
        headers = h.headers(jwt.encode(payload, settings.jwt_secret, algorithm="HS256"))
    with pytest.raises(WebSocketDisconnect):
        with h.client.websocket_connect("/live/7/sub/mse", headers=headers): pass
    assert h.registry.count == 0


def test_user_revocation_releases_running_mse_subscription(socket_harness):
    h = socket_harness
    with h.client.websocket_connect("/live/7/sub/mse", headers=h.headers()) as ws:
        assert ws.receive_json()["type"] == "init"
        ws.receive_bytes()
        h.user.is_active = False
        ws.send_json({"type": "refresh", "media_token": h.token()})
        assert ws.receive_json() == {"type": "error", "code": "mse_access_denied"}
    assert h.registry.released == 1


def test_camera_connection_change_invalidates_existing_view(socket_harness):
    h = socket_harness
    with h.client.websocket_connect("/live/7/sub/mse", headers=h.headers()) as ws:
        ws.receive_json(); ws.receive_bytes()
        h.camera.rtsp_main_url = "rtsp://unit.invalid/changed"
        ws.send_json({"type": "refresh", "media_token": h.token()})
        assert ws.receive_json()["code"] == "mse_access_denied"
    assert h.registry.released == 1


def test_authority_timer_wakeup_is_not_a_media_failure(socket_harness, monkeypatch):
    h = socket_harness
    clock = NS(now=time.time())
    monkeypatch.setattr(live_mse, "time", NS(time=lambda: clock.now))
    subscribe = h.registry.subscribe

    async def subscribe_with_boundary_wakeup(*args):
        item, peer = await subscribe(*args)
        receive = peer.receive
        calls = 0

        async def boundary_receive(timeout=15):
            nonlocal calls
            calls += 1
            if calls == 2:
                # The permission wakeup can fire just before the wall-clock
                # deadline (or after a concurrent refresh moved that deadline).
                # The next media packet is still queued and the producer is live.
                clock.now += 14.999
                raise asyncio.TimeoutError
            return await receive(timeout)

        peer.receive = boundary_receive
        return item, peer

    monkeypatch.setattr(h.registry, "subscribe", subscribe_with_boundary_wakeup)
    with h.client.websocket_connect("/live/7/sub/mse", headers=h.headers()) as ws:
        assert ws.receive_json()["type"] == "init"
        assert ws.receive_bytes() == b"init"
    assert h.registry.released == 1


def test_authority_wakeup_still_rechecks_a_revoked_user(socket_harness, monkeypatch):
    h = socket_harness
    clock = NS(now=time.time())
    monkeypatch.setattr(live_mse, "time", NS(time=lambda: clock.now))
    subscribe = h.registry.subscribe

    async def subscribe_with_due_wakeup(*args):
        item, peer = await subscribe(*args)
        receive = peer.receive
        calls = 0

        async def due_receive(timeout=15):
            nonlocal calls
            calls += 1
            if calls == 2:
                h.user.is_active = False
                clock.now += 16
                raise asyncio.TimeoutError
            return await receive(timeout)

        peer.receive = due_receive
        return item, peer

    monkeypatch.setattr(h.registry, "subscribe", subscribe_with_due_wakeup)
    with h.client.websocket_connect("/live/7/sub/mse", headers=h.headers()) as ws:
        assert ws.receive_json()["type"] == "init"
        assert ws.receive_json() == {"type": "error", "code": "mse_access_denied"}
    assert h.registry.released == 1


def test_actual_source_eof_still_notifies_and_releases_the_viewer(monkeypatch):
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        proc = NS(stdout=reader, returncode=0)

        async def start_process(*args, **kwargs):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", start_process)
        item = MseStream(NS(id=7, rtsp_transport="tcp"), "sub", "rtsp://unit.invalid/sub", "unit")
        peer = item.subscribe()
        await item.run()
        assert item.failure == "mse_stream_failed"
        assert await peer.receive() == {"type": "error", "code": "mse_stream_failed"}
        assert peer.closed

    asyncio.run(run())
