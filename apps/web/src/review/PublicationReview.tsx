import { useEffect, useMemo, useState } from "react";
import { api } from "../api/endpoints";
import type { FileDiff, PublicationArtifact, PublicationFile } from "../api/types";
import { CopyButton } from "../components/CopyButton";
import { Badge } from "../components/StatusBadge";
import { ErrorState, LoadingState } from "../components/StateViews";
import { CheckIcon, ExternalIcon, FileIcon } from "../components/icons";
import { formatAbsolute, formatBytes, formatTimeLeft, safePullRequestUrl, shortSha } from "../lib/format";
import type { Tone } from "../lib/status";
import { useApi } from "../lib/useApi";
import { CodeView, DiffView, TextBlock } from "./CodeViews";
import { ApproveDialog, RejectDialog } from "./DecisionDialogs";

interface ReviewData {
  artifact: PublicationArtifact | null;
  diff: Map<string, FileDiff>;
}

interface Props {
  taskId: string;
  taskStatus: string;
  /** A decision was recorded: the task should be reloaded. */
  onDecided: () => void;
}

interface Notice {
  tone: "success" | "warning";
  text: string;
}

export function PublicationReview({ taskId, taskStatus, onDecided }: Props) {
  // Kept here, not in Review: a reload can bring changes with a new digest,
  // which mounts a new Review, and the notice must outlive that.
  const [notice, setNotice] = useState<Notice | null>(null);
  const { data, error, loading, reload } = useApi<ReviewData>(`${taskId}|${taskStatus}`, async (signal) => {
    const artifact = await api.getPublication(taskId, signal);
    if (!artifact) return { artifact: null, diff: new Map() };
    // The diff is a convenience; the review never depends on it.
    const diff = await api.getDiff(taskId, signal).catch(() => null);
    return { artifact, diff: new Map((diff?.files ?? []).map((file) => [file.path, file])) };
  });

  if (error && !data) return <ErrorState title="Couldn't load the generated changes" error={error} onRetry={reload} />;
  if (!data) return loading ? <LoadingState label="Loading generated changes…" /> : null;
  if (!data.artifact) return notice ? <NoticeLine notice={notice} /> : null;
  return (
    <Review
      key={data.artifact.artifact_digest}
      artifact={data.artifact}
      diff={data.diff}
      taskStatus={taskStatus}
      notice={notice}
      onDecided={(next) => {
        setNotice(next);
        reload();
        onDecided();
      }}
    />
  );
}

interface Decision {
  label: string;
  tone: Tone;
  detail: string | null;
  canDecide: boolean;
}

export function decisionState(artifact: PublicationArtifact, taskStatus: string, now = Date.now()): Decision {
  const timeLeft = formatTimeLeft(artifact.expires_at, now);
  if (artifact.published) return { label: "Published", tone: "success", detail: null, canDecide: false };
  switch (artifact.approval_status) {
    case "APPROVED":
      return {
        label: "Approved",
        tone: "info",
        detail: artifact.approved_by ? `Approved by ${artifact.approved_by}. Publishing…` : "Publishing…",
        canDecide: false,
      };
    case "REJECTED":
      return { label: "Rejected", tone: "neutral", detail: "Nothing was published.", canDecide: false };
    case "EXPIRED":
      return { label: "Expired", tone: "neutral", detail: "The review window closed. Nothing was published.", canDecide: false };
    case "PENDING":
      if (taskStatus !== "WAITING_PUBLICATION") {
        return { label: "Not reviewable", tone: "neutral", detail: "The task is no longer waiting for review.", canDecide: false };
      }
      if (artifact.expires_at && !timeLeft) {
        return { label: "Expired", tone: "neutral", detail: "The review window closed. Nothing was published.", canDecide: false };
      }
      return {
        label: "Awaiting review",
        tone: "warning",
        detail: timeLeft ? `Expires in ${timeLeft} (${formatAbsolute(artifact.expires_at)}).` : null,
        canDecide: true,
      };
    default:
      return { label: "Not requested", tone: "neutral", detail: "No review has been requested for these changes.", canDecide: false };
  }
}

function fileKind(file: PublicationFile): { label: string; tone: Tone } {
  return file.action === "delete" ? { label: "Delete", tone: "danger" } : { label: "Write", tone: "info" };
}

interface ReviewProps {
  artifact: PublicationArtifact;
  diff: Map<string, FileDiff>;
  taskStatus: string;
  /** Shown above the review: the outcome of the last decision made here. */
  notice: Notice | null;
  /** A decision was answered (*next* says how): reload the changes and the task. */
  onDecided: (next: Notice) => void;
}

function NoticeLine({ notice }: { notice: Notice }) {
  return (
    <p className={`notice notice-${notice.tone}`} role="status">
      {notice.text}
    </p>
  );
}

function Review({ artifact, diff, taskStatus, notice, onDecided }: ReviewProps) {
  const files = artifact.files;
  const [selected, setSelected] = useState(files[0]?.path ?? null);
  const [viewed, setViewed] = useState<Set<string>>(() => new Set(files[0] ? [files[0].path] : []));
  const [dialog, setDialog] = useState<"approve" | "reject" | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const decision = decisionState(artifact, taskStatus, now);
  const allViewed = files.length > 0 && files.every((file) => viewed.has(file.path));
  const current = useMemo(() => files.find((file) => file.path === selected) ?? null, [files, selected]);
  const pullRequestUrl = safePullRequestUrl(artifact.pull_request_url);

  function open(path: string) {
    setSelected(path);
    setViewed((previous) => (previous.has(path) ? previous : new Set(previous).add(path)));
  }

  function finish(next: Notice) {
    setDialog(null);
    onDecided(next);
  }
  const done = (text: string) => finish({ tone: "success", text });
  // Decided, expired or replaced meanwhile: show what is current now.
  const stale = (text: string) =>
    finish({ tone: "warning", text: `The changes moved on before your decision was recorded: ${text} They have been reloaded.` });

  return (
    <section className="card review" aria-labelledby="review-title">
      <header className="card-header review-header">
        <div>
          <h2 className="card-title" id="review-title">
            Generated changes
          </h2>
          <p className="card-subtitle">
            Nothing reaches <strong>{artifact.repository}</strong> until someone with write access approves these exact
            changes.
          </p>
        </div>
        <Badge tone={decision.tone}>{decision.label}</Badge>
      </header>

      {notice && <NoticeLine notice={notice} />}
      {decision.detail && <p className="review-detail">{decision.detail}</p>}
      {pullRequestUrl && (
        <p>
          <a className="button button-secondary" href={pullRequestUrl} target="_blank" rel="noopener noreferrer">
            View pull request <ExternalIcon />
          </a>
        </p>
      )}

      <dl className="facts">
        <div>
          <dt>Base</dt>
          <dd>
            <code>{artifact.base_branch}</code> at <code title={artifact.base_sha}>{shortSha(artifact.base_sha)}</code>
          </dd>
        </div>
        <div>
          <dt>Files</dt>
          <dd>{files.length}</dd>
        </div>
        <div>
          <dt>Size</dt>
          <dd>{formatBytes(artifact.total_bytes)}</dd>
        </div>
        <div className="facts-wide">
          <dt>Digest</dt>
          <dd className="digest-inline">
            <code title={artifact.artifact_digest}>{artifact.artifact_digest.slice(0, 16)}…</code>
            <CopyButton value={artifact.artifact_digest} label="Copy artifact digest" />
          </dd>
        </div>
      </dl>

      <div className="review-section">
        <h3 className="section-title">Pull request</h3>
        <p className="pr-title">{artifact.pull_request_title}</p>
        <TextBlock text={artifact.pull_request_body} label="Pull request description" />
        <h3 className="section-title">Commit message</h3>
        <TextBlock text={artifact.commit_message} label="Commit message" />
      </div>

      <div className="review-section">
        <div className="section-heading">
          <h3 className="section-title">Files</h3>
          <span className="muted" aria-live="polite">
            {viewed.size} of {files.length} reviewed
          </span>
        </div>
        <div className="files-layout">
          <ul className="file-list" role="list" aria-label="Changed files">
            {files.map((file) => {
              const kind = fileKind(file);
              const isViewed = viewed.has(file.path);
              return (
                <li key={file.path}>
                  <button
                    type="button"
                    className="file-item"
                    aria-current={file.path === selected ? "true" : undefined}
                    onClick={() => open(file.path)}
                  >
                    <span className="file-item-icon">{isViewed ? <CheckIcon /> : <FileIcon />}</span>
                    <span className="file-item-path">{file.path}</span>
                    <Badge tone={kind.tone}>{kind.label}</Badge>
                    <span className="visually-hidden">{isViewed ? "(reviewed)" : "(not yet reviewed)"}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="file-viewer">
            {current ? <FilePanel key={current.path} file={current} diff={diff.get(current.path)} /> : <p>No files.</p>}
          </div>
        </div>
      </div>

      {decision.canDecide && (
        <div className="decision-bar">
          <p className="decision-hint" id="decision-hint">
            {allViewed
              ? "The digest covers the file contents, the pull request title and description, and the commit message."
              : `Open every file to enable approval (${viewed.size} of ${files.length} reviewed).`}
          </p>
          <div className="decision-actions">
            <button type="button" className="button button-danger-outline" onClick={() => setDialog("reject")}>
              Reject
            </button>
            <button
              type="button"
              className="button button-primary"
              disabled={!allViewed}
              aria-describedby="decision-hint"
              onClick={() => setDialog("approve")}
            >
              Approve and publish
            </button>
          </div>
        </div>
      )}

      {dialog === "approve" && (
        <ApproveDialog artifact={artifact} onClose={() => setDialog(null)} onDone={done} onStale={stale} />
      )}
      {dialog === "reject" && (
        <RejectDialog artifact={artifact} onClose={() => setDialog(null)} onDone={done} onStale={stale} />
      )}
    </section>
  );
}

function FilePanel({ file, diff }: { file: PublicationFile; diff: FileDiff | undefined }) {
  const hasDiff = Boolean(diff?.patch);
  const [tab, setTab] = useState<"content" | "diff">(hasDiff ? "diff" : "content");

  return (
    <div className="file-panel">
      <div className="file-panel-header">
        <span className="file-panel-path mono">{file.path}</span>
        {file.action !== "delete" && hasDiff && (
          <div className="tabs" role="group" aria-label="View">
            <button type="button" className="tab" aria-pressed={tab === "diff"} onClick={() => setTab("diff")}>
              Diff
            </button>
            <button type="button" className="tab" aria-pressed={tab === "content"} onClick={() => setTab("content")}>
              Exact content
            </button>
          </div>
        )}
      </div>
      {file.action === "delete" ? (
        <p className="notice notice-danger">This file will be deleted.</p>
      ) : tab === "diff" && diff ? (
        <>
          <p className="muted small">
            Diff from generation, for orientation. Approval covers the exact content, not this diff.
          </p>
          <DiffView patch={diff.patch} label={`Diff of ${file.path}`} />
        </>
      ) : (
        <CodeView content={file.content ?? ""} label={`Content of ${file.path}`} />
      )}
    </div>
  );
}
