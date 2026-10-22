import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskOverview } from "../api/types";
import { POLL_INTERVAL_MS } from "../lib/usePolling";
import { mockApi, renderApp, signedIn, summary, USER } from "../test/helpers";

beforeEach(() => signedIn());

const PLAN = "11111111-aaaa-bbbb-cccc-000000000001";
const FAILED = "22222222-aaaa-bbbb-cccc-000000000002";

function overview(overrides: Partial<TaskOverview> = {}): TaskOverview {
  return {
    total: 9,
    counts: { WAITING_APPROVAL: 1, CODING: 2, COMPLETED: 4, FAILED: 2 },
    needs_attention_total: 2,
    needs_attention: [
      {
        ...summary({ task_id: PLAN, status: "WAITING_APPROVAL", requested_task: "Add retries to the client" }),
        attention: "plan_approval",
        updated_at: new Date(Date.now() - 60_000).toISOString(),
      },
      {
        ...summary({ task_id: FAILED, status: "FAILED", failure_code: "GITHUB_UNAVAILABLE", requested_task: "Fix the build" }),
        attention: "failed",
        completed_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
        updated_at: new Date(Date.now() - 60_000).toISOString(),
      },
    ],
    failed_window_days: 14,
    recent_activity: [
      {
        task_id: PLAN,
        repository: "acme/widgets",
        requested_task: "Add retries to the client",
        from_status: "PLANNING",
        to_status: "WAITING_APPROVAL",
        at: new Date(Date.now() - 60_000).toISOString(),
      },
    ],
    repositories: ["acme/widgets"],
    repositories_truncated: false,
    generated_at: new Date().toISOString(),
    ...overrides,
  };
}

describe("overview", () => {
  it("shows exact counts, what needs a person, and recent activity", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks/summary": { body: overview() } });
    renderApp("/overview");

    const totals = await screen.findByRole("region", { name: "Totals" });
    const stat = (label: string) => within(totals).getByText(label).parentElement!;
    expect(stat("Needs attention")).toHaveTextContent("2");
    expect(stat("Awaiting a decision")).toHaveTextContent("1");
    expect(stat("In progress")).toHaveTextContent("2");
    expect(stat("Completed")).toHaveTextContent("4");
    expect(stat("Failed")).toHaveTextContent("2");

    const attention = screen.getByRole("region", { name: "Needs attention" });
    const links = within(attention).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([`/tasks/${PLAN}`, `/tasks/${FAILED}`]);
    expect(links[0]).toHaveTextContent("Plan approval");
    expect(links[1]).toHaveTextContent("Failed: GitHub unavailable");
    expect(links[1]).toHaveTextContent("Failed 2 hours ago"); // when it failed, not when the row changed
    expect(links[0]).toHaveTextContent("Updated 1 minute ago");

    const recent = screen.getByRole("region", { name: "Recent activity" });
    expect(within(recent).getByText("Planning → Awaiting plan approval")).toBeInTheDocument();

    const table = screen.getByRole("table");
    expect(within(table).getByRole("row", { name: /Completed 4/ })).toBeInTheDocument();
    expect(within(table).queryByText("Cancelled")).toBeNull(); // only statuses with tasks

    expect(screen.getByRole("link", { name: "acme/widgets" })).toHaveAttribute("href", "/?repository=acme%2Fwidgets");

    // Each number but "Needs attention" opens the list it counts.
    const href = (label: string) => within(totals).getByText(label).closest("a")?.getAttribute("href");
    expect(href("Needs attention")).toBeUndefined();
    expect(href("Awaiting a decision")).toBe("/?scope=readable&status=WAITING_APPROVAL%2CWAITING_PUBLICATION");
    expect(href("In progress")).toBe(
      "/?scope=readable&status=PENDING%2CQUEUED%2CANALYZING%2CPLANNING%2CCODING%2CTESTING%2CPUBLISHING",
    );
    expect(href("Completed")).toBe("/?scope=readable&status=COMPLETED");
    expect(href("Failed")).toBe("/?scope=readable&status=FAILED");
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
  });

  it("says when the list is longer than shown", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks/summary": { body: overview({ needs_attention_total: 31 }) } });
    renderApp("/overview");
    expect(await screen.findByText("Showing 2 of 31.")).toBeInTheDocument();
  });

  it("says when repositories were left out", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks/summary": { body: overview({ repositories_truncated: true }) },
    });
    renderApp("/overview");
    expect(await screen.findByText(/Not every registered repository was checked/)).toBeInTheDocument();
  });

  it("says when GitHub could not confirm some repositories", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks/summary": { body: overview({ repositories_unconfirmed: true }) },
    });
    renderApp("/overview");
    expect(await screen.findByText(/GitHub couldn't confirm your access to some repositories just now/)).toBeInTheDocument();
  });

  it("does not call an account empty without saying what was left out", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks/summary": {
        body: overview({ total: 0, counts: {}, needs_attention_total: 0, needs_attention: [], recent_activity: [],
          repositories: [], repositories_unconfirmed: true }),
      },
    });
    renderApp("/overview");
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
    expect(screen.getByText(/couldn't confirm your access to some repositories/)).toBeInTheDocument();
  });

  it("has an empty state", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks/summary": {
        body: overview({ total: 0, counts: {}, needs_attention_total: 0, needs_attention: [], recent_activity: [] }),
      },
    });
    renderApp("/overview");
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Totals" })).toBeNull();
  });

  it("explains an error and tries again", async () => {
    let fail = true;
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks/summary": () => (fail ? { status: 500, body: {} } : { body: overview() }),
    });
    renderApp("/overview");
    expect(await screen.findByText("Couldn't load the overview")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("region", { name: "Totals" })).toBeInTheDocument();
  });

  it("opens the matching list from a number", async () => {
    const api = mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks/summary": { body: overview() },
      "GET /tasks": { body: { total: 2, limit: 20, offset: 0, tasks: [summary({ status: "FAILED" })] } },
      "GET /system/status": { body: { github_app: true } },
    });
    renderApp("/overview");
    const totals = await screen.findByRole("region", { name: "Totals" });
    await userEvent.click(within(totals).getByText("Failed"));
    expect(await screen.findByRole("heading", { name: "Tasks" })).toBeInTheDocument();
    const call = api.callsTo("GET", "/tasks").at(-1)!;
    expect([call.query.get("scope"), call.query.get("status")]).toEqual(["readable", "FAILED"]);
    expect(screen.getByRole("button", { name: "Failed" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "All I can read" })).toHaveAttribute("aria-pressed", "true");
  });

  it("never sends a poll while the previous one is unanswered", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let release: () => void = () => undefined;
    let hang = false;
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks/summary": { body: overview() } });
    const original = api.fetchMock.getMockImplementation()!;
    let sent = 0;
    api.fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/tasks/summary")) {
        sent += 1;
        if (hang) await new Promise<void>((r) => (release = r));
      }
      return original(input, init);
    });
    renderApp("/overview"); // two tasks in progress: the page polls
    await screen.findByRole("region", { name: "Totals" });
    expect(sent).toBe(1);

    hang = true; // the API stops answering (a GitHub outage behind it)
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 4);
    expect(sent).toBe(2); // one request waiting, no pile-up behind it

    hang = false;
    release();
    await waitFor(() => expect(api.callsTo("GET", "/tasks/summary")).toHaveLength(2)); // answered
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    await waitFor(() => expect(sent).toBe(3)); // polling resumed
    vi.useRealTimers();
  });

  it("is reachable from the navigation", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": { body: { total: 0, limit: 20, offset: 0, tasks: [] } },
      "GET /system/status": { body: { github_app: true } },
      "GET /tasks/summary": { body: overview() },
    });
    renderApp("/");
    expect(screen.getByRole("link", { name: "Tasks" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("link", { name: "Overview" }));
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });
});
