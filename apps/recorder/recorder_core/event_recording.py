"""Recording intent only. The existing Recorder owns the writer and the archive."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from recorder_core.ai_supervisor import AI_KINDS

CHANNEL_PROGRESS_TIMEOUT = 150
STALLED_RECOVERY_SECONDS = 120
BUFFERED_PROFILES = ("balanced", "full_quality")
FAULTS = frozenset({"stalled", "failed", "exhausted", "recovering"})


def ai_policy(row):
    policy = getattr(row, "recording_policy", None)
    if (not isinstance(policy, dict) or policy.get("schema_version") != 1 or policy.get("source") != "ai"
            or type(policy.get("revision")) is not int or policy["revision"] < 1 or policy.get("rule_ids")):
        return None
    if policy.get("mode") == "continuous" and policy.get("profile") == "none":
        return policy
    if (policy.get("mode") == "events" and policy.get("profile") in BUFFERED_PROFILES
            and type(policy.get("prebuffer_seconds")) is int and 0 <= policy["prebuffer_seconds"] <= 120
            and type(policy.get("postrecord_minutes")) is int and 1 <= policy["postrecord_minutes"] <= 5
            and isinstance(policy.get("event_kinds"), list)
            and any(isinstance(kind, str) and kind in AI_KINDS for kind in policy["event_kinds"])):
        return policy
    return None


def event_policy(row):
    policy = getattr(row, "recording_policy", None)
    if (isinstance(policy, dict) and policy.get("schema_version") == 1
            and policy.get("mode") == "events" and policy.get("source") == "camera"
            and (policy.get("profile") == "economical"
                 or (policy.get("profile") in BUFFERED_PROFILES
                     and type(policy.get("prebuffer_seconds")) is int and 0 <= policy["prebuffer_seconds"] <= 120))
            and type(policy.get("revision")) is int and policy["revision"] > 0
            and type(policy.get("postrecord_minutes")) is int and 1 <= policy["postrecord_minutes"] <= 5):
        return policy
    return ai_policy(row)


def timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def progress_status(*, expected, since, completed_at, now, timeout):
    """A successful empty result is progress. Idle/scheduled-off sources are neutral."""
    if not expected:
        return "inactive"
    if completed_at is not None and since <= completed_at <= now and now - completed_at < timeout:
        return "healthy"
    return "connecting" if now - since < timeout else "stalled"


@dataclass(frozen=True)
class Decision:
    request: str | None
    source_status: str
    episode_until: float | None = None
    extended: bool = False


@dataclass
class Episode:
    """Deadline stays monotonic once accepted, including through a reconnect."""

    revision: int | None = None
    cutoff: str | None = None
    source_expected_since: float | None = None
    last_activity: float = 0
    deadline: float = 0
    episode_started_at: float | None = None
    episode_until: float | None = None
    extended: bool = False
    stalled: bool = False
    progress_after_stall: float | None = None

    def clear_episode(self, now):
        self.last_activity = max(self.last_activity, now)
        self.deadline = 0
        self.episode_started_at = None
        self.episode_until = None
        self.extended = False

    def _apply_activity(self, activity, policy, now, clock):
        active = self.deadline > clock
        if activity is not None and self.last_activity < activity <= now:
            next_until = activity + policy["postrecord_minutes"] * 60
            self.last_activity = activity
            if next_until > now:
                self.deadline = max(clock + next_until - now, self.deadline)
                if not active:
                    self.episode_started_at = activity
                self.episode_until = max(next_until, self.episode_until or next_until) if active else next_until
                self.extended = active
                active = True
        return active

    def reconcile(self, row, policy, *, now, clock, started, blocked, ai=None):
        state = row.recording_signal_state if isinstance(row.recording_signal_state, dict) else {}
        cutoff = state.get("recording_cutoff_at")
        revision = policy["revision"]
        if (self.revision, self.cutoff) != (revision, cutoff):
            self.revision, self.cutoff = revision, cutoff
            self.source_expected_since = None
            self.last_activity = max(started, timestamp(cutoff) or started)
            self.deadline = 0
            self.episode_started_at = None
            self.episode_until = None
            self.extended = False
            self.stalled = False
            self.progress_after_stall = None
        if blocked or not row.enabled:
            self.clear_episode(now)
        if policy.get("source") == "ai":
            health = ai.phase if ai is not None else "connecting"
            if blocked or not row.enabled:
                return Decision(None, health)
            if policy["mode"] == "continuous":
                return Decision("continuous", health)
            activity = ai.activity if ai is not None else None
            active = self._apply_activity(activity, policy, now, clock)
            emergency = bool(row.event_recording_emergency_enabled and health in FAULTS)
            return Decision("emergency" if emergency else "event" if active else None,
                            health, self.episode_until if active else None, active and self.extended)
        expected = row.protocol == "onvif" and bool(row.events_enabled)
        if not expected:
            self.source_expected_since = None
            self.clear_episode(now)
            self.stalled = False
            self.progress_after_stall = None
            return Decision(None, "inactive")
        if self.source_expected_since is None:
            self.source_expected_since = now

        channel = state.get("channel") or {}
        issued = timestamp(channel.get("sample_at"))
        current = bool(row.event_receiver_current and channel.get("policy_revision") == revision
                       and issued is not None and started < issued <= now)
        phase = channel.get("phase") if current else None
        completed_at = issued if phase in {"healthy", "recovering"} else None
        since = min(self.source_expected_since, completed_at) if completed_at is not None else self.source_expected_since
        health = progress_status(expected=True, since=since, completed_at=completed_at,
                                 now=now, timeout=CHANNEL_PROGRESS_TIMEOUT)
        if phase in {"failed", "exhausted"} or (phase == "recovering" and health == "healthy"):
            health = phase
            self.stalled = False
            self.progress_after_stall = None
        elif health == "stalled":
            self.stalled = True
            self.progress_after_stall = None
        elif self.stalled and health == "healthy":
            if self.progress_after_stall is None:
                self.progress_after_stall = completed_at
            if completed_at - self.progress_after_stall < STALLED_RECOVERY_SECONDS:
                health = "recovering"
            else:
                self.stalled = False
                self.progress_after_stall = None

        if blocked or not row.enabled:
            return Decision(None, health)

        activity = timestamp(state.get("last_activity_at"))
        # A fault after a successfully received pulse does not revoke that
        # pulse. Only this subscription's fenced, post-start activity qualifies.
        current_activity = (activity if current and health in {"healthy", "recovering", "failed", "exhausted"}
                            and state.get("source") == "camera" and state.get("policy_revision") == revision
                            and channel.get("generation") and state.get("source_generation") == channel["generation"] else None)
        active = self._apply_activity(current_activity, policy, now, clock)
        emergency = bool(row.event_recording_emergency_enabled and health in FAULTS)
        request = "emergency" if emergency else "event" if active else None
        return Decision(request, health, self.episode_until if active else None, active and self.extended)
