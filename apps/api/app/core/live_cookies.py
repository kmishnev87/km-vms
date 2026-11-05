"""Cookie authority is installation-specific (cookies themselves ignore ports)."""
import hashlib

from app.core.config import settings


def live_cookie_prefix() -> str:
    namespace = hashlib.sha256(("live-cookie:" + settings.jwt_secret).encode()).hexdigest()[:16]
    return f"kmvms_live_{namespace}"


def live_browser_cookie() -> str:
    return live_cookie_prefix() + "_browser"
