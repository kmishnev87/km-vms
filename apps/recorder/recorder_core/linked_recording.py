"""Resolve linked recording intent without depending on a camera's writer."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from .event_recording import BUFFERED_PROFILES, Decision

SOURCE_EVENT_FAULTS = ("failed", "stalled", "exhausted", "recovering")


def link_configured(row):
    policy = getattr(row, "recording_policy", None)
    return isinstance(policy, dict) and policy.get("mode") == "linked"


def linked_policy(row):
    if not link_configured(row):
        return None
    policy = row.recording_policy
    seconds = policy.get("prebuffer_seconds", 0)
    if (policy.get("schema_version") == 1 and policy.get("source") == "none"
            and type(policy.get("revision")) is int and policy["revision"] > 0
            and type(policy.get("linked_camera_id")) is int and policy["linked_camera_id"] > 0
            and type(seconds) is int and 0 <= seconds <= 120
            and (policy.get("profile") in BUFFERED_PROFILES
                 or (policy.get("profile") == "economical" and seconds == 0))):
        return policy
    return None


def present(row):
    return row is not None and getattr(row, "deleted_at", None) is None and getattr(row, "status", None) != "deleting"


def cutoff(row):
    state = getattr(row, "recording_signal_state", None)
    value = state.get("recording_cutoff_at") if isinstance(state, dict) else None
    return value if isinstance(value, str) else None


def generation(row, upstream=""):
    policy = getattr(row, "recording_policy", None)
    policy = policy if isinstance(policy, dict) else {}
    payload = (row.id, present(row), bool(row.enabled), row.protocol, row.recording_mode,
               bool(getattr(row, "events_enabled", False)), policy.get("mode"), policy.get("revision"),
               policy.get("linked_camera_id"), cutoff(row), upstream)
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Intent:
    origin_id: int | None
    mode: str
    decision: Decision
    generation: str = ""
    warm: bool = False
    started_at: float | None = None
    deadline: float | None = None
    event_health: str | None = None

    def allows_start(self, clock):
        return bool(self.decision.request and
                    (self.decision.request != "event" or (self.deadline is not None and clock < self.deadline)))


def stopped(row, reason, mode="unavailable"):
    return Intent(row.id if row is not None else None, mode, Decision(None, reason),
                  generation(row) if row is not None else "")


def origin_intent(row, *, suspended, decision=None, episode=None):
    """Expose scheduler intent; a missing segment or a stream fault is not a pause."""
    policy = getattr(row, "recording_policy", None) or {}
    is_continuous_ai = decision is not None and policy.get("source") == "ai" and policy.get("mode") == "continuous"
    mode = ("continuous" if is_continuous_ai else "events" if decision is not None
            else "continuous" if row.recording_mode == "always" else "idle")
    if not present(row):
        return stopped(row, "source_unavailable")
    if not row.enabled:
        return stopped(row, "source_paused", mode)
    if suspended:
        return stopped(row, "source_storage_suspended", mode)
    if decision is None:
        if mode != "continuous":
            return stopped(row, "source_waiting", mode)
        return Intent(row.id, mode, Decision("continuous", "following"), generation(row))

    health = decision.source_status
    if health == "inactive":
        reason = "source_events_disabled"
    elif decision.request:
        reason = "following"
    elif health in SOURCE_EVENT_FAULTS:
        reason = "source_event_fault"
    else:
        reason = "source_waiting"
    return Intent(row.id, mode, replace(decision, source_status=reason), generation(row),
                  warm=health != "inactive", started_at=episode.episode_started_at if decision.episode_until else None,
                  deadline=episode.deadline if decision.request == "event" else None, event_health=health)


def resolve_links(rows, origins):
    """Iterative, memoized resolution. Damaged/restored graph components fail closed."""
    by_id = {row.id: row for row in rows}
    intents = dict(origins)
    linked = {}
    for row in rows:
        if not link_configured(row):
            continue
        linked[row.id] = linked_policy(row)
        if not present(row):
            intents[row.id] = stopped(row, "source_unavailable")
        elif not row.enabled:
            intents[row.id] = stopped(row, "source_paused")
        elif linked[row.id] is None:
            intents[row.id] = stopped(row, "link_invalid")

    for camera_id in linked:
        current = camera_id
        path = []
        seen = set()
        while current not in intents:
            if current in seen:
                intents[current] = stopped(by_id[current], "link_cycle")
                break
            policy = linked.get(current)
            if not policy:  # Missing or no longer visible to this recorder.
                intents[current] = stopped(by_id.get(current), "source_unavailable")
                break
            seen.add(current)
            path.append(current)
            current = policy["linked_camera_id"]

        upstream = intents[current]
        for dependent_id in reversed(path):
            if dependent_id not in intents:
                upstream = replace(upstream, generation=generation(by_id[dependent_id], upstream.generation))
                intents[dependent_id] = upstream
            else:
                upstream = intents[dependent_id]
    return {key: intents[key] for key in linked}


def link_status(intent):
    if intent.decision.request is not None:
        return None
    if intent.decision.source_status in ("source_unavailable", "link_cycle", "link_invalid", "source_events_disabled"):
        return "linked_source_unavailable"
    if intent.decision.source_status == "source_event_fault":
        return "linked_source_fault"
    return "waiting_linked_source"
