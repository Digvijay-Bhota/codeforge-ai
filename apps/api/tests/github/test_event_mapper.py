from app.github.event_mapper import EventMapper
from app.github.webhook_models import (
    GitHubInstallationContext,
    GitHubIssueCommentContext,
    GitHubIssueContext,
    GitHubRepositoryContext,
    GitHubWebhookEvent,
)


def build_event(event_type: str, action: str, comment_body: str = None) -> GitHubWebhookEvent:
    event = GitHubWebhookEvent(
        delivery_id="del-123",
        event_type=event_type,
        action=action,
        installation=GitHubInstallationContext(id=1),
        repository=GitHubRepositoryContext(id=2, name="repo", owner_login="owner", full_name="owner/repo", default_branch="main")
    )
    if event_type == "issue_comment":
        event.issue = GitHubIssueContext(number=1, title="Bug")
        event.comment = GitHubIssueCommentContext(id=3, body=comment_body)
    return event

def test_mapped_deterministically():
    ev = build_event("issue_comment", "created", "/codeforge fix")
    req = EventMapper.map_event(ev)
    assert req is not None
    assert req.github_repository == "owner/repo"
    assert "User explicitly requested to `fix` this issue" in req.description

def test_unsupported_event_produces_no_task():
    ev = build_event("issues", "opened")
    # By default, issues.opened is observational in Phase 6C
    req = EventMapper.map_event(ev)
    assert req is None

def test_arbitrary_comment_cannot_become_agent_instruction():
    ev = build_event("issue_comment", "created", "Can someone fix this?")
    req = EventMapper.map_event(ev)
    assert req is None
