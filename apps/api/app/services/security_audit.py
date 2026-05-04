from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.services.audit_log import create_event, request_ip, request_user_agent

DENIAL_THROTTLE_SECONDS = 30
_DENIAL_THROTTLE: dict[tuple, datetime] = {}


def reset_security_audit_throttle() -> None:
    _DENIAL_THROTTLE.clear()


def _route_template(request) -> str | None:
    if request is None:
        return None
    route = (getattr(request, "scope", None) or {}).get("route")
    return getattr(route, "path", None) or getattr(request, "url", None) and getattr(request.url, "path", None)


def _method(request) -> str | None:
    return getattr(request, "method", None) if request is not None else None


def _usable_db(db: Session | None) -> bool:
    return db is not None and hasattr(db, "add") and hasattr(db, "commit")


def _should_emit(key: tuple, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    previous = _DENIAL_THROTTLE.get(key)
    if previous and now - previous < timedelta(seconds=DENIAL_THROTTLE_SECONDS):
        return False
    _DENIAL_THROTTLE[key] = now
    return True


def audit_security_denied(
    *,
    db: Session | None,
    actor=None,
    request=None,
    event_type: str,
    reason: str,
    status_code: int,
    required_permission: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not _usable_db(db):
        return
    route = _route_template(request)
    method = _method(request)
    actor_key = getattr(actor, "id", None) or getattr(actor, "username", None) or "anonymous"
    key = (event_type, actor_key, method, route, required_permission, reason)
    if not _should_emit(key):
        return
    safe_metadata = {
        "reason": reason,
        "status_code": status_code,
        "method": method,
        "route": route,
        "required_permission": required_permission,
        "throttle_seconds": DENIAL_THROTTLE_SECONDS,
    }
    safe_metadata.update(metadata or {})
    create_event(
        db=db,
        actor=actor,
        category="security",
        event_type=event_type,
        severity="security",
        message_ru=f"Security access denied: {reason}",
        message_en=f"Security access denied: {reason}",
        target_type="endpoint",
        target_id=route,
        metadata=safe_metadata,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
