"""Real owned PostgreSQL: exact migration, rollback, graph and signal locking."""
import dataclasses
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import Camera, RecordingSegment, SystemSettings
from app.schemas.ai_recording import PolicyUpdate
from app.services import ai_recording as domain, bootstrap, schema_update_control as control
from app.services.schema_migrations import AI_RECORDING_MIGRATION, MigrationRegistry, SchemaMigrationBlocked, execute_migration_plan, validate_current_target_schema
from test_schema_migration_runner_stage3 import seed_state

URL = os.getenv("KMVMS_STAGE3_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Disposable PostgreSQL required")


@pytest.fixture
def database(monkeypatch):
    url = make_url(URL)
    name = "ai_recording_" + uuid.uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url.set(database=name))
    monkeypatch.setattr(bootstrap, "engine", engine)
    try:
        bootstrap.init_db()
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()


def source13(engine):
    with Session(engine) as db:
        db.add(SystemSettings(id=1, system_initialized=True, timezone="UTC", language="en", storage_path="/not-mounted/archive"))
        db.add(Camera(id=1, name="First", storage_folder_name="first", protocol="onvif", host="192.0.2.1", port=80,
            enabled=False, events_enabled=True, default_record_stream="sub", default_live_stream="main"))
        db.flush()
        db.add(RecordingSegment(camera_id=1, file_path="/not-mounted/fixture.mkv", started_at=datetime(2026, 11, 18), status="ready"))
        db.commit()
        for sql in ("ALTER TABLE cameras DROP COLUMN recording_policy", "ALTER TABLE cameras DROP COLUMN recording_signal_state",
                    "ALTER TABLE system_settings DROP COLUMN event_recording_emergency_enabled",
                    "TRUNCATE schema_version_state, schema_migration_history"):
            db.execute(text(sql))
        db.commit()
        seed_state(db, version=13)


@pytest.mark.parametrize("retired", [False, True])
def test_exact_fresh_v14_shape(database, retired):
    with Session(database) as db:
        if retired:
            db.execute(text("ALTER TABLE system_settings ADD COLUMN system_name VARCHAR(100) NULL")); db.commit()
        assert control.target_shape_is_exact(db)[0]
        assert validate_current_target_schema(db)["schema_version"] == 14


@pytest.mark.parametrize("retired", [False, True])
def test_schema13_to14_preserves_all_previous_values_and_is_idempotent(database, retired):
    source13(database)
    with Session(database) as db:
        if retired:
            db.execute(text("ALTER TABLE system_settings ADD COLUMN system_name VARCHAR(100) NULL")); db.commit()
        before = {table: [dict(row) for row in db.execute(text(f'SELECT * FROM "{table}" ORDER BY 1')).mappings()]
                  for table in ("users", "cameras", "system_settings", "recording_segments", "audit_events")}
        result = execute_migration_plan(db)
        assert result["executed_migrations"] == [AI_RECORDING_MIGRATION.migration_id]
        assert control.target_shape_is_exact(db)[0]
        assert validate_current_target_schema(db)["schema_version"] == 14
        assert execute_migration_plan(db)["executed_migrations"] == []
        for table, expected in before.items():
            actual = [dict(row) for row in db.execute(text(f'SELECT * FROM "{table}" ORDER BY 1')).mappings()]
            for row in actual:
                if table == "cameras":
                    assert row.pop("recording_policy") is None
                    assert row.pop("recording_signal_state") is None
                if table == "system_settings":
                    assert row.pop("event_recording_emergency_enabled") is False
            assert actual == expected


def test_failed_migration_rolls_back_data_and_ddl(database):
    source13(database)
    with Session(database) as db:
        before = control.database_shape_fingerprint(db)
        def fail(_db): raise RuntimeError("injected verification failure")
        with pytest.raises(SchemaMigrationBlocked):
            execute_migration_plan(db, registry=MigrationRegistry([dataclasses.replace(AI_RECORDING_MIGRATION, verify=fail)]))
        assert control.database_shape_fingerprint(db) == before
        assert db.execute(text("SELECT schema_version FROM schema_version_state WHERE id='current'")).scalar_one() == 13
        assert "recording_policy" not in {column["name"] for column in inspect(db.connection()).get_columns("cameras")}


def test_concurrent_link_changes_cannot_form_cycle(database):
    with Session(database) as db:
        for cid in (1, 2):
            db.add(Camera(id=cid, name=f"Camera {cid}", storage_folder_name=f"cam{cid}", protocol="rtsp", host="192.0.2.1", port=554))
        db.commit()
    barrier = threading.Barrier(2)
    def save(cid, target):
        with Session(database) as db:
            barrier.wait(timeout=5)
            payload = PolicyUpdate(expected_revision=0, policy={"mode": "linked", "source": "none", "profile": "full_quality", "linked_camera_id": target})
            try:
                domain.save_policy(db, cid, payload, SimpleNamespace(id=1, username="test-admin", role="admin"))
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, 1, 2), pool.submit(save, 2, 1)]
        assert sorted(future.result(timeout=15) for future in futures) == [200, 422]


def test_product_backup_and_real_restore_preserve_policy_pause_and_emergency(database, tmp_path, monkeypatch):
    from app.services.backup_before_upgrade import BackupExecutionConfig, create_backup_before_upgrade, verify_backup_manifest
    from app.services.current_db_restore_executor import _pg_restore
    from app.services.schema_update_shapes import database_shape_payload
    from app.schemas.ai_recording import RecordingSignal
    with Session(database) as db:
        db.add(SystemSettings(id=1, system_initialized=True, timezone="UTC", language="en", storage_path="/not-mounted/archive",
                              event_recording_emergency_enabled=True))
        db.add(Camera(id=1, name="Preserved", storage_folder_name="preserved", protocol="onvif", host="192.0.2.1", port=80,
                      enabled=False, events_enabled=True, default_record_stream="sub"))
        db.commit()
        domain.save_policy(db, 1, PolicyUpdate(expected_revision=0, policy={"mode": "events", "source": "camera", "profile": "full_quality", "prebuffer_seconds": 30}),
                           SimpleNamespace(id=1, username="test-admin", role="admin"))
        value = RecordingSignal(source="camera", source_generation="fixture-generation", identity="a" * 64,
            kind="person", active=True, occurred_at=datetime(2026, 11, 18))
        assert domain.observe_recording_signal(db, 1, value, received=value.occurred_at)
        db.commit()
        policy = db.get(Camera, 1).recording_policy
        state = db.get(Camera, 1).recording_signal_state
        source_shape = database_shape_payload(db)
        assert control.target_shape_is_exact(db)[0]
        config = BackupExecutionConfig(backup_root=tmp_path / "owned-backups", allow_tmp_for_tests=True,
            require_persistent_root=False, min_required_bytes=0)
        result = create_backup_before_upgrade(db, config=config)
        assert verify_backup_manifest(result["manifest_path"])["valid"]
    url = make_url(URL)
    name = "ai_stage2_restore_" + uuid.uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    target = None
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        _pg_restore(url.set(database=name), Path(result["backup_file_path"]))
        target = create_engine(url.set(database=name))
        with Session(target) as db:
            camera = db.get(Camera, 1)
            assert camera.recording_policy == policy and camera.recording_signal_state == state
            assert not camera.enabled and camera.default_record_stream == "sub" and camera.recording_mode == "always"
            assert db.get(SystemSettings, 1).event_recording_emergency_enabled is True
            restored_shape = database_shape_payload(db)
            source_tables = {item["table"]: item for item in source_shape["tables"]}
            restored_tables = {item["table"]: item for item in restored_shape["tables"]}
            # pg_dump/pg_restore can re-deparse legacy control CHECK expressions
            # with different casts/parentheses. That pre-existing fingerprint
            # concern is not permission to relax any product admission guard.
            # Require exact application DDL, all control structure except the
            # expression rendering, and the ordinary product startup validator.
            assert source_tables.keys() == restored_tables.keys()
            for table_name, original in source_tables.items():
                restored = restored_tables[table_name]
                if table_name in {"schema_migration_control", "schema_migration_attempts"}:
                    for shape in (original, restored):
                        shape["constraints"]["checks"] = [{"name": check["name"]} for check in shape["constraints"]["checks"]]
                assert restored == original
            control.verify_control_shape(db)
            assert validate_current_target_schema(db)["schema_version"] == 14
        with monkeypatch.context() as restored_runtime:
            restored_runtime.setattr(bootstrap, "engine", target)
            bootstrap.init_db()  # Normal restart after restore, without DDL/guard bypass.
    finally:
        if target is not None:
            target.dispose()
        assert re.fullmatch(r"ai_stage2_restore_[0-9a-f]{32}", name), "owned_restore_database_name_required"
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
