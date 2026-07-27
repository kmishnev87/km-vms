from __future__ import annotations

import hashlib
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import engine
from app.services.archive_integrity_remediation import (
    recover_pending_remediation_once,
)
from app.services.schema_update_control import (
    PREPARATION_RECEIPT_PATH,
    RECOVERY_RECEIPT_PATH,
    SchemaControlError,
    acquire_schema_lock,
    canonical_bytes,
    classify_retry,
    load_existing_update_context,
    read_signed,
    release_schema_lock,
    resolve_schema_pipeline_execution_mode,
    stable_failure_reason,
    update_control_state,
    validate_stage_receipt_payload,
    write_stage_receipt,
)


RECOVERY_ID = "stage660128_accepted_remediation_recovery_v1"
RECOVERY_DEFINITION_FINGERPRINT = hashlib.sha256(
    canonical_bytes(
        {
            "schema_version": 1,
            "recovery_id": RECOVERY_ID,
            "public_entrypoint": (
                "app.services.archive_integrity_remediation."
                "recover_pending_remediation_once"
            ),
            "admits_new_work": False,
            "candidate_plan_states": ["running", "terminal_pending"],
            "candidate_item_states": [
                "running",
                "physical_mutation_prepared",
                "physical_mutation_committed",
            ],
            "max_iterations": 64,
            "max_seconds": 300,
            "archive_mount_contract": "generated_exact_multi_root_override",
        }
    )
).hexdigest()
MAX_ITERATIONS = 64
MAX_SECONDS = 300


def _recovery_tables_available(db: Session) -> bool:
    table_names = (
        "archive_integrity_remediation_plans",
        "archive_integrity_remediation_items",
        "storage_operations",
    )
    rows = db.execute(
        text(
            """
            SELECT table_name, to_regclass('public.' || table_name) IS NOT NULL
            FROM unnest(CAST(:table_names AS text[])) AS table_name
            ORDER BY table_name
            """
        ),
        {"table_names": list(table_names)},
    ).all()
    present = {str(name): bool(exists) for name, exists in rows}
    schema_version = db.execute(
        text(
            "SELECT schema_version FROM schema_version_state "
            "WHERE id='current'"
        )
    ).scalar_one_or_none()
    if schema_version is None:
        raise SchemaControlError(
            "accepted_operation_recovery_schema_version_missing"
        )
    remediation_present = (
        present["archive_integrity_remediation_plans"],
        present["archive_integrity_remediation_items"],
    )
    if int(schema_version) < 5:
        if any(remediation_present):
            raise SchemaControlError(
                "accepted_operation_recovery_partial_shape"
            )
        return False
    if not all(present.values()):
        raise SchemaControlError("accepted_operation_recovery_partial_shape")
    return True


def _validate_preparation_receipt(
    payload: dict[str, Any],
    *,
    context: Any,
    generation: int,
) -> None:
    try:
        validate_stage_receipt_payload(
            payload,
            context=context,
            generation=generation,
            expected_state="completed",
        )
    except SchemaControlError as exc:
        raise SchemaControlError(
            f"preparation_receipt_{exc}"
        ) from exc
    if payload["retryable"] is not False:
        raise SchemaControlError("preparation_receipt_retry_invalid")


def _control_generation(db: Session, context: Any) -> int:
    row = db.execute(
        text(
            """
            SELECT owner_attempt_id, request_id, fencing_generation,
                   target_commit, plan_fingerprint, state
            FROM schema_migration_control
            WHERE id='current'
            FOR UPDATE
            """
        )
    ).mappings().one_or_none()
    if row is None:
        raise SchemaControlError("migration_control_row_missing")
    if (
        row["owner_attempt_id"] != context.admission_attempt_id
        or row["request_id"] != context.request_id
        or row["target_commit"] != context.target_commit
        or row["plan_fingerprint"] != context.plan_fingerprint
        or row["state"] != "recovering"
    ):
        raise SchemaControlError("operation_recovery_control_binding_invalid")
    return int(row["fencing_generation"])


def _candidate_rows(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT DISTINCT
                plan.id AS plan_id,
                plan.apply_operation_id AS operation_id,
                plan.state AS plan_state,
                plan.canonical_hash AS canonical_hash
            FROM archive_integrity_remediation_plans AS plan
            LEFT JOIN archive_integrity_remediation_items AS item
              ON item.plan_id = plan.id
            LEFT JOIN storage_operations AS operation
              ON operation.id = plan.apply_operation_id
            WHERE
                plan.state = 'terminal_pending'
                OR (
                    plan.state = 'running'
                    AND plan.apply_operation_id IS NOT NULL
                    AND item.state IN (
                        'running',
                        'physical_mutation_prepared',
                        'physical_mutation_committed'
                    )
                )
                OR (
                    plan.state IN ('completed','partial','failed','cancelled')
                    AND operation.id IS NOT NULL
                    AND operation.status NOT IN (
                        'completed','partial','blocked','failed','cancelled'
                    )
                )
            ORDER BY plan.id
            """
        )
    ).mappings().all()
    return [
        {
            "plan_id": str(row["plan_id"]),
            "operation_id": (
                str(row["operation_id"])
                if row["operation_id"] is not None
                else None
            ),
            "plan_state": str(row["plan_state"]),
            "canonical_hash": str(row["canonical_hash"] or ""),
        }
        for row in rows
    ]


def _storage_operation_ids(db: Session) -> list[str]:
    return [
        str(row[0])
        for row in db.execute(
            text("SELECT id FROM storage_operations ORDER BY id")
        ).all()
    ]


def main(
    *,
    manage_lock: bool = True,
    pipeline_lock_backend_pid: int | None = None,
) -> None:
    context = None
    generation = 0
    initial_candidates: list[dict[str, Any]] = []
    recovery_tables_present = False
    physical_recovery_started = False
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
            context = load_existing_update_context(db)
            generation = _control_generation(db, context)
            preparation_receipt = read_signed(PREPARATION_RECEIPT_PATH)
            assert preparation_receipt is not None
            _validate_preparation_receipt(
                preparation_receipt,
                context=context,
                generation=generation,
            )
            recovery_tables_present = _recovery_tables_available(db)
            initial_candidates = (
                _candidate_rows(db) if recovery_tables_present else []
            )
            if len(initial_candidates) > MAX_ITERATIONS:
                raise SchemaControlError(
                    "accepted_remediation_recovery_bound_exceeded"
                )
            initial_plan_ids = {
                row["plan_id"] for row in initial_candidates
            }
            initial_operation_ids = (
                _storage_operation_ids(db)
                if recovery_tables_present
                else []
            )
            deadline = time.monotonic() + MAX_SECONDS
            iterations = 0
            remaining = list(initial_candidates)
            while remaining:
                if (
                    iterations >= MAX_ITERATIONS
                    or time.monotonic() >= deadline
                ):
                    raise SchemaControlError(
                        "accepted_remediation_recovery_incomplete"
                    )
                before_ids = {row["plan_id"] for row in remaining}
                physical_recovery_started = True
                progressed = recover_pending_remediation_once(db)
                db.expire_all()
                after = _candidate_rows(db)
                after_ids = {row["plan_id"] for row in after}
                if not after_ids.issubset(initial_plan_ids):
                    raise SchemaControlError(
                        "accepted_remediation_recovery_foreign_plan"
                    )
                if not progressed or after_ids == before_ids:
                    raise SchemaControlError(
                        "accepted_remediation_recovery_no_progress"
                    )
                remaining = after
                iterations += 1

            final_operation_ids = (
                _storage_operation_ids(db)
                if recovery_tables_present
                else []
            )
            if final_operation_ids != initial_operation_ids:
                raise SchemaControlError(
                    "accepted_remediation_recovery_created_operation"
                )
            update_control_state(
                db,
                context=context,
                generation=generation,
                state="migrating",
            )
            db.commit()
            write_stage_receipt(
                RECOVERY_RECEIPT_PATH,
                context=context,
                generation=generation,
                transition_attempt_id_value=context.admission_attempt_id,
                state="completed",
                retryable=False,
                error_code="",
                summary=(
                    "Accepted-operation recovery completed."
                    if initial_candidates
                    else "No accepted physical remediation required recovery."
                ),
                operator_action="Wait for canonical schema migration.",
                details={
                    "recovery_id": RECOVERY_ID,
                    "definition_fingerprint": (
                        RECOVERY_DEFINITION_FINGERPRINT
                    ),
                    "candidate_count": len(initial_candidates),
                    "iterations": iterations,
                    "plan_ids": sorted(initial_plan_ids),
                    "created_operation_count": 0,
                    "recovery_tables_present": recovery_tables_present,
                },
            )
        except Exception as exc:
            db.rollback()
            reason = stable_failure_reason(
                exc,
                "accepted_operation_recovery_unexpected",
            )
            retry_evidence = {
                "schema_version": 1,
                "mutation_started": physical_recovery_started,
                "physical_mutation_possible": bool(
                    physical_recovery_started
                    or initial_candidates
                ),
                "transaction_rolled_back": True,
                "rollback_verified": False,
                "schema_shape_unchanged": False,
                "history_unchanged": False,
                "canonical_transition_committed": False,
                "foreign_state_detected": reason
                in {
                    "accepted_remediation_recovery_foreign_plan",
                    "accepted_remediation_recovery_created_operation",
                },
            }
            retry = classify_retry(reason, retry_evidence)
            if context is not None and generation > 0:
                try:
                    update_control_state(
                        db,
                        context=context,
                        generation=generation,
                        state="failed",
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                write_stage_receipt(
                    RECOVERY_RECEIPT_PATH,
                    context=context,
                    generation=generation,
                    transition_attempt_id_value=context.admission_attempt_id,
                    state=retry.public_state,
                    retryable=retry.retryable,
                    error_code=reason,
                    summary=(
                        "An already accepted storage operation could not be "
                        "reconciled within the bounded recovery contract."
                    ),
                    operator_action=(
                        "Keep business services stopped and retry only through "
                        "the trusted update recovery action."
                    ),
                    details={
                        "failure_class": type(exc).__name__,
                        "candidate_count": len(initial_candidates),
                        "retry_evidence": retry_evidence,
                    },
                )
            raise
        finally:
            if manage_lock:
                release_schema_lock(db)


if __name__ == "__main__":
    main()
