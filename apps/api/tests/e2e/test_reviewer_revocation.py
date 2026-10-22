"""A plan decision sees a revoked write permission at once, not after the cache expires.

Collaborator permissions are cached in Redis for five minutes.  Reading a task
may use that cache; deciding on a plan may not: approving a plan is the human
gate that lets generated code be written and tested.  Same harness as
test_comment_to_approval.py; GitHub is the local fake, whose answer changes
when the reviewer is downgraded.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.models import TaskStatusEnum
from tests.e2e.test_comment_to_approval import (  # noqa: F401
    COMMAND,
    deliver,
    dispatch_all,
    e2e,
    login,
    run_queued_job,
    the_task,
)


async def waiting_for_approval(env: dict[str, Any]) -> tuple[str, int]:
    world = env["world"]
    assert (await deliver(env, world.payload(login(world, "dev"), COMMAND))).status_code == 202
    await dispatch_all(env)
    await run_queued_job(env)
    await dispatch_all(env)
    task = await the_task(env)
    assert task.status == TaskStatusEnum.WAITING_APPROVAL.value
    detail = await env["api"].get(f"/api/v1/tasks/{task.task_id}", headers=world.users["lead"][1])
    assert detail.status_code == 200  # the reviewer looked at the plan: "write" is now cached
    return task.task_id, detail.json()["latest_approval"]["id"]


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.asyncio
async def test_a_reviewer_downgraded_after_viewing_cannot_decide(e2e: dict[str, Any], decision: str) -> None:  # noqa: F811
    world = e2e["world"]
    task_id, approval_id = await waiting_for_approval(e2e)

    e2e["fake"].permissions[(world.full_name, login(world, "lead"))] = "read"  # revoked on GitHub
    response = await e2e["api"].post(f"/api/v1/tasks/{task_id}/{decision}", json={"approval_id": approval_id},
                                     headers=world.users["lead"][1])  # fmt: skip
    assert response.status_code == 403, response.text
    assert (await the_task(e2e)).status == TaskStatusEnum.WAITING_APPROVAL.value

    # Reading still works for a reader, and the fresh answer replaced the cached one.
    detail = await e2e["api"].get(f"/api/v1/tasks/{task_id}", headers=world.users["lead"][1])
    assert detail.status_code == 200


@pytest.mark.asyncio
async def test_a_current_writer_still_decides(e2e: dict[str, Any]) -> None:  # noqa: F811
    world = e2e["world"]
    task_id, approval_id = await waiting_for_approval(e2e)
    response = await e2e["api"].post(f"/api/v1/tasks/{task_id}/approve", json={"approval_id": approval_id},
                                     headers=world.users["lead"][1])  # fmt: skip
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_github_failing_during_a_decision_fails_closed_and_is_not_remembered(
    e2e: dict[str, Any], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from app.config import settings
    from tests.e2e.fake_github import Fault

    monkeypatch.setattr(settings, "github_client_retry_backoff_factor", 0.0)
    world = e2e["world"]
    task_id, approval_id = await waiting_for_approval(e2e)
    lead = login(world, "lead")
    outage = Fault("GET", rf"/repos/{world.full_name}/collaborators/{lead}/permission", 502)
    e2e["fake"].faults.append(outage)

    during = await e2e["api"].post(f"/api/v1/tasks/{task_id}/approve", json={"approval_id": approval_id},
                                   headers=world.users["lead"][1])  # fmt: skip
    assert during.status_code == 503, during.text  # not "you lack write access"
    assert "could not confirm" in during.json()["detail"]
    assert (await the_task(e2e)).status == TaskStatusEnum.WAITING_APPROVAL.value

    # GitHub is back: the failed lookup was not cached as "no permission".
    e2e["fake"].faults.remove(outage)
    detail = await e2e["api"].get(f"/api/v1/tasks/{task_id}", headers=world.users["lead"][1])
    assert detail.status_code == 200, detail.text
    after = await e2e["api"].post(f"/api/v1/tasks/{task_id}/approve", json={"approval_id": approval_id},
                                  headers=world.users["lead"][1])  # fmt: skip
    assert after.status_code == 200, after.text
