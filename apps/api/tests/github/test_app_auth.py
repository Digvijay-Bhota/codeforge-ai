from unittest.mock import AsyncMock, patch

import jwt
import pytest

from app.config import settings
from app.github.app_auth import GitHubAppAuth, GitHubAppAuthError
from app.github.client import GitHubClient
from app.github.exceptions import GitHubAuthorizationError
from app.github.token_service import TokenScope

# What GitHub reports granting for a contents/metadata read token on repository 42.
GRANTED_42 = {
    "repository_selection": "selected",
    "repositories": [{"id": 42, "name": "r", "full_name": "o/r"}],
    "permissions": {"contents": "read", "metadata": "read"},
}


def test_jwt_generation():
    # We need a valid RSA private key format for jwt to encode
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")

    settings.github_app_id = "12345"
    settings.github_app_private_key = pem

    token = GitHubAppAuth.generate_jwt()
    assert token is not None

    # Verify the token
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    decoded = jwt.decode(token, public_key, algorithms=["RS256"])
    assert decoded["iss"] == "12345"
    assert "iat" in decoded
    assert "exp" in decoded

def test_jwt_generation_unconfigured():
    settings.github_app_id = ""
    with pytest.raises(GitHubAppAuthError):
        GitHubAppAuth.generate_jwt()

@pytest.mark.asyncio
async def test_client_create_installation_token():
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status_code = 201
        from unittest.mock import MagicMock
        mock_response.json = MagicMock(return_value={"token": "v1.install.token", **GRANTED_42})
        mock_post.return_value = mock_response

        client = GitHubClient(token="jwt-token")
        scope = TokenScope.for_repository_id(42, {"contents": "read", "metadata": "read"})
        token = await client.create_installation_access_token(123, scope)
        assert token == "v1.install.token"

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "app/installations/123/access_tokens" in args[0]
        assert kwargs["headers"]["Authorization"] == "Bearer jwt-token"
        # CF-06: one repository, explicit permissions, nothing implicit.
        assert kwargs["json"] == {
            "repository_ids": [42],
            "permissions": {"contents": "read", "metadata": "read"},
        }


@pytest.mark.parametrize("missing", ["repository_selection", "repositories", "permissions"])
@pytest.mark.asyncio
async def test_client_refuses_an_installation_token_whose_grant_is_unreported(missing: str) -> None:
    from unittest.mock import MagicMock

    granted = {"token": "v1.unverified.token", **GRANTED_42}
    del granted[missing]
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status_code = 201
        mock_response.json = MagicMock(return_value=granted)
        mock_post.return_value = mock_response
        client = GitHubClient(token="jwt-token")
        scope = TokenScope.for_repository_id(42, {"contents": "read", "metadata": "read"})
        with pytest.raises(GitHubAuthorizationError, match="outside the requested scope") as exc_info:
            await client.create_installation_access_token(123, scope)
    assert "v1.unverified.token" not in str(exc_info.value)
