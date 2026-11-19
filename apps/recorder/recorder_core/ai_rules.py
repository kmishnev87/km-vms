"""Bounded rule evaluation on normalized detector output, not a second video pipeline."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from math import hypot, isfinite
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WIDTH, HEIGHT = 20, 12
MAX_TRACKS = 32
MAX_GAP = 5.0
KINDS = {"person", "vehicle", "animal"}


def oriented(a, b, p):
    return (b[0]-a[0])*(p[1]-a[1]) - (b[1]-a[1])*(p[0]-a[0])


def edges(points):
    return zip(points, points[1:] + points[:1])


def on_segment(a, b, p):
    return (abs(oriented(a, b, p)) < 1e-9 and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def intersects(a, b, c, d):
    o1, o2, o3, o4 = oriented(a, b, c), oriented(a, b, d), oriented(c, d, a), oriented(c, d, b)
    return (o1 * o2 < 0 and o3 * o4 < 0 or on_segment(a, b, c) or on_segment(a, b, d)
            or on_segment(c, d, a) or on_segment(c, d, b))


def contains(points, p):
    x, y = p
    inside = False
    for a, b in edges(points):
        if on_segment(a, b, p):
            return True
        if (a[1] > y) != (b[1] > y) and x < (b[0]-a[0]) * (y-a[1]) / (b[1]-a[1]) + a[0]:
            inside = not inside
    return inside


def excluded(box, points):
    x1, y1, x2, y2 = box
    corners = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    return (any(x1 <= x <= x2 and y1 <= y <= y2 for x, y in points)
            or any(contains(points, p) for p in corners)
            or any(intersects(a, b, c, d) for a, b in edges(points) for c, d in edges(corners)))


def window_open(schedule, local):
    if schedule is None:
        return True
    day = local.isoweekday()
    at = local.hour*60 + local.minute
    start, end = schedule["start"], schedule["end"]
    if start == end:
        return day in schedule["days"]
    if start < end:
        return day in schedule["days"] and start <= at < end
    return ((at >= start and day in schedule["days"])
            or (at < end and ((day-2) % 7 + 1) in schedule["days"]))


def reference_data(token):
    part = token.split(".")[0]
    data = json.loads(base64.urlsafe_b64decode(part + "="*(-len(part) % 4)))
    raw = data["edges"]
    packed = base64.urlsafe_b64decode(raw + "="*(-len(raw) % 4))
    if data["v"] != 1 or len(packed) != 112 or not 50 <= data["ratio"] <= 20000:
        raise ValueError("invalid_reference")
    return data["ratio"], tuple((byte >> bit) & 3 for byte in packed for bit in (0, 2, 4, 6))


def matches_geometry(anchor, size):
    if not anchor or not size or len(size) != 2 or any(type(x) is not int or x <= 0 for x in size):
        return False
    ratio = anchor[0]
    return abs(size[0]/size[1]*1000 - ratio) <= max(16, ratio*.018)


def matches_view(anchor, luma, size):
    if not luma or not matches_geometry(anchor, size):
        return False
    signed = anchor[1]
    diffs = [luma[i+1]-luma[i] for i in range(len(luma)) if i % WIDTH < WIDTH-1]
    diffs.extend(luma[i+WIDTH]-luma[i] for i in range(WIDTH*(HEIGHT-1)))
    known = [(direction, diff) for direction, diff in zip(signed, diffs) if direction]
    return len(known) >= 24 and sum((d >= 6 if s == 1 else d <= -6) for s, d in known)/len(known) >= .77


def motion(before, after, samples, fraction=.40):
    if before is None or after is None or len(samples) < 48:
        return False
    diff = [after[i]-before[i] for i in samples]
    common = median(diff)
    return sum(abs(value-common) > 28 for value in diff) >= len(diff)*fraction


def distance(a, b):
    return hypot(a[0]-b[0], a[1]-b[1])


def overlap(a, b):
    x = max(0, min(a[2], b[2])-max(a[0], b[0]))
    y = max(0, min(a[3], b[3])-max(a[1], b[1]))
    return x*y / max(1e-9, (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - x*y)


@dataclass
class Object:
    kind: str
    confidence: float
    box: tuple
    point: tuple


@dataclass
class Track:
    id: int
    object: Object
    sample: int
    when: float


@dataclass
class Memory:
    stable: bool | int | None = None
    stable_point: tuple | None = None
    pending: bool | int | None = None
    pending_point: tuple | None = None
    since: float = 0
    count: int = 0
    inside_since: float | None = None
    last_activity: float | None = None
    active: bool = False
    pulse: bool = False
    last_sample: int = 0


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    kind: str
    started: bool
    object_kinds: tuple[str, ...] = ()


def units(points):
    return tuple((x/1000, y/1000) for x, y in points)


class RuleEngine:
    def __init__(self, config, timezone, view_kind="ptz"):
        self.config = config
        self.view_kind = view_kind
        try:
            self.zone = ZoneInfo(timezone or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            self.zone = ZoneInfo("UTC")
        self.rule_rows = [(rule, units(rule["points"])) for rule in config["rules"][:8]]
        self.masks = tuple(units(points) for points in config["exclusions"][:4])
        self.spatial = bool(self.masks or any(points for _, points in self.rule_rows))
        self.scene = any(rule["kind"] == "scene" for rule, _ in self.rule_rows)
        try:
            self.anchor = reference_data(config["reference"]) if self.spatial else None
        except (AttributeError, TypeError, ValueError, KeyError):
            self.anchor = None
        self.scene_samples = tuple(i for i in range(WIDTH*HEIGHT)
            if not any(contains(points, ((i%WIDTH+.5)/WIDTH, (i//WIDTH+.5)/HEIGHT)) for points in self.masks))
        self.sequence = self.track_id = 0
        self.reset()

    def reset(self):
        self.tracks = {}
        self.memory = {}
        self.before = None
        self.last = None
        self.match_count = 0
        self.view = ("checking_profile" if self.view_kind == "unknown" else "checking_view") if self.spatial else "whole_frame"

    def set_view_kind(self, kind):
        if kind != self.view_kind:
            self.view_kind = kind
            self.reset()

    def objects(self, result):
        found = []
        for value in result.get("objects", [])[:MAX_TRACKS]:
            box = value.get("box") or ()
            confidence = value.get("confidence", 0)
            if (value.get("kind") not in KINDS or not isinstance(confidence, (int, float)) or not isfinite(confidence)
                    or len(box) != 4 or not all(isinstance(x, (int, float)) and isfinite(x) for x in box)):
                continue
            a, b, c, d = box
            if not (0 <= a < c <= 1 and 0 <= b < d <= 1) or any(excluded(box, mask) for mask in self.masks):
                continue
            found.append(Object(value["kind"], confidence, tuple(box), ((a+c)/2, d)))
        return found

    def update_tracks(self, found, when):
        old = {i: item for i, item in self.tracks.items() if 0 <= when-item.when <= MAX_GAP}
        candidates, alternatives = {}, {}
        for i, target in enumerate(found):
            for track_id, track in old.items():
                # Never bridge a wholly missing sample for lines, entry or dwell.
                if target.kind != track.object.kind or track.sample != self.sequence-1:
                    continue
                delta = distance(target.point, track.object.point)
                iou = overlap(target.box, track.object.box)
                if delta <= .22 and (iou >= .10 or delta <= .13):
                    rank = delta + .12*(1-iou)
                    candidates.setdefault(i, []).append((rank, track_id))
                    alternatives.setdefault(track_id, []).append((rank, i))
        tracks = {}
        for i, target in enumerate(found):
            options = sorted(candidates.get(i, ()))
            match = None
            if options:
                rank, track_id = options[0]
                rivals = sorted(alternatives[track_id])
                if (rivals[0][1] == i and (len(rivals) == 1 or rivals[1][0]-rank >= .035)
                        and (len(options) == 1 or options[1][0]-rank >= .035)):
                    match = track_id
            if match is None:
                self.track_id += 1
                match = self.track_id
            tracks[match] = Track(match, target, self.sequence, when)
        self.tracks = tracks
        return tracks

    def advance(self, rule, points, track, when):
        kind, rule_id = rule["kind"], rule["id"]
        obj = track.object
        key = (rule_id, track.id)
        memo = self.memory.setdefault(key, Memory())
        memo.last_sample = self.sequence
        value = contains(points, obj.point) if points else True
        if kind == "crossing":
            a, b = points
            offset = oriented(a, b, obj.point) / distance(a, b)
            if abs(offset) < .012:
                return False, False
            value = 1 if offset > 0 else -1
        old, old_point = memo.stable, memo.stable_point
        if value != memo.pending:
            memo.pending, memo.pending_point, memo.since, memo.count = value, obj.point, when, 1
        else:
            memo.count += 1
        threshold = rule["confirm_seconds"] if value or kind == "crossing" else rule["release_seconds"]
        pulse = False
        if memo.count >= 2 and when-memo.since >= threshold:
            memo.stable, memo.stable_point = value, obj.point
            if kind == "crossing" and old is not None and old != value:
                direction = "forward" if value > old else "reverse"
                pulse = (rule["direction"] in {"both", direction}
                         and distance(old_point, memo.pending_point) <= .24
                         and intersects(old_point, memo.pending_point, *points))
            elif old != value:
                if kind == "enter" and old is False and value is True:
                    pulse = True
                elif kind == "exit" and old is True and value is False:
                    pulse = True
                memo.inside_since = memo.since if value is True else None
        if kind not in {"presence", "dwell"}:
            return pulse, pulse
        qualifying = (memo.stable is True and value is True and memo.inside_since is not None
                      and (kind == "presence" or when-memo.inside_since >= rule["dwell_seconds"]))
        if qualifying:
            memo.last_activity = when
        keep = (memo.active and memo.last_activity is not None and memo.stable is True
                and when-memo.last_activity <= rule["release_seconds"])
        active = qualifying or keep
        started = active and not memo.active
        memo.active = active
        return active, started

    def process(self, result, when):
        if self.last is not None and not 0 < when-self.last <= MAX_GAP:
            self.reset()
        self.last = when
        self.sequence += 1
        local = datetime.fromtimestamp(when, self.zone)
        try:
            luma = bytes.fromhex(result.get("luma", ""))
            luma = luma if len(luma) == WIDTH*HEIGHT else None
        except ValueError:
            luma = None
        scene_shift = (self.scene or self.view_kind == "ptz") and motion(self.before, luma, self.scene_samples)
        self.before = luma
        was_aligned = self.view == "aligned"
        same_view = not self.spatial
        if self.spatial:
            size = result.get("size")
            if self.view_kind == "fixed":
                same_view = matches_geometry(self.anchor, size)
                self.view = "aligned" if same_view else "view_unconfirmed"
            elif self.view_kind == "ptz":
                same_view = matches_view(self.anchor, luma, size)
                aligned = same_view and not scene_shift
                self.match_count = min(self.match_count+1, 2) if aligned else 0
                self.view = "aligned" if self.match_count == 2 else "checking_view" if aligned else "view_unconfirmed"
            else:
                self.view = "checking_profile"
        # A brightness step on a fixed lens is not a camera move. An explicit
        # scene-change rule may still fire; it need not reset object rules.
        if scene_shift and (not self.spatial or self.view_kind == "ptz") or was_aligned and not same_view:
            self.tracks.clear()
            self.memory.clear()
        found = self.objects(result) if self.view in {"aligned", "whole_frame"} or not self.masks else []
        tracks = self.update_tracks(found, when)
        hits = []
        legal = set()
        for rule, points in self.rule_rows:
            if not window_open(rule["schedule"], local):
                continue
            kind = rule["kind"]
            active = started = False
            starting_objects = set()
            if kind == "scene":
                # Masked scenes cannot report pixel changes from an unverified view.
                active = started = scene_shift and (not self.masks or was_aligned and same_view)
            elif not points or self.view == "aligned":
                threshold = rule["confidence"]/100
                for track_id, track in tracks.items():
                    if track.object.kind in rule["classes"] and track.object.confidence >= threshold:
                        key = (rule["id"], track_id)
                        legal.add(key)
                        if key in self.memory and self.memory[key].last_sample != self.sequence-1:
                            self.memory.pop(key)
                        seen, began = self.advance(rule, points, track, when)
                        active |= seen
                        started |= began
                        if began:
                            starting_objects.add(track.object.kind)
            if active:
                hits.append(RuleHit(rule["id"], kind, started, tuple(sorted(starting_objects))))
        held = set()
        for key in sorted(self.memory.keys() - legal, key=lambda item: self.memory[item].last_activity or 0, reverse=True):
            memo = self.memory[key]
            # A short disappearance can delay ending only an already active
            # object rule. It can never invent an exit or a crossing.
            rule = next((item for item, points in self.rule_rows if item["id"] == key[0]
                         and item["kind"] in {"presence", "dwell"}
                         and (not points or self.view == "aligned")
                         and (not self.masks or self.view == "aligned")
                         and window_open(item["schedule"], local)), None)
            if (rule and rule["id"] not in held and memo.active and memo.last_activity is not None
                    and when-memo.last_activity <= rule["release_seconds"]):
                held.add(rule["id"])
                if not any(hit.rule_id == rule["id"] for hit in hits):
                    hits.append(RuleHit(rule["id"], rule["kind"], False))
            else:
                self.memory.pop(key)
        return hits
