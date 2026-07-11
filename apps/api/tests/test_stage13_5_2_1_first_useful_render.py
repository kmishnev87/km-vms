import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.routers import recordings as recordings_router
from app.services import recorder_diagnostics
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
)


def actor():
    return SimpleNamespace(id=1, username="stage21_user", role="owner", is_active=True)


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage13_5_2_1_")
    root = Path(tmp.name)
    original_storage_root = settings.storage_root
    settings.storage_root = str(root / "archive")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    session.add(
        SystemSettings(
            system_initialized=True,
            timezone="Asia/Yekaterinburg",
            language="ru",
            storage_path=settings.storage_root,
            recording_format="mkv",
        )
    )
    session.commit()
    ensure_archive_roots(session)
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        tmp.cleanup()


def add_camera(db, name="stage21_camera"):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        retention_days=1,
        storage_quota_gb=50,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def add_segment(db, camera, index):
    started_at = datetime(2026, 5, 18, 10, 0, 0) + timedelta(minutes=index)
    rel = f"kmvms/recordings/{camera.storage_folder_name}/segment-{index:03d}.mkv"
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(Path(settings.storage_root) / rel),
        relative_path=rel,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=5),
        duration_sec=300,
        size_bytes=100 + index,
        stream_type="main",
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=datetime.utcnow(),
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        finalized_at=started_at + timedelta(minutes=5),
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def add_active_job(db, camera):
    job = RecordingJob(
        id="stage13_5_2_1_active_job",
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        state="recording",
        source_stream="main",
        started_at=datetime(2026, 5, 18, 8, 0, 0),
        ownership="KM VMS",
        source="recorder",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_recordings_page_load_verifies_only_returned_page_not_full_archive(db, monkeypatch):
    camera = add_camera(db)
    segments = [add_segment(db, camera, index) for index in range(125)]
    expected_page_paths = {segment.relative_path for segment in segments[-30:]}
    checked_paths = []

    def page_scoped_file_check(segment):
        checked_paths.append(segment.relative_path)
        if segment.relative_path not in expected_page_paths:
            raise AssertionError("recordings list must not check off-page archive files")
        return Path(settings.storage_root) / segment.relative_path, True, None

    monkeypatch.setattr(recordings_router, "segment_file_resolution", page_scoped_file_check)

    payload = recordings_router.list_recordings(
        camera=None,
        date=None,
        from_ts=None,
        to_ts=None,
        limit=30,
        offset=0,
        sort_by="created_at",
        sort_dir="desc",
        db=db,
        current_user=actor(),
    )

    assert len(payload["items"]) == 30
    assert payload["pagination"]["limit"] == 30
    assert payload["pagination"]["offset"] == 0
    assert payload["pagination"]["total_count"] == 125
    assert payload["pagination"]["has_more"] is True
    assert payload["summary"]["count"] == 125
    assert len(checked_paths) == 30
    assert set(checked_paths) == expected_page_paths
    assert {item["availability_status"] for item in payload["items"]} == {"available"}
    assert {item["available"] for item in payload["items"]} == {True}


def test_recordings_page_serializes_available_missing_and_error_truthfully(db, monkeypatch):
    camera = add_camera(db)
    add_segment(db, camera, 1)
    add_segment(db, camera, 2)
    add_segment(db, camera, 3)

    def mixed_file_check(segment):
        file_path = Path(settings.storage_root) / segment.relative_path
        if segment.relative_path.endswith("segment-003.mkv"):
            return file_path, True, None
        if segment.relative_path.endswith("segment-002.mkv"):
            return file_path, False, "missing_file"
        return file_path, False, "verification_error"

    monkeypatch.setattr(recordings_router, "segment_file_resolution", mixed_file_check)

    payload = recordings_router.list_recordings(
        camera=None,
        date=None,
        from_ts=None,
        to_ts=None,
        limit=15,
        offset=0,
        sort_by="created_at",
        sort_dir="desc",
        db=db,
        current_user=actor(),
    )

    by_file = {item["filename"]: item for item in payload["items"]}
    assert by_file["segment-003.mkv"]["availability_status"] == "available"
    assert by_file["segment-003.mkv"]["available"] is True
    assert by_file["segment-002.mkv"]["availability_status"] == "missing"
    assert by_file["segment-002.mkv"]["available"] is False
    assert by_file["segment-001.mkv"]["availability_status"] == "error"
    assert by_file["segment-001.mkv"]["available"] is False
    assert all(str(settings.storage_root) not in str(item) for item in payload["items"])


def test_recordings_limit_is_clamped_to_safe_max(db):
    camera = add_camera(db)
    for index in range(105):
        add_segment(db, camera, index)

    payload = recordings_router.list_recordings(
        camera=None,
        date=None,
        from_ts=None,
        to_ts=None,
        limit=1000,
        offset=0,
        sort_by="created_at",
        sort_dir="desc",
        db=db,
        current_user=actor(),
    )

    assert len(payload["items"]) == 100
    assert payload["pagination"]["limit"] == 100
    assert payload["pagination"]["total_count"] == 105


def test_recordings_supported_page_sizes_and_unsupported_limit_are_bounded(db, monkeypatch):
    camera = add_camera(db)
    for index in range(120):
        add_segment(db, camera, index)

    checked_paths = []

    def counted_file_check(segment):
        checked_paths.append(segment.relative_path)
        return Path(settings.storage_root) / segment.relative_path, True, None

    monkeypatch.setattr(recordings_router, "segment_file_resolution", counted_file_check)

    for limit in (15, 30, 50, 100):
        checked_paths.clear()
        payload = recordings_router.list_recordings(
            camera=None,
            date=None,
            from_ts=None,
            to_ts=None,
            limit=limit,
            offset=limit,
            sort_by="created_at",
            sort_dir="desc",
            db=db,
            current_user=actor(),
        )
        expected_returned = min(limit, 120 - limit)
        assert payload["pagination"]["limit"] == limit
        assert len(payload["items"]) == expected_returned
        assert len(checked_paths) == expected_returned

    checked_paths.clear()
    payload = recordings_router.list_recordings(
        camera=None,
        date=None,
        from_ts=None,
        to_ts=None,
        limit=20,
        offset=0,
        sort_by="created_at",
        sort_dir="desc",
        db=db,
        current_user=actor(),
    )
    assert payload["pagination"]["limit"] == 30
    assert len(payload["items"]) == 30
    assert len(checked_paths) == 30


def test_recorder_summary_does_not_call_storage_or_retention_diagnostics(db, monkeypatch):
    add_camera(db)

    monkeypatch.setattr(
        recorder_diagnostics,
        "build_storage_monitoring_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("storage diagnostics must not run")),
    )
    monkeypatch.setattr(
        recorder_diagnostics,
        "retention_diagnostics",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("retention diagnostics must not run")),
    )

    payload = recorder_diagnostics.build_recorder_summary(db)

    assert payload["summary_contract"] == "lightweight_recorder_ui_status"
    assert "storage_state" not in payload
    assert "retention_status" not in payload
    assert isinstance(payload["camera_recording_states"], list)


def test_manual_delete_allows_finalized_segment_from_still_active_recording_job(db):
    camera = add_camera(db)
    job = add_active_job(db, camera)
    segment = add_segment(db, camera, 1)
    segment.job_id = job.id
    file_path = Path(settings.storage_root) / segment.relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"finalized-video")
    db.add(segment)
    db.commit()

    result = recordings_router.delete_recording(
        path=segment.relative_path,
        db=db,
        current_user=actor(),
    )
    db.refresh(segment)

    assert result["deleted_count"] == 1
    assert result["skipped_count"] == 0
    assert segment.status == "deleted"
    assert not file_path.exists()


def test_single_delete_reports_skipped_as_conflict_not_false_success(db):
    camera = add_camera(db)
    segment = add_segment(db, camera, 2)
    Path(settings.storage_root, "kmvms", "recordings").mkdir(parents=True, exist_ok=True)

    with pytest.raises(HTTPException) as exc:
        recordings_router.delete_recording(
            path=segment.relative_path,
            db=db,
            current_user=actor(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["skipped_count"] == 1
    assert exc.value.detail["items"][0]["reason"] == "file_missing"
