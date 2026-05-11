import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER
from app.core.security import create_access_token
from app.db.session import Base, get_db
from app.main import app
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.models.user import User
from app.services.backup_before_upgrade import BackupSafetyBlocked
from app.services.migration_maintenance import (
    apply_migration_maintenance,
    assert_migration_report_secret_safe,
    dry_run_migration_maintenance,
    inspect_migration_maintenance,
)
from app.services.schema_migrations import RISK_ADDITIVE_SAFE, RISK_METADATA_ONLY, MigrationDefinition, MigrationRegistry
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from app.services.upgrade_report import build_upgrade_report
from test_schema_migration_runner_stage3 import ok, seed_state


def sqlite_session(tmp_path, *, seed_version=CURRENT_SCHEMA_VERSION):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "stage13_migration_apply.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    seed_state(db, version=seed_version)
    return engine, db


def migration(
    *,
    migration_id="stage13_test_migration_apply",
    from_version=0,
    to_version=CURRENT_SCHEMA_VERSION,
    risk=RISK_METADATA_ONLY,
    apply_fn=ok,
):
    return MigrationDefinition(
        migration_id=migration_id,
        from_version=from_version,
        to_version=to_version,
        description="Stage 13 test-only migration apply.",
        risk=risk,
        transaction_mode="session_transaction",
        preflight=ok,
        apply=apply_fn,
        verify=ok,
        safe_failure_summary="stage13 test migration failed safely",
        rollback_note="Restore/rollback is a separate operator flow.",
    )


def registry_with(*migrations):
    return MigrationRegistry(list(migrations))


def table_counts(db, names):
    result = {}
    for name in names:
        result[name] = int(db.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)
    return result


def add_user(db, *, role=ROLE_ADMIN, username=None):
    user = User(
        username=username or f"stage13_migration_{role}",
        full_name=f"stage13 migration {role}",
        password_hash="hash",
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.username)}"}


@pytest.fixture
def client_db(tmp_path):
    engine, db = sqlite_session(tmp_path, seed_version=CURRENT_SCHEMA_VERSION)
    owner = add_user(db, role=ROLE_OWNER, username="stage13_migration_owner")
    operator = add_user(db, role=ROLE_OPERATOR, username="stage13_migration_operator")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), db, owner, operator
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_current_schema_status_and_dry_run_are_read_only(tmp_path):
    engine, db = sqlite_session(tmp_path, seed_version=CURRENT_SCHEMA_VERSION)
    before_history = db.query(SchemaMigrationHistory).count()
    before_tables = set(inspect(engine).get_table_names())

    status = inspect_migration_maintenance(db)
    dry_run = dry_run_migration_maintenance(db)

    assert status["status"] == "current"
    assert status["pending_count"] == 0
    assert status["can_apply"] is False
    assert dry_run["dry_run"] is True
    assert dry_run["mutates_database"] is False
    assert dry_run["creates_backup_files"] is False
    assert db.query(SchemaMigrationHistory).count() == before_history
    assert set(inspect(engine).get_table_names()) == before_tables


def test_pending_ready_migrations_are_ordered_sanitized_and_backup_plan_read_only(tmp_path):
    engine, db = sqlite_session(tmp_path, seed_version=0)
    reg = registry_with(
        migration(migration_id="stage13_test_002", from_version=1, to_version=2),
        migration(migration_id="stage13_test_001", from_version=0, to_version=1),
    )
    before_history = db.query(SchemaMigrationHistory).count()
    root = tmp_path / "safe-db-backups"

    payload = dry_run_migration_maintenance(db, registry=reg)
    raw = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "pending"
    assert payload["can_apply"] is True
    assert payload["backup_required"] is True
    assert [item["migration_id"] for item in payload["pending_migrations"]] == ["stage13_test_001", "stage13_test_002"]
    assert [item["order"] for item in payload["pending_migrations"]] == [1, 2]
    assert "SELECT " not in raw.upper()
    assert "client-controlled" not in raw.lower()
    assert not root.exists()
    assert db.query(SchemaMigrationHistory).count() == before_history
    assert inspect(engine).has_table("schema_version_state")


def test_unversioned_future_and_inconsistent_metadata_block_apply(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unversioned.sqlite3'}")
    Base.metadata.create_all(bind=engine)
    SchemaMigrationHistory.__table__.drop(bind=engine, checkfirst=True)
    SchemaVersionState.__table__.drop(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    assert inspect_migration_maintenance(db)["status"] == "blocked"
    with pytest.raises(Exception):
        apply_migration_maintenance(db, confirm=True, registry=registry_with(migration()))

    _engine2, db2 = sqlite_session(tmp_path / "future", seed_version=CURRENT_SCHEMA_VERSION + 1)
    assert inspect_migration_maintenance(db2)["blocked_reason"] == "future_version"

    db2.query(SchemaVersionState).delete()
    db2.commit()
    assert inspect_migration_maintenance(db2)["blocked_reason"] == "metadata_incomplete"


def test_apply_requires_confirmation_backup_first_and_is_idempotent(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path, seed_version=0)
    business_tables = ["users", "cameras", "system_settings", "recording_jobs", "recording_segments", "audit_events"]
    before_counts = table_counts(db, business_tables)
    backup_done = {"value": False}

    def assert_backup_first(_db):
        assert backup_done["value"] is True
        return {"ok": True}

    reg = registry_with(migration(apply_fn=assert_backup_first))
    original_backup = __import__("app.services.migration_maintenance", fromlist=["create_backup_before_upgrade"]).create_backup_before_upgrade

    def wrapped_backup(*args, **kwargs):
        result = original_backup(*args, **kwargs)
        backup_done["value"] = True
        return result

    monkeypatch.setattr("app.services.migration_maintenance.create_backup_before_upgrade", wrapped_backup)

    with pytest.raises(Exception) as confirm_block:
        apply_migration_maintenance(db, confirm=False, registry=reg, backup_root=str(tmp_path / "safe-db-backups"), allow_tmp_backup_root_for_tests=True)
    assert "confirm" in str(confirm_block.value).lower()

    result = apply_migration_maintenance(
        db,
        confirm=True,
        registry=reg,
        backup_root=str(tmp_path / "safe-db-backups"),
        allow_tmp_backup_root_for_tests=True,
    )
    rerun = apply_migration_maintenance(
        db,
        confirm=True,
        registry=reg,
        backup_root=str(tmp_path / "safe-db-backups"),
        allow_tmp_backup_root_for_tests=True,
    )

    assert result["status"] == "applied"
    assert result["applied_migrations"] == ["stage13_test_migration_apply"]
    assert result["backup_status"] == "verified"
    assert db.get(SchemaVersionState, "current").schema_version == CURRENT_SCHEMA_VERSION
    assert before_counts == table_counts(db, business_tables)
    assert rerun["status"] == "current"
    assert rerun["idempotent"] is True


def test_backup_failure_blocks_before_migration_execution(tmp_path, monkeypatch):
    _engine, db = sqlite_session(tmp_path, seed_version=0)
    executed = {"value": False}

    def mark_executed(_db):
        executed["value"] = True
        return {"ok": True}

    def fail_backup(*_args, **_kwargs):
        raise BackupSafetyBlocked("backup_failed", {"summary": "password=secret token=abc failed", "root_status": "ready"})

    monkeypatch.setattr("app.services.migration_maintenance.create_backup_before_upgrade", fail_backup)

    with pytest.raises(Exception) as exc:
        apply_migration_maintenance(
            db,
            confirm=True,
            registry=registry_with(migration(apply_fn=mark_executed)),
            backup_root=str(tmp_path / "safe-db-backups"),
            allow_tmp_backup_root_for_tests=True,
        )

    assert executed["value"] is False
    assert db.get(SchemaVersionState, "current").schema_version == 0
    assert "secret" not in str(exc.value)
    assert "abc" not in str(exc.value)


def test_migration_failure_report_is_sanitized_and_blocks_retry(tmp_path):
    def fail(_db):
        raise RuntimeError("password=s3cr3t token=abc failed")

    _engine, db = sqlite_session(tmp_path, seed_version=0)
    reg = registry_with(migration(risk=RISK_ADDITIVE_SAFE, apply_fn=fail))

    with pytest.raises(Exception) as exc:
        apply_migration_maintenance(
            db,
            confirm=True,
            registry=reg,
            backup_root=str(tmp_path / "safe-db-backups"),
            allow_tmp_backup_root_for_tests=True,
        )

    payload = exc.value.diagnostics
    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["status"] == "failed"
    assert payload["backup_created"] is True
    assert "s3cr3t" not in raw
    assert "abc" not in raw
    assert "rollback" in payload["report"]["rollback_guidance"].lower()
    assert inspect_migration_maintenance(db, registry=reg)["blocked_reason"] == "migration_failed_previous_attempt"


def test_migration_report_and_upgrade_report_are_sanitized(tmp_path):
    _engine, db = sqlite_session(tmp_path, seed_version=0)
    payload = dry_run_migration_maintenance(db, registry=registry_with(migration()))
    rendered = json.dumps(payload["report"], ensure_ascii=False)

    assert_migration_report_secret_safe(payload["report"])
    assert "rtsp://" not in rendered.lower()
    assert "postgresql://" not in rendered.lower()
    assert payload["report"]["side_effects"]["db_mutated"] is False

    report = build_upgrade_report(db)
    assert report["migration_maintenance"]["status"] in {"blocked", "current"}
    assert report["migration_maintenance"]["read_only"] is True
    assert report["migration_maintenance"]["side_effects"]["migration_executed"] is False


def test_no_startup_auto_apply_is_introduced():
    main_source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    assert "apply_migration_maintenance" not in main_source
    assert "execute_migration_plan" not in main_source


def test_migration_maintenance_api_permissions_and_public_payload_contract(client_db):
    client, _db, owner, operator = client_db

    assert client.get("/system/migrations/status").status_code == 401
    assert client.get("/system/migrations/status", headers=auth_headers(operator)).status_code == 403
    assert client.get("/system/migrations/status", headers=auth_headers(owner)).status_code == 200
    assert client.post("/system/migrations/dry-run", headers=auth_headers(owner)).status_code == 200

    rejected = client.post("/system/migrations/apply", json={"confirm": False}, headers=auth_headers(owner))
    assert rejected.status_code == 409

    for field in ["backup_root", "backup_path", "backup_dir", "path", "destination"]:
        response = client.post("/system/migrations/apply", json={"confirm": True, field: "/tmp/client-controlled"}, headers=auth_headers(owner))
        assert response.status_code == 422

    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("GET", "/system/migrations/status", "manage_settings") in rows
    assert ("POST", "/system/migrations/dry-run", "manage_settings") in rows
    assert ("POST", "/system/migrations/apply", "manage_settings") in rows
