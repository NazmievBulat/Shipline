"""What a session runs, and the wait loop that finds itself."""

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from workflow_harness import SCRIPTS

_HELPER = SCRIPTS / "running_command.py"


def _tool_shell(command: str) -> list[str]:
    """The arguments Claude Code gives the shell of one Bash tool call (Claude Code 2.1.303)."""
    return [
        "/bin/bash",
        "-c",
        "source /home/tester/.claude/shell-snapshots/snapshot-bash-1-abc.sh 2>/dev/null || true"
        " && export CLAUDE_PLUGIN_DATA='/home/tester/.claude/plugins/data/codex-openai-codex'\n"
        f": && eval {shlex.quote(command)} < /dev/null && pwd -P >| /tmp/claude-a589-cwd",
    ]


def _read(proc: Path, group: Path) -> dict[str, str | None]:
    done = subprocess.run(
        ["python3", str(_HELPER), str(proc), str(group)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(done.stdout)


class _Processes:
    """A `/proc` and a control group folder holding the processes a test gives."""

    def __init__(self, root: Path) -> None:
        self.proc = root / "proc"
        self.group = root / "cgroup" / "workflow-ticket-7-1.scope"
        self.group.mkdir(parents=True)
        self.pids: list[int] = []

    def add(self, arguments: list[str], started: int | None = None) -> int:
        pid = 5000 + len(self.pids)
        folder = self.proc / str(pid)
        folder.mkdir(parents=True)
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in arguments) + b"\0")
        ticks = started if started is not None else 100 + len(self.pids)
        fields = ["S", *["0"] * 18, str(ticks), "0"]
        (folder / "stat").write_text(f"{pid} (bash) {' '.join(fields)}\n")
        self.pids.append(pid)
        (self.group / "cgroup.procs").write_text("".join(f"{p}\n" for p in self.pids))
        return pid

    def read(self) -> dict[str, str | None]:
        return _read(self.proc, self.group)


@pytest.fixture
def processes(tmp_path: Path) -> _Processes:
    return _Processes(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        # A wait loop that finds itself.
        'while pgrep -f "codex exec" >/dev/null; do sleep 0.2; done',
        "while ps aux | grep -q 'codex exec'; do sleep 0.2; done",
    ],
)
def test_a_real_wait_loop_finds_its_own_shell_and_is_named(tmp_path: Path, command: str) -> None:
    # Nothing named `codex exec` runs, yet the loop never ends. A session's shell has no terminal
    # size: with the 80 columns a test runner without a terminal may pass on, `ps` would cut the
    # shell's own line before `codex exec`.
    unsized = {key: value for key, value in os.environ.items() if key not in ("COLUMNS", "LINES")}
    shell = subprocess.Popen(_tool_shell(command), env=unsized)
    try:
        time.sleep(1.5)
        assert shell.poll() is None, "the loop ended: pgrep did not find its own shell"
        group = tmp_path / "group"
        group.mkdir()
        (group / "cgroup.procs").write_text(f"{shell.pid}\n")

        found = _read(Path("/proc"), group)

        assert found["running"] == command
        assert found["finds_itself"] == command
    finally:
        shell.kill()
        shell.wait()


@pytest.mark.parametrize(
    "command",
    [
        'while pgrep -f "codex exec" >/dev/null; do sleep 30; done',
        "while pgrep -af codex; do sleep 30; done",
        "while pgrep -fl -u tester 'codex exec'; do sleep 30; done",
        "while [ -n \"$(pgrep -f 'codex exec')\" ]; do sleep 30; done",
        "while ps aux | grep -q 'codex exec'; do sleep 30; done",
        "while ps -ef | grep -iq 'CODEX EXEC'; do sleep 30; done",
        "echo waiting\nwhile pgrep --full codex >/dev/null\ndo sleep 30\ndone",
        "while ps -eo pid,args | grep codex | grep -q exec; do sleep 30; done",
        # Selections add up: `-e` lists every process whatever else is chosen.
        "while ps -e -p 99999999 -o args= | grep -q 'codex exec'; do sleep 30; done",
        "while ps ax -p 99999999 -o args | grep -q 'codex exec'; do sleep 30; done",
        # A width is not a pid. Without a choice ps lists its own terminal, the shell's too.
        "while ps -o args= --width 1000 | grep -q 'codex exec'; do sleep 30; done",
        # A column name is not a choice of processes, though `p` alone would be one.
        "while ps -e -o pid -o args | grep -q 'codex exec'; do sleep 30; done",
        # GNU grep's basic `\\|` is an alternative, which the shell's own line holds.
        "while ps aux | grep -q 'codex\\|exec'; do sleep 30; done",
        # The last command of the condition decides, and `||` keeps a found process true.
        "while sleep 5; pgrep -f 'codex exec' >/dev/null; do :; done",
        "while kill -0 4242 || pgrep -f 'codex exec' >/dev/null; do sleep 30; done",
        # A second loop of the same command.
        "while read -r line; do echo $line; done <list; while pgrep -f codex; do sleep 30; done",
    ],
)
def test_a_wait_loop_whose_search_matches_its_own_shell_is_named(
    processes: _Processes, command: str
) -> None:
    processes.add(_tool_shell(command))

    found = processes.read()

    assert found["finds_itself"] == found["running"]
    assert found["running"] == command.splitlines()[0]


@pytest.mark.parametrize(
    "command",
    [
        # The usual way out: the bracket keeps the pattern from matching its own text.
        'while pgrep -f "[c]odex exec" >/dev/null; do sleep 30; done',
        # Without -f pgrep reads only the program name, which is bash.
        "while pgrep codex >/dev/null; do sleep 30; done",
        # Narrowed to children, exact, or leaving out ancestors: these may end.
        "while pgrep -P 4242 -f codex; do sleep 30; done",
        "while pgrep -fx 'codex exec'; do sleep 30; done",
        "while pgrep -A -f codex; do sleep 30; done",
        # The shell's own line holds "grep" too, so `grep -v grep` removes it.
        "while ps aux | grep codex | grep -v grep; do sleep 30; done",
        # A ps that lists chosen processes only never lists the shell.
        'while ps -p "$pid" -o args= | grep -q "codex exec"; do sleep 30; done',
        'while ps -o args= -p "$pid" | grep -q "codex exec"; do sleep 30; done',
        "while ps -C codex -o args | grep -q 'codex exec'; do sleep 30; done",
        "while ps --ppid 4242 -o args | grep -q codex; do sleep 30; done",
        # Every grep of the pipe must match the shell: the bracket keeps the second one off it.
        "while ps aux | grep codex | grep -q '[e]xec'; do sleep 30; done",
        # Not a loop: a search that finds itself ends at once.
        "pgrep -f codex && echo running",
        "until pgrep -f codex; do sleep 30; done",
        "while kill -0 4242; do sleep 30; done",
        # Only the condition decides: a search in the body or after `done` does not.
        'while kill -0 "$pid"; do pgrep -f "codex exec" >/dev/null; sleep 30; done',
        'while sleep 5; do :; done; pgrep -f "codex exec"',
        "while pgrep -f 'codex exec' >/dev/null; kill -0 4242; do sleep 30; done",
        "while pgrep -f 'codex exec' >/dev/null && kill -0 4242; do sleep 30; done",
        # Found at once, so these end at once.
        "while ! pgrep -f 'codex exec' >/dev/null; do sleep 30; done",
        "while [ -z \"$(pgrep -f 'codex exec')\" ]; do sleep 30; done",
        # A pattern that is not a valid expression is left out, not guessed at.
        "while pgrep -f 'codex(' ; do sleep 30; done",
        # What pgrep found is filtered further: a pid kept by the filter can end.
        "while pgrep -f 'codex exec' | grep -q '^4242$'; do sleep 30; done",
        "while pgrep -f 'codex exec' | grep -qvx \"$$\"; do sleep 30; done",
    ],
)
def test_a_search_that_can_end_is_not_named(processes: _Processes, command: str) -> None:
    processes.add(_tool_shell(command))

    found = processes.read()

    assert found["running"] == command.splitlines()[0]
    assert found["finds_itself"] is None


def test_the_newest_tool_call_is_the_running_one_and_other_processes_are_not(
    processes: _Processes,
) -> None:
    processes.add(["claude", "--effort", "high", "Implement pull request 7"], started=10)
    processes.add(_tool_shell("npm run test:web"), started=300)
    processes.add(_tool_shell("git status"), started=200)
    processes.add(["npm", "exec", "@playwright/mcp@latest"], started=400)

    found = processes.read()

    assert found == {"running": "npm run test:web", "finds_itself": None}


def test_a_session_running_no_command_says_so(processes: _Processes) -> None:
    processes.add(["claude", "--effort", "high", "Implement pull request 7"])
    # A process that ended between the listing and the read is skipped.
    (processes.group / "cgroup.procs").write_text("5000\n9999\n")

    assert processes.read() == {"running": "", "finds_itself": None}


def test_a_long_first_line_is_shortened(processes: _Processes) -> None:
    processes.add(_tool_shell("echo " + "x" * 300))

    running = processes.read()["running"]

    assert running is not None
    assert len(running) == 160
    assert running.endswith("…")


def test_a_process_in_a_subgroup_of_the_session_is_read(processes: _Processes) -> None:
    processes.add(["claude", "--effort", "high", "Implement pull request 7"])
    pid = processes.add(_tool_shell("while pgrep -f codex; do sleep 30; done"))
    (processes.group / "cgroup.procs").write_text("5000\n")
    child = processes.group / "tests"
    child.mkdir()
    (child / "cgroup.procs").write_text(f"{pid}\n")

    assert processes.read()["finds_itself"] == "while pgrep -f codex; do sleep 30; done"
