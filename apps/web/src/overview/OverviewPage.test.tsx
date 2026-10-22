import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { TaskOverview } from "../api/types";
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
        updated_at: new Date(Date.now() - 3600_000).toISOString(),
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

    const recent = screen.getByRole("region", { name: "Recent activity" });
    expect(within(recent).getByText("Planning → Awaiting plan approval")).toBeInTheDocument();

    const table = screen.getByRole("table");
    expect(within(table).getByRole("row", { name: /Completed 4/ })).toBeInTheDocument();
    expect(within(table).queryByText("Cancelled")).toBeNull(); // only statuses with tasks

    expect(screen.getByRole("link", { name: "acme/widgets" })).toHaveAttribute("href", "/?repository=acme%2Fwidgets");
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
