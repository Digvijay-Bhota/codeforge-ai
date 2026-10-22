// What a task's failure_code means, and what to do about it.  The codes are
// the API's (apps/api/app/orchestration/failures.py, app/schemas/task.py
// GitHubFailureStage, app/worker.py); docs/deployment.md lists them too.  The
// failure_reason shown next to this carries the detail.

export interface FailureExplanation {
  /** Short label, also used in the task list. */
  title: string;
  /** What happened and what to do; null when the reason says it all. */
  advice: string | null;
}

const EXPLANATIONS: Record<string, FailureExplanation> = {
  REPOSITORY_ACQUISITION_FAILED: {
    title: "Couldn't clone the repository",
    advice: "The worker could not copy the repository. Check the reason: a missing git executable means the worker image must be rebuilt.",
  },
  REPOSITORY_RESOLUTION_FAILED: {
    title: "Repository not found",
    advice: "GitHub did not return the repository or its branch. Check that the GitHub App is installed on it.",
  },
  CREDENTIALS_FAILED: {
    title: "No repository access",
    advice: "No GitHub App credential could be issued for this repository. Check the App installation.",
  },
  MODEL_QUOTA_EXCEEDED: {
    title: "Model quota exceeded",
    advice:
      "The model provider refused the request: the account has no credit left or reached its billing limit. Retrying won't help until the account's billing is changed.",
  },
  MODEL_RATE_LIMITED: { title: "Model rate limited", advice: "The model provider is limiting requests. Retry later." },
  MODEL_AUTHENTICATION_FAILED: {
    title: "Model API key refused",
    advice: "The model provider rejected the server's API key, or the key can't use the configured model.",
  },
  MODEL_UNAVAILABLE: {
    title: "Model unavailable",
    advice: "The model provider could not be reached or had an error. Retry later.",
  },
  MODEL_REQUEST_REJECTED: {
    title: "Model request rejected",
    advice: "The model provider rejected the request, for example because the configured model doesn't exist.",
  },
  MODEL_OUTPUT_INVALID: {
    title: "No usable plan",
    advice: "The model answered, but not with a usable result. Retrying, or rephrasing the instructions, may help.",
  },
  ANALYSIS_FAILED: { title: "Repository analysis failed", advice: null },
  PLANNING_FAILED: { title: "Planning failed", advice: null },
  CODING_FAILED: { title: "Coding failed", advice: null },
  CODING_TURN_LIMIT: { title: "Coding turn limit reached", advice: null },
  NO_CHANGES: { title: "No changes produced", advice: null },
  TESTS_FAILED: { title: "Tests failed", advice: null },
  TEST_INFRASTRUCTURE_FAILED: { title: "Test runner failed", advice: null },
  ORCHESTRATION_FAILED: { title: "Workflow failed", advice: null },
  POLICY_REFUSED: { title: "Refused by policy", advice: "The server refused this task before anything ran." },
  PUBLICATION_FAILED: { title: "Publication failed", advice: null },
  BRANCH_CREATION_FAILED: { title: "Branch creation failed", advice: null },
  COMMIT_FAILED: { title: "Commit failed", advice: null },
  PUSH_FAILED: { title: "Push failed", advice: null },
  PULL_REQUEST_FAILED: { title: "Pull request failed", advice: null },
  JOB_INTERRUPTED: { title: "Interrupted", advice: "The worker stopped while running this task. Retry the task." },
  JOB_ATTEMPTS_EXHAUSTED: { title: "Gave up after retries", advice: "The job failed on every attempt. Retry the task." },
  WORKER_ERROR: { title: "Worker error", advice: "The worker hit an unexpected error. The server's log has the detail." },
  EXECUTION_UNAVAILABLE: {
    title: "Execution not available",
    advice:
      "The plan was approved, but this server has no permitted test executor, so the task stopped before any code was written. Nothing ran and nothing was published.",
  },
};

export function explainFailure(code: string | null | undefined): FailureExplanation {
  if (!code) return { title: "Failed", advice: null };
  return EXPLANATIONS[code] ?? { title: "Failed", advice: null };
}
