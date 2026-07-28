from __future__ import annotations

import hashlib
import inspect as python_inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.models.storage_operation import StorageOperation, StorageWorkerLease, StorageWorkSignal
from app.models.archive_integrity import (
    ArchiveIntegrityDirectoryWork,
    ArchiveIntegrityFinding,
    ArchiveIntegrityRemediationItem,
    ArchiveIntegrityRemediationPlan,
    ArchiveIntegrityScan,
    RecorderFileReceipt,
)
from app.models.archive_migration import ArchiveMigrationItem, ArchiveMigrationPlan
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
RISK_TRANSACTIONAL_SAFE = "transactional_schema_safe"
RISK_REQUIRES_BACKUP = "risky_requires_backup"
RISK_MANUAL_ONLY = "manual_only"
EXECUTABLE_RISKS = {
    RISK_METADATA_ONLY,
    RISK_ADDITIVE_SAFE,
    RISK_TRANSACTIONAL_SAFE,
}
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
    definition_material: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,99}", self.migration_id):
            raise MigrationRegistryError("migration_id must be stable lowercase id-like text")
        if self.to_version <= self.from_version:
            raise MigrationRegistryError("migration to_version must be greater than from_version")
        if self.risk not in SUPPORTED_RISKS:
            raise MigrationRegistryError(f"unsupported migration risk: {self.risk}")


@dataclass(frozen=True)
class SchemaPreparationDefinition:
    preparation_id: str
    description: str
    risk: str
    transaction_mode: str
    preflight: Callable[[Session], dict[str, Any] | None]
    apply: Callable[[Session], dict[str, Any] | None]
    verify: Callable[[Session], dict[str, Any] | None]
    safe_failure_summary: str
    rollback_note: str
    definition_material: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,99}", self.preparation_id):
            raise MigrationRegistryError(
                "preparation_id must be stable lowercase id-like text"
            )
        if self.risk not in EXECUTABLE_RISKS:
            raise MigrationRegistryError(
                f"unsupported preparation risk: {self.risk}"
            )


class MigrationRegistry:
    def __init__(
        self,
        migrations: list[MigrationDefinition] | tuple[MigrationDefinition, ...] = (),
        *,
        published_variants: (
            list[MigrationDefinition] | tuple[MigrationDefinition, ...]
        ) = (),
    ):
        self._migrations = tuple(
            sorted(
                migrations,
                key=lambda item: (
                    item.from_version,
                    item.to_version,
                    item.migration_id,
                ),
            )
        )
        self._published_variants = tuple(
            sorted(
                published_variants,
                key=lambda item: (
                    item.from_version,
                    item.to_version,
                    item.migration_id,
                ),
            )
        )
        self._validate()

    @property
    def migrations(self) -> tuple[MigrationDefinition, ...]:
        return self._migrations

    @property
    def published_migrations(self) -> tuple[MigrationDefinition, ...]:
        """All accepted immutable history definitions.

        ``migrations`` remains the one executable target path.  A published
        variant is history-only evidence for a transition that an older public
        release already executed; it never creates a second executable path.
        """

        return self._migrations + self._published_variants

    @property
    def target_version(self) -> int:
        if not self._migrations:
            return CURRENT_SCHEMA_VERSION
        return max(item.to_version for item in self._migrations)

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
        for migration in self._published_variants:
            if migration.migration_id in seen_ids:
                raise MigrationRegistryError(
                    f"duplicate migration_id: {migration.migration_id}"
                )
            seen_ids.add(migration.migration_id)
            edge = (migration.from_version, migration.to_version)
            if edge not in seen_edges:
                raise MigrationRegistryError(
                    "published history variant has no canonical edge: "
                    f"{migration.from_version}->{migration.to_version}"
                )

    def canonical_by_id(
        self,
        migration_id: str,
    ) -> MigrationDefinition | None:
        return next(
            (
                migration
                for migration in self._migrations
                if migration.migration_id == migration_id
            ),
            None,
        )

    def published_by_id(
        self,
        migration_id: str,
    ) -> MigrationDefinition | None:
        return next(
            (
                migration
                for migration in self.published_migrations
                if migration.migration_id == migration_id
            ),
            None,
        )

    def canonical_for_edge(
        self,
        from_version: int,
        to_version: int,
    ) -> MigrationDefinition | None:
        return next(
            (
                migration
                for migration in self._migrations
                if migration.from_version == from_version
                and migration.to_version == to_version
            ),
            None,
        )

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


STAGE4101_TABLES = (
    StorageOperation.__table__,
    StorageWorkerLease.__table__,
    StorageWorkSignal.__table__,
)


def _stage4101_storage_foundation_preflight(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    existing = sorted(table.name for table in STAGE4101_TABLES if inspector.has_table(table.name))
    return {
        "status": "ready",
        "existing_table_count": len(existing),
        "required_table_count": len(STAGE4101_TABLES),
    }


def _stage4101_storage_foundation_apply(db: Session) -> dict[str, Any]:
    bind = db.connection()
    for table in STAGE4101_TABLES:
        table.create(bind=bind, checkfirst=True)
    return {"created_or_verified_table_count": len(STAGE4101_TABLES)}


def _stage4101_storage_foundation_verify(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    missing = sorted(table.name for table in STAGE4101_TABLES if not inspector.has_table(table.name))
    if missing:
        raise RuntimeError("stage4101_storage_foundation_tables_missing")
    drift: dict[str, list[str]] = {}
    for table in STAGE4101_TABLES:
        actual_columns = {str(item["name"]) for item in inspector.get_columns(table.name)}
        missing_columns = sorted(set(table.c.keys()) - actual_columns)
        if missing_columns:
            drift[table.name] = missing_columns
    if drift:
        raise RuntimeError("stage4101_storage_foundation_schema_drift")
    return {"status": "verified", "table_count": len(STAGE4101_TABLES), "column_drift": False}


STAGE4101_STORAGE_FOUNDATION_MIGRATION = MigrationDefinition(
    migration_id="stage13_5_4_10_1_storage_operations_foundation_v2",
    from_version=1,
    to_version=2,
    description="Add durable storage operation, worker lease and coalesced work-signal tables.",
    risk=RISK_ADDITIVE_SAFE,
    transaction_mode="session_transaction",
    preflight=_stage4101_storage_foundation_preflight,
    apply=_stage4101_storage_foundation_apply,
    verify=_stage4101_storage_foundation_verify,
    safe_failure_summary="Storage operations foundation migration failed safely.",
    rollback_note="Additive tables are retained for evidence; destructive automatic downgrade is not supported.",
)


STAGE41011_COLUMNS = {
    "parent_snapshot": "JSON NULL",
    "retry_depth": "INTEGER NOT NULL DEFAULT 0",
}


def _stage41011_lineage_preflight(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    if not inspector.has_table(StorageOperation.__tablename__):
        raise RuntimeError("stage41011_storage_operations_table_missing")
    existing = {str(item["name"]) for item in inspector.get_columns(StorageOperation.__tablename__)}
    missing = sorted(set(STAGE41011_COLUMNS) - existing)
    return {"status": "ready", "missing_columns": missing}


def _stage41011_lineage_apply(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    existing = {str(item["name"]) for item in inspector.get_columns(StorageOperation.__tablename__)}
    added: list[str] = []
    for column_name, column_ddl in STAGE41011_COLUMNS.items():
        if column_name in existing:
            continue
        db.execute(
            text(
                f"ALTER TABLE {StorageOperation.__tablename__} "
                f"ADD COLUMN {column_name} {column_ddl}"
            )
        )
        added.append(column_name)
    return {"added_columns": sorted(added)}


def _stage41011_lineage_verify(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    actual = {str(item["name"]) for item in inspector.get_columns(StorageOperation.__tablename__)}
    missing = sorted(set(STAGE41011_COLUMNS) - actual)
    if missing:
        raise RuntimeError("stage41011_operation_lineage_columns_missing")
    return {"status": "verified", "column_drift": False}


STAGE41011_OPERATION_LINEAGE_MIGRATION = MigrationDefinition(
    migration_id="stage13_5_4_10_1_1_operation_lineage_v3",
    from_version=2,
    to_version=3,
    description="Add bounded storage-operation retry lineage metadata.",
    risk=RISK_ADDITIVE_SAFE,
    transaction_mode="session_transaction",
    preflight=_stage41011_lineage_preflight,
    apply=_stage41011_lineage_apply,
    verify=_stage41011_lineage_verify,
    safe_failure_summary="Storage operation lineage migration failed safely.",
    rollback_note="Additive lineage columns are retained; destructive automatic downgrade is not supported.",
)


STAGE4102_COLUMNS = {
    "cameras": {
        "retention_policy_version": "BIGINT NOT NULL DEFAULT 1",
    },
    "system_settings": {
        "auto_free_space_acknowledged_terms_version": "VARCHAR(64) NULL",
        "auto_free_space_acknowledged_at": "TIMESTAMP NULL",
        "auto_free_space_acknowledged_by_user_id": "INTEGER NULL",
        "low_disk_suspended_physical_volume_id": "VARCHAR(128) NULL",
        "low_disk_suspended_at": "TIMESTAMP NULL",
    },
}


def _stage4102_retention_preflight(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    missing: dict[str, list[str]] = {}
    for table_name, columns in STAGE4102_COLUMNS.items():
        if not inspector.has_table(table_name):
            raise RuntimeError(f"stage4102_{table_name}_table_missing")
        existing = {str(item["name"]) for item in inspector.get_columns(table_name)}
        absent = sorted(set(columns) - existing)
        if absent:
            missing[table_name] = absent
    return {"status": "ready", "missing_columns": missing}


def _stage4102_retention_apply(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    added: dict[str, list[str]] = {}
    for table_name, columns in STAGE4102_COLUMNS.items():
        existing = {str(item["name"]) for item in inspector.get_columns(table_name)}
        for column_name, column_ddl in columns.items():
            if column_name in existing:
                continue
            db.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_ddl}"))
            added.setdefault(table_name, []).append(column_name)
    db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_recording_segments_camera_status_started_id "
            "ON recording_segments (camera_id, status, started_at, id)"
        )
    )
    db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_recording_segments_root_status_started_id "
            "ON recording_segments (archive_root_id, status, started_at, id)"
        )
    )
    return {"added_columns": {key: sorted(value) for key, value in sorted(added.items())}}


def _stage4102_retention_verify(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    missing: dict[str, list[str]] = {}
    for table_name, columns in STAGE4102_COLUMNS.items():
        actual = {str(item["name"]) for item in inspector.get_columns(table_name)}
        absent = sorted(set(columns) - actual)
        if absent:
            missing[table_name] = absent
    if missing:
        raise RuntimeError("stage4102_retention_schema_drift")
    index_names = {str(item.get("name") or "") for item in inspector.get_indexes("recording_segments")}
    required_indexes = {
        "ix_recording_segments_camera_status_started_id",
        "ix_recording_segments_root_status_started_id",
    }
    if not required_indexes.issubset(index_names):
        raise RuntimeError("stage4102_retention_index_drift")
    return {"status": "verified", "column_drift": False, "index_drift": False}


STAGE4102_RETENTION_MIGRATION = MigrationDefinition(
    migration_id="stage13_5_4_10_2_retention_disk_protection_v4",
    from_version=3,
    to_version=4,
    description="Add versioned retention policy, auto-free acknowledgement and low-disk suspension evidence.",
    risk=RISK_ADDITIVE_SAFE,
    transaction_mode="session_transaction",
    preflight=_stage4102_retention_preflight,
    apply=_stage4102_retention_apply,
    verify=_stage4102_retention_verify,
    safe_failure_summary="Retention and disk-protection migration failed safely.",
    rollback_note="Additive evidence columns and indexes are retained; destructive automatic downgrade is not supported.",
)


STAGE4103_TABLES = (
    ArchiveIntegrityScan.__table__,
    ArchiveIntegrityFinding.__table__,
    ArchiveIntegrityDirectoryWork.__table__,
    RecorderFileReceipt.__table__,
    ArchiveIntegrityRemediationPlan.__table__,
    ArchiveIntegrityRemediationItem.__table__,
)

STAGE4103_REQUIRED_INDEXES = {
    "recording_segments": {
        "ix_recording_segments_root_relative_id",
    },
    ArchiveIntegrityScan.__tablename__: {
        "ix_archive_integrity_scans_status_created",
        "ix_archive_integrity_scans_finished_id",
    },
    ArchiveIntegrityFinding.__tablename__: {
        "uq_archive_integrity_active_metadata_finding",
        "uq_archive_integrity_active_file_finding",
        "ix_archive_integrity_findings_scan_id",
        "ix_archive_integrity_findings_scan_category_id",
        "ix_archive_integrity_findings_scan_root_id",
        "ix_archive_integrity_findings_scan_camera_id",
    },
    ArchiveIntegrityDirectoryWork.__tablename__: {
        "ix_archive_integrity_directory_queue",
        "ix_archive_integrity_directory_lease",
    },
    RecorderFileReceipt.__tablename__: {
        "ix_recorder_file_receipts_root_relative",
        "ix_recorder_file_receipts_root_object",
    },
    ArchiveIntegrityRemediationPlan.__tablename__: {
        "ix_archive_integrity_plans_scan_state",
        "ix_archive_integrity_plans_operation",
        "ix_archive_integrity_plans_apply_operation",
    },
    ArchiveIntegrityRemediationItem.__tablename__: {
        "ix_archive_integrity_items_plan_state",
        "ix_archive_integrity_items_root_object",
    },
}


def _stage4103_integrity_preflight(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    existing = sorted(table.name for table in STAGE4103_TABLES if inspector.has_table(table.name))
    if not inspector.has_table("recording_segments"):
        raise RuntimeError("stage4103_recording_segments_table_missing")
    return {
        "status": "ready",
        "existing_table_count": len(existing),
        "required_table_count": len(STAGE4103_TABLES),
    }


def _stage4103_integrity_apply(db: Session) -> dict[str, Any]:
    bind = db.connection()
    for table in STAGE4103_TABLES:
        table.create(bind=bind, checkfirst=True)
    # Keep the historical v5 PostgreSQL shape stable during multi-step upgrades.
    # Stage 4.10.5.2.2 widens this column explicitly in the ordered v6 -> v7 step.
    inspector = inspect(bind)
    if bind.dialect.name == "postgresql":
        state_column = next(
            (
                column
                for column in inspector.get_columns(ArchiveIntegrityRemediationItem.__tablename__)
                if str(column.get("name") or "") == "state"
            ),
            None,
        )
        if state_column is not None and getattr(state_column.get("type"), "length", None) != 24:
            db.execute(
                text(
                    "ALTER TABLE archive_integrity_remediation_items "
                    "ALTER COLUMN state TYPE VARCHAR(24)"
                )
            )
    db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_recording_segments_root_relative_id "
            "ON recording_segments (archive_root_id, relative_path, id)"
        )
    )
    return {"created_or_verified_table_count": len(STAGE4103_TABLES)}


def _stage4103_integrity_verify(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    missing_tables = sorted(table.name for table in STAGE4103_TABLES if not inspector.has_table(table.name))
    if missing_tables:
        raise RuntimeError("stage4103_integrity_tables_missing")
    missing_indexes: dict[str, list[str]] = {}
    for table_name, required in STAGE4103_REQUIRED_INDEXES.items():
        actual = {str(item.get("name") or "") for item in inspector.get_indexes(table_name)}
        missing = sorted(required - actual)
        if missing:
            missing_indexes[table_name] = missing
    if missing_indexes:
        raise RuntimeError("stage4103_integrity_indexes_missing")
    return {
        "status": "verified",
        "table_drift": False,
        "index_drift": False,
    }


STAGE4103_ARCHIVE_INTEGRITY_MIGRATION = MigrationDefinition(
    migration_id="stage13_5_4_10_3_archive_integrity_v5",
    from_version=4,
    to_version=5,
    description="Add durable archive-integrity scans, findings, provenance and remediation plans.",
    risk=RISK_ADDITIVE_SAFE,
    transaction_mode="session_transaction",
    preflight=_stage4103_integrity_preflight,
    apply=_stage4103_integrity_apply,
    verify=_stage4103_integrity_verify,
    safe_failure_summary="Archive-integrity schema migration failed safely.",
    rollback_note="Additive integrity tables and indexes are retained; destructive automatic downgrade is not supported.",
)


STAGE4104_TABLES = (
    ArchiveMigrationPlan.__table__,
    ArchiveMigrationItem.__table__,
)

STAGE4104_OPERATION_COLUMNS = {
    "domain_ref": "VARCHAR(96) NULL",
}


def _stage4104_migration_preflight(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    if not inspector.has_table(StorageOperation.__tablename__):
        raise RuntimeError("stage4104_storage_operations_table_missing")
    existing_columns = {
        str(item["name"])
        for item in inspector.get_columns(StorageOperation.__tablename__)
    }
    existing_tables = sorted(
        table.name for table in STAGE4104_TABLES if inspector.has_table(table.name)
    )
    return {
        "status": "ready",
        "existing_table_count": len(existing_tables),
        "required_table_count": len(STAGE4104_TABLES),
        "missing_operation_columns": sorted(set(STAGE4104_OPERATION_COLUMNS) - existing_columns),
    }


def _stage4104_migration_apply(db: Session) -> dict[str, Any]:
    bind = db.connection()
    for table in STAGE4104_TABLES:
        table.create(bind=bind, checkfirst=True)
    inspector = inspect(bind)
    existing_columns = {
        str(item["name"])
        for item in inspector.get_columns(StorageOperation.__tablename__)
    }
    added_columns: list[str] = []
    for column_name, column_ddl in STAGE4104_OPERATION_COLUMNS.items():
        if column_name in existing_columns:
            continue
        db.execute(
            text(
                f"ALTER TABLE {StorageOperation.__tablename__} "
                f"ADD COLUMN {column_name} {column_ddl}"
            )
        )
        added_columns.append(column_name)
    db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_storage_operations_domain_ref "
            "ON storage_operations (domain_ref)"
        )
    )
    return {
        "created_or_verified_table_count": len(STAGE4104_TABLES),
        "added_operation_columns": sorted(added_columns),
    }


def _stage4104_migration_verify(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    missing_tables = sorted(
        table.name for table in STAGE4104_TABLES if not inspector.has_table(table.name)
    )
    if missing_tables:
        raise RuntimeError("stage4104_archive_migration_tables_missing")
    drift: dict[str, list[str]] = {}
    for table in STAGE4104_TABLES:
        actual = {str(item["name"]) for item in inspector.get_columns(table.name)}
        missing = sorted(set(table.c.keys()) - actual)
        if missing:
            drift[table.name] = missing
    operation_columns = {
        str(item["name"])
        for item in inspector.get_columns(StorageOperation.__tablename__)
    }
    missing_operation_columns = sorted(set(STAGE4104_OPERATION_COLUMNS) - operation_columns)
    if missing_operation_columns:
        drift[StorageOperation.__tablename__] = missing_operation_columns
    operation_indexes = {
        str(item.get("name") or "")
        for item in inspector.get_indexes(StorageOperation.__tablename__)
    }
    if "ix_storage_operations_domain_ref" not in operation_indexes:
        raise RuntimeError("stage4104_archive_migration_operation_index_missing")
    if drift:
        raise RuntimeError("stage4104_archive_migration_schema_drift")
    return {
        "status": "verified",
        "table_drift": False,
        "column_drift": False,
        "index_drift": False,
    }


STAGE4104_ARCHIVE_MIGRATION = MigrationDefinition(
    migration_id="stage13_5_4_10_4_archive_migration_v6",
    from_version=5,
    to_version=6,
    description="Add durable archive migration plans, items and operation domain references.",
    risk=RISK_ADDITIVE_SAFE,
    transaction_mode="session_transaction",
    preflight=_stage4104_migration_preflight,
    apply=_stage4104_migration_apply,
    verify=_stage4104_migration_verify,
    safe_failure_summary="Archive migration schema migration failed safely.",
    rollback_note="Additive migration tables and operation metadata are retained; destructive automatic downgrade is not supported.",
)


STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID = (
    "stage13_5_4_10_5_2_2_integrity_item_state_width_v7"
)
STAGE410522_INTEGRITY_ITEM_STATE_SOURCE_LENGTH = 24
STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH = 64
STAGE410522_PHYSICAL_PENDING_ITEM_STATES = (
    "physical_mutation_prepared",
    "quarantine_prepared",
    "quarantined",
    "delete_committing",
    "physical_mutation_committed",
)
STAGE410522_PHYSICAL_PENDING_PLAN_STATES = ("running", "terminal_pending")


def _stage410522_state_column(db: Session) -> tuple[str, int | None]:
    bind = db.connection()
    inspector = inspect(bind)
    table_name = ArchiveIntegrityRemediationItem.__tablename__
    if not inspector.has_table(table_name):
        raise RuntimeError("stage410522_integrity_items_table_missing")
    column = next(
        (
            item
            for item in inspector.get_columns(table_name)
            if str(item.get("name") or "") == "state"
        ),
        None,
    )
    if column is None:
        raise RuntimeError("stage410522_integrity_item_state_column_missing")
    column_type = column.get("type")
    rendered_type = str(column_type or "").upper()
    if not (
        rendered_type.startswith("VARCHAR")
        or rendered_type.startswith("CHARACTER VARYING")
    ):
        raise RuntimeError("stage410522_integrity_item_state_type_inconsistent")
    return bind.dialect.name, getattr(column_type, "length", None)


def _stage410522_migration_truth(db: Session) -> tuple[int, int, int]:
    state = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if state is None or state.baseline_id != CURRENT_BASELINE_ID or state.status not in SAFE_STATUSES:
        raise RuntimeError("stage410522_schema_state_inconsistent")
    rows = (
        db.query(SchemaMigrationHistory)
        .filter(
            SchemaMigrationHistory.migration_id == STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID,
            SchemaMigrationHistory.source == MIGRATION_SOURCE,
        )
        .all()
    )
    exact = [
        row
        for row in rows
        if row.status == "applied"
        and row.target_version == 7
        and row.schema_version == 7
        and row.baseline_id == CURRENT_BASELINE_ID
    ]
    return int(state.schema_version), len(rows), len(exact)


def _stage410522_state_aggregates(db: Session) -> dict[str, int]:
    table_name = ArchiveIntegrityRemediationItem.__tablename__
    aggregate = db.execute(
        text(
            f"SELECT COUNT(*) AS row_count, "
            f"COALESCE(MAX(LENGTH(state)), 0) AS max_state_length FROM {table_name}"
        )
    ).mappings().one()
    physical_item_count = int(
        db.query(ArchiveIntegrityRemediationItem.id)
        .filter(ArchiveIntegrityRemediationItem.state.in_(STAGE410522_PHYSICAL_PENDING_ITEM_STATES))
        .count()
    )
    physical_plan_count = int(
        db.query(ArchiveIntegrityRemediationPlan.id)
        .filter(ArchiveIntegrityRemediationPlan.state.in_(STAGE410522_PHYSICAL_PENDING_PLAN_STATES))
        .count()
    )
    return {
        "row_count": int(aggregate.get("row_count") or 0),
        "max_state_length": int(aggregate.get("max_state_length") or 0),
        "physical_pending_item_count": physical_item_count,
        "physical_pending_plan_count": physical_plan_count,
    }


def _stage410522_integrity_item_state_preflight(db: Session) -> dict[str, Any]:
    dialect, actual_length = _stage410522_state_column(db)
    schema_version, history_count, exact_history_count = _stage410522_migration_truth(db)
    aggregates = _stage410522_state_aggregates(db)
    if aggregates["physical_pending_item_count"] or aggregates["physical_pending_plan_count"]:
        raise RuntimeError("stage410522_physical_remediation_active")
    if dialect == "postgresql":
        source_shape = schema_version == 6 and actual_length == STAGE410522_INTEGRITY_ITEM_STATE_SOURCE_LENGTH
        target_shape = (
            schema_version == 7
            and actual_length == STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH
            and history_count == exact_history_count == 1
        )
        if not source_shape and not target_shape:
            raise RuntimeError("stage410522_integrity_item_state_shape_inconsistent")
    elif dialect == "sqlite":
        source_shape = schema_version == 6 and actual_length in {
            STAGE410522_INTEGRITY_ITEM_STATE_SOURCE_LENGTH,
            STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH,
        }
        target_shape = (
            schema_version == 7
            and actual_length in {
                STAGE410522_INTEGRITY_ITEM_STATE_SOURCE_LENGTH,
                STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH,
            }
            and history_count == exact_history_count == 1
        )
        if not source_shape and not target_shape:
            raise RuntimeError("stage410522_integrity_item_state_shape_inconsistent")
    else:
        raise RuntimeError("stage410522_integrity_item_state_dialect_unsupported")
    if schema_version == 6 and history_count:
        raise RuntimeError("stage410522_integrity_item_state_history_inconsistent")
    return {
        "status": "already_applied" if schema_version == 7 else "ready",
        "dialect": dialect,
        "schema_version": schema_version,
        "column_length": actual_length,
        **aggregates,
    }


def _stage410522_integrity_item_state_apply(db: Session) -> dict[str, Any]:
    preflight = _stage410522_integrity_item_state_preflight(db)
    if preflight["status"] == "already_applied":
        return {"status": "already_applied", "column_length": preflight["column_length"]}
    if preflight["dialect"] == "postgresql":
        db.execute(
            text(
                "ALTER TABLE archive_integrity_remediation_items "
                "ALTER COLUMN state TYPE VARCHAR(64)"
            )
        )
        return {"status": "widened", "column_length": STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH}
    return {
        "status": "sqlite_model_contract_only",
        "column_length": preflight["column_length"],
    }


def _stage410522_integrity_item_state_verify(db: Session) -> dict[str, Any]:
    dialect, actual_length = _stage410522_state_column(db)
    model_length = getattr(ArchiveIntegrityRemediationItem.__table__.c.state.type, "length", None)
    if model_length != STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH:
        raise RuntimeError("stage410522_integrity_item_state_model_drift")
    if dialect == "postgresql" and actual_length != STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH:
        raise RuntimeError("stage410522_integrity_item_state_width_not_applied")
    if dialect == "sqlite" and actual_length not in {
        STAGE410522_INTEGRITY_ITEM_STATE_SOURCE_LENGTH,
        STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH,
    }:
        raise RuntimeError("stage410522_integrity_item_state_shape_inconsistent")
    aggregates = _stage410522_state_aggregates(db)
    return {
        "status": "verified",
        "dialect": dialect,
        "column_length": actual_length,
        "model_length": model_length,
        **aggregates,
    }


STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION = MigrationDefinition(
    migration_id=STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID,
    from_version=6,
    to_version=7,
    description="Widen durable archive-integrity remediation item states to 64 characters.",
    risk=RISK_ADDITIVE_SAFE,
    transaction_mode="session_transaction",
    preflight=_stage410522_integrity_item_state_preflight,
    apply=_stage410522_integrity_item_state_apply,
    verify=_stage410522_integrity_item_state_verify,
    safe_failure_summary="Archive-integrity remediation state width migration failed safely.",
    rollback_note="PostgreSQL transactional DDL preserves the v6 VARCHAR(24) shape on failure; automatic downgrade is not supported.",
)


STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION_ID = (
    "stage660128_remediation_state_width_compatibility"
)
STAGE660128_V6_TO_V7_FINALIZATION_ID = (
    "stage660128_remediation_safe_schema_v6_to_v7"
)
STAGE660128_V7_DROP_DEFAULT_COLUMNS = {
    "archive_roots": (
        "created_at",
        "is_active",
        "is_available",
        "is_readable",
        "is_writable",
        "storage_namespace",
        "updated_at",
    ),
    "cameras": (
        "retention_days",
        "segment_minutes",
        "storage_quota_gb",
    ),
    "recording_jobs": (
        "created_at",
        "created_by",
        "ownership",
        "source",
        "updated_at",
    ),
    "recording_segments": (
        "cleanup_candidate",
        "created_at",
        "ownership",
        "source",
        "updated_at",
    ),
    "storage_operations": ("retry_depth",),
    "system_settings": (
        "auto_free_space_cleanup_enabled",
        "recording_suspended_by_low_disk",
    ),
    "users": ("is_active", "updated_at"),
}
STAGE660128_V7_NOT_NULL_COLUMNS = {
    "cameras": (
        "retention_days",
        "segment_minutes",
        "storage_quota_gb",
    ),
    "schema_migration_history": (
        "applied_at",
        "created_at",
        "details",
        "service_name",
    ),
    "schema_version_state": (
        "applied_at",
        "created_at",
        "drift_classification",
        "updated_at",
    ),
}
STAGE660128_V7_REQUIRED_FOREIGN_KEYS = {
    "recording_segments_archive_root_id_fkey": {
        "table": "recording_segments",
        "columns": ("archive_root_id",),
        "referred_table": "archive_roots",
        "referred_columns": ("id",),
        "ondelete": "SET NULL",
    },
    "recording_segments_job_id_fkey": {
        "table": "recording_segments",
        "columns": ("job_id",),
        "referred_table": "recording_jobs",
        "referred_columns": ("id",),
        "ondelete": "SET NULL",
    },
}


def _stage660128_preparation_history_rows(db: Session) -> list[SchemaMigrationHistory]:
    return (
        db.query(SchemaMigrationHistory)
        .filter(
            SchemaMigrationHistory.migration_id
            == STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION_ID,
            SchemaMigrationHistory.source == MIGRATION_SOURCE,
        )
        .all()
    )


def _stage660128_v7_preparation_preflight(
    db: Session,
) -> dict[str, Any]:
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError(
            "stage660128_v7_preparation_dialect_unsupported"
        )
    inspector = inspect(db.connection())
    required_columns = {
        **STAGE660128_V7_DROP_DEFAULT_COLUMNS,
        **STAGE660128_V7_NOT_NULL_COLUMNS,
    }
    column_shapes: dict[str, dict[str, dict[str, Any]]] = {}
    for table_name, column_names in required_columns.items():
        if not inspector.has_table(table_name):
            raise RuntimeError(
                "stage660128_v7_preparation_table_missing"
            )
        actual = {
            str(column.get("name") or ""): column
            for column in inspector.get_columns(table_name)
        }
        if set(column_names) - set(actual):
            raise RuntimeError(
                "stage660128_v7_preparation_column_missing"
            )
        column_shapes[table_name] = actual

    default_drift = {
        (table_name, column_name)
        for table_name, column_names in (
            STAGE660128_V7_DROP_DEFAULT_COLUMNS.items()
        )
        for column_name in column_names
        if str(
            column_shapes[table_name][column_name].get("default")
            or ""
        ).strip()
    }
    nullable_drift = {
        (table_name, column_name)
        for table_name, column_names in (
            STAGE660128_V7_NOT_NULL_COLUMNS.items()
        )
        for column_name in column_names
        if bool(
            column_shapes[table_name][column_name].get("nullable")
        )
    }
    actual_foreign_keys = {
        str(item.get("name") or ""): item
        for item in inspector.get_foreign_keys("recording_segments")
    }
    missing_foreign_keys: set[str] = set()
    for constraint_name, spec in (
        STAGE660128_V7_REQUIRED_FOREIGN_KEYS.items()
    ):
        actual_foreign_key = actual_foreign_keys.get(
            constraint_name
        )
        if actual_foreign_key is None:
            missing_foreign_keys.add(constraint_name)
            continue
        if (
            tuple(
                actual_foreign_key.get("constrained_columns") or ()
            )
            != spec["columns"]
            or str(
                actual_foreign_key.get("referred_table") or ""
            )
            != spec["referred_table"]
            or tuple(
                actual_foreign_key.get("referred_columns") or ()
            )
            != spec["referred_columns"]
            or str(
                (
                    actual_foreign_key.get("options") or {}
                ).get("ondelete")
                or ""
            ).upper()
            != spec["ondelete"]
        ):
            raise RuntimeError(
                "stage660128_v7_preparation_foreign_key_invalid"
            )

    null_counts: dict[str, int] = {}
    for table_name, column_names in (
        STAGE660128_V7_NOT_NULL_COLUMNS.items()
    ):
        for column_name in column_names:
            key = f"{table_name}.{column_name}"
            null_counts[key] = int(
                db.execute(
                    text(
                        f"SELECT COUNT(*) FROM {table_name} "
                        f"WHERE {column_name} IS NULL"
                    )
                ).scalar_one()
                or 0
            )
    orphan_counts = {
        "recording_segments.archive_root_id": int(
            db.execute(
                text(
                    "SELECT COUNT(*) FROM recording_segments segment "
                    "WHERE segment.archive_root_id IS NOT NULL "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM archive_roots root "
                    "WHERE root.id=segment.archive_root_id)"
                )
            ).scalar_one()
            or 0
        ),
        "recording_segments.job_id": int(
            db.execute(
                text(
                    "SELECT COUNT(*) FROM recording_segments segment "
                    "WHERE segment.job_id IS NOT NULL "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM recording_jobs job "
                    "WHERE job.id=segment.job_id)"
                )
            ).scalar_one()
            or 0
        ),
    }
    if any(null_counts.values()):
        raise RuntimeError(
            "stage660128_v7_preparation_null_data_blocked"
        )
    if any(orphan_counts.values()):
        raise RuntimeError(
            "stage660128_v7_preparation_orphan_data_blocked"
        )
    rows = _stage660128_preparation_history_rows(db)
    if rows:
        expected = preparation_definition_fingerprint(
            STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
        )
        matching = [
            row
            for row in rows
            if row.status == "applied"
            and row.checksum == expected
            and row.previous_version == 7
            and row.target_version == 7
            and row.schema_version == 7
        ]
        if len(rows) != 1 or len(matching) != 1:
            raise RuntimeError(
                "stage660128_v7_preparation_receipt_inconsistent"
            )
        _stage660128_v7_preparation_verify(db)
        return {
            "status": "already_applied",
            "schema_version": 7,
            "normalization_profile": (
                "exact_supported_v7_bootstrap_shape"
            ),
            "null_counts": null_counts,
            "orphan_counts": orphan_counts,
        }
    expected_default_drift = {
        (table_name, column_name)
        for table_name, column_names in (
            STAGE660128_V7_DROP_DEFAULT_COLUMNS.items()
        )
        for column_name in column_names
    }
    expected_nullable_drift = {
        (table_name, column_name)
        for table_name, column_names in (
            STAGE660128_V7_NOT_NULL_COLUMNS.items()
        )
        for column_name in column_names
    }
    expected_missing_foreign_keys = set(
        STAGE660128_V7_REQUIRED_FOREIGN_KEYS
    )
    if (
        not default_drift
        and not nullable_drift
        and not missing_foreign_keys
    ):
        return {
            "status": "not_required",
            "schema_version": 7,
            "normalization_profile": "canonical_v7_source",
            "null_counts": null_counts,
            "orphan_counts": orphan_counts,
        }
    if (
        default_drift != expected_default_drift
        or nullable_drift != expected_nullable_drift
        or missing_foreign_keys
        != expected_missing_foreign_keys
    ):
        raise RuntimeError(
            "stage660128_v7_preparation_shape_inconsistent"
        )
    return {
        "status": "ready",
        "schema_version": 7,
        "normalization_profile": (
            "exact_supported_v7_bootstrap_shape"
        ),
        "null_counts": null_counts,
        "orphan_counts": orphan_counts,
    }


def _stage660128_v7_preparation_apply(
    db: Session,
) -> dict[str, Any]:
    preflight = _stage660128_v7_preparation_preflight(db)
    for table_name, column_names in (
        STAGE660128_V7_DROP_DEFAULT_COLUMNS.items()
    ):
        for column_name in column_names:
            db.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    f"ALTER COLUMN {column_name} DROP DEFAULT"
                )
            )
    for table_name, column_names in (
        STAGE660128_V7_NOT_NULL_COLUMNS.items()
    ):
        for column_name in column_names:
            db.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    f"ALTER COLUMN {column_name} SET NOT NULL"
                )
            )

    inspector = inspect(db.connection())
    existing_foreign_keys = {
        str(item.get("name") or ""): item
        for item in inspector.get_foreign_keys("recording_segments")
    }
    added_foreign_keys: list[str] = []
    for constraint_name, spec in (
        STAGE660128_V7_REQUIRED_FOREIGN_KEYS.items()
    ):
        if constraint_name in existing_foreign_keys:
            continue
        columns = ", ".join(spec["columns"])
        referred_columns = ", ".join(spec["referred_columns"])
        db.execute(
            text(
                f"ALTER TABLE {spec['table']} "
                f"ADD CONSTRAINT {constraint_name} "
                f"FOREIGN KEY ({columns}) "
                f"REFERENCES {spec['referred_table']} "
                f"({referred_columns}) "
                f"ON DELETE {spec['ondelete']}"
            )
        )
        added_foreign_keys.append(constraint_name)
    return {
        "status": "normalized",
        "normalization_profile": preflight[
            "normalization_profile"
        ],
        "dropped_default_count": sum(
            len(columns)
            for columns in (
                STAGE660128_V7_DROP_DEFAULT_COLUMNS.values()
            )
        ),
        "not_null_column_count": sum(
            len(columns)
            for columns in (
                STAGE660128_V7_NOT_NULL_COLUMNS.values()
            )
        ),
        "added_foreign_keys": sorted(added_foreign_keys),
    }


def _stage660128_v7_preparation_verify(
    db: Session,
) -> dict[str, Any]:
    inspector = inspect(db.connection())
    for table_name, column_names in (
        STAGE660128_V7_DROP_DEFAULT_COLUMNS.items()
    ):
        actual = {
            str(column.get("name") or ""): column
            for column in inspector.get_columns(table_name)
        }
        if any(
            str(actual[column_name].get("default") or "").strip()
            for column_name in column_names
        ):
            raise RuntimeError(
                "stage660128_v7_preparation_default_not_normalized"
            )
    for table_name, column_names in (
        STAGE660128_V7_NOT_NULL_COLUMNS.items()
    ):
        actual = {
            str(column.get("name") or ""): column
            for column in inspector.get_columns(table_name)
        }
        if any(
            bool(actual[column_name].get("nullable"))
            for column_name in column_names
        ):
            raise RuntimeError(
                "stage660128_v7_preparation_nullability_not_normalized"
            )

    actual_foreign_keys = {
        str(item.get("name") or ""): item
        for item in inspector.get_foreign_keys("recording_segments")
    }
    for constraint_name, spec in (
        STAGE660128_V7_REQUIRED_FOREIGN_KEYS.items()
    ):
        actual = actual_foreign_keys.get(constraint_name)
        if (
            actual is None
            or tuple(actual.get("constrained_columns") or ())
            != spec["columns"]
            or str(actual.get("referred_table") or "")
            != spec["referred_table"]
            or tuple(actual.get("referred_columns") or ())
            != spec["referred_columns"]
            or str(
                (actual.get("options") or {}).get("ondelete") or ""
            ).upper()
            != spec["ondelete"]
        ):
            raise RuntimeError(
                "stage660128_v7_preparation_foreign_key_invalid"
            )
    return {
        "status": "verified",
        "schema_version": 7,
        "normalization_profile": (
            "exact_supported_v7_bootstrap_shape"
        ),
    }


def _stage660128_remediation_compatibility_preflight(
    db: Session,
) -> dict[str, Any]:
    state = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if state is not None and state.schema_version == 7:
        return _stage660128_v7_preparation_preflight(db)
    if state is None or state.schema_version not in {5, 6}:
        return {
            "status": "not_required",
            "schema_version": state.schema_version if state is not None else None,
            "column_length": None,
            "physical_pending_item_count": 0,
            "physical_pending_plan_count": 0,
        }
    dialect, actual_length = _stage410522_state_column(db)
    aggregates = _stage410522_state_aggregates(db)
    active = bool(
        aggregates["physical_pending_item_count"]
        or aggregates["physical_pending_plan_count"]
    )
    if not active:
        return {
            "status": "not_required",
            "schema_version": state.schema_version,
            "column_length": actual_length,
            **aggregates,
        }
    if dialect != "postgresql":
        raise RuntimeError("stage660128_remediation_preparation_dialect_unsupported")
    if actual_length == STAGE410522_INTEGRITY_ITEM_STATE_SOURCE_LENGTH:
        return {
            "status": "ready",
            "schema_version": state.schema_version,
            "column_length": actual_length,
            **aggregates,
        }
    if actual_length != STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH:
        raise RuntimeError("stage660128_remediation_preparation_shape_inconsistent")
    expected = preparation_definition_fingerprint(
        STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
    )
    rows = _stage660128_preparation_history_rows(db)
    matching = [
        row
        for row in rows
        if row.status == "applied"
        and row.checksum == expected
        and row.schema_version in {5, 6}
        and row.target_version == row.schema_version
    ]
    if len(rows) != 1 or len(matching) != 1:
        raise RuntimeError(
            "stage660128_remediation_preparation_receipt_inconsistent"
        )
    return {
        "status": "already_applied",
        "schema_version": state.schema_version,
        "column_length": actual_length,
        **aggregates,
    }


def _stage660128_remediation_compatibility_apply(
    db: Session,
) -> dict[str, Any]:
    state = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if state is not None and state.schema_version == 7:
        return _stage660128_v7_preparation_apply(db)
    preflight = _stage660128_remediation_compatibility_preflight(db)
    if preflight["status"] != "ready":
        return {
            "status": preflight["status"],
            "column_length": preflight["column_length"],
        }
    db.execute(
        text(
            "ALTER TABLE archive_integrity_remediation_items "
            "ALTER COLUMN state TYPE VARCHAR(64)"
        )
    )
    return {
        "status": "widened",
        "column_length": STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH,
    }


def _stage660128_remediation_compatibility_verify(
    db: Session,
) -> dict[str, Any]:
    state = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if state is not None and state.schema_version == 7:
        return _stage660128_v7_preparation_verify(db)
    dialect, actual_length = _stage410522_state_column(db)
    aggregates = (
        _stage410522_state_aggregates(db)
        if state is not None and state.schema_version in {5, 6}
        else {
            "row_count": 0,
            "max_state_length": 0,
            "physical_pending_item_count": 0,
            "physical_pending_plan_count": 0,
        }
    )
    active = bool(
        aggregates["physical_pending_item_count"]
        or aggregates["physical_pending_plan_count"]
    )
    if active and (
        dialect != "postgresql"
        or actual_length != STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH
    ):
        raise RuntimeError("stage660128_remediation_preparation_not_applied")
    return {
        "status": "verified" if active else "not_required",
        "schema_version": state.schema_version if state is not None else None,
        "column_length": actual_length,
        **aggregates,
    }


STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION = (
    SchemaPreparationDefinition(
        preparation_id=STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION_ID,
        description=(
            "Under writer quiescence, normalize an exact supported legacy "
            "schema before bounded recovery and canonical migration."
        ),
        risk=RISK_TRANSACTIONAL_SAFE,
        transaction_mode="session_transaction",
        preflight=_stage660128_remediation_compatibility_preflight,
        apply=_stage660128_remediation_compatibility_apply,
        verify=_stage660128_remediation_compatibility_verify,
        safe_failure_summary=(
            "Remediation compatibility preparation failed safely."
        ),
        rollback_note=(
            "PostgreSQL transactional DDL preserves the exact source shape "
            "when the preparation transaction does not commit."
        ),
        definition_material={
            "eligible_schema_versions": [5, 6, 7],
            "physical_remediation_transition": {
                "table": "archive_integrity_remediation_items",
                "column": "state",
                "from_type": "VARCHAR(24)",
                "to_type": "VARCHAR(64)",
            },
            "v7_drop_default_columns": (
                STAGE660128_V7_DROP_DEFAULT_COLUMNS
            ),
            "v7_not_null_columns": (
                STAGE660128_V7_NOT_NULL_COLUMNS
            ),
            "v7_required_foreign_keys": (
                STAGE660128_V7_REQUIRED_FOREIGN_KEYS
            ),
            "requires_active_physical_remediation": True,
            "admits_new_work": False,
            "mutates_business_rows": False,
        },
    )
)


def _stage660128_v6_to_v7_preflight(db: Session) -> dict[str, Any]:
    dialect, actual_length = _stage410522_state_column(db)
    state = db.get(SchemaVersionState, CURRENT_STATE_ID)
    aggregates = _stage410522_state_aggregates(db)
    if state is None or state.schema_version != 6:
        raise RuntimeError("stage660128_v6_to_v7_source_state_invalid")
    if aggregates["physical_pending_item_count"] or aggregates[
        "physical_pending_plan_count"
    ]:
        raise RuntimeError("stage660128_v6_to_v7_remediation_not_reconciled")
    if dialect != "postgresql" or actual_length not in {
        STAGE410522_INTEGRITY_ITEM_STATE_SOURCE_LENGTH,
        STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH,
    }:
        raise RuntimeError("stage660128_v6_to_v7_source_shape_inconsistent")
    prepared = False
    if actual_length == STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH:
        expected = preparation_definition_fingerprint(
            STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
        )
        rows = _stage660128_preparation_history_rows(db)
        matching = [
            row
            for row in rows
            if row.status == "applied"
            and row.checksum == expected
            and row.schema_version in {5, 6}
            and row.target_version == row.schema_version
        ]
        if len(rows) != 1 or len(matching) != 1:
            raise RuntimeError(
                "stage660128_v6_to_v7_preparation_lineage_missing"
            )
        prepared = True
    return {
        "status": "ready",
        "dialect": dialect,
        "schema_version": state.schema_version,
        "column_length": actual_length,
        "compatibility_prepared": prepared,
        **aggregates,
    }


def _stage660128_v6_to_v7_apply(db: Session) -> dict[str, Any]:
    preflight = _stage660128_v6_to_v7_preflight(db)
    if (
        preflight["column_length"]
        == STAGE410522_INTEGRITY_ITEM_STATE_SOURCE_LENGTH
    ):
        db.execute(
            text(
                "ALTER TABLE archive_integrity_remediation_items "
                "ALTER COLUMN state TYPE VARCHAR(64)"
            )
        )
        return {"status": "widened_without_active_remediation"}
    return {"status": "compatibility_preparation_reused"}


def _stage660128_v6_to_v7_verify(db: Session) -> dict[str, Any]:
    dialect, actual_length = _stage410522_state_column(db)
    aggregates = _stage410522_state_aggregates(db)
    if (
        dialect != "postgresql"
        or actual_length != STAGE410522_INTEGRITY_ITEM_STATE_TARGET_LENGTH
        or aggregates["physical_pending_item_count"]
        or aggregates["physical_pending_plan_count"]
    ):
        raise RuntimeError("stage660128_v6_to_v7_target_shape_invalid")
    return {
        "status": "verified",
        "dialect": dialect,
        "column_length": actual_length,
        **aggregates,
    }


STAGE660128_V6_TO_V7_FINALIZATION = MigrationDefinition(
    migration_id=STAGE660128_V6_TO_V7_FINALIZATION_ID,
    from_version=6,
    to_version=7,
    description=(
        "Finalize schema v7 through a new lineage that safely handles either "
        "an inactive VARCHAR(24) source or the exact compatibility-prepared "
        "VARCHAR(64) recovery source."
    ),
    risk=RISK_TRANSACTIONAL_SAFE,
    transaction_mode="session_transaction",
    preflight=_stage660128_v6_to_v7_preflight,
    apply=_stage660128_v6_to_v7_apply,
    verify=_stage660128_v6_to_v7_verify,
    safe_failure_summary="Remediation-safe schema v6 to v7 finalization failed.",
    rollback_note=(
        "The finalization is transactional and never reuses or changes the "
        "published Stage 4.10.5.2.2 migration definition."
    ),
    definition_material={
        "published_migration_preserved": (
            STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION_ID
        ),
        "source_schema_version": 6,
        "target_schema_version": 7,
        "direct_source_type": "VARCHAR(24)",
        "prepared_source_type": "VARCHAR(64)",
        "target_type": "VARCHAR(64)",
        "active_physical_remediation_required": False,
    },
)


STAGE660128_UNIVERSAL_SCHEMA_MIGRATION_ID = (
    "stage660128_universal_skipped_release_schema_v8"
)
STAGE660128_BOOTSTRAP_COLUMNS = {
    "archive_roots": {
        "physical_identity": "VARCHAR(128) NULL",
        "retirement_status": "VARCHAR(50) NULL",
        "retirement_problem": "TEXT NULL",
        "retirement_operation_id": "VARCHAR(96) NULL",
        "retirement_cleanup_status": "VARCHAR(64) NULL",
        "retirement_cleanup_result": "JSON NULL",
    },
    "recording_segments": {
        "archive_root_resolution_status": "VARCHAR(64) NULL",
        "archive_root_resolution_detail": "TEXT NULL",
        "archive_root_resolved_at": "TIMESTAMP NULL",
        "media_progress_at": "TIMESTAMP NULL",
    },
}
STAGE660128_REQUIRED_INDEXES = {
    "archive_roots": {
        "ix_archive_roots_physical_identity": ("physical_identity",),
        "ix_archive_roots_retirement_status": ("retirement_status",),
        "ix_archive_roots_retirement_operation_id": ("retirement_operation_id",),
        "ix_archive_roots_retirement_cleanup_status": ("retirement_cleanup_status",),
    },
    "recording_segments": {
        "ix_recording_segments_archive_root_resolution_status": (
            "archive_root_resolution_status",
        ),
    },
}
STAGE660128_OBSOLETE_INDEXES = {
    "recording_segments": {"ix_recording_segments_root_relative_id"},
}
STAGE660128_ACTIVE_STATUS_QUERIES = {
    "storage_operations": (
        "SELECT COUNT(*) FROM storage_operations "
        "WHERE status IN ('queued','running','cancel_requested')"
    ),
    "archive_integrity_scans": (
        "SELECT COUNT(*) FROM archive_integrity_scans "
        "WHERE status IN ('queued','running','cancel_requested')"
    ),
    "archive_integrity_remediation_plans": (
        "SELECT COUNT(*) FROM archive_integrity_remediation_plans "
        "WHERE state IN ('running','terminal_pending')"
    ),
    "archive_migration_plans": (
        "SELECT COUNT(*) FROM archive_migration_plans "
        "WHERE status IN ('building','queued','running','cancel_requested')"
    ),
}
STAGE660128_TARGET_TABLES = {
    "archive_export_jobs",
    "archive_integrity_directory_work",
    "archive_integrity_findings",
    "archive_integrity_remediation_items",
    "archive_integrity_remediation_plans",
    "archive_integrity_scans",
    "archive_migration_items",
    "archive_migration_plans",
    "archive_roots",
    "audit_events",
    "cameras",
    "recorder_file_receipts",
    "recorder_runtime_status",
    "recording_jobs",
    "recording_segments",
    "schema_migration_attempts",
    "schema_migration_control",
    "schema_migration_history",
    "schema_version_state",
    "setup_locks",
    "storage_operations",
    "storage_work_signals",
    "storage_worker_leases",
    "system_settings",
    "user_workspace_layouts",
    "users",
}


def migration_definition_fingerprint(migration: MigrationDefinition) -> str:
    payload = {
        "schema_version": 1,
        "migration_id": migration.migration_id,
        "from_version": migration.from_version,
        "to_version": migration.to_version,
        "description": migration.description,
        "risk": migration.risk,
        "transaction_mode": migration.transaction_mode,
        "safe_failure_summary": migration.safe_failure_summary,
        "rollback_note": migration.rollback_note,
        "definition_material": migration.definition_material,
        "callables": [
            {
                "module": callback.__module__,
                "qualname": callback.__qualname__,
                "source": hashlib.sha256(
                    python_inspect.getsource(callback).encode("utf-8")
                ).hexdigest(),
            }
            for callback in (migration.preflight, migration.apply, migration.verify)
        ],
    }
    rendered = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def preparation_definition_fingerprint(
    preparation: SchemaPreparationDefinition,
) -> str:
    payload = {
        "schema_version": 1,
        "preparation_id": preparation.preparation_id,
        "description": preparation.description,
        "risk": preparation.risk,
        "transaction_mode": preparation.transaction_mode,
        "safe_failure_summary": preparation.safe_failure_summary,
        "rollback_note": preparation.rollback_note,
        "definition_material": preparation.definition_material,
        "callables": [
            {
                "module": callback.__module__,
                "qualname": callback.__qualname__,
                "source": hashlib.sha256(
                    python_inspect.getsource(callback).encode("utf-8")
                ).hexdigest(),
            }
            for callback in (
                preparation.preflight,
                preparation.apply,
                preparation.verify,
            )
        ],
    }
    rendered = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _stage660128_active_operation_counts(db: Session) -> dict[str, int]:
    inspector = inspect(db.connection())
    counts: dict[str, int] = {}
    for table_name, statement in STAGE660128_ACTIVE_STATUS_QUERIES.items():
        if not inspector.has_table(table_name):
            counts[table_name] = 0
            continue
        counts[table_name] = int(db.execute(text(statement)).scalar_one() or 0)
    return counts


def _stage660128_universal_schema_preflight(db: Session) -> dict[str, Any]:
    state = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if (
        state is None
        or state.schema_version != 7
        or state.baseline_id != CURRENT_BASELINE_ID
        or state.status not in SAFE_STATUSES
    ):
        raise RuntimeError("stage660128_source_schema_state_invalid")
    inspector = inspect(db.connection())
    missing_control = sorted(
        table_name
        for table_name in ("schema_migration_control", "schema_migration_attempts")
        if not inspector.has_table(table_name)
    )
    if missing_control:
        raise RuntimeError("stage660128_migration_control_not_bootstrapped")
    active_counts = _stage660128_active_operation_counts(db)
    if any(active_counts.values()):
        raise RuntimeError("stage660128_active_operation_not_reconciled")
    missing_columns: dict[str, list[str]] = {}
    for table_name, declared in STAGE660128_BOOTSTRAP_COLUMNS.items():
        if not inspector.has_table(table_name):
            raise RuntimeError(f"stage660128_{table_name}_missing")
        actual = {str(column["name"]) for column in inspector.get_columns(table_name)}
        absent = sorted(set(declared) - actual)
        if absent:
            missing_columns[table_name] = absent
    return {
        "status": "ready",
        "active_operation_counts": active_counts,
        "missing_columns": missing_columns,
    }


def _stage660128_universal_schema_apply(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    added: dict[str, list[str]] = {}
    for table_name, declared in STAGE660128_BOOTSTRAP_COLUMNS.items():
        actual = {str(column["name"]) for column in inspector.get_columns(table_name)}
        for column_name, column_ddl in declared.items():
            if column_name in actual:
                continue
            db.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    f"ADD COLUMN {column_name} {column_ddl}"
                )
            )
            added.setdefault(table_name, []).append(column_name)
    for table_name, indexes in STAGE660128_REQUIRED_INDEXES.items():
        for index_name, columns in indexes.items():
            rendered_columns = ", ".join(columns)
            db.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name} ({rendered_columns})"
                )
            )
    # The historical v4 migration added a server default that clean v7
    # installations never had.  V8 declares one canonical model-equivalent
    # shape without changing any stored value.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text(
                "ALTER TABLE cameras "
                "ALTER COLUMN retention_policy_version DROP DEFAULT"
            )
        )
    for table_name, indexes in STAGE660128_OBSOLETE_INDEXES.items():
        for index_name in indexes:
            db.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
    return {
        "added_columns": {
            table_name: sorted(column_names)
            for table_name, column_names in sorted(added.items())
        },
        "canonical_default_normalized": True,
        "obsolete_index_removed": True,
    }


def _stage660128_universal_schema_verify(db: Session) -> dict[str, Any]:
    inspector = inspect(db.connection())
    actual_tables = set(inspector.get_table_names())
    missing_tables = sorted(STAGE660128_TARGET_TABLES - actual_tables)
    unknown_tables = sorted(actual_tables - STAGE660128_TARGET_TABLES)
    if missing_tables or unknown_tables:
        raise RuntimeError("stage660128_target_table_set_mismatch")
    for table_name, declared in STAGE660128_BOOTSTRAP_COLUMNS.items():
        actual = {str(column["name"]): column for column in inspector.get_columns(table_name)}
        if set(declared) - set(actual):
            raise RuntimeError("stage660128_target_column_set_incomplete")
    for table_name, indexes in STAGE660128_REQUIRED_INDEXES.items():
        actual = {
            str(index.get("name") or ""): tuple(
                str(column) for column in index.get("column_names") or []
            )
            for index in inspector.get_indexes(table_name)
        }
        for index_name, columns in indexes.items():
            if actual.get(index_name) != columns:
                raise RuntimeError("stage660128_target_index_mismatch")
    for table_name, obsolete in STAGE660128_OBSOLETE_INDEXES.items():
        actual = {
            str(index.get("name") or "")
            for index in inspector.get_indexes(table_name)
        }
        if actual & obsolete:
            raise RuntimeError("stage660128_obsolete_index_present")
    if db.get_bind().dialect.name == "postgresql":
        default = next(
            column.get("default")
            for column in inspector.get_columns("cameras")
            if str(column.get("name") or "") == "retention_policy_version"
        )
        if default not in {None, ""}:
            raise RuntimeError("stage660128_retention_default_mismatch")
    active_counts = _stage660128_active_operation_counts(db)
    if any(active_counts.values()):
        raise RuntimeError("stage660128_active_operation_reappeared")
    return {
        "status": "verified",
        "table_count": len(actual_tables),
        "active_operation_counts": active_counts,
        "semantic_shape_verified": True,
    }


def validate_stage660128_target_schema(db: Session) -> dict[str, Any]:
    state = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if (
        state is None
        or state.schema_version != 8
        or state.baseline_id != CURRENT_BASELINE_ID
        or state.status not in SAFE_STATUSES
    ):
        raise RuntimeError("stage660128_target_schema_state_invalid")
    return _stage660128_universal_schema_verify(db)


STAGE660128_UNIVERSAL_SCHEMA_MIGRATION = MigrationDefinition(
    migration_id=STAGE660128_UNIVERSAL_SCHEMA_MIGRATION_ID,
    from_version=7,
    to_version=8,
    description=(
        "Declare and normalize all previously bootstrap-owned schema deltas for "
        "universal skipped-release updates."
    ),
    risk=RISK_TRANSACTIONAL_SAFE,
    transaction_mode="session_transaction",
    preflight=_stage660128_universal_schema_preflight,
    apply=_stage660128_universal_schema_apply,
    verify=_stage660128_universal_schema_verify,
    safe_failure_summary="Universal target schema transition failed safely.",
    rollback_note=(
        "PostgreSQL transactional DDL preserves the verified v7 shape on "
        "failure; automatic source downgrade is not supported."
    ),
    definition_material={
        "declared_bootstrap_columns": STAGE660128_BOOTSTRAP_COLUMNS,
        "required_indexes": STAGE660128_REQUIRED_INDEXES,
        "obsolete_indexes": {
            table_name: sorted(indexes)
            for table_name, indexes in STAGE660128_OBSOLETE_INDEXES.items()
        },
        "target_tables": sorted(STAGE660128_TARGET_TABLES),
        "camera_retention_policy_version_server_default": None,
    },
)


PRODUCTION_MIGRATIONS = MigrationRegistry(
    (
        STAGE4101_STORAGE_FOUNDATION_MIGRATION,
        STAGE41011_OPERATION_LINEAGE_MIGRATION,
        STAGE4102_RETENTION_MIGRATION,
        STAGE4103_ARCHIVE_INTEGRITY_MIGRATION,
        STAGE4104_ARCHIVE_MIGRATION,
        STAGE660128_V6_TO_V7_FINALIZATION,
        STAGE660128_UNIVERSAL_SCHEMA_MIGRATION,
    ),
    published_variants=(
        STAGE410522_INTEGRITY_ITEM_STATE_MIGRATION,
    ),
)


def migration_registry_fingerprint(
    registry: MigrationRegistry = PRODUCTION_MIGRATIONS,
) -> str:
    payload = {
        "schema_version": 1,
        "target_version": registry.target_version,
        "preparations": [
            {
                "preparation_id": (
                    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id
                ),
                "definition_fingerprint": preparation_definition_fingerprint(
                    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
                ),
            }
        ],
        "migrations": [
            {
                "migration_id": migration.migration_id,
                "from_version": migration.from_version,
                "to_version": migration.to_version,
                "definition_fingerprint": migration_definition_fingerprint(migration),
            }
            for migration in registry.migrations
        ],
        "published_history_variants": [
            {
                "migration_id": migration.migration_id,
                "from_version": migration.from_version,
                "to_version": migration.to_version,
                "risk": migration.risk,
                "definition_fingerprint": (
                    migration_definition_fingerprint(migration)
                ),
            }
            for migration in registry.published_migrations
            if registry.canonical_by_id(migration.migration_id) is None
        ],
    }
    rendered = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


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
            (
                SchemaMigrationHistory.previous_version.is_(None)
                | (
                    SchemaMigrationHistory.previous_version
                    != SchemaMigrationHistory.target_version
                )
            ),
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

    expected_fingerprint = migration_definition_fingerprint(migration)
    applied = [
        row
        for row in rows
        if row.status == "applied"
        and row.target_version == migration.to_version
        and row.schema_version == migration.to_version
        and row.checksum == expected_fingerprint
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
        "app_build_version_source": "installed_build_metadata_or_development_fallback",
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
    if status != "applied":
        raise MigrationRegistryError(
            "canonical schema history accepts applied transitions only"
        )
    history = migration_history_status(db, migration)
    if status == "applied" and history["status"] == HISTORY_APPLIED:
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
            checksum=migration_definition_fingerprint(migration),
            source=MIGRATION_SOURCE,
            service_name="schema_migration_gate",
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
    on_mutation_start: Callable[[], None] | None = None,
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

    if on_mutation_start is not None:
        on_mutation_start()
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
