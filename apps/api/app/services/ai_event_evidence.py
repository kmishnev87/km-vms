"""Resolve a confirmed AI event against the existing archive, without assuming a recording exists."""

from datetime import datetime, timedelta, timezone

from app.core.permissions import user_has_permission
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.services.camera_events import now_utc
from app.services.timezone_contract import format_system_iso, timezone_context


def archive_evidence(db, user, event):
    if not user_has_permission(user.role, "view_timeline"):
        return {"state": "forbidden"}
    try:
        occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        if occurred.tzinfo is None:
            return {"state": "unavailable"}
        instant = occurred.astimezone(timezone.utc).replace(tzinfo=None)
    except (AttributeError, TypeError, ValueError):
        return {"state": "unavailable"}
    camera = db.get(Camera, event["camera_id"])
    if camera is None:
        return {"state": "unavailable"}
    from app.routers.chronology import chronology_playback

    # A segment covering only the five-second pre-roll is not proof of the event.
    playback = chronology_playback(camera_id=camera.id, ts=occurred.isoformat(), db=db, current_user=user)
    if playback.get("has_video"):
        segment = db.get(RecordingSegment, playback["segment_id"])
        if (segment and segment.deleted_at is None and segment.source == "recorder" and segment.ownership == "KM VMS"
                and segment.started_at and segment.ended_at and segment.started_at <= instant < segment.ended_at):
            start = max(segment.started_at, instant - timedelta(seconds=5))
            end = min(segment.ended_at, instant + timedelta(seconds=20))
            second = start.replace(microsecond=0)
            # Web Timeline uses a whole-second calendar cursor. Don't round
            # backwards out of the segment that was proved to cover the event.
            if second < segment.started_at and second + timedelta(seconds=1) < segment.ended_at:
                second += timedelta(seconds=1)
            ctx = timezone_context(db)
            result = {"state": "ready", "camera_id": camera.id, "camera_name": event["camera_name"],
                      "timestamp_system": format_system_iso(second, ctx)[:19],
                      "timestamp_utc": start.replace(tzinfo=timezone.utc).isoformat(),
                      "url": "/chronology?camera_event=" + event["id"]}
            if end > start and user_has_permission(user.role, "export_recordings"):
                result["clip"] = {"camera_id": camera.id,
                                  "start_ts": start.replace(tzinfo=timezone.utc).isoformat(),
                                  "end_ts": end.replace(tzinfo=timezone.utc).isoformat()}
            return result
    if camera.enabled and camera.deleted_at is None and now_utc() - instant < timedelta(minutes=max(1, camera.segment_minutes or 5) + 2):
        from app.services.recorder_runtime_status import list_camera_recording_states

        states = list_camera_recording_states(db, camera_ids=[camera.id], include_finalized_history=False)
        proof = states[0] if states else {}
        current = db.get(RecordingSegment, proof["current_segment_id"]) if proof.get("current_segment_id") else None
        if (proof.get("heartbeat_current") and current and current.deleted_at is None and current.status == "writing"
                and current.source == "recorder" and current.ownership == "KM VMS" and current.started_at
                and current.started_at <= instant and not proof.get("stale_current_segment")):
            return {"state": "pending"}
    return {"state": "unavailable"}
