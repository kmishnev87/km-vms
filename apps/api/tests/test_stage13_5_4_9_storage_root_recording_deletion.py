from __future__ import annotations

import os
import multiprocessing
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.routers import recordings as recordings_router
from app.routers import storage as storage_router
from app.services import storage_monitoring
from app.services import recording_operations
from app.services.recording_operations import (
    DestructiveScopeConflict,
    acquire_scope_lease,
    destructive_scope_guard,
    new_operation_id,
    scopes_overlap,
)
from app.services.recording_storage import KMVMS_RECORDINGS_NAMESPACE, ROOT_RESOLUTION_RESOLVED


STRICT_RESULT_KEYS = {
    "ok",
    "status",
    "operation_id",
    "scope",
    "requested_count",
    "planned_count",
    "processed_count",
    "deleted_count",
    "skipped_count",
    "failed_count",
    "bytes_freed",
    "batch_count",
    "reason_counts",
    "skipped_reason_counts",
    "failed_reason_counts",
    "retryable",
}


def _scope_process_worker(control_path: str, operation_id: str, scope: dict, output) -> None:
    settings.storage_install_control = control_path
    try:
        lease = acquire_scope_lease(operation_id, scope, purpose="stage49-process-test")
    except DestructiveScopeConflict as exc:
        output.put(("blocked", str(exc.detail.get("reason") or "")))
        return
    try:
        output.put(("acquired", ""))
    finally:
        lease.release()


@pytest.fixture
def stage49(tmp_path):
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

    engine = create_engine(f"sqlite:///{tmp_path / 'stage49.sqlite3'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    actor = User(
        username="stage49_owner",
        full_name="Stage 4.9 Owner",
        password_hash="not-used",
        role="owner",
        is_active=True,
    )
    camera = Camera(
        name="Stage49 Camera",
        storage_folder_name="stage49-camera",
        enabled=True,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        status="enabled",
    )
    root = ArchiveRoot(
        id="stage49-root",
        label="Stage 4.9 Root",
        root_path=str(root_path),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="stage49-volume-3",
    )
    db.add_all([actor, camera, root])
    db.commit()
    db.refresh(actor)
    db.refresh(camera)
    try:
        yield {
            "db": db,
            "actor": actor,
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


def _segment(stage49, index: int, *, logical_size: int = 32, create_file: bool = True) -> RecordingSegment:
    db = stage49["db"]
    camera = stage49["camera"]
    relative = f"{KMVMS_RECORDINGS_NAMESPACE}/stage49-{index:05d}.mkv"
    path = stage49["root_path"] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_file:
        with path.open("wb") as handle:
            if logical_size > 0:
                handle.seek(logical_size - 1)
                handle.write(b"\0")
    now = datetime.utcnow()
    segment = RecordingSegment(
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
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
        archive_root_id=stage49["root"].id,
        archive_root_resolution_status=ROOT_RESOLUTION_RESOLVED,
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
    )
    db.add(segment)
    return segment


def _create_plan(stage49, scope="camera") -> dict:
    payload = recordings_router.RecordingDeletionPlanRequest(
        scope=scope,
        camera=stage49["camera"].name if scope == "camera" else None,
    )
    return recordings_router.create_recording_deletion_plan(
        payload,
        db=stage49["db"],
        current_user=stage49["actor"],
    )


def _execute_plan(stage49, plan: dict) -> dict:
    return recordings_router.execute_recording_deletion_plan(
        plan["plan_id"],
        recordings_router.RecordingDeletionExecuteRequest(confirm=True),
        db=stage49["db"],
        current_user=stage49["actor"],
    )


def test_manual_camera_delete_processes_78_sparse_segments_over_10_gib(stage49):
    logical_size = 140 * 1024 * 1024
    for index in range(78):
        _segment(stage49, index, logical_size=logical_size)
    stage49["db"].commit()

    plan = _create_plan(stage49)
    assert plan["planned_count"] == 78
    assert plan["planned_bytes"] > 10 * 1024 * 1024 * 1024

    result = _execute_plan(stage49, plan)
    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["deleted_count"] == 78
    assert result["processed_count"] == 78
    assert result["skipped_count"] == 0
    assert result["failed_count"] == 0
    assert result["limit_exceeded"] is False
    assert result["limit_applied"]["max_bytes"] is None
    assert stage49["db"].query(RecordingSegment).filter(RecordingSegment.status == "deleted").count() == 78


def test_manual_delete_all_uses_keyset_batches_for_more_than_1000_segments(stage49):
    for index in range(1005):
        _segment(stage49, index, logical_size=1)
    stage49["db"].commit()

    plan = _create_plan(stage49, scope="all")
    result = _execute_plan(stage49, plan)

    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["deleted_count"] == 1005
    assert result["processed_count"] == 1005
    assert result["batch_count"] == 11
    assert result["sample_eligible_count"] == 1005
    assert result["sample_truncated"] is True
    assert len(result["items"]) == 100


def test_dynamic_plan_excludes_segment_finalized_after_preview(stage49):
    first = _segment(stage49, 1)
    stage49["db"].commit()
    plan = _create_plan(stage49)
    second = _segment(stage49, 2)
    stage49["db"].commit()

    result = _execute_plan(stage49, plan)
    stage49["db"].refresh(first)
    stage49["db"].refresh(second)

    assert result["status"] == "completed"
    assert result["deleted_count"] == 1
    assert first.status == "deleted"
    assert second.status == "finalized"
    assert (stage49["root_path"] / second.relative_path).is_file()


def test_consumed_plan_returns_same_terminal_result_without_second_mutation(stage49):
    segment = _segment(stage49, 1)
    stage49["db"].commit()
    plan = _create_plan(stage49)
    first = _execute_plan(stage49, plan)
    second = _execute_plan(stage49, plan)

    assert first == second
    assert first["deleted_count"] == 1
    stage49["db"].refresh(segment)
    assert segment.status == "deleted"


def test_expired_plan_returns_strict_audited_block_without_mutation(stage49, monkeypatch):
    segment = _segment(stage49, 1)
    stage49["db"].commit()
    monkeypatch.setattr(recording_operations, "PLAN_TTL_SECONDS", -1)
    plan = _create_plan(stage49)

    result = _execute_plan(stage49, plan)

    assert STRICT_RESULT_KEYS.issubset(result)
    assert result["status"] == "blocked"
    assert result["ok"] is False
    assert result["reason_counts"] == {"deletion_plan_expired": 1}
    assert result["deleted_count"] == 0
    stage49["db"].refresh(segment)
    assert segment.status == "finalized"
    assert (stage49["root_path"] / segment.relative_path).is_file()
    assert stage49["db"].query(AuditEvent).filter(AuditEvent.event_type == "recordings.bulk_delete_blocked").count() == 1


def test_changed_plan_scope_blocks_before_filesystem_mutation(stage49):
    segment = _segment(stage49, 1)
    stage49["db"].commit()
    plan = _create_plan(stage49)
    segment.size_bytes += 1
    stage49["db"].add(segment)
    stage49["db"].commit()

    result = _execute_plan(stage49, plan)

    assert STRICT_RESULT_KEYS.issubset(result)
    assert result["status"] == "blocked"
    assert result["reason_counts"] == {"deletion_plan_item_changed": 1}
    assert result["deleted_count"] == 0
    assert (stage49["root_path"] / segment.relative_path).is_file()


def test_exact_single_delete_is_idempotent_by_operation_identity(stage49):
    segment = _segment(stage49, 1)
    stage49["db"].commit()
    operation_id = "stage49-exact-single-operation"

    first = recordings_router.delete_recording(
        path=None,
        segment_id=segment.id,
        archive_root_id=stage49["root"].id,
        recording_ref=None,
        operation_id=operation_id,
        db=stage49["db"],
        current_user=stage49["actor"],
    )
    second = recordings_router.delete_recording(
        path=None,
        segment_id=segment.id,
        archive_root_id=stage49["root"].id,
        recording_ref=None,
        operation_id=operation_id,
        db=stage49["db"],
        current_user=stage49["actor"],
    )

    assert first == second
    assert STRICT_RESULT_KEYS.issubset(first)
    assert first["status"] == "completed"
    assert first["deleted_count"] == 1
    assert not (stage49["root_path"] / segment.relative_path).exists()


def test_missing_file_is_blocked_not_green_success(stage49):
    _segment(stage49, 1, create_file=False)
    stage49["db"].commit()
    result = _execute_plan(stage49, _create_plan(stage49))

    assert result["status"] == "blocked"
    assert result["ok"] is False
    assert result["deleted_count"] == 0
    assert result["skipped_reason_counts"] == {"file_missing": 1}


def test_plan_is_actor_bound(stage49):
    _segment(stage49, 1)
    stage49["db"].commit()
    plan = _create_plan(stage49)
    other = User(
        username="stage49_other",
        full_name="Other",
        password_hash="not-used",
        role="owner",
        is_active=True,
    )
    stage49["db"].add(other)
    stage49["db"].commit()
    stage49["db"].refresh(other)

    with pytest.raises(HTTPException) as blocked:
        recordings_router.execute_recording_deletion_plan(
            plan["plan_id"],
            recordings_router.RecordingDeletionExecuteRequest(confirm=True),
            db=stage49["db"],
            current_user=other,
        )
    assert blocked.value.status_code == 409
    assert blocked.value.detail["error"] == "deletion_plan_actor_mismatch"


def test_scope_overlap_is_hierarchical_without_blocking_unrelated_exact_sets(stage49):
    camera_scope = {"type": "camera", "camera_ids": [1], "root_ids": [], "segment_ids": []}
    same_camera_exact = {"type": "segments", "camera_ids": [1], "root_ids": ["root-a"], "segment_ids": [10]}
    other_exact = {"type": "segments", "camera_ids": [2], "root_ids": ["root-b"], "segment_ids": [11]}
    root_scope = {"type": "root", "camera_ids": [1], "root_ids": ["root-a"], "segment_ids": []}
    all_scope = {"type": "all", "camera_ids": [], "root_ids": [], "segment_ids": []}

    assert scopes_overlap(camera_scope, same_camera_exact) is True
    assert scopes_overlap(root_scope, same_camera_exact) is True
    assert scopes_overlap(camera_scope, other_exact) is False
    assert scopes_overlap(all_scope, other_exact) is True

    operation_id = new_operation_id("scope-camera")
    with destructive_scope_guard(operation_id, camera_scope, purpose="manual_camera"):
        with pytest.raises(DestructiveScopeConflict):
            with destructive_scope_guard(new_operation_id("scope-exact"), same_camera_exact, purpose="retention"):
                pass
        with destructive_scope_guard(new_operation_id("scope-other"), other_exact, purpose="manual_exact"):
            pass


@pytest.mark.skipif(os.name != "posix", reason="production coordinator uses POSIX flock")
def test_scope_coordinator_blocks_overlap_across_processes_and_allows_unrelated_scope(stage49):
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    root_scope = {"type": "root", "camera_ids": [1], "root_ids": ["root-a"], "segment_ids": []}
    overlapping = {"type": "segments", "camera_ids": [1], "root_ids": ["root-a"], "segment_ids": [10]}
    unrelated = {"type": "segments", "camera_ids": [2], "root_ids": ["root-b"], "segment_ids": [11]}

    with destructive_scope_guard(new_operation_id("scope-parent"), root_scope, purpose="archive_root_delete"):
        blocked_process = context.Process(
            target=_scope_process_worker,
            args=(str(settings.storage_install_control), new_operation_id("scope-child-blocked"), overlapping, output),
        )
        blocked_process.start()
        blocked_process.join(timeout=10)
        assert blocked_process.exitcode == 0
        assert output.get(timeout=2) == ("blocked", "destructive_scope_conflict")

        allowed_process = context.Process(
            target=_scope_process_worker,
            args=(str(settings.storage_install_control), new_operation_id("scope-child-allowed"), unrelated, output),
        )
        allowed_process.start()
        allowed_process.join(timeout=10)
        assert allowed_process.exitcode == 0
        assert output.get(timeout=2) == ("acquired", "")


def test_per_camera_problem_total_includes_root_unavailable_and_matches_breakdown(stage49, monkeypatch):
    _segment(stage49, 1)
    stage49["db"].commit()
    monkeypatch.setattr(
        storage_monitoring,
        "archive_root_runtime_access_state",
        lambda _root: {
            "read_access_state": "unavailable",
            "write_access_state": "unavailable",
            "access_state": "unavailable",
            "problem": "root_missing",
        },
    )
    summary = storage_monitoring.build_storage_monitoring_summary(
        stage49["db"],
        include_namespace_observations=False,
        write_audit=False,
    )
    row = next(item for item in summary["storage_operations"]["per_camera_usage"] if item["camera_id"] == stage49["camera"].id)
    assert row["problem_file_count"] == 1
    assert row["problem_counts"] == {"root_unavailable": 1}


def _run_cleanup_helper(tmp_path, *, foreign=False, marker_path="/Volume3/Stage49", target_exists=True, marker_symlink=False):
    mount = tmp_path / "Volume3"
    target = mount / "Stage49"
    mount.mkdir(parents=True, exist_ok=True)
    marker = target / ".km-vms-storage-root.json"
    if target_exists:
        (target / "kmvms" / "recordings").mkdir(parents=True)
        marker_payload = target / "marker-payload.json" if marker_symlink else marker
        marker_payload.write_text(
            "{\n"
            '  "schema_version": 1,\n'
            '  "product": "KM VMS",\n'
            f'  "selected_host_path": "{marker_path}",\n'
            '  "container_archive_path": "/storage/archive"\n'
            "}\n",
            encoding="utf-8",
        )
        if marker_symlink:
            marker.symlink_to(marker_payload)
        if foreign:
            (target / "user-note.txt").write_text("keep", encoding="utf-8")
    script = Path(__file__).resolve().parents[3] / "scripts" / "km-vms-storage-root-cleanup.sh"
    env = {**os.environ, "KM_VMS_SELECTED_MOUNT_CONTAINER": str(mount)}
    completed = subprocess.run(
        [
            "sh",
            str(script),
            "--folder-name",
            "Stage49",
            "--expected-host-path",
            "/Volume3/Stage49",
            "--operation-id",
            "stage49-cleanup-operation",
            "--archive-root-id",
            "stage49-root",
            "--allow-missing-marker",
            "false",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    facts = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    return completed, facts, target, marker


def test_root_cleanup_helper_removes_only_marker_and_empty_root(tmp_path):
    completed, facts, target, _marker = _run_cleanup_helper(tmp_path)
    assert completed.returncode == 0
    assert facts["cleanup_status"] == "completed_removed"
    assert facts["retry_mode"] == "none"
    assert facts["next_action"] == "close"
    assert facts["retry_available"] == "false"
    assert facts["marker_removed"] == "true"
    assert facts["root_directory_removed"] == "true"
    assert not target.exists()


def test_root_cleanup_helper_preserves_foreign_content_and_reports_truthfully(tmp_path):
    completed, facts, target, marker = _run_cleanup_helper(tmp_path, foreign=True)
    assert completed.returncode == 0
    assert facts["cleanup_status"] == "completed_preserved_nonempty"
    assert facts["retry_mode"] == "none"
    assert facts["root_directory_removed"] == "false"
    assert target.is_dir()
    assert (target / "user-note.txt").read_text(encoding="utf-8") == "keep"
    assert not marker.exists()


def test_root_cleanup_helper_fails_closed_on_marker_path_mismatch(tmp_path):
    completed, facts, target, marker = _run_cleanup_helper(tmp_path, marker_path="/Volume3/Other")
    assert completed.returncode != 0
    assert facts["cleanup_status"] == "partial_cleanup"
    assert facts["reason"] == "root_marker_path_mismatch"
    assert facts["retry_mode"] == "none"
    assert facts["retry_available"] == "false"
    assert target.is_dir()
    assert marker.is_file()


def test_root_cleanup_helper_rejects_symlink_marker(tmp_path):
    completed, facts, target, marker = _run_cleanup_helper(tmp_path, marker_symlink=True)
    assert completed.returncode != 0
    assert facts["reason"] == "root_marker_symlink_rejected"
    assert facts["retry_mode"] == "none"
    assert target.is_dir()
    assert marker.is_symlink()


def test_root_cleanup_helper_is_idempotent_when_target_is_already_absent(tmp_path):
    completed, facts, target, _marker = _run_cleanup_helper(tmp_path, target_exists=False)
    assert completed.returncode == 0
    assert facts["cleanup_status"] == "completed_removed"
    assert facts["reason"] == "already_absent"
    assert facts["retry_mode"] == "none"
    assert not target.exists()


def test_empty_unmounted_inactive_root_uses_host_helper_without_api_access_preflight(stage49, monkeypatch):
    inactive = ArchiveRoot(
        id="stage49-inactive-unmounted",
        label="Stage 4.9 inactive",
        root_path=str(stage49["tmp_path"] / "Volume2" / "KM-VMS-Stage-13.5-4.9-empty"),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=False,
        is_readable=False,
        is_writable=False,
        is_available=False,
        physical_identity="stage49-volume-2",
    )
    stage49["db"].add(inactive)
    stage49["db"].commit()
    usage = storage_router.root_usage(stage49["db"], inactive)
    assert usage["root_access_problem_count"] == 0
    assert usage["root_access_problem"] is None
    monkeypatch.setattr(
        storage_router,
        "verify_archive_root_access",
        lambda *_args, **_kwargs: pytest.fail("empty unmounted root must be delegated to the host helper"),
    )
    monkeypatch.setattr(
        storage_router.setup_storage,
        "request_archive_root_cleanup",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "cleanup_status": "completed_removed",
            "reason": "already_absent",
            "marker_removed": True,
            "root_directory_removed": True,
            "root_directory_preserved_reason": "",
            "retry_available": False,
        },
    )
    monkeypatch.setattr(storage_router, "write_archive_roots_runtime_files", lambda _db: None)

    result = storage_router._delete_inactive_root(stage49["db"], inactive, stage49["actor"])

    assert result["ok"] is True
    assert result["cleanup_status"] == "completed_removed"
    stage49["db"].refresh(inactive)
    assert inactive.retired_at is not None
