import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.db.session import Base
from app.models.user import User
from app.routers.settings import BackupCreateRequest
from app.services import backup_before_upgrade, backup_manager, restore_maintenance
from app.services.backup_before_upgrade import (
    BackupExecutionConfig,
    backup_precondition_status,
    create_backup_before_upgrade,
    run_backup_create_operation,
    verify_backup_manifest,
)
from app.services.backup_manager import (
    BackupManagerBlocked,
    artifact_state_path,
    artifact_version_evidence,
    begin_backup_operation,
    build_backup_operation_diagnostics,
    build_backup_snapshot,
    configured_backup_root,
    current_validation_context,
    get_backup_operation,
    record_backup_operation_disposable_target,
    safe_receipt,
    update_backup_operation,
    write_artifact_state,
)
from app.services.restore_maintenance import (
    RestoreMaintenanceBlocked,
    delete_backup_artifact,
    run_backup_delete_operation,
    run_backup_validation_operation,
)
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from test_schema_migration_runner_stage3 import seed_state


def _actor(actor_id=1):
    return SimpleNamespace(id=actor_id, username=f"owner-{actor_id}", role="owner")


def _db(tmp_path, name="stage13710.sqlite3"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    db.add(
        User(
            username=f"stage13710_owner_{name}",
            full_name="Stage 13.7 Owner",
            password_hash="hash",
            role="owner",
            is_active=True,
        )
    )
    db.commit()
    return engine, db


def _backup(tmp_path, *, now=None):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    try:
        backup = create_backup_before_upgrade(
            db,
            config=BackupExecutionConfig(
                backup_root=root,
                allow_tmp_for_tests=True,
                source="stage13710_test",
                now=now,
            ),
        )
        return engine, db, root, backup
    except Exception:
        db.close()
        engine.dispose()
        raise


def _close(engine, db):
    db.close()
    engine.dispose()


def test_old_backup_integrity_is_independent_from_operation_freshness(tmp_path):
    engine, db, _, backup = _backup(tmp_path, now=datetime.utcnow() - timedelta(days=3))
    generic = verify_backup_manifest(backup["manifest_path"])
    freshness = verify_backup_manifest(backup["manifest_path"], max_age_minutes=60)
    precondition = backup_precondition_status(
        manifest_path=backup["manifest_path"],
        required=True,
        max_age_minutes=60,
    )
    assert generic["valid"] is True
    assert generic["integrity_status"] == "verified"
    assert generic["freshness_status"] == "not_evaluated"
    assert freshness["valid"] is True
    assert freshness["freshness_status"] == "stale"
    assert precondition["status"] == "blocked"
    _close(engine, db)


def test_cheap_snapshot_paginates_exact_totals_without_dump_hashing(tmp_path, monkeypatch):
    root = tmp_path / "safe-db-backups"
    root.mkdir()
    base = datetime(2026, 7, 5, 10, 0, 0)
    expected_ids = []
    for index in range(25):
        created_at = base + timedelta(minutes=index)
        backup_id = f"kmvms-db-{created_at.strftime('%Y%m%dT%H%M%S')}Z-{index:012x}"
        (root / f"{backup_id}.sqlite3").write_bytes(bytes([index]))
        (root / f"{backup_id}.metadata.json").write_text(
            json.dumps({"backup_id": backup_id}),
            encoding="utf-8",
        )
        (root / f"{backup_id}.manifest.json").write_text(
            json.dumps(
                {
                    "backup_id": backup_id,
                    "created_at": created_at.isoformat() + "Z",
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "db_backend": "sqlite",
                    "backup_file_label": f"{backup_id}.sqlite3",
                    "metadata_file_label": f"{backup_id}.metadata.json",
                    "file_size": 1,
                    "checksum_sha256": "not-read-by-overview",
                }
            ),
            encoding="utf-8",
        )
        expected_ids.append(backup_id)
    monkeypatch.setattr(
        backup_before_upgrade,
        "_sha256",
        lambda path: pytest.fail(f"overview must not hash {path}"),
    )
    first = build_backup_snapshot(backup_root=root, db_backend="sqlite", offset=0, limit=10)
    last = build_backup_snapshot(backup_root=root, db_backend="sqlite", offset=20, limit=10)
    assert first["total_count"] == 25
    assert first["total_bytes"] == 25
    assert first["has_more"] is True
    assert len(first["items"]) == 10
    assert [item["artifact_id"] for item in first["items"]] == list(reversed(expected_ids))[:10]
    assert all(item["integrity_status"] == "not_checked" for item in first["items"])
    assert all(item["restore_validation_status"] == "not_performed" for item in first["items"])
    assert len(last["items"]) == 5
    assert last["has_more"] is False


def test_create_receipt_is_idempotent_and_sidecar_has_terminal_evidence_only(tmp_path):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    submission_id = str(uuid.uuid4())
    first = run_backup_create_operation(
        db,
        submission_id=submission_id,
        actor=_actor(),
        backup_root=root,
    )
    replay = run_backup_create_operation(
        db,
        submission_id=submission_id,
        actor=_actor(),
        backup_root=root,
    )
    assert first["state"] == "completed"
    assert replay["replayed"] is True
    assert replay["operation_id"] == first["operation_id"]
    assert replay["artifact_id"] == first["artifact_id"]
    assert len(list(root.glob("*.manifest.json"))) == 1
    state = json.loads(artifact_state_path(root, first["artifact_id"]).read_text(encoding="utf-8"))
    rendered_state = json.dumps(state, sort_keys=True)
    assert state["integrity"]["status"] == "verified"
    assert state["restore_validation"]["status"] == "not_performed"
    assert len(state["integrity"]["evidence"]["checksum_sha256"]) == 64
    assert '"running"' not in rendered_state
    assert '"checking"' not in rendered_state
    assert '"deleted"' not in rendered_state
    rendered_receipt = json.dumps(first, sort_keys=True)
    assert "checksum_sha256" not in rendered_receipt
    assert "backup_file_label" not in rendered_receipt
    assert str(root) not in rendered_receipt
    _close(engine, db)


def test_backup_operation_diagnostics_is_bounded_and_excludes_internal_authority(tmp_path):
    root = tmp_path / "safe-db-backups"
    actor = _actor()
    receipt, _ = begin_backup_operation(
        submission_id=str(uuid.uuid4()),
        kind="create",
        actor=actor,
        planned_artifact_id="kmvms-db-20260729T120000Z-aaaaaaaaaaaa",
        backup_root=root,
    )
    update_backup_operation(
        receipt,
        state="completed",
        phase="completed",
        result={"status": "verified"},
        backup_root=root,
    )
    recent, _ = begin_backup_operation(
        submission_id=str(uuid.uuid4()),
        kind="create",
        actor=actor,
        planned_artifact_id="kmvms-db-20260729T120100Z-bbbbbbbbbbbb",
        backup_root=root,
    )
    internal = backup_manager._load_receipt(root, recent["submission_id"])
    internal["recovery"] = {
        "disposable_database_name": "kmvms_stage5_stage13_restore_validation_aaaaaaaaaaaa",
    }
    internal["checksum_sha256"] = "forbidden-checksum"
    internal["artifact_path"] = "/forbidden/backup.dump"
    backup_manager._write_receipt(root, internal)

    diagnostics = build_backup_operation_diagnostics(backup_root=root, limit=1)

    assert diagnostics["total_count"] == 2
    assert diagnostics["returned_count"] == 1
    assert diagnostics["limit"] == 1
    assert diagnostics["has_more"] is True
    rendered = json.dumps(diagnostics, ensure_ascii=False)
    assert "actor_key" not in rendered
    assert "recovery" not in rendered
    assert "disposable_database_name" not in rendered
    assert "kmvms_stage5_stage13_restore_validation_aaaaaaaaaaaa" not in rendered
    assert "forbidden-checksum" not in rendered
    assert "/forbidden/backup.dump" not in rendered


def test_submission_binding_and_actor_isolation_are_enforced(tmp_path):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    submission_id = str(uuid.uuid4())
    created = run_backup_create_operation(
        db,
        submission_id=submission_id,
        actor=_actor(1),
        backup_root=root,
    )
    with pytest.raises(BackupManagerBlocked) as hidden:
        get_backup_operation(submission_id=submission_id, actor=_actor(2), backup_root=root)
    assert hidden.value.code == "receipt_not_found"
    with pytest.raises(BackupManagerBlocked) as conflict:
        begin_backup_operation(
            submission_id=submission_id,
            kind="check",
            actor=_actor(1),
            artifact_id=created["artifact_id"],
            backup_root=root,
        )
    assert conflict.value.code == "submission_binding_conflict"
    _close(engine, db)


def test_restart_reconciliation_requires_exact_terminal_artifact_evidence(tmp_path):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    actor = _actor()
    created = run_backup_create_operation(
        db,
        submission_id=str(uuid.uuid4()),
        actor=actor,
        backup_root=root,
    )
    artifact_id = created["artifact_id"]
    check_receipt, _ = begin_backup_operation(
        submission_id=str(uuid.uuid4()),
        kind="check",
        actor=actor,
        artifact_id=artifact_id,
        backup_root=root,
    )
    check_receipt = update_backup_operation(
        check_receipt,
        state="running",
        phase="temporary_restore",
        backup_root=root,
    )
    interrupted = get_backup_operation(
        submission_id=check_receipt["submission_id"],
        actor=actor,
        backup_root=root,
        force_reconcile=True,
    )
    assert interrupted["state"] == "interrupted"
    assert interrupted["retryable"] is True

    manifest_path = root / f"{artifact_id}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verification = verify_backup_manifest(manifest_path)
    evidence = artifact_version_evidence(
        root,
        artifact_id,
        manifest,
        checksum_sha256=verification["observed_checksum_sha256"],
        context=current_validation_context("sqlite"),
    )
    second_receipt, _ = begin_backup_operation(
        submission_id=str(uuid.uuid4()),
        kind="check",
        actor=actor,
        artifact_id=artifact_id,
        backup_root=root,
    )
    second_receipt = update_backup_operation(
        second_receipt,
        state="running",
        phase="persisting_result",
        backup_root=root,
    )
    write_artifact_state(
        root,
        artifact_id,
        {
            "integrity": {
                "status": "verified",
                "checked_at": "2026-07-29T10:00:00Z",
                "operation_id": second_receipt["operation_id"],
                "reason_code": None,
                "evidence": evidence,
            },
            "restore_validation": {
                "status": "passed",
                "validated_at": "2026-07-29T10:00:01Z",
                "operation_id": second_receipt["operation_id"],
                "reason_code": None,
                "evidence": evidence,
            },
            "last_check": {
                "operation_id": second_receipt["operation_id"],
                "outcome": "completed",
                "completed_at": "2026-07-29T10:00:01Z",
                "result": {
                    "status": "validated",
                    "integrity_status": "verified",
                    "compatibility_status": "compatible",
                    "restore_validation_status": "passed",
                },
            },
        },
    )
    completed = get_backup_operation(
        submission_id=second_receipt["submission_id"],
        actor=actor,
        backup_root=root,
        force_reconcile=True,
    )
    assert completed["state"] == "completed"
    assert completed["result"]["restore_validation_status"] == "passed"
    _close(engine, db)


def test_restart_cleanup_retries_only_the_exact_recorded_disposable_target(tmp_path, monkeypatch):
    root = configured_backup_root(tmp_path / "safe-db-backups")
    actor = _actor()
    artifact_id = "kmvms-db-20260729T100000Z-aaaaaaaaaaaa"
    receipt, _ = begin_backup_operation(
        submission_id=str(uuid.uuid4()),
        kind="check",
        actor=actor,
        artifact_id=artifact_id,
        backup_root=root,
    )
    receipt = update_backup_operation(
        receipt,
        state="running",
        phase="temporary_restore",
        backup_root=root,
    )
    exact_target = "kmvms_stage5_stage13_restore_validation_abcdef123456"
    receipt = record_backup_operation_disposable_target(
        receipt,
        database_name=exact_target,
        backup_root=root,
    )
    observed = []
    monkeypatch.setattr(
        backup_manager,
        "_cleanup_receipt_disposable_target",
        lambda current: observed.append(current["recovery"]["disposable_database_name"]) or False,
    )
    retrying = get_backup_operation(
        submission_id=receipt["submission_id"],
        actor=actor,
        backup_root=root,
        force_reconcile=True,
    )
    assert retrying["state"] == "running"
    assert retrying["phase"] == "cleanup_retry"
    assert observed == [exact_target]

    monkeypatch.setattr(
        backup_manager,
        "_cleanup_receipt_disposable_target",
        lambda current: observed.append(current["recovery"]["disposable_database_name"]) or True,
    )
    interrupted = get_backup_operation(
        submission_id=receipt["submission_id"],
        actor=actor,
        backup_root=root,
        force_reconcile=True,
    )
    assert interrupted["state"] == "interrupted"
    assert observed == [exact_target, exact_target]
    assert "recovery" not in safe_receipt(interrupted)


def test_conflicts_are_limited_to_create_and_same_artifact_mutations(tmp_path):
    root = configured_backup_root(tmp_path / "safe-db-backups")
    actor = _actor()
    first_create, _ = begin_backup_operation(
        submission_id=str(uuid.uuid4()),
        kind="create",
        actor=actor,
        planned_artifact_id="kmvms-db-20260729T100000Z-aaaaaaaaaaaa",
        backup_root=root,
    )
    update_backup_operation(first_create, state="running", phase="creating_backup", backup_root=root)
    with pytest.raises(BackupManagerBlocked) as create_conflict:
        begin_backup_operation(
            submission_id=str(uuid.uuid4()),
            kind="create",
            actor=actor,
            planned_artifact_id="kmvms-db-20260729T100001Z-bbbbbbbbbbbb",
            backup_root=root,
        )
    assert create_conflict.value.code == "operation_conflict"
    artifact_a = "kmvms-db-20260729T100002Z-cccccccccccc"
    artifact_b = "kmvms-db-20260729T100003Z-dddddddddddd"
    check, _ = begin_backup_operation(
        submission_id=str(uuid.uuid4()),
        kind="check",
        actor=actor,
        artifact_id=artifact_a,
        backup_root=root,
    )
    update_backup_operation(check, state="running", phase="integrity_preflight", backup_root=root)
    with pytest.raises(BackupManagerBlocked) as same_artifact:
        begin_backup_operation(
            submission_id=str(uuid.uuid4()),
            kind="delete",
            actor=actor,
            artifact_id=artifact_a,
            backup_root=root,
        )
    assert same_artifact.value.code == "operation_conflict"
    unrelated, _ = begin_backup_operation(
        submission_id=str(uuid.uuid4()),
        kind="delete",
        actor=actor,
        artifact_id=artifact_b,
        backup_root=root,
    )
    assert unrelated["state"] == "queued"


def test_observable_change_is_stale_and_explicit_sha_finds_silent_corruption(tmp_path):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    created = run_backup_create_operation(
        db,
        submission_id=str(uuid.uuid4()),
        actor=_actor(),
        backup_root=root,
    )
    artifact_id = created["artifact_id"]
    state_path = artifact_state_path(root, artifact_id)
    state_before = state_path.read_bytes()
    snapshot = build_backup_snapshot(backup_root=root, db_backend="sqlite")
    assert snapshot["items"][0]["integrity_status"] == "verified"
    dump_path = next(path for path in root.glob(f"{artifact_id}.*") if path.suffix in {".dump", ".sqlite3"})
    original_stat = dump_path.stat()
    original = dump_path.read_bytes()
    changed = bytes([original[0] ^ 0x01]) + original[1:]
    dump_path.write_bytes(changed)
    os.utime(dump_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    cheap_unchanged = build_backup_snapshot(backup_root=root, db_backend="sqlite")
    assert cheap_unchanged["items"][0]["integrity_status"] == "verified"
    assert verify_backup_manifest(root / f"{artifact_id}.manifest.json")["integrity_status"] == "failed"
    dump_path.write_bytes(original + b"x")
    stale = build_backup_snapshot(backup_root=root, db_backend="sqlite")
    assert stale["items"][0]["integrity_status"] == "stale_evidence"
    assert state_path.read_bytes() == state_before
    _close(engine, db)


def test_problem_artifact_check_stops_at_failed_preflight(tmp_path, monkeypatch):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    created = run_backup_create_operation(
        db,
        submission_id=str(uuid.uuid4()),
        actor=_actor(),
        backup_root=root,
    )
    artifact_id = created["artifact_id"]
    dump_path = next(path for path in root.glob(f"{artifact_id}.*") if path.suffix in {".dump", ".sqlite3"})
    dump_path.write_bytes(dump_path.read_bytes() + b"corruption")
    monkeypatch.setattr(
        restore_maintenance,
        "apply_restore_maintenance",
        lambda *args, **kwargs: pytest.fail("corrupt artifact must not be restored"),
    )
    receipt = run_backup_validation_operation(
        db,
        submission_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        confirm=True,
        actor=_actor(),
        backup_root=str(root),
    )
    assert receipt["state"] == "failed"
    assert receipt["result"]["integrity_status"] == "failed"
    assert receipt["result"]["restore_validation_status"] == "not_performed"
    state = json.loads(artifact_state_path(root, artifact_id).read_text(encoding="utf-8"))
    assert state["integrity"]["status"] == "failed"
    assert state["last_check"]["outcome"] == "failed"
    _close(engine, db)


def test_validation_result_is_persisted_before_receipt_completion(tmp_path, monkeypatch):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    created = run_backup_create_operation(
        db,
        submission_id=str(uuid.uuid4()),
        actor=_actor(),
        backup_root=root,
    )
    artifact_id = created["artifact_id"]
    monkeypatch.setattr(
        restore_maintenance,
        "_target_status",
        lambda *args, **kwargs: {
            "status": "safe",
            "reason": "test_disposable_target",
            "target_kind": "temporary_validation_db",
            "temporary_validation_restore_supported": True,
            "temporary_validation_target": "server_side_disposable_test",
            "requires_current_backup": False,
        },
    )
    monkeypatch.setattr(
        restore_maintenance,
        "apply_restore_maintenance",
        lambda *args, **kwargs: {
            "status": "restored",
            "post_restore_validation_status": True,
            "temporary_validation_cleanup": {"status": "completed"},
        },
    )
    receipt = run_backup_validation_operation(
        db,
        submission_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        confirm=True,
        actor=_actor(),
        backup_root=str(root),
    )
    assert receipt["state"] == "completed"
    assert receipt["result"]["restore_validation_status"] == "passed"
    state = json.loads(artifact_state_path(root, artifact_id).read_text(encoding="utf-8"))
    assert state["restore_validation"]["status"] == "passed"
    assert state["last_check"]["operation_id"] == receipt["operation_id"]
    _close(engine, db)


def test_validation_success_cleanup_failure_stays_active_until_exact_cleanup(tmp_path, monkeypatch):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    actor = _actor()
    created = run_backup_create_operation(
        db,
        submission_id=str(uuid.uuid4()),
        actor=actor,
        backup_root=root,
    )
    artifact_id = created["artifact_id"]
    exact_target = "kmvms_stage5_stage13_restore_validation_111111111111"
    monkeypatch.setattr(
        restore_maintenance,
        "_target_status",
        lambda *args, **kwargs: {
            "status": "safe",
            "reason": "test_disposable_target",
            "target_kind": "temporary_validation_db",
            "temporary_validation_restore_supported": True,
            "temporary_validation_target": "server_side_disposable_test",
            "requires_current_backup": False,
        },
    )

    def fake_apply(*args, **kwargs):
        kwargs["on_disposable_target_created"](exact_target)
        return {
            "status": "restored",
            "post_restore_validation_status": True,
            "temporary_validation_cleanup": {"status": "failed"},
        }

    monkeypatch.setattr(restore_maintenance, "apply_restore_maintenance", fake_apply)
    receipt = run_backup_validation_operation(
        db,
        submission_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        confirm=True,
        actor=actor,
        backup_root=str(root),
    )

    assert receipt["state"] == "running"
    assert receipt["phase"] == "cleanup_retry"
    assert receipt["finished_at"] is None
    assert receipt["result"]["restore_validation_status"] == "passed"
    assert receipt["reason_code"] == "temporary_validation_cleanup_failed"
    observed = []
    monkeypatch.setattr(
        backup_manager,
        "_cleanup_receipt_disposable_target",
        lambda current: observed.append(current["recovery"]["disposable_database_name"]) or True,
    )
    completed = get_backup_operation(
        submission_id=receipt["submission_id"],
        actor=actor,
        backup_root=root,
        force_reconcile=True,
    )
    assert completed["state"] == "completed"
    assert completed["result"]["restore_validation_status"] == "passed"
    assert completed["reason_code"] is None
    assert observed == [exact_target]
    internal = backup_manager._load_receipt(root, receipt["submission_id"])
    assert "disposable_database_name" not in internal.get("recovery", {})
    assert "recovery" not in safe_receipt(completed)
    _close(engine, db)


def test_validation_failure_cleanup_failure_preserves_failure_after_cleanup(tmp_path, monkeypatch):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    actor = _actor()
    created = run_backup_create_operation(
        db,
        submission_id=str(uuid.uuid4()),
        actor=actor,
        backup_root=root,
    )
    artifact_id = created["artifact_id"]
    exact_target = "kmvms_stage5_stage13_restore_validation_222222222222"
    monkeypatch.setattr(
        restore_maintenance,
        "_target_status",
        lambda *args, **kwargs: {
            "status": "safe",
            "reason": "test_disposable_target",
            "target_kind": "temporary_validation_db",
            "temporary_validation_restore_supported": True,
            "temporary_validation_target": "server_side_disposable_test",
            "requires_current_backup": False,
        },
    )

    def fake_apply(*args, **kwargs):
        kwargs["on_disposable_target_created"](exact_target)
        raise RestoreMaintenanceBlocked(
            "temporary_restore_failed",
            {"status": "failed", "reason": "temporary restore failed"},
        )

    monkeypatch.setattr(restore_maintenance, "apply_restore_maintenance", fake_apply)
    receipt = run_backup_validation_operation(
        db,
        submission_id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        confirm=True,
        actor=actor,
        backup_root=str(root),
    )

    assert receipt["state"] == "running"
    assert receipt["phase"] == "cleanup_retry"
    assert receipt["finished_at"] is None
    assert receipt["result"]["restore_validation_status"] == "failed"
    observed = []
    monkeypatch.setattr(
        backup_manager,
        "_cleanup_receipt_disposable_target",
        lambda current: observed.append(current["recovery"]["disposable_database_name"]) or True,
    )
    failed = get_backup_operation(
        submission_id=receipt["submission_id"],
        actor=actor,
        backup_root=root,
        force_reconcile=True,
    )
    assert failed["state"] == "failed"
    assert failed["result"]["restore_validation_status"] == "failed"
    assert failed["reason_code"] == "temporary_restore_failed"
    assert observed == [exact_target]
    internal = backup_manager._load_receipt(root, receipt["submission_id"])
    assert "disposable_database_name" not in internal.get("recovery", {})
    assert "recovery" not in safe_receipt(failed)
    _close(engine, db)


def test_postgres_restore_validation_normalizes_the_proven_passed_result(tmp_path, monkeypatch):
    manifest_path = tmp_path / "kmvms-db-20260729T100000Z-aaaaaaaaaaaa.manifest.json"
    monkeypatch.setattr(
        restore_maintenance,
        "run_restore_validation",
        lambda *args, **kwargs: {
            "status": "validated",
            "backup_restore_validated": True,
            "checks": {"schema": {"passed": True}},
        },
    )
    result = restore_maintenance._restore_artifact_to_target(
        manifest_path,
        {
            "db_backend": "postgresql",
            "backup_file_label": "kmvms-db-20260729T100000Z-aaaaaaaaaaaa.dump",
        },
        target_kind="temporary_validation_db",
        target_database_url="postgresql://internal/test",
        source_database_url="postgresql://internal/source",
    )
    assert result["status"] == "restored"
    assert result["post_restore_validation"]["backup_restore_validated"] is True
    assert result["post_restore_validation"]["passed"] is True


def test_partial_delete_preserves_manifest_and_retry_removes_exact_components(tmp_path, monkeypatch):
    engine, db, root, backup = _backup(tmp_path)
    manifest_path = Path(backup["manifest_path"])
    metadata_path = Path(backup["metadata_path"])
    original_unlink = Path.unlink

    def fail_metadata_once(path, *args, **kwargs):
        if path == metadata_path:
            raise OSError("simulated component failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_metadata_once)
    with pytest.raises(RestoreMaintenanceBlocked) as blocked:
        delete_backup_artifact(
            artifact_id=backup["backup_id"],
            confirm=True,
            backup_root=str(root),
            operation_id="backup-operation-test",
        )
    assert blocked.value.status == "delete_failed"
    assert blocked.value.diagnostics["delete_status"] == "partial_retryable"
    assert manifest_path.exists()
    assert metadata_path.exists()
    assert artifact_state_path(root, backup["backup_id"]).exists()
    monkeypatch.setattr(Path, "unlink", original_unlink)
    result = delete_backup_artifact(
        artifact_id=backup["backup_id"],
        confirm=True,
        backup_root=str(root),
    )
    assert result["deleted"] is True
    assert not manifest_path.exists()
    assert not artifact_state_path(root, backup["backup_id"]).exists()
    assert not list(root.glob(f"{backup['backup_id']}*"))
    _close(engine, db)


def test_delete_receipt_replays_without_tombstone_or_second_mutation(tmp_path):
    engine, db = _db(tmp_path)
    root = tmp_path / "safe-db-backups"
    actor = _actor()
    created = run_backup_create_operation(
        db,
        submission_id=str(uuid.uuid4()),
        actor=actor,
        backup_root=root,
    )
    submission_id = str(uuid.uuid4())
    first = run_backup_delete_operation(
        submission_id=submission_id,
        artifact_id=created["artifact_id"],
        confirm=True,
        actor=actor,
        backup_root=str(root),
    )
    replay = run_backup_delete_operation(
        submission_id=submission_id,
        artifact_id=created["artifact_id"],
        confirm=True,
        actor=actor,
        backup_root=str(root),
    )
    assert first["state"] == "completed"
    assert replay["replayed"] is True
    assert replay["operation_id"] == first["operation_id"]
    assert not artifact_state_path(root, created["artifact_id"]).exists()
    assert not list(root.glob(f"{created['artifact_id']}*"))
    assert safe_receipt(
        get_backup_operation(submission_id=submission_id, actor=actor, backup_root=root)
    )["result"]["status"] in {"deleted", "deleted_with_missing_files"}
    _close(engine, db)


def test_public_contract_rejects_source_root_and_registers_receipt_endpoint():
    submission_id = str(uuid.uuid4())
    accepted = BackupCreateRequest(confirm=True, submission_id=submission_id)
    assert accepted.submission_id == submission_id
    with pytest.raises(ValidationError):
        BackupCreateRequest(confirm=True, submission_id=submission_id, source="test")
    with pytest.raises(ValidationError):
        BackupCreateRequest(confirm=True, submission_id=submission_id, backup_root="/tmp")
    with pytest.raises(ValidationError):
        BackupCreateRequest(confirm=True, submission_id="not-a-uuid")
    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("POST", "/system/restore/artifacts/{artifact_id}/delete", "manage_settings") in rows
    assert ("GET", "/system/backup/operations/{submission_id}", "manage_settings") in rows
