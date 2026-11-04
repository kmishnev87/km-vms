"""Recovery regressions: no real cameras, host settings, or archive mutations."""
import contextlib
import importlib
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.models.camera import Camera
from app.services.recorder_runtime_status import age_seconds

live_module = importlib.import_module("app.services.live_engine.manager")
live_instance_module = importlib.import_module("app.services.live_engine.stream_instance")


class Process:
    pid = 912345
    stderr = None

    def __init__(self, code=None):
        self.code = code
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.code


def camera():
    return Camera(id=1374, name="Recovery camera", storage_folder_name="recovery", enabled=True,
                  protocol="rtsp", host="example.invalid", port=554,
                  rtsp_main_url="rtsp://example.invalid/main", rtsp_sub_url="rtsp://example.invalid/sub",
                  rtsp_transport="tcp", recording_mode="always", segment_minutes=5)


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_previews", str(tmp_path))
    instance = live_module.StreamInstance(1374, "sub")
    monkeypatch.setattr(instance, "_audit_live_event", Mock())
    return instance


@pytest.fixture
def worker(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "archive"))
    source = (Path(__file__).resolve().parents[2] / "recorder" / "main.py").read_text()
    # Load the real definitions, not the service entry point and its infinite loop.
    source = source.split("\nsignal.signal(signal.SIGTERM", 1)[0]
    module = types.ModuleType("recorder_stage1374")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "recorder_stage1374", "exec"), module.__dict__)
    module.events = []
    monkeypatch.setattr(module, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "write_audit_event", lambda **kw: module.events.append(kw))
    monkeypatch.setattr(module, "update_camera_status_from_job", lambda job: None)
    monkeypatch.setattr(module, "update_recording_job", lambda *a, **kw: None)
    monkeypatch.setattr(module, "clear_recording_job_error", lambda job: None)
    return module


def worker_job(worker, now):
    job = worker.RecordingJob(1, "Recovery camera", "recovery", "always", True)
    job.process = Process()
    job.started_at = now - 120
    job.state = worker.RecorderState.RECORDING
    worker.jobs[job.camera_id] = job
    return job


def test_future_timestamp_is_not_fresh_evidence():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert age_seconds(now + timedelta(hours=5), now) is None
    assert age_seconds(now + timedelta(seconds=2), now) == 0
    assert age_seconds(now - timedelta(seconds=120), now) == 120


def test_stall_without_any_output_requires_two_samples_and_restarts_only_affected(worker, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(worker, "time", types.SimpleNamespace(time=lambda: now))
    job = worker_job(worker, now)
    other = worker.RecordingJob(2, "Other camera", "other", "always", True, process=Process())
    worker.jobs[2] = other
    monkeypatch.setattr(worker, "current_writing_segment", lambda job: None)
    stops = []

    def stop(camera_id, reason, **kwargs):
        stops.append((camera_id, reason, kwargs))
        job.process.terminate()
        job.process = None

    monkeypatch.setattr(worker, "stop_camera", stop)
    row = types.SimpleNamespace(id=1, segment_minutes=5)
    assert worker.handle_stale_current_segment(row, job) is False
    assert not stops
    assert worker.handle_stale_current_segment(row, job) is True
    assert stops[0][0] == 1
    assert stops[0][1] == worker.ERROR_MEDIA_STALLED
    assert other.process.poll() is None
    assert job.state == worker.RecorderState.RESTARTING
    assert job.next_retry_at > now
    assert len(worker.events) == 2  # detected once, retry scheduled once


def test_fourth_and_later_stall_recoveries_are_not_permanently_blocked(worker, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(worker, "time", types.SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(worker, "current_writing_segment", lambda job: None)
    job = worker_job(worker, now)
    monkeypatch.setattr(worker, "stop_camera", lambda *a, **kw: None)
    row = types.SimpleNamespace(id=1, segment_minutes=60)
    for attempt in range(1, 9):
        job.stalled_samples = 1
        assert worker.handle_stale_current_segment(row, job) is True
        assert job.stale_recovery_count == attempt
        assert job.next_retry_at - now <= 120
        assert job.state == worker.RecorderState.RESTARTING


def test_healthy_progress_does_not_trigger_stall_and_rotation_has_distinct_reason(worker, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(worker, "time", types.SimpleNamespace(time=lambda: now))
    job = worker_job(worker, now)
    job.last_media_progress_at = now - 1
    monkeypatch.setattr(worker, "current_writing_segment", lambda job: Path("part.mkv"))
    monkeypatch.setattr(worker, "storage_relative_path", lambda *a, **kw: "part.mkv")
    monkeypatch.setattr(worker, "current_segment_age_seconds", lambda p: 599)
    stop = Mock()
    monkeypatch.setattr(worker, "stop_camera", stop)
    row = types.SimpleNamespace(id=1, segment_minutes=5)
    assert worker.handle_stale_current_segment(row, job) is False
    stop.assert_not_called()
    monkeypatch.setattr(worker, "current_segment_age_seconds", lambda p: 601)
    assert worker.handle_stale_current_segment(row, job) is True
    assert stop.call_args.args[1] == worker.ERROR_STALE_SEGMENT


def test_stable_progress_resets_retry_budget_but_process_start_alone_does_not(worker, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(worker, "time", types.SimpleNamespace(time=lambda: now))
    job = worker_job(worker, now)
    job.state = worker.RecorderState.STARTING
    job.retry_count = 8
    job.stale_recovery_count = 8
    worker.confirm_recording_startups()
    assert job.state == worker.RecorderState.STARTING
    assert job.retry_count == 8
    job.last_media_progress_at = now - 1
    job.stable_progress_since = now - 5
    worker.confirm_recording_startups()
    assert job.state == worker.RecorderState.RECORDING
    assert job.retry_count == 8
    job.stable_progress_since = now - 61
    worker.confirm_recording_startups()
    assert job.retry_count == job.stale_recovery_count == 0
    assert job.next_retry_at is None
    assert worker.events[-1]["event_type"] == "recovery_succeeded"


@pytest.mark.parametrize("kind", ["auth_failed", "invalid_rtsp", "storage_unavailable", "permission_denied"])
def test_non_network_blockers_use_slow_retry(worker, monkeypatch, kind):
    now = 1000.0
    monkeypatch.setattr(worker, "time", types.SimpleNamespace(time=lambda: now))
    job = worker_job(worker, now)
    worker.schedule_retry(job, "Needs attention", kind)
    assert job.next_retry_at - now == 300


@pytest.mark.parametrize("detail,expected", [
    ("No route to host", "camera_unreachable"),
    ("Connection refused", "camera_unreachable"),
    ("Connection timed out", "camera_unreachable"),
    ("401 Unauthorized", "auth_failed"),
    ("QSV: failed to create device", None),
    ("[aac @ 0x55c401403780] Non-monotonic DTS; previous: 140131, current: 140132; frame=401", None),
    ("Server returned 401", "auth_failed"),
    ("method DESCRIBE failed: 403", "auth_failed"),
])
def test_live_source_failure_classification(detail, expected):
    assert live_module.classify_live_failure(detail) == expected


def test_ffmpeg_numbers_are_not_authentication_failures_in_recorder_or_summary(worker):
    from app.services.system_runtime_status import _safe_reason_from_error
    detail = "[aac @ 0x55c401403780] Non-monotonic DTS; previous: 140131, current: 140132; frame=401"
    assert worker.classify_error(detail, exit_code=1) == worker.ERROR_PROCESS_CRASHED
    assert _safe_reason_from_error(detail) != "auth_failed"
    for detail in ["401 Unauthorized", "403 Forbidden", "method DESCRIBE failed: 403"]:
        assert worker.classify_error(detail, exit_code=1) == worker.ERROR_AUTH_FAILED
        assert _safe_reason_from_error(detail) == "auth_failed"


def test_live_readiness_requires_fresh_files_and_living_process(live, monkeypatch):
    live.stream_dir.mkdir(parents=True)
    playlist = live.playlist_path
    segment = live.stream_dir / "seg_000001.ts"
    playlist.write_text("#EXTM3U\nseg_000001.ts\n")
    segment.write_bytes(b"video")
    monkeypatch.setattr(live, "is_running", lambda: False)
    assert live.is_ready() is False
    monkeypatch.setattr(live, "is_running", lambda: True)
    assert live.is_ready() is True
    old = time.time() - 120
    os.utime(playlist, (old, old))
    os.utime(segment, (old, old))
    assert live.is_ready() is False


def test_network_failure_does_not_mark_hardware_failed_or_fall_back_to_cpu(live, monkeypatch):
    live.proc = Process(1)
    live.mode = "hardware_transcode"
    live.camera_source = camera()
    monkeypatch.setattr(live, "_read_log_tail", lambda *a: "Error opening input: No route to host")
    monkeypatch.setattr(live, "process_info", lambda: {"running_verified": False})
    fallback = Mock()
    monkeypatch.setattr(live, "_retry_next_backend_or_cpu_locked", fallback)
    live.maintain_startup()
    fallback.assert_not_called()
    assert live.status == "restarting"
    assert live.failure_reason == "camera_unreachable"
    assert live.next_retry_at is not None
    assert not live.failed_backends


def test_live_retries_are_capped_not_exhausted_and_reset_after_real_recovery(live, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(live_instance_module, "time", types.SimpleNamespace(time=lambda: now))
    for attempt in range(1, 9):
        live.next_retry_at = None
        live._schedule_retry_locked("camera_unreachable")
        assert live.retry_count == attempt
        assert 0 < live.next_retry_at - now <= 120
    monkeypatch.setattr(live, "is_ready", lambda: True)
    monkeypatch.setattr(live, "is_running", lambda: True)
    monkeypatch.setattr(live, "process_info", lambda: {"running_verified": True})
    monkeypatch.setattr(live, "_read_log_tail", lambda *a: "")
    monkeypatch.setattr(live, "_maybe_escalate_cpu_to_hardware_locked", lambda *a: False)
    live.healthy_since = now - 31
    live.maintain_startup()
    assert live.retry_count == 0
    assert live.next_retry_at is None
    assert live.ever_ready


def test_closing_one_of_two_viewers_does_not_cancel_other_viewer(monkeypatch):
    manager = live_module.StreamManager()
    monkeypatch.setattr(manager, "_launch_viewer_start", lambda *a: None)
    first = manager.open_viewer(camera(), "sub")
    second = manager.open_viewer(camera(), "sub")
    instance = manager.streams[(1374, "sub")]
    assert manager.close_viewer(first["viewer_id"])
    assert not instance.start_cancelled.is_set()
    assert manager.touch_viewer(second["viewer_id"])
    assert manager.close_viewer(second["viewer_id"])
    assert instance.start_cancelled.is_set()


def test_close_during_probe_returns_quickly_and_never_spawns_unwanted_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_previews", str(tmp_path))
    manager = live_module.StreamManager()
    entered, proceed = threading.Event(), threading.Event()

    def choose(self, *args, **kwargs):
        entered.set()
        assert proceed.wait(3)
        return "hardware_transcode"

    monkeypatch.setattr(live_module.StreamInstance, "_choose_mode", choose)
    monkeypatch.setattr(live_module.StreamInstance, "_audit_live_event", lambda *a, **kw: None)
    spawn = Mock()
    monkeypatch.setattr(live_module.subprocess, "Popen", spawn)
    started = time.monotonic()
    viewer = manager.open_viewer(camera(), "sub")
    assert time.monotonic() - started < 1
    assert entered.wait(2)
    instance = manager.streams[(1374, "sub")]
    try:
        assert manager.close_viewer(viewer["viewer_id"])
        assert instance.start_cancelled.is_set()
    finally:
        proceed.set()
        instance.start_thread.join(3)
    assert not instance.start_thread.is_alive()
    spawn.assert_not_called()
    manager.cleanup()
    assert (1374, "sub") not in manager.streams


def test_live_retry_reloads_corrected_camera_configuration(monkeypatch):
    manager = live_module.StreamManager()
    original = camera()
    corrected = camera()
    corrected.rtsp_sub_url = "rtsp://example.invalid/corrected"
    db = Mock()
    db.get.return_value = corrected
    monkeypatch.setattr(live_module, "SessionLocal", lambda: contextlib.nullcontext(db))
    start = Mock(return_value={"ok": True})
    monkeypatch.setattr(live_module.StreamInstance, "start", start)
    viewer = manager.open_viewer(original, "sub")
    instance = manager.streams[(1374, "sub")]
    instance.start_thread.join(3)
    db.get.assert_not_called()
    instance.retry_count = 1
    manager._launch_viewer_start(instance, live_module._camera_source(original))
    instance.start_thread.join(3)
    db.get.assert_called_once_with(Camera, original.id)
    assert start.call_args.args[0].rtsp_sub_url == corrected.rtsp_sub_url
    manager.close_viewer(viewer["viewer_id"])


def test_live_retry_does_not_restart_a_deleted_camera(monkeypatch):
    manager = live_module.StreamManager()
    db = Mock()
    db.get.return_value = None
    monkeypatch.setattr(live_module, "SessionLocal", lambda: contextlib.nullcontext(db))
    start = Mock(return_value={"ok": True})
    monkeypatch.setattr(live_module.StreamInstance, "start", start)
    viewer = manager.open_viewer(camera(), "sub")
    instance = manager.streams[(1374, "sub")]
    instance.start_thread.join(3)
    instance.retry_count = 1
    manager._launch_viewer_start(instance, live_module._camera_source(camera()))
    instance.start_thread.join(3)
    assert start.call_count == 1
    assert not manager.touch_viewer(viewer["viewer_id"])
    assert instance.start_cancelled.is_set()


def test_new_viewer_arriving_during_cleanup_stop_is_recovered(monkeypatch):
    manager = live_module.StreamManager()
    monkeypatch.setattr(manager, "_launch_viewer_start", lambda *a: None)
    viewer = manager.open_viewer(camera(), "sub")
    instance = manager.streams[(1374, "sub")]
    instance.ever_ready = True
    assert manager.close_viewer(viewer['viewer_id'])

    def stop(**kwargs):
        # The real terminate/wait releases execution to another request thread.
        # Reproduce that request deterministically, without sleeps/processes.
        manager.open_viewer(camera(), "sub")
        instance.status = "stopped"

    monkeypatch.setattr(instance, "stop", stop)
    monkeypatch.setattr(instance, "_audit_live_event", lambda *a, **kw: None)
    manager.cleanup()
    assert (1374, "sub") in manager.streams
    assert not instance.start_cancelled.is_set()
    assert instance.status == "restarting"
    assert instance.next_retry_at is not None
    assert instance.failure_reason == "stream_stalled"


def test_existing_viewer_of_stopped_stream_is_not_left_waiting_forever(monkeypatch):
    manager = live_module.StreamManager()
    monkeypatch.setattr(manager, "_launch_viewer_start", lambda *a: None)
    manager.open_viewer(camera(), "sub")
    instance = manager.streams[(1374, "sub")]
    instance.status = "stopped"
    instance.start_cancelled.set()
    monkeypatch.setattr(instance, "_audit_live_event", lambda *a, **kw: None)
    manager.cleanup()
    assert not instance.start_cancelled.is_set()
    assert instance.status == "restarting"
    assert instance.next_retry_at is not None


@pytest.fixture
def pg_worker(worker, monkeypatch):
    url = os.getenv("KMVMS_STAGE2_POSTGRES_URL")
    if not url:
        pytest.skip("isolated PostgreSQL test service not configured")
    engine = create_engine(url)
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET LOCAL TIME ZONE 'Asia/Yekaterinburg'"))
        connection.execute(text("""CREATE TEMP TABLE recorder_runtime_status (
            recorder_instance_id TEXT PRIMARY KEY, service_status TEXT, loop_state TEXT,
            started_at TIMESTAMP, heartbeat_at TIMESTAMP, active_jobs_count INTEGER,
            recording_cameras_count INTEGER, failed_cameras_count INTEGER,
            last_error TEXT, last_exit_code INTEGER, updated_at TIMESTAMP) ON COMMIT DROP"""))

        @contextlib.contextmanager
        def begin():
            yield connection

        monkeypatch.setattr(worker, "engine", types.SimpleNamespace(begin=begin))
        try:
            yield worker, connection
        finally:
            transaction.rollback()
    engine.dispose()


def test_heartbeat_is_utc_even_with_non_utc_postgres_session(pg_worker):
    worker, connection = pg_worker
    worker.write_recorder_heartbeat()
    row = connection.execute(text("SELECT started_at, heartbeat_at FROM recorder_runtime_status")).one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((row.heartbeat_at - now).total_seconds()) < 5
    assert abs((row.started_at - now).total_seconds()) < 5


def test_reconcile_stops_only_owned_noncurrent_jobs_without_deleting_history(pg_worker):
    worker, connection = pg_worker
    connection.execute(text("""CREATE TEMP TABLE recording_jobs (
        id TEXT, recorder_instance_id TEXT, ownership TEXT, source TEXT,
        state TEXT, stop_reason TEXT, ffmpeg_pid INTEGER, updated_at TIMESTAMP) ON COMMIT DROP"""))
    for job_id, instance, owner in [('old', 'old-instance', 'KM VMS'), ('current', worker.RECORDER_INSTANCE_ID, 'KM VMS'), ('foreign', 'foreign-instance', 'external')]:
        connection.execute(text("INSERT INTO recording_jobs VALUES (:id,:instance,:owner,'recorder','recording',NULL,123,NOW())"), {'id': job_id, 'instance': instance, 'owner': owner})
    worker.reconcile_previous_recorder_instances()
    rows = {r.id: r.state for r in connection.execute(text("SELECT id,state FROM recording_jobs"))}
    assert rows == {'old': 'stopped', 'current': 'recording', 'foreign': 'recording'}
