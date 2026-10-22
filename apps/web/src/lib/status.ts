export type Tone = "neutral" | "info" | "warning" | "success" | "danger";

interface StatusInfo {
  label: string;
  tone: Tone;
}

const STATUS: Record<string, StatusInfo> = {
  PENDING: { label: "Pending", tone: "neutral" },
  QUEUED: { label: "Queued", tone: "neutral" },
  ANALYZING: { label: "Analyzing", tone: "info" },
  PLANNING: { label: "Planning", tone: "info" },
  WAITING_APPROVAL: { label: "Awaiting plan approval", tone: "warning" },
  WAITING_PUBLICATION: { label: "Awaiting review", tone: "warning" },
  CODING: { label: "Coding", tone: "info" },
  TESTING: { label: "Testing", tone: "info" },
  PUBLISHING: { label: "Publishing", tone: "info" },
  COMPLETED: { label: "Completed", tone: "success" },
  FAILED: { label: "Failed", tone: "danger" },
  CANCELLED: { label: "Cancelled", tone: "neutral" },
};

export function statusInfo(status: string): StatusInfo {
  return STATUS[status] ?? { label: status.replace(/_/g, " ").toLowerCase(), tone: "neutral" };
}

/** Statuses after which nothing changes without a person acting. */
const SETTLED = new Set(["COMPLETED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "WAITING_PUBLICATION"]);

export function isSettled(status: string): boolean {
  return SETTLED.has(status);
}

/** Ended without the result: the console offers to retry (the API also allows COMPLETED). */
export function canRetry(status: string): boolean {
  return status === "FAILED" || status === "CANCELLED";
}

/** Quick filters; several statuses are sent comma-separated (the API accepts them). */
export const STATUS_GROUPS = {
  awaitingDecision: { label: "Awaiting a decision", value: "WAITING_APPROVAL,WAITING_PUBLICATION" },
  planApproval: { label: "Plan approval", value: "WAITING_APPROVAL" },
  awaitingReview: { label: "Awaiting review", value: "WAITING_PUBLICATION" },
  inProgress: { label: "In progress", value: "PENDING,QUEUED,ANALYZING,PLANNING,CODING,TESTING,PUBLISHING" },
  failed: { label: "Failed", value: "FAILED" },
} as const;

/** A task list URL: *scope* "readable" lists every task the user can read, not just their own. */
export function taskListPath(filters: { status?: string; scope?: "readable" }): string {
  const params = new URLSearchParams();
  if (filters.scope) params.set("scope", filters.scope);
  if (filters.status) params.set("status", filters.status);
  const query = params.toString();
  return query ? `/?${query}` : "/";
}
