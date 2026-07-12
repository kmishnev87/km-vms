from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.user import User
from app.routers import recordings as recordings_router
from app.routers import storage as storage_router
from app.services import recording_operations
from app.services.recording_operations import OperationStateConflict
from app.services.recording_storage import KMVMS_RECORDINGS_NAMESPACE, ROOT_RESOLUTION_RESOLVED


@pytest.fixture
def stage491(tmp_path):
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

    engine = create_engine(f"sqlite:///{tmp_path / 'stage491.sqlite3'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    actor = User(username="stage491_owner", full_name="Owner", password_hash="unused", role="owner", is_active=True)
    other = User(username="stage491_other", full_name="Other", password_hash="unused", role="owner", is_active=True)
    camera = Camera(
        name="Stage491 Camera",
        storage_folder_name="stage491-camera",
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        status="enabled",
    )
    root = ArchiveRoot(
        id="stage491-root",
        label="Stage 4.9.1 Root",
        root_path=str(root_path),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage491-volume-3",
    )
    db.add_all([actor, other, camera, root])
    db.commit()
    for item in (actor, other, camera, root):
        db.refresh(item)
    try:
        yield {
            "db": db,
            "actor": actor,
            "other": other,
            "camera": camera,
            "root": root,
            "root_path": root_path,
        }
    finally:
        db.close()
        engine.dispose()
        for key, value in original.items():
            setattr(settings, key, value)


def _segment(stage491, index: int, *, logical_size: int = 32) -> RecordingSegment:
    relative = f"{KMVMS_RECORDINGS_NAMESPACE}/stage491-{index:05d}.mkv"
    file_path = stage491["root_path"] / relative
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x" * logical_size)
    now = datetime.utcnow()
    segment = RecordingSegment(
        camera_id=stage491["camera"].id,
        camera_name_snapshot=stage491["camera"].name,
        camera_folder_snapshot=stage491["camera"].storage_folder_name,
        file_path=relative,
        relative_path=relative,
        started_at=now - timedelta(minutes=index + 1),
        ended_at=now,
        finalized_at=now,
        duration_sec=60,
        size_bytes=logical_size,
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=stage491["root"].id,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
    )
    stage491["db"].add(segment)
    return segment


def _create_plan(stage491) -> dict:
    return recordings_router.create_recording_deletion_plan(
        recordings_router.RecordingDeletionPlanRequest(scope="camera", camera=stage491["camera"].name),
        db=stage491["db"],
        current_user=stage491["actor"],
    )


def _execute_plan(stage491, plan: dict) -> dict:
    return recordings_router.execute_recording_deletion_plan(
        plan["plan_id"],
        recordings_router.RecordingDeletionExecuteRequest(confirm=True),
        db=stage491["db"],
        current_user=stage491["actor"],
    )


def test_completed_root_reactivation_starts_fresh_delete_lifecycle(stage491, monkeypatch):
    root = stage491["root"]
    old_operation_id = "archive-root-old-lifecycle"
    root.retired_at = datetime.utcnow()
    root.retirement_status = "completed"
    root.retirement_problem = "old_problem"
    root.retirement_operation_id = old_operation_id
    root.retirement_cleanup_status = "completed_removed"
    root.retirement_cleanup_result = {
        "operation_id": old_operation_id,
        "marker_removed": True,
    }
    stage491["db"].commit()

    @contextmanager
    def mutation_guard(_purpose):
        yield "stage491-mutation"

    monkeypatch.setattr(storage_router, "archive_root_mutation_guard", mutation_guard)
    monkeypatch.setattr(
        storage_router,
        "_archive_root_path_from_payload",
        lambda _payload: (
            stage491["root_path"],
            {"physical_identity": root.physical_identity, "exists": True, "writable": True, "folder_name": "Archive"},
            stage491["root_path"].parent,
        ),
    )
    monkeypatch.setattr(storage_router, "sanitize_archive_root_path", lambda path, **_kwargs: path)
    monkeypatch.setattr(storage_router, "write_archive_roots_runtime_files", lambda _db: None)
    storage_router.create_archive_root(
        storage_router.ArchiveRootCreateRequest(candidate_id="candidate", folder_name="Archive"),
        db=stage491["db"],
        current_user=stage491["actor"],
    )
    stage491["db"].refresh(root)
    assert root.retired_at is None
    assert root.retirement_status is None
    assert root.retirement_problem is None
    assert root.retirement_operation_id is None
    assert root.retirement_cleanup_status is None
    assert root.retirement_cleanup_result is None

    captured = {}

    @contextmanager
    def scope_guard(operation_id, _scope, *, purpose):
        captured.update(operation_id=operation_id, purpose=purpose)
        yield object()

    monkeypatch.setattr(storage_router, "destructive_scope_guard", scope_guard)
    monkeypatch.setattr(
        storage_router,
        "_delete_inactive_root_owned",
        lambda _db, _root, _actor, *, operation_id, scope_lease: {"operation_id": operation_id},
    )
    result = storage_router._delete_inactive_root(stage491["db"], root, stage491["actor"])
    assert result["operation_id"] == captured["operation_id"]
    assert result["operation_id"] != old_operation_id


def test_same_partial_cleanup_retry_reuses_operation_and_marker_evidence(stage491, monkeypatch):
    root = stage491["root"]
    operation_id = "archive-root-partial-retry"
    root.retirement_status = "partial_cleanup"
    root.retirement_operation_id = operation_id
    root.retirement_cleanup_status = "partial_cleanup"
    root.retirement_cleanup_result = {"operation_id": operation_id, "marker_removed": True}
    stage491["db"].commit()
    captured = {}

    class Lease:
        def touch(self):
            return None

        def assert_owned(self):
            return None

    def cleanup(_root, *, operation_id, marker_already_removed):
        captured.update(operation_id=operation_id, marker_already_removed=marker_already_removed)
        return {
            "status": "completed",
            "cleanup_status": "completed_preserved_nonempty",
            "reason": "foreign_content_preserved",
            "marker_removed": True,
            "root_directory_removed": False,
            "root_directory_preserved_reason": "foreign_content_preserved",
            "retry_available": False,
        }

    monkeypatch.setattr(storage_router.setup_storage, "request_archive_root_cleanup", cleanup)
    monkeypatch.setattr(storage_router, "write_archive_roots_runtime_files", lambda _db: None)
    result = storage_router._delete_inactive_root_owned(
        stage491["db"], root, stage491["actor"], operation_id=operation_id, scope_lease=Lease()
    )
    assert result["status"] == "completed"
    assert captured == {"operation_id": operation_id, "marker_already_removed": True}


@pytest.mark.parametrize("reason", ["root_marker_missing", "root_marker_path_mismatch"])
def test_fresh_delete_lifecycle_never_trusts_old_marker_evidence(stage491, monkeypatch, reason):
    root = stage491["root"]
    root.retirement_status = None
    root.retirement_operation_id = None
    root.retirement_cleanup_status = None
    root.retirement_cleanup_result = {
        "operation_id": "archive-root-completed-old",
        "marker_removed": True,
    }
    stage491["db"].commit()
    captured = {}

    class Lease:
        def touch(self):
            return None

        def assert_owned(self):
            return None

    def cleanup(_root, *, operation_id, marker_already_removed):
        captured.update(operation_id=operation_id, marker_already_removed=marker_already_removed)
        raise ValueError(reason)

    monkeypatch.setattr(storage_router.setup_storage, "request_archive_root_cleanup", cleanup)
    result = storage_router._delete_inactive_root_owned(
        stage491["db"],
        root,
        stage491["actor"],
        operation_id="archive-root-fresh-delete",
        scope_lease=Lease(),
    )
    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["reason"] == reason
    assert captured["marker_already_removed"] is False


def test_plan_scope_is_revalidated_only_after_destructive_lease(stage491, monkeypatch):
    segment = _segment(stage491, 1)
    stage491["db"].commit()
    plan = _create_plan(stage491)

    class Lease:
        operation_id = plan["plan_id"]

        def touch(self):
            return None

        def assert_owned(self):
            return None

    @contextmanager
    def interleaving_guard(_operation_id, _scope, *, purpose):
        assert purpose == "manual_delete_by_camera"
        segment.size_bytes += 1
        stage491["db"].add(segment)
        stage491["db"].commit()
        yield Lease()

    monkeypatch.setattr(recordings_router, "destructive_scope_guard", interleaving_guard)
    result = _execute_plan(stage491, plan)
    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["reason_counts"] == {"deletion_plan_item_changed": 1}
    assert (stage491["root_path"] / segment.relative_path).is_file()


def test_plan_cannot_report_success_when_batch_accounts_n_minus_one(stage491, monkeypatch):
    first = _segment(stage491, 1)
    second = _segment(stage491, 2)
    stage491["db"].commit()
    plan = _create_plan(stage491)
    original_execute = recordings_router.execute_segments

    def execute_n_minus_one(db, segments, **kwargs):
        batch = list(segments)
        return original_execute(db, batch[:-1], **kwargs)

    monkeypatch.setattr(recordings_router, "execute_segments", execute_n_minus_one)
    result = _execute_plan(stage491, plan)
    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["deleted_count"] == 1
    assert result["failed_reason_counts"] == {"deletion_plan_accounting_mismatch": 1}
    assert result["processed_count"] == result["planned_count"] == 2
    assert not (stage491["root_path"] / first.relative_path).exists()
    assert (stage491["root_path"] / second.relative_path).exists()


def test_ready_plan_cancel_is_actor_bound_idempotent_and_running_safe(stage491):
    _segment(stage491, 1)
    stage491["db"].commit()
    first_plan = _create_plan(stage491)
    foreign = recording_operations.cancel_deletion_plan(first_plan["plan_id"], actor=stage491["other"])
    missing = recording_operations.cancel_deletion_plan("recording-plan-missing-0001", actor=stage491["actor"])
    assert foreign == {
        "ok": True,
        "status": "cancelled",
        "operation_id": first_plan["plan_id"],
        "cancelled": False,
    }
    assert {key: value for key, value in foreign.items() if key != "operation_id"} == {
        key: value for key, value in missing.items() if key != "operation_id"
    }

    cancelled = recording_operations.cancel_deletion_plan(first_plan["plan_id"], actor=stage491["actor"])
    repeated = recording_operations.cancel_deletion_plan(first_plan["plan_id"], actor=stage491["actor"])
    assert cancelled["cancelled"] is True
    assert repeated["cancelled"] is False

    running_plan = _create_plan(stage491)
    recording_operations.claim_deletion_plan(running_plan["plan_id"], actor=stage491["actor"])
    with pytest.raises(OperationStateConflict) as running:
        recording_operations.cancel_deletion_plan(running_plan["plan_id"], actor=stage491["actor"])
    assert running.value.detail["reason"] == "deletion_plan_already_running"


def test_cancel_execute_race_has_one_serialized_winner(stage491):
    _segment(stage491, 1)
    stage491["db"].commit()
    plan = _create_plan(stage491)
    barrier = threading.Barrier(2)
    outcomes = []

    def cancel():
        barrier.wait()
        try:
            result = recording_operations.cancel_deletion_plan(plan["plan_id"], actor=stage491["actor"])
            outcomes.append(("cancelled", result["cancelled"]))
        except OperationStateConflict as exc:
            outcomes.append(("cancel-blocked", exc.detail["reason"]))

    def claim():
        barrier.wait()
        try:
            result = recording_operations.claim_deletion_plan(plan["plan_id"], actor=stage491["actor"])
            outcomes.append(("claimed", result["state"]))
        except OperationStateConflict as exc:
            outcomes.append(("claim-blocked", exc.detail["reason"]))

    threads = [threading.Thread(target=cancel), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert len(outcomes) == 2
    assert outcomes in (
        [("cancelled", True), ("claim-blocked", "deletion_plan_not_found")],
        [("claim-blocked", "deletion_plan_not_found"), ("cancelled", True)],
        [("claimed", "claimed"), ("cancel-blocked", "deletion_plan_already_running")],
        [("cancel-blocked", "deletion_plan_already_running"), ("claimed", "claimed")],
    )


def test_operation_record_cleanup_is_bounded_and_preserves_unsafe_entries(stage491, monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(recording_operations, "OPERATION_CLEANUP_DELETE_LIMIT", 2)
    monkeypatch.setattr(recording_operations, "OPERATION_CLEANUP_SCAN_LIMIT", 64)
    operation_ids = []
    for index in range(3):
        operation_id = f"terminal-old-{index:02d}"
        operation_ids.append(operation_id)
        recording_operations._write_json(
            recording_operations._operation_path(operation_id),
            {
                "operation_id": operation_id,
                "status": "completed",
                "completed_at_epoch": now - recording_operations.TERMINAL_RETENTION_SECONDS - 1,
            },
        )
    young_id = "terminal-young-01"
    recording_operations._write_json(
        recording_operations._operation_path(young_id),
        {"operation_id": young_id, "status": "completed", "completed_at_epoch": now},
    )
    running_id = "running-live-01"
    recording_operations._write_json(
        recording_operations._operation_path(running_id),
        {"operation_id": running_id, "status": "running", "lease_expires_at_epoch": now + 60},
    )
    corrupt = recording_operations._operation_dir() / "corrupt.json"
    corrupt.write_text("not-json", encoding="utf-8")
    symlink = recording_operations._operation_dir() / "unsafe-link.json"
    try:
        symlink.symlink_to(corrupt)
    except OSError:
        symlink = None

    result = recording_operations.cleanup_operation_records(now=now)
    assert result["deleted_count"] == 2
    assert sum(recording_operations._operation_path(item).exists() for item in operation_ids) == 1
    assert recording_operations._operation_path(young_id).exists()
    assert recording_operations._operation_path(running_id).exists()
    assert corrupt.exists()
    if symlink is not None:
        assert symlink.is_symlink()


def test_expired_ready_and_stale_running_records_have_finite_cleanup(stage491):
    now = 3_000_000.0
    expired_id = "ready-expired-01"
    stale_id = "running-stale-01"
    recording_operations._write_json(
        recording_operations._operation_path(expired_id),
        {
            "operation_id": expired_id,
            "operation_type": "manual_delete_plan",
            "status": "ready",
            "expires_at_epoch": now - recording_operations.EXPIRED_READY_GRACE_SECONDS - 1,
        },
    )
    recording_operations._write_json(
        recording_operations._operation_path(stale_id),
        {
            "operation_id": stale_id,
            "status": "running",
            "lease_expires_at_epoch": now - recording_operations.STALE_RUNNING_RETENTION_SECONDS - 1,
        },
    )
    result = recording_operations.cleanup_operation_records(now=now)
    assert result["deleted_count"] == 2
    assert not recording_operations._operation_path(expired_id).exists()
    assert not recording_operations._operation_path(stale_id).exists()


def test_live_active_scope_preserves_stale_running_operation_record(stage491):
    now = 4_000_000.0
    operation_id = "running-active-scope-01"
    recording_operations._write_json(
        recording_operations._operation_path(operation_id),
        {
            "operation_id": operation_id,
            "status": "running",
            "lease_expires_at_epoch": now - recording_operations.STALE_RUNNING_RETENTION_SECONDS - 1,
        },
    )
    lease = recording_operations.acquire_scope_lease(
        operation_id,
        {"type": "all", "segment_ids": [], "camera_ids": [], "root_ids": []},
        purpose="stage491-live-scope",
    )
    try:
        result = recording_operations.cleanup_operation_records(now=now)
        assert result["deleted_count"] == 0
        assert recording_operations._operation_path(operation_id).exists()
    finally:
        lease.release()
