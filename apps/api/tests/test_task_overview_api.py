"""GET /tasks/summary, GET /tasks/{id}/activity and the multi-status task filter.

Runs against a freshly created schema (the conftest ``session`` fixture), so
every count below is exact.  GitHub repository permissions come from the
collaborator resolver override, as in test_phase10b1_auth.py.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    GitHubInstallation,
    GitHubInstallationRepository,
    Task,
    TaskApproval,
    TaskEvent,
    TaskGitHubLink,
    TaskStatusEnum,
    User,
)
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.main import create_app
from app.services import task_overview
from app.services.auth_service import AuthService
from app.services.authorization_service import set_collaborator_permission_resolver

S = TaskStatusEnum
NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_db_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def permissions() -> Iterator[dict[tuple[str, str], str]]:
    """(repository, login) -> GitHub permission; anything unlisted is "none"."""
    table: dict[tuple[str, str], str] = {}
    set_collaborator_permission_resolver(
        lambda owner, repo, login: table.get((f"{owner}/{repo}", login), "none")
    )
    yield table
    set_collaborator_permission_resolver(None)


class World:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.headers: dict[str, dict[str, str]] = {}
        self.users: dict[str, User] = {}
        self.logins: dict[str, str] = {}
        tag = uuid.uuid4().hex[:6]
        self.repo_a = f"org-{tag}/alpha"
        self.repo_b = f"org-{tag}/beta"

    async def setup(self, *names: str) -> None:
        installation_id = random.randint(1_000_000, 999_999_999)
        self.session.add(
            GitHubInstallation(
                installation_id=installation_id,
                account_login=self.repo_a.split("/")[0],
                account_type="Organization",
                active=True,
            )  # fmt: skip
        )
        for repo in (self.repo_a, self.repo_b):
            self.session.add(
                GitHubInstallationRepository(
                    installation_id=installation_id, repository=repo, authorized=True
                )
            )
        for name in names:
            login = f"{name}-{uuid.uuid4().hex[:6]}"
            user, identity = await UserRepository(self.session).upsert_github_user(
                github_user_id=random.randint(1_000_000, 999_999_999),
                github_login=login,
                display_name=name,
            )
            self.users[name], self.logins[name] = user, login
            token = AuthService().create_session_jwt(
                user, identity.github_user_id, identity.github_login
            )
            self.headers[name] = {"Authorization": f"Bearer {token}"}
        await self.session.commit()

    async def task(self, creator: str, status: S, *, repository: str | None = None, target: str = "github",
                   updated: datetime = NOW, parent: str | None = None, text: str = "Fix the bug") -> Task:  # fmt: skip
        task = Task(
            task_id=str(uuid.uuid4()),
            status=status.value,
            execution_target=target,
            repository=repository,
            requested_task=text,
            creator_id=self.users[creator].id,
            parent_task_id=parent,
            created_at=updated - timedelta(minutes=5),
            updated_at=updated,
        )
        self.session.add(task)
        await self.session.commit()
        return task

    async def event(self, task: Task, from_status: S | None, to_status: S, at: datetime,
                    message: str | None = None) -> None:  # fmt: skip
        self.session.add(
            TaskEvent(
                task_id=task.task_id,
                event_type=f"TASK_{to_status.value}",
                from_status=from_status and from_status.value,
                to_status=to_status.value,
                message=message,
                created_at=at,
            )  # fmt: skip
        )
        await self.session.commit()


@pytest_asyncio.fixture
async def world(session: AsyncSession, permissions: dict[tuple[str, str], str]) -> World:
    w = World(session)
    await w.setup("alice", "bob")
    permissions[(w.repo_a, w.logins["alice"])] = "write"
    permissions[(w.repo_a, w.logins["bob"])] = "write"
    permissions[(w.repo_b, w.logins["bob"])] = "write"
    return w


# ── Authentication ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/v1/tasks/summary", f"/api/v1/tasks/{uuid.uuid4()}/activity"]
)
async def test_requires_a_session(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401


# ── Overview ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_counts_exactly_the_tasks_the_caller_can_read(
    client: AsyncClient, world: World
) -> None:
    w = world
    await w.task("alice", S.COMPLETED, target="local")  # her own local task
    await w.task("bob", S.COMPLETED, target="local")  # someone else's local task: never visible
    await w.task("bob", S.PLANNING, repository=w.repo_a)  # readable repository
    await w.task("bob", S.PLANNING, repository=w.repo_b)  # repository she cannot read
    await w.task(
        "bob", S.COMPLETED, repository=w.repo_a, target="local"
    )  # local: creator-only despite the repo
    await w.task("bob", S.COMPLETED, repository=None)  # no repository: creator-only

    body = (await client.get("/api/v1/tasks/summary", headers=w.headers["alice"])).json()
    assert body["total"] == 2
    assert body["counts"] == {"COMPLETED": 1, "PLANNING": 1}
    assert body["repositories"] == [w.repo_a]
    assert body["repositories_truncated"] is False

    bob = (await client.get("/api/v1/tasks/summary", headers=w.headers["bob"])).json()
    assert bob["total"] == 5
    assert bob["counts"] == {"COMPLETED": 3, "PLANNING": 2}


@pytest.mark.asyncio
async def test_needs_attention_rules_and_order(client: AsyncClient, world: World) -> None:
    w = world
    old_failure = await w.task(
        "alice", S.FAILED, repository=w.repo_a, updated=NOW - timedelta(days=15)
    )
    retried = await w.task("alice", S.FAILED, repository=w.repo_a, updated=NOW - timedelta(hours=1))
    await w.task("alice", S.PLANNING, repository=w.repo_a, parent=retried.task_id)
    failure_late = await w.task(
        "alice", S.FAILED, repository=w.repo_a, updated=NOW - timedelta(minutes=1)
    )
    failure_early = await w.task(
        "alice", S.FAILED, repository=w.repo_a, updated=NOW - timedelta(days=2)
    )
    plan = await w.task(
        "bob", S.WAITING_APPROVAL, repository=w.repo_a, updated=NOW - timedelta(days=3)
    )
    publication = await w.task(
        "bob", S.WAITING_PUBLICATION, repository=w.repo_a, updated=NOW - timedelta(days=1)
    )
    await w.task("bob", S.WAITING_APPROVAL, repository=w.repo_b)  # not readable by alice
    await w.task("alice", S.CANCELLED, repository=w.repo_a)

    body = (await client.get("/api/v1/tasks/summary", headers=w.headers["alice"])).json()
    ids = [item["task_id"] for item in body["needs_attention"]]
    # Decisions first (most recently updated first), then failures not yet retried.
    assert ids == [publication.task_id, plan.task_id, failure_late.task_id, failure_early.task_id]
    assert old_failure.task_id not in ids and retried.task_id not in ids
    assert body["needs_attention_total"] == 4
    assert [item["attention"] for item in body["needs_attention"]] == [
        "publication_review",
        "plan_approval",
        "failed",
        "failed",
    ]
    assert body["failed_window_days"] == 14


@pytest.mark.asyncio
async def test_the_failure_window_counts_from_when_the_task_failed(
    client: AsyncClient, world: World, session: AsyncSession
) -> None:
    w = world
    # Failed 20 days ago; something touched the row yesterday (updated_at).
    old = await w.task("alice", S.FAILED, repository=w.repo_a, updated=NOW - timedelta(days=1))
    old.completed_at = NOW - timedelta(days=20)
    # Failed 2 days ago, untouched since.
    recent = await w.task("alice", S.FAILED, repository=w.repo_a, updated=NOW - timedelta(days=2))
    recent.completed_at = NOW - timedelta(days=2)
    await session.commit()
    for task, completed in ((old, NOW - timedelta(days=20)), (recent, NOW - timedelta(days=2))):
        # Committing bumped updated_at to now; put back the values under test.
        await session.execute(
            sa_update(Task)
            .where(Task.task_id == task.task_id)
            .values(updated_at=completed if task is recent else NOW - timedelta(days=1))
        )
    await session.commit()

    body = (await client.get("/api/v1/tasks/summary", headers=w.headers["alice"])).json()
    assert [item["task_id"] for item in body["needs_attention"]] == [recent.task_id]
    assert body["needs_attention"][0]["completed_at"] is not None


@pytest.mark.asyncio
async def test_needs_attention_list_is_bounded_but_the_total_is_exact(
    client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_overview, "NEEDS_ATTENTION_LIMIT", 2)
    for _ in range(3):
        await world.task("alice", S.WAITING_APPROVAL, repository=world.repo_a)
    body = (await client.get("/api/v1/tasks/summary", headers=world.headers["alice"])).json()
    assert len(body["needs_attention"]) == 2
    assert body["needs_attention_total"] == 3


@pytest.mark.asyncio
async def test_recent_activity_is_scoped_and_newest_first(
    client: AsyncClient, world: World
) -> None:
    w = world
    mine = await w.task("alice", S.PLANNING, repository=w.repo_a)
    hidden = await w.task("bob", S.PLANNING, repository=w.repo_b)
    await w.event(mine, S.PENDING, S.PLANNING, NOW - timedelta(minutes=2))
    await w.event(hidden, S.PENDING, S.PLANNING, NOW - timedelta(minutes=1))
    await w.event(mine, S.PLANNING, S.WAITING_APPROVAL, NOW)

    body = (await client.get("/api/v1/tasks/summary", headers=w.headers["alice"])).json()
    assert [(a["task_id"], a["to_status"]) for a in body["recent_activity"]] == [
        (mine.task_id, "WAITING_APPROVAL"),
        (mine.task_id, "PLANNING"),
    ]


@pytest.mark.asyncio
async def test_repositories_beyond_the_bound_are_reported(
    client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_overview, "MAX_SCOPE_REPOSITORIES", 1)
    body = (await client.get("/api/v1/tasks/summary", headers=world.headers["bob"])).json()
    assert body["repositories_truncated"] is True
    assert len(body["repositories"]) == 1


@pytest.mark.asyncio
async def test_repositories_github_could_not_confirm_are_flagged_but_not_named(
    client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overview says its counts may be incomplete without naming the
    repositories: one GitHub could not answer for may be a private repository
    the viewer has no access to."""
    from app.services.authorization_service import AuthorizationService

    w = world
    await w.task("bob", S.PLANNING, repository=w.repo_a)
    await w.task("bob", S.PLANNING, repository=w.repo_b)
    set_collaborator_permission_resolver(None)  # ask "GitHub", which fails for beta

    async def fetch(
        self: Any, installation_id: int, owner: str, repo: str, login: str, *args: Any
    ) -> str | None:
        return None if f"{owner}/{repo}" == w.repo_b else "write"

    monkeypatch.setattr(AuthorizationService, "_fetch_github_permission", fetch)
    # alice cannot read beta at all; GitHub failing must not reveal that it exists.
    response = await client.get("/api/v1/tasks/summary", headers=w.headers["alice"])
    body = response.json()
    assert body["repositories"] == [w.repo_a]
    assert body["repositories_unconfirmed"] is True
    assert (
        body["total"] == 1
    )  # beta's task is not counted, and the response says counts may be short
    assert w.repo_b not in response.text
    assert w.repo_b.split("/")[1] not in response.text


@pytest.mark.asyncio
async def test_an_outage_does_not_make_the_overview_ask_github_about_every_repository(
    client: AsyncClient, world: World, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown answers are never cached, and the page polls: without a limit, each
    request asked GitHub about every registered repository during an outage."""
    from sqlalchemy import select

    from app.services.authorization_service import AuthorizationService

    w = world
    set_collaborator_permission_resolver(None)
    asked: list[str] = []
    outage = {"on": False}

    async def fetch(
        self: Any, installation_id: int, owner: str, repo: str, login: str, *args: Any
    ) -> str | None:
        asked.append(f"{owner}/{repo}")
        return None if outage["on"] else "write"

    monkeypatch.setattr(AuthorizationService, "_fetch_github_permission", fetch)
    # Before the outage: alpha's answer is cached.  beta is a separate case below.
    assert (await client.get("/api/v1/tasks/summary", headers=w.headers["bob"])).status_code == 200
    installation = (
        await session.execute(
            select(GitHubInstallationRepository.installation_id).where(
                GitHubInstallationRepository.repository == w.repo_a
            )
        )
    ).scalar_one()
    owner = w.repo_a.split("/")[0]
    late = [f"{owner}/{name}" for name in ("aa-1", "aa-2", "aa-3", "aa-4")]  # sorted before alpha
    for name in late:
        session.add(
            GitHubInstallationRepository(
                installation_id=installation, repository=name, authorized=True
            )
        )
    await session.commit()

    outage["on"], asked[:] = True, []
    body = (await client.get("/api/v1/tasks/summary", headers=w.headers["bob"])).json()
    # The first unanswered ask pauses GitHub for that installation (all four share it).
    assert asked == late[:1]
    assert body["repositories_unconfirmed"] is True
    assert w.repo_a in body["repositories"]  # still answered from the cache


@pytest.mark.asyncio
async def test_an_empty_account_gets_zeroes(client: AsyncClient, world: World) -> None:
    body = (await client.get("/api/v1/tasks/summary", headers=world.headers["alice"])).json()
    assert body["total"] == 0 and body["counts"] == {} and body["needs_attention"] == []
    assert body["recent_activity"] == []


# ── Activity ───────────────────────────────────────────────────────────────


async def approval(session: AsyncSession, task: Task, kind: str, status: str, requested: datetime,
                   responded: datetime | None = None, by: str | None = None, comment: str | None = None) -> TaskApproval:  # fmt: skip
    row = TaskApproval(task_id=task.task_id, approval_type=kind, status=status, requested_at=requested,
                       responded_at=responded, approved_by=by, comment=comment)  # fmt: skip
    session.add(row)
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_activity_is_the_recorded_history_in_order(
    client: AsyncClient, world: World, session: AsyncSession
) -> None:
    w = world
    t0 = NOW - timedelta(hours=1)
    task = await w.task("bob", S.FAILED, repository=w.repo_a, updated=t0 + timedelta(minutes=20))
    task.created_at = t0
    session.add(
        TaskGitHubLink(
            task_id=task.task_id,
            installation_id=1,
            repository_id=101,
            repository_full_name=w.repo_a,
            issue_number=7,
            triggering_github_user_id=4242,
            triggering_github_login="octo-reviewer",
        )  # fmt: skip
    )
    await session.commit()
    await w.event(task, S.PENDING, S.QUEUED, t0 + timedelta(minutes=1))
    await w.event(task, S.PLANNING, S.WAITING_APPROVAL, t0 + timedelta(minutes=2))
    plan = await approval(session, task, "PLAN", "APPROVED", t0 + timedelta(minutes=2),
                          t0 + timedelta(minutes=5), by="octo-reviewer", comment="token ghp_abcdefghijklmnopqrstuvwx ok")  # fmt: skip
    await w.event(task, S.WAITING_APPROVAL, S.QUEUED, t0 + timedelta(minutes=5))
    await w.event(task, S.QUEUED, S.FAILED, t0 + timedelta(minutes=6),
                  message="push failed: https://x-access-token:s3cr3tvalue@github.com/o/r")  # fmt: skip
    retry = await w.task(
        "alice",
        S.PLANNING,
        repository=w.repo_a,
        parent=task.task_id,
        updated=t0 + timedelta(minutes=12),
    )

    response = await client.get(
        f"/api/v1/tasks/{task.task_id}/activity", headers=w.headers["alice"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is False and body["truncated"] is False
    kinds = [(i["kind"], i.get("to_status"), i.get("approval_status")) for i in body["items"]]
    assert kinds == [
        ("created", "PENDING", None),
        ("status", "QUEUED", None),
        ("status", "WAITING_APPROVAL", None),
        ("approval_requested", None, "PENDING"),
        # The decision before the change it caused, although both were recorded at minute 5.
        ("approval_decided", None, "APPROVED"),
        ("status", "QUEUED", None),
        ("status", "FAILED", None),
        ("retried", None, None),
    ]
    created, decided, failed, retried = (
        body["items"][0],
        body["items"][4],
        body["items"][6],
        body["items"][7],
    )
    assert created["actor"] == "octo-reviewer" and created["github"]["issue_number"] == 7
    assert decided["actor"] == "octo-reviewer" and decided["approval_id"] == plan.id
    assert decided["approval_type"] == "plan"
    assert "ghp_abcdefghijklmnopqrstuvwx" not in decided["message"]
    assert "s3cr3tvalue" not in failed["message"]
    assert retried["related_task_id"] == retry.task_id


@pytest.mark.asyncio
async def test_an_expired_approval_has_no_actor(
    client: AsyncClient, world: World, session: AsyncSession
) -> None:
    task = await world.task("alice", S.CANCELLED, target="local")
    await approval(
        session, task, "PUBLICATION", "EXPIRED", NOW - timedelta(days=1), NOW, by="someone"
    )
    body = (
        await client.get(f"/api/v1/tasks/{task.task_id}/activity", headers=world.headers["alice"])
    ).json()
    decided = [i for i in body["items"] if i["kind"] == "approval_decided"]
    assert len(decided) == 1 and decided[0]["actor"] is None
    assert (
        decided[0]["approval_type"] == "publication" and decided[0]["approval_status"] == "EXPIRED"
    )


@pytest.mark.asyncio
async def test_a_publication_decision_precedes_publishing(
    client: AsyncClient, world: World, session: AsyncSession
) -> None:
    w = world
    t0 = NOW - timedelta(hours=1)
    task = await w.task("alice", S.PUBLISHING, repository=w.repo_a, updated=t0)
    await w.event(task, S.TESTING, S.WAITING_PUBLICATION, t0)
    await approval(
        session, task, "PUBLICATION", "APPROVED", t0, t0 + timedelta(minutes=3), by="ada"
    )
    await w.event(task, S.WAITING_PUBLICATION, S.PUBLISHING, t0 + timedelta(minutes=3))
    body = (
        await client.get(f"/api/v1/tasks/{task.task_id}/activity", headers=w.headers["alice"])
    ).json()
    assert [(i["kind"], i["to_status"] or i["approval_status"]) for i in body["items"][-2:]] == [
        ("approval_decided", "APPROVED"),
        ("status", "PUBLISHING"),
    ]


@pytest.mark.asyncio
async def test_a_decision_recorded_long_after_a_change_stays_where_it_was_recorded(
    client: AsyncClient, world: World, session: AsyncSession
) -> None:
    # An expiry noticed an hour after the task was cancelled did not cause the
    # cancellation, so it is not moved before it.
    w = world
    t0 = NOW - timedelta(hours=2)
    task = await w.task("alice", S.CANCELLED, target="local", updated=t0)
    await w.event(task, S.PLANNING, S.WAITING_APPROVAL, t0)
    await approval(session, task, "PLAN", "EXPIRED", t0, t0 + timedelta(hours=1))
    await w.event(task, S.WAITING_APPROVAL, S.CANCELLED, t0 + timedelta(minutes=5))
    body = (
        await client.get(f"/api/v1/tasks/{task.task_id}/activity", headers=w.headers["alice"])
    ).json()
    assert [(i["kind"], i["to_status"] or i["approval_status"]) for i in body["items"][-2:]] == [
        ("status", "CANCELLED"),
        ("approval_decided", "EXPIRED"),
    ]


@pytest.mark.asyncio
async def test_history_that_was_never_recorded_is_reported_as_partial(
    client: AsyncClient, world: World
) -> None:
    w = world
    no_events = await w.task("alice", S.FAILED, target="local")
    late_start = await w.task("alice", S.COMPLETED, target="local")
    await w.event(late_start, S.TESTING, S.COMPLETED, NOW)
    pending = await w.task("alice", S.PENDING, target="local")

    first = (
        await client.get(f"/api/v1/tasks/{no_events.task_id}/activity", headers=w.headers["alice"])
    ).json()
    assert first["partial"] is True and "No status changes were recorded" in first["partial_reason"]
    second = (
        await client.get(f"/api/v1/tasks/{late_start.task_id}/activity", headers=w.headers["alice"])
    ).json()
    assert second["partial"] is True and "TESTING" in second["partial_reason"]
    third = (
        await client.get(f"/api/v1/tasks/{pending.task_id}/activity", headers=w.headers["alice"])
    ).json()
    assert third["partial"] is False and [i["kind"] for i in third["items"]] == ["created"]


@pytest.mark.asyncio
async def test_long_histories_are_truncated_and_say_so(
    client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_overview, "ACTIVITY_EVENT_LIMIT", 2)
    task = await world.task("alice", S.PLANNING, target="local")
    for i, (a, b) in enumerate(
        [(S.PENDING, S.QUEUED), (S.QUEUED, S.ANALYZING), (S.ANALYZING, S.PLANNING)]
    ):
        await world.event(task, a, b, NOW - timedelta(minutes=10 - i))
    body = (
        await client.get(f"/api/v1/tasks/{task.task_id}/activity", headers=world.headers["alice"])
    ).json()
    assert body["truncated"] is True
    assert [i["to_status"] for i in body["items"] if i["kind"] == "status"] == [
        "QUEUED",
        "ANALYZING",
    ]


@pytest.mark.asyncio
async def test_activity_follows_task_read_access(client: AsyncClient, world: World) -> None:
    w = world
    bobs_local = await w.task("bob", S.COMPLETED, target="local")
    unreadable = await w.task("bob", S.COMPLETED, repository=w.repo_b)
    for task in (bobs_local, unreadable):
        response = await client.get(
            f"/api/v1/tasks/{task.task_id}/activity", headers=w.headers["alice"]
        )
        assert response.status_code == 404
        assert (
            await client.get(f"/api/v1/tasks/{task.task_id}/activity", headers=w.headers["bob"])
        ).status_code == 200
    missing = await client.get(f"/api/v1/tasks/{uuid.uuid4()}/activity", headers=w.headers["alice"])
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_retries_the_viewer_cannot_read_are_left_out(
    client: AsyncClient, world: World
) -> None:
    w = world
    local = await w.task("alice", S.FAILED, target="local")
    # Another user's task naming it as parent (not something the API creates, but
    # the scope must not depend on that).
    await w.task("bob", S.PLANNING, target="local", parent=local.task_id)
    elsewhere = await w.task("bob", S.FAILED, repository=w.repo_a)
    await w.task("bob", S.PLANNING, repository=w.repo_b, parent=elsewhere.task_id)

    for task in (local, elsewhere):
        body = (
            await client.get(f"/api/v1/tasks/{task.task_id}/activity", headers=w.headers["alice"])
        ).json()
        assert [i for i in body["items"] if i["kind"] == "retried"] == []


@pytest.mark.asyncio
async def test_a_deleted_parent_leaves_no_dangling_link(
    client: AsyncClient, world: World, session: AsyncSession
) -> None:
    parent = await world.task("alice", S.FAILED, target="local")
    child = await world.task("alice", S.PLANNING, target="local", parent=parent.task_id)
    await session.delete(parent)
    await session.commit()
    await session.refresh(child)
    body = (
        await client.get(f"/api/v1/tasks/{child.task_id}/activity", headers=world.headers["alice"])
    ).json()
    assert body["items"][0]["related_task_id"] is None


# ── Task list status filter ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_filters_by_several_statuses(client: AsyncClient, world: World) -> None:
    w = world
    failed = await w.task("alice", S.FAILED, target="local")
    cancelled = await w.task("alice", S.CANCELLED, target="local")
    await w.task("alice", S.COMPLETED, target="local")

    async def listed(query: str) -> Any:
        response = await client.get(f"/api/v1/tasks?{query}", headers=w.headers["alice"])
        return response.status_code, response.json()

    status, body = await listed("status=FAILED,cancelled")
    assert status == 200 and body["total"] == 2
    assert {t["task_id"] for t in body["tasks"]} == {failed.task_id, cancelled.task_id}
    status, body = await listed("status=")
    assert status == 200 and body["total"] == 3
    status, body = await listed("status=FAILED,BOGUS")
    assert status == 422 and "BOGUS" in body["detail"]


@pytest.mark.asyncio
async def test_repository_list_filters_by_several_statuses(
    client: AsyncClient, world: World
) -> None:
    w = world
    await w.task("bob", S.WAITING_APPROVAL, repository=w.repo_a)
    await w.task("bob", S.WAITING_PUBLICATION, repository=w.repo_a)
    await w.task("bob", S.CODING, repository=w.repo_a)
    response = await client.get(
        "/api/v1/tasks",
        params={"repository": w.repo_a, "status": "WAITING_APPROVAL,WAITING_PUBLICATION"},
        headers=w.headers["alice"],
    )
    assert response.status_code == 200 and response.json()["total"] == 2


# ── Task list: every task you can read ─────────────────────────────────────


async def mixed_world(w: World) -> dict[str, Task]:
    """Tasks alice may and may not read, in several statuses."""
    return {
        "own_local": await w.task("alice", S.COMPLETED, target="local"),
        "readable_failed": await w.task("bob", S.FAILED, repository=w.repo_a),
        "readable_waiting": await w.task("bob", S.WAITING_APPROVAL, repository=w.repo_a),
        "readable_mine": await w.task("alice", S.CODING, repository=w.repo_a),
        "other_local": await w.task("bob", S.COMPLETED, target="local"),
        "unreadable_repo": await w.task("bob", S.FAILED, repository=w.repo_b),
        "local_with_repo": await w.task("bob", S.COMPLETED, repository=w.repo_a, target="local"),
        "no_repo": await w.task("bob", S.COMPLETED, repository=None),
    }


@pytest.mark.asyncio
async def test_list_every_task_you_can_read(client: AsyncClient, world: World) -> None:
    tasks = await mixed_world(world)
    response = await client.get(
        "/api/v1/tasks", params={"scope": "readable"}, headers=world.headers["alice"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    expected = {"own_local", "readable_failed", "readable_waiting", "readable_mine"}
    assert {t["task_id"] for t in body["tasks"]} == {tasks[k].task_id for k in expected}
    assert body["total"] == 4
    assert body["repositories_truncated"] is False and body["repositories_unconfirmed"] is False

    # The default stays "tasks you created".
    mine = (await client.get("/api/v1/tasks", headers=world.headers["alice"])).json()
    assert {t["task_id"] for t in mine["tasks"]} == {
        tasks["own_local"].task_id,
        tasks["readable_mine"].task_id,
    }


@pytest.mark.asyncio
async def test_list_totals_match_the_overview_counts(client: AsyncClient, world: World) -> None:
    """A number on the overview and the list it links to are the same set."""
    await mixed_world(world)
    for who in ("alice", "bob"):
        overview = (await client.get("/api/v1/tasks/summary", headers=world.headers[who])).json()
        for status in TaskStatusEnum:
            listed = (
                await client.get(
                    "/api/v1/tasks",
                    params={"scope": "readable", "status": status.value},
                    headers=world.headers[who],
                )
            ).json()
            assert listed["total"] == overview["counts"].get(status.value, 0), (who, status)
        everything = (
            await client.get(
                "/api/v1/tasks", params={"scope": "readable"}, headers=world.headers[who]
            )
        ).json()
        assert everything["total"] == overview["total"]


@pytest.mark.asyncio
async def test_readable_list_pages_newest_first(client: AsyncClient, world: World) -> None:
    created = [
        await world.task("alice", S.COMPLETED, target="local", updated=NOW - timedelta(hours=h))
        for h in range(5)
    ]
    seen: list[str] = []
    for offset in (0, 2, 4):
        page = (
            await client.get(
                "/api/v1/tasks",
                params={"scope": "readable", "limit": 2, "offset": offset},
                headers=world.headers["alice"],
            )
        ).json()
        assert page["total"] == 5
        seen += [t["task_id"] for t in page["tasks"]]
    assert seen == [t.task_id for t in created]  # newest first, no gaps or repeats


@pytest.mark.asyncio
async def test_a_repository_filter_wins_and_an_unknown_scope_is_refused(
    client: AsyncClient, world: World
) -> None:
    tasks = await mixed_world(world)
    in_repo = (
        await client.get(
            "/api/v1/tasks",
            params={"scope": "readable", "repository": world.repo_a},
            headers=world.headers["alice"],
        )
    ).json()
    assert {t["task_id"] for t in in_repo["tasks"]} == {
        tasks[k].task_id for k in ("readable_failed", "readable_waiting", "readable_mine")
    }
    bad = await client.get(
        "/api/v1/tasks", params={"scope": "everyone"}, headers=world.headers["alice"]
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_readable_list_says_when_tasks_may_be_missing_without_naming_repositories(
    client: AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.authorization_service import AuthorizationService

    await mixed_world(world)
    set_collaborator_permission_resolver(None)

    async def fetch(
        self: Any, installation_id: int, owner: str, repo: str, login: str, *args: Any
    ) -> str | None:
        return None if f"{owner}/{repo}" == world.repo_b else "write"

    monkeypatch.setattr(AuthorizationService, "_fetch_github_permission", fetch)
    response = await client.get(
        "/api/v1/tasks", params={"scope": "readable"}, headers=world.headers["alice"]
    )
    assert response.json()["repositories_unconfirmed"] is True
    assert world.repo_b not in response.text


async def _register(session: AsyncSession, world: World, names: list[str]) -> int:
    from sqlalchemy import select

    installation = (
        await session.execute(
            select(GitHubInstallationRepository.installation_id).where(
                GitHubInstallationRepository.repository == world.repo_a
            )
        )
    ).scalar_one()
    for name in names:
        session.add(GitHubInstallationRepository(installation_id=installation, repository=name, authorized=True))
    await session.commit()
    return int(installation)


@pytest.mark.asyncio
async def test_polling_overviews_during_an_outage_ask_github_once_per_pause(
    client: AsyncClient, world: World, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every overview request (the page polls every 5 s, several tabs or users
    at once) used to ask GitHub again during an outage, each ask taking up to
    ~40 s with the client's retries.  After an unanswered ask, the overview
    leaves that installation's GitHub alone for a short pause."""
    from app.services.authorization_service import AuthorizationService
    from app.services.queue_service import get_redis_client

    w = world
    set_collaborator_permission_resolver(None)
    asked: list[str] = []

    async def down(self: Any, installation_id: int, owner: str, repo: str, *args: Any) -> str | None:
        asked.append(f"{owner}/{repo}")
        return None

    monkeypatch.setattr(AuthorizationService, "_fetch_github_permission", down)
    owner = w.repo_a.split("/")[0]
    installation = await _register(session, w, [f"{owner}/aa-{n}" for n in range(4)])

    for _request in range(5):
        body = (await client.get("/api/v1/tasks/summary", headers=w.headers["bob"])).json()
        assert body["repositories_unconfirmed"] is True and body["repositories"] == []  # unknown never grants
        assert body["total"] == 0
    assert len(asked) == 1, asked

    # The pause is over: GitHub is asked again (once).
    await get_redis_client().delete(task_overview.github_pause_key(installation))
    await client.get("/api/v1/tasks/summary", headers=w.headers["bob"])
    assert len(asked) == 2


@pytest.mark.asyncio
async def test_a_cached_overview_does_not_query_per_repository(
    client: AsyncClient, world: World, session: AsyncSession
) -> None:
    from sqlalchemy import event

    w = world
    owner = w.repo_a.split("/")[0]
    await _register(session, w, [f"{owner}/many-{n:02d}" for n in range(20)])
    first = (await client.get("/api/v1/tasks/summary", headers=w.headers["bob"])).json()  # warms the cache

    statements: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    engine = session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", count)
    try:
        again = (await client.get("/api/v1/tasks/summary", headers=w.headers["bob"])).json()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert again["repositories"] == first["repositories"] and again["counts"] == first["counts"]
    assert len(statements) <= 15, f"{len(statements)} statements for 22 cached repositories"
