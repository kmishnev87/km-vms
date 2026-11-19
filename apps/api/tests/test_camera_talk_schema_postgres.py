"""Real disposable PostgreSQL coverage for the additive talk-credential edge."""
import os
import uuid
import dataclasses

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.session import Base
from app.services import bootstrap, schema_update_control as control
from app.services.schema_migrations import CAMERA_TALK_MIGRATION, AI_RECORDING_MIGRATION, MigrationRegistry, SchemaMigrationBlocked, execute_migration_plan, validate_current_target_schema
from app.services.schema_runtime_compatibility import RuntimeCompatibilityBlocked

URL = os.getenv("KMVMS_STAGE3_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Disposable PostgreSQL is required")


@pytest.fixture
def talk_database(monkeypatch):
    url = make_url(URL)
    name = "camera_talk_" + uuid.uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name))
    monkeypatch.setattr(bootstrap, "engine", engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


@pytest.mark.parametrize("retired", [False, True])
def test_exact_fresh_and_upgraded_target_shapes(talk_database, retired):
    bootstrap.init_db()
    with Session(talk_database) as db:
        if retired:
            db.execute(text("ALTER TABLE system_settings ADD COLUMN system_name VARCHAR(100) NULL")); db.commit()
        observed = control.database_shape_fingerprint(db)
        assert observed in control.TARGET_SHAPE_FINGERPRINTS, (retired, observed)
        assert validate_current_target_schema(db)["schema_version"] == 14
        assert inspect(db.connection()).get_columns("cameras")


def source12(engine):
    bootstrap.init_db()
    with Session(engine) as db:
        from datetime import datetime
        from app.models import Camera, RecordingSegment
        from app.core.security import encrypt_text
        db.add(Camera(id=1, name="Synthetic camera", storage_folder_name="synthetic", enabled=False,
                      host="192.0.2.1", port=554, protocol="rtsp", password_encrypted=encrypt_text("fixture-only")))
        db.flush()
        db.add(RecordingSegment(camera_id=1, file_path="/not-mounted/synthetic.mkv", relative_path="synthetic.mkv",
                                started_at=datetime(2026, 11, 17), status="ready", container_format="mkv"))
        db.commit()
        db.execute(text("ALTER TABLE cameras DROP COLUMN talk_password_encrypted"))
        db.execute(text("ALTER TABLE cameras DROP COLUMN recording_policy, DROP COLUMN recording_signal_state"))
        db.execute(text("ALTER TABLE system_settings DROP COLUMN event_recording_emergency_enabled"))
        db.execute(text("TRUNCATE schema_version_state, schema_migration_history"))
        db.commit()
        from test_schema_migration_runner_stage3 import seed_state
        seed_state(db, version=12)


def test_schema12_migration_preserves_credentials_settings_and_recordings(talk_database):
    source12(talk_database)
    with Session(talk_database) as db:
        before = {table: [dict(row) for row in db.execute(text(f'SELECT * FROM "{table}"')).mappings()] for table in ("users", "system_settings", "recording_segments")}
        camera_before = dict(db.execute(text("SELECT * FROM cameras WHERE id=1")).mappings().one())
        result = execute_migration_plan(db)
        assert result["executed_migrations"] == [CAMERA_TALK_MIGRATION.migration_id, AI_RECORDING_MIGRATION.migration_id]
        assert control.target_shape_is_exact(db)[0]
        assert validate_current_target_schema(db)["schema_version"] == 14
        assert execute_migration_plan(db)["executed_migrations"] == []
        for table, rows in before.items():
            assert [dict(row) for row in db.execute(text(f'SELECT * FROM "{table}"')).mappings()] == rows
        camera_after = dict(db.execute(text("SELECT * FROM cameras WHERE id=1")).mappings().one())
        assert camera_after.pop("talk_password_encrypted") is None
        assert camera_after.pop("recording_policy") is None
        assert camera_after.pop("recording_signal_state") is None
        assert camera_after == camera_before


def test_failed_edge_rolls_back_column_without_resetting_source_data(talk_database):
    source12(talk_database)
    with Session(talk_database) as db:
        before = control.database_shape_fingerprint(db)
        def fail(_db): raise RuntimeError("injected verification failure")
        with pytest.raises(SchemaMigrationBlocked):
            execute_migration_plan(db, registry=MigrationRegistry([dataclasses.replace(CAMERA_TALK_MIGRATION, verify=fail)]))
        assert control.database_shape_fingerprint(db) == before
        assert db.execute(text("SELECT schema_version FROM schema_version_state WHERE id='current'")).scalar_one() == 12
