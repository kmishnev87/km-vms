"""Isolated end-to-end profiles: genuine Recorder, FFmpeg, archive and export."""
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Camera, RecordingJob, RecordingSegment, SystemSettings
from app.models.recording import ArchiveRoot
from app.routers.chronology import chronology_playback
from app.schemas.ai_recording import PolicyUpdate
from app.services import ai_recording, archive_exports, camera_events
from app.services.camera_event_health import Recovery, publish_channel
from app.services.onvif_events import Signal
from app.services.storage_operations_foundation import acquire_worker_lease
from recorder_core.event_buffer import BufferedCamera, Feed, PacketBudget, Sample, capture_command
from recorder_core.storage_activation import recording_start_allowed
from test_self_recovery_stage1374 import worker
from test_stage14_history_large_totals_postgres import history_database


def ffmpeg(args, timeout=30):
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *args], capture_output=True,
                            timeout=timeout)
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-1200:]


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    return json.loads(result.stdout)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def video_span(path):
    data = probe(path)
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    return video["width"], video["height"], float(data["format"]["duration"])


def evidence_out(data):
    target = os.environ.get("KMVMS_STAGE4_EVIDENCE")
    if target:
        with Path(target, "profiles.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(data) + "\n")


@pytest.mark.parametrize("profile", ["economical", "full_quality", "balanced"])
def test_source_to_seekable_archive(profile, history_database, worker, monkeypatch, tmp_path):
    import av  # Fail clearly if the pinned media runtime was not installed.

    assert av.__version__ == "16.1.0"
    engine = history_database

    @event.listens_for(engine, "checkout")
    def use_nas_database_clock(connection, *_):
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

    fixture_path = os.environ.get("KMVMS_STAGE4_RTSP_FIXTURE")
    external = bool(fixture_path)
    transport = "tcp"
    budget_seconds = 30 if external and profile != "economical" else 4
    archive_format = "mkv" if profile == "full_quality" and not external else "mp4"
    if external:
        # Optional, explicitly supplied private owned-camera fixture. Never
        # print these URLs or use it for a shared or production database.
        config = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        configured_urls = {"main": config["preview_url"], "sub": config["recording_url"]}
        transport = config["transport"]
        expected_sizes = None
    else:
        configured_urls = {"main": "rtsp://192.0.2.4/preview", "sub": "rtsp://192.0.2.4/recording"}
        fixtures = {}
        expected_sizes = {"main": (320, 180), "sub": (640, 360)}
        for stream, (width, height) in expected_sizes.items():
            target = tmp_path / f"{stream}.mp4"
            # Differing resolution, optional sound, reordered video on the record stream.
            audio = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-c:a", "aac"] if stream == "sub" else []
            ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=12",
                    *audio, "-t", "8", "-c:v", "libx264", "-threads", "2", "-g", "12", "-bf",
                    "2" if stream == "sub" else "0", "-preset", "veryfast", str(target)])
            fixtures[configured_urls[stream]] = str(target)

        def from_file(args, url):
            return args[:args.index("-rtsp_transport")] + [
                "-re", "-stream_loop", "-1", "-i", fixtures[url],
            ] + args[args.index("-i") + 2:]

        monkeypatch.setattr(worker, "BufferedCamera", lambda *args: BufferedCamera(
            *args, command=lambda url, tx: from_file(capture_command(url, tx), url)))

    with Session(engine) as db:
        db.add(SystemSettings(id=1, system_initialized=True, timezone="Asia/Yekaterinburg", language="en",
                              storage_path=str(worker.STORAGE_ROOT), recording_format=archive_format))
        db.get(ArchiveRoot, worker.DEFAULT_ARCHIVE_ROOT_ID).physical_identity = "pv1:" + "4" * 32
        db.add(Camera(id=1, name="Owned stage 4 video", storage_folder_name="stage4-input",
                      host="192.0.2.4", port=80, protocol="onvif", enabled=True, events_enabled=True,
                      rtsp_main_url=configured_urls["main"], rtsp_sub_url=configured_urls["sub"],
                      default_record_stream="sub", default_live_stream="main", rtsp_transport=transport,
                      segment_minutes=1))
        db.commit()
        ai_recording.save_policy(db, 1, PolicyUpdate(expected_revision=0, policy={
            "mode": "events", "source": "camera", "profile": profile,
            "prebuffer_seconds": 0 if profile == "economical" else budget_seconds,
            "postrecord_minutes": 1,
        }), actor=None)
        lease = acquire_worker_lease(db, worker_key="stage4_fixture_onvif", owner_instance_id="fixture", lease_seconds=360)
    cameras = [{"id": 1, "name": "Owned stage 4 video", "events_enabled": True}]
    group = {"device_id": "stage4-fixture", "recording_generation": "fixture-v1", "cameras": cameras}

    def tick():
        worker.check_children()
        worker.sync_cameras()
        worker.confirm_recording_startups()

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            tick()
            if predicate():
                return
            time.sleep(.2)
        details = worker.jobs[1].status_payload()
        buffered = worker.jobs[1].event_buffer
        pytest.fail(f"condition not reached: state={details['state']}, error={details['last_error']}, "
                    f"buffer={buffered.status(worker.jobs[1].process) if buffered else None}")

    def pulse(active, topic="RuleEngine/CellMotionDetector/Motion"):
        at = now()
        with Session(engine) as db:
            return camera_events.record_signal(db, group, cameras[0], Signal(
                topic, "motion", active, False, at, at, False,
                source={"VideoSourceConfigurationToken": "fixture"}))

    try:
        tick()
        job = worker.jobs[1]
        with Session(engine) as db:
            publish_channel(db, group, lease, "fixture-v1", "healthy", sample=now(), recovery=Recovery())
            db.commit()
        assert job.process is None
        primary_process = None
        if profile != "economical":
            wait_for(lambda: job.event_buffer.status()["prebuffer_seconds_available"] >= budget_seconds - 2,
                     budget_seconds + 18)
            assert not job.event_buffer.second or job.event_buffer.second.proc is None
            primary_process = job.event_buffer.primary.proc
        else:
            assert job.event_buffer.primary.proc is None
            assert job.event_buffer.status()["prebuffer_status"] == "disabled_by_profile"
        waiting_ram = worker.event_buffer_budget.total
        assert not list(worker.STORAGE_ROOT.rglob("*.mp4")) and not list(worker.STORAGE_ROOT.rglob("*.mkv"))
        with Session(engine) as db:
            assert db.query(RecordingJob).count() == 0

        start_at = now()
        received = time.monotonic()
        assert pulse(True)
        tick()
        job_id = job.db_job_id
        assert job_id and job.process is not None
        original_process = job.process
        original_deadline = job.event_episode.deadline
        wait_for(lambda: job.state == "recording", 20)
        confirmation_seconds = time.monotonic() - received
        # This START is deduplicated only in UI history; the writer is extended.
        assert pulse(True) is None
        tick()
        assert job.event_episode.extended and job.event_episode.deadline > original_deadline
        assert job.db_job_id == job_id and job.process is original_process
        extended = job.event_episode.deadline
        pulse(False)
        tick()
        assert job.event_episode.deadline == extended
        if profile == "balanced":
            wait_for(lambda: original_process.stream_role == "sub", 16)
        if external:
            # Observe actual wall-clock expiry without manipulating the deadline.
            wait_for(lambda: job.process is None, 72)
        else:
            test_tail = time.monotonic() + (3 if profile == "balanced" else 1.2)
            wait_for(lambda: time.monotonic() >= test_tail, 5)
            job.event_episode.deadline = time.monotonic() - 1
            tick()
        assert job.process is None
        if profile != "economical":
            assert job.event_buffer.primary.proc is primary_process and primary_process.poll() is None
            assert not job.event_buffer.second or job.event_buffer.second.proc is None
        else:
            assert job.event_buffer.primary.proc is None and worker.event_buffer_budget.total == 0
        with Session(engine) as db:
            rows = (db.query(RecordingSegment).filter_by(job_id=job_id)
                    .order_by(RecordingSegment.started_at).all())
            saved = db.get(RecordingJob, job_id)
            assert saved.stop_reason == "event_recording_waiting"
            roles = [row.stream_type for row in rows]
            assert set(roles) == ({"main", "sub"} if profile == "balanced" else {"sub"}), roles
            assert roles[0] == ("main" if profile == "balanced" else "sub")
            assert roles[-1] == "sub"
            assert all(row.status == "finalized" for row in rows)
            parts = []
            previous = None
            for row in rows:
                target = Path(row.file_path)
                width, height, duration = video_span(target)
                if expected_sizes:
                    assert (width, height) == expected_sizes[row.stream_type]
                assert duration > 0
                wanted = (row.started_at + timedelta(seconds=duration * .45)).replace(tzinfo=timezone.utc)
                system = wanted.astimezone(ZoneInfo("Asia/Yekaterinburg")).replace(tzinfo=None)
                response = chronology_playback(camera_id=1, ts=system.isoformat(), db=db, current_user=None)
                assert response["has_video"] and response["segment_id"] == row.id
                assert abs(response["offset_sec"] - duration * .45) < .01
                ffmpeg(["-ss", f"{duration * .45:.4f}", "-i", str(target),
                        "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"])
                parts.append({"role": row.stream_type, "size": [width, height], "seconds": round(duration, 4),
                              "start_relative_sec": (row.started_at - start_at).total_seconds(),
                              "gap_sec": (row.started_at - previous.ended_at).total_seconds() if previous else None})
                previous = row
            history = (start_at - rows[0].started_at).total_seconds()
            if profile != "economical":
                assert 1 < history <= budget_seconds + 1
            else:
                assert -15 < history <= 0
            assert db.query(RecordingJob).count() == 1
            assert all(abs(item["gap_sec"]) <= (.75 if external else .22) for item in parts[1:]), parts
            if profile == "balanced":
                sources = [archive_exports.ResolvedExportSegment(
                    segment=row, path=Path(row.file_path), start_ts=row.started_at, end_ts=row.ended_at,
                    source_size=row.size_bytes, source_mtime_ns=Path(row.file_path).stat().st_mtime_ns,
                ) for row in rows]
                assert archive_exports._needs_buffered_clip_normalization(sources)
                size, audio = archive_exports._probe_clip_inputs(sources)
                plan = archive_exports._coverage_plan(rows[0].started_at, rows[-1].ended_at,
                                                      [(row.started_at, row.ended_at) for row in rows])
                normalized = []
                for index, (source_index, begin, end) in enumerate(plan):
                    path = tmp_path / f"normalized-{index}.mkv"
                    source = sources[source_index]
                    archive_exports._normalize_event_clip_part(source, path, begin, end,
                                                               size=size, has_audio=audio[source.path])
                    normalized.append(path)
                clip = tmp_path / "clip.mkv"
                archive_exports._concat_segments(normalized, clip, tmp_path / "concat.txt")
                clip_duration = video_span(clip)[-1]
                assert abs(clip_duration - sum((b - a).total_seconds() for _, a, b in plan)) < .5
                ffmpeg(["-ss", f"{clip_duration * .7:.3f}", "-i", str(clip),
                        "-t", "0.5", "-f", "null", "-"])
                fixture_dir = os.environ.get("KMVMS_STAGE4_BROWSER_FIXTURES")
                if fixture_dir and not external:
                    shutil.copyfile(rows[0].file_path, Path(fixture_dir) / "preview.mp4")
                    shutil.copyfile(rows[-1].file_path, Path(fixture_dir) / "recording.mp4")
            else:
                clip_duration = None
            evidence_out({
                "profile": profile, "inputs": "real_rtsp" if external else "synthetic",
                "archive_format": archive_format, "prehistory_seconds": round(history, 4),
                "confirm_seconds": round(confirmation_seconds, 4), "parts": parts,
                "clip_seconds": clip_duration, "ram_waiting_bytes": waiting_ram,
                "retained_ram_bytes": worker.event_buffer_budget.total,
                "archive_finalized": True, "seek": "pass",
            })
    finally:
        if worker.jobs.get(1) and worker.jobs[1].process:
            worker.stop_camera(1, "fixture_cleanup")
        if worker.jobs.get(1) and worker.jobs[1].event_buffer:
            worker.jobs[1].event_buffer.close()
        assert worker.event_buffer_budget.total == 0


def test_packet_budget_drops_whole_gops_across_feeds():
    pool = PacketBudget(per_camera=2600, total=3600)
    a = Feed(pool, 1, None, "tcp", "main", 30)
    b = Feed(pool, 1, None, "tcp", "sub", 30)
    c = Feed(pool, 2, None, "tcp", "main", 30)

    def add(feed, i, *, key=True, size=388, at=None):
        pool.append(feed, Sample(i, SimpleNamespace(size=size), i if at is None else at, .04, True, key))

    add(a, 1)
    add(a, 2, key=False)
    add(b, 1)  # Forces both packets of A's oldest GOP out.
    add(a, 3, key=False)
    assert not a.groups and pool.total == 900
    add(a, 4)
    add(c, 1)
    add(c, 2)
    add(c, 3)  # Per-camera pressure on C; global cap never crosses 3600.
    assert c.groups[0].samples[0].number == 2
    add(a, 5, size=5000)
    add(a, 6, key=False)
    assert a.next_key(0) is not None
    assert all(group.samples[0].key for group in pool.groups)
    assert pool.total <= 3600 and all(n <= 2600 for n in pool.cameras.values())
    for feed in (a, b, c):
        feed.close()
    assert pool.total == 0
