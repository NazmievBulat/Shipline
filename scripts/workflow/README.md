# Running Shipline

Shipline runs from a checkout of its own, kept apart from the repository it works on, which
holds none of its scripts. Clone it to `~/.local/share/shipline`, or elsewhere: a copy of
`bin/shipline` kept outside any checkout runs the one `SHIPLINE_HOME` names. A repository
calls it as `shipline <command>`: its rules, hooks and package scripts never name a path
inside Shipline. The first time, the launcher is run by its path:

```bash
~/.local/share/shipline/bin/shipline --repo /path/to/example-app install-timer
~/.local/share/shipline/bin/shipline --repo /path/to/example-app install-hooks
```

Each of the two writes the launcher of this checkout and the repository's main checkout, and
points the link `~/.local/bin/shipline` at that launcher. The repository is the main checkout
`--repo` names, else `SHIPLINE_REPO`, else the one the current folder is in; outside any
repository, with none named, a command stops and says how to name one. A command started
outside the repository runs in its main checkout. Every session gets `SHIPLINE_REPO` and the
checkout's `bin` in its environment. The files a repository keeps for the gate and the panel
(`pre-train-checks`, `advisory_service.py`, `pushing_checkout.py`, `run_proof.py`,
`mechanical_conflicts.py`, `server_tests_for_changes.py`, `browser_tests_for_changes.py`) are
read from its own `scripts/workflow`, and its E2E planner from `scripts/e2e-plan.py`; a
missing one means none: no check, no helper, no selected test (the panel then notes that CI
runs the whole suite).

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
| `runner.labels` | the runner labels the merge gate may re-run failed jobs on | built from `runner.prefix` and the machines in `~/.config/shipline/runner.sh` (`runner/README.md`); without a prefix, none: the gate re-runs nothing |
| `runner.prefix` | the one word the runner pool (`runner/`) names everything by: its images, network, services and containers, `<prefix>-runner…`, and its labels; `workflow-metrics` reads the CPU of the `<prefix>-runner…` services and containers | none: no runner figures |
| `runner.image_build` | the command line, run from the main checkout, that builds the runner images | none: the pool builds its own Dockerfile |
| `runner.label` | the label every machine's runners share; main's runners, on the one machine that has them, carry `<label>-main`, which the example workflows name, and the merge line's first member asks for `<label>-first` | `<runner.prefix>-home`; none without a prefix |
| `runner.identity_folder` | the runner programs' own GitHub identity (`github_identity.py`); a copy of it installed without `workflow_config.py` beside it uses the default | `~/.config/shipline-runner` |
| `merge.scratch_prefix` | the beginning of the merge gate's scratch branches, ended by `-` or `/`: the gate tests each prefix on one, named by this and 32 hexadecimal digits, and deletes only the branches it named; the example workflows run the gate's dispatches on them on main's runners | `workflow/merge-batch-` |
| `commands.test` | the command line that runs the repository's tests, from the root of its checkout: CI's test job runs it (`ci_proof.py test`), with the job's own arguments after it | none: the test job fails, saying so |
| `commands.browser` | the command line that runs the repository's browser tests, from the root of its checkout: E2E's `playwright` job runs it (`ci_proof.py browser`), with the job's own arguments after it | none: the browser job fails, saying so |

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
  `SHIPLINE_LABEL_WORKFLOW`, `SHIPLINE_PRIORITY_HIGH` and the rest, `SHIPLINE_CONFIG`, the
  file itself, and exports `SHIPLINE_REPO`, the repository's main checkout, to every program
  the script starts.
- jq includes `priority.jq`, which reads the label names from `SHIPLINE_CONFIG` in the
  environment (export it first) with the same defaults.
- Node (`codex-bridge.mjs`) reads the state folder from the file with the same default.

The machine-wide state, `~/.local/state/workflow`, is shared by every repository on one
machine, as is its GitHub budget, and is not part of the file.
