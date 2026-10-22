// Mirrors of the API's response schemas (apps/api/app/schemas).

export type TaskStatus =
  | "PENDING"
  | "QUEUED"
  | "ANALYZING"
  | "PLANNING"
  | "WAITING_APPROVAL"
  | "WAITING_PUBLICATION"
  | "CODING"
  | "TESTING"
  | "PUBLISHING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export const TASK_STATUSES: readonly TaskStatus[] = [
  "WAITING_PUBLICATION",
  "WAITING_APPROVAL",
  "PENDING",
  "QUEUED",
  "ANALYZING",
  "PLANNING",
  "CODING",
  "TESTING",
  "PUBLISHING",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];

export interface User {
  id: string;
  email: string | null;
  display_name: string;
  avatar_url: string | null;
  is_active: boolean;
  github_login: string | null;
  github_user_id: number | null;
}

export interface LoginResponse {
  authorization_url: string;
  state: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

/** The GitHub comment a task came from; absent for other tasks and from older APIs. */
export interface GitHubOrigin {
  issue_number: number | null;
  pull_request_number: number | null;
  triggered_by: string;
}

export interface TaskSummary {
  task_id: string;
  status: string;
  execution_target: string;
  repository: string | null;
  requested_task: string;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  /** Last change to the task (status, plan, result). */
  updated_at: string | null;
  parent_task_id: string | null;
  has_plan: boolean;
  has_diff: boolean;
  /** Set on failed tasks; absent from APIs older than this field. */
  failure_code?: string | null;
  github?: GitHubOrigin | null;
}

export interface TaskList {
  total: number;
  limit: number;
  offset: number;
  tasks: TaskSummary[];
  /** scope=readable: some repositories weren't checked, so tasks may be missing. */
  repositories_truncated?: boolean;
  repositories_unconfirmed?: boolean;
}

export interface ApprovalDetail {
  id: number;
  approval_type: string;
  status: string;
  requested_by: string | null;
  approved_by: string | null;
  comment: string | null;
  requested_at: string;
  responded_at: string | null;
  expires_at: string | null;
}

export interface TaskDetail {
  task_id: string;
  status: string;
  execution_target: string;
  repository: string | null;
  workspace_path: string | null;
  requested_task: string;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  parent_task_id: string | null;
  pr_metadata: Record<string, unknown> | null;
  /** The proposed plan, as the worker stored it: shape not guaranteed (see lib/plan.ts). */
  implementation_plan: Record<string, unknown> | null;
  failure_code: string | null;
  failure_reason: string | null;
  final_message: string | null;
  latest_approval: ApprovalDetail | null;
  github?: GitHubOrigin | null;
}

/** A task waiting for a person (GET /tasks/summary). */
export interface AttentionItem extends TaskSummary {
  attention: "plan_approval" | "publication_review" | "failed" | string;
}

export interface ActivityPreview {
  task_id: string;
  repository: string | null;
  requested_task: string;
  from_status: string | null;
  to_status: string | null;
  at: string;
}

/** GET /tasks/summary: exact counts over every task the caller may read. */
export interface TaskOverview {
  total: number;
  /** Per status; statuses without tasks are absent. */
  counts: Record<string, number>;
  needs_attention_total: number;
  needs_attention: AttentionItem[];
  failed_window_days: number;
  recent_activity: ActivityPreview[];
  repositories: string[];
  repositories_truncated: boolean;
  /** GitHub could not confirm access to some repositories just now; their tasks are not counted (never named). */
  repositories_unconfirmed?: boolean;
  generated_at: string;
}

export type ActivityKind = "created" | "status" | "approval_requested" | "approval_decided" | "retried";

/** One recorded fact about a task (GET /tasks/{id}/activity). */
export interface ActivityItem {
  kind: ActivityKind | string;
  at: string;
  from_status: string | null;
  to_status: string | null;
  /** A GitHub login; null when no person acted (or none was recorded). */
  actor: string | null;
  message: string | null;
  approval_id: number | null;
  approval_type: "plan" | "publication" | string | null;
  approval_status: ApprovalStatus | string | null;
  related_task_id: string | null;
  github: GitHubOrigin | null;
}

export interface TaskActivity {
  task_id: string;
  items: ActivityItem[];
  /** Earlier history was never recorded; partial_reason says what is missing. */
  partial: boolean;
  partial_reason: string | null;
  /** More status changes exist than were returned. */
  truncated: boolean;
}

export interface PublicationFile {
  path: string;
  action: "write" | "delete" | string;
  content: string | null;
}

export type ApprovalStatus = "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED";

export interface PublicationArtifact {
  task_id: string;
  repository: string;
  base_branch: string;
  base_sha: string;
  artifact_digest: string;
  total_bytes: number;
  commit_message: string;
  pull_request_title: string;
  pull_request_body: string;
  files: PublicationFile[];
  approval_status: ApprovalStatus | string | null;
  approved_by: string | null;
  expires_at: string | null;
  published: boolean;
  pull_request_url: string | null;
}

export interface FileDiff {
  path: string;
  status: string;
  additions: number;
  deletions: number;
  patch: string;
}

export interface TaskDiff {
  task_id: string;
  files_changed_count: number;
  additions: number;
  deletions: number;
  files: FileDiff[];
}

/** GET /system/status: which optional capabilities the API is configured for (no secrets). */
export interface SystemStatus {
  github_oauth: boolean;
  github_app: boolean;
  github_webhooks: boolean;
  local_execution: boolean;
  local_execution_trusted_accounts: number;
  test_execution_mode: string;
  session_secret: string;
}

export interface RetryResponse {
  task_id: string;
  parent_task_id: string;
  status: string;
}

export interface DecisionResponse {
  task_id: string;
  status: string;
  message: string;
}
