from datetime import datetime

from pydantic import BaseModel, Field

from app.core.permissions import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER


ROLES = {ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER}


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str = ""
    role: str
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login_at: datetime | None = None


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=8, max_length=256)
    role: str
    display_name: str | None = Field(default="", max_length=255)
    is_active: bool = True


class UserUpdateRequest(BaseModel):
    role: str | None = None
    display_name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)
