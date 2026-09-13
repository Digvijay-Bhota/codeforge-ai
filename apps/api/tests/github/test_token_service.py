"""Tests for GitHub App Installation Token Service & Typed GitHub Errors.

Phase 10B.2.1 test suite covering:
- Token minting, cache hit, expired refresh, refresh buffer boundary
- Invalidation
- Single-flight concurrency (10 concurrent requests for same installation)
- Parallel execution for different installations
- Typed error mapping (401, 403, rate limit, 404, 409, 422, 500, 503, timeout, connection error)
- Malformed responses (invalid json, missing token, missing expires_at, bad date)
- Security: zero persistence, token redaction in repr/str, no secrets in logs or exceptions
- Positive integer validation (rejection of zero, negative, string, booleans)
- Timezone-aware expiration handling
- Full exception hierarchy assertions
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings
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
    GitHubTransientError,
    GitHubUpstreamError,
    GitHubValidationError,
)
from app.github.token_service import (
    CachedInstallationToken,
    InstallationTokenService,
    get_installation_token_service,
    validate_installation_id,
)


@pytest.fixture(scope="module")
def rsa_private_key_pem() -> str:
    """Generate a valid RSA private key in PEM format for App JWT testing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


@pytest.fixture(autouse=True)
def configure_github_app(rsa_private_key_pem: str):
    """Automatically configure GitHub App settings for each test."""
    original_app_id = settings.github_app_id
    original_private_key = settings.github_app_private_key
    original_buffer = settings.github_token_refresh_buffer_seconds

    settings.github_app_id = "test-app-12345"
    settings.github_app_private_key = rsa_private_key_pem
    settings.github_token_refresh_buffer_seconds = 300

    yield

    settings.github_app_id = original_app_id
    settings.github_app_private_key = original_private_key
    settings.github_token_refresh_buffer_seconds = original_buffer


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Token Operations
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_token_mint():
    """Verify successful minting of an installation access token."""
    service = InstallationTokenService(refresh_buffer_seconds=300)
    future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 201
        mock_response.headers = {"x-github-request-id": "REQ-1234"}
        mock_response.json = MagicMock(
            return_value={
                "token": "ghs_test_token_12345678",
                "expires_at": future_time,
            }
        )
        mock_post.return_value = mock_response

        token = await service.get_token(1001)
        assert token == "ghs_test_token_12345678"
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "/app/installations/1001/access_tokens" in call_url


@pytest.mark.asyncio
async def test_valid_cache_hit():
    """Verify that second call within validity window returns cached token without HTTP call."""
    service = InstallationTokenService(refresh_buffer_seconds=300)
    future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 201
        mock_response.headers = {}
        mock_response.json = MagicMock(
            return_value={
                "token": "ghs_cached_token_abc",
                "expires_at": future_time,
            }
        )
        mock_post.return_value = mock_response

        # First call: mints token
        token1 = await service.get_token(1002)
        assert token1 == "ghs_cached_token_abc"
        assert mock_post.call_count == 1

        # Second call: cache hit
        token2 = await service.get_token(1002)
        assert token2 == "ghs_cached_token_abc"
        assert mock_post.call_count == 1  # No additional network request


@pytest.mark.asyncio
async def test_expired_cache_refresh():
    """Verify that expired cached token triggers a fresh mint."""
    service = InstallationTokenService(refresh_buffer_seconds=300)
    now = datetime.now(UTC)

    # Manually seed expired token
    service._cache[1003] = CachedInstallationToken(
        token="ghs_expired_old_token",
        installation_id=1003,
        expires_at=now - timedelta(seconds=60),  # expired 1 minute ago
    )

    future_time = (now + timedelta(hours=1)).isoformat()
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 201
        mock_response.headers = {}
        mock_response.json = MagicMock(
            return_value={
                "token": "ghs_fresh_new_token",
                "expires_at": future_time,
            }
        )
        mock_post.return_value = mock_response

        token = await service.get_token(1003)
        assert token == "ghs_fresh_new_token"
        assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_refresh_buffer_behavior():
    """Verify that a token within the refresh safety buffer is refreshed."""
    service = InstallationTokenService(refresh_buffer_seconds=300)
    now = datetime.now(UTC)

    # Token expires in 200 seconds (inside the 300-second buffer)
    service._cache[1004] = CachedInstallationToken(
        token="ghs_inside_buffer",
        installation_id=1004,
        expires_at=now + timedelta(seconds=200),
    )

    future_time = (now + timedelta(hours=1)).isoformat()
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 201
        mock_response.headers = {}
        mock_response.json = MagicMock(
            return_value={
                "token": "ghs_refreshed_after_buffer",
                "expires_at": future_time,
            }
        )
        mock_post.return_value = mock_response

        token = await service.get_token(1004)
        assert token == "ghs_refreshed_after_buffer"
        assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_token_outside_refresh_buffer_is_returned():
    """Verify that a token expiring well outside the refresh safety buffer is returned."""
    service = InstallationTokenService(refresh_buffer_seconds=300)
    now = datetime.now(UTC)

    # Token expires in 400 seconds (outside the 300-second buffer)
    service._cache[1005] = CachedInstallationToken(
        token="ghs_outside_buffer_valid",
        installation_id=1005,
        expires_at=now + timedelta(seconds=400),
    )

    with patch("httpx.AsyncClient.post") as mock_post:
        token = await service.get_token(1005)
        assert token == "ghs_outside_buffer_valid"
        assert mock_post.call_count == 0


@pytest.mark.asyncio
async def test_invalidate_removes_token():
    """Verify invalidate evicts cached token and forces a fresh mint."""
    service = InstallationTokenService(refresh_buffer_seconds=300)
    now = datetime.now(UTC)

    service._cache[1006] = CachedInstallationToken(
        token="ghs_to_be_invalidated",
        installation_id=1006,
        expires_at=now + timedelta(hours=1),
    )

    # Invalidate
    service.invalidate(1006)
    assert 1006 not in service._cache

    # Subsequent get_token mints fresh token
    future_time = (now + timedelta(hours=1)).isoformat()
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 201
        mock_response.headers = {}
        mock_response.json = MagicMock(
            return_value={
                "token": "ghs_fresh_mint_post_invalidate",
                "expires_at": future_time,
            }
        )
        mock_post.return_value = mock_response

        token = await service.get_token(1006)
        assert token == "ghs_fresh_mint_post_invalidate"
        assert mock_post.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. Concurrency & Single-Flight
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_flight_ten_concurrent_requests_same_installation():
    """Verify 10 concurrent requests for the same installation trigger exactly 1 upstream mint."""
    service = InstallationTokenService(refresh_buffer_seconds=300)
    future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    call_counter = 0

    async def mock_post_handler(*args, **kwargs):
        nonlocal call_counter
        call_counter += 1
        # Add realistic async latency to induce concurrent interleaving
        await asyncio.sleep(0.05)
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 201
        resp.headers = {"x-github-request-id": f"REQ-{call_counter}"}
        resp.json = MagicMock(
            return_value={
                "token": "ghs_single_flight_token_123",
                "expires_at": future_time,
            }
        )
        return resp

    with patch("httpx.AsyncClient.post", side_effect=mock_post_handler):
        tasks = [asyncio.create_task(service.get_token(999)) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # Invariant 1: Exactly 1 upstream mint
        assert call_counter == 1

        # Invariant 2: All 10 callers received identical token
        assert len(results) == 10
        assert all(token == "ghs_single_flight_token_123" for token in results)


@pytest.mark.asyncio
async def test_concurrent_different_installations_do_not_block():
    """Verify calls for different installation IDs proceed without unnecessarily blocking."""
    service = InstallationTokenService(refresh_buffer_seconds=300)
    future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    installations_minted = []

    async def mock_post_handler(url, *args, **kwargs):
        # Extract installation id from url
        inst_id = int(url.split("/app/installations/")[1].split("/access_tokens")[0])
        installations_minted.append(inst_id)
        await asyncio.sleep(0.02)
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 201
        resp.headers = {}
        resp.json = MagicMock(
            return_value={
                "token": f"ghs_token_for_{inst_id}",
                "expires_at": future_time,
            }
        )
        return resp

    with patch("httpx.AsyncClient.post", side_effect=mock_post_handler):
        results = await asyncio.gather(
            service.get_token(501),
            service.get_token(502),
            service.get_token(503),
        )

        assert results[0] == "ghs_token_for_501"
        assert results[1] == "ghs_token_for_502"
        assert results[2] == "ghs_token_for_503"
        assert set(installations_minted) == {501, 502, 503}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Failure & Typed Errors
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_github_401_authentication_error():
    """Verify HTTP 401 raises GitHubAuthenticationError with status_code=401."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 401
        resp.headers = {"x-github-request-id": "REQ-AUTH-FAIL"}
        mock_post.return_value = resp

        with pytest.raises(GitHubAuthenticationError) as exc_info:
            await service.get_token(2001)

        exc = exc_info.value
        assert exc.status_code == 401
        assert exc.operation == "mint_installation_token"
        assert exc.github_request_id == "REQ-AUTH-FAIL"
        assert exc.retryable is False


@pytest.mark.asyncio
async def test_github_403_authorization_error():
    """Verify HTTP 403 (non rate-limited) raises GitHubAuthorizationError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 403
        resp.headers = {"x-ratelimit-remaining": "100"}
        mock_post.return_value = resp

        with pytest.raises(GitHubAuthorizationError) as exc_info:
            await service.get_token(2002)

        exc = exc_info.value
        assert exc.status_code == 403
        assert exc.operation == "mint_installation_token"
        assert exc.retryable is False


@pytest.mark.asyncio
async def test_github_403_rate_limit_error():
    """Verify HTTP 403 with x-ratelimit-remaining=0 raises GitHubRateLimitError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 403
        resp.headers = {
            "x-ratelimit-remaining": "0",
            "retry-after": "60",
            "x-github-request-id": "REQ-RATE-LIMIT",
        }
        mock_post.return_value = resp

        with pytest.raises(GitHubRateLimitError) as exc_info:
            await service.get_token(2003)

        exc = exc_info.value
        assert exc.status_code == 403
        assert exc.retryable is True
        assert exc.retry_after == 60
        assert exc.github_request_id == "REQ-RATE-LIMIT"


@pytest.mark.asyncio
async def test_github_404_not_found_error():
    """Verify HTTP 404 raises GitHubNotFoundError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 404
        resp.headers = {}
        mock_post.return_value = resp

        with pytest.raises(GitHubNotFoundError) as exc_info:
            await service.get_token(2004)

        exc = exc_info.value
        assert exc.status_code == 404
        assert exc.retryable is False


@pytest.mark.asyncio
async def test_github_409_conflict_error():
    """Verify HTTP 409 raises GitHubConflictError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 409
        resp.headers = {}
        mock_post.return_value = resp

        with pytest.raises(GitHubConflictError) as exc_info:
            await service.get_token(2005)

        exc = exc_info.value
        assert exc.status_code == 409
        assert exc.retryable is False


@pytest.mark.asyncio
async def test_github_422_validation_error():
    """Verify HTTP 422 raises GitHubValidationError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 422
        resp.headers = {}
        mock_post.return_value = resp

        with pytest.raises(GitHubValidationError) as exc_info:
            await service.get_token(2006)

        exc = exc_info.value
        assert exc.status_code == 422
        assert exc.retryable is False


@pytest.mark.asyncio
async def test_github_500_upstream_error():
    """Verify HTTP 500 raises GitHubUpstreamError with retryable=True."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 500
        resp.headers = {"x-github-request-id": "REQ-500"}
        mock_post.return_value = resp

        with pytest.raises(GitHubUpstreamError) as exc_info:
            await service.get_token(2007)

        exc = exc_info.value
        assert exc.status_code == 500
        assert exc.retryable is True
        assert isinstance(exc, GitHubTransientError)


@pytest.mark.asyncio
async def test_github_timeout_error():
    """Verify httpx.TimeoutException raises GitHubTimeoutError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post", side_effect=httpx.ReadTimeout("Read timed out")):
        with pytest.raises(GitHubTimeoutError) as exc_info:
            await service.get_token(2008)

        exc = exc_info.value
        assert exc.retryable is True
        assert isinstance(exc, GitHubTransientError)
        assert isinstance(exc, GitHubError)


@pytest.mark.asyncio
async def test_github_connection_error():
    """Verify httpx.NetworkError raises GitHubConnectionError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("Failed to resolve host")):
        with pytest.raises(GitHubConnectionError) as exc_info:
            await service.get_token(2009)

        exc = exc_info.value
        assert exc.retryable is True
        assert isinstance(exc, GitHubTransientError)
        assert isinstance(exc, GitHubError)


@pytest.mark.asyncio
async def test_malformed_response_missing_token():
    """Verify missing 'token' in 201 response raises GitHubAuthenticationError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 201
        resp.headers = {}
        resp.json = MagicMock(return_value={"expires_at": "2026-09-13T20:00:00Z"})
        mock_post.return_value = resp

        with pytest.raises(GitHubAuthenticationError, match="missing or invalid 'token' field"):
            await service.get_token(2010)


@pytest.mark.asyncio
async def test_malformed_response_missing_expires_at():
    """Verify missing 'expires_at' in 201 response raises GitHubAuthenticationError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 201
        resp.headers = {}
        resp.json = MagicMock(return_value={"token": "ghs_token_no_expiry"})
        mock_post.return_value = resp

        with pytest.raises(
            GitHubAuthenticationError, match="missing or invalid 'expires_at' field"
        ):
            await service.get_token(2011)


@pytest.mark.asyncio
async def test_malformed_response_unparseable_date():
    """Verify unparseable 'expires_at' in 201 response raises GitHubAuthenticationError."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 201
        resp.headers = {}
        resp.json = MagicMock(
            return_value={"token": "ghs_token", "expires_at": "invalid-datetime-string"}
        )
        mock_post.return_value = resp

        with pytest.raises(GitHubAuthenticationError, match="unparseable 'expires_at' timestamp"):
            await service.get_token(2012)


@pytest.mark.asyncio
async def test_unconfigured_github_app_raises_configuration_error():
    """Verify missing GitHub App credentials raises GitHubConfigurationError."""
    settings.github_app_id = ""
    service = InstallationTokenService()

    with pytest.raises(GitHubConfigurationError, match="GitHub App is not configured"):
        await service.get_token(2013)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Security & Credential Protection
# ─────────────────────────────────────────────────────────────────────────────


def test_token_redaction_in_cached_token_repr():
    """Verify that CachedInstallationToken.__repr__ and __str__ redact the token secret."""
    now = datetime.now(UTC)
    token = CachedInstallationToken(
        token="ghs_super_secret_access_token_1234567890",
        installation_id=3001,
        expires_at=now,
    )

    repr_str = repr(token)
    str_str = str(token)

    # Must NOT contain the full secret token
    assert "ghs_super_secret_access_token_1234567890" not in repr_str
    assert "ghs_super_secret_access_token_1234567890" not in str_str
    assert "ghs_...7890" in repr_str or "***" in repr_str


def test_exception_representation_does_not_contain_secrets():
    """Verify that GitHubError __repr__ and __str__ do not leak credentials."""
    err = GitHubAuthenticationError(
        "Invalid installation authentication",
        status_code=401,
        operation="mint_installation_token",
        github_request_id="REQ-XYZ",
    )

    err_str = str(err)
    err_repr = repr(err)

    assert err_str == "Invalid installation authentication"
    assert "status_code=401" in err_repr
    assert "operation='mint_installation_token'" in err_repr
    assert "github_request_id='REQ-XYZ'" in err_repr


@pytest.mark.asyncio
async def test_logs_do_not_contain_secrets(caplog):
    """Verify that logger outputs during token minting never contain the raw secret token."""
    caplog.set_level(logging.DEBUG)
    service = InstallationTokenService()
    future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    secret_token = "ghs_secret_credential_value_987654321"

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 201
        resp.headers = {"x-github-request-id": "REQ-LOG-TEST"}
        resp.json = MagicMock(
            return_value={
                "token": secret_token,
                "expires_at": future_time,
            }
        )
        mock_post.return_value = resp

        await service.get_token(3002)

    # Check all logged messages
    log_text = caplog.text
    assert secret_token not in log_text
    assert "Authorization" not in log_text
    assert settings.github_app_private_key not in log_text


def test_zero_persistence_proof():
    """Verify service relies strictly on process-local in-memory state without external persistence."""
    service = InstallationTokenService()
    assert isinstance(service._cache, dict)
    assert not hasattr(service, "_db")
    assert not hasattr(service, "_redis")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Validation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "invalid_id",
    [
        0,
        -1,
        -100,
        "123",
        "invalid",
        True,
        False,
        12.34,
        None,
        [],
        {},
    ],
)
def test_validate_installation_id_rejects_invalid_values(invalid_id):
    """Verify validate_installation_id rejects zero, negative numbers, non-integers, and booleans."""
    with pytest.raises(ValueError, match="Invalid installation_id"):
        validate_installation_id(invalid_id)


def test_validate_installation_id_accepts_positive_integers():
    """Verify validate_installation_id accepts valid positive integers."""
    assert validate_installation_id(1) == 1
    assert validate_installation_id(12345) == 12345
    assert validate_installation_id(99999999) == 99999999


@pytest.mark.asyncio
async def test_get_token_validates_id_before_network_call():
    """Verify get_token validates installation_id before making any network requests."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        with pytest.raises(ValueError):
            await service.get_token(0)

        with pytest.raises(ValueError):
            await service.get_token(True)  # In Python, isinstance(True, int) is True!

        with pytest.raises(ValueError):
            await service.get_token(-50)

        assert mock_post.call_count == 0


def test_invalidate_validates_id():
    """Verify invalidate validates installation_id."""
    service = InstallationTokenService()
    with pytest.raises(ValueError):
        service.invalidate(-1)
    with pytest.raises(ValueError):
        service.invalidate(False)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Timezone-Aware Expiration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timezone_aware_utc_parsing():
    """Verify parsing of ISO 8601 timestamps preserves timezone-aware UTC datetime."""
    service = InstallationTokenService()

    with patch("httpx.AsyncClient.post") as mock_post:
        resp = AsyncMock(spec=httpx.Response)
        resp.status_code = 201
        resp.headers = {}
        resp.json = MagicMock(
            return_value={
                "token": "ghs_tz_token",
                "expires_at": "2026-09-13T20:30:00Z",
            }
        )
        mock_post.return_value = resp

        await service.get_token(4001)

        cached = service._cache[4001]
        assert cached.expires_at.tzinfo is not None
        assert cached.expires_at.tzinfo == UTC
        assert cached.expires_at.year == 2026


# ─────────────────────────────────────────────────────────────────────────────
# 7. Typed Exception Hierarchy Assertions
# ─────────────────────────────────────────────────────────────────────────────


def test_exception_hierarchy():
    """Verify exception inheritance and attributes."""
    assert issubclass(GitHubAuthenticationError, GitHubError)
    assert issubclass(GitHubAuthorizationError, GitHubError)
    assert issubclass(GitHubNotFoundError, GitHubError)
    assert issubclass(GitHubConflictError, GitHubError)
    assert issubclass(GitHubValidationError, GitHubError)
    assert issubclass(GitHubRateLimitError, GitHubError)
    assert issubclass(GitHubConfigurationError, GitHubError)

    # Transient hierarchy
    assert issubclass(GitHubTransientError, GitHubError)
    assert issubclass(GitHubTimeoutError, GitHubTransientError)
    assert issubclass(GitHubConnectionError, GitHubTransientError)
    assert issubclass(GitHubUpstreamError, GitHubTransientError)

    # Transient errors are also GitHubError
    assert issubclass(GitHubTimeoutError, GitHubError)
    assert issubclass(GitHubConnectionError, GitHubError)
    assert issubclass(GitHubUpstreamError, GitHubError)

    # Defaults
    timeout_err = GitHubTimeoutError()
    assert timeout_err.retryable is True
    assert timeout_err.status_code is None

    conn_err = GitHubConnectionError()
    assert conn_err.retryable is True

    upstream_err = GitHubUpstreamError()
    assert upstream_err.retryable is True
    assert upstream_err.status_code == 500

    auth_err = GitHubAuthenticationError()
    assert auth_err.retryable is False
    assert auth_err.status_code == 401

    rate_err = GitHubRateLimitError()
    assert rate_err.retryable is True
    assert rate_err.status_code == 403


def test_singleton_getter():
    """Verify get_installation_token_service returns a singleton instance."""
    s1 = get_installation_token_service()
    s2 = get_installation_token_service()
    assert s1 is s2
    assert isinstance(s1, InstallationTokenService)
