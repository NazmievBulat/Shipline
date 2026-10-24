"""Breaking tests for pull request 47: a failed check on a handed-off pull request.

Each replays a daily flow of an installation (a refresh that merges main in and then fails in the
server test job, a runner lost or a job timed out on the repository's own runners, a long line)
or a failure met there, through the real tick and the real gate.
"""

import json

import pytest
from workflow.test_merge_driver import (
    _RUNS,
    _fail,
    _fix_and_hand_off,
    _folder,
    _github_answers_repairs,
    _handbacks,
    _note,
    _repair_sentence,
    _stale_note,
    _started,
    _train,
    github,
    handed,
)
from workflow.test_merge_when_current import GitHub
from workflow_harness import session_arguments

__all__ = ["github", "handed"]

_JOBS = "repos/example-org/example-app/actions/runs/{run}/jobs?per_page=100"
_NOTES = "repos/example-org/example-app/check-runs/{job}/annotations?per_page=100"


def _gh_log(*lines: str, step: str = "Run server tests") -> str:
    """What `gh run view --log-failed` prints: job, step and time before each line."""
    return "".join(
        f"CI: server tests\t{step}\t2026-10-24T10:00:{index:02d}.0000000Z {line}\n"
        for index, line in enumerate(lines)
    )


def _refresh(handed: GitHub, run_id: int) -> str:
    """Main moved; the tick merges it into the handed-off pull request and pushes."""
    workflow = handed.workflow
    _stale_note(handed)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != handed.head
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    handed.head = refreshed
    handed.pulls["7"]["headRefOid"] = refreshed
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {refreshed}\n")
    handed.ci(handed.base, refreshed, run_id=run_id)
    handed.save()
    return refreshed


_SERVER_FAILURE = _gh_log(
    "============================= test session starts ==============================",
    "[gw1] [ 42%] FAILED tests/api/test_orders.py::test_totals_after_discount",
    "E       AssertionError: expected 200, got 500 Internal Server Error",
    "=========================== short test summary info ============================",
    "FAILED tests/api/test_orders.py::test_totals_after_discount - AssertionError: expected 200",
    "##[error]Process completed with exit code 1.",
)


def test_daily_refresh_then_a_server_test_failure_goes_to_a_medium_worker(
    handed: GitHub,
) -> None:
    """The refresh merges main in; the server test job fails on a real test."""
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    refreshed = _refresh(handed, run_id=50)
    _fail(handed, run_id=50, log=_SERVER_FAILURE)

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    assert workflow.calls("gh pr ready") == [
        "gh pr ready 7 -R example-org/example-app --undo"
    ]
    assert _started(workflow) == []
    assert _train(workflow)["blocked"]["7"] == refreshed
    assert "7" in _train(workflow)["waiting"]

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert _started(workflow) == ["ticket-7"]
    sentence = _repair_sentence(
        run=50, test="tests/api/test_orders.py::test_totals_after_discount"
    )
    assert session_arguments(workflow.state, "ticket-7")[-1] == (
        f'claude --effort medium --remote-control ticket-7 "{sentence}"'
    )


def _runner_failed(handed: GitHub, message: str, conclusion: str) -> None:
    """The job failed for its runner: GitHub's own annotation, and no failed log to read."""
    _fail(handed, log=None, conclusion=conclusion)
    handed.api[_NOTES.format(job=23)] = [{"title": "", "message": message}]
    handed.save()


_LOST = (
    "The self-hosted runner: example-runner-1 lost communication with the server. Verify the "
    "machine is running and has a healthy network connection. Anything in your workflow that "
    "terminates the runner process, starves it for CPU/Memory, or blocks its network access can "
    "cause this error."
)
_SHUTDOWN = (
    "The runner has received a shutdown signal. This can happen when the runner service is "
    "stopped, or a manually started runner is canceled."
)
_TIMED_OUT = (
    "The job running on runner example-runner-1 has exceeded the maximum execution time of 30 "
    "minutes."
)


@pytest.mark.parametrize(
    ("message", "conclusion"),
    [
        (_LOST, "failure"),
        (_SHUTDOWN, "failure"),
        (_TIMED_OUT, "timed_out"),
        (_TIMED_OUT, "cancelled"),
    ],
    ids=[
        "runner lost",
        "runner restarted",
        "timed out",
        "timed out reported cancelled",
    ],
)
def test_a_runner_error_as_github_reports_it_is_rerun_once(
    handed: GitHub, message: str, conclusion: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _runner_failed(handed, message, conclusion)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == [
        "gh run rerun 20 -R example-org/example-app --failed"
    ]
    assert workflow.calls("gh pr ready") == []
    assert _started(workflow) == []
    assert _note(workflow).exists()


@pytest.mark.parametrize(
    ("message", "conclusion"),
    [(_LOST, "failure"), (_TIMED_OUT, "timed_out")],
    ids=["runner lost", "timed out"],
)
def test_a_runner_error_in_one_part_beside_a_part_fail_fast_cancelled_is_rerun_once(
    handed: GitHub, message: str, conclusion: str
) -> None:
    """Two parts of one matrix: one fails for its runner, and fail-fast cancels the other."""
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _runner_failed(handed, message, conclusion)
    jobs = handed.api[_JOBS.format(run=20)]["jobs"]
    jobs.insert(
        0,
        {
            "id": 19,
            "name": "CI: server tests (2 of 2)",
            "status": "completed",
            "conclusion": "cancelled",
            "labels": ["example-app-home"],
        },
    )
    handed.api[_NOTES.format(job=19)] = [
        {
            "title": "",
            "message": 'The strategy configuration was canceled because "server-tests._1" failed',
        }
    ]
    (workflow.state / "job-logs/19").write_text(
        _gh_log("##[error]The operation was canceled.", step="Run server tests")
    )
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == [], (
        "a runner error went to a reviewer without its re-run"
    )
    assert workflow.calls("gh run rerun") == [
        "gh run rerun 20 -R example-org/example-app --failed"
    ]


def test_a_server_that_fails_to_start_on_its_own_code_goes_to_a_worker(
    handed: GitHub,
) -> None:
    """No test ran: the server answered 500 after main was merged in. The code is to blame."""
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(
        handed,
        log=_gh_log(
            "Waiting for the server on /health",
            'INFO:     127.0.0.1 - "GET /health HTTP/1.1" 500 Internal Server Error',
            "##[error]Process completed with exit code 1.",
            step="Start the server",
        ),
    )

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == [], (
        "the pull request's own failure was re-run"
    )
    assert workflow.calls("gh pr ready") == [
        "gh pr ready 7 -R example-org/example-app --undo"
    ]


def test_a_parametrized_test_id_with_spaces_is_named_whole(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(
        handed,
        log=_gh_log(
            "FAILED tests/api/test_orders.py::test_totals[two items] - assert 3 == 4",
            "##[error]Process completed with exit code 1.",
        ),
    )

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr

    sentence = _repair_sentence(test="tests/api/test_orders.py::test_totals[two items]")
    assert session_arguments(workflow.state, "ticket-7")[-1] == (
        f'claude --effort medium --remote-control ticket-7 "{sentence}"'
    )


def test_github_unreadable_while_the_failure_is_read_keeps_the_handoff(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed)
    notes = handed.api[_NOTES.format(job=23)]
    handed.api[_NOTES.format(job=23)] = "HTTP 502: Bad Gateway"
    handed.save()

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _note(workflow).exists()
        assert workflow.calls("gh pr ready") == []
        assert _started(workflow) == []
        assert not (_folder(workflow) / "merge-review-7").exists()

    handed.api[_NOTES.format(job=23)] = notes
    handed.save()
    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh pr ready") == [
        "gh pr ready 7 -R example-org/example-app --undo"
    ]
    assert not (_folder(workflow) / "merge-review-7").exists()


def _line(handed: GitHub, numbers: range) -> list[str]:
    """Ready pull requests waiting behind 7, without sessions or handoffs."""
    workflow = handed.workflow
    for number in numbers:
        branch = f"ticket/n{number}"
        workflow.git("branch", branch, handed.base)
        workflow.git("push", "origin", branch)
        head = workflow.git("rev-parse", branch)
        handed.pulls[str(number)] = {
            **json.loads(json.dumps(handed.pulls["7"])),
            "headRefOid": head,
            "headRefName": branch,
            "labels": [],
        }
        handed.ci(handed.base, head, run_id=100 + 10 * number)
        workflow.list_pull_request(number, draft=False, labels=())
        (workflow.state / "pr" / str(number)).write_text(f"OPEN {branch} {head}\n")
    handed.save()
    order = ["7", *map(str, numbers)]
    path = _folder(workflow) / "merge-train.json"
    train = json.loads(path.read_text())
    train["waiting"] = order
    path.write_text(json.dumps(train))
    return order


def test_a_line_of_twelve_keeps_the_failed_front_in_its_place_through_its_fix(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    order = _line(handed, range(8, 19))
    _fail(handed)

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _train(workflow)["waiting"] == order
        assert "7" in _train(workflow)["blocked"]
    assert _started(workflow) == ["ticket-7"]

    workflow.run("stop", "ticket-7")
    _fix_and_hand_off(handed)
    workflow.run("stop", "review-7")

    assert _train(workflow)["waiting"] == order
    assert "7" not in _train(workflow)["blocked"]


def test_a_failing_test_after_the_fix_passed_and_was_refreshed_is_a_first_one_again(
    handed: GitHub,
) -> None:
    """The fix passed; main moved on, the tick refreshed it and a new test failed: a worker."""
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed)
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    assert _started(workflow) == ["ticket-7"]
    workflow.run("stop", "ticket-7")
    _fix_and_hand_off(handed)
    workflow.run("stop", "review-7")
    _refresh(handed, run_id=60)
    assert not (_folder(workflow) / "check-repair-7.json").exists()
    _fail(handed, run_id=60)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh pr ready")) == 2
    assert "review-7" not in _started(workflow)[1:]
    assert any("a worker fixes job" in reason for reason in _handbacks(workflow))
    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    assert run["id"] == 60
