import { useEffect, useId, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/endpoints";
import { TASK_STATUSES } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/StateViews";
import { formatAbsolute, formatRelative, shortId, taskTitle } from "../lib/format";
import { statusInfo } from "../lib/status";
import { useApi } from "../lib/useApi";

export const PAGE_SIZE = 20;
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const NEEDS_A_PERSON = new Set(["WAITING_APPROVAL", "WAITING_PUBLICATION"]);

export function TaskListPage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const repository = params.get("repository") ?? "";
  const page = Math.max(1, Number.parseInt(params.get("page") ?? "1", 10) || 1);
  const offset = (page - 1) * PAGE_SIZE;

  const { data, error, loading, reload } = useApi(`${status}|${repository}|${offset}`, (signal) =>
    api.listTasks({ status: status || null, repository: repository || null, limit: PAGE_SIZE, offset }, signal),
  );

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
  const filtered = Boolean(status || repository);

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
            ) : (
              "Tasks you created. Enter a repository to see everyone's tasks in it, including changes waiting for your review."
            )}
          </p>
        </div>
      </div>

      <Filters
        status={status}
        repository={repository}
        onStatus={(value) => update({ status: value })}
        onRepository={(value) => update({ repository: value })}
      />

      <section aria-label="Task list" aria-busy={loading}>
        {error && !data ? (
          <ErrorState title="Couldn't load tasks" error={error} onRetry={reload} />
        ) : !data ? (
          <LoadingState label="Loading tasks…" />
        ) : data.tasks.length === 0 ? (
          <EmptyState title={filtered ? "No tasks match these filters" : "No tasks yet"}>
            {filtered ? (
              <button type="button" className="button button-secondary" onClick={() => setParams(new URLSearchParams())}>
                Clear filters
              </button>
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
                  <Link to={`/tasks/${encodeURIComponent(task.task_id)}`} className="task-row-link">
                    <span className="task-row-main">
                      <span className="task-row-title">{taskTitle(task.requested_task)}</span>
                      <span className="task-row-meta">
                        <span className="mono">{shortId(task.task_id)}</span>
                        {task.repository && <span>{task.repository}</span>}
                        <span title={formatAbsolute(task.created_at)}>Created {formatRelative(task.created_at)}</span>
                      </span>
                    </span>
                    <span className="task-row-status">
                      <StatusBadge status={task.status} />
                    </span>
                  </Link>
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

interface FiltersProps {
  status: string;
  repository: string;
  onStatus: (value: string) => void;
  onRepository: (value: string) => void;
}

function Filters({ status, repository, onStatus, onRepository }: FiltersProps) {
  const statusId = useId();
  const repositoryId = useId();
  const hintId = useId();
  const [draft, setDraft] = useState(repository);
  const [invalid, setInvalid] = useState(false);

  useEffect(() => setDraft(repository), [repository]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    if (value && !REPOSITORY.test(value)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    onRepository(value);
  }

  return (
    <div className="filters">
      <div className="chip-group" role="group" aria-label="Quick filters">
        <button type="button" className="chip" aria-pressed={status === ""} onClick={() => onStatus("")}>
          All
        </button>
        <button
          type="button"
          className="chip"
          aria-pressed={status === "WAITING_APPROVAL"}
          onClick={() => onStatus("WAITING_APPROVAL")}
        >
          Plan approval
        </button>
        <button
          type="button"
          className="chip"
          aria-pressed={status === "WAITING_PUBLICATION"}
          onClick={() => onStatus("WAITING_PUBLICATION")}
        >
          Awaiting review
        </button>
        <button type="button" className="chip" aria-pressed={status === "FAILED"} onClick={() => onStatus("FAILED")}>
          Failed
        </button>
      </div>
      <div className="filters-fields">
        <div className="field">
          <label htmlFor={statusId}>Status</label>
          <select id={statusId} value={status} onChange={(event) => onStatus(event.target.value)}>
            <option value="">Any status</option>
            {TASK_STATUSES.map((value) => (
              <option key={value} value={value}>
                {statusInfo(value).label}
              </option>
            ))}
          </select>
        </div>
        <form className="field field-grow" onSubmit={submit} role="search">
          <label htmlFor={repositoryId}>Repository</label>
          <div className="input-row">
            <input
              id={repositoryId}
              type="text"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              placeholder="owner/repository"
              value={draft}
              aria-invalid={invalid || undefined}
              aria-describedby={invalid ? hintId : undefined}
              onChange={(event) => setDraft(event.target.value)}
            />
            <button type="submit" className="button button-secondary">
              Apply
            </button>
          </div>
          {invalid && (
            <p className="field-error" id={hintId}>
              Use the form owner/repository.
            </p>
          )}
        </form>
      </div>
    </div>
  );
}
