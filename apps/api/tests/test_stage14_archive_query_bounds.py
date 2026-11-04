from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.models.recording import RecordingSegment
from app.services import archive_exports, system_runtime_status
from app.services.timezone_contract import timezone_context
from test_archive_export_stage11 import api_db, add_camera


def test_status_loads_one_orm_segment_per_camera_from_100k_rows(api_db):
    db, _ = api_db
    cameras = [add_camera(db, name=f"stage14-{index}") for index in range(2)]
    start = datetime(2026, 1, 1)
    for offset in range(0, 100000, 5000):
        db.execute(RecordingSegment.__table__.insert(), [
            {"camera_id": cameras[index % 2].id, "file_path": "catalog-only.mkv",
             "started_at": start + timedelta(seconds=index), "status": "finalized"}
            for index in range(offset, offset + 5000)
        ])
    db.commit()
    loaded = []
    def on_load(row, _):
        loaded.append(row.id)
    event.listen(RecordingSegment, "load", on_load)
    try:
        latest = system_runtime_status._latest_segment_by_camera(db, datetime.utcnow(), timezone_context(None))
        assert len(latest) == len(loaded) == 2
    finally:
        event.remove(RecordingSegment, "load", on_load)


@pytest.mark.parametrize("count", [120, 121])
def test_export_range_is_selected_before_limit(api_db, monkeypatch, count):
    db, root = api_db
    camera = add_camera(db)
    start = datetime(2026, 11, 4, 12)
    old = start - timedelta(days=1)
    rows = []
    for index in range(1000):
        rows.append({"camera_id": camera.id, "file_path": "old.mkv", "relative_path": "old.mkv",
                     "status": "finalized", "ownership": "KM VMS", "source": "recorder",
                     "started_at": old, "ended_at": old + timedelta(seconds=1)})
    for index in range(count):
        rows.append({"camera_id": camera.id, "file_path": "match.mkv", "relative_path": "match.mkv",
                     "status": "finalized", "ownership": "KM VMS", "source": "recorder",
                     "started_at": start + timedelta(seconds=index), "ended_at": start + timedelta(seconds=index + 1)})
    db.execute(RecordingSegment.__table__.insert(), rows)
    db.commit()
    media = root / "test-clip.mkv"
    media.write_bytes(b"media")
    monkeypatch.setattr(archive_exports, "resolve_segment_file_path", lambda *args, **kwargs: media)
    if count == 121:
        with pytest.raises(HTTPException) as error:
            archive_exports.preflight_source_segments(db, camera_id=camera.id, start_ts=start, end_ts=start + timedelta(minutes=3))
        assert error.value.status_code == 413
    else:
        result = archive_exports.preflight_source_segments(db, camera_id=camera.id, start_ts=start, end_ts=start + timedelta(minutes=3))
        assert len(result.segments) == count
