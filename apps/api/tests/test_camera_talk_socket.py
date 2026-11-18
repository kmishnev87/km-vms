import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.routers import talk
from app.services import media_tokens
from app.services.camera_talk.service import TalkManager, TalkSession
from app.services.camera_talk.types import TalkCandidate, TalkSource, TalkError


@pytest.fixture
def harness(monkeypatch):
    actor = NS(id=19, username="test-owner", role="owner", is_active=True)
    source = TalkSource(7, "192.0.2.1", 554, "test", "secret", "rtsp://192.0.2.1/main")
    users = {19: actor}
    class Db:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def query(self, _): return self
        def filter(self, condition): self.id = condition.right.value; return self
        def first(self): return users.get(self.id)
    monkeypatch.setattr(talk, "SessionLocal", Db)
    monkeypatch.setattr(talk, "source_for", lambda *_: source)
    monkeypatch.setattr(media_tokens, "audit_security_denied", lambda **_: None)
    manager = TalkManager()
    candidate = TalkCandidate("onvif", source)
    session = TalkSession("fixture-session-0123456789", 19, candidate)
    manager.sessions[session.id] = session
    frames = []
    class Transport:
        closed = False
        def send_audio(self, value): frames.append(value)
        def keepalive(self): pass
        def close(self): self.closed = True
    transport = Transport()
    async def start(item, socket):
        item.socket = socket; item.transport = transport
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(talk, "manager", manager)
    app = FastAPI(); app.include_router(talk.router)
    client = TestClient(app)
    def token(scope="talk", resource=None, user=None):
        return media_tokens.create_media_token(user=user or actor, scope=scope, resource=resource or talk.resource(session))[0]
    return NS(client=client, manager=manager, session=session, actor=actor, source=source, frames=frames, transport=transport,
              token=token, path=f"/talk/sessions/{session.id}/audio")


def test_native_socket_starts_relays_one_exact_frame_and_always_releases(harness):
    h = harness
    with h.client.websocket_connect(h.path, headers={"Authorization": "Bearer " + h.token()}) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(bytes(640))
        ws.send_json({"type": "authorization", "media_token": h.token()})
    # Socket teardown closes the blocking camera transport on a worker thread.
    # The TestClient context may return just before that thread finishes.
    deadline = time.monotonic() + 2
    while (not h.transport.closed or h.manager.sessions) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(h.frames) == 1 and len(h.frames[0]) == 160
    assert h.transport.closed and not h.manager.sessions


@pytest.mark.parametrize("case", ["missing", "wrong_scope", "wrong_camera", "wrong_user", "viewer", "inactive", "browser_origin"])
def test_unauthorized_socket_never_activates_speaker(harness, case):
    h = harness
    token = h.token()
    if case == "wrong_scope": token = h.token(scope="live")
    if case == "wrong_camera": token = h.token(resource={**talk.resource(h.session), "camera_id": 8})
    if case == "wrong_user": token = h.token(user=NS(id=20, username="other"))
    if case == "viewer": h.actor.role = "viewer"
    if case == "inactive": h.actor.is_active = False
    headers = {} if case == "missing" else {"Authorization": "Bearer " + token}
    if case == "browser_origin": headers["Origin"] = "http://other.invalid"
    with pytest.raises(WebSocketDisconnect):
        with h.client.websocket_connect(h.path, headers=headers): pass
    assert h.session.socket is None and not h.frames


def test_role_revocation_and_malformed_audio_close_active_call(harness):
    h = harness
    with h.client.websocket_connect(h.path, headers={"Authorization": "Bearer " + h.token()}) as ws:
        assert ws.receive_json()["type"] == "ready"
        h.actor.is_active = False
        ws.send_json({"type": "authorization", "media_token": h.token()})
        assert ws.receive_json()["type"] == "error"
    assert h.transport.closed and not h.manager.sessions


def test_wrong_audio_size_does_not_reach_camera(harness):
    h = harness
    with h.client.websocket_connect(h.path, headers={"Authorization": "Bearer " + h.token()}) as ws:
        ws.receive_json(); ws.send_bytes(bytes(6000))
        assert ws.receive_json() == {"type": "error", "reason": "invalid_audio_frame"}
    assert not h.frames and h.transport.closed


def test_authentication_failure_reaches_client_before_session_is_closed(harness, monkeypatch):
    h = harness
    async def fail_start(session, socket):
        session.socket = socket
        session.transport = h.transport
        raise TalkError("talk_authentication_failed")
    monkeypatch.setattr(h.manager, "start", fail_start)
    with h.client.websocket_connect(h.path, headers={"Authorization": "Bearer " + h.token()}) as ws:
        assert ws.receive_json() == {"type": "error", "reason": "talk_authentication_failed"}
    assert not h.frames and h.transport.closed and not h.manager.sessions


@pytest.mark.parametrize("reason,discarded", [("talk_authentication_failed", True), ("talk_access_denied", False), ("camera_timeout", False)])
def test_only_authentication_rejection_discards_the_legacy_tapo_secret_before_reply(harness, monkeypatch, reason, discarded):
    h = harness
    h.session.candidate = TalkCandidate("tapo", replace(h.source, talk_password="synthetic-account-password"))
    monkeypatch.setattr(talk, "source_for", lambda *_: h.session.candidate.source)
    cleared = []
    monkeypatch.setattr(talk, "forget_rejected_credential", lambda session: cleared.append(session.id))
    async def fail_start(session, socket):
        session.socket = socket
        session.transport = h.transport
        raise TalkError(reason)
    monkeypatch.setattr(h.manager, "start", fail_start)
    with h.client.websocket_connect(h.path, headers={"Authorization": "Bearer " + h.token()}) as ws:
        assert ws.receive_json() == {"type": "error", "reason": reason}
        assert bool(cleared) == discarded
    assert not h.frames


def test_server_heartbeat_keeps_upstream_alive_while_native_microphone_is_muted(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(talk, "SERVER_HEARTBEAT_SECONDS", 0)
    with h.client.websocket_connect(h.path, headers={"Authorization": "Bearer " + h.token()}) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "keepalive"})
        assert ws.receive_json() == {"type": "keepalive"}
    assert not h.frames and h.transport.closed
