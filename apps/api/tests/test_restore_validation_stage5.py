import json
import os
import secrets
import shutil
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import hash_password
from app.db.session import Base
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services.backup_before_upgrade import BackupExecutionConfig, create_backup_before_upgrade
from app.services.restore_validation import (
    DISPOSABLE_DB_PREFIX,
    RESTORE_VALIDATION_STATUS_VALIDATED,
    RestoreValidationBlocked,
    RestoreValidationConfig,
    backup_restore_validated,
    build_restore_validation_plan,
    run_restore_validation,
)
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION
from test_schema_migration_runner_stage3 import seed_state


POSTGRES_URL = os.getenv("KMVMS_STAGE2_POSTGRES_URL") or os.getenv("KMVMS_STAGE3_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL or not shutil.which("pg_dump") or not shutil.which("pg_restore"),
    reason="Disposable PostgreSQL URL, pg_dump and pg_restore are required for Stage 5 restore validation",
)


def _admin_url():
    url = make_url(POSTGRES_URL)
    return url.set(database=url.database or "postgres")


def _db_url(name: str):
    return make_url(POSTGRES_URL).set(database=name)


def _url_string(url) -> str:
    return url.render_as_string(hide_password=False)


def _create_database(name: str) -> None:
    engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


def _drop_database(name: str) -> None:
    engine = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


@pytest.fixture()
def disposable_databases():
    suffix = uuid.uuid4().hex
    source_name = f"{DISPOSABLE_DB_PREFIX}source_{suffix}"
    target_name = f"{DISPOSABLE_DB_PREFIX}restore_{suffix}"
    owner_password = secrets.token_urlsafe(24)
    _create_database(source_name)
    _create_database(target_name)
    try:
        yield _url_string(_db_url(source_name)), _url_string(_db_url(target_name)), owner_password
    finally:
        _drop_database(target_name)
        _drop_database(source_name)


def _seed_representative_source(db, owner_password: str):
    seed_state(db, version=CURRENT_SCHEMA_VERSION)
    owner = User(
        username="stage5_owner",
        full_name="Stage 5 Owner",
        password_hash=hash_password(owner_password),
        role="owner",
        is_active=True,
    )
    db.add(owner)
    db.flush()
    camera = Camera(
        name="Stage 5 Safe Camera",
        storage_folder_name="stage5-safe-camera",
        enabled=True,
        protocol="rtsp",
        host="198.51.100.10",
        port=554,
        username=None,
        password_encrypted=None,
        rtsp_main_url=None,
        rtsp_sub_url=None,
        recording_mode="always",
        default_live_stream="sub",
        default_record_stream="main",
        segment_minutes=5,
        retention_days=7,
        storage_quota_gb=10,
        status="online",
    )
    settings = SystemSettings(
        system_initialized=True,
        timezone="UTC",
        language="ru",
        storage_path="/storage/archive",
        recording_format="mkv",
        auto_free_space_cleanup_enabled=True,
    )
    archive_root = ArchiveRoot(
        id=str(uuid.uuid4()),
        label="Stage 5 Archive Root",
        root_path="/storage/archive",
        storage_namespace="kmvms/recordings",
        is_active=True,
        is_readable=True,
        is_writable=True,
        is_available=True,
    )
    db.add_all([camera, settings, archive_root])
    db.flush()
    job = RecordingJob(
        id=str(uuid.uuid4()),
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        state="stopped",
        source_stream="main",
        input_fingerprint="stage5-safe-fingerprint",
        recorder_instance_id="stage5-disposable-recorder",
        started_at=datetime.utcnow() - timedelta(minutes=10),
        stopped_at=datetime.utcnow() - timedelta(minutes=5),
        stop_reason="stage5_disposable_complete",
        created_by="KM VMS",
        ownership="KM VMS",
        source="recorder",
    )
    segment = RecordingSegment(
        job_id=job.id,
        camera_id=camera.id,
        camera_name_snapshot=camera.name,
        camera_folder_snapshot=camera.storage_folder_name,
        file_path="/storage/archive/stage5-safe-camera/2026/05/07/segment-0001.mkv",
        relative_path="stage5-safe-camera/2026/05/07/segment-0001.mkv",
        started_at=datetime.utcnow() - timedelta(minutes=10),
        ended_at=datetime.utcnow() - timedelta(minutes=5),
        duration_sec=300,
        size_bytes=123456,
        stream_type="main",
        status="finalized",
        ownership="KM VMS",
        source="recorder",
        archive_root_id=archive_root.id,
        storage_namespace=archive_root.storage_namespace,
        container_format="matroska",
        file_extension=".mkv",
        mime_type="video/x-matroska",
        integrity_status="ok",
        finalized_at=datetime.utcnow() - timedelta(minutes=5),
    )
    audit = AuditEvent(
        id=str(uuid.uuid4()),
        actor_user_id=owner.id,
        actor_username=owner.username,
        actor_role=owner.role,
        category="system",
        event_type="stage5.restore_validation_seeded",
        severity="info",
        message_ru="Stage 5 disposable restore validation seed",
        message_en="Stage 5 disposable restore validation seed",
        target_type="database",
        target_name="stage5_disposable",
        event_metadata={"scope": "restore_validation", "contains_secret": False},
    )
    db.add_all([job, segment, audit])
    db.commit()


def _seed_backup(source_url: str, tmp_path: Path, owner_password: str) -> dict:
    engine = create_engine(source_url)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        Base.metadata.create_all(bind=engine)
        _seed_representative_source(db, owner_password)
        return create_backup_before_upgrade(
            db,
            config=BackupExecutionConfig(backup_root=tmp_path / "stage5-backups", allow_tmp_for_tests=True, source="stage5_disposable_test"),
        )
    finally:
        db.close()
        engine.dispose()


def test_restore_validation_plan_is_read_only_and_requires_disposable_opt_in(disposable_databases, tmp_path):
    source_url, target_url, owner_password = disposable_databases
    backup = _seed_backup(source_url, tmp_path, owner_password)

    plan = build_restore_validation_plan(
        backup["manifest_path"],
        config=RestoreValidationConfig(target_database_url=target_url, validation_root=tmp_path / "validation"),
        source_database_url=source_url,
    )

    assert plan["status"] == "planned"
    assert plan["restore_target_status"] == "not_checked"
    assert plan["original_backup_mutated"] is False
    assert plan["video_archive_files_restored"] is False


def test_restore_validation_rejects_non_disposable_or_source_target(disposable_databases, tmp_path):
    source_url, _target_url, owner_password = disposable_databases
    backup = _seed_backup(source_url, tmp_path, owner_password)
    non_disposable = _url_string(make_url(source_url).set(database="kmvms_live_like"))

    with pytest.raises(RestoreValidationBlocked) as no_opt_in:
        run_restore_validation(
            backup["manifest_path"],
            config=RestoreValidationConfig(target_database_url=source_url, validation_root=tmp_path / "validation"),
            source_database_url=source_url,
        )
    assert no_opt_in.value.status == "restore_validation_requires_disposable_opt_in"

    with pytest.raises(RestoreValidationBlocked) as same_db:
        run_restore_validation(
            backup["manifest_path"],
            config=RestoreValidationConfig(
                target_database_url=source_url,
                validation_root=tmp_path / "validation",
                allow_disposable_target=True,
            ),
            source_database_url=source_url,
        )
    assert same_db.value.status == "restore_target_matches_source"

    with pytest.raises(RestoreValidationBlocked) as non_disposable_block:
        run_restore_validation(
            backup["manifest_path"],
            config=RestoreValidationConfig(
                target_database_url=non_disposable,
                validation_root=tmp_path / "validation",
                allow_disposable_target=True,
            ),
            source_database_url=source_url,
        )
    assert non_disposable_block.value.status == "restore_target_not_disposable"


def test_restore_validation_rejects_disposable_target_with_existing_product_tables(disposable_databases, tmp_path):
    source_url, target_url, owner_password = disposable_databases
    backup = _seed_backup(source_url, tmp_path, owner_password)
    target_engine = create_engine(target_url)
    try:
        Base.metadata.create_all(bind=target_engine)
    finally:
        target_engine.dispose()

    with pytest.raises(RestoreValidationBlocked) as blocked:
        run_restore_validation(
            backup["manifest_path"],
            config=RestoreValidationConfig(
                target_database_url=target_url,
                validation_root=tmp_path / "validation",
                allow_disposable_target=True,
            ),
            source_database_url=source_url,
        )

    assert blocked.value.status == "restore_target_not_empty"


def test_restore_validation_blocks_invalid_manifest_and_corrupt_artifact(disposable_databases, tmp_path):
    source_url, target_url, owner_password = disposable_databases
    backup = _seed_backup(source_url, tmp_path, owner_password)
    invalid_manifest = tmp_path / "invalid.manifest.json"
    invalid_manifest.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RestoreValidationBlocked) as invalid:
        build_restore_validation_plan(
            invalid_manifest,
            config=RestoreValidationConfig(target_database_url=target_url, validation_root=tmp_path / "validation"),
            source_database_url=source_url,
        )
    assert invalid.value.status == "backup_manifest_invalid"

    backup_path = Path(backup["backup_file_path"])
    backup_path.write_bytes(b"corrupt stage5 backup artifact")
    with pytest.raises(RestoreValidationBlocked) as corrupt:
        run_restore_validation(
            backup["manifest_path"],
            config=RestoreValidationConfig(
                target_database_url=target_url,
                validation_root=tmp_path / "validation",
                allow_disposable_target=True,
            ),
            source_database_url=source_url,
        )

    assert corrupt.value.status == "backup_manifest_invalid"


def test_postgres_backup_restores_to_disposable_target_and_validates_state(disposable_databases, tmp_path):
    source_url, target_url, owner_password = disposable_databases
    backup = _seed_backup(source_url, tmp_path, owner_password)

    result = run_restore_validation(
        backup["manifest_path"],
        config=RestoreValidationConfig(
            target_database_url=target_url,
            validation_root=tmp_path / "validation",
            allow_disposable_target=True,
            expected_owner_username="stage5_owner",
            expected_owner_password=owner_password,
            validation_id="stage5-restore-validation-test",
        ),
        source_database_url=source_url,
    )

    assert result["status"] == RESTORE_VALIDATION_STATUS_VALIDATED
    assert result["backup_restore_validated"] is True
    assert result["production_database_mutated"] is False
    assert result["video_archive_files_restored"] is False
    assert result["checks"]["owner_login_contract"]["passed"] is True
    assert result["checks"]["cameras"]["count"] == 1
    assert result["checks"]["recording_metadata"]["records_chronology_metadata_read_path"] == "metadata_query_without_video_files"
    assert backup_restore_validated(result["restore_manifest_path"], backup["manifest_path"])["valid"] is True
    manifest = json.loads(Path(result["restore_manifest_path"]).read_text(encoding="utf-8"))
    assert owner_password not in json.dumps(manifest)
    assert manifest["video_archive_restore_status"] == "not_covered_metadata_only"
