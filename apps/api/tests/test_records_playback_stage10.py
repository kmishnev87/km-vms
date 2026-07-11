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
from app.models.recording import RecordingSegment
from app.models.user import User
from app.routers.recordings import (
    RecordingMediaTokenRequest,
    collect_recording_files,
    download_recording,
    issue_recording_media_token,
    recording_media_resource_for_segment,
    stream_recording,
)
from app.services.media_tokens import create_media_token
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
)


class FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage10_records_playback_")
    root = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    settings.storage_root = str(root / "archive")
    settings.storage_previews = str(root / "previews")
    Path(settings.storage_root, "kmvms", "recordings").mkdir(parents=True, exist_ok=True)

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
        tmp.cleanup()


def add_user(db, *, role="operator"):
    user = User(username=f"stage10_{role}", full_name=f"stage10_{role}", password_hash="hash", role=role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def add_camera(db):
    camera = Camera(
        name="stage10_records_camera",
        storage_folder_name="stage10_records_camera",
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def add_segment(db, camera, *, name="stage10_missing_file.mkv", write_file=False):
    ensure_archive_roots(db)
    rel = f"kmvms/recordings/{camera.storage_folder_name}/{name}"
    path = Path(settings.storage_root) / rel
    if write_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stage10-video")
    started = datetime.utcnow() - timedelta(minutes=5)
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=rel,
        relative_path=rel,
        started_at=started,
        ended_at=started + timedelta(seconds=10),
        duration_sec=10,
        size_bytes=12345,
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
        finalized_at=started + timedelta(seconds=10),
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def test_stage10_missing_file_list_marks_recording_unavailable(db):
    camera = add_camera(db)
    missing = add_segment(db, camera, write_file=False)

    items = collect_recording_files(db, verify_files=True)

    assert len(items) == 1
    item = items[0]
    assert item["path"] == missing.relative_path
    assert item["available"] is False
    assert item["playback_available"] is False
    assert item["download_available"] is False
    assert item["availability_status"] == "missing"
    assert settings.storage_root not in str(item)


def test_stage10_missing_file_media_token_stream_and_download_fail_safely(db):
    user = add_user(db, role="operator")
    camera = add_camera(db)
    missing = add_segment(db, camera, write_file=False)

    with pytest.raises(HTTPException) as token_exc:
        issue_recording_media_token(
            RecordingMediaTokenRequest(path=missing.relative_path, action="stream"),
            db=db,
            current_user=user,
        )
    assert token_exc.value.status_code == 404
    assert token_exc.value.detail == "Recording file not found"

    stream_token, _ = create_media_token(
        user=user,
        scope="recording",
        resource=recording_media_resource_for_segment(missing, "stream"),
    )
    with pytest.raises(HTTPException) as stream_exc:
        stream_recording(FakeRequest(), path=missing.relative_path, media_token=stream_token, db=db)
    assert stream_exc.value.status_code == 404
    assert stream_exc.value.detail == "Recording file not found"

    download_token, _ = create_media_token(
        user=user,
        scope="recording",
        resource=recording_media_resource_for_segment(missing, "download"),
    )
    with pytest.raises(HTTPException) as download_exc:
        download_recording(FakeRequest(), path=missing.relative_path, media_token=download_token, db=db)
    assert download_exc.value.status_code == 404
    assert download_exc.value.detail == "Recording file not found"


def test_stage10_recording_stream_and_download_tokens_remain_action_bound(db):
    user = add_user(db, role="operator")
    camera = add_camera(db)
    segment = add_segment(db, camera, name="stage10_present_file.mkv", write_file=True)

    stream_token, _ = create_media_token(
        user=user,
        scope="recording",
        resource=recording_media_resource_for_segment(segment, "stream"),
    )
    download_token, _ = create_media_token(
        user=user,
        scope="recording",
        resource=recording_media_resource_for_segment(segment, "download"),
    )

    assert stream_recording(FakeRequest(), path=segment.relative_path, media_token=stream_token, db=db).status_code == 200
    assert download_recording(FakeRequest(), path=segment.relative_path, media_token=download_token, db=db).status_code == 200

    with pytest.raises(HTTPException) as wrong_stream:
        stream_recording(FakeRequest(), path=segment.relative_path, media_token=download_token, db=db)
    assert wrong_stream.value.status_code == 403

    with pytest.raises(HTTPException) as wrong_download:
        download_recording(FakeRequest(), path=segment.relative_path, media_token=stream_token, db=db)
    assert wrong_download.value.status_code == 403
