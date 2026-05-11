from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.recording import ArchiveRoot, RecordingJob, RecordingSegment
from app.models.system_settings import SystemSettings
from app.models.user import User
from app.services.backup_before_upgrade import BackupExecutionConfig, BackupSafetyBlocked, build_backup_plan, create_backup_before_upgrade
from app.services.migration_maintenance import inspect_migration_maintenance
from app.services.restore_maintenance import inspect_restore_maintenance
from app.services.update_check import build_update_status, run_update_check


UPDATE_APPLY_REPORT_VERSION = "stage13.update_apply.v1"
SENSITIVE_RE = re.compile(
    r"(password|passwd|secret|token|authorization|jwt|rtsp://|postgresql://|sqlite:///|registry)[^,\s\"']*",
    re.IGNORECASE,
)
DANGEROUS_REQUEST_FIELDS = {
    "url",
    "release_url",
    "package_url",
    "package_path",
    "manifest_path",
    "registry_token",
    "registry_password",
    "backup_root",
    "backup_path",
    "backup_dir",
    "path",
    "source_path",
    "destination",
    "compose_file",
    "compose_path",
    "command",
    "shell_command",
    "image",
    "image_override",
    "db_url",
    "database_url",
    "connection_string",
}


class UpdateMaintenanceBlocked(RuntimeError):
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


def _candidate(update: dict[str, Any]) -> dict[str, Any] | None:
    release = update.get("latest_release")
    if not release:
        return None
    return {
        "release_id": release.get("release_id"),
        "available_version": release.get("latest_version"),
        "build_id": release.get("build_id"),
        "severity": release.get("severity"),
        "release_date": release.get("release_date"),
        "release_validated": update.get("status") not in {"invalid_manifest", "failed", "not_configured"},
        "source_channel": (update.get("source_channel") or {}).get("source_channel_id"),
        "trusted_source_type": (update.get("source_channel") or {}).get("trusted_source_type"),
    }


def _backup_readiness(db: Session, *, required: bool) -> dict[str, Any]:
    if not required:
        return {
            "required": False,
            "status": "not_required",
            "backup_root_status": "not_checked",
            "backup_root_persistent": None,
            "creates_backup_files": False,
        }
    try:
        plan = build_backup_plan(db, config=BackupExecutionConfig(source="pre_update"))
        return {
            "required": True,
            "status": "ready" if plan.get("free_space", {}).get("passed") else "blocked",
            "backup_root_status": plan.get("backup_root_status"),
            "backup_root_persistent": bool(plan.get("backup_root_persistent")),
            "backup_root_classification": plan.get("backup_root_classification"),
            "free_space_passed": bool(plan.get("free_space", {}).get("passed")),
            "creates_backup_files": False,
        }
    except BackupSafetyBlocked as exc:
        return {
            "required": True,
            "status": "blocked",
            "backup_root_status": exc.status,
            "backup_root_persistent": False,
            "reason": _sanitize(exc.diagnostics.get("summary") or exc.status),
            "creates_backup_files": False,
        }


def _preservation_summary(db: Session, update: dict[str, Any]) -> dict[str, Any]:
    release = update.get("latest_release") or {}
    affected = set(release.get("affected_domains") or [])
    return {
        "camera_definitions": {"status": "preserved_by_contract", "count": db.query(Camera).count()},
        "camera_credentials": {"status": "not_exposed", "raw_values_included": False},
        "system_settings": {"status": "preserved_by_contract", "count": db.query(SystemSettings).count()},
        "database": {"status": "backup_required_before_any_apply"},
        "schema_metadata": {"status": "checked_by_migration_maintenance"},
        "recordings_archive_files": {
            "status": "not_mutated_by_stage4",
            "segments_count": db.query(RecordingSegment).count(),
            "archive_roots_count": db.query(ArchiveRoot).count(),
            "affected_by_release": bool(affected & {"archive", "media", "recordings", "storage"}),
        },
        "storage_roots": {"status": "not_mutated_by_stage4"},
        "recorder_configuration": {"status": "restart_only_if_future_apply_executor_exists", "jobs_count": db.query(RecordingJob).count()},
        "users": {"status": "preserved_by_contract", "count": db.query(User).count()},
    }


def _compose_plan(update: dict[str, Any]) -> dict[str, Any]:
    release = update.get("latest_release") or {}
    affected = set(release.get("affected_domains") or [])
    services = ["api", "web", "nginx"]
    if affected & {"recorder", "recordings", "media", "archive"}:
        services.insert(1, "recorder")
    return {
        "status": "restart_required_manual",
        "apply_supported": False,
        "affected_services": services,
        "restart_order": services,
        "service_scope": "km_vms_product_services_only",
        "compose_changes_supported": False,
        "image_pull_supported": False,
        "raw_commands_included": False,
        "registry_credentials_included": False,
        "expected_downtime": "maintenance_window_required",
        "manual_intervention_required": True,
        "blocked_reason": "update_apply_not_available_for_release",
    }


def _build_report(
    *,
    mode: str,
    status: str,
    reason: str,
    update: dict[str, Any],
    candidate: dict[str, Any] | None,
    backup: dict[str, Any],
    preservation: dict[str, Any],
    compose_plan: dict[str, Any],
    migration: dict[str, Any],
    restore: dict[str, Any],
    apply_result: dict[str, Any] | None = None,
    actor: Any = None,
) -> dict[str, Any]:
    report = {
        "report_version": UPDATE_APPLY_REPORT_VERSION,
        "report_id": f"kmvms-update-apply-{uuid.uuid4().hex[:12]}",
        "mode": mode,
        "generated_at": _utc_iso(),
        "actor": {
            "user_id": getattr(actor, "id", None),
            "username": _sanitize(getattr(actor, "username", None), 100) if getattr(actor, "username", None) else None,
            "role": _sanitize(getattr(actor, "role", None), 50) if getattr(actor, "role", None) else None,
        },
        "status": status,
        "reason": reason,
        "current_version": (update.get("installed_build") or {}).get("app_version"),
        "candidate": candidate,
        "source_channel": _safe_jsonable(update.get("source_channel")),
        "compatibility": _safe_jsonable((update.get("preflight") or {}).get("schema_compatibility")),
        "preflight": {
            "status": (update.get("preflight") or {}).get("status"),
            "blockers": (update.get("preflight") or {}).get("blockers") or [],
            "warnings": (update.get("preflight") or {}).get("warnings") or [],
            "side_effects": (update.get("preflight") or {}).get("side_effects") or {},
        },
        "backup_before_update": backup,
        "preservation": preservation,
        "compose_plan": compose_plan,
        "migration_dependency": {
            "status": migration.get("status"),
            "can_apply": migration.get("can_apply"),
            "auto_apply": False,
            "guidance": "Use explicit Stage 2 migration maintenance flow if schema migration is required.",
        },
        "restore_dependency": {
            "status": restore.get("status"),
            "can_restore": restore.get("can_restore"),
            "auto_restore": False,
            "guidance": "Use explicit Stage 3 restore flow for rollback; Stage 4 does not perform hidden rollback.",
        },
        "apply_result": apply_result,
        "side_effects": {
            "update_applied": False,
            "artifact_downloaded": False,
            "containers_restarted": False,
            "migration_executed": False,
            "backup_created": bool(apply_result and apply_result.get("backup_status") == "verified"),
            "restore_executed": False,
        },
        "redaction": {
            "raw_update_path_included": False,
            "raw_url_included": False,
            "raw_command_included": False,
            "raw_db_url_included": False,
            "registry_credentials_included": False,
            "sensitive_values_included": False,
        },
    }
    assert_update_report_secret_safe(report)
    return report


def assert_update_report_secret_safe(report: dict[str, Any]) -> None:
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
        raise UpdateMaintenanceBlocked("unsafe_update_report", {"status": "blocked", "reason": "Update report contains sensitive-looking data."})


def dry_run_update_maintenance(db: Session, *, actor: Any = None) -> dict[str, Any]:
    update = run_update_check(db, manual=False)
    candidate = _candidate(update)
    migration = inspect_migration_maintenance(db, include_backup_plan=False)
    restore = inspect_restore_maintenance()
    preflight = update.get("preflight") or {}
    backup_required = bool(candidate and (preflight.get("backup_requirement") or {}).get("required", True))
    backup = _backup_readiness(db, required=backup_required)
    preservation = _preservation_summary(db, update)
    compose_plan = _compose_plan(update)
    blockers = list(preflight.get("blockers") or [])
    if not candidate:
        blockers.append("trusted_update_package_not_configured")
    if candidate and update.get("status") in {"blocked", "invalid_manifest", "failed"}:
        blockers.append("release_preflight_blocked")
    if candidate:
        blockers.append("update_apply_not_available_for_release")
    status = "blocked" if blockers else ("current" if update.get("status") in {"current", "not_configured"} else "update_available")
    reason = blockers[0] if blockers else ("no_update_apply_action" if status == "current" else "update_available")
    report = _build_report(
        mode="dry_run",
        status=status,
        reason=reason,
        update=update,
        candidate=candidate,
        backup=backup,
        preservation=preservation,
        compose_plan=compose_plan,
        migration=migration,
        restore=restore,
        actor=actor,
    )
    return {
        "status": status,
        "reason": reason,
        "current_version": report["current_version"],
        "available_version": candidate.get("available_version") if candidate else None,
        "release_id": candidate.get("release_id") if candidate else None,
        "release_validated": bool(candidate and candidate.get("release_validated")),
        "compatibility_status": (report["compatibility"] or {}).get("status"),
        "backup_required": backup_required,
        "backup_root_status": backup.get("backup_root_status"),
        "migration_required": bool((migration.get("pending_count") or 0) > 0),
        "restore_available": bool(restore.get("can_restore")),
        "compose_plan_required": bool(candidate),
        "restart_required": bool(candidate),
        "can_apply": False,
        "requires_confirmation": False,
        "warnings": list(dict.fromkeys((preflight.get("warnings") or []) + blockers)),
        "dry_run": True,
        "mutates_database": False,
        "creates_backup": False,
        "artifact_downloaded": False,
        "containers_restarted": False,
        "report_id": report["report_id"],
        "report": report,
    }


def inspect_update_maintenance(db: Session, *, actor: Any = None) -> dict[str, Any]:
    status = build_update_status(db)
    last = status.get("last_update_check") if isinstance(status.get("last_update_check"), dict) else None
    update = last or status
    candidate = _candidate(update)
    preflight = update.get("preflight") or {}
    apply_reason = "update_apply_not_available_for_release" if candidate else "trusted_update_package_not_configured"
    maintenance_status = "blocked" if candidate or status.get("status") == "not_configured" else status.get("status")
    return {
        "status": maintenance_status,
        "reason": apply_reason,
        "current_version": (status.get("installed_build") or {}).get("app_version"),
        "available_version": candidate.get("available_version") if candidate else None,
        "source_channel": (status.get("source_channel") or {}).get("source_channel_id"),
        "release_id": candidate.get("release_id") if candidate else None,
        "release_validated": bool(candidate and candidate.get("release_validated")),
        "compatibility_status": (preflight.get("schema_compatibility") or {}).get("status"),
        "backup_required": bool((preflight.get("backup_requirement") or {}).get("required", False)),
        "backup_root_status": (preflight.get("backup_requirement") or {}).get("status"),
        "migration_required": bool(((preflight.get("migration_plan") or {}).get("pending_count") or 0) > 0),
        "restore_available": None,
        "compose_plan_required": bool(candidate),
        "restart_required": bool(candidate),
        "can_apply": False,
        "requires_confirmation": False,
        "apply_supported": False,
        "apply_status": "blocked" if candidate else "not_configured",
        "apply_blocked_reason": apply_reason,
        "side_effects": {
            "update_applied": False,
            "artifact_downloaded": False,
            "containers_restarted": False,
            "migration_executed": False,
            "backup_created": False,
            "restore_executed": False,
        },
        "read_only": True,
        "source": "cached_update_status_no_check",
    }


def apply_update_maintenance(
    db: Session,
    *,
    confirm: bool,
    release_id: str | None = None,
    actor: Any = None,
    allow_simulated_apply_for_tests: bool = False,
) -> dict[str, Any]:
    if not confirm:
        raise UpdateMaintenanceBlocked("confirmation_required", {"status": "blocked", "reason": "Explicit confirm=true is required for update apply."})
    dry = dry_run_update_maintenance(db, actor=actor)
    candidate = dry.get("report", {}).get("candidate")
    if not candidate:
        raise UpdateMaintenanceBlocked("trusted_update_package_not_configured", {**dry, "status": "blocked", "reason": "trusted_update_package_not_configured"})
    if release_id and release_id != candidate.get("release_id"):
        raise UpdateMaintenanceBlocked("release_reference_mismatch", {**dry, "status": "blocked", "reason": "release_reference_mismatch"})
    if dry["status"] == "blocked" and not allow_simulated_apply_for_tests:
        raise UpdateMaintenanceBlocked(dry["reason"], dry)

    if not allow_simulated_apply_for_tests:
        raise UpdateMaintenanceBlocked("update_apply_not_available_for_release", {**dry, "status": "blocked", "reason": "update_apply_not_available_for_release"})

    try:
        backup = create_backup_before_upgrade(
            db,
            config=BackupExecutionConfig(source="pre_update"),
            migration_plan_summary={"operation": "product_update_apply", "release_id": candidate.get("release_id")},
        )
    except BackupSafetyBlocked as exc:
        raise UpdateMaintenanceBlocked(
            exc.status,
            {
                **dry,
                "status": "blocked",
                "reason": _sanitize(exc.diagnostics.get("summary") or exc.status),
                "backup_status": exc.status,
                "update_apply_executed": False,
                "containers_restarted": False,
            },
        ) from exc

    apply_result = {
        "status": "simulated",
        "release_id": candidate.get("release_id"),
        "backup_status": backup.get("status"),
        "backup_id": backup.get("backup_id"),
        "update_apply_executed": False,
        "containers_restarted": False,
        "simulation_only": True,
    }
    report = _build_report(
        mode="apply",
        status="blocked",
        reason="simulated_apply_path_for_tests_only",
        update=run_update_check(db, manual=False),
        candidate=candidate,
        backup={"required": True, "status": backup.get("status"), "backup_root_status": "ready", "backup_root_persistent": True},
        preservation=dry["report"]["preservation"],
        compose_plan=dry["report"]["compose_plan"],
        migration=dry["report"]["migration_dependency"],
        restore=dry["report"]["restore_dependency"],
        apply_result=apply_result,
        actor=actor,
    )
    return {
        "status": "blocked",
        "reason": "simulated_apply_path_for_tests_only",
        "release_id": candidate.get("release_id"),
        "backup_status": backup.get("status"),
        "update_apply_executed": False,
        "containers_restarted": False,
        "migration_auto_apply": False,
        "restore_auto_run": False,
        "report_id": report["report_id"],
        "report": report,
    }
