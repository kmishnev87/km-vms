"""Real registered format migration, in owned disposable test databases only."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.services import bootstrap
from app.services import schema_update_control as control
from app.services.schema_migrations import (
    MigrationRegistry,
    STAGE1375_FORMAT_CHANGE_MIGRATION,
    SchemaMigrationBlocked,
    execute_migration_plan,
)
from test_schema_migration_runner_stage3 import seed_state

TEST_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="isolated PostgreSQL test URL is required")


@pytest.fixture
def format_database(monkeypatch):
    url = make_url(TEST_URL)
    name = "stage1375_" + uuid.uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name))
    try:
        monkeypatch.setattr(bootstrap, "engine", engine)
        bootstrap.init_db()
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE system_settings DROP COLUMN recording_format_apply_mode, DROP COLUMN recording_format_requested_at"))
            connection.execute(text("TRUNCATE schema_version_state, schema_migration_history"))
        with Session(engine) as db:
            seed_state(db, version=9)
            from app.models import Camera, RecordingSegment, SystemSettings, User
            db.add(User(id=1, username="preserved-test-user", password_hash="non-secret-test-hash", role="owner", is_active=True))
            db.add(Camera(id=1, name="Preserved camera", storage_folder_name="preserved-camera", host="192.0.2.1", port=554, protocol="rtsp", enabled=False))
            db.commit()
            db.execute(text("INSERT INTO system_settings (id,system_initialized,timezone,language,storage_path,recording_format,auto_free_space_cleanup_enabled,recording_suspended_by_low_disk,created_at,updated_at) VALUES (1,true,'UTC','ru','/not-mounted/archive','mkv',false,false,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
            db.add(RecordingSegment(camera_id=1, file_path="/not-mounted/archive/existing.mkv", relative_path="existing.mkv", started_at=datetime(2026,11,2,1,0), status="ready", container_format="mkv"))
            db.commit()
            yield engine, db
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


def business_snapshot(db):
    result = {}
    for table in ("users", "cameras", "recording_segments", "system_settings"):
        rows = [dict(row) for row in db.execute(text(f'SELECT * FROM "{table}" ORDER BY 1')).mappings()]
        for row in rows:
            row.pop("recording_format_apply_mode", None)
            row.pop("recording_format_requested_at", None)
        result[table] = rows
    return hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()


@pytest.mark.parametrize("retired_name", (False, True))
def test_registered_9_to_10_preserves_business_rows_and_is_idempotent(format_database, retired_name):
    engine, db = format_database
    if retired_name:
        db.execute(text("ALTER TABLE system_settings ADD COLUMN system_name VARCHAR(100) NULL"))
        db.commit()
    source = control.database_shape_fingerprint(db)
    assert source in control.SOURCE_SHAPE_FINGERPRINT_ALTERNATES["0.8.22"]
    before = business_snapshot(db)
    first = execute_migration_plan(db)
    assert first["executed_migrations"] == [STAGE1375_FORMAT_CHANGE_MIGRATION.migration_id]
    assert control.target_shape_is_exact(db)[0] is True
    assert business_snapshot(db) == before
    assert db.execute(text("SELECT recording_format_apply_mode, recording_format_requested_at FROM system_settings WHERE id=1")).one() == (None, None)
    assert execute_migration_plan(db)["executed_migrations"] == []
    assert business_snapshot(db) == before


def test_failed_verification_rolls_back_columns_state_and_preserves_rows(format_database):
    _engine, db = format_database
    before_shape = control.database_shape_fingerprint(db)
    before_data = business_snapshot(db)
    def fail(_db):
        raise RuntimeError("injected format migration verification failure")
    broken = dataclasses.replace(STAGE1375_FORMAT_CHANGE_MIGRATION, verify=fail)
    with pytest.raises(SchemaMigrationBlocked):
        execute_migration_plan(db, registry=MigrationRegistry([broken]))
    assert control.database_shape_fingerprint(db) == before_shape
    assert business_snapshot(db) == before_data
    assert db.execute(text("SELECT schema_version FROM schema_version_state WHERE id='current'")).scalar_one() == 9
    assert "recording_format_apply_mode" not in {col["name"] for col in inspect(db.connection()).get_columns("system_settings")}


def test_older_prepared_runtime_can_boot_read_write_and_keep_true_schema10(format_database, monkeypatch, tmp_path):
    from app.services import schema_runtime_compatibility as compat
    from app.services import schema_versioning, schema_migrations
    engine, db = format_database
    execute_migration_plan(db)
    assert compat.publish_runtime_compatibility(db)["minimum_writer_version"] == 9
    db.commit()
    monkeypatch.setattr(schema_versioning, "CURRENT_SCHEMA_VERSION", 9)
    monkeypatch.setattr(schema_migrations, "CURRENT_SCHEMA_VERSION", 9)
    monkeypatch.setattr(control, "TARGET_SCHEMA_VERSION", 9)
    monkeypatch.setattr(control, "UPDATE_STATUS_PATH", tmp_path / "no-status.json")
    history_before = control.canonical_schema_history_fingerprint(db)
    shape_before = control.database_shape_fingerprint(db)
    assert schema_versioning.schema_version_status(db)["runtime_compatibility"] == "verified"
    assert schema_migrations.build_migration_plan(db, target_version=9)["status"] == "current"
    schema_versioning.validate_schema_version_pre_bootstrap(engine, schema_versioning.inspect_schema_shape(engine))
    schema_migrations.validate_current_target_schema(db)
    assert control.resolve_schema_execution_mode(db) == "exact_target_noop"
    db.execute(text("UPDATE system_settings SET timezone='Europe/London' WHERE id=1"))
    db.commit()
    assert schema_versioning.schema_version_status(db)["schema_version"] == 10
    assert control.canonical_schema_history_fingerprint(db) == history_before
    assert control.database_shape_fingerprint(db) == shape_before
    db.execute(text("ALTER TABLE system_settings ADD COLUMN unregistered_change INTEGER NULL"))
    db.commit()
    assert schema_versioning.schema_version_status(db)["status"] == "future_version"


def test_update_after_application_rollback_binds_real_schema_not_old_tag_number(format_database, monkeypatch):
    from app.services import schema_runtime_compatibility as compat
    _engine, db = format_database
    execute_migration_plan(db)
    compat.publish_runtime_compatibility(db)
    db.commit()
    monkeypatch.setattr(control, "expected_source_lineage", lambda **_kw: ("0.8.23", "a" * 40, 9, control.SOURCE_SHAPE_FINGERPRINT_ALTERNATES["0.8.23"]))
    before = control.canonical_schema_history_fingerprint(db)
    installed, commit, actual, shape = control.validate_source_lineage(db, request_id="update-" + "1" * 32, target_release="0.8.24", target_commit="b" * 40)
    assert installed == "0.8.23" and commit == "a" * 40
    assert actual == 10 and shape in control.TARGET_SHAPE_FINGERPRINTS
    assert control.canonical_schema_history_fingerprint(db) == before
    state = db.get(compat.SchemaVersionState, "current")
    state.drift_classification = {}
    db.commit()
    with pytest.raises(control.SchemaControlError, match="installed_source_compatibility_not_verified"):
        control.validate_source_lineage(db, request_id="update-" + "1" * 32, target_release="0.8.24", target_commit="b" * 40)
