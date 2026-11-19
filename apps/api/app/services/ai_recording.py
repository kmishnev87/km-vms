"""Shared recording configuration and signals. Recorder owns all media writes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm.attributes import flag_modified

from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.system_settings import SystemSettings
from app.schemas.ai_recording import DetectionEvidence, RecordingPolicy, RecordingSignal
from app.services.ai_rule_frame import matching_references, read_editor_image, reference_is_fresh, verified_reference
from app.services.audit_log import create_event
from app.services.onvif_events import EVENT_KINDS

POLICY_VERSION = 1
MAX_SIGNAL_KEYS = 64
MAX_STATE_BYTES = 32768
MAX_FRESH_SECONDS = 60
MAX_FUTURE_SECONDS = 10
AI_KINDS = frozenset({"person", "vehicle", "animal", "motion"})
SOURCE_FAULTS = frozenset({"failed", "exhausted", "recovering", "stalled"})


def utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def iso(value):
    return utc(value).replace(tzinfo=timezone.utc).isoformat()


def effective_policy(camera):
    stored = camera.recording_policy
    if stored is None:
        return {"schema_version": POLICY_VERSION, "revision": 0, **RecordingPolicy(
            source="camera" if camera.events_enabled else "none").model_dump(exclude={"ai_rules"})}
    if not isinstance(stored, dict) or stored.get("schema_version") != POLICY_VERSION:
        raise HTTPException(409, detail={"code": "recording_policy_version_unsupported"})
    policy = RecordingPolicy.model_validate({k: v for k, v in stored.items() if k not in {"schema_version", "revision"}})
    result = policy.model_dump()
    if result["ai_rules"] is None:
        result.pop("ai_rules")
    return {"schema_version": POLICY_VERSION, "revision": stored["revision"], **result}


def event_policy_supported(camera):
    policy = camera.recording_policy
    return bool(isinstance(policy, dict) and policy.get("schema_version") == POLICY_VERSION
                and policy.get("mode") == "events" and policy.get("source") == "camera"
                and (policy.get("profile") == "economical"
                     or (policy.get("profile") in ("balanced", "full_quality")
                         and type(policy.get("prebuffer_seconds")) is int and 0 <= policy["prebuffer_seconds"] <= 120))
                and type(policy.get("revision")) is int and policy["revision"] > 0
                and type(policy.get("postrecord_minutes")) is int and 1 <= policy["postrecord_minutes"] <= 5)


def ai_policy_supported(camera):
    policy = camera.recording_policy
    if (not isinstance(policy, dict) or policy.get("schema_version") != POLICY_VERSION or policy.get("source") != "ai"
            or type(policy.get("revision")) is not int or policy["revision"] < 1 or policy.get("rule_ids")):
        return False
    if policy.get("mode") == "continuous" and policy.get("profile") == "none":
        return True
    return (policy.get("mode") == "events" and policy.get("profile") in ("balanced", "full_quality")
            and type(policy.get("prebuffer_seconds")) is int and 0 <= policy["prebuffer_seconds"] <= 120
            and type(policy.get("postrecord_minutes")) is int and 1 <= policy["postrecord_minutes"] <= 5
            and isinstance(policy.get("event_kinds"), list)
            and any(isinstance(kind, str) and kind in AI_KINDS for kind in policy["event_kinds"]))


def link_configured(camera):
    policy = camera.recording_policy
    return isinstance(policy, dict) and policy.get("mode") == "linked"


def linked_policy_supported(camera):
    if not link_configured(camera):
        return False
    policy = camera.recording_policy
    seconds = policy.get("prebuffer_seconds", 0)
    return bool(policy.get("schema_version") == POLICY_VERSION and policy.get("source") == "none"
                and type(policy.get("revision")) is int and policy["revision"] > 0
                and type(policy.get("linked_camera_id")) is int and policy["linked_camera_id"] > 0
                and type(seconds) is int and 0 <= seconds <= 120
                and (policy.get("profile") in ("balanced", "full_quality")
                     or (policy.get("profile") == "economical" and seconds == 0)))


def controlled_recording_configured(camera):
    # An invalid restored link must not be read as an implicit "always" camera.
    return event_policy_supported(camera) or ai_policy_supported(camera) or link_configured(camera)


def reset_recording_cutoff(db, camera, *, keep_channel):
    loaded_policy = camera.recording_policy
    if not isinstance(loaded_policy, dict) or loaded_policy.get("mode") not in ("events", "linked"):
        return
    # Lock and read the actual signal state, not the ORM copy from before
    # validation. A pause followed by a quick resume still invalidates old pulses.
    policy, state = db.query(Camera.recording_policy, Camera.recording_signal_state).filter(
        Camera.id == camera.id).with_for_update().one()
    if not isinstance(policy, dict) or policy.get("mode") not in ("events", "linked"):
        return
    cutoff = {"recording_cutoff_at": iso(datetime.now(timezone.utc))}
    if keep_channel and state and state.get("channel"):
        cutoff["channel"] = state["channel"]
    camera.recording_signal_state = cutoff
    flag_modified(camera, "recording_signal_state")


def camera_rows(db, cameras):
    cameras = list(cameras)
    runtime = {}
    ids = [camera.id for camera in cameras]
    if ids:
        from app.services.recorder_runtime_status import list_camera_recording_states

        runtime = {row["camera_id"]: row for row in list_camera_recording_states(
            db, camera_ids=ids, include_finalized_history=False)}
    active = db.query(SystemSettings.recording_suspended_by_low_disk).order_by(SystemSettings.id).first() if ids else None
    rows = [camera_row(camera, runtime.get(camera.id), storage_suspended=bool(active and active[0]))
            for camera in cameras]
    # In a linked chain the incident and any permitted retry belong to
    # the actual event source, never to the dependent camera's own receiver.
    sources = {camera.id: camera for camera in cameras}
    missing = {row["runtime"].get("link", {}).get("origin_camera_id") for row in rows} - sources.keys() - {None}
    if missing:
        sources.update({camera.id: camera for camera in db.query(Camera).filter(
            Camera.id.in_(missing), Camera.deleted_at.is_(None), Camera.status != "deleting").all()})
    for row in rows:
        view = row["runtime"]
        link = view.get("link") or {}
        if link.get("source_event_status") in SOURCE_FAULTS:
            origin = sources.get(link.get("origin_camera_id"))
            if origin is not None and origin.enabled and event_policy_supported(origin) and origin.events_enabled:
                attach_recovery(view, origin, link["source_event_status"])
            elif origin is not None and origin.enabled and ai_policy_supported(origin):
                source_view = (origin.recording_signal_state or {}).get("recorder") or {}
                proof = runtime.get(row["camera_id"], {})
                if (proof.get("heartbeat_current") and source_view.get("instance") == proof.get("current_recorder_instance_id")
                        and source_view.get("policy_revision") == origin.recording_policy["revision"]):
                    attach_ai(view, origin, source_view.get("ai") or {})
    return rows


def attach_recovery(runtime, camera, phase):
    channel = (camera.recording_signal_state or {}).get("channel") or {}
    if channel.get("policy_revision") != camera.recording_policy.get("revision"):
        return
    runtime["incident"] = channel.get("incident")
    if phase == "exhausted" and channel.get("phase") == "exhausted" and camera.events_enabled:
        runtime["retry_camera_id"] = camera.id


def attach_ai(runtime, camera, ai):
    runtime["ai"] = {key: ai[key] for key in ("phase", "reason", "model", "version", "backend", "device", "runtime", "rules_view") if key in ai}
    runtime["incident"] = ai.get("incident")
    if ai.get("phase") == "exhausted" and ai.get("retry_token"):
        runtime["retry_camera_id"] = camera.id


def actual_status(camera, runtime, evidence, *, storage_suspended):
    if not camera.enabled:
        return "paused"
    if storage_suspended:
        return "storage_suspended"
    managed = runtime["event_recording_available"] or runtime["linked_recording_available"] or runtime["ai_managed"]
    if managed:
        if not runtime["policy_applied"]:
            return "recorder_pending"
        if runtime.get("source_status") == "inactive":
            return "events_disabled"
        if runtime["status"] == "waiting" and runtime.get("source_status") in SOURCE_FAULTS:
            return "source_fault"
        if runtime["ai_managed"] and runtime["status"] == "waiting" and runtime.get("source_status") == "connecting":
            return "analysis_starting"
        if ((evidence.get("event_control") or {}).get("request") and not runtime["confirmed_recording"]
                and (evidence.get("stale_current_segment") or evidence.get("current_failure"))):
            return "error"
        return runtime["status"]
    if not evidence.get("heartbeat_current"):
        return "recorder_pending"
    if evidence.get("confirmed_recording"):
        return "recording"
    if evidence.get("stale_current_segment") or evidence.get("current_failure"):
        return "error"
    state = evidence.get("job_state")
    if state == "stopping":
        return "finalizing"
    if state in ("starting", "restarting") or state == "recording":
        return "restarting" if state == "restarting" else "starting"
    return "idle" if not evidence.get("recording_expected") else "recorder_pending"


def camera_row(camera, evidence=None, *, storage_suspended=False):
    evidence = evidence or {}
    state = camera.recording_signal_state or {}
    linked = linked_policy_supported(camera)
    ai = ai_policy_supported(camera)
    runtime = {"status": "prepared_not_active", "policy_applied": False,
               "legacy_recording_mode": camera.recording_mode, "ai_available": True, "ai_managed": ai,
               "event_recording_available": event_policy_supported(camera) or (ai and camera.recording_policy["mode"] == "events"),
               "linked_recording_available": linked}
    if runtime["event_recording_available"] or linked or ai:
        control = evidence.get("event_control") or {}
        confirmed = bool(evidence.get("confirmed_recording") and control.get("job_id") == evidence.get("job_id"))
        status = control.get("status") or "recorder_pending"
        if evidence.get("job_state") == "stopping" and evidence.get("heartbeat_current"):
            status = "finalizing"
        elif status == "recording":
            status = ("extended" if control.get("extended") else "recording") if confirmed else "starting"
        runtime.update(
            status=status, policy_applied=bool(control), source_status=control.get("source_status"),
            episode_until=control.get("episode_until"), confirmed_recording=confirmed,
            emergency_requested=control.get("request") == "emergency",
            emergency_recording=confirmed and camera.enabled and not storage_suspended and control.get("request") == "emergency",
            incident=None,
            prebuffer_status=control.get("prebuffer_status"),
            prebuffer_seconds_available=control.get("prebuffer_seconds_available", 0),
            prebuffer_capacity_limited=bool(control.get("prebuffer_capacity_limited")),
            main_stream_status=control.get("main_stream_status"),
            active_stream=control.get("active_stream"),
        )
        if ai:
            attach_ai(runtime, camera, control.get("ai") or {"phase": "pending"})
        elif not linked and control and camera.enabled and control.get("source_status") in SOURCE_FAULTS:
            attach_recovery(runtime, camera, control["source_status"])
        if linked:
            link = control.get("link") or {}
            runtime["link"] = {key: link.get(key) for key in (
                "origin_camera_id", "source_mode", "source_intent", "source_event_status",
                "source_writer_state", "source_recording_confirmed")}
            runtime["link"]["source_camera_id"] = camera.recording_policy["linked_camera_id"]
            runtime["link"]["source_recording_confirmed"] = bool(link.get("source_recording_confirmed"))
            runtime["link"]["postrecord_inherited"] = True
    runtime["actual_status"] = actual_status(camera, runtime, evidence, storage_suspended=storage_suspended)
    return {"camera_id": camera.id, "name": camera.name, "recording_enabled": bool(camera.enabled),
            "record_stream": camera.default_record_stream, "live_stream": camera.default_live_stream,
            "protocol": camera.protocol, "events_enabled": bool(camera.events_enabled), "policy": effective_policy(camera),
            "signal": {key: state.get(key) for key in ("last_activity_at", "last_observed_at", "capacity_limited")},
            "runtime": runtime}


def spatial_geometry(rules):
    if not rules:
        return ()
    points = tuple(sorted((item["id"], item["kind"], tuple(item["points"]))
                          for item in rules["rules"] if item["points"]))
    exclusions = tuple(tuple(polygon) for polygon in rules["exclusions"])
    return points, exclusions


def save_policy(db, camera_id, payload, actor):
    # Serialize graph changes in stable camera order, including previously absent
    # policy rows. Concurrent A->B / B->A updates cannot both pass validation.
    cameras = db.query(Camera).filter(Camera.deleted_at.is_(None)).order_by(Camera.id).with_for_update().all()
    by_id = {camera.id: camera for camera in cameras if camera.status != "deleting"}
    camera = by_id.get(camera_id)
    if camera is None:
        raise HTTPException(404, detail={"code": "camera_not_found"})
    current = effective_policy(camera)
    if payload.expected_revision != current["revision"]:
        raise HTTPException(409, detail={"code": "recording_policy_changed"})
    proposed = payload.policy.model_dump()
    previous_rules = current.get("ai_rules")
    if "ai_rules" not in payload.policy.model_fields_set:
        # Older clients must not clear the configuration on unrelated edits.
        proposed["ai_rules"] = previous_rules
    rules = proposed.get("ai_rules")
    if rules is None:
        proposed.pop("ai_rules", None)
    else:
        if proposed["source"] == "ai" and proposed["mode"] == "events" and not any(item["record"] for item in rules["rules"]):
            raise HTTPException(422, detail={"code": "ai_record_rule_required"})
        shape, old_shape = spatial_geometry(rules), spatial_geometry(previous_rules)
        anchor, old_anchor = rules["reference"], (previous_rules or {}).get("reference")
        same_anchor = old_anchor == anchor
        if not same_anchor and anchor:
            claims = verified_reference(camera, anchor)
            if not claims or not reference_is_fresh(camera, anchor):
                raise HTTPException(409, detail={"code": "ai_frame_reference_required"})
            if claims.get("editor") and read_editor_image(camera, anchor) is None:
                raise HTTPException(409, detail={"code": "ai_frame_reference_required"})
        if any(shape) and (shape != old_shape or not same_anchor):
            # Re-edit the exact committed Web image even after the capture window closes.
            # A legacy token without an image still needs a new frame after expiry.
            if same_anchor and not reference_is_fresh(camera, anchor) and read_editor_image(camera, anchor) is None:
                raise HTTPException(409, detail={"code": "ai_frame_reference_required"})
            if old_anchor and not same_anchor and not matching_references(old_anchor, anchor):
                if any(set(a) & set(b) for a, b in zip(old_shape, shape)):
                    raise HTTPException(409, detail={"code": "ai_frame_reference_changed"})
    if proposed["source"] == "ai" and "event_kinds" not in payload.policy.model_fields_set:
        proposed["event_kinds"] = ["person", "vehicle", "animal"]
    if proposed["source"] == "camera" and (camera.protocol != "onvif" or not camera.events_enabled):
        raise HTTPException(409, detail={"code": "camera_event_reception_required"})
    next_id = proposed["linked_camera_id"]
    seen = {camera_id}
    while next_id is not None:
        if next_id in seen:
            raise HTTPException(422, detail={"code": "recording_link_cycle"})
        seen.add(next_id)
        target = by_id.get(next_id)
        if target is None:
            raise HTTPException(422, detail={"code": "recording_link_source_unavailable"})
        next_id = effective_policy(target)["linked_camera_id"]
    previous = {k: v for k, v in current.items() if k not in {"schema_version", "revision"}}
    activate_continuous = proposed["mode"] == "continuous" and camera.recording_mode != "always"
    if previous == proposed and not activate_continuous:
        return camera_rows(db, [camera])[0]
    camera.recording_policy = {"schema_version": POLICY_VERSION, "revision": current["revision"] + 1, **proposed}
    if activate_continuous:
        # Apply an explicit editor choice, not an install-time migration.
        # The independent camera pause (enabled) is deliberately unchanged.
        camera.recording_mode = "always"
    # A new rule/source generation cannot reuse stale activity from its predecessor.
    camera.recording_signal_state = None
    audit = create_event(db=db, actor=actor, category="cameras", event_type="cameras.recording_policy_saved",
        message_ru="Сохранены настройки AI и записи камеры", message_en="Camera AI and recording policy saved",
        target_type="camera", target_id=camera.id, target_name=camera.name,
        metadata={"revision": camera.recording_policy["revision"], "policy_applied": False,
                  "changed_fields": sorted(key for key in proposed.keys() | previous.keys() if proposed.get(key) != previous.get(key))
                                    + (["recording_mode"] if activate_continuous else [])}, commit=False)
    if audit is None:
        raise RuntimeError("recording_policy_audit_failed")
    db.commit()
    return camera_rows(db, [camera])[0]


def read_emergency(db, *, lock=False):
    query = db.query(SystemSettings).order_by(SystemSettings.id)
    row = (query.with_for_update() if lock else query).first()
    return row, {"enabled": bool(row and row.event_recording_emergency_enabled),
                 "status": "available",
                 "eligible_modes": ["events"],
                 "available_sources": ["camera", "ai"],
                 "fault_reasons": ["camera_event_channel_failed", "ai_analysis_failed"],
                 "overrides_manual_pause": False, "overrides_storage_gates": False}


def emergency_response(db):
    _, settings = read_emergency(db)
    cameras = db.query(Camera).filter(Camera.recording_policy.is_not(None), Camera.deleted_at.is_(None)).all()
    runtime = [row["camera_id"] for row in camera_rows(db, cameras) if row["runtime"].get("emergency_recording")]
    return {**settings, "runtime_active": bool(runtime), "recording_camera_ids": runtime}


def ai_retry_candidate(camera, control, *, now=None):
    ai = control.get("ai") or {}
    if ai.get("phase") != "exhausted" or not ai.get("retry_token") or not control.get("instance"):
        return "ai_not_exhausted", None
    incident = ai.get("incident") or {}
    now = now or datetime.now(timezone.utc)
    try:
        cooldown = now - datetime.fromisoformat(incident["last_failure_at"]) < timedelta(seconds=30)
    except (KeyError, TypeError, ValueError):
        cooldown = True
    if cooldown:
        return "ai_retry_cooldown", None
    ticket = {"instance": control["instance"], "revision": camera.recording_policy["revision"], "token": ai["retry_token"]}
    state = camera.recording_signal_state or {}
    if state.get("ai_retry") == ticket:
        return "ai_retry_pending", None
    return "queued", ticket


def queue_ai_retry(db, camera_id):
    from app.services.recorder_runtime_status import list_camera_recording_states

    camera = db.query(Camera).filter(Camera.id == camera_id).populate_existing().with_for_update().first()
    if camera is None or camera.deleted_at is not None or camera.status == "deleting" or not ai_policy_supported(camera):
        return "ai_not_configured"
    proof, = list_camera_recording_states(db, camera_ids=[camera_id], include_finalized_history=False)
    status, ticket = ai_retry_candidate(camera, proof.get("event_control") or {})
    if ticket is None:
        return status
    state = camera.recording_signal_state or {}
    db.execute(update(Camera).where(Camera.id == camera_id).values(
        recording_signal_state={**state, "ai_retry": ticket}, updated_at=Camera.updated_at))
    db.commit()
    return "queued"


def save_emergency(db, payload, actor):
    row, current = read_emergency(db, lock=True)
    if row is None:
        raise HTTPException(409, detail={"code": "system_not_initialized"})
    if current["enabled"] != payload.expected_enabled:
        raise HTTPException(409, detail={"code": "emergency_setting_changed"})
    if payload.enabled != current["enabled"]:
        row.event_recording_emergency_enabled = payload.enabled
        audit = create_event(db=db, actor=actor, category="settings", event_type="settings.event_recording_emergency_updated",
            message_ru="Изменено разрешение аварийной записи", message_en="Emergency recording permission changed",
            metadata={"enabled": payload.enabled}, commit=False)
        if audit is None:
            raise RuntimeError("emergency_setting_audit_failed")
        db.commit()
    return emergency_response(db)


def observe_camera_signal(db, group, resolved_camera, signal, *, received):
    """Record bounded last activity BEFORE history suppression/caps/personal prefs.

    Only an already resolved opted-in lens qualifies. Initialized snapshots do
    not start/extend recordings. A state change is an observation, never proof
    that every object is inactive. Unknown clock data is explicitly flagged.
    """
    if resolved_camera is None or signal.initialized or getattr(signal, "operation", None) == "Initialized":
        return False
    # Legacy/unconfigured cameras do not enter the prepared controller at all.
    # A foreign/legacy event adapter must not break the existing history path.
    camera = db.get(Camera, resolved_camera["id"])
    if camera is None or camera.recording_policy is None:
        return False
    if not isinstance(signal.source, dict) or not isinstance(signal.key, dict):
        return False
    identity = hashlib.sha256(json.dumps([group["device_id"], signal.topic, signal.source, signal.key],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    generation = str(group.get("recording_generation") or group.get("fingerprint") or group["device_id"])[:64]
    kind = signal.kind
    if kind not in EVENT_KINDS:
        # Some cameras omit object-class flags on the end notification. Resolve
        # ONLY the same property's prior known class in this policy/subscription
        # generation, never a guessed class or another object's active state.
        state = camera.recording_signal_state or {}
        previous = (state.get("observations") or {}).get(identity, {})
        if (signal.active is not False or state.get("source_generation") != generation
                or state.get("policy_revision") != effective_policy(camera)["revision"]
                or previous.get("kind") not in EVENT_KINDS):
            return False
        kind = previous["kind"]
    # Stable opaque rule identity without device-provided display text in API.
    rule_id = hashlib.sha256(json.dumps([signal.topic, signal.source.get("Rule")],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    kinds = {kind, signal.rule_kind, signal.object_kind, *signal.object_kinds} & set(EVENT_KINDS)
    normalized = RecordingSignal(source="camera", source_generation=generation,
        identity=identity, kind=kind, matched_kinds=sorted(kinds), rule_id=rule_id,
        active=signal.active, initialized=signal.initialized, occurred_at=signal.occurred_at,
        source_at=signal.camera_at, clock_uncertain=signal.clock_uncertain)
    return observe_recording_signal(db, resolved_camera["id"], normalized, received=received)


def observe_recording_signal(db, camera_id, signal: RecordingSignal, *, received):
    """Common internal entry for camera/AI adapters; never a public ingestion API."""
    if signal.initialized:
        return False
    camera = db.query(Camera).filter(Camera.id == camera_id).populate_existing().with_for_update().first()
    if camera is None or camera.deleted_at is not None or camera.status == "deleting":
        return False
    if signal.source == "camera" and not camera.events_enabled:
        return False
    policy = effective_policy(camera)
    if policy["source"] != signal.source or policy["mode"] != "events":
        return False
    if signal.active is not False and not {signal.kind, *signal.matched_kinds}.intersection(policy["event_kinds"]):
        return False
    if policy["rule_ids"] and signal.rule_id not in policy["rule_ids"]:
        return False
    occurred, received = utc(signal.occurred_at), utc(received)
    if signal.active is not False and not signal.clock_uncertain and not -MAX_FUTURE_SECONDS <= (received - occurred).total_seconds() <= MAX_FRESH_SECONDS:
        return False
    identity, generation = signal.identity, signal.source_generation
    state = camera.recording_signal_state or {}
    cutoff = state.get("recording_cutoff_at")
    if cutoff and signal.active is not False and iso(received if signal.clock_uncertain else occurred) <= cutoff:
        return False
    if state.get("policy_revision") != policy["revision"] or state.get("source_generation") != generation:
        state = {key: state[key] for key in ("channel", "recorder", "recording_cutoff_at") if key in state}
        state.update(schema_version=1, policy_revision=policy["revision"], source=signal.source,
                     source_generation=generation, observations={})
    observations = dict(state.get("observations") or {})
    previous = observations.get(identity)
    stamp = iso(signal.source_at or occurred)
    if previous and previous.get("camera_at") == stamp and previous.get("active") == signal.active and previous.get("kind") == signal.kind:
        return False  # Exact replay is not new activity, even with a bad clock.
    if previous and not signal.clock_uncertain and previous["occurred_at"] > iso(occurred):
        return False
    if identity not in observations and len(observations) >= MAX_SIGNAL_KEYS:
        # Explicit degraded state instead of silently pretending complete coverage.
        observations.pop(min(observations, key=lambda key: observations[key]["received_at"]))
        state = {**state, "capacity_limited": True}
    observations[identity] = {"kind": signal.kind, "rule_id": signal.rule_id, "active": signal.active, "camera_at": stamp,
        "occurred_at": iso(occurred), "received_at": iso(received), "clock_uncertain": bool(signal.clock_uncertain)}
    state = {**state, "observations": observations, "last_observed_at": iso(received), "source_confirmed": True}
    if signal.active is not False:
        state["last_activity_at"] = iso(received)
    # An end never shortens the last activity deadline or ends another object.
    if len(json.dumps(state, separators=(",", ":")).encode()) > MAX_STATE_BYTES:
        raise RuntimeError("recording_signal_state_bound_exceeded")
    db.execute(update(Camera).where(Camera.id == camera.id).values(
        recording_signal_state=state, updated_at=Camera.updated_at))
    return True


def invalidate_camera_signals(db, group):
    """A disconnected subscription cannot leave a confirmed ongoing observation.

    The timestamp stays available for diagnostics. A disconnected observation
    cannot open a new episode; Recorder owns the deadline of an episode in progress.
    """
    ids = [camera["id"] for camera in group["cameras"]]
    rows = db.query(Camera).filter(Camera.id.in_(ids)).order_by(Camera.id).populate_existing().with_for_update().all()
    for camera in rows:
        state = camera.recording_signal_state
        if state and state.get("source") == "camera" and state.get("source_confirmed"):
            db.execute(update(Camera).where(Camera.id == camera.id).values(
                recording_signal_state={**state, "source_confirmed": False}, updated_at=Camera.updated_at))


def detection_metadata(db, camera_id, evidence: DetectionEvidence):
    """One event's metadata, not a per-frame store or a second history.

    Snapshot persistence/ownership is not implemented in step 2. Reject a supplied
    reference rather than accept a path or claim that an unverified image exists.
    Future snapshots must be owned by the same event and its retention/deletion.
    """
    camera = db.get(Camera, camera_id)
    if camera is None or camera.deleted_at is not None:
        raise ValueError("detection_camera_unavailable")
    if evidence.snapshot_id is not None:
        raise ValueError("detection_snapshot_storage_not_available")
    if evidence.recording_id is not None:
        segment = db.get(RecordingSegment, evidence.recording_id)
        occurred = utc(evidence.occurred_at)
        if (segment is None or segment.camera_id != camera_id or segment.deleted_at is not None
                or segment.finalized_at is None or segment.ended_at is None
                or segment.status != "finalized" or segment.ownership != "KM VMS" or segment.source != "recorder"
                or not segment.started_at <= occurred <= segment.ended_at):
            raise ValueError("detection_recording_reference_invalid")
    return evidence.model_dump(mode="json", exclude_none=True)
