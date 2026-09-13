"""Phase 10B.1 tests: Identity, Authentication, and Authorization Foundation.

Tests cover:
1. OAuth state generation, validation, expiration, single-use, and open-redirect defense.
2. Code exchange, user profile fetching, User + GitHubIdentity upserting, and session JWT issuance.
3. CodeForge session JWT encoding, decoding, validation, expiry, and inactive user rejection.
4. Authorization primitives: installation authorization, repository authorization, collaborator permissions, caching.
5. Cross-installation and revoked installation protections.
6. Transitioned Phase 10A APIs: unauthenticated 401, unauthorized 403/404, approver spoofing prevention.
7. Database constraints: task_github_links deduplication and tasks.creator_id linkage.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import (
    ApprovalStatusEnum,
    GitHubIdentity,
    GitHubInstallation,
    GitHubInstallationRepository,
    TaskApproval,
    TaskGitHubLink,
    TaskStatusEnum,
    User,
)
from app.db.repositories.task_repository import TaskRepository
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.main import create_app
from app.schemas.task import ExecutionTarget, TaskRequest
from app.services.auth_service import AuthService
from app.services.authorization_service import (
    AuthorizationService,
    set_collaborator_permission_resolver,
)
from app.services.task_service import TaskService

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def test_session():
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        future=True,
    )
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def auth_async_client(test_session: AsyncSession):
    app = create_app()

    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db_session] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ── 1. OAuth & Authentication Unit Tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_github_oauth_state():
    auth_service = AuthService()
    url, state = await auth_service.generate_oauth_url(
        redirect_uri="http://localhost:8000/api/v1/auth/github/callback"
    )

    assert state is not None
    assert len(state) >= 32
    assert "https://github.com/login/oauth/authorize" in url
    assert f"state={state}" in url

    # Consume state once
    consumed = await auth_service.verify_and_consume_state(state)
    assert consumed["redirect_uri"] == "http://localhost:8000/api/v1/auth/github/callback"

    # Single-use: Second consumption must fail
    with pytest.raises(HTTPException) as exc_info:
        await auth_service.verify_and_consume_state(state)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_invalid_oauth_state():
    auth_service = AuthService()
    with pytest.raises(HTTPException) as exc_info:
        await auth_service.verify_and_consume_state("non-existent-state-token")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_disallowed_redirect_uri():
    auth_service = AuthService()
    with pytest.raises(HTTPException) as exc_info:
        await auth_service.generate_oauth_url(
            redirect_uri="https://evil-phishing-site.com/steal-token"
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_jwt_issue_and_validate():
    auth_service = AuthService()
    user = User(
        id=str(uuid.uuid4()),
        display_name="Test Developer",
        email="dev@example.com",
        is_active=True,
    )
    token = auth_service.create_session_jwt(user, github_user_id=12345, github_login="octocat")
    assert token is not None

    payload = auth_service.decode_session_jwt(token)
    assert payload["sub"] == user.id
    assert payload["type"] == "session"
    assert payload["github_user_id"] == 12345
    assert payload["github_login"] == "octocat"
    assert payload["display_name"] == "Test Developer"


@pytest.mark.asyncio
async def test_expired_jwt():
    auth_service = AuthService()
    user = User(
        id=str(uuid.uuid4()),
        display_name="Expired User",
        email="expired@example.com",
        is_active=True,
    )

    # Manually craft an expired token
    now = datetime.now(UTC) - timedelta(hours=2)
    expired_payload = {
        "sub": user.id,
        "type": "session",
        "github_user_id": 111,
        "github_login": "olduser",
        "iat": int((now - timedelta(hours=1)).timestamp()),
        "exp": int(now.timestamp()),
    }
    expired_token = jwt.encode(
        expired_payload, auth_service.jwt_secret, algorithm=auth_service.jwt_algorithm
    )

    with pytest.raises(HTTPException) as exc_info:
        auth_service.decode_session_jwt(expired_token)
    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_wrong_token_type():
    auth_service = AuthService()
    now = datetime.now(UTC)
    wrong_payload = {
        "sub": "user-123",
        "type": "refresh_token",  # not session
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    token = jwt.encode(wrong_payload, auth_service.jwt_secret, algorithm=auth_service.jwt_algorithm)

    with pytest.raises(HTTPException) as exc_info:
        auth_service.decode_session_jwt(token)
    assert exc_info.value.status_code == 401
    assert "type" in exc_info.value.detail.lower()


# ── 2. Identity & Persistence Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_upsert(test_session: AsyncSession):
    repo = UserRepository(test_session)
    gh_id = random.randint(1_000_000, 999_999_999)
    login = f"test-coder-{uuid.uuid4().hex[:6]}"

    user1, identity1 = await repo.upsert_github_user(
        github_user_id=gh_id,
        github_login=login,
        display_name="Coder Initial",
        email="coder@initial.com",
        avatar_url="https://avatar.com/1.png",
    )
    await test_session.commit()

    assert user1.id is not None
    assert identity1.github_user_id == gh_id
    assert identity1.github_login == login

    # Second upsert with updated display name
    user2, identity2 = await repo.upsert_github_user(
        github_user_id=gh_id,
        github_login=f"{login}-updated",
        display_name="Coder Renamed",
        email="coder@updated.com",
        avatar_url="https://avatar.com/2.png",
    )
    await test_session.commit()

    # Must be the exact same user ID
    assert user2.id == user1.id
    assert identity2.id == identity1.id
    assert user2.display_name == "Coder Renamed"
    assert identity2.github_login == f"{login}-updated"


@pytest.mark.asyncio
async def test_github_identity_uniqueness(test_session: AsyncSession):
    repo = UserRepository(test_session)
    gh_id = random.randint(1_000_000, 999_999_999)

    user, identity = await repo.upsert_github_user(
        github_user_id=gh_id,
        github_login=f"unique-coder-{uuid.uuid4().hex[:6]}",
        display_name="Unique Coder",
    )
    await test_session.commit()

    # Attempt to manually create duplicate identity with same github_user_id
    duplicate_identity = GitHubIdentity(
        user_id=str(uuid.uuid4()),
        github_user_id=gh_id,
        github_login="duplicate-coder",
    )
    test_session.add(duplicate_identity)
    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()


# ── 3. Authorization Primitives Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_repository_authorization_matrix(test_session: AsyncSession):
    repo = UserRepository(test_session)
    auth_service = AuthorizationService(test_session)

    # 1. Setup GitHub Installation in DB
    install_id = random.randint(1_000_000, 999_999_999)
    repo_name = f"test-org/repo-{uuid.uuid4().hex[:8]}"

    inst = GitHubInstallation(
        installation_id=install_id,
        account_login=f"org-{uuid.uuid4().hex[:6]}",
        account_type="Organization",
        active=True,
    )
    test_session.add(inst)
    await test_session.flush()

    inst_repo = GitHubInstallationRepository(
        installation_id=install_id,
        repository=repo_name,
        authorized=True,
    )
    test_session.add(inst_repo)
    await test_session.flush()

    # 2. Setup 4 Users: Admin, Writer, Reader, and External
    admin_login = f"admin-{uuid.uuid4().hex[:6]}"
    writer_login = f"writer-{uuid.uuid4().hex[:6]}"
    reader_login = f"reader-{uuid.uuid4().hex[:6]}"
    external_login = f"external-{uuid.uuid4().hex[:6]}"

    admin_user, _ = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=admin_login,
        display_name="Admin",
    )
    writer_user, _ = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=writer_login,
        display_name="Writer",
    )
    reader_user, _ = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=reader_login,
        display_name="Reader",
    )
    external_user, _ = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=external_login,
        display_name="External",
    )
    await test_session.commit()

    # Mock collaborator resolver for testing permissions
    def mock_resolver(owner: str, repo: str, login: str) -> str:
        if login == admin_login:
            return "admin"
        if login == writer_login:
            return "write"
        if login == reader_login:
            return "read"
        return "none"

    set_collaborator_permission_resolver(mock_resolver)

    try:
        # Admin checks
        assert await auth_service.can_read_repository(admin_user, repo_name) is True
        assert await auth_service.can_write_repository(admin_user, repo_name) is True
        assert await auth_service.can_admin_repository(admin_user, repo_name) is True

        # Writer checks
        assert await auth_service.can_read_repository(writer_user, repo_name) is True
        assert await auth_service.can_write_repository(writer_user, repo_name) is True
        assert await auth_service.can_admin_repository(writer_user, repo_name) is False

        # Reader checks
        assert await auth_service.can_read_repository(reader_user, repo_name) is True
        assert await auth_service.can_write_repository(reader_user, repo_name) is False
        assert await auth_service.can_admin_repository(reader_user, repo_name) is False

        # External user checks
        assert await auth_service.can_read_repository(external_user, repo_name) is False
        assert await auth_service.can_write_repository(external_user, repo_name) is False
        assert await auth_service.can_admin_repository(external_user, repo_name) is False
    finally:
        set_collaborator_permission_resolver(None)


@pytest.mark.asyncio
async def test_cross_installation_and_revoked_repository(test_session: AsyncSession):
    repo = UserRepository(test_session)
    auth_service = AuthorizationService(test_session)

    inst_id_a = random.randint(1_000_000, 999_999_999)
    inst_id_b = random.randint(1_000_000, 999_999_999)
    org_a = f"org-a-{uuid.uuid4().hex[:6]}"
    org_b = f"org-b-{uuid.uuid4().hex[:6]}"
    repo_a = f"{org_a}/repo-a"
    repo_b = f"{org_b}/repo-b"
    repo_unauth = f"{org_a}/unauthorized-repo"

    # 1. Active installation on org-a
    inst_a = GitHubInstallation(
        installation_id=inst_id_a,
        account_login=org_a,
        account_type="Organization",
        active=True,
    )
    test_session.add(inst_a)
    inst_repo_a = GitHubInstallationRepository(
        installation_id=inst_id_a,
        repository=repo_a,
        authorized=True,
    )
    test_session.add(inst_repo_a)

    # 2. Inactive (revoked) installation on org-b
    inst_b = GitHubInstallation(
        installation_id=inst_id_b,
        account_login=org_b,
        account_type="Organization",
        active=False,  # REVOKED
    )
    test_session.add(inst_b)
    inst_repo_b = GitHubInstallationRepository(
        installation_id=inst_id_b,
        repository=repo_b,
        authorized=True,
    )
    test_session.add(inst_repo_b)

    # 3. Explicitly unauthorized repository on org-a
    inst_repo_unauth = GitHubInstallationRepository(
        installation_id=inst_id_a,
        repository=repo_unauth,
        authorized=False,
    )
    test_session.add(inst_repo_unauth)

    user, _ = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=f"dev-{uuid.uuid4().hex[:6]}",
        display_name="Dev",
    )
    await test_session.commit()

    # Even if user is admin, revoked/unauthorized repos must return False
    assert await auth_service.can_read_repository(user, repo_b) is False
    assert await auth_service.can_read_repository(user, repo_unauth) is False
    assert (
        await auth_service.can_read_repository(user, f"uninstalled-{uuid.uuid4().hex[:6]}/repo")
        is False
    )


# ── 4. Authenticated API Endpoints Tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_api_unauthenticated_requests_return_401(auth_async_client: AsyncClient):
    # GET /tasks without token
    res_list = await auth_async_client.get("/api/v1/tasks")
    assert res_list.status_code == 401

    # POST /tasks without token
    res_create = await auth_async_client.post(
        "/api/v1/tasks",
        json={"workspace_path": "/tmp/test", "description": "unauthenticated task test"},
    )
    assert res_create.status_code == 401

    # GET /tasks/{id} without token
    res_get = await auth_async_client.get(f"/api/v1/tasks/{uuid.uuid4()}")
    assert res_get.status_code == 401

    # POST /tasks/{id}/approve without token
    res_approve = await auth_async_client.post(f"/api/v1/tasks/{uuid.uuid4()}/approve")
    assert res_approve.status_code == 401


@pytest.mark.asyncio
async def test_api_inactive_user_rejected(
    auth_async_client: AsyncClient, test_session: AsyncSession
):
    repo = UserRepository(test_session)
    auth_service = AuthService()

    user, identity = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=f"inactive-{uuid.uuid4().hex[:6]}",
        display_name="Inactive Coder",
    )
    user.is_active = False
    await test_session.commit()

    token = auth_service.create_session_jwt(user, identity.github_user_id, identity.github_login)
    headers = {"Authorization": f"Bearer {token}"}

    res = await auth_async_client.get("/api/v1/tasks", headers=headers)
    assert res.status_code == 401
    assert "deactivated" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_api_authenticated_task_lifecycle(
    auth_async_client: AsyncClient, test_session: AsyncSession
):
    repo = UserRepository(test_session)
    auth_service = AuthService()

    # Setup active installation and authorized repo
    install_id = random.randint(1_000_000, 999_999_999)
    org_name = f"org-{uuid.uuid4().hex[:6]}"
    repo_name = f"{org_name}/lifecycle-repo"

    inst = GitHubInstallation(
        installation_id=install_id, account_login=org_name, account_type="Org", active=True
    )
    test_session.add(inst)
    inst_repo = GitHubInstallationRepository(
        installation_id=install_id, repository=repo_name, authorized=True
    )
    test_session.add(inst_repo)

    # Setup User A (Writer on repo) and User B (No access)
    login_a = f"coder-a-{uuid.uuid4().hex[:6]}"
    login_b = f"coder-b-{uuid.uuid4().hex[:6]}"
    user_a, id_a = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=login_a,
        display_name="Coder A",
    )
    user_b, id_b = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=login_b,
        display_name="Coder B",
    )
    await test_session.commit()

    token_a = auth_service.create_session_jwt(user_a, id_a.github_user_id, id_a.github_login)
    token_b = auth_service.create_session_jwt(user_b, id_b.github_user_id, id_b.github_login)

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Mock permission: login_a is write, login_b is none
    def mock_resolver(owner: str, repo: str, login: str) -> str:
        if login == login_a:
            return "write"
        return "none"

    set_collaborator_permission_resolver(mock_resolver)

    try:
        # 1. User B tries to create task on repo -> 403 Forbidden
        create_res_b = await auth_async_client.post(
            "/api/v1/tasks",
            json={
                "execution_target": "github",
                "github_repository": repo_name,
                "description": "Unauthorized task by user B",
            },
            headers=headers_b,
        )
        assert create_res_b.status_code == 403

        # 2. User A creates task on repo -> 200 OK
        create_res_a = await auth_async_client.post(
            "/api/v1/tasks",
            json={
                "execution_target": "github",
                "github_repository": repo_name,
                "description": "Authorized task by user A on github target",
                "require_plan_approval": True,
            },
            headers=headers_a,
        )
        assert create_res_a.status_code == 200
        task_id = create_res_a.json()["task_id"]

        # 3. User B tries to read task details -> 404 (not found to prevent enumeration)
        get_res_b = await auth_async_client.get(f"/api/v1/tasks/{task_id}", headers=headers_b)
        assert get_res_b.status_code == 404

        # 4. User A reads task details -> 200 OK
        get_res_a = await auth_async_client.get(f"/api/v1/tasks/{task_id}", headers=headers_a)
        assert get_res_a.status_code == 200
        assert get_res_a.json()["task_id"] == task_id

        # 5. Move task to WAITING_APPROVAL
        task_repo = TaskRepository(test_session)
        task_db = await task_repo.get_task(task_id)
        assert task_db is not None
        task_db.status = TaskStatusEnum.WAITING_APPROVAL.value
        app_rec = TaskApproval(
            task_id=task_id,
            status=ApprovalStatusEnum.PENDING.value,
            requested_by="worker",
        )
        await task_repo.create_approval(app_rec)
        await test_session.commit()

        # 6. User B attempts to approve -> 403 Forbidden
        approve_res_b = await auth_async_client.post(
            f"/api/v1/tasks/{task_id}/approve",
            json={"comment": "Malicious approval attempt"},
            headers=headers_b,
        )
        assert approve_res_b.status_code == 403

        # 7. User A approves task -> 200 OK
        approve_res_a = await auth_async_client.post(
            f"/api/v1/tasks/{task_id}/approve",
            json={"comment": "Approved by legitimate collaborator"},
            headers=headers_a,
        )
        assert approve_res_a.status_code == 200

        # Verify approver string in DB came from authenticated identity (login_a)
        await test_session.refresh(app_rec)
        assert app_rec.status == ApprovalStatusEnum.APPROVED.value
        assert app_rec.approved_by == login_a

    finally:
        set_collaborator_permission_resolver(None)


# ── 5. Database Constraint Tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_github_links_deduplication(test_session: AsyncSession):
    repo = UserRepository(test_session)
    task_service = TaskService(test_session)

    # Create task
    task = await task_service.create_task(
        TaskRequest(workspace_path="/tmp/test", description="Task link test")
    )
    await test_session.commit()

    inst_id = random.randint(1_000_000, 999_999_999)
    repo_id = random.randint(1_000_000, 999_999_999)
    comment_id = random.randint(1_000_000, 999_999_999)
    u_id = random.randint(1_000_000, 999_999_999)
    u_login = f"coder-{uuid.uuid4().hex[:6]}"

    link1 = TaskGitHubLink(
        task_id=task.task_id,
        installation_id=inst_id,
        repository_id=repo_id,
        repository_full_name="org/repo",
        issue_id=random.randint(1_000_000, 999_999_999),
        issue_number=10,
        trigger_comment_id=comment_id,
        triggering_github_user_id=u_id,
        triggering_github_login=u_login,
    )
    await repo.create_task_github_link(link1)
    await test_session.commit()

    # Attempt to insert duplicate link for same trigger comment
    task2 = await task_service.create_task(
        TaskRequest(workspace_path="/tmp/test", description="Task link test 2")
    )
    await test_session.commit()

    duplicate_link = TaskGitHubLink(
        task_id=task2.task_id,
        installation_id=inst_id,
        repository_id=repo_id,
        repository_full_name="org/repo",
        issue_id=random.randint(1_000_000, 999_999_999),
        issue_number=10,
        trigger_comment_id=comment_id,  # SAME trigger comment ID
        triggering_github_user_id=u_id,
        triggering_github_login=u_login,
    )
    test_session.add(duplicate_link)

    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()


@pytest.mark.asyncio
async def test_tasks_creator_id_linkage(test_session: AsyncSession):
    user_repo = UserRepository(test_session)
    task_service = TaskService(test_session)

    user, _ = await user_repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=f"creator-{uuid.uuid4().hex[:6]}",
        display_name="Creator",
    )
    await test_session.commit()

    task = await task_service.create_task(
        TaskRequest(workspace_path="/tmp/test", description="Task creator id test"),
        creator_id=user.id,
    )
    await test_session.commit()

    task_repo = TaskRepository(test_session)
    task_db = await task_repo.get_task(task.task_id)
    assert task_db is not None
    assert task_db.creator_id == user.id


@pytest.mark.asyncio
async def test_github_oauth_callback_flow(
    auth_async_client: AsyncClient, test_session: AsyncSession
):
    auth_service = AuthService()
    _, state = await auth_service.generate_oauth_url()

    gh_id = random.randint(1_000_000, 999_999_999)
    gh_login = f"oauth-{uuid.uuid4().hex[:6]}"
    gh_email = f"{gh_login}@example.com"

    mock_profile = {
        "github_user_id": gh_id,
        "github_login": gh_login,
        "display_name": "OAuth User",
        "email": gh_email,
        "avatar_url": "https://avatar.example.com/u.png",
    }

    with (
        patch.object(
            AuthService, "exchange_code_for_token", new=AsyncMock(return_value="gho_mock_token")
        ),
        patch.object(
            AuthService, "fetch_github_user_profile", new=AsyncMock(return_value=mock_profile)
        ),
    ):
        res = await auth_async_client.get(
            f"/api/v1/auth/github/callback?code=valid_code_123&state={state}"
        )
        assert res.status_code == 200
        data = res.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["github_login"] == gh_login
        assert data["user"]["email"] == gh_email


@pytest.mark.asyncio
async def test_api_get_me_endpoint(auth_async_client: AsyncClient, test_session: AsyncSession):
    repo = UserRepository(test_session)
    auth_service = AuthService()

    login = f"me-{uuid.uuid4().hex[:6]}"
    email = f"{login}@example.com"

    user, identity = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=login,
        display_name="Me User",
        email=email,
    )
    await test_session.commit()

    token = auth_service.create_session_jwt(user, identity.github_user_id, identity.github_login)
    headers = {"Authorization": f"Bearer {token}"}

    res = await auth_async_client.get("/api/v1/auth/me", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == user.id
    assert data["github_login"] == login
    assert data["email"] == email


@pytest.mark.asyncio
async def test_approver_cannot_be_spoofed(
    auth_async_client: AsyncClient, test_session: AsyncSession
):
    repo = UserRepository(test_session)
    auth_service = AuthService()

    install_id = random.randint(1_000_000, 999_999_999)
    org_name = f"org-{uuid.uuid4().hex[:6]}"
    repo_name = f"{org_name}/anti-spoof-repo"
    approver_login = f"real-approver-{uuid.uuid4().hex[:6]}"

    inst = GitHubInstallation(
        installation_id=install_id, account_login=org_name, account_type="Org", active=True
    )
    test_session.add(inst)
    inst_repo = GitHubInstallationRepository(
        installation_id=install_id, repository=repo_name, authorized=True
    )
    test_session.add(inst_repo)

    user, identity = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=approver_login,
        display_name="Real Approver",
    )
    await test_session.commit()

    token = auth_service.create_session_jwt(user, identity.github_user_id, identity.github_login)
    headers = {"Authorization": f"Bearer {token}"}

    def mock_resolver(owner: str, repo: str, login: str) -> str:
        if login == approver_login:
            return "admin"
        return "none"

    set_collaborator_permission_resolver(mock_resolver)

    try:
        # Create task
        task_service = TaskService(test_session)
        task = await task_service.create_task(
            TaskRequest(
                execution_target=ExecutionTarget.github,
                github_repository=repo_name,
                description="Anti-spoof task approval test",
                require_plan_approval=True,
            ),
            creator_id=user.id,
        )
        task_repo = TaskRepository(test_session)
        task.status = TaskStatusEnum.WAITING_APPROVAL.value
        app_rec = TaskApproval(
            task_id=task.task_id,
            status=ApprovalStatusEnum.PENDING.value,
            requested_by="worker",
        )
        await task_repo.create_approval(app_rec)
        await test_session.commit()

        # Attacker tries to submit a request attempting to specify approver in body
        res = await auth_async_client.post(
            f"/api/v1/tasks/{task.task_id}/approve",
            json={"comment": "Approved!", "approver": "impersonated_ceo"},
            headers=headers,
        )
        assert res.status_code == 200

        # Verify DB recorded real-approver from authenticated GitHub identity
        await test_session.refresh(app_rec)
        assert app_rec.approved_by == approver_login
        assert app_rec.approved_by != "impersonated_ceo"
    finally:
        set_collaborator_permission_resolver(None)


@pytest.mark.asyncio
async def test_user_id_unique_on_identity(test_session: AsyncSession):
    repo = UserRepository(test_session)
    user, _ = await repo.upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=f"user-{uuid.uuid4().hex[:6]}",
        display_name="User Base",
    )
    await test_session.commit()

    # Attempt to add second GitHubIdentity for same user_id
    second_identity = GitHubIdentity(
        user_id=user.id,
        github_user_id=random.randint(1_000_000, 999_999_999),
        github_login=f"user-{uuid.uuid4().hex[:6]}",
    )
    test_session.add(second_identity)
    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()
