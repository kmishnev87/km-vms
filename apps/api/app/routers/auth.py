from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.permissions import get_permissions_for_role
from app.core.login_throttle import LoginThrottled, login_throttle
from app.core.request_client import ClientIdentityUnavailable, request_client_ip
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.models.user import User
from app.routers.deps import get_current_user
from app.schemas.auth import LoginRequest, TokenResponse, UserMeResponse
from app.services.audit_log import create_event, request_ip, request_user_agent
from app.services.system_settings import get_system_settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    try:
        source_ip = request_client_ip(request, strict=True)
    except ClientIdentityUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Не удалось подтвердить адрес подключения. Повторите вход через несколько секунд.",
            headers={"Retry-After": "2"},
        ) from None
    try:
        attempt = login_throttle.acquire(source_ip)
    except LoginThrottled as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток входа с этого адреса. Повторите немного позже.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    try:
        return _authenticate_login(payload, request, db, attempt)
    finally:
        login_throttle.finish(attempt)


def _authenticate_login(payload, request, db, attempt):
    user = db.query(User).filter(User.username == payload.username).first()
    if user and not getattr(user, "is_active", True):
        create_event(
            db=db,
            actor=user,
            category="auth",
            event_type="auth.login_blocked_disabled",
            severity="security",
            message_ru=f"Вход пользователя {user.username} запрещён: пользователь отключён",
            message_en=f"Login for {user.username} blocked: user is disabled",
            target_type="user",
            target_id=user.id,
            target_name=user.username,
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
        )

    if not user or not verify_password(payload.password, user.password_hash):
        create_event(
            db=db,
            category="auth",
            event_type="auth.login_failed",
            severity="warning",
            message_ru=f"Неудачная попытка входа для пользователя {payload.username}",
            message_en=f"Failed login attempt for user {payload.username}",
            target_type="user",
            target_name=payload.username,
            ip_address=request_ip(request),
            user_agent=request_user_agent(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
        )

    attempt.failed = False
    expires_at = None
    if payload.stay_signed_in:
        system = get_system_settings(db)
        try:
            tz = ZoneInfo(system.timezone or "UTC")
        except Exception:
            tz = timezone.utc
        now_local = datetime.now(tz)
        expires_at = datetime.combine(now_local.date(), time.max, tzinfo=tz).replace(microsecond=0)
        expires_at = expires_at.astimezone(timezone.utc)

    token = create_access_token(user.username, expires_at=expires_at, user_id=user.id)
    user.last_login_at = datetime.utcnow()
    db.add(user)
    db.commit()
    create_event(
        db=db,
        actor=user,
        category="auth",
        event_type="auth.login_success",
        severity="info",
        message_ru=f"{user.username} вошёл в систему",
        message_en=f"{user.username} signed in",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        ip_address=request_ip(request),
        user_agent=request_user_agent(request),
    )
    return TokenResponse(access_token=token, expires_at=expires_at.isoformat() if expires_at else None)


@router.get("/me", response_model=UserMeResponse)
def me(current_user: User = Depends(get_current_user)):
    return UserMeResponse(
        id=current_user.id,
        username=current_user.username,
        full_name=current_user.full_name,
        role=current_user.role,
        permissions=sorted(get_permissions_for_role(current_user.role)),
        is_active=bool(current_user.is_active),
        last_login_at=current_user.last_login_at.isoformat() if getattr(current_user, "last_login_at", None) else None,
    )
