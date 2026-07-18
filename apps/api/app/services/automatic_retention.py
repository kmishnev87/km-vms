from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.archive_integrity import cleanup_old_integrity_generations
from app.services.retention_automation import (
    advance_retention_signal,
    claim_retention_signal,
    ensure_retention_signal,
    publish_due_retention_signal,
    retention_slice_preemption_required,
    retention_page_size,
    run_auto_free_pressure_groups,
    run_retention_signal_generation,
)
from app.services.storage_operations_foundation import (
    StorageOperationLeaseLost,
    acquire_worker_lease,
    cleanup_terminal_operations,
    release_worker_lease,
    renew_worker_lease,
)

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_worker_instance_id = f"automatic-retention:{os.getpid()}:{uuid.uuid4().hex}"
_signal_poll_seconds = 30


class _LeaderHeartbeat:
    def __init__(self, handle):
        self.handle = handle
        self.stop_event = threading.Event()
        self.lost = threading.Event()
        self.renew_lock = threading.Lock()
        self.last_verified = time.monotonic()
        self.thread = threading.Thread(target=self._run, name="automatic-retention-db-leader", daemon=True)

    def _renew(self) -> None:
        with self.renew_lock:
            if self.lost.is_set():
                raise StorageOperationLeaseLost("automatic_retention_leader_lease_lost")
            db = SessionLocal()
            try:
                renew_worker_lease(db, self.handle)
                self.last_verified = time.monotonic()
            except Exception:
                db.rollback()
                self.lost.set()
                raise
            finally:
                db.close()

    def _run(self) -> None:
        while not self.stop_event.wait(45):
            try:
                self._renew()
            except Exception:
                return

    def __enter__(self):
        self.thread.start()
        return self

    def assert_owned(self) -> None:
        if self.lost.is_set():
            raise StorageOperationLeaseLost("automatic_retention_leader_lease_lost")
        if time.monotonic() - self.last_verified >= 30:
            self._renew()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)


def automatic_retention_interval_seconds() -> int:
    return max(60, int(settings.automatic_retention_interval_seconds or 3600))


def automatic_retention_page_size() -> int:
    return retention_page_size(settings.automatic_retention_max_candidates)


def _run_storage_history_maintenance() -> dict:
    result = {
        "status": "completed",
        "integrity_deleted_count": 0,
        "operation_deleted_count": 0,
        "failed_phases": [],
    }
    phases = (
        ("integrity", cleanup_old_integrity_generations, "integrity_deleted_count"),
        ("operations", cleanup_terminal_operations, "operation_deleted_count"),
    )
    for phase, cleanup, count_key in phases:
        phase_db = None
        try:
            phase_db = SessionLocal()
            result[count_key] = max(0, int(cleanup(phase_db) or 0))
        except Exception as exc:
            if phase_db is not None:
                try:
                    phase_db.rollback()
                    phase_db.expire_all()
                except Exception:
                    pass
            result["failed_phases"].append(phase)
            logger.warning(
                "Storage history maintenance phase failed phase=%s reason=database_error error_class=%s",
                phase,
                type(exc).__name__,
            )
        finally:
            if phase_db is not None:
                try:
                    phase_db.close()
                except Exception:
                    pass
    failed_count = len(result["failed_phases"])
    if failed_count == len(phases):
        result["status"] = "failed"
    elif failed_count:
        result["status"] = "partial"
    return result


def run_automatic_retention_cycle(*, force_recovery: bool = False) -> dict:
    page_size = automatic_retention_page_size()
    db = SessionLocal()
    leader = None
    try:
        leader = acquire_worker_lease(
            db,
            worker_key="automatic-retention",
            owner_instance_id=_worker_instance_id,
        )
        if leader is None:
            return {"status": "not_leader", "retention": None, "auto_free_space_cleanup": None}
        with _LeaderHeartbeat(leader) as heartbeat:
            heartbeat.assert_owned()
            ensure_retention_signal(db)
            heartbeat.assert_owned()
            renew_worker_lease(db, leader)
            auto_free_space_result = run_auto_free_pressure_groups(db, page_size=page_size)
            heartbeat.assert_owned()
            publish_due_retention_signal(db)
            if force_recovery:
                advance_retention_signal(db)
            signal_handle = claim_retention_signal(db, owner_instance_id=_worker_instance_id)
            retention_result = None
            if signal_handle is not None:
                retention_result = run_retention_signal_generation(
                    db,
                    signal_handle,
                    page_size=page_size,
                    should_preempt=lambda: retention_slice_preemption_required(db),
                )
            heartbeat.assert_owned()
            history_cleanup = _run_storage_history_maintenance()
            heartbeat.assert_owned()
            return {
                "status": "completed",
                "retention": retention_result,
                "auto_free_space_cleanup": auto_free_space_result,
                "history_cleanup_count": int(history_cleanup["operation_deleted_count"]),
                "integrity_history_cleanup_count": int(history_cleanup["integrity_deleted_count"]),
                "history_cleanup": history_cleanup,
            }
    finally:
        if leader is not None:
            try:
                release_worker_lease(db, leader)
            except Exception:
                db.rollback()
        db.close()


def _worker() -> None:
    recovery_interval = automatic_retention_interval_seconds()
    next_recovery_at = time.monotonic()
    logger.info(
        "Automatic retention worker started poll=%ss recovery=%ss",
        _signal_poll_seconds,
        recovery_interval,
    )
    while not _stop_event.is_set():
        try:
            now = time.monotonic()
            force_recovery = now >= next_recovery_at
            run_automatic_retention_cycle(force_recovery=force_recovery)
            if force_recovery:
                next_recovery_at = time.monotonic() + recovery_interval
        except Exception:
            logger.exception("Automatic retention worker cycle failed")
        if _stop_event.wait(_signal_poll_seconds):
            break
    logger.info("Automatic retention worker stopped")


def start_automatic_retention_worker() -> None:
    global _worker_thread
    if not bool(settings.automatic_retention_enabled):
        logger.info("Automatic retention worker disabled by settings")
        return
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _stop_event.clear()
        _worker_thread = threading.Thread(target=_worker, name="automatic-retention-worker", daemon=True)
        _worker_thread.start()


def stop_automatic_retention_worker() -> None:
    global _worker_thread
    with _worker_lock:
        thread = _worker_thread
        if not thread:
            return
        _stop_event.set()
        thread.join(timeout=5)
        _worker_thread = None
