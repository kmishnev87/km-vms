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
from app.services.backup_before_upgrade import BackupExecutionConfig, BackupSafetyBlocked
from app.services.db_adoption import apply_db_adoption, assert_adoption_report_secret_safe, dry_run_db_adoption, inspect_db_adoption
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION, SCHEMA_METADATA_TABLES
from test_schema_migration_runner_stage3 import seed_state


def sqlite_session(tmp_path, *, create_product=True, drop_metadata=True):
    db_path = tmp_path / "stage13_adoption.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    if create_product:
        Base.metadata.create_all(bind=engine)
        if drop_metadata:
            for table_name in sorted(SCHEMA_METADATA_TABLES, reverse=True):
                table = Base.metadata.tables.get(table_name)
                if table is not None:
                    table.drop(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def table_counts(db, names):
    result = {}
    for name in names:
        result[name] = int(db.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)
    return result


def add_user(db, *, role=ROLE_ADMIN, username=None):
    user = User(
        username=username or f"stage13_{role}",
        full_name=f"stage13 {role}",
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
    engine, db = sqlite_session(tmp_path, drop_metadata=False)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    owner = add_user(db, role=ROLE_OWNER, username="stage13_owner")
    operator = add_user(db, role=ROLE_OPERATOR, username="stage13_operator")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), db, owner, operator
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_unversioned_known_schema_is_adoptable_and_dry_run_is_read_only(tmp_path):
    engine, db = sqlite_session(tmp_path)
    before_tables = set(inspect(engine).get_table_names())
    payload = dry_run_db_adoption(db)

    assert payload["status"] == "adoptable"
    assert payload["dry_run"] is True
    assert payload["mutates_database"] is False
    assert payload["creates_backup_files"] is False
    assert payload["migration_executed"] is False
    assert not inspect(engine).has_table("schema_version_state")
    assert set(inspect(engine).get_table_names()) == before_tables


def test_already_adopted_schema_reports_already_adopted(tmp_path):
    _engine, db = sqlite_session(tmp_path, drop_metadata=False)
    seed_state(db, version=CURRENT_SCHEMA_VERSION)

    payload = inspect_db_adoption(db)

    assert payload["status"] == "already_adopted"
    assert payload["already_adopted"] is True
    assert payload["can_adopt"] is False


def test_unknown_unrelated_schema_and_missing_required_table_are_blocked(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unknown.sqlite3'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE foreign_debug_table (id INTEGER PRIMARY KEY)"))
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    assert inspect_db_adoption(db)["status"] == "blocked"

    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    engine2, db2 = sqlite_session(missing_root)
    with engine2.begin() as conn:
        conn.execute(text("DROP TABLE users"))
    missing = inspect_db_adoption(db2)
    assert missing["status"] == "blocked"
    assert "users" in missing["required_tables_missing"]


def test_inconsistent_metadata_is_blocked(tmp_path):
    engine, db = sqlite_session(tmp_path, create_product=False, drop_metadata=False)
    SchemaVersionState.__table__.create(bind=engine)

    payload = inspect_db_adoption(db)

    assert payload["status"] == "blocked"
    assert payload["metadata_present"] is False


def test_migration_control_metadata_without_current_state_is_blocked(tmp_path):
    engine, db = sqlite_session(
        tmp_path,
        create_product=False,
        drop_metadata=False,
    )
    Base.metadata.tables["schema_migration_control"].create(bind=engine)

    payload = inspect_db_adoption(db)

    assert payload["status"] == "blocked"
    assert payload["metadata_present"] is False
    assert payload["can_adopt"] is False


def test_apply_requires_confirmation_and_backup_failure_writes_no_metadata(tmp_path, monkeypatch):
    engine, db = sqlite_session(tmp_path)

    with pytest.raises(Exception) as confirm_block:
        apply_db_adoption(db, confirm=False, backup_root=str(tmp_path / "safe-db-backups"), allow_tmp_backup_root_for_tests=True)
    assert "confirm" in str(confirm_block.value).lower()

    def fail_backup(*_args, **_kwargs):
        raise BackupSafetyBlocked("backup_failed", {"summary": "password=secret token=abc failed", "root_status": "ready"})

    monkeypatch.setattr("app.services.db_adoption.create_backup_before_upgrade", fail_backup)
    with pytest.raises(Exception) as backup_block:
        apply_db_adoption(db, confirm=True, backup_root=str(tmp_path / "safe-db-backups"), allow_tmp_backup_root_for_tests=True)
    assert "secret" not in str(backup_block.value)
    assert "abc" not in str(backup_block.value)
    assert not inspect(engine).has_table("schema_version_state")
    assert not inspect(engine).has_table("schema_migration_history")


def test_apply_writes_only_metadata_and_rerun_is_idempotent(tmp_path):
    engine, db = sqlite_session(tmp_path)
    business_tables = ["users", "cameras", "system_settings", "recording_jobs", "recording_segments", "audit_events"]
    before_counts = table_counts(db, business_tables)

    result = apply_db_adoption(
        db,
        confirm=True,
        backup_root=str(tmp_path / "safe-db-backups"),
        allow_tmp_backup_root_for_tests=True,
    )
    after_counts = table_counts(db, business_tables)
    rerun = apply_db_adoption(
        db,
        confirm=True,
        backup_root=str(tmp_path / "safe-db-backups"),
        allow_tmp_backup_root_for_tests=True,
    )

    assert result["status"] == "adopted"
    assert result["backup_status"] == "verified"
    assert result["migration_executed"] is False
    assert result["business_data_mutated"] is False
    assert before_counts == after_counts
    assert db.query(SchemaVersionState).count() == 1
    assert db.query(SchemaMigrationHistory).count() == 1
    assert rerun["status"] == "already_adopted"
    assert rerun["idempotent"] is True
    assert db.query(SchemaVersionState).count() == 1
    assert db.query(SchemaMigrationHistory).count() == 1


def test_migration_runner_apply_is_not_referenced_by_adoption_service():
    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "db_adoption.py").read_text(encoding="utf-8")

    assert "execute_migration_plan" not in source
    assert ".apply(" not in source


def test_adoption_report_is_sanitized_and_upgrade_report_includes_read_only_summary(tmp_path):
    _engine, db = sqlite_session(tmp_path)
    payload = dry_run_db_adoption(db)
    rendered = json.dumps(payload["report"], ensure_ascii=False)

    assert_adoption_report_secret_safe(payload["report"])
    assert "Bearer " not in rendered
    assert "rtsp://" not in rendered.lower()
    assert payload["report"]["side_effects"]["db_mutated"] is False

    from app.services.upgrade_report import build_upgrade_report

    report = build_upgrade_report(db)
    assert report["db_adoption"]["status"] == "adoptable"
    assert report["db_adoption"]["read_only"] is True
    assert report["db_adoption"]["side_effects"]["db_mutated"] is False


def test_db_adoption_api_permissions_and_endpoint_registry(client_db):
    client, _db, owner, operator = client_db

    assert client.get("/system/db-adoption/status").status_code == 401
    assert client.get("/system/db-adoption/status", headers=auth_headers(operator)).status_code == 403
    assert client.get("/system/db-adoption/status", headers=auth_headers(owner)).status_code == 200
    assert client.post("/system/db-adoption/dry-run", headers=auth_headers(owner)).status_code == 200
    rejected = client.post("/system/db-adoption/apply", json={"confirm": False}, headers=auth_headers(owner))
    assert rejected.status_code == 409

    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("GET", "/system/db-adoption/status", "manage_settings") in rows
    assert ("POST", "/system/db-adoption/dry-run", "manage_settings") in rows
    assert ("POST", "/system/db-adoption/apply", "manage_settings") in rows


def test_db_adoption_apply_api_rejects_public_backup_root_override(client_db):
    client, _db, owner, _operator = client_db

    response = client.post(
        "/system/db-adoption/apply",
        json={"confirm": True, "backup_root": "/tmp/client-controlled"},
        headers=auth_headers(owner),
    )

    assert response.status_code == 422


def test_db_adoption_apply_endpoint_does_not_pass_request_backup_root_to_service(client_db, monkeypatch):
    import app.routers.maintenance as maintenance_router

    client, _db, owner, _operator = client_db
    observed = {}

    def spy_apply_db_adoption(db, **kwargs):
        observed.update(kwargs)
        return {
            "status": "already_adopted",
            "reason": "Schema metadata is already valid.",
            "applied": False,
            "idempotent": True,
            "metadata_present": True,
            "report_id": "test-report",
        }

    monkeypatch.setattr(maintenance_router, "apply_db_adoption", spy_apply_db_adoption)

    response = client.post(
        "/system/db-adoption/apply",
        json={"confirm": True},
        headers=auth_headers(owner),
    )

    assert response.status_code == 200
    assert observed["confirm"] is True
    assert observed["actor"].username == owner.username
    assert "backup_root" not in observed
    assert "allow_tmp_backup_root_for_tests" not in observed
