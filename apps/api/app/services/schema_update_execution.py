from __future__ import annotations
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.services.schema_update_types import SchemaControlError, UpdateContext, ExecutionStatusObservation


@dataclass(frozen=True)
class SchemaExecutionStatus:
    """Validate lifecycle evidence before any schema execution is admitted."""

    UPDATE_STATUS_PATH: Path
    MAX_CONTROL_BYTES: int
    KNOWN_UPDATE_STATUSES: frozenset
    REQUEST_RE: Any
    UNBOUND_STATUS_SENTINEL_KEY_SETS: tuple
    UNBOUND_STATUS_SENTINEL_STATUSES: frozenset
    SCHEMA_COMPLETED_ACTIVATION_STATUSES: frozenset
    SCHEMA_MUTATION_UPDATE_STATUSES: frozenset
    LEGACY_STATUS_VERSION_FROM_REQUEST_PROFILES: frozenset
    COMMIT_RE: Any
    load_json_bytes: Callable
    exact_string: Callable
    parse_utc: Callable
    validate_terminal_update_request: Callable

    def _read_update_status_for_execution(self, ) -> dict[str, Any] | None:
        try:
            info = os.lstat(self.UPDATE_STATUS_PATH)
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
        if info.st_size <= 1 or info.st_size > self.MAX_CONTROL_BYTES:
            raise SchemaControlError(
                "schema_execution_status_invalid",
                subtype="malformed_json",
            )
        with self.UPDATE_STATUS_PATH.open("rb") as handle:
            raw = handle.read(self.MAX_CONTROL_BYTES + 1)
        if len(raw) > self.MAX_CONTROL_BYTES:
            raise SchemaControlError(
                "schema_execution_status_invalid",
                subtype="malformed_json",
            )
        try:
            payload = self.load_json_bytes(raw)
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
            or status not in self.KNOWN_UPDATE_STATUSES
        ):
            raise SchemaControlError(
                "schema_execution_status_invalid",
                subtype="status_unrecognized",
            )
        request_id = payload.get("request_id")
        if request_id is not None and (
            type(request_id) is not str
            or not self.REQUEST_RE.fullmatch(request_id)
        ):
            raise SchemaControlError(
                "schema_execution_status_invalid",
                subtype="request_invalid",
            )
        return payload

    def _is_exact_unbound_status_sentinel(self,
        payload: dict[str, Any],
    ) -> bool:
        return bool(
            frozenset(payload) in self.UNBOUND_STATUS_SENTINEL_KEY_SETS
            and type(payload.get("schema_version")) is int
            and payload["schema_version"] == 1
            and payload.get("status") in self.UNBOUND_STATUS_SENTINEL_STATUSES
            and payload.get("request_id") is None
        )

    def _status_has_operation_authority(self,
        payload: dict[str, Any],
    ) -> bool:
        return bool(
            payload.get("status") == "unknown"
            and not self._is_exact_unbound_status_sentinel(payload)
        )

    def _validate_matching_active_update_status(self,
        payload: dict[str, Any],
        *,
        request_id: str,
        request_profile: str,
        request_target_version: str | None,
        target_release: str,
        target_commit: str,
        schema_completed: bool = False,
    ) -> None:
        source = payload.get("source")
        target_version_bound = self._status_target_version_is_bound(
            payload,
            request_profile=request_profile,
            request_target_version=request_target_version,
            target_release=target_release,
        )
        if (
            payload.get("status") not in (
                self.SCHEMA_COMPLETED_ACTIVATION_STATUSES
                if schema_completed else self.SCHEMA_MUTATION_UPDATE_STATUSES
            )
            or payload.get("request_id") != request_id
            or not target_version_bound
            or payload.get("expected_commit") != target_commit
            or type(source) is not dict
            or source.get("commit") != target_commit
        ):
            raise SchemaControlError(
                "schema_execution_active_status_mismatch"
            )

    def _status_target_version_is_bound(self,
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
            in self.LEGACY_STATUS_VERSION_FROM_REQUEST_PROFILES
            and request_target_version == target_release
        ):
            # Public updaters through v0.7.23 did not emit target_version in their
            # active or completed status payload.  Their validated request carries
            # the version, while status/request/release all bind the same exact
            # target commit.  Keep that narrow historical path without allowing a
            # current, retry, or transitional status to omit the field.
            return True
        return False

    def _validate_matching_completed_update_status(self,
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
        expected_commit = self.exact_string(
            payload.get("expected_commit"),
            code="schema_execution_completed_status_commit_invalid",
            max_length=40,
        ).lower()
        installed_commit = self.exact_string(
            payload.get("installed_commit"),
            code="schema_execution_completed_status_commit_invalid",
            max_length=40,
        ).lower()
        if (
            not self.COMMIT_RE.fullmatch(expected_commit)
            or not self.COMMIT_RE.fullmatch(installed_commit)
            or expected_commit != context.target_commit
            or installed_commit != context.target_commit
        ):
            raise SchemaControlError(
                "schema_execution_completed_status_commit_mismatch"
            )
        phase = self.exact_string(
            payload.get("phase"),
            code="schema_execution_completed_status_phase_invalid",
            max_length=80,
        )
        current_step = self.exact_string(
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
        started_at = self.parse_utc(payload.get("started_at"))
        updated_at = self.parse_utc(payload.get("updated_at"))
        finished_at = self.parse_utc(
            payload.get("finished_at")
            if payload.get("finished_at") is not None
            else payload.get("updated_at")
        )
        if updated_at < started_at or finished_at < started_at:
            raise SchemaControlError(
                "schema_execution_completed_status_timestamp_invalid"
            )
        request_profile = self.validate_terminal_update_request(context.request)
        request_source = context.request.get("source")
        request_target_version = (
            request_source.get("version")
            if type(request_source) is dict
            else None
        )
        if not self._status_target_version_is_bound(
            payload,
            request_profile=request_profile,
            request_target_version=request_target_version,
            target_release=context.target_release,
        ):
            raise SchemaControlError(
                "schema_execution_completed_status_target_mismatch"
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
