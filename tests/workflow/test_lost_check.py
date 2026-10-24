"""A check GitHub still lists as running after its run completed: the gate runs CI again."""

import json
import time
from pathlib import Path
from typing import Any

import pytest
from workflow.test_merge_when_current import (
    GitHub,
    line_up,
    train_status,
    two_stale_pulls,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture

_REPO = "repos/example-org/example-app"


def _stamp(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _ci_run(github: GitHub, head: str) -> dict[str, Any]:
    runs = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
    return github.api[runs]["workflow_runs"][0]


def _lose(
    github: GitHub,
    number: str = "7",
    *,
    run_id: int = 20,
    finished_ago: float = 3600,
    name: str = "CI: server tests",
) -> None:
    """The run's jobs all completed (API), and GitHub's rollup still lists one as running."""
    _ci_run(github, github.pulls[number]["headRefOid"])["updated_at"] = _stamp(finished_ago)
    github.pulls[number]["statusCheckRollup"].append(
        {
            "__typename": "CheckRun",
            "name": name,
            "status": "IN_PROGRESS",
            "conclusion": None,
            "startedAt": _stamp(finished_ago + 300),
            "detailsUrl": f"https://github.com/{_REPO.removeprefix('repos/')}/actions/runs/"
            f"{run_id}/job/{run_id + 7}",
        }
    )
    github.save()


def _origin(github: GitHub, branch: str = "ticket/seven") -> str:
    return github.workflow.git("ls-remote", "origin", f"refs/heads/{branch}").split()[0]


def _github_shows_the_push(github: GitHub, *, lost_again: bool = False) -> bool:
    """What GitHub shows once it has the pushed head: its new CI run, green on current main."""
    if (github.workflow.state / "merged").exists():
        github.pulls["7"]["state"] = "MERGED"
        github.save()
        return False
    pushed = _origin(github)
    if pushed == github.pulls["7"]["headRefOid"]:
        return False
    github.head = pushed
    github.pulls["7"]["headRefOid"] = pushed
    github.pulls["7"]["statusCheckRollup"] = [
        {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    github.ci(github.base, pushed, run_id=50)
    if lost_again:
        _lose(github, run_id=50)
    github.save()
    return True


def _lost_notes(github: GitHub) -> list[dict[str, Any]]:
    pending = github.workflow.repo / ".local/workflow/lost-checks.pending"
    return [json.loads(path.read_text()) for path in sorted(pending.glob("*.json"))]


def test_a_lost_check_on_the_front_does_not_hold_the_train_for_good(github: GitHub) -> None:
    """The scan's regression test (scan-workflow-2). The stand-in GitHub does not change a pull
    request's head by itself, so between calls it is shown what GitHub shows after a push."""
    second = two_stale_pulls(github)
    workflow = github.workflow
    # 7 is green on current main: the CI run and every one of its jobs completed (API) ...
    github.ci(github.base, github.head)
    # ... but GitHub's rollup still lists one of that run's jobs as running: a lost job.
    _lose(github)
    outputs: list[tuple[int, str, int, str]] = []
    for _ in range(3):  # three calls of each gate, as the tick would make them over an hour
        first = github.run()
        later = workflow.run_script("merge-when-current", "8", cwd=second)
        outputs.append(
            (
                first.returncode,
                first.stderr.strip()[-80:],
                later.returncode,
                later.stdout.strip().splitlines()[-1][-60:],
            )
        )
        _github_shows_the_push(github)
    merged = (workflow.state / "merged").exists()
    refreshed = workflow.git("rev-parse", "HEAD", cwd=second) != github.pulls["8"]["headRefOid"]
    # Expected: either 7 merges (its run's jobs all finished green) or 8 gets to refresh.
    assert merged or refreshed, (outputs, train_status(github))


def test_the_gate_pushes_one_empty_commit_per_head_for_a_lost_check(github: GitHub) -> None:
    workflow = github.workflow
    lost_head = github.head
    _lose(github)

    first = github.run()

    assert first.returncode == 2, first.stdout + first.stderr
    assert "GitHub lost CI: server tests of run 20" in first.stdout
    pushed = _origin(github)
    assert pushed != lost_head
    assert workflow.git("rev-parse", f"{pushed}^") == lost_head
    assert workflow.git("rev-parse", f"{pushed}^{{tree}}") == workflow.git(
        "rev-parse", f"{lost_head}^{{tree}}"
    )
    assert workflow.git("log", "-1", "--format=%s", pushed) == (
        "ci: run CI again, GitHub lost CI: server tests of run 20"
    )
    assert not (workflow.state / "merged").exists()

    # GitHub has not shown the new head yet: the gate does not push again from the old one.
    stale = github.run()
    assert stale.returncode == 1, stale.stdout + stale.stderr
    assert "does not show the pushed head yet" in stale.stderr
    assert _origin(github) == pushed

    # The new head's own check is lost again: reported, never a second push.
    assert _github_shows_the_push(github, lost_again=True)
    again = github.run()
    assert again.returncode == 1, again.stdout + again.stderr
    assert "not pushed for twice" in again.stderr
    assert _origin(github) == pushed
    assert [
        (note["number"], note["head"], note["check"], note["run"]) for note in _lost_notes(github)
    ] == sorted(
        [("7", lost_head, "CI: server tests", 20), ("7", pushed, "CI: server tests", 50)],
        key=lambda note: f"{note[0]}-{note[1]}.json",
    )


@pytest.mark.parametrize(("minutes", "pushed"), [(9, False), (11, True)])
def test_a_check_is_lost_only_ten_minutes_after_its_run_finished(
    github: GitHub, minutes: int, pushed: bool
) -> None:
    head = github.head
    _lose(github, finished_ago=minutes * 60)

    done = github.run()

    assert (_origin(github) != head) == pushed, done.stdout + done.stderr
    if not pushed:
        assert done.returncode == 1
        assert "check is not passing: CI: server tests" in done.stderr


def test_a_lost_check_beside_one_still_running_is_waited_for(github: GitHub) -> None:
    head = github.head
    _lose(github)
    github.pulls["7"]["statusCheckRollup"].append(
        {
            "__typename": "CheckRun",
            "name": "E2E: browser tests (1 of 2)",
            "status": "IN_PROGRESS",
            "conclusion": None,
            "startedAt": _stamp(3000),
            "detailsUrl": "https://github.com/example-org/example-app/actions/runs/40/job/41",
        }
    )
    github.api[f"{_REPO}/actions/runs/40"] = {"id": 40, "status": "in_progress", "conclusion": None}

    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert "check is not passing" in done.stderr
    assert _origin(github) == head
    assert _lost_notes(github) == []


def test_a_lost_check_of_a_failed_run_is_reported_and_not_pushed_for(github: GitHub) -> None:
    head = github.head
    _lose(github, run_id=40, name="E2E: browser tests (2 of 2)")
    github.api[f"{_REPO}/actions/runs/40"] = {
        "id": 40,
        "status": "completed",
        "conclusion": "failure",
        "updated_at": _stamp(3600),
    }

    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert "which ended failure: treat it as a failed check" in done.stderr
    assert _origin(github) == head
    assert [note["run"] for note in _lost_notes(github)] == [40]


def test_a_failed_run_among_lost_checks_is_never_pushed_for(github: GitHub) -> None:
    """The green run's lost check is listed first; the failed run's one still decides."""
    head = github.head
    _lose(github)
    _lose(github, run_id=40, name="E2E: browser tests (2 of 2)")
    github.api[f"{_REPO}/actions/runs/40"] = {
        "id": 40,
        "status": "completed",
        "conclusion": "failure",
        "updated_at": _stamp(3600),
    }

    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert "of run 40, which ended failure" in done.stderr
    assert _origin(github) == head


def test_the_orchestrator_hears_of_a_lost_check_once_per_head(github: GitHub) -> None:
    folder = github.workflow.repo / ".local/workflow"
    _lose(github)
    line_up(github, ["7"])
    train_status(github)
    [note] = (folder / "lost-checks.pending").iterdir()
    # The wake delivered it.
    (folder / "lost-checks.sent").mkdir()
    note.rename(folder / "lost-checks.sent" / note.name)

    train_status(github)
    github.run()

    assert list((folder / "lost-checks.pending").iterdir()) == []


def test_a_green_member_behind_a_lost_check_takes_the_turn(github: GitHub) -> None:
    """Until its push, a lost check counts as a check still running."""
    two_stale_pulls(github)
    github.ci(github.base, github.head)  # 7: green on current main, but for its lost check.
    _lose(github)
    eight = github.pulls["8"]["headRefOid"]
    github.ci(github.base, eight, run_id=30)  # 8: green, tested on current main.
    github.save()
    line_up(github, ["7", "8"])

    train = train_status(github)

    assert train["first"] == "8"
    assert train["waiting"] == ["7", "8"]
    assert train["lost"] == {
        "7": {"head": github.head, "check": "CI: server tests", "run": 20, "conclusion": "success"}
    }
    assert [note["number"] for note in _lost_notes(github)] == ["7"]


def _waiting_reviewer(github: GitHub) -> None:
    workflow = github.workflow
    workflow.add_worktree(7, draft=False)
    workflow.set_pull_request(7, "OPEN", github.head)
    notes = workflow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "review-7.started").touch()
    (notes / "review-7.checks-wait").touch()
    workflow.screen("● Waiting for checks.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n", "review-7")
    (workflow.state / "rollup-7.json").write_text(
        json.dumps(github.pulls["7"]["statusCheckRollup"])
    )


def test_the_tick_wakes_the_front_reviewer_once_per_head_for_a_lost_check(github: GitHub) -> None:
    workflow = github.workflow
    _lose(github)
    line_up(github, ["7"])
    _waiting_reviewer(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    sent = workflow.calls("tmux review-7 send-keys")
    assert len(sent) == 2, sent  # literal text and Enter, once
    assert "GitHub lost a check on " + github.head in sent[0]
    [mark] = (workflow.repo / ".local/workflow").glob("review-7.train-nudged-*")
    assert mark.name.endswith(f"-{github.head}-lost")
    log = (Path(workflow.env["HOME"]) / ".local/state/workflow/wake.log").read_text()
    assert f"review-7 lost check on {github.head}; reviewer nudged" in log


def test_the_front_with_a_lost_check_is_woken_again_when_main_changes(github: GitHub) -> None:
    """A wake while main's checks run ends in a wait at the gate; main's end wakes it again."""
    workflow = github.workflow
    _lose(github)
    line_up(github, ["7"])
    _waiting_reviewer(github)
    main_run = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"][0]
    main_run.update(status="in_progress", conclusion=None)
    github.save()
    assert workflow.run_script("tick").returncode == 0
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-7 send-keys")) == 2

    main_run.update(status="completed", conclusion="success")
    github.save()
    assert workflow.run_script("tick").returncode == 0

    assert len(workflow.calls("tmux review-7 send-keys")) == 4


def test_a_lost_check_behind_the_front_does_not_wake_its_reviewer(github: GitHub) -> None:
    workflow = github.workflow
    two_stale_pulls(github)
    github.ci(github.base, github.head)
    _lose(github)
    line_up(github, ["8", "7"])
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert train_status(github)["first"] == "8"
    assert "7" in train_status(github)["lost"]
    assert workflow.calls("tmux review-7 send-keys") == []


def test_a_running_check_does_not_wake_the_reviewer(github: GitHub) -> None:
    workflow = github.workflow
    _lose(github, finished_ago=60)
    line_up(github, ["7"])
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert train_status(github)["lost"] == {}


def _red(github: GitHub, *, finished_ago: float = 3600) -> None:
    """7's CI run failed in CI: server tests (API), its jobs all completed."""
    run = _ci_run(github, github.head)
    run.update(conclusion="failure", updated_at=_stamp(finished_ago))
    jobs = github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"]
    jobs[1]["conclusion"] = "failure"
    jobs.append({"id": 23, "name": "CI: server tests", "status": "completed", "conclusion": "failure"})
    github.api[f"{_REPO}/actions/runs/20"] = run
    github.save()


def _failed_beside_lost(github: GitHub) -> None:
    """GitHub missed the end of the failed job: it shows running, the result check failed."""
    _red(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    _lose(github)


def _red_with_its_job_lost(github: GitHub) -> None:
    """The run failed, and GitHub lists every one of its checks as running still."""
    _red(github)
    github.pulls["7"]["statusCheckRollup"] = []
    _lose(github)
    _lose(github, name="CI result")


@pytest.mark.parametrize("shown", [_red_with_its_job_lost, _failed_beside_lost])
def test_a_failed_member_with_a_lost_check_wakes_its_reviewer_once(
    github: GitHub, shown: Any
) -> None:
    workflow = github.workflow
    head = github.head
    shown(github)
    line_up(github, ["7"])
    _waiting_reviewer(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    sent = workflow.calls("tmux review-7 send-keys")
    assert len(sent) == 2, sent  # literal text and Enter, once
    assert f"The checks on {head} have finished" in sent[0]
    assert (workflow.repo / f".local/workflow/review-7.checks-nudged-{head}").exists()
    log = (Path(workflow.env["HOME"]) / ".local/state/workflow/wake.log").read_text()
    assert f"review-7 checks finished on {head}; reviewer nudged" in log
    assert [
        (note["number"], note["head"], note["check"], note["run"]) for note in _lost_notes(github)
    ] == [("7", head, "CI: server tests", 20)]
    assert "7" in train_status(github)["blocked"]
    assert _origin(github) == head

    # The woken reviewer's gate call refuses for the failed check, and pushes nothing.
    done = github.run()

    assert done.returncode == 1, done.stdout + done.stderr
    assert _origin(github) == head
    assert not (workflow.state / "merged").exists()
    assert len(_lost_notes(github)) == 1


def test_a_failed_member_with_a_check_still_running_wakes_nobody(github: GitHub) -> None:
    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    github.pulls["7"]["statusCheckRollup"].append(
        {
            "__typename": "CheckRun",
            "name": "E2E: browser tests (1 of 2)",
            "status": "IN_PROGRESS",
            "conclusion": None,
            "startedAt": _stamp(3000),
            "detailsUrl": "https://github.com/example-org/example-app/actions/runs/40/job/41",
        }
    )
    github.api[f"{_REPO}/actions/runs/40"] = {"id": 40, "status": "in_progress", "conclusion": None}
    github.save()
    line_up(github, ["7"])
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    train = train_status(github)
    assert "7" in train["blocked"]
    assert train["lost"] == {}
    assert _lost_notes(github) == []
    assert _origin(github) == head


def test_a_failed_members_check_just_started_ends_the_search(github: GitHub) -> None:
    """Its run cannot have completed ten minutes ago: it is not read."""
    workflow = github.workflow
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    _lose(github, run_id=40, finished_ago=-240, name="E2E: browser tests (1 of 2)")
    line_up(github, ["7"])
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert train_status(github)["lost"] == {}
    assert not [call for call in workflow.calls("gh api") if call.endswith("/actions/runs/40")]
