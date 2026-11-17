"""Small bounded HTTP/RTSP framing; all failures remain credential-free."""
import base64
import hashlib
import secrets
import socket
import ssl
import time
from urllib.parse import urlsplit

from requests.utils import parse_dict_header

from .types import TalkError

MAX_HEADER = 16_384
MAX_BODY = 131_072


def digest_header(challenge, method, target, username, password, *, nonce_count=1, cnonce=None):
    def quoted(value):
        if len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise TalkError("invalid_authentication")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if challenge.lower().startswith("basic "):
        return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    if not challenge.lower().startswith("digest "):
        raise TalkError("authentication_unsupported")
    fields = parse_dict_header(challenge[7:])
    algorithm = fields.get("algorithm", "MD5").upper()
    if algorithm not in {"MD5", "MD5-SESS", "SHA-256", "SHA-256-SESS"}:
        raise TalkError("authentication_unsupported")
    hash_fn = hashlib.sha256 if algorithm.startswith("SHA-256") else hashlib.md5
    def digest(value):
        return hash_fn(value.encode()).hexdigest()
    realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
    if not nonce:
        raise TalkError("invalid_authentication")
    ha1 = digest(f"{username}:{realm}:{password}")
    cnonce = cnonce or secrets.token_hex(12)
    if not 1 <= nonce_count <= 0xffffffff:
        raise TalkError("invalid_authentication")
    count = f"{nonce_count:08x}"
    if algorithm.endswith("-SESS"):
        ha1 = digest(f"{ha1}:{nonce}:{cnonce}")
    ha2 = digest(f"{method}:{target}")
    qops = [x.strip() for x in fields.get("qop", "").split(",") if x.strip()]
    if qops and "auth" not in qops:
        raise TalkError("authentication_unsupported")
    response = digest(f"{ha1}:{nonce}:{count}:{cnonce}:auth:{ha2}") if qops else digest(f"{ha1}:{nonce}:{ha2}")
    result = [f"username={quoted(username)}", f"realm={quoted(realm)}", f"nonce={quoted(nonce)}",
              f"uri={quoted(target)}", f"response={quoted(response)}", f"algorithm={algorithm}"]
    if "opaque" in fields:
        result.append(f"opaque={quoted(fields['opaque'])}")
    if qops:
        result.extend(["qop=auth", f"nc={count}", f"cnonce={quoted(cnonce)}"])
    elif algorithm.endswith("-SESS"):
        result.append(f"cnonce={quoted(cnonce)}")
    return "Digest " + ", ".join(result)


def clean_uri(uri):
    parts = urlsplit(uri)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    return parts._replace(netloc=host, fragment="").geturl()


class Wire:
    def __init__(self, host, port, *, secure=False, timeout=5):
        self.sock = socket.create_connection((host, port), timeout)
        if secure:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=host)
        self.sock.settimeout(timeout)
        self.buffer = bytearray()
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout

    def receive(self, size):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TalkError("camera_timeout")
        self.sock.settimeout(min(self.timeout, remaining))
        return self.sock.recv(size)

    def read(self, length):
        if not 0 <= length <= MAX_BODY:
            raise TalkError("invalid_camera_response")
        while len(self.buffer) < length:
            chunk = self.receive(min(4096, length - len(self.buffer)))
            if not chunk:
                raise TalkError("camera_disconnected")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:length])
        del self.buffer[:length]
        return result

    def line(self):
        while True:
            end = self.buffer.find(b"\r\n")
            if end >= 0:
                if end > MAX_HEADER:
                    raise TalkError("invalid_camera_response")
                return self.read(end + 2)[:-2].decode("latin-1")
            if len(self.buffer) > MAX_HEADER:
                raise TalkError("invalid_camera_response")
            chunk = self.receive(4096)
            if not chunk:
                raise TalkError("camera_disconnected")
            self.buffer.extend(chunk)

    def headers(self):
        result, total = {}, 0
        while True:
            line = self.line()
            total += len(line) + 2
            if total > MAX_HEADER:
                raise TalkError("invalid_camera_response")
            if not line:
                return result
            key, sep, value = line.partition(":")
            key = key.lower().strip()
            if not sep or key in result:
                raise TalkError("invalid_camera_response")
            result[key] = value.strip()

    def response(self, protocol):
        self.deadline = time.monotonic() + self.timeout
        skipped = 0
        while protocol == "RTSP/1.0":
            if not self.buffer:
                chunk = self.receive(1)
                if not chunk:
                    raise TalkError("camera_disconnected")
                self.buffer.extend(chunk)
            if self.buffer[0] != 36:
                break
            header = self.read(4)
            self.read(int.from_bytes(header[2:4], "big"))
            skipped += 1
            if skipped > 16:
                raise TalkError("invalid_camera_response")
        status = self.line().split(" ", 2)
        # Embedded camera HTTP servers may legally answer an HTTP/1.1 request
        # with HTTP/1.0. This does not relax RTSP or authentication/framing.
        versions = {"HTTP/1.0", "HTTP/1.1"} if protocol == "HTTP/1.1" else {protocol}
        if len(status) < 2 or status[0] not in versions or not status[1].isdigit():
            raise TalkError("invalid_camera_response")
        headers = self.headers()
        if "transfer-encoding" in headers:
            raise TalkError("invalid_camera_response")
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            raise TalkError("invalid_camera_response") from None
        return int(status[1]), headers, self.read(length)

    def send(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
