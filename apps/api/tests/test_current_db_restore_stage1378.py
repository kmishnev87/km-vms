from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.routers.maintenance import CurrentRestoreApplyRequest
from app.services import current_db_restore as restore
from app.services import current_db_restore_executor as executor
from app.services import backup_manager
from app.services import maintenance_admission as admission


ARTIFACT_A = "kmvms-db-20260729T120000Z-aaaaaaaaaaaa"
ARTIFACT_B = "kmvms-db-20260729T120100Z-bbbbbbbbbbbb"
SUBMISSION = "674f28e0-b8b9-4b59-a931-00da55df9e4d"
NOW = "2026-07-29T12:00:00Z"
FINGERPRINT = "c" * 64


def _actor(*, user_id: int = 1, username: str = "owner") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username=username, role="owner")


def _evidence(artifact_id: str) -> dict:
    return {
        "artifact_id": artifact_id,
        "artifact_created_at": NOW,
        "artifact_schema_version": 9,
        "db_backend": "postgresql",
        "file_size": 1024,
        "integrity_verified": True,
        "temporary_restore_validated": True,
        "actor_access_verified": True,
        "fingerprint": FINGERPRINT,
        "video_archive_files_included": False,
    }


def _bind_control_roots(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "update_control_root", str(tmp_path / "update"))
    monkeypatch.setattr(settings, "restore_control_root", str(tmp_path / "restore"))
    monkeypatch.setattr(settings, "restore_public_root", str(tmp_path / "public"))
    monkeypatch.setattr(
        settings,
        "maintenance_control_root",
        str(tmp_path / "maintenance"),
    )
    monkeypatch.setattr(
        settings,
        "kmvms_db_backup_root",
        str(tmp_path / "backups"),
    )
    monkeypatch.setenv("KMVMS_DB_BACKUP_ROOT", str(tmp_path / "backups"))
    for name in (
        "update",
        "restore",
        "public",
        "maintenance",
        "backups",
    ):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)


def test_current_restore_request_forbids_extra_authority_fields() -> None:
    with pytest.raises(ValidationError):
        CurrentRestoreApplyRequest(
            artifact_id=ARTIFACT_A,
            submission_id=SUBMISSION,
            confirm=True,
            confirmation_phrase="RESTORE KM VMS",
            database_url="postgresql://forbidden",
        )


def test_current_restore_admission_is_idempotent_and_actor_bound(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_control_roots(tmp_path, monkeypatch)
    monkeypatch.setattr(
        restore,
        "current_restore_preflight",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "can_restore": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        restore,
        "_artifact_evidence",
        lambda artifact_id, **_kwargs: _evidence(artifact_id),
    )
    monkeypatch.setattr(
        restore,
        "assert_no_maintenance_conflicts",
        lambda *_args, **_kwargs: {
            "update": "idle",
            "backup": "idle",
            "schema": "idle",
            "restore": "idle",
        },
    )
    admission_events: list[str] = []
    original_write = restore.write_bounded_json_atomic

    def tracked_write(path, payload):
        if path == restore.restore_request_path():
            admission_events.append("request")
        return original_write(path, payload)

    def record_audit(_candidate):
        admission_events.append("audit")
        return True

    monkeypatch.setattr(restore, "write_bounded_json_atomic", tracked_write)

    first = restore.request_current_restore(
        object(),
        artifact_id=ARTIFACT_A,
        submission_id=SUBMISSION,
        confirm=True,
        confirmation_phrase="RESTORE KM VMS",
        actor=_actor(),
        before_admit=record_audit,
    )
    replay = restore.request_current_restore(
        object(),
        artifact_id=ARTIFACT_A,
        submission_id=SUBMISSION,
        confirm=True,
        confirmation_phrase="RESTORE KM VMS",
        actor=_actor(),
        before_admit=record_audit,
    )

    assert first["accepted"] is True
    assert first["replayed"] is False
    assert replay["operation_id"] == first["operation_id"]
    assert replay["replayed"] is True
    assert admission_events[:2] == ["audit", "request"]
    assert admission_events.count("audit") == 1
    request = json.loads(restore.restore_request_path().read_text(encoding="utf-8"))
    assert request["artifact"]["artifact_id"] == ARTIFACT_A
    assert request["requested_by"]["subject"] == "owner"
    assert "database_url" not in json.dumps(request)

    with pytest.raises(restore.CurrentRestoreBlocked) as conflict:
        restore.request_current_restore(
            object(),
            artifact_id=ARTIFACT_B,
            submission_id=SUBMISSION,
            confirm=True,
            confirmation_phrase="RESTORE KM VMS",
            actor=_actor(),
            before_admit=lambda _candidate: True,
        )
    assert conflict.value.code == "submission_binding_conflict"

    with pytest.raises(restore.CurrentRestoreBlocked):
        restore.read_current_restore_status(actor=_actor(user_id=2, username="other"))


def test_current_restore_public_contract_is_bounded_and_consistent() -> None:
    payload = {
        "schema": restore.RESTORE_PUBLIC_SCHEMA,
        "operation_id": "restore-" + ("a" * 32),
        "submission_id": SUBMISSION,
        "actor_subject": "owner",
        "status": "failed_rolled_back",
        "phase": "failed_rolled_back",
        "artifact": {
            "artifact_id": ARTIFACT_A,
            "artifact_created_at": NOW,
            "artifact_schema_version": 9,
            "db_backend": "postgresql",
        },
        "pre_restore_backup_id": ARTIFACT_B,
        "accepted_at": NOW,
        "started_at": NOW,
        "updated_at": NOW,
        "finished_at": NOW,
        "terminal_result": "failed_rolled_back",
        "reason_code": "test_restore_failed",
        "next_action": "current_database_restored",
        "video_archive_modified": False,
    }
    assert restore.restore_public_contract(payload) == payload
    with_failed_phase = {
        **payload,
        "failed_phase": "restore_running",
    }
    assert (
        restore.restore_public_contract(with_failed_phase)
        == with_failed_phase
    )

    contradictory = {**payload, "next_action": "sign_in_again"}
    assert restore.restore_public_contract(contradictory) is None
    injected = {**payload, "raw_path": "/storage/backups/db/secret.dump"}
    assert restore.restore_public_contract(injected) is None


def test_shared_admission_blocks_update_while_restore_is_active(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_control_roots(tmp_path, monkeypatch)
    (tmp_path / "backups").mkdir(exist_ok=True)
    admission.write_bounded_json_atomic(
        tmp_path / "restore" / "restore-request.json",
        {"state": "admitted"},
    )

    with admission.maintenance_admission_guard():
        with pytest.raises(admission.MaintenanceAdmissionBlocked) as blocked:
            admission.assert_no_maintenance_conflicts("update")

    assert blocked.value.code == "restore_operation_active"


def test_shared_admission_rejects_symlink_lock(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_control_roots(tmp_path, monkeypatch)
    maintenance_root = tmp_path / "maintenance"
    maintenance_root.mkdir(exist_ok=True)
    target = tmp_path / "outside.lock"
    target.write_text("", encoding="utf-8")
    (maintenance_root / "maintenance-admission.lock").symlink_to(target)

    with pytest.raises(admission.MaintenanceAdmissionBlocked) as blocked:
        with admission.maintenance_admission_guard():
            pass

    assert blocked.value.code == "maintenance_admission_lock_unsafe"


def test_stale_destructive_marker_does_not_bind_new_operation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "restore_control_root", str(tmp_path))
    admission.write_bounded_json_atomic(
        tmp_path / executor.DESTRUCTIVE_MARKER_FILE,
        {
            "schema_version": 1,
            "operation_id": "restore-" + ("a" * 32),
            "mutation_started": True,
        },
    )

    assert executor._destructive_started_for(
        "restore-" + ("a" * 32)
    ) is True
    assert executor._destructive_started_for(
        "restore-" + ("b" * 32)
    ) is False


def test_post_restore_integrity_identity_is_deterministic_and_outcome_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "operation_id": "restore-" + ("a" * 32),
        "state": "claimed",
        "requested_by": {
            "user_id": 1,
            "subject": "owner",
            "role": "owner",
        },
    }
    calls: list[tuple[int, str]] = []
    db = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(dispose=lambda: None)
    monkeypatch.setattr(executor, "_session", lambda: (db, engine))

    def start(_db, *, actor, idempotency_key):
        calls.append((actor.id, idempotency_key))
        return {
            "scan_id": "41030000-0000-0000-0000-000000000011",
            "status": "queued",
            "replayed": len(calls) > 1,
        }

    monkeypatch.setattr(executor, "start_integrity_scan", start)

    first = executor._enqueue_integrity_scan(
        request,
        final_db_outcome="source",
    )
    replay = executor._enqueue_integrity_scan(
        request,
        final_db_outcome="source",
    )
    rollback_key = executor._integrity_convergence_identity(
        request,
        "rollback",
    )[1]

    assert first["scan_id"] == replay["scan_id"]
    assert first["idempotency_key"] == replay["idempotency_key"]
    assert first["idempotency_key"] != rollback_key
    assert calls == [
        (1, first["idempotency_key"]),
        (1, first["idempotency_key"]),
    ]


@pytest.mark.parametrize(
    ("mode", "phase"),
    (
        ("source", "after_database_reset"),
        ("rollback", "after_rollback_database_reset"),
    ),
)
def test_restore_fault_injection_requires_explicit_test_mode(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    phase: str,
) -> None:
    _bind_control_roots(tmp_path, monkeypatch)
    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql+psycopg://vms:test@postgres:5432/vms",
    )
    monkeypatch.setattr(
        executor,
        "_exact_artifact",
        lambda *_args, **_kwargs: (
            Path(tmp_path / "artifact.manifest.json"),
            {"backup_file_label": "artifact.dump"},
            {},
        ),
    )
    events: list[str] = []
    monkeypatch.setattr(
        executor,
        "_reset_postgres_database",
        lambda _url: events.append("reset"),
    )
    monkeypatch.setattr(
        executor,
        "_pg_restore",
        lambda _url, _path: events.append("pg_restore"),
    )
    monkeypatch.setenv("KMVMS_TEST_FAULT_INJECTION", "1")
    monkeypatch.setenv("KMVMS_RESTORE_TEST_FAILURE_PHASE", phase)
    request = {"operation_id": "restore-" + ("a" * 32)}

    monkeypatch.setattr(settings, "app_env", "production")
    result = executor._restore(
        request,
        artifact_id=ARTIFACT_A,
        mode=mode,
    )
    assert result["restore_completed"] is True
    assert events == ["reset", "pg_restore"]

    events.clear()
    monkeypatch.setattr(settings, "app_env", "test")
    with pytest.raises(executor.RestoreExecutorBlocked) as injected:
        executor._restore(
            request,
            artifact_id=ARTIFACT_A,
            mode=mode,
        )
    assert injected.value.mutation_started is True
    assert events == ["reset"]


def test_recorder_proof_rejects_pre_start_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(dispose=lambda: None)
    monkeypatch.setattr(
        executor,
        "create_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        executor,
        "_latest_recorder_runtime",
        lambda _engine: {
            "recorder_instance_id": "old-instance",
            "loop_state": "loop",
            "started_at_epoch": 90.0,
            "heartbeat_at_epoch": 101.0,
            "heartbeat_age_seconds": 1.0,
        },
    )

    with pytest.raises(executor.RestoreExecutorBlocked) as blocked:
        executor._recorder_runtime_proof(
            not_before_epoch=100.0,
            timeout_seconds=0,
            poll_seconds=0,
        )

    assert blocked.value.code == "restore_recorder_heartbeat_timeout"


def test_recorder_proof_accepts_fresh_new_instance_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(dispose=lambda: None)
    monkeypatch.setattr(
        executor,
        "create_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        executor,
        "_latest_recorder_runtime",
        lambda _engine: {
            "recorder_instance_id": "new-instance",
            "loop_state": "loop",
            "started_at_epoch": 101.0,
            "heartbeat_at_epoch": 102.0,
            "heartbeat_age_seconds": 1.0,
        },
    )

    proof = executor._recorder_runtime_proof(
        not_before_epoch=100.0,
        timeout_seconds=0,
        poll_seconds=0,
    )

    assert proof == {
        "recorder_container_check_required": True,
        "recorder_instance_current": True,
        "recorder_heartbeat_fresh": True,
        "recorder_loop_operational": True,
    }


def test_concurrent_backup_and_restore_accept_exactly_one(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_control_roots(tmp_path, monkeypatch)
    monkeypatch.setattr(
        restore,
        "current_restore_preflight",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "can_restore": True,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        restore,
        "_artifact_evidence",
        lambda artifact_id, **_kwargs: _evidence(artifact_id),
    )

    def admit_restore() -> str:
        try:
            restore.request_current_restore(
                None,
                artifact_id=ARTIFACT_A,
                submission_id=SUBMISSION,
                confirm=True,
                confirmation_phrase="RESTORE KM VMS",
                actor=_actor(),
                before_admit=lambda _candidate: True,
            )
            return "restore"
        except restore.CurrentRestoreBlocked:
            return "blocked"

    def admit_backup() -> str:
        try:
            backup_manager.begin_backup_operation(
                submission_id="19992670-9c25-4fe3-8cd6-a329f59fa425",
                kind="create",
                actor=_actor(),
                planned_artifact_id=ARTIFACT_B,
                backup_root=tmp_path / "backups",
                db=None,
            )
            return "backup"
        except backup_manager.BackupManagerBlocked:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        restore_future = pool.submit(admit_restore)
        backup_future = pool.submit(admit_backup)
        outcomes = {
            restore_future.result(),
            backup_future.result(),
        }

    assert outcomes in ({"restore", "blocked"}, {"backup", "blocked"})
    assert not (
        tmp_path / "maintenance" / "active-operation.json"
    ).exists()
