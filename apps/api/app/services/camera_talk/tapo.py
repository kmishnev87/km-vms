"""Tapo local multipart talk protocol; authenticated only, no bypass credentials."""
import hashlib
import json
import re
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.utils import parse_dict_header

from .types import TalkCandidate, TalkError
from .wire import MAX_BODY, Wire, digest_header

CLIENT_BOUNDARY = "----client-stream-boundary--"


def crc_mpeg(data):
    value = 0xffffffff
    for byte in data:
        value ^= byte << 24
        for _ in range(8):
            value = ((value << 1) ^ (0x04c11db7 if value & 0x80000000 else 0)) & 0xffffffff
    return struct.pack("!I", value)


class AudioTransportStream:
    """One G.711 A-law elementary stream (Tapo stream type 0x90)."""

    def __init__(self):
        self.counters = {}
        self.pts = 0

    def packets(self, pid, data):
        output = bytearray()
        first = True
        while data:
            chunk, data = data[:184], data[184:]
            count = self.counters.get(pid, 0)
            self.counters[pid] = (count + 1) % 16
            prefix = bytes((0x47, ((pid >> 8) & 0x1f) | (0x40 if first else 0), pid & 255))
            if len(chunk) == 184:
                packet = prefix + bytes((0x10 | count,)) + chunk
            else:
                padding = 183 - len(chunk)
                adaptation = bytes((padding,)) + (b"\x00" + b"\xff" * (padding - 1) if padding else b"")
                packet = prefix + bytes((0x30 | count,)) + adaptation + chunk
            output.extend(packet)
            first = False
        return bytes(output)

    def header(self):
        pat = bytes.fromhex("00b00d0001c100000001f000")
        pmt = bytes.fromhex("02b0120001c10000fffff00090e100f000")
        return self.packets(0, b"\x00" + pat + crc_mpeg(pat)) + self.packets(0x1000, b"\x00" + pmt + crc_mpeg(pmt))

    def audio(self, payload):
        pts = self.pts & ((1 << 33) - 1)
        stamp = bytes((0x21 | ((pts >> 29) & 0xe), (pts >> 22) & 255,
                       ((pts >> 14) & 0xfe) | 1, (pts >> 7) & 255, ((pts << 1) & 0xfe) | 1))
        pes = b"\x00\x00\x01\xc0" + struct.pack("!H", len(payload) + 8) + b"\x80\x80\x05" + stamp + payload
        self.pts += len(payload) * 90000 // 8000
        return self.packets(0x100, pes)


def initial_request(wire, host, authorization=None):
    if any(ord(c) < 33 for c in host):
        raise TalkError("invalid_camera_address")
    headers = {"Host": host, "Content-Type": "multipart/mixed; boundary=--client-stream-boundary--", "Content-Length": "0"}
    if authorization:
        headers["Authorization"] = authorization
    request = "POST /stream HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    wire.send(request.encode())
    return wire.response("HTTP/1.1")


def probe(source):
    wire = Wire(source.host, 8800, timeout=2)
    try:
        status, headers, _ = initial_request(wire, source.host)
        challenge = headers.get("www-authenticate", "")
        if status != 401 or not challenge.startswith("Digest "):
            raise TalkError("backchannel_unsupported")
        realm = parse_dict_header(challenge[7:]).get("realm", "").lower()
        if not any(x in realm for x in ("tp-link", "tapo")):
            raise TalkError("backchannel_unsupported")
        return TalkCandidate("tapo", source)
    finally:
        wire.close()


class TapoTalk:
    def __init__(self, source):
        self.source = source
        self.wire = None
        self.session = ""
        self.mux = AudioTransportStream()

    def multipart(self, body, content_type, session=None):
        headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        if session:
            headers.update({"X-Session-Id": session, "X-If-Encrypt": "0"})
        envelope = CLIENT_BOUNDARY + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        self.wire.send(envelope.encode() + body + b"\r\n")

    def authenticate(self):
        """Check local credentials without opening a talk session or sending audio."""
        if not self.source.talk_password:
            raise TalkError("talk_credentials_required")
        self.wire = Wire(self.source.host, 8800)
        status, headers, _ = initial_request(self.wire, self.source.host)
        challenge = headers.get("www-authenticate", "")
        if status != 401 or not challenge.startswith("Digest "):
            raise TalkError("camera_rejected_call")
        fields = parse_dict_header(challenge[7:])
        if not any(brand in fields.get("realm", "").lower() for brand in ("tp-link", "tapo")):
            raise TalkError("backchannel_unsupported")
        hash_fn = hashlib.sha256 if fields.get("encrypt_type") == "3" else hashlib.md5
        secret = hash_fn(self.source.talk_password.encode()).hexdigest().upper()
        authorization = digest_header(challenge, "POST", "/stream", "admin", secret)
        if headers.get("connection", "").lower() == "close":
            self.wire.close()
            self.wire = Wire(self.source.host, 8800)
        status, headers, _ = initial_request(self.wire, self.source.host, authorization)
        if status == 401:
            raise TalkError("talk_authentication_failed")
        if status == 403:
            raise TalkError("talk_access_denied")
        if status != 200:
            raise TalkError("camera_rejected_call")
        return headers, secret

    def start(self, candidate):
        headers, secret = self.authenticate()
        exchange = parse_dict_header(headers.get("key-exchange", ""))
        self.multipart(json.dumps({"params": {"talk": {"mode": "aec"}, "method": "get"}, "seq": 3, "type": "request"}).encode(), "application/json")
        boundary = headers.get("content-type", "")
        match = re.search(r'boundary="?([^"; ]+)', boundary, re.I)
        marker = "--" + (match[1] if match else "--device-stream-boundary--")
        for _ in range(8):
            line = self.wire.line()
            if line == marker:
                break
        else:
            raise TalkError("invalid_camera_response")
        part = self.wire.headers()
        try:
            length = int(part.get("content-length", "0"))
        except ValueError:
            raise TalkError("invalid_camera_response") from None
        if not 0 < length <= MAX_BODY:
            raise TalkError("invalid_camera_response")
        body = self.wire.read(length)
        if part.get("x-if-encrypt") == "1":
            nonce = exchange.get("nonce", "")
            if not nonce or len(body) % 16:
                raise TalkError("invalid_camera_response")
            key = hashlib.md5(f"{nonce}:{secret}".encode()).digest()
            iv = hashlib.md5(f"admin:{nonce}".encode()).digest()
            decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            body = decryptor.update(body) + decryptor.finalize()
            padding = body[-1]
            if not 1 <= padding <= 16 or body[-padding:] != bytes((padding,)) * padding:
                raise TalkError("invalid_camera_response")
            body = body[:-padding]
        try:
            result = json.loads(body)
            self.session = result.get("params", {}).get("session_id", "")
        except (ValueError, AttributeError):
            raise TalkError("invalid_camera_response") from None
        if not isinstance(self.session, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.session):
            raise TalkError("camera_rejected_call")
        self.multipart(self.mux.header(), "audio/mp2t", self.session)

    def send_audio(self, payload):
        self.multipart(self.mux.audio(payload), "audio/mp2t", self.session)

    def close(self):
        if self.wire:
            self.wire.close()

    def keepalive(self):
        # Transport is held by the open authenticated multipart connection.
        pass


def verify_credential(source):
    transport = TapoTalk(source)
    try:
        transport.authenticate()
    finally:
        transport.close()
