from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

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
    try:
        if "storage_path" in data:
            storage_check = validate_storage_path(data["storage_path"], create=True)
            if not storage_check["ok"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"storage": storage_check},
                )
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
