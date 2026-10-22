import { useId, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/endpoints";
import type { TaskDetail } from "../api/types";
import { Dialog } from "../components/Dialog";
import { canRetry } from "../lib/status";
import { useDecision } from "../lib/useDecision";

const MAX_INSTRUCTIONS = 10000; // RetryTaskRequest.additional_instructions

/** "Retry task" for a failed or cancelled task: a new task from the same request. */
export function RetryTask({ task }: { task: TaskDetail }) {
  const [open, setOpen] = useState(false);
  if (!canRetry(task.status)) return null;
  return (
    <div className="next-step">
      <p className="next-step-text">
        Retrying creates a new task from the same request. This task and its record stay as they are.
      </p>
      <button type="button" className="button button-secondary" onClick={() => setOpen(true)}>
        Retry task
      </button>
      {open && <RetryDialog task={task} onClose={() => setOpen(false)} />}
    </div>
  );
}

function RetryDialog({ task, onClose }: { task: TaskDetail; onClose: () => void }) {
  const navigate = useNavigate();
  const instructionsId = useId();
  const [instructions, setInstructions] = useState("");
  // Navigation happens inside the submitted action, so the dialog closes with the page.
  const { busy, error, submit } = useDecision(() => undefined);
  const github = task.execution_target === "github";

  return (
    <Dialog
      title="Retry this task?"
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
            onClick={() =>
              submit(async () => {
                const created = await api.retryTask(task.task_id, instructions.trim() || null);
                navigate(`/tasks/${encodeURIComponent(created.task_id)}`);
                return { message: "" };
              })
            }
          >
            {busy ? "Creating…" : "Retry task"}
          </button>
        </>
      }
    >
      <p className="muted small">
        {task.repository ? (
          <>
            Repository <strong>{task.repository}</strong> ·{" "}
          </>
        ) : null}
        Task <code>{task.task_id}</code>
      </p>
      <ul className="dialog-list">
        <li>a new task is created from the same request, with your extra instructions if you add any;</li>
        {github ? (
          <li>
            it <strong>plans again, and the new plan waits for approval</strong> before anything is written;
          </li>
        ) : (
          <li>it runs in a copy of this task's workspace, and the server checks again that you may run it;</li>
        )}
        <li>this task keeps its status and history.</li>
      </ul>
      <div className="field">
        <label htmlFor={instructionsId}>Extra instructions (optional)</label>
        <textarea
          id={instructionsId}
          rows={3}
          maxLength={MAX_INSTRUCTIONS}
          value={instructions}
          onChange={(event) => setInstructions(event.target.value)}
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
