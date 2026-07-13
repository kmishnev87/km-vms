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
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.routers.cameras import create_camera, update_camera
from app.schemas.camera import CameraCreate, CameraUpdate
from app.services.recording_retention import (
    EXECUTION_POLICY_MANUAL_COMPLETE,
    build_retention_plan,
    execute_segments,
    run_automatic_retention_once,
)
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
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
        "manual_confirm_unverified": True,
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
    ensure_archive_roots(db)
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
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=datetime.utcnow(),
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


def test_manual_retention_complete_is_not_limited_by_legacy_automatic_byte_budget(db):
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
        policy=EXECUTION_POLICY_MANUAL_COMPLETE,
    )

    db.refresh(first)
    db.refresh(second)
    assert result["deleted_count"] == 2
    assert result["limit_exceeded"] is False
    assert first.status == "deleted"
    assert second.status == "deleted"
    assert not first_path.exists()
    assert not second_path.exists()


def test_automatic_retention_entry_point_uses_durable_coordinator_without_byte_budget():
    source = inspect.getsource(run_automatic_retention_once)

    assert "retention_automation" in source
    assert "claim_retention_signal" in source
    assert "max_bytes" not in source
    assert "oversized_single_segment_progress" not in source
