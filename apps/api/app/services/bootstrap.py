from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import ROLE_OWNER
from app.db.session import Base, engine
from app.models import SystemSettings, User
from app.services.audit_log import create_event
from app.services.schema_migrations import validate_schema_migrations_pre_bootstrap
from app.services.schema_versioning import ensure_schema_version_state, inspect_schema_shape, validate_schema_version_pre_bootstrap
from app.services.system_settings import default_timezone


def init_db() -> None:
    pre_bootstrap_shape = inspect_schema_shape(engine)
    validate_schema_migrations_pre_bootstrap(engine)
    validate_schema_version_pre_bootstrap(engine, pre_bootstrap_shape)
    Base.metadata.create_all(bind=engine)
    migrate_user_table()
    migrate_system_settings_table()
    migrate_archive_roots()
    migrate_recording_metadata_tables()
    migrate_recorder_runtime_status()
    with Session(engine) as db:
        ensure_schema_version_state(db, pre_bootstrap_shape=pre_bootstrap_shape)


def migrate_system_settings_table() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("system_settings"):
        return
    columns = {column["name"] for column in inspector.get_columns("system_settings")}
    with engine.begin() as conn:
        if "system_name" not in columns:
            conn.execute(text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS system_name VARCHAR(100) NULL"))
        if "auto_free_space_cleanup_enabled" not in columns:
            conn.execute(text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS auto_free_space_cleanup_enabled BOOLEAN DEFAULT FALSE NOT NULL"))
        if "recording_suspended_by_low_disk" not in columns:
            conn.execute(text("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS recording_suspended_by_low_disk BOOLEAN DEFAULT FALSE NOT NULL"))


def migrate_user_table() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "is_active" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE NOT NULL"))
        if "updated_at" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL"))
        if "last_login_at" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP NULL"))


def migrate_recording_metadata_tables() -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        if inspector.has_table("recording_segments"):
            columns = {column["name"] for column in inspector.get_columns("recording_segments")}
            additions = {
                "job_id": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS job_id VARCHAR(36) NULL",
                "camera_name_snapshot": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS camera_name_snapshot VARCHAR(255) NULL",
                "camera_folder_snapshot": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS camera_folder_snapshot VARCHAR(255) NULL",
                "relative_path": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS relative_path VARCHAR(1024) NULL",
                "error_message": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS error_message TEXT NULL",
                "ownership": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS ownership VARCHAR(50) DEFAULT 'KM VMS' NOT NULL",
                "source": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'recorder' NOT NULL",
                "archive_root_id": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS archive_root_id VARCHAR(36) NULL",
                "checksum": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS checksum VARCHAR(128) NULL",
                "storage_namespace": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS storage_namespace VARCHAR(255) NULL",
                "container_format": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS container_format VARCHAR(32) NULL",
                "file_extension": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS file_extension VARCHAR(16) NULL",
                "mime_type": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS mime_type VARCHAR(100) NULL",
                "integrity_status": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS integrity_status VARCHAR(100) NULL",
                "integrity_error": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS integrity_error TEXT NULL",
                "last_integrity_check_at": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS last_integrity_check_at TIMESTAMP NULL",
                "file_size_verified_at": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS file_size_verified_at TIMESTAMP NULL",
                "file_mtime": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS file_mtime TIMESTAMP NULL",
                "content_probe_status": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS content_probe_status VARCHAR(100) NULL",
                "cleanup_candidate": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS cleanup_candidate BOOLEAN DEFAULT FALSE NULL",
                "cleanup_reason": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS cleanup_reason TEXT NULL",
                "reconciliation_status": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS reconciliation_status VARCHAR(100) NULL",
                "reconciliation_checked_at": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS reconciliation_checked_at TIMESTAMP NULL",
                "finalized_at": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMP NULL",
                "deleted_at": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL",
                "deletion_reason": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS deletion_reason TEXT NULL",
                "deleted_by": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255) NULL",
                "deletion_source": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS deletion_source VARCHAR(100) NULL",
                "created_at": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL",
                "updated_at": "ALTER TABLE recording_segments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL",
            }
            for column, statement in additions.items():
                if column not in columns:
                    conn.execute(text(statement))

            conn.execute(text("ALTER TABLE recording_segments ALTER COLUMN ended_at DROP NOT NULL"))
            conn.execute(text("ALTER TABLE recording_segments ALTER COLUMN duration_sec SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE recording_segments ALTER COLUMN size_bytes SET DEFAULT 0"))

            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_job_id ON recording_segments (job_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_status ON recording_segments (status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_ownership ON recording_segments (ownership)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_archive_root_id ON recording_segments (archive_root_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_relative_path ON recording_segments (relative_path)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_job_relative_path ON recording_segments (job_id, relative_path)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_integrity_status ON recording_segments (integrity_status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_reconciliation_status ON recording_segments (reconciliation_status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_segments_deleted_at ON recording_segments (deleted_at)"))

        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_jobs_camera_id ON recording_jobs (camera_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_jobs_state ON recording_jobs (state)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_jobs_started_at ON recording_jobs (started_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_jobs_ownership ON recording_jobs (ownership)"))


def migrate_archive_roots() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS archive_roots (
                    id VARCHAR(36) PRIMARY KEY,
                    label VARCHAR(255) NOT NULL,
                    root_path VARCHAR(1024) NOT NULL UNIQUE,
                    storage_namespace VARCHAR(255) NOT NULL DEFAULT 'kmvms/recordings',
                    is_active BOOLEAN DEFAULT FALSE NOT NULL,
                    is_readable BOOLEAN DEFAULT TRUE NOT NULL,
                    is_writable BOOLEAN DEFAULT TRUE NOT NULL,
                    is_available BOOLEAN DEFAULT TRUE NOT NULL,
                    problem TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    last_seen_at TIMESTAMP NULL,
                    retired_at TIMESTAMP NULL
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_archive_roots_is_active ON archive_roots (is_active)"))


def migrate_recorder_runtime_status() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS recorder_runtime_status (
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
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recorder_runtime_status_heartbeat_at ON recorder_runtime_status (heartbeat_at)"))


def ensure_system_settings(db: Session) -> SystemSettings:
    row = db.query(SystemSettings).order_by(SystemSettings.id.asc()).first()
    if row:
        return row

    has_users = db.query(User).count() > 0
    row = SystemSettings(
        system_initialized=has_users,
        system_name="KM VMS",
        timezone=default_timezone(),
        language="ru",
        storage_path=settings.storage_root,
        recording_format="mkv",
        auto_free_space_cleanup_enabled=False,
        recording_suspended_by_low_disk=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def ensure_owner_migration(db: Session) -> None:
    system = ensure_system_settings(db)
    if not system.system_initialized:
        return
    ensure_active_owner_fallback(db)


def ensure_active_owner_fallback(db: Session) -> None:
    if db.query(User).filter(User.role == ROLE_OWNER, User.is_active == True).first():  # noqa: E712
        return

    fallback = (
        db.query(User)
        .filter(User.is_active == True)  # noqa: E712
        .order_by(User.id.asc())
        .first()
    )
    if fallback is None:
        return

    previous_role = fallback.role
    fallback.role = ROLE_OWNER
    db.add(fallback)
    db.commit()
    create_event(
        db=db,
        actor=None,
        category="system",
        event_type="system.owner_fallback_promoted",
        severity="security",
        message_ru="Bootstrap promoted first active user to owner",
        message_en="Bootstrap promoted first active user to owner",
        target_type="user",
        target_id=fallback.id,
        target_name=fallback.username,
        metadata={
            "previous_role": previous_role,
            "new_role": ROLE_OWNER,
            "reason": "initialized_system_without_active_owner",
            "system_initialized": True,
        },
    )


def ensure_admin(db: Session) -> None:
    system = ensure_system_settings(db)
    if not system.system_initialized:
        return

    ensure_owner_migration(db)
