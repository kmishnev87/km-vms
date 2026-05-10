from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.sanitization import redact_text
from app.db.session import SessionLocal
from app.models.audit_event import AuditEvent
from app.services.timezone_contract import (
    TimezoneContext,
    format_system_iso,
    format_storage_utc_iso,
    timezone_context,
    utc_now_storage,
)

logger = logging.getLogger(__name__)

CATEGORIES = {
    "auth",
    "users",
    "settings",
    "cameras",
    "live",
    "records",
    "chronology",
    "archive",
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
    r"(password|password_hash|secret|token|authorization|jwt|encryption_key|key|credential|cookie|raw_body|request_body)",
    re.IGNORECASE,
)


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
            created_at=utc_now_storage(),
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


def serialize_event(event: AuditEvent, ctx: TimezoneContext | None = None) -> dict:
    ctx = ctx or timezone_context(None)
    return {
        "id": event.id,
        "created_at": format_storage_utc_iso(event.created_at),
        "created_at_utc": format_storage_utc_iso(event.created_at),
        "created_at_system": format_system_iso(event.created_at, ctx),
        "system_timezone": ctx.name,
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


def audit_summary(events: list[AuditEvent], ctx: TimezoneContext | None = None) -> dict:
    ctx = ctx or timezone_context(None)
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    highlighted = []
    for event in events:
        by_category[event.category or "system"] = by_category.get(event.category or "system", 0) + 1
        by_severity[event.severity or "info"] = by_severity.get(event.severity or "info", 0) + 1
        if event.category == "security" or event.severity in {"warning", "error", "security"}:
            highlighted.append(
                {
                    "created_at": format_storage_utc_iso(event.created_at),
                    "created_at_utc": format_storage_utc_iso(event.created_at),
                    "created_at_system": format_system_iso(event.created_at, ctx),
                    "category": event.category,
                    "severity": event.severity,
                    "event_type": event.event_type,
                    "message": redact_text(event.message_en or event.message_ru or ""),
                    "target_type": event.target_type,
                    "target_id": event.target_id,
                    "target_name": redact_text(event.target_name) if event.target_name else None,
                }
            )
    return {
        "total": len(events),
        "by_category": dict(sorted(by_category.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "recent_security_warning_error": highlighted[:50],
        "timezone": {
            "system_timezone": ctx.name,
            "fallback_used": ctx.fallback_used,
            "operator_facing_fields": ["created_at_system"],
            "canonical_fields": ["created_at", "created_at_utc"],
        },
        "redaction": {
            "metadata_sanitized": True,
            "text_redaction": True,
            "raw_tokens_included": False,
            "raw_authorization_headers_included": False,
            "raw_cookies_included": False,
            "raw_rtsp_credentials_included": False,
        },
    }


def list_events(
    db: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    since_minutes: int | None = None,
    q: str | None = None,
) -> list[AuditEvent]:
    limit = max(1, min(int(limit or 50), 2000))
    offset = max(0, int(offset or 0))
    query = db.query(AuditEvent)
    if category:
        query = query.filter(AuditEvent.category == category)
    if severity:
        query = query.filter(AuditEvent.severity == severity)
    if event_type:
        query = query.filter(AuditEvent.event_type == event_type)
    if actor:
        actor_value = f"%{redact_text(actor).strip()[:120]}%"
        query = query.filter(
            or_(
                AuditEvent.actor_username.ilike(actor_value),
                AuditEvent.actor_role.ilike(actor_value),
            )
        )
    if target:
        target_value = f"%{redact_text(target).strip()[:120]}%"
        query = query.filter(
            or_(
                AuditEvent.target_type.ilike(target_value),
                AuditEvent.target_id.ilike(target_value),
                AuditEvent.target_name.ilike(target_value),
            )
        )
    if target_type:
        query = query.filter(AuditEvent.target_type == target_type)
    if target_id:
        query = query.filter(AuditEvent.target_id == str(target_id))
    if date_from:
        query = query.filter(AuditEvent.created_at >= date_from)
    if date_to:
        query = query.filter(AuditEvent.created_at <= date_to)
    if since_minutes is not None:
        query = query.filter(AuditEvent.created_at >= utc_now_storage() - timedelta(minutes=since_minutes))
    if q:
        q_value = f"%{redact_text(q).strip()[:120]}%"
        query = query.filter(
            or_(
                AuditEvent.event_type.ilike(q_value),
                AuditEvent.message_ru.ilike(q_value),
                AuditEvent.message_en.ilike(q_value),
                AuditEvent.actor_username.ilike(q_value),
                AuditEvent.target_type.ilike(q_value),
                AuditEvent.target_id.ilike(q_value),
                AuditEvent.target_name.ilike(q_value),
            )
        )
    return query.order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit).all()


def events_as_text(events: list[AuditEvent], ctx: TimezoneContext | None = None) -> str:
    ctx = ctx or timezone_context(None)
    lines = []
    for event in events:
        created = format_system_iso(event.created_at, ctx) or ""
        actor = event.actor_username or "system"
        lines.append(f"{created} | {event.category} | {event.severity} | {actor} | {event.message_ru}")
    return "\n".join(lines) + ("\n" if lines else "")
