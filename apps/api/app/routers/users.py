from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import ROLE_ADMIN, ROLE_OWNER, ROLE_PERMISSIONS
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.routers.deps import get_current_user, require_permission
from app.schemas.user import ROLES, UserCreateRequest, UserResponse, UserUpdateRequest

router = APIRouter(prefix="/users", tags=["users"])


def serialize_user(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.full_name or "",
        role=user.role,
        is_active=bool(user.is_active),
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login_at=getattr(user, "last_login_at", None),
    )


def validate_role(role: str) -> str:
    value = str(role or "").strip().lower()
    if value not in ROLES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Недопустимая роль")
    return value


def active_owner_count(db: Session, exclude_user_id: int | None = None) -> int:
    query = db.query(User).filter(User.role == ROLE_OWNER, User.is_active == True)  # noqa: E712
    if exclude_user_id is not None:
        query = query.filter(User.id != exclude_user_id)
    return query.count()


def ensure_not_last_active_owner(db: Session, user: User, next_role: str | None = None, next_active: bool | None = None):
    role_after = next_role if next_role is not None else user.role
    active_after = bool(next_active) if next_active is not None else bool(user.is_active)
    if user.role == ROLE_OWNER and bool(user.is_active) and (role_after != ROLE_OWNER or not active_after):
        if active_owner_count(db, exclude_user_id=user.id) <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Нельзя отключить или понизить последнего активного владельца",
            )


def ensure_can_create_role(current_user: User, role: str) -> None:
    if current_user.role == ROLE_OWNER and role in {ROLE_ADMIN, "operator", "viewer"}:
        return
    if current_user.role == ROLE_ADMIN and role in {"operator", "viewer"}:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для назначения этой роли")


def ensure_can_modify_user(current_user: User, target: User, next_role: str | None = None, next_active: bool | None = None) -> None:
    if target.role == ROLE_OWNER:
        if next_role is not None and next_role != ROLE_OWNER:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Владельца нельзя понизить")
        if next_active is False and bool(target.is_active):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Владельца нельзя отключить")
        if current_user.id != target.id and current_user.role != ROLE_OWNER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Администратор не может изменять владельца")
    if current_user.role == ROLE_ADMIN and target.role == ROLE_OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Администратор не может изменять владельца")
    if current_user.role == ROLE_ADMIN and next_role == ROLE_ADMIN and target.id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Администратор не может назначать роль администратора")
    if current_user.role == ROLE_ADMIN and next_role == ROLE_OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Администратор не может назначать роль владельца")
    if current_user.id == target.id:
        if next_role is not None and next_role != target.role:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя изменить собственную роль")
        if next_active is False and bool(target.is_active):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя отключить собственную учётную запись")
    elif current_user.role != ROLE_OWNER and target.role == ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Администратор не может изменять администратора")


@router.get("/me")
def users_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "display_name": current_user.full_name or "",
        "role": current_user.role,
        "is_active": bool(current_user.is_active),
        "permissions": sorted(ROLE_PERMISSIONS.get(current_user.role, set())),
        "last_login_at": getattr(current_user, "last_login_at", None),
    }


@router.get("", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin_access")),
):
    return [serialize_user(user) for user in db.query(User).order_by(User.username.asc()).all()]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin_access")),
):
    username = payload.username.strip()
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Пользователь с таким логином уже существует")
    role = validate_role(payload.role)
    ensure_can_create_role(current_user, role)

    user = User(
        username=username,
        full_name=(payload.display_name or "").strip(),
        password_hash=hash_password(payload.password),
        role=role,
        is_active=bool(payload.is_active),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return serialize_user(user)


@router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin_access")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    next_role = validate_role(payload.role) if payload.role is not None else None
    ensure_can_modify_user(current_user, user, next_role=next_role, next_active=payload.is_active)
    ensure_not_last_active_owner(db, user, next_role=next_role, next_active=payload.is_active)

    if payload.username is not None:
        username = payload.username.strip()
        existing = db.query(User).filter(User.username == username, User.id != user.id).first()
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Пользователь с таким логином уже существует")
        user.username = username
    if payload.display_name is not None:
        user.full_name = payload.display_name.strip()
    if next_role is not None:
        user.role = next_role
    if payload.is_active is not None:
        user.is_active = bool(payload.is_active)
    if payload.password:
        if current_user.id == user.id and not verify_password(payload.current_password or "", user.password_hash):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Укажите верный текущий пароль")
        user.password_hash = hash_password(payload.password)
    user.updated_at = datetime.utcnow()

    db.add(user)
    db.commit()
    db.refresh(user)
    return serialize_user(user)


@router.delete("/{user_id}", response_model=UserResponse)
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin_access")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    ensure_can_modify_user(current_user, user, next_active=False)
    ensure_not_last_active_owner(db, user, next_active=False)
    user.is_active = False
    user.updated_at = datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    return serialize_user(user)
