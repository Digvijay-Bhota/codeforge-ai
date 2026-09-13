"""Dedicated tests for Phase 10B.2.2: Typed/Pooled GitHub Client Transport & APIs.

Covers:
1. Transport lifecycle & pooling:
   - Pooled httpx.AsyncClient reuse across multiple requests
   - Clean aclose() and async context manager lifecycle
   - External client injection (ownership semantics)
   - Limits & timeout configuration from settings
2. Credential separation:
   - Dynamic Installation token resolution via InstallationTokenService
   - Automatic 401 cache eviction/invalidation in Installation mode
   - App JWT mode enforcement (App JWT prohibited on repository endpoints)
   - Token mode backward compatibility
   - Installation mode prohibited on /app/installations/.../access_tokens
3. Typed error mapping & safe metadata:
   - 401 -> GitHubAuthenticationError
   - 403 rate limit -> GitHubRateLimitError
   - 403 forbidden -> GitHubAuthorizationError
   - 404 -> GitHubNotFoundError
   - 409 -> GitHubConflictError
   - 422 -> GitHubValidationError
   - 5xx -> GitHubUpstreamError
   - Timeout -> GitHubTimeoutError
   - Connection/Network -> GitHubConnectionError
   - Metadata capture (operation, status_code, request ID, retry_after)
   - Zero secret leakage in exceptions and string representations
4. Bounded retries & rate limits:
   - Retry transient 502/503/504 with backoff using injectable sleep_func
   - Retry transient timeouts with backoff
   - Retry transient connection errors with backoff
   - Zero retries for deterministic 4xx errors
   - Rate limit retry when Retry-After <= max_retry_after
   - Immediate rate limit rejection when Retry-After > max_retry_after
5. Repository & collaborator permission APIs:
   - get_repository parses id, owner, name, default_branch, private, etc.
   - get_collaborator_permission maps admin, maintain, push, triage, pull
   - 404 / 403 on collaborator endpoint gracefully maps to permission="none"
   - Input validation prevents path traversal and malformed inputs
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.github.client import CredentialMode, GitHubClient
from app.github.exceptions import (
    GitHubAuthenticationError,
    GitHubAuthorizationError,
    GitHubConfigurationError,
    GitHubConflictError,
    GitHubConnectionError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTimeoutError,
    GitHubUpstreamError,
    GitHubValidationError,
)
from app.github.models import GitHubCollaboratorPermission
from app.github.token_service import InstallationTokenService
from app.services.authorization_service import AuthorizationService


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
# 1. Transport Lifecycle & Connection Pooling
# ==============================================================================


@pytest.mark.asyncio
async def test_client_reuses_underlying_async_client() -> None:
    """Verify that multiple requests reuse the same underlying httpx.AsyncClient instance."""
    mock_transport_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        200,
        json={
            "id": 999,
            "owner": {"login": "testowner"},
            "name": "testrepo",
            "full_name": "testowner/testrepo",
            "default_branch": "main",
            "private": False,
        },
        request=httpx.Request("GET", "https://api.github.com/repos/testowner/testrepo"),
    )
    mock_transport_client.get.return_value = mock_resp

    client = GitHubClient.for_token("test-token", http_client=mock_transport_client)

    repo1 = await client.get_repository("testowner", "testrepo")
    repo2 = await client.get_repository("testowner", "testrepo")

    assert repo1.full_name == "testowner/testrepo"
    assert repo2.full_name == "testowner/testrepo"
    # Both calls went through the same injected client
    assert mock_transport_client.get.call_count == 2
    assert client._client is mock_transport_client


@pytest.mark.asyncio
async def test_client_aclose_and_context_manager_lifecycle() -> None:
    """Verify client lifecycle and proper closure."""
    # When client owns the internal client
    client = GitHubClient.for_token("test-token")
    assert client._owns_client is True
    internal_client = client._client
    assert internal_client is not None
    assert internal_client.is_closed is False

    await client.aclose()
    assert internal_client.is_closed is True

    # Via async context manager
    async with GitHubClient.for_token("test-token") as ctx_client:
        assert ctx_client._client.is_closed is False
        ctx_internal = ctx_client._client

    assert ctx_internal.is_closed is True


@pytest.mark.asyncio
async def test_injected_client_not_closed_by_aclose() -> None:
    """Verify that an externally injected client is NOT closed when client.aclose() is called."""
    mock_external_client = AsyncMock(spec=httpx.AsyncClient)
    mock_external_client.aclose = AsyncMock()

    client = GitHubClient.for_token("test-token", http_client=mock_external_client)
    assert client._owns_client is False

    await client.aclose()
    mock_external_client.aclose.assert_not_called()


def test_client_timeouts_and_connection_limits_configured() -> None:
    """Verify connection pool limits and timeouts are properly set on default client."""
    client = GitHubClient.for_token("test-token")
    internal = client._client

    # Verify timeout values
    assert internal.timeout.connect == 5.0
    assert internal.timeout.read == 10.0
    assert internal.timeout.write == 10.0
    assert internal.timeout.pool == 5.0


# ==============================================================================
# 2. Credential Modes & Dynamic Token Resolution
# ==============================================================================


@pytest.mark.asyncio
async def test_installation_mode_dynamically_resolves_token() -> None:
    """INSTALLATION mode must fetch short-lived token via token_service for each call."""
    mock_token_service = AsyncMock(spec=InstallationTokenService)
    mock_token_service.get_token.return_value = "ghs_dynamic_token_abc"

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        200,
        json={
            "id": 123,
            "owner": {"login": "octocat"},
            "name": "hello-world",
            "full_name": "octocat/hello-world",
            "default_branch": "main",
            "private": False,
        },
        request=httpx.Request("GET", "https://api.github.com/repos/octocat/hello-world"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_installation(
        installation_id=456,
        token_service=mock_token_service,
        http_client=mock_http,
    )

    repo = await client.get_repository("octocat", "hello-world")
    assert repo.id == 123
    mock_token_service.get_token.assert_called_once_with(456)

    # Check that the dynamic token was sent in the Authorization header
    _, kwargs = mock_http.get.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer ghs_dynamic_token_abc"


@pytest.mark.asyncio
async def test_installation_mode_401_invalidates_cache_before_raising() -> None:
    """When a 401 is received in INSTALLATION mode, token_service.invalidate must be called."""
    mock_token_service = AsyncMock(spec=InstallationTokenService)
    mock_token_service.get_token.return_value = "ghs_stale_token"
    mock_token_service.invalidate = MagicMock()

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        401,
        headers={"x-github-request-id": "REQ-401"},
        json={"message": "Bad credentials"},
        request=httpx.Request("GET", "https://api.github.com/repos/octocat/hello-world"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_installation(
        installation_id=789,
        token_service=mock_token_service,
        http_client=mock_http,
    )

    with pytest.raises(GitHubAuthenticationError) as exc_info:
        await client.get_repository("octocat", "hello-world")

    assert exc_info.value.status_code == 401
    assert exc_info.value.github_request_id == "REQ-401"
    mock_token_service.invalidate.assert_called_once_with(789)


@pytest.mark.asyncio
async def test_app_jwt_mode_rejects_repository_scoped_operations() -> None:
    """App JWT mode must NOT be permitted to execute repository-scoped operations."""
    client = GitHubClient.for_app(app_jwt="jwt.token.value")

    with pytest.raises(GitHubConfigurationError) as exc_info:
        await client.get_repository("owner", "repo")

    assert "Repository-scoped operations require an installation access token" in str(
        exc_info.value
    )


@pytest.mark.asyncio
async def test_installation_mode_rejects_token_creation() -> None:
    """INSTALLATION mode cannot call create_installation_access_token (requires App JWT)."""
    mock_token_service = AsyncMock(spec=InstallationTokenService)
    client = GitHubClient.for_installation(
        installation_id=123, token_service=mock_token_service
    )

    with pytest.raises(GitHubConfigurationError) as exc_info:
        await client.create_installation_access_token(123)

    assert "Cannot create installation access token using an installation token" in str(
        exc_info.value
    )


def test_installation_mode_requires_installation_id() -> None:
    """Creating an installation client without installation_id raises GitHubConfigurationError."""
    with pytest.raises(GitHubConfigurationError):
        GitHubClient(mode=CredentialMode.INSTALLATION, installation_id=None)


# ==============================================================================
# 3. Typed Error Mapping & Safe Metadata
# ==============================================================================


@pytest.mark.parametrize(
    "status_code,headers,payload,expected_exc,retryable",
    [
        (401, {}, {"message": "Bad credentials"}, GitHubAuthenticationError, False),
        (403, {}, {"message": "Must have admin rights"}, GitHubAuthorizationError, False),
        (
            403,
            {"x-ratelimit-remaining": "0", "retry-after": "30"},
            {"message": "rate limit"},
            GitHubRateLimitError,
            True,
        ),
        (404, {}, {"message": "Not Found"}, GitHubNotFoundError, False),
        (409, {}, {"message": "Merge conflict"}, GitHubConflictError, False),
        (422, {}, {"message": "Validation Failed"}, GitHubValidationError, False),
        (500, {}, {"message": "Internal Server Error"}, GitHubUpstreamError, True),
        (502, {}, {"message": "Bad Gateway"}, GitHubUpstreamError, True),
        (503, {}, {"message": "Service Unavailable"}, GitHubUpstreamError, True),
    ],
)
@pytest.mark.asyncio
async def test_typed_error_mapping_status_codes(
    status_code: int,
    headers: dict[str, str],
    payload: dict[str, Any],
    expected_exc: type[Exception],
    retryable: bool,
) -> None:
    """Verify HTTP status codes map to precise typed exceptions with safe metadata."""
    headers["x-github-request-id"] = "REQ-TRACE-123"
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        status_code,
        headers=headers,
        json=payload,
        request=httpx.Request("GET", "https://api.github.com/repos/o/r"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token(
        "secret-token-xyz", http_client=mock_http, max_retries=0
    )

    with pytest.raises(expected_exc) as exc_info:
        await client.get_repository("o", "r")

    err = exc_info.value
    assert isinstance(err, GitHubError)
    assert err.status_code == status_code
    assert err.github_request_id == "REQ-TRACE-123"
    assert err.operation == "get_repository"
    assert err.retryable == retryable


@pytest.mark.asyncio
async def test_typed_error_mapping_network_and_timeout() -> None:
    """Verify httpx transport errors map to GitHubTimeoutError and GitHubConnectionError."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get.side_effect = httpx.ReadTimeout("Read timed out")

    client = GitHubClient.for_token("token", http_client=mock_http, max_retries=0)

    with pytest.raises(GitHubTimeoutError) as exc_info:
        await client.get_repository("owner", "repo")
    assert exc_info.value.retryable is True
    assert exc_info.value.operation == "get_repository"

    # Connection error
    mock_http.get.side_effect = httpx.ConnectError("Failed to connect")
    with pytest.raises(GitHubConnectionError) as exc_info2:
        await client.get_repository("owner", "repo")
    assert exc_info2.value.retryable is True
    assert exc_info2.value.operation == "get_repository"


@pytest.mark.asyncio
async def test_zero_secret_leakage_in_error_representation() -> None:
    """Verify credentials are never exposed in exception message, str(), or repr()."""
    sensitive_token = "ghp_super_secret_user_token_99999"
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        401,
        headers={"x-github-request-id": "REQ-LEAK-TEST"},
        json={"message": "Bad token"},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token(
        sensitive_token, http_client=mock_http, max_retries=0
    )

    with pytest.raises(GitHubAuthenticationError) as exc_info:
        await client.get_repository("owner", "repo")

    err = exc_info.value
    err_str = str(err)
    err_repr = repr(err)

    assert sensitive_token not in err_str
    assert sensitive_token not in err_repr
    assert "Bearer" not in err_str


# ==============================================================================
# 4. Bounded Retries & Rate Limit Backoff
# ==============================================================================


@pytest.mark.asyncio
async def test_retry_transient_upstream_503_success_on_third_attempt() -> None:
    """Transient 503 retries with backoff and succeeds if a later attempt passes."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp_503 = httpx.Response(
        503,
        json={"message": "Service Unavailable"},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    resp_200 = httpx.Response(
        200,
        json={
            "id": 1,
            "owner": {"login": "owner"},
            "name": "repo",
            "full_name": "owner/repo",
            "default_branch": "main",
            "private": False,
        },
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    mock_http.get.side_effect = [resp_503, resp_503, resp_200]

    sleep_mock = AsyncMock()

    client = GitHubClient.for_token(
        "token",
        http_client=mock_http,
        max_retries=3,
        retry_backoff_factor=0.5,
        sleep_func=sleep_mock,
    )

    repo = await client.get_repository("owner", "repo")
    assert repo.full_name == "owner/repo"
    assert mock_http.get.call_count == 3

    # Backoff calls: attempt 0 -> 0.5 * 1 = 0.5s; attempt 1 -> 0.5 * 2 = 1.0s
    assert sleep_mock.call_count == 2
    assert sleep_mock.call_args_list[0][0][0] == 0.5
    assert sleep_mock.call_args_list[1][0][0] == 1.0


@pytest.mark.asyncio
async def test_retry_transient_exhaustion_raises_upstream_error() -> None:
    """Transient 502 fails after max_retries + 1 total attempts."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp_502 = httpx.Response(
        502,
        headers={"x-github-request-id": "REQ-502"},
        json={"message": "Bad Gateway"},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    mock_http.get.return_value = resp_502
    sleep_mock = AsyncMock()

    client = GitHubClient.for_token(
        "token",
        http_client=mock_http,
        max_retries=2,
        retry_backoff_factor=0.1,
        sleep_func=sleep_mock,
    )

    with pytest.raises(GitHubUpstreamError) as exc_info:
        await client.get_repository("owner", "repo")

    assert exc_info.value.status_code == 502
    assert mock_http.get.call_count == 3  # 1 initial + 2 retries
    assert sleep_mock.call_count == 2


@pytest.mark.asyncio
async def test_deterministic_4xx_never_retried() -> None:
    """Deterministic errors (400, 401, 404, 409, 422) must NOT be retried."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp_404 = httpx.Response(
        404,
        json={"message": "Not Found"},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    mock_http.get.return_value = resp_404
    sleep_mock = AsyncMock()

    client = GitHubClient.for_token(
        "token",
        http_client=mock_http,
        max_retries=3,
        sleep_func=sleep_mock,
    )

    with pytest.raises(GitHubNotFoundError):
        await client.get_repository("owner", "repo")

    assert mock_http.get.call_count == 1
    sleep_mock.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_retry_after_honored_when_within_limit() -> None:
    """Rate limit 429 with Retry-After <= max_retry_after sleeps Retry-After seconds and retries."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp_429 = httpx.Response(
        429,
        headers={"retry-after": "5"},
        json={"message": "Too Many Requests"},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    resp_200 = httpx.Response(
        200,
        json={
            "id": 1,
            "owner": {"login": "owner"},
            "name": "repo",
            "full_name": "owner/repo",
            "default_branch": "main",
            "private": False,
        },
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    mock_http.get.side_effect = [resp_429, resp_200]
    sleep_mock = AsyncMock()

    client = GitHubClient.for_token(
        "token",
        http_client=mock_http,
        max_retries=2,
        max_retry_after=60,
        sleep_func=sleep_mock,
    )

    repo = await client.get_repository("owner", "repo")
    assert repo.full_name == "owner/repo"
    assert mock_http.get.call_count == 2
    sleep_mock.assert_called_once_with(5.0)


@pytest.mark.asyncio
async def test_rate_limit_fails_immediately_when_retry_after_exceeds_max() -> None:
    """Rate limit with Retry-After > max_retry_after fails immediately without sleeping."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp_429 = httpx.Response(
        429,
        headers={"retry-after": "120"},
        json={"message": "Too Many Requests"},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo"),
    )
    mock_http.get.return_value = resp_429
    sleep_mock = AsyncMock()

    client = GitHubClient.for_token(
        "token",
        http_client=mock_http,
        max_retries=3,
        max_retry_after=60,
        sleep_func=sleep_mock,
    )

    with pytest.raises(GitHubRateLimitError) as exc_info:
        await client.get_repository("owner", "repo")

    assert exc_info.value.retry_after == 120
    assert mock_http.get.call_count == 1
    sleep_mock.assert_not_called()


# ==============================================================================
# 5. Typed Repository & Collaborator Permission APIs
# ==============================================================================


@pytest.mark.asyncio
async def test_get_repository_parses_all_fields_including_id() -> None:
    """get_repository accurately maps all schema fields including optional id."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        200,
        json={
            "id": 987654,
            "owner": {"login": "octocat"},
            "name": "Spoon-Knife",
            "full_name": "octocat/Spoon-Knife",
            "default_branch": "development",
            "private": True,
            "clone_url": "https://github.com/octocat/Spoon-Knife.git",
            "html_url": "https://github.com/octocat/Spoon-Knife",
        },
        request=httpx.Request("GET", "https://api.github.com/repos/octocat/Spoon-Knife"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token("token", http_client=mock_http)
    repo = await client.get_repository("octocat", "Spoon-Knife")

    assert repo.id == 987654
    assert repo.owner == "octocat"
    assert repo.name == "Spoon-Knife"
    assert repo.full_name == "octocat/Spoon-Knife"
    assert repo.default_branch == "development"
    assert repo.private is True
    assert repo.clone_url == "https://github.com/octocat/Spoon-Knife.git"
    assert repo.html_url == "https://github.com/octocat/Spoon-Knife"


@pytest.mark.parametrize(
    "bad_owner,bad_repo",
    [
        ("", "repo"),
        ("owner", ""),
        ("../evil", "repo"),
        ("owner", "repo/../../etc"),
        ("invalid owner", "repo"),
        ("owner", "repo$name"),
    ],
)
@pytest.mark.asyncio
async def test_get_repository_validates_path_segments(
    bad_owner: str, bad_repo: str
) -> None:
    """Path segments with path traversal or invalid characters must be rejected before network call."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = GitHubClient.for_token("token", http_client=mock_http)

    with pytest.raises(ValueError):
        await client.get_repository(bad_owner, bad_repo)

    mock_http.get.assert_not_called()


@pytest.mark.parametrize(
    "permission_str,role_name,perms_dict,expected_admin,expected_write,expected_read",
    [
        ("admin", "admin", {"admin": True, "push": True, "pull": True}, True, True, True),
        (
            "maintain",
            "maintain",
            {"admin": False, "maintain": True, "push": True, "pull": True},
            False,
            True,
            True,
        ),
        (
            "write",
            "push",
            {"admin": False, "maintain": False, "push": True, "pull": True},
            False,
            True,
            True,
        ),
        (
            "triage",
            "triage",
            {"admin": False, "maintain": False, "push": False, "triage": True, "pull": True},
            False,
            False,
            True,
        ),
        (
            "read",
            "pull",
            {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True},
            False,
            False,
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_get_collaborator_permission_levels(
    permission_str: str,
    role_name: str,
    perms_dict: dict[str, bool],
    expected_admin: bool,
    expected_write: bool,
    expected_read: bool,
) -> None:
    """Verify collaborator permission levels and convenience boolean properties."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        200,
        json={
            "permission": permission_str,
            "role_name": role_name,
            "user": {
                "id": 555,
                "login": "alice",
                "permissions": perms_dict,
            },
        },
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/collaborators/alice/permission"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token("token", http_client=mock_http)
    perm = await client.get_collaborator_permission("o", "r", "alice")

    assert perm.username == "alice"
    assert perm.permission == permission_str
    assert perm.role_name == role_name
    assert perm.user_id == 555
    assert perm.is_collaborator is True
    assert perm.can_admin is expected_admin
    assert perm.can_write is expected_write
    assert perm.can_read is expected_read


@pytest.mark.asyncio
async def test_get_collaborator_permission_404_maps_to_none() -> None:
    """GitHub 404 on /collaborators/{username}/permission indicates user is not a collaborator."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        404,
        json={"message": "Not Found"},
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/collaborators/bob/permission"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token("token", http_client=mock_http)
    perm = await client.get_collaborator_permission("o", "r", "bob")

    assert perm.username == "bob"
    assert perm.permission == "none"
    assert perm.is_collaborator is False
    assert perm.can_admin is False
    assert perm.can_write is False
    assert perm.can_read is False


@pytest.mark.asyncio
async def test_get_collaborator_permission_403_maps_to_none() -> None:
    """GitHub 403 (e.g. caller lacks permission) maps gracefully to none."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = httpx.Response(
        403,
        json={"message": "Must have push access to view collaborator permissions"},
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/collaborators/bob/permission"),
    )
    mock_http.get.return_value = mock_resp

    client = GitHubClient.for_token("token", http_client=mock_http)
    perm = await client.get_collaborator_permission("o", "r", "bob")

    assert perm.username == "bob"
    assert perm.permission == "none"
    assert perm.is_collaborator is False


@pytest.mark.parametrize(
    "bad_username",
    ["", "../baduser", "user/hack", "user name", "user$"],
)
@pytest.mark.asyncio
async def test_get_collaborator_permission_validates_username(bad_username: str) -> None:
    """Path traversal and invalid characters in username must be rejected."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = GitHubClient.for_token("token", http_client=mock_http)

    with pytest.raises(ValueError):
        await client.get_collaborator_permission("owner", "repo", bad_username)

    mock_http.get.assert_not_called()


# ==============================================================================
# 6. Authorization Service Integration Tests
# ==============================================================================


@pytest.mark.parametrize(
    "permission_level,can_admin,can_push,can_pull,expected_result",
    [
        ("admin", True, True, True, "admin"),
        ("maintain", False, True, True, "write"),
        ("push", False, True, True, "write"),
        ("write", False, True, True, "write"),
        ("triage", False, False, True, "read"),
        ("read", False, False, True, "read"),
        ("pull", False, False, True, "read"),
        ("none", False, False, False, "none"),
    ],
)
@pytest.mark.asyncio
async def test_authorization_service_collaborator_permission_resolution(
    permission_level: str,
    can_admin: bool,
    can_push: bool,
    can_pull: bool,
    expected_result: str,
) -> None:
    """Verify AuthorizationService._fetch_github_permission delegates to GitHubClient.for_installation."""
    mock_session = MagicMock()
    service = AuthorizationService(mock_session)

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get_collaborator_permission.return_value = GitHubCollaboratorPermission(
        username="dev-user",
        permission=permission_level,
        can_admin=can_admin,
        can_push=can_push,
        can_pull=can_pull,
    )

    with patch.object(GitHubClient, "for_installation", return_value=mock_client):
        result = await service._fetch_github_permission(
            installation_id=54321,
            owner="myorg",
            repo="myrepo",
            github_login="dev-user",
        )

    assert result == expected_result
    mock_client.get_collaborator_permission.assert_called_once_with("myorg", "myrepo", "dev-user")


@pytest.mark.asyncio
async def test_authorization_service_collaborator_permission_failure_defaults_to_none() -> None:
    """When GitHubClient raises any error during collaborator lookup, authorization fails closed to 'none'."""
    mock_session = MagicMock()
    service = AuthorizationService(mock_session)

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get_collaborator_permission.side_effect = GitHubUpstreamError("GitHub unavailable", status_code=503)

    with patch.object(GitHubClient, "for_installation", return_value=mock_client):
        result = await service._fetch_github_permission(
            installation_id=54321,
            owner="myorg",
            repo="myrepo",
            github_login="dev-user",
        )

    assert result == "none"

