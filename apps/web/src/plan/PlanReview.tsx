import { useEffect, useState } from "react";
import type { ApprovalDetail, TaskDetail } from "../api/types";
import { Badge } from "../components/StatusBadge";
import { formatAbsolute, formatRelative, formatTimeLeft } from "../lib/format";
import { parsePlan, type PlanView } from "../lib/plan";
import type { Tone } from "../lib/status";
import { api } from "../api/endpoints";
import { useApi } from "../lib/useApi";
import { ApprovePlanDialog, RejectPlanDialog } from "./PlanDecisionDialogs";

interface Decision {
  label: string;
  tone: Tone;
  detail: string | null;
  /** The approval request a decision would answer, when one is allowed. */
  approvalId: number | null;
}

/** What the server says about the plan decision; the server re-checks all of it. */
export function planDecision(task: TaskDetail, now = Date.now()): Decision {
  const approval: ApprovalDetail | null = task.latest_approval;
  if (!approval) {
    return { label: "No approval requested", tone: "neutral", detail: null, approvalId: null };
  }
  const by = approval.approved_by ? ` by ${approval.approved_by}` : "";
  switch (approval.status) {
    case "APPROVED":
      return { label: "Plan approved", tone: "success", detail: `Approved${by}.`, approvalId: null };
    case "REJECTED":
      return {
        label: "Plan rejected",
        tone: "neutral",
        detail: `Rejected${by}.${approval.comment ? ` Reason: ${approval.comment}` : ""}`,
        approvalId: null,
      };
    case "EXPIRED":
      return { label: "Approval expired", tone: "neutral", detail: "The approval window closed; the task was cancelled.", approvalId: null };
    case "PENDING": {
      if (task.status !== "WAITING_APPROVAL") {
        return { label: "Not awaiting approval", tone: "neutral", detail: "The task is no longer waiting for a plan decision.", approvalId: null };
      }
      const timeLeft = formatTimeLeft(approval.expires_at, now);
      if (approval.expires_at && !timeLeft) {
        return { label: "Approval expired", tone: "neutral", detail: "The approval window closed.", approvalId: null };
      }
      return {
        label: "Awaiting plan approval",
        tone: "warning",
        detail: timeLeft ? `Expires in ${timeLeft} (${formatAbsolute(approval.expires_at)}).` : null,
        approvalId: approval.id,
      };
    }
    default:
      return { label: approval.status, tone: "neutral", detail: null, approvalId: null };
  }
}

const RISK_TONE: Record<string, Tone> = { low: "success", medium: "warning", high: "danger" };

/** Statuses up to and including the wait for a plan decision. */
const BEFORE_APPROVAL = new Set(["PENDING", "QUEUED", "ANALYZING", "PLANNING", "WAITING_APPROVAL"]);

interface Props {
  task: TaskDetail;
  /** A decision was recorded, or the server's state moved on: reload the task. */
  onChanged: () => void;
}

export function PlanReview({ task, onChanged }: Props) {
  const plan = parsePlan(task.implementation_plan);
  const [dialog, setDialog] = useState<"approve" | "reject" | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "warning"; text: string } | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const decision = planDecision(task, now);
  // Past the point where a plan waits for approval, with none on record: an
  // older task, or a local one that did not ask for approval.  State that,
  // not the rule.
  const unapprovedPastGate = !task.latest_approval && !BEFORE_APPROVAL.has(task.status);
  // Whether an approved plan can actually run here (the server decides; this only explains).
  const { data: system } = useApi("system-status", (signal) => api.systemStatus(signal));
  const executionDisabled = system?.test_execution_mode === "disabled";
  if (!plan && !task.latest_approval && task.status !== "WAITING_APPROVAL") return null;

  function finish(tone: "success" | "warning", text: string) {
    setDialog(null);
    setNotice({ tone, text });
    onChanged();
  }

  function stale(message: string) {
    finish("warning", `The task changed before your decision was recorded: ${message} It has been reloaded.`);
  }

  return (
    <section className="card plan" aria-labelledby="plan-title">
      <header className="card-header">
        <div>
          <h2 className="card-title" id="plan-title">
            Proposed plan
          </h2>
          <p className="card-subtitle">
            {unapprovedPastGate
              ? "No plan approval was recorded for this task."
              : "Nothing is implemented until someone with write access approves this plan. Approving it does not publish anything."}
          </p>
        </div>
        <Badge tone={decision.tone}>{decision.label}</Badge>
      </header>

      {notice && (
        <p className={`notice notice-${notice.tone}`} role="status">
          {notice.text}
        </p>
      )}
      {decision.detail && <p className="review-detail">{decision.detail}</p>}
      {task.latest_approval && (
        <p className="approval-meta">
          Approval request #{task.latest_approval.id} · requested{" "}
          <time dateTime={task.latest_approval.requested_at} title={formatAbsolute(task.latest_approval.requested_at)}>
            {formatRelative(task.latest_approval.requested_at)}
          </time>
          {decision.approvalId !== null &&
            ". A decision answers this request for this plan only; if the task moves on first, it is refused and nothing changes."}
        </p>
      )}

      {plan ? <PlanBody plan={plan} /> : <p className="muted">No plan details were recorded for this task.</p>}

      {decision.approvalId !== null && (
        <div className="decision-bar">
          <p className="decision-hint">
            {executionDisabled
              ? "Execution is disabled on this server: approving records your decision, but the task stops before any code is written."
              : "Approving starts the implementation. Publication is reviewed separately."}
          </p>
          <div className="decision-actions">
            <button type="button" className="button button-danger-outline" onClick={() => setDialog("reject")}>
              Reject plan
            </button>
            <button type="button" className="button button-primary" onClick={() => setDialog("approve")}>
              Approve plan
            </button>
          </div>
        </div>
      )}

      {dialog === "approve" && decision.approvalId !== null && (
        <ApprovePlanDialog
          task={task}
          executionDisabled={executionDisabled}
          approvalId={decision.approvalId}
          onClose={() => setDialog(null)}
          onDone={(message) => finish("success", message)}
          onStale={stale}
        />
      )}
      {dialog === "reject" && decision.approvalId !== null && (
        <RejectPlanDialog
          task={task}
          approvalId={decision.approvalId}
          onClose={() => setDialog(null)}
          onDone={(message) => finish("success", message)}
          onStale={stale}
        />
      )}
    </section>
  );
}

function List({ title, items, code = false }: { title: string; items: string[]; code?: boolean }) {
  if (items.length === 0) return null;
  return (
    <div className="plan-block">
      <h3 className="section-title">{title}</h3>
      <ul className="plan-list">
        {items.map((item, index) => (
          <li key={index}>{code ? <code>{item}</code> : item}</li>
        ))}
      </ul>
    </div>
  );
}

function PlanBody({ plan }: { plan: PlanView }) {
  return (
    <div className="plan-body">
      {(plan.summary || plan.goal || plan.riskLevel) && (
        <div className="plan-block">
          {plan.summary && <p className="plan-summary">{plan.summary}</p>}
          {plan.goal && plan.goal !== plan.summary && (
            <p>
              <span className="muted">Goal: </span>
              {plan.goal}
            </p>
          )}
          {plan.riskLevel && (
            <p className="plan-risk">
              <span className="muted">Risk </span>
              <Badge tone={RISK_TONE[plan.riskLevel] ?? "neutral"}>{plan.riskLevel}</Badge>
            </p>
          )}
        </div>
      )}

      {plan.steps.length > 0 && (
        <div className="plan-block">
          <h3 className="section-title">Steps</h3>
          <ol className="plan-steps">
            {plan.steps.map((step, index) => (
              <li key={index} className="plan-step">
                <div className="plan-step-head">
                  <span className="plan-step-number" aria-hidden="true">
                    {step.number ?? index + 1}
                  </span>
                  <span className="plan-step-description">{step.description}</span>
                  {step.action && <Badge tone="info">{step.action}</Badge>}
                </div>
                {step.rationale && <p className="plan-step-rationale">{step.rationale}</p>}
                {step.affectedPaths.length > 0 && (
                  <p className="plan-step-paths">
                    {step.affectedPaths.map((path) => (
                      <code key={path}>{path}</code>
                    ))}
                  </p>
                )}
                {step.dependencies.length > 0 && (
                  <p className="muted small">After step {step.dependencies.join(", ")}</p>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}

      <div className="plan-grid">
        <List title="Affected files" items={plan.affectedFiles} code />
        <List title="Tests" items={plan.testsNeeded} />
        <List title="Assumptions" items={plan.assumptions} />
        <List title="Risks" items={plan.risks} />
      </div>
      {plan.validationStrategy && (
        <div className="plan-block">
          <h3 className="section-title">Validation</h3>
          <p>{plan.validationStrategy}</p>
        </div>
      )}
    </div>
  );
}
