"""A real local TCP peer, not the user's camera, verifies the Tapo wire protocol."""
import hashlib
import json
import socket
import threading

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.services.camera_talk import tapo
from app.services.camera_talk.types import TalkCandidate, TalkSource, TalkError
from app.services.camera_talk.wire import Wire, digest_header


@pytest.mark.parametrize("encrypt_type", ["1", "3"])
@pytest.mark.parametrize("http_version", ["HTTP/1.0", "HTTP/1.1"])
def test_digest_hash_variants_encrypted_ack_and_mpegts_talk(encrypt_type, http_version, monkeypatch):
    listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1)
    port = listener.getsockname()[1]
    secret = "synthetic-account-password"
    hashed = (hashlib.sha256 if encrypt_type == "3" else hashlib.md5)(secret.encode()).hexdigest().upper()
    received, failures = [], []
    challenge = f'Digest realm="TP-Link IP-Camera", nonce="fixture-nonce", encrypt_type="{encrypt_type}"'
    def peer():
        try:
            sock, _ = listener.accept(); sock.settimeout(4)
            stream = sock.makefile("rb")
            with sock, stream:
                def headers():
                    result = {}
                    while True:
                        line = stream.readline().decode().strip()
                        if not line: return result
                        key, value = line.split(":", 1); result[key.lower()] = value.strip()
                assert stream.readline().startswith(b"POST /stream HTTP/1.1")
                assert "authorization" not in headers()
                sock.sendall(f"{http_version} 401 Unauthorized\r\nWWW-Authenticate: {challenge}\r\nContent-Length: 0\r\n\r\n".encode())
                assert stream.readline().startswith(b"POST /stream HTTP/1.1")
                auth = headers()["authorization"]
                assert auth == digest_header(challenge, "POST", "/stream", "admin", hashed)
                assert secret not in auth
                sock.sendall((http_version + ' 200 OK\r\nContent-Type: multipart/mixed; boundary=--device-stream-boundary--\r\nKey-Exchange: nonce="fixture-nonce"\r\n\r\n').encode())
                for index in range(3):
                    while not (line := stream.readline()).strip(): pass
                    assert line.strip() == b"----client-stream-boundary--"
                    fields = headers(); body = stream.read(int(fields["content-length"]))
                    if index == 0:
                        assert json.loads(body)["params"]["talk"]["mode"] == "aec"
                        payload = b'{"params":{"session_id":"fixture-session"}}'
                        pad = 16 - len(payload) % 16; payload += bytes((pad,)) * pad
                        key = hashlib.md5(f"fixture-nonce:{hashed}".encode()).digest()
                        iv = hashlib.md5(b"admin:fixture-nonce").digest()
                        cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
                        payload = cipher.update(payload) + cipher.finalize()
                        sock.sendall(f"----device-stream-boundary--\r\nContent-Type: application/json\r\nX-If-Encrypt: 1\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload + b"\r\n")
                    else:
                        assert fields["x-session-id"] == "fixture-session" and fields["x-if-encrypt"] == "0"
                        assert fields["content-type"] == "audio/mp2t"
                        received.append(body)
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            listener.close()
    thread = threading.Thread(target=peer, daemon=True); thread.start()
    original = Wire
    monkeypatch.setattr(tapo, "Wire", lambda host, requested, **kw: original(host, port, **kw))
    source = TalkSource(1, "127.0.0.1", 554, "camera-user", "camera-secret", "rtsp://127.0.0.1/main", talk_password=secret)
    call = tapo.TapoTalk(source)
    try:
        call.start(TalkCandidate("tapo", source)); call.send_audio(b"\xd5" * 160)
    finally:
        call.close()
    thread.join(5)
    assert not thread.is_alive() and not failures
    assert [len(value) for value in received] == [376, 188]


def test_tapo_never_attempts_unauthenticated_bypass_when_no_secret_is_saved():
    source = TalkSource(1, "192.0.2.1", 554, "camera-user", "camera-secret", "rtsp://192.0.2.1/main")
    with pytest.raises(TalkError, match="talk_credentials_required"):
        tapo.TapoTalk(source).start(TalkCandidate("tapo", source))


def test_tapo_signature_probe_accepts_http10_without_starting_a_call(monkeypatch):
    listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1)
    port = listener.getsockname()[1]
    def peer():
        with listener:
            sock, _ = listener.accept()
            with sock:
                sock.recv(4096)
                sock.sendall(b'HTTP/1.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="TP-Link IP-Camera", nonce="fixture"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    thread = threading.Thread(target=peer, daemon=True); thread.start()
    original = Wire
    monkeypatch.setattr(tapo, "Wire", lambda host, requested, **kw: original(host, port, **kw))
    source = TalkSource(1, "127.0.0.1", 554, "camera", "secret", "rtsp://127.0.0.1/main")
    result = tapo.probe(source)
    assert result.public()["status"] == "needs_credentials"
    thread.join(5)
    assert not thread.is_alive()
