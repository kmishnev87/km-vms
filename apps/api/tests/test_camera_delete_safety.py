import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.user import User
from app.routers.cameras import delete_camera, preview_delete_camera
from app.services.recording_retention import execute_segments


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


def actor(role="owner"):
    return SimpleNamespace(id=1, username=f"{role}_user", role=role, is_active=True)


def make_db():
    tmp = tempfile.TemporaryDirectory(prefix="stage1_delete_test_")
    tmp_path = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    settings.storage_root = str(tmp_path / "storage")
    settings.storage_previews = str(tmp_path / "previews")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    return session, tmp, original_storage_root, original_storage_previews


def cleanup_db(session, tmp, original_storage_root, original_storage_previews):
    try:
        session.close()
    finally:
        settings.storage_root = original_storage_root
        settings.storage_previews = original_storage_previews
        tmp.cleanup()


def with_db(test_func):
    db, tmp, original_storage_root, original_storage_previews = make_db()
    try:
        test_func(db)
    finally:
        cleanup_db(db, tmp, original_storage_root, original_storage_previews)


@pytest.fixture
def db():
    session, tmp, original_storage_root, original_storage_previews = make_db()
    try:
        yield session
    finally:
        cleanup_db(session, tmp, original_storage_root, original_storage_previews)


class raises_http:
    def __init__(self):
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            raise AssertionError("HTTPException was not raised")
        if not isinstance(exc, HTTPException):
            return False
        self.value = exc
        return True


def add_camera(db, *, name="stage1_delete_test_camera"):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="user",
        password_encrypted=None,
        recording_mode="manual",
        default_live_stream="main",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=1,
        storage_quota_gb=50,
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
    relative_path=None,
    status="finalized",
    ownership="KM VMS",
    source="recorder",
    integrity_status=None,
    job_id=None,
    file_exists=True,
):
    rel = relative_path or f"kmvms/recordings/camera_{camera.id}/stage1_delete_test_segment.mkv"
    path = Path(settings.storage_root) / rel
    if file_exists:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stage1")
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id=job_id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(path),
        relative_path=rel,
        started_at=datetime.utcnow(),
        ended_at=datetime.utcnow(),
        duration_sec=1,
        size_bytes=path.stat().st_size if path.exists() else 6,
        stream_type="main",
        status=status,
        ownership=ownership,
        source=source,
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status=integrity_status,
        finalized_at=datetime.utcnow() if status == "finalized" else None,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment, path


def test_camera_delete_without_files_blocks_when_recordings_exist(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)

    with raises_http() as exc:
        delete_camera(camera.id, FakeRequest(), delete_files=False, db=db, current_user=actor("owner"))

    assert exc.value.status_code == 409
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id) is not None
    assert exc.value.detail["recordings"]["reason_counts"]["recordings_exist_delete_files_false_requires_safe_policy"] == 1


def test_camera_delete_with_files_requires_delete_recordings_permission(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)

    with raises_http() as exc:
        delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("operator"))

    assert exc.value.status_code == 403
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id) is not None


def test_camera_delete_with_files_uses_safe_model_and_returns_summary(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)

    result = delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["ok"] is True
    assert result["status"] == "deleted"
    assert result["recordings"]["deleted_count"] == 1
    assert result["recordings"]["failed_count"] == 0
    assert result["recordings"]["skipped_count"] == 0
    assert not file_path.exists()
    assert db.get(Camera, camera.id) is None
    remaining = db.get(RecordingSegment, segment.id)
    if remaining is not None:
        assert remaining.status == "deleted"


def test_camera_delete_with_files_blocks_active_recording(db):
    camera = add_camera(db)
    job = RecordingJob(
        id="stage1_delete_test_job",
        camera_id=camera.id,
        state="recording",
        started_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    segment, file_path = add_segment(db, camera, job_id=job.id)

    with raises_http() as exc:
        delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert exc.value.status_code == 409
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id) is not None
    assert exc.value.detail["recordings"]["reason_counts"]["active_job"] == 1


def test_camera_delete_with_files_skips_foreign_and_outside_namespace(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera, ownership="third_party")

    with raises_http() as exc:
        delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert exc.value.status_code == 409
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id) is not None
    assert exc.value.detail["recordings"]["reason_counts"]["unowned"] == 1


def test_camera_delete_with_files_rejects_path_traversal(db):
    camera = add_camera(db)
    segment, _file_path = add_segment(db, camera, relative_path="../stage1_delete_test_escape.mkv", file_exists=False)

    with raises_http() as exc:
        delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert exc.value.status_code == 409
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id) is not None
    assert exc.value.detail["recordings"]["reason_counts"]["path_outside_storage"] == 1


def test_camera_delete_preview_does_not_mutate_files_or_metadata(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)

    result = preview_delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["status"] == "preview_safe"
    assert result["recordings"]["planned_count"] == 1
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id).status == "finalized"


def test_camera_delete_preview_marks_unsafe_segments_blocked(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera, ownership="third_party")

    result = preview_delete_camera(camera.id, FakeRequest(), delete_files=True, db=db, current_user=actor("owner"))

    assert result["status"] == "preview_blocked"
    assert result["recordings"]["ok"] is False
    assert result["recordings"]["skipped_count"] == 1
    assert result["recordings"]["reason_counts"]["unowned"] == 1
    assert file_path.exists()
    assert db.get(Camera, camera.id) is not None
    assert db.get(RecordingSegment, segment.id).status == "finalized"


def test_execute_segments_recovers_metadata_if_commit_fails_after_file_delete(db):
    camera = add_camera(db)
    segment, file_path = add_segment(db, camera)
    original_commit = db.commit
    calls = {"count": 0}

    def fail_once():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("stage1 simulated metadata commit failure")
        return original_commit()

    db.commit = fail_once
    result = execute_segments(
        db,
        [segment],
        actor=actor("owner"),
        operation="manual_single_delete",
        reason="stage1_commit_failure_test",
        max_candidates=1,
    )
    db.commit = original_commit

    recovered = db.get(RecordingSegment, segment.id)
    assert not file_path.exists()
    assert recovered.status == "deleted"
    assert result["failed_count"] == 1
    assert result["items"][0]["reason"] == "metadata_update_failed_recovered"


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            with_db(value)
    print("camera_delete_safety_tests_ok")
