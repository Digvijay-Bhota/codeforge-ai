import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { ApprovalDetail, TaskDetail } from "../api/types";
import { mockApi, renderApp, signedIn, taskDetail, USER } from "../test/helpers";
import { activity } from "../test/helpers";
import { parsePlan } from "../lib/plan";
import { planDecision } from "./PlanReview";

const TASK = "11111111-aaaa-bbbb-cccc-000000000001";
const PATH = `/tasks/${TASK}`;

const PLAN = {
  goal: "Fix the rounding of discounted totals",
  summary: "Round once, after the discount",
  risk_level: "medium",
  steps: [
    {
      step_number: 1,
      action: "modify",
      description: "Compute the subtotal exactly",
      affected_paths: ["billing/totals.py"],
      rationale: "Per-line rounding loses cents",
      dependencies: [],
    },
    { step_number: 2, action: "test", description: "Add a regression test", affected_paths: ["tests/test_totals.py"], rationale: "Guard the fix", dependencies: [1] },
  ],
  affected_files: ["billing/totals.py", "tests/test_totals.py"],
  tests_needed: ["test_discount_rounds_once"],
  validation_strategy: "Run the billing tests",
  assumptions: ["Prices are Decimal"],
  risks: ["Totals change by a cent"],
};

function approval(overrides: Partial<ApprovalDetail> = {}): ApprovalDetail {
  return {
    id: 7,
    approval_type: "plan",
    status: "PENDING",
    requested_by: "worker",
    approved_by: null,
    comment: null,
    requested_at: new Date(Date.now() - 60_000).toISOString(),
    responded_at: null,
    expires_at: new Date(Date.now() + 5 * 3600_000).toISOString(),
    ...overrides,
  };
}

function waiting(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return taskDetail({ status: "WAITING_APPROVAL", implementation_plan: PLAN, latest_approval: approval(), ...overrides });
}

interface Setup {
  task?: TaskDetail;
  approve?: (body: unknown) => { status?: number; body?: unknown };
  reject?: (body: unknown) => { status?: number; body?: unknown };
  /** The server's test execution mode (GET /system/status); omitted: not reported. */
  executionMode?: string;
}

function setup({ task = waiting(), approve, reject, executionMode }: Setup = {}) {
  let current = task;
  const api = mockApi({
    "GET /auth/me": { body: USER },
    ...(executionMode
      ? { "GET /system/status": { body: { github_oauth: true, github_app: true, github_webhooks: true, local_execution: false,
            local_execution_trusted_accounts: 0, test_execution_mode: executionMode, session_secret: "configured" } } }
      : {}),
    [`GET ${PATH}`]: () => ({ body: current }),
    [`GET ${PATH}/activity`]: { body: activity() },
    [`GET ${PATH}/publication`]: { status: 404, body: { detail: "No generated changes" } },
    [`POST ${PATH}/approve`]: (call) => {
      const reply = approve?.(call.body);
      if (reply && (reply.status ?? 200) >= 400) {
        if (reply.status === 409) current = { ...current, status: "CANCELLED", latest_approval: approval({ status: "EXPIRED" }) };
        return reply;
      }
      current = { ...current, status: "QUEUED", latest_approval: approval({ status: "APPROVED", approved_by: "ada" }) };
      return { body: { task_id: TASK, status: "QUEUED", message: "Task plan approved and queued for execution" } };
    },
    [`POST ${PATH}/reject`]: (call) => {
      const reply = reject?.(call.body);
      if (reply && (reply.status ?? 200) >= 400) return reply;
      current = { ...current, status: "CANCELLED", latest_approval: approval({ status: "REJECTED", approved_by: "ada", comment: "Wrong file" }) };
      return { body: { task_id: TASK, status: "CANCELLED", message: "Task plan rejected and cancelled" } };
    },
  });
  renderApp(PATH);
  return api;
}

beforeEach(() => signedIn());

describe("plan review", () => {
  it.each([
    ["an older GitHub task", "github", "COMPLETED"],
    ["a local task", "local", "CODING"],
  ])("does not claim the approval rule applied to %s with no approval recorded", async (_, target, status) => {
    setup({ task: taskDetail({ status, execution_target: target, implementation_plan: PLAN, latest_approval: null }) });
    const panel = await screen.findByRole("region", { name: "Proposed plan" });
    expect(within(panel).getByText("No plan approval was recorded for this task.")).toBeInTheDocument();
    expect(within(panel).queryByText(/Nothing is implemented until/)).toBeNull();
  });

  it("states the rule while a plan can still be approved", async () => {
    setup();
    const panel = await screen.findByRole("region", { name: "Proposed plan" });
    expect(within(panel).getByText(/Nothing is implemented until someone with write access approves/)).toBeInTheDocument();
  });

  it("names the approval request a decision answers", async () => {
    setup();
    const panel = await screen.findByRole("region", { name: "Proposed plan" });
    expect(within(panel).getByText(/Approval request #7 · requested/)).toHaveTextContent(
      "A decision answers this request for this plan only",
    );
    await userEvent.click(within(panel).getByRole("button", { name: "Approve plan" }));
    expect(within(screen.getByRole("dialog")).getByText(/Approval request/)).toHaveTextContent("#7");
  });

  it("shows the proposed plan with a distinct status", async () => {
    setup();
    expect(await screen.findByRole("heading", { name: "Proposed plan" })).toBeInTheDocument();
    const panel = screen.getByRole("region", { name: "Proposed plan" });
    expect(within(panel).getByText("Awaiting plan approval", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getAllByText("Awaiting plan approval").length).toBeGreaterThanOrEqual(2); // header badge too
    expect(within(panel).getByText("Round once, after the discount")).toBeInTheDocument();
    expect(within(panel).getByText("Compute the subtotal exactly")).toBeInTheDocument();
    expect(within(panel).getByText("Per-line rounding loses cents")).toBeInTheDocument();
    expect(within(panel).getAllByText("billing/totals.py").length).toBe(2); // step path and affected files
    expect(within(panel).getByText("After step 1")).toBeInTheDocument();
    expect(within(panel).getByText("medium")).toBeInTheDocument();
    expect(within(panel).getByText("Run the billing tests")).toBeInTheDocument();
    expect(within(panel).getByText(/Expires in/)).toBeInTheDocument();
  });

  it("renders plan text as text, never markup", async () => {
    const payload = '<img src=x onerror="window.__planPwned=1">';
    setup({ task: waiting({ implementation_plan: { summary: payload, steps: [{ description: payload, rationale: payload, affected_paths: [payload] }] } }) });
    const panel = await screen.findByRole("region", { name: "Proposed plan" });
    expect(within(panel).getAllByText(payload).length).toBeGreaterThanOrEqual(3);
    expect(document.querySelector("img[src='x']")).toBeNull();
    expect((window as unknown as { __planPwned?: number }).__planPwned).toBeUndefined();
  });

  it("approves exactly the request shown, explains the consequences, and reloads", async () => {
    const api = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    const dialog = screen.getByRole("dialog", { name: "Approve this plan?" });
    expect(dialog).toHaveTextContent("nothing is published");
    expect(dialog).toHaveTextContent("acme/widgets");
    expect(dialog).toHaveTextContent(TASK);
    await userEvent.type(within(dialog).getByLabelText("Comment (optional)"), "Looks right");
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve plan" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", `${PATH}/approve`)[0]?.body).toEqual({ approval_id: 7, comment: "Looks right" });
    expect(await screen.findByText("Task plan approved and queued for execution")).toBeInTheDocument();
    expect(await screen.findByText("Approved by ada.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve plan" })).toBeNull();
    // A plan approval never touches publication.
    expect(api.calls.some((call) => call.path.includes("/publication/"))).toBe(false);
  });

  it("explains a local task's consequences", async () => {
    setup({ task: waiting({ execution_target: "local", repository: null, workspace_path: "/work/42/billing" }) });
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("/work/42/billing");
  });

  it("rejects with a reason", async () => {
    const api = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Reject plan" }));
    const dialog = screen.getByRole("dialog", { name: "Reject this plan?" });
    expect(dialog).toHaveTextContent("nothing is implemented");
    await userEvent.type(within(dialog).getByLabelText("Reason (optional)"), "Wrong file");
    await userEvent.click(within(dialog).getByRole("button", { name: "Reject plan" }));
    await waitFor(() => expect(api.callsTo("POST", `${PATH}/reject`)).toHaveLength(1));
    expect(api.callsTo("POST", `${PATH}/reject`)[0]?.body).toEqual({ approval_id: 7, reason: "Wrong file" });
    expect(await screen.findByText(/Rejected by ada\. Reason: Wrong file/)).toBeInTheDocument();
  });

  it("reloads instead of retrying when the server's state moved on", async () => {
    const api = setup({ approve: () => ({ status: 409, body: { detail: "Approval request has expired" } }) });
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    const before = api.callsTo("GET", PATH).length;
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve plan" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByText(/The task changed before your decision was recorded: Approval request has expired/)).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("GET", PATH).length).toBeGreaterThan(before));
    expect(await screen.findByText("Approval expired", { selector: ".plan .badge" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve plan" })).toBeNull();
  });

  it.each(["forbidden", "network"])("keeps the dialog open and explains a failure (%s)", async (kind) => {
    setup({ approve: () => ({ status: 403, body: { detail: "Write permission required for repository acme/widgets" } }) });
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    const dialog = screen.getByRole("dialog");
    if (kind === "network") {
      (globalThis.fetch as unknown as { mockRejectedValueOnce: (e: unknown) => void }).mockRejectedValueOnce(
        new TypeError("Failed to fetch"),
      );
    }
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve plan" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      kind === "network" ? "Can't reach the CodeForge API" : "Write permission required",
    );
    expect(within(dialog).getByRole("button", { name: "Approve plan" })).toBeEnabled();
  });

  it.each([
    [503, "GitHub could not confirm your write access to acme/widgets right now; nothing was changed. Try again shortly."],
    [500, "The CodeForge API had a problem. Try again in a moment."],
  ])("says nothing changed and allows another try when the server can't decide (%i)", async (status, text) => {
    let fail = true;
    const api = setup({
      approve: () => (fail ? { status, body: status === 503 ? { detail: text } : {} } : {}),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve plan" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(text);
    expect(screen.getByText("Awaiting plan approval", { selector: ".plan .badge" })).toBeInTheDocument();

    fail = false; // GitHub answers again
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve plan" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", `${PATH}/approve`)).toHaveLength(2);
    expect(api.callsTo("POST", `${PATH}/approve`).map((c) => c.body)).toEqual([
      { approval_id: 7, comment: null },
      { approval_id: 7, comment: null },
    ]);
  });

  it("sends an expired session to sign in, saying why, without recording a decision", async () => {
    setup({ approve: () => ({ status: 401, body: { detail: "Session expired" } }) });
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve plan" }));
    expect(await screen.findByText("Your session ended. Sign in again to continue.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("submits a decision once, however often it is clicked", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const api = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Approve plan" }));
    const confirm = within(screen.getByRole("dialog")).getByRole("button", { name: "Approve plan" });
    const fetchMock = globalThis.fetch as unknown as { mockImplementationOnce: (fn: unknown) => void };
    const original = globalThis.fetch;
    fetchMock.mockImplementationOnce(async (...args: unknown[]) => {
      await gate;
      return (original as (...a: unknown[]) => Promise<Response>)(...args);
    });
    await userEvent.dblClick(confirm);
    expect(confirm).toBeDisabled();
    release();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.callsTo("POST", `${PATH}/approve`).length).toBeLessThanOrEqual(1);
  });

  it.each([
    ["APPROVED", "Plan approved", "QUEUED"],
    ["REJECTED", "Plan rejected", "CANCELLED"],
    ["EXPIRED", "Approval expired", "CANCELLED"],
  ])("shows a %s request without decision buttons", async (status, label, taskStatus) => {
    setup({ task: waiting({ status: taskStatus, latest_approval: approval({ status, approved_by: "ada" }) }) });
    expect(await screen.findByText(label, { selector: ".plan .badge" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve plan" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject plan" })).toBeNull();
  });

  it("shows a rejected task as cancelled, not failed", async () => {
    setup({
      task: waiting({ status: "CANCELLED", failure_reason: "Prefer rounding in the renderer", latest_approval: approval({ status: "REJECTED", approved_by: "ada" }) }),
    });
    expect(await screen.findByText("Cancelled", { selector: ".notice strong" })).toBeInTheDocument();
    expect(screen.queryByText("Failed", { selector: ".notice strong" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers no decision once the request has expired on the clock", async () => {
    setup({ task: waiting({ latest_approval: approval({ expires_at: new Date(Date.now() - 60_000).toISOString() }) }) });
    expect(await screen.findByText("Approval expired", { selector: ".plan .badge" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve plan" })).toBeNull();
  });

  it("is absent for tasks without a plan or approval", async () => {
    setup({ task: taskDetail({ status: "COMPLETED" }) });
    await screen.findByRole("heading", { name: "Activity" });
    expect(screen.queryByRole("heading", { name: "Proposed plan" })).toBeNull();
  });

  it("lets the list filter tasks awaiting plan approval", async () => {
    const api = mockApi({ "GET /auth/me": { body: USER }, "GET /tasks": { body: { total: 0, limit: 20, offset: 0, tasks: [] } } });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Plan approval" }));
    await waitFor(() => expect(api.callsTo("GET", "/tasks").at(-1)?.query.get("status")).toBe("WAITING_APPROVAL"));
  });
});

describe("execution availability", () => {
  it("warns before approval when execution is disabled on the server", async () => {
    setup({ executionMode: "disabled" });
    expect(await screen.findByText(/approving records your decision, but the task stops before any code is written/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    expect(within(screen.getByRole("dialog")).getByText(/Execution is disabled on this server/)).toBeInTheDocument();
  });

  it("says nothing extra when execution is available", async () => {
    setup({ executionMode: "docker" });
    expect(await screen.findByText("Approving starts the implementation. Publication is reviewed separately.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    expect(within(screen.getByRole("dialog")).queryByText(/Execution is disabled/)).toBeNull();
  });

  it("explains an approved plan that stopped because execution is unavailable", async () => {
    setup({
      task: waiting({
        status: "FAILED",
        failure_code: "EXECUTION_UNAVAILABLE",
        failure_reason: "Plan approved, but execution is not available on this server: Test execution is disabled. Nothing was run.",
        latest_approval: approval({ status: "APPROVED", approved_by: "ada" }),
      }),
    });
    expect(await screen.findByText("Stopped before running: execution is not available on this server")).toBeInTheDocument();
    expect(screen.getByText(/Nothing was run/)).toBeInTheDocument();
    expect(screen.queryByText(/^Failed/, { selector: ".notice strong" })).toBeNull();
    expect(screen.getByText("Plan approved", { selector: ".plan .badge" })).toBeInTheDocument();
  });
});

describe("planDecision", () => {
  const now = Date.parse("2026-10-21T12:00:00Z");
  const base = waiting({ latest_approval: approval({ expires_at: "2026-10-21T13:00:00Z" }) });

  it("allows a decision only on a pending, unexpired request while the task waits", () => {
    expect(planDecision(base, now).approvalId).toBe(7);
    expect(planDecision({ ...base, status: "QUEUED" }, now).approvalId).toBeNull();
    expect(planDecision(waiting({ latest_approval: approval({ expires_at: "2026-10-21T11:00:00Z" }) }), now).approvalId).toBeNull();
    expect(planDecision({ ...base, latest_approval: null }, now).approvalId).toBeNull();
    for (const status of ["APPROVED", "REJECTED", "EXPIRED"]) {
      expect(planDecision(waiting({ latest_approval: approval({ status }) }), now).approvalId).toBeNull();
    }
  });
});

describe("parsePlan", () => {
  it("keeps well-typed fields and drops the rest", () => {
    const plan = parsePlan({
      summary: 42,
      goal: "Fix it",
      steps: [{ description: "Do it", affected_paths: ["a.py", 5], dependencies: [1, "2"] }, { nothing: true }, "bad"],
      affected_files: "not a list",
    });
    expect(plan?.goal).toBe("Fix it");
    expect(plan?.summary).toBeNull();
    expect(plan?.steps).toEqual([
      { number: null, action: null, description: "Do it", rationale: null, affectedPaths: ["a.py"], dependencies: [1] },
    ]);
    expect(plan?.affectedFiles).toEqual([]);
  });

  it("accepts the worker's fallback shape and rejects empty input", () => {
    expect(parsePlan({ steps: [{ step: 3, description: "Only step" }] })?.steps[0]?.number).toBe(3);
    for (const raw of [null, [], "plan", {}, { steps: "x" }]) expect(parsePlan(raw)).toBeNull();
  });
});
