from app.github.authorization import ExecutionAuthorizationPolicy, GitHubInstallationAuthorizer
from app.github.webhook_models import (
    GitHubInstallationContext,
    GitHubIssueCommentContext,
    GitHubRepositoryContext,
    GitHubWebhookEvent,
)


def build_event(install_id: int, full_name: str) -> GitHubWebhookEvent:
    return GitHubWebhookEvent(
        delivery_id="del-123",
        event_type="issues",
        action="opened",
        installation=GitHubInstallationContext(id=install_id),
        repository=GitHubRepositoryContext(id=2, name="repo", owner_login="owner", full_name=full_name, default_branch="main")
    )

def test_no_configured_installation_unauthorized():
    auth = GitHubInstallationAuthorizer()
    ev = build_event(999, "owner/repo")
    assert auth.authorize(ev) is False

def test_unknown_installation_unauthorized():
    auth = GitHubInstallationAuthorizer()
    ev = build_event(999, "owner/repo")
    assert auth.authorize(ev) is False

def test_known_installation_wrong_repo_unauthorized():
    auth = GitHubInstallationAuthorizer()
    ev = build_event(1, "owner/wrong-repo")
    assert auth.authorize(ev) is False

def test_known_installation_authorized_repo_accepted():
    auth = GitHubInstallationAuthorizer()
    ev = build_event(1, "owner/repo")
    assert auth.authorize(ev) is True

def test_execution_authorization_policy():
    ev_issue = build_event(1, "owner/repo")
    assert ExecutionAuthorizationPolicy.is_authorized_for_write(ev_issue) is False

    ev_comment = GitHubWebhookEvent(
        delivery_id="del-123",
        event_type="issue_comment",
        action="created",
        installation=GitHubInstallationContext(id=1),
        repository=GitHubRepositoryContext(id=2, name="repo", owner_login="owner", full_name="owner/repo", default_branch="main"),
        comment=GitHubIssueCommentContext(id=3, body="/codeforge fix")
    )
    assert ExecutionAuthorizationPolicy.is_authorized_for_write(ev_comment) is True
