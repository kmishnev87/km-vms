"""One real Recorder/FFmpeg episode on an owned PostgreSQL and tmpfs archive."""
import shutil
import subprocess
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Camera, RecordingJob, RecordingSegment, SystemSettings
from app.models.recording import ArchiveRoot
from app.models.storage_operation import StorageOperation
from app.schemas.ai_recording import PolicyUpdate
from app.services import ai_recording, camera_events
from app.services.camera_event_health import Recovery, publish_channel
from app.services.onvif_events import Signal
from app.services.storage_operations_foundation import acquire_worker_lease
from recorder_core.storage_activation import recording_start_allowed
from recorder_core.event_buffer import BufferedCamera, capture_command
from test_self_recovery_stage1374 import worker
from test_stage14_history_large_totals_postgres import history_database


def test_fenced_receiver_signal_reaches_real_writer_and_seekable_archive(history_database, worker, monkeypatch, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe required")
    engine = history_database

    @event.listens_for(engine, "checkout")
    def use_nas_database_clock(connection, *_):
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'Asia/Yekaterinburg'")
        connection.commit()

    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(worker, "SessionLocal", sessionmaker(engine))
    worker.ensure_recording_metadata_schema()
    # Reuse the existing fixture's Recorder definitions. Use the real status
    # and job writes for this test, and only replace the RTSP transport input.
    monkeypatch.setattr(worker, "update_camera_status_from_job", lambda job: worker.mark_camera_status(
        job.camera_id, worker.external_camera_status(job.state), job.last_error))
    monkeypatch.setattr(worker, "update_recording_job", lambda job, **kwargs: worker.recording_job_store().update_recording_job(job, **kwargs))
    monkeypatch.setattr(worker, "recording_start_allowed", recording_start_allowed)
    monkeypatch.setattr(worker, "STARTUP_CONFIRM_SECONDS", .5)
    source = tmp_path / "synthetic.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "8",
                    "-c:v", "libx264", "-g", "12", "-preset", "ultrafast", "-c:a", "aac",
                    str(source)], check=True, timeout=25, capture_output=True)
    def local_input(url, transport):
        assert url == "rtsp://192.0.2.1/selected"
        args = capture_command(url, transport)
        return args[:args.index("-rtsp_transport")] + ["-re", "-stream_loop", "-1", "-i", str(source)] + args[args.index("-i") + 2:]

    monkeypatch.setattr(worker, "BufferedCamera", lambda *args: BufferedCamera(*args, command=local_input))
    with Session(engine) as db:
        db.add(SystemSettings(id=1, system_initialized=True, timezone="UTC", language="en",
                              storage_path=str(worker.STORAGE_ROOT), recording_format="mkv"))
        db.get(ArchiveRoot, worker.DEFAULT_ARCHIVE_ROOT_ID).physical_identity = "pv1:" + "3" * 32
        db.add(Camera(id=1, name="Owned input", storage_folder_name="owned-input", protocol="onvif",
                      host="192.0.2.1", port=80, rtsp_transport="tcp", enabled=True, events_enabled=True,
                      rtsp_main_url="rtsp://192.0.2.1/other", rtsp_sub_url="rtsp://192.0.2.1/selected",
                      default_record_stream="sub", segment_minutes=1))
        db.commit()
        ai_recording.save_policy(db, 1, PolicyUpdate(expected_revision=0, policy={
            "mode": "events", "source": "camera", "profile": "economical", "postrecord_minutes": 1,
        }), actor=None)
        lease = acquire_worker_lease(db, worker_key="stage3_test_onvif", owner_instance_id="test-owner", lease_seconds=150)
    group = {"device_id": "test-device", "recording_generation": "test-generation",
             "cameras": [{"id": 1, "name": "Owned input", "events_enabled": True}]}

    def now():
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def publish(phase, recovery=None):
        with Session(engine) as db:
            publish_channel(db, group, lease, "test-generation", phase, sample=now(), recovery=recovery or Recovery())
            db.commit()

    def activity():
        at = now()
        signal = Signal("RuleEngine/CellMotionDetector/Motion", "motion", True, False, at, at, False,
                        source={"VideoSourceConfigurationToken": "owned"})
        with Session(engine) as db:
            return camera_events.record_signal(db, group, group["cameras"][0], signal)

    try:
        # First state write also exercises the JSON-null to object merge on PG.
        worker.sync_cameras()
        assert worker.jobs[1].process is None
        publish("healthy")
        worker.sync_cameras()
        assert worker.jobs[1].process is None and worker.jobs[1].event_decision.source_status == "healthy"
        with Session(engine) as db:
            db.execute(text("UPDATE storage_worker_leases SET lease_expires_at = LOCALTIMESTAMP - INTERVAL '1 second'"))
            db.commit()
        worker.sync_cameras()
        assert worker.jobs[1].process is None and worker.jobs[1].event_decision.source_status == "connecting"
        with Session(engine) as db:
            lease = acquire_worker_lease(db, worker_key=lease.worker_key, owner_instance_id="replacement-owner", lease_seconds=150)
        # The previous receiver's evidence cannot become current again merely
        # because the same worker key now belongs to a replacement.
        worker.sync_cameras()
        assert worker.jobs[1].event_decision.source_status == "connecting"
        publish("healthy")
        assert activity()
        with Session(engine) as db:
            db.add(StorageOperation(id="stage3-root-fence", operation_type="archive_root_activation",
                actor_kind="system", actor_key="system:fixture", system_owner="fixture",
                idempotency_key="stage3-root-fence", request_fingerprint="a" * 64,
                status="queued", scope={}, progress={}))
            db.commit()
        worker.sync_cameras()
        assert worker.jobs[1].process is None and worker.jobs[1].waiting_for_storage_activation
        assert worker.jobs[1].event_buffer.primary.proc is None
        with Session(engine) as db:
            db.get(StorageOperation, "stage3-root-fence").status = "completed"
            db.commit()
        worker.sync_cameras()
        job = worker.jobs[1]
        writer, job_id = job.process, job.db_job_id
        assert writer and job_id
        assert activity() is None  # Audible/visible history can be deduplicated.
        worker.sync_cameras()
        assert job.process is writer and job.db_job_id == job_id and job.event_decision.extended

        media_deadline = time.monotonic() + 12
        while job.state != "recording" and time.monotonic() < media_deadline:
            time.sleep(.25)
            worker.sync_cameras()
            worker.confirm_recording_startups()
        assert job.state == "recording"
        worker.sync_cameras()
        worker.write_recorder_heartbeat()
        with Session(engine) as db:
            card = ai_recording.camera_rows(db, [db.get(Camera, 1)])[0]["runtime"]
            assert card["status"] == "extended" and card["confirmed_recording"] and not card["emergency_recording"]
            assert camera_events.event_query(db).count() == 1

        job.event_episode.deadline = time.monotonic() - 1
        worker.sync_cameras()
        assert job.process is None
        with Session(engine) as db:
            saved = db.get(RecordingJob, job_id)
            segment = db.query(RecordingSegment).filter_by(job_id=job_id).one()
            assert saved.state == "idle" and saved.stop_reason == "event_recording_waiting"
            assert segment.status == "finalized" and segment.finalized_at and segment.ended_at
            assert db.query(RecordingJob).count() == 1
            assert ai_recording.camera_rows(db, [db.get(Camera, 1)])[0]["runtime"]["status"] == "waiting"
            path = segment.file_path
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", path], check=True, capture_output=True, text=True, timeout=15)
        assert float(probe.stdout.strip()) >= .5
        subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", path, "-frames:v", "1", "-f", "null", "-"],
                       check=True, capture_output=True, timeout=15)

        recovery = Recovery()
        recovery.failed("test_timeout", now())
        publish("failed", recovery)
        worker.sync_cameras()
        assert job.process is None
        with Session(engine) as db:
            db.get(SystemSettings, 1).event_recording_emergency_enabled = True
            db.commit()
        worker.sync_cameras()
        assert job.process is not None and job.event_decision.request == "emergency"
        with Session(engine) as db:
            db.get(SystemSettings, 1).recording_suspended_by_low_disk = True
            db.commit()
        worker.sync_cameras()
        assert job.process is None
        with Session(engine) as db:
            db.get(SystemSettings, 1).recording_suspended_by_low_disk = False
            db.get(Camera, 1).enabled = False
            db.commit()
        worker.sync_cameras()
        assert job.process is None
        with Session(engine) as db:
            status = db.get(Camera, 1).recording_signal_state["recorder"]["status"]
            assert status == "paused"
    finally:
        job = worker.jobs.get(1)
        if job and job.process and job.process.poll() is None:
            job.process.terminate()
            job.process.wait(timeout=10)
