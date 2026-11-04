"""Independent PR 80 tests: outages, held locks, overlapping ticks and replayed events."""

import fcntl
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from workflow.break_80_support import NOW, answer, candidate, labelled, notes, tick, unused
from workflow_harness import Workflow


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("kind", ["missing", "non-numeric"])
def test_unknown_five_hour_usage_is_named_and_holds_wake_starts(
    workflow: Workflow, source: str, kind: str,
) -> None:
    workflow.orchestrator("idle")
    payload = unused()
    if kind == "missing":
        payload.pop("five_hour")
    else:
        payload["five_hour"] = {"utilization": "0", "resets_at": None}
    answer(workflow, payload, source)

    done = workflow.run_script("wake")

    assert done.returncode == 0, done.stderr
    [prompt] = workflow.sent()
    assert "5-hour window unknown, weekly 32%" in prompt
    assert "not started" not in prompt
    assert "starts: unknown" in workflow.wake_log.read_text()


@pytest.mark.parametrize("missing", ["cache", "credentials", "both"])
def test_unreadable_usage_holds_selection_and_recovers_without_manual_state_edits(
    workflow: Workflow, missing: str,
) -> None:
    candidate(workflow)
    cache = answer(workflow, unused(), "live")
    credentials = workflow.root / "home/.claude/.credentials.json"
    if missing == "cache":
        cache.write_text('{"seven_day":{"utilization":32,"resets_at":null}}')
        os.utime(cache, (NOW, NOW))
    elif missing == "credentials":
        credentials.unlink()
    else:
        credentials.unlink()
        cache.unlink(missing_ok=True)

    held = tick(workflow)
    assert held.returncode == 0, held.stdout + held.stderr
    assert labelled(workflow) == []
    assert "5-hour usage window could not be read" in held.stdout

    answer(workflow, unused(), "live")
    resumed = tick(workflow, NOW + 180)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert labelled(workflow) == ["7"]


@pytest.mark.parametrize("payload", ["{", "[]", "null", '{"error":{"type":"unavailable"}}'])
def test_corrupt_cache_falls_back_to_live_unused_window(workflow: Workflow, payload: str) -> None:
    candidate(workflow)
    cache = answer(workflow, unused(), "live")
    cache.write_text(payload)
    os.utime(cache, (NOW, NOW))
    done = tick(workflow)
    assert done.returncode == 0, done.stdout + done.stderr
    assert labelled(workflow) == ["7"]
    assert (workflow.state / "usage-arguments").exists()


def test_endpoint_timeout_is_bounded_silent_and_recovers(workflow: Workflow) -> None:
    answer(workflow, unused(), "live")
    workflow.fake("curl", '''#!/bin/bash
seconds=missing
while (($#)); do
  if [[ $1 == --max-time ]]; then seconds=$2; shift; fi
  shift
done
cat > /dev/null
[[ $seconds != missing ]] || exit 99
exec timeout "$seconds" /bin/sleep 10
''')
    started = time.monotonic()
    done = workflow.run_script("usage", env={"WORKFLOW_USAGE_SECONDS": "0.15"})
    assert time.monotonic() - started < 3
    assert done.returncode == 1
    assert done.stdout == ""
    assert "example-test-credential" not in done.stderr

    answer(workflow, unused(), "live")
    recovered = workflow.run_script("usage")
    assert recovered.returncode == 0, recovered.stderr
    assert "five_hour 0" in recovered.stdout and "not_started" in recovered.stdout


def test_long_held_tick_lock_skips_calls_then_recovers(workflow: Workflow) -> None:
    candidate(workflow)
    answer(workflow, unused())
    lock = workflow.repo / ".git/shipline-tick.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        os.utime(lock, (NOW - 7200, NOW - 7200))
        for moment in (NOW, NOW + 180):
            started = time.monotonic()
            skipped = tick(workflow, moment)
            assert time.monotonic() - started < 5
            assert skipped.returncode == 0, skipped.stderr
            assert labelled(workflow) == []
        fcntl.flock(held, fcntl.LOCK_UN)
    # A fresh endpoint answer is independent of the lock's age.
    answer(workflow, unused(), "live")
    recovered = tick(workflow, NOW + 180)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert labelled(workflow) == ["7"]


@pytest.mark.parametrize("program", ["local", "cloud"])
def test_overlapping_ticks_start_only_once(workflow: Workflow, program: str) -> None:
    if program == "local":
        candidate(workflow)
    else:
        workflow.add_pull_request(7)
    answer(workflow, unused(), "live")
    extra = {"WORKFLOW_WORKER_PROGRAM": "cloud"} if program == "cloud" else {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tick(workflow, **extra), range(2)))
    assert [done.returncode for done in results] == [0, 0], [done.stderr for done in results]
    if program == "local":
        assert labelled(workflow) == ["7"]
    else:
        assert len(workflow.calls("tmux cloud-7 new-session")) == 1


def test_lost_check_delivery_survives_unused_window_and_partial_replay(workflow: Workflow) -> None:
    workflow.orchestrator("busy")
    answer(workflow, unused(), "live")
    folder = notes(workflow)
    pending = folder / "lost-checks.pending"
    pending.mkdir()
    event = pending / f"7-{'a' * 40}.json"
    event.write_text(json.dumps({
        "number": "7", "head": "a" * 40, "check": "CI result", "run": 123,
        "conclusion": "success",
    }))
    incomplete = pending / ".partial.json.tmp"
    incomplete.write_text("{")
    (workflow.state / "send-fails").touch()

    failed = workflow.run_script("wake")
    assert failed.returncode == 1
    assert event.exists()
    (workflow.state / "send-fails").unlink()
    sent = workflow.run_script("wake", env={"WORKFLOW_NOW": str(NOW + 300)})
    replayed = workflow.run_script("wake", env={"WORKFLOW_NOW": str(NOW + 600)})

    assert sent.returncode == replayed.returncode == 0, sent.stderr + replayed.stderr
    assert len(workflow.sent()) == 2  # failed attempt, then one successful delivery
    assert "lost check: CI result of run 123 on pull request 7" in workflow.sent()[-1]
    assert "5-hour window 0% used, not started" in workflow.sent()[-1]
    assert not event.exists()
    assert (folder / "lost-checks.sent" / event.name).exists()
    assert incomplete.exists()
