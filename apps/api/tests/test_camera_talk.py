import asyncio
import dataclasses
import hashlib
import socket
import struct
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.permissions import user_has_permission
from app.services.camera_talk.types import TalkCandidate, TalkError, TalkSource
from app.services.camera_talk.rtsp import RtspTalk, track_candidate
from app.services.camera_talk.service import TalkManager, TalkSession
from app.services.camera_talk.tapo import AudioTransportStream, crc_mpeg
from app.services.camera_talk.wire import MAX_HEADER, Wire, clean_uri, digest_header


def source(**kwargs):
    return TalkSource(**{"camera_id": 1, "host": "127.0.0.1", "port": 554, "username": "example-user",
                        "password": "example-secret", "uri": "rtsp://127.0.0.1:554/main", **kwargs})


def sdp(control="track3", codec="PCMA/8000", direction="sendonly"):
    return f"v=0\r\nm=video 0 RTP/AVP 96\r\na=recvonly\r\nm=audio 0 RTP/AVP 8\r\na=rtpmap:8 {codec}\r\na=control:{control}\r\na={direction}\r\n".encode()


def test_talk_is_separate_from_live_and_has_no_viewer_or_operator_grant():
    for role in ("owner", "admin"):
        assert user_has_permission(role, "talk_cameras")
    for role in ("operator", "viewer", "unknown"):
        assert not user_has_permission(role, "talk_cameras")


def test_source_repr_and_public_capability_never_contain_secrets_or_stream_urls():
    value = source(talk_password="separate-secret")
    public = str(TalkCandidate("tapo", value).public()) + repr(value)
    assert all(secret not in public for secret in (value.password, value.talk_password, "rtsp://", value.username))
    assert source().signature != value.signature
    assert clean_uri("rtsp://user:secret@127.0.0.1:554/main#x") == "rtsp://127.0.0.1:554/main"


@pytest.mark.parametrize("direction,codec,reason", [("recvonly", "PCMA/8000", "backchannel_unsupported"),
                                                    ("sendonly", "AAC/16000", "codec_unsupported")])
def test_readonly_sdp_rejects_non_backchannel_or_unsupported_codecs(direction, codec, reason):
    with pytest.raises(TalkError, match=reason):
        track_candidate(source(), sdp(codec=codec, direction=direction), source().uri)


@pytest.mark.parametrize("target", ["rtsp://192.0.2.5/track", "rtsp://127.0.0.1:8000/track", "rtsp://user:pass@127.0.0.1:554/track"])
def test_camera_sdp_cannot_redirect_credentials_or_audio_to_another_endpoint(target):
    with pytest.raises(TalkError, match="invalid_camera_response"):
        track_candidate(source(), sdp(control=target), source().uri)


def test_digest_rfc2069_auth_and_sha256_variants_are_bounded():
    for algorithm in ("MD5", "MD5-sess", "SHA-256", "SHA-256-sess"):
        header = digest_header(f'Digest realm="test", nonce="nonce", qop="auth", algorithm={algorithm}', "DESCRIBE", "/test", "user", "secret")
        assert header.startswith("Digest ") and "response=" in header and "secret" not in header
    with pytest.raises(TalkError):
        digest_header('Digest realm="test", nonce="nonce", qop="auth-int"', "GET", "/", "user", "secret")
    with pytest.raises(TalkError):
        digest_header('Digest realm="test", nonce="nonce"', "GET", "/", "injected\r\nHeader: bad", "secret")


def test_wire_rejects_oversized_header_and_invalid_lengths():
    left, right = socket.socketpair()
    wire = Wire.__new__(Wire); wire.sock = left; wire.buffer = bytearray(b"x" * (MAX_HEADER + 1))
    try:
        with pytest.raises(TalkError): wire.line()
        with pytest.raises(TalkError): wire.read(200_000)
    finally:
        left.close(); right.close()


def test_mpegts_has_valid_crc_packet_lengths_continuity_and_20ms_pts():
    mux = AudioTransportStream()
    header = mux.header()
    assert len(header) == 376 and header[0] == header[188] == 0x47
    # MPEG section CRC must reduce the whole section to zero.
    pat = bytes.fromhex("00b00d0001c100000001f000")
    assert crc_mpeg(pat + crc_mpeg(pat)) == b"\0\0\0\0"
    audio = mux.audio(b"\xd5" * 160)
    again = mux.audio(b"\xd5" * 160)
    assert len(audio) == 188 and len(again) == 188
    assert audio[3] & 15 == 0 and again[3] & 15 == 1
    assert mux.pts == 3600


def test_pcm_exact_20ms_frames_codecs_and_no_unbounded_catchup_queue():
    session = TalkSession("test", 1, TalkCandidate("onvif", source()))
    assert len(session.encode(bytes(640))) == 160
    with pytest.raises(TalkError, match="invalid_audio_frame"): session.encode(bytes(638))
    for _ in range(8): session.encode(bytes(640))
    with pytest.raises(TalkError, match="audio_queue_overflow"):
        for _ in range(10): session.encode(bytes(640))


def test_exclusive_physical_output_actor_binding_and_close_release():
    async def scenario():
        manager = TalkManager()
        async def capability(value): return TalkCandidate("onvif", value)
        manager.capability = capability
        first = await manager.reserve(source(), 1)
        with pytest.raises(TalkError, match="camera_busy"):
            await manager.reserve(source(camera_id=2), 2)
        with pytest.raises(TalkError, match="call_not_found"):
            manager.owned(first.id, 2)
        await manager.close(first.id)
        next_call = await manager.reserve(source(camera_id=2), 2)
        await manager.close(next_call.id)
        assert manager.sessions == {}
    asyncio.run(scenario())


def test_digest_nonce_count_increments_across_rtsp_control_requests(monkeypatch):
    from app.services.camera_talk import rtsp
    class Peer:
        def __init__(self):
            self.sent = []
            self.responses = iter([(401, {"www-authenticate": 'Digest realm="fixture", nonce="nonce", qop="auth"'}, b""),
                                   (200, {}, b""), (200, {}, b"")])
        def send(self, data): self.sent.append(data.decode())
        def response(self, protocol): return next(self.responses)
    peer = Peer()
    monkeypatch.setattr(rtsp, "Wire", lambda *_args, **_kw: peer)
    call = rtsp.RtspTalk(source())
    call.request("DESCRIBE", "rtsp://127.0.0.1/main")
    call.request("GET_PARAMETER", "rtsp://127.0.0.1/main")
    assert "nc=00000001" in peer.sent[1] and "nc=00000002" in peer.sent[2]
    assert f'cnonce="{call.auth_cnonce}"' in peer.sent[1] and f'cnonce="{call.auth_cnonce}"' in peer.sent[2]


def test_manual_rtsp_url_credentials_are_accepted_without_exposing_them():
    from types import SimpleNamespace
    camera = SimpleNamespace(id=1, deleted_at=None, rtsp_main_url="rtsp://user%40cam:p%3Ass@192.0.2.1/main", rtsp_sub_url=None,
                             username=None, password_encrypted=None, talk_password_encrypted=None, port=80,
                             onvif_profile_token=None, onvif_path=None, protocol="rtsp")
    value = TalkSource.from_camera(camera)
    assert value.username == "user@cam" and value.password == "p:ss"
    assert "p:ss" not in repr(value) and "p%3Ass" not in repr(value)


def test_real_tcp_rtsp_digest_setup_play_rtp_and_teardown_without_video_subscription():
    listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(2)
    port = listener.getsockname()[1]
    value = source(port=port, uri=f"rtsp://127.0.0.1:{port}/main")
    failures, received, methods = [], [], []
    def camera():
        try:
            for connection in range(2):
                sock, _ = listener.accept(); sock.settimeout(4)
                stream = sock.makefile("rb")
                with sock, stream:
                    while True:
                        first = stream.read(1)
                        if not first: break
                        if first == b"$":
                            channel = stream.read(1)[0]; length = int.from_bytes(stream.read(2), "big")
                            received.append((channel, stream.read(length))); continue
                        line = first + stream.readline(); method = line.split()[0].decode(); methods.append(method)
                        headers = {}
                        while True:
                            header = stream.readline()
                            if header == b"\r\n": break
                            key, val = header.decode().split(":", 1); headers[key.lower()] = val.strip()
                        assert headers["require"] == "www.onvif.org/ver20/backchannel"
                        seq = headers["cseq"]
                        if "authorization" not in headers:
                            sock.sendall(f'RTSP/1.0 401 Unauthorized\r\nCSeq: {seq}\r\nWWW-Authenticate: Digest realm="test", nonce="nonce"\r\nContent-Length: 0\r\n\r\n'.encode()); continue
                        if method == "DESCRIBE":
                            body = sdp(); extra = f"Content-Base: {value.uri}/\r\n"
                        elif method == "SETUP": body = b""; extra = "Session: session123;timeout=60\r\nTransport: RTP/AVP/TCP;unicast;interleaved=2-3\r\n"
                        else: body = b""; extra = ""
                        sock.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {seq}\r\n{extra}Content-Length: {len(body)}\r\n\r\n".encode() + body)
                        if method == "TEARDOWN": break
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            listener.close()
    thread = threading.Thread(target=camera, daemon=True); thread.start()
    probe = RtspTalk(value)
    candidate = probe.describe(); probe.close()
    call = RtspTalk(value)
    try:
        call.start(candidate); call.send_audio(bytes(160))
    finally:
        call.close()
    thread.join(5)
    assert not thread.is_alive() and not failures
    assert received[0][0] == 2 and len(received[0][1]) == 172 and received[0][1][1] == 8
    assert "PLAY" in methods and methods[-1] == "TEARDOWN" and "RECORD" not in methods
