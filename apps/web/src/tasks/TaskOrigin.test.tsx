import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { githubOriginUrl, taskCommand, taskTitle } from "../lib/format";
import { mockApi, renderApp, signedIn, summary, taskDetail, USER } from "../test/helpers";

// Every task started by an @codeforge comment has the same first line,
// "Execute @codeforge <action> for issue #<n>.", so the list could not tell
// them apart, and nothing linked a task to the comment that started it.

const COMMENT_REQUEST =
  "Execute @codeforge implement for issue #2.\nTitle: Docs\nArguments:\nAdd a short \"Sandbox integration check\" section to README.md\nKeep it documentation-only.";
const TASK = "7b8cf1b3-64e2-438a-9564-58dd9ed5ea0d";

beforeEach(() => signedIn());

describe("titles and origins", () => {
  it("titles a comment task by its instructions, then the issue title, then the command", () => {
    expect(taskTitle(COMMENT_REQUEST)).toBe(
      'Add a short "Sandbox integration check" section to README.md Keep it documentation-only.',
    );
    expect(taskTitle(`Execute @codeforge fix for issue #1.\nArguments:\n${"word ".repeat(60)}`)).toHaveLength(138);
    expect(taskTitle("Execute @codeforge review for pull request #7.\nTitle: Speed up the cache")).toBe("Speed up the cache");
    expect(taskTitle("Execute @codeforge explain for issue #3.")).toBe("@codeforge explain");
    expect(taskTitle("Fix the widget\nmore")).toBe("Fix the widget"); // other tasks: unchanged
    expect(taskCommand(COMMENT_REQUEST)).toBe("@codeforge implement");
    expect(taskCommand("Execute the plan for issue #2.")).toBeNull();
  });

  it("links only well-formed repositories to their issue or pull request", () => {
    expect(githubOriginUrl("acme/widgets", { issue_number: 2, pull_request_number: null })).toBe(
      "https://github.com/acme/widgets/issues/2",
    );
    expect(githubOriginUrl("acme/widgets", { issue_number: null, pull_request_number: 7 })).toBe(
      "https://github.com/acme/widgets/pull/7",
    );
    expect(githubOriginUrl("acme/../evil", { issue_number: 2, pull_request_number: null })).toBeNull();
    expect(githubOriginUrl("acme/widgets", null)).toBeNull();
  });
});

describe("task list", () => {
  it("tells comment tasks apart and shows where each came from", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": {
        body: {
          total: 2, limit: 20, offset: 0,
          tasks: [
            summary({ task_id: TASK, status: "WAITING_APPROVAL", requested_task: COMMENT_REQUEST,
                      github: { issue_number: 2, pull_request_number: null, triggered_by: "dev" } }),
            summary({ task_id: "b-2", requested_task: "Execute @codeforge fix for issue #9.\nArguments:\nFix the rounding" }),
          ],
        },
      },
    }); // fmt: skip
    renderApp("/");
    const [first, second] = within(await screen.findByRole("list")).getAllByRole("link");
    expect(first).toHaveTextContent('Add a short "Sandbox integration check" section to README.md');
    expect(first).toHaveTextContent("Issue #2 by @dev");
    expect(first).toHaveTextContent("@codeforge implement");
    expect(second).toHaveTextContent("Fix the rounding");
    expect(second).not.toHaveTextContent("Issue #"); // no origin from an older API: nothing invented
  });

  it("opens a task by its ID from the search field, without filtering the list", async () => {
    const api = mockApi({
      "GET /auth/me": { body: USER },
      "GET /tasks": { body: { total: 0, limit: 20, offset: 0, tasks: [] } },
      [`GET /tasks/${TASK}`]: { body: taskDetail({ task_id: TASK, requested_task: COMMENT_REQUEST }) },
      [`GET /tasks/${TASK}/events`]: { body: [] },
      [`GET /tasks/${TASK}/publication`]: { status: 404, body: { detail: "none" } },
    });
    renderApp("/");
    await screen.findByText("No tasks yet");
    await userEvent.type(screen.getByLabelText("Repository or task ID"), ` ${TASK.toUpperCase()} `);
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Sandbox integration check");
    expect(api.callsTo("GET", "/tasks").every((call) => !call.query.get("repository"))).toBe(true);
  });
});

describe("task detail", () => {
  it("names the command and links the issue it came from", async () => {
    mockApi({
      "GET /auth/me": { body: USER },
      [`GET /tasks/${TASK}`]: {
        body: taskDetail({ task_id: TASK, repository: "acme/widgets", requested_task: COMMENT_REQUEST, status: "FAILED",
                           github: { issue_number: 2, pull_request_number: null, triggered_by: "dev" } }),
      },
      [`GET /tasks/${TASK}/events`]: { body: [] },
      [`GET /tasks/${TASK}/publication`]: { status: 404, body: { detail: "none" } },
    }); // fmt: skip
    renderApp(`/tasks/${TASK}`);
    const link = await screen.findByRole("link", { name: "Issue #2 (opens GitHub)" });
    expect(link).toHaveAttribute("href", "https://github.com/acme/widgets/issues/2");
    expect(link).toHaveAttribute("rel", "noreferrer noopener");
    expect(screen.getByText("@codeforge implement")).toBeInTheDocument();
    expect(screen.getByText(/by @dev/)).toBeInTheDocument();
  });
});
