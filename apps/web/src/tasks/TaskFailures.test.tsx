import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { TaskDetail } from "../api/types";
import { mockApi, renderApp, signedIn, summary, taskDetail, USER } from "../test/helpers";

// The first real GitHub run failed twice: no git in the worker image, then no
// model credit.  A failed task must say why in words, keep the code and the
// server's reason visible, and never imply that code ran when it did not.

const TASK = "11111111-aaaa-bbbb-cccc-000000000001";
const PATH = `/tasks/${TASK}`;

beforeEach(() => signedIn());

function showTask(task: TaskDetail, executionMode?: string) {
  mockApi({
    "GET /auth/me": { body: USER },
    ...(executionMode
      ? { "GET /system/status": { body: { github_oauth: true, github_app: true, github_webhooks: true, local_execution: false,
            local_execution_trusted_accounts: 0, test_execution_mode: executionMode, session_secret: "configured" } } }
      : {}),
    [`GET ${PATH}`]: { body: task },
    [`GET ${PATH}/events`]: { body: [] },
    [`GET ${PATH}/publication`]: { status: 404, body: { detail: "No generated changes" } },
  }); // fmt: skip
  renderApp(PATH);
}

function failed(failure_code: string | null, failure_reason: string | null): TaskDetail {
  return taskDetail({ status: "FAILED", failure_code, failure_reason, completed_at: new Date().toISOString() });
}

describe("failure details", () => {
  it("explains quota exhaustion, keeping the code and the server's reason", async () => {
    const reason =
      "Planning failed: Planner agent failed: Error code: 429 - insufficient_quota (credit_balance_exhausted)";
    showTask(failed("MODEL_QUOTA_EXCEEDED", reason));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Failed: Model quota exceeded")).toBeInTheDocument();
    expect(within(alert).getByText(/no credit left or reached its billing limit/)).toBeInTheDocument();
    expect(within(alert).getByText("MODEL_QUOTA_EXCEEDED")).toBeInTheDocument();
    expect(within(alert).getByText(reason)).toBeInTheDocument();
  });

  it("explains a repository that could not be cloned", async () => {
    showTask(failed("REPOSITORY_ACQUISITION_FAILED", "Failed to acquire repository: git is not installed on this worker"));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Failed: Couldn't clone the repository")).toBeInTheDocument();
    expect(within(alert).getByText(/git is not installed on this worker/)).toBeInTheDocument();
  });

  it("shows an unknown code as it is", async () => {
    showTask(failed("SOMETHING_NEW", "A new kind of failure"));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Failed", { selector: "strong" })).toBeInTheDocument();
    expect(within(alert).getByText("SOMETHING_NEW")).toBeInTheDocument();
  });

  it("still explains a task that failed before failure codes existed", async () => {
    showTask(failed(null, "Planning failed: something"));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Failed", { selector: "strong" })).toBeInTheDocument();
    expect(within(alert).getByText("Planning failed: something")).toBeInTheDocument();
    expect(within(alert).queryByText("Code")).toBeNull();
  });

  it("says so when a failed task recorded no details", async () => {
    showTask(failed(null, null));
    expect(await screen.findByText("No failure details were recorded for this task.")).toBeInTheDocument();
  });

  it("renders the reason as text, keeping its line breaks", async () => {
    const reason = 'line one <img src=x onerror="alert(1)">\nline two';
    showTask(failed("WORKER_ERROR", reason));
    const alert = await screen.findByRole("alert");
    expect(alert.querySelector("img")).toBeNull();
    expect(within(alert).getByText((_, el) => el?.textContent === reason && el.tagName === "DD")).toBeInTheDocument();
  });

  it("never calls an execution-unavailable stop a failure, and says nothing ran", async () => {
    showTask(
      failed("EXECUTION_UNAVAILABLE", "Plan approved, but execution is not available on this server: Test execution is disabled."),
    );
    expect(await screen.findByText("Stopped before running: execution is not available on this server")).toBeInTheDocument();
    expect(screen.getByText(/stopped before any code was written. Nothing ran and nothing was published./)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("task list", () => {
  it("names the reason a task failed, and nothing for other tasks", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": {
        body: {
          total: 3, limit: 20, offset: 0,
          tasks: [
            summary({ task_id: "a-1", status: "FAILED", failure_code: "MODEL_QUOTA_EXCEEDED" }),
            summary({ task_id: "b-2", status: "FAILED" }), // older API: no failure_code field
            summary({ task_id: "c-3", status: "WAITING_APPROVAL", failure_code: null }),
          ],
        },
      },
    }); // fmt: skip
    renderApp("/");
    const rows = within(await screen.findByRole("list")).getAllByRole("link");
    expect(rows[0]).toHaveTextContent("Model quota exceeded");
    expect(rows[1]).toHaveTextContent("Failed");
    expect(rows[1]).not.toHaveTextContent("quota");
    expect(rows[2]).not.toHaveTextContent("quota");
  });
});

describe("approving while execution is disabled", () => {
  const plan = {
    goal: "Document the sandbox",
    summary: "Add a README section",
    risk_level: "low",
    steps: [{ step_number: 1, action: "modify", description: "Edit README.md", affected_paths: ["README.md"] }],
  };
  const waiting = () =>
    taskDetail({
      status: "WAITING_APPROVAL",
      implementation_plan: plan,
      latest_approval: {
        id: 3, approval_type: "plan", status: "PENDING", requested_by: "worker", approved_by: null, comment: null,
        requested_at: new Date().toISOString(), responded_at: null, expires_at: new Date(Date.now() + 3600_000).toISOString(),
      },
    }); // fmt: skip

  it("does not claim that code will be written", async () => {
    showTask(waiting(), "disabled");
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByText(/writes code and runs the tests/)).toBeNull();
    expect(within(dialog).queryByText(/come back here/)).toBeNull(); // nothing will be generated
    expect(
      within(dialog).getByText("no code is written, no tests run and nothing is published.", { selector: "strong" }),
    ).toBeInTheDocument();
  });

  it("describes the implementation when execution is available", async () => {
    showTask(waiting(), "docker");
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    expect(within(screen.getByRole("dialog")).getByText(/writes code and runs the tests/)).toBeInTheDocument();
  });
});
