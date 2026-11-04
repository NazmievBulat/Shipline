"""Independent PR 80 tests: cached/live usage and the reset lifecycle."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from workflow.break_80_support import NOW, answer, dated, unused
from workflow_harness import Workflow


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("weekly_unused", [False, True])
def test_idle_account_preserves_window_lengths_and_source(
    workflow: Workflow, source: str, weekly_unused: bool,
) -> None:
    payload = unused()
    if weekly_unused:
        payload["seven_day"] = {"utilization": 0.0, "resets_at": None}
    cache = answer(workflow, payload, source)
    original = cache.read_bytes() if cache.exists() else None

    done = workflow.run_script("usage")

    assert done.returncode == 0, done.stderr
    weekly = (
        f"seven_day 0 {NOW + 604800} not_started\n" if weekly_unused
        else f"seven_day 32 {NOW + 172800}\n"
    )
    assert done.stdout == f"five_hour 0 {NOW + 18000} not_started\n" + weekly
    assert (workflow.state / "usage-arguments").exists() == (source == "live")
    assert (cache.read_bytes() if cache.exists() else None) == original


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("window", [
    "absent", None, [], 0, False,
    {"utilization": None, "resets_at": None},
    {"utilization": "0.0", "resets_at": None},
    {"utilization": False, "resets_at": None},
    {"utilization": 0.0},
    {"utilization": 0.0, "resets_at": ""},
])
def test_partial_response_never_invents_a_five_hour_window(
    workflow: Workflow, source: str, window: object,
) -> None:
    payload = unused()
    if window == "absent":
        payload.pop("five_hour")
    else:
        payload["five_hour"] = window
    answer(workflow, payload, source)

    done = workflow.run_script("usage")

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"seven_day 32 {NOW + 172800}\n"


@pytest.mark.parametrize("source", ["cache", "live"])
def test_reset_then_first_activity_then_second_reset(workflow: Workflow, source: str) -> None:
    payload = unused()
    seen = []
    states = [
        {"utilization": 100.0, "resets_at": dated(NOW - 60)},
        {"utilization": 0.0, "resets_at": None},
        {"utilization": 0.0, "resets_at": dated(NOW + 17900)},
        {"utilization": 3.7, "resets_at": dated(NOW + 17900)},
        {"utilization": 0.0, "resets_at": None},
    ]
    for state in states:
        payload["five_hour"] = state
        answer(workflow, payload, source)
        done = workflow.run_script("usage")
        assert done.returncode == 0, done.stderr
        seen.append(done.stdout.splitlines()[0])
    assert seen == [
        f"five_hour 100 {NOW - 60}",
        f"five_hour 0 {NOW + 18000} not_started",
        f"five_hour 0 {NOW + 17900}",
        f"five_hour 3 {NOW + 17900}",
        f"five_hour 0 {NOW + 18000} not_started",
    ]


@pytest.mark.parametrize("old_unused", [False, True])
def test_expired_cache_uses_current_live_state(workflow: Workflow, old_unused: bool) -> None:
    old = unused()
    full = {**unused(), "five_hour": {"utilization": 96.0, "resets_at": dated(NOW + 7200)}}
    answer(workflow, full if old_unused else unused(), "live")
    cache = workflow.state / "usage-cache.json"
    cache.write_text(json.dumps(old if old_unused else full))
    os.utime(cache, (NOW - 301, NOW - 301))

    done = workflow.run_script("usage")

    assert done.returncode == 0, done.stderr
    expected = f"five_hour 96 {NOW + 7200}" if old_unused else f"five_hour 0 {NOW + 18000} not_started"
    assert done.stdout.splitlines()[0] == expected
    assert (workflow.state / "usage-arguments").exists()


def test_concurrent_reads_are_identical_and_do_not_write_cache(workflow: Workflow) -> None:
    cache = answer(workflow, unused())
    original = cache.read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: workflow.run_script("usage"), range(2)))
    assert [done.returncode for done in results] == [0, 0]
    assert results[0].stdout == results[1].stdout
    assert "not_started" in results[0].stdout
    assert cache.read_bytes() == original


@pytest.mark.parametrize("source", ["cache", "live"])
def test_invalid_payload_does_not_execute_shell_or_leak_credentials(
    workflow: Workflow, source: str,
) -> None:
    payload = unused()
    payload["five_hour"] = {
        "utilization": "$(touch SHOULD_NOT_EXIST)", "resets_at": None,
    }
    payload["seven_day"] = {
        "utilization": 12, "resets_at": "$(touch SHOULD_NOT_EXIST)",
    }
    answer(workflow, payload, source)
    done = workflow.run_script("usage")
    assert done.returncode == 1
    assert done.stdout == ""
    assert "example-test-credential" not in done.stdout + done.stderr
    assert not (workflow.repo / "SHOULD_NOT_EXIST").exists()
    if source == "live":
        args = (workflow.state / "usage-arguments").read_text()
        assert "example-test-credential" not in args
        assert "--max-time\n4\n" in args
