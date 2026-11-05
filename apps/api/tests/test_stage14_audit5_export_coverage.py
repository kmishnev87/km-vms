from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.recording import ArchiveExportJob
from app.services.archive_exports import _coverage_plan, _validate_full_coverage, _ffprobe_duration
from test_archive_export_stage11 import api_db, client, add_user, add_camera, add_media_segment, valid_payload, auth_headers


@pytest.mark.parametrize("intervals,expected", [
    ([(0, 50), (10, 20)], [(0, 0, 50)]),
    ([(0, 30), (20, 50)], [(0, 0, 30), (1, 30, 50)]),
    ([(0, 50), (0, 20)], [(0, 0, 50)]),
    ([(0, 25), (25, 50)], [(0, 0, 25), (1, 25, 50)]),
    ([(0, 50), (0, 50)], [(0, 0, 50)]),
])
def test_shared_plan_never_repeats_an_instant(intervals, expected):
    start = datetime(2026, 1, 1)
    at = lambda seconds: start + timedelta(seconds=seconds)
    plan = _coverage_plan(at(0), at(50), [(at(a), at(b)) for a, b in intervals])
    assert [(index, int((a-start).total_seconds()), int((b-start).total_seconds())) for index, a, b in plan] == expected
    assert _validate_full_coverage(SimpleNamespace(start_ts=at(0), end_ts=at(50)), [(at(a), at(b)) for a, b in intervals]) == []
    with pytest.raises(ValueError, match="source_gap"):
        _validate_full_coverage(SimpleNamespace(start_ts=at(0), end_ts=at(50)), [(at(0), at(10)), (at(20), at(50))])


def test_real_ffmpeg_overlap_produces_requested_duration(client, api_db):
    db, _ = api_db
    actor = add_user(db)
    camera = add_camera(db)
    start = (datetime.now(timezone.utc)-timedelta(minutes=10)).replace(tzinfo=None, microsecond=0)
    first = add_media_segment(db, camera, started=start, seconds=6, name="overlap-first.mkv", gop=1)
    add_media_segment(db, camera, started=start+timedelta(seconds=4), seconds=6, name="overlap-second.mkv", gop=1)
    created = client.post("/archive/exports", json=valid_payload(camera, first,
        start_ts=start.replace(tzinfo=timezone.utc).isoformat(),
        end_ts=(start+timedelta(seconds=10)).replace(tzinfo=timezone.utc).isoformat()), headers=auth_headers(actor))
    assert created.status_code == 200
    result = client.post(f"/archive/exports/{created.json()['id']}/generate", headers=auth_headers(actor))
    assert result.status_code == 200 and result.json()["status"] == "done"
    job = db.get(ArchiveExportJob, created.json()["id"])
    assert abs(_ffprobe_duration(Path(settings.storage_exports)/job.internal_output_path) - 10) < 0.3
