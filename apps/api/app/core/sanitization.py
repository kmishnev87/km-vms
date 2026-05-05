from __future__ import annotations

import re

RTSP_CREDENTIALS_RE = re.compile(r"(rtsp://[^:\s/@]+):([^@\s]+)@", re.IGNORECASE)
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
TOKEN_QUERY_RE = re.compile(r"([?&](?:token|access_token|refresh_token|media_token)=)[^&\s]+", re.IGNORECASE)
COOKIE_RE = re.compile(r"((?:Cookie|Set-Cookie):\s*)[^\r\n;]+", re.IGNORECASE)
POSTGRES_CREDENTIALS_RE = re.compile(r"(postgresql(?:\+\w+)?://[^:\s/@]+):([^@\s]+)@", re.IGNORECASE)


def redact_text(value: str | None) -> str:
    if value is None:
        return ""
    text = RTSP_CREDENTIALS_RE.sub(r"\1:***@", str(value))
    text = POSTGRES_CREDENTIALS_RE.sub(r"\1:***@", text)
    text = BEARER_RE.sub(r"\1***", text)
    text = TOKEN_QUERY_RE.sub(r"\1***", text)
    text = COOKIE_RE.sub(r"\1***", text)
    return text
