import os
import secrets
import uuid

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
