# PR 92 independent break results

Target: `a7bef04c72c1e5454f5f5014300e3119b1131a04`.

The test service replaces curl; the usage script, configuration loader, ticket-start
check, cloud-start check and limit-reset helper are unchanged. Each probe creates an
isolated example installation. No GitHub or model calls run from these tests.

Install `requirements-test.txt` into a virtual environment inside the checkout, then run
this file alone:

```sh
mkdir -p .local/break92/tmp
TMPDIR="$PWD/.local/break92/tmp" .venv-break-92/bin/python -m pytest tests/workflow/test_break_92.py --basetemp=.local/break92/pytest-final -q --tb=short
```

Final output:

```text
......................................                                   [100%]
38 passed in 17.00s
```

The probes cover daily callers, shared rate limiting and recovery, HTTP dates and
malformed headers, transport timeouts and invalid payloads, a held lock, ten concurrent
callers, a killed request, atomic retention of the previous answer, expiry boundaries,
missing files, fresh status-cache precedence, explicit null windows, read-only shadow
calls, private permissions, credential exclusion, shell quoting, multiple header blocks,
cold failures, damaged JSON, the start ceiling and reset boundary, worktrees with external
configuration, and the short limit-reset deadline.

Choices: use the ticket's documented minimum pause of 300 seconds even for a shorter
Retry-After; accept silent failure for a cold lock contender until a good answer exists.
Both follow the target's stated behavior. No product or model-text changes were needed.

The target's aggregate CI and all eight server shards were successful when checked.
No existing test file or whole suite was run locally. The target diff, PR description and
commit messages were also inspected for exposure and security issues; none were found.

verdict: survived
