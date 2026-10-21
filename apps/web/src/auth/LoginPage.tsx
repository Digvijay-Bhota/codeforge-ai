import { useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { errorMessage } from "../api/client";
import { api } from "../api/endpoints";
import { Brand } from "../components/Brand";
import { GitHubIcon } from "../components/icons";
import { navigateAway } from "../lib/navigation";
import { useAuth } from "./AuthContext";
import { rememberLoginState, safeReturnTo } from "./session";

export const CALLBACK_PATH = "/auth/callback";

export function LoginPage() {
  const { session, endedBecause } = useAuth();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from;
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (session) return <Navigate to={safeReturnTo(from)} replace />;

  async function startLogin() {
    setPending(true);
    setError(null);
    try {
      const { authorization_url, state } = await api.startLogin(`${window.location.origin}${CALLBACK_PATH}`);
      if (new URL(authorization_url).protocol !== "https:") {
        throw new Error("The API returned an unexpected sign-in address.");
      }
      rememberLoginState(state, safeReturnTo(from));
      navigateAway(authorization_url);
    } catch (err) {
      setError(errorMessage(err));
      setPending(false);
    }
  }

  return (
    <main className="auth-page" id="main">
      <div className="auth-card">
        <Brand />
        <h1 className="auth-title">Review changes before they ship</h1>
        <p className="auth-lead">
          CodeForge pauses every generated change until someone with write access to the repository reviews and
          approves it. Sign in to see tasks and review what is waiting.
        </p>
        {endedBecause === "expired" && (
          <p className="notice notice-info" role="status">
            Your session ended. Sign in again to continue.
          </p>
        )}
        {error && (
          <p className="notice notice-danger" role="alert">
            {error}
          </p>
        )}
        <button type="button" className="button button-primary button-block" onClick={startLogin} disabled={pending}>
          <GitHubIcon />
          {pending ? "Redirecting to GitHub…" : "Sign in with GitHub"}
        </button>
        <p className="auth-footnote">You'll be asked to authorize CodeForge on GitHub.</p>
      </div>
    </main>
  );
}
