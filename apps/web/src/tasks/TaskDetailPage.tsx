import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { errorMessage } from "../api/client";
import { api } from "../api/endpoints";
import type { TaskActivity, TaskDetail } from "../api/types";
import { CopyButton } from "../components/CopyButton";
import { backTarget, TaskLink } from "../components/TaskLink";
import { ArrowLeftIcon } from "../components/icons";
import { StatusBadge } from "../components/StatusBadge";
import { ErrorState, LoadingState } from "../components/StateViews";
import { explainFailure, retryOutlook } from "../lib/failures";
import {
  formatAbsolute,
  formatRelative,
  githubOriginLabel,
  githubOriginUrl,
  shortId,
  taskCommand,
  taskTitle,
} from "../lib/format";
import { isSettled, statusInfo } from "../lib/status";
import { usePageTitle } from "../lib/usePageTitle";
import { useApi } from "../lib/useApi";
import { usePolling } from "../lib/usePolling";
import { retriesOf, STAGE_LABEL, stoppedStage } from "../lib/workflow";
import { PlanReview } from "../plan/PlanReview";
import { PublicationReview } from "../review/PublicationReview";
import { ActivityTimeline } from "./ActivityTimeline";
import { RetryTask } from "./RetryTask";
import { WorkflowProgress } from "./WorkflowProgress";

export function TaskDetailPage() {
  const { taskId = "" } = useParams();
  const task = useApi(taskId, (signal) => api.getTask(taskId, signal));
  const status = task.data?.status;

  // Follow a running task until it settles or needs a person.
  usePolling(Boolean(status) && !isSettled(status ?? ""), task.refresh);

  usePageTitle(task.data ? taskTitle(task.data.requested_task) : "Task");

  if (task.error && !task.data) {
    return (
      <div className="stack-lg">
        <BackLink />
        <ErrorState title="Couldn't load this task" error={task.error} onRetry={task.reload} />
      </div>
    );
  }
  if (!task.data) {
    return (
      <div className="stack-lg">
        <BackLink />
        <LoadingState label="Loading task…" />
      </div>
    );
  }

  return <LoadedTask detail={task.data} reload={task.reload} refreshError={task.error} />;
}

function LoadedTask({ detail, reload, refreshError }: { detail: TaskDetail; reload: () => void; refreshError: unknown }) {
  // Shared by the timeline, progress and failure panel.  Reloaded when the
  // status changes, keeping the current history on screen meanwhile.
  const activity = useApi(detail.task_id, (signal) => api.getActivity(detail.task_id, signal));
  const reloadActivity = activity.reload;
  const shownStatus = useRef(detail.status);
  // Read out by screen readers when the status changes while the page is open.
  const [announcement, setAnnouncement] = useState("");
  useEffect(() => {
    if (shownStatus.current === detail.status) return;
    shownStatus.current = detail.status;
    setAnnouncement(`Status changed to ${statusInfo(detail.status).label}.`);
    reloadActivity();
  }, [detail.status, reloadActivity]);
  return (
    <div className="stack-lg">
      <BackLink />
      <p className="visually-hidden" role="status">
        {announcement}
      </p>
      {refreshError ? (
        <p className="notice notice-warning" role="status">
          Couldn't refresh this task ({errorMessage(refreshError)}). Showing what was last loaded.
        </p>
      ) : null}
      <TaskHeader task={detail} />
      <WorkflowProgress task={detail} activity={activity.data} />
      <TaskNotices task={detail} activity={activity.data} />
      <RetryTask task={detail} retries={retriesOf(activity.data)} />
      <PlanReview task={detail} onChanged={reload} />
      {detail.execution_target === "github" && (
        <PublicationReview taskId={detail.task_id} taskStatus={detail.status} onDecided={reload} />
      )}
      <ActivityTimeline
        repository={detail.repository}
        activity={activity.data}
        error={activity.error}
        reload={activity.reload}
      />
    </div>
  );
}

function BackLink() {
  const back = backTarget(useLocation().state);
  return (
    <Link to={back.path} className="back-link">
      <ArrowLeftIcon /> {back.label}
    </Link>
  );
}

function TaskHeader({ task }: { task: TaskDetail }) {
  return (
    <header className="task-header">
      <div className="task-header-top">
        <StatusBadge status={task.status} />
        {task.repository && <span className="task-header-repo">{task.repository}</span>}
        <TaskOrigin task={task} />
      </div>
      <h1 className="page-title">{taskTitle(task.requested_task)}</h1>
      <dl className="facts">
        <div>
          <dt>Created</dt>
          <dd title={formatAbsolute(task.created_at)}>{formatRelative(task.created_at)}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd title={formatAbsolute(task.started_at)}>{formatRelative(task.started_at)}</dd>
        </div>
        <div>
          <dt>Finished</dt>
          <dd title={formatAbsolute(task.completed_at)}>{formatRelative(task.completed_at)}</dd>
        </div>
        {task.parent_task_id && (
          <div>
            <dt>Retry of</dt>
            <dd>
              <TaskLink taskId={task.parent_task_id} className="mono">
                {shortId(task.parent_task_id)}
              </TaskLink>
            </dd>
          </div>
        )}
        <div className="facts-wide">
          <dt>Task ID</dt>
          <dd className="digest-inline">
            <code>{task.task_id}</code>
            <CopyButton value={task.task_id} label="Copy task ID" />
          </dd>
        </div>
      </dl>
      {task.requested_task.trim().includes("\n") && (
        <details className="request-details">
          <summary>Full request</summary>
          <pre className="text-block">{task.requested_task}</pre>
        </details>
      )}
    </header>
  );
}

/** Where a comment task came from: the command, the issue or pull request (linked) and who asked. */
function TaskOrigin({ task }: { task: TaskDetail }) {
  const command = taskCommand(task.requested_task);
  const origin = task.github;
  if (!origin && !command) return null;
  const url = githubOriginUrl(task.repository, origin);
  return (
    <span className="task-header-origin">
      {command && <code>{command}</code>}
      {origin && (
        <>
          {" on "}
          {url ? (
            <a href={url} target="_blank" rel="noreferrer noopener">
              {githubOriginLabel(origin)}
              <span className="visually-hidden"> (opens GitHub)</span>
            </a>
          ) : (
            githubOriginLabel(origin)
          )}
          {` by @${origin.triggered_by}`}
        </>
      )}
    </span>
  );
}

function TaskNotices({ task, activity }: { task: TaskDetail; activity: TaskActivity | undefined }) {
  const failed = task.status === "FAILED";
  const stage = stoppedStage(task, activity);
  return (
    <>
      {task.status === "CANCELLED" && task.failure_reason ? (
        // Rejected, expired or withdrawn: a decision, not a failure.
        <div className="notice notice-info">
          <strong>Cancelled</strong>
          <p>{task.failure_reason}</p>
        </div>
      ) : task.failure_code === "EXECUTION_UNAVAILABLE" ? (
        // An approved plan that could not start: a configuration state, not a crash.
        <div className="notice notice-warning" role="status">
          <strong>Stopped before running: execution is not available on this server</strong>
          <p>{explainFailure(task.failure_code).advice}</p>
          {task.failure_reason && <FailureDetail code={task.failure_code} reason={task.failure_reason} />}
        </div>
      ) : failed || task.failure_reason ? (
        <FailureNotice
          code={task.failure_code}
          reason={task.failure_reason}
          stage={failed && stage ? STAGE_LABEL[stage] : null}
        />
      ) : null}
      {task.final_message && !task.failure_reason && !failed && <p className="notice notice-info">{task.final_message}</p>}
    </>
  );
}

const OUTLOOK: Record<string, string | null> = {
  transient: "This looks temporary: retrying is likely to work once the service has recovered.",
  needs_change: "Retrying won't help until the cause above is fixed.",
  depends: null,
};

function FailureNotice({ code, reason, stage }: { code: string | null; reason: string | null; stage: string | null }) {
  const { title, advice } = explainFailure(code);
  // Skip it when the advice already says what retrying does.
  const outlook = code && !(advice && /retry/i.test(advice)) ? OUTLOOK[retryOutlook(code)] : null;
  return (
    <div className="notice notice-danger" role="alert">
      <strong>{title === "Failed" ? "Failed" : `Failed: ${title}`}</strong>
      {stage && <p className="notice-stage">Stopped during: {stage}</p>}
      {advice && <p>{advice}</p>}
      {outlook && <p>{outlook}</p>}
      {reason || code ? (
        <FailureDetail code={code} reason={reason} />
      ) : (
        <p>No failure details were recorded for this task.</p>
      )}
    </div>
  );
}

/** The server's code and reason, as text: the reason can quote tool and model output. */
function FailureDetail({ code, reason }: { code: string | null; reason: string | null }) {
  return (
    <dl className="notice-detail">
      {code && (
        <div>
          <dt>Code</dt>
          <dd>
            <code>{code}</code>
          </dd>
        </div>
      )}
      {reason && (
        <div>
          <dt>Details</dt>
          <dd className="notice-reason">{reason}</dd>
        </div>
      )}
    </dl>
  );
}
