from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.camera import Camera
from app.services import migration_maintenance_runner as runner
from app.services.schema_migrations import (
    MigrationRegistry,
    STAGE13721_CAMERA_SUB_PROFILE_TOKEN_MIGRATION,
    STAGE13721_CAMERA_SUB_PROFILE_TOKEN_MIGRATION_ID,
    STAGE660128_UNIVERSAL_SCHEMA_MIGRATION,
    execute_migration_plan,
    migration_definition_fingerprint,
)
from app.services.schema_update_pipeline import PREVIOUS_RUNTIME_COMPATIBLE_MIGRATION_IDS
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from test_schema_migration_runner_stage3 import seed_state


def _camera() -> Camera:
    return Camera(
        name="migration-camera",
        storage_folder_name="migration-camera",
        enabled=False,
        protocol="onvif",
        host="camera.example.test",
        port=80,
        username="operator",
        password_encrypted="encrypted-value",
        rtsp_main_url="rtsp://operator:redacted@camera.example.test:554/main",
        rtsp_sub_url="rtsp://operator:redacted@camera.example.test:554/sub",
        rtsp_host="camera.example.test",
        rtsp_port=554,
        rtsp_transport="tcp",
        onvif_path="/onvif/device_service",
        onvif_profile_token="main-token",
        onvif_sub_profile_token=None,
        recording_mode="always",
        default_live_stream="sub",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=30,
        storage_quota_gb=50,
        status="disabled",
    )


def test_canonical_v7_to_v8_stays_historical_then_v8_to_v9_adds_only_nullable_sub_token():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.add(_camera())
    db.commit()
    db.execute(text("ALTER TABLE cameras DROP COLUMN onvif_sub_profile_token"))
    db.execute(
        text(
            """
            CREATE TABLE recorder_runtime_status (
                recorder_instance_id VARCHAR(255) PRIMARY KEY,
                service_status VARCHAR(50) NOT NULL,
                loop_state VARCHAR(100) NULL,
                started_at TIMESTAMP NULL,
                heartbeat_at TIMESTAMP NOT NULL,
                active_jobs_count INTEGER DEFAULT 0 NOT NULL,
                recording_cameras_count INTEGER DEFAULT 0 NOT NULL,
                failed_cameras_count INTEGER DEFAULT 0 NOT NULL,
                last_error TEXT NULL,
                last_exit_code INTEGER NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
            """
        )
    )
    db.commit()
    seed_state(db, version=7)

    before_columns = {item["name"] for item in inspect(engine).get_columns("cameras")}
    before_row = db.execute(
        text("SELECT name, rtsp_main_url, rtsp_sub_url, onvif_profile_token FROM cameras")
    ).mappings().one()

    v8 = execute_migration_plan(
        db,
        registry=MigrationRegistry([STAGE660128_UNIVERSAL_SCHEMA_MIGRATION]),
    )
    v8_columns = {item["name"] for item in inspect(engine).get_columns("cameras")}

    assert v8["executed_migrations"] == [STAGE660128_UNIVERSAL_SCHEMA_MIGRATION.migration_id]
    assert "onvif_sub_profile_token" not in before_columns
    assert "onvif_sub_profile_token" not in v8_columns
    assert migration_definition_fingerprint(STAGE660128_UNIVERSAL_SCHEMA_MIGRATION) == (
        "997c36101dc217a0355e0308cf146b947feca0c882b7b07a3b912b4db982004a"
    )

    v9 = execute_migration_plan(
        db,
        registry=MigrationRegistry([STAGE13721_CAMERA_SUB_PROFILE_TOKEN_MIGRATION]),
    )
    v9_column = next(
        item for item in inspect(engine).get_columns("cameras")
        if item["name"] == "onvif_sub_profile_token"
    )
    after_row = db.execute(
        text(
            "SELECT name, rtsp_main_url, rtsp_sub_url, onvif_profile_token, "
            "onvif_sub_profile_token FROM cameras"
        )
    ).mappings().one()

    assert CURRENT_SCHEMA_VERSION == 9
    assert v9["executed_migrations"] == [STAGE13721_CAMERA_SUB_PROFILE_TOKEN_MIGRATION_ID]
    assert v9_column["nullable"] is True
    assert dict(before_row) == {key: after_row[key] for key in before_row}
    assert after_row["onvif_sub_profile_token"] is None
    assert STAGE13721_CAMERA_SUB_PROFILE_TOKEN_MIGRATION_ID in PREVIOUS_RUNTIME_COMPATIBLE_MIGRATION_IDS

    current = execute_migration_plan(
        db,
        registry=MigrationRegistry([STAGE13721_CAMERA_SUB_PROFILE_TOKEN_MIGRATION]),
    )
    assert current["executed_migrations"] == []

    db.close()
    engine.dispose()


class _SessionContext:
    def __enter__(self):
        return SimpleNamespace()

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_prestart_runner_delegates_to_existing_maintenance_and_emits_bounded_json(monkeypatch, capsys):
    monkeypatch.setattr(runner, "Session", lambda _engine: _SessionContext())
    called = []

    def fake_dry_run(_db):
        called.append("dry-run")
        return {
            "status": "pending",
            "reason": "Migration is ready.",
            "current_version": 8,
            "target_version": 9,
            "pending_count": 1,
            "migration_executed": False,
            "report": {
                "backup": {
                    "status": "ready",
                    "backup_root_status": "ready",
                    "backup_root_persistent": True,
                }
            },
        }

    monkeypatch.setattr(runner, "dry_run_migration_maintenance", fake_dry_run)

    assert runner.main(["dry-run"]) == 0
    output = capsys.readouterr().out
    assert called == ["dry-run"]
    assert '"current_version": 8' in output
    assert '"target_version": 9' in output
    assert '"persistent": true' in output
    assert "DATABASE_URL" not in output
    assert "rtsp://" not in output
    assert "ALTER TABLE" not in output
