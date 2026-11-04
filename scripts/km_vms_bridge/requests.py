"""One request/status validation contract shared by all bridge entry points."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Any
from .contracts import MAX_LEGACY_ADMISSION_ENTRIES, MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, REQUEST_ID_RE, PROJECT_NAME_RE, HELPER_IMAGE_RE, IMAGE_ID_RE, COMMIT_SHA_RE, SUBMISSION_ID_RE, LEGACY_HISTORICAL_REQUEST_FIELDS, LEGACY_SNAPSHOT_REQUEST_FIELDS, SCHEMA_RETRY_REQUEST_FIELDS, CURRENT_SINGLE_REQUEST_FIELDS, NORMALIZED_REQUEST_FIELDS, TERMINAL_STATUSES, INACTIVE_STATUSES, KNOWN_STATUSES, RECEIPT_STATUSES, UNBOUND_STATUS_ALLOWED, BridgeError

from .state import read_json_object

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
