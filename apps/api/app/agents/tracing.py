"""Applies CODEFORGE_OPENAI_TRACING to the OpenAI Agents SDK.

Two layers, so the policy holds whichever entry point runs an agent:

* ``configure_agent_tracing`` sets the SDK's process-wide switch (it takes
  precedence over the SDK's own OPENAI_AGENTS_DISABLE_TRACING variable).
* ``agent_run_config`` is passed to every ``Runner.run`` and sets the same
  policy for that run, including whether model and tool data are recorded.

Model requests themselves are unaffected: tracing only controls the separate
trace export.
"""

from __future__ import annotations

from agents import RunConfig, set_tracing_disabled
from app.config import AgentTracingMode, settings


def _mode(mode: AgentTracingMode | None) -> AgentTracingMode:
    return AgentTracingMode(mode if mode is not None else settings.codeforge_openai_tracing)


def configure_agent_tracing(mode: AgentTracingMode | None = None) -> AgentTracingMode:
    """Set the SDK's global tracing switch from *mode* (default: the setting).  Returns the mode."""
    resolved = _mode(mode)
    set_tracing_disabled(resolved == AgentTracingMode.DISABLED)
    return resolved


def agent_run_config(mode: AgentTracingMode | None = None) -> RunConfig:
    """The RunConfig for one agent run under *mode* (default: the setting)."""
    resolved = _mode(mode)
    return RunConfig(
        tracing_disabled=resolved == AgentTracingMode.DISABLED,
        trace_include_sensitive_data=resolved == AgentTracingMode.FULL,
    )
