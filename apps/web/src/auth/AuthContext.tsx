import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { configureApiClient } from "../api/client";
import { api } from "../api/endpoints";
import type { TokenResponse } from "../api/types";
import { loadSession, saveSession, sessionFromToken, type Session } from "./session";

export type SignOutReason = "signed-out" | "expired";

interface AuthValue {
  session: Session | null;
  /** Why the last session ended, for the sign-in page to explain. */
  endedBecause: SignOutReason | null;
  signIn: (response: TokenResponse) => void;
  signOut: (reason?: SignOutReason) => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(() => loadSession());
  const [endedBecause, setEndedBecause] = useState<SignOutReason | null>(null);
  const sessionRef = useRef(session);
  sessionRef.current = session;

  const signOut = useCallback((reason: SignOutReason = "signed-out") => {
    saveSession(null);
    setSession(null);
    setEndedBecause(reason);
  }, []);
  const signOutRef = useRef(signOut);
  signOutRef.current = signOut;

  // Wire the API client during the first render, not in an effect: child
  // effects run before this component's, so a page's first requests after a
  // reload would otherwise go out without the stored session's token.
  useState(() =>
    configureApiClient({
      getToken: () => sessionRef.current?.token ?? null,
      onUnauthorized: () => {
        if (sessionRef.current) signOutRef.current("expired");
      },
    }),
  );

  const signIn = useCallback((response: TokenResponse) => {
    const next = sessionFromToken(response);
    saveSession(next);
    setSession(next);
    setEndedBecause(null);
  }, []);

  // End the session when the token expires, not at the next failed request.
  useEffect(() => {
    if (!session) return;
    const remaining = session.expiresAt - Date.now();
    const timer = window.setTimeout(() => signOut("expired"), Math.max(0, remaining));
    return () => window.clearTimeout(timer);
  }, [session, signOut]);

  // A restored session is re-checked once: a deactivated account or revoked
  // token ends it (the 401 handler signs out).
  const token = session?.token;
  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    api.me(controller.signal).then(
      (user) => {
        setSession((current) => {
          if (!current || current.token !== token) return current;
          const next = { ...current, user };
          saveSession(next);
          return next;
        });
      },
      () => undefined,
    );
    return () => controller.abort();
  }, [token]);

  const value = useMemo(() => ({ session, endedBecause, signIn, signOut }), [session, endedBecause, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
