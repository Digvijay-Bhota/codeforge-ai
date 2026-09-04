import pytest

from app.planning.validator import PlanValidationError, validate_plan
from app.schemas.plan import ImplementationPlan, PlanAction, PlanStep, RiskLevel


def make_valid_plan() -> ImplementationPlan:
    return ImplementationPlan(
        goal="Do a thing",
        validation_strategy="Run tests",
        risk_level=RiskLevel.low,
        summary="summary",
        steps=[
            PlanStep(
                step_number=1,
                action=PlanAction.modify,
                description="Change it",
                affected_paths=["src/auth.py"],
                rationale="Needs changing",
                dependencies=[]
            )
        ],
        affected_files=["src/auth.py"]
    )

def test_valid_plan():
    plan = make_valid_plan()
    validate_plan(plan)  # Should not raise

def test_zero_step_plan():
    plan = make_valid_plan()
    plan.steps = []
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "at least one step" in str(exc.value)

def test_invalid_step_ordering_duplicate():
    plan = make_valid_plan()
    plan.steps.append(
        PlanStep(step_number=1, action=PlanAction.modify, description="A", rationale="A")
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "Expected step 2 but got 1" in str(exc.value)

def test_invalid_step_ordering_gap():
    plan = make_valid_plan()
    plan.steps.append(
        PlanStep(step_number=3, action=PlanAction.modify, description="A", rationale="A")
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "Expected step 2 but got 3" in str(exc.value)

def test_negative_step_number():
    plan = make_valid_plan()
    plan.steps[0].step_number = -1
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "Expected step 1 but got -1" in str(exc.value)

def test_absolute_path_rejection():
    plan = make_valid_plan()
    plan.steps[0].affected_paths = ["/etc/passwd"]
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "must be relative" in str(exc.value)

def test_traversal_path_rejection():
    plan = make_valid_plan()
    plan.steps[0].affected_paths = ["../secret.txt"]
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "traversal not allowed" in str(exc.value)

def test_excessive_step_count():
    plan = make_valid_plan()
    plan.steps = [
        PlanStep(step_number=i+1, action=PlanAction.modify, description="A", rationale="A")
        for i in range(25)
    ]
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "exceeds maximum step count" in str(exc.value)

def test_dependency_on_later_step():
    plan = make_valid_plan()
    plan.steps[0].dependencies = [2]
    plan.steps.append(
        PlanStep(step_number=2, action=PlanAction.modify, description="B", rationale="B")
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "depends on invalid step 2 (must depend on an earlier step)" in str(exc.value)

def test_dependency_on_self():
    plan = make_valid_plan()
    plan.steps[0].dependencies = [1]
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "depends on invalid step 1" in str(exc.value)

def test_dependency_zero():
    plan = make_valid_plan()
    plan.steps[0].dependencies = [0]
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan)
    assert "step numbers start at 1" in str(exc.value)

def test_valid_dependency():
    plan = make_valid_plan()
    plan.steps.append(
        PlanStep(step_number=2, action=PlanAction.modify, description="B", rationale="B", dependencies=[1])
    )
    validate_plan(plan)  # Should not raise
