"""`session list`, `squeeze`, `protect` and `unprotect` read and move a session's memory."""

import subprocess
from pathlib import Path

import pytest
from workflow_harness import Workflow

_MB = 1024 * 1024

_DD_FREED_ONLY_PART = """#!/bin/bash
cat > /dev/null
echo "dd: error writing 'memory.reclaim': Resource temporarily unavailable" >&2
exit 1
"""

_DD_NOT_ALLOWED = """#!/bin/bash
cat > /dev/null
echo "dd: failed to open 'memory.reclaim': Permission denied" >&2
exit 1
"""


def _files(folder: Path) -> list[str]:
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


def test_list_prints_a_line_for_each_running_session(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7", held=139 * _MB, swapped=748 * _MB)
    workflow.running("audit-12", held=1024 * _MB + 5, swapped=0)

    done = workflow.run("list")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "audit-12  busy  1024 MB held  0 MB in swap",
        "ticket-7  busy  139 MB held  748 MB in swap",
    ]


def test_a_session_is_idle_while_its_idle_file_exists(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.running("audit-7")
    (worktree / ".local" / "workflow").mkdir(parents=True)
    (worktree / ".local" / "workflow" / "audit-7.idle").touch()

    done = workflow.run("list")

    assert done.stdout.splitlines() == [
        "audit-7  idle  0 MB held  0 MB in swap",
        "ticket-7  busy  0 MB held  0 MB in swap",
    ]


def test_list_leaves_out_what_does_not_run_or_is_not_a_workflow_session(
    workflow: Workflow,
) -> None:
    workflow.socket("ticket-7")
    workflow.running("default")
    workflow.running("worker-probe")

    done = workflow.run("list")

    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


def test_list_still_names_a_session_whose_memory_cannot_be_read(workflow: Workflow) -> None:
    group = workflow.running("ticket-7")
    (group / "memory.current").unlink()

    done = workflow.run("list")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["ticket-7  busy  memory unknown"]


def test_squeeze_asks_the_kernel_to_reclaim_all_the_session_holds(workflow: Workflow) -> None:
    group = workflow.running("ticket-7", held=900 * _MB)
    other = workflow.running("ticket-8", held=500 * _MB)

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode == 0, done.stderr
    assert (group / "memory.reclaim").read_text().strip() == str(900 * _MB)
    assert (other / "memory.reclaim").read_text() == ""


def test_squeeze_succeeds_when_the_kernel_could_free_only_part(workflow: Workflow) -> None:
    workflow.running("ticket-7", held=900 * _MB)
    workflow.fake("dd", _DD_FREED_ONLY_PART)

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode == 0, done.stderr


def test_squeeze_fails_when_the_write_is_refused_for_another_reason(workflow: Workflow) -> None:
    workflow.running("ticket-7", held=900 * _MB)
    workflow.fake("dd", _DD_NOT_ALLOWED)

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode != 0
    assert "Permission denied" in done.stderr


def test_protect_keeps_four_gigabytes_from_being_reclaimed(workflow: Workflow) -> None:
    group = workflow.running("ticket-7")

    done = workflow.run("protect", "ticket-7")

    assert done.returncode == 0, done.stderr
    assert (group / "memory.low").read_text().strip() == str(4 * 1024 * _MB)


def test_unprotect_takes_the_protection_away(workflow: Workflow) -> None:
    group = workflow.running("ticket-7")
    workflow.run("protect", "ticket-7")

    done = workflow.run("unprotect", "ticket-7")

    assert done.returncode == 0, done.stderr
    assert (group / "memory.low").read_text().strip() == "0"


@pytest.mark.parametrize("command", ["squeeze", "protect", "unprotect"])
def test_a_session_that_does_not_run_is_one_line_and_creates_nothing(
    workflow: Workflow, command: str
) -> None:
    workflow.socket("ticket-7")
    before = _files(workflow.cgroup_root)

    done = workflow.run(command, "ticket-7")

    assert done.returncode != 0
    assert done.stderr.splitlines() == ["ticket-7 is not running"]
    assert done.stdout == ""
    assert _files(workflow.cgroup_root) == before


@pytest.mark.parametrize(
    ("command", "missing"),
    [("squeeze", "memory.reclaim"), ("protect", "memory.low"), ("unprotect", "memory.low")],
)
def test_a_control_group_file_that_is_missing_is_never_created(
    workflow: Workflow, command: str, missing: str
) -> None:
    group = workflow.running("ticket-7", held=_MB)
    (group / missing).unlink()

    done = workflow.run(command, "ticket-7")

    assert done.returncode != 0
    assert not (group / missing).exists()


@pytest.mark.parametrize("command", ["squeeze", "protect", "unprotect", "stop"])
@pytest.mark.parametrize("name", ["", "default", "ticket-", "ticket-7a", "../ticket-7", "worker-7"])
def test_a_name_that_is_not_a_workflow_session_is_rejected(
    workflow: Workflow, command: str, name: str
) -> None:
    done = workflow.run(command, name)

    assert done.returncode != 0
    assert "usage" in done.stderr.lower()
    assert workflow.calls() == []


@pytest.mark.parametrize("command", ["squeeze", "protect", "unprotect"])
def test_a_session_without_a_control_group_of_its_own_is_refused(
    workflow: Workflow, command: str
) -> None:
    """Without this, the memory of everything else in the shared group would be moved."""
    shared = workflow.running("ticket-7", held=900 * _MB, scope="user.slice/session-2.scope")

    done = workflow.run(command, "ticket-7")

    assert done.returncode != 0
    assert done.stderr.splitlines() == ["ticket-7 has no control group of its own"]
    assert (shared / "memory.reclaim").read_text() == ""
    assert (shared / "memory.low").read_text() == "0\n"


def test_list_does_not_print_a_figure_it_could_not_read(workflow: Workflow) -> None:
    group = workflow.running("ticket-7")
    (group / "memory.swap.current").write_text("")

    done = workflow.run("list")

    assert done.stdout.splitlines() == ["ticket-7  busy  memory unknown"]


def test_squeeze_fails_when_the_held_memory_cannot_be_read(workflow: Workflow) -> None:
    group = workflow.running("ticket-7")
    (group / "memory.current").unlink()

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode != 0
    assert "cannot read" in done.stderr
    assert (group / "memory.reclaim").read_text() == ""


def test_browser_queue_lock_overrides_an_idle_mark(workflow: Workflow) -> None:
    import fcntl

    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")
    notes = worktree / ".local/workflow"
    notes.mkdir(parents=True)
    (notes / "ticket-7.idle").touch()
    with (notes / "browser-tests.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        assert "ticket-7  busy " in workflow.run("list").stdout
    assert "ticket-7  idle " in workflow.run("list").stdout


def _own_scope(name: str) -> str:
    return f"user.slice/user@1000.service/app.slice/workflow-{name}-4242.scope"


def test_list_shows_the_memory_limit_next_to_what_the_session_holds(workflow: Workflow) -> None:
    limited = workflow.running("ticket-7", held=139 * _MB, scope=_own_scope("ticket-7"))
    (limited / "memory.max").write_text(f"{8 * 1024 * _MB}\n")
    # A session started before sessions had a scope of their own.
    older = workflow.running("ticket-8", held=200 * _MB)
    (older / "memory.max").write_text("max\n")

    done = workflow.run("list")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "ticket-7  busy  139 MB held (limit 8 GB)  0 MB in swap",
        "ticket-8  busy  200 MB held (no limit)  0 MB in swap",
    ]


def test_a_limit_of_no_whole_gigabytes_is_shown_in_megabytes(workflow: Workflow) -> None:
    group = workflow.running("ticket-7", scope=_own_scope("ticket-7"))
    (group / "memory.max").write_text(f"{64 * _MB}\n")

    assert workflow.run("list").stdout.splitlines() == [
        "ticket-7  busy  0 MB held (limit 64 MB)  0 MB in swap"
    ]


@pytest.mark.parametrize(
    ("command", "file", "written"),
    [("squeeze", "memory.reclaim", 900 * _MB), ("protect", "memory.low", 4 * 1024 * _MB)],
)
def test_the_scope_a_session_was_started_in_is_its_own_group(
    workflow: Workflow, command: str, file: str, written: int
) -> None:
    group = workflow.running("ticket-7", held=900 * _MB, scope=_own_scope("ticket-7"))

    done = workflow.run(command, "ticket-7")

    assert done.returncode == 0, done.stderr
    assert (group / file).read_text().strip() == str(written)


@pytest.mark.parametrize("other", ["ticket-71", "ticket-8", "audit-7"])
def test_the_scope_of_another_session_is_refused(workflow: Workflow, other: str) -> None:
    """`ticket-7` must not act on `ticket-71`'s scope, whose name begins the same way."""
    group = workflow.running("ticket-7", held=900 * _MB, scope=_own_scope(other))

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode != 0
    assert done.stderr.splitlines() == ["ticket-7 has no control group of its own"]
    assert (group / "memory.reclaim").read_text() == ""


def _spawned(name: str) -> str:
    return f"user.slice/user@1000.service/app.slice/app-tmux.slice/tmux-spawn-{name}.scope"


def test_list_reads_the_scope_the_pane_s_child_made(workflow: Workflow) -> None:
    """A tmux with systemd support moves the pane's own process into a scope of its own; the
    session's command runs in the scope that process's child made."""
    group = workflow.running(
        "ticket-7", held=139 * _MB, scope=_own_scope("ticket-7"), pane_scope=_spawned("ticket-7")
    )
    (group / "memory.max").write_text(f"{8 * 1024 * _MB}\n")
    pane = workflow.cgroup_root / _spawned("ticket-7")
    pane.mkdir(parents=True)
    (pane / "memory.max").write_text("max\n")

    done = workflow.run("list")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["ticket-7  busy  139 MB held (limit 8 GB)  0 MB in swap"]


def test_squeeze_reclaims_the_scope_the_pane_s_child_made(workflow: Workflow) -> None:
    group = workflow.running(
        "ticket-7", held=900 * _MB, scope=_own_scope("ticket-7"), pane_scope="user.slice"
    )

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode == 0, done.stderr
    assert (group / "memory.reclaim").read_text().strip() == str(900 * _MB)


def test_a_child_in_the_scope_of_another_session_is_refused(workflow: Workflow) -> None:
    group = workflow.running(
        "ticket-7", held=900 * _MB, scope=_own_scope("ticket-71"), pane_scope="user.slice"
    )

    done = workflow.run("squeeze", "ticket-7")

    assert done.returncode != 0
    assert done.stderr.splitlines() == ["ticket-7 has no control group of its own"]
    assert (group / "memory.reclaim").read_text() == ""


def _scope_check(workflow: Workflow, name: str) -> list[str]:
    """The words the session's scope runs first, its command last, as `tmux` was given them."""
    arguments = (workflow.state / "args" / name).read_text().splitlines()
    return arguments[arguments.index("--") + 1 :]


@pytest.mark.parametrize(
    ("memory", "swap", "note", "ran"),
    [
        (f"{8 * 1024 * _MB}", f"{4 * 1024 * _MB}", "in place", True),
        ("max", f"{4 * 1024 * _MB}", f"memory.max max, memory.swap.max {4 * 1024 * _MB}", False),
        (f"{8 * 1024 * _MB}", "max", f"memory.max {8 * 1024 * _MB}, memory.swap.max max", False),
        (None, None, "memory.max missing, memory.swap.max missing", False),
    ],
    ids=["in-place", "no-memory-limit", "no-swap-limit", "no-memory-controller"],
)
def test_the_scope_runs_the_command_only_once_it_holds_the_limits(
    workflow: Workflow, memory: str | None, swap: str | None, note: str, ran: bool
) -> None:
    """The command never runs without its limits, whatever placed the pane."""
    workflow.add_pull_request(7)
    workflow.run("start", "worker", "7")
    check = _scope_check(workflow, "ticket-7")
    own = Path(Path("/proc/self/cgroup").read_text().split("::", 1)[1].strip())
    group = workflow.cgroup_root / own.relative_to("/")
    group.mkdir(parents=True, exist_ok=True)
    for file, value in (("memory.max", memory), ("memory.swap.max", swap)):
        (group / file).unlink(missing_ok=True)
        if value is not None:
            (group / file).write_text(f"{value}\n")
    marker = workflow.state / "command-ran"
    note_file = Path(check[4])
    note_file.unlink()

    done = subprocess.run([*check[:-1], f"touch {marker}"], capture_output=True, text=True)

    assert note_file.read_text() == f"{note}\n"
    assert marker.exists() == ran
    assert done.returncode == (0 if ran else 1)
    assert done.stderr == ""


def test_a_start_whose_scope_does_not_hold_the_limits_is_refused(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "limits-not-set-ticket-7").write_text("memory.max max, memory.swap.max max\n")

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == (
        "ticket-7 was not started: its scope does not hold its memory limits of 8 GB and 4 GB of "
        "swap (memory.max max, memory.swap.max max)"
    )
    assert done.stdout == ""
    assert not workflow.is_running("ticket-7")
    assert not (workflow.notes(7) / "ticket-7.url").exists()
    assert workflow.calls("sleep") == []


def test_a_start_whose_scope_never_starts_is_stopped_and_refused(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "scope-not-checked-ticket-7").touch()

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == (
        "ticket-7 was not started: its scope with its memory limits of 8 GB and 4 GB of swap "
        "did not start within 30 seconds"
    )
    assert not workflow.is_running("ticket-7")
    assert workflow.calls("sleep") == ["sleep 0.1"] * 300
    assert not (workflow.notes(7) / "ticket-7.url").exists()


def test_a_start_whose_session_ends_before_its_scope_started_is_refused_at_once(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / "scope-not-checked-ticket-7").touch()
    (workflow.state / "dies-at-once").touch()

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == (
        "ticket-7 was not started: its scope with its memory limits of 8 GB and 4 GB of swap "
        "did not start"
    )
    assert workflow.calls("sleep") == []


def test_the_limits_are_checked_in_bytes_as_the_kernel_holds_them(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    workflow.run(
        "start",
        "worker",
        "7",
        env={"WORKFLOW_SESSION_MEMORY_MAX": "96M", "WORKFLOW_SESSION_SWAP_MAX": "0"},
    )

    assert _scope_check(workflow, "ticket-7")[6:8] == [str(96 * _MB), "0"]


@pytest.mark.parametrize("variable", ["WORKFLOW_SESSION_MEMORY_MAX", "WORKFLOW_SESSION_SWAP_MAX"])
@pytest.mark.parametrize("value", ["lots", "infinity", "-1", "8 G"])
def test_a_limit_that_is_not_a_size_is_refused_before_anything_starts(
    workflow: Workflow, variable: str, value: str
) -> None:
    workflow.add_pull_request(7)

    done = workflow.run("start", "worker", "7", env={variable: value})

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == f"{variable} is '{value}', not a size such as 8G"
    assert workflow.calls("tmux ticket-7 new-session") == []


def test_a_hunt_refused_for_its_limits_leaves_no_report_folder(workflow: Workflow) -> None:
    """Else the next start of a hunt of that name finds a report already."""
    (workflow.state / "limits-not-set-hunt-foo").write_text("memory.max max, memory.swap.max max\n")

    done = workflow.run("start", "hunt", "foo")

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == (
        "hunt-foo was not started: its scope does not hold its memory limits of 8 GB and 4 GB of "
        "swap (memory.max max, memory.swap.max max)"
    )
    assert not (workflow.repo / ".local" / "hunts" / "foo").exists()


def test_a_cloud_launcher_refused_for_its_limits_says_so_without_waiting(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    # A scope with no memory controller to set the limits with.
    workflow.fake(
        "systemd-run", '#!/bin/bash\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n'
    )

    done = workflow.run("start", "worker", "7", "cloud")

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == (
        "cloud-7 was not started: its scope does not hold its memory limits of 8 GB and 4 GB of "
        "swap (memory.max missing, memory.swap.max missing)"
    )
    assert workflow.calls("claude --effort") == []
    assert workflow.calls("sleep") == []


def _pane_of_launch(workflow: Workflow, name: str) -> None:
    """The pane's own process is the one `launch` starts, which only waits for its child."""
    pid = (workflow.state / "pid" / name).read_text().strip()
    words = ["bash", "-c", '"$@"; exit', "bash", "systemd-run", "--user", "--scope"]
    (workflow.root / "proc" / pid / "cmdline").write_bytes("\0".join(words).encode() + b"\0")


@pytest.mark.parametrize("child", [False, True], ids=["no-child", "child-elsewhere"])
def test_tmux_s_own_scope_is_not_the_group_of_a_pane_launch_started(
    workflow: Workflow, child: bool
) -> None:
    """It holds only the waiting process: its child's scope, not found, is the session's group."""
    if child:
        workflow.running(
            "ticket-7", scope="user.slice/session-2.scope", pane_scope=_spawned("ticket-7")
        )
    else:
        workflow.running("ticket-7", scope=_spawned("ticket-7"))
    _pane_of_launch(workflow, "ticket-7")

    listed = workflow.run("list")
    squeezed = workflow.run("squeeze", "ticket-7")

    assert listed.stdout.splitlines() == ["ticket-7  busy  memory unknown"]
    assert squeezed.stderr.splitlines() == ["ticket-7 has no control group of its own"]
