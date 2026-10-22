import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskDetail } from "../api/types";
import { POLL_INTERVAL_MS } from "../lib/usePolling";
import { mockApi, renderApp, signedIn, summary, taskDetail, USER } from "../test/helpers";
import { activity } from "../test/helpers";

// The console told people to "retry the task" (failure explanations, reject
// dialogs) but offered no way to do it, and the task list never refreshed, so
// a task stayed "Planning" there while its own page had moved on.

const FAILED = "11111111-aaaa-bbbb-cccc-000000000001";
const CHILD = "22222222-aaaa-bbbb-cccc-000000000002";

beforeEach(() => signedIn());
afterEach(() => vi.useRealTimers());

function routesFor(task: TaskDetail, extra: Record<string, unknown> = {}) {
  const path = `/tasks/${task.task_id}`;
  return {
    [`GET ${path}`]: { body: task },
    [`GET ${path}/activity`]: { body: activity() },
    [`GET ${path}/publication`]: { status: 404, body: { detail: "none" } },
    ...extra,
  };
}

const failed = () =>
  taskDetail({ task_id: FAILED, status: "FAILED", failure_code: "GITHUB_UNAVAILABLE", failure_reason: "GitHub is unavailable" });

describe("retrying a task", () => {
  it("creates a new task that plans again, and opens it", async () => {
    const child = taskDetail({ task_id: CHILD, status: "PENDING", parent_task_id: FAILED });
    const api = mockApi({
      "GET /auth/me": { body: USER },
      ...routesFor(failed()),
      ...routesFor(child),
      [`POST /tasks/${FAILED}/retry`]: { body: { task_id: CHILD, parent_task_id: FAILED, status: "PENDING" } },
    });
    renderApp(`/tasks/${FAILED}`);
    await userEvent.click(await screen.findByRole("button", { name: "Retry task" }));

    const dialog = screen.getByRole("dialog", { name: "Retry this task?" });
    expect(within(dialog).getByText(/plans again, and the new plan waits for approval/)).toBeInTheDocument();
    expect(within(dialog).getByText(/this task keeps its status and history/)).toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText("Extra instructions (optional)"), "GitHub is back");
    await userEvent.click(within(dialog).getByRole("button", { name: "Retry task" }));

    expect(await screen.findByRole("link", { name: "11111111" })).toHaveAttribute("href", `/tasks/${FAILED}`); // "Retry of"
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.callsTo("POST", `/tasks/${FAILED}/retry`).map((c) => c.body)).toEqual([
      { additional_instructions: "GitHub is back" },
    ]);
  });

  it("submits once, however often it is clicked", async () => {
    let release: () => void = () => undefined;
    const api = mockApi({
      "GET /auth/me": { body: USER },
      ...routesFor(failed()),
      ...routesFor(taskDetail({ task_id: CHILD, status: "PENDING" })),
      [`POST /tasks/${FAILED}/retry`]: () => ({ body: { task_id: CHILD, parent_task_id: FAILED, status: "PENDING" } }),
    });
    const gate = new Promise<void>((resolve) => (release = resolve));
    const original = api.fetchMock.getMockImplementation()!;
    api.fetchMock.mockImplementation(async (input, init) => {
      if (init?.method === "POST") await gate;
      return original(input, init);
    });
    renderApp(`/tasks/${FAILED}`);
    await userEvent.click(await screen.findByRole("button", { name: "Retry task" }));
    const submit = within(screen.getByRole("dialog")).getByRole("button", { name: "Retry task" });
    await userEvent.click(submit);
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Creating…" }));
    release();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", `/tasks/${FAILED}/retry`)).toHaveLength(1);
  });

  it("shows the server's refusal and stays on the task", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      ...routesFor(failed()),
      [`POST /tasks/${FAILED}/retry`]: { status: 403, body: { detail: "Write permission required for repository acme/widgets" } },
    });
    renderApp(`/tasks/${FAILED}`);
    await userEvent.click(await screen.findByRole("button", { name: "Retry task" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Retry task" }));
    expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("Write permission required");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Fix the widget total calculation");
  });

  it.each(["WAITING_APPROVAL", "PLANNING", "COMPLETED"])("is not offered for a %s task", async (status) => {
    mockApi({ "GET /auth/me": { body: USER }, ...routesFor(taskDetail({ task_id: FAILED, status })) });
    renderApp(`/tasks/${FAILED}`);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("button", { name: "Retry task" })).toBeNull();
  });
});

describe("task list refresh", () => {
  it("follows running tasks until they settle, then stops", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "PLANNING";
    const api = mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": () => ({ body: { total: 1, limit: 20, offset: 0, tasks: [summary({ status })] } }),
    });
    renderApp("/");
    const list = await screen.findByRole("list");
    expect(within(list).getByText("Planning")).toBeInTheDocument();
    const initial = api.callsTo("GET", "/tasks").length;

    status = "WAITING_APPROVAL";
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(await within(screen.getByRole("list")).findByText("Awaiting plan approval")).toBeInTheDocument();
    expect(api.callsTo("GET", "/tasks")).toHaveLength(initial + 1);

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(api.callsTo("GET", "/tasks")).toHaveLength(initial + 1); // everything settled: no more requests
  });

  it("does not poll a list with nothing running", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": { body: { total: 1, limit: 20, offset: 0, tasks: [summary({ status: "FAILED" })] } },
    });
    renderApp("/");
    await screen.findByRole("list");
    const initial = api.callsTo("GET", "/tasks").length;
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(api.callsTo("GET", "/tasks")).toHaveLength(initial);
  });
});
