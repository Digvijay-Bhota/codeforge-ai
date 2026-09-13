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
    payload = {"action": "deleted"}  # missing other fields
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=raw,
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-1234",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code in (200, 202)
    assert resp.json()["reason"] == "unsupported event"


def test_webhook_api_missing_secret(client):
    settings.github_webhook_secret = ""
    resp = client.post(
        "/api/v1/github/webhooks",
        content=b"{}",
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-1",
            "X-Hub-Signature-256": "sha256=123",
        },
    )
    assert resp.status_code == 503


def test_webhook_api_invalid_signature(client):
    resp = client.post(
        "/api/v1/github/webhooks",
        content=b"{}",
        headers={
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "del-1",
            "X-Hub-Signature-256": "sha256=wrong",
        },
    )
    assert resp.status_code == 401


def test_webhook_api_success_ignored(client):
    payload = {
        "action": "opened",
        "installation": {"id": 1},
        "repository": {
            "id": 2,
            "name": "repo",
            "owner": {"login": "owner"},
            "full_name": "owner/repo",
            "default_branch": "main",
        },
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"},
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    with (
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.get_installation",
            new=AsyncMock(return_value=AsyncMock(active=True)),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.is_repository_authorized",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.record_delivery",
            new=AsyncMock(return_value=True),
        ),
        patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "del-valid-1",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code in (200, 202)
    assert resp.json()["status"] == "ignored"


def test_webhook_api_duplicate_ignored(client):
    payload = {
        "action": "opened",
        "installation": {"id": 1},
        "repository": {
            "id": 2,
            "name": "repo",
            "owner": {"login": "owner"},
            "full_name": "owner/repo",
            "default_branch": "main",
        },
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"},
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    with (
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.get_installation",
            new=AsyncMock(return_value=AsyncMock(active=True)),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.is_repository_authorized",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.record_delivery",
            new=AsyncMock(side_effect=[True, False]),
        ),
        patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new=AsyncMock()),
    ):
        client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "del-dup-1",
                "X-Hub-Signature-256": sig,
            },
        )

        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "del-dup-1",
                "X-Hub-Signature-256": sig,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_webhook_api_goes_through_outbox_event(client):
    payload = {
        "action": "created",
        "installation": {"id": 1},
        "repository": {
            "id": 2,
            "name": "repo",
            "owner": {"login": "owner"},
            "full_name": "owner/repo",
            "default_branch": "main",
        },
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"},
        "comment": {"id": 3, "body": "/codeforge fix", "user": {"id": 101, "login": "testauthor"}},
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    with (
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.get_installation",
            new=AsyncMock(return_value=AsyncMock(active=True)),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.is_repository_authorized",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.authorization_service.AuthorizationService.can_actor_write_repository",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.record_delivery",
            new=AsyncMock(side_effect=[True, False]),
        ),
        patch(
            "app.db.repositories.outbox_repository.OutboxRepository.create_event",
            new_callable=AsyncMock,
        ) as mock_create_event,
        patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-task-service-test-1",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code in (200, 202)
        mock_create_event.assert_called_once()
        event_arg = mock_create_event.call_args[0][0]
        assert event_arg.event_type == "GITHUB_COMMAND_INGESTED"
        assert event_arg.aggregate_id == "del-task-service-test-1"
        assert event_arg.payload["command"]["name"] == "fix"
        assert event_arg.payload["actor_login"] == "testauthor"


@pytest.mark.asyncio
async def test_webhook_api_duplicate_delivery_does_not_call_outbox(client):
    payload = {
        "action": "created",
        "installation": {"id": 1},
        "repository": {
            "id": 2,
            "name": "repo",
            "owner": {"login": "owner"},
            "full_name": "owner/repo",
            "default_branch": "main",
        },
        "issue": {"number": 1, "title": "A bug", "body": "Fix it please"},
        "comment": {"id": 3, "body": "/codeforge fix", "user": {"id": 101, "login": "testauthor"}},
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = get_signature(settings.github_webhook_secret, raw)

    with (
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.get_installation",
            new=AsyncMock(return_value=AsyncMock(active=True)),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.is_repository_authorized",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.authorization_service.AuthorizationService.can_actor_write_repository",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.record_delivery",
            new=AsyncMock(side_effect=[True, False]),
        ),
        patch(
            "app.db.repositories.outbox_repository.OutboxRepository.create_event",
            new_callable=AsyncMock,
        ) as mock_create_event,
        patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new=AsyncMock()),
    ):
        # First request
        resp1 = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-dup-test-1",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp1.status_code == 202

        # Duplicate request
        resp2 = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-dup-test-1",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp2.status_code in (200, 202)
        assert resp2.json()["reason"] == "duplicate delivery"

        # Prove it was only called ONCE
        mock_create_event.assert_called_once()
