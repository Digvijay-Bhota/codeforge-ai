"""GitHub App configuration: validated at startup, reported without secrets, fail closed.

Repository access (permission checks, cloning, publishing, status updates)
uses the GitHub App.  Its credentials are optional, but all or nothing and well
formed: a half-set or malformed pair stops startup with the reason, instead of
surfacing later as silent access denials.  Without them every repository
permission check answers "none", so nothing is authorized by accident.
"""

from __future__ import annotations

import logging
import random
import uuid
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user
from app.config import DEFAULT_JWT_SECRET, Settings, settings
from app.db.models import GitHubInstallation, GitHubInstallationRepository, Task, TaskApproval, User
from app.db.repositories.user_repository import UserRepository
from app.db.session import get_db_session
from app.main import create_app
from app.services.auth_service import AuthService
from app.services.configuration_status import (
    OPENAI_KEY_PLACEHOLDER,
    agent_model_key_status,
    configuration_status,
    log_configuration_status,
)


def _pem(key: Any) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


RSA_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
EC_PEM = _pem(ec.generate_private_key(ec.SECP256R1()))
# A recognizable fragment of the key body, to prove errors never echo it.
KEY_FRAGMENT = RSA_PEM.splitlines()[5]


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


# ── Startup validation ─────────────────────────────────────────────────────────


def test_no_app_credentials_is_valid_and_reports_repository_access_off() -> None:
    s = _settings()
    assert s.github_app_configured() is False
    assert configuration_status(s)["github_app"] is False


def test_valid_credentials_are_accepted() -> None:
    s = _settings(github_app_id="123456", github_app_private_key=RSA_PEM)
    assert s.github_app_configured() is True


def test_a_key_pasted_as_one_line_with_literal_newlines_is_restored() -> None:
    one_line = RSA_PEM.strip().replace("\n", "\\n")
    s = _settings(github_app_id="123456", github_app_private_key=one_line)
    assert s.github_app_private_key == RSA_PEM.strip()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"github_app_id": "123456"}, "GITHUB_APP_PRIVATE_KEY is not set"),
        ({"github_app_private_key": RSA_PEM}, "GITHUB_APP_ID is not set"),
        ({"github_app_id": "codeforge-app", "github_app_private_key": RSA_PEM}, "numeric App ID"),
        ({"github_app_id": "Iv1.abcdef0123456789", "github_app_private_key": RSA_PEM}, "numeric App ID"),
        ({"github_app_id": "123456", "github_app_private_key": "not a key"}, "is not a PEM private key"),
        ({"github_app_id": "123456", "github_app_private_key": RSA_PEM.replace(KEY_FRAGMENT, "A" * len(KEY_FRAGMENT))},
         "could not be read"),
        ({"github_app_id": "123456", "github_app_private_key": EC_PEM}, "must be an RSA private key"),
    ],
)  # fmt: skip
def test_incomplete_or_malformed_credentials_stop_startup_without_echoing_them(
    overrides: dict[str, str], reason: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        _settings(**overrides)
    message = str(exc_info.value)
    assert reason in message
    assert KEY_FRAGMENT not in message and "BEGIN PRIVATE KEY" not in message


# ── Status reporting ───────────────────────────────────────────────────────────

SECRETS = {
    "github_client_secret": "oauth-secret-0123456789abcdef0123456789abcd",
    "github_webhook_secret": "webhook-secret-do-not-leak-9f8e7d6c",
    "jwt_secret_key": "jwt-secret-do-not-leak-" + "x" * 40,
}


def test_status_reports_capabilities_never_values() -> None:
    s = _settings(github_client_id="Ov23liAbCdEf01234567", github_app_id="123456",
                  github_app_private_key=RSA_PEM, **SECRETS)  # fmt: skip
    status = configuration_status(s)
    assert status == {
        "github_oauth": True,
        "github_app": True,
        "github_webhooks": True,
        "local_execution": False,
        "local_execution_trusted_accounts": 0,
        "test_execution_mode": "disabled",
        "session_secret": "configured",
    }
    text = repr(status)
    for value in (*SECRETS.values(), KEY_FRAGMENT, "123456", "Ov23li"):
        assert value not in text


def test_the_public_development_jwt_secret_is_reported() -> None:
    s = _settings(app_env="development", jwt_secret_key=DEFAULT_JWT_SECRET)
    assert configuration_status(s)["session_secret"] == "development-default"


@pytest.mark.parametrize(
    ("value", "expected"), [(None, "missing"), ("", "missing"), (OPENAI_KEY_PLACEHOLDER, "placeholder"), ("sk-real", "configured")]
)
def test_agent_model_key_status(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str) -> None:
    if value is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", value)
    assert agent_model_key_status() == expected


def test_startup_log_names_what_is_off_without_secrets(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-do-not-log-12345")
    log_configuration_status(_settings(**SECRETS), "worker", include_agent=True)
    assert "worker configuration: github_oauth=False github_app=False" in caplog.text
    assert "agent_model_key=configured" in caplog.text
    assert "GitHub App not configured" in caplog.text
    for value in (*SECRETS.values(), "sk-real-key-do-not-log-12345"):
        assert value not in caplog.text


def test_status_endpoint_requires_sign_in_and_returns_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in SECRETS.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(settings, "github_app_id", "123456")
    monkeypatch.setattr(settings, "github_app_private_key", RSA_PEM)
    app = create_app()
    client = TestClient(app)
    assert client.get("/api/v1/system/status").status_code == 401

    app.dependency_overrides[get_current_user] = lambda: User(id="u1", display_name="Dev")
    res = client.get("/api/v1/system/status")
    assert res.status_code == 200
    assert res.json()["github_app"] is True
    for value in (*SECRETS.values(), KEY_FRAGMENT, "BEGIN PRIVATE KEY", "123456"):
        assert value not in res.text


# ── Without working App credentials, repository access is denied ───────────────


class _PermissionCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest_asyncio.fixture
async def repo_task(session: AsyncSession) -> Any:
    install_id = random.randint(1_000_000, 999_999_999)
    org = f"org-{uuid.uuid4().hex[:6]}"
    repo = f"{org}/billing"
    session.add(GitHubInstallation(installation_id=install_id, account_login=org, account_type="Org", active=True))
    session.add(GitHubInstallationRepository(installation_id=install_id, repository=repo, authorized=True))
    login = f"dev-{uuid.uuid4().hex[:6]}"
    user, identity = await UserRepository(session).upsert_github_user(
        github_user_id=random.randint(1_000_000, 999_999_999), github_login=login, display_name="Dev"
    )
    task = Task(task_id=str(uuid.uuid4()), status="WAITING_APPROVAL", execution_target="github", repository=repo,
                requested_task="Fix it", implementation_plan={"steps": []}, creator_id=user.id)  # fmt: skip
    session.add(task)
    await session.flush()
    session.add(TaskApproval(task_id=task.task_id, approval_type="plan", status="PENDING", requested_by="worker"))
    await session.commit()
    token = AuthService().create_session_jwt(user, identity.github_user_id, identity.github_login)

    maker = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    app = create_app()

    async def override_get_db() -> Any:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_get_db
    with patch("app.services.authorization_service.get_redis_client", return_value=_PermissionCache()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, task.task_id, repo, {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("app_credentials", ["missing", "unusable"])
@pytest.mark.asyncio
async def test_no_repository_access_without_working_app_credentials(
    repo_task: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, app_credentials: str
) -> None:
    # Even the task's own creator is denied: repository permission comes only
    # from GitHub, through the App.  "unusable" bypasses startup validation, as
    # a key revoked or corrupted after startup would.
    caplog.set_level(logging.DEBUG)
    client, task_id, repo, headers = repo_task
    if app_credentials == "missing":
        monkeypatch.setattr(settings, "github_app_id", "")
        monkeypatch.setattr(settings, "github_app_private_key", "")
    else:
        monkeypatch.setattr(settings, "github_app_id", "123456")
        monkeypatch.setattr(settings, "github_app_private_key", "corrupted-key-material-do-not-log")

    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).status_code == 404
    approve = await client.post(f"/api/v1/tasks/{task_id}/approve", json={}, headers=headers)
    assert approve.status_code == 403
    listing = await client.get("/api/v1/tasks", params={"repository": repo}, headers=headers)
    assert listing.status_code == 403
    assert "corrupted-key-material-do-not-log" not in caplog.text
