from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.user import User
from app.routers import chronology as chronology_router
from app.routers import recordings as recordings_router
from app.routers import cameras as cameras_router
from app.routers import storage as storage_router
from app.services import archive_exports, archive_root_activation, recording_storage, setup_storage
from app.services.recorder_runtime_status import list_camera_recording_states
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    ROOT_RESOLUTION_CONFLICT,
    ROOT_RESOLUTION_RESOLVED,
    ROOT_RESOLUTION_UNRESOLVED,
    migrate_archive_root_identities,
)
from app.services.storage_monitoring import build_storage_monitoring_summary


def test_host_discovery_excludes_volume_named_directory_without_distinct_mount(tmp_path):
    app_dir = tmp_path / "app"
    host_root = tmp_path / "host"
    stub_bin = tmp_path / "bin"
    (app_dir / "data" / "install-control").mkdir(parents=True)
    (host_root / "Volume1").mkdir(parents=True)
    (host_root / "volume1").mkdir(parents=True)
    stub_bin.mkdir()

    df_stub = stub_bin / "df"
    df_stub.write_text(
        "#!/bin/sh\n"
        "for target do :; done\n"
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
        "case \"$target\" in\n"
        "  */Volume1) printf '/dev/physical 1000 250 750 25%% %s\\n' \"$target\" ;;\n"
        "  *) printf '/dev/root 2000 500 1500 25%% %s\\n' \"${target%/*}\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stat_stub = stub_bin / "stat"
    stat_stub.write_text("#!/bin/sh\nprintf 'btrfs\\n'\n", encoding="utf-8")
    df_stub.chmod(0o755)
    stat_stub.chmod(0o755)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"1 0 0:1 / {host_root / 'Volume1'} rw - btrfs /dev/physical rw\n",
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["KM_VMS_MOUNTINFO_FILE"] = str(mountinfo)
    subprocess.run(
        [
            "sh",
            str(repo_root / "scripts" / "km-vms-storage-discovery.sh"),
            "--app-dir",
            str(app_dir),
            "--host-root",
            str(host_root),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    payload = json.loads((app_dir / "data" / "install-control" / "storage-discovery.json").read_text())
    assert [candidate["path"] for candidate in payload["candidates"]] == ["/Volume1"]


def _actor():
    return SimpleNamespace(id=1, username="stage48_owner", role="owner", is_active=True)


def _media_user(db) -> User:
    user = User(
        username="stage48_media_owner",
        full_name="Stage 4.8 media owner",
        password_hash="not-used",
        role="owner",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class _FakeRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


class _RangeRequest:
    client = SimpleNamespace(host="127.0.0.1")

    def __init__(self, value: str | None = None):
        self.headers = {"range": value} if value else {}


@pytest.fixture
def stage48(tmp_path, monkeypatch):
    original = {
        "storage_root": settings.storage_root,
        "storage_previews": settings.storage_previews,
        "storage_exports": settings.storage_exports,
        "storage_install_control": settings.storage_install_control,
    }
    root_a_path = tmp_path / "Volume1" / "ArchiveA"
    root_b_path = tmp_path / "Volume2" / "ArchiveB"
    for root in (root_a_path, root_b_path):
        (root / KMVMS_RECORDINGS_NAMESPACE).mkdir(parents=True)
    settings.storage_root = str(root_a_path)
    settings.storage_previews = str(tmp_path / "previews")
    settings.storage_exports = str(tmp_path / "exports")
    settings.storage_install_control = str(tmp_path / "control")

    engine = create_engine(
        f"sqlite:///{tmp_path / 'stage48.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS recorder_runtime_status (
                    recorder_instance_id VARCHAR(255) PRIMARY KEY,
                    service_status VARCHAR(50) NOT NULL,
                    loop_state VARCHAR(100),
                    started_at TIMESTAMP,
                    heartbeat_at TIMESTAMP NOT NULL,
                    active_jobs_count INTEGER DEFAULT 0 NOT NULL,
                    recording_cameras_count INTEGER DEFAULT 0 NOT NULL,
                    failed_cameras_count INTEGER DEFAULT 0 NOT NULL,
                    last_error TEXT,
                    last_exit_code INTEGER,
                    updated_at TIMESTAMP
                )
                """
            )
        )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    root_a = ArchiveRoot(
        id="root_a",
        label="Archive A",
        root_path=str(root_a_path),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="fs-volume-a",
    )
    root_b = ArchiveRoot(
        id="root_b",
        label="Archive B",
        root_path=str(root_b_path),
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        is_active=False,
        is_readable=True,
        is_writable=True,
        is_available=True,
        physical_identity="fs-volume-b",
    )
    db.add_all([root_a, root_b])
    db.commit()
    monkeypatch.setattr(
        setup_storage,
        "request_archive_root_cleanup",
        lambda root, operation_id, marker_already_removed=False, **_kwargs: {
            "status": "completed",
            "cleanup_status": "completed_removed",
            "operation_id": operation_id,
            "archive_root_id": root.id,
            "marker_removed": True,
            "root_directory_removed": True,
            "root_directory_preserved_reason": "",
            "retry_available": False,
        },
    )
    try:
        yield SimpleNamespace(
            db=db,
            Session=Session,
            engine=engine,
            tmp_path=tmp_path,
            root_a=root_a,
            root_b=root_b,
            root_a_path=root_a_path,
            root_b_path=root_b_path,
        )
    finally:
        db.close()
        engine.dispose()
        for key, value in original.items():
            setattr(settings, key, value)


def _camera(db, name: str, *, enabled: bool = True) -> Camera:
    camera = Camera(
        name=name,
        storage_folder_name=name,
        enabled=enabled,
        protocol="rtsp",
        host="127.0.0.1",
        port=554,
        recording_mode="always",
        status="enabled",
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return camera


def _segment(
    db,
    camera: Camera,
    *,
    root_id: str | None,
    relative_path: str,
    status: str = "finalized",
    job_id: str | None = None,
    progress_at: datetime | None = None,
    resolution: str | None = ROOT_RESOLUTION_RESOLVED,
) -> RecordingSegment:
    now = datetime.utcnow()
    segment = RecordingSegment(
        job_id=job_id,
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path=relative_path,
        relative_path=relative_path,
        started_at=now,
        ended_at=None if status == "writing" else now + timedelta(seconds=5),
        finalized_at=now + timedelta(seconds=5) if status == "finalized" else None,
        duration_sec=5,
        size_bytes=8,
        status=status,
        ownership="KM VMS",
        source="recorder",
        archive_root_id=root_id,
        archive_root_resolution_status=resolution,
        archive_root_resolved_at=now if resolution == ROOT_RESOLUTION_RESOLVED else None,
        storage_namespace=KMVMS_RECORDINGS_NAMESPACE,
        container_format="mkv",
        file_extension=".mkv",
        file_size_verified_at=progress_at,
        media_progress_at=progress_at,
        updated_at=now,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


def _write(root: Path, relative_path: str, content: bytes) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _operation_state(stage48, operation_id: str, *, camera_snapshots=None):
    state = archive_root_activation._new_operation_state(
        operation_id=operation_id,
        previous_root=stage48.root_a,
        target_root=stage48.root_b,
        actor=None,
        camera_snapshots=list(camera_snapshots or []),
        ignored_active_looking_count=0,
    )
    archive_root_activation._acquire_mutation_lock(
        owner=operation_id,
        purpose="archive_root_activation",
        operation_id=operation_id,
    )
    archive_root_activation._persist_new_state(state)
    return state


def test_confirmed_recording_requires_current_instance_media_progress(stage48):
    db = stage48.db
    confirmed = _camera(db, "confirmed")
    no_segment = _camera(db, "no_segment")
    stale = _camera(db, "stale")
    now = datetime.utcnow()
    with stage48.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO recorder_runtime_status (recorder_instance_id, service_status, loop_state, started_at, heartbeat_at, active_jobs_count, recording_cameras_count, failed_cameras_count, updated_at) VALUES (:id, 'running', 'loop', :now, :now, 3, 1, 0, :now)"
            ),
            {"id": "recorder-current", "now": now},
        )
    for camera, job_id, instance in (
        (confirmed, "job-confirmed", "recorder-current"),
        (no_segment, "job-no-segment", "recorder-current"),
        (stale, "job-old-instance", "recorder-old"),
    ):
        db.add(
            RecordingJob(
                id=job_id,
                camera_id=camera.id,
                state="recording",
                started_at=now,
                updated_at=now,
                ffmpeg_pid=1234,
                recorder_instance_id=instance,
            )
        )
    db.commit()
    _segment(
        db,
        confirmed,
        root_id="root_a",
        relative_path="kmvms/recordings/confirmed.mkv",
        status="writing",
        job_id="job-confirmed",
        progress_at=now,
    )
    stale_segment = _segment(
        db,
        stale,
        root_id="root_a",
        relative_path="kmvms/recordings/stale.mkv",
        status="writing",
        job_id="job-old-instance",
        progress_at=now - timedelta(minutes=5),
    )
    stale_segment.file_size_verified_at = now
    db.add(stale_segment)
    db.commit()

    states = {row["camera_id"]: row for row in list_camera_recording_states(db)}
    assert states[confirmed.id]["confirmed_recording"] is True
    assert states[no_segment.id]["confirmed_recording"] is False
    assert states[no_segment.id]["recording_health"] == "starting"
    assert states[stale.id]["confirmed_recording"] is False
    snapshot, ignored = archive_root_activation._activation_camera_snapshot(db, "op-evidence")
    assert [row["camera_id"] for row in snapshot] == [confirmed.id]
    assert ignored == 2


def test_activation_ownership_is_atomic_and_state_transition_rejects_stale_revision(stage48, monkeypatch):
    monkeypatch.setattr(archive_root_activation, "start_archive_root_activation_closeout_worker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(archive_root_activation, "list_camera_recording_states", lambda _db: [])
    monkeypatch.setattr(archive_root_activation, "create_event", lambda **_kwargs: None)
    barrier = threading.Barrier(2)
    results = []

    def request():
        db = stage48.Session()
        try:
            barrier.wait()
            root = db.get(ArchiveRoot, "root_b")
            results.append(archive_root_activation.request_archive_root_activation(db, root=root))
        except archive_root_activation.ArchiveRootMutationConflict as exc:
            results.append({"status": "conflict", "reason_code": exc.blocker.get("reason_code")})
        finally:
            db.close()

    threads = [threading.Thread(target=request), threading.Thread(target=request)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(item.get("status") == "queued" for item in results) == 1
    assert sum(item.get("status") in {"already_running", "conflict"} for item in results) == 1
    state = archive_root_activation.read_pending_archive_root_activation()
    revision = state["revision"]
    updated = archive_root_activation._transition(
        state["operation_id"],
        expected_revision=revision,
        expected_step="snapshot_created",
        current_step="root_preflight_checked",
    )
    assert updated["revision"] == revision + 1
    with pytest.raises(archive_root_activation.ActivationStateConflict):
        archive_root_activation._transition(state["operation_id"], expected_revision=revision, current_step="invalid")


def test_recovery_request_persists_running_rollback_state_before_worker(stage48, monkeypatch):
    state = _operation_state(stage48, "stage48-recovery-request")
    state = archive_root_activation._transition_from(
        state,
        status="failed_recovery_required",
        current_step="failed_recovery_required",
        rollback_status="failed",
        reason_code="rollback_access_failed",
    )
    monkeypatch.setattr(archive_root_activation, "start_archive_root_activation_closeout_worker", lambda *_args, **_kwargs: None)

    result = archive_root_activation.request_archive_root_activation(
        stage48.db,
        root=stage48.root_b,
        actor=_actor(),
        recovery=True,
    )

    persisted = archive_root_activation.read_pending_archive_root_activation()
    assert result["recovery_started"] is True
    assert result["status"] == "running"
    assert result["current_step"] == "rollback_requested"
    assert result["rollback_status"] == "pending"
    assert persisted["status"] == "running"
    assert persisted["current_step"] == "rollback_requested"


def test_stale_helper_result_and_stale_worker_lease_cannot_advance_current_operation(stage48, monkeypatch):
    operation_id = "op-current"
    _operation_state(stage48, operation_id)
    monkeypatch.setattr(
        archive_root_activation,
        "storage_confirmation_status",
        lambda: {"operation_id": "op-old", "runtime_request_id": "request-old", "apply_status": "active"},
    )
    result = archive_root_activation._confirmation_for_request(operation_id, "request-current")
    assert result == {"matched": False, "status": "stale_operation_result"}

    lease = archive_root_activation._worker_lease_path()
    archive_root_activation._write_json(lease, {"operation_id": "op-previous", "owner": "dead", "heartbeat_at": "old"})
    old = datetime.utcnow().timestamp() - archive_root_activation.WORKER_LEASE_STALE_SECONDS - 5
    Path(lease).touch()
    import os

    os.utime(lease, (old, old))
    handle = archive_root_activation._claim_worker_lease(operation_id)
    assert handle is not None
    assert handle.operation_id == operation_id
    archive_root_activation._release_worker_lease(handle)
    assert archive_root_activation.STATE_LOCK_WAIT_SECONDS > archive_root_activation.STATE_LOCK_STALE_SECONDS


def test_activation_preflight_revalidates_target_and_rollback_root_before_identity_backfill(stage48, monkeypatch):
    stage48.root_a.physical_identity = None
    stage48.root_b.physical_identity = None
    stage48.db.add_all([stage48.root_a, stage48.root_b])
    stage48.db.commit()
    calls = []

    def revalidate(root):
        calls.append(root.id)
        return {
            "physical_identity": f"physical-{root.id}",
            "exists": True,
            "writable": True,
        }

    monkeypatch.setattr(setup_storage, "revalidate_configured_archive_root", revalidate)
    monkeypatch.setattr(
        archive_root_activation,
        "_root_access",
        lambda root, require_write: {"ok": root.id == "root_a", "reason_code": None},
    )
    monkeypatch.setattr(archive_root_activation, "_runtime_binding_matches_root", lambda root: root.id == "root_a")
    state = archive_root_activation._new_operation_state(
        operation_id="op-preflight",
        previous_root=stage48.root_a,
        target_root=stage48.root_b,
        actor=None,
        camera_snapshots=[],
        ignored_active_looking_count=0,
    )

    result = archive_root_activation._target_preflight(stage48.db, state)
    stage48.db.refresh(stage48.root_a)
    stage48.db.refresh(stage48.root_b)
    assert result["ok"] is True
    assert calls == ["root_b", "root_a"]
    assert stage48.root_a.physical_identity == "physical-root_a"
    assert stage48.root_b.physical_identity == "physical-root_b"


def test_activation_resume_waits_for_recordings_after_pause_before_runtime_request(stage48, monkeypatch):
    operation_id = "op-resume-after-pause"
    state = _operation_state(stage48, operation_id)
    state = archive_root_activation._transition_from(
        state,
        status="running",
        current_step="recordings_stopped",
        complete_step="recordings_stopping",
        target_preflight_validated_at=datetime.utcnow().isoformat() + "Z",
        paused_camera_ids=[41],
    )
    calls: list[str] = []

    monkeypatch.setattr(
        archive_root_activation,
        "_wait_for_recordings_to_stop",
        lambda _db, camera_ids: calls.append(f"wait:{camera_ids}") or {
            "ok": False,
            "confirmed_recordings": 1,
            "writing_segments": 1,
        },
    )
    monkeypatch.setattr(
        archive_root_activation,
        "_detect_effective_root",
        lambda *_args, **_kwargs: {"ok": True, "root_id": "root_a", "root_label": "Archive A"},
    )
    monkeypatch.setattr(
        archive_root_activation,
        "_restore_operation_cameras",
        lambda *_args, **_kwargs: {"restored": [], "failed": []},
    )
    monkeypatch.setattr(
        archive_root_activation,
        "queue_runtime_activation",
        lambda *_args, **_kwargs: calls.append("runtime-requested"),
    )

    result = archive_root_activation._run_activation_operation(stage48.db, operation_id)

    assert result["status"] == "failed"
    assert result["failed_step"] == "recordings_stopped"
    assert calls == ["wait:[41]"]
    assert "runtime_activation_requested" not in result["completed_steps"]


def test_failure_before_runtime_apply_preserves_verified_previous_root(stage48, monkeypatch):
    state = _operation_state(stage48, "op-before-apply")
    monkeypatch.setattr(
        archive_root_activation,
        "_detect_effective_root",
        lambda *_args, **_kwargs: {"ok": True, "root_id": "root_a", "root_label": "Archive A"},
    )
    result = archive_root_activation._fail_before_target_apply(
        stage48.db,
        state,
        failed_step="root_preflight_checked",
        reason_code="target_root_preflight_failed",
    )
    assert result["status"] == "failed"
    assert result["effective_active_root_id"] == "root_a"
    assert result["rollback_status"] == "not_required"


def test_unproven_persistent_config_after_helper_failure_forces_verified_rollback(stage48, monkeypatch):
    state = _operation_state(stage48, "op-unproven-config")
    state = archive_root_activation._transition_from(
        state,
        status="running",
        complete_step="recordings_stopping",
        current_step="recordings_stopped",
        target_preflight_validated_at=datetime.utcnow().isoformat() + "Z",
    )
    state = archive_root_activation._transition_from(
        state,
        complete_step="recordings_stopped",
        current_step="root_preflight_checked",
    )
    state = archive_root_activation._transition_from(
        state,
        complete_step="root_preflight_checked",
        current_step="runtime_activation_requested",
    )
    state = archive_root_activation._transition_from(
        state,
        complete_step="runtime_activation_requested",
        current_step="runtime_applied",
        runtime_request_id="request-unproven-config",
    )
    monkeypatch.setattr(
        archive_root_activation,
        "_wait_for_helper",
        lambda *_args, **_kwargs: {
            "ok": False,
            "reason_code": "runtime_activation_timeout",
            "configuration_consistent": None,
        },
    )
    monkeypatch.setattr(
        archive_root_activation,
        "_detect_effective_root",
        lambda *_args, **_kwargs: {"ok": True, "root_id": "root_a", "root_label": "Archive A"},
    )
    rollback_calls = []
    monkeypatch.setattr(
        archive_root_activation,
        "_run_verified_rollback",
        lambda _db, current, *, original_reason_code: rollback_calls.append(
            (current["effective_active_root_id"], original_reason_code)
        ) or {"status": "rollback_started"},
    )

    result = archive_root_activation._run_activation_operation(stage48.db, state["operation_id"])

    assert result == {"status": "rollback_started"}
    assert rollback_calls == [("root_a", "runtime_activation_timeout")]


def test_helper_failure_reports_unrestored_persistent_configuration(stage48, monkeypatch):
    monkeypatch.setattr(
        archive_root_activation,
        "storage_confirmation_status",
        lambda: {
            "operation_id": "op-config-failed",
            "runtime_request_id": "request-config-failed",
            "apply_status": "activation_failed",
            "apply_state": {"configuration_consistent": False},
        },
    )

    result = archive_root_activation._wait_for_helper("op-config-failed", "request-config-failed")

    assert result["ok"] is False
    assert result["configuration_consistent"] is False
    assert result["reason_code"] == "persistent_storage_config_recovery_failed"


def test_rollback_failure_enters_recovery_and_does_not_restore_camera(stage48, monkeypatch):
    camera = _camera(stage48.db, "rollback-camera", enabled=False)
    snapshot = [{
        "operation_id": "op-rollback-fail",
        "camera_id": camera.id,
        "name": camera.name,
        "enabled_intent": True,
        "recording_mode": "always",
        "changed_by_operation": True,
    }]
    state = _operation_state(stage48, "op-rollback-fail", camera_snapshots=snapshot)
    state = archive_root_activation._transition(
        state["operation_id"],
        status="running",
        current_step="runtime_applied",
        runtime_apply_completed=True,
        paused_camera_ids=[camera.id],
    )
    monkeypatch.setattr(archive_root_activation, "queue_runtime_activation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(archive_root_activation, "_wait_for_helper", lambda *_args, **_kwargs: {"ok": False, "reason_code": "rollback_runtime_failed"})
    monkeypatch.setattr(archive_root_activation, "_detect_effective_root", lambda *_args, **_kwargs: {"ok": False})

    result = archive_root_activation._run_verified_rollback(stage48.db, state, original_reason_code="target_access_failed")
    stage48.db.refresh(camera)
    assert result["status"] == "failed_recovery_required"
    assert result["rollback_status"] == "failed"
    assert result["restored_camera_ids"] == []
    assert camera.enabled is False
    assert archive_root_activation.archive_root_mutation_blocker()["reason_code"] == "archive_root_recovery_required"


def test_verified_rollback_restores_only_operation_camera_after_previous_root_is_proven(stage48, monkeypatch):
    camera = _camera(stage48.db, "rollback-success", enabled=False)
    untouched = _camera(stage48.db, "untouched", enabled=False)
    snapshot = [{
        "operation_id": "op-rollback-ok",
        "camera_id": camera.id,
        "name": camera.name,
        "enabled_intent": True,
        "recording_mode": "always",
        "changed_by_operation": True,
    }]
    state = _operation_state(stage48, "op-rollback-ok", camera_snapshots=snapshot)
    state = archive_root_activation._transition(
        state["operation_id"],
        status="running",
        current_step="runtime_applied",
        runtime_apply_completed=True,
        paused_camera_ids=[camera.id],
    )
    monkeypatch.setattr(archive_root_activation, "queue_runtime_activation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(archive_root_activation, "_wait_for_helper", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(
        archive_root_activation,
        "_verify_effective_root",
        lambda *_args, **_kwargs: {"ok": True, "root_id": "root_a", "root_label": "Archive A"},
    )

    result = archive_root_activation._run_verified_rollback(stage48.db, state, original_reason_code="target_access_failed")
    stage48.db.refresh(camera)
    stage48.db.refresh(untouched)
    assert result["status"] == "failed"
    assert result["rollback_status"] == "completed"
    assert camera.enabled is True
    assert untouched.enabled is False
    assert result["restored_camera_ids"] == [camera.id]


def test_legacy_resolution_covers_all_states_and_second_run_is_idempotent(stage48):
    camera = _camera(stage48.db, "legacy")
    for index, status in enumerate(("finalized", "writing", "failed", "stale_writing"), start=1):
        relative = f"kmvms/recordings/legacy-{index}.mkv"
        _write(stage48.root_b_path, relative, f"root-b-{index}".encode())
        _segment(
            stage48.db,
            camera,
            root_id=None,
            relative_path=relative,
            status=status,
            resolution=None,
        )
    unresolved = _segment(
        stage48.db,
        camera,
        root_id=None,
        relative_path="kmvms/recordings/not-found.mkv",
        resolution=None,
    )
    conflict = _segment(
        stage48.db,
        camera,
        root_id="root_a",
        relative_path="kmvms/recordings/conflict.mkv",
        resolution=None,
    )
    _write(stage48.root_b_path, conflict.relative_path, b"wrong-root")

    first = migrate_archive_root_identities(stage48.db)
    second = migrate_archive_root_identities(stage48.db)
    rows = stage48.db.query(RecordingSegment).order_by(RecordingSegment.id.asc()).all()
    resolved = rows[:4]
    stage48.db.refresh(unresolved)
    stage48.db.refresh(conflict)
    assert all(row.archive_root_id == "root_b" and row.archive_root_resolution_status == ROOT_RESOLUTION_RESOLVED for row in resolved)
    assert unresolved.archive_root_resolution_status == ROOT_RESOLUTION_UNRESOLVED
    assert conflict.archive_root_id == "root_a"
    assert conflict.archive_root_resolution_status == ROOT_RESOLUTION_CONFLICT
    assert first["uniquely_resolved_count"] == 4
    assert first["root_identity_conflict_count"] == 1
    assert second["changed_count"] == 0
    assert second["idempotent_noop"] is True
    assert second["no_destructive_candidate_count"] == 2


def test_root_delete_partial_failure_preserves_root_identity_and_retry_state(stage48, monkeypatch):
    camera = _camera(stage48.db, "delete-partial")
    first_path = _write(stage48.root_b_path, "kmvms/recordings/01.mkv", b"one")
    second_path = _write(stage48.root_b_path, "kmvms/recordings/02.mkv", b"two")
    first = _segment(stage48.db, camera, root_id="root_b", relative_path="kmvms/recordings/01.mkv")
    second = _segment(stage48.db, camera, root_id="root_b", relative_path="kmvms/recordings/02.mkv")
    original_unlink = Path.unlink

    def fail_second(path, *args, **kwargs):
        if path == second_path:
            raise PermissionError("stage48 simulated unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)
    result = storage_router._delete_inactive_root(stage48.db, stage48.root_b, _actor())
    stage48.db.refresh(stage48.root_b)
    stage48.db.refresh(first)
    stage48.db.refresh(second)
    assert result["status"] == "partial"
    assert not first_path.exists()
    assert second_path.exists()
    assert first.status == "deleted" and first.archive_root_id == "root_b"
    assert second.status == "finalized" and second.archive_root_id == "root_b"
    assert stage48.root_b.retired_at is None
    assert stage48.root_b.retirement_status == "partial_deletion"


def test_root_delete_recovers_metadata_after_commit_failure(stage48):
    camera = _camera(stage48.db, "delete-metadata-recovery")
    path = _write(stage48.root_b_path, "kmvms/recordings/recover.mkv", b"recover")
    segment = _segment(
        stage48.db,
        camera,
        root_id="root_b",
        relative_path="kmvms/recordings/recover.mkv",
    )
    original_commit = stage48.db.commit
    calls = {"count": 0}

    def fail_once():
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("stage48 simulated metadata commit failure")
        return original_commit()

    stage48.db.commit = fail_once
    try:
        result = storage_router._delete_inactive_root(stage48.db, stage48.root_b, _actor())
    finally:
        stage48.db.commit = original_commit

    recovered = stage48.db.get(RecordingSegment, segment.id)
    stage48.db.refresh(stage48.root_b)
    assert result["status"] == "completed"
    assert result["metadata_recovered_count"] == 1
    assert not path.exists()
    assert recovered.status == "deleted"
    assert recovered.archive_root_id == "root_b"
    assert stage48.root_b.retirement_status == "completed"


def test_root_delete_runtime_finalization_failure_stays_visible_and_retryable(stage48, monkeypatch):
    calls = {"count": 0}

    def fail_runtime_files(_db):
        calls["count"] += 1
        raise OSError("stage48 simulated runtime manifest failure")

    monkeypatch.setattr(storage_router, "write_archive_roots_runtime_files", fail_runtime_files)
    result = storage_router._delete_inactive_root(stage48.db, stage48.root_b, _actor())
    stage48.db.refresh(stage48.root_b)

    assert result["status"] == "partial"
    assert result["retry_available"] is True
    assert result["finalization_pending"] is True
    assert stage48.root_b.retired_at is None
    assert stage48.root_b.retirement_status == "partial_finalization"
    assert stage48.root_b.retirement_problem == "runtime_manifest_recovery_failed"
    assert calls["count"] == 2
    blocked = archive_root_activation.request_archive_root_activation(
        stage48.db,
        root=stage48.root_b,
        actor=_actor(),
    )
    assert blocked == {
        "status": "blocked",
        "reason_code": "archive_root_partial_deletion_requires_retry",
    }


def test_duplicate_relative_path_uses_stable_root_identity_and_path_only_is_ambiguous(stage48):
    camera = _camera(stage48.db, "duplicate-path")
    relative = "kmvms/recordings/same.mkv"
    _write(stage48.root_a_path, relative, b"root-a-bytes")
    _write(stage48.root_b_path, relative, b"root-b-bytes")
    segment_a = _segment(stage48.db, camera, root_id="root_a", relative_path=relative)
    segment_b = _segment(stage48.db, camera, root_id="root_b", relative_path=relative)

    resolved_a = recordings_router.get_finalized_segment_by_identity(
        stage48.db,
        segment_id=segment_a.id,
        archive_root_id="root_a",
    )
    resolved_b = recordings_router.get_finalized_segment_by_identity(
        stage48.db,
        segment_id=segment_b.id,
        archive_root_id="root_b",
    )
    assert recordings_router.resolve_segment_file(resolved_a).read_bytes() == b"root-a-bytes"
    assert recordings_router.resolve_segment_file(resolved_b).read_bytes() == b"root-b-bytes"
    with pytest.raises(HTTPException) as exc:
        recordings_router.get_finalized_segment_by_path(stage48.db, relative)
    assert exc.value.status_code == 409


def test_path_only_compatibility_token_cannot_move_to_another_root_owner(stage48):
    user = _media_user(stage48.db)
    camera = _camera(stage48.db, "token-owner-change")
    relative = "kmvms/recordings/token-owner-change.mkv"
    _write(stage48.root_a_path, relative, b"root-a-token-source")
    segment_a = _segment(stage48.db, camera, root_id="root_a", relative_path=relative)

    records_token = recordings_router.issue_recording_media_token(
        recordings_router.RecordingMediaTokenRequest(path=relative, action="stream"),
        db=stage48.db,
        current_user=user,
    )["media_token"]
    chronology_token = chronology_router.issue_chronology_media_token(
        camera_id=camera.id,
        rel_path=relative,
        db=stage48.db,
        current_user=user,
    )["media_token"]

    segment_a.status = "deleted"
    segment_a.deleted_at = datetime.utcnow()
    stage48.db.add(segment_a)
    stage48.db.commit()
    _write(stage48.root_b_path, relative, b"root-b-new-owner")
    segment_b = _segment(stage48.db, camera, root_id="root_b", relative_path=relative)
    assert recordings_router.get_finalized_segment_by_path(stage48.db, relative).id == segment_b.id

    with pytest.raises(HTTPException) as records_denied:
        recordings_router.stream_recording(
            _FakeRequest(),
            path=relative,
            media_token=records_token,
            db=stage48.db,
        )
    assert records_denied.value.status_code == 403

    with pytest.raises(HTTPException) as chronology_denied:
        chronology_router.chronology_file(
            camera_id=camera.id,
            rel_path=relative,
            media_token=chronology_token,
            db=stage48.db,
            request=_FakeRequest(),
        )
    assert chronology_denied.value.status_code == 403


def test_chronology_file_streams_unbuffered_and_honors_byte_ranges(stage48):
    user = _media_user(stage48.db)
    camera = _camera(stage48.db, "chronology-range-stream")
    relative = "kmvms/recordings/chronology-range-stream.mkv"
    content = b"stage481-range-stream"
    _write(stage48.root_a_path, relative, content)
    segment = _segment(stage48.db, camera, root_id="root_a", relative_path=relative)
    segment.mime_type = "video/x-matroska"
    stage48.db.add(segment)
    stage48.db.commit()
    token = chronology_router.issue_chronology_media_token(
        segment_id=segment.id,
        archive_root_id="root_a",
        db=stage48.db,
        current_user=user,
    )["media_token"]

    full = chronology_router.chronology_file(
        segment_id=segment.id,
        archive_root_id="root_a",
        media_token=token,
        db=stage48.db,
        request=_RangeRequest(),
    )
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == str(len(content))
    assert full.headers["x-accel-buffering"] == "no"
    assert "content-range" not in full.headers
    assert full.media_type == "video/x-matroska"

    partial = chronology_router.chronology_file(
        segment_id=segment.id,
        archive_root_id="root_a",
        media_token=token,
        db=stage48.db,
        request=_RangeRequest("bytes=0-3"),
    )
    assert partial.status_code == 206
    assert partial.headers["content-range"] == f"bytes 0-3/{len(content)}"
    assert partial.headers["content-length"] == "4"
    assert partial.headers["x-accel-buffering"] == "no"


def test_bulk_delete_preserves_ambiguous_and_unresolved_refusal_reasons(stage48):
    actor = _media_user(stage48.db)
    camera = _camera(stage48.db, "bulk-refusal")
    duplicate = "kmvms/recordings/bulk-duplicate.mkv"
    _write(stage48.root_a_path, duplicate, b"root-a")
    _write(stage48.root_b_path, duplicate, b"root-b")
    _segment(stage48.db, camera, root_id="root_a", relative_path=duplicate)
    _segment(stage48.db, camera, root_id="root_b", relative_path=duplicate)
    unresolved_path = "kmvms/recordings/bulk-unresolved.mkv"
    unresolved_file = _write(stage48.root_b_path, unresolved_path, b"unresolved")
    unresolved = _segment(
        stage48.db,
        camera,
        root_id="root_b",
        relative_path=unresolved_path,
        resolution=ROOT_RESOLUTION_CONFLICT,
    )

    result = recordings_router.bulk_delete_recordings(
        recordings_router.BulkDeleteRequest(
            paths=[duplicate],
            items=[{"segment_id": unresolved.id, "archive_root_id": "root_b"}],
        ),
        db=stage48.db,
        current_user=actor,
    )

    reasons = {item["reason"] for item in result["items"]}
    assert result["deleted_count"] == 0
    assert result["skipped_count"] == 2
    assert result["not_found_count"] == 0
    assert reasons == {"recording_path_ambiguous", "recording_archive_root_unresolved"}
    assert unresolved_file.exists()


def test_discovery_snapshot_change_is_rejected_before_candidate_request(tmp_path, monkeypatch):
    original_control = settings.storage_install_control
    settings.storage_install_control = str(tmp_path / "control")
    control = Path(settings.storage_install_control)
    control.mkdir(parents=True)
    setup_storage._write_json(
        control / setup_storage.DISCOVERY_FILE,
        {
            "schema_version": 2,
            "snapshot_id": "snapshot-current",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "host_visibility": True,
            "candidates": [{
                "id": "mount-volume2",
                "path": "/Volume2",
                "physical_identity": "fs-volume2",
                "writable": True,
            }],
        },
    )
    queued = []
    monkeypatch.setattr(setup_storage, "_queue_discovery_request", lambda payload: queued.append(payload))
    try:
        with pytest.raises(ValueError, match="snapshot_stale"):
            setup_storage.revalidate_discovery_candidate("mount-volume2", "snapshot-old", "Archive")
        assert queued == []
    finally:
        settings.storage_install_control = original_control


def test_configured_legacy_root_identity_is_proven_then_backfilled(monkeypatch):
    root = SimpleNamespace(root_path="/Volume2/KM-VMS", physical_identity=None)
    monkeypatch.setattr(
        setup_storage,
        "request_discovery_refresh",
        lambda **_kwargs: {"freshness": "current", "snapshot_id": "snapshot-current"},
    )
    monkeypatch.setattr(
        setup_storage,
        "_all_discovered_candidates",
        lambda: [{
            "id": "mount-volume2",
            "path": "/Volume2",
            "physical_identity": "fs-volume2",
            "safety_status": "allowed",
        }],
    )
    monkeypatch.setattr(
        setup_storage,
        "revalidate_discovery_candidate",
        lambda candidate_id, snapshot_id, folder_name, **_kwargs: {
            "candidate_id": candidate_id,
            "snapshot_id": snapshot_id,
            "folder_name": folder_name,
            "final_host_path": "/Volume2/KM-VMS",
            "physical_identity": "fs-volume2",
            "writable": True,
        },
    )

    evidence = setup_storage.revalidate_configured_archive_root(root)

    assert evidence["physical_identity"] == "fs-volume2"
    root.physical_identity = "fs-replaced-volume"
    with pytest.raises(ValueError, match="storage_candidate_physical_identity_changed"):
        setup_storage.revalidate_configured_archive_root(root)


def test_retired_root_reactivation_reuses_identity_and_blocks_conflict_or_partial_state(stage48, monkeypatch):
    identity = {"value": "fs-volume-b"}

    def revalidate(_candidate_id, _snapshot_id, folder_name):
        return {
            "final_host_path": str(stage48.root_b_path),
            "selected_mount_path": str(stage48.root_b_path.parent),
            "folder_name": folder_name,
            "physical_identity": identity["value"],
            "exists": True,
            "writable": True,
        }

    monkeypatch.setattr(setup_storage, "revalidate_discovery_candidate", revalidate)
    payload = storage_router.ArchiveRootCreateRequest(
        candidate_id="mount-volume-b",
        discovery_snapshot_id="snapshot-current",
        folder_name=stage48.root_b_path.name,
    )
    stage48.root_b.retired_at = datetime.utcnow()
    stage48.root_b.retirement_status = "completed"
    stage48.db.add(stage48.root_b)
    stage48.db.commit()

    result = storage_router.create_archive_root(payload, db=stage48.db, current_user=_actor())
    stage48.db.refresh(stage48.root_b)
    assert result["id"] == "root_b"
    assert stage48.root_b.retired_at is None

    stage48.root_b.retired_at = datetime.utcnow()
    stage48.root_b.retirement_status = "completed"
    stage48.db.add(stage48.root_b)
    stage48.db.commit()
    identity["value"] = "fs-replaced-volume"
    with pytest.raises(HTTPException) as conflict:
        storage_router.create_archive_root(payload, db=stage48.db, current_user=_actor())
    assert conflict.value.status_code == 422
    assert conflict.value.detail["error"] == "root_identity_conflict"

    identity["value"] = "fs-volume-b"
    stage48.root_b.retirement_status = "partial_deletion"
    stage48.db.add(stage48.root_b)
    stage48.db.commit()
    with pytest.raises(HTTPException) as partial:
        storage_router.create_archive_root(payload, db=stage48.db, current_user=_actor())
    assert partial.value.status_code == 422
    assert partial.value.detail["error"] == "retired_root_partial_deletion_requires_retry"


def test_namespace_observation_is_scoped_per_root_for_duplicate_relative_paths(stage48):
    camera = _camera(stage48.db, "namespace-scope")
    relative = "kmvms/recordings/duplicate-visible.mkv"
    _write(stage48.root_a_path, relative, b"orphan-on-a")
    _write(stage48.root_b_path, relative, b"owned-on-b")
    _segment(stage48.db, camera, root_id="root_b", relative_path=relative)

    summary = build_storage_monitoring_summary(
        stage48.db,
        include_namespace_observations=True,
        write_audit=False,
    )
    observations = summary["namespace_observations"]
    assert observations["orphan_file_count"] == 1
    by_root = {item["root_id"]: item for item in observations["roots"]}
    assert by_root["root_a"]["orphan_file_count"] == 1
    assert by_root["root_b"]["orphan_file_count"] == 0


def test_current_owned_writing_segment_is_not_reported_as_orphan(stage48):
    camera = _camera(stage48.db, "writing-owned")
    relative = "kmvms/recordings/writing-owned/current.mkv"
    _write(stage48.root_a_path, relative, b"current-writing-media")
    _segment(
        stage48.db,
        camera,
        root_id="root_a",
        relative_path=relative,
        status="writing",
        progress_at=datetime.utcnow(),
    )

    summary = build_storage_monitoring_summary(
        stage48.db,
        include_namespace_observations=True,
        write_audit=False,
    )

    assert summary["namespace_observations"]["orphan_file_count"] == 0
    assert summary["reconciliation_summary"]["orphan_file_count"] == 0


def test_root_access_matrix_does_not_turn_namespace_failure_into_missing_files(stage48, monkeypatch):
    missing = stage48.tmp_path / "missing-root"
    missing_status = recording_storage.root_status(missing)
    assert missing_status["problem"] == "root_missing"

    no_namespace = stage48.tmp_path / "root-without-namespace"
    no_namespace.mkdir()
    namespace_status = recording_storage.root_status(no_namespace)
    assert namespace_status["problem"] == "namespace_missing"

    camera = _camera(stage48.db, "namespace-unreadable")
    relative = "kmvms/recordings/namespace-unreadable.mkv"
    _write(stage48.root_b_path, relative, b"present")
    _segment(stage48.db, camera, root_id="root_b", relative_path=relative)
    namespace = stage48.root_b_path / KMVMS_RECORDINGS_NAMESPACE
    original_access = recording_storage.os.access

    def deny_namespace_read(path, mode):
        if Path(path) == namespace and mode == (recording_storage.os.R_OK | recording_storage.os.X_OK):
            return False
        return original_access(path, mode)

    monkeypatch.setattr(recording_storage.os, "access", deny_namespace_read)
    summary = build_storage_monitoring_summary(
        stage48.db,
        include_namespace_observations=False,
        write_audit=False,
    )
    root = next(item for item in summary["archive_roots"] if item["id"] == "root_b")

    assert root["read_access_state"] == "unavailable"
    assert root["root_access_problem_count"] == 1
    assert root["inaccessible_file_count"] == 1
    assert root["missing_file_count"] == 0
    assert summary["reconciliation_summary"]["root_unavailable_count"] == 1
    assert summary["reconciliation_summary"]["missing_file_count"] == 0


def test_inactive_read_only_root_is_valid(stage48, monkeypatch):
    original_access = recording_storage.os.access
    root_b_namespace = stage48.root_b_path / KMVMS_RECORDINGS_NAMESPACE

    def deny_inactive_write(path, mode):
        if Path(path) == root_b_namespace and mode == (recording_storage.os.W_OK | recording_storage.os.X_OK):
            return False
        return original_access(path, mode)

    monkeypatch.setattr(recording_storage.os, "access", deny_inactive_write)
    inactive_summary = build_storage_monitoring_summary(
        stage48.db,
        include_namespace_observations=False,
        write_audit=False,
    )
    inactive = next(item for item in inactive_summary["archive_roots"] if item["id"] == "root_b")
    assert inactive["read_access_state"] == "available"
    assert inactive["write_access_state"] == "unavailable"
    assert inactive["problem"] is None
    assert inactive["root_access_problem_count"] == 0


def test_active_root_write_failure_is_explicit(stage48, monkeypatch):
    original_access = recording_storage.os.access
    root_a_namespace = stage48.root_a_path / KMVMS_RECORDINGS_NAMESPACE

    def deny_active_write(path, mode):
        if Path(path) == root_a_namespace and mode == (recording_storage.os.W_OK | recording_storage.os.X_OK):
            return False
        return original_access(path, mode)

    monkeypatch.setattr(recording_storage.os, "access", deny_active_write)
    active_summary = build_storage_monitoring_summary(
        stage48.db,
        include_namespace_observations=False,
        write_audit=False,
    )
    active = next(item for item in active_summary["archive_roots"] if item["id"] == "root_a")
    operations = active_summary["storage_operations"]
    assert active["read_access_state"] == "available"
    assert active["write_access_state"] == "unavailable"
    assert active["problem"] == "archive_root_not_writable"
    assert active_summary["reconciliation_summary"]["active_root_write_problem_count"] == 1
    assert operations["path_health"]["writable"] is False
    assert operations["reconciliation"]["active_root_write_problem_count"] == 1


def test_readable_root_with_exact_absent_file_is_one_confirmed_missing_file(stage48):
    camera = _camera(stage48.db, "confirmed-missing")
    _segment(
        stage48.db,
        camera,
        root_id="root_b",
        relative_path="kmvms/recordings/confirmed-missing.mkv",
    )

    summary = build_storage_monitoring_summary(
        stage48.db,
        include_namespace_observations=False,
        write_audit=False,
    )
    root = next(item for item in summary["archive_roots"] if item["id"] == "root_b")
    assert root["read_access_state"] == "available"
    assert root["root_access_problem_count"] == 0
    assert root["missing_file_count"] == 1
    assert summary["reconciliation_summary"]["missing_file_count"] == 1


def test_root_delete_blocks_unresolved_metadata_and_preserves_foreign_sentinel(stage48):
    camera = _camera(stage48.db, "root-delete-safety")
    relative = "kmvms/recordings/delete-safe.mkv"
    file_path = _write(stage48.root_b_path, relative, b"owned")
    sentinel = stage48.root_b_path / "foreign-sentinel.txt"
    sentinel.write_text("foreign", encoding="utf-8")
    segment = _segment(
        stage48.db,
        camera,
        root_id="root_b",
        relative_path=relative,
        resolution=ROOT_RESOLUTION_UNRESOLVED,
    )

    with pytest.raises(HTTPException) as blocked:
        storage_router._delete_inactive_root(stage48.db, stage48.root_b, _actor())
    assert blocked.value.status_code == 409
    assert blocked.value.detail["error"] == "archive_root_delete_preflight_blocked"
    assert file_path.exists() and sentinel.exists()

    segment.archive_root_resolution_status = ROOT_RESOLUTION_RESOLVED
    segment.archive_root_resolved_at = datetime.utcnow()
    stage48.db.add(segment)
    stage48.db.commit()
    result = storage_router._delete_inactive_root(stage48.db, stage48.root_b, _actor())
    assert result["ok"] is True
    assert not file_path.exists()
    assert sentinel.exists()
    assert stage48.root_b_path.exists()


def test_archive_export_and_camera_delete_are_root_aware_with_duplicate_paths(stage48):
    camera_a = _camera(stage48.db, "consumer-a")
    camera_b = _camera(stage48.db, "consumer-b")
    relative = "kmvms/recordings/consumer-duplicate.mkv"
    path_a = _write(stage48.root_a_path, relative, b"root-a-export")
    path_b = _write(stage48.root_b_path, relative, b"root-b-keep")
    start = datetime.utcnow() - timedelta(seconds=10)
    segment_a = _segment(stage48.db, camera_a, root_id="root_a", relative_path=relative)
    segment_b = _segment(stage48.db, camera_b, root_id="root_b", relative_path=relative)
    for segment in (segment_a, segment_b):
        segment.started_at = start
        segment.ended_at = start + timedelta(seconds=5)
        segment.finalized_at = segment.ended_at
        stage48.db.add(segment)
    stage48.db.commit()

    export = archive_exports.preflight_source_segments(
        stage48.db,
        camera_id=camera_a.id,
        start_ts=start,
        end_ts=start + timedelta(seconds=5),
    )
    assert [item.id for item in export.segments] == [segment_a.id]
    assert export.estimated_source_bytes >= len(b"root-a-export")

    result = cameras_router.delete_camera(
        camera_a.id,
        _FakeRequest(),
        delete_files=True,
        db=stage48.db,
        current_user=_actor(),
    )
    stage48.db.refresh(segment_b)
    assert result["status"] == "deleted"
    assert not path_a.exists()
    assert path_b.read_bytes() == b"root-b-keep"
    assert segment_b.status == "finalized"


def test_archive_export_refuses_unresolved_root_identity(stage48):
    camera = _camera(stage48.db, "export-unresolved")
    relative = "kmvms/recordings/export-unresolved.mkv"
    _write(stage48.root_b_path, relative, b"unresolved")
    start = datetime.utcnow() - timedelta(seconds=5)
    segment = _segment(
        stage48.db,
        camera,
        root_id="root_b",
        relative_path=relative,
        resolution=ROOT_RESOLUTION_CONFLICT,
    )
    segment.started_at = start
    segment.ended_at = start + timedelta(seconds=5)
    segment.finalized_at = segment.ended_at
    stage48.db.add(segment)
    stage48.db.commit()

    with pytest.raises(HTTPException) as blocked:
        archive_exports.preflight_source_segments(
            stage48.db,
            camera_id=camera.id,
            start_ts=start,
            end_ts=start + timedelta(seconds=5),
        )
    assert blocked.value.status_code == 409
