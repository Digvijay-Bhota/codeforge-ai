from __future__ import annotations

import json
import logging
import re
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

# OAuth state lives only in Redis (CF-13).  There is deliberately no in-process
# fallback: a write that fails ambiguously could leave one state in both stores
# (redeemable twice), and a per-process store does not work across API workers.
# Without Redis, sign-in fails closed with 503.
_STATE_KEY_PREFIX = "oauth_state:"
# secrets.token_urlsafe(32): 43 URL-safe base64 characters.
_STATE_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
_STATE_UNAVAILABLE = "Sign-in is temporarily unavailable. Try again shortly."


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
        """The redirect URI to use: the configured default, or an exact allowlist entry.

        Exact string comparison only (CF-08): no prefix, host or port rules, no
        normalization.  A local development URL is allowed only by listing it
        in ALLOWED_OAUTH_REDIRECT_URIS.
        """
        if not redirect_uri:
            return self.redirect_uri

        allowed = {*settings.allowed_oauth_redirect_uris, self.redirect_uri}
        if redirect_uri not in allowed:
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
                f"{_STATE_KEY_PREFIX}{state_nonce}",
                settings.oauth_state_ttl_seconds,
                payload,
            )
        except Exception as exc:
            # The write may or may not have happened; either way this state is
            # never handed out, so it cannot be redeemed (it simply expires).
            logger.warning("Redis unavailable for OAuth state; refusing sign-in: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=_STATE_UNAVAILABLE) from None

        params = {
            "client_id": self.client_id or "dummy_client_id",
            "redirect_uri": target_redirect_uri,
            "scope": "read:user user:email",
            "state": state_nonce,
        }
        url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"
        return url, state_nonce

    async def verify_and_consume_state(self, state_nonce: str) -> dict[str, Any]:
        """Verify the OAuth state nonce is valid, single-use, and not expired.

        GETDEL (Redis >= 6.2) reads and removes the state in one atomic step,
        so of any number of concurrent callbacks with the same state, at most
        one receives it (CF-13).  Expiry is the key's TTL.
        """
        if not state_nonce:
            raise HTTPException(status_code=400, detail="Missing OAuth state")
        if not _STATE_FORMAT.fullmatch(state_nonce):
            raise HTTPException(
                status_code=400, detail="Invalid, expired, or previously used OAuth state"
            )

        try:
            redis = get_redis_client()
            val = await redis.getdel(f"{_STATE_KEY_PREFIX}{state_nonce}")
        except Exception as exc:
            logger.warning("Redis unavailable for OAuth state; refusing sign-in: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=_STATE_UNAVAILABLE) from None

        payload_str: str | None = None
        if val is not None:
            payload_str = val if isinstance(val, str) else val.decode("utf-8")

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

        self._require_secure_secret()
        return jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)

    def _require_secure_secret(self) -> None:
        """Refuse to sign or verify sessions with an insecure secret.

        Settings already rejects one at startup; this re-check means a mutated
        or bypassed Settings object can never make session tokens forgeable.
        """
        if settings.jwt_secret_violation() is not None:
            logger.error("Session signing refused: JWT_SECRET_KEY is not securely configured")
            raise HTTPException(status_code=503, detail="Session signing is not configured")

    def decode_session_jwt(self, token: str) -> dict[str, Any]:
        """Validate and decode a CodeForge session JWT."""
        self._require_secure_secret()
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
