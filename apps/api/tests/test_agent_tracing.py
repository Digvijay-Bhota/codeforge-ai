"""CODEFORGE_OPENAI_TRACING decides what the Agents SDK records for export.

With tracing on, the SDK sends traces to OpenAI's trace ingestion endpoint.
The first real run did so: the worker's log showed ``POST
https://api.openai.com/v1/traces/ingest`` next to the model request.  These
tests run a real ``Runner.run`` with the SDK's real Responses model, served by
an in-memory transport (no network), and record what reaches the trace
processors in place of the exporter.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from agents import Agent, OpenAIResponsesModel, Runner
from agents.tracing import get_trace_provider, set_trace_processors
from agents.tracing.processor_interface import TracingProcessor
from app.agents.tracing import agent_run_config, configure_agent_tracing
from app.config import AgentTracingMode, Settings
from app.services.configuration_status import log_configuration_status

TASK_TEXT = "Add a Sandbox integration check section to README.md (marker 7f3a)"
RESPONSE = {
    "id": "resp_test", "object": "response", "created_at": 0, "status": "completed", "model": "gpt-4o",
    "output": [{"type": "message", "id": "msg_test", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "Plan: edit README.md", "annotations": []}]}],
    "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
              "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
}  # fmt: skip


class Recorder(TracingProcessor):
    def __init__(self) -> None:
        self.traces: list[Any] = []
        self.spans: list[Any] = []

    def on_trace_start(self, trace: Any) -> None:
        self.traces.append(trace)

    def on_trace_end(self, trace: Any) -> None:
        pass

    def on_span_start(self, span: Any) -> None:
        pass

    def on_span_end(self, span: Any) -> None:
        self.spans.append(span)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass

    def recorded_text(self) -> str:
        """Everything a span carries, as the exporter could see it."""
        parts = []
        for span in self.spans:
            data = span.span_data
            parts.append(json.dumps(span.export(), default=str))
            parts.append(repr(getattr(data, "input", None)))
            parts.append(repr(getattr(data, "response", None)))
        return "\n".join(parts)


@pytest.fixture
def recorder() -> Iterator[Recorder]:
    provider = get_trace_provider()
    saved = list(provider._multi_processor._processors)  # type: ignore[attr-defined]
    rec = Recorder()
    set_trace_processors([rec])  # replaces the exporter for the test
    try:
        yield rec
    finally:
        set_trace_processors(saved)
        configure_agent_tracing(AgentTracingMode.DISABLED)


@pytest.fixture
def requests_seen() -> list[str]:
    return []


@pytest.fixture
def agent(requests_seen: list[str]) -> Agent[Any]:
    def handle(request: httpx2.Request) -> httpx2.Response:
        requests_seen.append(str(request.url))
        return httpx2.Response(200, json=RESPONSE)

    client = AsyncOpenAI(api_key="sk-test-not-a-real-key", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)))
    return Agent(name="planner", instructions="Plan the task.", model=OpenAIResponsesModel("gpt-4o", client))


async def run(agent: Agent[Any], mode: AgentTracingMode) -> str:
    configure_agent_tracing(mode)
    result = await Runner.run(agent, TASK_TEXT, run_config=agent_run_config(mode))
    return str(result.final_output)


@pytest.mark.asyncio
async def test_disabled_records_no_trace_and_still_runs_the_model(
    agent: Agent[Any], recorder: Recorder, requests_seen: list[str]
) -> None:
    assert await run(agent, AgentTracingMode.DISABLED) == "Plan: edit README.md"
    assert recorder.traces == [] and recorder.spans == []
    assert requests_seen == ["https://api.openai.com/v1/responses"]  # the model call only


@pytest.mark.asyncio
async def test_metadata_records_spans_without_the_task_text(agent: Agent[Any], recorder: Recorder) -> None:
    await run(agent, AgentTracingMode.METADATA)
    assert recorder.traces and recorder.spans
    assert "marker 7f3a" not in recorder.recorded_text()


@pytest.mark.asyncio
async def test_full_records_the_task_text(agent: Agent[Any], recorder: Recorder) -> None:
    # The control for the two tests above: the recorder does see model input when allowed.
    await run(agent, AgentTracingMode.FULL)
    assert "marker 7f3a" in recorder.recorded_text()


@pytest.mark.asyncio
async def test_the_run_config_alone_disables_tracing(agent: Agent[Any], recorder: Recorder) -> None:
    # Even if the global switch were left on, each run carries the policy.
    configure_agent_tracing(AgentTracingMode.FULL)
    await Runner.run(agent, TASK_TEXT, run_config=agent_run_config(AgentTracingMode.DISABLED))
    assert recorder.traces == [] and recorder.spans == []


def test_tracing_is_disabled_by_default() -> None:
    assert Settings().codeforge_openai_tracing == AgentTracingMode.DISABLED
    config = agent_run_config(Settings().codeforge_openai_tracing)
    assert config.tracing_disabled is True and config.trace_include_sensitive_data is False


def test_an_unknown_mode_is_rejected_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEFORGE_OPENAI_TRACING", "everything")
    with pytest.raises(ValidationError):
        Settings()


def test_the_worker_reports_its_tracing_mode(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="app.services.configuration_status"):
        log_configuration_status(Settings(), "worker", include_agent=True)
    assert "agent_tracing=disabled" in caplog.text


@pytest.mark.asyncio
async def test_planning_runs_with_the_configured_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.orchestration.stages import run_planning_stage
    from tests.orchestration.test_orchestration import _make_context, _make_plan

    seen: list[Any] = []

    async def fake_run(agent: Any, prompt: str, **kwargs: Any) -> Any:
        seen.append(kwargs.get("run_config"))
        return MagicMock(final_output=_make_plan())

    with patch("app.orchestration.stages.make_planner_agent"), patch(
        "app.orchestration.stages.Runner.run", new=AsyncMock(side_effect=fake_run)
    ):
        await run_planning_stage("Add a README section", _make_context(), "gpt-4o")
    assert seen and seen[0].tracing_disabled is True
