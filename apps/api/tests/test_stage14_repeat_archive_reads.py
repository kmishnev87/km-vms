"""End-to-end read-path bounds and real recordings-route range semantics."""
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.routers import chronology, recordings
from app.services import recorder_runtime_status as recorder, system_runtime_status as system
from app.services.recording_storage import ROOT_RESOLUTION_RESOLVED
from app.services.timezone_contract import parse_api_timestamp, timezone_context
from test_storage_recording_contract import db, add_camera, actor
from test_stage14_history_large_totals_postgres import history_database


def seed_history(db, count, *, jobs=False):
    cameras = [add_camera(db, f"repeat-history-{index}") for index in range(2)]
    ids = [camera.id for camera in cameras]
    root = ArchiveRoot(id="repeat-root", label="Repeat", root_path="/tmp/repeat-dummy-archive")
    db.add(root)
    db.commit()
    start = datetime(2026, 1, 1)
    rows = [
        dict(camera_id=ids[index % 2], file_path="history.mkv", relative_path="history.mkv",
             started_at=start + timedelta(seconds=index * 10), ended_at=start + timedelta(seconds=index * 10 + 5),
             finalized_at=start + timedelta(seconds=index * 10 + 5), duration_sec=5,
             status="finalized", source="recorder", ownership="KM VMS", archive_root_id=root.id,
             archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED)
        for index in range(count)
    ]
    db.execute(RecordingSegment.__table__.insert(), rows)
    if jobs:
        db.execute(RecordingJob.__table__.insert(), [
            dict(id=f"repeat-job-{index}", camera_id=ids[index % 2], started_at=start + timedelta(seconds=index * 10),
                 state="stopped", recorder_instance_id="repeat-instance", ownership="KM VMS", source="recorder")
            for index in range(count)
        ])
        db.execute(RecordingSegment.__table__.insert(), [
            dict(camera_id=ids[index % 2], file_path="writing.mkv", relative_path="writing.mkv",
                 started_at=start + timedelta(seconds=index * 10), status="writing", source="recorder")
            for index in range(count)
        ])
        db.execute(text("""CREATE TABLE IF NOT EXISTS recorder_runtime_status (
            recorder_instance_id VARCHAR(255) PRIMARY KEY, service_status VARCHAR(50), loop_state VARCHAR(100),
            started_at TIMESTAMP, heartbeat_at TIMESTAMP, active_jobs_count INTEGER,
            recording_cameras_count INTEGER, failed_cameras_count INTEGER, last_error TEXT,
            last_exit_code INTEGER, updated_at TIMESTAMP)"""))
        db.execute(text("""INSERT INTO recorder_runtime_status
            (recorder_instance_id, service_status, loop_state, started_at, heartbeat_at,
             active_jobs_count, recording_cameras_count, failed_cameras_count, updated_at)
            VALUES ('repeat-instance', 'healthy', 'running', :now, :now, 0, 0, 0, :now)"""),
            {"now": recorder.utc_now()})
    db.commit()
    db.expunge_all()
    return ids, start


def assert_full_status_bound(db, count):
    ids, start = seed_history(db, count, jobs=True)
    segment_loads, job_loads = [], []
    def segment_loaded(row, _): segment_loads.append(row.id)
    def job_loaded(row, _): job_loads.append(row.id)
    event.listen(RecordingSegment, "load", segment_loaded)
    event.listen(RecordingJob, "load", job_loaded)
    try:
        result = system.build_operator_runtime_status(db)
    finally:
        event.remove(RecordingSegment, "load", segment_loaded)
        event.remove(RecordingJob, "load", job_loaded)
    assert len(result["domains"]["cameras"]["items"]) == 2
    assert len(segment_loads) <= 9, f"full status materialized {len(segment_loads)} historical segments"
    assert len(job_loads) <= 6, f"full status materialized {len(job_loads)} historical jobs"
    latest = recorder.current_recording_jobs(db, "repeat-instance")
    assert {job.id for job in latest.values()} == {f"repeat-job-{count - 2}", f"repeat-job-{count - 1}"}


@pytest.mark.parametrize("count", [1000, 4000])
def test_full_operator_status_materialization_is_bounded_by_cameras(db, count):
    assert_full_status_bound(db, count)


@pytest.mark.skipif(not os.getenv("KMVMS_STAGE2_POSTGRES_URL"), reason="owned test PostgreSQL required")
def test_full_operator_status_uses_valid_postgres_sql(history_database):
    with Session(history_database) as db:
        assert_full_status_bound(db, 1000)


def assert_chronology_bound(db, count, monkeypatch):
    ids, start = seed_history(db, count)
    loads, resolutions = [], []
    def loaded(row, _): loads.append(row.id)
    monkeypatch.setattr(chronology, "_resolve_segment_path", lambda segment, **kwargs: resolutions.append(segment.id) or Path("/tmp/dummy.mkv"))
    target_dt = start + timedelta(seconds=(count - 1) * 10 + 2)
    event.listen(RecordingSegment, "load", loaded)
    try:
        target = parse_api_timestamp(target_dt.isoformat() + "Z", timezone_context(db))
        point = chronology._segment_covering_timestamp(db, camera_id=ids[1], target=target)
        assert point is not None
        assert len(loads) <= 2
        loads.clear()
        playback = chronology.chronology_playback(ids[1], target_dt.isoformat() + "Z", db=db, current_user=actor())
        assert playback["has_video"] and playback["offset_sec"] == 2
        assert len(loads) <= 2 and len(resolutions) == 1
        loads.clear()
        resolutions.clear()
        result = chronology.chronology_ranges(
            camera_ids=",".join(map(str, ids)),
            from_ts=(target_dt - timedelta(seconds=12)).isoformat() + "Z",
            to_ts=(target_dt + timedelta(seconds=2)).isoformat() + "Z", db=db, current_user=actor())
        assert sum(len(value["ranges"]) for value in result["items"].values()) == 2
        assert len(loads) <= 4 and len(resolutions) == 2
    finally:
        event.remove(RecordingSegment, "load", loaded)


@pytest.mark.parametrize("count", [1000, 4000])
def test_point_playback_and_short_ranges_do_not_scan_older_history(db, monkeypatch, count):
    assert_chronology_bound(db, count, monkeypatch)


@pytest.mark.skipif(not os.getenv("KMVMS_STAGE2_POSTGRES_URL"), reason="owned test PostgreSQL required")
def test_chronology_overlap_uses_valid_postgres_sql(history_database, monkeypatch):
    with Session(history_database) as db:
        assert_chronology_bound(db, 1000, monkeypatch)


@pytest.mark.parametrize("end_source", ["ended_at", "finalized_at", "duration_sec"])
def test_point_sql_preserves_exact_end_precedence_and_subsecond_duration(db, end_source):
    camera = add_camera(db)
    start = datetime(2026, 1, 1, microsecond=125000)
    segment = RecordingSegment(camera_id=camera.id, file_path="precision.mkv", relative_path="precision.mkv",
        started_at=start, status="finalized", source="recorder", ownership="KM VMS", duration_sec=10)
    if end_source != "duration_sec":
        setattr(segment, end_source, start + timedelta(seconds=10))
    db.add(segment)
    db.commit()
    for offset, expected in [(9.999, True), (10, False), (-0.001, False)]:
        target = parse_api_timestamp((start + timedelta(seconds=offset)).isoformat() + "Z", timezone_context(db))
        selected = chronology._segment_covering_timestamp(db, camera_id=camera.id, target=target)
        assert (selected is not None) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("header,expected,status,content_range", [
    (None, b"0123456789", 200, None),
    ("bytes=-4", b"6789", 206, "bytes 6-9/10"),
    ("bytes=-99", b"0123456789", 206, "bytes 0-9/10"),
    ("bytes=0-99", b"0123456789", 206, "bytes 0-9/10"),
    ("bytes=2-5", b"2345", 206, "bytes 2-5/10"),
    ("bytes=6-", b"6789", 206, "bytes 6-9/10"),
])
async def test_real_recordings_stream_route_uses_correct_ranges_and_auth(db, monkeypatch, tmp_path, header, expected, status, content_range):
    camera = add_camera(db)
    segment = RecordingSegment(camera_id=camera.id, file_path="range.mkv", relative_path="range.mkv",
        started_at=datetime(2026, 1, 1), status="finalized", source="recorder", ownership="KM VMS",
        archive_root_id="repeat-root", archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED)
    # SQLite fixture permits the metadata-only root; no archive file lookup is performed.
    db.add(ArchiveRoot(id="repeat-root", label="Repeat", root_path="/tmp/repeat-dummy-archive"))
    db.add(segment)
    db.commit()
    media = tmp_path / "range.mkv"
    media.write_bytes(b"0123456789")
    auth = []
    monkeypatch.setattr(recordings, "validate_media_token", lambda *args, **kwargs: auth.append(kwargs))
    monkeypatch.setattr(recordings, "resolve_segment_file", lambda *args: media)
    request = Request({"type": "http", "headers": [(b"range", header.encode())] if header else []})
    response = recordings.stream_recording(
        request, path=None, segment_id=segment.id, archive_root_id="repeat-root", recording_ref=None,
        media_token="dummy", db=db)
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == expected and response.status_code == status
    assert response.headers.get("content-range") == content_range
    assert int(response.headers["content-length"]) == len(expected)
    assert auth[0]["scope"] == "recording" and auth[0]["permission"] == "view_recordings"


@pytest.mark.parametrize("header,status", [("bytes=10-", 416), ("bytes=6-2", 416), ("items=0-1", 400), ("bytes=0-1,3-4", 400)])
def test_range_errors_keep_deliberate_status_and_size_header(tmp_path, header, status):
    media = tmp_path / "range.mkv"
    media.write_bytes(b"0123456789")
    request = Request({"type": "http", "headers": [(b"range", header.encode())]})
    with pytest.raises(HTTPException) as error:
        recordings.stream_video(request, media)
    assert error.value.status_code == status
    if status == 416:
        assert error.value.headers["Content-Range"] == "bytes */10"
