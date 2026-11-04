"""Independent break tests for PR 82; run this file on its own.

The resource check is the real session command, with neutral local resource files.
Only external ticket listing and label delivery are replaced in picker tests.
"""

import fcntl
import importlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "workflow"
START = 1_800_000_000


class EndWatch(Exception):
    pass


@pytest.fixture
def installation(tmp_path, monkeypatch):
    local = tmp_path / "example-app"
    local.mkdir()
    config = local / "shipline.json"
    config.write_text(json.dumps({"repository": "example-org/example-app"}))
    programs = local / "bin"
    programs.mkdir()
    nproc = programs / "nproc"
    nproc.write_text("#!/bin/bash\necho 4\n")
    nproc.chmod(0o700)
    notes = local / ".local" / "workflow"
    notes.mkdir(parents=True)
    load = local / "loadavg"
    pressure = local / "pressure"
    swaps = local / "swaps"
    load.write_text("0.50 0.40 0.30 1/100 1234\n")
    pressure.write_text("some avg10=0.00 avg60=0.05 avg300=0.04 total=0\n")
    swaps.write_text("Filename Type Size Used Priority\n")
    # Keep configuration, usage and any logs within the test's directory.
    for key, value in {
        "HOME": str(local),
        "XDG_CONFIG_HOME": str(local / "config"),
        "SHIPLINE_CONFIG": str(config),
        "SHIPLINE_REPO": str(ROOT),
        "WORKFLOW_PRESSURE_FILE": str(pressure),
        "WORKFLOW_SWAPS_FILE": str(swaps),
        "WORKFLOW_LOADAVG_FILE": str(load),
        "WORKFLOW_NOW": str(START),
        "PATH": str(programs) + os.pathsep + os.environ["PATH"],
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module("ticket_starts")
    return SimpleNamespace(module=module, root=local, notes=notes, load=load,
                           pressure=pressure, nproc=nproc, monkeypatch=monkeypatch)


def at(installation, seconds):
    installation.monkeypatch.setenv("WORKFLOW_NOW", str(START + seconds))


def watch_until(installation, seconds, excursions=()):
    """Advance only the sampler clock; subprocess timeouts retain their real clock."""
    step = 0
    ordinary = installation.load.read_text(), installation.pressure.read_text()

    def advance(interval):
        nonlocal step
        assert interval == 180
        step += interval
        if step > seconds:
            raise EndWatch
        at(installation, step)
        installation.load.write_text(
            "8.00 8.00 8.00 1/100 1234\n" if (step, "load") in excursions else ordinary[0]
        )
        installation.pressure.write_text(
            "some avg10=50.00 avg60=50.00 avg300=50.00 total=0\n"
            if (step, "memory") in excursions else ordinary[1]
        )

    clock = SimpleNamespace(time=time.time, sleep=advance)
    installation.monkeypatch.setattr(installation.module, "time", clock)
    with pytest.raises(EndWatch):
        installation.module.watch_room(installation.notes, os.getppid())


def pick(installation):
    """Use the real picker and saved file evidence, without remote reads or a model call."""
    module = installation.module
    snapshot = installation.root / "snapshot.json"
    snapshot.write_text(json.dumps({"7": {
        "number": 7, "state": "OPEN", "isDraft": True, "baseRefName": "main",
        "headRefOid": "a" * 40, "headRefName": "ticket/example", "labels": [],
        "body": "## Files\nweb/example.py\n",
    }}))
    (installation.notes / "files-7").write_text("web/example.py\n")
    (installation.notes / "files-7.source").write_text("diff " + "a" * 40 + "\n")
    installation.monkeypatch.setattr(module, "running_numbers", lambda: set())
    labels = []
    installation.monkeypatch.setattr(module, "add_label", lambda n: labels.append(n) or True)
    assert module.pick(installation.root, snapshot) == 0
    return labels


@pytest.mark.parametrize("excursion", [None, "load", "memory"])
def test_twelve_minute_tick_and_resource_excursions(installation, excursion):
    watch_until(installation, 720, [(360, excursion)] if excursion else [])
    at(installation, 720)
    assert pick(installation) == ([] if excursion else ["7"])
    assert tuple(map(int, (installation.notes / "room").read_text().split())) == (
        START + (540 if excursion else 0), START + 720
    )


def test_stopped_timer_or_sleep_resets_evidence(installation):
    installation.module.observe_room(installation.notes)
    at(installation, 780)
    assert pick(installation) == []
    assert (installation.notes / "room").read_text().split() == [str(START + 780)] * 2


@pytest.mark.parametrize("record", [None, "", "1800000000", "partial write\n",
                                    "1800000500 1800000600\n", "1800000000 1799999999\n"])
def test_missing_partial_and_backward_records_reset(installation, record):
    if record is not None:
        (installation.notes / "room").write_text(record)
    assert pick(installation) == []
    assert (installation.notes / "room").read_text().split() == [str(START)] * 2


def test_quiet_period_and_usage_window_still_hold(installation):
    watch_until(installation, 720)
    at(installation, 780)
    (installation.notes / "last-start").write_text(str(START + 600))
    assert pick(installation) == []
    (installation.notes / "last-start").unlink()
    cache = installation.root / "usage-cache.json"
    cache.write_text(json.dumps({"five_hour": {
        "utilization": 90, "resets_at": "2027-01-16T10:00:00Z",
    }}))
    os.utime(cache, (START + 780, START + 780))
    installation.monkeypatch.setenv("WORKFLOW_USAGE_CACHE", str(cache))
    # The reset is over an hour after the movable clock.
    assert pick(installation) == []


def test_room_and_lock_are_private_and_replacement_is_atomic(installation):
    module = installation.module
    module.observe_room(installation.notes)
    record = installation.notes / "room"
    inode = record.stat().st_ino
    at(installation, 180)
    module.observe_room(installation.notes)
    assert record.stat().st_ino != inode
    assert record.stat().st_mode & 0o777 == 0o600
    assert (installation.notes / "room.lock").stat().st_mode & 0o777 == 0o600
    assert not list(installation.notes.glob(".room-*"))


def test_lock_symlink_is_refused_without_changing_target(installation):
    target = installation.root / "unrelated"
    target.write_text("unchanged\n")
    (installation.notes / "room.lock").symlink_to(target)
    with pytest.raises(OSError):
        installation.module.observe_room(installation.notes)
    assert target.read_text() == "unchanged\n"


def test_overlapping_checks_cannot_restore_pre_excursion_history(installation):
    """A healthy check waits behind an excursion already being checked."""
    ctx = multiprocessing.get_context("fork")
    entered, release, completed = ctx.Event(), ctx.Event(), ctx.Event()
    observed = ctx.Queue()
    installation.module.observe_room(installation.notes)
    high_load = installation.root / "high-load"
    high_load.write_text("8.00 8.00 8.00 1/100 1234\n")

    def first():
        os.environ["WORKFLOW_LOADAVG_FILE"] = str(high_load)
        os.environ["WORKFLOW_NOW"] = str(START + 180)
        real_run = installation.module.subprocess.run

        def held(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return real_run(*args, **kwargs)

        installation.module.subprocess = SimpleNamespace(
            run=held, SubprocessError=subprocess.SubprocessError
        )
        observed.put(installation.module.observe_room(installation.notes))

    def second():
        os.environ["WORKFLOW_NOW"] = str(START + 180)
        observed.put(installation.module.observe_room(installation.notes))
        completed.set()

    one, two = ctx.Process(target=first), ctx.Process(target=second)
    one.start()
    try:
        assert entered.wait(5)
        two.start()
        assert not completed.wait(0.15)
        release.set()
        one.join(5)
        two.join(5)
        assert one.exitcode == two.exitcode == 0
        results = [observed.get(timeout=1), observed.get(timeout=1)]
        assert None in results
        assert (START + 180, START + 180) in results
    finally:
        release.set()
        for child in (one, two):
            if child.pid is not None and child.is_alive():
                child.kill()
                child.join(5)
        observed.close()


def test_killed_lock_holder_does_not_block_next_observation(installation):
    ctx = multiprocessing.get_context("fork")
    held = ctx.Event()

    def hold():
        with (installation.notes / "room.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            held.set()
            signal.pause()

    child = ctx.Process(target=hold)
    child.start()
    try:
        assert held.wait(5)
        child.kill()
        child.join(5)
        assert installation.module.observe_room(installation.notes) == (START, START)
    finally:
        if child.is_alive():
            child.kill()
            child.join(5)


def test_sampler_stops_when_parent_is_gone(installation):
    installation.module.watch_room(installation.notes, os.getppid() + 1)
    assert not (installation.notes / "room").exists()


def test_excursion_during_file_discovery_still_blocks_final_start(installation):
    watch_until(installation, 720)
    at(installation, 780)
    original = installation.module.Files.of

    def discover(files, pull, fetched):
        result = original(files, pull, fetched)
        at(installation, 960)
        usual = installation.load.read_text()
        installation.load.write_text("8.00 8.00 8.00 1/100 1234\n")
        assert installation.module.observe_room(installation.notes) is None
        installation.load.write_text(usual)
        at(installation, 1140)
        installation.module.observe_room(installation.notes)
        return result

    installation.monkeypatch.setattr(installation.module.Files, "of", discover)
    assert pick(installation) == []


def _tick_environment(installation):
    """Isolate unrelated tick duties; execute its real sampler lifecycle and room check."""
    subprocess.run(["git", "init", "--quiet", str(installation.root)],
                   check=True, timeout=5)
    programs = installation.nproc.parent
    (programs / "python3").write_text('''#!/bin/bash
for argument in "$@"; do
  [[ $argument != */self_update.py ]] || exit 11
done
if [[ " $* " == *" watch-room "* ]]; then
  echo "$$" > "$BREAK_SAMPLER_PID"
  # Keep the implementation's loop and real checks, shortening only the interval.
  exec "$BREAK_PYTHON" -B -c '
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import ticket_starts
ticket_starts.LOOK_SECONDS = 0.03
ticket_starts.watch_room(pathlib.Path(sys.argv[2]), int(sys.argv[3]))
' "$BREAK_SCRIPTS" "$BREAK_NOTES" "${@: -1}"
fi
exec "$BREAK_PYTHON" "$@"
''')
    (programs / "bash").write_text('''#!/bin/bash
if [[ $1 == */session && $2 != room || $1 == */workflow-metrics ]]; then exit 0; fi
exec /bin/bash "$@"
''')
    (programs / "gh").write_text('''#!/bin/bash
touch "$BREAK_GITHUB_ENTERED"
if [[ $BREAK_SHADOW == 0 ]]; then
  for attempt in {1..300}; do
    if [[ -e $BREAK_RELEASE ]]; then exit 1; fi
    /bin/sleep 0.01
  done
fi
exit 1
''')
    installation.nproc.write_text('''#!/bin/bash
echo sample >> "$BREAK_SAMPLES"
echo 4
''')
    for program in ("python3", "bash", "gh"):
        (programs / program).chmod(0o700)
    scratch = installation.root / "scratch"
    scratch.mkdir()
    return {
        **os.environ,
        "SHIPLINE_REPO": str(installation.root),
        "WORKFLOW_METRICS_DAILY_AT": "", "WORKFLOW_ANALYST_DAILY_AT": "",
        "TMPDIR": str(scratch), "BREAK_PYTHON": sys.executable,
        "BREAK_SCRIPTS": str(SCRIPTS), "BREAK_NOTES": str(installation.notes),
        "BREAK_SAMPLER_PID": str(installation.root / "sampler-pid"),
        "BREAK_SAMPLES": str(installation.root / "samples"),
        "BREAK_GITHUB_ENTERED": str(installation.root / "github-entered"),
        "BREAK_RELEASE": str(installation.root / "release"), "BREAK_SHADOW": "0",
    }


def _eventually(condition):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    pytest.fail("tick lifecycle condition was not reached within five seconds")


@pytest.mark.parametrize("shadow", [False, True])
def test_tick_sampler_lifecycle_through_blocked_github_read(installation, shadow):
    environment = _tick_environment(installation)
    environment["BREAK_SHADOW"] = str(int(shadow))
    command = ["/bin/bash", str(SCRIPTS / "tick"), *(["--shadow"] if shadow else [])]
    child = subprocess.Popen(command, env=environment, cwd=installation.root,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    try:
        if not shadow:
            samples = installation.root / "samples"
            _eventually(lambda: samples.exists() and len(samples.read_text().splitlines()) >= 3)
            _eventually(lambda: (installation.root / "github-entered").exists())
            (installation.root / "release").touch()
        _, error = child.communicate(timeout=8)
        assert child.returncode == 1, error
        if shadow:
            assert not (installation.root / "sampler-pid").exists()
            assert not (installation.notes / "room").exists()
        else:
            sampler = int((installation.root / "sampler-pid").read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(sampler, 0)
            with (installation.root / ".git" / "shipline-tick.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)


def test_second_tick_skips_and_interruption_releases_sampler_and_tick_lock(installation):
    environment = _tick_environment(installation)
    command = ["/bin/bash", str(SCRIPTS / "tick")]
    child = subprocess.Popen(command, env=environment, cwd=installation.root,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    try:
        _eventually(lambda: (installation.root / "github-entered").exists())
        sampler = int((installation.root / "sampler-pid").read_text())
        second = subprocess.run(command, env=environment, cwd=installation.root,
                                capture_output=True, text=True, timeout=5)
        assert second.returncode == 0, second.stderr
        assert "the tick before this one is still running" in second.stdout
        assert int((installation.root / "sampler-pid").read_text()) == sampler
        os.killpg(child.pid, signal.SIGTERM)
        child.communicate(timeout=5)
        assert child.returncode == 143
        with pytest.raises(ProcessLookupError):
            os.kill(sampler, 0)
        with (installation.root / ".git" / "shipline-tick.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)


def test_transient_resource_timeout_recovers_during_the_same_long_tick(installation):
    """One slow local check must not discard the rest of a healthy 15-minute tick."""
    marker = installation.root / "slow-check-done"
    installation.nproc.write_text(
        '#!/bin/bash\nif [[ ! -e "$BREAK_SLOW_MARKER" ]]; then\n'
        '  touch "$BREAK_SLOW_MARKER"\n  /bin/sleep 0.2\nfi\necho 4\n'
    )
    installation.monkeypatch.setenv("BREAK_SLOW_MARKER", str(marker))
    installation.monkeypatch.setattr(installation.module, "GIT_SECONDS", 0.05)
    stopped = False
    try:
        watch_until(installation, 900)
    except subprocess.TimeoutExpired:
        stopped = True
    # The transient has ended. Even allowing the production timeout of 60 seconds
    # before the next three-minute sleep, +240 through +900 is over ten minutes.
    installation.monkeypatch.setattr(installation.module, "GIT_SECONDS", 60)
    at(installation, 900)
    labelled = pick(installation)
    assert labelled == ["7"], (
        f"sampler stopped after transient timeout: {stopped}; "
        f"labelled={labelled}; room={tuple(map(int, (installation.notes / 'room').read_text().split()))}"
    )
