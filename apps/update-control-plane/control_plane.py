from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


CONTROL_ROOT = Path(os.getenv("KMVMS_UPDATE_CONTROL_ROOT") or "/update-control")
PUBLIC_ROOT = Path(os.getenv("KMVMS_UPDATE_PUBLIC_ROOT") or "/update-public")
ROLE = str(os.getenv("KMVMS_CONTROL_ROLE") or "reader").strip().lower()
JWT_SECRET = str(os.getenv("JWT_SECRET") or "")
PORT = int(os.getenv("KMVMS_CONTROL_PORT") or "8080")
MAX_BYTES = 64 * 1024
MAX_DEPTH = 12
MAX_WIDTH = 80
MAX_KEY_LENGTH = 120
MAX_TIMESTAMP_LENGTH = 80
REQUEST_ID_RE = re.compile(r"^(?:update|stage609)-[0-9a-f]{32}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SAFE_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,100}$")
TERMINAL_HELPER_STATES = {"completed", "failed", "cancelled", "blocked"}
SAFE_RETRY_REASONS = frozenset(
    {
        "test_injected_preparation_failure_before_ddl",
        "test_injected_preparation_failure_after_ddl",
        "test_injected_retryable_schema_failure",
    }
)

AUTH_SNAPSHOT = CONTROL_ROOT / "schema-auth-snapshot.signed.json"
BOOTSTRAP_RECEIPT = CONTROL_ROOT / "schema-control-bootstrap.signed.json"
PREPARATION_RECEIPT = CONTROL_ROOT / "schema-preparation-receipt.signed.json"
RECOVERY_RECEIPT = CONTROL_ROOT / "operation-recovery-receipt.signed.json"
GATE_RECEIPT = CONTROL_ROOT / "schema-gate-receipt.signed.json"
FAILURE_PLANE = PUBLIC_ROOT / "update-failure-plane.signed.json"
HELPER_STATUS = CONTROL_ROOT / "update-status.json"
UPDATE_REQUEST = CONTROL_ROOT / "update-request.json"
RETRY_ADMISSION = CONTROL_ROOT / "update-retry-admission.signed.json"
RETRY_LOCK = threading.Lock()
CONTROLLER_READY = threading.Event()
TEST_FAULT_INJECTION = (
    str(os.getenv("KMVMS_TEST_FAULT_INJECTION") or "").strip() == "1"
)
RETRY_FAILURE_MODE = (
    str(os.getenv("KMVMS_TEST_RETRY_FAILURE_MODE") or "").strip().lower()
    if TEST_FAULT_INJECTION
    else ""
)


class ContractError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> float:
    if type(value) is not str:
        raise ContractError("timestamp_type_invalid")
    if (
        not value
        or len(value) > MAX_TIMESTAMP_LENGTH
        or "T" not in value
        or not (
            value.endswith("Z")
            or value.endswith("+00:00")
        )
    ):
        raise ContractError("timestamp_format_invalid")
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ContractError("timestamp_invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
    ):
        raise ContractError("timestamp_timezone_invalid")
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
        raise ContractError(code)
    return value


def validate_retry_invariant(
    *,
    state: Any,
    retryable: Any,
    reason: Any,
    details: Any | None = None,
    require_evidence: bool = True,
) -> None:
    if type(retryable) is not bool:
        raise ContractError("retryable_type_invalid")
    if retryable and state != "failed":
        raise ContractError("retryable_state_invalid")
    if not retryable:
        return
    if type(reason) is not str or reason not in SAFE_RETRY_REASONS:
        raise ContractError("retryable_reason_not_allowlisted")
    if not require_evidence:
        return
    if type(details) is not dict:
        raise ContractError("retryable_evidence_missing")
    evidence = details.get("retry_evidence")
    required = {
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
    if (
        type(evidence) is not dict
        or set(evidence) != required
        or type(evidence.get("schema_version")) is not int
        or evidence["schema_version"] != 1
        or any(
            type(evidence.get(key)) is not bool
            for key in required - {"schema_version"}
        )
        or evidence["physical_mutation_possible"]
        or not evidence["transaction_rolled_back"]
        or not evidence["rollback_verified"]
        or not evidence["schema_shape_unchanged"]
        or not evidence["history_unchanged"]
        or evidence["canonical_transition_committed"]
        or evidence["foreign_state_detected"]
    ):
        raise ContractError("retryable_evidence_invalid")


def no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ContractError(f"non_finite_json:{value}")


def validate_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ContractError("json_too_deep")
    if isinstance(value, dict):
        if len(value) > MAX_WIDTH:
            raise ContractError("json_too_wide")
        for key, item in value.items():
            if (
                type(key) is not str
                or len(key) > MAX_KEY_LENGTH
            ):
                raise ContractError("json_key_invalid")
            validate_depth(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_WIDTH:
            raise ContractError("json_list_too_long")
        for item in value:
            validate_depth(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ContractError("json_number_non_finite")


def load_json_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) <= 1 or len(raw) > MAX_BYTES:
        raise ContractError("json_size_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicate_object,
            parse_constant=reject_constant,
        )
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError("json_invalid") from exc
    if type(value) is not dict:
        raise ContractError("json_object_required")
    validate_depth(value)
    return value


def read_regular_json(path: Path, *, required: bool = True) -> dict[str, Any] | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise ContractError(f"{path.name}_missing")
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContractError(f"{path.name}_not_regular")
    if info.st_size <= 1 or info.st_size > MAX_BYTES:
        raise ContractError(f"{path.name}_size_invalid")
    if info.st_uid not in {0, os.getuid()}:
        raise ContractError(f"{path.name}_owner_invalid")
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ContractError(f"{path.name}_too_large")
    return load_json_bytes(raw)


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def control_key() -> bytes:
    if len(JWT_SECRET) < 16:
        raise ContractError("control_secret_unavailable")
    return hmac.new(
        JWT_SECRET.encode("utf-8"),
        b"KMVMS|stage660128|failure-control|v1",
        hashlib.sha256,
    ).digest()


def sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    signature = hmac.new(control_key(), canonical_bytes(payload), hashlib.sha256).hexdigest()
    return {"schema_version": 1, "payload": payload, "signature": signature}


def verify_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    if set(envelope) != {"schema_version", "payload", "signature"}:
        raise ContractError("signed_envelope_fields_invalid")
    if (
        type(envelope.get("schema_version")) is not int
        or envelope["schema_version"] != 1
    ):
        raise ContractError("signed_envelope_schema_invalid")
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if (
        type(payload) is not dict
        or type(signature) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        raise ContractError("signed_envelope_invalid")
    expected = hmac.new(control_key(), canonical_bytes(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ContractError("signed_envelope_signature_invalid")
    return payload


def read_signed(path: Path, *, required: bool = True) -> dict[str, Any] | None:
    envelope = read_regular_json(path, required=required)
    if envelope is None:
        return None
    return verify_envelope(envelope)


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(rendered) > MAX_BYTES:
        raise ContractError("control_output_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    info = os.lstat(path.parent)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ContractError("control_root_invalid")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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


def b64url_decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ContractError("jwt_segment_invalid")
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def decode_jwt(token: str) -> dict[str, Any]:
    if not token or len(token) > 8192:
        raise ContractError("bearer_token_invalid")
    parts = token.split(".")
    if len(parts) != 3:
        raise ContractError("bearer_token_invalid")
    header = load_json_bytes(b64url_decode(parts[0]))
    payload = load_json_bytes(b64url_decode(parts[1]))
    if header.get("alg") != "HS256":
        raise ContractError("bearer_algorithm_invalid")
    expected = hmac.new(
        JWT_SECRET.encode("utf-8"),
        f"{parts[0]}.{parts[1]}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        actual = b64url_decode(parts[2])
    except Exception as exc:
        raise ContractError("bearer_signature_invalid") from exc
    if not hmac.compare_digest(actual, expected):
        raise ContractError("bearer_signature_invalid")
    subject = payload.get("sub")
    expiry = payload.get("exp")
    issued = payload.get("iat")
    token_type = payload.get("typ")
    if not isinstance(subject, str) or not SAFE_SUBJECT_RE.fullmatch(subject):
        raise ContractError("bearer_subject_invalid")
    if type(expiry) is not int or expiry <= int(time.time()):
        raise ContractError("bearer_expired")
    if issued is not None and (type(issued) is not int or issued > int(time.time()) + 60):
        raise ContractError("bearer_issued_at_invalid")
    if token_type not in {None, "access"}:
        raise ContractError("bearer_type_invalid")
    return payload


def bearer_subject(headers: Any) -> str:
    value = headers.get("Authorization")
    if type(value) is not str or len(value) > 8192:
        raise ContractError("authorization_required")
    if not value.startswith("Bearer ") or value.count(" ") != 1:
        raise ContractError("authorization_required")
    subject = decode_jwt(value[7:]).get("sub")
    assert type(subject) is str
    return subject


def validate_failure_contract(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "request_id",
        "admission_attempt_id",
        "target_version",
        "target_commit",
        "target_schema_version",
        "registry_fingerprint",
        "plan_fingerprint",
        "actor_subject",
        "permission",
        "auth_expires_at",
        "fencing_generation",
        "state",
        "phase",
        "retryable",
        "helper_terminal",
        "updated_at",
        "error_code",
        "summary",
        "operator_action",
    }
    if (
        set(payload) != required
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise ContractError("failure_contract_fields_invalid")
    request_id = exact_string(
        payload.get("request_id"),
        code="failure_contract_request_invalid",
        max_length=80,
    )
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ContractError("failure_contract_request_invalid")
    target_commit = exact_string(
        payload.get("target_commit"),
        code="failure_contract_commit_invalid",
        max_length=40,
    )
    if not COMMIT_RE.fullmatch(target_commit):
        raise ContractError("failure_contract_commit_invalid")
    actor_subject = exact_string(
        payload.get("actor_subject"),
        code="failure_contract_subject_invalid",
        max_length=100,
    )
    if not SAFE_SUBJECT_RE.fullmatch(actor_subject):
        raise ContractError("failure_contract_subject_invalid")
    if payload.get("permission") != "manage_settings":
        raise ContractError("failure_contract_permission_invalid")
    if parse_utc(payload.get("auth_expires_at")) <= time.time():
        raise ContractError("failure_contract_expired")
    if (
        type(payload.get("fencing_generation")) is not int
        or payload["fencing_generation"] < 0
    ):
        raise ContractError("failure_contract_fencing_invalid")
    admission_attempt_id = exact_string(
        payload.get("admission_attempt_id"),
        code="failure_contract_attempt_invalid",
        max_length=64,
    )
    if not re.fullmatch(
        r"migration-attempt-[0-9a-f]{32}",
        admission_attempt_id,
    ):
        raise ContractError("failure_contract_attempt_invalid")
    if (
        type(payload.get("target_schema_version")) is not int
        or payload["target_schema_version"] != 8
    ):
        raise ContractError("failure_contract_schema_target_invalid")
    for key in ("registry_fingerprint", "plan_fingerprint"):
        value = exact_string(
            payload.get(key),
            code=f"failure_contract_{key}_invalid",
            max_length=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ContractError(f"failure_contract_{key}_invalid")
    state = exact_string(
        payload.get("state"),
        code="failure_contract_state_invalid",
        max_length=24,
    )
    if state not in {
        "initializing",
        "running",
        "failed",
        "recovery_required",
        "completed",
    }:
        raise ContractError("failure_contract_state_invalid")
    if type(payload.get("helper_terminal")) is not bool:
        raise ContractError("failure_contract_boolean_invalid")
    validate_retry_invariant(
        state=state,
        retryable=payload.get("retryable"),
        reason=payload.get("error_code"),
        require_evidence=False,
    )
    for key, max_length in (
        ("target_version", 80),
        ("phase", 80),
        ("error_code", 120),
        ("summary", 300),
        ("operator_action", 300),
    ):
        exact_string(
            payload.get(key),
            code=f"failure_contract_{key}_invalid",
            max_length=max_length,
            allow_empty=key in {"error_code"},
        )
    parse_utc(payload.get("updated_at"))
    return payload


def validate_stage_receipt(
    payload: dict[str, Any],
    *,
    auth: dict[str, Any],
) -> dict[str, Any]:
    required = {
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
        set(payload) != required
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise ContractError("stage_receipt_fields_invalid")
    request_id = exact_string(
        payload.get("request_id"),
        code="stage_receipt_request_id_invalid",
        max_length=80,
    )
    target_version = exact_string(
        payload.get("target_version"),
        code="stage_receipt_target_version_invalid",
        max_length=80,
    )
    target_commit = exact_string(
        payload.get("target_commit"),
        code="stage_receipt_target_commit_invalid",
        max_length=40,
    )
    if (
        request_id != auth.get("request_id")
        or target_version != auth.get("target_version")
        or target_commit != auth.get("target_commit")
        or payload.get("fencing_generation")
        != auth.get("fencing_generation")
    ):
        raise ContractError("stage_receipt_binding_invalid")
    if (
        not REQUEST_ID_RE.fullmatch(request_id)
        or not COMMIT_RE.fullmatch(target_commit)
    ):
        raise ContractError("stage_receipt_binding_invalid")
    if (
        type(payload.get("target_schema_version")) is not int
        or payload["target_schema_version"] != 8
    ):
        raise ContractError("stage_receipt_schema_target_invalid")
    for key in (
        "admission_attempt_id",
        "attempt_id",
    ):
        value = exact_string(
            payload.get(key),
            code=f"stage_receipt_{key}_invalid",
            max_length=64,
        )
        if not re.fullmatch(
            r"migration-attempt-[0-9a-f]{32}",
            value,
        ):
            raise ContractError(f"stage_receipt_{key}_invalid")
    for key in ("registry_fingerprint", "plan_fingerprint"):
        value = exact_string(
            payload.get(key),
            code=f"stage_receipt_{key}_invalid",
            max_length=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ContractError(f"stage_receipt_{key}_invalid")
    state = exact_string(
        payload.get("state"),
        code="stage_receipt_state_invalid",
        max_length=24,
    )
    if state not in {
        "completed",
        "failed",
        "blocked",
        "recovery_required",
    }:
        raise ContractError("stage_receipt_state_invalid")
    if type(payload.get("details")) is not dict:
        raise ContractError("stage_receipt_details_invalid")
    if len(canonical_bytes(payload["details"])) > 8 * 1024:
        raise ContractError("stage_receipt_details_too_large")
    for key, max_length in (
        ("phase", 80),
        ("error_code", 120),
        ("summary", 300),
        ("operator_action", 300),
    ):
        exact_string(
            payload.get(key),
            code=f"stage_receipt_{key}_invalid",
            max_length=max_length,
            allow_empty=key in {"error_code"},
        )
    if (
        type(payload.get("fencing_generation")) is not int
        or payload["fencing_generation"] < 0
    ):
        raise ContractError("stage_receipt_fencing_invalid")
    validate_retry_invariant(
        state=state,
        retryable=payload.get("retryable"),
        reason=payload.get("error_code"),
        details=payload.get("details"),
    )
    parse_utc(payload.get("updated_at"))
    return payload


def validate_bootstrap_receipt(
    payload: dict[str, Any],
    *,
    auth: dict[str, Any],
    stage: dict[str, Any],
) -> None:
    required = {
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
        set(payload) != required
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise ContractError("bootstrap_receipt_fields_invalid")
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
            code=f"bootstrap_receipt_{key}_invalid",
            max_length=max_length,
        )
    for key in (
        "target_schema_version",
        "source_schema_version",
        "fencing_generation",
    ):
        if type(payload.get(key)) is not int:
            raise ContractError(f"bootstrap_receipt_{key}_invalid")
    if (
        payload.get("request_id") != auth.get("request_id")
        or payload.get("target_release") != auth.get("target_version")
        or payload.get("target_commit") != auth.get("target_commit")
        or payload.get("fencing_generation")
        != auth.get("fencing_generation")
        or payload.get("admission_attempt_id")
        != stage.get("admission_attempt_id")
        or payload.get("target_schema_version")
        != stage.get("target_schema_version")
        or payload.get("registry_fingerprint")
        != stage.get("registry_fingerprint")
        or payload.get("plan_fingerprint")
        != stage.get("plan_fingerprint")
    ):
        raise ContractError("bootstrap_receipt_binding_invalid")
    if payload.get("state") != "adopted":
        raise ContractError("bootstrap_receipt_state_invalid")
    for key in (
        "source_shape_fingerprint",
        "registry_fingerprint",
        "plan_fingerprint",
        "control_definition_fingerprint",
        "control_shape_fingerprint",
    ):
        value = exact_string(
            payload.get(key),
            code=f"bootstrap_receipt_{key}_invalid",
            max_length=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ContractError(f"bootstrap_receipt_{key}_invalid")
    parse_utc(payload.get("updated_at"))


def controller_contract() -> dict[str, Any] | None:
    auth = read_signed(AUTH_SNAPSHOT, required=False)
    if not auth:
        return None
    expected_auth = {
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
        set(auth) != expected_auth
        or type(auth.get("schema_version")) is not int
        or auth["schema_version"] != 1
    ):
        raise ContractError("auth_snapshot_fields_invalid")
    request_id = exact_string(
        auth.get("request_id"),
        code="auth_snapshot_identity_invalid",
        max_length=80,
    )
    target_commit = exact_string(
        auth.get("target_commit"),
        code="auth_snapshot_identity_invalid",
        max_length=40,
    ).lower()
    actor_subject = exact_string(
        auth.get("actor_subject"),
        code="auth_snapshot_identity_invalid",
        max_length=100,
    )
    actor_user_id = exact_string(
        auth.get("actor_user_id"),
        code="auth_snapshot_identity_invalid",
        max_length=20,
    )
    exact_string(
        auth.get("target_version"),
        code="auth_snapshot_identity_invalid",
        max_length=80,
    )
    exact_string(
        auth.get("actor_role"),
        code="auth_snapshot_identity_invalid",
        max_length=20,
    )
    exact_string(
        auth.get("permission"),
        code="auth_snapshot_identity_invalid",
        max_length=40,
    )
    generation = auth.get("fencing_generation")
    if (
        not REQUEST_ID_RE.fullmatch(request_id)
        or not COMMIT_RE.fullmatch(target_commit)
        or not SAFE_SUBJECT_RE.fullmatch(actor_subject)
        or not actor_user_id.isdigit()
        or auth.get("actor_role") not in {"owner", "admin"}
        or type(generation) is not int
        or generation < 0
    ):
        raise ContractError("auth_snapshot_identity_invalid")
    if auth.get("permission") != "manage_settings":
        raise ContractError("auth_snapshot_permission_invalid")
    parse_utc(auth.get("issued_at"))
    if parse_utc(auth.get("expires_at")) <= time.time():
        raise ContractError("auth_snapshot_expired")

    current_stages: list[tuple[str, dict[str, Any]]] = []
    for name, path in (
        ("preparation", PREPARATION_RECEIPT),
        ("recovery", RECOVERY_RECEIPT),
        ("gate", GATE_RECEIPT),
    ):
        candidate = read_signed(path, required=False)
        if candidate is None:
            continue
        if candidate.get("request_id") != request_id:
            continue
        current_stages.append(
            (name, validate_stage_receipt(candidate, auth=auth))
        )
    order = [name for name, _payload in current_stages]
    if "recovery" in order and (
        "preparation" not in order
        or current_stages[order.index("preparation")][1].get("state")
        != "completed"
    ):
        raise ContractError("stage_receipt_preparation_predecessor_invalid")
    if "gate" in order and (
        "recovery" not in order
        or current_stages[order.index("recovery")][1].get("state")
        != "completed"
    ):
        raise ContractError("stage_receipt_recovery_predecessor_invalid")
    stage = current_stages[-1][1] if current_stages else None

    if generation == 0:
        if not stage or order != ["preparation"] or stage.get("state") not in {
            "blocked",
            "failed",
            "recovery_required",
        }:
            raise ContractError("prebootstrap_failure_receipt_invalid")
    elif generation > 0:
        if stage is None:
            raise ContractError("current_stage_receipt_missing")
        bootstrap = read_signed(BOOTSTRAP_RECEIPT)
        assert bootstrap is not None
        validate_bootstrap_receipt(bootstrap, auth=auth, stage=stage)

    helper = read_regular_json(HELPER_STATUS, required=False) or {}
    if helper:
        if (
            type(helper.get("schema_version")) is not int
            or helper["schema_version"] != 1
        ):
            raise ContractError("helper_status_schema_invalid")
        helper_request_id = exact_string(
            helper.get("request_id"),
            code="helper_status_request_invalid",
            max_length=80,
        )
        helper_status = exact_string(
            helper.get("status"),
            code="helper_status_state_invalid",
            max_length=40,
        )
        helper_phase = exact_string(
            helper.get("phase"),
            code="helper_status_phase_invalid",
            max_length=80,
        )
        helper_current_step = exact_string(
            helper.get("current_step"),
            code="helper_status_step_invalid",
            max_length=80,
        )
    else:
        helper_request_id = ""
        helper_status = ""
        helper_phase = ""
        helper_current_step = ""
    helper_matches = helper_request_id == request_id
    helper_state = helper_status if helper_matches else ""
    helper_terminal = helper_state in TERMINAL_HELPER_STATES
    phase = (
        helper_phase
        or helper_current_step
        or "preparing_database"
    )
    state = "running"
    retryable = False
    error_code = ""
    summary = "Automatic database preparation is in progress."
    operator_action = "Wait for the update operation to finish."

    assert stage is not None
    stage_state = stage["state"]
    stage_failed = stage_state in {
        "failed",
        "blocked",
        "recovery_required",
    }
    if stage_failed:
        phase = stage["phase"] or "preparing_database"
        if helper_terminal:
            state = (
                "failed"
                if stage.get("retryable")
                else "recovery_required"
            )
            retryable = stage["retryable"]
            error_code = stage["error_code"] or "schema_gate_failed"
            summary = (
                stage["summary"]
                or "Automatic database preparation failed."
            )
            operator_action = (
                stage["operator_action"]
                or "Review the update status."
            )
        else:
            # A stage receipt is written before the immutable legacy updater
            # returns from Compose and records its own terminal result.  Do not
            # expose that intermediate pair as public ``failed`` with retry
            # disabled: legacy clients treat it as a final, non-retryable
            # blocker.  The stage remains the authority for the eventual
            # failure, but publication stays non-terminal until the sole
            # updater owner has durably stopped.
            state = "running"
            retryable = False
            error_code = ""
            summary = (
                "Database preparation stopped at a verified safe point; "
                "the updater is finalizing the operation."
            )
            operator_action = (
                "Wait for the update operation to finish."
            )
    elif stage_state == "completed":
        summary = (
            "Database preparation completed; target services are being "
            "verified."
        )

    if helper_terminal:
        if helper_state == "completed":
            if state in {"failed", "recovery_required"}:
                raise ContractError("helper_stage_terminal_contradiction")
            state = "completed"
            retryable = False
            error_code = ""
            summary = "Update completed and target services were verified."
            operator_action = "Reload the product page."
        elif not stage_failed:
            state = "failed"
            retryable = False
            error_code = "post_schema_update_failed"
            summary = "The update failed after database preparation."
            operator_action = (
                "Review the sanitized update status before retrying."
            )

    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "admission_attempt_id": stage["admission_attempt_id"],
        "target_version": auth["target_version"],
        "target_commit": target_commit,
        "target_schema_version": stage["target_schema_version"],
        "registry_fingerprint": stage["registry_fingerprint"],
        "plan_fingerprint": stage["plan_fingerprint"],
        "actor_subject": actor_subject,
        "permission": "manage_settings",
        "auth_expires_at": auth["expires_at"],
        "fencing_generation": generation,
        "state": state,
        "phase": phase,
        "retryable": retryable,
        "helper_terminal": helper_terminal,
        "updated_at": utc_now(),
        "error_code": error_code,
        "summary": summary,
        "operator_action": operator_action,
    }
    return validate_failure_contract(payload)


def controller_loop() -> None:
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)
    Path("/tmp/controller-ready").write_text("ready\n", encoding="utf-8")
    CONTROLLER_READY.set()
    last_rendered = b""
    while True:
        try:
            contract = controller_contract()
            if contract is None:
                FAILURE_PLANE.unlink(missing_ok=True)
                last_rendered = b""
            else:
                envelope = sign_payload(contract)
                rendered = canonical_bytes(envelope)
                if rendered != last_rendered:
                    atomic_write(FAILURE_PLANE, envelope)
                    last_rendered = rendered
        except Exception:
            FAILURE_PLANE.unlink(missing_ok=True)
            last_rendered = b""
        time.sleep(0.5)


def legacy_check_payload(contract: dict[str, Any]) -> dict[str, Any]:
    retryable = bool(contract["retryable"] and contract["helper_terminal"])
    completed = contract["state"] == "completed"
    in_progress = contract["state"] == "running"
    latest = {
        "version": contract["target_version"],
        "latest_version": contract["target_version"],
        "commit": contract["target_commit"],
        "commit_sha": contract["target_commit"],
        "requires_backup": False,
        "requires_manual_action": False,
        "requires_migration": False,
    }
    return {
        "schema_version": 1,
        "status": (
            "current"
            if completed
            else "update_available"
            if retryable or in_progress
            else "blocked"
        ),
        "reason": (
            "current"
            if completed
            else "update_in_progress"
            if in_progress
            else contract["error_code"] or contract["state"]
        ),
        "summary": contract["summary"],
        "update_available": retryable or in_progress,
        "can_apply": retryable,
        "can_apply_from_ui": retryable,
        "apply_supported": retryable,
        "is_current": completed,
        "latest": latest,
        "latest_release": latest,
        "current_version": None,
        "installed_version": None,
        "provider_check_performed": False,
        "frozen_failure_plane": True,
        "blockers": (
            []
            if retryable or completed or in_progress
            else [{"code": contract["error_code"] or contract["state"]}]
        ),
        "warnings": (
            [{"code": "update_in_progress"}]
            if in_progress
            else []
        ),
    }


def legacy_apply_payload(contract: dict[str, Any]) -> dict[str, Any]:
    state = contract["state"]
    public_status = "completed" if state == "completed" else "failed" if state == "failed" else "blocked" if state == "recovery_required" else "rebuilding"
    return {
        "schema_version": 1,
        "request_id": contract["request_id"],
        "status": public_status,
        "effective_status": public_status,
        "phase": contract["phase"],
        "current_step": contract["phase"],
        "updated_at": contract["updated_at"],
        "expected_commit": contract["target_commit"],
        "installed_commit": contract["target_commit"] if state == "completed" else None,
        "commit_verified": state == "completed",
        "can_cancel": False,
        "retryable": bool(contract["retryable"] and contract["helper_terminal"]),
        "retry_supported": bool(contract["retryable"] and contract["helper_terminal"]),
        "rollback_supported": False,
        "frozen_failure_plane": True,
        "source": {
            "kind": "github-tarball",
            "commit": contract["target_commit"],
            "ref": contract["target_commit"],
            "apply_ref": contract["target_commit"],
        },
        "error": None
        if state == "completed"
        else {
            "category": contract["error_code"] or state,
            "message": contract["summary"],
            "operator_action": contract["operator_action"],
        },
        "steps": [
            {"name": "request", "status": "completed"},
            {"name": "acquire_source", "status": "completed"},
            {"name": "overlay", "status": "completed"},
            {"name": "rebuilding", "status": "failed" if state in {"failed", "recovery_required"} else "running"},
            {"name": "health_check", "status": "pending" if state != "completed" else "completed"},
        ],
    }


def active_contract(headers: Any) -> tuple[dict[str, Any], str]:
    contract = read_signed(FAILURE_PLANE)
    assert contract is not None
    contract = validate_failure_contract(contract)
    subject = bearer_subject(headers)
    if subject != contract["actor_subject"]:
        raise ContractError("foreign_actor_forbidden")
    return contract, subject


def read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = handler.headers.get("Content-Length")
    if content_length is None or not content_length.isdigit():
        raise ContractError("content_length_required")
    length = int(content_length)
    if length < 2 or length > 4096:
        raise ContractError("request_body_size_invalid")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise ContractError("request_body_incomplete")
    return load_json_bytes(raw)


def create_retry(contract: dict[str, Any], subject: str, body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"confirm", "expected_manifest_version", "expected_manifest_commit"}
    if set(body) - allowed or body.get("confirm") is not True:
        raise ContractError("update_retry_payload_invalid")
    expected_version = exact_string(
        body.get("expected_manifest_version"),
        code="update_retry_target_version_mismatch",
        max_length=80,
    )
    expected_commit = exact_string(
        body.get("expected_manifest_commit"),
        code="update_retry_target_commit_mismatch",
        max_length=40,
    )
    if expected_version != contract["target_version"]:
        raise ContractError("update_retry_target_version_mismatch")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", expected_commit)
        or expected_commit != contract["target_commit"]
    ):
        raise ContractError("update_retry_target_commit_mismatch")

    # Reconcile a duplicate only from the signed, durable admission record.  This
    # check intentionally precedes the live helper-terminal gate: immediately
    # after the first accepted retry the controller switches helper_terminal to
    # false, while an in-flight duplicate must still resolve to the already
    # admitted request/attempt rather than create work or return an ambiguous 409.
    contract_fingerprint = hashlib.sha256(
        canonical_bytes(
            {
                "schema_version": 1,
                "request_id": contract["request_id"],
                "admission_attempt_id": contract[
                    "admission_attempt_id"
                ],
                "target_version": contract["target_version"],
                "target_commit": contract["target_commit"],
                "target_schema_version": contract[
                    "target_schema_version"
                ],
                "registry_fingerprint": contract[
                    "registry_fingerprint"
                ],
                "plan_fingerprint": contract["plan_fingerprint"],
                "actor_subject": contract["actor_subject"],
                "fencing_generation": contract["fencing_generation"],
                "state": contract["state"],
                "error_code": contract["error_code"],
            }
        )
    ).hexdigest()
    prior = read_signed(RETRY_ADMISSION, required=False)
    if prior:
        expected_prior = {
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
            set(prior) != expected_prior
            or type(prior.get("schema_version")) is not int
            or prior["schema_version"] != 1
        ):
            raise ContractError("retry_admission_fields_invalid")
        for key in (
            "contract_fingerprint",
            "original_request_fingerprint",
            "registry_fingerprint",
            "plan_fingerprint",
        ):
            value = exact_string(
                prior.get(key),
                code=f"retry_admission_{key}_invalid",
                max_length=64,
            )
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ContractError(f"retry_admission_{key}_invalid")
        prior_original_request_id = exact_string(
            prior.get("original_request_id"),
            code="retry_admission_identity_invalid",
            max_length=80,
        )
        prior_request_id = exact_string(
            prior.get("request_id"),
            code="retry_admission_identity_invalid",
            max_length=80,
        )
        prior_attempt_id = exact_string(
            prior.get("attempt_id"),
            code="retry_admission_identity_invalid",
            max_length=64,
        )
        prior_actor = exact_string(
            prior.get("actor_subject"),
            code="retry_admission_identity_invalid",
            max_length=100,
        )
        prior_target_version = exact_string(
            prior.get("target_version"),
            code="retry_admission_identity_invalid",
            max_length=80,
        )
        prior_target_commit = exact_string(
            prior.get("target_commit"),
            code="retry_admission_identity_invalid",
            max_length=40,
        )
        if (
            not REQUEST_ID_RE.fullmatch(prior_original_request_id)
            or not re.fullmatch(
                r"update-[0-9a-f]{32}",
                prior_request_id,
            )
            or not re.fullmatch(
                r"migration-attempt-[0-9a-f]{32}",
                prior_attempt_id,
            )
            or not SAFE_SUBJECT_RE.fullmatch(prior_actor)
            or not re.fullmatch(r"[0-9a-f]{40}", prior_target_commit)
            or type(prior.get("target_schema_version")) is not int
            or prior["target_schema_version"] != 8
            or type(prior.get("fencing_generation")) is not int
            or prior["fencing_generation"] < 0
            or type(prior.get("retry_request")) is not dict
        ):
            raise ContractError("retry_admission_identity_invalid")
        parse_utc(prior.get("accepted_at"))
        if (
            prior.get("contract_fingerprint") == contract_fingerprint
            and prior_actor == subject
            and prior_original_request_id == contract["request_id"]
            and prior_target_version == contract["target_version"]
            and prior_target_commit == contract["target_commit"]
            and prior.get("target_schema_version")
            == contract["target_schema_version"]
            and prior.get("registry_fingerprint")
            == contract["registry_fingerprint"]
            and prior.get("plan_fingerprint")
            == contract["plan_fingerprint"]
            and prior.get("fencing_generation") == contract["fencing_generation"]
        ):
            attempt_id = prior_attempt_id
            request_id = prior_request_id
            retry_request = prior.get("retry_request")
            assert type(retry_request) is dict
            if (
                retry_request.get("request_id") == request_id
                and retry_request.get("migration_attempt_id")
                == attempt_id
                and retry_request.get("retry_of_request_id")
                == contract["request_id"]
            ):
                current_request = read_regular_json(UPDATE_REQUEST)
                assert current_request is not None
                current_fingerprint = hashlib.sha256(
                    canonical_bytes(current_request)
                ).hexdigest()
                retry_fingerprint = hashlib.sha256(
                    canonical_bytes(retry_request)
                ).hexdigest()
                if current_fingerprint == retry_fingerprint:
                    pass
                elif current_fingerprint == prior.get(
                    "original_request_fingerprint"
                ):
                    atomic_write(UPDATE_REQUEST, retry_request)
                else:
                    raise ContractError(
                        "retry_request_reconciliation_conflict"
                    )
                return {
                    "accepted": True,
                    "status": "queued",
                    "request_id": request_id,
                    "migration_attempt_id": attempt_id,
                    "idempotent_replay": True,
                }
            raise ContractError("retry_admission_identity_invalid")

    if contract["state"] != "failed" or not contract["retryable"] or not contract["helper_terminal"]:
        raise ContractError("update_retry_not_available")

    original = read_regular_json(UPDATE_REQUEST)
    assert original is not None
    original_request_id = exact_string(
        original.get("request_id"),
        code="update_retry_original_request_invalid",
        max_length=80,
    )
    if (
        not REQUEST_ID_RE.fullmatch(original_request_id)
        or original_request_id != contract["request_id"]
    ):
        raise ContractError("update_retry_original_request_mismatch")
    source = original.get("source")
    requested_by = original.get("requested_by")
    if type(source) is not dict or type(requested_by) is not dict:
        raise ContractError("update_retry_original_request_invalid")
    if set(source) != {
        "kind",
        "channel",
        "version",
        "commit",
        "apply_ref",
        "ref",
        "repo",
        "source_type",
    }:
        raise ContractError("update_retry_original_source_invalid")
    if set(requested_by) not in (
        {"user_id", "role"},
        {"user_id", "username", "role", "ip_address", "user_agent"},
    ):
        raise ContractError("update_retry_original_actor_invalid")
    source_commit = exact_string(
        source.get("commit"),
        code="update_retry_original_target_mismatch",
        max_length=40,
    )
    source_apply_ref = exact_string(
        source.get("apply_ref"),
        code="update_retry_original_source_invalid",
        max_length=40,
    )
    source_repo = exact_string(
        source.get("repo"),
        code="update_retry_original_source_invalid",
        max_length=160,
    )
    if (
        not re.fullmatch(r"[0-9a-f]{40}", source_commit)
        or source_commit != contract["target_commit"]
    ):
        raise ContractError("update_retry_original_target_mismatch")
    if (
        source_apply_ref != contract["target_commit"]
        or source_repo != "kmishnev87/km-vms"
    ):
        raise ContractError("update_retry_original_source_invalid")
    for key, max_length in (
        ("kind", 40),
        ("channel", 40),
        ("version", 80),
        ("ref", 120),
        ("source_type", 40),
    ):
        exact_string(
            source.get(key),
            code="update_retry_original_source_invalid",
            max_length=max_length,
        )
    user_id = exact_string(
        requested_by.get("user_id"),
        code="update_retry_original_actor_invalid",
        max_length=20,
    )
    role = exact_string(
        requested_by.get("role"),
        code="update_retry_original_actor_invalid",
        max_length=20,
    )
    if not user_id.isdigit() or role not in {"owner", "admin"}:
        raise ContractError("update_retry_original_actor_invalid")
    if "username" in requested_by:
        username = exact_string(
            requested_by.get("username"),
            code="update_retry_original_actor_invalid",
            max_length=100,
        )
        if not SAFE_SUBJECT_RE.fullmatch(username):
            raise ContractError("update_retry_original_actor_invalid")
        for key, max_length in (("ip_address", 80), ("user_agent", 400)):
            exact_string(
                requested_by.get(key),
                code="update_retry_original_actor_invalid",
                max_length=max_length,
                allow_empty=True,
            )

    new_request_id = "update-" + uuid.uuid4().hex
    attempt_id = "migration-attempt-" + uuid.uuid4().hex
    retry_request = {
        "schema_version": 1,
        "request_id": new_request_id,
        "requested_at": utc_now(),
        "requested_by": dict(requested_by),
        "intent": "apply_update",
        "confirmed": True,
        "source": dict(source),
        "preflight_required": True,
        "status_path": "data/update-control/update-status.json",
        "retry_of_request_id": contract["request_id"],
        "migration_attempt_id": attempt_id,
    }
    admission = {
        "schema_version": 1,
        "contract_fingerprint": contract_fingerprint,
        "original_request_id": contract["request_id"],
        "original_request_fingerprint": hashlib.sha256(
            canonical_bytes(original)
        ).hexdigest(),
        "request_id": new_request_id,
        "attempt_id": attempt_id,
        "actor_subject": subject,
        "target_version": contract["target_version"],
        "target_commit": contract["target_commit"],
        "target_schema_version": contract["target_schema_version"],
        "registry_fingerprint": contract["registry_fingerprint"],
        "plan_fingerprint": contract["plan_fingerprint"],
        "fencing_generation": contract["fencing_generation"],
        "retry_request": retry_request,
        "accepted_at": utc_now(),
    }
    if RETRY_FAILURE_MODE == "before-admission":
        raise ContractError("test_injected_retry_before_admission")
    atomic_write(RETRY_ADMISSION, sign_payload(admission))
    if RETRY_FAILURE_MODE == "after-admission-before-request":
        raise SystemExit("test_injected_retry_after_admission")
    atomic_write(UPDATE_REQUEST, retry_request)
    return {
        "accepted": True,
        "status": "queued",
        "request_id": new_request_id,
        "migration_attempt_id": attempt_id,
        "idempotent_replay": False,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "KMVMSFailureControl/1"
    sys_version = ""

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(rendered)))
        self.end_headers()
        self.wfile.write(rendered)

    def reject(self, code: str, status_code: int = 409) -> None:
        self.send_json(
            status_code,
            {
                "detail": {
                    "code": code[:120],
                    "message": "The update action is unavailable for the current trusted state.",
                    "retryable": False,
                }
            },
        )

    def do_GET(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if request_path.startswith("/api/"):
            request_path = request_path[4:]
        if request_path == "/health":
            if ROLE == "controller-retry" and not CONTROLLER_READY.is_set():
                self.send_json(
                    503,
                    {"status": "starting", "role": ROLE},
                )
                return
            self.send_json(200, {"status": "ok", "role": ROLE})
            return
        if ROLE != "reader" or request_path not in {
            "/system/update/status",
            "/system/update/apply/status",
        }:
            self.send_json(404, {"detail": {"code": "not_found"}})
            return
        try:
            contract, _subject = active_contract(self.headers)
            if request_path == "/system/update/status":
                self.send_json(200, legacy_check_payload(contract))
            else:
                self.send_json(200, legacy_apply_payload(contract))
        except ContractError as exc:
            code = str(exc)
            status = 401 if code.startswith(("authorization_", "bearer_")) else 403 if code == "foreign_actor_forbidden" else 503
            self.send_json(status, {"detail": {"code": code[:120], "message": "Update status is not available."}})

    def do_POST(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if request_path.startswith("/api/"):
            request_path = request_path[4:]
        if ROLE != "controller-retry":
            self.send_json(404, {"detail": {"code": "not_found"}})
            return
        if request_path == "/system/update/apply/cancel":
            self.reject("update_cancel_unavailable_during_schema_gate")
            return
        if request_path == "/system/update/check":
            self.reject("update_check_unavailable_during_schema_gate")
            return
        if request_path != "/system/update/apply":
            self.send_json(404, {"detail": {"code": "not_found"}})
            return
        try:
            contract, subject = active_contract(self.headers)
            body = read_body(self)
            with RETRY_LOCK:
                result = create_retry(contract, subject, body)
            result["apply_status"] = {
                "schema_version": 1,
                "request_id": result["request_id"],
                "status": "queued",
                "phase": "queued",
                "current_step": "queued",
                "can_cancel": False,
                "retryable": False,
            }
            self.send_json(202, result)
        except ContractError as exc:
            code = str(exc)
            status = 401 if code.startswith(("authorization_", "bearer_")) else 403 if code == "foreign_actor_forbidden" else 409
            self.reject(code, status)


def serve() -> None:
    if ROLE not in {"reader", "controller-retry"}:
        raise SystemExit("unsupported_http_role")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


def main() -> None:
    if ROLE == "controller-retry":
        controller = threading.Thread(
            target=controller_loop,
            name="update-status-projector",
            daemon=True,
        )
        controller.start()
    serve()


if __name__ == "__main__":
    main()
