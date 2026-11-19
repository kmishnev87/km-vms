"""Several recording modes on one real Recorder/FFmpeg/PostgreSQL fixture."""
import json
import os
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import AuditEvent, RecordingSegment
from recorder_core.ai_supervisor import AiSupervisor
from recorder_core.event_buffer import BufferedCamera, capture_command
from test_event_recording_profiles_postgres import ffmpeg, video_span
from test_linked_recording_postgres import event_source, stage5  # noqa: F401
from test_self_recovery_stage1374 import worker  # noqa: F401
from test_stage14_history_large_totals_postgres import history_database  # noqa: F401


def test_mixed_recording_profiles_are_isolated_under_input_loss_and_disk_hold(stage5, monkeypatch, tmp_path):
    s = stage5
    flags = {"person": False, "failure": False}
    attempts = []

    class Inference:
        def __init__(self, gpu):
            assert not gpu
            attempts.append(time.monotonic())

        def ready(self):
            return {"phase": "ready", "backend": "cpu", "version": "fixture",
                    "model": "fixture", "device": "CPU", "runtime": "injectable-fixture"}

        def analyze(self, frame):
            if flags["failure"]:
                raise TimeoutError
            return {"phase": "sample", "scene_change": False, "objects": [{"kind": "person"}] if flags["person"] else []}

        def close(self):
            pass

    streams = {url: tmp_path / f"{role}.mp4" for role, url in s.streams.items()}

    def capture(url, transport):
        args = capture_command(url, transport)
        s.connected.append(url)
        return args[:args.index("-rtsp_transport")] + ["-re", "-stream_loop", "-1", "-i", str(streams[url])] + args[args.index("-i") + 2:]

    ai = AiSupervisor(Inference)
    monkeypatch.setattr(s.worker, "local_ai", ai)
    monkeypatch.setattr(s.worker, "AI_GPU_ALLOWED", False)
    monkeypatch.setattr(s.worker, "BufferedCamera", lambda *a, **k: BufferedCamera(*a, command=capture, **k))
    s.camera(1)
    phase, pulse = event_source(s, 2, unavailable=False)
    s.camera(3, source=False)
    s.policy(3, mode="events", source="ai", profile="balanced", prebuffer_seconds=5, postrecord_minutes=1, event_kinds=["person"])
    s.camera(4, source=False)
    s.follow(4, 3, "full_quality")
    opened = time.monotonic()
    try:
        phase("healthy")
        s.until(lambda: s.worker.jobs[1].process is not None and s.status(3)["runtime"]["ai"]["phase"] == "healthy")
        jobs = s.worker.jobs
        assert all(jobs[i].process is None for i in (2, 3, 4))
        assert jobs[2].event_buffer.primary.proc is None
        s.until(lambda: jobs[3].event_buffer.status()["prebuffer_seconds_available"] >= 2)
        assert pulse()
        flags["person"] = True
        s.until(lambda: all(jobs[i].state == "recording" for i in (1, 2, 3, 4)), timeout=22)
        s.until(lambda: jobs[3].process.stream_role == "sub" and jobs[3].event_buffer.primary.proc is None)
        writing = {i: jobs[i].process for i in (1, 2, 3, 4)}
        original = jobs[2].event_buffer.primary.proc
        assert original is not None and original.poll() is None
        original.kill()  # Only one owned synthetic media source; no network/device changed.
        s.until(lambda: (reconnected := jobs[2].event_buffer.primary.proc) is not None and reconnected is not original
                         and reconnected.poll() is None, timeout=18)
        assert all(jobs[i].process is writing[i] for i in (1, 3, 4))
        prior = jobs[2].event_episode.deadline
        assert pulse() is None  # Suppressed history duplicate still extends video.
        s.tick()
        assert jobs[2].event_episode.deadline > prior

        soak = min(180, max(2, int(os.getenv("KMVMS_STAGE10_SOAK_SECONDS", "2"))))
        deadline = time.monotonic() + soak
        last = time.monotonic()
        max_buffer = 0
        while time.monotonic() < deadline:
            s.tick()
            assert all(jobs[i].process is writing[i] for i in (1, 3, 4))
            assert jobs[2].process is not None
            max_buffer = max(max_buffer, s.worker.event_buffer_budget.total)
            if time.monotonic() - last >= 19:
                phase("healthy")
                assert pulse() is None
                last = time.monotonic()
            time.sleep(.24)
        flags["failure"] = True
        s.until(lambda: s.status(3)["runtime"]["ai"]["phase"] == "failed")
        assert not s.status(3)["runtime"]["emergency_requested"]
        assert all(jobs[i].process is writing[i] for i in (1, 3, 4))
        s.flags(event_recording_emergency_enabled=True)
        s.until(lambda: all(s.status(i)["runtime"]["emergency_recording"] for i in (3, 4)))
        assert all(jobs[i].process is writing[i] for i in (1, 3, 4))
        assert len(attempts) == 1
        s.flags(recording_suspended_by_low_disk=True)
        # Allow the normal writer/finalization cycle to act on the global hold.
        s.until(lambda: all(jobs[i].process is None for i in (1, 2, 3, 4)), timeout=16)
        with Session(s.engine) as db:
            for cid in (1, 2, 3, 4):
                archived = db.query(RecordingSegment).filter_by(camera_id=cid, status="finalized").all()
                assert archived, f"missing finalized media for camera {cid}"
                for item in archived:
                    path = Path(item.file_path)
                    _width, _height, duration = video_span(path)
                    assert path.is_relative_to(s.worker.STORAGE_ROOT) and duration > 0.1
                    ffmpeg(["-ss", str(duration * .4), "-i", str(path), "-frames:v", "1", "-an", "-f", "null", "-"])
            assert db.query(AuditEvent).filter_by(category="camera_event", target_id="2").count() == 1
        print("STAGE10_MIXED", json.dumps({"soak_seconds": soak, "elapsed_seconds": round(time.monotonic()-opened, 2),
            "four_writers": True, "peak_buffer_bytes": max_buffer, "verified_seekable": True, "ai_attempts_before_backoff": len(attempts),
            "input_loss_isolated": True, "storage_hold": True}))
    finally:
        ai.stop()
        if ai.thread:
            ai.thread.join(5)
