"""API-process worker: sleeps on PostgreSQL NOTIFY / local socket when idle.

No recurring health subprocesses, NAS polling by a sleeping phone, or idle DB
scans. A bounded retry timer exists only after an actual failed delivery.
"""
from __future__ import annotations

import logging
import select
import socket
import threading
import uuid

from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.workspace_layout import UserWorkspaceLayout
from app.services import mobile_push_store as store
from app.services.camera_events import now_utc
from app.services.mobile_push import deliver_one

_log = logging.getLogger(__name__)
_guard = threading.Lock()
_thread = None
_stop = threading.Event()
_ready = threading.Event()
_read = _write = None


def wake_mobile_push_worker():
    with _guard:
        channel = _write
    if channel:
        try:
            channel.send(b"1")
        except (BlockingIOError, OSError):
            pass


def _run():
    import psycopg
    connection = None
    last_device = 0
    owner = "mobile-" + uuid.uuid4().hex
    try:
        while not _stop.is_set():
            if connection is None:
                try:
                    address = make_url(settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False)
                    connection = psycopg.connect(address, autocommit=True, connect_timeout=5)
                    connection.execute("LISTEN kmvms_mobile_push")
                    _ready.set()
                except Exception:
                    _ready.clear()
                    if connection:
                        connection.close()
                    connection = None
                    _stop.wait(30)
                    continue
            try:
                with SessionLocal() as db:
                    candidates = store.query(db).filter(UserWorkspaceLayout.layout["enabled"].as_boolean().is_(True))
                    identifiers = [value[0] for value in candidates.with_entities(UserWorkspaceLayout.id).order_by(UserWorkspaceLayout.id).all()]
                    identifiers = [value for value in identifiers if value > last_device] + [value for value in identifiers if value <= last_device]
                progressed = 0
                deferred = []
                for identifier in identifiers:
                    last_device = identifier
                    if _stop.is_set():
                        return
                    if deliver_one(SessionLocal, identifier, owner, defer=deferred.append):
                        progressed += 1
                    if progressed >= 32:
                        break
                if progressed:
                    continue  # Drain a real backlog in bounded, fair passes.
                with SessionLocal() as db:
                    pending = [store.decode(row) for row in store.query(db).filter(UserWorkspaceLayout.layout["enabled"].as_boolean().is_(True))]
                    due = min([row.retry_at for row in pending if row and row.retry_at] + deferred, default=None)
                timeout = max(1, (due - now_utc()).total_seconds()) if due else None
                ready, _, _ = select.select([_read, connection], [], [], timeout)
                if _read in ready:
                    try:
                        while _read.recv(1024):
                            pass
                    except BlockingIOError:
                        pass
                if connection in ready:
                    # The socket is already readable; this drains libpq's notification queue.
                    for _notice in connection.notifies(timeout=0.01, stop_after=256):
                        pass
            except Exception:
                # Never interpolate exception strings: endpoints are capability secrets.
                _log.warning("mobile_push_worker_retry")
                connection.close()
                connection = None
                _ready.clear()
                _stop.wait(30)
    finally:
        _ready.clear()
        if connection:
            connection.close()


def start_mobile_push_worker():
    global _thread, _read, _write
    with _guard:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _ready.clear()
        _read, _write = socket.socketpair()
        _read.setblocking(False)
        _write.setblocking(False)
        _thread = threading.Thread(target=_run, name="mobile-push", daemon=True)
        _thread.start()


def stop_mobile_push_worker():
    global _thread, _read, _write
    _stop.set()
    wake_mobile_push_worker()
    thread = _thread
    if thread:
        thread.join(timeout=15)
    with _guard:
        if _thread and _thread.is_alive():
            return
        for channel in (_read, _write):
            if channel:
                channel.close()
        _read = _write = _thread = None
