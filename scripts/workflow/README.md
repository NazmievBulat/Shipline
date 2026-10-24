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
points the link `~/.local/bin/shipline` at that launcher. `shipline uninstall` takes it all off
in one step: it puts back exactly the hooks and the timer the two replaced (kept in
`~/.claude/settings.json.shipline-replaced` and as `workflow-tick.*.before-shipline`), or
switches Shipline's timer off when there was none. A sentence that tells a session to run a
command names the launcher by its absolute path, so a session started before the switch runs
it too. The repository is the main checkout
`--repo` names, else `SHIPLINE_REPO`, else the one the current folder is in; outside any
repository, with none named, a command stops and says how to name one. A command started
outside the repository runs in its main checkout. Every session gets `SHIPLINE_REPO` and the
checkout's `bin` in its environment. The gate reads a repository's `run_proof.py` from its own
`scripts/workflow`; its other parts are the commands its `shipline.json` names (below,
"The repository's commands"), read from its main checkout; a missing one means none: no
check, no helper, no selected test (the panel then notes that CI runs the whole suite).

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
| `checks.dependency_audit` | the job that audits the dependencies against published advisories | none: `CI: dependencies` |
| `checks.advisory_annotation` | the title of the notice the audit writes on main when advisories alone failed it, a JSON list of `<advisory> in <package> (<summary>)`. With it, main failing only on such an advisory is `advisory`, not red, and the line keeps merging | none: an advisory makes main red |
| `annotations.*` | the titles of the planners' annotations the merge gate reads: `merge_tested`, `files_tested`, `server_tests`, `web_suite`, `browser_suite` | `Merge tested`, `Files tested`, `Server tests`, `Web suite`, `Browser suite` |
| `workflows.ci`, `workflows.e2e`, `workflows.images` | the workflow files under `.github/workflows/` | `ci.yml`, `e2e.yml`, `images.yml` |
| `labels.workflow`, `labels.audited` | the label that starts a ticket, and an audit's | `workflow`, `audited` |
| `labels.priority.blocker` … `.low` | the four priority labels | `priority: blocker` … `priority: low` |
| `runner.labels` | the runner labels the merge gate may re-run failed jobs on | the ones built from `runner.label`: `<label>`, `<label>-standby`, `<label>-first`, `<label>-main`, `<label>-main-batch`, `<label>-docker`, and `runner.planner_label`; without a label, none: the gate re-runs nothing |
| `runner.label` | the base label every machine's runners carry, so GitHub gives a job to whichever machine has one free (`runner/README.md`) | `<prefix>-home`; none without a prefix |
| `runner.planner_label` | the label the CI planner's runner carries | `<label>-planner` |
| `runner.prefix` | the one word the runner pool (`runner/`) names everything by: its images, network, services and containers, `<prefix>-runner…`, and its labels; `workflow-metrics` reads the CPU of the `<prefix>-runner…` services and containers | none: no runner figures |
| `runner.image_build` | the command line, run on main's files, that builds the runner images `<prefix>-runner` and `<prefix>-runner-docker` | none: the runner pool cannot be installed |
| `runner.hub_cache` | the command line, run on main's files, that runs the Docker Hub cache of the Docker-capable runners | none: no cache, and their virtual machines pull from Docker Hub |
| `runner.identity_folder` | the runner programs' own GitHub identity (`github_identity.py`); a copy of it installed without `workflow_config.py` beside it reads the folder from the `identity-folder` file beside it, which `runner/install.sh` writes, else uses the default | `~/.config/shipline-runner` |
| `merge.scratch_prefix` | the beginning of the merge gate's scratch branches, ended by `-` or `/`: the gate tests each prefix on one, named by this and 32 hexadecimal digits, and deletes only the branches it named; the example workflows run the gate's dispatches on them on main's runners | `workflow/merge-batch-` |
| `machines` | the machines `flow-page` samples for load and memory: a `name` each and, for another machine, the `address` `ssh` reaches it at (a destination, never a word starting with `-`); one without an address is this machine | none: only this machine |
| `hunts.areas` | the areas of the application two hunts may keep to at once: an object of each area's name (small letters, digits and dashes) to its screens, paths starting with `/` without a space, comma, quotation mark, dollar, backtick or backslash | none: one hunt runs at a time, on every screen |
| `hunts.plans` | the repository's file of hunt plans, which the sentence of a hunt with a plan names | none: the sentence names no file |
| `commands.test` | the command line that runs the repository's tests, from the root of its checkout: CI's test job runs it (`ci_proof.py test`), with the job's own arguments after it | none: the test job fails, saying so |
| `commands.browser` | the command line that runs the repository's browser tests, from the root of its checkout: E2E's `playwright` job runs it (`ci_proof.py browser`), with the job's own arguments after it | none: the browser job fails, saying so |

Change `merge.scratch_prefix` only while the merge line is empty: the gate deletes, and cancels
the runs of, only the scratch branches of the prefix it reads, so those of the old one would stay.
| `commands.*`, the others | the repository's own commands Shipline calls, each split into words (below) | none |
| `review.high_effort_paths` | regular expressions searched for in every path a pull request changes, both sides of a rename or deletion; a match runs its independent reviewer at high effort, not medium (`review-effort N`): stored data, text sent to a model, background work, locks and queues, account boundaries and security | none: only Shipline's own concurrent scripts and a description with the line `Review: high` |
| `answer_for_owner_after_minutes` | how long a question to the owner marked `Owner only: no` waits before the tick types its recommended reply (see `RULES.md`, "Asking the owner"); `0` turns that off, and it is at most `1440`, a day | `15` |

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
  file itself, each command as an array of its words, `SHIPLINE_COMMAND_HUNT_APP` and the
  others, empty when the repository names none, and exports `SHIPLINE_REPO`, the repository's
  main checkout, to every program the script starts.
- jq includes `priority.jq`, which reads the label names from `SHIPLINE_CONFIG` in the
  environment (export it first) with the same defaults.
- Node (`codex-bridge.mjs`) reads the state folder from the file with the same default.

The machine-wide state, `~/.local/state/workflow`, is shared by every repository on one
machine, as is its GitHub budget, and is not part of the file.

## The repository's commands

What only the repository knows (which tests a change reaches, how its application runs) it
gives Shipline as commands, under `commands` in `shipline.json`. Each but `test` (run by `bash`
from the root of the checkout, above) is a command line, split into words as the shell splits
them, with nothing expanded. A first word with a `/` in it is a
program in the repository's main checkout, whichever worktree runs it; a first word without
one is found on the `PATH`, and then the first later word with a `/` in it that is no option
is the script it runs, read from the main checkout too (`node tools/shipline/hunt-browser.mjs`):
a pull request's worktree never supplies the program. The command runs in the folder its caller is in, with the
arguments below after its own words. A command the file leaves out is one the repository does
not have, and Shipline does without it; a command it names but that cannot start stops the
call. A session runs one as `shipline <command>`, the name with dashes
(`shipline browser-tests-for-changes origin/main`).

| Command | Called by, with | Answers | Without it |
| --- | --- | --- | --- |
| `browser_tests_for_changes` | the review panel in a conflict round, and sessions: `<base> [--files-from <file>]` and the session's own words | the browser specs the change reaches, one path a line | nothing is selected, and CI runs the whole browser suite |
| `server_tests_for_changes` | the same: `<base> [--files-from <file>]` | one JSON object, `{"whole": bool, "tests": [paths], "total": int, "reasons": [str]}` | the whole server suite: `{"whole": true, "tests": [], "total": 0, "reasons": ["no server test selection is configured"]}` |
| `pre_train_checks` | the merge gate, before a pull request joins the merge train, in its worktree | exits non-zero when a check failed; what it prints is shown | every pull request is admitted |
| `mechanical_conflicts` | the merge gate's refresh: `resolve <head> <main>` with the merge of main in progress; `renumber <head> <main>` after a clean merge; `taken <head> <main>` before a batch | `resolve` stages its resolution, prints each file it resolved and exits 0 only when it resolved them all; `renumber` stages its moves and prints `<old path> <new path>` a line (paths without spaces), or exits non-zero, naming on standard output the files it cannot move (left as a conflict) and why on standard error; `taken` prints the branch's files whose number main took | every conflict is left to the session, nothing is renumbered and main took no number |
| `browser_plan` | the merge gate: the changed paths on standard input, each ended by a NUL | prints `needed` or `none` and exits 0; anything else, a crash or another exit included, counts as needed | every merge that changes a file needs browser tests while the merged tree holds the E2E workflow (`workflows.e2e`), and none does without it |
| `hunt_app` | `session` when a hunt stops: `stop`, in the hunt's worktree; the hunter itself, by `shipline hunt-app` | | `session start hunt` refuses |
| `hunt_browser` | the hunter, by `shipline hunt-browser`, which its sentence names | | the sentence names none |
| `hunt_cursor` | `session start hunt <name> <kind> cursor`: `<worktree> <report folder> <sentence>` | what it prints is kept in the report folder's `cursor.log` | a Cursor hunt is refused |
| `advisory_watch` | the merge gate: `unreachable` with a failed push's output, or a failed audit's annotations (without the advisories notice, `checks.advisory_annotation`), on standard input; `answers <service>` | `unreachable` prints the advisory service the text says was unreachable, or nothing; `answers` exits 0 when the service answers again. Beside the advisories notice, a service it names, or a failure of its own, keeps main red | no outage is read: a failed push is refused as it is, and main's advisories notice stands alone |
| `push_checkout` | the merge gate, before each push and after one that failed: `install <checkout>`; `refusal <checkout>` with the push's whole output on standard input | `install` installs what changed since its last install and exits non-zero with the reason when it failed, which is a wait; `refusal` prints the hook's failed step with its last lines | nothing is installed, and a failed push is refused with its last 10 lines |

The merge gate keeps the outage an advisory watch found in its state folder:
`advisory-service-down`, `{"service": ..., "since": "YYYY-MM-DD HH:MM UTC"}`, written by the
first push of an outage and removed when a push succeeds or the tick finds the service
answering, and `advisory-service.pending`, the line the wake tells once.

A command reads nothing but what is given on its standard input. Files a repository kept beside
the scripts before these commands existed, `scripts/workflow/pre-train-checks`,
`advisory_service.py`, `pushing_checkout.py`, `mechanical_conflicts.py`,
`server_tests_for_changes.py`, `browser_tests_for_changes.py` and `scripts/e2e-plan.py`, are no
longer read: each becomes a command named here, which answers in the form above. Its program now
lives in the main checkout whichever worktree runs it, so one that reads the change finds the
checkout from the folder it runs in (`git rev-parse --show-toplevel`), not from its own path.
