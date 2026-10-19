# CI trust and branch governance

What the required checks on `main` do and do not prove, why a pull request can
still weaken its own validation, and the repository settings that would reduce
that.  Written for the gVisor executor (Phase 10B.5 Stage 1D,
[`gvisor-test-executor.md`](gvisor-test-executor.md)), but it applies to every
required check.

State as read through the GitHub REST API on 2026-10-19.  These are repository
settings, not files: re-read them before relying on this page.

## 1. Current settings

| Setting | Observed | Source |
| --- | --- | --- |
| Repository | Public, owned by a personal account (not an organization) | `GET /repos/{owner}/{repo}` |
| Collaborators | One (the owner, admin); no pending invitations | `GET /repos/{owner}/{repo}/collaborators`, `…/invitations` |
| Rulesets | One, id 25398695, active, `refs/heads/main`, no bypass actors | `GET /repos/{owner}/{repo}/rulesets/25398695` |
| Rules on `main` | Only `required_status_checks`: `gVisor runtime feasibility (disposable runner)` and `Build, Lint, Type-check, Test, Migrate`, both from GitHub Actions (integration 15368), strict (branch must be up to date) | `GET /repos/{owner}/{repo}/rules/branches/main` |
| Pull request required | **No** | same |
| Approving reviews required | **No** | same |
| Force pushes / deletion blocked | **No** rule for either | same |
| Legacy branch protection | None: `404 Branch not protected`, read with admin permission (so not an access-denied response) | `GET /repos/{owner}/{repo}/branches/main/protection` |
| `CODEOWNERS` | None on `main` or the Stage 1D branch | contents API |
| Actions | Default token read-only; Actions cannot approve PRs; 0 secrets, 0 variables, 0 environments | `…/actions/permissions/workflow`, `…/actions/secrets`, `…/environments` |

## 2. What a green required check proves

For `pull_request`, GitHub runs "the workflow file from the merge commit of the
pull request"
([securely using `pull_request_target`](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target)).
So the workflow, the tests, the minimum test counts (`MIN_SPIKE_TESTS`,
`MIN_EXECUTOR_TESTS`) and `check_junit.py` that produce the required checks are
all the pull request's own versions.

A green check therefore shows that **the pull request's own tests passed under
its own workflow** on a disposable GitHub-hosted runner.  It is not an
independent verification: a pull request could lower a minimum, mark a step
`continue-on-error`, or weaken a test, and its own check would still pass.

Pinning the check's source does not change this.  The ruleset already pins
both checks to GitHub Actions (integration 15368), but every workflow in this
repository reports as that same app.  On commit `ec92ec7`, two check suites (a
push run and a pull-request run) each produced a check run named
`gVisor runtime feasibility (disposable runner)` from app 15368.  A required
check is matched by name and app, not by workflow file.  Since `a0922be` only
pull-request runs use that name, but that is a convention in the workflow, not
an enforcement.

## 3. Options evaluated

| Option | Would it stop a PR weakening its own validation? | Available here? |
| --- | --- | --- |
| Ruleset **"Require workflows to pass before merging"** with the workflow pinned to a trusted ref or SHA | Yes, if pinned: the enforced workflow comes from the configured source, not from the PR.  It is a specific workflow, not a check name. | **No.** GitHub documents it as configured "at the organization or enterprise level" ([available rules](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)); this repository belongs to a personal account. |
| **`CODEOWNERS`** for the trust-anchor files (§5) **plus** a pull-request rule with required approvals, "Require review from Code Owners" and "Require approval of the most recent reviewable push" | Yes, by a human: changes to those files need approval from someone other than the last pusher. | **Not yet.** "Pull request authors cannot approve their own pull requests" ([docs](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/reviewing-changes-in-pull-requests/approving-a-pull-request-with-required-reviews)), and there is only one collaborator.  Enabling it now would block every merge. |
| A second, "trusted" guard workflow on `pull_request_target` (runs the default branch's version) that fails when a PR touches the trust-anchor files | **No.** Its result would still be enforced as a required check matched by name and app 15368, and the PR can add a job that reports the same name (§2).  How GitHub resolves two same-named check runs with different conclusions was not tested. | Also rejected: it adds a privileged trigger.  GitHub's default event policy will block `pull_request_target` in public repositories from November 2, 2026, which would leave such a required check pending. |
| More checks inside the existing `pull_request` workflow (for example, comparing minimums with `main`) | **No.** The PR controls that code too.  It would at best catch accidental edits, which already show in the diff. | Not added. |
| `CODEOWNERS` file alone, without the rule | **No.** It requests reviews; it enforces nothing. | Not added. |

Conclusion: **no repository file can close this.**  It needs either an
organization-level required workflow (moving the repository into an
organization on a plan that offers it) or a second qualified reviewer with
write access.

## 4. Recommended settings

### Now (single maintainer)

Add a second ruleset rather than editing 25398695, so rollback is "delete
ruleset" and the required checks are untouched:

```json
{
  "name": "Protect main: pull requests, no force pushes, no deletion",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
  "bypass_actors": [],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": false,
      "allowed_merge_methods": ["merge", "squash", "rebase"]
    }}
  ]
}
```

```bash
gh api --method POST repos/Digvijay-Bhota/codeforge-ai/rulesets --input protect-main.json
gh api repos/Digvijay-Bhota/codeforge-ai/rules/branches/main   # verify
```

The same is available in the UI under Settings → Rules → Rulesets → New branch
ruleset.  It needs the repository **admin** role.  This JSON has not been
submitted to GitHub, so the API has not yet validated it; create it with
`"enforcement": "evaluate"` first if in doubt.

Effect:
* Every change to `main` goes through a pull request, so the required checks
  run on it.  Today no rule requires a pull request.  How GitHub evaluates a
  direct push against the required status checks alone was not tested.
* Force pushes and deletion of `main` are blocked.
* Zero approvals keeps merging possible for a single maintainer.

Risks:
* With no bypass actors, the owner is bound too.  Recovering from broken CI
  means editing the ruleset, which an admin can always do.
* Blocking force pushes also stops renaming the default branch without a
  bypass.
* Nothing in this repository pushes to `main` automatically today.

### When a second qualified reviewer has write access

1. Add `.github/CODEOWNERS` naming both reviewers for the trust-anchor paths (§5).
2. Change the pull-request rule: `required_approving_review_count: 1`,
   `require_code_owner_review: true`, `require_last_push_approval: true`.

Only then does a change to the workflow or the tests need someone else's
approval.  It still relies on that reviewer reading the diff, not the check.

## 5. Trust-anchor files

A change to any of these can change what the required gVisor check proves.
Review the diff itself, not the check result:

* `.github/workflows/gvisor-spike.yml`: triggers, job name, minimum test
  counts, `continue-on-error`, `if:` conditions, pinned SHAs and checksums;
* `.github/workflows/ci.yml`;
* `sandbox/gvisor/spike/check_junit.py`, `check_leftovers.py`,
  `register_runtimes.py`, `runtimes.json`;
* `sandbox/gvisor/spike/test_gvisor_spike.py`,
  `test_gvisor_executor_integration.py`;
* `apps/api/app/execution/gvisor_runtime.py`, `gvisor_executor.py`,
  `docker_executor.py`;
* `pyproject.toml` (pytest configuration).

## 6. What remains

* **F1 stays open** until §3's conclusion is met.  Until then, passing CI on a
  pull request that touches §5 is the author's own evidence.
* **Rulesets are settings, not code.**  An admin can change or disable them at
  any time, and nothing in this repository detects that.
