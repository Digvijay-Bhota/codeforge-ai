from pathlib import Path

from app.schemas.plan import MAX_PLAN_FILES, MAX_PLAN_STEPS, ImplementationPlan


class PlanValidationError(Exception):
    """Exception raised when an implementation plan fails deterministic validation."""

def validate_plan(plan: ImplementationPlan) -> None:
    """Deterministically validates an ImplementationPlan."""
    if not plan.steps:
        raise PlanValidationError("Plan must contain at least one step")

    if len(plan.steps) > MAX_PLAN_STEPS:
        raise PlanValidationError(f"Plan exceeds maximum step count of {MAX_PLAN_STEPS}")

    if len(plan.affected_files) > MAX_PLAN_FILES:
        raise PlanValidationError(f"Plan exceeds maximum affected files of {MAX_PLAN_FILES}")

    for file_path in plan.affected_files:
        if not file_path or file_path.startswith("/") or file_path.startswith("\\"):
            raise PlanValidationError(f"Affected file path must be relative: {file_path}")
        if ".." in Path(file_path).parts:
            raise PlanValidationError(f"Affected file path traversal not allowed: {file_path}")

    # Validate steps
    expected_step_num = 1
    for step in plan.steps:
        if step.step_number != expected_step_num:
            raise PlanValidationError(f"Invalid step ordering. Expected step {expected_step_num} but got {step.step_number}")

        for path in step.affected_paths:
            if not path or path.startswith("/") or path.startswith("\\"):
                raise PlanValidationError(f"Path must be relative: {path}")
            if ".." in Path(path).parts:
                raise PlanValidationError(f"Path traversal not allowed: {path}")

        for dep in step.dependencies:
            # Dependencies must refer to an existing step strictly before the current step
            # This ensures no self-dependency, no forward-dependency, and no cycles.
            if dep >= step.step_number:
                raise PlanValidationError(f"Step {step.step_number} depends on invalid step {dep} (must depend on an earlier step)")
            if dep < 1:
                raise PlanValidationError(f"Step {step.step_number} depends on invalid step {dep} (step numbers start at 1)")

        expected_step_num += 1
