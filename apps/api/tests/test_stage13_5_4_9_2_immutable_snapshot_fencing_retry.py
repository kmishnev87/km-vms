from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.user import User
from app.routers import recordings as recordings_router
from app.services import recording_operations
from app.services.recording_operations import (
    DestructiveScopeConflict,
    LeaseHeartbeat,
    ManifestValidationError,
    OperationStateConflict,
    acquire_scope_lease,
    claim_exact_operation,
    cleanup_operation_records,
    create_deletion_plan,
    open_verified_deletion_manifest,
    operation_scope_mutation_guard,
)
from app.services.recording_storage import KMVMS_RECORDINGS_NAMESPACE, ROOT_RESOLUTION_RESOLVED


@pytest.fixture
def stage492(tmp_path):
    original = {
        "storage_root": settings.storage_root,
        "storage_previews": settings.storage_previews,
        "storage_exports": settings.storage_exports,
        "storage_install_control": settings.storage_install_control,
    }
    root_path = tmp_path / "Volume3" / "Archive"
    (root_path / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True)
    settings.storage_root = str(root_path)
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")
    settings.storage_install_control = str(tmp_path / "install-control")

    engine = create_engine(
        f"sqlite:///{tmp_path / 'stage492.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    actor = User(
        username="stage492_owner",
        full_name="Stage 4.9.2 Owner",
        password_hash="unused",
        role="owner",
        is_active=True,
    )
    other = User(
        username="stage492_other",
        full_name="Stage 4.9.2 Other",
        password_hash="unused",
        role="owner",
        is_active=True,
    )
    camera = Camera(
        name="Stage492 Camera",
        storage_folder_name="stage492-camera",
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        status="enabled",
    )
    root = ArchiveRoot(
        id="stage492-root",
        label="Stage 4.9.2 Root",
        root_path=str(root_path),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage492-volume-3",
    )
    db.add_all([actor, other, camera, root])
    db.commit()
    for item in (actor, other, camera, root):
        db.refresh(item)
    try:
        yield {
            "db": db,
            "engine": engine,
            "Session": Session,
            "actor": actor,
            "other": other,
            "camera": camera,
            "root": root,
            "root_path": root_path,
            "tmp_path": tmp_path,
        }
    finally:
        db.close()
        engine.dispose()
        for key, value in original.items():
            setattr(settings, key, value)


def _segment(stage492, index: int, *, status: str = "finalized", logical_size: int = 32) -> RecordingSegment:
    relative = f"{KMVMS_RECORDINGS_NAMESPACE}/stage492-{index:05d}.mkv"
    file_path = stage492["root_path"] / relative
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x" * max(1, min(logical_size, 128)))
    now = datetime.utcnow()
    segment = RecordingSegment(
        camera_id=stage492["camera"].id,
        camera_name_snapshot=stage492["camera"].name,
        camera_folder_snapshot=stage492["camera"].storage_folder_name,
        file_path=relative,
        relative_path=relative,
        started_at=now - timedelta(minutes=index + 1),
        ended_at=now if status == "finalized" else None,
        finalized_at=now if status == "finalized" else None,
        duration_sec=60,
        size_bytes=logical_size,
        status=status,
        ownership="KM VMS",
        source="recorder",
        archive_root_id=stage492["root"].id,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
    )
    stage492["db"].add(segment)
    return segment


def _create_plan(stage492, *, scope: str = "camera") -> dict:
    return recordings_router.create_recording_deletion_plan(
        recordings_router.RecordingDeletionPlanRequest(
            scope=scope,
            camera=stage492["camera"].name if scope == "camera" else None,
        ),
        db=stage492["db"],
        current_user=stage492["actor"],
    )


def _execute_plan(stage492, plan: dict) -> dict:
    return recordings_router.execute_recording_deletion_plan(
        plan["plan_id"],
        recordings_router.RecordingDeletionExecuteRequest(confirm=True),
        db=stage492["db"],
        current_user=stage492["actor"],
    )


def _manifest_item(segment_id: int, *, root_id: str = "stage492-root", size_bytes: int = 10) -> dict:
    return {
        "segment_id": segment_id,
        "archive_root_id": root_id,
        "relative_path": f"{KMVMS_RECORDINGS_NAMESPACE}/manifest-{segment_id}.mkv",
        "size_bytes": size_bytes,
        "camera_id": 1,
    }


def test_exact_manifest_distinguishes_aggregate_collision_sets(stage492):
    first = create_deletion_plan(
        actor=stage492["actor"],
        scope_type="all",
        planned_items=[_manifest_item(item) for item in (1, 2, 5, 6)],
    )
    second = create_deletion_plan(
        actor=stage492["actor"],
        scope_type="all",
        planned_items=[_manifest_item(item) for item in (1, 3, 4, 6)],
    )

    assert first["planned_count"] == second["planned_count"] == 4
    assert first["planned_bytes"] == second["planned_bytes"] == 40
    assert first["manifest_sha256"] != second["manifest_sha256"]
    with open_verified_deletion_manifest(first) as first_manifest:
        first_ids = [item["segment_id"] for batch in first_manifest.iter_batches() for item in batch]
    with open_verified_deletion_manifest(second) as second_manifest:
        second_ids = [item["segment_id"] for batch in second_manifest.iter_batches() for item in batch]
    assert first_ids == [1, 2, 5, 6]
    assert second_ids == [1, 3, 4, 6]


def test_manifest_plan_creation_uses_one_streaming_recording_select(stage492):
    for index in range(205):
        _segment(stage492, index)
    stage492["db"].commit()
    recording_selects = []

    def before_cursor_execute(_connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(str(statement).lower().split())
        if " from recording_segments " in f" {normalized} ":
            recording_selects.append(normalized)

    event.listen(stage492["engine"], "before_cursor_execute", before_cursor_execute)
    try:
        plan = _create_plan(stage492)
    finally:
        event.remove(stage492["engine"], "before_cursor_execute", before_cursor_execute)

    assert plan["planned_count"] == 205
    assert len(recording_selects) == 1
    assert "order by recording_segments.id asc" in recording_selects[0]


def test_writing_segment_with_lower_id_finalized_after_plan_is_not_added(stage492):
    writing = _segment(stage492, 1, status="writing")
    planned = _segment(stage492, 2)
    stage492["db"].commit()
    assert writing.id < planned.id
    plan = _create_plan(stage492)

    writing.status = "finalized"
    writing.ended_at = datetime.utcnow()
    writing.finalized_at = datetime.utcnow()
    stage492["db"].commit()
    result = _execute_plan(stage492, plan)

    stage492["db"].refresh(writing)
    stage492["db"].refresh(planned)
    assert result["deleted_count"] == 1, result
    assert planned.status == "deleted"
    assert writing.status == "finalized"
    assert (stage492["root_path"] / writing.relative_path).is_file()


def test_writing_segment_finalized_between_manifest_validation_and_next_batch_is_not_added(stage492, monkeypatch):
    writing = _segment(stage492, 1, status="writing")
    planned = [_segment(stage492, index + 2) for index in range(101)]
    stage492["db"].commit()
    plan = _create_plan(stage492)
    original_execute = recordings_router.execute_segments
    calls = 0

    def execute_with_interleaving(*args, **kwargs):
        nonlocal calls
        result = original_execute(*args, **kwargs)
        calls += 1
        if calls == 1:
            writing.status = "finalized"
            writing.ended_at = datetime.utcnow()
            writing.finalized_at = datetime.utcnow()
            stage492["db"].commit()
        return result

    monkeypatch.setattr(recordings_router, "execute_segments", execute_with_interleaving)
    result = _execute_plan(stage492, plan)

    stage492["db"].refresh(writing)
    assert result["deleted_count"] == len(planned), result
    assert writing.status == "finalized"
    assert (stage492["root_path"] / writing.relative_path).is_file()


@pytest.mark.parametrize("change", ["size", "path", "status"])
def test_planned_identity_change_blocks_before_mutation(stage492, change):
    segment = _segment(stage492, 1)
    stage492["db"].commit()
    plan = _create_plan(stage492)
    if change == "size":
        segment.size_bytes += 1
    elif change == "path":
        changed_relative = f"{KMVMS_RECORDINGS_NAMESPACE}/changed-path.mkv"
        (stage492["root_path"] / changed_relative).write_bytes(b"changed")
        segment.relative_path = changed_relative
        segment.file_path = changed_relative
    else:
        segment.status = "writing"
    stage492["db"].commit()

    result = _execute_plan(stage492, plan)

    assert result["status"] == "blocked"
    assert result["deleted_count"] == 0
    assert result["reason_counts"] == {"deletion_plan_item_changed": 1}
    assert (stage492["root_path"] / f"{KMVMS_RECORDINGS_NAMESPACE}/stage492-00001.mkv").is_file()


def test_corrupt_manifest_fails_closed_without_file_mutation(stage492):
    segment = _segment(stage492, 1)
    stage492["db"].commit()
    plan = _create_plan(stage492)
    record = recording_operations.read_operation(plan["plan_id"])
    manifest_path = recording_operations._manifest_path(plan["plan_id"])
    manifest_path.chmod(0o600)
    with manifest_path.open("ab") as handle:
        handle.write(b"{}\n")

    result = _execute_plan(stage492, plan)

    assert result["status"] == "blocked"
    assert result["reason_counts"] == {"deletion_plan_manifest_verification_failed": 1}
    assert record["manifest_sha256"]
    assert (stage492["root_path"] / segment.relative_path).is_file()


def test_symlink_manifest_is_rejected_without_following_target(stage492):
    segment = _segment(stage492, 1)
    stage492["db"].commit()
    plan = _create_plan(stage492)
    manifest_path = recording_operations._manifest_path(plan["plan_id"])
    target = stage492["tmp_path"] / "untrusted-manifest-target"
    target.write_text("do-not-read", encoding="utf-8")
    manifest_path.unlink()
    try:
        manifest_path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(HTTPException) as blocked:
        _execute_plan(stage492, plan)

    assert blocked.value.status_code == 409
    assert blocked.value.detail["error"] == "deletion_plan_manifest_not_regular"
    assert target.read_text(encoding="utf-8") == "do-not-read"
    assert (stage492["root_path"] / segment.relative_path).is_file()


def test_ready_record_is_written_only_after_final_manifest_verifies(stage492, monkeypatch):
    observed = []
    original_write_json = recording_operations._write_json

    def checked_write(path, payload):
        if path.parent == recording_operations._operation_dir() and payload.get("status") == "ready":
            with open_verified_deletion_manifest(payload) as manifest:
                observed.append(manifest.facts["manifest_count"])
        return original_write_json(path, payload)

    monkeypatch.setattr(recording_operations, "_write_json", checked_write)
    plan = create_deletion_plan(
        actor=stage492["actor"],
        scope_type="all",
        planned_items=[_manifest_item(1)],
    )

    assert observed == [1]
    assert recording_operations.read_operation(plan["operation_id"])["status"] == "ready"


def test_failed_operation_record_publication_removes_exact_manifest(stage492, monkeypatch):
    before = set(recording_operations._manifest_dir().glob("*.ndjson"))
    original_write_json = recording_operations._write_json

    def fail_record(path, payload):
        if path.parent == recording_operations._operation_dir() and payload.get("status") == "ready":
            raise OSError("injected operation record failure")
        return original_write_json(path, payload)

    monkeypatch.setattr(recording_operations, "_write_json", fail_record)
    with pytest.raises(OSError, match="injected operation record failure"):
        create_deletion_plan(
            actor=stage492["actor"],
            scope_type="all",
            planned_items=[_manifest_item(77)],
        )
    assert set(recording_operations._manifest_dir().glob("*.ndjson")) == before


def test_orphan_manifest_cleanup_is_bounded_and_exact(stage492, monkeypatch):
    operation_id = "recording-plan-orphan-0001"
    orphan = recording_operations._manifest_path(operation_id)
    orphan.write_text("", encoding="utf-8")
    old = time.time() - recording_operations.MANIFEST_ORPHAN_GRACE_SECONDS - 5
    os.utime(orphan, (old, old))
    monkeypatch.setattr(recording_operations, "MANIFEST_CLEANUP_DELETE_LIMIT", 1)

    result = cleanup_operation_records(now=time.time())

    assert result["manifest_deleted_count"] == 1
    assert not orphan.exists()


def test_cancel_non_disclosure_and_manifest_lifecycle(stage492):
    _segment(stage492, 1)
    stage492["db"].commit()
    plan = _create_plan(stage492)
    manifest_path = recording_operations._manifest_path(plan["plan_id"])
    assert manifest_path.is_file()

    foreign = recording_operations.cancel_deletion_plan(plan["plan_id"], actor=stage492["other"])
    missing = recording_operations.cancel_deletion_plan("recording-plan-missing-0002", actor=stage492["actor"])
    assert foreign["cancelled"] is False
    assert {key: value for key, value in foreign.items() if key != "operation_id"} == {
        key: value for key, value in missing.items() if key != "operation_id"
    }
    assert manifest_path.is_file()

    cancelled = recording_operations.cancel_deletion_plan(plan["plan_id"], actor=stage492["actor"])
    repeated = recording_operations.cancel_deletion_plan(plan["plan_id"], actor=stage492["actor"])
    assert cancelled["cancelled"] is True
    assert repeated["cancelled"] is False
    assert not manifest_path.exists()


def test_revoked_permission_blocks_execute_cancel_and_terminal_replay(stage492):
    first = _segment(stage492, 1)
    second = _segment(stage492, 2)
    stage492["db"].commit()
    blocked_plan = _create_plan(stage492)

    stage492["actor"].role = "viewer"
    stage492["db"].commit()
    with pytest.raises(HTTPException) as execute_denied:
        _execute_plan(stage492, blocked_plan)
    assert execute_denied.value.status_code == 403
    with pytest.raises(HTTPException) as cancel_denied:
        recordings_router.cancel_recording_deletion_plan(
            blocked_plan["plan_id"],
            db=stage492["db"],
            current_user=stage492["actor"],
        )
    assert cancel_denied.value.status_code == 403
    assert (stage492["root_path"] / first.relative_path).is_file()

    stage492["actor"].role = "owner"
    stage492["db"].commit()
    terminal_plan = _create_plan(stage492)
    terminal = _execute_plan(stage492, terminal_plan)
    assert terminal["deleted_count"] == 2, terminal
    stage492["actor"].role = "viewer"
    stage492["db"].commit()
    with pytest.raises(HTTPException) as terminal_denied:
        _execute_plan(stage492, terminal_plan)
    assert terminal_denied.value.status_code == 403
    assert second.status == "deleted"


def test_owner_heartbeat_prevents_takeover_and_stops_deterministically(stage492, monkeypatch):
    monkeypatch.setattr(recording_operations, "SCOPE_LEASE_SECONDS", 0.25)
    monkeypatch.setattr(recording_operations, "OPERATION_LEASE_SECONDS", 0.25)
    claimed = claim_exact_operation(
        "stage492-heartbeat-operation",
        actor=stage492["actor"],
        operation_type="manual_single_delete",
        request_fingerprint="heartbeat-fingerprint",
    )
    scope = {"type": "root", "segment_ids": [], "camera_ids": [1], "root_ids": ["stage492-root"]}
    lease = acquire_scope_lease("stage492-heartbeat-operation", scope, purpose="stage492-heartbeat")
    started = time.monotonic()
    with LeaseHeartbeat(
        scope_lease=lease,
        operation_id="stage492-heartbeat-operation",
        owner_token=claimed["owner_token"],
        interval_seconds=0.03,
        progress_timeout_seconds=0.15,
    ) as heartbeat:
        while time.monotonic() - started < 0.45:
            heartbeat.progress()
            time.sleep(0.04)
        with pytest.raises(DestructiveScopeConflict):
            acquire_scope_lease("stage492-overlap-live", scope, purpose="stage492-overlap")

    time.sleep(0.3)
    recovered = acquire_scope_lease("stage492-overlap-recovered", scope, purpose="stage492-recovery")
    recovered.release()
    assert not any(thread.name.startswith("km-vms-lease-heartbeat-stage492") for thread in threading.enumerate())


def test_final_mutation_fence_serializes_reclaim_and_rejects_stale_owner(stage492, monkeypatch):
    monkeypatch.setattr(recording_operations, "SCOPE_LEASE_SECONDS", 0.15)
    monkeypatch.setattr(recording_operations, "OPERATION_LEASE_SECONDS", 0.15)
    claimed = claim_exact_operation(
        "stage492-fence-operation",
        actor=stage492["actor"],
        operation_type="manual_single_delete",
        request_fingerprint="fence-fingerprint",
    )
    scope = {"type": "root", "segment_ids": [], "camera_ids": [1], "root_ids": ["stage492-root"]}
    lease = acquire_scope_lease("stage492-fence-operation", scope, purpose="stage492-fence")
    entered = threading.Event()
    order = []
    acquired = []

    def reclaim():
        entered.wait(timeout=2)
        replacement = acquire_scope_lease("stage492-fence-replacement", scope, purpose="stage492-replacement")
        order.append("replacement")
        acquired.append(replacement)

    worker = threading.Thread(target=reclaim)
    worker.start()
    with operation_scope_mutation_guard("stage492-fence-operation", claimed["owner_token"], lease):
        entered.set()
        time.sleep(0.2)
        order.append("original-mutation")
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert order == ["original-mutation", "replacement"]
    with pytest.raises((OperationStateConflict, DestructiveScopeConflict)):
        with operation_scope_mutation_guard("stage492-fence-operation", claimed["owner_token"], lease):
            pass
    acquired[0].release()


def test_single_delete_final_identity_change_fails_closed(stage492, monkeypatch):
    segment = _segment(stage492, 1)
    stage492["db"].commit()
    from app.services import recording_retention

    original_fresh_segment = recording_retention._fresh_segment
    changed = False

    def changed_fresh(db, segment_id):
        nonlocal changed
        fresh = original_fresh_segment(db, segment_id)
        if fresh is not None and not changed:
            changed = True
            fresh.size_bytes += 1
            db.commit()
            fresh = original_fresh_segment(db, segment_id)
        return fresh

    monkeypatch.setattr(recording_retention, "_fresh_segment", changed_fresh)
    with pytest.raises(HTTPException) as blocked:
        recordings_router.delete_recording(
            path=None,
            segment_id=segment.id,
            archive_root_id=stage492["root"].id,
            recording_ref=None,
            operation_id="stage492-single-changed-operation",
            db=stage492["db"],
            current_user=stage492["actor"],
        )
    assert blocked.value.status_code == 409
    assert (stage492["root_path"] / segment.relative_path).is_file()


@pytest.mark.parametrize(
    ("reason", "status", "retry_mode", "next_action", "retry_available"),
    [
        ("root_directory_remove_failed", "partial_cleanup", "immediate", "retry_cleanup", True),
        ("selected_mount_missing", "partial_cleanup", "after_refresh", "refresh_storage_state", False),
        ("selected_mount_not_writable", "partial_cleanup", "after_external_fix", "correct_storage_access", False),
        ("root_marker_path_mismatch", "partial_cleanup", "none", "close", False),
        ("root_marker_symlink_rejected", "partial_cleanup", "none", "close", False),
        ("unknown_helper_reason", "partial_cleanup", "none", "close", False),
        ("foreign_or_user_content_preserved", "completed_preserved_nonempty", "none", "close", False),
    ],
)
def test_cleanup_retry_reason_matrix(reason, status, retry_mode, next_action, retry_available):
    from app.services.setup_storage import archive_root_cleanup_capability

    capability = archive_root_cleanup_capability(reason, status)
    assert capability == {
        "retry_mode": retry_mode,
        "next_action": next_action,
        "retry_available": retry_available,
    }
