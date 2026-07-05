from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.routers.deps import require_permission
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.db_adoption import DbAdoptionBlocked, apply_db_adoption, dry_run_db_adoption, inspect_db_adoption
from app.services.migration_maintenance import (
    MigrationMaintenanceBlocked,
    apply_migration_maintenance,
    dry_run_migration_maintenance,
    inspect_migration_maintenance,
)
from app.services.restore_maintenance import (
    RestoreMaintenanceBlocked,
    apply_restore_maintenance,
    delete_backup_artifact,
    dry_run_restore_maintenance,
    inspect_restore_maintenance,
)
from app.services.upgrade_report import build_upgrade_report


router = APIRouter(prefix="/system/db-adoption", tags=["maintenance"])
migrations_router = APIRouter(prefix="/system/migrations", tags=["maintenance"])
restore_router = APIRouter(prefix="/system/restore", tags=["maintenance"])
overview_router = APIRouter(prefix="/system/maintenance", tags=["maintenance"])


def _safe_restore_artifacts(payload: dict) -> list[dict]:
    items = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    safe_items = []
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        safe_items.append(
            {
                "artifact_id": item.get("artifact_id"),
                "artifact_created_at": item.get("artifact_created_at"),
                "artifact_schema_version": item.get("artifact_schema_version"),
                "db_backend": item.get("db_backend"),
                "file_size": item.get("file_size"),
                "validation_status": item.get("validation_status"),
                "valid": bool(item.get("valid")),
                "deletable": bool(item.get("deletable")),
                "delete_supported": bool(item.get("delete_supported")),
            }
        )
    return safe_items


def _warning_classification(item: dict) -> str:
    code = str(item.get("code") or "").strip()
    severity = str(item.get("severity") or "").strip().lower()
    if code in {
        "video_archive_restore_not_covered",
        "backup_root_persistence_unknown",
        "backup_status_source_unavailable",
        "restore_validation_status_source_unavailable",
        "restore_validation_missing_or_not_linked",
    }:
        return "informational"
    if severity in {"high", "critical", "error"}:
        return "actionable"
    if severity in {"medium", "warning"}:
        return "support"
    return "informational"


def _safe_warning_presentations(warnings: list[dict]) -> dict:
    items = []
    groups = {"actionable": 0, "informational": 0, "support": 0}
    for warning in warnings[:20]:
        if not isinstance(warning, dict):
            continue
        classification = _warning_classification(warning)
        groups[classification] = groups.get(classification, 0) + 1
        items.append(
            {
                "code": warning.get("code"),
                "severity": warning.get("severity"),
                "classification": classification,
                "stage_target": warning.get("stage_target"),
            }
        )
    return {"items": items, "groups": groups, "total": len(warnings)}


def _safe_flow_summary(name: str, payload: dict) -> dict:
    summary = {
        "name": name,
        "status": payload.get("status"),
        "reason": payload.get("reason") or payload.get("blocked_reason") or payload.get("apply_blocked_reason"),
        "can_apply": bool(payload.get("can_apply") or payload.get("can_adopt") or payload.get("can_restore")),
        "apply_supported": bool(
            payload.get("apply_supported", payload.get("can_apply") or payload.get("can_adopt") or payload.get("can_restore"))
        ),
        "backup_required": bool(payload.get("backup_required") or payload.get("requires_current_backup")),
        "requires_confirmation": bool(payload.get("requires_confirmation")),
        "report_id": payload.get("report_id"),
        "read_only": bool(payload.get("read_only", True)),
        "side_effects": payload.get("side_effects")
        or {
            "db_mutated": False,
            "backup_created": False,
            "restore_executed": False,
            "migration_executed": False,
            "update_applied": False,
        },
        "details": {
            "metadata_present": payload.get("metadata_present"),
            "already_adopted": payload.get("already_adopted"),
            "current_version": payload.get("current_version"),
            "target_version": payload.get("target_version"),
            "pending_count": payload.get("pending_count"),
            "artifact_count": payload.get("artifact_count"),
            "valid_artifact_count": payload.get("valid_artifact_count"),
            "current_product_restore_supported": payload.get("current_product_restore_supported"),
            "temporary_validation_restore_supported": payload.get("temporary_validation_restore_supported"),
            "current_version_label": payload.get("current_version"),
            "available_version": payload.get("available_version"),
            "release_validated": payload.get("release_validated"),
            "apply_status": payload.get("apply_status"),
            "artifacts": _safe_restore_artifacts(payload) if name == "restore" else None,
            "current_product_restore_reason": payload.get("current_product_restore_reason") if name == "restore" else None,
        },
    }
    presentation = _flow_presentation(name, summary)
    summary.update(presentation)
    summary["presentation"] = presentation
    return summary


def _flow_presentation(name: str, flow: dict) -> dict:
    status = str(flow.get("status") or "unknown")
    details = flow.get("details") if isinstance(flow.get("details"), dict) else {}
    can_apply = bool(flow.get("can_apply"))
    requires_confirmation = bool(flow.get("requires_confirmation"))

    if name == "db_adoption":
        title_key = "db_identity"
        facts = [
            {"key": "metadata_present", "value": bool(details.get("metadata_present"))},
            {"key": "already_adopted", "value": bool(details.get("already_adopted"))},
        ]
        if status in {"already_adopted", "adopted", "drift_known_safe"}:
            user_status = "ok"
            summary_key = "db_identity_ok"
            action_key = "db_identity_check_optional"
        elif status == "adoptable" or can_apply:
            user_status = "action_available"
            summary_key = "db_identity_adoptable"
            action_key = "db_identity_apply_requires_confirmation"
        else:
            user_status = "blocked"
            summary_key = "db_identity_blocked"
            action_key = "download_support_report"
    elif name == "migration":
        title_key = "db_schema"
        facts = [
            {"key": "current_version", "value": details.get("current_version")},
            {"key": "target_version", "value": details.get("target_version")},
            {"key": "pending_count", "value": int(details.get("pending_count") or 0)},
        ]
        if status == "current":
            user_status = "ok"
            summary_key = "db_schema_current"
            action_key = "migration_check_optional"
        elif status == "pending" or can_apply:
            user_status = "action_available"
            summary_key = "db_schema_pending"
            action_key = "migration_apply_requires_confirmation"
        else:
            user_status = "blocked"
            summary_key = "db_schema_blocked"
            action_key = "download_support_report"
    elif name == "restore":
        title_key = "backup_restore_check"
        valid_artifacts = int(details.get("valid_artifact_count") or 0)
        artifact_count = int(details.get("artifact_count") or 0)
        temporary_supported = bool(details.get("temporary_validation_restore_supported"))
        current_supported = bool(details.get("current_product_restore_supported"))
        facts = [
            {"key": "valid_artifacts", "value": f"{valid_artifacts}/{artifact_count}"},
            {"key": "temporary_validation", "value": temporary_supported},
            {"key": "current_product_restore", "value": current_supported},
        ]
        if status == "no_artifacts":
            user_status = "unavailable"
            summary_key = "backup_restore_no_artifacts"
            action_key = "backup_restore_create_backup_first"
        elif status == "available" and valid_artifacts > 0:
            user_status = "attention"
            summary_key = "backup_restore_artifacts_available"
            action_key = "backup_restore_check_available"
        elif can_apply:
            user_status = "action_available"
            summary_key = "backup_restore_validation_available"
            action_key = "backup_restore_check_available"
        else:
            user_status = "blocked"
            summary_key = "backup_restore_blocked"
            action_key = "download_support_report"
    else:
        title_key = name
        facts = []
        if status in {"ok", "current", "complete", "completed"}:
            user_status = "ok"
        elif can_apply:
            user_status = "action_available"
        elif status in {"blocked", "failed"}:
            user_status = "blocked"
        else:
            user_status = "attention"
        summary_key = f"{name}_{user_status}"
        action_key = "check_status"

    return {
        "key": name,
        "user_status": user_status,
        "title_key": title_key,
        "summary_key": summary_key,
        "operator_action_key": action_key,
        "can_check": name in {"db_adoption", "migration", "restore"},
        "can_apply": can_apply,
        "apply_supported": bool(flow.get("apply_supported")),
        "requires_confirmation": requires_confirmation,
        "backup_required": bool(flow.get("backup_required")),
        "dangerous_action": bool(can_apply and requires_confirmation),
        "support_report_available": True,
        "facts": facts,
    }


def _last_maintenance_action(report: dict) -> dict:
    history = report.get("schema_migration_history") if isinstance(report.get("schema_migration_history"), dict) else {}
    items = history.get("bounded_items") if isinstance(history.get("bounded_items"), list) else []
    last_item = items[-1] if items else None
    if last_item:
        return {
            "available": True,
            "operation": last_item.get("source") or last_item.get("migration_id"),
            "status": last_item.get("status"),
            "timestamp": last_item.get("applied_at"),
            "reason": last_item.get("error_summary"),
            "source": "schema_migration_history",
        }
    return {
        "available": False,
        "status": "limited",
        "reason": "No durable maintenance action history is available beyond current status and generated upgrade report summary.",
        "source": "current_read_only_snapshot",
    }


@overview_router.get("/overview")
def maintenance_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    adoption = inspect_db_adoption(db, include_backup_plan=False, actor=current_user)
    migrations = inspect_migration_maintenance(db, include_backup_plan=False, actor=current_user)
    restore = inspect_restore_maintenance(actor=current_user)
    report = build_upgrade_report(db)
    warning_presentations = _safe_warning_presentations(report.get("warnings") or [])
    return {
        "status": "ok",
        "read_only": True,
        "side_effects": {
            "db_mutated": False,
            "backup_created": False,
            "restore_executed": False,
            "migration_executed": False,
            "update_applied": False,
        },
        "flows": {
            "db_adoption": _safe_flow_summary("db_adoption", adoption),
            "migration": _safe_flow_summary("migration", migrations),
            "restore": _safe_flow_summary("restore", restore),
        },
        "upgrade_report": {
            "available": True,
            "report_id": report.get("report_id"),
            "generated_at": report.get("generated_at"),
            "status": report.get("status"),
            "warnings_count": len(report.get("warnings") or []),
            "warnings": warning_presentations["items"],
            "warning_groups": warning_presentations["groups"],
            "diagnostic_archive": report.get("diagnostic_archive"),
            "download_endpoint": "/system/upgrade/report",
        },
        "history": {
            "durable_history": "limited",
            "last_action": _last_maintenance_action(report),
            "source": "schema_migration_history_and_current_upgrade_report",
        },
    }


class DbAdoptionApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False


@router.get("/status")
def db_adoption_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return inspect_db_adoption(db, actor=current_user)


@router.post("/dry-run")
def db_adoption_dry_run(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return dry_run_db_adoption(db, actor=current_user)


@router.post("/apply")
def db_adoption_apply(
    payload: DbAdoptionApplyRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    try:
        result = apply_db_adoption(
            db,
            confirm=payload.confirm,
            actor=current_user,
        )
    except DbAdoptionBlocked as exc:
        create_event(
            db=db,
            actor=current_user,
            category="system",
            event_type="system.db_adoption_blocked",
            severity="warning",
            message_ru="DB adoption maintenance action was blocked.",
            message_en="DB adoption maintenance action was blocked.",
            target_type="db_adoption",
            metadata={"status": exc.status, "reason": str(exc)[:300]},
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.diagnostics)

    create_event(
        db=db,
        actor=current_user,
        category="system",
        event_type="system.db_adoption_completed" if result.get("status") == "adopted" else "system.db_adoption_already_adopted",
        severity="info",
        message_ru="DB adoption maintenance action completed.",
        message_en="DB adoption maintenance action completed.",
        target_type="db_adoption",
        metadata={
            "status": result.get("status"),
            "report_id": result.get("report_id"),
            "metadata_present": bool(result.get("metadata_present")),
            "migration_executed": False,
            "business_data_mutated": False,
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return result


class MigrationApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False


@migrations_router.get("/status")
def migration_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return inspect_migration_maintenance(db, actor=current_user)


@migrations_router.post("/dry-run")
def migration_dry_run(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return dry_run_migration_maintenance(db, actor=current_user)


@migrations_router.post("/apply")
def migration_apply(
    payload: MigrationApplyRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    try:
        result = apply_migration_maintenance(db, confirm=payload.confirm, actor=current_user)
    except MigrationMaintenanceBlocked as exc:
        create_event(
            db=db,
            actor=current_user,
            category="system",
            event_type="system.migration_apply_blocked" if exc.status != "migration_failed" else "system.migration_apply_failed",
            severity="warning",
            message_ru="Schema migration maintenance action was blocked.",
            message_en="Schema migration maintenance action was blocked.",
            target_type="schema_migration",
            metadata={"status": exc.status, "reason": str(exc)[:300]},
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.diagnostics)

    create_event(
        db=db,
        actor=current_user,
        category="system",
        event_type="system.migration_apply_completed" if result.get("applied") else "system.migration_apply_current",
        severity="info",
        message_ru="Schema migration maintenance action completed.",
        message_en="Schema migration maintenance action completed.",
        target_type="schema_migration",
        metadata={
            "status": result.get("status"),
            "report_id": result.get("report_id"),
            "current_version": result.get("current_version"),
            "target_version": result.get("target_version"),
            "applied_count": result.get("applied_count", 0),
            "backup_status": result.get("backup_status"),
            "business_data_outside_migration_runner_mutated": False,
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return result


class RestoreDryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str | None = None
    target_kind: str = "temporary_validation_db"


class RestoreApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False
    artifact_id: str
    target_kind: str


class BackupArtifactDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False


@restore_router.get("/status")
def restore_status(
    current_user: User = Depends(require_permission("manage_settings")),
):
    return inspect_restore_maintenance(actor=current_user)


@restore_router.post("/dry-run")
def restore_dry_run(
    payload: RestoreDryRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    return dry_run_restore_maintenance(db, artifact_id=payload.artifact_id, target_kind=payload.target_kind, actor=current_user)


@restore_router.post("/apply")
def restore_apply(
    payload: RestoreApplyRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    try:
        result = apply_restore_maintenance(
            db,
            confirm=payload.confirm,
            artifact_id=payload.artifact_id,
            target_kind=payload.target_kind,
            actor=current_user,
        )
    except RestoreMaintenanceBlocked as exc:
        create_event(
            db=db,
            actor=current_user,
            category="system",
            event_type="system.restore_apply_blocked" if exc.status != "restore_failed" else "system.restore_apply_failed",
            severity="warning",
            message_ru="Restore maintenance action was blocked.",
            message_en="Restore maintenance action was blocked.",
            target_type="restore_rollback",
            metadata={"status": exc.status, "reason": str(exc)[:300]},
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.diagnostics)

    create_event(
        db=db,
        actor=current_user,
        category="system",
        event_type="system.restore_apply_completed",
        severity="info",
        message_ru="Restore maintenance action completed.",
        message_en="Restore maintenance action completed.",
        target_type="restore_rollback",
        metadata={
            "status": result.get("status"),
            "report_id": result.get("report_id"),
            "target_kind": result.get("target_kind"),
            "current_backup_status": result.get("current_backup_status"),
            "video_archive_files_restored": False,
            "migration_auto_apply": False,
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return result


@restore_router.post("/artifacts/{artifact_id}/delete")
def restore_artifact_delete(
    artifact_id: str,
    payload: BackupArtifactDeleteRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    try:
        result = delete_backup_artifact(artifact_id=artifact_id, confirm=payload.confirm, actor=current_user)
    except RestoreMaintenanceBlocked as exc:
        create_event(
            db=db,
            actor=current_user,
            category="system",
            event_type="system.backup_artifact_delete_blocked" if exc.status != "delete_failed" else "system.backup_artifact_delete_failed",
            severity="warning",
            message_ru="Backup artifact delete was blocked.",
            message_en="Backup artifact delete was blocked.",
            target_type="db_backup",
            target_id=artifact_id[:80],
            metadata={"status": exc.status, "reason": str(exc)[:300]},
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.diagnostics)

    create_event(
        db=db,
        actor=current_user,
        category="system",
        event_type="system.backup_artifact_delete_completed",
        severity="warning",
        message_ru="Backup artifact was deleted.",
        message_en="Backup artifact was deleted.",
        target_type="db_backup",
        target_id=result.get("artifact_id"),
        metadata={
            "status": result.get("status"),
            "deleted_count": result.get("deleted_count"),
            "missing_count": result.get("missing_count"),
            "video_archive_files_deleted": False,
        },
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return result
