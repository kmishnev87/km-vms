from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - production/runtime tests use Linux
    fcntl = None

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingSegment
from app.models.user import User
from app.services.audit_log import create_event
from app.services.recorder_runtime_status import list_camera_recording_states
from app.services.recording_storage import (
    archive_root_host_display_path,
    archive_root_runtime_mount_path,
    archive_root_runtime_path,
    verify_archive_root_access,
    write_archive_roots_runtime_files,
)
from app.services.setup_storage import queue_runtime_activation, storage_confirmation_status

logger = logging.getLogger(__name__)
_WORKER_LEASE_THREAD_GUARD = threading.RLock()

PENDING_FILE = "archive-root-activation-pending.json"
LAST_FILE = "archive-root-activation-last.json"
MUTATION_LOCK_FILE = "archive-root-mutation.lock"
STATE_LOCK_FILE = "archive-root-activation-state.lock"
WORKER_LEASE_FILE = "archive-root-activation-worker.lock"
WORKER_LEASE_GUARD_FILE = "archive-root-activation-worker.guard"
STOP_WAIT_SECONDS = 75
STOP_POLL_SECONDS = 2
HELPER_WAIT_SECONDS = 210
HELPER_POLL_SECONDS = 2
STATE_LOCK_WAIT_SECONDS = 12
STATE_LOCK_STALE_SECONDS = 10
MUTATION_LOCK_STALE_SECONDS = 300
WORKER_LEASE_STALE_SECONDS = 20
WORKER_CLAIM_WAIT_SECONDS = 30
WORKER_HEARTBEAT_SECONDS = 5

TERMINAL_STATUSES = {"completed", "failed"}
BLOCKING_STATUSES = {"queued", "running", "failed_recovery_required"}
ACTIVE_JOB_STATES = ("starting", "recording", "stopping", "restarting")


class ArchiveRootMutationConflict(RuntimeError):
    def __init__(self, blocker: dict[str, Any]):
        super().__init__(str(blocker.get("reason_code") or "archive_root_mutation_blocked"))
        self.blocker = blocker


class ActivationStateConflict(RuntimeError):
    pass


class WorkerLeaseLost(ActivationStateConflict):
    pass


@dataclass(frozen=True)
class WorkerLeaseHandle:
    operation_id: str
    owner_token: str


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


def _mutation_lock_path() -> Path:
    return _control_dir() / MUTATION_LOCK_FILE


def _state_lock_path() -> Path:
    return _control_dir() / STATE_LOCK_FILE


def _worker_lease_path() -> Path:
    return _control_dir() / WORKER_LEASE_FILE


def _worker_lease_guard_path() -> Path:
    return _control_dir() / WORKER_LEASE_GUARD_FILE


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(tmp, path)
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
        logger.exception("Failed to read archive-root control state from %s", path)
        return {}


def _create_exclusive_json(path: Path, payload: dict[str, Any]) -> bool:
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _file_age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _move_stale_lock(path: Path) -> bool:
    stale = path.with_name(f"{path.name}.stale.{uuid.uuid4().hex}")
    try:
        os.replace(path, stale)
    except OSError:
        return False
    try:
        stale.unlink()
    except OSError:
        pass
    return True


@contextmanager
def _worker_lease_file_guard() -> Iterator[None]:
    """Serialize lease claim/touch/reclaim/release across API processes."""
    path = _worker_lease_guard_path()
    with _WORKER_LEASE_THREAD_GUARD:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@contextmanager
def _state_file_guard(owner: str) -> Iterator[None]:
    deadline = time.monotonic() + STATE_LOCK_WAIT_SECONDS
    lock_path = _state_lock_path()
    while True:
        if _create_exclusive_json(lock_path, {"owner": owner, "acquired_at": _utc_now()}):
            break
        age = _file_age_seconds(lock_path)
        if age is not None and age > STATE_LOCK_STALE_SECONDS and _move_stale_lock(lock_path):
            continue
        if time.monotonic() >= deadline:
            raise ActivationStateConflict("archive_root_activation_state_lock_busy")
        time.sleep(0.05)
    try:
        yield
    finally:
        current = _read_json(lock_path)
        if str(current.get("owner") or "") == owner:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def read_pending_archive_root_activation() -> dict[str, Any] | None:
    payload = _read_json(_pending_path())
    return payload or None


def _mutation_blocker_from_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state or str(state.get("status") or "") not in BLOCKING_STATUSES:
        return None
    return {
        "reason_code": (
            "archive_root_recovery_required"
            if state.get("status") == "failed_recovery_required"
            else "archive_root_activation_in_progress"
        ),
        "operation_id": state.get("operation_id"),
        "status": state.get("status"),
        "current_step": state.get("current_step"),
        "recovery_available": state.get("status") == "failed_recovery_required",
    }


def archive_root_mutation_blocker() -> dict[str, Any] | None:
    state_blocker = _mutation_blocker_from_state(read_pending_archive_root_activation())
    if state_blocker:
        return state_blocker
    lock = _read_json(_mutation_lock_path())
    if lock:
        return {
            "reason_code": "archive_root_mutation_in_progress",
            "operation_id": lock.get("operation_id"),
            "mutation": lock.get("purpose"),
        }
    return None


def _acquire_mutation_lock(*, owner: str, purpose: str, operation_id: str | None = None) -> None:
    path = _mutation_lock_path()
    payload = {
        "owner": owner,
        "purpose": purpose,
        "operation_id": operation_id,
        "acquired_at": _utc_now(),
        "heartbeat_at": _utc_now(),
    }
    if _create_exclusive_json(path, payload):
        return
    pending = read_pending_archive_root_activation()
    age = _file_age_seconds(path)
    if not pending and age is not None and age > MUTATION_LOCK_STALE_SECONDS and _move_stale_lock(path):
        if _create_exclusive_json(path, payload):
            return
    blocker = archive_root_mutation_blocker() or {"reason_code": "archive_root_mutation_in_progress"}
    raise ArchiveRootMutationConflict(blocker)


def _transfer_mutation_lock(*, current_owner: str, new_owner: str, operation_id: str) -> None:
    path = _mutation_lock_path()
    lock = _read_json(path)
    if str(lock.get("owner") or "") != current_owner:
        raise ArchiveRootMutationConflict(archive_root_mutation_blocker() or {"reason_code": "archive_root_mutation_lock_lost"})
    _write_json(
        path,
        {
            **lock,
            "owner": new_owner,
            "purpose": "archive_root_activation",
            "operation_id": operation_id,
            "heartbeat_at": _utc_now(),
        },
    )


def _touch_mutation_lock(operation_id: str) -> None:
    path = _mutation_lock_path()
    lock = _read_json(path)
    if str(lock.get("owner") or "") != operation_id:
        raise ArchiveRootMutationConflict({"reason_code": "archive_root_mutation_lock_lost", "operation_id": operation_id})
    _write_json(path, {**lock, "heartbeat_at": _utc_now()})


def _release_mutation_lock(owner: str) -> None:
    path = _mutation_lock_path()
    lock = _read_json(path)
    if str(lock.get("owner") or "") != owner:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def archive_root_mutation_guard(purpose: str) -> Iterator[str]:
    owner = f"mutation-{uuid.uuid4().hex}"
    _acquire_mutation_lock(owner=owner, purpose=purpose)
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(30):
            try:
                _touch_mutation_lock(owner)
            except ArchiveRootMutationConflict:
                return

    heartbeat_thread = threading.Thread(
        target=heartbeat,
        name=f"archive-root-mutation-{owner[-8:]}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        yield owner
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1)
        _release_mutation_lock(owner)


def _persist_new_state(state: dict[str, Any]) -> None:
    owner = f"state-create-{state['operation_id']}"
    with _state_file_guard(owner):
        if read_pending_archive_root_activation():
            raise ActivationStateConflict("archive_root_activation_already_exists")
        _write_json(_pending_path(), state)


def _transition(
    operation_id: str,
    *,
    expected_revision: int | None = None,
    expected_step: str | None = None,
    complete_step: str | None = None,
    worker_session: WorkerLeaseSession | None = None,
    **changes: Any,
) -> dict[str, Any]:
    with _worker_side_effect_fence(worker_session):
        owner = f"state-update-{operation_id}-{uuid.uuid4().hex}"
        with _state_file_guard(owner):
            state = read_pending_archive_root_activation()
            if not state or str(state.get("operation_id") or "") != operation_id:
                raise ActivationStateConflict("archive_root_activation_operation_mismatch")
            if expected_revision is not None and int(state.get("revision") or 0) != int(expected_revision):
                raise ActivationStateConflict("archive_root_activation_revision_conflict")
            if expected_step is not None and str(state.get("current_step") or "") != expected_step:
                raise ActivationStateConflict("archive_root_activation_step_conflict")
            completed = list(state.get("completed_steps") or [])
            if complete_step and complete_step not in completed:
                completed.append(complete_step)
            updated = {**state, **changes, "completed_steps": completed}
            updated["revision"] = int(state.get("revision") or 0) + 1
            updated["updated_at"] = _utc_now()
            _write_json(_pending_path(), updated)
        _touch_mutation_lock(operation_id)
    return updated


def _transition_from(
    state: dict[str, Any],
    *,
    complete_step: str | None = None,
    worker_session: WorkerLeaseSession | None = None,
    **changes: Any,
) -> dict[str, Any]:
    return _transition(
        str(state["operation_id"]),
        expected_revision=int(state.get("revision") or 0),
        expected_step=str(state.get("current_step") or ""),
        complete_step=complete_step,
        worker_session=worker_session,
        **changes,
    )


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": state.get("status"),
        "operation_id": state.get("operation_id"),
        "revision": state.get("revision"),
        "current_step": state.get("current_step"),
        "completed_steps": list(state.get("completed_steps") or []),
        "failed_step": state.get("failed_step"),
        "reason_code": state.get("reason_code"),
        "presentation_key": state.get("presentation_key"),
        "presentation_data": dict(state.get("presentation_data") or {}),
        "previous_root_id": state.get("previous_root_id"),
        "previous_root_label": state.get("previous_root_label"),
        "target_root_id": state.get("target_root_id"),
        "target_root_label": state.get("target_root_label"),
        "effective_active_root_id": state.get("effective_active_root_id"),
        "effective_active_root_label": state.get("effective_active_root_label"),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "completed_at": state.get("completed_at"),
        "affected_camera_ids": list(state.get("affected_camera_ids") or []),
        "paused_camera_ids": list(state.get("paused_camera_ids") or []),
        "restored_camera_ids": list(state.get("restored_camera_ids") or []),
        "camera_restore_failed_ids": list(state.get("camera_restore_failed_ids") or []),
        "duplicate_action_blocked": bool(state.get("duplicate_action_blocked")),
        "runtime_apply_completed": bool(state.get("runtime_apply_completed")),
        "rollback_status": state.get("rollback_status"),
        "rollback_failed_step": state.get("rollback_failed_step"),
        "rollback_reason_code": state.get("rollback_reason_code"),
        "recovery_available": state.get("status") == "failed_recovery_required",
    }


def archive_root_activation_public_status() -> dict[str, Any]:
    pending = read_pending_archive_root_activation()
    if pending:
        return _public_state(pending)
    last = _read_json(_last_path())
    if last and int(last.get("schema_version") or 0) >= 2:
        return _public_state(last)
    return {
        "status": "idle",
        "operation_id": None,
        "revision": None,
        "current_step": "idle",
        "completed_steps": [],
        "failed_step": None,
        "reason_code": None,
        "presentation_key": None,
        "presentation_data": {},
        "previous_root_id": None,
        "previous_root_label": None,
        "target_root_id": None,
        "target_root_label": None,
        "effective_active_root_id": None,
        "effective_active_root_label": None,
        "started_at": None,
        "updated_at": None,
        "completed_at": None,
        "affected_camera_ids": [],
        "paused_camera_ids": [],
        "restored_camera_ids": [],
        "camera_restore_failed_ids": [],
        "duplicate_action_blocked": False,
        "runtime_apply_completed": False,
        "rollback_status": "not_required",
        "rollback_failed_step": None,
        "rollback_reason_code": None,
        "recovery_available": False,
    }


def _terminalize(
    operation_id: str,
    *,
    expected_revision: int | None = None,
    expected_step: str | None = None,
    status: str,
    current_step: str,
    reason_code: str | None,
    presentation_key: str,
    presentation_data: dict[str, Any] | None = None,
    complete_step: str | None = None,
    worker_session: WorkerLeaseSession | None = None,
    **changes: Any,
) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise ValueError("archive_root_activation_terminal_status_invalid")
    with _worker_side_effect_fence(worker_session):
        owner = f"state-terminal-{operation_id}-{uuid.uuid4().hex}"
        with _state_file_guard(owner):
            state = read_pending_archive_root_activation()
            if not state or str(state.get("operation_id") or "") != operation_id:
                raise ActivationStateConflict("archive_root_activation_operation_mismatch")
            if expected_revision is not None and int(state.get("revision") or 0) != int(expected_revision):
                raise ActivationStateConflict("archive_root_activation_revision_conflict")
            if expected_step is not None and str(state.get("current_step") or "") != expected_step:
                raise ActivationStateConflict("archive_root_activation_step_conflict")
            completed_steps = list(state.get("completed_steps") or [])
            if complete_step and complete_step not in completed_steps:
                completed_steps.append(complete_step)
            completed = {
                **state,
                **changes,
                "revision": int(state.get("revision") or 0) + 1,
                "status": status,
                "current_step": current_step,
                "completed_steps": completed_steps,
                "failed_step": None if status == "completed" else current_step,
                "reason_code": reason_code,
                "presentation_key": presentation_key,
                "presentation_data": dict(presentation_data or {}),
                "duplicate_action_blocked": False,
                "updated_at": _utc_now(),
                "completed_at": _utc_now(),
            }
            _write_json(_last_path(), completed)
            current = read_pending_archive_root_activation()
            if current and str(current.get("operation_id") or "") == operation_id:
                _pending_path().unlink()
        _release_mutation_lock(operation_id)
    return completed


def _terminalize_from(
    state: dict[str, Any],
    *,
    worker_session: WorkerLeaseSession | None = None,
    **changes: Any,
) -> dict[str, Any]:
    return _terminalize(
        str(state["operation_id"]),
        expected_revision=int(state.get("revision") or 0),
        expected_step=str(state.get("current_step") or ""),
        worker_session=worker_session,
        **changes,
    )


def _mark_recovery_required(
    operation_id: str,
    *,
    failed_step: str,
    reason_code: str,
    effective_root_id: str | None,
    effective_root_label: str | None,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    state = read_pending_archive_root_activation()
    if not state or str(state.get("operation_id") or "") != operation_id:
        raise ActivationStateConflict("archive_root_activation_operation_mismatch")
    return _transition_from(
        state,
        worker_session=worker_session,
        status="failed_recovery_required",
        current_step="failed_recovery_required",
        failed_step=failed_step,
        reason_code=reason_code,
        presentation_key="storage_activation_recovery_required",
        presentation_data={"effective_root_label": effective_root_label},
        effective_active_root_id=effective_root_id,
        effective_active_root_label=effective_root_label,
        rollback_status="failed",
        rollback_failed_step=failed_step,
        rollback_reason_code=reason_code,
        duplicate_action_blocked=True,
    )


def _confirmed_recording_camera_ids(db: Session) -> set[int]:
    return {
        int(item["camera_id"])
        for item in list_camera_recording_states(db)
        if bool(item.get("confirmed_recording"))
        and bool(item.get("enabled"))
        and str(item.get("recording_mode") or "").lower() == "always"
    }


def _activation_camera_snapshot(db: Session, operation_id: str) -> tuple[list[dict[str, Any]], int]:
    snapshots: list[dict[str, Any]] = []
    ignored_active_looking = 0
    for item in list_camera_recording_states(db):
        active_looking = str(item.get("job_state") or "") in ACTIVE_JOB_STATES
        if active_looking and not bool(item.get("confirmed_recording")):
            ignored_active_looking += 1
        if not (
            bool(item.get("confirmed_recording"))
            and bool(item.get("enabled"))
            and str(item.get("recording_mode") or "").lower() == "always"
        ):
            continue
        snapshots.append(
            {
                "operation_id": operation_id,
                "camera_id": int(item["camera_id"]),
                "name": str(item.get("camera_name") or item["camera_id"]),
                "enabled_intent": True,
                "recording_mode": str(item.get("recording_mode") or "always"),
                "pre_operation_camera_status": item.get("camera_status"),
                "pre_operation_recording_health": item.get("recording_health"),
                "eligibility_reason": "confirmed_current_instance_media_progress",
                "confirmed_evidence_at": item.get("media_progress_at"),
                "confirmed_segment_id": item.get("current_segment_id"),
                "confirmed_job_id": item.get("job_id"),
                "confirmed_recorder_instance_id": item.get("recorder_instance_id"),
                "changed_by_operation": False,
            }
        )
    return snapshots, ignored_active_looking


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


def _restore_cameras(
    db: Session,
    camera_ids: list[int],
    *,
    reason: str,
    snapshot_by_id: dict[int, dict[str, Any]] | None = None,
) -> dict[str, list[int]]:
    restored: list[int] = []
    failed: list[int] = []
    for camera in _paused_camera_rows(db, camera_ids):
        snapshot = (snapshot_by_id or {}).get(int(camera.id), {})
        if not snapshot or not snapshot.get("enabled_intent", False) or not snapshot.get("changed_by_operation", False):
            continue
        try:
            camera.enabled = True
            camera.status = "enabled"
            camera.updated_at = datetime.utcnow()
            db.add(camera)
            restored.append(int(camera.id))
        except Exception:
            failed.append(int(camera.id))
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
    return {"restored": restored, "failed": failed}


def _wait_for_recordings_to_stop(
    db: Session,
    camera_ids: list[int],
    *,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    last_confirmed = 0
    last_writing = 0
    while True:
        _assert_worker_session(worker_session)
        db.expire_all()
        last_confirmed = sum(
            1
            for item in list_camera_recording_states(db)
            if int(item.get("camera_id") or 0) in camera_ids and bool(item.get("confirmed_recording"))
        )
        last_writing = _writing_segments_count(db, camera_ids)
        if last_confirmed == 0 and last_writing == 0:
            _assert_worker_session(worker_session)
            return {"ok": True, "confirmed_recordings": 0, "writing_segments": 0}
        if time.monotonic() >= deadline:
            _assert_worker_session(worker_session)
            return {"ok": False, "confirmed_recordings": int(last_confirmed), "writing_segments": int(last_writing)}
        time.sleep(STOP_POLL_SECONDS)


def _new_operation_state(
    *,
    operation_id: str,
    previous_root: ArchiveRoot,
    target_root: ArchiveRoot,
    actor: User | None,
    camera_snapshots: list[dict[str, Any]],
    ignored_active_looking_count: int,
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "schema_version": 2,
        "operation_id": operation_id,
        "revision": 1,
        "status": "queued",
        "current_step": "snapshot_created",
        "completed_steps": ["snapshot_created"],
        "failed_step": None,
        "reason_code": None,
        "presentation_key": "storage_activation_queued",
        "presentation_data": {},
        "previous_root_id": str(previous_root.id),
        "previous_root_label": str(previous_root.label or previous_root.id),
        "previous_host_path": archive_root_host_display_path(previous_root),
        "target_root_id": str(target_root.id),
        "target_root_label": str(target_root.label or target_root.id),
        "target_host_path": archive_root_host_display_path(target_root),
        "effective_active_root_id": str(previous_root.id),
        "effective_active_root_label": str(previous_root.label or previous_root.id),
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "actor_username": getattr(actor, "username", None),
        "camera_snapshots": camera_snapshots,
        "affected_camera_ids": [int(item["camera_id"]) for item in camera_snapshots],
        "paused_camera_ids": [],
        "restored_camera_ids": [],
        "camera_restore_failed_ids": [],
        "ignored_active_looking_camera_count": int(ignored_active_looking_count),
        "duplicate_action_blocked": True,
        "runtime_request_id": None,
        "runtime_apply_completed": False,
        "rollback_runtime_request_id": None,
        "rollback_status": "not_required",
        "rollback_failed_step": None,
        "rollback_reason_code": None,
        "worker_recovery_count": 0,
        "target_preflight_validated_at": None,
    }


def request_archive_root_activation(
    db: Session,
    *,
    root: ArchiveRoot,
    actor: User | None = None,
    mutation_owner: str | None = None,
    recovery: bool = False,
) -> dict[str, Any]:
    existing = read_pending_archive_root_activation()
    if existing:
        if recovery and existing.get("status") == "failed_recovery_required":
            existing = _transition_from(
                existing,
                status="running",
                current_step="rollback_requested",
                presentation_key="storage_activation_restoring_previous_location",
                rollback_status="pending",
                duplicate_action_blocked=True,
            )
            start_archive_root_activation_closeout_worker()
            return {**_public_state(existing), "recovery_started": True}
        return {"status": "already_running", "operation": _public_state(existing)}
    if recovery:
        return {"status": "blocked", "reason_code": "archive_root_recovery_not_available"}
    if root.retired_at is not None:
        return {"status": "blocked", "reason_code": "archive_root_retired"}
    if root.retirement_status == "partial_deletion":
        return {"status": "blocked", "reason_code": "archive_root_partial_deletion_requires_retry"}

    previous_root = (
        db.query(ArchiveRoot)
        .filter(ArchiveRoot.is_active == True, ArchiveRoot.retired_at.is_(None))  # noqa: E712
        .order_by(ArchiveRoot.updated_at.desc(), ArchiveRoot.id.asc())
        .first()
    )
    if previous_root is None:
        return {"status": "blocked", "reason_code": "active_archive_root_missing"}
    if str(previous_root.id) == str(root.id):
        return {"status": "already_active", "root_id": str(root.id)}

    operation_id = f"archive-root-{uuid.uuid4().hex}"
    lock_acquired = False
    try:
        if mutation_owner:
            _transfer_mutation_lock(current_owner=mutation_owner, new_owner=operation_id, operation_id=operation_id)
        else:
            _acquire_mutation_lock(owner=operation_id, purpose="archive_root_activation", operation_id=operation_id)
        lock_acquired = True
        camera_snapshots, ignored_count = _activation_camera_snapshot(db, operation_id)
        state = _new_operation_state(
            operation_id=operation_id,
            previous_root=previous_root,
            target_root=root,
            actor=actor,
            camera_snapshots=camera_snapshots,
            ignored_active_looking_count=ignored_count,
        )
        _persist_new_state(state)
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
                "operation_id": operation_id,
                "previous_root_id": str(previous_root.id),
                "target_root_id": str(root.id),
                "affected_camera_ids": state["affected_camera_ids"],
                "ignored_active_looking_camera_count": ignored_count,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        if lock_acquired:
            _release_mutation_lock(operation_id)
        raise

    start_archive_root_activation_closeout_worker()
    return _public_state(state)


def _worker_lease_payload(handle: WorkerLeaseHandle) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "operation_id": handle.operation_id,
        "owner_token": handle.owner_token,
        "worker_identity": f"{os.getpid()}:{threading.get_ident()}",
        "claimed_at": _utc_now(),
        "heartbeat_at": _utc_now(),
    }


def _worker_lease_snapshot(path: Path) -> tuple[dict[str, Any], tuple[int, int, int] | None, float | None]:
    try:
        before = path.stat()
    except OSError:
        return {}, None, None
    lease = _read_json(path)
    try:
        after = path.stat()
    except OSError:
        return {}, None, None
    marker = (int(after.st_ino), int(after.st_mtime_ns), int(after.st_size))
    if (int(before.st_ino), int(before.st_mtime_ns), int(before.st_size)) != marker:
        return lease, None, None
    age = max(0.0, time.time() - after.st_mtime)
    return lease, marker, age


def _worker_lease_matches(lease: dict[str, Any], handle: WorkerLeaseHandle) -> bool:
    return (
        str(lease.get("operation_id") or "") == handle.operation_id
        and str(lease.get("owner_token") or "") == handle.owner_token
    )


def _create_worker_lease_locked(path: Path, handle: WorkerLeaseHandle) -> bool:
    return _create_exclusive_json(path, _worker_lease_payload(handle))


def _try_claim_worker_lease(handle: WorkerLeaseHandle) -> str:
    path = _worker_lease_path()
    observed_lease, observed_marker, _observed_age = _worker_lease_snapshot(path)
    with _worker_lease_file_guard():
        current_lease, current_marker, current_age = _worker_lease_snapshot(path)
        if current_marker is None:
            if path.exists():
                return "retry"
            return "acquired" if _create_worker_lease_locked(path, handle) else "retry"
        if observed_marker is None or current_marker != observed_marker or current_lease != observed_lease:
            return "retry"
        if current_age is not None and current_age > WORKER_LEASE_STALE_SECONDS:
            if not _move_stale_lock(path):
                return "retry"
            return "acquired" if _create_worker_lease_locked(path, handle) else "retry"
        if str(current_lease.get("operation_id") or "") != handle.operation_id:
            return "unavailable"
        return "wait"


def _claim_worker_lease(operation_id: str) -> WorkerLeaseHandle | None:
    handle = WorkerLeaseHandle(operation_id=operation_id, owner_token=uuid.uuid4().hex)
    deadline = time.monotonic() + WORKER_CLAIM_WAIT_SECONDS
    while time.monotonic() < deadline:
        result = _try_claim_worker_lease(handle)
        if result == "acquired":
            return handle
        if result == "unavailable":
            return None
        time.sleep(0.05 if result == "retry" else 0.5)
    return None


def _validate_worker_lease_locked(handle: WorkerLeaseHandle) -> dict[str, Any]:
    lease = _read_json(_worker_lease_path())
    if not _worker_lease_matches(lease, handle):
        raise WorkerLeaseLost("archive_root_activation_worker_lease_lost")
    return lease


def _assert_worker_lease(handle: WorkerLeaseHandle) -> None:
    with _worker_lease_file_guard():
        _validate_worker_lease_locked(handle)


def _touch_worker_lease(handle: WorkerLeaseHandle) -> None:
    with _worker_lease_file_guard():
        lease = _validate_worker_lease_locked(handle)
        _write_json(_worker_lease_path(), {**lease, "heartbeat_at": _utc_now()})


def _release_worker_lease(handle: WorkerLeaseHandle) -> bool:
    path = _worker_lease_path()
    with _worker_lease_file_guard():
        lease = _read_json(path)
        if not _worker_lease_matches(lease, handle):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


class WorkerLeaseSession:
    def __init__(self, handle: WorkerLeaseHandle):
        self.handle = handle
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._lost_error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        _touch_worker_lease(self.handle)
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"archive-root-lease-{self.handle.owner_token[-8:]}",
            daemon=True,
        )
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(WORKER_HEARTBEAT_SECONDS):
            try:
                _touch_worker_lease(self.handle)
            except BaseException as exc:  # keep ownership failure visible to the worker
                self._lost_error = exc
                self._lost.set()
                return

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise WorkerLeaseLost("archive_root_activation_worker_heartbeat_failed") from self._lost_error
        _assert_worker_lease(self.handle)
        if self._lost.is_set():
            raise WorkerLeaseLost("archive_root_activation_worker_heartbeat_failed") from self._lost_error

    @contextmanager
    def fenced(self) -> Iterator[None]:
        if self._lost.is_set():
            raise WorkerLeaseLost("archive_root_activation_worker_heartbeat_failed") from self._lost_error
        with _worker_lease_file_guard():
            lease = _validate_worker_lease_locked(self.handle)
            _write_json(_worker_lease_path(), {**lease, "heartbeat_at": _utc_now()})
            yield
            lease = _validate_worker_lease_locked(self.handle)
            _write_json(_worker_lease_path(), {**lease, "heartbeat_at": _utc_now()})
        if self._lost.is_set():
            raise WorkerLeaseLost("archive_root_activation_worker_heartbeat_failed") from self._lost_error

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, WORKER_HEARTBEAT_SECONDS + 1.0))


@contextmanager
def _worker_side_effect_fence(worker_session: WorkerLeaseSession | None) -> Iterator[None]:
    if worker_session is None:
        yield
        return
    with worker_session.fenced():
        yield


def _assert_worker_session(worker_session: WorkerLeaseSession | None) -> None:
    if worker_session is not None:
        worker_session.assert_owned()


def _worker_session_kwargs(worker_session: WorkerLeaseSession | None) -> dict[str, WorkerLeaseSession]:
    return {"worker_session": worker_session} if worker_session is not None else {}


def _root_by_id(db: Session, root_id: str | None) -> ArchiveRoot | None:
    return db.get(ArchiveRoot, root_id) if root_id else None


def _root_access(root: ArchiveRoot | None, *, require_write: bool) -> dict[str, Any]:
    if root is None or root.retired_at is not None:
        return {"ok": False, "reason_code": "archive_root_missing"}
    try:
        result = verify_archive_root_access(root, require_write=require_write)
    except Exception:
        logger.exception("Archive-root access probe failed for %s", getattr(root, "id", None))
        return {"ok": False, "reason_code": "archive_root_access_probe_failed"}
    read_ok = result.get("read_access_state") == "available" and bool(result.get("verified"))
    write_ok = not require_write or result.get("write_access_state") == "available"
    return {
        "ok": bool(read_ok and write_ok),
        "reason_code": result.get("verification_error") or result.get("problem") or "archive_root_access_unavailable",
        "read_access_state": result.get("read_access_state"),
        "write_access_state": result.get("write_access_state"),
    }


def _runtime_binding_matches_root(root: ArchiveRoot | None) -> bool:
    if root is None or root.retired_at is not None:
        return False
    active_runtime = Path(settings.storage_root)
    root_runtime = archive_root_runtime_mount_path(root)
    if not root_runtime.exists():
        root_runtime = archive_root_runtime_path(root)
    try:
        active_stat = active_runtime.stat()
        root_stat = root_runtime.stat()
    except OSError:
        return False
    return (int(active_stat.st_dev), int(active_stat.st_ino)) == (int(root_stat.st_dev), int(root_stat.st_ino))


def _target_preflight(
    db: Session,
    state: dict[str, Any],
    *,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    _assert_worker_session(worker_session)
    target = _root_by_id(db, state.get("target_root_id"))
    previous = _root_by_id(db, state.get("previous_root_id"))
    if target is None or previous is None:
        return {"ok": False, "reason_code": "archive_root_missing"}

    # Revalidate both the target and rollback root before changing camera intent.
    # Older Stage 4.7 rows receive identity only after current host evidence proves it.
    try:
        from app.services.setup_storage import revalidate_configured_archive_root

        target_host = revalidate_configured_archive_root(target)
        _assert_worker_session(worker_session)
        previous_host = revalidate_configured_archive_root(previous)
        _assert_worker_session(worker_session)
    except WorkerLeaseLost:
        raise
    except Exception as exc:
        return {"ok": False, "reason_code": str(exc) or "archive_root_host_revalidation_failed"}

    previous_access = _root_access(previous, require_write=True)
    if not previous_access.get("ok"):
        return {"ok": False, "reason_code": previous_access.get("reason_code") or "previous_root_access_unavailable"}
    if not _runtime_binding_matches_root(previous):
        return {"ok": False, "reason_code": "previous_root_runtime_binding_mismatch"}

    identity_updates = False
    for root, evidence in ((target, target_host), (previous, previous_host)):
        identity = str(evidence.get("physical_identity") or "")
        if not identity:
            return {"ok": False, "reason_code": "storage_candidate_identity_unavailable"}
        if not getattr(root, "physical_identity", None):
            root.physical_identity = identity
            root.updated_at = datetime.utcnow()
            db.add(root)
            identity_updates = True
    if identity_updates:
        with _worker_side_effect_fence(worker_session):
            db.commit()
    return {
        "ok": True,
        "source": "host_helper",
        "target_physical_identity": target.physical_identity,
        "previous_physical_identity": previous.physical_identity,
    }


def _confirmation_for_request(operation_id: str, request_id: str | None) -> dict[str, Any]:
    confirmation = storage_confirmation_status()
    if str(confirmation.get("operation_id") or "") != operation_id:
        return {"matched": False, "status": "stale_operation_result"}
    if not request_id or str(confirmation.get("runtime_request_id") or "") != str(request_id):
        return {"matched": False, "status": "stale_runtime_request_result"}
    return {"matched": True, **confirmation}


def _wait_for_helper(
    operation_id: str,
    request_id: str,
    *,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + HELPER_WAIT_SECONDS
    last: dict[str, Any] = {"matched": False, "status": "waiting"}
    while time.monotonic() < deadline:
        _assert_worker_session(worker_session)
        last = _confirmation_for_request(operation_id, request_id)
        if last.get("matched"):
            status = str(last.get("apply_status") or last.get("status") or "")
            if status == "active":
                _assert_worker_session(worker_session)
                return {**last, "ok": True}
            if status in {"activation_failed", "failed", "validation_failed"}:
                apply_state = last.get("apply_state") if isinstance(last.get("apply_state"), dict) else {}
                configuration_consistent = apply_state.get("configuration_consistent")
                _assert_worker_session(worker_session)
                return {
                    **last,
                    "ok": False,
                    "reason_code": (
                        "persistent_storage_config_recovery_failed"
                        if configuration_consistent is False
                        else "runtime_activation_failed"
                    ),
                    "configuration_consistent": configuration_consistent,
                }
        with _worker_side_effect_fence(worker_session):
            _touch_mutation_lock(operation_id)
        time.sleep(HELPER_POLL_SECONDS)
    _assert_worker_session(worker_session)
    return {**last, "ok": False, "reason_code": "runtime_activation_timeout"}


def _verify_effective_root(
    db: Session,
    *,
    root_id: str | None,
    expected_host_path: str | None,
    operation_id: str | None = None,
    request_id: str | None = None,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    _assert_worker_session(worker_session)
    confirmation = storage_confirmation_status()
    if operation_id and str(confirmation.get("operation_id") or "") != operation_id:
        return {"ok": False, "reason_code": "effective_root_operation_mismatch"}
    if request_id and str(confirmation.get("runtime_request_id") or "") != request_id:
        return {"ok": False, "reason_code": "effective_root_request_mismatch"}
    if str(confirmation.get("apply_status") or "") != "active":
        return {"ok": False, "reason_code": "effective_root_not_active"}
    if str(confirmation.get("selected_host_path") or "") != str(expected_host_path or ""):
        return {"ok": False, "reason_code": "effective_root_path_mismatch"}
    root = _root_by_id(db, root_id)
    if not _runtime_binding_matches_root(root):
        return {"ok": False, "reason_code": "effective_root_runtime_binding_mismatch"}
    access = _root_access(root, require_write=True)
    _assert_worker_session(worker_session)
    return {
        **access,
        "root_id": getattr(root, "id", None),
        "root_label": getattr(root, "label", None),
    }


def _detect_effective_root(db: Session, state: dict[str, Any] | None = None) -> dict[str, Any]:
    confirmation = storage_confirmation_status()
    selected = str(confirmation.get("selected_host_path") or "")
    if state:
        candidates = (
            (state.get("target_root_id"), state.get("target_root_label"), state.get("target_host_path")),
            (state.get("previous_root_id"), state.get("previous_root_label"), state.get("previous_host_path")),
        )
        for root_id, root_label, host_path in candidates:
            root = _root_by_id(db, root_id)
            if not _runtime_binding_matches_root(root):
                continue
            access = _root_access(root, require_write=True)
            if access.get("ok"):
                return {
                    **access,
                    "root_id": str(root_id),
                    "root_label": str(root_label or root_id),
                    "confirmation_path_matched": selected == str(host_path or ""),
                }
        return {"ok": False, "reason_code": "effective_root_unknown"}
    for root in db.query(ArchiveRoot).filter(ArchiveRoot.retired_at.is_(None)).all():
        if not _runtime_binding_matches_root(root):
            continue
        access = _root_access(root, require_write=True)
        if access.get("ok"):
            return {
                **access,
                "root_id": str(root.id),
                "root_label": str(root.label or root.id),
                "confirmation_path_matched": archive_root_host_display_path(root) == selected,
            }
    return {"ok": False, "reason_code": "effective_root_unknown"}


def _set_active_root(
    db: Session,
    state: dict[str, Any],
    root_id: str,
    *,
    worker_session: WorkerLeaseSession | None = None,
) -> ArchiveRoot:
    root = _root_by_id(db, root_id)
    if root is None or root.retired_at is not None:
        raise RuntimeError("archive_root_missing")
    previous_id = str(state.get("previous_root_id") or "")
    previous_host_path = str(state.get("previous_host_path") or "")
    with _worker_side_effect_fence(worker_session):
        for item in db.query(ArchiveRoot).filter(ArchiveRoot.retired_at.is_(None)).all():
            if str(item.id) == previous_id and str(item.root_path) == str(settings.storage_root) and previous_host_path:
                item.root_path = previous_host_path
            item.is_active = str(item.id) == str(root_id)
            item.updated_at = datetime.utcnow()
            db.add(item)
        db.commit()
        write_archive_roots_runtime_files(db)
    return root


def _pause_operation_cameras(
    db: Session,
    state: dict[str, Any],
    *,
    worker_session: WorkerLeaseSession | None = None,
) -> tuple[list[int], list[dict[str, Any]]]:
    snapshots = [dict(item) for item in state.get("camera_snapshots") or [] if isinstance(item, dict)]
    snapshot_by_id = {int(item["camera_id"]): item for item in snapshots}
    changed: list[int] = []
    with _worker_side_effect_fence(worker_session):
        for camera in _paused_camera_rows(db, sorted(snapshot_by_id)):
            snapshot = snapshot_by_id[int(camera.id)]
            if bool(camera.enabled) and bool(snapshot.get("enabled_intent")):
                camera.enabled = False
                camera.status = "storage_switch_paused"
                camera.updated_at = datetime.utcnow()
                db.add(camera)
                snapshot["changed_by_operation"] = True
                changed.append(int(camera.id))
        db.commit()
    return changed, snapshots


def _restore_operation_cameras(
    db: Session,
    state: dict[str, Any],
    *,
    reason: str,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, list[int]]:
    snapshots = [item for item in state.get("camera_snapshots") or [] if isinstance(item, dict)]
    by_id = {int(item["camera_id"]): item for item in snapshots}
    with _worker_side_effect_fence(worker_session):
        result = _restore_cameras(
            db,
            [int(item) for item in state.get("paused_camera_ids") or []],
            reason=reason,
            snapshot_by_id=by_id,
        )
        db.commit()
    return result


def _verify_all_configured_roots(
    db: Session,
    *,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    for root in db.query(ArchiveRoot).filter(ArchiveRoot.retired_at.is_(None)).order_by(ArchiveRoot.id.asc()).all():
        _assert_worker_session(worker_session)
        access = _root_access(root, require_write=bool(root.is_active))
        _assert_worker_session(worker_session)
        if not access.get("ok"):
            problems.append({"root_id": str(root.id), "reason_code": access.get("reason_code")})
    return {"ok": not problems, "problems": problems}


def _fail_before_target_apply(
    db: Session,
    state: dict[str, Any],
    *,
    failed_step: str,
    reason_code: str,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    _assert_worker_session(worker_session)
    effective = _detect_effective_root(db, state)
    _assert_worker_session(worker_session)
    if not effective.get("ok") or str(effective.get("root_id") or "") != str(state.get("previous_root_id") or ""):
        return _mark_recovery_required(
            str(state["operation_id"]),
            failed_step=failed_step,
            reason_code=reason_code,
            effective_root_id=effective.get("root_id"),
            effective_root_label=effective.get("root_label"),
            worker_session=worker_session,
        )
    restore = _restore_operation_cameras(
        db,
        state,
        reason="activation_failed_before_apply",
        worker_session=worker_session,
    )
    return _terminalize_from(
        state,
        worker_session=worker_session,
        status="failed",
        current_step=failed_step,
        reason_code=reason_code,
        presentation_key="storage_activation_failed_previous_location_preserved",
        presentation_data={"effective_root_label": effective.get("root_label")},
        effective_active_root_id=effective.get("root_id"),
        effective_active_root_label=effective.get("root_label"),
        restored_camera_ids=restore["restored"],
        camera_restore_failed_ids=restore["failed"],
    )


def _run_verified_rollback(
    db: Session,
    state: dict[str, Any],
    *,
    original_reason_code: str,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    _assert_worker_session(worker_session)
    operation_id = str(state["operation_id"])
    previous = _root_by_id(db, state.get("previous_root_id"))
    if previous is None or previous.retired_at is not None:
        effective = _detect_effective_root(db, state)
        _assert_worker_session(worker_session)
        return _mark_recovery_required(
            operation_id,
            failed_step="rollback_requested",
            reason_code="rollback_root_missing",
            effective_root_id=effective.get("root_id"),
            effective_root_label=effective.get("root_label"),
            worker_session=worker_session,
        )

    runtime_request_id = None
    if state.get("status") != "failed_recovery_required":
        runtime_request_id = state.get("rollback_runtime_request_id")
    if not runtime_request_id:
        state = _transition_from(
            state,
            worker_session=worker_session,
            status="running",
            current_step="rollback_requested",
            reason_code=original_reason_code,
            presentation_key="storage_activation_restoring_previous_location",
            rollback_status="pending",
            rollback_failed_step=None,
            rollback_reason_code=None,
        )
        runtime_request_id = f"archive-root-rollback-{uuid.uuid4().hex}"
        try:
            with _worker_side_effect_fence(worker_session):
                write_archive_roots_runtime_files(db)
                queue_runtime_activation(
                    str(state.get("previous_host_path") or ""),
                    request_prefix="archive-root-rollback",
                    operation_id=operation_id,
                    physical_identity=getattr(previous, "physical_identity", None),
                    runtime_request_id=runtime_request_id,
                )
        except WorkerLeaseLost:
            raise
        except Exception:
            logger.exception("Failed to queue archive-root rollback for %s", operation_id)
            effective = _detect_effective_root(db, state)
            _assert_worker_session(worker_session)
            return _mark_recovery_required(
                operation_id,
                failed_step="rollback_requested",
                reason_code="rollback_request_failed",
                effective_root_id=effective.get("root_id"),
                effective_root_label=effective.get("root_label"),
                worker_session=worker_session,
            )
        state = _transition_from(
            state,
            worker_session=worker_session,
            complete_step="rollback_requested",
            current_step="rollback_runtime_applied",
            rollback_status="running",
            rollback_runtime_request_id=runtime_request_id,
        )

    helper = _wait_for_helper(
        operation_id,
        str(runtime_request_id),
        **_worker_session_kwargs(worker_session),
    )
    if not helper.get("ok"):
        effective = _detect_effective_root(db, state)
        _assert_worker_session(worker_session)
        return _mark_recovery_required(
            operation_id,
            failed_step="rollback_runtime_applied",
            reason_code=str(helper.get("reason_code") or "rollback_runtime_failed"),
            effective_root_id=effective.get("root_id"),
            effective_root_label=effective.get("root_label"),
            worker_session=worker_session,
        )
    verified = _verify_effective_root(
        db,
        root_id=state.get("previous_root_id"),
        expected_host_path=state.get("previous_host_path"),
        operation_id=operation_id,
        request_id=str(runtime_request_id),
        worker_session=worker_session,
    )
    if not verified.get("ok"):
        effective = _detect_effective_root(db, state)
        _assert_worker_session(worker_session)
        return _mark_recovery_required(
            operation_id,
            failed_step="rollback_access_checking",
            reason_code=str(verified.get("reason_code") or "rollback_access_failed"),
            effective_root_id=effective.get("root_id"),
            effective_root_label=effective.get("root_label"),
            worker_session=worker_session,
        )

    _set_active_root(db, state, str(previous.id), worker_session=worker_session)
    state = _transition_from(
        state,
        worker_session=worker_session,
        complete_step="rollback_runtime_applied",
        current_step="rollback_access_checking",
        effective_active_root_id=str(previous.id),
        effective_active_root_label=str(previous.label or previous.id),
    )
    state = _transition_from(
        state,
        worker_session=worker_session,
        complete_step="rollback_access_checking",
        current_step="rollback_access_checked",
    )
    state = _transition_from(
        state,
        worker_session=worker_session,
        complete_step="rollback_access_checked",
        current_step="rollback_completed",
        rollback_status="completed",
    )
    state = _transition_from(
        state,
        worker_session=worker_session,
        complete_step="rollback_completed",
        current_step="cameras_restoring",
    )
    restore = _restore_operation_cameras(
        db,
        state,
        reason="activation_rolled_back",
        worker_session=worker_session,
    )
    state = _transition_from(
        state,
        worker_session=worker_session,
        complete_step="cameras_restoring",
        current_step="cameras_restored",
        restored_camera_ids=restore["restored"],
        camera_restore_failed_ids=restore["failed"],
    )
    return _terminalize_from(
        state,
        worker_session=worker_session,
        status="failed",
        current_step="activation_rolled_back",
        reason_code=original_reason_code,
        presentation_key="storage_activation_failed_previous_location_restored",
        presentation_data={"effective_root_label": str(previous.label or previous.id)},
        complete_step="cameras_restored",
        effective_active_root_id=str(previous.id),
        effective_active_root_label=str(previous.label or previous.id),
        rollback_status="completed",
        restored_camera_ids=restore["restored"],
        camera_restore_failed_ids=restore["failed"],
    )


def _run_activation_operation(
    db: Session,
    operation_id: str,
    *,
    worker_session: WorkerLeaseSession | None = None,
) -> dict[str, Any]:
    _assert_worker_session(worker_session)
    state = read_pending_archive_root_activation()
    if not state or str(state.get("operation_id") or "") != operation_id:
        return {"status": "no_pending_activation"}
    if state.get("status") == "failed_recovery_required":
        return _run_verified_rollback(
            db,
            state,
            original_reason_code=str(state.get("reason_code") or "activation_recovery_required"),
            **_worker_session_kwargs(worker_session),
        )
    if str(state.get("current_step") or "").startswith("rollback_") or state.get("rollback_status") in {"pending", "running"}:
        return _run_verified_rollback(
            db,
            state,
            original_reason_code=str(state.get("reason_code") or "target_archive_access_failed"),
            **_worker_session_kwargs(worker_session),
        )

    completed = set(state.get("completed_steps") or [])
    if not state.get("target_preflight_validated_at"):
        preflight = _target_preflight(db, state, **_worker_session_kwargs(worker_session))
        if not preflight.get("ok"):
            return _fail_before_target_apply(
                db,
                state,
                failed_step="root_preflight_checked",
                reason_code=str(preflight.get("reason_code") or "target_root_preflight_failed"),
                **_worker_session_kwargs(worker_session),
            )
        state = _transition_from(
            state,
            worker_session=worker_session,
            status="running",
            current_step="recordings_stopping",
            presentation_key="storage_activation_stopping_recordings",
            target_preflight_validated_at=_utc_now(),
        )
        completed = set(state.get("completed_steps") or [])

    if "recordings_stopping" not in completed:
        changed_ids, snapshots = _pause_operation_cameras(
            db,
            state,
            **_worker_session_kwargs(worker_session),
        )
        state = _transition_from(
            state,
            worker_session=worker_session,
            complete_step="recordings_stopping",
            current_step="recordings_stopped",
            camera_snapshots=snapshots,
            paused_camera_ids=changed_ids,
        )
        completed = set(state.get("completed_steps") or [])

    if "recordings_stopped" not in completed:
        changed_ids = [int(item) for item in state.get("paused_camera_ids") or []]
        stopped = _wait_for_recordings_to_stop(
            db,
            changed_ids,
            **_worker_session_kwargs(worker_session),
        )
        if not stopped.get("ok"):
            return _fail_before_target_apply(
                db,
                state,
                failed_step="recordings_stopped",
                reason_code="recordings_did_not_stop_in_time",
                **_worker_session_kwargs(worker_session),
            )
        state = _transition_from(
            state,
            worker_session=worker_session,
            complete_step="recordings_stopped",
            current_step="root_preflight_checked",
            presentation_key="storage_activation_switching_location",
        )
        completed = set(state.get("completed_steps") or [])

    if "root_preflight_checked" not in completed:
        # Revalidate immediately before the runtime mutation. The preliminary
        # check prevents an avoidable recording pause; this second check closes
        # the race between that check and the completed stop operation.
        preflight = _target_preflight(db, state, **_worker_session_kwargs(worker_session))
        if not preflight.get("ok"):
            return _fail_before_target_apply(
                db,
                state,
                failed_step="root_preflight_checked",
                reason_code=str(preflight.get("reason_code") or "target_root_preflight_failed"),
                **_worker_session_kwargs(worker_session),
            )
        state = _transition_from(
            state,
            worker_session=worker_session,
            complete_step="root_preflight_checked",
            current_step="runtime_activation_requested",
            presentation_key="storage_activation_switching_location",
        )
        completed = set(state.get("completed_steps") or [])

    if "runtime_activation_requested" not in completed:
        target = _root_by_id(db, state.get("target_root_id"))
        if target is None:
            return _fail_before_target_apply(
                db,
                state,
                failed_step="runtime_activation_requested",
                reason_code="target_archive_root_missing",
                **_worker_session_kwargs(worker_session),
            )
        runtime_request_id = f"archive-root-target-{uuid.uuid4().hex}"
        try:
            with _worker_side_effect_fence(worker_session):
                write_archive_roots_runtime_files(db)
                queue_runtime_activation(
                    str(state.get("target_host_path") or ""),
                    request_prefix="archive-root-target",
                    operation_id=operation_id,
                    physical_identity=getattr(target, "physical_identity", None),
                    runtime_request_id=runtime_request_id,
                )
        except WorkerLeaseLost:
            raise
        except Exception:
            logger.exception("Failed to queue archive-root activation for %s", operation_id)
            return _fail_before_target_apply(
                db,
                state,
                failed_step="runtime_activation_requested",
                reason_code="runtime_activation_request_failed",
                **_worker_session_kwargs(worker_session),
            )
        state = _transition_from(
            state,
            worker_session=worker_session,
            complete_step="runtime_activation_requested",
            current_step="runtime_applied",
            runtime_request_id=runtime_request_id,
        )
        completed = set(state.get("completed_steps") or [])

    if "runtime_applied" not in completed:
        request_id = str(state.get("runtime_request_id") or "")
        helper = _wait_for_helper(
            operation_id,
            request_id,
            **_worker_session_kwargs(worker_session),
        )
        if not helper.get("ok"):
            effective = _detect_effective_root(db, state)
            _assert_worker_session(worker_session)
            if helper.get("configuration_consistent") is not True:
                if helper.get("reason_code") == "persistent_storage_config_recovery_failed":
                    return _mark_recovery_required(
                        operation_id,
                        failed_step="runtime_applied",
                        reason_code="persistent_storage_config_recovery_failed",
                        effective_root_id=effective.get("root_id"),
                        effective_root_label=effective.get("root_label"),
                        worker_session=worker_session,
                    )
                state = _transition_from(
                    state,
                    worker_session=worker_session,
                    effective_active_root_id=effective.get("root_id"),
                    effective_active_root_label=effective.get("root_label"),
                )
                return _run_verified_rollback(
                    db,
                    state,
                    original_reason_code=str(helper.get("reason_code") or "runtime_activation_state_unconfirmed"),
                    **_worker_session_kwargs(worker_session),
                )
            if str(effective.get("root_id") or "") == str(state.get("target_root_id") or ""):
                state = _transition_from(
                    state,
                    worker_session=worker_session,
                    runtime_apply_completed=True,
                    effective_active_root_id=effective.get("root_id"),
                    effective_active_root_label=effective.get("root_label"),
                )
                return _run_verified_rollback(
                    db,
                    state,
                    original_reason_code=str(helper.get("reason_code") or "runtime_activation_failed"),
                    **_worker_session_kwargs(worker_session),
                )
            if str(effective.get("root_id") or "") == str(state.get("previous_root_id") or ""):
                return _fail_before_target_apply(
                    db,
                    state,
                    failed_step="runtime_applied",
                    reason_code=str(helper.get("reason_code") or "runtime_activation_failed"),
                    **_worker_session_kwargs(worker_session),
                )
            return _mark_recovery_required(
                operation_id,
                failed_step="runtime_applied",
                reason_code=str(helper.get("reason_code") or "runtime_activation_state_unknown"),
                effective_root_id=effective.get("root_id"),
                effective_root_label=effective.get("root_label"),
                worker_session=worker_session,
            )
        verified = _verify_effective_root(
            db,
            root_id=state.get("target_root_id"),
            expected_host_path=state.get("target_host_path"),
            operation_id=operation_id,
            request_id=request_id,
            worker_session=worker_session,
        )
        if not verified.get("ok"):
            state = _transition_from(state, worker_session=worker_session, runtime_apply_completed=True)
            return _run_verified_rollback(
                db,
                state,
                original_reason_code=str(verified.get("reason_code") or "target_archive_access_failed"),
                **_worker_session_kwargs(worker_session),
            )
        target = _set_active_root(
            db,
            state,
            str(state.get("target_root_id")),
            worker_session=worker_session,
        )
        state = _transition_from(
            state,
            worker_session=worker_session,
            complete_step="runtime_applied",
            current_step="cameras_restoring",
            runtime_apply_completed=True,
            effective_active_root_id=str(target.id),
            effective_active_root_label=str(target.label or target.id),
            presentation_key="storage_activation_restoring_recordings",
        )
        completed = set(state.get("completed_steps") or [])

    if "cameras_restored" not in completed:
        restore = _restore_operation_cameras(
            db,
            state,
            reason="activation_completed",
            worker_session=worker_session,
        )
        state = _transition_from(
            state,
            worker_session=worker_session,
            complete_step="cameras_restoring",
            current_step="cameras_restored",
            restored_camera_ids=restore["restored"],
            camera_restore_failed_ids=restore["failed"],
        )
        state = _transition_from(
            state,
            worker_session=worker_session,
            complete_step="cameras_restored",
            current_step="archive_access_checking",
            presentation_key="storage_activation_checking_archive_access",
        )

    access = _verify_all_configured_roots(db, worker_session=worker_session)
    state = _transition_from(
        state,
        worker_session=worker_session,
        complete_step="archive_access_checking",
        current_step="archive_access_checked",
        archive_access_problems=access.get("problems") or [],
    )
    if not access.get("ok"):
        return _terminalize_from(
            state,
            worker_session=worker_session,
            status="failed",
            current_step="archive_access_checked",
            reason_code="archive_read_source_unavailable",
            presentation_key="storage_activation_completed_with_archive_access_problem",
            presentation_data={"effective_root_label": state.get("effective_active_root_label")},
            complete_step="archive_access_checked",
        )

    with _worker_side_effect_fence(worker_session):
        create_event(
            db=db,
            actor=None,
            category="storage",
            event_type="archive_root.activation_completed",
            severity="info",
            message_ru="Archive root activation completed",
            message_en="Archive root activation completed",
            target_type="archive_root",
            target_id=state.get("target_root_id"),
            target_name=state.get("target_root_label"),
            metadata={
                "operation_id": operation_id,
                "previous_root_id": state.get("previous_root_id"),
                "target_root_id": state.get("target_root_id"),
                "restored_camera_ids": state.get("restored_camera_ids") or [],
            },
        )
        db.commit()
    return _terminalize_from(
        state,
        worker_session=worker_session,
        status="completed",
        current_step="completed",
        reason_code=None,
        presentation_key="storage_activation_completed",
        presentation_data={"effective_root_label": state.get("effective_active_root_label")},
        complete_step="archive_access_checked",
    )


def finalize_pending_archive_root_activation(db: Session) -> dict[str, Any]:
    """Compatibility read: operation progression belongs only to the background worker."""
    pending = read_pending_archive_root_activation()
    if not pending:
        return {"status": "no_pending_activation"}
    return _public_state(pending)


def _closeout_worker(operation_id: str) -> None:
    lease = _claim_worker_lease(operation_id)
    if lease is None:
        return
    worker_session = WorkerLeaseSession(lease)
    db: Session | None = None
    try:
        worker_session.start()
        db = SessionLocal()
        state = read_pending_archive_root_activation()
        if state and str(state.get("operation_id") or "") == operation_id:
            if int(state.get("worker_recovery_count") or 0) > 0 or state.get("status") == "failed_recovery_required":
                state = _transition_from(
                    state,
                    worker_session=worker_session,
                    worker_recovery_count=int(state.get("worker_recovery_count") or 0) + 1,
                )
            _run_activation_operation(db, operation_id, worker_session=worker_session)
    except WorkerLeaseLost:
        if db is not None:
            db.rollback()
        logger.warning("Archive-root activation worker lost ownership and exited without mutation for %s", operation_id)
    except ActivationStateConflict:
        if db is not None:
            db.rollback()
        logger.warning("Archive-root activation worker hit a coordination conflict and exited without failure handling for %s", operation_id)
    except Exception:
        if db is not None:
            db.rollback()
        try:
            worker_session.assert_owned()
        except WorkerLeaseLost:
            logger.warning("Archive-root activation worker failed after losing ownership; current operation was not mutated for %s", operation_id)
            return
        logger.exception("Archive-root activation worker failed for %s", operation_id)
        if db is None:
            return
        state = read_pending_archive_root_activation()
        if state and str(state.get("operation_id") or "") == operation_id:
            effective = _detect_effective_root(db, state)
            try:
                worker_session.assert_owned()
                if bool(state.get("runtime_apply_completed")) or str(state.get("current_step") or "").startswith("rollback"):
                    _mark_recovery_required(
                        operation_id,
                        failed_step=str(state.get("current_step") or "activation_worker"),
                        reason_code="activation_worker_failed",
                        effective_root_id=effective.get("root_id"),
                        effective_root_label=effective.get("root_label"),
                        worker_session=worker_session,
                    )
                else:
                    _fail_before_target_apply(
                        db,
                        state,
                        failed_step=str(state.get("current_step") or "activation_worker"),
                        reason_code="activation_worker_failed",
                        worker_session=worker_session,
                    )
            except (WorkerLeaseLost, ActivationStateConflict):
                db.rollback()
                logger.warning("Archive-root activation failure handler lost coordination and exited without further mutation for %s", operation_id)
            except Exception:
                db.rollback()
                logger.exception("Archive-root activation failure handler failed for %s", operation_id)
    finally:
        if db is not None:
            db.close()
        worker_session.stop()
        _release_worker_lease(lease)


def start_archive_root_activation_closeout_worker() -> None:
    pending = read_pending_archive_root_activation()
    if not pending or not pending.get("operation_id"):
        return
    operation_id = str(pending["operation_id"])
    thread = threading.Thread(
        target=_closeout_worker,
        args=(operation_id,),
        name=f"archive-root-activation-{operation_id[-8:]}",
        daemon=True,
    )
    thread.start()
