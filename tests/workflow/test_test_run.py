"""`test-run` stops a session's test command at its whole-run limit and says why a run ended."""

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from workflow_harness import SCRIPTS

_GB = 1024**3

# A child that starts a session of its own, as Playwright's browsers do: neither the stop of the
# command's process group nor the kill of what is left of it reaches it.
_DETACHED = (
    "import subprocess, sys\n"
    "child = subprocess.Popen(['sleep', '600'], start_new_session=True)\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "child.wait()\n"
)


def _run(
    *command: str,
    env: dict[str, str] | None = None,
    session: bool = True,
    preexec_fn: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    base = {key: value for key, value in os.environ.items() if key != "WORKFLOW_SESSION"}
    if session:
        base["WORKFLOW_SESSION"] = "ticket-7"
    return subprocess.run(
        ["bash", str(SCRIPTS / "test-run"), *command],
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        preexec_fn=preexec_fn,
    )


def _ended(pid_file: Path, seconds: float = 10) -> bool:
    """Whether the process has ended. It is reaped by whoever adopted it, a moment later."""
    status = Path(f"/proc/{pid_file.read_text().strip()}/status")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if "\nState:\tZ" in status.read_text():
                return True
        except FileNotFoundError:
            return True
        time.sleep(0.05)
    return False


def _own_group(root: Path) -> Path:
    """A stand-in for the control group this test runs in, which the command inherits."""
    path = Path("/proc/self/cgroup").read_text().splitlines()[0].removeprefix("0::")
    group = root / path.lstrip("/")
    group.mkdir(parents=True)
    (group / "memory.events").write_text("low 0\nhigh 0\nmax 3\noom 0\noom_kill 0\n")
    return group


def test_outside_a_session_the_command_only_runs(tmp_path: Path) -> None:
    """In CI and in the owner's shell nothing is limited and nothing is added."""
    done = _run(
        "bash", "-c", "sleep 2; echo ran; exit 3",
        env={"WORKFLOW_TEST_RUN_SECONDS": "1"}, session=False,
    )  # fmt: skip

    assert done.returncode == 3
    assert done.stdout == "ran\n"
    assert done.stderr == ""


def test_a_session_command_that_ends_keeps_its_output_and_status() -> None:
    done = _run("bash", "-c", "echo ran; echo failed >&2; exit 3")

    assert done.returncode == 3
    assert done.stdout == "ran\n"
    assert done.stderr == "failed\n"


def _ignored_signals(done: subprocess.CompletedProcess[str]) -> set[int]:
    """The signals ignored by the command, from the `SigIgn` line it printed of its status."""
    mask = int(done.stdout.removeprefix("SigIgn:").strip(), 16)
    return {number for number in range(1, 65) if mask & (1 << (number - 1))}


def _handle_interrupt_and_quit(handler: signal.Handlers) -> Callable[[], None]:
    def start() -> None:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGQUIT, handler)

    return start


def test_the_command_gets_interrupt_and_quit_as_the_run_did() -> None:
    """A background job of a shell starts with both ignored, and Python never raises
    `KeyboardInterrupt` then: a test that stops a child with SIGINT would wait for it. The run
    starts with both at their default, whatever this test was started with: a pytest started
    as a background job has them ignored too."""
    done = _run(
        "grep", "SigIgn", "/proc/self/status",
        preexec_fn=_handle_interrupt_and_quit(signal.SIG_DFL),
    )  # fmt: skip

    assert done.returncode == 0
    assert _ignored_signals(done).isdisjoint({signal.SIGINT, signal.SIGQUIT})


def test_a_run_started_with_interrupt_and_quit_ignored_passes_them_on_as_timeout_does() -> None:
    """The run adds nothing of its own to a caller who ignored both: the command has them as
    `timeout` alone gives them. GNU `timeout` handles both itself, so its command never has them
    ignored; the `timeout` of uutils keeps them ignored."""
    ignore = _handle_interrupt_and_quit(signal.SIG_IGN)
    status = ("grep", "SigIgn", "/proc/self/status")
    done = _run(*status, preexec_fn=ignore)
    alone = subprocess.run(
        ["timeout", "60", *status],
        capture_output=True, text=True, timeout=60, check=False, preexec_fn=ignore,
    )  # fmt: skip

    assert done.returncode == 0
    assert alone.returncode == 0
    both = {signal.SIGINT, signal.SIGQUIT}
    assert _ignored_signals(done) & both == _ignored_signals(alone) & both


@pytest.mark.parametrize(
    ("caller", "ignored"),
    [(signal.SIG_DFL, set[int]()), (signal.SIG_IGN, {signal.SIGINT, signal.SIGQUIT})],
    ids=["default", "ignored"],
)
def test_with_a_timeout_that_passes_signals_on_the_command_has_them_as_the_caller_did(
    programs_folder: Path, caller: signal.Handlers, ignored: set[int]
) -> None:
    """GNU `timeout`, as in CI, gives its command both at their default whatever the run did, so
    the two tests above cannot catch a missing reset there. A `timeout` that changes no signal,
    as the one of uutils, shows what the run itself passes on."""
    stand_in = programs_folder / "timeout"
    stand_in.write_text('#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; done\nshift\nexec "$@"\n')
    stand_in.chmod(0o755)
    done = _run(
        "grep", "SigIgn", "/proc/self/status",
        env={"PATH": f"{programs_folder}:{os.environ['PATH']}"},
        preexec_fn=_handle_interrupt_and_quit(caller),
    )  # fmt: skip

    assert done.returncode == 0
    assert _ignored_signals(done) & {signal.SIGINT, signal.SIGQUIT} == ignored


def test_a_command_that_never_ends_is_stopped_at_the_limit_with_its_children(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child"
    started = time.monotonic()

    done = _run(
        "bash", "-c", f"sleep 600 & echo $! > {child}; wait",
        env={"WORKFLOW_TEST_RUN_SECONDS": "1"},
    )  # fmt: skip

    assert time.monotonic() - started < 30
    assert done.returncode == 124
    assert done.stderr.splitlines()[-1] == (
        "stopped: the test run ran longer than its limit of 1 s. A test or a step that never "
        "ends is the likely cause, not the machine: find it before running again."
    )
    # The process group goes, not only the command: a test runner's workers would stay behind.
    assert _ended(child)


def test_a_command_that_exits_124_by_itself_is_not_said_to_have_run_too_long() -> None:
    done = _run("bash", "-c", "exit 124", env={"WORKFLOW_TEST_RUN_SECONDS": "60"})

    assert done.returncode == 124
    assert done.stderr == ""


def test_a_run_the_kernel_killed_at_the_memory_limit_says_so(tmp_path: Path) -> None:
    group = _own_group(tmp_path / "cgroup")
    (group / "memory.max").write_text(f"{8 * _GB}\n")
    # What the kernel does to the largest process of the scope: it counts the kill and kills.
    kill = f"sed -i 's/^oom_kill 0$/oom_kill 1/' {group / 'memory.events'}; kill -9 $$"

    done = _run("bash", "-c", kill, env={"WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup")})

    assert done.returncode == 137
    assert done.stderr.splitlines()[-1] == (
        "stopped: the test run passed its memory limit of 8 GB. A loop or a leak is the likely "
        "cause, not the machine: find it before running again."
    )


def test_what_is_left_of_a_run_the_kernel_killed_at_the_memory_limit_goes_too(
    tmp_path: Path,
) -> None:
    """The kernel kills only the largest process: its workers would go on without a limit."""
    group = _own_group(tmp_path / "cgroup")
    (group / "memory.max").write_text(f"{8 * _GB}\n")
    child = tmp_path / "child"
    kill = (
        f"sleep 600 & echo $! > {child}; "
        f"sed -i 's/^oom_kill 0$/oom_kill 1/' {group / 'memory.events'}; kill -9 $$"
    )

    limits = {
        "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
        "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1",
    }

    done = _run("bash", "-c", kill, env=limits)

    assert done.returncode == 137
    assert done.stderr.splitlines()[-1].startswith("stopped: the test run passed its memory limit")
    assert _ended(child)


def test_a_failure_without_a_kill_at_the_limit_adds_nothing(tmp_path: Path) -> None:
    group = _own_group(tmp_path / "cgroup")
    (group / "memory.max").write_text(f"{8 * _GB}\n")

    done = _run("bash", "-c", "kill -9 $$", env={"WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup")})

    assert done.returncode == 137
    assert done.stderr == ""


def test_a_command_killed_while_the_shell_is_between_commands_prints_no_job_notice(
    tmp_path: Path,
) -> None:
    """Bash tells of a job killed by a signal ("<pid> Killed ( trap - INT QUIT; ...") before the
    first command after it reaped it, wherever that is, not only in a quiet `wait`. Each command
    of the run's shell is held back, as on a loaded machine, so the command dies between two."""
    (tmp_path / "slow.sh").write_text("trap 'sleep 0.05' DEBUG\n")  # bash reads `BASH_ENV`

    done = _run("bash", "-c", "kill -9 $$", env={"BASH_ENV": str(tmp_path / "slow.sh")})

    assert done.returncode == 137
    assert done.stderr == ""


def test_a_stop_sent_to_the_run_reaches_the_command_and_its_children(tmp_path: Path) -> None:
    """The session ending a run it gave up on must not leave the test runner going."""
    child = tmp_path / "child"
    run = subprocess.Popen(
        ["bash", str(SCRIPTS / "test-run"), "bash", "-c", f"sleep 600 & echo $! > {child}; wait"],
        env={**os.environ, "WORKFLOW_SESSION": "ticket-7"},
    )
    try:
        deadline = time.monotonic() + 20
        while not child.exists() or not child.read_text().strip():
            assert time.monotonic() < deadline
            time.sleep(0.05)

        run.terminate()

        assert run.wait(timeout=20) != 0
        assert _ended(child)
    finally:
        run.kill()


def test_a_command_that_ignores_the_stop_is_killed_after_its_grace_time(tmp_path: Path) -> None:
    done = _run(
        "bash", "-c", "trap '' TERM; sleep 600",
        env={"WORKFLOW_TEST_RUN_SECONDS": "1", "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1"},
    )  # fmt: skip

    assert done.returncode == 137
    assert done.stderr.splitlines()[-1].startswith(
        "stopped: the test run ran longer than its limit of 1 s."
    )


def test_a_memory_stop_without_a_limit_it_can_read_names_no_size(tmp_path: Path) -> None:
    group = _own_group(tmp_path / "cgroup")
    (group / "memory.max").write_text("max\n")
    kill = f"sed -i 's/^oom_kill 0$/oom_kill 1/' {group / 'memory.events'}; kill -9 $$"

    done = _run("bash", "-c", kill, env={"WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup")})

    assert done.stderr.splitlines()[-1].startswith(
        "stopped: the test run passed its memory limit. A loop or a leak"
    )


def test_a_worker_that_ignores_the_stop_goes_once_the_command_has_ended(tmp_path: Path) -> None:
    """`timeout` returns when the command dies of the stop, though its worker lives on."""
    child = tmp_path / "child"
    worker = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"

    done = _run(
        "bash", "-c", f'python3 -c "{worker}" & echo $! > {child}; wait',
        env={"WORKFLOW_TEST_RUN_SECONDS": "1", "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1"},
    )  # fmt: skip

    assert done.returncode == 124
    assert _ended(child)


def test_without_a_group_a_child_in_a_session_of_its_own_goes_at_the_time_limit(
    tmp_path: Path,
) -> None:
    """With no group to make (a stand-in root, as in Codex's sandbox), the run adopts orphans."""
    child = tmp_path / "child"
    (tmp_path / "detach.py").write_text(_DETACHED)

    done = _run(
        sys.executable, str(tmp_path / "detach.py"), str(child),
        env={
            "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
            "WORKFLOW_TEST_RUN_SECONDS": "2",
            "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1",
        },
    )  # fmt: skip

    assert done.returncode == 124, done.stderr
    assert _gone(child)


def test_without_a_group_a_child_in_a_session_of_its_own_goes_with_a_stop_sent_to_the_run(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child"
    (tmp_path / "detach.py").write_text(_DETACHED)
    env = {
        **os.environ,
        "WORKFLOW_SESSION": "ticket-7",
        "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
        "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1",
    }
    detach = [sys.executable, str(tmp_path / "detach.py"), str(child)]
    run = subprocess.Popen(["bash", str(SCRIPTS / "test-run"), *detach], env=env)
    try:
        _wait_for(child)

        run.terminate()

        assert run.wait(timeout=30) != 0
        assert _gone(child)
    finally:
        run.kill()


def test_without_a_group_a_child_in_a_session_of_its_own_goes_at_the_memory_limit(
    tmp_path: Path,
) -> None:
    group = _own_group(tmp_path / "cgroup")
    child = tmp_path / "child"
    (tmp_path / "detach.py").write_text(_DETACHED)
    kill = (
        f"{sys.executable} {tmp_path / 'detach.py'} {child} >/dev/null 2>&1 & "
        f"while [ ! -s {child} ]; do sleep 0.05; done; "
        f"sed -i 's/^oom_kill 0$/oom_kill 1/' {group / 'memory.events'}; kill -9 $$"
    )
    limits = {
        "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
        "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1",
    }

    done = _run("bash", "-c", kill, env=limits)

    assert done.returncode == 137, done.stderr
    assert done.stderr.splitlines()[-1].startswith("stopped: the test run passed its memory limit")
    assert _gone(child)


def test_without_a_group_the_children_of_a_child_in_a_session_of_its_own_go_too(
    tmp_path: Path,
) -> None:
    """A browser has renderers under it: each process killed leaves its children to the run."""
    child, grandchild = tmp_path / "child", tmp_path / "grandchild"
    command = (
        f"setsid bash -c 'sleep 600 & echo $! > {grandchild}; wait' >/dev/null 2>&1 & "
        f"echo $! > {child}; wait"
    )

    done = _run(
        "bash", "-c", command,
        env={
            "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
            "WORKFLOW_TEST_RUN_SECONDS": "2",
            "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1",
        },
    )  # fmt: skip

    assert done.returncode == 124, done.stderr
    assert _gone(child)
    assert _gone(grandchild)


# A detached process whose main thread ends while a worker thread runs on: it is shown as a
# zombie until that thread ends. The file gets the worker thread's number.
_DETACHED_THREAD = (
    "import ctypes, platform, sys, threading, time\n"
    "worker = threading.Thread(target=time.sleep, args=(600,))\n"
    "worker.start()\n"
    "open(sys.argv[1], 'w').write(str(worker.native_id))\n"
    "ctypes.CDLL(None).syscall({'x86_64': 60, 'aarch64': 93}[platform.machine()], 0)  # exit\n"
)


def test_without_a_group_a_detached_process_whose_main_thread_ended_goes_too(
    tmp_path: Path,
) -> None:
    worker = tmp_path / "worker"
    (tmp_path / "thread.py").write_text(_DETACHED_THREAD)
    command = f"setsid {sys.executable} {tmp_path / 'thread.py'} {worker} >/dev/null 2>&1 & wait"

    done = _run(
        "bash", "-c", command,
        env={
            "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
            "WORKFLOW_TEST_RUN_SECONDS": "2",
            "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1",
        },
    )  # fmt: skip

    assert done.returncode == 124, done.stderr
    assert _gone(worker)


def test_without_a_group_the_command_gets_the_signals_the_run_got(tmp_path: Path) -> None:
    """The step that makes the script a subreaper runs Python, which ignores SIGPIPE."""
    env = {"WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup")}
    pipe = "kill -s PIPE $$; echo survived"

    default = _run("bash", "-c", pipe, env=env)
    inner = f"bash {SCRIPTS / 'test-run'} bash -c '{pipe}'"
    ignored = _run("bash", "-c", f"trap '' PIPE; {inner}", env=env)

    assert (default.returncode, default.stdout) == (141, ""), default.stderr
    assert (ignored.returncode, ignored.stdout) == (0, "survived\n"), ignored.stderr


def test_without_a_group_a_run_that_ends_by_itself_leaves_its_children_alone(
    tmp_path: Path,
) -> None:
    """Only a stopped run is cleaned up, as with a group: a run that ended was not stopped."""
    child = tmp_path / "child"
    command = f"setsid sleep 600 >/dev/null 2>&1 & echo $! > {child}"

    done = _run("bash", "-c", command, env={"WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup")})

    assert done.returncode == 0, done.stderr
    assert not _ended(child, seconds=1)
    os.kill(int(child.read_text()), 9)


def _codex_sandbox() -> list[str]:
    """The sandbox of a Codex worker (`start_codex_worker` in `session`)."""
    return ["codex", "sandbox", "-P", ":workspace", "--"]


def _has_codex_sandbox() -> bool:
    if shutil.which("codex") is None:
        return False
    made = subprocess.run([*_codex_sandbox(), "true"], capture_output=True, timeout=60, check=False)
    return made.returncode == 0


# Environment prerequisite: Codex, which CI's runners do not have.
@pytest.mark.skipif(  # noqa: TID251
    not _has_codex_sandbox(), reason="Codex is not installed or its sandbox does not start"
)
def test_in_a_codex_sandbox_a_child_in_a_session_of_its_own_goes_at_the_time_limit(
    tmp_path: Path,
) -> None:
    """The sandbox mounts the control groups read-only, so the run has no group of its own."""
    child = tmp_path / "child"
    (tmp_path / "detach.py").write_text(_DETACHED)
    # The sandbox has process numbers of its own: the child is looked for from inside it.
    session = f"""
export WORKFLOW_SESSION=ticket-7 WORKFLOW_TEST_RUN_SECONDS=2 WORKFLOW_TEST_RUN_GRACE_SECONDS=1
bash {SCRIPTS / "test-run"} {sys.executable} {tmp_path / "detach.py"} {child}
echo "status: $?"
pid=$(cat {child})
# A letter other than Z: the child has not ended. It is reaped a moment after it ends.
for _ in $(seq 100); do
  [[ $(sed -n 's/^State:\\t//p' /proc/$pid/status 2>/dev/null) == [^Z]* ]] || break
  sleep 0.1
done
if [[ $(sed -n 's/^State:\\t//p' /proc/$pid/status 2>/dev/null) == [^Z]* ]]; then
  echo 'child: running'; kill -9 $pid
else
  echo 'child: gone'
fi
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("WORKFLOW_")}

    done = subprocess.run(
        [*_codex_sandbox(), *_script(tmp_path, session)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip

    assert done.stdout.splitlines()[-2:] == ["status: 124", "child: gone"], done.stderr


def _has_delegated_scopes() -> bool:
    """Whether this machine makes the scopes sessions run in: a user's systemd with memory."""
    if shutil.which("systemd-run") is None:
        return False
    made = subprocess.run([*_scope("16M"), "true"], capture_output=True, timeout=30, check=False)
    return made.returncode == 0


def _scope(memory: str = "1G") -> list[str]:
    """A scope like a session's (`session start`), which the run is started in."""
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "Delegate=yes",
        "-p", f"MemoryMax={memory}", "-p", "MemorySwapMax=0", "-p", "OOMPolicy=continue", "--",
    ]  # fmt: skip


def _session_env(**limits: str) -> dict[str, str]:
    base = {key: value for key, value in os.environ.items() if not key.startswith("WORKFLOW_")}
    return {
        **base,
        "WORKFLOW_SESSION": "ticket-7",
        "WORKFLOW_TEST_RUN_GRACE_SECONDS": "1",
        **limits,
    }


def _in_scope(
    tmp_path: Path, command: list[str], memory: str = "1G", **limits: str
) -> tuple[int, str]:
    """Runs `command` in a scope; its output goes to files, which a detached child may hold."""
    with (tmp_path / "out").open("w") as out, (tmp_path / "err").open("w") as err:
        done = subprocess.run(
            [*_scope(memory), *command],
            env=_session_env(**limits), stdout=out, stderr=err, timeout=60, check=False,
        )  # fmt: skip
    return done.returncode, (tmp_path / "err").read_text()


def _script(tmp_path: Path, text: str) -> list[str]:
    """A shell script for the scope: `systemd-run` would read a `$$` in its words as `$`."""
    script = tmp_path / "session.sh"
    script.write_text(text)
    return ["bash", str(script)]


def _gone(pid_file: Path) -> bool:
    """`_ended`, and a child that has not ended is killed, so a failure leaves nothing behind."""
    if _ended(pid_file):
        return True
    os.kill(int(pid_file.read_text()), 9)
    return False


def _wait_for(pid_file: Path) -> None:
    deadline = time.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline, "the detached child did not start"
        time.sleep(0.05)


# Environment prerequisite: a user's systemd that makes scopes; CI's runners have none.
_REAL_SCOPES = pytest.mark.skipif(  # noqa: TID251
    not _has_delegated_scopes(), reason="no user systemd to make scopes"
)


@_REAL_SCOPES
def test_a_child_in_a_session_of_its_own_goes_at_the_time_limit_and_the_session_stays(
    tmp_path: Path,
) -> None:
    """Only the run goes: a process of the session beside it, in the same scope, goes on."""
    child, beside = tmp_path / "child", tmp_path / "beside"
    (tmp_path / "detach.py").write_text(_DETACHED)
    session = f"""
sleep 600 & echo $! > {beside}
bash {SCRIPTS / "test-run"} bash -c '{sys.executable} {tmp_path / "detach.py"} {child} &
  while [ ! -s {child} ]; do sleep 0.05; done; wait'
status=$?
kill -0 "$(cat {beside})" && echo 'beside: running'
kill "$(cat {beside})"
exit $status
"""

    status, stderr = _in_scope(tmp_path, _script(tmp_path, session), WORKFLOW_TEST_RUN_SECONDS="3")

    assert status == 124, stderr
    assert stderr.splitlines()[-1].startswith(
        "stopped: the test run ran longer than its limit of 3 s."
    )
    assert _gone(child)
    assert (tmp_path / "out").read_text() == "beside: running\n"


@_REAL_SCOPES
def test_a_child_in_a_session_of_its_own_goes_at_the_memory_limit(tmp_path: Path) -> None:
    """The real kernel kills the largest process of the scope; the detached child goes too."""
    child = tmp_path / "child"
    (tmp_path / "detach.py").write_text(_DETACHED)
    (tmp_path / "allocate.py").write_text(
        "held = []\nwhile True:\n    held.append(b'x' * (8 << 20))\n"
    )
    command = (
        f"{sys.executable} {tmp_path / 'detach.py'} {child} & "
        f"while [ ! -s {child} ]; do sleep 0.05; done; "
        f"exec {sys.executable} {tmp_path / 'allocate.py'}"
    )

    status, stderr = _in_scope(
        tmp_path, ["bash", str(SCRIPTS / "test-run"), "bash", "-c", command], memory="96M"
    )

    assert status == 137, stderr
    assert stderr.splitlines()[-1] == (
        "stopped: the test run passed its memory limit of 96 MB. A loop or a leak is the likely "
        "cause, not the machine: find it before running again."
    )
    assert _gone(child)


@_REAL_SCOPES
def test_a_run_inside_a_run_reads_the_session_limit_and_takes_its_children(
    tmp_path: Path,
) -> None:
    """`test:python` runs these tests through a run: the inner group has no memory files."""
    child = tmp_path / "child"
    (tmp_path / "detach.py").write_text(_DETACHED)
    (tmp_path / "allocate.py").write_text(
        "held = []\nwhile True:\n    held.append(b'x' * (8 << 20))\n"
    )
    inner = (
        f"{sys.executable} {tmp_path / 'detach.py'} {child} & "
        f"while [ ! -s {child} ]; do sleep 0.05; done; "
        f"exec {sys.executable} {tmp_path / 'allocate.py'}"
    )
    run = SCRIPTS / "test-run"

    status, stderr = _in_scope(
        tmp_path, ["bash", str(run), "bash", str(run), "bash", "-c", inner], memory="96M"
    )

    line = (
        "stopped: the test run passed its memory limit of 96 MB. A loop or a leak is the likely "
        "cause, not the machine: find it before running again."
    )
    assert status == 137, stderr
    # The inner run says it, then the outer one.
    assert stderr.splitlines()[-2:] == [line, line]
    assert _gone(child)


@_REAL_SCOPES
def test_a_child_in_a_session_of_its_own_goes_with_a_stop_sent_to_the_run(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child"
    (tmp_path / "detach.py").write_text(_DETACHED)
    detach = [sys.executable, str(tmp_path / "detach.py"), str(child)]
    run = subprocess.Popen(
        [*_scope(), "bash", str(SCRIPTS / "test-run"), *detach], env=_session_env()
    )
    try:
        _wait_for(child)

        run.terminate()

        assert run.wait(timeout=30) != 0
        assert _gone(child)
    finally:
        run.kill()


@_REAL_SCOPES
def test_a_run_inside_a_run_killed_before_it_could_clean_up_leaves_no_group(
    tmp_path: Path,
) -> None:
    """The outer run kills the inner script after its grace time; the inner group goes too."""
    run = SCRIPTS / "test-run"
    (tmp_path / "inner.sh").write_text(
        f"WORKFLOW_TEST_RUN_GRACE_SECONDS=30 exec bash {run} bash -c \"trap '' TERM; sleep 600\"\n"
    )
    session = f"""
bash {run} bash {tmp_path / "inner.sh"}
status=$?
scope=/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)
find "$scope" -mindepth 1 -type d
exit $status
"""

    status, stderr = _in_scope(tmp_path, _script(tmp_path, session), WORKFLOW_TEST_RUN_SECONDS="2")

    assert status in (124, 137), stderr
    assert (tmp_path / "out").read_text() == ""


@_REAL_SCOPES
def test_the_groups_of_ended_runs_go_and_those_of_running_ones_stay(tmp_path: Path) -> None:
    """A run killed outright left its group; a run going on beside this one keeps its own."""
    session = f"""
scope=/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)
# No process has this number: the run that made it has ended.
mkdir -p "$scope/test-run-999999999/test-run-999999998" "$scope/test-run-$$"
echo "test-run-$$"
bash {SCRIPTS / "test-run"} true
find "$scope" -mindepth 1 -type d -printf '%f\\n'
rmdir "$scope/test-run-$$"
"""

    status, stderr = _in_scope(tmp_path, _script(tmp_path, session))

    assert status == 0, stderr
    running, *left = (tmp_path / "out").read_text().split()
    assert left == [running]


@_REAL_SCOPES
def test_the_command_runs_in_a_group_of_its_own_that_goes_when_it_ends(tmp_path: Path) -> None:
    """The run's group is made inside the session's scope, and nothing of it stays behind."""
    shown = (
        f"bash {SCRIPTS / 'test-run'} cat /proc/self/cgroup; "
        'scope=/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup); '
        'ls -d "$scope"/test-run-* 2>/dev/null; echo "left: $?"'
    )

    done = subprocess.run(
        [*_scope(), "bash", "-c", shown],
        env=_session_env(), capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip

    ran_in, left = done.stdout.splitlines()
    assert ran_in.rsplit("/", 2)[-2].endswith(".scope"), ran_in
    assert ran_in.rsplit("/", 1)[-1].startswith("test-run-"), ran_in
    assert left == "left: 2", done.stdout
