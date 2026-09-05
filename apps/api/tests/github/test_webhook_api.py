import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def get_signature(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()

@pytest.fixture
def client():
    # Setup test secrets
    settings.github_webhook_secret = "test_secret"
    return TestClient(app)

def test_webhook_api_valid_signature_unsupported_event(client):
    payload = {"action": "deleted"} # missing other fields
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=raw,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-1234",
            "X-Hub-Signature-256": sig
        }
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "unsupported event"

def test_webhook_api_missing_secret(client):
    settings.github_webhook_secret = ""
    resp = client.post(
        "/api/v1/github/webhooks",
        content=b"{}",
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-1",
            "X-Hub-Signature-256": "sha256=123"
        }
    )
    assert resp.status_code == 503

def test_webhook_api_invalid_signature(client):
    resp = client.post(
        "/api/v1/github/webhooks",
        content=b"{}",
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-1",
            "X-Hub-Signature-256": "sha256=wrong"
        }
    )
    assert resp.status_code == 401

def test_webhook_api_success_ignored(client):
    payload = {
        "action": "opened",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "repo", "owner": {"login": "owner"}, "full_name": "owner/repo", "default_branch": "main"},
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"}
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=raw,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-valid-1",
            "X-Hub-Signature-256": sig
        }
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"

def test_webhook_api_duplicate_ignored(client):
    payload = {
        "action": "opened",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "repo", "owner": {"login": "owner"}, "full_name": "owner/repo", "default_branch": "main"},
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"}
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    client.post(
        "/api/v1/github/webhooks",
        content=raw,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-dup-1",
            "X-Hub-Signature-256": sig
        }
    )
    resp = client.post(
        "/api/v1/github/webhooks",
        content=raw,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-dup-1",
            "X-Hub-Signature-256": sig
        }
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "duplicate delivery"


@pytest.mark.asyncio
async def test_webhook_api_goes_through_task_service(client):
    payload = {
        "action": "created",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "repo", "owner": {"login": "owner"}, "full_name": "owner/repo", "default_branch": "main"},
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"},
        "comment": {"id": 3, "body": "/codeforge fix"}
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    with patch("app.services.task_service.TaskService.run_task", new_callable=AsyncMock) as mock_run_task:
        mock_result = AsyncMock()
        mock_result.status = "success"
        mock_result.task_id = "test-123"
        mock_result.github = None
        mock_run_task.return_value = mock_result

        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-task-service-test-1",
                "X-Hub-Signature-256": sig
            }
        )
        assert resp.status_code == 200
        mock_run_task.assert_called_once()
        args, _ = mock_run_task.call_args
        assert args[0].execution_target.value == "github"

@pytest.mark.asyncio
async def test_webhook_api_duplicate_delivery_does_not_call_task_service(client):
    payload = {
        "action": "created",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "repo", "owner": {"login": "owner"}, "full_name": "owner/repo", "default_branch": "main"},
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"},
        "comment": {"id": 3, "body": "/codeforge fix"}
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    with patch("app.services.task_service.TaskService.run_task", new_callable=AsyncMock) as mock_run_task:
        mock_result = AsyncMock()
        mock_result.status = "success"
        mock_result.task_id = "test-dup"
        mock_result.github = None
        mock_run_task.return_value = mock_result

        # First request
        client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-dup-test-1",
                "X-Hub-Signature-256": sig
            }
        )
        # Duplicate request
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-dup-test-1",
                "X-Hub-Signature-256": sig
            }
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "duplicate delivery"

        # Prove it was only called ONCE
        mock_run_task.assert_called_once()
