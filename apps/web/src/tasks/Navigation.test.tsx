import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backTarget } from "../components/TaskLink";
import { POLL_INTERVAL_MS } from "../lib/usePolling";
import { activity, activityItem, mockApi, renderApp, signedIn, summary, taskDetail, USER } from "../test/helpers";

// A task's back link always went to the unfiltered task list, so reviewing
// tasks one by one from a filtered list (or from the overview) lost the
// filter, page and view every time.  Tabs all said "CodeForge", a failed
// refresh left stale data on screen without a word, and moving between
// pages left keyboard focus on a link that was no longer there.

const TASK = "11111111-aaaa-bbbb-cccc-000000000001";

beforeEach(() => signedIn());
afterEach(() => vi.useRealTimers());

const taskRoutes = (status = "WAITING_APPROVAL") => ({
  [`GET /tasks/${TASK}`]: { body: taskDetail({ task_id: TASK, status, execution_target: "local", repository: null }) },
  [`GET /tasks/${TASK}/activity`]: { body: activity() },
});

const list = { body: { total: 1, limit: 20, offset: 20, tasks: [summary({ task_id: TASK })] } };

describe("going back from a task", () => {
  it("returns to the filtered list page the task was opened from", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": list, ...taskRoutes() });
    renderApp("/?status=FAILED&scope=readable&page=2");
    await userEvent.click(await screen.findByRole("link", { name: /Fix the widget total calculation/ }));

    const back = await screen.findByRole("link", { name: "Back to tasks" });
    expect(back).toHaveAttribute("href", "/?status=FAILED&scope=readable&page=2");
    await userEvent.click(back);
    expect(await screen.findByRole("heading", { level: 1, name: /Tasks/ })).toBeInTheDocument();
  });

  it("returns to the overview when opened from there", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks/summary": {
        body: {
          total: 1,
          counts: { WAITING_APPROVAL: 1 },
          needs_attention_total: 1,
          needs_attention: [{ ...summary({ task_id: TASK, status: "WAITING_APPROVAL" }), attention: "plan_approval", updated_at: new Date().toISOString() }],
          failed_window_days: 14,
          recent_activity: [],
          repositories: [],
          repositories_truncated: false,
          generated_at: new Date().toISOString(),
        },
      },
      ...taskRoutes(),
    });
    renderApp("/overview");
    await userEvent.click(await screen.findByRole("link", { name: /Fix the widget total calculation/ }));
    expect(await screen.findByRole("link", { name: "Back to overview" })).toHaveAttribute("href", "/overview");
  });

  it("moves between a retry and the task it retries, and back", async () => {
    const CHILD = "22222222-aaaa-bbbb-cccc-000000000002";
    mockApi({
      "GET /auth/me": { body: USER },
      [`GET /tasks/${CHILD}`]: {
        body: taskDetail({ task_id: CHILD, status: "WAITING_APPROVAL", parent_task_id: TASK, requested_task: "Retry the upload fix" }),
      },
      [`GET /tasks/${CHILD}/activity`]: { body: activity() },
      [`GET /tasks/${CHILD}/publication`]: { status: 404, body: { detail: "none" } },
      [`GET /tasks/${TASK}`]: { body: taskDetail({ task_id: TASK, status: "FAILED", failure_code: "GITHUB_UNAVAILABLE" }) },
      [`GET /tasks/${TASK}/activity`]: {
        body: activity([activityItem({ kind: "retried", at: new Date().toISOString(), related_task_id: CHILD })]),
      },
      [`GET /tasks/${TASK}/publication`]: { status: 404, body: { detail: "none" } },
    });
    renderApp(`/tasks/${CHILD}`);
    await screen.findByRole("heading", { level: 1, name: "Retry the upload fix" });
    await userEvent.click(screen.getByRole("link", { name: "11111111" })); // "Retry of"

    expect(await screen.findByRole("heading", { level: 1, name: /Fix the widget total calculation/ })).toBeInTheDocument();
    const back = screen.getByRole("link", { name: "Back to previous task" });
    expect(back).toHaveAttribute("href", `/tasks/${CHILD}`);
    expect((await screen.findAllByRole("link", { name: "22222222" })).length).toBeGreaterThan(0); // its retries
    await userEvent.click(back);
    expect(await screen.findByRole("heading", { level: 1, name: "Retry the upload fix" })).toBeInTheDocument();
  });

  it("goes to all tasks when the page was opened directly", async () => {
    mockApi({ "GET /auth/me": { body: USER }, ...taskRoutes() });
    renderApp(`/tasks/${TASK}`);
    expect(await screen.findByRole("link", { name: "All tasks" })).toHaveAttribute("href", "/");
  });

  it("never follows a remembered location outside the app", () => {
    for (const from of ["https://evil.example/", "//evil.example/x", "javascript:alert(1)", 42, null, undefined]) {
      expect(backTarget({ from })).toEqual({ path: "/", label: "All tasks" });
    }
    expect(backTarget(null)).toEqual({ path: "/", label: "All tasks" });
    expect(backTarget({ from: "/tasks/abc" })).toEqual({ path: "/tasks/abc", label: "Back to previous task" });
    expect(backTarget({ from: "/" })).toEqual({ path: "/", label: "All tasks" });
  });
});

describe("a task that can't be opened", () => {
  it("says it was not found or is not readable, without offering a pointless retry", async () => {
    mockApi({ "GET /auth/me": { body: USER }, [`GET /tasks/${TASK}`]: { status: 404, body: { detail: "Task not found" } } });
    renderApp(`/tasks/${TASK}`);
    expect(await screen.findByRole("heading", { level: 1, name: "Task not found" })).toBeInTheDocument();
    expect(screen.getByText(/exists that you can read/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(screen.getByRole("link", { name: "All tasks" })).toHaveAttribute("href", "/");
    await waitFor(() => expect(document.title).toBe("Task not found · CodeForge"));
  });

  it("offers to try again when the server could not answer", async () => {
    let down = true;
    mockApi({
      "GET /auth/me": { body: USER },
      ...taskRoutes(),
      [`GET /tasks/${TASK}`]: () =>
        down ? { status: 503, body: { detail: "GitHub could not confirm your access right now." } } : { body: taskDetail({ task_id: TASK }) },
    });
    renderApp(`/tasks/${TASK}`);
    expect(await screen.findByRole("alert")).toHaveTextContent("GitHub could not confirm your access right now.");
    down = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { level: 1, name: /Fix the widget total calculation/ })).toBeInTheDocument();
  });
});

describe("page titles and focus", () => {
  it("names each page in the browser tab", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": list, ...taskRoutes() });
    renderApp("/");
    await waitFor(() => expect(document.title).toBe("Tasks · CodeForge"));
    await userEvent.click(await screen.findByRole("link", { name: /Fix the widget total calculation/ }));
    await waitFor(() => expect(document.title).toBe("Fix the widget total calculation · CodeForge"));
  });

  it("moves focus to the new page's content after navigating", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": list, ...taskRoutes() });
    renderApp("/");
    const row = await screen.findByRole("link", { name: /Fix the widget total calculation/ });
    expect(document.activeElement).not.toBe(document.getElementById("main")); // not on first load
    await userEvent.click(row);
    await screen.findByRole("link", { name: "All tasks" });
    expect(document.activeElement).toBe(document.getElementById("main"));
  });
});

describe("while a task page is open", () => {
  it("says so when a refresh fails, and keeps showing what was loaded", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let down = false;
    mockApi({
      "GET /auth/me": { body: USER },
      ...taskRoutes("PLANNING"),
      [`GET /tasks/${TASK}`]: () =>
        down ? { status: 503, body: { detail: "Service unavailable" } } : { body: taskDetail({ task_id: TASK, status: "PLANNING" }) },
    });
    renderApp(`/tasks/${TASK}`);
    expect(await screen.findByRole("heading", { level: 1, name: /Fix the widget total calculation/ })).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't refresh this task/)).toBeNull();

    down = true;
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + 10)));
    expect(await screen.findByText(/Couldn't refresh this task/)).toHaveTextContent("Showing what was last loaded");
    expect(screen.getByRole("heading", { level: 1, name: /Fix the widget total calculation/ })).toBeInTheDocument();

    down = false;
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + 10)));
    await waitFor(() => expect(screen.queryByText(/Couldn't refresh this task/)).toBeNull());
  });

  it("announces a status change to screen readers", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "PLANNING";
    mockApi({
      "GET /auth/me": { body: USER },
      ...taskRoutes(),
      [`GET /tasks/${TASK}`]: () => ({ body: taskDetail({ task_id: TASK, status, execution_target: "local", repository: null }) }),
    });
    renderApp(`/tasks/${TASK}`);
    await screen.findByRole("heading", { level: 1, name: /Fix the widget total calculation/ });
    const live = () => screen.getAllByRole("status").find((el) => el.classList.contains("visually-hidden"));
    expect(live()).toHaveTextContent(/^$/); // nothing read out on load

    status = "WAITING_APPROVAL";
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + 10)));
    await waitFor(() => expect(live()).toHaveTextContent("Status changed to Awaiting plan approval."));
  });
});
