// A small fetch wrapper for the CodeForge API.  Requests go to this origin
// (/api/v1); the dev server and production hosting forward them to the API.

export const API_BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

interface ClientHooks {
  getToken: () => string | null;
  onUnauthorized: () => void;
}

let hooks: ClientHooks = { getToken: () => null, onUnauthorized: () => undefined };

/** Wire the client to the session: where the token comes from, and what a 401 does. */
export function configureApiClient(next: ClientHooks): void {
  hooks = next;
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  query?: Record<string, string | number | null | undefined>;
  signal?: AbortSignal;
  /** False for the login endpoints, which run before there is a session. */
  authenticated?: boolean;
}

function messageFromDetail(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === "string") return first.msg;
  }
  if (status === 401) return "Your session has ended. Sign in again.";
  if (status === 403) return "You don't have permission to do that.";
  if (status === 404) return "Not found.";
  if (status >= 500) return "The CodeForge API had a problem. Try again in a moment.";
  return `Request failed (${status}).`;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, query, signal, authenticated = true } = options;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  }
  const url = `${API_BASE}${path}${params.size ? `?${params.toString()}` : ""}`;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = authenticated ? hooks.getToken() : null;
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      credentials: "same-origin",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "Can't reach the CodeForge API. Check your connection and try again.");
  }

  let payload: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    if (response.status === 401 && authenticated) hooks.onUnauthorized();
    const detail = (payload as { detail?: unknown } | null)?.detail;
    throw new ApiError(response.status, messageFromDetail(detail, response.status));
  }
  return payload as T;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return "Something went wrong.";
}
