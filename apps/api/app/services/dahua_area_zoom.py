"""Optional, profile-bound Dahua 3D positioning. No polling or RPC login session.

CGI HTTP API: ptzBase.moveDirectly, coordinates normalized to 0..8192.
The media service supplies the channel; opaque profile/node names never do.
"""
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import requests
from requests.auth import HTTPDigestAuth


class _Unavailable(Exception):
    """Unsupported CGI or credentials that are not valid for the native API."""


def _request(origin, username, password, endpoint, params):
    # Credentials never enter a URL, browser response, exception detail or log.
    with requests.Session() as session:
        session.trust_env = False
        session.auth = HTTPDigestAuth(username, password)
        with session.get(origin + endpoint, params=params, timeout=(3, 4),
                         allow_redirects=False, stream=True) as response:
            if response.status_code in (401, 403, 404, 405, 501):
                raise _Unavailable()
            if response.status_code != 200:
                raise ValueError("camera_cgi_failed")
            chunks, size = [], 0
            for chunk in response.iter_content(8192):
                size += len(chunk)
                if size > 65536:
                    raise ValueError("camera_cgi_response_too_large")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf8", errors="strict").strip()


@dataclass
class DahuaAreaZoom:
    origin: str
    channel: int
    username: str = field(repr=False)
    password: str = field(repr=False)

    def move(self, rectangle):
        from app.services.onvif_service import OnvifConfigurationError
        params = {"action": "moveDirectly", "channel": self.channel,
                  "startPoint[0]": round(rectangle["left"] * 8192),
                  "startPoint[1]": round(rectangle["top"] * 8192),
                  "endPoint[0]": round(rectangle["right"] * 8192),
                  "endPoint[1]": round(rectangle["bottom"] * 8192)}
        try:
            if params["startPoint[0]"] >= params["endPoint[0]"] or params["startPoint[1]"] >= params["endPoint[1]"]:
                raise ValueError("rectangle_rounding_empty")
            if _request(self.origin, self.username, self.password, "/cgi-bin/ptzBase.cgi", params) != "OK":
                raise ValueError("camera_rejected_area_zoom")
        except Exception:
            # A lost reply is not permission to replay a physical movement.
            raise OnvifConfigurationError("ptz_area_zoom_failed", "The camera did not confirm area positioning.") from None


def discover(cam, profile_token, host, port, username, password, onvif_path=None):
    """Read-only optional detection. Failure never disables ordinary ONVIF PTZ."""
    from app.services.onvif_service import _safe_attr
    try:
        device = getattr(cam, "devicemgmt", None)
        media = getattr(cam, "_km_ptz_media", None)
        if device is None or media is None:
            return None
        info = device.GetDeviceInformation()
        if str(_safe_attr(info, "Manufacturer", "")).strip().casefold() != "dahua":
            return None
        stream = media.GetStreamUri({"ProfileToken": profile_token,
            "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}})
        parsed = urlsplit(str(_safe_attr(stream, "Uri", "")))
        query = parse_qs(parsed.query)
        channels = query.get("channel", [])
        if parsed.path.lower().rstrip("/") != "/cam/realmonitor" or len(channels) != 1:
            return None
        if not channels[0].isascii() or not channels[0].isdigit() or not 1 <= int(channels[0]) <= 256:
            return None
        channel = int(channels[0])
        scheme = "https" if int(port) == 443 or str(onvif_path or "").lower().startswith("https://") else "http"
        authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
        origin = f"{scheme}://{authority}:{int(port)}"
        target = urlsplit(origin)
        if target.hostname != host.strip("[]").lower() or target.username or target.password or target.path:
            return None
        text = _request(origin, username, password, "/cgi-bin/ptz.cgi",
                        {"action": "getCurrentProtocolCaps", "channel": channel})
        caps = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        if caps.get("caps.Pan") != "true" or caps.get("caps.Zoom") != "true":
            return None
        if not any(key.startswith("caps.MoveDirectly.") for key in caps):
            return None
        return DahuaAreaZoom(origin, channel, username, password)
    except _Unavailable:
        # Never keep retrying a web login with an ONVIF-only camera account.
        return None
    except Exception:
        return False
