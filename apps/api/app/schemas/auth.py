from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str
    stay_signed_in: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str | None = None


class UserMeResponse(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    permissions: list[str] = []
    is_active: bool = True
    last_login_at: str | None = None
