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


router = APIRouter(prefix="/system/db-adoption", tags=["maintenance"])
migrations_router = APIRouter(prefix="/system/migrations", tags=["maintenance"])


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
