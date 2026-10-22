import { cleanup, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { TaskDetail } from "../api/types";
import { activity, mockApi, renderApp, signedIn, taskDetail, USER } from "../test/helpers";

// A completed task's page said "Completed" in a badge and nothing about what
// it produced; the pull request was only inside the publication card.

const TASK = "11111111-aaaa-bbbb-cccc-000000000001";

beforeEach(() => signedIn());

function open(task: TaskDetail) {
  mockApi({
    "GET /auth/me": { body: USER },
    [`GET /tasks/${TASK}`]: { body: task },
    [`GET /tasks/${TASK}/activity`]: { body: activity() },
    [`GET /tasks/${TASK}/publication`]: { status: 404, body: { detail: "none" } },
  });
  renderApp(`/tasks/${TASK}`);
}

describe("a completed task's outcome", () => {
  it("links the pull request it opened", async () => {
    open(
      taskDetail({
        status: "COMPLETED",
        completed_at: new Date().toISOString(),
        pr_metadata: { pull_request_url: "https://github.com/acme/widgets/pull/42" },
        final_message: "Opened a pull request with 2 changed files.",
      }),
    );
    const link = await screen.findByRole("link", { name: /acme\/widgets\/pull\/42/ });
    expect(link).toHaveAttribute("href", "https://github.com/acme/widgets/pull/42");
    expect(link).toHaveAttribute("rel", "noreferrer noopener");
    expect(screen.getByText("Opened a pull request with 2 changed files.")).toBeInTheDocument();
  });

  it("never links an address that is not a GitHub pull request", async () => {
    for (const url of ["javascript:alert(1)", "https://evil.example/acme/widgets/pull/42", "http://github.com/a/b/pull/1"]) {
      open(taskDetail({ status: "COMPLETED", pr_metadata: { pull_request_url: url } }));
      expect(await screen.findByText("No pull request was recorded for this task.")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /pull/ })).toBeNull();
      cleanup();
    }
  });

  it("shows a local task's result message", async () => {
    open(
      taskDetail({
        status: "COMPLETED",
        execution_target: "local",
        repository: null,
        final_message: "Updated README.md",
      }),
    );
    expect(await screen.findByText("Updated README.md")).toBeInTheDocument();
    expect(screen.queryByText(/No pull request was recorded/)).toBeNull();
  });

  it("says when nothing was recorded", async () => {
    open(taskDetail({ status: "COMPLETED", execution_target: "local", repository: null }));
    expect(await screen.findByText("No result message was recorded.")).toBeInTheDocument();
  });
});
