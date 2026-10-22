import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { mockApi, renderApp, signedIn, summary, USER } from "../test/helpers";

const page = (tasks: ReturnType<typeof summary>[], total = tasks.length, offset = 0) => ({
  body: { total, limit: 20, offset, tasks },
});

beforeEach(() => signedIn());

describe("task list", () => {
  it("lists tasks with their status and links to each", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": page([summary(), summary({ task_id: "22222222-x", status: "COMPLETED", requested_task: "Add docs" })]),
    });
    renderApp("/");
    const list = await screen.findByRole("list");
    const rows = within(list).getAllByRole("link");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Fix the widget total calculation");
    expect(rows[0]).toHaveTextContent("Awaiting review");
    expect(rows[0]).toHaveAttribute("href", "/tasks/11111111-aaaa-bbbb-cccc-000000000001");
    expect(rows[1]).toHaveTextContent("Completed");
  });

  it("filters by status and by repository through the API", async () => {
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([summary()]) });
    renderApp("/");
    await screen.findByRole("list");

    await userEvent.click(screen.getByRole("button", { name: "Awaiting review" }));
    await waitFor(() => expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("status")).toBe("WAITING_PUBLICATION"));
    expect(screen.getByRole("button", { name: "Awaiting review" })).toHaveAttribute("aria-pressed", "true");

    await userEvent.type(screen.getByLabelText("Search"), "acme/widgets");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("repository")).toBe("acme/widgets"));
    expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("status")).toBe("WAITING_PUBLICATION");
    expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("q")).toBeNull();
  });

  it("searches the tasks shown for any other text, keeping the view and status", async () => {
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([summary()]) });
    renderApp("/?scope=readable&status=FAILED&page=3");
    await screen.findByRole("list");
    await userEvent.type(screen.getByLabelText("Search"), "  flaky upload  ");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("q")).toBe("flaky upload"));
    const last = api.callsTo("GET", "/tasks").at(-1)!.query;
    expect(last.get("scope")).toBe("readable");
    expect(last.get("status")).toBe("FAILED");
    expect(last.get("offset")).toBe("0"); // a new search starts at the first page
    expect(last.get("repository")).toBeNull();
    expect(screen.getByLabelText("Search")).toHaveAttribute("maxlength", "200");
  });

  it("says what was searched for when nothing matches, and clears the search", async () => {
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": (call) => page(call.query.get("q") ? [] : [summary()]) });
    renderApp("/?q=nothing%20here");
    expect(await screen.findByText("No tasks match “nothing here”")).toBeInTheDocument();
    expect(screen.getByLabelText("Search")).toHaveValue("nothing here");
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await screen.findByRole("list");
    expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("q")).toBeNull();
    expect(screen.getByLabelText("Search")).toHaveValue("");
  });

  it("shows when each task last changed and which task it retries", async () => {
    const created = new Date(Date.now() - 3 * 3600_000).toISOString();
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": page([
        summary({ created_at: created, updated_at: new Date(Date.now() - 2 * 60_000).toISOString(), parent_task_id: "99999999-aaaa-bbbb-cccc-000000000009" }),
        summary({ task_id: "22222222-x", created_at: created, updated_at: created, requested_task: "Untouched" }),
      ]),
    });
    renderApp("/");
    const [changed, untouched] = within(await screen.findByRole("list")).getAllByRole("link");
    expect(changed).toHaveTextContent("Updated 2 minutes ago");
    expect(changed).toHaveTextContent("Retry of 99999999");
    expect(untouched).toHaveTextContent("Created 3 hours ago");
    expect(untouched).not.toHaveTextContent("Retry of");
  });

  it("pages through results", async () => {
    const api = mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": (call) => page([summary()], 45, Number(call.query.get("offset") ?? 0)),
    });
    renderApp("/");
    expect(await screen.findByText("1–1 of 45")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("offset")).toBe("20"));
  });

  it("filters by groups of statuses and clears every filter", async () => {
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([summary()]) });
    renderApp("/?repository=acme/widgets");
    await screen.findByRole("list");

    await userEvent.click(screen.getByRole("button", { name: "Awaiting a decision" }));
    await waitFor(() =>
      expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("status")).toBe("WAITING_APPROVAL,WAITING_PUBLICATION"),
    );
    expect(screen.getByLabelText("Status")).toHaveDisplayValue("Awaiting a decision");

    await userEvent.click(screen.getByRole("button", { name: "In progress" }));
    await waitFor(() =>
      expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("status")).toBe(
        "PENDING,QUEUED,ANALYZING,PLANNING,CODING,TESTING,PUBLISHING",
      ),
    );

    // Results are showing, and the filters can still be cleared in one step.
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/tasks").at(-1)!;
      expect(last.query.get("status")).toBeNull();
      expect(last.query.get("repository")).toBeNull();
    });
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });

  it("lists every task you can read, keeping that choice when filters are cleared", async () => {
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([summary()]) });
    renderApp("/");
    await screen.findByRole("list");
    expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("scope")).toBeNull();
    expect(screen.getByRole("button", { name: "My tasks" })).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(screen.getByRole("button", { name: "All I can read" }));
    await waitFor(() => expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("scope")).toBe("readable"));
    expect(screen.getByText(/Every task you can read: your own/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Failed" }));
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/tasks").at(-1)!;
      expect(last.query.get("status")).toBeNull();
      expect(last.query.get("scope")).toBe("readable");
    });
  });

  it("lets a repository filter decide the scope", async () => {
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([summary()]) });
    renderApp("/?scope=readable&repository=acme/widgets");
    await screen.findByRole("list");
    const last = api.callsTo("GET", "/tasks").at(-1)!;
    expect(last.query.get("repository")).toBe("acme/widgets");
    expect(last.query.get("scope")).toBeNull();
    expect(screen.queryByRole("group", { name: "Show" })).toBeNull();
  });

  it("says when tasks may be missing from the list of everything you can read", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": { body: { total: 1, limit: 20, offset: 0, tasks: [summary()], repositories_unconfirmed: true } },
    });
    renderApp("/?scope=readable");
    expect(await screen.findByText(/couldn't confirm your access to some repositories/)).toBeInTheDocument();
  });

  it("shows a helpful empty state", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([]) });
    renderApp("/");
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
  });

  it("offers to clear filters when nothing matches", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([]) });
    renderApp("/?status=FAILED");
    expect(await screen.findByText("No tasks match these filters")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
  });

  it("explains an error and retries", async () => {
    let fail = true;
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": () => (fail ? { status: 403, body: { detail: "Access denied to repository acme/secret" } } : page([summary()])),
    });
    renderApp("/?repository=acme/secret");
    expect(await screen.findByRole("alert")).toHaveTextContent("Access denied to repository acme/secret");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list")).toBeInTheDocument();
  });
});

describe("configuration notice", () => {
  const status = (github_app: boolean) => ({
    body: {
      github_oauth: true,
      github_app,
      github_webhooks: false,
      local_execution: false,
      local_execution_trusted_accounts: 0,
      test_execution_mode: "disabled",
      session_secret: "configured",
    },
  });

  it("explains that repository access isn't configured", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([]), "GET /system/status": status(false) });
    renderApp("/");
    expect(await screen.findByText(/Repository access isn't configured on this server/)).toBeInTheDocument();
  });

  it("stays quiet when the GitHub App is configured or the status is unavailable", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([]), "GET /system/status": status(true) });
    renderApp("/");
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
    expect(screen.queryByText(/Repository access isn't configured/)).toBeNull();
  });

  it("does not break the list when the status request fails", async () => {
    mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([summary()]), "GET /system/status": { status: 500 } });
    renderApp("/");
    expect(await screen.findByRole("list")).toBeInTheDocument();
    expect(screen.queryByText(/Repository access isn't configured/)).toBeNull();
  });
});
