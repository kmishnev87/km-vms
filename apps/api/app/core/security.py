import base64
import hashlib
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet
from passlib.context import CryptContext

from app.core.config import settings
from app.core.user_sessions import user_session_generation, validate_user_session_generation

ACCESS_TOKEN_TYPE = "access"

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def _build_fernet() -> Fernet:
    raw = settings.encryption_key.encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


fernet = _build_fernet()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(subject: str, expires_minutes: int = 60 * 12, expires_at: datetime | None = None, *, user_id: int) -> str:
    if type(user_id) is not int or user_id < 1:
        raise ValueError("A persisted user id is required")
    now = datetime.now(timezone.utc)
    expiry = expires_at or (now + timedelta(minutes=expires_minutes))
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    payload = {
        "typ": ACCESS_TOKEN_TYPE,
        "sub": subject,
        "uid": user_id,
        "usg": user_session_generation(create=True),
        "iat": int(now.timestamp()),
        "exp": int(expiry.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    token_type = payload.get("typ")
    if token_type != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("Invalid access token type")
    # A reusable username is display metadata, never the account authority.
    if type(payload.get("uid")) is not int or payload["uid"] < 1:
        raise jwt.InvalidTokenError("Invalid access token identity")
    try:
        validate_user_session_generation(payload)
    except (ValueError, OSError) as exc:
        raise jwt.InvalidTokenError("user_session_expired") from exc
    return payload


def encrypt_text(value: str | None) -> str | None:
    if not value:
        return None
    return fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_text(value: str | None) -> str | None:
    if not value:
        return None
    return fernet.decrypt(value.encode("utf-8")).decode("utf-8")
