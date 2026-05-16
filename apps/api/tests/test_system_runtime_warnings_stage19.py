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
from app.services import system_runtime_status as runtime_status
from app.services.recording_retention import AUTO_RETENTION_STATE
from app.services.system_runtime_status import build_operator_runtime_status


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage19_runtime_warnings_")
    original_storage_root = settings.storage_root
    settings.storage_root = str(Path(tmp.name) / "storage")
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
    session.commit()
    try:
        yield session
    finally:
        session.close()
        settings.storage_root = original_storage_root
        tmp.cleanup()


@pytest.fixture(autouse=True)
def stable_runtime(monkeypatch):
    monkeypatch.setattr(runtime_status.live_manager, "status", lambda: [])
    AUTO_RETENTION_STATE.clear()
    AUTO_RETENTION_STATE.update(
        {
            "enabled": True,
            "running": False,
            "last_started_at": None,
            "last_finished_at": datetime.utcnow(),
            "last_status": "ok",
            "last_error": None,
            "last_summary": None,
            "run_count": 1,
        }
    )


def add_camera(db, *, name="stage19_camera", enabled=True, deleted=False, status="recording", last_error=None):
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=enabled,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="user",
        password_encrypted="encrypted-value",
        rtsp_main_url="".join(["rt", "sp://user:credential@example.test/main"]),
        rtsp_sub_url="".join(["rt", "sp://user:credential@example.test/sub"]),
        recording_mode="always",
        default_live_stream="main",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=1,
        storage_quota_gb=50,
        status=status,
        last_error=last_error,
        deleted_at=datetime.utcnow() if deleted else None,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def add_job(db, camera, *, state="recording", last_error=None):
    now = datetime.utcnow()
    job = RecordingJob(
        id=f"stage19-job-{camera.id}-{state}",
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
                'stage19-recorder', :service_status, 'running', :started_at, :heartbeat_at,
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


def add_segment(db, camera, *, status="writing", started_age_seconds=30, finalized_age_seconds=None):
    now = datetime.utcnow()
    finalized_at = None if finalized_age_seconds is None else now - timedelta(seconds=finalized_age_seconds)
    ended_at = None if status == "writing" else finalized_at
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path="/storage/archive/redacted/file.mkv",
        relative_path=f"{camera.storage_folder_name}/file.mkv",
        started_at=now - timedelta(seconds=started_age_seconds),
        ended_at=ended_at,
        finalized_at=finalized_at,
        duration_sec=0 if status == "writing" else 10,
        size_bytes=100,
        status=status,
        ownership="KM VMS",
        source="recorder",
    )
    db.add(segment)
    db.commit()
    return segment


def fake_storage(*, storage_severity="ok", reconciliation_severity="ok"):
    def _summary(db_arg, *, include_namespace_observations=True, write_audit=False, audit_actor=None):
        unavailable = storage_severity == "error"
        return {
            "status": "unavailable" if unavailable else "available",
            "available": not unavailable,
            "checked_at": "2026-05-16T00:00:00Z",
            "storage_path_checks": {"readable": not unavailable, "writable": not unavailable},
            "capacity": {"total_bytes": 1000, "used_bytes": 100, "free_bytes": 900, "available_bytes": 900, "filesystem_probe_status": "ok"},
            "owned_archive": {"kmvms_owned_segments_count": 0, "kmvms_owned_existing_file_count": 0},
            "reconciliation_summary": {"missing_file_count": 1 if reconciliation_severity == "warning" else 0},
            "cleanup_candidates_summary": {"count": 0},
            "scan_limited": False,
            "partial": False,
        }

    return _summary


def rendered(payload):
    return str(payload)


def test_healthy_active_recording_has_no_camera_or_recorder_warning(db):
    camera = add_camera(db)
    add_job(db, camera)
    add_segment(db, camera, status="writing", started_age_seconds=40)
    add_heartbeat(db)

    payload = build_operator_runtime_status(db)
    item = payload["domains"]["cameras"]["items"][0]

    assert payload["domains"]["recorder"]["severity"] == "ok"
    assert item["recording_state"] == "recording"
    assert item["severity"] == "ok"
    assert item["reason_codes"] == []


def test_soft_deleted_camera_is_excluded_from_runtime_warnings(db, monkeypatch):
    camera = add_camera(db, deleted=True, status="error", last_error="".join(["rt", "sp://user:redacted@example.test/down"]))
    add_job(db, camera, state="error", last_error="redacted failure")
    add_heartbeat(db, service_status="healthy", active_jobs=0, recording_cameras=0)
    monkeypatch.setattr(
        runtime_status.live_manager,
        "status",
        lambda: [{"camera_id": camera.id, "stream": "main", "status": "failed", "ready": False, "running": False, "last_error": "redacted"}],
    )

    payload = build_operator_runtime_status(db)

    assert payload["domains"]["cameras"]["summary"]["total_count"] == 0
    assert payload["domains"]["cameras"]["items"] == []
    assert payload["domains"]["live"]["items"] == []
    assert "redacted" not in rendered(payload)
    assert "".join(["rt", "sp://"]) not in rendered(payload)


def test_stale_finalized_segment_does_not_warn_when_current_segment_is_valid(db):
    camera = add_camera(db)
    add_job(db, camera)
    add_segment(db, camera, status="ready", started_age_seconds=4000, finalized_age_seconds=3900)
    add_segment(db, camera, status="writing", started_age_seconds=80)
    add_heartbeat(db)

    payload = build_operator_runtime_status(db)
    item = payload["domains"]["cameras"]["items"][0]

    assert item["last_segment_age_seconds"] > item["stale_after_seconds"]
    assert item["current_segment_age_seconds"] <= item["stale_after_seconds"]
    assert item["severity"] == "ok"
    assert "recording_stale" not in item["reason_codes"]


def test_current_segment_beyond_stage15_threshold_still_warns(db):
    camera = add_camera(db)
    add_job(db, camera)
    add_segment(db, camera, status="writing", started_age_seconds=650)
    add_heartbeat(db)

    item = build_operator_runtime_status(db)["domains"]["cameras"]["items"][0]

    assert item["stale_after_seconds"] == 600
    assert item["severity"] == "warning"
    assert "recording_segment_not_rotating" in item["reason_codes"]


def test_recorder_heartbeat_stale_still_warns(db):
    camera = add_camera(db)
    add_job(db, camera)
    add_segment(db, camera, status="writing", started_age_seconds=40)
    add_heartbeat(db, age_seconds=runtime_status.HEARTBEAT_STALE_SECONDS + 5, active_jobs=1)

    recorder = build_operator_runtime_status(db)["domains"]["recorder"]

    assert recorder["severity"] == "error"
    assert "recorder_heartbeat_stale" in recorder["safe_reason_codes"]


def test_recording_expected_without_active_job_still_warns(db):
    add_camera(db)
    add_heartbeat(db, active_jobs=0, recording_cameras=0)

    item = build_operator_runtime_status(db)["domains"]["cameras"]["items"][0]

    assert item["severity"] == "warning"
    assert "no_evidence" in item["reason_codes"]


def test_camera_failure_still_warns_without_raw_error(db):
    camera = add_camera(db, status="error", last_error="".join(["rt", "sp://user:redacted@example.test/down"]))
    add_job(db, camera, state="error", last_error="redacted failed")
    add_heartbeat(db, service_status="healthy", active_jobs=0, recording_cameras=0)

    payload = build_operator_runtime_status(db)
    item = payload["domains"]["cameras"]["items"][0]

    assert item["severity"] == "error"
    assert "recording_failed" in item["reason_codes"]
    assert "redacted" not in rendered(payload)
    assert "".join(["rt", "sp://"]) not in rendered(payload)


def test_live_not_requested_or_not_applicable_stays_neutral(db, monkeypatch):
    camera = add_camera(db, enabled=False)
    add_heartbeat(db, active_jobs=0, recording_cameras=0)
    monkeypatch.setattr(runtime_status.live_manager, "status", lambda: [])

    live = build_operator_runtime_status(db)["domains"]["live"]

    assert live["severity"] in {"ok", "unknown"}
    assert live["summary"]["warning_count"] == 0
    assert live["summary"]["error_count"] == 0
    assert live["items"] == []


def test_live_starting_below_30_seconds_stays_neutral(db, monkeypatch):
    camera = add_camera(db)
    add_heartbeat(db, active_jobs=0, recording_cameras=0)
    monkeypatch.setattr(
        runtime_status.live_manager,
        "status",
        lambda: [
            {
                "camera_id": camera.id,
                "stream": "main",
                "status": "starting",
                "ready": False,
                "running": True,
                "viewers": 1,
                "startup_elapsed_seconds": runtime_status.LIVE_STARTING_WARNING_THRESHOLD_SECONDS - 1,
            }
        ],
    )

    live = build_operator_runtime_status(db)["domains"]["live"]

    assert live["severity"] == "ok"
    assert live["summary"]["warning_count"] == 0
    assert live["items"][0]["severity"] == "ok"
    assert "live_starting" not in live["items"][0]["reason_codes"]


def test_live_starting_at_30_seconds_warns_with_stuck_evidence(db, monkeypatch):
    camera = add_camera(db)
    add_heartbeat(db, active_jobs=0, recording_cameras=0)
    monkeypatch.setattr(
        runtime_status.live_manager,
        "status",
        lambda: [
            {
                "camera_id": camera.id,
                "stream": "main",
                "status": "restarting",
                "ready": False,
                "running": True,
                "viewers": 1,
                "startup_elapsed_seconds": runtime_status.LIVE_STARTING_WARNING_THRESHOLD_SECONDS,
            }
        ],
    )

    live = build_operator_runtime_status(db)["domains"]["live"]

    assert live["severity"] == "warning"
    assert live["items"][0]["severity"] == "warning"
    assert "live_starting" in live["items"][0]["reason_codes"]


def test_live_failure_warns_immediately_without_starting_threshold(db, monkeypatch):
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
                "viewers": 1,
                "startup_elapsed_seconds": 1,
                "last_error": "connect timeout",
            }
        ],
    )

    live = build_operator_runtime_status(db)["domains"]["live"]

    assert live["severity"] == "error"
    assert live["items"][0]["severity"] == "error"
    assert "live_failed" in live["items"][0]["reason_codes"]


def test_storage_retention_and_reconciliation_warnings_still_surface(db, monkeypatch):
    add_camera(db)
    monkeypatch.setattr(runtime_status, "build_storage_monitoring_summary", fake_storage(storage_severity="error", reconciliation_severity="warning"))
    AUTO_RETENTION_STATE.update({"enabled": True, "running": False, "last_status": "failed", "last_error": "/redacted/path", "run_count": 1})

    payload = build_operator_runtime_status(db)

    assert payload["domains"]["storage"]["severity"] == "error"
    assert "storage_unavailable" in payload["domains"]["storage"]["reason_codes"]
    assert payload["domains"]["retention"]["severity"] == "error"
    assert "retention_failed" in payload["domains"]["retention"]["reason_codes"]
    assert payload["domains"]["reconciliation"]["severity"] == "warning"
    assert "reconciliation_problems_found" in payload["domains"]["reconciliation"]["reason_codes"]
    assert "/redacted/path" not in rendered(payload)
