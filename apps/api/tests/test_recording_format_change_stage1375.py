"""Format selection and real recorder definitions, without physical cameras."""
import contextlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.recording import RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.routers.settings import SettingsUpdateRequest, patch_settings
from app.services import recording_format_change as change
from app.services.schema_migrations import _stage1375_format_apply, STAGE1375_FORMAT_CHANGE_MIGRATION
from app.services.system_settings import get_system_settings
from test_storage_recording_contract import db, actor, FakeRequest
from test_self_recovery_stage1374 import worker, Process


def settings_row(db, *, requested=True, mode="after_segment"):
    row = get_system_settings(db)
    row.recording_format = "mp4"
    row.recording_format_apply_mode = mode
    row.recording_format_requested_at = datetime.utcnow() - timedelta(seconds=10) if requested else None
    db.commit()
    return row


def active_job(db, *, job_id="active-format", camera_id=1, state="recording"):
    row = RecordingJob(id=job_id, camera_id=camera_id, state=state, started_at=datetime.utcnow(), recorder_instance_id="format-test")
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize("mode", ["after_segment", "immediate"])
def test_explicit_choice_persists_in_existing_settings(db, monkeypatch, mode):
    row = get_system_settings(db)
    row.recording_format = "mkv"
    db.commit()
    active_job(db)
    monkeypatch.setattr(change, "list_camera_recording_states", lambda db: [])
    response = patch_settings(SettingsUpdateRequest(recording_format="mp4", recording_format_apply_mode=mode, expected_recording_format="mkv"), FakeRequest(), db=db, current_user=actor())
    db.expire_all()
    saved = db.get(SystemSettings, row.id)
    assert saved.recording_format == "mp4"
    assert saved.recording_format_apply_mode == mode
    assert saved.recording_format_requested_at is not None
    assert response["recording_format_change"]["state"] == "applied"


def test_choice_rejection_does_not_save_other_settings(db):
    row = get_system_settings(db)
    original_zone = row.timezone
    active_job(db)
    with pytest.raises(HTTPException) as error:
        patch_settings(SettingsUpdateRequest(recording_format="mp4", timezone="Etc/GMT-3"), FakeRequest(), db=db, current_user=actor())
    assert error.value.status_code == 409
    assert error.value.detail["error"] == "recording_format_change_requires_choice"
    assert row.timezone == original_zone and row.recording_format == "mkv"
    assert row.recording_format_requested_at is None


def test_without_active_jobs_uses_safe_default(db, monkeypatch):
    monkeypatch.setattr(change, "list_camera_recording_states", lambda db: [])
    response = patch_settings(SettingsUpdateRequest(recording_format="mp4"), FakeRequest(), db=db, current_user=actor())
    assert response["recording_format"] == "mp4"
    assert response["recording_format_apply_mode"] == "after_segment"


def test_stale_choice_cannot_overwrite_newer_format(db):
    row = settings_row(db)
    with pytest.raises(HTTPException) as error:
        change.prepare_format_change(db, row, {"recording_format": "mkv"}, apply_mode="immediate", expected_format="mkv")
    assert error.value.detail["error"] == "recording_format_settings_changed"
    assert row.recording_format == "mp4" and row.recording_format_apply_mode == "after_segment"


def test_repeated_identical_choice_does_not_move_boundary_clock(db):
    row = settings_row(db)
    original_time = row.recording_format_requested_at
    assert change.prepare_format_change(db, row, {"recording_format": "mp4"}, apply_mode="after_segment", expected_format="mp4") is False
    assert row.recording_format_requested_at == original_time
    assert change.prepare_format_change(db, row, {"recording_format": "mp4"}, apply_mode="immediate", expected_format="mp4") is True
    assert row.recording_format_requested_at > original_time


@pytest.mark.parametrize("data,mode,expected", [({}, "immediate", "mkv"), ({"recording_format": "mkv"}, "invalid", "mp4"), ({"recording_format": "mkv"}, "immediate", None)])
def test_invalid_command_rejected_without_mutation(db, data, mode, expected):
    row = settings_row(db)
    with pytest.raises(HTTPException) as error:
        change.prepare_format_change(db, row, data, apply_mode=mode, expected_format=expected)
    assert error.value.status_code == 422
    assert row.recording_format == "mp4"


def test_request_schema_rejects_unknown_modes():
    with pytest.raises(ValidationError):
        SettingsUpdateRequest(recording_format_apply_mode="force-kill")


def runtime_row(camera_id=1, *, actual="mkv", confirmed=True, **extra):
    return {"camera_id": camera_id, "camera_name": "Camera " + str(camera_id), "enabled": True, "recording_mode": "always", "job_id": None, "job_state": "recording", "recording_container_format": actual, "confirmed_recording": confirmed, "heartbeat_current": True, "current_failure": False, "stale_current_segment": False, **extra}


def test_idle_does_not_read_runtime(db, monkeypatch):
    row = settings_row(db, requested=False)
    spy = Mock()
    monkeypatch.setattr(change, "list_camera_recording_states", spy)
    assert change.format_change_status(db, row)["state"] == "idle"
    spy.assert_not_called()


@pytest.mark.parametrize("mode,state", [("after_segment", "waiting_segment"), ("immediate", "switching")])
def test_mixed_cameras_have_per_camera_status(db, monkeypatch, mode, state):
    row = settings_row(db, mode=mode)
    monkeypatch.setattr(change, "list_camera_recording_states", lambda db: [runtime_row(1), runtime_row(2, actual="mp4")])
    status = change.format_change_status(db, row)
    assert status["state"] == "pending"
    assert status["pending_count"] == status["applied_count"] == 1
    assert status["items"][0]["state"] == state
    assert status["items"][1]["state"] == "applied"


def test_unconfirmed_new_file_is_not_success(db, monkeypatch):
    row = settings_row(db)
    monkeypatch.setattr(change, "list_camera_recording_states", lambda db: [runtime_row(actual="mp4", confirmed=False)])
    assert change.format_change_status(db, row)["state"] == "pending"


def test_camera_failure_is_named_and_safe(db, monkeypatch):
    row = settings_row(db)
    job = active_job(db, state="error")
    job.last_error_type = "auth_failed"
    job.last_error = "rtsp://private:secret@device/stream"
    db.commit()
    monkeypatch.setattr(change, "list_camera_recording_states", lambda db: [runtime_row(job_id=job.id, current_failure=True)])
    result = change.format_change_status(db, row)
    assert result["state"] == "failed"
    assert result["items"][0]["reason_code"] == "auth_failed"
    assert "secret" not in str(result) and "rtsp://" not in str(result)


def test_unfinalized_previous_file_prevents_false_success_and_repair_clears_it(db, monkeypatch):
    row = settings_row(db)
    job = active_job(db, state="stopped")
    job.stop_reason = "recording_format_finalization_failed"
    job.stopped_at = datetime.utcnow()
    segment = RecordingSegment(job_id=job.id, camera_id=1, file_path="/not-read.mkv", started_at=datetime.utcnow(), status="failed")
    db.add(segment)
    db.commit()
    monkeypatch.setattr(change, "list_camera_recording_states", lambda db: [runtime_row(actual="mp4")])
    assert change.format_change_status(db, row)["items"][0]["reason_code"] == "previous_segment_not_finalized"
    segment.status = "finalized"
    db.commit()
    assert change.format_change_status(db, row)["state"] == "applied"


def test_disabled_and_idle_cameras_do_not_block(db, monkeypatch):
    row = settings_row(db)
    monkeypatch.setattr(change, "list_camera_recording_states", lambda db: [runtime_row(enabled=False), runtime_row(2, recording_mode="off")])
    assert change.format_change_status(db, row)["state"] == "applied"


def test_low_disk_pause_is_not_reported_as_applied(db, monkeypatch):
    row = settings_row(db)
    row.recording_suspended_by_low_disk = True
    monkeypatch.setattr(change, "list_camera_recording_states", lambda db: [runtime_row(confirmed=False)])
    result = change.format_change_status(db, row)
    assert result["state"] == "pending"
    assert result["items"][0]["reason_code"] == "low_disk_suspended"


def test_additive_migration_is_idempotent_and_preserves_existing_setting():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE system_settings (id INTEGER PRIMARY KEY, recording_format VARCHAR(16))"))
        connection.execute(text("INSERT INTO system_settings VALUES (1, 'mp4')"))
    with Session(engine) as session:
        assert len(_stage1375_format_apply(session)["added_columns"]) == 2
        assert _stage1375_format_apply(session)["added_columns"] == []
        assert session.execute(text("SELECT recording_format FROM system_settings")).scalar_one() == "mp4"
        columns = {item["name"]: item for item in inspect(session.connection()).get_columns("system_settings")}
        assert columns["recording_format_apply_mode"]["nullable"] is True
        assert columns["recording_format_requested_at"]["nullable"] is True
    assert STAGE1375_FORMAT_CHANGE_MIGRATION.from_version == 9
    assert STAGE1375_FORMAT_CHANGE_MIGRATION.to_version == 10


def make_job(worker):
    job = worker.RecordingJob(1, "Format camera", "format", "always", True)
    job.process = Process()
    job.state = worker.RecorderState.RECORDING
    worker.jobs[1] = job
    return job


def test_immediate_does_not_wait_for_a_file(worker):
    assert worker.format_change_ready(make_job(worker), "mp4", "immediate", None)


def test_deferred_waits_for_boundary_and_does_not_mutate_actual_format(worker, monkeypatch):
    job = make_job(worker)
    first = Path("camera_1_2026-11-02-12-00-00.mkv")
    current = [first]
    monkeypatch.setattr(worker, "current_writing_segment", lambda job: current[0])
    requested = datetime(2026, 11, 2, 12, 0, 10).timestamp()
    assert not worker.format_change_ready(job, "mp4", "after_segment", requested)
    assert not worker.format_change_ready(job, "mp4", "after_segment", requested)
    assert job.recording_format == "mkv" and not job.process.terminated
    current[0] = Path("camera_1_2026-11-02-12-01-00.mkv")
    assert worker.format_change_ready(job, "mp4", "after_segment", requested)


def test_boundary_between_request_and_first_poll_is_not_missed(worker, monkeypatch):
    job = make_job(worker)
    monkeypatch.setattr(worker, "current_writing_segment", lambda job: Path("camera_1_2026-11-02-12-01-00.mkv"))
    assert worker.format_change_ready(job, "mp4", "after_segment", datetime(2026, 11, 2, 12, 0, 59).timestamp())


def test_new_request_resets_old_boundary_anchor(worker, monkeypatch):
    job = make_job(worker)
    current = [Path("camera_1_2026-11-02-12-00-00.mkv")]
    monkeypatch.setattr(worker, "current_writing_segment", lambda job: current[0])
    assert not worker.format_change_ready(job, "mp4", "after_segment", datetime(2026, 11, 2, 12, 0, 10).timestamp())
    current[0] = Path("camera_1_2026-11-02-12-01-00.mkv")
    assert not worker.format_change_ready(job, "mp4", "after_segment", datetime(2026, 11, 2, 12, 1, 10).timestamp())


def test_no_output_yet_waits_in_deferred_mode(worker, monkeypatch):
    monkeypatch.setattr(worker, "current_writing_segment", lambda job: None)
    assert not worker.format_change_ready(make_job(worker), "mp4", "after_segment", None)


class HungProcess(Process):
    def __init__(self):
        super().__init__()
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.code = 0

    def wait(self, timeout=None):
        if self.code is None:
            import subprocess
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.code


def test_format_change_never_force_kills_unfinished_writer(worker, monkeypatch):
    job = make_job(worker)
    job.process = HungProcess()
    process = job.process
    updates = []
    monkeypatch.setattr(worker, "update_recording_job", lambda *args, **kw: updates.append(kw))
    assert worker.stop_camera(1, "recording_format_change", graceful_format_change=True) is False
    assert process.terminated and not process.killed and job.process is process
    assert job.state == worker.RecorderState.RECORDING
    assert updates[-1]["stop_reason"] == "recording_format_finish_timeout"


def test_db_read_failure_does_not_fallback_and_restart_current_recording(worker, monkeypatch):
    job = make_job(worker)
    monkeypatch.setattr(worker, "read_recording_format_choice", lambda: None)
    fake_session = types.SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(worker, "SessionLocal", lambda: fake_session)
    worker.sync_cameras()
    assert not job.process.terminated


@pytest.mark.parametrize("unfinished,expected", [(0, True), (1, False)])
def test_graceful_completion_reports_finalization_result(worker, monkeypatch, unfinished, expected):
    job = make_job(worker)
    job.db_job_id = "format-job"
    closed = []
    monkeypatch.setattr(worker, "finalize_segments_for_job", lambda job: None)
    monkeypatch.setattr(worker, "close_recording_job", lambda job, **kw: closed.append(kw))
    connection = types.SimpleNamespace(execute=lambda *args: types.SimpleNamespace(scalar_one=lambda: unfinished))
    worker.engine = types.SimpleNamespace(begin=lambda: contextlib.nullcontext(connection))
    assert worker.stop_camera(1, "recording_format_change", graceful_format_change=True) is expected
    assert job.process is None
    assert closed[-1]["reason"] == ("recording_format_change" if expected else "recording_format_finalization_failed")


def test_sync_changes_only_the_camera_whose_file_boundary_has_arrived(worker, monkeypatch):
    rows = [types.SimpleNamespace(id=cid, name="Camera " + str(cid), storage_folder_name="cam" + str(cid), enabled=True,
        protocol="rtsp", rtsp_main_url="rtsp://example.invalid/main", rtsp_sub_url=None, rtsp_transport="tcp",
        recording_mode="always", default_record_stream="main", segment_minutes=1) for cid in (1, 2)]
    session = types.SimpleNamespace(execute=lambda *a: types.SimpleNamespace(fetchall=lambda: rows), close=lambda: None)
    monkeypatch.setattr(worker, "SessionLocal", lambda: session)
    request_epoch = datetime(2026, 11, 2, 12, 0, 30).timestamp()
    monkeypatch.setattr(worker, "read_recording_format_choice", lambda: ("mp4", "after_segment", request_epoch))
    monkeypatch.setattr(worker, "read_recording_suspended_by_low_disk", lambda: False)
    monkeypatch.setattr(worker, "retention_ready", lambda *a: True)
    monkeypatch.setattr(worker, "sync_segment_metadata_for_job", lambda *a: None)
    monkeypatch.setattr(worker, "handle_stale_current_segment", lambda *a: False)
    paths = {1: Path("camera_1_2026-11-02-12-01-00.mkv"), 2: Path("camera_2_2026-11-02-12-00-00.mkv")}
    monkeypatch.setattr(worker, "current_writing_segment", lambda job: paths[job.camera_id])
    for row in rows:
        job = worker.RecordingJob(row.id, row.name, row.storage_folder_name, "always", True)
        job.process = Process()
        job.recording_format = "mkv"
        job.state = worker.RecorderState.RECORDING
        job.config_signature = worker.camera_signature(row, "mkv")
        worker.jobs[row.id] = job
    switched = []
    def finish(camera_id, *args, **kwargs):
        job = worker.jobs[camera_id]
        assert job.recording_format == "mkv", "actual process format is unchanged until its files have finished"
        assert kwargs["graceful_format_change"] is True
        job.process = None
        job.state = worker.RecorderState.STOPPED
        return True
    def start(row):
        assert worker.jobs[row.id].recording_format == "mp4"
        switched.append(row.id)
    monkeypatch.setattr(worker, "stop_camera", finish)
    monkeypatch.setattr(worker, "start_camera", start)
    worker.sync_cameras()
    assert switched == [1]
    assert worker.jobs[2].recording_format == "mkv" and not worker.jobs[2].process.terminated


def test_failed_completion_history_is_not_overwritten_when_starting_next_job(worker, monkeypatch):
    job = make_job(worker)
    job.process = None
    job.db_job_id = "already-closed"
    job.stopped_at = worker.time.time()
    job.state = worker.RecorderState.STOPPED
    close = Mock()
    monkeypatch.setattr(worker, "close_recording_job", close)
    class StopHere(BaseException):
        pass
    def stop_before_launch(*args):
        raise StopHere()
    monkeypatch.setattr(worker, "create_recording_job", stop_before_launch)
    with pytest.raises(StopHere):
        worker.start_camera(types.SimpleNamespace(id=1, name=job.camera_name, storage_folder_name=job.folder_name, recording_mode="always", enabled=True))
    close.assert_not_called()
