from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.audit_event import AuditEvent

logger = logging.getLogger(__name__)

CATEGORIES = {
    "auth",
    "users",
    "settings",
    "cameras",
    "live",
    "records",
    "chronology",
    "security",
    "diagnostics",
    "system",
    "recorder",
    "storage",
    "retention",
    "reconciliation",
}
SEVERITIES = {"info", "warning", "error", "security"}
SENSITIVE_KEY_RE = re.compile(
    r"(password|password_hash|secret|token|authorization|jwt|encryption_key|key|credential|cookie)",
    re.IGNORECASE,
)
RTSP_CREDENTIALS_RE = re.compile(r"(rtsp://[^:\s/@]+):([^@\s]+)@", re.IGNORECASE)
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
TOKEN_QUERY_RE = re.compile(r"([?&](?:token|access_token|refresh_token|media_token)=)[^&\s]+", re.IGNORECASE)
COOKIE_RE = re.compile(r"((?:Cookie|Set-Cookie):\s*)[^\r\n;]+", re.IGNORECASE)
POSTGRES_CREDENTIALS_RE = re.compile(r"(postgresql(?:\+\w+)?://[^:\s/@]+):([^@\s]+)@", re.IGNORECASE)


def request_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:100]
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def request_user_agent(request) -> str | None:
    if request is None:
        return None
    value = request.headers.get("user-agent")
    return value[:1000] if value else None


def redact_text(value: str | None) -> str:
    if value is None:
        return ""
    text = RTSP_CREDENTIALS_RE.sub(r"\1:***@", str(value))
    text = POSTGRES_CREDENTIALS_RE.sub(r"\1:***@", text)
    text = BEARER_RE.sub(r"\1***", text)
    text = TOKEN_QUERY_RE.sub(r"\1***", text)
    text = COOKIE_RE.sub(r"\1***", text)
    return text


def sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text == "credential_changed":
                result[key_text] = sanitize_metadata(item)
            elif SENSITIVE_KEY_RE.search(key_text):
                result[key_text] = "***"
            else:
                result[key_text] = sanitize_metadata(item)
        return result
    if isinstance(value, list):
        return [sanitize_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_metadata(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


def _actor_fields(actor) -> dict:
    if actor is None:
        return {"actor_user_id": None, "actor_username": None, "actor_role": None}
    return {
        "actor_user_id": getattr(actor, "id", None),
        "actor_username": getattr(actor, "username", None),
        "actor_role": getattr(actor, "role", None),
    }


def create_event(
    *,
    db: Session | None = None,
    actor=None,
    category: str,
    event_type: str,
    severity: str = "info",
    message_ru: str,
    message_en: str = "",
    target_type: str | None = None,
    target_id: str | int | None = None,
    target_name: str | None = None,
    metadata: dict | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditEvent | None:
    category = category if category in CATEGORIES else "system"
    severity = severity if severity in SEVERITIES else "info"
    sanitized_metadata = sanitize_metadata(metadata or {})
    if event_type == "cameras.updated" and not sanitized_metadata.get("changed") and not sanitized_metadata.get("credential_changed"):
        return None
    owns_session = db is None
    session = db or SessionLocal()
    try:
        event = AuditEvent(
            id=str(uuid.uuid4()),
            created_at=datetime.utcnow(),
            **_actor_fields(actor),
            category=category,
            event_type=event_type,
            severity=severity,
            message_ru=redact_text(message_ru),
            message_en=redact_text(message_en or message_ru),
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            target_name=redact_text(target_name) if target_name else None,
            event_metadata=sanitized_metadata,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return event
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        logger.exception("Audit event write failed event_type=%s", event_type)
        return None
    finally:
        if owns_session:
            session.close()


def serialize_event(event: AuditEvent) -> dict:
    return {
        "id": event.id,
        "created_at": event.created_at.isoformat() + "Z" if event.created_at else None,
        "actor_user_id": event.actor_user_id,
        "actor_username": event.actor_username,
        "actor_role": event.actor_role,
        "category": event.category,
        "event_type": event.event_type,
        "severity": event.severity,
        "message_ru": event.message_ru,
        "message_en": event.message_en,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "target_name": event.target_name,
        "metadata": sanitize_metadata(event.event_metadata or {}),
        "ip_address": event.ip_address,
        "user_agent": event.user_agent,
    }


def list_events(
    db: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    severity: str | None = None,
    since_minutes: int | None = None,
) -> list[AuditEvent]:
    limit = max(1, min(int(limit or 50), 2000))
    offset = max(0, int(offset or 0))
    query = db.query(AuditEvent)
    if category:
        query = query.filter(AuditEvent.category == category)
    if severity:
        query = query.filter(AuditEvent.severity == severity)
    if since_minutes is not None:
        query = query.filter(AuditEvent.created_at >= datetime.utcnow() - timedelta(minutes=since_minutes))
    return query.order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit).all()


def events_as_text(events: list[AuditEvent]) -> str:
    lines = []
    for event in events:
        created = event.created_at.strftime("%Y-%m-%d %H:%M:%S") if event.created_at else ""
        actor = event.actor_username or "system"
        lines.append(f"{created} | {event.category} | {event.severity} | {actor} | {event.message_ru}")
    return "\n".join(lines) + ("\n" if lines else "")
