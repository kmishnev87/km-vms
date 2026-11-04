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
import importlib.util
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


def _load_slot_components():
    directory = Path(__file__).resolve().parent / "km_vms_slot_core"
    name = "_kmvms_slots_" + hashlib.sha256(str(directory).encode()).hexdigest()[:24]
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("slot_components_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


_slot_components = _load_slot_components()
RUNTIME_SCHEMA_VERSION = _slot_components.RUNTIME_SCHEMA_VERSION
MANIFEST_SCHEMA_VERSION = _slot_components.MANIFEST_SCHEMA_VERSION
CANDIDATE_SCHEMA_VERSION = _slot_components.CANDIDATE_SCHEMA_VERSION
EVIDENCE_SCHEMA_VERSION = _slot_components.EVIDENCE_SCHEMA_VERSION
JOURNAL_SCHEMA_VERSION = _slot_components.JOURNAL_SCHEMA_VERSION
RUNTIME_RELATIVE = _slot_components.RUNTIME_RELATIVE
SLOTS_RELATIVE = _slot_components.SLOTS_RELATIVE
STAGING_RELATIVE = _slot_components.STAGING_RELATIVE
ACTIVE_RELATIVE = _slot_components.ACTIVE_RELATIVE
JOURNAL_RELATIVE = _slot_components.JOURNAL_RELATIVE
INITIAL_MATERIALIZATION_RELATIVE = _slot_components.INITIAL_MATERIALIZATION_RELATIVE
INSTALLED_PROJECTION_RELATIVE = _slot_components.INSTALLED_PROJECTION_RELATIVE
MANIFEST_NAME = _slot_components.MANIFEST_NAME
CANDIDATE_NAME = _slot_components.CANDIDATE_NAME
RUNTIME_OVERRIDE_NAME = _slot_components.RUNTIME_OVERRIDE_NAME
SOURCE_DIR_NAME = _slot_components.SOURCE_DIR_NAME
PRODUCT_PATHS = _slot_components.PRODUCT_PATHS
TRUSTED_REQUIRED_SOURCE_PATHS = _slot_components.TRUSTED_REQUIRED_SOURCE_PATHS
LEGACY_REQUIRED_SOURCE_PATHS = _slot_components.LEGACY_REQUIRED_SOURCE_PATHS
ADOPTED_REQUIRED_IMAGE_SERVICES = _slot_components.ADOPTED_REQUIRED_IMAGE_SERVICES
TARGET_REQUIRED_IMAGE_SERVICES = _slot_components.TARGET_REQUIRED_IMAGE_SERVICES
TARGET_BUILT_IMAGE_SERVICES = _slot_components.TARGET_BUILT_IMAGE_SERVICES
REQUIRED_ADOPTED_COMPOSE_SERVICES = _slot_components.REQUIRED_ADOPTED_COMPOSE_SERVICES
KNOWN_COMPOSE_SERVICES = _slot_components.KNOWN_COMPOSE_SERVICES
COMMIT_RE = _slot_components.COMMIT_RE
DIGEST_RE = _slot_components.DIGEST_RE
REQUEST_RE = _slot_components.REQUEST_RE
VERSION_RE = _slot_components.VERSION_RE
PROJECT_RE = _slot_components.PROJECT_RE
SERVICE_RE = _slot_components.SERVICE_RE
IMAGE_ID_RE = _slot_components.IMAGE_ID_RE
TRUSTED_SLOT_RE = _slot_components.TRUSTED_SLOT_RE
ADOPTED_SLOT_RE = _slot_components.ADOPTED_SLOT_RE
INITIAL_SLOT_RE = _slot_components.INITIAL_SLOT_RE
SLOT_RE = _slot_components.SLOT_RE
MACHINE_CODE_RE = _slot_components.MACHINE_CODE_RE
RELEASE_NOTE_LOCALES = _slot_components.RELEASE_NOTE_LOCALES
RELEASE_NOTE_CONTROL_RE = _slot_components.RELEASE_NOTE_CONTROL_RE
RELEASE_NOTE_SENSITIVE_RE = _slot_components.RELEASE_NOTE_SENSITIVE_RE
ACTIVATION_PHASES = _slot_components.ACTIVATION_PHASES
TERMINAL_ACTIVATION_PHASES = _slot_components.TERMINAL_ACTIVATION_PHASES
ACTIVATION_TRANSITIONS = _slot_components.ACTIVATION_TRANSITIONS
MAX_INVENTORY_ENTRIES = _slot_components.MAX_INVENTORY_ENTRIES
MAX_INVENTORY_BYTES = _slot_components.MAX_INVENTORY_BYTES
MAX_JSON_BYTES = _slot_components.MAX_JSON_BYTES
MAX_LEGACY_PYTHON_CACHE_ENTRIES = _slot_components.MAX_LEGACY_PYTHON_CACHE_ENTRIES
SlotError = _slot_components.SlotError
utc_now = _slot_components.utc_now
canonical_json = _slot_components.canonical_json
_validated_release_note_fields = _slot_components._validated_release_note_fields
validate_compose_services = _slot_components.validate_compose_services
validate_compose_evidence = _slot_components.validate_compose_evidence
validate_image_evidence = _slot_components.validate_image_evidence
validate_health_evidence = _slot_components.validate_health_evidence
validate_manifest = _slot_components.validate_manifest
_payload_digest = _slot_components._payload_digest
validate_activation_slot_binding = _slot_components.validate_activation_slot_binding
validate_activation_journal = _slot_components.validate_activation_journal
_source_path_excluded = _slot_components._source_path_excluded
_safe_relative = _slot_components._safe_relative
_hash_file = _slot_components._hash_file
product_inventory = _slot_components.product_inventory
_copy_product_source = _slot_components._copy_product_source
SlotStateStore = _slot_components.SlotStateStore



















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


def initial_slot_id(nonce: str) -> str:
    normalized = str(nonce or "").lower()
    if not DIGEST_RE.fullmatch(normalized):
        raise SlotError("initial_nonce_invalid", "Initial install nonce is invalid.")
    return f"initial-{normalized}"


def prepare_initial_slot_id(app_dir: Path) -> str:
    """Create or reuse the durable, installer-owned initial slot identity."""

    app_dir = resolve_app_dir(app_dir)
    path = app_dir / INITIAL_MATERIALIZATION_RELATIVE
    _ensure_plain_directory(path.parent)
    existing = read_json(path, missing_ok=True)
    if existing is not None:
        if (
            set(existing)
            != {"schema_version", "document_type", "nonce", "slot_id", "created_at"}
            or existing.get("schema_version") != 1
            or existing.get("document_type") != "initial_slot_materialization"
            or type(existing.get("nonce")) is not str
            or type(existing.get("slot_id")) is not str
            or initial_slot_id(existing["nonce"]) != existing["slot_id"]
            or type(existing.get("created_at")) is not str
            or not existing["created_at"]
        ):
            raise SlotError(
                "initial_materialization_invalid",
                "Initial install materialization evidence is invalid.",
            )
        return existing["slot_id"]
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    slot_id = initial_slot_id(nonce)
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "document_type": "initial_slot_materialization",
            "nonce": nonce,
            "slot_id": slot_id,
            "created_at": utc_now(),
        },
    )
    return slot_id


def require_slot_id(value: str, *, target: bool = False) -> str:
    normalized = str(value or "").lower()
    expected = TRUSTED_SLOT_RE if target else SLOT_RE
    if not expected.fullmatch(normalized):
        code = "target_slot_invalid" if target else "slot_id_invalid"
        raise SlotError(code, "Release slot ID is invalid.")
    return normalized












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
    _validated_release_note_fields(descriptor)
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
    release_identity.update(_validated_release_note_fields(descriptor))
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


def stage_initial(
    app_dir: Path,
    source_dir: Path,
    *,
    request_id: str,
    declared_version: str,
) -> dict[str, Any]:
    """Materialize one non-circular inventory-bound fresh-install slot."""

    app_dir = resolve_app_dir(app_dir)
    layout = ensure_layout(app_dir)
    request_id = require_request_id(request_id)
    if not VERSION_RE.fullmatch(str(declared_version or "")):
        raise SlotError("release_version_invalid", "Initial release version is invalid.")
    slot_id = prepare_initial_slot_id(app_dir)
    source_input = Path(source_dir)
    try:
        source_info = source_input.lstat()
        source_dir = source_input.resolve(strict=True)
    except OSError as exc:
        raise SlotError(
            "initial_source_invalid",
            "Initial product source is unavailable.",
        ) from exc
    if stat.S_ISLNK(source_info.st_mode) or not source_dir.is_dir():
        raise SlotError("initial_source_invalid", "Initial product source is unsafe.")
    identity = read_json(source_dir / ".km-vms-release.json")
    assert identity is not None
    if (
        identity.get("schema_version") != 1
        or identity.get("product") != "KM VMS"
        or identity.get("version") != declared_version
        or identity.get("commit_sha") is not None
        or identity.get("identity_mode") != "inventory_bound"
        or identity.get("slot_kind") != "initial_install_snapshot"
        or identity.get("slot_id") != slot_id
        or "source_inventory_sha256" in identity
        or "manifest_sha256" in identity
    ):
        raise SlotError(
            "initial_identity_invalid",
            "Initial source identity is incomplete or circular.",
        )
    final_path = layout["slots"] / slot_id
    if final_path.exists():
        manifest = validate_slot(final_path, expected_slot_id=slot_id)
        if (
            manifest["kind"] != "initial_install_snapshot"
            or manifest["declared_identity"]
            != {"version": declared_version, "commit": None}
        ):
            raise SlotError(
                "slot_conflict",
                "Existing initial slot contradicts this installation.",
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
        copied = product_inventory(
            candidate_source,
            required_paths=TRUSTED_REQUIRED_SOURCE_PATHS,
        )
        if source_before != source_after or source_before != copied:
            raise SlotError(
                "source_changed_during_materialization",
                "Initial source changed while it was being materialized.",
            )
        candidate = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "kind": "initial_install_snapshot",
            "request_id": request_id,
            "slot_id": slot_id,
            "declared_identity": {
                "version": declared_version,
                "commit": None,
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


def _same_filesystem_object(path: Path, expected: os.stat_result) -> os.stat_result:
    try:
        current = path.lstat()
    except OSError as exc:
        raise SlotError(
            "slot_legacy_cache_changed",
            "Legacy Python cache changed during safe convergence.",
        ) from exc
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise SlotError(
            "slot_legacy_cache_changed",
            "Legacy Python cache changed during safe convergence.",
        )
    return current


def _legacy_python_cache_plan(slot_root: Path, *, expected_slot_id: str) -> list[dict[str, Any]]:
    if slot_root.is_symlink() or not slot_root.is_dir() or slot_root.name != expected_slot_id:
        raise SlotError("slot_path_invalid", "Release slot path is unsafe.")
    manifest = read_json(slot_root / MANIFEST_NAME)
    assert manifest is not None
    validate_manifest(manifest, expected_slot_id=expected_slot_id)
    source_root = slot_root / SOURCE_DIR_NAME
    try:
        source_info = source_root.lstat()
    except OSError as exc:
        raise SlotError("slot_path_invalid", "Release slot source is unavailable.") from exc
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        raise SlotError("slot_path_invalid", "Release slot source is unsafe.")

    plan: list[dict[str, Any]] = []
    scanned = 0
    cache_entries = 0
    for current, dirnames, filenames in os.walk(
        source_root,
        topdown=True,
        followlinks=False,
    ):
        scanned += len(dirnames) + len(filenames)
        if scanned > MAX_INVENTORY_ENTRIES:
            raise SlotError(
                "slot_inventory_too_large",
                "Release slot source inventory exceeds the bounded limit.",
            )
        current_path = Path(current)
        if "__pycache__" in filenames:
            raise SlotError(
                "slot_legacy_cache_unsafe",
                "Legacy Python cache path is not a directory.",
            )
        descend: list[str] = []
        for name in dirnames:
            path = current_path / name
            info = path.lstat()
            if name != "__pycache__":
                if not stat.S_ISLNK(info.st_mode):
                    descend.append(name)
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SlotError(
                    "slot_legacy_cache_unsafe",
                    "Legacy Python cache contains an unsafe filesystem object.",
                )
            files: list[tuple[Path, os.stat_result]] = []
            try:
                with os.scandir(path) as iterator:
                    entries = list(iterator)
            except OSError as exc:
                raise SlotError(
                    "slot_legacy_cache_unsafe",
                    "Legacy Python cache cannot be inspected safely.",
                ) from exc
            cache_entries += len(entries)
            if cache_entries > MAX_LEGACY_PYTHON_CACHE_ENTRIES:
                raise SlotError(
                    "slot_legacy_cache_too_large",
                    "Legacy Python cache exceeds the bounded cleanup limit.",
                )
            for entry in entries:
                entry_path = path / entry.name
                entry_info = entry_path.lstat()
                if (
                    not entry.name.endswith(".pyc")
                    or stat.S_ISLNK(entry_info.st_mode)
                    or not stat.S_ISREG(entry_info.st_mode)
                ):
                    raise SlotError(
                        "slot_legacy_cache_unsafe",
                        "Legacy Python cache contains an unexpected entry.",
                    )
                files.append((entry_path, entry_info))
            plan.append(
                {
                    "path": path,
                    "info": info,
                    "parent": current_path,
                    "parent_info": current_path.lstat(),
                    "files": files,
                }
            )
        dirnames[:] = descend
    return plan


def _converge_active_slot_legacy_python_cache(
    slot_root: Path,
    *,
    expected_slot_id: str,
) -> int:
    """Remove only legacy interpreter caches excluded from slot identity."""
    plan = _legacy_python_cache_plan(
        slot_root,
        expected_slot_id=expected_slot_id,
    )
    removed = 0
    for item in plan:
        cache_path = item["path"]
        parent_path = item["parent"]
        cache_info = _same_filesystem_object(cache_path, item["info"])
        parent_info = _same_filesystem_object(parent_path, item["parent_info"])
        if not stat.S_ISDIR(cache_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise SlotError(
                "slot_legacy_cache_changed",
                "Legacy Python cache changed during safe convergence.",
            )
        cache_mode = stat.S_IMODE(cache_info.st_mode)
        parent_mode = stat.S_IMODE(parent_info.st_mode)
        try:
            os.chmod(parent_path, parent_mode | stat.S_IWUSR | stat.S_IXUSR)
            os.chmod(cache_path, cache_mode | stat.S_IWUSR | stat.S_IXUSR)
            for file_path, file_info in item["files"]:
                current = _same_filesystem_object(file_path, file_info)
                if not stat.S_ISREG(current.st_mode):
                    raise SlotError(
                        "slot_legacy_cache_changed",
                        "Legacy Python cache changed during safe convergence.",
                    )
                file_path.unlink()
                removed += 1
            cache_path.rmdir()
        except SlotError:
            raise
        except OSError as exc:
            raise SlotError(
                "slot_legacy_cache_cleanup_failed",
                "Legacy Python cache could not be removed safely.",
            ) from exc
        finally:
            try:
                if cache_path.exists() and not cache_path.is_symlink():
                    os.chmod(cache_path, cache_mode)
            except OSError:
                pass
            try:
                current_parent = parent_path.lstat()
                if (
                    not stat.S_ISLNK(current_parent.st_mode)
                    and stat.S_ISDIR(current_parent.st_mode)
                ):
                    os.chmod(parent_path, parent_mode)
            except OSError:
                pass
    return removed


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
        LEGACY_REQUIRED_SOURCE_PATHS
        if manifest["kind"] == "adopted_pre_update_snapshot"
        else TRUSTED_REQUIRED_SOURCE_PATHS
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
            "Non-legacy slot contains an unexpected runtime override.",
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
        not in {
            "trusted_release",
            "adopted_pre_update_snapshot",
            "initial_install_snapshot",
        }
    ):
        raise SlotError("candidate_invalid", "Prepared release candidate is invalid.")
    required_paths = (
        LEGACY_REQUIRED_SOURCE_PATHS
        if candidate.get("kind") == "adopted_pre_update_snapshot"
        else TRUSTED_REQUIRED_SOURCE_PATHS
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
            raise SlotError(
                "health_evidence_invalid",
                "Non-legacy slot cannot claim pre-update health.",
            )
        if (
            compose["runtime_override_sha256"] is not None
            or (candidate_root / RUNTIME_OVERRIDE_NAME).exists()
            or (candidate_root / RUNTIME_OVERRIDE_NAME).is_symlink()
        ):
            raise SlotError(
                "runtime_override_invalid",
                "Non-legacy slot cannot contain a runtime override.",
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




def publish_installed_slot_projection(
    app_dir: Path,
    *,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish sanitized, derived installed-slot evidence outside source inventory."""

    app_dir = resolve_app_dir(app_dir)
    active = read_active_slot(app_dir)
    if active is None:
        raise SlotError(
            "installed_projection_unavailable",
            "An active slot is required before publishing installed evidence.",
        )
    slot_id, _source = active
    manifest = validate_slot(
        ensure_layout(app_dir)["slots"] / slot_id,
        expected_slot_id=slot_id,
    )
    base_binding = build_activation_slot_binding(app_dir, slot_id)
    if binding is None:
        binding = base_binding
    else:
        binding = validate_activation_slot_binding(binding)
        stable_keys = set(base_binding) - {
            "compose_plan_sha256",
            "archive_override_sha256",
        }
        if (
            binding["slot_id"] != slot_id
            or any(binding[key] != base_binding[key] for key in stable_keys)
        ):
            raise SlotError(
                "installed_projection_binding_invalid",
                "Installed-slot projection binding contradicts the active slot.",
            )
    identity_mode = (
        "inventory_bound"
        if manifest["kind"] == "initial_install_snapshot"
        else "trusted_release"
        if manifest["kind"] == "trusted_release"
        else "adopted_inventory"
    )
    payload = {
        "schema_version": 1,
        "document_type": "installed_slot_projection",
        "slot_id": slot_id,
        "kind": manifest["kind"],
        "identity_mode": identity_mode,
        "version": manifest["declared_identity"]["version"],
        "commit": manifest["declared_identity"]["commit"],
        "inventory_sha256": manifest["source_inventory"]["sha256"],
        "manifest_sha256": _payload_digest(manifest),
        "active_slot_id": slot_id,
        "binding": binding,
        "published_at": utc_now(),
    }
    path = app_dir / INSTALLED_PROJECTION_RELATIVE
    _ensure_plain_directory(path.parent)
    atomic_write_json(path, payload)
    try:
        path.chmod(0o444)
    except OSError as exc:
        raise SlotError(
            "installed_projection_publish_failed",
            "Installed-slot projection permissions could not be finalized.",
        ) from exc
    return payload


def _slot_state_store() -> SlotStateStore:
    return SlotStateStore(
        ensure_layout, resolve_app_dir, require_slot_id, require_request_id, validate_slot, _converge_active_slot_legacy_python_cache, _fsync_directory, read_json, atomic_write_json,
    )


def validate_installed_slot_projection(
    app_dir: Path,
    *,
    require_active: bool = True,
) -> dict[str, Any]:
    return _slot_state_store().validate_installed_slot_projection(app_dir, require_active=require_active)




def read_active_slot(app_dir: Path) -> tuple[str, Path] | None:
    return _slot_state_store().read_active_slot(app_dir)


def atomic_switch_pointer(app_dir: Path, slot_id: str) -> Path:
    return _slot_state_store().atomic_switch_pointer(app_dir, slot_id)


def read_activation_journal(
    app_dir: Path,
    *,
    missing_ok: bool = False,
) -> dict[str, Any] | None:
    return _slot_state_store().read_activation_journal(app_dir, missing_ok=missing_ok)


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
    return _slot_state_store().initialize_activation_journal(app_dir, request_id=request_id, previous=previous, target=target, compatibility_sha256=compatibility_sha256, source_schema_version=source_schema_version, target_schema_version=target_schema_version, migration_required=migration_required, migration_attempt_id=migration_attempt_id)


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
    rollback_database_verified: bool = False,
) -> dict[str, Any]:
    return _slot_state_store().transition_activation_journal(app_dir, request_id=request_id, phase=phase, pointer_slot_id=pointer_slot_id, record_pointer=record_pointer, migration_invoked=migration_invoked, migration_completed=migration_completed, target_verified=target_verified, previous_verified=previous_verified, rollback_trigger=rollback_trigger, failure_category=failure_category, rollback_database_verified=rollback_database_verified)


def protected_slot_ids(app_dir: Path) -> set[str]:
    return _slot_state_store().protected_slot_ids(app_dir)


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


def cleanup_failed_pre_journal_staging(
    app_dir: Path,
    *,
    request_id: str,
    failed_request_evidence: bool = False,
) -> dict[str, Any]:
    """Remove only one failed request's staging before activation begins."""

    app_dir = resolve_app_dir(app_dir)
    request_id = require_request_id(request_id)
    if failed_request_evidence is not True:
        raise SlotError(
            "cleanup_failure_evidence_required",
            "Failed request staging cleanup requires terminal failure evidence.",
        )
    journal = read_activation_journal(app_dir, missing_ok=True)
    if journal is not None and journal.get("request_id") == request_id:
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "request_id": request_id,
            "status": "activation_started",
            "removed": False,
        }
    removed = cleanup_request_staging(
        app_dir,
        request_id=request_id,
        terminal_evidence=True,
    )
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "request_id": request_id,
        "status": "removed" if removed else "absent",
        "removed": removed,
    }


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

    initial_id_parser = subparsers.add_parser("prepare-initial-id")
    initial_id_parser.add_argument("--app-dir", required=True)

    stage_initial_parser = subparsers.add_parser("stage-initial")
    stage_initial_parser.add_argument("--app-dir", required=True)
    stage_initial_parser.add_argument("--source-dir", required=True)
    stage_initial_parser.add_argument("--request-id", required=True)
    stage_initial_parser.add_argument("--declared-version", required=True)

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

    cleanup_failed_parser = subparsers.add_parser("cleanup-failed-staging")
    cleanup_failed_parser.add_argument("--app-dir", required=True)
    cleanup_failed_parser.add_argument("--request-id", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--app-dir", required=True)
    resolve_parser = subparsers.add_parser("resolve-active")
    resolve_parser.add_argument("--app-dir", required=True)
    resolve_path_parser = subparsers.add_parser("resolve-active-path")
    resolve_path_parser.add_argument("--app-dir", required=True)
    switch_parser = subparsers.add_parser("switch-active")
    switch_parser.add_argument("--app-dir", required=True)
    switch_parser.add_argument("--slot-id", required=True)
    publish_projection_parser = subparsers.add_parser(
        "publish-installed-projection"
    )
    publish_projection_parser.add_argument("--app-dir", required=True)
    validate_projection_parser = subparsers.add_parser(
        "validate-installed-projection"
    )
    validate_projection_parser.add_argument("--app-dir", required=True)
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
        elif args.command == "prepare-initial-id":
            app_dir = resolve_app_dir(args.app_dir)
            result = {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "slot_id": prepare_initial_slot_id(app_dir),
            }
        elif args.command == "stage-initial":
            result = stage_initial(
                Path(args.app_dir),
                Path(args.source_dir),
                request_id=args.request_id,
                declared_version=args.declared_version,
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
        elif args.command == "cleanup-failed-staging":
            result = cleanup_failed_pre_journal_staging(
                Path(args.app_dir),
                request_id=args.request_id,
                failed_request_evidence=True,
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
                raise SlotError(
                    "active_pointer_missing",
                    "No canonical active release slot is selected.",
                )
            result = {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "mode": "release_slot",
                "slot_id": active[0],
                "source_path": str(active[1]),
            }
        elif args.command == "resolve-active-path":
            app_dir = resolve_app_dir(args.app_dir)
            active = read_active_slot(app_dir)
            if active is None:
                raise SlotError(
                    "active_pointer_missing",
                    "No canonical active release slot is selected.",
                )
            print(str(active[1]))
            return 0
        elif args.command == "switch-active":
            app_dir = resolve_app_dir(args.app_dir)
            source = atomic_switch_pointer(app_dir, args.slot_id)
            result = {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "slot_id": require_slot_id(args.slot_id),
                "source_path": str(source),
            }
        elif args.command == "publish-installed-projection":
            result = publish_installed_slot_projection(
                Path(args.app_dir)
            )
        elif args.command == "validate-installed-projection":
            result = validate_installed_slot_projection(
                Path(args.app_dir)
            )
        else:
            raise SlotError("command_invalid", "Unsupported release-slot command.")
    except SlotError as exc:
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
