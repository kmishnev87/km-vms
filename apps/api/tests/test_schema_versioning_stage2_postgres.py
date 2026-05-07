import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.version import APP_VERSION
from app.db.session import Base
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.services.schema_versioning import (
    CURRENT_BASELINE_ID,
    CURRENT_SCHEMA_VERSION,
    SchemaVersionBlocked,
    create_schema_version_tables,
    ensure_schema_version_state,
    schema_version_status,
    shape_from_tables,
)
from test_schema_versioning_stage2 import existing_shape


POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL")


pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="KMVMS_STAGE2_POSTGRES_URL is required for disposable PostgreSQL validation")


@pytest.fixture
def pg_session():
    schema = f"stage2_{uuid.uuid4().hex}"
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


def test_postgres_fresh_bootstrap_write_path_and_idempotency(pg_session):
    engine, db = pg_session

    Base.metadata.create_all(bind=engine)
    first = ensure_schema_version_state(db, pre_bootstrap_shape=shape_from_tables({}))
    second = ensure_schema_version_state(db, pre_bootstrap_shape=shape_from_tables({}))

    assert first["status"] == "current"
    assert second["managed"] is True
    assert db.query(SchemaVersionState).count() == 1
    assert db.query(SchemaMigrationHistory).count() == 1


def test_postgres_existing_unversioned_and_known_drift_adoption(pg_session):
    _engine, db = pg_session

    adopted = ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape())
    assert adopted["status"] == "adopted_baseline"

    db.query(SchemaMigrationHistory).delete()
    db.query(SchemaVersionState).delete()
    db.commit()

    drift = ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape(missing_setup=True, nullable_camera=True))
    assert drift["status"] == "drift_known_safe"
    assert any(item["table"] == "setup_locks" for item in drift["known_safe_drift"])


def test_postgres_bad_states_and_incomplete_metadata_are_blocked(pg_session):
    engine, db = pg_session
    create_schema_version_tables(engine)

    payload = schema_version_status(db)
    assert payload["status"] == "unversioned"
    assert inspect(engine).has_table("schema_version_state")
    assert inspect(engine).has_table("schema_migration_history")

    row = SchemaVersionState(
        id="current",
        schema_version=CURRENT_SCHEMA_VERSION + 1,
        baseline_id=CURRENT_BASELINE_ID,
        app_version=APP_VERSION,
        app_build_version="test",
        status="current",
        source="migration_runner",
    )
    db.add(row)
    db.commit()
    before = (row.schema_version, row.status, row.error_summary)

    status = schema_version_status(db)
    db.refresh(row)

    assert status["status"] == "future_version"
    assert (row.schema_version, row.status, row.error_summary) == before
    with pytest.raises(SchemaVersionBlocked):
        ensure_schema_version_state(db, pre_bootstrap_shape=existing_shape())

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
            source="fresh_create_all",
        )
    )
    db.commit()

    incomplete = schema_version_status(db)
    assert incomplete["managed"] is False
    assert incomplete["status"] == "metadata_incomplete"


def test_postgres_status_before_metadata_tables_is_read_only(pg_session):
    engine, db = pg_session

    payload = schema_version_status(db)

    assert payload["status"] == "unversioned"
    assert not inspect(engine).has_table("schema_version_state")
    assert not inspect(engine).has_table("schema_migration_history")
