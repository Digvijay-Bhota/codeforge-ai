import { useId, useState } from "react";
import { api } from "../api/endpoints";
import type { PublicationArtifact } from "../api/types";
import { Dialog } from "../components/Dialog";
import { useDecision } from "../lib/useDecision";

interface DecisionProps {
  artifact: PublicationArtifact;
  onClose: () => void;
  onDone: (message: string) => void;
  /** The server's state moved on (409): reload rather than retry against stale changes. */
  onStale: (message: string) => void;
}

const MAX_NOTE = 2000;

export function ApproveDialog({ artifact, onClose, onDone, onStale }: DecisionProps) {
  const noteId = useId();
  const [comment, setComment] = useState("");
  const { busy, error, submit } = useDecision(onDone, onStale);
  const files = artifact.files.length;

  return (
    <Dialog
      title="Approve and publish these changes?"
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
              submit(() => api.approvePublication(artifact.task_id, artifact.artifact_digest, comment.trim() || null))
            }
          >
            {busy ? "Approving…" : "Approve and publish"}
          </button>
        </>
      }
    >
      <p>CodeForge will publish exactly what you reviewed to <strong>{artifact.repository}</strong>:</p>
      <ul className="dialog-list">
        <li>
          a branch <code>codeforge/task-{artifact.task_id}</code> from <code>{artifact.base_branch}</code>
        </li>
        <li>
          one commit changing {files} {files === 1 ? "file" : "files"}
        </li>
        <li>a pull request, which starts the repository's CI</li>
      </ul>
      <p className="muted">
        Your write access is checked again right before each step. If it is revoked, nothing more is published.
      </p>
      <div className="digest">
        <span className="digest-label">Approving digest</span>
        <code className="digest-value">{artifact.artifact_digest}</code>
      </div>
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

export function RejectDialog({ artifact, onClose, onDone, onStale }: DecisionProps) {
  const reasonId = useId();
  const [reason, setReason] = useState("");
  const { busy, error, submit } = useDecision(onDone, onStale);

  return (
    <Dialog
      title="Reject these changes?"
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
            onClick={() => submit(() => api.rejectPublication(artifact.task_id, reason.trim() || null))}
          >
            {busy ? "Rejecting…" : "Reject changes"}
          </button>
        </>
      }
    >
      <p>
        Nothing is pushed to <strong>{artifact.repository}</strong> and the task is cancelled. To try again, retry
        the task: it generates new changes for review.
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
