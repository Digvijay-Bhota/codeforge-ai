# CodeForge review console (`apps/web`)

A web console for CodeForge's two human gates: approving a task's **plan** before anything is implemented, and approving the **generated changes** before anything is pushed (CF-05). Sign in with GitHub, follow tasks, and decide. Every decision is an existing API call (`POST /api/v1/tasks/{id}/approve` / `reject` for plans, `…/publication/approve` / `reject` for changes), and the API makes every authorization decision.

React 18, TypeScript, Vite 5 and React Router 6. Plain CSS with design tokens (`src/styles/tokens.css`), with light and dark themes. No UI kit, web fonts or third-party scripts.

## What it does

- **Sign in** through GitHub OAuth (`/login` → GitHub → `/auth/callback`).
- **Tasks** (`/`) lists your tasks, filtered by status. Enter `owner/repository` to see every task in that repository you can read. Reviewers find work waiting for them this way, with the **Plan approval** and **Awaiting review** filters.
- **Task** (`/tasks/:id`) shows status, timing, failure details and activity, and follows a running task until it settles.
- **Proposed plan** shows the plan a task is waiting on (status **Awaiting plan approval**): summary, goal and risk level, each step with its rationale, affected paths and dependencies, plus affected files, tests, assumptions, risks and the validation strategy. **Approve plan** queues the implementation; for a GitHub task it publishes nothing, because the generated changes come back for their own review. **Reject plan** cancels the task. Both dialogs show the repository and task, explain what will happen, and send the ID of the approval request on screen (`approval_id`). If the server's state has moved on (already decided, expired, or a newer request), the API answers `409` and the console reloads the task rather than retrying. Approved, rejected and expired requests are shown without decision buttons.
- **Generated changes** shows, for a GitHub task, exactly what publication would write:
  - every file's full content (or its deletion);
  - the pull request title and description;
  - the commit message;
  - the base commit and the artifact digest.

  The generation diff is shown too, labelled as outside the approval: the digest covers the exact content, not the diff.
- **Approve and publish** stays disabled until every file has been opened. Approving sends the digest of the artifact on screen; the API refuses a digest that doesn't match what it stored. **Reject** cancels the task, and nothing is pushed.

## Security model

- **Same origin.** The browser talks only to this origin, and `/api` is forwarded to the API. The API needs no CORS setting.
- **Session.** The session token lives in `sessionStorage`: one tab, gone when the tab closes. It is never sent to the sign-in endpoints. A 401 ends the session, and so does reaching the token's expiry.
- **Login CSRF.** The OAuth `state` returned by `/auth/github/login` is remembered in this tab. A callback carrying any other state is refused before the code is exchanged, so a link holding someone else's code can't sign you in to their account.
- **Untrusted content.** Repository content, pull request text and agent output render as text, never markup. The lint config forbids `dangerouslySetInnerHTML`.
- **Links.** A pull request link is shown only for `https://github.com`. After sign-in, the app returns only to its own paths.
- **CSP.** Production builds carry a strict Content-Security-Policy (`vite.config.ts`): scripts, styles and connections from this origin only, and images also from `avatars.githubusercontent.com`. Serve the build with the same policy as a response header if you can, and add `frame-ancestors 'none'`, which a meta tag can't set.

## Develop

Requires Node 20+ and the API running on `http://localhost:8000` (see the root README).

```bash
cd apps/web
npm ci
npm run dev          # http://localhost:3000, /api forwarded to the API
```

Set `CODEFORGE_API_URL` to forward `/api` somewhere else.

**GitHub sign-in locally.** Your GitHub OAuth App's callback URL must cover `http://localhost:3000/auth/callback`. That URL is in the API's default `ALLOWED_OAUTH_REDIRECT_URIS`; keep it there if you override the setting.

## Check

```bash
npm run lint         # ESLint, no warnings allowed
npm run typecheck    # tsc, strict
npm test             # Vitest + Testing Library (jsdom)
npm run build        # type-check, then the production build in dist/
npm run preview      # serve dist/ on :3000, /api forwarded as in dev
```

CI runs lint, type-check, tests and build on every pull request into `main` (`.github/workflows/web.yml`, check `Web: Lint, Type-check, Test, Build`), with `npm ci --ignore-scripts`. It is not a required check unless the `main` ruleset is changed to require it.

## Deploy

`npm run build` produces static files in `dist/`. Serve them so that:

- unknown paths fall back to `index.html` (client-side routes such as `/tasks/:id`, `/auth/callback`);
- `/api/` is proxied to the API on the same origin;
- the CSP above is sent as a header.

Register `https://<console-host>/auth/callback` as the GitHub OAuth App callback, and add it to `ALLOWED_OAUTH_REDIRECT_URIS`.
