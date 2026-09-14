"""GitHub webhook signature validation and payload normalization."""

import hashlib
import hmac
import json
import logging

from .models import _validate_github_name
from .webhook_models import (
    GitHubInstallationContext,
    GitHubIssueCommentContext,
    GitHubIssueContext,
    GitHubPullRequestContext,
    GitHubRepositoryContext,
    GitHubWebhookEvent,
    NormalizedIssueCommentPayload,
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

    mac = hmac.new(secret.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256)
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
    if not repo_data or not all(
        k in repo_data for k in ("id", "name", "owner", "full_name", "default_branch")
    ):
        raise InvalidWebhookPayload("Missing repository context")

    installation = GitHubInstallationContext(id=install_data["id"])
    repository = GitHubRepositoryContext(
        id=repo_data["id"],
        name=repo_data["name"],
        owner_login=repo_data["owner"]["login"],
        full_name=repo_data["full_name"],
        default_branch=repo_data["default_branch"],
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
            title=issue_data["title"][:255],  # Bound title
            body=issue_data.get("body", "")[:10000]
            if issue_data.get("body")
            else None,  # Bound body
        )
    elif event_type == "issue_comment":
        issue_data = data.get("issue")
        comment_data = data.get("comment")
        if not issue_data or not comment_data:
            raise InvalidWebhookPayload("Missing issue or comment payload")
        event.issue = GitHubIssueContext(
            number=issue_data["number"],
            title=issue_data["title"][:255],
            body=issue_data.get("body", "")[:10000] if issue_data.get("body") else None,
        )
        event.comment = GitHubIssueCommentContext(
            id=comment_data["id"],
            body=comment_data.get("body", "")[:10000],  # Bound comment
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


def parse_issue_comment_payload(raw_body: bytes, delivery_id: str) -> NormalizedIssueCommentPayload:
    """Parse and normalize an issue_comment webhook payload into a typed representation.

    Raises:
        InvalidWebhookPayload: If payload is malformed or required contexts are missing.
        UnsupportedWebhookEvent: If action is not 'created'.
    """
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidWebhookPayload("Invalid JSON payload") from exc

    if not isinstance(data, dict):
        raise InvalidWebhookPayload("Payload must be a JSON object")

    action = data.get("action")
    if not action:
        raise InvalidWebhookPayload("Missing action in payload")

    if action != "created":
        raise UnsupportedWebhookEvent(f"Unsupported action for issue_comment: {action}")

    install_data = data.get("installation")
    if not install_data or not isinstance(install_data, dict) or "id" not in install_data:
        raise InvalidWebhookPayload("Missing installation context")

    try:
        install_id = int(install_data["id"])
    except (TypeError, ValueError) as exc:
        raise InvalidWebhookPayload("Invalid installation ID") from exc

    repo_data = data.get("repository")
    if not repo_data or not isinstance(repo_data, dict):
        raise InvalidWebhookPayload("Missing repository context")

    for field in ("id", "name", "owner", "full_name"):
        if field not in repo_data:
            raise InvalidWebhookPayload(f"Missing repository field: {field}")

    owner_obj = repo_data["owner"]
    if not isinstance(owner_obj, dict) or "login" not in owner_obj:
        raise InvalidWebhookPayload("Missing repository owner login")

    try:
        owner_login = _validate_github_name(str(owner_obj["login"]))
        repo_name = _validate_github_name(str(repo_data["name"]))
    except ValueError as exc:
        raise InvalidWebhookPayload(f"Invalid repository path component: {exc}") from exc

    repo_full_name = str(repo_data["full_name"])

    issue_data = data.get("issue")
    if not issue_data or not isinstance(issue_data, dict) or "number" not in issue_data:
        raise InvalidWebhookPayload("Missing issue context")

    try:
        issue_number = int(issue_data["number"])
    except (TypeError, ValueError) as exc:
        raise InvalidWebhookPayload("Invalid issue number") from exc

    comment_data = data.get("comment")
    if not comment_data or not isinstance(comment_data, dict) or "id" not in comment_data:
        raise InvalidWebhookPayload("Missing comment context")

    try:
        comment_id = int(comment_data["id"])
    except (TypeError, ValueError) as exc:
        raise InvalidWebhookPayload("Invalid comment ID") from exc

    comment_body = str(comment_data.get("body") or "")
    if len(comment_body) > 65535:
        comment_body = comment_body[:65535]

    # Authoritative actor extraction: check comment user, then fall back to sender
    comment_user = comment_data.get("user")
    sender_obj = data.get("sender")

    if isinstance(comment_user, dict) and "id" in comment_user and "login" in comment_user:
        try:
            actor_github_id = int(comment_user["id"])
        except (TypeError, ValueError) as exc:
            raise InvalidWebhookPayload("Invalid comment user ID") from exc
        actor_login = str(comment_user["login"])
    elif isinstance(sender_obj, dict) and "id" in sender_obj and "login" in sender_obj:
        try:
            actor_github_id = int(sender_obj["id"])
        except (TypeError, ValueError) as exc:
            raise InvalidWebhookPayload("Invalid sender ID") from exc
        actor_login = str(sender_obj["login"])
    else:
        raise InvalidWebhookPayload("Missing actor context in comment")

    sender_github_id = None
    sender_login = None
    if isinstance(sender_obj, dict):
        if "id" in sender_obj:
            try:
                sender_github_id = int(sender_obj["id"])
            except (TypeError, ValueError):
                pass
        if "login" in sender_obj:
            sender_login = str(sender_obj["login"])

    issue_title = str(issue_data["title"])[:255] if issue_data.get("title") else None
    issue_html_url = str(issue_data["html_url"]) if issue_data.get("html_url") else None
    is_pull_request = "pull_request" in issue_data

    head_sha = None
    pr_obj = issue_data.get("pull_request")
    if isinstance(pr_obj, dict):
        head_obj = pr_obj.get("head")
        if isinstance(head_obj, dict) and "sha" in head_obj:
            head_sha = str(head_obj["sha"])
        elif "head_sha" in pr_obj:
            head_sha = str(pr_obj["head_sha"])
    if not head_sha:
        raw_pr = data.get("pull_request")
        if isinstance(raw_pr, dict) and "head" in raw_pr and isinstance(raw_pr["head"], dict):
            head_sha = str(raw_pr["head"].get("sha") or "") or None

    return NormalizedIssueCommentPayload(
        delivery_id=delivery_id,
        event_name="issue_comment",
        action=action,
        installation_id=install_id,
        repository_id=int(repo_data["id"]),
        repository_full_name=repo_full_name,
        repository_owner=owner_login,
        repository_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_html_url=issue_html_url,
        is_pull_request=is_pull_request,
        head_sha=head_sha,
        comment_id=comment_id,
        comment_body=comment_body,
        actor_github_id=actor_github_id,
        actor_login=actor_login,
        sender_github_id=sender_github_id,
        sender_login=sender_login,
        created_at=str(comment_data.get("created_at")) if comment_data.get("created_at") else None,
    )
