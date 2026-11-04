import contextlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_self_recovery_stage1374 import worker, Process, camera


def test_job_id_is_published_only_after_commit_and_retry_reuses_pending_id(worker, monkeypatch):
    row = camera()
    job = worker.RecordingJob(row.id, row.name, row.storage_folder_name, "always", True)
    writes = []
    attempts = 0

    @contextlib.contextmanager
    def begin():
        nonlocal attempts
        attempts += 1
        yield SimpleNamespace(execute=lambda sql, params: writes.append(params["id"]))
        if attempts == 1:
            raise ConnectionError("commit reply lost")

    monkeypatch.setattr(worker, "engine", SimpleNamespace(begin=begin))
    with pytest.raises(ConnectionError):
        worker.create_recording_job(row, job)
    assert job.db_job_id is None
    pending = job.pending_db_job_id
    assert pending == writes[0]
    assert worker.create_recording_job(row, job) == pending
    assert writes == [pending, pending]
    assert job.pending_db_job_id is None


def test_metadata_failure_prevents_process_start_and_schedules_recovery(worker, monkeypatch):
    row = camera()
    command = Mock()
    launch = Mock()
    monkeypatch.setattr(worker, "create_recording_job", Mock(side_effect=ConnectionError("DB unavailable")))
    monkeypatch.setattr(worker, "ffmpeg_cmd", command)
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    worker.start_camera(row)
    job = worker.jobs[row.id]
    assert job.db_job_id is None
    assert job.state == worker.RecorderState.RESTARTING
    assert job.next_retry_at > worker.time.time()
    command.assert_not_called()
    launch.assert_not_called()


def test_pending_id_is_discarded_after_explicit_close(worker):
    job = worker.RecordingJob(1, "Camera", "camera", "always", True)
    job.pending_db_job_id = "unconfirmed"
    worker.close_recording_job(job, state="stopped", reason="disabled")
    assert job.pending_db_job_id is None and job.db_job_id is None


def test_segment_cache_is_not_published_before_segment_and_receipt_commit(worker, monkeypatch, tmp_path):
    job = worker.RecordingJob(1, 'Camera', 'camera', 'always', True)
    job.db_job_id = 'job-one'
    job.archive_root_id = 'root-one'
    job.archive_root_path = tmp_path
    path = tmp_path / 'Camera-2026-11-04-12-00-00.mkv'
    path.write_bytes(b'video')
    statements = []

    def execute(sql, params):
        value = str(sql)
        statements.append(value)
        if 'SELECT id, status' in value:
            return SimpleNamespace(first=lambda: None)
        if 'INSERT INTO recording_segments' in value:
            return SimpleNamespace(first=lambda: SimpleNamespace(id=12))
        if 'SELECT physical_identity' in value:
            return SimpleNamespace(scalar=lambda: 'physical-one')
        return SimpleNamespace(rowcount=1)

    @contextlib.contextmanager
    def begin():
        yield SimpleNamespace(execute=execute)
        raise ConnectionError('commit failed')

    monkeypatch.setattr(worker, 'engine', SimpleNamespace(begin=begin))
    with pytest.raises(ConnectionError):
        worker.create_segment_metadata_if_needed(job, path)
    assert any('INSERT INTO recorder_file_receipts' in value for value in statements)
    assert job.known_segment_paths == set()
    assert job.last_media_progress_at is None
