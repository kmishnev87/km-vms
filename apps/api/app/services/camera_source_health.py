"""Operator incidents projected from the existing receiver and Recorder evidence."""

from datetime import datetime, timezone

from app.services.ai_recording import SOURCE_FAULTS, ai_policy_supported, ai_retry_candidate, event_policy_supported
from app.services.camera_archive_placement import ARCHIVE_TARGET_FAILURES, storage_route_evidence
from app.services.camera_event_settings import camera_event_reception_selected
from app.services.camera_events_worker import receiver_fault_snapshots


_NETWORK_FAILURES = ("connection refused", "connection timed out", "connection reset", "no route to host",
                     "network is unreachable", "host is unreachable", "could not resolve", "name or service not known",
                     "temporary failure in name resolution")


def fresh(at, now, limit=120):
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if isinstance(at, datetime):
        at = at.replace(tzinfo=timezone.utc).timestamp() if at.tzinfo is None else at.timestamp()
    instant = now.replace(tzinfo=timezone.utc).timestamp() if now.tzinfo is None else now.timestamp()
    return type(at) in (float, int) and 0 <= instant - at <= limit


def is_video_network_failure(*errors):
    return any(value == "camera_unreachable" or any(token in value for token in _NETWORK_FAILURES)
               for error in errors if isinstance(error, str) for value in (error.lower(),))


def _camera_source_cause(subscription, proof, live, now):
    event_evidence = subscription.get("reachability") if fresh(subscription.get("failed_at"), now, 300) else None
    if event_evidence == "reachable" or proof.get("confirmed_recording") or "reachable" in live:
        return "subscription"
    current_job = (proof.get("heartbeat_current") and proof.get("recorder_instance_id")
                   and proof["recorder_instance_id"] == proof.get("current_recorder_instance_id"))
    failed_recording = (current_job and proof.get("recording_health") in {"retrying", "failed"}
                        and fresh(proof.get("job_updated_at"), now)
                        and is_video_network_failure(proof.get("last_error")))
    if event_evidence == "network_failure" and (failed_recording or "network_failure" in live):
        return "unreachable"
    return "no_response"


def _recording_state(camera, proof, storage_suspended):
    if not camera.enabled:
        return "paused"
    route = storage_route_evidence(camera, proof)
    if (storage_suspended and route is None) or (route and route["status"] == "blocked") or proof.get("camera_last_error") in ARCHIVE_TARGET_FAILURES:
        return "storage_suspended"
    if proof.get("confirmed_recording"):
        control = proof.get("event_control") or {}
        if proof.get("job_id") and control.get("job_id") == proof["job_id"] and control.get("request") == "emergency":
            return "emergency"
        return "recording"
    if not proof.get("heartbeat_current"):
        return "unconfirmed"
    if proof.get("job_state") in {"starting", "restarting", "recording", "stopping"}:
        return "unconfirmed"
    return "stopped"


def build_source_issues(cameras, recorder_states, now, *, live_evidence=None, storage_suspended=False, default_archive_root_id=None):
    cameras = {camera.id: camera for camera in cameras if camera.deleted_at is None and camera.status != "deleting"}
    receiver = receiver_fault_snapshots(cameras.values())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    groups, sources = {}, {}
    live_evidence = live_evidence or {}
    reachable_groups = {
        subscription["group"] for camera_id, subscription in receiver.items()
        if subscription.get("group") and _camera_source_cause(
            subscription, recorder_states.get(camera_id) or {}, live_evidence.get(camera_id, ()), now) == "subscription"
    }

    def affected(issue, camera, *, dependent=False):
        if any(item["camera_id"] == camera.id for item in issue["cameras"]):
            return
        issue["cameras"].append({
            "camera_id": camera.id, "name": camera.name,
            **({"dependent": True} if dependent else {}),
            "recording_state": _recording_state(camera, recorder_states.get(camera.id, {}), storage_suspended and
                                                getattr(camera, "recording_archive_root_id", None) in (None, default_archive_root_id)),
        })

    def register(camera, kind, *, phase, group=None, next_attempt_at=None, retry_available=False,
                 reachability=None, failed_at=None):
        cause = None
        if kind == "camera":
            cause = ("subscription" if group in reachable_groups else
                     _camera_source_cause({"reachability": reachability, "failed_at": failed_at},
                         recorder_states.get(camera.id) or {}, live_evidence.get(camera.id, ()), now))
            if cause == "unreachable":
                group, next_attempt_at, retry_available = "offline-cameras", None, False
        issue = groups.setdefault((kind, cause, group or camera.id), {
            "kind": kind, "camera_id": camera.id, "phase": phase, "cameras": [],
            **({"cause": cause} if cause else {}),
            "retry_available": bool(retry_available), "next_attempt_at": next_attempt_at,
        })
        if retry_available and not issue["retry_available"]:
            issue.update(camera_id=camera.id, phase=phase, retry_available=True, next_attempt_at=next_attempt_at)
        sources[kind, camera.id] = issue
        affected(issue, camera)

    for camera in cameras.values():
        proof = recorder_states.get(camera.id) or {}
        # The shared proof has already rejected an old Recorder instance or policy revision.
        control = proof.get("event_control") or {}
        subscription = receiver.get(camera.id)
        if subscription:
            register(camera, "camera", **subscription)
        elif (camera.enabled and camera_event_reception_selected(camera) and event_policy_supported(camera)
              and control.get("source_status") in SOURCE_FAULTS):
            channel = (camera.recording_signal_state or {}).get("channel") or {}
            incident = (channel.get("incident") or {}) if channel.get("policy_revision") == camera.recording_policy["revision"] else {}
            register(camera, "camera", phase=control["source_status"], group=incident.get("id"),
                     next_attempt_at=incident.get("next_attempt_at"))
        if camera.enabled and ai_policy_supported(camera):
            ai = control.get("ai") or {}
            if ai.get("phase") in SOURCE_FAULTS:
                _status, ticket = ai_retry_candidate(camera, control, now=now)
                incident = ai.get("incident") or {}
                # One inference worker serves the NAS. Only no-frame/capacity
                # stalls are per camera; a failed worker is one incident with
                # several affected writers, not several independent faults.
                instance = control.get("instance")
                common = ("worker", instance) if instance and incident and ai["phase"] != "stalled" else None
                register(camera, "ai", phase=ai["phase"], group=common, next_attempt_at=incident.get("next_attempt_at"),
                         retry_available=ticket is not None)

    for camera in cameras.values():
        proof = recorder_states.get(camera.id) or {}
        link = (proof.get("event_control") or {}).get("link") or {}
        if not camera.enabled or not proof.get("linked_policy_configured") or link.get("source_event_status") not in SOURCE_FAULTS:
            continue
        origin = cameras.get(link.get("origin_camera_id"))
        if origin is not None:
            issue = sources.get(("ai" if ai_policy_supported(origin) else "camera", origin.id))
            if issue:
                affected(issue, camera, dependent=True)
    return list(groups.values())
