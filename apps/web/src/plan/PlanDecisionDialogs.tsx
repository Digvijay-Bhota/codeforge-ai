import { useId, useState } from "react";
import { api } from "../api/endpoints";
import type { TaskDetail } from "../api/types";
import { Dialog } from "../components/Dialog";
import { useDecision } from "../lib/useDecision";

interface Props {
  task: TaskDetail;
  approvalId: number;
  /** The server reports test execution disabled: an approved plan will stop before coding. */
  executionDisabled?: boolean;
  onClose: () => void;
  onDone: (message: string) => void;
  /** The server's state moved on (409): reload instead of retrying. */
  onStale: (message: string) => void;
}

const MAX_NOTE = 5000;

/** Exactly what the decision answers: this task's approval request, which the server checks is still pending. */
function Identity({ task, approvalId }: { task: TaskDetail; approvalId: number }) {
  return (
    <p className="muted small">
      {task.repository ? <>Repository <strong>{task.repository}</strong> · </> : null}
      Task <code>{task.task_id}</code> · Approval request <strong>#{approvalId}</strong>
    </p>
  );
}

export function ApprovePlanDialog({ task, approvalId, executionDisabled = false, onClose, onDone, onStale }: Props) {
  const noteId = useId();
  const [comment, setComment] = useState("");
  const { busy, error, submit } = useDecision(onDone, onStale);
  const github = task.execution_target === "github";

  return (
    <Dialog
      title="Approve this plan?"
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="button button-primary"
            disabled={busy}
            onClick={() => submit(() => api.approvePlan(task.task_id, approvalId, comment.trim() || null))}
          >
            {busy ? "Approving…" : "Approve plan"}
          </button>
        </>
      }
    >
      <Identity task={task} approvalId={approvalId} />
      {executionDisabled ? (
        // Nothing will be generated, so there is nothing to describe beyond the stop.
        <p className="notice notice-warning">
          Execution is disabled on this server (test execution mode <code>disabled</code>). Approving records your
          decision, then the task stops: <strong>no code is written, no tests run and nothing is published.</strong>
        </p>
      ) : (
        <>
          <p>CodeForge will start implementing this plan:</p>
          <ul className="dialog-list">
            <li>the task is queued, and the worker writes code and runs the tests;</li>
            {github ? (
              <li>
                <strong>nothing is published.</strong> The generated changes come back here for a separate publication
                review before anything reaches the repository.
              </li>
            ) : (
              <li>
                changes are made in the task's workspace
                {task.workspace_path ? (
                  <>
                    {" "}
                    <code>{task.workspace_path}</code>
                  </>
                ) : null}
                .
              </li>
            )}
          </ul>
        </>
      )}
      <p className="muted">Your access is checked again by the server; this console decides nothing on its own.</p>
      <div className="field">
        <label htmlFor={noteId}>Comment (optional)</label>
        <textarea
          id={noteId}
          rows={3}
          maxLength={MAX_NOTE}
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          disabled={busy}
        />
      </div>
      {error && (
        <p className="notice notice-danger" role="alert">
          {error}
        </p>
      )}
    </Dialog>
  );
}

export function RejectPlanDialog({ task, approvalId, onClose, onDone, onStale }: Props) {
  const reasonId = useId();
  const [reason, setReason] = useState("");
  const { busy, error, submit } = useDecision(onDone, onStale);

  return (
    <Dialog
      title="Reject this plan?"
      onClose={onClose}
      busy={busy}
      footer={
        <>
          <button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="button button-danger"
            disabled={busy}
            onClick={() => submit(() => api.rejectPlan(task.task_id, approvalId, reason.trim() || null))}
          >
            {busy ? "Rejecting…" : "Reject plan"}
          </button>
        </>
      }
    >
      <Identity task={task} approvalId={approvalId} />
      <p>
        The task is cancelled and nothing is implemented. To try again, retry the task: it plans again, and the new
        plan comes back for approval.
      </p>
      <div className="field">
        <label htmlFor={reasonId}>Reason (optional)</label>
        <textarea
          id={reasonId}
          rows={3}
          maxLength={MAX_NOTE}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          disabled={busy}
        />
      </div>
      {error && (
        <p className="notice notice-danger" role="alert">
          {error}
        </p>
      )}
    </Dialog>
  );
}
