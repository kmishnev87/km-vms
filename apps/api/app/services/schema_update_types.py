"""Shared immutable schema-control contracts, without runtime paths or secrets."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from typing import Any

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
    source_identity_mode: str = "trusted_release"
    source_slot_id: str | None = None
    source_inventory_sha256: str | None = None


@dataclass(frozen=True)
class ExecutionStatusObservation:
    payload: dict[str, Any] | None
    invalid_subtype: str | None = None


@dataclass(frozen=True)
class RetryClassification:
    retryable: bool
    public_state: str


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
