"""Breaking tests for pull request 39: a passed handoff whose check failed, was re-run and passed
on an older main keeps its handoff, is refreshed at its turn and merges with no new reviewer.

Each test replays the line through real ticks: the line's state is the one the gate itself saved,
never written by hand, unless a test says it stands in for a GitHub read that failed.
"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from workflow.test_merge_driver import (  # noqa: F401
    _blocked_on_its_head,
    _commit,
    _folder,
    _mechanical_conflict,
    _note,
    _started,
    _write,
    handed,
)
from workflow.test_merge_when_current import (  # noqa: F401
    UNREACHABLE,
    GitHub,
    advisory_service_answers,
    pull_dependencies_red,
    rerun_calls,
)
from workflow.test_merge_when_current import github as github_fixture

github = github_fixture

_REPO = "repos/example-org/example-app"


_HELPER = '''"""A stand-in for a repository's advisory-service helper, which the gate imports."""
import re
import subprocess

ANNOTATION = "Advisory service unreachable"


def unreachable(output):
    found = re.search(r"Refused because (\\w+)'s advisory service could not be reached", output)
    return found[1] if found else None


def answers(service):
    done = subprocess.run(["curl", "-fsS", "--max-time", "5", "https://example.com/" + service],
                          capture_output=True, text=True, timeout=10, check=False)
    return done.returncode == 0


def down(folder):
    path = folder / "advisory-service-down"
    return {"service": path.read_text().strip()} if path.exists() else None


def note_down(folder, service):
    (folder / "advisory-service-down").write_text(service + "\\n")


def note_up(folder):
    (folder / "advisory-service-down").unlink(missing_ok=True)
'''


def _with_advisory_helper(handed: GitHub) -> None:
    """The installation holds the optional helper beside the scripts (merge_gate.py imports it)."""
    (handed.workflow.repo / "scripts" / "workflow" / "advisory_service.py").write_text(_HELPER)
    advisory_service_answers(handed.workflow, answers=True)


def _train(handed: GitHub) -> dict[str, Any]:
    return json.loads((_folder(handed.workflow) / "merge-train.json").read_text())


def _main_moves(handed: GitHub, times: int = 1) -> str:
    """Other pull requests merge: main moves on, and the pull request's run tested an older one."""
    workflow = handed.workflow
    old = handed.base
    for step in range(times):
        workflow.git("commit", "--allow-empty", "-m", f"fix: another merge {step}")
    handed.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    handed.main()
    handed.save()
    return old


def _run(handed: GitHub) -> dict[str, Any]:
    return handed.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={handed.head}&event=pull_request&per_page=100"
    ]["workflow_runs"][0]


def _rerun_running(handed: GitHub) -> None:
    """GitHub shows the re-run of the failed jobs: a second attempt, still running."""
    run = _run(handed)
    run.update(status="in_progress", conclusion=None, run_attempt=2)
    for job in handed.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"]:
        if job["conclusion"] == "failure":
            job.update(status="in_progress", conclusion=None)
    for check in handed.pulls["7"]["statusCheckRollup"]:
        if check.get("conclusion") == "FAILURE":
            check.update(status="IN_PROGRESS", conclusion=None)
    handed.save()


def _rerun_green(handed: GitHub) -> None:
    """The re-run passed, on the same tested merge as the first attempt: an older main."""
    run = _run(handed)
    run.update(status="completed", conclusion="success", run_attempt=2)
    for job in handed.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"]:
        job.update(status="completed", conclusion="success")
    for check in handed.pulls["7"]["statusCheckRollup"]:
        check.update(status="COMPLETED", conclusion="SUCCESS")
    handed.save()


def _tick(handed: GitHub, log: list[str]) -> str:
    done = handed.workflow.run_script("tick")
    log.append(done.stdout + done.stderr)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


def _kept(handed: GitHub, log: list[str]) -> None:
    workflow = handed.workflow
    assert _note(workflow).exists(), "\n----\n".join(log)
    assert "merge hand-back 7" not in log[-1], log[-1]
    assert _started(workflow) == [], "\n----\n".join(log)


def _refreshed_ci_passes(handed: GitHub) -> str:
    workflow = handed.workflow
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != handed.head
    assert workflow.git("rev-parse", "HEAD^2", cwd=workflow.worktree(7)) == handed.base
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    handed.head = refreshed
    handed.pulls["7"]["headRefOid"] = refreshed
    handed.pulls["7"]["statusCheckRollup"] = [
        {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {refreshed}\n")
    handed.ci(handed.base, refreshed, run_id=50)
    handed.save()
    return refreshed


def _until_refreshed(handed: GitHub, log: list[str], ticks: int = 3) -> None:
    workflow = handed.workflow
    for _ in range(ticks):
        if workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) != handed.head:
            return
        _tick(handed, log)
        _kept(handed, log)
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) != handed.head, (
        "never refreshed:\n" + "\n----\n".join(log)
    )


def test_a_tick_re_run_that_passes_on_an_older_main_is_refreshed_and_merged(
    handed: GitHub,
) -> None:
    """The daily flow: handed off, main moves, the audit could not reach its service, the tick
    re-runs the failed jobs once, the re-run passes on the older main; then the turn refreshes it
    and it merges, the handoff kept all along and no reviewer started."""
    workflow = handed.workflow
    log: list[str] = []
    old = _main_moves(handed, times=3)
    handed.ci(old, handed.head)
    pull_dependencies_red(handed, annotation=UNREACHABLE)
    _with_advisory_helper(handed)

    _tick(handed, log)
    _kept(handed, log)
    assert len(rerun_calls(workflow)) == 1, log[-1]
    assert "7" in _train(handed)["blocked"]

    _rerun_running(handed)
    _tick(handed, log)
    _kept(handed, log)

    _rerun_green(handed)
    _tick(handed, log)
    _kept(handed, log)
    _until_refreshed(handed, log)

    refreshed = _refreshed_ci_passes(handed)
    _tick(handed, log)

    assert (workflow.state / "merged").read_text() == "7", "\n----\n".join(log)
    assert any("--match-head-commit " + refreshed in call for call in workflow.calls("gh pr merge"))
    assert _started(workflow) == []
    assert len(rerun_calls(workflow)) == 1


def test_the_re_run_green_while_github_still_lists_the_failed_attempt(handed: GitHub) -> None:
    """A partial re-run: GitHub's rollup still shows the first attempt's failed checks for a
    while after the second attempt passed. The handoff waits, then is refreshed and merges."""
    workflow = handed.workflow
    log: list[str] = []
    old = _main_moves(handed)
    handed.ci(old, handed.head)
    pull_dependencies_red(handed, annotation=UNREACHABLE)
    _with_advisory_helper(handed)
    _tick(handed, log)
    _kept(handed, log)

    run = _run(handed)
    run.update(status="completed", conclusion="success", run_attempt=2)
    for job in handed.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"]:
        job.update(status="completed", conclusion="success")
    handed.save()  # the rollup still lists FAILURE
    _tick(handed, log)
    _kept(handed, log)

    _rerun_green(handed)
    _tick(handed, log)
    _kept(handed, log)
    _until_refreshed(handed, log)
    _refreshed_ci_passes(handed)
    _tick(handed, log)

    assert (workflow.state / "merged").read_text() == "7", "\n----\n".join(log)
    assert _started(workflow) == []


def test_a_reconcile_that_could_not_read_the_runs_does_not_drop_the_handoff(
    handed: GitHub,
) -> None:
    """The re-run passed and the line marked it refreshable; then one reconcile could not read
    its runs, which keeps it blocked and clears the mark. The next tick must not hand it back for
    what is only a refresh."""
    workflow = handed.workflow
    log: list[str] = []
    old = _main_moves(handed)
    handed.ci(old, handed.head)
    pull_dependencies_red(handed, annotation=UNREACHABLE)
    _with_advisory_helper(handed)
    _tick(handed, log)
    _rerun_green(handed)
    # Another member is first, so 7's turn does not come yet: only the driver judges it.
    _another_member_first(handed)
    _tick(handed, log)
    _kept(handed, log)
    # What a reconcile whose run read failed leaves: blocked kept, the refreshable mark cleared.
    train = _train(handed)
    assert train["blocked"].get("7") == handed.head, log[-1]
    train["refreshable"] = []
    (_folder(workflow) / "merge-train.json").write_text(json.dumps(train))

    _tick(handed, log)

    _kept(handed, log)


def _another_member_first(handed: GitHub) -> None:
    """Pull request 8 joined earlier with a higher priority and keeps the first place: its
    reviewer session is still running (not handed off), so the tick drives only 7."""
    workflow = handed.workflow
    second = workflow.worktree(8)
    if not second.exists():
        workflow.git("worktree", "add", "-b", "ticket/eight", str(second), handed.base)
        workflow.git("commit", "--allow-empty", "-m", "fix: eight", cwd=second)
        head = workflow.git("rev-parse", "HEAD", cwd=second)
        workflow.git("push", "origin", "HEAD:ticket/eight", cwd=second)
        handed.pulls["8"] = {
            **deepcopy(handed.pulls["7"]),
            "headRefOid": head,
            "headRefName": "ticket/eight",
            "labels": [{"name": "workflow"}, {"name": "priority: high"}],
            "statusCheckRollup": [
                {"name": "CI result", "status": "IN_PROGRESS", "conclusion": None}
            ],
        }
        handed.ci(handed.base, head, run_id=30)
        run = handed.api[
            f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
        ]["workflow_runs"][0]
        run.update(status="in_progress", conclusion=None)
        workflow.list_pull_request(8, draft=False)
        (workflow.state / "tmux/review-7").touch()
        workflow.running("review-8")
        (workflow.state / "tmux/review-7").unlink()
        handed.save()
    path = _folder(workflow) / "merge-train.json"
    train = json.loads(path.read_text())
    train["waiting"] = ["8", *[n for n in train["waiting"] if n != "8"]]
    train["priorities"] = {**train.get("priorities", {}), "8": "high"}
    path.write_text(json.dumps(train))


def test_a_refreshable_member_behind_others_keeps_its_handoff_tick_after_tick(
    handed: GitHub,
) -> None:
    """Ten to twenty members wait: a blocked-and-refreshable one waits its turn for several ticks
    with its handoff, then is refreshed and merges."""
    workflow = handed.workflow
    log: list[str] = []
    old = _main_moves(handed)
    handed.ci(old, handed.head)
    pull_dependencies_red(handed, annotation=UNREACHABLE)
    _with_advisory_helper(handed)
    _tick(handed, log)
    _rerun_green(handed)
    _another_member_first(handed)
    for _ in range(4):
        _tick(handed, log)
        _kept(handed, log)
        assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == handed.head
    # 8 leaves the line (merged elsewhere, or sent back): 7's turn comes.
    handed.pulls["8"]["state"] = "CLOSED"
    (workflow.state / "pr" / "8").write_text(
        f"CLOSED ticket/eight {handed.pulls['8']['headRefOid']}\n"
    )
    handed.save()
    _until_refreshed(handed, log)
    _refreshed_ci_passes(handed)
    _tick(handed, log)

    assert (workflow.state / "merged").read_text() == "7", "\n----\n".join(log)
    assert _started(workflow) == [], "\n----\n".join(log)


def test_a_re_run_that_fails_again_is_handed_back_once(handed: GitHub) -> None:
    """The re-run failed again: a real failure, handed back to one fresh reviewer."""
    workflow = handed.workflow
    log: list[str] = []
    old = _main_moves(handed)
    handed.ci(old, handed.head)
    pull_dependencies_red(handed, annotation=UNREACHABLE)
    _with_advisory_helper(handed)
    _tick(handed, log)
    _kept(handed, log)
    _run(handed).update(status="completed", conclusion="failure", run_attempt=2)
    handed.save()

    _tick(handed, log)

    assert not _note(workflow).exists(), log[-1]
    assert _started(workflow) == ["review-7"]
    assert not (workflow.state / "merged").exists()
    assert len(rerun_calls(workflow)) == 1


def _conflicting(handed: GitHub, kind: str) -> None:
    """Main moved with a change that conflicts with the branch; the branch's CI passed on the
    main it was handed off on."""
    workflow = handed.workflow
    _mechanical_conflict(handed, "list")
    branch = workflow.worktree(7)
    if kind == "prose":
        _write(branch, "docs/prose.md", "# Branch wording\n")
        _write(workflow.repo, "docs/prose.md", "# Main wording\n")
        handed.base = _commit(workflow, workflow.repo, "test: non-mechanical main wording")
        handed.main()
        handed.head = _commit(workflow, branch, "test: ambiguous branch change")
        handed.pulls["7"]["headRefOid"] = handed.head
        (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {handed.head}\n")
        note = json.loads(_note(workflow).read_text())
        note["head"] = handed.head
        _note(workflow).write_text(json.dumps(note))
    tested = workflow.git("merge-base", handed.head, handed.base)
    handed.ci(handed.base, handed.head, tested_base=tested)
    handed.save()


@pytest.mark.parametrize("blocked", [False, True], ids=["stale", "blocked and refreshable"])
@pytest.mark.parametrize("kind", ["list", "prose"])
def test_a_conflicting_refreshable_member_is_judged_as_a_plain_stale_one(
    handed: GitHub, kind: str, blocked: bool
) -> None:
    """A green re-run on an older main left it blocked and refreshable, and main then took a
    change that conflicts with the branch. It is handled as the same conflict without the block:
    not handed back as a failed check, nor kept waiting on a block nobody lifts."""
    workflow = handed.workflow
    _conflicting(handed, kind)
    if blocked:
        # Stands for the line the reconcile saved: it judges the checks of a conflicting
        # member too, so a green run on an older main is marked refreshable.
        _blocked_on_its_head(handed, refreshable=True)

    done = workflow.run_script("tick")

    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "failed checks" not in out, out
    assert "merge hand-back 7: merge refused: non-mechanical conflict in docs/list.md" in out, out
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]


@pytest.mark.xfail(
    strict=True,
    reason="low, not reached in use: the driver judges the saved line, which a reconcile left "
    "blocked and not yet refreshable (the pull request's own choice)",
)
def test_a_re_run_made_outside_the_tick_that_passes_between_two_ticks_is_kept(
    handed: GitHub,
) -> None:
    """Low, not reached in the daily flow (the tick makes the re-runs there): a job failed, a
    reconcile between two ticks (any gate call) saved the member blocked while its re-run, made
    outside the tick, was still running; the re-run then passed on an older main. Only a refresh
    is needed, but the next tick judges the saved line, in which it is blocked and not yet
    refreshable."""
    workflow = handed.workflow
    old = _main_moves(handed)
    handed.ci(old, handed.head)
    run = _run(handed)
    run.update(conclusion="failure", run_attempt=1)
    handed.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    handed.save()
    # A gate call of another session reconciles the line while 7's check shows failed.
    status = workflow.run_script("merge-when-current", "--train-status")
    assert status.returncode == 0, status.stdout + status.stderr
    assert "7" in _train(handed)["blocked"] and "7" not in _train(handed)["refreshable"]
    # The re-run, requested on GitHub, passes on the same tested merge: the older main.
    _rerun_green(handed)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "merge hand-back 7" not in done.stdout, done.stdout
    assert _note(workflow).exists()
    assert _started(workflow) == []
