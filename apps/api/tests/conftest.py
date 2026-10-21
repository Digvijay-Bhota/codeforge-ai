import os
import secrets
import uuid
from typing import Any

# Set before anything imports app.config, whose Settings singleton refuses the
# public development JWT secret unless APP_ENV is explicitly development/test.
# A random per-run secret keeps APP_ENV unset for the tests (the safe default).
os.environ.setdefault("JWT_SECRET_KEY", secrets.token_urlsafe(48))

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base  # noqa: E402
from app.db.models import GitHubIdentity, User  # noqa: E402

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", os.getenv("DATABASE_URL", "postgresql+asyncpg://codeforge:codeforge@localhost:5433/codeforge"))

@pytest_asyncio.fixture
async def setup_db():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()

# The GitHub user ID that local_workspace_root trusts for local execution
# (CODEFORGE_LOCAL_EXECUTION_GITHUB_IDS).  Tests that create or run local tasks
# need a creator whose GitHub identity has this ID.
LOCAL_TEST_GITHUB_ID = 990001


@pytest.fixture
def local_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Opt in to execution_target=local, confined to a fresh WORKSPACE_ROOT.

    Trusts LOCAL_TEST_GITHUB_ID, creates its WORKSPACE_ROOT/<id> directory and
    points GitHub clones at a separate directory.
    """
    from app.config import settings

    root = tmp_path / "workspace-root"
    (root / str(LOCAL_TEST_GITHUB_ID)).mkdir(parents=True)
    clones = tmp_path / "github-clones"
    clones.mkdir()
    monkeypatch.setattr(settings, "codeforge_allow_local_execution", True)
    monkeypatch.setattr(settings, "workspace_root", str(root))
    monkeypatch.setattr(settings, "codeforge_local_execution_github_ids", [LOCAL_TEST_GITHUB_ID])
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(clones))
    return root.resolve()


async def create_trusted_local_user(session: AsyncSession) -> User:
    """Get or create the active user whose GitHub identity is LOCAL_TEST_GITHUB_ID."""
    identity = (
        await session.execute(
            select(GitHubIdentity).where(GitHubIdentity.github_user_id == LOCAL_TEST_GITHUB_ID)
        )
    ).scalar_one_or_none()
    if identity is not None:
        user = await session.get(User, identity.user_id)
        assert user is not None
        return user
    user = User(id=str(uuid.uuid4()), display_name="Local Owner", is_active=True)
    session.add(user)
    await session.flush()
    session.add(
        GitHubIdentity(
            user_id=user.id, github_user_id=LOCAL_TEST_GITHUB_ID, github_login="local-owner"
        )
    )
    await session.commit()
    return user


@pytest.fixture
def local_owner_root(local_workspace_root: Path) -> Path:
    """WORKSPACE_ROOT/<LOCAL_TEST_GITHUB_ID>: where the trusted test account's workspaces live."""
    return local_workspace_root / str(LOCAL_TEST_GITHUB_ID)


@pytest.fixture
def local_workspace(local_owner_root: Path) -> Path:
    """A local workspace directory that satisfies the local execution policy."""
    ws = local_owner_root / "repo"
    ws.mkdir()
    return ws


@pytest_asyncio.fixture
async def session(setup_db):
    async_session = async_sessionmaker(setup_db, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


# ── GitHub credential doubles (CF-06) ──────────────────────────────────────────
# Repository access is resolved from authorized installations and every token
# is scoped to one repository and explicit permissions.  These doubles let
# tests drive GitHub code paths without an installation, while recording
# exactly which scopes were requested.


def repository_access_for(
    full_name: str = "o/r", *, installation_id: int = 1, repository_id: int = 101
) -> Any:
    from app.github.repository_access import RepositoryAccess

    owner, name = full_name.split("/")
    return RepositoryAccess(
        installation_id=installation_id, owner=owner, name=name, repository_id=repository_id
    )


class FakeInstallationTokens:
    """Stands in for InstallationTokenService: a distinct fake token per scope, every mint recorded."""

    def __init__(self) -> None:
        self.minted: list[tuple[int, Any]] = []

    @staticmethod
    def token_for(scope: Any) -> str:
        perms = "-".join(f"{name}.{level}" for name, level in scope.permissions)
        return f"ghs_fake_{scope.repository_key[1]}_{perms}"

    async def get_token(self, installation_id: int, scope: Any) -> str:
        self.minted.append((installation_id, scope))
        return self.token_for(scope)

    def invalidate(self, installation_id: int, scope: Any = None) -> None:
        return None

    def write_scopes(self) -> list[Any]:
        return [scope for _, scope in self.minted if any(level == "write" for _, level in scope.permissions)]


class RepositoryIdentityDouble:
    """Stands in for GitHub during resolve_repository_access's repository-identity check.

    Two answers must agree there: the repository GitHub reports granting for
    the name-scoped metadata token (granted_ids / granted_full_name), and
    GET /repos/{owner}/{repo} (listed_id / listed_full_name).  By default both
    are *full_name* with *repository_id*.  granted_ids is consumed one per
    grant, its last entry repeating, so a test can model a stale cached grant
    followed by a fresh one.
    """

    def __init__(self, full_name: str, repository_id: int = 101) -> None:
        from unittest.mock import AsyncMock, MagicMock

        self.full_name = full_name
        self.granted_ids: list[int | None] = [repository_id]
        self.granted_full_name: str | None = full_name
        self.listed_id: Any = repository_id
        self.listed_full_name = full_name
        self.grants: list[tuple[int, Any]] = []
        self.invalidated: list[tuple[int, Any]] = []
        self.client = MagicMock()
        self.client.aclose = AsyncMock()
        self.client.get_repository = AsyncMock(side_effect=self._get_repository)
        self.client_cls = MagicMock()
        self.client_cls.for_installation.return_value = self.client

    async def _get_repository(self, owner: str, name: str) -> Any:
        from app.github.models import GitHubRepository

        listed_owner, listed_name = self.listed_full_name.split("/")
        return GitHubRepository(
            id=self.listed_id, owner=listed_owner, name=listed_name, full_name=self.listed_full_name,
            default_branch="main", private=True, clone_url="", html_url="",
        )

    async def get_grant(self, installation_id: int, scope: Any) -> Any:
        from datetime import UTC, datetime, timedelta

        from app.github.token_service import CachedInstallationToken

        granted_id = self.granted_ids[min(len(self.grants), len(self.granted_ids) - 1)]
        self.grants.append((installation_id, scope))
        return CachedInstallationToken(
            token=f"ghs_identity_{len(self.grants)}", installation_id=installation_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1), scope=scope,
            repository_id=granted_id, repository_full_name=self.granted_full_name,
        )

    def invalidate(self, installation_id: int, scope: Any = None) -> None:
        self.invalidated.append((installation_id, scope))

    def patches(self) -> Any:
        """Context manager installing this double into app.github.repository_access."""
        from contextlib import ExitStack
        from unittest.mock import patch

        stack = ExitStack()
        stack.enter_context(patch("app.github.repository_access.GitHubClient", self.client_cls))
        stack.enter_context(patch("app.github.repository_access.get_installation_token_service", lambda: self))
        return stack


def install_github_doubles(monkeypatch: pytest.MonkeyPatch, client: Any) -> FakeInstallationTokens:
    """Make GitHubExecutionService use *client* for API calls and fake scoped Git tokens.

    Returns the token double, so tests can assert which scopes were minted.
    """
    from unittest.mock import AsyncMock, MagicMock

    if not isinstance(getattr(client, "aclose", None), AsyncMock):
        client.aclose = AsyncMock()
    client_cls = MagicMock()
    client_cls.for_installation.return_value = client
    tokens = FakeInstallationTokens()
    monkeypatch.setattr("app.execution.github_execution.GitHubClient", client_cls)
    monkeypatch.setattr("app.execution.github_execution.get_installation_token_service", lambda: tokens)
    return tokens
