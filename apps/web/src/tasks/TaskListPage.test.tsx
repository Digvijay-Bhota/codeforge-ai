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

    await userEvent.type(screen.getByLabelText("Repository"), "acme/widgets");
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("repository")).toBe("acme/widgets"));
    expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("status")).toBe("WAITING_PUBLICATION");
  });

  it("rejects a malformed repository without calling the API", async () => {
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": page([summary()]) });
    renderApp("/");
    await screen.findByRole("list");
    const before = api.callsTo("GET", "/tasks").length;
    await userEvent.type(screen.getByLabelText("Repository"), "not a repo");
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByText("Use the form owner/repository.")).toBeInTheDocument();
    expect(screen.getByLabelText("Repository")).toHaveAttribute("aria-invalid", "true");
    expect(api.callsTo("GET", "/tasks")).toHaveLength(before);
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
