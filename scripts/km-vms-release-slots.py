#!/usr/bin/env python3
"""KM VMS release-slot foundation.

Stage 6.6.1 keeps mutable user/runtime state in the stable install root and
materializes immutable product source in bounded release slots.  This module
owns slot paths, inventories, manifests and the atomic pointer primitive.

The CLI prepares/finalizes slots and exposes the strict journal primitives
used by the Stage C activation owner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


RUNTIME_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 1
EVIDENCE_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1

RUNTIME_RELATIVE = Path("data/update-runtime")
SLOTS_RELATIVE = RUNTIME_RELATIVE / "slots"
STAGING_RELATIVE = RUNTIME_RELATIVE / "staging"
ACTIVE_RELATIVE = RUNTIME_RELATIVE / "active"
JOURNAL_RELATIVE = Path("data/update-control/activation-journal.json")
MANIFEST_NAME = "slot-manifest.json"
CANDIDATE_NAME = "candidate-state.json"
RUNTIME_OVERRIDE_NAME = "docker-compose.runtime-override.yml"
SOURCE_DIR_NAME = "source"

PRODUCT_PATHS = (
    "apps",
    "deploy",
    "docs",
    "release",
    "scripts",
    "docker-compose.yml",
    "docker-compose.pytest.yml",
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    ".km-vms-release.json",
    ".km-vms-source.json",
)
TRUSTED_REQUIRED_SOURCE_PATHS = (
    "apps/api",
    "apps/web",
    "apps/recorder",
    "apps/update-helper",
    "deploy/nginx/default.conf",
    "release/km-vms-release.json",
    "scripts/install.sh",
    "scripts/km-vms-compose-common.sh",
    "scripts/km-vms-permission-gate.sh",
    "scripts/km-vms-update-helper-bridge.py",
    "scripts/km-vms-release-slots.py",
    "docker-compose.yml",
)
LEGACY_REQUIRED_SOURCE_PATHS = (
    "apps/api",
    "apps/web",
    "apps/recorder",
    "apps/update-helper",
    "deploy/nginx/default.conf",
    "release/km-vms-release.json",
    "scripts/install.sh",
    "scripts/km-vms-compose-common.sh",
    "scripts/update.sh",
    "docker-compose.yml",
)
ADOPTED_REQUIRED_IMAGE_SERVICES = frozenset(
    {
        "api",
        "recorder",
        "web",
        "nginx",
        "update-helper",
    }
)
TARGET_REQUIRED_IMAGE_SERVICES = ADOPTED_REQUIRED_IMAGE_SERVICES | frozenset(
    {
        "schema-update",
        "update-status-reader",
        "update-retry-admission",
    }
)
TARGET_BUILT_IMAGE_SERVICES = frozenset(
    {
        "schema-update",
        "api",
        "recorder",
        "web",
        "update-helper",
        "update-status-reader",
        "update-retry-admission",
    }
)
REQUIRED_ADOPTED_COMPOSE_SERVICES = ADOPTED_REQUIRED_IMAGE_SERVICES | frozenset(
    {"postgres", "redis"}
)

KNOWN_COMPOSE_SERVICES = frozenset(
    {
        "postgres",
        "redis",
        "update-helper-bootstrap",
        "update-status-reader",
        "update-retry-admission",
        "schema-update",
        "api",
        "setup-helper",
        "update-helper",
        "recorder",
        "web",
        "nginx",
    }
)

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
REQUEST_RE = re.compile(r"^(?:update|stage609|terminal)-[0-9a-f]{32}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
SERVICE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TRUSTED_SLOT_RE = re.compile(r"^release-[0-9a-f]{40}$")
ADOPTED_SLOT_RE = re.compile(r"^adopted-[0-9a-f]{64}$")
SLOT_RE = re.compile(
    r"^(?:release-[0-9a-f]{40}|adopted-[0-9a-f]{64})$"
)
MACHINE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")

ACTIVATION_PHASES = frozenset(
    {
        "target_prepared",
        "quiescing",
        "schema_preparing",
        "activating",
        "verifying_target",
        "committing_target",
        "rolling_back",
        "completed",
        "failed_rolled_back",
        "blocked",
    }
)
TERMINAL_ACTIVATION_PHASES = frozenset(
    {"completed", "failed_rolled_back", "blocked"}
)
ACTIVATION_TRANSITIONS = {
    "target_prepared": {
        "quiescing",
        "schema_preparing",
        "activating",
        "blocked",
    },
    "quiescing": {"schema_preparing", "activating", "blocked"},
    "schema_preparing": {"activating", "blocked"},
    "activating": {"verifying_target", "rolling_back", "blocked"},
    "verifying_target": {
        "committing_target",
        "rolling_back",
        "blocked",
    },
    "committing_target": {"completed", "rolling_back", "blocked"},
    "rolling_back": {"failed_rolled_back", "blocked"},
    "completed": set(),
    "failed_rolled_back": set(),
    "blocked": set(),
}

MAX_INVENTORY_ENTRIES = 200_000
MAX_INVENTORY_BYTES = 4 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024


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


class SlotError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(payload: Any) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise SlotError("json_invalid", "Evidence cannot be serialized safely.") from exc


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise SlotError(
            "directory_sync_failed",
            "Release-slot directory cannot be opened for durable synchronization.",
        ) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise SlotError(
            "directory_sync_failed",
            "Release-slot directory cannot be synchronized durably.",
        ) from exc
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = canonical_json(payload) + b"\n"
    if len(rendered) > MAX_JSON_BYTES:
        raise SlotError("json_too_large", "Release-slot evidence is too large.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except SlotError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SlotError(
            "evidence_write_failed",
            "Release-slot evidence could not be persisted atomically.",
        ) from exc


def read_json(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SlotError("evidence_missing", "Required release-slot evidence is missing.")
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size <= 1
        or info.st_size > MAX_JSON_BYTES
    ):
        raise SlotError("evidence_invalid", "Release-slot evidence is not a safe regular file.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SlotError("evidence_invalid", "Release-slot evidence is invalid.") from exc
    if type(payload) is not dict:
        raise SlotError("evidence_invalid", "Release-slot evidence must be a JSON object.")
    return payload


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_app_dir(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or any(char in str(path) for char in ("\x00", "\r", "\n"))
    ):
        raise SlotError("app_dir_invalid", "APP_DIR must be absolute.")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SlotError("app_dir_invalid", "APP_DIR is unavailable.") from exc
    if not resolved.is_dir() or not (resolved / "data").is_dir():
        raise SlotError("app_dir_invalid", "APP_DIR is not a KM VMS stable install root.")
    return resolved


def _ensure_plain_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SlotError(
            "runtime_path_invalid",
            "Release-slot runtime path is not a plain directory.",
        )


def ensure_layout(app_dir: Path) -> dict[str, Path]:
    runtime = app_dir / RUNTIME_RELATIVE
    slots = app_dir / SLOTS_RELATIVE
    staging = app_dir / STAGING_RELATIVE
    for directory in (runtime, slots, staging):
        _ensure_plain_directory(directory)
    if not _is_relative_to(runtime.resolve(), (app_dir / "data").resolve()):
        raise SlotError(
            "runtime_path_invalid",
            "Release-slot runtime escaped stable APP_DIR/data.",
        )
    runtime_device = runtime.stat().st_dev
    if slots.stat().st_dev != runtime_device or staging.stat().st_dev != runtime_device:
        raise SlotError(
            "runtime_filesystem_mismatch",
            "Release slots, staging and pointer must share one filesystem.",
        )
    return {
        "runtime": runtime,
        "slots": slots,
        "staging": staging,
        "active": app_dir / ACTIVE_RELATIVE,
        "journal": app_dir / JOURNAL_RELATIVE,
    }


def require_request_id(value: str) -> str:
    normalized = str(value or "").lower()
    if not REQUEST_RE.fullmatch(normalized):
        raise SlotError("request_id_invalid", "Canonical update request ID is invalid.")
    return normalized


def trusted_slot_id(commit: str) -> str:
    normalized = str(commit or "").lower()
    if not COMMIT_RE.fullmatch(normalized):
        raise SlotError("trusted_commit_invalid", "Trusted target commit must be exact 40-hex.")
    return f"release-{normalized}"


def adopted_slot_id(inventory_digest: str) -> str:
    normalized = str(inventory_digest or "").lower()
    if not DIGEST_RE.fullmatch(normalized):
        raise SlotError("inventory_digest_invalid", "Adopted inventory digest is invalid.")
    return f"adopted-{normalized}"


def require_slot_id(value: str, *, target: bool = False) -> str:
    normalized = str(value or "").lower()
    expected = TRUSTED_SLOT_RE if target else SLOT_RE
    if not expected.fullmatch(normalized):
        code = "target_slot_invalid" if target else "slot_id_invalid"
        raise SlotError(code, "Release slot ID is invalid.")
    return normalized


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


def _validate_release_descriptor(
    source: Path,
    *,
    trusted_commit: str,
    declared_version: str,
) -> dict[str, Any]:
    descriptor = read_json(source / "release/km-vms-release.json")
    assert descriptor is not None
    descriptor_commit = descriptor.get("commit_sha")
    if (
        descriptor.get("schema_version") != 1
        or descriptor.get("product") != "KM VMS"
        or descriptor.get("version") != declared_version
        or descriptor.get("tag") != f"v{declared_version}"
        or descriptor.get("source_kind") != "github-release"
        or descriptor.get("source_ref") != f"v{declared_version}"
        or type(descriptor.get("source_repo")) is not str
        or not descriptor["source_repo"]
        or (
            descriptor_commit is not None
            and str(descriptor_commit).lower() != trusted_commit
        )
    ):
        raise SlotError(
            "release_descriptor_invalid",
            "Trusted target release descriptor does not match the admitted release.",
        )
    return descriptor


def _write_target_identities(
    source: Path,
    *,
    descriptor: dict[str, Any],
    trusted_commit: str,
    prepared_at: str,
) -> None:
    release_identity = {
        "schema_version": 1,
        "product": "KM VMS",
        "version": descriptor["version"],
        "title": str(descriptor.get("title") or ""),
        "summary": str(descriptor.get("summary") or ""),
        "release_channel": str(descriptor.get("release_channel") or ""),
        "source_kind": descriptor["source_kind"],
        "source_repo": descriptor["source_repo"],
        "source_ref": descriptor["source_ref"],
        "commit_sha": trusted_commit,
        "installed_at": prepared_at,
        "installed_by": "slot_engine",
        "metadata_status": "complete",
        "metadata_source": "trusted_release_slot",
    }
    source_identity = {
        "schema_version": 1,
        "recorded_at": prepared_at,
        "source_kind": "github-tarball",
        "github_repo": descriptor["source_repo"],
        "ref": descriptor["source_ref"],
        "commit_sha": trusted_commit,
    }
    atomic_write_json(source / ".km-vms-release.json", release_identity)
    atomic_write_json(source / ".km-vms-source.json", source_identity)


def _candidate_root(layout: dict[str, Path], request_id: str) -> Path:
    path = layout["staging"] / request_id / "candidate"
    resolved_parent = path.parent.resolve(strict=False)
    if not _is_relative_to(resolved_parent, layout["staging"].resolve()):
        raise SlotError("staging_path_invalid", "Candidate staging escaped its bounded root.")
    return path


def _remove_candidate(path: Path, staging_root: Path) -> None:
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        return
    if resolved_parent.parent != staging_root.resolve() or path.name != "candidate":
        raise SlotError("staging_path_invalid", "Refusing to clean an unbounded candidate path.")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise SlotError("staging_path_invalid", "Candidate staging path is unsafe.")
        shutil.rmtree(path)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def stage_target(
    app_dir: Path,
    source_dir: Path,
    *,
    request_id: str,
    trusted_commit: str,
    declared_version: str,
) -> dict[str, Any]:
    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    request_id = require_request_id(request_id)
    trusted_commit = trusted_slot_id(trusted_commit).removeprefix("release-")
    if not VERSION_RE.fullmatch(str(declared_version or "")):
        raise SlotError("release_version_invalid", "Target release version is invalid.")
    source_input = Path(source_dir)
    try:
        source_info = source_input.lstat()
        source_dir = source_input.resolve(strict=True)
    except OSError as exc:
        raise SlotError(
            "target_source_invalid",
            "Trusted target source is unavailable.",
        ) from exc
    if stat.S_ISLNK(source_info.st_mode) or not source_dir.is_dir():
        raise SlotError("target_source_invalid", "Trusted target source is unsafe.")

    slot_id = trusted_slot_id(trusted_commit)
    final_path = layout["slots"] / slot_id
    if final_path.exists():
        manifest = validate_slot(final_path, expected_slot_id=slot_id)
        if (
            manifest["kind"] != "trusted_release"
            or manifest["declared_identity"]
            != {"version": declared_version, "commit": trusted_commit}
        ):
            raise SlotError(
                "slot_conflict",
                "Existing trusted release slot contradicts the admitted target.",
            )
        return {
            "status": "reused",
            "slot_id": slot_id,
            "slot_path": str(final_path),
            "source_path": str(final_path / SOURCE_DIR_NAME),
            "inventory": manifest["source_inventory"],
            "manifest": manifest,
        }

    candidate_root = _candidate_root(layout, request_id)
    _remove_candidate(candidate_root, layout["staging"])
    candidate_source = candidate_root / SOURCE_DIR_NAME
    candidate_root.mkdir(parents=True, mode=0o700)
    try:
        source_before = product_inventory(
            source_dir,
            required_paths=TRUSTED_REQUIRED_SOURCE_PATHS,
        )
        _copy_product_source(source_dir, candidate_source)
        source_after = product_inventory(
            source_dir,
            required_paths=TRUSTED_REQUIRED_SOURCE_PATHS,
        )
        if source_before != source_after:
            raise SlotError(
                "source_changed_during_materialization",
                "Trusted target source changed while it was being materialized.",
            )
        descriptor = _validate_release_descriptor(
            candidate_source,
            trusted_commit=trusted_commit,
            declared_version=declared_version,
        )
        prepared_at = utc_now()
        _write_target_identities(
            candidate_source,
            descriptor=descriptor,
            trusted_commit=trusted_commit,
            prepared_at=prepared_at,
        )
        inventory = product_inventory(
            candidate_source,
            required_paths=TRUSTED_REQUIRED_SOURCE_PATHS,
        )
        candidate = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "kind": "trusted_release",
            "request_id": request_id,
            "slot_id": slot_id,
            "declared_identity": {
                "version": declared_version,
                "commit": trusted_commit,
            },
            "official_source_match": True,
            "source_inventory": inventory,
            "prepared_at": prepared_at,
        }
        atomic_write_json(candidate_root / CANDIDATE_NAME, candidate)
        _fsync_directory(candidate_root)
    except Exception:
        _remove_candidate(candidate_root, layout["staging"])
        raise
    return {
        "status": "staged",
        "slot_id": slot_id,
        "candidate_path": str(candidate_root),
        "source_path": str(candidate_source),
        "inventory": inventory,
    }


def stage_adopted(
    app_dir: Path,
    *,
    request_id: str,
    declared_version: str,
    declared_commit: str,
) -> dict[str, Any]:
    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    request_id = require_request_id(request_id)
    declared_commit = str(declared_commit or "").lower()
    if not VERSION_RE.fullmatch(str(declared_version or "")):
        raise SlotError("release_version_invalid", "Installed release version is invalid.")
    if not COMMIT_RE.fullmatch(declared_commit):
        raise SlotError("installed_commit_invalid", "Installed release commit is invalid.")

    candidate_root = _candidate_root(layout, request_id)
    _remove_candidate(candidate_root, layout["staging"])
    candidate_source = candidate_root / SOURCE_DIR_NAME
    candidate_root.mkdir(parents=True, mode=0o700)
    try:
        source_before = product_inventory(
            app_dir,
            required_paths=LEGACY_REQUIRED_SOURCE_PATHS,
        )
        _copy_product_source(app_dir, candidate_source)
        source_after = product_inventory(
            app_dir,
            required_paths=LEGACY_REQUIRED_SOURCE_PATHS,
        )
        copied = product_inventory(
            candidate_source,
            required_paths=LEGACY_REQUIRED_SOURCE_PATHS,
        )
        if source_before != source_after or source_before != copied:
            raise SlotError(
                "source_changed_during_materialization",
                "Installed product source was not stable during snapshot materialization.",
            )
        slot_id = adopted_slot_id(copied["sha256"])
        final_path = layout["slots"] / slot_id
        if final_path.exists():
            manifest = validate_slot(final_path, expected_slot_id=slot_id)
            if (
                manifest["kind"] != "adopted_pre_update_snapshot"
                or manifest["declared_identity"]
                != {"version": declared_version, "commit": declared_commit}
            ):
                raise SlotError(
                    "slot_conflict",
                    "Existing adopted slot contradicts current installed identity.",
                )
            _remove_candidate(candidate_root, layout["staging"])
            return {
                "status": "reused",
                "slot_id": slot_id,
                "slot_path": str(final_path),
                "source_path": str(final_path / SOURCE_DIR_NAME),
                "inventory": manifest["source_inventory"],
                "manifest": manifest,
            }
        candidate = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "kind": "adopted_pre_update_snapshot",
            "request_id": request_id,
            "slot_id": slot_id,
            "declared_identity": {
                "version": declared_version,
                "commit": declared_commit,
            },
            "official_source_match": False,
            "source_inventory": copied,
            "prepared_at": utc_now(),
        }
        atomic_write_json(candidate_root / CANDIDATE_NAME, candidate)
        _fsync_directory(candidate_root)
    except Exception:
        _remove_candidate(candidate_root, layout["staging"])
        raise
    return {
        "status": "staged",
        "slot_id": slot_id,
        "candidate_path": str(candidate_root),
        "source_path": str(candidate_source),
        "inventory": copied,
    }


def validate_compose_services(value: Any) -> list[str]:
    if (
        type(value) is not list
        or not 1 <= len(value) <= 64
        or any(type(item) is not str or not SERVICE_RE.fullmatch(item) for item in value)
        or value != sorted(set(value))
        or not REQUIRED_ADOPTED_COMPOSE_SERVICES.issubset(value)
        or not set(value).issubset(KNOWN_COMPOSE_SERVICES)
    ):
        raise SlotError(
            "compose_services_invalid",
            "Compose service evidence is incomplete or contains an unknown service.",
        )
    return list(value)


def _yaml_volume(source: Path, target: str, *, read_only: bool = False) -> str:
    source_text = str(source)
    if (
        not source.is_absolute()
        or any(char in source_text for char in ("\x00", "\r", "\n", '"'))
        or not target.startswith("/")
        or any(char in target for char in ("\x00", "\r", "\n", '"', ":"))
    ):
        raise SlotError(
            "runtime_override_path_invalid",
            "Adopted slot runtime override contains an unsafe path.",
        )
    suffix = ":ro" if read_only else ""
    return json.dumps(f"{source_text}:{target}{suffix}", ensure_ascii=True)


def _render_adopted_runtime_override(
    *,
    app_dir: Path,
    final_source: Path,
    candidate_source: Path,
    services: list[str],
) -> bytes:
    stable_data = app_dir / "data"
    volumes: dict[str, list[tuple[Path, str, bool]]] = {
        "postgres": [
            (stable_data / "postgres", "/var/lib/postgresql/data", False),
        ],
        "redis": [
            (stable_data / "redis", "/data", False),
        ],
        "update-helper-bootstrap": [
            (app_dir, str(app_dir), False),
        ],
        "update-status-reader": [
            (stable_data / "update-public", "/update-public", True),
        ],
        "update-retry-admission": [
            (stable_data / "update-control", "/update-control", False),
            (stable_data / "update-public", "/update-public", False),
        ],
        "schema-update": [
            (stable_data / "update-control", "/update-control", False),
            (final_source / "release", "/app/release", True),
            (final_source / ".km-vms-release.json", "/app/.km-vms-release.json", True),
            (final_source / ".km-vms-source.json", "/app/.km-vms-source.json", True),
        ],
        "api": [
            (stable_data / "install-control", "/install-control", False),
            (stable_data / "update-control", "/update-control", False),
            (stable_data / "previews", "/storage/previews", False),
            (stable_data / "exports", "/storage/exports", False),
            (final_source / "release", "/app/release", True),
            (final_source / ".km-vms-release.json", "/app/.km-vms-release.json", True),
            (final_source / ".km-vms-source.json", "/app/.km-vms-source.json", True),
        ],
        "setup-helper": [
            (app_dir, str(app_dir), False),
        ],
        "update-helper": [
            (app_dir, "/host-app", False),
            (app_dir, str(app_dir), False),
        ],
        "nginx": [
            (
                final_source / "deploy/nginx/default.conf",
                "/etc/nginx/conf.d/default.conf",
                True,
            ),
            (stable_data / "exports", "/var/www/exports", True),
        ],
    }
    if (candidate_source / "deploy/nginx/update-recovery.html").is_file():
        volumes["nginx"].append(
            (
                final_source / "deploy/nginx/update-recovery.html",
                "/etc/nginx/update-recovery.html",
                True,
            )
        )

    lines = [
        "# Generated by KM VMS release-slot engine. Do not edit.",
        "services:",
    ]
    for service in services:
        service_volumes = volumes.get(service)
        if not service_volumes:
            continue
        lines.extend((f"  {service}:", "    volumes:"))
        for source, target, read_only in service_volumes:
            lines.append(
                "      - "
                + _yaml_volume(source, target, read_only=read_only)
            )
    rendered = ("\n".join(lines) + "\n").encode("utf-8")
    if len(rendered) > MAX_JSON_BYTES:
        raise SlotError(
            "runtime_override_too_large",
            "Adopted slot runtime override exceeds its size bound.",
        )
    return rendered


def _atomic_write_bytes(path: Path, rendered: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except SlotError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SlotError(
            "runtime_override_write_failed",
            "Adopted slot runtime override could not be persisted.",
        ) from exc


def prepare_adopted_runtime_override(
    app_dir: Path,
    *,
    request_id: str,
    services: list[str],
) -> dict[str, Any]:
    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    request_id = require_request_id(request_id)
    services = validate_compose_services(services)
    candidate_root = _candidate_root(layout, request_id)
    candidate = read_json(candidate_root / CANDIDATE_NAME)
    if candidate is None or candidate.get("kind") != "adopted_pre_update_snapshot":
        raise SlotError(
            "candidate_invalid",
            "Only an adopted pre-update candidate can receive a runtime override.",
        )
    slot_id = require_slot_id(str(candidate.get("slot_id") or ""))
    final_source = layout["slots"] / slot_id / SOURCE_DIR_NAME
    rendered = _render_adopted_runtime_override(
        app_dir=app_dir,
        final_source=final_source,
        candidate_source=candidate_root / SOURCE_DIR_NAME,
        services=services,
    )
    override_path = candidate_root / RUNTIME_OVERRIDE_NAME
    _atomic_write_bytes(override_path, rendered)
    return {
        "status": "prepared",
        "slot_id": slot_id,
        "override_path": str(override_path),
        "sha256": hashlib.sha256(rendered).hexdigest(),
    }


def validate_compose_evidence(value: Any) -> dict[str, Any]:
    expected = {
        "schema_version",
        "project_name",
        "project_directory",
        "captured_plan_sha256",
        "slot_plan_sha256",
        "archive_override_attached",
        "archive_override_sha256",
        "runtime_override_sha256",
        "shared_root_contract",
        "services",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or type(value.get("project_name")) is not str
        or not PROJECT_RE.fullmatch(value["project_name"])
        or value.get("project_directory") != SOURCE_DIR_NAME
        or type(value.get("captured_plan_sha256")) is not str
        or not DIGEST_RE.fullmatch(value["captured_plan_sha256"])
        or type(value.get("slot_plan_sha256")) is not str
        or not DIGEST_RE.fullmatch(value["slot_plan_sha256"])
        or type(value.get("archive_override_attached")) is not bool
        or value.get("shared_root_contract") != "stable_app_dir_v1"
    ):
        raise SlotError("compose_evidence_invalid", "Compose evidence is invalid.")
    validate_compose_services(value.get("services"))
    archive_digest = value.get("archive_override_sha256")
    if value["archive_override_attached"]:
        if type(archive_digest) is not str or not DIGEST_RE.fullmatch(archive_digest):
            raise SlotError("compose_evidence_invalid", "Archive-root Compose evidence is invalid.")
    elif archive_digest is not None:
        raise SlotError("compose_evidence_invalid", "Absent archive override has contradictory evidence.")
    runtime_digest = value.get("runtime_override_sha256")
    if runtime_digest is not None and (
        type(runtime_digest) is not str or not DIGEST_RE.fullmatch(runtime_digest)
    ):
        raise SlotError(
            "compose_evidence_invalid",
            "Slot runtime override evidence is invalid.",
        )
    return json.loads(json.dumps(value))


def validate_image_evidence(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "services"}
        or value.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or type(value.get("services")) is not dict
        or len(value["services"]) > 32
    ):
        raise SlotError("image_evidence_invalid", "Immutable image evidence is incomplete.")
    normalized: dict[str, Any] = {"schema_version": 1, "services": {}}
    for service, item in sorted(value["services"].items()):
        if (
            type(service) is not str
            or not SERVICE_RE.fullmatch(service)
            or type(item) is not dict
            or set(item)
            != {"image_id", "source_image_ref", "immutable_image_ref"}
            or type(item.get("image_id")) is not str
            or not IMAGE_ID_RE.fullmatch(item["image_id"])
            or any(
                type(item.get(key)) is not str
                or not 1 <= len(item[key]) <= 240
                or any(char in item[key] for char in "\r\n\x00")
                for key in ("source_image_ref", "immutable_image_ref")
            )
        ):
            raise SlotError("image_evidence_invalid", "Immutable image evidence is invalid.")
        normalized["services"][service] = dict(item)
    return normalized


def validate_health_evidence(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema_version",
            "status",
            "api_visible_identity_sha256",
            "core_services",
        }
        or value.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or value.get("status") != "healthy"
        or type(value.get("api_visible_identity_sha256")) is not str
        or not DIGEST_RE.fullmatch(value["api_visible_identity_sha256"])
        or type(value.get("core_services")) is not list
        or sorted(value["core_services"]) != ["api", "nginx", "recorder", "web"]
    ):
        raise SlotError("health_evidence_invalid", "Pre-update core health evidence is invalid.")
    return json.loads(json.dumps(value))


def validate_manifest(value: Any, *, expected_slot_id: str | None = None) -> dict[str, Any]:
    expected = {
        "schema_version",
        "kind",
        "slot_id",
        "declared_identity",
        "official_source_match",
        "source_inventory",
        "compose_evidence",
        "image_evidence",
        "pre_update_health",
        "prepared_at",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or value.get("kind")
        not in {"trusted_release", "adopted_pre_update_snapshot"}
        or type(value.get("slot_id")) is not str
        or not SLOT_RE.fullmatch(value["slot_id"])
        or (expected_slot_id is not None and value["slot_id"] != expected_slot_id)
        or type(value.get("declared_identity")) is not dict
        or set(value["declared_identity"]) != {"version", "commit"}
        or type(value["declared_identity"].get("version")) is not str
        or not VERSION_RE.fullmatch(value["declared_identity"]["version"])
        or type(value["declared_identity"].get("commit")) is not str
        or not COMMIT_RE.fullmatch(value["declared_identity"]["commit"])
        or type(value.get("official_source_match")) is not bool
        or type(value.get("source_inventory")) is not dict
        or set(value["source_inventory"])
        != {"schema_version", "sha256", "entry_count", "file_count", "total_bytes"}
        or value["source_inventory"].get("schema_version") != 1
        or type(value["source_inventory"].get("sha256")) is not str
        or not DIGEST_RE.fullmatch(value["source_inventory"]["sha256"])
        or any(
            type(value["source_inventory"].get(key)) is not int
            or value["source_inventory"][key] < 0
            for key in ("entry_count", "file_count", "total_bytes")
        )
        or type(value.get("prepared_at")) is not str
        or not value["prepared_at"]
    ):
        raise SlotError("slot_manifest_invalid", "Immutable slot manifest is invalid.")
    if value["kind"] == "trusted_release":
        if (
            not TRUSTED_SLOT_RE.fullmatch(value["slot_id"])
            or value["slot_id"].removeprefix("release-")
            != value["declared_identity"]["commit"]
            or value["official_source_match"] is not True
            or value.get("pre_update_health") is not None
        ):
            raise SlotError("slot_manifest_invalid", "Trusted release manifest is contradictory.")
        required_images = TARGET_REQUIRED_IMAGE_SERVICES
    else:
        if (
            not ADOPTED_SLOT_RE.fullmatch(value["slot_id"])
            or value["slot_id"].removeprefix("adopted-")
            != value["source_inventory"]["sha256"]
            or value["official_source_match"] is not False
        ):
            raise SlotError("slot_manifest_invalid", "Adopted snapshot manifest is contradictory.")
        validate_health_evidence(value.get("pre_update_health"))
        required_images = ADOPTED_REQUIRED_IMAGE_SERVICES
    compose = validate_compose_evidence(value.get("compose_evidence"))
    if value["kind"] == "trusted_release":
        if compose["runtime_override_sha256"] is not None:
            raise SlotError(
                "slot_manifest_invalid",
                "Trusted target slot has an unexpected legacy runtime override.",
            )
    elif compose["runtime_override_sha256"] is None:
        raise SlotError(
            "slot_manifest_invalid",
            "Adopted snapshot lacks its stable-root runtime override evidence.",
        )
    images = validate_image_evidence(value.get("image_evidence"))
    if not required_images.issubset(images["services"]):
        raise SlotError(
            "slot_manifest_invalid",
            "Release slot lacks required immutable image evidence.",
        )
    for service, item in images["services"].items():
        if not item["immutable_image_ref"].endswith(f":{value['slot_id']}"):
            raise SlotError(
                "slot_manifest_invalid",
                "Release slot image evidence is not namespaced by its immutable slot ID.",
            )
        if (
            value["kind"] == "trusted_release"
            and service in TARGET_BUILT_IMAGE_SERVICES
            and not item["source_image_ref"].endswith(f":{value['slot_id']}")
        ):
            raise SlotError(
                "slot_manifest_invalid",
                "Target build evidence used a mutable image tag.",
            )
    forbidden_roles = {"status", "current", "previous", "candidate", "active"}
    if forbidden_roles & set(value):
        raise SlotError("slot_manifest_role_forbidden", "Slot manifest contains a mutable role.")
    return json.loads(json.dumps(value))


def _freeze_slot_tree(slot_root: Path) -> None:
    paths: list[Path] = []
    for current, dirnames, filenames in os.walk(
        slot_root,
        topdown=False,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in filenames:
            paths.append(current_path / name)
        for name in dirnames:
            paths.append(current_path / name)
    paths.append(slot_root)
    try:
        for path in paths:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SlotError(
                    "slot_path_invalid",
                    "Immutable release slot contains a symlink.",
                )
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                os.chmod(path, (mode & ~0o222) | 0o500)
            elif stat.S_ISREG(info.st_mode):
                os.chmod(path, mode & ~0o222)
            else:
                raise SlotError(
                    "slot_path_invalid",
                    "Immutable release slot contains a special filesystem object.",
                )
    except SlotError:
        raise
    except OSError as exc:
        raise SlotError(
            "slot_freeze_failed",
            "Release slot could not be made read-only.",
        ) from exc


def _validate_slot_read_only(slot_root: Path) -> None:
    for current, dirnames, filenames in os.walk(
        slot_root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in [*dirnames, *filenames]:
            path = current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o222:
                raise SlotError(
                    "slot_mutable",
                    "Published release slot is not immutable.",
                )
    if stat.S_IMODE(slot_root.lstat().st_mode) & 0o222:
        raise SlotError("slot_mutable", "Published release slot is not immutable.")


def _make_tree_owner_writable(slot_root: Path) -> None:
    try:
        for current, dirnames, filenames in os.walk(
            slot_root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            os.chmod(current_path, stat.S_IMODE(current_path.lstat().st_mode) | 0o700)
            for name in filenames:
                path = current_path / name
                if not path.is_symlink():
                    os.chmod(path, stat.S_IMODE(path.lstat().st_mode) | 0o600)
            for name in dirnames:
                path = current_path / name
                if not path.is_symlink():
                    os.chmod(path, stat.S_IMODE(path.lstat().st_mode) | 0o700)
    except OSError:
        pass


def _restore_candidate_state(candidate_root: Path, candidate: dict[str, Any]) -> None:
    if not candidate_root.is_dir() or candidate_root.is_symlink():
        return
    _make_tree_owner_writable(candidate_root)
    try:
        (candidate_root / MANIFEST_NAME).unlink(missing_ok=True)
        atomic_write_json(candidate_root / CANDIDATE_NAME, candidate)
    except (OSError, SlotError):
        pass


def validate_slot(slot_root: Path, *, expected_slot_id: str | None = None) -> dict[str, Any]:
    if slot_root.is_symlink() or not slot_root.is_dir():
        raise SlotError("slot_path_invalid", "Release slot path is unsafe.")
    slot_id = require_slot_id(expected_slot_id or slot_root.name)
    if slot_root.name != slot_id:
        raise SlotError("slot_path_invalid", "Release slot directory and manifest identity differ.")
    manifest = read_json(slot_root / MANIFEST_NAME)
    assert manifest is not None
    manifest = validate_manifest(manifest, expected_slot_id=slot_id)
    required_paths = (
        TRUSTED_REQUIRED_SOURCE_PATHS
        if manifest["kind"] == "trusted_release"
        else LEGACY_REQUIRED_SOURCE_PATHS
    )
    inventory = product_inventory(
        slot_root / SOURCE_DIR_NAME,
        required_paths=required_paths,
    )
    if inventory != manifest["source_inventory"]:
        raise SlotError("slot_inventory_mismatch", "Release slot source no longer matches its manifest.")
    runtime_override = slot_root / RUNTIME_OVERRIDE_NAME
    runtime_digest = manifest["compose_evidence"]["runtime_override_sha256"]
    if manifest["kind"] == "adopted_pre_update_snapshot":
        try:
            override_info = runtime_override.lstat()
            override_bytes = runtime_override.read_bytes()
        except OSError as exc:
            raise SlotError(
                "runtime_override_invalid",
                "Adopted slot runtime override is unavailable.",
            ) from exc
        if (
            stat.S_ISLNK(override_info.st_mode)
            or not stat.S_ISREG(override_info.st_mode)
            or not override_bytes
            or len(override_bytes) > MAX_JSON_BYTES
            or hashlib.sha256(override_bytes).hexdigest() != runtime_digest
        ):
            raise SlotError(
                "runtime_override_invalid",
                "Adopted slot runtime override does not match immutable evidence.",
            )
    elif runtime_override.exists() or runtime_override.is_symlink():
        raise SlotError(
            "runtime_override_invalid",
            "Trusted release slot contains an unexpected runtime override.",
        )
    _validate_slot_read_only(slot_root)
    return manifest


def finalize_candidate(
    app_dir: Path,
    *,
    request_id: str,
    compose_evidence: dict[str, Any],
    image_evidence: dict[str, Any],
    health_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    request_id = require_request_id(request_id)
    candidate_root = _candidate_root(layout, request_id)
    if candidate_root.is_symlink() or not candidate_root.is_dir():
        raise SlotError("candidate_missing", "Prepared release candidate is unavailable.")
    candidate = read_json(candidate_root / CANDIDATE_NAME)
    assert candidate is not None
    expected_candidate_fields = {
        "schema_version",
        "kind",
        "request_id",
        "slot_id",
        "declared_identity",
        "official_source_match",
        "source_inventory",
        "prepared_at",
    }
    if (
        set(candidate) != expected_candidate_fields
        or candidate.get("schema_version") != CANDIDATE_SCHEMA_VERSION
        or candidate.get("request_id") != request_id
        or type(candidate.get("slot_id")) is not str
        or not SLOT_RE.fullmatch(candidate["slot_id"])
        or candidate.get("kind")
        not in {"trusted_release", "adopted_pre_update_snapshot"}
    ):
        raise SlotError("candidate_invalid", "Prepared release candidate is invalid.")
    required_paths = (
        TRUSTED_REQUIRED_SOURCE_PATHS
        if candidate.get("kind") == "trusted_release"
        else LEGACY_REQUIRED_SOURCE_PATHS
    )
    observed_inventory = product_inventory(
        candidate_root / SOURCE_DIR_NAME,
        required_paths=required_paths,
    )
    if observed_inventory != candidate.get("source_inventory"):
        raise SlotError("candidate_inventory_mismatch", "Prepared candidate source changed before finalization.")
    compose = validate_compose_evidence(compose_evidence)
    images = validate_image_evidence(image_evidence)
    if candidate["kind"] == "adopted_pre_update_snapshot":
        health = validate_health_evidence(health_evidence)
        override_path = candidate_root / RUNTIME_OVERRIDE_NAME
        try:
            override_info = override_path.lstat()
            override_bytes = override_path.read_bytes()
        except OSError as exc:
            raise SlotError(
                "runtime_override_invalid",
                "Adopted slot runtime override is unavailable.",
            ) from exc
        if (
            stat.S_ISLNK(override_info.st_mode)
            or not stat.S_ISREG(override_info.st_mode)
            or not override_bytes
            or len(override_bytes) > MAX_JSON_BYTES
            or hashlib.sha256(override_bytes).hexdigest()
            != compose["runtime_override_sha256"]
        ):
            raise SlotError(
                "runtime_override_invalid",
                "Adopted slot runtime override does not match Compose evidence.",
            )
    else:
        if health_evidence is not None:
            raise SlotError("health_evidence_invalid", "Target slot cannot claim pre-update health.")
        if (
            compose["runtime_override_sha256"] is not None
            or (candidate_root / RUNTIME_OVERRIDE_NAME).exists()
            or (candidate_root / RUNTIME_OVERRIDE_NAME).is_symlink()
        ):
            raise SlotError(
                "runtime_override_invalid",
                "Trusted target cannot contain a legacy runtime override.",
            )
        health = None
    manifest = validate_manifest(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "kind": candidate["kind"],
            "slot_id": candidate["slot_id"],
            "declared_identity": candidate["declared_identity"],
            "official_source_match": candidate["official_source_match"],
            "source_inventory": candidate["source_inventory"],
            "compose_evidence": compose,
            "image_evidence": images,
            "pre_update_health": health,
            "prepared_at": candidate["prepared_at"],
        },
        expected_slot_id=candidate["slot_id"],
    )
    final_path = layout["slots"] / candidate["slot_id"]
    if final_path.exists():
        existing = validate_slot(final_path, expected_slot_id=candidate["slot_id"])
        if existing != manifest:
            raise SlotError("slot_conflict", "Existing immutable slot contradicts the prepared candidate.")
        _remove_candidate(candidate_root, layout["staging"])
        return {
            "status": "reused",
            "slot_id": candidate["slot_id"],
            "slot_path": str(final_path),
            "manifest": existing,
        }
    atomic_write_json(candidate_root / MANIFEST_NAME, manifest)
    (candidate_root / CANDIDATE_NAME).unlink()
    _fsync_directory(candidate_root)
    try:
        _freeze_slot_tree(candidate_root)
    except SlotError:
        _restore_candidate_state(candidate_root, candidate)
        raise
    try:
        os.replace(candidate_root, final_path)
        _fsync_directory(layout["slots"])
    except OSError as exc:
        _restore_candidate_state(candidate_root, candidate)
        raise SlotError("slot_publish_failed", "Immutable release slot could not be published atomically.") from exc
    try:
        candidate_root.parent.rmdir()
    except OSError:
        pass
    verified = validate_slot(final_path, expected_slot_id=candidate["slot_id"])
    return {
        "status": "published",
        "slot_id": candidate["slot_id"],
        "slot_path": str(final_path),
        "manifest": verified,
    }


def _payload_digest(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def build_activation_slot_binding(
    app_dir: Path,
    slot_id: str,
    *,
    compose_plan_sha256: str | None = None,
    archive_override_sha256: str | None = None,
) -> dict[str, Any]:
    app_dir = resolve_app_dir(app_dir)
    slot_id = require_slot_id(slot_id)
    slot_root = ensure_layout(app_dir)["slots"] / slot_id
    manifest = validate_slot(slot_root, expected_slot_id=slot_id)
    identity_path = slot_root / SOURCE_DIR_NAME / ".km-vms-release.json"
    try:
        identity_sha256, _identity_size = _hash_file(identity_path)
    except SlotError as exc:
        raise SlotError(
            "activation_identity_invalid",
            "Release-slot identity evidence is unavailable.",
        ) from exc
    compose = manifest["compose_evidence"]
    selected_plan = (
        compose_plan_sha256
        if compose_plan_sha256 is not None
        else compose["slot_plan_sha256"]
    )
    if not DIGEST_RE.fullmatch(str(selected_plan or "")):
        raise SlotError(
            "activation_compose_evidence_invalid",
            "Activation Compose evidence is invalid.",
        )
    selected_archive = (
        archive_override_sha256
        if archive_override_sha256 is not None
        else compose["archive_override_sha256"]
    )
    if selected_archive is not None and not DIGEST_RE.fullmatch(
        str(selected_archive)
    ):
        raise SlotError(
            "activation_compose_evidence_invalid",
            "Activation archive-root evidence is invalid.",
        )
    return {
        "slot_id": slot_id,
        "kind": manifest["kind"],
        "official_source_match": manifest["official_source_match"],
        "version": manifest["declared_identity"]["version"],
        "commit": manifest["declared_identity"]["commit"],
        "manifest_sha256": _payload_digest(manifest),
        "inventory_sha256": manifest["source_inventory"]["sha256"],
        "compose_plan_sha256": selected_plan,
        "archive_override_sha256": selected_archive,
        "image_evidence_sha256": _payload_digest(
            manifest["image_evidence"]
        ),
        "api_identity_sha256": identity_sha256,
    }


def validate_activation_slot_binding(value: Any) -> dict[str, Any]:
    expected = {
        "slot_id",
        "kind",
        "official_source_match",
        "version",
        "commit",
        "manifest_sha256",
        "inventory_sha256",
        "compose_plan_sha256",
        "archive_override_sha256",
        "image_evidence_sha256",
        "api_identity_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or type(value.get("slot_id")) is not str
        or not SLOT_RE.fullmatch(value["slot_id"])
        or value.get("kind")
        not in {"trusted_release", "adopted_pre_update_snapshot"}
        or type(value.get("official_source_match")) is not bool
        or type(value.get("version")) is not str
        or not VERSION_RE.fullmatch(value["version"])
        or type(value.get("commit")) is not str
        or not COMMIT_RE.fullmatch(value["commit"])
        or any(
            type(value.get(key)) is not str
            or not DIGEST_RE.fullmatch(value[key])
            for key in (
                "manifest_sha256",
                "inventory_sha256",
                "compose_plan_sha256",
                "image_evidence_sha256",
                "api_identity_sha256",
            )
        )
        or (
            value.get("archive_override_sha256") is not None
            and (
                type(value["archive_override_sha256"]) is not str
                or not DIGEST_RE.fullmatch(
                    value["archive_override_sha256"]
                )
            )
        )
    ):
        raise SlotError(
            "activation_slot_binding_invalid",
            "Activation slot binding is invalid.",
        )
    if (
        value["kind"] == "trusted_release"
        and (
            not TRUSTED_SLOT_RE.fullmatch(value["slot_id"])
            or value["official_source_match"] is not True
        )
    ) or (
        value["kind"] == "adopted_pre_update_snapshot"
        and (
            not ADOPTED_SLOT_RE.fullmatch(value["slot_id"])
            or value["official_source_match"] is not False
        )
    ):
        raise SlotError(
            "activation_slot_binding_invalid",
            "Activation slot binding contradicts slot identity.",
        )
    return json.loads(json.dumps(value))


def validate_activation_journal(value: Any) -> dict[str, Any]:
    expected = {
        "schema_version",
        "document_type",
        "request_id",
        "generation",
        "phase",
        "previous",
        "target",
        "schema",
        "pointer_slot_id",
        "target_verified",
        "previous_verified",
        "rollback_trigger",
        "failure_category",
        "created_at",
        "updated_at",
        "terminal_at",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != JOURNAL_SCHEMA_VERSION
        or value.get("document_type") != "release_slot_activation"
        or type(value.get("request_id")) is not str
        or not REQUEST_RE.fullmatch(value["request_id"])
        or type(value.get("generation")) is not int
        or isinstance(value["generation"], bool)
        or value["generation"] < 1
        or value.get("phase") not in ACTIVATION_PHASES
        or type(value.get("schema")) is not dict
        or set(value["schema"])
        != {
            "compatibility",
            "compatibility_sha256",
            "source_version",
            "target_version",
            "migration_required",
            "migration_invoked",
            "migration_completed",
            "migration_attempt_id",
        }
        or value["schema"].get("compatibility") != "compatible"
        or type(value["schema"].get("compatibility_sha256")) is not str
        or not DIGEST_RE.fullmatch(
            value["schema"]["compatibility_sha256"]
        )
        or type(value["schema"].get("source_version")) is not int
        or isinstance(value["schema"]["source_version"], bool)
        or value["schema"]["source_version"] < 0
        or type(value["schema"].get("target_version")) is not int
        or isinstance(value["schema"]["target_version"], bool)
        or value["schema"]["target_version"] < 0
        or value["schema"]["source_version"]
        > value["schema"]["target_version"]
        or type(value["schema"].get("migration_required")) is not bool
        or type(value["schema"].get("migration_invoked")) is not bool
        or type(value["schema"].get("migration_completed")) is not bool
        or (
            value["schema"].get("migration_attempt_id") is not None
            and (
                type(value["schema"]["migration_attempt_id"]) is not str
                or not re.fullmatch(
                    r"^migration-attempt-[0-9a-f]{32}$",
                    value["schema"]["migration_attempt_id"],
                )
            )
        )
        or type(value.get("target_verified")) is not bool
        or type(value.get("previous_verified")) is not bool
        or type(value.get("created_at")) is not str
        or not value["created_at"]
        or type(value.get("updated_at")) is not str
        or not value["updated_at"]
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal is invalid.",
        )
    previous = validate_activation_slot_binding(value.get("previous"))
    target = validate_activation_slot_binding(value.get("target"))
    if (
        previous["slot_id"] == target["slot_id"]
        or target["kind"] != "trusted_release"
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal slot roles are contradictory.",
        )
    pointer_slot_id = value.get("pointer_slot_id")
    if pointer_slot_id is not None and pointer_slot_id not in {
        previous["slot_id"],
        target["slot_id"],
    }:
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal pointer evidence is contradictory.",
        )
    failure_category = value.get("failure_category")
    if failure_category is not None and (
        type(failure_category) is not str
        or not MACHINE_CODE_RE.fullmatch(failure_category)
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal failure category is invalid.",
        )
    rollback_trigger = value.get("rollback_trigger")
    if rollback_trigger is not None and (
        type(rollback_trigger) is not str
        or not MACHINE_CODE_RE.fullmatch(rollback_trigger)
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal rollback trigger is invalid.",
        )
    if rollback_trigger is not None and value["phase"] not in {
        "rolling_back",
        "failed_rolled_back",
        "blocked",
    }:
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal rollback trigger is contradictory.",
        )
    terminal_at = value.get("terminal_at")
    terminal = value["phase"] in TERMINAL_ACTIVATION_PHASES
    if terminal != (type(terminal_at) is str and bool(terminal_at)):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal terminal evidence is contradictory.",
        )
    schema = value["schema"]
    if (
        schema["migration_completed"]
        and (
            not schema["migration_required"]
            or not schema["migration_invoked"]
        )
    ) or (
        not schema["migration_required"]
        and (
            schema["migration_invoked"]
            or schema["migration_completed"]
        )
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Activation journal schema evidence is contradictory.",
        )
    if value["target_verified"] and value["phase"] in {
        "target_prepared",
        "quiescing",
        "schema_preparing",
        "activating",
    }:
        raise SlotError(
            "activation_journal_invalid",
            "Target verification evidence is contradictory.",
        )
    if value["previous_verified"] != (
        value["phase"] == "failed_rolled_back"
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Previous verification evidence is contradictory.",
        )
    if value["phase"] == "completed" and (
        not value["target_verified"]
        or pointer_slot_id != target["slot_id"]
        or failure_category is not None
        or rollback_trigger is not None
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Completed activation evidence is contradictory.",
        )
    if value["phase"] == "failed_rolled_back" and (
        pointer_slot_id != previous["slot_id"]
        or failure_category is None
        or rollback_trigger is None
    ):
        raise SlotError(
            "activation_journal_invalid",
            "Rollback terminal evidence is contradictory.",
        )
    if value["phase"] == "blocked" and failure_category is None:
        raise SlotError(
            "activation_journal_invalid",
            "Blocked activation lacks a bounded failure category.",
        )
    normalized = json.loads(json.dumps(value))
    normalized["previous"] = previous
    normalized["target"] = target
    return normalized


def read_active_slot(app_dir: Path) -> tuple[str, Path] | None:
    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    pointer = layout["active"]
    try:
        info = pointer.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        raise SlotError("active_pointer_invalid", "Active release pointer is not a symlink.")
    target = os.readlink(pointer)
    parts = Path(target).parts
    if len(parts) != 3 or parts[0] != "slots" or parts[2] != SOURCE_DIR_NAME:
        raise SlotError("active_pointer_invalid", "Active release pointer target is invalid.")
    slot_id = require_slot_id(parts[1])
    source = layout["slots"] / slot_id / SOURCE_DIR_NAME
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise SlotError("active_pointer_invalid", "Active release pointer target is unavailable.") from exc
    expected = (layout["slots"] / slot_id / SOURCE_DIR_NAME).resolve(strict=True)
    if resolved != expected:
        raise SlotError("active_pointer_invalid", "Active release pointer escaped its slot.")
    validate_slot(layout["slots"] / slot_id, expected_slot_id=slot_id)
    return slot_id, resolved


def atomic_switch_pointer(app_dir: Path, slot_id: str) -> Path:
    """Atomically select one already-validated immutable release slot."""

    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    slot_id = require_slot_id(slot_id)
    validate_slot(layout["slots"] / slot_id, expected_slot_id=slot_id)
    replacement = layout["runtime"] / f".active.{uuid.uuid4().hex}.next"
    relative_target = f"slots/{slot_id}/{SOURCE_DIR_NAME}"
    try:
        os.symlink(relative_target, replacement)
        if os.readlink(replacement) != relative_target:
            raise SlotError("active_pointer_prepare_failed", "Active pointer replacement verification failed.")
        _fsync_directory(layout["runtime"])
        os.replace(replacement, layout["active"])
        _fsync_directory(layout["runtime"])
    except SlotError:
        replacement.unlink(missing_ok=True)
        raise
    except OSError as exc:
        replacement.unlink(missing_ok=True)
        raise SlotError("active_pointer_switch_failed", "Active release pointer could not be switched atomically.") from exc
    active = read_active_slot(app_dir)
    if active is None or active[0] != slot_id:
        raise SlotError("active_pointer_switch_failed", "Active release pointer verification failed.")
    return active[1]


def read_activation_journal(
    app_dir: Path,
    *,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    app_dir = resolve_app_dir(app_dir)
    payload = read_json(
        ensure_layout(app_dir)["journal"],
        missing_ok=missing_ok,
    )
    if payload is None:
        return None
    return validate_activation_journal(payload)


def initialize_activation_journal(
    app_dir: Path,
    *,
    request_id: str,
    previous: dict[str, Any],
    target: dict[str, Any],
    compatibility_sha256: str,
    source_schema_version: int,
    target_schema_version: int,
    migration_required: bool,
    migration_attempt_id: str | None = None,
) -> dict[str, Any]:
    app_dir = resolve_app_dir(app_dir)
    request_id = require_request_id(request_id)
    previous = validate_activation_slot_binding(previous)
    target = validate_activation_slot_binding(target)
    layout = ensure_layout(app_dir)
    existing = read_activation_journal(app_dir, missing_ok=True)
    if existing is not None:
        if existing["request_id"] == request_id:
            if (
                existing["previous"] != previous
                or existing["target"] != target
                or existing["schema"]["compatibility_sha256"]
                != compatibility_sha256
                or existing["schema"]["source_version"]
                != source_schema_version
                or existing["schema"]["target_version"]
                != target_schema_version
                or existing["schema"]["migration_required"]
                is not migration_required
                or existing["schema"]["migration_attempt_id"]
                != migration_attempt_id
            ):
                raise SlotError(
                    "activation_journal_conflict",
                    "Existing activation journal contradicts this request.",
                )
            return existing
        if existing["phase"] not in TERMINAL_ACTIVATION_PHASES:
            raise SlotError(
                "activation_in_progress",
                "Another release activation is still active.",
            )
        generation = existing["generation"] + 1
    else:
        generation = 1
    if (
        type(compatibility_sha256) is not str
        or not DIGEST_RE.fullmatch(compatibility_sha256)
        or type(source_schema_version) is not int
        or isinstance(source_schema_version, bool)
        or source_schema_version < 0
        or type(target_schema_version) is not int
        or isinstance(target_schema_version, bool)
        or target_schema_version < source_schema_version
    ):
        raise SlotError(
            "activation_schema_compatibility_invalid",
            "Schema compatibility evidence is invalid.",
        )
    active = read_active_slot(app_dir)
    if active is not None and active[0] != previous["slot_id"]:
        raise SlotError(
            "activation_pointer_conflict",
            "Active pointer does not match the captured previous slot.",
        )
    if active is None and previous["kind"] != "adopted_pre_update_snapshot":
        raise SlotError(
            "activation_pointer_missing",
            "A trusted previous slot requires an active pointer.",
        )
    now = utc_now()
    journal = validate_activation_journal(
        {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "document_type": "release_slot_activation",
            "request_id": request_id,
            "generation": generation,
            "phase": "target_prepared",
            "previous": previous,
            "target": target,
            "schema": {
                "compatibility": "compatible",
                "compatibility_sha256": compatibility_sha256,
                "source_version": source_schema_version,
                "target_version": target_schema_version,
                "migration_required": migration_required,
                "migration_invoked": False,
                "migration_completed": False,
                "migration_attempt_id": migration_attempt_id,
            },
            "pointer_slot_id": active[0] if active is not None else None,
            "target_verified": False,
            "previous_verified": False,
            "rollback_trigger": None,
            "failure_category": None,
            "created_at": now,
            "updated_at": now,
            "terminal_at": None,
        }
    )
    atomic_write_json(layout["journal"], journal)
    return journal


def transition_activation_journal(
    app_dir: Path,
    *,
    request_id: str,
    phase: str,
    pointer_slot_id: str | None = None,
    record_pointer: bool = False,
    migration_invoked: bool | None = None,
    migration_completed: bool | None = None,
    target_verified: bool | None = None,
    previous_verified: bool | None = None,
    rollback_trigger: str | None = None,
    failure_category: str | None = None,
) -> dict[str, Any]:
    app_dir = resolve_app_dir(app_dir)
    request_id = require_request_id(request_id)
    current = read_activation_journal(app_dir)
    assert current is not None
    if current["request_id"] != request_id:
        raise SlotError(
            "activation_request_conflict",
            "Activation journal belongs to another request.",
        )
    if phase not in ACTIVATION_PHASES:
        raise SlotError(
            "activation_transition_invalid",
            "Activation phase is invalid.",
        )
    if (
        phase != current["phase"]
        and phase not in ACTIVATION_TRANSITIONS[current["phase"]]
    ):
        raise SlotError(
            "activation_transition_invalid",
            "Activation phase transition is not legal.",
        )
    updated = json.loads(json.dumps(current))
    updated["phase"] = phase
    if record_pointer:
        updated["pointer_slot_id"] = (
            require_slot_id(pointer_slot_id)
            if pointer_slot_id is not None
            else None
        )
    schema = updated["schema"]
    if migration_invoked is not None:
        if schema["migration_invoked"] and not migration_invoked:
            raise SlotError(
                "activation_transition_invalid",
                "Schema invocation evidence cannot move backwards.",
            )
        schema["migration_invoked"] = migration_invoked
    if migration_completed is not None:
        if schema["migration_completed"] and not migration_completed:
            raise SlotError(
                "activation_transition_invalid",
                "Schema completion evidence cannot move backwards.",
            )
        schema["migration_completed"] = migration_completed
    if target_verified is not None:
        if updated["target_verified"] and not target_verified:
            raise SlotError(
                "activation_transition_invalid",
                "Target verification evidence cannot move backwards.",
            )
        updated["target_verified"] = target_verified
    if previous_verified is not None:
        if updated["previous_verified"] and not previous_verified:
            raise SlotError(
                "activation_transition_invalid",
                "Previous verification evidence cannot move backwards.",
            )
        updated["previous_verified"] = previous_verified
    if failure_category is not None:
        if not MACHINE_CODE_RE.fullmatch(failure_category):
            raise SlotError(
                "activation_transition_invalid",
                "Activation failure category is invalid.",
            )
        if (
            updated["failure_category"] is not None
            and updated["failure_category"] != failure_category
        ):
            raise SlotError(
                "activation_transition_invalid",
                "Activation failure evidence cannot be replaced.",
            )
        updated["failure_category"] = failure_category
    if rollback_trigger is not None:
        if not MACHINE_CODE_RE.fullmatch(rollback_trigger):
            raise SlotError(
                "activation_transition_invalid",
                "Activation rollback trigger is invalid.",
            )
        if (
            updated["rollback_trigger"] is not None
            and updated["rollback_trigger"] != rollback_trigger
        ):
            raise SlotError(
                "activation_transition_invalid",
                "Activation rollback trigger cannot be replaced.",
            )
        updated["rollback_trigger"] = rollback_trigger
    if phase == "completed":
        updated["failure_category"] = None
        updated["rollback_trigger"] = None
    now = utc_now()
    updated["updated_at"] = now
    updated["terminal_at"] = (
        now if phase in TERMINAL_ACTIVATION_PHASES else None
    )
    validated = validate_activation_journal(updated)
    active = read_active_slot(app_dir)
    observed_pointer = active[0] if active is not None else None
    if validated["pointer_slot_id"] != observed_pointer:
        raise SlotError(
            "activation_pointer_conflict",
            "Activation journal pointer evidence does not match the active pointer.",
        )
    atomic_write_json(ensure_layout(app_dir)["journal"], validated)
    return validated


def protected_slot_ids(app_dir: Path) -> set[str]:
    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    protected: set[str] = set()
    active = read_active_slot(app_dir)
    if active:
        protected.add(active[0])
    journal = read_activation_journal(app_dir, missing_ok=True)
    if journal is not None:
        protected.add(journal["previous"]["slot_id"])
        protected.add(journal["target"]["slot_id"])
    return protected


def cleanup_unprotected_slots(
    app_dir: Path,
    *,
    retain_slot_ids: set[str],
    maximum_unprotected: int = 0,
    terminal_evidence: bool = False,
) -> list[str]:
    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    if maximum_unprotected < 0 or maximum_unprotected > 2:
        raise SlotError("cleanup_bound_invalid", "Slot cleanup retention bound is invalid.")
    if terminal_evidence is not True:
        raise SlotError(
            "cleanup_terminal_evidence_required",
            "Release-slot cleanup requires verified terminal operation evidence.",
        )
    protected = protected_slot_ids(app_dir) | {
        require_slot_id(slot_id) for slot_id in retain_slot_ids
    }
    candidates: list[tuple[float, str, Path]] = []
    for path in layout["slots"].iterdir():
        if path.is_symlink() or not path.is_dir() or not SLOT_RE.fullmatch(path.name):
            continue
        validate_slot(path, expected_slot_id=path.name)
        if path.name not in protected:
            candidates.append((path.stat().st_mtime, path.name, path))
    candidates.sort(reverse=True)
    removed: list[str] = []
    for _mtime, slot_id, path in candidates[maximum_unprotected:]:
        if path.parent.resolve() != layout["slots"].resolve():
            raise SlotError("cleanup_path_invalid", "Refusing unbounded slot cleanup.")
        _make_tree_owner_writable(path)
        shutil.rmtree(path)
        removed.append(slot_id)
    if removed:
        _fsync_directory(layout["slots"])
    return removed


def cleanup_request_staging(
    app_dir: Path,
    *,
    request_id: str,
    terminal_evidence: bool = False,
) -> bool:
    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    request_id = require_request_id(request_id)
    if terminal_evidence is not True:
        raise SlotError(
            "cleanup_terminal_evidence_required",
            "Request staging cleanup requires verified terminal operation evidence.",
        )
    request_root = layout["staging"] / request_id
    if request_root.parent.resolve() != layout["staging"].resolve():
        raise SlotError(
            "cleanup_path_invalid",
            "Refusing unbounded request staging cleanup.",
        )
    if not request_root.exists() and not request_root.is_symlink():
        return False
    if request_root.is_symlink() or not request_root.is_dir():
        raise SlotError(
            "staging_path_invalid",
            "Request staging path is unsafe.",
        )
    _make_tree_owner_writable(request_root)
    shutil.rmtree(request_root)
    _fsync_directory(layout["staging"])
    return True


def _load_evidence_file(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_absolute():
        raise SlotError("evidence_path_invalid", "Evidence file path must be absolute.")
    payload = read_json(path)
    assert payload is not None
    return payload


def _print_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KM VMS release-slot foundation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage_target_parser = subparsers.add_parser("stage-target")
    stage_target_parser.add_argument("--app-dir", required=True)
    stage_target_parser.add_argument("--source-dir", required=True)
    stage_target_parser.add_argument("--request-id", required=True)
    stage_target_parser.add_argument("--trusted-commit", required=True)
    stage_target_parser.add_argument("--declared-version", required=True)

    stage_adopted_parser = subparsers.add_parser("stage-adopted")
    stage_adopted_parser.add_argument("--app-dir", required=True)
    stage_adopted_parser.add_argument("--request-id", required=True)
    stage_adopted_parser.add_argument("--declared-version", required=True)
    stage_adopted_parser.add_argument("--declared-commit", required=True)

    runtime_parser = subparsers.add_parser("prepare-adopted-runtime")
    runtime_parser.add_argument("--app-dir", required=True)
    runtime_parser.add_argument("--request-id", required=True)
    runtime_parser.add_argument("--services-file", required=True)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--app-dir", required=True)
    finalize_parser.add_argument("--request-id", required=True)
    finalize_parser.add_argument("--compose-evidence-file", required=True)
    finalize_parser.add_argument("--image-evidence-file", required=True)
    finalize_parser.add_argument("--health-evidence-file")

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--app-dir", required=True)
    resolve_parser = subparsers.add_parser("resolve-active")
    resolve_parser.add_argument("--app-dir", required=True)
    resolve_path_parser = subparsers.add_parser("resolve-active-path")
    resolve_path_parser.add_argument("--app-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "stage-target":
            result = stage_target(
                Path(args.app_dir),
                Path(args.source_dir),
                request_id=args.request_id,
                trusted_commit=args.trusted_commit,
                declared_version=args.declared_version,
            )
        elif args.command == "stage-adopted":
            result = stage_adopted(
                Path(args.app_dir),
                request_id=args.request_id,
                declared_version=args.declared_version,
                declared_commit=args.declared_commit,
            )
        elif args.command == "prepare-adopted-runtime":
            service_payload = _load_evidence_file(args.services_file)
            if set(service_payload) != {"services"}:
                raise SlotError(
                    "compose_services_invalid",
                    "Compose service evidence has unexpected fields.",
                )
            result = prepare_adopted_runtime_override(
                Path(args.app_dir),
                request_id=args.request_id,
                services=service_payload["services"],
            )
        elif args.command == "finalize":
            result = finalize_candidate(
                Path(args.app_dir),
                request_id=args.request_id,
                compose_evidence=_load_evidence_file(args.compose_evidence_file),
                image_evidence=_load_evidence_file(args.image_evidence_file),
                health_evidence=(
                    _load_evidence_file(args.health_evidence_file)
                    if args.health_evidence_file
                    else None
                ),
            )
        elif args.command == "inspect":
            app_dir = resolve_app_dir(args.app_dir)
            layout = ensure_layout(app_dir)
            active = read_active_slot(app_dir)
            result = {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "layout": {
                    "runtime_relative": RUNTIME_RELATIVE.as_posix(),
                    "slots_relative": SLOTS_RELATIVE.as_posix(),
                    "staging_relative": STAGING_RELATIVE.as_posix(),
                    "active_relative": ACTIVE_RELATIVE.as_posix(),
                    "journal_relative": JOURNAL_RELATIVE.as_posix(),
                },
                "active_slot_id": active[0] if active else None,
                "slot_count": sum(
                    1
                    for path in layout["slots"].iterdir()
                    if path.is_dir() and not path.is_symlink() and SLOT_RE.fullmatch(path.name)
                ),
                "activation_cli_enabled": True,
            }
        elif args.command == "resolve-active":
            app_dir = resolve_app_dir(args.app_dir)
            active = read_active_slot(app_dir)
            if active is None:
                result = {
                    "schema_version": RUNTIME_SCHEMA_VERSION,
                    "mode": "legacy_root",
                    "slot_id": None,
                    "source_path": str(app_dir),
                }
            else:
                result = {
                    "schema_version": RUNTIME_SCHEMA_VERSION,
                    "mode": "release_slot",
                    "slot_id": active[0],
                    "source_path": str(active[1]),
                }
        elif args.command == "resolve-active-path":
            app_dir = resolve_app_dir(args.app_dir)
            active = read_active_slot(app_dir)
            print(str(active[1] if active else app_dir))
            return 0
        else:
            raise SlotError("command_invalid", "Unsupported release-slot command.")
    except SlotError as exc:
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
