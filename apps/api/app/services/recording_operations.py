from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from app.core.config import settings

try:
    import fcntl
except ImportError:  # pragma: no cover - production and NAS test runners are Linux.
    fcntl = None


CONTROL_SUBDIR = "recording-operations"
ACTIVE_SUBDIR = "active-scopes"
OPERATION_SUBDIR = "operations"
MANIFEST_SUBDIR = "manifests"
GUARD_FILE = ".coordinator.lock"
SCOPE_LEASE_SECONDS = 5 * 60
OPERATION_LEASE_SECONDS = 5 * 60
PLAN_TTL_SECONDS = 10 * 60
EXPIRED_READY_GRACE_SECONDS = 5 * 60
TERMINAL_RETENTION_SECONDS = 24 * 60 * 60
STALE_RUNNING_RETENTION_SECONDS = 7 * 24 * 60 * 60
OPERATION_CLEANUP_SCAN_LIMIT = 256
OPERATION_CLEANUP_DELETE_LIMIT = 128
MANIFEST_CLEANUP_SCAN_LIMIT = 256
MANIFEST_CLEANUP_DELETE_LIMIT = 128
MANIFEST_ORPHAN_GRACE_SECONDS = 5 * 60
MANIFEST_STREAM_BATCH_SIZE = 100
MANIFEST_MAX_LINE_BYTES = 4096
TERMINAL_STATUSES = {"completed", "partial", "failed", "blocked"}
SAFE_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,95}$")
SAFE_ARCHIVE_ROOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SAFE_MANIFEST_NAME_RE = re.compile(r"^[a-f0-9]{64}\.ndjson$")
_THREAD_GUARD = threading.RLock()


class DestructiveScopeConflict(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail.get("reason") or "destructive_scope_conflict"))


class OperationStateConflict(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail.get("reason") or "operation_state_conflict"))


class ManifestValidationError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = str(reason or "deletion_plan_manifest_invalid")[:96]
        super().__init__(self.reason)


def _now_epoch() -> float:
    return time.time()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _control_root() -> Path:
    root = Path(settings.storage_install_control) / CONTROL_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _active_dir() -> Path:
    path = _control_root() / ACTIVE_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _operation_dir() -> Path:
    path = _control_root() / OPERATION_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_dir() -> Path:
    path = _control_root() / MANIFEST_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _safe_operation_id(value: str | None, *, generate: bool = False) -> str:
    text = str(value or "").strip()
    if not text and generate:
        return f"recording-op-{uuid.uuid4().hex}"
    if not SAFE_OPERATION_ID_RE.fullmatch(text):
        raise ValueError("invalid_operation_id")
    return text


def new_operation_id(prefix: str = "recording-op") -> str:
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]", "-", prefix).strip("-.")[:32] or "recording-op"
    return _safe_operation_id(f"{safe_prefix}-{uuid.uuid4().hex}")


def _file_name(operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _active_path(operation_id: str) -> Path:
    return _active_dir() / _file_name(operation_id)


def _operation_path(operation_id: str) -> Path:
    return _operation_dir() / _file_name(operation_id)


def _manifest_name(operation_id: str) -> str:
    operation_id = _safe_operation_id(operation_id)
    return f"{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()}.ndjson"


def _manifest_path(operation_id: str) -> Path:
    return _manifest_dir() / _manifest_name(operation_id)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_manifest_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError("deletion_plan_manifest_item_invalid")
    try:
        segment_id = int(value.get("segment_id") or 0)
        camera_id = int(value.get("camera_id") or 0)
        size_bytes = int(value.get("size_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError("deletion_plan_manifest_item_invalid") from exc
    archive_root_id = str(value.get("archive_root_id") or "").strip()
    relative_path = str(value.get("relative_path") or "").replace("\\", "/").lstrip("/")
    path_parts = [part for part in relative_path.split("/") if part]
    if (
        segment_id <= 0
        or camera_id <= 0
        or size_bytes < 0
        or not SAFE_ARCHIVE_ROOT_ID_RE.fullmatch(archive_root_id)
        or not relative_path
        or len(relative_path) > 1024
        or not path_parts
        or any(part in {".", ".."} for part in path_parts)
    ):
        raise ManifestValidationError("deletion_plan_manifest_item_invalid")
    return {
        "archive_root_id": archive_root_id,
        "camera_id": camera_id,
        "relative_path": relative_path,
        "segment_id": segment_id,
        "size_bytes": size_bytes,
    }


def _manifest_item_bytes(item: dict[str, Any]) -> bytes:
    return (json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _parse_manifest_line(raw_line: bytes) -> dict[str, Any]:
    if not raw_line.endswith(b"\n") or len(raw_line) > MANIFEST_MAX_LINE_BYTES:
        raise ManifestValidationError("deletion_plan_manifest_format_invalid")
    try:
        decoded = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("deletion_plan_manifest_format_invalid") from exc
    item = _normalize_manifest_item(decoded)
    if _manifest_item_bytes(item) != raw_line:
        raise ManifestValidationError("deletion_plan_manifest_noncanonical")
    return item


def _manifest_stream_facts(handle, *, progress: Callable[[], None] | None = None) -> dict[str, Any]:
    handle.seek(0)
    digest = hashlib.sha256()
    count = 0
    planned_bytes = 0
    file_bytes = 0
    while True:
        raw_line = handle.readline(MANIFEST_MAX_LINE_BYTES + 1)
        if not raw_line:
            break
        item = _parse_manifest_line(raw_line)
        digest.update(raw_line)
        count += 1
        planned_bytes += int(item["size_bytes"])
        file_bytes += len(raw_line)
        if progress is not None and count % MANIFEST_STREAM_BATCH_SIZE == 0:
            progress()
    handle.seek(0)
    return {
        "manifest_sha256": digest.hexdigest(),
        "manifest_count": count,
        "manifest_planned_bytes": planned_bytes,
        "manifest_file_bytes": file_bytes,
    }


def _write_manifest_temp(operation_id: str, planned_items: Iterable[dict[str, Any]]) -> tuple[Path, dict[str, Any], dict[str, list[Any]]]:
    operation_id = _safe_operation_id(operation_id)
    final_path = _manifest_path(operation_id)
    temp_path = final_path.with_name(f".{final_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    count = 0
    planned_bytes = 0
    file_bytes = 0
    max_segment_id = 0
    camera_ids: set[int] = set()
    root_ids: set[str] = set()
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            for value in planned_items:
                item = _normalize_manifest_item(value)
                raw_line = _manifest_item_bytes(item)
                handle.write(raw_line)
                digest.update(raw_line)
                count += 1
                planned_bytes += int(item["size_bytes"])
                file_bytes += len(raw_line)
                max_segment_id = max(max_segment_id, int(item["segment_id"]))
                camera_ids.add(int(item["camera_id"]))
                root_ids.add(str(item["archive_root_id"]))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return (
        temp_path,
        {
            "manifest_name": _manifest_name(operation_id),
            "manifest_sha256": digest.hexdigest(),
            "manifest_count": count,
            "manifest_planned_bytes": planned_bytes,
            "manifest_file_bytes": file_bytes,
            "cutoff_segment_id": max_segment_id,
        },
        {"camera_ids": sorted(camera_ids), "root_ids": sorted(root_ids)},
    )


def _record_manifest_facts(record: dict[str, Any]) -> dict[str, Any]:
    operation_id = _safe_operation_id(str(record.get("operation_id") or ""))
    expected_name = _manifest_name(operation_id)
    manifest_name = str(record.get("manifest_name") or "")
    manifest_sha256 = str(record.get("manifest_sha256") or "")
    if manifest_name != expected_name or not SAFE_MANIFEST_NAME_RE.fullmatch(manifest_name):
        raise ManifestValidationError("deletion_plan_manifest_identity_invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256):
        raise ManifestValidationError("deletion_plan_manifest_hash_invalid")
    try:
        return {
            "operation_id": operation_id,
            "manifest_name": manifest_name,
            "manifest_sha256": manifest_sha256,
            "manifest_count": max(0, int(record.get("manifest_count") or 0)),
            "manifest_planned_bytes": max(0, int(record.get("manifest_planned_bytes") or 0)),
            "manifest_file_bytes": max(0, int(record.get("manifest_file_bytes") or 0)),
        }
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError("deletion_plan_manifest_facts_invalid") from exc


@dataclass
class VerifiedDeletionManifest:
    handle: Any
    facts: dict[str, Any]

    def iter_batches(
        self,
        *,
        batch_size: int = MANIFEST_STREAM_BATCH_SIZE,
        progress: Callable[[], None] | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        self.handle.seek(0)
        batch: list[dict[str, Any]] = []
        bounded_batch_size = max(1, min(int(batch_size or MANIFEST_STREAM_BATCH_SIZE), 1000))
        while True:
            raw_line = self.handle.readline(MANIFEST_MAX_LINE_BYTES + 1)
            if not raw_line:
                break
            batch.append(_parse_manifest_line(raw_line))
            if len(batch) >= bounded_batch_size:
                if progress is not None:
                    progress()
                yield batch
                batch = []
        if batch:
            if progress is not None:
                progress()
            yield batch
        self.handle.seek(0)

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> "VerifiedDeletionManifest":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def open_verified_deletion_manifest(
    record: dict[str, Any],
    *,
    progress: Callable[[], None] | None = None,
) -> VerifiedDeletionManifest:
    expected = _record_manifest_facts(record)
    path = _manifest_dir() / expected["manifest_name"]
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise ManifestValidationError("deletion_plan_manifest_unavailable") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ManifestValidationError("deletion_plan_manifest_not_regular")
        handle = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        try:
            actual = _manifest_stream_facts(handle, progress=progress)
        except ManifestValidationError as exc:
            handle.close()
            raise ManifestValidationError("deletion_plan_manifest_verification_failed") from exc
        if any(actual[key] != expected[key] for key in (
            "manifest_sha256",
            "manifest_count",
            "manifest_planned_bytes",
            "manifest_file_bytes",
        )):
            handle.close()
            raise ManifestValidationError("deletion_plan_manifest_verification_failed")
        return VerifiedDeletionManifest(handle=handle, facts=actual)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def _coordinator_guard() -> Iterator[None]:
    path = _control_root() / GUARD_FILE
    with _THREAD_GUARD:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _bounded_ints(values: Any) -> list[int]:
    result: list[int] = []
    for value in values or []:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item > 0:
            result.append(item)
    return sorted(set(result))


def _bounded_strings(values: Any, *, max_length: int = 96) -> list[str]:
    result = {
        str(value).strip()[:max_length]
        for value in values or []
        if str(value or "").strip()
    }
    return sorted(result)


def normalize_scope(scope: dict[str, Any]) -> dict[str, Any]:
    scope_type = str(scope.get("type") or "segments")
    if scope_type not in {"all", "camera", "root", "segments"}:
        raise ValueError("invalid_destructive_scope")
    return {
        "type": scope_type,
        "segment_ids": _bounded_ints(scope.get("segment_ids")),
        "camera_ids": _bounded_ints(scope.get("camera_ids")),
        "root_ids": _bounded_strings(scope.get("root_ids")),
    }


def scope_for_segments(segments: list[Any]) -> dict[str, Any]:
    return normalize_scope(
        {
            "type": "segments",
            "segment_ids": [getattr(segment, "id", None) for segment in segments],
            "camera_ids": [getattr(segment, "camera_id", None) for segment in segments],
            "root_ids": [getattr(segment, "archive_root_id", None) for segment in segments],
        }
    )


def _intersects(left: list[Any], right: list[Any]) -> bool:
    return bool(set(left).intersection(right))


def scopes_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left = normalize_scope(left)
    right = normalize_scope(right)
    if "all" in {left["type"], right["type"]}:
        return True
    if left["type"] == "segments" and right["type"] == "segments":
        return _intersects(left["segment_ids"], right["segment_ids"])
    if left["type"] == "camera" or right["type"] == "camera":
        if _intersects(left["camera_ids"], right["camera_ids"]):
            return True
    if left["type"] == "root" or right["type"] == "root":
        if _intersects(left["root_ids"], right["root_ids"]):
            return True
    if left["type"] == "segments" or right["type"] == "segments":
        return _intersects(left["segment_ids"], right["segment_ids"])
    return False


def _active_claims_locked() -> list[tuple[Path, dict[str, Any]]]:
    now = _now_epoch()
    claims: list[tuple[Path, dict[str, Any]]] = []
    for path in _active_dir().glob("*.json"):
        payload = _read_json(path)
        if not payload or float(payload.get("lease_expires_at_epoch") or 0) <= now:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        claims.append((path, payload))
    return claims


def _remove_record_manifest_locked(record: dict[str, Any]) -> bool:
    if record.get("operation_type") != "manual_delete_plan" and not record.get("manifest_name"):
        return True
    operation_id = str(record.get("operation_id") or "")
    try:
        expected_path = _manifest_path(operation_id)
    except ValueError:
        return False
    manifest_name = str(record.get("manifest_name") or "")
    if manifest_name and manifest_name != expected_path.name:
        return False
    try:
        entry_stat = expected_path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not (stat.S_ISREG(entry_stat.st_mode) or stat.S_ISLNK(entry_stat.st_mode)):
        return False
    try:
        expected_path.unlink()
        return True
    except (FileNotFoundError, OSError):
        return not expected_path.exists()


def _cleanup_orphan_manifests_locked(*, now: float) -> dict[str, int]:
    result = {
        "manifest_scanned_count": 0,
        "manifest_deleted_count": 0,
        "manifest_preserved_count": 0,
    }
    try:
        entries = os.scandir(_manifest_dir())
    except OSError:
        return result
    with entries:
        for entry in entries:
            if result["manifest_scanned_count"] >= MANIFEST_CLEANUP_SCAN_LIMIT:
                break
            result["manifest_scanned_count"] += 1
            if not SAFE_MANIFEST_NAME_RE.fullmatch(entry.name):
                result["manifest_preserved_count"] += 1
                continue
            digest = entry.name.removesuffix(".ndjson")
            operation_path = _operation_dir() / f"{digest}.json"
            if operation_path.exists():
                result["manifest_preserved_count"] += 1
                continue
            try:
                age = max(0.0, now - float(entry.stat(follow_symlinks=False).st_mtime))
                removable = entry.is_file(follow_symlinks=False) or entry.is_symlink()
            except OSError:
                result["manifest_preserved_count"] += 1
                continue
            if (
                not removable
                or age < MANIFEST_ORPHAN_GRACE_SECONDS
                or result["manifest_deleted_count"] >= MANIFEST_CLEANUP_DELETE_LIMIT
            ):
                result["manifest_preserved_count"] += 1
                continue
            try:
                Path(entry.path).unlink()
                result["manifest_deleted_count"] += 1
            except (FileNotFoundError, OSError):
                result["manifest_preserved_count"] += 1
    return result


def _cleanup_operation_records_locked(
    *,
    now: float | None = None,
    exclude_operation_ids: set[str] | None = None,
) -> dict[str, int]:
    now = _now_epoch() if now is None else float(now)
    excluded = set(exclude_operation_ids or set())
    active_operation_ids = {
        str(payload.get("operation_id") or "")
        for _path, payload in _active_claims_locked()
        if str(payload.get("operation_id") or "")
    }
    result = {
        "scanned_count": 0,
        "deleted_count": 0,
        "preserved_count": 0,
        "corrupt_count": 0,
        "symlink_count": 0,
        "manifest_scanned_count": 0,
        "manifest_deleted_count": 0,
        "manifest_preserved_count": 0,
    }

    try:
        entries = os.scandir(_operation_dir())
    except OSError:
        return result
    with entries:
        for entry in entries:
            if result["scanned_count"] >= OPERATION_CLEANUP_SCAN_LIMIT:
                break
            result["scanned_count"] += 1
            if not entry.name.endswith(".json"):
                result["preserved_count"] += 1
                continue
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    result["symlink_count"] += 1
                    result["preserved_count"] += 1
                    continue
                if not entry.is_file(follow_symlinks=False):
                    result["preserved_count"] += 1
                    continue
                record = _read_json(path)
                operation_id = str(record.get("operation_id") or "")
                status = str(record.get("status") or "")
                if not record or not operation_id or operation_id in excluded:
                    if not record:
                        result["corrupt_count"] += 1
                    result["preserved_count"] += 1
                    continue

                delete_record = False
                if status == "ready":
                    expires_at = float(record.get("expires_at_epoch") or 0)
                    delete_record = bool(expires_at and expires_at + EXPIRED_READY_GRACE_SECONDS <= now)
                elif status in TERMINAL_STATUSES:
                    completed_at = float(record.get("completed_at_epoch") or 0)
                    if not completed_at:
                        completed_at = float(entry.stat(follow_symlinks=False).st_mtime)
                    delete_record = completed_at + TERMINAL_RETENTION_SECONDS <= now
                elif status == "running":
                    lease_expires_at = float(record.get("lease_expires_at_epoch") or 0)
                    delete_record = bool(
                        operation_id not in active_operation_ids
                        and lease_expires_at
                        and lease_expires_at + STALE_RUNNING_RETENTION_SECONDS <= now
                    )
                else:
                    result["preserved_count"] += 1
                    continue

                if not delete_record or result["deleted_count"] >= OPERATION_CLEANUP_DELETE_LIMIT:
                    result["preserved_count"] += 1
                    continue
                if not _remove_record_manifest_locked(record):
                    result["preserved_count"] += 1
                    continue
                path.unlink()
                result["deleted_count"] += 1
            except (OSError, ValueError, TypeError):
                result["preserved_count"] += 1
    result.update(_cleanup_orphan_manifests_locked(now=now))
    return result


def cleanup_operation_records(*, now: float | None = None) -> dict[str, int]:
    with _coordinator_guard():
        return _cleanup_operation_records_locked(now=now)


@dataclass
class ScopeLease:
    operation_id: str
    owner_token: str
    scope: dict[str, Any]

    def _assert_owned_locked(self) -> dict[str, Any]:
        current = _read_json(_active_path(self.operation_id))
        if (
            str(current.get("owner_token") or "") != self.owner_token
            or float(current.get("lease_expires_at_epoch") or 0) <= _now_epoch()
        ):
            raise DestructiveScopeConflict(
                {
                    "reason": "destructive_scope_lease_lost",
                    "operation_id": self.operation_id,
                    "retryable": True,
                }
            )
        return current

    def assert_owned(self) -> None:
        with _coordinator_guard():
            self._assert_owned_locked()

    def touch(self) -> None:
        with _coordinator_guard():
            path = _active_path(self.operation_id)
            current = self._assert_owned_locked()
            _write_json(
                path,
                {
                    **current,
                    "heartbeat_at": _now_iso(),
                    "lease_expires_at_epoch": _now_epoch() + SCOPE_LEASE_SECONDS,
                },
            )

    @contextmanager
    def mutation_guard(self) -> Iterator[None]:
        with _coordinator_guard():
            self._assert_owned_locked()
            yield

    def release(self) -> None:
        with _coordinator_guard():
            path = _active_path(self.operation_id)
            current = _read_json(path)
            if str(current.get("owner_token") or "") != self.owner_token:
                return
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _assert_operation_owned_locked(operation_id: str, owner_token: str) -> dict[str, Any]:
    operation_id = _safe_operation_id(operation_id)
    record = _read_json(_operation_path(operation_id))
    if (
        record.get("status") != "running"
        or str(record.get("owner_token") or "") != str(owner_token or "")
        or float(record.get("lease_expires_at_epoch") or 0) <= _now_epoch()
    ):
        raise OperationStateConflict(
            {"reason": "operation_lease_lost", "operation_id": operation_id, "retryable": True}
        )
    return record


@contextmanager
def operation_scope_mutation_guard(
    operation_id: str,
    owner_token: str,
    scope_lease: ScopeLease,
) -> Iterator[None]:
    with _coordinator_guard():
        _assert_operation_owned_locked(operation_id, owner_token)
        scope_lease._assert_owned_locked()
        yield


def acquire_scope_lease(operation_id: str, scope: dict[str, Any], *, purpose: str) -> ScopeLease:
    operation_id = _safe_operation_id(operation_id)
    normalized = normalize_scope(scope)
    owner_token = uuid.uuid4().hex
    with _coordinator_guard():
        for _path, active in _active_claims_locked():
            active_operation_id = str(active.get("operation_id") or "")
            if active_operation_id == operation_id:
                raise DestructiveScopeConflict(
                    {
                        "reason": "destructive_operation_already_running",
                        "operation_id": operation_id,
                        "conflicting_operation_id": active_operation_id,
                        "retryable": True,
                    }
                )
            if scopes_overlap(normalized, active.get("scope") or {}):
                raise DestructiveScopeConflict(
                    {
                        "reason": "destructive_scope_conflict",
                        "operation_id": operation_id,
                        "conflicting_operation_id": active_operation_id or None,
                        "conflicting_purpose": str(active.get("purpose") or "")[:80] or None,
                        "retryable": True,
                    }
                )
        _write_json(
            _active_path(operation_id),
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "owner_token": owner_token,
                "purpose": str(purpose or "destructive_operation")[:80],
                "scope": normalized,
                "acquired_at": _now_iso(),
                "heartbeat_at": _now_iso(),
                "lease_expires_at_epoch": _now_epoch() + SCOPE_LEASE_SECONDS,
            },
        )
    return ScopeLease(operation_id=operation_id, owner_token=owner_token, scope=normalized)


@contextmanager
def destructive_scope_guard(operation_id: str, scope: dict[str, Any], *, purpose: str) -> Iterator[ScopeLease]:
    lease = acquire_scope_lease(operation_id, scope, purpose=purpose)
    try:
        yield lease
    finally:
        lease.release()


def _actor_fingerprint(actor: Any) -> dict[str, Any]:
    return {
        "user_id": int(getattr(actor, "id", 0) or 0),
        "role_at_creation": str(getattr(actor, "role", "") or "")[:50],
    }


def actor_matches(record: dict[str, Any], actor: Any) -> bool:
    stored_actor = record.get("actor") if isinstance(record.get("actor"), dict) else {}
    return int(stored_actor.get("user_id") or 0) > 0 and int(stored_actor.get("user_id") or 0) == int(
        getattr(actor, "id", 0) or 0
    )


def operation_fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def create_deletion_plan(
    *,
    actor: Any,
    scope_type: str,
    planned_items: Iterable[dict[str, Any]],
    camera_label: str | None = None,
) -> dict[str, Any]:
    if scope_type not in {"all", "camera"}:
        raise ValueError("invalid_deletion_plan_scope")
    operation_id = new_operation_id("recording-plan")
    now = _now_epoch()
    temp_path, manifest_facts, scope_facts = _write_manifest_temp(operation_id, planned_items)
    scope = normalize_scope(
        {
            "type": scope_type,
            "segment_ids": [],
            "camera_ids": scope_facts["camera_ids"] if scope_type == "camera" else [],
            "root_ids": scope_facts["root_ids"] if scope_type == "camera" else [],
        }
    )
    payload = {
        "schema_version": 2,
        "operation_id": operation_id,
        "operation_type": "manual_delete_plan",
        "actor": _actor_fingerprint(actor),
        "required_permission": "delete_recordings",
        "scope": scope,
        "cutoff_segment_id": int(manifest_facts["cutoff_segment_id"]),
        "planned_count": int(manifest_facts["manifest_count"]),
        "planned_bytes": int(manifest_facts["manifest_planned_bytes"]),
        "manifest_name": manifest_facts["manifest_name"],
        "manifest_sha256": manifest_facts["manifest_sha256"],
        "manifest_count": int(manifest_facts["manifest_count"]),
        "manifest_planned_bytes": int(manifest_facts["manifest_planned_bytes"]),
        "manifest_file_bytes": int(manifest_facts["manifest_file_bytes"]),
        "camera_label": str(camera_label or "")[:255] or None,
        "status": "ready",
        "created_at": _now_iso(),
        "expires_at_epoch": now + PLAN_TTL_SECONDS,
        "result": None,
    }
    final_path = _manifest_path(operation_id)
    published = False
    manifest_finalized = False
    try:
        with _coordinator_guard():
            _cleanup_operation_records_locked(now=now)
            if _operation_path(operation_id).exists() or final_path.exists():
                raise OperationStateConflict(
                    {"reason": "deletion_plan_identity_collision", "operation_id": operation_id, "retryable": True}
                )
            os.replace(temp_path, final_path)
            manifest_finalized = True
            try:
                final_path.chmod(0o400)
            except OSError:
                pass
            with open_verified_deletion_manifest(payload):
                pass
            try:
                _write_json(_operation_path(operation_id), payload)
            except Exception:
                try:
                    final_path.unlink()
                except FileNotFoundError:
                    pass
                raise
            published = True
        return payload
    finally:
        if not published:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            if manifest_finalized:
                try:
                    final_path.unlink()
                except FileNotFoundError:
                    pass


def read_operation(operation_id: str) -> dict[str, Any]:
    operation_id = _safe_operation_id(operation_id)
    with _coordinator_guard():
        _cleanup_operation_records_locked()
        return _read_json(_operation_path(operation_id))


def _claim_record_locked(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
    owner_token = uuid.uuid4().hex
    claimed = {
        **record,
        "status": "running",
        "owner_token": owner_token,
        "started_at": record.get("started_at") or _now_iso(),
        "heartbeat_at": _now_iso(),
        "lease_expires_at_epoch": _now_epoch() + OPERATION_LEASE_SECONDS,
    }
    return claimed, owner_token


def claim_deletion_plan(operation_id: str, *, actor: Any) -> dict[str, Any]:
    operation_id = _safe_operation_id(operation_id)
    path = _operation_path(operation_id)
    with _coordinator_guard():
        _cleanup_operation_records_locked()
        record = _read_json(path)
        if not record or record.get("operation_type") != "manual_delete_plan":
            raise OperationStateConflict({"reason": "deletion_plan_not_found", "operation_id": operation_id, "retryable": False})
        if not actor_matches(record, actor):
            raise OperationStateConflict({"reason": "deletion_plan_actor_mismatch", "operation_id": operation_id, "retryable": False})
        status = str(record.get("status") or "")
        if status in TERMINAL_STATUSES:
            return {"state": "terminal", "record": record, "result": record.get("result") or {}}
        try:
            manifest_facts = _record_manifest_facts(record)
            manifest_path = _manifest_dir() / manifest_facts["manifest_name"]
            manifest_stat = manifest_path.lstat()
            if not stat.S_ISREG(manifest_stat.st_mode):
                raise ManifestValidationError("deletion_plan_manifest_not_regular")
        except (ManifestValidationError, FileNotFoundError, OSError) as exc:
            reason = getattr(exc, "reason", "deletion_plan_manifest_unavailable")
            raise OperationStateConflict(
                {"reason": reason, "operation_id": operation_id, "retryable": False}
            ) from exc
        now = _now_epoch()
        if status == "ready" and float(record.get("expires_at_epoch") or 0) <= now:
            claimed, owner_token = _claim_record_locked(record)
            _write_json(path, claimed)
            return {"state": "expired", "record": claimed, "owner_token": owner_token}
        if status == "running" and float(record.get("lease_expires_at_epoch") or 0) > now:
            return {"state": "running", "record": record}
        claimed, owner_token = _claim_record_locked(record)
        _write_json(path, claimed)
        return {"state": "claimed", "record": claimed, "owner_token": owner_token}


def cancel_deletion_plan(operation_id: str, *, actor: Any) -> dict[str, Any]:
    operation_id = _safe_operation_id(operation_id)
    path = _operation_path(operation_id)
    with _coordinator_guard():
        _cleanup_operation_records_locked(exclude_operation_ids={operation_id})
        record = _read_json(path)
        generic = {"ok": True, "status": "cancelled", "operation_id": operation_id, "cancelled": False}
        if (
            not record
            or record.get("operation_type") != "manual_delete_plan"
            or not actor_matches(record, actor)
        ):
            return generic
        status = str(record.get("status") or "")
        if status == "ready":
            if not _remove_record_manifest_locked(record):
                raise OperationStateConflict(
                    {"reason": "deletion_plan_manifest_cleanup_failed", "operation_id": operation_id, "retryable": True}
                )
            try:
                path.unlink()
            except FileNotFoundError:
                return generic
            return {"ok": True, "status": "cancelled", "operation_id": operation_id, "cancelled": True}
        if status == "running":
            raise OperationStateConflict(
                {"reason": "deletion_plan_already_running", "operation_id": operation_id, "retryable": False}
            )
        return generic


def claim_exact_operation(
    operation_id: str | None,
    *,
    actor: Any,
    operation_type: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    operation_id = _safe_operation_id(operation_id, generate=True)
    path = _operation_path(operation_id)
    with _coordinator_guard():
        _cleanup_operation_records_locked()
        record = _read_json(path)
        if record:
            if not actor_matches(record, actor) or record.get("operation_type") != operation_type or record.get("request_fingerprint") != request_fingerprint:
                raise OperationStateConflict({"reason": "operation_identity_mismatch", "operation_id": operation_id, "retryable": False})
            status = str(record.get("status") or "")
            if status in TERMINAL_STATUSES:
                return {"state": "terminal", "record": record, "result": record.get("result") or {}}
            if status == "running" and float(record.get("lease_expires_at_epoch") or 0) > _now_epoch():
                return {"state": "running", "record": record}
        else:
            record = {
                "schema_version": 1,
                "operation_id": operation_id,
                "operation_type": operation_type,
                "request_fingerprint": request_fingerprint,
                "actor": _actor_fingerprint(actor),
                "created_at": _now_iso(),
                "status": "ready",
                "result": None,
            }
        claimed, owner_token = _claim_record_locked(record)
        _write_json(path, claimed)
        return {"state": "claimed", "record": claimed, "owner_token": owner_token}


def touch_operation(operation_id: str, owner_token: str) -> None:
    operation_id = _safe_operation_id(operation_id)
    path = _operation_path(operation_id)
    with _coordinator_guard():
        _cleanup_operation_records_locked(exclude_operation_ids={operation_id})
        record = _assert_operation_owned_locked(operation_id, owner_token)
        _write_json(
            path,
            {
                **record,
                "heartbeat_at": _now_iso(),
                "lease_expires_at_epoch": _now_epoch() + OPERATION_LEASE_SECONDS,
            },
        )


def finish_operation(operation_id: str, owner_token: str, result: dict[str, Any]) -> dict[str, Any]:
    operation_id = _safe_operation_id(operation_id)
    status = str(result.get("status") or "failed")
    if status not in TERMINAL_STATUSES:
        raise ValueError("operation_terminal_status_required")
    path = _operation_path(operation_id)
    with _coordinator_guard():
        _cleanup_operation_records_locked(exclude_operation_ids={operation_id})
        record = _assert_operation_owned_locked(operation_id, owner_token)
        completed = {
            **record,
            "status": status,
            "completed_at": _now_iso(),
            "completed_at_epoch": _now_epoch(),
            "lease_expires_at_epoch": 0,
            "result": {**result, "operation_id": operation_id},
        }
        _write_json(path, completed)
        return completed


class LeaseHeartbeat:
    def __init__(
        self,
        *,
        scope_lease: ScopeLease,
        operation_id: str | None = None,
        owner_token: str | None = None,
        interval_seconds: float | None = None,
        progress_timeout_seconds: float | None = None,
    ) -> None:
        if bool(operation_id) != bool(owner_token):
            raise ValueError("operation_heartbeat_identity_incomplete")
        self.scope_lease = scope_lease
        self.operation_id = _safe_operation_id(operation_id) if operation_id else None
        self.owner_token = str(owner_token or "") or None
        lease_ttl = float(min(SCOPE_LEASE_SECONDS, OPERATION_LEASE_SECONDS if operation_id else SCOPE_LEASE_SECONDS))
        default_interval = min(30.0, max(0.05, lease_ttl / 3.0))
        self.interval_seconds = max(0.01, float(interval_seconds or default_interval))
        default_progress_timeout = max(self.interval_seconds * 2.0, min(240.0, lease_ttl * 0.8))
        self.progress_timeout_seconds = max(
            self.interval_seconds * 2.0,
            float(progress_timeout_seconds or default_progress_timeout),
        )
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._last_progress = time.monotonic()
        self._failure: BaseException | None = None
        self._thread: threading.Thread | None = None

    def _set_failure(self, exc: BaseException) -> None:
        with self._state_lock:
            if self._failure is None:
                self._failure = exc
        self._stop.set()

    def _renew(self) -> None:
        if self.operation_id and self.owner_token:
            touch_operation(self.operation_id, self.owner_token)
        self.scope_lease.touch()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            with self._state_lock:
                stalled = time.monotonic() - self._last_progress > self.progress_timeout_seconds
            if stalled:
                self._set_failure(
                    OperationStateConflict(
                        {
                            "reason": "operation_progress_stalled",
                            "operation_id": self.operation_id or self.scope_lease.operation_id,
                            "retryable": True,
                        }
                    )
                )
                return
            try:
                self._renew()
            except BaseException as exc:  # Propagated on the owner execution thread.
                self._set_failure(exc)
                return

    def assert_healthy(self) -> None:
        with self._state_lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def progress(self) -> None:
        self.assert_healthy()
        with self._state_lock:
            self._last_progress = time.monotonic()

    def __enter__(self) -> "LeaseHeartbeat":
        self._renew()
        self._thread = threading.Thread(
            target=self._run,
            name=f"km-vms-lease-heartbeat-{self.scope_lease.operation_id[:24]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2.0))
            if self._thread.is_alive():
                self._set_failure(
                    OperationStateConflict(
                        {
                            "reason": "operation_heartbeat_shutdown_failed",
                            "operation_id": self.operation_id or self.scope_lease.operation_id,
                            "retryable": False,
                        }
                    )
                )
        if exc_type is None:
            self.assert_healthy()
