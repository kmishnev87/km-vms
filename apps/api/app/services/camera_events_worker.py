"""One leased long-poll per physical connection; no child processes or health probes."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from requests import exceptions as request_errors
from zeep.exceptions import Fault, TransportError

from app.core.security import decrypt_text
from app.db.session import SessionLocal
from app.models.camera import Camera
from app.models.system_settings import SystemSettings
from app.services.camera_event_health import HEALTH_SAMPLE_SECONDS, Recovery, publish_channel, pull_delay, record_transition
from app.services.camera_event_settings import camera_event_reception_selected
from app.services.camera_events import cleanup_history, record_signal, source_disconnected
from app.services.onvif_events import EVENT_KINDS, DeviceClient, EventDeviceError, ignored_notification, kind_for, parse_notification, profile_identities, resolve_source
from app.services.storage_operations_foundation import StorageOperationLeaseLost, acquire_worker_lease, release_worker_lease, renew_worker_lease

MAX_CONNECTIONS = 64
LEASE_SECONDS = 150
LEASE_MAINTENANCE_SECONDS = 55
MANUAL_RETRY_COOLDOWN_SECONDS = 30
_stop = threading.Event()
_wake = threading.Event()
_guard = threading.RLock()
_thread = None
_jobs = {}
_states = {}
_owner = "camera-events:" + str(os.getpid()) + ":" + uuid.uuid4().hex


def camera_receiving_revision(camera):
    fields = ("protocol", "host", "port", "username", "password_encrypted", "onvif_path", "onvif_profile_token")
    return hashlib.sha256(json.dumps([getattr(camera, key, None) for key in fields] +
                                    [camera_event_reception_selected(camera)]).encode()).hexdigest()


def connection_id(camera):
    return hashlib.sha256(json.dumps([camera.host.lower(), camera.port, camera.username, camera.onvif_path]).encode()).hexdigest()


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
            device_id = connection_id(camera)
            group = groups.setdefault(device_id, {"device_id": device_id, "host": camera.host, "port": camera.port,
                "username": camera.username, "password": decrypt_text(camera.password_encrypted), "onvif_path": camera.onvif_path, "cameras": []})
            group["cameras"].append({"id": camera.id, "name": camera.name, "profile_token": camera.onvif_profile_token,
                                     "events_enabled": camera_event_reception_selected(camera), "revision": camera_receiving_revision(camera)})
        groups = {key: group for key, group in groups.items() if any(c["events_enabled"] for c in group["cameras"])}
        for group in groups.values():
            group["fingerprint"] = hashlib.sha256(json.dumps(group, sort_keys=True).encode()).hexdigest()
        return groups


def wake_camera_events():
    _wake.set()


def receiver_states():
    with _guard:
        return {key: dict(value) for key, value in _states.items()}


def receiver_fault_snapshots(cameras):
    """Read the existing receiver state, without probing a camera or decrypting credentials."""
    faults = {}
    now = time.monotonic()
    with _guard:
        for camera in cameras:
            if not camera_event_reception_selected(camera):
                continue
            state = _states.get(str(camera.id)) or {}
            status = state.get("status")
            incident = state.get("incident") or {}
            if status not in {"reconnecting", "unsupported", "retry_exhausted"} or not incident.get("id"):
                continue
            job = _jobs.get(connection_id(camera))
            if (job is None or job.stopping() or not job.thread or not job.thread.is_alive()
                    or job.recovery.incident_id != incident["id"]):
                continue
            revision = camera_receiving_revision(camera)
            if not any(item["id"] == camera.id and item["events_enabled"] and item["revision"] == revision
                       for item in job.group["cameras"]):
                continue
            faults[camera.id] = {
                "group": incident["id"],
                "phase": "exhausted" if status == "retry_exhausted" else "failed",
                "reachability": state.get("reachability"),
                "failed_at": incident.get("last_failure_at"),
                "next_attempt_at": incident.get("next_attempt_at"),
                "retry_available": bool(status == "retry_exhausted" and job.manual_ready_at is not None
                                        and now >= job.manual_ready_at and not job.retry_requested.is_set()),
            }
    return faults


def event_link_evidence(error):
    """Classify what the failed request proved, not what needs to be retried."""
    for _ in range(5):
        if error is None:
            break
        if (isinstance(error, Fault)
                or isinstance(error, TransportError) and error.status_code
                or isinstance(error, request_errors.HTTPError) and error.response is not None
                or getattr(error, "code", None) in {"events_unsupported", "event_response_too_large",
                    "event_response_timeout", "onvif_authentication_failed"}):
            return "reachable"
        if isinstance(error, (request_errors.Timeout, request_errors.ConnectionError, ConnectionError, TimeoutError, socket.gaierror)):
            return "network_failure"
        error = error.__cause__ or error.__context__
    return None


def _state(group, status, **fields):
    with _guard:
        for camera in group["cameras"]:
            if camera["events_enabled"]:
                _states[str(camera["id"])] = {"camera_id": camera["id"], "status": status, **fields}


def group_is_current(db, group):
    # Lock until the current record/commit finishes. A completed opt-out cannot
    # race a late PullMessages response into another history insert.
    rows = db.query(Camera).filter(Camera.id.in_([c["id"] for c in group["cameras"]]),
        Camera.deleted_at.is_(None), Camera.status != "deleting", Camera.protocol == "onvif").order_by(Camera.id).populate_existing().with_for_update().all()
    current = {c.id: camera_receiving_revision(c) for c in rows}
    return current == {c["id"]: c["revision"] for c in group["cameras"]}


def signal_camera(group, identities, signal):
    camera = resolve_source(group["cameras"], identities, signal.source)
    if camera is not None:
        return camera, camera["events_enabled"]
    # Never turn an unknown/disabled source into a device-wide event when only
    # a subset of the physical device's lenses has opted in.
    return None, all(c["events_enabled"] for c in group["cameras"])


class ReceivingConfigurationChanged(Exception):
    pass


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def retry_camera_events(camera):
    """Queue at most one manual attempt on the current, exhausted receiver."""
    with _guard:
        revision = camera_receiving_revision(camera)
        for job in _jobs.values():
            if not job.stopping() and any(
                item["id"] == camera.id and item["events_enabled"] and item["revision"] == revision
                for item in job.group["cameras"]
            ):
                return job.request_retry()
    return "receiver_unavailable"


@dataclass
class Receiver:
    group: dict = field(repr=False)
    stop: threading.Event = field(default_factory=threading.Event)
    client: object = field(default=None, repr=False)
    thread: object = None
    recovery: Recovery = field(default_factory=Recovery)
    retry_requested: threading.Event = field(default_factory=threading.Event)
    manual_ready_at: float | None = None
    generation: str | None = None

    def cancel(self):
        self.stop.set()
        self.retry_requested.set()

    def stopping(self):
        return self.stop.is_set() or _stop.is_set()

    def request_retry(self):
        if self.retry_requested.is_set():
            return "queued"
        if self.manual_ready_at is None:
            return "receiver_not_exhausted"
        if time.monotonic() < self.manual_ready_at:
            return "retry_cooldown"
        self.retry_requested.set()
        return "queued"

    def _lock_group(self, db, lease):
        renew_worker_lease(db, lease, lease_seconds=LEASE_SECONDS, commit=False)
        if not group_is_current(db, self.group):
            raise ReceivingConfigurationChanged

    def _publish(self, lease, phase, *, generation=None, disconnected=False, transitions=()):
        with SessionLocal() as db:
            self._lock_group(db, lease)
            publish_channel(db, self.group, lease, generation, phase, sample=_utcnow(), recovery=self.recovery)
            for transition in transitions:
                record_transition(db, self.group, transition, self.recovery.details())
            if disconnected:
                source_disconnected(db, self.group)
            else:
                db.commit()

    def _disconnect(self, lease):
        try:
            self._publish(lease, "disconnected", disconnected=True)
        except ReceivingConfigurationChanged:
            # Opt-outs still reconcile old "ongoing" history. A camera moved
            # to another live receiver belongs to that receiver instead.
            ids = [item["id"] for item in self.group["cameras"]]
            with SessionLocal() as db:
                renew_worker_lease(db, lease, lease_seconds=LEASE_SECONDS, commit=False)
                rows = db.query(Camera).filter(Camera.id.in_(ids)).order_by(Camera.id).with_for_update().all()
                local_ids = {camera.id for camera in rows if (
                    not camera_event_reception_selected(camera) or camera.deleted_at is not None
                    or camera.status == "deleting" or camera.protocol != "onvif"
                    or connection_id(camera) == self.group["device_id"]
                )}
                source_disconnected(db, {**self.group, "cameras": [
                    item for item in self.group["cameras"] if item["id"] in local_ids
                ]})

    def _wait_to_retry(self, lease, delay):
        deadline = time.monotonic() + delay if delay is not None else None
        with _guard:
            self.retry_requested.clear()
            self.manual_ready_at = time.monotonic() + MANUAL_RETRY_COOLDOWN_SECONDS if deadline is None else None
        try:
            while not self.stopping():
                remaining = deadline - time.monotonic() if deadline is not None else LEASE_MAINTENANCE_SECONDS
                if remaining <= 0 or self.retry_requested.wait(min(remaining, LEASE_MAINTENANCE_SECONDS)):
                    break
                if not self.stopping() and (deadline is None or time.monotonic() < deadline):
                    # Retain physical ownership across the backoff. This is a
                    # local DB write; an exhausted receiver contacts no camera.
                    with SessionLocal() as db:
                        renew_worker_lease(db, lease, lease_seconds=LEASE_SECONDS)
        finally:
            with _guard:
                self.manual_ready_at = None
                self.retry_requested.clear()

    def _wait_between_pulls(self, started, has_messages):
        deadline = time.monotonic() + pull_delay(time.monotonic() - started, has_messages)
        while not self.stopping():
            now = time.monotonic()
            if now >= deadline:
                return
            renew_at = getattr(self.client, "renew_at", float("inf"))
            if now >= renew_at:
                self.client.renew_if_due()
            else:
                self.stop.wait(min(deadline, renew_at) - now)

    def _receive(self, lease):
        group = self.group
        recovering = self.recovery.incident_id is not None
        self.recovery.next_attempt_at = None
        self.generation = None
        self._publish(lease, "failed" if recovering else "connecting", disconnected=True)
        if self.stopping():
            return
        _state(group, "reconnecting" if recovering else "connecting", incident=self.recovery.details())
        self.client = DeviceClient(group["host"], group["port"], group["username"], group["password"], group["onvif_path"])
        topics = self.client.topics()
        identities = profile_identities(self.client.profiles())
        if self.stopping():
            return
        self.client.subscribe()
        generation = uuid.uuid4().hex
        self.generation = generation
        if not recovering:
            self._publish(lease, "connecting", generation=generation)
        signal_group = {**group, "recording_generation": generation}
        counts = {"messages_received": 0, "messages_parsed": 0, "messages_rejected": 0,
                  "messages_ignored": 0, "messages_unsupported": 0, "events_saved": 0, "excluded_source": 0,
                  "messages_initialized": 0, "messages_inactive": 0, "messages_deleted": 0,
                  "messages_without_state": 0, "messages_unclassified_end": 0, "messages_suppressed": 0}
        unclassified = {}
        supported_kinds = sorted({kind_for(topic) for topic in topics} & set(EVENT_KINDS))
        observed_kinds = set()
        next_sample = time.monotonic()
        while not self.stopping():
            with SessionLocal() as db:
                renew_worker_lease(db, lease, lease_seconds=LEASE_SECONDS)
            started = time.monotonic()
            messages = self.client.pull()
            completed = time.monotonic()
            if self.stopping():
                break
            recovered = self.recovery.received(completed)
            with SessionLocal() as db:
                self._lock_group(db, lease)
                if completed >= next_sample or recovered:
                    publish_channel(
                        db, group, lease, generation, "recovering" if self.recovery.incident_id else "healthy",
                        sample=_utcnow(), recovery=self.recovery,
                    )
                    next_sample = completed + HEALTH_SAMPLE_SECONDS
                if recovered:
                    record_transition(db, group, "recovered", recovered)
                db.commit()
                for message in messages:
                    if self.stopping():
                        break
                    counts["messages_received"] += 1
                    signal = parse_notification(message)
                    if signal is None:
                        counts["messages_ignored" if ignored_notification(message) else "messages_rejected"] += 1
                        continue
                    counts["messages_parsed"] += 1
                    counts["messages_initialized"] += int(signal.initialized)
                    counts["messages_inactive"] += int(signal.active is False)
                    counts["messages_deleted"] += int(signal.operation == "Deleted")
                    counts["messages_without_state"] += int(signal.active is None)
                    if signal.kind not in EVENT_KINDS:
                        counts["messages_unclassified_end" if signal.active is False else "messages_unsupported"] += 1
                        # Diagnostic names only, never raw messages or field values.
                        unclassified[signal.topic] = {"topic": signal.topic, "fields": list(signal.data_keys)}
                        if len(unclassified) > 16:
                            unclassified.pop(next(iter(unclassified)))
                        if signal.active is not False:
                            continue
                    else:
                        observed_kinds.add(signal.kind)
                    if not group_is_current(db, group):
                        raise ReceivingConfigurationChanged
                    camera, allowed = signal_camera(group, identities, signal)
                    if allowed:
                        saved = record_signal(db, signal_group, camera, signal, diagnostics=counts)
                        counts["events_saved"] += int(saved is not None)
                    else:
                        counts["excluded_source"] += 1
            if counts["messages_parsed"]:
                status = "partial_messages" if counts["messages_rejected"] else "connected"
            else:
                status = "invalid_messages" if counts["messages_rejected"] else "waiting"
            _state(group, status, kinds=sorted(set(supported_kinds) | observed_kinds),
                   advertised_kinds=supported_kinds, observed_kinds=sorted(observed_kinds),
                   unclassified=list(unclassified.values()), dropped=0, incident=self.recovery.details(), **counts)
            self._wait_between_pulls(started, bool(messages))

    def _serve(self, lease):
        while not self.stopping():
            try:
                self._receive(lease)
                return
            except (StorageOperationLeaseLost, ReceivingConfigurationChanged):
                raise
            except Exception as exc:
                if self.stopping():
                    return
                reason = exc.code if isinstance(exc, EventDeviceError) else "events_connection_failed"
                delay, transitions = self.recovery.failed(reason, _utcnow())
                phase = "exhausted" if delay is None else "failed"
                self._publish(lease, phase, generation=self.generation, disconnected=True, transitions=transitions)
                status = "retry_exhausted" if delay is None else "unsupported" if reason == "events_unsupported" else "reconnecting"
                _state(self.group, status, reason=reason, incident=self.recovery.details(), reachability=event_link_evidence(exc))
            finally:
                if self.client is not None:
                    self.client.close()
                    self.client = None
            self._wait_to_retry(lease, delay)

    def run(self):
        lease = None
        try:
            while not self.stopping() and lease is None:
                with SessionLocal() as db:
                    lease = acquire_worker_lease(
                        db, worker_key="camera_events_" + self.group["device_id"][:32],
                        owner_instance_id=_owner, lease_seconds=LEASE_SECONDS,
                    )
                if lease is None:
                    _state(self.group, "other_receiver")
                    self.stop.wait(LEASE_MAINTENANCE_SECONDS)
            if lease is not None:
                self._serve(lease)
        except (StorageOperationLeaseLost, ReceivingConfigurationChanged):
            pass
        except Exception:
            # A DB outage must neither cause unbounded replacement threads nor
            # turn an uncommitted incident into a second notification stream.
            self.stop.wait(LEASE_MAINTENANCE_SECONDS)
        finally:
            if lease is not None:
                try:
                    self._disconnect(lease)
                except Exception:
                    pass
                try:
                    with SessionLocal() as db:
                        release_worker_lease(db, lease)
                except Exception:
                    pass
            _wake.set()


def _run():
    last_cleanup = 0
    while not _stop.is_set():
        _wake.clear()
        try:
            groups = connection_groups()
            for key, job in list(_jobs.items()):
                if key not in groups or groups[key]["fingerprint"] != job.group["fingerprint"]:
                    job.cancel()
                if not job.thread.is_alive():
                    with _guard:
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
                with _guard:
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
    for receiver in list(_jobs.values()):
        receiver.cancel()
    deadline = time.monotonic() + 6
    for receiver in list(_jobs.values()):
        receiver.thread.join(timeout=max(0, deadline - time.monotonic()))
    with _guard:
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
