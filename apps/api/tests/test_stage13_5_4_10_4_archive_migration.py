import errno
import hashlib
import os
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.session import Base
from app.models.archive_migration import ArchiveMigrationItem, ArchiveMigrationPlan
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.storage_operation import StorageOperation
from app.models.user import User
from app.services import archive_migration as migration
from app.services.archive_migration import ArchiveMigrationBlocked, ArchiveMigrationPartial
from app.services.recording_storage import resolve_segment_file_path
from app.services.schema_migrations import STAGE4104_ARCHIVE_MIGRATION, STAGE4104_TABLES
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from app.services.storage_filesystem import MIGRATION_INTERNAL_NAMESPACE
from app.services.storage_operation_conflicts import operations_conflict_in_db
from app.services.storage_operations_foundation import MAX_RETRIES_PER_PARENT, MAX_RETRY_DEPTH, OperationHandle


STAGE4104_POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL") or os.getenv(
    "KMVMS_STAGE3_POSTGRES_URL"
)


@pytest.fixture
def stage4104(tmp_path, monkeypatch):
    original = {
        "storage_root": settings.storage_root,
        "storage_previews": settings.storage_previews,
        "storage_exports": settings.storage_exports,
        "storage_install_control": settings.storage_install_control,
    }
    source_path = tmp_path / "source"
    target_path = tmp_path / "target"
    third_path = tmp_path / "third"
    for root_path in (source_path, target_path, third_path):
        (root_path / "kmvms" / "recordings").mkdir(parents=True)
    settings.storage_root = str(source_path)
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")
    settings.storage_install_control = str(tmp_path / "control")

    engine = create_engine(
        f"sqlite:///{tmp_path / 'stage4104.sqlite'}",
        connect_args={"check_same_thread": False},
    )

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    owner = User(
        username="stage4104-owner",
        full_name="Stage 4104 Owner",
        password_hash="not-used",
        role="owner",
        is_active=True,
    )
    roots = {
        "source": ArchiveRoot(
            id="stage4104-source",
            label="Source",
            root_path=str(source_path),
            storage_namespace="kmvms/recordings",
            is_active=True,
            is_readable=True,
            is_writable=True,
            is_available=True,
            physical_identity="pv1:11111111111111111111111111111111",
        ),
        "target": ArchiveRoot(
            id="stage4104-target",
            label="Target",
            root_path=str(target_path),
            storage_namespace="kmvms/recordings",
            is_active=False,
            is_readable=True,
            is_writable=True,
            is_available=True,
            physical_identity="pv1:22222222222222222222222222222222",
        ),
        "third": ArchiveRoot(
            id="stage4104-third",
            label="Third",
            root_path=str(third_path),
            storage_namespace="kmvms/recordings",
            is_active=False,
            is_readable=True,
            is_writable=True,
            is_available=True,
            physical_identity="pv1:33333333333333333333333333333333",
        ),
    }
    db.add(owner)
    db.add_all(roots.values())
    db.commit()
    db.refresh(owner)
    monkeypatch.setattr(migration, "SessionLocal", Session)
    migration._worker_stop.clear()
    migration._reset_worker_candidate_scan_state()
    try:
        yield SimpleNamespace(
            db=db,
            engine=engine,
            Session=Session,
            owner=owner,
            roots=roots,
            paths={"source": source_path, "target": target_path, "third": third_path},
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
        )
    finally:
        migration._worker_stop.clear()
        migration._reset_worker_candidate_scan_state()
        db.close()
        engine.dispose()
        for key, value in original.items():
            setattr(settings, key, value)


@pytest.fixture
def stage4104_postgres():
    if not STAGE4104_POSTGRES_URL:
        pytest.fail(
            "KMVMS_STAGE2_POSTGRES_URL or KMVMS_STAGE3_POSTGRES_URL is required; "
            "the Stage 4.10.4 PostgreSQL exact-lookup test must not be skipped"
        )
    schema = f"stage4104_{uuid.uuid4().hex}"
    admin_engine = create_engine(STAGE4104_POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_engine(
        STAGE4104_POSTGRES_URL,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Session = sessionmaker(bind=scoped_engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        Base.metadata.create_all(bind=scoped_engine)
        yield SimpleNamespace(db=db, engine=scoped_engine, Session=Session, schema=schema)
    finally:
        db.close()
        scoped_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def add_camera(ctx, *, name="Camera A", soft_deleted=False):
    camera = Camera(
        name=name,
        storage_folder_name=name.lower().replace(" ", "-"),
        enabled=False,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
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
    root_name="source",
    name="segment.mkv",
    content=b"stage4104-video",
    status="finalized",
    age_minutes=30,
    create_file=True,
):
    root = ctx.roots[root_name]
    relative = f"kmvms/recordings/camera_{camera.id}/{name}"
    path = ctx.paths[root_name] / relative
    if create_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        stamp = (datetime.utcnow() - timedelta(minutes=age_minutes)).timestamp()
        os.utime(path, (stamp, stamp))
    old = datetime.utcnow() - timedelta(minutes=age_minutes)
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=relative,
        relative_path=relative,
        started_at=old,
        ended_at=old + timedelta(minutes=5) if status == "finalized" else None,
        finalized_at=old + timedelta(minutes=5) if status == "finalized" else None,
        duration_sec=300 if status == "finalized" else 0,
        size_bytes=len(content) if create_file else 0,
        stream_type="main",
        status=status,
        ownership="KM VMS",
        source="recorder",
        archive_root_id=root.id,
        archive_root_resolution_status="resolved",
        archive_root_resolution_detail="stage4104-test",
        archive_root_resolved_at=old,
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status="ok",
        reconciliation_status="ok_owned_finalized",
        created_at=old,
        updated_at=old,
    )
    ctx.db.add(segment)
    ctx.db.commit()
    ctx.db.refresh(segment)
    return segment, path


def add_archive_root(ctx, *, name):
    root_path = ctx.tmp_path / name
    (root_path / "kmvms" / "recordings").mkdir(parents=True)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    root = ArchiveRoot(
        id=f"stage4104-{name}"[:36],
        label=name,
        root_path=str(root_path),
        storage_namespace="kmvms/recordings",
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity=f"pv1:{digest}",
    )
    ctx.db.add(root)
    ctx.db.commit()
    ctx.roots[name] = root
    ctx.paths[name] = root_path
    return root


def prepare_plan(ctx, *, key="stage4104-plan", source_name="source", target_name="target"):
    requested = migration.request_migration_plan(
        ctx.db,
        actor=ctx.owner,
        source_root_id=ctx.roots[source_name].id,
        target_root_id=ctx.roots[target_name].id,
        idempotency_key=key,
    )
    assert requested["status"] == "building"
    assert migration._prepare_one_plan() is True
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveMigrationPlan, requested["plan_id"])
    assert plan.status in migration.PLAN_READY_STATUSES | {"blocked"}
    return plan


def queue_plan(ctx, plan, *, key="stage4104-apply"):
    result = migration.queue_migration_apply(
        ctx.db,
        actor=ctx.owner,
        plan_id=plan.id,
        expected_hash=plan.canonical_hash,
        idempotency_key=key,
    )
    assert result["operation"]["status"] == "queued"
    assert result["operation"]["operation_id"]
    return result


def create_queued_migration(ctx, *, key, source_name, target_name):
    camera = add_camera(ctx, name=f"Queued {key}")
    _segment, source_file = add_segment(
        ctx,
        camera,
        root_name=source_name,
        name=f"{key}.mkv",
        content=f"queued-{key}".encode("utf-8"),
    )
    plan = prepare_plan(
        ctx,
        key=f"{key}-plan",
        source_name=source_name,
        target_name=target_name,
    )
    queued = queue_plan(ctx, plan, key=f"{key}-apply")
    operation = ctx.db.get(StorageOperation, queued["operation"]["operation_id"])
    return plan, operation, source_file


def operation_invariant_snapshot(operation):
    return {
        "id": str(operation.id),
        "status": str(operation.status),
        "scope": deepcopy(operation.scope),
        "progress": deepcopy(operation.progress),
        "result": deepcopy(operation.result),
        "reason_code": operation.reason_code,
        "next_action": operation.next_action,
        "retry_mode": operation.retry_mode,
        "actor_kind": str(operation.actor_kind),
        "actor_key": str(operation.actor_key),
        "actor_user_id": operation.actor_user_id,
        "parent_operation_id": operation.parent_operation_id,
        "parent_snapshot": deepcopy(operation.parent_snapshot),
        "owner_token_hash": operation.owner_token_hash,
        "owner_instance_id": operation.owner_instance_id,
        "fencing_token": int(operation.fencing_token or 0),
        "started_at": operation.started_at,
        "heartbeat_at": operation.heartbeat_at,
        "finished_at": operation.finished_at,
    }


def add_malformed_worker_candidate(ctx, *, operation_id, queued_at, domain_ref):
    operation = StorageOperation(
        id=operation_id,
        operation_type=migration.MIGRATION_OPERATION_TYPE,
        actor_kind="user",
        actor_key=str(ctx.owner and f"user:{ctx.owner.id}"),
        actor_user_id=ctx.owner.id,
        idempotency_key=operation_id[:64],
        request_fingerprint=hashlib.sha256(operation_id.encode("utf-8")).hexdigest(),
        domain_ref=domain_ref,
        status="queued",
        scope={"root_ids": ["invalid-source", "invalid-target"]},
        progress={"phase": "queued"},
        cancel_allowed=True,
        fencing_token=0,
        queued_at=queued_at,
        created_at=queued_at,
        updated_at=queued_at,
    )
    ctx.db.add(operation)
    ctx.db.commit()
    return operation


def audit_event_ids(db):
    return {
        str(event_id)
        for (event_id,) in db.query(AuditEvent.id).order_by(AuditEvent.id.asc()).all()
    }


def invoke_orphan_repair(db, *, repair_mode, actor, parent_id, key):
    if repair_mode == "owner":
        return migration.retry_migration_operation(
            db,
            actor=actor,
            operation_id=parent_id,
            idempotency_key=key,
        )
    return migration.takeover_migration_cleanup(
        db,
        actor=actor,
        operation_id=parent_id,
        idempotency_key=key,
    )


def add_user(ctx, *, username, role="admin", active=True):
    user = User(
        username=username,
        full_name=username,
        password_hash="not-used",
        role=role,
        is_active=active,
    )
    ctx.db.add(user)
    ctx.db.commit()
    ctx.db.refresh(user)
    return user


def enable_sqlite_foreign_keys(ctx):
    ctx.db.commit()
    connection = ctx.db.connection()
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def create_cleanup_pending_migration(ctx, monkeypatch, *, key):
    camera = add_camera(ctx, name=f"Cleanup {key}")
    segment, source_file = add_segment(ctx, camera, name=f"{key}.mkv", content=b"cleanup-pending")
    plan = prepare_plan(ctx, key=f"{key}-plan")
    queued = queue_plan(ctx, plan, key=f"{key}-apply")
    real_cleanup = migration._cleanup_source

    def stop_before_cleanup(*_args, **_kwargs):
        raise ArchiveMigrationPartial("migration_source_cleanup_incomplete", retry_mode="cleanup_only")

    monkeypatch.setattr(migration, "_cleanup_source", stop_before_cleanup)
    assert migration._run_one_operation() is True
    monkeypatch.setattr(migration, "_cleanup_source", real_cleanup)
    ctx.db.expire_all()
    plan = ctx.db.get(ArchiveMigrationPlan, plan.id)
    operation = ctx.db.get(StorageOperation, queued["operation"]["operation_id"])
    item = ctx.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    assert plan.status == operation.status == "partial"
    assert plan.retry_mode == operation.retry_mode == "cleanup_only"
    assert item.phase == "metadata_switched"
    assert item.cleanup_pending is True
    assert source_file.exists()
    return plan, operation, item, segment, source_file


def fake_handle(ctx, plan, operation_id="stage4104-direct-op"):
    owner_token = "stage4104-owner-token"
    now = datetime.utcnow()
    operation = StorageOperation(
        id=operation_id,
        operation_type=migration.MIGRATION_OPERATION_TYPE,
        actor_kind="user",
        actor_key=str(plan.actor_key),
        actor_user_id=int(plan.actor_user_id),
        idempotency_key=operation_id[:64],
        request_fingerprint=migration.request_fingerprint(migration._operation_identity(plan)),
        domain_ref=migration._operation_domain_ref(str(plan.id)),
        status="running",
        scope={"root_ids": [str(plan.source_root_id), str(plan.target_root_id)]},
        progress={"permission_contract": migration._plan_permission_contract(plan)},
        cancel_allowed=True,
        owner_token_hash=hashlib.sha256(owner_token.encode("utf-8")).hexdigest(),
        owner_instance_id="stage4104-direct-worker",
        fencing_token=1,
        revision=1,
        queued_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )
    ctx.db.add(operation)
    plan.current_operation_id = operation_id
    plan.status = "running"
    plan.phase = "running"
    ctx.db.add(plan)
    ctx.db.commit()
    return OperationHandle(
        operation_id=operation_id,
        owner_token=owner_token,
        fencing_token=1,
        operation_type=migration.MIGRATION_OPERATION_TYPE,
    )


def take_over_handle(ctx, handle):
    owner_token = f"stage4104-takeover-{int(handle.fencing_token) + 1}"
    operation = (
        ctx.db.query(StorageOperation)
        .filter(StorageOperation.id == str(handle.operation_id))
        .populate_existing()
        .one()
    )
    operation.owner_token_hash = hashlib.sha256(owner_token.encode("utf-8")).hexdigest()
    operation.owner_instance_id = f"stage4104-worker-{int(handle.fencing_token) + 1}"
    operation.fencing_token = int(handle.fencing_token) + 1
    operation.heartbeat_at = datetime.utcnow()
    operation.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    operation.revision = int(operation.revision or 0) + 1
    ctx.db.add(operation)
    ctx.db.commit()
    return OperationHandle(
        operation_id=str(handle.operation_id),
        owner_token=owner_token,
        fencing_token=int(operation.fencing_token),
        operation_type=str(handle.operation_type),
    )


def bind_cleanup_item_to_terminal_parent(ctx, plan, item, handle, *, parent_fence=7):
    now = datetime.utcnow()
    current = ctx.db.get(StorageOperation, handle.operation_id)
    parent = StorageOperation(
        id=f"{handle.operation_id}-parent",
        operation_type=migration.MIGRATION_OPERATION_TYPE,
        actor_kind="user",
        actor_key=str(plan.actor_key),
        actor_user_id=int(plan.actor_user_id),
        idempotency_key=f"{handle.operation_id}-parent"[:64],
        request_fingerprint=migration.request_fingerprint(migration._operation_identity(plan)),
        domain_ref=migration._operation_domain_ref(str(plan.id)),
        status="partial",
        scope={"root_ids": [str(plan.source_root_id), str(plan.target_root_id)]},
        progress={"permission_contract": migration._plan_permission_contract(plan)},
        retry_mode="cleanup_only",
        retry_allowed=True,
        fencing_token=int(parent_fence),
        revision=1,
        retry_depth=0,
        queued_at=now,
        started_at=now,
        heartbeat_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    ctx.db.add(parent)
    ctx.db.flush()
    current.parent_operation_id = str(parent.id)
    current.parent_snapshot = {
        "operation_id": str(parent.id),
        "status": str(parent.status),
        "reason_code": parent.reason_code,
        "finished_at": parent.finished_at.isoformat(),
        "retry_depth": int(parent.retry_depth),
        "actor_key": str(parent.actor_key),
        "original_actor_key": str(plan.actor_key),
        "domain_ref": migration._operation_domain_ref(str(plan.id)),
        "retry_mode": "cleanup_only",
        "cross_actor_recovery": None,
    }
    current.retry_depth = 1
    plan.retry_mode = "cleanup_only"
    item.phase = "metadata_switched"
    item.metadata_switched_at = now
    item.cleanup_pending = True
    item.operation_id = str(parent.id)
    item.operation_fencing_token = int(parent_fence)
    ctx.db.add_all((current, plan, item))
    ctx.db.commit()
    return parent, current, item


def create_permission_revoked_cleanup_child(ctx, monkeypatch, *, key):
    plan, parent, item, _segment, source_file = create_cleanup_pending_migration(
        ctx,
        monkeypatch,
        key=key,
    )
    recovery_admin = add_user(ctx, username=f"{key}-recovery-admin")
    parent.fencing_token = 7
    item.operation_fencing_token = 7
    ctx.owner.is_active = False
    ctx.db.add_all((parent, item, ctx.owner))
    ctx.db.commit()
    queued = migration.takeover_migration_cleanup(
        ctx.db,
        actor=recovery_admin,
        operation_id=parent.id,
        idempotency_key=f"{key}-first-recovery",
    )
    recovery_admin.role = "viewer"
    ctx.db.commit()
    assert migration._run_one_operation() is True
    ctx.db.expire_all()
    child = ctx.db.get(StorageOperation, queued["operation"]["operation_id"])
    plan = ctx.db.get(ArchiveMigrationPlan, plan.id)
    item = ctx.db.get(ArchiveMigrationItem, item.id)
    assert child.status == plan.status == "blocked"
    assert child.reason_code == plan.reason_code == "migration_recovery_permission_revoked"
    assert child.retry_allowed is True
    assert child.retry_mode == plan.retry_mode == "cleanup_only"
    assert item.operation_id == parent.id
    assert item.operation_fencing_token == 7
    assert item.cleanup_pending is True
    assert source_file.exists()
    return plan, parent, child, item, recovery_admin, source_file


def terminalize_cleanup_child(ctx, plan, child, *, reason="migration_recovery_permission_revoked"):
    now = datetime.utcnow()
    child.status = "blocked"
    child.reason_code = reason
    child.retry_mode = "cleanup_only"
    child.retry_allowed = True
    child.cancel_allowed = False
    child.lease_expires_at = None
    child.finished_at = now
    child.updated_at = now
    plan.current_operation_id = str(child.id)
    plan.status = "blocked"
    plan.phase = "blocked"
    plan.reason_code = reason
    plan.retry_mode = "cleanup_only"
    plan.cleanup_pending = True
    plan.finished_at = now
    ctx.db.add_all((child, plan))
    ctx.db.commit()
    return child


def build_cleanup_retry_chain(ctx, monkeypatch, *, key, depth=MAX_RETRY_DEPTH):
    plan, parent, item, _segment, source_file = create_cleanup_pending_migration(
        ctx,
        monkeypatch,
        key=key,
    )
    current = parent
    for index in range(1, depth + 1):
        queued = migration.retry_migration_operation(
            ctx.db,
            actor=ctx.owner,
            operation_id=current.id,
            idempotency_key=f"{key}-depth-{index}",
        )
        current = ctx.db.get(StorageOperation, queued["operation"]["operation_id"])
        terminalize_cleanup_child(ctx, plan, current)
    ctx.db.expire_all()
    return (
        ctx.db.get(ArchiveMigrationPlan, plan.id),
        ctx.db.get(StorageOperation, current.id),
        ctx.db.get(ArchiveMigrationItem, item.id),
        source_file,
    )


def claim_orphan_cleanup_child(ctx, plan, previous, *, actor, key, cross_actor=False):
    authorization = (
        migration._cleanup_recovery_authorization(previous=previous, plan=plan)
        if cross_actor
        else None
    )
    claimed = migration.claim_operation_with_conflicts(
        ctx.db,
        operation_type=migration.MIGRATION_OPERATION_TYPE,
        scope={"root_ids": [str(plan.source_root_id), str(plan.target_root_id)]},
        request_identity=migration._operation_identity(plan),
        actor=actor,
        idempotency_key=key,
        parent_operation_id=str(previous.id),
        cross_actor_recovery=authorization,
        initial_progress=migration._operation_progress(plan, phase="queued"),
        start_immediately=False,
        cancel_allowed=True,
        domain_ref=migration._operation_domain_ref(str(plan.id)),
    )
    child = ctx.db.get(StorageOperation, claimed["operation"]["operation_id"])
    assert str(plan.current_operation_id) == str(previous.id)
    assert child.status == "queued"
    return child


def test_capacity_formulas_are_named_and_fail_closed_for_unknown_identity(stage4104):
    reserve = migration.migration_reserve_bytes(50_000_000_000)
    assert migration.migration_required_free_bytes(
        same_physical_volume=False,
        remaining_not_target_finalized_bytes=8_000,
        largest_next_item_size_bytes=3_000,
        reserve_bytes=reserve,
    ) == reserve + 8_000
    assert migration.migration_required_free_bytes(
        same_physical_volume=True,
        remaining_not_target_finalized_bytes=8_000,
        largest_next_item_size_bytes=3_000,
        reserve_bytes=reserve,
    ) == reserve + 3_000

    stage4104.roots["target"].physical_identity = None
    stage4104.db.commit()
    with pytest.raises(ArchiveMigrationBlocked, match="archive_root_physical_identity_unknown"):
        migration.request_migration_plan(
            stage4104.db,
            actor=stage4104.owner,
            source_root_id=stage4104.roots["source"].id,
            target_root_id=stage4104.roots["target"].id,
            idempotency_key="unknown-identity",
        )


def test_stage4104_additive_schema_preflight_apply_verify(stage4104):
    for table in reversed(STAGE4104_TABLES):
        table.drop(bind=stage4104.engine, checkfirst=True)
    preflight = STAGE4104_ARCHIVE_MIGRATION.preflight(stage4104.db)
    applied = STAGE4104_ARCHIVE_MIGRATION.apply(stage4104.db)
    verified = STAGE4104_ARCHIVE_MIGRATION.verify(stage4104.db)
    stage4104.db.commit()
    table_names = set(inspect(stage4104.engine).get_table_names())

    assert CURRENT_SCHEMA_VERSION == 6
    assert preflight["status"] == "ready"
    assert applied["created_or_verified_table_count"] == len(STAGE4104_TABLES)
    assert verified == {
        "status": "verified",
        "table_drift": False,
        "column_drift": False,
        "index_drift": False,
    }
    assert {table.name for table in STAGE4104_TABLES}.issubset(table_names)


def test_source_target_and_nested_overlap_are_blocked(stage4104):
    with pytest.raises(ArchiveMigrationBlocked, match="migration_source_equals_target"):
        migration.request_migration_plan(
            stage4104.db,
            actor=stage4104.owner,
            source_root_id=stage4104.roots["source"].id,
            target_root_id=stage4104.roots["source"].id,
            idempotency_key="same-root",
        )

    nested_path = stage4104.paths["source"] / "nested"
    (nested_path / "kmvms" / "recordings").mkdir(parents=True)
    nested = ArchiveRoot(
        id="stage4104-nested",
        label="Nested",
        root_path=str(nested_path),
        storage_namespace="kmvms/recordings",
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity=stage4104.roots["source"].physical_identity,
    )
    stage4104.db.add(nested)
    stage4104.db.commit()
    with pytest.raises(ArchiveMigrationBlocked, match="archive_root_overlap"):
        migration.request_migration_plan(
            stage4104.db,
            actor=stage4104.owner,
            source_root_id=stage4104.roots["source"].id,
            target_root_id=nested.id,
            idempotency_key="nested-root",
        )


def test_plan_scope_is_source_only_and_includes_soft_deleted_camera_archive(stage4104):
    camera = add_camera(stage4104, name="Normal Camera")
    soft_camera = add_camera(stage4104, name="Soft Deleted Camera", soft_deleted=True)
    included, _ = add_segment(stage4104, camera, name="included.mkv")
    retained, _ = add_segment(stage4104, soft_camera, name="retained.mkv")
    add_segment(stage4104, camera, root_name="third", name="third.mkv")
    add_segment(stage4104, camera, name="writing.mkv", status="writing")
    add_segment(stage4104, camera, name="recent.mkv", age_minutes=1)

    requested = migration.request_migration_plan(
        stage4104.db,
        actor=stage4104.owner,
        source_root_id=stage4104.roots["source"].id,
        target_root_id=stage4104.roots["target"].id,
        idempotency_key="source-scope",
    )
    new_after_watermark, _ = add_segment(stage4104, camera, name="new-after-watermark.mkv")
    assert migration._prepare_one_plan() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, requested["plan_id"])
    item_segment_ids = {
        value
        for (value,) in stage4104.db.query(ArchiveMigrationItem.segment_id)
        .filter(ArchiveMigrationItem.plan_id == plan.id)
        .all()
    }

    assert plan.status == "ready_with_exclusions"
    assert item_segment_ids == {included.id, retained.id}
    assert new_after_watermark.id not in item_segment_ids
    assert plan.excluded_summary["recording_not_finalized"] == 1
    assert plan.excluded_summary["recording_active_or_recent"] == 1


def test_plan_materializes_more_than_4096_items_without_hidden_ceiling(stage4104):
    camera = add_camera(stage4104, name="Large Camera")
    old = datetime.utcnow() - timedelta(hours=2)
    rows = []
    namespace = stage4104.paths["source"] / "kmvms" / "recordings" / f"camera_{camera.id}"
    namespace.mkdir(parents=True)
    for index in range(4105):
        relative = f"kmvms/recordings/camera_{camera.id}/segment-{index:05d}.mkv"
        (stage4104.paths["source"] / relative).touch()
        rows.append(
            RecordingSegment(
                camera_id=camera.id,
                camera_name_snapshot=camera.name,
                camera_folder_snapshot=camera.storage_folder_name,
                file_path=relative,
                relative_path=relative,
                started_at=old,
                ended_at=old + timedelta(minutes=5),
                finalized_at=old + timedelta(minutes=5),
                duration_sec=300,
                size_bytes=0,
                stream_type="main",
                status="finalized",
                ownership="KM VMS",
                source="recorder",
                archive_root_id=stage4104.roots["source"].id,
                archive_root_resolution_status="resolved",
                storage_namespace="kmvms/recordings",
                created_at=old,
                updated_at=old,
            )
        )
    stage4104.db.add_all(rows)
    stage4104.db.commit()

    plan = prepare_plan(stage4104, key="large-plan")
    assert plan.status == "ready"
    assert plan.item_count == 4105
    assert (
        stage4104.db.query(ArchiveMigrationItem)
        .filter(ArchiveMigrationItem.plan_id == plan.id)
        .count()
        == 4105
    )
    page = migration.list_migration_items(
        stage4104.db,
        actor=stage4104.owner,
        plan_id=plan.id,
        limit=10_000,
    )
    assert len(page["items"]) == migration.ITEM_PAGE_MAX
    assert page["has_more"] is True
    assert all("source_relative_path" not in item for item in page["items"])


def test_apply_is_queued_then_moves_exact_manifest_and_resolves_playback(stage4104):
    camera = add_camera(stage4104)
    segment, source_file = add_segment(stage4104, camera, content=b"move-me")
    plan = prepare_plan(stage4104)
    queued = queue_plan(stage4104, plan)

    assert source_file.exists()
    assert not (stage4104.paths["target"] / segment.relative_path).exists()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    assert operation.status == "queued"
    assert migration._run_one_operation() is True

    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    segment = stage4104.db.get(RecordingSegment, segment.id)
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    target_file = stage4104.paths["target"] / segment.relative_path
    assert plan.status == "completed"
    assert plan.completed_count == plan.item_count == 1
    assert plan.retained_source_count == 0
    assert item.phase == "completed"
    assert item.cleanup_pending is False
    assert operation.status == "completed"
    assert segment.archive_root_id == stage4104.roots["target"].id
    assert not source_file.exists()
    assert target_file.read_bytes() == b"move-me"
    assert target_file.stat().st_mode & 0o777 == 0o640
    assert resolve_segment_file_path(stage4104.db, segment, require_exists=True) == target_file


def test_exact_finalized_target_recovery_rejects_checksum_equal_foreign_inode(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    segment, source_file = add_segment(stage4104, camera, content=b"same-content")
    plan = prepare_plan(stage4104)
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan)
    monkeypatch.setattr(migration, "heartbeat_operation", lambda *_args, **_kwargs: None)
    source, target, _ = migration._runtime_item_guard(stage4104.db, plan, item)
    migration._copy_or_resume_temp(stage4104.db, plan, item, source, target, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    migration._verify_and_finalize_target(stage4104.db, plan, item, source, target, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    final_path = stage4104.paths["target"] / item.target_final_relative_path
    final_path.unlink()
    final_path.write_bytes(b"same-content")

    with pytest.raises(ArchiveMigrationPartial, match="migration_final_provenance_mismatch"):
        migration._process_item(stage4104.db, plan, item, handle)
    stage4104.db.refresh(segment)
    assert segment.archive_root_id == stage4104.roots["source"].id
    assert source_file.exists()


def test_recovery_after_physical_cleanup_before_item_persistence_is_idempotent(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    segment, source_file = add_segment(stage4104, camera, content=b"cleanup-crash")
    plan = prepare_plan(stage4104)
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, "stage4104-cleanup-op")
    monkeypatch.setattr(migration, "heartbeat_operation", lambda *_args, **_kwargs: None)
    source, target, _ = migration._runtime_item_guard(stage4104.db, plan, item)
    migration._copy_or_resume_temp(stage4104.db, plan, item, source, target, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    migration._verify_and_finalize_target(stage4104.db, plan, item, source, target, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    migration._switch_metadata(stage4104.db, plan, item, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    real_unlink = migration.unlink_relative
    calls = {"count": 0}

    def unlink_then_crash(*args, **kwargs):
        calls["count"] += 1
        real_unlink(*args, **kwargs)
        raise OSError("simulated crash after unlink")

    monkeypatch.setattr(migration, "unlink_relative", unlink_then_crash)
    with pytest.raises(OSError, match="simulated crash"):
        migration._cleanup_source(stage4104.db, plan, item, source, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert item.phase == "source_delete_committing"
    assert not source_file.exists()

    monkeypatch.setattr(migration, "unlink_relative", real_unlink)
    migration._process_item(stage4104.db, plan, item, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    stage4104.db.refresh(segment)
    assert calls["count"] == 1
    assert item.phase == "completed"
    assert item.cleanup_pending is False
    assert segment.archive_root_id == stage4104.roots["target"].id


def test_cleanup_retry_removes_only_exact_uncommitted_target_without_recopy(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    _segment, source_file = add_segment(stage4104, camera, content=b"cleanup-only")
    plan = prepare_plan(stage4104)
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, "stage4104-residue-op")
    monkeypatch.setattr(migration, "heartbeat_operation", lambda *_args, **_kwargs: None)
    source, target, _ = migration._runtime_item_guard(stage4104.db, plan, item)
    migration._copy_or_resume_temp(stage4104.db, plan, item, source, target, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    migration._verify_and_finalize_target(stage4104.db, plan, item, source, target, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    final_path = stage4104.paths["target"] / item.target_final_relative_path
    assert final_path.exists()
    monkeypatch.setattr(
        migration,
        "_copy_or_resume_temp",
        lambda *_args, **_kwargs: pytest.fail("cleanup-only retry must not recopy"),
    )

    migration._cleanup_retry_item(stage4104.db, plan, item, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert item.phase == "failed"
    assert item.cleanup_pending is False
    assert not final_path.exists()
    assert source_file.exists()


def test_permission_conjunction_is_revalidated_before_worker_mutation(stage4104):
    camera = add_camera(stage4104)
    segment, source_file = add_segment(stage4104, camera)
    plan = prepare_plan(stage4104)
    queued = queue_plan(stage4104, plan)
    stage4104.owner.role = "viewer"
    stage4104.db.commit()

    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    segment = stage4104.db.get(RecordingSegment, segment.id)
    assert plan.status == "blocked"
    assert plan.reason_code == "migration_permission_revoked"
    assert operation.status == "blocked"
    assert plan.retry_mode == operation.retry_mode == "after_permission_restore"
    assert operation.retry_allowed is True
    assert segment.archive_root_id == stage4104.roots["source"].id
    assert source_file.exists()

    recovery_admin = add_user(stage4104, username="no-cleanup-recovery-admin")
    public = migration.get_migration_operation(
        stage4104.db,
        actor=recovery_admin,
        operation_id=operation.id,
    )
    assert public["operation"]["capabilities"]["cleanup_takeover_allowed"] is False


def test_permission_revocation_at_source_cleanup_boundary_preserves_truth(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    segment, source_file = add_segment(stage4104, camera, content=b"authority-boundary")
    plan = prepare_plan(stage4104)
    queued = queue_plan(stage4104, plan, key="authority-boundary-apply")
    real_cleanup = migration._cleanup_source
    calls = {"count": 0}

    def revoke_before_cleanup(db, current_plan, item, source, handle):
        calls["count"] += 1
        actor = db.get(User, current_plan.actor_user_id)
        actor.role = "viewer"
        db.add(actor)
        db.commit()
        return real_cleanup(db, current_plan, item, source, handle)

    monkeypatch.setattr(migration, "_cleanup_source", revoke_before_cleanup)
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    segment = stage4104.db.get(RecordingSegment, segment.id)
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])

    assert calls["count"] == 1
    assert plan.status == "partial"
    assert plan.reason_code == "migration_permission_revoked"
    assert operation.status == "partial"
    assert plan.retry_mode == operation.retry_mode == "cleanup_only"
    assert operation.retry_allowed is True
    assert operation.next_action == "retry_cleanup"
    assert item.phase == "metadata_switched"
    assert item.cleanup_pending is True
    assert segment.archive_root_id == stage4104.roots["target"].id
    assert source_file.exists()
    assert (stage4104.paths["target"] / item.target_final_relative_path).exists()

    recovery_admin = add_user(stage4104, username="initial-permission-loss-recovery-admin")
    public = migration.get_migration_operation(
        stage4104.db,
        actor=recovery_admin,
        operation_id=operation.id,
    )
    assert public["operation"]["capabilities"]["cleanup_takeover_allowed"] is True
    takeover = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=recovery_admin,
        operation_id=operation.id,
        idempotency_key="initial-permission-loss-takeover",
    )
    assert takeover["operation"]["status"] == "queued"


def test_apply_service_rejects_actor_without_both_permissions(stage4104):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera)
    plan = prepare_plan(stage4104)
    stage4104.owner.role = "operator"
    stage4104.db.commit()
    with pytest.raises(ArchiveMigrationBlocked, match="migration_permission_required"):
        migration.queue_migration_apply(
            stage4104.db,
            actor=stage4104.owner,
            plan_id=plan.id,
            expected_hash=plan.canonical_hash,
            idempotency_key="permission-denied",
        )


def test_queued_cancel_is_terminal_without_filesystem_mutation(stage4104):
    camera = add_camera(stage4104)
    _segment, source_file = add_segment(stage4104, camera)
    plan = prepare_plan(stage4104)
    queued = queue_plan(stage4104, plan)
    result = migration.cancel_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=queued["operation"]["operation_id"],
    )
    stage4104.db.expire_all()
    assert result["operation"]["status"] == "cancelled"
    cancelled_plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    cancelled_item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    assert cancelled_plan.status == "cancelled"
    assert cancelled_plan.cancelled_count == cancelled_plan.item_count == 1
    assert cancelled_item.phase == "cancelled"
    assert cancelled_item.cleanup_pending is False
    assert source_file.exists()


def test_status_and_item_payloads_are_bounded_and_recurring_reads_are_clean(stage4104):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera)
    plan = prepare_plan(stage4104)
    stage4104.db.refresh(plan)
    before = (plan.status, plan.phase, plan.updated_at, plan.current_operation_id)
    first = migration.bounded_migration_summary(stage4104.db)
    second = migration.bounded_migration_summary(stage4104.db)
    public = migration.get_migration_plan(stage4104.db, actor=stage4104.owner, plan_id=plan.id)
    items = migration.list_migration_items(
        stage4104.db,
        actor=stage4104.owner,
        plan_id=plan.id,
        limit=100_000,
    )
    stage4104.db.refresh(plan)

    assert first == second
    assert (plan.status, plan.phase, plan.updated_at, plan.current_operation_id) == before
    assert len(items["items"]) <= migration.ITEM_PAGE_MAX
    assert "source_relative_path" not in items["items"][0]
    assert "source_snapshot_key" not in public
    assert str(stage4104.paths["source"]) not in str(public)


def test_reserved_internal_namespace_is_outside_archive_inventory_namespace(stage4104):
    assert MIGRATION_INTERNAL_NAMESPACE == ".km-vms-internal/migration"
    assert not MIGRATION_INTERNAL_NAMESPACE.startswith("kmvms/recordings/")
    internal = stage4104.paths["source"] / MIGRATION_INTERNAL_NAMESPACE / "plan" / "item" / "source-quarantine"
    internal.parent.mkdir(parents=True)
    internal.write_bytes(b"internal")
    camera = add_camera(stage4104)
    segment, _ = add_segment(stage4104, camera)
    plan = prepare_plan(stage4104)
    item_ids = {
        value
        for (value,) in stage4104.db.query(ArchiveMigrationItem.segment_id)
        .filter(ArchiveMigrationItem.plan_id == plan.id)
        .all()
    }
    assert item_ids == {segment.id}
    assert internal.exists()


def test_prepare_authority_and_building_cancel_are_revalidated(stage4104):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera)
    requested = migration.request_migration_plan(
        stage4104.db,
        actor=stage4104.owner,
        source_root_id=stage4104.roots["source"].id,
        target_root_id=stage4104.roots["target"].id,
        idempotency_key="prepare-authority",
    )
    stage4104.owner.role = "viewer"
    stage4104.db.commit()
    assert migration._prepare_one_plan() is True
    stage4104.db.expire_all()
    blocked = stage4104.db.get(ArchiveMigrationPlan, requested["plan_id"])
    assert blocked.status == "blocked"
    assert blocked.reason_code == "migration_prepare_permission_revoked"
    assert stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=blocked.id).count() == 0

    with pytest.raises(ArchiveMigrationBlocked, match="migration_prepare_permission_required"):
        migration.request_migration_plan(
            stage4104.db,
            actor=stage4104.db.get(User, stage4104.owner.id),
            source_root_id=stage4104.roots["source"].id,
            target_root_id=stage4104.roots["target"].id,
            idempotency_key="prepare-denied",
        )

    owner = stage4104.db.get(User, stage4104.owner.id)
    owner.role = "owner"
    stage4104.db.commit()
    cancelled = migration.request_migration_plan(
        stage4104.db,
        actor=owner,
        source_root_id=stage4104.roots["source"].id,
        target_root_id=stage4104.roots["target"].id,
        idempotency_key="building-cancel",
    )
    migration.cancel_migration_plan(stage4104.db, actor=owner, plan_id=cancelled["plan_id"])
    assert migration._prepare_one_plan() is False
    stage4104.db.expire_all()
    cancelled_plan = stage4104.db.get(ArchiveMigrationPlan, cancelled["plan_id"])
    assert cancelled_plan.status == "cancelled"
    assert stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=cancelled_plan.id).count() == 0


def test_manifest_and_operation_authority_tamper_block_before_mutation(stage4104):
    camera = add_camera(stage4104)
    segment, source_file = add_segment(stage4104, camera, content=b"tamper-proof")
    plan = prepare_plan(stage4104, key="manifest-tamper")
    queued = queue_plan(stage4104, plan, key="manifest-tamper-apply")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    item.source_size_bytes += 1
    stage4104.db.commit()
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    assert operation.status == "blocked"
    assert operation.reason_code == "migration_manifest_tampered"
    assert source_file.exists()
    assert stage4104.db.get(RecordingSegment, segment.id).archive_root_id == stage4104.roots["source"].id

    item.source_size_bytes -= 1
    stage4104.db.commit()
    second_plan = prepare_plan(stage4104, key="authority-tamper")
    second = queue_plan(stage4104, second_plan, key="authority-tamper-apply")
    second_operation = stage4104.db.get(StorageOperation, second["operation"]["operation_id"])
    second_operation.progress = {**dict(second_operation.progress or {}), "permission_contract": {"apply": []}}
    stage4104.db.commit()
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    second_operation = stage4104.db.get(StorageOperation, second_operation.id)
    assert second_operation.status == "blocked"
    assert second_operation.reason_code == "migration_authority_contract_invalid"
    assert source_file.exists()


def test_plan_expiry_hash_and_root_replacement_fail_closed(stage4104):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera)
    expired = prepare_plan(stage4104, key="expiry")
    expired.expires_at = datetime.utcnow() - timedelta(seconds=1)
    stage4104.db.commit()
    public = migration.get_migration_plan(stage4104.db, actor=stage4104.owner, plan_id=expired.id)
    assert public["status"] == "expired"
    assert public["reason_code"] == "migration_plan_expired"

    stale = prepare_plan(stage4104, key="stale-hash")
    with pytest.raises(ArchiveMigrationBlocked, match="migration_plan_stale_or_tampered"):
        migration.queue_migration_apply(
            stage4104.db,
            actor=stage4104.owner,
            plan_id=stale.id,
            expected_hash="0" * 64,
            idempotency_key="stale-hash-apply",
        )

    replacement = prepare_plan(stage4104, key="root-replacement")
    stage4104.roots["target"].physical_identity = "pv1:ffffffffffffffffffffffffffffffff"
    stage4104.db.commit()
    with pytest.raises(ArchiveMigrationBlocked, match="archive_root_identity_changed"):
        migration.queue_migration_apply(
            stage4104.db,
            actor=stage4104.owner,
            plan_id=replacement.id,
            expected_hash=replacement.canonical_hash,
            idempotency_key="root-replacement-apply",
        )


def test_changed_source_and_foreign_temp_are_preserved_without_false_adoption(stage4104):
    camera = add_camera(stage4104)
    segment, source_file = add_segment(stage4104, camera, name="changed-source.mkv", content=b"original")
    plan = prepare_plan(stage4104, key="changed-source")
    queued = queue_plan(stage4104, plan, key="changed-source-apply")
    source_file.write_bytes(b"changed-after-plan")
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    assert operation.status == "blocked"
    assert operation.reason_code == "migration_source_changed"
    assert stage4104.db.get(RecordingSegment, segment.id).archive_root_id == stage4104.roots["source"].id
    assert not (stage4104.paths["target"] / segment.relative_path).exists()
    retired = stage4104.db.get(RecordingSegment, segment.id)
    retired.status = "deleted"
    retired.deleted_at = datetime.utcnow()
    stage4104.db.commit()

    foreign_segment, foreign_source = add_segment(
        stage4104,
        camera,
        name="foreign-temp.mkv",
        content=b"foreign-temp-source",
    )
    foreign_plan = prepare_plan(stage4104, key="foreign-temp")
    foreign_item = (
        stage4104.db.query(ArchiveMigrationItem)
        .filter_by(plan_id=foreign_plan.id, segment_id=foreign_segment.id)
        .one()
    )
    foreign_temp = stage4104.paths["target"] / foreign_item.target_temp_relative_path
    foreign_temp.parent.mkdir(parents=True, exist_ok=True)
    foreign_temp.write_bytes(b"not-owned-by-operation")
    foreign_queue = queue_plan(stage4104, foreign_plan, key="foreign-temp-apply")
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    foreign_operation = stage4104.db.get(StorageOperation, foreign_queue["operation"]["operation_id"])
    assert foreign_operation.status == "blocked"
    assert foreign_operation.reason_code == "migration_temp_pending_object_ambiguous"
    assert foreign_temp.read_bytes() == b"not-owned-by-operation"
    assert foreign_source.exists()


def test_manifest_aware_conflicts_allow_unrelated_items_on_same_root(stage4104):
    planned_camera = add_camera(stage4104, name="Planned Camera")
    planned_segment, _ = add_segment(stage4104, planned_camera, name="planned.mkv")
    plan = prepare_plan(stage4104, key="manifest-conflicts")
    other_camera = add_camera(stage4104, name="Other Camera")
    other_segment, _ = add_segment(stage4104, other_camera, name="other.mkv")
    migration_scope = {
        "global": False,
        "physical_volume_ids": [stage4104.roots["source"].physical_identity],
        "root_ids": [stage4104.roots["source"].id, stage4104.roots["target"].id],
        "camera_ids": [],
        "segment_ids": [],
    }
    domain_ref = f"migration-plan:{plan.id}"

    def conflicts(operation_type, *, camera_id=None, segment_id=None):
        other_scope = {
            "global": False,
            "physical_volume_ids": [stage4104.roots["source"].physical_identity],
            "root_ids": [stage4104.roots["source"].id],
            "camera_ids": [camera_id] if camera_id is not None else [],
            "segment_ids": [segment_id] if segment_id is not None else [],
        }
        return operations_conflict_in_db(
            stage4104.db,
            "archive_migration_apply",
            migration_scope,
            domain_ref,
            operation_type,
            other_scope,
            None,
        )

    assert conflicts("manual_single_delete", segment_id=planned_segment.id) is True
    assert conflicts("manual_single_delete", segment_id=other_segment.id) is False
    assert conflicts("retention_auto_run", camera_id=planned_camera.id) is True
    assert conflicts("retention_auto_run", camera_id=other_camera.id) is False
    assert conflicts("archive_root_delete") is True
    assert conflicts("integrity_scan") is False


def test_capacity_loss_before_mutation_blocks_and_enospc_after_progress_is_partial(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    first, first_source = add_segment(stage4104, camera, name="first.mkv", content=b"first")
    plan = prepare_plan(stage4104, key="capacity-before")
    queued = queue_plan(stage4104, plan, key="capacity-before-apply")
    real_snapshot = migration.assert_root_snapshot

    def no_free_space(root, expected, *, require_write):
        result = real_snapshot(root, expected, require_write=require_write)
        if root.id == stage4104.roots["target"].id:
            result = {**result, "free_bytes": 0}
        return result

    monkeypatch.setattr(migration, "assert_root_snapshot", no_free_space)
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    assert operation.status == "blocked"
    assert operation.reason_code == "migration_insufficient_target_space"
    assert first_source.exists()
    assert stage4104.db.get(RecordingSegment, first.id).archive_root_id == stage4104.roots["source"].id

    monkeypatch.setattr(migration, "assert_root_snapshot", real_snapshot)
    first_row = stage4104.db.get(RecordingSegment, first.id)
    first_row.status = "deleted"
    first_row.deleted_at = datetime.utcnow()
    stage4104.db.commit()
    second, second_source = add_segment(stage4104, camera, name="second.mkv", content=b"second")
    third, third_source = add_segment(stage4104, camera, name="third.mkv", content=b"third")
    second_plan = prepare_plan(stage4104, key="enospc-after-progress")
    second_queue = queue_plan(stage4104, second_plan, key="enospc-after-progress-apply")
    real_write = migration.os.write
    writes = {"count": 0}

    def fail_second_write(descriptor, value):
        writes["count"] += 1
        if writes["count"] == 2:
            raise OSError(errno.ENOSPC, "simulated target full")
        return real_write(descriptor, value)

    monkeypatch.setattr(migration.os, "write", fail_second_write)
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    second_operation = stage4104.db.get(StorageOperation, second_queue["operation"]["operation_id"])
    second_plan = stage4104.db.get(ArchiveMigrationPlan, second_plan.id)
    assert second_operation.status == "partial"
    assert second_plan.completed_count == 1
    assert second_plan.cleanup_pending is True
    moved = stage4104.db.get(RecordingSegment, second.id)
    pending = stage4104.db.get(RecordingSegment, third.id)
    assert moved.archive_root_id == stage4104.roots["target"].id
    assert pending.archive_root_id == stage4104.roots["source"].id
    assert not second_source.exists()
    assert third_source.exists()


def test_cancel_after_interrupted_copy_finishes_current_item_and_cancels_remainder(stage4104):
    camera = add_camera(stage4104)
    first, first_source = add_segment(stage4104, camera, name="cancel-first.mkv", content=b"first-recording")
    second, second_source = add_segment(stage4104, camera, name="cancel-second.mkv", content=b"second-recording")
    plan = prepare_plan(stage4104, key="cancel-interrupted-copy")
    queued = queue_plan(stage4104, plan, key="cancel-interrupted-copy-apply")
    items = (
        stage4104.db.query(ArchiveMigrationItem)
        .filter_by(plan_id=plan.id)
        .order_by(ArchiveMigrationItem.item_index.asc())
        .all()
    )
    current = items[0]
    temp_path = stage4104.paths["target"] / current.target_temp_relative_path
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(b"fir")
    temp_stat = temp_path.stat()
    current.phase = "copying"
    current.target_device = int(temp_stat.st_dev)
    current.target_inode = int(temp_stat.st_ino)
    current.transferred_bytes = int(temp_stat.st_size)
    current.operation_id = queued["operation"]["operation_id"]
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    operation.status = "running"
    operation.fencing_token = 1
    operation.owner_token_hash = "0" * 64
    operation.owner_instance_id = "interrupted-worker"
    operation.started_at = datetime.utcnow()
    operation.heartbeat_at = datetime.utcnow() - timedelta(minutes=10)
    operation.lease_expires_at = datetime.utcnow() - timedelta(minutes=5)
    plan.status = "running"
    plan.phase = "running"
    stage4104.db.add_all([current, operation, plan])
    stage4104.db.commit()

    migration.cancel_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=operation.id,
    )
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    operation = stage4104.db.get(StorageOperation, operation.id)
    items = (
        stage4104.db.query(ArchiveMigrationItem)
        .filter_by(plan_id=plan.id)
        .order_by(ArchiveMigrationItem.item_index.asc())
        .all()
    )
    assert plan.status == operation.status == "cancelled"
    assert plan.completed_count == 1
    assert plan.cancelled_count == 1
    assert [item.phase for item in items] == ["completed", "cancelled"]
    assert plan.cleanup_pending is False
    assert stage4104.db.get(RecordingSegment, first.id).archive_root_id == stage4104.roots["target"].id
    assert stage4104.db.get(RecordingSegment, second.id).archive_root_id == stage4104.roots["source"].id
    assert not first_source.exists()
    assert second_source.exists()
    assert not any(path.is_file() for path in (stage4104.paths["target"] / ".km-vms-internal").rglob("*"))


def test_domain_terminal_recovery_and_missing_terminal_audit_are_idempotent(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"terminal-recovery")
    plan = prepare_plan(stage4104, key="terminal-recovery")
    queued = queue_plan(stage4104, plan, key="terminal-recovery-apply")
    real_stage_terminal = migration.stage_operation_terminal
    calls = {"count": 0}

    def crash_before_outer_terminal(*args, **kwargs):
        calls["count"] += 1
        raise OSError("simulated crash before outer terminal persistence")

    monkeypatch.setattr(migration, "stage_operation_terminal", crash_before_outer_terminal)
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    assert plan.status not in migration.PLAN_TERMINAL_STATUSES
    assert operation.status in {"running", "cancel_requested"}

    monkeypatch.setattr(migration, "stage_operation_terminal", real_stage_terminal)
    operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    stage4104.db.commit()
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    operation = stage4104.db.get(StorageOperation, operation.id)
    assert plan.status == "completed"
    assert operation.status == "completed"

    event_type = "archive_migration.operation_completed"
    stage4104.db.query(AuditEvent).filter(
        AuditEvent.event_type == event_type,
        AuditEvent.target_type == "archive_migration_plan",
        AuditEvent.target_id == plan.id,
    ).delete(synchronize_session=False)
    stage4104.db.commit()
    assert migration._recover_terminal_audit_once() is True
    assert migration._recover_terminal_audit_once() is False
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_type == "archive_migration_plan",
            AuditEvent.target_id == plan.id,
        )
        .count()
        == 1
    )


def test_temp_create_intent_is_durable_before_exclusive_create(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"intent-before-create")
    plan = prepare_plan(stage4104, key="intent-before-create")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    source, target, _ = migration._runtime_item_guard(stage4104.db, plan, item)
    real_create = migration.create_relative_exclusive
    observed = {"durable": False}

    def assert_durable_intent(root_fd, relative_path, *, mode):
        with stage4104.Session() as verify_db:
            persisted = verify_db.get(ArchiveMigrationItem, item.id)
            observed["durable"] = bool(
                persisted
                and persisted.phase == "target_temp_create_pending"
                and persisted.operation_id == "intent-before-create-op"
                and persisted.operation_fencing_token == 1
                and persisted.target_device is None
                and persisted.target_inode is None
            )
        return real_create(root_fd, relative_path, mode=mode)

    monkeypatch.setattr(migration, "create_relative_exclusive", assert_durable_intent)
    monkeypatch.setattr(migration, "heartbeat_operation", lambda *_args, **_kwargs: None)
    migration._copy_or_resume_temp(
        stage4104.db,
        plan,
        item,
        source,
        target,
        fake_handle(stage4104, plan, "intent-before-create-op"),
    )
    stage4104.db.expire_all()
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert observed["durable"] is True
    assert item.phase == "target_temp_written"
    assert item.target_device is not None and item.target_inode is not None


def test_stale_generation_cannot_create_or_persist_temp_after_takeover(stage4104):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"fenced-temp")
    plan = prepare_plan(stage4104, key="fenced-temp")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    old_handle = fake_handle(stage4104, plan, "fenced-temp-op")
    migration._stage_temp_create_intent(stage4104.db, plan, item, old_handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    source, target, _segment = migration._runtime_item_guard(stage4104.db, plan, item)
    temp_path = stage4104.paths["target"] / item.target_temp_relative_path
    new_handle = take_over_handle(stage4104, old_handle)

    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._copy_or_resume_temp(
            stage4104.db,
            plan,
            item,
            source,
            target,
            old_handle,
        )
    stage4104.db.rollback()
    stage4104.db.expire_all()
    assert not temp_path.exists()
    assert stage4104.db.get(ArchiveMigrationItem, item.id).phase == "target_temp_create_pending"

    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(b"")
    os.chmod(temp_path, migration.TEMP_CREATE_MODE)
    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._copy_or_resume_temp(
            stage4104.db,
            plan,
            stage4104.db.get(ArchiveMigrationItem, item.id),
            source,
            target,
            old_handle,
        )
    stage4104.db.rollback()
    stage4104.db.expire_all()
    pending = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert pending.phase == "target_temp_create_pending"
    assert pending.target_device is None and pending.target_inode is None

    migration._copy_or_resume_temp(
        stage4104.db,
        plan,
        pending,
        source,
        target,
        new_handle,
    )
    stage4104.db.expire_all()
    recovered = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert recovered.phase == "target_temp_written"
    assert recovered.operation_fencing_token == new_handle.fencing_token


def test_item_fence_adoption_rejects_unrelated_operation_lineage(stage4104):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"unrelated-lineage")
    plan = prepare_plan(stage4104, key="unrelated-lineage")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, "lineage-current-op")
    item.operation_id = "lineage-unrelated-op"
    item.operation_fencing_token = 1
    stage4104.db.add(item)
    stage4104.db.commit()

    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._stage_temp_create_intent(stage4104.db, plan, item, handle)
    stage4104.db.rollback()
    stage4104.db.expire_all()
    persisted = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert persisted.phase == "planned"
    assert persisted.operation_id == "lineage-unrelated-op"


def test_item_fence_is_scoped_to_bound_parent_and_adopted_durably(stage4104):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"parent-fence-scope")
    plan = prepare_plan(stage4104, key="parent-fence-scope")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, "parent-fence-scope-op")
    parent, _current, item = bind_cleanup_item_to_terminal_parent(
        stage4104,
        plan,
        item,
        handle,
        parent_fence=7,
    )

    operation, _locked_plan, adopted = migration._lock_owned_migration_item(
        stage4104.db,
        plan,
        item,
        handle,
    )
    assert parent.fencing_token == 7
    assert operation.fencing_token == handle.fencing_token == 1
    assert adopted.operation_id == handle.operation_id
    assert adopted.operation_fencing_token == handle.fencing_token
    with stage4104.Session() as verify_db:
        persisted = verify_db.get(ArchiveMigrationItem, item.id)
        assert persisted.operation_id == handle.operation_id
        assert persisted.operation_fencing_token == handle.fencing_token


def test_same_operation_future_item_fence_is_rejected(stage4104):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"future-item-fence")
    plan = prepare_plan(stage4104, key="future-item-fence")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, "future-item-fence-op")
    item.operation_id = handle.operation_id
    item.operation_fencing_token = handle.fencing_token + 1
    stage4104.db.add(item)
    stage4104.db.commit()

    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._lock_owned_migration_item(stage4104.db, plan, item, handle)
    stage4104.db.rollback()


@pytest.mark.parametrize(
    "tamper",
    ("bound_future_fence", "scope", "fingerprint", "snapshot", "missing", "cycle", "depth"),
)
def test_ancestor_adoption_rejects_invalid_db_lineage(stage4104, tamper):
    camera = add_camera(stage4104, name=f"Invalid lineage {tamper}")
    add_segment(stage4104, camera, content=f"invalid-{tamper}".encode())
    plan = prepare_plan(stage4104, key=f"invalid-lineage-{tamper}")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, f"invalid-lineage-{tamper}-op")
    parent, current, item = bind_cleanup_item_to_terminal_parent(stage4104, plan, item, handle)

    if tamper == "bound_future_fence":
        item.operation_fencing_token = int(parent.fencing_token) + 1
    elif tamper == "scope":
        parent.scope = {"root_ids": [str(plan.source_root_id), str(stage4104.roots["third"].id)]}
    elif tamper == "fingerprint":
        parent.request_fingerprint = "0" * 64
    elif tamper == "snapshot":
        snapshot = dict(current.parent_snapshot or {})
        snapshot["domain_ref"] = "migration-plan:tampered"
        current.parent_snapshot = snapshot
    elif tamper == "missing":
        current.parent_operation_id = "missing-lineage-operation"
    elif tamper == "cycle":
        parent.parent_operation_id = str(parent.id)
        item.operation_id = "unreachable-bound-operation"
        item.operation_fencing_token = 1
    elif tamper == "depth":
        parent.retry_depth = MAX_RETRY_DEPTH
        current.retry_depth = MAX_RETRY_DEPTH + 1
        snapshot = dict(current.parent_snapshot or {})
        snapshot["retry_depth"] = MAX_RETRY_DEPTH
        current.parent_snapshot = snapshot
    stage4104.db.add_all((parent, current, item))
    stage4104.db.commit()

    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._lock_owned_migration_item(stage4104.db, plan, item, handle)
    stage4104.db.rollback()
    stage4104.db.expire_all()
    persisted = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert persisted.operation_id != handle.operation_id
    assert persisted.cleanup_pending is True


def test_stale_generation_is_fenced_at_finalize_metadata_and_source_cleanup(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    segment, source_path = add_segment(stage4104, camera, content=b"fenced-boundaries")
    plan = prepare_plan(stage4104, key="fenced-boundaries")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    first = fake_handle(stage4104, plan, "fenced-boundaries-op")
    source, target, _segment = migration._runtime_item_guard(stage4104.db, plan, item)
    migration._copy_or_resume_temp(stage4104.db, plan, item, source, target, first)
    stage4104.db.expire_all()
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    item.target_sha256 = item.source_sha256
    item.phase = "target_verified"
    stage4104.db.add(item)
    stage4104.db.commit()
    temp_path = stage4104.paths["target"] / item.target_temp_relative_path
    final_path = stage4104.paths["target"] / item.target_final_relative_path

    second = take_over_handle(stage4104, first)
    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._verify_and_finalize_target(stage4104.db, plan, item, source, target, first)
    stage4104.db.rollback()
    assert temp_path.exists() and not final_path.exists()
    assert stage4104.db.get(ArchiveMigrationItem, item.id).phase == "target_verified"

    migration._verify_and_finalize_target(stage4104.db, plan, item, source, target, second)
    stage4104.db.expire_all()
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert item.phase == "target_finalized"
    third = take_over_handle(stage4104, second)
    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._switch_metadata(stage4104.db, plan, item, second)
    stage4104.db.rollback()
    stage4104.db.expire_all()
    assert stage4104.db.get(RecordingSegment, segment.id).archive_root_id == stage4104.roots["source"].id
    assert stage4104.db.get(ArchiveMigrationItem, item.id).phase == "target_finalized"

    migration._switch_metadata(
        stage4104.db,
        plan,
        stage4104.db.get(ArchiveMigrationItem, item.id),
        third,
    )
    stage4104.db.expire_all()
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert item.phase == "metadata_switched"
    fourth = take_over_handle(stage4104, third)
    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._cleanup_source(stage4104.db, plan, item, source, third)
    stage4104.db.rollback()
    assert source_path.exists()
    assert stage4104.db.get(ArchiveMigrationItem, item.id).phase == "metadata_switched"

    real_unlink = migration.unlink_relative

    def stop_before_unlink(*_args, **_kwargs):
        raise OSError("stop before fenced unlink")

    monkeypatch.setattr(migration, "unlink_relative", stop_before_unlink)
    with pytest.raises(OSError, match="stop before fenced unlink"):
        migration._cleanup_source(
            stage4104.db,
            plan,
            stage4104.db.get(ArchiveMigrationItem, item.id),
            source,
            fourth,
        )
    stage4104.db.rollback()
    stage4104.db.expire_all()
    pending_delete = stage4104.db.get(ArchiveMigrationItem, item.id)
    quarantine_path = stage4104.paths["source"] / pending_delete.source_quarantine_relative_path
    assert pending_delete.phase == "source_delete_committing"
    assert not source_path.exists() and quarantine_path.exists()

    fifth = take_over_handle(stage4104, fourth)
    with pytest.raises(migration.StorageOperationLeaseLost):
        migration._cleanup_source(stage4104.db, plan, pending_delete, source, fourth)
    stage4104.db.rollback()
    assert quarantine_path.exists()
    assert stage4104.db.get(ArchiveMigrationItem, item.id).phase == "source_delete_committing"

    monkeypatch.setattr(migration, "unlink_relative", real_unlink)
    migration._cleanup_source(
        stage4104.db,
        plan,
        stage4104.db.get(ArchiveMigrationItem, item.id),
        source,
        fifth,
    )
    stage4104.db.expire_all()
    completed = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert completed.phase == "completed"
    assert completed.operation_fencing_token == fifth.fencing_token
    assert not quarantine_path.exists()


def test_pending_missing_and_exact_zero_temp_recover_without_guessing(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"pending-recovery")
    plan = prepare_plan(stage4104, key="pending-recovery")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, "pending-recovery-op")
    migration._stage_temp_create_intent(stage4104.db, plan, item, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    temp_path = stage4104.paths["target"] / item.target_temp_relative_path
    assert not temp_path.exists()
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(b"")
    os.chmod(temp_path, migration.TEMP_CREATE_MODE)
    expected_inode = temp_path.stat().st_ino
    captured = {"fd": None, "closed": False}
    real_open = migration._open_pending_temp_for_recovery
    real_close = migration.os.close

    def capture_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        captured["fd"] = descriptor
        return descriptor

    def capture_close(descriptor):
        if descriptor == captured["fd"]:
            captured["closed"] = True
        return real_close(descriptor)

    monkeypatch.setattr(migration, "_open_pending_temp_for_recovery", capture_open)
    monkeypatch.setattr(migration.os, "close", capture_close)
    monkeypatch.setattr(migration, "heartbeat_operation", lambda *_args, **_kwargs: None)
    source, target, _ = migration._runtime_item_guard(stage4104.db, plan, item)
    migration._copy_or_resume_temp(stage4104.db, plan, item, source, target, handle)
    stage4104.db.expire_all()
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert captured["closed"] is True
    assert item.target_inode == expected_inode
    assert item.phase == "target_temp_written"


def test_post_create_persistence_failure_closes_fd_and_restart_adopts_exact_temp(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"post-create-commit")
    plan = prepare_plan(stage4104, key="post-create-commit")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, "post-create-commit-op")
    migration._stage_temp_create_intent(stage4104.db, plan, item, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    source, target, _ = migration._runtime_item_guard(stage4104.db, plan, item)
    temp_path = stage4104.paths["target"] / item.target_temp_relative_path
    real_create = migration.create_relative_exclusive
    real_close = migration.os.close
    real_commit = stage4104.db.commit
    captured = {"fd": None, "closed": False, "failed": False}

    def capture_create(*args, **kwargs):
        descriptor = real_create(*args, **kwargs)
        captured["fd"] = descriptor
        return descriptor

    def capture_close(descriptor):
        if descriptor == captured["fd"]:
            captured["closed"] = True
        return real_close(descriptor)

    def fail_provenance_commit():
        if not captured["failed"] and item.phase == "copying":
            captured["failed"] = True
            raise RuntimeError("private database detail must stay internal")
        return real_commit()

    monkeypatch.setattr(migration, "create_relative_exclusive", capture_create)
    monkeypatch.setattr(migration.os, "close", capture_close)
    monkeypatch.setattr(stage4104.db, "commit", fail_provenance_commit)
    monkeypatch.setattr(migration, "heartbeat_operation", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="private database detail"):
        migration._copy_or_resume_temp(stage4104.db, plan, item, source, target, handle)
    assert captured["closed"] is True
    assert temp_path.exists() and temp_path.stat().st_size == 0

    monkeypatch.setattr(stage4104.db, "commit", real_commit)
    monkeypatch.setattr(migration.os, "close", real_close)
    stage4104.db.expire_all()
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert item.phase == "target_temp_create_pending"
    source, target, _ = migration._runtime_item_guard(stage4104.db, plan, item)
    migration._copy_or_resume_temp(stage4104.db, plan, item, source, target, handle)
    stage4104.db.expire_all()
    assert stage4104.db.get(ArchiveMigrationItem, item.id).phase == "target_temp_written"


@pytest.mark.parametrize("object_kind", ["nonzero", "wrong_mode", "symlink", "hardlink", "directory"])
def test_pending_ambiguous_objects_are_never_adopted_or_deleted(stage4104, monkeypatch, object_kind):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"ambiguous-pending")
    plan = prepare_plan(stage4104, key=f"ambiguous-{object_kind}")
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    handle = fake_handle(stage4104, plan, f"ambiguous-{object_kind}-op")
    migration._stage_temp_create_intent(stage4104.db, plan, item, handle)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    temp_path = stage4104.paths["target"] / item.target_temp_relative_path
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    foreign = stage4104.tmp_path / f"foreign-{object_kind}"
    if object_kind == "nonzero":
        temp_path.write_bytes(b"foreign")
        os.chmod(temp_path, migration.TEMP_CREATE_MODE)
    elif object_kind == "wrong_mode":
        temp_path.write_bytes(b"")
        os.chmod(temp_path, 0o640)
    elif object_kind == "symlink":
        foreign.write_bytes(b"foreign-link-target")
        temp_path.symlink_to(foreign)
    elif object_kind == "hardlink":
        foreign.write_bytes(b"")
        os.chmod(foreign, migration.TEMP_CREATE_MODE)
        os.link(foreign, temp_path)
    else:
        temp_path.mkdir()
    monkeypatch.setattr(migration, "heartbeat_operation", lambda *_args, **_kwargs: None)
    source, target, _ = migration._runtime_item_guard(stage4104.db, plan, item)

    with pytest.raises(ArchiveMigrationPartial, match="migration_temp_pending_object_ambiguous"):
        migration._copy_or_resume_temp(stage4104.db, plan, item, source, target, handle)
    assert os.path.lexists(temp_path)
    if foreign.exists():
        assert foreign.read_bytes() in {b"", b"foreign-link-target"}


def test_admin_initial_apply_is_rejected_but_exact_cleanup_takeover_is_idempotent(stage4104, monkeypatch):
    camera = add_camera(stage4104)
    add_segment(stage4104, camera, content=b"takeover")
    plan = prepare_plan(stage4104, key="takeover")
    admin = add_user(stage4104, username="stage4104-admin")
    before_operations = stage4104.db.query(StorageOperation).count()
    with pytest.raises(ArchiveMigrationBlocked, match="migration_plan_actor_mismatch"):
        migration.queue_migration_apply(
            stage4104.db,
            actor=admin,
            plan_id=plan.id,
            expected_hash=plan.canonical_hash,
            idempotency_key="foreign-initial-apply",
        )
    assert stage4104.db.query(StorageOperation).count() == before_operations

    queued = queue_plan(stage4104, plan, key="takeover-owner-apply")
    real_cleanup = migration._cleanup_source
    monkeypatch.setattr(
        migration,
        "_cleanup_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ArchiveMigrationPartial("migration_source_cleanup_incomplete", retry_mode="cleanup_only")
        ),
    )
    assert migration._run_one_operation() is True
    monkeypatch.setattr(migration, "_cleanup_source", real_cleanup)
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    parent = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    source_file = stage4104.paths["source"] / (
        stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one().source_relative_path
    )
    stage4104.owner.is_active = False
    stage4104.db.commit()

    first = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=admin,
        operation_id=parent.id,
        idempotency_key="admin-cleanup-takeover",
    )
    second = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=admin,
        operation_id=parent.id,
        idempotency_key="admin-cleanup-takeover",
    )
    child_id = first["operation"]["operation_id"]
    assert second["operation"]["operation_id"] == child_id
    assert second["replayed"] is True
    child = stage4104.db.get(StorageOperation, child_id)
    snapshot = dict(child.parent_snapshot or {})
    assert child.parent_operation_id == parent.id
    assert child.actor_user_id == admin.id
    assert snapshot["original_actor_key"] == plan.actor_key
    assert snapshot["cross_actor_recovery"] == "migration_cleanup_takeover"
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.cleanup_takeover_queued",
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == child_id,
        )
        .count()
        == 1
    )

    monkeypatch.setattr(migration, "_copy_or_resume_temp", lambda *_a, **_k: pytest.fail("takeover recopy"))
    monkeypatch.setattr(migration, "_verify_and_finalize_target", lambda *_a, **_k: pytest.fail("takeover finalize"))
    monkeypatch.setattr(migration, "_switch_metadata", lambda *_a, **_k: pytest.fail("takeover metadata switch"))
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    child = stage4104.db.get(StorageOperation, child_id)
    assert plan.status == child.status == "completed"
    assert not source_file.exists()
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.cleanup_takeover_outcome",
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == child_id,
        )
        .count()
        == 1
    )


def test_takeover_requires_original_actor_loss_and_current_admin_permissions(stage4104, monkeypatch):
    plan, parent, item, _segment, source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="takeover-permissions",
    )
    admin = add_user(stage4104, username="stage4104-recovery-admin")
    with pytest.raises(ArchiveMigrationBlocked, match="migration_cleanup_takeover_not_allowed"):
        migration.takeover_migration_cleanup(
            stage4104.db,
            actor=admin,
            operation_id=parent.id,
            idempotency_key="takeover-while-owner-active",
        )

    stage4104.owner.is_active = False
    stage4104.db.commit()
    real_permission = migration.user_has_permission

    def missing_delete_permission(role, permission):
        if role == "admin" and permission == "delete_recordings":
            return False
        return real_permission(role, permission)

    monkeypatch.setattr(migration, "user_has_permission", missing_delete_permission)
    with pytest.raises(ArchiveMigrationBlocked, match="migration_cleanup_takeover_not_allowed"):
        migration.takeover_migration_cleanup(
            stage4104.db,
            actor=admin,
            operation_id=parent.id,
            idempotency_key="takeover-underprivileged",
        )
    assert source_file.exists()
    assert stage4104.db.get(ArchiveMigrationItem, item.id).cleanup_pending is True


def test_recovery_admin_permission_revocation_stops_before_cleanup_mutation(stage4104, monkeypatch):
    plan, parent, item, _segment, source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="takeover-revoked",
    )
    admin = add_user(stage4104, username="stage4104-revoked-admin")
    stage4104.owner.is_active = False
    stage4104.db.commit()
    queued = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=admin,
        operation_id=parent.id,
        idempotency_key="takeover-revoked-child",
    )
    admin.role = "viewer"
    stage4104.db.commit()
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    child = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    assert child.status == plan.status == "blocked"
    assert child.reason_code == plan.reason_code == "migration_recovery_permission_revoked"
    assert child.retry_allowed is True
    assert child.retry_mode == plan.retry_mode == "cleanup_only"
    assert source_file.exists()
    assert stage4104.db.get(ArchiveMigrationItem, item.id).cleanup_pending is True


def test_same_recovery_admin_continues_ancestor_bound_cleanup_after_permission_restore(stage4104, monkeypatch):
    plan, parent, blocked_child, item, recovery_admin, source_file = create_permission_revoked_cleanup_child(
        stage4104,
        monkeypatch,
        key="same-admin-lineage",
    )
    recovery_admin.role = "admin"
    stage4104.owner.is_active = True
    stage4104.db.commit()
    public = migration.get_migration_operation(
        stage4104.db,
        actor=recovery_admin,
        operation_id=blocked_child.id,
    )
    assert public["operation"]["capabilities"]["cleanup_takeover_allowed"] is True
    queued = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=recovery_admin,
        operation_id=blocked_child.id,
        idempotency_key="same-admin-second-recovery",
    )
    child_id = queued["operation"]["operation_id"]
    child = stage4104.db.get(StorageOperation, child_id)
    assert child.parent_operation_id == blocked_child.id
    assert child.retry_depth == 2
    assert dict(child.parent_snapshot or {})["original_actor_key"] == plan.actor_key

    monkeypatch.setattr(migration, "_copy_or_resume_temp", lambda *_a, **_k: pytest.fail("recovery recopy"))
    monkeypatch.setattr(migration, "_verify_and_finalize_target", lambda *_a, **_k: pytest.fail("recovery finalize"))
    monkeypatch.setattr(migration, "_switch_metadata", lambda *_a, **_k: pytest.fail("recovery metadata switch"))
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    child = stage4104.db.get(StorageOperation, child_id)
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert child.status == plan.status == "completed"
    assert item.phase == "completed"
    assert item.operation_id == child_id
    assert item.operation_fencing_token == child.fencing_token == 1
    assert parent.fencing_token == 7
    assert not source_file.exists()

    replay = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=recovery_admin,
        operation_id=blocked_child.id,
        idempotency_key="same-admin-second-recovery",
    )
    assert replay["replayed"] is True
    assert replay["operation"]["operation_id"] == child_id
    for event_type in (
        "archive_migration.cleanup_takeover_queued",
        "archive_migration.cleanup_takeover_outcome",
    ):
        assert (
            stage4104.db.query(AuditEvent)
            .filter(
                AuditEvent.event_type == event_type,
                AuditEvent.target_type == "storage_operation",
                AuditEvent.target_id == child_id,
            )
            .count()
            == 1
        )


def test_different_authorized_admin_continues_exact_cleanup_after_permission_loss(stage4104, monkeypatch):
    plan, _parent, blocked_child, item, first_admin, source_file = create_permission_revoked_cleanup_child(
        stage4104,
        monkeypatch,
        key="different-admin-lineage",
    )
    second_admin = add_user(stage4104, username="different-admin-lineage-second-admin")
    assert first_admin.role == "viewer"
    stage4104.owner.is_active = True
    stage4104.db.commit()
    public = migration.get_migration_operation(
        stage4104.db,
        actor=second_admin,
        operation_id=blocked_child.id,
    )
    assert public["operation"]["capabilities"]["cleanup_takeover_allowed"] is True
    queued = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=second_admin,
        operation_id=blocked_child.id,
        idempotency_key="different-admin-second-recovery",
    )
    child_id = queued["operation"]["operation_id"]
    child = stage4104.db.get(StorageOperation, child_id)
    snapshot = dict(child.parent_snapshot or {})
    assert child.parent_operation_id == blocked_child.id
    assert child.actor_user_id == second_admin.id
    assert snapshot["original_actor_key"] == plan.actor_key
    assert snapshot["cross_actor_recovery"] == "migration_cleanup_takeover"

    monkeypatch.setattr(migration, "_copy_or_resume_temp", lambda *_a, **_k: pytest.fail("recovery recopy"))
    monkeypatch.setattr(migration, "_verify_and_finalize_target", lambda *_a, **_k: pytest.fail("recovery finalize"))
    monkeypatch.setattr(migration, "_switch_metadata", lambda *_a, **_k: pytest.fail("recovery metadata switch"))
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    child = stage4104.db.get(StorageOperation, child_id)
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert child.status == plan.status == "completed"
    assert item.phase == "completed"
    assert item.operation_id == child_id
    assert not source_file.exists()
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.cleanup_takeover_outcome",
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == child_id,
        )
        .count()
        == 1
    )


def test_restored_original_owner_continues_cleanup_after_recovery_admin_child(stage4104, monkeypatch):
    plan, _parent, blocked_child, item, _recovery_admin, source_file = create_permission_revoked_cleanup_child(
        stage4104,
        monkeypatch,
        key="restored-original-owner",
    )
    stage4104.owner.is_active = True
    stage4104.db.commit()

    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=blocked_child.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is True
    assert public["operation"]["capabilities"]["cleanup_takeover_allowed"] is False

    queued = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=blocked_child.id,
        idempotency_key="restored-original-owner-continuation",
    )
    child_id = queued["operation"]["operation_id"]
    child = stage4104.db.get(StorageOperation, child_id)
    snapshot = dict(child.parent_snapshot or {})
    assert child.parent_operation_id == blocked_child.id
    assert child.actor_user_id == stage4104.owner.id
    assert snapshot["original_actor_key"] == plan.actor_key
    assert snapshot["cross_actor_recovery"] == "migration_cleanup_takeover"

    monkeypatch.setattr(migration, "_copy_or_resume_temp", lambda *_a, **_k: pytest.fail("owner recopy"))
    monkeypatch.setattr(migration, "_verify_and_finalize_target", lambda *_a, **_k: pytest.fail("owner finalize"))
    monkeypatch.setattr(migration, "_switch_metadata", lambda *_a, **_k: pytest.fail("owner metadata switch"))
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    child = stage4104.db.get(StorageOperation, child_id)
    plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert child.status == plan.status == "completed"
    assert item.phase == "completed"
    assert item.operation_id == child_id
    assert not source_file.exists()

    replay = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=blocked_child.id,
        idempotency_key="restored-original-owner-continuation",
    )
    assert replay["replayed"] is True
    assert replay["operation"]["operation_id"] == child_id
    outcomes = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.cleanup_retry_outcome",
            AuditEvent.target_type == "archive_migration_plan",
            AuditEvent.target_id == plan.id,
        )
        .all()
    )
    assert len(outcomes) == 2
    outcome_metadata = [dict(event.event_metadata or {}) for event in outcomes]
    assert {str(value.get("operation_id") or "") for value in outcome_metadata} == {
        str(blocked_child.id),
        str(child_id),
    }
    outcome_fingerprints = {
        str(value.get("migration_transition_fingerprint") or "") for value in outcome_metadata
    }
    assert "" not in outcome_fingerprints
    assert "***" not in outcome_fingerprints
    assert len(outcome_fingerprints) == 2


def test_throughput_sampler_and_progress_are_bounded_and_phase_truthful(stage4104):
    clock_value = {"now": 100.0}
    sampler = migration._CopyThroughputSampler(clock=lambda: clock_value["now"])
    sampler.observe(0)
    assert sampler.values(remaining_bytes=10_000_000) == (None, None)
    clock_value["now"] += 0.6
    sampler.observe(1_048_576)
    clock_value["now"] += 0.6
    sampler.observe(2_097_152)
    speed, eta = sampler.values(remaining_bytes=4_194_304)
    assert speed is not None and 0 < speed <= migration.THROUGHPUT_MAX_BYTES_PER_SECOND
    assert eta is not None and 0 <= eta <= migration.THROUGHPUT_MAX_ETA_SECONDS
    clock_value["now"] += migration.THROUGHPUT_STALE_SECONDS + 0.1
    assert sampler.values(remaining_bytes=4_194_304) == (None, None)

    camera = add_camera(stage4104)
    add_segment(stage4104, camera)
    plan = prepare_plan(stage4104, key="bounded-progress")
    copying = migration._operation_progress(
        plan,
        phase="copying",
        speed_bytes_per_second=migration.THROUGHPUT_MAX_BYTES_PER_SECOND + 1,
        eta_seconds=migration.THROUGHPUT_MAX_ETA_SECONDS + 1,
    )
    assert copying["speed_bytes_per_second"] is None
    assert copying["eta_seconds"] is None
    non_copy = migration._operation_progress(
        plan,
        phase="target_verified",
        speed_bytes_per_second=123,
        eta_seconds=456,
    )
    assert non_copy["speed_bytes_per_second"] is None
    assert non_copy["eta_seconds"] is None


def test_generic_migration_exceptions_are_sanitized_from_public_and_audit(stage4104, monkeypatch):
    secret = "sqlite SELECT /private/archive secret-token"
    real_root_snapshot = migration.root_snapshot
    monkeypatch.setattr(migration, "root_snapshot", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(secret)))
    with pytest.raises(ArchiveMigrationBlocked) as request_error:
        migration.request_migration_plan(
            stage4104.db,
            actor=stage4104.owner,
            source_root_id=stage4104.roots["source"].id,
            target_root_id=stage4104.roots["target"].id,
            idempotency_key="sanitized-request",
        )
    assert request_error.value.reason_code == "migration_plan_preparation_failed"
    assert secret not in str(request_error.value)

    monkeypatch.setattr(migration, "root_snapshot", real_root_snapshot)
    requested = migration.request_migration_plan(
        stage4104.db,
        actor=stage4104.owner,
        source_root_id=stage4104.roots["source"].id,
        target_root_id=stage4104.roots["target"].id,
        idempotency_key="sanitized-worker",
    )
    real_materialize = migration._materialize_plan_batch
    monkeypatch.setattr(
        migration,
        "_materialize_plan_batch",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    assert migration._prepare_one_plan() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, requested["plan_id"])
    assert plan.reason_code == "migration_plan_preparation_failed"
    audit_rows = stage4104.db.query(AuditEvent).filter(AuditEvent.target_id == plan.id).all()
    assert audit_rows
    assert all(secret not in str(row.event_metadata) for row in audit_rows)
    monkeypatch.setattr(migration, "_materialize_plan_batch", real_materialize)

    camera = add_camera(stage4104, name="Sanitized Worker")
    add_segment(stage4104, camera, name="sanitized-worker.mkv")
    worker_plan = prepare_plan(stage4104, key="sanitized-operation")
    queued = queue_plan(stage4104, worker_plan, key="sanitized-operation-apply")
    monkeypatch.setattr(
        migration,
        "_process_item",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    assert operation.reason_code == "migration_worker_failure"
    assert secret not in str(operation.result or {})
    assert secret not in str(operation.progress or {})
    operation_audits = stage4104.db.query(AuditEvent).filter(AuditEvent.target_id == worker_plan.id).all()
    assert all(secret not in str(row.event_metadata) for row in operation_audits)


def test_max_depth_owner_cleanup_continuation_is_executable_and_idempotent(stage4104, monkeypatch):
    plan, operation, item, source_file = build_cleanup_retry_chain(
        stage4104,
        monkeypatch,
        key="max-depth-owner",
    )
    assert operation.retry_depth == MAX_RETRY_DEPTH
    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=operation.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is True
    old_fence = operation.fencing_token
    queued = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=operation.id,
        idempotency_key="max-depth-owner-continuation",
    )
    assert queued["operation"]["operation_id"] == operation.id
    assert queued["operation"]["status"] == "queued"
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, operation.id)
    continuation = dict(operation.parent_snapshot or {})["cleanup_continuation"]
    assert operation.retry_depth == MAX_RETRY_DEPTH
    assert operation.fencing_token == old_fence + 1
    assert continuation["attempt"] == 1
    assert continuation["actor_user_id"] == stage4104.owner.id

    monkeypatch.setattr(migration, "_copy_or_resume_temp", lambda *_a, **_k: pytest.fail("recovery recopy"))
    monkeypatch.setattr(migration, "_verify_and_finalize_target", lambda *_a, **_k: pytest.fail("recovery finalize"))
    monkeypatch.setattr(migration, "_switch_metadata", lambda *_a, **_k: pytest.fail("recovery metadata switch"))
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, operation.id)
    item = stage4104.db.get(ArchiveMigrationItem, item.id)
    assert operation.status == "completed"
    assert item.phase == "completed"
    assert not source_file.exists()

    replay = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=operation.id,
        idempotency_key="max-depth-owner-continuation",
    )
    assert replay["replayed"] is True
    assert replay["operation"]["operation_id"] == operation.id
    attempt_target = migration._cleanup_attempt_target_id(operation.id, continuation["attempt_id"])
    for event_type in (
        "archive_migration.cleanup_continuation_queued",
        "archive_migration.cleanup_continuation_outcome",
    ):
        assert (
            stage4104.db.query(AuditEvent)
            .filter(
                AuditEvent.event_type == event_type,
                AuditEvent.target_type == "storage_operation_attempt",
                AuditEvent.target_id == attempt_target,
            )
            .count()
            == 1
        )


def test_max_depth_owner_same_admin_and_other_admin_keep_executable_cleanup(stage4104, monkeypatch):
    plan, operation, item, source_file = build_cleanup_retry_chain(
        stage4104,
        monkeypatch,
        key="max-depth-cross-actor",
    )
    first_admin = add_user(stage4104, username="max-depth-first-admin")
    second_admin = add_user(stage4104, username="max-depth-second-admin")
    stage4104.owner.is_active = False
    stage4104.db.commit()

    first = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=first_admin,
        operation_id=operation.id,
        idempotency_key="max-depth-first-admin-attempt",
    )
    assert first["operation"]["operation_id"] == operation.id
    first_admin.role = "viewer"
    stage4104.db.commit()
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, operation.id)
    assert operation.status == "blocked"
    assert operation.retry_depth == MAX_RETRY_DEPTH

    stage4104.owner.is_active = True
    stage4104.db.commit()
    owner_attempt = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=operation.id,
        idempotency_key="max-depth-restored-owner-attempt",
    )
    assert owner_attempt["operation"]["operation_id"] == operation.id
    stage4104.owner.is_active = False
    stage4104.db.commit()
    assert migration._run_one_operation() is True

    first_admin.role = "admin"
    stage4104.owner.is_active = True
    stage4104.db.commit()
    same_admin_attempt = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=first_admin,
        operation_id=operation.id,
        idempotency_key="max-depth-same-admin-attempt",
    )
    assert same_admin_attempt["operation"]["operation_id"] == operation.id
    first_admin.role = "viewer"
    stage4104.db.commit()
    assert migration._run_one_operation() is True

    public = migration.get_migration_operation(
        stage4104.db,
        actor=second_admin,
        operation_id=operation.id,
    )
    assert public["operation"]["capabilities"]["cleanup_takeover_allowed"] is True
    other_admin_attempt = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=second_admin,
        operation_id=operation.id,
        idempotency_key="max-depth-other-admin-attempt",
    )
    assert other_admin_attempt["operation"]["operation_id"] == operation.id
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, operation.id)
    continuation = dict(operation.parent_snapshot or {})["cleanup_continuation"]
    assert continuation["attempt"] == 4
    assert continuation["actor_user_id"] == second_admin.id

    monkeypatch.setattr(migration, "_copy_or_resume_temp", lambda *_a, **_k: pytest.fail("recovery recopy"))
    monkeypatch.setattr(migration, "_verify_and_finalize_target", lambda *_a, **_k: pytest.fail("recovery finalize"))
    monkeypatch.setattr(migration, "_switch_metadata", lambda *_a, **_k: pytest.fail("recovery metadata switch"))
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    assert stage4104.db.get(StorageOperation, operation.id).status == "completed"
    assert stage4104.db.get(ArchiveMigrationItem, item.id).phase == "completed"
    assert not source_file.exists()


def test_sibling_limit_uses_same_operation_cleanup_continuation(stage4104, monkeypatch):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="sibling-limit",
    )
    for index in range(MAX_RETRIES_PER_PARENT):
        child = claim_orphan_cleanup_child(
            stage4104,
            plan,
            parent,
            actor=stage4104.owner,
            key=f"sibling-limit-child-{index}",
        )
        terminalize_cleanup_child(stage4104, plan, child)
        plan.current_operation_id = parent.id
        plan.status = parent.status
        plan.phase = parent.status
        plan.reason_code = parent.reason_code
        plan.retry_mode = "cleanup_only"
        plan.finished_at = parent.finished_at
        stage4104.db.add(plan)
        stage4104.db.commit()
    assert (
        stage4104.db.query(StorageOperation)
        .filter(StorageOperation.parent_operation_id == parent.id)
        .count()
        == MAX_RETRIES_PER_PARENT
    )
    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is True
    queued = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
        idempotency_key="sibling-limit-same-operation",
    )
    assert queued["operation"]["operation_id"] == parent.id
    assert stage4104.db.get(StorageOperation, parent.id).retry_depth == 0
    assert MAX_RETRY_DEPTH == 4
    assert MAX_RETRIES_PER_PARENT == 8


def test_owner_orphan_child_is_adopted_with_new_key_and_single_audit(stage4104, monkeypatch):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="owner-orphan-adoption",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="owner-orphan-original-key",
    )
    assert not dict(child.parent_snapshot or {}).get("cross_actor_recovery")
    stage4104.db.query(AuditEvent).filter(
        AuditEvent.event_type == "storage_operation.queued",
        AuditEvent.target_type == "storage_operation",
        AuditEvent.target_id == child.id,
    ).delete(synchronize_session=False)
    stage4104.db.commit()

    adopted = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
        idempotency_key="owner-orphan-new-browser-key",
    )
    assert adopted["replayed"] is True
    assert adopted["operation"]["operation_id"] == child.id
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child.id
    assert (
        stage4104.db.query(StorageOperation)
        .filter(StorageOperation.parent_operation_id == parent.id)
        .count()
        == 1
    )
    for event_type in ("storage_operation.queued", "archive_migration.retry_child_adopted"):
        assert (
            stage4104.db.query(AuditEvent)
            .filter(AuditEvent.event_type == event_type, AuditEvent.target_id == child.id)
            .count()
            == 1
        )
    adoption_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.retry_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .one()
    )
    assert adoption_event.actor_user_id == stage4104.owner.id
    assert adoption_event.event_metadata["repair_origin"] == "endpoint"
    replay = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
        idempotency_key="owner-orphan-another-key",
    )
    assert replay["replayed"] is True
    assert replay["operation"]["operation_id"] == child.id


def test_admin_orphan_child_is_adopted_after_original_actor_returns(stage4104, monkeypatch):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="admin-orphan-adoption",
    )
    admin = add_user(stage4104, username="admin-orphan-adoption-user")
    stage4104.owner.is_active = False
    stage4104.db.commit()
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=admin,
        key="admin-orphan-original-key",
        cross_actor=True,
    )
    assert dict(child.parent_snapshot or {}).get("cross_actor_recovery") == "migration_cleanup_takeover"
    stage4104.owner.is_active = True
    stage4104.db.commit()
    adopted = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=admin,
        operation_id=parent.id,
        idempotency_key="admin-orphan-new-key",
    )
    assert adopted["replayed"] is True
    assert adopted["operation"]["operation_id"] == child.id
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child.id
    adoption_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.retry_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .one()
    )
    assert adoption_event.actor_user_id == admin.id
    assert adoption_event.event_metadata["repair_origin"] == "endpoint"


def test_worker_repairs_exact_orphan_before_claim_and_rejects_tampered_orphan(stage4104, monkeypatch):
    plan, parent, item, _segment, source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="worker-orphan-repair",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="worker-orphan-child",
    )
    real_copy = migration._copy_or_resume_temp
    real_finalize = migration._verify_and_finalize_target
    real_switch = migration._switch_metadata
    monkeypatch.setattr(migration, "_copy_or_resume_temp", lambda *_a, **_k: pytest.fail("recovery recopy"))
    monkeypatch.setattr(migration, "_verify_and_finalize_target", lambda *_a, **_k: pytest.fail("recovery finalize"))
    monkeypatch.setattr(migration, "_switch_metadata", lambda *_a, **_k: pytest.fail("recovery metadata switch"))
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child.id
    assert stage4104.db.get(ArchiveMigrationItem, item.id).phase == "completed"
    assert not source_file.exists()
    monkeypatch.setattr(migration, "_copy_or_resume_temp", real_copy)
    monkeypatch.setattr(migration, "_verify_and_finalize_target", real_finalize)
    monkeypatch.setattr(migration, "_switch_metadata", real_switch)

    other_plan, other_parent, _other_item, _other_segment, _other_source = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="tampered-orphan-repair",
    )
    tampered = claim_orphan_cleanup_child(
        stage4104,
        other_plan,
        other_parent,
        actor=stage4104.owner,
        key="tampered-orphan-child",
    )
    tampered.scope = {"root_ids": [str(other_plan.source_root_id)]}
    stage4104.db.add(tampered)
    stage4104.db.commit()
    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=other_parent.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is False
    with pytest.raises(ArchiveMigrationBlocked, match="migration_retry_child_ambiguous"):
        migration.retry_migration_operation(
            stage4104.db,
            actor=stage4104.owner,
            operation_id=other_parent.id,
            idempotency_key="tampered-orphan-new-key",
        )
    assert stage4104.db.get(ArchiveMigrationPlan, other_plan.id).current_operation_id == other_parent.id


def test_deleted_owner_orphan_is_adopted_without_takeover_marker_and_reconstructs_queue_audit(
    stage4104,
    monkeypatch,
):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="deleted-owner-orphan",
    )
    repair_admin = add_user(stage4104, username="deleted-owner-repair-admin")
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="deleted-owner-orphan-child",
    )
    assert not dict(child.parent_snapshot or {}).get("cross_actor_recovery")
    original_actor_key = str(child.actor_key)
    original_owner_id = int(stage4104.owner.id)
    stage4104.db.query(AuditEvent).filter(
        AuditEvent.event_type == "storage_operation.queued",
        AuditEvent.target_type == "storage_operation",
        AuditEvent.target_id == child.id,
    ).delete(synchronize_session=False)
    enable_sqlite_foreign_keys(stage4104)
    stage4104.db.delete(stage4104.owner)
    stage4104.db.commit()
    stage4104.db.expire_all()
    assert stage4104.db.get(User, original_owner_id) is None
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).actor_user_id is None
    assert stage4104.db.get(StorageOperation, parent.id).actor_user_id is None
    deleted_child = stage4104.db.get(StorageOperation, child.id)
    assert deleted_child.actor_user_id is None
    assert deleted_child.actor_key == original_actor_key

    adopted = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=repair_admin,
        operation_id=parent.id,
        idempotency_key="deleted-owner-repair-key",
    )
    assert adopted["operation"]["operation_id"] == child.id
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child.id
    assert (
        stage4104.db.query(StorageOperation)
        .filter(StorageOperation.parent_operation_id == parent.id)
        .count()
        == 1
    )
    queue_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "storage_operation.queued",
            AuditEvent.target_id == child.id,
        )
        .one()
    )
    assert queue_event.actor_user_id is None
    assert queue_event.event_metadata["queued_actor_user_id"] == original_owner_id
    assert queue_event.event_metadata["queued_actor_deleted"] is True
    assert queue_event.event_metadata["reconstructed_after_binding_crash"] is True
    adoption_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.retry_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .one()
    )
    assert adoption_event.actor_user_id == repair_admin.id
    assert adoption_event.event_metadata["queued_actor_user_id"] == original_owner_id
    assert adoption_event.event_metadata["queued_actor_deleted"] is True
    assert adoption_event.event_metadata["repair_origin"] == "endpoint"
    assert stage4104.db.get(StorageOperation, child.id).actor_user_id is None
    assert stage4104.db.get(StorageOperation, child.id).actor_key == original_actor_key


def test_deleted_recovery_admin_orphan_requires_typed_evidence_and_is_adopted_by_another_admin(
    stage4104,
    monkeypatch,
):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="deleted-recovery-admin-orphan",
    )
    first_admin = add_user(stage4104, username="deleted-recovery-first-admin")
    second_admin = add_user(stage4104, username="deleted-recovery-second-admin")
    stage4104.owner.is_active = False
    stage4104.db.commit()
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=first_admin,
        key="deleted-recovery-admin-child",
        cross_actor=True,
    )
    assert dict(child.parent_snapshot or {}).get("cross_actor_recovery") == "migration_cleanup_takeover"
    original_admin_id = int(first_admin.id)
    original_actor_key = str(child.actor_key)
    stage4104.owner.is_active = True
    enable_sqlite_foreign_keys(stage4104)
    stage4104.db.delete(first_admin)
    stage4104.db.commit()
    stage4104.db.expire_all()
    assert stage4104.db.get(User, original_admin_id) is None
    deleted_child = stage4104.db.get(StorageOperation, child.id)
    assert deleted_child.actor_user_id is None
    assert deleted_child.actor_key == original_actor_key

    adopted = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=second_admin,
        operation_id=parent.id,
        idempotency_key="deleted-recovery-second-admin-key",
    )
    assert adopted["operation"]["operation_id"] == child.id
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child.id
    assert (
        stage4104.db.query(StorageOperation)
        .filter(StorageOperation.parent_operation_id == parent.id)
        .count()
        == 1
    )
    queue_events = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "storage_operation.queued",
            AuditEvent.target_id == child.id,
        )
        .all()
    )
    assert len(queue_events) == 1
    assert queue_events[0].actor_user_id == original_admin_id
    adoption_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.retry_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .one()
    )
    assert adoption_event.actor_user_id == second_admin.id
    assert adoption_event.event_metadata["repair_origin"] == "endpoint"
    assert adoption_event.event_metadata["queued_actor_user_id"] == original_admin_id


def test_worker_repairs_deleted_owner_orphan_as_system_then_exposes_truthful_admin_recovery(
    stage4104,
    monkeypatch,
):
    plan, parent, _item, _segment, source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="deleted-owner-worker-orphan",
    )
    repair_admin = add_user(stage4104, username="deleted-owner-worker-admin")
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="deleted-owner-worker-child",
    )
    owner_id = int(stage4104.owner.id)
    enable_sqlite_foreign_keys(stage4104)
    stage4104.db.delete(stage4104.owner)
    stage4104.db.commit()
    stage4104.db.expire_all()
    assert stage4104.db.get(User, owner_id) is None
    assert stage4104.db.get(StorageOperation, child.id).actor_user_id is None

    monkeypatch.setattr(migration, "_copy_or_resume_temp", lambda *_a, **_k: pytest.fail("worker recopy"))
    monkeypatch.setattr(migration, "_verify_and_finalize_target", lambda *_a, **_k: pytest.fail("worker finalize"))
    monkeypatch.setattr(migration, "_switch_metadata", lambda *_a, **_k: pytest.fail("worker metadata switch"))
    claimed = migration._claim_next_operation()
    assert claimed is not None
    worker_db, worker_plan, handle = claimed
    try:
        assert handle.operation_id == child.id
        migration._execute_operation(worker_db, worker_plan, handle)
    finally:
        worker_db.close()

    stage4104.db.expire_all()
    repaired_plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    repaired_child = stage4104.db.get(StorageOperation, child.id)
    assert repaired_plan.current_operation_id == child.id
    assert repaired_child.status == "blocked"
    assert repaired_child.reason_code == "migration_permission_revoked"
    assert repaired_child.retry_mode == "cleanup_only"
    assert source_file.exists()
    adoption_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.retry_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .one()
    )
    assert adoption_event.actor_user_id is None
    assert adoption_event.event_metadata["repair_origin"] == "system_worker"
    public = migration.get_migration_operation(
        stage4104.db,
        actor=repair_admin,
        operation_id=child.id,
    )
    assert public["operation"]["capabilities"]["cleanup_takeover_allowed"] is True


def test_owner_orphan_does_not_bypass_takeover_eligibility_while_owner_is_available(
    stage4104,
    monkeypatch,
):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="owner-orphan-no-admin-bypass",
    )
    unrelated_admin = add_user(stage4104, username="owner-orphan-unrelated-admin")
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="owner-orphan-no-admin-bypass-child",
    )
    with pytest.raises(ArchiveMigrationBlocked, match="migration_cleanup_takeover_not_allowed"):
        migration.takeover_migration_cleanup(
            stage4104.db,
            actor=unrelated_admin,
            operation_id=parent.id,
            idempotency_key="owner-orphan-unrelated-admin-key",
        )
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == parent.id
    assert (
        stage4104.db.query(StorageOperation)
        .filter(StorageOperation.parent_operation_id == parent.id)
        .count()
        == 1
    )
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.retry_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .count()
        == 0
    )


def test_inactive_owner_orphan_is_adopted_by_authorized_admin(stage4104, monkeypatch):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="inactive-owner-orphan",
    )
    repair_admin = add_user(stage4104, username="inactive-owner-repair-admin")
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="inactive-owner-orphan-child",
    )
    assert not dict(child.parent_snapshot or {}).get("cross_actor_recovery")
    stage4104.owner.is_active = False
    stage4104.db.commit()
    adopted = migration.takeover_migration_cleanup(
        stage4104.db,
        actor=repair_admin,
        operation_id=parent.id,
        idempotency_key="inactive-owner-repair-key",
    )
    assert adopted["operation"]["operation_id"] == child.id
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child.id
    adoption_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.retry_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .one()
    )
    assert adoption_event.actor_user_id == repair_admin.id


def test_recovery_admin_orphan_without_typed_marker_fails_closed(stage4104, monkeypatch):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="recovery-admin-missing-marker",
    )
    recovery_admin = add_user(stage4104, username="missing-marker-recovery-admin")
    stage4104.owner.is_active = False
    stage4104.db.commit()
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=recovery_admin,
        key="recovery-admin-missing-marker-child",
        cross_actor=True,
    )
    snapshot = dict(child.parent_snapshot or {})
    assert snapshot.get("cross_actor_recovery") == "migration_cleanup_takeover"
    snapshot["cross_actor_recovery"] = None
    child.parent_snapshot = snapshot
    stage4104.db.add(child)
    stage4104.db.commit()
    public = migration.get_migration_operation(
        stage4104.db,
        actor=recovery_admin,
        operation_id=parent.id,
    )
    assert public["operation"]["capabilities"]["cleanup_takeover_allowed"] is False
    with pytest.raises(ArchiveMigrationBlocked, match="migration_retry_child_ambiguous"):
        migration.takeover_migration_cleanup(
            stage4104.db,
            actor=recovery_admin,
            operation_id=parent.id,
            idempotency_key="recovery-admin-missing-marker-repair",
        )
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == parent.id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("finished_at", datetime(2026, 1, 1)),
        ("result", {"status": "completed"}),
        ("reason_code", "migration_filesystem_failure"),
        ("next_action", "retry"),
        ("started_at", datetime(2026, 1, 1)),
        ("heartbeat_at", datetime(2026, 1, 1)),
        ("owner_token_hash", "a" * 64),
        ("owner_instance_id", "contradictory-worker"),
        ("fencing_token", 1),
        ("retry_allowed", True),
        ("retry_mode", "cleanup_only"),
        ("cancel_allowed", False),
        ("status", "running"),
        ("status", "cancel_requested"),
    ],
)
def test_contradictory_active_orphan_child_fails_closed(stage4104, monkeypatch, field, value):
    case_id = hashlib.sha256(f"{field}:{value!r}".encode("utf-8")).hexdigest()[:8]
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key=f"contradictory-orphan-{field}-{case_id}",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key=f"contradictory-child-{field}-{case_id}",
    )
    setattr(child, field, deepcopy(value))
    stage4104.db.add(child)
    stage4104.db.commit()
    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is False
    with pytest.raises(ArchiveMigrationBlocked, match="migration_retry_child_ambiguous"):
        migration.retry_migration_operation(
            stage4104.db,
            actor=stage4104.owner,
            operation_id=parent.id,
            idempotency_key=f"contradictory-repair-{field}-{case_id}",
        )
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == parent.id


def test_execution_audit_without_claim_fields_makes_orphan_ambiguous(stage4104, monkeypatch):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="execution-audit-orphan",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="execution-audit-orphan-child",
    )
    migration.create_event(
        db=stage4104.db,
        actor=stage4104.owner,
        category="storage",
        event_type="storage_operation.started",
        message_ru="test",
        message_en="test",
        target_type="storage_operation",
        target_id=child.id,
    )
    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is False
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == parent.id


def test_legitimate_queue_lease_and_initial_progress_are_accepted(stage4104, monkeypatch):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="legitimate-queue-lease-orphan",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="legitimate-queue-lease-child",
    )
    assert child.lease_expires_at is not None
    assert child.progress["phase"] == "queued"
    assert migration._operation_was_never_claimed(stage4104.db, child, plan) is True
    repaired = migration.retry_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
        idempotency_key="legitimate-queue-lease-repair",
    )
    assert repaired["operation"]["operation_id"] == child.id


@pytest.mark.parametrize(
    "bad_actor_key",
    [
        "",
        "user:",
        "user:0",
        "user:-1",
        "user:+1",
        "user:01",
        "user: 1",
        "user:1 ",
        "user:1-extra",
        "system:1",
        "user:١",
    ],
)
def test_null_actor_with_malformed_or_noncanonical_key_fails_closed(
    stage4104,
    monkeypatch,
    bad_actor_key,
):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key=f"bad-null-actor-{hashlib.sha256(bad_actor_key.encode()).hexdigest()[:8]}",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key=f"bad-null-child-{hashlib.sha256(bad_actor_key.encode()).hexdigest()[:8]}",
    )
    child.actor_user_id = None
    child.actor_key = bad_actor_key
    stage4104.db.add(child)
    stage4104.db.commit()
    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is False
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == parent.id


@pytest.mark.parametrize("tamper", ["actor_kind", "system_owner", "mismatched_user_id"])
def test_orphan_actor_contract_tampering_fails_closed(stage4104, monkeypatch, tamper):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key=f"actor-contract-tamper-{tamper}",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key=f"actor-contract-tamper-child-{tamper}",
    )
    if tamper == "actor_kind":
        child.actor_kind = "system"
    elif tamper == "system_owner":
        child.system_owner = "archive-migration"
    else:
        another_user = add_user(stage4104, username="actor-contract-mismatch-user")
        child.actor_user_id = another_user.id
    stage4104.db.add(child)
    stage4104.db.commit()
    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is False
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == parent.id


def test_multiple_active_orphan_candidates_fail_closed(stage4104, monkeypatch):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key="multiple-orphan-candidates",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key="multiple-orphan-first",
    )
    values = {
        column.name: deepcopy(getattr(child, column.name))
        for column in StorageOperation.__table__.columns
    }
    values["id"] = "multiple-orphan-second-child"
    values["idempotency_key"] = "multiple-orphan-second"
    second = StorageOperation(**values)
    stage4104.db.add(second)
    stage4104.db.commit()
    public = migration.get_migration_operation(
        stage4104.db,
        actor=stage4104.owner,
        operation_id=parent.id,
    )
    assert public["operation"]["capabilities"]["owner_retry_allowed"] is False
    with pytest.raises(ArchiveMigrationBlocked, match="migration_retry_child_ambiguous"):
        migration.retry_migration_operation(
            stage4104.db,
            actor=stage4104.owner,
            operation_id=parent.id,
            idempotency_key="multiple-orphan-repair",
        )
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == parent.id


@pytest.mark.parametrize("first_repair", ["endpoint", "worker"])
def test_endpoint_worker_repair_race_reuses_one_binding_child_and_adoption_audit(
    stage4104,
    monkeypatch,
    first_repair,
):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key=f"repair-race-{first_repair}",
    )
    child = claim_orphan_cleanup_child(
        stage4104,
        plan,
        parent,
        actor=stage4104.owner,
        key=f"repair-race-child-{first_repair}",
    )
    if first_repair == "endpoint":
        first = migration._adopt_exact_orphan_migration_child(
            stage4104.db,
            plan=plan,
            previous=parent,
            expected_child_id=child.id,
            repair_actor=stage4104.owner,
            repair_intent="owner_retry",
        )
        second = migration._adopt_exact_orphan_migration_child(
            stage4104.db,
            plan=plan,
            previous=parent,
            expected_child_id=child.id,
            repair_actor=None,
            repair_intent="system_worker",
        )
        expected_audit_actor_id = stage4104.owner.id
        expected_origin = "endpoint"
    else:
        first = migration._adopt_exact_orphan_migration_child(
            stage4104.db,
            plan=plan,
            previous=parent,
            expected_child_id=child.id,
            repair_actor=None,
            repair_intent="system_worker",
        )
        second = migration._adopt_exact_orphan_migration_child(
            stage4104.db,
            plan=plan,
            previous=parent,
            expected_child_id=child.id,
            repair_actor=stage4104.owner,
            repair_intent="owner_retry",
        )
        expected_audit_actor_id = None
        expected_origin = "system_worker"
    assert first.id == second.id == child.id
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child.id
    assert (
        stage4104.db.query(StorageOperation)
        .filter(StorageOperation.parent_operation_id == parent.id)
        .count()
        == 1
    )
    adoption_events = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.retry_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .all()
    )
    assert len(adoption_events) == 1
    assert adoption_events[0].actor_user_id == expected_audit_actor_id
    assert adoption_events[0].event_metadata["repair_origin"] == expected_origin
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "storage_operation.queued",
            AuditEvent.target_id == child.id,
        )
        .count()
        == 1
    )


@pytest.mark.parametrize(
    ("repair_mode", "fault_event"),
    [
        ("owner", "archive_migration.cleanup_retry_queued"),
        ("admin", "archive_migration.cleanup_takeover_queued"),
        ("owner", "storage_operation.queued"),
        ("owner", "archive_migration.retry_child_adopted"),
    ],
)
def test_orphan_adoption_audit_fault_rolls_back_and_retries_exactly_once(
    stage4104,
    monkeypatch,
    repair_mode,
    fault_event,
):
    case = fault_event.rsplit(".", 1)[-1]
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key=f"audit-fault-{repair_mode}-{case}",
    )
    if repair_mode == "admin":
        repair_actor = add_user(stage4104, username=f"audit-fault-admin-{case}")
        stage4104.owner.is_active = False
        stage4104.db.add(stage4104.owner)
        stage4104.db.commit()
        child = claim_orphan_cleanup_child(
            stage4104,
            plan,
            parent,
            actor=repair_actor,
            key=f"audit-fault-admin-child-{case}",
            cross_actor=True,
        )
        cleanup_event = "archive_migration.cleanup_takeover_queued"
        cleanup_target_id = str(child.id)
    else:
        repair_actor = stage4104.owner
        child = claim_orphan_cleanup_child(
            stage4104,
            plan,
            parent,
            actor=repair_actor,
            key=f"audit-fault-owner-child-{case}",
        )
        cleanup_event = "archive_migration.cleanup_retry_queued"
        cleanup_target_id = str(plan.id)

    if fault_event == "storage_operation.queued":
        stage4104.db.query(AuditEvent).filter(
            AuditEvent.event_type == "storage_operation.queued",
            AuditEvent.target_type == "storage_operation",
            AuditEvent.target_id == str(child.id),
        ).delete(synchronize_session=False)
        stage4104.db.commit()

    plan_id = str(plan.id)
    parent_id = str(parent.id)
    child_id = str(child.id)
    actor_id = int(repair_actor.id)
    child_before = operation_invariant_snapshot(child)
    before_events = audit_event_ids(stage4104.db)
    attempted_events = []
    real_create_event = migration.create_event

    def failing_create_event(**kwargs):
        event_type = str(kwargs.get("event_type") or "")
        attempted_events.append(event_type)
        if event_type == fault_event:
            kwargs["db"].rollback()
            return None
        return real_create_event(**kwargs)

    monkeypatch.setattr(migration, "create_event", failing_create_event)
    fault_db = stage4104.Session()
    try:
        fault_actor = fault_db.get(User, actor_id)
        with pytest.raises(ArchiveMigrationBlocked, match="migration_audit_persistence_failed"):
            invoke_orphan_repair(
                fault_db,
                repair_mode=repair_mode,
                actor=fault_actor,
                parent_id=parent_id,
                key=f"audit-fault-attempt-{case}",
            )
    finally:
        fault_db.rollback()
        fault_db.close()

    assert fault_event in attempted_events
    if fault_event != "archive_migration.retry_child_adopted":
        assert "archive_migration.retry_child_adopted" not in attempted_events

    verify_db = stage4104.Session()
    try:
        failed_plan = verify_db.get(ArchiveMigrationPlan, plan_id)
        failed_child = verify_db.get(StorageOperation, child_id)
        assert failed_plan.current_operation_id == parent_id
        assert operation_invariant_snapshot(failed_child) == child_before
        assert audit_event_ids(verify_db) == before_events
        assert (
            verify_db.query(StorageOperation)
            .filter(StorageOperation.parent_operation_id == parent_id)
            .count()
            == 1
        )
        assert (
            verify_db.query(AuditEvent)
            .filter(
                AuditEvent.event_type == "archive_migration.retry_child_adopted",
                AuditEvent.target_id == child_id,
            )
            .count()
            == 0
        )
    finally:
        verify_db.close()

    monkeypatch.setattr(migration, "create_event", real_create_event)
    retry_db = stage4104.Session()
    try:
        retry_actor = retry_db.get(User, actor_id)
        repaired = invoke_orphan_repair(
            retry_db,
            repair_mode=repair_mode,
            actor=retry_actor,
            parent_id=parent_id,
            key=f"audit-fault-retry-{case}",
        )
        assert repaired["replayed"] is True
        assert repaired["operation"]["operation_id"] == child_id
    finally:
        retry_db.close()

    final_db = stage4104.Session()
    try:
        assert final_db.get(ArchiveMigrationPlan, plan_id).current_operation_id == child_id
        assert (
            final_db.query(StorageOperation)
            .filter(StorageOperation.parent_operation_id == parent_id)
            .count()
            == 1
        )
        assert (
            final_db.query(AuditEvent)
            .filter(
                AuditEvent.event_type == "storage_operation.queued",
                AuditEvent.target_id == child_id,
            )
            .count()
            == 1
        )
        assert (
            final_db.query(AuditEvent)
            .filter(
                AuditEvent.event_type == cleanup_event,
                AuditEvent.target_id == cleanup_target_id,
            )
            .count()
            == 1
        )
        assert (
            final_db.query(AuditEvent)
            .filter(
                AuditEvent.event_type == "archive_migration.retry_child_adopted",
                AuditEvent.target_id == child_id,
            )
            .count()
            == 1
        )
        successful_events = audit_event_ids(final_db)
    finally:
        final_db.close()

    replay_db = stage4104.Session()
    try:
        replay_actor = replay_db.get(User, actor_id)
        replay = invoke_orphan_repair(
            replay_db,
            repair_mode=repair_mode,
            actor=replay_actor,
            parent_id=parent_id,
            key=f"audit-fault-replay-{case}",
        )
        assert replay["replayed"] is True
        assert replay["operation"]["operation_id"] == child_id
        assert audit_event_ids(replay_db) == successful_events
    finally:
        replay_db.close()


@pytest.mark.parametrize(
    "reject_kind",
    ["malformed_domain", "missing_plan", "contradictory_orphan", "multiple_orphans"],
)
def test_worker_isolates_controlled_reject_and_executes_independent_migration(
    stage4104,
    monkeypatch,
    reject_kind,
):
    rejected = []
    if reject_kind in {"malformed_domain", "missing_plan"}:
        domain_ref = (
            "invalid-domain"
            if reject_kind == "malformed_domain"
            else "migration-plan:missing-stage4104-plan"
        )
        rejected.append(
            add_malformed_worker_candidate(
                stage4104,
                operation_id=f"worker-reject-{reject_kind}",
                queued_at=datetime.utcnow() - timedelta(hours=2),
                domain_ref=domain_ref,
            )
        )
    else:
        plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
            stage4104,
            monkeypatch,
            key=f"worker-reject-{reject_kind}",
        )
        child = claim_orphan_cleanup_child(
            stage4104,
            plan,
            parent,
            actor=stage4104.owner,
            key=f"worker-reject-child-{reject_kind}",
        )
        if reject_kind == "contradictory_orphan":
            child.reason_code = "migration_filesystem_failure"
            stage4104.db.add(child)
            stage4104.db.commit()
            rejected.append(child)
        else:
            values = {
                column.name: deepcopy(getattr(child, column.name))
                for column in StorageOperation.__table__.columns
            }
            values["id"] = "worker-reject-multiple-second"
            values["idempotency_key"] = "worker-reject-multiple-second"
            second = StorageOperation(**values)
            stage4104.db.add(second)
            stage4104.db.commit()
            rejected.extend((child, second))

    add_archive_root(stage4104, name="worker-source")
    add_archive_root(stage4104, name="worker-target")
    valid_plan, valid_operation, valid_source = create_queued_migration(
        stage4104,
        key=f"worker-valid-{reject_kind}",
        source_name="worker-source",
        target_name="worker-target",
    )
    rejected_ids = [str(row.id) for row in rejected]
    rejected_before = {
        str(row.id): operation_invariant_snapshot(row)
        for row in rejected
    }
    rejected_audits_before = {
        str(event_id)
        for (event_id,) in stage4104.db.query(AuditEvent.id)
        .filter(AuditEvent.target_id.in_(rejected_ids))
        .all()
    }

    claimed = migration._claim_next_operation()
    assert claimed is not None
    worker_db, worker_plan, handle = claimed
    try:
        assert handle.operation_id == valid_operation.id
        migration._execute_operation(worker_db, worker_plan, handle)
    finally:
        worker_db.close()

    verify_db = stage4104.Session()
    try:
        assert verify_db.get(ArchiveMigrationPlan, valid_plan.id).status == "completed"
        assert verify_db.get(StorageOperation, valid_operation.id).status == "completed"
        assert not valid_source.exists()
        for rejected_id in rejected_ids:
            assert operation_invariant_snapshot(verify_db.get(StorageOperation, rejected_id)) == rejected_before[rejected_id]
        rejected_audits_after = {
            str(event_id)
            for (event_id,) in verify_db.query(AuditEvent.id)
            .filter(AuditEvent.target_id.in_(rejected_ids))
            .all()
        }
        assert rejected_audits_after == rejected_audits_before
        assert (
            verify_db.query(AuditEvent)
            .filter(
                AuditEvent.event_type == "archive_migration.retry_child_adopted",
                AuditEvent.target_id.in_(rejected_ids),
            )
            .count()
            == 0
        )
    finally:
        verify_db.close()


def test_worker_fairness_reaches_valid_candidate_beyond_rejected_batch_after_restart(
    stage4104,
    monkeypatch,
):
    monkeypatch.setattr(migration, "WORKER_CANDIDATE_BATCH_SIZE", 3)
    rejected = []
    base_time = datetime.utcnow() - timedelta(hours=2)
    for index in range(7):
        rejected.append(
            add_malformed_worker_candidate(
                stage4104,
                operation_id=f"fairness-corrupt-{index:02d}",
                queued_at=base_time + timedelta(seconds=index),
                domain_ref="invalid-domain",
            )
        )
    add_archive_root(stage4104, name="fairness-source")
    add_archive_root(stage4104, name="fairness-target")
    valid_plan, valid_operation, valid_source = create_queued_migration(
        stage4104,
        key="fairness-valid",
        source_name="fairness-source",
        target_name="fairness-target",
    )
    rejected_before = {
        str(row.id): operation_invariant_snapshot(row)
        for row in rejected
    }
    observed_batch_sizes = []
    real_load_batch = migration._load_worker_candidate_batch

    def observed_load_batch(cursor):
        batch = real_load_batch(cursor)
        observed_batch_sizes.append(len(batch))
        return batch

    monkeypatch.setattr(migration, "_load_worker_candidate_batch", observed_load_batch)
    assert migration._claim_next_operation() is None
    assert migration._worker_candidate_cursor_snapshot() is not None

    migration._reset_worker_candidate_scan_state()
    claimed = None
    for _attempt in range(4):
        claimed = migration._claim_next_operation()
        if claimed is not None:
            break
    assert claimed is not None
    worker_db, worker_plan, handle = claimed
    try:
        assert handle.operation_id == valid_operation.id
        migration._execute_operation(worker_db, worker_plan, handle)
    finally:
        worker_db.close()

    assert observed_batch_sizes
    assert all(size <= 3 for size in observed_batch_sizes)
    verify_db = stage4104.Session()
    try:
        assert verify_db.get(ArchiveMigrationPlan, valid_plan.id).status == "completed"
        assert not valid_source.exists()
        for rejected_id, snapshot in rejected_before.items():
            assert operation_invariant_snapshot(verify_db.get(StorageOperation, rejected_id)) == snapshot
    finally:
        verify_db.close()


def test_worker_candidate_conflict_remains_authoritative(stage4104):
    plan, operation, _source_file = create_queued_migration(
        stage4104,
        key="worker-conflict-valid",
        source_name="source",
        target_name="target",
    )
    now = datetime.utcnow()
    blocker = StorageOperation(
        id="worker-conflict-blocker",
        operation_type="archive_root_delete",
        actor_kind="system",
        actor_key="system:stage4104-worker-conflict",
        system_owner="stage4104-worker-conflict",
        idempotency_key="worker-conflict-blocker",
        request_fingerprint=hashlib.sha256(b"worker-conflict-blocker").hexdigest(),
        domain_ref=f"archive-root:{plan.source_root_id}",
        status="running",
        scope={"root_ids": [str(plan.source_root_id)]},
        progress={"phase": "running"},
        owner_token_hash=hashlib.sha256(b"worker-conflict-token").hexdigest(),
        owner_instance_id="stage4104-worker-conflict",
        fencing_token=1,
        revision=1,
        queued_at=now - timedelta(seconds=1),
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )
    stage4104.db.add(blocker)
    stage4104.db.commit()
    operation_before = operation_invariant_snapshot(operation)

    assert migration._claim_next_operation() is None
    verify_db = stage4104.Session()
    try:
        assert operation_invariant_snapshot(verify_db.get(StorageOperation, operation.id)) == operation_before
        assert verify_db.get(StorageOperation, operation.id).status == "queued"
        assert verify_db.get(StorageOperation, blocker.id).status == "running"
    finally:
        verify_db.close()


def test_worker_unknown_infrastructure_failure_aborts_poll_without_cursor_advance(
    stage4104,
    monkeypatch,
):
    first_plan, first_operation, _first_source = create_queued_migration(
        stage4104,
        key="worker-infrastructure-first",
        source_name="source",
        target_name="target",
    )
    add_archive_root(stage4104, name="infrastructure-source")
    add_archive_root(stage4104, name="infrastructure-target")
    _second_plan, second_operation, _second_source = create_queued_migration(
        stage4104,
        key="worker-infrastructure-second",
        source_name="infrastructure-source",
        target_name="infrastructure-target",
    )
    first_before = operation_invariant_snapshot(first_operation)
    second_before = operation_invariant_snapshot(second_operation)
    calls = []
    real_reclaim = migration.reclaim_operation_with_conflicts

    def fail_reclaim(*args, **kwargs):
        calls.append(str(kwargs.get("operation_id") or ""))
        raise RuntimeError("synthetic infrastructure failure")

    monkeypatch.setattr(migration, "reclaim_operation_with_conflicts", fail_reclaim)
    assert migration._claim_next_operation() is None
    assert calls == [str(first_operation.id)]
    assert migration._worker_candidate_cursor_snapshot() is None

    verify_db = stage4104.Session()
    try:
        assert operation_invariant_snapshot(verify_db.get(StorageOperation, first_operation.id)) == first_before
        assert operation_invariant_snapshot(verify_db.get(StorageOperation, second_operation.id)) == second_before
    finally:
        verify_db.close()

    monkeypatch.setattr(migration, "reclaim_operation_with_conflicts", real_reclaim)
    claimed = migration._claim_next_operation()
    assert claimed is not None
    worker_db, worker_plan, handle = claimed
    try:
        assert worker_plan.id == first_plan.id
        assert handle.operation_id == first_operation.id
    finally:
        worker_db.rollback()
        worker_db.close()


def test_worker_rejection_diagnostics_are_throttled_bounded_and_non_mutating(
    stage4104,
    monkeypatch,
):
    monkeypatch.setattr(migration, "WORKER_CANDIDATE_DIAGNOSTIC_MAX_KEYS", 4)
    monkeypatch.setattr(migration, "WORKER_CANDIDATE_DIAGNOSTIC_INTERVAL_SECONDS", 60.0)
    rejected = [
        add_malformed_worker_candidate(
            stage4104,
            operation_id=f"diagnostic-reject-{index}",
            queued_at=datetime.utcnow() - timedelta(hours=1, seconds=10 - index),
            domain_ref="invalid-domain",
        )
        for index in range(2)
    ]
    rejected_before = {
        str(row.id): operation_invariant_snapshot(row)
        for row in rejected
    }
    audit_before = audit_event_ids(stage4104.db)
    warnings = []

    def capture_warning(*args, **kwargs):
        warnings.append((args, kwargs))

    monkeypatch.setattr(migration.logger, "warning", capture_warning)
    assert migration._claim_next_operation() is None
    assert len(warnings) == 2
    assert migration._claim_next_operation() is None
    assert migration._worker_candidate_cursor_snapshot() is None
    assert migration._claim_next_operation() is None
    assert len(warnings) == 2

    for index in range(10):
        migration._log_worker_candidate_rejection(
            f"bounded-diagnostic-{index}",
            "migration_worker_candidate_rejected",
        )
    assert len(migration._worker_candidate_diagnostics) <= 4

    verify_db = stage4104.Session()
    try:
        assert audit_event_ids(verify_db) == audit_before
        for rejected_id, snapshot in rejected_before.items():
            assert operation_invariant_snapshot(verify_db.get(StorageOperation, rejected_id)) == snapshot
    finally:
        verify_db.close()


def install_migration_audit_rollback_fault(monkeypatch, event_type):
    real_create_event = migration.create_event
    observed = []

    def failing_create_event(**kwargs):
        if str(kwargs.get("event_type") or "") == event_type:
            observed.append(
                {
                    "target_id": str(kwargs.get("target_id") or ""),
                    "metadata": dict(kwargs.get("metadata") or {}),
                }
            )
            kwargs["db"].rollback()
            return None
        return real_create_event(**kwargs)

    monkeypatch.setattr(migration, "create_event", failing_create_event)
    return real_create_event, observed


def create_initial_apply_orphan(ctx, monkeypatch, *, key):
    camera = add_camera(ctx, name=f"Initial Orphan {key}")
    _segment, source_file = add_segment(
        ctx,
        camera,
        name=f"{key}.mkv",
        content=b"initial-orphan",
    )
    plan = prepare_plan(ctx, key=f"{key}-plan")
    real_create_event, observed = install_migration_audit_rollback_fault(
        monkeypatch,
        "archive_migration.apply_queued",
    )
    original_key = f"{key}-original-child"
    with pytest.raises(ArchiveMigrationBlocked, match="migration_audit_persistence_failed"):
        migration.queue_migration_apply(
            ctx.db,
            actor=ctx.owner,
            plan_id=plan.id,
            expected_hash=plan.canonical_hash,
            idempotency_key=original_key,
        )
    assert len(observed) == 1
    child_id = str(observed[0]["metadata"]["operation_id"])
    monkeypatch.setattr(migration, "create_event", real_create_event)
    ctx.db.expire_all()
    authoritative_plan = ctx.db.get(ArchiveMigrationPlan, str(plan.id))
    child = ctx.db.get(StorageOperation, child_id)
    assert authoritative_plan is not None
    assert authoritative_plan.current_operation_id is None
    assert authoritative_plan.status in migration.PLAN_READY_STATUSES
    assert child is not None and child.status == "queued"
    assert child.idempotency_key == original_key
    return authoritative_plan, child, source_file, original_key


def test_plan_requested_audit_rollback_never_returns_phantom_plan(stage4104, monkeypatch):
    real_create_event, observed = install_migration_audit_rollback_fault(
        monkeypatch,
        "archive_migration.plan_requested",
    )
    with pytest.raises(ArchiveMigrationBlocked, match="migration_audit_persistence_failed"):
        migration.request_migration_plan(
            stage4104.db,
            actor=stage4104.owner,
            source_root_id=stage4104.roots["source"].id,
            target_root_id=stage4104.roots["target"].id,
            idempotency_key="audit-plan-requested",
        )
    assert len(observed) == 1
    phantom_plan_id = observed[0]["target_id"]
    verify_db = stage4104.Session()
    try:
        assert verify_db.get(ArchiveMigrationPlan, phantom_plan_id) is None
        assert verify_db.query(ArchiveMigrationPlan).count() == 0
    finally:
        verify_db.close()

    monkeypatch.setattr(migration, "create_event", real_create_event)
    created = migration.request_migration_plan(
        stage4104.db,
        actor=stage4104.owner,
        source_root_id=stage4104.roots["source"].id,
        target_root_id=stage4104.roots["target"].id,
        idempotency_key="audit-plan-requested",
    )
    assert stage4104.db.get(ArchiveMigrationPlan, created["plan_id"]) is not None
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.plan_requested",
            AuditEvent.target_id == created["plan_id"],
        )
        .count()
        == 1
    )


@pytest.mark.parametrize(
    ("expected_event", "expected_status", "with_segment"),
    [
        ("archive_migration.plan_ready", "ready", True),
        ("archive_migration.plan_blocked", "blocked", False),
    ],
)
def test_plan_finalization_audit_fault_keeps_worker_retryable(
    stage4104,
    monkeypatch,
    expected_event,
    expected_status,
    with_segment,
):
    if with_segment:
        camera = add_camera(stage4104, name="Audit Finalize")
        add_segment(stage4104, camera, name="audit-finalize.mkv")
    requested = migration.request_migration_plan(
        stage4104.db,
        actor=stage4104.owner,
        source_root_id=stage4104.roots["source"].id,
        target_root_id=stage4104.roots["target"].id,
        idempotency_key=f"audit-finalize-{expected_status}",
    )
    real_create_event, observed = install_migration_audit_rollback_fault(monkeypatch, expected_event)
    assert migration._prepare_one_plan() is True
    assert len(observed) == 1
    verify_db = stage4104.Session()
    try:
        assert verify_db.get(ArchiveMigrationPlan, requested["plan_id"]).status == "building"
    finally:
        verify_db.close()

    monkeypatch.setattr(migration, "create_event", real_create_event)
    assert migration._prepare_one_plan() is True
    stage4104.db.expire_all()
    plan = stage4104.db.get(ArchiveMigrationPlan, requested["plan_id"])
    assert plan.status == expected_status
    assert (
        stage4104.db.query(AuditEvent)
        .filter(AuditEvent.event_type == expected_event, AuditEvent.target_id == plan.id)
        .count()
        == 1
    )


def test_plan_expiry_audit_fault_blocks_apply_until_exact_retry(stage4104, monkeypatch):
    camera = add_camera(stage4104, name="Audit Expiry")
    add_segment(stage4104, camera, name="audit-expiry.mkv")
    plan = prepare_plan(stage4104, key="audit-expiry-plan")
    plan.expires_at = datetime.utcnow() - timedelta(seconds=1)
    stage4104.db.commit()
    real_create_event, observed = install_migration_audit_rollback_fault(
        monkeypatch,
        "archive_migration.plan_expired",
    )

    with pytest.raises(ArchiveMigrationBlocked, match="migration_audit_persistence_failed"):
        migration.queue_migration_apply(
            stage4104.db,
            actor=stage4104.owner,
            plan_id=plan.id,
            expected_hash=plan.canonical_hash,
            idempotency_key="audit-expiry-apply",
        )
    assert len(observed) == 1
    verify_db = stage4104.Session()
    try:
        authoritative = verify_db.get(ArchiveMigrationPlan, plan.id)
        assert authoritative.status in migration.PLAN_READY_STATUSES
        assert verify_db.query(StorageOperation).count() == 0
    finally:
        verify_db.close()

    monkeypatch.setattr(migration, "create_event", real_create_event)
    status = migration.get_migration_plan(stage4104.db, actor=stage4104.owner, plan_id=plan.id)
    assert status["status"] == "expired"
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.plan_expired",
            AuditEvent.target_id == plan.id,
        )
        .count()
        == 1
    )


def test_apply_queue_audit_fault_reuses_precommitted_child_without_false_success(
    stage4104,
    monkeypatch,
):
    plan, child, _source_file, original_key = create_initial_apply_orphan(
        stage4104,
        monkeypatch,
        key="audit-apply-queue",
    )
    child_id = str(child.id)
    replacement_key = "audit-apply-queue-new-browser-key"
    queued = migration.queue_migration_apply(
        stage4104.db,
        actor=stage4104.owner,
        plan_id=plan.id,
        expected_hash=plan.canonical_hash,
        idempotency_key=replacement_key,
    )
    assert queued["operation"]["operation_id"] == child_id
    assert queued["replayed"] is True
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child_id
    assert stage4104.db.query(StorageOperation).filter_by(id=child_id).count() == 1
    assert stage4104.db.get(StorageOperation, child_id).idempotency_key == original_key

    for replay_key in (original_key, replacement_key, "audit-apply-queue-third-key"):
        replay = migration.queue_migration_apply(
            stage4104.db,
            actor=stage4104.owner,
            plan_id=plan.id,
            expected_hash=plan.canonical_hash,
            idempotency_key=replay_key,
        )
        assert replay["operation"]["operation_id"] == child_id
        assert replay["replayed"] is True

    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.apply_queued",
            AuditEvent.target_id == plan.id,
        )
        .count()
        == 1
    )
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "storage_operation.queued",
            AuditEvent.target_id == child_id,
        )
        .count()
        == 1
    )
    adoption_events = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.initial_child_adopted",
            AuditEvent.target_id == child_id,
        )
        .all()
    )
    assert len(adoption_events) == 1
    assert adoption_events[0].event_metadata["repair_origin"] == "endpoint"
    assert adoption_events[0].actor_user_id == stage4104.owner.id


def test_worker_adopts_initial_orphan_with_normal_queue_lease_without_browser_key(
    stage4104,
    monkeypatch,
):
    plan, child, source_file, original_key = create_initial_apply_orphan(
        stage4104,
        monkeypatch,
        key="initial-worker-repair",
    )
    assert child.lease_expires_at is not None
    assert migration._operation_was_never_claimed(stage4104.db, child, plan) is True
    assert migration._run_one_operation() is True

    stage4104.db.expire_all()
    authoritative_plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    authoritative_child = stage4104.db.get(StorageOperation, child.id)
    assert authoritative_plan.current_operation_id == child.id
    assert authoritative_plan.status == "completed"
    assert authoritative_child.status == "completed"
    assert authoritative_child.idempotency_key == original_key
    assert not source_file.exists()
    adoption_events = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.initial_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .all()
    )
    assert len(adoption_events) == 1
    assert adoption_events[0].actor_user_id is None
    assert adoption_events[0].event_metadata["repair_origin"] == "system_worker"


def test_deleted_initial_actor_converges_blocked_without_physical_mutation(
    stage4104,
    monkeypatch,
):
    enable_sqlite_foreign_keys(stage4104)
    plan, child, source_file, _original_key = create_initial_apply_orphan(
        stage4104,
        monkeypatch,
        key="initial-deleted-actor",
    )
    original_actor_key = str(plan.actor_key)
    original_actor_id = int(stage4104.owner.id)
    process_calls = []
    monkeypatch.setattr(
        migration,
        "_process_item",
        lambda *_args, **_kwargs: process_calls.append("unexpected"),
    )

    stage4104.db.delete(stage4104.owner)
    stage4104.db.commit()
    stage4104.db.expire_all()
    deleted_plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    deleted_child = stage4104.db.get(StorageOperation, child.id)
    assert deleted_plan.actor_user_id is None
    assert deleted_child.actor_user_id is None
    assert deleted_plan.actor_key == deleted_child.actor_key == original_actor_key
    assert migration._canonical_plan_actor_id(deleted_plan) == original_actor_id
    assert migration._canonical_user_operation_actor_id(deleted_child) == original_actor_id

    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    blocked_plan = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    blocked_child = stage4104.db.get(StorageOperation, child.id)
    assert blocked_plan.current_operation_id == child.id
    assert blocked_plan.status == "blocked"
    assert blocked_child.status == "blocked"
    assert blocked_plan.reason_code == "migration_permission_revoked"
    assert blocked_child.reason_code == "migration_permission_revoked"
    assert process_calls == []
    assert source_file.exists()
    assert blocked_child.owner_token_hash is None
    assert blocked_child.owner_instance_id is None
    assert blocked_child.lease_expires_at is None
    assert (
        stage4104.db.query(StorageOperation)
        .filter(StorageOperation.status.in_(tuple(migration.ACTIVE_OPERATION_STATUSES)))
        .count()
        == 0
    )
    adoption_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.initial_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .one()
    )
    assert adoption_event.actor_user_id is None
    assert adoption_event.event_metadata["queued_actor_deleted"] is True


def test_concurrent_new_key_claim_converges_to_exact_initial_child(
    stage4104,
    monkeypatch,
):
    camera = add_camera(stage4104, name="Initial Concurrent Claim")
    add_segment(stage4104, camera, name="initial-concurrent-claim.mkv")
    plan = prepare_plan(stage4104, key="initial-concurrent-plan")
    real_claim = migration.claim_operation_with_conflicts
    winner = {}

    def competing_claim(db, **kwargs):
        if not winner:
            winner_result = real_claim(
                db,
                **{**kwargs, "idempotency_key": "initial-concurrent-winner"},
            )
            winner["operation_id"] = winner_result["operation"]["operation_id"]
            raise migration.StorageOperationConflict(
                {
                    "reason_code": "storage_operation_scope_conflict",
                    "retryable": True,
                }
            )
        return real_claim(db, **kwargs)

    monkeypatch.setattr(migration, "claim_operation_with_conflicts", competing_claim)
    result = migration.queue_migration_apply(
        stage4104.db,
        actor=stage4104.owner,
        plan_id=plan.id,
        expected_hash=plan.canonical_hash,
        idempotency_key="initial-concurrent-loser",
    )
    assert result["replayed"] is True
    assert result["operation"]["operation_id"] == winner["operation_id"]
    operation = stage4104.db.get(StorageOperation, winner["operation_id"])
    assert operation.idempotency_key == "initial-concurrent-winner"
    assert stage4104.db.query(StorageOperation).count() == 1
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == operation.id
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.initial_child_adopted",
            AuditEvent.target_id == operation.id,
        )
        .count()
        == 1
    )


@pytest.mark.parametrize("first_repair", ["endpoint", "worker"])
def test_initial_endpoint_worker_binding_race_is_exactly_once(
    stage4104,
    monkeypatch,
    first_repair,
):
    plan, child, _source_file, _original_key = create_initial_apply_orphan(
        stage4104,
        monkeypatch,
        key=f"initial-binding-race-{first_repair}",
    )
    if first_repair == "endpoint":
        first_actor, first_origin = stage4104.owner, "endpoint"
        second_actor, second_origin = None, "system_worker"
    else:
        first_actor, first_origin = None, "system_worker"
        second_actor, second_origin = stage4104.owner, "endpoint"
    first_plan, first_child, first_replayed = migration._bind_exact_initial_migration_child(
        stage4104.db,
        plan_id=plan.id,
        expected_child_id=child.id,
        audit_actor=first_actor,
        repair_origin=first_origin,
    )
    second_plan, second_child, second_replayed = migration._bind_exact_initial_migration_child(
        stage4104.db,
        plan_id=plan.id,
        expected_child_id=child.id,
        audit_actor=second_actor,
        repair_origin=second_origin,
    )
    assert first_plan.current_operation_id == second_plan.current_operation_id == child.id
    assert first_child.id == second_child.id == child.id
    assert first_replayed is False
    assert second_replayed is True
    assert stage4104.db.query(StorageOperation).count() == 1
    adoption_events = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.initial_child_adopted",
            AuditEvent.target_id == child.id,
        )
        .all()
    )
    assert len(adoption_events) == 1
    assert adoption_events[0].event_metadata["repair_origin"] == first_origin


@pytest.mark.parametrize(
    "tamper",
    [
        "actor",
        "domain",
        "fingerprint",
        "scope",
        "parent",
        "retry_depth",
        "status",
        "fence",
        "owner",
        "started",
        "heartbeat",
        "result",
        "permission_progress",
        "plan_hash",
        "execution_audit",
    ],
)
def test_malformed_initial_orphan_candidates_fail_closed_without_mutation(
    stage4104,
    monkeypatch,
    tamper,
):
    plan, child, source_file, original_key = create_initial_apply_orphan(
        stage4104,
        monkeypatch,
        key=f"initial-candidate-{tamper}",
    )
    if tamper == "actor":
        child.actor_key = "user:999999"
    elif tamper == "domain":
        child.domain_ref = "migration-plan:unrelated"
    elif tamper == "fingerprint":
        child.request_fingerprint = "f" * 64
    elif tamper == "scope":
        child.scope = {"root_ids": [stage4104.roots["third"].id]}
    elif tamper == "parent":
        child.parent_operation_id = "unrelated-parent"
    elif tamper == "retry_depth":
        child.retry_depth = 1
    elif tamper == "status":
        child.status = "running"
    elif tamper == "fence":
        child.fencing_token = 1
    elif tamper == "owner":
        child.owner_token_hash = "a" * 64
        child.owner_instance_id = "unexpected-owner"
    elif tamper == "started":
        child.started_at = datetime.utcnow()
    elif tamper == "heartbeat":
        child.heartbeat_at = datetime.utcnow()
    elif tamper == "result":
        child.result = {"status": "unexpected"}
    elif tamper == "permission_progress":
        child.progress = {**dict(child.progress or {}), "permission_contract": {"apply": []}}
    elif tamper == "plan_hash":
        plan.canonical_hash = "0" * 64
        stage4104.db.add(plan)
    elif tamper == "execution_audit":
        migration.create_event(
            db=stage4104.db,
            actor=stage4104.owner,
            category="storage",
            event_type="storage_operation.started",
            message_ru="test",
            message_en="test",
            target_type="storage_operation",
            target_id=child.id,
        )
    stage4104.db.add(child)
    stage4104.db.commit()
    child_id = str(child.id)

    try:
        candidate = migration._resolve_exact_initial_migration_child(
            stage4104.db,
            plan=stage4104.db.get(ArchiveMigrationPlan, plan.id),
        )
    except ArchiveMigrationBlocked as exc:
        assert exc.reason_code == "migration_initial_child_ambiguous"
        candidate = None
    assert candidate is None
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id is None
    persisted = stage4104.db.get(StorageOperation, child_id)
    assert persisted is not None
    assert persisted.idempotency_key == original_key
    assert source_file.exists()
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.initial_child_adopted",
            AuditEvent.target_id == child_id,
        )
        .count()
        == 0
    )


def test_multiple_exact_initial_orphans_fail_closed(stage4104, monkeypatch):
    plan, child, source_file, _original_key = create_initial_apply_orphan(
        stage4104,
        monkeypatch,
        key="initial-multiple-candidates",
    )
    values = {
        column.name: deepcopy(getattr(child, column.name))
        for column in StorageOperation.__table__.columns
    }
    values["id"] = "initial-multiple-second-child"
    values["idempotency_key"] = "initial-multiple-second-key"
    stage4104.db.add(StorageOperation(**values))
    stage4104.db.commit()

    with pytest.raises(ArchiveMigrationBlocked, match="migration_initial_child_ambiguous"):
        migration._resolve_exact_initial_migration_child(stage4104.db, plan=plan)
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id is None
    assert stage4104.db.query(StorageOperation).count() == 2
    assert source_file.exists()


@pytest.mark.parametrize(
    "fault_event",
    [
        "storage_operation.queued",
        "archive_migration.apply_queued",
        "archive_migration.initial_child_adopted",
    ],
)
def test_initial_adoption_audit_fault_rolls_back_binding_and_retries_exactly_once(
    stage4104,
    monkeypatch,
    fault_event,
):
    plan, child, source_file, original_key = create_initial_apply_orphan(
        stage4104,
        monkeypatch,
        key=f"initial-adoption-fault-{fault_event.rsplit('.', 1)[-1]}",
    )
    if fault_event == "storage_operation.queued":
        stage4104.db.query(AuditEvent).filter(
            AuditEvent.event_type == "storage_operation.queued",
            AuditEvent.target_id == child.id,
        ).delete(synchronize_session=False)
        stage4104.db.commit()
    events_before = audit_event_ids(stage4104.db)
    real_create_event = migration.create_event

    def fail_mandatory_event(**kwargs):
        if str(kwargs.get("event_type") or "") == fault_event:
            kwargs["db"].rollback()
            return None
        return real_create_event(**kwargs)

    monkeypatch.setattr(migration, "create_event", fail_mandatory_event)
    with pytest.raises(ArchiveMigrationBlocked, match="migration_audit_persistence_failed"):
        migration._bind_exact_initial_migration_child(
            stage4104.db,
            plan_id=plan.id,
            expected_child_id=child.id,
            audit_actor=stage4104.owner,
            repair_origin="endpoint",
        )

    verify_db = stage4104.Session()
    try:
        assert verify_db.get(ArchiveMigrationPlan, plan.id).current_operation_id is None
        persisted_child = verify_db.get(StorageOperation, child.id)
        assert persisted_child is not None
        assert persisted_child.idempotency_key == original_key
        assert audit_event_ids(verify_db) == events_before
    finally:
        verify_db.close()
    assert source_file.exists()

    monkeypatch.setattr(migration, "create_event", real_create_event)
    stage4104.db.expire_all()
    bound_plan, bound_child, replayed = migration._bind_exact_initial_migration_child(
        stage4104.db,
        plan_id=plan.id,
        expected_child_id=child.id,
        audit_actor=stage4104.owner,
        repair_origin="endpoint",
    )
    assert replayed is False
    assert bound_plan.current_operation_id == bound_child.id == child.id
    for event_type, target_id in (
        ("storage_operation.queued", child.id),
        ("archive_migration.apply_queued", plan.id),
        ("archive_migration.initial_child_adopted", child.id),
    ):
        assert (
            stage4104.db.query(AuditEvent)
            .filter(AuditEvent.event_type == event_type, AuditEvent.target_id == target_id)
            .count()
            == 1
        )


@pytest.mark.parametrize("recovery_mode", ["owner", "admin"])
def test_cleanup_child_queue_audit_fault_converges_to_same_exact_child(
    stage4104,
    monkeypatch,
    recovery_mode,
):
    plan, parent, _item, _segment, _source_file = create_cleanup_pending_migration(
        stage4104,
        monkeypatch,
        key=f"audit-normal-{recovery_mode}",
    )
    actor = stage4104.owner
    event_type = "archive_migration.cleanup_retry_queued"
    if recovery_mode == "admin":
        actor = add_user(stage4104, username="audit-normal-admin")
        stage4104.owner.is_active = False
        stage4104.db.add(stage4104.owner)
        stage4104.db.commit()
        event_type = "archive_migration.cleanup_takeover_queued"
    real_create_event, observed = install_migration_audit_rollback_fault(monkeypatch, event_type)
    with pytest.raises(ArchiveMigrationBlocked, match="migration_audit_persistence_failed"):
        invoke_orphan_repair(
            stage4104.db,
            repair_mode=recovery_mode,
            actor=actor,
            parent_id=parent.id,
            key=f"audit-normal-{recovery_mode}-child",
        )
    assert len(observed) == 1
    child_id = observed[0]["metadata"]["operation_id"]
    verify_db = stage4104.Session()
    try:
        assert verify_db.get(ArchiveMigrationPlan, plan.id).current_operation_id == parent.id
        assert verify_db.get(StorageOperation, child_id) is not None
        assert (
            verify_db.query(StorageOperation)
            .filter(StorageOperation.parent_operation_id == parent.id)
            .count()
            == 1
        )
    finally:
        verify_db.close()

    monkeypatch.setattr(migration, "create_event", real_create_event)
    stage4104.db.expire_all()
    retry_actor = stage4104.db.get(User, actor.id)
    repaired = invoke_orphan_repair(
        stage4104.db,
        repair_mode=recovery_mode,
        actor=retry_actor,
        parent_id=parent.id,
        key=f"audit-normal-{recovery_mode}-child",
    )
    assert repaired["operation"]["operation_id"] == child_id
    assert repaired["replayed"] is True
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).current_operation_id == child_id
    assert (
        stage4104.db.query(StorageOperation)
        .filter(StorageOperation.parent_operation_id == parent.id)
        .count()
        == 1
    )


def test_apply_started_audit_fault_stops_before_physical_mutation_and_worker_recovers(
    stage4104,
    monkeypatch,
):
    camera = add_camera(stage4104, name="Audit Apply Start")
    _segment, source_file = add_segment(stage4104, camera, name="audit-apply-start.mkv")
    plan = prepare_plan(stage4104, key="audit-apply-start-plan")
    queued = queue_plan(stage4104, plan, key="audit-apply-start-child")
    real_process_item = migration._process_item
    process_calls = []

    def counted_process_item(*args, **kwargs):
        process_calls.append(str(args[2].id))
        return real_process_item(*args, **kwargs)

    monkeypatch.setattr(migration, "_process_item", counted_process_item)
    real_create_event, observed = install_migration_audit_rollback_fault(
        monkeypatch,
        "archive_migration.apply_started",
    )
    assert migration._run_one_operation() is True
    assert len(observed) == 1
    assert process_calls == []
    assert source_file.exists()

    monkeypatch.setattr(migration, "create_event", real_create_event)
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    stage4104.db.commit()
    assert migration._run_one_operation() is True
    assert len(process_calls) == 1
    stage4104.db.expire_all()
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).status == "completed"
    assert stage4104.db.get(StorageOperation, operation.id).status == "completed"


@pytest.mark.parametrize("cancel_with_operation", [False, True])
def test_cancel_audit_fault_converges_without_false_success(
    stage4104,
    monkeypatch,
    cancel_with_operation,
):
    camera = add_camera(stage4104, name=f"Audit Cancel {cancel_with_operation}")
    add_segment(stage4104, camera, name=f"audit-cancel-{cancel_with_operation}.mkv")
    plan = prepare_plan(stage4104, key=f"audit-cancel-{cancel_with_operation}-plan")
    operation = None
    if cancel_with_operation:
        queued = queue_plan(stage4104, plan, key="audit-cancel-operation")
        operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    real_create_event, observed = install_migration_audit_rollback_fault(
        monkeypatch,
        "archive_migration.cancel_requested",
    )
    with pytest.raises(ArchiveMigrationBlocked, match="migration_audit_persistence_failed"):
        if operation is None:
            migration.cancel_migration_plan(stage4104.db, actor=stage4104.owner, plan_id=plan.id)
        else:
            migration.cancel_migration_operation(
                stage4104.db,
                actor=stage4104.owner,
                operation_id=operation.id,
            )
    assert len(observed) == 1

    monkeypatch.setattr(migration, "create_event", real_create_event)
    stage4104.db.expire_all()
    if operation is None:
        result = migration.cancel_migration_plan(stage4104.db, actor=stage4104.owner, plan_id=plan.id)
    else:
        result = migration.cancel_migration_operation(
            stage4104.db,
            actor=stage4104.owner,
            operation_id=operation.id,
        )
    assert result["plan"]["status"] == "cancelled"
    authoritative = stage4104.db.get(ArchiveMigrationPlan, plan.id)
    assert authoritative.status == "cancelled"
    if operation is not None:
        assert stage4104.db.get(StorageOperation, operation.id).status == "cancelled"


def test_terminal_audit_fault_retries_from_item_truth_without_repeating_mutation(
    stage4104,
    monkeypatch,
):
    camera = add_camera(stage4104, name="Audit Terminal")
    _segment, source_file = add_segment(stage4104, camera, name="audit-terminal.mkv")
    plan = prepare_plan(stage4104, key="audit-terminal-plan")
    queued = queue_plan(stage4104, plan, key="audit-terminal-operation")
    real_create_event, observed = install_migration_audit_rollback_fault(
        monkeypatch,
        "archive_migration.operation_completed",
    )
    assert migration._run_one_operation() is True
    assert len(observed) == 1
    stage4104.db.expire_all()
    item = stage4104.db.query(ArchiveMigrationItem).filter_by(plan_id=plan.id).one()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    assert item.phase == "completed"
    assert operation.status == "running"
    assert not source_file.exists()

    monkeypatch.setattr(migration, "create_event", real_create_event)

    def repeated_item_mutation_forbidden(*_args, **_kwargs):
        raise AssertionError("terminal audit retry repeated item mutation")

    monkeypatch.setattr(migration, "_process_item", repeated_item_mutation_forbidden)
    operation.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    stage4104.db.commit()
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).status == "completed"
    assert stage4104.db.get(StorageOperation, operation.id).status == "completed"
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.operation_completed",
            AuditEvent.target_id == plan.id,
        )
        .count()
        == 1
    )


def test_terminal_audit_recovery_fault_is_truthful_and_idempotent(stage4104, monkeypatch):
    camera = add_camera(stage4104, name="Audit Terminal Recovery")
    add_segment(stage4104, camera, name="audit-terminal-recovery.mkv")
    plan = prepare_plan(stage4104, key="audit-terminal-recovery-plan")
    queued = queue_plan(stage4104, plan, key="audit-terminal-recovery-operation")
    assert migration._run_one_operation() is True
    stage4104.db.expire_all()
    operation = stage4104.db.get(StorageOperation, queued["operation"]["operation_id"])
    stage4104.db.query(AuditEvent).filter(
        AuditEvent.event_type == "archive_migration.operation_completed",
        AuditEvent.target_id == plan.id,
    ).delete(synchronize_session=False)
    stage4104.db.commit()
    real_create_event, observed = install_migration_audit_rollback_fault(
        monkeypatch,
        "archive_migration.operation_completed",
    )
    assert migration._recover_terminal_audit_once() is False
    assert len(observed) == 1
    assert stage4104.db.get(ArchiveMigrationPlan, plan.id).status == "completed"
    assert stage4104.db.get(StorageOperation, operation.id).status == "completed"

    monkeypatch.setattr(migration, "create_event", real_create_event)
    assert migration._recover_terminal_audit_once() is True
    assert migration._recover_terminal_audit_once() is False
    assert (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.operation_completed",
            AuditEvent.target_id == plan.id,
        )
        .count()
        == 1
    )


def test_repeated_operation_audits_use_exact_operation_and_fence_identity(stage4104):
    camera = add_camera(stage4104, name="Audit Identity")
    add_segment(stage4104, camera, name="audit-identity.mkv")
    plan = prepare_plan(stage4104, key="audit-identity-plan")
    first_handle = fake_handle(stage4104, plan, operation_id="audit-identity-operation-one")
    first = stage4104.db.get(StorageOperation, first_handle.operation_id)
    migration._audit(
        stage4104.db,
        event_type="archive_migration.cleanup_retry_queued",
        plan=plan,
        operation=first,
    )
    stage4104.db.commit()
    migration._audit(
        stage4104.db,
        event_type="archive_migration.cleanup_retry_queued",
        plan=plan,
        operation=first,
    )
    stage4104.db.commit()
    first.fencing_token = int(first.fencing_token) + 1
    stage4104.db.add(first)
    migration._audit(
        stage4104.db,
        event_type="archive_migration.cleanup_retry_queued",
        plan=plan,
        operation=first,
    )
    stage4104.db.commit()

    second_handle = fake_handle(stage4104, plan, operation_id="audit-identity-operation-two")
    second = stage4104.db.get(StorageOperation, second_handle.operation_id)
    migration._audit(
        stage4104.db,
        event_type="archive_migration.cleanup_retry_queued",
        plan=plan,
        operation=second,
    )
    stage4104.db.commit()
    events = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == "archive_migration.cleanup_retry_queued",
            AuditEvent.target_id == plan.id,
        )
        .all()
    )
    assert len(events) == 3
    transition_fingerprints = {
        str(dict(event.event_metadata or {}).get("migration_transition_fingerprint") or "")
        for event in events
    }
    assert "" not in transition_fingerprints
    assert "***" not in transition_fingerprints
    assert len(transition_fingerprints) == 3


def configure_cleanup_audit_attempt(plan, operation, *, attempt):
    operation.retry_mode = "cleanup_only"
    plan.retry_mode = "cleanup_only"
    operation.parent_snapshot = {
        "cleanup_continuation": {
            "mode": migration.MIGRATION_CLEANUP_CONTINUATION_MODE,
            "attempt": int(attempt),
            "attempt_id": hashlib.sha256(
                f"{operation.id}:attempt:{attempt}".encode("utf-8")
            ).hexdigest()[:32],
            "actor_user_id": int(str(operation.actor_key).removeprefix("user:")),
            "idempotency_fingerprint": hashlib.sha256(
                f"{operation.id}:idempotency:{attempt}".encode("utf-8")
            ).hexdigest(),
            "queued_fencing_token": int(operation.fencing_token),
        }
    }


def clone_exact_audit_event(db, event):
    duplicate = AuditEvent(
        id=str(uuid.uuid4()),
        created_at=datetime.utcnow(),
        actor_user_id=event.actor_user_id,
        actor_username=event.actor_username,
        actor_role=event.actor_role,
        category=event.category,
        event_type=event.event_type,
        severity=event.severity,
        message_ru=event.message_ru,
        message_en=event.message_en,
        target_type=event.target_type,
        target_id=event.target_id,
        target_name=event.target_name,
        event_metadata=deepcopy(event.event_metadata),
        ip_address=None,
        user_agent=None,
    )
    db.add(duplicate)
    db.commit()
    return duplicate


def test_exact_migration_audit_lookup_has_no_latest_64_ceiling(stage4104):
    camera = add_camera(stage4104, name="Audit History Ceiling")
    add_segment(stage4104, camera, name="audit-history-ceiling.mkv")
    plan = prepare_plan(stage4104, key="audit-history-ceiling-plan")
    handle = fake_handle(
        stage4104,
        plan,
        operation_id="audit-history-ceiling-operation",
    )
    operation = stage4104.db.get(StorageOperation, handle.operation_id)
    event_type = "archive_migration.audit_history_ceiling"

    for fence in range(1, 71):
        operation.fencing_token = fence
        stage4104.db.add(operation)
        assert migration._audit(
            stage4104.db,
            event_type=event_type,
            plan=plan,
            operation=operation,
        ) is True
        stage4104.db.commit()
    assert (
        stage4104.db.query(AuditEvent)
        .filter(AuditEvent.event_type == event_type, AuditEvent.target_id == plan.id)
        .count()
        == 70
    )

    operation.fencing_token = 1
    stage4104.db.add(operation)
    assert migration._audit(
        stage4104.db,
        event_type=event_type,
        plan=plan,
        operation=operation,
    ) is False
    stage4104.db.commit()

    operation.fencing_token = 71
    stage4104.db.add(operation)
    assert migration._audit(
        stage4104.db,
        event_type=event_type,
        plan=plan,
        operation=operation,
    ) is True
    stage4104.db.commit()

    second_handle = fake_handle(
        stage4104,
        plan,
        operation_id="audit-history-second-operation",
    )
    second = stage4104.db.get(StorageOperation, second_handle.operation_id)
    assert migration._audit(
        stage4104.db,
        event_type=event_type,
        plan=plan,
        operation=second,
    ) is True
    stage4104.db.commit()

    configure_cleanup_audit_attempt(plan, second, attempt=1)
    stage4104.db.add_all((plan, second))
    assert migration._audit(
        stage4104.db,
        event_type=event_type,
        plan=plan,
        operation=second,
    ) is True
    stage4104.db.commit()
    configure_cleanup_audit_attempt(plan, second, attempt=2)
    stage4104.db.add_all((plan, second))
    assert migration._audit(
        stage4104.db,
        event_type=event_type,
        plan=plan,
        operation=second,
    ) is True
    stage4104.db.commit()

    _fingerprint, metadata = migration._migration_audit_context(
        event_type=event_type,
        plan=plan,
        operation=second,
    )
    exact_event = (
        stage4104.db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_id == plan.id,
            AuditEvent.event_metadata[
                "migration_transition_fingerprint"
            ].as_string()
            == metadata["migration_transition_fingerprint"],
        )
        .one()
    )
    clone_exact_audit_event(stage4104.db, exact_event)
    count_with_preexisting_duplicate = (
        stage4104.db.query(AuditEvent)
        .filter(AuditEvent.event_type == event_type, AuditEvent.target_id == plan.id)
        .count()
    )
    assert migration._audit(
        stage4104.db,
        event_type=event_type,
        plan=plan,
        operation=second,
    ) is False
    stage4104.db.commit()
    assert (
        stage4104.db.query(AuditEvent)
        .filter(AuditEvent.event_type == event_type, AuditEvent.target_id == plan.id)
        .count()
        == count_with_preexisting_duplicate
    )


def postgres_audit_plan_and_operation(ctx):
    db = ctx.db
    actor = User(
        username=f"stage4104-pg-{uuid.uuid4().hex}",
        full_name="Stage 4104 PostgreSQL",
        password_hash="not-used",
        role="owner",
        is_active=True,
    )
    db.add(actor)
    db.commit()
    db.refresh(actor)
    plan = ArchiveMigrationPlan(
        id=str(uuid.uuid4()),
        actor_user_id=actor.id,
        actor_key=f"user:{actor.id}",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint="1" * 64,
        source_root_id=str(uuid.uuid4()),
        target_root_id=str(uuid.uuid4()),
        source_label_snapshot="Source",
        target_label_snapshot="Target",
        source_physical_identity="pv1:" + "1" * 32,
        target_physical_identity="pv1:" + "2" * 32,
        source_snapshot_key="3" * 64,
        target_snapshot_key="4" * 64,
        source_access_identity="5" * 64,
        target_access_identity="6" * 64,
        canonical_hash="7" * 64,
        status="ready",
        phase="ready",
    )
    db.add(plan)
    db.commit()
    operation = StorageOperation(
        id=f"archive_migration_apply:{uuid.uuid4().hex}",
        operation_type=migration.MIGRATION_OPERATION_TYPE,
        actor_kind="user",
        actor_key=f"user:{actor.id}",
        actor_user_id=actor.id,
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint=migration.request_fingerprint(
            migration._operation_identity(plan)
        ),
        domain_ref=migration._operation_domain_ref(str(plan.id)),
        status="running",
        scope={
            "root_ids": [str(plan.source_root_id), str(plan.target_root_id)],
            "physical_volume_ids": [
                str(plan.source_physical_identity),
                str(plan.target_physical_identity),
            ],
        },
        progress={"permission_contract": migration._plan_permission_contract(plan)},
        cancel_allowed=True,
        owner_token_hash="8" * 64,
        owner_instance_id="stage4104-postgres",
        fencing_token=1,
        queued_at=datetime.utcnow(),
        started_at=datetime.utcnow(),
        heartbeat_at=datetime.utcnow(),
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    db.add(operation)
    db.commit()
    return actor, plan, operation


def test_exact_migration_audit_lookup_postgres_has_no_latest_64_ceiling(
    stage4104_postgres,
):
    db = stage4104_postgres.db
    _actor, plan, operation = postgres_audit_plan_and_operation(stage4104_postgres)
    event_type = "archive_migration.postgres_exact_lookup"

    for fence in range(1, 71):
        operation.fencing_token = fence
        db.add(operation)
        assert migration._audit(
            db,
            event_type=event_type,
            plan=plan,
            operation=operation,
        ) is True
        db.commit()
    assert db.query(AuditEvent).filter(AuditEvent.event_type == event_type).count() == 70

    operation.fencing_token = 1
    db.add(operation)
    assert migration._audit(
        db,
        event_type=event_type,
        plan=plan,
        operation=operation,
    ) is False
    db.commit()

    operation.fencing_token = 71
    db.add(operation)
    assert migration._audit(
        db,
        event_type=event_type,
        plan=plan,
        operation=operation,
    ) is True
    db.commit()

    second = StorageOperation(
        id=f"archive_migration_apply:{uuid.uuid4().hex}",
        operation_type=operation.operation_type,
        actor_kind=operation.actor_kind,
        actor_key=operation.actor_key,
        actor_user_id=operation.actor_user_id,
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint=operation.request_fingerprint,
        domain_ref=operation.domain_ref,
        status="running",
        scope=deepcopy(operation.scope),
        progress=deepcopy(operation.progress),
        cancel_allowed=True,
        owner_token_hash="9" * 64,
        owner_instance_id="stage4104-postgres-second",
        fencing_token=71,
        queued_at=datetime.utcnow(),
        started_at=datetime.utcnow(),
        heartbeat_at=datetime.utcnow(),
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    db.add(second)
    db.commit()
    assert migration._audit(
        db,
        event_type=event_type,
        plan=plan,
        operation=second,
    ) is True
    db.commit()

    configure_cleanup_audit_attempt(plan, second, attempt=1)
    db.add_all((plan, second))
    assert migration._audit(
        db,
        event_type=event_type,
        plan=plan,
        operation=second,
    ) is True
    db.commit()
    configure_cleanup_audit_attempt(plan, second, attempt=2)
    db.add_all((plan, second))
    assert migration._audit(
        db,
        event_type=event_type,
        plan=plan,
        operation=second,
    ) is True
    db.commit()

    _fingerprint, metadata = migration._migration_audit_context(
        event_type=event_type,
        plan=plan,
        operation=second,
    )
    exact_event = (
        db.query(AuditEvent)
        .filter(
            AuditEvent.event_type == event_type,
            AuditEvent.target_id == plan.id,
            AuditEvent.event_metadata[
                "migration_transition_fingerprint"
            ].as_string()
            == metadata["migration_transition_fingerprint"],
        )
        .one()
    )
    clone_exact_audit_event(db, exact_event)
    count_with_preexisting_duplicate = (
        db.query(AuditEvent).filter(AuditEvent.event_type == event_type).count()
    )
    assert migration._audit(
        db,
        event_type=event_type,
        plan=plan,
        operation=second,
    ) is False
    db.commit()
    assert (
        db.query(AuditEvent).filter(AuditEvent.event_type == event_type).count()
        == count_with_preexisting_duplicate
    )
