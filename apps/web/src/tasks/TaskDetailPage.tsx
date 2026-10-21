import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/endpoints";
import type { TaskDetail, TaskEvent } from "../api/types";
import { CopyButton } from "../components/CopyButton";
import { ArrowLeftIcon } from "../components/icons";
import { StatusBadge } from "../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { formatAbsolute, formatRelative, taskTitle } from "../lib/format";
import { isSettled, statusInfo } from "../lib/status";
import { useApi } from "../lib/useApi";
import { PlanReview } from "../plan/PlanReview";
import { PublicationReview } from "../review/PublicationReview";

export const POLL_INTERVAL_MS = 5000;

export function TaskDetailPage() {
  const { taskId = "" } = useParams();
  const task = useApi(taskId, (signal) => api.getTask(taskId, signal));
  const status = task.data?.status;
  const reloadTask = task.reload;

  // Follow a running task until it settles or needs a person.
  useEffect(() => {
    if (!status || isSettled(status)) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reloadTask();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [status, reloadTask]);

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

  const detail = task.data;
  return (
    <div className="stack-lg">
      <BackLink />
      <TaskHeader task={detail} />
      <TaskNotices task={detail} />
      <PlanReview task={detail} onChanged={task.reload} />
      {detail.execution_target === "github" && (
        <PublicationReview taskId={detail.task_id} taskStatus={detail.status} onDecided={task.reload} />
      )}
      <Activity taskId={detail.task_id} status={detail.status} />
    </div>
  );
}

function BackLink() {
  return (
    <Link to="/" className="back-link">
      <ArrowLeftIcon /> All tasks
    </Link>
  );
}

function TaskHeader({ task }: { task: TaskDetail }) {
  return (
    <header className="task-header">
      <div className="task-header-top">
        <StatusBadge status={task.status} />
        {task.repository && <span className="task-header-repo">{task.repository}</span>}
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

function TaskNotices({ task }: { task: TaskDetail }) {
  return (
    <>
      {task.failure_reason && (
        <div className="notice notice-danger" role="alert">
          <strong>{task.failure_code ? `Failed: ${task.failure_code}` : "Failed"}</strong>
          <p>{task.failure_reason}</p>
        </div>
      )}
      {task.final_message && !task.failure_reason && <p className="notice notice-info">{task.final_message}</p>}
    </>
  );
}

function eventSummary(event: TaskEvent): string {
  if (event.from_status && event.to_status) {
    return `${statusInfo(event.from_status).label} → ${statusInfo(event.to_status).label}`;
  }
  if (event.to_status) return statusInfo(event.to_status).label;
  return event.event_type.replace(/_/g, " ").toLowerCase();
}

function Activity({ taskId, status }: { taskId: string; status: string }) {
  const { data, error, reload } = useApi(`${taskId}|${status}`, (signal) => api.getEvents(taskId, signal));

  return (
    <section className="card" aria-labelledby="activity-title">
      <header className="card-header">
        <h2 className="card-title" id="activity-title">
          Activity
        </h2>
      </header>
      {error && !data ? (
        <ErrorState title="Couldn't load activity" error={error} onRetry={reload} />
      ) : !data ? (
        <LoadingState label="Loading activity…" />
      ) : data.length === 0 ? (
        <EmptyState title="No activity recorded yet" />
      ) : (
        <ol className="timeline">
          {data.map((event) => (
            <li key={event.id} className="timeline-item">
              <span className="timeline-dot" aria-hidden="true" />
              <div className="timeline-body">
                <p className="timeline-title">{eventSummary(event)}</p>
                {event.message && <p className="timeline-message">{event.message}</p>}
                <p className="timeline-time" title={formatAbsolute(event.created_at)}>
                  {formatRelative(event.created_at)}
                </p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
