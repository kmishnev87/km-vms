from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, func, inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.services.backup_manager import (
    BackupManagerBlocked,
    artifact_version_evidence,
    begin_backup_operation,
    configured_backup_root,
    current_validation_context,
    new_backup_artifact_id,
    safe_receipt,
    update_backup_operation,
    write_artifact_state,
)
from app.services.schema_versioning import schema_version_status


BACKUP_STATUS_PLANNED = "planned"
BACKUP_STATUS_COMPLETED = "completed"
BACKUP_STATUS_FAILED = "failed"
BACKUP_STATUS_VERIFIED = "verified"
BACKUP_STATUS_INVALID = "invalid"
RESTORE_VALIDATION_STATUS = "not_performed_stage5_deferred"
DEFAULT_BACKUP_ROOT = "/storage/backups/db"
CONTAINER_ONLY_BACKUP_ROOTS = {"/var/lib/km-vms/backups/db"}
DEFAULT_RECENCY_MINUTES = 60 * 24
MIN_FREE_MARGIN_BYTES = 16 * 1024 * 1024
FALLBACK_ESTIMATE_BYTES = 64 * 1024 * 1024
SENSITIVE_RE = re.compile(
    r"(password|passwd|secret|token|authorization|jwt|rtsp://|postgresql://|sqlite:///)[^,\s\"']*",
    re.IGNORECASE,
)


class BackupSafetyBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("summary") or status)


@dataclass(frozen=True)
class BackupExecutionConfig:
    backup_root: Path | None = None
    source: str = "manual_admin"
    backup_id: str | None = None
    allow_tmp_for_tests: bool = False
    pg_dump_path: str = "pg_dump"
    pg_restore_path: str = "pg_restore"
    now: datetime | None = None
    min_required_bytes: int | None = None
    require_persistent_root: bool = True


def sanitize_error(value: Any) -> str:
    return SENSITIVE_RE.sub(lambda match: match.group(1).split(":")[0] + "=***", str(value or ""))[:300]


def detect_db_backend(db: Session) -> str:
    name = str(db.get_bind().url.get_backend_name()).lower()
    if name.startswith("postgresql"):
        return "postgresql"
    if name.startswith("sqlite"):
        return "sqlite"
    return "unknown"


def _utc_now(config: BackupExecutionConfig) -> datetime:
    return config.now or datetime.utcnow()


def _backup_root(config: BackupExecutionConfig) -> Path:
    return config.backup_root or Path(os.getenv("KMVMS_DB_BACKUP_ROOT") or settings.kmvms_db_backup_root or DEFAULT_BACKUP_ROOT)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _safe_path_label(path: Path) -> str:
    return f"configured_backup_root/{path.name}"


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _validate_backup_root(path: Path, *, allow_tmp_for_tests: bool) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    cwd = Path.cwd().resolve()
    if str(resolved) in CONTAINER_ONLY_BACKUP_ROOTS:
        raise BackupSafetyBlocked(
            "container_only_root_blocked",
            {
                "status": "blocked",
                "summary": "Backup destination is a legacy container-only path and is not persistent enough for upgrade safety.",
                "reason": "container_only_root_blocked",
                "root_status": "container_only_root_blocked",
            },
        )
    forbidden = [
        (cwd, "product_git_tree"),
        (cwd / "Working folder", "working_folder"),
        (Path("/storage/archive"), "video_archive_folder"),
        (Path(str(settings.storage_root or "/storage/archive")), "video_archive_folder"),
        (Path(str(settings.storage_previews or "/storage/previews")), "storage_previews_folder"),
        (Path(str(settings.storage_exports or "/storage/exports")), "storage_exports_folder"),
    ]
    for forbidden_path, reason in forbidden:
        if _inside(resolved, forbidden_path):
            status = "backup_root_inside_archive_root" if reason == "video_archive_folder" else "unsafe_backup_location"
            raise BackupSafetyBlocked(
                status,
                {
                    "status": "blocked",
                    "summary": f"Backup destination is inside forbidden {reason}.",
                    "reason": reason,
                    "root_status": status,
                },
            )
    if _inside(resolved, Path("/tmp")) and not allow_tmp_for_tests:
        raise BackupSafetyBlocked(
            "unsafe_backup_location",
            {
                "status": "blocked",
                "summary": "Backup destination may not use /tmp as final destination.",
                "reason": "tmp_final_destination",
                "root_status": "unsafe_backup_location",
            },
        )
    root_status = "disposable_test_root" if allow_tmp_for_tests else "configured_persistent_root"
    return {"path": resolved, "path_label": "configured_backup_root", "root_status": root_status}


def _ensure_backup_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2, default=str), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _reflected_table(db: Session, table_name: str) -> Table:
    return Table(table_name, MetaData(), autoload_with=db.get_bind())


def _table_count(db: Session, table_name: str, table_names: set[str]) -> int | None:
    if table_name not in table_names:
        return None
    try:
        table = _reflected_table(db, table_name)
        return int(db.execute(select(func.count()).select_from(table)).scalar() or 0)
    except Exception:
        return None


def _first_existing_row(db: Session, table_name: str, columns: tuple[str, ...]) -> dict[str, Any] | None:
    table = _reflected_table(db, table_name)
    selected = [table.c[name] for name in columns if name in table.c]
    if not selected:
        return None
    row = db.execute(select(*selected).limit(1)).mappings().first()
    return dict(row) if row else None


def build_backup_metadata_snapshot(
    db: Session,
    *,
    backup_id: str,
    db_backend: str,
    source: str,
    backup_checksum: str | None,
    migration_plan_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inspector = inspect(db.get_bind())
    table_names = sorted(inspector.get_table_names())
    table_name_set = set(table_names)
    settings_row = (
        _first_existing_row(
            db,
            "system_settings",
            ("recording_format", "auto_free_space_cleanup_enabled"),
        )
        if "system_settings" in table_name_set
        else None
    )
    archive_roots = []
    if "archive_roots" in table_name_set:
        archive_root_table = _reflected_table(db, "archive_roots")
        archive_root_columns = [
            archive_root_table.c[name]
            for name in ("id", "label", "is_active", "is_available", "storage_namespace")
            if name in archive_root_table.c
        ]
        rows = db.execute(select(*archive_root_columns).limit(50)).mappings().all() if archive_root_columns else []
        for item in rows:
            archive_roots.append(
                {
                    "id": item.get("id"),
                    "label": item.get("label"),
                    "active": bool(item.get("is_active")),
                    "available": bool(item.get("is_available")),
                    "path_label": "archive_root_path_redacted",
                    "namespace": item.get("storage_namespace"),
                }
            )
    return {
        "backup_id": backup_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "source": source,
        "db_backend": db_backend,
        "app_version": APP_VERSION,
        "app_build_version": APP_BUILD_VERSION,
        "schema_version_state": schema_version_status(db),
        "migration_plan_summary": migration_plan_summary or {"available": False},
        "table_summary": {"tables": table_names, "count": len(table_names)},
        "entity_counts": {
            "users": _table_count(db, "users", table_name_set),
            "cameras": _table_count(db, "cameras", table_name_set),
            "system_settings": _table_count(db, "system_settings", table_name_set),
            "archive_roots": _table_count(db, "archive_roots", table_name_set),
            "recording_jobs": _table_count(db, "recording_jobs", table_name_set),
            "recording_segments": _table_count(db, "recording_segments", table_name_set),
            "schema_migration_history": _table_count(db, "schema_migration_history", table_name_set),
            "schema_version_state": _table_count(db, "schema_version_state", table_name_set),
        },
        "storage_settings_summary": {
            "storage_path_label": "storage_path_redacted",
            "recording_format": settings_row.get("recording_format") if settings_row else None,
            "auto_free_space_cleanup_enabled": bool(settings_row.get("auto_free_space_cleanup_enabled")) if settings_row else None,
        },
        "archive_roots_summary": archive_roots,
        "backup_checksum": backup_checksum,
        "backup_requires_restore_validation": True,
        "restore_validation_status": RESTORE_VALIDATION_STATUS,
    }


def _iter_payload_values(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_payload_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_payload_values(item)
    else:
        yield value


def _assert_secret_safe(payload: dict[str, Any]) -> None:
    raw = "\n".join(str(item) for item in _iter_payload_values(payload))
    if SENSITIVE_RE.search(raw):
        raise BackupSafetyBlocked(
            "unsafe_backup_metadata",
            {"status": "blocked", "summary": "Backup metadata snapshot contains sensitive-looking data."},
        )


def _estimate_db_size(db: Session, backend: str) -> dict[str, Any]:
    if backend == "postgresql":
        try:
            value = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
            return {"estimated_bytes": int(value or 0), "method": "postgres_pg_database_size"}
        except Exception:
            return {"estimated_bytes": FALLBACK_ESTIMATE_BYTES, "method": "conservative_fallback"}
    if backend == "sqlite":
        database = db.get_bind().url.database
        if database and database != ":memory:" and Path(database).exists():
            return {"estimated_bytes": int(Path(database).stat().st_size), "method": "sqlite_file_size"}
    return {"estimated_bytes": FALLBACK_ESTIMATE_BYTES, "method": "conservative_fallback"}


def build_backup_plan(
    db: Session,
    *,
    config: BackupExecutionConfig | None = None,
    migration_plan_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or BackupExecutionConfig()
    backend = detect_db_backend(db)
    root_info = _validate_backup_root(_backup_root(config), allow_tmp_for_tests=config.allow_tmp_for_tests)
    estimate = _estimate_db_size(db, backend)
    free = shutil.disk_usage(_nearest_existing_parent(root_info["path"])).free
    required = int(config.min_required_bytes or max(estimate["estimated_bytes"] * 2, estimate["estimated_bytes"] + MIN_FREE_MARGIN_BYTES))
    preflight_passed = backend in {"postgresql", "sqlite"} and free >= required
    return {
        "backup_id": None,
        "status": BACKUP_STATUS_PLANNED,
        "created_at": None,
        "db_backend": backend,
        "source": config.source,
        "backup_location_label": root_info["path_label"],
        "backup_root_status": "ready_for_tests_only" if root_info["root_status"] == "disposable_test_root" else "ready",
        "backup_root_classification": root_info["root_status"],
        "backup_root_persistent": root_info["root_status"] == "configured_persistent_root",
        "backup_root_archive_scope": "outside_archive_root_and_retention_scope",
        "free_space": {
            "destination_free_bytes": int(free),
            "estimated_backup_bytes": int(estimate["estimated_bytes"]),
            "required_free_bytes": required,
            "method": estimate["method"],
            "passed": bool(preflight_passed),
        },
        "schema_version": schema_version_status(db),
        "migration_plan_summary": migration_plan_summary or {"available": False},
        "backup_requires_restore_validation": True,
        "restore_validation_status": RESTORE_VALIDATION_STATUS,
        "mutates_database": False,
        "creates_backup_files": False,
    }


def _postgres_command(db: Session, backup_path: Path, pg_dump_path: str) -> tuple[list[str], dict[str, str]]:
    url = db.get_bind().url
    cmd = [pg_dump_path, "--format=custom", "--file", str(backup_path)]
    if url.host:
        cmd.extend(["--host", url.host])
    if url.port:
        cmd.extend(["--port", str(url.port)])
    if url.username:
        cmd.extend(["--username", url.username])
    if url.database:
        cmd.append(url.database)
    env = os.environ.copy()
    password = url.password
    if password:
        env["PGPASSWORD"] = password
    return cmd, env


def _run_postgres_backup(db: Session, tmp_path: Path, config: BackupExecutionConfig) -> None:
    cmd, env = _postgres_command(db, tmp_path, config.pg_dump_path)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise BackupSafetyBlocked(
            "backup_failed",
            {"status": BACKUP_STATUS_FAILED, "summary": sanitize_error(result.stderr or result.stdout or "pg_dump failed")},
        )


def _run_sqlite_backup(db: Session, tmp_path: Path) -> None:
    database = db.get_bind().url.database
    if not database or database == ":memory:":
        raise BackupSafetyBlocked(
            "backup_failed",
            {"status": BACKUP_STATUS_FAILED, "summary": "SQLite file backup requires a file-backed local/test database."},
        )
    source = Path(database)
    if not source.exists():
        raise BackupSafetyBlocked("backup_failed", {"status": BACKUP_STATUS_FAILED, "summary": "SQLite database file is not readable."})
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(tmp_path)) as dst:
        src.backup(dst)


def create_backup_before_upgrade(
    db: Session,
    *,
    config: BackupExecutionConfig | None = None,
    migration_plan_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or BackupExecutionConfig()
    plan = build_backup_plan(db, config=config, migration_plan_summary=migration_plan_summary)
    if not plan["free_space"]["passed"]:
        raise BackupSafetyBlocked("insufficient_free_space", {**plan, "status": BACKUP_STATUS_FAILED, "summary": "Insufficient free space for DB backup."})
    root = _validate_backup_root(_backup_root(config), allow_tmp_for_tests=config.allow_tmp_for_tests)["path"]
    _ensure_backup_root(root)
    now = _utc_now(config)
    backup_id = config.backup_id or f"kmvms-db-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    if not re.fullmatch(r"kmvms-db-\d{8}T\d{6}Z-[a-f0-9]{12}", backup_id):
        raise BackupSafetyBlocked(
            "backup_id_invalid",
            {"status": BACKUP_STATUS_FAILED, "summary": "Internal backup artifact identity is invalid."},
        )
    backend = plan["db_backend"]
    extension = ".dump" if backend == "postgresql" else ".sqlite3"
    tmp_path = root / f".{backup_id}{extension}.tmp"
    backup_path = root / f"{backup_id}{extension}"
    if backup_path.exists():
        raise BackupSafetyBlocked("backup_exists", {"status": BACKUP_STATUS_FAILED, "summary": "Backup artifact already exists."})
    started_at = datetime.utcnow()
    try:
        if backend == "postgresql":
            _run_postgres_backup(db, tmp_path, config)
        elif backend == "sqlite":
            _run_sqlite_backup(db, tmp_path)
        else:
            raise BackupSafetyBlocked("unsupported_backend", {"status": BACKUP_STATUS_FAILED, "summary": "Unsupported database backend."})
        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            raise BackupSafetyBlocked("backup_failed", {"status": BACKUP_STATUS_FAILED, "summary": "Backup artifact was not created."})
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        tmp_path.replace(backup_path)
        checksum = _sha256(backup_path)
        metadata = build_backup_metadata_snapshot(
            db,
            backup_id=backup_id,
            db_backend=backend,
            source=config.source,
            backup_checksum=checksum,
            migration_plan_summary=migration_plan_summary,
        )
        _assert_secret_safe(metadata)
        metadata_path = root / f"{backup_id}.metadata.json"
        manifest_path = root / f"{backup_id}.manifest.json"
        _write_json_atomic(metadata_path, metadata)
        manifest = {
            "backup_id": backup_id,
            "created_at": now.isoformat() + "Z",
            "started_at": started_at.isoformat() + "Z",
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "db_backend": backend,
            "source": config.source,
            "schema_version": plan["schema_version"],
            "migration_plan_summary": migration_plan_summary or {"available": False},
            "app_version": APP_VERSION,
            "app_build_version": APP_BUILD_VERSION,
            "backup_file_label": _safe_path_label(backup_path),
            "metadata_file_label": _safe_path_label(metadata_path),
            "file_size": backup_path.stat().st_size,
            "checksum_sha256": checksum,
            "status": BACKUP_STATUS_VERIFIED,
            "backup_requires_restore_validation": True,
            "restore_validation_status": RESTORE_VALIDATION_STATUS,
            "video_archive_files_included": False,
            "sensitive_fields_included": False,
            "free_space": plan["free_space"],
            "error_summary": None,
        }
        _assert_secret_safe(manifest)
        _write_json_atomic(manifest_path, manifest)
        return {**manifest, "manifest_path": str(manifest_path), "backup_file_path": str(backup_path), "metadata_path": str(metadata_path)}
    except BackupSafetyBlocked:
        for artifact in (tmp_path, backup_path, root / f"{backup_id}.metadata.json", root / f"{backup_id}.manifest.json"):
            if artifact.exists():
                artifact.unlink()
        raise
    except Exception as exc:
        for artifact in (tmp_path, backup_path, root / f"{backup_id}.metadata.json", root / f"{backup_id}.manifest.json"):
            if artifact.exists():
                artifact.unlink()
        raise BackupSafetyBlocked("backup_failed", {"status": BACKUP_STATUS_FAILED, "summary": sanitize_error(exc)}) from exc


def run_backup_create_operation(
    db: Session,
    *,
    submission_id: str,
    actor: Any,
    backup_root: str | Path | None = None,
    migration_plan_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = configured_backup_root(backup_root)
    planned_id = new_backup_artifact_id()
    try:
        receipt, replayed = begin_backup_operation(
            submission_id=submission_id,
            kind="create",
            actor=actor,
            planned_artifact_id=planned_id,
            backup_root=root,
        )
    except BackupManagerBlocked as exc:
        raise BackupSafetyBlocked(exc.code, exc.diagnostics) from exc
    if replayed:
        return safe_receipt(receipt, replayed=True)

    receipt = update_backup_operation(
        receipt,
        state="running",
        phase="creating_backup",
        backup_root=root,
    )
    try:
        result = create_backup_before_upgrade(
            db,
            config=BackupExecutionConfig(
                backup_root=root if backup_root is not None else None,
                source="manual_admin",
                backup_id=planned_id,
                allow_tmp_for_tests=backup_root is not None,
            ),
            migration_plan_summary=migration_plan_summary,
        )
        manifest_path = Path(result["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        evidence = artifact_version_evidence(
            root,
            planned_id,
            manifest,
            checksum_sha256=str(result.get("checksum_sha256") or ""),
            context=current_validation_context(detect_db_backend(db)),
        )
        completed_at = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
        public_result = {
            "status": "verified",
            "availability_status": "available",
            "integrity_status": "verified",
            "compatibility_status": "compatible",
            "restore_validation_status": "not_performed",
            "delete_status": "allowed",
            "file_size": int(result.get("file_size") or 0),
            "db_backend": result.get("db_backend"),
            "checked_at": completed_at,
            "video_archive_files_included": False,
        }
        write_artifact_state(
            root,
            planned_id,
            {
                "create_evidence": {
                    "operation_id": receipt["operation_id"],
                    "outcome": "completed",
                    "completed_at": completed_at,
                    "result": public_result,
                },
                "integrity": {
                    "status": "verified",
                    "checked_at": completed_at,
                    "operation_id": receipt["operation_id"],
                    "reason_code": None,
                    "evidence": evidence,
                },
                "restore_validation": {
                    "status": "not_performed",
                    "validated_at": None,
                    "operation_id": None,
                    "reason_code": "not_performed",
                    "evidence": None,
                },
                "delete_status": "allowed",
            },
        )
        receipt = update_backup_operation(
            receipt,
            state="completed",
            phase="completed",
            result=public_result,
            backup_root=root,
        )
        return safe_receipt(receipt)
    except BackupSafetyBlocked as exc:
        receipt = update_backup_operation(
            receipt,
            state="failed",
            phase="failed",
            retryable=True,
            reason_code=exc.status,
            result={"status": "failed"},
            backup_root=root,
        )
        exc.diagnostics = {**exc.diagnostics, "receipt": safe_receipt(receipt)}
        raise
    except Exception as exc:
        receipt = update_backup_operation(
            receipt,
            state="failed",
            phase="failed",
            retryable=True,
            reason_code="backup_create_failed",
            result={"status": "failed"},
            backup_root=root,
        )
        raise BackupSafetyBlocked(
            "backup_create_failed",
            {
                "status": "failed",
                "summary": sanitize_error(exc),
                "receipt": safe_receipt(receipt),
            },
        ) from exc


def verify_backup_manifest(
    manifest_path: str | Path,
    *,
    max_age_minutes: int | None = None,
) -> dict[str, Any]:
    path = Path(manifest_path)
    if path.is_symlink():
        return {
            "valid": False,
            "status": BACKUP_STATUS_INVALID,
            "availability_status": "unsafe",
            "integrity_status": "failed",
            "freshness_status": "not_evaluated",
            "summary": "Backup manifest ownership evidence is unsafe.",
        }
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "valid": False,
            "status": BACKUP_STATUS_INVALID,
            "availability_status": "unsafe",
            "integrity_status": "failed",
            "freshness_status": "not_evaluated",
            "summary": sanitize_error(exc),
        }
    backup_path = path.with_name(Path(str(manifest.get("backup_file_label", ""))).name)
    metadata_path = path.with_name(Path(str(manifest.get("metadata_file_label", ""))).name)
    backup_id = str(manifest.get("backup_id") or "")
    expected_backup_names = {f"{backup_id}.dump", f"{backup_id}.sqlite3"}
    if (
        not backup_id
        or backup_path.name not in expected_backup_names
        or metadata_path.name != f"{backup_id}.metadata.json"
        or backup_path.is_symlink()
        or metadata_path.is_symlink()
        or backup_path.parent.resolve() != path.parent.resolve()
        or metadata_path.parent.resolve() != path.parent.resolve()
    ):
        return {
            "valid": False,
            "status": BACKUP_STATUS_INVALID,
            "availability_status": "unsafe",
            "integrity_status": "failed",
            "freshness_status": "not_evaluated",
            "summary": "Backup artifact ownership evidence is unsafe.",
        }
    if not backup_path.exists():
        return {
            "valid": False,
            "status": BACKUP_STATUS_INVALID,
            "availability_status": "missing",
            "integrity_status": "failed",
            "freshness_status": "not_evaluated",
            "summary": "Backup artifact is missing.",
        }
    size = backup_path.stat().st_size
    if size <= 0 or int(manifest.get("file_size") or 0) != size:
        return {
            "valid": False,
            "status": BACKUP_STATUS_INVALID,
            "availability_status": "incomplete",
            "integrity_status": "failed",
            "freshness_status": "not_evaluated",
            "summary": "Backup size does not match manifest.",
        }
    observed_checksum = _sha256(backup_path)
    if observed_checksum != manifest.get("checksum_sha256"):
        return {
            "valid": False,
            "status": BACKUP_STATUS_INVALID,
            "availability_status": "available",
            "integrity_status": "failed",
            "freshness_status": "not_evaluated",
            "observed_checksum_sha256": observed_checksum,
            "summary": "Backup checksum does not match manifest.",
        }
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        _assert_secret_safe(metadata)
    except Exception as exc:
        return {
            "valid": False,
            "status": BACKUP_STATUS_INVALID,
            "availability_status": "incomplete",
            "integrity_status": "failed",
            "freshness_status": "not_evaluated",
            "summary": sanitize_error(exc),
        }
    freshness_status = "not_evaluated"
    if max_age_minutes is not None:
        try:
            created_at = datetime.fromisoformat(str(manifest["created_at"]).replace("Z", ""))
            freshness_status = (
                "fresh"
                if created_at >= datetime.utcnow() - timedelta(minutes=max(0, int(max_age_minutes)))
                else "stale"
            )
        except Exception:
            freshness_status = "unknown"
    return {
        "valid": True,
        "status": BACKUP_STATUS_VERIFIED,
        "availability_status": "available",
        "integrity_status": "verified",
        "freshness_status": freshness_status,
        "backup_id": manifest.get("backup_id"),
        "db_backend": manifest.get("db_backend"),
        "source": manifest.get("source"),
        "file_size": size,
        "checksum_sha256": manifest.get("checksum_sha256"),
        "observed_checksum_sha256": observed_checksum,
        "restore_validation_status": manifest.get("restore_validation_status"),
    }


def backup_precondition_status(
    *,
    manifest_path: str | Path | None,
    required: bool,
    manual_only: bool = False,
    manual_authorized: bool = False,
    max_age_minutes: int = DEFAULT_RECENCY_MINUTES,
) -> dict[str, Any]:
    if manual_only and not manual_authorized:
        return {
            "status": "blocked",
            "backup_required": required,
            "manual_authorization_required": True,
            "summary": "Manual-only migration requires explicit manual authorization.",
        }
    if not required:
        return {"status": "not_required", "backup_required": False, "summary": "Backup is not required for this operation."}
    if not manifest_path:
        return {"status": "blocked", "backup_required": True, "summary": "A recent verified backup manifest is required."}
    verification = verify_backup_manifest(manifest_path, max_age_minutes=max_age_minutes)
    if not verification["valid"] or verification.get("freshness_status") != "fresh":
        if verification.get("freshness_status") == "stale":
            verification = {**verification, "summary": "A recent verified backup manifest is required."}
        return {"status": "blocked", "backup_required": True, "verification": verification, "summary": verification["summary"]}
    return {"status": "satisfied", "backup_required": True, "verification": verification, "summary": "Backup precondition is satisfied."}
