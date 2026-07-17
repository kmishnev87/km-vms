from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState


CURRENT_SCHEMA_VERSION = 6
CURRENT_BASELINE_ID = "chapter06_stage4_baseline"
CURRENT_MIGRATION_ID = f"{CURRENT_BASELINE_ID}_schema_v{CURRENT_SCHEMA_VERSION}"
CURRENT_STATE_ID = "current"
SCHEMA_METADATA_TABLES = {"schema_version_state", "schema_migration_history"}
BASELINE_MODEL_TABLES = {
    "users",
    "cameras",
    "system_settings",
    "setup_locks",
    "archive_roots",
    "archive_export_jobs",
    "recording_jobs",
    "recording_segments",
    "audit_events",
    "user_workspace_layouts",
    "storage_operations",
    "storage_worker_leases",
    "storage_work_signals",
    "archive_migration_plans",
    "archive_migration_items",
}
LEGACY_DB_ONLY_TABLES = {"recorder_runtime_status"}
KNOWN_SAFE_MISSING_TABLES = {
    "setup_locks",
    "recorder_runtime_status",
    "user_workspace_layouts",
    "archive_export_jobs",
    "storage_operations",
    "storage_worker_leases",
    "storage_work_signals",
    "archive_migration_plans",
    "archive_migration_items",
}
KNOWN_OPTIONAL_MISSING_TABLES = {
    "archive_export_jobs",
    "storage_operations",
    "storage_worker_leases",
    "storage_work_signals",
    "archive_migration_plans",
    "archive_migration_items",
}
KNOWN_CAMERA_NULLABLE_DRIFT_COLUMNS = {"segment_minutes", "retention_days", "storage_quota_gb"}
KNOWN_SAFE_MISSING_COLUMNS = {"system_settings": {"system_name"}, "cameras": {"rtsp_host", "rtsp_port", "deleted_at"}}
SAFE_STATUSES = {"current", "adopted_baseline", "drift_known_safe"}
BLOCKED_STATUSES = {"unknown", "future_version", "downgrade_blocked", "drift_blocked", "adoption_failed"}
METADATA_INCOMPLETE_STATUS = "metadata_incomplete"


class SchemaVersionBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("summary") or status)


@dataclass
class SchemaShape:
    tables: dict[str, dict[str, Any]]

    @property
    def table_names(self) -> set[str]:
        return set(self.tables)


def create_schema_version_tables(bind) -> None:
    SchemaVersionState.__table__.create(bind=bind, checkfirst=True)
    SchemaMigrationHistory.__table__.create(bind=bind, checkfirst=True)


def inspect_schema_shape(bind) -> SchemaShape:
    inspector = inspect(bind)
    tables: dict[str, dict[str, Any]] = {}
    for table_name in sorted(inspector.get_table_names()):
        columns = {}
        try:
            for column in inspector.get_columns(table_name):
                columns[column["name"]] = {
                    "nullable": bool(column.get("nullable")),
                    "type": str(column.get("type")),
                }
        except Exception:
            columns = {}
        tables[table_name] = {"columns": columns}
    return SchemaShape(tables=tables)


def shape_from_tables(tables: dict[str, dict[str, Any]]) -> SchemaShape:
    return SchemaShape(tables=tables)


def _bounded_list(values: set[str] | list[str], limit: int = 20) -> list[str]:
    return sorted(str(value)[:120] for value in list(values)[:limit])


def _checksum(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def classify_schema_shape(shape: SchemaShape) -> dict[str, Any]:
    table_names = shape.table_names
    product_tables = table_names - SCHEMA_METADATA_TABLES
    if not product_tables:
        return {
            "status": "current",
            "source": "fresh_create_all",
            "summary": "Fresh database before product schema creation.",
            "known_safe_drift": [],
            "unsafe_drift": [],
            "table_counts": {"observed": 0},
        }

    expected_or_legacy = BASELINE_MODEL_TABLES | LEGACY_DB_ONLY_TABLES | SCHEMA_METADATA_TABLES
    missing_tables = BASELINE_MODEL_TABLES - table_names
    unknown_tables = product_tables - expected_or_legacy
    known_safe: list[dict[str, Any]] = []
    unsafe: list[dict[str, Any]] = []

    for table in sorted(missing_tables):
        if table in KNOWN_SAFE_MISSING_TABLES:
            known_safe.append({"type": "missing_table", "table": table, "classification": "known_safe_stage1_drift"})
        else:
            unsafe.append({"type": "missing_required_table", "table": table})

    if "recorder_runtime_status" in table_names:
        known_safe.append(
            {
                "type": "db_only_table",
                "table": "recorder_runtime_status",
                "classification": "legacy_runtime_owned_tolerated",
            }
        )

    if unknown_tables:
        unsafe.append({"type": "unknown_extra_tables", "tables": _bounded_list(unknown_tables)})

    system_columns = shape.tables.get("system_settings", {}).get("columns", {})
    if "system_settings" in table_names and "system_name" not in system_columns:
        known_safe.append({"type": "missing_column", "table": "system_settings", "column": "system_name"})

    camera_columns = shape.tables.get("cameras", {}).get("columns", {})
    for column in sorted(KNOWN_SAFE_MISSING_COLUMNS.get("cameras", set())):
        if "cameras" in table_names and column not in camera_columns:
            known_safe.append({"type": "missing_column", "table": "cameras", "column": column})
    nullable_drift = []
    for column in sorted(KNOWN_CAMERA_NULLABLE_DRIFT_COLUMNS):
        if column in camera_columns and camera_columns[column].get("nullable") is True:
            nullable_drift.append(column)
    if nullable_drift:
        known_safe.append(
            {
                "type": "nullable_column_drift",
                "table": "cameras",
                "columns": nullable_drift,
                "classification": "known_safe_stage1_drift_not_repaired",
            }
        )

    status_affecting_known_safe = [
        item
        for item in known_safe
        if item.get("type") != "db_only_table" and not (item.get("type") == "missing_table" and item.get("table") in KNOWN_OPTIONAL_MISSING_TABLES)
    ]

    if unsafe:
        status = "drift_blocked"
        source = "adoption_blocked"
        summary = "Existing unversioned database has unsafe or unknown schema drift."
    elif status_affecting_known_safe:
        status = "drift_known_safe"
        source = "adopted_existing_db"
        summary = "Existing unversioned database has only known safe drift."
    else:
        status = "adopted_baseline"
        source = "adopted_existing_db"
        summary = "Existing unversioned database matches the initial managed baseline."

    return {
        "status": status,
        "source": source,
        "summary": summary,
        "known_safe_drift": known_safe,
        "unsafe_drift": unsafe,
        "table_counts": {"observed": len(table_names), "expected_source": len(BASELINE_MODEL_TABLES)},
    }


def _sanitized_details(classification: dict[str, Any]) -> dict[str, Any]:
    return {
        "baseline_id": CURRENT_BASELINE_ID,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "classification": classification,
        "stage3_handoff": "Stage 3 migration runner must consume schema_version_state and schema_migration_history.",
        "recorder_metadata_owner": "api_bootstrap_only",
    }


def _event_exists(db: Session, migration_id: str, source: str) -> bool:
    return (
        db.query(SchemaMigrationHistory)
        .filter(SchemaMigrationHistory.migration_id == migration_id, SchemaMigrationHistory.source == source)
        .first()
        is not None
    )


def _record_history_once(
    db: Session,
    *,
    migration_id: str,
    previous_version: int | None,
    target_version: int,
    status: str,
    source: str,
    checksum: str,
    details: dict[str, Any],
    error_summary: str | None = None,
) -> None:
    if _event_exists(db, migration_id, source):
        return
    db.add(
        SchemaMigrationHistory(
            migration_id=migration_id,
            previous_version=previous_version,
            target_version=target_version,
            schema_version=target_version,
            baseline_id=CURRENT_BASELINE_ID,
            app_version=APP_VERSION,
            app_build_version=APP_BUILD_VERSION,
            status=status,
            checksum=checksum,
            source=source,
            service_name="api_bootstrap",
            details=details,
            error_summary=error_summary,
        )
    )


def _status_payload(row: SchemaVersionState | None, classification: dict[str, Any] | None = None) -> dict[str, Any]:
    if row is None:
        return {
            "managed": False,
            "schema_version": None,
            "baseline_id": None,
            "app_version": APP_VERSION,
            "app_build_version": APP_BUILD_VERSION,
            "status": "unversioned",
            "source": None,
            "known_safe_drift": [],
            "unsafe_drift": [],
        }
    drift = row.drift_classification or classification or {}
    return {
        "managed": row.status in SAFE_STATUSES,
        "schema_version": row.schema_version,
        "baseline_id": row.baseline_id,
        "app_version": row.app_version,
        "app_build_version": row.app_build_version,
        "status": row.status,
        "source": row.source,
        "metadata_checksum": row.metadata_checksum,
        "known_safe_drift": drift.get("known_safe_drift", []),
        "unsafe_drift": drift.get("unsafe_drift", []),
        "summary": drift.get("summary") or row.notes,
        "stage3_required": row.schema_version < CURRENT_SCHEMA_VERSION,
        "recorder_metadata_owner": "api_bootstrap_only",
    }


def _unversioned_status() -> dict[str, Any]:
    return {
        "managed": False,
        "schema_version": None,
        "baseline_id": None,
        "app_version": APP_VERSION,
        "app_build_version": APP_BUILD_VERSION,
        "status": "unversioned",
        "source": None,
        "known_safe_drift": [],
        "unsafe_drift": [],
        "summary": "Schema version metadata is not initialized. Adoption is required.",
        "stage3_required": False,
        "manual_review_required": False,
        "recorder_metadata_owner": "api_bootstrap_only",
    }


def _metadata_incomplete_status(summary: str, row: SchemaVersionState | None = None) -> dict[str, Any]:
    payload = _status_payload(row) if row is not None else _unversioned_status()
    payload.update(
        {
            "managed": False,
            "status": METADATA_INCOMPLETE_STATUS,
            "summary": summary,
            "manual_review_required": True,
            "stage3_required": False,
        }
    )
    return payload


def _blocked_status(row: SchemaVersionState, status: str, summary: str) -> dict[str, Any]:
    payload = _status_payload(row)
    payload.update(
        {
            "managed": False,
            "status": status,
            "summary": summary,
            "manual_review_required": True,
        }
    )
    return payload


def _valid_history_rows(db: Session, row: SchemaVersionState) -> list[SchemaMigrationHistory]:
    return (
        db.query(SchemaMigrationHistory)
        .filter(
            SchemaMigrationHistory.source == row.source,
            SchemaMigrationHistory.baseline_id == row.baseline_id,
            SchemaMigrationHistory.schema_version == row.schema_version,
            SchemaMigrationHistory.target_version == row.schema_version,
            SchemaMigrationHistory.status.in_(tuple(SAFE_STATUSES | {"applied"})),
        )
        .all()
    )


def _status_with_history_consistency(db: Session, row: SchemaVersionState | None) -> dict[str, Any]:
    if row is None:
        history_count = db.query(SchemaMigrationHistory).count()
        if history_count:
            return _metadata_incomplete_status("Schema history exists without a current schema state row.")
        return _unversioned_status()

    if row.status == "adoption_failed":
        return _blocked_status(row, "adoption_failed", "Schema adoption is incomplete or failed.")
    if row.schema_version > CURRENT_SCHEMA_VERSION:
        return _blocked_status(row, "future_version", "Database schema version is newer than this application supports.")
    if row.schema_version < CURRENT_SCHEMA_VERSION:
        return _blocked_status(row, "downgrade_blocked", "Stage 3 deterministic migration runner is required for lower schema versions.")
    if row.baseline_id != CURRENT_BASELINE_ID or row.status not in SAFE_STATUSES:
        return _blocked_status(row, "unknown", "Schema version metadata is unknown or malformed for this application.")

    valid_history = _valid_history_rows(db, row)
    if len(valid_history) != 1:
        return _metadata_incomplete_status("Schema current state has no exactly matching immutable baseline history row.", row)

    payload = _status_payload(row)
    payload["manual_review_required"] = False
    return payload


def validate_schema_version_pre_bootstrap(bind, pre_bootstrap_shape: SchemaShape) -> None:
    table_names = pre_bootstrap_shape.table_names
    product_tables = table_names - SCHEMA_METADATA_TABLES
    if "schema_version_state" in table_names:
        with Session(bind=bind) as db:
            row = db.get(SchemaVersionState, CURRENT_STATE_ID)
            if row is None:
                raise SchemaVersionBlocked(
                    "unknown",
                    {
                        "managed": False,
                        "status": "unknown",
                        "summary": "Schema version metadata table exists without a current state row.",
                    },
                )
            status = _status_with_history_consistency(db, row)
            if status["status"] == METADATA_INCOMPLETE_STATUS:
                raise SchemaVersionBlocked(METADATA_INCOMPLETE_STATUS, status)
            if row.schema_version > CURRENT_SCHEMA_VERSION:
                raise SchemaVersionBlocked("future_version", _status_payload(row))
            if row.schema_version < CURRENT_SCHEMA_VERSION:
                raise SchemaVersionBlocked("downgrade_blocked", _status_payload(row))
            if row.baseline_id != CURRENT_BASELINE_ID or row.status not in SAFE_STATUSES:
                raise SchemaVersionBlocked("unknown", _status_payload(row))
        return

    if not product_tables:
        return

    classification = classify_schema_shape(pre_bootstrap_shape)
    if classification["status"] == "drift_blocked":
        raise SchemaVersionBlocked(
            "drift_blocked",
            {
                "managed": False,
                "schema_version": CURRENT_SCHEMA_VERSION,
                "baseline_id": CURRENT_BASELINE_ID,
                "app_version": APP_VERSION,
                "app_build_version": APP_BUILD_VERSION,
                **classification,
            },
        )


def ensure_schema_version_state(db: Session, pre_bootstrap_shape: SchemaShape | None = None) -> dict[str, Any]:
    bind = db.get_bind()
    create_schema_version_tables(bind)
    row = db.get(SchemaVersionState, CURRENT_STATE_ID)
    if row is not None:
        if row.status == "adoption_failed":
            payload = _status_payload(row)
            raise SchemaVersionBlocked("adoption_failed", {**payload, "summary": "Previous adoption failed or is incomplete."})
        if row.schema_version > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionBlocked(
                "future_version",
                _blocked_status(row, "future_version", "Database schema version is newer than this application supports."),
            )
        if row.schema_version < CURRENT_SCHEMA_VERSION:
            raise SchemaVersionBlocked(
                "downgrade_blocked",
                _blocked_status(row, "downgrade_blocked", "Stage 3 deterministic migration runner is required for lower schema versions."),
            )
        if row.baseline_id != CURRENT_BASELINE_ID or row.status not in SAFE_STATUSES:
            raise SchemaVersionBlocked(
                "unknown",
                _blocked_status(row, "unknown", "Schema version metadata is unknown or malformed for this application."),
            )
        status = _status_with_history_consistency(db, row)
        if status["status"] == METADATA_INCOMPLETE_STATUS:
            raise SchemaVersionBlocked(METADATA_INCOMPLETE_STATUS, status)
        return _status_payload(row)

    if db.query(SchemaMigrationHistory).count():
        raise SchemaVersionBlocked(
            METADATA_INCOMPLETE_STATUS,
            _metadata_incomplete_status("Schema history exists without a current schema state row."),
        )

    classification = classify_schema_shape(pre_bootstrap_shape or inspect_schema_shape(bind))
    checksum = _checksum(classification)
    if classification["status"] == "drift_blocked":
        blocked = {
            "managed": False,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "baseline_id": CURRENT_BASELINE_ID,
            "app_version": APP_VERSION,
            "app_build_version": APP_BUILD_VERSION,
            **classification,
        }
        raise SchemaVersionBlocked("drift_blocked", blocked)

    now = datetime.utcnow()
    row = SchemaVersionState(
        id=CURRENT_STATE_ID,
        schema_version=CURRENT_SCHEMA_VERSION,
        baseline_id=CURRENT_BASELINE_ID,
        app_version=APP_VERSION,
        app_build_version=APP_BUILD_VERSION,
        status=classification["status"],
        source=classification["source"],
        metadata_checksum=checksum,
        drift_classification=classification,
        notes=classification["summary"][:500],
        applied_at=now,
    )
    db.add(row)
    _record_history_once(
        db,
        migration_id=CURRENT_MIGRATION_ID,
        previous_version=None,
        target_version=CURRENT_SCHEMA_VERSION,
        status=classification["status"],
        source=classification["source"],
        checksum=checksum,
        details=_sanitized_details(classification),
    )
    db.commit()
    return _status_payload(row, classification)


def schema_version_status(db: Session) -> dict[str, Any]:
    bind = db.get_bind()
    inspector = inspect(bind)
    has_state = inspector.has_table("schema_version_state")
    has_history = inspector.has_table("schema_migration_history")
    if not has_state and not has_history:
        return _unversioned_status()
    if has_state != has_history:
        return _metadata_incomplete_status("Schema version metadata tables are incomplete.")
    row = db.get(SchemaVersionState, CURRENT_STATE_ID)
    return _status_with_history_consistency(db, row)
