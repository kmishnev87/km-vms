import inspect
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.routers.cameras import create_camera, update_camera
from app.routers.chronology import chronology_playback
from app.routers.recordings import collect_recording_files
from app.schemas.camera import CameraCreate, CameraUpdate
from app.services.recording_retention import (
    AUTO_RETENTION_STATE,
    AUTO_RETENTION_STATE_LOCK,
    build_retention_plan,
    execute_segments,
    retention_diagnostics,
    run_automatic_retention_once,
)


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


def actor(role="owner"):
    return SimpleNamespace(id=1, username=f"{role}_user", role=role, is_active=True)


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage11_retention_")
    tmp_path = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    original_storage_exports = settings.storage_exports
    settings.storage_root = str(tmp_path / "archive")
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        with AUTO_RETENTION_STATE_LOCK:
            AUTO_RETENTION_STATE.update(
                {
                    "running": False,
                    "last_status": "never_run",
                    "last_error": None,
                    "last_summary": None,
                    "run_count": 0,
                }
            )
        session.close()
        settings.storage_root = original_storage_root
        settings.storage_previews = original_storage_previews
        settings.storage_exports = original_storage_exports
        tmp.cleanup()


def camera_payload(**overrides):
    data = {
        "name": "stage11_retention_camera",
        "enabled": False,
        "protocol": "rtsp",
        "host": "127.0.0.1",
        "port": 554,
        "username": "user",
        "password": "password",
        "rtsp_main_url": "/stage11-main",
        "rtsp_sub_url": "/stage11-sub",
        "rtsp_transport": "tcp",
        "recording_mode": "always",
        "default_live_stream": "main",
        "default_record_stream": "main",
        "segment_minutes": 5,
        "retention_days": 1,
        "storage_quota_gb": 1,
    }
    data.update(overrides)
    return CameraCreate(**data)


def add_camera(db, *, name="stage11_retention_camera", retention_days=1, storage_quota_gb=1):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="user",
        password_encrypted=None,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=retention_days,
        storage_quota_gb=storage_quota_gb,
        status="created",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def add_segment(
    db,
    camera,
    *,
    name,
    days_ago=2,
    apparent_size=1024,
    status="finalized",
    ownership="KM VMS",
    source="recorder",
    integrity_status=None,
    job_id=None,
):
    rel = f"kmvms/recordings/{camera.storage_folder_name}/{name}.mkv"
    path = Path(settings.storage_root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        if apparent_size > 0:
            fh.seek(apparent_size - 1)
            fh.write(b"0")
    started = datetime.utcnow() - timedelta(days=days_ago)
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id=job_id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(path),
        relative_path=rel,
        started_at=started,
        ended_at=started + timedelta(seconds=60),
        duration_sec=60,
        size_bytes=apparent_size,
        stream_type="main",
        status=status,
        ownership=ownership,
        source=source,
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status=integrity_status,
        finalized_at=started + timedelta(seconds=60) if status == "finalized" else None,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment, path


def test_create_camera_with_one_gb_quota_persists_without_clamp(db):
    camera = create_camera(camera_payload(storage_quota_gb=1), FakeRequest(), db=db, current_user=actor())

    assert camera.storage_quota_gb == 1
    assert db.get(Camera, camera.id).storage_quota_gb == 1


def test_update_camera_with_one_gb_quota_persists_and_reload_shows_one(db):
    camera = create_camera(camera_payload(name="stage11_update_camera"), FakeRequest(), db=db, current_user=actor())
    camera.storage_quota_gb = 50
    db.commit()

    updated = update_camera(camera.id, CameraUpdate(storage_quota_gb=1), FakeRequest(), db=db, current_user=actor())

    db.refresh(camera)
    assert updated.storage_quota_gb == 1
    assert camera.storage_quota_gb == 1


def test_invalid_quota_and_retention_values_are_rejected():
    with pytest.raises(ValidationError):
        camera_payload(storage_quota_gb=0)
    with pytest.raises(ValidationError):
        CameraUpdate(storage_quota_gb=0)
    with pytest.raises(ValidationError):
        camera_payload(retention_days=0)


def test_retention_plan_marks_old_segments_and_quota_candidates(db):
    camera = add_camera(db, retention_days=1, storage_quota_gb=1)
    old_segment, _old_path = add_segment(db, camera, name="stage11_retention_old", days_ago=3, apparent_size=1024)
    add_segment(db, camera, name="stage11_retention_new_a", days_ago=0, apparent_size=700 * 1024 * 1024)
    add_segment(db, camera, name="stage11_retention_new_b", days_ago=0, apparent_size=700 * 1024 * 1024)

    plan = build_retention_plan(db, camera_id=camera.id)
    reasons = {item["segment_id"]: item["reason"] for item in plan["items"]}

    assert "retention_days" in reasons[old_segment.id]
    assert any("storage_quota" in reason for reason in reasons.values())


def test_automatic_retention_run_once_uses_safe_runner_and_deletes_eligible_old_segment(db):
    source = inspect.getsource(run_automatic_retention_once)
    assert "execute_segments(" in source
    assert ".unlink(" not in source

    camera = add_camera(db, retention_days=1, storage_quota_gb=50)
    segment, path = add_segment(db, camera, name="stage11_retention_auto_old", days_ago=3)

    result = run_automatic_retention_once(db, max_candidates=5, max_bytes=1024 * 1024)

    db.refresh(segment)
    assert result["operation"] == "retention_auto_run"
    assert result["deleted_count"] == 1
    assert segment.status == "deleted"
    assert segment.deletion_source == "retention_auto_run"
    assert not path.exists()
    assert collect_recording_files(db) == []
    playback = chronology_playback(camera_id=camera.id, ts=segment.started_at.isoformat(), db=db, current_user=actor())
    assert playback["has_video"] is False


def test_automatic_retention_skips_active_non_finalized_foreign_and_problem_segments(db):
    camera = add_camera(db, retention_days=1, storage_quota_gb=50)
    job = RecordingJob(id="stage11_retention_active_job", camera_id=camera.id, state="recording", started_at=datetime.utcnow())
    db.add(job)
    db.commit()
    active, active_path = add_segment(db, camera, name="stage11_retention_active", days_ago=3, job_id=job.id)
    draft, draft_path = add_segment(db, camera, name="stage11_retention_draft", days_ago=3, status="recording")
    foreign, foreign_path = add_segment(db, camera, name="stage11_retention_foreign", days_ago=3, ownership="third_party")
    problem, problem_path = add_segment(db, camera, name="stage11_retention_problem", days_ago=3, integrity_status="corrupted_file")

    result = run_automatic_retention_once(db, max_candidates=10, max_bytes=1024 * 1024)

    assert result["deleted_count"] == 0
    assert result["skipped_count"] >= 1
    for segment, path in ((active, active_path), (draft, draft_path), (foreign, foreign_path), (problem, problem_path)):
        db.refresh(segment)
        assert segment.status != "deleted"
        assert path.exists()


def test_automatic_retention_enforces_quota_oldest_first_with_bounds(db):
    camera = add_camera(db, retention_days=30, storage_quota_gb=1)
    oldest, oldest_path = add_segment(db, camera, name="stage11_retention_quota_oldest", days_ago=3, apparent_size=700 * 1024 * 1024)
    newer, newer_path = add_segment(db, camera, name="stage11_retention_quota_newer", days_ago=2, apparent_size=700 * 1024 * 1024)

    result = run_automatic_retention_once(db, max_candidates=1, max_bytes=800 * 1024 * 1024)

    db.refresh(oldest)
    db.refresh(newer)
    assert result["deleted_count"] == 1
    assert oldest.status == "deleted"
    assert newer.status == "finalized"
    assert not oldest_path.exists()
    assert newer_path.exists()


def test_automatic_retention_makes_bounded_progress_when_plan_bytes_exceed_max_bytes(db):
    camera = add_camera(db, retention_days=30, storage_quota_gb=1)
    oldest, oldest_path = add_segment(db, camera, name="stage111_retention_quota_oldest", days_ago=4, apparent_size=700 * 1024 * 1024)
    middle, middle_path = add_segment(db, camera, name="stage111_retention_quota_middle", days_ago=3, apparent_size=700 * 1024 * 1024)
    newest, newest_path = add_segment(db, camera, name="stage111_retention_quota_newest", days_ago=2, apparent_size=700 * 1024 * 1024)

    first = run_automatic_retention_once(db, max_candidates=5, max_bytes=800 * 1024 * 1024)
    db.refresh(oldest)
    db.refresh(middle)
    db.refresh(newest)

    assert first["deleted_count"] == 1
    assert first["bounded_requested_count"] == 2
    assert first["bounded_executed_count"] == 1
    assert first["bounded_skipped_due_to_limit_count"] == 1
    assert first["oversized_single_segment_progress"] is False
    assert oldest.status == "deleted"
    assert middle.status == "finalized"
    assert newest.status == "finalized"
    assert not oldest_path.exists()
    assert middle_path.exists()
    assert newest_path.exists()

    second = run_automatic_retention_once(db, max_candidates=5, max_bytes=800 * 1024 * 1024)
    db.refresh(middle)
    db.refresh(newest)

    assert second["deleted_count"] == 1
    assert middle.status == "deleted"
    assert newest.status == "finalized"
    assert not middle_path.exists()
    assert newest_path.exists()


def test_automatic_retention_deletes_one_oversized_oldest_segment_for_progress(db):
    camera = add_camera(db, retention_days=30, storage_quota_gb=1)
    oversized, oversized_path = add_segment(db, camera, name="stage111_retention_oversized", days_ago=3, apparent_size=2 * 1024 * 1024 * 1024)
    small, small_path = add_segment(db, camera, name="stage111_retention_small", days_ago=2, apparent_size=128 * 1024 * 1024)

    result = run_automatic_retention_once(db, max_candidates=5, max_bytes=1024 * 1024 * 1024)

    db.refresh(oversized)
    db.refresh(small)
    assert result["deleted_count"] == 1
    assert result["bounded_requested_count"] == 1
    assert result["bounded_executed_count"] == 1
    assert result["oversized_single_segment_progress"] is True
    assert "oversized_single_segment_progress" in result["warnings"]
    assert oversized.status == "deleted"
    assert small.status == "finalized"
    assert not oversized_path.exists()
    assert small_path.exists()


def test_manual_retention_keeps_strict_limit_exceeded_semantics(db):
    camera = add_camera(db, retention_days=30, storage_quota_gb=50)
    first, first_path = add_segment(db, camera, name="stage111_manual_first", days_ago=3, apparent_size=700 * 1024 * 1024)
    second, second_path = add_segment(db, camera, name="stage111_manual_second", days_ago=2, apparent_size=700 * 1024 * 1024)

    result = execute_segments(
        db,
        [first, second],
        actor=actor(),
        operation="manual_retention_contract_test",
        reason="manual_limit_contract",
        max_candidates=5,
        max_bytes=800 * 1024 * 1024,
    )

    db.refresh(first)
    db.refresh(second)
    assert result["deleted_count"] == 0
    assert result["limit_exceeded"] is True
    assert first.status == "finalized"
    assert second.status == "finalized"
    assert first_path.exists()
    assert second_path.exists()


def test_automatic_retention_is_concurrency_safe(db):
    with AUTO_RETENTION_STATE_LOCK:
        AUTO_RETENTION_STATE["running"] = True
    result = run_automatic_retention_once(db)

    assert result["ok"] is False
    assert result["items"][0]["reason"] == "automatic_retention_already_running"


def test_automatic_retention_diagnostics_and_audit_summary(db):
    camera = add_camera(db, retention_days=1, storage_quota_gb=50)
    add_segment(db, camera, name="stage11_retention_diag_old", days_ago=3)

    result = run_automatic_retention_once(db, max_candidates=5, max_bytes=1024 * 1024)
    diagnostics = retention_diagnostics(db)
    event_types = {event.event_type for event in db.query(AuditEvent).all()}

    assert result["deleted_count"] == 1
    assert diagnostics["automatic_retention"]["last_summary"]["deleted_count"] == 1
    assert "retention.auto_run_started" in event_types
    assert "retention.auto_run_completed" in event_types
