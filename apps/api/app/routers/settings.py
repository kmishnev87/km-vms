from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import ROLE_OWNER
from app.core.security import hash_password
from app.db.session import get_db
from app.models.user import User
from app.routers.deps import require_permission
from app.services.system_settings import (
    get_system_settings,
    serialize_settings,
    update_system_settings,
    validate_settings_payload,
    validate_storage_path,
)

router = APIRouter(tags=["settings"])


class SetupRequest(BaseModel):
    username: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=8, max_length=256)
    timezone: str
    language: str
    storage_path: str
    recording_format: str
    hardware_preferred_backend: str | None = None


class SettingsUpdateRequest(BaseModel):
    timezone: str | None = None
    language: str | None = None
    storage_path: str | None = None
    recording_format: str | None = None
    hardware_preferred_backend: str | None = None


class BugReportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    include_logs: bool = True


class StorageValidateRequest(BaseModel):
    storage_path: str
    create: bool = True


@router.get("/system/status")
def system_status(db: Session = Depends(get_db)):
    system = get_system_settings(db)
    return {
        "initialized": system.system_initialized,
        "setup_required": not system.system_initialized,
        "language": system.language,
        "timezone": system.timezone,
    }


@router.post("/setup", status_code=status.HTTP_201_CREATED)
def setup(payload: SetupRequest, db: Session = Depends(get_db)):
    system = get_system_settings(db)
    if system.system_initialized:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="System is already initialized")

    username = payload.username.strip()
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

    try:
        settings_data = validate_settings_payload(payload.model_dump(), partial=False)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    storage_check = validate_storage_path(settings_data["storage_path"], create=True)
    if not storage_check["ok"]:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"storage": storage_check})

    admin = User(
        username=username,
        full_name="Administrator",
        password_hash=hash_password(payload.password),
        role=ROLE_OWNER,
        is_active=True,
    )
    db.add(admin)

    for key, value in settings_data.items():
        setattr(system, key, value)
    system.system_initialized = True
    system.updated_at = datetime.utcnow()
    db.add(system)
    db.commit()
    db.refresh(system)

    return {
        "ok": True,
        "settings": serialize_settings(system),
        "storage_validation": storage_check,
    }


@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    system = get_system_settings(db)
    return serialize_settings(system)


@router.patch("/settings")
def patch_settings(
    payload: SettingsUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    data = payload.model_dump(exclude_unset=True)
    data.pop("storage_path", None)
    try:
        system = update_system_settings(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return serialize_settings(system)


@router.post("/settings/storage/validate")
def validate_storage(
    payload: StorageValidateRequest,
    current_user: User = Depends(require_permission("manage_settings")),
):
    return validate_storage_path(payload.storage_path, create=payload.create)


def iter_log_files() -> list[Path]:
    roots = [
        Path(settings.storage_previews),
        Path(settings.storage_exports),
        Path(settings.storage_root),
        Path("/tmp"),
    ]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.log"):
            if path.is_file():
                files.append(path)
    return sorted(files, key=lambda item: str(item))[:200]


def build_log_archive(report_text: str | None = None, include_logs: bool = True) -> io.BytesIO:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("system/info.txt", f"created_at={datetime.utcnow().isoformat()}Z\napp_env={settings.app_env}\n")
        if report_text is not None:
            bundle.writestr("bug-report.txt", report_text.strip() + "\n")
        if include_logs:
            for path in iter_log_files():
                try:
                    bundle.write(path, arcname=f"logs/{path.name}")
                except OSError:
                    continue
    archive.seek(0)
    return archive


@router.get("/settings/logs/archive")
def download_log_archive(
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    archive = build_log_archive()
    filename = f"km-vms-logs-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/settings/bug-report")
def create_bug_report(
    payload: BugReportRequest,
    current_user: User = Depends(require_permission("run_diagnostics")),
):
    archive = build_log_archive(report_text=payload.text, include_logs=payload.include_logs)
    filename = f"km-vms-bug-report-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
