from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GitHubInstallation, GitHubInstallationRepository, User
from app.db.repositories.user_repository import UserRepository
from app.github.app_auth import GitHubAppAuth
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


class PermissionCheckUnavailable(Exception):
    """GitHub could not be asked for a permission that must be checked fresh."""


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

    async def get_user_repository_permission(
        self, user: User, repository: str, *, use_cache: bool = True
    ) -> str:
        """Resolve effective permission for a user on a repository.

        Returns one of: 'admin', 'write', 'read', 'none'.  When GitHub cannot
        be asked: 'none' from the cache path, PermissionCheckUnavailable when
        use_cache=False.
        Combines:
        1. CodeForge installation authorization
        2. User's linked GitHub identity
        3. GitHub API collaborator permission (cached in Redis for 5m;
           use_cache=False skips reading the cache, for checks that must
           see a revocation at once)
        """
        permission = await self.repository_permission_if_known(user, repository, use_cache=use_cache)
        if permission is None:
            # GitHub did not answer.  Fail closed, but never remember it as
            # "none": that would refuse the user for five minutes after
            # GitHub recovers.  A fresh check reports the outage instead.
            if not use_cache:
                raise PermissionCheckUnavailable(f"GitHub could not confirm permissions on {repository}")
            return "none"
        return permission

    async def repository_permission_if_known(
        self, user: User, repository: str, *, use_cache: bool = True, ask_github: bool = True
    ) -> str | None:
        """As get_user_repository_permission, but None when GitHub could not be asked.

        *ask_github* False answers from the cache alone (None when nothing is
        cached), for callers that check many repositories and stop asking
        GitHub once it has failed them.
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
            cached = await redis.get(cache_key) if use_cache else None
            if cached:
                cached_str = cached if isinstance(cached, str) else cached.decode("utf-8")
                return cached_str
        except Exception as exc:
            logger.debug("Redis cache check error: %s", exc)

        if not ask_github:
            return None

        # 4. Check test override if configured
        owner, repo = repository.split("/") if "/" in repository else ("", "")
        if _collaborator_resolver_override is not None:
            perm = _collaborator_resolver_override(owner, repo, identity.github_login)
            await self._cache_permission(cache_key, perm)
            return perm

        # 5. Query GitHub Collaborator API using GitHub App installation token
        fetched = await self._fetch_github_permission(
            install_repo.installation_id, owner, repo, identity.github_login
        )
        if fetched is None:
            return None
        await self._cache_permission(cache_key, fetched)
        return fetched

    async def cached_repository_permissions(self, user: User, repositories: list[str]) -> dict[str, str]:
        """GitHub's cached answers for *repositories*, in one Redis round trip; uncached ones are absent.

        The same entries repository_permission_if_known reads; it still
        decides everything that is not cached (and checks the installation).
        """
        if not repositories:
            return {}
        identity = await self.user_repo.get_github_identity(user.id)
        if not identity:
            return {}
        keys = [f"cf_perm:{repository}:{identity.github_user_id}" for repository in repositories]
        try:
            values = await get_redis_client().mget(keys)
        except Exception as exc:
            logger.debug("Redis cache read error: %s", exc)
            return {}
        return {
            repository: value if isinstance(value, str) else value.decode("utf-8")
            for repository, value in zip(repositories, values, strict=True)
            if value
        }

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
        self, installation_id: int, owner: str, repo: str, github_login: str,
        repository_id: int | None = None,
    ) -> str | None:
        """Call GitHub API to determine collaborator permission level.

        *repository_id*, when known (a signed webhook), must be the
        repository the name resolves to.  None: GitHub could not be asked
        (an error, an outage); callers fail closed without caching it.
        """
        if not GitHubAppAuth.is_configured():
            logger.warning("GitHub App unconfigured; failing closed for collaborator permission")
            return "none"

        try:
            # A metadata-only token for this one repository (CF-06), from the
            # same installation that authorized it.
            from app.github.repository_access import (
                COLLABORATOR_PERMISSIONS,
                resolve_repository_access,
            )

            access = await resolve_repository_access(
                self.session,
                f"{owner}/{repo}",
                expected_installation_id=installation_id,
                expected_repository_id=repository_id,
            )
            async with access.client(COLLABORATOR_PERMISSIONS) as client:
                perm = await client.get_collaborator_permission(owner, repo, github_login)

            result = "none"
            if perm.permission == "admin" or perm.can_admin:
                result = "admin"
            elif (
                perm.permission in ("maintain", "write", "push") or perm.can_push or perm.can_write
            ):
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
            # A definitive refusal (repository not authorized for this
            # installation, credentials rejected) is "none".  A transient
            # failure, or one we cannot classify, is unknown: None.
            if getattr(exc, "retryable", True) is False:
                return "none"
            return None

    async def can_read_repository(self, user: User, repository: str) -> bool:
        perm = await self.get_user_repository_permission(user, repository)
        return perm in ("read", "write", "admin")

    async def can_write_repository(self, user: User, repository: str, *, use_cache: bool = True) -> bool:
        perm = await self.get_user_repository_permission(user, repository, use_cache=use_cache)
        return perm in ("write", "admin")

    async def can_admin_repository(self, user: User, repository: str) -> bool:
        perm = await self.get_user_repository_permission(user, repository)
        return perm == "admin"

    async def get_actor_repository_permission(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        github_login: str,
        github_user_id: int | None = None,
        repository_id: int | None = None,
    ) -> str:
        """Resolve effective collaborator permission for a GitHub actor on a repository.

        Returns one of: 'admin', 'write', 'read', 'none'.
        *repository_id* is the signed webhook's repository, when there is one.
        Combines:
        1. CodeForge installation authorization
        2. Redis permission caching (300s TTL)
        3. Collaborator resolver override (for testing)
        4. GitHub Collaborator API via installation token
        """
        repository = f"{owner}/{repo}"

        # 1. Verify CodeForge is installed & authorized for this repository
        install_repo = await self.get_authorized_installation(repository)
        if not install_repo or install_repo.installation_id != installation_id:
            logger.warning(
                "Repository %s has no active CodeForge installation matching installation_id=%s",
                repository,
                installation_id,
            )
            return "none"

        # 2. Check Redis cache
        cache_id = github_user_id if github_user_id is not None else github_login
        cache_key = f"cf_perm:{repository}:{cache_id}"
        try:
            redis = get_redis_client()
            cached = await redis.get(cache_key)
            if cached:
                cached_str = cached if isinstance(cached, str) else cached.decode("utf-8")
                return cached_str
        except Exception as exc:
            logger.debug("Redis cache check error: %s", exc)

        # 3. Check test override if configured
        if _collaborator_resolver_override is not None:
            perm = _collaborator_resolver_override(owner, repo, github_login)
            await self._cache_permission(cache_key, perm)
            return perm

        # 4. Query GitHub Collaborator API using GitHub App installation token
        fetched = await self._fetch_github_permission(
            installation_id, owner, repo, github_login, repository_id=repository_id
        )
        if fetched is None:
            return "none"  # unknown: refuse now, but do not cache the refusal
        await self._cache_permission(cache_key, fetched)
        return fetched

    async def can_actor_write_repository(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        github_login: str,
        github_user_id: int | None = None,
        repository_id: int | None = None,
    ) -> bool:
        """Return True if actor has write or admin permission on the repository."""
        perm = await self.get_actor_repository_permission(
            installation_id, owner, repo, github_login, github_user_id, repository_id=repository_id
        )
        return perm in ("write", "admin")
