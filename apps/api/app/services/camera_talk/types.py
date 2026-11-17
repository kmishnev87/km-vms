from dataclasses import dataclass
import hashlib
import json
from urllib.parse import unquote, urlsplit

from app.core.security import decrypt_text


class TalkError(Exception):
    """Only this public reason code may cross the API boundary."""

    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, repr=False)
class TalkSource:
    camera_id: int
    host: str
    port: int
    username: str
    password: str
    uri: str
    talk_password: str | None = None
    onvif_port: int = 80
    profile: str | None = None
    onvif_path: str | None = None
    protocol: str = "rtsp"

    def __repr__(self):
        return f"TalkSource(camera_id={self.camera_id}, credentials=<redacted>)"

    @property
    def signature(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_camera(cls, camera):
        if camera.deleted_at is not None:
            raise TalkError("camera_unavailable", 404)
        uri = camera.rtsp_main_url or camera.rtsp_sub_url
        parts = urlsplit(uri or "")
        if parts.scheme not in {"rtsp", "rtsps"} or not parts.hostname:
            raise TalkError("stream_unavailable")
        return cls(camera.id, parts.hostname, parts.port or 554,
                   camera.username or unquote(parts.username or ""),
                   decrypt_text(camera.password_encrypted) if camera.password_encrypted else unquote(parts.password or ""),
                   uri, decrypt_text(camera.talk_password_encrypted) if camera.talk_password_encrypted else None,
                   int(camera.port or 80), camera.onvif_profile_token, camera.onvif_path, camera.protocol)


@dataclass(frozen=True)
class TalkCandidate:
    adapter: str
    source: TalkSource
    codec: str = "PCMA"
    payload: int = 8
    control: str = ""
    aggregate: str = ""

    @property
    def output_key(self):
        # Unknown physical output topology: serialize all lenses on the same host.
        return self.source.host.lower()

    def public(self):
        needs = self.adapter == "tapo" and not self.source.talk_password
        return {"status": "needs_credentials" if needs else "supported", "adapter": self.adapter,
                "duplex": "full" if self.adapter == "tapo" else "push_to_talk", "codec": self.codec,
                "credential_required": self.adapter == "tapo", "credential_saved": bool(self.source.talk_password)}
