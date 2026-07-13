import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.db.session import Base
from app.models.camera import Camera
from app.models.system_settings import SystemSettings
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.routers.settings import system_backup_plan, system_schema_plan, system_schema_status
from app.services.backup_before_upgrade import (
    BackupExecutionConfig,
    BackupSafetyBlocked,
    DEFAULT_BACKUP_ROOT,
    RESTORE_VALIDATION_STATUS,
    backup_precondition_status,
    build_backup_metadata_snapshot,
    build_backup_plan,
    create_backup_before_upgrade,
    verify_backup_manifest,
)
from app.services.schema_migrations import (
    RISK_MANUAL_ONLY,
    RISK_METADATA_ONLY,
    RISK_REQUIRES_BACKUP,
    MIGRATION_SOURCE,
    MigrationDefinition,
    MigrationRegistry,
    SchemaMigrationBlocked,
    build_migration_plan,
    execute_migration_plan,
)
from app.services.schema_versioning import CURRENT_BASELINE_ID, CURRENT_SCHEMA_VERSION, CURRENT_STATE_ID
from test_schema_migration_runner_stage3 import ok, seed_state


POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL") or os.getenv("KMVMS_STAGE3_POSTGRES_URL")


def sqlite_session(tmp_path):
    db_path = tmp_path / "stage4.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def stage4_migration(risk=RISK_REQUIRES_BACKUP, migration_id="stage4_test_requires_backup"):
    return MigrationDefinition(
        migration_id=migration_id,
        from_version=0,
        to_version=1,
        description="Stage 4 test-only migration.",
        risk=risk,
        transaction_mode="session_transaction",
        preflight=ok,
        apply=ok,
        verify=ok,
        safe_failure_summary="stage4 test migration failed safely",
        rollback_note="restore validation is deferred to Stage 5",
    )


def backup_config(tmp_path, **kwargs):
    return BackupExecutionConfig(backup_root=tmp_path / "safe-db-backups", allow_tmp_for_tests=True, source="test", **kwargs)


def test_backup_plan_is_read_only_and_creates_no_files_or_metadata_rows(tmp_path):
    engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    root = tmp_path / "safe-db-backups"

    plan = build_backup_plan(db, config=backup_config(tmp_path))

    assert plan["status"] == "planned"
    assert plan["creates_backup_files"] is False
    assert not root.exists()
    assert inspect(engine).has_table("schema_version_state")
    assert db.query(SchemaMigrationHistory).count() == 1


def test_sqlite_backup_creates_manifest_metadata_and_verifies(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)

    result = create_backup_before_upgrade(db, config=backup_config(tmp_path))
    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))

    assert Path(result["backup_file_path"]).exists()
    assert manifest["status"] == "verified"
    assert manifest["file_size"] > 0
    assert manifest["restore_validation_status"] == RESTORE_VALIDATION_STATUS
    assert manifest["video_archive_files_included"] is False
    assert verify_backup_manifest(manifest_path)["valid"] is True
    assert metadata["backup_requires_restore_validation"] is True
    assert metadata["restore_validation_status"] == RESTORE_VALIDATION_STATUS


def test_metadata_snapshot_is_secret_safe_and_uses_counts_not_secret_fields(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)

    payload = build_backup_metadata_snapshot(
        db,
        backup_id="stage4-test",
        db_backend="sqlite",
        source="test",
        backup_checksum="abc",
        migration_plan_summary={"status": "current"},
    )
    raw = json.dumps(payload, ensure_ascii=True)

    assert payload["entity_counts"]["users"] == 0
    assert "password_hash" not in raw
    assert "rtsp://" not in raw
    assert "storage_path_redacted" in raw


def test_backup_metadata_uses_actual_pre_migration_schema_with_newer_orm_models(tmp_path):
    engine, db = sqlite_session(tmp_path)
    seed_state(db, version=3)
    db.add(SystemSettings(recording_format="mp4", auto_free_space_cleanup_enabled=True))
    db.add(Camera(name="Pre-migration camera", storage_folder_name="pre-migration", protocol="rtsp", host="127.0.0.1", port=554))
    db.commit()

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE cameras DROP COLUMN retention_policy_version"))
        connection.execute(text("ALTER TABLE system_settings DROP COLUMN auto_free_space_acknowledged_terms_version"))
        connection.execute(text("ALTER TABLE system_settings DROP COLUMN auto_free_space_acknowledged_at"))
        connection.execute(text("ALTER TABLE system_settings DROP COLUMN auto_free_space_acknowledged_by_user_id"))
        connection.execute(text("ALTER TABLE system_settings DROP COLUMN recording_suspended_by_low_disk"))
        connection.execute(text("ALTER TABLE system_settings DROP COLUMN low_disk_suspended_physical_volume_id"))
        connection.execute(text("ALTER TABLE system_settings DROP COLUMN low_disk_suspended_at"))

    payload = build_backup_metadata_snapshot(
        db,
        backup_id="pre-migration-test",
        db_backend="sqlite",
        source="test",
        backup_checksum="abc",
    )
    result = create_backup_before_upgrade(db, config=backup_config(tmp_path))

    assert payload["entity_counts"]["cameras"] == 1
    assert payload["storage_settings_summary"]["recording_format"] == "mp4"
    assert payload["storage_settings_summary"]["auto_free_space_cleanup_enabled"] is True
    assert verify_backup_manifest(result["manifest_path"])["valid"] is True


def test_backup_metadata_failure_removes_completed_dump_and_sidecars(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    root = tmp_path / "safe-db-backups"
    monkeypatch.setattr(
        "app.services.backup_before_upgrade.build_backup_metadata_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("metadata failed")),
    )

    with pytest.raises(BackupSafetyBlocked) as exc:
        create_backup_before_upgrade(db, config=backup_config(tmp_path))

    assert exc.value.status == "backup_failed"
    assert list(root.iterdir()) == []


def test_unsafe_backup_locations_and_tmp_final_destination_are_rejected(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)

    with pytest.raises(BackupSafetyBlocked) as tmp_block:
        build_backup_plan(db, config=BackupExecutionConfig(backup_root=Path("/tmp/kmvms-stage4"), source="test"))
    assert tmp_block.value.status == "unsafe_backup_location"

    with pytest.raises(BackupSafetyBlocked) as git_block:
        build_backup_plan(db, config=BackupExecutionConfig(backup_root=Path.cwd() / "backups", source="test"))
    assert git_block.value.status == "unsafe_backup_location"

    with pytest.raises(BackupSafetyBlocked) as legacy_block:
        build_backup_plan(db, config=BackupExecutionConfig(backup_root=Path("/var/lib/km-vms/backups/db"), source="test"))
    assert legacy_block.value.status == "container_only_root_blocked"

    with pytest.raises(BackupSafetyBlocked) as archive_block:
        build_backup_plan(db, config=BackupExecutionConfig(backup_root=Path("/storage/archive"), source="test"))
    assert archive_block.value.status == "backup_root_inside_archive_root"

    with pytest.raises(BackupSafetyBlocked) as archive_child_block:
        build_backup_plan(db, config=BackupExecutionConfig(backup_root=Path("/storage/archive/db-backups"), source="test"))
    assert archive_child_block.value.status == "backup_root_inside_archive_root"


def test_default_backup_root_is_persistent_container_mount_contract(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    monkeypatch.delenv("KMVMS_DB_BACKUP_ROOT", raising=False)

    plan = build_backup_plan(db)

    assert DEFAULT_BACKUP_ROOT == "/storage/backups/db"
    assert plan["backup_root_status"] == "ready"
    assert plan["backup_root_classification"] == "configured_persistent_root"
    assert plan["backup_root_persistent"] is True
    assert plan["backup_root_archive_scope"] == "outside_archive_root_and_retention_scope"


def test_backup_create_rejects_unsafe_root_before_invoking_pg_dump(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("app.services.backup_before_upgrade.detect_db_backend", lambda _db: "postgresql")
    monkeypatch.setattr("app.services.backup_before_upgrade.subprocess.run", fake_run)

    with pytest.raises(BackupSafetyBlocked):
        create_backup_before_upgrade(db, config=BackupExecutionConfig(backup_root=Path("/storage/archive/db-backups"), source="test"))

    assert called is False


def test_compose_env_install_and_gitignore_keep_backup_root_persistent_and_outside_archive_scope():
    root = Path(__file__).resolve().parents[3]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    install = (root / "scripts/install.sh").read_text(encoding="utf-8")
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")

    assert "postgresql-client" in (root / "apps/api/Dockerfile").read_text(encoding="utf-8")
    assert "KMVMS_DB_BACKUP_ROOT: ${KMVMS_DB_BACKUP_ROOT:-/storage/backups/db}" in compose
    assert "${KMVMS_HOST_DB_BACKUP_ROOT:-./data/backups/db}:${KMVMS_DB_BACKUP_ROOT:-/storage/backups/db}" in compose
    assert "KMVMS_HOST_DB_BACKUP_ROOT=./data/backups/db" in env_example
    assert "KMVMS_DB_BACKUP_ROOT=/storage/backups/db" in env_example
    assert "backup_dir=\"$APP_DIR/data/backups/db\"" in install
    assert "chmod 700 \"$backup_dir\"" in install
    assert "data/" in gitignore


def test_free_space_failure_blocks_without_partial_artifact(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    root = tmp_path / "safe-db-backups"

    with pytest.raises(BackupSafetyBlocked) as exc:
        create_backup_before_upgrade(db, config=backup_config(tmp_path, min_required_bytes=10**30))

    assert exc.value.status == "insufficient_free_space"
    assert not list(root.glob("*.tmp"))
    assert not list(root.glob("*.sqlite3"))


def test_pg_dump_failure_is_sanitized_and_no_partial_file_remains(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    monkeypatch.setattr("app.services.backup_before_upgrade.detect_db_backend", lambda _db: "postgresql")
    monkeypatch.setattr(
        "app.services.backup_before_upgrade.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="password=secret token=jwt rtsp://user:pass@camera failed", stdout=""),
    )

    with pytest.raises(BackupSafetyBlocked) as exc:
        create_backup_before_upgrade(db, config=backup_config(tmp_path))

    assert exc.value.status == "backup_failed"
    assert "secret" not in exc.value.diagnostics["summary"]
    assert "jwt" not in exc.value.diagnostics["summary"]
    assert not list((tmp_path / "safe-db-backups").glob("*.tmp"))


def test_backup_required_gate_blocks_without_manifest_and_allows_with_valid_manifest_without_auto_execution(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=0)
    registry = MigrationRegistry([stage4_migration()])

    blocked = build_migration_plan(db, registry=registry)
    assert blocked["blocked_reason"] == "backup_required"

    backup = create_backup_before_upgrade(db, config=backup_config(tmp_path), migration_plan_summary=blocked)
    ready = build_migration_plan(db, registry=registry, backup_manifest_path=backup["manifest_path"])

    assert ready["status"] == "ready"
    assert ready["backup_before_upgrade"]["status"] == "satisfied"
    assert db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == 0


def test_execute_backup_required_requires_valid_manifest_and_does_not_make_manual_only_automatic(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=0)
    with pytest.raises(SchemaMigrationBlocked):
        execute_migration_plan(db, registry=MigrationRegistry([stage4_migration()]))

    backup = create_backup_before_upgrade(db, config=backup_config(tmp_path))
    result = execute_migration_plan(db, registry=MigrationRegistry([stage4_migration()]), backup_manifest_path=backup["manifest_path"])
    assert result["executed_migrations"] == ["stage4_test_requires_backup"]

    db.query(SchemaMigrationHistory).delete()
    db.query(SchemaVersionState).delete()
    db.commit()
    seed_state(db, version=0)
    manual_plan = build_migration_plan(
        db,
        registry=MigrationRegistry([stage4_migration(risk=RISK_MANUAL_ONLY, migration_id="stage4_test_manual_only")]),
        backup_manifest_path=backup["manifest_path"],
    )
    assert manual_plan["blocked_reason"] == "manual_authorization_required"


def test_invalid_and_stale_manifest_block_precondition(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    backup = create_backup_before_upgrade(db, config=backup_config(tmp_path))
    manifest_path = Path(backup["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = (datetime.utcnow() - timedelta(days=3)).isoformat() + "Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    stale = backup_precondition_status(manifest_path=manifest_path, required=True, max_age_minutes=1)

    assert stale["status"] == "blocked"


def test_system_schema_and_backup_endpoints_are_protected_and_read_only(tmp_path):
    engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    before = db.query(SchemaMigrationHistory).count()

    assert system_schema_status(db=db, current_user=object())["status"] == "current"
    assert system_schema_plan(db=db, current_user=object())["mutates_database"] is False
    assert system_backup_plan(db=db, current_user=object())["creates_backup_files"] is False
    assert db.query(SchemaMigrationHistory).count() == before
    assert inspect(engine).has_table("schema_version_state")

    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("GET", "/system/backup/plan", "manage_settings") in rows
    assert ("POST", "/system/backup/create", "manage_settings") in rows
    assert ("GET", "/system/schema/status", "manage_settings") in rows
    assert ("GET", "/system/schema/plan", "manage_settings") in rows


def test_recorder_remains_outside_backup_and_migration_ownership(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)

    plan = build_migration_plan(db)
    backup_plan = build_backup_plan(db, config=backup_config(tmp_path), migration_plan_summary=plan)

    assert plan["recorder_metadata_owner"] == "api_bootstrap_only"
    assert backup_plan["mutates_database"] is False


@pytest.mark.skipif(not POSTGRES_URL or not shutil.which("pg_dump"), reason="Disposable PostgreSQL URL and pg_dump are required")
def test_postgres_disposable_backup_create_verify(tmp_path):
    schema = f"stage4_{uuid.uuid4().hex}"
    engine = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_engine(POSTGRES_URL, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=scoped_engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        Base.metadata.create_all(bind=scoped_engine)
        seed_state(db, version=CURRENT_SCHEMA_VERSION)
        result = create_backup_before_upgrade(db, config=backup_config(tmp_path))
        assert result["db_backend"] == "postgresql"
        assert verify_backup_manifest(result["manifest_path"])["valid"] is True
    finally:
        db.close()
        scoped_engine.dispose()
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
