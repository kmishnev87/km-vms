from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import verify_password
from app.models.audit_event import AuditEvent
from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services.backup_before_upgrade import sanitize_error, verify_backup_manifest
from app.services.schema_migrations import build_migration_plan
from app.services.schema_versioning import CURRENT_STATE_ID, schema_version_status


RESTORE_VALIDATION_STATUS_VALIDATED = "restore_validated"
RESTORE_VALIDATION_STATUS_FAILED = "restore_validation_failed"
DISPOSABLE_DB_PREFIX = "kmvms_stage5_"
PRODUCT_TABLES = {
    "users",
    "cameras",
    "system_settings",
    "archive_roots",
    "recording_jobs",
    "recording_segments",
    "schema_version_state",
    "schema_migration_history",
    "audit_events",
}


class RestoreValidationBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("summary") or status)


@dataclass(frozen=True)
class RestoreValidationConfig:
    target_database_url: str
    validation_root: Path
    pg_restore_path: str = "pg_restore"
    allow_disposable_target: bool = False
    expected_owner_username: str | None = None
    expected_owner_password: str | None = None
    validation_id: str | None = None
    timeout_seconds: int = 300


def _utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_backup_manifest(path: Path) -> dict[str, Any]:
    try:
        return _read_json(path)
    except Exception as exc:
        raise RestoreValidationBlocked(
            "backup_manifest_invalid",
            {"status": "blocked", "summary": sanitize_error(exc)},
        ) from exc


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2, default=str), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _manifest_artifact_path(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    artifact_name = Path(str(manifest.get("backup_file_label") or "")).name
    if not artifact_name:
        raise RestoreValidationBlocked(
            "backup_manifest_invalid",
            {"status": "blocked", "summary": "Backup manifest does not reference a backup artifact."},
        )
    return manifest_path.with_name(artifact_name)


def _normalized_url(value: str) -> URL:
    try:
        return make_url(value)
    except Exception as exc:
        raise RestoreValidationBlocked(
            "target_database_url_invalid",
            {"status": "blocked", "summary": sanitize_error(exc)},
        ) from exc


def _safe_url_identity(url: URL) -> tuple[str | None, int | None, str | None, str | None]:
    return (url.host, url.port, url.database, url.username)


def _validate_target_database_url(target_url: URL, source_url: URL | None = None) -> None:
    backend = str(target_url.get_backend_name()).lower()
    if not backend.startswith("postgresql"):
        raise RestoreValidationBlocked(
            "restore_target_not_postgresql",
            {"status": "blocked", "summary": "Stage 5 restore validation only restores PostgreSQL custom-format backups."},
        )
    database = str(target_url.database or "")
    if not database.startswith(DISPOSABLE_DB_PREFIX):
        raise RestoreValidationBlocked(
            "restore_target_not_disposable",
            {
                "status": "blocked",
                "summary": "Restore validation target database must be a disposable Stage 5 database.",
                "required_prefix": DISPOSABLE_DB_PREFIX,
            },
        )
    if source_url is not None and _safe_url_identity(target_url) == _safe_url_identity(source_url):
        raise RestoreValidationBlocked(
            "restore_target_matches_source",
            {"status": "blocked", "summary": "Restore validation target must not be the source/current database."},
        )


def _assert_target_is_empty_or_disposable(target_url: URL) -> None:
    engine = create_engine(target_url)
    try:
        with engine.begin() as conn:
            tables = set(inspect(conn).get_table_names())
            product_tables = sorted(tables & PRODUCT_TABLES)
            if product_tables:
                raise RestoreValidationBlocked(
                    "restore_target_not_empty",
                    {
                        "status": "blocked",
                        "summary": "Restore validation target already contains KM VMS product tables.",
                        "tables": product_tables,
                    },
                )
    finally:
        engine.dispose()


def build_restore_validation_plan(
    manifest_path: str | Path,
    *,
    config: RestoreValidationConfig,
    source_database_url: str | None = None,
) -> dict[str, Any]:
    target_url = _normalized_url(config.target_database_url)
    source_url = _normalized_url(source_database_url) if source_database_url else None
    manifest_path = Path(manifest_path)
    manifest = _read_backup_manifest(manifest_path)
    verification = verify_backup_manifest(manifest_path)
    artifact_path = _manifest_artifact_path(manifest_path, manifest)
    target_status = "not_checked"
    if config.allow_disposable_target:
        _validate_target_database_url(target_url, source_url)
        _assert_target_is_empty_or_disposable(target_url)
        target_status = "disposable_empty"
    return {
        "status": "planned",
        "mutates_production_database": False,
        "restore_target_classification": "disposable_postgresql" if target_status == "disposable_empty" else "requires_explicit_disposable_target",
        "restore_target_status": target_status,
        "backup_id": manifest.get("backup_id"),
        "backup_checksum_sha256": manifest.get("checksum_sha256"),
        "backup_manifest_valid": bool(verification.get("valid")),
        "backup_artifact_present": artifact_path.exists(),
        "backup_file_label": manifest.get("backup_file_label"),
        "original_backup_mutated": False,
        "video_archive_files_restored": False,
    }


def _pg_restore_command(url: URL, artifact_path: Path, pg_restore_path: str) -> tuple[list[str], dict[str, str]]:
    cmd = [pg_restore_path, "--no-owner", "--no-privileges"]
    if url.host:
        cmd.extend(["--host", url.host])
    if url.port:
        cmd.extend(["--port", str(url.port)])
    if url.username:
        cmd.extend(["--username", url.username])
    if url.database:
        cmd.extend(["--dbname", url.database])
    cmd.append(str(artifact_path))
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    return cmd, env


def _restore_postgres_custom_dump(artifact_path: Path, target_url: URL, config: RestoreValidationConfig) -> None:
    cmd, env = _pg_restore_command(target_url, artifact_path, config.pg_restore_path)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=config.timeout_seconds, check=False)
    if result.returncode != 0:
        output = result.stderr or result.stdout or ""
        benign_transaction_timeout_warning = (
            'unrecognized configuration parameter "transaction_timeout"' in output
            and "errors ignored on restore: 1" in output
        )
        if benign_transaction_timeout_warning:
            return
        raise RestoreValidationBlocked(
            "pg_restore_failed",
            {"status": RESTORE_VALIDATION_STATUS_FAILED, "summary": sanitize_error(output or "pg_restore failed")},
        )


def _count(db: Session, model: Any) -> int:
    return int(db.query(model).count())


def _validate_restored_state(db: Session, *, config: RestoreValidationConfig) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    owner = None
    if config.expected_owner_username:
        owner = db.query(User).filter(User.username == config.expected_owner_username).first()
    if owner is None:
        owner = db.query(User).filter(User.role == "owner").order_by(User.id.asc()).first()
    checks["owner_user"] = {
        "passed": bool(owner and owner.is_active and owner.role in {"owner", "admin"}),
        "username": owner.username if owner else None,
        "role": owner.role if owner else None,
    }
    if config.expected_owner_password and owner:
        checks["owner_login_contract"] = {
            "passed": bool(verify_password(config.expected_owner_password, owner.password_hash)),
            "method": "password_hash_verify_backend_auth_contract",
        }
    else:
        checks["owner_login_contract"] = {"passed": bool(owner), "method": "row_presence_only"}

    state = db.get(SchemaVersionState, CURRENT_STATE_ID)
    history_count = _count(db, SchemaMigrationHistory)
    checks["schema_version"] = {
        "passed": bool(state and history_count >= 1 and schema_version_status(db).get("status") in {"current", "managed"}),
        "status": schema_version_status(db).get("status"),
        "history_count": history_count,
    }
    plan = build_migration_plan(db)
    checks["migration_plan_readable"] = {"passed": bool(plan.get("status")), "status": plan.get("status")}
    checks["users"] = {"passed": _count(db, User) >= 1, "count": _count(db, User)}
    checks["cameras"] = {"passed": _count(db, Camera) >= 1, "count": _count(db, Camera)}
    settings_row = db.query(SystemSettings).first()
    checks["system_settings"] = {
        "passed": bool(settings_row and settings_row.storage_path),
        "timezone": settings_row.timezone if settings_row else None,
        "recording_format": settings_row.recording_format if settings_row else None,
        "storage_path_label": "storage_path_redacted" if settings_row else None,
    }
    finalized_segments = (
        db.query(RecordingSegment)
        .filter(RecordingSegment.relative_path.isnot(None), RecordingSegment.status.in_(["ready", "finalized"]))
        .count()
    )
    checks["recording_metadata"] = {
        "passed": _count(db, ArchiveRoot) >= 1 and _count(db, RecordingJob) >= 1 and finalized_segments >= 1,
        "archive_roots": _count(db, ArchiveRoot),
        "recording_jobs": _count(db, RecordingJob),
        "segments": _count(db, RecordingSegment),
        "records_chronology_metadata_read_path": "metadata_query_without_video_files",
    }
    recorder_owned = (
        db.query(RecordingSegment)
        .filter(RecordingSegment.ownership == "KM VMS", RecordingSegment.source == "recorder")
        .count()
    )
    checks["recorder_ownership"] = {"passed": recorder_owned >= 1, "recorder_owned_segments": int(recorder_owned)}
    checks["audit_summary"] = {"passed": _count(db, AuditEvent) >= 1, "count": _count(db, AuditEvent)}
    return checks


def _all_checks_passed(checks: dict[str, Any]) -> bool:
    return all(bool(item.get("passed")) for item in checks.values() if isinstance(item, dict))


def run_restore_validation(
    manifest_path: str | Path,
    *,
    config: RestoreValidationConfig,
    source_database_url: str | None = None,
) -> dict[str, Any]:
    if not config.allow_disposable_target:
        raise RestoreValidationBlocked(
            "restore_validation_requires_disposable_opt_in",
            {"status": "blocked", "summary": "Restore validation requires explicit disposable target opt-in."},
        )
    manifest_path = Path(manifest_path)
    manifest = _read_backup_manifest(manifest_path)
    verification = verify_backup_manifest(manifest_path)
    if not verification.get("valid"):
        raise RestoreValidationBlocked(
            "backup_manifest_invalid",
            {"status": "blocked", "summary": verification.get("summary") or "Backup manifest is invalid."},
        )
    artifact_path = _manifest_artifact_path(manifest_path, manifest)
    if not artifact_path.exists() or _sha256(artifact_path) != manifest.get("checksum_sha256"):
        raise RestoreValidationBlocked(
            "backup_artifact_invalid",
            {"status": "blocked", "summary": "Backup artifact is missing or checksum does not match manifest."},
        )

    target_url = _normalized_url(config.target_database_url)
    source_url = _normalized_url(source_database_url) if source_database_url else None
    _validate_target_database_url(target_url, source_url)
    _assert_target_is_empty_or_disposable(target_url)

    validation_id = config.validation_id or f"kmvms-restore-validation-{uuid.uuid4().hex[:12]}"
    config.validation_root.mkdir(parents=True, exist_ok=True)
    _restore_postgres_custom_dump(artifact_path, target_url, config)

    engine = create_engine(target_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        checks = _validate_restored_state(db, config=config)
        passed = _all_checks_passed(checks)
        status = RESTORE_VALIDATION_STATUS_VALIDATED if passed else RESTORE_VALIDATION_STATUS_FAILED
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS restore_validation_disposable_marker (validation_id text primary key, created_at timestamp)"))
            conn.execute(
                text("INSERT INTO restore_validation_disposable_marker (validation_id, created_at) VALUES (:validation_id, now()) ON CONFLICT DO NOTHING"),
                {"validation_id": validation_id},
            )
    finally:
        db.close()
        engine.dispose()

    restore_manifest = {
        "validation_id": validation_id,
        "created_at": _utc_iso(),
        "status": status,
        "backup_restore_validated": bool(passed),
        "backup_id": manifest.get("backup_id"),
        "backup_checksum_sha256": manifest.get("checksum_sha256"),
        "backup_file_label": manifest.get("backup_file_label"),
        "backup_manifest_label": manifest_path.name,
        "restore_target_classification": "disposable_postgresql",
        "restore_tool": "pg_restore_custom_format",
        "checks": checks,
        "original_backup_mutated": False,
        "production_database_mutated": False,
        "video_archive_files_restored": False,
        "video_archive_restore_status": "not_covered_metadata_only",
    }
    restore_manifest_path = config.validation_root / f"{validation_id}.restore-validation.json"
    _write_json_atomic(restore_manifest_path, restore_manifest)
    return {**restore_manifest, "restore_manifest_path": str(restore_manifest_path)}


def backup_restore_validated(restore_manifest_path: str | Path, backup_manifest_path: str | Path) -> dict[str, Any]:
    try:
        restore_manifest = _read_json(Path(restore_manifest_path))
        backup_manifest = _read_json(Path(backup_manifest_path))
    except Exception as exc:
        return {"valid": False, "status": "invalid", "summary": sanitize_error(exc)}
    backup_id_matches = restore_manifest.get("backup_id") == backup_manifest.get("backup_id")
    checksum_matches = restore_manifest.get("backup_checksum_sha256") == backup_manifest.get("checksum_sha256")
    valid = (
        restore_manifest.get("status") == RESTORE_VALIDATION_STATUS_VALIDATED
        and restore_manifest.get("backup_restore_validated") is True
        and backup_id_matches
        and checksum_matches
    )
    return {
        "valid": bool(valid),
        "status": RESTORE_VALIDATION_STATUS_VALIDATED if valid else "invalid",
        "backup_id": restore_manifest.get("backup_id"),
        "checksum_matches": bool(checksum_matches),
        "backup_id_matches": bool(backup_id_matches),
        "video_archive_restore_status": restore_manifest.get("video_archive_restore_status"),
    }
