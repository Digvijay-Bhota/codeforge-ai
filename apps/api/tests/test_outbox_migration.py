"""The outbox delivery-state migration (e1a2b3c4d5f6) on rows written under the old scheme.

Runs the real alembic against a scratch database on the test server: up to
the previous head, rows as the old dispatcher left them, then upgrade and
downgrade.  The scratch database is dropped afterwards.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - runs alembic from this repository
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import TEST_DB_URL

API_DIR = Path(__file__).resolve().parents[1]
PREVIOUS = "d7e8f9a0b1c2"


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "APP_ENV": "test"}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API_DIR, env=env, check=True,
                   capture_output=True, text=True, timeout=120)  # fmt: skip  # nosec B603


@pytest.fixture
def scratch_url() -> Iterator[str]:
    name = f"codeforge_migration_{uuid.uuid4().hex[:8]}"
    base, _, _ = TEST_DB_URL.rpartition("/")

    async def admin(sql: str) -> None:
        conn = await asyncpg.connect(_dsn(TEST_DB_URL))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    import asyncio

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    try:
        yield f"{base}/{name}"
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_old_terminal_events_become_failed_events_and_back(scratch_url: str) -> None:
    import asyncio

    _alembic(scratch_url, "upgrade", PREVIOUS)

    async def old_rows() -> None:
        conn = await asyncpg.connect(_dsn(scratch_url))
        try:
            await conn.execute(
                """
                INSERT INTO outbox_events (id, event_type, aggregate_id, payload, created_at, published_at) VALUES
                 (1, 'GITHUB_STATUS_UPDATE', 't-1', '{"task_id": "t-1"}', now() - interval '2 hours', now() - interval '2 hours'),
                 (2, 'GITHUB_STATUS_UPDATE', 't-2', '{"task_id": "t-2", "attempts": 3, "last_error": "GitHubUpstreamError: 502", "terminal_failure": true, "max_attempts_exceeded": true}', now() - interval '1 hour', now() - interval '50 minutes'),
                 (3, 'GITHUB_COMMAND_INGESTED', 'd-3', '{"error": "actor cannot be resolved", "error_type": "UnresolvableIdentityError", "terminal_failure": true}', now() - interval '1 hour', now() - interval '59 minutes'),
                 (4, 'JOB_CREATED', '9', '{"job_id": 9, "attempts": 1, "last_error": "redis down"}', now(), NULL)
                """
            )
        finally:
            await conn.close()

    asyncio.run(old_rows())
    _alembic(scratch_url, "upgrade", "head")

    async def read() -> dict[int, asyncpg.Record]:
        conn = await asyncpg.connect(_dsn(scratch_url))
        try:
            rows = await conn.fetch("SELECT * FROM outbox_events ORDER BY id")
            return {r["id"]: r for r in rows}
        finally:
            await conn.close()

    rows = asyncio.run(read())
    delivered, exhausted, refused, pending = rows[1], rows[2], rows[3], rows[4]
    assert delivered["published_at"] is not None and delivered["failed_at"] is None and delivered["attempts"] == 0
    assert exhausted["published_at"] is None and exhausted["failed_at"] is not None
    assert exhausted["attempts"] == 3 and exhausted["last_error_category"] == "transient"
    assert exhausted["last_error"] == "GitHubUpstreamError: 502"
    assert refused["failed_at"] is not None and refused["last_error_category"] == "permanent"
    assert refused["last_error"] == "actor cannot be resolved" and refused["attempts"] == 1
    # Still pending: its old attempt count is history, not counted against the new budget.
    assert pending["published_at"] is None and pending["failed_at"] is None and pending["attempts"] == 0
    assert '"terminal_failure": true' in exhausted["payload"]  # payloads are left as they were

    _alembic(scratch_url, "downgrade", PREVIOUS)
    rows = asyncio.run(read())
    assert rows[2]["published_at"] is not None and rows[3]["published_at"] is not None  # the old terminal state
    assert rows[4]["published_at"] is None and "failed_at" not in rows[4]
