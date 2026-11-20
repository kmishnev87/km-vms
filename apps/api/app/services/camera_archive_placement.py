"""Camera destinations are root identities, never host paths supplied by a client."""
import os
from pathlib import Path
from typing import Annotated

from fastapi import HTTPException
from pydantic import StringConstraints
from sqlalchemy import func

from app.core.config import settings
from app.core.permissions import user_has_permission
from app.models.archive_migration import ArchiveMigrationPlan
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.services.archive_root_provisioning import provision_status
from app.services.recording_storage import (
    archive_root_host_display_path, archive_root_runtime_mount_path, root_status,
)

RootId = Annotated[str, StringConstraints(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9_.-]+$")]
ARCHIVE_TARGET_FAILURES = frozenset({
    "camera_archive_root_unavailable", "camera_archive_root_read_only", "camera_archive_root_low_disk",
})


def storage_route_evidence(camera, recorder):
    value = getattr(camera, "recording_storage_state", None)
    if not isinstance(value, dict) or not camera.enabled or not recorder.get("heartbeat_current"):
        return None
    if value.get("instance") != recorder.get("current_recorder_instance_id"):
        return None
    state = value.get("status")
    if state not in {"blocked", "waiting", "writing", "returning"}:
        return None
    confirmed = bool(value.get("job_id") and value["job_id"] == recorder.get("job_id")
                     and recorder.get("confirmed_recording"))
    return {"status": state if state != "writing" or confirmed else "waiting",
            "reason": value.get("reason") if value.get("reason") in ARCHIVE_TARGET_FAILURES else None,
            "primary_root_id": value.get("primary_root_id"), "effective_root_id": value.get("effective_root_id"),
            "actual_root_id": value.get("actual_root_id") if confirmed else None}


def current_default_id(db):
    return db.query(ArchiveRoot.id).filter(ArchiveRoot.is_active.is_(True), ArchiveRoot.retired_at.is_(None)).order_by(
        ArchiveRoot.updated_at.desc(), ArchiveRoot.id).limit(1).scalar()


def can_move_camera_archive(actor):
    return bool(actor and actor.is_active and all(user_has_permission(actor.role, permission) for permission in (
        "manage_cameras", "manage_settings", "delete_recordings")))


def archive_target_status(root, default_id):
    runtime = Path(settings.storage_root) if root.id == default_id else archive_root_runtime_mount_path(root)
    status = root_status(runtime)
    if root.retirement_status == "removal_pending" or (root.retirement_status == "deleting"
            and str(root.retirement_operation_id or "").startswith("retire-")):
        return {**status, "writable": False, "state": "removing", "problem": None}
    attachment = provision_status(root.id)
    if attachment and attachment["status"] == "provisioning":
        return {**status, "writable": False, "state": "provisioning", "problem": None}
    if root.retirement_status:
        status.update(writable=False, problem="camera_archive_target_unavailable")
    if attachment and attachment["status"] == "failed":
        return {**status, "writable": False, "state": "unavailable", "problem": attachment["reason"]}
    if not status["writable"]:
        return {**status, "state": "read_only" if status["readable"] else "unavailable"}
    try:
        space = os.statvfs(runtime)
        if space.f_flag & os.ST_RDONLY:
            return {**status, "writable": False, "state": "read_only", "problem": "archive_root_not_writable"}
        free = 100 * space.f_bavail / space.f_blocks if space.f_blocks else 0
        if free < 10:
            return {**status, "writable": free >= 1, "state": "low_space", "problem": "camera_archive_root_low_disk"}
    except OSError:
        return {**status, "writable": False, "state": "unavailable", "problem": "root_missing"}
    status["state"] = "ready"
    return status


def target_catalog(db, actor):
    default_id = current_default_id(db)
    roots = db.query(ArchiveRoot).filter(ArchiveRoot.retired_at.is_(None)).order_by(
        ArchiveRoot.is_active.desc(), ArchiveRoot.label, ArchiveRoot.id).all()
    options = []
    for root in roots:
        access = archive_target_status(root, default_id)
        options.append({"id": root.id, "path": archive_root_host_display_path(root),
                        "writable": access["writable"], "problem": access["problem"], "state": access["state"]})
    return {"items": options, "default_root_id": default_id, "can_move": can_move_camera_archive(actor)}


def validate_target(db, value, *, existing=None):
    effective = value or current_default_id(db)
    if value == existing:
        return effective
    root = (db.query(ArchiveRoot).filter(ArchiveRoot.id == effective)
            .with_for_update().populate_existing().one_or_none()) if effective else None
    if root is None or root.retired_at is not None or root.retirement_status:
        raise HTTPException(409, detail={"code": "camera_archive_target_unknown"})
    if not archive_target_status(root, current_default_id(db))["writable"]:
        raise HTTPException(409, detail={"code": "camera_archive_target_unavailable"})
    return effective


def initial_camera_target(db, declared):
    if declared:
        return validate_target(db, declared)
    catalog = target_catalog(db, None)
    ready = [item["id"] for item in catalog["items"] if item["writable"]]
    if len(ready) == 1:
        return validate_target(db, ready[0])
    # Old clients did not send a destination. Do not silently route them
    # somewhere else; keep this compatibility with their visible default.
    if catalog["default_root_id"] in ready:
        return validate_target(db, catalog["default_root_id"])
    return None


def move_source_counts(db, camera_id, target_id):
    return db.query(RecordingSegment.archive_root_id, func.count(RecordingSegment.id)).filter(
        RecordingSegment.camera_id == camera_id,
        RecordingSegment.deleted_at.is_(None),
        RecordingSegment.source == "recorder",
        RecordingSegment.ownership == "KM VMS",
        RecordingSegment.status.in_(("writing", "finalized")),
        RecordingSegment.archive_root_id.is_not(None),
        RecordingSegment.archive_root_id != target_id,
    ).group_by(RecordingSegment.archive_root_id).all()


def archive_move_offer(db, camera, target_id, actor):
    if not target_id or not can_move_camera_archive(actor):
        return None
    sources = move_source_counts(db, camera.id, target_id)
    target = db.get(ArchiveRoot, target_id)
    if not sources or target is None:
        return None
    return {"camera_id": camera.id, "camera_name": camera.name, "target_root_id": target.id,
            "target_path": archive_root_host_display_path(target), "recording_count": sum(count for _, count in sources)}


def affected_cameras_by_root(db):
    default_id = current_default_id(db)
    result = {}
    cameras = db.query(Camera.id, Camera.name, Camera.recording_archive_root_id).filter(
        Camera.deleted_at.is_(None)).order_by(Camera.name, Camera.id).all()
    for camera_id, name, assigned in cameras:
        root_id = assigned or default_id
        if root_id:
            result.setdefault(root_id, []).append({"id": camera_id, "name": name, "explicit": bool(assigned)})
    return result


def recent_camera_archive_moves(db):
    # The ordinary Storage refresh is enough; never scan files for progress.
    rows = db.query(ArchiveMigrationPlan, Camera.name, ArchiveRoot).outerjoin(
        Camera, Camera.id == ArchiveMigrationPlan.selected_camera_id).outerjoin(
        ArchiveRoot, ArchiveRoot.id == ArchiveMigrationPlan.target_root_id).filter(
        ArchiveMigrationPlan.selected_camera_id.is_not(None)).order_by(
        ArchiveMigrationPlan.created_at.desc(), ArchiveMigrationPlan.id).limit(12).all()
    return [{
        "id": plan.id, "camera_id": plan.selected_camera_id, "camera_name": name,
        "target_path": archive_root_host_display_path(target) if target else plan.target_label_snapshot,
        "status": plan.status, "phase": plan.phase, "item_count": plan.item_count,
        "completed_count": plan.completed_count, "reason": plan.reason_code,
    } for plan, name, target in rows]
