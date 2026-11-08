Independent breaker results for PR 93
=====================================

Reviewed head: `2e0ba4116a323ff5ce1c1626c8acd5e000bec85f`.
Test file: `tests/workflow/test_break_93.py` on `break/93`.

Run this file alone, with the pinned dependencies installed in a virtual environment:

```sh
python -m pytest tests/workflow/test_break_93.py --basetemp=.local/break-93-tmp --no-header --tb=short -q
```

Result: **1 failed, 33 passed in 3.30s**. The existing suite was not run.

Finding: passed ancestor skipped after rollback and a cancelled CI re-run
------------------------------------------------------------------------

`test_safe_ancestor_is_still_taken_after_rollback_and_failed_ci_rerun` replays:

1. The installed version is A. Versions B and C land, both with passed CI.
2. The updater installs C. Three tick crashes cause the existing rollback to A.
3. CI on C is re-run during diagnosis; attempt two is cancelled. B still has passed CI.
4. After the fetch interval, the updater should take B, excluding C as known bad.

It stays at A. `update()` returns immediately when the fetched head is in `bad`, before
walking first parents. This skips B even though B is a clean fast-forward, on main, newer
than A, with passed CI. The existing rollback itself succeeds. Selection should exclude
the bad commit while continuing to inspect eligible ancestors.

Failing output, last assertion lines:

```text
E   AssertionError: eligible passed ancestor is skipped when main head is known bad
E   assert 0 == 10
FAILED tests/workflow/test_break_93.py::test_safe_ancestor_is_still_taken_after_rollback_and_failed_ci_rerun
1 failed, 33 passed in 3.30s
```

Surviving cases
---------------

- Newest passed first-parent selection with failed, cancelled, pending, or missing head CI;
  late success then advances to head. A passed side branch and a known-bad ancestor are excluded.
- Missing CI starts the one-hour clock on first observation, waits strictly over an hour,
  and reports once. Missing or damaged timing notes recover. Fallback notices name the actual
  installed commit. The latest pending CI supersedes an older passed run.
- Cancelled, runner-failed, and timed-out runs retry once. The retry claim has mode 0600.
  Failed tests on later job or annotation pages, or in a cancelled log, block retries.
- Partial re-runs are not retried again; later success updates. Budget refusal releases the
  claim, while ambiguous request timeout or OS error retains it. Two simultaneous retry calls
  issue one request.
- GitHub read failures and timeouts leave the checkout unchanged. A held update lock returns
  without waiting. A second tick arriving during CI inspection prevents movement.
- Local edits block fallback. Unrelated event, branch, workflow, or SHA cannot authorize it.
  An untrusted conclusion becomes `unknown` in the wake and does not execute shell text.
- The exact installed-commit read block from wake clears inherited Git repository variables
  and reads the installation's commit. The message includes that field.

Boundaries: these are independent tests using real local Git repositories and a stand-in
for GitHub calls. Process identity and installation discovery are isolated from the changed
logic. Wake's changed read block is exercised in isolation. No real CI re-runs are requested.
The diff, PR description, and commit messages were checked for exposure; changed commands
were checked for injection, permissions, timeouts, and execution of fork-controlled code.
No additional security or exposure finding was identified.

Choice: apply the existing bad-version exclusion to the candidate commit; preserving rollback
does not require excluding other passed commits. This is the selection expectation used by
the failing test.

verdict: broke
