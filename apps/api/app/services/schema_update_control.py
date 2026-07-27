from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models.schema_migration_control import (
    SchemaMigrationAttempt,
    SchemaMigrationControl,
)
from app.models.schema_version import (
    SchemaMigrationHistory,
    SchemaVersionState,
)
from app.services.schema_migrations import (
    MIGRATION_SOURCE,
    PRODUCTION_MIGRATIONS,
    STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION,
    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION,
    STAGE660128_UNIVERSAL_SCHEMA_MIGRATION,
    migration_definition_fingerprint,
    migration_registry_fingerprint,
    preparation_definition_fingerprint,
    validate_stage660128_target_schema,
)
from app.services.schema_versioning import (
    CURRENT_BASELINE_ID,
    CURRENT_MIGRATION_ID,
    CURRENT_STATE_ID,
    SAFE_STATUSES,
)


CONTROL_ROOT = Path(os.getenv("UPDATE_CONTROL_ROOT") or "/update-control")
REQUEST_PATH = CONTROL_ROOT / "schema-update-request.json"
PRE_OVERLAY_IDENTITY_PATH = CONTROL_ROOT / "pre-overlay-source-identity.json"
UPDATE_STATUS_PATH = CONTROL_ROOT / "update-status.json"
CONTROL_BOOTSTRAP_RECEIPT_PATH = (
    CONTROL_ROOT / "schema-control-bootstrap.signed.json"
)
AUTH_PATH = CONTROL_ROOT / "schema-auth-snapshot.signed.json"
PREPARATION_RECEIPT_PATH = CONTROL_ROOT / "schema-preparation-receipt.signed.json"
RECOVERY_RECEIPT_PATH = CONTROL_ROOT / "operation-recovery-receipt.signed.json"
GATE_RECEIPT_PATH = CONTROL_ROOT / "schema-gate-receipt.signed.json"
RETRY_ADMISSION_PATH = CONTROL_ROOT / "update-retry-admission.signed.json"
RELEASE_PATH = Path(
    os.getenv("KMVMS_RELEASE_IDENTITY_FILE") or "/app/.km-vms-release.json"
)
JWT_SECRET = str(os.getenv("JWT_SECRET") or "")
MAX_CONTROL_BYTES = 64 * 1024
MAX_DETAILS_BYTES = 8 * 1024
MAX_TIMESTAMP_LENGTH = 80
TARGET_SCHEMA_VERSION = 8
REQUEST_RE = re.compile(r"^(?:update|stage609)-[0-9a-f]{32}$")
ATTEMPT_RE = re.compile(r"^migration-attempt-[0-9a-f]{32}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SAFE_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,100}$")
ALLOWED_ROLES = {"owner", "admin"}
CONTROL_BOOTSTRAP_MIGRATION_ID = "stage660128_migration_control_bootstrap_v1"
EXACT_TARGET_STATE_SOURCES = {"fresh_create_all", MIGRATION_SOURCE}
ACTIVE_UPDATE_STATUSES = frozenset(
    {
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
)
TERMINAL_UPDATE_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "blocked"}
)
INACTIVE_UPDATE_STATUSES = frozenset({"idle", "unknown"})
KNOWN_UPDATE_STATUSES = (
    ACTIVE_UPDATE_STATUSES
    | TERMINAL_UPDATE_STATUSES
    | INACTIVE_UPDATE_STATUSES
)
UNBOUND_STATUS_SENTINEL_STATUSES = frozenset(
    {"idle", "unknown", "blocked"}
)
UNBOUND_STATUS_SENTINEL_KEY_SETS = (
    frozenset({"schema_version", "status"}),
    frozenset({"schema_version", "status", "request_id"}),
)
SIGNED_AUTHORITY_PATH_NAMES = {
    "auth_snapshot": "schema-auth-snapshot.signed.json",
    "control_bootstrap_receipt": "schema-control-bootstrap.signed.json",
    "preparation_receipt": "schema-preparation-receipt.signed.json",
    "recovery_receipt": "operation-recovery-receipt.signed.json",
    "gate_receipt": "schema-gate-receipt.signed.json",
    "retry_admission": "update-retry-admission.signed.json",
}
SAFE_RETRY_REASONS = frozenset(
    {
        "test_injected_preparation_failure_before_ddl",
        "test_injected_preparation_failure_after_ddl",
        "test_injected_retryable_schema_failure",
    }
)
REQUEST_SOURCE_FIELDS = {
    "kind",
    "channel",
    "version",
    "commit",
    "apply_ref",
    "ref",
    "repo",
    "source_type",
}
LEGACY_MINIMAL_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "requested_at",
    "intent",
    "confirmed",
    "source",
}
LEGACY_HISTORICAL_REQUEST_FIELDS = LEGACY_MINIMAL_REQUEST_FIELDS | {
    "requested_by",
    "preflight_required",
    "status_path",
}
LEGACY_SNAPSHOT_REQUEST_FIELDS = LEGACY_HISTORICAL_REQUEST_FIELDS | {
    "apply_candidate",
}
LEGACY_TRANSITIONAL_REQUEST_FIELDS = LEGACY_SNAPSHOT_REQUEST_FIELDS | {
    "submission_id",
}
SCHEMA_RETRY_REQUEST_FIELDS = LEGACY_HISTORICAL_REQUEST_FIELDS | {
    "retry_of_request_id",
    "migration_attempt_id",
}
LEGACY_STATUS_VERSION_FROM_REQUEST_PROFILES = frozenset(
    {"minimal", "historical", "snapshot"}
)
CURRENT_REQUEST_FIELDS = {
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
RELEASE_IDENTITY_FIELDS = {
    "schema_version",
    "product",
    "version",
    "title",
    "summary",
    "release_channel",
    "source_kind",
    "source_repo",
    "source_ref",
    "commit_sha",
    "installed_at",
    "installed_by",
    "metadata_status",
    "metadata_source",
}


UPDATE_LINEAGE_FILENAME = "km-vms-update-lineage.json"
UPDATE_LINEAGE_MAX_BYTES = 128 * 1024


def _update_lineage_path() -> Path:
    configured = str(os.getenv("KMVMS_UPDATE_LINEAGE_FILE") or "").strip()
    if configured:
        return Path(configured)
    candidates = [
        parent / "release" / UPDATE_LINEAGE_FILENAME
        for parent in (Path.cwd(), *Path(__file__).resolve().parents)
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("update_lineage_file_missing")


def _load_update_lineage() -> dict[str, Any]:
    path = _update_lineage_path()
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size <= 1
        or info.st_size > UPDATE_LINEAGE_MAX_BYTES
    ):
        raise RuntimeError("update_lineage_file_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("update_lineage_json_invalid") from exc
    expected_keys = {
        "schema_version",
        "product",
        "tag_commits",
        "schema_versions",
        "shape_fingerprints",
        "shape_alternates",
    }
    if (
        type(payload) is not dict
        or set(payload) != expected_keys
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or payload.get("product") != "KM VMS"
    ):
        raise RuntimeError("update_lineage_contract_invalid")
    tag_commits = payload.get("tag_commits")
    schema_versions = payload.get("schema_versions")
    shape_fingerprints = payload.get("shape_fingerprints")
    shape_alternates = payload.get("shape_alternates")
    if not all(
        type(value) is dict
        for value in (
            tag_commits,
            schema_versions,
            shape_fingerprints,
            shape_alternates,
        )
    ):
        raise RuntimeError("update_lineage_maps_invalid")
    versions = list(tag_commits)
    if (
        not versions
        or len(versions) > 256
        or set(versions) != set(schema_versions)
        or set(versions) != set(shape_fingerprints)
        or not set(shape_alternates).issubset(versions)
    ):
        raise RuntimeError("update_lineage_versions_invalid")

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
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or type(schema_version) is not int
            or schema_version < 1
            or schema_version > TARGET_SCHEMA_VERSION
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


_UPDATE_LINEAGE = _load_update_lineage()
SOURCE_TAG_COMMITS: dict[str, str] = dict(_UPDATE_LINEAGE["tag_commits"])
SOURCE_SCHEMA_VERSIONS: dict[str, int] = dict(
    _UPDATE_LINEAGE["schema_versions"]
)
SOURCE_SHAPE_FINGERPRINTS: dict[str, str] = dict(
    _UPDATE_LINEAGE["shape_fingerprints"]
)
SOURCE_SHAPE_FINGERPRINT_ALTERNATES: dict[str, frozenset[str]] = {
    version: frozenset(values)
    for version, values in _UPDATE_LINEAGE["shape_alternates"].items()
}
WORKING_NAS_V0724_SOURCE_SHAPE_FINGERPRINT = next(
    iter(SOURCE_SHAPE_FINGERPRINT_ALTERNATES["0.7.24"])
)
TARGET_SHAPE_FINGERPRINT = (
    "18055105892ae40bff200d32fa6a898d"
    "18ffbde340c164d6585c3c893f4f501a"
)


CONTROL_DDL = """
CREATE TABLE schema_migration_control (
    id VARCHAR(32) PRIMARY KEY,
    fencing_generation BIGINT NOT NULL,
    owner_attempt_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(80) NOT NULL,
    installed_version VARCHAR(80) NOT NULL,
    installed_commit VARCHAR(40) NOT NULL,
    source_schema_version INTEGER NOT NULL,
    target_commit VARCHAR(40) NOT NULL,
    target_release VARCHAR(80) NOT NULL,
    target_schema_version INTEGER NOT NULL,
    registry_fingerprint VARCHAR(64) NOT NULL,
    plan_fingerprint VARCHAR(64) NOT NULL,
    source_shape_fingerprint VARCHAR(64) NOT NULL,
    control_definition_fingerprint VARCHAR(64) NOT NULL,
    state VARCHAR(20) NOT NULL,
    lease_expires_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT ck_schema_migration_control_fingerprints
        CHECK (
            char_length(registry_fingerprint)=64
            AND char_length(plan_fingerprint)=64
            AND char_length(source_shape_fingerprint)=64
            AND char_length(control_definition_fingerprint)=64
        ),
    CONSTRAINT ck_schema_migration_control_state
        CHECK (state IN ('prepared','recovering','migrating','completed','failed'))
);
CREATE TABLE schema_migration_attempts (
    attempt_id VARCHAR(64) PRIMARY KEY,
    admission_attempt_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(80) NOT NULL,
    migration_id VARCHAR(100) NOT NULL,
    previous_version INTEGER NULL,
    target_version INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP NULL,
    fencing_generation BIGINT NOT NULL,
    installed_version VARCHAR(80) NULL,
    installed_commit VARCHAR(40) NULL,
    target_release VARCHAR(80) NOT NULL,
    target_commit VARCHAR(40) NOT NULL,
    registry_fingerprint VARCHAR(64) NOT NULL,
    plan_fingerprint VARCHAR(64) NOT NULL,
    definition_fingerprint VARCHAR(64) NOT NULL,
    before_shape_fingerprint VARCHAR(64) NOT NULL,
    after_shape_fingerprint VARCHAR(64) NULL,
    failure_class VARCHAR(96) NULL,
    failure_summary VARCHAR(300) NULL,
    resumable BOOLEAN NOT NULL DEFAULT FALSE,
    details JSON NOT NULL DEFAULT '{}'::json,
    CONSTRAINT ck_schema_migration_attempt_status
        CHECK (status IN ('started','applied','failed','blocked','interrupted')),
    CONSTRAINT ck_schema_migration_attempt_fingerprints
        CHECK (
            char_length(registry_fingerprint)=64
            AND char_length(plan_fingerprint)=64
            AND char_length(definition_fingerprint)=64
            AND char_length(before_shape_fingerprint)=64
            AND (
                after_shape_fingerprint IS NULL
                OR char_length(after_shape_fingerprint)=64
            )
        )
);
CREATE INDEX ix_schema_migration_attempt_request
    ON schema_migration_attempts (request_id, started_at);
CREATE INDEX ix_schema_migration_attempt_status
    ON schema_migration_attempts (status, started_at);
CREATE UNIQUE INDEX uq_schema_migration_attempt_applied_lineage
    ON schema_migration_attempts (migration_id, definition_fingerprint)
    WHERE status='applied';
"""

CONTROL_DEFINITION_FINGERPRINT = hashlib.sha256(
    re.sub(r"\s+", " ", CONTROL_DDL).strip().encode("utf-8")
).hexdigest()
REGISTRY_FINGERPRINT = migration_registry_fingerprint()


class SchemaControlError(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        subtype: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.subtype = subtype


def stable_failure_reason(
    exc: BaseException,
    fallback: str,
) -> str:
    value = (
        str(exc)
        if isinstance(exc, SchemaControlError)
        else getattr(exc, "status", "")
    )
    if (
        type(value) is str
        and re.fullmatch(r"[a-z][a-z0-9_]{0,119}", value)
    ):
        return value
    return fallback


@dataclass(frozen=True)
class UpdateContext:
    request: dict[str, Any]
    request_id: str
    admission_attempt_id: str
    target_release: str
    target_commit: str
    installed_version: str
    installed_commit: str
    source_schema_version: int
    source_shape_fingerprint: str
    registry_fingerprint: str
    plan_fingerprint: str


@dataclass(frozen=True)
class ExecutionStatusObservation:
    payload: dict[str, Any] | None
    invalid_subtype: str | None = None


@dataclass(frozen=True)
class RetryClassification:
    retryable: bool
    public_state: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def bounded_details(payload: dict[str, Any]) -> str:
    rendered = canonical_bytes(payload)
    if len(rendered) > MAX_DETAILS_BYTES:
        raise SchemaControlError("migration_attempt_details_too_large")
    return rendered.decode("utf-8")


def load_json_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) <= 1 or len(raw) > MAX_CONTROL_BYTES:
        raise SchemaControlError("json_size_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SchemaControlError("json_invalid") from exc
    if type(payload) is not dict:
        raise SchemaControlError("json_object_required")
    return payload


def parse_utc(value: Any) -> float:
    if type(value) is not str:
        raise SchemaControlError("timestamp_type_invalid")
    if (
        not value
        or len(value) > MAX_TIMESTAMP_LENGTH
        or "T" not in value
        or not (
            value.endswith("Z")
            or value.endswith("+00:00")
        )
    ):
        raise SchemaControlError("timestamp_format_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaControlError("timestamp_invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
    ):
        raise SchemaControlError("timestamp_timezone_invalid")
    return parsed.timestamp()


def exact_string(
    value: Any,
    *,
    code: str,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    if (
        type(value) is not str
        or len(value) > max_length
        or (not allow_empty and not value)
    ):
        raise SchemaControlError(code)
    return value


def _validate_request_source(
    value: Any,
    *,
    minimal_legacy: bool,
) -> dict[str, Any]:
    expected = {"version", "commit"} if minimal_legacy else REQUEST_SOURCE_FIELDS
    if type(value) is not dict or set(value) != expected:
        raise SchemaControlError("request_source_fields_invalid")
    version = exact_string(
        value.get("version"),
        code="request_source_version_invalid",
        max_length=80,
    )
    commit = exact_string(
        value.get("commit"),
        code="request_target_commit_invalid",
        max_length=40,
    )
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}", version)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", commit)
    ):
        raise SchemaControlError("request_source_identity_invalid")
    if minimal_legacy:
        return value
    for key, max_length in (
        ("kind", 40),
        ("channel", 80),
        ("apply_ref", 40),
        ("ref", 120),
        ("repo", 160),
        ("source_type", 40),
    ):
        exact_string(
            value.get(key),
            code="request_source_identity_invalid",
            max_length=max_length,
        )
    if (
        value["kind"] != "trusted_manifest"
        or value["source_type"] != "github_tarball"
        or value["repo"].lower() != "kmishnev87/km-vms"
        or not COMMIT_RE.fullmatch(value["apply_ref"])
        or value["apply_ref"].lower() != commit.lower()
        or ".." in value["ref"]
        or "@{" in value["ref"]
        or value["ref"].endswith(".")
    ):
        raise SchemaControlError("request_source_identity_invalid")
    return value


def _validate_apply_candidate(value: Any) -> None:
    if type(value) is not dict or set(value) != {"source", "snapshot"}:
        raise SchemaControlError("request_apply_candidate_invalid")
    if value.get("source") not in {"trusted_snapshot", "live_check"}:
        raise SchemaControlError("request_apply_candidate_invalid")
    snapshot = value.get("snapshot")
    if type(snapshot) is not dict:
        raise SchemaControlError("request_apply_candidate_invalid")
    compact = {"available", "fresh", "age_seconds", "fresh_for_seconds"}
    full = compact | {"version", "commit_short", "provider"}
    snapshot_fields = frozenset(snapshot)
    if snapshot_fields not in {frozenset(compact), frozenset(full)}:
        raise SchemaControlError("request_apply_candidate_invalid")
    if (
        type(snapshot.get("available")) is not bool
        or type(snapshot.get("fresh")) is not bool
        or (
            snapshot.get("age_seconds") is not None
            and (
                type(snapshot["age_seconds"]) is not int
                or snapshot["age_seconds"] < 0
                or snapshot["age_seconds"] > 315_360_000
            )
        )
        or type(snapshot.get("fresh_for_seconds")) is not int
        or snapshot["fresh_for_seconds"] < 0
        or snapshot["fresh_for_seconds"] > 86_400
    ):
        raise SchemaControlError("request_apply_candidate_invalid")
    if snapshot_fields == frozenset(compact):
        if (
            value["source"] != "live_check"
            or snapshot["available"] is not False
        ):
            raise SchemaControlError("request_apply_candidate_invalid")
        return
    for key, max_length in (
        ("version", 80),
        ("commit_short", 12),
        ("provider", 80),
    ):
        item = snapshot.get(key)
        if item is not None:
            exact_string(
                item,
                code="request_apply_candidate_invalid",
                max_length=max_length,
            )


def _validate_requested_by(
    value: Any,
    *,
    current: bool,
) -> None:
    expected = (
        {"user_id", "username", "role", "ip_address", "user_agent"}
        if current
        else {"user_id", "role"}
    )
    if type(value) is not dict or set(value) != expected:
        raise SchemaControlError("request_actor_fields_invalid")
    user_id = value.get("user_id")
    if current:
        if (
            type(user_id) is not int
            or user_id <= 0
            or user_id > 9_223_372_036_854_775_807
        ):
            raise SchemaControlError("request_actor_id_invalid")
        username = exact_string(
            value.get("username"),
            code="request_actor_identity_invalid",
            max_length=100,
        )
        if not SAFE_SUBJECT_RE.fullmatch(username):
            raise SchemaControlError("request_actor_identity_invalid")
        for key, max_length in (("ip_address", 100), ("user_agent", 300)):
            item = value.get(key)
            if item is not None:
                exact_string(
                    item,
                    code="request_actor_identity_invalid",
                    max_length=max_length,
                    allow_empty=True,
                )
    else:
        if not (
            type(user_id) is int
            and 0 < user_id <= 9_223_372_036_854_775_807
        ) and not (
            type(user_id) is str
            and 0 < len(user_id) <= 20
            and user_id.isdigit()
            and int(user_id) > 0
        ):
            raise SchemaControlError("request_actor_id_invalid")
    role = exact_string(
        value.get("role"),
        code="request_actor_role_invalid",
        max_length=50,
    )
    if role not in ALLOWED_ROLES:
        raise SchemaControlError("request_actor_role_invalid")


def validate_update_request(request: dict[str, Any]) -> str:
    if type(request) is not dict:
        raise SchemaControlError("request_object_invalid")
    schema_version = request.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise SchemaControlError("request_schema_version_invalid")
    keys = set(request)
    if schema_version == 2:
        if keys != CURRENT_REQUEST_FIELDS:
            raise SchemaControlError("request_fields_invalid")
        profile = "current"
    elif keys == LEGACY_MINIMAL_REQUEST_FIELDS:
        profile = "minimal"
    elif keys == LEGACY_HISTORICAL_REQUEST_FIELDS:
        profile = "historical"
    elif keys == LEGACY_SNAPSHOT_REQUEST_FIELDS:
        profile = "snapshot"
    elif keys == LEGACY_TRANSITIONAL_REQUEST_FIELDS:
        profile = "transitional"
    elif keys == SCHEMA_RETRY_REQUEST_FIELDS:
        profile = "schema_retry"
    else:
        raise SchemaControlError("request_fields_invalid")

    request_id = exact_string(
        request.get("request_id"),
        code="request_id_invalid",
        max_length=80,
    )
    if not REQUEST_RE.fullmatch(request_id):
        raise SchemaControlError("request_id_invalid")
    if profile == "current" and not request_id.startswith("update-"):
        raise SchemaControlError("request_id_invalid")
    if (
        request.get("intent") != "apply_update"
        or request.get("confirmed") is not True
    ):
        raise SchemaControlError("request_intent_invalid")
    parse_utc(request.get("requested_at"))
    _validate_request_source(
        request.get("source"),
        minimal_legacy=profile == "minimal",
    )
    if profile != "minimal":
        if (
            request.get("preflight_required") is not True
            or request.get("status_path")
            != "data/update-control/update-status.json"
        ):
            raise SchemaControlError("request_preflight_contract_invalid")
        actor_value = request.get("requested_by")
        current_retry_actor = (
            profile == "schema_retry"
            and type(actor_value) is dict
            and set(actor_value)
            == {
                "user_id",
                "username",
                "role",
                "ip_address",
                "user_agent",
            }
        )
        _validate_requested_by(
            actor_value,
            current=profile == "current" or current_retry_actor,
        )
    if profile in {"current", "snapshot", "transitional"}:
        _validate_apply_candidate(request.get("apply_candidate"))
    if profile in {"current", "transitional"}:
        submission_id = exact_string(
            request.get("submission_id"),
            code="request_submission_id_invalid",
            max_length=36,
        )
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
            r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
            submission_id,
        ):
            raise SchemaControlError("request_submission_id_invalid")
    if profile == "schema_retry":
        retry_of = exact_string(
            request.get("retry_of_request_id"),
            code="retry_request_identity_invalid",
            max_length=80,
        )
        attempt_id = exact_string(
            request.get("migration_attempt_id"),
            code="retry_request_identity_invalid",
            max_length=64,
        )
        if (
            not REQUEST_RE.fullmatch(retry_of)
            or retry_of == request_id
            or not ATTEMPT_RE.fullmatch(attempt_id)
            or not request_id.startswith("update-")
        ):
            raise SchemaControlError("retry_request_identity_invalid")
    return profile


def validate_terminal_update_request(request: dict[str, Any]) -> str:
    """Validate completed evidence, including one historical actor form.

    A schema-v1 historical request written by the pre-v2 updater could contain
    the five-field actor snapshot while retaining a string user id. That form
    is accepted only after the resolver binds it to a completed control row;
    active update admission remains strict.
    """
    try:
        return validate_update_request(request)
    except SchemaControlError as strict_error:
        if (
            type(request) is not dict
            or request.get("schema_version") != 1
            or set(request) != LEGACY_HISTORICAL_REQUEST_FIELDS
        ):
            raise strict_error
        actor = request.get("requested_by")
        current_actor_fields = {
            "user_id",
            "username",
            "role",
            "ip_address",
            "user_agent",
        }
        if type(actor) is not dict or set(actor) != current_actor_fields:
            raise strict_error
        raw_user_id = actor.get("user_id")
        if (
            type(raw_user_id) is int
            and 0 < raw_user_id <= 9_223_372_036_854_775_807
        ):
            normalized_user_id = raw_user_id
        elif (
            type(raw_user_id) is str
            and 0 < len(raw_user_id) <= 20
            and raw_user_id.isdigit()
            and int(raw_user_id) > 0
        ):
            normalized_user_id = int(raw_user_id)
        else:
            raise strict_error
        normalized_actor = dict(actor)
        normalized_actor["user_id"] = normalized_user_id
        _validate_requested_by(normalized_actor, current=True)

        legacy_request = dict(request)
        legacy_request["requested_by"] = {
            "user_id": raw_user_id,
            "role": actor.get("role"),
        }
        return validate_update_request(legacy_request)


def read_regular_json(
    path: Path,
    *,
    required: bool = True,
) -> dict[str, Any] | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not required:
            return None
        raise SchemaControlError(f"{path.name}_missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SchemaControlError(f"{path.name}_not_regular")
    if info.st_size <= 1 or info.st_size > MAX_CONTROL_BYTES:
        raise SchemaControlError(f"{path.name}_size_invalid")
    with path.open("rb") as handle:
        raw = handle.read(MAX_CONTROL_BYTES + 1)
    if len(raw) > MAX_CONTROL_BYTES:
        raise SchemaControlError(f"{path.name}_too_large")
    try:
        return load_json_bytes(raw)
    except SchemaControlError as exc:
        raise SchemaControlError(
            f"{path.name}_{str(exc)}"
        ) from exc


def control_key() -> bytes:
    if len(JWT_SECRET) < 16:
        raise SchemaControlError("control_secret_unavailable")
    return hmac.new(
        JWT_SECRET.encode("utf-8"),
        b"KMVMS|stage660128|failure-control|v1",
        hashlib.sha256,
    ).digest()


def sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    signature = hmac.new(
        control_key(),
        canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    return {
        "schema_version": 1,
        "payload": payload,
        "signature": signature,
    }


def read_signed(
    path: Path,
    *,
    required: bool = True,
) -> dict[str, Any] | None:
    envelope = read_regular_json(path, required=required)
    if envelope is None:
        return None
    if set(envelope) != {"schema_version", "payload", "signature"}:
        raise SchemaControlError(f"{path.name}_envelope_fields_invalid")
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if (
        type(envelope.get("schema_version")) is not int
        or envelope["schema_version"] != 1
        or type(payload) is not dict
        or type(signature) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        raise SchemaControlError(f"{path.name}_envelope_invalid")
    expected = hmac.new(
        control_key(),
        canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise SchemaControlError(f"{path.name}_signature_invalid")
    return payload


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    rendered = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(rendered) > MAX_CONTROL_BYTES:
        raise SchemaControlError("control_output_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = os.lstat(path.parent)
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise SchemaControlError("control_root_invalid")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_signed(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, sign_payload(payload))


def _normalize_sql(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _constraint_payload(inspector: Any, table_name: str) -> dict[str, Any]:
    primary = inspector.get_pk_constraint(table_name, schema="public") or {}
    unique = [
        {
            "name": str(item.get("name") or ""),
            "columns": sorted(
                str(column) for column in item.get("column_names") or []
            ),
        }
        for item in inspector.get_unique_constraints(table_name, schema="public")
    ]
    foreign = [
        {
            "name": str(item.get("name") or ""),
            "columns": [str(column) for column in item.get("constrained_columns") or []],
            "referred_schema": str(item.get("referred_schema") or ""),
            "referred_table": str(item.get("referred_table") or ""),
            "referred_columns": [
                str(column) for column in item.get("referred_columns") or []
            ],
            "options": {
                str(key): str(value)
                for key, value in sorted((item.get("options") or {}).items())
            },
        }
        for item in inspector.get_foreign_keys(table_name, schema="public")
    ]
    checks = [
        {
            "name": str(item.get("name") or ""),
            "sql": _normalize_sql(item.get("sqltext")),
        }
        for item in inspector.get_check_constraints(table_name, schema="public")
    ]
    return {
        "primary": {
            "name": str(primary.get("name") or ""),
            "columns": [
                str(column) for column in primary.get("constrained_columns") or []
            ],
        },
        "unique": sorted(unique, key=canonical_bytes),
        "foreign": sorted(foreign, key=canonical_bytes),
        "checks": sorted(checks, key=canonical_bytes),
    }


def database_shape_payload(
    db: Session,
    *,
    exclude_tables: Iterable[str] = (),
) -> dict[str, Any]:
    inspector = inspect(db.connection())
    excluded = set(exclude_tables)
    tables: list[dict[str, Any]] = []
    for table_name in sorted(
        set(inspector.get_table_names(schema="public")) - excluded
    ):
        columns = [
            {
                "name": str(column.get("name") or ""),
                "type": str(column.get("type") or "").upper(),
                "nullable": bool(column.get("nullable")),
                "default": _normalize_sql(column.get("default")),
            }
            for column in inspector.get_columns(table_name, schema="public")
        ]
        indexes = [
            {
                "name": str(index.get("name") or ""),
                "columns": [
                    str(column) for column in index.get("column_names") or []
                ],
                "unique": bool(index.get("unique")),
                "predicate": _normalize_sql(
                    (index.get("dialect_options") or {}).get(
                        "postgresql_where"
                    )
                ),
            }
            for index in inspector.get_indexes(table_name, schema="public")
        ]
        tables.append(
            {
                "table": table_name,
                "columns": sorted(columns, key=lambda item: item["name"]),
                "indexes": sorted(indexes, key=lambda item: item["name"]),
                "constraints": _constraint_payload(inspector, table_name),
            }
        )
    return {"schema_version": 1, "tables": tables}


def database_shape_fingerprint(
    db: Session,
    *,
    exclude_tables: Iterable[str] = (),
) -> str:
    return hashlib.sha256(
        canonical_bytes(
            database_shape_payload(db, exclude_tables=exclude_tables)
        )
    ).hexdigest()


def current_schema_version(db: Session) -> int | None:
    exists = db.execute(
        text(
            "SELECT to_regclass('public.schema_version_state') "
            "IS NOT NULL"
        )
    ).scalar_one()
    if not exists:
        return None
    value = db.execute(
        text(
            "SELECT schema_version FROM schema_version_state "
            "WHERE id='current'"
        )
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def _regular_file_presence(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SchemaControlError(
            f"schema_authority_{path.name}_not_regular"
        )
    return True


def schema_execution_authority_presence() -> dict[str, bool]:
    return {
        "request": _regular_file_presence(REQUEST_PATH),
        "pre_overlay_identity": _regular_file_presence(
            PRE_OVERLAY_IDENTITY_PATH
        ),
        "auth_snapshot": _regular_file_presence(AUTH_PATH),
        "control_bootstrap_receipt": _regular_file_presence(
            CONTROL_BOOTSTRAP_RECEIPT_PATH
        ),
        "preparation_receipt": _regular_file_presence(
            PREPARATION_RECEIPT_PATH
        ),
        "recovery_receipt": _regular_file_presence(
            RECOVERY_RECEIPT_PATH
        ),
        "gate_receipt": _regular_file_presence(GATE_RECEIPT_PATH),
        "retry_admission": _regular_file_presence(
            RETRY_ADMISSION_PATH
        ),
    }


def database_is_empty(db: Session) -> bool:
    inspector = inspect(db.connection())
    return not inspector.get_table_names(schema="public")


def is_empty_fresh_install(db: Session) -> bool:
    authority = schema_execution_authority_presence()
    return not any(authority.values()) and database_is_empty(db)


def canonical_schema_history_fingerprint(db: Session) -> str:
    rows = (
        db.query(SchemaMigrationHistory)
        .order_by(
            SchemaMigrationHistory.id.asc(),
            SchemaMigrationHistory.migration_id.asc(),
        )
        .all()
    )
    payload = [
        {
            "id": int(row.id),
            "migration_id": row.migration_id,
            "previous_version": row.previous_version,
            "target_version": row.target_version,
            "schema_version": row.schema_version,
            "baseline_id": row.baseline_id,
            "app_version": row.app_version,
            "app_build_version": row.app_build_version,
            "applied_at": (
                row.applied_at.isoformat()
                if row.applied_at is not None
                else None
            ),
            "status": row.status,
            "checksum": row.checksum,
            "source": row.source,
            "service_name": row.service_name,
            "details": row.details,
            "error_summary": row.error_summary,
            "created_at": (
                row.created_at.isoformat()
                if row.created_at is not None
                else None
            ),
        }
        for row in rows
    ]
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _validate_fresh_target_history(
    histories: list[SchemaMigrationHistory],
    state: SchemaVersionState,
    controls: list[SchemaMigrationControl],
    attempts: list[SchemaMigrationAttempt],
) -> None:
    if controls or attempts:
        raise SchemaControlError(
            "no_active_fresh_target_control_state_present"
        )
    if len(histories) != 1:
        raise SchemaControlError(
            "no_active_fresh_target_history_count_invalid"
        )
    row = histories[0]
    if (
        row.migration_id != CURRENT_MIGRATION_ID
        or row.previous_version is not None
        or row.target_version != TARGET_SCHEMA_VERSION
        or row.schema_version != TARGET_SCHEMA_VERSION
        or row.baseline_id != CURRENT_BASELINE_ID
        or row.status != state.status
        or row.source != "fresh_create_all"
        or row.error_summary is not None
    ):
        raise SchemaControlError(
            "no_active_fresh_target_history_invalid"
        )


def _known_history_migrations() -> dict[str, Any]:
    migrations = {
        migration.migration_id: migration
        for migration in PRODUCTION_MIGRATIONS.migrations
    }
    migrations[
        STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION.migration_id
    ] = STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION
    return migrations


def _validate_terminal_control(
    controls: list[SchemaMigrationControl],
) -> SchemaMigrationControl:
    if len(controls) != 1 or controls[0].id != CURRENT_STATE_ID:
        raise SchemaControlError(
            "no_active_terminal_control_count_invalid"
        )
    row = controls[0]
    expected_commit = SOURCE_TAG_COMMITS.get(row.installed_version)
    expected_schema = SOURCE_SCHEMA_VERSIONS.get(row.installed_version)
    expected_shapes = set(
        SOURCE_SHAPE_FINGERPRINT_ALTERNATES.get(
            row.installed_version,
            frozenset(),
        )
    )
    primary_shape = SOURCE_SHAPE_FINGERPRINTS.get(
        row.installed_version
    )
    if primary_shape:
        expected_shapes.add(primary_shape)
    expected_plan = plan_fingerprint(
        installed_version=row.installed_version,
        installed_commit=row.installed_commit.lower(),
        source_schema_version=row.source_schema_version,
        source_shape_fingerprint=row.source_shape_fingerprint,
        target_release=row.target_release,
        target_commit=row.target_commit.lower(),
    )
    if (
        row.state != "completed"
        or row.target_schema_version != TARGET_SCHEMA_VERSION
        or row.registry_fingerprint != REGISTRY_FINGERPRINT
        or row.control_definition_fingerprint
        != CONTROL_DEFINITION_FINGERPRINT
        or expected_commit is None
        or row.installed_commit.lower() != expected_commit
        or expected_schema != row.source_schema_version
        or row.source_shape_fingerprint not in expected_shapes
        or row.plan_fingerprint != expected_plan
        or not REQUEST_RE.fullmatch(row.request_id)
        or not ATTEMPT_RE.fullmatch(row.owner_attempt_id)
        or not COMMIT_RE.fullmatch(row.target_commit)
        or row.fencing_generation < 1
    ):
        raise SchemaControlError(
            "no_active_terminal_control_invalid"
        )
    return row


def _validate_migrated_target_history(
    histories: list[SchemaMigrationHistory],
    control: SchemaMigrationControl,
    attempts: list[SchemaMigrationAttempt],
) -> None:
    seen: set[tuple[str, str]] = set()
    known = _known_history_migrations()
    baseline_count = 0
    observed_runner: set[str] = set()
    preparation_count = 0
    for row in histories:
        identity = (row.migration_id, row.source)
        if identity in seen:
            raise SchemaControlError(
                "no_active_target_history_duplicate"
            )
        seen.add(identity)
        if (
            row.baseline_id != CURRENT_BASELINE_ID
            or row.error_summary is not None
        ):
            raise SchemaControlError(
                "no_active_target_history_metadata_invalid"
            )
        if row.source != MIGRATION_SOURCE:
            baseline_count += 1
            if (
                baseline_count != 1
                or not re.fullmatch(
                    r"chapter06_stage4_baseline_schema_v[1-8]",
                    row.migration_id,
                )
                or row.previous_version is not None
                or row.target_version != row.schema_version
                or row.status not in SAFE_STATUSES
                or row.source
                not in {"fresh_create_all", "adopted_existing_db"}
            ):
                raise SchemaControlError(
                    "no_active_target_baseline_history_invalid"
                )
            continue
        if (
            row.migration_id
            == STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id
        ):
            preparation_count += 1
            if (
                preparation_count != 1
                or row.status != "applied"
                or row.previous_version not in {5, 6, 7}
                or row.target_version != row.previous_version
                or row.schema_version != row.previous_version
                or row.checksum
                != preparation_definition_fingerprint(
                    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
                )
            ):
                raise SchemaControlError(
                    "no_active_target_preparation_history_invalid"
                )
            continue
        migration = known.get(row.migration_id)
        if (
            migration is None
            or row.status != "applied"
            or row.previous_version != migration.from_version
            or row.target_version != migration.to_version
            or row.schema_version != migration.to_version
        ):
            raise SchemaControlError(
                "no_active_target_migration_history_invalid"
            )
        if (
            migration.to_version > control.source_schema_version
            and row.checksum
            != migration_definition_fingerprint(migration)
        ):
            raise SchemaControlError(
                "no_active_target_migration_checksum_invalid"
            )
        if (
            migration.to_version <= control.source_schema_version
            and row.checksum is not None
            and row.checksum
            != migration_definition_fingerprint(migration)
        ):
            raise SchemaControlError(
                "no_active_legacy_migration_checksum_invalid"
            )
        observed_runner.add(row.migration_id)
    if baseline_count != 1:
        raise SchemaControlError(
            "no_active_target_baseline_history_missing"
        )
    expected_path = PRODUCTION_MIGRATIONS.path(
        control.source_schema_version,
        TARGET_SCHEMA_VERSION,
    )
    expected_ids = {
        migration.migration_id for migration in expected_path
    }
    if (
        len(expected_ids) != len(expected_path)
        or not expected_ids.issubset(observed_runner)
        or STAGE660128_UNIVERSAL_SCHEMA_MIGRATION.migration_id
        not in observed_runner
    ):
        raise SchemaControlError(
            "no_active_target_history_lineage_incomplete"
        )

    applied_by_lineage = {
        (attempt.migration_id, attempt.definition_fingerprint)
        for attempt in attempts
        if attempt.status == "applied"
    }
    current_generation = int(control.fencing_generation)
    historical_groups: dict[
        tuple[str, str, str, str, str, str],
        list[SchemaMigrationAttempt],
    ] = {}
    for attempt in attempts:
        if attempt.migration_id == CONTROL_BOOTSTRAP_MIGRATION_ID:
            expected_definition = CONTROL_DEFINITION_FINGERPRINT
        elif (
            attempt.migration_id
            == STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id
        ):
            expected_definition = preparation_definition_fingerprint(
                STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
            )
        else:
            migration = known.get(attempt.migration_id)
            expected_definition = (
                migration_definition_fingerprint(migration)
                if migration is not None
                else None
            )
        if (
            attempt.status in {"started", "interrupted"}
            or not ATTEMPT_RE.fullmatch(attempt.attempt_id)
            or not ATTEMPT_RE.fullmatch(attempt.admission_attempt_id)
            or not REQUEST_RE.fullmatch(attempt.request_id)
            or attempt.fencing_generation < 1
            or attempt.fencing_generation > current_generation
            or expected_definition is None
            or attempt.definition_fingerprint != expected_definition
        ):
            raise SchemaControlError(
                "no_active_target_attempt_ambiguous"
            )
        if attempt.fencing_generation == current_generation:
            if (
                attempt.admission_attempt_id
                != control.owner_attempt_id
                or attempt.request_id != control.request_id
                or attempt.target_release != control.target_release
                or attempt.target_commit.lower()
                != control.target_commit.lower()
                or attempt.registry_fingerprint
                != control.registry_fingerprint
                or attempt.plan_fingerprint != control.plan_fingerprint
            ):
                raise SchemaControlError(
                    "no_active_target_attempt_binding_invalid"
                )
        else:
            expected_installed_commit = SOURCE_TAG_COMMITS.get(
                str(attempt.installed_version or "")
            )
            if (
                expected_installed_commit is None
                or attempt.installed_commit != expected_installed_commit
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}",
                    str(attempt.target_release or ""),
                )
                or not COMMIT_RE.fullmatch(
                    str(attempt.target_commit or "")
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(attempt.registry_fingerprint or ""),
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(attempt.plan_fingerprint or ""),
                )
            ):
                raise SchemaControlError(
                    "no_active_historical_attempt_lineage_invalid"
                )
            binding = (
                str(attempt.installed_version),
                str(attempt.installed_commit),
                str(attempt.target_release),
                str(attempt.target_commit).lower(),
                str(attempt.registry_fingerprint),
                str(attempt.plan_fingerprint),
            )
            historical_groups.setdefault(binding, []).append(attempt)
        if attempt.status == "applied":
            if (
                attempt.completed_at is None
                or attempt.after_shape_fingerprint is None
                or attempt.failure_class is not None
                or attempt.failure_summary is not None
                or bool(attempt.resumable)
            ):
                raise SchemaControlError(
                    "no_active_target_applied_attempt_invalid"
                )
        elif (
            attempt.status in {"failed", "blocked"}
            and (
                attempt.migration_id,
                attempt.definition_fingerprint,
            )
            not in applied_by_lineage
        ):
            raise SchemaControlError(
                "no_active_target_terminal_attempt_unresolved"
            )
    for binding, group in historical_groups.items():
        (
            installed_version,
            installed_commit,
            target_release,
            target_commit,
            registry_fingerprint,
            stored_plan,
        ) = binding
        source_schema = SOURCE_SCHEMA_VERSIONS[installed_version]
        source_shapes = {
            SOURCE_SHAPE_FINGERPRINTS[installed_version],
            *SOURCE_SHAPE_FINGERPRINT_ALTERNATES.get(
                installed_version,
                frozenset(),
            ),
        }
        source_attempts = [
            attempt
            for attempt in group
            if attempt.previous_version == source_schema
            and attempt.before_shape_fingerprint in source_shapes
        ]
        if not source_attempts:
            raise SchemaControlError(
                "no_active_historical_attempt_plan_unverifiable"
            )
        expected_plans = {
            plan_fingerprint(
                installed_version=installed_version,
                installed_commit=installed_commit,
                source_schema_version=source_schema,
                source_shape_fingerprint=attempt.before_shape_fingerprint,
                target_release=target_release,
                target_commit=target_commit,
            )
            for attempt in source_attempts
        }
        if (
            registry_fingerprint != REGISTRY_FINGERPRINT
            or expected_plans != {stored_plan}
        ):
            raise SchemaControlError(
                "no_active_historical_attempt_plan_invalid"
            )
    for migration in expected_path:
        lineage = (
            migration.migration_id,
            migration_definition_fingerprint(migration),
        )
        if lineage not in applied_by_lineage:
            raise SchemaControlError(
                "no_active_target_attempt_lineage_incomplete"
            )


def validate_exact_target_noop(db: Session) -> None:
    inspector = inspect(db.connection())
    tables = set(inspector.get_table_names(schema="public"))
    required = {"schema_version_state", "schema_migration_history"}
    if not required.issubset(tables):
        raise SchemaControlError(
            "no_active_schema_metadata_missing"
        )
    states = db.query(SchemaVersionState).all()
    if len(states) != 1 or states[0].id != CURRENT_STATE_ID:
        raise SchemaControlError(
            "no_active_schema_current_row_invalid"
        )
    state = states[0]
    if state.schema_version < TARGET_SCHEMA_VERSION:
        raise SchemaControlError("no_active_schema_below_target")
    if state.schema_version > TARGET_SCHEMA_VERSION:
        raise SchemaControlError("no_active_schema_future_target")
    if (
        state.baseline_id != CURRENT_BASELINE_ID
        or state.status not in SAFE_STATUSES
        or state.source not in EXACT_TARGET_STATE_SOURCES
    ):
        raise SchemaControlError(
            "no_active_schema_state_invalid"
        )

    has_control = "schema_migration_control" in tables
    has_attempts = "schema_migration_attempts" in tables
    if has_control != has_attempts:
        raise SchemaControlError(
            "no_active_migration_control_partial_shape"
        )
    controls = (
        db.query(SchemaMigrationControl).all()
        if has_control
        else []
    )
    attempts = (
        db.query(SchemaMigrationAttempt).all()
        if has_attempts
        else []
    )
    histories = (
        db.query(SchemaMigrationHistory)
        .order_by(SchemaMigrationHistory.id.asc())
        .all()
    )
    if state.source == "fresh_create_all":
        _validate_fresh_target_history(
            histories,
            state,
            controls,
            attempts,
        )
    else:
        control = _validate_terminal_control(controls)
        _validate_migrated_target_history(
            histories,
            control,
            attempts,
        )
    try:
        validate_stage660128_target_schema(db)
    except Exception as exc:
        raise SchemaControlError(
            "no_active_target_semantic_validation_failed"
        ) from exc
    exact, _fingerprint = target_shape_is_exact(db)
    if not exact:
        raise SchemaControlError(
            "no_active_target_shape_fingerprint_mismatch"
        )


def _schema_control_row_for_execution(
    db: Session,
) -> SchemaMigrationControl | None:
    inspector = inspect(db.connection())
    has_control = inspector.has_table("schema_migration_control")
    has_attempts = inspector.has_table("schema_migration_attempts")
    if has_control != has_attempts:
        raise SchemaControlError(
            "schema_execution_migration_control_partial_shape"
        )
    if not has_control:
        return None
    rows = db.query(SchemaMigrationControl).all()
    if not rows:
        return None
    if len(rows) != 1 or rows[0].id != CURRENT_STATE_ID:
        raise SchemaControlError(
            "schema_execution_migration_control_count_invalid"
        )
    if rows[0].state not in {
        "prepared",
        "recovering",
        "migrating",
        "completed",
        "failed",
    }:
        raise SchemaControlError(
            "schema_execution_migration_control_state_invalid"
        )
    return rows[0]


def _read_update_status_for_execution() -> dict[str, Any] | None:
    try:
        info = os.lstat(UPDATE_STATUS_PATH)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise SchemaControlError(
            "schema_execution_status_invalid",
            subtype="symlink",
        )
    if not stat.S_ISREG(info.st_mode):
        raise SchemaControlError(
            "schema_execution_status_invalid",
            subtype="non_regular",
        )
    if info.st_size <= 1 or info.st_size > MAX_CONTROL_BYTES:
        raise SchemaControlError(
            "schema_execution_status_invalid",
            subtype="malformed_json",
        )
    with UPDATE_STATUS_PATH.open("rb") as handle:
        raw = handle.read(MAX_CONTROL_BYTES + 1)
    if len(raw) > MAX_CONTROL_BYTES:
        raise SchemaControlError(
            "schema_execution_status_invalid",
            subtype="malformed_json",
        )
    try:
        payload = load_json_bytes(raw)
    except SchemaControlError as exc:
        raise SchemaControlError(
            "schema_execution_status_invalid",
            subtype="malformed_json",
        ) from exc
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise SchemaControlError(
            "schema_execution_status_invalid",
            subtype="schema_invalid",
        )
    status = payload.get("status")
    if (
        type(status) is not str
        or not status
        or len(status) > 40
        or status not in KNOWN_UPDATE_STATUSES
    ):
        raise SchemaControlError(
            "schema_execution_status_invalid",
            subtype="status_unrecognized",
        )
    request_id = payload.get("request_id")
    if request_id is not None and (
        type(request_id) is not str
        or not REQUEST_RE.fullmatch(request_id)
    ):
        raise SchemaControlError(
            "schema_execution_status_invalid",
            subtype="request_invalid",
        )
    return payload


def _observe_update_status_for_execution() -> ExecutionStatusObservation:
    try:
        return ExecutionStatusObservation(
            payload=_read_update_status_for_execution(),
        )
    except SchemaControlError as exc:
        subtype = getattr(exc, "subtype", None)
        if subtype not in {
            "non_regular",
            "symlink",
            "malformed_json",
            "schema_invalid",
            "status_unrecognized",
            "request_invalid",
        }:
            subtype = "malformed_json"
        return ExecutionStatusObservation(
            payload=None,
            invalid_subtype=subtype,
        )


def _raise_status_observation_error(
    observation: ExecutionStatusObservation,
    *,
    pair_state: str,
) -> None:
    if observation.invalid_subtype is None:
        return
    reason = "schema_execution_status_invalid"
    if pair_state == "complete":
        reason = (
            "schema_execution_status_invalid_with_complete_authority_pair"
        )
    elif pair_state == "partial":
        reason = (
            "schema_execution_status_invalid_with_partial_authority_pair"
        )
    raise SchemaControlError(
        reason,
        subtype=observation.invalid_subtype,
    )


def _is_exact_unbound_status_sentinel(
    payload: dict[str, Any],
) -> bool:
    return bool(
        frozenset(payload) in UNBOUND_STATUS_SENTINEL_KEY_SETS
        and type(payload.get("schema_version")) is int
        and payload["schema_version"] == 1
        and payload.get("status") in UNBOUND_STATUS_SENTINEL_STATUSES
        and payload.get("request_id") is None
    )


def _status_has_operation_authority(
    payload: dict[str, Any],
) -> bool:
    return bool(
        payload.get("status") == "unknown"
        and not _is_exact_unbound_status_sentinel(payload)
    )


def _validate_matching_active_update_status(
    payload: dict[str, Any],
    *,
    request_id: str,
    request_profile: str,
    request_target_version: str | None,
    target_release: str,
    target_commit: str,
) -> None:
    source = payload.get("source")
    target_version_bound = _status_target_version_is_bound(
        payload,
        request_profile=request_profile,
        request_target_version=request_target_version,
        target_release=target_release,
    )
    if (
        payload.get("status") not in ACTIVE_UPDATE_STATUSES
        or payload.get("request_id") != request_id
        or not target_version_bound
        or payload.get("expected_commit") != target_commit
        or type(source) is not dict
        or source.get("commit") != target_commit
    ):
        raise SchemaControlError(
            "schema_execution_active_status_mismatch"
        )


def _status_target_version_is_bound(
    payload: dict[str, Any],
    *,
    request_profile: str,
    request_target_version: str | None,
    target_release: str,
) -> bool:
    if payload.get("target_version") == target_release:
        return True
    if (
        "target_version" not in payload
        and request_profile
        in LEGACY_STATUS_VERSION_FROM_REQUEST_PROFILES
        and request_target_version == target_release
    ):
        # Public updaters through v0.7.23 did not emit target_version in their
        # active or completed status payload.  Their validated request carries
        # the version, while status/request/release all bind the same exact
        # target commit.  Keep that narrow historical path without allowing a
        # current, retry, or transitional status to omit the field.
        return True
    return False


def _validate_matching_completed_update_status(
    payload: dict[str, Any],
    *,
    context: UpdateContext,
) -> None:
    if (
        payload.get("request_id") != context.request_id
        or payload.get("status") != "completed"
        or payload.get("commit_verified") is not True
        or payload.get("error") is not None
    ):
        raise SchemaControlError(
            "schema_execution_completed_status_invalid"
        )
    expected_commit = exact_string(
        payload.get("expected_commit"),
        code="schema_execution_completed_status_commit_invalid",
        max_length=40,
    ).lower()
    installed_commit = exact_string(
        payload.get("installed_commit"),
        code="schema_execution_completed_status_commit_invalid",
        max_length=40,
    ).lower()
    if (
        not COMMIT_RE.fullmatch(expected_commit)
        or not COMMIT_RE.fullmatch(installed_commit)
        or expected_commit != context.target_commit
        or installed_commit != context.target_commit
    ):
        raise SchemaControlError(
            "schema_execution_completed_status_commit_mismatch"
        )
    phase = exact_string(
        payload.get("phase"),
        code="schema_execution_completed_status_phase_invalid",
        max_length=80,
    )
    current_step = exact_string(
        payload.get("current_step"),
        code="schema_execution_completed_status_phase_invalid",
        max_length=80,
    )
    if phase != current_step or phase not in {
        "completed",
        "commit_verification",
    }:
        raise SchemaControlError(
            "schema_execution_completed_status_phase_invalid"
        )
    started_at = parse_utc(payload.get("started_at"))
    updated_at = parse_utc(payload.get("updated_at"))
    finished_at = parse_utc(
        payload.get("finished_at")
        if payload.get("finished_at") is not None
        else payload.get("updated_at")
    )
    if updated_at < started_at or finished_at < started_at:
        raise SchemaControlError(
            "schema_execution_completed_status_timestamp_invalid"
        )
    request_profile = validate_terminal_update_request(context.request)
    request_source = context.request.get("source")
    request_target_version = (
        request_source.get("version")
        if type(request_source) is dict
        else None
    )
    if not _status_target_version_is_bound(
        payload,
        request_profile=request_profile,
        request_target_version=request_target_version,
        target_release=context.target_release,
    ):
        raise SchemaControlError(
            "schema_execution_completed_status_target_mismatch"
        )


def _validate_terminal_auth_snapshot(
    *,
    context: UpdateContext,
    generation: int,
) -> None:
    payload = read_signed(AUTH_PATH)
    assert payload is not None
    expected = {
        "schema_version",
        "request_id",
        "target_version",
        "target_commit",
        "actor_subject",
        "actor_user_id",
        "actor_role",
        "permission",
        "issued_at",
        "expires_at",
        "fencing_generation",
    }
    if (
        set(payload) != expected
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or payload.get("request_id") != context.request_id
        or payload.get("target_version") != context.target_release
        or payload.get("target_commit") != context.target_commit
        or payload.get("fencing_generation") != generation
        or type(payload.get("fencing_generation")) is not int
        or payload.get("permission") != "manage_settings"
    ):
        raise SchemaControlError(
            "schema_execution_terminal_auth_binding_invalid"
        )
    actor_subject = exact_string(
        payload.get("actor_subject"),
        code="schema_execution_terminal_auth_actor_invalid",
        max_length=100,
    )
    actor_user_id = exact_string(
        payload.get("actor_user_id"),
        code="schema_execution_terminal_auth_actor_invalid",
        max_length=20,
    )
    actor_role = exact_string(
        payload.get("actor_role"),
        code="schema_execution_terminal_auth_actor_invalid",
        max_length=50,
    )
    if (
        not SAFE_SUBJECT_RE.fullmatch(actor_subject)
        or not actor_user_id.isdigit()
        or int(actor_user_id) <= 0
        or actor_role not in ALLOWED_ROLES
    ):
        raise SchemaControlError(
            "schema_execution_terminal_auth_actor_invalid"
        )
    requested_by = context.request.get("requested_by")
    if type(requested_by) is dict:
        if (
            str(requested_by.get("user_id") or "") != actor_user_id
            or requested_by.get("role") != actor_role
            or (
                requested_by.get("username") is not None
                and requested_by.get("username") != actor_subject
            )
        ):
            raise SchemaControlError(
                "schema_execution_terminal_auth_actor_mismatch"
            )
    issued_at = parse_utc(payload.get("issued_at"))
    expires_at = parse_utc(payload.get("expires_at"))
    if expires_at <= issued_at:
        raise SchemaControlError(
            "schema_execution_terminal_auth_timestamp_invalid"
        )


def _bootstrap_context_from_receipt(
    payload: dict[str, Any],
) -> UpdateContext:
    raw_source_schema = payload.get("source_schema_version")
    return UpdateContext(
        request={},
        request_id=str(payload.get("request_id") or ""),
        admission_attempt_id=str(
            payload.get("admission_attempt_id") or ""
        ),
        target_release=str(payload.get("target_release") or ""),
        target_commit=str(payload.get("target_commit") or ""),
        installed_version=str(payload.get("installed_version") or ""),
        installed_commit=str(payload.get("installed_commit") or ""),
        source_schema_version=(
            raw_source_schema
            if type(raw_source_schema) is int
            else 0
        ),
        source_shape_fingerprint=str(
            payload.get("source_shape_fingerprint") or ""
        ),
        registry_fingerprint=str(
            payload.get("registry_fingerprint") or ""
        ),
        plan_fingerprint=str(payload.get("plan_fingerprint") or ""),
    )


def _validate_persistent_bootstrap_evidence(
    db: Session,
    payload: dict[str, Any],
    *,
    current_generation: int,
    allow_prepared: bool = False,
) -> tuple[UpdateContext, str]:
    receipt_context = _bootstrap_context_from_receipt(payload)
    _validate_bootstrap_receipt(payload, context=receipt_context)
    receipt_generation = int(payload["fencing_generation"])
    if (
        payload.get("state")
        not in ({"prepared", "adopted"} if allow_prepared else {"adopted"})
        or receipt_generation > current_generation
    ):
        raise SchemaControlError(
            "schema_execution_terminal_bootstrap_receipt_mismatch"
        )
    control_shape = verify_control_shape(db)
    if (
        payload.get("state") == "adopted"
        and payload.get("control_shape_fingerprint") != control_shape
    ):
        raise SchemaControlError(
            "schema_execution_terminal_bootstrap_shape_mismatch"
        )
    bootstrap_attempt_id = transition_attempt_id(
        str(payload["admission_attempt_id"]),
        CONTROL_BOOTSTRAP_MIGRATION_ID,
    )
    attempt = db.get(SchemaMigrationAttempt, bootstrap_attempt_id)
    if (
        attempt is None
        or attempt.status != "applied"
        or attempt.request_id != payload["request_id"]
        or attempt.admission_attempt_id
        != payload["admission_attempt_id"]
        or attempt.migration_id != CONTROL_BOOTSTRAP_MIGRATION_ID
        or attempt.fencing_generation != receipt_generation
        or attempt.previous_version
        != receipt_context.source_schema_version
        or attempt.target_version != receipt_context.source_schema_version
        or attempt.installed_version != receipt_context.installed_version
        or attempt.installed_commit != receipt_context.installed_commit
        or attempt.target_release != receipt_context.target_release
        or attempt.target_commit.lower() != receipt_context.target_commit
        or attempt.registry_fingerprint
        != receipt_context.registry_fingerprint
        or attempt.plan_fingerprint != receipt_context.plan_fingerprint
        or attempt.definition_fingerprint
        != CONTROL_DEFINITION_FINGERPRINT
        or attempt.before_shape_fingerprint
        != receipt_context.source_shape_fingerprint
        or attempt.after_shape_fingerprint != control_shape
        or attempt.failure_class is not None
        or attempt.failure_summary is not None
        or bool(attempt.resumable)
    ):
        raise SchemaControlError(
            "schema_execution_terminal_bootstrap_database_mismatch"
        )
    return receipt_context, control_shape


def _validate_completed_terminal_authority(
    db: Session,
) -> UpdateContext:
    request = read_regular_json(REQUEST_PATH)
    assert request is not None
    validate_terminal_update_request(request)
    request_id = request["request_id"]
    target_release, target_commit = target_identity(
        request,
        terminal_evidence=True,
    )
    context = load_existing_update_context(db, terminal_evidence=True)
    (
        installed_version,
        installed_commit,
        source_schema_version,
        source_shapes,
    ) = expected_source_lineage(
        request_id=request_id,
        target_release=target_release,
        target_commit=target_commit,
    )
    if (
        context.request_id != request_id
        or context.target_release != target_release
        or context.target_commit != target_commit
        or context.installed_version != installed_version
        or context.installed_commit != installed_commit
        or context.source_schema_version != source_schema_version
        or context.source_shape_fingerprint not in source_shapes
    ):
        raise SchemaControlError(
            "schema_execution_terminal_context_mismatch"
        )

    validate_exact_target_noop(db)
    controls = db.query(SchemaMigrationControl).all()
    if len(controls) != 1:
        raise SchemaControlError(
            "schema_execution_terminal_control_count_invalid"
        )
    control = controls[0]
    if (
        control.id != CURRENT_STATE_ID
        or control.state != "completed"
        or control.request_id != context.request_id
        or control.owner_attempt_id != context.admission_attempt_id
        or control.target_release != context.target_release
        or control.target_commit.lower() != context.target_commit
        or control.registry_fingerprint != context.registry_fingerprint
        or control.plan_fingerprint != context.plan_fingerprint
        or control.fencing_generation < 1
    ):
        raise SchemaControlError(
            "schema_execution_terminal_control_binding_invalid"
        )
    generation = int(control.fencing_generation)

    bootstrap_receipt = read_signed(CONTROL_BOOTSTRAP_RECEIPT_PATH)
    assert bootstrap_receipt is not None
    _validate_persistent_bootstrap_evidence(
        db,
        bootstrap_receipt,
        current_generation=generation,
    )

    _validate_terminal_auth_snapshot(
        context=context,
        generation=generation,
    )
    for path in (
        PREPARATION_RECEIPT_PATH,
        RECOVERY_RECEIPT_PATH,
        GATE_RECEIPT_PATH,
    ):
        receipt = read_signed(path)
        assert receipt is not None
        try:
            validate_stage_receipt_payload(
                receipt,
                context=context,
                generation=generation,
                expected_state="completed",
            )
        except SchemaControlError as exc:
            raise SchemaControlError(
                f"schema_execution_terminal_{path.name}_{exc}"
            ) from exc
        if receipt.get("retryable") is not False:
            raise SchemaControlError(
                f"schema_execution_terminal_{path.name}_retry_invalid"
            )

    retry_expected = bool(context.request.get("migration_attempt_id"))
    if retry_expected and not _regular_file_presence(RETRY_ADMISSION_PATH):
        raise SchemaControlError(
            "schema_execution_terminal_retry_authority_mismatch"
        )
    return context


def resolve_schema_execution_mode(db: Session) -> str:
    status_observation = _observe_update_status_for_execution()
    authority = schema_execution_authority_presence()
    request = authority["request"]
    identity = authority["pre_overlay_identity"]
    pair_state = (
        "complete"
        if request and identity
        else "partial"
        if request != identity
        else "absent"
    )
    _raise_status_observation_error(
        status_observation,
        pair_state=pair_state,
    )
    if request != identity:
        raise SchemaControlError(
            "schema_update_authority_partial"
        )
    if request:
        status_payload = status_observation.payload
        if status_payload is None:
            raise SchemaControlError(
                "schema_execution_status_missing_with_complete_authority_pair"
            )
        if (
            _is_exact_unbound_status_sentinel(status_payload)
            or _status_has_operation_authority(status_payload)
        ):
            raise SchemaControlError(
                "schema_execution_status_contradictory_with_complete_authority_pair"
            )
        request_payload = read_regular_json(REQUEST_PATH)
        assert request_payload is not None
        strict_authority_error: SchemaControlError | None = None
        try:
            request_profile = validate_update_request(request_payload)
            target_release, target_commit = target_identity(request_payload)
        except SchemaControlError as exc:
            strict_authority_error = exc
            request_profile = validate_terminal_update_request(
                request_payload
            )
            target_release, target_commit = target_identity(
                request_payload,
                terminal_evidence=True,
            )
        request_id = request_payload["request_id"]
        control = _schema_control_row_for_execution(db)
        same_terminal_control = bool(
            control is not None
            and control.state == "completed"
            and control.request_id == request_id
            and control.target_release == target_release
            and control.target_commit.lower() == target_commit
        )
        if strict_authority_error is not None and not same_terminal_control:
            raise strict_authority_error
        if same_terminal_control:
            context = _validate_completed_terminal_authority(db)
            status = status_payload["status"]
            status_request_id = status_payload.get("request_id")
            if status_request_id == context.request_id:
                if status != "completed":
                    raise SchemaControlError(
                        "schema_execution_terminal_status_contradictory"
                    )
                _validate_matching_completed_update_status(
                    status_payload,
                    context=context,
                )
            elif status in ACTIVE_UPDATE_STATUSES:
                raise SchemaControlError(
                    "schema_execution_foreign_active_status"
                )
            else:
                raise SchemaControlError(
                    "schema_execution_status_contradictory_with_complete_authority_pair"
                )
            return "exact_target_noop"

        if (
            control is not None
            and control.request_id == request_id
            and control.state in {"completed", "failed"}
        ):
            raise SchemaControlError(
                "schema_execution_terminal_control_incomplete"
            )
        if (
            control is not None
            and control.request_id != request_id
            and control.state in {"prepared", "recovering", "migrating"}
        ):
            raise SchemaControlError(
                "schema_execution_foreign_control_active"
            )
        request_source = request_payload.get("source")
        request_target_version = (
            request_source.get("version")
            if type(request_source) is dict
            else None
        )
        _validate_matching_active_update_status(
            status_payload,
            request_id=request_id,
            request_profile=request_profile,
            request_target_version=request_target_version,
            target_release=target_release,
            target_commit=target_commit,
        )
        return "authorized_update"
    status_payload = status_observation.payload
    if status_payload is not None:
        status = status_payload["status"]
        if status in ACTIVE_UPDATE_STATUSES:
            raise SchemaControlError(
                "schema_execution_active_status_without_matching_authority_pair"
            )
        if not _is_exact_unbound_status_sentinel(status_payload):
            raise SchemaControlError(
                "schema_execution_status_orphaned"
            )
    orphaned = [
        name
        for name, present in authority.items()
        if name not in {"request", "pre_overlay_identity"} and present
    ]
    if database_is_empty(db):
        if orphaned:
            raise SchemaControlError(
                "schema_update_signed_authority_orphaned"
            )
        return "fresh_install"
    validate_exact_target_noop(db)
    return "exact_target_noop"


def wait_for_writer_quiescence(
    db: Session,
    *,
    owned_backend_pid: int | None = None,
    timeout_seconds: int = 30,
) -> None:
    if owned_backend_pid is not None and (
        type(owned_backend_pid) is not int or owned_backend_pid <= 0
    ):
        raise SchemaControlError("schema_pipeline_backend_pid_invalid")
    owned_backend_clause = ""
    parameters: dict[str, int] = {}
    if owned_backend_pid is not None:
        owned_backend_clause = "AND pid <> :owned_backend_pid"
        parameters["owned_backend_pid"] = owned_backend_pid
    deadline = time.monotonic() + timeout_seconds
    while True:
        rows = db.execute(
            text(
                f"""
                SELECT pid
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  {owned_backend_clause}
                  AND backend_type = 'client backend'
                ORDER BY pid
                """
            ),
            parameters,
        ).all()
        if not rows:
            return
        if time.monotonic() >= deadline:
            raise SchemaControlError("product_database_writer_not_quiescent")
        db.rollback()
        time.sleep(0.5)


def resolve_schema_pipeline_execution_mode(
    db: Session,
    *,
    owned_backend_pid: int | None = None,
) -> str:
    mode = resolve_schema_execution_mode(db)
    if mode != "authorized_update":
        return mode
    wait_for_writer_quiescence(db, owned_backend_pid=owned_backend_pid)
    return resolve_schema_execution_mode(db)


def target_identity(
    request: dict[str, Any],
    *,
    terminal_evidence: bool = False,
) -> tuple[str, str]:
    release = read_regular_json(RELEASE_PATH)
    assert release is not None
    source = request.get("source")
    if type(source) is not dict:
        raise SchemaControlError("request_source_invalid")
    if (
        set(release) != RELEASE_IDENTITY_FIELDS
        or type(release.get("schema_version")) is not int
        or release["schema_version"] != 1
        or release.get("product") != "KM VMS"
        or release.get("release_channel") != "public-github"
        or release.get("source_kind") != "github-release"
        or release.get("source_repo") != "kmishnev87/km-vms"
        or release.get("metadata_status")
        not in {"precompose", "partial", "complete"}
    ):
        raise SchemaControlError("release_identity_contract_invalid")
    if terminal_evidence:
        if release.get("metadata_status") != "complete":
            raise SchemaControlError(
                "release_identity_terminal_metadata_incomplete"
            )
        for key, max_length in (
            ("installed_by", 80),
            ("metadata_source", 100),
        ):
            value = exact_string(
                release.get(key),
                code="release_identity_contract_invalid",
                max_length=max_length,
            )
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
                raise SchemaControlError(
                    "release_identity_contract_invalid"
                )
    elif (
        release.get("installed_by")
        not in {
            "install",
            "in_app_helper",
            "terminal_update",
            "release_cycle_closeout",
        }
        or release.get("metadata_source")
        not in {
            "official_install",
            "official_update",
            "helper",
            "release_cycle_closeout",
        }
    ):
        raise SchemaControlError("release_identity_contract_invalid")
    target_commit_value = exact_string(
        source.get("commit"),
        code="request_target_commit_invalid",
        max_length=40,
    )
    release_commit_value = exact_string(
        release.get("commit_sha"),
        code="release_target_commit_invalid",
        max_length=40,
    )
    target_release = exact_string(
        release.get("version"),
        code="release_target_version_missing",
        max_length=80,
    )
    release_ref = exact_string(
        release.get("source_ref"),
        code="release_target_version_invalid",
        max_length=81,
    )
    for key, max_length in (("title", 200), ("summary", 1000)):
        exact_string(
            release.get(key),
            code="release_identity_contract_invalid",
            max_length=max_length,
        )
    parse_utc(release.get("installed_at"))
    valid_release_refs = {f"v{target_release}"}
    if terminal_evidence:
        valid_release_refs.add(target_commit_value.lower())
    if (
        not COMMIT_RE.fullmatch(target_commit_value)
        or not re.fullmatch(r"[0-9a-f]{40}", release_commit_value)
        or release_ref.lower() not in valid_release_refs
    ):
        raise SchemaControlError("request_target_commit_invalid")
    target_commit = target_commit_value.lower()
    release_commit = release_commit_value
    if target_commit != release_commit:
        raise SchemaControlError("release_target_commit_mismatch")
    return target_release, target_commit


def actor_snapshot(
    db: Session,
    request: dict[str, Any],
    *,
    installed_version: str,
) -> tuple[int, str, str]:
    requested_by = request.get("requested_by")
    if type(requested_by) is not dict:
        raise SchemaControlError("request_actor_missing")
    current_actor_fields = {
        "user_id",
        "username",
        "role",
        "ip_address",
        "user_agent",
    }
    current_actor = (
        request.get("schema_version") == 2
        or set(requested_by) == current_actor_fields
    )
    expected_actor_fields = (
        current_actor_fields if current_actor else {"user_id", "role"}
    )
    if set(requested_by) != expected_actor_fields:
        raise SchemaControlError("request_actor_fields_invalid")
    raw_user_id = requested_by.get("user_id")
    if type(raw_user_id) is int:
        user_id = raw_user_id
    elif (
        type(raw_user_id) is str
        and raw_user_id.isdigit()
        and len(raw_user_id) <= 20
    ):
        user_id = int(raw_user_id)
    else:
        raise SchemaControlError("request_actor_id_invalid")
    if user_id <= 0:
        raise SchemaControlError("request_actor_id_invalid")
    row = db.execute(
        text(
            "SELECT id, username, role, is_active "
            "FROM users WHERE id=:user_id"
        ),
        {"user_id": user_id},
    ).mappings().one_or_none()
    if row is None or not bool(row.get("is_active")):
        raise SchemaControlError("request_actor_unavailable")
    role = row.get("role")
    username = row.get("username")
    if (
        type(role) is not str
        or type(username) is not str
        or role not in ALLOWED_ROLES
        or role != requested_by.get("role")
        or (
            current_actor
            and username != requested_by.get("username")
        )
        or not SAFE_SUBJECT_RE.fullmatch(username)
    ):
        raise SchemaControlError("request_actor_permission_invalid")
    return int(row["id"]), username, role


def _source_identity_payload() -> dict[str, Any]:
    payload = read_regular_json(PRE_OVERLAY_IDENTITY_PATH)
    assert payload is not None
    required = {
        "schema_version",
        "request_id",
        "installed_version",
        "installed_commit",
        "recorded_at",
    }
    if (
        set(payload) != required
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise SchemaControlError("pre_overlay_identity_fields_invalid")
    request_id = exact_string(
        payload.get("request_id"),
        code="pre_overlay_identity_request_invalid",
        max_length=80,
    )
    installed_version = exact_string(
        payload.get("installed_version"),
        code="pre_overlay_identity_version_invalid",
        max_length=80,
    )
    installed_commit = exact_string(
        payload.get("installed_commit"),
        code="pre_overlay_identity_commit_invalid",
        max_length=40,
    )
    if (
        not REQUEST_RE.fullmatch(request_id)
        or not re.fullmatch(r"[0-9a-f]{40}", installed_commit)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}",
            installed_version,
        )
    ):
        raise SchemaControlError("pre_overlay_identity_values_invalid")
    parse_utc(payload.get("recorded_at"))
    return payload


def expected_source_lineage(
    *,
    request_id: str,
    target_release: str,
    target_commit: str,
) -> tuple[str, str, int, frozenset[str]]:
    payload = _source_identity_payload()
    installed_version = payload["installed_version"]
    installed_commit = payload["installed_commit"]
    if payload.get("request_id") != request_id:
        raise SchemaControlError("pre_overlay_identity_request_mismatch")
    same_target = (
        installed_version == target_release
        and installed_commit == target_commit
    )
    if not same_target and (
        installed_version not in SOURCE_TAG_COMMITS
        or SOURCE_TAG_COMMITS[installed_version] != installed_commit
    ):
        raise SchemaControlError("installed_source_lineage_unsupported")
    expected_schema_version = (
        TARGET_SCHEMA_VERSION
        if same_target
        else SOURCE_SCHEMA_VERSIONS[installed_version]
    )
    primary_shape = (
        TARGET_SHAPE_FINGERPRINT
        if same_target
        else SOURCE_SHAPE_FINGERPRINTS.get(installed_version)
    )
    if not primary_shape:
        raise SchemaControlError("source_shape_evidence_missing")
    expected_shapes = frozenset(
        {primary_shape}
        | (
            set()
            if same_target
            else set(
                SOURCE_SHAPE_FINGERPRINT_ALTERNATES.get(
                    installed_version,
                    frozenset(),
                )
            )
        )
    )
    if request_id.startswith("stage609-") != (
        installed_version in {"0.7.2", "0.7.3"}
    ):
        raise SchemaControlError("request_id_source_family_mismatch")
    return (
        installed_version,
        installed_commit,
        expected_schema_version,
        expected_shapes,
    )


def validate_source_lineage(
    db: Session,
    *,
    request_id: str,
    target_release: str,
    target_commit: str,
) -> tuple[str, str, int, str]:
    (
        installed_version,
        installed_commit,
        expected_schema_version,
        expected_shapes,
    ) = expected_source_lineage(
        request_id=request_id,
        target_release=target_release,
        target_commit=target_commit,
    )
    source_schema_version = current_schema_version(db)
    if source_schema_version != expected_schema_version:
        raise SchemaControlError("installed_source_schema_version_mismatch")
    shape = database_shape_fingerprint(db)
    if shape not in expected_shapes:
        raise SchemaControlError("installed_source_shape_mismatch")
    return installed_version, installed_commit, source_schema_version, shape


def plan_fingerprint(
    *,
    installed_version: str,
    installed_commit: str,
    source_schema_version: int,
    source_shape_fingerprint: str,
    target_release: str,
    target_commit: str,
) -> str:
    transitions = PRODUCTION_MIGRATIONS.path(
        source_schema_version,
        TARGET_SCHEMA_VERSION,
    )
    payload = {
        "schema_version": 1,
        "installed_version": installed_version,
        "installed_commit": installed_commit,
        "source_schema_version": source_schema_version,
        "source_shape_fingerprint": source_shape_fingerprint,
        "target_release": target_release,
        "target_commit": target_commit,
        "target_schema_version": TARGET_SCHEMA_VERSION,
        "registry_fingerprint": REGISTRY_FINGERPRINT,
        "conditional_preparation": {
            "preparation_id": (
                STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id
            ),
            "definition_fingerprint": preparation_definition_fingerprint(
                STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
            ),
        },
        "transitions": [
            {
                "migration_id": migration.migration_id,
                "from_version": migration.from_version,
                "to_version": migration.to_version,
                "definition_fingerprint": migration_definition_fingerprint(
                    migration
                ),
            }
            for migration in transitions
        ],
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def admission_attempt_id(request: dict[str, Any], request_id: str) -> str:
    supplied_value = request.get("migration_attempt_id")
    retry_of_value = request.get("retry_of_request_id")
    supplied = (
        supplied_value if type(supplied_value) is str else ""
    )
    retry_of = (
        retry_of_value if type(retry_of_value) is str else ""
    )
    if supplied_value is not None and type(supplied_value) is not str:
        raise SchemaControlError("migration_attempt_id_invalid")
    if retry_of_value is not None and type(retry_of_value) is not str:
        raise SchemaControlError("migration_attempt_id_invalid")
    if supplied:
        if (
            not ATTEMPT_RE.fullmatch(supplied)
            or not REQUEST_RE.fullmatch(retry_of)
            or retry_of == request_id
            or not request_id.startswith("update-")
        ):
            raise SchemaControlError("migration_attempt_id_invalid")
        return supplied
    if retry_of:
        raise SchemaControlError("retry_attempt_id_missing")
    target = request.get("source")
    if type(target) is not dict:
        raise SchemaControlError("request_source_invalid")
    target_commit_value = exact_string(
        target.get("commit"),
        code="request_target_commit_invalid",
        max_length=40,
    )
    if not COMMIT_RE.fullmatch(target_commit_value):
        raise SchemaControlError("request_target_commit_invalid")
    target_commit = target_commit_value.lower()
    return "migration-attempt-" + hashlib.sha256(
        f"{request_id}|{target_commit}|admission".encode("ascii")
    ).hexdigest()[:32]


def validate_retry_admission(
    request: dict[str, Any],
    *,
    request_id: str,
    target_release: str,
    target_commit: str,
    plan_fingerprint_value: str,
) -> None:
    supplied_value = request.get("migration_attempt_id")
    retry_of_value = request.get("retry_of_request_id")
    supplied = supplied_value if type(supplied_value) is str else ""
    retry_of = retry_of_value if type(retry_of_value) is str else ""
    if supplied_value is not None and type(supplied_value) is not str:
        raise SchemaControlError("retry_admission_binding_invalid")
    if retry_of_value is not None and type(retry_of_value) is not str:
        raise SchemaControlError("retry_admission_binding_invalid")
    if not supplied and not retry_of:
        return
    payload = read_signed(RETRY_ADMISSION_PATH)
    assert payload is not None
    required = {
        "schema_version",
        "contract_fingerprint",
        "original_request_id",
        "original_request_fingerprint",
        "request_id",
        "attempt_id",
        "actor_subject",
        "target_version",
        "target_commit",
        "target_schema_version",
        "registry_fingerprint",
        "plan_fingerprint",
        "fencing_generation",
        "retry_request",
        "accepted_at",
    }
    if (
        set(payload) != required
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise SchemaControlError("retry_admission_fields_invalid")
    actor_subject = exact_string(
        payload.get("actor_subject"),
        code="retry_admission_binding_invalid",
        max_length=100,
    )
    payload_target_commit = exact_string(
        payload.get("target_commit"),
        code="retry_admission_binding_invalid",
        max_length=40,
    )
    if (
        payload.get("request_id") != request_id
        or payload.get("attempt_id") != supplied
        or payload.get("original_request_id") != retry_of
        or payload.get("target_version") != target_release
        or not re.fullmatch(r"[0-9a-f]{40}", payload_target_commit)
        or payload_target_commit != target_commit
        or payload.get("target_schema_version") != TARGET_SCHEMA_VERSION
        or type(payload.get("target_schema_version")) is not int
        or payload.get("registry_fingerprint") != REGISTRY_FINGERPRINT
        or payload.get("plan_fingerprint") != plan_fingerprint_value
        or type(payload.get("retry_request")) is not dict
        or payload.get("retry_request") != request
        or type(payload.get("fencing_generation")) is not int
        or payload["fencing_generation"] <= 0
        or not SAFE_SUBJECT_RE.fullmatch(actor_subject)
    ):
        raise SchemaControlError("retry_admission_binding_invalid")
    for key, pattern, max_length in (
        ("request_id", REQUEST_RE, 80),
        ("original_request_id", REQUEST_RE, 80),
        ("attempt_id", ATTEMPT_RE, 64),
    ):
        value = exact_string(
            payload.get(key),
            code="retry_admission_binding_invalid",
            max_length=max_length,
        )
        if not pattern.fullmatch(value):
            raise SchemaControlError("retry_admission_binding_invalid")
    exact_string(
        payload.get("target_version"),
        code="retry_admission_binding_invalid",
        max_length=80,
    )
    for key in (
        "contract_fingerprint",
        "original_request_fingerprint",
        "registry_fingerprint",
        "plan_fingerprint",
    ):
        value = exact_string(
            payload.get(key),
            code=f"retry_admission_{key}_invalid",
            max_length=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise SchemaControlError(f"retry_admission_{key}_invalid")
    parse_utc(payload.get("accepted_at"))


def validate_retry_actor(
    request: dict[str, Any],
    *,
    actor_subject: str,
) -> None:
    if not request.get("migration_attempt_id"):
        return
    payload = read_signed(RETRY_ADMISSION_PATH)
    assert payload is not None
    if payload.get("actor_subject") != actor_subject:
        raise SchemaControlError("retry_admission_actor_mismatch")


def transition_attempt_id(
    admission_id: str,
    transition_id: str,
) -> str:
    return "migration-attempt-" + hashlib.sha256(
        f"{admission_id}|{transition_id}".encode("ascii")
    ).hexdigest()[:32]


def load_prebootstrap_update_context(
    db: Session | None = None,
) -> UpdateContext:
    request = read_regular_json(REQUEST_PATH)
    assert request is not None
    validate_update_request(request)
    request_id = request["request_id"]
    target_release, target_commit = target_identity(request)
    (
        installed_version,
        installed_commit,
        source_schema,
        expected_source_shapes,
    ) = expected_source_lineage(
        request_id=request_id,
        target_release=target_release,
        target_commit=target_commit,
    )
    if db is None:
        if len(expected_source_shapes) != 1:
            raise SchemaControlError(
                "source_shape_database_validation_required"
            )
        source_shape = next(iter(expected_source_shapes))
    else:
        source_schema_actual = current_schema_version(db)
        if source_schema_actual != source_schema:
            raise SchemaControlError(
                "installed_source_schema_version_mismatch"
            )
        source_shape = database_shape_fingerprint(db)
        if source_shape not in expected_source_shapes:
            raise SchemaControlError("installed_source_shape_mismatch")
    fingerprint = plan_fingerprint(
        installed_version=installed_version,
        installed_commit=installed_commit,
        source_schema_version=source_schema,
        source_shape_fingerprint=source_shape,
        target_release=target_release,
        target_commit=target_commit,
    )
    validate_retry_admission(
        request,
        request_id=request_id,
        target_release=target_release,
        target_commit=target_commit,
        plan_fingerprint_value=fingerprint,
    )
    return UpdateContext(
        request=request,
        request_id=request_id,
        admission_attempt_id=admission_attempt_id(request, request_id),
        target_release=target_release,
        target_commit=target_commit,
        installed_version=installed_version,
        installed_commit=installed_commit,
        source_schema_version=source_schema,
        source_shape_fingerprint=source_shape,
        registry_fingerprint=REGISTRY_FINGERPRINT,
        plan_fingerprint=fingerprint,
    )


def validate_initial_context_database(
    db: Session,
    *,
    context: UpdateContext,
) -> None:
    (
        installed_version,
        installed_commit,
        source_schema,
        source_shape,
    ) = validate_source_lineage(
        db,
        request_id=context.request_id,
        target_release=context.target_release,
        target_commit=context.target_commit,
    )
    if (
        installed_version != context.installed_version
        or installed_commit != context.installed_commit
        or source_schema != context.source_schema_version
        or source_shape != context.source_shape_fingerprint
    ):
        raise SchemaControlError("installed_source_context_changed")


def load_initial_update_context(db: Session) -> UpdateContext:
    context = load_prebootstrap_update_context(db)
    validate_initial_context_database(db, context=context)
    return context


def load_existing_update_context(
    db: Session,
    *,
    terminal_evidence: bool = False,
    allow_completed_rollover: bool = False,
) -> UpdateContext:
    request = read_regular_json(REQUEST_PATH)
    assert request is not None
    if terminal_evidence:
        validate_terminal_update_request(request)
    else:
        validate_update_request(request)
    request_id = request["request_id"]
    target_release, target_commit = target_identity(
        request,
        terminal_evidence=terminal_evidence,
    )
    row = db.execute(
        text(
            """
            SELECT *
            FROM schema_migration_control
            WHERE id='current'
            FOR UPDATE
            """
        )
    ).mappings().one_or_none()
    if row is None:
        raise SchemaControlError("schema_migration_control_row_missing")
    target_mismatch = (
        str(row["target_commit"]).lower() != target_commit
        or str(row["target_release"]) != target_release
        or int(row["target_schema_version"]) != TARGET_SCHEMA_VERSION
        or str(row["registry_fingerprint"]) != REGISTRY_FINGERPRINT
        or str(row["control_definition_fingerprint"])
        != CONTROL_DEFINITION_FINGERPRINT
    )
    if target_mismatch and allow_completed_rollover:
        if str(row["state"]) != "completed":
            raise SchemaControlError(
                "schema_migration_control_rollover_requires_completed"
            )
        return load_prebootstrap_update_context(db)
    if target_mismatch:
        raise SchemaControlError("schema_migration_control_target_mismatch")
    expected_plan = plan_fingerprint(
        installed_version=str(row["installed_version"]),
        installed_commit=str(row["installed_commit"]).lower(),
        source_schema_version=int(row["source_schema_version"]),
        source_shape_fingerprint=str(row["source_shape_fingerprint"]),
        target_release=target_release,
        target_commit=target_commit,
    )
    if str(row["plan_fingerprint"]) != expected_plan:
        raise SchemaControlError("schema_migration_control_plan_mismatch")
    validate_retry_admission(
        request,
        request_id=request_id,
        target_release=target_release,
        target_commit=target_commit,
        plan_fingerprint_value=expected_plan,
    )
    return UpdateContext(
        request=request,
        request_id=request_id,
        admission_attempt_id=admission_attempt_id(request, request_id),
        target_release=target_release,
        target_commit=target_commit,
        installed_version=str(row["installed_version"]),
        installed_commit=str(row["installed_commit"]).lower(),
        source_schema_version=int(row["source_schema_version"]),
        source_shape_fingerprint=str(row["source_shape_fingerprint"]),
        registry_fingerprint=REGISTRY_FINGERPRINT,
        plan_fingerprint=expected_plan,
    )


def _control_tables_present(db: Session) -> tuple[bool, bool]:
    inspector = inspect(db.connection())
    return (
        inspector.has_table("schema_migration_control"),
        inspector.has_table("schema_migration_attempts"),
    )


def verify_control_shape(db: Session) -> str:
    inspector = inspect(db.connection())
    expected_columns = {
        "schema_migration_control": {
            "id",
            "fencing_generation",
            "owner_attempt_id",
            "request_id",
            "installed_version",
            "installed_commit",
            "source_schema_version",
            "target_commit",
            "target_release",
            "target_schema_version",
            "registry_fingerprint",
            "plan_fingerprint",
            "source_shape_fingerprint",
            "control_definition_fingerprint",
            "state",
            "lease_expires_at",
            "updated_at",
        },
        "schema_migration_attempts": {
            "attempt_id",
            "admission_attempt_id",
            "request_id",
            "migration_id",
            "previous_version",
            "target_version",
            "status",
            "started_at",
            "completed_at",
            "fencing_generation",
            "installed_version",
            "installed_commit",
            "target_release",
            "target_commit",
            "registry_fingerprint",
            "plan_fingerprint",
            "definition_fingerprint",
            "before_shape_fingerprint",
            "after_shape_fingerprint",
            "failure_class",
            "failure_summary",
            "resumable",
            "details",
        },
    }
    expected_indexes = {
        "schema_migration_control": set(),
        "schema_migration_attempts": {
            "ix_schema_migration_attempt_request",
            "ix_schema_migration_attempt_status",
            "uq_schema_migration_attempt_applied_lineage",
        },
    }
    expected_checks = {
        "schema_migration_control": {
            "ck_schema_migration_control_fingerprints",
            "ck_schema_migration_control_state",
        },
        "schema_migration_attempts": {
            "ck_schema_migration_attempt_status",
            "ck_schema_migration_attempt_fingerprints",
        },
    }
    for table_name, columns in expected_columns.items():
        actual_columns = {
            str(column.get("name") or "")
            for column in inspector.get_columns(table_name)
        }
        if actual_columns != columns:
            raise SchemaControlError(f"{table_name}_column_shape_mismatch")
        indexes = {
            str(index.get("name") or "")
            for index in inspector.get_indexes(table_name)
        }
        if indexes != expected_indexes[table_name]:
            raise SchemaControlError(f"{table_name}_index_shape_mismatch")
        checks = {
            str(constraint.get("name") or "")
            for constraint in inspector.get_check_constraints(table_name)
        }
        if checks != expected_checks[table_name]:
            raise SchemaControlError(f"{table_name}_check_shape_mismatch")
        primary = inspector.get_pk_constraint(table_name)
        expected_primary = (
            ["id"]
            if table_name == "schema_migration_control"
            else ["attempt_id"]
        )
        if list(primary.get("constrained_columns") or []) != expected_primary:
            raise SchemaControlError(f"{table_name}_primary_key_mismatch")
    index_definition = db.execute(
        text(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname='public'
              AND tablename='schema_migration_attempts'
              AND indexname='uq_schema_migration_attempt_applied_lineage'
            """
        )
    ).scalar_one()
    normalized = _normalize_sql(index_definition).lower()
    if (
        "create unique index uq_schema_migration_attempt_applied_lineage"
        not in normalized
        or "schema_migration_attempts" not in normalized
        or "(migration_id, definition_fingerprint)" not in normalized
        or " where " not in normalized
        or "status" not in normalized
        or "'applied'" not in normalized
    ):
        raise SchemaControlError("schema_migration_attempt_unique_lineage_mismatch")
    return database_shape_fingerprint(
        db,
        exclude_tables=set(inspector.get_table_names())
        - {"schema_migration_control", "schema_migration_attempts"},
    )


def write_auth_snapshot(
    *,
    context: UpdateContext,
    actor_user_id: int,
    actor_subject: str,
    actor_role: str,
    generation: int,
) -> None:
    now = datetime.now(timezone.utc)
    write_signed(
        AUTH_PATH,
        {
            "schema_version": 1,
            "request_id": context.request_id,
            "target_version": context.target_release,
            "target_commit": context.target_commit,
            "actor_subject": actor_subject,
            "actor_user_id": str(actor_user_id),
            "actor_role": actor_role,
            "permission": "manage_settings",
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(hours=4))
            .isoformat()
            .replace("+00:00", "Z"),
            "fencing_generation": generation,
        },
    )


def _bootstrap_receipt_payload(
    *,
    context: UpdateContext,
    generation: int,
    state: str,
    control_shape_fingerprint: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": context.request_id,
        "admission_attempt_id": context.admission_attempt_id,
        "target_release": context.target_release,
        "target_commit": context.target_commit,
        "target_schema_version": TARGET_SCHEMA_VERSION,
        "installed_version": context.installed_version,
        "installed_commit": context.installed_commit,
        "source_schema_version": context.source_schema_version,
        "source_shape_fingerprint": context.source_shape_fingerprint,
        "registry_fingerprint": context.registry_fingerprint,
        "plan_fingerprint": context.plan_fingerprint,
        "control_definition_fingerprint": CONTROL_DEFINITION_FINGERPRINT,
        "control_shape_fingerprint": control_shape_fingerprint,
        "fencing_generation": generation,
        "state": state,
        "updated_at": utc_now(),
    }


def _validate_bootstrap_receipt(
    payload: dict[str, Any],
    *,
    context: UpdateContext,
) -> None:
    expected = {
        "schema_version",
        "request_id",
        "admission_attempt_id",
        "target_release",
        "target_commit",
        "target_schema_version",
        "installed_version",
        "installed_commit",
        "source_schema_version",
        "source_shape_fingerprint",
        "registry_fingerprint",
        "plan_fingerprint",
        "control_definition_fingerprint",
        "control_shape_fingerprint",
        "fencing_generation",
        "state",
        "updated_at",
    }
    if (
        set(payload) != expected
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise SchemaControlError("control_bootstrap_receipt_fields_invalid")
    for key, max_length in (
        ("request_id", 80),
        ("admission_attempt_id", 64),
        ("target_release", 80),
        ("target_commit", 40),
        ("installed_version", 80),
        ("installed_commit", 40),
        ("state", 20),
    ):
        exact_string(
            payload.get(key),
            code=f"control_bootstrap_receipt_{key}_invalid",
            max_length=max_length,
        )
    for key in (
        "target_schema_version",
        "source_schema_version",
        "fencing_generation",
    ):
        if type(payload.get(key)) is not int:
            raise SchemaControlError(
                f"control_bootstrap_receipt_{key}_invalid"
            )
    for key in (
        "source_shape_fingerprint",
        "registry_fingerprint",
        "plan_fingerprint",
        "control_definition_fingerprint",
    ):
        value = exact_string(
            payload.get(key),
            code=f"control_bootstrap_receipt_{key}_invalid",
            max_length=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise SchemaControlError(
                f"control_bootstrap_receipt_{key}_invalid"
            )
    bindings = {
        "target_release": context.target_release,
        "target_commit": context.target_commit,
        "target_schema_version": TARGET_SCHEMA_VERSION,
        "installed_version": context.installed_version,
        "installed_commit": context.installed_commit,
        "source_schema_version": context.source_schema_version,
        "source_shape_fingerprint": context.source_shape_fingerprint,
        "registry_fingerprint": context.registry_fingerprint,
        "plan_fingerprint": context.plan_fingerprint,
        "control_definition_fingerprint": CONTROL_DEFINITION_FINGERPRINT,
    }
    for key, value in bindings.items():
        if payload.get(key) != value:
            raise SchemaControlError(
                f"control_bootstrap_receipt_{key}_mismatch"
            )
    if not REQUEST_RE.fullmatch(payload["request_id"]):
        raise SchemaControlError("control_bootstrap_receipt_request_invalid")
    if not ATTEMPT_RE.fullmatch(payload["admission_attempt_id"]):
        raise SchemaControlError(
            "control_bootstrap_receipt_attempt_invalid"
        )
    if payload.get("state") not in {"prepared", "adopted"}:
        raise SchemaControlError("control_bootstrap_receipt_state_invalid")
    if (
        type(payload.get("fencing_generation")) is not int
        or payload["fencing_generation"] <= 0
    ):
        raise SchemaControlError("control_bootstrap_receipt_fencing_invalid")
    control_shape = payload.get("control_shape_fingerprint")
    if payload["state"] == "prepared":
        if control_shape is not None:
            raise SchemaControlError(
                "control_bootstrap_receipt_control_shape_invalid"
            )
    elif (
        type(control_shape) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", control_shape)
    ):
        raise SchemaControlError(
            "control_bootstrap_receipt_control_shape_invalid"
        )
    parse_utc(payload.get("updated_at"))


def start_attempt(
    db: Session,
    *,
    context: UpdateContext,
    generation: int,
    transition_id: str,
    previous_version: int | None,
    target_version: int,
    definition_fingerprint: str,
    before_shape_fingerprint: str,
) -> tuple[str, str]:
    attempt_id = transition_attempt_id(
        context.admission_attempt_id,
        transition_id,
    )
    existing = db.get(SchemaMigrationAttempt, attempt_id)
    if existing is not None:
        exact = (
            existing.admission_attempt_id == context.admission_attempt_id
            and existing.request_id == context.request_id
            and existing.migration_id == transition_id
            and existing.previous_version == previous_version
            and existing.target_version == target_version
            and existing.target_commit == context.target_commit
            and existing.registry_fingerprint == context.registry_fingerprint
            and existing.plan_fingerprint == context.plan_fingerprint
            and existing.definition_fingerprint == definition_fingerprint
            and existing.before_shape_fingerprint == before_shape_fingerprint
        )
        if not exact:
            raise SchemaControlError("migration_attempt_identity_mismatch")
        return attempt_id, str(existing.status)
    db.add(
        SchemaMigrationAttempt(
            attempt_id=attempt_id,
            admission_attempt_id=context.admission_attempt_id,
            request_id=context.request_id,
            migration_id=transition_id,
            previous_version=previous_version,
            target_version=target_version,
            status="started",
            started_at=naive_utc_now(),
            completed_at=None,
            fencing_generation=generation,
            installed_version=context.installed_version,
            installed_commit=context.installed_commit,
            target_release=context.target_release,
            target_commit=context.target_commit,
            registry_fingerprint=context.registry_fingerprint,
            plan_fingerprint=context.plan_fingerprint,
            definition_fingerprint=definition_fingerprint,
            before_shape_fingerprint=before_shape_fingerprint,
            after_shape_fingerprint=None,
            failure_class=None,
            failure_summary=None,
            resumable=False,
            details={},
        )
    )
    db.flush()
    return attempt_id, "started"


def finish_attempt(
    db: Session,
    *,
    attempt_id: str,
    status: str,
    after_shape_fingerprint: str | None,
    details: dict[str, Any],
    failure_class: str | None = None,
    failure_summary: str | None = None,
    resumable: bool = False,
) -> None:
    if status not in {"applied", "failed", "blocked", "interrupted"}:
        raise SchemaControlError("migration_attempt_terminal_status_invalid")
    result = db.execute(
        text(
            """
            UPDATE schema_migration_attempts
            SET status=:status,
                completed_at=:completed_at,
                after_shape_fingerprint=:after_shape,
                failure_class=:failure_class,
                failure_summary=:failure_summary,
                resumable=:resumable,
                details=CAST(:details AS JSON)
            WHERE attempt_id=:attempt_id
              AND status='started'
            """
        ),
        {
            "status": status,
            "completed_at": naive_utc_now(),
            "after_shape": after_shape_fingerprint,
            "failure_class": (failure_class or "")[:96] or None,
            "failure_summary": (failure_summary or "")[:300] or None,
            "resumable": bool(resumable),
            "details": bounded_details(details),
            "attempt_id": attempt_id,
        },
    )
    if result.rowcount != 1:
        existing = db.get(SchemaMigrationAttempt, attempt_id)
        if (
            existing is None
            or str(existing.status) != status
            or existing.after_shape_fingerprint != after_shape_fingerprint
        ):
            raise SchemaControlError("migration_attempt_transition_conflict")


def bootstrap_or_resume_control(
    db: Session,
    *,
    context: UpdateContext,
    actor_user_id: int,
    actor_subject: str,
    actor_role: str,
) -> int:
    has_control, has_attempts = _control_tables_present(db)
    if has_control != has_attempts:
        raise SchemaControlError("migration_control_partial_shape")
    if not has_control:
        existing_receipt = read_signed(
            CONTROL_BOOTSTRAP_RECEIPT_PATH,
            required=False,
        )
        if existing_receipt is not None:
            raise SchemaControlError(
                "control_bootstrap_receipt_without_tables"
            )
        generation = 1
        write_auth_snapshot(
            context=context,
            actor_user_id=actor_user_id,
            actor_subject=actor_subject,
            actor_role=actor_role,
            generation=generation,
        )
        write_signed(
            CONTROL_BOOTSTRAP_RECEIPT_PATH,
            _bootstrap_receipt_payload(
                context=context,
                generation=generation,
                state="prepared",
                control_shape_fingerprint=None,
            ),
        )
        db.execute(text(CONTROL_DDL))
        control_shape = verify_control_shape(db)
        now = naive_utc_now()
        db.execute(
            text(
                """
                INSERT INTO schema_migration_control (
                    id, fencing_generation, owner_attempt_id, request_id,
                    installed_version, installed_commit,
                    source_schema_version, target_commit, target_release,
                    target_schema_version, registry_fingerprint,
                    plan_fingerprint, source_shape_fingerprint,
                    control_definition_fingerprint, state,
                    lease_expires_at, updated_at
                ) VALUES (
                    'current', :generation, :owner_attempt_id, :request_id,
                    :installed_version, :installed_commit,
                    :source_schema_version, :target_commit, :target_release,
                    :target_schema_version, :registry_fingerprint,
                    :plan_fingerprint, :source_shape_fingerprint,
                    :control_definition_fingerprint, 'prepared',
                    :lease_expires_at, :updated_at
                )
                """
            ),
            {
                "generation": generation,
                "owner_attempt_id": context.admission_attempt_id,
                "request_id": context.request_id,
                "installed_version": context.installed_version,
                "installed_commit": context.installed_commit,
                "source_schema_version": context.source_schema_version,
                "target_commit": context.target_commit,
                "target_release": context.target_release,
                "target_schema_version": TARGET_SCHEMA_VERSION,
                "registry_fingerprint": context.registry_fingerprint,
                "plan_fingerprint": context.plan_fingerprint,
                "source_shape_fingerprint": context.source_shape_fingerprint,
                "control_definition_fingerprint": (
                    CONTROL_DEFINITION_FINGERPRINT
                ),
                "lease_expires_at": now + timedelta(minutes=15),
                "updated_at": now,
            },
        )
        attempt_id, attempt_status = start_attempt(
            db,
            context=context,
            generation=generation,
            transition_id=CONTROL_BOOTSTRAP_MIGRATION_ID,
            previous_version=context.source_schema_version,
            target_version=context.source_schema_version,
            definition_fingerprint=CONTROL_DEFINITION_FINGERPRINT,
            before_shape_fingerprint=context.source_shape_fingerprint,
        )
        if attempt_status != "started":
            raise SchemaControlError("control_bootstrap_attempt_preexisting")
        finish_attempt(
            db,
            attempt_id=attempt_id,
            status="applied",
            after_shape_fingerprint=control_shape,
            details={"control_shape_verified": True},
        )
        db.commit()
        write_signed(
            CONTROL_BOOTSTRAP_RECEIPT_PATH,
            _bootstrap_receipt_payload(
                context=context,
                generation=generation,
                state="adopted",
                control_shape_fingerprint=control_shape,
            ),
        )
        return generation

    row = db.execute(
        text(
            "SELECT * FROM schema_migration_control "
            "WHERE id='current' FOR UPDATE"
        )
    ).mappings().one()
    receipt = read_signed(CONTROL_BOOTSTRAP_RECEIPT_PATH)
    assert receipt is not None
    (
        receipt_context,
        control_shape,
    ) = _validate_persistent_bootstrap_evidence(
        db,
        receipt,
        current_generation=int(row["fencing_generation"]),
        allow_prepared=True,
    )
    if receipt.get("state") == "prepared":
        write_signed(
            CONTROL_BOOTSTRAP_RECEIPT_PATH,
            _bootstrap_receipt_payload(
                context=receipt_context,
                generation=int(receipt["fencing_generation"]),
                state="adopted",
                control_shape_fingerprint=control_shape,
            ),
        )

    same_admission = (
        str(row["owner_attempt_id"]) == context.admission_attempt_id
        and str(row["request_id"]) == context.request_id
    )
    if same_admission:
        generation = int(row["fencing_generation"])
    else:
        prior_state = str(row["state"])
        if prior_state == "completed":
            validate_exact_target_noop(db)
        elif prior_state == "failed":
            if (
                context.request.get("retry_of_request_id")
                != str(row["request_id"])
                or not context.request.get("migration_attempt_id")
            ):
                raise SchemaControlError(
                    "migration_control_failed_rollover_forbidden"
                )
        else:
            raise SchemaControlError(
                "migration_control_active_rollover_forbidden"
            )
        generation = int(row["fencing_generation"]) + 1
        updated = db.execute(
            text(
                """
                UPDATE schema_migration_control
                SET fencing_generation=:generation,
                    owner_attempt_id=:owner_attempt_id,
                    request_id=:request_id,
                    installed_version=:installed_version,
                    installed_commit=:installed_commit,
                    source_schema_version=:source_schema_version,
                    target_commit=:target_commit,
                    target_release=:target_release,
                    target_schema_version=:target_schema_version,
                    registry_fingerprint=:registry_fingerprint,
                    plan_fingerprint=:plan_fingerprint,
                    source_shape_fingerprint=:source_shape_fingerprint,
                    control_definition_fingerprint=:control_definition_fingerprint,
                    state='prepared',
                    lease_expires_at=:lease_expires_at,
                    updated_at=:updated_at
                WHERE id='current'
                  AND fencing_generation=:prior_generation
                  AND owner_attempt_id=:prior_owner_attempt_id
                  AND request_id=:prior_request_id
                  AND state=:prior_state
                """
            ),
            {
                "generation": generation,
                "owner_attempt_id": context.admission_attempt_id,
                "request_id": context.request_id,
                "installed_version": context.installed_version,
                "installed_commit": context.installed_commit,
                "source_schema_version": context.source_schema_version,
                "target_commit": context.target_commit,
                "target_release": context.target_release,
                "target_schema_version": TARGET_SCHEMA_VERSION,
                "registry_fingerprint": context.registry_fingerprint,
                "plan_fingerprint": context.plan_fingerprint,
                "source_shape_fingerprint": (
                    context.source_shape_fingerprint
                ),
                "control_definition_fingerprint": (
                    CONTROL_DEFINITION_FINGERPRINT
                ),
                "lease_expires_at": naive_utc_now()
                + timedelta(minutes=15),
                "updated_at": naive_utc_now(),
                "prior_generation": int(row["fencing_generation"]),
                "prior_owner_attempt_id": str(row["owner_attempt_id"]),
                "prior_request_id": str(row["request_id"]),
                "prior_state": prior_state,
            },
        )
        if updated.rowcount != 1:
            raise SchemaControlError(
                "migration_control_rollover_fence_lost"
            )
        db.commit()
    write_auth_snapshot(
        context=context,
        actor_user_id=actor_user_id,
        actor_subject=actor_subject,
        actor_role=actor_role,
        generation=generation,
    )
    return generation


def update_control_state(
    db: Session,
    *,
    context: UpdateContext,
    generation: int,
    state: str,
) -> None:
    if state not in {"prepared", "recovering", "migrating", "completed", "failed"}:
        raise SchemaControlError("migration_control_state_invalid")
    result = db.execute(
        text(
            """
            UPDATE schema_migration_control
            SET state=:state,
                lease_expires_at=:lease_expires_at,
                updated_at=:updated_at
            WHERE id='current'
              AND owner_attempt_id=:owner_attempt_id
              AND request_id=:request_id
              AND fencing_generation=:generation
              AND target_commit=:target_commit
              AND plan_fingerprint=:plan_fingerprint
            """
        ),
        {
            "state": state,
            "lease_expires_at": naive_utc_now() + timedelta(minutes=15),
            "updated_at": naive_utc_now(),
            "owner_attempt_id": context.admission_attempt_id,
            "request_id": context.request_id,
            "generation": generation,
            "target_commit": context.target_commit,
            "plan_fingerprint": context.plan_fingerprint,
        },
    )
    if result.rowcount != 1:
        raise SchemaControlError("migration_control_fence_lost")


def validate_stage_receipt_payload(
    payload: dict[str, Any],
    *,
    context: UpdateContext,
    generation: int,
    expected_state: str | None = None,
) -> dict[str, Any]:
    expected = {
        "schema_version",
        "request_id",
        "admission_attempt_id",
        "target_version",
        "target_commit",
        "target_schema_version",
        "registry_fingerprint",
        "plan_fingerprint",
        "fencing_generation",
        "attempt_id",
        "state",
        "phase",
        "retryable",
        "error_code",
        "summary",
        "operator_action",
        "details",
        "updated_at",
    }
    if (
        type(payload) is not dict
        or set(payload) != expected
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise SchemaControlError("stage_receipt_fields_invalid")
    for key, max_length in (
        ("request_id", 80),
        ("admission_attempt_id", 64),
        ("target_version", 80),
        ("target_commit", 40),
        ("attempt_id", 64),
        ("state", 24),
        ("phase", 80),
        ("error_code", 120),
        ("summary", 300),
        ("operator_action", 300),
    ):
        exact_string(
            payload.get(key),
            code=f"stage_receipt_{key}_invalid",
            max_length=max_length,
            allow_empty=key == "error_code",
        )
    if (
        not REQUEST_RE.fullmatch(payload["request_id"])
        or not ATTEMPT_RE.fullmatch(payload["admission_attempt_id"])
        or not ATTEMPT_RE.fullmatch(payload["attempt_id"])
        or not re.fullmatch(r"[0-9a-f]{40}", payload["target_commit"])
        or type(payload.get("target_schema_version")) is not int
        or payload["target_schema_version"] != TARGET_SCHEMA_VERSION
        or type(payload.get("fencing_generation")) is not int
        or payload["fencing_generation"] < 0
        or type(payload.get("retryable")) is not bool
        or type(payload.get("details")) is not dict
    ):
        raise SchemaControlError("stage_receipt_types_invalid")
    for key in ("registry_fingerprint", "plan_fingerprint"):
        value = exact_string(
            payload.get(key),
            code=f"stage_receipt_{key}_invalid",
            max_length=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise SchemaControlError(f"stage_receipt_{key}_invalid")
    if len(canonical_bytes(payload["details"])) > MAX_DETAILS_BYTES:
        raise SchemaControlError("stage_receipt_details_too_large")
    bindings = {
        "request_id": context.request_id,
        "admission_attempt_id": context.admission_attempt_id,
        "target_version": context.target_release,
        "target_commit": context.target_commit,
        "target_schema_version": TARGET_SCHEMA_VERSION,
        "registry_fingerprint": context.registry_fingerprint,
        "plan_fingerprint": context.plan_fingerprint,
        "fencing_generation": generation,
    }
    for key, value in bindings.items():
        if payload.get(key) != value:
            raise SchemaControlError(f"stage_receipt_{key}_mismatch")
    state = payload["state"]
    if state not in {
        "completed",
        "failed",
        "blocked",
        "recovery_required",
    }:
        raise SchemaControlError("stage_receipt_state_invalid")
    if expected_state is not None and state != expected_state:
        raise SchemaControlError("stage_receipt_state_mismatch")
    retryable = payload["retryable"]
    if retryable:
        classification = classify_retry(
            payload["error_code"],
            payload["details"].get("retry_evidence", {}),
        )
        if state != "failed" or not classification.retryable:
            raise SchemaControlError(
                "stage_receipt_retryable_invariant_invalid"
            )
    elif state == "completed" and payload["error_code"]:
        raise SchemaControlError(
            "stage_receipt_completed_error_invalid"
        )
    parse_utc(payload.get("updated_at"))
    return payload


def write_stage_receipt(
    path: Path,
    *,
    context: UpdateContext,
    generation: int,
    transition_attempt_id_value: str,
    state: str,
    retryable: bool,
    error_code: str,
    summary: str,
    operator_action: str,
    details: dict[str, Any] | None = None,
) -> None:
    if type(retryable) is not bool:
        raise SchemaControlError("stage_receipt_retryable_type_invalid")
    for value, code, max_length, allow_empty in (
        (state, "stage_receipt_state_invalid", 24, False),
        (error_code, "stage_receipt_error_code_invalid", 120, True),
        (summary, "stage_receipt_summary_invalid", 300, False),
        (
            operator_action,
            "stage_receipt_operator_action_invalid",
            300,
            False,
        ),
    ):
        exact_string(
            value,
            code=code,
            max_length=max_length,
            allow_empty=allow_empty,
        )
    details_value = {} if details is None else details
    if type(details_value) is not dict:
        raise SchemaControlError("stage_receipt_details_invalid")
    if retryable:
        classification = classify_retry(
            error_code,
            details_value.get("retry_evidence", {}),
        )
        if state != "failed" or not classification.retryable:
            raise SchemaControlError(
                "stage_receipt_retryable_invariant_invalid"
            )
    elif state in {"completed", "blocked", "recovery_required", "failed"}:
        pass
    else:
        raise SchemaControlError("stage_receipt_state_invalid")
    write_signed(
        path,
        {
            "schema_version": 1,
            "request_id": context.request_id,
            "admission_attempt_id": context.admission_attempt_id,
            "target_version": context.target_release,
            "target_commit": context.target_commit,
            "target_schema_version": TARGET_SCHEMA_VERSION,
            "registry_fingerprint": context.registry_fingerprint,
            "plan_fingerprint": context.plan_fingerprint,
            "fencing_generation": generation,
            "attempt_id": transition_attempt_id_value,
            "state": state,
            "phase": "preparing_database",
            "retryable": retryable,
            "error_code": error_code,
            "summary": summary,
            "operator_action": operator_action,
            "details": details_value,
            "updated_at": utc_now(),
        },
    )


def acquire_schema_lock(db: Session) -> int:
    backend_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
    if type(backend_pid) is not int or backend_pid <= 0:
        raise SchemaControlError("schema_pipeline_backend_pid_invalid")
    db.execute(text("SELECT pg_advisory_lock(660128)"))
    return backend_pid


def release_schema_lock(db: Session) -> None:
    try:
        db.execute(text("SELECT pg_advisory_unlock(660128)"))
        db.commit()
    except Exception:
        db.rollback()


def target_shape_is_exact(db: Session) -> tuple[bool, str]:
    actual = database_shape_fingerprint(db)
    if not re.fullmatch(r"[0-9a-f]{64}", TARGET_SHAPE_FINGERPRINT):
        raise SchemaControlError("target_shape_evidence_missing")
    return actual == TARGET_SHAPE_FINGERPRINT, actual


def classify_retry(
    reason: str,
    evidence: Mapping[str, Any],
) -> RetryClassification:
    expected = {
        "schema_version",
        "mutation_started",
        "physical_mutation_possible",
        "transaction_rolled_back",
        "rollback_verified",
        "schema_shape_unchanged",
        "history_unchanged",
        "canonical_transition_committed",
        "foreign_state_detected",
    }
    exact_types = (
        set(evidence) == expected
        and type(evidence.get("schema_version")) is int
        and evidence["schema_version"] == 1
        and all(
            type(evidence.get(key)) is bool
            for key in expected - {"schema_version"}
        )
    )
    retryable = bool(
        type(reason) is str
        and reason in SAFE_RETRY_REASONS
        and exact_types
        and not evidence["physical_mutation_possible"]
        and evidence["transaction_rolled_back"]
        and evidence["rollback_verified"]
        and evidence["schema_shape_unchanged"]
        and evidence["history_unchanged"]
        and not evidence["canonical_transition_committed"]
        and not evidence["foreign_state_detected"]
    )
    return RetryClassification(
        retryable=retryable,
        public_state="failed" if retryable else "recovery_required",
    )
