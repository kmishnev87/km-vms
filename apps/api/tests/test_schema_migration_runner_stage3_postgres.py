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
    SchemaMigrationBlocked,
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
        to_version=1,
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
        to_version=1,
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
        to_version=1,
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


def test_postgres_failed_history_rerun_does_not_mark_current(pg_session):
    _engine, db = pg_session
    seed_state(db, version=0)
    migration = pg_failing_migration()
    registry = MigrationRegistry([migration])

    with pytest.raises(SchemaMigrationBlocked) as first:
        execute_migration_plan(db, registry=registry)

    row = db.get(SchemaVersionState, CURRENT_STATE_ID)
    failed = db.query(SchemaMigrationHistory).filter(SchemaMigrationHistory.migration_id == "stage3_pg_test_failure").one()
    assert row.schema_version == 0
    assert failed.status == "failed"
    assert "pg-secret" not in failed.error_summary
    assert "pg-token" not in failed.error_summary
    assert "pg-secret" not in first.value.diagnostics["summary"]

    retry_plan = build_migration_plan(db, registry=registry)
    assert retry_plan["blocked_reason"] == "migration_failed_previous_attempt"
    with pytest.raises(SchemaMigrationBlocked) as second:
        execute_migration_plan(db, registry=registry)

    db.refresh(row)
    assert row.schema_version == 0
    assert second.value.status == "migration_failed_previous_attempt"
    assert (
        db.query(SchemaMigrationHistory)
        .filter(SchemaMigrationHistory.migration_id == "stage3_pg_test_failure", SchemaMigrationHistory.status == "applied")
        .count()
        == 0
    )
