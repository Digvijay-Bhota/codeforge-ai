"""Health endpoint tests.

These tests use FastAPI's TestClient so no real server is required.
Both the root-level endpoint and the versioned endpoint are covered.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Return a TestClient bound to a fresh application instance."""
    return TestClient(create_app())


def test_root_health_status_code(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_root_health_response_body(client: TestClient) -> None:
    response = client.get("/health")
    assert response.json() == {"status": "ok"}


def test_v1_health_status_code(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200


def test_v1_health_response_body(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.json() == {"status": "ok"}
