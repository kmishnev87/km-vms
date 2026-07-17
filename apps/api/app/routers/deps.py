from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.permissions import user_has_permission
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User
from app.services.security_audit import audit_security_denied

bearer_scheme = HTTPBearer(auto_error=False)
FORBIDDEN_DETAIL = "Раздел недоступен. Ограничены права пользователя."
INVALID_TOKEN_DETAIL = "Недействительный токен"
USER_NOT_FOUND_DETAIL = "Пользователь не найден"


def get_current_user(
    request: Request = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        audit_security_denied(
            db=db,
            request=request,
            event_type="security.unauthenticated_access_denied",
            reason="missing_authorization",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_TOKEN_DETAIL,
        )
    try:
        payload = decode_access_token(credentials.credentials)
    except Exception:
        audit_security_denied(
            db=db,
            request=request,
            event_type="security.unauthenticated_access_denied",
            reason="invalid_access_token",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_TOKEN_DETAIL,
        )

    username = payload.get("sub")
    if not username:
        audit_security_denied(
            db=db,
            request=request,
            event_type="security.unauthenticated_access_denied",
            reason="missing_subject",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_TOKEN_DETAIL,
        )

    user = db.query(User).filter(User.username == username).first()
    if not user or not getattr(user, "is_active", True):
        audit_security_denied(
            db=db,
            request=request,
            event_type="security.unauthenticated_access_denied",
            reason="user_not_found_or_inactive",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=USER_NOT_FOUND_DETAIL,
        )
    return user


def require_auth(current_user: User = Depends(get_current_user)) -> User:
    return current_user


def require_permission(permission: str):
    def dependency(
        current_user: User = Depends(get_current_user),
        request: Request = None,
        db: Session = Depends(get_db),
    ) -> User:
        if not user_has_permission(current_user.role, permission):
            audit_security_denied(
                db=db,
                actor=current_user,
                request=request,
                event_type="security.permission_denied",
                reason="missing_permission",
                status_code=status.HTTP_403_FORBIDDEN,
                required_permission=permission,
                metadata={"actor_role": getattr(current_user, "role", None)},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return current_user

    return dependency


def require_permissions(*permissions: str):
    required = tuple(dict.fromkeys(str(item) for item in permissions if str(item)))
    if not required:
        raise ValueError("at_least_one_permission_required")

    def dependency(
        current_user: User = Depends(get_current_user),
        request: Request = None,
        db: Session = Depends(get_db),
    ) -> User:
        missing = [permission for permission in required if not user_has_permission(current_user.role, permission)]
        if missing:
            audit_security_denied(
                db=db,
                actor=current_user,
                request=request,
                event_type="security.permission_denied",
                reason="missing_permission",
                status_code=status.HTTP_403_FORBIDDEN,
                required_permission="+".join(required),
                metadata={"actor_role": getattr(current_user, "role", None)},
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL)
        return current_user

    return dependency
