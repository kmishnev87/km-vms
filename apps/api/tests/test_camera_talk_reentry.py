"""Rejected talk sign-in must be recoverable, secret-free and non-destructive."""
import asyncio
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException

from app.core.security import encrypt_text, decrypt_text
from app.routers import talk
from app.services.camera_talk import tapo
from app.services.camera_talk.service import TalkManager
from app.services.camera_talk.types import TalkCandidate, TalkSource, TalkError


@pytest.fixture
def store(monkeypatch):
    camera = NS(id=7, deleted_at=None, rtsp_main_url="rtsp://192.0.2.7/main", rtsp_sub_url=None,
                username="camera-user", password_encrypted=encrypt_text("rtsp-password"),
                talk_password_encrypted=encrypt_text("previous-account-password"), port=80,
                onvif_profile_token=None, onvif_path=None, protocol="rtsp")
    class Db:
        commits = 0
        def get(self, *_): return camera
        def rollback(self): pass
        def expire_all(self): pass
        def query(self, *_): return self
        def filter(self, *_): return self
        def with_for_update(self): return self
        def first(self): return camera
        def commit(self): self.commits += 1
    db = Db()
    monkeypatch.setattr(talk, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(talk, "manager", TalkManager())
    return NS(camera=camera, db=db, original=TalkSource.from_camera(camera))


def test_password_is_verified_before_persisting_without_starting_a_call(store, monkeypatch):
    seen = []
    def verify(source):
        assert decrypt_text(store.camera.talk_password_encrypted) == "previous-account-password"
        assert store.db.commits == 0
        seen.append(source.talk_password)
    monkeypatch.setattr(talk, "verify_credential", verify)
    result = asyncio.run(talk.save_credential(7, talk.TalkCredential(password="new-account-password"), user=NS(id=1), db=store.db))
    assert result == {"saved": True} and seen == ["new-account-password"]
    assert decrypt_text(store.camera.talk_password_encrypted) == "new-account-password"
    assert store.db.commits == 1


@pytest.mark.parametrize("reason", ["talk_authentication_failed", "talk_access_denied", "camera_timeout"])
def test_rejected_or_unreachable_camera_never_overwrites_saved_credentials(store, monkeypatch, reason):
    def reject(_): raise TalkError(reason)
    monkeypatch.setattr(talk, "verify_credential", reject)
    with pytest.raises(HTTPException) as error:
        asyncio.run(talk.save_credential(7, talk.TalkCredential(password="unverified-password"), user=NS(id=1), db=store.db))
    assert error.value.detail == reason
    assert store.db.commits == 0
    assert decrypt_text(store.camera.talk_password_encrypted) == "previous-account-password"
    assert "unverified-password" not in str(error.value)


def test_password_clear_does_not_require_a_reachable_camera(store, monkeypatch):
    def forbidden(_): pytest.fail("clear must not contact camera")
    monkeypatch.setattr(talk, "verify_credential", forbidden)
    assert asyncio.run(talk.save_credential(7, talk.TalkCredential(password=None), user=NS(id=1), db=store.db)) == {"saved": False}
    assert store.camera.talk_password_encrypted is None


def test_concurrent_configuration_change_is_not_overwritten_after_probe(store, monkeypatch):
    def changed(_): store.camera.talk_password_encrypted = encrypt_text("concurrent-password")
    monkeypatch.setattr(talk, "verify_credential", changed)
    with pytest.raises(HTTPException) as error:
        asyncio.run(talk.save_credential(7, talk.TalkCredential(password="new-password"), user=NS(id=1), db=store.db))
    assert error.value.detail == "camera_configuration_changed"
    assert decrypt_text(store.camera.talk_password_encrypted) == "concurrent-password"
    assert store.db.commits == 0


@pytest.mark.parametrize("changed", [False, True])
def test_legacy_bad_password_is_discarded_but_concurrent_replacement_survives(store, changed):
    session = NS(candidate=TalkCandidate("tapo", store.original))
    if changed: store.camera.talk_password_encrypted = encrypt_text("concurrent-password")
    talk.forget_rejected_credential(session)
    if changed:
        assert decrypt_text(store.camera.talk_password_encrypted) == "concurrent-password"
        assert store.db.commits == 0
    else:
        assert store.camera.talk_password_encrypted is None and store.db.commits == 1
        assert TalkCandidate("tapo", TalkSource.from_camera(store.camera)).public()["status"] == "needs_credentials"


@pytest.mark.parametrize("status,reason", [(200, None), (401, "talk_authentication_failed"), (403, "talk_access_denied")])
def test_verify_only_authenticates_never_opens_talk_or_sends_audio(monkeypatch, status, reason):
    class Peer:
        closed = False
        sent = []
        responses = iter([(401, {"www-authenticate": 'Digest realm="TP-Link IP-Camera", nonce="fixture", encrypt_type="3"'}, b""),
                          (status, {}, b"")])
        def send(self, data): self.sent.append(data)
        def response(self, _): return next(self.responses)
        def close(self): self.closed = True
    peer = Peer()
    monkeypatch.setattr(tapo, "Wire", lambda *_args, **_kw: peer)
    source = TalkSource(7, "192.0.2.7", 554, "rtsp-user", "rtsp-password", "rtsp://192.0.2.7/main", talk_password="account-password")
    if reason:
        with pytest.raises(TalkError, match=reason): tapo.verify_credential(source)
    else:
        tapo.verify_credential(source)
    assert peer.closed and len(peer.sent) == 2
    assert all(frame.endswith(b"\r\n\r\n") and b"Content-Length: 0" in frame for frame in peer.sent)
    assert all(b'"talk"' not in frame and b"account-password" not in frame for frame in peer.sent)
