Breaker evidence for PR 77
=========================

Target: `a4fe7de45d738879236e54c96bf78009bee1312a`.
Only independent breaker tests were executed, one file at a time. The existing
workflow harness supplies throwaway repositories and stand-ins; no live model,
notification service, or workflow timer is called.

Reproduce inside the checkout:

```sh
python3 -m venv .local/breaker-77/venv
.local/breaker-77/venv/bin/python -m pip install --require-hashes -r requirements-test.txt
mkdir -p .local/breaker-77/tmp
TMPDIR="$PWD/.local/breaker-77/tmp" .local/breaker-77/venv/bin/python -m pytest tests/workflow/test_break_77_capacity_viewport.py --basetemp=.local/breaker-77/viewport-tmp --tb=short -q
TMPDIR="$PWD/.local/breaker-77/tmp" .local/breaker-77/venv/bin/python -m pytest tests/workflow/test_break_77_capacity_flows.py --basetemp=.local/breaker-77/flows-tmp --tb=short -q
TMPDIR="$PWD/.local/breaker-77/tmp" .local/breaker-77/venv/bin/python -m pytest tests/workflow/test_break_77_capacity_checks.py --basetemp=.local/breaker-77/checks-tmp --tb=short -q
```

Findings
--------

1. The entire visible output is hashed as a stop identity. Resizing the pane can
   restart the five-minute wait or clear an existing delivery receipt. Conversely,
   a full pane of repeating prompt/error pairs can have identical visible text
   after a genuinely new stop and never get another retry. All three viewport
   tests fail:

   ```text
   same stop was submitted 2 times after viewport resize
   first stop reached five minutes, but retries = 0
   new capacity stop received no second retry; retries = 1
   3 failed in 24.34s
   ```

   The full-viewport test adjusts only its throwaway tmux stand-in to preserve
   historical prompt rows, which a real capture preserves.

2. A failed GitHub listing exits the tick before `local_duties`. An ordinary
   outage or a command timeout prevents both observation and delivery of the
   local capacity retry. These two tests fail:

   ```text
   GitHub outage blocked local retry; retries = 0
   GitHub timeout blocked local retry; retries = 0
   ```

   The timeout test supplies the bounded command's exit status 124; it does not
   wait on a real network timeout.

Survived
--------

Normal worker/reviewer retries and 5/15/30/30-minute backoff; one wake per repeated
stop; a held action lock; two overlapping ticks; pending owner replies, owner
blocks and drafts in either pane; swallowed Enter recovery; shadow observation;
noninteractive hunters; capacity notifications; unreadable captures; lost,
partial and late check rollups; a custom state folder; absent, empty, truncated
and malformed receipts. No additional exposure or security finding was found in
the target diff, commit messages or PR description.

The flow file's initial run was `1 failed, 14 passed in 117.03s`; its corrected
notification fixtures and outage diagnostic were subsequently checked with a
targeted selection in the same file. The checks file was
`1 failed, 7 passed in 47.65s`.
The final flow selection was `1 failed, 2 passed, 12 deselected in 1.73s`.
Across the 26 independent cases, 21 survived and 5 failed.

No implementation file was changed. No existing suite was run.

verdict: broke
