import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.routers.recordings import collect_camera_names, collect_recording_files
from app.services.storage_monitoring import build_storage_monitoring_summary


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage10_deleted_camera_visibility_")
    root = Path(tmp.name)
    original_storage_root = settings.storage_root
    original_storage_previews = settings.storage_previews
    settings.storage_root = str(root / "archive")
    settings.storage_previews = str(root / "previews")

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


def add_camera(db, *, name, storage_folder_name=None, deleted=False, enabled=True):
    camera = Camera(
        name=name,
        storage_folder_name=storage_folder_name or name,
        enabled=enabled,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        status="deleted" if deleted else "enabled",
        deleted_at=datetime.utcnow() if deleted else None,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def write_storage_file(relative_path: str, content: bytes = b"video") -> None:
    path = Path(settings.storage_root) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def add_segment(
    db,
    camera,
    *,
    name="segment.mkv",
    snapshot=None,
    folder_snapshot=None,
    write_file=True,
):
    relative_path = f"kmvms/recordings/{camera.storage_folder_name}/{name}"
    if write_file:
        write_storage_file(relative_path)
    started = datetime.utcnow() - timedelta(minutes=5)
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=snapshot if snapshot is not None else camera.name,
        camera_folder_snapshot=folder_snapshot if folder_snapshot is not None else camera.storage_folder_name,
        file_path=relative_path,
        relative_path=relative_path,
        started_at=started,
        ended_at=started + timedelta(seconds=10),
        duration_sec=10,
        size_bytes=12345,
        stream_type="main",
        status="finalized",
        ownership="KM VMS",
        source="recorder",
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


def technical_deleted_name(camera_id=77):
    return f"stage10_old_name__deleted_{camera_id}_1777777777"


def test_recordings_cameras_excludes_soft_deleted_technical_rows_and_keeps_active(db):
    active = add_camera(db, name="Active Camera")
    deleted = add_camera(
        db,
        name=technical_deleted_name(),
        storage_folder_name="stage10_folder__deleted_77_1777777777",
        deleted=True,
        enabled=False,
    )
    add_segment(db, active, snapshot="Active Camera")
    add_segment(db, deleted, snapshot=deleted.name)

    names = collect_camera_names(db)

    assert "Active Camera" in names
    assert deleted.name not in names
    assert not any("__deleted_" in name for name in names)


def test_recordings_cameras_keeps_safe_historical_snapshot_for_retained_deleted_recordings(db):
    deleted = add_camera(
        db,
        name=technical_deleted_name(88),
        storage_folder_name="stage10_deleted_folder__deleted_88_1777777777",
        deleted=True,
        enabled=False,
    )
    add_segment(db, deleted, snapshot="Lobby archive", folder_snapshot="Lobby archive")

    names = collect_camera_names(db)
    items = collect_recording_files(db)

    assert names == ["Lobby archive"]
    assert items[0]["camera"] == "Lobby archive"
    assert "__deleted_" not in items[0]["camera"]


def test_stale_deleted_records_filter_falls_back_to_all_recordings(db):
    active = add_camera(db, name="Active Camera")
    deleted = add_camera(
        db,
        name=technical_deleted_name(89),
        storage_folder_name="stage10_deleted_folder__deleted_89_1777777777",
        deleted=True,
        enabled=False,
    )
    active_segment = add_segment(db, active, name="active.mkv", snapshot="Active Camera")
    add_segment(db, deleted, name="deleted.mkv", snapshot="Deleted archive")

    all_items = collect_recording_files(db)
    stale_items = collect_recording_files(db, camera_name=deleted.name)

    assert len(all_items) == 2
    assert [item["path"] for item in stale_items] == [item["path"] for item in all_items]
    assert active_segment.relative_path in {item["path"] for item in stale_items}


def test_storage_monitoring_excludes_soft_deleted_zero_usage_camera_and_keeps_active_zero_usage(db):
    active = add_camera(db, name="Active Zero Usage")
    deleted = add_camera(
        db,
        name=technical_deleted_name(90),
        storage_folder_name="stage10_deleted_folder__deleted_90_1777777777",
        deleted=True,
        enabled=False,
    )

    summary = build_storage_monitoring_summary(db, include_namespace_observations=False)
    rows = summary["storage_operations"]["per_camera_usage"]
    names = [row["camera_name"] for row in rows]

    assert active.name in names
    assert deleted.name not in names
    assert not any("__deleted_" in name for name in names if name)


def test_storage_monitoring_deleted_camera_with_real_usage_uses_safe_snapshot_label(db):
    deleted = add_camera(
        db,
        name=technical_deleted_name(91),
        storage_folder_name="stage10_deleted_folder__deleted_91_1777777777",
        deleted=True,
        enabled=False,
    )
    add_segment(db, deleted, snapshot="Warehouse archive", folder_snapshot="Warehouse archive")

    summary = build_storage_monitoring_summary(db, include_namespace_observations=False)
    rows = summary["storage_operations"]["per_camera_usage"]

    assert len(rows) == 1
    assert rows[0]["camera_name"] == "Warehouse archive"
    assert rows[0]["segment_count"] == 1
    assert rows[0]["existing_file_count"] == 1
    assert "__deleted_" not in str(rows)


def test_storage_monitoring_deleted_camera_with_only_technical_usage_uses_generic_safe_label(db):
    deleted = add_camera(
        db,
        name=technical_deleted_name(92),
        storage_folder_name="stage10_deleted_folder__deleted_92_1777777777",
        deleted=True,
        enabled=False,
    )
    add_segment(db, deleted, snapshot=deleted.name, folder_snapshot=deleted.storage_folder_name)

    summary = build_storage_monitoring_summary(db, include_namespace_observations=False)
    rows = summary["storage_operations"]["per_camera_usage"]

    assert len(rows) == 1
    assert rows[0]["camera_name"] == "Удалённая камера"
    assert rows[0]["segment_count"] == 1
    assert "__deleted_" not in str(rows)
