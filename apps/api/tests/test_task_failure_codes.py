"""A failed task says why, with a failure code, and never quotes a credential.

The first real GitHub run failed twice with an empty ``failure_code``: once
because the worker image had no ``git`` executable, once because the OpenAI
account had no credit left.  These tests pin the classification from the SDK's
exception to the stored task, the redaction of the stored and displayed text,
and that a failed planning job is not retried.

No network calls are made: OpenAI errors are built from the SDK's own
exception classes, git is either real (against local paths) or its absence is
simulated exactly as ``subprocess`` reports it.
"""

from __future__ import annotations

import errno
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import openai
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from app.db.models import OutboxEvent, Task, TaskStatusEnum
from app.github.git import GitError, SafeGitWrapper
from app.observability.redaction import redact_query, redact_secrets
from app.orchestration import failures
from app.orchestration.models import WorkflowStatus
from app.orchestration.orchestrator import Orchestrator
from app.orchestration.stages import PlanningStageError, run_planning_stage
from app.schemas.task import GitHubFailureStage, TaskResult, TaskStatus
from app.services.task_service import _map_to_task_result
from app.worker import WORKER_ERROR
from app.workspace.manager import WorkspaceManager
from tests.orchestration.test_orchestration import _make_context, _make_orchestrator, _make_plan
from tests.security.test_github_plan_gate import (  # noqa: F401
    comment_task,
    maker,
    pending_jobs,
    run_jobs,
    snapshot,
)
from tests.security.test_plan_approval_api import (  # noqa: F401
    _headers,
    client,
    permission_cache,
    world,
)

# Synthetic credentials, shaped like the real ones.
OPENAI_KEY = "sk-proj-synthetic-test-key-not-real-0000"
GITHUB_TOKEN = "ghs_SyntheticTestTokenNotReal0000"
DB_URL = "postgresql+asyncpg://codeforge:hunter2-db-password@postgres:5432/codeforge"

REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def status_error(cls: type[openai.APIStatusError], status: int, **error: Any) -> openai.APIStatusError:
    """An SDK status error as the client raises it (``body`` is the response's ``error`` object)."""
    body = {"message": "synthetic", "type": None, "param": None, "code": None, **error}
    return cls("synthetic", response=httpx2.Response(status, request=REQUEST, json={"error": body}), body=body)


# ── Classification of model errors ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        # The real response from the integration test: HTTP 429, no credit.
        (status_error(openai.RateLimitError, 429, type="insufficient_quota", code="credit_balance_exhausted"),
         failures.MODEL_QUOTA_EXCEEDED),
        (status_error(openai.RateLimitError, 429, type="insufficient_quota", code="insufficient_quota"),
         failures.MODEL_QUOTA_EXCEEDED),
        # Same status, but a temporary limit: a different, retryable class.
        (status_error(openai.RateLimitError, 429, type="requests", code="rate_limit_exceeded"),
         failures.MODEL_RATE_LIMITED),
        (status_error(openai.AuthenticationError, 401, code="invalid_api_key"), failures.MODEL_AUTHENTICATION_FAILED),
        (status_error(openai.PermissionDeniedError, 403), failures.MODEL_AUTHENTICATION_FAILED),
        (openai.APIConnectionError(request=REQUEST), failures.MODEL_UNAVAILABLE),
        (openai.APITimeoutError(request=REQUEST), failures.MODEL_UNAVAILABLE),
        (status_error(openai.InternalServerError, 503), failures.MODEL_UNAVAILABLE),
        (status_error(openai.NotFoundError, 404, code="model_not_found"), failures.MODEL_REQUEST_REJECTED),
        (status_error(openai.BadRequestError, 400), failures.MODEL_REQUEST_REJECTED),
        (ModelBehaviorError("Invalid JSON when parsing the output"), failures.MODEL_OUTPUT_INVALID),
        (MaxTurnsExceeded("Max turns (10) exceeded"), failures.MODEL_OUTPUT_INVALID),
    ],  # fmt: skip
)
def test_model_errors_are_classified_by_exception_type(exc: BaseException, code: str) -> None:
    assert failures.classify_model_error(exc) == code


def test_a_wrapped_model_error_is_classified_through_its_cause() -> None:
    quota = status_error(openai.RateLimitError, 429, type="insufficient_quota")
    try:
        try:
            raise quota
        except openai.RateLimitError as inner:
            raise RuntimeError("agent run failed") from inner
    except RuntimeError as outer:
        assert failures.classify_model_error(outer) == failures.MODEL_QUOTA_EXCEEDED


def test_other_errors_are_not_model_errors() -> None:
    assert failures.classify_model_error(ValueError("bad value")) is None
    # The message alone never decides: this is not an SDK quota error.
    assert failures.classify_model_error(RuntimeError("429 insufficient_quota")) is None


# ── Planning stage → orchestrator → TaskResult ───────────────────────────────


def _planner_raising(exc: BaseException) -> Any:
    return patch("app.orchestration.stages.Runner.run", new=AsyncMock(side_effect=exc))


@pytest.mark.asyncio
async def test_planning_stage_carries_the_model_failure_code() -> None:
    quota = status_error(openai.RateLimitError, 429, type="insufficient_quota", code="credit_balance_exhausted")
    with patch("app.orchestration.stages.make_planner_agent"), _planner_raising(quota):
        with pytest.raises(PlanningStageError) as info:
            await run_planning_stage("Add a README section", _make_context(), "gpt-4o")
    assert info.value.code == failures.MODEL_QUOTA_EXCEEDED
    assert "Planner agent failed" in str(info.value)


@pytest.mark.asyncio
async def test_planning_stage_reports_unexpected_output_as_invalid_model_output() -> None:
    result = MagicMock(final_output="not a plan")
    with (
        patch("app.orchestration.stages.make_planner_agent"),
        patch("app.orchestration.stages.Runner.run", new=AsyncMock(return_value=result)),
    ):
        with pytest.raises(PlanningStageError) as info:
            await run_planning_stage("Add a README section", _make_context(), "gpt-4o")
    assert info.value.code == failures.MODEL_OUTPUT_INVALID


@pytest.mark.asyncio
async def test_planning_stage_falls_back_to_planning_failed() -> None:
    with patch("app.orchestration.stages.make_planner_agent"), _planner_raising(RuntimeError("unexpected")):
        with pytest.raises(PlanningStageError) as info:
            await run_planning_stage("Add a README section", _make_context(), "gpt-4o")
    assert info.value.code == failures.PLANNING_FAILED


@pytest.mark.asyncio
async def test_a_plan_only_run_reports_the_failure_code_in_its_task_result(tmp_path: Path) -> None:
    quota = status_error(openai.RateLimitError, 429, type="insufficient_quota")
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.stages.make_planner_agent"),
        _planner_raising(quota),
    ):
        final = await _make_orchestrator().run(WorkspaceManager(tmp_path), "Add a README section", stop_after_plan=True)
    assert final.workflow_status == WorkflowStatus.FAILED
    assert final.failure_code == failures.MODEL_QUOTA_EXCEEDED

    result = _map_to_task_result(final, "task-1")
    assert result.status == TaskStatus.error
    assert result.failure_code == failures.MODEL_QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_analysis_failure_has_its_own_code(tmp_path: Path) -> None:
    from app.orchestration.stages import AnalysisStageError

    with patch("app.orchestration.orchestrator.run_analysis_stage", side_effect=AnalysisStageError("unreadable")):
        final = await Orchestrator(executor=MagicMock()).run(WorkspaceManager(tmp_path), "Add a README section")
    assert final.failure_code == failures.ANALYSIS_FAILED


@pytest.mark.asyncio
async def test_a_successful_plan_has_no_failure_code(tmp_path: Path) -> None:
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=_make_context()),
        patch("app.orchestration.orchestrator.run_planning_stage", new=AsyncMock(return_value=_make_plan())),
    ):
        final = await _make_orchestrator().run(WorkspaceManager(tmp_path), "Add a README section", stop_after_plan=True)
    assert final.workflow_status == WorkflowStatus.COMPLETED
    assert final.failure_code is None


# ── git: a missing executable is reported as such ───────────────────────────


def test_a_missing_git_executable_is_an_actionable_git_error(tmp_path: Path) -> None:
    # Exactly what subprocess raises when the executable is not on the PATH.
    missing = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "git")
    wrapper = SafeGitWrapper(tmp_path, GITHUB_TOKEN)
    with patch("app.github.git.subprocess.run", side_effect=missing):
        with pytest.raises(GitError) as info:
            wrapper.clone("owner", "repo")
    message = str(info.value)
    assert "git is not installed on this worker" in message
    assert GITHUB_TOKEN not in message


def test_a_missing_working_directory_is_not_reported_as_missing_git(tmp_path: Path) -> None:
    # Real git, real subprocess: here the working directory is what is missing.
    wrapper = SafeGitWrapper(tmp_path / "does-not-exist", GITHUB_TOKEN)
    with pytest.raises(GitError) as info:
        wrapper.clone("owner", "repo")
    assert "not installed" not in str(info.value)


# ── Redaction ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "secret",
    [
        OPENAI_KEY,
        GITHUB_TOKEN,
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789",
        "hunter2-db-password",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ],
)
def test_redaction_removes_credentials_and_keeps_the_message(secret: str) -> None:
    text = (
        f"Planner agent failed: key {OPENAI_KEY} rejected; clone of "
        f"https://x-access-token:{GITHUB_TOKEN}@github.com/o/r.git failed; db {DB_URL}; "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c; "
        "token github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789"
    )
    redacted = redact_secrets(text)
    assert secret not in redacted
    assert "Planner agent failed" in redacted
    assert "github.com/o/r.git" in redacted
    assert "postgres:5432/codeforge" in redacted


def test_redaction_removes_private_keys() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nSYNTHETIC-NOT-A-REAL-KEY\n-----END RSA PRIVATE KEY-----"
    redacted = redact_secrets(f"could not load {pem} from settings")
    assert "SYNTHETIC-NOT-A-REAL-KEY" not in redacted
    assert redacted.startswith("could not load ") and redacted.endswith(" from settings")


def test_redaction_leaves_ordinary_failure_text_alone() -> None:
    text = "Failed to acquire repository: git is not installed on this worker; the worker image must provide the git executable."
    assert redact_secrets(text) == text


def test_query_redaction_is_selective() -> None:
    assert redact_query("/api/v1/auth/github/callback?code=abc123&state=xyz789") == (
        "/api/v1/auth/github/callback?code=***&state=***"
    )
    assert redact_query("/api/v1/tasks?limit=20&offset=0") == "/api/v1/tasks?limit=20&offset=0"


# ── Worker: the stored task ──────────────────────────────────────────────────


def failed_plan(task_id: str, **fields: Any) -> TaskResult:
    return TaskResult(
        task_id=task_id, status=TaskStatus.error, description="Add a README section", plan=[], changed_files=[],
        diff="", test_result=None, agent_output="", **fields,
    )  # fmt: skip


async def fail_planning(session: AsyncSession, maker: Any, **fields: Any) -> str:  # noqa: F811
    """A comment task whose planning job returns a failed result with *fields*."""
    task_id = await comment_task(session)
    result = failed_plan(task_id, **fields)
    with patch("app.services.task_service.TaskService.execute_task", new=AsyncMock(return_value=result)):
        await run_jobs(maker, task_id)
    return task_id


async def job_failed_payloads(session: AsyncSession, task_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == task_id))).scalars().all()
    return [e.payload for e in rows if e.event_type == "GITHUB_STATUS_UPDATE" and e.payload.get("event") == "JOB_FAILED"]


@pytest.mark.asyncio
async def test_quota_exhaustion_fails_the_task_with_its_code_and_no_retry(session: AsyncSession, maker: Any) -> None:  # noqa: F811
    reason = (
        "Planning failed: Planner agent failed: Error code: 429 - insufficient_quota "
        f"(key {OPENAI_KEY}, clone https://x-access-token:{GITHUB_TOKEN}@github.com/o/r.git)"
    )
    task_id = await fail_planning(session, maker, error_message=reason, failure_code=failures.MODEL_QUOTA_EXCEEDED)

    st = await snapshot(maker, task_id)
    assert st["status"] == TaskStatusEnum.FAILED.value
    assert st["failure_code"] == failures.MODEL_QUOTA_EXCEEDED
    assert "insufficient_quota" in st["failure_reason"]
    assert OPENAI_KEY not in st["failure_reason"] and GITHUB_TOKEN not in st["failure_reason"]
    assert st["approvals"] == []  # no plan, nothing to approve
    assert await pending_jobs(maker, task_id) == []  # not re-queued

    payloads = await job_failed_payloads(session, task_id)
    assert payloads and all(OPENAI_KEY not in p["error"] and GITHUB_TOKEN not in p["error"] for p in payloads)


@pytest.mark.asyncio
async def test_repository_acquisition_failure_is_stored_with_its_stage(session: AsyncSession, maker: Any) -> None:  # noqa: F811
    task_id = await fail_planning(
        session,
        maker,
        error_message="Failed to acquire repository: git is not installed on this worker; "
        "the worker image must provide the git executable.",
        failure_stage=GitHubFailureStage.REPOSITORY_ACQUISITION_FAILED,
    )
    st = await snapshot(maker, task_id)
    assert st["status"] == TaskStatusEnum.FAILED.value
    assert st["failure_code"] == "REPOSITORY_ACQUISITION_FAILED"
    assert "git is not installed" in st["failure_reason"]


@pytest.mark.asyncio
async def test_a_workflow_code_wins_over_the_generic_github_stage(session: AsyncSession, maker: Any) -> None:  # noqa: F811
    task_id = await fail_planning(
        session, maker, error_message="Planner agent failed", failure_code=failures.MODEL_AUTHENTICATION_FAILED,
        failure_stage=GitHubFailureStage.ORCHESTRATION_FAILED,
    )  # fmt: skip
    assert (await snapshot(maker, task_id))["failure_code"] == failures.MODEL_AUTHENTICATION_FAILED


@pytest.mark.asyncio
async def test_an_unclassified_planning_failure_still_has_a_code(session: AsyncSession, maker: Any) -> None:  # noqa: F811
    task_id = await fail_planning(session, maker, error_message="Planning failed: something else")
    assert (await snapshot(maker, task_id))["failure_code"] == failures.PLANNING_FAILED


@pytest.mark.asyncio
async def test_a_worker_exception_is_stored_as_worker_error_without_credentials(
    session: AsyncSession, maker: Any  # noqa: F811
) -> None:
    task_id = await comment_task(session)
    boom = RuntimeError(f"could not connect to {DB_URL}")
    with patch("app.services.task_service.TaskService.execute_task", new=AsyncMock(side_effect=boom)):
        await run_jobs(maker, task_id)
    st = await snapshot(maker, task_id)
    assert st["status"] == TaskStatusEnum.FAILED.value
    assert st["failure_code"] == WORKER_ERROR
    assert "hunter2-db-password" not in st["failure_reason"]
    assert "could not connect to" in st["failure_reason"]


# ── API: list and detail ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_shows_the_code_and_redacts_historical_failure_text(
    session: AsyncSession, client: Any, world: Any  # noqa: F811
) -> None:
    # A row written before redaction existed, quoting a credential.
    task = Task(
        task_id=str(uuid.uuid4()), status=TaskStatusEnum.FAILED.value, execution_target="github",
        repository=world["repo"], requested_task="Add a README section", creator_id=None,
        failure_code=failures.MODEL_QUOTA_EXCEEDED,
        failure_reason=f"Planner agent failed: 429 insufficient_quota for key {OPENAI_KEY}",
    )  # fmt: skip
    session.add(task)
    await session.commit()

    detail = await client.get(f"/api/v1/tasks/{task.task_id}", headers=world["writer_h"])
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["failure_code"] == failures.MODEL_QUOTA_EXCEEDED
    assert "insufficient_quota" in body["failure_reason"]
    assert OPENAI_KEY not in detail.text

    listing = await client.get("/api/v1/tasks", params={"repository": world["repo"]}, headers=world["writer_h"])
    assert listing.status_code == 200, listing.text
    summaries = {t["task_id"]: t for t in listing.json()["tasks"]}
    assert summaries[task.task_id]["failure_code"] == failures.MODEL_QUOTA_EXCEEDED
