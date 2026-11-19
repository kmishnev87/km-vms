"""Server-owned admission for camera and local AI attention, not delivery.

The caller locks one owning camera before accepting a pulse. The episode
continues independently of its history row, app sessions, and archive files.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update

from app.models.audit_event import AuditEvent
from app.models.camera import Camera

MEMBERS = ("alert_episode", "device_alert_episode")
DEFAULT_MINUTES = 1


def settings_from(policy):
    value = (policy or {}).get("postrecord_minutes")
    return value if type(value) is int and 1 <= value <= 5 else DEFAULT_MINUTES


def epoch(value):
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def timestamp(value):
    return value if type(value) in (float, int) and 0 < value < 4_102_444_800 else None


def _current(state, source, device):
    member = "device_alert_episode" if device is not None else "alert_episode"
    old = state.get(member)
    if not isinstance(old, dict) or old.get("source") != source or old.get("device") != device or not old.get("event_id"):
        return member, {}
    return member, old


def is_replay(camera, *, source, device, fingerprint):
    _, old = _current(camera.recording_signal_state or {}, source, device)
    return fingerprint is not None and fingerprint in (old.get("seen") or [])


@dataclass(frozen=True)
class Admission:
    opening: bool
    until: float
    last: float

    @property
    def metadata(self):
        return {"alert_episode_until": self.until, "last_seen_at": iso(self.last), "active": True}


def admit(db, camera, *, source, at, minutes, identifier=None, device=None, fingerprint=None):
    at = timestamp(at)
    if at is None:
        return None
    state = dict(camera.recording_signal_state or {})
    member, old = _current(state, source, device)
    if fingerprint and fingerprint in (old.get("seen") or []):
        return None  # An identical transport replay is not activity.
    last = timestamp(old.get("last"))
    if last is not None and at <= last:
        return None  # Out-of-order progress cannot reopen an episode.
    until = timestamp(old.get("until"))
    opening = until is None or at >= until
    if opening and identifier is None:
        return None  # Only a qualifying event, not a progress pulse, can open it.
    admission = Admission(opening, at + minutes * 60, at)
    event_id = identifier if opening else old["event_id"]
    seen = list(old.get("seen") or [])
    if fingerprint:
        seen = [*seen[-15:], fingerprint]
    state[member] = {"source": source, "device": device, "last": at, "until": admission.until, "event_id": event_id,
                     **({"seen": seen} if seen else {})}
    db.execute(update(Camera).where(Camera.id == camera.id).values(
        recording_signal_state=state, updated_at=Camera.updated_at))
    if not opening:
        row = db.query(AuditEvent).filter(AuditEvent.id == event_id, AuditEvent.category == "camera_event").with_for_update().first()
        if row is not None:
            row.event_metadata = {**(row.event_metadata or {}), **admission.metadata}
    return admission
