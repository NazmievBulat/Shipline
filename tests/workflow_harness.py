"""A throwaway repository and stand-ins for everything the scripts in `scripts/workflow` touch.

`git` is the real one, run against a repository made for the test. `gh`, `tmux`, `worktree-setup`,
`sleep`, `curl`, `systemctl`, `systemd-run` and `claude` are small scripts placed first on the
`PATH`; they write what they were asked to a log and answer from files the test prepares. The
control group and `/proc` folders are folders in the test's own directory.
"""

import atexit
import compileall
import json
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "workflow"
SESSION_LINK = "https://claude.ai/code/session_01TestLinkForWorkflow"

# A fake outside call whose cancellation is observable independently of runner speed. Its trap
# first ignores TERM: GNU timeout sends a second one to the process group, and bash would run
# the trap again for it, writing one call down as two.
HANGING_CALL = (
    "#!/bin/bash\n"
    "name=${0##*/}\n"
    "echo hangs-$name >> '@STATE@/calls.log'\n"
    "trap 'trap \"\" TERM; echo interrupted-$name >> @STATE@/calls.log; exit 124' TERM\n"
    "/bin/sleep 600 & wait\n"
    "echo completed-$name >> '@STATE@/calls.log'\n"
)
# How the sentence a session is started with ends: where its rules are read from.
RULES_ON_MAIN = (
    "by the rules of AGENTS.md as it is on main, not the copy in this worktree; "
    "read them with: git fetch origin main && git show origin/main:AGENTS.md"
)


def scope_words(name: str, unit: str, memory: str = "8G", swap: str = "4G") -> list[str]:
    """The words `tmux` is given before a session's command: its own scope, with its limits."""
    assert re.fullmatch(rf"--unit=workflow-{re.escape(name)}-[0-9]+", unit), unit
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        unit,
        "-p",
        f"MemoryMax={memory}",
        "-p",
        f"MemorySwapMax={swap}",
        "-p",
        "OOMPolicy=continue",
        "-p",
        "Delegate=yes",
        "--",
        "bash",
        "-c",
    ]


def session_arguments(state: Path, name: str) -> list[str]:
    """What `tmux new-session` was given for a session, its scope checked and left out."""
    arguments = (state / "args" / name).read_text().splitlines()
    start = arguments.index("systemd-run")
    words = scope_words(name, arguments[start + 5])
    assert arguments[start : start + len(words)] == words
    return arguments[:start] + arguments[start + len(words) :]


def instruction(sentence: str) -> str:
    """The whole sentence a session is started with: what to do, then where the rules are."""
    return f"{sentence} {RULES_ON_MAIN}"


# Answers only the question the script is meant to ask, so a changed question fails a test.
_FAKE_GH = """#!/bin/bash
echo "gh $*" >> "@STATE@/calls.log"
snapshot_fields=number,isDraft,baseRefName,headRefOid,headRefName,statusCheckRollup,mergeable,mergeStateStatus,state,labels,body
if [ "$*" = "pr list --state open --limit 1000 --json $snapshot_fields" ]; then
  [ ! -e "@STATE@/fail-gh-list" ] || exit 1
  for listed in "@STATE@"/listing/*; do
    [ -f "$listed" ] || continue
    number=${listed##*/}
    read -r state branch head < "@STATE@/pr/$number"
    [ "$state" = OPEN ] || continue
    read -r draft labels < "$listed"
    base=main
    [ ! -f "@STATE@/base-branch-$number" ] || base=$(cat "@STATE@/base-branch-$number")
    mergeable=MERGEABLE
    merge_state=CLEAN
    [ ! -f "@STATE@/mergeability-$number" ] ||
      read -r mergeable merge_state < "@STATE@/mergeability-$number"
    checks="@STATE@/rollup-$number.json"
    [ -f "$checks" ] || checks=/dev/null
    if [ -e "@STATE@/fail-gh-checks" ]; then checks=/dev/null; fi
    body="Backend workflow maintenance."
    [ ! -f "@STATE@/body-$number" ] || body=$(cat "@STATE@/body-$number")
    jq -n --argjson number "$number" --arg state "$state" --arg branch "$branch" \\
      --arg head "$head" --argjson draft "$draft" --arg base "$base" \\
      --arg labels "$labels" --arg mergeable "$mergeable" --arg merge_state "$merge_state" \\
      --slurpfile checks "$checks" --arg body "$body" \\
      '{number: $number, state: $state, headRefName: $branch, headRefOid: $head,
        isDraft: $draft, baseRefName: $base, mergeable: $mergeable, mergeStateStatus: $merge_state,
        statusCheckRollup: ($checks[0] // []), body: $body,
        labels: ($labels | split(" ") | map(select(. != "")) | map({name: gsub("[+]"; " ")}))}'
  done | jq -s .
  exit 0
fi
if [ "$1 $2 $4 $5" = "pr view --json $snapshot_fields" ]; then
  [ ! -e "@STATE@/fail-gh-checks" ] || exit 1
  read -r state branch head < "@STATE@/pr/$3" || exit 1
  body="Backend workflow maintenance."
  [ ! -f "@STATE@/body-$3" ] || body=$(cat "@STATE@/body-$3")
  jq -n --arg state "$state" --arg branch "$branch" --arg head "$head" --arg body "$body" \\
    '{state: $state, headRefName: $branch, headRefOid: $head, body: $body}'
  exit 0
fi
if [[ "$1 $2" == "api --paginate" && $3 == */comments[?]per_page=100 ]]; then
  [ "$4 $5" = '--jq .[] | @json' ] || exit 1
  [ ! -e "@STATE@/fail-gh-comments" ] || exit 1
  number=${3%/comments*}
  number=${number##*/}
  comments="@STATE@/comments-$number.json"
  if [ -f "$comments" ]; then jq -r '.[][] | @json' "$comments"; fi
  exit 0
fi
if [ "$1 $2" = "api graphql" ]; then
  # History is newest-first: only the oldest retained creation record is authoritative.
  for field in 'userContentEdits(last: 1)' includesCreatedEdit lastEditedAt \
      'nodes { diff deletedAt }' 'timelineItems(first: 1, itemTypes: [RENAMED_TITLE_EVENT])' \
      previousTitle; do
    case "$*" in *"$field"*) ;; *) exit 1 ;; esac
  done
  [ ! -e "@STATE@/fail-task-history" ] || exit 1
  if [ -f "@STATE@/task-history.json" ]; then cat "@STATE@/task-history.json"; else
    echo '{"data":{"repository":{"pullRequest":{"includesCreatedEdit":false,
"lastEditedAt":null,"userContentEdits":{"nodes":[]},"timelineItems":{"nodes":[]}}}}}'
  fi
  exit 0
fi
if [ "$1 $2" = "api --paginate" ]; then
  number=${3%/reviews}
  number=${number##*/}
  [ ! -e "@STATE@/fail-gh-reviews" ] || exit 1
  reviews="@STATE@/reviews-$number.json"
  if [ -f "$reviews" ]; then cat "$reviews"; else echo '[]'; fi
  exit 0
fi
check_fields=state,isDraft,headRefOid,statusCheckRollup,mergeable,mergeStateStatus,baseRefName
if [ "$1 $2 $4 $5" = "pr view --json $check_fields" ]; then
  [ ! -e "@STATE@/fail-gh-checks" ] || exit 1
  read -r state _ head < "@STATE@/pr/$3"
  read -r draft _ < "@STATE@/listing/$3"
  base=main
  [ ! -f "@STATE@/base-branch-$3" ] || base=$(cat "@STATE@/base-branch-$3")
  mergeable=MERGEABLE
  merge_state=CLEAN
  [ ! -f "@STATE@/mergeability-$3" ] || read -r mergeable merge_state < "@STATE@/mergeability-$3"
  checks="@STATE@/rollup-$3.json"
  [ -f "$checks" ] || exit 1
  jq -n --arg state "$state" --arg head "$head" --argjson draft "$draft" \\
    --arg base "$base" --arg mergeable "$mergeable" --arg merge_state "$merge_state" \\
    --slurpfile checks "$checks" \\
    '{state: $state, isDraft: $draft, headRefOid: $head, statusCheckRollup: $checks[0],
      baseRefName: $base, mergeable: $mergeable, mergeStateStatus: $merge_state}'
  exit 0
fi
if [ "$1 $2 $4 $5" = "pr view --json title,body,baseRefName,headRefOid" ]; then
  [ ! -f "@STATE@/fail-view-$3" ] || exit 1
  read -r _ _ head < "@STATE@/pr/$3"
  body="Backend workflow maintenance."
  [ ! -f "@STATE@/body-$3" ] || body=$(cat "@STATE@/body-$3")
  title="Test ticket"
  [ ! -f "@STATE@/title-$3" ] || title=$(cat "@STATE@/title-$3")
  jq -n --arg title "$title" --arg body "$body" --arg head "$head" \\
    '{title: $title, body: $body, baseRefName: "main", headRefOid: $head}'
  exit 0
fi
if [ "$1 $2" = "pr diff" ]; then
  if [ -f "@STATE@/diff-$3" ]; then cat "@STATE@/diff-$3"; else
    read -r _ _ head < "@STATE@/pr/$3"
    git diff "main...$head"
  fi
  exit 0
fi
asked="$1 $2 $4 $5 $6"
if [ "$asked" = "pr view --json state,headRefName,headRefOid --jq" ] && [ -f "@STATE@/pr/$3" ]; then
  [ ! -e "@STATE@/fail-gh-view-$3" ] || exit 1
  # What GitHub says now, when it changed after the tick's snapshot.
  if [ -f "@STATE@/pr-now/$3" ]; then cat "@STATE@/pr-now/$3"; else cat "@STATE@/pr/$3"; fi
  exit 0
fi
# Like the real one, it knows the repository only from the folder it is called in.
if [ "$asked $7" = "pr view --json isDraft --jq .isDraft" ] && [ -f "@STATE@/draft/$3" ] \
  && git rev-parse --git-dir >/dev/null 2>&1; then
  cat "@STATE@/draft/$3"
  exit 0
fi
# The open pull requests that carry the label, as the JSON GitHub answers with. The script's
# own filter turns it into lines, as the real `gh` would.
fields=number,isDraft,baseRefName
asked="pr list --label workflow --state open --limit 1000 --json $fields --jq ${12}"
if [ "$*" = "$asked" ] && [ ! -e "@STATE@/fail-gh-list" ]; then
  for listed in "@STATE@"/listing/*; do
    [ -f "$listed" ] || continue
    number=${listed##*/}
    read -r state _ < "@STATE@/pr/$number"
    read -r draft labels < "$listed"
    base=main
    [ ! -f "@STATE@/base-branch-$number" ] || base=$(cat "@STATE@/base-branch-$number")
    case " $labels " in
      *" workflow "*) [ "$state" != OPEN ] || echo "$number $draft $base" ;;
    esac
  done | jq --raw-input 'split(" ") | {number: (.[0] | tonumber), isDraft: (.[1] == "true"),
    baseRefName: .[2]}' |
    jq --slurp --raw-output "${12}"
  exit 0
fi
# All open pull requests with their labels, as the wake asks.
asked="pr list --state open --limit 200 --json number,isDraft,labels,statusCheckRollup --jq ${10}"
if [ "$*" = "$asked" ] && [ ! -e "@STATE@/fail-gh-list" ]; then
  for listed in "@STATE@"/listing/*; do
    [ -f "$listed" ] || continue
    number=${listed##*/}
    read -r state _ < "@STATE@/pr/$number"
    [ "$state" = OPEN ] || continue
    # `checks/<n>` holds the conclusion of its one check; without it, it has none.
    checks=""
    [ ! -f "@STATE@/checks/$number" ] || checks=$(cat "@STATE@/checks/$number")
    echo "$number ${checks:--} $(cat "$listed")"
  done | jq --raw-input 'split(" ") | {number: (.[0] | tonumber), isDraft: (.[2] == "true"),
    statusCheckRollup: (if .[1] == "-" then [] else [{conclusion: .[1]}] end),
    labels: (.[3:] | map(select(. != "")) | map({name: gsub("[+]"; " ")}))}' |
    jq --slurp --raw-output "${10}"
  exit 0
fi
# A label added to a pull request joins its labels in the listing.
if [[ "$1 $2 $3 $5" == "api -X POST -f" && $4 == repos/*/issues/*/labels ]]; then
  [ ! -e "@STATE@/fail-gh-label" ] || exit 1
  number=${4%/labels}
  number=${number##*/}
  read -r draft labels < "@STATE@/listing/$number"
  echo "$draft $labels ${6#labels[]=}" > "@STATE@/listing/$number"
  echo "$number" >> "@STATE@/labelled"
  exit 0
fi
asked="label list --limit 1000 --json name --jq ${8}"
if [ "$*" = "$asked" ] && [ ! -e "@STATE@/fail-gh-labels" ]; then
  touch "@STATE@/labels"
  jq --raw-input '{name: .}' "@STATE@/labels" | jq --slurp --raw-output "${8}"
  exit 0
fi
if [ "$1 $2" = "label create" ] && [ ! -e "@STATE@/fail-gh-label-create" ]; then
  echo "$3" >> "@STATE@/labels"
  exit 0
fi
echo "gh: could not answer $*" >&2
exit 1
"""

# Called as `tmux -L <name> <command> ...`. A server "runs" while the file tmux/<name> exists.
# Like the real one, it finds a session only by `=<name>` and a pane only by `=<name>:`.
_FAKE_TMUX = """#!/bin/bash
name=$2
command=$3
shift 3
echo "tmux $name $command $*" >> "@STATE@/calls.log"
target() {
  while [ $# -gt 0 ]; do
    if [ "$1" = -t ]; then echo "$2"; return; fi
    shift
  done
}
case "$command" in
  has-session) [ "$(target "$@")" = "=$name" ] && [ -e "@STATE@/tmux/$name" ] ;;
  new-session)
    [ ! -e "@STATE@/refuse-launch-$name" ] || exit 1
    # `dies-at-once` names the sessions that end as they start; left empty it means all.
    dies="@STATE@/dies-at-once"
    if [ ! -e "$dies" ] || { [ -s "$dies" ] && ! grep -qx "$name" "$dies"; }; then
      touch "@STATE@/tmux/$name"
      mkdir -p "$TMUX_TMPDIR/tmux-$(id -u)"
      touch "$TMUX_TMPDIR/tmux-$(id -u)/$name"
    fi
    env > "@STATE@/env/$name"
    printf '%s\\n' "$@" > "@STATE@/args/$name"
    if [[ $name == cloud-* ]]; then
      # Execute the real launcher argv after tmux's working-directory option.
      while [ "$1" != -c ]; do shift; done
      shift 2
      "$@" > "@STATE@/screen/$name" 2>&1 || true
      rm -f "@STATE@/tmux/$name"
    fi
    ;;
  capture-pane)
    [ "$(target "$@")" = "=$name:" ] || { echo "can't find pane" >&2; exit 1; }
    # Lines that left the top of the screen are printed only when the history is asked for.
    case " $* " in
      *" -S -200 "*) [ ! -f "@STATE@/scrollback/$name" ] || cat "@STATE@/scrollback/$name" ;;
    esac
    screen="@STATE@/screen/$name"
    [ -f "$screen" ] || screen="@STATE@/screen/default"
    if [ -f "@STATE@/input-$name" ]; then
      python3 - "$screen" "@STATE@/input-$name" <<'BOX'
import re
import sys
from pathlib import Path
screen = Path(sys.argv[1]).read_text()
text = Path(sys.argv[2]).read_text()
lines = screen.splitlines()
rules = [i for i, line in enumerate(lines)
         if re.sub(r"\\x1b\\[[0-?]*[ -/]*[@-~]", "", line).startswith("─")]
if len(rules) >= 2:
    lines[rules[-2] + 1:rules[-1]] = ["> " + text]
else:
    prompts = (">", chr(0x276f), chr(0x203a))
    boxes = [i for i, line in enumerate(lines) if line.startswith(prompts)]
    at = boxes[-1] - len(boxes) + 1 if boxes else len(lines)
    lines = [line for line in lines if not line.startswith(prompts)]
    # The box stays where the prompt was drawn, above the rows of the footer.
    lines[at:at] = ["> " + text]
print("\\n".join(lines))
BOX
    else
      cat "$screen"
    fi
    ;;
  list-panes)
    [ "$(target "$@")" = "=$name" ] && [ "$*" = "-t =$name -F #{pane_pid}" ] || exit 1
    cat "@STATE@/pid/$name"
    ;;
  # Like the real one it fails when no server runs; `survives-kill` names the servers that
  # refuse to end.
  kill-server)
    [ -e "@STATE@/tmux/$name" ] || { echo "no server running on $name" >&2; exit 1; }
    ! grep -qx "$name" "@STATE@/survives-kill" 2>/dev/null || { echo "kill failed" >&2; exit 1; }
    rm -f "@STATE@/tmux/$name"
    ;;
  # What is typed into a session. The call is in the log; `send-keys-fails` makes it fail.
  send-keys)
    [ "$(target "$@")" = "=$name:" ] || { echo "can't find pane" >&2; exit 1; }
    [ ! -e "@STATE@/send-keys-fails" ] || exit 1
    if [ "$3" = -l ]; then
      text=$4
      [ "$4" != -- ] || text=$5
      printf '%s' "$text" > "@STATE@/input-$name"
      # What the session draws once text is typed, when a test gives it.
      typed="@STATE@/screen-after-typing-$name"
      [ ! -f "$typed" ] || mv "$typed" "@STATE@/screen/$name"
      if [ -e "@STATE@/hide-input-$name" ]; then
        mv "@STATE@/input-$name" "@STATE@/pending-input-$name"
      fi
    elif [ "$3" = Enter ]; then
      swallowed="@STATE@/swallow-enter-$name"
      remaining=0
      [ ! -f "$swallowed" ] || remaining=$(cat "$swallowed")
      if [ "$remaining" -gt 0 ]; then
        echo $((remaining - 1)) > "$swallowed"
      else
        : > "@STATE@/input-$name"
        # What the session draws once an Enter is taken, when a test gives it.
        after="@STATE@/screen-after-enter-$name"
        [ ! -f "$after" ] || mv "$after" "@STATE@/screen/$name"
      fi
    fi
    ;;
  *) exit 1 ;;
esac
"""

_FAKE_INSTALLER = """#!/bin/bash
echo "@TOOL@ $* in $PWD" >> "@STATE@/calls.log"
[ ! -e "@STATE@/fail-@TOOL@" ]
"""

_FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "@STATE@/calls.log"
"""

# Runs what follows `--` in place, as the real one does once it has made the scope.
_FAKE_SYSTEMD_RUN = """#!/bin/bash
echo "systemd-run $*" >> "@STATE@/calls.log"
[ ! -e "@STATE@/refuse-scope-$WORKFLOW_SESSION" ] || exit 1
while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done
shift
exec "$@"
"""

# `claude agents --json` answers with `agents.json`, or with no session. `claude -p` keeps its
# prompt in `sent/<n>` and answers as a delivered `SendMessage` does, unless `send-fails` exists.
_FAKE_CLAUDE = """#!/bin/bash
echo "claude $1 $2" >> "@STATE@/calls.log"
if [[ $1 == --effort && $3 == --cloud ]]; then
  printf '%s\\0' "$@" > "@STATE@/cloud-args"
  [ ! -f "@STATE@/fail-cloud" ] || { echo 'Cloud launch failed'; exit 1; }
  echo 'Created cloud session: Test worker'
  [ -f "@STATE@/cloud-no-address" ] || echo 'View: https://claude.ai/code/session_01Cloud?from=cli&m=0'
  exit 0
fi
if [ "$*" = "agents --json" ]; then
  [ ! -f "@STATE@/agents.json" ] || cat "@STATE@/agents.json"
  [ -f "@STATE@/agents.json" ] || echo "[]"
  exit 0
fi
[ "$1" = -p ] || exit 1
# The tick's question of which files a ticket touches is not a message: it names one folder.
if [[ " $* " == *" --tools  "* ]]; then
  echo "docs/stand-in"
  exit 0
fi
mkdir -p "@STATE@/sent"
printf '%s\\n' "$@" > "@STATE@/sent/$(ls "@STATE@/sent" | wc -l)"
# What the real one prints when its standard input is left open, mixed in when asked to.
[ -t 0 ] || [ ! -p /dev/stdin ] || echo "Warning: no stdin data received in 3s" >&2
[ ! -e "@STATE@/stderr-warning" ] || echo "Warning: a line that is not JSON" >&2
[ ! -e "@STATE@/send-fails" ] || { echo '{"type":"result","is_error":true}'; exit 1; }
sent='{"type":"tool_use","id":"t1","name":"SendMessage"}'
echo "{\\"type\\":\\"assistant\\",\\"message\\":{\\"content\\":[$sent]}}"
echo '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1"}]}}'
echo '{"type":"result","is_error":false,"result":"sent"}'
"""

# A test may leave a shell snippet in `on-sleep`; it runs each time the script sleeps.
_FAKE_SLEEP = """#!/bin/bash
echo "sleep $*" >> "@STATE@/calls.log"
if [ -f "@STATE@/on-sleep" ]; then . "@STATE@/on-sleep"; fi
"""

# Keeps one folder per call: the arguments, what came on standard input, and a copy of every file
# an argument names as `<field>@<path>`. Pushover "fails" while the file pushover-fails exists.
_FAKE_CURL = """#!/bin/bash
echo "curl" >> "@STATE@/calls.log"
call="@STATE@/push/$(ls "@STATE@/push" | wc -l)"
mkdir "$call"
printf '%s\\n' "$@" > "$call/args"
cat > "$call/input"
for argument in "$@"; do
  case "$argument" in
    *@/*) cp "${argument#*@}" "$call/${argument%%@*}" ;;
  esac
done
[ ! -e "@STATE@/pushover-fails" ]
"""


@dataclass
class Push:
    """One call of the stand-in `curl`."""

    arguments: list[str]
    input: str
    title: str
    message: str
    url: str | None


@dataclass
class Workflow:
    """One test's repository, stand-ins and environment."""

    root: Path
    repo: Path
    state: Path
    bin: Path
    env: dict[str, str]

    @property
    def script(self) -> Path:
        return self.repo / "scripts" / "workflow" / "session"

    @property
    def cgroup_root(self) -> Path:
        return self.root / "cgroup"

    @property
    def pressure_file(self) -> Path:
        return self.root / "proc" / "pressure" / "memory"

    @property
    def swaps_file(self) -> Path:
        return self.root / "proc" / "swaps"

    @property
    def loadavg_file(self) -> Path:
        return self.root / "proc" / "loadavg"

    def run(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.run_script("session", *args, env=env)

    def trace_timeouts(self) -> None:
        """Keep real cancellation, recording the allowed work independently of scheduling."""
        self.fake(
            "timeout",
            '#!/bin/bash\necho "timeout $1" >> "@STATE@/calls.log"\nexec /usr/bin/timeout "$@"\n',
        )

    def timeout_budget(self) -> float:
        return sum(float(call.split()[1]) for call in self.calls("timeout "))

    def run_script(
        self,
        script: str,
        *args: str,
        env: dict[str, str] | None = None,
        stdin: str = "",
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.script.parent / script), *args],
            cwd=cwd or self.repo,
            env={**self.env, **(env or {})},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def notify(
        self,
        event: dict[str, object] | str,
        session: str | None = "ticket-7",
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Call `notify` as a hook of the session would: the event as JSON on standard input."""
        named = {} if session is None else {"WORKFLOW_SESSION": session}
        stdin = event if isinstance(event, str) else json.dumps(event)
        return self.run_script("notify", env={**named, **(env or {})}, stdin=stdin, cwd=cwd)

    def set_draft(self, number: int, draft: bool) -> None:
        (self.state / "draft" / str(number)).write_text("true\n" if draft else "false\n")

    def pushes(self) -> list[Push]:
        calls = sorted((self.state / "push").iterdir(), key=lambda call: int(call.name))
        return [
            Push(
                arguments=(call / "args").read_text().splitlines(),
                input=(call / "input").read_text(),
                title=(call / "title").read_text(),
                message=(call / "message").read_text(),
                url=(call / "url").read_text() if (call / "url").exists() else None,
            )
            for call in calls
        ]

    def notes(self, number: int) -> Path:
        """Where a session's link and idle mark are kept."""
        return self.worktree(number) / ".local" / "workflow"

    @property
    def missed_pushes(self) -> Path:
        return self.root / "home" / ".local" / "state" / "workflow" / "missed-pushes.log"

    def break_session(self, command: str, only_call: int | None = None) -> None:
        """Makes `session <command>` fail, and leaves every other command as it is.

        With `only_call` it fails that one time, counted from 1, and works the other times.
        """
        real = self.script.with_name("session.real")
        if not real.exists():
            self.script.rename(real)
        count = self.state / f"count-{command}"
        self.script.write_text(
            "#!/bin/bash\n"
            f'if [ "$1" = {command} ]; then\n'
            f'  echo x >> "{count}"\n'
            f'  calls=$(wc -l < "{count}")\n'
            f'  if [ -z "{only_call or ""}" ] || [ "$calls" -eq "{only_call or 0}" ]; then\n'
            f'    echo "session {command} failed" >&2; exit 1\n'
            "  fi\n"
            "fi\n"
            f'exec bash "{real}" "$@"\n'
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def worktree(self, number: int) -> Path:
        return self.repo / ".claude" / "worktrees" / f"ticket-{number}"

    def branch(self, number: int) -> str:
        return f"ticket/change-{number}"

    def hunt_worktree(self, name: str) -> Path:
        """The worktree of the hunt `hunt-<name>`: a copy of `main`, on no branch."""
        return self.repo / ".claude" / "worktrees" / f"hunt-{name}"

    def hunt_report(self, name: str) -> Path:
        """Where the hunt `hunt-<name>` leaves its report: in the main checkout, kept."""
        return self.repo / ".local" / "hunts" / name

    def add_hunt(self, name: str) -> Path:
        """A hunt whose worktree and report folder already exist, as after `start hunt`."""
        path = self.hunt_worktree(name)
        self.git("worktree", "add", "--quiet", "--detach", str(path), "origin/main")
        (path / ".local" / "workflow").mkdir(parents=True)
        self.hunt_report(name).mkdir(parents=True)
        return path

    def add_pull_request(
        self,
        number: int,
        state: str = "OPEN",
        *,
        draft: bool = True,
        labels: tuple[str, ...] = ("workflow",),
    ) -> str:
        """A branch that exists only on the remote, and what `gh` says of it. Returns its head."""
        self.list_pull_request(number, draft=draft, labels=labels)
        branch = self.branch(number)
        self.git("branch", branch, "main")
        self.git("push", "--quiet", "origin", branch)
        head = self.git("rev-parse", branch)
        self.git("branch", "-D", branch)
        self.set_pull_request(number, state, head)
        return head

    def list_pull_request(
        self, number: int, *, draft: bool, labels: tuple[str, ...] = ("workflow",)
    ) -> None:
        """What the list of pull requests says of this one: draft or ready, and its labels.

        A `+` in a label stands for a space: `priority:+high` is the label `priority: high`.
        """
        self.set_draft(number, draft)
        word = "true" if draft else "false"
        (self.state / "listing" / str(number)).write_text(f"{word} {' '.join(labels)}\n")

    def push_from_elsewhere(self, number: int) -> str:
        """Someone else pushes a commit to the pull request. Returns the new head."""
        clone = self.root / f"clone-{number}"
        self.git(
            "clone",
            "--quiet",
            "--branch",
            self.branch(number),
            self.git("remote", "get-url", "origin"),
            str(clone),
        )
        self.git("commit", "--quiet", "--allow-empty", "-m", "feat: pushed elsewhere", cwd=clone)
        self.git("push", "--quiet", "origin", self.branch(number), cwd=clone)
        head = self.git("rev-parse", "HEAD", cwd=clone)
        self.set_pull_request(number, "OPEN", head)
        return head

    def set_pull_request(self, number: int, state: str, head: str) -> None:
        (self.state / "pr" / str(number)).write_text(f"{state} {self.branch(number)} {head}\n")

    def add_worktree(self, number: int, state: str = "OPEN", *, draft: bool = True) -> Path:
        """A pull request whose worktree already exists, as after an earlier `start`."""
        self.add_pull_request(number, state, draft=draft)
        path = self.worktree(number)
        self.git("fetch", "--quiet", "origin", self.branch(number))
        self.git("worktree", "add", "--quiet", "-b", self.branch(number), str(path), "FETCH_HEAD")
        return path

    def running(self, name: str, held: int = 0, swapped: int = 0, scope: str | None = None) -> Path:
        """Mark a session as running in its own control group. Returns the group's folder."""
        pid = str(4000 + len(list((self.state / "tmux").iterdir())))
        (self.state / "tmux" / name).touch()
        (self.state / "pid" / name).write_text(pid + "\n")
        self.socket(name)
        scope = scope or f"user.slice/app.slice/tmux-spawn-{name}.scope"
        process = self.root / "proc" / pid
        process.mkdir(parents=True)
        (process / "cgroup").write_text(f"0::/{scope}\n")
        group = self.cgroup_root / scope
        group.mkdir(parents=True, exist_ok=True)
        (group / "memory.current").write_text(f"{held}\n")
        (group / "memory.swap.current").write_text(f"{swapped}\n")
        (group / "memory.reclaim").write_text("")
        (group / "memory.low").write_text("0\n")
        return group

    def socket(self, name: str) -> None:
        """The socket file a `tmux` server leaves behind, whether or not it still runs."""
        folder = self.root / "tmp" / f"tmux-{os.getuid()}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).touch()

    def survives_kill(self, name: str) -> None:
        """Makes `tmux kill-server` of that session fail and leave it running."""
        with (self.state / "survives-kill").open("a") as file:
            file.write(f"{name}\n")

    def is_running(self, name: str) -> bool:
        return (self.state / "tmux" / name).exists()

    def calls(self, tool: str = "") -> list[str]:
        log = self.state / "calls.log"
        if not log.exists():
            return []
        return [line for line in log.read_text().splitlines() if line.startswith(tool)]

    def screen(self, text: str, name: str = "default") -> None:
        (self.state / "screen" / name).write_text(text)

    def scrollback(self, text: str, name: str) -> None:
        """What has scrolled off the top of a session's screen."""
        (self.state / "scrollback" / name).write_text(text)

    def orchestrator(self, status: str = "idle", name: str = "Check-in") -> None:
        """The interactive session that runs in the main checkout, as `claude agents` lists it."""
        others = [
            {
                "name": "ticket-7-ab",
                "kind": "interactive",
                "status": "idle",
                "cwd": str(self.worktree(7)),
            },
            {"name": "old", "kind": "background", "status": None, "cwd": str(self.repo)},
        ]
        mine = {"name": name, "kind": "interactive", "status": status, "cwd": str(self.repo)}
        (self.state / "agents.json").write_text(json.dumps([*others, mine]))
        registration = self.repo / ".local" / "workflow" / "orchestrator"
        registration.parent.mkdir(parents=True, exist_ok=True)
        registration.write_text(name + "\n")

    def sent(self) -> list[str]:
        """What `claude -p` was asked to deliver, one prompt per call, in order."""
        folder = self.state / "sent"
        if not folder.exists():
            return []
        calls = sorted(folder.iterdir(), key=lambda call: int(call.name))
        return [call.read_text() for call in calls]

    def usage(
        self,
        five: int,
        five_resets: str,
        seven: int = 30,
        seven_resets: str = "2026-10-16T07:00:00.318066+00:00",
    ) -> None:
        """The figures the status line keeps, fresh."""
        cache = self.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "five_hour": {"utilization": float(five), "resets_at": five_resets},
                    "seven_day": {"utilization": float(seven), "resets_at": seven_resets},
                }
            )
        )

    @property
    def wake_log(self) -> Path:
        return self.root / "home" / ".local" / "state" / "workflow" / "wake.log"

    def fake(self, tool: str, text: str) -> None:
        path = self.bin / tool
        path.write_text(text.replace("@STATE@", str(self.state)).replace("@TOOL@", tool))
        path.chmod(0o755)


def make_workflow(tmp_path: Path, bin_folder: Path) -> Workflow:
    """`bin_folder` holds the stand-ins, so it must be on a drive that allows running programs.

    CI gives pytest a folder in `/dev/shm`, which does not. For the same reason the script, which
    is placed in that folder, is run through `bash`.
    """
    state = tmp_path / "state"
    for folder in (
        "pr",
        "draft",
        "push",
        "listing",
        "tmux",
        "env",
        "args",
        "screen",
        "scrollback",
        "pid",
    ):
        (state / folder).mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    (tmp_path / "scratch").mkdir()
    (tmp_path / "proc" / "pressure").mkdir(parents=True)
    (tmp_path / "cgroup").mkdir()

    env = {
        "PATH": f"{bin_folder}:{os.environ['PATH']}",
        "XDG_CACHE_HOME": str(bin_folder / "cache"),
        "HOME": str(tmp_path / "home"),
        "USER": "tester",
        "SHELL": "/bin/bash",
        "LANG": "C.UTF-8",
        "TERM": "xterm",
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "TMUX_TMPDIR": str(tmp_path / "tmp"),
        "TMPDIR": str(tmp_path / "scratch"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
        "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
        "WORKFLOW_PROC_ROOT": str(tmp_path / "proc"),
        "WORKFLOW_PRESSURE_FILE": str(tmp_path / "proc" / "pressure" / "memory"),
        "WORKFLOW_SWAPS_FILE": str(tmp_path / "proc" / "swaps"),
        "WORKFLOW_LOADAVG_FILE": str(tmp_path / "proc" / "loadavg"),
        "WORKFLOW_CLAUDE_TMP_ROOT": str(tmp_path / "claude-tmp"),
    }
    made = Workflow(root=tmp_path, repo=tmp_path / "repo", state=state, bin=bin_folder, env=env)
    made.fake("gh", _FAKE_GH)
    made.fake("tmux", _FAKE_TMUX)
    made.fake("worktree-setup", _FAKE_INSTALLER)
    made.fake("sleep", _FAKE_SLEEP)
    made.fake("curl", _FAKE_CURL)
    made.fake("systemctl", _FAKE_SYSTEMCTL)
    made.fake("systemd-run", _FAKE_SYSTEMD_RUN)
    made.fake("claude", _FAKE_CLAUDE)
    made.fake("codex", _FAKE_INSTALLER)
    made.fake(
        "df",
        "#!/bin/bash\necho 'Filesystem Inodes IUsed IFree IUse% Mounted on'\n"
        "echo 'tmpfs 1000 100 900 10% /tmp'\n",
    )
    # Four processors, so a start waits above a 5-minute load of 6.
    made.fake("nproc", "#!/bin/bash\necho 4\n")
    made.loadavg_file.write_text("0.50 0.40 0.30 1/100 1234\n")
    made.screen(f"Welcome\n  Remote Control: {SESSION_LINK}\n> \n")
    made.pressure_file.write_text(
        "some avg10=0.00 avg60=0.05 avg300=0.04 total=22247292\n"
        "full avg10=0.00 avg60=0.05 avg300=0.04 total=21634252\n"
    )
    made.swaps_file.write_text(
        "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
        "/dev/zram0                              partition\t15705084\t435356\t\t100\n"
        "/swapfile                               file\t\t33554428\t0\t\t10\n"
    )

    origin = tmp_path / "origin.git"
    template = _template()
    shutil.copytree(template / "origin.git", origin, symlinks=True)
    shutil.copytree(template / "repo", made.repo, symlinks=True)
    made.git("remote", "set-url", "origin", str(origin))
    return made


@cache
def _template() -> Path:
    """The repository every test starts from and its origin, made once per test process.

    Each test gets a copy: building it took 0.09 s a test, and the scripts come compiled. The
    gate and the helpers run as `python3 -B` and would compile their sources at every start,
    20 to 30 ms each, many times a test. A compiled file is used only while its source has the
    same hash, not the same time and size, so a test that changes a source runs what it wrote.
    Git ignores them through `info/exclude`, which is not part of any commit.
    """
    root = Path(tempfile.mkdtemp(prefix="workflow-template-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    origin, repo = root / "origin.git", root / "repo"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True)

    subprocess.run(
        ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(repo)], env=env, check=True
    )
    scripts = repo / "scripts" / "workflow"
    # Not the bytecode cache: another test worker may be writing a file there while this copies.
    shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    # The repository's own setup command, which `session start` runs in a new worktree.
    setup = scripts.parent / "setup-worktree"
    setup.write_text('#!/bin/bash\nexec worktree-setup "$@"\n')
    setup.chmod(0o755)
    # The merge gate asks E2E's planner whether a merge needs browser tests.
    shutil.copy2(SCRIPTS.parent / "e2e-plan.py", scripts.parent / "e2e-plan.py")
    # These tests drive merge/tick state machines through hundreds of fake reads and hundreds
    # of fake merges, reruns and dispatches. Mock the pacing of both: the mutations' 1 to 1.5 s
    # apart were 486 s of these tests' 4,000 in one measurement. Dedicated helper subprocess tests
    # (test_github_budget.py) exercise its real spacing, concurrency and cooldowns. Keep
    # admission here.
    budget_helper = scripts / "github_budget.py"
    helper = budget_helper.read_text()
    for wait in ("MIN_SPACING = 0.1", "MAX_SPACING = 0.3", "random.uniform(1, 1.5)"):
        # A change of the helper's wording must not bring a wait back unseen.
        assert wait in helper, f"github_budget.py no longer says {wait}"
        helper = helper.replace(wait, wait.split("= ")[0] + "= 0.0" if "=" in wait else "0.0")
    budget_helper.write_text(helper)
    runner_settings = repo / ".github" / "runner" / "settings.sh"
    runner_settings.parent.mkdir(parents=True)
    shutil.copy2(SCRIPTS.parents[1] / ".github" / "runner" / "settings.sh", runner_settings)
    for name in ("install-github-budget.sh", "github-gh.sh", "github_budget.py"):
        shutil.copy2(SCRIPTS.parents[1] / ".github/runner" / name, runner_settings.with_name(name))
    ci_workflow = repo / ".github/workflows/ci.yml"
    ci_workflow.parent.mkdir(parents=True)
    shutil.copy2(SCRIPTS.parents[1] / ".github/workflows/ci.yml", ci_workflow)
    (repo / ".gitignore").write_text(".local/\n.claude/\n")
    git("add", "-A")
    git("commit", "--quiet", "-m", "chore: start")
    git("remote", "add", "origin", str(origin))
    git("push", "--quiet", "-u", "origin", "main")
    compileall.compile_dir(
        scripts, quiet=1, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH
    )
    (repo / ".git" / "info" / "exclude").write_text("__pycache__/\n")
    return root
