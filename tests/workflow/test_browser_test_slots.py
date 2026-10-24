"""Bounded, real-process tests of the local browser queue (no browser or server)."""

import fcntl
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/workflow/browser_test_slots.py"


def _wait(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), str(path)


@pytest.mark.parametrize("slots", [3, 1])
def test_waiter_runs_after_a_killed_run_releases_its_slot(tmp_path: Path, slots: int) -> None:
    processes: list[subprocess.Popen[str]] = []
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "WORKFLOW_BROWSER_SLOTS": str(slots),
    }
    if slots == 3:
        env.pop("WORKFLOW_BROWSER_SLOTS")  # Exercise the default as well as the override.
    try:
        for number in range(slots + 1):
            folder = tmp_path / str(number)
            folder.mkdir()
            started = folder / "started"
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(_SCRIPT),
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import time; "
                    "Path('started').touch(); time.sleep(30)",
                ],
                cwd=folder,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            processes.append(process)
            if number < slots:
                _wait(started)
        queue = tmp_path / "state/workflow/browser-tests"
        deadline = time.monotonic() + 5
        while not list(queue.glob("wait-*")) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert list(queue.glob("wait-*")), "fourth run never joined the queue"
        # A ticket is created before admission is checked. Wait for the flushed
        # announcement so freeing a slot cannot race that first check.
        assert processes[-1].stderr is not None
        assert select.select([processes[-1].stderr], [], [], 5)[0]
        assert processes[-1].stderr.readline().strip() == (
            "waiting for a browser test slot, 0 ahead"
        )
        assert not (tmp_path / str(slots) / "started").exists()
        # Kill the complete run, as when a session is stopped or crashes.
        os.killpg(processes[0].pid, signal.SIGKILL)
        processes[0].wait(timeout=5)
        _wait(tmp_path / str(slots) / "started")
        os.killpg(processes[-1].pid, signal.SIGTERM)
        processes[-1].wait(timeout=5)
        # Keep reading through the same wrapper: readline may have buffered more.
        error = processes[-1].stderr.read()
        assert "waiting for a browser test slot" not in error
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)


def test_fifo_skips_dead_waiters_and_retains_a_surviving_child_slot(tmp_path: Path) -> None:
    processes: list[subprocess.Popen[str]] = []
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "WORKFLOW_BROWSER_SLOTS": "1"}
    queue = tmp_path / "state/workflow/browser-tests"
    try:
        for number in range(4):
            folder = tmp_path / str(number)
            folder.mkdir()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(_SCRIPT),
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import os,time; "
                    "Path('started').write_text(str(os.getpid())); time.sleep(30)",
                ],
                cwd=folder,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            processes.append(process)
            if number == 0:
                _wait(folder / "started")
            else:
                deadline = time.monotonic() + 5
                while not list(queue.glob(f"wait-*-{process.pid}")) and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert list(queue.glob(f"wait-*-{process.pid}"))
                assert process.stderr is not None
                assert select.select([process.stderr], [], [], 5)[0]
                assert process.stderr.readline().strip() == (
                    f"waiting for a browser test slot, {number - 1} ahead"
                )
        # A dead waiter must not block the remaining FIFO queue.
        processes[1].kill()
        processes[1].wait(timeout=5)
        # The surviving browser child keeps the slot and activity lock after its parent dies.
        processes[0].kill()
        processes[0].wait(timeout=5)
        with (
            (tmp_path / "0/.local/workflow/browser-tests.lock").open("r") as lock,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The kernel lock proves the child still owns admission; no scheduling
        # delay is evidence that a waiter cannot start.
        with (queue / "slot-0.lock").open("r") as slot, pytest.raises(BlockingIOError):
            fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not (tmp_path / "2/started").exists()
        os.killpg(processes[0].pid, signal.SIGKILL)
        _wait(tmp_path / "2/started")
        assert not (tmp_path / "3/started").exists()
        os.killpg(processes[2].pid, signal.SIGKILL)
        _wait(tmp_path / "3/started")
    finally:
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
