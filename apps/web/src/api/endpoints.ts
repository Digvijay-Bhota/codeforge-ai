import { ApiError, apiRequest } from "./client";
import type {
  DecisionResponse,
  LoginResponse,
  PublicationArtifact,
  TaskDetail,
  TaskDiff,
  TaskEvent,
  SystemStatus,
  TaskList,
  TokenResponse,
  User,
} from "./types";

const task = (taskId: string) => `/tasks/${encodeURIComponent(taskId)}`;

async function orNullIfMissing<T>(request: Promise<T>): Promise<T | null> {
  try {
    return await request;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export const api = {
  startLogin: (redirectUri: string) =>
    apiRequest<LoginResponse>("/auth/github/login", {
      query: { redirect_uri: redirectUri },
      authenticated: false,
    }),

  completeLogin: (code: string, state: string) =>
    apiRequest<TokenResponse>("/auth/github/callback", { query: { code, state }, authenticated: false }),

  me: (signal?: AbortSignal) => apiRequest<User>("/auth/me", { signal }),

  systemStatus: (signal?: AbortSignal) => apiRequest<SystemStatus>("/system/status", { signal }),

  listTasks: (
    filters: { status?: string | null; repository?: string | null; limit: number; offset: number },
    signal?: AbortSignal,
  ) => apiRequest<TaskList>("/tasks", { query: filters, signal }),

  getTask: (taskId: string, signal?: AbortSignal) => apiRequest<TaskDetail>(task(taskId), { signal }),

  getEvents: (taskId: string, signal?: AbortSignal) =>
    apiRequest<TaskEvent[]>(`${task(taskId)}/events`, { query: { limit: 100 }, signal }),

  /** The changes awaiting publication approval, or null if the task captured none. */
  getPublication: (taskId: string, signal?: AbortSignal) =>
    orNullIfMissing(apiRequest<PublicationArtifact>(`${task(taskId)}/publication`, { signal })),

  /** The generation diff, or null if there is none. */
  getDiff: (taskId: string, signal?: AbortSignal) =>
    orNullIfMissing(apiRequest<TaskDiff>(`${task(taskId)}/diff`, { signal })),

  /** Approve the plan of exactly this approval request (409 if it is no longer the pending one). */
  approvePlan: (taskId: string, approvalId: number, comment: string | null) =>
    apiRequest<DecisionResponse>(`${task(taskId)}/approve`, {
      method: "POST",
      body: { approval_id: approvalId, comment },
    }),

  rejectPlan: (taskId: string, approvalId: number, reason: string | null) =>
    apiRequest<DecisionResponse>(`${task(taskId)}/reject`, {
      method: "POST",
      body: { approval_id: approvalId, reason },
    }),

  approvePublication: (taskId: string, artifactDigest: string, comment: string | null) =>
    apiRequest<DecisionResponse>(`${task(taskId)}/publication/approve`, {
      method: "POST",
      body: { artifact_digest: artifactDigest, comment },
    }),

  rejectPublication: (taskId: string, reason: string | null) =>
    apiRequest<DecisionResponse>(`${task(taskId)}/publication/reject`, {
      method: "POST",
      body: { reason },
    }),
};
