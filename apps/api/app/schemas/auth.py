from __future__ import annotations

from pydantic import BaseModel, Field


class UserResponse(BaseModel):
    id: str
    email: str | None = None
    display_name: str
    avatar_url: str | None = None
    is_active: bool = True
    github_login: str | None = None
    github_user_id: int | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class LoginResponse(BaseModel):
    authorization_url: str
    state: str = Field(..., description="CSRF protection state nonce")
