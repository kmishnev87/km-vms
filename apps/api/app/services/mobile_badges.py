"""Versioned, account-scoped unread snapshots; silent, opt-in push delivery.

The existing JSON store and push worker own this state. No schema changes,
periodic polling, notification records or camera/recording operations.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from app.core.permissions import user_has_permission
from app.core.security import decrypt_text
from app.models.user import User
from app.models.workspace_layout import UserWorkspaceLayout
from app.models.storage_operation import StorageWorkerLease
from app.services import camera_events as events, mobile_push_store as store
from app.services.mobile_push_transport import PushDeliveryError, send_push
from app.services.storage_operations_foundation import acquire_worker_lease, release_worker_lease, renew_worker_lease

KEY = "mobile_badge:v1"


def _state(db, user_id):
    def query():
        return db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.user_id == user_id,
            UserWorkspaceLayout.workspace_key == KEY).populate_existing().with_for_update().first()
    row = query()
    if row is None:
        # Serialize the first snapshot with registration, including on PostgreSQL.
        db.query(User).filter(User.id == user_id).with_for_update().one()
        row = query()
        if row is None:
            row = UserWorkspaceLayout(user_id=user_id, workspace_key=KEY, layout_version=1,
                layout={"epoch": str(uuid.uuid4()), "revision": 1})
            db.add(row)
            db.flush()
    return row


def snapshot(db, user_id):
    row = _state(db, user_id)
    count = events.event_query(db).filter(~events.read_expression(user_id)).count()
    return {"v": 1, "type": "unread_badge", "epoch": row.layout["epoch"],
            "revision": row.layout["revision"], "count": count}


def touch(db, user_id=None):
    """Part of the event/read/delete transaction, never a separate early commit."""
    query = db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.workspace_key == KEY)
    if user_id is not None:
        query = query.filter(UserWorkspaceLayout.user_id == user_id)
    rows = query.order_by(UserWorkspaceLayout.user_id).populate_existing().with_for_update().all()
    for row in rows:
        row.layout = {**row.layout, "revision": int(row.layout["revision"]) + 1}
    if rows:
        from app.services.mobile_push import signal_change
        signal_change(db)


def deliver_one(factory, device_id, owner, *, sender=send_push, defer=None):
    """Coalesce changed counts, with independent retries and registration fencing."""
    from app.services.mobile_push import REGISTRATION_DAYS
    handle = None
    try:
        with factory() as db:
            row = store.get(db, device_id)
            now = events.now_utc()
            if row is None or not row.badge_enabled:
                return False
            if row.badge_retry_at and row.badge_retry_at > now:
                if defer:
                    defer(row.badge_retry_at)
                return False
            handle = acquire_worker_lease(db, worker_key="mobile.badge." + str(device_id),
                owner_instance_id=owner, lease_seconds=45)
            if handle is None:
                lease = db.get(StorageWorkerLease, "mobile.badge." + str(device_id))
                if defer and lease:
                    defer(lease.lease_expires_at)
                return False
            user = db.get(User, row.user_id)
            if not user or not user.is_active or not user_has_permission(user.role, "view_live") or row.registered_at < now - timedelta(days=REGISTRATION_DAYS):
                row.enabled, row.badge_enabled, row.subscription_encrypted = False, False, None
                row.save(); db.commit()
                return True
            value = snapshot(db, row.user_id)
            if row.badge_epoch == value["epoch"] and row.badge_revision >= value["revision"]:
                return False
            revision = row.revision
            # Reading an already-read event does not wake a sleeping phone again.
            unchanged = row.badge_epoch == value["epoch"] and row.badge_count == value["count"]
            try:
                subscription = json.loads(decrypt_text(row.subscription_encrypted))
            except Exception:
                subscription = None
            db.commit()
        if unchanged:
            # Never acquire the device lock while holding the account snapshot
            # lock: registration acquires those in the opposite order.
            with factory() as db:
                renew_worker_lease(db, handle, lease_seconds=45)
                row = store.get(db, device_id, lock=True)
                if row and row.badge_enabled and row.revision == revision:
                    row.badge_epoch, row.badge_revision = value["epoch"], value["revision"]
                    row.save(); db.commit()
            return True
        failure = None
        try:
            if not subscription:
                raise PushDeliveryError("push_registration_invalid", permanent=True)
            sender(subscription, json.dumps(value, separators=(",", ":")))
        except PushDeliveryError as error:
            failure = error
        except Exception:
            failure = PushDeliveryError("push_delivery_unavailable")
        with factory() as db:
            renew_worker_lease(db, handle, lease_seconds=45)
            row = store.get(db, device_id, lock=True)
            if row is None or not row.badge_enabled or row.revision != revision:
                return True
            if failure:
                row.badge_failures = min(20, row.badge_failures + 1)
                if failure.permanent:
                    row.enabled, row.badge_enabled, row.subscription_encrypted = False, False, None
                    row.badge_retry_at = None
                else:
                    row.badge_retry_at = events.now_utc() + timedelta(seconds=min(1800, 30 * 2 ** min(row.badge_failures - 1, 6)))
            else:
                row.badge_epoch, row.badge_revision, row.badge_count = value["epoch"], value["revision"], value["count"]
                row.badge_retry_at, row.badge_failures = None, 0
            row.save(); db.commit()
        return True
    finally:
        if handle is not None:
            with factory() as db:
                release_worker_lease(db, handle)
