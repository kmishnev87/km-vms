from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from app.services.schema_update_types import SchemaControlError, UpdateContext, canonical_bytes


@dataclass(frozen=True)
class SchemaStageReceipts:
    """Validate results and emit them only through the supplied signing boundary."""

    TARGET_SCHEMA_VERSION: int
    MAX_DETAILS_BYTES: int
    REQUEST_RE: Any
    ATTEMPT_RE: Any
    classify_retry: Callable
    exact_string: Callable
    parse_utc: Callable
    utc_now: Callable
    write_signed: Callable

    def validate_stage_receipt_payload(self,
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
            self.exact_string(
                payload.get(key),
                code=f"stage_receipt_{key}_invalid",
                max_length=max_length,
                allow_empty=key == "error_code",
            )
        if (
            not self.REQUEST_RE.fullmatch(payload["request_id"])
            or not self.ATTEMPT_RE.fullmatch(payload["admission_attempt_id"])
            or not self.ATTEMPT_RE.fullmatch(payload["attempt_id"])
            or not re.fullmatch(r"[0-9a-f]{40}", payload["target_commit"])
            or type(payload.get("target_schema_version")) is not int
            or payload["target_schema_version"] != self.TARGET_SCHEMA_VERSION
            or type(payload.get("fencing_generation")) is not int
            or payload["fencing_generation"] < 0
            or type(payload.get("retryable")) is not bool
            or type(payload.get("details")) is not dict
        ):
            raise SchemaControlError("stage_receipt_types_invalid")
        for key in ("registry_fingerprint", "plan_fingerprint"):
            value = self.exact_string(
                payload.get(key),
                code=f"stage_receipt_{key}_invalid",
                max_length=64,
            )
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise SchemaControlError(f"stage_receipt_{key}_invalid")
        if len(canonical_bytes(payload["details"])) > self.MAX_DETAILS_BYTES:
            raise SchemaControlError("stage_receipt_details_too_large")
        bindings = {
            "request_id": context.request_id,
            "admission_attempt_id": context.admission_attempt_id,
            "target_version": context.target_release,
            "target_commit": context.target_commit,
            "target_schema_version": self.TARGET_SCHEMA_VERSION,
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
            classification = self.classify_retry(
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
        self.parse_utc(payload.get("updated_at"))
        return payload

    def write_stage_receipt(self,
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
            self.exact_string(
                value,
                code=code,
                max_length=max_length,
                allow_empty=allow_empty,
            )
        details_value = {} if details is None else details
        if type(details_value) is not dict:
            raise SchemaControlError("stage_receipt_details_invalid")
        if retryable:
            classification = self.classify_retry(
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
        self.write_signed(
            path,
            {
                "schema_version": 1,
                "request_id": context.request_id,
                "admission_attempt_id": context.admission_attempt_id,
                "target_version": context.target_release,
                "target_commit": context.target_commit,
                "target_schema_version": self.TARGET_SCHEMA_VERSION,
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
                "updated_at": self.utc_now(),
            },
        )
