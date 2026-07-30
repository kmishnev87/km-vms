from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services.backup_before_upgrade import (
    BackupExecutionConfig,
    create_backup_before_upgrade,
    verify_backup_manifest,
)
from app.services.backup_manager import (
    actor_binding_key,
    artifact_version_evidence,
    current_restore_artifact_evidence,
    current_validation_context,
    write_artifact_state,
)
from app.services.current_db_restore import (
    RESTORE_CONFIRMATION_PHRASE,
    RESTORE_REQUEST_SCHEMA,
    restore_public_contract,
    utc_iso,
)
from app.services.maintenance_admission import (
    write_bounded_json_atomic,
)
from app.services.restore_validation import (
    DISPOSABLE_DB_PREFIX,
    RestoreValidationConfig,
    backup_restore_validated,
    run_restore_validation,
)
from test_restore_validation_stage5 import _seed_representative_source


POSTGRES_URL = os.getenv("KMVMS_STAGE1378_POSTGRES_URL")
BACKUP_ROOT = Path(
    os.getenv("KMVMS_STAGE1378_BACKUP_ROOT") or "/stage1378-backups"
)
RUNTIME_ROOT = Path("/stage1378-runtime")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL
    or not shutil.which("pg_dump")
    or not shutil.which("pg_restore"),
    reason=(
        "Stage 13.7.8 requires the isolated PostgreSQL Compose service "
        "and PostgreSQL client tools"
    ),
)

HELPER_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "km-vms-update-helper.py"
)
HELPER_SPEC = importlib.util.spec_from_file_location(
    "stage1378_disposable_restore_helper",
    HELPER_PATH,
)
assert HELPER_SPEC and HELPER_SPEC.loader
helper = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(helper)


def _url_string(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _db_url(name: str) -> URL:
    return make_url(str(POSTGRES_URL)).set(database=name)


def _admin_url() -> URL:
    return make_url(str(POSTGRES_URL))


def _create_database(name: str) -> None:
    engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


def _drop_database(name: str) -> None:
    engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname=:name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            )
    finally:
        engine.dispose()


def _seed_database(
    url: URL,
    *,
    owner_password: str,
    sentinel: str,
) -> SimpleNamespace:
    engine = create_engine(url)
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )
    db = SessionLocal()
    try:
        Base.metadata.create_all(bind=engine)
        _seed_representative_source(db, owner_password)
        db.execute(text("UPDATE cameras SET enabled=false"))
        system = db.query(SystemSettings).first()
        assert system is not None
        system.system_name = sentinel
        owner = db.query(User).filter(User.username == "stage5_owner").one()
        db.commit()
        return SimpleNamespace(
            id=owner.id,
            username=owner.username,
            role=owner.role,
        )
    finally:
        db.close()
        engine.dispose()


def _database_sentinel(url: URL) -> str:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    text("SELECT system_name FROM system_settings LIMIT 1")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _start_disposable_recorder(
    *,
    current_url: URL,
    storage_root: Path,
) -> subprocess.Popen:
    recorder_root = (
        Path(__file__).resolve().parents[2] / "recorder"
    )
    storage_root.mkdir(parents=True, exist_ok=True)
    child_env = os.environ.copy()
    child_env.update(
        {
            "DATABASE_URL": _url_string(current_url),
            "STORAGE_ROOT": str(storage_root),
            "RECORDER_INSTANCE_ID": (
                f"stage1378-{uuid.uuid4().hex[:16]}"
            ),
            "FFMPEG_LOGLEVEL": "error",
        }
    )
    return subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=recorder_root,
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_disposable_recorder(
    process: subprocess.Popen | None,
) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _create_source_artifact(
    source_url: URL,
    *,
    actor: SimpleNamespace,
    owner_password: str,
    backup_root: Path,
    validation_url: URL,
    validation_root: Path,
) -> tuple[str, dict]:
    engine = create_engine(source_url)
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )
    db = SessionLocal()
    try:
        backup = create_backup_before_upgrade(
            db,
            config=BackupExecutionConfig(
                backup_root=backup_root,
                source="stage1378_disposable_source",
            ),
        )
    finally:
        db.close()
        engine.dispose()
    validation = run_restore_validation(
        backup["manifest_path"],
        config=RestoreValidationConfig(
            target_database_url=_url_string(validation_url),
            validation_root=validation_root,
            allow_disposable_target=True,
            expected_owner_username=actor.username,
            expected_owner_password=owner_password,
        ),
        source_database_url=_url_string(source_url),
    )
    assert backup_restore_validated(
        validation["restore_manifest_path"],
        backup["manifest_path"],
    )["valid"] is True
    manifest_path = Path(backup["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verification = verify_backup_manifest(manifest_path)
    assert verification["valid"] is True
    evidence = artifact_version_evidence(
        backup_root,
        backup["backup_id"],
        manifest,
        checksum_sha256=str(
            verification.get("observed_checksum_sha256")
            or verification.get("checksum_sha256")
            or ""
        ),
        context=current_validation_context("postgresql"),
    )
    checked_at = utc_iso()
    write_artifact_state(
        backup_root,
        backup["backup_id"],
        {
            "integrity": {
                "status": "verified",
                "checked_at": checked_at,
                "operation_id": "stage1378-integration-validation",
                "reason_code": None,
                "evidence": evidence,
            },
            "restore_validation": {
                "status": "passed",
                "validated_at": checked_at,
                "operation_id": "stage1378-integration-validation",
                "reason_code": None,
                "evidence": evidence,
                "actor_key": actor_binding_key(actor),
                "actor_subject": actor.username,
                "actor_role": actor.role,
            },
            "delete_status": "allowed",
        },
    )
    return backup["backup_id"], backup


def _bind_helper_paths(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_root: Path,
) -> tuple[Path, Path, Path]:
    restore_control = run_root / "restore-control"
    restore_public = run_root / "restore-public"
    maintenance_control = run_root / "maintenance-control"
    for directory in (
        restore_control,
        restore_public,
        maintenance_control,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(helper, "RESTORE_CONTROL_DIR", restore_control)
    monkeypatch.setattr(helper, "RESTORE_PUBLIC_DIR", restore_public)
    monkeypatch.setattr(
        helper,
        "MAINTENANCE_CONTROL_DIR",
        maintenance_control,
    )
    monkeypatch.setattr(
        helper,
        "ADMISSION_LOCK_FILE",
        maintenance_control / "maintenance-admission.lock",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_REQUEST_FILE",
        restore_control / "restore-request.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_PUBLIC_STATUS_FILE",
        restore_public / "restore-status.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_HELPER_HEALTH_FILE",
        restore_public / "helper-health.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_JOURNAL_FILE",
        restore_control / "restore-journal.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_JOURNAL_DIR",
        restore_control / "journal",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_EXECUTOR_RESULT_FILE",
        restore_control / "restore-executor-result.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_DESTRUCTIVE_MARKER_FILE",
        restore_control / "restore-destructive-started.json",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_HELPER_LEASE_FILE",
        restore_control / "restore-helper-claim.lock",
    )
    monkeypatch.setattr(
        helper,
        "RESTORE_RECEIPT_DIR",
        restore_control / "receipts",
    )
    return restore_control, restore_public, maintenance_control


def _restore_request(
    *,
    artifact_id: str,
    actor: SimpleNamespace,
    backup_root: Path,
) -> dict:
    evidence = current_restore_artifact_evidence(
        artifact_id,
        actor=actor,
        db_backend="postgresql",
        backup_root=backup_root,
    )
    assert evidence["integrity_verified"] is True
    assert evidence["temporary_restore_validated"] is True
    assert evidence["actor_access_verified"] is True
    now = utc_iso()
    return {
        "schema": RESTORE_REQUEST_SCHEMA,
        "operation_id": f"restore-{uuid.uuid4().hex}",
        "submission_id": str(uuid.uuid4()),
        "intent": "restore_current_database",
        "requested_at": now,
        "updated_at": now,
        "requested_by": {
            "user_id": actor.id,
            "subject": actor.username,
            "role": actor.role,
            "binding": actor_binding_key(actor),
        },
        "artifact": {
            "artifact_id": evidence["artifact_id"],
            "artifact_created_at": evidence["artifact_created_at"],
            "artifact_schema_version": evidence[
                "artifact_schema_version"
            ],
            "db_backend": evidence["db_backend"],
            "file_size": evidence["file_size"],
            "fingerprint": evidence["fingerprint"],
        },
        "confirmed": True,
        "confirmation_phrase": RESTORE_CONFIRMATION_PHRASE,
        "state": "claimed",
        "claimed_at": now,
        "terminal": None,
        "video_archive_scope": "excluded",
        "migration_auto_apply": False,
    }


def _run_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_url: URL,
    actor: SimpleNamespace,
    artifact_id: str,
    backup_root: Path,
    run_root: Path,
    failure_mode: str | None,
) -> tuple[dict, list[str]]:
    restore_control, restore_public, maintenance_control = (
        _bind_helper_paths(monkeypatch, run_root=run_root)
    )
    update_control = run_root / "update-control"
    update_control.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "database_url", _url_string(current_url))
    monkeypatch.setattr(
        settings,
        "kmvms_db_backup_root",
        str(backup_root),
    )
    monkeypatch.setattr(
        settings,
        "update_control_root",
        str(update_control),
    )
    monkeypatch.setattr(
        settings,
        "restore_control_root",
        str(restore_control),
    )
    monkeypatch.setattr(
        settings,
        "restore_public_root",
        str(restore_public),
    )
    monkeypatch.setattr(
        settings,
        "maintenance_control_root",
        str(maintenance_control),
    )
    monkeypatch.setenv("KMVMS_DB_BACKUP_ROOT", str(backup_root))
    request = _restore_request(
        artifact_id=artifact_id,
        actor=actor,
        backup_root=backup_root,
    )
    write_bounded_json_atomic(
        restore_control / "restore-request.json",
        request,
    )
    events: list[str] = []
    api_root = Path(__file__).resolve().parents[1]
    recorder_process: subprocess.Popen | None = None

    def compose(*arguments: str, **_kwargs):
        nonlocal recorder_process
        if arguments[0] == "stop":
            _stop_disposable_recorder(recorder_process)
            recorder_process = None
            events.append("writers:stop")
            return subprocess.CompletedProcess(
                list(arguments),
                0,
                "",
                "",
            )
        if arguments[0] == "start":
            events.append(f"service:start:{arguments[1]}")
            if arguments[1] == "recorder":
                recorder_process = _start_disposable_recorder(
                    current_url=current_url,
                    storage_root=run_root / "recorder-storage",
                )
            return subprocess.CompletedProcess(
                list(arguments),
                0,
                "",
                "",
            )
        assert arguments[:4] == (
            "run",
            "--rm",
            "--no-deps",
            "restore-executor",
        )
        cli = list(arguments[4:])
        action = cli[0]
        mode = (
            cli[cli.index("--mode") + 1]
            if "--mode" in cli
            else ""
        )
        events.append(
            f"executor:{action}" + (f":{mode}" if mode else "")
        )
        child_env = os.environ.copy()
        child_env.update(
            {
                "APP_ENV": "test",
                "DATABASE_URL": _url_string(current_url),
                "KMVMS_DB_BACKUP_ROOT": str(backup_root),
                "UPDATE_CONTROL_ROOT": str(update_control),
                "RESTORE_CONTROL_ROOT": str(restore_control),
                "RESTORE_PUBLIC_ROOT": str(restore_public),
                "MAINTENANCE_CONTROL_ROOT": str(maintenance_control),
                "KMVMS_TEST_FAULT_INJECTION": "0",
                "KMVMS_RESTORE_TEST_FAILURE_PHASE": "",
            }
        )
        if (
            action == "restore"
            and mode == "source"
            and failure_mode in {"source", "rollback"}
        ):
            child_env["KMVMS_TEST_FAULT_INJECTION"] = "1"
            child_env[
                "KMVMS_RESTORE_TEST_FAILURE_PHASE"
            ] = "after_database_reset"
        if (
            action == "restore"
            and mode == "rollback"
            and failure_mode == "rollback"
        ):
            child_env["KMVMS_TEST_FAULT_INJECTION"] = "1"
            child_env[
                "KMVMS_RESTORE_TEST_FAILURE_PHASE"
            ] = "after_rollback_database_reset"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.services.current_db_restore_executor",
                *cli,
            ],
            cwd=api_root,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        result_payload = json.loads(
            (restore_control / "restore-executor-result.json").read_text(
                encoding="utf-8"
            )
        )
        events.append(
            "executor-result:"
            f"{action}:{mode or 'none'}:{result.returncode}:"
            f"{result_payload.get('reason_code') or 'ok'}"
        )
        return result

    monkeypatch.setattr(helper, "restore_compose_command", compose)
    monkeypatch.setattr(
        helper,
        "wait_for_service",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        helper,
        "wait_for_restore_writers_stopped",
        lambda **_kwargs: True,
    )

    try:
        helper.run_current_restore(request)
    finally:
        _stop_disposable_recorder(recorder_process)

    status = helper.read_json(
        restore_public / "restore-status.json"
    )
    assert status is not None
    assert restore_public_contract(status) == status
    return status, events


def test_disposable_current_restore_happy_rollback_and_recovery_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    backup_root = BACKUP_ROOT / suffix
    runtime_root = RUNTIME_ROOT / suffix
    source_name = f"kmvms_stage1378_source_{suffix}"
    validation_name = f"{DISPOSABLE_DB_PREFIX}stage1378_{suffix}"
    current_names = {
        "happy": f"kmvms_stage1378_happy_{suffix}",
        "rollback": f"kmvms_stage1378_rollback_{suffix}",
        "recovery": f"kmvms_stage1378_recovery_{suffix}",
    }
    databases = [
        source_name,
        validation_name,
        *current_names.values(),
    ]
    video_sentinel = runtime_root / "video-archive-sentinel.txt"
    owner_password = f"stage1378-{uuid.uuid4().hex}"
    for name in databases:
        _create_database(name)
    try:
        source_actor = _seed_database(
            _db_url(source_name),
            owner_password=owner_password,
            sentinel="SENTINEL-B",
        )
        for name in current_names.values():
            actor = _seed_database(
                _db_url(name),
                owner_password=owner_password,
                sentinel="SENTINEL-A",
            )
            assert actor.username == source_actor.username
            assert actor.role == source_actor.role
            assert actor.id == source_actor.id
        runtime_root.mkdir(parents=True, exist_ok=True)
        video_sentinel.write_text(
            "stage1378-video-archive-must-not-change",
            encoding="utf-8",
        )
        artifact_id, _backup = _create_source_artifact(
            _db_url(source_name),
            actor=source_actor,
            owner_password=owner_password,
            backup_root=backup_root,
            validation_url=_db_url(validation_name),
            validation_root=runtime_root / "validation",
        )

        happy, happy_events = _run_flow(
            monkeypatch,
            current_url=_db_url(current_names["happy"]),
            actor=source_actor,
            artifact_id=artifact_id,
            backup_root=backup_root,
            run_root=runtime_root / "happy",
            failure_mode=None,
        )
        assert happy["terminal_result"] == "completed", (
            happy.get("reason_code"),
            happy.get("next_action"),
            [
                event
                for event in happy_events
                if event.startswith("executor-result:")
            ],
        )
        assert _database_sentinel(
            _db_url(current_names["happy"])
        ) == "SENTINEL-B"
        assert happy_events.index("writers:stop") < happy_events.index(
            "executor:pre-restore-backup"
        )
        assert happy_events.index(
            "executor:post-check"
        ) < happy_events.index("service:start:recorder")
        assert happy_events.index(
            "service:start:recorder"
        ) < happy_events.index("executor:recorder-proof")

        rolled_back, rollback_events = _run_flow(
            monkeypatch,
            current_url=_db_url(current_names["rollback"]),
            actor=source_actor,
            artifact_id=artifact_id,
            backup_root=backup_root,
            run_root=runtime_root / "rollback",
            failure_mode="source",
        )
        assert rolled_back["terminal_result"] == "failed_rolled_back"
        assert rolled_back["next_action"] == "current_database_restored"
        assert _database_sentinel(
            _db_url(current_names["rollback"])
        ) == "SENTINEL-A"
        assert rollback_events.count("executor:restore:source") == 1
        assert rollback_events.count("executor:restore:rollback") == 1
        assert rollback_events.count("executor:recorder-proof") == 1

        recovery, recovery_events = _run_flow(
            monkeypatch,
            current_url=_db_url(current_names["recovery"]),
            actor=source_actor,
            artifact_id=artifact_id,
            backup_root=backup_root,
            run_root=runtime_root / "recovery",
            failure_mode="rollback",
        )
        assert (
            recovery["terminal_result"]
            == "failed_recovery_required"
        )
        assert recovery["next_action"] == "contact_support"
        assert recovery_events.count("executor:restore:source") == 1
        assert recovery_events.count("executor:restore:rollback") == 1

        assert video_sentinel.read_text(
            encoding="utf-8"
        ) == "stage1378-video-archive-must-not-change"
        assert (backup_root / f"{artifact_id}.manifest.json").is_file()
        for status in (happy, rolled_back, recovery):
            pre_restore_id = status.get("pre_restore_backup_id")
            assert isinstance(pre_restore_id, str)
            assert (
                backup_root / f"{pre_restore_id}.manifest.json"
            ).is_file()
    finally:
        for name in reversed(databases):
            _drop_database(name)
        shutil.rmtree(runtime_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)
