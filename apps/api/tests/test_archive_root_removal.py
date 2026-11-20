import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models.archive_migration import ArchiveMigrationPlan
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.user import User
from app.routers import storage
from app.services import archive_migration, archive_root_removal as removal, camera_archive_placement, setup_storage
from app.services.recording_storage import resolve_segment_file_path
from test_stage13_5_4_10_4_archive_migration import add_camera, add_segment, stage4104, stage4104_postgres


@pytest.fixture
def setup(stage4104, stage4104_postgres, monkeypatch):
    ctx = stage4104
    # The coordinator and its fencing leases deliberately use separate
    # connections. Exercise the product's PostgreSQL locking semantics.
    ctx.owner = User(**{field.name: getattr(ctx.owner, field.name) for field in User.__table__.columns})
    ctx.roots = {key: ArchiveRoot(**{field.name: getattr(root, field.name) for field in ArchiveRoot.__table__.columns})
                 for key, root in ctx.roots.items()}
    ctx.db, ctx.Session, ctx.engine = stage4104_postgres.db, stage4104_postgres.Session, stage4104_postgres.engine
    ctx.db.add_all([ctx.owner, *ctx.roots.values()])
    ctx.db.commit()
    monkeypatch.setattr(archive_migration, "SessionLocal", ctx.Session)
    monkeypatch.setattr(removal, "SessionLocal", ctx.Session)
    monkeypatch.setattr(removal, "RECHECK_AFTER", timedelta(0))
    monkeypatch.setattr(camera_archive_placement, "archive_root_runtime_mount_path", lambda root: Path(root.root_path))
    # Only the privileged NAS helper receipt is mocked. File operations,
    # manifests and operation leases all use separate pytest fixture paths.
    cleanup_calls = []

    def cleanup(root, *, operation_id, **_):
        cleanup_calls.append((root.id, operation_id))
        return {"status": "completed", "cleanup_status": "completed_preserved_nonempty", "operation_id": operation_id,
                "marker_removed": True, "root_directory_removed": False, "root_directory_preserved_reason": "nonempty"}

    monkeypatch.setattr(setup_storage, "request_archive_root_cleanup", cleanup)
    ctx.cleanup_calls = cleanup_calls
    return ctx


def assign(ctx, source="target", name="Dahua 5MP"):
    camera = add_camera(ctx, name=name)
    camera.recording_archive_root_id = ctx.roots[source].id
    ctx.db.commit()
    return camera


def preview(ctx, name="target"):
    return removal.preview_removal(ctx.db, ctx.roots[name].id, ctx.owner)


def choice(model, target_id, *, move=False, confirmed=False):
    return storage.ArchiveRootRemovalRequest(request_id=uuid.uuid4(), target_root_id=target_id,
        mode="move" if move else "delete", confirm_delete=confirmed,
        recording_count=model["recording_count"], scope_hash=model["scope_hash"])


def reopen(ctx):
    ctx.db.rollback()
    ctx.db.expire_all()


def test_preview_is_read_only_and_delete_requires_a_destination_and_confirmed_exact_records(setup):
    ctx = setup
    camera = assign(ctx)
    segment, path = add_segment(ctx, camera, root_name="target")
    model = preview(ctx)
    source = ctx.roots["target"]
    assert model["cameras"] == [{"id": camera.id, "name": camera.name}]
    assert model["recording_count"] == 1 and model["requires_target"]
    assert {item["id"] for item in model["targets"]} == {ctx.roots["source"].id, ctx.roots["third"].id}
    assert source.retirement_status is None and path.exists()
    with pytest.raises(HTTPException) as error:
        removal.start_removal(ctx.db, source.id, choice(model, None, confirmed=True), ctx.owner)
    assert error.value.detail["error"] == "archive_root_removal_target_required"
    reopen(ctx)
    with pytest.raises(HTTPException) as error:
        removal.start_removal(ctx.db, source.id, choice(model, ctx.roots["source"].id), ctx.owner)
    assert error.value.detail["error"] == "archive_root_removal_delete_confirmation_required"
    reopen(ctx)
    assert camera.recording_archive_root_id == source.id and path.exists()
    assert segment.deleted_at is None and not ctx.cleanup_calls


def test_delete_reassigns_camera_and_deletes_only_confirmed_root_even_after_worker_restarts(setup):
    ctx = setup
    camera = assign(ctx)
    source, destination = ctx.roots["target"], ctx.roots["source"]
    segment, path = add_segment(ctx, camera, root_name="target")
    kept, kept_path = add_segment(ctx, camera, root_name="source")
    unrelated = ctx.paths["target"] / "not-owned.txt"
    unrelated.write_text("do not remove")
    payload = choice(preview(ctx), destination.id, confirmed=True)
    job = removal.start_removal(ctx.db, source.id, payload, ctx.owner)
    assert job["status"] == "queued" and removal.start_removal(ctx.db, source.id, payload, ctx.owner) == job
    assert camera.recording_archive_root_id == destination.id and path.exists()
    with pytest.raises(HTTPException) as error:
        storage.delete_archive_root(source.id, storage.ArchiveRootDeleteRequest(confirm=True), ctx.db, ctx.owner)
    assert error.value.detail["error"] == "archive_root_removal_in_progress"
    reopen(ctx)
    assert removal.process_one_removal(lambda: None)
    reopen(ctx)
    assert removal.public_removal(ctx.db, source.id, job["id"])["status"] == "completed"
    assert ctx.db.get(RecordingSegment, segment.id).deleted_at is not None and not path.exists()
    assert ctx.db.get(RecordingSegment, kept.id).deleted_at is None and kept_path.exists()
    assert source.retired_at is not None and unrelated.read_text() == "do not remove"
    assert ctx.cleanup_calls == [(source.id, job["id"])]


def test_new_recording_after_delete_confirmation_is_never_deleted(setup):
    ctx = setup
    camera = assign(ctx)
    original, original_path = add_segment(ctx, camera, root_name="target")
    source = ctx.roots["target"]
    model = preview(ctx)
    job = removal.start_removal(ctx.db, source.id, choice(model, ctx.roots["source"].id, confirmed=True), ctx.owner)
    later, later_path = add_segment(ctx, camera, root_name="target", name="late-finalizer.mkv")
    assert removal.process_one_removal(lambda: None)
    reopen(ctx)
    result = removal.public_removal(ctx.db, source.id, job["id"])
    assert result["status"] == "blocked" and result["reason"] == "archive_root_removal_changed"
    assert original_path.exists() and later_path.exists() and source.retired_at is None
    assert original.deleted_at is later.deleted_at is None and not ctx.cleanup_calls


def test_root_move_uses_real_migration_and_covers_archived_camera_files(setup):
    ctx = setup
    camera = assign(ctx)
    archived_camera = add_camera(ctx, name="Old lens", soft_deleted=True)
    first, first_path = add_segment(ctx, camera, root_name="target", content=b"present camera video")
    second, second_path = add_segment(ctx, archived_camera, root_name="target", content=b"archived camera video")
    source, destination = ctx.roots["target"], ctx.roots["third"]
    other, other_path = add_segment(ctx, camera, root_name="source", name="unchanged.mkv")
    job = removal.start_removal(ctx.db, source.id, choice(preview(ctx), destination.id, move=True), ctx.owner)
    assert removal.process_one_removal(lambda: None)
    reopen(ctx)
    plan = ctx.db.query(ArchiveMigrationPlan).one()
    assert plan.selected_camera_id is None and plan.source_root_id == source.id
    for _ in range(8):
        archive_migration._prepare_one_plan()
        reopen(ctx)
        if plan.status != "building":
            break
    assert plan.status == "ready" and plan.item_count == 2
    assert not ctx.cleanup_calls and first_path.exists() and second_path.exists()
    assert removal.process_one_removal(lambda: None)
    assert archive_migration._run_one_operation()
    reopen(ctx)
    assert plan.status == "completed"
    assert removal.process_one_removal(lambda: None)
    reopen(ctx)
    assert removal.public_removal(ctx.db, source.id, job["id"])["status"] == "completed"
    assert not first_path.exists() and not second_path.exists()
    assert all(record.archive_root_id == destination.id and record.deleted_at is None for record in (first, second))
    assert resolve_segment_file_path(ctx.db, first, require_exists=True).read_bytes() == b"present camera video"
    assert resolve_segment_file_path(ctx.db, second, require_exists=True).read_bytes() == b"archived camera video"
    assert other_path.exists() and other.archive_root_id == ctx.roots["source"].id


def test_partial_archive_move_never_falls_back_to_delete(setup):
    ctx = setup
    camera = assign(ctx)
    original, original_path = add_segment(ctx, camera, root_name="target")
    source = ctx.roots["target"]
    job = removal.start_removal(ctx.db, source.id, choice(preview(ctx), ctx.roots["source"].id, move=True), ctx.owner)
    assert removal.process_one_removal(lambda: None)
    reopen(ctx)
    plan = ctx.db.query(ArchiveMigrationPlan).one()
    plan.status = "partial"
    ctx.db.commit()
    assert removal.process_one_removal(lambda: None)
    reopen(ctx)
    assert removal.public_removal(ctx.db, source.id, job["id"])["status"] == "blocked"
    assert original_path.exists() and original.deleted_at is None and source.retired_at is None and not ctx.cleanup_calls


def test_installer_binding_is_rotated_before_default_root_can_retire(setup, monkeypatch):
    ctx = setup
    camera = add_camera(ctx, name="Implicit camera")
    initial, destination = ctx.roots["source"], ctx.roots["target"]
    assert camera.recording_archive_root_id is None
    job = removal.start_removal(ctx.db, initial.id, choice(preview(ctx, "source"), destination.id, move=True), ctx.owner)
    seen = []
    receipt = {"status": "idle"}
    monkeypatch.setattr(removal, "archive_root_activation_public_status", lambda: receipt)

    def activation(root_id, db, actor, *, operation_id):
        seen.append((root_id, actor.id, operation_id))
        receipt.update(status="running", operation_id=operation_id)
        return dict(receipt)

    monkeypatch.setattr(storage, "_schedule_archive_root_activation", activation)
    assert removal.process_one_removal(lambda: None)
    assert seen == [(destination.id, ctx.owner.id, f"base-{job['id']}")]
    assert not removal.process_one_removal(lambda: None)
    reopen(ctx)
    assert initial.retired_at is None and camera.recording_archive_root_id == destination.id and not ctx.cleanup_calls
    initial.is_active, destination.is_active = False, True
    ctx.db.commit()
    receipt.update(status="completed")
    assert removal.process_one_removal(lambda: None)
    reopen(ctx)
    assert initial.retired_at is not None and destination.is_active


def test_retiring_root_is_not_reassignable_while_waiting_for_an_open_file(setup):
    ctx = setup
    camera = assign(ctx)
    segment, path = add_segment(ctx, camera, root_name="target", status="writing")
    source = ctx.roots["target"]
    job = removal.start_removal(ctx.db, source.id, choice(preview(ctx), ctx.roots["source"].id, confirmed=True), ctx.owner)
    assert not removal.process_one_removal(lambda: None)
    reopen(ctx)
    assert removal.public_removal(ctx.db, source.id, job["id"])["status"] == "queued"
    assert camera_archive_placement.archive_target_status(source, ctx.roots["source"].id)["state"] == "removing"
    assert path.exists() and segment.deleted_at is None and not ctx.cleanup_calls
