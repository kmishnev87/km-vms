import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.permissions import ROLE_OPERATOR, ROLE_OWNER
from app.core.security import create_access_token, hash_password
from app.db.session import Base, get_db
from app.main import app
from app.models.schema_version import SchemaVersionState
from app.models.user import User
from app.services.backup_before_upgrade import BackupExecutionConfig, BackupSafetyBlocked, create_backup_before_upgrade
from app.services.restore_maintenance import (
    TARGET_CURRENT_PRODUCT_DB,
    TARGET_TEMPORARY_VALIDATION_DB,
    RestoreMaintenanceBlocked,
    apply_restore_maintenance,
    assert_restore_report_secret_safe,
    dry_run_restore_maintenance,
    inspect_restore_maintenance,
)
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from app.services.upgrade_report import build_upgrade_report
from test_schema_migration_runner_stage3 import seed_state


def sqlite_session(tmp_path, *, seed=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "stage13_restore_source.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    if seed:
        seed_state(db, version=CURRENT_SCHEMA_VERSION)
        owner = User(
            username="stage13_restore_owner",
            full_name="Stage 13 Restore Owner",
            password_hash=hash_password("stage13-test-password"),
            role="owner",
            is_active=True,
        )
        db.add(owner)
        db.commit()
    return engine, db


def make_backup(tmp_path):
    engine, db = sqlite_session(tmp_path)
    try:
        backup = create_backup_before_upgrade(
            db,
            config=BackupExecutionConfig(backup_root=tmp_path / "safe-db-backups", allow_tmp_for_tests=True, source="stage13_restore_test"),
        )
        return engine, db, backup
    except Exception:
        db.close()
        engine.dispose()
        raise


def add_user(db, *, role=ROLE_OWNER, username=None):
    user = User(
        username=username or f"stage13_restore_{role}",
        full_name=f"stage13 restore {role}",
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
    engine, db = sqlite_session(tmp_path)
    owner = add_user(db, role=ROLE_OWNER, username="stage13_restore_api_owner")
    operator = add_user(db, role=ROLE_OPERATOR, username="stage13_restore_api_operator")

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), db, owner, operator
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def test_restore_status_no_artifacts_is_read_only(tmp_path):
    root = tmp_path / "empty-backups"

    payload = inspect_restore_maintenance(backup_root=str(root))

    assert payload["status"] == "no_artifacts"
    assert payload["artifact_count"] == 0
    assert payload["can_restore"] is False
    assert payload["current_product_restore_supported"] is False
    assert payload["current_product_restore_status"] == "blocked"
    assert payload["current_product_restore_reason"] == "current_product_restore_not_enabled"
    assert not root.exists()


def test_valid_artifact_dry_run_is_read_only_and_sanitized(tmp_path):
    engine, db, backup = make_backup(tmp_path)
    before_tables = set(inspect(engine).get_table_names())
    target_url = f"sqlite:///{tmp_path / 'stage13_restore_target.sqlite3'}"

    payload = dry_run_restore_maintenance(
        db,
        artifact_id=backup["backup_id"],
        target_kind=TARGET_TEMPORARY_VALIDATION_DB,
        backup_root=str(tmp_path / "safe-db-backups"),
        target_database_url=target_url,
    )
    raw = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "valid"
    assert payload["can_restore"] is True
    assert payload["mutates_database"] is False
    assert payload["creates_current_backup"] is False
    assert payload["compatibility_status"] == "compatible"
    assert payload["current_product_restore_supported"] is False
    assert "sqlite:///" not in raw
    assert "password" not in raw.lower()
    assert set(inspect(engine).get_table_names()) == before_tables
    assert not Path(tmp_path / "stage13_restore_target.sqlite3").exists()


def test_invalid_and_future_artifacts_are_blocked(tmp_path):
    engine, db, backup = make_backup(tmp_path)
    root = tmp_path / "safe-db-backups"
    invalid = root / "invalid.manifest.json"
    invalid.write_text("{not-json", encoding="utf-8")

    status = inspect_restore_maintenance(backup_root=str(root))
    assert any(item["artifact_id"] == invalid.name and item["valid"] is False for item in status["artifacts"])

    manifest_path = Path(backup["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"]["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    payload = dry_run_restore_maintenance(
        db,
        artifact_id=backup["backup_id"],
        target_kind=TARGET_TEMPORARY_VALIDATION_DB,
        backup_root=str(root),
        target_database_url=f"sqlite:///{tmp_path / 'future_target.sqlite3'}",
    )

    assert payload["status"] == "blocked"
    assert payload["compatibility_status"] == "blocked"
    engine.dispose()


def test_temporary_validation_apply_restores_isolated_db_and_validates_owner(tmp_path):
    _engine, db, backup = make_backup(tmp_path)
    target_path = tmp_path / "stage13_restore_target.sqlite3"

    result = apply_restore_maintenance(
        db,
        confirm=True,
        artifact_id=backup["backup_id"],
        target_kind=TARGET_TEMPORARY_VALIDATION_DB,
        backup_root=str(tmp_path / "safe-db-backups"),
        target_database_url=f"sqlite:///{target_path}",
    )

    assert result["status"] == "restored"
    assert result["restore_executed"] is True
    assert result["current_backup_status"] == "not_required"
    assert result["temporary_validation_restore_supported"] is True
    assert result["current_product_restore_supported"] is False
    assert result["video_archive_files_restored"] is False
    assert result["migration_auto_apply"] is False
    assert target_path.exists()

    target_engine = create_engine(f"sqlite:///{target_path}")
    TargetSession = sessionmaker(bind=target_engine, autoflush=False, autocommit=False)
    target_db = TargetSession()
    try:
        assert target_db.get(SchemaVersionState, "current").schema_version == CURRENT_SCHEMA_VERSION
        assert target_db.query(User).filter(User.role == "owner").count() >= 1
    finally:
        target_db.close()
        target_engine.dispose()


def test_current_product_target_requires_backup_before_restore_and_blocks_backup_failure(tmp_path, monkeypatch):
    _engine, db, backup = make_backup(tmp_path)
    order = []

    def fake_backup(*_args, **_kwargs):
        order.append("backup")
        return {
            "status": "verified",
            "backup_id": "stage13-current-backup",
            "backup_file_label": "configured_backup_root/current.sqlite3",
            "metadata_file_label": "configured_backup_root/current.metadata.json",
            "restore_validation_status": "not_performed_stage5_deferred",
        }

    def fake_restore(*_args, **_kwargs):
        order.append("restore")
        return {
            "status": "restored",
            "post_restore_validation": {"passed": True, "checks": {"owner_or_admin_access": {"passed": True}}},
            "video_archive_files_restored": False,
        }

    monkeypatch.setattr("app.services.restore_maintenance.create_backup_before_upgrade", fake_backup)
    monkeypatch.setattr("app.services.restore_maintenance._restore_artifact_to_target", fake_restore)
    monkeypatch.setattr("app.services.restore_maintenance._target_status", lambda *args, **kwargs: {"status": "safe", "target_kind": TARGET_CURRENT_PRODUCT_DB, "requires_current_backup": True})

    result = apply_restore_maintenance(
        db,
        confirm=True,
        artifact_id=backup["backup_id"],
        target_kind=TARGET_CURRENT_PRODUCT_DB,
        backup_root=str(tmp_path / "safe-db-backups"),
        allow_current_product_db_for_tests=True,
    )

    assert result["status"] == "restored"
    assert order == ["backup", "restore"]

    order.clear()

    def fail_backup(*_args, **_kwargs):
        order.append("backup")
        raise BackupSafetyBlocked("backup_failed", {"summary": "password=secret token=abc failed"})

    monkeypatch.setattr("app.services.restore_maintenance.create_backup_before_upgrade", fail_backup)
    with pytest.raises(RestoreMaintenanceBlocked) as exc:
        apply_restore_maintenance(
            db,
            confirm=True,
            artifact_id=backup["backup_id"],
            target_kind=TARGET_CURRENT_PRODUCT_DB,
            backup_root=str(tmp_path / "safe-db-backups"),
            allow_current_product_db_for_tests=True,
        )

    assert order == ["backup"]
    assert exc.value.status == "backup_failed"
    assert "secret" not in str(exc.value)
    assert "abc" not in str(exc.value)


def test_current_product_public_apply_is_explicitly_blocked_before_restore(tmp_path, monkeypatch):
    _engine, db, backup = make_backup(tmp_path)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("restore execution must not run for public current_product_db apply")

    monkeypatch.setattr("app.services.restore_maintenance._restore_artifact_to_target", fail_if_called)
    blocked = dry_run_restore_maintenance(
        db,
        artifact_id=backup["backup_id"],
        target_kind=TARGET_CURRENT_PRODUCT_DB,
        backup_root=str(tmp_path / "safe-db-backups"),
    )

    assert blocked["status"] == "blocked"
    assert blocked["current_product_restore_supported"] is False
    assert blocked["current_product_restore_status"] == "blocked"
    assert blocked["current_product_restore_reason"] == "current_product_restore_not_enabled"

    with pytest.raises(RestoreMaintenanceBlocked) as exc:
        apply_restore_maintenance(
            db,
            confirm=True,
            artifact_id=backup["backup_id"],
            target_kind=TARGET_CURRENT_PRODUCT_DB,
            backup_root=str(tmp_path / "safe-db-backups"),
        )

    assert exc.value.status == "current_product_restore_not_enabled"
    assert exc.value.diagnostics["restore_executed"] is False


def test_public_temporary_apply_uses_server_side_disposable_target_and_cleanup(tmp_path, monkeypatch):
    _engine, db, backup = make_backup(tmp_path)
    target_path = tmp_path / "server_side_stage13_target.sqlite3"
    calls = []

    def fake_create(current_url):
        calls.append(("create", current_url.database))
        return {
            "target_database_url": f"sqlite:///{target_path}",
            "database_name": "kmvms_stage5_stage13_restore_validation_test",
            "target_label": "server_side_disposable_validation_db",
            "cleanup": {"attempted": False, "status": "not_attempted"},
        }

    def fake_drop(current_url, database_name):
        calls.append(("drop", database_name))
        return {"attempted": True, "status": "completed", "target_label": "server_side_disposable_validation_db"}

    monkeypatch.setattr(
        "app.services.restore_maintenance._target_status",
        lambda *args, **kwargs: {
            "status": "safe",
            "reason": "server_side_disposable_validation_target_planned",
            "target_kind": TARGET_TEMPORARY_VALIDATION_DB,
            "temporary_validation_restore_supported": True,
            "temporary_validation_target": "server_side_disposable_postgresql",
            "requires_current_backup": False,
        },
    )
    monkeypatch.setattr("app.services.restore_maintenance._create_server_side_disposable_target", fake_create)
    monkeypatch.setattr("app.services.restore_maintenance._drop_server_side_disposable_target", fake_drop)

    result = apply_restore_maintenance(
        db,
        confirm=True,
        artifact_id=backup["backup_id"],
        target_kind=TARGET_TEMPORARY_VALIDATION_DB,
        backup_root=str(tmp_path / "safe-db-backups"),
    )
    raw = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "restored"
    assert result["temporary_validation_target"] == "server_side_disposable_postgresql"
    assert result["temporary_validation_cleanup"]["status"] == "completed"
    assert calls[0][0] == "create"
    assert calls[1] == ("drop", "kmvms_stage5_stage13_restore_validation_test")
    assert target_path.exists()
    assert "sqlite:///" not in raw
    assert "server_side_stage13_target" not in raw


def test_confirmation_target_and_owner_validation_failures_are_safe(tmp_path):
    _engine, db, backup = make_backup(tmp_path)

    with pytest.raises(RestoreMaintenanceBlocked) as confirm:
        apply_restore_maintenance(
            db,
            confirm=False,
            artifact_id=backup["backup_id"],
            target_kind=TARGET_TEMPORARY_VALIDATION_DB,
            backup_root=str(tmp_path / "safe-db-backups"),
            target_database_url=f"sqlite:///{tmp_path / 'target.sqlite3'}",
        )
    assert "confirm" in str(confirm.value).lower()

    target_path = tmp_path / "non_empty.sqlite3"
    target_engine = create_engine(f"sqlite:///{target_path}")
    Base.metadata.create_all(bind=target_engine)
    target_engine.dispose()
    blocked = dry_run_restore_maintenance(
        db,
        artifact_id=backup["backup_id"],
        target_kind=TARGET_TEMPORARY_VALIDATION_DB,
        backup_root=str(tmp_path / "safe-db-backups"),
        target_database_url=f"sqlite:///{target_path}",
    )
    assert blocked["status"] == "blocked"


def test_restore_report_and_upgrade_report_are_sanitized(tmp_path):
    _engine, db, backup = make_backup(tmp_path)
    payload = dry_run_restore_maintenance(
        db,
        artifact_id=backup["backup_id"],
        target_kind=TARGET_TEMPORARY_VALIDATION_DB,
        backup_root=str(tmp_path / "safe-db-backups"),
        target_database_url=f"sqlite:///{tmp_path / 'target.sqlite3'}",
    )
    rendered = json.dumps(payload["report"], ensure_ascii=False)

    assert_restore_report_secret_safe(payload["report"])
    assert "sqlite:///" not in rendered
    assert "pg_restore" not in rendered
    assert payload["report"]["side_effects"]["db_restored"] is False

    report = build_upgrade_report(db)
    assert report["restore_maintenance"]["read_only"] is True
    assert report["restore_maintenance"]["side_effects"]["db_restored"] is False


def test_restore_maintenance_does_not_auto_apply_migration_or_update():
    main_source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    restore_source = (Path(__file__).resolve().parents[1] / "app" / "services" / "restore_maintenance.py").read_text(encoding="utf-8")

    assert "apply_restore_maintenance" not in main_source
    assert "execute_migration_plan" not in restore_source
    assert "run_update_check" not in restore_source


def test_restore_maintenance_api_permissions_and_public_payload_contract(client_db):
    client, _db, owner, operator = client_db

    assert client.get("/system/restore/status").status_code == 401
    assert client.get("/system/restore/status", headers=auth_headers(operator)).status_code == 403
    assert client.get("/system/restore/status", headers=auth_headers(owner)).status_code == 200
    assert client.post("/system/restore/dry-run", json={}, headers=auth_headers(owner)).status_code == 200

    for endpoint in ["/system/restore/dry-run", "/system/restore/apply"]:
        for field in [
            "backup_root",
            "backup_path",
            "backup_dir",
            "path",
            "source_path",
            "destination",
            "target_database_url",
            "db_url",
            "database_url",
            "connection_string",
        ]:
            payload = {"artifact_id": "kmvms-db-test", "target_kind": TARGET_TEMPORARY_VALIDATION_DB, field: "/tmp/client-controlled"}
            if endpoint.endswith("/apply"):
                payload["confirm"] = True
            response = client.post(endpoint, json=payload, headers=auth_headers(owner))
            assert response.status_code == 422

    rejected = client.post(
        "/system/restore/apply",
        json={"confirm": False, "artifact_id": "kmvms-db-test", "target_kind": TARGET_TEMPORARY_VALIDATION_DB},
        headers=auth_headers(owner),
    )
    assert rejected.status_code == 409

    rows = {(item.method, item.path, item.decision) for item in ENDPOINT_PERMISSIONS}
    assert ("GET", "/system/restore/status", "manage_settings") in rows
    assert ("POST", "/system/restore/dry-run", "manage_settings") in rows
    assert ("POST", "/system/restore/apply", "manage_settings") in rows
