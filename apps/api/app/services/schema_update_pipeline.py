from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db.session import engine
from app.services import (
    operation_recovery,
    schema_migration_gate,
    schema_preparation,
)
from app.services.backup_before_upgrade import (
    BackupExecutionConfig,
    build_backup_plan,
    create_backup_before_upgrade,
)
from app.services.schema_migrations import PRODUCTION_MIGRATIONS
from app.services.schema_update_control import (
    CONTROL_ROOT,
    TARGET_SCHEMA_VERSION,
    acquire_schema_lock,
    atomic_write,
    load_initial_update_context,
    release_schema_lock,
    utc_now,
    validate_released_source_history,
)


MUTATION_STATE_PATH = CONTROL_ROOT / "schema-mutation-state.json"


def _migration_summary(pending: list[Any]) -> dict[str, Any]:
    return {
        "source_schema_version": (
            pending[0].from_version
            if pending
            else TARGET_SCHEMA_VERSION
        ),
        "target_schema_version": TARGET_SCHEMA_VERSION,
        "migration_required": bool(pending),
        "migration_count": len(pending),
        "migration_ids": [migration.migration_id for migration in pending],
    }


def read_only_update_preflight(
    db: Session,
) -> tuple[Any, dict[str, Any]]:
    """Resolve the exact target path without writing DB or control state."""

    context = load_initial_update_context(db)
    history = validate_released_source_history(
        db,
        source_schema_version=context.source_schema_version,
        allow_target_rows=False,
    )
    pending = PRODUCTION_MIGRATIONS.path(
        context.source_schema_version,
        TARGET_SCHEMA_VERSION,
    )
    summary = _migration_summary(pending)
    summary["source_schema_version"] = context.source_schema_version
    if pending:
        backup = build_backup_plan(
            db,
            config=BackupExecutionConfig(
                source="in_app_update_schema_preflight"
            ),
            migration_plan_summary=summary,
        )
        if (
            not backup["free_space"]["passed"]
            or not backup["backup_root_persistent"]
        ):
            raise RuntimeError("schema_backup_preflight_failed")
        summary["backup_preflight"] = {
            "required": True,
            "persistent": bool(backup["backup_root_persistent"]),
            "free_space_passed": True,
        }
    else:
        summary["backup_preflight"] = {
            "required": False,
            "persistent": None,
            "free_space_passed": None,
        }
    summary.update(history)
    return context, summary


def _write_mutation_state(
    *,
    context: Any,
    mutation_started: bool,
    state: str,
    backup_id: str | None = None,
) -> None:
    atomic_write(
        MUTATION_STATE_PATH,
        {
            "schema_version": 1,
            "request_id": context.request_id,
            "target_release": context.target_release,
            "target_commit": context.target_commit,
            "mutation_started": mutation_started,
            "state": state,
            "backup_id": backup_id,
            "updated_at": utc_now(),
        },
    )


def _run_phases(
    lock_backend_pid: int,
    *,
    on_mutation_start: Callable[[], None] | None = None,
) -> None:
    schema_preparation.main(
        manage_lock=False,
        pipeline_lock_backend_pid=lock_backend_pid,
        on_mutation_start=on_mutation_start,
    )
    operation_recovery.main(
        manage_lock=False,
        pipeline_lock_backend_pid=lock_backend_pid,
        on_mutation_start=on_mutation_start,
    )
    schema_migration_gate.main(
        manage_lock=False,
        pipeline_lock_backend_pid=lock_backend_pid,
        on_mutation_start=on_mutation_start,
    )


def run_update_preflight() -> None:
    with Session(engine) as db:
        _context, summary = read_only_update_preflight(db)
    print(
        "schema_migration_required="
        + ("true" if summary["migration_required"] else "false")
    )
    print(
        "schema_source_version="
        + str(summary["source_schema_version"])
    )
    print(
        "schema_target_version="
        + str(summary["target_schema_version"])
    )
    print(
        "schema_preflight="
        + json.dumps(summary, sort_keys=True, separators=(",", ":"))
    )


def run_update_migration() -> None:
    with Session(engine) as lock_db:
        lock_backend_pid = acquire_schema_lock(lock_db)
        try:
            context, summary = read_only_update_preflight(lock_db)
            if not summary["migration_required"]:
                print("schema_migration_required=false")
                return
            _write_mutation_state(
                context=context,
                mutation_started=False,
                state="backup_pending",
            )
            backup = create_backup_before_upgrade(
                lock_db,
                config=BackupExecutionConfig(
                    source="in_app_update_schema"
                ),
                migration_plan_summary=summary,
            )
            # The advisory lock is session-scoped and survives rollback.  Close
            # the read-only preflight/backup transaction before phase workers
            # issue DDL from their own sessions, otherwise our lock session can
            # retain relation locks and block its own migration.
            lock_db.rollback()
            mutation_marked = False

            def mark_mutation_started() -> None:
                nonlocal mutation_marked
                if mutation_marked:
                    return
                _write_mutation_state(
                    context=context,
                    mutation_started=True,
                    state="migrating",
                    backup_id=str(backup["backup_id"]),
                )
                mutation_marked = True

            _run_phases(
                lock_backend_pid,
                on_mutation_start=mark_mutation_started,
            )
            if not mutation_marked:
                raise RuntimeError("schema_mutation_not_started")
            _write_mutation_state(
                context=context,
                mutation_started=True,
                state="completed",
                backup_id=str(backup["backup_id"]),
            )
            print("schema_migration_required=true")
            print("schema_migration=completed")
        finally:
            release_schema_lock(lock_db)


def run_default_pipeline() -> None:
    """Run install/post-overlay phases under one advisory lock."""

    with Session(engine) as lock_db:
        lock_backend_pid = acquire_schema_lock(lock_db)
        try:
            _run_phases(lock_backend_pid)
        finally:
            release_schema_lock(lock_db)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="KM VMS schema update pipeline"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--migrate", action="store_true")
    args = parser.parse_args([] if argv is None else argv)
    if args.preflight:
        run_update_preflight()
    elif args.migrate:
        run_update_migration()
    else:
        run_default_pipeline()


if __name__ == "__main__":
    main(sys.argv[1:])
