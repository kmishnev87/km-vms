from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.services.backup_before_upgrade import BackupExecutionConfig, BackupSafetyBlocked, build_backup_plan, create_backup_before_upgrade
from app.services.schema_migrations import (
    PRODUCTION_MIGRATIONS,
    MigrationRegistry,
    SchemaMigrationBlocked,
    build_migration_plan,
    execute_migration_plan,
)


MIGRATION_MAINTENANCE_REPORT_VERSION = "stage13.migration_apply.v1"
SENSITIVE_RE = re.compile(r"(password|passwd|secret|token|authorization|jwt|rtsp://|postgresql://|sqlite:///)[^,\s\"']*", re.IGNORECASE)


class MigrationMaintenanceBlocked(RuntimeError):
    def __init__(self, status: str, diagnostics: dict[str, Any]):
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(diagnostics.get("reason") or diagnostics.get("summary") or status)


def _utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sanitize(value: Any, limit: int = 500) -> str:
    return SENSITIVE_RE.sub("redacted=***", str(value or ""))[:limit]


def _safe_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(item) for item in value]
    if isinstance(value, str):
        return _sanitize(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize(value)


def _pending_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for index, item in enumerate(plan.get("pending_migrations") or [], start=1):
        items.append(
            {
                "order": index,
                "migration_id": _sanitize(item.get("migration_id"), 120),
                "from_version": item.get("from_version"),
                "to_version": item.get("to_version"),
                "description": _sanitize(item.get("description"), 240),
                "risk": _sanitize(item.get("risk"), 80),
                "requires_backup": True,
                "manual_only": bool(item.get("manual_only")),
                "auto_executable": bool(item.get("auto_executable")),
                "transaction_mode": _sanitize(item.get("transaction_mode"), 80),
                "rollback_note": _sanitize(item.get("rollback_note"), 240),
            }
        )
    return items


def _fingerprint(plan: dict[str, Any]) -> str:
    payload = {
        "current_schema_version": plan.get("current_schema_version"),
        "target_schema_version": plan.get("target_schema_version"),
        "pending_migrations": _pending_items(plan),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")).hexdigest()


def _status_from_plan(plan: dict[str, Any], backup_plan: dict[str, Any] | None = None, backup_block: dict[str, Any] | None = None) -> tuple[str, str]:
    if plan.get("status") == "current":
        return "current", "Schema is current; no pending migrations."
    if plan.get("status") == "ready":
        if backup_block:
            return "blocked", backup_block.get("summary") or "Backup root is not ready."
        if backup_plan and not backup_plan.get("backup_root_persistent"):
            return "blocked", "Configured backup root is not verified as persistent."
        return "pending", plan.get("summary") or "Pending migrations are ready for explicit apply."
    return "blocked", plan.get("summary") or plan.get("blocked_reason") or "Migration plan is blocked."


def _build_report(
    *,
    mode: str,
    status: str,
    reason: str,
    plan: dict[str, Any],
    backup: dict[str, Any] | None,
    actor: Any = None,
    applied_migrations: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    pending = _pending_items(plan)
    report = {
        "report_version": MIGRATION_MAINTENANCE_REPORT_VERSION,
        "report_id": f"kmvms-migration-apply-{uuid.uuid4().hex[:12]}",
        "operation": "schema_migration_apply",
        "mode": mode,
        "generated_at": _utc_iso(),
        "actor": {
            "user_id": getattr(actor, "id", None),
            "username": _sanitize(getattr(actor, "username", None), 100) if getattr(actor, "username", None) else None,
            "role": _sanitize(getattr(actor, "role", None), 50) if getattr(actor, "role", None) else None,
        },
        "status": status,
        "reason": _sanitize(reason),
        "current_version": plan.get("current_schema_version"),
        "target_version": plan.get("target_schema_version"),
        "plan_status": plan.get("status"),
        "plan_blocked_reason": _sanitize(plan.get("blocked_reason"), 160) if plan.get("blocked_reason") else None,
        "plan_fingerprint": _fingerprint(plan),
        "pending_count": len(pending),
        "pending_migrations": pending,
        "backup": backup
        or {
            "required": mode == "apply",
            "status": "not_created_for_read_only_operation",
            "backup_root_status": "not_checked_for_write",
            "backup_root_persistent": None,
        },
        "applied_migrations": [_sanitize(item, 120) for item in (applied_migrations or [])],
        "mutation_scope": {
            "schema_metadata": mode == "apply" and status == "applied",
            "schema_migrations": mode == "apply" and status == "applied",
            "business_data_outside_migration_runner": False,
            "recordings_or_archive_files": False,
        },
        "side_effects": {
            "db_mutated": mode == "apply" and status == "applied",
            "backup_created": bool(backup and backup.get("status") == "verified"),
            "migration_executed": bool(applied_migrations),
        },
        "startup_execution_policy": "no_startup_auto_apply",
        "rollback_guidance": "Automatic rollback is not performed by Stage 2; use a separate restore/rollback operator flow if recovery is needed.",
        "warnings": warnings or [],
        "redaction": {
            "raw_db_url_included": False,
            "raw_backup_path_included": False,
            "raw_sql_included": False,
            "passwords_or_tokens_included": False,
        },
    }
    assert_migration_report_secret_safe(report)
    return report


def assert_migration_report_secret_safe(report: dict[str, Any]) -> None:
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
    if SENSITIVE_RE.search(rendered):
        raise MigrationMaintenanceBlocked(
            "unsafe_report",
            {"status": "blocked", "reason": "Migration maintenance report contains sensitive-looking data."},
        )


def inspect_migration_maintenance(
    db: Session,
    *,
    registry: MigrationRegistry = PRODUCTION_MIGRATIONS,
    include_backup_plan: bool = True,
    actor: Any = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    backup_plan: dict[str, Any] | None = None
    backup_block: dict[str, Any] | None = None
    try:
        plan = build_migration_plan(db, registry=registry)
    except SchemaMigrationBlocked as exc:
        plan = _safe_jsonable(exc.diagnostics)
        plan["status"] = "blocked"
        plan["blocked_reason"] = exc.status
    if include_backup_plan and plan.get("status") == "ready":
        try:
            backup_plan = build_backup_plan(db, migration_plan_summary={"operation": "schema_migration_apply", "plan": _safe_jsonable(plan)})
        except BackupSafetyBlocked as exc:
            backup_block = {
                "status": "blocked",
                "summary": _sanitize(exc.diagnostics.get("summary") or exc.status),
                "backup_root_status": exc.diagnostics.get("root_status") or exc.status,
                "backup_root_persistent": False,
            }
            warnings.append("Configured backup root is not ready; apply is blocked.")
    status, reason = _status_from_plan(plan, backup_plan, backup_block)
    pending = _pending_items(plan)
    backup_summary = None
    if backup_block:
        backup_summary = {
            "required": True,
            "status": backup_block["status"],
            "backup_root_status": backup_block["backup_root_status"],
            "backup_root_persistent": False,
            "backup_created": False,
        }
    elif backup_plan:
        backup_summary = {
            "required": bool(pending),
            "status": backup_plan.get("status"),
            "backup_root_status": backup_plan.get("backup_root_status"),
            "backup_root_persistent": backup_plan.get("backup_root_persistent"),
            "creates_backup_files": False,
        }
    report = _build_report(mode="status", status=status, reason=reason, plan=plan, backup=backup_summary, actor=actor, warnings=warnings)
    return {
        "status": status,
        "reason": report["reason"],
        "current_version": plan.get("current_schema_version"),
        "target_version": plan.get("target_schema_version"),
        "pending_count": len(pending),
        "pending_migrations": pending,
        "plan_id": report["plan_fingerprint"],
        "backup_required": status == "pending",
        "backup_root_status": (backup_summary or {}).get("backup_root_status"),
        "can_apply": status == "pending" and bool((backup_summary or {}).get("backup_root_persistent")),
        "requires_confirmation": status == "pending",
        "blocked_reason": plan.get("blocked_reason") if status == "blocked" else None,
        "warnings": warnings,
        "report_id": report["report_id"],
        "report": report,
    }


def dry_run_migration_maintenance(
    db: Session,
    *,
    registry: MigrationRegistry = PRODUCTION_MIGRATIONS,
    actor: Any = None,
) -> dict[str, Any]:
    payload = inspect_migration_maintenance(db, registry=registry, include_backup_plan=True, actor=actor)
    payload["dry_run"] = True
    payload["mutates_database"] = False
    payload["creates_backup_files"] = False
    payload["migration_executed"] = False
    payload["report"]["mode"] = "dry_run"
    return payload


def apply_migration_maintenance(
    db: Session,
    *,
    confirm: bool,
    registry: MigrationRegistry = PRODUCTION_MIGRATIONS,
    actor: Any = None,
    backup_root: str | None = None,
    allow_tmp_backup_root_for_tests: bool = False,
) -> dict[str, Any]:
    if not confirm:
        raise MigrationMaintenanceBlocked(
            "confirmation_required",
            {"status": "blocked", "reason": "Explicit confirm=true is required for migration apply."},
        )

    before = inspect_migration_maintenance(db, registry=registry, include_backup_plan=False, actor=actor)
    if before["status"] == "current":
        result = {**before, "applied": False, "idempotent": True}
        result["report"]["mode"] = "apply"
        result["report"]["status"] = "current"
        return result
    if before["status"] != "pending":
        raise MigrationMaintenanceBlocked(str(before.get("blocked_reason") or before["status"]), before)

    plan = build_migration_plan(db, registry=registry)
    if plan.get("status") != "ready" or not plan.get("pending_migrations"):
        raise MigrationMaintenanceBlocked(
            str(plan.get("blocked_reason") or plan.get("status") or "blocked"),
            inspect_migration_maintenance(db, registry=registry, include_backup_plan=True, actor=actor),
        )

    backup_config = BackupExecutionConfig(
        backup_root=Path(backup_root) if backup_root else None,
        source="pre_upgrade",
        allow_tmp_for_tests=allow_tmp_backup_root_for_tests,
    )
    try:
        backup_plan = build_backup_plan(
            db,
            config=backup_config,
            migration_plan_summary={"operation": "schema_migration_apply", "plan": _safe_jsonable(plan)},
        )
    except BackupSafetyBlocked as exc:
        raise MigrationMaintenanceBlocked(
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
                "migration_executed": False,
            },
        ) from exc
    if not backup_plan.get("backup_root_persistent") and not allow_tmp_backup_root_for_tests:
        raise MigrationMaintenanceBlocked(
            "backup_root_not_persistent",
            {
                "status": "blocked",
                "reason": "Configured backup root is not verified as persistent.",
                "backup_root_status": backup_plan.get("backup_root_status"),
                "backup_root_persistent": False,
                "migration_executed": False,
            },
        )

    try:
        backup = create_backup_before_upgrade(
            db,
            config=backup_config,
            migration_plan_summary={"operation": "schema_migration_apply", "plan": _safe_jsonable(plan)},
        )
    except BackupSafetyBlocked as exc:
        raise MigrationMaintenanceBlocked(
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
                "migration_executed": False,
            },
        ) from exc

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

    try:
        executed = execute_migration_plan(db, registry=registry, backup_manifest_path=backup["manifest_path"])
    except SchemaMigrationBlocked as exc:
        failed_report = _build_report(
            mode="apply",
            status="failed",
            reason=_sanitize(exc.diagnostics.get("summary") or exc.status),
            plan=plan,
            backup=backup_summary,
            actor=actor,
            applied_migrations=[],
            warnings=["Migration apply failed after backup; restore/rollback remains a separate operator flow."],
        )
        raise MigrationMaintenanceBlocked(
            exc.status,
            {
                "status": "failed",
                "reason": failed_report["reason"],
                "backup_created": True,
                "migration_executed": False,
                "report_id": failed_report["report_id"],
                "report": failed_report,
            },
        ) from exc

    applied = list(executed.get("executed_migrations") or [])
    after_plan = build_migration_plan(db, registry=registry)
    report = _build_report(
        mode="apply",
        status="applied" if applied else "current",
        reason="Migration apply completed." if applied else "Schema is current; no pending migrations.",
        plan=after_plan,
        backup=backup_summary,
        actor=actor,
        applied_migrations=applied,
    )
    return {
        "status": report["status"],
        "reason": report["reason"],
        "applied": bool(applied),
        "idempotent": not bool(applied),
        "current_version": after_plan.get("current_schema_version"),
        "target_version": after_plan.get("target_schema_version"),
        "applied_count": len(applied),
        "applied_migrations": applied,
        "backup_required": True,
        "backup_status": backup_summary["status"],
        "backup_root_status": backup_summary["backup_root_status"],
        "backup_root_persistent": backup_summary["backup_root_persistent"],
        "migration_executed": bool(applied),
        "business_data_outside_migration_runner_mutated": False,
        "report_id": report["report_id"],
        "report": report,
    }
