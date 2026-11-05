from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import chronology
from test_storage_recording_contract import db, actor
from test_stage14_repeat_archive_reads import seed_history


def test_duplicate_camera_ids_do_not_repeat_work(db, monkeypatch):
    ids, start = seed_history(db, 20)
    probes = []
    monkeypatch.setattr(chronology, "_resolve_segment_path", lambda row: probes.append(row.id) or Path("dummy"))
    def request(ids_csv):
        return chronology.chronology_ranges(camera_ids=ids_csv, from_ts=start.isoformat()+"Z",
            to_ts=(start+timedelta(days=1)).isoformat()+"Z", db=db, current_user=actor())
    expected = request(str(ids[0]))
    count = len(probes)
    probes.clear()
    assert request(",".join([str(ids[0])] * 300)) == expected
    assert len(probes) == count == 10
    monkeypatch.setattr(chronology, "MAX_RANGE_SEGMENTS", 3)
    probes.clear()
    with pytest.raises(HTTPException) as error:
        request(str(ids[0]))
    assert error.value.status_code == 422
    assert len(probes) == 3
