from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import user_has_permission
from app.models.user import User
from app.routers.deps import FORBIDDEN_DETAIL

MEDIA_TOKEN_TYPE = "media"
MEDIA_TOKEN_EXPIRES_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_media_token(
    *,
    user: User,
    scope: str,
    resource: dict[str, Any],
    expires_seconds: int = MEDIA_TOKEN_EXPIRES_SECONDS,
) -> tuple[str, datetime]:
    now = _now()
    expires_at = now + timedelta(seconds=max(30, min(int(expires_seconds or MEDIA_TOKEN_EXPIRES_SECONDS), 600)))
    payload = {
        "typ": MEDIA_TOKEN_TYPE,
        "sub": user.username,
        "uid": getattr(user, "id", None),
        "scope": scope,
        "resource": resource,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, expires_at


def _invalid_token() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid media token")


def _resource_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, value in expected.items():
        if str(actual.get(key)) != str(value):
            return False
    return True


def validate_media_token(
    db: Session,
    *,
    token: str | None,
    scope: str,
    resource: dict[str, Any],
    permission: str,
) -> User:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Media token required")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Media token expired")
    except jwt.InvalidTokenError:
        raise _invalid_token()

    if payload.get("typ") != MEDIA_TOKEN_TYPE or payload.get("scope") != scope:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)
    if not _resource_matches(payload.get("resource") or {}, resource):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)

    username = payload.get("sub")
    user = db.query(User).filter(User.username == username).first() if username else None
    if not user or not getattr(user, "is_active", True):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user_has_permission(user.role, permission):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)
    return user


def media_token_response(token: str, expires_at: datetime) -> dict:
    return {
        "media_token": token,
        "token_type": "media",
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "expires_in": max(0, int((expires_at - _now()).total_seconds())),
    }
