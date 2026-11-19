"""Registered additive online-mode migration in disposable PostgreSQL databases."""
import dataclasses
import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.services import bootstrap, schema_update_control as control
from app.services.schema_migrations import ONLINE_MODE_MIGRATION, CAMERA_TALK_MIGRATION, AI_RECORDING_MIGRATION, MigrationRegistry, SchemaMigrationBlocked, execute_migration_plan
from test_schema_migration_runner_stage3 import seed_state

TEST_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="owned PostgreSQL test URL required")


@pytest.fixture
def old_database(monkeypatch):
    url = make_url(TEST_URL)
    name = "online_mode_" + uuid.uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name))
    try:
        monkeypatch.setattr(bootstrap, "engine", engine)
        bootstrap.init_db()
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE system_settings DROP COLUMN online_mode"))
            connection.execute(text("ALTER TABLE cameras DROP COLUMN talk_password_encrypted"))
            connection.execute(text("ALTER TABLE cameras DROP COLUMN recording_policy, DROP COLUMN recording_signal_state"))
            connection.execute(text("ALTER TABLE system_settings DROP COLUMN event_recording_emergency_enabled"))
            connection.execute(text("TRUNCATE schema_version_state, schema_migration_history"))
        with Session(engine) as db:
            seed_state(db, version=11)
            db.execute(text("INSERT INTO system_settings (id,system_initialized,timezone,language,storage_path,recording_format,auto_free_space_cleanup_enabled,recording_suspended_by_low_disk,created_at,updated_at) VALUES (1,true,'UTC','ru','/not-mounted/archive','mkv',false,false,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
            db.commit()
            yield engine, db
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


@pytest.mark.parametrize("retired_name", [False, True])
def test_schema11_to12_keeps_settings_and_is_idempotent(old_database, retired_name):
    _engine, db = old_database
    if retired_name:
        db.execute(text("ALTER TABLE system_settings ADD COLUMN system_name VARCHAR(100) NULL"))
        db.commit()
    before = tuple(db.execute(text("SELECT timezone,language,storage_path,recording_format FROM system_settings")).one())
    assert execute_migration_plan(db)["executed_migrations"] == [ONLINE_MODE_MIGRATION.migration_id, CAMERA_TALK_MIGRATION.migration_id, AI_RECORDING_MIGRATION.migration_id]
    assert db.execute(text("SELECT online_mode FROM system_settings")).scalar_one() == "stable"
    assert tuple(db.execute(text("SELECT timezone,language,storage_path,recording_format FROM system_settings")).one()) == before
    matched, shape = control.target_shape_is_exact(db)
    assert matched, shape
    db.execute(text("UPDATE system_settings SET online_mode='fast'")); db.commit()
    assert execute_migration_plan(db)["executed_migrations"] == []
    assert db.execute(text("SELECT online_mode FROM system_settings")).scalar_one() == "fast"


def test_failed_online_mode_verification_rolls_back_atomically(old_database):
    engine, db = old_database
    def fail(_db):
        raise RuntimeError("injected online-mode verification failure")
    broken = dataclasses.replace(ONLINE_MODE_MIGRATION, verify=fail)
    with pytest.raises(SchemaMigrationBlocked):
        execute_migration_plan(db, registry=MigrationRegistry([broken]))
    assert "online_mode" not in {column["name"] for column in inspect(db.connection()).get_columns("system_settings")}
    assert db.execute(text("SELECT schema_version FROM schema_version_state WHERE id='current'")).scalar_one() == 11
