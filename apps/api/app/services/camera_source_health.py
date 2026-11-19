"""Operator incidents projected from the existing receiver and Recorder evidence."""

from datetime import timezone

from app.services.ai_recording import SOURCE_FAULTS, ai_policy_supported, ai_retry_candidate, event_policy_supported
from app.services.camera_event_settings import camera_event_reception_selected
from app.services.camera_events_worker import receiver_fault_snapshots


def _recording_state(camera, proof, storage_suspended):
    if not camera.enabled:
        return "paused"
    if storage_suspended:
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


def build_source_issues(cameras, recorder_states, now, *, storage_suspended=False):
    cameras = {camera.id: camera for camera in cameras if camera.deleted_at is None and camera.status != "deleting"}
    receiver = receiver_fault_snapshots(cameras.values())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    groups, sources = {}, {}

    def affected(issue, camera):
        if any(item["camera_id"] == camera.id for item in issue["cameras"]):
            return
        issue["cameras"].append({
            "camera_id": camera.id, "name": camera.name,
            "recording_state": _recording_state(camera, recorder_states.get(camera.id, {}), storage_suspended),
        })

    def register(camera, kind, *, phase, group=None, next_attempt_at=None, retry_available=False):
        issue = groups.setdefault((kind, group or camera.id), {
            "kind": kind, "camera_id": camera.id, "phase": phase, "cameras": [],
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
                affected(issue, camera)
    return list(groups.values())
