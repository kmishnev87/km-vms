"""ONVIF Streaming backchannel (Require negotiation, TCP interleaved RTP)."""
import re
import secrets
import struct
from urllib.parse import urljoin, urlsplit

from .types import TalkCandidate, TalkError
from .wire import Wire, clean_uri, digest_header

REQUIRE = "www.onvif.org/ver20/backchannel"


def track_candidate(source, body, base):
    text = body.decode("utf-8", "replace")
    sections = re.split(r"(?m)^m=", text.replace("\r", ""))[1:]
    saw_audio = False
    for section in sections:
        lines = section.splitlines()
        media = lines[0].split()
        if len(media) < 4 or media[0] != "audio" or "a=sendonly" not in lines:
            continue
        saw_audio = True
        codecs = {"0": ("PCMU", 8000), "8": ("PCMA", 8000)}
        for line in lines:
            match = re.fullmatch(r"a=rtpmap:(\d+) ([A-Za-z0-9-]+)/(\d+)(?:/1)?", line)
            if match:
                codecs[match[1]] = (match[2].upper(), int(match[3]))
        for payload in media[3:]:
            codec, rate = codecs.get(payload, ("", 0))
            if codec not in {"PCMA", "PCMU"} or rate != 8000 or not payload.isdigit() or int(payload) > 127:
                continue
            control = next((x[10:] for x in lines if x.startswith("a=control:")), "")
            if not control or control == "*":
                continue
            target = control if "://" in control else urljoin(base.rstrip("/") + "/", control)
            parts = urlsplit(target)
            original = urlsplit(base)
            if parts.scheme != original.scheme or parts.hostname != source.host or (parts.port or 554) != source.port:
                raise TalkError("invalid_camera_response")
            if parts.username or parts.password or any(ord(c) < 32 for c in target):
                raise TalkError("invalid_camera_response")
            return TalkCandidate("onvif", source, codec, int(payload), target, base)
    raise TalkError("codec_unsupported" if saw_audio else "backchannel_unsupported")


class RtspTalk:
    def __init__(self, source):
        self.source = source
        self.wire = Wire(source.host, source.port, secure=urlsplit(source.uri).scheme == "rtsps")
        self.challenge = None
        self.auth_count = 0
        self.auth_cnonce = secrets.token_hex(12)
        self.sequence = 0
        self.session = None
        self.channel = 0
        self.rtp_sequence = secrets.randbelow(65536)
        self.timestamp = secrets.randbits(32)
        self.ssrc = secrets.randbits(32)
        self.candidate = None

    def request(self, method, target, extra=None):
        for attempt in range(2):
            self.sequence += 1
            headers = {"CSeq": str(self.sequence), "Require": REQUIRE, "User-Agent": "KM-VMS Camera Call"}
            headers.update(extra or {})
            if self.session:
                headers["Session"] = self.session
            if self.challenge:
                self.auth_count += 1
                headers["Authorization"] = digest_header(self.challenge, method, target, self.source.username, self.source.password,
                                                         nonce_count=self.auth_count, cnonce=self.auth_cnonce)
            raw = f"{method} {target} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            self.wire.send(raw.encode())
            status, response, body = self.wire.response("RTSP/1.0")
            if status == 401 and attempt == 0 and "www-authenticate" in response:
                if self.challenge != response["www-authenticate"]:
                    self.auth_count = 0
                    self.auth_cnonce = secrets.token_hex(12)
                self.challenge = response["www-authenticate"]
                continue
            if status in {401, 403}:
                raise TalkError("camera_authentication_failed")
            if status == 551:
                raise TalkError("backchannel_unsupported")
            if status not in range(200, 300):
                raise TalkError("camera_rejected_call")
            return response, body
        raise TalkError("camera_authentication_failed")

    def describe(self):
        uri = clean_uri(self.source.uri)
        headers, body = self.request("DESCRIBE", uri, {"Accept": "application/sdp"})
        base = headers.get("content-base", uri)
        # Never follow arbitrary Content-Base hosts / ports from a camera.
        if urlsplit(base).hostname != self.source.host or (urlsplit(base).port or 554) != self.source.port:
            raise TalkError("invalid_camera_response")
        self.candidate = track_candidate(self.source, body, base)
        return self.candidate

    def start(self, candidate):
        # Negotiate/authenticate the new RTSP connection too, not just its cached probe.
        current = self.describe()
        if (current.codec, current.control) != (candidate.codec, candidate.control):
            raise TalkError("camera_configuration_changed")
        self.candidate = candidate
        headers, _ = self.request("SETUP", candidate.control, {"Transport": 'RTP/AVP/TCP;unicast;interleaved=0-1;mode="PLAY"'})
        session = headers.get("session", "").split(";", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session):
            raise TalkError("invalid_camera_response")
        self.session = session
        transport = headers.get("transport", "")
        match = re.search(r"interleaved=(\d+)-(\d+)", transport, re.I)
        if "RTP/AVP/TCP" not in transport.upper() or not match or int(match[1]) > 254:
            raise TalkError("transport_unsupported")
        self.channel = int(match[1])
        self.request("PLAY", candidate.aggregate, {"Range": "npt=0.000-"})

    def send_audio(self, payload):
        packet = struct.pack("!BBHII", 0x80, self.candidate.payload, self.rtp_sequence, self.timestamp, self.ssrc) + payload
        self.wire.send(struct.pack("!BBH", 36, self.channel, len(packet)) + packet)
        self.rtp_sequence = (self.rtp_sequence + 1) & 0xffff
        self.timestamp = (self.timestamp + len(payload)) & 0xffffffff

    def close(self):
        if self.session and self.candidate:
            try:
                self.wire.sock.settimeout(0.5)
                self.request("TEARDOWN", self.candidate.aggregate)
            except Exception:
                pass
        self.wire.close()

    def keepalive(self):
        self.request("GET_PARAMETER", self.candidate.aggregate)
