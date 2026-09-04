"""Shared pytest fixtures for the CodeForge API test suite."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# Location of the static fixture repositories used in tests.
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def client() -> TestClient:
    """A FastAPI TestClient for the full application."""
    return TestClient(create_app())


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    """Return a *copy* of the fixture sample repository in a temp directory.

    Each test gets its own isolated copy so mutations do not bleed across tests.
    """
    src = FIXTURES_DIR / "sample_repo"
    dest = tmp_path / "sample_repo"
    shutil.copytree(src, dest)
    return dest
