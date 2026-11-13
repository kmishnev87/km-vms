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
from app.services.onvif_events import EVENT_KINDS, DeviceClient, EventDeviceError, ignored_notification, kind_for, parse_notification, profile_identities, resolve_source
from app.services.storage_operations_foundation import acquire_worker_lease, release_worker_lease, renew_worker_lease

MAX_CONNECTIONS = 64
_stop = threading.Event()
_wake = threading.Event()
_guard = threading.RLock()
_thread = None
_jobs = {}
_states = {}
_owner = "camera-events:" + str(os.getpid()) + ":" + uuid.uuid4().hex


def camera_receiving_revision(camera):
    fields = ("protocol", "host", "port", "username", "password_encrypted", "onvif_path", "onvif_profile_token", "events_enabled")
    return hashlib.sha256(json.dumps([getattr(camera, key, None) for key in fields]).encode()).hexdigest()


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
            group["cameras"].append({"id": camera.id, "name": camera.name, "profile_token": camera.onvif_profile_token,
                                     "events_enabled": bool(camera.events_enabled), "revision": camera_receiving_revision(camera)})
        groups = {key: group for key, group in groups.items() if any(c["events_enabled"] for c in group["cameras"])}
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
            if camera["events_enabled"]:
                _states[str(camera["id"])] = {"camera_id": camera["id"], "status": status, **fields}


def group_is_current(db, group):
    # Lock until the current record/commit finishes. A completed opt-out cannot
    # race a late PullMessages response into another history insert.
    rows = db.query(Camera).filter(Camera.id.in_([c["id"] for c in group["cameras"]]),
        Camera.deleted_at.is_(None), Camera.status != "deleting", Camera.protocol == "onvif").populate_existing().with_for_update().all()
    current = {c.id: camera_receiving_revision(c) for c in rows}
    return current == {c["id"]: c["revision"] for c in group["cameras"]}


def signal_camera(group, identities, signal):
    camera = resolve_source(group["cameras"], identities, signal.source)
    if camera is not None:
        return camera, camera["events_enabled"]
    # Never turn an unknown/disabled source into a device-wide event when only
    # a subset of the physical device's lenses has opted in.
    return None, all(c["events_enabled"] for c in group["cameras"])


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
                counts = {"messages_received": 0, "messages_parsed": 0, "messages_rejected": 0,
                          "messages_ignored": 0, "messages_unsupported": 0, "events_saved": 0, "excluded_source": 0}
                unclassified = {}
                supported_kinds = sorted({kind_for(topic) for topic in topics} & set(EVENT_KINDS))
                _state(group, "waiting", kinds=supported_kinds, dropped=0, unclassified=[], **counts)
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
                        if not group_is_current(db, group):
                            _wake.set()
                            return
                        for message in messages:
                            counts["messages_received"] += 1
                            if time.monotonic() - minute > 60:
                                minute, accepted = time.monotonic(), 0
                            signal = parse_notification(message)
                            if signal is None:
                                counts["messages_ignored" if ignored_notification(message) else "messages_rejected"] += 1
                                continue
                            counts["messages_parsed"] += 1
                            if signal.kind not in EVENT_KINDS:
                                counts["messages_unsupported"] += 1
                                # Bounded, in-memory diagnostic types/field names only;
                                # never XML, field values, credentials or history spam.
                                unclassified[signal.topic] = {"topic": signal.topic, "fields": list(signal.data_keys)}
                                if len(unclassified) > 16:
                                    unclassified.pop(next(iter(unclassified)))
                                continue
                            if accepted >= 120 and (signal is None or signal.active is not False):
                                dropped += 1
                                continue
                            if not group_is_current(db, group):
                                _wake.set()
                                return
                            camera, allowed = signal_camera(group, identities, signal)
                            if allowed:
                                saved = record_signal(db, group, camera, signal)
                                counts["events_saved"] += int(saved is not None)
                            else:
                                counts["excluded_source"] += 1
                            accepted += 1
                    status = ("partial_messages" if counts["messages_rejected"] else "connected") if counts["messages_parsed"] else "invalid_messages" if counts["messages_rejected"] else "waiting"
                    _state(group, status, kinds=supported_kinds,
                           unclassified=list(unclassified.values()), dropped=dropped, **counts)
                    # Some devices ignore the requested timeout. Empty replies must
                    # not turn a long-poll into a hot retry loop.
                    elapsed = time.monotonic() - started
                    if (not messages or accepted >= 120) and elapsed < 1:
                        self.stop.wait(30)
                    elif elapsed < 1:
                        # Also bound service/unknown-only fast replies; unlike a
                        # 30s empty-response backoff this keeps detections timely.
                        self.stop.wait(1 - elapsed)
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
                if key not in groups or groups[key]["fingerprint"] != job.group["fingerprint"] or not job.thread.is_alive():
                    job.cancel()
                    job.thread.join(timeout=.1)
                    _jobs.pop(key, None)
            active_ids = {str(c["id"]) for group in groups.values() for c in group["cameras"] if c["events_enabled"]}
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
