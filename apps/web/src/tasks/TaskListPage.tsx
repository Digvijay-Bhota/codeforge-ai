import { useEffect, useId, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/endpoints";
import { TASK_STATUSES, type TaskSummary } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { TaskLink, taskPath, useFromState } from "../components/TaskLink";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { explainFailure } from "../lib/failures";
import { formatAbsolute, formatRelative, githubOriginLabel, shortId, taskCommand, taskTitle } from "../lib/format";
import { isSettled, STATUS_GROUPS, statusInfo } from "../lib/status";
import { useApi } from "../lib/useApi";
import { usePageTitle } from "../lib/usePageTitle";
import { usePolling } from "../lib/usePolling";

export const PAGE_SIZE = 20;
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
// Task IDs are UUIDs (the acknowledgement comment on GitHub shows one).
const TASK_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// As the API's limit for ?q=.
const SEARCH_MAX_LENGTH = 200;
const NEEDS_A_PERSON = new Set(["WAITING_APPROVAL", "WAITING_PUBLICATION"]);

const GROUPS = Object.values(STATUS_GROUPS);

export function TaskListPage() {
  usePageTitle("Tasks");
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const repository = params.get("repository") ?? "";
  const q = params.get("q") ?? "";
  // "readable": every task the user can read; otherwise their own.  A repository filter wins.
  const readable = params.get("scope") === "readable" && !repository;
  const page = Math.max(1, Number.parseInt(params.get("page") ?? "1", 10) || 1);
  const offset = (page - 1) * PAGE_SIZE;

  const { data, error, loading, reload, refresh } = useApi(`${status}|${repository}|${q}|${readable}|${offset}`, (signal) =>
    api.listTasks(
      {
        status: status || null,
        repository: repository || null,
        scope: readable ? "readable" : null,
        q: q || null,
        limit: PAGE_SIZE,
        offset,
      },
      signal,
    ),
  );
  // Keep running tasks' statuses current, as the task page does; stops once all have settled.
  usePolling(Boolean(data?.tasks.some((task) => !isSettled(task.status))), refresh);

  function update(next: Record<string, string>) {
    const merged = new URLSearchParams(params);
    for (const [key, value] of Object.entries(next)) {
      if (value) merged.set(key, value);
      else merged.delete(key);
    }
    if (!("page" in next)) merged.delete("page");
    setParams(merged);
  }

  const total = data?.total ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const filtered = Boolean(status || repository || q);

  return (
    <div className="stack-lg">
      <div className="page-header">
        <div>
          <h1 className="page-title">Tasks</h1>
          <p className="page-subtitle">
            {repository ? (
              <>
                Every task in <strong>{repository}</strong> you can read.
              </>
            ) : readable ? (
              "Every task you can read: your own, and every task in the repositories you have access to."
            ) : (
              "Tasks you created. Show all you can read, or enter a repository, to see everyone's tasks, including changes waiting for your review."
            )}
          </p>
        </div>
        {!repository && (
          <div className="segmented" role="group" aria-label="Show">
            <button type="button" aria-pressed={!readable} onClick={() => update({ scope: "" })}>
              My tasks
            </button>
            <button type="button" aria-pressed={readable} onClick={() => update({ scope: "readable" })}>
              All I can read
            </button>
          </div>
        )}
      </div>

      <ConfigurationNotice />

      <Filters
        status={status}
        repository={repository}
        search={q}
        onStatus={(value) => update({ status: value })}
        onSearch={(next) => update(next)}
        onClear={filtered ? () => update({ status: "", repository: "", q: "" }) : undefined}
      />

      {readable && (data?.repositories_truncated || data?.repositories_unconfirmed) && (
        <p className="notice notice-warning" role="status">
          {data.repositories_unconfirmed
            ? "GitHub couldn't confirm your access to some repositories just now, so their tasks are not listed. Reload in a moment."
            : "More repositories are registered than the server checks, so some tasks are not listed. Enter a repository to see its tasks."}
        </p>
      )}

      <section aria-label="Task list" aria-busy={loading}>
        {error && !data ? (
          <ErrorState title="Couldn't load tasks" error={error} onRetry={reload} />
        ) : !data ? (
          <LoadingState label="Loading tasks…" />
        ) : data.tasks.length === 0 ? (
          <EmptyState title={q ? `No tasks match “${q}”` : filtered ? "No tasks match these filters" : "No tasks yet"}>
            {filtered ? (
              <p>Clear the filters to see every task.</p>
            ) : (
              <p>
                Comment <code>@codeforge</code> on an issue or pull request in a connected repository to start one.
              </p>
            )}
          </EmptyState>
        ) : (
          <>
            {error && (
              <p className="notice notice-danger" role="alert">
                Couldn't refresh the list. Showing the last results.
              </p>
            )}
            <ul className="task-list" role="list">
              {data.tasks.map((task) => (
                <li key={task.task_id} className={NEEDS_A_PERSON.has(task.status) ? "task-row is-attention" : "task-row"}>
                  <TaskLink taskId={task.task_id} className="task-row-link">
                    <span className="task-row-main">
                      <span className="task-row-title">{taskTitle(task.requested_task)}</span>
                      <span className="task-row-meta">
                        <span className="mono">{shortId(task.task_id)}</span>
                        {task.repository && <span>{task.repository}</span>}
                        {task.github && (
                          <span>
                            {githubOriginLabel(task.github)} by @{task.github.triggered_by}
                          </span>
                        )}
                        {taskCommand(task.requested_task) && (
                          <span className="mono">{taskCommand(task.requested_task)}</span>
                        )}
                        <UpdatedAt task={task} />
                        {task.parent_task_id && (
                          <span>
                            Retry of <span className="mono">{shortId(task.parent_task_id)}</span>
                          </span>
                        )}
                        {task.status === "FAILED" && task.failure_code && (
                          <span className="task-row-failure">{explainFailure(task.failure_code).title}</span>
                        )}
                      </span>
                    </span>
                    <span className="task-row-status">
                      <StatusBadge status={task.status} />
                    </span>
                  </TaskLink>
                </li>
              ))}
            </ul>
            <nav className="pagination" aria-label="Pagination">
              <span className="pagination-summary">
                {offset + 1}–{offset + data.tasks.length} of {total}
              </span>
              <div className="pagination-buttons">
                <button
                  type="button"
                  className="button button-secondary button-small"
                  disabled={page <= 1}
                  onClick={() => update({ page: String(page - 1) })}
                >
                  Previous
                </button>
                <button
                  type="button"
                  className="button button-secondary button-small"
                  disabled={page >= lastPage}
                  onClick={() => update({ page: String(page + 1) })}
                >
                  Next
                </button>
              </div>
            </nav>
          </>
        )}
      </section>
    </div>
  );
}

/** When the task last changed; its creation time if the server did not say. */
function UpdatedAt({ task }: { task: TaskSummary }) {
  const changed = task.updated_at && task.updated_at !== task.created_at ? task.updated_at : null;
  const at = changed ?? task.created_at;
  return (
    <span title={`Created ${formatAbsolute(task.created_at)}${changed ? `, updated ${formatAbsolute(changed)}` : ""}`}>
      {changed ? "Updated" : "Created"} {formatRelative(at)}
    </span>
  );
}

interface FiltersProps {
  status: string;
  repository: string;
  search: string;
  onStatus: (value: string) => void;
  /** A repository filter or a text search; setting one clears the other. */
  onSearch: (next: { repository: string; q: string }) => void;
  /** Present while any filter is set. */
  onClear?: () => void;
}

function Filters({ status, repository, search, onStatus, onSearch, onClear }: FiltersProps) {
  const navigate = useNavigate();
  const from = useFromState();
  const statusId = useId();
  const repositoryId = useId();
  const hintId = useId();
  const [draft, setDraft] = useState(repository || search);

  useEffect(() => setDraft(repository || search), [repository, search]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    if (TASK_ID.test(value)) {
      navigate(taskPath(value.toLowerCase()), { state: from });
      return;
    }
    // owner/repository lists that repository's tasks (everyone's, if you can read it);
    // anything else searches the tasks shown.
    if (REPOSITORY.test(value)) onSearch({ repository: value, q: "" });
    else onSearch({ repository: "", q: value });
  }

  return (
    <div className="filters">
      <div className="chip-group" role="group" aria-label="Quick filters">
        <button type="button" className="chip" aria-pressed={status === ""} onClick={() => onStatus("")}>
          All
        </button>
        {GROUPS.map((group) => (
          <button
            key={group.value}
            type="button"
            className="chip"
            aria-pressed={status === group.value}
            onClick={() => onStatus(group.value)}
          >
            {group.label}
          </button>
        ))}
        {onClear && (
          <button type="button" className="button button-ghost button-small filters-clear" onClick={onClear}>
            Clear filters
          </button>
        )}
      </div>
      <div className="filters-fields">
        <div className="field">
          <label htmlFor={statusId}>Status</label>
          <select id={statusId} value={status} onChange={(event) => onStatus(event.target.value)}>
            <option value="">Any status</option>
            {status.includes(",") && (
              <option value={status}>
                {GROUPS.find((group) => group.value === status)?.label ?? "Several statuses"}
              </option>
            )}
            {TASK_STATUSES.map((value) => (
              <option key={value} value={value}>
                {statusInfo(value).label}
              </option>
            ))}
          </select>
        </div>
        <form className="field field-grow" onSubmit={submit} role="search">
          <label htmlFor={repositoryId}>Search</label>
          <div className="input-row">
            <input
              id={repositoryId}
              type="search"
              autoComplete="off"
              spellCheck={false}
              maxLength={SEARCH_MAX_LENGTH}
              placeholder="Words, owner/repository or task ID"
              value={draft}
              aria-describedby={hintId}
              onChange={(event) => setDraft(event.target.value)}
            />
            <button type="submit" className="button button-secondary">
              Search
            </button>
          </div>
          <p className="field-hint" id={hintId}>
            Words from the request, an owner/repository to list its tasks, or a full task ID to open it.
          </p>
        </form>
      </div>
    </div>
  );
}

/** Explains refusals that come from server configuration, not permissions. */
function ConfigurationNotice() {
  const { data } = useApi("system-status", (signal) => api.systemStatus(signal));
  if (!data || data.github_app) return null;
  return (
    <p className="notice notice-warning" role="status">
      Repository access isn't configured on this server: the GitHub App credentials are not set, so repository
      tasks can't be listed, reviewed or run. See "GitHub App" in docs/deployment.md.
    </p>
  );
}
