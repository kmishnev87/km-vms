from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.models.schema_migration_control import SchemaMigrationAttempt
from app.services.schema_update_types import SchemaControlError, UpdateContext


@dataclass(frozen=True)
class SchemaAttemptStore:
    """One owner for durable attempt and control-state writes."""

    inventory_bound_source_token: Callable
    transition_attempt_id: Callable
    naive_utc_now: Callable
    bounded_details: Callable

    def start_attempt(self,
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
        attempt_id = self.transition_attempt_id(
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
        source_identity_details: dict[str, Any] = {}
        identity_mode = getattr(
            context,
            "source_identity_mode",
            "trusted_release",
        )
        source_slot_id = getattr(context, "source_slot_id", None)
        source_inventory_sha256 = getattr(
            context,
            "source_inventory_sha256",
            None,
        )
        if identity_mode == "inventory_bound":
            if (
                type(source_slot_id) is not str
                or not re.fullmatch(r"initial-[0-9a-f]{64}", source_slot_id)
                or type(source_inventory_sha256) is not str
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    source_inventory_sha256,
                )
                or context.installed_commit
                != self.inventory_bound_source_token(
                    source_slot_id,
                    source_inventory_sha256,
                )
            ):
                raise SchemaControlError(
                    "inventory_source_attempt_evidence_invalid"
                )
            source_identity_details = {
                "source_identity": {
                    "identity_mode": "inventory_bound",
                    "slot_id": source_slot_id,
                    "inventory_sha256": source_inventory_sha256,
                }
            }
        elif (
            identity_mode != "trusted_release"
            or source_slot_id is not None
            or source_inventory_sha256 is not None
        ):
            raise SchemaControlError(
                "trusted_source_attempt_evidence_invalid"
            )
        db.add(
            SchemaMigrationAttempt(
                attempt_id=attempt_id,
                admission_attempt_id=context.admission_attempt_id,
                request_id=context.request_id,
                migration_id=transition_id,
                previous_version=previous_version,
                target_version=target_version,
                status="started",
                started_at=self.naive_utc_now(),
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
                details=source_identity_details,
            )
        )
        db.flush()
        return attempt_id, "started"

    def finish_attempt(self,
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
        existing = db.get(SchemaMigrationAttempt, attempt_id)
        persisted_details = (
            getattr(existing, "details", {})
            if existing is not None
            else {}
        )
        if type(persisted_details) is not dict or type(details) is not dict:
            raise SchemaControlError("migration_attempt_details_invalid")
        merged_details = dict(persisted_details)
        for key, value in details.items():
            if key == "source_identity" and key in merged_details and merged_details[key] != value:
                raise SchemaControlError(
                    "migration_attempt_source_identity_conflict"
                )
            merged_details[key] = value
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
                "completed_at": self.naive_utc_now(),
                "after_shape": after_shape_fingerprint,
                "failure_class": (failure_class or "")[:96] or None,
                "failure_summary": (failure_summary or "")[:300] or None,
                "resumable": bool(resumable),
                "details": self.bounded_details(merged_details),
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

    def update_control_state(self,
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
                "lease_expires_at": self.naive_utc_now() + timedelta(minutes=15),
                "updated_at": self.naive_utc_now(),
                "owner_attempt_id": context.admission_attempt_id,
                "request_id": context.request_id,
                "generation": generation,
                "target_commit": context.target_commit,
                "plan_fingerprint": context.plan_fingerprint,
            },
        )
        if result.rowcount != 1:
            raise SchemaControlError("migration_control_fence_lost")
