from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_OWNER, ROLE_VIEWER, get_permissions_for_role
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.routers.deps import FORBIDDEN_DETAIL, get_current_user, require_permission
from app.schemas.user import ROLES, UserCreateRequest, UserResponse, UserUpdateRequest
from app.services.audit_log import create_event

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


def active_critical_user_count(db: Session, exclude_user_id: int | None = None) -> int:
    query = db.query(User).filter(
        User.role.in_([ROLE_OWNER, ROLE_ADMIN]),
        User.is_active == True,  # noqa: E712
    )
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
    if current_user.role == ROLE_OWNER and role in {ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER}:
        return
    if current_user.role == ROLE_ADMIN and role in {ROLE_OPERATOR, ROLE_VIEWER}:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)


def ensure_can_modify_user(
    current_user: User,
    target: User,
    next_role: str | None = None,
    next_active: bool | None = None,
) -> None:
    if current_user.id == target.id:
        if next_role is not None and next_role != target.role:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя изменить собственную роль")
        if next_active is False and bool(target.is_active):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя отключить собственную учетную запись")

    if target.role == ROLE_OWNER:
        if current_user.id != target.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)
        if next_role is not None and next_role != ROLE_OWNER:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Владельца нельзя понизить")
        if next_active is False and bool(target.is_active):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Владельца нельзя отключить")

    if current_user.role == ROLE_OWNER:
        return

    if current_user.role == ROLE_ADMIN:
        if target.role in {ROLE_OWNER, ROLE_ADMIN} and target.id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)
        if next_role in {ROLE_OWNER, ROLE_ADMIN} and target.id != current_user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)
        return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)


@router.get("/me")
def users_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "display_name": current_user.full_name or "",
        "role": current_user.role,
        "is_active": bool(current_user.is_active),
        "permissions": sorted(get_permissions_for_role(current_user.role)),
        "last_login_at": getattr(current_user, "last_login_at", None),
    }


@router.get("", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    return [serialize_user(user) for user in db.query(User).order_by(User.username.asc()).all()]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_users")),
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
    create_event(
        db=db,
        actor=current_user,
        category="users",
        event_type="users.created",
        message_ru=f"{current_user.username} создал пользователя {user.username}",
        message_en=f"{current_user.username} created user {user.username}",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        metadata={"role": user.role, "is_active": bool(user.is_active)},
    )
    return serialize_user(user)


@router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    next_role = validate_role(payload.role) if payload.role is not None else None
    if current_user.id == user.id and (
        (next_role is not None and next_role != user.role)
        or (payload.is_active is False and bool(user.is_active))
    ):
        create_event(
            db=db,
            actor=current_user,
            category="security",
            event_type="security.forbidden_self_action",
            severity="security",
            message_ru=f"{current_user.username} попытался выполнить запрещённое действие над своей учётной записью",
            message_en=f"{current_user.username} attempted forbidden self-action",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
        )
    if user.role == ROLE_OWNER and current_user.id != user.id:
        create_event(
            db=db,
            actor=current_user,
            category="security",
            event_type="security.forbidden_owner_modify",
            severity="security",
            message_ru=f"{current_user.username} попытался изменить владельца {user.username}, действие запрещено",
            message_en=f"{current_user.username} attempted forbidden owner modification for {user.username}",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
        )
    ensure_can_modify_user(current_user, user, next_role=next_role, next_active=payload.is_active)
    ensure_not_last_active_owner(db, user, next_role=next_role, next_active=payload.is_active)

    old_username = user.username
    old_full_name = user.full_name
    old_role = user.role
    old_active = bool(user.is_active)
    password_changed = bool(payload.password)

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
    changes = {}
    if old_username != user.username:
        changes["username"] = {"old": old_username, "new": user.username}
    if old_full_name != user.full_name:
        changes["display_name"] = {"old": old_full_name, "new": user.full_name}
    if old_role != user.role:
        changes["role"] = {"old": old_role, "new": user.role}
        create_event(
            db=db,
            actor=current_user,
            category="users",
            event_type="users.role_changed",
            message_ru=f"{current_user.username} изменил роль {user.username}: {old_role} → {user.role}",
            message_en=f"{current_user.username} changed role for {user.username}: {old_role} -> {user.role}",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
            metadata={"old_role": old_role, "new_role": user.role},
        )
    if old_active != bool(user.is_active):
        enabled = bool(user.is_active)
        create_event(
            db=db,
            actor=current_user,
            category="users",
            event_type="users.enabled" if enabled else "users.disabled",
            message_ru=f"{current_user.username} {'включил' if enabled else 'отключил'} пользователя {user.username}",
            message_en=f"{current_user.username} {'enabled' if enabled else 'disabled'} user {user.username}",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
            metadata={"old_active": old_active, "new_active": enabled},
        )
    if password_changed:
        self_change = current_user.id == user.id
        create_event(
            db=db,
            actor=current_user,
            category="users",
            event_type="users.password_changed_self" if self_change else "users.password_reset_by_admin",
            message_ru=f"{current_user.username} изменил свой пароль" if self_change else f"{current_user.username} сбросил пароль пользователя {user.username}",
            message_en=f"{current_user.username} changed own password" if self_change else f"{current_user.username} reset password for {user.username}",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
        )
    if changes:
        create_event(
            db=db,
            actor=current_user,
            category="users",
            event_type="users.updated",
            message_ru=f"{current_user.username} изменил пользователя {user.username}",
            message_en=f"{current_user.username} updated user {user.username}",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
            metadata={"changed": changes},
        )
    return serialize_user(user)


@router.delete("/{user_id}", response_model=UserResponse)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("manage_users")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    if user.role == ROLE_OWNER:
        create_event(
            db=db,
            actor=current_user,
            category="security",
            event_type="security.forbidden_owner_modify",
            severity="security",
            message_ru=f"{current_user.username} attempted forbidden owner modification for {user.username}",
            message_en=f"{current_user.username} attempted forbidden owner modification for {user.username}",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Владельца нельзя удалить")
    if user.id == current_user.id:
        create_event(
            db=db,
            actor=current_user,
            category="security",
            event_type="security.forbidden_self_action",
            severity="security",
            message_ru=f"{current_user.username} attempted forbidden self-action",
            message_en=f"{current_user.username} attempted forbidden self-action",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя удалить собственную учетную запись")
    can_delete = current_user.role == ROLE_OWNER or (
        current_user.role == ROLE_ADMIN and user.role not in {ROLE_OWNER, ROLE_ADMIN}
    )
    if not can_delete:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)
    if bool(user.is_active) and user.role in {ROLE_OWNER, ROLE_ADMIN} and active_critical_user_count(db, exclude_user_id=user.id) <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя удалить последнего критического пользователя")

    response = serialize_user(user)
    target_id = user.id
    target_name = user.username
    target_role = user.role
    db.delete(user)
    db.commit()
    create_event(
        db=db,
        actor=current_user,
        category="users",
        event_type="users.deleted",
        message_ru=f"{current_user.username} удалил пользователя {target_name}",
        message_en=f"{current_user.username} deleted user {target_name}",
        target_type="user",
        target_id=target_id,
        target_name=target_name,
        metadata={"role": target_role},
    )
    return response
