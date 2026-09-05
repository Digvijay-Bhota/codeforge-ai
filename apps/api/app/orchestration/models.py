"""Phase 5: Typed workflow state and artifact models.

All models are Pydantic v2 models.  Existing shared models are imported
and reused; nothing is duplicated here.

Workflow state machine:

    PENDING → ANALYZING → PLANNING → CODING → TESTING → COMPLETED
                                                        ↘ FAILED
    Any non-COMPLETED terminal state can also transition → FAILED.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# ── Reused from existing phases ──────────────────────────────────────────────
# (imported only to re-export for convenience in orchestrator.py)
from app.repository.models import RepositoryContext  # Phase 2
from app.schemas.plan import ImplementationPlan  # Phase 3
from app.schemas.task import TestResult  # Phase 1


class WorkflowStatus(str, Enum):
    """Ordered lifecycle states for the multi-agent orchestration workflow."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    CODING = "coding"
    TESTING = "testing"
    COMPLETED = "completed"
    FAILED = "failed"


# Valid forward transitions — the orchestrator enforces these.
_VALID_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.PENDING: frozenset({WorkflowStatus.ANALYZING, WorkflowStatus.FAILED}),
    WorkflowStatus.ANALYZING: frozenset({WorkflowStatus.PLANNING, WorkflowStatus.FAILED}),
    WorkflowStatus.PLANNING: frozenset({WorkflowStatus.CODING, WorkflowStatus.FAILED}),
    WorkflowStatus.CODING: frozenset({WorkflowStatus.TESTING, WorkflowStatus.FAILED}),
    WorkflowStatus.TESTING: frozenset({WorkflowStatus.COMPLETED, WorkflowStatus.FAILED}),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
}


def is_valid_transition(from_status: WorkflowStatus, to_status: WorkflowStatus) -> bool:
    """Return True iff *from_status* → *to_status* is an allowed transition."""
    return to_status in _VALID_TRANSITIONS.get(from_status, frozenset())


# ── Per-stage typed artifacts ────────────────────────────────────────────────


class CodingResult(BaseModel):
    """Typed artifact produced by the Coding Agent stage."""

    changes_made: bool = Field(
        description="Whether the Coding Agent made any file changes."
    )
    changed_paths: list[str] = Field(
        default_factory=list,
        description="Workspace-relative paths that were created, modified, or deleted.",
    )
    diff: str = Field(
        default="",
        description="Unified diff of all changes made in this session.",
    )
    message: str = Field(
        description="Summary produced by the Coding Agent."
    )
    deviations: str = Field(
        default="",
        description="Any reported deviations from the Implementation Plan.",
    )


class FinalTaskResult(BaseModel):
    """Top-level result returned to the API layer after the complete workflow."""

    model_config = {"arbitrary_types_allowed": True}  # type: ignore[assignment]

    task_id: str
    workflow_status: WorkflowStatus
    task_description: str
    implementation_plan: ImplementationPlan | None = Field(
        default=None,
        description=(
            "The complete validated ImplementationPlan produced by the Planner. "
            "Populated whenever the planner stage succeeded (COMPLETED or any "
            "later-stage FAILED). None if the planner itself did not run or failed."
        ),
    )
    plan_summary: str = Field(
        default="",
        description="High-level goal from the ImplementationPlan, if one was produced.",
    )
    changed_files: list[str] = Field(default_factory=list)
    diff: str = Field(default="")
    test_result: TestResult | None = None
    final_message: str = Field(
        description="Human-readable summary of the workflow outcome."
    )
    failure_reason: str | None = Field(
        default=None,
        description=(
            "Bounded, sanitised failure description (no stack traces). "
            "Set only when workflow_status is FAILED."
        ),
    )


# ── Internal mutable workflow state (not exposed to API) ─────────────────────


class WorkflowState(BaseModel):
    """Internal mutable state threaded through the orchestration stages."""

    task_id: str
    task_description: str
    status: WorkflowStatus = WorkflowStatus.PENDING

    # Artifacts set by each stage
    repository_context: RepositoryContext | None = None
    implementation_plan: ImplementationPlan | None = None
    coding_result: CodingResult | None = None
    test_result: TestResult | None = None

    # Failure information — cleared on any fresh attempt (none in Phase 5)
    failure_reason: str | None = None

    model_config = {"arbitrary_types_allowed": True}  # type: ignore[assignment]

    def transition(self, to: WorkflowStatus) -> None:
        """Advance the workflow to *to*, raising ValueError on illegal moves."""
        if not is_valid_transition(self.status, to):
            raise ValueError(
                f"Invalid workflow transition: {self.status!r} → {to!r}"
            )
        self.status = to
