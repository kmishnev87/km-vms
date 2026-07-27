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
from app.models.storage_operation import StorageOperation
from app.routers.settings import system_status
from app.services import system_runtime_status as runtime_status
from app.services.system_runtime_status import build_operator_runtime_status
from app.services.recording_storage import (
    DEFAULT_ARCHIVE_ROOT_ID,
    ROOT_RESOLUTION_RESOLVED,
    ensure_archive_roots,
)


RECORDER_INSTANCE_ID = "stage20-recorder"


@pytest.fixture
def db():
    tmp = tempfile.TemporaryDirectory(prefix="stage20_runtime_status_")
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
def no_live_evidence(monkeypatch):
    monkeypatch.setattr(runtime_status.live_manager, "status", lambda: [])


def add_retention_operation(db, *, status, result_status=None, reason_code=None):
    now = datetime.utcnow()
    index = db.query(StorageOperation).filter(StorageOperation.operation_type == "retention_auto_run").count() + 1
    operation = StorageOperation(
        id=f"runtime-retention-{index}",
        operation_type="retention_auto_run",
        actor_kind="system",
        actor_key="system:runtime-test",
        system_owner="runtime-test",
        idempotency_key=f"runtime-retention-{index}",
        request_fingerprint=f"{index:064x}",
        status=status,
        scope={"global": False, "physical_volume_ids": [], "root_ids": [], "camera_ids": [index], "segment_ids": [], "scope_escalated": False},
        progress={},
        result={"status": result_status or status, "deleted_count": 0},
        reason_code=reason_code,
        queued_at=now,
        started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(operation)
    db.commit()
    return operation


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
        recorder_instance_id=RECORDER_INSTANCE_ID,
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
    ensure_archive_roots(db)
    now = datetime.utcnow()
    job = db.query(RecordingJob).filter(RecordingJob.camera_id == camera.id).order_by(RecordingJob.updated_at.desc()).first()
    segment = RecordingSegment(
        camera_id=camera.id,
        job_id=job.id if job else None,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path="/storage/archive/redacted/file.mkv",
        relative_path="camera/file.mkv",
        started_at=now - timedelta(seconds=age_seconds + 10),
        ended_at=None,
        finalized_at=None,
        duration_sec=0,
        size_bytes=100,
        media_progress_at=now,
        status="writing",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=now,
    )
    db.add(segment)
    db.commit()
    return segment


def add_owned_segment(db, camera, *, relative_path="kmvms/recordings/camera/file.mkv", status="ready", finalized_age_seconds=30):
    ensure_archive_roots(db)
    if not relative_path.startswith("kmvms/recordings/"):
        relative_path = f"kmvms/recordings/{relative_path.lstrip('/')}"
    root = Path(settings.storage_root)
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"stage3 video bytes")
    now = datetime.utcnow()
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(target),
        relative_path=relative_path,
        started_at=now - timedelta(seconds=finalized_age_seconds + 10),
        ended_at=now - timedelta(seconds=finalized_age_seconds),
        finalized_at=now - timedelta(seconds=finalized_age_seconds),
        duration_sec=10,
        size_bytes=18,
        status=status,
        ownership="KM VMS",
        source="recorder",
        archive_root_id=DEFAULT_ARCHIVE_ROOT_ID,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        archive_root_resolved_at=now,
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
                :instance_id, :service_status, 'running', :started_at, :heartbeat_at,
                :active_jobs, :recording_cameras, :failed_cameras, NULL, NULL, :updated_at
            )
            """
        ),
        {
            "service_status": service_status,
            "instance_id": RECORDER_INSTANCE_ID,
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

    assert set(payload["domains"]) == {"cameras", "live", "recorder", "storage", "retention", "reconciliation"}
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


def test_recording_job_with_stale_current_segment_maps_to_warning(db):
    camera = add_camera(db)
    job = add_job(db, camera, state="recording")
    now = datetime.utcnow()
    db.add(
        RecordingSegment(
            camera_id=camera.id,
            job_id=job.id,
            camera_name_snapshot=camera.name,
            camera_folder_snapshot=camera.storage_folder_name,
            file_path="/storage/archive/redacted/stale.mkv",
            relative_path="camera/stale.mkv",
            started_at=now - timedelta(minutes=65),
            ended_at=None,
            finalized_at=None,
            duration_sec=0,
            size_bytes=100,
            media_progress_at=now - timedelta(minutes=65),
            status="writing",
            ownership="KM VMS",
            source="recorder",
        )
    )
    db.commit()
    add_heartbeat(db)

    payload = build_operator_runtime_status(db)
    camera_item = payload["domains"]["cameras"]["items"][0]

    assert camera_item["recording_state"] == "recording"
    assert camera_item["recording_health"] == "degraded"
    assert camera_item["severity"] == "warning"
    assert camera_item["stale_current_segment"] is True
    assert camera_item["stale_after_seconds"] == 600
    assert camera_item["expected_segment_duration_seconds"] == 300
    assert "recording_segment_not_rotating" in camera_item["reason_codes"]
    assert "recording_stale" in camera_item["reason_codes"]


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


def test_storage_domain_available_readable_writable_maps_to_ok_without_paths(db):
    camera = add_camera(db)
    add_owned_segment(db, camera)

    payload = build_operator_runtime_status(db)
    storage = payload["domains"]["storage"]

    assert storage["severity"] == "ok"
    assert storage["available"] is True
    assert storage["readable"] is True
    assert storage["writable"] is True
    assert storage["capacity"]["total_bytes"] is not None
    assert storage["summary"]["existing_file_count"] is None
    assert storage["evidence_status"] == "fresh"
    assert str(settings.storage_root) not in rendered(storage)
    assert "camera/file.mkv" not in rendered(storage)


def test_storage_domain_unavailable_maps_to_error_without_raw_path(db):
    settings.storage_root = str(Path(settings.storage_root) / "missing-root")

    payload = build_operator_runtime_status(db)
    storage = payload["domains"]["storage"]

    assert storage["severity"] == "error"
    assert storage["available"] is False
    assert "storage_unavailable" in storage["reason_codes"]
    assert settings.storage_root not in rendered(storage)


def test_retention_no_evidence_is_unknown_and_failed_state_is_error(db):
    camera = add_camera(db)
    payload = build_operator_runtime_status(db)
    retention = payload["domains"]["retention"]

    assert retention["severity"] == "unknown"
    assert "retention_never_run" in retention["reason_codes"]
    assert retention["policy_count"] == 1

    add_retention_operation(db, status="failed", reason_code="automatic_retention_failed")
    failed_payload = build_operator_runtime_status(db)
    failed = failed_payload["domains"]["retention"]

    assert failed["severity"] == "error"
    assert "retention_failed" in failed["reason_codes"]
    assert "/secret/path" not in rendered(failed_payload)
    assert camera.name in rendered(failed_payload)


def test_fresh_install_without_cameras_does_not_report_retention_policy_risk(db):
    payload = build_operator_runtime_status(db)
    retention = payload["domains"]["retention"]

    assert retention["camera_count"] == 0
    assert retention["policy_count"] == 0
    assert "retention_policy_risk" not in retention["reason_codes"]


@pytest.mark.parametrize("last_status", ["ok", "completed", "success", "completed_successfully", "succeeded"])
def test_retention_success_aliases_are_normalized(last_status):
    assert runtime_status._normalize_retention_status(last_status) == "success"


def test_durable_retention_completed_maps_to_ok(db):
    add_camera(db)
    add_retention_operation(db, status="completed", result_status="compliant")

    payload = build_operator_runtime_status(db)
    retention = payload["domains"]["retention"]

    assert retention["severity"] == "ok"
    assert retention["evidence_status"] == "fresh"
    assert retention["reason_codes"] == []


@pytest.mark.parametrize("last_status", ["completed_with_warnings", "skipped_concurrent"])
def test_retention_warning_aliases_are_normalized(last_status):
    assert runtime_status._normalize_retention_status(last_status) == "warning"


@pytest.mark.parametrize("operation_status", ["partial", "blocked", "cancelled"])
def test_durable_retention_non_success_terminal_maps_to_warning(db, operation_status):
    add_camera(db)
    add_retention_operation(db, status=operation_status, result_status="no_safe_candidate")

    payload = build_operator_runtime_status(db)
    retention = payload["domains"]["retention"]

    assert retention["severity"] == "warning"
    assert "retention_completed_with_warnings" in retention["reason_codes"]
    assert retention["evidence_status"] == "fresh"


def test_retention_missing_and_unsupported_statuses_do_not_fake_ok(db):
    add_camera(db)

    missing_payload = build_operator_runtime_status(db)
    missing = missing_payload["domains"]["retention"]

    assert missing["severity"] == "unknown"
    assert missing["evidence_status"] == "missing"
    assert "retention_never_run" in missing["reason_codes"]

    assert runtime_status._normalize_retention_status("mystery_status") == "unknown"


def test_reconciliation_problem_counts_map_to_warning_without_samples_or_paths(db, monkeypatch):
    camera = add_camera(db)
    add_owned_segment(db, camera, relative_path="missing/missing.mkv")
    summary = runtime_status.build_lightweight_storage_monitoring_summary(db)
    summary["reconciliation_summary"] = {
        "status": "completed_with_findings",
        "evidence_status": "fresh",
        "source": "durable_archive_integrity_scan",
        "scan_id": "runtime-contract-scan",
        "active": False,
        "phase": "completed",
        "checked_count": 1,
        "failed_count": 0,
        "last_checked_at": datetime.utcnow().isoformat() + "Z",
        "missing_file_count": 1,
        "root_unavailable_count": 0,
        "root_unresolved_count": 0,
        "invalid_path_count": 0,
        "path_outside_storage_count": 0,
        "orphan_file_count": 0,
        "problem_file_count": 1,
    }
    monkeypatch.setattr(
        runtime_status,
        "build_lightweight_storage_monitoring_summary",
        lambda _db: summary,
    )

    payload = build_operator_runtime_status(db)
    reconciliation = payload["domains"]["reconciliation"]

    assert reconciliation["severity"] == "warning"
    assert reconciliation["missing_file_count"] == 1
    assert reconciliation["problem_file_count"] == 1
    assert "reconciliation_problems_found" in reconciliation["reason_codes"]
    assert "missing/missing.mkv" not in rendered(reconciliation)
    assert "samples" not in rendered(reconciliation)


def test_runtime_status_uses_same_bounded_orphan_evidence_as_storage(db):
    ensure_archive_roots(db)
    orphan = Path(settings.storage_root, "kmvms", "recordings", "orphan.mkv")
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    payload = build_operator_runtime_status(db)
    storage = payload["domains"]["storage"]
    reconciliation = payload["domains"]["reconciliation"]

    assert storage["severity"] == "ok"
    assert reconciliation["severity"] == "unknown"
    assert reconciliation["orphan_file_count"] == 0
    assert reconciliation["problem_file_count"] == 0
    assert "reconciliation_unknown" in reconciliation["reason_codes"]
    assert "orphan.mkv" not in rendered(payload)


def test_reconciliation_missing_evidence_is_unknown_not_fresh(db, monkeypatch):
    def fake_storage_summary(db_arg, *, include_namespace_observations=True, write_audit=False, audit_actor=None):
        return {
            "status": "available",
            "available": True,
            "checked_at": "2026-05-05T00:00:00Z",
            "storage_path_checks": {"readable": True, "writable": True},
            "capacity": {"total_bytes": 100, "used_bytes": 50, "free_bytes": 50, "available_bytes": 50, "filesystem_probe_status": "ok"},
            "owned_archive": {},
            "scan_limited": False,
            "partial": False,
        }

    monkeypatch.setattr(runtime_status, "build_lightweight_storage_monitoring_summary", fake_storage_summary)
    payload = build_operator_runtime_status(db)
    reconciliation = payload["domains"]["reconciliation"]

    assert reconciliation["severity"] == "unknown"
    assert reconciliation["status"] == "no_evidence"
    assert reconciliation["evidence_status"] == "missing"
    assert reconciliation["source"] == "reconciliation_evidence_missing"
    assert "no_evidence" in reconciliation["reason_codes"]
    assert "reconciliation_unknown" in reconciliation["reason_codes"]


@pytest.mark.parametrize(
    "reconciliation_summary, cleanup_candidates_summary",
    [
        (None, None),
        (None, {"count": 0}),
        ({}, None),
        ("not-a-dict", {}),
        ({}, ["not-a-dict"]),
        ("not-a-dict", ["not-a-dict"]),
    ],
)
def test_reconciliation_invalid_evidence_shape_is_unknown_not_fresh(
    db,
    monkeypatch,
    reconciliation_summary,
    cleanup_candidates_summary,
):
    def fake_storage_summary(db_arg, *, include_namespace_observations=True, write_audit=False, audit_actor=None):
        return {
            "status": "available",
            "available": True,
            "checked_at": "2026-05-05T00:00:00Z",
            "storage_path_checks": {"readable": True, "writable": True},
            "capacity": {"total_bytes": 100, "used_bytes": 50, "free_bytes": 50, "available_bytes": 50, "filesystem_probe_status": "ok"},
            "owned_archive": {},
            "reconciliation_summary": reconciliation_summary,
            "cleanup_candidates_summary": cleanup_candidates_summary,
            "scan_limited": False,
            "partial": False,
        }

    monkeypatch.setattr(runtime_status, "build_lightweight_storage_monitoring_summary", fake_storage_summary)
    payload = build_operator_runtime_status(db)
    reconciliation = payload["domains"]["reconciliation"]

    assert reconciliation["severity"] == "unknown"
    assert reconciliation["status"] == "no_evidence"
    assert reconciliation["evidence_status"] == "missing"
    assert reconciliation["source"] == "reconciliation_evidence_missing"
    assert "no_evidence" in reconciliation["reason_codes"]
    assert "reconciliation_unknown" in reconciliation["reason_codes"]


def test_reconciliation_explicit_zero_evidence_can_be_ok(db, monkeypatch):
    def fake_storage_summary(db_arg, *, include_namespace_observations=True, write_audit=False, audit_actor=None):
        return {
            "status": "available",
            "available": True,
            "checked_at": "2026-05-05T00:00:00Z",
            "storage_path_checks": {"readable": True, "writable": True},
            "capacity": {"total_bytes": 100, "used_bytes": 50, "free_bytes": 50, "available_bytes": 50, "filesystem_probe_status": "ok"},
            "owned_archive": {},
            "reconciliation_summary": {
                "evidence_status": "fresh",
                "source": "explicit_reconciliation",
                "missing_file_count": 0,
                "orphan_file_count": 0,
                "path_outside_storage_count": 0,
                "invalid_path_count": 0,
            },
            "cleanup_candidates_summary": {"count": 0},
            "scan_limited": False,
            "partial": False,
        }

    monkeypatch.setattr(runtime_status, "build_lightweight_storage_monitoring_summary", fake_storage_summary)
    payload = build_operator_runtime_status(db)
    reconciliation = payload["domains"]["reconciliation"]

    assert reconciliation["severity"] == "ok"
    assert reconciliation["status"] == "ok"
    assert reconciliation["evidence_status"] == "fresh"
    assert reconciliation["reason_codes"] == []


def test_reconciliation_cleanup_candidates_map_to_warning(db, monkeypatch):
    def fake_storage_summary(db_arg, *, include_namespace_observations=True, write_audit=False, audit_actor=None):
        return {
            "status": "available",
            "available": True,
            "checked_at": "2026-05-05T00:00:00Z",
            "storage_path_checks": {"readable": True, "writable": True},
            "capacity": {"total_bytes": 100, "used_bytes": 50, "free_bytes": 50, "available_bytes": 50, "filesystem_probe_status": "ok"},
            "owned_archive": {},
            "reconciliation_summary": {"evidence_status": "fresh", "source": "explicit_reconciliation"},
            "cleanup_candidates_summary": {"count": 2},
            "scan_limited": False,
            "partial": False,
        }

    monkeypatch.setattr(runtime_status, "build_lightweight_storage_monitoring_summary", fake_storage_summary)
    payload = build_operator_runtime_status(db)
    reconciliation = payload["domains"]["reconciliation"]

    assert reconciliation["severity"] == "warning"
    assert reconciliation["cleanup_candidate_count"] == 2
    assert "cleanup_candidates_present" in reconciliation["reason_codes"]


def test_stage3_aggregate_has_no_audit_side_effects_and_no_diagnostic_archive_builder(db, monkeypatch):
    called = {"storage": None}

    def fake_storage_summary(db_arg):
        called["storage"] = "lightweight"
        return {
            "status": "available",
            "available": True,
            "checked_at": "2026-05-05T00:00:00Z",
            "storage_path_checks": {"readable": True, "writable": True},
            "capacity": {"total_bytes": 100, "used_bytes": 50, "free_bytes": 50, "available_bytes": 50, "filesystem_probe_status": "ok"},
            "owned_archive": {},
            "reconciliation_summary": {},
            "cleanup_candidates_summary": {},
            "scan_limited": False,
            "partial": False,
        }

    monkeypatch.setattr(runtime_status, "build_lightweight_storage_monitoring_summary", fake_storage_summary)
    payload = build_operator_runtime_status(db)

    assert called["storage"] == "lightweight"
    assert payload["domains"]["storage"]["source"] == "storage_monitoring_metadata_summary"
