import type { TaskActivity, TaskDetail } from "../api/types";
import { workflowSteps, type StepState } from "../lib/workflow";

const STATE_TEXT: Record<StepState, string> = {
  done: "done",
  current: "in progress",
  waiting: "waiting for a person",
  failed: "failed here",
  stopped: "stopped here",
  upcoming: "not reached",
  unrecorded: "not recorded",
};

const MARKER: Partial<Record<StepState, string>> = { done: "✓", failed: "!", stopped: "–", unrecorded: "?" };

/** Plan → plan approval → code & tests → publication review → pull request, for a GitHub task. */
export function WorkflowProgress({ task, activity }: { task: TaskDetail; activity: TaskActivity | undefined }) {
  const steps = workflowSteps(task, activity);
  if (!steps) return null;
  const unknownStop = (task.status === "FAILED" || task.status === "CANCELLED") && steps.every((s) => s.state === "upcoming");
  return (
    // A group, not a navigation landmark: nothing in it is a link.
    <div className="workflow" role="group" aria-label="Workflow progress">
      <ol className="workflow-steps">
        {steps.map((step, index) => (
          <li
            key={step.key}
            className={`workflow-step is-${step.state}`}
            aria-current={step.state === "current" || step.state === "waiting" ? "step" : undefined}
          >
            <span className="workflow-marker" aria-hidden="true">
              {MARKER[step.state] ?? index + 1}
            </span>
            <span className="workflow-label">
              {step.label}
              <span className="workflow-state">{STATE_TEXT[step.state]}</span>
            </span>
          </li>
        ))}
      </ol>
      {steps.some((step) => step.state === "unrecorded") && (
        <p className="muted small">
          Steps marked "not recorded" have no record that they happened.
        </p>
      )}
      {unknownStop && (
        <p className="muted small">The history doesn't record which step this task stopped in.</p>
      )}
    </div>
  );
}
