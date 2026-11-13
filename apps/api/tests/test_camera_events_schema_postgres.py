"""Registered 10 -> 11 migration on real owned disposable PostgreSQL databases."""
import dataclasses

import pytest
from sqlalchemy import inspect, text

from app.services import bootstrap
from app.services import schema_update_control as control
from app.services.schema_migrations import (
    MigrationRegistry, STAGE172_EVENT_RECEIVING_MIGRATION, SchemaMigrationBlocked,
    execute_migration_plan, validate_current_target_schema,
)
from test_format_schema_stage1375_postgres import format_database, business_snapshot, pytestmark


def source10(db):
    execute_migration_plan(db, target_version=10)
    assert "events_enabled" not in {column["name"] for column in inspect(db.connection()).get_columns("cameras")}


@pytest.mark.parametrize("retired_name", [False, True])
def test_exact_migration_preserves_accounts_recordings_camera_and_default_off(format_database, retired_name):
    engine, db = format_database
    source10(db)
    if retired_name:
        db.execute(text("ALTER TABLE system_settings ADD COLUMN system_name VARCHAR(100) NULL")); db.commit()
    before = business_snapshot(db)
    result = execute_migration_plan(db)
    assert result["executed_migrations"] == [STAGE172_EVENT_RECEIVING_MIGRATION.migration_id]
    assert business_snapshot(db) == before
    assert db.execute(text("SELECT events_enabled FROM cameras WHERE id=1")).scalar_one() is False
    assert control.target_shape_is_exact(db)[0]
    assert validate_current_target_schema(db)["schema_version"] == 11
    assert execute_migration_plan(db)["executed_migrations"] == []
    db.execute(text("UPDATE cameras SET events_enabled=TRUE WHERE id=1")); db.commit()
    assert execute_migration_plan(db)["executed_migrations"] == []
    assert db.execute(text("SELECT events_enabled FROM cameras WHERE id=1")).scalar_one() is True
    db.rollback()
    bootstrap.init_db()  # A normal restart does not reset an explicit opt-in.


def test_failed_v11_verification_rolls_back_only_its_column_and_keeps_source_data(format_database):
    _engine, db = format_database
    source10(db)
    shape, rows = control.database_shape_fingerprint(db), business_snapshot(db)
    def fail(_db):
        raise RuntimeError("injected verification failure")
    broken = dataclasses.replace(STAGE172_EVENT_RECEIVING_MIGRATION, verify=fail)
    with pytest.raises(SchemaMigrationBlocked):
        execute_migration_plan(db, registry=MigrationRegistry([broken]))
    assert control.database_shape_fingerprint(db) == shape
    assert business_snapshot(db) == rows
    assert db.execute(text("SELECT schema_version FROM schema_version_state WHERE id='current'")).scalar_one() == 10
