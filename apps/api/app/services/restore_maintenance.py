from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.schema_version import SchemaVersionState
from app.models.user import User
from app.services.backup_before_upgrade import (
    DEFAULT_BACKUP_ROOT,
    BackupExecutionConfig,
    BackupSafetyBlocked,
    create_backup_before_upgrade,
    sanitize_error,
    verify_backup_manifest,
)
from app.services.restore_validation import RestoreValidationBlocked, RestoreValidationConfig, run_restore_validation
from app.services.schema_migrations import build_migration_plan
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION, CURRENT_STATE_ID, schema_version_status


RESTORE_REPORT_VERSION = "stage13.restore_rollback.v1"
TARGET_TEMPORARY_VALIDATION_DB = "temporary_validation_db"
TARGET_CURRENT_PRODUCT_DB = "current_product_db"
TARGET_KINDS = {TARGET_TEMPORARY_VALIDATION_DB, TARGET_CURRENT_PRODUCT_DB}
TEMPORARY_VALIDATION_DB_PREFIX = "kmvms_stage5_stage13_restore_validation_"
BACKUP_ARTIFACT_ID_RE = re.compile(r"^kmvms-db-\d{8}T\d{6}Z-[a-f0-9]{12}$")
SENSITIVE_RE = re.compile(
    r"(password|passwd|secret|token|authorization|jwt|rtsp://|postgresql://|sqlite:///)[^,\s\"']*",
    re.IGNORECASE,
)


class RestoreMaintenanceBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("reason") or diagnostics.get("summary") or status)


def _utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sanitize(value: Any, limit: int = 500) -> str:
    return SENSITIVE_RE.sub("redacted=***", str(value or ""))[:limit]


def _safe_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(item) for item in value]
    if isinstance(value, str):
        return _sanitize(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize(value)


def _backup_root(backup_root: str | None = None) -> Path:
    return Path(backup_root or os.getenv("KMVMS_DB_BACKUP_ROOT") or settings.kmvms_db_backup_root or DEFAULT_BACKUP_ROOT)


def _manifest_paths(root: Path) -> list[Path]:
    try:
        return sorted(root.expanduser().resolve().glob("*.manifest.json"))
    except Exception:
        return []


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_id(manifest: dict[str, Any], manifest_path: Path) -> str:
    backup_id = str(manifest.get("backup_id") or "").strip()
    return backup_id or manifest_path.name.removesuffix(".manifest.json")


def _parse_backup_created_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _backup_timestamp_from_name(path: Path) -> datetime | None:
    match = re.search(r"kmvms-db-(\d{8}T\d{6})Z-[a-f0-9]{12}", path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except Exception:
        return None


def _manifest_sort_key(path: Path) -> tuple[float, str]:
    created_at: datetime | None = None
    try:
        manifest = _read_json(path)
        created_at = _parse_backup_created_at(manifest.get("created_at"))
    except Exception:
        created_at = None
    created_at = created_at or _backup_timestamp_from_name(path)
    if created_at:
        return (created_at.timestamp(), path.name)
    try:
        return (path.stat().st_mtime, path.name)
    except Exception:
        return (0.0, path.name)


def _newest_manifest_paths(root: Path, *, limit: int) -> list[Path]:
    safe_limit = max(0, min(int(limit or 0), 100))
    paths = _manifest_paths(root)
    return sorted(paths, key=_manifest_sort_key, reverse=True)[:safe_limit]


def _manifest_for_artifact(artifact_id: str, *, backup_root: str | None = None) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{6,160}", artifact_id or ""):
        raise RestoreMaintenanceBlocked("artifact_invalid", {"status": "blocked", "reason": "Restore artifact reference is invalid."})
    root = _backup_root(backup_root)
    for manifest_path in _manifest_paths(root):
        try:
            manifest = _read_json(manifest_path)
        except Exception:
            continue
        if artifact_id in {_artifact_id(manifest, manifest_path), manifest_path.name}:
            return manifest_path, manifest
    raise RestoreMaintenanceBlocked("artifact_not_found", {"status": "blocked", "reason": "Restore artifact was not found."})


def _validate_product_backup_artifact_id(artifact_id: str) -> str:
    value = str(artifact_id or "").strip()
    if not BACKUP_ARTIFACT_ID_RE.fullmatch(value):
        raise RestoreMaintenanceBlocked(
            "artifact_invalid",
            {"status": "blocked", "reason": "Backup artifact reference is invalid.", "artifact_id_accepted": False},
        )
    return value


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _owned_backup_artifact_paths(artifact_id: str, *, backup_root: str | None = None) -> tuple[Path, dict[str, Any], list[Path], list[str]]:
    safe_id = _validate_product_backup_artifact_id(artifact_id)
    root = _backup_root(backup_root).expanduser().resolve()
    manifest_path = root / f"{safe_id}.manifest.json"
    if not manifest_path.exists():
        raise RestoreMaintenanceBlocked("artifact_not_found", {"status": "blocked", "reason": "Backup artifact was not found."})
    if manifest_path.is_symlink():
        raise RestoreMaintenanceBlocked("artifact_unsafe", {"status": "blocked", "reason": "Backup manifest ownership evidence is unsafe."})
    manifest = _read_json(manifest_path)
    if manifest.get("backup_id") != safe_id:
        raise RestoreMaintenanceBlocked("artifact_unsafe", {"status": "blocked", "reason": "Backup artifact ownership evidence is incomplete."})

    labels = [
        f"{safe_id}.manifest.json",
        Path(str(manifest.get("backup_file_label") or "")).name,
        Path(str(manifest.get("metadata_file_label") or "")).name,
    ]
    if not labels[1] or not labels[2]:
        raise RestoreMaintenanceBlocked("artifact_unsafe", {"status": "blocked", "reason": "Backup artifact ownership evidence is incomplete."})
    if labels[1] not in {f"{safe_id}.dump", f"{safe_id}.sqlite3"} or labels[2] != f"{safe_id}.metadata.json":
        raise RestoreMaintenanceBlocked("artifact_unsafe", {"status": "blocked", "reason": "Backup artifact file labels are not product-owned."})

    paths: list[Path] = []
    missing: list[str] = []
    for label in labels:
        candidate = (root / label).resolve()
        if not _inside_root(candidate, root):
            raise RestoreMaintenanceBlocked("artifact_unsafe", {"status": "blocked", "reason": "Backup artifact is outside the configured backup root."})
        if candidate.is_symlink():
            raise RestoreMaintenanceBlocked("artifact_unsafe", {"status": "blocked", "reason": "Backup artifact ownership evidence is unsafe."})
        if candidate.exists():
            paths.append(candidate)
        else:
            missing.append(label)
    return manifest_path, manifest, paths, missing


def _schema_version_from_manifest(manifest: dict[str, Any]) -> int | None:
    schema = manifest.get("schema_version")
    if isinstance(schema, dict):
        value = schema.get("schema_version")
    else:
        value = schema
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _artifact_summary(manifest_path: Path, manifest: dict[str, Any], verification: dict[str, Any] | None = None) -> dict[str, Any]:
    artifact_id = _artifact_id(manifest, manifest_path)
    deletable = bool(BACKUP_ARTIFACT_ID_RE.fullmatch(artifact_id or ""))
    return {
        "artifact_id": artifact_id,
        "artifact_label": manifest_path.name,
        "artifact_created_at": _sanitize(manifest.get("created_at"), 80) if manifest.get("created_at") else None,
        "artifact_schema_version": _schema_version_from_manifest(manifest),
        "db_backend": _sanitize(manifest.get("db_backend"), 40),
        "file_size": manifest.get("file_size"),
        "validation_status": (verification or {}).get("status"),
        "valid": bool((verification or {}).get("valid")),
        "deletable": deletable,
        "delete_supported": deletable,
    }


def list_restore_artifacts(*, backup_root: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for manifest_path in _newest_manifest_paths(_backup_root(backup_root), limit=limit):
        try:
            manifest = _read_json(manifest_path)
            verification = verify_backup_manifest(manifest_path)
            items.append(_artifact_summary(manifest_path, manifest, verification))
        except Exception:
            items.append(
                {
                    "artifact_id": manifest_path.name,
                    "artifact_label": manifest_path.name,
                    "valid": False,
                    "validation_status": "invalid",
                    "deletable": False,
                    "delete_supported": False,
                }
            )
    return items


def delete_backup_artifact(
    *,
    artifact_id: str,
    confirm: bool,
    backup_root: str | None = None,
    actor: Any = None,
) -> dict[str, Any]:
    if not confirm:
        raise RestoreMaintenanceBlocked(
            "confirmation_required",
            {"status": "blocked", "reason": "Explicit confirm=true is required for backup artifact deletion.", "deleted": False},
        )
    safe_id = _validate_product_backup_artifact_id(artifact_id)
    manifest_path, manifest, paths, missing = _owned_backup_artifact_paths(safe_id, backup_root=backup_root)
    deleted_labels: list[str] = []
    failed_labels: list[str] = []
    for path in sorted(paths, key=lambda item: 0 if item == manifest_path else 1, reverse=True):
        try:
            label = path.name
            path.unlink()
            deleted_labels.append(label)
        except Exception:
            failed_labels.append(path.name)
    if failed_labels:
        raise RestoreMaintenanceBlocked(
            "delete_failed",
            {
                "status": "blocked",
                "reason": "Backup artifact could not be fully deleted.",
                "artifact_id": safe_id,
                "deleted_count": len(deleted_labels),
                "failed_count": len(failed_labels),
                "deleted": False,
            },
        )
    return {
        "status": "deleted_with_missing_files" if missing else "deleted",
        "artifact_id": safe_id,
        "deleted": True,
        "deleted_count": len(deleted_labels),
        "missing_count": len(missing),
        "db_backend": _sanitize(manifest.get("db_backend"), 40),
        "video_archive_files_deleted": False,
        "actor": {
            "user_id": getattr(actor, "id", None),
            "role": _sanitize(getattr(actor, "role", None), 50) if getattr(actor, "role", None) else None,
        },
    }


def _compatibility(manifest: dict[str, Any]) -> dict[str, Any]:
    version = _schema_version_from_manifest(manifest)
    if version is None:
        return {"status": "blocked", "reason": "Backup artifact has no schema version metadata.", "artifact_schema_version": None}
    if version > CURRENT_SCHEMA_VERSION:
        return {"status": "blocked", "reason": "Backup artifact schema version is newer than this app supports.", "artifact_schema_version": version}
    if version < CURRENT_SCHEMA_VERSION:
        return {
            "status": "blocked",
            "reason": "Backup artifact schema version is older; restore requires explicit Stage 2 migration apply after restore and is blocked here.",
            "artifact_schema_version": version,
        }
    return {"status": "compatible", "reason": "Backup artifact schema version is compatible.", "artifact_schema_version": version}


def _target_status(target_kind: str, *, target_database_url: str | None = None, current_db_url: URL | None = None) -> dict[str, Any]:
    if target_kind not in TARGET_KINDS:
        return {"status": "blocked", "reason": "Unknown restore target kind.", "target_kind": target_kind}
    if target_kind == TARGET_CURRENT_PRODUCT_DB:
        return {
            "status": "blocked",
            "reason": "current_product_restore_not_enabled",
            "target_kind": target_kind,
            "current_product_restore_supported": False,
            "current_product_restore_status": "blocked",
            "current_product_restore_reason": "current_product_restore_not_enabled",
            "requires_explicit_future_enablement": True,
            "requires_current_backup": True,
        }
    if not target_database_url:
        backend = str(current_db_url.get_backend_name()).lower() if current_db_url is not None else ""
        if backend.startswith("postgresql"):
            return {
                "status": "safe",
                "reason": "server_side_disposable_validation_target_planned",
                "target_kind": target_kind,
                "temporary_validation_restore_supported": True,
                "temporary_validation_target": "server_side_disposable_postgresql",
                "requires_current_backup": False,
            }
        return {
            "status": "blocked",
            "reason": "temporary_validation_target_not_available",
            "target_kind": target_kind,
            "temporary_validation_restore_supported": False,
            "temporary_validation_target": "not_available",
            "requires_current_backup": False,
        }
    try:
        target_url = make_url(target_database_url)
    except Exception as exc:
        return {"status": "blocked", "reason": sanitize_error(exc), "target_kind": target_kind}
    if current_db_url is not None and (
        target_url.host,
        target_url.port,
        target_url.database,
        target_url.username,
    ) == (
        current_db_url.host,
        current_db_url.port,
        current_db_url.database,
        current_db_url.username,
    ):
        return {"status": "blocked", "reason": "Temporary validation target must not match current DB.", "target_kind": target_kind}
    if str(target_url.get_backend_name()).lower().startswith("sqlite"):
        database = Path(str(target_url.database or ""))
        if not database:
            return {"status": "blocked", "reason": "Temporary SQLite target path is invalid.", "target_kind": target_kind}
        if database.exists():
            try:
                engine = create_engine(target_url)
                with engine.begin() as conn:
                    if inspect(conn).get_table_names():
                        return {"status": "blocked", "reason": "Temporary validation target is not empty.", "target_kind": target_kind}
            finally:
                engine.dispose()
        return {"status": "safe", "reason": "Temporary validation SQLite target is isolated.", "target_kind": target_kind, "requires_current_backup": False}
    if str(target_url.get_backend_name()).lower().startswith("postgresql"):
        database = str(target_url.database or "")
        if not database.startswith("kmvms_stage5_") and not database.startswith("stage13_restore_test_"):
            return {"status": "blocked", "reason": "Temporary PostgreSQL validation target is not disposable.", "target_kind": target_kind}
        return {"status": "safe", "reason": "Temporary validation PostgreSQL target is disposable.", "target_kind": target_kind, "requires_current_backup": False}
    return {"status": "blocked", "reason": "Unsupported temporary validation target backend.", "target_kind": target_kind}


def _quote_pg_identifier(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", name):
        raise RestoreMaintenanceBlocked("temporary_validation_target_invalid", {"status": "blocked", "reason": "Generated validation target name is invalid."})
    return '"' + name.replace('"', '""') + '"'


def _admin_postgres_url(current_url: URL) -> URL:
    return current_url.set(database="postgres" if current_url.database != "postgres" else "template1")


def _create_server_side_disposable_target(current_url: URL) -> dict[str, Any]:
    backend = str(current_url.get_backend_name()).lower()
    if not backend.startswith("postgresql"):
        raise RestoreMaintenanceBlocked(
            "temporary_validation_target_not_available",
            {"status": "blocked", "reason": "temporary_validation_target_not_available"},
        )
    database_name = f"{TEMPORARY_VALIDATION_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    target_url = current_url.set(database=database_name)
    admin_engine = create_engine(_admin_postgres_url(current_url), isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.begin() as conn:
            conn.execute(text(f"CREATE DATABASE {_quote_pg_identifier(database_name)}"))
    except Exception as exc:
        raise RestoreMaintenanceBlocked(
            "temporary_validation_target_create_failed",
            {"status": "blocked", "reason": _sanitize(exc)},
        ) from exc
    finally:
        admin_engine.dispose()
    return {
        "target_database_url": target_url.render_as_string(hide_password=False),
        "database_name": database_name,
        "target_label": "server_side_disposable_validation_db",
        "cleanup": {"attempted": False, "status": "not_attempted"},
    }


def _drop_server_side_disposable_target(current_url: URL, database_name: str) -> dict[str, Any]:
    if not database_name.startswith(TEMPORARY_VALIDATION_DB_PREFIX):
        return {"attempted": False, "status": "blocked", "reason": "validation_target_name_not_disposable"}
    admin_engine = create_engine(_admin_postgres_url(current_url), isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.begin() as conn:
            quoted = _quote_pg_identifier(database_name)
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted}"))
        return {"attempted": True, "status": "completed", "target_label": "server_side_disposable_validation_db"}
    except Exception as exc:
        return {"attempted": True, "status": "failed", "reason": _sanitize(exc), "target_label": "server_side_disposable_validation_db"}
    finally:
        admin_engine.dispose()


def _manifest_artifact_path(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    artifact_name = Path(str(manifest.get("backup_file_label") or "")).name
    if not artifact_name:
        raise RestoreMaintenanceBlocked("artifact_invalid", {"status": "blocked", "reason": "Backup manifest does not reference an artifact."})
    return manifest_path.with_name(artifact_name)


def _validate_sqlite_restored_state(target_database_url: str) -> dict[str, Any]:
    engine = create_engine(target_database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.role.in_(["owner", "admin"]), User.is_active.is_(True)).first()
        state = db.get(SchemaVersionState, CURRENT_STATE_ID)
        schema_status = schema_version_status(db)
        migration_plan = build_migration_plan(db)
        checks = {
            "owner_or_admin_access": {"passed": bool(owner), "method": "active_owner_or_admin_row_presence"},
            "schema_version": {
                "passed": bool(state and schema_status.get("status") in {"current", "adopted_baseline", "drift_known_safe"}),
                "status": schema_status.get("status"),
            },
            "migration_plan_readable": {"passed": bool(migration_plan.get("status")), "status": migration_plan.get("status")},
        }
        return {"passed": all(item["passed"] for item in checks.values()), "checks": checks}
    finally:
        db.close()
        engine.dispose()


def _restore_artifact_to_target(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    target_kind: str,
    target_database_url: str | None,
    source_database_url: str | None = None,
) -> dict[str, Any]:
    artifact_path = _manifest_artifact_path(manifest_path, manifest)
    backend = str(manifest.get("db_backend") or "").lower()
    if target_kind == TARGET_CURRENT_PRODUCT_DB:
        if not target_database_url:
            raise RestoreMaintenanceBlocked("target_invalid", {"status": "blocked", "reason": "Current product DB target URL is unavailable."})
    if backend == "sqlite":
        if not target_database_url:
            raise RestoreMaintenanceBlocked("target_invalid", {"status": "blocked", "reason": "SQLite restore requires an internal target database URL."})
        target_url = make_url(target_database_url)
        if not str(target_url.get_backend_name()).lower().startswith("sqlite"):
            raise RestoreMaintenanceBlocked("target_backend_mismatch", {"status": "blocked", "reason": "SQLite backup can only restore to SQLite target."})
        target_path = Path(str(target_url.database or ""))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact_path, target_path)
        validation = _validate_sqlite_restored_state(target_database_url)
        return {
            "status": "restored" if validation["passed"] else "failed",
            "target_kind": target_kind,
            "post_restore_validation": validation,
            "video_archive_files_restored": False,
        }
    if backend == "postgresql":
        if not target_database_url:
            raise RestoreMaintenanceBlocked("target_invalid", {"status": "blocked", "reason": "PostgreSQL restore requires an internal disposable target."})
        result = run_restore_validation(
            manifest_path,
            config=RestoreValidationConfig(
                target_database_url=target_database_url,
                validation_root=manifest_path.parent,
                allow_disposable_target=True,
            ),
            source_database_url=source_database_url,
        )
        return {
            "status": "restored" if result.get("backup_restore_validated") else "failed",
            "target_kind": target_kind,
            "post_restore_validation": _safe_jsonable(result),
            "video_archive_files_restored": False,
        }
    raise RestoreMaintenanceBlocked("unsupported_backup_backend", {"status": "blocked", "reason": "Unsupported backup artifact backend."})


def _build_report(
    *,
    mode: str,
    status: str,
    reason: str,
    artifact: dict[str, Any] | None,
    target: dict[str, Any] | None,
    compatibility: dict[str, Any] | None,
    current_backup: dict[str, Any] | None = None,
    restore_result: dict[str, Any] | None = None,
    actor: Any = None,
) -> dict[str, Any]:
    report = {
        "report_version": RESTORE_REPORT_VERSION,
        "report_id": f"kmvms-restore-rollback-{uuid.uuid4().hex[:12]}",
        "operation": "restore_rollback",
        "mode": mode,
        "generated_at": _utc_iso(),
        "actor": {
            "user_id": getattr(actor, "id", None),
            "username": _sanitize(getattr(actor, "username", None), 100) if getattr(actor, "username", None) else None,
            "role": _sanitize(getattr(actor, "role", None), 50) if getattr(actor, "role", None) else None,
        },
        "status": status,
        "reason": _sanitize(reason),
        "artifact": _safe_jsonable(artifact),
        "target": _safe_jsonable(target),
        "compatibility": _safe_jsonable(compatibility),
        "current_backup": current_backup
        or {
            "required": bool(target and target.get("requires_current_backup")),
            "status": "not_created_for_read_only_or_temporary_target",
            "backup_root_status": "not_checked_for_write",
        },
        "restore_result": _safe_jsonable(restore_result),
        "support": {
            "current_product_restore_supported": False,
            "current_product_restore_status": "blocked",
            "current_product_restore_reason": "current_product_restore_not_enabled",
            "requires_explicit_future_enablement": True,
            "temporary_validation_restore_supported": bool(
                target
                and target.get("target_kind") == TARGET_TEMPORARY_VALIDATION_DB
                and target.get("status") == "safe"
            ),
            "temporary_validation_target": target.get("temporary_validation_target") if target else None,
        },
        "mutation_scope": {
            "current_product_db": target.get("target_kind") == TARGET_CURRENT_PRODUCT_DB and status == "restored" if target else False,
            "temporary_validation_db": target.get("target_kind") == TARGET_TEMPORARY_VALIDATION_DB and status == "restored" if target else False,
            "recordings_or_archive_files": False,
            "migration_auto_apply": False,
            "product_update_orchestration": False,
        },
        "side_effects": {
            "db_restored": mode == "apply" and status == "restored",
            "current_backup_created": bool(current_backup and current_backup.get("status") == "verified"),
            "video_archive_files_restored": False,
        },
        "rollback_guidance": "Retry/rollback must use this explicit restore flow; Stage 3 does not perform hidden automatic rollback.",
        "redaction": {
            "raw_db_url_included": False,
            "raw_backup_path_included": False,
            "raw_restore_command_included": False,
            "sensitive_values_included": False,
        },
    }
    assert_restore_report_secret_safe(report)
    return report


def assert_restore_report_secret_safe(report: dict[str, Any]) -> None:
    def iter_values(value: Any):
        if isinstance(value, dict):
            for item in value.values():
                yield from iter_values(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                yield from iter_values(item)
        else:
            yield value

    rendered = "\n".join(str(item) for item in iter_values(report))
    if SENSITIVE_RE.search(rendered):
        raise RestoreMaintenanceBlocked("unsafe_report", {"status": "blocked", "reason": "Restore report contains sensitive-looking data."})


def inspect_restore_maintenance(*, backup_root: str | None = None, actor: Any = None) -> dict[str, Any]:
    artifacts = list_restore_artifacts(backup_root=backup_root)
    valid = [item for item in artifacts if item.get("valid")]
    status = "available" if valid else "no_artifacts"
    reason = "Valid restore artifacts are available." if valid else "No valid restore artifacts are available in configured backup root."
    report = _build_report(mode="status", status=status, reason=reason, artifact=None, target=None, compatibility=None, actor=actor)
    return {
        "status": status,
        "reason": reason,
        "artifact_count": len(artifacts),
        "valid_artifact_count": len(valid),
        "artifacts": artifacts,
        "can_restore": False,
        "temporary_validation_restore_supported": True,
        "temporary_validation_target": "server_side_disposable_postgresql",
        "current_product_restore_supported": False,
        "current_product_restore_status": "blocked",
        "current_product_restore_reason": "current_product_restore_not_enabled",
        "requires_explicit_future_enablement": True,
        "requires_confirmation": False,
        "report_id": report["report_id"],
        "report": report,
    }


def dry_run_restore_maintenance(
    db: Session,
    *,
    artifact_id: str | None,
    target_kind: str = TARGET_TEMPORARY_VALIDATION_DB,
    backup_root: str | None = None,
    target_database_url: str | None = None,
    actor: Any = None,
) -> dict[str, Any]:
    if not artifact_id:
        status_payload = inspect_restore_maintenance(backup_root=backup_root, actor=actor)
        status_payload["dry_run"] = True
        status_payload["mutates_database"] = False
        status_payload["creates_current_backup"] = False
        return status_payload
    manifest_path, manifest = _manifest_for_artifact(artifact_id, backup_root=backup_root)
    verification = verify_backup_manifest(manifest_path)
    artifact = _artifact_summary(manifest_path, manifest, verification)
    compatibility = _compatibility(manifest)
    target = _target_status(target_kind, target_database_url=target_database_url, current_db_url=db.get_bind().url)
    status = "valid" if verification.get("valid") and compatibility["status"] == "compatible" and target["status"] == "safe" else "blocked"
    reason = "Restore dry-run validation passed." if status == "valid" else next(
        item.get("reason") for item in [compatibility, target, {"reason": verification.get("summary")}] if item.get("reason")
    )
    report = _build_report(mode="dry_run", status=status, reason=reason, artifact=artifact, target=target, compatibility=compatibility, actor=actor)
    return {
        "status": status,
        "reason": report["reason"],
        "artifact_id": artifact["artifact_id"],
        "artifact_created_at": artifact["artifact_created_at"],
        "artifact_schema_version": artifact["artifact_schema_version"],
        "current_schema_version": schema_version_status(db).get("schema_version"),
        "target_kind": target_kind,
        "requires_current_backup": bool(target.get("requires_current_backup")),
        "temporary_validation_restore_supported": bool(target.get("temporary_validation_restore_supported", target_kind == TARGET_TEMPORARY_VALIDATION_DB and target.get("status") == "safe")),
        "temporary_validation_target": target.get("temporary_validation_target"),
        "current_product_restore_supported": False,
        "current_product_restore_status": "blocked",
        "current_product_restore_reason": "current_product_restore_not_enabled",
        "requires_explicit_future_enablement": target_kind == TARGET_CURRENT_PRODUCT_DB,
        "current_backup_status": "not_created_for_dry_run",
        "compatibility_status": compatibility["status"],
        "can_restore": status == "valid",
        "requires_confirmation": status == "valid",
        "dry_run": True,
        "mutates_database": False,
        "creates_current_backup": False,
        "warnings": [] if status == "valid" else [report["reason"]],
        "report_id": report["report_id"],
        "report": report,
    }


def apply_restore_maintenance(
    db: Session,
    *,
    confirm: bool,
    artifact_id: str,
    target_kind: str,
    backup_root: str | None = None,
    target_database_url: str | None = None,
    actor: Any = None,
    allow_current_product_db_for_tests: bool = False,
) -> dict[str, Any]:
    if not confirm:
        raise RestoreMaintenanceBlocked("confirmation_required", {"status": "blocked", "reason": "Explicit confirm=true is required for restore apply."})
    if target_kind == TARGET_CURRENT_PRODUCT_DB and not allow_current_product_db_for_tests:
        raise RestoreMaintenanceBlocked(
            "current_product_restore_not_enabled",
            {
                "status": "blocked",
                "reason": "current_product_restore_not_enabled",
                "current_product_restore_supported": False,
                "current_product_restore_status": "blocked",
                "requires_explicit_future_enablement": True,
                "restore_executed": False,
            },
        )
    dry = dry_run_restore_maintenance(
        db,
        artifact_id=artifact_id,
        target_kind=target_kind,
        backup_root=backup_root,
        target_database_url=target_database_url,
        actor=actor,
    )
    if dry["status"] != "valid":
        raise RestoreMaintenanceBlocked(str(dry.get("compatibility_status") or dry["status"]), dry)

    manifest_path, manifest = _manifest_for_artifact(artifact_id, backup_root=backup_root)
    current_backup = None
    disposable_target: dict[str, Any] | None = None
    if target_kind == TARGET_CURRENT_PRODUCT_DB:
        try:
            backup = create_backup_before_upgrade(
                db,
                config=BackupExecutionConfig(source="pre_restore", backup_root=Path(backup_root) if backup_root else None, allow_tmp_for_tests=bool(backup_root)),
                migration_plan_summary={"operation": "restore_rollback", "target_kind": target_kind, "artifact_id": artifact_id},
            )
        except BackupSafetyBlocked as exc:
            raise RestoreMaintenanceBlocked(
                exc.status,
                {
                    "status": "blocked",
                    "reason": _sanitize(exc.diagnostics.get("summary") or exc.status),
                    "current_backup": {"status": exc.status, "backup_created": False},
                    "restore_executed": False,
                },
            ) from exc
        current_backup = {
            "required": True,
            "status": backup["status"],
            "backup_id": backup["backup_id"],
            "backup_root_status": "ready",
            "backup_file_label": backup["backup_file_label"],
            "metadata_file_label": backup["metadata_file_label"],
            "manifest_reference": "configured_backup_root/manifest",
        }

    if target_kind == TARGET_TEMPORARY_VALIDATION_DB and not target_database_url:
        disposable_target = _create_server_side_disposable_target(db.get_bind().url)
        restore_target_url = disposable_target["target_database_url"]
    else:
        restore_target_url = target_database_url or (db.get_bind().url.render_as_string(hide_password=False) if target_kind == TARGET_CURRENT_PRODUCT_DB else None)
    try:
        restore_result = _restore_artifact_to_target(
            manifest_path,
            manifest,
            target_kind=target_kind,
            target_database_url=restore_target_url,
            source_database_url=db.get_bind().url.render_as_string(hide_password=False),
        )
    except (RestoreMaintenanceBlocked, RestoreValidationBlocked) as exc:
        status_value = getattr(exc, "status", "restore_failed")
        diagnostics = getattr(exc, "diagnostics", {"summary": str(exc)})
        report = _build_report(
            mode="apply",
            status="failed",
            reason=_sanitize(diagnostics.get("summary") or diagnostics.get("reason") or status_value),
            artifact=dry["report"]["artifact"],
            target=dry["report"]["target"],
            compatibility=dry["report"]["compatibility"],
            current_backup=current_backup,
            restore_result={"status": "failed", "step": status_value},
            actor=actor,
        )
        raise RestoreMaintenanceBlocked(status_value, {"status": "failed", "reason": report["reason"], "report": report}) from exc
    finally:
        if disposable_target is not None:
            cleanup = _drop_server_side_disposable_target(db.get_bind().url, str(disposable_target["database_name"]))
            disposable_target["cleanup"] = cleanup
            if "restore_result" in locals():
                restore_result["temporary_validation_cleanup"] = cleanup

    status_value = "restored" if restore_result.get("status") == "restored" else "failed"
    report = _build_report(
        mode="apply",
        status=status_value,
        reason="Restore completed and post-restore validation passed." if status_value == "restored" else "Restore failed post-restore validation.",
        artifact=dry["report"]["artifact"],
        target=dry["report"]["target"],
        compatibility=dry["report"]["compatibility"],
        current_backup=current_backup,
        restore_result=restore_result,
        actor=actor,
    )
    return {
        "status": status_value,
        "reason": report["reason"],
        "artifact_id": artifact_id,
        "target_kind": target_kind,
        "current_backup_required": target_kind == TARGET_CURRENT_PRODUCT_DB,
        "current_backup_status": (current_backup or {}).get("status") or "not_required",
        "temporary_validation_restore_supported": target_kind == TARGET_TEMPORARY_VALIDATION_DB,
        "temporary_validation_target": "server_side_disposable_postgresql" if disposable_target is not None else dry["report"]["target"].get("temporary_validation_target"),
        "temporary_validation_cleanup": (disposable_target or {}).get("cleanup"),
        "current_product_restore_supported": False,
        "current_product_restore_status": "blocked",
        "current_product_restore_reason": "current_product_restore_not_enabled",
        "restore_executed": status_value == "restored",
        "post_restore_validation_status": restore_result.get("post_restore_validation", {}).get("passed"),
        "video_archive_files_restored": False,
        "migration_auto_apply": False,
        "product_update_orchestration": False,
        "report_id": report["report_id"],
        "report": report,
    }
