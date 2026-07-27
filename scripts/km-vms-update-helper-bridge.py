#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


MAX_CONTROL_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 7800
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 9000
DEFAULT_POLL_SECONDS = 2
PERMISSION_GATE_TIMEOUT_SECONDS = 300

REQUEST_ID_RE = re.compile(
    r"^(?:update|stage609)-[0-9a-f]{32}$",
    re.IGNORECASE,
)
PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
HELPER_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
ARCHIVE_RUNTIME_TARGET_RE = re.compile(
    r"^/storage/archive-roots/[A-Za-z0-9_.-]{1,180}$"
)

UPDATE_LINEAGE_FILENAME = "km-vms-update-lineage.json"
UPDATE_LINEAGE_MAX_BYTES = 128 * 1024


def load_update_lineage() -> dict[str, Any]:
    configured = str(os.getenv("KMVMS_UPDATE_LINEAGE_FILE") or "").strip()
    candidates = (
        [Path(configured)]
        if configured
        else [
            parent / "release" / UPDATE_LINEAGE_FILENAME
            for parent in (Path.cwd(), *Path(__file__).resolve().parents)
        ]
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise RuntimeError("update_lineage_file_missing")
    info = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or info.st_size <= 1
        or info.st_size > UPDATE_LINEAGE_MAX_BYTES
    ):
        raise RuntimeError("update_lineage_file_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("update_lineage_json_invalid") from exc
    required = {
        "schema_version",
        "product",
        "tag_commits",
        "schema_versions",
        "shape_fingerprints",
        "shape_alternates",
    }
    tag_commits = payload.get("tag_commits") if type(payload) is dict else None
    schema_versions = payload.get("schema_versions") if type(payload) is dict else None
    shape_fingerprints = (
        payload.get("shape_fingerprints") if type(payload) is dict else None
    )
    shape_alternates = (
        payload.get("shape_alternates") if type(payload) is dict else None
    )
    if (
        type(payload) is not dict
        or set(payload) != required
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or payload.get("product") != "KM VMS"
        or type(tag_commits) is not dict
        or type(schema_versions) is not dict
        or type(shape_fingerprints) is not dict
        or type(shape_alternates) is not dict
        or not tag_commits
        or len(tag_commits) > 256
        or set(tag_commits) != set(schema_versions)
        or set(tag_commits) != set(shape_fingerprints)
        or not set(shape_alternates).issubset(tag_commits)
    ):
        raise RuntimeError("update_lineage_contract_invalid")
    versions = list(tag_commits)

    def version_key(value: str) -> tuple[int, int, int]:
        if not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise RuntimeError("update_lineage_version_invalid")
        return tuple(int(part) for part in value.split("."))

    if versions != sorted(versions, key=version_key):
        raise RuntimeError("update_lineage_order_invalid")
    for version in versions:
        commit = tag_commits.get(version)
        schema_version = schema_versions.get(version)
        shape = shape_fingerprints.get(version)
        alternates = shape_alternates.get(version, [])
        if (
            type(commit) is not str
            or not COMMIT_SHA_RE.fullmatch(commit)
            or commit != commit.lower()
            or type(schema_version) is not int
            or schema_version < 1
            or schema_version > 8
            or type(shape) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", shape)
            or type(alternates) is not list
            or len(alternates) > 4
            or len(set(alternates)) != len(alternates)
            or shape in alternates
            or any(
                type(item) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", item)
                for item in alternates
            )
        ):
            raise RuntimeError("update_lineage_entry_invalid")
    return payload


UPDATE_LINEAGE = load_update_lineage()
SOURCE_TAG_COMMITS: dict[str, str] = dict(UPDATE_LINEAGE["tag_commits"])

ACTIVE_STATUSES = {
    "queued",
    "starting_helper",
    "preflight",
    "acquire_source",
    "downloading",
    "extracting",
    "validating_source",
    "overlay",
    "applying",
    "compose_config",
    "rebuilding",
    "restarting",
    "health_check",
    "commit_verification",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked"}
INACTIVE_STATUSES = {"idle", "unknown"}
KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES | INACTIVE_STATUSES
RECEIPT_STATUSES = {"waiting_for_terminal", "recreating", "completed", "failed"}
UNBOUND_STATUS_ALLOWED = {"idle", "unknown", "blocked"}


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            raise BridgeError("control_file_invalid", "Update control data is not a regular file.")
        if not path.exists():
            if missing_ok:
                return None
            raise BridgeError("control_file_missing", "Required update control data is missing.")
        if not path.is_file() or path.is_symlink():
            raise BridgeError("control_file_invalid", "Update control data is not a regular file.")
        if path.stat().st_size > MAX_CONTROL_BYTES:
            raise BridgeError("control_file_too_large", "Update control data exceeds its size limit.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except BridgeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise BridgeError("control_file_invalid", "Update control data is unavailable or invalid.") from exc
    if not isinstance(payload, dict):
        raise BridgeError("control_file_invalid", "Update control data must be a JSON object.")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(rendered.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise BridgeError("receipt_too_large", "Update-helper refresh receipt exceeds its size limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(tmp, 0o600)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise BridgeError("receipt_write_failed", "Cannot persist update-helper refresh receipt.") from exc


def read_regular_text(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise BridgeError(
                "archive_roots_override_invalid",
                "Generated archive-roots Compose override is not a regular file.",
            )
        if path.stat().st_size > MAX_CONTROL_BYTES:
            raise BridgeError(
                "archive_roots_override_invalid",
                "Generated archive-roots Compose override exceeds its size limit.",
            )
        return path.read_text(encoding="utf-8")
    except BridgeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BridgeError(
            "archive_roots_override_invalid",
            "Generated archive-roots Compose override is unavailable.",
        ) from exc


def atomic_write_text(path: Path, rendered: str) -> None:
    if len(rendered.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise BridgeError(
            "archive_roots_override_invalid",
            "Generated archive-roots Compose override exceeds its size limit.",
        )
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(tmp, 0o600)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise BridgeError(
            "archive_roots_override_write_failed",
            "Cannot normalize the generated archive-roots Compose override.",
        ) from exc


def _compose_yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _archive_volume_lines(manifest: dict[str, Any]) -> list[str]:
    expected_manifest_fields = {
        "schema_version",
        "runtime_base",
        "compose_override_file",
        "items",
        "raw_runtime_paths_user_visible",
    }
    if (
        set(manifest) != expected_manifest_fields
        or manifest.get("schema_version") != 1
        or manifest.get("runtime_base") != "/storage/archive-roots"
        or manifest.get("compose_override_file")
        != "docker-compose.archive-roots.yml"
        or manifest.get("raw_runtime_paths_user_visible") is not False
        or not isinstance(manifest.get("items"), list)
        or len(manifest["items"]) > 128
    ):
        raise BridgeError(
            "archive_roots_manifest_invalid",
            "Generated archive-roots runtime manifest is invalid.",
        )
    expected_item_fields = {
        "root_id",
        "user_display_path",
        "backend_runtime_path",
        "physical_volume_id",
        "storage_namespace",
        "active_write_target",
    }
    lines: list[str] = []
    seen_targets: set[str] = set()
    for item in manifest["items"]:
        if not isinstance(item, dict) or set(item) != expected_item_fields:
            raise BridgeError(
                "archive_roots_manifest_invalid",
                "Generated archive-roots runtime manifest has an invalid item.",
            )
        source = item.get("user_display_path")
        target = item.get("backend_runtime_path")
        if (
            not isinstance(source, str)
            or not source.startswith("/")
            or len(source) > 1024
            or any(char in source for char in ("\x00", "\r", "\n"))
            or any(part == ".." for part in Path(source).parts)
            or not isinstance(target, str)
            or not ARCHIVE_RUNTIME_TARGET_RE.fullmatch(target)
            or target in seen_targets
            or type(item.get("active_write_target")) is not bool
        ):
            raise BridgeError(
                "archive_roots_manifest_invalid",
                "Generated archive-roots runtime manifest has an unsafe item.",
            )
        seen_targets.add(target)
        lines.extend(
            [
                "      - type: bind",
                f"        source: {_compose_yaml_quote(source)}",
                f"        target: {_compose_yaml_quote(target)}",
                "        read_only: false",
                "        bind:",
                "          create_host_path: false",
            ]
        )
    return lines


def normalize_archive_roots_override(app_dir: Path) -> bool:
    control_dir = app_dir / "data/install-control"
    manifest_path = control_dir / "archive-roots-runtime.json"
    override_path = control_dir / "docker-compose.archive-roots.yml"
    manifest_exists = manifest_path.exists()
    override_exists = override_path.exists()
    if not manifest_exists and not override_exists:
        return False
    if manifest_exists != override_exists:
        raise BridgeError(
            "archive_roots_contract_partial",
            "Generated archive-roots runtime contract is partial.",
        )
    manifest = read_json_object(manifest_path)
    assert manifest is not None
    volume_lines = _archive_volume_lines(manifest)
    if volume_lines:
        legacy = "\n".join(
            [
                "# Generated by KM VMS. Do not edit manually.",
                "services:",
                "  api:",
                "    volumes:",
                *volume_lines,
                "",
            ]
        )
        intermediate = "\n".join(
            [
                "# Generated by KM VMS. Do not edit manually.",
                "services:",
                "  api:",
                "    volumes:",
                *volume_lines,
                "  operation-recovery:",
                "    volumes:",
                *volume_lines,
                "",
            ]
        )
        target = "\n".join(
            [
                "# Generated by KM VMS. Do not edit manually.",
                "services:",
                "  api:",
                "    volumes:",
                *volume_lines,
                "  schema-update:",
                "    volumes:",
                *volume_lines,
                "",
            ]
        )
    else:
        legacy = (
            "# Generated by KM VMS. No archive roots configured.\n"
            "services: {}\n"
        )
        intermediate = legacy
        target = legacy
    current = read_regular_text(override_path)
    if current == target:
        return False
    if current not in {legacy, intermediate}:
        raise BridgeError(
            "archive_roots_override_invalid",
            "Generated archive-roots Compose override does not match its manifest.",
        )
    atomic_write_text(override_path, target)
    return True


def extract_active_request(
    authority: dict[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    if (
        type(authority.get("schema_version")) is int
        and authority["schema_version"] == 1
    ):
        if authority.get("request_id") != request_id:
            raise BridgeError(
                "source_handoff_request_mismatch",
                "Legacy update request does not match the active helper request.",
            )
        return authority
    document_fields = {
        "schema_version",
        "document_type",
        "current_submission_id",
        "entries",
        "updated_at",
    }
    if (
        set(authority) != document_fields
        or type(authority.get("schema_version")) is not int
        or authority["schema_version"] != 2
        or authority.get("document_type")
        != "update_apply_admission"
        or type(authority.get("entries")) is not list
        or len(authority["entries"]) > 64
    ):
        raise BridgeError(
            "source_handoff_authority_invalid",
            "Update admission authority has an unsupported shape.",
        )
    entry_fields = {
        "submission_id",
        "request_id",
        "target_version",
        "target_commit",
        "requested_at",
        "updated_at",
        "state",
        "request",
        "audit",
        "claimed_at",
        "terminal",
    }
    matches = [
        entry
        for entry in authority["entries"]
        if type(entry) is dict
        and set(entry) == entry_fields
        and entry.get("request_id") == request_id
    ]
    if len(matches) != 1:
        raise BridgeError(
            "source_handoff_authority_invalid",
            "Active update admission is missing or ambiguous.",
        )
    entry = matches[0]
    request = entry.get("request")
    request_source = (
        request.get("source")
        if type(request) is dict
        else None
    )
    entry_commit = entry.get("target_commit")
    if (
        authority.get("current_submission_id")
        != entry.get("submission_id")
        or type(entry.get("submission_id")) is not str
        or not entry["submission_id"]
        or entry.get("state") != "claimed"
        or type(request) is not dict
        or request.get("request_id") != request_id
        or request.get("submission_id") != entry.get("submission_id")
        or type(request.get("schema_version")) is not int
        or request["schema_version"] != 2
        or type(request_source) is not dict
        or type(entry_commit) is not str
        or not COMMIT_SHA_RE.fullmatch(entry_commit)
        or type(request_source.get("commit")) is not str
        or entry_commit.lower() != request_source["commit"].lower()
        or type(entry.get("target_version")) is not str
        or entry["target_version"] != request_source.get("version")
    ):
        raise BridgeError(
            "source_handoff_authority_invalid",
            "Active update admission bindings are contradictory.",
        )
    return request


def capture_installed_source_identity(
    app_dir: Path,
    *,
    request_id: str,
) -> None:
    control_dir = app_dir / "data/update-control"
    authority = read_json_object(control_dir / "update-request.json")
    source_identity = read_json_object(app_dir / ".km-vms-source.json")
    release_identity = read_json_object(app_dir / "release/km-vms-release.json")
    assert authority is not None
    assert source_identity is not None
    assert release_identity is not None
    request = extract_active_request(
        authority,
        request_id=request_id,
    )
    source = request.get("source")
    target_commit = (
        str(source.get("commit") or "").lower()
        if isinstance(source, dict)
        else ""
    )
    requested_target_version = (
        str(source.get("version") or "")
        if isinstance(source, dict)
        else ""
    )
    target_repo = (
        str(source.get("repo") or "").lower()
        if isinstance(source, dict)
        else ""
    )
    release_commit_value = release_identity.get("commit_sha")
    release_commit = (
        str(release_commit_value).lower()
        if isinstance(release_commit_value, str)
        else None
    )
    release_version = str(release_identity.get("version") or "")
    installed_commit = str(source_identity.get("commit_sha") or "").lower()
    installed_repo = str(source_identity.get("github_repo") or "")
    if (
        type(request.get("schema_version")) is not int
        or request.get("schema_version") not in {1, 2}
        or request.get("request_id") != request_id
        or request.get("intent") != "apply_update"
        or request.get("confirmed") is not True
        or not COMMIT_SHA_RE.fullmatch(target_commit)
        or target_repo != "kmishnev87/km-vms"
        or not release_version
        or (
            requested_target_version
            and requested_target_version != release_version
        )
        or type(release_identity.get("schema_version")) is not int
        or release_identity["schema_version"] != 1
        or release_identity.get("product") != "KM VMS"
        or release_identity.get("tag") != f"v{release_version}"
        or release_identity.get("source_kind") != "github-release"
        or str(release_identity.get("source_repo") or "").lower()
        != target_repo
        or release_identity.get("source_ref") != f"v{release_version}"
        or release_identity.get("evidence_model")
        != "semver_tag_resolves_to_commit"
        or "commit_sha" not in release_identity
        or (
            release_commit is not None
            and (
                not COMMIT_SHA_RE.fullmatch(release_commit)
                or release_commit != target_commit
            )
        )
        or installed_repo.lower() != "kmishnev87/km-vms"
        or type(source_identity.get("schema_version")) is not int
        or source_identity["schema_version"] != 1
        or not COMMIT_SHA_RE.fullmatch(installed_commit)
    ):
        raise BridgeError(
            "source_handoff_invalid",
            "Installed and target source identities cannot be bound safely.",
        )
    installed_version = next(
        (
            version
            for version, commit in SOURCE_TAG_COMMITS.items()
            if commit == installed_commit
        ),
        None,
    )
    if installed_version is None and installed_commit == target_commit:
        installed_version = release_version
    if installed_version is None:
        raise BridgeError(
            "installed_source_unsupported",
            "Installed source commit is outside the supported update lineage.",
        )
    if request_id.lower().startswith("stage609-") != (
        installed_version in {"0.7.2", "0.7.3"}
    ):
        raise BridgeError(
            "source_request_family_mismatch",
            "Installed source and legacy request families do not match.",
        )
    identity = {
        "schema_version": 1,
        "request_id": request_id.lower(),
        "installed_version": installed_version,
        "installed_commit": installed_commit,
        "recorded_at": utcnow(),
    }
    identity_path = control_dir / "pre-overlay-source-identity.json"
    existing = read_json_object(identity_path, missing_ok=True)
    if existing is not None and existing.get("request_id") == request_id.lower():
        existing_fields = {
            "schema_version",
            "request_id",
            "installed_version",
            "installed_commit",
            "recorded_at",
        }
        existing_version = str(existing.get("installed_version") or "")
        existing_commit = str(existing.get("installed_commit") or "").lower()
        lineage_commit = SOURCE_TAG_COMMITS.get(existing_version)
        existing_lineage_valid = (
            lineage_commit == existing_commit
            if lineage_commit is not None
            else (
                existing_version == release_version
                and existing_commit == target_commit
            )
        )
        existing_family_valid = (
            request_id.lower().startswith("stage609-")
            == (existing_version in {"0.7.2", "0.7.3"})
        )
        if (
            set(existing) != existing_fields
            or existing.get("schema_version") != 1
            or existing.get("request_id") != request_id.lower()
            or not isinstance(existing.get("recorded_at"), str)
            or not existing.get("recorded_at")
            or not COMMIT_SHA_RE.fullmatch(existing_commit)
            or not existing_lineage_valid
            or not existing_family_valid
        ):
            raise BridgeError(
                "source_handoff_conflict",
                "Installed source handoff evidence is contradictory.",
            )
    else:
        atomic_write_json(identity_path, identity)
    request_path = control_dir / "schema-update-request.json"
    existing_request = read_json_object(request_path, missing_ok=True)
    if existing_request is not None:
        if (
            existing_request.get("request_id") == request_id
            and existing_request != request
        ):
            raise BridgeError(
                "source_handoff_request_conflict",
                "Normalized schema update request is contradictory.",
            )
        if existing_request.get("request_id") != request_id:
            atomic_write_json(request_path, request)
    else:
        atomic_write_json(request_path, request)


def read_status(path: Path) -> tuple[str | None, str, dict[str, Any]] | None:
    payload = read_json_object(path, missing_ok=True)
    if payload is None:
        return None
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
        raise BridgeError("status_schema_invalid", "Update status has an unsupported schema version.")
    request_id = payload.get("request_id")
    status = payload.get("status")
    if not isinstance(status, str) or status not in KNOWN_STATUSES:
        raise BridgeError("status_value_invalid", "Update status has an unsupported top-level state.")
    if request_id is None and status in UNBOUND_STATUS_ALLOWED:
        return None, status, payload
    if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id):
        return request_id, status, payload
    if status in TERMINAL_STATUSES or status in INACTIVE_STATUSES:
        # A pre-canonical historical terminal record is inert. It must not
        # prevent a normal restart after this bridge is first installed, but
        # it can never be accepted by a coordinator waiting for a canonical
        # active request because its returned identity is deliberately None.
        return None, status, payload
    raise BridgeError("status_request_invalid", "Update status has no canonical request identity.")


def validate_completed_status(payload: dict[str, Any], request_id: str) -> None:
    if payload.get("request_id") != request_id or payload.get("status") != "completed":
        raise BridgeError("terminal_status_invalid", "Terminal update completion is not bound to the expected request.")
    expected_commit = payload.get("expected_commit")
    installed_commit = payload.get("installed_commit")
    if (
        payload.get("commit_verified") is not True
        or not isinstance(expected_commit, str)
        or not COMMIT_SHA_RE.fullmatch(expected_commit)
        or not isinstance(installed_commit, str)
        or not COMMIT_SHA_RE.fullmatch(installed_commit)
        or expected_commit.lower() != installed_commit.lower()
    ):
        raise BridgeError("terminal_commit_invalid", "Terminal update completion lacks matching verified commit evidence.")
    terminal_timestamp = payload.get("finished_at")
    if terminal_timestamp is None:
        # Legacy helpers use updated_at as the completion timestamp and
        # synthesize finished_at only in their immutable history item.
        terminal_timestamp = payload.get("updated_at")
    try:
        parsed_terminal_timestamp = datetime.fromisoformat(
            str(terminal_timestamp).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        parsed_terminal_timestamp = None
    if (
        not isinstance(terminal_timestamp, str)
        or not terminal_timestamp
        or len(terminal_timestamp) > 80
        or parsed_terminal_timestamp is None
        or parsed_terminal_timestamp.tzinfo is None
    ):
        raise BridgeError("terminal_timestamp_invalid", "Terminal update completion lacks a finish timestamp.")


def require_request_id(value: str) -> str:
    if not REQUEST_ID_RE.fullmatch(value):
        raise BridgeError("request_id_invalid", "A canonical update request id is required.")
    return value


def require_project_name(value: str) -> str:
    if not PROJECT_NAME_RE.fullmatch(value):
        raise BridgeError("project_name_invalid", "A safe Docker Compose project name is required.")
    return value


def require_helper_image(value: str) -> str:
    if not HELPER_IMAGE_RE.fullmatch(value):
        raise BridgeError("helper_image_invalid", "A safe target update-helper image reference is required.")
    return value


def require_image_id(value: str) -> str:
    normalized = value.strip().lower()
    if not IMAGE_ID_RE.fullmatch(normalized):
        raise BridgeError("image_id_invalid", "Target update-helper image identity is invalid.")
    return normalized


def require_timeout(value: int) -> int:
    if value < MIN_TIMEOUT_SECONDS or value > MAX_TIMEOUT_SECONDS:
        raise BridgeError(
            "timeout_invalid",
            f"Refresh timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS} seconds.",
        )
    return value


def require_app_dir(value: str) -> Path:
    if not value or "\x00" in value or "\n" in value or "\r" in value or ":" in value:
        raise BridgeError("app_dir_invalid", "A safe absolute host app directory is required.")
    app_dir = Path(value)
    if not app_dir.is_absolute() or not app_dir.is_dir():
        raise BridgeError("app_dir_invalid", "The host app directory is not mounted or is not absolute.")
    for relative in (
        "docker-compose.yml",
        ".env",
        "scripts/km-vms-permission-gate.sh",
        "scripts/km-vms-update-helper-bridge.py",
    ):
        candidate = app_dir / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise BridgeError("app_dir_incomplete", "The host app directory is missing required bridge files.")
    return app_dir


def run_command(
    args: Sequence[str],
    *,
    timeout: int,
    error_code: str,
    error_message: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError(error_code, error_message) from exc
    if check and result.returncode != 0:
        raise BridgeError(error_code, error_message)
    return result


def ensure_docker_runtime() -> None:
    run_command(
        ["docker", "version"],
        timeout=20,
        error_code="docker_unavailable",
        error_message="Docker daemon is unavailable to the update-helper bridge.",
    )
    run_command(
        ["docker", "compose", "version"],
        timeout=20,
        error_code="compose_unavailable",
        error_message="Docker Compose is unavailable to the update-helper bridge.",
    )


def docker_image_id(image: str) -> str:
    result = run_command(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        timeout=30,
        error_code="helper_image_missing",
        error_message="The target update-helper image was not prepared.",
    )
    return require_image_id(result.stdout.strip())


def inspected_container_image(name_or_id: str) -> str | None:
    result = run_command(
        ["docker", "inspect", "--format", "{{.Image}}", name_or_id],
        timeout=20,
        error_code="container_inspect_failed",
        error_message="Cannot inspect the update-helper bridge container.",
        check=False,
    )
    if result.returncode != 0:
        return None
    return require_image_id(result.stdout.strip())


def write_receipt(
    receipt_file: Path,
    *,
    request_id: str,
    status: str,
    expected_image_id: str,
    message: str,
    helper_container_id: str | None = None,
) -> None:
    atomic_write_json(
        receipt_file,
        {
            "schema_version": 1,
            "request_id": request_id,
            "status": status,
            "expected_image_id": expected_image_id,
            "helper_container_id": helper_container_id,
            "updated_at": utcnow(),
            "message": message,
        },
    )


def validate_receipt_binding(receipt_file: Path, request_id: str, expected_image_id: str) -> None:
    payload = read_json_object(receipt_file, missing_ok=True)
    if payload is None or payload.get("request_id") != request_id:
        return
    status = payload.get("status")
    if status not in RECEIPT_STATUSES:
        raise BridgeError("receipt_status_invalid", "Existing helper refresh receipt has an invalid status.")
    if payload.get("expected_image_id") != expected_image_id:
        raise BridgeError("receipt_image_mismatch", "Existing helper refresh receipt names another target image.")
    if status == "completed":
        raise BridgeError("receipt_state_contradiction", "Helper refresh is completed while the same update remains active.")


def run_target_permission_gate(app_dir: Path) -> None:
    gate = app_dir / "scripts/km-vms-permission-gate.sh"
    for action, error_code, error_message in (
        (
            "--fix",
            "target_permission_fix_failed",
            "Target product permissions could not be normalized after the legacy overlay.",
        ),
        (
            "--check",
            "target_permission_check_failed",
            "Target product permissions failed the post-overlay verification.",
        ),
    ):
        result = run_command(
            ["sh", str(gate), action, "--app-dir", str(app_dir)],
            timeout=PERMISSION_GATE_TIMEOUT_SECONDS,
            error_code=error_code,
            error_message=error_message,
            check=False,
        )
        if result.returncode != 0:
            raise BridgeError(error_code, error_message)
        output_lines = set(result.stdout.splitlines())
        required_lines = {
            "permission_gate=PASS",
            f"permission_action={action.removeprefix('--')}",
            f"permission_app_dir={app_dir}",
            "permission_contract=target",
        }
        if not required_lines.issubset(output_lines):
            raise BridgeError(
                "target_permission_result_invalid",
                "Target product permission gate returned incomplete success evidence.",
            )


def schedule_refresh(
    *,
    app_dir: Path,
    project_name: str,
    helper_image: str,
    request_id: str,
    expected_image_id: str,
    timeout_seconds: int,
) -> str:
    coordinator_name = f"km-vms-helper-refresh-{request_id.removeprefix('update-').lower()}"
    existing_image_id = inspected_container_image(coordinator_name)
    if existing_image_id is not None:
        if existing_image_id != expected_image_id:
            raise BridgeError("coordinator_image_mismatch", "Existing helper refresh coordinator uses another image.")
        return "already_scheduled"

    script_path = app_dir / "scripts/km-vms-update-helper-bridge.py"
    command = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        coordinator_name,
        "-v",
        f"{app_dir}:{app_dir}",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-w",
        str(app_dir),
        "--entrypoint",
        "python3",
        expected_image_id,
        str(script_path),
        "refresh",
        "--app-dir",
        str(app_dir),
        "--project-name",
        project_name,
        "--helper-image",
        helper_image,
        "--request-id",
        request_id,
        "--expected-image-id",
        expected_image_id,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    result = run_command(
        command,
        timeout=60,
        error_code="coordinator_start_failed",
        error_message="Cannot start the detached update-helper refresh coordinator.",
        check=False,
    )
    if result.returncode == 0:
        return "scheduled"

    # A concurrent bootstrap may have won the atomic Docker container-name race.
    existing_image_id = inspected_container_image(coordinator_name)
    if existing_image_id == expected_image_id:
        return "already_scheduled"
    raise BridgeError("coordinator_start_failed", "Cannot start the detached update-helper refresh coordinator.")


def bootstrap(args: argparse.Namespace) -> int:
    app_dir = require_app_dir(args.app_dir or os.getenv("KM_VMS_BOOTSTRAP_APP_DIR", ""))
    project_name = require_project_name(
        args.project_name or os.getenv("KM_VMS_BOOTSTRAP_PROJECT_NAME", "")
    )
    helper_image = require_helper_image(
        args.helper_image or os.getenv("KM_VMS_BOOTSTRAP_HELPER_IMAGE", "")
    )
    explicit_request_binding = bool(args.require_request_id)
    env_request_id = os.getenv("KM_VMS_UPDATE_CONTROL_REQUEST_ID", "").strip()
    required_request_id = (
        require_request_id(args.require_request_id)
        if args.require_request_id
        else require_request_id(env_request_id)
        if env_request_id
        else None
    )
    if (
        args.require_request_id
        and env_request_id
        and require_request_id(env_request_id) != required_request_id
    ):
        raise BridgeError(
            "active_request_mismatch",
            "Explicit and helper-owned update request identities differ.",
        )
    raw_timeout = args.timeout_seconds
    if raw_timeout is None:
        try:
            raw_timeout = int(
                os.getenv("KM_VMS_BOOTSTRAP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
            )
        except ValueError as exc:
            raise BridgeError("timeout_invalid", "Refresh timeout must be an integer.") from exc
    timeout_seconds = require_timeout(raw_timeout)
    ensure_docker_runtime()

    control_dir = app_dir / "data/update-control"
    status_record = read_status(control_dir / "update-status.json")
    if status_record is None:
        if required_request_id:
            raise BridgeError("active_status_missing", "The expected active update status is missing.")
        print("update_helper_bootstrap=NO_ACTIVE_UPDATE")
        return 0

    request_id, status, _payload = status_record
    if status not in ACTIVE_STATUSES:
        if required_request_id:
            raise BridgeError("active_status_missing", "The expected update is not active.")
        print("update_helper_bootstrap=NO_ACTIVE_UPDATE")
        return 0
    if required_request_id and request_id != required_request_id:
        raise BridgeError("active_request_mismatch", "The active update does not match the required request.")
    if request_id is None:
        raise BridgeError("status_request_invalid", "Active update status has no canonical request identity.")

    run_target_permission_gate(app_dir)
    capture_installed_source_identity(
        app_dir,
        request_id=request_id,
    )
    archive_override_changed = normalize_archive_roots_override(app_dir)
    if archive_override_changed and not explicit_request_binding:
        raise BridgeError(
            "compose_reparse_required",
            "Generated archive-root mounts were normalized; Compose must reparse them before schema recovery.",
        )
    expected_image_id = docker_image_id(helper_image)
    receipt_file = control_dir / "update-helper-refresh.json"
    validate_receipt_binding(receipt_file, request_id, expected_image_id)

    result = schedule_refresh(
        app_dir=app_dir,
        project_name=project_name,
        helper_image=helper_image,
        request_id=request_id,
        expected_image_id=expected_image_id,
        timeout_seconds=timeout_seconds,
    )
    print("permission_gate=PASS")
    print("permission_contract=target")
    print("update_helper_bootstrap=PASS" if result == "scheduled" else "update_helper_bootstrap=ALREADY_SCHEDULED")
    print(f"update_helper_request_id={request_id}")
    print(f"update_helper_expected_image_id={expected_image_id}")
    return 0


def wait_for_terminal_status(
    status_file: Path,
    *,
    request_id: str,
    timeout_seconds: int,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = read_status(status_file)
        if record is None:
            time.sleep(poll_seconds)
            continue
        observed_request_id, status, payload = record
        if observed_request_id != request_id:
            raise BridgeError("status_request_mismatch", "Update status changed to another request during helper handoff.")
        if status == "completed":
            validate_completed_status(payload, request_id)
            return payload
        if status in TERMINAL_STATUSES:
            raise BridgeError("update_not_completed", "The current update reached a non-success terminal state.")
        if status in INACTIVE_STATUSES:
            raise BridgeError("update_status_inactive", "The current update became inactive before terminal completion.")
        time.sleep(poll_seconds)
    raise BridgeError("terminal_wait_timeout", "Timed out waiting for terminal update completion.")


def wait_for_helper_lease_release(control_dir: Path, timeout_seconds: int = 60) -> None:
    lease_file = control_dir / "update-helper-claim.lock"
    deadline = time.monotonic() + timeout_seconds
    try:
        with lease_file.open("a+", encoding="utf-8") as stream:
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(1)
                    continue
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return
    except OSError as exc:
        raise BridgeError("helper_lease_unavailable", "Cannot inspect the active update-helper lease.") from exc
    raise BridgeError("helper_lease_timeout", "The active update-helper did not release its execution lease.")


def compose_base(app_dir: Path, project_name: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(app_dir / ".env"),
        "-f",
        str(app_dir / "docker-compose.yml"),
        "-p",
        project_name,
    ]


def recreate_and_verify_helper(
    *,
    app_dir: Path,
    project_name: str,
    helper_image: str,
    expected_image_id: str,
) -> str:
    if docker_image_id(helper_image) != expected_image_id:
        raise BridgeError("prepared_image_changed", "The prepared update-helper image changed before activation.")
    compose = compose_base(app_dir, project_name)
    run_command(
        [*compose, "up", "-d", "--no-deps", "--force-recreate", "update-helper"],
        timeout=300,
        error_code="helper_recreate_failed",
        error_message="Docker Compose could not recreate update-helper.",
    )
    result = run_command(
        [*compose, "ps", "-q", "update-helper"],
        timeout=30,
        error_code="helper_identity_missing",
        error_message="Recreated update-helper container identity is unavailable.",
    )
    container_id = result.stdout.strip().lower()
    if not CONTAINER_ID_RE.fullmatch(container_id):
        raise BridgeError("helper_identity_missing", "Recreated update-helper container identity is unavailable.")

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        running = run_command(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
            timeout=20,
            error_code="helper_inspect_failed",
            error_message="Cannot inspect recreated update-helper.",
            check=False,
        )
        if running.returncode == 0 and running.stdout.strip() == "true":
            break
        time.sleep(1)
    else:
        raise BridgeError("helper_not_running", "Recreated update-helper did not reach running state.")

    if inspected_container_image(container_id) != expected_image_id:
        raise BridgeError("helper_image_mismatch", "Recreated update-helper is not using the prepared target image.")
    running = run_command(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
        timeout=20,
        error_code="helper_inspect_failed",
        error_message="Cannot inspect recreated update-helper.",
    )
    if running.stdout.strip() != "true":
        raise BridgeError("helper_not_running", "Recreated update-helper is not running after runtime verification.")
    return container_id


def refresh(args: argparse.Namespace) -> int:
    app_dir = require_app_dir(args.app_dir)
    project_name = require_project_name(args.project_name)
    helper_image = require_helper_image(args.helper_image)
    request_id = require_request_id(args.request_id)
    expected_image_id = require_image_id(args.expected_image_id)
    timeout_seconds = require_timeout(args.timeout_seconds)
    control_dir = app_dir / "data/update-control"
    receipt_file = control_dir / "update-helper-refresh.json"
    write_receipt(
        receipt_file,
        request_id=request_id,
        status="waiting_for_terminal",
        expected_image_id=expected_image_id,
        message="Waiting for exact terminal update completion.",
    )
    try:
        ensure_docker_runtime()
        wait_for_terminal_status(
            control_dir / "update-status.json",
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        wait_for_helper_lease_release(control_dir)
        write_receipt(
            receipt_file,
            request_id=request_id,
            status="recreating",
            expected_image_id=expected_image_id,
            message="Activating the prepared target update-helper image.",
        )
        container_id = recreate_and_verify_helper(
            app_dir=app_dir,
            project_name=project_name,
            helper_image=helper_image,
            expected_image_id=expected_image_id,
        )
        write_receipt(
            receipt_file,
            request_id=request_id,
            status="completed",
            expected_image_id=expected_image_id,
            helper_container_id=container_id,
            message="update-helper was recreated and its ACL runtime was verified.",
        )
    except BridgeError as exc:
        try:
            write_receipt(
                receipt_file,
                request_id=request_id,
                status="failed",
                expected_image_id=expected_image_id,
                message=exc.code,
            )
        except BridgeError:
            pass
        raise

    print("update_helper_refresh=PASS")
    print(f"update_helper_request_id={request_id}")
    print(f"update_helper_container_id={container_id}")
    print(f"update_helper_image_id={expected_image_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KM VMS update-helper legacy transition bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap", help="schedule a refresh for an active update")
    bootstrap_parser.add_argument("--app-dir")
    bootstrap_parser.add_argument("--project-name")
    bootstrap_parser.add_argument("--helper-image")
    bootstrap_parser.add_argument("--require-request-id")
    bootstrap_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
    )
    bootstrap_parser.set_defaults(handler=bootstrap)

    refresh_parser = subparsers.add_parser("refresh", help="wait for completion and recreate update-helper")
    refresh_parser.add_argument("--app-dir", required=True)
    refresh_parser.add_argument("--project-name", required=True)
    refresh_parser.add_argument("--helper-image", required=True)
    refresh_parser.add_argument("--request-id", required=True)
    refresh_parser.add_argument("--expected-image-id", required=True)
    refresh_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    refresh_parser.set_defaults(handler=refresh)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.handler(args))
    except BridgeError as exc:
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
