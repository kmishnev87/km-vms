"""Bounded source inventory and copy, with symlink/path rejection."""
from __future__ import annotations
import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Sequence
from .contracts import PRODUCT_PATHS, TRUSTED_REQUIRED_SOURCE_PATHS, MAX_INVENTORY_ENTRIES, MAX_INVENTORY_BYTES, SlotError, canonical_json

def _source_path_excluded(relative: Path) -> bool:
    normalized = relative.as_posix()
    if normalized == ".env.example":
        return False
    parts = relative.parts
    blocked_directories = {
        ".git",
        "data",
        "logs",
        "log",
        "node_modules",
        ".next",
        "__pycache__",
        ".pytest_cache",
        "coverage",
        "dist",
        "build",
        "service-artifacts",
        "service_artifacts",
        ".ssh",
    }
    if any(part in blocked_directories for part in parts):
        return True
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return True
    name = relative.name
    lowered = name.lower()
    if lowered in {"id_rsa", "id_ed25519"}:
        return True
    if lowered.endswith(
        (
            ".zip",
            ".tar",
            ".tar.gz",
            ".tgz",
            ".rar",
            ".7z",
            ".pem",
            ".key",
            ".p12",
            ".pfx",
            ".crt",
            ".csr",
            ".token",
            ".secret",
            ".dump",
            ".sql",
            ".sqlite",
            ".db",
        )
    ):
        return True
    if "credential" in lowered or "password" in lowered:
        return True
    if (
        lowered.endswith(("token.txt", "secret.txt", "secret.json"))
        or lowered.startswith(
            ("github-token", "auth-token", "authorization-token")
        )
    ):
        return True
    return False


def _safe_relative(relative: str) -> Path:
    path = Path(relative)
    if (
        not relative
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != relative
    ):
        raise SlotError("product_path_invalid", "Product inventory path is unsafe.")
    return path


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_INVENTORY_BYTES:
                    raise SlotError(
                        "inventory_too_large",
                        "Product source inventory exceeds its byte bound.",
                    )
                digest.update(chunk)
    except SlotError:
        raise
    except OSError as exc:
        raise SlotError("inventory_read_failed", "Product source file cannot be read.") from exc
    return digest.hexdigest(), size


def product_inventory(
    root: Path,
    *,
    required_paths: Sequence[str] = TRUSTED_REQUIRED_SOURCE_PATHS,
) -> dict[str, Any]:
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise SlotError("source_unavailable", "Product source root is unavailable.") from exc
    if not root.is_dir():
        raise SlotError("source_unavailable", "Product source root is not a directory.")
    for relative in required_paths:
        path = root / _safe_relative(relative)
        if not path.exists() or path.is_symlink():
            raise SlotError(
                "source_incomplete",
                f"Required product source path is missing or unsafe: {relative}",
            )

    entries: list[dict[str, Any]] = []
    total_bytes = 0

    def add_entry(entry: dict[str, Any]) -> None:
        if len(entries) >= MAX_INVENTORY_ENTRIES:
            raise SlotError(
                "inventory_too_large",
                "Product source inventory exceeds its entry bound.",
            )
        entries.append(entry)

    for relative_text in PRODUCT_PATHS:
        relative = _safe_relative(relative_text)
        if _source_path_excluded(relative):
            continue
        start = root / relative
        try:
            start_info = start.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(start_info.st_mode):
            raise SlotError("source_symlink_forbidden", "Product source contains a symlink.")
        if stat.S_ISREG(start_info.st_mode):
            file_digest, file_size = _hash_file(start)
            total_bytes += file_size
            if total_bytes > MAX_INVENTORY_BYTES:
                raise SlotError(
                    "inventory_too_large",
                    "Product source inventory exceeds its byte bound.",
                )
            add_entry(
                {
                    "path": relative.as_posix(),
                    "type": "file",
                    "mode": stat.S_IMODE(start_info.st_mode) & ~0o222,
                    "size": file_size,
                    "sha256": file_digest,
                }
            )
            continue
        if not stat.S_ISDIR(start_info.st_mode):
            raise SlotError(
                "source_special_file_forbidden",
                "Product source contains a non-regular filesystem object.",
            )
        for current, dirnames, filenames in os.walk(start, topdown=True, followlinks=False):
            current_path = Path(current)
            current_info = current_path.lstat()
            if stat.S_ISLNK(current_info.st_mode) or not stat.S_ISDIR(current_info.st_mode):
                raise SlotError("source_symlink_forbidden", "Product source contains an unsafe directory.")
            current_relative = current_path.relative_to(root).as_posix()
            add_entry(
                {
                    "path": current_relative,
                    "type": "directory",
                    "mode": stat.S_IMODE(current_info.st_mode) & ~0o222,
                }
            )
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not _source_path_excluded(
                    (current_path / name).relative_to(root)
                )
            )
            filenames = sorted(
                name
                for name in filenames
                if not _source_path_excluded(
                    (current_path / name).relative_to(root)
                )
            )
            for name in list(dirnames):
                child = current_path / name
                child_info = child.lstat()
                if stat.S_ISLNK(child_info.st_mode):
                    raise SlotError("source_symlink_forbidden", "Product source contains a symlink.")
                if not stat.S_ISDIR(child_info.st_mode):
                    raise SlotError(
                        "source_special_file_forbidden",
                        "Product source contains an unsafe directory entry.",
                    )
            for name in filenames:
                child = current_path / name
                child_info = child.lstat()
                if stat.S_ISLNK(child_info.st_mode):
                    raise SlotError("source_symlink_forbidden", "Product source contains a symlink.")
                if not stat.S_ISREG(child_info.st_mode):
                    raise SlotError(
                        "source_special_file_forbidden",
                        "Product source contains a non-regular file.",
                    )
                file_digest, file_size = _hash_file(child)
                total_bytes += file_size
                if total_bytes > MAX_INVENTORY_BYTES:
                    raise SlotError(
                        "inventory_too_large",
                        "Product source inventory exceeds its byte bound.",
                    )
                add_entry(
                    {
                        "path": child.relative_to(root).as_posix(),
                        "type": "file",
                        "mode": stat.S_IMODE(child_info.st_mode) & ~0o222,
                        "size": file_size,
                        "sha256": file_digest,
                    }
                )

    entries.sort(key=lambda item: (item["path"], item["type"]))
    return {
        "schema_version": 1,
        "sha256": hashlib.sha256(canonical_json(entries)).hexdigest(),
        "entry_count": len(entries),
        "file_count": sum(item["type"] == "file" for item in entries),
        "total_bytes": total_bytes,
    }


def _copy_product_source(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True)

    def ignored(current: str, names: list[str]) -> set[str]:
        current_path = Path(current)
        return {
            name
            for name in names
            if _source_path_excluded(
                (current_path / name).relative_to(source)
            )
        }

    for relative_text in PRODUCT_PATHS:
        relative = _safe_relative(relative_text)
        if _source_path_excluded(relative):
            continue
        source_path = source / relative
        try:
            info = source_path.lstat()
        except FileNotFoundError:
            continue
        destination_path = destination / relative
        if stat.S_ISLNK(info.st_mode):
            raise SlotError("source_symlink_forbidden", "Product source contains a symlink.")
        try:
            if stat.S_ISDIR(info.st_mode):
                shutil.copytree(
                    source_path,
                    destination_path,
                    # Preserve a raced-in symlink as a symlink so the mandatory
                    # post-copy inventory rejects it instead of following it.
                    symlinks=True,
                    copy_function=shutil.copy2,
                    ignore=ignored,
                )
            elif stat.S_ISREG(info.st_mode):
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination_path)
            else:
                raise SlotError(
                    "source_special_file_forbidden",
                    "Product source contains a non-regular filesystem object.",
                )
        except SlotError:
            raise
        except OSError as exc:
            raise SlotError(
                "source_copy_failed",
                "Product source could not be materialized in release staging.",
            ) from exc
