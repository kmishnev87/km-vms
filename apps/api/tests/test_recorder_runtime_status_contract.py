import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.services import recorder_runtime_status
from app.services.recorder_diagnostics import _health_from, build_recorder_status
from app.services.recorder_runtime_status import list_camera_recording_states, stale_current_segment_after_seconds
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
)


RECORDER_INSTANCE_ID = "stage201-recorder"


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage201_recorder_status_")
    original_storage_root = settings.storage_root
    settings.storage_root = str(Path(tmp.name) / "storage")

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
    session.commit()
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


def add_camera(db, *, name="stage201_camera", enabled=True, status="recording", last_error=None, segment_minutes=5):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=enabled,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="user",
        password_encrypted=None,
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        segment_minutes=segment_minutes,
        retention_days=1,
        storage_quota_gb=50,
        status=status,
        last_error=last_error,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def set_heartbeat(db, *, active_jobs=1, recording_cameras=1):
    now = datetime.utcnow()
    db.execute(text("DELETE FROM recorder_runtime_status"))
    db.execute(
        text(
            """
            INSERT INTO recorder_runtime_status (
                recorder_instance_id, service_status, loop_state, started_at, heartbeat_at,
                active_jobs_count, recording_cameras_count, failed_cameras_count,
                last_error, last_exit_code, updated_at
            ) VALUES (
                :instance_id, 'healthy', 'running', :started_at, :heartbeat_at,
                :active_jobs, :recording_cameras, 0, NULL, NULL, :updated_at
            )
            """
        ),
        {
            "instance_id": RECORDER_INSTANCE_ID,
            "started_at": now - timedelta(minutes=5),
            "heartbeat_at": now,
            "active_jobs": active_jobs,
            "recording_cameras": recording_cameras,
            "updated_at": now,
        },
    )
    db.commit()


def add_job(
    db,
    camera,
    *,
    job_id,
    state,
    last_error=None,
    last_error_type=None,
    last_exit_code=None,
    updated_delta_seconds=0,
):
    now = datetime.utcnow() + timedelta(seconds=updated_delta_seconds)
    job = RecordingJob(
        id=job_id,
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        state=state,
        recorder_instance_id=RECORDER_INSTANCE_ID,
        started_at=now,
        updated_at=now,
        last_error=last_error,
        last_error_type=last_error_type,
        last_exit_code=last_exit_code,
        ffmpeg_pid=123 if state == "recording" else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    set_heartbeat(
        db,
        active_jobs=1 if state in recorder_runtime_status.ACTIVE_JOB_STATES else 0,
        recording_cameras=1 if state == "recording" else 0,
    )
    return job


def add_segment(db, camera, *, status, started_delta_seconds, duration_seconds=10, name="segment.mkv"):
    ensure_archive_roots(db)
    started_at = datetime.utcnow() + timedelta(seconds=started_delta_seconds)
    job = db.query(RecordingJob).filter(RecordingJob.camera_id == camera.id).order_by(RecordingJob.updated_at.desc()).first()
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id=job.id if job else None,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=f"/storage/redacted/{name}",
        relative_path=f"camera_{camera.id}/{name}",
        started_at=started_at,
        ended_at=None if status == "writing" else started_at + timedelta(seconds=duration_seconds),
        finalized_at=None if status == "writing" else started_at + timedelta(seconds=duration_seconds),
        duration_sec=0 if status == "writing" else duration_seconds,
        size_bytes=100,
        media_progress_at=datetime.utcnow() if status == "writing" else None,
        status=status,
        ownership="KM VMS",
        source="recorder",
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=datetime.utcnow(),
        integrity_status=status,
        reconciliation_status="pending" if status == "writing" else "ok_owned_finalized",
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def test_shared_recorder_runtime_status_import_boundary():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    core_dir = app_dir / "core"
    services_dir = Path(__file__).resolve().parents[1] / "app" / "services"
    main_source = (app_dir / "main.py").read_text(encoding="utf-8")
    system_runtime_source = (services_dir / "system_runtime_status.py").read_text(encoding="utf-8")
    shared_source = (services_dir / "recorder_runtime_status.py").read_text(encoding="utf-8")
    sanitization_source = (core_dir / "sanitization.py").read_text(encoding="utf-8")
    non_audit_consumers = [
        app_dir / "main.py",
        services_dir / "recorder_runtime_status.py",
        services_dir / "recording_reconciliation.py",
        services_dir / "recording_retention.py",
        services_dir / "storage_monitoring.py",
        app_dir / "routers" / "cameras.py",
    ]

    assert "app.services.recorder_diagnostics" not in system_runtime_source
    assert "system_runtime_status" not in shared_source
    assert "recorder_diagnostics" not in shared_source
    assert "app.services.audit_log import redact_text" not in main_source
    assert "app.services.audit_log" not in sanitization_source
    for path in non_audit_consumers:
        source = path.read_text(encoding="utf-8")
        assert "app.services.audit_log import redact_text" not in source
        assert "redact_text as audit_redact_text" not in source


def test_recording_job_with_stale_error_is_not_current_failure(db):
    camera = add_camera(db, last_error="old camera error")
    add_job(
        db,
        camera,
        job_id="stage201_recording_stale_error",
        state="recording",
        last_error="old job error",
        last_error_type="invalid_rtsp",
        last_exit_code=183,
    )

    state = list_camera_recording_states(db)[0]
    status = build_recorder_status(db)

    assert state["job_state"] == "recording"
    assert state["current_failure"] is False
    assert state["stale_error_ignored"] is True
    assert status["failed_cameras_count"] == 0
    assert "camera_recording_errors" not in status["health_reasons"]


def test_fresh_current_writing_segment_is_not_stale(db):
    camera = add_camera(db)
    add_job(db, camera, job_id="stage201_recording_fresh_segment", state="recording")
    add_segment(db, camera, status="writing", started_delta_seconds=-120, name="fresh.mkv")

    state = list_camera_recording_states(db)[0]

    assert state["current_failure"] is False
    assert state["stale_current_segment"] is False
    assert state["current_segment_age_seconds"] >= 120
    assert state["expected_segment_duration_seconds"] == 300
    assert state["stale_current_segment_after_seconds"] == 600
    assert state["recording_health"] == "recording"


@pytest.mark.parametrize(
    ("segment_minutes", "threshold_seconds"),
    [
        (5, 600),
        (30, 2100),
        (60, 3900),
        (120, 7500),
    ],
)
def test_strict_stale_current_segment_threshold_is_duration_plus_300(db, segment_minutes, threshold_seconds):
    camera = add_camera(db, segment_minutes=segment_minutes)

    assert stale_current_segment_after_seconds(camera) == threshold_seconds


@pytest.mark.parametrize(
    ("segment_minutes", "age_seconds", "expected_stale"),
    [
        (5, 599, False),
        (5, 601, True),
        (30, 2160, True),
        (30, 2700, True),
        (60, 3960, True),
        (120, 7800, True),
    ],
)
def test_current_writing_segment_strict_threshold_examples(db, segment_minutes, age_seconds, expected_stale):
    camera = add_camera(db, segment_minutes=segment_minutes)
    add_job(db, camera, job_id=f"stage201_threshold_{segment_minutes}_{age_seconds}", state="recording")
    add_segment(db, camera, status="writing", started_delta_seconds=-age_seconds, name=f"threshold_{segment_minutes}.mkv")

    state = list_camera_recording_states(db)[0]

    assert state["stale_current_segment_after_seconds"] == (segment_minutes * 60) + 300
    assert state["stale_current_segment"] is expected_stale
    assert state["recording_health"] == ("degraded" if expected_stale else "recording")


def test_product_local_naive_current_segment_age_uses_system_timezone(db, monkeypatch):
    add_settings(db, "Asia/Yekaterinburg")
    camera = add_camera(db, segment_minutes=30)
    add_job(db, camera, job_id="stage201_local_naive_age", state="recording")
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id="stage201_local_naive_age",
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path="/storage/redacted/Dahua-2026-05-14-11-24-00.mkv",
        relative_path="camera_1/Dahua-2026-05-14-11-24-00.mkv",
        started_at=datetime(2026, 5, 14, 11, 24, 0),
        ended_at=None,
        finalized_at=None,
        duration_sec=0,
        size_bytes=100,
        media_progress_at=datetime(2026, 5, 14, 6, 59, 30),
        status="writing",
        ownership="KM VMS",
        source="recorder",
        integrity_status="writing",
        reconciliation_status="pending",
    )
    db.add(segment)
    db.commit()
    monkeypatch.setattr(recorder_runtime_status, "utc_now", lambda: datetime(2026, 5, 14, 7, 0, 0))

    state = list_camera_recording_states(db)[0]

    assert state["current_segment_age_seconds"] == 2160
    assert state["stale_current_segment_after_seconds"] == 2100
    assert state["stale_current_segment"] is True
    assert state["recording_health"] == "degraded"


def test_stale_current_writing_segment_is_degraded_not_healthy(db):
    camera = add_camera(db)
    add_job(db, camera, job_id="stage201_recording_stale_segment", state="recording")
    add_segment(db, camera, status="writing", started_delta_seconds=-3900, name="stale.mkv")

    state = list_camera_recording_states(db)[0]

    assert state["current_failure"] is False
    assert state["stale_current_segment"] is True
    assert state["stale_current_segment_reason"] == "recording_segment_not_rotating"
    assert "recording_segment_not_rotating" in state["recording_health_reason_codes"]
    assert state["recording_health"] == "degraded"


def test_recorder_watchdog_source_uses_strict_duration_plus_300():
    source = (Path(__file__).resolve().parents[2] / "recorder" / "main.py").read_text(encoding="utf-8")

    assert "return segment_duration_seconds_for_row(row) + 300" in source
    assert "segment_seconds * 2" not in source
    assert "datetime.utcnow() - started_at" not in source


def test_restarting_job_remains_current_failure(db):
    camera = add_camera(db, status="error", last_error="current retry")
    add_job(
        db,
        camera,
        job_id="stage201_restarting_current_error",
        state="restarting",
        last_error="current retry",
        last_error_type="invalid_rtsp",
        last_exit_code=183,
    )

    state = list_camera_recording_states(db)[0]
    status = build_recorder_status(db)

    assert state["current_failure"] is True
    assert status["failed_cameras_count"] == 1
    assert "camera_recording_errors" in status["health_reasons"]


def test_newer_healthy_job_overrides_older_errored_job(db):
    camera = add_camera(db, last_error="old camera error")
    add_job(
        db,
        camera,
        job_id="stage201_old_error",
        state="error",
        last_error="old failure",
        last_error_type="invalid_rtsp",
        last_exit_code=183,
        updated_delta_seconds=-60,
    )
    add_job(
        db,
        camera,
        job_id="stage201_new_recording",
        state="recording",
        updated_delta_seconds=60,
    )

    state = list_camera_recording_states(db)[0]

    assert state["job_id"] == "stage201_new_recording"
    assert state["job_state"] == "recording"
    assert state["current_failure"] is False


def test_disabled_stale_error_is_not_current_failure(db):
    camera = add_camera(db, enabled=False, status="disabled", last_error="old disabled error")
    add_job(
        db,
        camera,
        job_id="stage201_disabled_stale_error",
        state="disabled",
        last_error="old disabled job error",
        last_exit_code=255,
    )

    state = list_camera_recording_states(db)[0]
    status = build_recorder_status(db)

    assert state["job_state"] == "disabled"
    assert state["current_failure"] is False
    assert state["stale_error_ignored"] is True
    assert status["failed_cameras_count"] == 0


def test_health_uses_current_failure_contract():
    health, reasons = _health_from(
        heartbeat={"available": True, "service_status": "healthy"},
        heartbeat_age_seconds=1,
        job_summary={"active_count": 1},
        camera_states=[
            {
                "enabled": True,
                "recording_mode": "always",
                "job_state": "recording",
                "last_error": "old error",
                "camera_last_error": "old camera error",
                "current_failure": False,
            }
        ],
        storage_state={"status": "available"},
    )

    assert health == "healthy"
    assert reasons == ["all_checks_passed"]
