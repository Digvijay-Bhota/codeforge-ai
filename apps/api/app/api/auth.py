from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models import User
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.observability.events import AuditEventType, EventType
from app.observability.tracing import record_audit_event, record_event
from app.schemas.auth import LoginResponse, TokenResponse, UserResponse
from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/github/login", response_model=LoginResponse, summary="Initiate GitHub OAuth login")
async def github_login(
    redirect_uri: str | None = Query(None, description="Optional post-auth redirect URI"),
) -> LoginResponse:
    """Generate a GitHub OAuth authorization URL and a single-use CSRF state token."""
    auth_service = AuthService()
    auth_url, state_nonce = await auth_service.generate_oauth_url(redirect_uri=redirect_uri)
    return LoginResponse(authorization_url=auth_url, state=state_nonce)


@router.get("/github/callback", response_model=TokenResponse, summary="Complete GitHub OAuth login")
async def github_callback(
    code: str = Query(..., description="GitHub authorization code"),
    state: str = Query(..., description="CSRF state token"),
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    """Exchange authorization code, link GitHub user identity, and issue a CodeForge session token."""
    auth_service = AuthService()

    # 1. Verify single-use state nonce
    try:
        state_data = await auth_service.verify_and_consume_state(state)
    except HTTPException:
        await record_event(
            session,
            EventType.GITHUB_AUTH_FAILED,
            component="auth",
            metadata={"reason": "invalid_state"},
        )
        raise

    redirect_uri = state_data.get("redirect_uri")

    # 2. Exchange code for GitHub access token
    try:
        access_token = await auth_service.exchange_code_for_token(code, redirect_uri=redirect_uri)
    except HTTPException:
        await record_event(
            session,
            EventType.GITHUB_AUTH_FAILED,
            component="auth",
            metadata={"reason": "code_exchange_failed"},
        )
        raise

    # 3. Fetch GitHub identity
    profile = await auth_service.fetch_github_user_profile(access_token)

    # 4. Upsert User and GitHubIdentity
    user_repo = UserRepository(session)
    user, identity = await user_repo.upsert_github_user(
        github_user_id=profile["github_user_id"],
        github_login=profile["github_login"],
        display_name=profile["display_name"],
        email=profile.get("email"),
        avatar_url=profile.get("avatar_url"),
    )
    await session.commit()
    await session.refresh(user)

    # 5. Issue session JWT
    jwt_token = auth_service.create_session_jwt(
        user=user,
        github_user_id=identity.github_user_id,
        github_login=identity.github_login,
    )

    # 6. Observability
    await record_event(
        session,
        EventType.GITHUB_AUTH_SUCCESS,
        component="auth",
        metadata={"user_id": user.id, "github_login": identity.github_login},
    )
    await record_audit_event(
        session,
        AuditEventType.USER_AUTHENTICATED,
        actor_type="USER",
        actor_id=user.id,
        resource_type="auth",
        resource_id=identity.github_login,
        metadata={"github_user_id": identity.github_user_id},
    )

    return TokenResponse(
        access_token=jwt_token,
        token_type="bearer",  # nosec B106
        expires_in=auth_service.jwt_expiration_seconds,
        user=UserResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            is_active=user.is_active,
            github_login=identity.github_login,
            github_user_id=identity.github_user_id,
        ),
    )


@router.get("/me", response_model=UserResponse, summary="Get current authenticated user profile")
async def get_me(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """Retrieve profile and linked GitHub identity for the authenticated user."""
    user_repo = UserRepository(session)
    identity = await user_repo.get_github_identity(current_user.id)

    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        display_name=current_user.display_name,
        avatar_url=current_user.avatar_url,
        is_active=current_user.is_active,
        github_login=identity.github_login if identity else None,
        github_user_id=identity.github_user_id if identity else None,
    )
