import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.archive_integrity import (
    ArchiveIntegrityDirectoryWork,
    ArchiveIntegrityFinding,
    ArchiveIntegrityRemediationItem,
    ArchiveIntegrityRemediationPlan,
    ArchiveIntegrityScan,
    RecorderFileReceipt,
)
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.system_settings import SystemSettings
from app.models.storage_operation import StorageOperation
from app.services import archive_integrity as integrity
from app.services import archive_integrity_remediation as remediation
from app.services.archive_integrity import (
    CATEGORY_CONTRACT,
    _bounded_fingerprint,
    _classify_filesystem_entry,
    _classify_metadata_page,
    _refresh_scan_summary,
    _reset_interrupted_directory_work,
    _root_access_identity,
    _root_public_snapshot,
    _stable_object_key,
    cancel_integrity_scan,
    cleanup_old_integrity_generations,
    latest_integrity_scan,
    list_integrity_findings,
    run_integrity_worker_once,
    start_integrity_scan,
)
from app.services.archive_integrity_remediation import (
    IntegrityRemediationBlocked,
    apply_remediation_plan,
    create_remediation_plan,
    recover_pending_remediation_once,
)
from app.services.recording_storage import archive_root_runtime_access_state
from app.services.schema_migrations import STAGE4103_ARCHIVE_INTEGRITY_MIGRATION, STAGE4103_TABLES
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from app.services.storage_monitoring import (
    _build_storage_operations_summary,
    build_lightweight_storage_monitoring_summary,
)
from app.services.storage_operations_foundation import (
    StorageOperationContractError,
    StorageOperationLeaseLost,
)


class Heartbeat:
    def touch(self, *, force=False):
        return force


def owner(user_id=4103):
    return SimpleNamespace(id=user_id, username=f"stage4103-owner-{user_id}", role="owner", is_active=True)


def diagnostics_only(user_id=4104):
    return SimpleNamespace(id=user_id, username=f"stage4103-diagnostics-{user_id}", role="viewer", is_active=True)


@pytest.fixture
def stage4103(tmp_path, monkeypatch):
    original = {
        "storage_root": settings.storage_root,
        "storage_previews": settings.storage_previews,
        "storage_exports": settings.storage_exports,
    }
    archive = tmp_path / "archive-a"
    namespace = archive / "kmvms" / "recordings"
    namespace.mkdir(parents=True)
    settings.storage_root = str(archive)
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")

    engine = create_engine(
        f"sqlite:///{tmp_path / 'stage4103.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    root = ArchiveRoot(
        id="stage4103-root-a",
        label="Volume A",
        root_path=str(archive),
        storage_namespace="kmvms/recordings",
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="fs-stage4103-volume-a",
    )
    db.add_all(
        [
            SystemSettings(
                system_initialized=True,
                timezone="UTC",
                language="ru",
                storage_path=str(archive),
                recording_format="mkv",
            ),
            root,
        ]
    )
    db.commit()
    monkeypatch.setattr(integrity, "SessionLocal", Session)
    monkeypatch.setattr(integrity, "_safe_probe", lambda _path: (True, "probe_ok"))
    integrity._worker_stop.clear()
    try:
        yield SimpleNamespace(
            db=db,
            engine=engine,
            Session=Session,
            root=root,
            archive=archive,
            namespace=namespace,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
        )
    finally:
        integrity._worker_stop.clear()
        db.close()
        engine.dispose()
        settings.storage_root = original["storage_root"]
        settings.storage_previews = original["storage_previews"]
        settings.storage_exports = original["storage_exports"]


def add_camera(ctx, *, name="Camera A", soft_deleted=False):
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
        retention_days=30,
        storage_quota_gb=100,
        status="disabled",
        deleted_at=datetime.utcnow() if soft_deleted else None,
    )
    ctx.db.add(camera)
    ctx.db.commit()
    ctx.db.refresh(camera)
    return camera


def add_segment(
    ctx,
    camera,
    *,
    name="segment.mkv",
    create_file=True,
    content=b"valid-media",
    status="finalized",
    ownership="KM VMS",
    source="recorder",
    deleted=False,
    age_minutes=45,
):
    relative = f"kmvms/recordings/camera_{camera.id}/{name}"
    path = ctx.archive / relative
    if create_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        old = datetime.utcnow() - timedelta(minutes=age_minutes)
        timestamp = old.timestamp()
        path.touch()
        import os

        os.utime(path, (timestamp, timestamp))
    old = datetime.utcnow() - timedelta(minutes=age_minutes)
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(path),
        relative_path=relative,
        started_at=old,
        ended_at=old + timedelta(minutes=5) if status == "finalized" else None,
        finalized_at=old + timedelta(minutes=5) if status == "finalized" else None,
        duration_sec=300 if status == "finalized" else 0,
        size_bytes=len(content) if create_file else 0,
        stream_type="main",
        status="deleted" if deleted else status,
        ownership=ownership,
        source=source,
        archive_root_id=ctx.root.id,
        archive_root_resolution_status="resolved",
        archive_root_resolution_detail="stage4103-test",
        archive_root_resolved_at=old,
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status="ok" if status == "finalized" else status,
        reconciliation_status="ok_owned_finalized" if status == "finalized" else status,
        deleted_at=old if deleted else None,
        created_at=old,
        updated_at=old,
    )
    ctx.db.add(segment)
    ctx.db.commit()
    ctx.db.refresh(segment)
    return segment, path


def run_scan(ctx, *, actor=None, key="stage4103-scan"):
    started = start_integrity_scan(ctx.db, actor=actor or owner(), idempotency_key=key)
    assert started["status"] == "queued"
    assert run_integrity_worker_once() is True
    ctx.db.expire_all()
    return integrity.get_integrity_scan(ctx.db, started["scan_id"])


def active_findings(ctx, scan_id):
    return (
        ctx.db.query(ArchiveIntegrityFinding)
        .filter(ArchiveIntegrityFinding.scan_id == scan_id, ArchiveIntegrityFinding.is_active.is_(True))
        .order_by(ArchiveIntegrityFinding.category.asc(), ArchiveIntegrityFinding.id.asc())
        .all()
    )


def expire_operation(ctx, operation_id):
    operation = ctx.db.get(StorageOperation, str(operation_id))
    operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    ctx.db.add(operation)
    ctx.db.commit()
    ctx.db.expire_all()


def eligible_orphan(ctx, *, name: str, receipt_id: str, key_prefix: str):
    path = ctx.namespace / name
    path.write_bytes(f"owned-{name}".encode("utf-8"))
    old = datetime.utcnow() - timedelta(hours=2)
    import os

    os.utime(path, (old.timestamp(), old.timestamp()))
    stat_result = path.stat()
    relative = f"kmvms/recordings/{name}"
    ctx.db.add(
        RecorderFileReceipt(
            id=receipt_id,
            contract_version=1,
            segment_id=999999,
            root_id=ctx.root.id,
            physical_identity=ctx.root.physical_identity,
            relative_path=relative,
            state="finalized",
            object_identity=f"receipt-{name}",
            device_id=str(stat_result.st_dev),
            inode=str(stat_result.st_ino),
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            content_fingerprint=_bounded_fingerprint(path, stat_result),
            finalized_at=old,
        )
    )
    ctx.db.commit()
    first = run_scan(ctx, key=f"{key_prefix}-first")
    first_finding = next(row for row in active_findings(ctx, first["scan_id"]) if row.display_name == path.name)
    first_finding.first_observed_at = datetime.utcnow() - integrity.ORPHAN_OBSERVATION_GRACE - timedelta(seconds=1)
    ctx.db.add(first_finding)
    ctx.db.commit()
    second = run_scan(ctx, key=f"{key_prefix}-second")
    finding = next(row for row in active_findings(ctx, second["scan_id"]) if row.display_name == path.name)
    return path, finding


def test_lightweight_status_bridge_preserves_durable_integrity_truth(stage4103):
    summary = _build_storage_operations_summary(
        stage4103.db,
        {
            "reconciliation_summary": {
                "status": "completed",
                "evidence_status": "completed",
                "source": "durable_archive_integrity_scan",
                "scan_id": "scan-public-1",
                "active": False,
                "phase": "completed",
                "checked_count": 1049,
                "failed_count": 0,
                "problem_file_count": 11,
                "category_counts": {"partial_file": 5, "pre_metadata_km_vms_file": 6},
                "last_checked_at": "2026-07-13T10:24:41",
            }
        },
    )

    reconciliation = summary["reconciliation"]
    assert reconciliation["status"] == "completed"
    assert reconciliation["evidence_status"] == "completed"
    assert reconciliation["problem_file_count"] == 11
    assert reconciliation["category_counts"] == {"partial_file": 5, "pre_metadata_km_vms_file": 6}
    assert reconciliation["checked_count"] == 1049
    assert reconciliation["failed_count"] == 0
    assert reconciliation["partial"] is False


def test_residual_partial_recording_is_not_presented_as_an_active_write():
    contract = CATEGORY_CONTRACT["partial_file"]
    assert contract["impact"] == "recording_incomplete"
    assert contract["action"] == "delete_unusable_recording"
    assert contract["permission"] == "delete_recordings"


def test_start_is_durable_fast_idempotent_and_coalesces(stage4103):
    first = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key="same-request")
    replay = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key="same-request")
    coalesced = start_integrity_scan(stage4103.db, actor=owner(99), idempotency_key="other-request")
    assert first["status"] == "queued"
    assert replay["scan_id"] == first["scan_id"] and replay["replayed"] is True
    assert coalesced["scan_id"] == first["scan_id"] and coalesced["coalesced"] is True
    assert stage4103.db.query(ArchiveIntegrityScan).count() == 1


def test_restore_invalidation_makes_restored_integrity_truth_non_executable(stage4103):
    camera = add_camera(stage4103)
    add_segment(stage4103, camera, name="restore-missing.mkv", create_file=False)
    result = run_scan(stage4103, key="restore-invalidation-scan")
    finding = active_findings(stage4103, result["scan_id"])[0]
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="retire_missing_recording",
        actor=owner(),
        idempotency_key="restore-invalidation-plan",
    )
    scan = stage4103.db.get(ArchiveIntegrityScan, result["scan_id"])
    historical_found_count = int(scan.found_count or 0)
    historical_categories = dict(scan.category_summary or {})
    historical_impacts = dict(scan.impact_summary or {})
    historical_roots = dict(scan.root_summary or {})
    operation = stage4103.db.get(StorageOperation, scan.operation_id)
    directory_work = (
        stage4103.db.query(ArchiveIntegrityDirectoryWork)
        .filter(ArchiveIntegrityDirectoryWork.scan_id == scan.id)
        .first()
    )
    scan.status = "running"
    scan.phase = "filesystem"
    scan.active_slot = integrity.SCAN_ACTIVE_SLOT
    scan.is_stale = False
    operation.status = "running"
    operation.owner_instance_id = "restored-worker"
    operation.owner_token_hash = "a" * 64
    if directory_work is not None:
        directory_work.status = "claimed"
        directory_work.owner_instance_id = "restored-worker"
    stage4103.db.commit()
    stage4103.monkeypatch.setattr(
        integrity,
        "_scan_roots",
        lambda _db: pytest.fail("restore invalidation must not read archive roots"),
    )

    invalidated = integrity.invalidate_integrity_truth_after_restore(
        stage4103.db,
        restore_operation_id="restore-" + ("a" * 32),
        final_db_outcome="source",
    )
    replay = integrity.invalidate_integrity_truth_after_restore(
        stage4103.db,
        restore_operation_id="restore-" + ("a" * 32),
        final_db_outcome="source",
    )
    stage4103.db.expire_all()

    scan = stage4103.db.get(ArchiveIntegrityScan, result["scan_id"])
    operation = stage4103.db.get(StorageOperation, scan.operation_id)
    persisted_finding = stage4103.db.get(ArchiveIntegrityFinding, finding.id)
    persisted_plan = stage4103.db.get(
        ArchiveIntegrityRemediationPlan,
        plan["plan_id"],
    )
    assert invalidated["archive_roots_read"] is False
    assert replay["active_scan_count"] == 0
    assert scan.status == "failed" and scan.is_stale is True
    assert scan.active_slot is None
    assert operation.status == "failed"
    assert operation.owner_instance_id is None
    assert persisted_finding.is_active is False
    assert persisted_finding.state == "superseded"
    assert persisted_plan.state == "blocked"
    assert int(scan.found_count or 0) == historical_found_count
    assert dict(scan.category_summary or {}) == historical_categories
    assert dict(scan.impact_summary or {}) == historical_impacts
    assert dict(scan.root_summary or {}) == historical_roots

    current_scan = latest_integrity_scan(stage4103.db)
    assert current_scan["stale"] is True
    assert current_scan["progress"]["found_count"] == 0
    assert current_scan["category_counts"] == {}
    assert current_scan["impact_counts"] == {}
    assert current_scan["root_counts"] == {}
    current_findings = list_integrity_findings(
        stage4103.db,
        scan.id,
        role="owner",
    )
    assert current_findings["items"] == []
    assert current_findings["has_more"] is False

    current_summary = integrity.latest_integrity_summary_for_status(stage4103.db)
    assert current_summary["evidence_status"] == "stale"
    assert current_summary["problem_count"] == 0
    assert current_summary["problem_file_count"] == 0
    assert current_summary["category_counts"] == {}
    assert current_summary["active"] is False

    storage_summary = build_lightweight_storage_monitoring_summary(stage4103.db)
    assert storage_summary["reconciliation_summary"]["evidence_status"] == "stale"
    assert storage_summary["reconciliation_summary"]["problem_file_count"] == 0
    assert storage_summary["reconciliation_summary"]["category_counts"] == {}
    assert storage_summary["status"] != "degraded"
    if directory_work is not None:
        persisted_work = stage4103.db.get(
            ArchiveIntegrityDirectoryWork,
            directory_work.id,
        )
        assert persisted_work.status == "failed"
        assert persisted_work.owner_instance_id is None


def test_phase_progress_separates_metadata_and_filesystem_counts(stage4103):
    camera = add_camera(stage4103)
    add_segment(stage4103, camera, name="phase-progress.mkv")
    result = run_scan(stage4103, key="phase-progress-scan")
    progress = result["progress"]
    assert progress["planned_count"] == 1
    assert progress["metadata_checked_count"] == 1
    assert progress["filesystem_checked_count"] >= 1
    assert progress["checked_count"] == (
        progress["metadata_checked_count"]
        + progress["filesystem_checked_count"]
    )


def test_soft_deleted_camera_retained_archive_is_scanned(stage4103):
    camera = add_camera(stage4103, soft_deleted=True)
    segment, path = add_segment(stage4103, camera)
    result = run_scan(stage4103)
    assert result["status"] == "completed"
    assert result["progress"]["checked_count"] >= 1
    assert active_findings(stage4103, result["scan_id"]) == []
    assert path.exists()
    assert stage4103.db.get(RecordingSegment, segment.id).deleted_at is None


def test_finally_retired_segment_is_excluded(stage4103):
    camera = add_camera(stage4103)
    add_segment(stage4103, camera, create_file=False, deleted=True)
    result = run_scan(stage4103)
    assert result["progress"]["planned_count"] == 0
    assert result["category_counts"] == {}


def test_deterministic_precedence_produces_one_metadata_finding(stage4103):
    camera = add_camera(stage4103)
    zero, _path = add_segment(stage4103, camera, name="zero.mkv", content=b"")
    missing, _ = add_segment(stage4103, camera, name="missing.mkv", create_file=False)
    stale, _ = add_segment(stage4103, camera, name="stale.mkv", status="writing")
    stage4103.monkeypatch.setattr(integrity, "_safe_probe", lambda _path: (False, "probe_failed"))
    result = run_scan(stage4103)
    by_segment = {row.segment_id: row.category for row in active_findings(stage4103, result["scan_id"]) if row.segment_id}
    assert by_segment[zero.id] == "zero_size_file"
    assert by_segment[missing.id] == "missing_file"
    assert by_segment[stale.id] == "stale_writing_segment"
    assert len([row for row in active_findings(stage4103, result["scan_id"]) if row.segment_id == zero.id]) == 1


def test_recent_and_authoritatively_active_objects_are_not_actionable(stage4103):
    camera = add_camera(stage4103)
    recent, _ = add_segment(stage4103, camera, name="recent.mkv", status="writing", age_minutes=1)
    result = run_scan(stage4103)
    assert all(row.segment_id != recent.id for row in active_findings(stage4103, result["scan_id"]))


def test_unavailable_root_creates_one_root_finding_without_missing_fanout(stage4103):
    camera = add_camera(stage4103)
    add_segment(stage4103, camera, name="one.mkv", create_file=False)
    add_segment(stage4103, camera, name="two.mkv", create_file=False)
    stage4103.namespace.rmdir()
    result = run_scan(stage4103)
    rows = active_findings(stage4103, result["scan_id"])
    assert [row.category for row in rows] == ["storage_unavailable"]
    assert rows[0].segment_id is None


def test_unproven_historical_file_never_becomes_deletable_orphan(stage4103):
    path = stage4103.namespace / "legacy.mkv"
    path.write_bytes(b"legacy")
    old = datetime.utcnow() - timedelta(hours=2)
    import os

    os.utime(path, (old.timestamp(), old.timestamp()))
    first = run_scan(stage4103, key="legacy-first")
    second = run_scan(stage4103, key="legacy-second")
    rows = active_findings(stage4103, second["scan_id"])
    legacy = next(row for row in rows if row.display_name == "legacy.mkv")
    assert legacy.category == "pre_metadata_km_vms_file"
    assert legacy.action_key is None
    assert legacy.observation_count >= 2
    assert path.exists()
    assert first["status"] == "completed"


def test_exact_recorder_receipt_is_required_for_orphan_eligibility(stage4103):
    path, orphan = eligible_orphan(
        stage4103,
        name="future-orphan.mkv",
        receipt_id="41030000-0000-0000-0000-000000000001",
        key_prefix="receipt",
    )
    assert orphan.category == "orphan_file"
    assert orphan.action_key == "delete_proven_orphan"
    assert orphan.observation_count >= 2

    plan = create_remediation_plan(
        stage4103.db,
        finding_id=orphan.id,
        action_key="delete_proven_orphan",
        actor=owner(),
        idempotency_key="orphan-delete-plan",
    )
    applied = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-orphan-delete-4103",
    )
    audit_count = stage4103.db.query(AuditEvent).filter(AuditEvent.event_type.like("archive_integrity.remediation_%")).count()
    replay = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-orphan-delete-replay-4103",
    )
    assert applied["state"] == "completed"
    assert replay["state"] == "completed" and replay["replayed"] is True
    assert path.exists() is False
    assert stage4103.db.query(AuditEvent).filter(AuditEvent.event_type.like("archive_integrity.remediation_%")).count() == audit_count


def test_same_name_orphan_replacement_is_not_deleted(stage4103):
    path, orphan = eligible_orphan(
        stage4103,
        name="replace-orphan.mkv",
        receipt_id="41030000-0000-0000-0000-000000000002",
        key_prefix="replacement",
    )
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=orphan.id,
        action_key="delete_proven_orphan",
        actor=owner(),
        idempotency_key="orphan-replacement-plan",
    )
    path.unlink()
    path.write_bytes(b"same-name-replacement")
    blocked = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-orphan-replacement-4103",
    )
    audit_count = stage4103.db.query(AuditEvent).filter(AuditEvent.event_type.like("archive_integrity.remediation_%")).count()
    replay = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-orphan-replacement-replay-4103",
    )
    assert blocked["state"] == "blocked"
    assert replay["state"] == "blocked" and replay["replayed"] is True
    assert path.read_bytes() == b"same-name-replacement"
    assert stage4103.db.query(AuditEvent).filter(AuditEvent.event_type.like("archive_integrity.remediation_%")).count() == audit_count


def test_partial_uniqueness_rejects_duplicate_active_findings(stage4103):
    camera = add_camera(stage4103)
    segment, _ = add_segment(stage4103, camera, create_file=False)
    result = run_scan(stage4103)
    existing = next(row for row in active_findings(stage4103, result["scan_id"]) if row.segment_id == segment.id)
    duplicate = ArchiveIntegrityFinding(
        id="41030000-0000-0000-0000-000000000099",
        scan_id=result["scan_id"],
        finding_scope="metadata",
        category="corrupted_file",
        severity="error",
        impact_key="recording_unplayable",
        segment_id=segment.id,
        observed_facts={},
        is_active=True,
        state="active",
    )
    stage4103.db.add(duplicate)
    with pytest.raises(IntegrityError):
        stage4103.db.commit()
    stage4103.db.rollback()
    assert stage4103.db.get(ArchiveIntegrityFinding, existing.id) is not None


def test_cursor_pagination_is_bounded_safe_and_cross_generation_isolated(stage4103):
    camera = add_camera(stage4103)
    for index in range(4):
        add_segment(stage4103, camera, name=f"missing-{index}.mkv", create_file=False)
    result = run_scan(stage4103)
    first = list_integrity_findings(stage4103.db, result["scan_id"], role="owner", limit=2)
    second = list_integrity_findings(
        stage4103.db,
        result["scan_id"],
        role="owner",
        limit=2,
        cursor=first["next_cursor"],
    )
    first_ids = {row["finding_id"] for row in first["items"]}
    second_ids = {row["finding_id"] for row in second["items"]}
    assert len(first["items"]) == 2 and first["has_more"] is True
    assert first_ids.isdisjoint(second_ids)
    assert all("relative_ref" not in row and "observed_facts" not in row for row in first["items"] + second["items"])
    assert all("/" not in str(row.get("display_name") or "") for row in first["items"] + second["items"])


def test_missing_retirement_is_exact_permissioned_metadata_only_and_replay_safe(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(stage4103, camera, create_file=False)
    result = run_scan(stage4103)
    finding = next(row for row in active_findings(stage4103, result["scan_id"]) if row.segment_id == segment.id)
    with pytest.raises(IntegrityRemediationBlocked):
        create_remediation_plan(
            stage4103.db,
            finding_id=finding.id,
            action_key="retire_missing_recording",
            actor=diagnostics_only(),
            idempotency_key="permission-denied",
        )
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="retire_missing_recording",
        actor=owner(),
        idempotency_key="missing-plan",
    )
    applied = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-apply-missing-4103",
    )
    replay = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-apply-missing-replay-4103",
    )
    stage4103.db.expire_all()
    retired = stage4103.db.get(RecordingSegment, segment.id)
    resolved = stage4103.db.get(ArchiveIntegrityFinding, finding.id)
    assert applied["state"] == "completed" and replay["replayed"] is True
    assert retired.status == "deleted" and retired.deleted_at is not None
    assert path.exists() is False
    assert resolved.is_active is False and resolved.state == "resolved" and resolved.resolved_at is not None
    assert stage4103.db.get(ArchiveIntegrityScan, result["scan_id"]).found_count == 0


def test_stable_failed_missing_recording_can_be_retired_but_reappeared_file_blocks(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(
        stage4103,
        camera,
        name="failed-missing.mkv",
        create_file=False,
        status="failed",
    )
    result = run_scan(stage4103, key="failed-missing-scan")
    finding = next(
        row
        for row in active_findings(stage4103, result["scan_id"])
        if row.segment_id == segment.id
    )
    blocked_plan = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="retire_missing_recording",
        actor=owner(),
        idempotency_key="failed-missing-reappeared-plan",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"recording-reappeared")
    blocked = apply_remediation_plan(
        stage4103.db,
        plan_id=blocked_plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="failed-missing-reappeared-apply",
    )
    assert blocked["state"] == "blocked"
    assert blocked["reason_code"] == "archive_integrity_missing_file_reappeared"
    path.unlink()

    fresh = run_scan(stage4103, key="failed-missing-fresh-scan")
    fresh_finding = next(
        row
        for row in active_findings(stage4103, fresh["scan_id"])
        if row.segment_id == segment.id
    )
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=fresh_finding.id,
        action_key="retire_missing_recording",
        actor=owner(),
        idempotency_key="failed-missing-retire-plan",
    )
    applied = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="failed-missing-retire-apply",
    )
    stage4103.db.expire_all()
    retired = stage4103.db.get(RecordingSegment, segment.id)
    assert applied["state"] == "completed"
    assert retired.status == "deleted" and retired.deleted_at is not None


def test_stale_writing_without_exact_receipt_stays_unresolved_without_false_action(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(stage4103, camera, name="stale-writing.mkv", status="writing")
    result = run_scan(stage4103, key="stale-writing-scan")
    finding = next(row for row in active_findings(stage4103, result["scan_id"]) if row.segment_id == segment.id)
    assert finding.category == "stale_writing_segment"
    assert finding.action_key is None
    assert finding.no_action_reason == "automatic_reconciliation_pending"
    with pytest.raises(IntegrityRemediationBlocked):
        create_remediation_plan(
            stage4103.db,
            finding_id=finding.id,
            action_key="mark_stale_recording",
            actor=owner(),
            idempotency_key="stale-action-not-published",
        )
    stage4103.db.expire_all()
    updated = stage4103.db.get(RecordingSegment, segment.id)
    assert updated.status == "writing"
    assert path.exists()


@pytest.mark.parametrize(
    ("name", "content", "category"),
    (("zero.mkv", b"", "zero_size_file"), ("corrupt.mkv", b"corrupt", "corrupted_file")),
)
def test_trusted_unusable_recording_uses_exact_immutable_deletion_contract(stage4103, name, content, category):
    camera = add_camera(stage4103)
    segment, _path = add_segment(stage4103, camera, name=name, content=content)
    if category == "corrupted_file":
        stage4103.monkeypatch.setattr(integrity, "_safe_probe", lambda _path: (False, "probe_failed"))
        stage4103.monkeypatch.setattr(remediation, "_safe_probe", lambda _path: (False, "probe_failed"))
    result = run_scan(stage4103, key=f"{category}-scan")
    finding = next(row for row in active_findings(stage4103, result["scan_id"]) if row.segment_id == segment.id)
    assert finding.category == category
    assert finding.action_key == "delete_unusable_recording"
    calls = []

    def execute_exact(db, segments, **kwargs):
        calls.append((list(segments), kwargs))
        return {"status": "completed", "deleted_count": 1, "failed_count": 0, "skipped_count": 0, "bytes_freed": 0}

    stage4103.monkeypatch.setattr(remediation, "execute_segments", execute_exact)
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=owner(),
        idempotency_key=f"{category}-plan",
    )
    applied = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id=f"integrity-{category}-apply-4103",
    )
    replay = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id=f"integrity-{category}-replay-4103",
    )
    assert applied["state"] == "completed"
    assert replay["state"] == "completed" and replay["replayed"] is True
    assert len(calls) == 1
    called_segments, kwargs = calls[0]
    assert [row.id for row in called_segments] == [segment.id]
    assert kwargs["scope"]["segment_ids"] == [segment.id]
    assert kwargs["expected_identities"][segment.id]["relative_path"] == segment.relative_path
    assert category in kwargs["allowed_integrity_statuses"]
    assert kwargs["manage_outer_operation"] is False


def test_metadata_remediation_and_outer_terminal_are_one_atomic_fenced_transaction(stage4103):
    camera = add_camera(stage4103)
    segment, _ = add_segment(stage4103, camera, name="atomic-missing.mkv", create_file=False)
    scan = run_scan(stage4103, key="atomic-missing-scan")
    finding = next(row for row in active_findings(stage4103, scan["scan_id"]) if row.segment_id == segment.id)
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="retire_missing_recording",
        actor=owner(),
        idempotency_key="atomic-missing-plan",
    )
    original_terminal = remediation.stage_operation_terminal
    stage4103.monkeypatch.setattr(
        remediation,
        "stage_operation_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StorageOperationLeaseLost("forced_lease_loss")),
    )

    interrupted = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-atomic-missing-4103",
    )
    stage4103.db.expire_all()
    persisted_plan = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"])
    assert interrupted["state"] == "running"
    assert stage4103.db.get(RecordingSegment, segment.id).deleted_at is None
    assert stage4103.db.get(ArchiveIntegrityFinding, finding.id).is_active is True
    assert persisted_plan.state == "running"

    stage4103.monkeypatch.setattr(remediation, "stage_operation_terminal", original_terminal)
    expire_operation(stage4103, persisted_plan.apply_operation_id)
    recovered = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="ignored-new-operation-id-4103",
    )
    stage4103.db.expire_all()
    operation = stage4103.db.get(StorageOperation, persisted_plan.apply_operation_id)
    assert recovered["state"] == "completed"
    assert stage4103.db.get(RecordingSegment, segment.id).status == "deleted"
    assert stage4103.db.get(ArchiveIntegrityFinding, finding.id).is_active is False
    assert operation.status == "completed"
    assert operation.result["retired_count"] == 1


def test_generic_terminal_exception_after_committed_physical_outcome_keeps_exact_truth(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(stage4103, camera, name="generic-zero.mkv", content=b"")
    scan = run_scan(stage4103, key="generic-zero-scan")
    finding = next(row for row in active_findings(stage4103, scan["scan_id"]) if row.segment_id == segment.id)
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=owner(),
        idempotency_key="generic-zero-plan",
    )
    mutations = []

    def committed_delete(db, segments, **_kwargs):
        mutations.append(int(segments[0].id))
        path.unlink()
        fresh = db.get(RecordingSegment, int(segments[0].id))
        fresh.status = "deleted"
        fresh.deleted_at = datetime.utcnow()
        db.add(fresh)
        db.commit()
        return {"status": "completed", "deleted_count": 1, "failed_count": 0, "skipped_count": 0, "bytes_freed": 0}

    original_terminal = remediation.stage_operation_terminal
    failures = {"remaining": 1}

    def fail_once(*args, **kwargs):
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise RuntimeError("forced_terminal_write_failure")
        return original_terminal(*args, **kwargs)

    stage4103.monkeypatch.setattr(remediation, "execute_segments", committed_delete)
    stage4103.monkeypatch.setattr(remediation, "stage_operation_terminal", fail_once)
    applied = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-generic-zero-4103",
    )
    stage4103.db.expire_all()
    persisted = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"])
    operation = stage4103.db.get(StorageOperation, persisted.apply_operation_id)
    assert applied["state"] == "completed"
    assert mutations == [segment.id]
    assert path.exists() is False
    assert persisted.result_summary["deleted_count"] == 1
    assert operation.status == "completed" and operation.result["deleted_count"] == 1
    assert operation.result.get("mutated_count") is None


def test_terminal_pending_restart_reclaims_exact_outcome_without_repeat_or_duplicate_audit(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(stage4103, camera, name="restart-zero.mkv", content=b"")
    scan = run_scan(stage4103, key="restart-zero-scan")
    finding = next(row for row in active_findings(stage4103, scan["scan_id"]) if row.segment_id == segment.id)
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=owner(),
        idempotency_key="restart-zero-plan",
    )
    mutations = []

    def committed_delete(db, segments, **_kwargs):
        mutations.append(int(segments[0].id))
        path.unlink()
        fresh = db.get(RecordingSegment, int(segments[0].id))
        fresh.status = "deleted"
        fresh.deleted_at = datetime.utcnow()
        db.add(fresh)
        db.commit()
        return {"status": "completed", "deleted_count": 1, "failed_count": 0, "skipped_count": 0, "bytes_freed": 0}

    original_terminal = remediation.stage_operation_terminal
    stage4103.monkeypatch.setattr(remediation, "execute_segments", committed_delete)
    stage4103.monkeypatch.setattr(
        remediation,
        "stage_operation_terminal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StorageOperationLeaseLost("forced_post_mutation_loss")),
    )
    pending = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-restart-zero-4103",
    )
    stage4103.db.expire_all()
    persisted = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"])
    assert pending["state"] == "running"
    assert persisted.state == remediation.TERMINAL_PENDING_PLAN_STATE
    assert persisted.result_summary["deleted_count"] == 1
    assert mutations == [segment.id]

    stage4103.monkeypatch.setattr(remediation, "stage_operation_terminal", original_terminal)
    expire_operation(stage4103, persisted.apply_operation_id)
    assert recover_pending_remediation_once(stage4103.db) is True
    stage4103.db.expire_all()
    persisted = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"])
    item = stage4103.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan["plan_id"]).one()
    operation = stage4103.db.get(StorageOperation, persisted.apply_operation_id)
    audit_count = stage4103.db.query(AuditEvent).filter(
        AuditEvent.event_type == "archive_integrity.remediation_completed",
        AuditEvent.target_id == plan["plan_id"],
    ).count()
    replay = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="ignored-restart-operation-4103",
    )
    assert persisted.state == item.state == operation.status == "completed"
    assert persisted.result_summary["deleted_count"] == operation.result["deleted_count"] == 1
    assert replay["state"] == "completed" and replay["replayed"] is True
    assert mutations == [segment.id]
    assert stage4103.db.query(AuditEvent).filter(
        AuditEvent.event_type == "archive_integrity.remediation_completed",
        AuditEvent.target_id == plan["plan_id"],
    ).count() == audit_count == 1
    with pytest.raises(StorageOperationContractError, match="physical_outcome_conflict"):
        remediation._persist_physical_outcome(
            stage4103.db,
            plan=persisted,
            item=item,
            result={"status": "partial", "deleted_count": 0, "failed_count": 1},
        )
    stage4103.db.expire_all()
    assert stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"]).state == "completed"


def test_unusable_recovery_fails_closed_when_root_changes_before_absence_observation(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(stage4103, camera, name="root-loss-zero.mkv", content=b"")
    scan = run_scan(stage4103, key="root-loss-zero-scan")
    finding = next(row for row in active_findings(stage4103, scan["scan_id"]) if row.segment_id == segment.id)
    plan_public = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=owner(),
        idempotency_key="root-loss-zero-plan",
    )
    plan = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan_public["plan_id"])
    item = stage4103.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan.id).one()
    plan.state = "running"
    item.state = "physical_mutation_prepared"
    stage4103.db.add_all((plan, item))
    stage4103.db.commit()

    original_root_for_finding = remediation._root_for_finding
    detached = stage4103.tmp_path / "archive-a-detached-unusable"
    swapped = {"done": False}

    def root_then_replace(db, current_finding):
        root = original_root_for_finding(db, current_finding)
        if not swapped["done"]:
            stage4103.archive.rename(detached)
            (stage4103.archive / "kmvms" / "recordings").mkdir(parents=True)
            swapped["done"] = True
        return root

    stage4103.monkeypatch.setattr(remediation, "_root_for_finding", root_then_replace)
    try:
        with pytest.raises(IntegrityRemediationBlocked, match="archive_integrity_root_access_changed"):
            remediation._recover_unusable_outcome(stage4103.db, plan, item, finding, owner())
    finally:
        stage4103.db.rollback()
        if swapped["done"]:
            (stage4103.archive / "kmvms" / "recordings").rmdir()
            (stage4103.archive / "kmvms").rmdir()
            stage4103.archive.rmdir()
            detached.rename(stage4103.archive)

    stage4103.monkeypatch.setattr(remediation, "_root_for_finding", original_root_for_finding)
    stage4103.db.expire_all()
    persisted_segment = stage4103.db.get(RecordingSegment, segment.id)
    persisted_finding = stage4103.db.get(ArchiveIntegrityFinding, finding.id)
    persisted_item = stage4103.db.get(ArchiveIntegrityRemediationItem, item.id)
    assert persisted_segment.status == "finalized"
    assert persisted_finding.is_active is True
    assert persisted_item.state == "physical_mutation_prepared"
    assert path.exists()
    assert remediation._recover_unusable_outcome(
        stage4103.db,
        stage4103.db.get(ArchiveIntegrityRemediationPlan, plan.id),
        persisted_item,
        persisted_finding,
        owner(),
    ) is None


def test_unusable_recovery_accepts_absence_only_on_the_verified_root(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(stage4103, camera, name="stable-absence-zero.mkv", content=b"")
    scan = run_scan(stage4103, key="stable-absence-zero-scan")
    finding = next(row for row in active_findings(stage4103, scan["scan_id"]) if row.segment_id == segment.id)
    plan_public = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=owner(),
        idempotency_key="stable-absence-zero-plan",
    )
    plan = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan_public["plan_id"])
    item = stage4103.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan.id).one()
    plan.state = "running"
    item.state = "physical_mutation_prepared"
    stage4103.db.add_all((plan, item))
    stage4103.db.commit()
    path.unlink()

    outcome = remediation._recover_unusable_outcome(stage4103.db, plan, item, finding, owner())

    assert outcome["status"] == "completed"
    assert outcome["deleted_count"] == 1
    assert stage4103.db.get(RecordingSegment, segment.id).status == "deleted"


def test_unusable_recovery_waits_for_root_return_then_converges_without_second_mutation(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(stage4103, camera, name="root-return-zero.mkv", content=b"")
    scan = run_scan(stage4103, key="root-return-zero-scan")
    finding = next(row for row in active_findings(stage4103, scan["scan_id"]) if row.segment_id == segment.id)
    plan_public = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=owner(),
        idempotency_key="root-return-zero-plan",
    )
    mutations = []

    def unlink_then_lose_lease(_db, segments, **_kwargs):
        mutations.append(int(segments[0].id))
        path.unlink()
        raise StorageOperationLeaseLost("forced_after_unusable_unlink")

    stage4103.monkeypatch.setattr(remediation, "execute_segments", unlink_then_lose_lease)
    pending = apply_remediation_plan(
        stage4103.db,
        plan_id=plan_public["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-root-return-zero-4103",
    )
    stage4103.db.expire_all()
    plan = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan_public["plan_id"])
    item = stage4103.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan.id).one()
    assert pending["state"] == "running"
    assert item.state == "physical_mutation_prepared"
    assert mutations == [segment.id]
    assert path.exists() is False

    expire_operation(stage4103, plan.apply_operation_id)
    original_root_for_finding = remediation._root_for_finding
    detached = stage4103.tmp_path / "archive-a-detached-root-return"
    calls = {"count": 0}

    def replace_during_recovery(db, current_finding):
        root = original_root_for_finding(db, current_finding)
        calls["count"] += 1
        if calls["count"] == 1:
            stage4103.archive.rename(detached)
            (stage4103.archive / "kmvms" / "recordings").mkdir(parents=True)
        return root

    stage4103.monkeypatch.setattr(remediation, "_root_for_finding", replace_during_recovery)
    try:
        unavailable = apply_remediation_plan(
            stage4103.db,
            plan_id=plan.id,
            actor=owner(),
            confirm=True,
            operation_id="ignored-root-unavailable-4103",
        )
    finally:
        stage4103.db.rollback()
        (stage4103.archive / "kmvms" / "recordings").rmdir()
        (stage4103.archive / "kmvms").rmdir()
        stage4103.archive.rmdir()
        detached.rename(stage4103.archive)

    stage4103.db.expire_all()
    assert unavailable["state"] == "running"
    assert unavailable["reason_code"] == "archive_integrity_root_access_changed"
    assert stage4103.db.get(RecordingSegment, segment.id).status == "finalized"
    assert stage4103.db.get(ArchiveIntegrityFinding, finding.id).is_active is True
    assert mutations == [segment.id]

    stage4103.monkeypatch.setattr(remediation, "_root_for_finding", original_root_for_finding)
    plan = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan.id)
    expire_operation(stage4103, plan.apply_operation_id)
    completed = apply_remediation_plan(
        stage4103.db,
        plan_id=plan.id,
        actor=owner(),
        confirm=True,
        operation_id="ignored-root-restored-4103",
    )
    stage4103.db.expire_all()
    assert completed["state"] == "completed"
    assert stage4103.db.get(RecordingSegment, segment.id).status == "deleted"
    assert stage4103.db.get(ArchiveIntegrityFinding, finding.id).is_active is False
    assert mutations == [segment.id]


def test_orphan_recovery_fails_closed_when_delete_committing_root_changes(stage4103):
    _path, finding = eligible_orphan(
        stage4103,
        name="root-loss-orphan.mkv",
        receipt_id="41030000-0000-0000-0000-000000000004",
        key_prefix="root-loss-orphan",
    )
    plan_public = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="delete_proven_orphan",
        actor=owner(),
        idempotency_key="root-loss-orphan-plan",
    )
    plan = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan_public["plan_id"])
    item = stage4103.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan.id).one()
    plan.state = "running"
    plan.apply_operation_id = "integrity-root-loss-orphan-4103"
    item.state = "delete_committing"
    stage4103.db.add_all((plan, item))
    stage4103.db.commit()

    original_root_for_finding = remediation._root_for_finding
    detached = stage4103.tmp_path / "archive-a-detached-orphan"
    swapped = {"done": False}

    def root_then_replace(db, current_finding):
        root = original_root_for_finding(db, current_finding)
        if not swapped["done"]:
            stage4103.archive.rename(detached)
            (stage4103.archive / "kmvms" / "recordings").mkdir(parents=True)
            swapped["done"] = True
        return root

    stage4103.monkeypatch.setattr(remediation, "_root_for_finding", root_then_replace)
    stage4103.monkeypatch.setattr(remediation, "assert_operation_owned", lambda *_args, **_kwargs: None)
    try:
        with pytest.raises(IntegrityRemediationBlocked, match="archive_integrity_root_access_changed"):
            remediation._recover_orphan_outcome(stage4103.db, plan, item, finding, object())
    finally:
        stage4103.db.rollback()
        if swapped["done"]:
            (stage4103.archive / "kmvms" / "recordings").rmdir()
            (stage4103.archive / "kmvms").rmdir()
            stage4103.archive.rmdir()
            detached.rename(stage4103.archive)

    stage4103.db.expire_all()
    assert stage4103.db.get(ArchiveIntegrityFinding, finding.id).is_active is True
    assert stage4103.db.get(ArchiveIntegrityRemediationItem, item.id).state == "delete_committing"


def test_orphan_unlink_crash_is_recovered_from_durable_item_without_second_unlink(stage4103):
    path, orphan = eligible_orphan(
        stage4103,
        name="crash-orphan.mkv",
        receipt_id="41030000-0000-0000-0000-000000000003",
        key_prefix="crash-orphan",
    )
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=orphan.id,
        action_key="delete_proven_orphan",
        actor=owner(),
        idempotency_key="crash-orphan-plan",
    )
    original_unlink = remediation.os.unlink
    original_persist = remediation._persist_physical_outcome
    unlink_calls = []
    crash = {"pending": True}

    def tracked_unlink(*args, **kwargs):
        if str(args[0]).startswith("orphan-"):
            unlink_calls.append(str(args[0]))
        return original_unlink(*args, **kwargs)

    def crash_after_unlink(*args, **kwargs):
        if crash["pending"]:
            crash["pending"] = False
            raise StorageOperationLeaseLost("forced_after_orphan_unlink")
        return original_persist(*args, **kwargs)

    stage4103.monkeypatch.setattr(remediation.os, "unlink", tracked_unlink)
    stage4103.monkeypatch.setattr(remediation, "_persist_physical_outcome", crash_after_unlink)
    pending = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-crash-orphan-4103",
    )
    stage4103.db.expire_all()
    persisted = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"])
    item = stage4103.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan["plan_id"]).one()
    assert pending["state"] == "running"
    assert path.exists() is False and item.state == "delete_committing"
    assert len(unlink_calls) == 1

    stage4103.monkeypatch.setattr(remediation, "_persist_physical_outcome", original_persist)
    expire_operation(stage4103, persisted.apply_operation_id)
    assert recover_pending_remediation_once(stage4103.db) is True
    stage4103.db.expire_all()
    assert stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"]).state == "completed"
    assert len(unlink_calls) == 1


def test_partial_physical_outcome_remains_partial_and_does_not_resolve_unmodified_finding(stage4103):
    camera = add_camera(stage4103)
    segment, path = add_segment(stage4103, camera, name="partial-zero.mkv", content=b"")
    scan = run_scan(stage4103, key="partial-zero-scan")
    finding = next(row for row in active_findings(stage4103, scan["scan_id"]) if row.segment_id == segment.id)
    plan = create_remediation_plan(
        stage4103.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=owner(),
        idempotency_key="partial-zero-plan",
    )
    stage4103.monkeypatch.setattr(
        remediation,
        "execute_segments",
        lambda *_args, **_kwargs: {
            "status": "partial",
            "deleted_count": 0,
            "failed_count": 1,
            "skipped_count": 0,
            "bytes_freed": 0,
        },
    )
    applied = apply_remediation_plan(
        stage4103.db,
        plan_id=plan["plan_id"],
        actor=owner(),
        confirm=True,
        operation_id="integrity-partial-zero-4103",
    )
    stage4103.db.expire_all()
    persisted = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"])
    item = stage4103.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan["plan_id"]).one()
    operation = stage4103.db.get(StorageOperation, persisted.apply_operation_id)
    assert applied["state"] == persisted.state == item.state == operation.status == "partial"
    assert persisted.result_summary["failed_count"] == operation.result["failed_count"] == 1
    assert stage4103.db.get(ArchiveIntegrityFinding, finding.id).is_active is True
    assert stage4103.db.get(RecordingSegment, segment.id).deleted_at is None and path.exists()


def test_pre_mutation_blocked_failed_and_cancelled_truth_is_preserved(stage4103):
    camera = add_camera(stage4103)
    segments = [add_segment(stage4103, camera, name=f"terminal-{index}.mkv", create_file=False)[0] for index in range(3)]
    scan = run_scan(stage4103, key="pre-mutation-terminal-scan")
    findings = {row.segment_id: row for row in active_findings(stage4103, scan["scan_id"]) if row.segment_id}
    original_apply = remediation._apply_missing
    scenarios = (
        ("blocked", IntegrityRemediationBlocked("forced_blocked", retry_mode="new_scan")),
        ("failed", RuntimeError("forced_failed")),
        ("cancelled", None),
    )
    for index, (expected, failure) in enumerate(scenarios):
        finding = findings[segments[index].id]
        plan = create_remediation_plan(
            stage4103.db,
            finding_id=finding.id,
            action_key="retire_missing_recording",
            actor=owner(),
            idempotency_key=f"pre-mutation-{expected}-plan",
        )
        if expected == "cancelled":
            stage4103.monkeypatch.setattr(remediation, "operation_cancel_requested", lambda *_args, **_kwargs: True)
        else:
            stage4103.monkeypatch.setattr(
                remediation,
                "_apply_missing",
                lambda *_args, _failure=failure, **_kwargs: (_ for _ in ()).throw(_failure),
            )
        applied = apply_remediation_plan(
            stage4103.db,
            plan_id=plan["plan_id"],
            actor=owner(),
            confirm=True,
            operation_id=f"integrity-pre-mutation-{expected}-4103",
        )
        stage4103.db.expire_all()
        persisted = stage4103.db.get(ArchiveIntegrityRemediationPlan, plan["plan_id"])
        operation = stage4103.db.get(StorageOperation, persisted.apply_operation_id)
        assert applied["state"] == persisted.state == operation.status == expected
        assert operation.result["mutated_count"] == 0
        assert stage4103.db.get(RecordingSegment, segments[index].id).deleted_at is None
        assert stage4103.db.get(ArchiveIntegrityFinding, finding.id).is_active is True
        stage4103.monkeypatch.setattr(remediation, "_apply_missing", original_apply)
        stage4103.monkeypatch.setattr(remediation, "operation_cancel_requested", lambda db, handle: False)


def test_changed_root_identity_blocks_plan_and_preserves_catalog(stage4103):
    camera = add_camera(stage4103)
    segment, _ = add_segment(stage4103, camera, create_file=False)
    result = run_scan(stage4103)
    finding = next(row for row in active_findings(stage4103, result["scan_id"]) if row.segment_id == segment.id)
    stage4103.root.physical_identity = "changed-volume"
    stage4103.db.add(stage4103.root)
    stage4103.db.commit()
    with pytest.raises(IntegrityRemediationBlocked):
        create_remediation_plan(
            stage4103.db,
            finding_id=finding.id,
            action_key="retire_missing_recording",
            actor=owner(),
            idempotency_key="changed-root",
        )
    assert stage4103.db.get(RecordingSegment, segment.id).deleted_at is None


def test_interrupted_directory_is_rescanned_from_start_without_offset(stage4103):
    started = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key="directory-restart")
    work = stage4103.db.query(ArchiveIntegrityDirectoryWork).filter_by(scan_id=started["scan_id"]).one()
    work.status = "claimed"
    work.owner_instance_id = "dead-worker"
    work.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    work.attempt_count = 1
    stage4103.db.add(work)
    stage4103.db.commit()
    _reset_interrupted_directory_work(stage4103.db, stage4103.db.get(ArchiveIntegrityScan, started["scan_id"]))
    stage4103.db.refresh(work)
    assert work.status == "interrupted" and work.owner_instance_id is None
    assert not hasattr(work, "entry_offset")
    assert "offset" not in inspect.getsource(integrity._process_directory_unit).lower()


@pytest.mark.parametrize("terminal", ("completed", "partial", "failed", "cancelled"))
def test_terminal_scan_recovery_preserves_exact_status_releases_slot_and_deduplicates_audit(stage4103, terminal):
    started = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key=f"terminal-scan-{terminal}")
    scan = stage4103.db.get(ArchiveIntegrityScan, started["scan_id"])
    scan.status = terminal
    scan.phase = terminal
    scan.reason_code = None if terminal == "completed" else f"archive_integrity_scan_{terminal}"
    scan.retry_mode = "new_scan" if terminal in {"partial", "failed"} else None
    scan.next_action = "retry_integrity_scan" if scan.retry_mode else None
    scan.finished_at = datetime.utcnow()
    stage4103.db.add(scan)
    stage4103.db.commit()
    operation = stage4103.db.get(StorageOperation, scan.operation_id)
    assert integrity._recover_terminal_scan(stage4103.db, scan, operation) is True
    stage4103.db.expire_all()
    scan = stage4103.db.get(ArchiveIntegrityScan, started["scan_id"])
    operation = stage4103.db.get(StorageOperation, scan.operation_id)
    audit_count = stage4103.db.query(AuditEvent).filter(
        AuditEvent.event_type == f"archive_integrity.scan_{terminal}",
        AuditEvent.target_id == scan.id,
    ).count()
    assert scan.status == operation.status == terminal
    assert scan.active_slot is None
    assert integrity._scan_outer_terminal_matches(scan, operation)
    assert latest_integrity_scan(stage4103.db)["status"] == terminal
    assert integrity._recover_terminal_scan(stage4103.db, scan, operation) is False
    integrity._ensure_scan_terminal_audit(stage4103.db, scan)
    assert stage4103.db.query(AuditEvent).filter(
        AuditEvent.event_type == f"archive_integrity.scan_{terminal}",
        AuditEvent.target_id == scan.id,
    ).count() == audit_count == 1


def test_scan_terminal_lease_loss_restarts_only_terminalization_not_classification(stage4103):
    camera = add_camera(stage4103)
    add_segment(stage4103, camera, name="terminal-recovery.mkv")
    classified = []
    original_classify = integrity._classify_filesystem_entry
    original_terminal = integrity.stage_operation_terminal

    def tracked_classify(*args, **kwargs):
        classified.append(str(kwargs["relative_ref"]))
        return original_classify(*args, **kwargs)

    failures = {"remaining": 1}

    def fail_terminal_once(*args, **kwargs):
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise StorageOperationLeaseLost("forced_scan_terminal_loss")
        return original_terminal(*args, **kwargs)

    stage4103.monkeypatch.setattr(integrity, "_classify_filesystem_entry", tracked_classify)
    stage4103.monkeypatch.setattr(integrity, "stage_operation_terminal", fail_terminal_once)
    started = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key="scan-terminal-loss")
    assert run_integrity_worker_once() is True
    stage4103.db.expire_all()
    interrupted = stage4103.db.get(ArchiveIntegrityScan, started["scan_id"])
    first_classification_count = len(classified)
    assert interrupted.status == "interrupted" and interrupted.active_slot == integrity.SCAN_ACTIVE_SLOT
    expire_operation(stage4103, interrupted.operation_id)
    stage4103.monkeypatch.setattr(integrity, "stage_operation_terminal", original_terminal)
    assert run_integrity_worker_once() is True
    stage4103.db.expire_all()
    recovered = stage4103.db.get(ArchiveIntegrityScan, started["scan_id"])
    operation = stage4103.db.get(StorageOperation, recovered.operation_id)
    assert recovered.status == operation.status == "completed"
    assert recovered.active_slot is None
    assert len(classified) == first_classification_count
    assert stage4103.db.query(AuditEvent).filter(
        AuditEvent.event_type == "archive_integrity.scan_completed",
        AuditEvent.target_id == recovered.id,
    ).count() == 1


def test_directory_above_previous_4096_ceiling_is_fully_streamed_without_partial(stage4103):
    total = 4105
    for index in range(total):
        (stage4103.namespace / f"large-{index:05d}.bin").write_bytes(b"x")
    visited = []
    stage4103.monkeypatch.setattr(
        integrity,
        "_classify_filesystem_entry",
        lambda *_args, **kwargs: visited.append(str(kwargs["relative_ref"])),
    )
    result = run_scan(stage4103, key="large-directory-over-4096")
    work = stage4103.db.query(ArchiveIntegrityDirectoryWork).filter_by(scan_id=result["scan_id"]).one()
    assert result["status"] == "completed"
    assert result["progress"]["checked_count"] == total
    assert result["progress"]["failed_count"] == 0
    assert len(visited) == total
    assert work.status == "completed" and work.discovered_file_count == total
    assert "limit" not in str(work.reason_code or "")


def test_directory_slice_interruption_rescans_to_completion_without_counter_inflation(stage4103):
    total = 96
    for index in range(total):
        (stage4103.namespace / f"restart-{index:03d}.bin").write_bytes(b"x")
    stage4103.monkeypatch.setattr(integrity, "DIRECTORY_COMMIT_SLICE", 8)
    visits = []
    interruption = {"pending": True}

    def interrupt_once(*_args, **kwargs):
        visits.append(str(kwargs["relative_ref"]))
        if interruption["pending"] and len(visits) == 20:
            interruption["pending"] = False
            raise StorageOperationLeaseLost("forced_directory_interruption")

    stage4103.monkeypatch.setattr(integrity, "_classify_filesystem_entry", interrupt_once)
    started = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key="directory-slice-restart")
    assert run_integrity_worker_once() is True
    stage4103.db.expire_all()
    scan = stage4103.db.get(ArchiveIntegrityScan, started["scan_id"])
    work = stage4103.db.query(ArchiveIntegrityDirectoryWork).filter_by(scan_id=scan.id).one()
    assert scan.status == "interrupted" and scan.checked_count == 0
    operation = stage4103.db.get(StorageOperation, scan.operation_id)
    operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    work.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    stage4103.db.add_all((operation, work))
    stage4103.db.commit()
    assert run_integrity_worker_once() is True
    stage4103.db.expire_all()
    scan = stage4103.db.get(ArchiveIntegrityScan, started["scan_id"])
    work = stage4103.db.query(ArchiveIntegrityDirectoryWork).filter_by(scan_id=scan.id).one()
    assert scan.status == "completed"
    assert scan.checked_count == total and scan.checked_bytes == total
    assert work.status == "completed" and work.attempt_count == 2
    assert len(visits) > total


def test_child_directories_beyond_slice_are_not_lost(stage4103):
    total = 70
    for index in range(total):
        child = stage4103.namespace / f"child-{index:03d}"
        child.mkdir()
        (child / "one.bin").write_bytes(b"x")
    stage4103.monkeypatch.setattr(integrity, "DIRECTORY_COMMIT_SLICE", 8)
    visited = []
    stage4103.monkeypatch.setattr(
        integrity,
        "_classify_filesystem_entry",
        lambda *_args, **kwargs: visited.append(str(kwargs["relative_ref"])),
    )
    result = run_scan(stage4103, key="child-directories-over-slice")
    works = stage4103.db.query(ArchiveIntegrityDirectoryWork).filter_by(scan_id=result["scan_id"]).all()
    assert result["status"] == "completed"
    assert result["progress"]["checked_count"] == total
    assert len(visited) == total
    assert len(works) == total + 1 and all(row.status == "completed" for row in works)


def test_post_cutoff_file_is_checked_but_never_promoted_to_deletable_orphan(stage4103):
    started = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key="post-cutoff-object")
    late = stage4103.namespace / "post-cutoff.mkv"
    late.write_bytes(b"recent")
    assert run_integrity_worker_once() is True
    stage4103.db.expire_all()
    scan = stage4103.db.get(ArchiveIntegrityScan, started["scan_id"])
    assert scan.status == "completed"
    assert scan.checked_count == 1
    assert active_findings(stage4103, scan.id) == []


def test_cancel_persists_truth_without_scan_mutation(stage4103):
    started = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key="cancel-me")
    cancelled = cancel_integrity_scan(stage4103.db, started["scan_id"], actor=owner())
    assert cancelled["status"] in {"cancel_requested", "cancelled"}
    assert stage4103.db.query(RecordingSegment).count() == 0


def test_cleanup_retains_active_and_plan_bound_generations(stage4103):
    started = start_integrity_scan(stage4103.db, actor=owner(), idempotency_key="protected-generation")
    scan = stage4103.db.get(ArchiveIntegrityScan, started["scan_id"])
    scan.finished_at = datetime.utcnow() - timedelta(days=90)
    scan.expires_at = datetime.utcnow() - timedelta(days=60)
    stage4103.db.add(scan)
    stage4103.db.commit()
    assert cleanup_old_integrity_generations(stage4103.db, now=datetime.utcnow()) == 0
    assert stage4103.db.get(ArchiveIntegrityScan, scan.id) is not None


def test_scan_progress_and_operation_payloads_are_bounded(stage4103):
    camera = add_camera(stage4103)
    for index in range(80):
        add_segment(stage4103, camera, name=f"missing-bounded-{index}.mkv", create_file=False)
    result = run_scan(stage4103)
    assert len(result["category_counts"]) <= 64
    assert "items" not in result and "findings" not in result
    assert len(str(result["operation"])) < 8192


def test_recorder_receipt_and_remediation_plan_are_db_durable(stage4103):
    table_names = set(Base.metadata.tables)
    assert {
        "archive_integrity_scans",
        "archive_integrity_findings",
        "archive_integrity_directory_work",
        "recorder_file_receipts",
        "archive_integrity_remediation_plans",
        "archive_integrity_remediation_items",
    }.issubset(table_names)
    assert ArchiveIntegrityRemediationPlan.__table__.c.canonical_hash.nullable is False
    assert RecorderFileReceipt.__table__.c.content_fingerprint.nullable is False


def test_stage4103_additive_schema_migration_preflight_apply_verify(stage4103):
    for table in reversed(STAGE4103_TABLES):
        table.drop(bind=stage4103.engine, checkfirst=True)
    with stage4103.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX IF EXISTS ix_recording_segments_root_relative_id")
    preflight = STAGE4103_ARCHIVE_INTEGRITY_MIGRATION.preflight(stage4103.db)
    applied = STAGE4103_ARCHIVE_INTEGRITY_MIGRATION.apply(stage4103.db)
    verified = STAGE4103_ARCHIVE_INTEGRITY_MIGRATION.verify(stage4103.db)
    stage4103.db.commit()
    assert CURRENT_SCHEMA_VERSION >= 5
    assert preflight["status"] == "ready"
    assert applied["created_or_verified_table_count"] == len(STAGE4103_TABLES)
    assert verified == {"status": "verified", "table_drift": False, "index_drift": False}


def test_source_contract_has_keyset_bounds_no_corpus_materialization_and_identity_bound_delete():
    metadata_source = inspect.getsource(integrity._process_metadata_unit)
    traversal_source = inspect.getsource(integrity._process_directory_unit)
    orphan_source = inspect.getsource(remediation._apply_orphan)
    assert "RecordingSegment.id >" in metadata_source
    assert "RecordingSegment.id <=" in metadata_source
    assert ".limit(METADATA_PAGE_SIZE)" in metadata_source
    assert "os.scandir" in traversal_source and "follow_symlinks=False" in traversal_source
    assert "DIRECTORY_ENTRY_LIMIT" not in traversal_source
    assert "DIRECTORY_COMMIT_SLICE" in traversal_source
    assert "list(os.scandir" not in traversal_source and "sorted(" not in traversal_source
    assert "dir_fd=" in orphan_source and "O_NOFOLLOW" in orphan_source
    assert "os.rename" in orphan_source and "os.unlink" in orphan_source
    assert "unsupported" in orphan_source
