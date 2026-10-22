import type { ReactNode } from "react";
import type { ActivityItem, TaskActivity } from "../api/types";
import { TaskLink } from "../components/TaskLink";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { formatAbsolute, formatRelative, githubOriginLabel, githubOriginUrl, shortId } from "../lib/format";
import { statusInfo, type Tone } from "../lib/status";

interface Props {
  repository: string | null;
  activity: TaskActivity | undefined;
  error: unknown;
  reload: () => void;
}

/** The task's recorded history, oldest first: what happened, when, and who acted. */
export function ActivityTimeline({ repository, activity, error, reload }: Props) {
  return (
    <section className="card" aria-labelledby="activity-title">
      <header className="card-header">
        <div>
          <h2 className="card-title" id="activity-title">
            Activity
          </h2>
          <p className="card-subtitle">Status changes, approvals and retries, as the server recorded them.</p>
        </div>
      </header>
      {error && !activity ? (
        <ErrorState title="Couldn't load activity" error={error} onRetry={reload} />
      ) : !activity ? (
        <LoadingState label="Loading activity…" />
      ) : (
        <>
          {activity.partial && (
            <p className="notice notice-info">
              <strong>Earlier history wasn't recorded.</strong> {activity.partial_reason}
            </p>
          )}
          {activity.items.length === 0 ? (
            <EmptyState title="No activity recorded yet" />
          ) : (
            <ol className="timeline">
              {activity.items.map((item, index) => (
                <TimelineEntry key={`${item.kind}-${item.at}-${index}`} item={item} repository={repository} />
              ))}
            </ol>
          )}
          {activity.truncated && (
            <p className="muted small timeline-footnote">
              Only the first status changes are shown; this task has more than the console displays.
            </p>
          )}
        </>
      )}
    </section>
  );
}

function TimelineEntry({ item, repository }: { item: ActivityItem; repository: string | null }) {
  const { title, tone, detail } = describe(item, repository);
  return (
    <li className={`timeline-item timeline-${tone}`}>
      <span className="timeline-dot" aria-hidden="true" />
      <div className="timeline-body">
        <p className="timeline-title">{title}</p>
        {detail && <p className="timeline-detail">{detail}</p>}
        {item.message && <p className="timeline-message">{item.message}</p>}
        <p className="timeline-time">
          <time dateTime={item.at} title={formatAbsolute(item.at)}>
            {formatRelative(item.at)}
          </time>
          {item.actor && <span> · @{item.actor}</span>}
        </p>
      </div>
    </li>
  );
}

const APPROVAL_NAME: Record<string, string> = { plan: "Plan", publication: "Publication" };

const DECISION: Record<string, { verb: string; tone: Tone }> = {
  APPROVED: { verb: "approved", tone: "success" },
  REJECTED: { verb: "rejected", tone: "neutral" },
  EXPIRED: { verb: "approval expired", tone: "neutral" },
};

function taskLink(taskId: string): ReactNode {
  return (
    <TaskLink taskId={taskId} className="mono">
      {shortId(taskId)}
    </TaskLink>
  );
}

function describe(item: ActivityItem, repository: string | null): { title: ReactNode; tone: Tone; detail: ReactNode } {
  switch (item.kind) {
    case "created": {
      const origin = item.github;
      const url = githubOriginUrl(repository, origin);
      return {
        title: "Task created",
        tone: "neutral",
        detail: (
          <>
            {origin &&
              (url ? (
                <>
                  From{" "}
                  <a href={url} target="_blank" rel="noreferrer noopener">
                    {githubOriginLabel(origin)}
                    <span className="visually-hidden"> (opens GitHub)</span>
                  </a>
                  .{" "}
                </>
              ) : (
                <>From {githubOriginLabel(origin)}. </>
              ))}
            {item.related_task_id && <>Retry of {taskLink(item.related_task_id)}.</>}
          </>
        ),
      };
    }
    case "status": {
      const to = item.to_status ? statusInfo(item.to_status) : null;
      const from = item.from_status ? statusInfo(item.from_status).label : null;
      return {
        title: from && to ? `${from} → ${to.label}` : (to?.label ?? "Status changed"),
        tone: to?.tone ?? "neutral",
        detail: null,
      };
    }
    case "approval_requested":
      return {
        title: `${APPROVAL_NAME[item.approval_type ?? ""] ?? "Approval"} approval requested`,
        tone: "warning",
        detail: item.approval_id !== null ? `Request #${item.approval_id}` : null,
      };
    case "approval_decided": {
      const name = APPROVAL_NAME[item.approval_type ?? ""] ?? "Approval";
      const decision = DECISION[item.approval_status ?? ""];
      return {
        title: decision ? `${name} ${decision.verb}` : `${name}: ${item.approval_status ?? "decided"}`,
        tone: decision?.tone ?? "neutral",
        detail: item.approval_id !== null ? `Request #${item.approval_id}` : null,
      };
    }
    case "retried":
      return {
        title: "Retried",
        tone: "info",
        detail: item.related_task_id ? <>New task {taskLink(item.related_task_id)}</> : null,
      };
    default:
      return { title: item.kind.replace(/_/g, " "), tone: "neutral", detail: null };
  }
}
