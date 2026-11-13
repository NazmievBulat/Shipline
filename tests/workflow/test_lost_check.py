"""A check GitHub still lists as running after its run completed: the gate runs CI again."""

import fcntl
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow
from workflow.merge_gate_fixtures import _tick_context
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import (
    GitHub,
    blocked_first,
    line_up,
    refreshed_first,
    train_status,
    two_stale_pulls,
    waiting_reviewers,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture
handed = handed_fixture

_REPO = "repos/example-org/example-app"


def _stamp(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _outside_check(github: GitHub) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    check = {
        "__typename": "CheckRun", "name": ".github/dependabot.yml", "status": "QUEUED",
        "conclusion": None, "startedAt": "0001-01-01T00:00:00Z", "detailsUrl": "",
        "workflowName": "",
    }
    github.pulls["7"]["statusCheckRollup"].append(check)
    run = {
        "id": 80, "name": check["name"], "head_sha": github.head, "status": "queued",
        "conclusion": None, "started_at": None, "details_url": "",
        "app": {"slug": "dependabot"}, "check_suite": {"id": 90},
    }
    suite = {
        "id": 90, "head_sha": github.head, "status": "queued", "conclusion": None,
        "created_at": _stamp(3600), "updated_at": _stamp(3600),
        "app": {"slug": "dependabot"},
    }
    github.api[f"{_REPO}/commits/{github.head}/check-runs?per_page=100"] = {"check_runs": [run]}
    github.api[f"{_REPO}/check-suites/90"] = suite
    return check, run, suite


def _ignored_notes(github: GitHub, kind: str = "pending") -> list[dict[str, Any]]:
    pending = github.workflow.repo / f".local/workflow/ignored-checks.{kind}"
    return [json.loads(path.read_text()) for path in pending.glob("*.json")]


@pytest.mark.parametrize("started", [None, "0001-01-01T00:00:00Z"])
def test_an_outside_check_that_never_started_does_not_hold_the_merge(
    github: GitHub, started: str | None,
) -> None:
    check, _, _ = _outside_check(github)
    check["startedAt"] = started
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    line = "ignored never-started check: .github/dependabot.yml from dependabot on pull request 7"
    assert line in done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert [note["line"] for note in _ignored_notes(github)] == [line]
    assert not github.workflow.calls("gh api -X POST")
    assert not github.workflow.calls("gh run rerun")
    assert _origin(github) == github.head


@pytest.mark.parametrize("case", [
    "recent", "in progress", "failed", "started", "REST started", "REST running",
    "REST failed", "own app", "unknown app", "unknown creation", "recent update",
    "different head", "different suite head", "ambiguous name", "invalid start", "status context",
])
def test_an_outside_check_is_ignored_only_with_evidence_it_never_started(
    github: GitHub, case: str,
) -> None:
    check, run, suite = _outside_check(github)
    if case == "recent":
        suite.update(created_at=_stamp(60), updated_at=_stamp(60))
    elif case == "in progress":
        check.update(status="IN_PROGRESS", startedAt=_stamp(3600))
    elif case == "failed":
        check.update(status="COMPLETED", conclusion="FAILURE")
    elif case == "started":
        check["startedAt"] = _stamp(3600)
    elif case == "REST started":
        run["started_at"] = _stamp(3600)
    elif case == "REST running":
        run["status"] = "in_progress"
    elif case == "REST failed":
        run.update(status="completed", conclusion="failure")
    elif case == "own app":
        run["app"] = suite["app"] = {"slug": "github-actions"}
    elif case == "unknown app":
        run["app"] = {}
    elif case == "unknown creation":
        suite["created_at"] = None
    elif case == "recent update":
        suite["updated_at"] = _stamp(60)
    elif case == "different head":
        run["head_sha"] = github.base
    elif case == "different suite head":
        suite["head_sha"] = github.base
    elif case == "ambiguous name":
        github.api[f"{_REPO}/commits/{github.head}/check-runs?per_page=100"]["check_runs"].append(
            {**run, "id": 81, "app": {"slug": "github-actions"}}
        )
    elif case == "invalid start":
        check["startedAt"] = "unreadable"
    elif case == "status context":
        check.update(__typename="StatusContext", context="external", state="PENDING")
    done = github.run()
    assert done.returncode == 1, done.stdout + done.stderr
    if case == "failed":
        assert "merge refused:" in done.stderr
        assert "failed checks" in done.stderr
    else:
        assert "merge waiting:" in done.stderr
        assert "check is not passing:" in done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert _ignored_notes(github) == []
    assert _origin(github) == github.head


def test_ignoring_an_outside_check_keeps_own_lost_check_recovery(github: GitHub) -> None:
    _outside_check(github)
    _lose(github)
    done = github.run()
    assert done.returncode == 2, done.stdout + done.stderr
    assert "GitHub lost CI: server tests of run 20" in done.stdout
    assert _origin(github) != github.head
    assert not (github.workflow.state / "merged").exists()


def test_an_own_queued_check_past_the_limit_is_still_recovered(github: GitHub) -> None:
    _lose(github)
    check = github.pulls["7"]["statusCheckRollup"][-1]
    check.update(status="QUEUED", startedAt="0001-01-01T00:00:00Z")
    done = github.run()
    assert done.returncode == 2, done.stdout + done.stderr
    assert "GitHub lost CI: server tests of run 20" in done.stdout
    assert _ignored_notes(github) == []


def test_the_train_uses_the_same_outside_check_rule_and_keeps_json_output(github: GitHub) -> None:
    _outside_check(github)
    github.save()
    line_up(github, ["7"])
    train = train_status(github)
    assert train["first"] == "7"
    saved = json.loads((github.workflow.repo / ".local/workflow/merge-train.json").read_text())
    assert saved["pending"] == []
    assert len(_ignored_notes(github)) == 1


def test_the_outside_check_wait_uses_the_configured_queue_limit(github: GitHub) -> None:
    _outside_check(github)
    path = github.workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config.setdefault("merge_train", {})["queued_limit_minutes"] = 120
    path.write_text(json.dumps(config))
    done = github.run()
    assert done.returncode == 1, done.stdout + done.stderr
    assert "check is not passing:" in done.stderr
    assert _ignored_notes(github) == []


def test_every_ignored_check_is_named_and_check_run_pages_are_read(github: GitHub) -> None:
    check, run, _ = _outside_check(github)
    github.pulls["7"]["statusCheckRollup"].append({**check, "name": "dependency policy"})
    other = {**run, "id": 81, "name": "dependency policy"}
    github.api[f"{_REPO}/commits/{github.head}/check-runs?per_page=100"] = {
        "_pages": [{"check_runs": [run]}, {"check_runs": [other]}],
    }
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert {note["check"] for note in _ignored_notes(github)} == {
        ".github/dependabot.yml", "dependency policy",
    }


def test_an_outside_check_is_reported_again_on_a_new_head(github: GitHub) -> None:
    _outside_check(github)
    assert github.run().returncode == 0
    previous = github.head
    github.workflow.git("commit", "--allow-empty", "-m", "fix: next head")
    github.workflow.git("push", "origin", "HEAD:ticket/seven")
    github.head = github.workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.pulls["7"]["statusCheckRollup"] = [
        {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    github.ci(github.base, github.head, run_id=50)
    _outside_check(github)
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert {note["head"] for note in _ignored_notes(github)} == {previous, github.head}


def test_an_unreadable_outside_check_suite_never_allows_a_merge(github: GitHub) -> None:
    _outside_check(github)
    github.api[f"{_REPO}/check-suites/90"] = "HTTP 502: unavailable"
    done = github.run()
    assert done.returncode == 1, done.stdout + done.stderr
    assert "HTTP 502" in done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert _ignored_notes(github) == []


def test_an_own_failure_is_refused_even_if_outside_check_metadata_is_unreadable(github: GitHub) -> None:
    _outside_check(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    github.api[f"{_REPO}/check-suites/90"] = "HTTP 502: unavailable"
    done = github.run()
    assert done.returncode == 1, done.stdout + done.stderr
    assert "merge refused:" in done.stderr
    assert "failed checks" in done.stderr
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("in_tick", [False, True], ids=["direct", "tick"])
def test_an_outside_check_that_started_after_planning_still_blocks_the_merge(
    github: GitHub, in_tick: bool,
) -> None:
    two_stale_pulls(github)
    github.ci(github.base, github.head)
    _, run, _ = _outside_check(github)
    # The rollup still says queued. Only the first REST read says never started;
    # every later read reports the start, including the merge's own fresh read.
    running = {**run, "status": "in_progress", "started_at": _stamp(60)}
    github.api[f"{_REPO}/commits/{github.head}/check-runs?per_page=100"] = {
        "_alternate": [{"check_runs": [run]}, *[{"check_runs": [running]} for _ in range(20)]],
    }
    line_up(github, ["7", "8"])
    github.save()
    done = github.workflow.run_script(
        "merge-when-current", "7", env=_tick_context(github) if in_tick else None,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert "merge waiting: check is not passing:" in done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert len(github.workflow.calls(
        f"gh api --paginate {_REPO}/commits/{github.head}/check-runs?per_page=100"
    )) >= 2


def test_an_ignored_check_is_told_once_per_head_and_delivery_is_durable(github: GitHub) -> None:
    workflow = github.workflow
    _outside_check(github)
    for _ in range(2):
        done = github.run()
        assert done.returncode == 0, done.stdout + done.stderr
    assert len(_ignored_notes(github)) == 1
    workflow.orchestrator("busy")
    (workflow.state / "send-fails").touch()
    now = int(time.time())
    assert workflow.run_script("wake", env={"WORKFLOW_NOW": str(now)}).returncode == 1
    assert len(_ignored_notes(github)) == 1
    (workflow.state / "send-fails").unlink()
    assert workflow.run_script("wake", env={"WORKFLOW_NOW": str(now + 300)}).returncode == 0
    assert _ignored_notes(github) == []
    assert len(_ignored_notes(github, "sent")) == 1
    assert "ignored never-started check: .github/dependabot.yml from dependabot on pull request 7" in workflow.sent()[-1]
    assert github.run().returncode == 0
    assert _ignored_notes(github) == []


# A run that GitHub never dispatched needs recovery before there is a completed check.
_ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")


def _never_started(github: GitHub, *, run_id: int = 20, workflow: str = "ci") -> dict[str, Any]:
    run = {
        "id": run_id, "head_sha": github.head, "head_branch": "ticket/seven",
        "event": "pull_request", "status": "queued", "conclusion": None,
        "path": f".github/workflows/{workflow}.yml", "run_attempt": 1,
        "created_at": _stamp(3600),
    }
    for status in _ACTIVE:
        github.api.setdefault(
            f"{_REPO}/actions/runs?status={status}&per_page=100", {"workflow_runs": []}
        )
    github.api[f"{_REPO}/actions/runs?status=queued&per_page=100"]["workflow_runs"].append(run)
    github.api[f"{_REPO}/actions/runs/{run_id}"] = run
    github.api[f"{_REPO}/actions/runs/{run_id}/cancel"] = {}
    github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"] = {"jobs": []}
    return run


def _recover_queue(github: GitHub):
    github.save()
    snapshot = github.workflow.state / "queued-pulls.json"
    snapshot.write_text(json.dumps(github.pulls))
    return github.workflow.run_python(
        "tick_queued_runs.py", str(github.workflow.repo / ".local/workflow"), str(snapshot)
    )


def _queue_notes(github: GitHub) -> list[dict[str, Any]]:
    folder = github.workflow.repo / ".local/workflow/never-started.pending"
    return [json.loads(path.read_text()) for path in folder.glob("*.json")]


def _stuck_retry(github: GitHub, run: dict[str, Any]) -> None:
    attempt = run["run_attempt"]
    assert _recover_queue(github).returncode == 0
    run.update(status="completed", conclusion="cancelled")
    # GitHub can keep showing the cancelled first attempt after accepting its rerun.
    for _ in range(3):
        assert _recover_queue(github).returncode == 0
    run.update(status="queued", conclusion=None, run_attempt=attempt + 1)
    github.api[f"{_REPO}/actions/runs/{run['id']}/jobs?per_page=100"] = {
        "jobs": [{"id": 201, "run_attempt": attempt + 1, "status": "queued", "created_at": _stamp(3600)}]
    }


def _queue_worktree(github: GitHub) -> Path:
    workflow = github.workflow
    workflow.git("checkout", "main")
    worktree = workflow.repo / ".claude/worktrees/ticket-7"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    workflow.git("worktree", "add", str(worktree), "ticket/seven")
    return worktree


@pytest.mark.parametrize("suite", ["ci", "e2e"])
def test_a_never_started_run_is_cancelled_by_api_and_rerun_once(github: GitHub, suite: str) -> None:
    run = _never_started(github, workflow=suite)
    for _ in range(2):
        done = _recover_queue(github)
        assert done.returncode == 0, done.stderr
    assert len(github.workflow.calls(f"gh api -X POST {_REPO}/actions/runs/20/cancel")) == 1
    assert not github.workflow.calls("gh run cancel")
    run.update(status="completed", conclusion="cancelled")
    for _ in range(2):
        done = _recover_queue(github)
        assert done.returncode == 0, done.stderr
    assert len(github.workflow.calls("gh run rerun 20 ")) == 1
    assert len(_queue_notes(github)) == 1


@pytest.mark.parametrize("attempt", [1, 3])
def test_a_stuck_retry_gets_one_empty_commit_from_its_clean_worktree(github: GitHub, attempt: int) -> None:
    worktree = _queue_worktree(github)
    run = _never_started(github)
    run["run_attempt"] = attempt
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"] = {
        "jobs": [{"id": 21, "run_attempt": attempt, "status": "queued", "created_at": _stamp(3600)}]
    }
    _stuck_retry(github, run)
    before = github.head
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    pushed = _origin(github)
    assert pushed != before
    assert github.workflow.git("rev-parse", f"{pushed}^") == before
    assert github.workflow.git("rev-parse", f"{pushed}^{{tree}}") == github.workflow.git(
        "rev-parse", f"{before}^{{tree}}"
    )
    assert github.workflow.git("log", "-1", "--format=%s", cwd=worktree) == (
        "ci: run CI again, GitHub never started CI of run 20"
    )
    assert _recover_queue(github).returncode == 0
    assert _origin(github) == pushed


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending", "requested"])
def test_a_busy_repository_does_not_cancel_a_never_started_run(github: GitHub, status: str) -> None:
    run = _never_started(github)
    other = {**run, "id": 19, "created_at": _stamp(7200), "status": status,
             "head_branch": "ticket/other", "head_sha": github.base}
    github.api[f"{_REPO}/actions/runs?status={status}&per_page=100"]["workflow_runs"].append(other)
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert not github.workflow.calls("gh api -X POST")
    assert not _queue_notes(github)


@pytest.mark.parametrize("job", [
    {"id": 21, "status": "completed", "conclusion": "success"},
    {"id": 21, "status": "queued", "started_at": "2026-01-01T00:00:00Z"},
    {"id": 21, "status": "in_progress"},
])
def test_any_started_job_prevents_queue_recovery(github: GitHub, job: dict[str, Any]) -> None:
    _never_started(github)
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"] = {"jobs": [job]}
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert not github.workflow.calls("gh api -X POST")


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_a_never_started_main_run_is_only_reported(github: GitHub, event: str) -> None:
    run = _never_started(github)
    run.update(head_branch="main", event=event, head_sha=github.base)
    for _ in range(2):
        done = _recover_queue(github)
        assert done.returncode == 0, done.stderr
    assert not github.workflow.calls("gh api -X POST")
    assert not github.workflow.calls("gh run rerun")
    assert _origin(github) == github.head
    [note] = _queue_notes(github)
    assert "main" in note["line"]


def test_a_second_stuck_run_on_the_same_head_is_refused_and_reported(github: GitHub) -> None:
    _queue_worktree(github)
    first = _never_started(github)
    _stuck_retry(github, first)
    assert _recover_queue(github).returncode == 0
    pushed = _origin(github)
    first.update(status="completed", conclusion="cancelled")
    second = _never_started(github, run_id=30, workflow="e2e")
    _stuck_retry(github, second)
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert _origin(github) == pushed
    assert any("not pushed for twice" in note["line"] for note in _queue_notes(github))


def test_a_new_retry_wait_is_timed_from_its_own_jobs(github: GitHub) -> None:
    _queue_worktree(github)
    run = _never_started(github)
    _stuck_retry(github, run)
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"][0]["created_at"] = _stamp(60)
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert _origin(github) == github.head


def test_queue_recovery_is_called_by_the_tick(github: GitHub) -> None:
    _never_started(github)
    github.save()
    workflow = github.workflow
    workflow.list_pull_request(7, draft=False, labels=())
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {github.head}\n")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls(f"gh api -X POST {_REPO}/actions/runs/20/cancel")) == 1


@pytest.mark.parametrize("change", ["head", "branch", "draft", "closed", "event", "workflow"])
def test_queue_recovery_requires_a_current_open_pull_run(github: GitHub, change: str) -> None:
    run = _never_started(github)
    if change == "head":
        run["head_sha"] = github.base
    elif change == "branch":
        run["head_branch"] = "ticket/other"
    elif change == "draft":
        github.pulls["7"]["isDraft"] = True
    elif change == "closed":
        github.pulls["7"]["state"] = "CLOSED"
    elif change == "event":
        run["event"] = "workflow_dispatch"
    else:
        run["path"] = ".github/workflows/images.yml"
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert not github.workflow.calls("gh api -X POST")


def test_a_dirty_worktree_is_not_used_for_queue_recovery(github: GitHub) -> None:
    worktree = _queue_worktree(github)
    (worktree / "shared.txt").write_text("uncommitted\n")
    run = _never_started(github)
    _stuck_retry(github, run)
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert _origin(github) == github.head
    assert (worktree / "shared.txt").read_text() == "uncommitted\n"
    assert any("uncommitted" in note["line"] for note in _queue_notes(github))


def test_missing_queue_timestamps_start_a_durable_observation(github: GitHub) -> None:
    run = _never_started(github)
    run.pop("created_at")
    assert _recover_queue(github).returncode == 0
    assert not github.workflow.calls("gh api -X POST")
    path = github.workflow.repo / ".local/workflow/never-started-runs.json"
    state = json.loads(path.read_text())
    state["runs"]["20"]["observed"]["1"] -= 3600
    path.write_text(json.dumps(state))
    assert _recover_queue(github).returncode == 0
    assert len(github.workflow.calls(f"gh api -X POST {_REPO}/actions/runs/20/cancel")) == 1


def test_queue_cancellation_lost_to_an_outage_can_retry(github: GitHub) -> None:
    _never_started(github)
    endpoint = f"{_REPO}/actions/runs/20/cancel"
    github.api[endpoint] = "HTTP 502: Bad Gateway"
    assert _recover_queue(github).returncode == 1
    github.api[endpoint] = {}
    assert _recover_queue(github).returncode == 0
    assert _recover_queue(github).returncode == 0
    assert len(github.workflow.calls(f"gh api -X POST {endpoint}")) == 2


@pytest.mark.parametrize("operation", ["cancel", "rerun"])
def test_an_accepted_recovery_with_a_lost_response_is_reconciled(github: GitHub, operation: str) -> None:
    _queue_worktree(github)
    run = _never_started(github)
    endpoint = f"{_REPO}/actions/runs/20/cancel"
    if operation == "cancel":
        github.api[endpoint] = "HTTP 502: Bad Gateway"
        assert _recover_queue(github).returncode == 1
    else:
        assert _recover_queue(github).returncode == 0
    run.update(status="completed", conclusion="cancelled")
    if operation == "rerun":
        (github.workflow.state / "rerun-unavailable").write_text("HTTP 502: Bad Gateway")
        assert _recover_queue(github).returncode == 1
    else:
        github.api[endpoint] = {}
        assert _recover_queue(github).returncode == 0
    # The request reached GitHub, whose next response reveals the accepted new attempt.
    run.update(status="queued", conclusion=None, run_attempt=2)
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"] = {
        "jobs": [{"id": 201, "run_attempt": 2, "status": "queued", "created_at": _stamp(3600)}]
    }
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert _origin(github) != github.head


def test_an_unreadable_queue_never_authorizes_cancellation(github: GitHub) -> None:
    _never_started(github)
    github.api[f"{_REPO}/actions/runs?status=in_progress&per_page=100"] = "HTTP 403: Forbidden"
    assert _recover_queue(github).returncode == 1
    assert not github.workflow.calls("gh api -X POST")


def test_queue_recovery_leaves_while_the_merge_lock_is_held(github: GitHub) -> None:
    _never_started(github)
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert _recover_queue(github).returncode == 0
    assert not github.workflow.calls("gh api -X POST")


def test_equal_queue_timestamps_do_not_make_two_stuck_runs_block_each_other(github: GitHub) -> None:
    first = _never_started(github)
    second = _never_started(github, run_id=30, workflow="e2e")
    second["created_at"] = first["created_at"]
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert len(github.workflow.calls(f"gh api -X POST {_REPO}/actions/runs/20/cancel")) == 1
    assert not github.workflow.calls(f"gh api -X POST {_REPO}/actions/runs/30/cancel")


def test_a_manual_retry_does_not_jump_a_job_waiting_before_that_attempt(github: GitHub) -> None:
    run = _never_started(github)
    run["run_attempt"] = 3
    github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"] = {
        "jobs": [{"id": 21, "run_attempt": 3, "status": "queued", "created_at": _stamp(1800)}]
    }
    other = _never_started(github, run_id=30, workflow="e2e")
    other["created_at"] = _stamp(2700)
    done = _recover_queue(github)
    assert done.returncode == 0, done.stderr
    assert not github.workflow.calls(f"gh api -X POST {_REPO}/actions/runs/20/cancel")


def _cancelled_recovery(handed: GitHub) -> dict[str, Any]:
    run = _never_started(handed)
    handed.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={handed.head}&event=pull_request&per_page=100"
    ]["workflow_runs"] = [run]
    for workflow in ("images", "e2e"):
        handed.api[
            f"{_REPO}/actions/workflows/{workflow}.yml/runs?head_sha={handed.head}&event=pull_request&per_page=100"
        ] = {"workflow_runs": []}
    assert _recover_queue(handed).returncode == 0
    run.update(status="completed", conclusion="cancelled")
    handed.pulls["7"]["statusCheckRollup"] = [{
        "__typename": "CheckRun", "name": "CI result", "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "detailsUrl": "https://github.com/example-org/example-app/actions/runs/20/job/21",
    }]
    return run


@pytest.mark.parametrize("stage", [
    "cancelled", "rerun metadata", "retry queued", "retry passed", "long retry passed", "passed between polls",
])
def test_recovery_cancellation_keeps_the_passed_review_handoff(handed: GitHub, stage: str) -> None:
    run = _cancelled_recovery(handed)
    if stage != "cancelled":
        assert _recover_queue(handed).returncode == 0
    if stage == "retry queued":
        run.update(status="queued", conclusion=None, run_attempt=2)
    elif "passed" in stage:
        run.update(status="completed", conclusion="success", run_attempt=2)
        handed.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"] = {"jobs": [{
            "id": 221, "name": "CI result", "status": "completed", "conclusion": "success",
            "run_attempt": 2,
        }]}
        if stage != "retry passed":
            path = handed.workflow.repo / ".local/workflow/never-started-runs.json"
            data = json.loads(path.read_text())
            data["runs"]["20"]["requested_at"] -= 86400
            path.write_text(json.dumps(data))
        if stage == "passed between polls":
            run["updated_at"] = _stamp(1)
        else:
            assert _recover_queue(handed).returncode == 0
    handed.save()
    done = handed.workflow.run_python("merge_driver.py")
    assert done.returncode == 0, done.stdout + done.stderr
    folder = handed.workflow.repo / ".local/workflow"
    assert (folder / "merge-wanted-7").exists(), done.stdout + done.stderr
    assert not (folder / "merge-review-7").exists(), done.stdout + done.stderr
    assert not (handed.workflow.state / "merged").exists()
    assert not any(" new-session " in call for call in handed.workflow.calls("tmux"))


@pytest.mark.parametrize("problem", ["expired", "head", "run", "attempt", "failure", "check failure", "abandoned", "passed expired"])
def test_recovery_pending_does_not_hide_other_failures(handed: GitHub, problem: str) -> None:
    run = _cancelled_recovery(handed)
    path = handed.workflow.repo / ".local/workflow/never-started-runs.json"
    if problem == "passed expired":
        assert _recover_queue(handed).returncode == 0
        run.update(status="completed", conclusion="success", run_attempt=2)
        handed.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"] = {"jobs": [{
            "id": 221, "name": "CI result", "status": "completed", "conclusion": "success",
            "run_attempt": 2,
        }]}
        assert _recover_queue(handed).returncode == 0
    data = json.loads(path.read_text())
    entry = data["runs"]["20"]
    if problem == "expired":
        entry["requested_at"] -= 86400
    elif problem == "head":
        entry["head"] = handed.base
    elif problem == "run":
        data["runs"]["999"] = data["runs"].pop("20")
    elif problem == "attempt":
        run["run_attempt"] = 2
    elif problem == "failure":
        run["conclusion"] = "failure"
    elif problem == "check failure":
        handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    elif problem == "passed expired":
        entry["finished_at"] -= 86400
    else:
        entry["state"] = "refused"
    path.write_text(json.dumps(data))
    handed.save()
    done = handed.workflow.run_python("merge_driver.py")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (path.parent / "merge-wanted-7").exists(), done.stdout + done.stderr
    assert (path.parent / "merge-review-7").exists(), done.stdout + done.stderr


def test_an_empty_commit_keeps_its_handoff_while_github_shows_the_old_head(handed: GitHub) -> None:
    run = _never_started(handed)
    _stuck_retry(handed, run)
    assert _recover_queue(handed).returncode == 0
    published = _origin(handed)
    assert published != handed.head
    handed.save()
    for _ in range(2):
        done = handed.workflow.run_python("merge_driver.py")
        assert done.returncode == 0, done.stdout + done.stderr
        folder = handed.workflow.repo / ".local/workflow"
        note = folder / "merge-wanted-7"
        assert note.exists(), done.stdout + done.stderr
        assert json.loads(note.read_text())["head"] == published
        assert not (folder / "merge-review-7").exists()


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


# Before the gate: a reviewer or audit that waits for its checks and has not joined the line.


def _run(
    github: GitHub,
    run_id: int = 20,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    finished_ago: float = 3600,
) -> None:
    """What GitHub says of the workflow run itself."""
    github.api[f"{_REPO}/actions/runs/{run_id}"] = {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "updated_at": _stamp(finished_ago),
    }
    github.save()


def _waiting_audit(github: GitHub) -> None:
    """An audit waiting for its checks; its pull request has no `workflow` label."""
    workflow = github.workflow
    workflow.running("audit-7")
    _waiting_reviewer(github)
    (workflow.notes(7) / "review-7.started").unlink()
    (workflow.notes(7) / "review-7.checks-wait").unlink()
    (workflow.notes(7) / "audit-7.checks-wait").touch()
    workflow.list_pull_request(7, draft=False, labels=())
    workflow.screen("● Waiting for checks.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n", "audit-7")


def _run_reads(github: GitHub, run_id: int = 20) -> int:
    return sum(
        call.endswith(f"/actions/runs/{run_id}") for call in github.workflow.calls("gh api")
    )


def _wake_log(github: GitHub) -> str:
    log = Path(github.workflow.env["HOME"]) / ".local/state/workflow/wake.log"
    return log.read_text() if log.exists() else ""


def test_a_reviewer_before_the_gate_is_woken_once_per_head_for_a_lost_check(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    assert train_status(github)["waiting"] == []
    sent = workflow.calls("tmux review-7 send-keys")
    assert len(sent) == 2, sent  # literal text and Enter, once
    assert "GitHub lost a check on " + github.head in sent[0]
    assert (workflow.repo / f".local/workflow/review-7.lost-nudged-{github.head}").exists()
    assert f"review-7 lost check on {github.head}; reviewer nudged" in _wake_log(github)


def test_a_new_head_with_a_lost_check_wakes_the_reviewer_again(github: GitHub) -> None:
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-7 send-keys")) == 2

    new = workflow.git("commit-tree", f"{github.head}^{{tree}}", "-p", github.head, "-m", "next")
    github.head = new
    github.pulls["7"]["headRefOid"] = new
    github.pulls["7"]["statusCheckRollup"] = [
        {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    github.ci(github.base, new, run_id=50)
    _lose(github, run_id=50)
    _run(github, 50)
    assert workflow.run_script("tick").returncode == 0

    sent = workflow.calls("tmux review-7 send-keys")
    assert len(sent) == 4, sent
    assert "GitHub lost a check on " + new in sent[2]


def test_an_unlabelled_audit_before_the_gate_is_woken_once_for_a_lost_check(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_audit(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    sent = workflow.calls("tmux audit-7 send-keys")
    assert len(sent) == 2, sent
    assert "GitHub lost a check on " + github.head in sent[0]
    assert workflow.calls("tmux review-7 send-keys") == []
    assert f"audit-7 lost check on {github.head}; reviewer nudged" in _wake_log(github)


def test_the_orchestrator_hears_of_a_lost_check_before_the_gate_once(github: GitHub) -> None:
    workflow = github.workflow
    workflow.orchestrator("busy")
    _lose(github)
    _run(github)
    _waiting_reviewer(github)

    assert workflow.run_script("tick").returncode == 0
    assert workflow.run_script("tick").returncode == 0

    told = [prompt for prompt in workflow.sent() if "lost check:" in prompt]
    assert len(told) == 1, workflow.sent()
    assert "\nlost check: CI: server tests of run 20 on pull request 7\n" in told[0]
    sent = workflow.repo / ".local/workflow/lost-checks.sent"
    assert [path.name for path in sent.iterdir()] == [f"7-{github.head}.json"]


@pytest.mark.parametrize(
    ("case", "reads"),
    [
        ("still running", 1),
        ("its run finished just now", 1),
        ("it started just now", 0),
    ],
)
def test_a_check_not_lost_before_the_gate_wakes_nobody(
    github: GitHub, case: str, reads: int
) -> None:
    workflow = github.workflow
    if case == "it started just now":
        _lose(github, finished_ago=60)
    else:
        _lose(github)
    if case == "still running":
        _run(github, status="in_progress", conclusion=None)
    elif case == "its run finished just now":
        _run(github, finished_ago=60)
    else:
        _run(github)
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert list(workflow.repo.glob(".local/workflow/review-7.*-nudged-*")) == []
    assert _lost_notes(github) == []
    assert _run_reads(github) == reads


def test_a_reviewer_waiting_for_the_owner_is_not_woken_for_a_lost_check(github: GitHub) -> None:
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)
    (workflow.notes(7) / "review-7.owner-blocked").touch()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert _run_reads(github) == 0


@pytest.mark.parametrize("readable", [True, False])
def test_a_run_is_read_once_per_tick_before_the_gate(github: GitHub, readable: bool) -> None:
    """A reviewer and an audit wait on the same pull request: one read of the run serves both."""
    workflow = github.workflow
    _lose(github)
    _lose(github, name="CI: web")  # a second lost check of the same run
    _run(github)
    if not readable:
        github.api[f"{_REPO}/actions/runs/20"] = None
        github.save()
    _waiting_audit(github)
    (workflow.notes(7) / "review-7.started").touch()
    (workflow.notes(7) / "review-7.checks-wait").touch()
    workflow.list_pull_request(7, draft=False)

    first = workflow.run_script("tick")

    assert _run_reads(github) == 1, workflow.calls("gh api")
    woken = [workflow.calls(f"tmux {name} send-keys") for name in ("review-7", "audit-7")]
    if readable:
        assert first.returncode == 0, first.stdout + first.stderr
        assert [len(sent) for sent in woken] == [2, 2]
    else:
        assert first.returncode == 1
        assert "GitHub unavailable" in first.stderr
        assert woken == [[], []]
        # The cache lived for that tick only: the next tick asks again, once.
        workflow.run_script("tick")
        assert _run_reads(github) == 2



def test_the_runs_are_read_as_the_ticks_background_work(github: GitHub) -> None:
    """Not as a session's: a read never waits out the reserve while the tick holds its lock."""
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)

    assert workflow.run_script("tick").returncode == 0
    assert _run_reads(github) == 1
    log = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget/requests.jsonl"
    programs = {json.loads(line)["program"] for line in log.read_text().splitlines()}
    assert "sessions" not in programs, programs


@pytest.mark.parametrize("kind", ["review", "audit"])
def test_a_new_session_on_the_same_head_is_woken_for_a_lost_check_again(
    workflow: Workflow, kind: str
) -> None:
    """The mark of an earlier session's wake is not this one's."""
    workflow.add_worktree(7, draft=False)
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    mark = folder / f"{kind}-7.lost-nudged-{'a' * 40}"
    mark.touch()

    done = workflow.run("start", kind, "7")

    assert done.returncode == 0, done.stderr
    assert not mark.exists()


def test_a_helper_that_fails_fails_the_tick(github: GitHub) -> None:
    """Only its own answer says no check was lost; a crash is a failure, never "none"."""
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)
    # The helper crashes, as on an error it does not expect.
    workflow.fake(
        "python3",
        "#!/usr/bin/env bash\n"
        'if [[ $2 == */tick_lost_check.py ]]; then echo "Traceback: broken" >&2; exit 1; fi\n'
        f'exec {sys.executable} "$@"\n',
    )

    done = workflow.run_script("tick")

    assert done.returncode == 1, done.stdout + done.stderr
    assert "Traceback: broken" in done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []


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
    # The red run the line read for its verdict is not read again; a failed check alone is not
    # a run read, so its lost check's run is.
    reads = [call for call in workflow.calls("gh api") if call.endswith("/actions/runs/20")]
    assert bool(reads) == (shown is _failed_beside_lost), reads

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
    """Its run cannot have completed ten minutes ago: it is not read, and the lost check
    beside it wakes nobody."""
    workflow = github.workflow
    _failed_beside_lost(github)
    github.pulls["7"]["statusCheckRollup"].append(
        {
            "__typename": "CheckRun",
            "name": "E2E: browser tests (1 of 2)",
            "status": "IN_PROGRESS",
            "conclusion": None,
            "startedAt": _stamp(60),
            "detailsUrl": "https://github.com/example-org/example-app/actions/runs/40/job/41",
        }
    )
    github.save()
    line_up(github, ["7"])
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert train_status(github)["lost"] == {}
    assert _lost_notes(github) == []
    assert not [call for call in workflow.calls("gh api") if call.endswith("/actions/runs/40")]


def _audit_of_a_new_head(github: GitHub) -> str:
    """7 was blocked for a failed check; its audit waits for the checks of the fix it pushed."""
    blocked_first(github)
    workflow = github.workflow
    waiting_reviewers(github)
    workflow.list_pull_request(7, draft=False, labels=())
    workflow.running("audit-7")
    (workflow.state / "tmux/review-7").unlink()
    workflow.screen("● Waiting for checks.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n", "audit-7")
    (workflow.notes(7) / "audit-7.checks-wait").touch()
    workflow.git("commit", "--allow-empty", "-m", "fix: failed check")
    refreshed_first(github)
    workflow.set_pull_request(7, "OPEN", github.head)
    return github.head


def _shown_to_the_tick(github: GitHub) -> None:
    github.save()
    (github.workflow.state / "rollup-7.json").write_text(
        json.dumps(github.pulls["7"]["statusCheckRollup"])
    )


@pytest.mark.parametrize("rollup_failed", [True, False], ids=["failed-check", "red-run"])
def test_a_blocked_members_new_head_with_a_lost_check_wakes_its_audit_once(
    github: GitHub, rollup_failed: bool
) -> None:
    workflow = github.workflow
    head = _audit_of_a_new_head(github)
    run = _ci_run(github, head)
    run.update(conclusion="failure", updated_at=_stamp(3600))
    github.api[f"{_REPO}/actions/runs/50"] = run
    if rollup_failed:
        github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    else:
        github.pulls["7"]["statusCheckRollup"] = []
    _lose(github, run_id=50)
    _shown_to_the_tick(github)
    pushed = _origin(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    sent = workflow.calls("tmux audit-7 send-keys")
    assert len(sent) == 2, sent
    assert f"The checks on {head} have finished" in sent[0]
    assert [(note["head"], note["run"]) for note in _lost_notes(github)] == [(head, 50)]
    assert _origin(github) == pushed


def test_a_blocked_members_new_head_green_beside_a_lost_check_is_not_woken_as_failed(
    github: GitHub,
) -> None:
    """A green run's lost check is the gate's to run again, not a failure."""
    workflow = github.workflow
    head = _audit_of_a_new_head(github)
    _ci_run(github, head)["updated_at"] = _stamp(3600)
    github.api[f"{_REPO}/actions/runs/50"] = _ci_run(github, head)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    _lose(github, run_id=50)
    _shown_to_the_tick(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux audit-7 send-keys") == []
    assert train_status(github)["lost"] == {}


@pytest.mark.parametrize("shown", [_red_with_its_job_lost, _failed_beside_lost])
def test_a_reviewer_before_the_gate_is_woken_as_finished_for_a_failed_run_with_a_lost_check(
    github: GitHub, shown: Any
) -> None:
    """Not run again: its checks failed, so it is woken as if they had finished."""
    workflow = github.workflow
    head = github.head
    shown(github)
    _waiting_reviewer(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    assert train_status(github)["waiting"] == []
    sent = workflow.calls("tmux review-7 send-keys")
    assert len(sent) == 2, sent  # literal text and Enter, once
    assert f"The checks on {head} have finished" in sent[0]
    assert (workflow.repo / f".local/workflow/review-7.checks-nudged-{head}").exists()
    assert not (workflow.repo / f".local/workflow/review-7.lost-nudged-{head}").exists()
    assert f"review-7 checks finished on {head}; reviewer nudged" in _wake_log(github)
    assert [(note["head"], note["check"], note["run"]) for note in _lost_notes(github)] == [
        (head, "CI: server tests", 20)
    ]
    assert _origin(github) == head


def test_an_unlabelled_audit_before_the_gate_is_woken_as_finished_for_a_failed_lost_check(
    github: GitHub,
) -> None:
    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    _waiting_audit(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    sent = workflow.calls("tmux audit-7 send-keys")
    assert len(sent) == 2, sent
    assert f"The checks on {head} have finished" in sent[0]
    assert len(_lost_notes(github)) == 1


def test_a_failed_check_beside_one_still_running_before_the_gate_wakes_nobody(
    github: GitHub,
) -> None:
    workflow = github.workflow
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
    _run(github, 40, status="in_progress", conclusion=None)
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert list(workflow.repo.glob(".local/workflow/review-7.*-nudged-*")) == []
    assert _lost_notes(github) == []


def test_a_lost_check_of_a_passed_run_beside_another_failed_check_wakes_as_finished(
    github: GitHub,
) -> None:
    """The pull request failed by its other check: its lost one is not run again."""
    workflow = github.workflow
    head = github.head
    _lose(github)
    _run(github)
    github.pulls["7"]["statusCheckRollup"].append(
        {"name": "CI: web", "status": "COMPLETED", "conclusion": "FAILURE"}
    )
    github.save()
    _waiting_reviewer(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    sent = workflow.calls("tmux review-7 send-keys")
    assert len(sent) == 2, sent
    assert f"The checks on {head} have finished" in sent[0]
    assert (workflow.repo / f".local/workflow/review-7.checks-nudged-{head}").exists()
    assert not (workflow.repo / f".local/workflow/review-7.lost-nudged-{head}").exists()
    assert [note["run"] for note in _lost_notes(github)] == [20]
    assert _run_reads(github) == 1
    assert _origin(github) == head


def test_a_lost_check_beside_a_check_that_did_not_start_is_searched_as_main_before_the_gate(
    github: GitHub,
) -> None:
    """Not a failure by the gate's own reading, so its gate would not run CI again either."""
    workflow = github.workflow
    _lose(github)
    _run(github)
    github.pulls["7"]["statusCheckRollup"].append(
        {"name": "CI: web", "status": "COMPLETED", "conclusion": "STARTUP_FAILURE"}
    )
    github.save()
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert _lost_notes(github) == []
    assert _run_reads(github) == 0
