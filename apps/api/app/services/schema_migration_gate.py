from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models.schema_migration_control import SchemaMigrationAttempt
from app.models.schema_version import SchemaMigrationHistory
from app.services.schema_migrations import (
    MIGRATION_SOURCE,
    PRODUCTION_MIGRATIONS,
    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION,
    execute_migration_plan,
    migration_definition_fingerprint,
    validate_stage660128_target_schema,
)
from app.services.schema_update_control import (
    GATE_RECEIPT_PATH,
    RECOVERY_RECEIPT_PATH,
    SchemaControlError,
    acquire_schema_lock,
    canonical_schema_history_fingerprint,
    classify_retry,
    current_schema_version,
    database_shape_fingerprint,
    finish_attempt,
    load_existing_update_context,
    read_signed,
    release_schema_lock,
    resolve_schema_pipeline_execution_mode,
    stable_failure_reason,
    start_attempt,
    target_shape_is_exact,
    transition_attempt_id,
    update_control_state,
    validate_released_source_history,
    validate_stage_receipt_payload,
    write_stage_receipt,
)


TEST_FAULT_INJECTION = (
    str(os.getenv("KMVMS_TEST_FAULT_INJECTION") or "").strip() == "1"
)
FAILURE_MODE = (
    str(os.getenv("KMVMS_TEST_SCHEMA_FAILURE_MODE") or "").strip().lower()
    if TEST_FAULT_INJECTION
    else ""
)
CONTROL_ROOT = Path(os.getenv("UPDATE_CONTROL_ROOT") or "/update-control")
RETRY_MARKER = CONTROL_ROOT / "test-retry-once-consumed"


class TerminalAttemptReplayEvidenceError(SchemaControlError):
    pass


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
        or row["state"] != "migrating"
    ):
        raise SchemaControlError("schema_gate_control_binding_invalid")
    return int(row["fencing_generation"])


def _validate_recovery_receipt(
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
            f"operation_recovery_receipt_{exc}"
        ) from exc
    if payload["retryable"] is not False:
        raise SchemaControlError(
            "operation_recovery_receipt_retry_invalid"
        )


def _validate_target_attempt_evidence(
    db: Session,
    *,
    context: Any,
    generation: int,
    migration: Any,
) -> str:
    attempt = db.get(
        SchemaMigrationAttempt,
        transition_attempt_id(
            context.admission_attempt_id,
            migration.migration_id,
        ),
    )
    expected_definition = migration_definition_fingerprint(migration)
    if (
        attempt is None
        or attempt.admission_attempt_id != context.admission_attempt_id
        or attempt.request_id != context.request_id
        or attempt.migration_id != migration.migration_id
        or attempt.previous_version != migration.from_version
        or attempt.target_version != migration.to_version
        or attempt.status not in {"started", "applied"}
        or int(attempt.fencing_generation) != generation
        or attempt.installed_version != context.installed_version
        or attempt.installed_commit != context.installed_commit
        or attempt.target_release != context.target_release
        or attempt.target_commit != context.target_commit
        or attempt.registry_fingerprint != context.registry_fingerprint
        or attempt.plan_fingerprint != context.plan_fingerprint
        or attempt.definition_fingerprint != expected_definition
    ):
        raise SchemaControlError(
            "target_migration_attempt_evidence_invalid"
        )
    if attempt.status == "applied":
        if (
            attempt.completed_at is None
            or attempt.after_shape_fingerprint is None
            or attempt.failure_class is not None
            or attempt.failure_summary is not None
            or bool(attempt.resumable)
        ):
            raise SchemaControlError(
                "target_migration_applied_attempt_evidence_invalid"
            )
    elif (
        attempt.completed_at is not None
        or attempt.after_shape_fingerprint is not None
        or attempt.failure_class is not None
        or attempt.failure_summary is not None
        or bool(attempt.resumable)
    ):
        raise SchemaControlError(
            "target_migration_started_attempt_evidence_invalid"
        )
    return str(attempt.status)


def _validate_released_history_lineage(
    db: Session,
    context: Any,
    *,
    generation: int,
) -> dict[str, int]:
    source_evidence = validate_released_source_history(
        db,
        source_schema_version=context.source_schema_version,
        allow_target_rows=True,
    )
    rows = (
        db.query(SchemaMigrationHistory)
        .filter(SchemaMigrationHistory.source == MIGRATION_SOURCE)
        .order_by(SchemaMigrationHistory.id.asc())
        .all()
    )
    target_applied_count = 0
    target_started_count = 0
    version = current_schema_version(db)
    if (
        version is None
        or version < context.source_schema_version
        or version > 8
    ):
        raise SchemaControlError("migration_history_version_invalid")
    expected_target_path = PRODUCTION_MIGRATIONS.path(
        context.source_schema_version,
        version,
    )
    expected_target_ids = {
        migration.migration_id for migration in expected_target_path
    }
    observed_target_ids: set[str] = set()
    for row in rows:
        if (
            str(row.migration_id)
            == STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id
        ):
            continue
        migration_id = str(row.migration_id)
        migration = PRODUCTION_MIGRATIONS.published_by_id(migration_id)
        if migration is None:
            raise SchemaControlError("legacy_migration_history_unknown_id")
        if migration.to_version <= context.source_schema_version:
            continue

        target_migration = PRODUCTION_MIGRATIONS.canonical_by_id(
            migration_id
        )
        if (
            target_migration is None
            or migration_id not in expected_target_ids
            or row.checksum
            != migration_definition_fingerprint(target_migration)
        ):
            raise SchemaControlError(
                "target_migration_history_lineage_invalid"
            )
        attempt_status = _validate_target_attempt_evidence(
            db,
            context=context,
            generation=generation,
            migration=target_migration,
        )
        observed_target_ids.add(migration_id)
        if attempt_status == "applied":
            target_applied_count += 1
        else:
            target_started_count += 1

    if observed_target_ids != expected_target_ids:
        raise SchemaControlError(
            "target_migration_history_lineage_incomplete"
        )
    if len(expected_target_path) != len(expected_target_ids):
        raise SchemaControlError(
            "target_migration_registry_path_duplicate"
        )
    return {
        **source_evidence,
        "target_applied_attempt_count": target_applied_count,
        "target_started_attempt_count": target_started_count,
    }


def _canonical_transition_applied(
    db: Session,
    migration: Any,
) -> bool:
    expected = migration_definition_fingerprint(migration)
    rows = (
        db.query(SchemaMigrationHistory)
        .filter(
            SchemaMigrationHistory.migration_id == migration.migration_id,
            SchemaMigrationHistory.source == MIGRATION_SOURCE,
        )
        .all()
    )
    return (
        len(rows) == 1
        and rows[0].status == "applied"
        and rows[0].previous_version == migration.from_version
        and rows[0].target_version == migration.to_version
        and rows[0].schema_version == migration.to_version
        and rows[0].checksum == expected
    )


def _reconcile_started_attempts(db: Session, context: Any) -> int:
    attempts = (
        db.query(SchemaMigrationAttempt)
        .filter(
            SchemaMigrationAttempt.status == "started",
            SchemaMigrationAttempt.target_commit == context.target_commit,
            SchemaMigrationAttempt.plan_fingerprint == context.plan_fingerprint,
        )
        .order_by(SchemaMigrationAttempt.started_at.asc())
        .all()
    )
    reconciled = 0
    version = current_schema_version(db)
    for attempt in attempts:
        migration = PRODUCTION_MIGRATIONS.canonical_by_id(
            str(attempt.migration_id)
        )
        if migration is None:
            continue
        if (
            version is not None
            and version >= migration.to_version
            and _canonical_transition_applied(db, migration)
        ):
            finish_attempt(
                db,
                attempt_id=attempt.attempt_id,
                status="applied",
                after_shape_fingerprint=database_shape_fingerprint(db),
                details={
                    "reconciled_after_commit_before_attempt_receipt": True
                },
            )
            reconciled += 1
        elif version != migration.from_version:
            raise SchemaControlError(
                "started_migration_attempt_state_contradiction"
            )
    if reconciled:
        db.commit()
    return reconciled


def _inject_retry_once() -> bool:
    if FAILURE_MODE != "retry-once":
        return False
    try:
        descriptor = os.lstat(RETRY_MARKER)
    except FileNotFoundError:
        descriptor = None
    if descriptor is not None:
        if (
            stat.S_ISLNK(descriptor.st_mode)
            or not stat.S_ISREG(descriptor.st_mode)
        ):
            raise SchemaControlError("test_retry_marker_invalid")
        return False
    from app.services.schema_update_control import atomic_write, utc_now

    atomic_write(
        RETRY_MARKER,
        {
            "schema_version": 1,
            "consumed_at": utc_now(),
        },
    )
    return True


def _replay_existing_failed_attempt(
    db: Session,
    *,
    context: Any,
    generation: int,
    attempt_id: str,
) -> None:
    attempt = db.get(SchemaMigrationAttempt, attempt_id)
    if attempt is None or attempt.status != "failed":
        raise TerminalAttemptReplayEvidenceError(
            "migration_failed_attempt_evidence_missing"
        )
    if (
        attempt.admission_attempt_id != context.admission_attempt_id
        or attempt.request_id != context.request_id
        or attempt.target_commit != context.target_commit
        or attempt.registry_fingerprint
        != context.registry_fingerprint
        or attempt.plan_fingerprint != context.plan_fingerprint
        or attempt.fencing_generation != generation
        or attempt.completed_at is None
        or not attempt.failure_class
        or not attempt.failure_summary
    ):
        raise TerminalAttemptReplayEvidenceError(
            "migration_failed_attempt_evidence_invalid"
        )

    try:
        receipt = read_signed(GATE_RECEIPT_PATH)
    except SchemaControlError as exc:
        raise TerminalAttemptReplayEvidenceError(str(exc)) from exc
    assert receipt is not None
    retryable = bool(attempt.resumable)
    expected_state = "failed" if retryable else "recovery_required"
    try:
        validate_stage_receipt_payload(
            receipt,
            context=context,
            generation=generation,
            expected_state=expected_state,
        )
    except SchemaControlError as exc:
        reason = str(exc)
        if reason.startswith("stage_receipt_"):
            reason = reason[len("stage_receipt_") :]
        raise TerminalAttemptReplayEvidenceError(
            f"schema_gate_terminal_receipt_{reason}"
        ) from exc
    if (
        receipt["attempt_id"] != attempt_id
        or receipt["phase"] != "preparing_database"
        or receipt["retryable"] is not retryable
        or not receipt["error_code"]
        or (
            type(attempt.details) is not dict
            or attempt.details.get("retry_evidence")
            != receipt["details"].get("retry_evidence")
        )
    ):
        raise TerminalAttemptReplayEvidenceError(
            "schema_gate_terminal_receipt_evidence_invalid"
        )

    # Immutable legacy updaters can invoke Compose again after the first
    # schema-gate failure.  Reassert the already proven terminal operation
    # instead of trying to reuse its deterministic transition attempt and
    # overwriting the authoritative receipt with a conflict.  A user retry
    # has a new request/admission identity and therefore takes the normal
    # path below.
    update_control_state(
        db,
        context=context,
        generation=generation,
        state="failed",
    )
    db.commit()
    raise SystemExit(42 if retryable else 43)


def main(
    *,
    manage_lock: bool = True,
    pipeline_lock_backend_pid: int | None = None,
    on_mutation_start: Callable[[], None] | None = None,
) -> None:
    context = None
    generation = 0
    active_attempt_id = ""
    active_migration = None
    committed = False
    legacy_history_evidence: dict[str, int] = {}
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
            context = load_existing_update_context(db)
            generation = _control_generation(db, context)
            recovery_receipt = read_signed(RECOVERY_RECEIPT_PATH)
            assert recovery_receipt is not None
            _validate_recovery_receipt(
                recovery_receipt,
                context=context,
                generation=generation,
            )
            legacy_history_evidence = _validate_released_history_lineage(
                db,
                context,
                generation=generation,
            )
            reconciled_count = _reconcile_started_attempts(db, context)

            while True:
                version = current_schema_version(db)
                if version is None:
                    raise SchemaControlError("schema_gate_version_missing")
                if version == 8:
                    break
                path = PRODUCTION_MIGRATIONS.path(version, 8)
                if not path:
                    raise SchemaControlError("schema_gate_path_missing")
                active_migration = path[0]
                definition_fingerprint = migration_definition_fingerprint(
                    active_migration
                )
                before_shape = database_shape_fingerprint(db)
                before_history = canonical_schema_history_fingerprint(db)
                mutation_started = False
                active_attempt_id, attempt_status = start_attempt(
                    db,
                    context=context,
                    generation=generation,
                    transition_id=active_migration.migration_id,
                    previous_version=active_migration.from_version,
                    target_version=active_migration.to_version,
                    definition_fingerprint=definition_fingerprint,
                    before_shape_fingerprint=before_shape,
                )
                if attempt_status == "applied":
                    if not _canonical_transition_applied(
                        db,
                        active_migration,
                    ):
                        raise SchemaControlError(
                            "applied_attempt_without_canonical_history"
                        )
                    active_attempt_id = ""
                    active_migration = None
                    continue
                if attempt_status == "failed":
                    _replay_existing_failed_attempt(
                        db,
                        context=context,
                        generation=generation,
                        attempt_id=active_attempt_id,
                    )
                if attempt_status != "started":
                    raise SchemaControlError(
                        "migration_attempt_terminal_conflict"
                    )
                db.commit()

                if _inject_retry_once():
                    retry_evidence = {
                        "schema_version": 1,
                        "mutation_started": False,
                        "physical_mutation_possible": False,
                        "transaction_rolled_back": True,
                        "rollback_verified": True,
                        "schema_shape_unchanged": (
                            database_shape_fingerprint(db)
                            == before_shape
                        ),
                        "history_unchanged": (
                            canonical_schema_history_fingerprint(db)
                            == before_history
                        ),
                        "canonical_transition_committed": False,
                        "foreign_state_detected": False,
                    }
                    retry = classify_retry(
                        "test_injected_retryable_schema_failure",
                        retry_evidence,
                    )
                    finish_attempt(
                        db,
                        attempt_id=active_attempt_id,
                        status="failed",
                        after_shape_fingerprint=None,
                        details={
                            "test_injected": True,
                            "ddl_started": False,
                            "retry_evidence": retry_evidence,
                        },
                        failure_class=(
                            "test_injected_retryable_schema_failure"
                        ),
                        failure_summary=(
                            "Injected bounded schema gate failure."
                        ),
                        resumable=retry.retryable,
                    )
                    update_control_state(
                        db,
                        context=context,
                        generation=generation,
                        state="failed",
                    )
                    db.commit()
                    write_stage_receipt(
                        GATE_RECEIPT_PATH,
                        context=context,
                        generation=generation,
                        transition_attempt_id_value=active_attempt_id,
                        state=retry.public_state,
                        retryable=retry.retryable,
                        error_code=(
                            "test_injected_retryable_schema_failure"
                        ),
                        summary=(
                            "Automatic database preparation stopped at a "
                            "verified safe point."
                        ),
                        operator_action="Retry the same trusted update.",
                        details={
                            "migration_id": active_migration.migration_id,
                            "definition_fingerprint": definition_fingerprint,
                            "retry_evidence": retry_evidence,
                        },
                    )
                    raise SystemExit(42 if retry.retryable else 43)

                def mark_gate_mutation_started() -> None:
                    nonlocal mutation_started
                    if mutation_started:
                        return
                    if on_mutation_start is not None:
                        on_mutation_start()
                    mutation_started = True

                result = execute_migration_plan(
                    db,
                    registry=PRODUCTION_MIGRATIONS,
                    target_version=active_migration.to_version,
                    on_mutation_start=mark_gate_mutation_started,
                )
                committed = _canonical_transition_applied(
                    db,
                    active_migration,
                )
                if (
                    not committed
                    or current_schema_version(db)
                    != active_migration.to_version
                ):
                    raise SchemaControlError(
                        "canonical_transition_commit_not_verified"
                    )
                after_shape = database_shape_fingerprint(db)
                finish_attempt(
                    db,
                    attempt_id=active_attempt_id,
                    status="applied",
                    after_shape_fingerprint=after_shape,
                    details={
                        "runner_status": result.get("status"),
                        "executed_migrations": result.get(
                            "executed_migrations"
                        )
                        or [],
                    },
                )
                db.commit()
                active_attempt_id = ""
                active_migration = None
                committed = False

            validate_stage660128_target_schema(db)
            exact, target_shape = target_shape_is_exact(db)
            if not exact:
                raise SchemaControlError(
                    "target_schema_shape_fingerprint_mismatch"
                )
            update_control_state(
                db,
                context=context,
                generation=generation,
                state="completed",
            )
            db.commit()
            write_stage_receipt(
                GATE_RECEIPT_PATH,
                context=context,
                generation=generation,
                transition_attempt_id_value=(
                    active_attempt_id or context.admission_attempt_id
                ),
                state="completed",
                retryable=False,
                error_code="",
                summary="Automatic database preparation completed.",
                operator_action="Wait for target service verification.",
                details={
                    "target_schema_version": 8,
                    "target_shape_fingerprint": target_shape,
                    "reconciled_attempt_count": reconciled_count,
                    **legacy_history_evidence,
                },
            )
        except SystemExit:
            raise
        except TerminalAttemptReplayEvidenceError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            reason = stable_failure_reason(
                exc,
                "canonical_schema_migration_unexpected",
            )
            after_shape = ""
            after_history = ""
            try:
                after_shape = database_shape_fingerprint(db)
                after_history = canonical_schema_history_fingerprint(db)
            except Exception:
                db.rollback()
            rollback_verified = bool(
                before_shape
                and before_history
                and after_shape == before_shape
                and after_history == before_history
            )
            retry_evidence = {
                "schema_version": 1,
                "mutation_started": mutation_started,
                "physical_mutation_possible": False,
                "transaction_rolled_back": not committed,
                "rollback_verified": rollback_verified,
                "schema_shape_unchanged": bool(
                    after_shape and after_shape == before_shape
                ),
                "history_unchanged": bool(
                    after_history and after_history == before_history
                ),
                "canonical_transition_committed": committed,
                "foreign_state_detected": False,
            }
            retry = classify_retry(reason, retry_evidence)
            failure_state = (
                retry.public_state
                if mutation_started or active_attempt_id
                else "blocked"
            )
            if (
                context is not None
                and active_attempt_id
                and active_migration is not None
            ):
                try:
                    committed = committed or (
                        current_schema_version(db)
                        is not None
                        and current_schema_version(db)
                        >= active_migration.to_version
                        and _canonical_transition_applied(
                            db,
                            active_migration,
                        )
                    )
                    retry_evidence[
                        "canonical_transition_committed"
                    ] = committed
                    retry_evidence[
                        "transaction_rolled_back"
                    ] = not committed
                    retry = classify_retry(reason, retry_evidence)
                    failure_state = retry.public_state
                    if committed:
                        finish_attempt(
                            db,
                            attempt_id=active_attempt_id,
                            status="applied",
                            after_shape_fingerprint=(
                                database_shape_fingerprint(db)
                            ),
                            details={
                                "transition_committed_before_gate_failure": True,
                                "retry_evidence": retry_evidence,
                            },
                        )
                    else:
                        finish_attempt(
                            db,
                            attempt_id=active_attempt_id,
                            status="failed",
                            after_shape_fingerprint=None,
                            details={
                                "transaction_rolled_back": True,
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
                    retry_evidence["rollback_verified"] = False
                    retry_evidence["schema_shape_unchanged"] = False
                    retry_evidence["history_unchanged"] = False
                    retry = classify_retry(reason, retry_evidence)
                    failure_state = "recovery_required"
            elif context is not None and generation > 0:
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
            if context is not None and generation > 0:
                write_stage_receipt(
                    GATE_RECEIPT_PATH,
                    context=context,
                    generation=generation,
                    transition_attempt_id_value=(
                        active_attempt_id or context.admission_attempt_id
                    ),
                    state=failure_state,
                    retryable=retry.retryable,
                    error_code=reason,
                    summary=(
                        "Canonical database migration did not reach the exact "
                        "declared target shape."
                    ),
                    operator_action=(
                        "Retry the trusted update."
                        if retry.retryable
                        else "Keep business services stopped and review the "
                        "sanitized support report."
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
