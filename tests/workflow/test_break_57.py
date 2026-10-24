"""Breaking tests for pull request 57: a bounded wait for a scratch job held in main's lane.

They replay daily uses with neutral names: a prefix whose server tests run in two parts and
whose browser run asks for main's lane, main's machine rebooting while both wait there, an
image check waiting long for a Docker runner, a pool with main's runners turned off, a second
attempt waiting behind pull requests' own CI, and GitHub failing the one retry the gate sends.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from workflow.test_merge_when_current import (
    _REPO,
    GitHub,
    _batch_call,
    _batch_setup,
    _finish_batch,
    _merges,
    _prefixes,
    _queue_config,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture

_LANE = ["self-hosted", "example-app-home-main-batch"]
_ORDINARY = ["self-hosted", "example-app-home"]
_DOCKER = ["self-hosted", "example-app-home-docker"]
_ROOT = Path(__file__).resolve().parents[2]


def _ago(minutes: float) -> str:
    return datetime.fromtimestamp(time.time() - minutes * 60, UTC).isoformat()


def _job(identity: int, name: str, labels: list[str], *, minutes: float, attempt: int = 1,
         status: str = "queued") -> dict[str, Any]:
    return {
        "id": identity, "name": name, "status": status, "conclusion": None,
        "labels": labels, "created_at": _ago(minutes), "run_attempt": attempt,
    }


def _identity(github: GitHub, number: str, workflow: str) -> int:
    # The ids _finish_batch gives, so a finished run is the same run the gate retried.
    members = _prefixes(github)
    position = [member["number"] for member in members].index(number)
    scratch = members[position]["scratch"]
    return 100 + position * 1000 + scratch["workflows"].index(workflow) * 10


def _put_run(
    github: GitHub, number: str, workflow: str, jobs: list[dict[str, Any]], *,
    attempt: int = 1, status: str = "in_progress", conclusion: str | None = None,
) -> int:
    member = next(member for member in _prefixes(github) if member["number"] == number)
    scratch = member["scratch"]
    identity = _identity(github, number, workflow)
    github.api[
        f"{_REPO}/actions/workflows/{workflow}.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    ] = {"workflow_runs": [{
        "id": identity, "head_sha": scratch["head"], "head_branch": scratch["branch"],
        "event": "workflow_dispatch", "status": status, "conclusion": conclusion,
        "run_attempt": attempt,
    }]}
    github.api[f"{_REPO}/actions/runs/{identity}/jobs?per_page=100"] = {"jobs": jobs}
    return identity


def _two_part_ci(identity: int, *, minutes: float, labels: list[str] = _LANE,
                 attempt: int = 1) -> list[dict[str, Any]]:
    planner = _job(identity + 1, "CI: what to run", ["home-planner"], minutes=minutes + 2,
                   attempt=attempt, status="completed")
    planner["conclusion"] = "success"
    return [
        planner,
        _job(identity + 3, "CI: server tests (1 of 2)", labels, minutes=minutes, attempt=attempt),
        _job(identity + 4, "CI: server tests (2 of 2)", labels, minutes=minutes, attempt=attempt),
    ]


def _browser(identity: int, *, minutes: float, labels: list[str] = _LANE,
             attempt: int = 1) -> list[dict[str, Any]]:
    return [_job(identity + 3, "playwright", labels, minutes=minutes, attempt=attempt)]


def _calls(github: GitHub, words: str) -> list[str]:
    return github.workflow.calls(words)


def _finish_on_attempt(github: GitHub, attempts: dict[int, int]) -> None:
    _finish_batch(github)
    for member in _prefixes(github):
        scratch = member["scratch"]
        for workflow in scratch["workflows"]:
            run = github.api[
                f"{_REPO}/actions/workflows/{workflow}.yml/runs?head_sha={scratch['head']}"
                "&event=workflow_dispatch&per_page=100"
            ]["workflow_runs"][0]
            run["run_attempt"] = attempts.get(run["id"], 1)


def _started(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3


def test_a_lane_job_taken_before_the_limit_merges_without_any_retry(github: GitHub) -> None:
    _started(github)
    ci = _identity(github, "7", "ci")
    e2e = _identity(github, "7", "e2e")
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=5))
    _put_run(github, "7", "e2e", _browser(e2e, minutes=5))
    assert _batch_call(github).returncode == 3
    _finish_on_attempt(github, {})
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert not _calls(github, "gh run cancel")
    assert not _calls(github, "gh run rerun")


def test_main_machine_rebooting_under_both_runs_retries_each_once_and_the_line_merges(
    github: GitHub,
) -> None:
    _started(github)
    ci = _identity(github, "7", "ci")
    e2e = _identity(github, "7", "e2e")
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=25))
    _put_run(github, "7", "e2e", _browser(e2e, minutes=25))
    assert _batch_call(github).returncode == 3
    assert _batch_call(github).returncode == 3
    assert len(_calls(github, f"gh run cancel {ci} ")) == 1
    assert len(_calls(github, f"gh run cancel {e2e} ")) == 1
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=25), status="completed",
             conclusion="cancelled")
    _put_run(github, "7", "e2e", _browser(e2e, minutes=25), status="completed",
             conclusion="cancelled")
    assert _batch_call(github).returncode == 3
    assert _batch_call(github).returncode == 3
    assert _calls(github, f"gh run rerun {ci} ") == [f"gh run rerun {ci} -R example-org/example-app"]
    assert _calls(github, f"gh run rerun {e2e} ") == [
        f"gh run rerun {e2e} -R example-org/example-app"
    ]
    # Attempt 2 runs on the ordinary label: running, nothing more is sent.
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=1, labels=_ORDINARY, attempt=2),
             attempt=2)
    _put_run(github, "7", "e2e", _browser(e2e, minutes=1, labels=_ORDINARY, attempt=2),
             attempt=2)
    assert _batch_call(github).returncode == 3
    _finish_on_attempt(github, {ci: 2, e2e: 2})
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert len(_calls(github, "gh run cancel")) == 2
    assert len(_calls(github, "gh run rerun")) == 2


def test_an_image_check_waiting_for_a_docker_runner_is_never_counted(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    member = next(member for member in _prefixes(github) if member["number"] == "7")
    assert "images" in member["scratch"]["workflows"]
    ci = _identity(github, "7", "ci")
    images = _identity(github, "7", "images")
    jobs = _two_part_ci(ci, minutes=1)
    for job in jobs[1:]:
        job["status"] = "in_progress"
    # A Docker job inside CI and the image check's own job both wait 87 minutes.
    jobs.append(_job(ci + 5, "CI: image smoke", _DOCKER, minutes=87))
    _put_run(github, "7", "ci", jobs)
    _put_run(github, "7", "images", [_job(images + 2, "Docker images: image tests", _DOCKER,
                                          minutes=87)])
    for _ in range(3):
        done = _batch_call(github)
        assert done.returncode == 3
        assert "queued past" not in done.stdout
    assert not _calls(github, "gh run cancel")
    assert not _calls(github, "gh run rerun")


def test_with_main_runners_off_a_long_ordinary_wait_changes_nothing(github: GitHub) -> None:
    _started(github)
    ci = _identity(github, "7", "ci")
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=60, labels=_ORDINARY))
    for _ in range(2):
        done = _batch_call(github)
        assert done.returncode == 3
        assert "queued past" not in done.stdout
    assert not _calls(github, "gh run cancel")
    assert not (github.workflow.repo / ".local/workflow/prefix-queues.pending").exists()


def test_a_second_attempt_behind_pull_requests_is_told_once_then_merges(github: GitHub) -> None:
    _started(github)
    ci = _identity(github, "7", "ci")
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=25))
    assert _batch_call(github).returncode == 3
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=25), status="completed",
             conclusion="cancelled")
    assert _batch_call(github).returncode == 3
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=60, labels=_ORDINARY, attempt=2),
             attempt=2)
    outputs = [_batch_call(github).stdout for _ in range(3)]
    assert sum("queued past 20 minutes" in output for output in outputs) == 1
    _finish_on_attempt(github, {ci: 2})
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(_calls(github, "gh run rerun")) == 1


def test_a_cancel_that_loses_the_race_to_a_green_finish_is_judged_by_its_result(
    github: GitHub,
) -> None:
    _started(github)
    ci = _identity(github, "7", "ci")
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=25))
    assert _batch_call(github).returncode == 3
    assert len(_calls(github, f"gh run cancel {ci} ")) == 1
    _finish_on_attempt(github, {})
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert not _calls(github, "gh run rerun")


def _rerun_fails_once(github: GitHub, error: str) -> None:
    """The stand-in answers the next `gh run rerun` with `error`, as GitHub in an outage."""
    stand_in = github.workflow.bin / "gh.py"
    text = stand_in.read_text()
    old = '    elif args[:2] == ["run", "rerun"]:\n'
    assert old in text
    stand_in.write_text(text.replace(old, old + (
        '        once = state / "rerun-fails-once"\n'
        '        if once.exists():\n'
        '            message = once.read_text()\n'
        '            once.unlink()\n'
        '            sys.exit(message)\n'
    )))
    (github.workflow.state / "rerun-fails-once").write_text(error)


@pytest.mark.parametrize("error", [
    "HTTP 502: Bad Gateway", "Post \"https://api.github.com/x\": context deadline exceeded",
])
def test_the_one_retry_lost_to_a_github_outage_is_sent_again_once_github_answers(
    github: GitHub, error: str,
) -> None:
    _started(github)
    ci = _identity(github, "7", "ci")
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=25))
    assert _batch_call(github).returncode == 3
    _put_run(github, "7", "ci", _two_part_ci(ci, minutes=25), status="completed",
             conclusion="cancelled")
    _rerun_fails_once(github, error)
    _batch_call(github)
    assert len(_calls(github, f"gh run rerun {ci} ")) == 1
    # GitHub answers again; the run is still its cancelled first attempt. The gate's later
    # calls must still get it re-run, or the prefix, and the whole line, waits for ever.
    outputs = [_batch_call(github).stdout for _ in range(3)]
    assert len(_calls(github, f"gh run rerun {ci} ")) == 2, outputs[-1][-600:]


def test_the_example_config_names_each_key_once() -> None:
    pairs: list[tuple[str, Any]] = []

    def record(items: list[tuple[str, Any]]) -> dict[str, Any]:
        pairs.extend(items)
        keys = [key for key, _ in items]
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        assert not duplicated, f"examples/shipline.json names {duplicated} twice"
        return dict(items)

    json.loads((_ROOT / "examples/shipline.json").read_text(), object_pairs_hook=record)
