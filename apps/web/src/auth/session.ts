import type { TokenResponse, User } from "../api/types";

// The session lives in sessionStorage: it is scoped to this tab and gone when
// the tab closes.  The page loads no third-party scripts and production
// builds enforce a strict Content-Security-Policy, which is what keeps it
// out of reach of injected code.

const SESSION_KEY = "codeforge.session";
const LOGIN_STATE_KEY = "codeforge.login.state";
const RETURN_TO_KEY = "codeforge.login.returnTo";

export interface Session {
  token: string;
  expiresAt: number;
  user: User;
}

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function read(key: string): string | null {
  try {
    return storage()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) storage()?.removeItem(key);
    else storage()?.setItem(key, value);
  } catch {
    // Storage unavailable: the session simply does not survive a reload.
  }
}

export function sessionFromToken(response: TokenResponse, now = Date.now()): Session {
  return { token: response.access_token, expiresAt: now + response.expires_in * 1000, user: response.user };
}

export function loadSession(now = Date.now()): Session | null {
  const raw = read(SESSION_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<Session>;
    if (
      typeof parsed.token === "string" &&
      parsed.token &&
      typeof parsed.expiresAt === "number" &&
      parsed.expiresAt > now &&
      parsed.user &&
      typeof parsed.user.id === "string"
    ) {
      return parsed as Session;
    }
  } catch {
    // fall through: a malformed entry is discarded
  }
  write(SESSION_KEY, null);
  return null;
}

export function saveSession(session: Session | null): void {
  write(SESSION_KEY, session ? JSON.stringify(session) : null);
}

/** Remember the OAuth state this tab started, so only that login can complete here. */
export function rememberLoginState(state: string, returnTo: string): void {
  write(LOGIN_STATE_KEY, state);
  write(RETURN_TO_KEY, safeReturnTo(returnTo));
}

/** The state this tab started (consumed), and where to go afterwards. */
export function takeLoginState(): { state: string | null; returnTo: string } {
  const state = read(LOGIN_STATE_KEY);
  const returnTo = safeReturnTo(read(RETURN_TO_KEY));
  write(LOGIN_STATE_KEY, null);
  write(RETURN_TO_KEY, null);
  return { state, returnTo };
}

/** Only same-app paths: never another origin ("//evil", "https://...") or the auth pages. */
export function safeReturnTo(path: string | null | undefined): string {
  if (!path || !path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) return "/";
  if (path.startsWith("/login") || path.startsWith("/auth/")) return "/";
  return path;
}
