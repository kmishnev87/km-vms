from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION


BACKUP_MANAGER_SCHEMA = "stage13.7.10.backup-manager.v1"
BACKUP_STATE_SCHEMA = "stage13.7.10.backup-state.v1"
BACKUP_RECEIPT_SCHEMA = "stage13.7.10.backup-receipt.v1"
BACKUP_ARTIFACT_ID_RE = re.compile(r"^kmvms-db-\d{8}T\d{6}Z-[a-f0-9]{12}$")
SUBMISSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
DEFAULT_BACKUP_ROOT = "/storage/backups/db"
RECEIPT_DIR_NAME = ".kmvms-backup-operations"
STATE_SUFFIX = ".state.json"
MAX_JSON_BYTES = 512 * 1024
MAX_RECEIPTS = 100
MAX_PAGE_SIZE = 50
OPERATION_KINDS = {"create", "check", "delete"}
ACTIVE_STATES = {"queued", "running"}
TERMINAL_STATES = {"completed", "failed", "interrupted"}
TEMPORARY_VALIDATION_DB_PREFIX = "kmvms_stage5_stage13_restore_validation_"
TEMPORARY_VALIDATION_DB_RE = re.compile(
    rf"^{re.escape(TEMPORARY_VALIDATION_DB_PREFIX)}[a-f0-9]{{12}}$"
)
PROCESS_INSTANCE_ID = uuid.uuid4().hex
_THREAD_LOCK = threading.RLock()


class BackupManagerBlocked(RuntimeError):
    def __init__(self, code: str, diagnostics: dict[str, Any] | None = None):
        self.code = code
        self.diagnostics = diagnostics or {}
        super().__init__(code)


def utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def configured_backup_root(backup_root: str | Path | None = None) -> Path:
    configured = backup_root or os.getenv("KMVMS_DB_BACKUP_ROOT") or settings.kmvms_db_backup_root or DEFAULT_BACKUP_ROOT
    expanded = Path(configured).expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def new_backup_artifact_id(now: datetime | None = None) -> str:
    value = now or datetime.utcnow()
    return f"kmvms-db-{value.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"


def validate_artifact_id(value: str) -> str:
    artifact_id = str(value or "").strip()
    if not BACKUP_ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise BackupManagerBlocked("artifact_invalid", {"status": "blocked", "reason_code": "artifact_invalid"})
    return artifact_id


def validate_submission_id(value: str) -> str:
    submission_id = str(value or "").strip().lower()
    if not SUBMISSION_ID_RE.fullmatch(submission_id):
        raise BackupManagerBlocked("submission_id_invalid", {"status": "blocked", "reason_code": "submission_id_invalid"})
    try:
        if str(uuid.UUID(submission_id)) != submission_id:
            raise ValueError("non-canonical UUID")
    except ValueError as exc:
        raise BackupManagerBlocked(
            "submission_id_invalid",
            {"status": "blocked", "reason_code": "submission_id_invalid"},
        ) from exc
    return submission_id


def actor_binding_key(actor: Any) -> str:
    actor_id = getattr(actor, "id", None)
    username = getattr(actor, "username", None)
    identity = f"id={actor_id}" if actor_id is not None else f"internal={username or 'service'}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _ensure_trusted_root(root: Path) -> None:
    if root.exists() and root.is_symlink():
        raise BackupManagerBlocked("backup_root_unsafe", {"status": "blocked", "reason_code": "backup_root_unsafe"})
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass


def _ensure_receipt_dir(root: Path) -> Path:
    _ensure_trusted_root(root)
    directory = root / RECEIPT_DIR_NAME
    if directory.exists() and directory.is_symlink():
        raise BackupManagerBlocked("receipt_store_unsafe", {"status": "blocked", "reason_code": "receipt_store_unsafe"})
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def _read_json_bounded(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise BackupManagerBlocked("state_unsafe", {"status": "blocked", "reason_code": "state_unsafe"})
    stat = path.stat()
    if stat.st_size <= 0 or stat.st_size > MAX_JSON_BYTES:
        raise BackupManagerBlocked("state_invalid", {"status": "blocked", "reason_code": "state_invalid"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise BackupManagerBlocked("state_invalid", {"status": "blocked", "reason_code": "state_invalid"})
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    encoded = rendered.encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise BackupManagerBlocked("state_too_large", {"status": "blocked", "reason_code": "state_too_large"})
    if path.exists() and path.is_symlink():
        raise BackupManagerBlocked("state_unsafe", {"status": "blocked", "reason_code": "state_unsafe"})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _receipt_lock(root: Path) -> Iterator[None]:
    directory = _ensure_receipt_dir(root)
    lock_path = directory / ".lock"
    with _THREAD_LOCK:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            os.close(descriptor)


def receipt_path(root: Path, submission_id: str) -> Path:
    safe_submission = validate_submission_id(submission_id)
    return root / RECEIPT_DIR_NAME / f"{safe_submission}.json"


def artifact_state_path(root: Path, artifact_id: str) -> Path:
    return root / f"{validate_artifact_id(artifact_id)}{STATE_SUFFIX}"


def _receipt_files(root: Path) -> list[Path]:
    directory = root / RECEIPT_DIR_NAME
    if not directory.exists() or directory.is_symlink():
        return []
    paths: list[Path] = []
    for path in directory.glob("*.json"):
        if path.is_symlink() or not SUBMISSION_ID_RE.fullmatch(path.stem):
            continue
        paths.append(path)
    return paths


def _load_receipt(root: Path, submission_id: str) -> dict[str, Any] | None:
    path = receipt_path(root, submission_id)
    if not path.exists():
        return None
    payload = _read_json_bounded(path)
    if payload.get("schema") != BACKUP_RECEIPT_SCHEMA or payload.get("submission_id") != submission_id:
        raise BackupManagerBlocked("receipt_invalid", {"status": "blocked", "reason_code": "receipt_invalid"})
    return payload


def _safe_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    allowed = {
        "status",
        "availability_status",
        "integrity_status",
        "compatibility_status",
        "restore_validation_status",
        "delete_status",
        "deleted_count",
        "missing_count",
        "failed_count",
        "file_size",
        "db_backend",
        "checked_at",
        "validated_at",
        "video_archive_files_included",
        "video_archive_files_deleted",
        "video_archive_files_restored",
    }
    return {key: value for key, value in result.items() if key in allowed and isinstance(value, (str, int, float, bool, type(None)))}


def safe_receipt(receipt: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
    return {
        "operation_id": receipt.get("operation_id"),
        "submission_id": receipt.get("submission_id"),
        "kind": receipt.get("kind"),
        "artifact_id": receipt.get("artifact_id"),
        "state": receipt.get("state"),
        "phase": receipt.get("phase"),
        "created_at": receipt.get("created_at"),
        "started_at": receipt.get("started_at"),
        "updated_at": receipt.get("updated_at"),
        "finished_at": receipt.get("finished_at"),
        "retryable": bool(receipt.get("retryable")),
        "reason_code": receipt.get("reason_code"),
        "result": _safe_result(receipt.get("result")),
        "replayed": bool(replayed),
    }


def build_backup_operation_diagnostics(
    *,
    backup_root: str | Path | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or 20), MAX_PAGE_SIZE))
    root = configured_backup_root(backup_root)
    if not root.exists():
        return {
            "schema": BACKUP_RECEIPT_SCHEMA,
            "status": "no_receipt_store",
            "total_count": 0,
            "returned_count": 0,
            "invalid_count": 0,
            "limit": safe_limit,
            "has_more": False,
            "items": [],
        }
    if root.is_symlink() or not root.is_dir():
        return {
            "schema": BACKUP_RECEIPT_SCHEMA,
            "status": "unavailable",
            "reason_code": "backup_root_unsafe",
            "total_count": 0,
            "returned_count": 0,
            "invalid_count": 0,
            "limit": safe_limit,
            "has_more": False,
            "items": [],
        }
    receipts: list[dict[str, Any]] = []
    invalid_count = 0
    for path in _receipt_files(root):
        try:
            receipt = _read_json_bounded(path)
            if (
                receipt.get("schema") != BACKUP_RECEIPT_SCHEMA
                or receipt.get("submission_id") != path.stem
            ):
                raise BackupManagerBlocked("receipt_invalid")
            receipts.append(receipt)
        except Exception:
            invalid_count += 1
    receipts.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("submission_id") or ""),
        ),
        reverse=True,
    )
    page = [safe_receipt(receipt) for receipt in receipts[:safe_limit]]
    return {
        "schema": BACKUP_RECEIPT_SCHEMA,
        "status": "available" if receipts else "no_receipts",
        "total_count": len(receipts),
        "returned_count": len(page),
        "invalid_count": invalid_count,
        "limit": safe_limit,
        "has_more": len(receipts) > len(page),
        "items": page,
    }


def _write_receipt(root: Path, receipt: dict[str, Any]) -> None:
    _write_json_atomic(receipt_path(root, str(receipt["submission_id"])), receipt)


def _receipt_binding_matches(
    receipt: dict[str, Any],
    *,
    actor_key: str,
    kind: str,
    binding_artifact_id: str | None,
) -> bool:
    return (
        receipt.get("actor_key") == actor_key
        and receipt.get("kind") == kind
        and receipt.get("binding_artifact_id") == binding_artifact_id
        and receipt.get("binding_scope") == ("create_without_artifact" if kind == "create" else "exact_artifact")
    )


def _operation_conflicts(existing: dict[str, Any], *, kind: str, artifact_id: str | None) -> bool:
    if existing.get("state") not in ACTIVE_STATES:
        return False
    existing_kind = existing.get("kind")
    if kind == "create" and existing_kind == "create":
        return True
    if kind in {"check", "delete"} and existing_kind in {"check", "delete"}:
        return bool(artifact_id and existing.get("binding_artifact_id") == artifact_id)
    return False


def _read_artifact_state(root: Path, artifact_id: str) -> dict[str, Any] | None:
    path = artifact_state_path(root, artifact_id)
    if not path.exists():
        return None
    payload = _read_json_bounded(path)
    if payload.get("schema") != BACKUP_STATE_SCHEMA or payload.get("artifact_id") != artifact_id:
        raise BackupManagerBlocked("artifact_state_invalid", {"status": "blocked", "reason_code": "artifact_state_invalid"})
    return payload


def write_artifact_state(root: Path, artifact_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    safe_id = validate_artifact_id(artifact_id)
    current = _read_artifact_state(root, safe_id) or {
        "schema": BACKUP_STATE_SCHEMA,
        "artifact_id": safe_id,
        "created_at": utc_iso(),
    }
    updated = {**current, **payload, "schema": BACKUP_STATE_SCHEMA, "artifact_id": safe_id, "updated_at": utc_iso()}
    forbidden_values = {"queued", "running", "checking", "deleted"}
    rendered = json.dumps(updated, ensure_ascii=True, sort_keys=True)
    if any(f'"{value}"' in rendered for value in forbidden_values):
        raise BackupManagerBlocked(
            "artifact_state_active_value_forbidden",
            {"status": "blocked", "reason_code": "artifact_state_active_value_forbidden"},
        )
    _write_json_atomic(artifact_state_path(root, safe_id), updated)
    return updated


def _small_file_fingerprint(path: Path) -> str | None:
    if not path.exists() or path.is_symlink():
        return None
    stat = path.stat()
    if stat.st_size <= 0 or stat.st_size > MAX_JSON_BYTES:
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump_stat(path: Path) -> dict[str, int] | None:
    if not path.exists() or path.is_symlink():
        return None
    stat = path.stat()
    payload = {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    if hasattr(stat, "st_dev"):
        payload["device"] = int(stat.st_dev)
    if hasattr(stat, "st_ino"):
        payload["inode"] = int(stat.st_ino)
    return payload


def _manifest_labels(root: Path, artifact_id: str, manifest: dict[str, Any]) -> tuple[Path, Path, Path]:
    manifest_path = root / f"{artifact_id}.manifest.json"
    dump_name = Path(str(manifest.get("backup_file_label") or "")).name
    metadata_name = Path(str(manifest.get("metadata_file_label") or "")).name
    if dump_name not in {f"{artifact_id}.dump", f"{artifact_id}.sqlite3"} or metadata_name != f"{artifact_id}.metadata.json":
        raise BackupManagerBlocked("artifact_unsafe", {"status": "blocked", "reason_code": "artifact_unsafe"})
    dump_path = root / dump_name
    metadata_path = root / metadata_name
    for path in (manifest_path, dump_path, metadata_path):
        if path.is_symlink() or path.parent.resolve() != root.resolve():
            raise BackupManagerBlocked("artifact_unsafe", {"status": "blocked", "reason_code": "artifact_unsafe"})
    return manifest_path, dump_path, metadata_path


def artifact_version_evidence(
    root: Path,
    artifact_id: str,
    manifest: dict[str, Any],
    *,
    checksum_sha256: str | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    manifest_path, dump_path, metadata_path = _manifest_labels(root, artifact_id, manifest)
    return {
        "artifact_id": artifact_id,
        "observed_dump_size": int(dump_path.stat().st_size) if dump_path.exists() and not dump_path.is_symlink() else None,
        "checksum_sha256": checksum_sha256,
        "manifest_fingerprint": _small_file_fingerprint(manifest_path),
        "metadata_fingerprint": _small_file_fingerprint(metadata_path),
        "dump_stat": _dump_stat(dump_path),
        "artifact_context": {
            "db_backend": str(manifest.get("db_backend") or "unknown")[:40],
            "schema_version": _schema_version(manifest),
            "app_version": str(manifest.get("app_version") or "")[:80] or None,
            "app_build_version": str(manifest.get("app_build_version") or "")[:80] or None,
        },
        "validation_context": {
            "db_backend": str(context.get("db_backend") or "unknown")[:40],
            "schema_version": _int_or_none(context.get("schema_version")),
            "app_version": str(context.get("app_version") or APP_VERSION)[:80],
            "app_build_version": str(context.get("app_build_version") or APP_BUILD_VERSION)[:80],
        },
    }


def current_validation_context(db_backend: str | None = None) -> dict[str, Any]:
    return {
        "db_backend": str(db_backend or "unknown"),
        "schema_version": CURRENT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "app_build_version": APP_BUILD_VERSION,
    }


def _schema_version(manifest: dict[str, Any]) -> int | None:
    value = manifest.get("schema_version")
    if isinstance(value, dict):
        value = value.get("schema_version")
    return _int_or_none(value)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _evidence_matches(
    saved: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    compare_validation_context: bool,
) -> bool:
    if not isinstance(saved, dict) or not isinstance(current, dict):
        return False
    keys = (
        "artifact_id",
        "observed_dump_size",
        "manifest_fingerprint",
        "metadata_fingerprint",
        "dump_stat",
    )
    if any(saved.get(key) != current.get(key) for key in keys):
        return False
    if compare_validation_context and saved.get("validation_context") != current.get("validation_context"):
        return False
    return True


def _compatibility_status(manifest: dict[str, Any], *, current_context: dict[str, Any]) -> str:
    artifact_backend = str(manifest.get("db_backend") or "").lower()
    current_backend = str(current_context.get("db_backend") or "").lower()
    if artifact_backend not in {"postgresql", "sqlite"}:
        return "unsupported_backend"
    if current_backend not in {"postgresql", "sqlite"} or current_backend != artifact_backend:
        return "unsupported_backend"
    artifact_schema = _schema_version(manifest)
    current_schema = _int_or_none(current_context.get("schema_version"))
    if artifact_schema is None or current_schema is None:
        return "unknown"
    if artifact_schema > current_schema:
        return "newer_than_supported"
    if artifact_schema < current_schema:
        return "migration_required"
    return "compatible"


def _cheap_artifact_summary(
    root: Path,
    manifest_path: Path,
    *,
    current_context: dict[str, Any],
) -> dict[str, Any]:
    fallback_id = manifest_path.name.removesuffix(".manifest.json")
    base = {
        "artifact_id": fallback_id,
        "artifact_created_at": None,
        "artifact_schema_version": None,
        "db_backend": "unknown",
        "file_size": 0,
        "availability_status": "unsafe",
        "integrity_status": "not_checked",
        "compatibility_status": "unknown",
        "restore_validation_status": "not_performed",
        "delete_status": "blocked",
        "checked_at": None,
        "validated_at": None,
        "delete_supported": False,
    }
    try:
        manifest = _read_json_bounded(manifest_path)
        artifact_id = validate_artifact_id(str(manifest.get("backup_id") or fallback_id))
        if artifact_id != fallback_id:
            raise BackupManagerBlocked("artifact_unsafe")
        _, dump_path, metadata_path = _manifest_labels(root, artifact_id, manifest)
        dump_exists = dump_path.exists() and not dump_path.is_symlink()
        metadata_exists = metadata_path.exists() and not metadata_path.is_symlink()
        if dump_exists and metadata_exists:
            availability = "available"
        elif not dump_exists:
            availability = "missing"
        else:
            availability = "incomplete"
        current_evidence = artifact_version_evidence(
            root,
            artifact_id,
            manifest,
            checksum_sha256=None,
            context=current_context,
        )
        state = _read_artifact_state(root, artifact_id)
        integrity = state.get("integrity") if isinstance(state, dict) and isinstance(state.get("integrity"), dict) else {}
        restore_validation = (
            state.get("restore_validation")
            if isinstance(state, dict) and isinstance(state.get("restore_validation"), dict)
            else {}
        )
        integrity_status = str(integrity.get("status") or "not_checked")
        if integrity_status not in {"not_checked", "verified", "failed", "stale_evidence"}:
            integrity_status = "not_checked"
        if integrity_status in {"verified", "failed"} and not _evidence_matches(
            integrity.get("evidence"),
            current_evidence,
            compare_validation_context=False,
        ):
            integrity_status = "stale_evidence"
        restore_status = str(restore_validation.get("status") or "not_performed")
        if restore_status not in {"not_performed", "passed", "failed", "stale_evidence"}:
            restore_status = "not_performed"
        if restore_status in {"passed", "failed"} and not _evidence_matches(
            restore_validation.get("evidence"),
            current_evidence,
            compare_validation_context=True,
        ):
            restore_status = "stale_evidence"
        delete_status = str((state or {}).get("delete_status") or "allowed")
        if delete_status not in {"allowed", "blocked", "partial_retryable"}:
            delete_status = "allowed"
        return {
            **base,
            "artifact_id": artifact_id,
            "artifact_created_at": str(manifest.get("created_at") or "")[:80] or None,
            "artifact_schema_version": _schema_version(manifest),
            "db_backend": str(manifest.get("db_backend") or "unknown")[:40],
            "file_size": int(dump_path.stat().st_size) if dump_exists else 0,
            "availability_status": availability,
            "integrity_status": integrity_status,
            "compatibility_status": _compatibility_status(manifest, current_context=current_context),
            "restore_validation_status": restore_status,
            "delete_status": delete_status,
            "checked_at": integrity.get("checked_at"),
            "validated_at": restore_validation.get("validated_at"),
            "delete_supported": delete_status in {"allowed", "partial_retryable"},
        }
    except Exception:
        return base


def _created_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("artifact_created_at") or ""), str(item.get("artifact_id") or ""))


def build_backup_snapshot(
    *,
    backup_root: str | Path | None = None,
    db_backend: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 20), MAX_PAGE_SIZE))
    root = configured_backup_root(backup_root)
    context = current_validation_context(db_backend)
    if not root.exists():
        return {
            "schema": BACKUP_MANAGER_SCHEMA,
            "root_status": "missing",
            "status": "no_artifacts",
            "total_count": 0,
            "total_bytes": 0,
            "verified_compatible_count": 0,
            "available_count": 0,
            "offset": safe_offset,
            "limit": safe_limit,
            "has_more": False,
            "items": [],
        }
    if root.is_symlink() or not root.is_dir():
        return {
            "schema": BACKUP_MANAGER_SCHEMA,
            "root_status": "unsafe",
            "status": "unavailable",
            "total_count": 0,
            "total_bytes": 0,
            "verified_compatible_count": 0,
            "available_count": 0,
            "offset": safe_offset,
            "limit": safe_limit,
            "has_more": False,
            "items": [],
        }
    manifests = [path for path in root.glob("*.manifest.json") if path.parent.resolve() == root.resolve()]
    all_items = sorted(
        (_cheap_artifact_summary(root, path, current_context=context) for path in manifests),
        key=_created_sort_key,
        reverse=True,
    )
    total_count = len(all_items)
    total_bytes = sum(int(item.get("file_size") or 0) for item in all_items if item.get("availability_status") == "available")
    verified_compatible_count = sum(
        1
        for item in all_items
        if item.get("integrity_status") == "verified" and item.get("compatibility_status") == "compatible"
    )
    available_count = sum(1 for item in all_items if item.get("availability_status") == "available")
    page_items = all_items[safe_offset : safe_offset + safe_limit]
    return {
        "schema": BACKUP_MANAGER_SCHEMA,
        "root_status": "available",
        "status": "available" if total_count else "no_artifacts",
        "total_count": total_count,
        "total_bytes": total_bytes,
        "verified_compatible_count": verified_compatible_count,
        "available_count": available_count,
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more": safe_offset + len(page_items) < total_count,
        "items": page_items,
    }


def _prune_receipts(root: Path) -> None:
    terminal: list[tuple[str, Path]] = []
    for path in _receipt_files(root):
        try:
            receipt = _read_json_bounded(path)
        except Exception:
            continue
        if receipt.get("state") in TERMINAL_STATES:
            terminal.append((str(receipt.get("updated_at") or ""), path))
    for _, path in sorted(terminal, reverse=True)[MAX_RECEIPTS:]:
        try:
            path.unlink()
        except OSError:
            pass


def _delete_completion_proven(root: Path, artifact_id: str) -> bool:
    candidates = (
        root / f"{artifact_id}.manifest.json",
        root / f"{artifact_id}.metadata.json",
        root / f"{artifact_id}.dump",
        root / f"{artifact_id}.sqlite3",
        artifact_state_path(root, artifact_id),
    )
    return all(not path.exists() for path in candidates)


def _cleanup_receipt_disposable_target(receipt: dict[str, Any]) -> bool:
    recovery = receipt.get("recovery") if isinstance(receipt.get("recovery"), dict) else {}
    database_name = str(recovery.get("disposable_database_name") or "")
    if not database_name:
        return True
    if not TEMPORARY_VALIDATION_DB_RE.fullmatch(database_name):
        return False
    try:
        current_url = make_url(settings.database_url)
        if not str(current_url.get_backend_name()).lower().startswith("postgresql"):
            return False
        admin_url = current_url.set(database="postgres" if current_url.database != "postgres" else "template1")
        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
                remaining = connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                    {"database_name": database_name},
                ).scalar()
        finally:
            engine.dispose()
        return remaining is None
    except Exception:
        return False


def defer_backup_operation_until_cleanup(
    receipt: dict[str, Any],
    *,
    terminal_state: str,
    terminal_phase: str,
    terminal_retryable: bool,
    terminal_reason_code: str | None,
    terminal_result: dict[str, Any],
    backup_root: str | Path | None = None,
) -> dict[str, Any]:
    if terminal_state not in TERMINAL_STATES:
        raise BackupManagerBlocked(
            "operation_state_invalid",
            {"status": "blocked", "reason_code": "operation_state_invalid"},
        )
    root = configured_backup_root(backup_root)
    with _receipt_lock(root):
        current = _load_receipt(root, str(receipt.get("submission_id") or ""))
        recovery = current.get("recovery") if isinstance(current, dict) and isinstance(current.get("recovery"), dict) else {}
        database_name = str(recovery.get("disposable_database_name") or "")
        if (
            current is None
            or current.get("operation_id") != receipt.get("operation_id")
            or current.get("kind") != "check"
            or current.get("state") not in ACTIVE_STATES
            or not TEMPORARY_VALIDATION_DB_RE.fullmatch(database_name)
        ):
            raise BackupManagerBlocked(
                "receipt_not_active",
                {"status": "blocked", "reason_code": "receipt_not_active"},
            )
        now = utc_iso()
        updated = {
            **current,
            "state": "running",
            "phase": "cleanup_retry",
            "started_at": current.get("started_at") or now,
            "updated_at": now,
            "finished_at": None,
            "retryable": True,
            "reason_code": "temporary_validation_cleanup_failed",
            "result": _safe_result(terminal_result),
            "executor_instance_id": PROCESS_INSTANCE_ID,
            "recovery": {
                "disposable_database_name": database_name,
                "deferred_terminal": {
                    "state": terminal_state,
                    "phase": str(terminal_phase or terminal_state)[:80],
                    "retryable": bool(terminal_retryable),
                    "reason_code": str(terminal_reason_code)[:80] if terminal_reason_code else None,
                    "result": _safe_result(terminal_result),
                },
            },
        }
        _write_receipt(root, updated)
        return updated


def clear_backup_operation_disposable_target(
    receipt: dict[str, Any],
    *,
    backup_root: str | Path | None = None,
) -> dict[str, Any]:
    root = configured_backup_root(backup_root)
    with _receipt_lock(root):
        current = _load_receipt(root, str(receipt.get("submission_id") or ""))
        if current is None or current.get("operation_id") != receipt.get("operation_id"):
            raise BackupManagerBlocked(
                "receipt_not_found",
                {"status": "blocked", "reason_code": "receipt_not_found"},
            )
        updated = dict(current)
        updated.pop("recovery", None)
        updated["updated_at"] = utc_iso()
        _write_receipt(root, updated)
        return updated


def _deferred_terminal_after_cleanup(receipt: dict[str, Any]) -> dict[str, Any] | None:
    recovery = receipt.get("recovery") if isinstance(receipt.get("recovery"), dict) else {}
    deferred = recovery.get("deferred_terminal") if isinstance(recovery.get("deferred_terminal"), dict) else None
    if deferred is None:
        return None
    state = str(deferred.get("state") or "")
    phase = str(deferred.get("phase") or "")
    reason_code = deferred.get("reason_code")
    result = _safe_result(deferred.get("result"))
    if (
        state not in TERMINAL_STATES
        or not phase
        or len(phase) > 80
        or (reason_code is not None and (not isinstance(reason_code, str) or len(reason_code) > 80))
        or result is None
    ):
        return None
    return {
        "state": state,
        "phase": phase,
        "retryable": bool(deferred.get("retryable")),
        "reason_code": reason_code,
        "result": result,
    }


def _artifact_terminal_evidence_proven(
    root: Path,
    artifact_id: str,
    state: dict[str, Any],
    *,
    kind: str,
    operation_id: str,
) -> bool:
    manifest_path = root / f"{artifact_id}.manifest.json"
    try:
        manifest = _read_json_bounded(manifest_path)
        integrity = state.get("integrity") if isinstance(state.get("integrity"), dict) else {}
        saved_evidence = integrity.get("evidence") if isinstance(integrity.get("evidence"), dict) else None
        if kind == "check":
            validation = state.get("restore_validation") if isinstance(state.get("restore_validation"), dict) else {}
            validation_evidence = validation.get("evidence") if isinstance(validation.get("evidence"), dict) else None
            if validation.get("operation_id") == operation_id and validation_evidence is not None:
                saved_evidence = validation_evidence
        if not isinstance(saved_evidence, dict):
            return False
        context = saved_evidence.get("validation_context") if isinstance(saved_evidence.get("validation_context"), dict) else {}
        current = artifact_version_evidence(
            root,
            artifact_id,
            manifest,
            checksum_sha256=None,
            context=context,
        )
        return _evidence_matches(saved_evidence, current, compare_validation_context=False)
    except Exception:
        return False


def _reconcile_receipt(root: Path, receipt: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    if receipt.get("state") not in ACTIVE_STATES:
        return receipt
    if (
        not force
        and receipt.get("executor_instance_id") == PROCESS_INSTANCE_ID
        and receipt.get("phase") != "cleanup_retry"
    ):
        return receipt
    if receipt.get("kind") == "check":
        recovery = receipt.get("recovery") if isinstance(receipt.get("recovery"), dict) else {}
        if recovery.get("disposable_database_name"):
            if not _cleanup_receipt_disposable_target(receipt):
                receipt = {
                    **receipt,
                    "state": "running",
                    "phase": "cleanup_retry",
                    "updated_at": utc_iso(),
                    "finished_at": None,
                    "retryable": True,
                    "reason_code": "temporary_validation_cleanup_failed",
                }
                _write_receipt(root, receipt)
                return receipt
            deferred = _deferred_terminal_after_cleanup(receipt)
            if deferred is not None:
                now = utc_iso()
                receipt = {
                    **receipt,
                    **deferred,
                    "updated_at": now,
                    "finished_at": now,
                }
                receipt.pop("recovery", None)
                _write_receipt(root, receipt)
                return receipt
            receipt = dict(receipt)
            receipt.pop("recovery", None)
    operation_id = receipt.get("operation_id")
    artifact_id = receipt.get("artifact_id")
    proven_state: str | None = None
    result: dict[str, Any] | None = None
    if artifact_id and receipt.get("kind") in {"create", "check"}:
        try:
            state = _read_artifact_state(root, artifact_id)
        except Exception:
            state = None
        evidence_key = "create_evidence" if receipt.get("kind") == "create" else "last_check"
        evidence = state.get(evidence_key) if isinstance(state, dict) and isinstance(state.get(evidence_key), dict) else {}
        if (
            evidence.get("operation_id") == operation_id
            and evidence.get("outcome") in {"completed", "failed"}
            and _artifact_terminal_evidence_proven(
                root,
                artifact_id,
                state,
                kind=str(receipt.get("kind")),
                operation_id=str(operation_id),
            )
        ):
            proven_state = str(evidence["outcome"])
            result = _safe_result(evidence.get("result"))
    elif artifact_id and receipt.get("kind") == "delete" and _delete_completion_proven(root, artifact_id):
        proven_state = "completed"
        result = {"status": "deleted", "delete_status": "allowed"}
    now = utc_iso()
    if proven_state:
        receipt = {
            **receipt,
            "state": proven_state,
            "phase": "completed" if proven_state == "completed" else "failed",
            "updated_at": now,
            "finished_at": now,
            "retryable": proven_state != "completed",
            "reason_code": None if proven_state == "completed" else "operation_failed",
            "result": result,
        }
    else:
        receipt = {
            **receipt,
            "state": "interrupted",
            "phase": "interrupted",
            "updated_at": now,
            "finished_at": now,
            "retryable": True,
            "reason_code": "operation_interrupted",
        }
    receipt.pop("recovery", None)
    _write_receipt(root, receipt)
    return receipt


def begin_backup_operation(
    *,
    submission_id: str,
    kind: str,
    actor: Any,
    artifact_id: str | None = None,
    planned_artifact_id: str | None = None,
    backup_root: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    safe_submission = validate_submission_id(submission_id)
    if kind not in OPERATION_KINDS:
        raise BackupManagerBlocked("operation_kind_invalid", {"status": "blocked", "reason_code": "operation_kind_invalid"})
    binding_artifact_id = None if kind == "create" else validate_artifact_id(str(artifact_id or ""))
    operation_artifact_id = (
        validate_artifact_id(str(planned_artifact_id or "")) if kind == "create" else binding_artifact_id
    )
    root = configured_backup_root(backup_root)
    actor_key = actor_binding_key(actor)
    with _receipt_lock(root):
        existing = _load_receipt(root, safe_submission)
        if existing is not None:
            if not _receipt_binding_matches(
                existing,
                actor_key=actor_key,
                kind=kind,
                binding_artifact_id=binding_artifact_id,
            ):
                raise BackupManagerBlocked(
                    "submission_binding_conflict",
                    {"status": "blocked", "reason_code": "submission_binding_conflict"},
                )
            existing = _reconcile_receipt(root, existing)
            return existing, True
        for path in _receipt_files(root):
            try:
                other = _reconcile_receipt(root, _read_json_bounded(path))
            except Exception:
                continue
            if _operation_conflicts(other, kind=kind, artifact_id=binding_artifact_id):
                raise BackupManagerBlocked(
                    "operation_conflict",
                    {"status": "blocked", "reason_code": "operation_conflict"},
                )
        now = utc_iso()
        receipt = {
            "schema": BACKUP_RECEIPT_SCHEMA,
            "operation_id": f"backup-operation-{uuid.uuid4().hex}",
            "submission_id": safe_submission,
            "actor_key": actor_key,
            "kind": kind,
            "binding_scope": "create_without_artifact" if kind == "create" else "exact_artifact",
            "binding_artifact_id": binding_artifact_id,
            "artifact_id": operation_artifact_id,
            "state": "queued",
            "phase": "accepted",
            "created_at": now,
            "started_at": None,
            "updated_at": now,
            "finished_at": None,
            "retryable": False,
            "reason_code": None,
            "result": None,
            "executor_instance_id": PROCESS_INSTANCE_ID,
        }
        _write_receipt(root, receipt)
        _prune_receipts(root)
        return receipt, False


def update_backup_operation(
    receipt: dict[str, Any],
    *,
    state: str,
    phase: str,
    retryable: bool = False,
    reason_code: str | None = None,
    result: dict[str, Any] | None = None,
    backup_root: str | Path | None = None,
) -> dict[str, Any]:
    if state not in ACTIVE_STATES | TERMINAL_STATES:
        raise BackupManagerBlocked("operation_state_invalid", {"status": "blocked", "reason_code": "operation_state_invalid"})
    root = configured_backup_root(backup_root)
    with _receipt_lock(root):
        current = _load_receipt(root, str(receipt.get("submission_id") or ""))
        if current is None or current.get("operation_id") != receipt.get("operation_id"):
            raise BackupManagerBlocked("receipt_not_found", {"status": "blocked", "reason_code": "receipt_not_found"})
        now = utc_iso()
        updated = {
            **current,
            "state": state,
            "phase": str(phase or state)[:80],
            "started_at": current.get("started_at") or (now if state == "running" else None),
            "updated_at": now,
            "finished_at": now if state in TERMINAL_STATES else None,
            "retryable": bool(retryable),
            "reason_code": str(reason_code)[:80] if reason_code else None,
            "result": _safe_result(result),
            "executor_instance_id": PROCESS_INSTANCE_ID,
        }
        _write_receipt(root, updated)
        return updated


def record_backup_operation_disposable_target(
    receipt: dict[str, Any],
    *,
    database_name: str,
    backup_root: str | Path | None = None,
) -> dict[str, Any]:
    if not TEMPORARY_VALIDATION_DB_RE.fullmatch(str(database_name or "")):
        raise BackupManagerBlocked(
            "temporary_validation_target_invalid",
            {"status": "blocked", "reason_code": "temporary_validation_target_invalid"},
        )
    root = configured_backup_root(backup_root)
    with _receipt_lock(root):
        current = _load_receipt(root, str(receipt.get("submission_id") or ""))
        if (
            current is None
            or current.get("operation_id") != receipt.get("operation_id")
            or current.get("kind") != "check"
            or current.get("state") not in ACTIVE_STATES
        ):
            raise BackupManagerBlocked(
                "receipt_not_active",
                {"status": "blocked", "reason_code": "receipt_not_active"},
            )
        updated = {
            **current,
            "updated_at": utc_iso(),
            "recovery": {"disposable_database_name": database_name},
        }
        _write_receipt(root, updated)
        return updated


def get_backup_operation(
    *,
    submission_id: str,
    actor: Any,
    backup_root: str | Path | None = None,
    force_reconcile: bool = False,
) -> dict[str, Any]:
    safe_submission = validate_submission_id(submission_id)
    root = configured_backup_root(backup_root)
    with _receipt_lock(root):
        receipt = _load_receipt(root, safe_submission)
        if receipt is None:
            raise BackupManagerBlocked("receipt_not_found", {"status": "not_found", "reason_code": "receipt_not_found"})
        if receipt.get("actor_key") != actor_binding_key(actor):
            raise BackupManagerBlocked(
                "receipt_not_found",
                {"status": "not_found", "reason_code": "receipt_not_found"},
            )
        return _reconcile_receipt(root, receipt, force=force_reconcile)
