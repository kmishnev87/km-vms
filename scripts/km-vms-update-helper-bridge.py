#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


MAX_CONTROL_BYTES = 64 * 1024
MAX_LEGACY_ADMISSION_BYTES = 512 * 1024
MAX_LEGACY_ADMISSION_ENTRIES = 256
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
SLOT_ID_RE = re.compile(
    r"^(?:release-[0-9a-f]{40}|adopted-[0-9a-f]{64})$",
    re.IGNORECASE,
)
ARCHIVE_RUNTIME_TARGET_RE = re.compile(
    r"^/storage/archive-roots/[A-Za-z0-9_.-]{1,180}$"
)
SUBMISSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

LEGACY_REQUIRED_IMAGE_SERVICES = (
    "api",
    "recorder",
    "web",
    "nginx",
    "update-helper",
)
OPTIONAL_PERSISTENT_IMAGE_SERVICES = (
    "update-status-reader",
    "update-retry-admission",
)
TARGET_BUILD_SERVICES = (
    "schema-update",
    "api",
    "recorder",
    "web",
    "update-helper",
    "update-status-reader",
)
TARGET_EVIDENCE_SERVICES = (
    *LEGACY_REQUIRED_IMAGE_SERVICES,
    *OPTIONAL_PERSISTENT_IMAGE_SERVICES,
    "schema-update",
)
ACTIVATION_RUNTIME_SERVICES = (
    "update-status-reader",
    "update-retry-admission",
    "api",
    "recorder",
    "web",
    "nginx",
    "setup-helper",
)
CORE_RUNTIME_SERVICES = ("api", "recorder", "web", "nginx")
REQUEST_SCOPED_COMPOSE_EVIDENCE_FIELDS = frozenset(
    {
        "captured_plan_sha256",
        "slot_plan_sha256",
    }
)
REQUEST_ID_COMPOSE_TOKEN = "${KM_VMS_UPDATE_CONTROL_REQUEST_ID}"

LEGACY_HISTORICAL_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "requested_at",
    "requested_by",
    "intent",
    "source",
    "confirmed",
    "preflight_required",
    "status_path",
}
LEGACY_SNAPSHOT_REQUEST_FIELDS = LEGACY_HISTORICAL_REQUEST_FIELDS | {
    "apply_candidate",
}
SCHEMA_RETRY_REQUEST_FIELDS = LEGACY_HISTORICAL_REQUEST_FIELDS | {
    "retry_of_request_id",
    "migration_attempt_id",
}
CURRENT_SINGLE_REQUEST_FIELDS = {
    "schema_version",
    "document_type",
    "request_id",
    "submission_id",
    "requested_at",
    "updated_at",
    "requested_by",
    "intent",
    "source",
    "apply_candidate",
    "confirmed",
    "preflight_required",
    "status_path",
    "state",
    "claimed_at",
    "terminal",
    "audit_event_id",
}
NORMALIZED_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "submission_id",
    "requested_at",
    "requested_by",
    "intent",
    "source",
    "apply_candidate",
    "confirmed",
    "preflight_required",
    "status_path",
}

UPDATE_LINEAGE_FILENAME = "km-vms-update-lineage.json"
UPDATE_LINEAGE_MAX_BYTES = 128 * 1024


def load_update_lineage() -> dict[str, Any]:
    configured = str(os.getenv("KMVMS_UPDATE_LINEAGE_FILE") or "").strip()
    candidates = (
        [Path(configured)]
        if configured
        else [
            parent / "release" / UPDATE_LINEAGE_FILENAME
            for parent in (*Path(__file__).resolve().parents, Path.cwd())
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
    "preparing",
    "staging",
    "activating",
    "reconnecting",
    "rolling_back",
}
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "failed_rolled_back",
    "cancelled",
    "blocked",
}
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


def read_json_object(
    path: Path,
    *,
    missing_ok: bool = False,
    max_bytes: int = MAX_CONTROL_BYTES,
) -> dict[str, Any] | None:
    try:
        if path.is_symlink():
            raise BridgeError("control_file_invalid", "Update control data is not a regular file.")
        if not path.exists():
            if missing_ok:
                return None
            raise BridgeError("control_file_missing", "Required update control data is missing.")
        if not path.is_file() or path.is_symlink():
            raise BridgeError("control_file_invalid", "Update control data is not a regular file.")
        if path.stat().st_size > max_bytes:
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
    schema_version = authority.get("schema_version")
    if type(schema_version) is int and schema_version == 1:
        if frozenset(authority) not in {
            frozenset(LEGACY_HISTORICAL_REQUEST_FIELDS),
            frozenset(LEGACY_SNAPSHOT_REQUEST_FIELDS),
            frozenset(SCHEMA_RETRY_REQUEST_FIELDS),
        }:
            raise BridgeError(
                "source_handoff_authority_invalid",
                "Legacy update request has an unsupported published shape.",
            )
        if authority.get("request_id") != request_id:
            raise BridgeError(
                "source_handoff_request_mismatch",
                "Legacy update request does not match the active helper request.",
            )
        return authority
    if type(schema_version) is int and schema_version == 3:
        source = authority.get("source")
        if (
            set(authority) != CURRENT_SINGLE_REQUEST_FIELDS
            or authority.get("document_type") != "update_apply_request"
            or authority.get("request_id") != request_id
            or authority.get("state") != "claimed"
            or authority.get("terminal") is not None
            or type(authority.get("submission_id")) is not str
            or not SUBMISSION_ID_RE.fullmatch(authority["submission_id"])
            or type(authority.get("claimed_at")) is not str
            or not authority["claimed_at"]
            or type(source) is not dict
            or type(source.get("commit")) is not str
            or not COMMIT_SHA_RE.fullmatch(source["commit"])
        ):
            raise BridgeError(
                "source_handoff_authority_invalid",
                "Current update admission is not an exact claimed request.",
            )
        return {
            key: authority[key]
            for key in NORMALIZED_REQUEST_FIELDS
        } | {"schema_version": 2}
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
        or len(authority["entries"]) > MAX_LEGACY_ADMISSION_ENTRIES
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
        or set(request) != NORMALIZED_REQUEST_FIELDS
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
    target_source_dir: Path | None = None,
    installed_source_dir: Path | None = None,
    request_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    control_dir = app_dir / "data/update-control"
    installed_root = installed_source_dir or app_dir
    source_identity = read_json_object(
        installed_root / ".km-vms-source.json"
    )
    release_root = target_source_dir or app_dir
    release_identity = read_json_object(
        release_root / "release/km-vms-release.json"
    )
    assert source_identity is not None
    assert release_identity is not None
    if request_override is None:
        authority = read_json_object(
            control_dir / "update-request.json",
            max_bytes=MAX_LEGACY_ADMISSION_BYTES,
        )
        assert authority is not None
        request = extract_active_request(
            authority,
            request_id=request_id,
        )
    else:
        source_override = request_override.get("source")
        if (
            set(request_override)
            != {
                "schema_version",
                "request_id",
                "requested_at",
                "intent",
                "confirmed",
                "source",
            }
            or request_override.get("schema_version") != 1
            or request_override.get("request_id") != request_id
            or request_override.get("intent") != "apply_update"
            or request_override.get("confirmed") is not True
            or type(request_override.get("requested_at")) is not str
            or not request_override["requested_at"]
            or type(source_override) is not dict
            or set(source_override) != {"version", "commit"}
            or type(source_override.get("version")) is not str
            or not source_override["version"]
            or type(source_override.get("commit")) is not str
            or not COMMIT_SHA_RE.fullmatch(source_override["commit"])
        ):
            raise BridgeError(
                "terminal_request_invalid",
                "Terminal update activation request is invalid.",
            )
        request = json.loads(json.dumps(request_override))
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
        and request_override is None
        else ""
    )
    if request_override is not None:
        target_repo = "kmishnev87/km-vms"
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
    return identity


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise BridgeError(
            "slot_evidence_unavailable",
            "Pre-update release-slot evidence could not be read.",
        ) from exc
    return digest.hexdigest()


def load_slot_engine(source_dir: Path):
    module_path = source_dir / "scripts/km-vms-release-slots.py"
    if module_path.is_symlink() or not module_path.is_file():
        raise BridgeError(
            "slot_engine_missing",
            "Release-slot activation engine is unavailable.",
        )
    spec = importlib.util.spec_from_file_location(
        "km_vms_release_slots_runtime",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise BridgeError(
            "slot_engine_missing",
            "Release-slot activation engine could not be loaded.",
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise BridgeError(
            "slot_engine_invalid",
            "Release-slot activation engine is invalid.",
        ) from exc
    return module


def _parse_command_json(
    result: subprocess.CompletedProcess[str],
    *,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise BridgeError(error_code, error_message) from exc
    if type(payload) is not dict:
        raise BridgeError(error_code, error_message)
    return payload


def _compose_container_id(
    compose: Sequence[str],
    service: str,
    *,
    env: dict[str, str] | None = None,
) -> str:
    result = run_command(
        [*compose, "ps", "-q", service],
        timeout=30,
        error_code="slot_image_evidence_missing",
        error_message="A required pre-update service has no exact running container.",
        env=env,
    )
    container_id = result.stdout.strip().lower()
    if not CONTAINER_ID_RE.fullmatch(container_id):
        raise BridgeError(
            "slot_image_evidence_missing",
            "A required pre-update service has no exact running container.",
        )
    return container_id


def _compose_services(
    compose: Sequence[str],
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    result = run_command(
        [*compose, "config", "--services"],
        timeout=60,
        error_code="slot_compose_evidence_failed",
        error_message="Compose service inventory could not be captured.",
        env=env,
    )
    services = sorted(
        {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        }
    )
    if (
        not services
        or len(services) > 64
        or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", item) for item in services)
    ):
        raise BridgeError(
            "slot_compose_evidence_failed",
            "Compose service inventory is invalid.",
        )
    return services


def _normalized_compose_digest(
    rendered: str,
    *,
    app_dir: Path,
    source_dir: Path,
) -> str:
    normalized = rendered.replace("\r\n", "\n")
    source_text = str(source_dir.resolve())
    app_text = str(app_dir.resolve())
    if source_text == app_text:
        replacements = [
            (source_text, "${KM_VMS_STABLE_LEGACY_SOURCE}"),
        ]
    else:
        replacements = sorted(
            [
                (source_text, "${KM_VMS_PRODUCT_SOURCE}"),
                (app_text, "${KM_VMS_STABLE_APP_DIR}"),
            ],
            key=lambda item: len(item[0]),
            reverse=True,
        )
    for observed, token in replacements:
        normalized = normalized.replace(observed, token)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_current_compose_contract(
    value: Any,
    *,
    app_dir: Path,
    source_dir: Path,
    request_id: str,
) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_current_compose_contract(
                item,
                app_dir=app_dir,
                source_dir=source_dir,
                request_id=request_id,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _normalize_current_compose_contract(
                item,
                app_dir=app_dir,
                source_dir=source_dir,
                request_id=request_id,
            )
            for item in value
        ]
    if not isinstance(value, str):
        return value
    normalized = value
    source_text = str(source_dir.resolve())
    app_text = str(app_dir.resolve())
    if source_text != app_text:
        normalized = normalized.replace(source_text, app_text)
    return normalized.replace(request_id, REQUEST_ID_COMPOSE_TOKEN)


def _current_compose_security_contract(
    compose: Sequence[str],
    *,
    app_dir: Path,
    source_dir: Path,
    request_id: str,
) -> dict[str, Any]:
    rendered = run_command(
        [*compose, "config", "--format", "json"],
        timeout=60,
        error_code="slot_compose_evidence_failed",
        error_message="Current Compose security contract could not be captured.",
    )
    try:
        payload = json.loads(rendered.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError(
            "slot_compose_evidence_failed",
            "Current Compose security contract is invalid.",
        ) from exc
    if type(payload) is not dict or type(payload.get("services")) is not dict:
        raise BridgeError(
            "slot_compose_evidence_failed",
            "Current Compose security contract is invalid.",
        )
    return _normalize_current_compose_contract(
        payload,
        app_dir=app_dir,
        source_dir=source_dir,
        request_id=request_id,
    )


def _historical_compose_evidence_matches(
    historical: Any,
    current: dict[str, Any],
) -> bool:
    if type(historical) is not dict or set(historical) != set(current):
        return False
    if any(
        type(historical.get(key)) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", historical[key])
        or type(current.get(key)) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", current[key])
        for key in REQUEST_SCOPED_COMPOSE_EVIDENCE_FIELDS
    ):
        return False
    return all(
        historical[key] == value
        for key, value in current.items()
        if key not in REQUEST_SCOPED_COMPOSE_EVIDENCE_FIELDS
    )


def _reused_adopted_evidence_matches(
    manifest: dict[str, Any],
    *,
    compose_evidence: dict[str, Any],
    image_evidence: dict[str, Any],
    health_evidence: dict[str, Any],
    installed_identity: dict[str, Any],
) -> bool:
    return (
        _historical_compose_evidence_matches(
            manifest.get("compose_evidence"),
            compose_evidence,
        )
        and manifest.get("image_evidence") == image_evidence
        and manifest.get("pre_update_health") == health_evidence
        and manifest.get("declared_identity")
        == {
            "version": str(installed_identity["installed_version"]),
            "commit": str(installed_identity["installed_commit"]),
        }
    )


def _archive_override_evidence(app_dir: Path) -> tuple[bool, str | None]:
    archive_override = (
        app_dir / "data/install-control/docker-compose.archive-roots.yml"
    )
    if archive_override.is_symlink():
        raise BridgeError(
            "archive_roots_override_invalid",
            "Generated archive-roots Compose override is unsafe.",
        )
    attached = archive_override.is_file()
    return attached, _sha256_file(archive_override) if attached else None


def capture_pre_update_slot_evidence(
    app_dir: Path,
    *,
    project_name: str,
    request_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    ensure_docker_runtime()
    compose = compose_base(
        app_dir,
        project_name,
        source_dir=app_dir,
        include_archive_override=True,
    )
    config = run_command(
        [*compose, "config"],
        timeout=60,
        error_code="slot_compose_evidence_failed",
        error_message="Current Compose plan could not be captured before target build.",
    )
    services = _compose_services(compose)
    if not set(LEGACY_REQUIRED_IMAGE_SERVICES).issubset(services):
        raise BridgeError(
            "slot_compose_evidence_failed",
            "Current Compose plan lacks a required rollback service.",
        )
    archive_attached, archive_digest = _archive_override_evidence(app_dir)
    current_plan_digest = _normalized_compose_digest(
        config.stdout,
        app_dir=app_dir,
        source_dir=app_dir,
    )
    current_security_contract = _current_compose_security_contract(
        compose,
        app_dir=app_dir,
        source_dir=app_dir,
        request_id=request_id,
    )
    compose_evidence = {
        "schema_version": 1,
        "project_name": project_name,
        "project_directory": "source",
        "captured_plan_sha256": current_plan_digest,
        "slot_plan_sha256": current_plan_digest,
        "archive_override_attached": archive_attached,
        "archive_override_sha256": archive_digest,
        "runtime_override_sha256": None,
        "shared_root_contract": "stable_app_dir_v1",
        "services": services,
    }

    image_services: dict[str, Any] = {}
    evidence_services = [
        *LEGACY_REQUIRED_IMAGE_SERVICES,
        *(
            service
            for service in OPTIONAL_PERSISTENT_IMAGE_SERVICES
            if service in services
        ),
    ]
    for service in evidence_services:
        container_id = _compose_container_id(compose, service)
        inspected = run_command(
            ["docker", "inspect", container_id],
            timeout=30,
            error_code="slot_image_evidence_failed",
            error_message="A required pre-update service image could not be inspected.",
        )
        try:
            rows = json.loads(inspected.stdout)
            row = rows[0]
            image_id = str(row["Image"]).lower()
            image_ref = str(row["Config"]["Image"])
            state = row["State"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise BridgeError(
                "slot_image_evidence_failed",
                "A required pre-update service image returned invalid evidence.",
            ) from exc
        if not IMAGE_ID_RE.fullmatch(image_id) or not image_ref:
            raise BridgeError(
                "slot_image_evidence_failed",
                "A required pre-update service image is not exact.",
            )
        if state.get("Running") is not True:
            raise BridgeError(
                "slot_current_runtime_unhealthy",
                "Current core services must be running before legacy adoption.",
            )
        if service == "api":
            health = state.get("Health")
            if type(health) is not dict or health.get("Status") != "healthy":
                raise BridgeError(
                    "slot_current_runtime_unhealthy",
                    "Current API must be healthy before legacy adoption.",
                )
        image_services[service] = {
            "image_id": image_id,
            "source_image_ref": image_ref,
        }

    identity_digest = run_command(
        [
            *compose,
            "exec",
            "-T",
            "api",
            "python3",
            "-c",
            (
                "import hashlib;"
                "from pathlib import Path;"
                "print(hashlib.sha256("
                "Path('/app/.km-vms-release.json').read_bytes()"
                ").hexdigest())"
            ),
        ],
        timeout=30,
        error_code="slot_api_identity_unavailable",
        error_message="API-visible pre-update release identity could not be captured.",
    ).stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", identity_digest):
        raise BridgeError(
            "slot_api_identity_unavailable",
            "API-visible pre-update release identity is invalid.",
        )
    host_identity = app_dir / ".km-vms-release.json"
    if (
        host_identity.is_symlink()
        or not host_identity.is_file()
        or _sha256_file(host_identity) != identity_digest
    ):
        raise BridgeError(
            "slot_api_identity_mismatch",
            "Host and API-visible pre-update release identities differ.",
        )
    image_evidence = {
        "schema_version": 1,
        "services": image_services,
    }
    health_evidence = {
        "schema_version": 1,
        "status": "healthy",
        "api_visible_identity_sha256": identity_digest,
        "core_services": ["api", "nginx", "recorder", "web"],
    }
    return (
        compose_evidence,
        image_evidence,
        health_evidence,
        current_security_contract,
    )


def preserve_slot_images(
    image_evidence: dict[str, Any],
    *,
    project_name: str,
    slot_id: str,
) -> dict[str, Any]:
    services = image_evidence.get("services")
    if (
        image_evidence.get("schema_version") != 1
        or type(services) is not dict
        or not services
        or not re.fullmatch(
            r"(?:release-[0-9a-f]{40}|adopted-[0-9a-f]{64})",
            slot_id,
        )
    ):
        raise BridgeError(
            "slot_image_evidence_failed",
            "Immutable image alias input is invalid.",
        )
    result: dict[str, Any] = {"schema_version": 1, "services": {}}
    for service, item in sorted(services.items()):
        image_id = str(item.get("image_id") or "").lower()
        source_ref = str(item.get("source_image_ref") or "")
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", str(service))
            or not IMAGE_ID_RE.fullmatch(image_id)
            or not source_ref
        ):
            raise BridgeError(
                "slot_image_evidence_failed",
                "Immutable image alias input is invalid.",
            )
        immutable_ref = (
            f"km-vms-{project_name}-slot-{service}:{slot_id}"
        )
        existing = run_command(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                immutable_ref,
            ],
            timeout=30,
            error_code="slot_image_alias_failed",
            error_message="Immutable release-slot image alias could not be inspected.",
            check=False,
        )
        if existing.returncode == 0:
            if existing.stdout.strip().lower() != image_id:
                raise BridgeError(
                    "slot_image_alias_conflict",
                    "An immutable release-slot image alias already points elsewhere.",
                )
        else:
            run_command(
                ["docker", "image", "tag", image_id, immutable_ref],
                timeout=30,
                error_code="slot_image_alias_failed",
                error_message="Exact pre-update image could not be preserved.",
            )
        verified = run_command(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                immutable_ref,
            ],
            timeout=30,
            error_code="slot_image_alias_failed",
            error_message="Immutable release-slot image alias could not be verified.",
        ).stdout.strip().lower()
        if verified != image_id:
            raise BridgeError(
                "slot_image_alias_failed",
                "Immutable release-slot image alias changed during verification.",
            )
        result["services"][service] = {
            "image_id": image_id,
            "source_image_ref": source_ref,
            "immutable_image_ref": immutable_ref,
        }
    return result


def prepare_legacy_adopted_slot(
    *,
    app_dir: Path,
    target_source_dir: Path,
    request_id: str,
    installed_identity: dict[str, Any],
) -> str:
    slot_tool = target_source_dir / "scripts/km-vms-release-slots.py"
    if slot_tool.is_symlink() or not slot_tool.is_file():
        raise BridgeError(
            "slot_engine_missing",
            "Trusted target release has no release-slot engine.",
        )
    project_name = require_project_name(
        os.getenv("KM_VMS_PROJECT_NAME", "").strip()
    )
    (
        compose_evidence,
        captured_images,
        health_evidence,
        current_security_contract,
    ) = (
        capture_pre_update_slot_evidence(
            app_dir,
            project_name=project_name,
            request_id=request_id,
        )
    )
    inspect_result = run_command(
        [
            "python3",
            str(slot_tool),
            "inspect",
            "--app-dir",
            str(app_dir),
        ],
        timeout=30,
        error_code="slot_layout_prepare_failed",
        error_message="Stable release-slot layout could not be prepared.",
    )
    inspect_payload = _parse_command_json(
        inspect_result,
        error_code="slot_layout_prepare_failed",
        error_message="Stable release-slot layout returned invalid evidence.",
    )
    if inspect_payload.get("activation_cli_enabled") is not True:
        raise BridgeError(
            "slot_activation_unavailable",
            "Trusted target release does not provide the Stage C activation engine.",
        )

    evidence_root = app_dir / "data/update-runtime/staging"
    with tempfile.TemporaryDirectory(
        prefix=".legacy-evidence-",
        dir=evidence_root,
    ) as temporary:
        temporary_root = Path(temporary)
        services_path = temporary_root / "services.json"
        compose_path = temporary_root / "compose.json"
        image_path = temporary_root / "images.json"
        health_path = temporary_root / "health.json"
        atomic_write_json(
            services_path,
            {"services": compose_evidence["services"]},
        )
        stage_result = run_command(
            [
                "python3",
                str(slot_tool),
                "stage-adopted",
                "--app-dir",
                str(app_dir),
                "--request-id",
                request_id,
                "--declared-version",
                str(installed_identity["installed_version"]),
                "--declared-commit",
                str(installed_identity["installed_commit"]),
            ],
            timeout=300,
            error_code="slot_adoption_snapshot_failed",
            error_message="Exact pre-update source snapshot could not be materialized.",
        )
        stage_payload = _parse_command_json(
            stage_result,
            error_code="slot_adoption_snapshot_failed",
            error_message="Pre-update source snapshot returned invalid evidence.",
        )
        slot_id = str(stage_payload.get("slot_id") or "")
        if not re.fullmatch(r"adopted-[0-9a-f]{64}", slot_id):
            raise BridgeError(
                "slot_adoption_snapshot_failed",
                "Pre-update source snapshot has an invalid adopted slot identity.",
            )
        source_path = Path(str(stage_payload.get("source_path") or ""))
        if not source_path.is_absolute() or not source_path.is_dir():
            raise BridgeError(
                "slot_adoption_snapshot_failed",
                "Pre-update source snapshot path is invalid.",
            )

        if stage_payload.get("status") == "staged":
            runtime_result = run_command(
                [
                    "python3",
                    str(slot_tool),
                    "prepare-adopted-runtime",
                    "--app-dir",
                    str(app_dir),
                    "--request-id",
                    request_id,
                    "--services-file",
                    str(services_path),
                ],
                timeout=30,
                error_code="slot_runtime_override_failed",
                error_message="Stable-root runtime override could not be prepared.",
            )
            runtime_payload = _parse_command_json(
                runtime_result,
                error_code="slot_runtime_override_failed",
                error_message="Stable-root runtime override returned invalid evidence.",
            )
            runtime_override = Path(
                str(runtime_payload.get("override_path") or "")
            )
            runtime_digest = str(runtime_payload.get("sha256") or "")
        elif stage_payload.get("status") == "reused":
            manifest = stage_payload.get("manifest")
            if type(manifest) is not dict:
                raise BridgeError(
                    "slot_adoption_snapshot_failed",
                    "Reused adopted slot returned no immutable manifest.",
                )
            runtime_override = (
                app_dir
                / "data/update-runtime/slots"
                / slot_id
                / "docker-compose.runtime-override.yml"
            )
            runtime_digest = str(
                manifest.get("compose_evidence", {}).get(
                    "runtime_override_sha256"
                )
                or ""
            )
        else:
            raise BridgeError(
                "slot_adoption_snapshot_failed",
                "Pre-update source snapshot did not reach a reusable state.",
            )
        if (
            not runtime_override.is_absolute()
            or runtime_override.is_symlink()
            or not runtime_override.is_file()
            or _sha256_file(runtime_override) != runtime_digest
        ):
            raise BridgeError(
                "slot_runtime_override_failed",
                "Stable-root runtime override evidence is contradictory.",
            )

        slot_compose = compose_base(
            app_dir,
            project_name,
            source_dir=source_path,
            runtime_override=runtime_override,
            include_archive_override=True,
        )
        slot_config = run_command(
            [*slot_compose, "config"],
            timeout=60,
            error_code="slot_compose_evidence_failed",
            error_message="Adopted slot Compose plan could not be validated.",
        )
        if _compose_services(slot_compose) != compose_evidence["services"]:
            raise BridgeError(
                "slot_compose_evidence_failed",
                "Adopted slot changed the current Compose service set.",
            )
        slot_security_contract = _current_compose_security_contract(
            slot_compose,
            app_dir=app_dir,
            source_dir=source_path,
            request_id=request_id,
        )
        if slot_security_contract != current_security_contract:
            raise BridgeError(
                "slot_adoption_conflict",
                "Existing adopted slot no longer matches the running legacy source.",
            )
        compose_evidence["slot_plan_sha256"] = _normalized_compose_digest(
            slot_config.stdout,
            app_dir=app_dir,
            source_dir=source_path,
        )
        compose_evidence["runtime_override_sha256"] = runtime_digest
        image_evidence = preserve_slot_images(
            captured_images,
            project_name=project_name,
            slot_id=slot_id,
        )

        if stage_payload.get("status") == "reused":
            if not _reused_adopted_evidence_matches(
                manifest,
                compose_evidence=compose_evidence,
                image_evidence=image_evidence,
                health_evidence=health_evidence,
                installed_identity=installed_identity,
            ):
                raise BridgeError(
                    "slot_adoption_conflict",
                    "Existing adopted slot no longer matches the running legacy source.",
                )
            return slot_id

        atomic_write_json(compose_path, compose_evidence)
        atomic_write_json(image_path, image_evidence)
        atomic_write_json(health_path, health_evidence)
        finalize_result = run_command(
            [
                "python3",
                str(slot_tool),
                "finalize",
                "--app-dir",
                str(app_dir),
                "--request-id",
                request_id,
                "--compose-evidence-file",
                str(compose_path),
                "--image-evidence-file",
                str(image_path),
                "--health-evidence-file",
                str(health_path),
            ],
            timeout=300,
            error_code="slot_adoption_finalize_failed",
            error_message="Exact pre-update release slot could not be finalized.",
        )
        finalized = _parse_command_json(
            finalize_result,
            error_code="slot_adoption_finalize_failed",
            error_message="Final pre-update release slot returned invalid evidence.",
        )
    if (
        finalized.get("slot_id") != slot_id
        or finalized.get("status") not in {"published", "reused"}
    ):
        raise BridgeError(
            "slot_adoption_finalize_failed",
            "Final pre-update release slot evidence is contradictory.",
        )
    return slot_id


def _compose_image_refs(
    compose: Sequence[str],
    *,
    env: dict[str, str],
) -> dict[str, str]:
    result = run_command(
        [*compose, "config", "--format", "json"],
        timeout=60,
        error_code="slot_compose_evidence_failed",
        error_message="Target Compose image plan could not be resolved.",
        env=env,
    )
    try:
        payload = json.loads(result.stdout)
        services = payload["services"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BridgeError(
            "slot_compose_evidence_failed",
            "Target Compose image plan is invalid.",
        ) from exc
    if type(services) is not dict:
        raise BridgeError(
            "slot_compose_evidence_failed",
            "Target Compose image plan is invalid.",
        )
    refs: dict[str, str] = {}
    for service, item in services.items():
        if (
            type(service) is not str
            or type(item) is not dict
            or type(item.get("image")) is not str
            or not item["image"]
        ):
            raise BridgeError(
                "slot_compose_evidence_failed",
                "Target Compose image plan is incomplete.",
            )
        refs[service] = item["image"]
    return refs


def _image_id_for_ref(image_ref: str) -> str:
    result = run_command(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image_ref,
        ],
        timeout=30,
        error_code="slot_image_evidence_missing",
        error_message="A prepared target image is unavailable.",
    )
    image_id = result.stdout.strip().lower()
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise BridgeError(
            "slot_image_evidence_failed",
            "A prepared target image has invalid identity evidence.",
        )
    return image_id


def verify_immutable_images(image_evidence: dict[str, Any]) -> None:
    services = image_evidence.get("services")
    if type(services) is not dict or not services:
        raise BridgeError(
            "slot_image_evidence_failed",
            "Immutable image evidence is unavailable.",
        )
    for item in services.values():
        if type(item) is not dict:
            raise BridgeError(
                "slot_image_evidence_failed",
                "Immutable image evidence is invalid.",
            )
        expected = str(item.get("image_id") or "").lower()
        immutable_ref = str(item.get("immutable_image_ref") or "")
        if (
            not IMAGE_ID_RE.fullmatch(expected)
            or not immutable_ref
            or _image_id_for_ref(immutable_ref) != expected
        ):
            raise BridgeError(
                "slot_image_evidence_missing",
                "An immutable release-slot image is no longer available.",
            )


def bridge_source_root() -> Path:
    root = Path(__file__).resolve().parent.parent
    if not (root / "docker-compose.yml").is_file():
        raise BridgeError(
            "bridge_source_invalid",
            "Update bridge product source is unavailable.",
        )
    return root


def slot_record(
    app_dir: Path,
    slot_id: str,
    *,
    engine: Any,
) -> tuple[Path, Path, dict[str, Any]]:
    slot_root = app_dir / "data/update-runtime/slots" / slot_id
    try:
        manifest = engine.validate_slot(
            slot_root,
            expected_slot_id=slot_id,
        )
    except Exception as exc:
        raise BridgeError(
            getattr(exc, "code", "slot_manifest_invalid"),
            "Immutable release slot is unavailable or invalid.",
        ) from exc
    source_dir = slot_root / "source"
    return slot_root, source_dir, manifest


def slot_environment(project_name: str, slot_id: str) -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = project_name
    env["KM_VMS_RELEASE_IMAGE_TAG"] = slot_id
    return env


def write_slot_image_override(
    destination: Path,
    manifest: dict[str, Any],
    *,
    service_images: dict[str, str] | None = None,
) -> Path:
    services = manifest.get("image_evidence", {}).get("services")
    if type(services) is not dict or not services:
        raise BridgeError(
            "slot_image_evidence_failed",
            "Immutable release-slot image evidence is unavailable.",
        )
    selected = {
        service: str(item.get("immutable_image_ref") or "")
        for service, item in services.items()
        if type(item) is dict
    }
    if service_images:
        selected.update(service_images)
    compose_services = set(
        manifest.get("compose_evidence", {}).get("services") or []
    )
    selected = {
        service: image
        for service, image in selected.items()
        if service in compose_services
    }
    if (
        not selected
        or any(
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", service)
            or not image
            or any(char in image for char in "\r\n\x00")
            for service, image in selected.items()
        )
    ):
        raise BridgeError(
            "slot_image_evidence_failed",
            "Immutable release-slot image override is invalid.",
        )
    lines = [
        "# Generated transiently from immutable slot evidence.",
        "services:",
    ]
    for service, image in sorted(selected.items()):
        lines.extend(
            (
                f"  {service}:",
                f"    image: {json.dumps(image, ensure_ascii=True)}",
            )
        )
    atomic_write_text(destination, "\n".join(lines) + "\n")
    return destination


def slot_compose(
    app_dir: Path,
    project_name: str,
    slot_id: str,
    *,
    engine: Any,
    override_root: Path,
    with_image_override: bool,
) -> tuple[list[str], dict[str, str], Path, dict[str, Any]]:
    _slot_root, source_dir, manifest = slot_record(
        app_dir,
        slot_id,
        engine=engine,
    )
    image_override = None
    if with_image_override:
        verify_immutable_images(manifest["image_evidence"])
        image_override = write_slot_image_override(
            override_root / f"{slot_id}-images.yml",
            manifest,
        )
    env = slot_environment(project_name, slot_id)
    compose = compose_base(
        app_dir,
        project_name,
        source_dir=source_dir,
        image_override=image_override,
        include_archive_override=True,
    )
    return compose, env, source_dir, manifest


def _inspect_running_service(
    compose: Sequence[str],
    service: str,
    *,
    env: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    container_id = _compose_container_id(
        compose,
        service,
        env=env,
    )
    inspected = run_command(
        ["docker", "inspect", container_id],
        timeout=30,
        error_code="slot_runtime_evidence_failed",
        error_message="Release-slot runtime evidence is unavailable.",
    )
    try:
        row = json.loads(inspected.stdout)[0]
        image_id = str(row["Image"]).lower()
        state = row["State"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise BridgeError(
            "slot_runtime_evidence_failed",
            "Release-slot runtime evidence is invalid.",
        ) from exc
    if (
        not IMAGE_ID_RE.fullmatch(image_id)
        or type(state) is not dict
        or state.get("Running") is not True
    ):
        raise BridgeError(
            "slot_runtime_unhealthy",
            "A core release-slot service is not running.",
        )
    if service == "api":
        health = state.get("Health")
        if type(health) is not dict or health.get("Status") != "healthy":
            raise BridgeError(
                "slot_runtime_unhealthy",
                "Release-slot API did not become healthy.",
            )
    return image_id, state


def _api_visible_identity_digest(
    compose: Sequence[str],
    *,
    env: dict[str, str],
) -> str:
    result = run_command(
        [
            *compose,
            "exec",
            "-T",
            "api",
            "python3",
            "-c",
            (
                "import hashlib;"
                "from pathlib import Path;"
                "print(hashlib.sha256("
                "Path('/app/.km-vms-release.json').read_bytes()"
                ").hexdigest())"
            ),
        ],
        timeout=30,
        error_code="slot_api_identity_unavailable",
        error_message="API-visible release identity is unavailable.",
        env=env,
    )
    digest = result.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise BridgeError(
            "slot_api_identity_unavailable",
            "API-visible release identity is invalid.",
        )
    return digest


def _api_visible_release_identity(
    compose: Sequence[str],
    *,
    env: dict[str, str],
) -> dict[str, Any]:
    result = run_command(
        [
            *compose,
            "exec",
            "-T",
            "api",
            "python3",
            "-c",
            (
                "import json;"
                "from app.services.update_check import "
                "read_installed_update_state;"
                "s=read_installed_update_state();"
                "print(json.dumps({"
                "'version':s.installed_version,"
                "'commit':s.installed_commit,"
                "'metadata_status':s.release_metadata_status,"
                "'identity_validity':s.identity_validity"
                "},sort_keys=True,separators=(',',':')))"
            ),
        ],
        timeout=30,
        error_code="slot_api_identity_unavailable",
        error_message="API-visible release identity is unavailable.",
        env=env,
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BridgeError(
            "slot_api_identity_unavailable",
            "API-visible release identity is invalid.",
        ) from exc
    if (
        type(payload) is not dict
        or set(payload)
        != {
            "version",
            "commit",
            "metadata_status",
            "identity_validity",
        }
    ):
        raise BridgeError(
            "slot_api_identity_unavailable",
            "API-visible release identity is invalid.",
        )
    return payload


def capture_slot_runtime_binding(
    app_dir: Path,
    project_name: str,
    slot_id: str,
    *,
    engine: Any,
    require_http: bool,
    require_helper_image: bool = False,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix="km-vms-slot-capture-",
    ) as temporary:
        temporary_root = Path(temporary)
        compose, env, source_dir, manifest = slot_compose(
            app_dir,
            project_name,
            slot_id,
            engine=engine,
            override_root=temporary_root,
            with_image_override=False,
        )
        config = run_command(
            [*compose, "config"],
            timeout=60,
            error_code="slot_compose_evidence_failed",
            error_message="Release-slot Compose plan is unavailable.",
            env=env,
        )
        plan_digest = _normalized_compose_digest(
            config.stdout,
            app_dir=app_dir,
            source_dir=source_dir,
        )
        _archive_attached, archive_digest = _archive_override_evidence(
            app_dir
        )
        try:
            binding = engine.build_activation_slot_binding(
                app_dir,
                slot_id,
                compose_plan_sha256=plan_digest,
                archive_override_sha256=archive_digest,
            )
            # ``None`` is real current evidence (no generated override), not
            # a request to reuse the digest captured in the immutable slot.
            binding["archive_override_sha256"] = archive_digest
            binding = engine.validate_activation_slot_binding(binding)
        except Exception as exc:
            raise BridgeError(
                getattr(
                    exc,
                    "code",
                    "activation_slot_binding_invalid",
                ),
                "Release-slot activation binding is invalid.",
            ) from exc
        image_services = manifest["image_evidence"]["services"]
        required = list(CORE_RUNTIME_SERVICES)
        if require_helper_image:
            required.append("update-helper")
        required.extend(
            service
            for service in OPTIONAL_PERSISTENT_IMAGE_SERVICES
            if service in image_services
        )
        for service in required:
            expected = image_services.get(service, {}).get("image_id")
            image_id, _state = _inspect_running_service(
                compose,
                service,
                env=env,
            )
            if image_id != expected:
                raise BridgeError(
                    "slot_runtime_image_mismatch",
                    "A running service does not use its immutable slot image.",
                )
        identity_path = source_dir / ".km-vms-release.json"
        identity = read_json_object(identity_path)
        assert identity is not None
        api_identity = _api_visible_release_identity(
            compose,
            env=env,
        )
        if (
            identity.get("metadata_status") != "complete"
            or str(identity.get("version") or "")
            != binding["version"]
            or str(identity.get("commit_sha") or "").lower()
            != binding["commit"]
            or _sha256_file(identity_path)
            != binding["api_identity_sha256"]
            or _api_visible_identity_digest(compose, env=env)
            != binding["api_identity_sha256"]
            or api_identity
            != {
                "version": binding["version"],
                "commit": binding["commit"],
                "metadata_status": "complete",
                "identity_validity": "valid",
            }
        ):
            raise BridgeError(
                "slot_runtime_identity_mismatch",
                "Release-slot identity does not match API-visible identity.",
            )
        if require_http:
            for url in (
                "http://api:8000/health",
                "http://nginx/api/health",
                "http://web:3000/",
            ):
                result = run_command(
                    [
                        "curl",
                        "-fsSL",
                        "--max-time",
                        "5",
                        url,
                    ],
                    timeout=10,
                    error_code="slot_runtime_unhealthy",
                    error_message="Release-slot HTTP readiness failed.",
                    check=False,
                )
                if result.returncode != 0:
                    raise BridgeError(
                        "slot_runtime_unhealthy",
                        "Release-slot HTTP readiness failed.",
                    )
        return binding


def verify_slot_runtime(
    app_dir: Path,
    project_name: str,
    binding: dict[str, Any],
    *,
    engine: Any,
    timeout_seconds: int = 180,
    require_helper_image: bool = False,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: BridgeError | None = None
    while time.monotonic() < deadline:
        try:
            observed = capture_slot_runtime_binding(
                app_dir,
                project_name,
                str(binding["slot_id"]),
                engine=engine,
                require_http=True,
                require_helper_image=require_helper_image,
            )
            if observed != binding:
                raise BridgeError(
                    "slot_runtime_evidence_mismatch",
                    "Release-slot runtime no longer matches activation evidence.",
                )
            return
        except BridgeError as exc:
            last_error = exc
            time.sleep(2)
    if last_error is not None:
        raise last_error
    raise BridgeError(
        "slot_runtime_unhealthy",
        "Release-slot runtime verification timed out.",
    )


def reconcile_slot_runtime(
    app_dir: Path,
    project_name: str,
    slot_id: str,
    *,
    engine: Any,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="km-vms-slot-reconcile-",
    ) as temporary:
        compose, env, _source, manifest = slot_compose(
            app_dir,
            project_name,
            slot_id,
            engine=engine,
            override_root=Path(temporary),
            with_image_override=True,
        )
        services = [
            service
            for service in ACTIVATION_RUNTIME_SERVICES
            if service in manifest["compose_evidence"]["services"]
        ]
        run_command(
            [
                *compose,
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--force-recreate",
                *services,
            ],
            timeout=600,
            error_code="slot_runtime_start_failed",
            error_message="Release-slot core services could not be started.",
            env=env,
        )


def stop_slot_schema_writers(
    app_dir: Path,
    project_name: str,
    slot_id: str,
    *,
    engine: Any,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="km-vms-slot-quiesce-",
    ) as temporary:
        compose, env, _source, _manifest = slot_compose(
            app_dir,
            project_name,
            slot_id,
            engine=engine,
            override_root=Path(temporary),
            with_image_override=True,
        )
        run_command(
            [*compose, "stop", "api", "recorder"],
            timeout=180,
            error_code="slot_quiesce_failed",
            error_message="Database writers could not be paused for migration.",
            env=env,
        )


def write_activation_progress(
    request_id: str,
    *,
    status: str,
    phase: str,
    current_step: str,
) -> None:
    path_text = str(os.getenv("KM_VMS_UPDATE_PROGRESS_FILE") or "")
    if not path_text:
        return
    path = Path(path_text)
    if not path.is_absolute():
        return
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "status": status,
            "phase": phase,
            "current_step": current_step,
            "updated_at": utcnow(),
            "request_id": request_id,
            "message": "",
        },
    )


def run_target_schema_preflight(
    app_dir: Path,
    project_name: str,
    target_slot_id: str,
    *,
    engine: Any,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix="km-vms-slot-schema-preflight-",
    ) as temporary:
        compose, env, _source, _manifest = slot_compose(
            app_dir,
            project_name,
            target_slot_id,
            engine=engine,
            override_root=Path(temporary),
            with_image_override=True,
        )
        result = run_command(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "schema-update",
                "python3",
                "-m",
                "app.services.schema_update_pipeline",
                "--preflight",
            ],
            timeout=900,
            error_code="schema_preflight_failed",
            error_message="Target schema preflight failed before activation.",
            env=env,
        )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {
            "schema_migration_required",
            "schema_source_version",
            "schema_target_version",
            "schema_previous_runtime_compatible",
            "schema_preflight",
        }:
            values[key] = value
    try:
        summary = json.loads(values["schema_preflight"])
        source_version = int(values["schema_source_version"])
        target_version = int(values["schema_target_version"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BridgeError(
            "schema_preflight_evidence_invalid",
            "Target schema preflight returned invalid evidence.",
        ) from exc
    migration_required = values.get("schema_migration_required") == "true"
    compatible = (
        values.get("schema_previous_runtime_compatible") == "true"
    )
    compatibility = (
        summary.get("previous_runtime_compatibility")
        if type(summary) is dict
        else None
    )
    if (
        type(summary) is not dict
        or type(compatibility) is not dict
        or compatibility.get("status")
        != ("compatible" if compatible else "blocked")
        or bool(summary.get("migration_required"))
        is not migration_required
        or summary.get("source_schema_version") != source_version
        or summary.get("target_schema_version") != target_version
        or source_version > target_version
    ):
        raise BridgeError(
            "schema_preflight_evidence_invalid",
            "Target schema preflight returned contradictory evidence.",
        )
    if not compatible:
        raise BridgeError(
            "schema_previous_runtime_incompatible",
            "Automatic application rollback is not compatible with the planned schema path.",
        )
    compatibility_sha256 = hashlib.sha256(
        json.dumps(
            summary,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "migration_required": migration_required,
        "source_schema_version": source_version,
        "target_schema_version": target_version,
        "compatibility_sha256": compatibility_sha256,
    }


def run_target_schema_migration(
    app_dir: Path,
    project_name: str,
    target_slot_id: str,
    *,
    engine: Any,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix="km-vms-slot-schema-migrate-",
    ) as temporary:
        compose, env, _source, _manifest = slot_compose(
            app_dir,
            project_name,
            target_slot_id,
            engine=engine,
            override_root=Path(temporary),
            with_image_override=True,
        )
        run_command(
            [
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "schema-update",
                "python3",
                "-m",
                "app.services.schema_update_pipeline",
                "--migrate",
            ],
            timeout=3600,
            error_code="schema_update_failed",
            error_message="Target database migration did not complete.",
            env=env,
        )


def schema_mutation_completed(
    app_dir: Path,
    *,
    request_id: str,
    target_commit: str,
) -> bool:
    payload = read_json_object(
        app_dir / "data/update-control/schema-mutation-state.json",
        missing_ok=True,
    )
    if payload is None:
        return False
    return bool(
        payload.get("schema_version") == 1
        and payload.get("request_id") == request_id
        and str(payload.get("target_commit") or "").lower()
        == target_commit
        and payload.get("mutation_started") is True
        and payload.get("state") == "completed"
    )


def _observed_slot_id(engine: Any, app_dir: Path) -> str | None:
    try:
        active = engine.read_active_slot(app_dir)
    except Exception as exc:
        raise BridgeError(
            getattr(exc, "code", "active_pointer_invalid"),
            "Active release pointer is invalid.",
        ) from exc
    return active[0] if active is not None else None


def transition_journal(
    engine: Any,
    app_dir: Path,
    request_id: str,
    phase: str,
    **updates: Any,
) -> dict[str, Any]:
    updates.setdefault("record_pointer", True)
    updates.setdefault(
        "pointer_slot_id",
        _observed_slot_id(engine, app_dir),
    )
    try:
        return engine.transition_activation_journal(
            app_dir,
            request_id=request_id,
            phase=phase,
            **updates,
        )
    except Exception as exc:
        raise BridgeError(
            getattr(exc, "code", "activation_journal_invalid"),
            "Activation journal transition failed.",
        ) from exc


def block_activation(
    engine: Any,
    app_dir: Path,
    request_id: str,
    category: str,
    *,
    rollback_trigger: str | None = None,
) -> dict[str, Any]:
    return transition_journal(
        engine,
        app_dir,
        request_id,
        "blocked",
        failure_category=category,
        rollback_trigger=rollback_trigger,
    )


def rollback_activation(
    engine: Any,
    app_dir: Path,
    project_name: str,
    journal: dict[str, Any],
    *,
    trigger: str,
) -> dict[str, Any]:
    request_id = journal["request_id"]
    previous = journal["previous"]
    target = journal["target"]
    observed = _observed_slot_id(engine, app_dir)
    if observed not in {target["slot_id"], previous["slot_id"]}:
        return block_activation(
            engine,
            app_dir,
            request_id,
            "rollback_pointer_conflict",
            rollback_trigger=trigger,
        )
    transition_journal(
        engine,
        app_dir,
        request_id,
        "rolling_back",
        rollback_trigger=trigger,
    )
    write_activation_progress(
        request_id,
        status="rolling_back",
        phase="rolling_back",
        current_step="health_check",
    )
    try:
        if observed != previous["slot_id"]:
            engine.atomic_switch_pointer(
                app_dir,
                previous["slot_id"],
            )
        transition_journal(
            engine,
            app_dir,
            request_id,
            "rolling_back",
            rollback_trigger=trigger,
        )
        reconcile_slot_runtime(
            app_dir,
            project_name,
            previous["slot_id"],
            engine=engine,
        )
        verify_slot_runtime(
            app_dir,
            project_name,
            previous,
            engine=engine,
            require_helper_image=True,
        )
        terminal = transition_journal(
            engine,
            app_dir,
            request_id,
            "failed_rolled_back",
            previous_verified=True,
            rollback_trigger=trigger,
            failure_category=trigger,
        )
        attempt_terminal_release_cleanup(
            engine,
            app_dir,
            project_name,
            terminal,
        )
        return terminal
    except Exception:
        return block_activation(
            engine,
            app_dir,
            request_id,
            "rollback_verification_failed",
            rollback_trigger=trigger,
        )


def block_after_restoring_previous(
    engine: Any,
    app_dir: Path,
    project_name: str,
    journal: dict[str, Any],
    category: str,
) -> dict[str, Any]:
    try:
        if _observed_slot_id(engine, app_dir) not in {
            None,
            journal["previous"]["slot_id"],
        }:
            return block_activation(
                engine,
                app_dir,
                journal["request_id"],
                "activation_pointer_conflict",
            )
        reconcile_slot_runtime(
            app_dir,
            project_name,
            journal["previous"]["slot_id"],
            engine=engine,
        )
        verify_slot_runtime(
            app_dir,
            project_name,
            journal["previous"],
            engine=engine,
            require_helper_image=True,
        )
    except Exception:
        return block_activation(
            engine,
            app_dir,
            journal["request_id"],
            "previous_recovery_failed",
        )
    return block_activation(
        engine,
        app_dir,
        journal["request_id"],
        category,
    )


def cleanup_unprotected_slot_images(
    project_name: str,
    protected_slot_ids: set[str],
) -> list[str]:
    project_name = require_project_name(project_name)
    protected = {
        slot_id.lower()
        for slot_id in protected_slot_ids
        if SLOT_ID_RE.fullmatch(str(slot_id))
    }
    if len(protected) != len(protected_slot_ids):
        raise BridgeError(
            "slot_cleanup_evidence_invalid",
            "Protected release-slot cleanup evidence is invalid.",
        )
    listed = run_command(
        [
            "docker",
            "image",
            "ls",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ],
        timeout=60,
        error_code="slot_image_cleanup_failed",
        error_message="Product-owned release-slot image aliases could not be listed.",
    )
    alias_repository_prefix = f"km-vms-{project_name}-slot-"
    release_repositories = {
        f"{project_name}-api",
        f"{project_name}-recorder",
        f"{project_name}-web",
        f"km-vms-{project_name}-update-control",
        f"km-vms-{project_name}-update-helper",
    }
    candidates: set[str] = set()
    for raw_ref in listed.stdout.splitlines():
        image_ref = raw_ref.strip()
        repository, separator, tag = image_ref.rpartition(":")
        if (
            separator != ":"
            or not SLOT_ID_RE.fullmatch(tag)
        ):
            continue
        product_owned = repository in release_repositories
        if repository.startswith(alias_repository_prefix):
            service = repository[len(alias_repository_prefix) :]
            product_owned = bool(
                re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", service)
            )
        if not product_owned:
            continue
        if tag.lower() not in protected:
            candidates.add(image_ref)

    removed: list[str] = []
    failed: list[str] = []
    for image_ref in sorted(candidates):
        result = run_command(
            ["docker", "image", "rm", image_ref],
            timeout=60,
            error_code="slot_image_cleanup_failed",
            error_message="A product-owned release-slot image alias could not be removed.",
            check=False,
        )
        if result.returncode == 0:
            removed.append(image_ref)
        else:
            failed.append(image_ref)
    if failed:
        raise BridgeError(
            "slot_image_cleanup_failed",
            "One or more product-owned release-slot image aliases could not be removed.",
        )
    return removed


def cleanup_terminal_release_artifacts(
    engine: Any,
    app_dir: Path,
    project_name: str,
    journal: dict[str, Any],
) -> dict[str, Any]:
    if journal.get("phase") not in {
        "completed",
        "failed_rolled_back",
    }:
        raise BridgeError(
            "slot_cleanup_terminal_evidence_required",
            "Release-slot cleanup requires a verified terminal activation.",
        )
    removed_slots = engine.cleanup_unprotected_slots(
        app_dir,
        retain_slot_ids=set(),
        maximum_unprotected=0,
        terminal_evidence=True,
    )
    protected = engine.protected_slot_ids(app_dir)
    removed_images = cleanup_unprotected_slot_images(
        project_name,
        protected,
    )
    staging_removed = engine.cleanup_request_staging(
        app_dir,
        request_id=journal["request_id"],
        terminal_evidence=True,
    )
    return {
        "removed_slots": removed_slots,
        "removed_image_refs": removed_images,
        "request_staging_removed": staging_removed,
    }


def attempt_terminal_release_cleanup(
    engine: Any,
    app_dir: Path,
    project_name: str,
    journal: dict[str, Any],
) -> None:
    try:
        cleanup_terminal_release_artifacts(
            engine,
            app_dir,
            project_name,
            journal,
        )
    except Exception as exc:
        print(
            "WARNING [terminal_release_cleanup_deferred]: "
            f"{exc}",
            file=sys.stderr,
        )


def schedule_target_helper_handoff(
    engine: Any,
    app_dir: Path,
    project_name: str,
    request_id: str,
    target_slot_id: str,
) -> None:
    _root, source_dir, manifest = slot_record(
        app_dir,
        target_slot_id,
        engine=engine,
    )
    helper = manifest["image_evidence"]["services"].get(
        "update-helper"
    )
    if type(helper) is not dict:
        raise BridgeError(
            "helper_handoff_evidence_missing",
            "Prepared target helper evidence is unavailable.",
        )
    helper_image = str(helper.get("immutable_image_ref") or "")
    expected_image_id = str(helper.get("image_id") or "").lower()
    if (
        not helper_image
        or not IMAGE_ID_RE.fullmatch(expected_image_id)
    ):
        raise BridgeError(
            "helper_handoff_evidence_missing",
            "Prepared target helper evidence is invalid.",
        )
    bridge_script = (
        source_dir / "scripts/km-vms-update-helper-bridge.py"
    )
    schedule_refresh(
        app_dir=app_dir,
        project_name=project_name,
        helper_image=helper_image,
        request_id=request_id,
        expected_image_id=expected_image_id,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        bridge_script=bridge_script,
        target_slot=target_slot_id,
    )


def converge_activation(
    engine: Any,
    app_dir: Path,
    project_name: str,
    request_id: str,
    *,
    terminal_owner: bool = False,
) -> dict[str, Any]:
    for _iteration in range(16):
        try:
            journal = engine.read_activation_journal(app_dir)
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "activation_journal_invalid"),
                "Activation journal is unavailable or invalid.",
            ) from exc
        if journal["request_id"] != request_id:
            raise BridgeError(
                "activation_request_conflict",
                "Activation journal belongs to another request.",
            )
        phase = journal["phase"]
        if phase in {"completed", "failed_rolled_back", "blocked"}:
            return journal
        previous = journal["previous"]
        target = journal["target"]
        schema = journal["schema"]
        observed = _observed_slot_id(engine, app_dir)

        if phase == "target_prepared":
            if observed not in {None, previous["slot_id"]}:
                return block_activation(
                    engine,
                    app_dir,
                    request_id,
                    "activation_pointer_conflict",
                )
            if schema["migration_required"]:
                transition_journal(
                    engine,
                    app_dir,
                    request_id,
                    "quiescing",
                )
            else:
                transition_journal(
                    engine,
                    app_dir,
                    request_id,
                    "activating",
                )
            continue

        if phase == "quiescing":
            if not schema["migration_required"]:
                return block_activation(
                    engine,
                    app_dir,
                    request_id,
                    "schema_journal_conflict",
                )
            try:
                stop_slot_schema_writers(
                    app_dir,
                    project_name,
                    previous["slot_id"],
                    engine=engine,
                )
                transition_journal(
                    engine,
                    app_dir,
                    request_id,
                    "schema_preparing",
                    migration_invoked=True,
                )
            except BridgeError:
                return block_after_restoring_previous(
                    engine,
                    app_dir,
                    project_name,
                    journal,
                    "slot_quiesce_failed",
                )
            try:
                run_target_schema_migration(
                    app_dir,
                    project_name,
                    target["slot_id"],
                    engine=engine,
                )
            except BridgeError:
                if not schema_mutation_completed(
                    app_dir,
                    request_id=request_id,
                    target_commit=target["commit"],
                ):
                    return block_after_restoring_previous(
                        engine,
                        app_dir,
                        project_name,
                        journal,
                        "schema_update_failed",
                    )
            if not schema_mutation_completed(
                app_dir,
                request_id=request_id,
                target_commit=target["commit"],
            ):
                return block_after_restoring_previous(
                    engine,
                    app_dir,
                    project_name,
                    journal,
                    "schema_evidence_missing",
                )
            transition_journal(
                engine,
                app_dir,
                request_id,
                "schema_preparing",
                migration_invoked=True,
                migration_completed=True,
            )
            continue

        if phase == "schema_preparing":
            if not schema["migration_required"]:
                return block_activation(
                    engine,
                    app_dir,
                    request_id,
                    "schema_journal_conflict",
                )
            if schema["migration_completed"] or schema_mutation_completed(
                app_dir,
                request_id=request_id,
                target_commit=target["commit"],
            ):
                transition_journal(
                    engine,
                    app_dir,
                    request_id,
                    "schema_preparing",
                    migration_invoked=True,
                    migration_completed=True,
                )
                transition_journal(
                    engine,
                    app_dir,
                    request_id,
                    "activating",
                )
                continue
            return block_after_restoring_previous(
                engine,
                app_dir,
                project_name,
                journal,
                (
                    "schema_migration_interrupted"
                    if schema["migration_invoked"]
                    else "schema_journal_conflict"
                ),
            )

        if phase == "activating":
            if schema["migration_required"] and not schema[
                "migration_completed"
            ]:
                return block_activation(
                    engine,
                    app_dir,
                    request_id,
                    "schema_evidence_missing",
                )
            if observed not in {
                None,
                previous["slot_id"],
                target["slot_id"],
            }:
                return block_activation(
                    engine,
                    app_dir,
                    request_id,
                    "activation_pointer_conflict",
                )
            write_activation_progress(
                request_id,
                status="activating",
                phase="activating",
                current_step="applying",
            )
            if observed != target["slot_id"]:
                try:
                    engine.atomic_switch_pointer(
                        app_dir,
                        target["slot_id"],
                    )
                except Exception:
                    try:
                        observed_after_switch = _observed_slot_id(
                            engine,
                            app_dir,
                        )
                    except BridgeError:
                        return block_activation(
                            engine,
                            app_dir,
                            request_id,
                            "active_pointer_switch_failed",
                        )
                    if observed_after_switch == target["slot_id"]:
                        transition_journal(
                            engine,
                            app_dir,
                            request_id,
                            "verifying_target",
                        )
                        continue
                    if observed_after_switch in {
                        None,
                        previous["slot_id"],
                    }:
                        return block_after_restoring_previous(
                            engine,
                            app_dir,
                            project_name,
                            journal,
                            "active_pointer_switch_failed",
                        )
                    return block_activation(
                        engine,
                        app_dir,
                        request_id,
                        "activation_pointer_conflict",
                    )
            transition_journal(
                engine,
                app_dir,
                request_id,
                "verifying_target",
            )
            continue

        if phase == "verifying_target":
            if observed != target["slot_id"]:
                return block_activation(
                    engine,
                    app_dir,
                    request_id,
                    "target_pointer_mismatch",
                )
            write_activation_progress(
                request_id,
                status="reconnecting",
                phase="verifying_target",
                current_step="health_check",
            )
            try:
                reconcile_slot_runtime(
                    app_dir,
                    project_name,
                    target["slot_id"],
                    engine=engine,
                )
                verify_slot_runtime(
                    app_dir,
                    project_name,
                    target,
                    engine=engine,
                )
            except BridgeError as exc:
                trigger = (
                    "target_identity_mismatch"
                    if "identity" in exc.code
                    or "evidence_mismatch" in exc.code
                    else "target_health_failed"
                )
                return rollback_activation(
                    engine,
                    app_dir,
                    project_name,
                    journal,
                    trigger=trigger,
                )
            transition_journal(
                engine,
                app_dir,
                request_id,
                "committing_target",
                target_verified=True,
            )
            continue

        if phase == "committing_target":
            try:
                verify_slot_runtime(
                    app_dir,
                    project_name,
                    target,
                    engine=engine,
                )
            except BridgeError as exc:
                trigger = (
                    "target_identity_mismatch"
                    if "identity" in exc.code
                    or "evidence_mismatch" in exc.code
                    else "target_health_failed"
                )
                return rollback_activation(
                    engine,
                    app_dir,
                    project_name,
                    journal,
                    trigger=trigger,
                )
            write_activation_progress(
                request_id,
                status="applying",
                phase="committing_target",
                current_step="commit_verification",
            )
            completed = transition_journal(
                engine,
                app_dir,
                request_id,
                "completed",
                target_verified=True,
            )
            try:
                if terminal_owner:
                    _root, _source, manifest = slot_record(
                        app_dir,
                        target["slot_id"],
                        engine=engine,
                    )
                    helper = manifest["image_evidence"]["services"].get(
                        "update-helper"
                    )
                    if type(helper) is not dict:
                        raise BridgeError(
                            "helper_handoff_evidence_missing",
                            "Prepared target helper evidence is unavailable.",
                        )
                    helper_image = str(
                        helper.get("immutable_image_ref") or ""
                    )
                    helper_image_id = str(
                        helper.get("image_id") or ""
                    ).lower()
                    if (
                        not helper_image
                        or not IMAGE_ID_RE.fullmatch(helper_image_id)
                    ):
                        raise BridgeError(
                            "helper_handoff_evidence_missing",
                            "Prepared target helper evidence is invalid.",
                        )
                    recreate_and_verify_helper(
                        app_dir=app_dir,
                        project_name=project_name,
                        helper_image=helper_image,
                        expected_image_id=helper_image_id,
                        target_slot=target["slot_id"],
                        engine=engine,
                    )
                else:
                    schedule_target_helper_handoff(
                        engine,
                        app_dir,
                        project_name,
                        request_id,
                        target["slot_id"],
                    )
            except BridgeError as exc:
                # The target release is already healthy and committed. A
                # post-terminal helper-image refresh is retryable operational
                # work, not a reason to roll back a verified product runtime.
                print(
                    "WARNING [helper_handoff_deferred]: "
                    f"{exc}",
                    file=sys.stderr,
                )
            attempt_terminal_release_cleanup(
                engine,
                app_dir,
                project_name,
                completed,
            )
            return completed

        if phase == "rolling_back":
            trigger = str(
                journal.get("rollback_trigger")
                or "target_health_failed"
            )
            return rollback_activation(
                engine,
                app_dir,
                project_name,
                journal,
                trigger=trigger,
            )

        return block_activation(
            engine,
            app_dir,
            request_id,
            "activation_phase_invalid",
        )
    return block_activation(
        engine,
        app_dir,
        request_id,
        "activation_retry_exhausted",
    )


def activate_or_resume(args: argparse.Namespace) -> int:
    app_dir = require_app_dir(args.app_dir)
    project_name = require_project_name(args.project_name)
    request_id = require_request_id(args.request_id)
    engine = load_slot_engine(bridge_source_root())
    try:
        journal = engine.read_activation_journal(
            app_dir,
            missing_ok=True,
        )
    except Exception as exc:
        raise BridgeError(
            getattr(exc, "code", "activation_journal_invalid"),
            "Activation journal is unavailable or invalid.",
        ) from exc
    supplied_previous_raw = str(
        getattr(args, "previous_slot", None) or ""
    )
    supplied_target_raw = str(
        getattr(args, "target_slot", None) or ""
    )
    try:
        supplied_previous = (
            engine.require_slot_id(supplied_previous_raw)
            if supplied_previous_raw
            else ""
        )
        supplied_target = (
            engine.require_slot_id(supplied_target_raw, target=True)
            if supplied_target_raw
            else ""
        )
    except Exception as exc:
        raise BridgeError(
            getattr(exc, "code", "activation_slot_invalid"),
            "Prepared activation slot identity is invalid.",
        ) from exc
    starting_new = journal is None or (
        journal["phase"] in {"completed", "failed_rolled_back", "blocked"}
        and journal["request_id"] != request_id
    )
    if starting_new:
        previous_slot_id = supplied_previous
        target_slot_id = supplied_target
        if not previous_slot_id or not target_slot_id:
            raise BridgeError(
                "activation_slots_missing",
                "Prepared previous and target slots are required.",
            )
        expected_commit = str(
            getattr(args, "target_commit", None) or ""
        ).lower()
        expected_version = str(
            getattr(args, "target_version", None) or ""
        )
        if (
            not COMMIT_SHA_RE.fullmatch(expected_commit)
            or not expected_version
        ):
            raise BridgeError(
                "activation_target_identity_mismatch",
                "Prepared target does not match the admitted release.",
            )
        try:
            active = engine.read_active_slot(app_dir)
            if active is None or active[0] != previous_slot_id:
                raise BridgeError(
                    "activation_previous_binding_mismatch",
                    "Prepared previous slot is not the active runtime.",
                )
            target = engine.build_activation_slot_binding(
                app_dir,
                target_slot_id,
            )
            if (
                target["commit"] != expected_commit
                or target["version"] != expected_version
            ):
                raise BridgeError(
                    "activation_target_identity_mismatch",
                    "Prepared target does not match the admitted release.",
                )
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "activation_slot_binding_invalid"),
                "Prepared activation slot binding is invalid.",
            ) from exc
        previous = capture_slot_runtime_binding(
            app_dir,
            project_name,
            previous_slot_id,
            engine=engine,
            require_http=True,
            require_helper_image=True,
        )
        schema = run_target_schema_preflight(
            app_dir,
            project_name,
            target_slot_id,
            engine=engine,
        )
        write_activation_progress(
            request_id,
            status="preparing",
            phase="target_prepared",
            current_step="preflight",
        )
        try:
            active_after = engine.read_active_slot(app_dir)
            target_after = engine.build_activation_slot_binding(
                app_dir,
                target_slot_id,
            )
            if (
                active_after is None
                or active_after[0] != previous_slot_id
                or target_after != target
            ):
                raise BridgeError(
                    "activation_slot_binding_changed",
                    "Release-slot binding changed during schema preflight.",
                )
            previous_after = capture_slot_runtime_binding(
                app_dir,
                project_name,
                previous_slot_id,
                engine=engine,
                require_http=True,
                require_helper_image=True,
            )
            if previous_after != previous:
                raise BridgeError(
                    "activation_slot_binding_changed",
                    "Active runtime binding changed during schema preflight.",
                )
            journal = engine.initialize_activation_journal(
                app_dir,
                request_id=request_id,
                previous=previous,
                target=target,
                compatibility_sha256=schema[
                    "compatibility_sha256"
                ],
                source_schema_version=schema[
                    "source_schema_version"
                ],
                target_schema_version=schema[
                    "target_schema_version"
                ],
                migration_required=schema["migration_required"],
            )
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "activation_journal_invalid"),
                "Prepared activation could not be journaled safely.",
            ) from exc
    elif journal["request_id"] != request_id:
        raise BridgeError(
            "activation_request_conflict",
            "Activation journal belongs to another request.",
        )
    elif supplied_previous or supplied_target:
        if (
            supplied_previous != journal["previous"]["slot_id"]
            or supplied_target != journal["target"]["slot_id"]
            or str(getattr(args, "target_commit", None) or "").lower()
            != journal["target"]["commit"]
            or str(getattr(args, "target_version", None) or "")
            != journal["target"]["version"]
        ):
            raise BridgeError(
                "activation_journal_conflict",
                "Activation resume arguments contradict its journal.",
            )
    result = converge_activation(
        engine,
        app_dir,
        project_name,
        request_id,
        terminal_owner=bool(getattr(args, "terminal", False)),
    )
    print(
        json.dumps(
            {
                "activation": result["phase"],
                "request_id": request_id,
                "previous_slot": result["previous"]["slot_id"],
                "target_slot": result["target"]["slot_id"],
                "failure_category": result["failure_category"],
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def prepare_trusted_target_slot(args: argparse.Namespace) -> int:
    """Build and finalize a trusted target without changing the active pointer."""

    app_dir = require_app_dir(args.app_dir)
    source_input = Path(args.target_source_dir)
    try:
        source_input.lstat()
        target_source_dir = source_input.resolve(strict=True)
    except OSError as exc:
        raise BridgeError(
            "target_source_invalid",
            "The staged target source is unavailable.",
        ) from exc
    if source_input.is_symlink() or not target_source_dir.is_dir():
        raise BridgeError(
            "target_source_invalid",
            "The staged target source is unsafe.",
        )
    request_id = require_request_id(args.request_id)
    trusted_commit = str(args.trusted_commit or "").lower()
    declared_version = str(args.declared_version or "")
    project_name = require_project_name(args.project_name)
    if not COMMIT_SHA_RE.fullmatch(trusted_commit):
        raise BridgeError(
            "target_commit_invalid",
            "Trusted target commit must be exact 40-hex.",
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}", declared_version):
        raise BridgeError(
            "target_version_invalid",
            "Trusted target version is invalid.",
        )
    slot_tool = target_source_dir / "scripts/km-vms-release-slots.py"
    if slot_tool.is_symlink() or not slot_tool.is_file():
        raise BridgeError(
            "slot_engine_missing",
            "Trusted target release has no release-slot engine.",
        )
    inspect_payload = _parse_command_json(
        run_command(
            [
                "python3",
                str(slot_tool),
                "inspect",
                "--app-dir",
                str(app_dir),
            ],
            timeout=30,
            error_code="slot_layout_prepare_failed",
            error_message="Stable release-slot layout could not be prepared.",
        ),
        error_code="slot_layout_prepare_failed",
        error_message="Stable release-slot layout returned invalid evidence.",
    )
    if inspect_payload.get("activation_cli_enabled") is not True:
        raise BridgeError(
            "slot_activation_unavailable",
            "Trusted target release does not provide the Stage C activation engine.",
        )
    stage_payload = _parse_command_json(
        run_command(
            [
                "python3",
                str(slot_tool),
                "stage-target",
                "--app-dir",
                str(app_dir),
                "--source-dir",
                str(target_source_dir),
                "--request-id",
                request_id,
                "--trusted-commit",
                trusted_commit,
                "--declared-version",
                declared_version,
            ],
            timeout=300,
            error_code="slot_target_stage_failed",
            error_message="Trusted target source could not be materialized.",
        ),
        error_code="slot_target_stage_failed",
        error_message="Trusted target source returned invalid staging evidence.",
    )
    slot_id = str(stage_payload.get("slot_id") or "")
    if slot_id != f"release-{trusted_commit}":
        raise BridgeError(
            "slot_target_stage_failed",
            "Trusted target slot identity is contradictory.",
        )
    if stage_payload.get("status") == "reused":
        manifest = stage_payload.get("manifest")
        if type(manifest) is not dict:
            raise BridgeError(
                "slot_target_stage_failed",
                "Reused target slot returned no immutable manifest.",
            )
        verify_immutable_images(manifest.get("image_evidence", {}))
        print("target_slot_prepare=REUSED")
        print(f"target_slot={slot_id}")
        print("activation_enabled=true")
        return 0
    if stage_payload.get("status") != "staged":
        raise BridgeError(
            "slot_target_stage_failed",
            "Trusted target did not reach a staged state.",
        )
    staged_source = Path(str(stage_payload.get("source_path") or ""))
    if not staged_source.is_absolute() or not staged_source.is_dir():
        raise BridgeError(
            "slot_target_stage_failed",
            "Trusted target staged source path is invalid.",
        )

    target_env = os.environ.copy()
    target_env["COMPOSE_PROJECT_NAME"] = project_name
    target_env["KM_VMS_RELEASE_IMAGE_TAG"] = slot_id
    compose = compose_base(
        app_dir,
        project_name,
        source_dir=staged_source,
        include_archive_override=True,
    )
    config = run_command(
        [*compose, "config"],
        timeout=60,
        error_code="slot_compose_evidence_failed",
        error_message="Trusted target Compose plan could not be validated.",
        env=target_env,
    )
    services = _compose_services(compose, env=target_env)
    if not set(TARGET_EVIDENCE_SERVICES).issubset(services):
        raise BridgeError(
            "slot_compose_evidence_failed",
            "Trusted target Compose plan lacks a required service.",
        )
    image_refs = _compose_image_refs(compose, env=target_env)
    run_command(
        [*compose, "build", *TARGET_BUILD_SERVICES],
        timeout=3600,
        error_code="slot_target_build_failed",
        error_message="Trusted target images could not be built before activation.",
        env=target_env,
    )
    nginx_ref = image_refs.get("nginx")
    if not nginx_ref:
        raise BridgeError(
            "slot_image_evidence_missing",
            "Trusted target Nginx image plan is incomplete.",
        )
    nginx_present = run_command(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            nginx_ref,
        ],
        timeout=30,
        error_code="slot_target_pull_failed",
        error_message="Trusted target Nginx image could not be inspected.",
        check=False,
    )
    if nginx_present.returncode != 0:
        run_command(
            [*compose, "pull", "nginx"],
            timeout=600,
            error_code="slot_target_pull_failed",
            error_message="Trusted target Nginx image could not be prepared.",
            env=target_env,
        )
    raw_images: dict[str, Any] = {"schema_version": 1, "services": {}}
    for service in TARGET_EVIDENCE_SERVICES:
        image_ref = image_refs.get(service)
        if not image_ref:
            raise BridgeError(
                "slot_image_evidence_missing",
                "Trusted target image plan is incomplete.",
            )
        if service != "nginx" and not image_ref.endswith(f":{slot_id}"):
            raise BridgeError(
                "slot_image_evidence_failed",
                "A target-built image used a mutable tag.",
            )
        raw_images["services"][service] = {
            "image_id": _image_id_for_ref(image_ref),
            "source_image_ref": image_ref,
        }
    image_evidence = preserve_slot_images(
        raw_images,
        project_name=project_name,
        slot_id=slot_id,
    )
    archive_attached, archive_digest = _archive_override_evidence(app_dir)
    plan_digest = _normalized_compose_digest(
        config.stdout,
        app_dir=app_dir,
        source_dir=staged_source,
    )
    compose_evidence = {
        "schema_version": 1,
        "project_name": project_name,
        "project_directory": "source",
        "captured_plan_sha256": plan_digest,
        "slot_plan_sha256": plan_digest,
        "archive_override_attached": archive_attached,
        "archive_override_sha256": archive_digest,
        "runtime_override_sha256": None,
        "shared_root_contract": "stable_app_dir_v1",
        "services": services,
    }
    evidence_root = app_dir / "data/update-runtime/staging"
    with tempfile.TemporaryDirectory(
        prefix=".target-evidence-",
        dir=evidence_root,
    ) as temporary:
        temporary_root = Path(temporary)
        compose_path = temporary_root / "compose.json"
        image_path = temporary_root / "images.json"
        atomic_write_json(compose_path, compose_evidence)
        atomic_write_json(image_path, image_evidence)
        finalized = _parse_command_json(
            run_command(
                [
                    "python3",
                    str(slot_tool),
                    "finalize",
                    "--app-dir",
                    str(app_dir),
                    "--request-id",
                    request_id,
                    "--compose-evidence-file",
                    str(compose_path),
                    "--image-evidence-file",
                    str(image_path),
                ],
                timeout=300,
                error_code="slot_target_finalize_failed",
                error_message="Trusted target slot could not be finalized.",
            ),
            error_code="slot_target_finalize_failed",
            error_message="Trusted target slot returned invalid final evidence.",
        )
    if (
        finalized.get("slot_id") != slot_id
        or finalized.get("status") not in {"published", "reused"}
    ):
        raise BridgeError(
            "slot_target_finalize_failed",
            "Trusted target final evidence is contradictory.",
        )
    print("target_slot_prepare=PASS")
    print(f"target_slot={slot_id}")
    print("activation_enabled=true")
    return 0


def handoff(args: argparse.Namespace) -> int:
    """Bind one admitted request to its exact current release."""

    app_dir = require_app_dir(args.app_dir)
    target_source_input = Path(args.target_source_dir)
    try:
        target_source_input.lstat()
        target_source_dir = target_source_input.resolve(strict=True)
    except OSError as exc:
        raise BridgeError(
            "target_source_invalid",
            "The staged target source is unavailable or incomplete.",
        ) from exc
    if (
        target_source_input.is_symlink()
        or not target_source_dir.is_dir()
        or target_source_dir.is_symlink()
        or not (
            target_source_dir / "release/km-vms-release.json"
        ).is_file()
    ):
        raise BridgeError(
            "target_source_invalid",
            "The staged target source is unavailable or incomplete.",
        )
    request_id = require_request_id(args.request_id)
    archive_override_changed = normalize_archive_roots_override(app_dir)
    engine = load_slot_engine(target_source_dir)
    try:
        active = engine.read_active_slot(app_dir)
    except Exception as exc:
        raise BridgeError(
            getattr(exc, "code", "active_pointer_invalid"),
            "Active release pointer is invalid.",
        ) from exc
    terminal_request: dict[str, Any] | None = None
    if bool(getattr(args, "terminal", False)):
        trusted_commit = str(
            getattr(args, "trusted_commit", None) or ""
        ).lower()
        declared_version = str(
            getattr(args, "declared_version", None) or ""
        )
        if (
            not COMMIT_SHA_RE.fullmatch(trusted_commit)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}",
                declared_version,
            )
        ):
            raise BridgeError(
                "terminal_request_invalid",
                "Terminal update target identity is invalid.",
            )
        terminal_request = {
            "schema_version": 1,
            "request_id": request_id,
            "requested_at": utcnow(),
            "intent": "apply_update",
            "confirmed": True,
            "source": {
                "version": declared_version,
                "commit": trusted_commit,
            },
        }
    if active is not None:
        previous_slot_id, active_source = active
        installed_identity = capture_installed_source_identity(
            app_dir,
            request_id=request_id,
            target_source_dir=target_source_dir,
            installed_source_dir=active_source,
            request_override=terminal_request,
        )
        project_name = require_project_name(
            str(getattr(args, "project_name", None) or "").strip()
            or os.getenv("KM_VMS_PROJECT_NAME", "").strip()
        )
        capture_slot_runtime_binding(
            app_dir,
            project_name,
            previous_slot_id,
            engine=engine,
            require_http=True,
            require_helper_image=True,
        )
        slot_result = previous_slot_id
        handoff_kind = "active_slot"
    else:
        installed_identity = capture_installed_source_identity(
            app_dir,
            request_id=request_id,
            target_source_dir=target_source_dir,
            request_override=terminal_request,
        )
        slot_result = prepare_legacy_adopted_slot(
            app_dir=app_dir,
            target_source_dir=target_source_dir,
            request_id=request_id,
            installed_identity=installed_identity,
        )
        handoff_kind = "legacy_adoption"
    print("schema_handoff=PASS")
    print(f"schema_handoff_request_id={request_id}")
    print(
        "archive_roots_override="
        + ("normalized" if archive_override_changed else "unchanged")
    )
    print(f"handoff_kind={handoff_kind}")
    print(
        "activation_owner="
        + ("terminal_lock" if terminal_request is not None else "update_helper")
    )
    print(f"previous_slot={slot_result}")
    if handoff_kind == "legacy_adoption":
        print(f"adopted_slot={slot_result}")
    return 0


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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError(error_code, error_message) from exc
    if check and result.returncode != 0:
        raise BridgeError(error_code, error_message)
    return result


def docker_compose_command() -> list[str]:
    configured = str(os.getenv("KM_VMS_DOCKER_COMPOSE") or "").strip()
    kind = str(
        os.getenv("KM_VMS_DOCKER_COMPOSE_KIND") or ""
    ).strip()
    if not configured:
        return ["docker", "compose"]
    if (
        any(char.isspace() or char in "\r\n\x00" for char in configured)
        or kind not in {"plugin", "standalone"}
    ):
        raise BridgeError(
            "compose_unavailable",
            "Docker Compose command binding is invalid.",
        )
    return (
        [configured, "compose"]
        if kind == "plugin"
        else [configured]
    )


def ensure_docker_runtime() -> None:
    run_command(
        ["docker", "version"],
        timeout=20,
        error_code="docker_unavailable",
        error_message="Docker daemon is unavailable to the update-helper bridge.",
    )
    run_command(
        [*docker_compose_command(), "version"],
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
    bridge_script: Path | None = None,
    target_slot: str | None = None,
) -> str:
    coordinator_name = f"km-vms-helper-refresh-{request_id.removeprefix('update-').lower()}"
    existing_image_id = inspected_container_image(coordinator_name)
    if existing_image_id is not None:
        if existing_image_id != expected_image_id:
            raise BridgeError("coordinator_image_mismatch", "Existing helper refresh coordinator uses another image.")
        return "already_scheduled"

    script_path = bridge_script or (
        app_dir / "scripts/km-vms-update-helper-bridge.py"
    )
    if (
        not script_path.is_absolute()
        or script_path.is_symlink()
        or not script_path.is_file()
        or not script_path.resolve().is_relative_to(app_dir.resolve())
    ):
        raise BridgeError(
            "coordinator_bridge_invalid",
            "Helper refresh bridge is unavailable or unsafe.",
        )
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
    if target_slot is not None:
        command.extend(["--target-slot", target_slot])
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


def acquire_helper_transition_lease(control_dir: Path):
    lease_file = control_dir / "update-helper-claim.lock"
    try:
        stream = lease_file.open("a+", encoding="utf-8")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return stream
    except OSError as exc:
        try:
            stream.close()
        except (NameError, OSError):
            pass
        raise BridgeError("helper_lease_unavailable", "Cannot inspect the active update-helper lease.") from exc


def release_helper_transition_lease(stream: Any) -> None:
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def compose_base(
    app_dir: Path,
    project_name: str,
    *,
    source_dir: Path | None = None,
    runtime_override: Path | None = None,
    image_override: Path | None = None,
    include_archive_override: bool = True,
) -> list[str]:
    source = source_dir or app_dir
    if source.is_symlink() or not (source / "docker-compose.yml").is_file():
        raise BridgeError(
            "slot_compose_evidence_failed",
            "Compose product source is unavailable or unsafe.",
        )
    command = [
        *docker_compose_command(),
        "--env-file",
        str(app_dir / ".env"),
        "--project-directory",
        str(source),
        "-f",
        str(source / "docker-compose.yml"),
        "-p",
        project_name,
    ]
    if runtime_override is None:
        possible = source.parent / "docker-compose.runtime-override.yml"
        if (
            source.name == "source"
            and possible.is_file()
            and not possible.is_symlink()
        ):
            runtime_override = possible
    if runtime_override is not None:
        if (
            not runtime_override.is_absolute()
            or runtime_override.is_symlink()
            or not runtime_override.is_file()
        ):
            raise BridgeError(
                "slot_runtime_override_failed",
                "Release-slot runtime Compose override is unavailable or unsafe.",
            )
        command.extend(["-f", str(runtime_override)])
    if image_override is not None:
        if (
            not image_override.is_absolute()
            or image_override.is_symlink()
            or not image_override.is_file()
        ):
            raise BridgeError(
                "slot_image_override_failed",
                "Release-slot image override is unavailable or unsafe.",
            )
        command.extend(["-f", str(image_override)])
    archive_override = (
        app_dir / "data/install-control/docker-compose.archive-roots.yml"
    )
    if include_archive_override and archive_override.is_file():
        if archive_override.is_symlink():
            raise BridgeError(
                "archive_roots_override_invalid",
                "Generated archive-roots Compose override is unsafe.",
            )
        command.extend(["-f", str(archive_override)])
    return command


def recreate_and_verify_helper(
    *,
    app_dir: Path,
    project_name: str,
    helper_image: str,
    expected_image_id: str,
    target_slot: str | None = None,
    engine: Any | None = None,
) -> str:
    environment: dict[str, str] | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if target_slot is None:
            if docker_image_id(helper_image) != expected_image_id:
                raise BridgeError(
                    "prepared_image_changed",
                    "The prepared update-helper image changed before activation.",
                )
            compose = compose_base(app_dir, project_name)
        else:
            if engine is None:
                raise BridgeError(
                    "slot_engine_missing",
                    "Release-slot engine is unavailable.",
                )
            temporary = tempfile.TemporaryDirectory(
                prefix="km-vms-helper-refresh-"
            )
            compose, environment, _source, manifest = slot_compose(
                app_dir,
                project_name,
                target_slot,
                engine=engine,
                override_root=Path(temporary.name),
                with_image_override=True,
            )
            helper = manifest["image_evidence"]["services"].get(
                "update-helper"
            )
            if (
                type(helper) is not dict
                or helper.get("image_id") != expected_image_id
                or helper.get("immutable_image_ref") != helper_image
            ):
                raise BridgeError(
                    "prepared_image_changed",
                    "Prepared target helper evidence changed before handoff.",
                )
        run_command(
            [
                *compose,
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--force-recreate",
                "update-helper",
            ],
            timeout=300,
            error_code="helper_recreate_failed",
            error_message="Docker Compose could not recreate update-helper.",
            env=environment,
        )
        result = run_command(
            [*compose, "ps", "-q", "update-helper"],
            timeout=30,
            error_code="helper_identity_missing",
            error_message="Recreated update-helper container identity is unavailable.",
            env=environment,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
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
    target_slot = str(getattr(args, "target_slot", None) or "") or None
    engine = load_slot_engine(bridge_source_root()) if target_slot else None
    if target_slot is not None:
        try:
            target_slot = engine.require_slot_id(target_slot, target=True)
            journal = engine.read_activation_journal(app_dir)
            active = engine.read_active_slot(app_dir)
        except Exception as exc:
            raise BridgeError(
                getattr(exc, "code", "activation_journal_invalid"),
                "Target helper handoff evidence is invalid.",
            ) from exc
        if (
            journal["request_id"] != request_id
            or journal["phase"] != "completed"
            or journal["target"]["slot_id"] != target_slot
            or active is None
            or active[0] != target_slot
        ):
            raise BridgeError(
                "helper_handoff_conflict",
                "Target helper handoff does not match completed activation.",
            )
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
        helper_lease = acquire_helper_transition_lease(control_dir)
        try:
            wait_for_terminal_status(
                control_dir / "update-status.json",
                request_id=request_id,
                timeout_seconds=timeout_seconds,
            )
        finally:
            release_helper_transition_lease(helper_lease)
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
            target_slot=target_slot,
            engine=engine,
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

    handoff_parser = subparsers.add_parser(
        "handoff",
        help="normalize source identity and schema request before overlay",
    )
    handoff_parser.add_argument("--app-dir", required=True)
    handoff_parser.add_argument("--target-source-dir", required=True)
    handoff_parser.add_argument("--request-id", required=True)
    handoff_parser.add_argument("--project-name")
    handoff_parser.add_argument("--terminal", action="store_true")
    handoff_parser.add_argument("--trusted-commit")
    handoff_parser.add_argument("--declared-version")
    handoff_parser.set_defaults(handler=handoff)

    target_parser = subparsers.add_parser(
        "prepare-target",
        help="prepare immutable target slot without activation",
    )
    target_parser.add_argument("--app-dir", required=True)
    target_parser.add_argument("--target-source-dir", required=True)
    target_parser.add_argument("--request-id", required=True)
    target_parser.add_argument("--trusted-commit", required=True)
    target_parser.add_argument("--declared-version", required=True)
    target_parser.add_argument("--project-name", required=True)
    target_parser.set_defaults(handler=prepare_trusted_target_slot)

    activate_parser = subparsers.add_parser(
        "activate-target",
        help="activate one prepared trusted target with automatic rollback",
    )
    activate_parser.add_argument("--app-dir", required=True)
    activate_parser.add_argument("--project-name", required=True)
    activate_parser.add_argument("--request-id", required=True)
    activate_parser.add_argument("--previous-slot", required=True)
    activate_parser.add_argument("--target-slot", required=True)
    activate_parser.add_argument("--target-commit", required=True)
    activate_parser.add_argument("--target-version", required=True)
    activate_parser.add_argument("--terminal", action="store_true")
    activate_parser.set_defaults(handler=activate_or_resume)

    resume_parser = subparsers.add_parser(
        "resume-activation",
        help="resume one unfinished journaled activation",
    )
    resume_parser.add_argument("--app-dir", required=True)
    resume_parser.add_argument("--project-name", required=True)
    resume_parser.add_argument("--request-id", required=True)
    resume_parser.add_argument("--terminal", action="store_true")
    resume_parser.set_defaults(
        handler=activate_or_resume,
        previous_slot=None,
        target_slot=None,
        target_commit=None,
        target_version=None,
    )

    refresh_parser = subparsers.add_parser("refresh", help="wait for completion and recreate update-helper")
    refresh_parser.add_argument("--app-dir", required=True)
    refresh_parser.add_argument("--project-name", required=True)
    refresh_parser.add_argument("--helper-image", required=True)
    refresh_parser.add_argument("--request-id", required=True)
    refresh_parser.add_argument("--expected-image-id", required=True)
    refresh_parser.add_argument("--target-slot")
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
