from unittest.mock import AsyncMock, patch

import jwt
import pytest

from app.config import settings
from app.github.app_auth import GitHubAppAuth, GitHubAppAuthError
from app.github.client import GitHubClient


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
        mock_response.json = MagicMock(return_value={"token": "v1.install.token"})
        mock_post.return_value = mock_response

        client = GitHubClient(token="jwt-token")
        token = await client.create_installation_access_token(123)
        assert token == "v1.install.token"

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "app/installations/123/access_tokens" in args[0]
        assert kwargs["headers"]["Authorization"] == "Bearer jwt-token"
