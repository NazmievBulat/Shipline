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

The tick's explicit `SHIPLINE_CONFIG` and `XDG_CONFIG_HOME` settings reach every session it
starts, with relative paths made absolute before changing folders. A session's own `shipline`
calls therefore select the same file. `runner/install.sh` uses this same lookup and reports the
selected external config path. It checks the config against `origin/main` only when reading the
main checkout's `shipline.json`; an external config has no file there to compare with. The
installed runner commands remember that external path and still run on `origin/main`'s files.

`examples/shipline.json` names every key for `example-app` with a server suite, a browser suite
and an image check. A repository needs no config or adapter scripts of its own.

The installation folder is the parent folder of a file selected by `SHIPLINE_CONFIG` or from
the home config folder. The optional `install_folder` key overrides it: an absolute path, or a
path relative to the selected config file. A checkout config has no installation folder unless
it names one. Programs and a `parity.known` file can live there. Relative program paths still
use the main checkout first, then the installation folder; an `@install/` prefix uses only the
installation folder, even if the checkout has a file of the same name.

<!-- shipline-config:start -->
| Key | Kind | What it names | Default |
| --- | --- | --- | --- |
| `annotations.browser_suite` | name | The browser suite annotation title read by the merge gate. | `"Browser suite"` |
| `annotations.files_tested` | name | The tested tree annotation title read by the merge gate. | `"Files tested"` |
| `annotations.merge_tested` | name | The tested merge annotation title read by the merge gate. | `"Merge tested"` |
| `annotations.server_tests` | name | The server tests annotation title read by the merge gate. | `"Server tests"` |
| `annotations.web_suite` | name | The web suite annotation title read by the merge gate. | `"Web suite"` |
| `answer_for_owner_after_minutes` | minutes | Minutes before the tick answers a question marked Owner only: no; 0 disables it, at most 1440. | `15` |
| `checks.advisory_annotation` | name | The advisory notice title on main; omitted means advisories make main red. | none |
| `checks.ci_plan` | name | The CI planner check name. | `"CI: what to run"` |
| `checks.ci_result` | name | The check that sums up CI. | `"CI result"` |
| `checks.dependency_audit` | name | The dependency audit job name; omitted means CI: dependencies. | none |
| `checks.e2e_plan` | name | The browser suite planner check name. | `"E2E: what to run"` |
| `checks.images` | name | The prefix of image checks; its planner appends : what to run. | `"Docker images"` |
| `checks.prefix_only` | names | Distinct groups run only in merge prefixes: images, browser_whole and server_whole. | `[]` |
| `cloud_pre_push.suites` | suites | Named cloud pre-push suites with select, run, workers and optional part and probe commands. | `{}` |
| `cloud_wake_after_minutes` | minutes | Minutes without a push or report before a cloud worker is woken; 0 disables it. | `30` |
| `commands.advisory_watch` | command | The command that detects an advisory service outage and checks whether the service answers again. | none |
| `commands.browser` | command | The repository browser test command run by bash from the checkout with the E2E job's arguments. | none |
| `commands.browser_plan` | command | The command that reads NUL-ended changed paths and prints needed or none. | none |
| `commands.browser_tests_for_changes` | command | The command that selects browser spec paths for a changed base. | none |
| `commands.hunt_app` | command | The command that manages the application under a hunt, including stop. | none |
| `commands.hunt_browser` | command | The browser command named in a hunt sentence. | none |
| `commands.hunt_cursor` | command | The command that starts a Cursor hunt with its worktree, report folder and sentence. | none |
| `commands.mechanical_conflicts` | command | The command that resolves conflicts, renumbers files and reports taken numbers. | none |
| `commands.pre_train_checks` | command | The checks run before a pull request is admitted to the merge train. | none |
| `commands.push_checkout` | command | The command that installs changed checkout files and explains a failed push hook. | none |
| `commands.push_checks` | command | The checks run by the repository's pre-push hook. | none |
| `commands.server_tests_for_changes` | command | The command that selects server tests and returns whole, tests, total and reasons. | none |
| `commands.test` | command | The repository test command run by bash from the checkout with the CI job's arguments. | none |
| `conflict_review` | conflict | The effort, model and max_changed_lines for a small handed-off conflict; omitted means the usual reviewer. | none |
| `hot_paths` | paths | GitHub path patterns whose matching tickets run separately and merge back to back within their priority. | `[]` |
| `hunts.areas` | areas | Area names mapped to lists of screen paths; omitted means one hunt runs on every screen. | `{}` |
| `hunts.plans` | name | The checkout path of hunt plans named in a hunt sentence. | none |
| `install_folder` | name | The installation folder, resolved against the selected file; external configs use their parent when omitted. | none |
| `labels.audited` | name | The label that marks an audit. | `"audited"` |
| `labels.priority.blocker` | name | The blocker priority label. | `"priority: blocker"` |
| `labels.priority.high` | name | The high priority label. | `"priority: high"` |
| `labels.priority.low` | name | The low priority label. | `"priority: low"` |
| `labels.priority.medium` | name | The medium priority label. | `"priority: medium"` |
| `labels.workflow` | name | The label that starts a ticket. | `"workflow"` |
| `machines` | machines | Machines sampled by flow-page, each with a name and optional SSH address; omitted samples this machine. | `[]` |
| `merge.scratch_prefix` | name | The beginning of scratch branch names for tested merge prefixes, ended by - or /. | `"workflow/merge-batch-"` |
| `merge_train.queued_limit_minutes` | minutes | Minutes a scratch CI run may remain queued before its wait is bounded. | `20` |
| `parity.known` | name | The expected differences file read by tools/parity; @install/ requires an installation folder. | none |
| `parity.runner_tests` | name | The runner tests folder compared by tools/parity. | `"tests/runner"` |
| `parity.workflow_tests` | name | The workflow tests folder compared by tools/parity. | `"tests/workflow"` |
| `refresh.clear_generated` | names | Ignored, untracked generated paths or globs cleared after a refresh; tracked paths, links and paths outside the checkout are refused. | `[]` |
| `repository` | name | The GitHub repository as owner/name; discovered from runner settings, then origin when omitted. | none |
| `review.high_effort_paths` | names | Regular expressions selecting changed paths for an independent review at high effort. | `[]` |
| `runner.hub_cache` | name | The command running the Docker Hub cache; omitted means direct pulls. | none |
| `runner.identity_folder` | name | The folder holding the runner programs' own GitHub identity. | `"~/.config/shipline-runner"` |
| `runner.image_build` | name | The command that builds runner images; omitted means the pool cannot be installed. | none |
| `runner.label` | name | The base runner label; derived as `<prefix>-home` when omitted. | none |
| `runner.labels` | names | The runner labels eligible for failed-job reruns; derived from the base label when omitted. | `[]` |
| `runner.planner_label` | name | The planner runner label; derived as `<label>-planner` when omitted. | none |
| `runner.prefix` | name | The word naming runner images, services, network and containers; omitted means no runner figures. | none |
| `state_folder` | name | The folder of workflow notes in the main checkout and each worktree. | `".local/workflow"` |
| `workflows.ci` | name | The CI workflow file under .github/workflows/. | `"ci.yml"` |
| `workflows.e2e` | name | The browser workflow file under .github/workflows/. | `"e2e.yml"` |
| `workflows.images` | name | The image workflow file under .github/workflows/. | `"images.yml"` |
| `worktree_setup` | name | The command that prepares a new worktree, run only when present. | `"scripts/setup-worktree"` |
<!-- shipline-config:end -->

To add a configuration key, add one `option(...)` field in its dataclass section in
`workflow_config.py`, sorted by key name. Its kind selects validation; its default, meaning
and optional example generate the tables and example file. A new section uses `section()`.
Run `python3 scripts/workflow/workflow_config.py docs --write` and commit the generated files;
CI checks that they are current. Give keys whose omitted default is `None` a valid example.
The `position` metadata preserves existing positional constructors; keep those positions.
Shell exports use the field's `shell=(position, name)` metadata;
keep existing positions, and give new exports an unused position to preserve the interface.
Defaults in the table apply to omitted keys; `none` means no configured value.

Change `merge.scratch_prefix` only while the merge line is empty: the gate deletes, and cancels
the runs of, only the scratch branches of the prefix it reads, so those of the old one would stay.

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

<!-- shipline-commands:start -->
| Command | Called by, with | Answers | Without it |
| --- | --- | --- | --- |
| `advisory_watch` | the merge gate: `unreachable` with a failed push's output, or a failed audit's annotations (without the advisories notice, `checks.advisory_annotation`), on standard input; `answers <service>` | `unreachable` prints the advisory service the text says was unreachable, or nothing; `answers` exits 0 when the service answers again. Beside the advisories notice, a service it names, or a failure of its own, keeps main red | no outage is read: a failed push is refused as it is, and main's advisories notice stands alone |
| `browser` | E2E's browser job: the job's own arguments. | The command's output and exit code. | The browser job fails and asks for commands.browser. |
| `browser_plan` | the merge gate: the changed paths on standard input, each ended by a NUL | prints `needed` or `none` and exits 0; anything else, a crash or another exit included, counts as needed | every merge that changes a file needs browser tests while the merged tree holds the E2E workflow (`workflows.e2e`), and none does without it |
| `browser_tests_for_changes` | the review panel in a conflict round, and sessions: `<base> [--files-from <file>]` and the session's own words | the browser specs the change reaches, one path a line | nothing is selected, and CI runs the whole browser suite |
| `hunt_app` | `session` when a hunt stops: `stop`, in the hunt's worktree; the hunter itself, by `shipline hunt-app` |  | `session start hunt` refuses |
| `hunt_browser` | the hunter, by `shipline hunt-browser`, which its sentence names |  | the sentence names none |
| `hunt_cursor` | `session start hunt <name> <kind> cursor`: `<worktree> <report folder> <sentence>` | what it prints is kept in the report folder's `cursor.log` | a Cursor hunt is refused |
| `mechanical_conflicts` | the merge gate's refresh: `resolve <head> <main>` with the merge of main in progress; `renumber <head> <main>` after a clean merge; `taken <head> <main>` before a batch | `resolve` stages its resolution, prints each file it resolved and exits 0 only when it resolved them all; `renumber` stages its moves and prints `<old path> <new path>` a line (paths without spaces), or exits non-zero, naming on standard output the files it cannot move (left as a conflict) and why on standard error; `taken` prints the branch's files whose number main took | every conflict is left to the session, nothing is renumbered and main took no number |
| `pre_train_checks` | the merge gate, before a pull request joins the merge train, in its worktree | exits non-zero when a check failed; what it prints is shown | every pull request is admitted |
| `push_checkout` | the merge gate, before each push and after one that failed: `install <checkout>`; `refusal <checkout>` with the push's whole output on standard input | `install` installs what changed since its last install and exits non-zero with the reason when it failed, which is a wait; `refusal` prints the hook's failed step with its last lines | nothing is installed, and a failed push is refused with its last 10 lines |
| `push_checks` | The pre-push hook: the selected changes. | The command's output and exit code. | No additional push checks run. |
| `server_tests_for_changes` | the review panel in a conflict round, and sessions: `<base> [--files-from <file>]` | one JSON object, `{"whole": bool, "tests": [paths], "total": int, "reasons": [str]}` | the whole server suite: `{"whole": true, "tests": [], "total": 0, "reasons": ["no server test selection is configured"]}` |
| `test` | CI's test job: the job's own arguments. | The command's output and exit code. | The test job fails and asks for commands.test. |
<!-- shipline-commands:end -->

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
