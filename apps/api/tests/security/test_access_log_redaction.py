"""OAuth authorization codes and state values never reach the access log.

GitHub redirects the browser to the callback with ``?code=...&state=...``, and
the console forwards both to ``/api/v1/auth/github/callback``.  Uvicorn's access
log writes the full request target, so both values were logged in the first
real sign-in.  The API now redacts sensitive query values from its access log;
the console's nginx logs paths without query strings (apps/web/nginx.conf,
checked by apps/web/src/test/nginx.test.ts).

The values below are synthetic.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from uvicorn.config import LOGGING_CONFIG

from app.main import create_app
from app.observability.redaction import QueryRedactionFilter, install_access_log_redaction

CODE = "synthetic-oauth-code-6f1d2c"
STATE = "synthetic-oauth-state-Zq9XbW"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def access_log() -> Iterator[_Capture]:
    """Captures uvicorn.access, starting without any redaction filter (logger state is process-wide)."""
    capture = _Capture()
    logger = logging.getLogger("uvicorn.access")
    saved = list(logger.filters)
    logger.filters = [f for f in saved if not isinstance(f, QueryRedactionFilter)]
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
        logger.filters = saved


def _access_record(target: str) -> logging.LogRecord:
    # Uvicorn's own call: access_logger.info('%s - "%s %s HTTP/%s" %d', client, method, target, version, status)
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "GET", target, "1.1", 200), None,
    )  # fmt: skip


def test_filter_redacts_code_and_state_and_keeps_the_rest() -> None:
    record = _access_record(f"/api/v1/auth/github/callback?code={CODE}&state={STATE}&iss=github")
    assert QueryRedactionFilter().filter(record) is True
    line = record.getMessage()
    assert CODE not in line and STATE not in line
    assert line == '127.0.0.1:50000 - "GET /api/v1/auth/github/callback?code=***&state=***&iss=github HTTP/1.1" 200'


def test_filter_leaves_other_requests_unchanged() -> None:
    record = _access_record("/api/v1/tasks?limit=20&offset=0")
    QueryRedactionFilter().filter(record)
    assert record.getMessage() == '127.0.0.1:50000 - "GET /api/v1/tasks?limit=20&offset=0 HTTP/1.1" 200'


def test_installation_is_idempotent_and_survives_uvicorn_logging_configuration() -> None:
    create_app()
    install_access_log_redaction()
    # Uvicorn applies its dictConfig before importing the app; applying it again
    # afterwards (as reloads and embedding servers do) must not drop the filter.
    logging.config.dictConfig(LOGGING_CONFIG)
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, QueryRedactionFilter)]
    assert len(filters) == 1


@pytest.mark.asyncio
async def test_a_real_uvicorn_access_log_never_contains_the_oauth_values(access_log: _Capture) -> None:
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_config=None))
    serve = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            # Any route: the access log line is written for every request.
            response = await client.get("/health", params={"code": CODE, "state": STATE, "verbose": "1"})
        assert response.status_code == 200
    finally:
        server.should_exit = True
        await serve

    lines = [line for line in access_log.lines if "/health" in line]
    assert lines, access_log.lines
    for line in lines:
        assert CODE not in line and STATE not in line
        assert '"GET /health?code=***&state=***&verbose=1 HTTP/1.1" 200' in line
