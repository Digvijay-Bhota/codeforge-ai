"""CF-02 regression tests: the public default JWT secret is refused outside dev/test.

The session-signing default (also in .env.example) is public, so anyone could
forge a session token with it.  It used to be refused only when APP_ENV was
exactly "production": with APP_ENV unset, "staging", "prod" and so on, the
application accepted it and a forged token decoded.  Now any environment
other than an *explicit* APP_ENV=development or APP_ENV=test needs a random
secret of at least 32 characters, and validation errors never echo secrets.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import DEFAULT_JWT_SECRET, MIN_JWT_SECRET_LENGTH, Settings, settings
from app.services.auth_service import AuthService

STRONG = secrets.token_urlsafe(48)
POLICY_ERROR = "JWT_SECRET_KEY must be securely configured"
# Production also requires these; set so only the JWT rule is under test.
PRODUCTION_REQUIREMENTS = {
    "openai_api_key": "sk-test",
    "database_url": "postgresql+asyncpg://user:pass@db:5432/prod",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from no APP_ENV and no JWT_SECRET_KEY (conftest sets a random one)."""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)


def build(app_env: str | None, **overrides: Any) -> Settings:
    kwargs: dict[str, Any] = {"_env_file": None, **overrides}
    if app_env is not None:
        kwargs["app_env"] = app_env
        if app_env == "production":
            kwargs = {**PRODUCTION_REQUIREMENTS, **kwargs}
    return Settings(**kwargs)


# APP_ENV values that must not accept a weak secret.  None means unset.
FAIL_CLOSED_ENVIRONMENTS = [
    None, "", "production", "staging", "prod", "Production", "DEVELOPMENT", "dev",
    " development", "test ", "unknown",
]  # fmt: skip


@pytest.mark.parametrize("app_env", FAIL_CLOSED_ENVIRONMENTS)
def test_default_secret_is_refused_outside_explicit_development(app_env: str | None) -> None:
    with pytest.raises(ValidationError, match=POLICY_ERROR):
        build(app_env)


@pytest.mark.parametrize("app_env", [None, "staging", "production"])
@pytest.mark.parametrize(
    "secret",
    [
        "",
        "x" * (MIN_JWT_SECRET_LENGTH - 1),
        "INSECURE-placeholder-value-that-is-plenty-long-enough",
        DEFAULT_JWT_SECRET.upper(),
    ],
    ids=["empty", "too-short", "placeholder", "default-uppercased"],
)
def test_weak_secrets_are_refused_outside_explicit_development(
    app_env: str | None, secret: str
) -> None:
    with pytest.raises(ValidationError, match=POLICY_ERROR):
        build(app_env, jwt_secret_key=secret)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_default_secret_from_environment_variables_is_refused(
    monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    """The way a deployment actually configures it: environment variables."""
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("JWT_SECRET_KEY", DEFAULT_JWT_SECRET)
    for key, value in PRODUCTION_REQUIREMENTS.items():
        monkeypatch.setenv(key.upper(), value)
    with pytest.raises(ValidationError, match=POLICY_ERROR):
        Settings(_env_file=None)


def test_unset_app_env_with_default_secret_is_refused() -> None:
    """The reproduced CF-02 case: nothing configured at all."""
    with pytest.raises(ValidationError, match=POLICY_ERROR):
        Settings(_env_file=None)


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_explicit_development_accepts_default_secret(
    monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    assert build(app_env).jwt_secret_key == DEFAULT_JWT_SECRET
    monkeypatch.setenv("APP_ENV", app_env)
    assert Settings(_env_file=None).jwt_secret_violation() is None


@pytest.mark.parametrize("app_env", [None, "staging", "prod", "production"])
def test_strong_secret_is_accepted_everywhere(app_env: str | None) -> None:
    loaded = build(app_env, jwt_secret_key=STRONG)
    assert loaded.jwt_secret_violation() is None


def test_minimum_length_secret_is_accepted() -> None:
    assert build("production", jwt_secret_key="k" * MIN_JWT_SECRET_LENGTH).app_env == "production"


def test_policy_does_not_change_host_execution_gate() -> None:
    """The shared helper keeps host_unsafe tied to an explicit development APP_ENV."""
    assert build(None, jwt_secret_key=STRONG).allows_host_test_execution() is False
    assert build("staging", jwt_secret_key=STRONG).allows_host_test_execution() is False
    assert build("test").allows_host_test_execution() is True


def test_validation_errors_never_echo_secrets_or_credentials() -> None:
    weak = "SENTINEL-short-secret"
    database_url = "postgresql+asyncpg://dbuser:SENTINEL-db-password@db:5432/prod"
    with pytest.raises(ValidationError) as excinfo:
        build("production", jwt_secret_key=weak, database_url=database_url)
    text = str(excinfo.value) + repr(excinfo.value)
    assert POLICY_ERROR in text
    assert "SENTINEL" not in text
    with pytest.raises(ValidationError) as excinfo:
        build("staging")
    assert DEFAULT_JWT_SECRET not in str(excinfo.value) + repr(excinfo.value)


# ── Tokens ─────────────────────────────────────────────────────────────────────


def _forged(secret: str) -> str:
    return jwt.encode(
        {"sub": "victim-user-id", "type": "session", "exp": int(time.time()) + 600},
        secret,
        algorithm="HS256",
    )


def test_configured_secret_round_trips_and_rejects_default_forgery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret_key", STRONG)
    service = AuthService()
    user = type("U", (), {"id": "u-1", "display_name": "u", "email": None})()
    token = service.create_session_jwt(user, github_user_id=1, github_login="u")
    assert service.decode_session_jwt(token)["sub"] == "u-1"
    with pytest.raises(HTTPException) as excinfo:
        service.decode_session_jwt(_forged(DEFAULT_JWT_SECRET))
    assert excinfo.value.status_code == 401


def test_mutated_settings_cannot_make_tokens_forgeable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if the singleton is mutated past validation, the default never signs or verifies."""
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", DEFAULT_JWT_SECRET)
    service = AuthService()
    with pytest.raises(HTTPException) as excinfo:
        service.decode_session_jwt(_forged(DEFAULT_JWT_SECRET))
    assert excinfo.value.status_code == 503
    assert DEFAULT_JWT_SECRET not in str(excinfo.value.detail)
    user = type("U", (), {"id": "u-1", "display_name": "u", "email": None})()
    with pytest.raises(HTTPException) as excinfo:
        service.create_session_jwt(user, github_user_id=1, github_login="u")
    assert excinfo.value.status_code == 503
