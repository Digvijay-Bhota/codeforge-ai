"""GitHub webhook signature validation and payload normalization."""

import hashlib
import hmac
import json
import logging

from .webhook_models import (
    GitHubInstallationContext,
    GitHubIssueCommentContext,
    GitHubIssueContext,
    GitHubPullRequestContext,
    GitHubRepositoryContext,
    GitHubWebhookEvent,
)

logger = logging.getLogger(__name__)


class WebhookError(Exception):
    """Base exception for webhook processing errors."""
    pass


class InvalidWebhookSignature(WebhookError):
    pass


class UnsupportedWebhookEvent(WebhookError):
    pass


class InvalidWebhookPayload(WebhookError):
    pass


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub webhook signature using HMAC SHA-256."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_signature = signature_header[7:]  # Strip 'sha256='

    mac = hmac.new(
        secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256
    )
    return hmac.compare_digest(mac.hexdigest(), expected_signature)


def parse_webhook_payload(raw_body: bytes, event_type: str, delivery_id: str) -> GitHubWebhookEvent:
    """Parse and normalize a raw GitHub webhook payload into a typed internal event.

    Raises:
        InvalidWebhookPayload: If parsing fails or required fields are missing.
        UnsupportedWebhookEvent: If the event or action is not supported.
    """
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidWebhookPayload("Invalid JSON payload") from exc

    action = data.get("action")
    if not action:
        raise InvalidWebhookPayload("Missing action in payload")

    # Only support specific combinations
    supported = {
        "issues": ["opened"],
        "issue_comment": ["created"],
        "pull_request": ["opened", "synchronize", "reopened"],
    }

    if event_type not in supported or action not in supported[event_type]:
        raise UnsupportedWebhookEvent(f"Unsupported event/action: {event_type}.{action}")

    # Extract common contexts
    install_data = data.get("installation")
    if not install_data or "id" not in install_data:
        raise InvalidWebhookPayload("Missing installation context")

    repo_data = data.get("repository")
    if not repo_data or not all(k in repo_data for k in ("id", "name", "owner", "full_name", "default_branch")):
        raise InvalidWebhookPayload("Missing repository context")

    installation = GitHubInstallationContext(id=install_data["id"])
    repository = GitHubRepositoryContext(
        id=repo_data["id"],
        name=repo_data["name"],
        owner_login=repo_data["owner"]["login"],
        full_name=repo_data["full_name"],
        default_branch=repo_data["default_branch"]
    )

    event = GitHubWebhookEvent(
        delivery_id=delivery_id,
        event_type=event_type,
        action=action,
        installation=installation,
        repository=repository,
    )

    # Extract specific contexts safely
    if event_type == "issues":
        issue_data = data.get("issue")
        if not issue_data:
            raise InvalidWebhookPayload("Missing issue payload")
        event.issue = GitHubIssueContext(
            number=issue_data["number"],
            title=issue_data["title"][:255], # Bound title
            body=issue_data.get("body", "")[:10000] if issue_data.get("body") else None # Bound body
        )
    elif event_type == "issue_comment":
        issue_data = data.get("issue")
        comment_data = data.get("comment")
        if not issue_data or not comment_data:
            raise InvalidWebhookPayload("Missing issue or comment payload")
        event.issue = GitHubIssueContext(
            number=issue_data["number"],
            title=issue_data["title"][:255],
            body=issue_data.get("body", "")[:10000] if issue_data.get("body") else None
        )
        event.comment = GitHubIssueCommentContext(
            id=comment_data["id"],
            body=comment_data.get("body", "")[:10000] # Bound comment
        )
    elif event_type == "pull_request":
        pr_data = data.get("pull_request")
        if not pr_data:
            raise InvalidWebhookPayload("Missing pull_request payload")
        event.pull_request = GitHubPullRequestContext(
            number=pr_data["number"],
            title=pr_data["title"][:255],
            body=pr_data.get("body", "")[:10000] if pr_data.get("body") else None,
            head_ref=pr_data["head"]["ref"],
            base_ref=pr_data["base"]["ref"],
        )

    return event
