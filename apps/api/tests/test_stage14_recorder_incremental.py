from unittest.mock import Mock
import pytest

from test_self_recovery_stage1374 import worker


def test_finished_files_are_not_refinalized_and_retention_releases_names(worker, monkeypatch, tmp_path):
    job = worker.RecordingJob(1, "Camera", "camera", "always", True)
    job.db_job_id = "stage14"
    job.archive_root_path = tmp_path
    directory = tmp_path / "segments"
    directory.mkdir()
    job.current_output_path = str(directory / "Camera-%Y-%m-%d-%H-%M-%S.mkv")
    for minute in range(5):
        (directory / f"Camera-2026-11-04-12-{minute:02}-00.mkv").write_bytes(b"frame")
    created = Mock(side_effect=lambda job, path: job.known_segment_paths.add(worker.storage_relative_path(path, root=tmp_path)))
    finalized = Mock(side_effect=lambda job, path: worker.remember_processed_segment(job, path))
    monkeypatch.setattr(worker, "create_segment_metadata_if_needed", created)
    monkeypatch.setattr(worker, "finalize_segment_path", finalized)
    monkeypatch.setattr(worker, "update_writing_segment_progress", Mock())
    worker.sync_segment_metadata_for_job(job)
    assert finalized.call_count == 4
    for _ in range(20):
        worker.sync_segment_metadata_for_job(job)
    assert finalized.call_count == 4
    assert len(job.segment_discovery_cache) == 1
    assert len(job.known_segment_paths) == 1
    (directory / "Camera-2026-11-04-12-05-00.mkv").write_bytes(b"frame")
    worker.sync_segment_metadata_for_job(job)
    assert finalized.call_count == 5
    (directory / "Camera-2026-11-04-12-00-00.mkv").unlink()
    worker.sync_segment_metadata_for_job(job)
    assert "Camera-2026-11-04-12-00-00.mkv" not in job.processed_segment_names


def test_metadata_work_per_poll_is_bounded_but_latest_file_is_included(worker, monkeypatch, tmp_path):
    job = worker.RecordingJob(1, "Camera", "camera", "always", True)
    job.db_job_id = "stage14"
    job.archive_root_path = tmp_path
    job.current_output_path = str(tmp_path / "Camera-%Y-%m-%d-%H-%M-%S.mkv")
    for second in range(200):
        (tmp_path / f"Camera-2026-11-04-12-{second // 60:02}-{second % 60:02}.mkv").write_bytes(b"frame")
    created = Mock()
    progress = Mock()
    monkeypatch.setattr(worker, "create_segment_metadata_if_needed", created)
    monkeypatch.setattr(worker, "finalize_segment_path", Mock())
    monkeypatch.setattr(worker, "update_writing_segment_progress", progress)
    worker.sync_segment_metadata_for_job(job)
    assert created.call_count == worker.SEGMENT_METADATA_BATCH_SIZE
    assert progress.call_args.args[1].name == "Camera-2026-11-04-12-03-19.mkv"


@pytest.mark.parametrize("renamed", ["New-Name", "Camera__deleted_1_123"])
def test_writer_keeps_original_prefix_after_rename_and_discovery_refresh(worker, monkeypatch, tmp_path, renamed):
    job = worker.RecordingJob(1, "Original", "camera", "always", True)
    job.db_job_id = "original-writer"
    job.archive_root_path = tmp_path
    job.current_output_path = str(tmp_path / "Original-%Y-%m-%d-%H-%M-%S.mkv")
    original = tmp_path / "Original-2026-11-05-12-00-00.mkv"
    original.write_bytes(b"original-tail")
    foreign = tmp_path / "Other-2026-11-05-12-00-00.mkv"
    foreign.write_bytes(b"foreign")
    assert worker.discover_job_segments(job) == [original]
    job.camera_name = renamed
    job.segment_directory_checked_at = 0
    assert worker.discover_job_segments(job) == [original]
    rotated = tmp_path / "Original-2026-11-05-12-05-00.mkv"
    rotated.write_bytes(b"rotated-tail")
    assert worker.discover_job_segments(job) == [original, rotated]
    finalized = []
    monkeypatch.setattr(worker, "create_segment_metadata_if_needed", Mock())
    monkeypatch.setattr(worker, "update_writing_segment_progress", Mock())
    def finish(job, path):
        finalized.append(path)
        worker.remember_processed_segment(job, path)
    monkeypatch.setattr(worker, "finalize_segment_path", finish)
    worker.sync_segment_metadata_for_job(job)
    assert finalized == [original]
    worker.sync_segment_metadata_for_job(job)
    assert finalized == [original]
    assert original.read_bytes() == b"original-tail" and foreign.read_bytes() == b"foreign"
    new_file = tmp_path / f"{worker.safe_name(renamed)}-2026-11-05-12-10-00.mkv"
    new_file.write_bytes(b"new-writer")
    job.current_output_path = str(tmp_path / f"{worker.safe_name(renamed)}-%Y-%m-%d-%H-%M-%S.mkv")
    assert worker.discover_job_segments(job) == [new_file]
