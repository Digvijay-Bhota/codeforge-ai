"""CF-06 regression tests: repository-scoped, least-privilege GitHub credentials.

Before this fix installation tokens were minted with no request body, so each
covered every repository and every permission of the installation, and they
were cached per installation.  Worse, cloning, branch creation, pushing and
pull-request creation did not use installation tokens at all: they used one
static, long-lived GITHUB_TOKEN for every repository.

Now every token request names exactly one repository and an explicit minimal
permission set; tokens are cached per (installation, repository, permissions);
generation only ever holds a read-only token; publication obtains a write
token only after the CF-05 approval checks; and nothing falls back to a
static token.
"""

from __future__ import annotations

import json
import logging
import random
import re
import subprocess  # nosec B404 - runs git on local test repositories only
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import GitHubInstallation, GitHubInstallationRepository, Task, TaskGitHubLink
from app.execution.github_execution import GitHubExecutionService
from app.github.exceptions import GitHubAuthorizationError, GitHubTimeoutError
from app.github.git import SafeGitWrapper
from app.github.models import GitHubRepository
from app.github.repository_access import (
    COLLABORATOR_PERMISSIONS,
    GENERATION_PERMISSIONS,
    IDENTITY_PERMISSIONS,
    PUBLICATION_PERMISSIONS,
    STATUS_PERMISSIONS,
    RepositoryAccessError,
    clear_repository_id_cache,
    resolve_repository_access,
)
from app.github.token_service import (
    ALLOWED_TOKEN_PERMISSIONS,
    CachedInstallationToken,
    InstallationTokenService,
    TokenScope,
    _cache_key,
)
from app.schemas.task import ExecutionTarget, GitHubFailureStage, TaskRequest, TaskStatus
from app.services.task_service import TaskService
from tests.conftest import install_github_doubles, repository_access_for

APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture(autouse=True)
def github_app(monkeypatch: pytest.MonkeyPatch) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    monkeypatch.setattr(settings, "github_app_id", "12345")
    monkeypatch.setattr(settings, "github_app_private_key", pem)
    clear_repository_id_cache()
    yield
    clear_repository_id_cache()


class TokenEndpoint:
    """A fake GitHub /app/installations/{id}/access_tokens endpoint."""

    def __init__(self, respond: Any = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"null")
        self.requests.append({"url": str(request.url), "body": body})
        if self.respond is not None:
            return self.respond(body)
        return httpx.Response(201, json=self.grant(body, token=f"ghs_tok_{len(self.requests)}"))

    @staticmethod
    def grant(body: dict[str, Any], *, token: str, expires_in: int = 3600) -> dict[str, Any]:
        """What GitHub returns for a correctly scoped request."""
        repos = [{"id": rid} for rid in body.get("repository_ids", [])] + [
            {"name": name} for name in body.get("repositories", [])
        ]
        return {
            "token": token,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
            "permissions": body["permissions"],
            "repository_selection": "selected",
            "repositories": repos,
        }

    def service(self) -> InstallationTokenService:
        return InstallationTokenService(
            refresh_buffer_seconds=300, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self))
        )


READ_A = TokenScope.for_repository_id(101, GENERATION_PERMISSIONS)
READ_B = TokenScope.for_repository_id(202, GENERATION_PERMISSIONS)
WRITE_A = TokenScope.for_repository_id(101, PUBLICATION_PERMISSIONS)


# ── Token requests are scoped ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_request_names_one_repository_and_explicit_permissions() -> None:
    endpoint = TokenEndpoint()
    token = await endpoint.service().get_token(77, WRITE_A)
    assert token == "ghs_tok_1"
    [request] = endpoint.requests
    assert request["url"].endswith("/app/installations/77/access_tokens")
    assert request["body"] == {
        "repository_ids": [101],
        "permissions": {"contents": "write", "pull_requests": "write"},
    }


@pytest.mark.asyncio
async def test_identity_lookup_token_is_name_scoped_and_metadata_only() -> None:
    endpoint = TokenEndpoint()
    await endpoint.service().get_token(77, TokenScope.for_repository_name("widgets", IDENTITY_PERMISSIONS))
    assert endpoint.requests[0]["body"] == {"repositories": ["widgets"], "permissions": {"metadata": "read"}}


@pytest.mark.asyncio
async def test_no_token_without_an_explicit_scope() -> None:
    endpoint = TokenEndpoint()
    service = endpoint.service()
    with pytest.raises(TypeError):
        await service.get_token(77)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="TokenScope"):
        await service.get_token(77, None)  # type: ignore[arg-type]
    assert endpoint.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"permissions": ()},  # nothing implicit: an empty permission set is refused
        {"permissions": (("administration", "write"),)},
        {"permissions": (("workflows", "write"),)},
        {"permissions": (("secrets", "read"),)},
        {"permissions": (("metadata", "write"),)},
        {"permissions": (("contents", "admin"),)},
        {"permissions": (("pull_requests", "write"), ("contents", "read"))},  # not normalized
        {"permissions": (("contents", "read"), ("contents", "write"))},
    ],
)
def test_overly_broad_or_malformed_permission_sets_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        TokenScope(repository_id=101, **kwargs)


@pytest.mark.parametrize(
    "repository",
    [{}, {"repository_id": 1, "repository_name": "r"}, {"repository_id": 0}, {"repository_id": -5},
     {"repository_id": True}, {"repository_name": "a/b"}, {"repository_name": ".."}, {"repository_name": ""}],
)  # fmt: skip
def test_scope_must_name_exactly_one_valid_repository(repository: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        TokenScope(permissions=(("metadata", "read"),), **repository)


def test_consumer_permission_sets_match_the_documented_minimum() -> None:
    assert GENERATION_PERMISSIONS == {"contents": "read", "metadata": "read"}
    assert PUBLICATION_PERMISSIONS == {"contents": "write", "pull_requests": "write"}
    assert STATUS_PERMISSIONS == {"checks": "write", "issues": "write", "pull_requests": "read"}
    assert COLLABORATOR_PERMISSIONS == IDENTITY_PERMISSIONS == {"metadata": "read"}
    assert "write" not in GENERATION_PERMISSIONS.values()
    for permissions in (GENERATION_PERMISSIONS, PUBLICATION_PERMISSIONS, STATUS_PERMISSIONS, COLLABORATOR_PERMISSIONS):
        for name, level in permissions.items():
            assert level in ALLOWED_TOKEN_PERMISSIONS[name]


# ── Cache separation and expiry ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tokens_are_never_shared_across_repositories_permissions_or_installations() -> None:
    endpoint = TokenEndpoint()
    service = endpoint.service()
    tokens = [
        await service.get_token(77, READ_A),
        await service.get_token(77, READ_B),     # other repository
        await service.get_token(77, WRITE_A),    # other permission set
        await service.get_token(88, READ_A),     # other installation
    ]  # fmt: skip
    assert len(set(tokens)) == 4 and len(endpoint.requests) == 4
    # Same scope again: served from the cache, no new request.
    assert await service.get_token(77, READ_B) == tokens[1]
    assert await service.get_token(77, TokenScope.for_repository_id(101, dict(GENERATION_PERMISSIONS))) == tokens[0]
    assert len(endpoint.requests) == 4


@pytest.mark.parametrize("remaining", [-60, 0, 299])
@pytest.mark.asyncio
async def test_expired_or_near_expiry_tokens_are_not_reused(remaining: int) -> None:
    endpoint = TokenEndpoint()
    service = endpoint.service()
    service._cache[_cache_key(77, READ_A)] = CachedInstallationToken(
        token="ghs_old", installation_id=77, expires_at=datetime.now(UTC) + timedelta(seconds=remaining), scope=READ_A
    )
    assert await service.get_token(77, READ_A) == "ghs_tok_1"
    assert len(endpoint.requests) == 1


@pytest.mark.asyncio
async def test_invalidation_evicts_one_scope_or_the_whole_installation() -> None:
    endpoint = TokenEndpoint()
    service = endpoint.service()
    for scope in (READ_A, READ_B, WRITE_A):
        await service.get_token(77, scope)
    await service.get_token(88, READ_A)
    service.invalidate(77, READ_A)
    assert _cache_key(77, READ_A) not in service._cache and _cache_key(77, READ_B) in service._cache
    service.invalidate(77)
    assert [key for key in service._cache if key[0] == 77] == []
    assert _cache_key(88, READ_A) in service._cache


# ── A token broader than requested is refused ──────────────────────────────────


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda g: g.update(repository_selection="all"), "repository_selection"),
        (lambda g: g.update(repositories=[{"id": 101}, {"id": 999}]), "more than one repository"),
        (lambda g: g.update(repositories=[{"id": 999}]), "different repository"),
        (lambda g: g["permissions"].update(administration="write"), "unrequested permission"),
        (lambda g: g["permissions"].update(contents="write"), "unrequested permission"),
        (lambda g: g["permissions"].pop("contents"), "was not granted"),
    ],
)
@pytest.mark.asyncio
async def test_token_broader_than_requested_is_refused_and_not_cached(
    mutate: Any, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def respond(body: dict[str, Any]) -> httpx.Response:
        granted = TokenEndpoint.grant(body, token="ghs_too_broad_secret")
        mutate(granted)
        return httpx.Response(201, json=granted)

    endpoint = TokenEndpoint(respond)
    service = endpoint.service()
    with pytest.raises(GitHubAuthorizationError, match=reason) as exc_info:
        await service.get_token(77, READ_A)
    assert service._cache == {}
    assert "ghs_too_broad_secret" not in str(exc_info.value) and "ghs_too_broad_secret" not in caplog.text
    with pytest.raises(GitHubAuthorizationError):
        await service.get_token(77, READ_A)
    assert len(endpoint.requests) == 2  # retried, never served from cache


@pytest.mark.asyncio
async def test_extra_read_only_metadata_grant_is_tolerated() -> None:
    def respond(body: dict[str, Any]) -> httpx.Response:
        granted = TokenEndpoint.grant(body, token="ghs_ok")
        granted["permissions"]["metadata"] = "read"
        return httpx.Response(201, json=granted)

    assert await TokenEndpoint(respond).service().get_token(77, WRITE_A) == "ghs_ok"


@pytest.mark.asyncio
async def test_minted_tokens_never_appear_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    service = TokenEndpoint().service()
    token = await service.get_token(77, WRITE_A)
    assert token not in caplog.text
    assert token not in repr(service._cache[_cache_key(77, WRITE_A)])


# ── Repository resolution fails closed ─────────────────────────────────────────


async def _install(session: AsyncSession, *, owner: str, repo: str, account: str | None = None,
                   active: bool = True, authorized: bool = True) -> int:  # fmt: skip
    installation_id = random.randint(1_000_000, 999_999_999)
    session.add(GitHubInstallation(installation_id=installation_id, account_login=account or owner,
                                   account_type="Organization", active=active))  # fmt: skip
    session.add(GitHubInstallationRepository(installation_id=installation_id, repository=f"{owner}/{repo}",
                                             authorized=authorized))  # fmt: skip
    await session.commit()
    return installation_id


def _identity(full_name: str | None = None, repository_id: int = 4242) -> MagicMock:
    client = MagicMock()
    client.aclose = AsyncMock()
    client.get_repository = AsyncMock(side_effect=lambda owner, name: GitHubRepository(
        id=repository_id, owner=owner, name=name, full_name=full_name or f"{owner}/{name}",
        default_branch="main", private=True, clone_url="", html_url="",
    ))  # fmt: skip
    cls = MagicMock()
    cls.for_installation.return_value = client
    return cls


@pytest.mark.asyncio
async def test_repository_is_resolved_to_a_verified_id_with_a_metadata_only_token(session: AsyncSession) -> None:
    owner = f"org-{uuid.uuid4().hex[:6]}"
    installation_id = await _install(session, owner=owner, repo="widgets")
    identity = _identity(repository_id=4242)
    with patch("app.github.repository_access.GitHubClient", identity):
        access = await resolve_repository_access(session, f"{owner}/widgets")
        again = await resolve_repository_access(session, f"{owner}/widgets")
    assert (access.installation_id, access.repository_id, access.full_name) == (installation_id, 4242, f"{owner}/widgets")
    assert again == access
    identity.for_installation.assert_called_once_with(
        installation_id, scope=TokenScope.for_repository_name("widgets", IDENTITY_PERMISSIONS)
    )  # the verified ID is cached; only one lookup
    assert access.scope(PUBLICATION_PERMISSIONS) == TokenScope.for_repository_id(4242, PUBLICATION_PERMISSIONS)


@pytest.mark.parametrize(
    "setup",
    [
        {"installed": False},
        {"authorized": False},
        {"active": False},
        {"account": "someone-else"},  # the row names a repository outside the installation's account
    ],
)
@pytest.mark.asyncio
async def test_unauthorized_or_foreign_repository_gets_no_credential(session: AsyncSession, setup: dict[str, Any]) -> None:
    owner = f"org-{uuid.uuid4().hex[:6]}"
    if setup.pop("installed", True):
        await _install(session, owner=owner, repo="widgets", **setup)
    identity = _identity()
    with patch("app.github.repository_access.GitHubClient", identity), pytest.raises(RepositoryAccessError):
        await resolve_repository_access(session, f"{owner}/widgets")
    identity.for_installation.assert_not_called()


@pytest.mark.asyncio
async def test_repository_identity_mismatch_fails_closed(session: AsyncSession) -> None:
    owner = f"org-{uuid.uuid4().hex[:6]}"
    await _install(session, owner=owner, repo="widgets")
    with patch("app.github.repository_access.GitHubClient", _identity(full_name="other/widgets")), \
            pytest.raises(RepositoryAccessError, match="different repository"):  # fmt: skip
        await resolve_repository_access(session, f"{owner}/widgets")


@pytest.mark.asyncio
async def test_transient_identity_failure_is_retryable_and_never_falls_back(session: AsyncSession) -> None:
    owner = f"org-{uuid.uuid4().hex[:6]}"
    await _install(session, owner=owner, repo="widgets")
    identity = _identity()
    identity.for_installation.return_value.get_repository = AsyncMock(side_effect=GitHubTimeoutError("slow"))
    with patch("app.github.repository_access.GitHubClient", identity), pytest.raises(RepositoryAccessError) as exc:
        await resolve_repository_access(session, f"{owner}/widgets")
    assert exc.value.retryable is True


# ── Generation: read-only credentials, fail closed ─────────────────────────────


def _request(repository: str = "o/r") -> TaskRequest:
    return TaskRequest(execution_target=ExecutionTarget.github, github_repository=repository, description="Fix the widget calculation")


@pytest.mark.asyncio
async def test_generation_without_an_authorized_installation_never_touches_github(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_static_token_must_not_be_used")
    with patch("app.execution.github_execution.GitHubExecutionService") as service_cls, \
            patch("app.github.repository_access.GitHubClient") as identity:  # fmt: skip
        result = await TaskService(session).execute_task(_request("nobody/nothing"), "t1")
    assert result.status == TaskStatus.error
    assert "not authorized" in (result.error_message or "")
    service_cls.assert_not_called()
    identity.for_installation.assert_not_called()


@pytest.mark.asyncio
async def test_generation_token_failure_stops_before_clone_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_static_token_must_not_be_used")
    monkeypatch.setattr(settings, "codeforge_github_clone_root", str(tmp_path))
    client = MagicMock()
    client.get_repository = AsyncMock(return_value=MagicMock(default_branch="main"))
    client.get_branch = AsyncMock(return_value=MagicMock(sha="a" * 40))
    tokens = install_github_doubles(monkeypatch, client)
    tokens.get_token = AsyncMock(side_effect=GitHubAuthorizationError("app lacks contents:read"))  # type: ignore[method-assign]
    git_cls = MagicMock()
    with patch("app.execution.github_execution.SafeGitWrapper", git_cls), patch("app.services.task_service.Orchestrator"):
        result = await GitHubExecutionService(repository_access_for("o/r")).execute(_request(), "t1")
    assert result.status == TaskStatus.error
    assert result.failure_stage == GitHubFailureStage.CREDENTIALS_FAILED
    git_cls.assert_not_called()


@pytest.mark.asyncio
async def test_credentials_for_another_repository_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    tokens = install_github_doubles(monkeypatch, client)
    with patch("app.services.task_service.Orchestrator"):
        service = GitHubExecutionService(repository_access_for("o/other"))
        result = await service.execute(_request("o/r"), "t1")
        with pytest.raises(Exception, match="do not match"):
            await service.publish("t1", {"repository": "o/r"}, approved_by="a", authorize=AsyncMock())
    assert result.failure_stage == GitHubFailureStage.CREDENTIALS_FAILED
    assert tokens.minted == []


# ── Status updates ─────────────────────────────────────────────────────────────


async def _linked_task(session: AsyncSession, *, active: bool) -> TaskGitHubLink:
    owner = f"org-{uuid.uuid4().hex[:6]}"
    installation_id = await _install(session, owner=owner, repo="widgets", active=active)
    task = Task(task_id=str(uuid.uuid4()), status="RUNNING", requested_task="x", execution_target="github",
                repository=f"{owner}/widgets")  # fmt: skip
    session.add(task)
    await session.flush()
    link = TaskGitHubLink(task_id=task.task_id, installation_id=installation_id, repository_id=5150,
                          repository_full_name=f"{owner}/widgets", issue_number=7, triggering_github_user_id=1,
                          triggering_github_login="dev", acknowledgement_comment_id=99)  # fmt: skip
    session.add(link)
    await session.commit()
    return link


@pytest.mark.asyncio
async def test_status_updates_use_a_status_only_token_for_the_linked_repository(session: AsyncSession) -> None:
    from app.github.status_consumer import GitHubStatusConsumer

    link = await _linked_task(session, active=True)
    with patch("app.github.status_consumer.GitHubClient") as client_cls:
        client = await GitHubStatusConsumer(session)._get_client(link)
    assert client is client_cls.for_installation.return_value
    client_cls.for_installation.assert_called_once_with(
        link.installation_id, scope=TokenScope.for_repository_id(5150, STATUS_PERMISSIONS)
    )


@pytest.mark.asyncio
async def test_status_updates_stop_when_the_installation_is_no_longer_active(session: AsyncSession) -> None:
    from app.github.status_consumer import GitHubStatusConsumer

    link = await _linked_task(session, active=False)
    with patch("app.github.status_consumer.GitHubClient") as client_cls:
        assert await GitHubStatusConsumer(session).sync_status(link.task_id, event_name="JOB_STARTED") is None
    client_cls.for_installation.assert_not_called()


# ── No static-token path remains in the application ────────────────────────────


# Patterns that would reintroduce a static or unscoped credential in runtime code.
FORBIDDEN = {
    "static token setting": re.compile(r"\bgithub_token\b(?!_)"),
    "GITHUB_TOKEN from the environment": re.compile(r"GITHUB_TOKEN"),
    "explicit-token client": re.compile(r"\.for_token\("),
    "token credential mode": re.compile(r"CredentialMode\.TOKEN"),
    "client without credentials": re.compile(r"\bGitHubClient\(\s*\)"),
    "unscoped installation client": re.compile(r"for_installation\((?![^)]*scope=)[^)]*\)"),
}


def test_no_runtime_code_uses_a_static_or_unscoped_github_credential() -> None:
    offenders = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel == "github/client.py":  # defines the modes; checked by its own tests
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            for what, pattern in FORBIDDEN.items():
                if pattern.search(code):
                    offenders.append(f"{rel}:{number}: {what}")
    assert offenders == []
    assert not hasattr(settings, "github_token")


def test_static_scan_catches_the_patterns_it_forbids() -> None:
    samples = {
        "static token setting": "token = settings.github_token",
        "GITHUB_TOKEN from the environment": 'os.environ["GITHUB_TOKEN"]',
        "explicit-token client": "GitHubClient.for_token(secret)",
        "token credential mode": "mode=CredentialMode.TOKEN",
        "client without credentials": "client = GitHubClient()",
        "unscoped installation client": "GitHubClient.for_installation(installation_id)",
    }
    for what, sample in samples.items():
        assert FORBIDDEN[what].search(sample), what
    assert not FORBIDDEN["static token setting"].search("settings.github_token_refresh_buffer_seconds")
    assert not FORBIDDEN["explicit-token client"].search("exchange_code_for_token(code)")
    assert not FORBIDDEN["unscoped installation client"].search("GitHubClient.for_installation(i, scope=s)")


# ── Tokens stay out of git configuration and error output ──────────────────────


def test_switching_tokens_keeps_every_token_out_of_config_and_errors(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # nosec B603 B607
    wrapper = SafeGitWrapper(repo, "ghs_read_only_token_111")
    wrapper.set_token("ghs_write_token_222")
    assert wrapper.has_changes() is False  # runs authenticated-capable git commands
    config = (repo / ".git" / "config").read_text()
    for secret in ("ghs_read_only_token_111", "ghs_write_token_222"):
        assert secret not in config
        assert secret not in wrapper._redact(f"fatal: header {secret} rejected")
