"""A stopped workflow command takes every process it started with it (real processes, no server)."""

import fcntl
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest
from workflow_harness import SCRIPTS

# The command `test-run` runs: a test in `timeout`'s process group, and a child that starts a
# session of its own, as Playwright's browsers do. Both hold the output pipes of the gate.
_TESTS = (
    "import subprocess, sys, time\n"
    "detached = subprocess.Popen(['sleep', '600'], start_new_session=True)\n"
    "open(sys.argv[2], 'w').write(str(detached.pid))\n"
    "open(sys.argv[1], 'w').write(str(subprocess.os.getpid()))\n"
    "time.sleep(600)\n"
)


def _ended(pid: int) -> bool:
    """Whether the process has ended. It is reaped by whoever adopted it, a moment later."""
    status = Path(f"/proc/{pid}/status")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if "\nState:\tZ" in status.read_text():
                return True
        except FileNotFoundError:
            return True
        time.sleep(0.05)
    return False


def _pid(path: Path) -> int:
    deadline = time.monotonic() + 20
    while not path.exists() or not path.read_text().strip():
        assert time.monotonic() < deadline, f"{path.name} never started"
        time.sleep(0.05)
    return int(path.read_text())


@pytest.mark.parametrize("stop", ["signal", "time limit"])
def test_a_stopped_push_takes_the_tests_its_hook_started_with_it(
    tmp_path: Path, programs_folder: Path, stop: str
) -> None:
    """A gate stopped during its push ends, with the tests of the hook and the push slot gone."""
    limit = "8" if stop == "time limit" else "None"
    test, detached, orphan = tmp_path / "test", tmp_path / "detached", tmp_path / "orphan"
    tests = tmp_path / "tests.py"
    tests.write_text(_TESTS)
    # git runs the hook, which runs the tests through `test-run`, and waits for it. A child of
    # the hook in a session of its own loses its parent to the first kill, and with it its place
    # in the run's tree.
    git = programs_folder / "git"
    git.write_text(
        f'#!/bin/bash\nsetsid sleep 600 & echo $! > "{orphan}"\n'
        f'bash "{SCRIPTS / "test-run"}" "{sys.executable}" "{tests}" "{test}" "{detached}"\n'
    )
    git.chmod(0o755)
    gate = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import workflow_commands as w; w.install_signal_handlers(); "
            f"w.run('git', 'push', timeout={limit})",
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(SCRIPTS),
            "PATH": f"{programs_folder}:{os.environ['PATH']}",
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "WORKFLOW_SESSION": "ticket-7",
            # No control group file system: the run has only its process groups, as when
            # `test-run` cannot make a group of its own.
            "WORKFLOW_CGROUP_ROOT": str(tmp_path / "cgroup"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pids = []
    try:
        pids = [_pid(test), _pid(detached), _pid(orphan)]

        if stop == "signal":
            gate.send_signal(signal.SIGTERM)

        error = gate.communicate(timeout=20)[1]
        if stop == "signal":
            assert gate.returncode == 128 + signal.SIGTERM
        else:
            assert "CommandTimeout: git push timed out after 8s" in error
        assert [pid for pid in pids if not _ended(pid)] == []
        slots = sorted((tmp_path / "state/workflow/pushes").glob("slot-*.lock"))
        assert slots
        for path in slots:
            with path.open("a+") as slot:
                fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for pid in pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        with suppress(ProcessLookupError):
            os.killpg(gate.pid, signal.SIGKILL)
        gate.communicate()


def test_a_number_known_from_the_run_but_now_used_by_another_process_is_left_alone() -> None:
    """A process of the run that ended may leave its number to an unrelated one."""
    other = subprocess.Popen(["sleep", "600"])
    try:
        # No process has the leader's number as its session: only what is known is of the run.
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import workflow_commands as w\n"
                "pid = int(sys.argv[2]); start = w._processes()[pid][3]\n"
                "print(sorted(w._run_processes(2**30, {(pid, start + 1)})) == [])\n"
                "print(sorted(w._run_processes(2**30, {(pid, start)})) == [(pid, start)])\n",
                str(SCRIPTS),
                str(other.pid),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        other.kill()
        other.wait()

    assert done.stdout.split() == ["True", "True"], done.stdout + done.stderr


def test_a_timed_out_command_carries_the_output_read_before_its_stop() -> None:
    """What a command printed before its deadline is kept; its message stays the same."""
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import workflow_commands as w\n"
            "try:\n"
            "    w.run('sh', '-c', 'echo first; echo second >&2; exec sleep 600', timeout=1)\n"
            "except w.CommandTimeout as error:\n"
            "    print(error)\n"
            "    print(error.output, end='')\n",
            str(SCRIPTS),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.stdout.splitlines() == [
        "sh -c echo first; echo second >&2; exec sleep 600 timed out after 1s",
        "first",
        "second",
    ], done.stdout + done.stderr
