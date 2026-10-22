// Where a GitHub task is in its workflow, derived only from what the API
// recorded: the task's status, its plan approval and its activity.  Nothing
// here decides anything; the server enforces every step.
//
//   plan -> plan approval -> code & tests -> publication review -> pull request
//
// A GitHub task completes only by publishing (apps/api/app/worker.py), so
// COMPLETED means every step is done.

import type { ActivityItem, TaskActivity, TaskDetail } from "../api/types";

export type StageKey = "plan" | "plan_approval" | "build" | "publication_review" | "publish";

export type StepState = "done" | "current" | "waiting" | "failed" | "stopped" | "upcoming";

export interface WorkflowStep {
  key: StageKey;
  label: string;
  state: StepState;
}

const STAGES: { key: StageKey; label: string }[] = [
  { key: "plan", label: "Plan" },
  { key: "plan_approval", label: "Plan approval" },
  { key: "build", label: "Code & tests" },
  { key: "publication_review", label: "Publication review" },
  { key: "publish", label: "Pull request" },
];

export const STAGE_LABEL: Record<StageKey, string> = Object.fromEntries(
  STAGES.map(({ key, label }) => [key, label]),
) as Record<StageKey, string>;

/** The stage a task is in while it has *status*.  QUEUED comes before planning and again after approval. */
export function stageOf(status: string, planApproved: boolean): StageKey | null {
  switch (status) {
    case "PENDING":
    case "ANALYZING":
    case "PLANNING":
      return "plan";
    case "QUEUED":
      return planApproved ? "build" : "plan";
    case "WAITING_APPROVAL":
      return "plan_approval";
    case "CODING":
    case "TESTING":
      return "build";
    case "WAITING_PUBLICATION":
      return "publication_review";
    case "PUBLISHING":
      return "publish";
    default:
      return null;
  }
}

function planApprovedIn(task: TaskDetail, items: ActivityItem[]): boolean {
  if (task.latest_approval?.status === "APPROVED") return true;
  return items.some(
    (item) => item.kind === "approval_decided" && item.approval_type === "plan" && item.approval_status === "APPROVED",
  );
}

/** The recorded transition that ended the task (to FAILED or CANCELLED), if there is one. */
export function endingTransition(task: TaskDetail, activity: TaskActivity | undefined): ActivityItem | null {
  if (!activity || (task.status !== "FAILED" && task.status !== "CANCELLED")) return null;
  for (let i = activity.items.length - 1; i >= 0; i--) {
    const item = activity.items[i];
    if (item?.kind === "status" && item.to_status === task.status) return item;
  }
  return null;
}

/**
 * The stage a failed or cancelled task stopped in, or null when its history
 * does not say (older tasks, or a truncated history).
 */
export function stoppedStage(task: TaskDetail, activity: TaskActivity | undefined): StageKey | null {
  const ending = endingTransition(task, activity);
  if (!ending?.from_status) return null;
  return stageOf(ending.from_status, planApprovedIn(task, activity?.items ?? []));
}

/** The workflow steps of a GitHub task; null for other tasks, whose steps differ. */
export function workflowSteps(task: TaskDetail, activity: TaskActivity | undefined): WorkflowStep[] | null {
  if (task.execution_target !== "github") return null;
  const index = (key: StageKey | null) => (key ? STAGES.findIndex((stage) => stage.key === key) : -1);

  if (task.status === "COMPLETED") return STAGES.map((stage) => ({ ...stage, state: "done" }));

  if (task.status === "FAILED" || task.status === "CANCELLED") {
    const at = index(stoppedStage(task, activity));
    const ended: StepState = task.status === "FAILED" ? "failed" : "stopped";
    // Unknown stage: say nothing about any step rather than guess.
    if (at < 0) return STAGES.map((stage) => ({ ...stage, state: "upcoming" }));
    return STAGES.map((stage, i) => ({ ...stage, state: i < at ? "done" : i === at ? ended : "upcoming" }));
  }

  const at = index(stageOf(task.status, planApprovedIn(task, activity?.items ?? [])));
  if (at < 0) return null;
  const waiting = task.status === "WAITING_APPROVAL" || task.status === "WAITING_PUBLICATION";
  return STAGES.map((stage, i) => ({
    ...stage,
    state: i < at ? "done" : i === at ? (waiting ? "waiting" : "current") : "upcoming",
  }));
}

/** Tasks created as retries of this one that the viewer can read, oldest first. */
export function retriesOf(activity: TaskActivity | undefined): string[] {
  return (activity?.items ?? [])
    .filter((item) => item.kind === "retried" && item.related_task_id)
    .map((item) => item.related_task_id as string);
}
