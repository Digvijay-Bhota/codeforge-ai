"""CF-08 and CF-13 regression tests: OAuth redirect allowlist and one-time state.

CF-08: redirect URIs were accepted if they merely started with
"http://localhost:" or "http://127.0.0.1:", so any port, path, or even another
host ("http://localhost:@evil.example/") passed.  Now only exact allowlist
entries (and the configured default) are accepted.

CF-13: state was consumed with a separate GET and DELETE, so concurrent
callbacks could both accept it, and a Redis write that failed ambiguously also
stored the state in an in-process dict, making it redeemable twice.  Now state
lives only in Redis, is consumed with one atomic GETDEL, and sign-in fails
closed (503) without Redis.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient
from redis.asyncio import Redis

import app.services.auth_service as auth_module
from app.config import settings
from app.main import create_app
from app.services.auth_service import AuthService
from tests.conftest import FakeOAuthRedis

CONSOLE_CALLBACK = "http://localhost:3000/auth/callback"


# ── CF-08: redirect URIs must match the allowlist exactly ──────────────────────


@pytest.fixture
def allowlist(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    entries = [
        "http://localhost:8000/api/v1/auth/github/callback",
        CONSOLE_CALLBACK,
        "https://codeforge.example.com/auth/callback",
    ]
    monkeypatch.setattr(settings, "allowed_oauth_redirect_uris", entries)
    monkeypatch.setattr(settings, "github_oauth_redirect_uri", "http://localhost:8000/api/v1/auth/github/callback")
    return entries


def test_exact_allowlist_entries_and_the_default_are_accepted(allowlist: list[str]) -> None:
    service = AuthService()
    for entry in allowlist:
        assert service.validate_redirect_uri(entry) == entry
    assert service.validate_redirect_uri(None) == "http://localhost:8000/api/v1/auth/github/callback"
    assert service.validate_redirect_uri("") == "http://localhost:8000/api/v1/auth/github/callback"


def test_the_configured_default_is_accepted_even_if_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allowed_oauth_redirect_uris", [])
    monkeypatch.setattr(settings, "github_oauth_redirect_uri", "https://api.example.com/api/v1/auth/github/callback")
    service = AuthService()
    assert service.validate_redirect_uri("https://api.example.com/api/v1/auth/github/callback")
    with pytest.raises(HTTPException):
        service.validate_redirect_uri(CONSOLE_CALLBACK)


@pytest.mark.parametrize(
    "candidate",
    [
        # Unlisted ports and hosts that the old prefix rule accepted.
        "http://localhost:3001/auth/callback",
        "http://localhost:9999/",
        "http://127.0.0.1:3000/auth/callback",
        "http://127.0.0.1:8000/anything",
        # Lookalike hosts: userinfo tricks and look-alike domains.
        "http://localhost:@evil.example/auth/callback",
        "http://localhost:3000@evil.example/auth/callback",
        "http://localhost:3000.evil.example/auth/callback",
        "http://localhost.evil.example:3000/auth/callback",
        "http://localhost%3A3000@evil.example/",
        "https://codeforge.example.com.evil.example/auth/callback",
        # Same origin, unexpected paths.
        "http://localhost:3000/auth/callback/",
        "http://localhost:3000/auth/callbackx",
        "http://localhost:3000/auth/callback/../../steal",
        "http://localhost:3000/auth/callback?next=https://evil.example",
        "http://localhost:3000/auth/callback#x",
        "http://localhost:3000/",
        # Scheme and case variants, and malformed values.
        "https://localhost:3000/auth/callback",
        "HTTP://LOCALHOST:3000/auth/callback",
        "//localhost:3000/auth/callback",
        "javascript:alert(1)",
        " http://localhost:3000/auth/callback",
        "http://localhost:3000/auth/callback\n",
        "not a url",
        "https://evil-phishing-site.com/steal-token",
    ],
)
def test_anything_not_listed_exactly_is_refused(allowlist: list[str], candidate: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        AuthService().validate_redirect_uri(candidate)
    assert exc_info.value.status_code == 400


def test_the_review_console_callback_is_allowed_by_default() -> None:
    # apps/web signs in through http://localhost:3000/auth/callback.
    factory = type(settings).model_fields["allowed_oauth_redirect_uris"].default_factory
    assert factory is not None
    default = factory()  # type: ignore[call-arg]
    assert CONSOLE_CALLBACK in default
    for entry in default:
        parsed = urlparse(entry)
        assert parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
        assert not parsed.query and not parsed.fragment


def test_login_endpoint_refuses_an_unlisted_redirect(
    allowlist: list[str], fake_oauth_redis: FakeOAuthRedis
) -> None:
    client = TestClient(create_app())
    refused = client.get("/api/v1/auth/github/login", params={"redirect_uri": "http://localhost:@evil.example/"})
    assert refused.status_code == 400
    assert fake_oauth_redis.data == {}  # no state issued for a refused redirect
    accepted = client.get("/api/v1/auth/github/login", params={"redirect_uri": CONSOLE_CALLBACK})
    assert accepted.status_code == 200
    query = parse_qs(urlparse(accepted.json()["authorization_url"]).query)
    assert query["redirect_uri"] == [CONSOLE_CALLBACK]


# ── CF-13: state is consumed once, atomically, and only from Redis ─────────────


@pytest.mark.asyncio
async def test_state_round_trip_and_sequential_replay(fake_oauth_redis: FakeOAuthRedis) -> None:
    service = AuthService()
    _, state = await service.generate_oauth_url(CONSOLE_CALLBACK)
    assert len(state) == 43
    assert (await service.verify_and_consume_state(state))["redirect_uri"] == CONSOLE_CALLBACK
    with pytest.raises(HTTPException) as exc_info:
        await service.verify_and_consume_state(state)
    assert exc_info.value.status_code == 400
    assert fake_oauth_redis.data == {}


@pytest.mark.asyncio
async def test_concurrent_callbacks_accept_a_state_at_most_once(fake_oauth_redis: FakeOAuthRedis) -> None:
    service = AuthService()
    _, state = await service.generate_oauth_url(CONSOLE_CALLBACK)

    async def consume() -> bool:
        try:
            await service.verify_and_consume_state(state)
            return True
        except HTTPException:
            return False

    results = await asyncio.gather(*(consume() for _ in range(20)))
    assert results.count(True) == 1


@pytest.mark.asyncio
async def test_expired_state_is_refused(fake_oauth_redis: FakeOAuthRedis) -> None:
    service = AuthService()
    _, state = await service.generate_oauth_url(CONSOLE_CALLBACK)
    key = f"oauth_state:{state}"
    assert fake_oauth_redis.data[key][1] == settings.oauth_state_ttl_seconds  # TTL preserved
    fake_oauth_redis.now = settings.oauth_state_ttl_seconds + 1
    with pytest.raises(HTTPException) as exc_info:
        await service.verify_and_consume_state(state)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("phase", ["login", "callback"])
@pytest.mark.asyncio
async def test_without_redis_sign_in_fails_closed(fake_oauth_redis: FakeOAuthRedis, phase: str) -> None:
    service = AuthService()
    if phase == "login":
        fake_oauth_redis.unavailable = True
        with pytest.raises(HTTPException) as exc_info:
            await service.generate_oauth_url(CONSOLE_CALLBACK)
    else:
        _, state = await service.generate_oauth_url(CONSOLE_CALLBACK)
        fake_oauth_redis.unavailable = True
        with pytest.raises(HTTPException) as exc_info:
            await service.verify_and_consume_state(state)
        # Once Redis is back, the state is still single-use: nothing was
        # accepted from anywhere else meanwhile.
        fake_oauth_redis.unavailable = False
        await service.verify_and_consume_state(state)
        with pytest.raises(HTTPException):
            await service.verify_and_consume_state(state)
    assert exc_info.value.status_code == 503


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.asyncio
async def test_an_ambiguous_state_write_issues_no_state(fake_oauth_redis: FakeOAuthRedis, when: str) -> None:
    fake_oauth_redis.fail_setex = when
    with pytest.raises(HTTPException) as exc_info:
        await AuthService().generate_oauth_url(CONSOLE_CALLBACK)
    assert exc_info.value.status_code == 503
    # At most the one Redis copy exists (if the write landed), never a second
    # one anywhere else, and its nonce never left the server.
    assert len(fake_oauth_redis.data) == (1 if when == "after" else 0)
    for key in list(fake_oauth_redis.data):
        state = key.removeprefix("oauth_state:")
        fake_oauth_redis.fail_setex = None
        await AuthService().verify_and_consume_state(state)
        with pytest.raises(HTTPException):
            await AuthService().verify_and_consume_state(state)


def test_there_is_no_in_process_state_store() -> None:
    # The fallback that made CF-13's double redemption possible is gone, so
    # there is nothing to bound or prune.
    names = [name for name in vars(auth_module) if "memory" in name.lower() or "store" in name.lower()]
    assert names == []


@pytest.mark.parametrize("state", ["", "short", "x" * 44, "a" * 42 + "!", "../" + "a" * 40, "a" * 43 + "\n"])
@pytest.mark.asyncio
async def test_malformed_states_are_refused_without_touching_redis(
    fake_oauth_redis: FakeOAuthRedis, state: str
) -> None:
    fake_oauth_redis.unavailable = True  # any Redis call would raise 503
    with pytest.raises(HTTPException) as exc_info:
        await AuthService().verify_and_consume_state(state)
    assert exc_info.value.status_code == 400


def test_callback_endpoint_fails_closed_without_redis(fake_oauth_redis: FakeOAuthRedis) -> None:
    fake_oauth_redis.unavailable = True
    client = TestClient(create_app())
    assert client.get("/api/v1/auth/github/login").status_code == 503
    response = client.get("/api/v1/auth/github/callback", params={"code": "c", "state": "a" * 43})
    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]


@pytest.mark.asyncio
async def test_state_values_and_codes_never_reach_the_logs(
    fake_oauth_redis: FakeOAuthRedis, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    service = AuthService()
    _, state = await service.generate_oauth_url(CONSOLE_CALLBACK)
    await service.verify_and_consume_state(state)
    fake_oauth_redis.unavailable = True
    with pytest.raises(HTTPException):
        await service.verify_and_consume_state(state)
    assert state not in caplog.text


# ── Against a real Redis server, when one is reachable ─────────────────────────


@pytest_asyncio.fixture
async def real_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    client = Redis.from_url(os.getenv("REDIS_URL", settings.redis_url), decode_responses=True, socket_connect_timeout=1)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis is not reachable; the fake-based tests above cover this logic")
    monkeypatch.setattr("app.services.auth_service.get_redis_client", lambda: client)
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_real_redis_accepts_a_state_once_under_concurrency(real_redis: Redis) -> None:
    service = AuthService()
    _, state = await service.generate_oauth_url(CONSOLE_CALLBACK)
    ttl = await real_redis.ttl(f"oauth_state:{state}")
    assert 0 < ttl <= settings.oauth_state_ttl_seconds

    async def consume() -> bool:
        try:
            await service.verify_and_consume_state(state)
            return True
        except HTTPException:
            return False

    results = await asyncio.gather(*(consume() for _ in range(25)))
    assert results.count(True) == 1
    assert await real_redis.exists(f"oauth_state:{state}") == 0
