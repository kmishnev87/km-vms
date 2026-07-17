from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any, Iterator

from app.models.recording import ArchiveRoot
from app.services.recording_storage import (
    KMVMS_RECORDINGS_NAMESPACE,
    archive_root_runtime_access_state,
    archive_root_runtime_path,
)


MIGRATION_INTERNAL_NAMESPACE = ".km-vms-internal/migration"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class StorageFilesystemError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_relative_path(value: str, *, allow_internal: bool = False) -> str:
    raw = str(value or "").replace("\\", "/").lstrip("/")
    if not raw or len(raw) > 1024:
        raise StorageFilesystemError("archive_relative_path_invalid")
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise StorageFilesystemError("archive_relative_path_invalid")
    normalized = "/".join(parts)
    if is_migration_internal_relative(normalized) and not allow_internal:
        raise StorageFilesystemError("migration_internal_namespace_forbidden")
    return normalized


def is_migration_internal_relative(value: str | None) -> bool:
    raw = str(value or "").replace("\\", "/").lstrip("/")
    return raw == MIGRATION_INTERNAL_NAMESPACE or raw.startswith(f"{MIGRATION_INTERNAL_NAMESPACE}/")


def migration_internal_relative(plan_id: str, item_id: str, kind: str) -> str:
    if kind not in {"target-temp", "source-quarantine"}:
        raise StorageFilesystemError("migration_internal_kind_invalid")
    for value in (plan_id, item_id):
        if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in value):
            raise StorageFilesystemError("migration_internal_identity_invalid")
    return f"{MIGRATION_INTERNAL_NAMESPACE}/{plan_id}/{item_id}/{kind}"


def root_snapshot(root: ArchiveRoot, *, require_write: bool) -> dict[str, Any]:
    access = archive_root_runtime_access_state(root)
    if access.get("read_access_state") != "available":
        raise StorageFilesystemError("archive_root_not_readable")
    if require_write and access.get("write_access_state") != "available":
        raise StorageFilesystemError("archive_root_not_writable")
    if not root.physical_identity:
        raise StorageFilesystemError("archive_root_physical_identity_unknown")
    runtime_path = archive_root_runtime_path(root)
    try:
        root_stat = runtime_path.stat()
        namespace_path = runtime_path / str(root.storage_namespace or KMVMS_RECORDINGS_NAMESPACE)
        namespace_stat = namespace_path.stat()
        volume = os.statvfs(runtime_path)
    except (OSError, RuntimeError) as exc:
        raise StorageFilesystemError("archive_root_runtime_unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISDIR(namespace_stat.st_mode):
        raise StorageFilesystemError("archive_root_runtime_not_directory")
    snapshot_key = _digest(
        {
            "root_id": str(root.id),
            "root_path": str(root.root_path or ""),
            "namespace": str(root.storage_namespace or KMVMS_RECORDINGS_NAMESPACE),
            "physical_identity": str(root.physical_identity),
            "retired_at": root.retired_at.isoformat() if root.retired_at else None,
        }
    )
    access_identity = _digest(
        {
            "snapshot_key": snapshot_key,
            "root_device": int(root_stat.st_dev),
            "root_inode": int(root_stat.st_ino),
            "namespace_device": int(namespace_stat.st_dev),
            "namespace_inode": int(namespace_stat.st_ino),
        }
    )
    return {
        "root_id": str(root.id),
        "physical_identity": str(root.physical_identity),
        "snapshot_key": snapshot_key,
        "access_identity": access_identity,
        "root_device": int(root_stat.st_dev),
        "root_inode": int(root_stat.st_ino),
        "namespace_device": int(namespace_stat.st_dev),
        "namespace_inode": int(namespace_stat.st_ino),
        "total_bytes": int(volume.f_blocks * volume.f_frsize),
        "free_bytes": int(volume.f_bavail * volume.f_frsize),
    }


def assert_root_snapshot(root: ArchiveRoot, expected: dict[str, Any], *, require_write: bool) -> dict[str, Any]:
    current = root_snapshot(root, require_write=require_write)
    for key in ("root_id", "physical_identity", "snapshot_key", "access_identity"):
        if str(current.get(key) or "") != str(expected.get(key) or ""):
            raise StorageFilesystemError("archive_root_identity_changed")
    return current


def archive_roots_overlap(first: ArchiveRoot, second: ArchiveRoot) -> bool:
    try:
        first_path = archive_root_runtime_path(first).resolve(strict=True)
        second_path = archive_root_runtime_path(second).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StorageFilesystemError("archive_root_runtime_unavailable") from exc
    return bool(
        first_path == second_path
        or first_path in second_path.parents
        or second_path in first_path.parents
    )


@contextmanager
def verified_root_fd(
    root: ArchiveRoot,
    expected: dict[str, Any],
    *,
    require_write: bool,
) -> Iterator[int]:
    current = assert_root_snapshot(root, expected, require_write=require_write)
    flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
    try:
        descriptor = os.open(archive_root_runtime_path(root), flags)
    except OSError as exc:
        raise StorageFilesystemError("archive_root_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        if int(opened.st_dev) != int(current["root_device"]) or int(opened.st_ino) != int(current["root_inode"]):
            raise StorageFilesystemError("archive_root_identity_changed")
        yield descriptor
    finally:
        os.close(descriptor)


def _open_directory_chain(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            try:
                next_descriptor = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o750, dir_fd=descriptor)
                next_descriptor = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=descriptor)
            opened = os.fstat(next_descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_descriptor)
                raise StorageFilesystemError("archive_path_component_not_directory")
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def relative_parent_fd(
    root_fd: int,
    relative_path: str,
    *,
    create: bool,
    allow_internal: bool,
) -> Iterator[tuple[int, str]]:
    normalized = normalize_relative_path(relative_path, allow_internal=allow_internal)
    parts = PurePosixPath(normalized).parts
    parent_fd = _open_directory_chain(root_fd, parts[:-1], create=create)
    try:
        yield parent_fd, parts[-1]
    finally:
        os.close(parent_fd)


def stat_relative(root_fd: int, relative_path: str, *, allow_internal: bool = False) -> os.stat_result:
    with relative_parent_fd(root_fd, relative_path, create=False, allow_internal=allow_internal) as (parent_fd, name):
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(result.st_mode):
            raise StorageFilesystemError("archive_object_not_regular_file")
        return result


def open_relative_read(root_fd: int, relative_path: str, *, allow_internal: bool = False) -> int:
    with relative_parent_fd(root_fd, relative_path, create=False, allow_internal=allow_internal) as (parent_fd, name):
        descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise StorageFilesystemError("archive_object_not_regular_file")
    return descriptor


def create_relative_exclusive(root_fd: int, relative_path: str, *, mode: int = 0o640) -> int:
    with relative_parent_fd(root_fd, relative_path, create=True, allow_internal=True) as (parent_fd, name):
        return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode, dir_fd=parent_fd)


def rename_relative(
    source_root_fd: int,
    source_relative: str,
    target_root_fd: int,
    target_relative: str,
    *,
    source_internal: bool,
    target_internal: bool,
) -> None:
    with relative_parent_fd(
        source_root_fd,
        source_relative,
        create=False,
        allow_internal=source_internal,
    ) as (source_parent, source_name):
        with relative_parent_fd(
            target_root_fd,
            target_relative,
            create=True,
            allow_internal=target_internal,
        ) as (target_parent, target_name):
            try:
                os.stat(target_name, dir_fd=target_parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise StorageFilesystemError("archive_target_collision")
            os.rename(
                source_name,
                target_name,
                src_dir_fd=source_parent,
                dst_dir_fd=target_parent,
            )
            fsync_directory(target_parent)
            if source_parent != target_parent:
                fsync_directory(source_parent)


def unlink_relative(root_fd: int, relative_path: str, *, allow_internal: bool) -> None:
    with relative_parent_fd(root_fd, relative_path, create=False, allow_internal=allow_internal) as (parent_fd, name):
        os.unlink(name, dir_fd=parent_fd)
        fsync_directory(parent_fd)


def fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        # Some network filesystems do not support directory fsync. File fsync
        # and durable DB evidence remain mandatory; the caller records failure
        # when the actual rename/unlink cannot be completed.
        pass


def remove_empty_internal_parents(root_fd: int, relative_path: str) -> None:
    normalized = normalize_relative_path(relative_path, allow_internal=True)
    if not is_migration_internal_relative(normalized):
        return
    parts = PurePosixPath(normalized).parts
    for directory_index in range(len(parts) - 2, 1, -1):
        parent_parts = parts[:directory_index]
        name = parts[directory_index]
        parent_fd = _open_directory_chain(root_fd, parent_parts, create=False)
        try:
            os.rmdir(name, dir_fd=parent_fd)
            fsync_directory(parent_fd)
        except OSError:
            return
        finally:
            os.close(parent_fd)
