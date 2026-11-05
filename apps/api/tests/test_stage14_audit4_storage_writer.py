from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.models.storage_operation import StorageOperation
from app.services import archive_root_activation as activation
from recorder_core.storage_activation import recording_start_allowed
from test_self_recovery_stage1374 import worker, camera
from test_stage14_activation_pause import setup_pause
from test_storage_recording_contract import db


def test_unconfirmed_camera_does_not_need_fake_confirmation_to_fence_new_writers(db, monkeypatch):
    row, state = setup_pause(db)
    state['camera_snapshots'] = []
    assert recording_start_allowed(db.get_bind()) is False
    ids, snapshots = activation._pause_operation_cameras(db, state)
    assert ids == [] and snapshots == []
    db.refresh(row)
    assert row.enabled is True
    operation = db.get(StorageOperation, state['operation_id'])
    assert operation.progress['activation_writer_fenced'] is True
    # A failed recovery is not an implicit unlock, even after an API restart.
    operation.status = 'partial'
    db.commit()
    assert recording_start_allowed(db.get_bind()) is False
    monkeypatch.setattr(activation, 'create_event', lambda **kwargs: None)
    activation._restore_operation_cameras(db, state, reason='activation_completed')
    assert recording_start_allowed(db.get_bind()) is True
    db.refresh(row)
    assert row.enabled is True


@pytest.mark.parametrize('reason', ['activation_completed', 'rollback_completed', 'activation_failed_before_apply'])
def test_verified_completion_releases_only_owned_pause_and_writer_fence(db, monkeypatch, reason):
    row, state = setup_pause(db)
    monkeypatch.setattr(activation, 'create_event', lambda **kwargs: None)
    activation._pause_operation_cameras(db, state)
    activation.preserve_manual_camera_disable(db, row.id)
    row.enabled = False
    operation = db.get(StorageOperation, state['operation_id'])
    operation.status = 'partial'
    db.commit()
    assert recording_start_allowed(db.get_bind()) is False
    activation._restore_operation_cameras(db, state, reason=reason)
    assert recording_start_allowed(db.get_bind()) is True
    db.refresh(row)
    assert row.enabled is False


def test_root_admission_blocks_before_metadata_or_ffmpeg_and_resumes_without_backoff(worker, monkeypatch):
    row = camera()
    allowed = False
    monkeypatch.setattr(worker, 'recording_start_allowed', lambda _engine: allowed)
    metadata, command, launch = Mock(), Mock(), Mock()
    monkeypatch.setattr(worker, 'create_recording_job', metadata)
    monkeypatch.setattr(worker, 'ffmpeg_cmd', command)
    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    worker.start_camera(row)
    job = worker.jobs[row.id]
    assert job.waiting_for_storage_activation and job.retry_count == 0
    assert job.next_retry_at is None
    metadata.assert_not_called()
    command.assert_not_called()
    launch.assert_not_called()
    # The normal next attempt is admitted; no 5-minute error retry is introduced.
    allowed = True
    command.return_value = (None, None)
    worker.start_camera(row)
    assert not job.waiting_for_storage_activation
    metadata.assert_called_once()
    command.assert_called_once()


def test_unknown_admission_never_starts_writer(worker, monkeypatch):
    monkeypatch.setattr(worker, 'recording_start_allowed', Mock(side_effect=ConnectionError('unavailable')))
    metadata = Mock()
    monkeypatch.setattr(worker, 'create_recording_job', metadata)
    worker.start_camera(camera())
    metadata.assert_not_called()
    assert worker.jobs[camera().id].waiting_for_storage_activation


def test_unrelated_storage_work_does_not_block_recording(db):
    row, state = setup_pause(db)
    operation = db.get(StorageOperation, state['operation_id'])
    operation.operation_type = 'retention_run'
    operation.progress = {'activation_writer_fenced': True}
    db.commit()
    assert recording_start_allowed(db.get_bind()) is True
