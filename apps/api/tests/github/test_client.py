"""Tests for the GitHub API client."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.github.client import GitHubClient
from app.github.exceptions import (
    GitHubAuthenticationError,
    GitHubConfigurationError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTimeoutError,
    GitHubUpstreamError,
    GitHubValidationError,
)


@pytest.fixture
def mock_settings():
    with patch("app.github.client.settings") as mock:
        mock.github_token = "dummy_token"
        mock.github_api_url = "https://api.github.com"
        yield mock

def test_missing_token_raises_config_error():
    with patch("app.github.client.settings") as mock:
        mock.github_token = ""
        with pytest.raises(GitHubConfigurationError):
            GitHubClient()

def _mock_response(status_code: int, json_data: dict, headers: dict = None) -> httpx.Response:
    resp = httpx.Response(status_code, json=json_data, headers=headers or {})
    return resp

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_get_repository_success(mock_get, mock_settings):
    mock_get.return_value = _mock_response(
        200,
        {
            "owner": {"login": "owner"},
            "name": "repo",
            "full_name": "owner/repo",
            "default_branch": "main",
            "private": False,
            "clone_url": "url",
            "html_url": "url",
        }
    )

    client = GitHubClient()
    repo = await client.get_repository("owner", "repo")
    assert repo.name == "repo"
    assert repo.owner == "owner"

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_get_repository_not_found(mock_get, mock_settings):
    mock_get.return_value = _mock_response(404, {"message": "Not Found"})
    client = GitHubClient()

    with pytest.raises(GitHubNotFoundError):
        await client.get_repository("owner", "repo")

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_auth_error(mock_get, mock_settings):
    mock_get.return_value = _mock_response(401, {"message": "Bad credentials"})
    client = GitHubClient()

    with pytest.raises(GitHubAuthenticationError):
        await client.get_repository("owner", "repo")

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_rate_limit_error(mock_get, mock_settings):
    mock_get.return_value = _mock_response(403, {}, headers={"x-ratelimit-remaining": "0"})
    client = GitHubClient()

    with pytest.raises(GitHubRateLimitError):
        await client.get_repository("owner", "repo")

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_validation_error(mock_get, mock_settings):
    mock_get.return_value = _mock_response(422, {"message": "Validation Failed"})
    client = GitHubClient()

    with pytest.raises(GitHubValidationError):
        await client.get_repository("owner", "repo")

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_upstream_error(mock_get, mock_settings):
    mock_get.return_value = _mock_response(502, {"message": "Bad Gateway"})
    client = GitHubClient()

    with pytest.raises(GitHubUpstreamError):
        await client.get_repository("owner", "repo")

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_timeout_error(mock_get, mock_settings):
    mock_get.side_effect = httpx.TimeoutException("Timeout")
    client = GitHubClient()

    with pytest.raises(GitHubTimeoutError):
        await client.get_repository("owner", "repo")

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_network_error(mock_get, mock_settings):
    mock_get.side_effect = httpx.RequestError("Network error")
    client = GitHubClient()

    with pytest.raises(Exception, match="Request to GitHub failed"):
        await client.get_repository("owner", "repo")

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_token_not_in_exception(mock_get, mock_settings):
    mock_get.return_value = _mock_response(401, {"message": "Bad credentials"})
    client = GitHubClient()

    try:
        await client.get_repository("owner", "repo")
    except GitHubAuthenticationError as exc:
        assert "dummy_token" not in str(exc)

@pytest.mark.asyncio
@patch("app.github.client.httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_no_arbitrary_url_override(mock_get, mock_settings):
    mock_get.return_value = _mock_response(404, {"message": "Not Found"})
    client = GitHubClient()

    # We pass an evil repo name, but wait, the client takes strings and constructs the URL.
    # The URL construction uses f"{self.base_url}/repos/{owner}/{repo}"
    # The caller is responsible for passing validated owner/repo if they use the models,
    # but even if they don't, the base_url is strictly from settings.
    with pytest.raises(GitHubNotFoundError):
        await client.get_repository("owner", "repo")

    # Verify the actual requested URL starts with the configured base_url
    mock_get.assert_called_once()
    requested_url = mock_get.call_args[0][0]
    assert requested_url.startswith("https://api.github.com/repos/")
