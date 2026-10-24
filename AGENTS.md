# How Shipline is developed

`scripts/workflow/RULES.md` holds the rules Shipline sets for a repository that uses it: the
merge gate, the merge line and the sessions. Shipline itself is developed another way, by the
rules below.

1. Every change is a pull request to `main`. Nothing is pushed to `main` directly. Never
   force-push.
2. Every ticket is an empty draft pull request. The orchestrator opens it as a branch
   `ticket/<slug>` from `main` with one empty commit. Its description states the problem, what
   done means, what is out of scope and the files. It is the task's single source of truth. A
   job larger than the ticket becomes a new empty draft pull request, opened by the
   orchestrator.
3. Claude in the cloud builds each ticket, one cloud session per ticket, on that branch and pull
   request only; it never opens another. It adds its choices and runs below the ticket text.
4. GitHub CI runs every test, on GitHub-hosted runners only. A self-hosted runner is never used.
5. Codex reviews the change by reading only: an `@codex review` comment on the pull request. It
   runs no tests and nothing from the pull request.
6. Another cloud session, which did not build the change, writes and runs its own tests that
   try to break it: a lock held too long, a lost GitHub check, two calls at once, a partial
   re-run. It pushes them to a branch `break/<N>` only. It reports with one comment starting
   `<!-- shipline-breaker -->` and ending `verdict: survived` or `verdict: broke`. Re-running
   the suite is CI's job.
7. An independent Claude cloud reviewer decides. It never reads the builder's transcript. Its
   evidence is CI, the Codex review and the breaking tests. Nothing merges with a finding open.
   A send-back is a review starting `<!-- shipline-review result=sent-back -->`, with the pull
   request back to draft. A pass is a review starting `<!-- shipline-review result=passed -->`.
   When all is good, that reviewer squash-merges the pull request with GitHub's own merge.
   Shipline uses no merge gate or merge line of its own for itself.
8. Every commit is authored as "Bulat Nazmiev <82903523+NazmievBulat@users.noreply.github.com>",
   set with `git config user.name` and `git config user.email` before the first commit. Never
   any other address.
9. Never done without the owner's word: a force-push, a change to text that is sent to a model,
   and any product decision. `RULES.md` sets the same rule.
10. No personal data, secrets, tokens, host names, addresses, home paths or e-mail addresses in
    any file, commit or comment. Names in examples are neutral (`example-app`).
11. Outside contributions: issues are open now. Pull requests from others are taken after the
    first release, and the owner approves a newcomer's CI before it runs.

## Building a ticket

- Follow the existing code first: read how Shipline already does a thing and do the same.
- For anything the code does not answer, ask the orchestrator with a comment on the pull request
  starting `<!-- shipline-worker --> question`, then the question in one or two neutral lines.
  Carry on with what does not depend on it. The orchestrator answers in a comment starting
  `<!-- shipline-orchestrator -->` or by a message into the session. The builder never asks the
  owner.
- Decide only where neither answers. List each such choice, with one line of reason, under
  "Choices" in the description.
- Test first: for each piece, a failing test, run and seen red, then made green. The red and the
  green run (the command and the last lines) go in the description.
- The ready check: the whole suite passes, then one round of the builder's own review of the
  whole diff (a reviewer subagent), with its findings fixed. Then mark the pull request ready and
  comment `<!-- shipline-worker --> ready`.
- A blocker that cannot be passed: comment `<!-- shipline-worker --> Blocked` with the exact
  error, and stop.

## Review guidelines

Every reviewer, Codex included, follows these.

- Correctness first.
- Nothing in the diff, the commit messages or the pull request description exposes personal
  data, secrets or tokens, host or machine names, IP addresses, home paths, e-mail addresses
  other than a GitHub no-reply address, session or chat links, or details of any other project
  or local setup.
- Security:
  - injection through commands, paths or shell quoting;
  - secrets reaching logs or comments;
  - unsafe permissions on files the scripts write;
  - network calls without timeouts;
  - anything a fork's pull request could make the scripts run.
