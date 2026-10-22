# Agent tracing (CODEFORGE_OPENAI_TRACING)

The planner and coding agent run on the OpenAI Agents SDK (`openai-agents`,
pinned in `apps/api/requirements.txt`). Separately from the model requests, the
SDK can record a **trace** of each run and export it to OpenAI's tracing
service (`POST https://api.openai.com/v1/traces/ingest`, shown in the OpenAI
platform's Traces view), using the same API key. With the SDK's defaults a
trace includes the model input and output: the task text, the repository
context CodeForge sends, the plan, and tool inputs and outputs.

CodeForge turns this off unless you choose otherwise.

## The setting

`CODEFORGE_OPENAI_TRACING` (worker), one of:

| Value | Effect |
|---|---|
| `disabled` (default) | No traces are created, so none are exported. |
| `metadata` | Traces are exported with span types and timings, the workflow and agent names, tool names, the output type, OpenAI response IDs and token usage, but **without model or tool inputs and outputs** (the SDK's `trace_include_sensitive_data=False`). |
| `full` | The SDK's default behaviour: traces include model inputs and outputs and tool inputs and outputs. |

Any other value stops the worker at startup. The worker logs its mode in its
startup line (`agent_tracing=disabled`).

How it is applied (`apps/api/app/agents/tracing.py`):

- At worker startup, the SDK's global switch (`set_tracing_disabled`) is set
  from the mode. It takes precedence over the SDK's own
  `OPENAI_AGENTS_DISABLE_TRACING` variable, so set `CODEFORGE_OPENAI_TRACING`,
  not that one.
- Every `Runner.run` call also receives a `RunConfig` with `tracing_disabled`
  and `trace_include_sensitive_data` set from the mode, so the policy holds for
  each run independently of the global switch.

The Compose stack does not pass `CODEFORGE_OPENAI_TRACING` to the worker, so
it always runs with tracing disabled. To try `metadata` or `full` locally, add
the variable to the worker's `environment` in a Compose override file.

## What still leaves the system

Tracing only controls the **trace export**. Every planning and coding run
still sends its prompt (the task text and repository context) to the model API
configured for the SDK, because that is how the model produces a plan. Choose
the OpenAI organisation and project for `OPENAI_API_KEY` with that in mind.
How OpenAI retains or uses API requests and traces is governed by OpenAI's
terms and your organisation's data controls, not by CodeForge.

Local observability is unaffected: CodeForge's own task events, stage timings
and logs are written to its database and logs whatever the tracing mode.

## Tests

`apps/api/tests/test_agent_tracing.py` runs a real `Runner.run` against the
SDK's Responses model, served from memory (no network), and records what reaches
the trace processors: nothing for `disabled`; spans without the task text for
`metadata`; the task text for `full` (the control). The test suite also sets
`OPENAI_AGENTS_DISABLE_TRACING=1` (`apps/api/tests/conftest.py`), so no test
can export a trace.
