from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.services.auth_service import AuthService


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Validate Bearer session token and load the active authenticated User."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header[7:].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Empty bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_service = AuthService()
    payload = auth_service.decode_session_jwt(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject claim")

    user_repo = UserRepository(session)
    user = await user_repo.get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User account not found")

    if not user.is_active:
        raise HTTPException(status_code=401, detail="User account is deactivated or disabled")

    return user
