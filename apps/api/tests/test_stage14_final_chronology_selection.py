"""F06: one available source for playback/download, with scoped identity."""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.user import User
from app.routers import chronology
from test_stage11_retention_automation import db, add_camera, add_segment


def overlap(db):
    camera = add_camera(db)
    first, first_path = add_segment(db, camera, name="first", days_ago=0)
    second, second_path = add_segment(db, camera, name="second", days_ago=0)
    start = datetime(2026, 11, 5, 3, 0)
    for row, offset in [(first, 0), (second, 30)]:
        row.started_at = start + timedelta(seconds=offset)
        row.ended_at = row.started_at + timedelta(seconds=120)
        row.finalized_at = row.ended_at
        row.duration_sec = 120
    user = User(username="selection-synthetic-owner", role="owner", password_hash="unused-synthetic")
    db.add(user)
    db.commit()
    first_path.write_bytes(b"first-recording")
    second_path.write_bytes(b"second-recording")
    return camera, first, first_path, second, second_path, user, (start + timedelta(seconds=45)).isoformat() + "Z"


def test_playback_token_and_download_choose_same_overlapping_source(db):
    camera, first, path, second, _, user, ts = overlap(db)
    playback = chronology.chronology_playback(camera.id, ts, db, user)
    assert playback["segment_id"] == first.id
    token = chronology.issue_chronology_download_token(camera.id, ts, db, user)
    assert token["segment_id"] == first.id
    response = chronology.chronology_download_current_recording(camera.id, ts, token["media_token"], db)
    assert str(response.path) == str(path)
    pinned = chronology.issue_chronology_download_token(camera.id, ts, db, user,
        segment_id=second.id, archive_root_id=second.archive_root_id)
    assert pinned["segment_id"] == second.id


def test_missing_first_candidate_falls_back_but_old_token_cannot_authorize_other_file(db):
    camera, first, first_path, second, second_path, user, ts = overlap(db)
    old = chronology.issue_chronology_download_token(camera.id, ts, db, user)
    first_path.unlink()  # Only this test's synthetic file.
    playback = chronology.chronology_playback(camera.id, ts, db, user)
    assert playback["has_video"] and playback["segment_id"] == second.id
    token = chronology.issue_chronology_download_token(camera.id, ts, db, user)
    response = chronology.chronology_download_current_recording(camera.id, ts, token["media_token"], db)
    assert str(response.path) == str(second_path)
    with pytest.raises(HTTPException) as stale:
        chronology.chronology_download_current_recording(camera.id, ts, old["media_token"], db)
    assert stale.value.status_code == 403
    with pytest.raises(HTTPException) as pinned_missing:
        chronology.issue_chronology_download_token(camera.id, ts, db, user,
            segment_id=first.id, archive_root_id=first.archive_root_id)
    assert pinned_missing.value.status_code == 409
    second_path.unlink()
    unavailable = chronology.chronology_playback(camera.id, ts, db, user)
    assert not unavailable["has_video"] and unavailable["availability_status"] == "unavailable"
    with pytest.raises(HTTPException) as unavailable_token:
        chronology.issue_chronology_download_token(camera.id, ts, db, user)
    assert unavailable_token.value.status_code == 409
    with pytest.raises(HTTPException) as unauthenticated:
        chronology.chronology_download_current_recording(camera.id, ts, None, db)
    assert unauthenticated.value.status_code == 401


def test_pinned_identity_must_cover_requested_time_and_camera(db):
    camera, first, _, second, _, user, ts = overlap(db)
    other_camera = add_camera(db, name="other-synthetic-camera")
    for camera_id, timestamp in [(other_camera.id, ts), (camera.id, "2026-11-06T03:00:00Z")]:
        with pytest.raises(HTTPException) as invalid:
            chronology.issue_chronology_download_token(camera_id, timestamp, db, user,
                segment_id=first.id, archive_root_id=first.archive_root_id)
        assert invalid.value.status_code == 404
