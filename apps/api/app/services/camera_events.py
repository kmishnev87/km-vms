"""Camera-event domain over the existing indexed event envelope.

Physical persistence is shared, not the administrative journal or its API.
Only normalized episodes are stored; no XML, camera credentials or heartbeats.
This additive domain needs no DDL and preserves the published schema-v10 path.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from app.models.audit_event import AuditEvent
from app.models.workspace_layout import UserWorkspaceLayout
from app.services.onvif_events import KINDS
from app.services.timezone_contract import format_storage_utc_iso, format_system_iso, timezone_context

CATEGORY = "camera_event"
READ_CATEGORY = "camera_event_read"
PREFERENCE_KEY = "camera_events"
RETENTION_DAYS = 30
MAX_EVENTS = 100_000
MAX_PAGE = 100
_listeners = {}
_listeners_guard = threading.Lock()


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def notify_change():
    with _listeners_guard:
        listeners = list(_listeners.values())
    for _, loop, event in listeners:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass


def listen(user_id):
    with _listeners_guard:
        if sum(value[0] == user_id for value in _listeners.values()) >= 8 or len(_listeners) >= 256:
            return None
        key, event = uuid.uuid4().hex, asyncio.Event()
        _listeners[key] = (user_id, asyncio.get_running_loop(), event)
        return key, event


def unlisten(key):
    with _listeners_guard:
        _listeners.pop(key, None)


def event_query(db):
    return db.query(AuditEvent).filter(AuditEvent.category == CATEGORY)


def preferences(db, user_id, *, lock=False):
    query = db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.user_id == user_id, UserWorkspaceLayout.workspace_key == PREFERENCE_KEY)
    row = (query.with_for_update() if lock else query).first()
    value = dict(row.layout or {}) if row else {}
    return {"notifications": bool(value.get("notifications", True)), "camera_ids": value.get("camera_ids"),
            "kinds": value.get("kinds"), "hours_enabled": bool(value.get("hours_enabled", False)),
            "start_minute": int(value.get("start_minute", 0)), "end_minute": int(value.get("end_minute", 0))}, row


def save_preferences(db, user_id, value):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(UserWorkspaceLayout).values(user_id=user_id, workspace_key=PREFERENCE_KEY,
        layout_version=1, layout=value, updated_at=now_utc())
    db.execute(statement.on_conflict_do_update(index_elements=[UserWorkspaceLayout.user_id, UserWorkspaceLayout.workspace_key],
        set_={"layout": value, "updated_at": now_utc()}))
    db.commit()
    return value


def read_expression(user_id):
    marker = aliased(AuditEvent)
    return select(marker.id).where(marker.category == READ_CATEGORY, marker.actor_user_id == user_id, marker.target_id == AuditEvent.id).exists()


def filtered(query, *, camera_id=None, kind=None, since=None, until=None, unread=False, user_id=None):
    if camera_id is not None:
        query = query.filter(AuditEvent.target_type == "camera", AuditEvent.target_id == str(camera_id))
    if kind:
        query = query.filter(AuditEvent.event_type == "camera." + kind)
    if since:
        query = query.filter(AuditEvent.created_at >= since)
    if until:
        query = query.filter(AuditEvent.created_at <= until)
    if unread:
        query = query.filter(~read_expression(user_id))
    return query


def serialize(row, read=False, ctx=None):
    data = row.event_metadata or {}
    kind = row.event_type.removeprefix("camera.")
    return {"id": row.id, "camera_id": int(row.target_id) if row.target_type == "camera" and str(row.target_id).isdigit() else None,
            "camera_name": row.target_name, "kind": kind if kind in KINDS else "other",
            "occurred_at": data.get("occurred_at") or format_storage_utc_iso(row.created_at),
            "received_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "ended_at": data.get("ended_at"), "active": data.get("active"), "clock_uncertain": bool(data.get("clock_uncertain")),
            "source_ambiguous": bool(data.get("source_ambiguous")), "occurrences": int(data.get("occurrences", 1)),
            "read": bool(read), "state_unconfirmed": bool(data.get("state_unconfirmed")),
            "topic": str(data.get("topic") or "")[:512]}


def list_camera_events(db, user_id, *, limit=50, offset=0, **filters):
    query = filtered(event_query(db), user_id=user_id, **filters)
    total = query.count()
    rows = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset(offset).limit(min(MAX_PAGE, limit)).all()
    ids = [r.id for r in rows]
    read_ids = {r[0] for r in db.query(AuditEvent.target_id).filter(AuditEvent.category == READ_CATEGORY,
                AuditEvent.actor_user_id == user_id, AuditEvent.target_id.in_(ids)).all()} if ids else set()
    return {"items": [serialize(row, row.id in read_ids) for row in rows], "total": total, "limit": limit, "offset": offset,
            "unread_count": event_query(db).filter(~read_expression(user_id)).count(),
            "retention_days": RETENTION_DAYS, "max_events": MAX_EVENTS, "timezone": timezone_context(db).name}


def mark_read(db, user_id, ids):
    ids = list(dict.fromkeys(ids))[:200]
    rows = event_query(db).filter(AuditEvent.id.in_(ids)).all()
    values = [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"km-vms-camera-read:{user_id}:{row.id}")),
               "created_at": now_utc(), "actor_user_id": user_id, "category": READ_CATEGORY,
               "event_type": "camera_event.read", "severity": "info", "message_ru": "", "message_en": "",
               "target_type": "camera_event", "target_id": row.id, "event_metadata": {}} for row in rows]
    if values:
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(AuditEvent).values(values).on_conflict_do_nothing(index_elements=[AuditEvent.id]))
    db.commit()
    notify_change()
    return len(rows)


def record_signal(db, group, camera, signal):
    received = now_utc()
    target_type = "camera" if camera else "camera_device"
    target_id = str(camera["id"]) if camera else group["device_id"]
    name = camera["name"] if camera else " / ".join(c["name"] for c in group["cameras"])[:255]
    episode = hashlib.sha256(json.dumps([group["device_id"], signal.topic, signal.source], sort_keys=True).encode()).hexdigest()
    # SQL narrows by indexed category/type and age. JSON holds only domain data.
    prior = event_query(db).filter(AuditEvent.event_type == "camera." + signal.kind,
                AuditEvent.target_id == target_id, AuditEvent.created_at >= received - timedelta(days=1),
                AuditEvent.event_metadata["episode"].as_string() == episode).order_by(AuditEvent.created_at.desc()).first()
    if prior is not None and not (prior.event_metadata.get("active") is True or
                                  signal.active is None and (received - prior.created_at).total_seconds() < 10):
        prior = None
    if signal.initialized and signal.active is not False:
        return None
    if signal.active is False:
        if prior is not None:
            prior.event_metadata = {**prior.event_metadata, "active": False, "ended_at": signal.occurred_at.replace(tzinfo=timezone.utc).isoformat()}
            db.commit()
            notify_change()
        return None
    if signal.initialized:
        return None
    if prior is not None:
        prior.event_metadata = {**prior.event_metadata, "last_seen_at": received.replace(tzinfo=timezone.utc).isoformat(),
                                "occurrences": min(1000000, int(prior.event_metadata.get("occurrences", 1)) + 1)}
        db.commit()
        return None
    row = AuditEvent(id=str(uuid.uuid4()), created_at=received, category=CATEGORY, event_type="camera." + signal.kind,
                     severity="info", message_ru="Событие камеры", message_en="Camera event", target_type=target_type,
                     target_id=target_id, target_name=name, event_metadata={
                         "episode": episode, "topic": signal.topic, "occurred_at": signal.occurred_at.replace(tzinfo=timezone.utc).isoformat(),
                         "camera_at": signal.camera_at.replace(tzinfo=timezone.utc).isoformat() if signal.camera_at else None,
                         "active": signal.active, "clock_uncertain": signal.clock_uncertain,
                         "source_ambiguous": camera is None, "occurrences": 1,
                     })
    db.add(row)
    db.commit()
    notify_change()
    return row.id


def source_disconnected(db, group):
    ids = [str(c["id"]) for c in group["cameras"]] + [group["device_id"]]
    rows = event_query(db).filter(AuditEvent.target_id.in_(ids), AuditEvent.created_at >= now_utc() - timedelta(days=1),
                                  AuditEvent.event_metadata["active"].as_boolean().is_(True)).limit(256).all()
    for row in rows:
        # Lost observation is neither a confirmed end nor an ongoing alarm.
        row.event_metadata = {**row.event_metadata, "active": None, "state_unconfirmed": True}
    db.commit()
    if rows:
        notify_change()


def cursor_for(row):
    return row.created_at.isoformat(timespec="microseconds") + "|" + row.id if row else None


def updates(db, user_id, cursor=None):
    newest = event_query(db).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).first()
    baseline = cursor_for(newest) or now_utc().isoformat(timespec="microseconds") + "|00000000-0000-0000-0000-000000000000"
    result = {"cursor": cursor or baseline, "items": [], "unread_count": event_query(db).filter(~read_expression(user_id)).count(),
              "preferences": preferences(db, user_id)[0]}
    if not cursor:
        return result
    stamp, identifier = cursor.split("|", 1)
    cutoff = datetime.fromisoformat(stamp).replace(tzinfo=None)
    query = event_query(db).filter(or_(AuditEvent.created_at > cutoff, (AuditEvent.created_at == cutoff) & (AuditEvent.id > identifier)))
    rows = query.order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc()).limit(20).all()
    if rows:
        result["cursor"] = cursor_for(rows[-1])
        result["items"] = [serialize(row) for row in rows]
    return result


def notifications_allowed(preference, event, ctx):
    if not preference.get("notifications", True):
        return False
    if preference.get("camera_ids") is not None and event.get("camera_id") not in preference["camera_ids"]:
        return False
    if preference.get("kinds") is not None and event.get("kind") not in preference["kinds"]:
        return False
    if preference.get("hours_enabled"):
        local = datetime.now(ctx.zone)
        minute, start, end = local.hour * 60 + local.minute, preference["start_minute"], preference["end_minute"]
        if start < end and not start <= minute < end:
            return False
        if start > end and not (minute >= start or minute < end):
            return False
    return True


def _cleanup_batch(db):
    removed = 0
    old = [r.id for r in event_query(db).filter(AuditEvent.created_at < now_utc() - timedelta(days=RETENTION_DAYS)).limit(2000).all()]
    excess = [r.id for r in event_query(db).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset(MAX_EVENTS).limit(2000).all()]
    ids = list(set(old + excess))
    if ids:
        db.query(AuditEvent).filter(AuditEvent.category == READ_CATEGORY, AuditEvent.target_id.in_(ids)).delete(synchronize_session=False)
        removed = db.query(AuditEvent).filter(AuditEvent.category == CATEGORY, AuditEvent.id.in_(ids)).delete(synchronize_session=False)
    db.query(AuditEvent).filter(AuditEvent.category == READ_CATEGORY, AuditEvent.created_at < now_utc() - timedelta(days=RETENTION_DAYS)).delete(synchronize_session=False)
    db.commit()
    return removed


def cleanup_history(db):
    # A five-minute sweep can receive up to 64 * 120 * 5 events. Draining only
    # one 2,000-row batch could never catch up. Bounded batches cover that
    # maximum without a single large transaction or touching any video.
    removed = 0
    for _ in range(24):
        count = _cleanup_batch(db)
        removed += count
        if not count:
            break
    return removed
