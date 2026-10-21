"""Repository-scoped GitHub credentials (CF-06).

Every GitHub operation CodeForge performs on a repository uses a GitHub App
installation token limited to that one repository and to the permissions the
operation needs.  There is no static, installation-wide or account-wide
credential.

resolve_repository_access() turns a repository name into a RepositoryAccess:
the active installation that CodeForge is authorized to use for it, and the
repository's numeric ID.  The ID is the one GitHub reports granting for a
read-only metadata token limited to that name within the installation, and
it must agree with GET /repos/{owner}/{repo}: so the ID is a repository of
the installation and is the repository the name denotes.  It is verified on
every resolution, never cached, so a renamed or replaced repository is
noticed at once.  A task started by a GitHub event also names the
installation and repository ID GitHub signed; any disagreement with them is
refused.  Anything that cannot be resolved fails closed.

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
from app.db.repositories.user_repository import UserRepository
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


def _split(repository: str) -> tuple[str, str]:
    parts = repository.split("/") if isinstance(repository, str) else []
    if len(parts) != 2 or not all(parts):
        raise RepositoryAccessError(f"Invalid repository name: {repository!r}")
    return parts[0], parts[1]


async def resolve_repository_access(
    session: AsyncSession,
    repository: str,
    *,
    expected_installation_id: int | None = None,
    expected_repository_id: int | None = None,
) -> RepositoryAccess:
    """The installation and verified repository ID for *repository*, or RepositoryAccessError.

    Requires an authorized repository row on an active installation whose
    account owns the repository.  Never falls back to any other credential.

    expected_installation_id and expected_repository_id are what a signed
    GitHub event said (a task's TaskGitHubLink, or the webhook itself): the
    resolution must arrive at exactly that installation and repository.
    """
    owner, name = _split(repository)
    query = (
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
        # Deterministic if stale rows leave two active installations listing
        # the same name.
        .order_by(GitHubInstallationRepository.installation_id)
    )
    if expected_installation_id is not None:
        query = query.where(GitHubInstallationRepository.installation_id == expected_installation_id)
    row = (await session.execute(query)).first()
    if row is None:
        if expected_installation_id is not None:
            raise RepositoryAccessError(
                f"{repository} is not authorized for installation {expected_installation_id} "
                "named by its GitHub event, or that installation is not active"
            )
        raise RepositoryAccessError(f"{repository} is not authorized for an active GitHub App installation")
    installation_id, account_login = int(row[0]), str(row[1])
    if account_login.lower() != owner.lower():
        # An installation only covers its own account's repositories; a name
        # scope would otherwise select that account's same-named repository.
        raise RepositoryAccessError(
            f"{repository} does not belong to the account of installation {installation_id}"
        )

    repository_id = await _verified_repository_id(installation_id, owner, name)
    if expected_repository_id is not None and repository_id != expected_repository_id:
        raise RepositoryAccessError(
            f"{repository} is now repository_id={repository_id}, "
            f"not repository_id={expected_repository_id} named by its GitHub event"
        )
    logger.info("Verified %s as repository_id=%s (installation %s)", repository, repository_id, installation_id)
    return RepositoryAccess(installation_id=installation_id, owner=owner, name=name, repository_id=repository_id)


async def signed_repository_of_task(session: AsyncSession, task_id: str) -> dict[str, int]:
    """What a GitHub-triggered task's signed event named, as resolve_repository_access keywords.

    Empty for a task with no GitHub link (created through the API): such a
    task has only the repository name to go on.
    """
    link = await UserRepository(session).get_task_github_link(task_id)
    if link is None:
        return {}
    return {
        "expected_installation_id": link.installation_id,
        "expected_repository_id": link.repository_id,
    }


async def _verified_repository_id(installation_id: int, owner: str, name: str) -> int:
    """The ID of the installation's repository *owner*/*name*, or RepositoryAccessError.

    Two independent answers must agree.  GitHub's grant for a metadata token
    limited to *name* says which repository of the installation that is;
    GET /repos/{owner}/{name} says which repository the full name denotes
    (for a public repository, whatever the token).  A cached grant can
    predate a rename, so a disagreement discards it and asks once more.
    """
    repository = f"{owner}/{name}"
    scope = TokenScope.for_repository_name(name, IDENTITY_PERMISSIONS)
    service = get_installation_token_service()
    for attempt in (1, 2):
        client = GitHubClient.for_installation(installation_id, scope=scope)
        try:
            grant = await service.get_grant(installation_id, scope)
            gh_repo = await client.get_repository(owner, name)
        except GitHubError as exc:
            raise RepositoryAccessError(
                f"Cannot verify {repository} with its installation: {exc}",
                retryable=bool(getattr(exc, "retryable", False)),
            ) from None
        finally:
            await client.aclose()
        granted_id = grant.repository_id
        if (
            isinstance(granted_id, int)
            and not isinstance(granted_id, bool)
            and granted_id > 0
            and str(grant.repository_full_name).lower() == repository.lower()
            and gh_repo.id == granted_id
            and not isinstance(gh_repo.id, bool)
            and gh_repo.full_name.lower() == repository.lower()
        ):
            return granted_id
        service.invalidate(installation_id, scope)
        logger.warning(
            "Repository identity disagreement for %s (installation %s, attempt %s): granted %s=%s, GET %s=%s",
            repository, installation_id, attempt,
            grant.repository_full_name, granted_id, gh_repo.full_name, gh_repo.id,
        )  # fmt: skip
    raise RepositoryAccessError(f"GitHub returned a different repository for {repository}")
