"""Real missing-index / hard-kill regression, isolated from customer archives."""
import json
import os
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from recorder_core import segment_recovery as recovery, segment_repair as repair
from recorder_core.segment_accounting import SegmentAccounting, receipt_object_identity
from test_recorder_interrupted_finalization import configured_job, make_media
from test_self_recovery_stage1374 import Process, worker
from test_stage14_history_large_totals_postgres import history_database


def broken_media(path):
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
        "-t", "4", "-c:v", "mpeg4", "-g", "10", "-threads", "1", "-an",
        "-f", "matroska", "pipe:1"], capture_output=True, check=True, timeout=15)
    # A streaming header has no final Duration/Cues, and the final block is cut.
    path.write_bytes(result.stdout[:-61])
    assert recovery.inspect_closed_segment(path)[0] is None


@pytest.mark.parametrize("duration", [None, "0", "-1", "nan", "inf", "garbage"])
def test_first_frame_without_duration_never_means_finalized(tmp_path, monkeypatch, duration):
    path = tmp_path / "closed.mkv"
    path.write_bytes(b"fixture")
    payload = {"frames": [{"media_type": "video"}], "format": {"duration": duration}}
    monkeypatch.setattr(recovery.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))))
    assert recovery.inspect_closed_segment(path)[1] == "segment_recovery_missing_duration"


def test_duration_without_cues_never_means_seekable(tmp_path, monkeypatch):
    path = tmp_path / "closed.mkv"
    path.write_bytes(b"not an indexed container")
    payload = {"frames": [{"media_type": "video"}], "format": {"duration": "10"}}
    monkeypatch.setattr(recovery.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))))
    assert recovery.inspect_closed_segment(path)[1] == "segment_recovery_missing_seek_index"


def test_normal_finalization_rejects_an_unfinished_container(worker, monkeypatch, tmp_path, history_database):
    job = configured_job(worker, monkeypatch, tmp_path, history_database)
    path = tmp_path / "Camera-2026-11-12-10-00-00.mkv"
    broken_media(path)
    worker.create_segment_metadata_if_needed(job, path)
    assert not worker.finalize_segment_path(job, path)
    with history_database.connect() as conn:
        assert conn.execute(text("SELECT status FROM recording_segments")).scalar_one() == "writing"


def test_buffered_mp4_missing_index_is_repaired_before_publication(worker, monkeypatch, tmp_path, history_database):
    job = configured_job(worker, monkeypatch, tmp_path, history_database)
    path = tmp_path / "Camera-ev4-sub-2026-11-12T10-00-00-123000Z.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10",
        "-t", "3", "-c:v", "libx264", "-threads", "1", "-g", "10", "-an",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof", str(path),
    ], check=True, timeout=15, capture_output=True)
    # FFprobe reads this file, but a Media3 clipping seek cannot use it yet.
    assert recovery.inspect_closed_segment(path)[1] == "segment_recovery_missing_seek_index"
    worker.recover_interrupted_segments(job)
    worker.segment_repairs._queue.join()
    media, reason = recovery.inspect_closed_segment(path)
    assert media is not None and reason == "probe_ok"
    with history_database.connect() as conn:
        row = conn.execute(text("SELECT status, stream_type, started_at FROM recording_segments")).one()
        assert row.status == "finalized" and row.stream_type == "sub"
        assert row.started_at.microsecond == 123000


@pytest.mark.parametrize("fault", ["streaming_tail", "sigkill"])
def test_real_broken_tail_becomes_indexed_without_stopping_other_camera(worker, monkeypatch, tmp_path, history_database, fault):
    job = configured_job(worker, monkeypatch, tmp_path, history_database)
    path = tmp_path / "Camera-2026-11-12-10-00-00.mkv"
    if fault == "streaming_tail":
        broken_media(path)
    else:
        proc = subprocess.Popen([
            "ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
            "-c:v", "mpeg4", "-g", "10", "-threads", "1", "-an", "-cluster_time_limit", "200",
            "-flush_packets", "1", "-stats_period", "0.1", "-progress", "pipe:1", "-y", str(path)
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for line in proc.stdout:
                if line.startswith("frame=") and int(line.split("=", 1)[1]) >= 30:
                    break
            proc.kill()
            proc.wait(timeout=5)
            assert proc.returncode == -9
            assert recovery.inspect_closed_segment(path)[0] is None
            job.process = proc
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=5)
            proc.stdout.close()
    before = path.read_bytes()
    original = path.stat()
    other = worker.RecordingJob(job.camera_id + 1000, "Other", "other", "always", True, process=Process())
    worker.jobs[other.camera_id] = other
    worker.check_children()
    assert other.process.poll() is None
    worker.segment_repairs._queue.join()
    media, reason = recovery.inspect_closed_segment(path)
    assert media is not None and reason == "probe_ok"
    assert recovery.matroska_has_seek_index(path, path.stat().st_size)
    assert path.read_bytes() != before
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    with history_database.connect() as conn:
        segment = conn.execute(text("SELECT * FROM recording_segments")).mappings().one()
        receipt = conn.execute(text("SELECT * FROM recorder_file_receipts")).mappings().one()
        assert segment["status"] == receipt["state"] == "finalized"
        assert segment["size_bytes"] == receipt["size_bytes"] == path.stat().st_size
        assert receipt["object_identity"] == receipt_object_identity(job.archive_root_id, path.name, path.stat())
        assert segment["content_probe_status"] == "probe_ok"
        assert (segment["ended_at"] - segment["started_at"]).total_seconds() == pytest.approx(media.duration_seconds, abs=.001)
    assert other.process.poll() is None and job.next_retry_at is not None
    assert not list(tmp_path.glob(".recovery-*"))


@pytest.mark.parametrize("failure", ["receipt_mismatch", "source_changed", "database_after_replace"])
def test_repaired_publication_never_loses_original_on_conflict(worker, monkeypatch, tmp_path, history_database, failure):
    job = configured_job(worker, monkeypatch, tmp_path, history_database)
    path = tmp_path / "Camera-2026-11-12-10-00-00.mkv"
    broken_media(path)
    worker.create_segment_metadata_if_needed(job, path)
    original = path.stat()
    candidate = repair.remux_closed_segment(path, original)
    if failure == "receipt_mismatch":
        with history_database.begin() as conn:
            conn.execute(text("UPDATE recorder_file_receipts SET inode='foreign'"))
    if failure == "source_changed":
        path.write_bytes(path.read_bytes() + b"changed")
    if failure == "database_after_replace":
        def fail_after_replace(*args, **kwargs):
            assert recovery.inspect_closed_segment(path)[0] is not None
            raise RuntimeError("injected catalog rollback")
        monkeypatch.setattr(SegmentAccounting, "_finalize_verified_segment", fail_after_replace)
    expected = path.read_bytes()
    try:
        if failure == "database_after_replace":
            with pytest.raises(RuntimeError, match="catalog rollback"):
                worker.segment_accounting().finalize_segment_path(job, path, verified_stat=original, replacement_path=candidate)
        else:
            assert not worker.segment_accounting().finalize_segment_path(job, path, verified_stat=original, replacement_path=candidate)
        assert path.read_bytes() == expected
        assert path.stat().st_ino == original.st_ino
        with history_database.connect() as conn:
            assert conn.execute(text("SELECT status FROM recording_segments")).scalar_one() == "writing"
    finally:
        candidate.unlink(missing_ok=True)
    assert not list(tmp_path.glob(".recovery-*"))


@pytest.mark.parametrize("failure", ["disk_full", "ffmpeg_failure", "timeout"])
def test_failed_remux_preserves_source_and_cleans_only_own_temp(tmp_path, monkeypatch, failure):
    path = tmp_path / "source.mkv"
    broken_media(path)
    original = path.stat()
    before = path.read_bytes()
    if failure == "disk_full":
        monkeypatch.setattr(repair.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    else:
        monkeypatch.setattr(repair.subprocess, "run", Mock(
            side_effect=subprocess.TimeoutExpired("ffmpeg", 1) if failure == "timeout" else None,
            return_value=SimpleNamespace(returncode=1)))
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        repair.remux_closed_segment(path, original, timeout=1)
    assert path.read_bytes() == before
    assert path.stat().st_ino == original.st_ino
    assert not list(tmp_path.glob(".recovery-*"))


def test_queue_is_bounded_deduplicated_and_nonblocking(tmp_path, monkeypatch):
    path = tmp_path / "source.mkv"
    path.write_bytes(b"source")
    candidate = tmp_path / "candidate.mkv"
    candidate.write_bytes(b"candidate")
    started, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return candidate
    monkeypatch.setattr(repair, "remux_closed_segment", slow)
    pending = repair.SegmentRepairQueue(capacity=1)
    failed = Mock()
    key = ("job", "source.mkv")
    try:
        assert pending.submit(key, path, path.stat(), lambda *args: True, failed)
        assert started.wait(5)
        assert pending.contains(*key)
        assert pending.submit(key, path, path.stat(), lambda *args: True, failed)
        assert not pending.submit(("other", "other.mkv"), path, path.stat(), lambda *args: True, failed)
        assert path.read_bytes() == b"source"
    finally:
        release.set()
        pending._queue.join()
    failed.assert_not_called()
    assert not pending.contains(*key)


def test_rtsp_socket_has_bounded_read_timeout(worker, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "choose_input_url", lambda _: "rtsp://example.invalid/live")
    monkeypatch.setattr(worker, "active_archive_root", lambda: ("default", tmp_path))
    job = worker.RecordingJob(1, "Camera", "camera", "always", True, db_job_id="own-job")
    row = SimpleNamespace(id=1, name="Camera", segment_minutes=5, rtsp_transport="tcp")
    command, _ = worker.ffmpeg_cmd(row, job)
    assert command[command.index("-timeout") + 1] == "15000000"
    assert command.index("-timeout") < command.index("-i")
