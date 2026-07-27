from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.services.backup_before_upgrade import BackupExecutionConfig, BackupSafetyBlocked, build_backup_plan, create_backup_before_upgrade
from app.services.schema_migrations import build_migration_plan
from app.services.schema_versioning import (
    BASELINE_MODEL_TABLES,
    CURRENT_BASELINE_ID,
    CURRENT_SCHEMA_VERSION,
    CURRENT_STATE_ID,
    LEGACY_DB_ONLY_TABLES,
    KNOWN_OPTIONAL_MODEL_TABLES,
    METADATA_INCOMPLETE_STATUS,
    SAFE_STATUSES,
    SCHEMA_METADATA_TABLES,
    SchemaVersionBlocked,
    classify_schema_shape,
    ensure_schema_version_state,
    inspect_schema_shape,
    schema_version_status,
)


ADOPTION_REPORT_VERSION = "stage13.db_adoption.v1"
SAFE_REASON_RE = re.compile(r"(password|passwd|secret|token|authorization|jwt|rtsp://|postgresql://|sqlite:///)[^,\s\"']*", re.IGNORECASE)


class DbAdoptionBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("reason") or diagnostics.get("summary") or status)


def _utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sanitize(value: Any) -> str:
    return SAFE_REASON_RE.sub(lambda match: match.group(1).split(":")[0] + "=***", str(value or ""))[:300]


def _bounded(values: set[str] | list[str], limit: int = 20) -> list[str]:
    return sorted(str(item)[:120] for item in list(values))[:limit]


def _metadata_tables_present(db: Session) -> dict[str, bool]:
    inspector = inspect(db.get_bind())
    return {
        table_name: inspector.has_table(table_name)
        for table_name in sorted(SCHEMA_METADATA_TABLES)
    }


def _metadata_summary(db: Session) -> dict[str, Any]:
    presence = _metadata_tables_present(db)
    has_state = presence["schema_version_state"]
    has_history = presence["schema_migration_history"]
    row = db.get(SchemaVersionState, CURRENT_STATE_ID) if has_state else None
    history_count = db.query(SchemaMigrationHistory).count() if has_history else 0
    return {
        "metadata_present": has_state and has_history and row is not None,
        "any_metadata_present": any(presence.values()),
        "metadata_tables": presence,
        "current_state_present": row is not None,
        "history_count": history_count,
        "schema_version": row.schema_version if row else None,
        "baseline_id": row.baseline_id if row else None,
        "status": row.status if row else None,
        "source": row.source if row else None,
    }


def _safe_table_summary(shape) -> dict[str, Any]:
    table_names = shape.table_names
    product_tables = table_names - SCHEMA_METADATA_TABLES
    required_missing = BASELINE_MODEL_TABLES - table_names
    expected_or_metadata = (
        BASELINE_MODEL_TABLES
        | LEGACY_DB_ONLY_TABLES
        | KNOWN_OPTIONAL_MODEL_TABLES
        | SCHEMA_METADATA_TABLES
    )
    unknown_tables = product_tables - expected_or_metadata
    return {
        "known_tables_found": len(product_tables & BASELINE_MODEL_TABLES),
        "known_table_sample": _bounded(product_tables & BASELINE_MODEL_TABLES, 20),
        "required_tables_missing": _bounded(required_missing, 20),
        "unknown_tables_count": len(unknown_tables),
        "unknown_table_sample": _bounded(unknown_tables, 10),
        "observed_table_count": len(table_names),
        "metadata_table_count": len(table_names & SCHEMA_METADATA_TABLES),
    }


def _status_from_schema_status(status_payload: dict[str, Any]) -> str:
    if status_payload.get("managed") and status_payload.get("status") in SAFE_STATUSES:
        return "already_adopted"
    return "blocked"


def _build_report(
    *,
    operation: str,
    status: str,
    reason: str,
    shape,
    metadata_before: dict[str, Any],
    metadata_after: dict[str, Any] | None = None,
    backup: dict[str, Any] | None = None,
    actor: Any = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    table_summary = _safe_table_summary(shape)
    report = {
        "report_version": ADOPTION_REPORT_VERSION,
        "report_id": f"kmvms-db-adoption-{uuid.uuid4().hex[:12]}",
        "operation": "db_adoption",
        "mode": operation,
        "generated_at": _utc_iso(),
        "actor": {
            "user_id": getattr(actor, "id", None),
            "username": str(getattr(actor, "username", "") or "")[:100] or None,
            "role": str(getattr(actor, "role", "") or "")[:50] or None,
        },
        "status": status,
        "reason": _sanitize(reason),
        "table_validation": table_summary,
        "metadata_before": metadata_before,
        "metadata_after": metadata_after,
        "backup": backup
        or {
            "required": operation == "apply",
            "status": "not_created_for_read_only_operation",
            "backup_root_status": "not_checked_for_write",
            "backup_root_persistent": None,
        },
        "mutation_scope": {
            "schema_metadata_tables": status == "adopted",
            "business_tables": False,
            "migration_executed": False,
            "backup_created": bool(backup and backup.get("status") == "verified"),
        },
        "side_effects": {
            "db_mutated": status == "adopted",
            "business_data_mutated": False,
            "migration_executed": False,
        },
        "warnings": warnings or [],
        "redaction": {
            "raw_db_url_included": False,
            "raw_backup_path_included": False,
            "passwords_or_tokens_included": False,
        },
    }
    assert_adoption_report_secret_safe(report)
    return report


def assert_adoption_report_secret_safe(report: dict[str, Any]) -> None:
    def iter_values(value: Any):
        if isinstance(value, dict):
            for item in value.values():
                yield from iter_values(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                yield from iter_values(item)
        else:
            yield value

    rendered = "\n".join(str(item) for item in iter_values(report))
    if SAFE_REASON_RE.search(rendered):
        raise DbAdoptionBlocked("unsafe_report", {"status": "blocked", "reason": "Adoption report contains sensitive-looking data."})


def inspect_db_adoption(db: Session, *, include_backup_plan: bool = True, actor: Any = None) -> dict[str, Any]:
    shape = inspect_schema_shape(db.get_bind())
    metadata = _metadata_summary(db)
    schema_status = schema_version_status(db)
    table_summary = _safe_table_summary(shape)
    warnings: list[str] = []
    backup_plan: dict[str, Any] | None = None

    if include_backup_plan:
        try:
            backup_plan = build_backup_plan(db, migration_plan_summary={"operation": "db_adoption", "status": "planned"})
        except BackupSafetyBlocked as exc:
            backup_plan = {
                "status": "blocked",
                "backup_root_status": exc.diagnostics.get("root_status") or exc.status,
                "backup_root_persistent": False,
                "summary": _sanitize(exc.diagnostics.get("summary") or exc.status),
                "creates_backup_files": False,
            }

    if metadata["any_metadata_present"]:
        status = _status_from_schema_status(schema_status)
        reason = "Schema metadata is already valid." if status == "already_adopted" else schema_status.get("summary", "Schema metadata is not adoptable.")
        report = _build_report(
            operation="status",
            status=status,
            reason=reason,
            shape=shape,
            metadata_before=metadata,
            backup=backup_plan,
            actor=actor,
            warnings=warnings,
        )
        return {
            "status": status,
            "reason": reason,
            "known_tables_found": table_summary["known_tables_found"],
            "required_tables_missing": table_summary["required_tables_missing"],
            "unknown_tables_count": table_summary["unknown_tables_count"],
            "unknown_table_sample": table_summary["unknown_table_sample"],
            "metadata_present": metadata["metadata_present"],
            "backup_required": False,
            "backup_root_status": backup_plan.get("backup_root_status") if backup_plan else None,
            "can_adopt": False,
            "already_adopted": status == "already_adopted",
            "report_id": report["report_id"],
            "warnings": warnings,
            "report": report,
        }

    classification = classify_schema_shape(shape)
    product_tables = shape.table_names - SCHEMA_METADATA_TABLES
    if not product_tables:
        status = "blocked"
        reason = "Database has no KM VMS product tables to adopt."
    elif classification["status"] == "drift_blocked":
        status = "blocked"
        reason = classification["summary"]
    else:
        status = "adoptable"
        reason = classification["summary"]

    if backup_plan and not backup_plan.get("backup_root_persistent"):
        warnings.append("Backup root is not verified as persistent; apply will be blocked.")

    report = _build_report(
        operation="status",
        status=status,
        reason=reason,
        shape=shape,
        metadata_before=metadata,
        backup=backup_plan,
        actor=actor,
        warnings=warnings,
    )
    return {
        "status": status,
        "reason": reason,
        "known_tables_found": table_summary["known_tables_found"],
        "required_tables_missing": table_summary["required_tables_missing"],
        "unknown_tables_count": table_summary["unknown_tables_count"],
        "unknown_table_sample": table_summary["unknown_table_sample"],
        "metadata_present": metadata["metadata_present"],
        "backup_required": status == "adoptable",
        "backup_root_status": backup_plan.get("backup_root_status") if backup_plan else None,
        "can_adopt": status == "adoptable" and (not backup_plan or bool(backup_plan.get("backup_root_persistent"))),
        "already_adopted": False,
        "report_id": report["report_id"],
        "warnings": warnings,
        "report": report,
    }


def dry_run_db_adoption(db: Session, *, actor: Any = None) -> dict[str, Any]:
    payload = inspect_db_adoption(db, include_backup_plan=True, actor=actor)
    payload["dry_run"] = True
    payload["mutates_database"] = False
    payload["creates_backup_files"] = False
    payload["migration_executed"] = False
    return payload


def apply_db_adoption(
    db: Session,
    *,
    confirm: bool,
    actor: Any = None,
    backup_root: str | None = None,
    allow_tmp_backup_root_for_tests: bool = False,
) -> dict[str, Any]:
    if not confirm:
        raise DbAdoptionBlocked(
            "confirmation_required",
            {"status": "blocked", "reason": "Explicit confirm=true is required for DB adoption apply."},
        )

    before = inspect_db_adoption(db, include_backup_plan=True, actor=actor)
    if before["status"] == "already_adopted":
        result = {**before, "status": "already_adopted", "applied": False, "idempotent": True}
        result["report"]["mode"] = "apply"
        result["report"]["status"] = "already_adopted"
        return result
    if before["status"] != "adoptable" or not before.get("can_adopt"):
        raise DbAdoptionBlocked(str(before["status"]), before)

    migration_plan = build_migration_plan(db, production_adoption_status="production_adoption_inspected")
    if migration_plan.get("status") not in {"blocked", "current"} or migration_plan.get("blocked_reason") not in {"unversioned", None}:
        raise DbAdoptionBlocked(
            "migration_state_blocks_adoption",
            {
                "status": "blocked",
                "reason": "Current migration state is not safe for metadata-only adoption.",
                "migration_status": migration_plan.get("status"),
                "blocked_reason": migration_plan.get("blocked_reason"),
            },
        )

    shape = inspect_schema_shape(db.get_bind())
    metadata_before = _metadata_summary(db)
    config = BackupExecutionConfig(
        backup_root=Path(backup_root) if backup_root else None,
        source="pre_adoption",
        allow_tmp_for_tests=allow_tmp_backup_root_for_tests,
    )
    try:
        backup = create_backup_before_upgrade(db, config=config, migration_plan_summary={"operation": "db_adoption", "status": "pre_adoption"})
    except BackupSafetyBlocked as exc:
        raise DbAdoptionBlocked(
            exc.status,
            {
                "status": "blocked",
                "reason": _sanitize(exc.diagnostics.get("summary") or exc.status),
                "backup": {
                    "status": exc.status,
                    "backup_root_status": exc.diagnostics.get("root_status") or exc.status,
                    "backup_root_persistent": False,
                    "backup_created": False,
                },
            },
        ) from exc

    try:
        metadata_result = ensure_schema_version_state(db, pre_bootstrap_shape=shape)
    except SchemaVersionBlocked as exc:
        raise DbAdoptionBlocked(exc.status, {"status": "blocked", "reason": _sanitize(exc.diagnostics.get("summary") or exc.status)}) from exc

    metadata_after = _metadata_summary(db)
    backup_summary = {
        "required": True,
        "status": backup["status"],
        "backup_root_status": "ready",
        "backup_root_persistent": not allow_tmp_backup_root_for_tests,
        "backup_id": backup["backup_id"],
        "backup_file_label": backup["backup_file_label"],
        "metadata_file_label": backup["metadata_file_label"],
        "manifest_reference": "configured_backup_root/manifest",
        "restore_validation_status": backup["restore_validation_status"],
    }
    report = _build_report(
        operation="apply",
        status="adopted",
        reason=metadata_result.get("summary") or "Schema metadata adopted.",
        shape=shape,
        metadata_before=metadata_before,
        metadata_after=metadata_after,
        backup=backup_summary,
        actor=actor,
    )
    return {
        "status": "adopted",
        "reason": report["reason"],
        "applied": True,
        "idempotent": False,
        "metadata_present": metadata_after["metadata_present"],
        "backup_required": True,
        "backup_status": backup_summary["status"],
        "backup_root_status": backup_summary["backup_root_status"],
        "backup_root_persistent": backup_summary["backup_root_persistent"],
        "migration_executed": False,
        "business_data_mutated": False,
        "report_id": report["report_id"],
        "report": report,
    }
