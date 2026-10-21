// The worker stores a plan as free-form JSON (apps/api/app/schemas/plan.py's
// ImplementationPlan, or a bare {"steps": [...]} fallback).  Read it
// defensively: keep only well-typed fields, so a malformed plan shows less,
// never breaks the page or renders anything but text.

export interface PlanStepView {
  number: number | null;
  action: string | null;
  description: string;
  rationale: string | null;
  affectedPaths: string[];
  dependencies: number[];
}

export interface PlanView {
  summary: string | null;
  goal: string | null;
  riskLevel: string | null;
  steps: PlanStepView[];
  affectedFiles: string[];
  testsNeeded: string[];
  validationStrategy: string | null;
  assumptions: string[];
  risks: string[];
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim() !== "") : [];
}

function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function step(raw: unknown): PlanStepView | null {
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Record<string, unknown>;
  const description = text(item.description);
  if (!description) return null;
  return {
    number: integer(item.step_number) ?? integer(item.step),
    action: text(item.action),
    description,
    rationale: text(item.rationale),
    affectedPaths: texts(item.affected_paths),
    dependencies: Array.isArray(item.dependencies)
      ? item.dependencies.filter((n): n is number => typeof n === "number" && Number.isInteger(n))
      : [],
  };
}

/** A displayable plan, or null if *raw* holds nothing usable. */
export function parsePlan(raw: unknown): PlanView | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const plan = raw as Record<string, unknown>;
  const steps = Array.isArray(plan.steps) ? plan.steps.map(step).filter((s): s is PlanStepView => s !== null) : [];
  const view: PlanView = {
    summary: text(plan.summary),
    goal: text(plan.goal),
    riskLevel: text(plan.risk_level),
    steps,
    affectedFiles: texts(plan.affected_files),
    testsNeeded: texts(plan.tests_needed),
    validationStrategy: text(plan.validation_strategy),
    assumptions: texts(plan.assumptions),
    risks: texts(plan.risks),
  };
  const empty = !view.summary && !view.goal && steps.length === 0 && view.affectedFiles.length === 0;
  return empty ? null : view;
}
