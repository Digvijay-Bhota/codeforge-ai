"""Plan approval over the HTTP API, as the review console uses it.

A plan decision is bound to the approval request the reviewer was shown
(approval_id): a decision aimed at a request that is no longer the latest is
refused with 409 rather than applied to a plan nobody reviewed.  Approving a
plan never approves publication (CF-05).  Authorization is the API's: write
access to approve or reject, read access to see the plan.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    ApprovalStatusEnum,
    GitHubInstallation,
    GitHubInstallationRepository,
    Task,
    TaskApproval,
    TaskStatusEnum,
)
from app.db.repositories.task_repository import PUBLICATION_APPROVAL
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.main import create_app
from app.services.auth_service import AuthService
from app.services.authorization_service import set_collaborator_permission_resolver

PLAN = {
    "goal": "Fix the rounding of discounted totals",
    "summary": "Round once, after the discount",
    "risk_level": "low",
    "steps": [
        {
            "step_number": 1,
            "action": "modify",
            "description": "Compute the subtotal exactly",
            "affected_paths": ["billing/totals.py"],
            "rationale": "Per-line rounding loses cents",
            "dependencies": [],
        }
    ],
    "affected_files": ["billing/totals.py"],
    "tests_needed": ["test_discount_rounds_once"],
    "validation_strategy": "Run the billing tests",
    "assumptions": [],
    "risks": [],
}


class _PermissionCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest_asyncio.fixture(autouse=True)
async def permission_cache() -> Any:
    import app.services.queue_service as queue_service

    queue_service._redis_client = None
    with patch("app.services.authorization_service.get_redis_client", return_value=_PermissionCache()):
        yield
    client, queue_service._redis_client = queue_service._redis_client, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # nosec B110 - best-effort cleanup of a test client
            pass


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> Any:
    maker = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    app = create_app()

    async def override_get_db() -> Any:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _headers(session: AsyncSession, prefix: str) -> tuple[str, dict[str, str]]:
    login = f"{prefix}-{uuid.uuid4().hex[:6]}"
    user, identity = await UserRepository(session).upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999), github_login=login, display_name=prefix
    )
    await session.commit()
    token = AuthService().create_session_jwt(user, identity.github_user_id, identity.github_login)
    return login, {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def world(session: AsyncSession) -> Any:
    install_id = random.randint(1_000_000, 999_999_999)
    org = f"org-{uuid.uuid4().hex[:6]}"
    repo = f"{org}/billing"
    session.add(GitHubInstallation(installation_id=install_id, account_login=org, account_type="Org", active=True))
    session.add(GitHubInstallationRepository(installation_id=install_id, repository=repo, authorized=True))
    await session.commit()
    writer, writer_h = await _headers(session, "writer")
    reader, reader_h = await _headers(session, "reader")
    _, outsider_h = await _headers(session, "outsider")
    perms = {writer: "write", reader: "read"}
    set_collaborator_permission_resolver(lambda owner, name, login: perms.get(login, "none"))
    try:
        yield {"repo": repo, "writer": writer, "writer_h": writer_h, "reader_h": reader_h, "outsider_h": outsider_h}
    finally:
        set_collaborator_permission_resolver(None)


async def _waiting_task(session: AsyncSession, repo: str, *, expires_in: timedelta = timedelta(hours=24)) -> tuple[str, int]:
    task = Task(
        task_id=str(uuid.uuid4()),
        status=TaskStatusEnum.WAITING_APPROVAL.value,
        execution_target="github",
        repository=repo,
        requested_task="Fix the rounding of discounted totals",
        implementation_plan=PLAN,
        approval_config={"require_plan_approval": True},
    )
    session.add(task)
    await session.flush()
    approval = TaskApproval(
        task_id=task.task_id,
        approval_type="plan",
        status=ApprovalStatusEnum.PENDING.value,
        requested_by="worker",
        expires_at=datetime.now(UTC) + expires_in,
    )
    session.add(approval)
    await session.commit()
    return task.task_id, approval.id


async def _state(session: AsyncSession, task_id: str) -> tuple[str, list[tuple[str, str]]]:
    session.expire_all()
    task = (await session.execute(select(Task).where(Task.task_id == task_id))).scalar_one()
    approvals = (
        await session.execute(select(TaskApproval).where(TaskApproval.task_id == task_id).order_by(TaskApproval.id))
    ).scalars()
    return task.status, [(a.approval_type, a.status) for a in approvals]


@pytest.mark.asyncio
async def test_reviewers_see_the_plan_and_the_request_to_decide(session: AsyncSession, client: AsyncClient, world: Any) -> None:
    task_id, approval_id = await _waiting_task(session, world["repo"])
    res = await client.get(f"/api/v1/tasks/{task_id}", headers=world["reader_h"])
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "WAITING_APPROVAL"
    assert body["implementation_plan"]["steps"][0]["affected_paths"] == ["billing/totals.py"]
    assert body["latest_approval"]["id"] == approval_id
    assert body["latest_approval"]["status"] == "PENDING"
    assert body["latest_approval"]["expires_at"]


@pytest.mark.asyncio
async def test_approving_the_shown_request_queues_the_plan_and_nothing_else(
    session: AsyncSession, client: AsyncClient, world: Any
) -> None:
    task_id, approval_id = await _waiting_task(session, world["repo"])
    res = await client.post(
        f"/api/v1/tasks/{task_id}/approve", json={"approval_id": approval_id, "comment": "ok"}, headers=world["writer_h"]
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "QUEUED"
    status, approvals = await _state(session, task_id)
    assert status == "QUEUED"
    # Only the plan was approved: no publication approval exists or was granted.
    assert approvals == [("plan", "APPROVED")]
    assert all(kind != PUBLICATION_APPROVAL for kind, _ in approvals)
    detail = (await client.get(f"/api/v1/tasks/{task_id}", headers=world["writer_h"])).json()
    assert detail["latest_approval"]["approved_by"] == world["writer"]

    # Approving again is idempotent for the same request...
    again = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"approval_id": approval_id}, headers=world["writer_h"])
    assert again.status_code == 200
    # ...but a reject of it now conflicts with the recorded decision.
    late = await client.post(f"/api/v1/tasks/{task_id}/reject", json={"approval_id": approval_id}, headers=world["writer_h"])
    assert late.status_code == 409
    assert (await _state(session, task_id))[0] == "QUEUED"


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.asyncio
async def test_a_decision_on_a_superseded_request_is_refused(
    session: AsyncSession, client: AsyncClient, world: Any, decision: str
) -> None:
    task_id, old_id = await _waiting_task(session, world["repo"])
    # The plan was regenerated: a newer request replaced the one the reviewer saw.
    newer = TaskApproval(task_id=task_id, approval_type="plan", status="PENDING", requested_by="worker",
                         expires_at=datetime.now(UTC) + timedelta(hours=1))  # fmt: skip
    session.add(newer)
    await session.commit()
    before = await _state(session, task_id)

    for stale in (old_id, 999_999_999):
        res = await client.post(f"/api/v1/tasks/{task_id}/{decision}", json={"approval_id": stale}, headers=world["writer_h"])
        assert res.status_code == 409
        assert "no longer current" in res.json()["detail"]
    assert await _state(session, task_id) == before


@pytest.mark.asyncio
async def test_rejecting_cancels_the_task(session: AsyncSession, client: AsyncClient, world: Any) -> None:
    task_id, approval_id = await _waiting_task(session, world["repo"])
    res = await client.post(
        f"/api/v1/tasks/{task_id}/reject", json={"approval_id": approval_id, "reason": "wrong file"}, headers=world["writer_h"]
    )
    assert res.status_code == 200
    assert await _state(session, task_id) == ("CANCELLED", [("plan", "REJECTED")])
    late = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"approval_id": approval_id}, headers=world["writer_h"])
    assert late.status_code == 409


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.asyncio
async def test_readers_and_outsiders_cannot_decide(session: AsyncSession, client: AsyncClient, world: Any, decision: str) -> None:
    task_id, approval_id = await _waiting_task(session, world["repo"])
    reader = await client.post(f"/api/v1/tasks/{task_id}/{decision}", json={"approval_id": approval_id}, headers=world["reader_h"])
    assert reader.status_code == 403
    outsider = await client.post(f"/api/v1/tasks/{task_id}/{decision}", json={"approval_id": approval_id}, headers=world["outsider_h"])
    assert outsider.status_code == 403
    anonymous = await client.post(f"/api/v1/tasks/{task_id}/{decision}", json={"approval_id": approval_id})
    assert anonymous.status_code == 401
    assert await _state(session, task_id) == ("WAITING_APPROVAL", [("plan", "PENDING")])


@pytest.mark.asyncio
async def test_an_expired_request_cannot_be_approved(session: AsyncSession, client: AsyncClient, world: Any) -> None:
    task_id, approval_id = await _waiting_task(session, world["repo"], expires_in=timedelta(minutes=-1))
    res = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"approval_id": approval_id}, headers=world["writer_h"])
    assert res.status_code == 409
    status, approvals = await _state(session, task_id)
    assert status == "CANCELLED" and approvals == [("plan", "EXPIRED")]


@pytest.mark.parametrize("approval_id", [0, -1, "abc", 1.5])
@pytest.mark.asyncio
async def test_malformed_approval_ids_are_rejected(session: AsyncSession, client: AsyncClient, world: Any, approval_id: Any) -> None:
    task_id, _ = await _waiting_task(session, world["repo"])
    res = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"approval_id": approval_id}, headers=world["writer_h"])
    assert res.status_code == 422
    assert (await _state(session, task_id))[0] == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_omitting_approval_id_keeps_the_previous_behaviour(session: AsyncSession, client: AsyncClient, world: Any) -> None:
    task_id, _ = await _waiting_task(session, world["repo"])
    res = await client.post(f"/api/v1/tasks/{task_id}/approve", json={"comment": "ok"}, headers=world["writer_h"])
    assert res.status_code == 200
    assert (await _state(session, task_id))[0] == "QUEUED"
