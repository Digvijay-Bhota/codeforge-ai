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
