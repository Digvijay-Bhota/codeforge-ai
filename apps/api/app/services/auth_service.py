from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException

from app.config import settings
from app.db.models import User
from app.services.queue_service import get_redis_client

logger = logging.getLogger(__name__)

# In-memory fallback cache for state when Redis is unavailable (e.g. lightweight unit tests)
_in_memory_state_store: dict[str, tuple[str, float]] = {}


class AuthService:
    """Manages GitHub OAuth flows, state validation, and CodeForge session JWTs."""

    def __init__(self) -> None:
        self.client_id = settings.github_client_id
        self.client_secret = settings.github_client_secret
        self.redirect_uri = settings.github_oauth_redirect_uri
        self.jwt_secret = settings.jwt_secret_key
        self.jwt_algorithm = settings.jwt_algorithm
        self.jwt_expiration_seconds = settings.jwt_expiration_seconds

    def validate_redirect_uri(self, redirect_uri: str | None) -> str:
        """Ensure the redirect URI is allowed to prevent open redirect vulnerabilities."""
        if not redirect_uri:
            return self.redirect_uri

        allowed = settings.allowed_oauth_redirect_uris or [self.redirect_uri]
        if redirect_uri not in allowed:
            # Also allow localhost variants
            if not any(
                redirect_uri.startswith(allowed_prefix)
                for allowed_prefix in ["http://localhost:", "http://127.0.0.1:"]
            ):
                raise HTTPException(status_code=400, detail="Invalid or disallowed redirect URI")

        return redirect_uri

    async def generate_oauth_url(self, redirect_uri: str | None = None) -> tuple[str, str]:
        """Generate a cryptographically secure OAuth authorization URL and state nonce."""
        target_redirect_uri = self.validate_redirect_uri(redirect_uri)
        state_nonce = secrets.token_urlsafe(32)

        payload = json.dumps(
            {
                "redirect_uri": target_redirect_uri,
                "created_at": time.time(),
            }
        )

        try:
            redis = get_redis_client()
            await redis.setex(
                f"oauth_state:{state_nonce}",
                settings.oauth_state_ttl_seconds,
                payload,
            )
        except Exception as exc:
            logger.warning(
                "Redis unavailable for OAuth state, using in-memory store fallback: %s", exc
            )
            _in_memory_state_store[state_nonce] = (
                payload,
                time.time() + settings.oauth_state_ttl_seconds,
            )

        params = {
            "client_id": self.client_id or "dummy_client_id",
            "redirect_uri": target_redirect_uri,
            "scope": "read:user user:email",
            "state": state_nonce,
        }
        url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"
        return url, state_nonce

    async def verify_and_consume_state(self, state_nonce: str) -> dict[str, Any]:
        """Verify the OAuth state nonce is valid, single-use, and not expired."""
        if not state_nonce:
            raise HTTPException(status_code=400, detail="Missing OAuth state")

        payload_str: str | None = None

        try:
            redis = get_redis_client()
            # Atomic get and delete for single-use guarantee
            val = await redis.get(f"oauth_state:{state_nonce}")
            if val is not None:
                await redis.delete(f"oauth_state:{state_nonce}")
                payload_str = val if isinstance(val, str) else val.decode("utf-8")
        except Exception as exc:
            logger.debug("Redis state lookup error, checking fallback: %s", exc)

        if not payload_str and state_nonce in _in_memory_state_store:
            stored_payload, expiry = _in_memory_state_store.pop(state_nonce)
            if time.time() <= expiry:
                payload_str = stored_payload

        if not payload_str:
            raise HTTPException(
                status_code=400, detail="Invalid, expired, or previously used OAuth state"
            )

        try:
            data = json.loads(payload_str)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Malformed OAuth state metadata") from exc

    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> str:
        """Exchange GitHub OAuth authorization code for a temporary user access token."""
        if not self.client_id or not self.client_secret:
            raise HTTPException(
                status_code=503,
                detail="GitHub OAuth credentials are not configured on the server",
            )

        url = "https://github.com/login/oauth/access_token"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
        }
        if redirect_uri:
            data["redirect_uri"] = redirect_uri

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(url, headers=headers, data=data)
            except httpx.RequestError as exc:
                logger.error("Failed to connect to GitHub OAuth endpoint: %s", exc)
                raise HTTPException(
                    status_code=502, detail="Failed to reach GitHub for OAuth exchange"
                ) from exc

        if response.status_code != 200:
            logger.error("GitHub OAuth exchange returned HTTP %s", response.status_code)
            raise HTTPException(
                status_code=400, detail="Failed to exchange authorization code with GitHub"
            )

        token_data = response.json()
        if "error" in token_data:
            error_desc = token_data.get("error_description", token_data["error"])
            logger.warning("GitHub OAuth exchange error: %s", error_desc)
            raise HTTPException(status_code=400, detail=f"GitHub OAuth error: {error_desc}")

        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received from GitHub")

        return str(access_token)

    async def fetch_github_user_profile(self, access_token: str) -> dict[str, Any]:
        """Fetch authenticated user profile and primary email from GitHub."""
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {access_token}",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                user_res = await client.get("https://api.github.com/user", headers=headers)
            except httpx.RequestError as exc:
                raise HTTPException(
                    status_code=502, detail="Failed to contact GitHub API for user profile"
                ) from exc

            if user_res.status_code != 200:
                raise HTTPException(
                    status_code=user_res.status_code, detail="Failed to fetch GitHub user profile"
                )

            user_data = user_res.json()

            # If public email is empty, fetch user emails
            email = user_data.get("email")
            if not email:
                try:
                    emails_res = await client.get(
                        "https://api.github.com/user/emails", headers=headers
                    )
                    if emails_res.status_code == 200:
                        emails_data = emails_res.json()
                        for entry in emails_data:
                            if entry.get("primary") and entry.get("verified"):
                                email = entry.get("email")
                                break
                            if not email and entry.get("email"):
                                email = entry.get("email")
                except Exception as exc:
                    logger.debug("Failed to fetch user emails: %s", exc)

        return {
            "github_user_id": user_data["id"],
            "github_login": user_data["login"],
            "display_name": user_data.get("name") or user_data["login"],
            "email": email,
            "avatar_url": user_data.get("avatar_url"),
        }

    def create_session_jwt(
        self,
        user: User,
        github_user_id: int,
        github_login: str,
    ) -> str:
        """Issue a signed CodeForge session JWT."""
        now = datetime.now(UTC)
        exp = now + timedelta(seconds=self.jwt_expiration_seconds)

        payload = {
            "sub": str(user.id),
            "type": "session",
            "github_user_id": github_user_id,
            "github_login": github_login,
            "display_name": user.display_name,
            "email": user.email,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "jti": str(uuid.uuid4()),
        }

        return jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)

    def decode_session_jwt(self, token: str) -> dict[str, Any]:
        """Validate and decode a CodeForge session JWT."""
        try:
            payload = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=[self.jwt_algorithm],
                options={"require": ["sub", "exp", "type"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="Session token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail="Invalid session token") from exc

        if payload.get("type") != "session":
            raise HTTPException(status_code=401, detail="Invalid token type")

        return payload
