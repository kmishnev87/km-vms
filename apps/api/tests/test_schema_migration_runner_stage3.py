import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.db.session import Base
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.routers.settings import system_schema_plan, system_schema_status
from app.services.schema_migrations import (
    RISK_ADDITIVE_SAFE,
    RISK_MANUAL_ONLY,
    RISK_METADATA_ONLY,
    RISK_REQUIRES_BACKUP,
    MIGRATION_SOURCE,
    MigrationDefinition,
    MigrationRegistry,
    MigrationRegistryError,
    SchemaMigrationBlocked,
    build_migration_plan,
    execute_migration_plan,
    validate_schema_migrations_pre_bootstrap,
)
from app.services.schema_versioning import (
    CURRENT_BASELINE_ID,
    CURRENT_MIGRATION_ID,
    CURRENT_SCHEMA_VERSION,
    CURRENT_STATE_ID,
    SchemaVersionBlocked,
    create_schema_version_tables,
    ensure_schema_version_state,
    schema_version_status,
    shape_from_tables,
)
from test_schema_versioning_stage2 import existing_shape


def session_with_metadata():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def session_without_tables():
    engine = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def ok(_db):
    return {"ok": True}


def safe_migration(from_version=0, to_version=CURRENT_SCHEMA_VERSION, migration_id="stage3_test_safe_metadata"):
    return MigrationDefinition(
        migration_id=migration_id,
        from_version=from_version,
        to_version=to_version,
        description="Test-only metadata migration for runner validation.",
        risk=RISK_METADATA_ONLY,
        transaction_mode="session_transaction",
        preflight=ok,
        apply=ok,
        verify=ok,
        safe_failure_summary="test migration failed safely",
        rollback_note="not available until Stage 5",
    )


def risky_migration():
    return MigrationDefinition(
        migration_id="stage3_test_risky_requires_backup",
        from_version=0,
        to_version=CURRENT_SCHEMA_VERSION,
        description="Test-only risky migration classification.",
        risk=RISK_REQUIRES_BACKUP,
        transaction_mode="manual_boundary",
        preflight=ok,
        apply=ok,
        verify=ok,
        safe_failure_summary="test risky migration not executed",
        rollback_note="requires Stage 5 rollback plan",
    )


def manual_migration():
    return MigrationDefinition(
        migration_id="stage3_test_manual_only",
        from_version=0,
        to_version=CURRENT_SCHEMA_VERSION,
        description="Test-only manual migration classification.",
        risk=RISK_MANUAL_ONLY,
        transaction_mode="manual_boundary",
        preflight=ok,
        apply=ok,
        verify=ok,
        safe_failure_summary="test manual migration not executed",
        rollback_note="manual operator plan required",
    )


def seed_state(db, *, version=0, status="current", source=MIGRATION_SOURCE, baseline=CURRENT_BASELINE_ID):
    create_schema_version_tables(db.get_bind())
    db.add(
        SchemaVersionState(
            id=CURRENT_STATE_ID,
            schema_version=version,
            baseline_id=baseline,
            app_version=APP_VERSION,
            app_build_version="test",
            status=status,
            source=source,
        )
    )
    db.add(
        SchemaMigrationHistory(
            migration_id=f"seed_v{version}",
            previous_version=None,
            target_version=version,
            schema_version=version,
            baseline_id=baseline,
            app_version=APP_VERSION,
            app_build_version="test",
            status=status,
            source=source,
        )
    )
    db.commit()


def test_migration_registry_ordering_is_deterministic():
    registry = MigrationRegistry(
        [
            safe_migration(from_version=1, to_version=2, migration_id="stage3_test_002"),
            safe_migration(from_version=0, to_version=1, migration_id="stage3_test_001"),
        ]
    )

    assert [item.migration_id for item in registry.migrations] == ["stage3_test_001", "stage3_test_002"]


def test_duplicate_migration_id_or_conflicting_edge_is_detected():
    with pytest.raises(MigrationRegistryError):
        MigrationRegistry([safe_migration(migration_id="stage3_test_dup"), safe_migration(migration_id="stage3_test_dup")])
    with pytest.raises(MigrationRegistryError):
        MigrationRegistry(
            [
                safe_migration(migration_id="stage3_test_a"),
                safe_migration(migration_id="stage3_test_b"),
            ]
        )


def test_fresh_db_initializes_baseline_and_runner_reports_current():
    _engine, db = session_with_metadata()
    ensure_schema_version_state(db, pre_bootstrap_shape=shape_from_tables({}))

    plan = build_migration_plan(db)

    assert plan["status"] == "current"
    assert plan["pending_migrations"] == []
    assert plan["production_adoption_status"] == "production_adoption_deferred"


def test_managed_current_db_startup_is_idempotent_without_duplicate_history():
    _engine, db = session_with_metadata()
    ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape())
    ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape())

    first = build_migration_plan(db)
    second = execute_migration_plan(db)

    assert first["status"] == "current"
    assert second["executed_migrations"] == []
    assert db.query(SchemaMigrationHistory).count() == 1


def test_plan_mode_is_read_only_and_does_not_create_tables_or_mutate_rows():
    engine, db = session_without_tables()

    payload = build_migration_plan(db)

    assert payload["status"] == "blocked"
    assert payload["blocked_reason"] == "unversioned"
    assert not inspect(engine).has_table("schema_version_state")
    assert not inspect(engine).has_table("schema_migration_history")


def test_future_unknown_and_incomplete_metadata_block_plan_and_execute():
    _engine, db = session_with_metadata()
    seed_state(db, version=CURRENT_SCHEMA_VERSION + 1)
    plan = build_migration_plan(db)
    assert plan["blocked_reason"] == "future_version"
    with pytest.raises(SchemaMigrationBlocked):
        execute_migration_plan(db, registry=MigrationRegistry([safe_migration()]))

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
            migration_id="orphan",
            previous_version=None,
            target_version=CURRENT_SCHEMA_VERSION,
            schema_version=CURRENT_SCHEMA_VERSION,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version="test",
            status="current",
            source=MIGRATION_SOURCE,
        )
    )
    db.commit()
    assert build_migration_plan(db)["blocked_reason"] == "metadata_incomplete"


def test_lower_version_produces_plan_and_missing_migration_blocks_safely():
    _engine, db = session_with_metadata()
    seed_state(db, version=0)

    ready = build_migration_plan(db, registry=MigrationRegistry([safe_migration()]))
    assert ready["status"] == "ready"
    assert [item["migration_id"] for item in ready["pending_migrations"]] == ["stage3_test_safe_metadata"]

    with pytest.raises(SchemaMigrationBlocked) as exc:
        build_migration_plan(db, registry=MigrationRegistry(()))
    assert exc.value.status == "missing_migration"


def test_safe_migration_executes_once_and_records_history():
    _engine, db = session_with_metadata()
    seed_state(db, version=0)
    registry = MigrationRegistry([safe_migration()])

    first = execute_migration_plan(db, registry=registry)
    second = execute_migration_plan(db, registry=registry)
    row = db.get(SchemaVersionState, CURRENT_STATE_ID)

    assert first["executed_migrations"] == ["stage3_test_safe_metadata"]
    assert second["executed_migrations"] == []
    assert row.schema_version == CURRENT_SCHEMA_VERSION
    assert row.source == MIGRATION_SOURCE
    assert db.query(SchemaMigrationHistory).filter(SchemaMigrationHistory.migration_id == "stage3_test_safe_metadata").count() == 1


def test_risky_and_manual_migrations_are_planned_but_not_executed_before_stage4():
    for migration, risk in [(risky_migration(), RISK_REQUIRES_BACKUP), (manual_migration(), RISK_MANUAL_ONLY)]:
        _engine, db = session_with_metadata()
        seed_state(db, version=0)
        plan = build_migration_plan(db, registry=MigrationRegistry([migration]))

        assert plan["status"] == "blocked"
        assert plan["pending_migrations"][0]["risk"] == risk
        with pytest.raises(SchemaMigrationBlocked):
            execute_migration_plan(db, registry=MigrationRegistry([migration]))
        assert db.get(SchemaVersionState, CURRENT_STATE_ID).schema_version == 0


def test_migration_failure_does_not_mark_current_and_sanitizes_error():
    def fail(_db):
        raise RuntimeError("password=s3cr3t token=abc failed")

    _engine, db = session_with_metadata()
    seed_state(db, version=0)
    migration = MigrationDefinition(
        migration_id="stage3_test_failure",
        from_version=0,
        to_version=CURRENT_SCHEMA_VERSION,
        description="Test-only failure migration.",
        risk=RISK_ADDITIVE_SAFE,
        transaction_mode="session_transaction",
        preflight=ok,
        apply=fail,
        verify=ok,
        safe_failure_summary="test failure",
        rollback_note="not available until Stage 5",
    )

    with pytest.raises(SchemaMigrationBlocked) as exc:
        execute_migration_plan(db, registry=MigrationRegistry([migration]))

    row = db.get(SchemaVersionState, CURRENT_STATE_ID)
    failed = db.query(SchemaMigrationHistory).filter(SchemaMigrationHistory.migration_id == "stage3_test_failure").one()
    assert row.schema_version == 0
    assert failed.status == "failed"
    assert "s3cr3t" not in failed.error_summary
    assert "abc" not in failed.error_summary
    assert "s3cr3t" not in exc.value.diagnostics["summary"]

    retry_plan = build_migration_plan(db, registry=MigrationRegistry([migration]))
    assert retry_plan["status"] == "blocked"
    assert retry_plan["blocked_reason"] == "migration_failed_previous_attempt"
    with pytest.raises(SchemaMigrationBlocked) as retry_exc:
        execute_migration_plan(db, registry=MigrationRegistry([migration]))

    db.refresh(row)
    assert row.schema_version == 0
    assert retry_exc.value.status == "migration_failed_previous_attempt"
    assert db.query(SchemaMigrationHistory).filter(SchemaMigrationHistory.migration_id == "stage3_test_failure").count() == 1
    assert (
        db.query(SchemaMigrationHistory)
        .filter(SchemaMigrationHistory.migration_id == "stage3_test_failure", SchemaMigrationHistory.status == "applied")
        .count()
        == 0
    )
    assert "s3cr3t" not in retry_exc.value.diagnostics["summary"]
    assert "abc" not in retry_exc.value.diagnostics["summary"]


def test_schema_status_remains_read_only_and_plan_endpoint_is_protected():
    engine, db = session_without_tables()

    status = system_schema_status(db=db, current_user=object())
    plan = system_schema_plan(db=db, current_user=object())

    assert status["status"] == "unversioned"
    assert plan["blocked_reason"] == "unversioned"
    assert not inspect(engine).has_table("schema_version_state")
    assert any(item.method == "GET" and item.path == "/system/schema/plan" and item.decision == "manage_settings" for item in ENDPOINT_PERMISSIONS)


def test_recorder_does_not_reference_schema_version_metadata_tables():
    recorder_source = Path(__file__).resolve().parents[2] / "recorder" / "main.py"
    text = recorder_source.read_text(encoding="utf-8")

    assert "schema_version_state" not in text
    assert "schema_migration_history" not in text
    assert "SchemaMigrationHistory" not in text


def test_app_build_version_source_channel_work_remains_deferred():
    assert APP_BUILD_VERSION == "development"
    _engine, db = session_with_metadata()
    ensure_schema_version_state(db, pre_bootstrap_shape=shape_from_tables({}))
    plan = build_migration_plan(db)
    assert plan["app_build_version_source"] == "installed_build_metadata_or_development_fallback"


def test_pre_create_all_runner_hook_blocks_existing_unversioned_schema_before_legacy_repair():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.tables["users"].create(bind=engine)

    with pytest.raises(SchemaVersionBlocked) as exc:
        validate_schema_migrations_pre_bootstrap(engine)

    assert exc.value.status == "unversioned"
    assert not inspect(engine).has_table("schema_version_state")
    assert not inspect(engine).has_table("schema_migration_history")
