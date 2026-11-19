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

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.workspace_layout import UserWorkspaceLayout
from app.services.onvif_events import EVENT_KINDS, KINDS, LEGACY_TOPIC_KINDS, SERVICE_HISTORY_TOPICS
from app.services import camera_alert_episodes as episodes
from app.services.timezone_contract import format_storage_utc_iso, format_system_iso, timezone_context

CATEGORY = "camera_event"
READ_CATEGORY = "camera_event_read"
PREFERENCE_KEY = "camera_events"
RETENTION_DAYS = 30
MAX_EVENTS = 100_000
MAX_PAGE = 100
AI_ORIGIN = "nas_ai"
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
    return preference_value(row.layout if row else {}), row


def preference_value(layout):
    value = dict(layout or {})
    return {"notifications": bool(value.get("notifications", True)), "camera_ids": value.get("camera_ids"),
            "kinds": None,  # Old clients still parse the field; it no longer filters delivery.
            "hours_enabled": bool(value.get("hours_enabled", False)),
            "start_minute": int(value.get("start_minute", 0)), "end_minute": int(value.get("end_minute", 0)),
            "schedule_mode": value.get("schedule_mode", "all"), "camera_schedules": value.get("camera_schedules") or {}}


def lock_notification_preferences(db, user_id):
    # Materialize the default row before locking: FOR UPDATE alone cannot lock
    # an absent row. No user-row lock that could invert ingestion's FK ordering.
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(UserWorkspaceLayout).values(user_id=user_id, workspace_key=PREFERENCE_KEY,
        layout_version=1, layout=preference_value({}), updated_at=now_utc()).on_conflict_do_nothing(
            index_elements=[UserWorkspaceLayout.user_id, UserWorkspaceLayout.workspace_key]))
    return preferences(db, user_id, lock=True)[0]


def save_preferences(db, user_id, value):
    value = {**value, "kinds": None}
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(UserWorkspaceLayout).values(user_id=user_id, workspace_key=PREFERENCE_KEY,
        layout_version=1, layout=value, updated_at=now_utc())
    db.execute(statement.on_conflict_do_update(index_elements=[UserWorkspaceLayout.user_id, UserWorkspaceLayout.workspace_key],
        set_={"layout": value, "updated_at": now_utc()}))
    db.commit()
    # Wake existing long polls so other signed-in clients can refresh personal
    # settings. Saving never acknowledges any event, including the old backlog.
    notify_change()
    return value


def read_expression(user_id):
    marker = aliased(AuditEvent)
    return or_(AuditEvent.event_metadata["ai_notify"].as_boolean().is_(False),
               select(marker.id).where(marker.category == READ_CATEGORY, marker.actor_user_id == user_id, marker.target_id == AuditEvent.id).exists())


def marked_read_ids(db, user_id, ids):
    return {row[0] for row in db.query(AuditEvent.target_id).filter(AuditEvent.category == READ_CATEGORY,
        AuditEvent.actor_user_id == user_id, AuditEvent.target_id.in_(ids)).all()} if ids else set()


def filtered(query, *, camera_id=None, kind=None, since=None, until=None, unread=False, user_id=None,
             source=None, rule=None, condition=None):
    if camera_id is not None:
        query = query.filter(AuditEvent.target_type == "camera", AuditEvent.target_id == str(camera_id))
    if kind:
        query = query.filter(or_(AuditEvent.event_type == "camera." + kind,
                                 AuditEvent.event_metadata["object_kind"].as_string() == kind,
                                 *(AuditEvent.event_metadata["object_kinds"][index].as_string() == kind for index in range(3)),
                                 AuditEvent.event_metadata["rule_kind"].as_string() == kind,
                                 (AuditEvent.event_type == "camera.other") & AuditEvent.event_metadata["topic"].as_string().in_(
                                     [topic for topic, label in LEGACY_TOPIC_KINDS.items() if label == kind])))
    origin = AuditEvent.event_metadata["origin"].as_string()
    if source == "ai" or rule or condition:
        query = query.filter(origin == AI_ORIGIN)
    elif source == "camera":
        query = query.filter(or_(origin.is_(None), origin != AI_ORIGIN))
    if rule and (term := rule.strip()):
        query = query.filter(AuditEvent.event_metadata["ai_rule_name"].as_string().icontains(term, autoescape=True))
    if condition:
        query = query.filter(AuditEvent.event_metadata["ai_condition"].as_string() == condition)
    # Device events retain their receipt-time semantics; NAS AI knows when it captured the frame.
    capture = AuditEvent.event_metadata["ai_captured_epoch"].as_float()
    use_receipt = or_(origin.is_(None), origin != AI_ORIGIN, capture.is_(None))
    if since:
        query = query.filter(AuditEvent.created_at >= since - timedelta(seconds=5),
            or_(and_(use_receipt, AuditEvent.created_at >= since),
                and_(origin == AI_ORIGIN, capture >= since.replace(tzinfo=timezone.utc).timestamp())))
    if until:
        query = query.filter(AuditEvent.created_at <= until + timedelta(minutes=10),
            or_(and_(use_receipt, AuditEvent.created_at <= until),
                and_(origin == AI_ORIGIN, capture <= until.replace(tzinfo=timezone.utc).timestamp())))
    if unread:
        query = query.filter(~read_expression(user_id))
    return query


def serialize(row, read=False, ctx=None):
    data = row.event_metadata or {}
    expires = episodes.timestamp(data.get("alert_episode_until"))
    active = data.get("active") if expires is None else episodes.epoch(now_utc()) < expires
    ended_at = data.get("ended_at") if expires is None else episodes.iso(expires) if not active else None
    kind = row.event_type.removeprefix("camera.")
    if kind == "other":
        kind = LEGACY_TOPIC_KINDS.get(data.get("topic"), kind)
    ai = data.get("origin") == AI_ORIGIN
    result = {"id": row.id, "camera_id": int(row.target_id) if row.target_type == "camera" and str(row.target_id).isdigit() else None,
            "camera_name": row.target_name, "kind": kind if kind in KINDS else "other",
            "occurred_at": data.get("occurred_at") or format_storage_utc_iso(row.created_at),
            "received_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "ended_at": ended_at, "active": active, "clock_uncertain": bool(data.get("clock_uncertain")),
            # This is an observation, not a promise that an old alarm is live now.
            "state_observed_at": data.get("last_seen_at") or format_storage_utc_iso(row.created_at),
            "initial_state": bool(data.get("initial_state")),
            "source_ambiguous": bool(data.get("source_ambiguous")), "occurrences": int(data.get("occurrences", 1)),
            "read": bool(read or (ai and data.get("ai_notify") is False)), "state_unconfirmed": bool(data.get("state_unconfirmed")),
            "topic": str(data.get("topic") or "")[:512],
            "object_kind": data.get("object_kind") if data.get("object_kind") in {"person", "vehicle", "animal"} else None,
            "object_kinds": [kind for kind in ("person", "vehicle", "animal") if kind in (data.get("object_kinds") or [data.get("object_kind")])],
            "rule_kind": data.get("rule_kind") if data.get("rule_kind") in EVENT_KINDS else None}
    if ai:
        condition = data.get("ai_condition")
        scope = data.get("ai_scope")
        if scope not in {"frame", "zone", "line"}:
            scope = "line" if condition == "crossing" else "zone" if condition in {"enter", "exit", "dwell"} else "frame"
        result["ai"] = {"condition": condition, "rule_id": data.get("ai_rule_id"), "scope": scope,
                        "rule_name": str(data.get("ai_rule_name") or "")[:48],
                        "snapshot_available": data.get("ai_snapshot") is True,
                        "false_positive": data.get("ai_false_positive") is True,
                        "notify_requested": data.get("ai_notify") is True}
    return result


def list_camera_events(db, user_id, *, limit=50, offset=0, selection_id=None, **filters):
    # Capture the boundary before loading the list. A bulk acknowledgement must
    # never consume events which arrived after the browser's snapshot.
    latest = event_query(db).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).first()
    read_cursor = cursor_for(latest)
    history_count = event_query(db).filter(through_snapshot(latest.created_at, latest.id)).count() if latest else 0
    query = filtered(event_query(db), user_id=user_id, **filters)
    total = query.count()
    rows = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset(offset).limit(min(MAX_PAGE, limit)).all()
    ids = [r.id for r in rows]
    read_ids = marked_read_ids(db, user_id, ids)
    selection = None
    if selection_id is not None:
        from app.services import camera_event_selections
        selected_row, members = camera_event_selections.load(db, user_id, selection_id)
        selection = camera_event_selections.describe(selected_row, members, ids)
    return {"items": [serialize(row, row.id in read_ids) for row in rows], "total": total, "limit": limit, "offset": offset,
            "unread_count": event_query(db).filter(~read_expression(user_id)).count(),
            "retention_days": RETENTION_DAYS, "max_events": MAX_EVENTS, "timezone": timezone_context(db).name,
            "read_cursor": read_cursor, "history_count": history_count, "selection": selection}


def _insert_read_markers(db, user_id, rows, *, automatic=False):
    values = [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"km-vms-camera-read:{user_id}:{row.id}")),
               "created_at": now_utc(), "actor_user_id": user_id, "category": READ_CATEGORY,
               "event_type": "camera_event.read", "severity": "info", "message_ru": "", "message_en": "",
               "target_type": "camera_event", "target_id": row.id,
               "event_metadata": {"automatic": True} if automatic else {}} for row in rows]
    if values:
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(AuditEvent).values(values).on_conflict_do_nothing(index_elements=[AuditEvent.id]))
        from app.services.mobile_badges import touch
        touch(db, user_id)


def auto_read_new_event(db, row, received):
    """Freeze muted personal attention when a NEW event is accepted, not read.

    Users without saved preferences use the all-day/all-camera default. Read
    markers belong only to the excluded user and share the event transaction.
    Lock existing preference rows against a simultaneous settings save; neither
    a later client visit nor re-enabling alerts can rewrite this decision.
    """
    settings = db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.workspace_key == PREFERENCE_KEY).order_by(
        UserWorkspaceLayout.user_id).with_for_update().all()
    event, context = serialize(row), timezone_context(db)
    for setting in settings:
        if not notifications_allowed(preference_value(setting.layout), event, context, at=received):
            _insert_read_markers(db, setting.user_id, [row], automatic=True)


def automatic_read_ids(db, user_id, ids):
    return {row[0] for row in db.query(AuditEvent.target_id).filter(AuditEvent.category == READ_CATEGORY,
        AuditEvent.actor_user_id == user_id, AuditEvent.target_id.in_(ids),
        AuditEvent.event_metadata["automatic"].as_boolean().is_(True)).all()} if ids else set()


def event_notification_allowed(db, user_id, preference, event, ctx):
    # A muted event stays silent even if a phone was offline until alerts were
    # enabled again. Ordinary manual read markers keep their existing semantics.
    return notifications_allowed(preference, event, ctx) and not automatic_read_ids(db, user_id, [event["id"]])


def mark_read(db, user_id, ids):
    ids = list(dict.fromkeys(ids))[:200]
    rows = event_query(db).filter(AuditEvent.id.in_(ids)).order_by(AuditEvent.created_at, AuditEvent.id).with_for_update().all()
    _insert_read_markers(db, user_id, rows)
    db.commit()
    notify_change()
    return len(rows)


def through_snapshot(stamp, identifier):
    return or_(AuditEvent.created_at < stamp, (AuditEvent.created_at == stamp) & (AuditEvent.id <= identifier))


def mark_all_read(db, user_id, stamp, identifier):
    query = event_query(db).filter(~read_expression(user_id), through_snapshot(stamp, identifier)).order_by(AuditEvent.created_at, AuditEvent.id).with_for_update()
    marked = 0
    # Bounded database batches inside one atomic user action, not a request per
    # page or event. Deterministic markers make concurrent retries idempotent.
    while marked < MAX_EVENTS:
        rows = query.limit(min(1000, MAX_EVENTS - marked)).all()
        if not rows:
            break
        _insert_read_markers(db, user_id, rows)
        marked += len(rows)
    db.commit()
    notify_change()
    return marked


def delete_camera_events(db, *, ids=None, through=None, actor=None):
    """Delete only explicitly selected episodes or a frozen history snapshot.

    Read markers belong to every user, but neither this domain nor its cleanup
    owns any recording, camera, preference or administrative journal row.
    """
    if (ids is None) == (through is None):
        raise ValueError("camera_event_delete_scope_required")
    query = event_query(db)
    if ids is not None:
        identifiers = list(dict.fromkeys(ids))
        if not 1 <= len(identifiers) <= 200:
            raise ValueError("camera_event_delete_selection_invalid")
        query = query.filter(AuditEvent.id.in_(identifiers))
    else:
        query = query.filter(through_snapshot(*through))
    deleted = 0
    try:
        # The retention cap is a cleanup policy, not permission to leave part
        # of an explicitly deleted snapshot behind when cleanup is catching up.
        while True:
            rows = query.order_by(AuditEvent.created_at, AuditEvent.id).limit(1000).with_for_update().all()
            if not rows:
                break
            batch = [row.id for row in rows]
            db.query(AuditEvent).filter(AuditEvent.category == READ_CATEGORY, AuditEvent.target_id.in_(batch)).delete(synchronize_session=False)
            deleted += event_query(db).filter(AuditEvent.id.in_(batch)).delete(synchronize_session=False)
        if deleted and actor is not None:
            from app.services.audit_log import create_event
            log = create_event(db=db, actor=actor, category="cameras", event_type="cameras.events.deleted",
                message_ru="Удалена история событий камер", message_en="Camera event history deleted",
                metadata={"count": deleted, "scope": "selected" if ids is not None else "history"}, commit=False)
            if log is None:
                raise RuntimeError("camera_event_delete_audit_failed")
        if deleted:
            from app.services.mobile_badges import touch
            touch(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    notify_change()
    return deleted


def record_signal(db, group, camera, signal, *, diagnostics=None):
    if signal.kind not in EVENT_KINDS and signal.active is not False:
        return None  # Unknown signals belong to bounded receiver diagnostics.
    received = now_utc()
    from app.services.ai_recording import camera_history_matches_policy, observe_camera_signal
    target_type = "camera" if camera else "camera_device"
    target_id = str(camera["id"]) if camera else group["device_id"]
    name = camera["name"] if camera else " / ".join(c["name"] for c in group["cameras"])[:255]
    identity = [group["device_id"], signal.topic, signal.source]
    if signal.key:
        identity.append(signal.key)
    episode = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if signal.active is False:
        observe_camera_signal(db, group, camera, signal, received=received)
        # Close pre-upgrade property alarms without changing the new episode's
        # deadline. The end of one object is not the end of all activity.
        changed = False
        pending = event_query(db).filter(AuditEvent.target_type == target_type, AuditEvent.target_id == target_id,
            AuditEvent.event_metadata["episode"].as_string() == episode,
            AuditEvent.event_metadata["alert_episode_until"].as_float().is_(None),
            or_(AuditEvent.event_metadata["active"].as_boolean().is_(True),
                AuditEvent.event_metadata["state_unconfirmed"].as_boolean().is_(True)))
        while True:
            rows = pending.order_by(AuditEvent.created_at, AuditEvent.id).with_for_update().limit(256).all()
            if not rows:
                break
            for row in rows:
                row.event_metadata = {**row.event_metadata, "active": False, "state_unconfirmed": False,
                                      "last_seen_at": received.replace(tzinfo=timezone.utc).isoformat(),
                                      "ended_at": signal.occurred_at.replace(tzinfo=timezone.utc).isoformat()}
            db.flush()
            changed = True
        db.commit()
        if changed:
            notify_change()
        return None
    if signal.initialized or getattr(signal, "operation", None) == "Initialized":
        return None
    owner_id = camera["id"] if camera else min((lens["id"] for lens in group["cameras"]), default=None)
    owner = db.query(Camera).filter(Camera.id == owner_id, Camera.deleted_at.is_(None),
                                   Camera.status != "deleting").populate_existing().with_for_update().first()
    if owner is None or not camera_history_matches_policy(db, group, camera, signal):
        db.commit()
        return None
    policy = owner.recording_policy or {}
    fingerprint = None
    if signal.camera_at is not None:
        fingerprint = hashlib.sha256(json.dumps(
            [episode, signal.camera_at.isoformat(), signal.kind, getattr(signal, "rule_kind", None),
             getattr(signal, "object_kind", None), sorted(getattr(signal, "object_kinds", ()) or ())],
            separators=(",", ":")).encode()).hexdigest()
    device = None if camera else group["device_id"]
    if episodes.is_replay(owner, source="camera", device=device, fingerprint=fingerprint):
        db.commit()
        return None  # A new ONVIF subscription generation can replay a device stamp.
    observed = observe_camera_signal(db, group, camera, signal, received=received)
    if camera and policy.get("mode") == "events" and not observed:
        db.commit()
        return None  # The same validation prevents stale pulses extending the Recorder window.
    identifier = str(uuid.uuid4())
    admitted = episodes.admit(db, owner, source="camera", at=episodes.epoch(received),
        minutes=episodes.settings_from(policy if camera else None), identifier=identifier,
        device=device, fingerprint=fingerprint)
    if admitted is None or not admitted.opening:
        if diagnostics is not None:
            diagnostics["messages_suppressed"] = diagnostics.get("messages_suppressed", 0) + 1
        db.commit()
        return None
    row = AuditEvent(id=identifier, created_at=received, category=CATEGORY, event_type="camera." + signal.kind,
                     severity="info", message_ru="Событие камеры", message_en="Camera event", target_type=target_type,
                     target_id=target_id, target_name=name, event_metadata={
                         "episode": episode, "topic": signal.topic, "occurred_at": signal.occurred_at.replace(tzinfo=timezone.utc).isoformat(),
                         "camera_at": signal.camera_at.replace(tzinfo=timezone.utc).isoformat() if signal.camera_at else None,
                         "active": signal.active, "initial_state": signal.initialized, "clock_uncertain": signal.clock_uncertain,
                         "source_ambiguous": camera is None, "occurrences": 1,
                         "rule_kind": getattr(signal, "rule_kind", None),
                         "object_kind": getattr(signal, "object_kind", None),
                         "object_kinds": list(getattr(signal, "object_kinds", ()) or filter(None, [getattr(signal, "object_kind", None)])),
                         **admitted.metadata,
                     })
    db.add(row)
    db.flush()
    auto_read_new_event(db, row, received)
    from app.services.mobile_badges import touch
    touch(db)
    from app.services.mobile_push import signal_change, wake_local
    signal_change(db)
    db.commit()
    notify_change()
    wake_local()
    if camera and camera.get("revision") and not signal.clock_uncertain:
        from app.services.camera_event_frames import queue_frame

        queue_frame(camera["id"], row.id, camera["revision"], signal.occurred_at)
    return row.id


def source_disconnected(db, group):
    from app.services.ai_recording import invalidate_camera_signals
    invalidate_camera_signals(db, group)
    ids = [str(c["id"]) for c in group["cameras"]] + [group["device_id"]]
    query = event_query(db).filter(AuditEvent.target_id.in_(ids), AuditEvent.event_metadata["active"].as_boolean().is_(True),
        AuditEvent.event_metadata["alert_episode_until"].as_float().is_(None),
        AuditEvent.event_metadata["origin"].as_string().is_distinct_from(AI_ORIGIN))
    changed = False
    while True:
        rows = query.order_by(AuditEvent.created_at, AuditEvent.id).limit(256).with_for_update().all()
        if not rows:
            break
        for row in rows:
            # Lost observation is neither a confirmed end nor an ongoing alarm.
            row.event_metadata = {**row.event_metadata, "active": None, "state_unconfirmed": True}
        db.flush()
        changed = True
    db.commit()
    if changed:
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
        ids = [row.id for row in rows]
        read_ids = marked_read_ids(db, user_id, ids)
        automatic = automatic_read_ids(db, user_id, ids)
        context = timezone_context(db)
        result["items"] = [{**serialize(row, row.id in read_ids),
            "notify": row.id not in automatic and notifications_allowed(result["preferences"], serialize(row), context)} for row in rows]
    return result


def notifications_allowed(preference, event, ctx, *, at=None):
    if event.get("ai") is not None and event["ai"].get("notify_requested") is not True:
        return False
    if not preference.get("notifications", True):
        return False
    if preference.get("camera_ids") is not None and event.get("camera_id") not in preference["camera_ids"]:
        return False
    if preference.get("hours_enabled"):
        # Arrival uses the NAS clock, not a camera's possibly wrong timestamp.
        # Delivery also respects the user's CURRENT quiet hours.
        instant = at or datetime.now(timezone.utc)
        local = instant.replace(tzinfo=timezone.utc).astimezone(ctx.zone) if instant.tzinfo is None else instant.astimezone(ctx.zone)
        schedule = (preference.get("camera_schedules") or {}).get(str(event.get("camera_id")), {}) if preference.get("schedule_mode") == "per_camera" else {}
        minute = local.hour * 60 + local.minute
        start, end = schedule.get("start_minute", preference["start_minute"]), schedule.get("end_minute", preference["end_minute"])
        if start < end and not start <= minute < end:
            return False
        if start > end and not (minute >= start or minute < end):
            return False
    return True


def _cleanup_batch(db):
    removed = 0
    old = [r.id for r in event_query(db).filter(AuditEvent.created_at < now_utc() - timedelta(days=RETENTION_DAYS)).limit(2000).all()]
    excess = [r.id for r in event_query(db).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset(MAX_EVENTS).limit(2000).all()]
    service = [r.id for r in event_query(db).filter(AuditEvent.event_metadata["topic"].as_string().in_(SERVICE_HISTORY_TOPICS)).limit(2000).all()]
    ids = list(set(old + excess + service))
    if ids:
        ids = [r.id for r in event_query(db).filter(AuditEvent.id.in_(ids)).order_by(AuditEvent.created_at, AuditEvent.id).with_for_update().all()]
        db.query(AuditEvent).filter(AuditEvent.category == READ_CATEGORY, AuditEvent.target_id.in_(ids)).delete(synchronize_session=False)
        removed = db.query(AuditEvent).filter(AuditEvent.category == CATEGORY, AuditEvent.id.in_(ids)).delete(synchronize_session=False)
    db.query(AuditEvent).filter(AuditEvent.category == READ_CATEGORY, AuditEvent.created_at < now_utc() - timedelta(days=RETENTION_DAYS)).delete(synchronize_session=False)
    if removed:
        from app.services.mobile_badges import touch
        touch(db)
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
