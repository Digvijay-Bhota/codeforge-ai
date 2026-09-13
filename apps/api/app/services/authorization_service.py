from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import GitHubInstallation, GitHubInstallationRepository, User
from app.db.repositories.user_repository import UserRepository
from app.github.app_auth import GitHubAppAuth
from app.github.client import GitHubClient
from app.services.queue_service import get_redis_client

logger = logging.getLogger(__name__)

# Type for optional permission resolver override in tests
PermissionResolver = Callable[
    [str, str, str], str
]  # (owner, repo, github_login) -> "admin"|"write"|"read"|"none"
_collaborator_resolver_override: PermissionResolver | None = None


def set_collaborator_permission_resolver(resolver: PermissionResolver | None) -> None:
    """Set custom collaborator permission resolver (useful for testing)."""
    global _collaborator_resolver_override
    _collaborator_resolver_override = resolver


class AuthorizationService:
    """Combines CodeForge installation status with GitHub repository permissions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.user_repo = UserRepository(session)

    async def get_authorized_installation(
        self, repository: str
    ) -> GitHubInstallationRepository | None:
        """Find active and authorized GitHub App installation for a repository."""
        stmt = (
            select(GitHubInstallationRepository)
            .join(
                GitHubInstallation,
                GitHubInstallation.installation_id == GitHubInstallationRepository.installation_id,
            )
            .where(
                GitHubInstallationRepository.repository == repository,
                GitHubInstallationRepository.authorized.is_(True),
                GitHubInstallation.active.is_(True),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_user_repository_permission(self, user: User, repository: str) -> str:
        """Resolve effective permission for a user on a repository.

        Returns one of: 'admin', 'write', 'read', 'none'.
        Combines:
        1. CodeForge installation authorization
        2. User's linked GitHub identity
        3. GitHub API collaborator permission (cached in Redis for 5m)
        """
        # 1. Verify CodeForge is installed & authorized for this repository
        install_repo = await self.get_authorized_installation(repository)
        if not install_repo:
            logger.info("Repository %s has no active CodeForge installation", repository)
            return "none"

        # 2. Check user's linked GitHub identity
        identity = await self.user_repo.get_github_identity(user.id)
        if not identity:
            logger.info("User %s has no linked GitHub identity", user.id)
            return "none"

        # 3. Check Redis cache
        cache_key = f"cf_perm:{repository}:{identity.github_user_id}"
        try:
            redis = get_redis_client()
            cached = await redis.get(cache_key)
            if cached:
                cached_str = cached if isinstance(cached, str) else cached.decode("utf-8")
                return cached_str
        except Exception as exc:
            logger.debug("Redis cache check error: %s", exc)

        # 4. Check test override if configured
        owner, repo = repository.split("/") if "/" in repository else ("", "")
        if _collaborator_resolver_override is not None:
            perm = _collaborator_resolver_override(owner, repo, identity.github_login)
            await self._cache_permission(cache_key, perm)
            return perm

        # 5. Query GitHub Collaborator API using GitHub App installation token
        perm = await self._fetch_github_permission(
            install_repo.installation_id, owner, repo, identity.github_login
        )
        await self._cache_permission(cache_key, perm)
        return perm

    async def _cache_permission(self, cache_key: str, permission: str) -> None:
        try:
            redis = get_redis_client()
            await redis.setex(cache_key, 300, permission)
        except Exception as exc:
            logger.debug("Failed to cache permission in Redis: %s", exc)

    async def invalidate_permission_cache(self, repository: str, github_user_id: int) -> None:
        cache_key = f"cf_perm:{repository}:{github_user_id}"
        try:
            redis = get_redis_client()
            await redis.delete(cache_key)
        except Exception as exc:
            logger.debug("Failed to invalidate permission cache: %s", exc)

    async def _fetch_github_permission(
        self, installation_id: int, owner: str, repo: str, github_login: str
    ) -> str:
        """Call GitHub API to determine collaborator permission level."""
        if not GitHubAppAuth.is_configured():
            logger.warning("GitHub App unconfigured; failing closed for collaborator permission")
            return "none"

        try:
            jwt_token = GitHubAppAuth.generate_jwt()
            app_client = GitHubClient(token=jwt_token)
            install_token = await app_client.create_installation_access_token(installation_id)

            url = f"{settings.github_api_url.rstrip('/')}/repos/{owner}/{repo}/collaborators/{github_login}/permission"
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"Bearer {install_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                permission = data.get("permission", "none")
                if permission in ("admin", "write", "read"):
                    return str(permission)
                # Map role_name or granular permissions if available
                perms_obj = data.get("user", {}).get("permissions", {})
                if perms_obj.get("admin"):
                    return "admin"
                if perms_obj.get("push"):
                    return "write"
                if perms_obj.get("pull"):
                    return "read"
                return "none"
            elif resp.status_code in (404, 403):
                # User is not a collaborator on this repository
                return "none"
            else:
                logger.error("GitHub collaborator check failed with HTTP %s", resp.status_code)
                return "none"
        except Exception as exc:
            logger.error("Error checking repository collaborator permission: %s", exc)
            return "none"

    async def can_read_repository(self, user: User, repository: str) -> bool:
        perm = await self.get_user_repository_permission(user, repository)
        return perm in ("read", "write", "admin")

    async def can_write_repository(self, user: User, repository: str) -> bool:
        perm = await self.get_user_repository_permission(user, repository)
        return perm in ("write", "admin")

    async def can_admin_repository(self, user: User, repository: str) -> bool:
        perm = await self.get_user_repository_permission(user, repository)
        return perm == "admin"
