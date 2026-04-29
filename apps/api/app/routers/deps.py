from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.permissions import user_has_permission
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=True)
FORBIDDEN_DETAIL = "Раздел недоступен. Ограничены права пользователя."
INVALID_TOKEN_DETAIL = "Недействительный токен"
USER_NOT_FOUND_DETAIL = "Пользователь не найден"


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = decode_access_token(credentials.credentials)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_TOKEN_DETAIL,
        )

    username = payload.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_TOKEN_DETAIL,
        )

    user = db.query(User).filter(User.username == username).first()
    if not user or not getattr(user, "is_active", True):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=USER_NOT_FOUND_DETAIL,
        )
    return user


def require_auth(current_user: User = Depends(get_current_user)) -> User:
    return current_user


def require_permission(permission: str):
    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if not user_has_permission(current_user.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return current_user

    return dependency
