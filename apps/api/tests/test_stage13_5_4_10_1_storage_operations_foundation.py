import json
import sys
import threading
import time
from contextlib import nullcontext
from itertools import product
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.models.storage_operation import StorageOperation, StorageWorkerLease, StorageWorkSignal
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services import automatic_retention, storage_monitoring, storage_operations_foundation, system_runtime_status
from app.services import archive_root_activation
from app.services import storage_operation_conflicts
from app.routers import storage as storage_router
from app.services.schema_migrations import (
    MIGRATION_SOURCE,
    PRODUCTION_MIGRATIONS,
    STAGE4101_TABLES,
    STAGE4101_STORAGE_FOUNDATION_MIGRATION,
    STAGE41011_OPERATION_LINEAGE_MIGRATION,
    STAGE4102_RETENTION_MIGRATION,
    execute_migration_plan,
    validate_schema_migrations_pre_bootstrap,
)
from app.services.schema_versioning import (
    CURRENT_BASELINE_ID,
    CURRENT_SCHEMA_VERSION,
    CURRENT_STATE_ID,
    SchemaVersionBlocked,
)
from app.services.storage_operation_conflicts import (
    EXACT_ITEM_TYPES,
    OPERATION_TYPES,
    ROOT_EXCLUSIVE_TYPES,
    StorageOperationConflict,
    active_recorder_write_guard,
    active_write_conflict,
    claim_operation_with_conflicts,
    operations_conflict,
    reclaim_operation_with_conflicts,
    scope_with_physical_volumes,
    terminal_replay_result,
)
from app.services.storage_operations_foundation import (
    ACTIVE_SUMMARY_LIMIT,
    MAX_RETRIES_PER_PARENT,
    MAX_RETRY_DEPTH,
    OPERATION_PROGRESS_MAX_BYTES,
    OperationHeartbeatController,
    RECENT_SUMMARY_LIMIT,
    TERMINAL_HISTORY_DAYS,
    TERMINAL_HISTORY_MAX_ROWS,
    StorageOperationContractError,
    StorageOperationLeaseLost,
    acknowledge_work_signal,
    acquire_worker_lease,
    canonical_operation_scope,
    canonical_work_signal_scope_key,
    claim_operation,
    claim_work_signal,
    cleanup_terminal_operations,
    create_operation,
    finish_operation,
    heartbeat_operation,
    heartbeat_work_signal,
    normalize_operation_scope,
    operation_cancel_requested,
    operation_summaries,
    public_operation_summary,
    publish_work_signal,
    reclaim_operation,
    release_worker_lease,
    renew_worker_lease,
    request_operation_cancel,
    stage_operation_terminal,
    work_signal_scope_key,
)


def _session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture
def stage4101(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = _session(engine)
    archive = tmp_path / "archive"
    (archive / "kmvms" / "recordings").mkdir(parents=True)
    monkeypatch.setattr(settings, "storage_root", str(archive))
    monkeypatch.setattr(settings, "storage_previews", str(tmp_path / "previews"))
    monkeypatch.setattr(settings, "storage_exports", str(tmp_path / "exports"))

    owner = User(username="stage4101_owner", full_name="Owner", password_hash="test", role="owner", is_active=True)
    other = User(username="stage4101_other", full_name="Other", password_hash="test", role="owner", is_active=True)
    root = ArchiveRoot(
        id="stage4101-root",
        label="Stage 4.10.1",
        root_path=str(archive),
        storage_namespace="kmvms/recordings",
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage4101-volume",
    )
    camera = Camera(
        name="Stage 4.10.1 Camera",
        storage_folder_name="stage4101-camera",
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
    )
    system = SystemSettings(
        system_initialized=True,
        system_name="KM VMS",
        timezone="UTC",
        language="ru",
        storage_path=str(archive),
        recording_format="mkv",
        auto_free_space_cleanup_enabled=False,
        recording_suspended_by_low_disk=False,
    )
    db.add_all([owner, other, root, camera, system])
    db.commit()
    for row in (owner, other, root, camera):
        db.refresh(row)
    try:
        yield {
            "engine": engine,
            "db": db,
            "owner": owner,
            "other": other,
            "root": root,
            "camera": camera,
            "archive": archive,
        }
    finally:
        db.close()
        engine.dispose()


def _segment(camera, root, index, *, status="finalized", reconciliation_status=None, checked_at=None):
    now = datetime.utcnow()
    return RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=f"kmvms/recordings/{camera.id}/{index}.mkv",
        relative_path=f"kmvms/recordings/{camera.id}/{index}.mkv",
        started_at=now,
        ended_at=now if status == "finalized" else None,
        finalized_at=now if status == "finalized" else None,
        duration_sec=10,
        size_bytes=1024,
        status=status,
        ownership="KM VMS",
        source="recorder",
        archive_root_id=root.id,
        archive_root_resolution_status="resolved",
        archive_root_resolved_at=now,
        storage_namespace="kmvms/recordings",
        container_format="mkv",
        file_extension=".mkv",
        reconciliation_status=reconciliation_status,
        reconciliation_checked_at=checked_at,
    )


def _scope(*, root="root-a", camera=1, segment=1, volume="volume-a", global_scope=False):
    return {
        "global": global_scope,
        "physical_volume_ids": [volume] if volume else [],
        "root_ids": [root] if root else [],
        "camera_ids": [camera] if camera is not None else [],
        "segment_ids": [segment] if segment is not None else [],
    }


def test_lightweight_status_is_read_only_and_does_not_call_heavy_paths(stage4101, monkeypatch):
    db = stage4101["db"]
    db.add(_segment(stage4101["camera"], stage4101["root"], 1))
    db.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("heavy recurring status path was called")

    monkeypatch.setattr(storage_monitoring, "migration_preview", forbidden)
    monkeypatch.setattr(storage_monitoring, "_observe_namespace", forbidden)
    monkeypatch.setattr(storage_monitoring, "_safe_stat_segment", forbidden)
    flushes = []

    def before_flush(*_args):
        flushes.append(True)

    event.listen(db, "before_flush", before_flush)
    audit_before = db.query(AuditEvent).count()
    settings_before = db.query(SystemSettings).count()
    first = storage_monitoring.build_lightweight_storage_monitoring_summary(db)
    second = storage_monitoring.build_lightweight_storage_monitoring_summary(db)
    event.remove(db, "before_flush", before_flush)

    assert flushes == []
    assert not db.new and not db.dirty and not db.deleted
    assert db.query(AuditEvent).count() == audit_before
    assert db.query(SystemSettings).count() == settings_before
    assert "migration_preview" not in first
    assert "migration_preview" not in first["storage_operations"]
    assert first["namespace_observations"] is None
    assert first["reconciliation_summary"]["evidence_status"] == "not_checked"
    assert first["reconciliation_summary"]["status"] == "not_run"
    assert second["owned_archive"]["kmvms_owned_segments_count"] == 1


def test_lightweight_status_query_count_and_payload_do_not_scale_with_segments(stage4101):
    db = stage4101["db"]
    engine = stage4101["engine"]
    camera = stage4101["camera"]
    root = stage4101["root"]
    db.add(_segment(camera, root, 1))
    db.commit()

    statements = []
    loaded_segments = []

    def before_cursor(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    def loaded(_target, _context):
        loaded_segments.append(True)

    event.listen(engine, "before_cursor_execute", before_cursor)
    event.listen(RecordingSegment, "load", loaded)
    statements.clear()
    small = storage_monitoring.build_lightweight_storage_monitoring_summary(db)
    small_count = len(statements)
    small_size = len(json.dumps(small, ensure_ascii=False, default=str).encode("utf-8"))

    db.bulk_save_objects([_segment(camera, root, index) for index in range(2, 2002)])
    db.commit()
    db.expunge_all()
    statements.clear()
    loaded_segments.clear()
    large = storage_monitoring.build_lightweight_storage_monitoring_summary(db)
    large_count = len(statements)
    large_size = len(json.dumps(large, ensure_ascii=False, default=str).encode("utf-8"))
    event.remove(engine, "before_cursor_execute", before_cursor)
    event.remove(RecordingSegment, "load", loaded)

    assert loaded_segments == []
    assert large_count == small_count
    assert large["owned_archive"]["kmvms_owned_segments_count"] == 2001
    assert large_size <= small_size + 512
    assert large_size < 100_000


def test_system_status_reuses_lightweight_owner_and_preserves_unknown(stage4101, monkeypatch):
    calls = []
    lightweight = storage_monitoring.build_lightweight_storage_monitoring_summary(stage4101["db"])

    def supplied(_db):
        calls.append(True)
        return lightweight

    monkeypatch.setattr(system_runtime_status, "build_lightweight_storage_monitoring_summary", supplied)
    payload = system_runtime_status.build_operator_runtime_status(stage4101["db"])

    assert calls == [True]
    assert payload["domains"]["reconciliation"]["severity"] == "unknown"
    assert payload["domains"]["reconciliation"]["evidence_status"] in {"missing", "metadata_only"}


def test_active_recording_is_not_a_storage_or_migration_warning(stage4101):
    db = stage4101["db"]
    camera = stage4101["camera"]
    root = stage4101["root"]
    db.add(RecordingJob(id="stage4101-active", camera_id=camera.id, state="recording", started_at=datetime.utcnow()))
    db.add(_segment(camera, root, 1, status="writing"))
    db.commit()

    summary = storage_monitoring.build_lightweight_storage_monitoring_summary(db)

    assert summary["status"] == "available"
    assert "migration_preview" not in summary
    assert not any("migration" in str(item).lower() for item in summary.get("warnings") or [])


def test_staged_terminal_update_obeys_caller_transaction_boundary(stage4101):
    db = stage4101["db"]
    claimed = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(),
        request_identity={"request": "staged-terminal"},
        actor=stage4101["owner"],
        operation_id="stage4101-staged-terminal",
        idempotency_key="stage4101-staged-terminal",
        owner_instance_id="stage4101-staged-terminal-worker",
    )
    handle = claimed["handle"]
    stage_operation_terminal(
        db,
        handle,
        status="completed",
        result={"status": "completed", "updated_count": 1},
    )
    db.rollback()
    db.expire_all()
    assert db.get(StorageOperation, handle.operation_id).status == "running"
    assert db.query(AuditEvent).filter(
        AuditEvent.event_type == "storage_operation.finished",
        AuditEvent.target_id == handle.operation_id,
    ).count() == 0


def test_operation_create_claim_heartbeat_cancel_finish_and_restart(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    scope = _scope()
    queued = create_operation(
        db,
        operation_type="retention_run",
        scope=scope,
        request_identity={"request": 1},
        actor=owner,
        operation_id="stage4101-lifecycle",
        idempotency_key="stage4101-lifecycle",
        cancel_allowed=True,
    )
    assert queued["state"] == "queued"

    claimed = claim_operation(
        db,
        operation_type="retention_run",
        scope=scope,
        request_identity={"request": 1},
        actor=owner,
        operation_id="stage4101-lifecycle",
        idempotency_key="stage4101-lifecycle",
        owner_instance_id="worker-a",
        cancel_allowed=True,
    )
    handle = claimed["handle"]
    heartbeat = heartbeat_operation(db, handle, progress={"planned_count": 3, "completed_count": 1})
    assert heartbeat["progress"]["completed_count"] == 1
    cancelled = request_operation_cancel(db, handle.operation_id, actor=owner)
    assert cancelled["status"] == "cancel_requested"
    assert operation_cancel_requested(db, handle) is True
    terminal = finish_operation(
        db,
        handle,
        status="cancelled",
        result={"status": "cancelled", "planned_count": 3},
        progress={"planned_count": 3, "completed_count": 1},
        reason_code="operator_cancelled",
    )
    assert terminal["status"] == "cancelled"

    replacement = _session(stage4101["engine"])
    replay = claim_operation(
        replacement,
        operation_type="retention_run",
        scope=scope,
        request_identity={"request": 1},
        actor=replacement.get(User, owner.id),
        operation_id="stage4101-lifecycle",
        idempotency_key="stage4101-lifecycle",
        owner_instance_id="worker-b",
    )
    assert replay["state"] == "terminal"
    assert replay["operation"]["status"] == "cancelled"
    replacement.close()


def test_actor_binding_terminal_immutability_and_retry_parent(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    other = stage4101["other"]
    scope = _scope()
    first = claim_operation(
        db,
        operation_type="manual_single_delete",
        scope=scope,
        request_identity={"segment": 1},
        actor=owner,
        operation_id="stage4101-parent",
        idempotency_key="stage4101-parent",
        owner_instance_id="worker-a",
    )
    finish_operation(db, first["handle"], status="failed", result={"status": "failed"}, reason_code="test_failure", retry_allowed=True)
    with pytest.raises(StorageOperationLeaseLost):
        finish_operation(db, first["handle"], status="completed", result={"status": "completed"})
    with pytest.raises(StorageOperationContractError, match="operation_identity_mismatch"):
        claim_operation(
            db,
            operation_type="manual_single_delete",
            scope=scope,
            request_identity={"segment": 1},
            actor=other,
            operation_id="stage4101-parent",
            idempotency_key="stage4101-parent",
            owner_instance_id="worker-b",
        )
    retry = claim_operation(
        db,
        operation_type="manual_single_delete",
        scope=scope,
        request_identity={"segment": 1, "retry": 1},
        actor=owner,
        operation_id="stage4101-retry",
        idempotency_key="stage4101-retry",
        owner_instance_id="worker-c",
        parent_operation_id="stage4101-parent",
    )
    assert retry["state"] == "claimed"
    finish_operation(db, retry["handle"], status="completed", result={"status": "completed"})


def test_stale_takeover_fences_previous_operation_owner(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    kwargs = {
        "operation_type": "retention_run",
        "scope": _scope(),
        "request_identity": {"run": "takeover"},
        "actor": owner,
        "operation_id": "stage4101-takeover",
        "idempotency_key": "stage4101-takeover",
    }
    old = claim_operation(db, owner_instance_id="old", lease_seconds=5, **kwargs)["handle"]
    row = db.get(StorageOperation, old.operation_id)
    row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.add(row)
    db.commit()
    interrupted = claim_operation(db, owner_instance_id="new", **kwargs)
    assert interrupted["state"] == "interrupted"
    new = reclaim_operation(
        db,
        operation_id="stage4101-takeover",
        operation_type="retention_run",
        request_identity={"run": "takeover"},
        idempotency_key="stage4101-takeover",
        owner_instance_id="new",
    )["handle"]

    assert new.fencing_token > old.fencing_token
    with pytest.raises(StorageOperationLeaseLost):
        heartbeat_operation(db, old)
    with pytest.raises(StorageOperationLeaseLost):
        finish_operation(db, old, status="completed", result={"status": "completed"})
    finish_operation(db, new, status="completed", result={"status": "completed"})


def test_operation_heartbeat_controller_uses_independent_transaction(stage4101):
    db = stage4101["db"]
    claim = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(),
        request_identity={"run": "heartbeat-controller"},
        actor=stage4101["owner"],
        operation_id="stage4101-heartbeat-controller",
        idempotency_key="stage4101-heartbeat-controller",
        owner_instance_id="worker-a",
        lease_seconds=5,
    )
    handle = claim["handle"]
    before = db.get(StorageOperation, handle.operation_id).lease_expires_at
    controller = OperationHeartbeatController(stage4101["engine"], handle, interval_seconds=1)
    controller._last_heartbeat -= 2
    controller.touch()
    db.expire_all()
    after = db.get(StorageOperation, handle.operation_id).lease_expires_at

    assert after > before
    finish_operation(db, handle, status="completed", result={"status": "completed"})


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"owner_token": "secret"}, "operation_payload_sensitive_key"),
        ({"path": "/Volume3/private"}, "operation_payload_absolute_path_forbidden"),
        ({"value": "x" * (OPERATION_PROGRESS_MAX_BYTES + 1)}, "operation_payload_string_too_large"),
        ({"value": float("nan")}, "operation_payload_number_invalid"),
    ],
)
def test_operation_payload_bounds_fail_closed(stage4101, payload, error):
    db = stage4101["db"]
    claim = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(),
        request_identity={"payload": error},
        actor=stage4101["owner"],
        operation_id=f"stage4101-bounds-{abs(hash(error))}",
        idempotency_key=f"bounds-{abs(hash(error))}",
        owner_instance_id="bounds",
    )
    with pytest.raises(StorageOperationContractError, match=error):
        heartbeat_operation(db, claim["handle"], progress=payload)


def test_terminal_result_bound_rejects_oversize_without_exposing_owner(stage4101):
    db = stage4101["db"]
    claim = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(),
        request_identity={"result": "bound"},
        actor=stage4101["owner"],
        operation_id="stage4101-result-bound",
        idempotency_key="stage4101-result-bound",
        owner_instance_id="result-bound",
    )
    with pytest.raises(StorageOperationContractError):
        finish_operation(db, claim["handle"], status="completed", result={"value": "x" * 9000})
    summary = heartbeat_operation(db, claim["handle"], progress={"completed_count": 0})
    assert "owner_token" not in json.dumps(summary)
    assert "fencing_token" not in summary
    finish_operation(db, claim["handle"], status="completed", result={"status": "completed"})


def test_operation_history_is_age_and_count_bounded_without_deleting_active(stage4101):
    db = stage4101["db"]
    now = datetime.utcnow()
    rows = []
    for index in range(TERMINAL_HISTORY_MAX_ROWS + 5):
        finished = now - timedelta(days=TERMINAL_HISTORY_DAYS + 1) if index == 0 else now - timedelta(seconds=index)
        rows.append(
            StorageOperation(
                id=f"history-{index}",
                operation_type="retention_run",
                actor_kind="system",
                actor_key="system:test",
                system_owner="test",
                idempotency_key=f"history-{index}",
                request_fingerprint=f"{index:064x}",
                status="completed",
                scope=normalize_operation_scope(_scope(segment=index + 1)),
                progress={},
                result={"status": "completed"},
                fencing_token=1,
                revision=2,
                queued_at=finished,
                started_at=finished,
                heartbeat_at=finished,
                finished_at=finished,
                created_at=finished,
                updated_at=finished,
            )
        )
    active = StorageOperation(
        id="history-active",
        operation_type="retention_run",
        actor_kind="system",
        actor_key="system:test",
        system_owner="test",
        idempotency_key="history-active",
        request_fingerprint="a" * 64,
        status="running",
        scope=normalize_operation_scope(_scope(segment=9999)),
        progress={},
        fencing_token=1,
        revision=2,
        owner_token_hash="hash",
        owner_instance_id="test",
        lease_expires_at=now + timedelta(minutes=3),
        queued_at=now,
        started_at=now,
        heartbeat_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add_all(rows + [active])
    db.commit()

    deleted = cleanup_terminal_operations(db, now=now)
    terminal_rows = db.query(StorageOperation).filter(StorageOperation.status == "completed").all()
    summaries = operation_summaries(db)

    assert deleted >= 5
    assert len(terminal_rows) <= TERMINAL_HISTORY_MAX_ROWS
    assert all(row.finished_at >= now - timedelta(days=TERMINAL_HISTORY_DAYS) for row in terminal_rows)
    assert db.get(StorageOperation, "history-active") is not None
    assert len(summaries["active"]) <= ACTIVE_SUMMARY_LIMIT
    assert len(summaries["recent"]) <= RECENT_SUMMARY_LIMIT


def test_worker_leader_stale_takeover_and_old_fence_denial(stage4101):
    engine = stage4101["engine"]
    first_db = _session(engine)
    second_db = _session(engine)
    old = acquire_worker_lease(first_db, worker_key="retention-leader", owner_instance_id="one", lease_seconds=5)
    assert old is not None
    assert acquire_worker_lease(second_db, worker_key="retention-leader", owner_instance_id="two") is None
    row = first_db.get(StorageWorkerLease, "retention-leader")
    row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    first_db.add(row)
    first_db.commit()
    new = acquire_worker_lease(second_db, worker_key="retention-leader", owner_instance_id="two")
    assert new is not None and new.fencing_token > old.fencing_token
    with pytest.raises(StorageOperationLeaseLost):
        renew_worker_lease(first_db, old)
    assert release_worker_lease(first_db, old) is False
    renew_worker_lease(second_db, new)
    assert release_worker_lease(second_db, new) is True
    first_db.close()
    second_db.close()


def test_automatic_retention_db_failure_fails_closed(stage4101, monkeypatch):
    calls = []

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(automatic_retention, "acquire_worker_lease", unavailable)
    monkeypatch.setattr(automatic_retention, "run_retention_signal_generation", lambda *_a, **_k: calls.append("retention"))
    monkeypatch.setattr(automatic_retention, "run_auto_free_pressure_groups", lambda *_a, **_k: calls.append("auto_free"))
    with pytest.raises(RuntimeError, match="database unavailable"):
        automatic_retention.run_automatic_retention_cycle()
    assert calls == []


def test_work_signal_coalesces_running_arrivals_and_fences_stale_owner(stage4101):
    engine = stage4101["engine"]
    first_db = _session(engine)
    second_db = _session(engine)
    scope = _scope(segment=None)
    first = publish_work_signal(first_db, signal_type="retention_evaluate", scope=scope, watermark=5)
    scope_key = first["scope_key"]
    old = claim_work_signal(first_db, signal_type="retention_evaluate", scope_key=scope_key, owner_instance_id="one", lease_seconds=5)
    assert old is not None and old.claimed_watermark == 5
    heartbeat_work_signal(first_db, old)
    publish_work_signal(second_db, signal_type="retention_evaluate", scope=scope, watermark=9)
    after_ack = acknowledge_work_signal(first_db, old)
    assert after_ack["status"] == "pending"
    assert after_ack["requested_watermark"] == 9
    assert after_ack["consumed_watermark"] == 5
    new = claim_work_signal(second_db, signal_type="retention_evaluate", scope_key=scope_key, owner_instance_id="two")
    assert new is not None and new.claimed_watermark == 9
    with pytest.raises(StorageOperationLeaseLost):
        acknowledge_work_signal(first_db, old)
    final = acknowledge_work_signal(second_db, new)
    assert final["status"] == "idle"
    assert final["consumed_watermark"] == 9
    assert second_db.query(StorageWorkSignal).count() == 1
    first_db.close()
    second_db.close()


def test_work_signal_scope_and_rows_are_hard_bounded(stage4101, monkeypatch):
    db = stage4101["db"]
    with pytest.raises(StorageOperationContractError, match="work_signal_segment_scope_forbidden"):
        publish_work_signal(db, signal_type="retention_evaluate", scope=_scope(segment=1), watermark=1)
    with pytest.raises(StorageOperationContractError, match="work_signal_type_unsupported"):
        publish_work_signal(db, signal_type="unapproved_signal", scope=_scope(segment=None), watermark=1)

    monkeypatch.setattr(storage_operations_foundation, "WORK_SIGNAL_MAX_ROWS", 2)
    first_scope = _scope(root="root-a", camera=1, segment=None, volume="volume-a")
    same_scope_reordered = {
        "camera_ids": [1],
        "root_ids": ["root-a"],
        "physical_volume_ids": ["volume-a"],
        "segment_ids": [],
        "global": False,
    }
    first = publish_work_signal(db, signal_type="retention_evaluate", scope=first_scope, watermark=1)
    repeated = publish_work_signal(db, signal_type="retention_evaluate", scope=same_scope_reordered, watermark=2)
    assert repeated["scope_key"] == first["scope_key"]
    assert db.query(StorageWorkSignal).count() == 1
    publish_work_signal(
        db,
        signal_type="retention_evaluate",
        scope=_scope(root="root-b", camera=2, segment=None, volume="volume-b"),
        watermark=1,
    )
    with pytest.raises(StorageOperationContractError, match="work_signal_row_limit_reached"):
        publish_work_signal(
            db,
            signal_type="retention_evaluate",
            scope=_scope(root="root-c", camera=3, segment=None, volume="volume-c"),
            watermark=1,
        )
    assert db.query(StorageWorkSignal).count() == 2


@pytest.mark.parametrize(
    "left_type,left_scope,right_type,right_scope,expected",
    [
        ("manual_single_delete", _scope(segment=1), "manual_bulk_delete", _scope(segment=2), False),
        ("manual_single_delete", _scope(segment=1), "manual_bulk_delete", _scope(segment=1), True),
        ("camera_delete_with_files", _scope(camera=1, segment=None), "manual_single_delete", _scope(camera=2, segment=2), False),
        ("camera_delete_with_files", _scope(camera=1, segment=None), "manual_single_delete", _scope(camera=1, segment=2), True),
        ("archive_root_delete", _scope(root="root-a", volume="volume-a", segment=None), "manual_single_delete", _scope(root="root-a", volume="volume-a"), True),
        ("archive_root_delete", _scope(root="root-a", volume="volume-a", segment=None), "manual_single_delete", _scope(root="root-b", volume="volume-b"), False),
        ("archive_root_activation", _scope(global_scope=True), "manual_single_delete", _scope(), True),
        ("manual_delete_all", _scope(global_scope=True), "retention_run", _scope(), True),
    ],
)
def test_conflict_matrix_policy(left_type, left_scope, right_type, right_scope, expected):
    left = normalize_operation_scope(left_scope)
    right = normalize_operation_scope(right_scope)
    assert operations_conflict(left_type, left, right_type, right) is expected
    assert operations_conflict(right_type, right, left_type, left) is expected


@pytest.mark.parametrize(
    "scenario,left_scope,right_scope",
    [
        ("same_scope", _scope(), _scope()),
        ("fully_disjoint", _scope(), _scope(root="root-b", camera=2, segment=2, volume="volume-b")),
        ("same_root", _scope(), _scope(root="root-a", camera=2, segment=2, volume="volume-b")),
        ("same_volume", _scope(), _scope(root="root-b", camera=2, segment=2, volume="volume-a")),
        ("same_camera", _scope(), _scope(root="root-b", camera=1, segment=2, volume="volume-b")),
    ],
)
def test_conflict_matrix_covers_every_operation_type_pair(scenario, left_scope, right_scope):
    left_normalized = normalize_operation_scope(left_scope)
    right_normalized = normalize_operation_scope(right_scope)
    for left_type, right_type in product(sorted(OPERATION_TYPES), repeat=2):
        if scenario == "same_scope":
            expected = True
        elif scenario == "fully_disjoint":
            expected = False
        elif scenario in {"same_root", "same_volume"}:
            expected = left_type in ROOT_EXCLUSIVE_TYPES or right_type in ROOT_EXCLUSIVE_TYPES
        else:
            expected = bool(
                left_type not in ROOT_EXCLUSIVE_TYPES
                and right_type not in ROOT_EXCLUSIVE_TYPES
                and not (left_type in EXACT_ITEM_TYPES and right_type in EXACT_ITEM_TYPES)
            )
        actual = operations_conflict(left_type, left_normalized, right_type, right_normalized)
        assert actual is expected, (scenario, left_type, right_type, expected, actual)


def test_global_conflict_scopes_block_every_operation_type_pair():
    global_scope = normalize_operation_scope(_scope(global_scope=True))
    regular_scope = normalize_operation_scope(_scope())
    for left_type, right_type in product(sorted(OPERATION_TYPES), repeat=2):
        assert operations_conflict(left_type, global_scope, right_type, regular_scope) is True
        assert operations_conflict(left_type, regular_scope, right_type, global_scope) is True


def test_conflict_claims_allow_disjoint_exact_scope_and_block_overlap(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    real_scope = {"root": stage4101["root"].id, "volume": None}
    first = claim_operation_with_conflicts(
        db,
        operation_type="manual_single_delete",
        scope=_scope(segment=1, **real_scope),
        request_identity={"segment": 1},
        actor=owner,
        operation_id="conflict-first",
        idempotency_key="conflict-first",
        owner_instance_id="first",
    )
    second = claim_operation_with_conflicts(
        db,
        operation_type="manual_bulk_delete",
        scope=_scope(segment=2, **real_scope),
        request_identity={"segment": 2},
        actor=owner,
        operation_id="conflict-second",
        idempotency_key="conflict-second",
        owner_instance_id="second",
    )
    with pytest.raises(StorageOperationConflict, match="storage_operation_scope_conflict"):
        claim_operation_with_conflicts(
            db,
            operation_type="retention_run",
            scope=_scope(segment=1, **real_scope),
            request_identity={"segment": 1, "retention": True},
            system_owner="retention",
            operation_id="conflict-third",
            idempotency_key="conflict-third",
            owner_instance_id="third",
        )
    assert db.query(AuditEvent).filter(AuditEvent.event_type == "storage_operation.conflict").count() == 1
    finish_operation(db, first["handle"], status="completed", result={"status": "completed"})
    finish_operation(db, second["handle"], status="failed", result={"status": "failed"})
    retry = claim_operation_with_conflicts(
        db,
        operation_type="retention_run",
        scope=_scope(segment=1, **real_scope),
        request_identity={"segment": 1, "retention": True},
        system_owner="retention",
        operation_id="conflict-fourth",
        idempotency_key="conflict-fourth",
        owner_instance_id="fourth",
    )
    assert retry["state"] == "claimed"
    finish_operation(db, retry["handle"], status="cancelled", result={"status": "cancelled"})


def test_common_conflict_model_uses_active_recorder_write_guards(stage4101):
    db = stage4101["db"]
    camera = stage4101["camera"]
    root = stage4101["root"]
    job = RecordingJob(id="guard-job", camera_id=camera.id, state="recording", started_at=datetime.utcnow())
    writing = _segment(camera, root, 1, status="writing")
    db.add_all([job, writing])
    db.commit()
    guard = active_recorder_write_guard(db)
    exact_other = normalize_operation_scope(_scope(root=root.id, camera=camera.id, segment=999, volume=None))
    exact_current = normalize_operation_scope(_scope(root=root.id, camera=camera.id, segment=writing.id, volume=None))
    camera_scope = normalize_operation_scope(_scope(root=root.id, camera=camera.id, segment=None, volume=None))
    root_scope = normalize_operation_scope(_scope(root=root.id, camera=None, segment=None, volume=None))

    assert active_write_conflict("manual_single_delete", exact_other, guard) is None
    assert active_write_conflict("integrity_metadata_repair", exact_current, guard)["conflict_scope"] == "segment"
    assert active_write_conflict("manual_delete_by_camera", camera_scope, guard) is None
    assert active_write_conflict("camera_delete_with_files", camera_scope, guard)["conflict_scope"] == "camera"
    assert active_write_conflict("archive_root_delete", root_scope, guard)["conflict_scope"] == "archive_root"
    assert active_write_conflict("archive_root_activation", {"global": True}, guard) is None


def test_archive_root_activation_outer_claim_precedes_service_and_stays_running(stage4101, tmp_path, monkeypatch):
    db = stage4101["db"]
    target_path = tmp_path / "activation-target"
    (target_path / "kmvms" / "recordings").mkdir(parents=True)
    target = ArchiveRoot(
        id="stage4101-target",
        label="Target",
        root_path=str(target_path),
        storage_namespace="kmvms/recordings",
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage4101-target-volume",
    )
    db.add(target)
    db.commit()
    observed = {}

    def request_stub(_db, *, root, actor, recovery, operation_id, outer_handle):
        row = _db.get(StorageOperation, operation_id)
        observed.update({"root_id": root.id, "actor_id": actor.id, "handle": outer_handle, "status": row.status})
        return {"status": "queued", "operation_id": operation_id}

    monkeypatch.setattr(storage_router, "read_pending_archive_root_activation", lambda: None)
    monkeypatch.setattr(storage_router, "request_archive_root_activation", request_stub)
    result = storage_router.activate_archive_root(
        target.id,
        storage_router.ArchiveRootActivateRequest(confirm=True),
        db=db,
        current_user=stage4101["owner"],
    )

    assert result["status"] == "queued"
    assert observed["status"] == "running"
    assert observed["root_id"] == target.id
    assert observed["actor_id"] == stage4101["owner"].id
    assert db.get(StorageOperation, observed["handle"].operation_id).status == "running"
    finish_operation(db, observed["handle"], status="cancelled", result={"status": "cancelled"})


def test_activation_worker_terminalizes_outer_only_after_inner_terminal_state(stage4101, monkeypatch):
    db = stage4101["db"]
    claim = claim_operation(
        db,
        operation_type="archive_root_activation",
        scope={"global": True, "root_ids": [stage4101["root"].id]},
        request_identity={"activation": "lifetime"},
        actor=stage4101["owner"],
        operation_id="stage4101-activation-lifetime",
        idempotency_key="stage4101-activation-lifetime",
        owner_instance_id="activation-worker",
    )
    outer_handle = claim["handle"]
    state = {
        "operation_id": outer_handle.operation_id,
        "status": "running",
        "actor_user_id": stage4101["owner"].id,
        "affected_camera_ids": [stage4101["camera"].id],
        "restored_camera_ids": [],
        "camera_restore_failed_ids": [],
        "worker_recovery_count": 0,
    }
    inner_observations = []

    class FakeWorkerSession:
        def __init__(self, _lease, supplied_outer):
            self.outer_handle = supplied_outer

        def start(self):
            return None

        def stop(self):
            return None

        def assert_owned(self):
            return None

    def inner_terminal(inner_db, _operation_id, *, worker_session):
        inner_observations.append(inner_db.get(StorageOperation, outer_handle.operation_id).status)
        assert worker_session.outer_handle == outer_handle
        return {
            **state,
            "status": "completed",
            "restored_camera_ids": [stage4101["camera"].id],
        }

    Session = sessionmaker(bind=stage4101["engine"], autoflush=False, autocommit=False)
    monkeypatch.setattr(archive_root_activation, "SessionLocal", Session)
    monkeypatch.setattr(archive_root_activation, "_claim_worker_lease", lambda _operation_id: object())
    monkeypatch.setattr(archive_root_activation, "_release_worker_lease", lambda _lease: True)
    monkeypatch.setattr(archive_root_activation, "WorkerLeaseSession", FakeWorkerSession)
    monkeypatch.setattr(archive_root_activation, "read_pending_archive_root_activation", lambda: dict(state))
    monkeypatch.setattr(archive_root_activation, "_run_activation_operation", inner_terminal)

    archive_root_activation._closeout_worker(outer_handle.operation_id, outer_handle=outer_handle)
    db.expire_all()

    assert inner_observations == ["running"]
    assert db.get(StorageOperation, outer_handle.operation_id).status == "completed"


def test_activation_recovery_reclaims_original_identity_after_actor_deletion(stage4101):
    db = stage4101["db"]
    recovery_actor = User(
        username="stage4101_recovery_actor",
        full_name="Recovery Actor",
        password_hash="test",
        role="owner",
        is_active=True,
    )
    db.add(recovery_actor)
    db.commit()
    db.refresh(recovery_actor)
    actor_id = recovery_actor.id
    operation_id = "stage4101-activation-recovery"
    request_identity = {
        "operation_id": operation_id,
        "previous_root_id": stage4101["root"].id,
        "target_root_id": "stage4101-target-root",
    }
    original = claim_operation(
        db,
        operation_type="archive_root_activation",
        scope={
            "global": True,
            "root_ids": [stage4101["root"].id, "stage4101-target-root"],
            "camera_ids": [],
            "segment_ids": [],
            "physical_volume_ids": [],
        },
        request_identity=request_identity,
        actor=recovery_actor,
        operation_id=operation_id,
        idempotency_key=operation_id,
        owner_instance_id="original-worker",
        lease_seconds=5,
    )
    row = db.get(StorageOperation, operation_id)
    original_actor_key = row.actor_key
    row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    row.actor_user_id = None
    db.add(row)
    db.commit()
    db.delete(recovery_actor)
    db.commit()

    recovered = archive_root_activation._claim_recovered_storage_outer(
        db,
        {
            **request_identity,
            "actor_user_id": actor_id,
            "affected_camera_ids": [stage4101["camera"].id],
        },
    )

    assert recovered is not None
    assert recovered.fencing_token > original["handle"].fencing_token
    db.expire_all()
    recovered_row = db.get(StorageOperation, operation_id)
    assert recovered_row.actor_key == original_actor_key
    assert recovered_row.actor_user_id is None
    assert recovered_row.scope["camera_ids"] == []
    finish_operation(db, recovered, status="cancelled", result={"status": "cancelled"})


def _seed_schema_v1(db):
    now = datetime.utcnow()
    db.add(
        SchemaVersionState(
            id=CURRENT_STATE_ID,
            schema_version=1,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status="current",
            source=MIGRATION_SOURCE,
            applied_at=now,
        )
    )
    db.add(
        SchemaMigrationHistory(
            migration_id="stage4101_seed_v1",
            previous_version=None,
            target_version=1,
            schema_version=1,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status="current",
            source=MIGRATION_SOURCE,
        )
    )
    db.commit()


def test_schema_clean_install_upgrade_restart_and_prebootstrap_gate():
    assert CURRENT_SCHEMA_VERSION == 4
    fresh = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=fresh)
    fresh_inspector = inspect(fresh)
    assert all(fresh_inspector.has_table(name) for name in ("storage_operations", "storage_worker_leases", "storage_work_signals"))
    fresh.dispose()

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    for table in reversed(STAGE4101_TABLES):
        table.drop(bind=engine, checkfirst=True)
    db = _session(engine)
    _seed_schema_v1(db)
    with pytest.raises(SchemaVersionBlocked):
        validate_schema_migrations_pre_bootstrap(engine)
    first = execute_migration_plan(db, registry=PRODUCTION_MIGRATIONS)
    second = execute_migration_plan(db, registry=PRODUCTION_MIGRATIONS)
    inspector = inspect(engine)

    assert first["executed_migrations"] == [
        STAGE4101_STORAGE_FOUNDATION_MIGRATION.migration_id,
        STAGE41011_OPERATION_LINEAGE_MIGRATION.migration_id,
        STAGE4102_RETENTION_MIGRATION.migration_id,
    ]
    assert second["executed_migrations"] == []
    assert db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == 4
    assert all(inspector.has_table(name) for name in ("storage_operations", "storage_worker_leases", "storage_work_signals"))
    operation_columns = {item["name"] for item in inspector.get_columns("storage_operations")}
    assert {"parent_snapshot", "retry_depth"}.issubset(operation_columns)
    camera_columns = {item["name"] for item in inspector.get_columns("cameras")}
    settings_columns = {item["name"] for item in inspector.get_columns("system_settings")}
    assert "retention_policy_version" in camera_columns
    assert {
        "auto_free_space_acknowledged_terms_version",
        "auto_free_space_acknowledged_at",
        "auto_free_space_acknowledged_by_user_id",
        "low_disk_suspended_physical_volume_id",
        "low_disk_suspended_at",
    }.issubset(settings_columns)
    validate_schema_migrations_pre_bootstrap(engine)
    db.close()
    engine.dispose()


def test_canonical_scope_and_work_signal_key_are_idempotent():
    raw = _scope(volume="physical-volume-a", segment=None)
    canonical = normalize_operation_scope(raw)

    assert canonical["physical_volume_ids"][0].startswith("pv1:")
    assert normalize_operation_scope(canonical) == canonical
    assert canonical_operation_scope(canonical) == canonical
    assert work_signal_scope_key(raw) == canonical_work_signal_scope_key(canonical)

    legacy = {**canonical, "physical_volume_ids": [canonical["physical_volume_ids"][0].split(":", 1)[1]]}
    assert canonical_operation_scope(legacy) == canonical
    assert normalize_operation_scope(legacy) != canonical


def test_stale_recovery_keeps_same_volume_conflict(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    stale = claim_operation_with_conflicts(
        db,
        operation_type="archive_migration_apply",
        scope=_scope(root="root-a", camera=None, segment=None, volume="shared-volume"),
        request_identity={"plan": "stale"},
        actor=owner,
        operation_id="stage41011-stale-volume",
        idempotency_key="stage41011-stale-volume",
        owner_instance_id="stale",
    )
    stale_row = db.get(StorageOperation, stale["handle"].operation_id)
    original_scope = dict(stale_row.scope)
    stale_row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.add(stale_row)
    db.commit()

    active = claim_operation_with_conflicts(
        db,
        operation_type="archive_root_delete",
        scope=_scope(root="root-b", camera=None, segment=None, volume="shared-volume"),
        request_identity={"root": "root-b"},
        actor=owner,
        operation_id="stage41011-active-volume",
        idempotency_key="stage41011-active-volume",
        owner_instance_id="active",
    )

    with pytest.raises(StorageOperationConflict, match="storage_operation_scope_conflict"):
        reclaim_operation_with_conflicts(
            db,
            operation_id=stale["handle"].operation_id,
            operation_type="archive_migration_apply",
            request_identity={"plan": "stale"},
            idempotency_key="stage41011-stale-volume",
            owner_instance_id="recovery",
        )

    db.expire_all()
    assert db.get(StorageOperation, stale["handle"].operation_id).scope == original_scope
    finish_operation(db, active["handle"], status="completed", result={"status": "completed"})


def test_pristine_queue_is_waiting_but_expired_owned_operation_is_interrupted(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    queued = create_operation(
        db,
        operation_type="retention_run",
        scope=_scope(segment=10),
        request_identity={"queue": 1},
        actor=owner,
        operation_id="stage41011-pristine-queue",
        idempotency_key="stage41011-pristine-queue",
    )
    queued_row = db.get(StorageOperation, queued["operation"]["operation_id"])
    queued_row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.add(queued_row)
    db.commit()

    running = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(segment=11),
        request_identity={"run": 1},
        actor=owner,
        operation_id="stage41011-abandoned-run",
        idempotency_key="stage41011-abandoned-run",
        owner_instance_id="old-worker",
    )
    running_row = db.get(StorageOperation, running["handle"].operation_id)
    running_row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.add(running_row)
    db.commit()

    flushes = []
    event.listen(db, "before_flush", lambda *_args: flushes.append(True))
    summaries = operation_summaries(db)

    assert any(item["operation_id"] == queued_row.id and item["status"] == "queued" for item in summaries["active"])
    assert any(item["operation_id"] == running_row.id and item["status"] == "interrupted" for item in summaries["interrupted"])
    assert all(item["operation_id"] != running_row.id for item in summaries["active"])
    assert flushes == []
    assert not db.new and not db.dirty and not db.deleted


def test_terminal_claim_replays_bounded_result_without_new_operation(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    kwargs = {
        "operation_type": "archive_migration_apply",
        "scope": _scope(root=stage4101["root"].id, camera=None, segment=None, volume=None),
        "request_identity": {"plan_id": "stage41011-replay"},
        "actor": owner,
        "idempotency_key": "stage41011-replay",
        "owner_instance_id": "first",
    }
    first = claim_operation_with_conflicts(db, **kwargs)
    finish_operation(
        db,
        first["handle"],
        status="completed",
        result={"status": "completed", "executed_count": 3},
    )
    count_before = db.query(StorageOperation).count()

    replay = claim_operation_with_conflicts(db, **{**kwargs, "owner_instance_id": "second"})

    assert replay["state"] == "terminal"
    assert terminal_replay_result(replay)["executed_count"] == 3
    assert terminal_replay_result(replay)["replayed"] is True
    assert db.query(StorageOperation).count() == count_before


@pytest.mark.parametrize(
    ("terminal_status", "reason_code", "next_action", "retry_mode", "retry_allowed"),
    [
        ("completed", None, None, None, False),
        ("partial", "archive_cleanup_partial", "inspect_result", "refresh", True),
        ("blocked", "marker_mismatch", "inspect_root", None, False),
        ("failed", "storage_operation_internal_failure", "retry_operation", "immediate", True),
        ("cancelled", "storage_operation_cancelled", "close", None, False),
    ],
)
def test_terminal_replay_preserves_public_capability_truth(
    stage4101,
    terminal_status,
    reason_code,
    next_action,
    retry_mode,
    retry_allowed,
):
    db = stage4101["db"]
    owner = stage4101["owner"]
    operation_id = f"stage41012-replay-{terminal_status}"
    kwargs = {
        "operation_type": "archive_migration_apply",
        "scope": _scope(root=stage4101["root"].id, camera=None, segment=None, volume=None),
        "request_identity": {"plan_id": operation_id},
        "actor": owner,
        "operation_id": operation_id,
        "idempotency_key": operation_id,
        "owner_instance_id": "stage41012-first",
    }
    claimed = claim_operation_with_conflicts(db, **kwargs)
    finish_operation(
        db,
        claimed["handle"],
        status=terminal_status,
        result={
            "status": terminal_status,
            "executed_count": 1,
            "owner_instance_id": "must-not-replay",
            "fencing_token": 999,
            "scope": {"global": True},
        },
        reason_code=reason_code,
        next_action=next_action,
        retry_mode=retry_mode,
        retry_allowed=retry_allowed,
    )

    replay = claim_operation_with_conflicts(db, **{**kwargs, "owner_instance_id": "stage41012-replay"})
    result = terminal_replay_result(replay)

    assert result["status"] == terminal_status
    assert result["operation_id"] == claimed["operation"]["operation_id"]
    assert result["reason_code"] == reason_code
    assert result["next_action"] == next_action
    assert result["retry_mode"] == retry_mode
    assert result["retry_allowed"] is retry_allowed
    assert result["cancel_allowed"] is False
    assert result["replayed"] is True
    assert "owner_instance_id" not in result
    assert "fencing_token" not in result
    assert "scope" not in result


def test_public_operation_summary_requires_explicit_database_time(stage4101, monkeypatch):
    db = stage4101["db"]
    owner = stage4101["owner"]
    claimed = claim_operation(
        db,
        operation_type="archive_migration_apply",
        scope=_scope(root=stage4101["root"].id, camera=None, segment=None, volume=None),
        request_identity={"plan_id": "stage41012-db-time"},
        actor=owner,
        idempotency_key="stage41012-db-time",
        owner_instance_id="stage41012-db-time",
        lease_seconds=60,
    )
    row = db.get(StorageOperation, claimed["handle"].operation_id)
    db_now = storage_operations_foundation.database_now(db)

    class ForbiddenProcessClock:
        @classmethod
        def utcnow(cls):
            raise AssertionError("process clock must not decide lease liveness")

    monkeypatch.setattr(storage_operations_foundation, "datetime", ForbiddenProcessClock)

    assert public_operation_summary(row, now=db_now)["status"] == "running"
    assert public_operation_summary(row, now=row.lease_expires_at + timedelta(seconds=1))["status"] == "interrupted"
    with pytest.raises(TypeError):
        public_operation_summary(row)

    source = Path(storage_operations_foundation.__file__).read_text(encoding="utf-8")
    summary_source = source[source.index("def public_operation_summary"):source.index("def operation_summaries")]
    assert "datetime.utcnow" not in summary_source


def test_migration_adapter_terminal_replay_does_not_execute_twice(stage4101, monkeypatch):
    db = stage4101["db"]
    owner = stage4101["owner"]
    calls = []

    def apply_stub(*_args, **_kwargs):
        calls.append(True)
        return {
            "status": "completed",
            "planned_count": 1,
            "executed": [{"segment_id": 1}],
            "failed": [],
            "executed_bytes": 1024,
            "source_preserved": True,
            "cleanup_pending": False,
        }

    monkeypatch.setattr(storage_router, "apply_storage_migration", apply_stub)
    payload = storage_router.MigrationApplyRequest(
        target_root_id=stage4101["root"].id,
        plan_id="stage41011-migration-replay",
        confirm=True,
    )

    first = storage_router.storage_migration_apply(payload, db=db, current_user=owner)
    second = storage_router.storage_migration_apply(payload, db=db, current_user=owner)

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert second["executed_count"] == 1
    assert second["replayed"] is True
    assert calls == [True]


def test_migration_adapter_exception_terminalizes_outer(stage4101, monkeypatch):
    db = stage4101["db"]
    owner = stage4101["owner"]

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(storage_router, "apply_storage_migration", fail)
    payload = storage_router.MigrationApplyRequest(
        target_root_id=stage4101["root"].id,
        plan_id="stage41011-migration-failure",
        confirm=True,
    )

    with pytest.raises(RuntimeError, match="injected migration failure"):
        storage_router.storage_migration_apply(payload, db=db, current_user=owner)

    row = (
        db.query(StorageOperation)
        .filter(StorageOperation.operation_type == "archive_migration_apply")
        .order_by(StorageOperation.created_at.desc())
        .first()
    )
    assert row.status == "failed"
    assert row.lease_expires_at is None


def test_root_delete_terminal_replay_does_not_repeat_mutation(stage4101, tmp_path, monkeypatch):
    db = stage4101["db"]
    owner = stage4101["owner"]
    root_path = tmp_path / "replay-root"
    (root_path / "kmvms" / "recordings").mkdir(parents=True)
    root = ArchiveRoot(
        id="stage41011-root-replay",
        label="Replay root",
        root_path=str(root_path),
        storage_namespace="kmvms/recordings",
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage41011-root-replay-volume",
    )
    db.add(root)
    db.commit()
    calls = []

    def delete_stub(_db, target, *_args, operation_id=None, **_kwargs):
        calls.append(operation_id)
        target.retired_at = datetime.utcnow()
        _db.add(target)
        _db.commit()
        return {
            "status": "completed",
            "operation_id": operation_id,
            "segments_deleted": 2,
            "files_deleted": 2,
            "bytes_freed": 2048,
            "cleanup_status": "completed_removed_empty",
            "root_directory_removed": True,
        }

    monkeypatch.setattr(storage_router, "archive_root_mutation_guard", lambda _purpose: nullcontext())
    monkeypatch.setattr(storage_router, "_delete_inactive_root", delete_stub)
    payload = storage_router.ArchiveRootDeleteRequest(
        confirm=True,
        operation_id="stage41011-root-replay-operation",
    )

    first = storage_router.delete_archive_root(root.id, payload, db=db, current_user=owner)
    second = storage_router.delete_archive_root(root.id, payload, db=db, current_user=owner)

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert second["replayed"] is True
    assert second["cleanup_status"] == "completed_removed_empty"
    assert calls == ["stage41011-root-replay-operation"]
    assert db.query(StorageOperation).filter(StorageOperation.operation_type == "archive_root_delete").count() == 1


def test_root_delete_terminal_replay_is_actor_bound(stage4101, tmp_path, monkeypatch):
    db = stage4101["db"]
    root_path = tmp_path / "actor-bound-root"
    (root_path / "kmvms" / "recordings").mkdir(parents=True)
    root = ArchiveRoot(
        id="stage41011-root-actor-bound",
        label="Actor bound root",
        root_path=str(root_path),
        storage_namespace="kmvms/recordings",
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage41011-root-actor-bound-volume",
    )
    db.add(root)
    db.commit()

    def delete_stub(_db, target, *_args, operation_id=None, **_kwargs):
        target.retired_at = datetime.utcnow()
        _db.add(target)
        _db.commit()
        return {"status": "completed", "operation_id": operation_id}

    monkeypatch.setattr(storage_router, "archive_root_mutation_guard", lambda _purpose: nullcontext())
    monkeypatch.setattr(storage_router, "_delete_inactive_root", delete_stub)
    payload = storage_router.ArchiveRootDeleteRequest(
        confirm=True,
        operation_id="stage41011-root-actor-operation",
    )
    storage_router.delete_archive_root(root.id, payload, db=db, current_user=stage4101["owner"])

    with pytest.raises(storage_router.HTTPException) as exc_info:
        storage_router.delete_archive_root(root.id, payload, db=db, current_user=stage4101["other"])

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["reason_code"] == "storage_operation_identity_mismatch"


def test_reconciliation_terminal_replay_does_not_apply_twice(stage4101, monkeypatch):
    db = stage4101["db"]
    calls = []

    def reconcile_stub(*_args, **_kwargs):
        calls.append(True)
        return {"status": "completed", "total_rows": 3, "updated_count": 2, "failed_count": 0}

    monkeypatch.setattr(storage_router, "reconcile_recordings", reconcile_stub)
    payload = storage_router.ReconciliationRequest(
        mode="apply_safe",
        operation_id="stage41011-reconcile-replay",
    )

    first = storage_router.storage_reconcile(payload, db=db, current_user=stage4101["owner"])
    second = storage_router.storage_reconcile(payload, db=db, current_user=stage4101["owner"])

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert second["replayed"] is True
    assert second["updated_count"] == 2
    assert calls == [True]


def test_inner_persisted_result_becomes_partial_if_outer_closeout_fails(stage4101, monkeypatch):
    db = stage4101["db"]
    monkeypatch.setattr(
        storage_router,
        "reconcile_recordings",
        lambda *_args, **_kwargs: {"status": "completed", "total_rows": 1, "updated_count": 1},
    )

    original_touch = storage_operations_foundation.OperationHeartbeatController.touch

    def fail_forced_touch(self, *, force=False):
        if force:
            raise RuntimeError("injected outer closeout heartbeat failure")
        return original_touch(self, force=force)

    monkeypatch.setattr(storage_operations_foundation.OperationHeartbeatController, "touch", fail_forced_touch)
    payload = storage_router.ReconciliationRequest(
        mode="apply_safe",
        operation_id="stage41011-reconcile-partial",
    )

    with pytest.raises(RuntimeError, match="outer closeout heartbeat failure"):
        storage_router.storage_reconcile(payload, db=db, current_user=stage4101["owner"])

    row = db.get(StorageOperation, "stage41011-reconcile-partial")
    assert row.status == "partial"
    assert row.result["updated_count"] == 1
    assert row.lease_expires_at is None


def test_root_delete_exception_terminalizes_outer(stage4101, tmp_path, monkeypatch):
    db = stage4101["db"]
    owner = stage4101["owner"]
    inactive_path = tmp_path / "inactive-root"
    (inactive_path / "kmvms" / "recordings").mkdir(parents=True)
    root = ArchiveRoot(
        id="stage41011-delete-root",
        label="Delete root",
        root_path=str(inactive_path),
        storage_namespace="kmvms/recordings",
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage41011-delete-volume",
    )
    db.add(root)
    db.commit()

    monkeypatch.setattr(storage_router, "archive_root_mutation_guard", lambda _purpose: nullcontext())
    monkeypatch.setattr(
        storage_router,
        "_delete_inactive_root",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected root delete failure")),
    )

    with pytest.raises(RuntimeError, match="injected root delete failure"):
        storage_router.delete_archive_root(
            root.id,
            storage_router.ArchiveRootDeleteRequest(confirm=True),
            db=db,
            current_user=owner,
        )

    row = (
        db.query(StorageOperation)
        .filter(StorageOperation.operation_type == "archive_root_delete")
        .order_by(StorageOperation.created_at.desc())
        .first()
    )
    assert row.status == "failed"
    assert row.lease_expires_at is None


def test_reconciliation_exception_terminalizes_outer(stage4101, monkeypatch):
    db = stage4101["db"]
    owner = stage4101["owner"]
    monkeypatch.setattr(
        storage_router,
        "reconcile_recordings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected reconciliation failure")),
    )

    with pytest.raises(RuntimeError, match="injected reconciliation failure"):
        storage_router.storage_reconcile(
            storage_router.ReconciliationRequest(mode="apply_safe"),
            db=db,
            current_user=owner,
        )

    row = (
        db.query(StorageOperation)
        .filter(StorageOperation.operation_type == "integrity_metadata_repair")
        .order_by(StorageOperation.created_at.desc())
        .first()
    )
    assert row.status == "failed"
    assert row.lease_expires_at is None


def test_activation_scheduling_exception_terminalizes_outer(stage4101, tmp_path, monkeypatch):
    db = stage4101["db"]
    owner = stage4101["owner"]
    target_path = tmp_path / "activation-failure-target"
    (target_path / "kmvms" / "recordings").mkdir(parents=True)
    target = ArchiveRoot(
        id="stage41011-activation-failure",
        label="Activation failure",
        root_path=str(target_path),
        storage_namespace="kmvms/recordings",
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage41011-activation-volume",
    )
    db.add(target)
    db.commit()
    monkeypatch.setattr(storage_router, "read_pending_archive_root_activation", lambda: None)
    monkeypatch.setattr(
        storage_router,
        "request_archive_root_activation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected scheduling failure")),
    )

    with pytest.raises(RuntimeError, match="injected scheduling failure"):
        storage_router.activate_archive_root(
            target.id,
            storage_router.ArchiveRootActivateRequest(confirm=True),
            db=db,
            current_user=owner,
        )

    row = (
        db.query(StorageOperation)
        .filter(StorageOperation.operation_type == "archive_root_activation")
        .order_by(StorageOperation.created_at.desc())
        .first()
    )
    assert row.status == "failed"
    assert row.lease_expires_at is None


def test_terminal_cleanup_preserves_bounded_parent_snapshot(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    parent = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(segment=20),
        request_identity={"parent": 1},
        actor=owner,
        operation_id="stage41011-parent-cleanup",
        idempotency_key="stage41011-parent-cleanup",
        owner_instance_id="parent",
    )
    finish_operation(db, parent["handle"], status="failed", result={"status": "failed"}, reason_code="test_parent")
    parent_row = db.get(StorageOperation, parent["handle"].operation_id)
    parent_id = str(parent_row.id)
    parent_row.finished_at = datetime.utcnow() - timedelta(days=TERMINAL_HISTORY_DAYS + 1)
    db.add(parent_row)
    db.commit()

    child = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(segment=20),
        request_identity={"child": 1},
        actor=owner,
        operation_id="stage41011-child-cleanup",
        idempotency_key="stage41011-child-cleanup",
        parent_operation_id=parent_row.id,
        owner_instance_id="child",
    )
    finish_operation(db, child["handle"], status="completed", result={"status": "completed"})

    assert cleanup_terminal_operations(db, now=datetime.utcnow()) == 1
    db.expire_all()
    child_row = db.get(StorageOperation, child["handle"].operation_id)
    assert db.get(StorageOperation, parent_id) is None
    assert child_row.parent_operation_id is None
    assert child_row.parent_snapshot["operation_id"] == "stage41011-parent-cleanup"
    assert child_row.retry_depth == 1
    assert cleanup_terminal_operations(db, now=datetime.utcnow()) == 0


def test_retry_lineage_depth_and_per_parent_rows_are_hard_bounded(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    parent = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(segment=30),
        request_identity={"lineage": "root"},
        actor=owner,
        operation_id="stage41011-lineage-root",
        idempotency_key="stage41011-lineage-root",
        owner_instance_id="root",
    )
    finish_operation(db, parent["handle"], status="failed", result={"status": "failed"})
    current_parent_id = parent["handle"].operation_id
    for depth in range(1, MAX_RETRY_DEPTH + 1):
        child = claim_operation(
            db,
            operation_type="retention_run",
            scope=_scope(segment=30),
            request_identity={"lineage": depth},
            actor=owner,
            operation_id=f"stage41011-lineage-{depth}",
            idempotency_key=f"stage41011-lineage-{depth}",
            parent_operation_id=current_parent_id,
            owner_instance_id=f"lineage-{depth}",
        )
        finish_operation(db, child["handle"], status="failed", result={"status": "failed"})
        assert db.get(StorageOperation, child["handle"].operation_id).retry_depth == depth
        current_parent_id = child["handle"].operation_id

    with pytest.raises(StorageOperationContractError, match="operation_retry_depth_exceeded"):
        claim_operation(
            db,
            operation_type="retention_run",
            scope=_scope(segment=30),
            request_identity={"lineage": "overflow"},
            actor=owner,
            operation_id="stage41011-lineage-overflow",
            idempotency_key="stage41011-lineage-overflow",
            parent_operation_id=current_parent_id,
            owner_instance_id="lineage-overflow",
        )

    direct_parent_claim = claim_operation(
        db,
        operation_type="retention_run",
        scope=_scope(segment=31),
        request_identity={"direct_parent": True},
        actor=owner,
        operation_id="stage41011-direct-parent",
        idempotency_key="stage41011-direct-parent",
        owner_instance_id="direct-parent",
    )
    finish_operation(db, direct_parent_claim["handle"], status="failed", result={"status": "failed"})
    direct_parent = db.get(StorageOperation, direct_parent_claim["handle"].operation_id)
    for index in range(MAX_RETRIES_PER_PARENT):
        direct = claim_operation(
            db,
            operation_type="retention_run",
            scope=_scope(segment=31 + index),
            request_identity={"direct_retry": index},
            actor=owner,
            operation_id=f"stage41011-direct-retry-{index}",
            idempotency_key=f"stage41011-direct-retry-{index}",
            parent_operation_id=direct_parent.id,
            owner_instance_id=f"direct-retry-{index}",
        )
        finish_operation(db, direct["handle"], status="failed", result={"status": "failed"})

    with pytest.raises(StorageOperationContractError, match="operation_retry_parent_limit_reached"):
        claim_operation(
            db,
            operation_type="retention_run",
            scope=_scope(segment=99),
            request_identity={"direct_retry": "overflow"},
            actor=owner,
            operation_id="stage41011-direct-retry-overflow",
            idempotency_key="stage41011-direct-retry-overflow",
            parent_operation_id=direct_parent.id,
            owner_instance_id="direct-retry-overflow",
        )


def test_terminal_replay_is_concurrent_and_does_not_create_rows(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'terminal-replay.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with Session() as db:
        first = claim_operation_with_conflicts(
            db,
            operation_type="archive_migration_apply",
            scope={"global": True, "root_ids": [], "camera_ids": [], "segment_ids": [], "physical_volume_ids": []},
            request_identity={"plan": "concurrent-replay"},
            system_owner="concurrent-replay",
            operation_id="stage41011-concurrent-replay",
            idempotency_key="stage41011-concurrent-replay",
            owner_instance_id="first",
        )
        finish_operation(db, first["handle"], status="completed", result={"status": "completed", "executed_count": 1})

    results = []
    errors = []

    def replay_worker(index):
        try:
            with Session() as db:
                results.append(
                    claim_operation_with_conflicts(
                        db,
                        operation_type="archive_migration_apply",
                        scope={"global": True, "root_ids": [], "camera_ids": [], "segment_ids": [], "physical_volume_ids": []},
                        request_identity={"plan": "concurrent-replay"},
                        system_owner="concurrent-replay",
                        operation_id="stage41011-concurrent-replay",
                        idempotency_key="stage41011-concurrent-replay",
                        owner_instance_id=f"replay-{index}",
                    )
                )
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=replay_worker, args=(index,)) for index in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert errors == []
    assert len(results) == 6
    assert all(item["state"] == "terminal" for item in results)
    assert all(terminal_replay_result(item)["executed_count"] == 1 for item in results)
    with Session() as db:
        assert db.query(StorageOperation).count() == 1
    engine.dispose()


def test_adapter_scope_resolves_physical_volume_from_root(stage4101):
    scope = scope_with_physical_volumes(
        stage4101["db"],
        {
            "global": False,
            "root_ids": [stage4101["root"].id],
            "camera_ids": [stage4101["camera"].id],
            "segment_ids": [],
            "physical_volume_ids": [],
        },
    )

    assert scope["physical_volume_ids"] == normalize_operation_scope(
        {"physical_volume_ids": [stage4101["root"].physical_identity]}
    )["physical_volume_ids"]


def test_real_adapter_scopes_conflict_on_shared_physical_volume(stage4101):
    db = stage4101["db"]
    owner = stage4101["owner"]
    second_root = ArchiveRoot(
        id="stage41011-shared-volume-root",
        label="Shared volume root",
        root_path=str(stage4101["archive"] / "second"),
        storage_namespace="kmvms/recordings",
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity=stage4101["root"].physical_identity,
    )
    db.add(second_root)
    db.commit()
    first = claim_operation_with_conflicts(
        db,
        operation_type="archive_root_delete",
        scope={"global": False, "root_ids": [stage4101["root"].id], "camera_ids": [], "segment_ids": [], "physical_volume_ids": []},
        request_identity={"root": stage4101["root"].id},
        actor=owner,
        operation_id="stage41011-shared-volume-first",
        idempotency_key="stage41011-shared-volume-first",
        owner_instance_id="first",
    )

    with pytest.raises(StorageOperationConflict, match="storage_operation_scope_conflict"):
        claim_operation_with_conflicts(
            db,
            operation_type="archive_migration_apply",
            scope={"global": False, "root_ids": [second_root.id], "camera_ids": [], "segment_ids": [], "physical_volume_ids": []},
            request_identity={"root": second_root.id},
            actor=owner,
            operation_id="stage41011-shared-volume-second",
            idempotency_key="stage41011-shared-volume-second",
            owner_instance_id="second",
        )
    finish_operation(db, first["handle"], status="completed", result={"status": "completed"})


def test_missing_physical_identity_escalates_adapter_scope_to_global(stage4101):
    db = stage4101["db"]
    root = ArchiveRoot(
        id="stage41011-unknown-volume-root",
        label="Unknown volume root",
        root_path=str(stage4101["archive"] / "unknown"),
        storage_namespace="kmvms/recordings",
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity=None,
    )
    db.add(root)
    db.commit()

    scope = scope_with_physical_volumes(
        db,
        {"global": False, "root_ids": [root.id], "camera_ids": [], "segment_ids": [], "physical_volume_ids": []},
    )

    assert scope["global"] is True
    assert scope["root_ids"] == []
    assert scope["physical_volume_ids"] == []


def test_coordinator_acquisition_does_not_flush_request_session(stage4101):
    db = stage4101["db"]
    pending = AuditEvent(
        category="test",
        event_type="stage41011.pending",
        severity="info",
        message_ru="pending",
        message_en="pending",
    )
    db.add(pending)

    coordinator = storage_operation_conflicts._coordinator_lease(db, "stage41011-independent-session")
    coordinator.close()

    assert pending in db.new
    assert pending.id is None


def test_schema_v2_to_v3_adds_lineage_columns():
    engine = create_engine("sqlite:///:memory:")
    SchemaVersionState.__table__.create(bind=engine)
    SchemaMigrationHistory.__table__.create(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE storage_operations (id VARCHAR(96) PRIMARY KEY)"))
    db = _session(engine)
    now = datetime.utcnow()
    db.add(
        SchemaVersionState(
            id=CURRENT_STATE_ID,
            schema_version=2,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status="current",
            source=MIGRATION_SOURCE,
            applied_at=now,
        )
    )
    db.add(
        SchemaMigrationHistory(
            migration_id="stage41011_seed_v2",
            previous_version=1,
            target_version=2,
            schema_version=2,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status="current",
            source=MIGRATION_SOURCE,
        )
    )
    db.commit()

    result = execute_migration_plan(db, registry=PRODUCTION_MIGRATIONS, target_version=3)
    columns = {item["name"] for item in inspect(engine).get_columns("storage_operations")}

    assert result["executed_migrations"] == [STAGE41011_OPERATION_LINEAGE_MIGRATION.migration_id]
    assert {"parent_snapshot", "retry_depth"}.issubset(columns)
    assert db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == 3
    db.close()
    engine.dispose()


def test_coordinator_heartbeat_prevents_takeover_during_long_critical_section(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'coordinator.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    with engine.begin() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    entered = threading.Event()
    first_result = []
    second_result = []
    original_guard = storage_operation_conflicts.active_recorder_write_guard

    def delayed_guard(db):
        if threading.current_thread().name == "stage41011-first":
            entered.set()
            time.sleep(6)
        return original_guard(db)

    monkeypatch.setattr(storage_operation_conflicts, "COORDINATOR_LEASE_SECONDS", 5)
    monkeypatch.setattr(storage_operation_conflicts, "active_recorder_write_guard", delayed_guard)

    def first_worker():
        with Session() as db:
            first_result.append(
                claim_operation_with_conflicts(
                    db,
                    operation_type="archive_root_delete",
                    scope=_scope(root="root-a", camera=None, segment=None, volume="shared"),
                    request_identity={"worker": 1},
                    system_owner="stage41011-first",
                    operation_id="stage41011-coordinator-first",
                    idempotency_key="stage41011-coordinator-first",
                    owner_instance_id="first",
                )
            )

    def second_worker():
        assert entered.wait(timeout=5)
        with Session() as db:
            try:
                claim_operation_with_conflicts(
                    db,
                    operation_type="archive_migration_apply",
                    scope=_scope(root="root-b", camera=None, segment=None, volume="shared"),
                    request_identity={"worker": 2},
                    system_owner="stage41011-second",
                    operation_id="stage41011-coordinator-second",
                    idempotency_key="stage41011-coordinator-second",
                    owner_instance_id="second",
                )
            except StorageOperationConflict as exc:
                second_result.append(exc.detail["reason_code"])

    first_thread = threading.Thread(target=first_worker, name="stage41011-first")
    second_thread = threading.Thread(target=second_worker, name="stage41011-second")
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=15)
    second_thread.join(timeout=15)

    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert first_result[0]["state"] == "claimed"
    assert second_result == ["storage_operation_coordinator_busy"]
    with Session() as db:
        finish_operation(db, first_result[0]["handle"], status="completed", result={"status": "completed"})
    engine.dispose()
