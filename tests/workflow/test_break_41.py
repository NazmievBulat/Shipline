"""Breaking tests for pull request 41: a session keeps its memory limit when tmux moves its pane.

The real `tmux` and the real kernel's control groups (version 2): a process that moves into a
group leaves its children where they are, as it does on a machine with systemd. What systemd
would do is played by two stand-ins:

- `systemd-run` makes the scope's group, moves its own process into it, as the real one does
  before it runs the command, and writes the limits it was given as the files `memory.max` and
  `memory.swap.max` of that group under the test's control group folder (this kernel's memory
  controller is not on version 2, so those files are the test's own);
- a tmux built with systemd support is played by a shim the pane's command starts with: it moves
  the pane's own process, and only that one, into a `tmux-spawn-*.scope` of its own, after a
  delay of the test's choosing, while the pane runs, as tmux's server does. That scope has no
  limit (`max`).

Skipped where the test cannot make control groups of its own (not root, no version 2).
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from workflow_harness import SESSION_LINK, Workflow

_TMUX = shutil.which("tmux")
_UNIFIED = Path("/sys/fs/cgroup/unified")
_MB = 1024 * 1024
_LIMITS = {"WORKFLOW_SESSION_MEMORY_MAX": "96M", "WORKFLOW_SESSION_SWAP_MAX": "16M"}


def _own_groups() -> Path | None:
    """A folder of version 2 control groups this test may make and move processes into."""
    for root in (_UNIFIED, Path("/sys/fs/cgroup")):
        if not (root / "cgroup.procs").exists() or not os.access(root, os.W_OK):
            continue
        # Only where the kernel names this process's group from that root.
        own = Path("/proc/self/cgroup").read_text()
        if re.search(r"^0::/$", own, re.MULTILINE):
            return root
    return None


_ROOT = _own_groups()
pytestmark = pytest.mark.skipif(  # noqa: TID251
    _TMUX is None or _ROOT is None,
    reason="needs tmux and control groups of the test's own",
)


@pytest.fixture
def scoped(workflow: Workflow) -> Iterator[Workflow]:
    assert _ROOT is not None and _TMUX is not None
    prefix = f"shipline-break-{uuid.uuid4().hex[:10]}"
    real = _ROOT / prefix
    real.mkdir()
    # The kernel names this process's group from the root of `_ROOT`, as `_own_groups` checked.
    named = f"/{prefix}"
    real_of = str(real)
    sockets = Path(tempfile.mkdtemp(prefix="wf-"))
    fake = workflow.cgroup_root
    (workflow.bin / "tmux").unlink()
    (workflow.bin / "sleep").unlink()
    workflow.env["TMUX_TMPDIR"] = str(sockets)
    shim = workflow.bin / "spawn-scope"
    workflow.fake(
        "spawn-scope",
        "#!/bin/bash\n"
        "# Like tmux's server: the pane's own process, and only it, into a scope of its own.\n"
        'delay=$(cat "@STATE@/move-delay-$WORKFLOW_SESSION" 2>/dev/null || echo never)\n'
        "scope=app.slice/app-tmux.slice/tmux-spawn-$$.scope\n"
        f'mkdir -p "{real_of}/$scope" "{fake}{named}/$scope"\n'
        f'for f in memory.max memory.swap.max; do echo max > "{fake}{named}/$scope/$f"; done\n'
        f'for f in memory.current memory.swap.current memory.low; do echo 0 > "{fake}{named}/$scope/$f"; done\n'
        'if [ "$delay" != never ]; then\n'
        f'  setsid -f bash -c \'/bin/sleep "$1"; echo "$2" > "$3/cgroup.procs"\' bash "$delay" "$$" '
        f'"{real_of}/$scope" </dev/null >/dev/null 2>&1\n'
        "fi\n"
        'exec "$@"\n',
    )
    workflow.fake(
        "tmux",
        "#!/bin/bash\n"
        "# The real tmux; a new session's command starts with the move tmux's server makes.\n"
        'out=(); new=""; placed=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  out+=("$1")\n'
        '  [ "$1" != new-session ] || new=1\n'
        '  if [ -n "$new" ] && [ -z "$placed" ] && [ "$1" = -c ]; then\n'
        f'    out+=("$2" "{shim}"); shift; placed=1\n'
        "  fi\n"
        "  shift\n"
        "done\n"
        f'exec {_TMUX} "${{out[@]}}"\n',
    )
    workflow.fake(
        "systemd-run",
        "#!/bin/bash\n"
        'echo "systemd-run $*" >> "@STATE@/calls.log"\n'
        'unit=""; memory=""; swap=""\n'
        'while [ $# -gt 0 ] && [ "$1" != -- ]; do\n'
        "  case $1 in\n"
        "    --unit=*) unit=${1#--unit=} ;;\n"
        '    MemoryMax=*) memory=$(numfmt --from=iec "${1#*=}") ;;\n'
        '    MemorySwapMax=*) swap=$(numfmt --from=iec "${1#*=}") ;;\n'
        "  esac\n"
        "  shift\n"
        "done\n"
        "shift\n"
        "scope=app.slice/$unit.scope\n"
        f'mkdir -p "{real_of}/$scope" "{fake}{named}/$scope"\n'
        f'echo "$memory" > "{fake}{named}/$scope/memory.max"\n'
        f'echo "$swap" > "{fake}{named}/$scope/memory.swap.max"\n'
        f'echo 0 > "{fake}{named}/$scope/memory.current"\n'
        f'echo 0 > "{fake}{named}/$scope/memory.swap.current"\n'
        f'echo 0 > "{fake}{named}/$scope/memory.low"\n'
        f'echo $$ > "{real_of}/$scope/cgroup.procs"\n'
        'exec "$@"\n',
    )
    workflow.fake(
        "claude",
        "#!/bin/bash\n"
        'if [ "$*" = "agents --json" ]; then echo "[]"; exit 0; fi\n'
        'sed -n "s/^0:://p" /proc/self/cgroup > "@STATE@/group-$WORKFLOW_SESSION"\n'
        f'echo "Remote Control: {SESSION_LINK}"\n'
        "exec /bin/sleep 600\n",
    )
    workflow.env["WORKFLOW_PROC_ROOT"] = "/proc"
    try:
        yield workflow
    finally:
        for socket in sockets.glob("tmux-*/*"):
            subprocess.run(
                [_TMUX, "-L", socket.name, "kill-server"],
                env=workflow.env,
                capture_output=True,
                check=False,
            )
        shutil.rmtree(sockets, ignore_errors=True)
        # Every process left in the test's groups ends, then the groups go.
        for _ in range(50):
            procs = [
                int(pid)
                for listing in real.rglob("cgroup.procs")
                for pid in listing.read_text().split()
            ]
            for pid in procs:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
            if not procs:
                break
            time.sleep(0.1)
        for folder in sorted(real.rglob("*"), key=lambda path: -len(path.parts)):
            if folder.is_dir():
                folder.rmdir()
        real.rmdir()


def _start_at_once(
    workflow: Workflow, numbers: list[int]
) -> list[tuple[int, str, str]]:
    """`session start worker N` for each, all at once, as a batch of tickets starts."""
    started = [
        subprocess.Popen(
            ["bash", str(workflow.script), "start", "worker", str(number)],
            cwd=workflow.repo,
            env={**workflow.env, **_LIMITS},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for number in numbers
    ]
    results = []
    for number, process in zip(numbers, started, strict=True):
        out, err = process.communicate(timeout=120)
        # Two fetches into one checkout at once can collide in git before anything of this pull
        # request runs; that start is made again, as the next tick would.
        if (
            process.returncode != 0
            and "systemd-run" not in err
            and not _launched(workflow, number)
        ):
            again = workflow.run("start", "worker", str(number), env=_LIMITS)
            out, err = again.stdout, again.stderr
            results.append((again.returncode, out, err))
            continue
        results.append((process.returncode, out, err))
    return results


def _launched(workflow: Workflow, number: int) -> bool:
    """Whether the start reached the session's scope at all."""
    calls = workflow.state / "calls.log"
    return calls.exists() and f"--unit=workflow-ticket-{number}-" in calls.read_text()


def _group_of_claude(workflow: Workflow, name: str) -> str:
    return (workflow.state / f"group-{name}").read_text().strip()


# The moment tmux's server moves the pane, after its process started: before the pane's process
# has started its child, about when it does, or well after.
_DELAYS = ["0", "0.001", "0.003", "0.01", "0.02", "0.05", "0.1", "0.2", "0.5", "never"]


@pytest.mark.parametrize("count", [3, 10, 15])
def test_sessions_started_at_once_each_hold_their_limit_whenever_tmux_moves_the_pane(
    scoped: Workflow, count: int
) -> None:
    numbers = list(range(1, count + 1))
    for number in numbers:
        scoped.add_pull_request(number)
        delay = _DELAYS[(number - 1) % len(_DELAYS)]
        (scoped.state / f"move-delay-ticket-{number}").write_text(delay + "\n")

    started = _start_at_once(scoped, numbers)
    # Every move has happened before the groups are read.
    time.sleep(0.8)

    outcomes = {
        f"ticket-{number}": (code, err.strip().splitlines()[-1:])
        for number, (code, _, err) in zip(numbers, started, strict=True)
    }
    refused = {name: said for name, (code, said) in outcomes.items() if code != 0}
    assert refused == {}
    groups = {f"ticket-{n}": _group_of_claude(scoped, f"ticket-{n}") for n in numbers}
    assert all(
        re.search(rf"/app\.slice/workflow-{name}-[0-9]+\.scope$", group)
        for name, group in groups.items()
    ), groups
    listed = scoped.run("list").stdout.splitlines()
    assert sorted(line.split("  ")[0] for line in listed) == sorted(groups), listed
    assert all("(limit 96 MB)" in line for line in listed), listed
    assert not any("no limit" in line for line in listed), listed


def test_a_session_started_while_tmux_moves_the_pane_is_squeezed_and_protected_in_its_own_scope(
    scoped: Workflow,
) -> None:
    """The daily commands act on the session's own group, never on tmux's scope of the pane."""
    scoped.add_pull_request(7)
    (scoped.state / "move-delay-ticket-7").write_text("0.02\n")
    done = scoped.run("start", "worker", "7", env=_LIMITS)
    assert done.returncode == 0, done.stderr
    time.sleep(0.5)
    group = Path(str(scoped.cgroup_root) + _group_of_claude(scoped, "ticket-7"))

    protected = scoped.run("protect", "ticket-7")
    running = scoped.run("running", "ticket-7")

    assert protected.returncode == 0, protected.stderr
    assert (group / "memory.low").read_text().strip() == str(4 * 1024 * _MB)
    assert running.returncode == 0, running.stderr
    spawn = [p for p in scoped.cgroup_root.rglob("tmux-spawn-*.scope") if p.is_dir()]
    assert spawn, "the pane was never moved"
    assert all((p / "memory.low").read_text().strip() == "0" for p in spawn)


def test_a_stopped_session_leaves_no_process_behind_in_its_scope(
    scoped: Workflow,
) -> None:
    """The session's command is now the pane's grandchild; stopping still ends it."""
    scoped.add_pull_request(7)
    (scoped.state / "move-delay-ticket-7").write_text("0.01\n")
    done = scoped.run("start", "worker", "7", env=_LIMITS)
    assert done.returncode == 0, done.stderr
    named = _group_of_claude(scoped, "ticket-7")
    assert _ROOT is not None
    real = Path(str(_ROOT) + named)
    assert (real / "cgroup.procs").read_text().split(), "nothing ran in the scope"

    stopped = scoped.run("stop", "ticket-7")

    assert stopped.returncode == 0, stopped.stderr
    for _ in range(50):
        if not (real / "cgroup.procs").read_text().split():
            break
        time.sleep(0.1)
    assert (real / "cgroup.procs").read_text().split() == []


def test_a_restart_never_takes_the_last_runs_note_for_its_own(scoped: Workflow) -> None:
    """The note of a session that held its limits is gone before the next start of that name,
    whose scope this time does not hold them."""
    scoped.add_pull_request(7)
    done = scoped.run("start", "worker", "7", env=_LIMITS)
    assert done.returncode == 0, done.stderr
    assert scoped.run("stop", "ticket-7").returncode == 0
    # The next scope is made without its limits.
    scoped.fake(
        "systemd-run",
        '#!/bin/bash\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n',
    )

    again = scoped.run("start", "worker", "7", env=_LIMITS)

    assert again.returncode != 0
    assert "was not started: its scope does not hold its memory limits" in again.stderr
    assert scoped.run("list").stdout == ""


def test_a_move_after_the_start_has_returned_leaves_the_limit_in_place(
    scoped: Workflow,
) -> None:
    """tmux's server is late, as on a loaded machine: it moves the pane after `session start`
    already said the session started."""
    scoped.add_pull_request(7)
    (scoped.state / "move-delay-ticket-7").write_text("2\n")
    done = scoped.run("start", "worker", "7", env=_LIMITS)
    assert done.returncode == 0, done.stderr
    time.sleep(2.5)
    spawn = [p for p in scoped.cgroup_root.rglob("tmux-spawn-*.scope") if p.is_dir()]
    assert spawn
    assert _ROOT is not None
    moved = [
        pid
        for p in spawn
        for pid in (_ROOT / p.relative_to(scoped.cgroup_root) / "cgroup.procs")
        .read_text()
        .split()
    ]

    listed = scoped.run("list").stdout

    assert moved, "the pane was never moved"
    assert re.search(
        r"/workflow-ticket-7-[0-9]+\.scope$", _group_of_claude(scoped, "ticket-7")
    )
    assert "(limit 96 MB)" in listed, listed


def test_a_scope_systemd_refuses_to_make_leaves_nothing_and_the_next_start_goes(
    scoped: Workflow,
) -> None:
    """The user's systemd refuses the scope (a unit of that name exists, its manager is not
    reachable): the start says so, and the next one, once systemd is back, starts."""
    scoped.add_pull_request(7)
    working = (scoped.bin / "systemd-run").read_text()
    scoped.fake(
        "systemd-run",
        "#!/bin/bash\necho 'Failed to start transient scope unit' >&2\nexit 1\n",
    )

    refused = scoped.run("start", "worker", "7", env=_LIMITS)

    assert refused.returncode != 0
    assert refused.stderr.strip().splitlines()[-1] == (
        "ticket-7 was not started: its scope with its memory limits of 96 MB and 16 MB of swap "
        "did not start"
    )
    assert "started:" not in refused.stdout
    assert scoped.run("list").stdout == ""
    (scoped.bin / "systemd-run").write_text(working)
    again = scoped.run("start", "worker", "7", env=_LIMITS)
    assert again.returncode == 0, again.stderr
    assert "(limit 96 MB)" in scoped.run("list").stdout
