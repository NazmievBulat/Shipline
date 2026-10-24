"""Breaking tests for pull request 32: GitHub answering no CI runs for a head for a while."""

import json
from copy import deepcopy

from workflow.test_merge_when_current import (
    _SUITE_RUNS,
    GitHub,
    _check_runs,
    _ci_runs,
    _green_rerun_on_old_main,
    _line,
    _train,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture

_REPO = "repos/example-org/example-app"


def _check_runs_path(head: str) -> str:
    return f"{_REPO}/commits/{head}/check-runs?per_page=100"


def test_a_moment_with_no_runs_and_no_check_runs_keeps_the_green_rerun_waiting(
    github: GitHub,
) -> None:
    """Right after a re-run GitHub can list neither the run nor the head's check runs for a
    minute or two. The green re-run must keep its place through that look, and a gate call
    then must wait, not refuse it as failed."""
    _green_rerun_on_old_main(github)
    _check_runs(github, github.head, ("CI: what to run", "completed"), ("CI result", "completed"))
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    listed = github.api[_ci_runs(github.head)]
    checks = github.api[_check_runs_path(github.head)]
    # For a moment both lists come back empty.
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    github.api[_check_runs_path(github.head)] = {"total_count": 0, "check_runs": []}
    github.api[_SUITE_RUNS] = {"workflow_runs": []}
    github.save()
    train = _train(github)
    gate = github.run()
    # Then GitHub lists both again.
    github.api[_ci_runs(github.head)] = listed
    github.api[_check_runs_path(github.head)] = checks
    github.save()
    after = _train(github)
    assert after["waiting"] == ["7", "8"]
    assert train["blocked"] == ["7"]
    assert train["waiting"] == ["7", "8"], train
    assert train["first"] == "7", train
    assert "once a re-run is green" not in gate.stderr, gate.stderr


def _unlisted_line(github: GitHub, members: int) -> list[str]:
    """A line of `members` pull requests whose CI jobs all finished, but whose CI runs GitHub
    lists neither by head nor by check suite: the moment GitHub's rate limit hits them all."""
    numbers = [str(7 + index) for index in range(members)]
    for index, number in enumerate(numbers):
        if number not in github.pulls:
            github.pulls[number] = {
                **deepcopy(github.pulls["7"]),
                "headRefOid": f"{index + 1:040x}",
                "headRefName": f"ticket/member-{number}",
            }
            github.workflow.running(f"review-{number}")
        head = github.pulls[number]["headRefOid"]
        github.api[_ci_runs(head)] = {"workflow_runs": []}
        _check_runs(github, head, ("CI: what to run", "completed"), ("CI result", "completed"))
    github.api[_SUITE_RUNS] = {"workflow_runs": []}
    github.save()
    _line(github, numbers)
    return numbers


def test_one_read_of_a_line_github_lists_no_runs_for_pauses_once_not_per_member(
    github: GitHub,
) -> None:
    """One reconciliation holds the merge lock. Its pause must not grow with the line: with
    six members it would hold the lock three minutes, a whole gate period."""
    _unlisted_line(github, 6)
    done = github.workflow.run_script("merge-when-current", "--train-status")
    assert done.returncode == 0, done.stderr
    sleeps = github.workflow.calls("sleep 30")
    assert len(sleeps) <= 1, f"{len(sleeps)} pauses of 30 s under the merge lock in one read"


def test_a_gate_call_on_a_line_github_lists_no_runs_for_pauses_once(github: GitHub) -> None:
    """The first member's gate call reconciles the whole line under the merge lock."""
    _unlisted_line(github, 4)
    done = github.run()
    assert done.returncode != 0
    sleeps = github.workflow.calls("sleep 30")
    assert len(sleeps) <= 1, f"{len(sleeps)} pauses of 30 s under the merge lock in one call"


def test_the_saved_line_after_an_empty_look_still_names_the_rerun_refreshable(
    github: GitHub,
) -> None:
    """The run is listed nowhere for one look, but the check runs show the CI jobs finished:
    the saved line keeps the member's mark, so the next tick reads it as waiting."""
    _green_rerun_on_old_main(github)
    _check_runs(github, github.head, ("CI: what to run", "completed"), ("CI result", "completed"))
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    listed = github.api[_ci_runs(github.head)]
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    github.api[_SUITE_RUNS] = {"workflow_runs": []}
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    saved = json.loads((github.workflow.repo / ".local/workflow/merge-train.json").read_text())
    assert "7" in json.dumps(saved.get("refreshable", saved))
    github.api[_ci_runs(github.head)] = listed
    github.save()
    assert _train(github)["waiting"] == ["7", "8"]
    gate = github.run()
    assert gate.returncode == 2, gate.stdout + gate.stderr
    assert "failed checks" not in gate.stderr


def test_a_partial_rerun_still_running_is_pending_and_keeps_no_stale_mark(
    github: GitHub,
) -> None:
    """A failed job re-run in part: its new attempt runs while the other jobs are finished.
    With no run listed, the member is not waiting yet and nothing is refreshed."""
    _green_rerun_on_old_main(github)
    _check_runs(
        github,
        github.head,
        ("CI: what to run", "completed"),
        ("CI: server tests", "in_progress"),
        ("CI result", "in_progress"),
    )
    run = github.api[_ci_runs(github.head)]["workflow_runs"][0]
    github.api[_ci_runs(github.head)] = {"workflow_runs": []}
    github.api[_SUITE_RUNS] = {
        "workflow_runs": [
            {
                **run,
                "status": "in_progress",
                "conclusion": None,
                "path": ".github/workflows/ci.yml",
            }
        ]
    }
    github.save()
    train = _train(github)
    assert train["blocked"] == ["7"]
    assert train["waiting"] == ["8"]
    assert not github.workflow.calls("sleep 30")
