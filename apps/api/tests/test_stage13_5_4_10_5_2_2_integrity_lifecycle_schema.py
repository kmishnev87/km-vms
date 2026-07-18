import os
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.version import APP_VERSION
from app.db.session import Base
import app.models  # noqa: F401,E402 - register the complete production graph
from app.models.archive_integrity import (
    ArchiveIntegrityFinding,
    ArchiveIntegrityRemediationItem,
    ArchiveIntegrityRemediationPlan,
    ArchiveIntegrityScan,
)
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.models.storage_operation import StorageOperation
from app.models.user import User
from app.services import archive_integrity as integrity
from app.services import archive_integrity_remediation as remediation
from app.services import migration_maintenance
from app.services.archive_integrity import cleanup_old_integrity_generations
from app.services.recording_storage import archive_root_runtime_access_state
from app.services.schema_migrations import (
    MIGRATION_SOURCE,
    PRODUCTION_MIGRATIONS,
    STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION,
    STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID,
    MigrationRegistry,
    SchemaMigrationBlocked,
    build_migration_plan,
    execute_migration_plan,
)
from app.services.schema_versioning import (
    CURRENT_BASELINE_ID,
    CURRENT_SCHEMA_VERSION,
    CURRENT_STATE_ID,
)
from app.services.storage_operations_foundation import (
    StorageOperationLeaseLost,
    actor_identity,
    request_fingerprint,
)
from test_stage13_5_4_10_3_archive_integrity import (
    active_findings,
    add_camera,
    add_segment,
    eligible_orphan,
    expire_operation,
    owner,
    run_scan,
)


POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL") or os.getenv("KMVMS_STAGE3_POSTGRES_URL")


class SimulatedClaimCrash(RuntimeError):
    pass


@pytest.fixture
def stage410522_postgres(tmp_path, monkeypatch):
    if not POSTGRES_URL:
        pytest.fail("A disposable PostgreSQL URL is required for Stage 4.10.5.2.2")
    schema = f"stage410522_{uuid.uuid4().hex}"
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
    original_storage_root = settings.storage_root
    settings.storage_root = str(tmp_path / "default-archive")
    Path(settings.storage_root, "kmvms", "recordings").mkdir(parents=True)
    remediation._reset_unbound_recovery_scan_state()
    try:
        yield type(
            "Stage410522Context",
            (),
            {
                "db": db,
                "engine": engine,
                "Session": Session,
                "schema": schema,
                "tmp_path": tmp_path,
                "monkeypatch": monkeypatch,
            },
        )()
    finally:
        db.close()
        engine.dispose()
        base_engine.dispose()
        remediation._reset_unbound_recovery_scan_state()
        settings.storage_root = original_storage_root
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _audit(*, event_type: str, target_type: str, target_id: str, when: datetime) -> AuditEvent:
    return AuditEvent(
        id=str(uuid.uuid4()),
        created_at=when,
        actor_user_id=None,
        actor_username=None,
        actor_role=None,
        category="storage",
        event_type=event_type,
        severity="info",
        message_ru="Stage 4.10.5.2.2 evidence",
        message_en="Stage 4.10.5.2.2 evidence",
        target_type=target_type,
        target_id=target_id,
        event_metadata={},
    )


def _completed_operation(
    *,
    operation_id: str,
    operation_type: str,
    actor_key: str,
    idempotency_key: str,
    fingerprint: str,
    result: dict,
    scope: dict,
    when: datetime,
) -> StorageOperation:
    return StorageOperation(
        id=operation_id,
        operation_type=operation_type,
        actor_kind="system",
        actor_key=actor_key,
        actor_user_id=None,
        system_owner="system",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        status="completed",
        scope=scope,
        progress={"planned_count": 1, "completed_count": 1},
        result=result,
        cancel_allowed=False,
        retry_allowed=False,
        fencing_token=1,
        revision=2,
        queued_at=when,
        started_at=when,
        heartbeat_at=when,
        finished_at=when,
        created_at=when,
        updated_at=when,
    )


def _add_scan(ctx, *, actor_key: str, root: ArchiveRoot | None, when: datetime) -> ArchiveIntegrityScan:
    operation_id = f"integrity-scan-{uuid.uuid4().hex}"
    operation = _completed_operation(
        operation_id=operation_id,
        operation_type="integrity_scan",
        actor_key=actor_key,
        idempotency_key=uuid.uuid4().hex,
        fingerprint=uuid.uuid4().hex * 2,
        result={"status": "completed"},
        scope={"global": True},
        when=when,
    )
    ctx.db.add(operation)
    ctx.db.flush()
    scan = ArchiveIntegrityScan(
        id=str(uuid.uuid4()),
        operation_id=operation.id,
        actor_user_id=None,
        actor_key=actor_key,
        active_slot=None,
        status="completed",
        phase="completed",
        root_snapshot=[] if root is None else [{"root_id": root.id}],
        root_snapshot_hash=uuid.uuid4().hex * 2,
        segment_high_watermark=0,
        scan_cutoff_at=when,
        metadata_cursor=0,
        planned_count=1 if root is not None else 0,
        checked_count=1 if root is not None else 0,
        found_count=1 if root is not None else 0,
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
        finished_at=when,
        created_at=when,
        updated_at=when,
    )
    ctx.db.add(scan)
    ctx.db.add_all(
        [
            _audit(
                event_type="storage_operation.finished",
                target_type="storage_operation",
                target_id=operation.id,
                when=when,
            ),
            _audit(
                event_type="archive_integrity.scan_completed",
                target_type="archive_integrity_scan",
                target_id=scan.id,
                when=when,
            ),
        ]
    )
    ctx.db.commit()
    return scan


def _add_prepared_plan(ctx, *, label: str, when: datetime | None = None):
    when = when or datetime.utcnow()
    actor_key = actor_identity(None)[1]
    archive = ctx.tmp_path / f"archive-{label}"
    namespace = archive / "kmvms" / "recordings"
    namespace.mkdir(parents=True)
    root = ArchiveRoot(
        id=str(uuid.uuid4()),
        label=f"Root {label}",
        root_path=str(archive),
        storage_namespace="kmvms/recordings",
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity=f"physical-{label}-{uuid.uuid4().hex}",
        created_at=when,
        updated_at=when,
    )
    camera = Camera(
        name=f"Camera {label} {uuid.uuid4().hex}",
        storage_folder_name=f"camera-{label}-{uuid.uuid4().hex}",
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
        created_at=when,
        updated_at=when,
    )
    ctx.db.add_all((root, camera))
    ctx.db.flush()
    relative_ref = f"kmvms/recordings/{label}-missing.mkv"
    segment = RecordingSegment(
        camera_id=int(camera.id),
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=str(archive / relative_ref),
        relative_path=relative_ref,
        started_at=when - timedelta(minutes=40),
        ended_at=when - timedelta(minutes=30),
        duration_sec=300,
        size_bytes=1024,
        stream_type="main",
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=root.id,
        storage_namespace="kmvms/recordings",
        finalized_at=when - timedelta(minutes=30),
        created_at=when,
        updated_at=when,
    )
    ctx.db.add(segment)
    ctx.db.flush()
    scan = _add_scan(ctx, actor_key=actor_key, root=root, when=when)
    access = archive_root_runtime_access_state(root)
    root_snapshot_key = integrity._root_snapshot_key(root)
    root_access_identity = integrity._root_access_identity(root, access)
    metadata_version = integrity._metadata_version(segment)
    finding = ArchiveIntegrityFinding(
        id=str(uuid.uuid4()),
        scan_id=scan.id,
        finding_scope="metadata",
        category="missing_file",
        severity="error",
        impact_key="recording_unavailable",
        root_id=root.id,
        root_label_snapshot=root.label,
        physical_identity=root.physical_identity,
        camera_id=int(camera.id),
        camera_name_snapshot=camera.name,
        segment_id=int(segment.id),
        relative_ref=relative_ref,
        display_name=Path(relative_ref).name,
        observed_facts={
            "root_snapshot_key": root_snapshot_key,
            "root_access_identity": root_access_identity,
        },
        metadata_version=metadata_version,
        action_key="retire_missing_recording",
        required_permission="delete_recordings",
        confirmation_level="destructive_catalog",
        is_active=True,
        state="active",
        observation_count=1,
        first_observed_at=when,
        last_observed_at=when,
        created_at=when,
        updated_at=when,
    )
    ctx.db.add(finding)
    ctx.db.flush()
    plan_id = str(uuid.uuid4())
    item_id = str(uuid.uuid4())
    idempotency_key = uuid.uuid4().hex
    request_identity = {
        "finding_id": finding.id,
        "action_key": "retire_missing_recording",
        "scan_id": scan.id,
        "idempotency_key": idempotency_key,
    }
    fingerprint = request_fingerprint(request_identity)
    prepare = _completed_operation(
        operation_id=f"integrity-plan-{uuid.uuid4().hex}",
        operation_type="integrity_plan_prepare",
        actor_key=actor_key,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        result={"status": "completed", "plan_id": plan_id, "item_count": 1},
        scope={
            "global": False,
            "root_ids": [root.id],
            "camera_ids": [int(camera.id)],
            "segment_ids": [int(segment.id)],
        },
        when=when,
    )
    evidence = {
        "segment_version": metadata_version,
        "root_snapshot_key": root_snapshot_key,
        "root_access_identity": root_access_identity,
        "relative_ref": relative_ref,
    }
    item = ArchiveIntegrityRemediationItem(
        id=item_id,
        plan_id=plan_id,
        finding_id=finding.id,
        item_index=0,
        segment_id=int(segment.id),
        root_id=root.id,
        relative_ref=relative_ref,
        intended_mutation="retire_missing_metadata",
        evidence=evidence,
        state="prepared",
        created_at=when,
        updated_at=when,
    )
    canonical_hash = remediation._canonical_hash(
        {
            "schema_version": remediation.PLAN_SCHEMA_VERSION,
            "plan_id": plan_id,
            "scan_id": scan.id,
            "finding_id": finding.id,
            "action_kind": "retire_missing_recording",
            "required_permission": "delete_recordings",
            "item_id": item_id,
            "item_index": 0,
            "intended_mutation": "retire_missing_metadata",
            "evidence": evidence,
        }
    )
    plan = ArchiveIntegrityRemediationPlan(
        id=plan_id,
        scan_id=scan.id,
        finding_id=finding.id,
        operation_id=prepare.id,
        apply_operation_id=None,
        actor_user_id=None,
        actor_key=actor_key,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        action_kind="retire_missing_recording",
        required_permission="delete_recordings",
        confirmation_level="destructive_catalog",
        schema_version=remediation.PLAN_SCHEMA_VERSION,
        item_count=1,
        total_bytes=1024,
        canonical_hash=canonical_hash,
        state="prepared",
        created_at=when,
        expires_at=datetime.utcnow() + timedelta(minutes=30),
        updated_at=when,
    )
    ctx.db.add(prepare)
    ctx.db.flush()
    ctx.db.add(plan)
    ctx.db.flush()
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
    return type(
        "PreparedPlan",
        (),
        {
            "plan_id": plan.id,
            "item_id": item.id,
            "finding_id": finding.id,
            "scan_id": scan.id,
            "root_id": root.id,
            "segment_id": segment.id,
            "archive": archive,
        },
    )()


def _claim_then_crash(ctx, prepared, monkeypatch) -> str:
    real_claim = remediation.claim_operation_with_conflicts
    requested_operation_id = f"integrity-apply-{uuid.uuid4().hex}"

    def claim_and_crash(*args, **kwargs):
        real_claim(*args, **kwargs)
        raise SimulatedClaimCrash("crash_after_real_foundation_claim_commit")

    monkeypatch.setattr(remediation, "claim_operation_with_conflicts", claim_and_crash)
    with pytest.raises(SimulatedClaimCrash):
        remediation._coordinated_apply_claim(
            ctx.db,
            plan_id=prepared.plan_id,
            actor=None,
            requested_operation_id=requested_operation_id,
            expected_actor_key=actor_identity(None)[1],
            allow_create=True,
        )
    monkeypatch.setattr(remediation, "claim_operation_with_conflicts", real_claim)
    ctx.db.rollback()
    operation = (
        ctx.db.query(StorageOperation)
        .filter(StorageOperation.domain_ref == prepared.plan_id)
        .one()
    )
    assert operation.id == requested_operation_id
    operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    ctx.db.add(operation)
    ctx.db.commit()
    return str(operation.id)


def _terminal_audit_count(ctx, *, event_type: str, target_type: str, target_id: str) -> int:
    return int(
        ctx.db.query(AuditEvent.id)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_type == target_type,
            AuditEvent.target_id == target_id,
        )
        .count()
    )


def _assert_blocked_convergence(ctx, prepared, operation_id: str, reason_code: str):
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    item = ctx.db.get(ArchiveIntegrityRemediationItem, prepared.item_id)
    operation = ctx.db.get(StorageOperation, operation_id)
    assert plan.apply_operation_id == operation_id
    assert plan.state == item.state == operation.status == "blocked"
    assert plan.reason_code == item.result_code == operation.reason_code == reason_code
    assert plan.result_summary == {"status": "blocked", "mutated_count": 0}
    assert operation.result == {
        "status": "blocked",
        "plan_id": prepared.plan_id,
        "mutated_count": 0,
    }
    assert plan.retry_mode == operation.retry_mode == "new_scan"
    assert plan.next_action == operation.next_action == "create_new_integrity_scan"
    assert operation.retry_allowed is True
    assert _terminal_audit_count(
        ctx,
        event_type="storage_operation.finished",
        target_type="storage_operation",
        target_id=operation_id,
    ) == 1
    assert _terminal_audit_count(
        ctx,
        event_type="archive_integrity.remediation_blocked",
        target_type="archive_integrity_plan",
        target_id=prepared.plan_id,
    ) == 1


def _forbid_remediation_mutations(monkeypatch):
    calls = {"count": 0}

    def forbidden(*_args, **_kwargs):
        calls["count"] += 1
        raise AssertionError("physical remediation helper must not run")

    for helper_name in ("_apply_missing", "_apply_stale", "_apply_unusable", "_apply_orphan"):
        monkeypatch.setattr(remediation, helper_name, forbidden)
    return calls


def _same_apply_identity_count(ctx, operation: StorageOperation) -> int:
    return int(
        ctx.db.query(StorageOperation.id)
        .filter(
            StorageOperation.actor_key == str(operation.actor_key),
            StorageOperation.operation_type == str(operation.operation_type),
            StorageOperation.idempotency_key == str(operation.idempotency_key),
            StorageOperation.request_fingerprint == str(operation.request_fingerprint),
        )
        .count()
    )


@pytest.mark.parametrize(
    "stale_kind",
    ("finding_inactive", "scan_stale", "root_retired", "root_missing", "segment_deleted"),
)
def test_postgresql_exact_unbound_permanent_context_converges_without_mutation(
    stage410522_postgres,
    monkeypatch,
    stale_kind,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label=stale_kind)
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    if stale_kind == "finding_inactive":
        finding = ctx.db.get(ArchiveIntegrityFinding, prepared.finding_id)
        finding.is_active = False
        finding.state = "resolved"
        finding.resolved_at = datetime.utcnow()
        ctx.db.add(finding)
    elif stale_kind == "scan_stale":
        scan = ctx.db.get(ArchiveIntegrityScan, prepared.scan_id)
        scan.is_stale = True
        ctx.db.add(scan)
    elif stale_kind == "root_retired":
        root = ctx.db.get(ArchiveRoot, prepared.root_id)
        root.retired_at = datetime.utcnow()
        ctx.db.add(root)
    elif stale_kind == "root_missing":
        root = ctx.db.get(ArchiveRoot, prepared.root_id)
        ctx.db.delete(root)
    else:
        segment = ctx.db.get(RecordingSegment, prepared.segment_id)
        segment.status = "deleted"
        segment.deleted_at = datetime.utcnow()
        ctx.db.add(segment)
    ctx.db.commit()

    mutation_calls = {"count": 0}

    def forbidden_mutation(*_args, **_kwargs):
        mutation_calls["count"] += 1
        raise AssertionError("destructive remediation helper must not run")

    monkeypatch.setattr(remediation, "_apply_missing", forbidden_mutation)
    monkeypatch.setattr(remediation, "_apply_stale", forbidden_mutation)
    monkeypatch.setattr(remediation, "_apply_unusable", forbidden_mutation)
    monkeypatch.setattr(remediation, "_apply_orphan", forbidden_mutation)

    assert remediation.recover_pending_remediation_once(ctx.db) is True
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_context_stale_before_binding",
    )
    assert mutation_calls["count"] == 0
    assert remediation.recover_pending_remediation_once(ctx.db) is False
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_context_stale_before_binding",
    )


def test_postgresql_exact_unbound_expired_plan_converges_with_same_operation(stage410522_postgres, monkeypatch):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="expired")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
    ctx.db.add(plan)
    ctx.db.commit()

    assert remediation.recover_pending_remediation_once(ctx.db) is True
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_expired_before_binding",
    )
    assert ctx.db.query(StorageOperation).filter(StorageOperation.domain_ref == prepared.plan_id).count() == 1


def test_postgresql_transient_root_unavailable_retries_then_expiry_converges(stage410522_postgres, monkeypatch):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="transient")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    real_access = remediation.archive_root_runtime_access_state

    def unavailable_access(root):
        return {**real_access(root), "read_access_state": "unavailable"}

    monkeypatch.setattr(remediation, "archive_root_runtime_access_state", unavailable_access)
    assert remediation.recover_pending_remediation_once(ctx.db) is False
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    item = ctx.db.get(ArchiveIntegrityRemediationItem, prepared.item_id)
    operation = ctx.db.get(StorageOperation, operation_id)
    assert plan.apply_operation_id is None and plan.state == item.state == "prepared"
    assert operation.status == "running" and operation.finished_at is None
    plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
    ctx.db.add(plan)
    ctx.db.commit()
    assert remediation.recover_pending_remediation_once(ctx.db) is True
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_expired_before_binding",
    )


def test_postgresql_crash_after_reclaim_converges_on_next_poll(stage410522_postgres, monkeypatch):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="reclaim-crash")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    finding = ctx.db.get(ArchiveIntegrityFinding, prepared.finding_id)
    finding.is_active = False
    finding.state = "resolved"
    ctx.db.add(finding)
    ctx.db.commit()
    real_stage = remediation.stage_operation_terminal

    def crash_before_terminal_commit(*_args, **_kwargs):
        raise RuntimeError("crash_after_reclaim_before_terminal_commit")

    monkeypatch.setattr(remediation, "stage_operation_terminal", crash_before_terminal_commit)
    with pytest.raises(RuntimeError, match="crash_after_reclaim"):
        remediation.recover_pending_remediation_once(ctx.db)
    monkeypatch.setattr(remediation, "stage_operation_terminal", real_stage)
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    operation = ctx.db.get(StorageOperation, operation_id)
    assert plan.apply_operation_id is None and plan.state == "prepared"
    assert operation.status == "running" and operation.finished_at is None
    operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    ctx.db.add(operation)
    ctx.db.commit()

    assert remediation.recover_pending_remediation_once(ctx.db) is True
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_context_stale_before_binding",
    )


@pytest.mark.parametrize("audit_boundary", ("operation", "remediation"))
def test_postgresql_terminal_commit_audit_boundaries_recover_exactly_once(
    stage410522_postgres,
    monkeypatch,
    audit_boundary,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label=f"audit-{audit_boundary}")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
    ctx.db.add(plan)
    ctx.db.commit()
    if audit_boundary == "operation":
        real_helper = remediation.ensure_operation_terminal_audit
        monkeypatch.setattr(
            remediation,
            "ensure_operation_terminal_audit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("operation_audit_crash")),
        )
    else:
        real_helper = remediation._ensure_remediation_terminal_audit
        monkeypatch.setattr(
            remediation,
            "_ensure_remediation_terminal_audit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("remediation_audit_crash")),
        )

    with pytest.raises(RuntimeError, match="audit_crash"):
        remediation.recover_pending_remediation_once(ctx.db)
    if audit_boundary == "operation":
        monkeypatch.setattr(remediation, "ensure_operation_terminal_audit", real_helper)
    else:
        monkeypatch.setattr(remediation, "_ensure_remediation_terminal_audit", real_helper)
    ctx.db.rollback()
    assert remediation.recover_terminal_remediation_audit_once(ctx.db) is True
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_expired_before_binding",
    )
    assert remediation.recover_terminal_remediation_audit_once(ctx.db) is False


def test_postgresql_unknown_session_failure_rolls_back_and_preserves_candidate(stage410522_postgres, monkeypatch):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="unknown-db")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)

    def fail_unknown(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, RuntimeError("infrastructure unavailable"))

    monkeypatch.setattr(remediation, "_apply_context", fail_unknown)
    with pytest.raises(OperationalError):
        remediation.recover_pending_remediation_once(ctx.db)
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    operation = ctx.db.get(StorageOperation, operation_id)
    assert plan.apply_operation_id is None and plan.state == "prepared"
    assert operation.status == "running" and operation.finished_at is None
    assert ctx.db.query(ArchiveIntegrityRemediationPlan).count() == 1


@pytest.mark.parametrize("bad_truth", ("tampered_scope", "multiple_candidates"))
def test_postgresql_tampered_or_multiple_candidates_fail_closed(stage410522_postgres, monkeypatch, bad_truth):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label=bad_truth)
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    operation = ctx.db.get(StorageOperation, operation_id)
    if bad_truth == "tampered_scope":
        operation.scope = {
            "global": False,
            "physical_volume_ids": ["tampered-volume"],
            "root_ids": ["tampered-root"],
            "camera_ids": [],
            "segment_ids": [],
        }
        ctx.db.add(operation)
    else:
        duplicate = StorageOperation(
            id=f"integrity-apply-{uuid.uuid4().hex}",
            operation_type=operation.operation_type,
            actor_kind="system",
            actor_key="system:contradictory",
            system_owner="contradictory",
            idempotency_key=uuid.uuid4().hex,
            request_fingerprint=uuid.uuid4().hex * 2,
            domain_ref=prepared.plan_id,
            status="running",
            scope=dict(operation.scope or {}),
            progress={},
            result=None,
            cancel_allowed=False,
            retry_allowed=False,
            fencing_token=1,
            revision=1,
            lease_expires_at=datetime.utcnow() - timedelta(seconds=1),
        )
        ctx.db.add(duplicate)
    ctx.db.commit()

    assert remediation.recover_pending_remediation_once(ctx.db) is False
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    item = ctx.db.get(ArchiveIntegrityRemediationItem, prepared.item_id)
    assert plan.apply_operation_id is None and plan.state == item.state == "prepared"
    assert ctx.db.get(StorageOperation, operation_id).status == "running"


def test_postgresql_valid_exact_candidate_adopts_and_completes_without_second_operation(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="valid-adoption")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)

    assert remediation.recover_pending_remediation_once(ctx.db) is True
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    item = ctx.db.get(ArchiveIntegrityRemediationItem, prepared.item_id)
    operation = ctx.db.get(StorageOperation, operation_id)
    segment = ctx.db.get(RecordingSegment, prepared.segment_id)
    assert plan.apply_operation_id == operation_id
    assert plan.state == item.state == operation.status == "completed"
    assert segment.status == "deleted"
    assert ctx.db.query(StorageOperation).filter(StorageOperation.domain_ref == prepared.plan_id).count() == 1


def test_postgresql_live_foreign_exact_candidate_stays_unbound_without_mutation(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="live-foreign-owner")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    operation = ctx.db.get(StorageOperation, operation_id)
    operation.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    ctx.db.add(operation)
    ctx.db.commit()
    mutation_calls = _forbid_remediation_mutations(monkeypatch)
    audit_count_before = ctx.db.query(AuditEvent.id).count()

    coordinated = remediation._coordinated_apply_claim(
        ctx.db,
        plan_id=prepared.plan_id,
        actor=None,
        requested_operation_id=operation_id,
        expected_actor_key=None,
        allow_create=False,
    )

    assert coordinated["claimed"]["state"] == "preserved"
    assert coordinated["claimed"]["operation"]["operation_id"] == operation_id
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    item = ctx.db.get(ArchiveIntegrityRemediationItem, prepared.item_id)
    operation = ctx.db.get(StorageOperation, operation_id)
    assert plan.apply_operation_id is None
    assert plan.state == item.state == "prepared"
    assert operation.status == "running"
    assert mutation_calls["count"] == 0
    assert ctx.db.query(AuditEvent.id).count() == audit_count_before


def test_postgresql_bad_oldest_candidate_does_not_starve_later_valid_work(stage410522_postgres, monkeypatch):
    ctx = stage410522_postgres
    old = datetime.utcnow() - timedelta(minutes=2)
    bad = _add_prepared_plan(ctx, label="fairness-bad", when=old)
    bad_operation_id = _claim_then_crash(ctx, bad, monkeypatch)
    bad_operation = ctx.db.get(StorageOperation, bad_operation_id)
    bad_operation.scope = {"global": True, "root_ids": [], "camera_ids": [], "segment_ids": []}
    bad_plan = ctx.db.get(ArchiveIntegrityRemediationPlan, bad.plan_id)
    bad_plan.updated_at = old
    ctx.db.add_all((bad_operation, bad_plan))
    ctx.db.commit()

    good = _add_prepared_plan(ctx, label="fairness-good")
    good_operation_id = _claim_then_crash(ctx, good, monkeypatch)
    good_plan = ctx.db.get(ArchiveIntegrityRemediationPlan, good.plan_id)
    good_plan.updated_at = datetime.utcnow()
    ctx.db.add(good_plan)
    ctx.db.commit()

    assert remediation.recover_pending_remediation_once(ctx.db) is True
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, bad.plan_id).apply_operation_id is None
    assert ctx.db.get(StorageOperation, bad_operation_id).status == "running"
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, good.plan_id).apply_operation_id == good_operation_id
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, good.plan_id).state == "completed"


def test_postgresql_blocked_convergence_eventually_releases_old_history(stage410522_postgres, monkeypatch):
    ctx = stage410522_postgres
    old = datetime.utcnow() - timedelta(days=60)
    prepared = _add_prepared_plan(ctx, label="history-release", when=old)
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    finding = ctx.db.get(ArchiveIntegrityFinding, prepared.finding_id)
    finding.is_active = False
    finding.state = "resolved"
    finding.resolved_at = old
    ctx.db.add(finding)
    ctx.db.commit()
    assert remediation.recover_pending_remediation_once(ctx.db) is True
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_context_stale_before_binding",
    )
    terminal_plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    terminal_plan.expires_at = old
    ctx.db.add(terminal_plan)
    ctx.db.commit()
    _add_scan(ctx, actor_key=actor_identity(None)[1], root=None, when=datetime.utcnow())

    assert cleanup_old_integrity_generations(ctx.db, now=datetime.utcnow()) == 1
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityScan, prepared.scan_id) is None
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id) is None


def test_postgresql_background_worker_discovers_legacy_unbound_candidate_and_releases_history(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    old = datetime.utcnow() - timedelta(days=60)
    prepared = _add_prepared_plan(ctx, label="legacy-worker", when=old)
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    operation = ctx.db.get(StorageOperation, operation_id)
    operation.domain_ref = None
    operation.updated_at = old
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    plan.updated_at = old
    ctx.db.add_all((operation, plan))
    ctx.db.commit()

    assert remediation.recover_pending_remediation_once(ctx.db) is True
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    operation = ctx.db.get(StorageOperation, operation_id)
    segment = ctx.db.get(RecordingSegment, prepared.segment_id)
    assert plan.apply_operation_id == operation_id
    assert plan.state == operation.status == "completed"
    assert operation.domain_ref is None
    assert segment.status == "deleted"
    assert _same_apply_identity_count(ctx, operation) == 1

    plan.expires_at = old
    plan.updated_at = old
    ctx.db.add(plan)
    ctx.db.commit()
    _add_scan(ctx, actor_key=actor_identity(None)[1], root=None, when=datetime.utcnow())

    assert cleanup_old_integrity_generations(ctx.db, now=datetime.utcnow()) == 1
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityScan, prepared.scan_id) is None
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id) is None


def test_postgresql_legacy_unbound_identity_mismatch_stays_unbound(stage410522_postgres, monkeypatch):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="legacy-mismatch")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    operation = ctx.db.get(StorageOperation, operation_id)
    operation.domain_ref = None
    operation.request_fingerprint = uuid.uuid4().hex * 2
    original_status = str(operation.status)
    ctx.db.add(operation)
    ctx.db.commit()

    assert remediation.recover_pending_remediation_once(ctx.db) is False
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    operation = ctx.db.get(StorageOperation, operation_id)
    assert plan.apply_operation_id is None and plan.state == "prepared"
    assert operation.status == original_status and operation.finished_at is None
    assert _terminal_audit_count(
        ctx,
        event_type="storage_operation.finished",
        target_type="storage_operation",
        target_id=operation_id,
    ) == 0


def test_postgresql_unbound_worker_crosses_full_rejected_page_and_wraps_without_mutation(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    old = datetime.utcnow() - timedelta(hours=2)
    rejected = []
    for index in range(remediation.UNBOUND_RECOVERY_BATCH):
        prepared = _add_prepared_plan(
            ctx,
            label=f"fairness-rejected-{index}",
            when=old + timedelta(seconds=index),
        )
        operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
        operation = ctx.db.get(StorageOperation, operation_id)
        operation.scope = {"global": True, "root_ids": [], "camera_ids": [], "segment_ids": []}
        plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
        plan.updated_at = old + timedelta(seconds=index)
        ctx.db.add_all((operation, plan))
        ctx.db.commit()
        rejected.append(
            (
                prepared,
                operation_id,
                int(
                    ctx.db.query(AuditEvent.id)
                    .filter(AuditEvent.target_id.in_((prepared.plan_id, operation_id)))
                    .count()
                ),
            )
        )

    valid = _add_prepared_plan(
        ctx,
        label="fairness-valid-17",
        when=old + timedelta(minutes=5),
    )
    valid_operation_id = _claim_then_crash(ctx, valid, monkeypatch)
    valid_plan = ctx.db.get(ArchiveIntegrityRemediationPlan, valid.plan_id)
    valid_plan.updated_at = old + timedelta(minutes=5)
    ctx.db.add(valid_plan)
    ctx.db.commit()

    assert remediation.recover_pending_remediation_once(ctx.db) is True
    assert remediation._unbound_recovery_cursor_snapshot() is None
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, valid.plan_id).state == "completed"
    assert ctx.db.get(StorageOperation, valid_operation_id).status == "completed"

    for prepared, operation_id, audit_count in rejected:
        plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
        operation = ctx.db.get(StorageOperation, operation_id)
        assert plan.apply_operation_id is None and plan.state == "prepared"
        assert operation.status == "running" and operation.finished_at is None
        assert (
            ctx.db.query(AuditEvent.id)
            .filter(AuditEvent.target_id.in_((prepared.plan_id, operation_id)))
            .count()
            == audit_count
        )

    assert remediation.recover_pending_remediation_once(ctx.db) is False
    assert remediation._unbound_recovery_cursor_snapshot() is None
    for prepared, operation_id, audit_count in rejected:
        assert (
            ctx.db.query(AuditEvent.id)
            .filter(AuditEvent.target_id.in_((prepared.plan_id, operation_id)))
            .count()
            == audit_count
        )


def test_postgresql_unbound_worker_unknown_failure_does_not_advance_cursor(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="fairness-unknown")
    real_recover = remediation._recover_selected_remediation_once

    def fail_unknown(db, plan_id):
        assert plan_id == prepared.plan_id
        raise OperationalError("SELECT", {}, RuntimeError("infrastructure unavailable"))

    monkeypatch.setattr(remediation, "_recover_selected_remediation_once", fail_unknown)
    with pytest.raises(OperationalError):
        remediation.recover_pending_remediation_once(ctx.db)
    assert remediation._unbound_recovery_cursor_snapshot() is None
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id).state == "prepared"
    assert ctx.db.query(ArchiveIntegrityRemediationPlan.id).count() == 1
    monkeypatch.setattr(remediation, "_recover_selected_remediation_once", real_recover)


def test_postgresql_initial_claim_expiry_before_binding_blocks_same_operation(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="initial-claim-expiry")
    requested_operation_id = f"integrity-apply-{uuid.uuid4().hex}"
    real_bind = remediation._bind_apply_operation
    injected = {"done": False}
    mutation_calls = _forbid_remediation_mutations(monkeypatch)

    def expire_before_bind(db, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            plan = db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
            plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.add(plan)
            db.commit()
        return real_bind(db, **kwargs)

    monkeypatch.setattr(remediation, "_bind_apply_operation", expire_before_bind)
    coordinated = remediation._coordinated_apply_claim(
        ctx.db,
        plan_id=prepared.plan_id,
        actor=None,
        requested_operation_id=requested_operation_id,
        expected_actor_key=actor_identity(None)[1],
        allow_create=True,
    )
    assert coordinated["terminal_response"]["state"] == "blocked"
    _assert_blocked_convergence(
        ctx,
        prepared,
        requested_operation_id,
        "archive_integrity_apply_expired_before_binding",
    )
    operation = ctx.db.get(StorageOperation, requested_operation_id)
    assert _same_apply_identity_count(ctx, operation) == 1
    assert mutation_calls["count"] == 0

    replay = remediation._coordinated_apply_claim(
        ctx.db,
        plan_id=prepared.plan_id,
        actor=None,
        requested_operation_id=requested_operation_id,
        expected_actor_key=actor_identity(None)[1],
        allow_create=False,
    )
    assert replay["terminal_response"]["replayed"] is True
    _assert_blocked_convergence(
        ctx,
        prepared,
        requested_operation_id,
        "archive_integrity_apply_expired_before_binding",
    )


def test_postgresql_existing_exact_candidate_expiry_before_binding_blocks_same_operation(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="existing-candidate-expiry")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    real_bind = remediation._bind_apply_operation
    injected = {"done": False}
    mutation_calls = _forbid_remediation_mutations(monkeypatch)

    def expire_before_bind(db, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            plan = db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
            plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.add(plan)
            db.commit()
        return real_bind(db, **kwargs)

    monkeypatch.setattr(remediation, "_bind_apply_operation", expire_before_bind)
    coordinated = remediation._coordinated_apply_claim(
        ctx.db,
        plan_id=prepared.plan_id,
        actor=None,
        requested_operation_id=operation_id,
        expected_actor_key=actor_identity(None)[1],
        allow_create=False,
    )
    assert coordinated["terminal_response"]["state"] == "blocked"
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_expired_before_binding",
    )
    operation = ctx.db.get(StorageOperation, operation_id)
    assert _same_apply_identity_count(ctx, operation) == 1
    assert mutation_calls["count"] == 0


def test_postgresql_reclaim_expiry_with_restored_context_blocks_before_binding(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="reclaim-expiry-restored-context")
    operation_id = _claim_then_crash(ctx, prepared, monkeypatch)
    finding = ctx.db.get(ArchiveIntegrityFinding, prepared.finding_id)
    finding.is_active = False
    finding.state = "resolved"
    finding.resolved_at = datetime.utcnow()
    ctx.db.add(finding)
    ctx.db.commit()
    real_reclaim = remediation.reclaim_operation
    mutation_calls = _forbid_remediation_mutations(monkeypatch)

    def reclaim_then_expire(*args, **kwargs):
        claimed = real_reclaim(*args, **kwargs)
        plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
        restored = ctx.db.get(ArchiveIntegrityFinding, prepared.finding_id)
        plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
        restored.is_active = True
        restored.state = "active"
        restored.resolved_at = None
        ctx.db.add_all((plan, restored))
        ctx.db.commit()
        return claimed

    monkeypatch.setattr(remediation, "reclaim_operation", reclaim_then_expire)
    assert remediation.recover_pending_remediation_once(ctx.db) is True
    _assert_blocked_convergence(
        ctx,
        prepared,
        operation_id,
        "archive_integrity_apply_expired_before_binding",
    )
    assert mutation_calls["count"] == 0
    assert _same_apply_identity_count(ctx, ctx.db.get(StorageOperation, operation_id)) == 1


def test_postgresql_expiry_after_valid_binding_does_not_become_execution_timeout(
    stage410522_postgres,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="bound-before-expiry")
    requested_operation_id = f"integrity-apply-{uuid.uuid4().hex}"
    coordinated = remediation._coordinated_apply_claim(
        ctx.db,
        plan_id=prepared.plan_id,
        actor=None,
        requested_operation_id=requested_operation_id,
        expected_actor_key=actor_identity(None)[1],
        allow_create=True,
    )
    assert coordinated["claimed"]["state"] == "claimed"
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    assert plan.apply_operation_id == requested_operation_id and plan.state == "running"
    plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
    ctx.db.add(plan)
    ctx.db.commit()

    replay = remediation._coordinated_apply_claim(
        ctx.db,
        plan_id=prepared.plan_id,
        actor=None,
        requested_operation_id=requested_operation_id,
        expected_actor_key=actor_identity(None)[1],
        allow_create=False,
    )
    assert replay["claimed"]["state"] == "running"
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, prepared.plan_id)
    operation = ctx.db.get(StorageOperation, requested_operation_id)
    assert plan.state == "running" and operation.status == "running"
    assert plan.reason_code is None and operation.reason_code is None


def _seed_schema_truth(ctx, *, version: int, baseline: str = CURRENT_BASELINE_ID) -> None:
    ctx.db.query(SchemaMigrationHistory).delete(synchronize_session=False)
    ctx.db.query(SchemaVersionState).delete(synchronize_session=False)
    ctx.db.add(
        SchemaVersionState(
            id=CURRENT_STATE_ID,
            schema_version=version,
            baseline_id=baseline,
            app_version=APP_VERSION,
            app_build_version="stage410522-test",
            status="current",
            source=MIGRATION_SOURCE,
        )
    )
    ctx.db.add(
        SchemaMigrationHistory(
            migration_id=f"stage410522_seed_v{version}",
            previous_version=None,
            target_version=version,
            schema_version=version,
            baseline_id=baseline,
            app_version=APP_VERSION,
            app_build_version="stage410522-test",
            status="current",
            source=MIGRATION_SOURCE,
        )
    )
    ctx.db.commit()


def _force_v6_schema(ctx) -> None:
    ctx.db.execute(
        text(
            "ALTER TABLE archive_integrity_remediation_items "
            "ALTER COLUMN state TYPE VARCHAR(24)"
        )
    )
    ctx.db.commit()
    _seed_schema_truth(ctx, version=6)


def _state_column_length(ctx) -> int | None:
    column = next(
        item
        for item in sa_inspect(ctx.engine).get_columns("archive_integrity_remediation_items")
        if item["name"] == "state"
    )
    return getattr(column["type"], "length", None)


def _physical_test_context(ctx, *, label: str):
    archive = ctx.tmp_path / f"physical-{label}"
    namespace = archive / "kmvms" / "recordings"
    namespace.mkdir(parents=True)
    root = ArchiveRoot(
        id=str(uuid.uuid4()),
        label=f"Physical {label}",
        root_path=str(archive),
        storage_namespace="kmvms/recordings",
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity=f"physical-{label}-{uuid.uuid4().hex}",
    )
    actor = User(
        id=4103,
        username=f"stage410522-{label}-{uuid.uuid4().hex}",
        full_name="Stage 4.10.5.2.2 PostgreSQL actor",
        password_hash="test-only-not-a-login-secret",
        role="owner",
        is_active=True,
    )
    ctx.db.add_all((root, actor))
    ctx.db.commit()
    ctx.monkeypatch.setattr(integrity, "SessionLocal", ctx.Session)
    ctx.monkeypatch.setattr(integrity, "_safe_probe", lambda _path: (True, "probe_ok"))
    integrity._worker_stop.clear()
    return SimpleNamespace(
        db=ctx.db,
        engine=ctx.engine,
        Session=ctx.Session,
        root=root,
        actor=actor,
        archive=archive,
        namespace=namespace,
        tmp_path=ctx.tmp_path,
        monkeypatch=ctx.monkeypatch,
    )


def _unusable_plan(ctx, *, label: str):
    actor = ctx.actor
    camera = add_camera(ctx, name=f"Physical camera {label}")
    segment, path = add_segment(ctx, camera, name=f"{label}.mkv", content=b"")
    scan = run_scan(ctx, actor=actor, key=f"stage410522-{label}-scan")
    finding = next(
        row
        for row in active_findings(ctx, scan["scan_id"])
        if row.segment_id == segment.id and row.category == "zero_size_file"
    )
    plan = remediation.create_remediation_plan(
        ctx.db,
        finding_id=finding.id,
        action_key="delete_unusable_recording",
        actor=actor,
        idempotency_key=f"stage410522-{label}-plan",
    )
    return actor, segment, path, finding, plan


def test_postgresql_v6_to_v7_width_migration_preserves_rows_and_is_idempotent(
    stage410522_postgres,
    monkeypatch,
):
    ctx = stage410522_postgres
    prepared = _add_prepared_plan(ctx, label="schema-row")
    item_before = ctx.db.get(ArchiveIntegrityRemediationItem, prepared.item_id)
    value_before = (
        item_before.state,
        item_before.intended_mutation,
        dict(item_before.evidence or {}),
    )
    inspector = sa_inspect(ctx.engine)
    indexes_before = {
        item.get("name")
        for item in inspector.get_indexes("archive_integrity_remediation_items")
    }
    foreign_keys_before = {
        (
            tuple(item.get("constrained_columns") or ()),
            item.get("referred_table"),
            tuple(item.get("referred_columns") or ()),
        )
        for item in inspector.get_foreign_keys("archive_integrity_remediation_items")
    }
    _force_v6_schema(ctx)

    plan = build_migration_plan(ctx.db, registry=PRODUCTION_MIGRATIONS)
    assert plan["status"] == "ready"
    assert [item["migration_id"] for item in plan["pending_migrations"]] == [
        STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID
    ]
    monkeypatch.setattr(
        migration_maintenance,
        "build_backup_plan",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "backup_root_status": "ready",
            "backup_root_persistent": True,
        },
    )
    maintenance = migration_maintenance.inspect_migration_maintenance(
        ctx.db,
        registry=PRODUCTION_MIGRATIONS,
        actor=owner(4522),
    )
    assert maintenance["status"] == "pending"
    assert maintenance["backup_required"] is True
    assert maintenance["can_apply"] is True

    applied = execute_migration_plan(ctx.db, registry=PRODUCTION_MIGRATIONS)
    repeated = execute_migration_plan(ctx.db, registry=PRODUCTION_MIGRATIONS)
    ctx.db.expire_all()
    item_after = ctx.db.get(ArchiveIntegrityRemediationItem, prepared.item_id)
    inspector = sa_inspect(ctx.engine)
    indexes_after = {
        item.get("name")
        for item in inspector.get_indexes("archive_integrity_remediation_items")
    }
    foreign_keys_after = {
        (
            tuple(item.get("constrained_columns") or ()),
            item.get("referred_table"),
            tuple(item.get("referred_columns") or ()),
        )
        for item in inspector.get_foreign_keys("archive_integrity_remediation_items")
    }

    assert applied["executed_migrations"] == [STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID]
    assert repeated["executed_migrations"] == []
    assert _state_column_length(ctx) == 64
    assert ctx.db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == CURRENT_SCHEMA_VERSION == 7
    assert (
        ctx.db.query(SchemaMigrationHistory)
        .filter(
            SchemaMigrationHistory.migration_id == STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID,
            SchemaMigrationHistory.status == "applied",
        )
        .count()
        == 1
    )
    assert (item_after.state, item_after.intended_mutation, dict(item_after.evidence or {})) == value_before
    assert indexes_after == indexes_before
    assert foreign_keys_after == foreign_keys_before
    assert STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION.preflight(ctx.db)["status"] == "already_applied"


def test_postgresql_width_migration_failure_rolls_back_shape_version_and_history(stage410522_postgres):
    ctx = stage410522_postgres
    _force_v6_schema(ctx)

    def fail_verify(_db):
        raise RuntimeError("stage410522_injected_verify_failure")

    failing = replace(
        STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION,
        migration_id="stage13_5_4_10_5_2_2_integrity_item_state_width_failure_test",
        verify=fail_verify,
    )
    with pytest.raises(SchemaMigrationBlocked, match="stage410522_injected_verify_failure"):
        execute_migration_plan(ctx.db, registry=MigrationRegistry([failing]))

    ctx.db.expire_all()
    assert _state_column_length(ctx) == 24
    assert ctx.db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == 6
    failed_rows = (
        ctx.db.query(SchemaMigrationHistory)
        .filter(SchemaMigrationHistory.migration_id == failing.migration_id)
        .all()
    )
    assert len(failed_rows) == 1 and failed_rows[0].status == "failed"
    assert (
        ctx.db.query(SchemaMigrationHistory)
        .filter(SchemaMigrationHistory.migration_id == STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID)
        .count()
        == 0
    )


def test_postgresql_width_preflight_rejects_unexpected_or_unproven_shape(stage410522_postgres):
    ctx = stage410522_postgres
    _force_v6_schema(ctx)
    ctx.db.execute(
        text(
            "ALTER TABLE archive_integrity_remediation_items "
            "ALTER COLUMN state TYPE VARCHAR(32)"
        )
    )
    ctx.db.commit()
    with pytest.raises(RuntimeError, match="stage410522_integrity_item_state_shape_inconsistent"):
        STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION.preflight(ctx.db)

    ctx.db.execute(
        text(
            "ALTER TABLE archive_integrity_remediation_items "
            "ALTER COLUMN state TYPE VARCHAR(64)"
        )
    )
    ctx.db.commit()
    _seed_schema_truth(ctx, version=7)
    with pytest.raises(RuntimeError, match="stage410522_integrity_item_state_shape_inconsistent"):
        STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION.preflight(ctx.db)


def test_postgresql_current_model_declares_and_creates_state_width_64(stage410522_postgres):
    ctx = stage410522_postgres
    assert ArchiveIntegrityRemediationItem.__table__.c.state.type.length == 64
    assert _state_column_length(ctx) == 64


@pytest.mark.parametrize(
    ("version", "baseline", "blocked_reason"),
    ((8, CURRENT_BASELINE_ID, "future_version"), (6, "foreign-baseline", "unknown")),
)
def test_postgresql_width_migration_keeps_schema_version_guards(
    stage410522_postgres,
    version,
    baseline,
    blocked_reason,
):
    ctx = stage410522_postgres
    _seed_schema_truth(ctx, version=version, baseline=baseline)
    assert build_migration_plan(ctx.db, registry=PRODUCTION_MIGRATIONS)["blocked_reason"] == blocked_reason


def test_postgresql_physical_states_persist_in_real_apply_and_finish_once(stage410522_postgres):
    ctx = stage410522_postgres
    physical = _physical_test_context(ctx, label="state-width")
    actor, _segment, path, _finding, plan_public = _unusable_plan(physical, label="state-width")
    observed = {"prepared": False, "committed": False}
    original_execute = remediation.execute_segments
    original_terminal = remediation.stage_operation_terminal

    def observe_prepared(*args, **kwargs):
        with ctx.Session() as check:
            item = check.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan_public["plan_id"]).one()
            observed["prepared"] = item.state == "physical_mutation_prepared"
        return original_execute(*args, **kwargs)

    def observe_committed(*args, **kwargs):
        with ctx.Session() as check:
            plan = check.get(ArchiveIntegrityRemediationPlan, plan_public["plan_id"])
            item = check.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan.id).one()
            observed["committed"] = (
                plan.state == remediation.TERMINAL_PENDING_PLAN_STATE
                and item.state == "physical_mutation_committed"
            )
        return original_terminal(*args, **kwargs)

    ctx.monkeypatch.setattr(remediation, "execute_segments", observe_prepared)
    ctx.monkeypatch.setattr(remediation, "stage_operation_terminal", observe_committed)
    applied = remediation.apply_remediation_plan(
        ctx.db,
        plan_id=plan_public["plan_id"],
        actor=actor,
        confirm=True,
        operation_id=f"integrity-apply-{uuid.uuid4().hex}",
    )
    replay = remediation.apply_remediation_plan(
        ctx.db,
        plan_id=plan_public["plan_id"],
        actor=actor,
        confirm=True,
        operation_id=f"integrity-replay-{uuid.uuid4().hex}",
    )

    ctx.db.expire_all()
    item = ctx.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan_public["plan_id"]).one()
    assert observed == {"prepared": True, "committed": True}
    assert applied["state"] == item.state == "completed"
    assert replay["state"] == "completed" and replay["replayed"] is True
    assert path.exists() is False
    assert (
        ctx.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_integrity.remediation_completed",
            AuditEvent.target_id == plan_public["plan_id"],
        )
        .count()
        == 1
    )


def test_postgresql_crash_after_prepared_retries_then_mutates_once(stage410522_postgres):
    ctx = stage410522_postgres
    physical = _physical_test_context(ctx, label="prepared-crash")
    actor, _segment, path, _finding, plan_public = _unusable_plan(physical, label="prepared-crash")
    original_execute = remediation.execute_segments
    calls = {"count": 0}

    def crash_before_mutation(*_args, **_kwargs):
        calls["count"] += 1
        raise StorageOperationLeaseLost("stage410522_crash_before_physical_mutation")

    ctx.monkeypatch.setattr(remediation, "execute_segments", crash_before_mutation)
    pending = remediation.apply_remediation_plan(
        ctx.db,
        plan_id=plan_public["plan_id"],
        actor=actor,
        confirm=True,
        operation_id=f"integrity-apply-{uuid.uuid4().hex}",
    )
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, plan_public["plan_id"])
    item = ctx.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan.id).one()
    assert pending["state"] == "running"
    assert item.state == "physical_mutation_prepared" and path.exists()

    ctx.monkeypatch.setattr(remediation, "execute_segments", original_execute)
    expire_operation(physical, plan.apply_operation_id)
    assert remediation.recover_pending_remediation_once(ctx.db) is True
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, plan.id).state == "completed"
    assert path.exists() is False
    assert calls["count"] == 1


def test_postgresql_unlink_crash_recovers_without_second_delete(stage410522_postgres):
    ctx = stage410522_postgres
    physical = _physical_test_context(ctx, label="unlink-crash")
    actor, _segment, path, _finding, plan_public = _unusable_plan(physical, label="unlink-crash")
    original_execute = remediation.execute_segments
    original_persist = remediation._persist_physical_outcome
    execute_calls = {"count": 0}

    def count_execute(*args, **kwargs):
        execute_calls["count"] += 1
        return original_execute(*args, **kwargs)

    def crash_before_outcome(*_args, **_kwargs):
        raise StorageOperationLeaseLost("stage410522_crash_after_unlink")

    ctx.monkeypatch.setattr(remediation, "execute_segments", count_execute)
    ctx.monkeypatch.setattr(remediation, "_persist_physical_outcome", crash_before_outcome)
    pending = remediation.apply_remediation_plan(
        ctx.db,
        plan_id=plan_public["plan_id"],
        actor=actor,
        confirm=True,
        operation_id=f"integrity-apply-{uuid.uuid4().hex}",
    )
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, plan_public["plan_id"])
    item = ctx.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan.id).one()
    assert pending["state"] == "running"
    assert item.state == "physical_mutation_prepared"
    assert path.exists() is False and execute_calls["count"] == 1

    ctx.monkeypatch.setattr(remediation, "_persist_physical_outcome", original_persist)
    expire_operation(physical, plan.apply_operation_id)
    assert remediation.recover_pending_remediation_once(ctx.db) is True
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, plan.id).state == "completed"
    assert execute_calls["count"] == 1


def test_postgresql_orphan_unlink_crash_recovers_without_second_unlink(stage410522_postgres):
    ctx = stage410522_postgres
    physical = _physical_test_context(ctx, label="orphan-crash")
    actor = physical.actor
    path, finding = eligible_orphan(
        physical,
        name="stage410522-orphan.mkv",
        receipt_id="45220000-0000-0000-0000-000000000001",
        key_prefix="stage410522-orphan",
    )
    plan_public = remediation.create_remediation_plan(
        ctx.db,
        finding_id=finding.id,
        action_key="delete_proven_orphan",
        actor=actor,
        idempotency_key="stage410522-orphan-plan",
    )
    original_unlink = remediation.os.unlink
    original_persist = remediation._persist_physical_outcome
    unlink_calls: list[str] = []

    def tracked_unlink(*args, **kwargs):
        if str(args[0]).startswith("orphan-"):
            unlink_calls.append(str(args[0]))
        return original_unlink(*args, **kwargs)

    def crash_after_unlink(*_args, **_kwargs):
        raise StorageOperationLeaseLost("stage410522_crash_after_orphan_unlink")

    ctx.monkeypatch.setattr(remediation.os, "unlink", tracked_unlink)
    ctx.monkeypatch.setattr(remediation, "_persist_physical_outcome", crash_after_unlink)
    pending = remediation.apply_remediation_plan(
        ctx.db,
        plan_id=plan_public["plan_id"],
        actor=actor,
        confirm=True,
        operation_id=f"integrity-apply-{uuid.uuid4().hex}",
    )
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveIntegrityRemediationPlan, plan_public["plan_id"])
    item = ctx.db.query(ArchiveIntegrityRemediationItem).filter_by(plan_id=plan.id).one()
    assert pending["state"] == "running"
    assert item.state == "delete_committing"
    assert path.exists() is False and len(unlink_calls) == 1

    ctx.monkeypatch.setattr(remediation, "_persist_physical_outcome", original_persist)
    expire_operation(physical, plan.apply_operation_id)
    assert remediation.recover_pending_remediation_once(ctx.db) is True
    ctx.db.expire_all()
    assert ctx.db.get(ArchiveIntegrityRemediationPlan, plan.id).state == "completed"
    assert len(unlink_calls) == 1
