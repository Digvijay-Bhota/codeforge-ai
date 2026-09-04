from enum import Enum

from pydantic import BaseModel, Field

MAX_PLAN_STEPS = 20
MAX_PLAN_FILES = 50
MAX_PLAN_TEXT_LENGTH = 2000

class PlanAction(str, Enum):
    create = "create"
    modify = "modify"
    delete = "delete"
    test = "test"
    investigate = "investigate"

class PlanStep(BaseModel):
    step_number: int
    action: PlanAction
    description: str = Field(..., max_length=MAX_PLAN_TEXT_LENGTH)
    affected_paths: list[str] = Field(default_factory=list, max_length=10)
    rationale: str = Field(..., max_length=MAX_PLAN_TEXT_LENGTH)
    dependencies: list[int] = Field(default_factory=list, description="Step numbers this step depends on")

class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"

class ImplementationPlan(BaseModel):
    goal: str = Field(..., max_length=MAX_PLAN_TEXT_LENGTH)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    steps: list[PlanStep] = Field(..., min_length=1, max_length=MAX_PLAN_STEPS)
    affected_files: list[str] = Field(default_factory=list, max_length=MAX_PLAN_FILES)
    tests_needed: list[str] = Field(default_factory=list, max_length=20)
    validation_strategy: str = Field(..., max_length=MAX_PLAN_TEXT_LENGTH)
    risks: list[str] = Field(default_factory=list, max_length=20)
    risk_level: RiskLevel
    summary: str = Field(..., max_length=MAX_PLAN_TEXT_LENGTH)
