"""Restart the owner of a hard-cut file without touching any real NAS archive."""
import subprocess
import threading
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from recorder_core import segment_repair as repair
from recorder_core.segment_recovery import inspect_closed_segment
from recorder_core.startup_recovery import StartupSegmentRecovery
from test_recorder_interrupted_finalization import configured_job, make_media
from test_recorder_seekable_recovery import broken_media
from test_self_recovery_stage1374 import Process, worker
from test_stage14_history_large_totals_postgres import history_database


def prepare_old_owner(worker, monkeypatch, root, engine, kind="broken_mkv"):
    job = configured_job(worker, monkeypatch, root, engine)
    path = root / ("Camera-2026-11-12-10-00-00.mp4" if kind == "broken_mp4" else "Camera-2026-11-12-10-00-00.mkv")
    if kind == "closed":
        make_media(path)
    elif kind == "broken_mp4":
        result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
            "-t", "4", "-c:v", "mpeg4", "-g", "10", "-threads", "1", "-an", "-movflags", "+frag_keyframe+empty_moov",
            "-f", "mp4", "pipe:1"], capture_output=True, check=True, timeout=15)
        path.write_bytes(result.stdout[:-61])
    else:
        broken_media(path)
    worker.create_segment_metadata_if_needed(job, path)
    with engine.begin() as conn:
        conn.execute(text("UPDATE recording_jobs SET recorder_instance_id='previous-boot',state='recording'"))
    worker.jobs.clear()  # RAM/process state is gone, exactly as after restart.
    worker.mark_stale_writing_segments_on_startup()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE recorder_runtime_status (recorder_instance_id TEXT, loop_state TEXT)"))
    worker.reconcile_previous_recorder_instances()
    return job, path


def sweep(worker, engine):
    queue = repair.SegmentRepairQueue()
    service = StartupSegmentRecovery(engine, worker.RECORDER_INSTANCE_ID, worker.segment_accounting(), queue, Mock())
    service.run()
    queue._queue.join()
    return service


@pytest.mark.parametrize("kind", ["broken_mkv", "broken_mp4", "closed"])
def test_old_boot_files_become_seekable_while_new_camera_keeps_recording(worker, monkeypatch, tmp_path, history_database, kind):
    old, path = prepare_old_owner(worker, monkeypatch, tmp_path, history_database, kind)
    before = path.stat()
    healthy = worker.RecordingJob(old.camera_id + 1000, "Other", "other", "always", True, process=Process())
    worker.jobs[healthy.camera_id] = healthy
    sweep(worker, history_database)
    media, reason = inspect_closed_segment(path)
    assert media is not None and reason == "probe_ok"
    assert healthy.process.poll() is None
    with history_database.connect() as conn:
        segment = conn.execute(text("SELECT * FROM recording_segments")).mappings().one()
        receipt = conn.execute(text("SELECT * FROM recorder_file_receipts")).mappings().one()
        assert segment["status"] == receipt["state"] == "finalized"
        assert segment["content_probe_status"] == "probe_ok"
        assert receipt["inode"] == str(path.stat().st_ino)
        assert segment["duration_sec"] > 0
    if kind == "closed":
        assert path.stat().st_ino == before.st_ino  # No rewrite of an already correct file.
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    inode = path.stat().st_ino
    sweep(worker, history_database)  # Repeating startup must be a no-op.
    assert path.stat().st_ino == inode
    assert not list(tmp_path.glob(".recovery-*"))


@pytest.mark.parametrize("conflict", ["receipt", "current_owner", "deleted", "active_old_owner"])
def test_startup_never_adopts_a_foreign_or_live_file(worker, monkeypatch, tmp_path, history_database, conflict):
    _, path = prepare_old_owner(worker, monkeypatch, tmp_path, history_database)
    before, inode = path.read_bytes(), path.stat().st_ino
    with history_database.begin() as conn:
        if conflict == "receipt":
            conn.execute(text("UPDATE recorder_file_receipts SET inode='foreign'"))
        elif conflict == "current_owner":
            conn.execute(text("UPDATE recording_jobs SET recorder_instance_id=:instance"), {"instance": worker.RECORDER_INSTANCE_ID})
        elif conflict == "active_old_owner":
            conn.execute(text("UPDATE recording_jobs SET state='recording'"))
        else:
            conn.execute(text("UPDATE recording_segments SET deleted_at=NOW()"))
    sweep(worker, history_database)
    assert path.read_bytes() == before and path.stat().st_ino == inode
    with history_database.connect() as conn:
        assert conn.execute(text("SELECT status FROM recording_segments")).scalar_one() == "stale_writing"


def test_startup_thread_does_not_block_the_recorder_loop():
    entered, release = threading.Event(), threading.Event()
    service = StartupSegmentRecovery(None, "new", None, None, Mock())
    def slow(_):
        entered.set()
        assert release.wait(5)
        return []
    service._rows = slow
    service._recover_publications = lambda: None
    try:
        service.start()
        assert entered.wait(2)
        assert service.thread.is_alive()
        service.stop()
    finally:
        release.set(); service.thread.join(5)
    assert not service.thread.is_alive()


@pytest.mark.parametrize("phase", ["before_rename", "after_rename", "after_database_commit"])
def test_a_second_power_loss_during_repair_is_reconciled_on_next_boot(worker, monkeypatch, tmp_path, history_database, phase):
    job, path = prepare_old_owner(worker, monkeypatch, tmp_path, history_database)
    original = path.stat()
    candidate = repair.remux_closed_segment(path, original)
    if phase == "after_database_commit":
        with monkeypatch.context() as change:
            # Simulate process death: Python's context-manager cleanup never ran.
            change.setattr(repair.ReplacementPublication, "__exit__", lambda *args: False)
            assert worker.segment_accounting().finalize_segment_path(job, path, verified_stat=original,
                replacement_path=candidate, interrupted_instance=worker.RECORDER_INSTANCE_ID)
    else:
        publication = repair.ReplacementPublication(path, candidate, original, root=tmp_path)
        if phase == "before_rename":
            replace = repair.os.replace
            def power_loss(source, target):
                if source == candidate:
                    raise SystemExit("simulated power loss")
                return replace(source, target)
            with monkeypatch.context() as change:
                change.setattr(repair.os, "replace", power_loss)
                with pytest.raises(SystemExit):
                    publication.publish()
        else:
            publication.publish()
    assert list((tmp_path / repair.JOURNAL_DIRECTORY).glob("*.json"))
    sweep(worker, history_database)
    media, reason = inspect_closed_segment(path)
    assert media is not None and reason == "probe_ok"
    with history_database.connect() as connection:
        assert connection.execute(text("SELECT status FROM recording_segments")).scalar_one() == "finalized"
        assert connection.execute(text("SELECT inode FROM recorder_file_receipts")).scalar_one() == str(path.stat().st_ino)
    assert not list((tmp_path / repair.JOURNAL_DIRECTORY).iterdir())
    assert not list(tmp_path.glob(".recovery-*"))
