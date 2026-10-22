import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { TASK_STATUSES, type AttentionItem, type TaskOverview } from "../api/types";
import { Badge } from "../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { explainFailure } from "../lib/failures";
import { formatAbsolute, formatRelative, githubOriginLabel, shortId, taskTitle } from "../lib/format";
import { isSettled, STATUS_GROUPS, statusInfo, taskListPath, type Tone } from "../lib/status";
import { useApi } from "../lib/useApi";
import { usePolling } from "../lib/usePolling";

const IN_PROGRESS = TASK_STATUSES.filter((status) => !isSettled(status));

function sum(counts: Record<string, number>, statuses: readonly string[]): number {
  return statuses.reduce((total, status) => total + (counts[status] ?? 0), 0);
}

/** Counts, what is waiting on a person, and recent activity, over every task the user may read. */
export function OverviewPage() {
  const { data, error, loading, reload } = useApi("overview", (signal) => api.overview(signal));
  // Keep the numbers current while something is running; stops once everything has settled.
  usePolling(Boolean(data && sum(data.counts, IN_PROGRESS) > 0), reload);

  return (
    <div className="stack-lg">
      <div className="page-header">
        <div>
          <h1 className="page-title">Overview</h1>
          <p className="page-subtitle">
            Every task you can read: your own, and every task in the repositories you have access to.
          </p>
        </div>
      </div>
      <div aria-busy={loading} className="stack-lg">
        {error && !data ? (
          <ErrorState title="Couldn't load the overview" error={error} onRetry={reload} />
        ) : !data ? (
          <LoadingState label="Loading overview…" />
        ) : (
          <>
            {error && (
              <p className="notice notice-danger" role="alert">
                Couldn't refresh the overview. Showing the last results.
              </p>
            )}
            <Overview data={data} />
          </>
        )}
      </div>
    </div>
  );
}

function Overview({ data }: { data: TaskOverview }) {
  if (data.total === 0) {
    return (
      <>
        <UnverifiedNotice data={data} />
        <EmptyState title="No tasks yet">
          <p>
            Comment <code>@codeforge</code> on an issue or pull request in a connected repository to start one.
          </p>
        </EmptyState>
      </>
    );
  }
  const counts = data.counts;
  const decisions = sum(counts, ["WAITING_APPROVAL", "WAITING_PUBLICATION"]);
  return (
    <>
      <section aria-label="Totals" className="stat-grid">
        <Stat label="Needs attention" value={data.needs_attention_total} tone={data.needs_attention_total ? "warning" : "neutral"}>
          Decisions, and failures from the last {data.failed_window_days} days not yet retried
        </Stat>
        <Stat
          label="Awaiting a decision"
          value={decisions}
          tone={decisions ? "warning" : "neutral"}
          to={taskListPath({ scope: "readable", status: STATUS_GROUPS.awaitingDecision.value })}
        >
          Plan approvals and publication reviews
        </Stat>
        <Stat
          label="In progress"
          value={sum(counts, IN_PROGRESS)}
          tone="info"
          to={taskListPath({ scope: "readable", status: STATUS_GROUPS.inProgress.value })}
        >
          Queued, planning, coding, testing or publishing
        </Stat>
        <Stat
          label="Completed"
          value={counts.COMPLETED ?? 0}
          tone="success"
          to={taskListPath({ scope: "readable", status: "COMPLETED" })}
        >
          Finished successfully
        </Stat>
        <Stat
          label="Failed"
          value={counts.FAILED ?? 0}
          tone={counts.FAILED ? "danger" : "neutral"}
          to={taskListPath({ scope: "readable", status: STATUS_GROUPS.failed.value })}
        >
          All time, including tasks already retried
        </Stat>
      </section>

      <div className="overview-grid">
        <AttentionList data={data} />
        <RecentActivity data={data} />
      </div>

      <div className="overview-grid">
        <StatusTable data={data} />
        <Repositories data={data} />
      </div>

      <p className="muted small">
        Counted <time dateTime={data.generated_at}>{formatAbsolute(data.generated_at)}</time>, over {data.total}{" "}
        {data.total === 1 ? "task" : "tasks"}.
      </p>
    </>
  );
}

/** A headline number; with *to*, a link to the task list it counts (same scope, same statuses). */
function Stat({
  label,
  value,
  tone,
  to,
  children,
}: {
  label: string;
  value: number;
  tone: Tone;
  to?: string;
  children: ReactNode;
}) {
  const body = (
    <>
      <p className="stat-label">{label}</p>
      <p className="stat-value">{value.toLocaleString()}</p>
      <p className="stat-hint">{children}</p>
    </>
  );
  return to ? (
    <Link to={to} className={`stat stat-${tone} stat-link`}>
      {body}
    </Link>
  ) : (
    <div className={`stat stat-${tone}`}>{body}</div>
  );
}

const ATTENTION: Record<string, { label: string; tone: Tone }> = {
  plan_approval: { label: "Plan approval", tone: "warning" },
  publication_review: { label: "Publication review", tone: "warning" },
};

function attentionBadge(item: AttentionItem): { label: string; tone: Tone } {
  return ATTENTION[item.attention] ?? { label: `Failed: ${explainFailure(item.failure_code).title}`, tone: "danger" };
}

/** When a failed task failed; for a decision, when the task last changed. */
function AttentionTime({ item }: { item: AttentionItem }) {
  const failed = item.attention === "failed";
  const at = failed ? (item.completed_at ?? item.updated_at) : item.updated_at;
  return (
    <span title={formatAbsolute(at)}>
      {failed ? "Failed" : "Updated"} {formatRelative(at)}
    </span>
  );
}

function AttentionList({ data }: { data: TaskOverview }) {
  const shown = data.needs_attention.length;
  return (
    <section className="card" aria-labelledby="attention-title">
      <header className="card-header">
        <div>
          <h2 className="card-title" id="attention-title">
            Needs attention
          </h2>
          <p className="card-subtitle">Decisions first, then the latest failures.</p>
        </div>
      </header>
      {shown === 0 ? (
        <p className="muted">Nothing is waiting on a person.</p>
      ) : (
        <>
          <ul className="compact-list" role="list">
            {data.needs_attention.map((item) => {
              const badge = attentionBadge(item);
              return (
                <li key={item.task_id} className="compact-row">
                  <Link to={`/tasks/${encodeURIComponent(item.task_id)}`} className="compact-row-link">
                    <span className="compact-row-title">{taskTitle(item.requested_task)}</span>
                    <span className="compact-row-meta">
                      <Badge tone={badge.tone}>{badge.label}</Badge>
                      <span>{item.repository ?? "Local task"}</span>
                      {item.github && <span>{githubOriginLabel(item.github)}</span>}
                      <AttentionTime item={item} />
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
          {data.needs_attention_total > shown && (
            <p className="muted small">
              Showing {shown} of {data.needs_attention_total}.
            </p>
          )}
        </>
      )}
    </section>
  );
}

function RecentActivity({ data }: { data: TaskOverview }) {
  return (
    <section className="card" aria-labelledby="recent-title">
      <header className="card-header">
        <div>
          <h2 className="card-title" id="recent-title">
            Recent activity
          </h2>
          <p className="card-subtitle">The latest recorded status changes.</p>
        </div>
      </header>
      {data.recent_activity.length === 0 ? (
        <p className="muted">No status changes recorded yet.</p>
      ) : (
        <ol className="timeline">
          {data.recent_activity.map((entry, index) => {
            const to = entry.to_status ? statusInfo(entry.to_status) : null;
            return (
              <li key={`${entry.task_id}-${entry.at}-${index}`} className={`timeline-item timeline-${to?.tone ?? "neutral"}`}>
                <span className="timeline-dot" aria-hidden="true" />
                <div className="timeline-body">
                  <p className="timeline-title">
                    <Link to={`/tasks/${encodeURIComponent(entry.task_id)}`}>{taskTitle(entry.requested_task)}</Link>
                  </p>
                  <p className="timeline-detail">
                    {entry.from_status ? `${statusInfo(entry.from_status).label} → ` : ""}
                    {to?.label ?? "Status changed"}
                  </p>
                  <p className="timeline-time">
                    <time dateTime={entry.at} title={formatAbsolute(entry.at)}>
                      {formatRelative(entry.at)}
                    </time>
                    {" · "}
                    <span className="mono">{shortId(entry.task_id)}</span>
                    {entry.repository && ` · ${entry.repository}`}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

/** Per-status counts as a table: twelve classes are too many to tell apart by colour. */
function StatusTable({ data }: { data: TaskOverview }) {
  const rows = TASK_STATUSES.filter((status) => (data.counts[status] ?? 0) > 0);
  const max = Math.max(1, ...rows.map((status) => data.counts[status] ?? 0));
  return (
    <section className="card" aria-labelledby="status-title">
      <header className="card-header">
        <h2 className="card-title" id="status-title">
          Tasks by status
        </h2>
      </header>
      <table className="status-table">
        <thead>
          <tr>
            <th scope="col">Status</th>
            <th scope="col" className="numeric">
              Tasks
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((status) => {
            const count = data.counts[status] ?? 0;
            return (
              <tr key={status}>
                <th scope="row">{statusInfo(status).label}</th>
                <td className="numeric">
                  <span className="status-bar" aria-hidden="true">
                    <span style={{ width: `${(count / max) * 100}%` }} />
                  </span>
                  <span className="status-count">{count.toLocaleString()}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function Repositories({ data }: { data: TaskOverview }) {
  return (
    <section className="card" aria-labelledby="repositories-title">
      <header className="card-header">
        <div>
          <h2 className="card-title" id="repositories-title">
            Repositories
          </h2>
          <p className="card-subtitle">Registered repositories you can read. Open one to see everyone's tasks in it.</p>
        </div>
      </header>
      {data.repositories.length === 0 ? (
        <p className="muted">None. Only your own tasks are counted.</p>
      ) : (
        <ul className="repository-list" role="list">
          {data.repositories.map((repository) => (
            <li key={repository}>
              <Link to={`/?repository=${encodeURIComponent(repository)}`} className="mono">
                {repository}
              </Link>
            </li>
          ))}
        </ul>
      )}
      <UnverifiedNotice data={data} />
      {data.repositories_truncated && (
        <p className="notice notice-warning">
          Not every registered repository was checked, so tasks in the others are not counted here. Enter a
          repository on the Tasks page to see its tasks.
        </p>
      )}
    </section>
  );
}

/** Repositories left out because GitHub didn't answer: the counts may be lower than they are. */
function UnverifiedNotice({ data }: { data: TaskOverview }) {
  if (!data.repositories_unconfirmed) return null;
  return (
    <p className="notice notice-warning" role="status">
      GitHub couldn't confirm your access to some repositories just now, so their tasks are not counted. Reload in a
      moment.
    </p>
  );
}
