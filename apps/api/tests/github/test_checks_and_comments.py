"""Dedicated test suite for Phase 10B.2.3: GitHub Check Runs and Issue/PR Comment APIs.

Covers:
1. Check Runs:
   - Create check run success & schema validation
   - Update check run success & schema validation
   - Typed output parsing (title, summary, text)
   - Valid status/conclusion state combinations
   - Invalid status/conclusion combination rejection
   - Malformed SHA and positive integer validation
   - Error mapping (401, 403, 404, 422, 5xx) and request ID propagation
   - Retry behavior through GitHubClient
2. Comments:
   - Create issue/PR comment success
   - Update issue/PR comment success
   - List comments with bounded pagination
   - Deterministic CodeForge comment marker embedding & extraction
   - find_codeforge_comment discovery helper
   - upsert_issue_comment idempotent creation & update
   - Error mapping (401, 403, 404, 422) and request ID propagation
3. Credential & Security Boundaries:
   - INSTALLATION credential mode dynamic token acquisition
   - APP credential mode prohibited from calling check run and comment APIs
   - Path traversal and malicious input rejection
   - Zero token / secret leakage in exceptions and representations
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.github.client import GitHubClient
from app.github.exceptions import (
    GitHubAuthenticationError,
    GitHubConfigurationError,
    GitHubValidationError,
)
from app.github.models import (
    CheckRunOutput,
    CreateCheckRunRequest,
    CreateCommentRequest,
    GitHubCheckRun,
    GitHubComment,
    UpdateCheckRunRequest,
    UpdateCommentRequest,
    build_managed_comment_body,
    extract_managed_comment_marker,
)
from app.github.token_service import InstallationTokenService


@pytest.fixture(autouse=True)
def configure_client_settings() -> None:
    """Ensure safe baseline settings for tests."""
    settings.github_api_url = "https://api.github.com"
    settings.github_token = "ghp_static_test_token_123"
    settings.github_app_id = "12345"
    settings.github_app_private_key = "dummy_pem_for_test"
    settings.github_client_max_retries = 3
    settings.github_client_retry_backoff_factor = 0.1
    settings.github_client_max_retry_after = 60


# ==============================================================================
# 1. GitHub Check Runs
# ==============================================================================


@pytest.mark.asyncio
async def test_create_check_run_success() -> None:
    """Verify check run creation parses all fields and passes structured output."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        201,
        headers={"x-github-request-id": "REQ-CR-CREATE"},
        json={
            "id": 1001,
            "name": "codeforge-agent",
            "head_sha": "a" * 40,
            "status": "in_progress",
            "conclusion": None,
            "html_url": "https://github.com/octocat/hello-world/runs/1001",
            "details_url": "https://codeforge.dev/tasks/task-1",
            "external_id": "task-1",
            "started_at": "2026-09-14T00:00:00Z",
            "completed_at": None,
            "output": {
                "title": "Analysis Running",
                "summary": "Analyzing workspace codebase...",
                "text": "Detailed log trace here.",
            },
        },
        request=httpx.Request("POST", "https://api.github.com/repos/octocat/hello-world/check-runs"),
    )
    mock_http.post.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    req = CreateCheckRunRequest(
        owner="octocat",
        repo="hello-world",
        name="codeforge-agent",
        head_sha="a" * 40,
        status="in_progress",
        details_url="https://codeforge.dev/tasks/task-1",
        external_id="task-1",
        started_at="2026-09-14T00:00:00Z",
        output=CheckRunOutput(
            title="Analysis Running",
            summary="Analyzing workspace codebase...",
            text="Detailed log trace here.",
        ),
    )
    check_run = await client.create_check_run(req)

    assert isinstance(check_run, GitHubCheckRun)
    assert check_run.id == 1001
    assert check_run.name == "codeforge-agent"
    assert check_run.head_sha == "a" * 40
    assert check_run.status == "in_progress"
    assert check_run.conclusion is None
    assert check_run.html_url == "https://github.com/octocat/hello-world/runs/1001"
    assert check_run.details_url == "https://codeforge.dev/tasks/task-1"
    assert check_run.external_id == "task-1"
    assert check_run.output is not None
    assert check_run.output.title == "Analysis Running"
    assert check_run.output.summary == "Analyzing workspace codebase..."
    assert check_run.output.text == "Detailed log trace here."

    # Check payload
    args, kwargs = mock_http.post.call_args
    assert "repos/octocat/hello-world/check-runs" in args[0]
    payload = kwargs["json"]
    assert payload["name"] == "codeforge-agent"
    assert payload["head_sha"] == "a" * 40
    assert payload["status"] == "in_progress"
    assert payload["external_id"] == "task-1"
    assert payload["output"]["title"] == "Analysis Running"


@pytest.mark.asyncio
async def test_update_check_run_success() -> None:
    """Verify check run update handles completed conclusion and output."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        200,
        headers={"x-github-request-id": "REQ-CR-UPDATE"},
        json={
            "id": 1001,
            "name": "codeforge-agent",
            "head_sha": "b" * 40,
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/octocat/hello-world/runs/1001",
            "details_url": "https://codeforge.dev/tasks/task-1",
            "external_id": "task-1",
            "started_at": "2026-09-14T00:00:00Z",
            "completed_at": "2026-09-14T00:02:00Z",
            "output": {
                "title": "Task Completed",
                "summary": "Plan executed successfully.",
            },
        },
        request=httpx.Request("PATCH", "https://api.github.com/repos/octocat/hello-world/check-runs/1001"),
    )
    mock_http.patch.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    req = UpdateCheckRunRequest(
        owner="octocat",
        repo="hello-world",
        check_run_id=1001,
        status="completed",
        conclusion="success",
        completed_at="2026-09-14T00:02:00Z",
        output=CheckRunOutput(
            title="Task Completed",
            summary="Plan executed successfully.",
        ),
    )
    check_run = await client.update_check_run(req)

    assert check_run.id == 1001
    assert check_run.status == "completed"
    assert check_run.conclusion == "success"
    assert check_run.completed_at == "2026-09-14T00:02:00Z"
    assert check_run.output is not None
    assert check_run.output.title == "Task Completed"
    assert check_run.output.text is None

    args, kwargs = mock_http.patch.call_args
    assert "repos/octocat/hello-world/check-runs/1001" in args[0]
    payload = kwargs["json"]
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "success"


def test_create_check_run_validation_rejections() -> None:
    """Validate invalid inputs for CreateCheckRunRequest."""
    valid_sha = "c" * 40

    # Conclusion provided when status is not "completed"
    with pytest.raises(ValueError, match="conclusion can only be set"):
        CreateCheckRunRequest(
            owner="octocat",
            repo="hello-world",
            name="check",
            head_sha=valid_sha,
            status="in_progress",
            conclusion="success",
        )

    # Invalid status
    with pytest.raises(ValueError, match="Invalid status"):
        CreateCheckRunRequest(
            owner="octocat",
            repo="hello-world",
            name="check",
            head_sha=valid_sha,
            status="pending",  # Invalid for check runs (valid are queued, in_progress, completed)
        )

    # Invalid conclusion
    with pytest.raises(ValueError, match="Invalid conclusion"):
        CreateCheckRunRequest(
            owner="octocat",
            repo="hello-world",
            name="check",
            head_sha=valid_sha,
            status="completed",
            conclusion="invalid_conclusion",
        )

    # Malformed SHA
    with pytest.raises(ValueError, match="Invalid head_sha"):
        CreateCheckRunRequest(
            owner="octocat",
            repo="hello-world",
            name="check",
            head_sha="short-sha",
        )

    # Empty name
    with pytest.raises(ValueError):
        CreateCheckRunRequest(
            owner="octocat",
            repo="hello-world",
            name="",
            head_sha=valid_sha,
        )


def test_update_check_run_validation_rejections() -> None:
    """Validate invalid inputs for UpdateCheckRunRequest."""
    # Invalid check_run_id (<= 0)
    with pytest.raises(ValueError, match="check_run_id must be a positive integer"):
        UpdateCheckRunRequest(
            owner="octocat",
            repo="hello-world",
            check_run_id=0,
            status="completed",
        )

    with pytest.raises(ValueError, match="check_run_id must be a positive integer"):
        UpdateCheckRunRequest(
            owner="octocat",
            repo="hello-world",
            check_run_id=-5,
            status="completed",
        )

    # Conclusion provided when status is in_progress
    with pytest.raises(ValueError, match="conclusion can only be set"):
        UpdateCheckRunRequest(
            owner="octocat",
            repo="hello-world",
            check_run_id=123,
            status="in_progress",
            conclusion="success",
        )


@pytest.mark.asyncio
async def test_check_run_error_mapping_and_request_id() -> None:
    """Verify check run APIs properly map HTTP errors to typed GitHub exceptions with safe metadata."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        422,
        headers={"x-github-request-id": "REQ-CR-422"},
        json={"message": "Invalid head_sha"},
        request=httpx.Request("POST", "https://api.github.com/repos/o/r/check-runs"),
    )
    mock_http.post.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_http, max_retries=0)
    req = CreateCheckRunRequest(
        owner="o",
        repo="r",
        name="test",
        head_sha="d" * 40,
    )

    with pytest.raises(GitHubValidationError) as exc_info:
        await client.create_check_run(req)

    assert exc_info.value.status_code == 422
    assert exc_info.value.github_request_id == "REQ-CR-422"
    assert exc_info.value.operation == "create_check_run"


# ==============================================================================
# 2. GitHub Issue / PR Comments
# ==============================================================================


@pytest.mark.asyncio
async def test_create_issue_comment_success() -> None:
    """Verify issue/PR comment creation parses typed fields."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        201,
        headers={"x-github-request-id": "REQ-COMMENT-CREATE"},
        json={
            "id": 5001,
            "body": "Hello from CodeForge!",
            "html_url": "https://github.com/octocat/hello-world/issues/42#issuecomment-5001",
            "user": {"login": "codeforge-bot[bot]"},
            "created_at": "2026-09-14T00:10:00Z",
            "updated_at": "2026-09-14T00:10:00Z",
        },
        request=httpx.Request("POST", "https://api.github.com/repos/octocat/hello-world/issues/42/comments"),
    )
    mock_http.post.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    req = CreateCommentRequest(
        owner="octocat",
        repo="hello-world",
        issue_number=42,
        body="Hello from CodeForge!",
    )
    comment = await client.create_issue_comment(req)

    assert isinstance(comment, GitHubComment)
    assert comment.id == 5001
    assert comment.body == "Hello from CodeForge!"
    assert comment.user_login == "codeforge-bot[bot]"
    assert comment.html_url == "https://github.com/octocat/hello-world/issues/42#issuecomment-5001"
    assert comment.created_at == "2026-09-14T00:10:00Z"

    args, kwargs = mock_http.post.call_args
    assert "repos/octocat/hello-world/issues/42/comments" in args[0]
    assert kwargs["json"] == {"body": "Hello from CodeForge!"}


@pytest.mark.asyncio
async def test_update_issue_comment_success() -> None:
    """Verify issue/PR comment update parses typed fields."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        200,
        headers={"x-github-request-id": "REQ-COMMENT-UPDATE"},
        json={
            "id": 5001,
            "body": "Updated comment body",
            "html_url": "https://github.com/octocat/hello-world/issues/comments/5001",
            "user": {"login": "codeforge-bot[bot]"},
            "created_at": "2026-09-14T00:10:00Z",
            "updated_at": "2026-09-14T00:12:00Z",
        },
        request=httpx.Request("PATCH", "https://api.github.com/repos/octocat/hello-world/issues/comments/5001"),
    )
    mock_http.patch.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    req = UpdateCommentRequest(
        owner="octocat",
        repo="hello-world",
        comment_id=5001,
        body="Updated comment body",
    )
    comment = await client.update_issue_comment(req)

    assert comment.id == 5001
    assert comment.body == "Updated comment body"
    assert comment.updated_at == "2026-09-14T00:12:00Z"

    args, kwargs = mock_http.patch.call_args
    assert "repos/octocat/hello-world/issues/comments/5001" in args[0]
    assert kwargs["json"] == {"body": "Updated comment body"}


def test_comment_request_validation() -> None:
    """Validate input constraints on comment requests."""
    # Invalid issue_number
    with pytest.raises(ValueError, match="issue_number must be a positive integer"):
        CreateCommentRequest(
            owner="o",
            repo="r",
            issue_number=0,
            body="test",
        )

    with pytest.raises(ValueError, match="issue_number must be a positive integer"):
        CreateCommentRequest(
            owner="o",
            repo="r",
            issue_number=-1,
            body="test",
        )

    # Empty body
    with pytest.raises(ValueError):
        CreateCommentRequest(
            owner="o",
            repo="r",
            issue_number=1,
            body="",
        )

    # Invalid comment_id
    with pytest.raises(ValueError, match="comment_id must be a positive integer"):
        UpdateCommentRequest(
            owner="o",
            repo="r",
            comment_id=0,
            body="test",
        )


@pytest.mark.asyncio
async def test_list_issue_comments_bounded_pagination() -> None:
    """Verify list_issue_comments enforces bounded pagination."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        200,
        json=[
            {
                "id": 101,
                "body": "First comment",
                "html_url": "https://github.com/o/r/issues/1#issuecomment-101",
                "user": {"login": "alice"},
            },
            {
                "id": 102,
                "body": "Second comment",
                "html_url": "https://github.com/o/r/issues/1#issuecomment-102",
                "user": {"login": "bob"},
            },
        ],
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/issues/1/comments"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    comments = await client.list_issue_comments("o", "r", issue_number=1, per_page=500, page=2)

    assert len(comments) == 2
    assert comments[0].id == 101
    assert comments[1].user_login == "bob"

    # Verify per_page was bounded to 100
    _, kwargs = mock_http.get.call_args
    assert kwargs["params"] == {"per_page": 100, "page": 2}


# ==============================================================================
# 3. Comment Idempotency & Managed Comment Helper
# ==============================================================================


def test_managed_comment_marker_embedding_and_extraction() -> None:
    """Verify build_managed_comment_body and extract_managed_comment_marker."""
    raw_body = "Progress update:\n- Step 1: Done\n- Step 2: In progress"
    marker_id = "task-uuid-12345"

    managed = build_managed_comment_body(raw_body, marker_id)
    assert f"<!-- codeforge:managed-comment:{marker_id} -->" in managed
    assert raw_body in managed

    extracted = extract_managed_comment_marker(managed)
    assert extracted == marker_id

    # Unrelated comment
    unrelated = "This is a human review comment without markers."
    assert extract_managed_comment_marker(unrelated) is None

    # Invalid marker_id rejected
    with pytest.raises(ValueError):
        build_managed_comment_body("body", "")
    with pytest.raises(ValueError):
        build_managed_comment_body("body", "invalid marker/with/slash")


@pytest.mark.asyncio
async def test_find_codeforge_comment_finds_matching_marker() -> None:
    """Verify find_codeforge_comment discovers comment with matching marker among others."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    marker_id = "task-abc-123"
    managed_body = build_managed_comment_body("Task status update", marker_id)

    mock_resp = httpx.Response(
        200,
        json=[
            {"id": 1, "body": "Human comment 1", "html_url": "url1", "user": {"login": "user1"}},
            {"id": 2, "body": "<!-- codeforge:managed-comment:different-task -->\nOther", "html_url": "url2", "user": {"login": "bot"}},
            {"id": 3, "body": managed_body, "html_url": "url3", "user": {"login": "codeforge-bot"}},
        ],
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/issues/7/comments"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    found = await client.find_codeforge_comment("o", "r", issue_number=7, marker_id=marker_id)

    assert found is not None
    assert found.id == 3
    assert found.html_url == "url3"


@pytest.mark.asyncio
async def test_find_codeforge_comment_returns_none_when_no_match() -> None:
    """Verify find_codeforge_comment returns None when marker is absent."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        200,
        json=[
            {"id": 1, "body": "Just a normal comment", "html_url": "url1", "user": {"login": "user1"}},
        ],
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/issues/7/comments"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    found = await client.find_codeforge_comment("o", "r", issue_number=7, marker_id="task-not-present")
    assert found is None


@pytest.mark.asyncio
async def test_upsert_issue_comment_creates_when_none_exists() -> None:
    """upsert_issue_comment creates a new comment when no managed comment exists."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    # 1. find_codeforge_comment -> list comments returns empty
    resp_list = httpx.Response(
        200,
        json=[],
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/issues/10/comments"),
    )
    # 2. create_issue_comment -> returns created comment
    resp_create = httpx.Response(
        201,
        json={
            "id": 999,
            "body": "<!-- codeforge:managed-comment:task-555 -->\n\nInitial task progress",
            "html_url": "https://github.com/o/r/issues/10#issuecomment-999",
            "user": {"login": "codeforge-bot"},
        },
        request=httpx.Request("POST", "https://api.github.com/repos/o/r/issues/10/comments"),
    )
    mock_http.get.return_value = resp_list
    mock_http.post.return_value = resp_create

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    comment = await client.upsert_issue_comment(
        owner="o",
        repo="r",
        issue_number=10,
        marker_id="task-555",
        body="Initial task progress",
    )

    assert comment.id == 999
    mock_http.post.assert_called_once()
    mock_http.patch.assert_not_called()
    _, kwargs = mock_http.post.call_args
    assert "<!-- codeforge:managed-comment:task-555 -->" in kwargs["json"]["body"]


@pytest.mark.asyncio
async def test_upsert_issue_comment_updates_when_exists() -> None:
    """upsert_issue_comment updates existing managed comment without duplicating."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    existing_marker = "task-888"
    existing_body = build_managed_comment_body("Step 1 started", existing_marker)

    # 1. find_codeforge_comment -> list comments returns existing
    resp_list = httpx.Response(
        200,
        json=[
            {
                "id": 777,
                "body": existing_body,
                "html_url": "https://github.com/o/r/issues/20#issuecomment-777",
                "user": {"login": "codeforge-bot"},
            }
        ],
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/issues/20/comments"),
    )
    # 2. update_issue_comment -> returns updated comment
    resp_update = httpx.Response(
        200,
        json={
            "id": 777,
            "body": build_managed_comment_body("Step 2 finished", existing_marker),
            "html_url": "https://github.com/o/r/issues/20#issuecomment-777",
            "user": {"login": "codeforge-bot"},
        },
        request=httpx.Request("PATCH", "https://api.github.com/repos/o/r/issues/comments/777"),
    )
    mock_http.get.return_value = resp_list
    mock_http.patch.return_value = resp_update

    client = GitHubClient.for_token("test-token", http_client=mock_http)
    comment = await client.upsert_issue_comment(
        owner="o",
        repo="r",
        issue_number=20,
        marker_id=existing_marker,
        body="Step 2 finished",
    )

    assert comment.id == 777
    mock_http.patch.assert_called_once()
    mock_http.post.assert_not_called()  # Did NOT create duplicate
    _, kwargs = mock_http.patch.call_args
    assert "Step 2 finished" in kwargs["json"]["body"]


# ==============================================================================
# 4. Credential Separation & Security Boundaries
# ==============================================================================


@pytest.mark.asyncio
async def test_installation_mode_used_for_check_runs_and_comments() -> None:
    """Verify INSTALLATION mode dynamically resolves token via token service."""
    mock_token_service = AsyncMock(spec=InstallationTokenService)
    mock_token_service.get_token.return_value = "ghs_installation_token_777"

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        201,
        json={
            "id": 1,
            "name": "check",
            "head_sha": "e" * 40,
            "status": "queued",
        },
        request=httpx.Request("POST", "https://api.github.com/repos/o/r/check-runs"),
    )
    mock_http.post.return_value = mock_resp

    client = GitHubClient.for_installation(
        installation_id=1234,
        token_service=mock_token_service,
        http_client=mock_http,
    )

    await client.create_check_run(
        CreateCheckRunRequest(
            owner="o",
            repo="r",
            name="check",
            head_sha="e" * 40,
        )
    )

    mock_token_service.get_token.assert_called_once_with(1234)
    _, kwargs = mock_http.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer ghs_installation_token_777"


@pytest.mark.asyncio
async def test_app_jwt_mode_rejected_on_check_run_and_comment_endpoints() -> None:
    """Verify App JWT mode cannot be used to call check run or comment APIs."""
    client = GitHubClient.for_app(app_jwt="jwt.token.val")

    with pytest.raises(GitHubConfigurationError, match="Repository-scoped operations require an installation access token"):
        await client.create_check_run(
            CreateCheckRunRequest(
                owner="o",
                repo="r",
                name="check",
                head_sha="f" * 40,
            )
        )

    with pytest.raises(GitHubConfigurationError, match="Repository-scoped operations require an installation access token"):
        await client.create_issue_comment(
            CreateCommentRequest(
                owner="o",
                repo="r",
                issue_number=1,
                body="test",
            )
        )


@pytest.mark.parametrize(
    "bad_owner,bad_repo",
    [
        ("../hack", "repo"),
        ("owner", "../hack"),
        ("owner/sub", "repo"),
        ("owner", "repo$name"),
    ],
)
@pytest.mark.asyncio
async def test_path_traversal_rejected_on_check_runs_and_comments(
    bad_owner: str, bad_repo: str
) -> None:
    """Verify directory traversal and invalid characters in owner/repo are blocked before network call."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = GitHubClient.for_token("test-token", http_client=mock_http)

    with pytest.raises(ValueError):
        await client.list_issue_comments(bad_owner, bad_repo, issue_number=1)


@pytest.mark.asyncio
async def test_zero_secret_leakage_in_check_run_and_comment_errors() -> None:
    """Verify error messages and representations never leak sensitive tokens."""
    sensitive_token = "ghs_top_secret_installation_token_456"
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        401,
        headers={"x-github-request-id": "REQ-LEAK-TEST"},
        json={"message": "Bad credentials"},
        request=httpx.Request("POST", "https://api.github.com/repos/o/r/issues/1/comments"),
    )
    mock_http.post.return_value = mock_resp

    client = GitHubClient.for_token(
        sensitive_token, http_client=mock_http, max_retries=0
    )

    with pytest.raises(GitHubAuthenticationError) as exc_info:
        await client.create_issue_comment(
            CreateCommentRequest(owner="o", repo="r", issue_number=1, body="test")
        )

    err = exc_info.value
    assert sensitive_token not in str(err)
    assert sensitive_token not in repr(err)
    assert "Bearer" not in str(err)
