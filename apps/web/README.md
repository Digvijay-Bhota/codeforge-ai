# CodeForge review console (`apps/web`)

A web console for CodeForge's two human gates: approving a task's **plan** before anything is implemented, and approving the **generated changes** before anything is pushed (CF-05). Sign in with GitHub, follow tasks, and decide. Every decision is an existing API call (`POST /api/v1/tasks/{id}/approve` / `reject` for plans, `…/publication/approve` / `reject` for changes), and the API makes every authorization decision.

React 18, TypeScript, Vite 5 and React Router 6. Plain CSS with design tokens (`src/styles/tokens.css`), with light and dark themes. No UI kit, web fonts or third-party scripts.

## What it does

- **Sign in** through GitHub OAuth (`/login` → GitHub → `/auth/callback`).
- **Overview** (`/overview`) shows exact counts over every task you can read (`GET /tasks/summary`): your own tasks, and every task in the registered repositories you have read access to. It lists what **needs attention** (plan approvals and publication reviews first, then failures from the last 14 days that have not been retried), the latest recorded status changes, tasks per status, and the repositories counted, each linking to its task list. It refreshes itself while a task is running. No figure is estimated: if more repositories are registered than the server checks, or GitHub can't confirm access to some just now, the page says their tasks were not counted (without naming repositories you may not be able to read).
- **Tasks** (`/`) lists your tasks, filtered by status. The quick filters include **Needs attention** and **In progress**, which send several statuses at once (`status=WAITING_APPROVAL,WAITING_PUBLICATION`); **Clear filters** is shown whenever a filter is set. Enter `owner/repository` to see every task in that repository you can read, or paste a task ID (as shown in CodeForge's acknowledgement comment on GitHub) to open that task. Reviewers find work waiting for them this way, with the **Plan approval** and **Awaiting review** filters. A task started by an `@codeforge` comment is titled by the commenter's instructions and shows its command, issue or pull request, and who commented; the task page links to the issue on GitHub.
- **Task** (`/tasks/:id`) shows status, timing, failure details and activity, and follows a running task until it settles. A GitHub task shows its **workflow progress** (plan → plan approval → code & tests → publication review → pull request), derived only from its status, plan approval, pull request and recorded history (`src/lib/workflow.ts`). A step behind the current one is marked done only when the record shows it happened; older tasks that skipped plan approval or publication review show those steps as **not recorded**, and when the history doesn't say where a failed task stopped, no step is marked. The **Activity** timeline (`GET /tasks/{id}/activity`) lists, oldest first, the task's creation and GitHub origin, every recorded status change, each approval request (with its number) and decision (with who decided; an expiry has no actor), and retries you can read. Tasks older than activity history are marked as partially recorded, and a decision is shown before the status change it caused. The history reloads when the task's status changes, keeping the current one on screen meanwhile. A failed task's notice names the step it stopped in, and says whether retrying is likely to help (an outage) or won't until something changes (billing, credentials, the App installation); a task already retried links to its retries. A failed task says what went wrong in words (`src/lib/failures.ts`), with the server's failure code and reason as text; the task list names the reason too. A task stopped with `EXECUTION_UNAVAILABLE` is shown as stopped before running, not as a crash. A failed or cancelled task offers **Retry task** (`POST /tasks/{id}/retry`): a new task from the same request, optionally with extra instructions; a GitHub task plans again and waits for approval. The new task links back to the one it retries. The task list refreshes itself every 5 seconds while a listed task is still running and the page is visible, and stops once all have settled.
- **Proposed plan** shows the plan a task is waiting on (status **Awaiting plan approval**). Every GitHub task, including those started by `@codeforge` comments, waits here before any code is written. When the server reports test execution as disabled, the panel and the approve dialog say that an approved plan will stop before running, and a stopped task explains why. The panel shows: summary, goal and risk level, each step with its rationale, affected paths and dependencies, plus affected files, tests, assumptions, risks and the validation strategy. **Approve plan** queues the implementation; for a GitHub task it publishes nothing, because the generated changes come back for their own review. **Reject plan** cancels the task. Both dialogs show the repository and task, explain what will happen, and send the ID of the approval request on screen (`approval_id`), which the panel and both dialogs show ("Approval request #12"). If the server's state has moved on (already decided, expired, or a newer request), the API answers `409` and the console reloads the task rather than retrying. Approved, rejected and expired requests are shown without decision buttons.
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

## Run with Docker Compose

The root `docker-compose.yml` includes a `web` service that builds this app (`Dockerfile`) and serves it with nginx (`nginx.conf`) on `http://localhost:3000`, forwarding `/api/` to the API. See [docs/deployment.md](../../docs/deployment.md#local-setup-step-by-step).

## Deploy

`npm run build` produces static files in `dist/`. Serve them so that:

- unknown paths fall back to `index.html` (client-side routes such as `/tasks/:id`, `/auth/callback`);
- `/api/` is proxied to the API on the same origin;
- the CSP above is sent as a header.

Register `https://<console-host>/auth/callback` as the GitHub OAuth App callback, and add it to `ALLOWED_OAUTH_REDIRECT_URIS`.
