from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.version import APP_BUILD_VERSION, APP_VERSION
from app.models.schema_version import SchemaMigrationHistory, SchemaVersionState
from app.services.backup_before_upgrade import (
    DEFAULT_BACKUP_ROOT,
    RESTORE_VALIDATION_STATUS as BACKUP_RESTORE_NOT_PERFORMED,
    backup_precondition_status,
    sanitize_error,
    verify_backup_manifest,
)
from app.services.restore_validation import backup_restore_validated
from app.services.schema_migrations import PRODUCTION_ADOPTION_DEFERRED, build_migration_plan
from app.services.schema_versioning import CURRENT_SCHEMA_VERSION, CURRENT_STATE_ID, schema_version_status


REPORT_VERSION = "stage6.upgrade_report.v1"
REPORT_STATUS_COMPLETE = "complete"
REPORT_STATUS_PARTIAL = "partial"
SENSITIVE_RE = re.compile(
    r"(password|passwd|secret|token|authorization|jwt|rtsp://|postgresql://|sqlite:///)[^,\s\"']*",
    re.IGNORECASE,
)


def _utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sanitize(value: Any) -> str:
    return SENSITIVE_RE.sub("redacted=***", str(value or ""))[:500]


def _safe_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _sanitize(value) if isinstance(value, str) else value
    return _sanitize(value)


def assert_upgrade_report_secret_safe(report: dict[str, Any]) -> None:
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
        raise ValueError("upgrade report contains sensitive-looking data")


def _warning(code: str, severity: str, message: str, *, stage_target: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "stage_target": stage_target,
        "evidence": _safe_jsonable(evidence or {}),
    }


def _history_summary(db: Session) -> dict[str, Any]:
    inspector = inspect(db.get_bind())
    if not inspector.has_table("schema_migration_history"):
        return {
            "available": False,
            "status": "metadata_unavailable",
            "counts": {"total": 0, "applied": 0, "adopted": 0, "failed": 0, "blocked": 0},
            "bounded_items": [],
            "previous_schema_version": None,
        }

    rows = db.query(SchemaMigrationHistory).order_by(SchemaMigrationHistory.applied_at.asc(), SchemaMigrationHistory.id.asc()).all()
    counts = {"total": len(rows), "applied": 0, "adopted": 0, "failed": 0, "blocked": 0, "other": 0}
    bounded_items = []
    previous_values = []
    for row in rows:
        status = str(row.status or "unknown")
        if status == "applied":
            counts["applied"] += 1
        elif status in {"current", "adopted_baseline", "drift_known_safe"} or str(row.source or "").startswith("adopted"):
            counts["adopted"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif status in {"blocked", "incomplete", "manual_review", "unknown"}:
            counts["blocked"] += 1
        else:
            counts["other"] += 1
        if row.previous_version is not None:
            previous_values.append(row.previous_version)
        if len(bounded_items) < 20:
            bounded_items.append(
                {
                    "migration_id": _sanitize(row.migration_id),
                    "source": _sanitize(row.source),
                    "status": _sanitize(status),
                    "previous_version": row.previous_version,
                    "target_version": row.target_version,
                    "schema_version": row.schema_version,
                    "applied_at": row.applied_at.isoformat() + "Z" if row.applied_at else None,
                    "error_summary": _sanitize(row.error_summary) if row.error_summary else None,
                }
            )
    return {
        "available": True,
        "status": "available",
        "counts": counts,
        "bounded_items": bounded_items,
        "previous_schema_version": previous_values[-1] if previous_values else None,
    }


def _version_summary(db: Session, schema_status: dict[str, Any], history: dict[str, Any], migration_plan: dict[str, Any]) -> dict[str, Any]:
    row = db.get(SchemaVersionState, CURRENT_STATE_ID) if inspect(db.get_bind()).has_table("schema_version_state") else None
    return {
        "app_version": APP_VERSION,
        "app_build_version": APP_BUILD_VERSION,
        "app_build_version_source": "temporary_app_build_version_source",
        "app_build_version_limitation": "replacement deferred to Chapter 07 Stage 7.0",
        "schema_version_is_app_version": False,
        "previous_schema_version": history.get("previous_schema_version"),
        "current_schema_version": schema_status.get("schema_version"),
        "target_schema_version": migration_plan.get("target_schema_version", CURRENT_SCHEMA_VERSION),
        "supported_schema_version": CURRENT_SCHEMA_VERSION,
        "state_app_version": getattr(row, "app_version", None),
        "state_app_build_version": getattr(row, "app_build_version", None),
    }


def _pending_summary(migration_plan: dict[str, Any]) -> dict[str, Any]:
    pending = list(migration_plan.get("pending_migrations") or [])
    by_risk: dict[str, int] = {}
    bounded = []
    for item in pending:
        risk = str(item.get("risk") or "unknown")
        by_risk[risk] = by_risk.get(risk, 0) + 1
        if len(bounded) < 20:
            bounded.append(
                {
                    "migration_id": _sanitize(item.get("migration_id")),
                    "from_version": item.get("from_version"),
                    "to_version": item.get("to_version"),
                    "risk": risk,
                    "backup_required": bool(item.get("backup_required")),
                    "manual_only": bool(item.get("manual_only")),
                    "auto_executable": bool(item.get("auto_executable")),
                }
            )
    return {"count": len(pending), "by_risk": by_risk, "bounded_items": bounded}


def _backup_root_evidence() -> dict[str, Any]:
    configured = os.getenv("KMVMS_DB_BACKUP_ROOT") or settings.kmvms_db_backup_root or DEFAULT_BACKUP_ROOT
    known_container_only = configured == "/var/lib/km-vms/backups/db"
    tmp_final = configured.startswith("/tmp/")
    unsafe_archive_overlap = configured == "/storage/archive" or configured.startswith("/storage/archive/")
    if known_container_only:
        configured_contract_status = "unsafe_legacy_container_only"
    elif tmp_final:
        configured_contract_status = "configured_disposable_test_contract"
    elif unsafe_archive_overlap:
        configured_contract_status = "unsafe_archive_overlap"
    elif configured:
        configured_contract_status = "configured_persistent_contract"
    else:
        configured_contract_status = "not_configured"
    return {
        "configured_contract_status": configured_contract_status,
        "persistence_evidence_status": "unknown_safe_check_unavailable",
        "evidence_source": "current_config",
        "path_label": "configured_backup_root",
        "container_path_label": "configured_backup_root",
        "default_container_path": DEFAULT_BACKUP_ROOT,
        "legacy_container_only_blocked": known_container_only,
        "tmp_final_destination": tmp_final,
        "unsafe_archive_overlap": unsafe_archive_overlap,
        "read_only_check": True,
        "write_probe_performed": False,
        "host_mount_proven": False,
        "summary": "Report uses process configuration only. It records the configured contract but does not prove host mount persistence.",
    }


def _backup_summary(backup_manifest_path: str | Path | None, restore_validation_manifest_path: str | Path | None) -> dict[str, Any]:
    root_evidence = _backup_root_evidence()
    base = {
        "status": "backup_status_source_unavailable",
        "status_semantics": "source_unavailable",
        "backup_status_source": "source_unavailable",
        "backup_available": False,
        "backup_id": None,
        "created_at": None,
        "db_backend": "unknown",
        "file_size": None,
        "checksum_status": "unknown",
        "manifest_status": "source_unavailable",
        "backup_precondition_status": {
            "status": "unknown",
            "backup_required": True,
            "source": "source_unavailable",
            "summary": "No safe product-owned backup status source is connected to the read-only upgrade report.",
        },
        "restore_validation_status": "restore_status_source_unavailable",
        "restore_validation_status_source": "source_unavailable",
        "restore_validation_semantics": "source_unavailable",
        "backup_restore_validated": False,
        "backup_path_label": "not_reported",
        "raw_backup_path_included": False,
        "test_only_source": False,
        "source_limitation": "Endpoint and diagnostic archive do not accept arbitrary backup/restore manifest paths. Without a connected safe product-owned source, status is unknown.",
        "root_evidence": root_evidence,
    }
    if not backup_manifest_path:
        return base

    verification = verify_backup_manifest(backup_manifest_path)
    base["backup_status_source"] = "provided_manifest_path_for_test_only"
    base["test_only_source"] = True
    base["manifest_status"] = verification.get("status", "unknown")
    base["backup_precondition_status"] = backup_precondition_status(manifest_path=backup_manifest_path, required=True)
    if verification.get("valid"):
        base.update(
            {
                "status": "backup_available",
                "status_semantics": "backup_available",
                "backup_available": True,
                "backup_id": verification.get("backup_id"),
                "db_backend": verification.get("db_backend"),
                "file_size": verification.get("file_size"),
                "checksum_status": "matched",
                "backup_path_label": "configured_backup_root/backup_artifact",
                "backup_manifest_reference": "sanitized_manifest_reference",
                "backup_source_limitation": "Service-level test/disposable manifest reference only; not accepted from browser/API users.",
                "manifest_restore_validation_status": verification.get("restore_validation_status") or BACKUP_RESTORE_NOT_PERFORMED,
            }
        )
    else:
        missing = verification.get("summary") == "Backup artifact is missing."
        base.update(
            {
                "status": "backup_missing" if missing else "backup_invalid",
                "status_semantics": "backup_missing" if missing else "invalid",
                "checksum_status": "unknown",
                "error_summary": _sanitize(verification.get("summary")),
            }
        )
    if restore_validation_manifest_path:
        base["restore_validation_status_source"] = "provided_manifest_path_for_test_only"
        restored = backup_restore_validated(restore_validation_manifest_path, backup_manifest_path)
        base["backup_restore_validated"] = bool(restored.get("valid"))
        base["restore_validation_status"] = restored.get("status", "invalid")
        base["restore_validation_semantics"] = "restore_validated" if restored.get("valid") else "invalid"
        base["restore_validation_summary"] = _safe_jsonable({**restored, "source_limitation": "test/disposable restore-validation manifest reference only"})
        if restored.get("valid"):
            base["status"] = "restore_validated"
    elif backup_manifest_path:
        base["restore_validation_status"] = "restore_status_source_unavailable"
        base["restore_validation_status_source"] = "source_unavailable"
        base["restore_validation_semantics"] = "source_unavailable"
    return base


def _production_status(migration_plan: dict[str, Any]) -> dict[str, Any]:
    adoption = migration_plan.get("production_adoption_status") or PRODUCTION_ADOPTION_DEFERRED
    return {
        "production_adoption_status": adoption,
        "production_adoption_status_source": "migration_plan_read_only",
        "production_adoption_performed": False,
        "production_adoption_performed_source": "report_generation_read_only",
        "production_adoption_deferred": adoption == PRODUCTION_ADOPTION_DEFERRED,
        "production_adoption_deferred_source": "migration_plan_read_only",
        "production_adoption_blocked": migration_plan.get("status") == "blocked",
        "production_migration_executed": False,
        "production_migration_executed_source": "report_generation_read_only",
        "production_read_only_inspected_only": False,
        "production_read_only_inspected_only_source": "unknown",
        "production_read_only_inspected_only_limitation": "Report generation did not perform or consume a separate live production read-only inspection artifact.",
        "production_not_touched": True,
        "production_not_touched_source": "report_generation_read_only",
        "startup_execution_policy": "preflight_block_only",
        "schema_version_managed_claim": "not_claimed_for_production_adoption_deferred",
    }


def _diagnostic_archive_summary() -> dict[str, Any]:
    return {
        "upgrade_report_json_entry": "upgrade/report.json",
        "upgrade_report_text_entry": "upgrade/summary.txt",
        "included_in_existing_diagnostic_archive": True,
        "real_backup_dumps_included": False,
        "restore_artifacts_included": False,
        "video_archive_files_included": False,
        "env_files_included": False,
        "redaction_status": "scoped",
        "redaction_scope": "upgrade_report_fields_and_diagnostic_archive_upgrade_summary",
    }


def build_upgrade_report(
    db: Session,
    *,
    backup_manifest_path: str | Path | None = None,
    restore_validation_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    generated_at = _utc_iso()
    warnings: list[dict[str, Any]] = []
    schema_status = schema_version_status(db)
    migration_plan = build_migration_plan(db)
    history = _history_summary(db)
    versions = _version_summary(db, schema_status, history, migration_plan)
    pending = _pending_summary(migration_plan)
    backup = _backup_summary(backup_manifest_path, restore_validation_manifest_path)
    production = _production_status(migration_plan)

    warnings.append(
        _warning(
            "app_build_version_temporary",
            "info",
            "APP_BUILD_VERSION uses the temporary Stage 2 metadata source until Stage 7.",
            stage_target="Stage 7.0",
            evidence={"app_build_version_source": versions["app_build_version_source"]},
        )
    )
    if production["production_adoption_deferred"]:
        warnings.append(
            _warning(
                "production_adoption_deferred",
                "medium",
                "Production adoption/migration remains deferred and report generation is read-only.",
                stage_target="Stage 6.0",
                evidence={"production_not_touched": True},
            )
        )
    if backup["backup_status_source"] == "source_unavailable":
        warnings.append(
            _warning(
                "backup_status_source_unavailable",
                "medium",
                "No safe product-owned backup status source is connected to this read-only report.",
                stage_target="Stage 6.0.1",
                evidence={"backup_status_source": backup["backup_status_source"], "backup_status": backup["status"]},
            )
        )
    elif not backup["backup_available"]:
        warnings.append(
            _warning(
                "backup_not_available_for_report",
                "medium",
                "Connected backup evidence does not prove a verified backup is available.",
                stage_target="Stage 4.0",
                evidence={"backup_status_source": backup["backup_status_source"], "backup_status": backup["status"]},
            )
        )
    if backup["restore_validation_status_source"] == "source_unavailable":
        warnings.append(
            _warning(
                "restore_validation_status_source_unavailable",
                "medium",
                "No safe restore-validation status source is connected to this read-only report.",
                stage_target="Stage 6.0.1",
                evidence={
                    "restore_validation_status_source": backup["restore_validation_status_source"],
                    "restore_validation_status": backup["restore_validation_status"],
                },
            )
        )
    elif not backup["backup_restore_validated"]:
        warnings.append(
            _warning(
                "restore_validation_missing_or_not_linked",
                "medium",
                "No linked restore-validation manifest proves this backup in the report context.",
                stage_target="Stage 5.0",
                evidence={
                    "restore_validation_status_source": backup["restore_validation_status_source"],
                    "restore_validation_status": backup["restore_validation_status"],
                },
            )
        )
    if backup["root_evidence"]["persistence_evidence_status"] == "unknown_safe_check_unavailable":
        warnings.append(
            _warning(
                "backup_root_persistence_unknown",
                "low",
                "Report records the configured backup-root contract but did not prove host mount persistence.",
                stage_target="Stage 6.0",
                evidence={
                    "configured_contract_status": backup["root_evidence"]["configured_contract_status"],
                    "persistence_evidence_status": backup["root_evidence"]["persistence_evidence_status"],
                    "write_probe_performed": False,
                },
            )
        )
    warnings.append(
        _warning(
            "video_archive_restore_not_covered",
            "info",
            "DB backup/restore validation does not restore video archive files.",
            stage_target="future_restore_operator_flow",
            evidence={"video_archive_files_included": False},
        )
    )
    if pending["by_risk"].get("risky_requires_backup"):
        warnings.append(
            _warning(
                "pending_risky_migrations_require_backup",
                "high",
                "Pending risky migrations require verified backup policy before execution.",
                stage_target="future_migration_policy",
                evidence={"count": pending["by_risk"]["risky_requires_backup"]},
            )
        )
    if pending["by_risk"].get("manual_only"):
        warnings.append(
            _warning(
                "manual_only_migrations_require_operator_action",
                "high",
                "Manual-only migrations require explicit operator authorization.",
                stage_target="future_migration_policy",
                evidence={"count": pending["by_risk"]["manual_only"]},
            )
        )

    report = {
        "report_version": REPORT_VERSION,
        "report_id": f"kmvms-upgrade-report-{uuid.uuid4().hex[:12]}",
        "generated_at": generated_at,
        "status": REPORT_STATUS_COMPLETE,
        "data_freshness": {"status": "current_read_only_snapshot", "generated_at": generated_at},
        "data_sources": {
            "schema_version_state": "read_only",
            "schema_migration_history": "read_only",
            "migration_runner_plan": "read_only",
            "backup_manifest": "optional_read_only" if backup_manifest_path else "not_provided",
            "restore_validation_manifest": "optional_read_only" if restore_validation_manifest_path else "not_provided",
            "backup_root_config": "process_config_read_only_no_probe",
            "backup_status": backup["backup_status_source"],
            "restore_validation_status": backup["restore_validation_status_source"],
        },
        "limitations": [
            "APP_BUILD_VERSION source replacement is deferred to Chapter 07 Stage 7.0.",
            "Report generation does not execute backup, restore, migration or adoption.",
            "Report generation does not run backup-root marker/write probes by default.",
            "DB restore validation does not cover video archive file recovery.",
        ],
        "versions": versions,
        "schema_version_state": _safe_jsonable(schema_status),
        "schema_migration_history": history,
        "migration_runner": {
            "status": migration_plan.get("status"),
            "blocked_reason": migration_plan.get("blocked_reason"),
            "current_schema_version": migration_plan.get("current_schema_version"),
            "target_schema_version": migration_plan.get("target_schema_version"),
            "schema_status": migration_plan.get("schema_status"),
            "pending_migrations": pending,
            "mutates_database": False,
            "startup_execution_policy": "preflight_block_only",
        },
        "production": production,
        "backup": backup,
        "restore_validation": {
            "status": backup["restore_validation_status"],
            "status_source": backup["restore_validation_status_source"],
            "status_semantics": backup["restore_validation_semantics"],
            "backup_restore_validated": backup["backup_restore_validated"],
            "semantics": "DB backup recoverability validated only in disposable environment when restore_validated.",
            "production_rollback_automated": False,
            "video_archive_restore_covered": False,
        },
        "warnings": warnings,
        "errors": [],
        "redaction": {
            "redaction_status": "scoped_check_passed",
            "redaction_scope": "upgrade_report_fields_only",
            "checked_outputs": ["upgrade_report_json_fields"],
            "limitations": [
                "Runtime report does not claim a blanket pass for service artifacts or archives generated outside this call.",
                "Diagnostic archive tests scan upgrade/report.json and upgrade/summary.txt separately.",
            ],
            "raw_db_urls_included": False,
            "password_hashes_included": False,
            "rtsp_credentials_included": False,
            "env_values_included": False,
            "raw_backup_paths_included": False,
            "real_backup_dump_files_included": False,
        },
        "diagnostic_archive": _diagnostic_archive_summary(),
        "side_effects": {
            "db_mutated": False,
            "filesystem_write_probe_performed": False,
            "backup_created": False,
            "restore_executed": False,
            "migration_executed": False,
            "production_adoption_written": False,
        },
    }
    assert_upgrade_report_secret_safe(report)
    return report


def upgrade_report_text_summary(report: dict[str, Any]) -> str:
    versions = report.get("versions", {})
    backup = report.get("backup", {})
    restore = report.get("restore_validation", {})
    migration = report.get("migration_runner", {})
    warnings = report.get("warnings", [])
    return "\n".join(
        [
            "KM VMS upgrade report",
            f"generated_at: {report.get('generated_at')}",
            f"app_version: {versions.get('app_version')}",
            f"app_build_version_source: {versions.get('app_build_version_source')}",
            f"current_schema_version: {versions.get('current_schema_version')}",
            f"target_schema_version: {versions.get('target_schema_version')}",
            f"migration_runner_status: {migration.get('status')}",
            f"backup_status: {backup.get('status')}",
            f"backup_status_source: {backup.get('backup_status_source')}",
            f"restore_validation_status: {restore.get('status')}",
            f"restore_validation_status_source: {restore.get('status_source')}",
            f"backup_restore_validated: {restore.get('backup_restore_validated')}",
            f"warnings_count: {len(warnings)}",
            "production_restore_executed: false",
            "production_migration_executed: false",
            "video_archive_restore_covered: false",
        ]
    )
