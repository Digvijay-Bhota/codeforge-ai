import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { TaskActivity, TaskDetail } from "../api/types";
import { activity, activityItem, mockApi, renderApp, signedIn, taskDetail, USER } from "../test/helpers";
import { POLL_INTERVAL_MS } from "../lib/usePolling";
import { retryRefusal } from "./RetryTask";

beforeEach(() => signedIn());

const TASK = "11111111-aaaa-bbbb-cccc-000000000001";
const RETRY = "33333333-aaaa-bbbb-cccc-000000000003";
const PATH = `/tasks/${TASK}`;

function open(task: TaskDetail, history: TaskActivity, extra: Record<string, unknown> = {}) {
  return mockApi({
    "GET /auth/me": { body: USER },
    [`GET ${PATH}`]: { body: task },
    [`GET ${PATH}/activity`]: { body: history },
    [`GET ${PATH}/publication`]: { status: 404, body: { detail: "none" } },
    "GET /system/status": { body: { test_execution_mode: "disabled" } },
    ...extra,
  });
}

const at = (minutes: number) => new Date(Date.UTC(2026, 9, 20, 10, minutes)).toISOString();

const failedHistory = activity([
  activityItem({ kind: "created", at: at(0), to_status: "PENDING", actor: "octo", github: { issue_number: 2, pull_request_number: null, triggered_by: "octo" } }),
  activityItem({ kind: "status", at: at(1), from_status: "PENDING", to_status: "QUEUED" }),
  activityItem({ kind: "status", at: at(2), from_status: "ANALYZING", to_status: "PLANNING" }),
  activityItem({ kind: "status", at: at(3), from_status: "PLANNING", to_status: "FAILED", message: "GitHub returned 502" }),
  activityItem({ kind: "retried", at: at(9), related_task_id: RETRY }),
]);

describe("task activity", () => {
  it("shows the recorded history with links, actors and approval requests", async () => {
    const history = activity([
      activityItem({ kind: "created", at: at(0), to_status: "PENDING", actor: "octo", github: { issue_number: 2, pull_request_number: null, triggered_by: "octo" } }),
      activityItem({ kind: "status", at: at(2), from_status: "PLANNING", to_status: "WAITING_APPROVAL" }),
      activityItem({ kind: "approval_requested", at: at(2), approval_id: 12, approval_type: "plan", approval_status: "PENDING" }),
      activityItem({ kind: "approval_decided", at: at(5), approval_id: 12, approval_type: "plan", approval_status: "REJECTED", actor: "ada", message: "Too broad" }),
      activityItem({ kind: "status", at: at(5), from_status: "WAITING_APPROVAL", to_status: "CANCELLED" }),
    ]);
    open(taskDetail({ status: "CANCELLED", failure_reason: "Plan rejected" }), history);
    renderApp(PATH);

    const section = await screen.findByRole("region", { name: "Activity" });
    const entries = await within(section).findAllByRole("listitem");
    expect(entries.map((entry) => within(entry).getAllByText(/.+/)[0]?.textContent)).toEqual([
      "Task created",
      "Planning → Awaiting plan approval",
      "Plan approval requested",
      "Plan rejected",
      "Awaiting plan approval → Cancelled",
    ]);
    expect(within(entries[0]!).getByRole("link", { name: /Issue #2/ })).toHaveAttribute(
      "href",
      "https://github.com/acme/widgets/issues/2",
    );
    expect(entries[2]).toHaveTextContent("Request #12");
    expect(entries[3]).toHaveTextContent("Too broad");
    expect(entries[3]).toHaveTextContent("@ada");
    expect(within(entries[1]!).getByText(/ago|now|on|at|\d/, { selector: "time" })).toHaveAttribute("datetime", at(2));

    // The decision stopped the plan-approval step.
    const progress = screen.getByRole("group", { name: "Workflow progress" });
    expect(within(progress).getByText("Plan approval").closest("li")).toHaveClass("is-stopped");
  });

  it("says when earlier history was not recorded, or was cut short", async () => {
    open(
      taskDetail({ status: "COMPLETED" }),
      activity([activityItem({ kind: "status", at: at(3), from_status: "TESTING", to_status: "COMPLETED" })], {
        partial: true,
        partial_reason: "Status changes before 10:03 were not recorded.",
        truncated: true,
      }),
    );
    renderApp(PATH);
    expect(await screen.findByText("Earlier history wasn't recorded.")).toBeInTheDocument();
    expect(screen.getByText(/Status changes before 10:03 were not recorded/)).toBeInTheDocument();
    expect(screen.getByText(/this task has more than the console displays/)).toBeInTheDocument();

    // Completed, but nothing records a plan approval, a publication review or a pull request.
    const progress = screen.getByRole("group", { name: "Workflow progress" });
    for (const step of ["Plan approval", "Publication review", "Pull request"]) {
      expect(within(progress).getByText(step).closest("li")).toHaveTextContent("not recorded");
    }
    // Completing required the code to run; that much the status itself records.
    expect(within(progress).getByText("Code & tests").closest("li")).toHaveTextContent("done");
    expect(within(progress).getByText(/have no record that they happened/)).toBeInTheDocument();
  });

  it("keeps the task readable when its activity can't be loaded", async () => {
    open(taskDetail({ status: "WAITING_PUBLICATION" }), activity(), {
      [`GET ${PATH}/activity`]: { status: 503, body: { detail: "Database unavailable" } },
    });
    renderApp(PATH);
    expect(await screen.findByText("Couldn't load activity")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Fix the widget total calculation");
  });
});

describe("following a running task", () => {
  afterEach(() => vi.useRealTimers());

  it("reloads the history when the status changes, keeping the old one on screen meanwhile", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status = "PLANNING";
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const first = activity([activityItem({ kind: "status", at: at(1), from_status: "ANALYZING", to_status: "PLANNING" })]);
    const second = activity([
      ...first.items,
      activityItem({ kind: "status", at: at(2), from_status: "PLANNING", to_status: "WAITING_APPROVAL" }),
    ]);
    const api = open(taskDetail({ status }), first, {
      [`GET ${PATH}`]: () => ({ body: taskDetail({ status }) }),
      [`GET ${PATH}/activity`]: () => ({ body: status === "PLANNING" ? first : second }),
    });
    const original = api.fetchMock.getMockImplementation()!;
    let activityRequests = 0;
    api.fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/activity")) {
        activityRequests += 1;
        if (status !== "PLANNING") await gate;
      }
      return original(input, init);
    });
    renderApp(PATH);
    const section = await screen.findByRole("region", { name: "Activity" });
    expect(await within(section).findByText("Analyzing → Planning")).toBeInTheDocument();
    expect(activityRequests).toBe(1);

    status = "WAITING_APPROVAL";
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    await waitFor(() => expect(activityRequests).toBe(2));
    // Still loading the new history: the old one stays.
    expect(within(section).getByText("Analyzing → Planning")).toBeInTheDocument();
    expect(within(section).queryByText("Loading activity…")).toBeNull();
    release();
    expect(await within(section).findByText("Planning → Awaiting plan approval")).toBeInTheDocument();

    // Polling a settled task stops, and nothing reloads without a status change.
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(activityRequests).toBe(2);
  });
});

describe("failure recovery", () => {
  it("names the failed step, says whether retrying helps, and links the existing retry", async () => {
    open(
      taskDetail({ status: "FAILED", failure_code: "MODEL_AUTHENTICATION_FAILED", failure_reason: "401 from the provider" }),
      failedHistory,
    );
    renderApp(PATH);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Failed: Model API key refused");
    expect(await within(alert).findByText("Stopped during: Plan")).toBeInTheDocument();
    expect(alert).toHaveTextContent("Retrying won't help until the cause above is fixed.");
    expect(screen.getByText(/Already retried as/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "33333333" })[0]).toHaveAttribute("href", `/tasks/${RETRY}`);
    expect(within(screen.getByRole("group", { name: "Workflow progress" })).getByText("Plan").closest("li")).toHaveClass(
      "is-failed",
    );
  });

  it("calls an outage temporary without repeating the advice", async () => {
    open(taskDetail({ status: "FAILED", failure_code: "MODEL_UNAVAILABLE", failure_reason: "timeout" }), failedHistory);
    renderApp(PATH);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Model unavailable");
    // The advice already says "Retry later"; no second retry sentence.
    expect(alert).not.toHaveTextContent("This looks temporary");
  });

  it("explains a refused retry and keeps the dialog open", async () => {
    open(taskDetail({ status: "FAILED", failure_code: "GITHUB_UNAVAILABLE" }), failedHistory, {
      [`POST ${PATH}/retry`]: { status: 403, body: { detail: "Write permission required for repository acme/widgets" } },
    });
    renderApp(PATH);
    await userEvent.click(await screen.findByRole("button", { name: "Retry task" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Retry task" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Write permission required for repository acme/widgets. Only people with write access to the repository can retry its tasks.",
    );
  });
});

describe("retry refusals", () => {
  it.each([
    [409, "Cannot retry task x with active status 'PLANNING'.", /Reload the page to see the task's current state\.$/],
    [503, "GitHub could not confirm your write access to acme/widgets right now; nothing was changed.", /nothing was changed\.$/],
    [404, "Task not found", /no longer exists, or you can no longer see it/],
    [0, "Can't reach the CodeForge API.", /may not have reached the server: check the task list/],
  ])("explains a %s", (status, message, expected) => {
    expect(retryRefusal(new ApiError(status, message))).toMatch(expected);
  });
});
