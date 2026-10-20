"""CF-01 regression tests: task observability endpoints require task read access.

``GET /api/v1/tasks/{task_id}/timeline`` returns persisted tool-call arguments
and results (repository file content) and ``/evaluation`` returns the task's
evaluation.  Both were reachable without any authentication.  They must now
apply the same policy as ``GET /api/v1/tasks/{task_id}``: an authenticated,
active user who can read the task's repository (or created the local task).
A caller without access gets the same 404 as for a task that does not exist.

The database is replaced by in-memory fakes at the repository layer; session
tokens, ``get_current_user`` and the task access helpers run unmodified.  The
route inventory test guards against a future router being mounted without
authentication.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import deps, observability, task_access
from app.api.deps import get_current_user
from app.config import DEFAULT_JWT_SECRET, settings
from app.db.session import get_db_session
from app.main import create_app
from app.services.auth_service import AuthService

# Stands in for repository file content captured in tool-call metadata.
PROTECTED = "PROTECTED-REPOSITORY-CONTENT-7f3a"
REPO = "acme/widgets"
ENDPOINTS = ("timeline", "evaluation")

OWNER = SimpleNamespace(
    id=str(uuid.uuid4()), is_active=True, display_name="owner", email="owner@example.com"
)
OTHER = SimpleNamespace(
    id=str(uuid.uuid4()), is_active=True, display_name="other", email="other@example.com"
)
REPO_READER = SimpleNamespace(
    id=str(uuid.uuid4()), is_active=True, display_name="reader", email="reader@example.com"
)
INACTIVE = SimpleNamespace(
    id=str(uuid.uuid4()), is_active=False, display_name="gone", email="gone@example.com"
)
USERS = {u.id: u for u in (OWNER, OTHER, REPO_READER, INACTIVE)}

LOCAL_TASK = SimpleNamespace(task_id=str(uuid.uuid4()), repository=None, creator_id=OWNER.id)
REPO_TASK = SimpleNamespace(task_id=str(uuid.uuid4()), repository=REPO, creator_id=OWNER.id)
TASKS = {t.task_id: t for t in (LOCAL_TASK, REPO_TASK)}
# Only these users can read REPO on GitHub.
REPO_READERS = {OWNER.id, REPO_READER.id}


class FakeUserRepository:
    def __init__(self, session: Any) -> None:
        pass

    async def get_user(self, user_id: str) -> Any:
        return USERS.get(user_id)


class FakeTaskRepository:
    def __init__(self, session: Any) -> None:
        pass

    async def get_task(self, task_id: str) -> Any:
        return TASKS.get(task_id)


class FakeAuthorizationService:
    def __init__(self, session: Any) -> None:
        pass

    async def can_read_repository(self, user: Any, repository: str) -> bool:
        return repository == REPO and user.id in REPO_READERS


class FakeObservabilityRepository:
    """Records every read, so tests can prove denied requests never reach the data."""

    reads: list[str] = []

    def __init__(self, session: Any) -> None:
        pass

    async def get_timeline(self, task_id: str, limit: int, offset: int) -> list[Any]:
        self.reads.append(task_id)
        return [
            SimpleNamespace(
                event_type="TOOL_CALL_COMPLETED",
                component="mcp",
                stage=None,
                started_at=None,
                completed_at=None,
                duration_ms=1,
                status="success",
                metadata_payload={"tool": "repository.read_file", "result_summary": PROTECTED},
                error_code=None,
                created_at=None,
            )
        ]

    async def get_evaluation(self, task_id: str) -> Any:
        self.reads.append(task_id)
        return SimpleNamespace(
            task_id=task_id,
            execution_id="exec-1",
            overall_status="completed",
            task_success=True,
            tests_passed=1,
            tests_failed=0,
            tests_total=1,
            changes_made=True,
            planned_files=1,
            changed_files=1,
            plan_adherence=1.0,
            regression_detected=False,
            security_violation=False,
            tool_failure_count=0,
            model_call_count=1,
            duration_ms=1,
            estimated_cost_usd=0.0,
            score=PROTECTED,
            created_at=None,
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(deps, "UserRepository", FakeUserRepository)
    monkeypatch.setattr(task_access, "TaskRepository", FakeTaskRepository)
    monkeypatch.setattr(task_access, "AuthorizationService", FakeAuthorizationService)
    monkeypatch.setattr(observability, "ObservabilityRepository", FakeObservabilityRepository)
    FakeObservabilityRepository.reads = []

    async def no_db() -> Any:
        yield None

    app = create_app()
    app.dependency_overrides[get_db_session] = no_db
    # Not used as a context manager: the lifespan would dispose real DB/Redis clients.
    yield TestClient(app)


def bearer(user: Any) -> dict[str, str]:
    token = AuthService().create_session_jwt(user, github_user_id=1, github_login=user.display_name)
    return {"Authorization": f"Bearer {token}"}


def url(task_id: str, endpoint: str) -> str:
    return f"/api/v1/tasks/{task_id}/{endpoint}"


# ── Anonymous and invalid credentials ──────────────────────────────────────────


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("task", [LOCAL_TASK, REPO_TASK], ids=["local", "repository"])
def test_anonymous_request_is_rejected(client: TestClient, endpoint: str, task: Any) -> None:
    res = client.get(url(task.task_id, endpoint))
    assert res.status_code == 401
    assert PROTECTED not in res.text
    assert FakeObservabilityRepository.reads == []


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Basic b3duZXI6cGFzcw=="},
    ],
    ids=["empty", "garbage", "wrong-scheme"],
)
def test_invalid_credentials_are_rejected(
    client: TestClient, endpoint: str, headers: dict[str, str]
) -> None:
    res = client.get(url(LOCAL_TASK.task_id, endpoint), headers=headers)
    assert res.status_code == 401
    assert PROTECTED not in res.text
    assert FakeObservabilityRepository.reads == []


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_token_forged_with_public_default_secret_is_rejected(
    client: TestClient, endpoint: str
) -> None:
    """The CF-02 reproduction, end to end: a default-secret token for the owner fails."""
    assert settings.jwt_secret_key != DEFAULT_JWT_SECRET
    forged = jwt.encode(
        {"sub": OWNER.id, "type": "session", "exp": int(time.time()) + 600},
        DEFAULT_JWT_SECRET,
        algorithm="HS256",
    )
    res = client.get(
        url(LOCAL_TASK.task_id, endpoint), headers={"Authorization": f"Bearer {forged}"}
    )
    assert res.status_code == 401
    assert PROTECTED not in res.text
    assert FakeObservabilityRepository.reads == []


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_inactive_user_is_rejected(client: TestClient, endpoint: str) -> None:
    res = client.get(url(LOCAL_TASK.task_id, endpoint), headers=bearer(INACTIVE))
    assert res.status_code == 401
    assert FakeObservabilityRepository.reads == []


# ── Authorized access ──────────────────────────────────────────────────────────


def test_owner_reads_local_task_timeline(client: TestClient) -> None:
    res = client.get(url(LOCAL_TASK.task_id, "timeline"), headers=bearer(OWNER))
    assert res.status_code == 200
    body = res.json()
    assert body["task_id"] == LOCAL_TASK.task_id
    assert body["events"][0]["metadata"]["result_summary"] == PROTECTED


def test_owner_reads_local_task_evaluation(client: TestClient) -> None:
    res = client.get(url(LOCAL_TASK.task_id, "evaluation"), headers=bearer(OWNER))
    assert res.status_code == 200
    assert res.json()["task_id"] == LOCAL_TASK.task_id


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_repository_reader_can_read_repository_task(client: TestClient, endpoint: str) -> None:
    res = client.get(url(REPO_TASK.task_id, endpoint), headers=bearer(REPO_READER))
    assert res.status_code == 200
    assert FakeObservabilityRepository.reads == [REPO_TASK.task_id]


def test_missing_evaluation_for_readable_task_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def none(self: Any, task_id: str) -> None:
        return None

    monkeypatch.setattr(FakeObservabilityRepository, "get_evaluation", none)
    res = client.get(url(LOCAL_TASK.task_id, "evaluation"), headers=bearer(OWNER))
    assert res.status_code == 404
    assert res.json() == {"detail": "Evaluation not found for task"}


# ── Unrelated users and existence disclosure ───────────────────────────────────


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("task", [LOCAL_TASK, REPO_TASK], ids=["local", "repository"])
def test_unrelated_user_cannot_read_task(client: TestClient, endpoint: str, task: Any) -> None:
    res = client.get(url(task.task_id, endpoint), headers=bearer(OTHER))
    assert res.status_code == 404
    assert res.json() == {"detail": "Task not found"}
    assert PROTECTED not in res.text
    assert FakeObservabilityRepository.reads == []


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_denied_and_missing_tasks_are_indistinguishable(client: TestClient, endpoint: str) -> None:
    denied = client.get(url(REPO_TASK.task_id, endpoint), headers=bearer(OTHER))
    missing = client.get(url(str(uuid.uuid4()), endpoint), headers=bearer(OTHER))
    assert (denied.status_code, denied.json()) == (missing.status_code, missing.json())


# ── Intentionally public routes stay public ────────────────────────────────────

# Every /api/v1 route must depend on get_current_user except these.
PUBLIC_API_ROUTES = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/ready"),
    ("GET", "/api/v1/auth/github/login"),
    ("GET", "/api/v1/auth/github/callback"),
    ("POST", "/api/v1/github/webhooks"),  # authenticated by its HMAC signature
}


def _depends_on(dependant: Any, target: Any) -> bool:
    return any(
        dep.call is target or _depends_on(dep, target) for dep in dependant.dependencies
    )


def test_every_api_route_requires_authentication_unless_explicitly_public() -> None:
    routes = [
        (method, route)
        for route in create_app().routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/")
        for method in route.methods
    ]
    seen = {(method, route.path) for method, route in routes}
    assert PUBLIC_API_ROUTES <= seen, "public allow-list names a route that no longer exists"
    unauthenticated = sorted(
        f"{method} {route.path}"
        for method, route in routes
        if (method, route.path) not in PUBLIC_API_ROUTES
        and not _depends_on(route.dependant, get_current_user)
    )
    assert unauthenticated == []


def test_public_routes_need_no_session_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_redis() -> Any:
        raise ConnectionError("redis unavailable in this test")

    # OAuth state falls back to the in-process store without Redis.
    monkeypatch.setattr("app.services.auth_service.get_redis_client", no_redis)
    monkeypatch.setattr(settings, "github_webhook_secret", "")

    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/health").status_code == 200
    login = client.get("/api/v1/auth/github/login")
    assert login.status_code == 200
    assert login.json()["state"]
    # Reaches the webhook's own checks (here: no signing secret configured),
    # not the session-token check.
    webhook = client.post(
        "/api/v1/github/webhooks",
        content=b"{}",
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=0"},
    )
    assert webhook.status_code == 503
