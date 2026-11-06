# PR 90 break tests

Base: `28e40b87f4befe7798ee1e463addd5f4359bbe23`.

These independent tests use the existing service stand-ins. They run no existing
test and make no real model or GitHub call. All generated repositories, state,
programs, and dependency files can stay inside the clone:

```sh
mkdir -p .local/break90/tmp
python3 -m venv .local/break90/venv
.local/break90/venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements-test.txt
TMPDIR="$PWD/.local/break90/tmp" .local/break90/venv/bin/python -m pytest -q -n 2 tests/break/test_break_90.py --basetemp=.local/break90/replay --tb=short
```

The retained Enter behavior is modeled; these tests do not diagnose why a real
model session could retain its original input.

## Results

34 distinct cases were exercised: 32 survived, two broke.

- The full file before the final lost-check case: `2 failed, 31 passed in 106.01s`.
- Final targeted confirmation, using `-k 'github or lost_check'`, after adding the
  lost-check case and making the API pause a normal one-hour reset:
  `2 failed, 4 passed in 24.14s`.

Survived: worker/reviewer status recovery with and without wait notes; two retained
initial Enters with wait removal; the three-attempt bound and single recorded
alert; changed input, multiline owner input, shell metacharacters, permission and
owner guards; an edit during settling; missing ownership notes; a held tick lock;
overlapping ticks; interruption after the retry count is persisted; missing,
pending, completed and partly rerun checks; a completed run whose check remains
running in the rollup; release after manual delivery; Escape-restored automation
and owner text; legacy and modern usage-reset recovery during an API pause.

## Blocking finding

`test_local_status_recovery_continues_when_github_is_unavailable` fails for both
`budget-pause` and `listing-failure`. An unchanged recorded reviewer status with
no wait note is eligible for `would-retry-status`, but three consecutive ticks
send no key. The regular recovery loop is after the successful pull-request
snapshot path. A budget pause exits through local duties, which recover only
usage-limit statuses; a listing error exits before the regular loop too.

Last assertion lines:

```text
AssertionError: GitHub budget-pause: three ticks returned [0, 0, 0]; expected local recovery Enter, got []
assert [] == ['Enter']
AssertionError: GitHub listing-failure: three ticks returned [1, 1, 1]; expected local recovery Enter, got []
assert [] == ['Enter']
```

Choice: a temporary API failure is a reachable recovery failure; the bounded
local retry should retain its existing input protections during that failure.

verdict: broke
