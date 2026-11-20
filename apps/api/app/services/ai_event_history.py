"""Private local inbox → the existing camera-event history, never a public ingest API."""
from __future__ import annotations

from bisect import bisect_left
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
from sqlalchemy import and_, or_

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.services import camera_alert_episodes as episodes, camera_events as events
from app.services.ai_recording import ai_policy_supported
from app.services.ai_rule_frame import jpeg_size

ORIGIN = "nas_ai"
MAX_JPEG = 128 * 1024
MAX_FRAMES = 4096
OBJECTS = ("person", "vehicle", "animal")
RULE_KINDS = {"enter": "intrusion", "exit": "intrusion", "crossing": "line_crossing", "dwell": "loitering", "scene": "motion"}
_HEX = re.compile(r"[a-f0-9]{32}\Z")
_stop = threading.Event()
_thread = None
_log = logging.getLogger(__name__)


def root():
    return Path(settings.storage_previews) / ".ai-events"


def frame_path(event_id):
    return root() / "frames" / (UUID(str(event_id)).hex + ".jpg")


def _owned_recording_id(db, camera_id, instant):
    row = db.query(RecordingSegment.id).filter(
        RecordingSegment.camera_id == camera_id, RecordingSegment.deleted_at.is_(None),
        RecordingSegment.source == "recorder", RecordingSegment.ownership == "KM VMS",
        RecordingSegment.status.in_(("ready", "finalized")), RecordingSegment.finalized_at.isnot(None),
        RecordingSegment.started_at <= instant, RecordingSegment.ended_at > instant,
    ).order_by(RecordingSegment.started_at.desc(), RecordingSegment.id.desc()).first()
    return row[0] if row else None


def _link_recordings(db, pending):
    """Resolve one bounded batch, also recognizing a finalized file since deleted."""
    if not pending:
        return {}
    ranges = {}
    for camera_id, instant, event_id in pending:
        ranges.setdefault(camera_id, []).append((instant, event_id))
    windows = []
    for camera_id, values in ranges.items():
        values.sort()
        windows.append(and_(RecordingSegment.camera_id == camera_id, RecordingSegment.started_at <= values[-1][0],
                            RecordingSegment.ended_at > values[0][0]))
    rows = (db.query(RecordingSegment.id, RecordingSegment.camera_id, RecordingSegment.started_at,
                     RecordingSegment.ended_at, RecordingSegment.deleted_at, RecordingSegment.status)
            .filter(RecordingSegment.source == "recorder", RecordingSegment.ownership == "KM VMS",
                    RecordingSegment.status.in_(("ready", "finalized", "deleted")),
                    RecordingSegment.finalized_at.isnot(None), or_(*windows))
            .order_by(RecordingSegment.started_at.desc(), RecordingSegment.id.desc()).yield_per(128))
    result = {}
    for seg in rows:
        values = ranges[seg.camera_id]
        start, end = bisect_left(values, (seg.started_at, "")), bisect_left(values, (seg.ended_at, ""))
        for _, event_id in values[start:end]:
            # A covering file that is still retained wins over a tombstone.
            lost = seg.deleted_at is not None or seg.status == "deleted"
            if event_id not in result or (result[event_id][1] and not lost):
                result[event_id] = (seg.id, lost)
        if len(result) == len(pending) and all(not lost for _, lost in result.values()):
            break
    return result


def _header(handle):
    line = handle.readline(4097)
    if len(line) > 4096 or not line.endswith(b"\n"):
        raise ValueError("invalid_ai_event_header")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("invalid_ai_event_envelope")
    return value


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid_ai_event_file")
    with path.open("rb") as handle:
        header = _header(handle)
        jpeg = handle.read(MAX_JPEG + 1)
    if len(jpeg) > MAX_JPEG:
        raise ValueError("invalid_ai_event_envelope")
    if jpeg and (not jpeg.endswith(b"\xff\xd9") or not jpeg_size(jpeg, 640, 360)):
        jpeg = b""  # An invalid still cannot remove an otherwise valid event.
    return header, jpeg


def _queued_events(paths):
    # A final progress pulse and the next opening can be written in the same
    # filesystem clock tick. Order by captured video time, not file mtime or UUID.
    ordered = []
    for changed, path in paths:
        try:
            with path.open("rb") as handle:
                captured = episodes.timestamp(_header(handle).get("captured"))
            if captured is None:
                raise ValueError("invalid_ai_event_timestamp")
            ordered.append((captured, changed, path))
        except (ValueError, FileNotFoundError):
            path.unlink(missing_ok=True)
    return [path for _, _, path in sorted(ordered)]


def _sensor_identity(camera, value):
    policy = camera.recording_policy
    rule_id, condition, objects = value.get("rule_id"), value.get("condition"), value.get("objects")
    if (not isinstance(objects, list) or len(objects) > 3 or any(kind not in OBJECTS for kind in objects)
            or not isinstance(condition, str)):
        return None
    kinds = [kind for kind in OBJECTS if kind in objects]
    config = policy.get("ai_rules")
    if config:
        if not isinstance(rule_id, str) or len(rule_id) > 64:
            return None
        rule = next((item for item in config.get("rules", []) if item["id"] == rule_id), None)
        if not rule or not set(kinds).issubset(rule["classes"]):
            return None
        scene_objects = rule["kind"] == "scene" and rule.get("scene_objects") is True and condition == "presence"
        if rule["kind"] != condition and not scene_objects:
            return None
        name = rule.get("name", "")
        scope = "line" if condition == "crossing" else "zone" if rule.get("points") or condition in {"enter", "exit", "dwell"} else "frame"
    else:
        selected = policy.get("event_kinds") or []
        if rule_id is not None or condition not in {"presence", "scene"} or not set(kinds).issubset(selected):
            return None
        if condition == "scene" and "motion" not in selected:
            return None
        name, scope = "", "frame"
    if (condition == "scene") != (not kinds) or condition not in ("presence", *RULE_KINDS):
        return None
    return rule_id, condition, kinds, str(name or "")[:48], scope


def ingest(db, path, *, received=None):
    """The file is removed by the caller only after a commit or a permanent rejection."""
    identifier = str(UUID(hex=path.stem))
    value, jpeg = _read(path)
    received = received or events.now_utc()
    camera_id, revision, captured = value.get("camera_id"), value.get("revision"), value.get("captured")
    if (value.get("v") not in (1, 2) or type(camera_id) is not int or not 1 <= camera_id <= 2**31 - 1
            or type(revision) is not int or revision < 1 or type(captured) not in (int, float)):
        return False
    try:
        occurred = datetime.fromtimestamp(captured, timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError, OSError):
        return False
    if not received - timedelta(minutes=10) <= occurred <= received + timedelta(seconds=5):
        return False
    camera = db.query(Camera).filter(Camera.id == camera_id).populate_existing().with_for_update().first()
    if (camera is None or camera.deleted_at is not None or camera.status == "deleting" or not camera.enabled
            or not ai_policy_supported(camera) or camera.recording_policy["revision"] != revision):
        return False
    minutes = episodes.settings_from(camera.recording_policy)
    if value.get("v") == 2 and value.get("progress") is True:
        episodes.admit(db, camera, source="ai", at=captured, minutes=minutes)
        db.commit()
        return False   # Extends the current window, never adds a history row or wakes push.
    resolved = _sensor_identity(camera, value)
    if not resolved or db.get(AuditEvent, identifier) is not None:
        return False
    rule_id, condition, kinds, rule_name, scope = resolved
    kind = (kinds[0] if condition == "presence" else RULE_KINDS[condition])
    admitted = episodes.admit(db, camera, source="ai", at=captured, minutes=minutes, identifier=identifier)
    if admitted is None or not admitted.opening:
        db.commit()
        return False
    picture_saved = False
    if jpeg:
        destination = frame_path(identifier)
        temporary = destination.with_suffix(".part")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as output:
                output.write(jpeg)
            os.replace(temporary, destination)
            picture_saved = True
        except OSError:
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    recording_id = _owned_recording_id(db, camera_id, occurred) if picture_saved else None
    row = AuditEvent(id=identifier, created_at=received, category=events.CATEGORY, event_type="camera." + kind,
                     severity="info", message_ru="Событие AI", message_en="AI event", target_type="camera",
                     target_id=str(camera_id), target_name=camera.name, event_metadata={
                         "origin": ORIGIN, "ai_condition": condition, "ai_rule_id": rule_id, "ai_rule_name": rule_name, "ai_scope": scope,
                         "ai_snapshot": picture_saved, "ai_notify": True, "ai_record": camera.recording_policy["mode"] == "events",
                         **({"ai_recording_id": recording_id} if recording_id is not None else {}),
                         "ai_captured_epoch": captured,
                         "occurred_at": occurred.replace(tzinfo=timezone.utc).isoformat(), "occurrences": 1,
                         "rule_kind": RULE_KINDS.get(condition), "object_kinds": kinds,
                         "object_kind": kinds[0] if kinds else None, **admitted.metadata,
                     })
    db.add(row)
    db.flush()
    from app.services.mobile_badges import touch
    from app.services.mobile_push import signal_change, wake_local

    events.auto_read_new_event(db, row, received)
    touch(db)
    signal_change(db)
    db.commit()
    events.notify_change()
    wake_local()
    return True


def _sweep_frames(db):
    path = root() / "frames"
    if not path.is_dir():
        return
    now = time.time()
    cutoff = now - 300
    files = []
    with os.scandir(path) as entries:
        for entry in entries:
            name, sep, suffix = entry.name.rpartition(".")
            if sep and _HEX.fullmatch(name) and entry.is_file(follow_symlinks=False):
                changed = entry.stat(follow_symlinks=False).st_mtime
                if suffix == "jpg":
                    files.append((changed, name, Path(entry.path)))
                elif suffix == "part" and changed < cutoff:
                    Path(entry.path).unlink(missing_ok=True)
    files.sort()
    excess = max(0, len(files) - MAX_FRAMES)
    for start in range(0, len(files), 256):
        batch = files[start:start + 256]
        row_ids = [str(UUID(hex=name)) for _, name, _ in batch]
        rows = {row.id: row for row in events.event_query(db).filter(AuditEvent.id.in_(row_ids))
                .order_by(AuditEvent.created_at, AuditEvent.id).with_for_update().all()}
        camera_ids = [int(row.target_id) for row in rows.values() if str(row.target_id).isdigit()]
        living = dict(db.query(Camera.id, Camera.segment_minutes).filter(Camera.id.in_(camera_ids), Camera.deleted_at.is_(None)).all())
        segment_ids = [i for row in rows.values() if type(i := row.event_metadata.get("ai_recording_id")) is int]
        segments = {s[0] for s in db.query(RecordingSegment.id).filter(RecordingSegment.id.in_(segment_ids),
                    RecordingSegment.deleted_at.is_(None), RecordingSegment.status != "deleted").all()}
        def camera_id_of(row):
            return int(row.target_id) if row and str(row.target_id).isdigit() else None
        pending = []
        for changed, _, file in batch:
            row_id = str(UUID(hex=file.stem))
            row = rows.get(row_id)
            camera_id = camera_id_of(row)
            data = row.event_metadata if row else {}
            captured = data.get("ai_captured_epoch")
            # The usual segment is an hour and some installations use longer
            # segments. Reconcile through its expected close plus grace.
            if (camera_id in living and data.get("ai_recording_id") is None and type(captured) in (int, float)
                    and changed >= now - min(events.RETENTION_DAYS * 86400, max(3600, (living[camera_id] or 5) * 60) + 900)):
                pending.append((camera_id, datetime.fromtimestamp(captured, timezone.utc).replace(tzinfo=None), row_id))
        matches = _link_recordings(db, pending)
        for index, ((changed, _, file), row_id) in enumerate(zip(batch, row_ids), start):
            row = rows.get(row_id)
            if changed >= cutoff and row is None:
                continue  # The writer may not have committed a just-created thumbnail yet.
            data = row.event_metadata if row else {}
            linked = data.get("ai_recording_id")
            camera_id = camera_id_of(row)
            match = matches.get(row_id)
            if (not row or index < excess or camera_id not in living or (linked is not None and linked not in segments)
                    or (match is not None and match[1])):
                file.unlink(missing_ok=True)
                if row:
                    key = "ai_snapshot" if data.get("origin") == ORIGIN else "camera_snapshot"
                    row.event_metadata = {**data, key: False}
            elif match is not None:
                row.event_metadata = {**data, "ai_recording_id": match[0]}
        db.commit()


def _work():
    last_sweep = 0.0
    last_error = -300.0
    while not _stop.is_set():
        try:
            inbox = root() / "inbox"
            if inbox.is_dir():
                paths = []
                with os.scandir(inbox) as entries:
                    for entry in entries:
                        name, _, suffix = entry.name.rpartition(".")
                        if not _HEX.fullmatch(name) or suffix not in {"evt", "part"} or not entry.is_file(follow_symlinks=False):
                            continue
                        try:
                            changed = entry.stat(follow_symlinks=False).st_mtime
                        except FileNotFoundError:
                            continue
                        file = Path(entry.path)
                        if suffix == "evt":
                            paths.append((changed, file))
                        elif changed < time.time() - 300:
                            file.unlink(missing_ok=True)
                for path in _queued_events(paths)[:24]:
                    if _stop.is_set():
                        break
                    with SessionLocal() as db:
                        try:
                            ingest(db, path)
                        except (ValueError, FileNotFoundError):
                            pass
                    path.unlink(missing_ok=True)
            if time.monotonic() - last_sweep >= 300:
                with SessionLocal() as db:
                    _sweep_frames(db)
                last_sweep = time.monotonic()
        except Exception as exc:
            # A failed transaction leaves the inbox item available for retry.
            if time.monotonic() - last_error >= 300:
                last_error = time.monotonic()
                _log.warning("Local AI event intake delayed (%s)", type(exc).__name__)
        _stop.wait(1.5)


def start():
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_work, name="ai-event-history", daemon=True)
        _thread.start()


def stop():
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2)
