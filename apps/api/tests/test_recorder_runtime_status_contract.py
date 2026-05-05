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
from app.models.recording import RecordingJob
from app.services.recorder_diagnostics import _health_from, build_recorder_status
from app.services.recorder_runtime_status import list_camera_recording_states


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage201_recorder_status_")
    original_storage_root = settings.storage_root
    settings.storage_root = str(Path(tmp.name) / "storage")

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


def add_camera(db, *, name="stage201_camera", enabled=True, status="recording", last_error=None):
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
        segment_minutes=5,
        retention_days=1,
        storage_quota_gb=50,
        status=status,
        last_error=last_error,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


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
    return job


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
