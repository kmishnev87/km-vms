"""Bounded recovery and Recorder evidence from the existing ONVIF receiver."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.models.camera import Camera
from app.services.audit_log import create_event

RETRY_DELAYS = (30, 120, 300, 900) + (3600,) * 24
RECOVERY_STABLE_SECONDS = 120
HEALTH_SAMPLE_SECONDS = 25
EMPTY_PULL_INTERVAL_SECONDS = 30


def utc_iso(now):
    return now.replace(tzinfo=timezone.utc).isoformat()


def pull_delay(elapsed, has_messages):
    return max(0, (1 if has_messages else EMPTY_PULL_INTERVAL_SECONDS) - elapsed)


@dataclass
class Recovery:
    failures: int = 0
    incident_id: str | None = None
    reason: str | None = None
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    next_attempt_at: datetime | None = None
    healthy_since: float | None = None
    exhausted_announced: bool = False

    def failed(self, reason, now):
        transitions = []
        if self.incident_id is None:
            self.incident_id = uuid.uuid4().hex
            self.first_failure_at = now
            transitions.append("failed")
        self.healthy_since = None
        self.reason = reason
        self.last_failure_at = now
        self.failures += 1
        delay = RETRY_DELAYS[self.failures - 1] if self.failures <= len(RETRY_DELAYS) else None
        self.next_attempt_at = now + timedelta(seconds=delay) if delay is not None else None
        if delay is None and not self.exhausted_announced:
            self.exhausted_announced = True
            transitions.append("exhausted")
        return delay, transitions

    def received(self, monotonic_now):
        if self.incident_id is None:
            return None
        if self.healthy_since is None:
            self.healthy_since = monotonic_now
        if monotonic_now - self.healthy_since < RECOVERY_STABLE_SECONDS:
            return None
        incident = self.details()
        self.__dict__.update(Recovery().__dict__)
        return incident

    def details(self):
        if self.incident_id is None:
            return None
        return {
            "id": self.incident_id,
            "reason": self.reason,
            "failed_attempts": self.failures,
            "first_failure_at": utc_iso(self.first_failure_at),
            "last_failure_at": utc_iso(self.last_failure_at),
            "next_attempt_at": utc_iso(self.next_attempt_at) if self.next_attempt_at else None,
        }


def publish_channel(db, group, lease, generation, phase, *, sample, recovery):
    """Caller holds the matching worker lease and the current group row locks."""
    camera_ids = [item["id"] for item in group["cameras"] if item["events_enabled"]]
    if not camera_ids:
        return
    for camera in db.query(Camera).filter(Camera.id.in_(camera_ids)).order_by(Camera.id).all():
        policy = camera.recording_policy
        if (not isinstance(policy, dict) or policy.get("schema_version") != 1
                or policy.get("mode") != "events" or policy.get("source") != "camera"):
            continue
        state = camera.recording_signal_state or {}
        channel = {
            "policy_revision": policy["revision"],
            "generation": generation,
            "lease_key": lease.worker_key,
            "lease_owner": lease.owner_instance_id,
            "lease_fence": lease.fencing_token,
            "phase": phase,
            "sample_at": utc_iso(sample),
            "incident": recovery.details(),
        }
        db.execute(update(Camera).where(Camera.id == camera.id).values(
            recording_signal_state={**state, "channel": channel}, updated_at=Camera.updated_at))


INCIDENT_MESSAGES = {
    "failed": ("warning", "Приём событий камеры прерван", "Camera event reception interrupted"),
    "exhausted": ("error", "Попытки восстановить события камеры исчерпаны", "Camera event recovery attempts exhausted"),
    "recovered": ("info", "Приём событий камеры восстановлен", "Camera event reception restored"),
}


def record_transition(db, group, transition, incident):
    severity, ru, en = INCIDENT_MESSAGES[transition]
    created = create_event(
        db=db, category="system", event_type="camera_events.channel_" + transition,
        severity=severity, message_ru=ru, message_en=en,
        target_type="camera_device", target_id=group["device_id"],
        target_name=" / ".join(item["name"] for item in group["cameras"] if item["events_enabled"])[:255],
        metadata={"incident": incident, "camera_ids": [item["id"] for item in group["cameras"] if item["events_enabled"]]},
        commit=False,
    )
    if created is None:
        raise RuntimeError("camera_event_incident_write_failed")
