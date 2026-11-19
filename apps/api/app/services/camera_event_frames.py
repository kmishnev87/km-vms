"""Best-effort full-frame evidence, never a prerequisite for delivery or recording."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import subprocess
import threading
from urllib.parse import urlsplit

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.camera import Camera
from app.services.ai_rule_frame import jpeg_size, run_ffmpeg
from app.services.camera_event_settings import camera_event_reception_selected

WINDOW_SECONDS = 6
MAX_JPEG = 128 * 1024
_active: set[int] = set()
_lock = threading.Lock()
_segment_name = re.compile(r"[A-Za-z0-9_-]+\.ts\Z")


def utc_now():
    return datetime.now(timezone.utc)


def forward_segment(playlist: Path, occurred: datetime) -> Path | None:
    """Use only an existing segment whose program date starts at/after the event."""
    try:
        with playlist.open(encoding="utf-8") as handle:
            contents = handle.read(16385)
    except (OSError, ValueError):
        return None
    if len(contents) > 16384:
        return None
    stamped = None
    possible = []
    for line in contents.splitlines():
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            try:
                stamped = datetime.fromisoformat(line.split(":", 1)[1])
            except ValueError:
                stamped = None
        elif line and not line.startswith("#"):
            if (stamped is not None and stamped.tzinfo is not None and _segment_name.fullmatch(line)
                    and timedelta() <= stamped - occurred <= timedelta(seconds=WINDOW_SECONDS)):
                name = playlist.parent / line
                if name.is_file() and not name.is_symlink():
                    possible.append((stamped, name))
            stamped = None
    return min(possible)[1] if possible else None


def capture(camera, occurred):
    from app.core.config import settings

    track = "sub" if camera.default_live_stream == "sub" and camera.rtsp_sub_url else "main"
    deadline = occurred + timedelta(seconds=WINDOW_SECONDS)
    now = utc_now()
    if not occurred <= now < deadline:
        return b""
    # Reuse existing HLS if a sufficiently recent part already exists. Do not
    # open a viewer or read an arbitrary file named by a playlist.
    playlist = Path(settings.storage_previews) / "live" / str(camera.id) / track / "index.m3u8"
    part = forward_segment(playlist, occurred)
    if part:
        args = ["-threads", "1", "-noautorotate", "-i", str(part)]
        frame_filter = ""
    else:
        policy = camera.recording_policy or {}
        if policy.get("mode") == "events" and policy.get("profile") == "economical":
            return b""  # Never pull a stream solely for a preview in this mode.
        url = camera.rtsp_sub_url if track == "sub" else camera.rtsp_main_url
        if not url or urlsplit(url).scheme not in {"rtsp", "rtsps"}:
            return b""
        transport = "udp" if camera.rtsp_transport == "udp" else "tcp"
        # A new, short live read starts only after occurred. Never reuse an
        # onboarding JPEG or an earlier cached keyframe.
        args = ["-rtsp_transport", transport, "-analyzeduration", "640000", "-probesize", "262144",
                "-threads", "1", "-noautorotate", "-i", url]
        frame_filter = "select='gte(t,0.4)',"
    limit = (deadline - utc_now()).total_seconds()
    if limit < 0.25:
        return b""
    image = run_ffmpeg([*args, "-map", "0:v:0", "-an", "-sn", "-vf",
        frame_filter + "scale=w='min(640,iw)':h='min(480,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
        "-frames:v", "1", "-threads", "1", "-q:v", "5", "-f", "mjpeg", "pipe:1"], timeout=limit)
    return image if utc_now() <= deadline and len(image) <= MAX_JPEG and jpeg_size(image, 640, 480) else b""


def _work(camera_id, event_id, revision, occurred):
    from app.services import camera_events as events
    from app.services.ai_event_history import frame_path
    from app.services.camera_events_worker import camera_receiving_revision

    def eligible(camera):
        return (camera is not None and camera.deleted_at is None and camera.status != "deleting"
                and camera_event_reception_selected(camera)
                and (revision is None or revision == camera_receiving_revision(camera)))

    try:
        with SessionLocal() as db:
            camera = db.get(Camera, camera_id)
            if not eligible(camera):
                return
            db.expunge(camera)  # No DB transaction held during network I/O.
        image = capture(camera, occurred)
        if not image:
            return
        with SessionLocal() as db:
            current = db.execute(select(Camera).where(Camera.id == camera_id).with_for_update()).scalar_one_or_none()
            if not eligible(current) or camera_receiving_revision(camera) != camera_receiving_revision(current):
                return
            event = events.event_query(db).filter_by(id=event_id, target_type="camera", target_id=str(camera_id)).with_for_update().first()
            if event is None or (event.event_metadata or {}).get("origin") == events.AI_ORIGIN:
                return
            target = frame_path(event_id)
            part = target.with_suffix(".part")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with part.open("xb") as output:
                    output.write(image)
                os.replace(part, target)
                event.event_metadata = {**(event.event_metadata or {}), "camera_snapshot": True}
                db.commit()
            finally:
                part.unlink(missing_ok=True)
    except Exception:
        # Evidence is optional; no credential, URL or camera error enters logs
        # or the notification payload from this path.
        pass
    finally:
        with _lock:
            _active.discard(camera_id)


def queue_frame(camera_id, event_id, revision, occurred):
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    if not timedelta() <= utc_now() - occurred < timedelta(seconds=WINDOW_SECONDS):
        return
    with _lock:
        if camera_id in _active or len(_active) >= 2:
            return  # No unbounded queue and never parallel capture of one lens.
        _active.add(camera_id)
    try:
        threading.Thread(target=_work, args=(camera_id, event_id, revision, occurred),
                         name="camera-event-frame", daemon=True).start()
    except RuntimeError:
        with _lock:
            _active.discard(camera_id)
