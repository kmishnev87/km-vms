from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.user import User
from app.services.audit_log import create_event
from app.services.recording_storage import archive_root_host_display_path
from app.services.setup_storage import queue_runtime_activation, storage_confirmation_status

logger = logging.getLogger(__name__)

ACTIVE_JOB_STATES = ("starting", "recording", "stopping", "restarting")
PENDING_FILE = "archive-root-activation-pending.json"
LAST_FILE = "archive-root-activation-last.json"
STOP_WAIT_SECONDS = 75
STOP_POLL_SECONDS = 2
STOP_SETTLE_SECONDS = 12
CLOSEOUT_POLL_SECONDS = 2
CLOSEOUT_MAX_SECONDS = 180


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _control_dir() -> Path:
    path = Path(settings.storage_install_control)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pending_path() -> Path:
    return _control_dir() / PENDING_FILE


def _last_path() -> Path:
    return _control_dir() / LAST_FILE


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Failed to read archive root activation state from %s", path)
        return {}


def read_pending_archive_root_activation() -> dict[str, Any] | None:
    payload = _read_json(_pending_path())
    return payload or None


def archive_root_activation_public_status() -> dict[str, Any]:
    pending = read_pending_archive_root_activation()
    last = _read_json(_last_path())
    if pending:
        phase = str(pending.get("phase") or "activation_requested")
        return {
            "status": "running",
            "operation_id": pending.get("activation_request_id"),
            "current_step": phase,
            "completed_steps": ["recordings_stopped", "activation_requested"] if phase == "activation_requested" else [],
            "failed_step": None,
            "human_reason": None,
            "started_at": pending.get("requested_at"),
            "updated_at": _utc_now(),
            "affected_camera_ids": pending.get("paused_camera_ids") or [],
            "duplicate_action_blocked": True,
            "pending": pending,
        }
    if last:
        result = last.get("result") or {}
        status = "completed" if last.get("phase") == "completed" else "failed"
        return {
            "status": status,
            "operation_id": last.get("activation_request_id"),
            "current_step": status,
            "completed_steps": ["recordings_stopped", "activation_requested", "runtime_applied", "cameras_restored"] if status == "completed" else [],
            "failed_step": result.get("reason") if status == "failed" else None,
            "human_reason": result.get("reason") or result.get("status"),
            "started_at": last.get("requested_at"),
            "updated_at": last.get("completed_at"),
            "affected_camera_ids": last.get("paused_camera_ids") or [],
            "duplicate_action_blocked": False,
            "result": result,
        }
    return {
        "status": "idle",
        "operation_id": None,
        "current_step": "idle",
        "completed_steps": [],
        "failed_step": None,
        "human_reason": None,
        "started_at": None,
        "updated_at": None,
        "affected_camera_ids": [],
        "duplicate_action_blocked": False,
    }


def _finish_pending(payload: dict[str, Any], *, status: str, result: dict[str, Any]) -> None:
    completed = {
        **payload,
        "phase": status,
        "completed_at": _utc_now(),
        "result": result,
    }
    _write_json(_last_path(), completed)
    try:
        _pending_path().unlink()
    except FileNotFoundError:
        pass


def _active_recording_jobs(db: Session) -> list[RecordingJob]:
    return (
        db.query(RecordingJob)
        .filter(RecordingJob.state.in_(ACTIVE_JOB_STATES))
        .order_by(RecordingJob.camera_id.asc(), RecordingJob.updated_at.desc().nullslast())
        .all()
    )


def _activation_camera_snapshot(db: Session) -> list[dict[str, Any]]:
    active_camera_ids = sorted({int(job.camera_id) for job in _active_recording_jobs(db) if job.camera_id is not None})
    if not active_camera_ids:
        return []
    cameras = (
        db.query(Camera)
        .filter(Camera.id.in_(active_camera_ids), Camera.enabled == True, Camera.deleted_at.is_(None))  # noqa: E712
        .order_by(Camera.id.asc())
        .all()
    )
    snapshots: list[dict[str, Any]] = []
    for camera in cameras:
        mode = str(getattr(camera, "recording_mode", "") or "").strip().lower()
        if mode != "always":
            continue
        snapshots.append(
            {
                "camera_id": int(camera.id),
                "name": camera.name,
                "enabled": bool(camera.enabled),
                "status": camera.status,
                "recording_mode": camera.recording_mode,
            }
        )
    return snapshots


def _writing_segments_count(db: Session, camera_ids: list[int]) -> int:
    if not camera_ids:
        return 0
    return (
        db.query(RecordingSegment)
        .filter(
            RecordingSegment.camera_id.in_(camera_ids),
            RecordingSegment.status.in_(("writing", "starting")),
            RecordingSegment.deleted_at.is_(None),
        )
        .count()
    )


def _paused_camera_rows(db: Session, camera_ids: list[int]) -> list[Camera]:
    if not camera_ids:
        return []
    return (
        db.query(Camera)
        .filter(Camera.id.in_(camera_ids), Camera.deleted_at.is_(None))
        .order_by(Camera.id.asc())
        .all()
    )


def _restore_cameras(db: Session, camera_ids: list[int], *, reason: str, snapshot_by_id: dict[int, dict[str, Any]] | None = None) -> list[int]:
    restored: list[int] = []
    for camera in _paused_camera_rows(db, camera_ids):
        snapshot = (snapshot_by_id or {}).get(int(camera.id), {})
        if snapshot and not snapshot.get("enabled", False):
            continue
        camera.enabled = True
        camera.status = "enabled" if reason == "activation_completed" else str(snapshot.get("status") or "enabled")
        camera.last_error = None
        camera.updated_at = datetime.utcnow()
        db.add(camera)
        restored.append(int(camera.id))
    if restored:
        create_event(
            db=db,
            actor=None,
            category="storage",
            event_type="archive_root.activation_recordings_restored",
            severity="info",
            message_ru="Archive root activation restored paused recordings",
            message_en="Archive root activation restored paused recordings",
            metadata={"camera_ids": restored, "reason": reason},
        )
    return restored


def _wait_for_recordings_to_stop(db: Session, camera_ids: list[int], *, paused_at: datetime) -> dict[str, Any]:
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    settle_until = time.monotonic() + STOP_SETTLE_SECONDS
    last_recent_active = 0
    last_writing = 0
    while True:
        db.expire_all()
        last_recent_active = (
            db.query(RecordingJob)
            .filter(
                RecordingJob.camera_id.in_(camera_ids),
                RecordingJob.state.in_(ACTIVE_JOB_STATES),
                RecordingJob.updated_at >= paused_at,
            )
            .count()
            if camera_ids
            else 0
        )
        last_writing = _writing_segments_count(db, camera_ids)
        if time.monotonic() >= settle_until and last_recent_active == 0 and last_writing == 0:
            return {"ok": True, "recent_active_jobs": 0, "writing_segments": 0}
        if time.monotonic() >= deadline:
            return {"ok": False, "recent_active_jobs": int(last_recent_active), "writing_segments": int(last_writing)}
        time.sleep(STOP_POLL_SECONDS)


def request_archive_root_activation(db: Session, *, root: ArchiveRoot, actor: User | None = None) -> dict[str, Any]:
    existing = read_pending_archive_root_activation()
    if existing:
        return {
            "status": "already_running",
            "pending": existing,
            "message": "archive_root_activation_already_running",
        }

    target_host_path = archive_root_host_display_path(root)
    previous_active_roots = [
        {
            "id": item.id,
            "root_path": item.root_path,
            "host_path": archive_root_host_display_path(item),
        }
        for item in db.query(ArchiveRoot).filter(ArchiveRoot.is_active == True).order_by(ArchiveRoot.id.asc()).all()  # noqa: E712
    ]
    stale_active_jobs = _active_recording_jobs(db)
    camera_snapshots = _activation_camera_snapshot(db)
    snapshot_by_id = {int(item["camera_id"]): item for item in camera_snapshots}
    paused_camera_ids = sorted(snapshot_by_id)
    paused_camera_names = [str(item.get("name") or item.get("camera_id")) for item in camera_snapshots]

    paused_at = datetime.utcnow()
    for camera in _paused_camera_rows(db, paused_camera_ids):
        if bool(camera.enabled):
            camera.enabled = False
            camera.status = "storage_switch_paused"
            camera.last_error = None
            camera.updated_at = datetime.utcnow()
            db.add(camera)
    if paused_camera_ids:
        create_event(
            db=db,
            actor=actor,
            category="storage",
            event_type="archive_root.activation_recordings_pause_requested",
            severity="warning",
            message_ru="Archive root activation paused active recordings",
            message_en="Archive root activation paused active recordings",
            target_type="archive_root",
            target_id=root.id,
            target_name=root.label,
            metadata={
                "camera_ids": paused_camera_ids,
                "camera_names": paused_camera_names,
                "snapshot_source": "confirmed_recording_cameras_before_activation",
                "ignored_stale_active_job_count": len(stale_active_jobs),
            },
        )
    db.commit()

    stopped = _wait_for_recordings_to_stop(db, paused_camera_ids, paused_at=paused_at)
    if not stopped["ok"]:
        restored = _restore_cameras(db, paused_camera_ids, reason="stop_timeout", snapshot_by_id=snapshot_by_id)
        db.commit()
        return {
            "status": "blocked",
            "reason": "recordings_did_not_stop_in_time",
            "recent_active_jobs": stopped["recent_active_jobs"],
            "writing_segments": stopped["writing_segments"],
            "restored_camera_ids": restored,
        }

    try:
        activation = queue_runtime_activation(target_host_path, request_prefix="archive-root")
    except Exception:
        restored = _restore_cameras(db, paused_camera_ids, reason="activation_request_failed", snapshot_by_id=snapshot_by_id)
        db.commit()
        raise

    pending = {
        "schema_version": 1,
        "phase": "activation_requested",
        "root_id": root.id,
        "root_label": root.label,
        "target_host_path": target_host_path,
        "requested_at": _utc_now(),
        "paused_at": paused_at.isoformat() + "Z",
        "actor_username": getattr(actor, "username", None),
        "paused_camera_ids": paused_camera_ids,
        "paused_camera_names": paused_camera_names,
        "camera_snapshots": camera_snapshots,
        "previous_active_roots": previous_active_roots,
        "activation_request_id": activation.get("request_id"),
        "storage_apply_status": activation.get("apply_status"),
    }
    _write_json(_pending_path(), pending)
    create_event(
        db=db,
        actor=actor,
        category="storage",
        event_type="archive_root.activation_requested",
        severity="warning",
        message_ru="Archive root activation requested",
        message_en="Archive root activation requested",
        target_type="archive_root",
        target_id=root.id,
        target_name=root.label,
        metadata={
            "root_id": root.id,
            "target_host_path": target_host_path,
            "paused_camera_ids": paused_camera_ids,
            "snapshot_source": "confirmed_recording_cameras_before_activation",
            "ignored_stale_active_job_count": len(stale_active_jobs),
            "activation_request_id": activation.get("request_id"),
        },
    )
    db.commit()
    return {
        "status": "activation_requested",
        "root_id": root.id,
        "target_host_path": target_host_path,
        "paused_camera_ids": paused_camera_ids,
        "activation_request_id": activation.get("request_id"),
        "storage_confirmation": activation.get("storage_confirmation"),
    }


def finalize_pending_archive_root_activation(db: Session) -> dict[str, Any]:
    pending = read_pending_archive_root_activation()
    if not pending:
        return {"status": "no_pending_activation"}

    confirmation = storage_confirmation_status()
    apply_status = str(confirmation.get("apply_status") or confirmation.get("status") or "")
    root = db.get(ArchiveRoot, pending.get("root_id"))
    paused_camera_ids = [int(item) for item in pending.get("paused_camera_ids") or [] if str(item).isdigit()]
    snapshot_by_id = {
        int(item["camera_id"]): item
        for item in pending.get("camera_snapshots") or []
        if isinstance(item, dict) and str(item.get("camera_id", "")).isdigit()
    }

    if apply_status in {"activation_failed", "failed", "validation_failed"}:
        restored = _restore_cameras(db, paused_camera_ids, reason="activation_failed", snapshot_by_id=snapshot_by_id)
        db.commit()
        result = {"status": "activation_failed", "apply_status": apply_status, "restored_camera_ids": restored}
        _finish_pending(pending, status="failed", result=result)
        return result

    if apply_status != "active":
        return {
            "status": "activation_pending",
            "apply_status": apply_status or "unknown",
            "pending": pending,
        }

    selected_host_path = str(confirmation.get("selected_host_path") or "")
    target_host_path = str(pending.get("target_host_path") or "")
    if selected_host_path != target_host_path:
        restored = _restore_cameras(db, paused_camera_ids, reason="activation_path_mismatch", snapshot_by_id=snapshot_by_id)
        db.commit()
        result = {
            "status": "activation_failed",
            "reason": "activation_path_mismatch",
            "selected_host_path": selected_host_path,
            "target_host_path": target_host_path,
            "restored_camera_ids": restored,
        }
        _finish_pending(pending, status="failed", result=result)
        return result

    if root is None:
        root = next(
            (
                item
                for item in db.query(ArchiveRoot).order_by(ArchiveRoot.created_at.asc()).all()
                if archive_root_host_display_path(item) == selected_host_path
            ),
            None,
        )
    if root is None:
        restored = _restore_cameras(db, paused_camera_ids, reason="activation_root_missing", snapshot_by_id=snapshot_by_id)
        db.commit()
        result = {"status": "activation_failed", "reason": "activation_root_missing", "restored_camera_ids": restored}
        _finish_pending(pending, status="failed", result=result)
        return result

    previous_snapshot = {
        str(item.get("id")): item
        for item in pending.get("previous_active_roots") or []
        if isinstance(item, dict) and item.get("id")
    }
    previous = db.query(ArchiveRoot).filter(ArchiveRoot.is_active == True).all()  # noqa: E712
    previous_ids = [item.id for item in previous]
    for item in previous:
        snapshot = previous_snapshot.get(str(item.id)) or {}
        previous_host_path = str(snapshot.get("host_path") or "").strip()
        if (
            previous_host_path
            and previous_host_path != selected_host_path
            and str(item.root_path or "") == str(settings.storage_root)
        ):
            item.root_path = previous_host_path
        item.is_active = False
        item.updated_at = datetime.utcnow()
        db.add(item)
    root.is_active = True
    root.is_available = True
    root.is_readable = True
    root.is_writable = True
    root.problem = None
    root.last_seen_at = datetime.utcnow()
    root.updated_at = datetime.utcnow()
    db.add(root)
    restored = _restore_cameras(db, paused_camera_ids, reason="activation_completed", snapshot_by_id=snapshot_by_id)
    create_event(
        db=db,
        actor=None,
        category="storage",
        event_type="archive_root.activation_completed",
        severity="info",
        message_ru="Archive root activation completed",
        message_en="Archive root activation completed",
        target_type="archive_root",
        target_id=root.id,
        target_name=root.label,
        metadata={
            "previous_root_ids": previous_ids,
            "active_root_id": root.id,
            "selected_host_path": selected_host_path,
            "restored_camera_ids": restored,
            "activation_request_id": pending.get("activation_request_id"),
        },
    )
    db.commit()
    result = {
        "status": "activation_completed",
        "active_root_id": root.id,
        "selected_host_path": selected_host_path,
        "restored_camera_ids": restored,
    }
    _finish_pending(pending, status="completed", result=result)
    return result


def _closeout_worker() -> None:
    deadline = time.monotonic() + CLOSEOUT_MAX_SECONDS
    while time.monotonic() < deadline:
        if not read_pending_archive_root_activation():
            return
        db = SessionLocal()
        try:
            result = finalize_pending_archive_root_activation(db)
            if result.get("status") in {"activation_completed", "activation_failed", "no_pending_activation"}:
                return
        except Exception:
            logger.exception("Archive root activation closeout failed")
        finally:
            db.close()
        time.sleep(CLOSEOUT_POLL_SECONDS)


def start_archive_root_activation_closeout_worker() -> None:
    if not read_pending_archive_root_activation():
        return
    thread = threading.Thread(target=_closeout_worker, name="archive-root-activation-closeout", daemon=True)
    thread.start()
