"""Advisory, persistent stream dimensions. No camera I/O and no credentials on disk."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from app.core.config import settings


def dimensions(value):
    if not isinstance(value, dict):
        return None
    for key in ("current", "video", "settings"):
        if isinstance(value.get(key), dict):
            found = dimensions(value[key])
            if found:
                return found
    width, height = value.get("width"), value.get("height")
    if width is None or height is None:
        match = re.fullmatch(r"\s*(\d{1,5})[xX×](\d{1,5})\s*", str(value.get("resolution") or ""))
        if not match:
            return None
        width, height = match.groups()
    try:
        if isinstance(width, bool) or isinstance(height, bool):
            return None
        width, height = float(width), float(height)
        if not all(math.isfinite(v) and v.is_integer() and 1 <= v <= 65536 for v in (width, height)):
            return None
        return {"width": int(width), "height": int(height)}
    except (TypeError, ValueError, OverflowError):
        return None


def _identity(camera, role):
    if role not in ("main", "sub") or not getattr(camera, f"rtsp_{role}_url", None):
        return None
    # No name, recording settings or online state: those do not change the stream.
    keys = ("protocol", "host", "port", "username", "password_encrypted", "rtsp_host", "rtsp_port", "rtsp_transport",
            "onvif_path", "onvif_channel_id", f"rtsp_{role}_url",
            "onvif_profile_token" if role == "main" else "onvif_sub_profile_token")
    value = [str(getattr(camera, key, None) or "") for key in keys]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _path(camera, role):
    identifier = int(camera.id)
    if identifier <= 0 or role not in ("main", "sub"):
        raise ValueError("Invalid stream identity")
    return Path(settings.storage_previews) / ".camera-stream-dimensions" / f"{identifier}-{role}.json"


def read_dimensions(camera):
    result = {}
    for role in ("main", "sub"):
        try:
            identity = _identity(camera, role)
            path = _path(camera, role)
            if not identity or path.parent.is_symlink() or path.is_symlink() or path.stat().st_size > 2048:
                continue
            saved = json.loads(path.read_text(encoding="utf8"))
            if saved.get("identity") == identity:
                found = dimensions(saved)
                if found:
                    result[role] = found
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return result


def remember_dimensions(camera, role, value):
    """Best effort: a cache failure must never fail saving or playing a camera."""
    temporary = None
    try:
        size, identity = dimensions(value), _identity(camera, role)
        if not size or not identity:
            return False
        if read_dimensions(camera).get(role) == size:
            return True
        path = _path(camera, role)
        if path.parent.is_symlink():
            return False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # One independent file per role; concurrent Main/Sub cannot overwrite each other.
        fd, temporary = tempfile.mkstemp(prefix=f".{camera.id}-{role}-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf8") as stream:
            json.dump({"identity": identity, **size}, stream, separators=(",", ":"))
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def remember_onvif_dimensions(camera, credentials, profiles):
    """Ignore unsaved connection drafts and bind measurements to exact saved tokens."""
    if camera is None or str(getattr(camera, "protocol", "")).lower() != "onvif":
        return
    for key in ("host", "port", "username", "onvif_path"):
        if str(credentials.get(key) or "") != str(getattr(camera, key, None) or ""):
            return
    by_token = {str(p.get("token") or p.get("profile_token") or ""): p
                for p in profiles if isinstance(p, dict)}
    for role, key in (("main", "onvif_profile_token"), ("sub", "onvif_sub_profile_token")):
        token = str(getattr(camera, key, None) or "")
        if token and token in by_token:
            remember_dimensions(camera, role, by_token[token])
