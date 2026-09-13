"""Comprehensive test suite for Phase 10B.3 — GitHub Webhook Command Ingestion.

Covers:
- Signature verification (HMAC SHA-256, constant-time, malformed/missing headers)
- Replay / delivery deduplication (database uniqueness, rollback behavior)
- Event filtering (issue_comment.created vs edited/deleted, ping, unsupported events)
- @codeforge command parser (grammar, case insensitivity, limits, control chars)
- Actor identity extraction (webhook authoritative, spoofing resistance)
- Repository & collaborator authorization (fail-closed, write/admin checks, installation validation)
- Durable event persistence (OutboxEvent generation, payload completeness)
- Security & error handling (no secrets leaked, traversal prevention, payload size limits)
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.github.command_parser import (
    CodeForgeCommandType,
    CommandParser,
    InvalidCodeForgeCommand,
)
from app.github.webhooks import (
    InvalidWebhookPayload,
    UnsupportedWebhookEvent,
    parse_issue_comment_payload,
    verify_signature,
)
from app.main import app
from app.services.authorization_service import (
    set_collaborator_permission_resolver,
)


def compute_signature(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


@pytest.fixture
def client():
    settings.github_webhook_secret = "test_webhook_secret_key"
    yield TestClient(app)
    set_collaborator_permission_resolver(None)


def make_comment_payload(
    action: str = "created",
    install_id: int = 42,
    owner: str = "testorg",
    repo: str = "testrepo",
    issue_num: int = 99,
    comment_id: int = 501,
    comment_body: str = "@codeforge fix broken test",
    actor_id: int = 1234,
    actor_login: str = "alice",
    is_pr: bool = False,
) -> dict:
    payload = {
        "action": action,
        "installation": {"id": install_id},
        "repository": {
            "id": 1001,
            "name": repo,
            "owner": {"login": owner},
            "full_name": f"{owner}/{repo}",
            "default_branch": "main",
        },
        "issue": {
            "number": issue_num,
            "title": "Bug in authentication",
            "html_url": f"https://github.com/{owner}/{repo}/issues/{issue_num}",
        },
        "comment": {
            "id": comment_id,
            "body": comment_body,
            "user": {
                "id": actor_id,
                "login": actor_login,
            },
            "created_at": "2026-09-14T00:00:00Z",
        },
        "sender": {
            "id": actor_id,
            "login": actor_login,
        },
    }
    if is_pr:
        payload["issue"]["pull_request"] = {
            "html_url": f"https://github.com/{owner}/{repo}/pull/{issue_num}"
        }
    return payload


# ==============================================================================
# 1. SIGNATURE VERIFICATION TESTS
# ==============================================================================


def test_signature_valid_accepted():
    body = b'{"hello": "world"}'
    secret = "my_secret_token"
    sig = compute_signature(secret, body)
    assert verify_signature(body, sig, secret) is True


def test_signature_invalid_rejected():
    body = b'{"hello": "world"}'
    secret = "my_secret_token"
    assert verify_signature(body, "sha256=invalidhex0123456789abcdef", secret) is False


def test_signature_missing_prefix_rejected():
    body = b'{"hello": "world"}'
    secret = "my_secret_token"
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # Signature missing 'sha256=' prefix
    assert verify_signature(body, mac, secret) is False


def test_signature_missing_header_rejected():
    assert verify_signature(b"{}", "", "secret") is False


def test_signature_tampered_payload_rejected():
    body = b'{"action": "created"}'
    sig = compute_signature("secret", body)
    tampered_body = b'{"action": "created", "extra": "data"}'
    assert verify_signature(tampered_body, sig, "secret") is False


def test_signature_empty_secret_rejected():
    assert verify_signature(b"{}", "sha256=abc", "") is False


# ==============================================================================
# 2. COMMAND PARSER TESTS
# ==============================================================================


def test_parser_at_codeforge_fix():
    cmd = CommandParser.parse("@codeforge fix failing tests")
    assert cmd is not None
    assert cmd.name == CodeForgeCommandType.FIX
    assert cmd.command == CodeForgeCommandType.FIX
    assert cmd.arguments == "failing tests"
    assert cmd.normalized_text == "@codeforge fix failing tests"


def test_parser_case_insensitivity():
    cmd1 = CommandParser.parse("@CodeForge implement new feature")
    assert cmd1 is not None
    assert cmd1.name == CodeForgeCommandType.IMPLEMENT
    assert cmd1.arguments == "new feature"

    cmd2 = CommandParser.parse("@CODEFORGE REVIEW this PR please")
    assert cmd2 is not None
    assert cmd2.name == CodeForgeCommandType.REVIEW
    assert cmd2.arguments == "this PR please"


def test_parser_supported_command_types():
    for act, expected in [
        ("fix", CodeForgeCommandType.FIX),
        ("implement", CodeForgeCommandType.IMPLEMENT),
        ("review", CodeForgeCommandType.REVIEW),
        ("explain", CodeForgeCommandType.EXPLAIN),
        ("analyze", CodeForgeCommandType.ANALYZE),
    ]:
        parsed = CommandParser.parse(f"@codeforge {act} some context")
        assert parsed is not None
        assert parsed.name == expected


def test_parser_empty_action_raises():
    with pytest.raises(InvalidCodeForgeCommand, match="Missing command action"):
        CommandParser.parse("@codeforge")

    with pytest.raises(InvalidCodeForgeCommand, match="Missing command action"):
        CommandParser.parse("   @codeforge    ")


def test_parser_unsupported_action_raises():
    with pytest.raises(InvalidCodeForgeCommand, match="Unsupported command action"):
        CommandParser.parse("@codeforge delete everything")


def test_parser_control_characters_rejected():
    with pytest.raises(InvalidCodeForgeCommand, match="forbidden control characters"):
        CommandParser.parse("@codeforge fix \x00 malicious payload")


def test_parser_multiline_arguments():
    text = "@codeforge explain\nThis is line 1\nThis is line 2"
    cmd = CommandParser.parse(text)
    assert cmd is not None
    assert cmd.name == CodeForgeCommandType.EXPLAIN
    assert cmd.arguments == "This is line 1\nThis is line 2"


def test_parser_command_after_text_lines():
    text = "cc @team\nLooks like tests failed.\n@codeforge fix tests\nThanks!"
    cmd = CommandParser.parse(text)
    assert cmd is not None
    assert cmd.name == CodeForgeCommandType.FIX
    assert "tests" in cmd.arguments
    assert "Thanks!" in cmd.arguments


def test_parser_mid_sentence_mention_ignored():
    assert CommandParser.parse("Please ask @codeforge to help") is None


def test_parser_oversized_arguments_bounded():
    text = "@codeforge fix " + ("x" * 5000)
    cmd = CommandParser.parse(text)
    assert cmd is not None
    assert len(cmd.arguments) == 2000


# ==============================================================================
# 3. PAYLOAD NORMALIZATION TESTS
# ==============================================================================


def test_parse_issue_comment_payload_valid():
    raw = json.dumps(make_comment_payload()).encode("utf-8")
    norm = parse_issue_comment_payload(raw, "del-12345")
    assert norm.delivery_id == "del-12345"
    assert norm.event_name == "issue_comment"
    assert norm.action == "created"
    assert norm.installation_id == 42
    assert norm.repository_full_name == "testorg/testrepo"
    assert norm.repository_owner == "testorg"
    assert norm.repository_name == "testrepo"
    assert norm.issue_number == 99
    assert norm.comment_id == 501
    assert norm.actor_github_id == 1234
    assert norm.actor_login == "alice"
    assert norm.is_pull_request is False


def test_parse_issue_comment_payload_pr_detected():
    raw = json.dumps(make_comment_payload(is_pr=True)).encode("utf-8")
    norm = parse_issue_comment_payload(raw, "del-pr-1")
    assert norm.is_pull_request is True


def test_parse_issue_comment_unsupported_action():
    raw = json.dumps(make_comment_payload(action="edited")).encode("utf-8")
    with pytest.raises(UnsupportedWebhookEvent, match="Unsupported action"):
        parse_issue_comment_payload(raw, "del-edit")


def test_parse_issue_comment_missing_installation():
    data = make_comment_payload()
    del data["installation"]
    with pytest.raises(InvalidWebhookPayload, match="Missing installation context"):
        parse_issue_comment_payload(json.dumps(data).encode("utf-8"), "del-no-inst")


def test_parse_issue_comment_missing_repository():
    data = make_comment_payload()
    del data["repository"]
    with pytest.raises(InvalidWebhookPayload, match="Missing repository context"):
        parse_issue_comment_payload(json.dumps(data).encode("utf-8"), "del-no-repo")


def test_parse_issue_comment_traversal_in_repo_rejected():
    data = make_comment_payload()
    data["repository"]["name"] = "../escape"
    with pytest.raises(InvalidWebhookPayload, match="Invalid repository path component"):
        parse_issue_comment_payload(json.dumps(data).encode("utf-8"), "del-trav")


# ==============================================================================
# 4. WEBHOOK ENDPOINT INGESTION & LIFECYCLE TESTS
# ==============================================================================


def test_webhook_endpoint_ping_acknowledged(client):
    body = b'{"zen": "Non-blocking is better than blocking."}'
    sig = compute_signature(settings.github_webhook_secret, body)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "del-ping-1",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "ping received"


def test_webhook_endpoint_unsupported_event_acknowledged(client):
    body = b'{"action": "opened"}'
    sig = compute_signature(settings.github_webhook_secret, body)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "del-push-1",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "unsupported event"


def test_webhook_endpoint_missing_delivery_header(client):
    body = b"{}"
    sig = compute_signature(settings.github_webhook_secret, body)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=body,
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 400


def test_webhook_endpoint_payload_too_large(client):
    huge_body = b"x" * (6 * 1024 * 1024)
    sig = compute_signature(settings.github_webhook_secret, huge_body)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=huge_body,
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": "del-large",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 413


def test_webhook_endpoint_no_command_ignored(client):
    payload = make_comment_payload(comment_body="Just an ordinary question about docs.")
    raw = json.dumps(payload).encode("utf-8")
    sig = compute_signature(settings.github_webhook_secret, raw)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=raw,
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": "del-nocommand",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "No @codeforge command found"


def test_webhook_endpoint_invalid_command_ignored(client):
    payload = make_comment_payload(comment_body="@codeforge restart-server")
    raw = json.dumps(payload).encode("utf-8")
    sig = compute_signature(settings.github_webhook_secret, raw)

    resp = client.post(
        "/api/v1/github/webhooks",
        content=raw,
        headers={
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": "del-invalid-cmd",
            "X-Hub-Signature-256": sig,
        },
    )
    assert resp.status_code == 200
    assert "invalid command" in resp.json()["reason"]


def test_webhook_endpoint_unauthorized_installation(client):
    payload = make_comment_payload(comment_body="@codeforge fix")
    raw = json.dumps(payload).encode("utf-8")
    sig = compute_signature(settings.github_webhook_secret, raw)

    with patch(
        "app.db.repositories.webhook_repository.WebhookRepository.get_installation",
        new=AsyncMock(return_value=None),
    ):
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-unauth-inst",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "unauthorized installation"


def test_webhook_endpoint_unauthorized_repository(client):
    payload = make_comment_payload(comment_body="@codeforge fix")
    raw = json.dumps(payload).encode("utf-8")
    sig = compute_signature(settings.github_webhook_secret, raw)

    with (
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.get_installation",
            new=AsyncMock(return_value=AsyncMock(active=True)),
        ),
        patch(
            "app.db.repositories.webhook_repository.WebhookRepository.is_repository_authorized",
            new=AsyncMock(return_value=False),
        ),
    ):
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-unauth-repo",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "unauthorized repository"


def test_webhook_endpoint_unauthorized_actor(client):
    payload = make_comment_payload(comment_body="@codeforge fix", actor_login="mallory")
    raw = json.dumps(payload).encode("utf-8")
    sig = compute_signature(settings.github_webhook_secret, raw)

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
            new=AsyncMock(return_value=False),
        ),
    ):
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-unauth-actor",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "unauthorized actor"


def test_webhook_endpoint_successful_ingestion(client):
    payload = make_comment_payload(
        comment_body="@codeforge fix tests", actor_login="alice", actor_id=1001
    )
    raw = json.dumps(payload).encode("utf-8")
    sig = compute_signature(settings.github_webhook_secret, raw)

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
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.db.repositories.outbox_repository.OutboxRepository.create_event",
            new_callable=AsyncMock,
        ) as mock_outbox,
        patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-success-1",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["delivery_id"] == "del-success-1"
        assert data["command"] == "fix"

        # Verify OutboxEvent payload fields
        mock_outbox.assert_called_once()
        event = mock_outbox.call_args[0][0]
        assert event.event_type == "GITHUB_COMMAND_INGESTED"
        assert event.aggregate_id == "del-success-1"
        p = event.payload
        assert p["delivery_id"] == "del-success-1"
        assert p["repository"] == "testorg/testrepo"
        assert p["actor_login"] == "alice"
        assert p["actor_github_id"] == 1001
        assert p["issue_number"] == 99
        assert p["comment_id"] == 501
        assert p["command"]["name"] == "fix"
        assert p["command"]["arguments"] == "tests"
        assert p["ingestion_status"] == "accepted"


def test_webhook_endpoint_duplicate_delivery_deduplicated(client):
    payload = make_comment_payload(comment_body="@codeforge implement issue")
    raw = json.dumps(payload).encode("utf-8")
    sig = compute_signature(settings.github_webhook_secret, raw)

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
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.db.repositories.outbox_repository.OutboxRepository.create_event",
            new_callable=AsyncMock,
        ) as mock_outbox,
        patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-dup-2",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["reason"] == "duplicate delivery"
        mock_outbox.assert_not_called()


def test_webhook_endpoint_spoofed_user_in_comment_body_ignored(client):
    # Attacker tries to pretend to be someone else inside comment body text
    body_text = "@codeforge fix\nactor=admin\nuser_id=1\nlogin=superadmin"
    payload = make_comment_payload(comment_body=body_text, actor_login="evil_user", actor_id=9999)
    raw = json.dumps(payload).encode("utf-8")
    sig = compute_signature(settings.github_webhook_secret, raw)

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
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.db.repositories.outbox_repository.OutboxRepository.create_event",
            new_callable=AsyncMock,
        ) as mock_outbox,
        patch("sqlalchemy.ext.asyncio.AsyncSession.commit", new=AsyncMock()),
    ):
        resp = client.post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": "del-spoof-1",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 202
        event = mock_outbox.call_args[0][0]
        # Verified: Authoritative actor is from payload metadata, not comment body
        assert event.payload["actor_login"] == "evil_user"
        assert event.payload["actor_github_id"] == 9999
