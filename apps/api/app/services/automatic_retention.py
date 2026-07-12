from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.recording_retention import run_auto_free_space_cleanup_once, run_automatic_retention_once
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


def automatic_retention_bounds() -> tuple[int, int]:
    return (
        max(1, int(settings.automatic_retention_max_candidates or 25)),
        max(1, int(settings.automatic_retention_max_bytes or 1024 * 1024 * 1024)),
    )


def run_automatic_retention_cycle() -> dict:
    max_candidates, max_bytes = automatic_retention_bounds()
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
            retention_result = run_automatic_retention_once(
                db,
                max_candidates=max_candidates,
                max_bytes=max_bytes,
                operation_heartbeat=heartbeat.assert_owned,
            )
            heartbeat.assert_owned()
            renew_worker_lease(db, leader)
            auto_free_space_result = run_auto_free_space_cleanup_once(
                db,
                max_candidates=max_candidates,
                max_bytes=max_bytes,
                operation_heartbeat=heartbeat.assert_owned,
            )
            heartbeat.assert_owned()
            history_cleanup_count = cleanup_terminal_operations(db)
            heartbeat.assert_owned()
            return {
                "status": "completed",
                "retention": retention_result,
                "auto_free_space_cleanup": auto_free_space_result,
                "history_cleanup_count": history_cleanup_count,
            }
    finally:
        if leader is not None:
            try:
                release_worker_lease(db, leader)
            except Exception:
                db.rollback()
        db.close()


def _worker() -> None:
    interval = automatic_retention_interval_seconds()
    logger.info("Automatic retention worker started interval=%ss", interval)
    while not _stop_event.wait(interval):
        try:
            run_automatic_retention_cycle()
        except Exception:
            logger.exception("Automatic retention worker cycle failed")
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
