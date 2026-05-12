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
    dry_run_restore_maintenance,
    inspect_restore_maintenance,
)
from app.services.update_maintenance import inspect_update_maintenance
from app.services.upgrade_report import build_upgrade_report


router = APIRouter(prefix="/system/db-adoption", tags=["maintenance"])
migrations_router = APIRouter(prefix="/system/migrations", tags=["maintenance"])
restore_router = APIRouter(prefix="/system/restore", tags=["maintenance"])
overview_router = APIRouter(prefix="/system/maintenance", tags=["maintenance"])


def _safe_flow_summary(name: str, payload: dict) -> dict:
    return {
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
        },
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
    update = inspect_update_maintenance(db, actor=current_user)
    report = build_upgrade_report(db)
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
            "update": _safe_flow_summary("update", update),
        },
        "upgrade_report": {
            "available": True,
            "report_id": report.get("report_id"),
            "generated_at": report.get("generated_at"),
            "status": report.get("status"),
            "warnings_count": len(report.get("warnings") or []),
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
