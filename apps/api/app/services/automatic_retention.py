from __future__ import annotations

import logging
import threading

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.recording_retention import run_auto_free_space_cleanup_once, run_automatic_retention_once

logger = logging.getLogger(__name__)

_stop_event = threading.Event()
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None


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
    try:
        retention_result = run_automatic_retention_once(db, max_candidates=max_candidates, max_bytes=max_bytes)
        auto_free_space_result = run_auto_free_space_cleanup_once(db, max_candidates=max_candidates, max_bytes=max_bytes)
        return {"retention": retention_result, "auto_free_space_cleanup": auto_free_space_result}
    finally:
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
