"""GitHub failing during planning: the task ends clearly, nothing runs, and a retry recovers.

Same harness as test_comment_to_approval.py (real CodeForge code, GitHub
replaced by a local HTTP server), with faults injected into that server: the
real token service and GitHubClient see real 502, 503, 429 and 404 responses.

A GitHub outage or rate limit used to end the task as POLICY_REFUSED ("refused
by policy") or REPOSITORY_RESOLUTION_FAILED ("repository not found"), and a
429 while minting a token was not even treated as transient.  The contract is
unchanged: a failed plan is not retried automatically; the reviewer retries.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.db.models import TaskStatusEnum
from app.orchestration import failures
from tests.e2e.fake_github import Fault
from tests.e2e.test_comment_to_approval import (  # noqa: F401
    COMMAND,
    deliver,
    dispatch_all,
    e2e,
    login,
    run_queued_job,
    the_task,
)


async def queued(env: dict[str, Any]) -> None:
    """A signed comment accepted, dispatched and acknowledged; its planning job waits in the queue."""
    world = env["world"]
    response = await deliver(env, world.payload(login(world, "dev"), COMMAND))
    assert response.status_code == 202, response.text
    await dispatch_all(env)
    assert len(env["fake"].comment_bodies(world.full_name, 2)) == 1  # acknowledged


async def plan_with(env: dict[str, Any], *faults: Fault) -> Any:
    env["fake"].faults.extend(faults)
    before = len(env["fake"].requests)
    await run_queued_job(env)
    env["planning_requests"] = env["fake"].requests[before:]  # the worker's requests only
    # Status updates go out too (they may fail and be retried while the fault lasts).
    await dispatch_all(env)
    return await the_task(env)


def assert_nothing_ran(env: dict[str, Any]) -> None:
    assert env["model"].requests == []  # no model call
    assert list(env["clone_root"].iterdir()) == []  # nothing cloned, or cleaned up
    assert env["fake"].writes() == [] and env["fake"].unexpected == []


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client's retries still run; only the waits between them are skipped.
    monkeypatch.setattr(settings, "github_client_retry_backoff_factor", 0.0)


@pytest.mark.asyncio
async def test_a_github_outage_during_repository_verification_is_github_unavailable(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    await queued(e2e)
    repo_path = rf"/repos/{e2e['world'].full_name}"
    task = await plan_with(e2e, Fault("GET", repo_path, 502))

    assert task.status == TaskStatusEnum.FAILED.value
    assert task.failure_code == failures.GITHUB_UNAVAILABLE, task.failure_reason
    assert "GitHub is unavailable" in task.failure_reason and "502" in task.failure_reason
    # The client retried (1 + github_client_max_retries attempts) before giving up.
    attempts = [r for r in e2e["planning_requests"] if r.method == "GET" and r.path == repo_path]
    assert len(attempts) == 1 + settings.github_client_max_retries
    assert_nothing_ran(e2e)


@pytest.mark.asyncio
async def test_a_rate_limited_token_mint_is_github_unavailable(e2e: dict[str, Any], no_backoff: None) -> None:  # noqa: F811
    import app.github.token_service as token_service

    await queued(e2e)
    token_service._default_token_service = None  # no cached grants: planning must mint
    task = await plan_with(
        e2e, Fault("POST", r"/app/installations/\d+/access_tokens", 429, headers={"retry-after": "60"})
    )
    assert task.failure_code == failures.GITHUB_UNAVAILABLE, task.failure_reason
    assert "429" in task.failure_reason
    assert_nothing_ran(e2e)


@pytest.mark.asyncio
async def test_an_outage_on_the_branch_lookup_is_github_unavailable(e2e: dict[str, Any], no_backoff: None) -> None:  # noqa: F811
    await queued(e2e)
    task = await plan_with(e2e, Fault("GET", rf"/repos/{e2e['world'].full_name}/branches/main", 503))
    assert task.failure_code == failures.GITHUB_UNAVAILABLE, task.failure_reason
    assert "Failed to resolve base branch" in task.failure_reason
    assert_nothing_ran(e2e)


@pytest.mark.asyncio
async def test_losing_access_to_the_repository_is_still_a_refusal(e2e: dict[str, Any], no_backoff: None) -> None:  # noqa: F811
    # The App was removed from the repository after the comment: permanent, not transient.
    await queued(e2e)
    task = await plan_with(e2e, Fault("GET", rf"/repos/{e2e['world'].full_name}", 404))
    assert task.failure_code == failures.POLICY_REFUSED, task.failure_reason
    assert "GitHub repository access refused" in task.failure_reason
    assert_nothing_ran(e2e)


@pytest.mark.asyncio
async def test_after_the_outage_a_retry_plans_again_and_waits_for_approval(
    e2e: dict[str, Any], no_backoff: None  # noqa: F811
) -> None:
    world = e2e["world"]
    await queued(e2e)
    failed = await plan_with(e2e, Fault("GET", rf"/repos/{world.full_name}", 502, times=4))
    assert failed.failure_code == failures.GITHUB_UNAVAILABLE

    # The outage is over (the fault is exhausted).  A writer retries through the API.
    response = await e2e["api"].post(f"/api/v1/tasks/{failed.task_id}/retry", json={}, headers=world.users["lead"][1])
    assert response.status_code == 200, response.text
    child_id = response.json()["task_id"]
    await dispatch_all(e2e)
    await run_queued_job(e2e)

    async with e2e["maker"]() as s:
        from app.db.models import Task

        child = await s.get(Task, child_id)
        parent = await s.get(Task, failed.task_id)
    assert child is not None and parent is not None
    assert child.parent_task_id == failed.task_id
    # The retry goes through the plan gate again: it waits for a human, nothing more.
    assert child.status == TaskStatusEnum.WAITING_APPROVAL.value, (child.failure_code, child.failure_reason)
    assert parent.status == TaskStatusEnum.FAILED.value  # the failed task's record is kept
    assert len(e2e["model"].requests) == 1
    assert e2e["fake"].writes() == [] and e2e["fake"].unexpected == []
