"""Execute Recorder SQL against owned PostgreSQL, not a permissive fake connection."""
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.recording import ArchiveRoot
from recorder_core.contracts import UNSET
from recorder_core.job_store import RecordingJobStore
from recorder_core.segment_accounting import SegmentAccounting
from recorder_core.storage_activation import recording_start_allowed
from app.models.storage_operation import StorageOperation
from test_stage14_history_large_totals_postgres import history_database

pytestmark = pytest.mark.skipif(
    not os.getenv('KMVMS_STAGE2_POSTGRES_URL'), reason='owned isolated PostgreSQL required'
)


def test_real_postgres_storage_admission_json_and_partial_recovery(history_database):
    with Session(history_database) as db:
        operation = StorageOperation(id='root-admission', operation_type='archive_root_activation',
            actor_kind='system', actor_key='system:root-admission', system_owner='root-admission',
            idempotency_key='root-admission', request_fingerprint='a' * 64,
            status='queued', scope={}, progress={})
        db.add(operation)
        db.commit()
        assert recording_start_allowed(history_database) is False
        operation.status = 'partial'
        operation.progress = {'activation_writer_fenced': True}
        db.commit()
        assert recording_start_allowed(history_database) is False
        operation.progress = {'activation_writer_fenced': False}
        db.commit()
        assert recording_start_allowed(history_database) is True


def recording_context(engine, tmp_path):
    camera = Camera(name='Recorder SQL proof', storage_folder_name='recorder-sql-proof',
                    protocol='rtsp', host='example.invalid', port=554, enabled=False)
    with Session(engine) as db:
        db.add(camera)
        db.add(ArchiveRoot(id='recorder-sql-root', label='Own test root', root_path=str(tmp_path),
                           physical_identity='pv1:' + '1' * 32, is_active=True))
        db.commit()
        db.refresh(camera)
        camera_id = camera.id
    row = SimpleNamespace(id=camera_id)
    job = SimpleNamespace(camera_id=camera_id, camera_name='Recorder SQL proof',
                          folder_name='recorder-sql-proof', db_job_id=None, pending_db_job_id=None,
                          source_stream=None, archive_root_id='recorder-sql-root',
                          archive_root_path=tmp_path, segment_baseline_paths=set(),
                          known_segment_paths=set(), last_media_progress_at=None,
                          stable_progress_since=None, current_media_size=0,
                          current_media_relative_path=None)
    store = RecordingJobStore(engine, 'recorder-sql-instance', lambda _: 'main',
                              lambda _: 'fingerprint-proof', str, str, lambda *a, **kw: None)
    return row, job, store


def test_real_postgres_job_create_update_clear_and_idempotent_retry(history_database, tmp_path):
    row, job, store = recording_context(history_database, tmp_path)
    identity = store.create_recording_job(row, job)
    assert store.create_recording_job(row, job) == identity
    assert job.pending_db_job_id is None
    with history_database.connect() as connection:
        saved = connection.execute(text('SELECT * FROM recording_jobs')).mappings().one()
        assert saved['id'] == identity
        assert saved['input_fingerprint'] == 'fingerprint-proof'
        assert saved['recorder_instance_id'] == 'recorder-sql-instance'
        assert saved['state'] == 'starting'
    store.update_recording_job(job, state='error', last_error='controlled failure',
                               last_error_type='probe', last_exit_code=3, ffmpeg_pid=123)
    store.clear_recording_job_error(job)
    store.update_recording_job(job, state='stopped', stop_reason='disabled', stopped=True,
                               last_error=UNSET)
    with history_database.connect() as connection:
        saved = connection.execute(text('SELECT * FROM recording_jobs')).mappings().one()
        assert saved['state'] == 'stopped' and saved['stop_reason'] == 'disabled'
        assert saved['stopped_at'] is not None
        assert saved['last_error'] is None and saved['last_error_type'] is None
        assert saved['last_exit_code'] is None and saved['ffmpeg_pid'] == 123


def test_real_postgres_segments_receipts_progress_and_retention_signal(history_database, tmp_path):
    row, job, store = recording_context(history_database, tmp_path)
    store.create_recording_job(row, job)
    processed = []
    accounting = SegmentAccounting(
        engine=history_database, default_record_stream='main', media_stall_seconds=60,
        storage_relative_path=lambda path, *, root: path.relative_to(root).as_posix(),
        parse_segment_start_time=lambda _: datetime.utcnow() - timedelta(seconds=2),
        path_format_metadata=lambda _: {'container_format': 'mkv', 'file_extension': '.mkv',
                                       'mime_type': 'video/x-matroska'},
        remember_processed_segment=lambda *args: processed.append(args),
        redact_text=str, truncate_error=str, classify_error=lambda _: 'controlled',
        log_event=lambda *a, **kw: None, write_audit_event=lambda **kw: None,
    )
    path = tmp_path / 'recording.mkv'
    path.write_bytes(b'owned test bytes')
    accounting.create_segment_metadata_if_needed(job, path)
    accounting.create_segment_metadata_if_needed(job, path)
    assert job.known_segment_paths == {'recording.mkv'}
    path.write_bytes(b'owned test bytes with progress')
    assert accounting.update_writing_segment_progress(job, path)
    assert accounting.finalize_segment_path(job, path)
    assert not accounting.finalize_segment_path(job, path)
    with history_database.connect() as connection:
        segment = connection.execute(text('SELECT * FROM recording_segments')).mappings().one()
        receipt = connection.execute(text('SELECT * FROM recorder_file_receipts')).mappings().one()
        signal = connection.execute(text('SELECT * FROM storage_work_signals')).mappings().one()
        assert segment['status'] == receipt['state'] == 'finalized'
        assert segment['size_bytes'] == receipt['size_bytes'] == path.stat().st_size
        assert receipt['segment_id'] == signal['requested_watermark'] == segment['id']
        assert signal['signal_type'] == 'retention_evaluate'
    failed = tmp_path / 'failed.mkv'
    failed.write_bytes(b'partial owned bytes')
    accounting.create_segment_metadata_if_needed(job, failed)
    accounting.mark_segment_failed(job, 'failed.mkv', 'controlled partial file')
    with history_database.connect() as connection:
        assert connection.execute(text("SELECT status FROM recording_segments WHERE relative_path='failed.mkv'")).scalar_one() == 'failed'


def test_real_postgres_failed_job_commit_does_not_publish_authority(history_database, tmp_path):
    row, job, store = recording_context(history_database, tmp_path)

    def reject_commit(connection):
        raise ConnectionError('controlled commit failure')

    event.listen(history_database, 'commit', reject_commit)
    try:
        with pytest.raises(ConnectionError, match='controlled commit failure'):
            store.create_recording_job(row, job)
    finally:
        event.remove(history_database, 'commit', reject_commit)
    pending = job.pending_db_job_id
    assert pending and job.db_job_id is None
    with history_database.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM recording_jobs')).scalar_one() == 0
    assert store.create_recording_job(row, job) == pending
    with history_database.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM recording_jobs')).scalar_one() == 1
