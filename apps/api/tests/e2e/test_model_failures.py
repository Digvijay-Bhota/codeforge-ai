"""The model failing during planning: a precise failure code, a redacted reason, nothing else.

Same harness as test_comment_to_approval.py.  The fake model answers through
the OpenAI SDK's own HTTP client with the error bodies OpenAI sends (the
quota error is the one the first real run received), so the real Agents SDK,
its retries, the classifier and the worker decide the outcome.  No request
leaves the process.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.models import Job, JobStatusEnum, TaskApproval, TaskStatusEnum
from app.orchestration import failures
from tests.e2e.test_comment_to_approval import (  # noqa: F401
    COMMAND,
    PLAN,
    deliver,
    dispatch_all,
    e2e,
    login,
    run_queued_job,
    the_task,
)

KEY_IN_MESSAGE = "sk-e2e-not-a-real-key-123456"


async def plan_once(env: dict[str, Any]) -> Any:
    world = env["world"]
    assert (await deliver(env, world.payload(login(world, "dev"), COMMAND))).status_code == 202
    await dispatch_all(env)
    await run_queued_job(env)
    await dispatch_all(env)
    return await the_task(env)


async def assert_stopped_cleanly(env: dict[str, Any], task: Any) -> None:
    assert task.status == TaskStatusEnum.FAILED.value
    async with env["maker"]() as s:
        approvals = (await s.execute(select(func.count()).select_from(TaskApproval)
                                     .where(TaskApproval.task_id == task.task_id))).scalar_one()  # fmt: skip
        pending = (await s.execute(select(func.count()).select_from(Job).where(
            Job.task_id == task.task_id, Job.status == JobStatusEnum.PENDING.value))).scalar_one()  # fmt: skip
    assert approvals == 0 and pending == 0  # nothing to approve, nothing re-queued
    assert list(env["clone_root"].iterdir()) == []  # the clone was removed
    assert env["fake"].writes() == [] and env["fake"].unexpected == []


@pytest.mark.asyncio
async def test_no_credit_left_is_model_quota_exceeded(e2e: dict[str, Any]) -> None:  # noqa: F811
    e2e["model"].error = (429, {"message": "You have no credits remaining.", "type": "insufficient_quota",
                                "param": None, "code": "credit_balance_exhausted"})  # fmt: skip
    task = await plan_once(e2e)
    assert task.failure_code == failures.MODEL_QUOTA_EXCEEDED, task.failure_reason
    assert "credit_balance_exhausted" in task.failure_reason
    # The SDK's own bounded retries (OpenAI client default: 2), then the task stops.
    assert len(e2e["model"].requests) == 3
    await assert_stopped_cleanly(e2e, task)


@pytest.mark.asyncio
async def test_a_rejected_key_is_model_authentication_failed_and_never_echoed(e2e: dict[str, Any]) -> None:  # noqa: F811
    e2e["model"].error = (401, {"message": f"Incorrect API key provided: {KEY_IN_MESSAGE}.",
                                "type": "invalid_request_error", "param": None, "code": "invalid_api_key"})  # fmt: skip
    task = await plan_once(e2e)
    assert task.failure_code == failures.MODEL_AUTHENTICATION_FAILED, task.failure_reason
    assert len(e2e["model"].requests) == 1  # a 401 is not retried
    assert KEY_IN_MESSAGE not in task.failure_reason and "Incorrect API key provided" in task.failure_reason
    detail = await e2e["api"].get(f"/api/v1/tasks/{task.task_id}", headers=e2e["world"].users["lead"][1])
    assert KEY_IN_MESSAGE not in detail.text
    await assert_stopped_cleanly(e2e, task)


@pytest.mark.asyncio
async def test_output_that_is_not_a_plan_is_model_output_invalid(e2e: dict[str, Any]) -> None:  # noqa: F811
    e2e["model"].output_text = "Sure! Here is my plan: edit the README."
    task = await plan_once(e2e)
    assert task.failure_code == failures.MODEL_OUTPUT_INVALID, task.failure_reason
    await assert_stopped_cleanly(e2e, task)


@pytest.mark.asyncio
async def test_a_plan_that_escapes_the_repository_is_rejected_by_validation(e2e: dict[str, Any]) -> None:  # noqa: F811
    escaping = {**PLAN, "affected_files": ["../../etc/passwd"]}
    e2e["model"].output_text = json.dumps(escaping)
    task = await plan_once(e2e)
    assert task.failure_code == failures.MODEL_OUTPUT_INVALID, task.failure_reason
    assert "traversal" in task.failure_reason
    await assert_stopped_cleanly(e2e, task)
