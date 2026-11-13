from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Iterable
from sqlalchemy.orm import Session
from app.models.schema_migration_control import SchemaMigrationAttempt, SchemaMigrationControl
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.services.schema_migrations import (
    MIGRATION_SOURCE, STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION,
    migration_definition_fingerprint, preparation_definition_fingerprint,
)
from app.services.schema_versioning import CURRENT_BASELINE_ID, CURRENT_MIGRATION_ID
from app.services.schema_update_types import SchemaControlError


@dataclass(frozen=True)
class SchemaHistoryValidator:
    """Read-only verification of released history, using one supplied registry."""

    TARGET_SCHEMA_VERSION: int
    PRODUCTION_MIGRATIONS: Any

    def _validate_fresh_target_history(self,
        histories: list[SchemaMigrationHistory],
        state: SchemaVersionState,
        controls: list[SchemaMigrationControl],
        attempts: list[SchemaMigrationAttempt],
    ) -> None:
        if controls or attempts:
            raise SchemaControlError(
                "no_active_fresh_target_control_state_present"
            )
        if len(histories) != 1:
            raise SchemaControlError(
                "no_active_fresh_target_history_count_invalid"
            )
        row = histories[0]
        if (
            row.migration_id != f"{CURRENT_BASELINE_ID}_schema_v{self.TARGET_SCHEMA_VERSION}"
            or row.previous_version is not None
            or row.target_version != self.TARGET_SCHEMA_VERSION
            or row.schema_version != self.TARGET_SCHEMA_VERSION
            or row.baseline_id != CURRENT_BASELINE_ID
            or row.status != state.status
            or row.source != "fresh_create_all"
            or row.error_summary is not None
        ):
            raise SchemaControlError(
                "no_active_fresh_target_history_invalid"
            )

    def _known_history_migrations(self, ) -> dict[str, Any]:
        return {
            migration.migration_id: migration
            for migration in self.PRODUCTION_MIGRATIONS.published_migrations
        }

    def validate_released_source_history(self,
        db: Session | None,
        *,
        source_schema_version: int,
        allow_target_rows: bool,
        histories: Iterable[SchemaMigrationHistory] | None = None,
    ) -> dict[str, int]:
        """Validate one ordered published migration prefix for a trusted source.

        The caller first binds the installed commit, declared source schema and
        physical DB shape.  A NULL checksum from an older release is therefore
        accepted only inside that already-bound source prefix.  New target-side
        rows always require their canonical fingerprint in the gate.
        """

        if not 1 <= source_schema_version <= self.TARGET_SCHEMA_VERSION:
            raise SchemaControlError("migration_history_version_invalid")
        if histories is None:
            if db is None:
                raise SchemaControlError("migration_history_session_missing")
            rows = (
                db.query(SchemaMigrationHistory)
                .filter(SchemaMigrationHistory.source == MIGRATION_SOURCE)
                .order_by(SchemaMigrationHistory.id.asc())
                .all()
            )
        else:
            rows = [
                row
                for row in histories
                if row.source == MIGRATION_SOURCE
            ]
        seen_ids: set[str] = set()
        seen_edges: set[tuple[int, int]] = set()
        source_edges: list[tuple[int, int]] = []
        null_checksum_count = 0
        exact_checksum_count = 0
        preparation_count = 0

        for row in rows:
            migration_id = str(row.migration_id)
            if (
                migration_id
                == STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id
            ):
                if migration_id in seen_ids:
                    raise SchemaControlError(
                        "compatibility_preparation_history_duplicate"
                    )
                seen_ids.add(migration_id)
                if not (
                    row.status == "applied"
                    and row.previous_version in {5, 6, 7}
                    and row.target_version == row.previous_version
                    and row.schema_version == row.previous_version
                    and row.schema_version <= source_schema_version
                    and row.checksum
                    == preparation_definition_fingerprint(
                        STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
                    )
                ):
                    raise SchemaControlError(
                        "compatibility_preparation_history_invalid"
                    )
                preparation_count += 1
                continue

            if migration_id in seen_ids:
                raise SchemaControlError("legacy_migration_history_duplicate")
            seen_ids.add(migration_id)
            migration = self.PRODUCTION_MIGRATIONS.published_by_id(migration_id)
            if migration is None:
                raise SchemaControlError("legacy_migration_history_unknown_id")
            edge = (migration.from_version, migration.to_version)
            if edge in seen_edges:
                raise SchemaControlError(
                    "legacy_migration_history_duplicate_edge"
                )
            seen_edges.add(edge)
            if (
                row.status != "applied"
                or row.previous_version != migration.from_version
                or row.target_version != migration.to_version
                or row.schema_version != migration.to_version
                or self.PRODUCTION_MIGRATIONS.canonical_for_edge(*edge) is None
            ):
                raise SchemaControlError(
                    "legacy_migration_history_lineage_invalid"
                )
            if migration.to_version > source_schema_version:
                if not allow_target_rows:
                    raise SchemaControlError(
                        "migration_history_ahead_of_source"
                    )
                continue
            if row.checksum is None:
                null_checksum_count += 1
            elif row.checksum == migration_definition_fingerprint(migration):
                exact_checksum_count += 1
            else:
                raise SchemaControlError(
                    "legacy_migration_history_definition_mismatch"
                )
            source_edges.append(edge)

        if source_edges:
            previous_target = source_edges[0][0]
            for from_version, to_version in source_edges:
                if from_version != previous_target:
                    raise SchemaControlError(
                        "legacy_migration_history_path_inconsistent"
                    )
                previous_target = to_version
            if previous_target != source_schema_version:
                raise SchemaControlError(
                    "legacy_migration_history_path_incomplete"
                )

        return {
            "legacy_applied_count": (
                null_checksum_count + exact_checksum_count
            ),
            "legacy_null_checksum_adopted_count": null_checksum_count,
            "legacy_exact_checksum_count": exact_checksum_count,
            "compatibility_preparation_count": preparation_count,
        }
