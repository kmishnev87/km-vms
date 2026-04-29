from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER, ROLE_VIEWER
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.routers.deps import require_permission

router = APIRouter(prefix="/users", tags=["users"])

ROLES = {ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER}
ADMIN_ASSIGNABLE = {ROLE_OPERATOR, ROLE_VIEWER}
OWNER_ASSIGNABLE = {ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER}


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=2, max_length=100)
    full_name: str = Field(default="", max_length=255)
    password: str = Field(min_length=8, max_length=256)
    role: str
    is_active: bool = True


class UserUpdateRequest(BaseModel):
    username: str | None = Field(default=None, min_length=2, max_length=100)
    full_name: str | None = Field(default=None, max_length=255)
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)
    current_password: str | None = None


def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


def normalize_username(value: str) -> str:
    username = value.strip()
    if len(username) < 2:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Username is too short")
    return username


def ensure_username_available(db: Session, username: str, user_id: int | None = None) -> None:
    existing = db.query(User).filter(User.username == username).first()
    if existing and existing.id != user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")


def ensure_actor_can_manage(actor: User, target: User) -> None:
    if actor.role == ROLE_OWNER:
        return
    if actor.role == ROLE_ADMIN and target.role != ROLE_OWNER:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")


def ensure_role_assignable(actor: User, role: str) -> None:
    if role not in ROLES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown role")
    if actor.role == ROLE_OWNER and role in OWNER_ASSIGNABLE:
        return
    if actor.role == ROLE_ADMIN and role in ADMIN_ASSIGNABLE:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Role is not allowed")


@router.get("")
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    if current_user.role not in {ROLE_OWNER, ROLE_ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    return [serialize_user(user) for user in db.query(User).order_by(User.id.asc()).all()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    if current_user.role not in {ROLE_OWNER, ROLE_ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    ensure_role_assignable(current_user, payload.role)
    username = normalize_username(payload.username)
    ensure_username_available(db, username)

    user = User(
        username=username,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=payload.is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return serialize_user(user)


@router.patch("/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_settings")),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    ensure_actor_can_manage(current_user, target)
    is_self = target.id == current_user.id

    if target.role == ROLE_OWNER and not is_self:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner cannot be modified")
    if is_self and payload.role is not None and payload.role != target.role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot change your own role")
    if is_self and payload.is_active is False:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot disable yourself")
    if target.role == ROLE_OWNER and payload.role is not None and payload.role != ROLE_OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner cannot be demoted")
    if target.role == ROLE_OWNER and payload.is_active is False:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner cannot be disabled")
    if payload.role is not None and payload.role != target.role:
        ensure_role_assignable(current_user, payload.role)

    own_credentials_changed = False
    if payload.username is not None:
        username = normalize_username(payload.username)
        ensure_username_available(db, username, user_id=target.id)
        own_credentials_changed = own_credentials_changed or (is_self and username != target.username)
        target.username = username
    if payload.full_name is not None:
        target.full_name = payload.full_name.strip()
    if payload.role is not None:
        target.role = payload.role
    if payload.is_active is not None:
        target.is_active = payload.is_active
    if payload.password:
        if is_self:
            if not payload.current_password:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Current password is required")
            if not verify_password(payload.current_password, target.password_hash):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Current password is incorrect")
            own_credentials_changed = True
        target.password_hash = hash_password(payload.password)

    target.updated_at = datetime.utcnow()
    db.add(target)
    db.commit()
    db.refresh(target)
    return {
        "user": serialize_user(target),
        "own_credentials_changed": own_credentials_changed,
    }
