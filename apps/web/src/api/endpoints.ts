import { ApiError, apiRequest } from "./client";
import type {
  DecisionResponse,
  LoginResponse,
  PublicationArtifact,
  TaskDetail,
  TaskDiff,
  TaskEvent,
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
