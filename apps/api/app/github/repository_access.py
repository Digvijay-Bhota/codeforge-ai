"""Repository-scoped GitHub credentials (CF-06).

Every GitHub operation CodeForge performs on a repository uses a GitHub App
installation token limited to that one repository and to the permissions the
operation needs.  There is no static, installation-wide or account-wide
credential.

resolve_repository_access() turns a repository name into a RepositoryAccess:
the active installation that CodeForge is authorized to use for it, and the
repository's numeric ID.  The ID is looked up with a read-only metadata
token and checked against the requested name, so a token is never minted for
a different repository that happens to share the name.  Anything that cannot
be resolved fails closed.

Permission sets, from GitHub's "Permissions required for GitHub Apps"
(https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)
and "Choosing permissions for Git access"
(https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app):

- IDENTITY: GET /repos/{owner}/{repo} needs Metadata read.
- GENERATION: the read-only clone (Git over HTTPS needs Contents), GET
  /repos/{owner}/{repo} (Metadata read) and GET .../branches/{branch}
  (Contents read).  No write access of any kind.
- PUBLICATION: POST .../git/refs (Contents write), the push (Contents),
  GET .../pulls (Pull requests read) and POST .../pulls (Pull requests
  write).  The publication clone uses a GENERATION token.
- STATUS: check runs (Checks write), issue/PR comments (Issues write; Issues
  or Pull requests suffices), GET .../pulls/{number} (Pull requests read;
  Contents or Pull requests suffices).
- COLLABORATOR: GET .../collaborators/{username}/permission needs Metadata read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GitHubInstallation, GitHubInstallationRepository
from app.github.client import GitHubClient
from app.github.exceptions import GitHubError
from app.github.token_service import TokenScope, get_installation_token_service

logger = logging.getLogger(__name__)

IDENTITY_PERMISSIONS = {"metadata": "read"}
GENERATION_PERMISSIONS = {"contents": "read", "metadata": "read"}
PUBLICATION_PERMISSIONS = {"contents": "write", "pull_requests": "write"}
STATUS_PERMISSIONS = {"checks": "write", "issues": "write", "pull_requests": "read"}
COLLABORATOR_PERMISSIONS = {"metadata": "read"}


class RepositoryAccessError(Exception):
    """CodeForge may not, or cannot now, obtain a credential for the repository."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class RepositoryAccess:
    """An authorized repository and the installation whose tokens may access it."""

    installation_id: int
    owner: str
    name: str
    repository_id: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def scope(self, permissions: dict[str, str]) -> TokenScope:
        return TokenScope.for_repository_id(self.repository_id, permissions)

    def client(self, permissions: dict[str, str], **kwargs: Any) -> GitHubClient:
        """An API client whose tokens cover only this repository and *permissions*."""
        return GitHubClient.for_installation(self.installation_id, scope=self.scope(permissions), **kwargs)

    async def token(self, permissions: dict[str, str]) -> str:
        """A token covering only this repository and *permissions*, for Git over HTTPS."""
        return await get_installation_token_service().get_token(
            self.installation_id, self.scope(permissions)
        )


# (installation_id, lower-case full name) -> verified repository ID.
_repository_ids: dict[tuple[int, str], int] = {}


def _split(repository: str) -> tuple[str, str]:
    parts = repository.split("/") if isinstance(repository, str) else []
    if len(parts) != 2 or not all(parts):
        raise RepositoryAccessError(f"Invalid repository name: {repository!r}")
    return parts[0], parts[1]


async def resolve_repository_access(session: AsyncSession, repository: str) -> RepositoryAccess:
    """The installation and verified repository ID for *repository*, or RepositoryAccessError.

    Requires an authorized repository row on an active installation whose
    account owns the repository.  Never falls back to any other credential.
    """
    owner, name = _split(repository)
    row = (
        await session.execute(
            select(GitHubInstallationRepository.installation_id, GitHubInstallation.account_login)
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
    ).first()
    if row is None:
        raise RepositoryAccessError(f"{repository} is not authorized for an active GitHub App installation")
    installation_id, account_login = int(row[0]), str(row[1])
    if account_login.lower() != owner.lower():
        # An installation only covers its own account's repositories; a name
        # scope would otherwise select that account's same-named repository.
        raise RepositoryAccessError(
            f"{repository} does not belong to the account of installation {installation_id}"
        )

    key = (installation_id, repository.lower())
    repository_id = _repository_ids.get(key)
    if repository_id is None:
        scope = TokenScope.for_repository_name(name, IDENTITY_PERMISSIONS)
        client = GitHubClient.for_installation(installation_id, scope=scope)
        try:
            gh_repo = await client.get_repository(owner, name)
        except GitHubError as exc:
            raise RepositoryAccessError(
                f"Cannot verify {repository} with its installation: {exc}",
                retryable=bool(getattr(exc, "retryable", False)),
            ) from None
        finally:
            await client.aclose()
        if (
            gh_repo.full_name.lower() != repository.lower()
            or not isinstance(gh_repo.id, int)
            or isinstance(gh_repo.id, bool)
            or gh_repo.id <= 0
        ):
            raise RepositoryAccessError(f"GitHub returned a different repository for {repository}")
        repository_id = gh_repo.id
        _repository_ids[key] = repository_id
        logger.info("Verified %s as repository_id=%s (installation %s)", repository, repository_id, installation_id)
    return RepositoryAccess(installation_id=installation_id, owner=owner, name=name, repository_id=repository_id)


def clear_repository_id_cache() -> None:
    """Forget verified repository IDs (tests; and after installation changes)."""
    _repository_ids.clear()
