from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.models.user import User
from app.routers.deps import get_current_user
from app.schemas.auth import LoginRequest, TokenResponse, UserMeResponse
from app.services.system_settings import get_system_settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not getattr(user, "is_active", True) or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
        )

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

    token = create_access_token(user.username, expires_at=expires_at)
    user.last_login_at = datetime.utcnow()
    db.add(user)
    db.commit()
    return TokenResponse(access_token=token, expires_at=expires_at.isoformat() if expires_at else None)


@router.get("/me", response_model=UserMeResponse)
def me(current_user: User = Depends(get_current_user)):
    return UserMeResponse(
        id=current_user.id,
        username=current_user.username,
        full_name=current_user.full_name,
        role=current_user.role,
        permissions=sorted(ROLE_PERMISSIONS.get(current_user.role, set())),
        is_active=bool(current_user.is_active),
        last_login_at=current_user.last_login_at.isoformat() if getattr(current_user, "last_login_at", None) else None,
    )
