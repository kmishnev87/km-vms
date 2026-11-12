"""A lost camera must not turn readable saved video into a permanent failed tail."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from recorder_core import segment_recovery as recovery
from test_self_recovery_stage1374 import Process, worker
from test_stage14_recorder_store_postgres import recording_context
from test_stage14_history_large_totals_postgres import history_database


@pytest.mark.parametrize("payload,code,reason", [
    ({"frames": [{"media_type": "video"}]}, 0, "probe_ok"),
    ({"frames": [{"media_type": "audio"}]}, 0, "segment_recovery_no_video"),
    ({"frames": []}, 0, "segment_recovery_no_video"),
    ({"frames": "video"}, 0, "segment_recovery_no_video"),
    ([], 0, "segment_recovery_no_video"),
    ({"frames": [{"media_type": "video"}]}, 1, "segment_recovery_probe_failed"),
])
def test_probe_requires_a_video_frame_and_success(tmp_path, monkeypatch, payload, code, reason):
    path = tmp_path / "closed.mkv"
    path.write_bytes(b"own video fixture")
    run = Mock(return_value=SimpleNamespace(returncode=code, stdout=json.dumps(payload)))
    monkeypatch.setattr(recovery.subprocess, "run", run)
    verified, actual = recovery.probe_interrupted_segment(path, timeout=0.5)
    assert actual == reason
    assert (verified is not None) == (reason == "probe_ok")
    assert run.call_args.kwargs["timeout"] == 0.5
    assert "%+#32" in run.call_args.args[0]
    assert path.read_bytes() == b"own video fixture"


@pytest.mark.parametrize("error,reason", [
    (subprocess.TimeoutExpired("ffprobe", 0.5), "segment_recovery_probe_timeout"),
    (FileNotFoundError("ffprobe"), "segment_recovery_probe_unavailable"),
])
def test_probe_failure_is_not_video_corruption(tmp_path, monkeypatch, error, reason):
    path = tmp_path / "closed.mkv"
    path.write_bytes(b"own fixture")
    monkeypatch.setattr(recovery.subprocess, "run", Mock(side_effect=error))
    assert recovery.probe_interrupted_segment(path) == (None, reason)
    assert path.read_bytes() == b"own fixture"


@pytest.mark.parametrize("kind", ["empty", "directory", "symlink", "missing"])
def test_invalid_files_are_never_probed(tmp_path, monkeypatch, kind):
    path = tmp_path / "closed.mkv"
    if kind == "empty":
        path.touch()
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "other.mkv"
        target.write_bytes(b"other video")
        path.symlink_to(target)
    run = Mock()
    monkeypatch.setattr(recovery.subprocess, "run", run)
    assert recovery.probe_interrupted_segment(path)[0] is None
    run.assert_not_called()


def test_file_changed_during_probe_is_not_finalized(tmp_path, monkeypatch):
    path = tmp_path / "closed.mkv"
    path.write_bytes(b"first")
    def changed(*args, **kwargs):
        path.write_bytes(b"changed video")
        return SimpleNamespace(returncode=0, stdout='{"frames":[{"media_type":"video"}]}')
    monkeypatch.setattr(recovery.subprocess, "run", changed)
    assert recovery.probe_interrupted_segment(path) == (None, "segment_recovery_file_changed")
    assert path.read_bytes() == b"changed video"


def test_malformed_probe_output_remains_unconfirmed(tmp_path, monkeypatch):
    path = tmp_path / "closed.mkv"
    path.write_bytes(b"own fixture")
    monkeypatch.setattr(recovery.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stdout="bad json")))
    assert recovery.probe_interrupted_segment(path)[0] is None


def make_media(path):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x90:rate=5", "-t", "1", "-c:v", "mpeg4",
                    "-threads", "1", "-an", "-y", str(path)],
                   check=True, capture_output=True, timeout=15)


@pytest.mark.parametrize("extension", ["mkv", "mp4"])
def test_actual_saved_video_is_readable_without_modification(tmp_path, extension):
    path = tmp_path / f"closed.{extension}"
    make_media(path)
    before = path.read_bytes()
    verified, reason = recovery.probe_interrupted_segment(path)
    assert verified is not None and reason == "probe_ok"
    assert path.read_bytes() == before


def configured_job(worker, monkeypatch, tmp_path, engine):
    row, context, store = recording_context(engine, tmp_path)
    job = worker.RecordingJob(row.id, "Camera", "camera", "always", True)
    job.archive_root_id, job.archive_root_path = context.archive_root_id, tmp_path
    store.create_recording_job(row, job)
    job.current_output_path = str(tmp_path / "Camera-%Y-%m-%d-%H-%M-%S.mkv")
    job.process = Process(1)
    job.state = worker.RecorderState.RECORDING
    worker.jobs[job.camera_id] = job
    monkeypatch.setattr(worker, "engine", engine)
    return job


@pytest.mark.parametrize("kind", ["readable", "empty", "corrupted"])
def test_real_exit_updates_segment_and_receipt_and_still_retries(worker, monkeypatch, tmp_path, history_database, kind):
    job = configured_job(worker, monkeypatch, tmp_path, history_database)
    path = tmp_path / "Camera-2026-11-12-10-00-00.mkv"
    if kind == "readable":
        make_media(path)
    else:
        path.write_bytes(b"" if kind == "empty" else b"not a video container")
    before = path.read_bytes()
    other = worker.RecordingJob(job.camera_id + 1000, "Other", "other", "always", True, process=Process())
    worker.jobs[other.camera_id] = other
    worker.check_children()
    with history_database.connect() as conn:
        segment = conn.execute(text("SELECT * FROM recording_segments")).mappings().one()
        receipt = conn.execute(text("SELECT * FROM recorder_file_receipts")).mappings().one()
        assert segment["status"] == ("finalized" if kind == "readable" else "failed")
        assert receipt["state"] == ("finalized" if kind == "readable" else "writing")
        if kind == "readable":
            assert segment["content_probe_status"] == "probe_ok"
            assert segment["size_bytes"] == receipt["size_bytes"] == len(before)
    assert path.read_bytes() == before
    assert job.process is None and job.next_retry_at is not None
    assert job.state == worker.RecorderState.RESTARTING
    assert other.process.poll() is None
    first_retries = job.retry_count
    worker.check_children()
    assert job.retry_count == first_retries, "the same exited process cannot be handled twice"


@pytest.mark.parametrize("race", ["changed_file", "receipt_mismatch"])
def test_recovery_preserves_evidence_guards(worker, monkeypatch, tmp_path, history_database, race):
    job = configured_job(worker, monkeypatch, tmp_path, history_database)
    path = tmp_path / "Camera-2026-11-12-10-00-00.mkv"
    make_media(path)
    worker.create_segment_metadata_if_needed(job, path)
    if race == "receipt_mismatch":
        with history_database.begin() as conn:
            conn.execute(text("UPDATE recorder_file_receipts SET inode = 'not-the-original-inode'"))
    else:
        before = path.lstat()
        path.write_bytes(path.read_bytes() + b"changed-after-probe")
        monkeypatch.setattr(worker, "probe_interrupted_segment", lambda *a, **kw: (before, "probe_ok"))
    expected = path.read_bytes()
    worker.check_children()
    with history_database.connect() as conn:
        assert conn.execute(text("SELECT status FROM recording_segments")).scalar_one() == "failed"
        assert conn.execute(text("SELECT state FROM recorder_file_receipts")).scalar_one() == "writing"
    assert path.read_bytes() == expected
    assert job.process is None and job.next_retry_at is not None


def test_live_writer_and_previous_files_are_not_recovered(worker, monkeypatch, tmp_path):
    job = worker.RecordingJob(1, "Camera", "camera", "always", True, db_job_id="current")
    job.archive_root_path = tmp_path
    job.current_output_path = str(tmp_path / "Camera-%Y-%m-%d-%H-%M-%S.mkv")
    path = tmp_path / "Camera-2026-11-12-10-00-00.mkv"
    path.write_bytes(b"other writer")
    job.segment_baseline_paths.add(path.name)
    create = Mock()
    probe = Mock()
    monkeypatch.setattr(worker, "create_segment_metadata_if_needed", create)
    monkeypatch.setattr(worker, "probe_interrupted_segment", probe)
    worker.recover_interrupted_segments(job)
    create.assert_not_called()
    job.segment_baseline_paths.clear()
    job.process = Process()
    worker.recover_interrupted_segments(job)
    create.assert_not_called()
    probe.assert_not_called()


def test_recovery_has_a_total_time_budget(worker, monkeypatch, tmp_path):
    job = worker.RecordingJob(1, "Camera", "camera", "always", True, db_job_id="current")
    job.archive_root_path = tmp_path
    paths = [tmp_path / f"fragment-{i}.mkv" for i in range(10)]
    monkeypatch.setattr(worker, "discover_job_segments", lambda _: paths)
    monkeypatch.setattr(worker, "create_segment_metadata_if_needed", lambda j, p: j.known_segment_paths.add(p.name))
    clock = [0.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    def expired(path, *, timeout):
        assert 0 < timeout <= 3
        clock[0] += timeout
        return None, "segment_recovery_probe_timeout"
    probe = Mock(side_effect=expired)
    monkeypatch.setattr(worker, "probe_interrupted_segment", probe)
    worker.recover_interrupted_segments(job)
    assert probe.call_count == 2 and clock[0] == 6


def test_catalog_failure_does_not_prevent_retry(worker, monkeypatch):
    job = worker.RecordingJob(1, "Camera", "camera", "always", True, process=Process(1))
    worker.jobs[job.camera_id] = job
    monkeypatch.setattr(worker, "recover_interrupted_segments", Mock(side_effect=RuntimeError("catalog unavailable")))
    worker.check_children()
    assert job.process is None and job.next_retry_at is not None


def test_real_interrupted_ffmpeg_tail_is_preserved(worker, monkeypatch, tmp_path, history_database):
    job = configured_job(worker, monkeypatch, tmp_path, history_database)
    path = tmp_path / "Camera-2026-11-12-10-00-00.mkv"
    # A local synthetic source; no physical camera, network traffic or user archive.
    proc = subprocess.Popen([
        "ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
        "testsrc=size=160x90:rate=5", "-t", "5", "-c:v", "mpeg4",
        "-threads", "1", "-an", "-stats_period", "0.2", "-progress", "pipe:1",
        "-y", str(path),
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            if line.startswith("frame=") and int(line.split("=", 1)[1]) >= 4:
                break
        assert proc.poll() is None, "the fixture must interrupt a still-active writer"
        proc.terminate()
        proc.wait(timeout=5)
        assert proc.returncode != 0
        job.process = proc
        before = path.read_bytes()
        assert before
        worker.check_children()
        with history_database.connect() as conn:
            assert conn.execute(text("SELECT status FROM recording_segments")).scalar_one() == "finalized"
            assert conn.execute(text("SELECT state FROM recorder_file_receipts")).scalar_one() == "finalized"
        assert path.read_bytes() == before
        assert job.process is None and job.next_retry_at is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()


def test_forced_stop_validates_instead_of_blind_finalization(worker, monkeypatch):
    class HungProcess(Process):
        def terminate(self):
            pass
        def wait(self, timeout=None):
            if self.code is None:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return self.code
        def kill(self):
            self.code = -9
    job = worker.RecordingJob(1, "Camera", "camera", "always", True, process=HungProcess())
    worker.jobs[job.camera_id] = job
    recover, fail, blind = Mock(), Mock(), Mock()
    monkeypatch.setattr(worker, "recover_interrupted_segments", recover)
    monkeypatch.setattr(worker, "mark_segments_failed_for_job", fail)
    monkeypatch.setattr(worker, "finalize_segments_for_job", blind)
    monkeypatch.setattr(worker, "close_recording_job", Mock())
    worker.stop_camera(job.camera_id, "media_stalled")
    recover.assert_called_once_with(job)
    fail.assert_called_once()
    blind.assert_not_called()
    assert job.process is None
