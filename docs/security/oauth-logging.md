# OAuth codes and state in logs

GitHub sign-in ends with two requests whose query strings carry an OAuth
authorization code and the single-use CSRF state:

1. GitHub redirects the browser to the console: `GET /auth/callback?code=…&state=…`.
2. The console forwards both to the API: `GET /api/v1/auth/github/callback?code=…&state=…`.

The code is exchanged once for a GitHub token and the state is consumed
atomically (CF-13), so a logged value cannot be replayed after a successful
sign-in. A log line written before the exchange completes, or for a sign-in
that failed half way, can still hold a usable code for its short lifetime.
Neither value belongs in a log.

## What CodeForge does

| Where | What is logged |
|---|---|
| API access log (Uvicorn, `uvicorn.access`) | Method, path, status and query string, with the **values** of sensitive parameters replaced by `***`: `code`, `state`, `access_token`, `refresh_token`, `id_token`, `token`, `client_secret`, `password`, `api_key`, `key`, `signature`, `sig`. Other parameters (`limit`, `offset`, …) are kept. A filter installed by `create_app()` does this (`app/observability/redaction.py`); it also applies to a `gunicorn.access` logger. |
| API application logs | The callback handler never logs the code, the state or the GitHub token. The code is sent to GitHub in a POST body, so the HTTP client's request log shows no code either. |
| Console access log (nginx, `apps/web/nginx.conf`) | Method, the requested **path without its query string** (a trailing `?` shows that one was present), status, size and user agent. No referrer. nginx cannot remove single arguments, so the whole query string is left out. |
| Stored failure text | Task failure reasons are redacted before they are stored, sent to GitHub or returned by the API (see `redact_secrets`). |

`apps/api/tests/security/test_access_log_redaction.py` runs a real Uvicorn
server and checks the access log; `apps/web/src/test/nginx.test.ts` checks the
nginx log format.

## What CodeForge cannot control

- **nginx's error log** quotes the full request line, query string included,
  when it fails to serve a request, for example `api could not be resolved`
  while the API is restarting during a sign-in. Its format cannot be changed.
  Keep the error log at its default level, treat it as sensitive, and retain it
  briefly.
- **Other proxies and load balancers** in front of the console (a cloud load
  balancer, an ingress controller, a tunnel) usually log full URLs. Configure
  them to drop query strings for `/auth/callback` and `/api/v1/auth/github/callback`,
  or not to log those paths.
- **The browser** keeps the callback URL in its history until the page
  replaces it. Error-tracking or analytics scripts would see it too; the
  console loads none.
- **GitHub** logs its own redirect.

If a code may have been exposed before it was used, nothing needs rotating:
it expires within minutes and is single-use. If OAuth client credentials
(`GITHUB_CLIENT_SECRET`) ever appear in a log, rotate them on the OAuth App.
