"""Breaking tests for the self-update of pull request 5: they replay how an installation uses the
self-update every day (one checkout, a tick every 3 minutes, sessions calling the gate from the
checkout) and the failures it meets. Each test states what that use needs; a failing test is a
weak spot."""

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest
from test_self_update import _MOVED, _TRACEBACK, Shipline, _gate


@pytest.fixture
def shipline(tmp_path: Path) -> Shipline:
    return Shipline(tmp_path)


def _sleeper() -> subprocess.Popen[bytes]:
    """A live process that stands for a running tick or gate call."""
    return subprocess.Popen(["sleep", "300"])


def _tick_with(shipline: Shipline, code: int = 0, gate: list[tuple[int, str]] | None = None) -> int:
    """One tick: its start, the gate runs its merge driver made, its end. 3 minutes pass."""
    pid = str(os.getpid())
    started = shipline.run("start", pid)
    assert started.returncode in (0, _MOVED), started.stderr
    for gate_code, stderr in gate or []:
        _gate(shipline, gate_code, stderr)
    ended = shipline.run("end", pid, str(code))
    assert ended.returncode == 0, ended.stderr
    shipline.later(3)
    return started.returncode


# 1. The daily flow: a tick every 3 minutes; main lands, its CI runs a while, then passes.
def test_daily_flow_main_lands_ci_turns_green_and_the_checkout_follows(shipline: Shipline) -> None:
    shipline.good()
    landed = shipline.land(ci="in_progress -")
    shipline.later(10)
    moved_at = None
    for minute in range(0, 60, 3):
        if minute == 6:
            shipline.ci(landed, "completed success")
        # The merge line works as usual: waits (exit 3) and refusals are answers.
        if _tick_with(
            shipline,
            gate=[(3, "merge waiting: main moved\n"), (1, "merge refused: check is not passing: CI\n")],
        ) == _MOVED:
            moved_at = minute
            break
    assert moved_at is not None and moved_at <= 15
    assert shipline.head == landed
    for _ in range(5):
        _tick_with(shipline, gate=[(3, "merge waiting: main moved\n")])
    assert shipline.versions()[0] == landed


# 2. A gate crash is a failure; it must not leave the version on its way to "good".
def test_a_gate_crash_sets_the_good_count_back_to_0(shipline: Shipline) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    assert _tick_with(shipline) == _MOVED
    _tick_with(shipline)
    _tick_with(shipline)
    # The only pull request in the line: the gate crashes; the merge driver hands it back.
    _tick_with(shipline, gate=[(1, _TRACEBACK)])
    # The rest of the night, nothing is in the line.
    _tick_with(shipline)
    assert bad not in shipline.versions(), "a version that crashed the gate 1 tick ago is good"


def test_a_version_that_crashed_the_gate_at_night_still_rolls_back_in_the_morning(
    shipline: Shipline,
) -> None:
    shipline.good()
    bad = shipline.land()
    shipline.later(10)
    assert _tick_with(shipline) == _MOVED
    _tick_with(shipline, gate=[(1, _TRACEBACK)])
    for _ in range(10):
        _tick_with(shipline)
    # Morning: each pull request that reaches the gate crashes it, and is handed back.
    for _ in range(3):
        _tick_with(shipline, gate=[(1, _TRACEBACK)])
    _tick_with(shipline)
    assert shipline.head == shipline.first, "the version that crashes every gate run stays"


# 3. Two calls at once: a tick run by hand while the timer's tick fetches.
def test_no_move_under_a_tick_that_started_while_another_fetched(shipline: Shipline) -> None:
    shipline.good()
    landed = shipline.land()
    shipline.later(10)
    gate_file = shipline.root / "fetch-release"
    entered = shipline.root / "fetch-entered"
    wrapper = shipline.root / "bin" / "git"
    wrapper.write_text(
        "#!/bin/bash\n"
        f'if [ "$3" = fetch ]; then touch {entered}; '
        f"while [ ! -e {gate_file} ]; do sleep 0.05; done; fi\n"
        'exec /usr/bin/git "$@"\n'
    )
    wrapper.chmod(0o755)
    first, second = _sleeper(), _sleeper()
    try:
        fetching = subprocess.Popen(
            ["python3", "-B", str(shipline.home / "scripts/workflow/self_update.py"), "start",
             str(first.pid)],
            cwd=shipline.repo, env=shipline.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 30
        while not entered.exists():
            assert time.time() < deadline
            time.sleep(0.05)
        # The second tick starts while the first fetches, and goes on to run from the checkout.
        started = shipline.run("start", str(second.pid))
        assert started.returncode == 0, started.stderr
        assert shipline.head == shipline.first
        gate_file.touch()
        fetching.communicate(timeout=60)
        assert second.poll() is None
        assert shipline.head == shipline.first, (
            f"moved to {landed[:12]} under the second tick, which still runs"
        )
    finally:
        gate_file.touch()
        first.kill()
        second.kill()


# 4. A session's gate call runs from the checkout and holds the repository's merge lock.
def test_no_move_under_a_running_gate_call_of_a_session(shipline: Shipline) -> None:
    shipline.good()
    shipline.land()
    shipline.later(10)
    lock_path = shipline.repo / ".local" / "workflow" / "merge.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("w") as lock:
        # As `merge-when-current <N>` of a reviewer session holds it through its checks.
        fcntl.flock(lock, fcntl.LOCK_EX)
        _tick_with(shipline)
        assert shipline.head == shipline.first, "moved under a running gate call"


# 5. The internet goes away for ten minutes right after an update; the tick exits 1 each time
#    ("cannot list the pull requests"), which is neither a crash nor a traceback.
def test_a_short_outage_after_an_update_does_not_roll_a_working_version_back(
    shipline: Shipline,
) -> None:
    shipline.good()
    landed = shipline.land()
    shipline.later(10)
    assert _tick_with(shipline) == _MOVED
    for _ in range(3):
        _tick_with(shipline, code=1)
    _tick_with(shipline)
    assert shipline.head == landed, "a working version was rolled back for an outage"


# 6. A lock held too long: a tick still running after an hour (a long merge train).
def test_no_move_under_a_tick_running_longer_than_an_hour(shipline: Shipline) -> None:
    shipline.good()
    long_tick = _sleeper()
    try:
        assert shipline.run("start", str(long_tick.pid)).returncode == 0
        shipline.land()
        shipline.later(61)
        os.utime(shipline.notes / "runs" / str(long_tick.pid), (shipline.now - 61 * 60,) * 2)
        _tick_with(shipline)
        assert long_tick.poll() is None
        assert shipline.head == shipline.first, "moved under a tick that still runs"
    finally:
        long_tick.kill()


# 7. A pin in the file holds, and a missing or broken note does not stop the merge line.
def test_a_pin_file_holds_updates_and_roll_backs(shipline: Shipline) -> None:
    shipline.good()
    shipline.land()
    shipline.notes.mkdir(parents=True, exist_ok=True)
    (shipline.notes / "pin").write_text(shipline.first + "\n")
    shipline.later(10)
    for _ in range(4):
        assert _tick_with(shipline, code=1) == 0
    assert shipline.head == shipline.first


def test_broken_notes_do_not_fail_the_tick(shipline: Shipline) -> None:
    shipline.good()
    (shipline.notes / "trial.json").write_text("{not json")
    (shipline.notes / "versions").write_text("garbage\n")
    (shipline.notes / "fetched").write_text("x\n")
    (shipline.notes / "runs").mkdir(exist_ok=True)
    (shipline.notes / "runs" / "123").write_text("")
    landed = shipline.land()
    shipline.later(10)
    assert _tick_with(shipline) == _MOVED
    assert shipline.head == landed
