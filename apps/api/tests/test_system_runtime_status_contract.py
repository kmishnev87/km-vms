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
from app.routers.settings import system_status
from app.services import system_runtime_status as runtime_status
from app.services.system_runtime_status import build_operator_runtime_status


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage20_runtime_status_")
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


@pytest.fixture(autouse=True)
def no_live_evidence(monkeypatch):
    monkeypatch.setattr(runtime_status.live_manager, "status", lambda: [])


def add_camera(
    db,
    *,
    name="stage20_camera",
    enabled=True,
    status="recording",
    last_error=None,
    recording_mode="always",
):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=enabled,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="user",
        password_encrypted="encrypted-secret",
        rtsp_main_url="rtsp://user:credential@example.test/main",
        rtsp_sub_url="rtsp://user:credential@example.test/sub",
        recording_mode=recording_mode,
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


def add_job(db, camera, *, job_id="job", state="recording", last_error=None, updated_delta_seconds=0):
    now = datetime.utcnow() + timedelta(seconds=updated_delta_seconds)
    job = RecordingJob(
        id=job_id,
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        state=state,
        source_stream="main",
        started_at=now,
        updated_at=now,
        last_error=last_error,
        ffmpeg_pid=123 if state == "recording" else None,
    )
    db.add(job)
    db.commit()
    return job


def add_segment(db, camera, *, age_seconds=30):
    now = datetime.utcnow()
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path="/storage/archive/redacted/file.mkv",
        relative_path="camera/file.mkv",
        started_at=now - timedelta(seconds=age_seconds + 10),
        ended_at=now - timedelta(seconds=age_seconds),
        finalized_at=now - timedelta(seconds=age_seconds),
        duration_sec=10,
        size_bytes=100,
        status="ready",
    )
    db.add(segment)
    db.commit()
    return segment


def add_heartbeat(db, *, service_status="healthy", age_seconds=1, active_jobs=1, recording_cameras=1, failed_cameras=0):
    now = datetime.utcnow()
    db.execute(
        text(
            """
            INSERT INTO recorder_runtime_status (
                recorder_instance_id, service_status, loop_state, started_at, heartbeat_at,
                active_jobs_count, recording_cameras_count, failed_cameras_count,
                last_error, last_exit_code, updated_at
            ) VALUES (
                'stage20-recorder', :service_status, 'running', :started_at, :heartbeat_at,
                :active_jobs, :recording_cameras, :failed_cameras, NULL, NULL, :updated_at
            )
            """
        ),
        {
            "service_status": service_status,
            "started_at": now - timedelta(minutes=5),
            "heartbeat_at": now - timedelta(seconds=age_seconds),
            "active_jobs": active_jobs,
            "recording_cameras": recording_cameras,
            "failed_cameras": failed_cameras,
            "updated_at": now - timedelta(seconds=age_seconds),
        },
    )
    db.commit()


def rendered(payload):
    return str(payload)


def test_public_system_status_remains_setup_only(db):
    payload = system_status(db)

    assert payload["setup_required"] is True
    assert set(payload) <= {"initialized", "setup_required", "language", "timezone", "runtime"}
    assert payload["runtime"] == {"available": False, "setup_required": True}
    forbidden = {"cameras", "live", "recorder", "storage", "retention", "reconciliation", "diagnostics", "hardware", "database"}
    assert forbidden.isdisjoint(payload)


def test_runtime_status_domains_summary_and_disabled_camera_not_error(db):
    add_camera(db, name="disabled", enabled=False, status="disabled", last_error="old disabled rtsp://user:secret@host")
    payload = build_operator_runtime_status(db)
    camera_item = payload["domains"]["cameras"]["items"][0]

    assert set(payload["domains"]) == {"cameras", "live", "recorder"}
    assert payload["severity"] in {"ok", "warning", "error", "unknown"}
    assert camera_item["severity"] == "ok"
    assert camera_item["reason_codes"] == ["disabled"]
    assert payload["domains"]["cameras"]["summary"]["disabled_count"] == 1
    assert "secret" not in rendered(payload)
    assert "rtsp://" not in rendered(payload)


def test_healthy_current_recorder_job_ignores_stale_old_error_and_excludes_debug_fields(db, monkeypatch):
    camera = add_camera(db, last_error="old camera secret")
    add_job(db, camera, state="recording", last_error="old job secret")
    add_segment(db, camera, age_seconds=10)
    add_heartbeat(db)
    monkeypatch.setattr(
        runtime_status.live_manager,
        "status",
        lambda: [
            {
                "camera_id": camera.id,
                "stream": "main",
                "status": "ready",
                "ready": True,
                "running": True,
                "viewers": 2,
                "viewer_ids": ["viewer-secret"],
                "viewer_sessions": [{"id": "viewer-secret"}],
                "command": "ffmpeg -i rtsp://user:secret@example.test/live",
                "playlist_path": "/secret/path/index.m3u8",
                "stderr_tail": "secret stderr",
            }
        ],
    )

    payload = build_operator_runtime_status(db)
    camera_item = payload["domains"]["cameras"]["items"][0]

    assert payload["domains"]["recorder"]["severity"] == "ok"
    assert camera_item["recording_state"] == "recording"
    assert camera_item["severity"] == "ok"
    assert payload["domains"]["live"]["summary"]["viewer_count"] == 2
    forbidden = ["viewer-secret", "ffmpeg", "rtsp://", "secret", "playlist_path", "stderr_tail", "command"]
    assert all(value not in rendered(payload) for value in forbidden)


def test_enabled_always_camera_without_active_recorder_evidence_is_warning(db):
    add_camera(db)

    payload = build_operator_runtime_status(db)
    camera_item = payload["domains"]["cameras"]["items"][0]

    assert camera_item["severity"] == "warning"
    assert "no_evidence" in camera_item["reason_codes"]
    assert payload["domains"]["cameras"]["summary"]["warning_count"] == 1


def test_stale_recorder_heartbeat_maps_to_error_when_jobs_claim_active(db):
    camera = add_camera(db)
    add_job(db, camera, state="recording")
    add_heartbeat(db, age_seconds=runtime_status.HEARTBEAT_STALE_SECONDS + 30, active_jobs=1)

    payload = build_operator_runtime_status(db)

    assert payload["domains"]["recorder"]["severity"] == "error"
    assert "recorder_heartbeat_stale" in payload["domains"]["recorder"]["safe_reason_codes"]


def test_live_failed_state_maps_to_safe_reason_code_without_raw_error(db, monkeypatch):
    camera = add_camera(db)
    add_heartbeat(db, active_jobs=0, recording_cameras=0)
    monkeypatch.setattr(
        runtime_status.live_manager,
        "status",
        lambda: [
            {
                "camera_id": camera.id,
                "stream": "main",
                "status": "failed",
                "ready": False,
                "running": False,
                "viewers": 0,
                "last_error": "rtsp://user:super-secret@example.test/live unreachable",
            }
        ],
    )

    payload = build_operator_runtime_status(db)
    live_item = payload["domains"]["live"]["items"][0]

    assert live_item["severity"] == "error"
    assert "live_failed" in live_item["reason_codes"]
    assert "camera_unreachable" in live_item["reason_codes"]
    assert live_item["safe_failure_reason"] == "camera_unreachable"
    assert "super-secret" not in rendered(payload)
    assert "rtsp://" not in rendered(payload)
