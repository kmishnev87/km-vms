import os
import inspect
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import Base
import app.models  # noqa: F401,E402 - register the complete production FK graph
from app.models.archive_integrity import (
    ArchiveIntegrityDirectoryWork,
    ArchiveIntegrityFinding,
    ArchiveIntegrityRemediationItem,
    ArchiveIntegrityRemediationPlan,
    ArchiveIntegrityScan,
    RecorderFileReceipt,
)
from app.models.archive_migration import ArchiveMigrationPlan
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.storage_operation import StorageOperation, StorageWorkerLease
from app.services import archive_integrity as integrity
from app.services import archive_integrity_remediation as remediation
from app.services import automatic_retention, storage_monitoring
from app.routers import storage as storage_router
from app.services.archive_integrity import cleanup_old_integrity_generations
from app.services.storage_operations_foundation import cleanup_terminal_operations


POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL") or os.getenv("KMVMS_STAGE3_POSTGRES_URL")


@pytest.fixture
def sqlite_history(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stage41052.sqlite'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield SimpleNamespace(db=db, engine=engine, Session=Session)
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def postgres_history():
    if not POSTGRES_URL:
        pytest.fail("A disposable PostgreSQL URL is required for Stage 4.10.5.2")
    schema = f"stage41052_{uuid.uuid4().hex}"
    admin_engine = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    base_engine = create_engine(
        POSTGRES_URL,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    engine = base_engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(bind=engine, checkfirst=False)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield SimpleNamespace(db=db, engine=engine, Session=Session, schema=schema)
    finally:
        db.close()
        engine.dispose()
        base_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _operation(operation_id: str, *, when: datetime, status: str = "completed") -> StorageOperation:
    terminal = status in {"completed", "partial", "blocked", "failed", "cancelled"}
    return StorageOperation(
        id=operation_id,
        operation_type="integrity_scan",
        actor_kind="system",
        actor_key="system:stage41052",
        actor_user_id=None,
        system_owner="stage41052",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint=uuid.uuid4().hex * 2,
        status=status,
        scope={},
        progress={},
        result={"status": status},
        cancel_allowed=False,
        retry_allowed=False,
        fencing_token=1,
        revision=2,
        queued_at=when,
        started_at=when,
        heartbeat_at=when,
        finished_at=when if terminal else None,
        created_at=when,
        updated_at=when,
    )


def _audit(
    *,
    event_type: str,
    target_type: str,
    target_id: str,
    when: datetime,
) -> AuditEvent:
    return AuditEvent(
        id=str(uuid.uuid4()),
        created_at=when,
        actor_user_id=None,
        actor_username=None,
        actor_role=None,
        category="storage",
        event_type=event_type,
        severity="info",
        message_ru="Stage 4.10.5.2 lifecycle evidence",
        message_en="Stage 4.10.5.2 lifecycle evidence",
        target_type=target_type,
        target_id=target_id,
        event_metadata={},
    )


def _add_terminal_scan(
    ctx,
    *,
    label: str,
    when: datetime,
    status: str = "completed",
    active_slot: str | None = None,
    complete_audit: bool = True,
):
    operation_id = f"integrity-scan-{label}-{uuid.uuid4().hex}"
    operation = _operation(operation_id, when=when, status=status)
    ctx.db.add(operation)
    ctx.db.flush()
    scan = ArchiveIntegrityScan(
        id=str(uuid.uuid4()),
        operation_id=operation.id,
        actor_user_id=None,
        actor_key="system:stage41052",
        active_slot=active_slot,
        status=status,
        phase=status,
        root_snapshot=[],
        root_snapshot_hash=uuid.uuid4().hex * 2,
        segment_high_watermark=0,
        scan_cutoff_at=when,
        metadata_cursor=0,
        planned_count=0,
        checked_count=0,
        found_count=0,
        failed_count=0,
        skipped_count=0,
        checked_bytes=0,
        category_summary={},
        root_summary={},
        impact_summary={},
        cancel_requested=False,
        is_stale=False,
        started_at=when,
        heartbeat_at=when,
        finished_at=when if status in integrity.TERMINAL_SCAN_STATUSES else None,
        created_at=when,
        updated_at=when,
    )
    ctx.db.add(scan)
    ctx.db.flush()
    if complete_audit:
        ctx.db.add_all(
            [
                _audit(
                    event_type="storage_operation.finished",
                    target_type="storage_operation",
                    target_id=operation.id,
                    when=when,
                ),
                _audit(
                    event_type=f"archive_integrity.scan_{status}",
                    target_type="archive_integrity_scan",
                    target_id=scan.id,
                    when=when,
                ),
            ]
        )
    ctx.db.commit()
    return scan, operation


def _add_plan_chain(
    ctx,
    *,
    scan: ArchiveIntegrityScan,
    when: datetime,
    state: str = "completed",
    expires_at: datetime | None = None,
    retry_mode: str | None = None,
    complete_evidence: bool = True,
):
    prepare = _operation(f"integrity-plan-{uuid.uuid4().hex}", when=when)
    apply = _operation(f"integrity-apply-{uuid.uuid4().hex}", when=when, status=state)
    ctx.db.add_all((prepare, apply))
    ctx.db.flush()
    finding = ArchiveIntegrityFinding(
        id=str(uuid.uuid4()),
        scan_id=scan.id,
        finding_scope="metadata",
        category="missing_file",
        severity="error",
        impact_key="recording_unavailable",
        observed_facts={},
        action_key="retire_missing_recording",
        required_permission="delete_recordings",
        confirmation_level="destructive_catalog",
        is_active=False,
        state="resolved",
        observation_count=1,
        first_observed_at=when,
        last_observed_at=when,
        resolved_at=when,
        created_at=when,
        updated_at=when,
    )
    ctx.db.add(finding)
    ctx.db.flush()
    plan = ArchiveIntegrityRemediationPlan(
        id=str(uuid.uuid4()),
        scan_id=scan.id,
        finding_id=finding.id,
        operation_id=prepare.id,
        apply_operation_id=apply.id,
        actor_user_id=None,
        actor_key="system:stage41052",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint=uuid.uuid4().hex * 2,
        action_kind="retire_missing_recording",
        required_permission="delete_recordings",
        confirmation_level="destructive_catalog",
        schema_version=1,
        item_count=1,
        total_bytes=0,
        canonical_hash=uuid.uuid4().hex * 2,
        state=state,
        result_summary={"status": state} if complete_evidence else None,
        retry_mode=retry_mode,
        next_action="retry_remediation" if retry_mode else None,
        created_at=when,
        expires_at=expires_at or when + timedelta(minutes=30),
        started_at=when,
        finished_at=when if complete_evidence else None,
        updated_at=when,
    )
    ctx.db.add(plan)
    ctx.db.flush()
    item = ArchiveIntegrityRemediationItem(
        id=str(uuid.uuid4()),
        plan_id=plan.id,
        finding_id=finding.id,
        item_index=0,
        intended_mutation="retire_missing_metadata",
        evidence={},
        state=state if complete_evidence else "running",
        result_code=state if complete_evidence else None,
        created_at=when,
        updated_at=when,
    )
    plan.canonical_hash = remediation._canonical_hash(
        {
            "schema_version": int(plan.schema_version),
            "plan_id": plan.id,
            "scan_id": plan.scan_id,
            "finding_id": plan.finding_id,
            "action_kind": plan.action_kind,
            "required_permission": plan.required_permission,
            "item_id": item.id,
            "item_index": int(item.item_index),
            "intended_mutation": item.intended_mutation,
            "evidence": dict(item.evidence or {}),
        }
    )
    ctx.db.add(item)
    if complete_evidence:
        ctx.db.add_all(
            [
                _audit(
                    event_type="storage_operation.finished",
                    target_type="storage_operation",
                    target_id=prepare.id,
                    when=when,
                ),
                _audit(
                    event_type="storage_operation.finished",
                    target_type="storage_operation",
                    target_id=apply.id,
                    when=when,
                ),
                _audit(
                    event_type="archive_integrity.remediation_plan_created",
                    target_type="archive_integrity_plan",
                    target_id=plan.id,
                    when=when,
                ),
                _audit(
                    event_type=f"archive_integrity.remediation_{state}",
                    target_type="archive_integrity_plan",
                    target_id=plan.id,
                    when=when,
                ),
            ]
        )
    ctx.db.commit()
    return plan, item, prepare, apply


def _add_never_applied_plan(
    ctx,
    *,
    scan: ArchiveIntegrityScan,
    when: datetime,
    state: str = "prepared",
):
    plan_id = str(uuid.uuid4())
    finding = ArchiveIntegrityFinding(
        id=str(uuid.uuid4()),
        scan_id=scan.id,
        finding_scope="metadata",
        category="missing_file",
        severity="error",
        impact_key="recording_unavailable",
        observed_facts={},
        action_key="retire_missing_recording",
        required_permission="delete_recordings",
        confirmation_level="destructive_catalog",
        is_active=True,
        state="open",
        observation_count=1,
        first_observed_at=when,
        last_observed_at=when,
        created_at=when,
        updated_at=when,
    )
    prepare = _operation(f"integrity-plan-{uuid.uuid4().hex}", when=when)
    prepare.operation_type = "integrity_plan_prepare"
    prepare.result = {"status": "completed", "plan_id": plan_id, "item_count": 1}
    ctx.db.add_all((finding, prepare))
    ctx.db.flush()
    plan = ArchiveIntegrityRemediationPlan(
        id=plan_id,
        scan_id=scan.id,
        finding_id=finding.id,
        operation_id=prepare.id,
        apply_operation_id=None,
        actor_user_id=None,
        actor_key="system:stage41052",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint=uuid.uuid4().hex * 2,
        action_kind="retire_missing_recording",
        required_permission="delete_recordings",
        confirmation_level="destructive_catalog",
        schema_version=1,
        item_count=1,
        total_bytes=0,
        canonical_hash=uuid.uuid4().hex * 2,
        state=state,
        result_summary=None,
        reason_code="archive_integrity_plan_expired" if state == "blocked" else None,
        retry_mode="new_scan" if state == "blocked" else None,
        next_action=None,
        created_at=when,
        expires_at=when + timedelta(minutes=30),
        started_at=None,
        finished_at=None,
        updated_at=when,
    )
    ctx.db.add(plan)
    ctx.db.flush()
    item = ArchiveIntegrityRemediationItem(
        id=str(uuid.uuid4()),
        plan_id=plan.id,
        finding_id=finding.id,
        item_index=0,
        intended_mutation="retire_missing_metadata",
        evidence={},
        state="prepared",
        result_code=None,
        quarantine_ref=None,
        created_at=when,
        updated_at=when,
    )
    plan.canonical_hash = remediation._canonical_hash(
        {
            "schema_version": int(plan.schema_version),
            "plan_id": plan.id,
            "scan_id": plan.scan_id,
            "finding_id": plan.finding_id,
            "action_kind": plan.action_kind,
            "required_permission": plan.required_permission,
            "item_id": item.id,
            "item_index": int(item.item_index),
            "intended_mutation": item.intended_mutation,
            "evidence": dict(item.evidence or {}),
        }
    )
    ctx.db.add(item)
    ctx.db.add_all(
        [
            _audit(
                event_type="storage_operation.finished",
                target_type="storage_operation",
                target_id=prepare.id,
                when=when,
            ),
            _audit(
                event_type="archive_integrity.remediation_plan_created",
                target_type="archive_integrity_plan",
                target_id=plan.id,
                when=when,
            ),
        ]
    )
    ctx.db.commit()
    return plan, item, finding, prepare


def _migration_plan(operation_id: str, *, when: datetime) -> ArchiveMigrationPlan:
    return ArchiveMigrationPlan(
        id=str(uuid.uuid4()),
        actor_user_id=None,
        actor_key="system:stage41052",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint=uuid.uuid4().hex * 2,
        source_root_id="source",
        target_root_id="target",
        source_label_snapshot="Source",
        target_label_snapshot="Target",
        source_physical_identity="source-volume",
        target_physical_identity="target-volume",
        source_snapshot_key="a" * 64,
        target_snapshot_key="b" * 64,
        source_access_identity="c" * 64,
        target_access_identity="d" * 64,
        excluded_summary={},
        blocker_summary={},
        status="completed",
        phase="completed",
        current_operation_id=operation_id,
        finished_at=when,
        created_at=when,
        updated_at=when,
    )


def test_postgresql_generic_cleanup_excludes_restrict_refs_and_preserves_set_null(postgres_history):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    scan, scan_operation = _add_terminal_scan(ctx, label="protected", when=old)
    plan, _item, prepare, apply = _add_plan_chain(
        ctx,
        scan=scan,
        when=old,
        state="partial",
        expires_at=now + timedelta(minutes=10),
        retry_mode="immediate",
    )
    independent = _operation(f"independent-{uuid.uuid4().hex}", when=old)
    migration_operation = _operation(f"migration-{uuid.uuid4().hex}", when=old)
    ctx.db.add_all((independent, migration_operation))
    ctx.db.flush()
    migration_plan = _migration_plan(migration_operation.id, when=old)
    ctx.db.add(migration_plan)
    ctx.db.commit()
    independent_id = independent.id
    migration_operation_id = migration_operation.id
    migration_plan_id = migration_plan.id
    scan_operation_id = scan_operation.id
    prepare_id = prepare.id
    apply_id = apply.id
    plan_id = plan.id

    deleted = cleanup_terminal_operations(ctx.db, now=now)
    ctx.db.expire_all()

    assert deleted == 2
    assert ctx.db.get(StorageOperation, independent_id) is None
    assert ctx.db.get(StorageOperation, migration_operation_id) is None
    assert ctx.db.get(ArchiveMigrationPlan, migration_plan_id).current_operation_id is None
    assert ctx.db.get(StorageOperation, scan_operation_id) is not None
    assert ctx.db.get(StorageOperation, prepare_id) is not None
    assert ctx.db.get(StorageOperation, apply_id) is not None
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, plan_id) is not None
    assert cleanup_terminal_operations(ctx.db, now=now) == 0
    assert ctx.db.query(StorageOperation).count() == 3


def test_postgresql_safe_domain_retirement_releases_operations_and_keeps_audits(postgres_history):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    old_scan, scan_operation = _add_terminal_scan(ctx, label="retire", when=old)
    plan, item, prepare, apply = _add_plan_chain(
        ctx,
        scan=old_scan,
        when=old,
        expires_at=now - timedelta(days=1),
    )
    latest_scan, _latest_operation = _add_terminal_scan(ctx, label="latest", when=now - timedelta(minutes=1))
    directory = ArchiveIntegrityDirectoryWork(
        id=str(uuid.uuid4()),
        scan_id=old_scan.id,
        root_id="stage41052-root",
        root_snapshot_key="a" * 64,
        relative_directory=".",
        status="completed",
        completed_at=old,
        created_at=old,
        updated_at=old,
    )
    root = ArchiveRoot(
        id="stage41052-root",
        label="Stage 4.10.5.2",
        root_path="/stage41052/archive",
        storage_namespace="kmvms/recordings",
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage41052-volume",
    )
    camera = Camera(
        name="Stage 4.10.5.2 Camera",
        storage_folder_name="stage41052-camera",
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        default_live_stream="sub",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=30,
        storage_quota_gb=10,
        status="disabled",
    )
    ctx.db.add_all((directory, root, camera))
    ctx.db.flush()
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path="/stage41052/archive/kmvms/recordings/one.mkv",
        relative_path="kmvms/recordings/one.mkv",
        started_at=old,
        ended_at=old + timedelta(minutes=5),
        duration_sec=300,
        size_bytes=128,
        status="ready",
        archive_root_id=root.id,
        ownership="KM VMS",
        source="recorder",
    )
    ctx.db.add(segment)
    ctx.db.flush()
    receipt = RecorderFileReceipt(
        id=str(uuid.uuid4()),
        segment_id=segment.id,
        camera_id=camera.id,
        root_id=root.id,
        relative_path=segment.relative_path,
        state="finalized",
        object_identity="stage41052-object",
        size_bytes=segment.size_bytes,
        mtime_ns=1,
        content_fingerprint="f" * 64,
        finalized_at=old,
    )
    ctx.db.add(receipt)
    ctx.db.commit()
    audit_ids = {
        row.id
        for row in ctx.db.query(AuditEvent).filter(
            AuditEvent.target_id.in_([old_scan.id, plan.id, scan_operation.id, prepare.id, apply.id])
        )
    }
    old_scan_id = old_scan.id
    latest_scan_id = latest_scan.id
    item_id = item.id
    plan_id = plan.id
    finding_id = plan.finding_id
    directory_id = directory.id
    scan_operation_id = scan_operation.id
    prepare_id = prepare.id
    apply_id = apply.id
    segment_id = segment.id
    receipt_id = receipt.id
    audit_count = ctx.db.query(AuditEvent).count()
    assert cleanup_old_integrity_generations(ctx.db, now=now) == 1
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityRemediationItem, item_id) is None
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, plan_id) is None
    assert ctx.db.get(ArchiveIntegrityScan, old_scan_id) is None
    assert ctx.db.get(ArchiveIntegrityFinding, finding_id) is None
    assert ctx.db.get(ArchiveIntegrityDirectoryWork, directory_id) is None
    assert ctx.db.get(ArchiveIntegrityScan, latest_scan_id) is not None
    assert audit_ids.issubset({row.id for row in ctx.db.query(AuditEvent).all()})
    assert ctx.db.query(AuditEvent).count() == audit_count
    assert ctx.db.get(RecordingSegment, segment_id) is not None
    assert ctx.db.get(RecorderFileReceipt, receipt_id) is not None

    assert cleanup_terminal_operations(ctx.db, now=now) == 3
    assert ctx.db.get(StorageOperation, scan_operation_id) is None
    assert ctx.db.get(StorageOperation, prepare_id) is None
    assert ctx.db.get(StorageOperation, apply_id) is None
    assert cleanup_old_integrity_generations(ctx.db, now=now) == 0
    assert cleanup_terminal_operations(ctx.db, now=now) == 0


@pytest.mark.parametrize("plan_state", ["prepared", "blocked"])
def test_postgresql_expired_never_applied_plan_retires_without_synthetic_apply_truth(
    postgres_history,
    plan_state,
):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    scan, scan_operation = _add_terminal_scan(
        ctx,
        label=f"never-applied-{plan_state}",
        when=old,
    )
    plan, item, finding, prepare = _add_never_applied_plan(
        ctx,
        scan=scan,
        when=old,
        state=plan_state,
    )
    _add_terminal_scan(ctx, label=f"never-applied-latest-{plan_state}", when=now)
    ids = {
        "scan": scan.id,
        "scan_operation": scan_operation.id,
        "plan": plan.id,
        "item": item.id,
        "finding": finding.id,
        "prepare": prepare.id,
    }
    audit_ids = {
        row.id
        for row in ctx.db.query(AuditEvent)
        .filter(AuditEvent.target_id.in_((plan.id, prepare.id)))
        .all()
    }
    audit_count = ctx.db.query(AuditEvent).count()
    locked_items = (
        ctx.db.query(ArchiveIntegrityRemediationItem)
        .filter(ArchiveIntegrityRemediationItem.plan_id == plan.id)
        .all()
    )
    evidence = integrity._never_applied_plan_retirement_evidence(now, dialect_name="postgresql")
    assert (
        ctx.db.query(ArchiveIntegrityRemediationPlan.id)
        .filter(
            ArchiveIntegrityRemediationPlan.id == plan.id,
            evidence,
        )
        .first()
        is not None
    )
    assert integrity._never_applied_plan_revalidates(
        ctx.db,
        plan,
        locked_items,
        current=now,
        dialect_name="postgresql",
    )

    assert cleanup_old_integrity_generations(ctx.db, now=now) == 1
    assert cleanup_old_integrity_generations(ctx.db, now=now) == 0
    ctx.db.expire_all()

    assert ctx.db.get(ArchiveIntegrityRemediationItem, ids["item"]) is None
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, ids["plan"]) is None
    assert ctx.db.get(ArchiveIntegrityFinding, ids["finding"]) is None
    assert ctx.db.get(ArchiveIntegrityScan, ids["scan"]) is None
    assert ctx.db.get(StorageOperation, ids["prepare"]) is not None
    assert ctx.db.query(StorageOperation).filter(
        StorageOperation.operation_type.in_(
            (
                "integrity_catalog_retirement",
                "integrity_metadata_repair",
                "integrity_recording_delete",
                "orphan_file_cleanup",
            )
        ),
        StorageOperation.actor_key == "system:stage41052",
    ).count() == 0
    assert audit_ids.issubset({row.id for row in ctx.db.query(AuditEvent).all()})
    assert ctx.db.query(AuditEvent).count() == audit_count

    cleanup_terminal_operations(ctx.db, now=now)
    ctx.db.expire_all()
    assert ctx.db.get(StorageOperation, ids["prepare"]) is None
    assert ctx.db.get(StorageOperation, ids["scan_operation"]) is None
    assert audit_ids.issubset({row.id for row in ctx.db.query(AuditEvent).all()})


def test_postgresql_never_applied_retirement_fails_closed_for_incomplete_or_contradictory_truth(
    postgres_history,
):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    protected: dict[str, str] = {}
    cases = (
        "unexpired",
        "blocked_other_reason",
        "apply_operation_bound",
        "started",
        "result_summary",
        "item_mutation_state",
        "item_result_code",
        "item_quarantine_ref",
        "terminal_audit",
        "prepare_result_mismatch",
        "prepare_audit_missing",
        "plan_audit_missing",
        "canonical_hash_mismatch",
        "item_index_mismatch",
        "intended_mutation_mismatch",
        "unbound_apply_operation",
    )
    for offset, case in enumerate(cases):
        scan, _scan_operation = _add_terminal_scan(
            ctx,
            label=f"protected-{case}",
            when=old + timedelta(seconds=offset),
        )
        plan, item, _finding, prepare = _add_never_applied_plan(
            ctx,
            scan=scan,
            when=old,
        )
        if case == "unexpired":
            plan.expires_at = now + timedelta(days=1)
        elif case == "blocked_other_reason":
            plan.state = "blocked"
            plan.reason_code = "archive_integrity_support_required"
            plan.retry_mode = "support"
        elif case == "apply_operation_bound":
            plan.apply_operation_id = prepare.id
        elif case == "started":
            plan.started_at = old
        elif case == "result_summary":
            plan.result_summary = {"status": "unknown"}
        elif case == "item_mutation_state":
            item.state = "quarantine_prepared"
        elif case == "item_result_code":
            item.result_code = "archive_integrity_test_result"
        elif case == "item_quarantine_ref":
            item.quarantine_ref = "quarantine-test-ref"
        elif case == "terminal_audit":
            ctx.db.add(
                _audit(
                    event_type="archive_integrity.remediation_failed",
                    target_type="archive_integrity_plan",
                    target_id=plan.id,
                    when=old,
                )
            )
        elif case == "prepare_result_mismatch":
            prepare.result = {"status": "completed", "plan_id": str(uuid.uuid4()), "item_count": 1}
        elif case == "prepare_audit_missing":
            ctx.db.query(AuditEvent).filter(
                AuditEvent.event_type == "storage_operation.finished",
                AuditEvent.target_id == prepare.id,
            ).delete(synchronize_session=False)
        elif case == "plan_audit_missing":
            ctx.db.query(AuditEvent).filter(
                AuditEvent.event_type == "archive_integrity.remediation_plan_created",
                AuditEvent.target_id == plan.id,
            ).delete(synchronize_session=False)
        elif case == "canonical_hash_mismatch":
            plan.canonical_hash = "f" * 64
        elif case == "item_index_mismatch":
            item.item_index = 1
        elif case == "intended_mutation_mismatch":
            item.intended_mutation = "unexpected_mutation"
        elif case == "unbound_apply_operation":
            request_identity, idempotency_key = integrity.remediation_apply_operation_identity(plan)
            operation = _operation(f"integrity-apply-{uuid.uuid4().hex}", when=old, status="running")
            operation.operation_type = "integrity_catalog_retirement"
            operation.actor_key = plan.actor_key
            operation.idempotency_key = idempotency_key
            operation.request_fingerprint = remediation.request_fingerprint(request_identity)
            operation.domain_ref = plan.id
            operation.scope = {
                "global": True,
                "physical_volume_ids": [],
                "root_ids": [],
                "camera_ids": [],
                "segment_ids": [],
            }
            operation.result = None
            operation.finished_at = None
            ctx.db.add(operation)
        ctx.db.add_all((plan, item, prepare))
        ctx.db.commit()
        protected[case] = scan.id

    _add_terminal_scan(ctx, label="protected-latest", when=now)
    audit_count = ctx.db.query(AuditEvent).count()

    assert cleanup_old_integrity_generations(ctx.db, now=now) == 0
    ctx.db.expire_all()
    assert all(ctx.db.get(ArchiveIntegrityScan, scan_id) is not None for scan_id in protected.values())
    assert ctx.db.query(AuditEvent).count() == audit_count


def test_postgresql_ambiguous_plan_preserves_scan_and_protected_oldest_does_not_starve_fairness(
    postgres_history,
    monkeypatch,
):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    protected_scan, _ = _add_terminal_scan(ctx, label="ambiguous-oldest", when=old)
    _add_never_applied_plan(ctx, scan=protected_scan, when=old)
    blocked_plan, blocked_item, _finding, _prepare = _add_never_applied_plan(
        ctx,
        scan=protected_scan,
        when=old,
        state="blocked",
    )
    blocked_plan.reason_code = "archive_integrity_support_required"
    blocked_plan.retry_mode = "support"
    ctx.db.add_all((blocked_plan, blocked_item))
    ctx.db.commit()
    eligible_scan, _ = _add_terminal_scan(ctx, label="eligible-after-protected", when=old + timedelta(seconds=1))
    _add_terminal_scan(ctx, label="fairness-latest", when=now)
    protected_scan_id = str(protected_scan.id)
    eligible_scan_id = str(eligible_scan.id)
    monkeypatch.setattr(integrity, "SCAN_CLEANUP_BATCH", 1)

    assert cleanup_old_integrity_generations(ctx.db, now=now) == 1
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityScan, protected_scan_id) is not None
    assert ctx.db.get(ArchiveIntegrityScan, eligible_scan_id) is None


def _fake_global_apply_context(db, plan):
    item = remediation._plan_item(db, plan)
    finding = db.get(ArchiveIntegrityFinding, str(plan.finding_id))
    return (
        item,
        finding,
        remediation.MUTATING_ACTIONS[str(plan.action_kind)],
        {
            "global": True,
            "physical_volume_ids": [],
            "root_ids": [],
            "camera_ids": [],
            "segment_ids": [],
        },
    )


def test_postgresql_apply_claim_crosses_foundation_commit_and_cleanup_revalidates_after_apply_wins(
    postgres_history,
    monkeypatch,
):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    scan, _ = _add_terminal_scan(ctx, label="apply-wins", when=old)
    plan, item, _finding, _prepare = _add_never_applied_plan(ctx, scan=scan, when=old)
    plan.actor_key = "system:system"
    plan.expires_at = datetime.utcnow() + timedelta(milliseconds=600)
    ctx.db.add(plan)
    ctx.db.commit()
    _add_terminal_scan(ctx, label="apply-wins-latest", when=now)
    plan_id = str(plan.id)
    scan_id = str(scan.id)
    item_id = str(item.id)
    claimed_boundary = threading.Event()
    release_claim = threading.Event()
    real_claim = remediation.claim_operation_with_conflicts
    outcome: dict[str, object] = {}

    def claim_then_pause(*args, **kwargs):
        result = real_claim(*args, **kwargs)
        claimed_boundary.set()
        if not release_claim.wait(timeout=5):
            raise AssertionError("foundation boundary synchronization timed out")
        return result

    def run_apply():
        with ctx.Session() as session:
            try:
                outcome["apply"] = remediation._coordinated_apply_claim(
                    session,
                    plan_id=plan_id,
                    actor=None,
                    requested_operation_id=f"integrity-apply-{uuid.uuid4().hex}",
                    expected_actor_key="system:system",
                    allow_create=True,
                )
            except BaseException as exc:
                outcome["apply_error"] = exc

    def run_cleanup():
        with ctx.Session() as session:
            try:
                outcome["cleanup"] = cleanup_old_integrity_generations(session, now=datetime.utcnow())
            except BaseException as exc:
                outcome["cleanup_error"] = exc

    monkeypatch.setattr(remediation, "_apply_context", _fake_global_apply_context)
    monkeypatch.setattr(remediation, "claim_operation_with_conflicts", claim_then_pause)
    apply_thread = threading.Thread(target=run_apply)
    apply_thread.start()
    assert claimed_boundary.wait(timeout=5)
    time.sleep(0.7)
    cleanup_thread = threading.Thread(target=run_cleanup)
    cleanup_thread.start()
    time.sleep(0.1)
    release_claim.set()
    apply_thread.join(timeout=8)
    cleanup_thread.join(timeout=8)

    assert not apply_thread.is_alive() and not cleanup_thread.is_alive()
    assert "apply_error" not in outcome and "cleanup_error" not in outcome
    assert outcome["cleanup"] == 0
    ctx.db.expire_all()
    saved_plan = ctx.db.get(ArchiveIntegrityRemediationPlan, plan_id)
    assert saved_plan is not None and saved_plan.apply_operation_id is not None
    assert saved_plan.state == "running"
    assert ctx.db.get(ArchiveIntegrityRemediationItem, item_id).state == "running"
    assert ctx.db.get(ArchiveIntegrityScan, scan_id) is not None
    assert ctx.db.get(StorageOperation, str(saved_plan.apply_operation_id)) is not None


def test_postgresql_retirement_wins_before_apply_without_orphan_operation_or_mutation(
    postgres_history,
    monkeypatch,
):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    scan, _ = _add_terminal_scan(ctx, label="retirement-wins", when=old)
    plan, _item, _finding, _prepare = _add_never_applied_plan(ctx, scan=scan, when=old)
    plan.actor_key = "system:system"
    ctx.db.add(plan)
    ctx.db.commit()
    _add_terminal_scan(ctx, label="retirement-wins-latest", when=now)
    plan_id = str(plan.id)
    scan_id = str(scan.id)
    retirement_revalidating = threading.Event()
    release_retirement = threading.Event()
    real_revalidation = integrity._remediation_plan_revalidates_for_retirement
    outcome: dict[str, object] = {}

    def pause_revalidation(*args, **kwargs):
        result = real_revalidation(*args, **kwargs)
        retirement_revalidating.set()
        if not release_retirement.wait(timeout=5):
            raise AssertionError("retirement synchronization timed out")
        return result

    def run_cleanup():
        with ctx.Session() as session:
            try:
                outcome["cleanup"] = cleanup_old_integrity_generations(session, now=now)
            except BaseException as exc:
                outcome["cleanup_error"] = exc

    def run_apply():
        with ctx.Session() as session:
            try:
                outcome["apply"] = remediation._coordinated_apply_claim(
                    session,
                    plan_id=plan_id,
                    actor=None,
                    requested_operation_id=f"integrity-apply-{uuid.uuid4().hex}",
                    expected_actor_key="system:system",
                    allow_create=True,
                )
            except BaseException as exc:
                outcome["apply_error"] = exc

    monkeypatch.setattr(integrity, "_remediation_plan_revalidates_for_retirement", pause_revalidation)
    monkeypatch.setattr(remediation, "_apply_context", _fake_global_apply_context)
    cleanup_thread = threading.Thread(target=run_cleanup)
    cleanup_thread.start()
    assert retirement_revalidating.wait(timeout=5)
    apply_thread = threading.Thread(target=run_apply)
    apply_thread.start()
    time.sleep(0.1)
    release_retirement.set()
    cleanup_thread.join(timeout=8)
    apply_thread.join(timeout=8)

    assert not cleanup_thread.is_alive() and not apply_thread.is_alive()
    assert "cleanup_error" not in outcome
    assert outcome["cleanup"] == 1
    assert isinstance(outcome.get("apply_error"), remediation.StorageOperationContractError)
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityScan, scan_id) is None
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, plan_id) is None
    assert ctx.db.query(StorageOperation).filter(
        StorageOperation.operation_type.in_(tuple(integrity.REMEDIATION_ACTION_OPERATION_TYPES.values()))
    ).count() == 0


def test_postgresql_background_recovery_adopts_exact_domain_operation_under_coordinator(
    postgres_history,
    monkeypatch,
):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=1)
    scan, _ = _add_terminal_scan(ctx, label="background-adopt", when=old)
    plan, item, _finding, prepare = _add_never_applied_plan(ctx, scan=scan, when=old)
    plan.actor_key = "system:system"
    plan.idempotency_key = prepare.idempotency_key
    plan.request_fingerprint = prepare.request_fingerprint
    prepare.actor_key = plan.actor_key
    plan.expires_at = now + timedelta(days=1)
    request_identity, idempotency_key = integrity.remediation_apply_operation_identity(plan)
    apply_operation = _operation(f"integrity-apply-{uuid.uuid4().hex}", when=old, status="running")
    apply_operation.operation_type = "integrity_catalog_retirement"
    apply_operation.actor_kind = "system"
    apply_operation.actor_key = plan.actor_key
    apply_operation.system_owner = "system"
    apply_operation.idempotency_key = idempotency_key
    apply_operation.request_fingerprint = remediation.request_fingerprint(request_identity)
    apply_operation.domain_ref = plan.id
    apply_operation.scope = {
        "global": True,
        "physical_volume_ids": [],
        "root_ids": [],
        "camera_ids": [],
        "segment_ids": [],
    }
    apply_operation.result = None
    apply_operation.finished_at = None
    apply_operation.lease_expires_at = old
    ctx.db.add_all((plan, apply_operation))
    ctx.db.commit()
    mutation_calls = {"count": 0}

    def apply_missing(_db, _plan, _item, _finding, _actor):
        mutation_calls["count"] += 1
        return {"status": "completed", "mutated_count": 1}

    monkeypatch.setattr(remediation, "_apply_context", _fake_global_apply_context)
    monkeypatch.setattr(remediation, "_durable_apply_context", _fake_global_apply_context)
    monkeypatch.setattr(remediation, "_apply_missing", apply_missing)

    assert remediation.recover_pending_remediation_once(ctx.db) is True
    ctx.db.expire_all()
    saved_plan = ctx.db.get(ArchiveIntegrityRemediationPlan, str(plan.id))
    assert saved_plan.apply_operation_id == apply_operation.id
    assert saved_plan.state == "completed"
    assert ctx.db.get(ArchiveIntegrityRemediationItem, str(item.id)).state == "completed"
    assert mutation_calls["count"] == 1


def test_integrity_cleanup_preserves_latest_active_actionable_and_incomplete_truth(sqlite_history):
    ctx = sqlite_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    actionable_scan, _ = _add_terminal_scan(ctx, label="actionable", when=old)
    _add_plan_chain(
        ctx,
        scan=actionable_scan,
        when=old,
        state="partial",
        expires_at=now + timedelta(minutes=10),
        retry_mode="immediate",
    )
    incomplete_scan, _ = _add_terminal_scan(ctx, label="incomplete", when=old + timedelta(seconds=1))
    _add_plan_chain(
        ctx,
        scan=incomplete_scan,
        when=old,
        state="failed",
        expires_at=now - timedelta(days=1),
        complete_evidence=False,
    )
    active_scan, _ = _add_terminal_scan(
        ctx,
        label="active",
        when=old + timedelta(seconds=2),
        status="running",
        active_slot="global",
        complete_audit=False,
    )
    lifecycle_scans = []
    for offset, state in enumerate(("prepared", "running", "terminal_pending"), start=4):
        lifecycle_scan, _ = _add_terminal_scan(
            ctx,
            label=f"lifecycle-{state}",
            when=old + timedelta(seconds=offset),
        )
        lifecycle_plan, lifecycle_item, _prepare, _apply = _add_plan_chain(
            ctx,
            scan=lifecycle_scan,
            when=old,
            expires_at=now - timedelta(days=1),
        )
        lifecycle_plan.state = state
        lifecycle_plan.finished_at = None
        lifecycle_plan.result_summary = None
        lifecycle_item.state = state
        if state == "prepared":
            lifecycle_plan.apply_operation_id = None
        ctx.db.add_all((lifecycle_plan, lifecycle_item))
        ctx.db.commit()
        lifecycle_scans.append(lifecycle_scan)
    latest_scan, _ = _add_terminal_scan(ctx, label="latest", when=now - timedelta(minutes=1))

    assert cleanup_old_integrity_generations(ctx.db, now=now) == 0
    assert ctx.db.get(ArchiveIntegrityScan, actionable_scan.id) is not None
    assert ctx.db.get(ArchiveIntegrityScan, incomplete_scan.id) is not None
    assert ctx.db.get(ArchiveIntegrityScan, active_scan.id) is not None
    assert ctx.db.get(ArchiveIntegrityScan, latest_scan.id) is not None
    assert all(ctx.db.get(ArchiveIntegrityScan, row.id) is not None for row in lifecycle_scans)


def test_postgresql_concurrent_fk_race_rolls_back_recomputes_and_keeps_session_usable(postgres_history):
    ctx = postgres_history
    now = datetime.utcnow()
    old = now - timedelta(days=60)
    raced_operation = _operation(f"race-{uuid.uuid4().hex}", when=old - timedelta(minutes=1))
    independent = _operation(f"race-independent-{uuid.uuid4().hex}", when=old)
    ctx.db.add_all((raced_operation, independent))
    ctx.db.commit()
    raced_operation_id = raced_operation.id
    independent_id = independent.id
    scan_id = str(uuid.uuid4())
    values = {
        "id": scan_id,
        "operation_id": raced_operation_id,
        "actor_user_id": None,
        "actor_key": "system:stage41052-race",
        "active_slot": None,
        "status": "completed",
        "phase": "completed",
        "root_snapshot": [],
        "root_snapshot_hash": "e" * 64,
        "segment_high_watermark": 0,
        "scan_cutoff_at": old,
        "metadata_cursor": 0,
        "planned_count": 0,
        "checked_count": 0,
        "found_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "checked_bytes": 0,
        "category_summary": {},
        "root_summary": {},
        "impact_summary": {},
        "cancel_requested": False,
        "is_stale": False,
        "started_at": old,
        "heartbeat_at": old,
        "finished_at": old,
        "expires_at": None,
        "created_at": old,
        "updated_at": old,
    }
    state = {"injected": False, "persisted": False}

    def inject_reference(connection, _cursor, statement, _parameters, _context, _many):
        if state["injected"] or not statement.lstrip().upper().startswith("DELETE FROM"):
            return
        if "storage_operations" not in statement:
            return
        state["injected"] = True
        connection.execute(ArchiveIntegrityScan.__table__.insert().values(**values))

    def persist_reference_after_rollback(_session):
        if not state["injected"] or state["persisted"]:
            return
        state["persisted"] = True
        with ctx.engine.begin() as connection:
            connection.execute(ArchiveIntegrityScan.__table__.insert().values(**values))

    event.listen(ctx.engine, "before_cursor_execute", inject_reference)
    event.listen(ctx.db, "after_rollback", persist_reference_after_rollback)
    try:
        assert cleanup_terminal_operations(ctx.db, now=now) == 1
    finally:
        event.remove(ctx.engine, "before_cursor_execute", inject_reference)
        event.remove(ctx.db, "after_rollback", persist_reference_after_rollback)

    assert state == {"injected": True, "persisted": True}
    assert ctx.db.get(StorageOperation, raced_operation_id) is not None
    assert ctx.db.get(ArchiveIntegrityScan, scan_id) is not None
    assert ctx.db.get(StorageOperation, independent_id) is None
    assert ctx.db.query(StorageOperation).count() == 1


def test_integrity_age_and_count_bounds_are_independent_and_batches_converge(sqlite_history, monkeypatch):
    ctx = sqlite_history
    now = datetime.utcnow()
    monkeypatch.setattr(integrity, "SCAN_HISTORY_MAX_ROWS", 2)
    monkeypatch.setattr(integrity, "SCAN_CLEANUP_BATCH", 2)
    scans = []
    for index in range(6):
        scan, _operation_row = _add_terminal_scan(
            ctx,
            label=f"count-{index}",
            when=now - timedelta(minutes=10 - index),
        )
        scans.append(scan)

    assert cleanup_old_integrity_generations(ctx.db, now=now) == 2
    assert cleanup_old_integrity_generations(ctx.db, now=now) == 2
    assert cleanup_old_integrity_generations(ctx.db, now=now) == 0
    remaining = ctx.db.query(ArchiveIntegrityScan).order_by(ArchiveIntegrityScan.created_at.desc()).all()
    assert [row.id for row in remaining] == [scans[-1].id, scans[-2].id]

    monkeypatch.setattr(integrity, "SCAN_HISTORY_MAX_ROWS", 100)
    old_scan, _ = _add_terminal_scan(ctx, label="age-only", when=now - timedelta(days=60))
    old_scan_id = old_scan.id
    assert cleanup_old_integrity_generations(ctx.db, now=now) == 1
    assert ctx.db.get(ArchiveIntegrityScan, old_scan_id) is None


class _NoThreadHeartbeat:
    def __init__(self, _handle):
        pass

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return None

    def assert_owned(self):
        return None


def test_history_worker_failure_is_sanitized_releases_leader_and_next_cycle_runs(
    sqlite_history,
    monkeypatch,
    caplog,
):
    ctx = sqlite_history
    monkeypatch.setattr(automatic_retention, "SessionLocal", ctx.Session)
    monkeypatch.setattr(automatic_retention, "_LeaderHeartbeat", _NoThreadHeartbeat)
    monkeypatch.setattr(automatic_retention, "ensure_retention_signal", lambda _db: None)
    monkeypatch.setattr(automatic_retention, "run_auto_free_pressure_groups", lambda _db, **_kwargs: {})
    monkeypatch.setattr(automatic_retention, "publish_due_retention_signal", lambda _db: None)
    monkeypatch.setattr(automatic_retention, "claim_retention_signal", lambda _db, **_kwargs: None)
    monkeypatch.setattr(automatic_retention, "cleanup_terminal_operations", lambda _db: 2)

    def fail_integrity(_db):
        raise RuntimeError("sensitive SQL row must not reach logs")

    monkeypatch.setattr(automatic_retention, "cleanup_old_integrity_generations", fail_integrity)
    first = automatic_retention.run_automatic_retention_cycle()
    assert first["status"] == "completed"
    assert first["history_cleanup"] == {
        "status": "partial",
        "integrity_deleted_count": 0,
        "operation_deleted_count": 2,
        "failed_phases": ["integrity"],
    }
    assert "sensitive SQL row" not in caplog.text
    ctx.db.expire_all()
    lease = ctx.db.get(StorageWorkerLease, "automatic-retention")
    assert lease is not None and lease.lease_expires_at <= datetime.utcnow()

    monkeypatch.setattr(automatic_retention, "cleanup_old_integrity_generations", lambda _db: 1)
    second = automatic_retention.run_automatic_retention_cycle()
    assert second["history_cleanup"]["status"] == "completed"
    assert second["integrity_history_cleanup_count"] == 1
    assert second["history_cleanup_count"] == 2
    ctx.db.expire_all()
    lease = ctx.db.get(StorageWorkerLease, "automatic-retention")
    assert lease is not None and lease.lease_expires_at <= datetime.utcnow()


def test_history_cleanup_is_background_db_only_and_storage_status_remains_read_only():
    cleanup_source = inspect.getsource(integrity._cleanup_old_integrity_generations_once)
    cleanup_source += inspect.getsource(cleanup_terminal_operations)
    status_source = inspect.getsource(storage_router.storage_status)
    status_source += inspect.getsource(storage_monitoring.build_storage_monitoring_summary)
    status_source += inspect.getsource(storage_monitoring.build_lightweight_storage_monitoring_summary)

    assert "cleanup_old_integrity_generations" not in status_source
    assert "cleanup_terminal_operations" not in status_source
    for forbidden in ("archive_root_runtime_path", "safe_resolve_relative_for_root", "unlink(", "rmtree("):
        assert forbidden not in cleanup_source
