"""Sliding 45-second repeat filter, independent of ONVIF property lifecycle.

Only a timestamp/hash on the last retained event is updated. Repeated starts do
not become history rows, unread markers, alert counters, or a hidden event list.
Property keys identify individual objects for endings, not a new alert type.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.models.audit_event import AuditEvent

REPEAT_SECONDS = 45


def signature(group, signal):
    objects = sorted(set(getattr(signal, "object_kinds", ()) or
                         filter(None, [getattr(signal, "object_kind", None)])))
    identity = [group["device_id"], signal.topic, signal.source, signal.kind,
                getattr(signal, "rule_kind", None), objects]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def attributes(key, received):
    return {"repeat_signature": key,
            "repeat_received_at": received.replace(tzinfo=timezone.utc).isoformat()}


def check(db, target_type, target_id, key, received):
    previous = db.query(AuditEvent).filter(
        AuditEvent.category == "camera_event", AuditEvent.target_type == target_type,
        AuditEvent.target_id == target_id,
        AuditEvent.event_metadata["repeat_signature"].as_string() == key,
    ).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).with_for_update().first()
    if previous is None:
        return False
    try:
        last = datetime.fromisoformat(previous.event_metadata["repeat_received_at"])
        if last.tzinfo is not None:
            last = last.astimezone(timezone.utc).replace(tzinfo=None)
        elapsed = (received - last).total_seconds()
    except (ValueError, TypeError, KeyError):
        return False
    if not 0 <= elapsed < REPEAT_SECONDS:
        return False
    # Advance from EVERY received matching start, including suppressed starts.
    # Do not change the retained event's physical start/end/last observation.
    previous.event_metadata = {**previous.event_metadata, **attributes(key, received)}
    db.flush()
    return True
