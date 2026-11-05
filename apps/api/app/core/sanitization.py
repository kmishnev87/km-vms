from __future__ import annotations

import re
from urllib.parse import unquote_plus

SENSITIVE_QUERY_KEYS = frozenset({
    "access_token", "refresh_token", "media_token", "token", "auth", "authorization",
    "credential", "credentials", "key", "password", "passwd", "secret", "signature",
})
RTSP_CREDENTIALS_RE = re.compile(r"((?:rtsps?|https?)://)([^@\s/]+)@", re.IGNORECASE)
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
TOKEN_QUERY_RE = re.compile(r"([?&]([^=&\s#]+)=)[^&\s#\"'<>]*", re.IGNORECASE)
COOKIE_RE = re.compile(r"((?:Cookie|Set-Cookie):\s*)[^\r\n]+", re.IGNORECASE)
POSTGRES_CREDENTIALS_RE = re.compile(r"(postgresql(?:\+\w+)?://[^:\s/@]+):([^@\s]+)@", re.IGNORECASE)


def is_sensitive_query_key(key: str) -> bool:
    return unquote_plus(str(key)).casefold() in SENSITIVE_QUERY_KEYS


def _redact_query_parameter(match: re.Match) -> str:
    return match.group(1) + "***" if is_sensitive_query_key(match.group(2)) else match.group(0)


def redact_text(value: str | None) -> str:
    if value is None:
        return ""
    text = RTSP_CREDENTIALS_RE.sub(r"\1***@", str(value))
    text = POSTGRES_CREDENTIALS_RE.sub(r"\1:***@", text)
    text = BEARER_RE.sub(r"\1***", text)
    text = TOKEN_QUERY_RE.sub(_redact_query_parameter, text)
    text = COOKIE_RE.sub(r"\1***", text)
    return text
