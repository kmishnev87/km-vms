from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.db.session import engine
from app.models.schema_migration_control import SchemaMigrationAttempt
from app.models.schema_version import SchemaMigrationHistory
from app.services.schema_migrations import (
    CURRENT_BASELINE_ID,
    MIGRATION_SOURCE,
    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION,
    preparation_definition_fingerprint,
)
from app.services.schema_update_control import (
    PREPARATION_RECEIPT_PATH,
    SchemaControlError,
    acquire_schema_lock,
    actor_snapshot,
    bootstrap_or_resume_control,
    canonical_schema_history_fingerprint,
    classify_retry,
    current_schema_version,
    database_shape_fingerprint,
    finish_attempt,
    load_existing_update_context,
    load_prebootstrap_update_context,
    naive_utc_now,
    release_schema_lock,
    resolve_schema_pipeline_execution_mode,
    stable_failure_reason,
    start_attempt,
    transition_attempt_id,
    update_control_state,
    validate_initial_context_database,
    validate_retry_actor,
    write_auth_snapshot,
    write_stage_receipt,
)


TEST_FAULT_INJECTION = (
    str(os.getenv("KMVMS_TEST_FAULT_INJECTION") or "").strip() == "1"
)
FAILURE_MODE = (
    str(os.getenv("KMVMS_TEST_PREPARATION_FAILURE_MODE") or "").strip().lower()
    if TEST_FAULT_INJECTION
    else ""
)


def _control_tables_exist(db: Session) -> bool:
    inspector = inspect(db.connection())
    control = inspector.has_table("schema_migration_control")
    attempts = inspector.has_table("schema_migration_attempts")
    if control != attempts:
        raise SchemaControlError("migration_control_partial_shape")
    return control


def _record_preparation_history(
    db: Session,
    *,
    source_schema_version: int,
    definition_fingerprint: str,
    details: dict[str, Any],
) -> None:
    existing = (
        db.query(SchemaMigrationHistory)
        .filter(
            SchemaMigrationHistory.migration_id
            == STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id,
            SchemaMigrationHistory.source == MIGRATION_SOURCE,
        )
        .all()
    )
    if existing:
        raise SchemaControlError("preparation_canonical_history_preexisting")
    db.add(
        SchemaMigrationHistory(
            migration_id=(
                STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id
            ),
            previous_version=source_schema_version,
            target_version=source_schema_version,
            schema_version=source_schema_version,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status="applied",
            checksum=definition_fingerprint,
            source=MIGRATION_SOURCE,
            service_name="schema_compatibility_preparation",
            details=details,
            error_summary=None,
        )
    )


def _matching_applied_attempt(
    db: Session,
    *,
    definition_fingerprint: str,
) -> SchemaMigrationAttempt:
    rows = (
        db.query(SchemaMigrationAttempt)
        .filter(
            SchemaMigrationAttempt.migration_id
            == STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id,
            SchemaMigrationAttempt.definition_fingerprint
            == definition_fingerprint,
            SchemaMigrationAttempt.status == "applied",
        )
        .all()
    )
    if len(rows) != 1:
        raise SchemaControlError("preparation_attempt_evidence_inconsistent")
    return rows[0]


def main(
    *,
    manage_lock: bool = True,
    pipeline_lock_backend_pid: int | None = None,
) -> None:
    context = None
    generation = -1
    active_attempt_id = ""
    before_shape = ""
    before_history = ""
    mutation_started = False
    with Session(engine) as db:
        if manage_lock:
            acquire_schema_lock(db)
        try:
            mode = resolve_schema_pipeline_execution_mode(
                db,
                owned_backend_pid=pipeline_lock_backend_pid,
            )
            if mode in {"fresh_install", "exact_target_noop"}:
                return
            control_tables_exist = _control_tables_exist(db)
            if control_tables_exist:
                context = load_existing_update_context(db)
            else:
                context = load_prebootstrap_update_context(db)
            actor_user_id, actor_subject, actor_role = actor_snapshot(
                db,
                context.request,
                installed_version=context.installed_version,
            )
            validate_retry_actor(
                context.request,
                actor_subject=actor_subject,
            )
            if not control_tables_exist:
                generation = 0
                write_auth_snapshot(
                    context=context,
                    actor_user_id=actor_user_id,
                    actor_subject=actor_subject,
                    actor_role=actor_role,
                    generation=generation,
                )
                validate_initial_context_database(db, context=context)
            generation = bootstrap_or_resume_control(
                db,
                context=context,
                actor_user_id=actor_user_id,
                actor_subject=actor_subject,
                actor_role=actor_role,
            )
            update_control_state(
                db,
                context=context,
                generation=generation,
                state="prepared",
            )
            db.commit()

            preparation = STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
            definition_fingerprint = preparation_definition_fingerprint(
                preparation
            )
            preflight = preparation.preflight(db) or {}
            preflight_status = str(preflight.get("status") or "")
            if preflight_status == "not_required":
                update_control_state(
                    db,
                    context=context,
                    generation=generation,
                    state="recovering",
                )
                db.commit()
                write_stage_receipt(
                    PREPARATION_RECEIPT_PATH,
                    context=context,
                    generation=generation,
                    transition_attempt_id_value=context.admission_attempt_id,
                    state="completed",
                    retryable=False,
                    error_code="",
                    summary="Database compatibility preparation was not required.",
                    operator_action="Wait for accepted-operation recovery checks.",
                    details={
                        "preparation_id": preparation.preparation_id,
                        "definition_fingerprint": definition_fingerprint,
                        "status": "not_required",
                    },
                )
                return

            if preflight_status == "already_applied":
                applied_attempt = _matching_applied_attempt(
                    db,
                    definition_fingerprint=definition_fingerprint,
                )
                update_control_state(
                    db,
                    context=context,
                    generation=generation,
                    state="recovering",
                )
                db.commit()
                write_stage_receipt(
                    PREPARATION_RECEIPT_PATH,
                    context=context,
                    generation=generation,
                    transition_attempt_id_value=applied_attempt.attempt_id,
                    state="completed",
                    retryable=False,
                    error_code="",
                    summary="Committed compatibility preparation was reconciled.",
                    operator_action="Wait for accepted-operation recovery checks.",
                    details={
                        "preparation_id": preparation.preparation_id,
                        "definition_fingerprint": definition_fingerprint,
                        "status": "reconciled",
                    },
                )
                return

            if preflight_status != "ready":
                raise SchemaControlError("preparation_preflight_status_invalid")
            previous_version = current_schema_version(db)
            if previous_version not in {5, 6, 7}:
                raise SchemaControlError("preparation_source_version_invalid")
            before_shape = database_shape_fingerprint(db)
            before_history = canonical_schema_history_fingerprint(db)
            active_attempt_id, attempt_status = start_attempt(
                db,
                context=context,
                generation=generation,
                transition_id=preparation.preparation_id,
                previous_version=previous_version,
                target_version=previous_version,
                definition_fingerprint=definition_fingerprint,
                before_shape_fingerprint=before_shape,
            )
            if attempt_status != "started":
                raise SchemaControlError(
                    "preparation_attempt_terminal_without_reconciliation"
                )
            db.commit()

            if FAILURE_MODE == "before-ddl":
                raise SchemaControlError(
                    "test_injected_preparation_failure_before_ddl"
                )
            mutation_started = True
            applied = preparation.apply(db) or {}
            verified = preparation.verify(db) or {}
            if FAILURE_MODE == "after-ddl-before-commit":
                raise SchemaControlError(
                    "test_injected_preparation_failure_after_ddl"
                )
            after_shape = database_shape_fingerprint(db)
            details = {
                "preflight": preflight,
                "apply": applied,
                "verify": verified,
                "rollback_note": preparation.rollback_note,
            }
            _record_preparation_history(
                db,
                source_schema_version=previous_version,
                definition_fingerprint=definition_fingerprint,
                details=details,
            )
            finish_attempt(
                db,
                attempt_id=active_attempt_id,
                status="applied",
                after_shape_fingerprint=after_shape,
                details=details,
            )
            update_control_state(
                db,
                context=context,
                generation=generation,
                state="recovering",
            )
            db.commit()
            if FAILURE_MODE == "after-commit-before-receipt":
                raise SystemExit(43)
            write_stage_receipt(
                PREPARATION_RECEIPT_PATH,
                context=context,
                generation=generation,
                transition_attempt_id_value=active_attempt_id,
                state="completed",
                retryable=False,
                error_code="",
                summary="Compatibility preparation completed transactionally.",
                operator_action="Wait for accepted-operation recovery.",
                details={
                    "preparation_id": preparation.preparation_id,
                    "definition_fingerprint": definition_fingerprint,
                    "before_shape_fingerprint": before_shape,
                    "after_shape_fingerprint": after_shape,
                },
            )
        except Exception as exc:
            db.rollback()
            reason = stable_failure_reason(
                exc,
                "schema_compatibility_preparation_unexpected",
            )
            after_shape = ""
            after_history = ""
            if active_attempt_id and before_shape and before_history:
                try:
                    after_shape = database_shape_fingerprint(db)
                    after_history = (
                        canonical_schema_history_fingerprint(db)
                    )
                except Exception:
                    db.rollback()
            retry_evidence = {
                "schema_version": 1,
                "mutation_started": mutation_started,
                "physical_mutation_possible": False,
                "transaction_rolled_back": True,
                "rollback_verified": bool(
                    after_shape
                    and after_history
                    and after_shape == before_shape
                    and after_history == before_history
                ),
                "schema_shape_unchanged": bool(
                    after_shape and after_shape == before_shape
                ),
                "history_unchanged": bool(
                    after_history and after_history == before_history
                ),
                "canonical_transition_committed": False,
                "foreign_state_detected": False,
            }
            retry = classify_retry(reason, retry_evidence)
            if context is not None and generation > 0:
                try:
                    if active_attempt_id:
                        finish_attempt(
                            db,
                            attempt_id=active_attempt_id,
                            status="failed",
                            after_shape_fingerprint=None,
                            details={
                                "rolled_back": True,
                                "retry_evidence": retry_evidence,
                            },
                            failure_class=type(exc).__name__,
                            failure_summary=reason,
                            resumable=retry.retryable,
                        )
                    update_control_state(
                        db,
                        context=context,
                        generation=generation,
                        state="failed",
                    )
                    db.commit()
                except Exception:
                    db.rollback()
            if context is not None and generation >= 0:
                blocked_reason = reason in {
                    "migration_control_partial_shape",
                    "preparation_canonical_history_preexisting",
                    "preparation_preflight_status_invalid",
                    "preparation_source_version_invalid",
                }
                public_state = (
                    retry.public_state
                    if generation > 0 and not blocked_reason
                    else "blocked"
                )
                write_stage_receipt(
                    PREPARATION_RECEIPT_PATH,
                    context=context,
                    generation=generation,
                    transition_attempt_id_value=(
                        active_attempt_id or context.admission_attempt_id
                    ),
                    state=public_state,
                    retryable=bool(
                        generation > 0 and retry.retryable
                    ),
                    error_code=reason,
                    summary=(
                        "Database compatibility preparation stopped at a "
                        "verified transactional boundary."
                    ),
                    operator_action=(
                        "Retry the same trusted update only when the status "
                        "surface offers retry."
                        if retry.retryable
                        else "Review the migration diagnostics before retrying."
                    ),
                    details={
                        "failure_class": type(exc).__name__[:96],
                        "retry_evidence": retry_evidence,
                    },
                )
            raise
        finally:
            if manage_lock:
                release_schema_lock(db)


if __name__ == "__main__":
    main()
