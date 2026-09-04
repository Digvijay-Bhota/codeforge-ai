"""Integration tests for the POST /api/v1/tasks endpoint.

The TaskService is mocked so no LLM calls or real file I/O are required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.task import (
    ChangedFile,
    PlanStep,
    TaskResult,
    TaskStatus,
    TestResult,
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def _make_success_result() -> TaskResult:
    return TaskResult(
        task_id="test-task-id",
        status=TaskStatus.success,
        description="Fix the add function in the calculator module",
        plan=[PlanStep(step=1, description="Read and fix")],
        changed_files=[ChangedFile(path="calculator.py", action="modified")],
        diff="--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1 @@\n-    return a - b\n+    return a + b\n",
        test_result=TestResult(
            passed=True, exit_code=0, stdout="1 passed", stderr="", duration_seconds=0.5
        ),
        agent_output="Fixed the add function.",
    )


# ── Request validation ────────────────────────────────────────────────────────


def test_create_task_missing_workspace(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={"description": "Fix something important here"},
    )
    assert response.status_code == 422


def test_create_task_description_too_short(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={"workspace_path": "/tmp/x", "description": "short"},
    )
    assert response.status_code == 422


def test_create_task_missing_description(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={"workspace_path": "/tmp/x"},
    )
    assert response.status_code == 422


def test_create_task_empty_body(client: TestClient) -> None:
    response = client.post("/api/v1/tasks", json={})
    assert response.status_code == 422


# ── Successful task (mocked service) ─────────────────────────────────────────


def test_create_task_success(client: TestClient) -> None:
    success_result = _make_success_result()
    with patch(
        "app.api.tasks._task_service.run_task",
        new=AsyncMock(return_value=success_result),
    ):
        response = client.post(
            "/api/v1/tasks",
            json={
                "workspace_path": "/tmp/repo",
                "description": "Fix the add function in calculator.py",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["task_id"] == "test-task-id"
    assert len(body["changed_files"]) == 1
    assert body["changed_files"][0]["path"] == "calculator.py"
    assert body["test_result"]["passed"] is True
    assert body["diff"] != ""


# ── Error result (mocked service) ─────────────────────────────────────────────


def test_create_task_returns_error_status(client: TestClient) -> None:
    error_result = TaskResult(
        task_id="err-id",
        status=TaskStatus.error,
        description="Fix something important in the codebase",
        plan=[],
        changed_files=[],
        diff="",
        error_message="Workspace does not exist",
        agent_output="",
    )
    with patch(
        "app.api.tasks._task_service.run_task",
        new=AsyncMock(return_value=error_result),
    ):
        response = client.post(
            "/api/v1/tasks",
            json={
                "workspace_path": "/nonexistent",
                "description": "Fix the add function in calculator.py",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "does not exist" in body["error_message"]


def test_create_task_returns_failure_status(client: TestClient) -> None:
    failure_result = TaskResult(
        task_id="fail-id",
        status=TaskStatus.failure,
        description="Fix the add function in calculator.py please",
        plan=[PlanStep(step=1, description="Attempted fix")],
        changed_files=[ChangedFile(path="calculator.py", action="modified")],
        diff="--- a\n+++ b\n",
        test_result=TestResult(
            passed=False, exit_code=1, stdout="1 failed", stderr="", duration_seconds=0.3
        ),
        agent_output="Tests still failing.",
    )
    with patch(
        "app.api.tasks._task_service.run_task",
        new=AsyncMock(return_value=failure_result),
    ):
        response = client.post(
            "/api/v1/tasks",
            json={
                "workspace_path": "/tmp/repo",
                "description": "Fix the add function in calculator.py please",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failure"
    assert body["test_result"]["passed"] is False


# ── Phase 0 regression: existing endpoints must still work ────────────────────


def test_health_still_works(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_v1_health_still_works(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
