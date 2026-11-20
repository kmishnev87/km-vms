"""Coordinate confirmed camera reassignment and safe retirement of one archive root."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, or_

from app.core.permissions import user_has_permission
from app.db.session import SessionLocal
from app.models.archive_migration import ArchiveMigrationPlan
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.storage_operation import StorageOperation
from app.models.user import User
from app.services.archive_root_activation import (
    ArchiveRootMutationConflict, archive_root_activation_public_status, archive_root_mutation_guard,
)
from app.services.audit_log import create_event
from app.services.camera_archive_placement import archive_target_status, current_default_id, target_catalog
from app.services.migration_contracts import (
    ACTIVE_JOB_STATES, ELIGIBLE_SEGMENT_STATUSES, PLAN_TERMINAL_STATUSES, RECENT_WRITE_WINDOW,
)
from app.services.recording_storage import archive_root_host_display_path
from app.services.recorder_runtime_status import current_recording_jobs
from app.services.storage_filesystem import archive_roots_overlap
from app.services.storage_operations_foundation import database_now, safe_reason_code


REMOVING = "removal_pending"
REQUEST_KEY = "confirmed_removal_v1"
RECHECK_AFTER = timedelta(seconds=8)
REQUEST_TIMEOUT = timedelta(hours=26)


def removal_request(root):
    value = (root.retirement_cleanup_result or {}).get(REQUEST_KEY)
    return value if isinstance(value, dict) else None


def _root(db, root_id):
    root = db.get(ArchiveRoot, root_id)
    if not root:
        raise HTTPException(404, detail={"error": "archive_root_not_found"})
    return root


def _bound_cameras(db, root, *, lock=False):
    assigned = Camera.recording_archive_root_id == root.id
    if root.is_active:
        assigned = or_(assigned, Camera.recording_archive_root_id.is_(None))
    query = db.query(Camera).filter(Camera.deleted_at.is_(None), assigned).order_by(Camera.id)
    return (query.with_for_update().populate_existing() if lock else query).all()


def root_recordings(db, root_id):
    return db.query(RecordingSegment).filter(
        RecordingSegment.archive_root_id == root_id, RecordingSegment.deleted_at.is_(None),
        RecordingSegment.status != "deleted",
    )


def recording_scope(db, root_id):
    """Hash the exact set, independent of a segment's transition to finalized."""
    ids = root_recordings(db, root_id).with_entities(RecordingSegment.id).order_by(RecordingSegment.id).yield_per(1000)
    return exact_recording_scope(segment_id for (segment_id,) in ids)


def exact_recording_scope(ids):
    digest = hashlib.sha256()
    count = 0
    for segment_id in ids:
        digest.update(f"{segment_id}\n".encode("ascii"))
        count += 1
    return count, digest.hexdigest()


def _scope_hash(root, cameras, digest):
    return hashlib.sha256(repr((root.id, root.physical_identity, root.root_path, bool(root.is_active),
                                 tuple(camera.id for camera in cameras), digest)).encode("utf-8")).hexdigest()


def _selectable(db, source, target):
    return (target.id != source.id and target.retired_at is None and not target.retirement_status
            and not archive_roots_overlap(source, target)
            and archive_target_status(target, current_default_id(db))["writable"])


def preview_removal(db, root_id, actor):
    root = _root(db, root_id)
    if root.retired_at is not None:
        raise HTTPException(404, detail={"error": "archive_root_not_found"})
    cameras = _bound_cameras(db, root)
    count, digest = recording_scope(db, root.id)
    catalog = target_catalog(db, actor)["items"]
    eligible = {item["id"] for item in catalog if item["writable"] and item["id"] != root.id}
    # Show only roots to which the backend can actually send writes.
    choices = [item for item in catalog if item["id"] in eligible and not archive_roots_overlap(root, db.get(ArchiveRoot, item["id"]))]
    return {"root_id": root.id, "path": archive_root_host_display_path(root),
            "cameras": [{"id": camera.id, "name": camera.name} for camera in cameras],
            "recording_count": count, "requires_target": bool(root.is_active or cameras),
            "targets": [{"id": item["id"], "path": item["path"]} for item in choices],
            "scope_hash": _scope_hash(root, cameras, digest),
            "operation": public_removal(db, root.id, root.retirement_operation_id)
            if root.retirement_status == REMOVING else None}


def public_removal(db, root_id, operation_id):
    root = _root(db, root_id)
    # A restarted coordinator or a later attempt must not make an old receipt
    # report the outcome of a different operation.
    if root.retirement_operation_id == operation_id:
        if root.retired_at is not None:
            return {"id": operation_id, "root_id": root_id, "status": "completed"}
        request = removal_request(root)
        if request:
            return {"id": operation_id, "root_id": root_id, "mode": request["mode"],
                    "status": request["status"], "reason": request.get("reason")}
    operation = db.get(StorageOperation, operation_id) if operation_id else None
    if operation and operation.operation_type == "archive_root_delete" and root_id in (operation.scope or {}).get("root_ids", ()):
        return {"id": operation_id, "root_id": root_id, "status": operation.status, "reason": operation.reason_code}
    raise HTTPException(404, detail={"error": "archive_root_removal_not_found"})


def start_removal(db, root_id, payload, actor):
    operation_id = f"retire-{payload.request_id.hex}"
    target_id = payload.target_root_id
    try:
        with archive_root_mutation_guard("archive_root_removal"):
            root = _root(db, root_id)
            if root.retirement_operation_id == operation_id:
                request = removal_request(root)
                if request and any(request.get(key) != value for key, value in (
                    ("mode", payload.mode), ("target", target_id), ("scope_hash", payload.scope_hash), ("actor_id", actor.id),
                )):
                    raise HTTPException(409, detail={"error": "archive_root_removal_changed"})
                return public_removal(db, root_id, operation_id)
            locked_cameras = _bound_cameras(db, root, lock=True)
            roots = {row.id: row for row in db.query(ArchiveRoot).filter(
                ArchiveRoot.id.in_([value for value in (root_id, target_id) if value]),
            ).order_by(ArchiveRoot.id).with_for_update().populate_existing().all()}
            root = roots[root_id]
            if root.retired_at is not None or root.retirement_status in {REMOVING, "deleting"}:
                raise HTTPException(409, detail={"error": "archive_root_removal_in_progress"})
            if any(removal_request(other) and other.retirement_status == REMOVING
                   and removal_request(other).get("target") == root_id
                   for other in db.query(ArchiveRoot).filter(ArchiveRoot.retirement_status == REMOVING)):
                raise HTTPException(409, detail={"error": "archive_root_removal_target_in_use"})
            cameras = _bound_cameras(db, root)
            if [camera.id for camera in locked_cameras] != [camera.id for camera in cameras]:
                raise HTTPException(409, detail={"error": "archive_root_removal_changed"})
            count, digest = recording_scope(db, root.id)
            if payload.scope_hash != _scope_hash(root, cameras, digest) or payload.recording_count != count:
                raise HTTPException(409, detail={"error": "archive_root_removal_changed"})
            target = roots.get(target_id)
            if (root.is_active or cameras or payload.mode == "move") and target is None:
                raise HTTPException(409, detail={"error": "archive_root_removal_target_required"})
            if target is not None and not _selectable(db, root, target):
                raise HTTPException(409, detail={"error": "camera_archive_target_unavailable"})
            if payload.mode == "delete" and count and not payload.confirm_delete:
                raise HTTPException(409, detail={"error": "archive_root_removal_delete_confirmation_required"})
            target_id = target.id if target else None
            now = database_now(db)
            request = {"mode": payload.mode, "scope_hash": payload.scope_hash, "records": digest,
                       "count": count, "target": target_id, "actor_id": actor.id,
                       "camera_ids": [camera.id for camera in cameras], "status": "queued",
                       "at": now.isoformat(), "switch_base": bool(root.is_active)}
            root.retirement_status = REMOVING
            root.retirement_operation_id = operation_id
            root.retirement_problem = None
            root.retirement_cleanup_result = {REQUEST_KEY: request}
            for camera in cameras:
                camera.recording_archive_root_id = target_id
            event = create_event(
                db=db, actor=actor, category="storage", event_type="archive_root.removal_requested",
                message_ru="Запрошено отключение папки архива", message_en="Archive folder removal requested",
                target_type="archive_root", target_id=root.id, target_name=root.label,
                metadata={"operation_id": operation_id, "target_root_id": target_id, "mode": payload.mode,
                          "camera_ids": request["camera_ids"], "recording_count": count}, commit=False,
            )
            if event is None:
                raise RuntimeError("archive_root_removal_audit_failed")
            db.commit()
            return public_removal(db, root_id, operation_id)
    except ArchiveRootMutationConflict as exc:
        raise HTTPException(409, detail=exc.blocker) from exc


def _source_busy(db, root, request, now):
    scope = root_recordings(db, root.id)
    if scope.filter(RecordingSegment.status.in_(("starting", "writing", "stopping", "restarting"))).limit(1).first():
        return True
    jobs = [writer for writer in current_recording_jobs(db).values() if writer.state in ACTIVE_JOB_STATES]
    if jobs and scope.filter(RecordingSegment.job_id.in_([writer.id for writer in jobs])).limit(1).first():
        return True
    # The Recorder might be closing a writer that has not indexed its first
    # segment yet. Look only at current-instance jobs predating the choice.
    admitted = datetime.fromisoformat(request["at"])
    for writer in jobs:
        if writer.camera_id in request["camera_ids"] and writer.started_at <= admitted and not db.query(
                RecordingSegment.id).filter(RecordingSegment.job_id == writer.id).limit(1).first():
            return True
    if request["mode"] == "move":
        return scope.filter(RecordingSegment.status.in_(tuple(ELIGIBLE_SEGMENT_STATUSES)),
            func.coalesce(RecordingSegment.updated_at, RecordingSegment.finalized_at,
                          RecordingSegment.created_at, RecordingSegment.started_at) > now - RECENT_WRITE_WINDOW).limit(1).first() is not None
    return False


def _fail(db, root, request, reason):
    reason = safe_reason_code(reason) or "archive_root_removal_failed"
    root.retirement_problem = reason
    if root.retirement_status == REMOVING:
        root.retirement_status = None  # Preserve the archive and let the operator try again.
    root.retirement_cleanup_result = {**(root.retirement_cleanup_result or {}),
                                      REQUEST_KEY: {**request, "status": "blocked", "reason": reason}}
    create_event(db=db, actor=None, category="storage", event_type="archive_root.removal_blocked", severity="warning",
                 message_ru="Папка архива не отключена", message_en="Archive folder was not removed",
                 target_type="archive_root", target_id=root.id, target_name=root.label,
                 metadata={"operation_id": root.retirement_operation_id, "reason": reason}, commit=False)
    db.commit()
    return True


def _step(db, root, request, assert_owned):
    from app.routers.storage import _execute_archive_root_delete, _schedule_archive_root_activation, ArchiveRootDeleteRequest
    from app.services.archive_migration import queue_migration_apply, request_migration_plan

    actor = db.get(User, request["actor_id"])
    if actor is None or not actor.is_active or not all(user_has_permission(actor.role, permission) for permission in (
            "manage_settings", "manage_cameras", "delete_recordings")):
        return _fail(db, root, request, "archive_root_removal_permission_lost")
    now = database_now(db)
    elapsed = now - datetime.fromisoformat(request["at"])
    if elapsed > REQUEST_TIMEOUT:
        return _fail(db, root, request, "archive_root_removal_expired")
    if elapsed < RECHECK_AFTER or _source_busy(db, root, request, now):
        return False
    if _bound_cameras(db, root):
        return _fail(db, root, request, "archive_root_removal_changed")
    target_id, operation_id = request["target"], root.retirement_operation_id
    if target_id and not _selectable(db, root, _root(db, target_id)):
        return _fail(db, root, request, "camera_archive_target_unavailable")
    if request["switch_base"]:
        activation_id = f"base-{operation_id}"
        state = archive_root_activation_public_status()
        if state.get("operation_id") == activation_id:
            if state.get("status") not in {"completed", "queued", "running"}:
                return _fail(db, root, request, state.get("reason_code") or "archive_root_removal_base_switch_failed")
            if state.get("status") != "completed":
                return False
            if root.is_active or current_default_id(db) != target_id:
                return _fail(db, root, request, "archive_root_removal_base_switch_failed")
        elif root.is_active:
            assert_owned()
            _schedule_archive_root_activation(target_id, db, actor, operation_id=activation_id)
            return True
        else:
            return _fail(db, root, request, "archive_root_removal_base_switch_failed")
    count, digest = recording_scope(db, root.id)
    if request["mode"] == "move":
        plan = db.query(ArchiveMigrationPlan).filter(
            ArchiveMigrationPlan.idempotency_key == operation_id, ArchiveMigrationPlan.actor_user_id == actor.id,
            ArchiveMigrationPlan.source_root_id == root.id, ArchiveMigrationPlan.target_root_id == target_id,
        ).one_or_none()
        if plan is None and count:
            assert_owned()
            request_migration_plan(db, actor=actor, source_root_id=root.id, target_root_id=target_id,
                                   idempotency_key=operation_id)
            return True
        if plan is not None:
            if plan.status == "ready" and not plan.excluded_count:
                assert_owned()
                queue_migration_apply(db, actor=actor, plan_id=plan.id, expected_hash=plan.canonical_hash,
                                      idempotency_key=operation_id)
                return True
            if plan.status == "ready_with_exclusions" or plan.status in PLAN_TERMINAL_STATUSES - {"completed"}:
                return _fail(db, root, request, plan.reason_code or "archive_root_removal_move_incomplete")
            if plan.status != "completed":
                return False
            if count or plan.cleanup_pending or plan.excluded_count:
                return _fail(db, root, request, "archive_root_removal_move_incomplete")
    elif count != request["count"] or digest != request["records"]:
        return _fail(db, root, request, "archive_root_removal_changed")
    # Use the same installer-owned removal and deletion worker/leases as the
    # old path. The final guarded pass is also given an exact count and hash;
    # under a move instruction it is only ever allowed to retire an empty root.
    assert_owned()
    _execute_archive_root_delete(root.id, ArchiveRootDeleteRequest(confirm=True, operation_id=operation_id),
                                 db, actor, coordinated_scope=(count, digest))
    return True


def process_one_removal(assert_owned):
    """Run in the existing archive worker, with no new timer or worker pool."""
    from app.services.archive_migration import ArchiveMigrationBlocked
    from app.services.storage_operation_conflicts import StorageOperationConflict

    with SessionLocal() as db:
        roots = db.query(ArchiveRoot).filter(ArchiveRoot.retired_at.is_(None),
            ArchiveRoot.retirement_status.in_((REMOVING, "deleting"))).order_by(
            ArchiveRoot.updated_at, ArchiveRoot.id).limit(32).all()
        for root in roots:
            request = removal_request(root)
            if not request or request.get("status") != "queued":
                continue
            root_id = root.id
            try:
                if _step(db, root, request, assert_owned):
                    return True
            except (ArchiveRootMutationConflict, StorageOperationConflict):
                db.rollback()
            except (ArchiveMigrationBlocked, HTTPException) as exc:
                db.rollback()
                detail = exc.detail if isinstance(exc, HTTPException) else exc.reason_code
                reason = safe_reason_code(detail, fallback="archive_root_removal_failed")
                if reason in {"archive_root_mutation_conflict", "archive_root_operation_in_progress",
                              "destructive_scope_conflict", "storage_operation_conflict",
                              "archive_root_activation_already_running", "active_recording_jobs",
                              "active_recorder_write_conflict", "archive_root_delete_active_writes_blocked",
                              "already_running"}:
                    continue
                root = db.get(ArchiveRoot, root_id)
                if root and root.retired_at is None:
                    return _fail(db, root, request, reason)
        return False
