import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { errorMessage } from "../api/client";
import { api } from "../api/endpoints";
import { Brand } from "../components/Brand";
import { Spinner } from "../components/Spinner";
import { useAuth } from "./AuthContext";
import { takeLoginState } from "./session";

export function CallbackPage() {
  const [params] = useSearchParams();
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  // An authorization code works once, so the exchange must run once, even
  // when React runs the effect twice in development.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    const code = params.get("code");
    const state = params.get("state");
    const githubError = params.get("error");
    const expected = takeLoginState();

    if (githubError) {
      setError(githubError === "access_denied" ? "Sign-in was cancelled on GitHub." : "GitHub could not sign you in.");
      return;
    }
    if (!code || !state) {
      setError("This sign-in link is incomplete. Start again from the sign-in page.");
      return;
    }
    // Only finish a login this tab started: a link carrying someone else's
    // code would otherwise sign you in to their account.
    if (!expected.state || expected.state !== state) {
      setError("This sign-in was not started from this browser tab. Start again from the sign-in page.");
      return;
    }
    api.completeLogin(code, state).then(
      (response) => {
        signIn(response);
        navigate(expected.returnTo, { replace: true });
      },
      (err) => setError(errorMessage(err)),
    );
  }, [params, signIn, navigate]);

  return (
    <main className="auth-page" id="main">
      <div className="auth-card">
        <Brand />
        {error ? (
          <>
            <h1 className="auth-title">Sign-in didn't complete</h1>
            <p className="notice notice-danger" role="alert">
              {error}
            </p>
            <Link className="button button-primary button-block" to="/login" replace>
              Back to sign in
            </Link>
          </>
        ) : (
          <div className="auth-progress" role="status">
            <Spinner />
            <span>Signing you in…</span>
          </div>
        )}
      </div>
    </main>
  );
}
