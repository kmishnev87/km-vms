from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.services.backup_before_upgrade import backup_precondition_status
from app.services.schema_versioning import (
    CURRENT_BASELINE_ID,
    CURRENT_SCHEMA_VERSION,
    CURRENT_STATE_ID,
    SAFE_STATUSES,
    SCHEMA_METADATA_TABLES,
    inspect_schema_shape,
    SchemaVersionBlocked,
    create_schema_version_tables,
    schema_version_status,
)


MIGRATION_SOURCE = "migration_runner"
PRODUCTION_ADOPTION_DEFERRED = "production_adoption_deferred"
RISK_METADATA_ONLY = "metadata_only"
RISK_ADDITIVE_SAFE = "additive_safe"
RISK_REQUIRES_BACKUP = "risky_requires_backup"
RISK_MANUAL_ONLY = "manual_only"
EXECUTABLE_RISKS = {RISK_METADATA_ONLY, RISK_ADDITIVE_SAFE}
SUPPORTED_RISKS = EXECUTABLE_RISKS | {RISK_REQUIRES_BACKUP, RISK_MANUAL_ONLY}
TRANSACTION_DEFAULT = "session_transaction"
HISTORY_APPLIED = "applied"
HISTORY_FAILED = "failed"
HISTORY_INCONSISTENT = "inconsistent"
HISTORY_MISSING = "missing"
HISTORY_PREVIOUS_FAILURE = "previous_failure"


class SchemaMigrationBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("summary") or status)


class MigrationRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class MigrationDefinition:
    migration_id: str
    from_version: int
    to_version: int
    description: str
    risk: str
    transaction_mode: str
    preflight: Callable[[Session], dict[str, Any] | None]
    apply: Callable[[Session], dict[str, Any] | None]
    verify: Callable[[Session], dict[str, Any] | None]
    safe_failure_summary: str
    rollback_note: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,99}", self.migration_id):
            raise MigrationRegistryError("migration_id must be stable lowercase id-like text")
        if self.to_version <= self.from_version:
            raise MigrationRegistryError("migration to_version must be greater than from_version")
        if self.risk not in SUPPORTED_RISKS:
            raise MigrationRegistryError(f"unsupported migration risk: {self.risk}")


class MigrationRegistry:
    def __init__(self, migrations: list[MigrationDefinition] | tuple[MigrationDefinition, ...] = ()):
        self._migrations = tuple(sorted(migrations, key=lambda item: (item.from_version, item.to_version, item.migration_id)))
        self._validate()

    @property
    def migrations(self) -> tuple[MigrationDefinition, ...]:
        return self._migrations

    @property
    def target_version(self) -> int:
        if not self._migrations:
            return CURRENT_SCHEMA_VERSION
        return max(CURRENT_SCHEMA_VERSION, *(item.to_version for item in self._migrations))

    def _validate(self) -> None:
        seen_ids: set[str] = set()
        seen_edges: set[tuple[int, int]] = set()
        for migration in self._migrations:
            if migration.migration_id in seen_ids:
                raise MigrationRegistryError(f"duplicate migration_id: {migration.migration_id}")
            seen_ids.add(migration.migration_id)
            edge = (migration.from_version, migration.to_version)
            if edge in seen_edges:
                raise MigrationRegistryError(f"conflicting migration edge: {migration.from_version}->{migration.to_version}")
            seen_edges.add(edge)

    def path(self, current_version: int, target_version: int | None = None) -> list[MigrationDefinition]:
        target = self.target_version if target_version is None else target_version
        pending: list[MigrationDefinition] = []
        version = current_version
        while version < target:
            candidates = [item for item in self._migrations if item.from_version == version and item.to_version <= target]
            if not candidates:
                raise SchemaMigrationBlocked(
                    "missing_migration",
                    {
                        "managed": False,
                        "status": "missing_migration",
                        "summary": f"No ordered migration is registered from schema version {version}.",
                        "current_schema_version": current_version,
                        "target_schema_version": target,
                    },
                )
            migration = sorted(candidates, key=lambda item: (item.to_version, item.migration_id))[0]
            pending.append(migration)
            version = migration.to_version
        return pending


PRODUCTION_MIGRATIONS = MigrationRegistry(())


def _has_metadata_tables(db: Session) -> tuple[bool, bool]:
    inspector = inspect(db.get_bind())
    return inspector.has_table("schema_version_state"), inspector.has_table("schema_migration_history")


def _current_history_count(db: Session, row: SchemaVersionState) -> int:
    return (
        db.query(SchemaMigrationHistory)
        .filter(
            SchemaMigrationHistory.baseline_id == row.baseline_id,
            SchemaMigrationHistory.source == row.source,
            SchemaMigrationHistory.schema_version == row.schema_version,
            SchemaMigrationHistory.target_version == row.schema_version,
        )
        .count()
    )


def _raw_current_state_for_plan(db: Session) -> tuple[SchemaVersionState | None, dict[str, Any] | None]:
    has_state, has_history = _has_metadata_tables(db)
    if not has_state and not has_history:
        return None, {
            "status": "unversioned",
            "summary": "Schema version metadata is not initialized; Stage 3 plan is read-only and will not adopt.",
        }
    if has_state != has_history:
        return None, {
            "status": "metadata_incomplete",
            "summary": "Schema version metadata tables are incomplete.",
        }
    row = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if row is None:
        if db.query(SchemaMigrationHistory).count():
            return None, {
                "status": "metadata_incomplete",
                "summary": "Schema history exists without current schema state.",
            }
        return None, {
            "status": "unversioned",
            "summary": "Schema version metadata is not initialized; Stage 3 plan is read-only and will not adopt.",
        }
    if row.schema_version > CURRENT_SCHEMA_VERSION:
        return row, {"status": "future_version", "summary": "Database schema version is newer than this app supports."}
    if row.baseline_id != CURRENT_BASELINE_ID or row.status not in SAFE_STATUSES:
        return row, {"status": "unknown", "summary": "Schema version metadata is unknown or malformed."}
    if _current_history_count(db, row) != 1:
        return row, {
            "status": "metadata_incomplete",
            "summary": "Schema current state has no exactly matching immutable history row.",
        }
    return row, None


def _migration_payload(migration: MigrationDefinition) -> dict[str, Any]:
    return {
        "migration_id": migration.migration_id,
        "from_version": migration.from_version,
        "to_version": migration.to_version,
        "description": migration.description,
        "risk": migration.risk,
        "backup_required": migration.risk == RISK_REQUIRES_BACKUP,
        "manual_only": migration.risk == RISK_MANUAL_ONLY,
        "transaction_mode": migration.transaction_mode,
        "rollback_note": migration.rollback_note,
        "auto_executable": migration.risk in EXECUTABLE_RISKS,
    }


def _sanitize_failure(value: str) -> str:
    text = re.sub(r"(password|secret|token|authorization|jwt|rtsp://)[^,\s)]*", r"\1=***", str(value), flags=re.IGNORECASE)
    return text[:300]


def migration_history_status(db: Session, migration: MigrationDefinition) -> dict[str, Any]:
    rows = (
        db.query(SchemaMigrationHistory)
        .filter(SchemaMigrationHistory.migration_id == migration.migration_id, SchemaMigrationHistory.source == MIGRATION_SOURCE)
        .all()
    )
    if not rows:
        return {"status": HISTORY_MISSING, "summary": "Migration history is missing."}

    applied = [
        row
        for row in rows
        if row.status == "applied" and row.target_version == migration.to_version and row.schema_version == migration.to_version
    ]
    incompatible_applied = [row for row in rows if row.status == "applied" and row not in applied]
    non_applied = [row for row in rows if row.status != "applied"]

    if len(applied) == 1 and not non_applied and not incompatible_applied:
        return {"status": HISTORY_APPLIED, "summary": "Migration was already applied."}
    if len(applied) > 1 or incompatible_applied or (applied and non_applied):
        return {"status": HISTORY_INCONSISTENT, "summary": "Migration history contains duplicate or conflicting rows."}

    failed_like = [
        row
        for row in non_applied
        if row.status in {"failed", "blocked", "incomplete", "started", "pending", "manual_review", "unknown"}
    ]
    if failed_like:
        error = next((row.error_summary for row in failed_like if row.error_summary), None)
        return {
            "status": HISTORY_PREVIOUS_FAILURE,
            "summary": _sanitize_failure(error or "Previous migration attempt did not complete successfully."),
        }

    return {"status": HISTORY_INCONSISTENT, "summary": "Migration history status is not a compatible applied marker."}


def build_migration_plan(
    db: Session,
    *,
    registry: MigrationRegistry = PRODUCTION_MIGRATIONS,
    target_version: int | None = None,
    production_adoption_status: str = PRODUCTION_ADOPTION_DEFERRED,
    backup_manifest_path: str | None = None,
    manual_authorized: bool = False,
) -> dict[str, Any]:
    target = registry.target_version if target_version is None else target_version
    row, blocker = _raw_current_state_for_plan(db)
    status_payload = schema_version_status(db)

    base = {
        "managed": bool(row and row.status in SAFE_STATUSES),
        "status": "blocked" if blocker else "planned",
        "current_schema_version": row.schema_version if row else status_payload.get("schema_version"),
        "target_schema_version": target,
        "baseline_id": row.baseline_id if row else status_payload.get("baseline_id"),
        "schema_status": status_payload.get("status"),
        "pending_migrations": [],
        "blocked_reason": None,
        "production_adoption_status": production_adoption_status,
        "recorder_metadata_owner": "api_bootstrap_only",
        "app_build_version_source": "temporary_stage2_metadata_source_deferred_to_stage7",
        "backup_before_upgrade": {
            "required": False,
            "status": "not_required",
            "restore_validation_status": "not_performed_stage5_deferred",
            "production_backup_status": "production_backup_deferred",
        },
        "mutates_database": False,
    }
    if blocker:
        base["blocked_reason"] = blocker["status"]
        base["summary"] = blocker["summary"]
        return base

    assert row is not None
    if row.schema_version == target:
        base.update({"status": "current", "summary": "Schema is current; no pending migrations."})
        return base
    if row.schema_version > target:
        base.update(
            {
                "status": "blocked",
                "blocked_reason": "future_version",
                "summary": "Database schema version is newer than the target supported version.",
            }
        )
        return base

    pending = registry.path(row.schema_version, target)
    base["pending_migrations"] = [_migration_payload(item) for item in pending]
    history_blockers = []
    for migration in pending:
        history = migration_history_status(db, migration)
        if history["status"] == HISTORY_APPLIED:
            if row.schema_version >= migration.to_version:
                continue
            history_blockers.append({"migration_id": migration.migration_id, "status": HISTORY_INCONSISTENT, "summary": history["summary"]})
        elif history["status"] != HISTORY_MISSING:
            history_blockers.append({"migration_id": migration.migration_id, **history})
    if history_blockers:
        first = history_blockers[0]
        base.update(
            {
                "status": "blocked",
                "blocked_reason": "migration_failed_previous_attempt"
                if first["status"] == HISTORY_PREVIOUS_FAILURE
                else "migration_history_inconsistent",
                "summary": first["summary"],
                "history_blockers": history_blockers,
            }
        )
        return base

    manual_blocked = [item for item in pending if item.risk == RISK_MANUAL_ONLY]
    backup_blocked = [item for item in pending if item.risk == RISK_REQUIRES_BACKUP]
    if manual_blocked:
        gate = backup_precondition_status(
            manifest_path=backup_manifest_path,
            required=True,
            manual_only=True,
            manual_authorized=manual_authorized,
        )
        base["backup_before_upgrade"] = {
            "required": True,
            "status": gate["status"],
            "summary": gate["summary"],
            "manual_authorization_required": True,
            "restore_validation_status": "not_performed_stage5_deferred",
            "production_backup_status": "production_backup_deferred",
        }
        base.update(
            {
                "status": "blocked",
                "blocked_reason": "manual_authorization_required",
                "summary": gate["summary"],
            }
        )
    elif backup_blocked:
        gate = backup_precondition_status(manifest_path=backup_manifest_path, required=True)
        base["backup_before_upgrade"] = {
            "required": True,
            "status": gate["status"],
            "summary": gate["summary"],
            "restore_validation_status": "not_performed_stage5_deferred",
            "production_backup_status": "production_backup_deferred",
        }
        if gate["status"] == "satisfied":
            base.update({"status": "ready", "summary": "Backup-required migrations are eligible for controlled runner execution."})
        else:
            base.update({"status": "blocked", "blocked_reason": "backup_required", "summary": gate["summary"]})
    else:
        base.update({"status": "ready", "summary": "Pending migrations are eligible for controlled runner execution."})
    return base


def applied_history_exists(db: Session, migration: MigrationDefinition) -> bool:
    return migration_history_status(db, migration)["status"] == HISTORY_APPLIED


def _record_runner_history(
    db: Session,
    migration: MigrationDefinition,
    *,
    previous_version: int,
    status: str,
    details: dict[str, Any],
    error_summary: str | None = None,
) -> None:
    history = migration_history_status(db, migration)
    if status == "applied" and history["status"] == HISTORY_APPLIED:
        return
    if status == "failed" and history["status"] == HISTORY_PREVIOUS_FAILURE:
        return
    db.add(
        SchemaMigrationHistory(
            migration_id=migration.migration_id,
            previous_version=previous_version,
            target_version=migration.to_version,
            schema_version=migration.to_version if status == "applied" else previous_version,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status=status,
            checksum=None,
            source=MIGRATION_SOURCE,
            service_name="api_bootstrap",
            details=details,
            error_summary=error_summary,
        )
    )


def execute_migration_plan(
    db: Session,
    *,
    registry: MigrationRegistry = PRODUCTION_MIGRATIONS,
    target_version: int | None = None,
    backup_manifest_path: str | None = None,
    manual_authorized: bool = False,
) -> dict[str, Any]:
    plan = build_migration_plan(
        db,
        registry=registry,
        target_version=target_version,
        backup_manifest_path=backup_manifest_path,
        manual_authorized=manual_authorized,
    )
    if plan["status"] == "current":
        return {**plan, "executed_migrations": []}
    if plan["status"] != "ready":
        raise SchemaMigrationBlocked(str(plan.get("blocked_reason") or plan["status"]), plan)

    create_schema_version_tables(db.get_bind())
    row = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if row is None:
        raise SchemaMigrationBlocked("metadata_incomplete", {**plan, "summary": "Current schema state row is missing."})

    executed: list[str] = []
    for migration in registry.path(row.schema_version, target_version):
        if migration.risk == RISK_MANUAL_ONLY and not manual_authorized:
            raise SchemaMigrationBlocked("manual_authorization_required", plan)
        if migration.risk == RISK_REQUIRES_BACKUP and plan.get("backup_before_upgrade", {}).get("status") != "satisfied":
            raise SchemaMigrationBlocked("backup_or_manual_required", plan)
        if migration.risk not in EXECUTABLE_RISKS | {RISK_REQUIRES_BACKUP}:
            raise SchemaMigrationBlocked("backup_or_manual_required", plan)
        history = migration_history_status(db, migration)
        if history["status"] == HISTORY_APPLIED:
            if row.schema_version >= migration.to_version and row.status in SAFE_STATUSES:
                continue
            raise SchemaMigrationBlocked(
                "migration_history_inconsistent",
                {**plan, "summary": "Applied migration history does not match current schema state."},
            )
        if history["status"] == HISTORY_PREVIOUS_FAILURE:
            raise SchemaMigrationBlocked(
                "migration_failed_previous_attempt",
                {**plan, "summary": history["summary"], "blocked_reason": "migration_failed_previous_attempt"},
            )
        if history["status"] == HISTORY_INCONSISTENT:
            raise SchemaMigrationBlocked(
                "migration_history_inconsistent",
                {**plan, "summary": history["summary"], "blocked_reason": "migration_history_inconsistent"},
            )
        if history["status"] != HISTORY_MISSING:
            raise SchemaMigrationBlocked("migration_history_inconsistent", {**plan, "summary": history["summary"]})
        try:
            preflight = migration.preflight(db) or {}
            applied = migration.apply(db) or {}
            verified = migration.verify(db) or {}
            now = datetime.utcnow()
            row.schema_version = migration.to_version
            row.status = "current"
            row.source = MIGRATION_SOURCE
            row.app_version = APP_VERSION
            row.app_build_version = APP_BUILD_VERSION
            row.applied_at = now
            row.notes = migration.description[:500]
            row.error_summary = None
            _record_runner_history(
                db,
                migration,
                previous_version=migration.from_version,
                status="applied",
                details={
                    "risk": migration.risk,
                    "preflight": preflight,
                    "apply": applied,
                    "verify": verified,
                    "rollback_note": migration.rollback_note,
                },
            )
            db.add(row)
            db.commit()
            executed.append(migration.migration_id)
        except Exception as exc:
            db.rollback()
            safe_error = _sanitize_failure(str(exc) or migration.safe_failure_summary)
            _record_runner_history(
                db,
                migration,
                previous_version=migration.from_version,
                status="failed",
                details={"risk": migration.risk, "safe_failure_summary": migration.safe_failure_summary},
                error_summary=safe_error,
            )
            db.commit()
            raise SchemaMigrationBlocked("migration_failed", {**plan, "summary": safe_error}) from exc

    return {
        **build_migration_plan(
            db,
            registry=registry,
            target_version=target_version,
            backup_manifest_path=backup_manifest_path,
            manual_authorized=manual_authorized,
        ),
        "executed_migrations": executed,
    }


def validate_schema_migrations_pre_bootstrap(bind) -> None:
    shape = inspect_schema_shape(bind)
    has_product_tables = bool(shape.table_names - SCHEMA_METADATA_TABLES)
    has_metadata = bool(shape.table_names & SCHEMA_METADATA_TABLES)
    if not has_product_tables and not has_metadata:
        return
    with Session(bind=bind) as db:
        plan = build_migration_plan(db)
    if plan["status"] == "blocked" and plan.get("blocked_reason") == "unversioned" and not has_product_tables:
        return
    if plan["status"] in {"blocked", "ready"}:
        raise SchemaVersionBlocked(str(plan.get("blocked_reason") or plan["status"]), plan)
