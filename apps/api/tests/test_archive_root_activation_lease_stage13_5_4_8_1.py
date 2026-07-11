from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import archive_root_activation, setup_storage


@pytest.fixture
def lease_control(tmp_path, monkeypatch):
    control = tmp_path / "control"
    monkeypatch.setattr(settings, "storage_install_control", str(control))
    monkeypatch.setattr(archive_root_activation, "WORKER_LEASE_STALE_SECONDS", 0.08)
    monkeypatch.setattr(archive_root_activation, "WORKER_CLAIM_WAIT_SECONDS", 0.04)
    monkeypatch.setattr(archive_root_activation, "WORKER_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(archive_root_activation, "STOP_WAIT_SECONDS", 0.16)
    monkeypatch.setattr(archive_root_activation, "STOP_POLL_SECONDS", 0.01)
    return control


def _write_lease(handle: archive_root_activation.WorkerLeaseHandle) -> None:
    with archive_root_activation._worker_lease_file_guard():
        archive_root_activation._write_json(
            archive_root_activation._worker_lease_path(),
            archive_root_activation._worker_lease_payload(handle),
        )


def _replace_lease(operation_id: str, owner_token: str) -> archive_root_activation.WorkerLeaseHandle:
    handle = archive_root_activation.WorkerLeaseHandle(operation_id, owner_token)
    _write_lease(handle)
    return handle


def _minimal_state(operation_id: str, *, runtime_applied: bool = False) -> dict:
    completed_steps = ["snapshot_created"]
    current_step = "snapshot_created"
    if runtime_applied:
        completed_steps.extend(
            [
                "recordings_stopping",
                "recordings_stopped",
                "root_preflight_checked",
                "runtime_activation_requested",
            ]
        )
        current_step = "runtime_applied"
    return {
        "schema_version": 2,
        "operation_id": operation_id,
        "revision": 1,
        "status": "running" if runtime_applied else "queued",
        "current_step": current_step,
        "completed_steps": completed_steps,
        "target_preflight_validated_at": "2026-07-11T00:00:00Z" if runtime_applied else None,
        "previous_root_id": "root-previous",
        "previous_root_label": "Previous",
        "previous_host_path": "/Volume1/Previous",
        "target_root_id": "root-target",
        "target_root_label": "Target",
        "target_host_path": "/Volume2/Target",
        "runtime_request_id": "request-target" if runtime_applied else None,
        "camera_snapshots": [],
        "paused_camera_ids": [],
        "worker_recovery_count": 0,
        "rollback_status": "not_required",
    }


class _FakeDb:
    def __init__(self, roots: dict[str, object] | None = None):
        self.roots = roots or {}
        self.query_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False

    def get(self, _model, root_id):
        return self.roots.get(str(root_id))

    def query(self, _model):
        self.query_calls += 1
        raise AssertionError("database mutation query must be fenced before execution")

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1

    def close(self):
        self.closed = True


def test_recording_stop_wait_longer_than_stale_threshold_keeps_owner(lease_control, monkeypatch):
    operation_id = "lease-long-recording-stop"
    handle = archive_root_activation._claim_worker_lease(operation_id)
    assert handle is not None
    session = archive_root_activation.WorkerLeaseSession(handle)
    phase_entered = threading.Event()
    release_phase = threading.Event()
    result: dict = {}

    class FakeDb:
        def expire_all(self):
            return None

    def recording_states(_db):
        phase_entered.set()
        assert release_phase.wait(timeout=1)
        return [{"camera_id": 41, "confirmed_recording": True}]

    monkeypatch.setattr(archive_root_activation, "list_camera_recording_states", recording_states)
    monkeypatch.setattr(archive_root_activation, "_writing_segments_count", lambda *_args: 0)

    session.start()
    worker = threading.Thread(
        target=lambda: result.update(
            archive_root_activation._wait_for_recordings_to_stop(
                FakeDb(),
                [41],
                worker_session=session,
            )
        )
    )
    worker.start()
    assert phase_entered.wait(timeout=1)
    time.sleep(archive_root_activation.WORKER_LEASE_STALE_SECONDS * 1.5)
    assert archive_root_activation._claim_worker_lease(operation_id) is None
    release_phase.set()
    worker.join(timeout=1)
    session.stop()

    assert not worker.is_alive()
    assert result["ok"] is False
    assert archive_root_activation._release_worker_lease(handle) is True


def test_target_preflight_longer_than_stale_threshold_keeps_owner(lease_control, monkeypatch):
    operation_id = "lease-long-preflight"
    handle = archive_root_activation._claim_worker_lease(operation_id)
    assert handle is not None
    session = archive_root_activation.WorkerLeaseSession(handle)
    phase_entered = threading.Event()
    release_phase = threading.Event()
    result: dict = {}
    previous = SimpleNamespace(id="root-previous", retired_at=None, physical_identity="volume-previous")
    target = SimpleNamespace(id="root-target", retired_at=None, physical_identity="volume-target")
    db = _FakeDb({"root-previous": previous, "root-target": target})
    state = _minimal_state(operation_id)
    calls = 0

    def revalidate(root):
        nonlocal calls
        calls += 1
        if calls == 1:
            phase_entered.set()
            assert release_phase.wait(timeout=1)
        return {"physical_identity": root.physical_identity}

    monkeypatch.setattr(setup_storage, "revalidate_configured_archive_root", revalidate)
    monkeypatch.setattr(archive_root_activation, "_root_access", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(archive_root_activation, "_runtime_binding_matches_root", lambda _root: True)

    session.start()
    worker = threading.Thread(
        target=lambda: result.update(
            archive_root_activation._target_preflight(db, state, worker_session=session)
        )
    )
    worker.start()
    assert phase_entered.wait(timeout=1)
    time.sleep(archive_root_activation.WORKER_LEASE_STALE_SECONDS * 1.5)
    assert archive_root_activation._claim_worker_lease(operation_id) is None
    release_phase.set()
    worker.join(timeout=1)
    session.stop()

    assert not worker.is_alive()
    assert result["ok"] is True
    assert archive_root_activation._release_worker_lease(handle) is True


def test_old_owner_cannot_touch_newer_owner_lease(lease_control):
    operation_id = "lease-touch-owner-fence"
    old = archive_root_activation._claim_worker_lease(operation_id)
    assert old is not None
    new = _replace_lease(operation_id, "new-owner-touch")

    with pytest.raises(archive_root_activation.WorkerLeaseLost):
        archive_root_activation._touch_worker_lease(old)

    current = archive_root_activation._read_json(archive_root_activation._worker_lease_path())
    assert current["owner_token"] == new.owner_token
    assert archive_root_activation._release_worker_lease(new) is True


def test_old_owner_cannot_release_newer_owner_lease(lease_control):
    operation_id = "lease-release-owner-fence"
    old = archive_root_activation._claim_worker_lease(operation_id)
    assert old is not None
    new = _replace_lease(operation_id, "new-owner-release")

    assert archive_root_activation._release_worker_lease(old) is False
    current = archive_root_activation._read_json(archive_root_activation._worker_lease_path())
    assert current["owner_token"] == new.owner_token
    assert archive_root_activation._release_worker_lease(new) is True


def test_heartbeat_refresh_between_stale_observation_and_reclaim_prevents_takeover(lease_control, monkeypatch):
    operation_id = "lease-stale-observation-race"
    current = archive_root_activation.WorkerLeaseHandle(operation_id, "current-owner")
    _write_lease(current)
    stale_at = time.time() - archive_root_activation.WORKER_LEASE_STALE_SECONDS - 1
    os.utime(archive_root_activation._worker_lease_path(), (stale_at, stale_at))
    candidate = archive_root_activation.WorkerLeaseHandle(operation_id, "candidate-owner")
    original_snapshot = archive_root_activation._worker_lease_snapshot
    calls = 0

    def snapshot_with_interleaved_heartbeat(path: Path):
        nonlocal calls
        calls += 1
        snapshot = original_snapshot(path)
        if calls == 1:
            archive_root_activation._write_json(
                path,
                archive_root_activation._worker_lease_payload(current),
            )
        return snapshot

    monkeypatch.setattr(archive_root_activation, "_worker_lease_snapshot", snapshot_with_interleaved_heartbeat)

    assert archive_root_activation._try_claim_worker_lease(candidate) == "retry"
    persisted = archive_root_activation._read_json(archive_root_activation._worker_lease_path())
    assert persisted["owner_token"] == current.owner_token
    assert archive_root_activation._release_worker_lease(current) is True


def test_lease_replacement_after_preflight_blocks_camera_pause_and_state_mutation(lease_control, monkeypatch):
    operation_id = "lease-lost-before-camera-pause"
    old = archive_root_activation._claim_worker_lease(operation_id)
    assert old is not None
    session = archive_root_activation.WorkerLeaseSession(old)
    state = _minimal_state(operation_id)
    archive_root_activation._write_json(archive_root_activation._pending_path(), state)
    pause_calls: list[str] = []

    def preflight(_db, _state, **_kwargs):
        _replace_lease(operation_id, "new-owner-before-pause")
        return {"ok": True}

    monkeypatch.setattr(archive_root_activation, "_target_preflight", preflight)
    monkeypatch.setattr(
        archive_root_activation,
        "_pause_operation_cameras",
        lambda *_args, **_kwargs: pause_calls.append("pause") or ([], []),
    )

    with pytest.raises(archive_root_activation.WorkerLeaseLost):
        archive_root_activation._run_activation_operation(_FakeDb(), operation_id, worker_session=session)

    assert pause_calls == []
    assert archive_root_activation.read_pending_archive_root_activation() == state


def test_lease_replacement_after_helper_success_blocks_active_root_mutation(lease_control, monkeypatch):
    operation_id = "lease-lost-before-active-root"
    old = archive_root_activation._claim_worker_lease(operation_id)
    assert old is not None
    session = archive_root_activation.WorkerLeaseSession(old)
    state = _minimal_state(operation_id, runtime_applied=True)
    archive_root_activation._write_json(archive_root_activation._pending_path(), state)
    target = SimpleNamespace(id="root-target", retired_at=None, label="Target")
    db = _FakeDb({"root-target": target})
    runtime_writes: list[str] = []

    monkeypatch.setattr(archive_root_activation, "_wait_for_helper", lambda *_args, **_kwargs: {"ok": True})

    def verified(*_args, **_kwargs):
        _replace_lease(operation_id, "new-owner-before-root-mutation")
        return {"ok": True}

    monkeypatch.setattr(archive_root_activation, "_verify_effective_root", verified)
    monkeypatch.setattr(
        archive_root_activation,
        "write_archive_roots_runtime_files",
        lambda _db: runtime_writes.append("runtime-write"),
    )

    with pytest.raises(archive_root_activation.WorkerLeaseLost):
        archive_root_activation._run_activation_operation(db, operation_id, worker_session=session)

    assert db.query_calls == 0
    assert db.commit_calls == 0
    assert runtime_writes == []


def test_stale_worker_conflict_exits_without_failure_or_cleanup_side_effects(lease_control, monkeypatch):
    operation_id = "lease-stale-worker-conflict"
    old = archive_root_activation.WorkerLeaseHandle(operation_id, "old-owner")
    new = archive_root_activation.WorkerLeaseHandle(operation_id, "new-owner")
    _write_lease(old)
    archive_root_activation._write_json(
        archive_root_activation._pending_path(),
        _minimal_state(operation_id),
    )
    db = _FakeDb()
    forbidden_calls: list[str] = []

    monkeypatch.setattr(archive_root_activation, "_claim_worker_lease", lambda _operation_id: old)
    monkeypatch.setattr(archive_root_activation, "SessionLocal", lambda: db)

    def lose_ownership(*_args, **_kwargs):
        _write_lease(new)
        raise archive_root_activation.ActivationStateConflict("stale-worker-cas-conflict")

    monkeypatch.setattr(archive_root_activation, "_run_activation_operation", lose_ownership)
    for name in (
        "_fail_before_target_apply",
        "_mark_recovery_required",
        "_run_verified_rollback",
        "_restore_operation_cameras",
        "_release_mutation_lock",
        "queue_runtime_activation",
        "create_event",
    ):
        monkeypatch.setattr(
            archive_root_activation,
            name,
            lambda *_args, _name=name, **_kwargs: forbidden_calls.append(_name),
        )

    archive_root_activation._closeout_worker(operation_id)

    assert forbidden_calls == []
    persisted = archive_root_activation._read_json(archive_root_activation._worker_lease_path())
    assert persisted["owner_token"] == new.owner_token
    assert db.closed is True


def test_dead_stale_worker_lease_recovers_through_closeout_path(lease_control, monkeypatch):
    operation_id = "lease-dead-worker-recovery"
    dead = archive_root_activation.WorkerLeaseHandle(operation_id, "dead-owner")
    _write_lease(dead)
    stale_at = time.time() - archive_root_activation.WORKER_LEASE_STALE_SECONDS - 1
    os.utime(archive_root_activation._worker_lease_path(), (stale_at, stale_at))
    archive_root_activation._write_json(
        archive_root_activation._pending_path(),
        _minimal_state(operation_id),
    )
    db = _FakeDb()
    observed: list[str] = []

    monkeypatch.setattr(archive_root_activation, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        archive_root_activation,
        "_run_activation_operation",
        lambda _db, current_operation_id, *, worker_session: observed.append(
            f"{current_operation_id}:{worker_session.handle.owner_token}"
        ),
    )

    archive_root_activation._closeout_worker(operation_id)

    assert len(observed) == 1
    assert not observed[0].endswith(dead.owner_token)
    assert not archive_root_activation._worker_lease_path().exists()
    assert db.closed is True


def test_normal_completion_stops_heartbeat_and_releases_exact_ownership(lease_control, monkeypatch):
    operation_id = "lease-normal-completion"
    state = _minimal_state(operation_id)
    archive_root_activation._persist_new_state(state)
    archive_root_activation._acquire_mutation_lock(
        owner=operation_id,
        purpose="archive_root_activation",
        operation_id=operation_id,
    )
    db = _FakeDb()
    sessions: list[archive_root_activation.WorkerLeaseSession] = []

    monkeypatch.setattr(archive_root_activation, "SessionLocal", lambda: db)

    def complete(_db, current_operation_id, *, worker_session):
        assert current_operation_id == operation_id
        sessions.append(worker_session)
        current = archive_root_activation.read_pending_archive_root_activation()
        return archive_root_activation._terminalize_from(
            current,
            worker_session=worker_session,
            status="completed",
            current_step="completed",
            reason_code=None,
            presentation_key="storage_activation_completed",
        )

    monkeypatch.setattr(archive_root_activation, "_run_activation_operation", complete)

    archive_root_activation._closeout_worker(operation_id)

    assert len(sessions) == 1
    assert sessions[0]._thread is not None
    assert not sessions[0]._thread.is_alive()
    assert not archive_root_activation._worker_lease_path().exists()
    assert not archive_root_activation._mutation_lock_path().exists()
    assert not archive_root_activation._pending_path().exists()
    assert archive_root_activation._read_json(archive_root_activation._last_path())["status"] == "completed"
    assert db.closed is True
