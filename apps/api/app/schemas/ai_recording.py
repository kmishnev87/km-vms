"""Versioned vendor/model-neutral policy and bounded detection evidence."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from app.services.onvif_events import EVENT_KINDS

Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Kind = Literal["motion", "person", "vehicle", "animal", "intrusion", "line_crossing", "loitering", "tamper", "audio", "wrong_direction", "fire"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

Coordinate = Annotated[StrictInt, Field(ge=0, le=1000)]
Point = tuple[Coordinate, Coordinate]
Class = Literal["person", "vehicle", "animal"]
Minute = Annotated[StrictInt, Field(ge=0, le=1439)]
Day = Annotated[StrictInt, Field(ge=1, le=7)]


def _orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _intersects(a, b, c, d):
    if (max(min(a[0], b[0]), min(c[0], d[0])) > min(max(a[0], b[0]), max(c[0], d[0]))
            or max(min(a[1], b[1]), min(c[1], d[1])) > min(max(a[1], b[1]), max(c[1], d[1]))):
        return False
    return _orientation(a, b, c) * _orientation(a, b, d) <= 0 and _orientation(c, d, a) * _orientation(c, d, b) <= 0


def polygon(points):
    edges = list(zip(points, (*points[1:], points[0]))) if points else []
    area = abs(sum(x * v - y * u for (x, y), (u, v) in edges))
    if not 3 <= len(points) <= 10 or len(set(points)) != len(points) or area < 800:
        raise ValueError("invalid_ai_polygon")
    for i, (a, b) in enumerate(edges):
        for j in range(i+2, len(edges)):
            if (i != 0 or j != len(edges)-1) and _intersects(a, b, *edges[j]):
                raise ValueError("self_intersecting_ai_polygon")
    return points


class AiSchedule(StrictModel):
    days: list[Day] = Field(min_length=1, max_length=7)
    start: Minute = 0
    end: Minute = 0  # Equal endpoints mean the full selected day.

    @field_validator("days")
    @classmethod
    def distinct_days(cls, days):
        if len(set(days)) != len(days):
            raise ValueError("duplicate_ai_schedule_day")
        return sorted(days)


class AiRule(StrictModel):
    id: Identifier
    name: str = Field(default="", max_length=48)
    kind: Literal["presence", "enter", "exit", "crossing", "dwell", "scene"]
    scene_objects: StrictBool = False
    classes: list[Class] = Field(default_factory=lambda: ["person"], max_length=3)
    points: list[Point] = Field(default_factory=list, max_length=10)
    direction: Literal["both", "forward", "reverse"] = "both"
    confidence: StrictInt = Field(default=50, ge=35, le=95)
    confirm_seconds: StrictInt = Field(default=0, ge=0, le=20)
    release_seconds: StrictInt = Field(default=2, ge=0, le=15)
    dwell_seconds: StrictInt = Field(default=10, ge=3, le=300)
    schedule: AiSchedule | None = None
    # Retained on the wire for old clients and rollback only. The recording
    # scenario and personal alert preferences now determine the actions.
    record: StrictBool = True
    notify: StrictBool = True

    @field_validator("record", "notify")
    @classmethod
    def compatibility_action(cls, _value):
        return True

    @model_validator(mode="after")
    def valid_rule(self):
        if self.scene_objects and self.kind != "scene":
            raise ValueError("scene_objects_requires_scene_rule")
        if (self.kind != "scene" or self.scene_objects) and (not self.classes or len(set(self.classes)) != len(self.classes)):
            raise ValueError("ai_rule_classes_required")
        if self.kind != "crossing" and self.direction != "both":
            raise ValueError("direction_requires_ai_line")
        if self.kind == "crossing":
            if len(self.points) != 2 or sum((a-b)**2 for a, b in zip(*self.points)) < 2500:
                raise ValueError("invalid_ai_line")
        elif self.kind in {"enter", "exit", "dwell"} or self.kind == "presence" and self.points:
            polygon(self.points)
        elif self.points:
            raise ValueError("unexpected_ai_geometry")
        return self


class AiRules(StrictModel):
    rules: list[AiRule] = Field(min_length=1, max_length=8)
    exclusions: list[Annotated[list[Point], Field(min_length=3, max_length=10)]] = Field(default_factory=list, max_length=4)
    reference: str | None = Field(default=None, max_length=1024, pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def valid_rules(self):
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("duplicate_ai_rule_id")
        for points in self.exclusions:
            polygon(points)
        if (self.exclusions or any(rule.points for rule in self.rules)) and not self.reference:
            raise ValueError("ai_frame_reference_required")
        return self


class RecordingPolicy(StrictModel):
    mode: Literal["continuous", "events", "linked"] = "continuous"
    source: Literal["none", "camera", "ai"] = "none"
    profile: Literal["none", "economical", "balanced", "full_quality"] = "none"
    prebuffer_seconds: StrictInt = Field(default=0, ge=0, le=120)
    postrecord_minutes: StrictInt = Field(default=1, ge=1, le=5)
    event_kinds: list[Kind] = Field(default_factory=lambda: list(EVENT_KINDS), min_length=1, max_length=len(EVENT_KINDS))
    rule_ids: list[Identifier] = Field(default_factory=list, max_length=32)
    ai_rules: AiRules | None = None
    linked_camera_id: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_combination(self):
        if len(set(self.event_kinds)) != len(self.event_kinds) or len(set(self.rule_ids)) != len(self.rule_ids):
            raise ValueError("duplicate_recording_rules")
        if self.mode == "continuous":
            if self.profile != "none" or self.prebuffer_seconds or self.linked_camera_id is not None:
                raise ValueError("continuous_policy_has_no_event_profile_or_link")
        elif self.mode == "events":
            if self.source == "none" or self.profile == "none" or self.linked_camera_id is not None:
                raise ValueError("event_source_and_profile_required")
            if self.source == "ai" and self.profile == "economical":
                raise ValueError("ai_requires_continuous_video_input")
        elif self.source != "none" or self.linked_camera_id is None or self.profile == "none":
            raise ValueError("linked_policy_requires_explicit_camera_and_profile")
        if self.profile in {"none", "economical"} and self.prebuffer_seconds:
            raise ValueError("profile_does_not_have_prebuffer")
        return self


class PolicyUpdate(StrictModel):
    expected_revision: StrictInt = Field(ge=0)
    policy: RecordingPolicy
    # Explicit for new clients; an older editor always sent a neutral 1 for
    # continuous mode and must not erase a chosen notification interval.
    alert_quiet_minutes: StrictInt | None = Field(default=None, ge=1, le=5)


class EmergencyUpdate(StrictModel):
    expected_enabled: StrictBool
    enabled: StrictBool


class RecordingSignal(StrictModel):
    source: Literal["camera", "ai"]
    source_generation: Identifier
    identity: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    kind: Kind
    matched_kinds: list[Kind] = Field(default_factory=list, max_length=len(EVENT_KINDS))
    rule_id: Identifier | None = None
    active: StrictBool | None
    initialized: StrictBool = False
    occurred_at: datetime
    source_at: datetime | None = None
    clock_uncertain: StrictBool = False


class DetectionObject(StrictModel):
    kind: Literal["person", "vehicle", "animal"]
    confidence: float = Field(ge=0, le=1)
    # Normalized coordinates, not a camera/model-specific pixel convention.
    box: tuple[float, float, float, float]

    @model_validator(mode="after")
    def valid_box(self):
        x1, y1, x2, y2 = self.box
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError("normalized_box_required")
        return self


class DetectionEvidence(StrictModel):
    schema_version: Literal[1] = 1
    model_id: Identifier
    model_version: Identifier
    rule_id: Identifier
    rule_version: StrictInt = Field(ge=1)
    occurred_at: datetime
    objects: list[DetectionObject] = Field(default_factory=list, max_length=32)
    recording_id: StrictInt | None = Field(default=None, ge=1)
    # Opaque owned artifact identity only; no path/URL/base64 or raw frame input.
    snapshot_id: UUID | None = None

    @field_validator("occurred_at")
    @classmethod
    def explicit_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value

    @model_validator(mode="after")
    def bounded_serialization(self):
        if len(self.model_dump_json().encode("utf-8")) > 8192:
            raise ValueError("detection_evidence_too_large")
        return self
