from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.db.models import Task, TaskStatusEnum, User
from app.main import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = create_app()
    mock_user = User(
        id="test-legacy-user",
        display_name="Legacy Tester",
        email="legacy@example.com",
        is_active=True,
    )
    app.dependency_overrides[get_current_user] = lambda: mock_user
    return TestClient(app)

def test_create_task_missing_workspace(client: TestClient) -> None:
    response = client.post("/api/v1/tasks", json={"description": "Fix something important here"})
    assert response.status_code == 422

def test_create_task_description_too_short(client: TestClient) -> None:
    response = client.post("/api/v1/tasks", json={"workspace_path": "/tmp/x", "description": "short"})
    assert response.status_code == 422

def test_create_task_missing_description(client: TestClient) -> None:
    response = client.post("/api/v1/tasks", json={"workspace_path": "/tmp/x"})
    assert response.status_code == 422

def test_create_task_empty_body(client: TestClient) -> None:
    response = client.post("/api/v1/tasks", json={})
    assert response.status_code == 422

def test_create_task_success(client: TestClient) -> None:
    mock_task = Task(task_id="test-task-id", status=TaskStatusEnum.PENDING.value)
    with patch("app.services.task_service.TaskService.create_task", new=AsyncMock(return_value=mock_task)):
        # Mock commit
        with patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new=AsyncMock()):
            response = client.post(
                "/api/v1/tasks",
                json={
                    "workspace_path": "/tmp/repo",
                    "description": "Fix the add function in calculator.py",
                },
            )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["task_id"] == "test-task-id"

def test_health_still_works(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200

def test_v1_health_still_works(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
