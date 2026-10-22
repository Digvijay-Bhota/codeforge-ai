"""Hermetic end-to-end test: GitHub issue comment → plan → human approval → EXECUTION_UNAVAILABLE.

What runs for real (CodeForge's own code, unpatched):
  the FastAPI webhook route with HMAC verification; installation, repository
  and actor authorization (including the GitHub App JWT, scoped installation
  tokens and their verification, the repository-identity check and the
  collaborator-permission lookup, all over HTTP); duplicate-delivery
  protection; PostgreSQL persistence through the normal repositories; the
  outbox dispatcher and both consumers; the Redis job queue; the worker's
  process_job with its plan gate; SafeGitWrapper's hardened git clone; the
  repository analyst; the planner agent run by the OpenAI Agents SDK,
  structured-output parsing and plan validation; the acknowledgement comment;
  the approval API with its reviewer authorization and approval-ID binding;
  the execution-availability gate.

What is replaced, at the external boundaries only:
  * GitHub's REST API: tests/e2e/fake_github.py, a local HTTP server reached
    through the real ``settings.github_api_url`` setting.  It records every
    request; any branch, push, pull-request or check-run call is "unexpected".
  * github.com's git transport: the clone reads a local bare repository
    (``file://``), the convention of tests/security/test_git_hardening.py.
  * OpenAI: the SDK's default client (``set_default_openai_client``) answers
    from memory with a fixed, valid plan.  No API key, no network, no credit.

Nothing here is a real GitHub delivery or a real model call; that remains the
manual test in docs (the cf-gh-test stack).  Needs the test PostgreSQL and
Redis (CI provides both).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import subprocess  # nosec B404
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import httpx2
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from openai import AsyncOpenAI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents import set_default_openai_client
from agents.models import _openai_shared
from app.config import ExecutionMode, settings
from app.db.models import (
    GitHubInstallation,
    GitHubInstallationRepository,
    Job,
    ObservabilityEvent,
    OutboxEvent,
    Task,
    TaskApproval,
    TaskPublicationArtifact,
    TaskStatusEnum,
    WebhookDelivery,
)
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.github.git import SafeGitWrapper
from app.main import create_app
from app.orchestration import orchestrator as orchestrator_module
from app.outbox_dispatcher import dispatch_batch
from app.services.auth_service import AuthService
from app.services.authorization_service import set_collaborator_permission_resolver
from app.services.queue_service import QueueService
from app.worker import EXECUTION_UNAVAILABLE, process_job
from tests.e2e.fake_github import BOT_LOGIN, FakeGitHub, Repo, Served

WEBHOOK_SECRET = "e2e-webhook-secret-not-real"  # noqa: S105 - test value
INSTRUCTIONS = (
    'Add a short "Sandbox integration check" section to README.md explaining\n'
    "that this repository is used to validate CodeForge AI's planning workflow.\n"
    "Keep the proposed change documentation-only."
)
PLAN = {
    "goal": "Document the sandbox repository's purpose in README.md",
    "summary": "Add a 'Sandbox integration check' section to README.md; documentation only.",
    "risk_level": "low",
    "assumptions": ["README.md is the repository's main documentation"],
    "steps": [
        {"step_number": 1, "action": "modify", "description": "Add a 'Sandbox integration check' section to README.md",
         "affected_paths": ["README.md"], "rationale": "States that the repository validates CodeForge AI's planning",
         "dependencies": []},
    ],
    "affected_files": ["README.md"],
    "tests_needed": [],
    "validation_strategy": "Review the rendered README; no code changes.",
    "risks": ["None beyond wording"],
}  # fmt: skip


# ── Boundaries ─────────────────────────────────────────────────────────────────


class FakeModel:
    """OpenAI's Responses API, answered from memory with PLAN as structured output."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        # Instead of the plan: an OpenAI error (status, its "error" object), or other output text.
        self.error: tuple[int, dict[str, Any]] | None = None
        self.output_text: str = json.dumps(PLAN)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append({"url": str(request.url), "body": json.loads(request.content or b"{}")})
        if not str(request.url).endswith("/responses"):
            return httpx2.Response(404, json={"error": {"message": "unexpected endpoint"}})
        if self.error is not None:
            status, error = self.error
            return httpx2.Response(status, json={"error": error})
        return httpx2.Response(200, json={
            "id": f"resp_e2e_{len(self.requests)}", "object": "response", "created_at": 0, "status": "completed",
            "model": "gpt-4o", "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "output": [{"type": "message", "id": "msg_e2e", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": self.output_text, "annotations": []}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        })  # fmt: skip


def _git(cwd: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "f@example.com",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "f@example.com"}  # fmt: skip
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603


def make_remote(root: Path, owner: str, name: str) -> tuple[Path, str]:
    """A bare repository at root/owner/name.git with a README and one source file; returns it and its head."""
    work = root / "work"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text("# Sandbox\n\nA repository for testing CodeForge.\n")
    (work / "app.py").write_text("def hello() -> str:\n    return 'hello'\n")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "Initial commit")
    bare = root / owner / f"{name}.git"
    bare.parent.mkdir(parents=True)
    _git(root, "clone", "-q", "--bare", str(work), str(bare))
    return bare, _git(bare, "rev-parse", "HEAD")


# ── The world ──────────────────────────────────────────────────────────────────


class World:
    def __init__(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.owner = f"e2e-org-{suffix}"
        self.name = "sandbox"
        self.full_name = f"{self.owner}/{self.name}"
        self.installation_id = random.randint(10_000_000, 99_999_999)
        self.repository_id = random.randint(100_000_000, 999_999_999)
        self.users: dict[str, tuple[int, dict[str, str]]] = {}  # login -> (github id, auth headers)
        self.delivery_ids = (f"e2e-{suffix}-{n}" for n in range(1, 1000))
        self.comment_ids = iter(range(5_000_000, 6_000_000))

    def payload(self, login: str, body: str, *, repository: str | None = None, repository_id: int | None = None,
                installation_id: int | None = None, user_type: str = "User", pull_request_head: str | None = None) -> bytes:
        """A comment webhook; with *pull_request_head* (a commit SHA), on a pull request instead of an issue."""
        full_name = repository or self.full_name
        owner, name = full_name.split("/")
        github_id = self.users[login][0] if login in self.users else 4242
        issue: dict[str, Any] = {"number": 2, "title": "Docs", "body": "", "state": "open"}
        if pull_request_head is not None:
            issue["pull_request"] = {"head": {"sha": pull_request_head}}
        return json.dumps({
            "action": "created",
            "installation": {"id": installation_id or self.installation_id},
            "repository": {"id": repository_id or self.repository_id, "name": name, "full_name": full_name,
                           "owner": {"login": owner}, "default_branch": "main", "private": True},
            "issue": issue,
            "comment": {"id": next(self.comment_ids), "body": body,
                        "user": {"id": github_id, "login": login, "type": user_type}},
            "sender": {"id": github_id, "login": login, "type": user_type},
        }).encode()  # fmt: skip


def signature(raw: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture
async def e2e(session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    import app.github.git as git_module
    import app.github.token_service as token_service
    import app.services.queue_service as queue_service

    world = World()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()  # fmt: skip
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()  # fmt: skip
    fake = FakeGitHub(app_id="314159", public_key=public_pem)
    bare, head = make_remote(tmp_path / "remotes", world.owner, world.name)
    fake.add_repo(Repo(id=world.repository_id, owner=world.owner, name=world.name,
                       installation_id=world.installation_id, head_sha=head))  # fmt: skip
    clone_root = tmp_path / "clones"
    clone_root.mkdir()

    # Configuration, as an operator would set it, plus the two transport seams.
    monkeypatch.setattr(settings, "github_app_id", fake.app_id)
    monkeypatch.setattr(settings, "github_app_private_key", private_pem)
    monkeypatch.setattr(settings, "github_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "task_queue_name", f"e2e-queue-{uuid.uuid4().hex[:8]}")
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(clone_root))
    monkeypatch.setattr(git_module, "GITHUB_URL", f"file://{tmp_path / 'remotes'}/")
    monkeypatch.setattr(SafeGitWrapper, "ALLOWED_PROTOCOLS", ("file",))
    assert settings.codeforge_test_execution_mode == ExecutionMode.DISABLED  # the production default
    assert not settings.codeforge_allow_local_execution
    set_collaborator_permission_resolver(None)  # the real GitHub permission lookup
    monkeypatch.setattr(token_service, "_default_token_service", None)
    queue_service._redis_client = None

    model = FakeModel()
    previous_client = _openai_shared.get_default_openai_client()
    set_default_openai_client(
        AsyncOpenAI(api_key="sk-e2e-not-a-key", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(model.handle))),
        use_for_tracing=False,
    )

    maker = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    session.add(GitHubInstallation(installation_id=world.installation_id, account_login=world.owner,
                                   account_type="Organization", active=True))  # fmt: skip
    session.add(GitHubInstallationRepository(installation_id=world.installation_id, repository=world.full_name,
                                             authorized=True))  # fmt: skip
    await session.commit()
    for login, level in (("dev", "write"), ("lead", "write"), ("viewer", "read")):
        full_login = f"{login}-{world.owner[-8:]}"
        user, identity = await UserRepository(session).upsert_github_user(
            github_user_id=random.randint(1_000_000, 999_999_999), github_login=full_login, display_name=login)
        await session.commit()
        token = AuthService().create_session_jwt(user, identity.github_user_id, identity.github_login)
        world.users[login] = (identity.github_user_id, {"Authorization": f"Bearer {token}"})
        world.users[full_login] = world.users[login]
        fake.permissions[(world.full_name, full_login)] = level

    app = create_app()

    async def db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db_session] = db
    try:
        async with Served(fake):
            monkeypatch.setattr(settings, "github_api_url", fake.base_url)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://codeforge.test") as api:
                with patch("app.worker.async_session_maker", maker):
                    yield {"world": world, "fake": fake, "model": model, "api": api, "maker": maker,
                           "bare": bare, "head": head, "clone_root": clone_root}  # fmt: skip
    finally:
        _openai_shared._default_openai_client = previous_client  # the SDK has no unset
        client, queue_service._redis_client = queue_service._redis_client, None
        if client is not None:
            await client.aclose()


def login(world: World, who: str) -> str:
    return f"{who}-{world.owner[-8:]}"


async def deliver(env: dict[str, Any], raw: bytes, *, delivery_id: str | None = None, secret: str = WEBHOOK_SECRET) -> httpx.Response:
    world: World = env["world"]
    return await env["api"].post("/api/v1/github/webhooks", content=raw, headers={
        "X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": delivery_id or next(world.delivery_ids),
        "X-Hub-Signature-256": signature(raw, secret), "Content-Type": "application/json"})  # fmt: skip


async def dispatch_all(env: dict[str, Any]) -> int:
    """Run the real outbox dispatcher until nothing is left to publish."""
    total = 0
    async with env["maker"]() as s:
        while n := await dispatch_batch(session=s, queue=QueueService()):
            total += n
    return total


async def count(env: dict[str, Any], model: Any, *where: Any) -> int:
    async with env["maker"]() as s:
        return int((await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one())


async def the_task(env: dict[str, Any]) -> Task:
    async with env["maker"]() as s:
        return (await s.execute(select(Task).where(Task.repository == env["world"].full_name))).scalar_one()


async def run_queued_job(env: dict[str, Any]) -> int:
    """Take the next job from the real Redis queue and run it as the worker does."""
    job_id = await QueueService().dequeue(timeout=1)
    assert job_id is not None, "nothing was enqueued"
    await process_job(int(job_id))
    return int(job_id)


COMMAND = f"@codeforge implement\n\n{INSTRUCTIONS}"


# ── The full scenario ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_comment_to_plan_to_approval_stops_before_execution(e2e: dict[str, Any]) -> None:
    world: World = e2e["world"]
    fake: FakeGitHub = e2e["fake"]
    model: FakeModel = e2e["model"]
    raw = world.payload(login(world, "dev"), COMMAND)

    # 1. A forged signature is refused before anything is stored.
    forged = await deliver(e2e, raw, secret="not-the-secret")
    assert forged.status_code == 401
    assert await count(e2e, WebhookDelivery) == 0

    # 2. The signed comment is accepted and stored durably, once.
    delivery_id = next(world.delivery_ids)
    accepted = await deliver(e2e, raw, delivery_id=delivery_id)
    assert accepted.status_code == 202, accepted.text
    again = await deliver(e2e, raw, delivery_id=delivery_id)
    assert again.status_code == 200 and again.json()["reason"] == "duplicate delivery"
    assert await count(e2e, WebhookDelivery, WebhookDelivery.delivery_id == delivery_id) == 1
    assert await count(e2e, OutboxEvent, OutboxEvent.event_type == "GITHUB_COMMAND_INGESTED") == 1
    # The actor check asked GitHub, with an installation token minted from the App JWT.
    assert any(r.path.endswith(f"/collaborators/{login(world, 'dev')}/permission") for r in fake.requests)

    # 3. The dispatcher creates one task and one job, queues the job, and acknowledges on GitHub.
    await dispatch_all(e2e)
    task = await the_task(e2e)
    assert task.execution_target == "github" and task.approval_config == {"require_plan_approval": True}
    assert await count(e2e, Job, Job.task_id == task.task_id) == 1
    [ack] = fake.comment_bodies(world.full_name, 2)
    assert "`@codeforge implement`" in ack and INSTRUCTIONS in ack

    # 4. The App's own acknowledgement, delivered back as a comment, starts nothing.
    echo = await deliver(e2e, world.payload(BOT_LOGIN, ack, user_type="Bot"))
    assert echo.status_code == 200 and echo.json()["reason"] == "CodeForge managed comment"
    assert await count(e2e, OutboxEvent, OutboxEvent.event_type == "GITHUB_COMMAND_INGESTED") == 1

    # 5. The worker plans: real clone, real analysis, the planner agent (fake model), then waits.
    with (
        patch.object(SafeGitWrapper, "clone", autospec=True, side_effect=SafeGitWrapper.clone) as clones,
        patch.object(orchestrator_module, "run_coding_stage", wraps=orchestrator_module.run_coding_stage) as coding,
        patch.object(orchestrator_module, "run_testing_stage", wraps=orchestrator_module.run_testing_stage) as testing,
    ):
        await run_queued_job(e2e)
        await dispatch_all(e2e)
        task = await the_task(e2e)
        assert task.status == TaskStatusEnum.WAITING_APPROVAL.value, (task.failure_code, task.failure_reason)
        assert clones.call_count == 1
        assert task.implementation_plan["summary"] == PLAN["summary"]
        assert task.implementation_plan["affected_files"] == ["README.md"]
        [request] = model.requests  # exactly one model call, with the instructions and the cloned files
        prompt = json.dumps(request["body"])
        assert "Sandbox integration check" in prompt and "README.md" in prompt
        assert request["body"]["text"]["format"]["type"] == "json_schema"  # structured output, as in production

        async with e2e["maker"]() as s:
            [approval] = (await s.execute(select(TaskApproval).where(TaskApproval.task_id == task.task_id))).scalars().all()
        assert (approval.approval_type, approval.status) == ("plan", "PENDING")
        # Telemetry recorded during the job (e.g. each GitHub token mint) is committed with
        # the job, not left in an uncommitted transaction on a closed session.
        assert await count(e2e, ObservabilityEvent, ObservabilityEvent.task_id == task.task_id,
                           ObservabilityEvent.event_type == "GITHUB_OPERATION_COMPLETED") > 0  # fmt: skip

        # Nothing was written while the plan waits: the remote is unchanged, the clone is gone,
        # no changes were captured, and GitHub saw no write other than the acknowledgement.
        assert _git(e2e["bare"], "rev-parse", "HEAD") == e2e["head"]
        assert _git(e2e["bare"], "for-each-ref", "--format=%(refname)") == "refs/heads/main"
        assert list(e2e["clone_root"].iterdir()) == []
        assert await count(e2e, TaskPublicationArtifact) == 0
        assert fake.writes() == [] and fake.unexpected == []
        assert all(level == "read" for *_, perms in fake.minted for name, level in perms.items()
                   if name in ("contents", "pull_requests", "metadata"))  # fmt: skip

        # The console's view: the plan awaits approval, with the triggering issue and commenter.
        origin = {"issue_number": 2, "pull_request_number": None, "triggered_by": login(world, "dev")}
        detail = await e2e["api"].get(f"/api/v1/tasks/{task.task_id}", headers=world.users["lead"][1])
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "WAITING_APPROVAL" and detail.json()["github"] == origin
        assert detail.json()["latest_approval"]["id"] == approval.id
        listing = await e2e["api"].get("/api/v1/tasks", params={"repository": world.full_name},
                                       headers=world.users["lead"][1])  # fmt: skip
        assert [(t["task_id"], t["github"]) for t in listing.json()["tasks"]] == [(task.task_id, origin)]

        # 6. Reviewers: read access is refused; a stale approval ID is refused; nothing changes.
        url = f"/api/v1/tasks/{task.task_id}/approve"
        viewer = await e2e["api"].post(url, json={"approval_id": approval.id}, headers=world.users["viewer"][1])
        assert viewer.status_code == 403, viewer.text
        stale = await e2e["api"].post(url, json={"approval_id": approval.id + 1000}, headers=world.users["lead"][1])
        assert stale.status_code == 409, stale.text
        assert (await the_task(e2e)).status == TaskStatusEnum.WAITING_APPROVAL.value

        # 7. A writer approves exactly the request shown.
        approved = await e2e["api"].post(url, json={"approval_id": approval.id, "comment": "Looks right"},
                                         headers=world.users["lead"][1])  # fmt: skip
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == TaskStatusEnum.QUEUED.value

        # 8. The worker stops at the execution gate: no clone, no coding agent, no tests.
        await dispatch_all(e2e)
        await run_queued_job(e2e)
        await dispatch_all(e2e)
        assert clones.call_count == 1 and not coding.called and not testing.called
    task = await the_task(e2e)
    assert task.status == TaskStatusEnum.FAILED.value
    assert task.failure_code == EXECUTION_UNAVAILABLE
    assert "Nothing was run" in (task.failure_reason or "")
    assert len(model.requests) == 1

    # 9. Approving the plan approved nothing else: no publication request, no artifact,
    # no write-scoped token, no branch, push or pull request.
    async with e2e["maker"]() as s:
        approvals = (await s.execute(select(TaskApproval).where(TaskApproval.task_id == task.task_id))).scalars().all()
    assert [(a.approval_type, a.status, a.approved_by) for a in approvals] == [("plan", "APPROVED", login(world, "lead"))]
    assert await count(e2e, TaskPublicationArtifact) == 0
    assert not any(level == "write" and name in ("contents", "pull_requests")
                   for *_, perms in fake.minted for name, level in perms.items())  # fmt: skip
    assert fake.writes() == [] and fake.unexpected == []
    assert _git(e2e["bare"], "for-each-ref", "--format=%(refname)") == "refs/heads/main"
    assert await count(e2e, Task) == 1


# ── Refusals: nothing is created ───────────────────────────────────────────────


async def assert_nothing_created(env: dict[str, Any]) -> None:
    assert await count(env, OutboxEvent) == 0
    assert await count(env, Task) == 0
    assert await count(env, WebhookDelivery) == 0


@pytest.mark.asyncio
async def test_a_reader_cannot_start_a_task(e2e: dict[str, Any]) -> None:
    world: World = e2e["world"]
    response = await deliver(e2e, world.payload(login(world, "viewer"), COMMAND))
    assert response.status_code == 200 and response.json()["reason"] == "unauthorized actor"
    await assert_nothing_created(e2e)


@pytest.mark.asyncio
async def test_a_non_collaborator_cannot_start_a_task(e2e: dict[str, Any]) -> None:
    world: World = e2e["world"]
    world.users["outsider"] = (random.randint(1_000_000, 999_999_999), {})
    response = await deliver(e2e, world.payload("outsider", COMMAND))
    assert response.status_code == 200 and response.json()["reason"] == "unauthorized actor"
    await assert_nothing_created(e2e)


@pytest.mark.asyncio
async def test_an_unregistered_repository_cannot_start_a_task(e2e: dict[str, Any]) -> None:
    world: World = e2e["world"]
    other = f"{world.owner}/other"
    response = await deliver(e2e, world.payload(login(world, "dev"), COMMAND, repository=other, repository_id=7))
    assert response.status_code == 200 and response.json()["reason"] == "unauthorized repository"
    await assert_nothing_created(e2e)


@pytest.mark.asyncio
async def test_another_installation_cannot_start_a_task(e2e: dict[str, Any]) -> None:
    world: World = e2e["world"]
    response = await deliver(e2e, world.payload(login(world, "dev"), COMMAND, installation_id=world.installation_id + 1))
    assert response.status_code == 200 and response.json()["reason"] == "unauthorized installation"
    await assert_nothing_created(e2e)
