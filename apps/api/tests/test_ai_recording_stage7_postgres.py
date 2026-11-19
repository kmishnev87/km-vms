"""Owned archive fixture: product Recorder, packet tap, policy and PostgreSQL."""
import shutil
import time

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.models import Camera, RecordingSegment, SystemSettings
from app.models.recording import ArchiveRoot
from app.schemas.ai_recording import PolicyUpdate
from app.services.ai_recording import camera_rows, save_policy
from app.services import ai_event_history
from app.services.camera_events import event_query, serialize
from app.services.mobile_push import event_payload
from app.services.ai_rule_frame import make_reference
from recorder_core.ai_supervisor import AiSupervisor
from recorder_core.ai_event_outbox import AiEventOutbox
from recorder_core.event_buffer import BufferedCamera, capture_command
from recorder_core.storage_activation import recording_start_allowed
from test_event_recording_profiles_postgres import ffmpeg, video_span
from test_self_recovery_stage1374 import worker  # noqa: F401
from test_stage14_history_large_totals_postgres import history_database  # noqa: F401
from test_ai_rules_stage8 import LUMA, ZONE, object_at, sample


@pytest.mark.parametrize("mode,profile,with_rules", [("continuous", "none", False), ("events", "balanced", False),
                                                  ("events", "full_quality", False), ("events", "full_quality", True)])
def test_selected_ai_receives_only_recorder_input_and_saves_seekable_media(mode, profile, with_rules, worker, history_database, monkeypatch, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe required")
    engine = history_database
    record_url = "rtsp://192.0.2.4/record"
    live_url = "rtsp://192.0.2.4/preview"
    files = {}
    for url, dims in [(record_url, "640x360"), (live_url, "320x180")]:
        file = tmp_path / (dims + ".mp4")
        ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size={dims}:rate=12", "-t", "8", "-c:v", "libx264",
                "-preset", "ultrafast", "-threads", "2", "-g", "12", str(file)])
        files[url] = file
    opened, inputs, errors = [], [], []
    detected, failed = [False], [False]

    class Inference:
        def __init__(self, gpu):
            assert not gpu

        def ready(self):
            return {"phase": "ready", "backend": "cpu", "version": "fixture-v1", "model": "fixture-model",
                    "device": "CPU", "runtime": "fixture"}

        def analyze(self, frame):
            inputs.append(frame.stream)
            if failed[0]:
                raise TimeoutError
            if with_rules:
                assert frame.advanced
                # The saved frame is daytime. A fixed camera must still
                # trigger on the corresponding IR image, not just draw it.
                return sample(*([object_at(.5)] if detected[0] else []),
                              image=bytes(255-n for n in LUMA) if detected[0] else LUMA)
            return {"phase": "sample", "scene_change": False, "objects": [{"kind": "person"}] if detected[0] else []}

        def close(self):
            pass

    def capture(url, transport):
        args = capture_command(url, transport)
        opened.append(url)
        return args[:args.index("-rtsp_transport")] + ["-re", "-stream_loop", "-1", "-i", str(files[url])] + args[args.index("-i") + 2:]

    monkeypatch.setattr(ai_event_history.settings, "storage_previews", str(tmp_path / "previews"))
    monkeypatch.setattr("app.services.mobile_push.wake_local", lambda: None)
    outbox = AiEventOutbox(ai_event_history.root()) if with_rules else None
    manager = AiSupervisor(Inference, event_sink=outbox)
    monkeypatch.setattr(worker, "local_ai", manager)
    monkeypatch.setattr(worker, "AI_GPU_ALLOWED", False)
    monkeypatch.setattr(worker, "log_event", lambda level, name, **details:
        errors.append((name, details.get("error"))) if level == "error" else None)
    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(worker, "SessionLocal", sessionmaker(engine))
    monkeypatch.setattr(worker, "recording_start_allowed", recording_start_allowed)
    monkeypatch.setattr(worker, "STARTUP_CONFIRM_SECONDS", .25)
    monkeypatch.setattr(worker, "update_camera_status_from_job", lambda job: worker.mark_camera_status(
        job.camera_id, worker.external_camera_status(job.state), job.last_error))
    monkeypatch.setattr(worker, "update_recording_job", lambda job, **kw: worker.recording_job_store().update_recording_job(job, **kw))
    monkeypatch.setattr(worker, "BufferedCamera", lambda *args, **kw: BufferedCamera(*args, command=capture, **kw))
    worker.ensure_recording_metadata_schema()
    with Session(engine) as db:
        db.add(SystemSettings(id=1, system_initialized=True, timezone="UTC", language="en",
                              storage_path=str(worker.STORAGE_ROOT), recording_format="mkv"))
        db.get(ArchiveRoot, worker.DEFAULT_ARCHIVE_ROOT_ID).physical_identity = "pv1:" + "7" * 32
        db.add(Camera(id=1, name="AI fixture", storage_folder_name="ai-source", protocol="rtsp",
                      host="192.0.2.4", port=554, rtsp_transport="tcp", enabled=True, events_enabled=False,
                      rtsp_main_url=record_url, rtsp_sub_url=live_url, default_record_stream="main", default_live_stream="sub",
                      segment_minutes=1))
        db.commit()
        rules = {"rules":[{"id":"gate", "kind":"presence", "points":ZONE}],
                 "reference":make_reference(db.get(Camera, 1),640,360,LUMA)} if with_rules else None
        save_policy(db, 1, PolicyUpdate(expected_revision=0, policy={"mode": mode, "source": "ai", "profile": profile,
            "prebuffer_seconds": 0 if mode == "continuous" else 5, "event_kinds": ["person"], "ai_rules":rules}), actor=None)

    def tick():
        worker.check_children()
        worker.sync_cameras()
        worker.confirm_recording_startups()
        worker.write_recorder_heartbeat()

    def wait_until(predicate, limit=14):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            tick()
            assert not errors, errors
            if predicate():
                return
            time.sleep(.2)
        job = worker.jobs[1]
        pytest.fail(f"did not converge: writer={job.state}/{job.last_error_type}, ai={job.ai_status}, opened={len(opened)}")

    def view():
        with Session(engine) as db:
            return camera_rows(db, [db.get(Camera, 1)])[0]["runtime"]

    try:
        wait_until(lambda: bool(inputs) and view().get("ai", {}).get("phase") == "healthy")
        job = worker.jobs[1]
        if mode == "events":
            assert job.process is None
            assert view()["actual_status"] == "waiting"
            assert opened == [live_url if profile == "balanced" else record_url]
            detected[0] = True
        wait_until(lambda: view()["actual_status"] in {"recording", "extended"} and job.process is not None)
        if profile == "balanced":
            wait_until(lambda: job.process.stream_role == "main" and job.event_buffer.primary.proc is None)
            assert opened == [live_url, record_url]
        else:
            assert opened == [record_url]
        if with_rules:
            assert view()["ai"]["rules_view"] == "aligned"
            wait_until(lambda: any(outbox.root.glob("*.evt")))
            notice = next(outbox.root.glob("*.evt"))
            with Session(engine) as db:
                assert ai_event_history.ingest(db, notice)
                row = event_query(db).one()
                assert row.event_metadata["ai_notify"] is True
                assert row.event_metadata["ai_record"] is True
                assert '"ai_condition":"presence"' in event_payload(serialize(row)).replace(" ", "")
        if mode == "continuous":
            failed[0] = True
            wait_until(lambda: view().get("ai", {}).get("phase") == "failed")
            assert view()["actual_status"] == "recording"
            assert not view()["emergency_requested"]
        with Session(engine) as db:
            db.get(Camera, 1).enabled = False
            db.commit()
        wait_until(lambda: job.process is None and view()["actual_status"] == "paused")
        with Session(engine) as db:
            media = db.query(RecordingSegment).filter_by(camera_id=1, status="finalized").all()
            assert media
            assert any(video_span(worker.STORAGE_ROOT / item.relative_path)[0:2] == (640, 360) for item in media)
    finally:
        if job := worker.jobs.get(1):
            worker.stop_camera(1, "fixture_done")
            if job.event_buffer:
                job.event_buffer.close()
        manager.stop()
        if outbox:
            outbox.stop()
        if manager.thread:
            manager.thread.join(5)
