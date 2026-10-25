"""Merge safety for train."""

import fcntl
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow

from workflow.merge_gate_fixtures import (
    GitHub,
    _DEPENDENCIES,
    _REPO,
    _blocked_first,
    _green_rerun_on_old_main,
    _hot,
    _images,
    _labelled,
    _line,
    _main_image_run,
    _members_changing,
    _pr_job,
    _refreshed_first,
    _rerun_failed_jobs,
    _train,
    _two_stale_pulls,
    _waiting_reviewers,
    gate,
    github,
)


def test_only_the_front_of_the_train_refreshes_and_the_next_runs_after_merge(
    github: GitHub,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    before = workflow.git("rev-parse", "HEAD", cwd=second)
    waiting = workflow.run_script("merge-when-current", "8", cwd=second)
    assert waiting.returncode == 3, waiting.stdout + waiting.stderr
    assert "notify waiting" in waiting.stdout
    assert workflow.git("rev-parse", "HEAD", cwd=second) == before
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/eight").split()[0] == before
    _refreshed_first(github)
    assert github.run().returncode == 0
    github.pulls["7"]["state"] = "MERGED"
    github.save()
    next_pull = workflow.run_script("merge-when-current", "8", cwd=second)
    assert next_pull.returncode == 2, next_pull.stdout + next_pull.stderr
    assert workflow.git("rev-parse", "HEAD^2", cwd=second) == github.base


def test_a_failed_front_leaves_the_train_and_rejoins_at_the_back_after_a_fix(
    github: GitHub,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    github.save()
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 2
    assert github.run().returncode == 1
    workflow.git("commit", "--allow-empty", "-m", "fix: failed check")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"].update(headRefOid=github.head, statusCheckRollup=[])
    assert github.run().returncode == 3


def test_the_train_survives_restarting_the_tick(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    status = workflow.run_script("merge-when-current", "--train-status")
    assert status.returncode == 0, status.stderr
    train = json.loads(status.stdout)
    assert train["first"] == "7"
    assert train["waiting"] == ["7", "8"]
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3


def test_the_tick_reads_the_saved_train_while_a_gate_holds_the_lock(github: GitHub) -> None:
    """A refresh pushes under the lock, and the push hooks take minutes."""
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    views = len(workflow.calls("gh pr view"))
    with (workflow.repo / ".local/workflow/merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = workflow.run_script("merge-when-current", "--train-status")
    assert status.returncode == 0, status.stderr
    train = json.loads(status.stdout)
    assert train["first"] == "7"
    assert train["waiting"] == ["7", "8"]
    # Nothing was read from GitHub: reconciling waits for a tick that gets the lock.
    assert len(workflow.calls("gh pr view")) == views


@pytest.mark.parametrize("outcome", ["merged", "failed"])
def test_the_tick_wakes_the_next_turn_once_even_if_its_checks_already_woke_it(
    github: GitHub,
    outcome: str,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    done = workflow.run_script(
        "merge-when-current", "8", cwd=second, env={"WORKFLOW_SESSION": "review-8"}
    )
    assert done.returncode == 3, done.stderr
    assert (workflow.notes(8) / "review-8.waiting").exists()
    # The previous checks nudge sent the reviewer to the gate, where it had to wait.
    head = github.pulls["8"]["headRefOid"]
    (workflow.repo / f".local/workflow/review-8.checks-nudged-{head}").touch()
    assert workflow.run_script("tick").returncode == 0
    assert workflow.calls("tmux review-8 send-keys") == []
    if outcome == "merged":
        github.pulls["7"]["state"] = "MERGED"
    else:
        github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    github.save()
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    second_tick = workflow.run_script("tick")
    assert second_tick.returncode == 0, second_tick.stdout + second_tick.stderr
    assert len(workflow.calls("tmux review-8 send-keys")) == 2  # literal text and Enter


def test_the_front_is_woken_again_when_main_finishes_its_checks(github: GitHub) -> None:
    _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    main_run = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"][0]
    main_run.update(status="in_progress", conclusion=None)
    assert github.run().returncode == 2
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-7 send-keys")) == 2
    main_run.update(status="completed", conclusion="success")
    github.save()
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-7 send-keys")) == 4


@pytest.mark.parametrize("change", ["closed", "draft", "stacked", "unreadable"])
def test_train_reconciliation_releases_ineligible_pulls_but_fails_closed_on_read_errors(
    github: GitHub,
    change: str,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    if change == "closed":
        github.pulls["7"]["state"] = "CLOSED"
    elif change == "draft":
        github.pulls["7"]["isDraft"] = True
    elif change == "stacked":
        github.pulls["7"]["baseRefName"] = "ticket/other"
    else:
        del github.pulls["7"]
    github.save()
    before = workflow.git("rev-parse", "HEAD", cwd=second)
    done = workflow.run_script("merge-when-current", "8", cwd=second)
    if change == "unreadable":
        assert done.returncode == 1
        assert workflow.git("rev-parse", "HEAD", cwd=second) == before
    else:
        assert done.returncode == 2, done.stderr


def test_a_new_reviewer_does_not_inherit_the_previous_reviewers_train_nudge(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7, draft=False)
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    mark = folder / "review-7.train-nudged-1-main-health-head"
    mark.touch()
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    assert not mark.exists()


@pytest.mark.parametrize("main_red", [True, False])
@pytest.mark.parametrize("proof", ["fresh", "partial", "skipped", "stale", "unreadable"])
def test_a_proven_fix_at_the_back_takes_the_red_main_turn(
    github: GitHub, main_red: bool, proof: str
) -> None:
    _two_stale_pulls(github)
    workflow = github.workflow
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    if proof == "stale":
        github.ci(github.base, head, tested_base=workflow.git("rev-parse", "main^"), run_id=30)
    if proof != "partial":
        github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
            {
                "id": 39,
                "name": "CI: web",
                "status": "completed",
                "conclusion": "skipped" if proof == "skipped" else "success",
            }
        )
    if proof == "unreadable":
        github.api[f"{_REPO}/check-runs/31/annotations?per_page=100"] = None
    github.main("failure" if main_red else "success")
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert workflow.run_script("main-status").returncode == 0
    train = _train(github)
    fixed = main_red and proof == "fresh"
    assert train["first"] == ("8" if fixed else "7")
    # The waiting are listed in the order served, the fix first.
    assert train["waiting"] == (["8", "7"] if fixed else ["7", "8"])
    assert _train(github)["turn"] == train["turn"]
    # A separate status process also recovers the original order after main goes green.
    github.main()
    github.save()
    assert workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == "7"
    assert _train(github)["waiting"] == ["7", "8"]


@pytest.mark.parametrize("warned", [True, False])
def test_a_pass_only_over_mains_advisory_does_not_take_the_red_main_turn(
    github: GitHub, warned: bool
) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": _DEPENDENCIES, "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = (
        [{"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}]
        if warned
        else []
    )
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": _DEPENDENCIES, "status": "completed", "conclusion": "failure"},
    ]
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    # The gate would not merge a pass over main's own advisory as the fix, so it is not first.
    assert _train(github)["first"] == ("7" if warned else "8")


def test_a_red_main_judges_each_pull_request_once_until_its_checks_change(
    github: GitHub,
) -> None:
    _two_stale_pulls(github)
    workflow = github.workflow
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    # Pull request 7's own CI has not passed: nothing about it can prove a fix.
    github.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    github.main("failure")
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))

    def reads(run: int) -> int:
        # The proof reads which merge a run tested. The first member's turn reads it too.
        return len(workflow.calls(f"gh api --paginate {_REPO}/check-runs/{run + 1}/annotations"))

    def first_and_reads(run: int) -> tuple[str, int]:
        before = reads(run)
        first = _train(github)["first"]
        return first, reads(run) - before

    assert workflow.run_script("main-status").returncode == 0
    assert reads(20) == 0
    first, judged = first_and_reads(30)
    assert first == "8"
    # The tick reads the train every 3 minutes; GitHub's shared budget pays for each read.
    assert first_and_reads(30) == ("8", judged - 1)
    assert reads(20) == 0
    # A changed check on the pull request is new evidence, judged again.
    github.pulls["8"]["statusCheckRollup"].append(
        {"name": "CI: web", "status": "COMPLETED", "conclusion": "SUCCESS"}
    )
    github.save()
    assert first_and_reads(30) == ("8", judged)


@pytest.mark.parametrize(
    "labels", [{}, {"7": ["priority: low"], "8": ["priority: high"]}], ids=["none", "labelled"]
)
def test_multiple_red_main_fixes_keep_their_original_queue_order(
    github: GitHub, labels: dict[str, list[str]]
) -> None:
    """Every proven fix counts as the first blocker, whatever its label."""
    _two_stale_pulls(github)
    _labelled(github, labels)
    github.ci(github.base, github.head)
    _pr_job(github, "CI: web", "success")
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    github.main("failure")
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == "7"
    assert json.loads((folder / "merge-train.json").read_text())["fixes"] == ["7", "8"]


@pytest.mark.parametrize(
    "labels,served,priorities",
    [
        # A later high goes before an earlier medium.
        ({"8": ["workflow", "priority: high"]}, ["8", "7"], {"7": "medium", "8": "high"}),
        # Within one priority, the order of joining.
        (
            {"7": ["priority: high"], "8": ["priority: high"]},
            ["7", "8"],
            {"7": "high", "8": "high"},
        ),
        # No label counts as medium: before a low, after a high.
        ({"7": ["priority: low"]}, ["8", "7"], {"7": "low", "8": "medium"}),
        ({"7": ["priority: medium"], "8": []}, ["7", "8"], {"7": "medium", "8": "medium"}),
        # A blocker before a high, whichever joined first.
        (
            {"7": ["priority: high"], "8": ["priority: blocker"]},
            ["8", "7"],
            {"7": "high", "8": "blocker"},
        ),
    ],
)
def test_the_train_serves_by_priority_then_by_the_order_of_joining(
    github: GitHub, labels: dict[str, list[str]], served: list[str], priorities: dict[str, str]
) -> None:
    _two_stale_pulls(github)
    _labelled(github, labels)
    _line(github, ["7", "8"])

    train = _train(github)

    assert train["first"] == served[0]
    assert train["waiting"] == served
    assert train["priorities"] == priorities
    # The order of joining is kept, so a priority that changes back restores it.
    saved = json.loads((github.workflow.repo / ".local/workflow/merge-train.json").read_text())
    assert saved["waiting"] == ["7", "8"]


@pytest.mark.parametrize("hot", [True, False], ids=["hot path", "no hot paths"])
def test_members_sharing_a_hot_path_are_served_back_to_back(github: GitHub, hot: bool) -> None:
    """9 is refreshed once on 7's squash instead of conflicting with it after 8's."""
    if hot:
        _hot(github, "web/src/pages/**")
    _members_changing(
        github,
        {
            "7": "web/src/pages/menu.tsx",
            "8": "src/example_app/other.py",
            "9": "web/src/pages/settings.tsx",
            "10": "web/src/pages/menu.tsx",
        },
    )

    train = _train(github)

    assert train["waiting"] == (["7", "9", "10", "8"] if hot else ["7", "8", "9", "10"])
    if hot:
        assert train["hot_paths"] == {
            "7": "web/src/pages/**",
            "9": "web/src/pages/**",
            "10": "web/src/pages/**",
        }
    else:
        assert "hot_paths" not in train
    # The order of joining is kept, so a member leaving the group restores it.
    saved = json.loads((github.workflow.repo / ".local/workflow/merge-train.json").read_text())
    assert saved["waiting"] == ["7", "8", "9", "10"]
    # Without hot paths the line is saved as before.
    assert ("hot_paths" in saved) == hot


def test_a_hot_path_never_moves_a_member_across_priorities(github: GitHub) -> None:
    _hot(github, "web/src/pages/**")
    _members_changing(
        github,
        {
            "7": "web/src/pages/menu.tsx",
            "8": "src/example_app/other.py",
            "9": "web/src/pages/settings.tsx",
        },
    )
    _labelled(github, {"7": ["priority: high"], "8": ["priority: high"]})

    train = _train(github)

    # 9 is medium: it shares 7's hot path but stays behind every high member.
    assert train["waiting"] == ["7", "8", "9"]
    assert train["hot_paths"] == {}


@pytest.mark.parametrize("failing", ["ls-remote", "fetch"])
def test_a_stalled_github_is_waited_for_once_when_hot_paths_are_read(
    gate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failing: str
) -> None:
    """Each read of GitHub may take minutes under the merge lock: thirty members with new
    heads wait for one stalled read, not one each."""
    monkeypatch.setattr(gate, "CONFIG", replace(gate.CONFIG, hot_paths=("web/src/pages/**",)))
    numbers = [str(100 + index) for index in range(30)]
    train = gate.Train(tmp_path)
    train.queue = numbers
    pulls = {number: {"headRefOid": f"{int(number):040x}"} for number in numbers}
    calls: list[str] = []

    def git(*args: str) -> str:
        calls.append(args[0])
        if args[0] == failing:
            raise gate.Waiting(f"git {args[0]} timed out")
        return "f" * 40 + "\trefs/heads/main"

    monkeypatch.setattr(gate, "git", git)
    # No member's head is in the local store yet: each would be fetched.
    monkeypatch.setattr(
        gate, "command", lambda *args, **_: subprocess.CompletedProcess(args, 1, "", "")
    )

    train.read_hot_paths(pulls)

    assert calls.count(failing) == 1
    # Unread, each is served as without hot paths and read again at the next turn.
    assert train.hot == {}


def test_a_member_whose_new_head_leaves_the_hot_path_is_served_by_joining_again(
    github: GitHub,
) -> None:
    _hot(github, "web/src/pages/**")
    _members_changing(
        github,
        {
            "7": "web/src/pages/menu.tsx",
            "8": "src/example_app/other.py",
            "9": "web/src/pages/settings.tsx",
        },
    )
    assert _train(github)["waiting"] == ["7", "9", "8"]

    # 9 drops its change to the page: its new head changes another file.
    nine = github.workflow.root / "nine"
    github.workflow.git("rm", "--quiet", "web/src/pages/settings.tsx", cwd=nine)
    (nine / "docs.md").write_text("docs\n")
    github.workflow.git("add", "docs.md", cwd=nine)
    github.workflow.git("commit", "--quiet", "-m", "fix: docs only", cwd=nine)
    head = github.workflow.git("rev-parse", "HEAD", cwd=nine)
    github.workflow.git("push", "--quiet", "origin", "HEAD:ticket/nine", cwd=nine)
    github.pulls["9"]["headRefOid"] = head
    github.ci(github.base, head, run_id=90)
    github.save()

    assert _train(github)["waiting"] == ["7", "8", "9"]


def test_a_label_changed_while_waiting_moves_the_pull_request_at_the_next_turn(
    github: GitHub,
) -> None:
    _two_stale_pulls(github)
    _labelled(github, {"8": ["priority: high"]})
    _line(github, ["7", "8"])
    before = _train(github)
    assert before["first"] == "8"

    _labelled(github, {"8": ["priority: low"]})
    after = _train(github)

    assert after["first"] == "7"
    assert after["waiting"] == ["7", "8"]
    assert after["priorities"]["8"] == "low"
    assert after["turn"] == before["turn"] + 1


def test_the_fix_of_a_red_main_is_first_whatever_its_label(github: GitHub) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    github.main("failure")
    _labelled(github, {"7": ["priority: blocker"], "8": ["priority: low"]})
    _line(github, ["7", "8"])
    assert github.workflow.run_script("main-status").returncode == 0

    train = _train(github)

    assert train["first"] == "8"
    assert train["waiting"] == ["8", "7"]
    # The status shows the priority it is served at, not its label.
    assert train["priorities"] == {"8": "blocker", "7": "blocker"}


def test_a_green_member_behind_a_pending_more_urgent_one_takes_the_turn(github: GitHub) -> None:
    """Behind a pending member means behind it in the order served, not in the order joined."""
    _two_stale_pulls(github)
    github.ci(github.base, github.head)  # 7: green, tested on current main.
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    runs = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
    github.api[runs]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    _labelled(github, {"8": ["priority: high"]})
    _line(github, ["7", "8"])

    train = _train(github)

    assert train["first"] == "7"
    # 8 keeps its place at the front of the line while its checks run.
    assert train["waiting"] == ["8", "7"]


def test_a_later_high_pull_request_is_served_before_an_earlier_medium_one(
    github: GitHub,
) -> None:
    """The gate itself: the earlier medium one waits while the later high one refreshes."""
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _labelled(github, {"8": ["workflow", "priority: high"]})
    assert github.run().returncode == 2  # 7 is alone in the line and refreshes.
    _refreshed_first(github)
    refreshed = workflow.git("rev-parse", "HEAD")

    joined = workflow.run_script("merge-when-current", "8", cwd=second)
    waiting = github.run()

    assert joined.returncode == 2, joined.stdout + joined.stderr
    assert "CI requested" in joined.stdout
    assert waiting.returncode == 3, waiting.stdout + waiting.stderr
    assert "waits behind 8" in waiting.stdout
    assert not (workflow.state / "merged").exists()
    assert workflow.git("rev-parse", "HEAD") == refreshed


@pytest.mark.parametrize("proof", ["fresh", "wrong-tree", "skipped"])
def test_an_image_fix_at_the_back_takes_the_red_main_turn(github: GitHub, proof: str) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    _main_image_run(github, 60, github.base, conclusion="failure")
    original_head = github.head
    github.head = head
    _images(github, tree="f" * 40 if proof == "wrong-tree" else None)
    github.head = original_head
    github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"].append(
        {
            "id": 44,
            "name": "Docker images: browser tests (1 of 2)",
            "status": "completed",
            "conclusion": "skipped" if proof == "skipped" else "success",
        }
    )
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == ("8" if proof == "fresh" else "7")


def test_an_image_fix_whose_failed_jobs_alone_were_rerun_takes_the_red_main_turn(
    github: GitHub,
) -> None:
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    _main_image_run(github, 60, github.base, conclusion="failure")
    original_head = github.head
    github.head = head
    _images(github)
    github.head = original_head
    github.api[f"{_REPO}/actions/runs/40/jobs?per_page=100"]["jobs"].append(
        {
            "id": 44,
            "name": "Docker images: browser tests (1 of 2)",
            "status": "completed",
            "conclusion": "success",
        }
    )
    _rerun_failed_jobs(github, 40, head, "Docker images: what to run")
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert _train(github)["first"] == "8"


def test_the_tick_wakes_a_red_main_fix_ahead_of_the_queue(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": "CI: web", "status": "completed", "conclusion": "success"}
    )
    github.main("failure")
    github.save()
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    # Both reviewers already received their checks wake; only the new turn should wake one.
    for number in ("7", "8"):
        (folder / f"review-{number}.checks-nudged-{github.pulls[number]['headRefOid']}").touch()
        (workflow.notes(int(number)) / f"review-{number}.waiting").touch()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _train(github)["first"] == "8"
    assert len(workflow.calls("tmux review-8 send-keys")) == 2
    assert workflow.calls("tmux review-7 send-keys") == []
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-8 send-keys")) == 2
    merged = workflow.run_script("merge-when-current", "8", cwd=second)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert (workflow.state / "merged").read_text() == "8"


def test_a_front_that_cannot_fix_red_main_lets_the_next_candidate_try(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.main("failure")
    refused = github.run()
    assert refused.returncode == 1
    assert "merging stopped: main: red" in refused.stderr
    assert not (workflow.state / "merged").exists()
    next_pull = workflow.run_script("merge-when-current", "8", cwd=second)
    assert next_pull.returncode == 2, next_pull.stdout + next_pull.stderr


def test_a_red_main_candidate_that_joins_later_can_take_the_turn(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    assert github.run().returncode == 2
    _refreshed_first(github)
    github.main("failure")
    assert github.run().returncode == 1
    next_pull = github.workflow.run_script("merge-when-current", "8", cwd=second)
    assert next_pull.returncode == 2, next_pull.stdout + next_pull.stderr


def test_red_main_candidates_sleep_until_main_or_their_head_changes(github: GitHub) -> None:
    _two_stale_pulls(github)
    assert github.run().returncode == 2
    _refreshed_first(github)
    github.main("failure")
    assert github.run().returncode == 1
    workflow = github.workflow
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] is None
    github.main()
    github.save()
    assert workflow.run_script("main-status").returncode == 0
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] == "7"
    assert github.run().returncode == 0


@pytest.mark.parametrize("outcome", ["merged", "failed", "self-failed"])
def test_an_unlabelled_audit_is_woken_when_its_train_turn_arrives(
    github: GitHub,
    outcome: str,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    workflow.list_pull_request(8, draft=False, labels=())
    workflow.running("audit-8")
    (workflow.state / "tmux/review-8").unlink()
    workflow.screen("● Waiting for checks.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n", "audit-8")
    assert github.run().returncode == 2
    waiting = workflow.run_script(
        "merge-when-current", "8", cwd=second, env={"WORKFLOW_SESSION": "audit-8"}
    )
    assert waiting.returncode == 3
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Waiting."}, "audit-8")
    assert (workflow.notes(8) / "audit-8.checks-wait").exists()
    assert workflow.run_script("tick").returncode == 0
    assert workflow.calls("tmux audit-8 send-keys") == []
    if outcome == "merged":
        github.pulls["7"]["state"] = "MERGED"
    elif outcome == "failed":
        github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    else:
        github.pulls["8"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        rollup = workflow.state / "rollup-8.json"
        checks = json.loads(rollup.read_text())
        checks[0]["conclusion"] = "FAILURE"
        rollup.write_text(json.dumps(checks))
    github.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux audit-8 send-keys")) == 2
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux audit-8 send-keys")) == 2


@pytest.mark.parametrize(
    "block", ["owner-blocked", "interaction-blocked", "pushed", "stopped", "limit"]
)
def test_an_unavailable_front_yields_and_recovers_its_old_place(
    github: GitHub,
    block: str,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    if block == "stopped":
        marker = workflow.state / "tmux/review-7"
        marker.unlink()
        github.api[f"{_REPO}/pulls/7/reviews"] = []
        github.save()
    elif block == "limit":
        # Listed as `limit` by its own line on the screen; the default screen comes back after.
        marker = workflow.state / "screen/review-7"
        workflow.screen(
            "  ⎿  You've hit your session limit\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n",
            "review-7",
        )
    else:
        marker = workflow.notes(7) / f"review-7.{block}"
        marker.touch()
    if block != "stopped":
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert len(workflow.calls("tmux review-8 send-keys")) == 2
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] == "8"
    refreshed = workflow.run_script("merge-when-current", "8", cwd=second)
    assert refreshed.returncode == 2, refreshed.stderr
    if block == "stopped":
        marker.touch()
    else:
        marker.unlink()
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] == "7"
    assert json.loads(status.stdout)["waiting"] == ["7", "8"]


def test_a_conflicted_front_lets_the_next_refresh_and_returns_after_resolution(
    github: GitHub,
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    shared = workflow.repo / "shared.txt"
    shared.write_text("branch edit\n")
    workflow.git("commit", "-am", "fix: branch edit")
    github.head = workflow.git("rev-parse", "HEAD")
    workflow.git("checkout", "main")
    shared.write_text("main edit\n")
    workflow.git("commit", "-am", "fix: main edit")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.pulls["7"].update(headRefOid=github.head, mergeable="CONFLICTING")
    github.main()
    refused = github.run()
    assert refused.returncode == 1
    assert "resolve the conflicts" in refused.stderr
    refreshed = workflow.run_script("merge-when-current", "8", cwd=second)
    assert refreshed.returncode == 2, refreshed.stdout + refreshed.stderr
    status = workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["first"] == "8"
    shared.write_text("resolved\n")
    workflow.git("add", "shared.txt")
    workflow.git("commit", "--no-edit")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.pulls["7"].update(mergeable="MERGEABLE")
    _refreshed_first(github)
    assert github.run().returncode == 0


@pytest.mark.parametrize("block", ["owner-blocked", "interaction-blocked", "pushed"])
def test_the_session_calling_the_gate_is_not_waiting_for_the_owner(
    github: GitHub,
    block: str,
) -> None:
    # A permission dialog answered at the machine, or a wake by the session's own job, leaves
    # a note behind while the session runs the gate itself.
    notes = github.workflow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"review-7.{block}").touch()

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").exists()


def test_an_audit_with_a_new_fix_still_gets_its_checks_finished_wake(github: GitHub) -> None:
    _blocked_first(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    workflow.list_pull_request(7, draft=False, labels=())
    workflow.running("audit-7")
    (workflow.state / "tmux/review-7").unlink()
    workflow.screen("● Waiting for checks.\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n", "audit-7")
    (workflow.notes(7) / "audit-7.checks-wait").touch()
    workflow.git("commit", "--allow-empty", "-m", "fix: failed check")
    _refreshed_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    workflow.set_pull_request(7, "OPEN", github.head)
    github.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux audit-7 send-keys")) == 2
    # Its new fix still rejoins at the back when the audit calls the gate.
    assert github.run().returncode == 3


def test_a_green_rerun_recovers_the_blocked_heads_old_place(github: GitHub) -> None:
    _blocked_first(github)
    assert _train(github)["first"] == "8"
    # A rerun updates the same run id and head, rather than requesting a new commit.
    github.ci(github.base, github.head, run_id=50)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    github.save()
    train = _train(github)
    assert train["blocked"] == []
    assert train["waiting"] == ["7", "8"]
    assert train["first"] == "7"
    before = github.workflow.git("rev-parse", "HEAD")
    assert github.run().returncode == 0
    assert github.workflow.git("rev-parse", "HEAD") == before


def test_a_green_rerun_on_old_main_stays_blocked_until_refreshed(github: GitHub) -> None:
    _green_rerun_on_old_main(github)
    workflow = github.workflow
    stale = _train(github)
    assert stale["blocked"] == ["7"]
    assert _train(github)["turn"] == stale["turn"]
    done = github.run()
    assert done.returncode == 2, done.stdout + done.stderr
    assert not (workflow.state / "merged").exists()
    assert workflow.git("rev-parse", "HEAD^2") == github.base
    # While the refreshed head's CI runs, it holds its place like any other refresh.
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ] = {"workflow_runs": []}
    github.save()
    assert _train(github)["first"] == "7"
    second = workflow.root / "second"
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    again = github.run()
    assert again.returncode == 1
    assert "has not started" in again.stderr and "failed checks" not in again.stderr
    _refreshed_first(github)
    refreshed = _train(github)
    assert refreshed["blocked"] == []
    assert refreshed["waiting"] == ["7", "8"]
    assert refreshed["first"] == "7"


@pytest.mark.parametrize("result", ["failure", "pending", "unreadable", "new-failure"])
def test_a_blocked_head_needs_a_readable_green_latest_run(github: GitHub, result: str) -> None:
    _blocked_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    if result == "unreadable":
        github.api[f"{_REPO}/actions/runs/50/jobs?per_page=100"] = None
    elif result == "pending":
        github.ci(github.base, github.head, run_id=50)
        github.api[
            f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}"
            "&event=pull_request&per_page=100"
        ]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    elif result == "new-failure":
        github.ci(github.base, github.head, run_id=50)
        github.save()
        assert _train(github)["first"] == "7"
        endpoint = (
            f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.head}"
            "&event=pull_request&per_page=100"
        )
        old_run = github.api[endpoint]["workflow_runs"][0]
        github.api[endpoint]["workflow_runs"].append({**old_run, "id": 60, "conclusion": "failure"})
        github.api[f"{_REPO}/actions/runs/60/jobs?per_page=100"] = github.api[
            f"{_REPO}/actions/runs/50/jobs?per_page=100"
        ]
    github.save()
    train = _train(github)
    assert train["blocked"] == ["7"]
    assert train["first"] == "8"
    assert train["waiting"] == ["8"]  # The tick can still deliver the failure's checks wake.
    # A session that calls the gate meanwhile is told a green re-run also lifts the block.
    refused = github.run()
    assert refused.returncode == 1
    assert "once a re-run is green" in refused.stderr


def test_a_green_rerun_whose_refresh_conflicts_keeps_its_place(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    shared = workflow.repo / "shared.txt"
    shared.write_text("branch edit\n")
    workflow.git("commit", "-am", "fix: branch edit")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    _refreshed_first(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    github.api[f"{_REPO}/actions/runs/50/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"
    assert github.run().returncode == 1
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 2
    old = github.base
    workflow.git("checkout", "main")
    shared.write_text("main edit\n")
    workflow.git("commit", "-am", "fix: main edit")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old, run_id=50)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    github.pulls["7"].update(mergeable="CONFLICTING", mergeStateStatus="DIRTY")
    refused = github.run()
    assert refused.returncode == 1
    assert "resolve the conflicts in shared.txt" in refused.stderr
    shared.write_text("resolved\n")
    workflow.git("add", "shared.txt")
    workflow.git("commit", "--no-edit")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.pulls["7"].update(mergeable="MERGEABLE", mergeStateStatus="CLEAN")
    # The resolution is the refresh, not a new fix: it keeps the place ahead of 8.
    _refreshed_first(github)
    train = _train(github)
    assert train["blocked"] == []
    assert train["first"] == "7"
    assert github.run().returncode == 0


@pytest.mark.parametrize(
    ("mergeable", "merge_state"), [("CONFLICTING", "UNKNOWN"), ("UNKNOWN", "DIRTY")]
)
def test_github_conflict_yields_before_a_gate_attempt_and_wakes_once_per_head(
    github: GitHub, mergeable: str, merge_state: str
) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.pulls["7"].update(mergeable=mergeable, mergeStateStatus=merge_state)
    # Conflicts can prevent CI from starting at all.
    github.pulls["7"]["statusCheckRollup"] = []
    github.save()
    (workflow.state / "rollup-7.json").write_text("[]")
    (workflow.state / "mergeability-7").write_text(f"{mergeable} {merge_state}")
    workflow.set_pull_request(7, "OPEN", github.head)

    assert _train(github)["first"] == "8"
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    wakes = workflow.calls("tmux review-7 send-keys")
    assert len(wakes) == 2
    assert f"GitHub reports a merge conflict on {github.head}" in wakes[0]
    assert "non-mechanical conflict" in wakes[0]
    assert len(workflow.calls("tmux review-8 send-keys")) == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 2

    # A newly conflicting head receives a fresh wake, even before it finishes CI.
    workflow.git("commit", "--allow-empty", "-m", "fix: conflict still present")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"]["headRefOid"] = github.head
    workflow.set_pull_request(7, "OPEN", github.head)
    github.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux review-7 send-keys")) == 4

    github.pulls["7"].update(mergeable="MERGEABLE", mergeStateStatus="CLEAN")
    github.ci(github.base, github.head, run_id=50)
    github.save()
    assert _train(github)["first"] == "7"


def test_unknown_mergeability_keeps_its_train_place_without_a_conflict_wake(github: GitHub) -> None:
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    github.pulls["7"].update(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN", statusCheckRollup=[])
    github.save()
    (workflow.state / "rollup-7.json").write_text("[]")
    (workflow.state / "mergeability-7").write_text("UNKNOWN UNKNOWN")
    assert _train(github)["first"] == "7"
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert workflow.calls("tmux review-8 send-keys") == []
    assert "not determined" in github.run().stderr
