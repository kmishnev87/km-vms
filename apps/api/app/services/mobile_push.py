"""Durable mobile delivery state, separated from camera reception and UI markers."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, cast, func, or_, text, type_coerce

from app.core.permissions import user_has_permission
from app.core.security import decrypt_text, encrypt_text
from app.models.workspace_layout import UserWorkspaceLayout
from app.models.user import User
from app.models.storage_operation import StorageWorkerLease
from app.services import camera_events as events
from app.services.mobile_push_transport import PushDeliveryError, send_push, validate_subscription
from app.services import mobile_push_store as store
from app.services.storage_operations_foundation import acquire_worker_lease, release_worker_lease, renew_worker_lease
from app.services.timezone_contract import timezone_context

MAX_USER_DEVICES = 16
REGISTRATION_DAYS = 90
DELIVERY_TTL_HOURS = 24
ZERO_ID = "00000000-0000-0000-0000-000000000000"


def signal_change(db):
    """PostgreSQL notifications are delivered only if the event transaction commits."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_notify('kmvms_mobile_push', '')"))


def wake_local():
    from app.services.mobile_push_worker import wake_mobile_push_worker
    wake_mobile_push_worker()


def status(row):
    return {"device_id": row.device_id, "enabled": row.enabled, "badge_enabled": row.badge_enabled, "revision": row.revision,
            "label": row.label, "status": row.last_status,
            "registered_at": row.registered_at.isoformat() + "Z",
            "last_sent_at": row.last_sent_at.isoformat() + "Z" if row.last_sent_at else None}


def register_device(db, user_id, device_id, revision, label, enabled, subscription, *, badge_enabled=False):
    normalized = validate_subscription(subscription) if enabled or badge_enabled else None
    configuration = {"label": label, "enabled": enabled, "subscription": normalized}
    if badge_enabled:
        configuration["badge_enabled"] = True
    # Preserve exact fingerprints of existing clients which never opted in.
    material = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(material.encode()).hexdigest()
    # The account lock serializes creation/quota checks, not network delivery.
    db.query(User).filter(User.id == user_id).with_for_update().one()
    row = store.by_user(db, user_id, device_id, lock=True)
    if row and (revision < row.revision or revision == row.revision and fingerprint != row.configuration_hash):
        raise ValueError("mobile_push_revision_conflict")
    if row and revision == row.revision:
        return status(row)
    now = events.now_utc()
    if row is None:
        # Reinstalled phones do not fill the account forever with expired routes.
        for previous in store.query(db).filter(UserWorkspaceLayout.user_id == user_id).with_for_update().all():
            expired = store.decode(previous)
            if expired and expired.registered_at < now - timedelta(days=REGISTRATION_DAYS):
                db.delete(previous)
        db.flush()
        if store.query(db).filter(UserWorkspaceLayout.user_id == user_id).count() >= MAX_USER_DEVICES:
            raise ValueError("mobile_push_device_limit")
        envelope = UserWorkspaceLayout(user_id=user_id, workspace_key=store.PREFIX + device_id, layout_version=1)
        db.add(envelope)
        row = store.Device(row=envelope,
            label=label, revision=revision, configuration_hash=fingerprint, enabled=False,
            subscription_encrypted=None, registered_at=now, cursor_at=now, cursor_id=ZERO_ID)
    if enabled and not row.enabled:
        latest = events.event_query(db).order_by(events.AuditEvent.created_at.desc(), events.AuditEvent.id.desc()).first()
        row.cursor_at = max(now, latest.created_at) if latest else now
        row.cursor_id = latest.id if latest and latest.created_at >= now else ZERO_ID
    row.label, row.revision, row.configuration_hash = label, revision, fingerprint
    row.subscription_encrypted = encrypt_text(json.dumps(normalized)) if normalized else None
    row.enabled, row.registered_at = enabled, now
    row.badge_enabled = badge_enabled
    row.badge_retry_at, row.badge_failures = None, 0
    if badge_enabled:
        row.badge_epoch, row.badge_revision, row.badge_count = None, -1, None
    row.retry_at, row.failures, row.last_status = None, 0, "registered" if enabled else "disabled"
    row.save()
    if badge_enabled:
        from app.services.mobile_badges import snapshot
        snapshot(db, user_id)
    signal_change(db)
    db.commit()
    wake_local()
    return status(row)


def push_timestamp(value):
    """UTC-Z on the wire, including clients with older Android java.time."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def event_payload(event):
    """No NAS address, password, access token, media URL, XML or arbitrary command."""
    data = {"v": 1, "type": "camera_event", "event_id": event["id"],
        "camera_id": event.get("camera_id"), "camera_name": (event.get("camera_name") or "")[:160],
        "kind": event["kind"], "objects": event.get("object_kinds") or [],
        "occurred_at": push_timestamp(event["occurred_at"])}
    if (ai := event.get("ai")) and ai.get("condition") in {"presence", "enter", "exit", "dwell", "crossing", "scene"}:
        data["ai_condition"] = ai["condition"]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def send_test(db, user_id, device_id, *, sender=send_push):
    row = store.by_user(db, user_id, device_id, lock=True)
    if row is None or not row.enabled:
        raise ValueError("mobile_push_device_not_found")
    now = events.now_utc()
    if row.last_test_at and (now - row.last_test_at).total_seconds() < 30:
        raise ValueError("mobile_push_test_rate_limit")
    try:
        subscription = json.loads(decrypt_text(row.subscription_encrypted))
    except Exception:
        raise ValueError("mobile_push_subscription_invalid") from None
    row.last_test_at = now
    row.save()
    db.commit()
    identifier = str(uuid.uuid4())
    sender(subscription, json.dumps({"v": 1, "type": "delivery_test", "event_id": identifier,
        "occurred_at": push_timestamp(now)}))
    return {"status": "service_accepted", "test_id": identifier}


def _delivery_time(db):
    envelope = events.AuditEvent
    changed = envelope.event_metadata["mobile_notification_at"].as_string()
    # PostgreSQL timestamps and SQLite's canonical six-digit stored timestamps
    # need different casts, not a lossy seconds-only extraction.
    if db.get_bind().dialect.name == "sqlite":
        return type_coerce(func.coalesce(changed, envelope.created_at), DateTime)
    return func.coalesce(cast(changed, DateTime), envelope.created_at)


def _after(row, time_column):
    envelope = events.AuditEvent
    return or_(time_column > row.cursor_at,
        (time_column == row.cursor_at) & (envelope.id > row.cursor_id))


def deliver_one(factory, device_id, owner, *, sender=send_push, defer=None):
    """One fenced attempt, with no database transaction held during HTTPS I/O.

    A lost response may be resent. The phone deduplicates by NAS + event UUID;
    never claim exactly-once delivery over an unreliable network.
    """
    handle = None
    try:
        with factory() as db:
            row = store.get(db, device_id)
            now = events.now_utc()
            if row is None or not row.enabled or row.retry_at and row.retry_at > now:
                return False
            handle = acquire_worker_lease(db, worker_key="mobile.push." + str(device_id), owner_instance_id=owner, lease_seconds=45)
            if handle is None:
                lease = db.get(StorageWorkerLease, "mobile.push." + str(device_id))
                if defer and lease:
                    defer(lease.lease_expires_at)
                return False
            row = store.get(db, device_id)
            user = db.get(User, row.user_id)
            if not user or not user.is_active or not user_has_permission(user.role, "view_live") or row.registered_at < now - timedelta(days=REGISTRATION_DAYS):
                row.enabled, row.badge_enabled, row.subscription_encrypted, row.last_status = False, False, None, "registration_inactive"
                row.save()
                db.commit()
                return True
            time_column = _delivery_time(db)
            candidate = events.event_query(db).add_columns(time_column.label("delivery_at")).filter(
                events.AuditEvent.created_at >= now - timedelta(hours=DELIVERY_TTL_HOURS), _after(row, time_column)
            ).order_by(time_column, events.AuditEvent.id).first()
            event = candidate[0] if candidate else None
            if event is None:
                return False
            cursor, identifier, revision = candidate.delivery_at, event.id, row.revision
            serialized = events.serialize(event, ctx=timezone_context(db))
            preference = events.preferences(db, user.id)[0]
            allowed = cursor >= now - timedelta(hours=DELIVERY_TTL_HOURS) and events.event_notification_allowed(
                db, user.id, preference, serialized, timezone_context(db))
            payload = event_payload(serialized) if allowed else None
            try:
                subscription = json.loads(decrypt_text(row.subscription_encrypted)) if allowed else None
            except Exception:
                subscription = None
            db.commit()
        failure = None
        if allowed:
            try:
                if not subscription:
                    raise PushDeliveryError("push_registration_invalid", permanent=True)
                sender(subscription, payload)
            except PushDeliveryError as exc:
                failure = exc
            except Exception:
                failure = PushDeliveryError("push_delivery_unavailable")
        with factory() as db:
            renew_worker_lease(db, handle, lease_seconds=45)
            row = store.get(db, device_id, lock=True)
            # A concurrent logout/endpoint rotation wins over this old result.
            if row is None or not row.enabled or row.revision != revision:
                return True
            if failure:
                row.failures = min(20, row.failures + 1)
                row.last_status = failure.code
                if failure.permanent:
                    row.enabled, row.badge_enabled, row.subscription_encrypted, row.retry_at = False, False, None, None
                else:
                    row.retry_at = events.now_utc() + timedelta(seconds=min(1800, 30 * 2 ** min(row.failures - 1, 6)))
            else:
                row.cursor_at, row.cursor_id = cursor, identifier
                row.retry_at, row.failures = None, 0
                row.last_status = "service_accepted" if allowed else "filtered"
                if allowed:
                    row.last_sent_at = events.now_utc()
            row.save()
            db.commit()
        return True
    finally:
        if handle is not None:
            with factory() as db:
                release_worker_lease(db, handle)
