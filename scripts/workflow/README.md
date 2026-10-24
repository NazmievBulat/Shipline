# Workflow workers

The tick reads `WORKFLOW_WORKER_PROGRAM` from its environment first, then
`.local/workflow/worker-program` in the main checkout. The default is `claude`.
Set the environment value to `cloud`, or put the single word `cloud` in that file,
to start eligible labelled draft pull requests in the cloud at medium effort.
Use `claude` to switch back to local Claude, or `codex` for local Codex. Remove an
environment override before changing the file; the environment takes precedence.
Changing the setting affects future starts, leaving running sessions alone.

Cloud uses the same screen exclusions as Codex: screen changes and unclear tasks
with no diff stay with local Claude. Test IDs and data-only route lists retain the
existing exceptions. A cloud-built ticket returns to the cloud for a fix round,
even after the default changes, subject to the same screen exclusions.
A ticket's own record, the single word `cloud`, `claude` or `codex` in
`.local/workflow/worker-program-N`, overrides the default and these fix-round
markers for that ticket, under the same screen exclusions; only a cloud
reservation outranks it. A record holding anything else is read as no record,
and the wake names it once.
Independent reviewers always stay local.

Cloud starts bypass the local memory and load checks. The tick waits when the
5-hour usage exceeds 85% with more than an hour until reset, or usage is unreadable.
An existing cloud reservation prevents a local worker or reviewer from starting.
A launch refused before Claude was invoked is retried on a later tick, keeping
its reservation. A missing View address after invocation is ambiguous: the tick
keeps the reservation and retries address recovery from the saved launch output
and pane. It never creates a second session to replace an unknown one. Failures
are reported once through the wake, with pending reports retained until delivery.

The owner answers cloud questions in the cloud session. Its reports are relayed
from pull request comments, and its completion releases the reservation only once
the pull request is ready. `session list` shows addresses and report ages.

Dependency 614 supplies repository-local security rules for cloud pushes. The
orchestrator records the first three cloud tickets' independent review round counts
against Claude-built tickets and the PC's observed load in their descriptions;
those measurements require real workers after rollout and must not be estimated.

# The configuration file

What differs between the repositories that use Shipline is in one file, `shipline.json`,
at the root of the repository's main checkout. JSON, so Python, Node and jq read it with
nothing to install. `examples/shipline.json` names every key, for a repository
`example-app` with a server suite, a browser suite and an image check.

| Key | What it names | Default |
| --- | --- | --- |
| `repository` | `owner/name` on GitHub | `repository` as Bash reads it from `.github/runner/settings.sh` (sourced in a `bash` of its own, with only `PATH` and `HOME`, for at most 5 seconds), else the `origin` remote on GitHub; a script that needs it stops when neither names one |
| `state_folder` | the folder of the workflow's notes, in the main checkout and in each worktree | `.local/workflow` |
| `worktree_setup` | the command that prepares a new worktree, run only when present | `scripts/setup-worktree` |
| `checks.ci_result` | the check that sums up CI | `CI result` |
| `checks.ci_plan`, `checks.e2e_plan` | the planner checks of CI and of the browser suite | `CI: what to run`, `E2E: what to run` |
| `checks.images` | the prefix of every image check; its planner is `<prefix>: what to run` | `Docker images` |
| `annotations.*` | the titles of the planners' annotations the merge gate reads: `merge_tested`, `files_tested`, `server_tests`, `web_suite`, `browser_suite` | `Merge tested`, `Files tested`, `Server tests`, `Web suite`, `Browser suite` |
| `workflows.ci`, `workflows.e2e`, `workflows.images` | the workflow files under `.github/workflows/` | `ci.yml`, `e2e.yml`, `images.yml` |
| `labels.workflow`, `labels.audited` | the label that starts a ticket, and an audit's | `workflow`, `audited` |
| `labels.priority.blocker` … `.low` | the four priority labels | `priority: blocker` … `priority: low` |
| `runner.labels` | the runner labels the merge gate may re-run failed jobs on | none: the gate re-runs nothing |
| `runner.prefix` | the prefix the runner pool names its services and containers by; `workflow-metrics` reads their CPU | none: no runner figures |
| `runner.identity_folder` | the runner programs' own GitHub identity (`github_identity.py`); a copy of it installed without `workflow_config.py` beside it uses the default | `~/.config/shipline-runner` |
| `hunts.areas` | the areas of the application two hunts may keep to at once: an object of each area's name (small letters, digits and dashes) to its screens, paths starting with `/` without a space, comma, quotation mark, dollar, backtick or backslash | none: one hunt runs at a time, on every screen |
| `hunts.plans` | the repository's file of hunt plans, which the sentence of a hunt with a plan names | none: the sentence names no file |
| `commands.test` | the command line that runs the repository's tests, from the root of its checkout: CI's test job runs it (`ci_proof.py test`), with the job's own arguments after it | none: the test job fails, saying so |

Change `state_folder` only with every session stopped: running sessions keep their notes and
locks in the folder they started with, and the tick, the gate and `session` look for them in the
new one. The tick's own lock, `shipline-tick.lock` in the git common directory, does not move
with it, so two ticks never run at once across the change.

A key the file leaves out keeps its default, one by one, also inside `checks`, `labels` and the
others; without the file every value is its default. A key Shipline does not know, or a value
of the wrong kind, is refused with the key's name, and the script that read it stops.

How a script reads it:

- Python imports `workflow_config.py` and calls `workflow_config.current()` once a run.
- Bash reads every value at once, never one Python start per value:

  ```bash
  config=$(python3 "$here/workflow_config.py" shell) || exit 1
  eval "$config"
  ```

  which sets `SHIPLINE_REPOSITORY`, `SHIPLINE_STATE_FOLDER`, `SHIPLINE_CI_RESULT`,
  `SHIPLINE_LABEL_WORKFLOW`, `SHIPLINE_PRIORITY_HIGH` and the rest, and `SHIPLINE_CONFIG`, the
  file itself.
- jq includes `priority.jq`, which reads the label names from `SHIPLINE_CONFIG` in the
  environment (export it first) with the same defaults.
- Node (`codex-bridge.mjs`) reads the state folder from the file with the same default.

The machine-wide state, `~/.local/state/workflow`, is shared by every repository on one
machine, as is its GitHub budget, and is not part of the file.
