import json
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.routers import settings as settings_router
from app.routers.audit import audit_events
from app.services.timezone_contract import utc_now_storage
from app.services.recorder_diagnostics import build_recorder_status
from app.services.system_runtime_status import build_operator_runtime_status
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
)


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage12_stage3_")
    original_storage_root = settings.storage_root
    settings.storage_root = str(Path(tmp.name) / "archive")
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    session.execute(
        text(
            """
            CREATE TABLE recorder_runtime_status (
                recorder_instance_id TEXT,
                service_status TEXT,
                loop_state TEXT,
                started_at DATETIME,
                heartbeat_at DATETIME,
                active_jobs_count INTEGER,
                recording_cameras_count INTEGER,
                failed_cameras_count INTEGER,
                last_error TEXT,
                last_exit_code INTEGER,
                updated_at DATETIME
            )
            """
        )
    )
    session.add(SystemSettings(system_initialized=True, timezone="Asia/Yekaterinburg", language="ru", storage_path=settings.storage_root))
    session.commit()
    ensure_archive_roots(session)
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        tmp.cleanup()


def actor():
    return User(id=1, username="stage12_stage3_owner", role="owner", is_active=True)


def call_audit(db, **filters):
    params = {
        "limit": 50,
        "offset": 0,
        "category": None,
        "severity": None,
        "event_type": None,
        "actor": None,
        "target": None,
        "target_type": None,
        "target_id": None,
        "date_from": None,
        "date_to": None,
        "since_minutes": None,
        "q": None,
        "db": db,
        "current_user": actor(),
    }
    params.update(filters)
    return audit_events(**params)


def add_audit_event(db, created_at):
    row = AuditEvent(
        id=f"stage12-stage3-{created_at.hour}",
        created_at=created_at,
        actor_username="stage12",
        actor_role="owner",
        category="diagnostics",
        event_type="diagnostics.stage12_stage3",
        severity="info",
        message_ru="stage12 stage3",
        message_en="stage12 stage3",
        event_metadata={},
    )
    db.add(row)
    db.commit()
    return row


def add_camera(db):
    camera = Camera(
        name="stage12_stage3_camera",
        storage_folder_name="stage12_stage3_camera",
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=1,
        storage_quota_gb=50,
        status="recording",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def add_runtime_rows(
    db,
    camera,
    *,
    now=datetime(2026, 5, 10, 5, 0, 0),
    segment_started_at=None,
    segment_ended_at=None,
    segment_finalized_at=None,
    segment_name="stage12_stage3_camera/file.mkv",
):
    segment_started_at = segment_started_at or (now - timedelta(minutes=1))
    segment_ended_at = segment_ended_at if segment_ended_at is not None else now
    segment_finalized_at = segment_finalized_at if segment_finalized_at is not None else now
    db.execute(
        text(
            """
            INSERT INTO recorder_runtime_status (
                recorder_instance_id, service_status, loop_state, started_at, heartbeat_at,
                active_jobs_count, recording_cameras_count, failed_cameras_count,
                last_error, last_exit_code, updated_at
            ) VALUES (
                'stage12-stage3-recorder', 'healthy', 'running', :started_at, :heartbeat_at,
                1, 1, 0, NULL, NULL, :updated_at
            )
            """
        ),
        {
            "started_at": now - timedelta(minutes=5),
            "heartbeat_at": now,
            "updated_at": now,
        },
    )
    db.add(
        RecordingJob(
            id="stage12_stage3_job",
            camera_id=camera.id,
            camera_name_snapshot=camera.name,
            camera_folder_snapshot=camera.storage_folder_name,
            state="recording",
            recorder_instance_id="stage12-stage3-recorder",
            source_stream="main",
            started_at=now,
            updated_at=now,
            ffmpeg_pid=123,
        )
    )
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id="stage12_stage3_job",
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(Path(settings.storage_root) / segment_name),
        relative_path=segment_name,
        started_at=segment_started_at,
        ended_at=segment_ended_at,
        finalized_at=segment_finalized_at,
        duration_sec=60,
        size_bytes=10,
        media_progress_at=now if segment_ended_at is None else None,
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=now,
    )
    db.add(segment)
    db.commit()


def test_audit_events_expose_system_timezone_and_filter_naive_as_system_local(db):
    matching = add_audit_event(db, datetime(2026, 5, 10, 5, 0, 0))
    add_audit_event(db, datetime(2026, 5, 10, 6, 0, 0))

    naive = call_audit(db, date_from="2026-05-10T10:00:00", date_to="2026-05-10T10:00:30")
    aware = call_audit(db, date_from="2026-05-10T05:00:00Z", date_to="2026-05-10T05:00:30Z")

    assert [item["id"] for item in naive["items"]] == [matching.id]
    assert [item["id"] for item in aware["items"]] == [matching.id]
    item = naive["items"][0]
    assert item["created_at"] == "2026-05-10T05:00:00Z"
    assert item["created_at_utc"] == "2026-05-10T05:00:00Z"
    assert item["created_at_system"] == "2026-05-10T10:00:00+05:00"
    assert naive["timezone"]["id"] == "Asia/Yekaterinburg"


def test_audit_events_invalid_and_reversed_filters_remain_422(db):
    with pytest.raises(HTTPException) as invalid:
        call_audit(db, date_from="not-a-date")
    assert invalid.value.status_code == 422

    with pytest.raises(HTTPException) as reversed_range:
        call_audit(db, date_from="2026-05-10T11:00:00", date_to="2026-05-10T10:00:00")
    assert reversed_range.value.status_code == 422


def test_recorder_and_runtime_status_include_utc_and_system_timestamp_fields(db, monkeypatch):
    camera = add_camera(db)
    add_runtime_rows(db, camera)
    monkeypatch.setattr("app.services.system_runtime_status.live_manager.status", lambda: [])

    recorder = build_recorder_status(db)
    runtime = build_operator_runtime_status(db)

    assert recorder["timezone"]["id"] == "Asia/Yekaterinburg"
    assert recorder["generated_at_utc"].endswith("Z")
    assert recorder["generated_at_system"].endswith("+05:00")
    assert recorder["heartbeat"]["heartbeat_at"] == "2026-05-10T05:00:00Z"
    assert recorder["heartbeat"]["heartbeat_at_system"] == "2026-05-10T10:00:00+05:00"
    assert recorder["last_segment_time_system"] == "2026-05-10T10:00:00+05:00"

    assert runtime["timezone"]["id"] == "Asia/Yekaterinburg"
    assert runtime["generated_at_system"].endswith("+05:00")
    assert runtime["domains"]["recorder"]["summary"]["last_segment_time_system"] == "2026-05-10T10:00:00+05:00"
    assert runtime["domains"]["cameras"]["items"][0]["last_segment_time_system"] == "2026-05-10T10:00:00+05:00"
    assert runtime["domains"]["storage"]["last_checked_at_system"].endswith("+05:00")


def test_segment_diagnostics_preserve_local_naive_filename_display(db, monkeypatch):
    camera = add_camera(db)
    local_started = datetime(2026, 5, 10, 10, 53, 26)
    add_runtime_rows(
        db,
        camera,
        now=datetime(2026, 5, 10, 5, 0, 0),
        segment_started_at=local_started,
        segment_ended_at=local_started,
        segment_finalized_at=local_started,
        segment_name="stage12_stage3_camera/Dahua_8MP_Panorama-2026-05-10-10-53-26.mkv",
    )
    monkeypatch.setattr("app.services.system_runtime_status.live_manager.status", lambda: [])

    recorder = build_recorder_status(db)
    runtime = build_operator_runtime_status(db)
    rendered = json.dumps({"recorder": recorder, "runtime": runtime}, ensure_ascii=False)

    assert recorder["last_segment_time_system"] == "2026-05-10T10:53:26+05:00"
    assert recorder["last_segment_time_display_semantic"] == "product_local_naive"
    assert recorder["segment_summary"]["last_segment"]["started_at_system"] == "2026-05-10T10:53:26+05:00"
    assert recorder["segment_summary"]["last_segment"]["timestamp_display_semantic"] == "product_local_naive"
    assert runtime["domains"]["recorder"]["summary"]["last_segment_time_system"] == "2026-05-10T10:53:26+05:00"
    assert runtime["domains"]["recorder"]["summary"]["last_segment_time_display_semantic"] == "product_local_naive"
    assert runtime["domains"]["cameras"]["items"][0]["last_segment_time_system"] == "2026-05-10T10:53:26+05:00"
    assert runtime["domains"]["cameras"]["items"][0]["last_segment_time_display_semantic"] == "product_local_naive"
    assert "2026-05-10T15:53:26+05:00" not in rendered


def test_diagnostic_archive_contains_timezone_metadata_and_audit_system_fields(db, monkeypatch):
    event = add_audit_event(db, datetime(2026, 5, 10, 5, 0, 0))

    monkeypatch.setattr(settings_router, "storage_diagnostics", lambda: {"status": "ok"})
    monkeypatch.setattr(settings_router, "build_storage_monitoring_summary", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "reconciliation_diagnostics", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "retention_diagnostics", lambda db: {"status": "ok"})
    monkeypatch.setattr(settings_router, "build_recorder_archive_payloads", lambda db: {})
    monkeypatch.setattr(settings_router, "get_hardware_capabilities", lambda: {"available_backends": []})
    monkeypatch.setattr(settings_router, "camera_diagnostics", lambda db: [])
    monkeypatch.setattr(settings_router.live_manager, "status", lambda: [])
    monkeypatch.setattr(settings_router.live_manager, "debug", lambda: {})
    monkeypatch.setattr(settings_router, "recordings_diagnostics", lambda db: {"count": 0})
    monkeypatch.setattr(settings_router, "chronology_diagnostics", lambda db: {"items": []})
    monkeypatch.setattr(settings_router, "list_events", lambda db, **kwargs: [event])
    monkeypatch.setattr(settings_router, "utc_now_storage", lambda: utc_now_storage())

    archive = settings_router.build_log_archive(db, mode="normal", include_logs=False)
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("system/manifest.json").decode("utf-8"))
        timezone = json.loads(bundle.read("system/timezone.json").decode("utf-8"))
        audit_items = json.loads(bundle.read("audit/events_recent.json").decode("utf-8"))
        audit_summary = json.loads(bundle.read("audit/summary.json").decode("utf-8"))

    assert manifest["timezone"]["id"] == "Asia/Yekaterinburg"
    assert manifest["created_at_utc"].endswith("Z")
    assert manifest["created_at_system"].endswith("+05:00")
    assert timezone["filename_timestamp_semantic"].startswith("UTC safe archive filename")
    assert audit_items[0]["created_at_system"] == "2026-05-10T10:00:00+05:00"
    assert audit_summary["timezone"]["system_timezone"] == "Asia/Yekaterinburg"
