"""Independent break tests for PR 91; run this file alone with requirements-test.txt.

External services and session launches use the shared stand-ins, never live accounts.
The assertions and scenarios here do not import the ticket's regression tests.
"""

import fcntl
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from workflow_harness import Workflow


def _reset(moment: int) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


def _answer(moment: int, five: object = None) -> dict[str, object]:
    return {
        "five_hour": five,
        "seven_day": {"utilization": 36.0, "resets_at": _reset(moment + 86400)},
    }


def _feed(workflow: Workflow, answer: object, source: str, moment: int) -> None:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    cache = workflow.state / "example-app-usage.json"
    workflow.env.update(WORKFLOW_USAGE_CACHE=str(cache), WORKFLOW_NOW=str(moment), TZ="UTC")
    if source == "cache":
        cache.write_text(text)
        cache.chmod(0o600)
        os.utime(cache, (moment, moment))
    else:
        credentials = workflow.state / "example-app-credentials.json"
        credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "example-value"}}))
        credentials.chmod(0o600)
        workflow.env["WORKFLOW_CREDENTIALS"] = str(credentials)
        (workflow.state / "example-app-answer").write_text(text)
        workflow.fake("curl", '#!/bin/bash\n'
                      'printf "%s\\n" "$@" > "@STATE@/example-app-curl-args"\n'
                      'cat >/dev/null\ncat "@STATE@/example-app-answer"\n')


def _usage(workflow: Workflow) -> subprocess.CompletedProcess[str]:
    return workflow.run_script("usage")


def _tick(workflow: Workflow, **env: str) -> str:
    done = workflow.run_script("tick", env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _notes(workflow: Workflow):
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True, exist_ok=True)
    return notes


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("nulls", [("five_hour",), ("seven_day",), ("five_hour", "seven_day")])
def test_nulls_preserve_the_other_window_and_do_not_write_the_input(
    workflow: Workflow, source: str, nulls: tuple[str, ...]
) -> None:
    moment = int(time.time())
    answer = _answer(moment, {"utilization": 42.5, "resets_at": _reset(moment + 7200)})
    answer.update(dict.fromkeys(nulls))
    _feed(workflow, answer, source, moment)
    path = workflow.state / ("example-app-usage.json" if source == "cache" else "example-app-answer")
    before = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode
    done = _usage(workflow)
    assert done.returncode == 0, done.stderr
    expected = []
    for name, length, percent, reset in [
        ("five_hour", 18000, 42, moment + 7200),
        ("seven_day", 604800, 36, moment + 86400),
    ]:
        expected.append(f"{name} 0 {moment + length} not_started\n" if name in nulls
                        else f"{name} {percent} {reset}\n")
    assert done.stdout == "".join(expected)
    assert done.stderr == ""
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == before


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("bad", [False, 0, [], "null", {}, {"utilization": "0", "resets_at": None}])
def test_bad_five_hour_is_not_zero_just_because_weekly_is_readable(
    workflow: Workflow, source: str, bad: object
) -> None:
    moment = int(time.time())
    _feed(workflow, _answer(moment, bad), source, moment)
    done = _usage(workflow)
    assert done.returncode == 0
    assert done.stdout == f"seven_day 36 {moment + 86400}\n"


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("text", [
    "", "null", "[]", '{"seven_day":null}',
    '{"five_hour":null,', '{"five_hour":null} trailing',
    '{"five_hour":null}\n{"seven_day":null}',
])
def test_missing_or_incomplete_readings_never_invent_a_five_hour_window(
    workflow: Workflow, source: str, text: str
) -> None:
    moment = int(time.time())
    _feed(workflow, text, source, moment)
    done = _usage(workflow)
    if text == '{"seven_day":null}':
        assert done.returncode == 0
        assert done.stdout == f"seven_day 0 {moment + 604800} not_started\n"
    else:
        assert done.returncode == 1
        assert done.stdout == ""
    assert done.stderr == ""


@pytest.mark.parametrize("age", [299, 301])
@pytest.mark.parametrize("cached_null", [False, True])
def test_expiring_cache_switches_between_null_and_high_usage(
    workflow: Workflow, age: int, cached_null: bool
) -> None:
    moment = int(time.time())
    high = {"utilization": 96, "resets_at": _reset(moment + 7200)}
    _feed(workflow, _answer(moment, None if cached_null else high), "cache", moment - age)
    _feed(workflow, _answer(moment, high if cached_null else None), "live", moment)
    done = _usage(workflow)
    assert done.returncode == 0
    expected_null = cached_null if age == 299 else not cached_null
    assert done.stdout.splitlines()[0] == (
        f"five_hour 0 {moment + 18000} not_started" if expected_null
        else f"five_hour 96 {moment + 7200}"
    )
    assert (workflow.state / "example-app-curl-args").exists() == (age == 301)


def test_a_partial_cache_write_recovers_from_the_service(workflow: Workflow) -> None:
    moment = int(time.time())
    _feed(workflow, '{"five_hour":null,', "cache", moment)
    _feed(workflow, _answer(moment), "live", moment)
    done = _usage(workflow)
    assert done.returncode == 0
    assert done.stdout.splitlines()[0] == f"five_hour 0 {moment + 18000} not_started"
    assert (workflow.state / "example-app-curl-args").exists()


def test_missing_cache_and_credentials_fail_closed(workflow: Workflow) -> None:
    workflow.env["WORKFLOW_USAGE_CACHE"] = str(workflow.state / "missing-cache")
    workflow.env["WORKFLOW_CREDENTIALS"] = str(workflow.state / "missing-credentials")
    done = _usage(workflow)
    assert done.returncode == 1
    assert done.stdout == done.stderr == ""
    assert workflow.calls("curl") == []


@pytest.mark.parametrize("source", ["cache", "live"])
def test_simultaneous_readers_return_complete_independent_lines(workflow: Workflow, source: str) -> None:
    moment = int(time.time())
    _feed(workflow, {"five_hour": None, "seven_day": None}, source, moment)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _usage(workflow), range(4)))
    for done in results:
        assert done.returncode == 0
        assert done.stdout == (f"five_hour 0 {moment + 18000} not_started\n"
                               f"seven_day 0 {moment + 604800} not_started\n")


@pytest.mark.parametrize("failure", [22, 28])
def test_service_errors_discard_even_a_null_body_and_keep_credentials_off_arguments(
    workflow: Workflow, failure: int
) -> None:
    _feed(workflow, _answer(int(time.time())), "live", int(time.time()))
    original = (workflow.bin / "curl").read_text()
    workflow.fake("curl", original + f"exit {failure}\n")
    workflow.env["WORKFLOW_USAGE_SECONDS"] = "1"
    done = _usage(workflow)
    assert done.returncode == 1
    assert done.stdout == done.stderr == ""
    args = (workflow.state / "example-app-curl-args").read_text().splitlines()
    assert args[args.index("--max-time") + 1] == "1"
    assert args[args.index("--config") + 1] == "-"
    assert "example-value" not in " ".join(args)


@pytest.mark.parametrize("source", ["cache", "live"])
def test_reset_text_cannot_run_a_shell_command(workflow: Workflow, source: str) -> None:
    moment = int(time.time())
    _feed(workflow, _answer(moment, {
        "utilization": 0, "resets_at": "$(touch example-app-injected)",
    }), source, moment)
    done = _usage(workflow)
    assert done.stdout == f"seven_day 36 {moment + 86400}\n"
    assert not (workflow.repo / "example-app-injected").exists()


@pytest.mark.parametrize("source", ["cache", "live"])
def test_both_python_start_readers_recheck_null_high_missing_and_recovered(
    workflow: Workflow, source: str
) -> None:
    moment = int(time.time())
    program = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "import ticket_starts, cloud_workers; "
        "print('ticket', ticket_starts.usage_wait(int(sys.argv[2])) is None); "
        "print('cloud', cloud_workers._allowed(Path(sys.argv[1])))"
    )
    for five, allowed in [
        (None, True),
        ({"utilization": 96, "resets_at": _reset(moment + 7200)}, False),
        ({"utilization": 96, "resets_at": _reset(moment + 3600)}, True),
        ({"utilization": 85, "resets_at": _reset(moment + 7200)}, True),
        ("missing", False), (None, True),
    ]:
        answer = _answer(moment, five)
        if five == "missing":
            answer.pop("five_hour")
        _feed(workflow, answer, source, moment)
        done = subprocess.run(
            ["python3", "-B", "-c", program, str(workflow.script.parent), str(moment)],
            cwd=workflow.repo, env=workflow.env, capture_output=True, text=True, timeout=20,
        )
        assert done.returncode == 0, done.stderr
        assert f"ticket {allowed}" in done.stdout
        assert f"cloud {allowed}" in done.stdout


@pytest.mark.parametrize("source", ["cache", "live"])
def test_wake_explains_null_without_reporting_unknown(workflow: Workflow, source: str) -> None:
    moment = int(datetime(2026, 10, 14, 23, tzinfo=UTC).timestamp())
    workflow.orchestrator("idle")
    _feed(workflow, _answer(moment), source, moment)
    done = workflow.run_script("wake")
    assert done.returncode == 0, done.stderr
    assert "5-hour window 0% used, not started, weekly 36%" in workflow.sent()[-1]
    assert "starts: allowed" in workflow.wake_log.read_text()
    assert "window unknown" not in workflow.wake_log.read_text()


@pytest.mark.parametrize("source", ["cache", "live"])
def test_waiting_ticket_is_picked_then_launched_once(workflow: Workflow, source: str) -> None:
    moment = int(time.time())
    workflow.add_pull_request(7, labels=())
    (workflow.state / "body-7").write_text("Update example-app.\n\n## Files\n\nsrc/example_app.py\n")
    (_notes(workflow) / "room").write_text(f"{moment - 900} {moment - 180}\n")
    _feed(workflow, _answer(moment), source, moment)
    _tick(workflow)
    assert (workflow.state / "labelled").read_text().split() == ["7"]
    # The service can keep answering null after work has started; no manual recovery needed.
    for later in [moment + 180, moment + 360]:
        _tick(workflow, WORKFLOW_NOW=str(later))
    assert workflow.is_running("ticket-7")
    assert len(workflow.calls("tmux ticket-7 new-session")) == 1
    assert (workflow.state / "labelled").read_text().split() == ["7"]


def test_a_held_start_lock_defers_cloud_work_and_retry_launches_once(workflow: Workflow) -> None:
    moment = int(time.time())
    workflow.add_pull_request(7)
    _feed(workflow, _answer(moment), "live", moment)
    with (_notes(workflow) / "start-7.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for later in [moment, moment + 900]:
            done = workflow.run_script("tick", env={
                "WORKFLOW_NOW": str(later), "WORKFLOW_WORKER_PROGRAM": "cloud",
            })
            # Contention is surfaced as a failed tick; the next tick retries the start.
            assert done.returncode == 1
            assert "another start for pull request 7 is under way" in done.stderr
            assert not workflow.calls("tmux cloud-7 new-session")
            assert not (_notes(workflow) / "cloud-7").exists()
    for later in [moment + 1080, moment + 1260]:
        _tick(workflow, WORKFLOW_NOW=str(later), WORKFLOW_WORKER_PROGRAM="cloud")
    assert len(workflow.calls("tmux cloud-7 new-session")) == 1
    assert (_notes(workflow) / "cloud-7").exists()


def test_two_tick_calls_do_not_duplicate_cloud_launches(workflow: Workflow) -> None:
    moment = int(time.time())
    workflow.add_pull_request(7)
    _feed(workflow, _answer(moment), "live", moment)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: workflow.run_script(
            "tick", env={"WORKFLOW_WORKER_PROGRAM": "cloud"}), range(2)))
    assert all(done.returncode == 0 for done in results)
    assert len(workflow.calls("tmux cloud-7 new-session")) == 1


@pytest.mark.parametrize("checks", ["missing", "late", "partial-rerun"])
def test_null_usage_does_not_make_unproven_ready_work_merge(workflow: Workflow, checks: str) -> None:
    moment = int(time.time())
    workflow.add_pull_request(7, draft=False)
    _feed(workflow, _answer(moment), "live", moment)
    rollup = workflow.state / "rollup-7.json"
    cases = {
        "missing": [],
        "late": [{"name": "CI", "status": "IN_PROGRESS", "conclusion": None}],
        "partial-rerun": [
            {"name": "CI: tests (1 of 2)", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "CI: tests (2 of 2)", "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    }
    rollup.write_text(json.dumps(cases[checks]))
    before = (workflow.state / "pr/7").read_text()
    _tick(workflow, WORKFLOW_WORKER_PROGRAM="cloud")
    assert (workflow.state / "pr/7").read_text() == before
    assert not workflow.calls("gh pr merge")
