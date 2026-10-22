import { describe, expect, it } from "vitest";
import { activity, activityItem, taskDetail } from "../test/helpers";
import { retriesOf, stageOf, stoppedStage, workflowSteps } from "./workflow";

const states = (steps: ReturnType<typeof workflowSteps>) => steps?.map((step) => `${step.key}:${step.state}`);

const approval = (status: string) => ({
  id: 7,
  approval_type: "PLAN",
  status,
  requested_by: null,
  approved_by: "ada",
  comment: null,
  requested_at: "2026-10-20T10:00:00Z",
  responded_at: null,
  expires_at: null,
});

describe("workflow progress", () => {
  it("places QUEUED before planning, or after an approved plan", () => {
    expect(stageOf("QUEUED", false)).toBe("plan");
    expect(stageOf("QUEUED", true)).toBe("build");
    expect(stageOf("COMPLETED", true)).toBeNull();
  });

  it("marks a decision as waiting for a person", () => {
    expect(states(workflowSteps(taskDetail({ status: "WAITING_APPROVAL" }), undefined))).toEqual([
      "plan:done",
      "plan_approval:waiting",
      "build:upcoming",
      "publication_review:upcoming",
      "publish:upcoming",
    ]);
  });

  it("puts an approved, queued task in code & tests", () => {
    const task = taskDetail({ status: "QUEUED", latest_approval: approval("APPROVED") });
    expect(states(workflowSteps(task, undefined))?.slice(0, 3)).toEqual(["plan:done", "plan_approval:done", "build:current"]);
  });

  it("completes every step of a GitHub task", () => {
    expect(workflowSteps(taskDetail({ status: "COMPLETED" }), undefined)?.every((s) => s.state === "done")).toBe(true);
  });

  it("finds the failed step from the recorded transition", () => {
    const task = taskDetail({ status: "FAILED", latest_approval: approval("APPROVED") });
    const history = activity([
      activityItem({ kind: "status", at: "2026-10-20T10:05:00Z", from_status: "WAITING_APPROVAL", to_status: "QUEUED" }),
      activityItem({ kind: "status", at: "2026-10-20T10:06:00Z", from_status: "QUEUED", to_status: "FAILED" }),
    ]);
    expect(stoppedStage(task, history)).toBe("build");
    expect(states(workflowSteps(task, history))).toEqual([
      "plan:done",
      "plan_approval:done",
      "build:failed",
      "publication_review:upcoming",
      "publish:upcoming",
    ]);
  });

  it("fails in planning when the plan was never approved", () => {
    const task = taskDetail({ status: "FAILED" });
    const history = activity([
      activityItem({ kind: "status", at: "2026-10-20T10:06:00Z", from_status: "PLANNING", to_status: "FAILED" }),
    ]);
    expect(states(workflowSteps(task, history))?.[0]).toBe("plan:failed");
  });

  it("marks a cancelled task as stopped, not failed", () => {
    const task = taskDetail({ status: "CANCELLED" });
    const history = activity([
      activityItem({ kind: "status", at: "2026-10-20T10:06:00Z", from_status: "WAITING_APPROVAL", to_status: "CANCELLED" }),
    ]);
    expect(states(workflowSteps(task, history))?.slice(0, 2)).toEqual(["plan:done", "plan_approval:stopped"]);
  });

  it("claims no step when the history doesn't say where a task stopped", () => {
    const task = taskDetail({ status: "FAILED" });
    expect(stoppedStage(task, activity([], { partial: true }))).toBeNull();
    expect(workflowSteps(task, activity([]))?.every((s) => s.state === "upcoming")).toBe(true);
  });

  it("has no GitHub workflow for local tasks", () => {
    expect(workflowSteps(taskDetail({ execution_target: "local", status: "CODING" }), undefined)).toBeNull();
  });

  it("lists retries in order", () => {
    const history = activity([
      activityItem({ kind: "retried", at: "2026-10-20T11:00:00Z", related_task_id: "a" }),
      activityItem({ kind: "status", at: "2026-10-20T11:01:00Z", to_status: "FAILED" }),
      activityItem({ kind: "retried", at: "2026-10-20T12:00:00Z", related_task_id: "b" }),
    ]);
    expect(retriesOf(history)).toEqual(["a", "b"]);
  });
});
