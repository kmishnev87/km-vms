from __future__ import annotations
import hashlib
import re
from typing import Any, Iterable
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from app.models.schema_version import SchemaMigrationHistory
from app.services.schema_update_types import canonical_bytes


def _normalize_sql(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _constraint_payload(inspector: Any, table_name: str) -> dict[str, Any]:
    primary = inspector.get_pk_constraint(table_name, schema="public") or {}
    unique = [
        {
            "name": str(item.get("name") or ""),
            "columns": sorted(
                str(column) for column in item.get("column_names") or []
            ),
        }
        for item in inspector.get_unique_constraints(table_name, schema="public")
    ]
    foreign = [
        {
            "name": str(item.get("name") or ""),
            "columns": [str(column) for column in item.get("constrained_columns") or []],
            "referred_schema": str(item.get("referred_schema") or ""),
            "referred_table": str(item.get("referred_table") or ""),
            "referred_columns": [
                str(column) for column in item.get("referred_columns") or []
            ],
            "options": {
                str(key): str(value)
                for key, value in sorted((item.get("options") or {}).items())
            },
        }
        for item in inspector.get_foreign_keys(table_name, schema="public")
    ]
    checks = [
        {
            "name": str(item.get("name") or ""),
            "sql": _normalize_sql(item.get("sqltext")),
        }
        for item in inspector.get_check_constraints(table_name, schema="public")
    ]
    return {
        "primary": {
            "name": str(primary.get("name") or ""),
            "columns": [
                str(column) for column in primary.get("constrained_columns") or []
            ],
        },
        "unique": sorted(unique, key=canonical_bytes),
        "foreign": sorted(foreign, key=canonical_bytes),
        "checks": sorted(checks, key=canonical_bytes),
    }


def database_shape_payload(
    db: Session,
    *,
    exclude_tables: Iterable[str] = (),
) -> dict[str, Any]:
    inspector = inspect(db.connection())
    excluded = set(exclude_tables)
    tables: list[dict[str, Any]] = []
    for table_name in sorted(
        set(inspector.get_table_names(schema="public")) - excluded
    ):
        columns = [
            {
                "name": str(column.get("name") or ""),
                "type": str(column.get("type") or "").upper(),
                "nullable": bool(column.get("nullable")),
                "default": _normalize_sql(column.get("default")),
            }
            for column in inspector.get_columns(table_name, schema="public")
        ]
        indexes = [
            {
                "name": str(index.get("name") or ""),
                "columns": [
                    str(column) for column in index.get("column_names") or []
                ],
                "unique": bool(index.get("unique")),
                "predicate": _normalize_sql(
                    (index.get("dialect_options") or {}).get(
                        "postgresql_where"
                    )
                ),
            }
            for index in inspector.get_indexes(table_name, schema="public")
        ]
        tables.append(
            {
                "table": table_name,
                "columns": sorted(columns, key=lambda item: item["name"]),
                "indexes": sorted(indexes, key=lambda item: item["name"]),
                "constraints": _constraint_payload(inspector, table_name),
            }
        )
    return {"schema_version": 1, "tables": tables}


def database_shape_fingerprint(
    db: Session,
    *,
    exclude_tables: Iterable[str] = (),
) -> str:
    return hashlib.sha256(
        canonical_bytes(
            database_shape_payload(db, exclude_tables=exclude_tables)
        )
    ).hexdigest()


def current_schema_version(db: Session) -> int | None:
    exists = db.execute(
        text(
            "SELECT to_regclass('public.schema_version_state') "
            "IS NOT NULL"
        )
    ).scalar_one()
    if not exists:
        return None
    value = db.execute(
        text(
            "SELECT schema_version FROM schema_version_state "
            "WHERE id='current'"
        )
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def database_is_empty(db: Session) -> bool:
    inspector = inspect(db.connection())
    return not inspector.get_table_names(schema="public")


def canonical_schema_history_fingerprint(db: Session) -> str:
    rows = (
        db.query(SchemaMigrationHistory)
        .order_by(
            SchemaMigrationHistory.id.asc(),
            SchemaMigrationHistory.migration_id.asc(),
        )
        .all()
    )
    payload = [
        {
            "id": int(row.id),
            "migration_id": row.migration_id,
            "previous_version": row.previous_version,
            "target_version": row.target_version,
            "schema_version": row.schema_version,
            "baseline_id": row.baseline_id,
            "app_version": row.app_version,
            "app_build_version": row.app_build_version,
            "applied_at": (
                row.applied_at.isoformat()
                if row.applied_at is not None
                else None
            ),
            "status": row.status,
            "checksum": row.checksum,
            "source": row.source,
            "service_name": row.service_name,
            "details": row.details,
            "error_summary": row.error_summary,
            "created_at": (
                row.created_at.isoformat()
                if row.created_at is not None
                else None
            ),
        }
        for row in rows
    ]
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
