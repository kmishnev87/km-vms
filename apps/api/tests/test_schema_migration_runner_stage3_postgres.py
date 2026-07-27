import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import Base
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.services.schema_migrations import (
    RISK_MANUAL_ONLY,
    RISK_METADATA_ONLY,
    RISK_REQUIRES_BACKUP,
    MIGRATION_SOURCE,
    MigrationDefinition,
    MigrationRegistry,
    PRODUCTION_MIGRATIONS,
    SchemaMigrationBlocked,
    STAGE4101_STORAGE_FOUNDATION_MIGRATION,
    STAGE41011_OPERATION_LINEAGE_MIGRATION,
    STAGE4102_RETENTION_MIGRATION,
    STAGE4103_ARCHIVE_INTEGRITY_MIGRATION,
    STAGE4104_ARCHIVE_MIGRATION,
    STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION,
    STAGE4101_TABLES,
    STAGE4103_REQUIRED_INDEXES,
    STAGE4103_TABLES,
    STAGE4104_TABLES,
    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION,
    STAGE660128_V7_DROP_DEFAULT_COLUMNS,
    STAGE660128_V7_NOT_NULL_COLUMNS,
    STAGE660128_V7_REQUIRED_FOREIGN_KEYS,
    build_migration_plan,
    execute_migration_plan,
)
from app.services.schema_versioning import (
    CURRENT_BASELINE_ID,
    CURRENT_SCHEMA_VERSION,
    CURRENT_STATE_ID,
    ensure_schema_version_state,
    schema_version_status,
    shape_from_tables,
)
from test_schema_migration_runner_stage3 import ok, seed_state


POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL") or os.getenv("KMVMS_STAGE3_POSTGRES_URL")


pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="KMVMS_STAGE2_POSTGRES_URL or KMVMS_STAGE3_POSTGRES_URL is required")


@pytest.fixture
def pg_session():
    schema = f"stage3_{uuid.uuid4().hex}"
    engine = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_engine(POSTGRES_URL, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=scoped_engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield scoped_engine, session
    finally:
        session.close()
        scoped_engine.dispose()
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def pg_safe_migration():
    return MigrationDefinition(
        migration_id="stage3_pg_test_safe_metadata",
        from_version=0,
        to_version=CURRENT_SCHEMA_VERSION,
        description="PostgreSQL test-only metadata migration.",
        risk=RISK_METADATA_ONLY,
        transaction_mode="session_transaction",
        preflight=ok,
        apply=ok,
        verify=ok,
        safe_failure_summary="postgres test migration failed safely",
        rollback_note="not available until Stage 5",
    )


def pg_risky_migration(risk):
    return MigrationDefinition(
        migration_id=f"stage3_pg_test_{risk}",
        from_version=0,
        to_version=CURRENT_SCHEMA_VERSION,
        description="PostgreSQL test-only blocked risk migration.",
        risk=risk,
        transaction_mode="manual_boundary",
        preflight=ok,
        apply=ok,
        verify=ok,
        safe_failure_summary="postgres blocked risk migration",
        rollback_note="Stage 4/5 safety required",
    )


def pg_failing_migration():
    def fail(_db):
        raise RuntimeError("password=pg-secret token=pg-token failed")

    return MigrationDefinition(
        migration_id="stage3_pg_test_failure",
        from_version=0,
        to_version=CURRENT_SCHEMA_VERSION,
        description="PostgreSQL test-only failure migration.",
        risk=RISK_METADATA_ONLY,
        transaction_mode="session_transaction",
        preflight=ok,
        apply=fail,
        verify=ok,
        safe_failure_summary="postgres test failure",
        rollback_note="not available until Stage 5",
    )


def test_postgres_fresh_runner_current_and_managed_idempotency(pg_session):
    engine, db = pg_session

    Base.metadata.create_all(bind=engine)
    ensure_schema_version_state(db, pre_bootstrap_shape=shape_from_tables({}))
    first = build_migration_plan(db)
    second = execute_migration_plan(db)

    assert first["status"] == "current"
    assert second["executed_migrations"] == []
    assert db.query(SchemaMigrationHistory).count() == 1


def test_postgres_lower_safe_migration_executes_once(pg_session):
    _engine, db = pg_session
    seed_state(db, version=0)
    registry = MigrationRegistry([pg_safe_migration()])

    first = execute_migration_plan(db, registry=registry)
    second = execute_migration_plan(db, registry=registry)

    assert first["executed_migrations"] == ["stage3_pg_test_safe_metadata"]
    assert second["executed_migrations"] == []
    assert db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == CURRENT_SCHEMA_VERSION
    assert db.query(SchemaMigrationHistory).filter(SchemaMigrationHistory.migration_id == "stage3_pg_test_safe_metadata").count() == 1
    assert schema_version_status(db)["status"] == "current"


def test_postgres_stage4101_additive_tables_upgrade_from_v1_and_restart(pg_session):
    engine, db = pg_session
    Base.metadata.create_all(bind=engine)
    for table in reversed(STAGE4104_TABLES):
        table.drop(bind=engine, checkfirst=True)
    for table in reversed(STAGE4103_TABLES):
        table.drop(bind=engine, checkfirst=True)
    for table in reversed(STAGE4101_TABLES):
        table.drop(bind=engine, checkfirst=True)
    db.query(SchemaMigrationHistory).delete()
    db.query(SchemaVersionState).delete()
    db.commit()
    seed_state(db, version=1)

    first = execute_migration_plan(db, registry=PRODUCTION_MIGRATIONS, target_version=6)
    second = execute_migration_plan(db, registry=PRODUCTION_MIGRATIONS, target_version=6)
    inspector = inspect(engine)

    assert first["executed_migrations"] == [
        STAGE4101_STORAGE_FOUNDATION_MIGRATION.migration_id,
        STAGE41011_OPERATION_LINEAGE_MIGRATION.migration_id,
        STAGE4102_RETENTION_MIGRATION.migration_id,
        STAGE4103_ARCHIVE_INTEGRITY_MIGRATION.migration_id,
        STAGE4104_ARCHIVE_MIGRATION.migration_id,
    ]
    assert second["executed_migrations"] == []
    assert all(inspector.has_table(table.name) for table in STAGE4101_TABLES)
    assert all(inspector.has_table(table.name) for table in STAGE4103_TABLES)
    for table_name, required_indexes in STAGE4103_REQUIRED_INDEXES.items():
        actual_indexes = {str(item.get("name") or "") for item in inspector.get_indexes(table_name)}
        assert required_indexes.issubset(actual_indexes)
    operation_columns = {item["name"] for item in inspector.get_columns("storage_operations")}
    assert {"parent_snapshot", "retry_depth", "domain_ref"}.issubset(operation_columns)
    state_column = next(
        item
        for item in inspector.get_columns("archive_integrity_remediation_items")
        if item["name"] == "state"
    )
    assert state_column["type"].length == 24
    camera_columns = {item["name"] for item in inspector.get_columns("cameras")}
    settings_columns = {item["name"] for item in inspector.get_columns("system_settings")}
    assert "retention_policy_version" in camera_columns
    assert {
        "auto_free_space_acknowledged_terms_version",
        "auto_free_space_acknowledged_at",
        "auto_free_space_acknowledged_by_user_id",
        "low_disk_suspended_physical_volume_id",
        "low_disk_suspended_at",
    }.issubset(settings_columns)
    assert db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == 6


def test_postgres_stage660128_v7_bootstrap_drift_normalizes_without_row_rewrite(
    pg_session,
):
    engine, db = pg_session
    Base.metadata.create_all(bind=engine)
    db.query(SchemaMigrationHistory).delete()
    db.query(SchemaVersionState).delete()
    db.commit()
    seed_state(db, version=7)

    drift_defaults = {
        ("archive_roots", "created_at"): "CURRENT_TIMESTAMP",
        ("archive_roots", "is_active"): "false",
        ("archive_roots", "is_available"): "true",
        ("archive_roots", "is_readable"): "true",
        ("archive_roots", "is_writable"): "true",
        ("archive_roots", "storage_namespace"): "'kmvms/recordings'",
        ("archive_roots", "updated_at"): "CURRENT_TIMESTAMP",
        ("cameras", "retention_days"): "30",
        ("cameras", "segment_minutes"): "5",
        ("cameras", "storage_quota_gb"): "50",
        ("recording_jobs", "created_at"): "CURRENT_TIMESTAMP",
        ("recording_jobs", "created_by"): "'KM VMS'",
        ("recording_jobs", "ownership"): "'KM VMS'",
        ("recording_jobs", "source"): "'recorder'",
        ("recording_jobs", "updated_at"): "CURRENT_TIMESTAMP",
        ("recording_segments", "cleanup_candidate"): "false",
        ("recording_segments", "created_at"): "CURRENT_TIMESTAMP",
        ("recording_segments", "ownership"): "'KM VMS'",
        ("recording_segments", "source"): "'recorder'",
        ("recording_segments", "updated_at"): "CURRENT_TIMESTAMP",
        ("storage_operations", "retry_depth"): "0",
        (
            "system_settings",
            "auto_free_space_cleanup_enabled",
        ): "false",
        (
            "system_settings",
            "recording_suspended_by_low_disk",
        ): "false",
        ("users", "is_active"): "true",
        ("users", "updated_at"): "CURRENT_TIMESTAMP",
    }
    assert set(drift_defaults) == {
        (table_name, column_name)
        for table_name, column_names in (
            STAGE660128_V7_DROP_DEFAULT_COLUMNS.items()
        )
        for column_name in column_names
    }
    for (table_name, column_name), expression in (
        drift_defaults.items()
    ):
        db.execute(
            text(
                f"ALTER TABLE {table_name} "
                f"ALTER COLUMN {column_name} "
                f"SET DEFAULT {expression}"
            )
        )
    for table_name, column_names in (
        STAGE660128_V7_NOT_NULL_COLUMNS.items()
    ):
        for column_name in column_names:
            db.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    f"ALTER COLUMN {column_name} DROP NOT NULL"
                )
            )
    for constraint_name in (
        STAGE660128_V7_REQUIRED_FOREIGN_KEYS
    ):
        db.execute(
            text(
                "ALTER TABLE recording_segments "
                f"DROP CONSTRAINT IF EXISTS {constraint_name}"
            )
        )
    db.commit()

    before_counts = {
        table_name: int(
            db.execute(
                text(f"SELECT COUNT(*) FROM {table_name}")
            ).scalar_one()
        )
        for table_name in (
            "users",
            "cameras",
            "recording_jobs",
            "recording_segments",
            "archive_roots",
        )
    }
    preparation = (
        STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
    )
    assert preparation.preflight(db)["status"] == "ready"
    result = preparation.apply(db)
    verified = preparation.verify(db)
    db.commit()
    after_counts = {
        table_name: int(
            db.execute(
                text(f"SELECT COUNT(*) FROM {table_name}")
            ).scalar_one()
        )
        for table_name in before_counts
    }

    assert result["status"] == "normalized"
    assert verified["status"] == "verified"
    assert before_counts == after_counts


def test_postgres_future_unknown_incomplete_and_read_only_plan(pg_session):
    engine, db = pg_session

    plan = build_migration_plan(db)
    assert plan["blocked_reason"] == "unversioned"
    assert not inspect(engine).has_table("schema_version_state")
    assert not inspect(engine).has_table("schema_migration_history")

    seed_state(db, version=CURRENT_SCHEMA_VERSION + 1)
    assert build_migration_plan(db)["blocked_reason"] == "future_version"
    with pytest.raises(SchemaMigrationBlocked):
        execute_migration_plan(db, registry=MigrationRegistry([pg_safe_migration()]))

    db.query(SchemaMigrationHistory).delete()
    db.query(SchemaVersionState).delete()
    db.commit()
    seed_state(db, version=CURRENT_SCHEMA_VERSION, baseline="foreign")
    assert build_migration_plan(db)["blocked_reason"] == "unknown"

    db.query(SchemaMigrationHistory).delete()
    db.query(SchemaVersionState).delete()
    db.commit()
    db.add(
        SchemaMigrationHistory(
            migration_id="stage3_pg_orphan",
            previous_version=None,
            target_version=CURRENT_SCHEMA_VERSION,
            schema_version=CURRENT_SCHEMA_VERSION,
            baseline_id=CURRENT_BASELINE_ID,
            app_version="test",
            app_build_version="test",
            status="current",
            source=MIGRATION_SOURCE,
        )
    )
    db.commit()
    assert build_migration_plan(db)["blocked_reason"] == "metadata_incomplete"


def test_postgres_risky_and_manual_are_not_executed(pg_session):
    for risk in (RISK_REQUIRES_BACKUP, RISK_MANUAL_ONLY):
        _engine, db = pg_session
        if inspect(db.get_bind()).has_table("schema_migration_history"):
            db.query(SchemaMigrationHistory).delete()
            db.query(SchemaVersionState).delete()
            db.commit()
        seed_state(db, version=0)
        migration = pg_risky_migration(risk)
        registry = MigrationRegistry([migration])

        plan = build_migration_plan(db, registry=registry)

        assert plan["status"] == "blocked"
        assert plan["pending_migrations"][0]["risk"] == risk
        with pytest.raises(SchemaMigrationBlocked):
            execute_migration_plan(db, registry=registry)
        assert db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == 0


def test_postgres_failed_transition_rolls_back_without_polluting_canonical_history(pg_session):
    _engine, db = pg_session
    seed_state(db, version=0)
    migration = pg_failing_migration()
    registry = MigrationRegistry([migration])

    with pytest.raises(SchemaMigrationBlocked) as first:
        execute_migration_plan(db, registry=registry)

    row = db.get(SchemaVersionState, CURRENT_STATE_ID)
    assert row.schema_version == 0
    assert "pg-secret" not in first.value.diagnostics["summary"]
    assert "pg-token" not in first.value.diagnostics["summary"]
    assert (
        db.query(SchemaMigrationHistory)
        .filter(SchemaMigrationHistory.migration_id == "stage3_pg_test_failure")
        .count()
        == 0
    )

    retry_plan = build_migration_plan(db, registry=registry)
    assert retry_plan["status"] == "ready"
    with pytest.raises(SchemaMigrationBlocked) as second:
        execute_migration_plan(db, registry=registry)

    db.refresh(row)
    assert row.schema_version == 0
    assert second.value.status == "migration_failed"
    assert db.query(SchemaMigrationHistory).filter(SchemaMigrationHistory.migration_id == "stage3_pg_test_failure").count() == 0
