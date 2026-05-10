import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import RecordingSegment
from app.models.system_settings import SystemSettings
from app.routers.chronology import chronology_playback, chronology_ranges
from app.routers.recordings import collect_recording_files
from app.services.recording_retention import build_retention_plan
from app.services.timezone_contract import (
    local_day_storage_bounds,
    parse_api_timestamp,
    retention_cutoff_storage,
    timezone_context,
)


def actor():
    return SimpleNamespace(id=1, username="stage12_user", role="owner", is_active=True)


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage12_timezone_")
    root = Path(tmp.name)
    original_storage_root = settings.storage_root
    settings.storage_root = str(root / "archive")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        tmp.cleanup()


def add_settings(db, timezone_name="Asia/Yekaterinburg"):
    row = SystemSettings(
        system_initialized=True,
        timezone=timezone_name,
        language="ru",
        storage_path=settings.storage_root,
        recording_format="mkv",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def add_camera(db):
    camera = Camera(
        name="stage12_timezone_camera",
        storage_folder_name="stage12_timezone_camera",
        enabled=False,
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


def add_segment(db, camera, started_at, *, name="stage12.mkv", write_file=True):
    rel = f"kmvms/recordings/{camera.storage_folder_name}/{name}"
    path = Path(settings.storage_root) / rel
    if write_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stage12")
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(path),
        relative_path=rel,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=10),
        duration_sec=600,
        size_bytes=7,
        stream_type="main",
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        finalized_at=started_at + timedelta(minutes=10),
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def test_timezone_utility_uses_system_settings_and_converts_naive_local_to_storage(db):
    add_settings(db, "Asia/Yekaterinburg")
    ctx = timezone_context(db)

    parsed = parse_api_timestamp("2026-05-10T10:00:00", ctx)
    aware = parse_api_timestamp("2026-05-10T10:00:00+05:00", ctx)

    assert ctx.name == "Asia/Yekaterinburg"
    assert parsed.storage_utc == datetime(2026, 5, 10, 5, 0, 0)
    assert parsed.compatibility_local == datetime(2026, 5, 10, 10, 0, 0)
    assert aware.storage_utc == datetime(2026, 5, 10, 5, 0, 0)
    assert aware.compatibility_local is None


def test_records_date_filter_uses_system_timezone_and_preserves_local_naive_compat(db):
    add_settings(db, "Asia/Yekaterinburg")
    camera = add_camera(db)
    storage_utc = add_segment(db, camera, datetime(2026, 5, 9, 19, 30), name="utc.mkv")
    local_compat = add_segment(db, camera, datetime(2026, 5, 10, 0, 30), name="local.mkv")
    add_segment(db, camera, datetime(2026, 5, 8, 18, 59), name="outside.mkv")

    items = collect_recording_files(db, date_value="2026-05-10")
    names = {item["filename"] for item in items}

    assert names == {Path(storage_utc.relative_path).name, Path(local_compat.relative_path).name}
    assert all(item["display_timezone"] == "Asia/Yekaterinburg" for item in items)
    assert any(item["started_at_system"].endswith("+05:00") for item in items)


def test_records_display_preserves_existing_local_naive_filename_timestamp(db):
    add_settings(db, "Asia/Yekaterinburg")
    camera = add_camera(db)
    add_segment(
        db,
        camera,
        datetime(2026, 5, 10, 10, 53, 25),
        name="stage12-camera-2026-05-10-10-53-25.mkv",
    )

    item = collect_recording_files(db, date_value="2026-05-10")[0]

    assert item["created_at"] == "10.05.2026, 10:53:25"
    assert item["started_at_system"] == "2026-05-10T10:53:25+05:00"
    assert "15:53:25" not in item["created_at"]
    assert "15:53:25" not in item["started_at_system"]
    assert item["timestamp_display_semantic"] == "product_local_naive"


def test_records_display_converts_utc_naive_canonical_row_to_system_timezone(db):
    add_settings(db, "Asia/Yekaterinburg")
    camera = add_camera(db)
    add_segment(db, camera, datetime(2026, 5, 10, 5, 53, 25), name="stage12-utc-canonical.mkv")

    item = collect_recording_files(db, date_value="2026-05-10")[0]

    assert item["created_at"] == "10.05.2026, 10:53:25"
    assert item["started_at_system"] == "2026-05-10T10:53:25+05:00"
    assert item["timestamp_display_semantic"] == "storage_utc_naive"


def test_chronology_naive_and_offset_aware_inputs_find_existing_segments(db):
    add_settings(db, "Asia/Yekaterinburg")
    camera = add_camera(db)
    add_segment(db, camera, datetime(2026, 5, 10, 10, 0), name="stage12-2026-05-10-10-00-00.mkv")

    naive = chronology_playback(camera_id=camera.id, ts="2026-05-10T10:05:00", db=db, current_user=actor())
    aware = chronology_playback(camera_id=camera.id, ts="2026-05-10T10:05:00+05:00", db=db, current_user=actor())
    ranges = chronology_ranges(
        camera_ids=str(camera.id),
        from_ts="2026-05-10T10:00:00",
        to_ts="2026-05-10T10:15:00",
        db=db,
        current_user=actor(),
    )

    assert naive["has_video"] is True
    assert naive["file_start_system"] == "2026-05-10T10:00:00+05:00"
    assert naive["timestamp_display_semantic"] == "product_local_naive"
    assert aware["has_video"] is False
    assert ranges["timezone"]["id"] == "Asia/Yekaterinburg"
    first_range = ranges["items"][str(camera.id)]["ranges"][0]
    assert first_range["start_system"] == "2026-05-10T10:00:00+05:00"
    assert first_range["timestamp_display_semantic"] == "product_local_naive"


def test_chronology_offset_aware_input_finds_utc_storage_segment(db):
    add_settings(db, "Asia/Yekaterinburg")
    camera = add_camera(db)
    add_segment(db, camera, datetime(2026, 5, 10, 5, 0), name="utc_existing.mkv")

    result = chronology_playback(camera_id=camera.id, ts="2026-05-10T10:05:00+05:00", db=db, current_user=actor())
    ranges = chronology_ranges(
        camera_ids=str(camera.id),
        from_ts="2026-05-10T10:00:00+05:00",
        to_ts="2026-05-10T10:15:00+05:00",
        db=db,
        current_user=actor(),
    )

    assert result["has_video"] is True
    assert result["offset_sec"] == 300
    assert result["display_timezone"] == "Asia/Yekaterinburg"
    assert result["file_start_system"] == "2026-05-10T10:00:00+05:00"
    assert result["timestamp_display_semantic"] == "storage_utc_naive"
    first_range = ranges["items"][str(camera.id)]["ranges"][0]
    assert first_range["start_system"] == "2026-05-10T10:00:00+05:00"
    assert first_range["timestamp_display_semantic"] == "storage_utc_naive"


def test_retention_cutoff_uses_system_timezone_calendar_day_dst_and_non_dst(db):
    berlin = timezone_context(None).__class__("Europe/Berlin", ZoneInfo("Europe/Berlin"))
    yeka = timezone_context(None).__class__("Asia/Yekaterinburg", ZoneInfo("Asia/Yekaterinburg"))

    spring_cutoff, spring_local = retention_cutoff_storage(1, berlin, now_utc=datetime(2026, 3, 29, 10, 0))
    fall_cutoff, fall_local = retention_cutoff_storage(1, berlin, now_utc=datetime(2026, 10, 25, 10, 0))
    yeka_cutoff, yeka_local = retention_cutoff_storage(1, yeka, now_utc=datetime(2026, 5, 10, 5, 0))

    assert spring_local == datetime(2026, 3, 28, 0, 0)
    assert spring_cutoff == datetime(2026, 3, 27, 23, 0)
    assert fall_local == datetime(2026, 10, 24, 0, 0)
    assert fall_cutoff == datetime(2026, 10, 23, 22, 0)
    assert yeka_local == datetime(2026, 5, 9, 0, 0)
    assert yeka_cutoff == datetime(2026, 5, 8, 19, 0)


def test_retention_plan_marks_old_segment_by_system_local_day(db):
    add_settings(db, "Asia/Yekaterinburg")
    camera = add_camera(db)
    old = add_segment(db, camera, datetime.utcnow() - timedelta(days=3), name="old.mkv")
    add_segment(db, camera, datetime.utcnow(), name="new.mkv")

    plan = build_retention_plan(db, camera_id=camera.id)
    reasons = {item["segment_id"]: item["reason"] for item in plan["items"]}

    assert reasons[old.id] == "retention_days"
    assert plan["policy"]["days"][str(camera.id)]["boundary"] == "system_timezone_calendar_day"
