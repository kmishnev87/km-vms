import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.endpoint_permissions import ENDPOINT_PERMISSIONS
from app.core.version import APP_VERSION
from app.db.session import Base
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.routers.settings import system_schema_status
from app.services.schema_versioning import (
    CURRENT_BASELINE_ID,
    CURRENT_MIGRATION_ID,
    CURRENT_SCHEMA_VERSION,
    SchemaVersionBlocked,
    create_schema_version_tables,
    ensure_schema_version_state,
    schema_version_status,
    shape_from_tables,
    validate_schema_version_pre_bootstrap,
)


def session_with_schema():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def existing_shape(*, missing_setup=False, recorder_runtime=True, nullable_camera=False, extra_table=False):
    tables = {
        "users": {"columns": {"id": {"nullable": False}}},
        "cameras": {
            "columns": {
                "id": {"nullable": False},
                "segment_minutes": {"nullable": nullable_camera},
                "retention_days": {"nullable": nullable_camera},
                "storage_quota_gb": {"nullable": nullable_camera},
            }
        },
        "system_settings": {"columns": {"id": {"nullable": False}, "system_name": {"nullable": True}}},
        "archive_roots": {"columns": {"id": {"nullable": False}}},
        "recording_jobs": {"columns": {"id": {"nullable": False}}},
        "recording_segments": {"columns": {"id": {"nullable": False}}},
        "audit_events": {"columns": {"id": {"nullable": False}}},
    }
    if not missing_setup:
        tables["setup_locks"] = {"columns": {"name": {"nullable": False}}}
    if recorder_runtime:
        tables["recorder_runtime_status"] = {"columns": {"recorder_instance_id": {"nullable": False}}}
    if extra_table:
        tables["foreign_debug_table"] = {"columns": {"id": {"nullable": False}}}
    return shape_from_tables(tables)


def test_fresh_db_initializes_schema_version_once():
    _engine, db = session_with_schema()

    first = ensure_schema_version_state(db, pre_bootstrap_shape=shape_from_tables({}))
    second = ensure_schema_version_state(db, pre_bootstrap_shape=shape_from_tables({}))

    assert first["schema_version"] == CURRENT_SCHEMA_VERSION
    assert first["baseline_id"] == CURRENT_BASELINE_ID
    assert first["app_version"] == APP_VERSION
    assert first["status"] == "current"
    assert second["status"] == "current"
    assert db.query(SchemaVersionState).count() == 1
    assert db.query(SchemaMigrationHistory).count() == 1


def test_existing_unversioned_matching_baseline_is_adopted_idempotently():
    _engine, db = session_with_schema()

    first = ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape())
    second = ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape())

    assert first["status"] == "adopted_baseline"
    assert first["source"] == "adopted_existing_db"
    assert second["managed"] is True
    assert db.query(SchemaMigrationHistory).count() == 1


def test_known_safe_drift_is_adopted_and_classified_without_repairing_product_schema():
    engine = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    payload = ensure_schema_version_state(
        db,
        pre_bootstrap_shape=existing_shape(missing_setup=True, nullable_camera=True),
    )

    assert payload["status"] == "drift_known_safe"
    assert any(item["type"] == "missing_table" and item["table"] == "setup_locks" for item in payload["known_safe_drift"])
    assert any(item["type"] == "nullable_column_drift" and item["table"] == "cameras" for item in payload["known_safe_drift"])
    assert inspect(engine).has_table("schema_version_state")
    assert inspect(engine).has_table("schema_migration_history")
    assert not inspect(engine).has_table("setup_locks")


def test_db_only_recorder_runtime_status_is_tolerated():
    _engine, db = session_with_schema()

    payload = ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape(recorder_runtime=True))

    assert payload["status"] == "adopted_baseline"
    assert any(item["table"] == "recorder_runtime_status" for item in payload["known_safe_drift"])


def test_unknown_extra_table_blocks_adoption_without_metadata_write():
    _engine, db = session_with_schema()

    with pytest.raises(SchemaVersionBlocked) as exc:
        ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape(extra_table=True))

    assert exc.value.status == "drift_blocked"
    assert "foreign_debug_table" in str(exc.value.diagnostics)
    assert db.query(SchemaVersionState).count() == 0
    assert db.query(SchemaMigrationHistory).count() == 0


def test_pre_bootstrap_guard_blocks_unsafe_unversioned_schema_before_create_all_repair():
    engine = create_engine("sqlite:///:memory:")

    with pytest.raises(SchemaVersionBlocked) as exc:
        validate_schema_version_pre_bootstrap(engine, existing_shape(extra_table=True))

    assert exc.value.status == "drift_blocked"
    assert not inspect(engine).has_table("schema_version_state")
    assert not inspect(engine).has_table("schema_migration_history")


def test_schema_status_is_read_only_when_metadata_tables_are_absent():
    engine = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    payload = schema_version_status(db)

    assert payload["managed"] is False
    assert payload["status"] == "unversioned"
    assert payload["summary"] == "Schema version metadata is not initialized. Adoption is required."
    assert not inspect(engine).has_table("schema_version_state")
    assert not inspect(engine).has_table("schema_migration_history")


def test_schema_status_reports_partial_metadata_without_creating_or_repairing_tables():
    engine = create_engine("sqlite:///:memory:")
    SchemaVersionState.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    payload = schema_version_status(db)

    assert payload["managed"] is False
    assert payload["status"] == "metadata_incomplete"
    assert inspect(engine).has_table("schema_version_state")
    assert not inspect(engine).has_table("schema_migration_history")


def test_schema_status_requires_matching_current_state_and_history():
    engine = create_engine("sqlite:///:memory:")
    create_schema_version_tables(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(
        SchemaVersionState(
            id="current",
            schema_version=CURRENT_SCHEMA_VERSION,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version="test",
            status="current",
            source="fresh_create_all",
        )
    )
    db.commit()

    missing_history = schema_version_status(db)
    assert missing_history["managed"] is False
    assert missing_history["status"] == "metadata_incomplete"

    db.add(
        SchemaMigrationHistory(
            migration_id=CURRENT_MIGRATION_ID,
            previous_version=None,
            target_version=CURRENT_SCHEMA_VERSION,
            schema_version=CURRENT_SCHEMA_VERSION,
            baseline_id="wrong_baseline",
            app_version=APP_VERSION,
            app_build_version="test",
            status="current",
            source="fresh_create_all",
        )
    )
    db.commit()

    mismatched_history = schema_version_status(db)
    assert mismatched_history["managed"] is False
    assert mismatched_history["status"] == "metadata_incomplete"


def test_schema_status_reports_bad_states_without_mutating_rows():
    _engine, db = session_with_schema()
    cases = [
        (
            SchemaVersionState(
                id="current",
                schema_version=CURRENT_SCHEMA_VERSION + 1,
                baseline_id=CURRENT_BASELINE_ID,
                app_version=APP_VERSION,
                app_build_version="test",
                status="current",
                source="migration_runner",
            ),
            "future_version",
        ),
        (
            SchemaVersionState(
                id="current",
                schema_version=0,
                baseline_id=CURRENT_BASELINE_ID,
                app_version=APP_VERSION,
                app_build_version="test",
                status="current",
                source="migration_runner",
            ),
            "downgrade_blocked",
        ),
        (
            SchemaVersionState(
                id="current",
                schema_version=CURRENT_SCHEMA_VERSION,
                baseline_id="foreign_baseline",
                app_version=APP_VERSION,
                app_build_version="test",
                status="current",
                source="manual_admin",
            ),
            "unknown",
        ),
        (
            SchemaVersionState(
                id="current",
                schema_version=CURRENT_SCHEMA_VERSION,
                baseline_id=CURRENT_BASELINE_ID,
                app_version=APP_VERSION,
                app_build_version="test",
                status="adoption_failed",
                source="adopted_existing_db",
            ),
            "adoption_failed",
        ),
    ]

    for row, expected_status in cases:
        db.query(SchemaMigrationHistory).delete()
        db.query(SchemaVersionState).delete()
        db.commit()
        db.add(row)
        db.commit()
        before = (
            row.schema_version,
            row.baseline_id,
            row.status,
            row.error_summary,
            row.updated_at,
        )

        payload = schema_version_status(db)
        db.refresh(row)
        after = (
            row.schema_version,
            row.baseline_id,
            row.status,
            row.error_summary,
            row.updated_at,
        )

        assert payload["managed"] is False
        assert payload["status"] == expected_status
        assert after == before


def test_future_unknown_lower_and_failed_states_block_risky_mutation():
    _engine, db = session_with_schema()
    cases = [
        SchemaVersionState(
            id="current",
            schema_version=CURRENT_SCHEMA_VERSION + 1,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version="test",
            status="current",
            source="migration_runner",
        ),
        SchemaVersionState(
            id="current",
            schema_version=CURRENT_SCHEMA_VERSION,
            baseline_id="foreign_baseline",
            app_version=APP_VERSION,
            app_build_version="test",
            status="current",
            source="manual_admin",
        ),
        SchemaVersionState(
            id="current",
            schema_version=0,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version="test",
            status="current",
            source="migration_runner",
        ),
        SchemaVersionState(
            id="current",
            schema_version=CURRENT_SCHEMA_VERSION,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version="test",
            status="adoption_failed",
            source="adopted_existing_db",
        ),
    ]
    expected = ["future_version", "unknown", "downgrade_blocked", "adoption_failed"]

    for row, status in zip(cases, expected, strict=True):
        db.query(SchemaVersionState).delete()
        db.commit()
        db.add(row)
        db.commit()
        with pytest.raises(SchemaVersionBlocked) as exc:
            ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape())
        assert exc.value.status == status


def test_diagnostic_status_is_protected_sanitized_and_separates_app_from_schema_version():
    _engine, db = session_with_schema()
    ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape(missing_setup=True, nullable_camera=True))

    payload = system_schema_status(db=db, current_user=object())
    rendered = str(payload)

    assert payload["schema_version"] == CURRENT_SCHEMA_VERSION
    assert payload["app_version"] == APP_VERSION
    assert payload["schema_version"] != payload["app_version"]
    assert payload["recorder_metadata_owner"] == "api_bootstrap_only"
    assert "rtsp://" not in rendered.lower()
    assert "authorization" not in rendered.lower()
    assert "password" not in rendered.lower()
    assert "secret" not in rendered.lower()
    assert any(
        item.method == "GET"
        and item.path == "/system/schema/status"
        and item.decision == "manage_settings"
        for item in ENDPOINT_PERMISSIONS
    )


def test_recorder_is_not_schema_version_metadata_owner():
    recorder_source = Path(__file__).resolve().parents[2] / "recorder" / "main.py"
    text = recorder_source.read_text(encoding="utf-8")

    assert "schema_version_state" not in text
    assert "schema_migration_history" not in text
    assert "SchemaVersionState" not in text
