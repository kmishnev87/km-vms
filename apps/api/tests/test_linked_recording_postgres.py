"""Real Recorder, media and archive tests in an owned disposable PostgreSQL."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Camera, RecordingJob, RecordingSegment, SystemSettings
from app.models.recording import ArchiveRoot
from app.models.storage_operation import StorageOperation
from app.schemas.ai_recording import PolicyUpdate
from app.services import ai_recording, camera_events
from app.services.activation_camera_intent import CameraPauseCoordinator
from app.services.camera_event_health import Recovery, publish_channel
from app.services.onvif_events import Signal
from app.services.recorder_runtime_status import list_camera_recording_states
from app.services.storage_operations_foundation import acquire_worker_lease
from recorder_core.event_buffer import BufferedCamera, capture_command
from recorder_core.storage_activation import recording_start_allowed
from test_event_recording_profiles_postgres import ffmpeg, now, video_span
from test_self_recovery_stage1374 import worker
from test_stage14_history_large_totals_postgres import history_database


@pytest.fixture
def stage5(history_database, worker, monkeypatch, tmp_path):
    engine = history_database

    @event.listens_for(engine, "checkout")
    def local_clock(connection, *_):
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'Asia/Yekaterinburg'")
        connection.commit()

    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(worker, "SessionLocal", sessionmaker(engine))
    monkeypatch.setattr(worker, "update_camera_status_from_job", lambda job: worker.mark_camera_status(
        job.camera_id, worker.external_camera_status(job.state), job.last_error))
    monkeypatch.setattr(worker, "update_recording_job",
                        lambda job, **kw: worker.recording_job_store().update_recording_job(job, **kw))
    monkeypatch.setattr(worker, "recording_start_allowed", recording_start_allowed)
    monkeypatch.setattr(worker, "STARTUP_CONFIRM_SECONDS", .25)
    worker.ensure_recording_metadata_schema()

    clips = {}
    configured = {role: "rtsp://192.0.2.50/" + role for role in ("preview", "recording", "source")}
    for role, width in (("preview", 320), ("recording", 640), ("source", 480)):
        path = tmp_path / f"{role}.mp4"
        ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size={width}x180:rate=12",
                "-t", "6", "-c:v", "libx264", "-threads", "2", "-g", "12",
                "-preset", "ultrafast", str(path)])
        clips[configured[role]] = str(path)
    connected = []

    def local_command(args, url):
        connected.append(url)
        return (args[:args.index("-rtsp_transport")] + ["-re", "-stream_loop", "-1", "-i", clips[url]]
                + args[args.index("-i") + 2:])

    def local_capture(url, transport):
        return local_command(capture_command(url, transport), url)

    original_ffmpeg_cmd = worker.ffmpeg_cmd

    def render_ffmpeg_command(row, job):
        args, pattern = original_ffmpeg_cmd(row, job)
        if args and not job.event_buffer:
            args = local_command(args, worker.choose_input_url(row))
        return args, pattern

    monkeypatch.setattr(worker, "BufferedCamera", lambda *args: BufferedCamera(*args, command=local_capture))
    monkeypatch.setattr(worker, "ffmpeg_cmd", render_ffmpeg_command)

    with Session(engine) as db:
        db.add(SystemSettings(id=1, system_initialized=True, timezone="Etc/GMT-5", language="en",
                              storage_path=str(worker.STORAGE_ROOT), recording_format="mp4"))
        db.get(ArchiveRoot, worker.DEFAULT_ARCHIVE_ROOT_ID).physical_identity = "pv1:" + "5" * 32
        db.commit()

    def camera(cid, *, enabled=True, events=False, source=True, unavailable=False):
        with Session(engine) as db:
            db.add(Camera(id=cid, name=f"Owned {cid}", storage_folder_name=f"stage5-{cid}", enabled=enabled,
                          protocol="onvif" if events else "rtsp", events_enabled=events,
                          host="192.0.2.50", port=80, rtsp_transport="tcp",
                          rtsp_main_url=None if unavailable else configured["source" if source else "preview"],
                          rtsp_sub_url=None if source else configured["recording"],
                          default_record_stream="main" if source else "sub", default_live_stream="main",
                          recording_mode="always" if source else "off", segment_minutes=1))
            db.commit()

    def policy(cid, **configured_policy):
        with Session(engine) as db:
            model = db.get(Camera, cid)
            revision = (model.recording_policy or {}).get("revision", 0)
            return ai_recording.save_policy(db, cid, PolicyUpdate(
                expected_revision=revision, policy=configured_policy), actor=None)

    def follow(cid, src, profile="economical"):
        return policy(cid, mode="linked", source="none", linked_camera_id=src, profile=profile,
                      prebuffer_seconds=4 if profile != "economical" else 0, postrecord_minutes=5)

    def status(cid):
        with Session(engine) as db:
            return ai_recording.camera_rows(db, [db.get(Camera, cid)])[0]

    def toggle(cid, on):
        with Session(engine) as db:
            row = db.get(Camera, cid)
            ai_recording.reset_recording_cutoff(db, row, keep_channel=True)
            row.enabled = on
            row.status = "enabled" if on else "disabled"
            row.last_error = None
            db.commit()

    def flags(**values):
        with Session(engine) as db:
            for key, value in values.items():
                setattr(db.get(SystemSettings, 1), key, value)
            db.commit()

    def tick():
        worker.check_children()
        worker.sync_cameras()
        worker.confirm_recording_startups()
        worker.write_recorder_heartbeat()

    def until(predicate, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            tick()
            if predicate():
                return
            time.sleep(.2)
        pytest.fail(f"Recorder timed out: {[(cid, job.state) for cid, job in worker.jobs.items()]}")

    def verify_archive(job_id, *, width, roles):
        with Session(engine) as db:
            rows = db.query(RecordingSegment).filter_by(job_id=job_id).order_by(RecordingSegment.started_at).all()
            assert rows and db.get(RecordingJob, job_id).stopped_at
            assert {row.stream_type for row in rows} == roles
            assert all(row.status == "finalized" and row.ended_at > row.started_at for row in rows)
            for row in rows:
                path = Path(row.file_path)
                assert path.is_relative_to(worker.STORAGE_ROOT) and path.is_file()
                assert video_span(path)[0] == (320 if row.stream_type == "main" and len(roles) > 1 else width)
                duration = (row.ended_at - row.started_at).total_seconds()
                ffmpeg(["-ss", f"{duration * .4:.4f}", "-i", str(path), "-frames:v", "1", "-an", "-f", "null", "-"])

    try:
        yield SimpleNamespace(engine=engine, camera=camera, follow=follow, policy=policy, tick=tick, until=until,
                              toggle=toggle, flags=flags, status=status, worker=worker, connected=connected,
                              streams=configured, verify_archive=verify_archive)
    finally:
        for cid, job in list(worker.jobs.items()):
            try:
                if job.process:
                    worker.stop_camera(cid, "fixture_cleanup")
            finally:
                if job.event_buffer:
                    job.event_buffer.close()
        assert worker.event_buffer_budget.total == 0


def event_source(stage5, cid=90, *, unavailable=True):
    stage5.camera(cid, events=True, unavailable=unavailable)
    stage5.policy(cid, mode="events", source="camera", profile="economical", postrecord_minutes=1)
    target = {"id": cid, "name": "Owned source", "events_enabled": True}
    info = {"device_id": "stage5-fixture", "recording_generation": "fixture-v1", "cameras": [target]}
    with Session(stage5.engine) as db:
        lease = acquire_worker_lease(db, worker_key="stage5_fixture_onvif", owner_instance_id="fixture", lease_seconds=180)

    def phase(value):
        with Session(stage5.engine) as db:
            publish_channel(db, info, lease, "fixture-v1", value, sample=now(), recovery=Recovery())
            db.commit()

    def pulse():
        at = now()
        with Session(stage5.engine) as db:
            return camera_events.record_signal(db, info, target, Signal(
                "RuleEngine/CellMotionDetector/Motion", "motion", True, False, at, at, False,
                source={"VideoSourceConfigurationToken": "fixture"}))

    return phase, pulse


def test_event_dependent_uses_own_video_and_source_lifetime_even_when_source_video_fails(stage5):
    phase, pulse = event_source(stage5)
    stage5.camera(1, source=False)
    stage5.follow(1, 90, "balanced")
    phase("healthy")
    stage5.tick()
    job, source = stage5.worker.jobs[1], stage5.worker.jobs[90]
    assert job.process is None and source.process is None
    assert stage5.status(1)["runtime"]["status"] == "waiting_linked_source"
    stage5.until(lambda: job.event_buffer.status()["prebuffer_seconds_available"] >= 2)
    assert stage5.streams["recording"] not in stage5.connected
    assert pulse()
    stage5.until(lambda: job.state == "recording" and job.process.stream_role == "sub")
    assert source.state == "restarting" and source.process is None
    writer, saved = job.process, job.db_job_id
    prior = job.link_intent.deadline
    assert pulse() is None  # Same event can be absent from UI but extend the source.
    stage5.tick()
    info = stage5.status(1)["runtime"]
    assert job.process is writer and job.db_job_id == saved
    assert job.link_intent.deadline == source.event_episode.deadline and job.link_intent.deadline > prior
    assert info["status"] == "extended" and info["confirmed_recording"]
    assert info["link"]["source_intent"] == "event" and info["link"]["postrecord_inherited"]
    assert info["link"]["source_writer_state"] == "restarting" and not info["link"]["source_recording_confirmed"]
    deadline = time.monotonic() + 1.2
    stage5.until(lambda: time.monotonic() >= deadline)

    source.event_episode.deadline = time.monotonic() - 1
    stage5.tick()
    assert job.process is None and job.enabled and job.state == "idle"
    stage5.verify_archive(saved, width=640, roles={"main", "sub"})
    assert stage5.status(1)["runtime"]["status"] == "waiting_linked_source"

    phase("failed")
    stage5.tick()
    assert job.process is None and stage5.status(1)["runtime"]["status"] == "linked_source_fault"
    stage5.flags(event_recording_emergency_enabled=True)
    stage5.until(lambda: job.state == "recording")
    stage5.tick()
    emergency = stage5.status(1)["runtime"]
    assert emergency["emergency_recording"] and emergency["link"]["source_intent"] == "emergency"

    stage5.toggle(1, False)
    stage5.tick()
    assert job.process is None and stage5.status(1)["runtime"]["status"] == "paused"
    stage5.toggle(1, True)
    stage5.until(lambda: job.state == "recording")
    stage5.toggle(90, False)
    stage5.tick()
    info = stage5.status(1)
    assert job.process is None and job.event_buffer is None
    assert info["recording_enabled"] and info["runtime"]["status"] == "waiting_linked_source"
    assert info["runtime"]["source_status"] == "source_paused"


def test_continuous_link_storage_source_switch_and_delete(stage5):
    stage5.camera(91)
    stage5.camera(92, enabled=False)
    stage5.camera(1, source=False)
    stage5.follow(1, 91, "full_quality")
    with Session(stage5.engine) as db:
        db.add(StorageOperation(id="stage5-root-fence", operation_type="archive_root_activation",
                                actor_kind="system", actor_key="system:fixture", system_owner="fixture",
                                idempotency_key="stage5-root-fence", request_fingerprint="b" * 64,
                                status="queued", scope={}, progress={}))
        db.commit()
    stage5.tick()
    job, source = stage5.worker.jobs[1], stage5.worker.jobs[91]
    assert job.waiting_for_storage_activation and job.process is None
    assert job.event_buffer.primary.proc is None
    with Session(stage5.engine) as db:
        db.get(StorageOperation, "stage5-root-fence").status = "completed"
        db.commit()
    stage5.until(lambda: job.state == "recording" and source.state == "recording")
    stage5.tick()
    view = stage5.status(1)["runtime"]
    assert view["link"]["source_mode"] == "continuous" and view["link"]["source_recording_confirmed"]
    assert view["prebuffer_status"] == "not_applicable" and view["prebuffer_seconds_available"] == 0
    assert stage5.streams["preview"] not in stage5.connected
    with Session(stage5.engine) as db:
        coordinator = CameraPauseCoordinator(list_camera_recording_states, None, None, 5, .1)
        assert coordinator._confirmed_recording_camera_ids(db) == {1, 91}  # includes linked with legacy "off"
    saved = job.db_job_id

    stage5.toggle(91, False)
    stage5.tick()
    assert job.process is None and job.enabled and not source.enabled
    stage5.verify_archive(saved, width=640, roles={"sub"})
    stage5.toggle(91, True)
    stage5.until(lambda: job.state == "recording")
    assert job.db_job_id != saved
    stage5.follow(1, 92, "full_quality")
    stage5.tick()
    assert job.process is None and source.process is not None
    assert stage5.status(1)["runtime"]["link"]["source_camera_id"] == 92
    stage5.toggle(92, True)
    stage5.until(lambda: job.state == "recording")
    stage5.flags(recording_suspended_by_low_disk=True)
    stage5.tick()
    assert job.process is None and stage5.status(1)["runtime"]["status"] == "storage_suspended"
    stage5.flags(recording_suspended_by_low_disk=False)
    stage5.until(lambda: job.state == "recording" and source.state == "recording")
    with Session(stage5.engine) as db:
        db.get(Camera, 92).deleted_at = now()
        db.commit()
    stage5.tick()
    assert source.process is not None and job.process is None
    info = stage5.status(1)
    assert info["recording_enabled"] and info["runtime"]["status"] == "linked_source_unavailable"
    assert info["runtime"]["source_status"] == "source_unavailable"


def test_short_pause_between_poll_and_start_fences_stale_source_pulse(stage5, monkeypatch):
    phase, pulse = event_source(stage5)
    stage5.camera(1, source=False)
    stage5.follow(1, 90)
    phase("healthy")
    stage5.tick()
    assert pulse()
    start = stage5.worker.start_camera
    seen = []

    def protect(row):
        if row.id == 1 and not seen:
            seen.append(True)
            stage5.toggle(90, False)
            stage5.toggle(90, True)
        start(row)

    monkeypatch.setattr(stage5.worker, "start_camera", protect)
    stage5.tick()
    assert seen and stage5.worker.jobs[1].process is None
    assert not stage5.worker.jobs[1].waiting_for_storage_activation
    stage5.tick()
    assert stage5.status(1)["runtime"]["status"] == "waiting_linked_source"
    pulse()
    stage5.until(lambda: stage5.worker.jobs[1].state == "recording")
