"""Phase 10B.5 Stage 1A — TestExecutor boundary, execution modes and repair gate.

Repository-controlled code (pytest, conftest.py, plugins) must only ever run
through one TestExecutor, chosen by an explicit execution mode.  Host
execution is a development convenience, never a production fallback, and
automatic repair attempts require an executor that reports
IsolationLevel.SANDBOX.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.tool_context import ToolContext
from app.agents.coding_agent import make_coding_agent
from app.config import ExecutionMode, Settings, settings
from app.execution.host_executor import HostTestExecutor
from app.execution.test_executor import (
    ExecutionUnavailableError,
    IsolationLevel,
    TestExecutor,
    UnavailableTestExecutor,
    get_test_executor,
    run_tests_in_snapshot,
)
from app.orchestration.models import CodingResult, WorkflowStatus
from app.orchestration.orchestrator import Orchestrator
from app.schemas.plan import ImplementationPlan, PlanAction, PlanStep, RiskLevel
from app.schemas.task import TestResult
from app.workspace.manager import WorkspaceManager
from app.workspace.snapshot import WorkspaceSnapshot

APP_DIR = Path(__file__).resolve().parents[2] / "app"

PRODUCTION_REQUIRED = {
    "openai_api_key": "sk-test",
    "database_url": "postgresql+asyncpg://u:p@db:5432/prod",
    "jwt_secret_key": "a-production-grade-secret-key-of-sufficient-length",
}


class FakeExecutor:
    """Records calls; reports whatever isolation level the test needs."""

    __test__ = False

    def __init__(self, isolation: IsolationLevel, results: list[TestResult] | None = None) -> None:
        self._isolation = isolation
        self.results = list(results or [_result(True)])
        self.calls: list[WorkspaceSnapshot] = []

    @property
    def isolation(self) -> IsolationLevel:
        return self._isolation

    async def run(self, snapshot: WorkspaceSnapshot) -> TestResult:
        self.calls.append(snapshot)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def _result(passed: bool) -> TestResult:
    return TestResult(
        passed=passed, exit_code=0 if passed else 1, stdout="", stderr="", duration_seconds=0.1
    )


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _plan() -> ImplementationPlan:
    return ImplementationPlan(
        goal="Fix it",
        validation_strategy="Run pytest",
        risk_level=RiskLevel.low,
        summary="summary",
        steps=[
            PlanStep(
                step_number=1, action=PlanAction.modify, description="Fix it", rationale="Because"
            )
        ],
    )


def _coding(changed: bool = True) -> CodingResult:
    return CodingResult(
        changes_made=changed,
        changed_paths=["a.py"] if changed else [],
        diff="--- a/a.py\n+++ b/a.py\n",
        message="done",
        deviations="",
    )


# ── Execution mode configuration (fail closed) ─────────────────────────────────


def test_default_execution_mode_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEFORGE_TEST_EXECUTION_MODE", raising=False)
    assert _settings().codeforge_test_execution_mode is ExecutionMode.DISABLED


def test_default_repair_budget_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEFORGE_MAX_REPAIR_ATTEMPTS", raising=False)
    assert _settings().codeforge_max_repair_attempts == 0


@pytest.mark.parametrize("env", ["development", "test"])
def test_host_mode_allowed_only_in_development_and_test(env: str) -> None:
    loaded = _settings(app_env=env, codeforge_test_execution_mode="host_unsafe")
    assert loaded.codeforge_test_execution_mode is ExecutionMode.HOST_UNSAFE


@pytest.mark.parametrize("env", ["production", "staging", "prod", "", "Development"])
def test_host_mode_rejected_outside_development_and_test(env: str) -> None:
    with pytest.raises(ValueError, match="host_unsafe"):
        _settings(app_env=env, codeforge_test_execution_mode="host_unsafe", **PRODUCTION_REQUIRED)


@pytest.mark.parametrize("mode", ["disabled", "sandbox"])
def test_production_accepts_only_disabled_or_sandbox(mode: str) -> None:
    loaded = _settings(app_env="production", codeforge_test_execution_mode=mode, **PRODUCTION_REQUIRED)
    assert loaded.codeforge_test_execution_mode.value == mode


def test_unknown_execution_mode_rejected() -> None:
    with pytest.raises(ValueError):
        _settings(codeforge_test_execution_mode="docker")


# A deployment that forgets APP_ENV gets the "development" default.  That
# default must not be enough to run repository code on the host.


def test_host_mode_rejected_when_app_env_is_only_defaulted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(ValueError, match="APP_ENV"):
        _settings(codeforge_test_execution_mode="host_unsafe")


def test_host_mode_rejected_via_env_vars_when_app_env_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("CODEFORGE_TEST_EXECUTION_MODE", "host_unsafe")
    with pytest.raises(ValueError, match="APP_ENV"):
        _settings()


@pytest.mark.parametrize("env", ["development", "test"])
def test_host_mode_accepted_with_explicit_app_env_variable(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("CODEFORGE_TEST_EXECUTION_MODE", "host_unsafe")
    assert _settings().codeforge_test_execution_mode is ExecutionMode.HOST_UNSAFE


def test_host_mode_accepted_with_app_env_in_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("CODEFORGE_TEST_EXECUTION_MODE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=development\nCODEFORGE_TEST_EXECUTION_MODE=host_unsafe\n")
    loaded = Settings(_env_file=str(env_file))
    assert loaded.codeforge_test_execution_mode is ExecutionMode.HOST_UNSAFE


def test_defaulted_app_env_still_allows_disabled_and_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    assert _settings().codeforge_test_execution_mode is ExecutionMode.DISABLED
    assert _settings(codeforge_test_execution_mode="sandbox").codeforge_test_execution_mode is (
        ExecutionMode.SANDBOX
    )


@pytest.mark.asyncio
async def test_runtime_checks_refuse_host_execution_with_defaulted_app_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bypass the validator (mutated settings) and prove factory and executor refuse too."""
    monkeypatch.delenv("APP_ENV", raising=False)
    defaulted = _settings()
    defaulted.codeforge_test_execution_mode = ExecutionMode.HOST_UNSAFE
    assert defaulted.app_env == "development" and "app_env" not in defaulted.model_fields_set
    monkeypatch.setattr("app.execution.test_executor.settings", defaulted)
    monkeypatch.setattr("app.execution.host_executor.settings", defaulted)
    marker = tmp_path / "ran"
    (tmp_path / "conftest.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (tmp_path / "test_a.py").write_text("def test_ok(): assert True\n")

    assert not isinstance(get_test_executor(), HostTestExecutor)
    with pytest.raises(ExecutionUnavailableError, match="APP_ENV"):
        await run_tests_in_snapshot(HostTestExecutor(), tmp_path)
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["disabled", "host_unsafe"])
def test_nonzero_repairs_rejected_without_sandbox_mode(mode: str) -> None:
    # Explicit APP_ENV so host_unsafe itself is valid and only the repair gate applies.
    with pytest.raises(ValueError, match="sandbox"):
        _settings(
            app_env="development",
            codeforge_test_execution_mode=mode,
            codeforge_max_repair_attempts=1,
        )


def test_nonzero_repairs_accepted_with_sandbox_mode() -> None:
    loaded = _settings(codeforge_test_execution_mode="sandbox", codeforge_max_repair_attempts=2)
    assert loaded.codeforge_max_repair_attempts == 2


# ── Executor factory ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_mode_executor_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.DISABLED)
    executor = get_test_executor()
    assert isinstance(executor, UnavailableTestExecutor)
    assert executor.isolation is IsolationLevel.NONE
    with pytest.raises(ExecutionUnavailableError, match="disabled"):
        await executor.run(tmp_path)


@pytest.mark.asyncio
async def test_sandbox_mode_fails_closed_until_a_sandbox_runtime_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.SANDBOX)
    executor = get_test_executor()
    # It must not claim isolation it cannot provide, and must never fall back to the host.
    assert executor.isolation is IsolationLevel.NONE
    assert not isinstance(executor, HostTestExecutor)
    with pytest.raises(ExecutionUnavailableError, match="sandbox"):
        await executor.run(tmp_path)


def test_host_mode_returns_host_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.HOST_UNSAFE)
    executor = get_test_executor()
    assert isinstance(executor, HostTestExecutor)
    assert executor.isolation is IsolationLevel.NONE


def test_factory_refuses_host_mode_in_production_even_if_settings_were_mutated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Validators run at load; this proves the runtime check does not rely on them.
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "codeforge_test_execution_mode", ExecutionMode.HOST_UNSAFE)
    executor = get_test_executor()
    assert not isinstance(executor, HostTestExecutor)


def test_executors_satisfy_protocol() -> None:
    assert isinstance(HostTestExecutor(), TestExecutor)
    assert isinstance(UnavailableTestExecutor("x"), TestExecutor)


# ── One executor for every call site ───────────────────────────────────────────


def _patched_stages(coding: AsyncMock, testing: AsyncMock) -> Any:
    return (
        patch("app.orchestration.orchestrator.run_analysis_stage", return_value=MagicMock()),
        patch(
            "app.orchestration.orchestrator.run_planning_stage",
            new_callable=AsyncMock,
            return_value=_plan(),
        ),
        patch("app.orchestration.orchestrator.run_coding_stage", new=coding),
        patch("app.orchestration.orchestrator.run_testing_stage", new=testing),
    )


@pytest.mark.asyncio
async def test_testing_stage_coding_tool_and_repairs_share_one_executor(tmp_path: Path) -> None:
    executor = FakeExecutor(IsolationLevel.SANDBOX)
    coding = AsyncMock(return_value=_coding())
    testing = AsyncMock(side_effect=[_result(False), _result(True)])
    workspace = WorkspaceManager(tmp_path)
    diffs = iter(f"diff-{i}" for i in range(100))
    p1, p2, p3, p4 = _patched_stages(coding, testing)
    with p1, p2, p3, p4, patch.object(workspace, "generate_diff", side_effect=lambda: next(diffs)):
        final = await Orchestrator(executor=executor, max_repair_attempts=1).run(workspace, "Fix it")

    assert final.workflow_status == WorkflowStatus.COMPLETED
    assert final.repair_attempts_used == 1
    # initial coding + one repair, both handed the same executor
    assert coding.await_count == 2
    assert all(call.kwargs["executor"] is executor for call in coding.await_args_list)
    # both test runs used it too
    assert testing.await_count == 2
    assert all(call.args[1] is executor for call in testing.await_args_list)


@pytest.mark.asyncio
async def test_orchestrator_resolves_configured_executor_at_run_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = FakeExecutor(IsolationLevel.NONE)
    monkeypatch.setattr("app.orchestration.orchestrator.get_test_executor", lambda: configured)
    coding = AsyncMock(return_value=_coding())
    testing = AsyncMock(return_value=_result(True))
    p1, p2, p3, p4 = _patched_stages(coding, testing)
    with p1, p2, p3, p4:
        await Orchestrator().run(WorkspaceManager(tmp_path), "Fix it")
    assert coding.await_args.kwargs["executor"] is configured
    assert testing.await_args.args[1] is configured


def _run_tests_tool(workspace: WorkspaceManager, executor: Any) -> Any:
    agent = make_coding_agent(workspace=workspace, executor=executor, model="gpt-4o")
    return next(t for t in agent.tools if t.name == "run_tests")


async def _invoke(tool: Any) -> str:
    ctx = ToolContext(context=None, tool_name="run_tests", tool_call_id="1", tool_arguments="{}")
    return str(await tool.on_invoke_tool(ctx, "{}"))


@pytest.mark.asyncio
async def test_run_tests_tool_awaits_the_executor(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("def test_a(): pass\n")
    workspace = WorkspaceManager(tmp_path)
    executor = FakeExecutor(IsolationLevel.NONE, [_result(False)])
    output = await _invoke(_run_tests_tool(workspace, executor))
    # The executor gets a snapshot of the workspace, never the workspace itself.
    [snapshot] = executor.calls
    assert isinstance(snapshot, WorkspaceSnapshot)
    assert [e.path for e in snapshot.manifest.entries] == ["test_a.py"]
    assert "PASSED: False" in output


@pytest.mark.asyncio
async def test_run_tests_tool_reports_unavailable_execution(tmp_path: Path) -> None:
    output = await _invoke(
        _run_tests_tool(WorkspaceManager(tmp_path), UnavailableTestExecutor("Test execution is disabled."))
    )
    assert "unavailable" in output.lower()
    assert "disabled" in output


@pytest.mark.asyncio
async def test_disabled_execution_fails_the_task_closed(tmp_path: Path) -> None:
    coding = AsyncMock(return_value=_coding())
    p1, p2, p3 = _patched_stages(coding, AsyncMock())[:3]
    with p1, p2, p3:
        final = await Orchestrator(
            executor=UnavailableTestExecutor("Test execution is disabled.")
        ).run(WorkspaceManager(tmp_path), "Fix it")
    assert final.workflow_status == WorkflowStatus.FAILED
    assert "disabled" in (final.failure_reason or "")
    assert final.test_result is None


# ── Repair attempts require sandbox isolation ──────────────────────────────────


@pytest.mark.parametrize("attempts", [1, 5])
def test_constructor_rejects_repairs_with_unisolated_executor(attempts: int) -> None:
    with pytest.raises(ValueError, match="SANDBOX"):
        Orchestrator(executor=FakeExecutor(IsolationLevel.NONE), max_repair_attempts=attempts)
    with pytest.raises(ValueError, match="SANDBOX"):
        Orchestrator(executor=HostTestExecutor(), max_repair_attempts=attempts)


def test_constructor_allows_zero_repairs_with_unisolated_executor() -> None:
    Orchestrator(executor=HostTestExecutor(), max_repair_attempts=0)


@pytest.mark.asyncio
async def test_configured_repairs_with_unisolated_executor_fail_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bypass the Settings validator to prove the orchestrator enforces it too.
    monkeypatch.setattr(settings, "codeforge_max_repair_attempts", 2)
    analysis = MagicMock()
    coding = AsyncMock(return_value=_coding())
    testing = AsyncMock(return_value=_result(False))
    with (
        patch("app.orchestration.orchestrator.run_analysis_stage", new=analysis),
        patch("app.orchestration.orchestrator.run_coding_stage", new=coding),
        patch("app.orchestration.orchestrator.run_testing_stage", new=testing),
    ):
        final = await Orchestrator(executor=FakeExecutor(IsolationLevel.NONE)).run(
            WorkspaceManager(tmp_path), "Fix it"
        )
    assert final.workflow_status == WorkflowStatus.FAILED
    assert "SANDBOX" in (final.failure_reason or "")
    analysis.assert_not_called()
    coding.assert_not_awaited()
    testing.assert_not_awaited()


@pytest.mark.asyncio
async def test_unisolated_executor_never_triggers_repair_after_failed_tests(tmp_path: Path) -> None:
    coding = AsyncMock(return_value=_coding())
    testing = AsyncMock(return_value=_result(False))
    p1, p2, p3, p4 = _patched_stages(coding, testing)
    with p1, p2, p3, p4:
        final = await Orchestrator(executor=FakeExecutor(IsolationLevel.NONE)).run(
            WorkspaceManager(tmp_path), "Fix it"
        )
    assert final.workflow_status == WorkflowStatus.FAILED
    assert final.repair_attempts_used == 0
    assert coding.await_count == 1
    assert testing.await_count == 1


# ── Static boundary: only approved modules may spawn processes ─────────────────

# Modules allowed to start subprocesses, and why.
SUBPROCESS_ALLOWLIST = {
    "execution/host_executor.py",  # development-only host test execution
    "github/git.py",  # hardened git (SafeGitWrapper / read-only queries)
}

_SPAWN_ATTRS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "fork"),
    ("os", "forkpty"),
    ("os", "posix_spawn"),
    ("os", "posix_spawnp"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
}


def _spawning_modules() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hits += [a.name for a in node.names if a.name.split(".")[0] in {"subprocess", "pty"}]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in {"subprocess", "pty"}:
                    hits.append(node.module)
                elif node.module in {"os", "asyncio"}:
                    hits += [
                        f"{node.module}.{a.name}"
                        for a in node.names
                        if (node.module, a.name) in _SPAWN_ATTRS
                        or a.name.startswith(("exec", "spawn"))
                    ]
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                pair = (node.value.id, node.attr)
                if pair in _SPAWN_ATTRS or (
                    node.value.id == "os" and node.attr.startswith(("exec", "spawn"))
                ):
                    hits.append(f"{pair[0]}.{pair[1]}")
        if hits:
            found[rel] = hits
    return found


def test_only_allowlisted_modules_can_spawn_processes() -> None:
    offenders = {m: h for m, h in _spawning_modules().items() if m not in SUBPROCESS_ALLOWLIST}
    assert offenders == {}


def test_allowlisted_modules_still_exist() -> None:
    # Keeps the allowlist honest if files move.
    assert set(_spawning_modules()) == SUBPROCESS_ALLOWLIST


def test_legacy_test_runner_is_gone() -> None:
    assert not (APP_DIR / "workspace" / "runner.py").exists()
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "TestRunner" not in text, path
        assert "workspace.runner" not in text, path
