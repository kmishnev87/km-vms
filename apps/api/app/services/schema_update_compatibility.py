from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable
from sqlalchemy.orm import Session
from app.models.schema_version import SchemaVersionState
from app.services.schema_migrations import STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION, migration_definition_fingerprint, preparation_definition_fingerprint
from app.services.schema_versioning import CURRENT_STATE_ID
from app.services.schema_update_types import SchemaControlError, UpdateContext, canonical_bytes


@dataclass(frozen=True)
class SchemaTransitionCompatibility:
    """Compare actual source and target without taking DDL authority."""

    TARGET_SCHEMA_VERSION: int
    TARGET_SHAPE_FINGERPRINTS: frozenset
    REGISTRY_FINGERPRINT: str
    PRODUCTION_MIGRATIONS: Any
    current_schema_version: Callable
    database_shape_fingerprint: Callable
    expected_source_lineage: Callable

    def bind_actual_source_database(self, db: Session, expected_version: int, expected_shapes: frozenset[str]) -> tuple[int, str]:
        """After application rollback the DB can truthfully remain newer than its tag.

        This is not arbitrary drift admission: the completed migration certificate
        must explicitly support the *known installed* reader/writer contract.
        """
        actual = self.current_schema_version(db)
        if actual == expected_version:
            shape = self.database_shape_fingerprint(db)
            if shape not in expected_shapes:
                raise SchemaControlError("installed_source_shape_mismatch")
            return actual, shape
        if type(actual) is int and expected_version < actual <= self.TARGET_SCHEMA_VERSION:
            from app.services.schema_runtime_compatibility import validate_runtime_compatibility
            state = db.get(SchemaVersionState, CURRENT_STATE_ID)
            if state is not None:
                try:
                    proof = validate_runtime_compatibility(db, state, expected_version)
                    return actual, proof["shape_sha256"]
                except RuntimeError as exc:
                    raise SchemaControlError("installed_source_compatibility_not_verified") from exc
        raise SchemaControlError("installed_source_schema_version_mismatch")

    def validate_source_lineage(self,
        db: Session,
        *,
        request_id: str,
        target_release: str,
        target_commit: str,
    ) -> tuple[str, str, int, str]:
        (
            installed_version,
            installed_commit,
            expected_schema_version,
            expected_shapes,
        ) = self.expected_source_lineage(
            request_id=request_id,
            target_release=target_release,
            target_commit=target_commit,
        )
        source_schema_version, shape = self.bind_actual_source_database(db, expected_schema_version, expected_shapes)
        return installed_version, installed_commit, source_schema_version, shape

    def plan_fingerprint(self,
        *,
        installed_version: str,
        installed_commit: str,
        source_schema_version: int,
        source_shape_fingerprint: str,
        target_release: str,
        target_commit: str,
        registry_fingerprint_value: str | None = None,
    ) -> str:
        registry_fingerprint_value = (
            self.REGISTRY_FINGERPRINT
            if registry_fingerprint_value is None
            else registry_fingerprint_value
        )
        if not re.fullmatch(r"[0-9a-f]{64}", registry_fingerprint_value):
            raise SchemaControlError("plan_registry_fingerprint_invalid")
        transitions = self.PRODUCTION_MIGRATIONS.path(
            source_schema_version,
            self.TARGET_SCHEMA_VERSION,
        )
        payload = {
            "schema_version": 1,
            "installed_version": installed_version,
            "installed_commit": installed_commit,
            "source_schema_version": source_schema_version,
            "source_shape_fingerprint": source_shape_fingerprint,
            "target_release": target_release,
            "target_commit": target_commit,
            "target_schema_version": self.TARGET_SCHEMA_VERSION,
            "registry_fingerprint": registry_fingerprint_value,
            "conditional_preparation": {
                "preparation_id": (
                    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION.preparation_id
                ),
                "definition_fingerprint": preparation_definition_fingerprint(
                    STAGE660128_REMEDIATION_COMPATIBILITY_PREPARATION
                ),
            },
            "transitions": [
                {
                    "migration_id": migration.migration_id,
                    "from_version": migration.from_version,
                    "to_version": migration.to_version,
                    "definition_fingerprint": migration_definition_fingerprint(
                        migration
                    ),
                }
                for migration in transitions
            ],
        }
        return hashlib.sha256(canonical_bytes(payload)).hexdigest()

    def validate_initial_context_database(self,
        db: Session,
        *,
        context: UpdateContext,
    ) -> None:
        (
            installed_version,
            installed_commit,
            source_schema,
            source_shape,
        ) = self.validate_source_lineage(
            db,
            request_id=context.request_id,
            target_release=context.target_release,
            target_commit=context.target_commit,
        )
        if (
            installed_version != context.installed_version
            or installed_commit != context.installed_commit
            or source_schema != context.source_schema_version
            or source_shape != context.source_shape_fingerprint
        ):
            raise SchemaControlError("installed_source_context_changed")

    def target_shape_is_exact(self, db: Session) -> tuple[bool, str]:
        actual = self.database_shape_fingerprint(db)
        if (
            not self.TARGET_SHAPE_FINGERPRINTS
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                for fingerprint in self.TARGET_SHAPE_FINGERPRINTS
            )
        ):
            raise SchemaControlError("target_shape_evidence_missing")
        return actual in self.TARGET_SHAPE_FINGERPRINTS, actual
