"""Versioned mobile preferences in the existing per-user JSON store.

Like camera_events preferences, these are user-owned state, not camera schema.
Use a private, non-UI namespace; the endpoint and encryption keys are additionally
encrypted with the installation key. No published DDL/migration is changed.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime

from app.models.workspace_layout import UserWorkspaceLayout

PREFIX = "mobile_push:"


@dataclass
class Device:
    row: UserWorkspaceLayout
    label: str
    revision: int
    configuration_hash: str
    subscription_encrypted: str | None
    enabled: bool
    registered_at: datetime
    cursor_at: datetime
    cursor_id: str
    retry_at: datetime | None = None
    failures: int = 0
    last_status: str = "registered"
    last_sent_at: datetime | None = None
    last_test_at: datetime | None = None
    badge_enabled: bool = False
    badge_epoch: str | None = None
    badge_revision: int = -1
    badge_count: int | None = None
    badge_retry_at: datetime | None = None
    badge_failures: int = 0

    @property
    def id(self):
        return self.row.id

    @property
    def user_id(self):
        return self.row.user_id

    @property
    def device_id(self):
        return self.row.workspace_key[len(PREFIX):]

    def save(self):
        values = {}
        for field in fields(self):
            if field.name == "row":
                continue
            value = getattr(self, field.name)
            values[field.name] = value.isoformat() if isinstance(value, datetime) else value
        self.row.layout = values
        self.row.layout_version = 1


def query(db):
    return db.query(UserWorkspaceLayout).filter(UserWorkspaceLayout.workspace_key.startswith(PREFIX, autoescape=True))


def decode(row):
    if row is None or not row.workspace_key.startswith(PREFIX) or row.layout_version != 1:
        return None
    value = dict(row.layout or {})
    try:
        for name in ("registered_at", "cursor_at", "retry_at", "last_sent_at", "last_test_at", "badge_retry_at"):
            if value.get(name) is not None:
                value[name] = datetime.fromisoformat(value[name])
        if type(value["enabled"]) is not bool or type(value["revision"]) is not int or type(value.get("badge_enabled", False)) is not bool:
            return None
        return Device(row=row, **value)
    except (ValueError, TypeError, KeyError):
        return None


def get(db, identifier, *, lock=False):
    candidate = query(db).filter(UserWorkspaceLayout.id == identifier)
    return decode((candidate.with_for_update() if lock else candidate).first())


def by_user(db, user_id, device_id, *, lock=False):
    candidate = query(db).filter(UserWorkspaceLayout.user_id == user_id, UserWorkspaceLayout.workspace_key == PREFIX + device_id)
    return decode((candidate.with_for_update() if lock else candidate).first())
