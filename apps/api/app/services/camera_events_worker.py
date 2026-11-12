"""One leased long-poll per physical connection; no child processes or health probes."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

from app.core.security import decrypt_text
from app.db.session import SessionLocal
from app.models.camera import Camera
from app.models.system_settings import SystemSettings
from app.services.camera_events import cleanup_history, record_signal, source_disconnected
from app.services.onvif_events import DeviceClient, EventDeviceError, kind_for, parse_notification, profile_identities, resolve_source
from app.services.storage_operations_foundation import acquire_worker_lease, release_worker_lease, renew_worker_lease

MAX_CONNECTIONS = 64
_stop = threading.Event()
_wake = threading.Event()
_guard = threading.RLock()
_thread = None
_jobs = {}
_states = {}
_owner = "camera-events:" + str(os.getpid()) + ":" + uuid.uuid4().hex


def connection_groups():
    with SessionLocal() as db:
        configured = db.query(SystemSettings).first()
        if configured is None or not configured.system_initialized:
            return {}
        groups = {}
        # Camera.enabled controls Recorder; Live is intentionally independent.
        # Events likewise remain useful when continuous recording is switched off.
        for camera in db.query(Camera).filter(Camera.deleted_at.is_(None), Camera.status != "deleting", Camera.protocol == "onvif").all():
            if not camera.username or not camera.password_encrypted or not camera.onvif_profile_token:
                continue
            device_id = hashlib.sha256(json.dumps([camera.host.lower(), camera.port, camera.username, camera.onvif_path]).encode()).hexdigest()
            group = groups.setdefault(device_id, {"device_id": device_id, "host": camera.host, "port": camera.port,
                "username": camera.username, "password": decrypt_text(camera.password_encrypted), "onvif_path": camera.onvif_path, "cameras": []})
            group["cameras"].append({"id": camera.id, "name": camera.name, "profile_token": camera.onvif_profile_token})
        for group in groups.values():
            group["fingerprint"] = hashlib.sha256(json.dumps(group, sort_keys=True).encode()).hexdigest()
        return groups


def wake_camera_events():
    _wake.set()


def receiver_states():
    with _guard:
        return {key: dict(value) for key, value in _states.items()}


def _state(group, status, **fields):
    with _guard:
        for camera in group["cameras"]:
            _states[str(camera["id"])] = {"camera_id": camera["id"], "status": status, **fields}


@dataclass
class Receiver:
    group: dict = field(repr=False)
    stop: threading.Event = field(default_factory=threading.Event)
    client: object = field(default=None, repr=False)
    thread: object = None

    def cancel(self):
        self.stop.set()
        if self.client is not None:
            threading.Thread(target=self.client.close, name="camera-events-unsubscribe", daemon=True).start()

    def run(self):
        group, handle, failures = self.group, None, 0
        while not self.stop.is_set() and not _stop.is_set():
            try:
                with SessionLocal() as db:
                    handle = acquire_worker_lease(db, worker_key="camera_events_" + group["device_id"][:32], owner_instance_id=_owner, lease_seconds=150)
                if handle is None:
                    _state(group, "other_receiver")
                    self.stop.wait(60)
                    continue
                _state(group, "connecting")
                self.client = DeviceClient(group["host"], group["port"], group["username"], group["password"], group["onvif_path"])
                topics = self.client.topics()
                identities = profile_identities(self.client.profiles())
                self.client.subscribe()
                failures = 0
                _state(group, "connected", kinds=sorted({kind_for(topic) for topic in topics}), dropped=0)
                minute, accepted, dropped = time.monotonic(), 0, 0
                while not self.stop.is_set() and not _stop.is_set():
                    with SessionLocal() as db:
                        renew_worker_lease(db, handle, lease_seconds=150)
                    started = time.monotonic()
                    messages = self.client.pull()
                    if self.stop.is_set() or _stop.is_set():
                        break
                    with SessionLocal() as db:
                        # A late response cannot write after its lease was replaced.
                        renew_worker_lease(db, handle, lease_seconds=150)
                        active = {c.id for c in db.query(Camera).filter(Camera.id.in_([c["id"] for c in group["cameras"]]),
                                  Camera.deleted_at.is_(None), Camera.status != "deleting", Camera.protocol == "onvif").all()}
                        if active != {c["id"] for c in group["cameras"]}:
                            _wake.set()
                            break
                        for message in messages:
                            if time.monotonic() - minute > 60:
                                minute, accepted = time.monotonic(), 0
                            signal = parse_notification(message)
                            if accepted >= 120 and (signal is None or signal.active is not False):
                                dropped += 1
                                continue
                            if signal is not None:
                                camera = resolve_source(group["cameras"], identities, signal.source)
                                record_signal(db, group, camera, signal)
                                accepted += 1
                    _state(group, "connected", kinds=sorted({kind_for(topic) for topic in topics}), dropped=dropped)
                    # Some devices ignore the requested timeout. Empty replies must
                    # not turn a long-poll into a hot retry loop.
                    if (not messages or accepted >= 120) and time.monotonic() - started < 1:
                        self.stop.wait(30)
            except Exception as exc:
                failures += 1
                status = "unsupported" if isinstance(exc, EventDeviceError) and exc.code == "events_unsupported" else "reconnecting"
                _state(group, status, reason=getattr(exc, "code", "events_connection_failed"))
            finally:
                if self.client is not None:
                    self.client.close()
                    self.client = None
                if handle is not None:
                    try:
                        with SessionLocal() as db:
                            renew_worker_lease(db, handle, lease_seconds=150)
                            source_disconnected(db, group)
                            release_worker_lease(db, handle)
                    except Exception:
                        pass
                    handle = None
            if not self.stop.is_set() and not _stop.is_set():
                self.stop.wait(min(600, 30 * 2 ** min(failures, 4)))


def _run():
    last_cleanup = 0
    while not _stop.is_set():
        try:
            groups = connection_groups()
            for key, job in list(_jobs.items()):
                if key not in groups or groups[key]["fingerprint"] != job.group["fingerprint"]:
                    job.cancel()
                    job.thread.join(timeout=.1)
                    _jobs.pop(key, None)
            active_ids = {str(c["id"]) for group in groups.values() for c in group["cameras"]}
            with _guard:
                for key in list(_states):
                    if key not in active_ids:
                        _states.pop(key, None)
            for key, group in groups.items():
                if key in _jobs:
                    continue
                if len(_jobs) >= MAX_CONNECTIONS:
                    _state(group, "capacity_limit")
                    continue
                receiver = Receiver(group)
                receiver.thread = threading.Thread(target=receiver.run, name="camera-events-device", daemon=True)
                _jobs[key] = receiver
                receiver.thread.start()
            if time.monotonic() - last_cleanup > 300:
                with SessionLocal() as db:
                    cleanup_history(db)
                last_cleanup = time.monotonic()
        except Exception:
            # Transient DB/setup/maintenance unavailability must not kill the API.
            pass
        _wake.wait(60)
        _wake.clear()
    for receiver in list(_jobs.values()):
        receiver.cancel()
    deadline = time.monotonic() + 6
    for receiver in list(_jobs.values()):
        receiver.thread.join(timeout=max(0, deadline - time.monotonic()))
    _jobs.clear()


def start_camera_events_worker():
    global _thread
    with _guard:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _wake.clear()
        _thread = threading.Thread(target=_run, name="camera-events-coordinator", daemon=True)
        _thread.start()


def stop_camera_events_worker():
    _stop.set()
    _wake.set()
    thread = _thread
    if thread:
        thread.join(timeout=7)
