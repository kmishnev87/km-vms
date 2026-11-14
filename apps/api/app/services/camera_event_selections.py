"""Private, bounded immutable event selections over the existing user-state store.

The browser keeps one small identifier, not a request for every history page.
No table/migration or recurring task is needed; expired snapshots are pruned
when their owner creates another selection. Normal layout APIs cannot access
this namespace. IDs freeze mutable unread/object-class filters as well as time.
"""
from datetime import timedelta, timezone
import uuid

from app.models.audit_event import AuditEvent
from app.models.user import User
from app.models.workspace_layout import UserWorkspaceLayout
from app.services import camera_events as events

PREFIX = "ce-select:"
TTL = timedelta(days=1)
MAX_SNAPSHOTS = 4


class SelectionError(ValueError):
    def __init__(self, code="event_selection_expired"):
        self.code = code
        super().__init__(code)


def _query(db, user_id):
    return db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.user_id == user_id,
        UserWorkspaceLayout.workspace_key.startswith(PREFIX, autoescape=True))


def load(db, user_id, selection_id, *, lock=False):
    query = _query(db, user_id).filter(UserWorkspaceLayout.workspace_key == PREFIX + str(selection_id))
    row = (query.with_for_update() if lock else query).first()
    if row is None or row.layout_version != 1 or row.created_at < events.now_utc() - TTL:
        raise SelectionError()
    ids = (row.layout or {}).get("ids")
    if not isinstance(ids, list) or len(ids) > events.MAX_EVENTS or any(not isinstance(value, str) for value in ids):
        raise SelectionError()
    return row, ids


def describe(row, ids, visible_ids=()):
    members = set(ids)
    return {"id": row.workspace_key[len(PREFIX):], "count": len(ids),
        "expires_at": (row.created_at + TTL).replace(tzinfo=timezone.utc).isoformat(),
        "visible_ids": [str(value) for value in visible_ids if str(value) in members]}


def create(db, user_id, through, filters, visible_ids=()):
    try:
        # Serialise only the owner's snapshot-retention changes, not the history.
        if db.query(User.id).filter(User.id == user_id).with_for_update().first() is None:
            raise SelectionError()
        rows = events.filtered(events.event_query(db), user_id=user_id, **filters).with_entities(AuditEvent.id).filter(
            events.through_snapshot(*through)).order_by(AuditEvent.created_at, AuditEvent.id).limit(events.MAX_EVENTS + 1).all()
        if len(rows) > events.MAX_EVENTS:
            raise SelectionError("event_selection_too_large")
        ids = [row.id for row in rows]
        instant = events.now_utc()
        selection_id = str(uuid.uuid4())
        row = UserWorkspaceLayout(user_id=user_id, workspace_key=PREFIX + selection_id,
            layout_version=1, layout={"ids": ids}, created_at=instant, updated_at=instant)
        previous = _query(db, user_id).order_by(UserWorkspaceLayout.created_at.desc(), UserWorkspaceLayout.id.desc()).all()
        for index, old in enumerate(previous):
            if index >= MAX_SNAPSHOTS - 1 or old.created_at < instant - TTL:
                db.delete(old)
        db.add(row)
        db.commit()
        return describe(row, ids, visible_ids)
    except Exception:
        db.rollback()
        raise


def apply(db, user_id, selection_id, *, excluded_ids=(), delete=False, actor=None):
    try:
        _row, members = load(db, user_id, selection_id, lock=True)
        excluded = set(str(value) for value in excluded_ids)
        ids = [value for value in members if value not in excluded]
        changed = 0
        # One atomic user action with bounded SQL parameters, including on
        # PostgreSQL. Never expand the saved IDs by re-running a mutable filter.
        for offset in range(0, len(ids), 1000):
            rows = events.event_query(db).filter(AuditEvent.id.in_(ids[offset:offset + 1000])).order_by(
                AuditEvent.created_at, AuditEvent.id).with_for_update().all()
            if not rows:
                continue
            if delete:
                batch = [row.id for row in rows]
                db.query(AuditEvent).filter(AuditEvent.category == events.READ_CATEGORY,
                    AuditEvent.target_id.in_(batch)).delete(synchronize_session=False)
                changed += events.event_query(db).filter(AuditEvent.id.in_(batch)).delete(synchronize_session=False)
            else:
                events._insert_read_markers(db, user_id, rows)
                changed += len(rows)
        if delete and changed and actor is not None:
            from app.services.audit_log import create_event
            logged = create_event(db=db, actor=actor, category="cameras", event_type="cameras.events.deleted",
                message_ru="Удалена выбранная история событий камер", message_en="Selected camera event history deleted",
                metadata={"count": changed, "scope": "filter_snapshot"}, commit=False)
            if logged is None:
                raise RuntimeError("camera_event_delete_audit_failed")
        db.commit()
    except Exception:
        db.rollback()
        raise
    events.notify_change()
    return changed
