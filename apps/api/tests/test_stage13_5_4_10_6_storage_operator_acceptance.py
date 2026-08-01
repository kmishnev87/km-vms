import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
import app.models  # noqa: F401,E402 - register the complete production model graph
from app.models.archive_integrity import ArchiveIntegrityFinding, RecorderFileReceipt
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.storage_operation import StorageOperation, StorageWorkSignal
from app.models.system_settings import SystemSettings
from app.services import archive_integrity as integrity
from app.services import archive_integrity_remediation as remediation
from app.services import recording_retention as deletion
from app.services import retention_automation as retention
from app.services.archive_integrity import latest_integrity_scan, run_integrity_worker_once, start_integrity_scan
from app.services.archive_integrity_remediation import IntegrityRemediationBlocked, apply_remediation_plan, create_remediation_plan
from app.services.storage_monitoring import useful_storage_operation_history
from app.services.storage_operations_foundation import database_now


REAL_SAFE_PROBE = integrity._safe_probe


def owner(user_id=4106):
    return SimpleNamespace(id=user_id, username=f"stage4106-owner-{user_id}", role="owner", is_active=True)


@pytest.fixture
def stage4106(tmp_path, monkeypatch):
    original = {
        "storage_root": settings.storage_root,
        "storage_previews": settings.storage_previews,
        "storage_exports": settings.storage_exports,
    }
    archive = tmp_path / "archive"
    namespace = archive / "kmvms" / "recordings"
    namespace.mkdir(parents=True)
    settings.storage_root = str(archive)
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")
    engine = create_engine(f"sqlite:///{tmp_path / 'stage4106.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    root = ArchiveRoot(
        id="stage4106-root",
        label="Stage 4.10.6 volume",
        root_path=str(archive),
        storage_namespace="kmvms/recordings",
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="fs-stage4106",
    )
    db.add_all(
        [
            SystemSettings(
                system_initialized=True,
                timezone="UTC",
                language="ru",
                storage_path=str(archive),
                recording_format="mkv",
                auto_free_space_cleanup_enabled=False,
                recording_suspended_by_low_disk=False,
            ),
            root,
        ]
    )
    db.commit()
    monkeypatch.setattr(integrity, "SessionLocal", Session)
    monkeypatch.setattr(integrity, "_safe_probe", lambda _path: (True, "probe_ok"))
    integrity._worker_stop.clear()
    try:
        yield SimpleNamespace(db=db, engine=engine, Session=Session, archive=archive, namespace=namespace, root=root)
    finally:
        integrity._worker_stop.clear()
        db.close()
        engine.dispose()
        settings.storage_root = original["storage_root"]
        settings.storage_previews = original["storage_previews"]
        settings.storage_exports = original["storage_exports"]


def add_camera(ctx, *, name="Camera 4.10.6", days=30, quota=50):
    camera = Camera(
        name=name,
        storage_folder_name=name.lower().replace(" ", "-"),
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        username="test",
        recording_mode="always",
        default_live_stream="sub",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=days,
        storage_quota_gb=quota,
        retention_policy_version=1,
        status="disabled",
    )
    ctx.db.add(camera)
    ctx.db.commit()
    ctx.db.refresh(camera)
    return camera


def add_segment(ctx, camera, *, name, status="finalized", age_minutes=60, content=b"stage4106-video", commit=True):
    relative = f"kmvms/recordings/camera_{camera.id}/{name}"
    path = ctx.archive / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = datetime.utcnow() - timedelta(minutes=age_minutes)
    os.utime(path, (old.timestamp(), old.timestamp()))
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(path),
        relative_path=relative,
        started_at=old - timedelta(minutes=5),
        ended_at=old if status == "finalized" else None,
        finalized_at=old if status == "finalized" else None,
        duration_sec=300 if status == "finalized" else 0,
        size_bytes=len(content),
        stream_type="main",
        status=status,
        ownership="KM VMS",
        source="recorder",
        archive_root_id=ctx.root.id,
        archive_root_resolution_status="resolved",
        archive_root_resolution_detail="stage4106-test",
        archive_root_resolved_at=old,
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status="ok_owned_finalized" if status == "finalized" else status,
        reconciliation_status="ok_owned_finalized" if status == "finalized" else status,
        created_at=old,
        updated_at=old,
    )
    ctx.db.add(segment)
    if commit:
        ctx.db.commit()
        ctx.db.refresh(segment)
    return segment, path


def add_writing_receipt(ctx, segment, path):
    stat_result = path.stat()
    receipt = RecorderFileReceipt(
        id=str(uuid.uuid4()),
        contract_version=1,
        segment_id=int(segment.id),
        job_id=segment.job_id,
        camera_id=int(segment.camera_id),
        root_id=str(ctx.root.id),
        physical_identity=ctx.root.physical_identity,
        relative_path=str(segment.relative_path),
        state="writing",
        object_identity=integrity._receipt_object_identity(str(ctx.root.id), str(segment.relative_path), stat_result),
        device_id=str(int(stat_result.st_dev)),
        inode=str(int(stat_result.st_ino)),
        size_bytes=int(stat_result.st_size),
        mtime_ns=int(stat_result.st_mtime_ns),
        content_fingerprint=integrity._bounded_fingerprint(path, stat_result),
    )
    ctx.db.add(receipt)
    ctx.db.commit()
    return receipt


def run_scan(ctx, key):
    queued = start_integrity_scan(ctx.db, actor=owner(), idempotency_key=key)
    assert queued["status"] == "queued"
    assert run_integrity_worker_once() is True
    ctx.db.expire_all()
    return latest_integrity_scan(ctx.db)


def active_findings(ctx, scan_id):
    return (
        ctx.db.query(ArchiveIntegrityFinding)
        .filter(ArchiveIntegrityFinding.scan_id == scan_id, ArchiveIntegrityFinding.is_active.is_(True))
        .order_by(ArchiveIntegrityFinding.id.asc())
        .all()
    )


def add_operation(ctx, *, operation_id, operation_type, when, status="completed", deleted=0, freed=0, retry=False):
    row = StorageOperation(
        id=operation_id,
        operation_type=operation_type,
        actor_kind="system",
        actor_key="system:stage4106",
        system_owner="stage4106",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint=uuid.uuid4().hex * 2,
        status=status,
        scope={},
        progress={},
        result={"status": status, "deleted_count": deleted, "bytes_freed": freed},
        reason_code="stage4106_attention" if status != "completed" else None,
        next_action="retry_operation" if retry else None,
        retry_mode="scheduled" if retry else None,
        retry_allowed=retry,
        cancel_allowed=False,
        fencing_token=1,
        revision=2,
        queued_at=when,
        started_at=when,
        heartbeat_at=when,
        finished_at=when,
        created_at=when,
        updated_at=when,
    )
    ctx.db.add(row)
    return row


def test_compliant_signal_runs_do_not_grow_terminal_history_then_violation_executes(stage4106):
    camera = add_camera(stage4106, days=30)
    segment, path = add_segment(stage4106, camera, name="retention.mkv", age_minutes=4 * 24 * 60)
    for _index in range(2):
        retention.advance_retention_signal(stage4106.db)
        handle = retention.claim_retention_signal(stage4106.db, owner_instance_id=f"stage4106-{_index}")
        result = retention.run_retention_signal_generation(stage4106.db, handle, page_size=5)
        assert result["status"] == "completed"
    assert stage4106.db.query(StorageOperation).filter(StorageOperation.operation_type == "retention_auto_run").count() == 0
    signal = stage4106.db.query(StorageWorkSignal).filter(StorageWorkSignal.signal_type == retention.RETENTION_SIGNAL_TYPE).one()
    assert signal.consumed_watermark == signal.requested_watermark

    camera.retention_days = 1
    camera.retention_policy_version = 2
    stage4106.db.commit()
    retention.advance_retention_signal(stage4106.db)
    handle = retention.claim_retention_signal(stage4106.db, owner_instance_id="stage4106-violation")
    retention.run_retention_signal_generation(stage4106.db, handle, page_size=5)
    assert not path.exists()
    assert stage4106.db.get(RecordingSegment, segment.id).status == "deleted"
    assert stage4106.db.query(StorageOperation).filter(StorageOperation.operation_type == "retention_auto_run").count() == 1


def test_policy_change_between_measurement_and_claim_never_calls_delete(stage4106, monkeypatch):
    camera = add_camera(stage4106, days=1)
    segment, path = add_segment(stage4106, camera, name="race.mkv", age_minutes=4 * 24 * 60)
    snapshot = retention.camera_policy_snapshot(
        camera,
        signal_watermark=77,
        high_watermark=segment.id,
        evaluation_at=database_now(stage4106.db),
    )
    real_claim = retention._claim_camera_operation

    def change_policy_after_claim(db, claimed_snapshot):
        claim = real_claim(db, claimed_snapshot)
        current = db.get(Camera, camera.id)
        current.retention_days = 365
        current.retention_policy_version = 2
        db.add(current)
        db.commit()
        return claim

    monkeypatch.setattr(retention, "_claim_camera_operation", change_policy_after_claim)
    monkeypatch.setattr(retention, "execute_segments", lambda *_args, **_kwargs: pytest.fail("delete executor called"))
    result = retention.run_camera_retention_operation(stage4106.db, snapshot, page_size=5)
    assert result["status"] == "superseded_policy_changed"
    assert path.exists()


def test_useful_history_aggregates_deletions_filters_zero_rows_before_limit_and_keeps_attention(stage4106):
    now = datetime.utcnow()
    for index in range(80):
        add_operation(
            stage4106,
            operation_id=f"zero-{index}",
            operation_type="retention_auto_run",
            when=now - timedelta(minutes=index),
        )
    add_operation(
        stage4106,
        operation_id="useful-old-day",
        operation_type="retention_auto_run",
        when=now - timedelta(days=5),
        deleted=7,
        freed=7000,
    )
    add_operation(
        stage4106,
        operation_id="auto-free-actual",
        operation_type="retention_auto_free_space",
        when=now - timedelta(hours=2),
        deleted=3,
        freed=3000,
    )
    add_operation(
        stage4106,
        operation_id="attention",
        operation_type="retention_auto_run",
        when=now - timedelta(hours=1),
        status="partial",
        retry=True,
    )
    harmless_completed = add_operation(
        stage4106,
        operation_id="completed-no-retry",
        operation_type="archive_root_delete",
        when=now - timedelta(minutes=30),
    )
    harmless_completed.retry_mode = "none"
    stage4106.db.commit()

    history = useful_storage_operation_history(stage4106.db)
    assert history["summary"]["auto_free_space"]["deleted_count"] == 3
    assert any(item["day"] == (now - timedelta(days=5)).date().isoformat() for item in history["daily_items"])
    assert all(item["deleted_count"] > 0 or item["bytes_freed"] > 0 for item in history["daily_items"])
    assert [item["id"] for item in history["attention_items"]] == ["attention"]
    assert "owner_instance_id" not in str(history) and "scope" not in str(history)


def test_safe_stale_convergence_atomically_finalizes_segment_receipt_and_signal(stage4106):
    camera = add_camera(stage4106)
    segment, path = add_segment(stage4106, camera, name="stale-ok.mkv", status="writing")
    receipt = add_writing_receipt(stage4106, segment, path)
    scan = run_scan(stage4106, "stale-success")

    finalized = stage4106.db.get(RecordingSegment, segment.id)
    finalized_receipt = stage4106.db.get(RecorderFileReceipt, receipt.id)
    assert finalized.status == "finalized"
    assert finalized.integrity_status == "ok_owned_finalized"
    assert finalized_receipt.state == "finalized"
    assert finalized_receipt.size_bytes == path.stat().st_size
    assert stage4106.db.query(StorageWorkSignal).filter(StorageWorkSignal.signal_type == retention.RETENTION_SIGNAL_TYPE).count() == 1
    assert active_findings(stage4106, scan["scan_id"]) == []

    second = run_scan(stage4106, "stale-success-repeat")
    assert active_findings(stage4106, second["scan_id"]) == []
    assert stage4106.db.query(RecorderFileReceipt).filter(RecorderFileReceipt.segment_id == segment.id).count() == 1
    assert stage4106.db.query(StorageWorkSignal).filter(StorageWorkSignal.signal_type == retention.RETENTION_SIGNAL_TYPE).count() == 1


def test_real_media_probe_drives_small_stale_finalization_fixture(stage4106, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are unavailable in this test runtime")
    camera = add_camera(stage4106)
    segment, path = add_segment(stage4106, camera, name="real-probe.mkv", status="writing")
    subprocess.run(
        [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=0.2", "-c:v", "mpeg4", str(path)],
        check=True,
        capture_output=True,
        timeout=15,
    )
    old = datetime.utcnow() - timedelta(hours=1)
    os.utime(path, (old.timestamp(), old.timestamp()))
    segment = stage4106.db.get(RecordingSegment, segment.id)
    segment.size_bytes = path.stat().st_size
    segment.updated_at = old
    stage4106.db.add(segment)
    stage4106.db.commit()
    add_writing_receipt(stage4106, segment, path)
    monkeypatch.setattr(integrity, "_safe_probe", REAL_SAFE_PROBE)
    scan = run_scan(stage4106, "stale-real-probe")
    assert stage4106.db.get(RecordingSegment, segment.id).status == "finalized"
    assert active_findings(stage4106, scan["scan_id"]) == []


def test_actual_recent_file_mtime_blocks_stale_convergence_in_non_utc_timezone(stage4106):
    if not hasattr(time, "tzset"):
        pytest.skip("process timezone switching is unavailable")
    previous_timezone = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Yekaterinburg"
    time.tzset()
    try:
        camera = add_camera(stage4106)
        segment, path = add_segment(stage4106, camera, name="recent-stale.mkv", status="writing")
        os.utime(path, None)
        receipt = add_writing_receipt(stage4106, segment, path)

        scan = run_scan(stage4106, "recent-stale-file-non-utc")

        assert stage4106.db.get(RecordingSegment, segment.id).status == "writing"
        assert stage4106.db.get(RecorderFileReceipt, receipt.id).state == "writing"
        assert stage4106.db.query(StorageWorkSignal).filter(StorageWorkSignal.signal_type == retention.RETENTION_SIGNAL_TYPE).count() == 0
        assert all(item.segment_id != segment.id for item in active_findings(stage4106, scan["scan_id"]))
    finally:
        if previous_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_timezone
        time.tzset()


def test_epoch_recent_window_keeps_old_files_eligible_and_future_mtime_safe():
    now_ns = 1_800_000_000_000_000_000
    second_ns = 1_000_000_000
    window_ns = int(integrity.RECENT_WRITE_WINDOW.total_seconds() * second_ns)

    assert integrity._file_within_recent_write_window(
        SimpleNamespace(st_mtime_ns=now_ns - 5 * second_ns),
        current_time_ns=now_ns,
    ) is True
    assert integrity._file_within_recent_write_window(
        SimpleNamespace(st_mtime_ns=now_ns - window_ns - second_ns),
        current_time_ns=now_ns,
    ) is False
    assert integrity._file_within_recent_write_window(
        SimpleNamespace(st_mtime_ns=now_ns + second_ns),
        current_time_ns=now_ns,
    ) is True


def test_actual_recent_file_mtime_blocks_partial_visibility_prepare_and_final_gate(stage4106):
    camera = add_camera(stage4106)
    recent, recent_path = add_segment(stage4106, camera, name="recent-partial.mkv", status="failed")
    os.utime(recent_path, None)
    recent_scan = run_scan(stage4106, "recent-partial-file")
    assert all(item.segment_id != recent.id for item in active_findings(stage4106, recent_scan["scan_id"]))

    legacy, legacy_path = add_segment(stage4106, camera, name="legacy-recent-partial.mkv", status="failed")
    legacy_scan = run_scan(stage4106, "legacy-recent-partial")
    finding = next(item for item in active_findings(stage4106, legacy_scan["scan_id"]) if item.segment_id == legacy.id)
    os.utime(legacy_path, None)
    current_stat = legacy_path.stat()
    observed = dict(finding.observed_facts or {})
    observed.update(
        {
            "device_id": str(int(current_stat.st_dev)),
            "inode": str(int(current_stat.st_ino)),
            "size_bytes": int(current_stat.st_size),
            "mtime_ns": int(current_stat.st_mtime_ns),
        }
    )
    finding.observed_facts = observed
    stage4106.db.add(finding)
    stage4106.db.commit()

    with pytest.raises(IntegrityRemediationBlocked) as blocked:
        create_remediation_plan(
            stage4106.db,
            finding_id=finding.id,
            action_key="delete_unusable_recording",
            actor=owner(),
            idempotency_key="recent-partial-plan",
        )
    assert blocked.value.reason_code == "archive_integrity_recorder_window_active"
    assert legacy_path.exists()
    assert stage4106.db.get(RecordingSegment, legacy.id).status == "failed"

    file_facts_match, reason = deletion._matches_expected_file_facts(
        legacy_path,
        {
            "file_facts": {
                "device_id": str(int(current_stat.st_dev)),
                "inode": str(int(current_stat.st_ino)),
                "size_bytes": int(current_stat.st_size),
                "mtime_ns": int(current_stat.st_mtime_ns),
                "minimum_age_seconds": int(integrity.RECENT_WRITE_WINDOW.total_seconds()),
            }
        },
    )
    assert file_facts_match is False
    assert reason == "deletion_plan_file_recent"


@pytest.mark.parametrize("failure_owner", ["receipt", "signal"])
def test_stale_convergence_failure_rolls_back_all_atomic_facts(stage4106, monkeypatch, failure_owner):
    camera = add_camera(stage4106)
    segment, path = add_segment(stage4106, camera, name=f"stale-{failure_owner}.mkv", status="writing")
    receipt = add_writing_receipt(stage4106, segment, path)

    def injected_failure(*_args, **_kwargs):
        raise RuntimeError(f"injected-{failure_owner}-failure")

    monkeypatch.setattr(
        integrity,
        "_update_converged_receipt" if failure_owner == "receipt" else "_advance_converged_retention_signal",
        injected_failure,
    )
    scan = run_scan(stage4106, f"stale-{failure_owner}-failure")
    current = stage4106.db.get(RecordingSegment, segment.id)
    current_receipt = stage4106.db.get(RecorderFileReceipt, receipt.id)
    assert current.status == "writing"
    assert current_receipt.state == "writing"
    assert stage4106.db.query(StorageWorkSignal).filter(StorageWorkSignal.signal_type == retention.RETENTION_SIGNAL_TYPE).count() == 0
    assert [item.category for item in active_findings(stage4106, scan["scan_id"])] == ["stale_writing_segment"]


def test_missing_receipt_and_recent_segment_fail_closed_without_manual_click_requirement(stage4106):
    camera = add_camera(stage4106)
    stale, _path = add_segment(stage4106, camera, name="missing-receipt.mkv", status="writing")
    recent, _recent_path = add_segment(stage4106, camera, name="recent.mkv", status="writing", age_minutes=1)
    scan = run_scan(stage4106, "missing-receipt")
    rows = active_findings(stage4106, scan["scan_id"])
    finding = next(item for item in rows if item.segment_id == stale.id)
    assert finding.category == "stale_writing_segment"
    assert finding.action_key is None
    assert all(item.segment_id != recent.id for item in rows)
    assert stage4106.db.get(RecordingSegment, stale.id).status == "writing"

    with pytest.raises(IntegrityRemediationBlocked):
        remediation._apply_stale(stage4106.db, None, None, finding, owner())


def test_incomplete_recording_uses_exact_confirmed_delete_contract(stage4106):
    camera = add_camera(stage4106)
    segment, path = add_segment(stage4106, camera, name="incomplete.mkv", status="failed")
    scan = run_scan(stage4106, "incomplete-delete")
    finding = next(item for item in active_findings(stage4106, scan["scan_id"]) if item.segment_id == segment.id)
    assert finding.category == "partial_file"
    assert finding.action_key == "delete_unusable_recording"
    plan = create_remediation_plan(
        stage4106.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=owner(),
        idempotency_key="incomplete-delete-plan",
    )
    with pytest.raises(IntegrityRemediationBlocked):
        apply_remediation_plan(
            stage4106.db,
            plan_id=plan["plan_id"],
            actor=owner(),
            confirm=False,
            operation_id="incomplete-delete-not-confirmed",
        )
    applied = apply_remediation_plan(
        stage4106.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="incomplete-delete-confirmed",
    )
    assert applied["state"] == "completed"
    assert not path.exists()
    assert stage4106.db.get(RecordingSegment, segment.id).status == "deleted"


def test_simulated_five_hundred_stale_candidates_converge_without_five_hundred_actions(stage4106, monkeypatch):
    camera = add_camera(stage4106)
    for index in range(500):
        add_segment(stage4106, camera, name=f"bulk-{index:03d}.mkv", status="writing", commit=False)
    stage4106.db.commit()
    calls = {"count": 0}

    def simulated_convergence(*_args, **_kwargs):
        calls["count"] += 1
        return ("finalized", "finalized")

    monkeypatch.setattr(integrity, "_converge_stale_segment", simulated_convergence)
    scan = run_scan(stage4106, "bulk-500")
    assert calls["count"] == 500
    assert active_findings(stage4106, scan["scan_id"]) == []
