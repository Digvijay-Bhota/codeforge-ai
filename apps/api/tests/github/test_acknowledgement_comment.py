"""The acknowledgement comment shows the command as typed and quotes instructions safely.

The first real acknowledgement showed ``@implement`` instead of ``@codeforge
implement`` and put the multi-line instruction inside a single-line table cell,
which broke the table.  The instructions are untrusted text: they must not
render as Markdown or HTML, must not notify anyone they @mention, and must not
make the App's own comment trigger another command.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.github.command_parser import CodeForgeCommandType, CommandParser
from app.github.models import build_managed_comment_body
from app.github.status_consumer import GitHubStatusConsumer
from app.main import app
from tests.github.test_webhook_api import get_signature

TASK_ID = "7b8cf1b3-64e2-438a-9564-58dd9ed5ea0d"
INTERNAL_PROMPT = "Execute @codeforge implement for issue #2.\nTitle: internal prompt text"


def ack(command: str | None, arguments: str | None, *, pr: int | None = None) -> str:
    task = SimpleNamespace(task_id=TASK_ID, requested_task=INTERNAL_PROMPT)
    link = SimpleNamespace(repository_full_name="owner/sandbox", pull_request_number=pr, issue_number=None if pr else 2)
    return GitHubStatusConsumer._build_acknowledgement_body(task, None, link, command, arguments)  # type: ignore[arg-type]


def fenced_blocks(body: str) -> list[str]:
    """Contents of the fenced code blocks, as CommonMark reads them (fence of 3+ backticks)."""
    return [m.group(2) for m in re.finditer(r"^(`{3,})text\n(.*?)\n\1$", body, re.MULTILINE | re.DOTALL)]


def outside_fences(body: str) -> str:
    return re.sub(r"^(`{3,})text\n.*?\n\1$", "", body, flags=re.MULTILINE | re.DOTALL)


@pytest.mark.parametrize("command", list(CodeForgeCommandType))
def test_every_command_is_shown_as_typed(command: CodeForgeCommandType) -> None:
    body = ack(command.value, "do it")
    assert f"| **Command** | `@codeforge {command.value}` |" in body
    assert f"`@{command.value}" not in body


@pytest.mark.parametrize("command", [None, "", "deploy", "fix`|<b>"])
def test_an_unknown_command_is_not_echoed(command: str | None) -> None:
    body = ack(command, None)
    assert "| **Command** | `@codeforge` |" in body
    if command:
        assert command not in body


def test_multiline_instructions_are_preserved_verbatim_outside_the_table() -> None:
    instructions = (
        'Add a short "Sandbox integration check" section to README.md.\n\n'
        "Keep the proposed change documentation-only.\n"
        "Integration test marker: CF-GH-TEST-PLAN-ONLY-2026-10-21-A"
    )
    body = ack("implement", instructions)
    assert fenced_blocks(body) == [instructions]
    # Every table row is one line and stays a row.
    rows = [line for line in body.splitlines() if line.startswith("|")]
    assert len(rows) == 6 and all(line.endswith("|") for line in rows)
    assert "| **Target** | Issue #2 |" in body
    assert "| **Repository** | `owner/sandbox` |" in body
    assert f"| **Task ID** | `{TASK_ID}` |" in body


def test_markdown_html_and_mentions_stay_inside_the_code_block() -> None:
    hostile = "| col | x |\n**bold** <img src=x onerror=alert(1)> @someone\n``` closing fence\n````\nend"
    body = ack("fix", hostile)
    assert fenced_blocks(body) == [hostile]  # the longer fence cannot be closed from inside
    rest = outside_fences(body)
    for fragment in ("@someone", "<img", "**bold**", "closing fence"):
        assert fragment not in rest


def test_invisible_and_bidirectional_control_characters_are_removed() -> None:
    body = ack("fix", "fix‮ the​ bug\x07\r\nnext line")
    assert fenced_blocks(body) == ["fix the bug\nnext line"]


def test_the_internal_prompt_is_never_shown() -> None:
    for arguments in (None, "do it"):
        body = ack("implement", arguments)
        assert "internal prompt text" not in body


def test_no_instructions_means_no_empty_block() -> None:
    body = ack("review", None)
    assert fenced_blocks(body) == [] and "**Instructions**" not in body


def test_pull_request_target_and_next_step() -> None:
    body = ack("review", None, pr=42)
    assert "| **Target** | Pull Request #42 |" in body
    assert "no code is written until the plan is approved in the CodeForge console" in body


# ── The App's own comment never becomes a command ───────────────────────────


def comment_payload(body: str) -> dict[str, Any]:
    return {
        "action": "created",
        "installation": {"id": 1},
        "repository": {"id": 2, "name": "sandbox", "owner": {"login": "owner"}, "full_name": "owner/sandbox",
                       "default_branch": "main"},
        "issue": {"number": 2, "title": "Docs", "body": ""},
        "comment": {"id": 9, "body": body, "user": {"id": 77, "login": "codeforge-ai-sandbox[bot]", "type": "Bot"}},
    }  # fmt: skip


def test_an_acknowledgement_quoting_a_command_is_ignored_by_the_webhook() -> None:
    # The instructions themselves contain a command line.
    body = build_managed_comment_body(ack("implement", "@codeforge fix\nsomething else"), f"ack-task-{TASK_ID}")
    assert CommandParser.parse(body) is not None  # the quoted line alone would parse

    settings.github_webhook_secret = "test_secret"  # noqa: S105 - test value
    raw = json.dumps(comment_payload(body)).encode()
    with (
        patch("app.db.repositories.webhook_repository.WebhookRepository.record_delivery", new=AsyncMock()) as record,
        patch("app.db.repositories.outbox_repository.OutboxRepository.create_event", new=AsyncMock()) as create,
    ):
        response = TestClient(app).post(
            "/api/v1/github/webhooks",
            content=raw,
            headers={"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "del-ack-1",
                     "X-Hub-Signature-256": get_signature(settings.github_webhook_secret, raw)},
        )  # fmt: skip
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "CodeForge managed comment"}
    record.assert_not_called()
    create.assert_not_called()
