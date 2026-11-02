"""Read/write compatibility is separate from the physical schema revision.

Only a completed, verified migration can issue this bounded database certificate.
An older prepared runtime verifies it without rewriting schema/history or running
DDL. Unknown changes and missing evidence still fail closed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState

PROTOCOL_VERSION = 1
IMAGE_LABEL = "org.km-vms.schema-runtime-compatibility"
METADATA_KEY = "runtime_read_write_compatibility"
MIN_PREPARED_SCHEMA = 9


class RuntimeCompatibilityBlocked(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str).encode("utf-8")


def _signature(payload: dict[str, Any]) -> str:
    secret = os.getenv("JWT_SECRET", "")
    if len(secret) < 16:
        raise RuntimeCompatibilityBlocked("schema_compatibility_secret_unavailable")
    key = hmac.new(secret.encode(), b"KMVMS|schema-read-write-compatibility|v1", hashlib.sha256).digest()
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def migration_compatibility_floor(migration: Any) -> int | None:
    floor = getattr(migration, "runtime_compatibility_floor", None)
    if type(floor) is not int or not MIN_PREPARED_SCHEMA <= floor <= migration.from_version:
        return None
    return floor


def target_compatibility_floor(registry: Any, target: int) -> int:
    """Every edge must explicitly preserve the whole advertised older contract."""
    floor = target
    for candidate in range(MIN_PREPARED_SCHEMA, target):
        try:
            path = registry.path(candidate, target)
        except (ValueError, RuntimeError):
            continue
        if path and all((value := migration_compatibility_floor(item)) is not None and value <= candidate for item in path):
            floor = candidate
            break
    return floor


def _control_snapshot(db: Session) -> str:
    tables = set(inspect(db.connection()).get_table_names())
    control_tables = {"schema_migration_control", "schema_migration_attempts"}
    if bool(tables & control_tables) and not control_tables <= tables:
        raise RuntimeCompatibilityBlocked("schema_compatibility_control_partial")
    snapshot: dict[str, Any] = {}
    if control_tables <= tables:
        for table in sorted(control_tables):
            snapshot[table] = [dict(row) for row in db.execute(text(f'SELECT * FROM "{table}" ORDER BY 1')).mappings()]
        controls = snapshot["schema_migration_control"]
        if len(controls) > 1 or any(row["id"] != "current" or row["state"] != "completed" for row in controls):
            raise RuntimeCompatibilityBlocked("schema_compatibility_migration_not_completed")
        if any(row["status"] == "started" for row in snapshot["schema_migration_attempts"]):
            raise RuntimeCompatibilityBlocked("schema_compatibility_attempt_not_completed")
    return hashlib.sha256(_canonical(snapshot)).hexdigest()


def _observed_payload(db: Session, state: SchemaVersionState, floor: int) -> dict[str, Any]:
    # Lazy imports avoid a versioning -> migrations -> versioning import cycle.
    from app.services.schema_update_control import canonical_schema_history_fingerprint, database_shape_fingerprint
    from app.services.schema_versioning import CURRENT_BASELINE_ID, SAFE_STATUSES

    states = db.query(SchemaVersionState).all()
    if len(states) != 1 or state.id != "current" or state.baseline_id != CURRENT_BASELINE_ID or state.status not in SAFE_STATUSES:
        raise RuntimeCompatibilityBlocked("schema_compatibility_state_invalid")
    matching = db.query(SchemaMigrationHistory).filter(
        SchemaMigrationHistory.source == state.source,
        SchemaMigrationHistory.baseline_id == state.baseline_id,
        SchemaMigrationHistory.schema_version == state.schema_version,
        SchemaMigrationHistory.target_version == state.schema_version,
        SchemaMigrationHistory.status.in_(tuple(SAFE_STATUSES | {"applied"})),
    ).count()
    if matching != 1:
        raise RuntimeCompatibilityBlocked("schema_compatibility_history_invalid")
    return {
        "purpose": "schema_runtime_read_write",
        "protocol": PROTOCOL_VERSION,
        "schema_version": state.schema_version,
        "baseline_id": state.baseline_id,
        "minimum_reader_version": floor,
        "minimum_writer_version": floor,
        "shape_sha256": database_shape_fingerprint(db),
        "history_sha256": canonical_schema_history_fingerprint(db),
        "control_sha256": _control_snapshot(db),
    }


def publish_runtime_compatibility(db: Session) -> dict[str, Any] | None:
    """Caller owns the transaction; never commit or modify immutable history."""
    from app.services.schema_migrations import PRODUCTION_MIGRATIONS, validate_current_target_schema
    from app.services.schema_update_control import target_shape_is_exact
    from app.services.schema_versioning import CURRENT_SCHEMA_VERSION

    state = db.get(SchemaVersionState, "current")
    if state is None or state.schema_version != CURRENT_SCHEMA_VERSION:
        raise RuntimeCompatibilityBlocked("schema_compatibility_target_invalid")
    floor = target_compatibility_floor(PRODUCTION_MIGRATIONS, CURRENT_SCHEMA_VERSION)
    if floor == CURRENT_SCHEMA_VERSION:
        return None
    validate_current_target_schema(db)
    if not target_shape_is_exact(db)[0]:
        raise RuntimeCompatibilityBlocked("schema_compatibility_target_shape_invalid")
    db.flush()
    payload = _observed_payload(db, state, floor)
    state.drift_classification = {
        **(state.drift_classification or {}),
        METADATA_KEY: {"payload": payload, "signature": _signature(payload)},
    }
    db.flush()
    return payload


def validate_runtime_compatibility(db: Session, state: SchemaVersionState, runtime_schema: int) -> dict[str, Any]:
    if type(runtime_schema) is not int or not MIN_PREPARED_SCHEMA <= runtime_schema < state.schema_version:
        raise RuntimeCompatibilityBlocked("schema_compatibility_runtime_invalid")
    envelope = (state.drift_classification or {}).get(METADATA_KEY)
    if type(envelope) is not dict or set(envelope) != {"payload", "signature"}:
        raise RuntimeCompatibilityBlocked("schema_compatibility_certificate_missing")
    payload, signature = envelope["payload"], envelope["signature"]
    if type(payload) is not dict or type(signature) is not str or len(signature) != 64:
        raise RuntimeCompatibilityBlocked("schema_compatibility_certificate_invalid")
    if not hmac.compare_digest(signature, _signature(payload)):
        raise RuntimeCompatibilityBlocked("schema_compatibility_signature_invalid")
    floor = payload.get("minimum_writer_version")
    if type(floor) is not int or not MIN_PREPARED_SCHEMA <= floor <= runtime_schema or payload.get("minimum_reader_version") != floor:
        raise RuntimeCompatibilityBlocked("schema_compatibility_runtime_not_supported")
    if payload != _observed_payload(db, state, floor):
        raise RuntimeCompatibilityBlocked("schema_compatibility_database_evidence_changed")
    return payload


def supports_future_schema(db: Session, state: SchemaVersionState, runtime_schema: int) -> bool:
    try:
        validate_runtime_compatibility(db, state, runtime_schema)
        return True
    except RuntimeCompatibilityBlocked:
        return False
