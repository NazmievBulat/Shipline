PR 95 breaker results
=====================

Reviewed head: `0c1c3d11f75fd54f58fdcc61cc7889365fe7c81c`.
Comparison base: `aa4b353b046b22973d55e88991816985dd6def6c`.

Only the two independent test files below were run. The existing suite was not
rerun. The command smoke tests generate one small probe file and run only that
file, including deliberately failing probes whose nonzero status is expected.
No production files or original tests were edited.

Setup, from the checkout root:

```sh
python3 -m venv .local/breaker-venv
.local/breaker-venv/bin/python -m pip install --no-cache-dir --require-hashes -r requirements-test.txt
mkdir -p .local/breaker-tmp
```

Run these commands separately:

```sh
TMPDIR="$PWD/.local/breaker-tmp" .local/breaker-venv/bin/python -m pytest breaker/95/test_shards.py -q --basetemp=.local/breaker-shards-tmp -o cache_dir=.local/breaker-cache --tb=short
```

Last line: `15 passed in 1.96s`.

```sh
TMPDIR="$PWD/.local/breaker-tmp" .local/breaker-venv/bin/python -m pytest breaker/95/test_check_failures.py -q --basetemp=.local/breaker-checks-tmp -o cache_dir=.local/breaker-cache --tb=short
```

Last line: `145 passed in 0.13s`.

`test_shards.py` verifies exactly-once file coverage, unchanged syntax trees for
all original driver tests, decorators and helpers, explicit fixture imports,
the CLI-dependent file's placement, hosted runners, read-only permissions,
failure isolation, the actual result-step shell, and the actual parallel
command with passing and failing probes at four and six workers.

`test_check_failures.py` replays every shard missing, duplicated, queued,
running, failed, cancelled and timed out against the real gate and proof
functions. Proof inheritance also rejects skipped shards. It covers two
simultaneous proof readers, failed-job reruns with carried planner notes,
an eighth shard missing from a rerun, unreadable jobs and annotations,
missing proof, changed trees, fork proof rejection, GitHub read deadlines,
and lost rollup checks on both sides of the grace period for all eight shards.
All GitHub responses in these replays are independent in-memory fixtures.

`observed-run.json` contains only selected public job metadata, read with `gh`
from run 42483227551. The run passed on the reviewed head with all eight jobs
on `ubuntu-latest`; elapsed shard times ranged from 11m 45s to 18m 08s.
The performance assertion uses the ticket's amended under-20-minute criterion,
which leaves the 30-minute cancellation limit intact. This is observed hosted
CI evidence, not a fresh benchmark or a guarantee for future runner loads.

Security and exposure review found no new secret, private identifier, unsafe
permission, shell injection, unbounded network call, or fork privilege in the
change. Original moved tests retain their syntax trees. Commit messages and
the PR description were also checked. No finding blocks the merge.

Choice: use the amended acceptance criterion recorded in the ticket; make no
product or model-text change. No lock test applies to this matrix-only change;
the independent concurrent-reader replay exercises its shared proof path.

verdict: survived
