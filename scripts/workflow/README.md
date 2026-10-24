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
A cloud worker whose draft has had no push and no report for
`cloud_wake_after_minutes` (30), with no open question, is woken once by the tick
through `claude --cloud <address>` in a short-lived pane; an answered question wakes
it at once. One still quiet that long after is named in the wake once.

Dependency 614 supplies repository-local security rules for cloud pushes. The
orchestrator records the first three cloud tickets' independent review round counts
against Claude-built tickets and the PC's observed load in their descriptions;
those measurements require real workers after rollout and must not be estimated.

# The configuration file

Shipline reads one JSON file, using the first available source in this order:

1. `SHIPLINE_CONFIG`, a file path. A relative path is relative to the caller's current folder;
   an explicit file that is missing or unreadable is refused.
2. `$XDG_CONFIG_HOME/shipline/<owner>-<name>/shipline.json`, with `~/.config` as the config home
   when `XDG_CONFIG_HOME` is unset or empty. The repository identity for this lookup is the
   runner settings' `repository`, else the GitHub `origin`, before any config is read.
3. `shipline.json` in the repository's main checkout, as before.

Only that file is read; files are never merged. Without any file the defaults apply. An invalid
installation file is refused with its own path, without falling through to the checkout file.
Worktrees use their main checkout's identity and config, never a worktree's config copy.
`examples/shipline.json` names every key for `example-app` with a server suite, a browser suite
and an image check. A repository needs no config or adapter scripts of its own.

The installation folder is the parent folder of a file selected by `SHIPLINE_CONFIG` or from
the home config folder. The optional `install_folder` key overrides it: an absolute path, or a
path relative to the selected config file. A checkout config has no installation folder unless
it names one. Programs and a `parity.known` file can live there. Relative program paths still
use the main checkout first, then the installation folder; an `@install/` prefix uses only the
installation folder, even if the checkout has a file of the same name.

| Key | What it names | Default |
| --- | --- | --- |
| `repository` | `owner/name` on GitHub | `repository` as Bash reads it from `.github/runner/settings.sh` (sourced in a `bash` of its own, with only `PATH` and `HOME`, for at most 5 seconds), else the `origin` remote on GitHub; a script that needs it stops when neither names one |
| `install_folder` | the folder of installation programs; absolute, or relative to the selected config file | the parent of an external config; none for a checkout config |
| `state_folder` | the folder of the workflow's notes, in the main checkout and in each worktree | `.local/workflow` |
| `worktree_setup` | the command that prepares a new worktree, run only when present | `scripts/setup-worktree` |
| `refresh.clear_generated` | generated output a refresh clears in the pull request's worktree after it merges main and before it pushes: paths or globs (`*`, `?`, `[…]`, `**`) from the top of the checkout; only the ignored, untracked files under them are removed, and a path that is tracked, not ignored, outside the checkout or through a symbolic link is refused when the file is read | none: nothing is cleared |
| `checks.ci_result` | the check that sums up CI | `CI result` |
| `checks.ci_plan`, `checks.e2e_plan` | the planner checks of CI and of the browser suite | `CI: what to run`, `E2E: what to run` |
| `checks.images` | the prefix of every image check; its planner is `<prefix>: what to run` | `Docker images` |
| `checks.dependency_audit` | the job that audits the dependencies against published advisories | none: `CI: dependencies` |
| `checks.advisory_annotation` | the title of the notice the audit writes on main when advisories alone failed it, a JSON list of `<advisory> in <package> (<summary>)`. With it, main failing only on such an advisory is `advisory`, not red, and the line keeps merging | none: an advisory makes main red |
| `annotations.*` | the titles of the planners' annotations the merge gate reads: `merge_tested`, `files_tested`, `server_tests`, `web_suite`, `browser_suite` | `Merge tested`, `Files tested`, `Server tests`, `Web suite`, `Browser suite` |
| `workflows.ci`, `workflows.e2e`, `workflows.images` | the workflow files under `.github/workflows/` | `ci.yml`, `e2e.yml`, `images.yml` |
| `labels.workflow`, `labels.audited` | the label that starts a ticket, and an audit's | `workflow`, `audited` |
| `labels.priority.blocker` … `.low` | the four priority labels | `priority: blocker` … `priority: low` |
| `runner.labels` | the runner labels the merge gate may re-run failed jobs on | the ones built from `runner.label`: `<label>`, `<label>-standby`, `<label>-first`, `<label>-main`, `<label>-docker`, and `runner.planner_label`; without a label, none: the gate re-runs nothing |
| `runner.label` | the base label every machine's runners carry, so GitHub gives a job to whichever machine has one free (`runner/README.md`) | `<prefix>-home`; none without a prefix |
| `runner.planner_label` | the label the CI planner's runner carries | `<label>-planner` |
| `runner.prefix` | the one word the runner pool (`runner/`) names everything by: its images, network, services and containers, `<prefix>-runner…`, and its labels; `workflow-metrics` reads the CPU of the `<prefix>-runner…` services and containers | none: no runner figures |
| `runner.image_build` | the command line, run on main's files, that builds the runner images `<prefix>-runner` and `<prefix>-runner-docker` | none: the runner pool cannot be installed |
| `runner.hub_cache` | the command line, run on main's files, that runs the Docker Hub cache of the Docker-capable runners | none: no cache, and their virtual machines pull from Docker Hub |
| `runner.identity_folder` | the runner programs' own GitHub identity (`github_identity.py`); a copy of it installed without `workflow_config.py` beside it reads the folder from the `identity-folder` file beside it, which `runner/install.sh` writes, else uses the default | `~/.config/shipline-runner` |
| `machines` | the machines `flow-page` samples for load and memory: a `name` each and, for another machine, the `address` `ssh` reaches it at (a destination, never a word starting with `-`); one without an address is this machine | none: only this machine |
| `hunts.areas` | the areas of the application two hunts may keep to at once: an object of each area's name (small letters, digits and dashes) to its screens, paths starting with `/` without a space, comma, quotation mark, dollar, backtick or backslash | none: one hunt runs at a time, on every screen |
| `hunts.plans` | the repository's file of hunt plans, which the sentence of a hunt with a plan names | none: the sentence names no file |
| `commands.test` | the command line that runs the repository's tests, from the root of its checkout: CI's test job runs it (`ci_proof.py test`), with the job's own arguments after it | none: the test job fails, saying so |
| `commands.*`, the others | the repository's own commands and answer forms Shipline calls (below) | none |
| `review.high_effort_paths` | regular expressions searched for in every path a pull request changes, both sides of a rename or deletion; a match runs its independent reviewer at high effort, not medium (`review-effort N`): stored data, text sent to a model, background work, locks and queues, account boundaries and security | none: only Shipline's own concurrent scripts and a description with the line `Review: high` |
| `answer_for_owner_after_minutes` | how long a question to the owner marked `Owner only: no` waits before the tick types its recommended reply (see `RULES.md`, "Asking the owner"); `0` turns that off, and it is at most `1440`, a day | `15` |
| `cloud_wake_after_minutes` | the minutes without a push or a report after which the tick wakes a cloud worker by typing into its session; `0` never does | `30` |
| `hot_paths` | path patterns of the few files many tickets edit, by GitHub's path filter rules (`*` stays inside a folder, `**` crosses folders): two tickets whose files match the same one never run at once, a pull request waiting in the merge line counting as running, and the merge line serves members sharing one back to back within their priority | none |
| `conflict_review` | `{"effort": ..., "model": ..., "max_changed_lines": N}`: the reviewer of a conflict on a handed-off pull request whose conflicting hunks, and then whose resolution, stay within `N` lines of the conflicting files and touch no other file; a larger one gets the usual reviewer | none: Claude Opus at high effort for every conflict |

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
  selected file's absolute path (empty when no file exists), `SHIPLINE_INSTALL_FOLDER`, the
  absolute installation folder (empty without one), `SHIPLINE_CONFIG_JSON`, the file's JSON
  contents, each command as an array of its words, `SHIPLINE_COMMAND_HUNT_APP` and the
  others, empty when the repository names none, and exports `SHIPLINE_REPO`, the repository's
  main checkout, to every program the script starts.
  The installation folder and JSON payload are exported too. An explicit `SHIPLINE_CONFIG`
  is exported as an absolute path so child processes keep it after changing folders; otherwise
  each child uses the same lookup order. Installed hooks and timers preserve an explicit config
  path and `XDG_CONFIG_HOME` for their future runs.
- jq includes `priority.jq`, which reads labels from the exported `SHIPLINE_CONFIG_JSON`
  payload with the same defaults; `SHIPLINE_CONFIG` always means a path.
- Node (`codex-bridge.mjs`) calls `workflow_config.py json` to use the same lookup and validation,
  then reads the state folder with the same default.

The machine-wide state, `~/.local/state/workflow`, is shared by every repository on one
machine, as is its GitHub budget, and is not part of the file.

## The repository's commands

What only the repository knows (which tests a change reaches, how its application runs) it
gives Shipline as commands, under `commands` in `shipline.json`. Each but `test` (run by `bash`
from the root of the checkout, above) is a command line, split into words as the shell splits
them, with nothing expanded. A first word with a `/` in it is a
program resolved from the repository's main checkout first, then the installation folder,
whichever worktree runs it. A first word without one is found on the `PATH`, and then the first
later word with a `/` in it that is no option is the script it runs, resolved the same way
(`node tools/shipline/hunt-browser.mjs`). Use `python3 @install/tools/check.py` to require an
installed script, or `@install/tools/check` for an installed executable; neither falls back to
the checkout. With no installation folder, `@install/` is refused. A pull request's worktree
never supplies the program. The command runs in the folder its caller is in, with the
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

Each entry may instead be an object with `run` and `form`. Plain strings keep the documented
answers above. `run` is split and located as above; optional `args` is a list of literal words
added after `run`'s words and before Shipline's arguments. No shell expands those words.
`commands.test` keeps its existing shell command semantics, including when written as an
object; its `args` are still literal words passed before the job's arguments.

| Form | Commands | Meaning |
| --- | --- | --- |
| `lines` | any | The documented answer, passed through unchanged (including the server selector's JSON). |
| `exit` | `test`, `pre_train_checks`, `hunt_app`, `hunt_browser`, `hunt_cursor`, `push_checks` | The documented output and exit code, passed through unchanged. |
| `functions` | `mechanical_conflicts`, `advisory_watch`, `push_checkout`, `browser_plan` | `run` is a Python file; `functions` maps the calls below to its function names. |
| `json` | `browser_tests_for_changes` | `field` names the JSON object's list of spec paths, printed one a line. |
| `json` | `server_tests_for_changes` | `fields` maps all four documented keys (`whole`, `tests`, `total`, `reasons`) to keys in the script's JSON object. |

For example, existing selectors may print differently named JSON keys or require leading
arguments, while checks keep their exit code:

```json
{
  "commands": {
    "browser_tests_for_changes": {
      "run": "tools/select-browser", "form": "json", "field": "specs", "args": ["--ci"]
    },
    "server_tests_for_changes": {
      "run": "python3 tools/select-server.py", "form": "json",
      "fields": {"whole": "all", "tests": "selected", "total": "count", "reasons": "why"}
    },
    "pre_train_checks": {"run": "tools/check-change", "form": "exit"},
    "hunt_app": {"run": "tools/hunt-app", "form": "exit"},
    "hunt_browser": {"run": "node tools/hunt-browser.mjs", "form": "exit"},
    "browser_plan": {"run": "tools/browser-plan", "form": "lines"}
  }
}
```

A function form needs no command-line entry point or adapter in the repository:

```json
{
  "commands": {
    "mechanical_conflicts": {
      "run": "tools/conflicts.py", "form": "functions",
      "functions": {"resolve": "resolve", "renumber": "renumber_files", "taken": "taken_files"}
    },
    "advisory_watch": {
      "run": "tools/advisories.py", "form": "functions",
      "functions": {"unreachable": "unreachable", "answers": "answers"}
    },
    "push_checkout": {
      "run": "tools/checkout.py", "form": "functions",
      "functions": {"install": "install_changed", "refusal": "refusal"}
    },
    "browser_plan": {
      "run": "tools/plan.py", "form": "functions",
      "functions": {"browser_plan": "browser_needed"}
    }
  }
}
```

The complete function maps and their arguments are:

| Command | Map keys | Python arguments |
| --- | --- | --- |
| `mechanical_conflicts` | `resolve`, `renumber`, `taken` | `(head: str, main: str)` |
| `advisory_watch` | `unreachable`, `answers` | `(output: str)` for unreachable; `(service: str)` for answers |
| `push_checkout` | `install`, `refusal` | `(checkout: Path)` for install; `(output: str, checkout: Path)` for refusal |
| `browser_plan` | `browser_plan` | `(changed: list[bytes])`, decoded from the NUL-separated input without losing path bytes |

Shipline imports the module in a separate Python process in the caller's worktree, with the
module's own folder first on `sys.path` so its sibling imports work. `run` names only the file,
without an interpreter or arguments, found as a program is (`@install/` included). Mapped
functions must be synchronous functions defined at the module's top level; configuration loading
parses the file without executing it, refusing a missing function or unsupported form with the
command's name.

A list return prints one entry a line; a mapping prints `<old> <new>` lines; a string prints
itself; `None` and booleans print nothing. `answers` exits 0 for a true answer and non-zero for
`None` or `False`. `install` treats a returned string as a failure reason and exits non-zero;
`None` means success; the reason is also the last line on standard error, after the function's
diagnostics. `browser_plan` requires a bool and prints `needed` for `True`, `none` for
`False`. An exception exits non-zero with its message on standard error. A `renumber` exception
with a `paths` attribute, a list of the files it cannot move, also names them on standard
output, so they are left as a conflict. Python diagnostic
prints go to standard error, leaving the return value as the answer. Invalid selector JSON or
fields fail the call rather than selecting no tests.

A command reads nothing but what is given on its standard input. Programs and modules live in
the main checkout or installation folder whichever worktree runs them, so one that reads the
change finds the checkout from the folder it runs in (`git rev-parse --show-toplevel`), not from
its own path.

# A shadow run

Before a repository that runs the workflow from its own copy of the scripts moves onto
`shipline <command>`, Shipline is run beside it in shadow: `shipline --repo <checkout> tick
--shadow` takes the decisions of a tick on that repository and carries out none (`tick` says
what it reads and what it leaves). It writes them to `.local/shipline/shadow.log` of the
checkout, readable by its owner alone, and leaves every note of the installation as it was. Its
reads of GitHub are real calls, counted in the machine's shared GitHub budget beside the
tick's own, and a reviewer's head pushed from elsewhere is fetched as the real tick fetches it.

Each shadow tick runs on a timer of its own, 75 seconds before each of the installation's ticks
(theirs runs every 3 minutes, up to 25 seconds late), and stops itself after 60 seconds, whatever
it waits for, logging that it was cut short: so it reads the state the real tick will read, has
ended before the real tick starts, and never holds it back. Its service's own timeouts are the
last bound. A real tick still running from its last start when a shadow tick starts may be
writing the marks the shadow reads: the shadow tick then takes nothing and logs that it was
skipped (`SHIPLINE_SHADOW_BESIDE` names the real tick's service).

The owner writes the two units, the service with every `Environment=` line of the
installation's `workflow-tick.service` (its `PATH`, its `TMUX_TMPDIR`, its own `WORKFLOW_*`
settings), so that the shadow tick finds the same sessions and settings:

    # ~/.config/systemd/user/shipline-shadow.service
    [Unit]
    Description=Shipline in shadow beside the workflow tick: decides, carries out nothing

    [Service]
    Type=oneshot
    WorkingDirectory=<checkout>
    <every Environment= line of workflow-tick.service>
    Environment=SHIPLINE_SHADOW_BESIDE=workflow-tick.service
    ExecStart=<path of the Shipline checkout>/bin/shipline --repo <checkout> tick --shadow
    TimeoutStartSec=66
    TimeoutStopSec=2

    # ~/.config/systemd/user/shipline-shadow.timer
    [Timer]
    OnCalendar=*:1/3:45
    AccuracySec=1s

    [Install]
    WantedBy=timers.target

and starts it with `systemctl --user enable --now shipline-shadow.timer`. After the run:

    journalctl --user -u workflow-tick.service -o short-unix --since <start> >tick.journal
    tools/compare-shadow --shadow <checkout>/.local/shipline/shadow.log \
      --wake ~/.local/state/workflow/wake.log --journal tick.journal

lists every decision the two took differently, with both versions, and exits 0 when none
differs. A decision that came due in the seconds between the two ticks' clocks is a timing
difference, counted on its last line and listed with `--timing`.
