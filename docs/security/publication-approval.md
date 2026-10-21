# Publication approval for generated changes (CF-05)

Pushing a branch to a GitHub repository, or opening a pull request from a
branch in it, runs that repository's CI with its secrets. CI runs whatever the
changes contain: tests, build and package scripts, dependencies, workflows. It
can also read the pull request's text (`github.event.pull_request.body`).
CodeForge therefore never pushes generated changes until a person with write
access to the repository has approved the exact changes.

## Lifecycle

1. **Generation.** The worker clones the repository and runs the agent. It
   creates no branch and pushes nothing. When the agent finishes, the worker
   stores an *artifact*: repository, base branch and commit, every changed
   file's full content (or its deletion), and the commit message and pull
   request title and body that would be published. The artifact is
   identified by a sha256 digest over all of those fields. The task moves to
   `WAITING_PUBLICATION`, with a pending publication approval that expires
   after the task's `approval_config.timeout_hours` (24 hours by default).
   The GitHub check run, if any, stays in progress.
2. **Review.** `GET /api/v1/tasks/{task_id}/publication` (read access) returns
   the artifact: the files and their full contents, the commit message, the
   pull request title and body, the digest, and the approval status.
3. **Decision.**
   - `POST /api/v1/tasks/{task_id}/publication/approve` with
     `{"artifact_digest": "<digest from step 2>", "comment": "..."}` (write
     access and a linked GitHub account). The digest must match the stored
     artifact, so you approve what you reviewed. The approver is recorded
     from the session. Repeating the same approval has no further effect.
     The task moves to `PUBLISHING` and one publication job is queued.
   - `POST /api/v1/tasks/{task_id}/publication/reject` with
     `{"reason": "..."}` (write access). The task is `CANCELLED`, nothing is
     pushed, and the check run fails.
4. **Publication.** The publication job checks the approval again: the
   approval matches the stored artifact's digest, the artifact is intact,
   and the approver is still active and still has write access. It then
   writes the stored files onto the base commit and checks that they
   reproduce the digest. Only then does it create branch
   `codeforge/task-<task_id>`, commit (with fixed dates, so a retry makes the
   same commit), push, and open the pull request, or reuse one that is
   already open. The approver's active status and write access are checked
   again, bypassing the permission cache, immediately before creating the
   branch, before pushing, and before opening the pull request. This narrows
   the window for a revocation to take effect; it is not atomic, because
   GitHub permissions can change right after any check. If a check fails,
   nothing further is published. A branch already pushed by then stays in
   the repository. The pull request body is the reviewed body plus a trailer
   naming the digest and the approver. Temporary GitHub failures are retried
   under the same approval, up to the job attempt limit. Any other failure
   ends the task as `FAILED` (`PUBLICATION_FAILED`).
5. **Expiry.** An unanswered request is cancelled by the worker's periodic
   recovery pass (about once a minute) once it expires, or immediately if
   someone tries to approve it after expiry. Expiry never publishes.

Paths under `.github/`, and files that look like secrets, are refused at
generation and again at publication. This is defense in depth only. The
approval is the control, because CI also runs ordinary code.

## Limitations

- Plan approval (`WAITING_APPROVAL`, `POST /api/v1/tasks/{id}/approve`) is a
  separate, earlier gate, required for every GitHub task (the worker only
  plans until the plan is approved). Approving a plan queues implementation
  and never approves publication. The console keeps the two apart.
- Reviewers use the review console (`apps/web`) or the API above. The
  console shows the exact artifact and approves its digest; it holds no
  authority of its own. The check run does not ask for approval: watch for
  `WAITING_PUBLICATION` (the console's "Awaiting review" filter).
- Approval means a person with write access chose to publish these exact
  changes. CodeForge cannot make that review thorough. The task's creator
  may approve it if they have write access.
- A publication that fails permanently cannot be approved again. Retry the
  task, which generates and reviews new changes.
- Changes are published onto the base commit they were generated from, so
  the pull request may need a rebase.
- If GitHub cannot be asked for the approver's permission at one of those
  checks, publication fails closed and the task must be retried.
- Only text files are supported. A change containing a NUL character cannot
  be stored for review, and the task fails.
