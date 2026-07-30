from __future__ import annotations

import json
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import fcntl
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.config import settings


MAX_CONTROL_BYTES = 64 * 1024
MAX_BACKUP_RECEIPTS = 256
ACTIVE_BACKUP_STATES = {"queued", "running"}
ACTIVE_UPDATE_STATES = {"admitted", "claimed"}
ACTIVE_UPDATE_STATUSES = {
    "queued",
    "preflight",
    "applying",
    "rebuilding",
    "restarting",
    "staging",
    "activating",
    "rolling_back",
}
ACTIVE_SCHEMA_STATES = {"accepted", "running", "prepared", "recovering", "migrating"}
ACTIVE_RESTORE_STATES = {"admitted", "claimed"}
_THREAD_LOCK = threading.RLock()


class MaintenanceAdmissionBlocked(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code or "maintenance_admission_blocked")[:80]
        super().__init__(self.code)


def maintenance_control_root() -> Path:
    configured = str(getattr(settings, "maintenance_control_root", "") or "").strip()
    if (
        configured in {"", "/maintenance-control"}
        and str(settings.update_control_root) != "/update-control"
    ):
        return Path(settings.update_control_root).parent / "maintenance-control"
    return Path(configured or "/maintenance-control")


def maintenance_admission_lock_path() -> Path:
    return maintenance_control_root() / "maintenance-admission.lock"


def manual_schema_operation_path() -> Path:
    return maintenance_control_root() / "manual-schema-operation.json"


@contextmanager
def maintenance_admission_guard() -> Iterator[None]:
    root = maintenance_control_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        root_info = root.lstat()
    except OSError as exc:
        raise MaintenanceAdmissionBlocked("maintenance_control_root_unavailable") from exc
    if root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
        raise MaintenanceAdmissionBlocked("maintenance_control_root_unsafe")
    lock_path = maintenance_admission_lock_path()
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _THREAD_LOCK:
        lock_fd: int | None = None
        try:
            lock_fd = os.open(lock_path, flags, 0o600)
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode):
                raise OSError("admission lock is not a regular file")
        except OSError as exc:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            raise MaintenanceAdmissionBlocked("maintenance_admission_lock_unsafe") from exc
        with os.fdopen(lock_fd, "r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_bounded_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    descriptor: int | None = None
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None, "missing"
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size <= 1
            or info.st_size > MAX_CONTROL_BYTES
        ):
            return None, "invalid"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(MAX_CONTROL_BYTES + 1)
        if len(raw) > MAX_CONTROL_BYTES:
            return None, "invalid"
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None, "invalid"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return (payload, "valid") if isinstance(payload, dict) else (None, "invalid")


def write_bounded_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if len(rendered.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise MaintenanceAdmissionBlocked("maintenance_control_payload_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent_info = path.parent.lstat()
        target_info = path.lstat()
    except FileNotFoundError:
        target_info = None
        parent_info = path.parent.lstat()
    except OSError as exc:
        raise MaintenanceAdmissionBlocked("maintenance_control_write_failed") from exc
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or (
            target_info is not None
            and (
                stat.S_ISLNK(target_info.st_mode)
                or not stat.S_ISREG(target_info.st_mode)
            )
        )
    ):
        raise MaintenanceAdmissionBlocked("maintenance_control_path_unsafe")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor: int | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise MaintenanceAdmissionBlocked("maintenance_control_write_failed") from exc


def _update_state() -> str:
    control = Path(settings.update_control_root)
    request, request_state = read_bounded_json(control / "update-request.json")
    if request_state == "invalid":
        return "unavailable"
    if request:
        state = request.get("state")
        if state in ACTIVE_UPDATE_STATES:
            return "active"
        if state is None and request.get("intent") == "apply_update":
            status_payload, status_state = read_bounded_json(control / "update-status.json")
            if status_state == "invalid":
                return "unavailable"
            if not status_payload or status_payload.get("status") in ACTIVE_UPDATE_STATUSES:
                return "active"
    status_payload, status_state = read_bounded_json(control / "update-status.json")
    if status_state == "invalid":
        return "unavailable"
    if status_payload and status_payload.get("status") in ACTIVE_UPDATE_STATUSES:
        return "active"
    return "idle"


def _restore_state() -> str:
    root = Path(getattr(settings, "restore_control_root", "/restore-control"))
    request, state = read_bounded_json(root / "restore-request.json")
    if state == "invalid":
        return "unavailable"
    if request and request.get("state") in ACTIVE_RESTORE_STATES:
        return "active"
    return "idle"


def _backup_state(backup_root: str | Path | None = None) -> str:
    root = Path(
        backup_root
        or os.getenv("KMVMS_DB_BACKUP_ROOT")
        or settings.kmvms_db_backup_root
        or "/storage/backups/db"
    )
    receipt_dir = root / ".kmvms-backup-operations"
    try:
        from app.services.backup_manager import reconcile_backup_operations

        reconcile_backup_operations(backup_root=root)
    except (ImportError, RuntimeError):
        pass
    try:
        if not receipt_dir.exists():
            return "idle"
        if receipt_dir.is_symlink() or not receipt_dir.is_dir():
            return "unavailable"
        paths = sorted(receipt_dir.glob("*.json"), key=lambda item: item.name)
    except OSError:
        return "unavailable"
    if len(paths) > MAX_BACKUP_RECEIPTS:
        return "unavailable"
    for path in paths:
        payload, state = read_bounded_json(path)
        if state == "invalid":
            return "unavailable"
        if payload and payload.get("state") in ACTIVE_BACKUP_STATES:
            return "active"
    return "idle"


def _schema_state(db: Session | None) -> str:
    manual, manual_state = read_bounded_json(manual_schema_operation_path())
    if manual_state == "invalid":
        return "unavailable"
    if manual and manual.get("state") in ACTIVE_SCHEMA_STATES:
        return "active"
    if db is None:
        return "idle"
    try:
        bind = db.get_bind()
        if not inspect(bind).has_table("schema_migration_control"):
            return "idle"
        row = db.execute(
            text("SELECT state FROM schema_migration_control WHERE id='current' LIMIT 1")
        ).mappings().first()
    except Exception:
        return "unavailable"
    if row and row.get("state") in {"prepared", "recovering", "migrating"}:
        return "active"
    return "idle"


def maintenance_flow_states(
    *,
    db: Session | None = None,
    backup_root: str | Path | None = None,
) -> dict[str, str]:
    return {
        "update": _update_state(),
        "backup": _backup_state(backup_root),
        "schema": _schema_state(db),
        "restore": _restore_state(),
    }


def assert_no_maintenance_conflicts(
    selected_flow: str,
    *,
    db: Session | None = None,
    backup_root: str | Path | None = None,
) -> dict[str, str]:
    if selected_flow not in {"update", "backup", "schema", "restore"}:
        raise MaintenanceAdmissionBlocked("maintenance_flow_invalid")
    states = maintenance_flow_states(db=db, backup_root=backup_root)
    for flow, state in states.items():
        if flow == selected_flow:
            continue
        if state == "unavailable":
            raise MaintenanceAdmissionBlocked(f"{flow}_state_unavailable")
        if state == "active":
            raise MaintenanceAdmissionBlocked(f"{flow}_operation_active")
    return states
