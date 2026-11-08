"""`usage` prints the figures the status line shows, from its file or from Anthropic."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from workflow_harness import Workflow

_FIVE_RESETS = "2026-10-14T01:10:00.318040+00:00"
_SEVEN_RESETS = "2026-10-16T07:00:00+00:00"
_ANSWER = {
    "five_hour": {"utilization": 76.0, "resets_at": _FIVE_RESETS},
    "seven_day": {"utilization": 41.0, "resets_at": _SEVEN_RESETS},
}
_LINES = "five_hour 76 1791940200\nseven_day 41 1792134000\n"
_NOW = 1791940000


def _usage_answer(workflow: Workflow, answer: dict[str, object], source: str) -> None:
    """The same payload, through either the fresh cache or the live endpoint."""
    cache = workflow.state / "usage-cache.json"
    workflow.env["WORKFLOW_USAGE_CACHE"] = str(cache)
    if source == "cache":
        cache.write_text(json.dumps(answer))
    else:
        _credentials(workflow)
        _answering(workflow, json.dumps(answer))


def _answering(workflow: Workflow, answer: str) -> None:
    """A stand-in for `curl` that answers as Anthropic does, and keeps what it was given."""
    (workflow.state / "answer").write_text(answer)
    workflow.fake(
        "curl",
        "#!/bin/bash\n"
        'printf \'%s\\n\' "$@" > "@STATE@/curl-args"\n'
        'cat > "@STATE@/curl-input"\n'
        'cat "@STATE@/answer"\n',
    )


def _credentials(workflow: Workflow) -> None:
    path = workflow.root / "home" / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tokenOfTheOwner"}}))


def test_the_status_line_file_is_read_while_it_is_fresh(workflow: Workflow) -> None:
    workflow.usage(76, _FIVE_RESETS, seven=41, seven_resets=_SEVEN_RESETS)

    done = workflow.run_script("usage")

    assert done.returncode == 0, done.stderr
    assert done.stdout == _LINES
    assert workflow.calls("curl") == []


def test_an_old_file_is_replaced_by_the_answer_of_anthropic(workflow: Workflow) -> None:
    workflow.usage(10, "2026-10-13T20:00:00+00:00")
    cache = workflow.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
    old = time.time() - 600
    os.utime(cache, (old, old))
    _credentials(workflow)
    _answering(workflow, json.dumps(_ANSWER))

    done = workflow.run_script("usage")

    assert done.returncode == 0, done.stderr
    assert done.stdout == _LINES
    # The token is given on standard input, never as an argument.
    assert "tokenOfTheOwner" not in (workflow.state / "curl-args").read_text()
    assert "Bearer tokenOfTheOwner" in (workflow.state / "curl-input").read_text()


def test_without_a_file_or_a_token_it_fails_and_prints_nothing(workflow: Workflow) -> None:
    done = workflow.run_script("usage")

    assert done.returncode == 1
    assert done.stdout == ""


def test_an_answer_that_is_not_the_figures_fails(workflow: Workflow) -> None:
    _credentials(workflow)
    _answering(workflow, '{"error": {"type": "authentication_error"}}')

    done = workflow.run_script("usage")

    assert done.returncode == 1
    assert done.stdout == ""


def test_a_window_without_its_reset_is_left_out(workflow: Workflow) -> None:
    _credentials(workflow)
    _answering(workflow, json.dumps({**_ANSWER, "seven_day": {"utilization": 3.0}}))

    done = workflow.run_script("usage")

    assert done.stdout == "five_hour 76 1791940200\n"


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("percent", [0.0, 12.5])
def test_not_started_windows_have_their_full_length_and_an_explicit_marker(
    workflow: Workflow, source: str, percent: float
) -> None:
    _usage_answer(workflow, {
        window: {"utilization": percent, "resets_at": None}
        for window in ("five_hour", "seven_day")
    }, source)

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        f"five_hour {int(percent)} {_NOW + 5 * 3600} not_started\n"
        f"seven_day {int(percent)} {_NOW + 7 * 86400} not_started\n"
    )
    assert (workflow.state / "curl-args").exists() is (source == "live")


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("windows", [("five_hour",), ("seven_day",), ("five_hour", "seven_day")])
def test_null_windows_have_zero_usage_and_have_not_started(
    workflow: Workflow, source: str, windows: tuple[str, ...]
) -> None:
    _usage_answer(workflow, {**_ANSWER, **dict.fromkeys(windows)}, source)

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert done.stdout == "".join(
        f"{window} 0 {_NOW + length} not_started\n" if window in windows else line
        for window, length, line in zip(
            ("five_hour", "seven_day"), (5 * 3600, 7 * 86400), _LINES.splitlines(keepends=True)
        )
    )
    assert (workflow.state / "curl-args").exists() is (source == "live")


@pytest.mark.parametrize("source", ["cache", "live"])
def test_zero_utilization_with_a_dated_reset_has_started(
    workflow: Workflow, source: str
) -> None:
    _usage_answer(workflow, {
        "five_hour": {"utilization": 0.0, "resets_at": _FIVE_RESETS},
    }, source)

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert done.stdout == "five_hour 0 1791940200\n"


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
@pytest.mark.parametrize("value", [
    [], "unknown", 0, False,
    {"resets_at": None},
    {"utilization": None, "resets_at": None},
    {"utilization": "0", "resets_at": None},
    {"utilization": False, "resets_at": None},
    {"utilization": "12", "resets_at": _FIVE_RESETS},
    {"utilization": 0, "resets_at": ""},
    {"utilization": 0, "resets_at": "invalid"},
    {"utilization": 0, "resets_at": "not_started"},
    {"utilization": 0},
])
def test_an_invalid_window_stays_unknown(
    workflow: Workflow, source: str, window: str, value: object
) -> None:
    _usage_answer(workflow, {window: value}, source)

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 1
    assert done.stdout == ""


@pytest.mark.parametrize("source", ["cache", "live"])
def test_a_missing_window_is_not_invented(workflow: Workflow, source: str) -> None:
    _usage_answer(workflow, {"seven_day": _ANSWER["seven_day"]}, source)

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert done.stdout == _LINES.splitlines(keepends=True)[1]


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("answer", [
    "not JSON",
    '{"five_hour": null, "seven_day": null} trailing',
    '{"five_hour": null}\n{"seven_day": null}',
])
def test_a_non_json_answer_fails_and_prints_nothing(
    workflow: Workflow, source: str, answer: str
) -> None:
    _credentials(workflow)
    _answering(workflow, answer)
    cache = workflow.state / "usage-cache.json"
    workflow.env["WORKFLOW_USAGE_CACHE"] = str(cache)
    if source == "cache":
        cache.write_text(answer)

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 1
    assert done.stdout == ""


@pytest.mark.parametrize(("age", "fresh"), [(299, True), (301, False)])
def test_the_file_counts_as_fresh_for_5_minutes(workflow: Workflow, age: int, fresh: bool) -> None:
    workflow.usage(76, _FIVE_RESETS)
    cache = workflow.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
    changed = int(time.time())
    os.utime(cache, (changed, changed))

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(changed + age)})

    assert (done.returncode == 0) == fresh


def _service(workflow: Workflow) -> Path:
    """A stale status cache and an endpoint with controllable headers and failures."""
    cache = workflow.state / "usage-cache.json"
    cache.write_text(json.dumps({"five_hour": {"utilization": 10, "resets_at": _FIVE_RESETS}}))
    os.utime(cache, (_NOW - 600, _NOW - 600))
    workflow.env.update(WORKFLOW_USAGE_CACHE=str(cache), WORKFLOW_NOW=str(_NOW))
    _credentials(workflow)
    (workflow.state / "answer").write_text(json.dumps(_ANSWER))
    workflow.fake("curl", """#!/bin/bash
echo curl >> "@STATE@/requests"
cat >/dev/null
while (( $# )); do
  if [[ $1 == --dump-header ]]; then
    if [[ -f "@STATE@/headers" ]]; then cat "@STATE@/headers" >"$2"; fi
    shift
  fi
  shift
done
if [[ -f "@STATE@/hold" ]]; then
  touch "@STATE@/in-flight"
  for ((i=0; i<500; i++)); do
    [[ -f "@STATE@/hold" ]] || break
    sleep 0.01
  done
  [[ ! -f "@STATE@/hold" ]] || exit 28
fi
[[ ! -f "@STATE@/fail" ]] || exit 22
cat "@STATE@/answer"
""")
    return workflow.repo / ".local" / "workflow"


def _requests(workflow: Workflow) -> int:
    return len((workflow.state / "requests").read_text().splitlines())


def test_ten_calls_share_one_service_answer(workflow: Workflow) -> None:
    _service(workflow)

    for _ in range(10):
        done = workflow.run_script("usage")
        assert done.returncode == 0, done.stderr
        assert done.stdout == _LINES

    assert _requests(workflow) == 1


def test_worktrees_share_the_configured_installation_state(workflow: Workflow) -> None:
    _service(workflow)
    config_file = workflow.repo / "shipline.json"
    config = json.loads(config_file.read_text())
    config["state_folder"] = ".local/usage state"
    config_file.write_text(json.dumps(config))
    worktree = workflow.worktree(7)
    workflow.git("worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")

    assert workflow.run_script("usage").stdout == _LINES
    assert workflow.run_script("usage", cwd=worktree).stdout == _LINES

    assert _requests(workflow) == 1
    assert (workflow.repo / config["state_folder"] / "usage-answer.json").is_file()
    assert not (worktree / config["state_folder"]).exists()


@pytest.mark.parametrize("retry_after", ["92", "600", "Wed, 14 Oct 2026 01:21:40 GMT"])
def test_failure_honors_retry_after_and_keeps_the_good_answer(
    workflow: Workflow, retry_after: str
) -> None:
    _service(workflow)
    assert workflow.run_script("usage").stdout == _LINES
    failed_at = _NOW + 300
    (workflow.state / "fail").touch()
    (workflow.state / "headers").write_text(
        f"HTTP/2 429\r\nrEtRy-AfTeR: {retry_after}\r\n\r\n"
    )

    assert workflow.run_script("usage", env={"WORKFLOW_NOW": str(failed_at)}).stdout == _LINES
    (workflow.state / "fail").unlink()
    pause = 300 if retry_after == "92" else 600
    for elapsed in (0, 91, 92, pause - 1):
        done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(failed_at + elapsed)})
        assert done.returncode == 0, done.stderr
        assert done.stdout == _LINES
        assert _requests(workflow) == 2

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(failed_at + pause)})
    assert done.stdout == _LINES
    assert _requests(workflow) == 3


@pytest.mark.parametrize("failure", ["http", "invalid"])
def test_failure_without_retry_after_waits_five_minutes(workflow: Workflow, failure: str) -> None:
    _service(workflow)
    if failure == "http":
        (workflow.state / "fail").touch()
    else:
        (workflow.state / "answer").write_text('{"error": {"type": "authentication_error"}}')

    for elapsed in (0, 1, 299, 300):
        done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW + elapsed)})
        assert done.returncode == 1
        assert done.stdout == ""
        assert _requests(workflow) == (2 if elapsed == 300 else 1)


@pytest.mark.parametrize(("age", "usable"), [(1800, True), (1801, False)])
def test_kept_answer_expires_after_thirty_minutes(
    workflow: Workflow, age: int, usable: bool
) -> None:
    _service(workflow)
    assert workflow.run_script("usage").stdout == _LINES
    (workflow.state / "fail").touch()

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW + age)})

    assert done.returncode == (0 if usable else 1), done.stderr
    assert done.stdout == (_LINES if usable else "")


@pytest.mark.parametrize("kept", [True, False])
def test_concurrent_callers_do_not_wait_for_or_repeat_a_request(
    workflow: Workflow, kept: bool
) -> None:
    _service(workflow)
    if kept:
        assert workflow.run_script("usage").stdout == _LINES
    workflow.env["WORKFLOW_NOW"] = str(_NOW + 300)
    hold = workflow.state / "hold"
    hold.touch()
    with ThreadPoolExecutor(max_workers=10) as callers:
        request = callers.submit(workflow.run_script, "usage")
        try:
            deadline = time.monotonic() + 10
            while not (workflow.state / "in-flight").exists():
                assert time.monotonic() < deadline, "the usage request did not start"
                time.sleep(0.01)
            others = [callers.submit(workflow.run_script, "usage") for _ in range(9)]
            for caller in others:
                done = caller.result(timeout=4)
                assert done.returncode == (0 if kept else 1), done.stderr
                assert done.stdout == (_LINES if kept else "")
            assert _requests(workflow) == (2 if kept else 1)
        finally:
            hold.unlink()
        assert request.result(timeout=10).stdout == _LINES


def test_kept_answer_is_private_and_holds_only_usage_figures(workflow: Workflow) -> None:
    folder = _service(workflow)
    (workflow.state / "answer").write_text(json.dumps({**_ANSWER, "extra": "tokenOfTheOwner"}))

    assert workflow.run_script("usage").stdout == _LINES

    kept = folder / "usage-answer.json"
    assert kept.is_file()
    assert kept.stat().st_mode & 0o777 == 0o600
    for file in folder.glob("usage-*"):
        assert "tokenOfTheOwner" not in file.read_text()
        assert file.stat().st_mode & 0o777 == 0o600


def test_fresh_status_cache_is_preferred_to_a_kept_answer(workflow: Workflow) -> None:
    _service(workflow)
    assert workflow.run_script("usage").stdout == _LINES
    cache = Path(workflow.env["WORKFLOW_USAGE_CACHE"])
    cache.write_text(json.dumps({"five_hour": {"utilization": 10, "resets_at": _FIVE_RESETS}}))
    os.utime(cache, (_NOW + 301, _NOW + 301))

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW + 301)})

    assert done.stdout == "five_hour 10 1791940200\n"
    assert _requests(workflow) == 1


def test_kept_null_window_is_still_not_started(workflow: Workflow) -> None:
    _service(workflow)
    (workflow.state / "answer").write_text('{"five_hour": null}')
    assert workflow.run_script("usage").returncode == 0

    done = workflow.run_script("usage", env={"WORKFLOW_NOW": str(_NOW + 120)})

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"five_hour 0 {_NOW + 120 + 5 * 3600} not_started\n"
    assert _requests(workflow) == 1


@pytest.mark.parametrize("kept", [True, False])
def test_shadow_ticks_read_usage_without_changing_live_state(workflow: Workflow, kept: bool) -> None:
    folder = _service(workflow)
    if kept:
        assert workflow.run_script("usage").stdout == _LINES

    def snapshot() -> tuple[bool, dict[str, tuple[bytes, int]]]:
        return folder.exists(), {
            file.name: (file.read_bytes(), file.stat().st_mtime_ns)
            for file in folder.glob("*")
        }

    before = snapshot()
    done = workflow.run_script("usage", env={
        "WORKFLOW_NOW": str(_NOW + 301),
        "SHIPLINE_SHADOW": str(workflow.repo / ".local" / "shipline" / "shadow.log"),
    })

    assert done.returncode == (0 if kept else 1), done.stderr
    assert done.stdout == (_LINES if kept else "")
    assert snapshot() == before
    if kept:
        assert _requests(workflow) == 1
    else:
        assert not (workflow.state / "requests").exists()
