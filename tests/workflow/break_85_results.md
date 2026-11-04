# PR 85 breaker results

Reviewed head: `fcb8db1836630c242c92ce735f7b1e7c55bbd3bf`.
Branch: `break/85`. Tests: `tests/workflow/test_break_85.py`.
Only independent tests were run, one file at a time. Production files were not changed.

The complete independent file finished with **5 failed, 23 passed in 110.21s**.
After increasing the test-only merge deadline from 0.25s to 0.75s and asserting that the
write had actually started, the timeout case was run separately and still failed.

To replay inside a checkout, create a virtual environment from `requirements-test.txt`,
then run this file alone. Keep the temporary directory and pytest cache inside the checkout:

```bash
mkdir -p .local/break85/tmp
python3 -m venv .local/break85/venv
.local/break85/venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements-test.txt
TMPDIR="$PWD/.local/break85/tmp" .local/break85/venv/bin/python -m pytest -q tests/workflow/test_break_85.py --basetemp=.local/break85/replay -o cache_dir=.local/break85/pytest-cache --tb=short
```

## Reachable failures

1. **Batch squashes bypass the new outage handling.**
   `test_a_tested_batch_keeps_handoffs_when_a_squash_gets_502[7]` prepares three reviewed
   members, finishes their prefix checks, and returns the incident's 502 from the first
   squash. The batch path exhausts its existing attempts, drops the prefixes, and revokes
   the first member's handoff. This is the same repeated-review failure the ticket seeks
   to prevent. With `[8]`, the first member merges and the next gets 502; its handoff stays,
   but no outage notice is queued. Both cases broke.

   ```text
   merge hand-back 7: merge refused: the squash of batch member 7 failed: non-200 OK status code: 502 Bad Gateway body: "<html>502 Bad Gateway</html>"
   AssertionError: a GitHub 502 during batch squash revoked the reviewed handoff
   AssertionError: a GitHub 502 during batch squash did not queue the outage notice
   ```

   The missed route is `merge_squash._batch_squash`, called by
   `merge_scratch._merge_prefixes`; it does not reach the changed direct-merge handler.

2. **Successful batch merges leave the new outage marker set.**
   `test_successful_batch_clears_the_marker_so_a_later_outage_can_be_reported` starts with
   the marker of an earlier outage and successfully merges all three tested batch members.
   The marker survives, so `note_github_server_errors` suppresses a later outage. Broke.

   ```text
   AssertionError: successful batch merges left the outage marker set
   ```

3. **The first server failure of a lost-check recovery push revokes its handoff.**
   `test_lost_check_recovery_push_keeps_the_review_during_server_failure[True]` leaves a
   check running after its successful run completed, then rejects the gate's recovery
   commit with the named Bad Gateway receive-pack error. `_push_refreshed` recognizes the
   server failure, but cannot retry because `rerun_lost` supplies no saved refresh. It
   immediately hands back. The same recovery without an outage (`[False]`) survives.
   This is an existing retry-state gap exposed with a newly supported incident format,
   not a newly introduced regression. Broke.

   ```text
   merge hand-back 7: merge refused: could not push the refreshed branch: ! [remote rejected] HEAD -> ticket/example-app (Bad Gateway)
   AssertionError: a server error on the lost-check recovery push revoked the passed handoff
   ```

4. **A merge deadline bypasses reconciliation.**
   `test_merge_deadline_keeps_handoff_and_reads_back_the_write` lets the merge request
   start, then withholds its response. Only that command's deadline is shortened in the
   test; the real cancellation path runs. `TimedOut` reaches the refusal handler instead
   of reading the pull request back and waiting. Exit 1 is returned instead of 3. This
   predates the PR and is an adjacent reachable gap, separate from the named 502 formats.
   Broke. The temporary argument and commit hash are elided below.

   ```text
   merge refused: gh pr merge ... timed out after 0.75s
   AssertionError: a merge deadline became a refusal
   assert 1 == 3
   ```

## Surviving cases

All names below refer to tests in `tests/workflow/test_break_85.py` on `break/85`.

| Test | Replay | Result |
| --- | --- | --- |
| `test_incident_formats_and_mixed_client_failures` | Six incident forms; each combined in either order with four client-error forms | survived |
| `test_server_preamble_does_not_override_a_real_branch_rejection` | Server preamble with non-fast-forward, hook or protection rejection | survived |
| `test_merge_outage_repeats_without_losing_review_then_recovers` | Two failed direct-merge ticks, one wake notice, then successful recovery | survived |
| `test_accepted_merge_requires_readback_of_the_expected_head` | A 502 hides a completed merge, including a changed-head control | survived |
| `test_failed_readback_preserves_handoff_and_next_tick_reconciles` | Readback unavailable after an accepted or unaccepted merge; next tick recovers | survived |
| `test_two_callers_behind_a_busy_merge_lock_keep_one_notice` | Lock held six seconds, two callers, one private notice | survived |
| `test_refresh_response_loss_reuses_the_saved_commit_and_review` | Rejected or already accepted refresh push; next tick reuses its commit | survived |
| `test_lost_check_recovery_push_keeps_the_review_during_server_failure[False]` | Successful lost-check recovery control | survived |
| `test_partial_ci_rerun_and_late_metadata_still_wait_after_an_outage` | Some jobs reused, one still running, then jobs green before run metadata catches up | survived |
| `test_new_notice_folders_are_private_and_a_missing_marker_can_be_rebuilt` | Missing state directories or marker; files have no group or other access | survived |
| `test_wake_delivery_failure_does_not_lose_the_outage_notice` | No recipient initially; delivery succeeds at the next allowed retry | survived |
| `test_error_body_is_not_executed_or_copied_into_the_outage_notice` | Shell-shaped error body; only the fixed notice reaches the pending file | survived |
| `test_a_removed_handoff_cannot_authorize_a_retry` | Authorization disappears between failed merge calls | survived |

## Review and choices

The PR diff, commit messages, description, and touched call paths were checked for exposure,
command injection, error output, file modes, network deadlines, and fork execution. No
additional security or exposure finding was established. The new notice is fixed text and
uses private files; changed command arguments remain separate shell-free arguments. CI's
placement changes keep hosted runners and read-only permissions.

The existing three-attempt refresh limit was not treated as a finding: the ticket explicitly
keeps retry counts out of scope. Batch calls and lost-check recovery were exercised as real
users of the same merge and push gate. The timeout gap is identified as pre-existing. No
production or model text, product policy, merge, force-push, or extra review round was needed.

verdict: broke
