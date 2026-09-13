from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
            async with GitHubClient.for_installation(installation_id) as client:
                perm = await client.get_collaborator_permission(
                    owner, repo, github_login
                )

            result = "none"
            if perm.permission == "admin" or perm.can_admin:
                result = "admin"
            elif perm.permission in ("maintain", "write", "push") or perm.can_push or perm.can_write:
                result = "write"
            elif perm.permission in ("triage", "read", "pull") or perm.can_pull or perm.can_read:
                result = "read"

            logger.info(
                "Collaborator permission resolved: installation_id=%s repo=%s/%s github_login=%s permission=%s",
                installation_id,
                owner,
                repo,
                github_login,
                result,
            )
            return result
        except Exception as exc:
            logger.error(
                "Error checking repository collaborator permission for %s/%s (installation_id=%s, github_login=%s): %s",
                owner,
                repo,
                installation_id,
                github_login,
                exc,
            )
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
