"""GitHub App Authentication Service."""

import logging
import time

import jwt

from app.config import settings

logger = logging.getLogger(__name__)

class GitHubAppAuthError(Exception):
    """Raised when GitHub App authentication fails."""
    pass

class GitHubAppAuth:
    """Generates JWTs for GitHub App authentication."""

    @staticmethod
    def is_configured() -> bool:
        """Check if GitHub App credentials are provided."""
        return bool(settings.github_app_id and settings.github_app_private_key)

    @staticmethod
    def generate_jwt() -> str:
        """Generate a GitHub App JWT.

        Raises:
            GitHubAppAuthError: If generation fails or app is unconfigured.
        """
        if not GitHubAppAuth.is_configured():
            raise GitHubAppAuthError("GitHub App is not configured.")

        # GitHub App JWTs are valid for 10 minutes maximum (we use 5)
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + (5 * 60),
            "iss": settings.github_app_id,
        }

        try:
            # RS256 algorithm is required by GitHub
            encoded_jwt = jwt.encode(
                payload,
                settings.github_app_private_key,
                algorithm="RS256"
            )
            return encoded_jwt
        except Exception as exc:
            logger.error("Failed to generate GitHub App JWT")
            raise GitHubAppAuthError("Failed to generate GitHub App JWT") from exc
