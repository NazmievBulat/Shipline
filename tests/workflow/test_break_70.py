"""Breaking tests for pull request 70: a repaired fix whose green CI tested an older main.

Each test replays the daily flow with neutral names: pull request 7 fails its own test after its
handoff, a worker pushes a fix, CI passes on the fix and the main of then, main moves, the
fix's reviewer hands off and the tick runs. Pull request 8 waits behind 7 throughout.
"""

import json
import subprocess
import threading
import time

import pytest
from workflow.test_merge_driver import (
    GitHub,
    _behind,
    _fail,
    _folder,
    _github_answers_repairs,
    _note,
    _pass_fix_and_hand_off,
    _push_fix,
    _started,
    _train,
)
from workflow.test_merge_driver import (
    github as github_fixture,
)
from workflow.test_merge_driver import (
    handed as handed_fixture,
)

github = github_fixture
handed = handed_fixture


def _move_main(github: GitHub, message: str = "fix: main advanced") -> str:
    """Another pull request merged: main moves on the origin."""
    workflow = github.workflow
    old = github.base
    # The line's own squash may have moved main on the origin already.
    workflow.git("fetch", "origin", "main")
    workflow.git("merge", "--ff-only", "origin/main")
    workflow.git("commit", "--allow-empty", "-m", message)
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main()
    github.save()
    return old


def _repaired(handed: GitHub) -> str:
    """7 failed its own test after its handoff and went to a worker; returns the failed head."""
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _behind(handed)
    failed = handed.head
    _fail(handed)
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    assert _train(workflow)["repairing"] == {"7": failed}
    second = workflow.run_script("tick")
    assert second.returncode == 0, second.stdout + second.stderr
    assert _started(workflow) == ["ticket-7"]
    workflow.run("stop", "ticket-7")
    return failed


def _publish_head(github: GitHub, head: str) -> None:
    github.head = head
    github.pulls["7"]["headRefOid"] = head
    (github.workflow.state / "pr/7").write_text(f"OPEN ticket/seven {head}\n")


def _assert_held_in_place(
    workflow,
    tick: subprocess.CompletedProcess[str],
    started: tuple[str, ...] = ("ticket-7",),
) -> None:
    """No hand-back, its place kept, and no reviewer beyond those started before its handoff."""
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert "merge hand-back 7" not in tick.stdout, tick.stdout
    assert not list(_folder(workflow).glob("merge-review-*"))
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["blocked"]
    assert _started(workflow) == list(started)


def _assert_refreshed(workflow, fixed: str, base: str) -> None:
    """Its turn merged current main into the fix and the handoff followed the pushed head."""
    head = workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0]
    assert workflow.git("show", "-s", "--format=%P", head) == f"{fixed} {base}"
    assert json.loads(_note(workflow).read_text())["head"] == head
    assert _train(workflow)["approved"]["7"] == head


def test_main_moves_again_while_the_refreshed_head_runs_its_ci(handed: GitHub) -> None:
    """Two merges land minutes apart: one after the fix's CI, one after its refresh."""
    workflow = handed.workflow
    _repaired(handed)
    _push_fix(handed)
    fixed = handed.head
    _move_main(handed)
    _pass_fix_and_hand_off(handed)

    first = workflow.run_script("tick")

    _assert_held_in_place(workflow, first)
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert (
        workflow.git("show", "-s", "--format=%P", refreshed) == f"{fixed} {handed.base}"
    )
    tested = _move_main(handed, "fix: main advanced again")
    _publish_head(handed, refreshed)
    # Its refreshed head's CI passed on the main of the first refresh, already old.
    handed.ci(tested, refreshed, run_id=60)
    handed.save()

    second = workflow.run_script("tick")

    _assert_held_in_place(workflow, second)
    again = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert again != refreshed
    assert (
        workflow.git("show", "-s", "--format=%P", again) == f"{refreshed} {handed.base}"
    )
    assert json.loads(_note(workflow).read_text())["head"] == again
    assert _train(workflow)["approved"]["7"] == again
    assert not (workflow.state / "merged").exists()

    _publish_head(handed, again)
    handed.ci(handed.base, again, run_id=70)
    handed.save()

    merged = workflow.run_script("tick")

    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "merge hand-back 7" not in merged.stdout
    assert (workflow.state / "merged").read_text() == "7"
    assert _train(workflow)["waiting"] == ["8"]
    assert _started(workflow) == ["ticket-7"]


def test_the_fix_is_handed_off_while_its_ci_still_runs_and_main_moves(
    handed: GitHub,
) -> None:
    """The reviewer passes the fix before its CI ends; that CI then passes on the old main."""
    workflow = handed.workflow
    _repaired(handed)
    _push_fix(handed)
    fixed = handed.head
    runs = handed.api[
        "repos/example-org/example-app/actions/workflows/ci.yml/runs"
        f"?head_sha={fixed}&event=pull_request&per_page=100"
    ]
    finished = json.loads(json.dumps(runs))
    runs["workflow_runs"][0].update(status="in_progress", conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(
        status="IN_PROGRESS", conclusion=""
    )
    handed.save()
    _move_main(handed)
    _pass_fix_and_hand_off(handed)
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["repairing"]

    waiting = workflow.run_script("tick")

    _assert_held_in_place(workflow, waiting)
    handed.api[
        "repos/example-org/example-app/actions/workflows/ci.yml/runs"
        f"?head_sha={fixed}&event=pull_request&per_page=100"
    ] = finished
    handed.pulls["7"]["statusCheckRollup"][0].update(
        status="COMPLETED", conclusion="SUCCESS"
    )
    handed.save()

    done = workflow.run_script("tick")

    _assert_held_in_place(workflow, done)
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert (
        workflow.git("show", "-s", "--format=%P", refreshed) == f"{fixed} {handed.base}"
    )
    assert json.loads(_note(workflow).read_text())["head"] == refreshed


@pytest.mark.parametrize("delay", [0.0, 0.5, 1.5, 3.0])
def test_the_tick_runs_while_the_reviewer_hands_off(
    handed: GitHub, delay: float
) -> None:
    """Two calls at once: the tick's turn and the reviewer's handoff of the stale green fix."""
    workflow = handed.workflow
    _repaired(handed)
    _push_fix(handed)
    fixed = handed.head
    _move_main(handed)
    reviews = handed.api["repos/example-org/example-app/pulls/7/reviews"]
    reviews.append(
        {
            "body": "<!-- workflow-review round=2 result=passed -->\nFixed.",
            "commit_id": fixed,
        }
    )
    handed.save()

    results: dict[str, subprocess.CompletedProcess[str]] = {}

    def tick() -> None:
        results["tick"] = workflow.run_script("tick")

    racer = threading.Thread(target=tick)
    racer.start()
    time.sleep(delay)
    results["handoff"] = workflow.run_script(
        "merge-when-current", "--handoff", "7", cwd=workflow.worktree(7)
    )
    racer.join()

    handoff, raced = results["handoff"], results["tick"]
    assert raced.returncode == 0, raced.stdout + raced.stderr
    assert "merge hand-back 7" not in raced.stdout, raced.stdout
    if handoff.returncode != 0:
        # A handoff that lost the race says to run it again; the reviewer does.
        assert "run the handoff again" in handoff.stdout + handoff.stderr, (
            handoff.stdout + handoff.stderr
        )
        retried = workflow.run_script(
            "merge-when-current", "--handoff", "7", cwd=workflow.worktree(7)
        )
        assert retried.returncode == 0, retried.stdout + retried.stderr
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["repairing"]

    head = workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0]
    print(f"handoff exit {handoff.returncode}; racing tick refreshed: {head != fixed}")
    # A tick that ran before the handoff started the fix's own reviewer, the one handing off.
    reviewers = tuple(_started(workflow))
    assert reviewers in (("ticket-7",), ("ticket-7", "review-7"))
    if head == fixed:
        # The racing tick ran before the handoff landed: the reviewer ends, this tick refreshes.
        workflow.run("stop", "review-7")
        after = workflow.run_script("tick")
        _assert_held_in_place(workflow, after, reviewers)
        head = workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0]
    assert workflow.git("show", "-s", "--format=%P", head).split() == [
        fixed,
        handed.base,
    ]
    assert json.loads(_note(workflow).read_text())["head"] == head
    assert tuple(_started(workflow)) == reviewers
    _publish_head(handed, head)
    handed.ci(handed.base, head, run_id=60)
    handed.save()

    merged = workflow.run_script("tick")

    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "merge hand-back 7" not in merged.stdout, merged.stdout
    assert (workflow.state / "merged").read_text() == "7"


def test_a_handoff_behind_a_busy_merge_lock_keeps_the_place(handed: GitHub) -> None:
    """A long merge holds the lock past the handoff's fresh read; the handoff reads again."""
    workflow = handed.workflow
    _repaired(handed)
    _push_fix(handed)
    fixed = handed.head
    _move_main(handed)
    reviews = handed.api["repos/example-org/example-app/pulls/7/reviews"]
    reviews.append(
        {
            "body": "<!-- workflow-review round=2 result=passed -->\nFixed.",
            "commit_id": fixed,
        }
    )
    handed.save()
    lock = _folder(workflow) / "merge.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "8"])
    time.sleep(1)
    try:
        handoff = workflow.run_script(
            "merge-when-current", "--handoff", "7", cwd=workflow.worktree(7)
        )
    finally:
        holder.wait()

    assert handoff.returncode == 0, handoff.stdout + handoff.stderr
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["repairing"]
    assert _train(workflow)["approved"]["7"] == fixed
    after = workflow.run_script("tick")
    _assert_held_in_place(workflow, after)


def test_an_installation_upgraded_mid_repair_keeps_the_place(handed: GitHub) -> None:
    """The line was saved by a Shipline without `repairing`, its repair note still on disk."""
    workflow = handed.workflow
    _repaired(handed)
    path = _folder(workflow) / "merge-train.json"
    saved = json.loads(path.read_text())
    del saved["repairing"]
    path.write_text(json.dumps(saved))
    _push_fix(handed)
    fixed = handed.head
    _move_main(handed)
    between = workflow.run_script("tick")
    assert between.returncode == 0, between.stdout + between.stderr
    assert _train(workflow)["waiting"] == ["7", "8"]
    reviewers = tuple(_started(workflow))

    _pass_fix_and_hand_off(handed)

    assert _train(workflow)["waiting"] == ["7", "8"]
    workflow.run("stop", "review-7")
    after = workflow.run_script("tick")
    _assert_held_in_place(workflow, after, reviewers)
    _assert_refreshed(workflow, fixed, handed.base)


def test_the_reviewer_hands_off_the_same_fix_twice(handed: GitHub) -> None:
    """A reviewer that lost its turn's output calls the handoff again on the same head."""
    workflow = handed.workflow
    _repaired(handed)
    _push_fix(handed)
    _move_main(handed)
    _pass_fix_and_hand_off(handed)
    again = workflow.run_script(
        "merge-when-current", "--handoff", "7", cwd=workflow.worktree(7)
    )
    assert again.returncode == 0, again.stdout + again.stderr
    assert _train(workflow)["waiting"] == ["7", "8"]

    after = workflow.run_script("tick")

    _assert_held_in_place(workflow, after)


@pytest.mark.parametrize("ticks", [1, 3])
def test_ticks_while_the_fix_waits_for_its_reviewer_keep_the_place(
    handed: GitHub, ticks: int
) -> None:
    """The fix is green on the old main; ticks run every few minutes before its reviewer ends."""
    workflow = handed.workflow
    failed = _repaired(handed)
    _push_fix(handed)
    fixed = handed.head
    _move_main(handed)
    for _ in range(ticks):
        tick = workflow.run_script("tick")
        assert tick.returncode == 0, tick.stdout + tick.stderr
        assert "merge hand-back 7" not in tick.stdout
        assert _train(workflow)["waiting"] == ["7", "8"]
        assert _train(workflow)["blocked"] == {"7": failed}
    # The tick starts the fix's own reviewer once the worker marks it ready.
    reviewers = tuple(_started(workflow))
    assert reviewers == ("ticket-7", "review-7")
    _pass_fix_and_hand_off(handed)
    workflow.run("stop", "review-7")
    after = workflow.run_script("tick")
    _assert_held_in_place(workflow, after, reviewers)
    _assert_refreshed(workflow, fixed, handed.base)


def _runs_key(head: str) -> str:
    return (
        "repos/example-org/example-app/actions/workflows/ci.yml/runs"
        f"?head_sha={head}&event=pull_request&per_page=100"
    )


def test_github_lists_no_run_for_the_fix_yet_when_the_tick_comes(
    handed: GitHub,
) -> None:
    """GitHub lags: the fix's rollup is green but its run is not listed at the next tick."""
    workflow = handed.workflow
    _repaired(handed)
    _push_fix(handed)
    fixed = handed.head
    _move_main(handed)
    _pass_fix_and_hand_off(handed)
    listed = handed.api[_runs_key(fixed)]
    handed.api[_runs_key(fixed)] = {"workflow_runs": []}
    handed.save()

    lagging = workflow.run_script("tick")

    _assert_held_in_place(workflow, lagging)
    handed.api[_runs_key(fixed)] = listed
    handed.save()
    caught_up = workflow.run_script("tick")
    _assert_held_in_place(workflow, caught_up)
    _assert_refreshed(workflow, fixed, handed.base)


def test_the_fix_passed_by_a_rerun_of_its_failed_jobs_on_the_old_main(
    handed: GitHub,
) -> None:
    """The fix's first attempt lost a runner; only its failed jobs re-ran, on the old main."""
    workflow = handed.workflow
    _repaired(handed)
    _push_fix(handed)
    fixed = handed.head
    handed.api[_runs_key(fixed)]["workflow_runs"][0]["run_attempt"] = 2
    handed.save()
    _move_main(handed)
    _pass_fix_and_hand_off(handed)

    done = workflow.run_script("tick")

    _assert_held_in_place(workflow, done)
    _assert_refreshed(workflow, fixed, handed.base)


@pytest.mark.xfail(
    strict=True,
    reason="unchanged by this pull request: any refresh push the origin drops is handed back",
)
def test_the_refresh_push_fails_once(handed: GitHub) -> None:
    """The origin drops the refresh's push once; the next tick pushes it."""
    workflow = handed.workflow
    _repaired(handed)
    _push_fix(handed)
    fixed = handed.head
    _move_main(handed)
    _pass_fix_and_hand_off(handed)
    hooks = workflow.bin / "flaky-hooks"
    hooks.mkdir()
    marker = workflow.state / "push-dropped"
    hook = hooks / "pre-push"
    hook.write_text(
        "#!/bin/bash\n"
        f"if [[ ! -e {marker} ]]; then touch {marker}; "
        "echo 'fatal: the remote end hung up unexpectedly' >&2; exit 1; fi\n"
    )
    hook.chmod(0o755)
    workflow.git("config", "core.hooksPath", str(hooks))

    dropped = workflow.run_script("tick")

    assert dropped.returncode == 0, dropped.stdout + dropped.stderr
    assert _train(workflow)["waiting"] == ["7", "8"]
    if "merge hand-back 7" in dropped.stdout:
        pytest.fail("a dropped refresh push handed the fix back: " + dropped.stdout)
    again = workflow.run_script("tick")
    _assert_held_in_place(workflow, again)
    _assert_refreshed(workflow, fixed, handed.base)


def test_the_member_behind_merges_during_the_repair_and_the_fix_follows(
    handed: GitHub,
) -> None:
    """Main moves because 8, passed over while 7 is repaired, merges through the line."""
    workflow = handed.workflow
    failed = _repaired(handed)
    second = workflow.worktree(8)
    workflow.git("worktree", "add", str(second), "ticket/eight")
    workflow.git("commit", "--allow-empty", "-m", "feat: eight", cwd=second)
    eight = workflow.git("rev-parse", "HEAD", cwd=second)
    workflow.git("push", "origin", "HEAD:ticket/eight", cwd=second)
    handed.pulls["8"].update(headRefOid=eight, labels=[{"name": "workflow"}])
    (workflow.state / "pr/8").write_text(f"OPEN ticket/eight {eight}\n")
    workflow.list_pull_request(8, draft=False)
    handed.ci(handed.base, eight, run_id=30)
    handed.api["repos/example-org/example-app/pulls/8/reviews"] = [
        {
            "body": "<!-- workflow-review round=1 result=passed -->\nReviewed.",
            "commit_id": eight,
        }
    ]
    handed.save()
    done = workflow.run_script("merge-when-current", "--handoff", "8", cwd=second)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _train(workflow)["waiting"] == ["7", "8"]

    passed_over = workflow.run_script("tick")

    assert passed_over.returncode == 0, passed_over.stdout + passed_over.stderr
    assert (workflow.state / "merged").read_text() == "8"
    assert "merge hand-back 7" not in passed_over.stdout
    assert _train(workflow)["blocked"] == {"7": failed}
    assert _train(workflow)["repairing"] == {"7": failed}
    assert _train(workflow)["waiting"] == ["7"]
    (workflow.state / "merged").unlink()
    handed.pulls["8"]["state"] = "MERGED"
    (workflow.state / "pr/8").write_text(f"MERGED ticket/eight {eight}\n")
    handed.save()
    # The fix's CI ran on the main of before 8 merged.
    _push_fix(handed)
    fixed = handed.head
    _move_main(handed, "feat: eight (#8)")
    _pass_fix_and_hand_off(handed)

    refreshed_tick = workflow.run_script("tick")

    assert refreshed_tick.returncode == 0, refreshed_tick.stdout + refreshed_tick.stderr
    assert "merge hand-back 7" not in refreshed_tick.stdout, refreshed_tick.stdout
    assert _train(workflow)["waiting"] == ["7"]
    _assert_refreshed(workflow, fixed, handed.base)
    assert _started(workflow) == ["ticket-7"]


def test_the_worker_reruns_the_failed_head_green_on_the_old_main(
    handed: GitHub,
) -> None:
    """The worker finds the failure a flake: a green re-run on the same head, then main moves."""
    workflow = handed.workflow
    failed = _repaired(handed)
    tested = handed.base
    handed.pulls["7"].update(isDraft=False)
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    workflow.list_pull_request(7, draft=False)
    handed.ci(tested, failed, run_id=20)
    handed.api[_runs_key(failed)]["workflow_runs"][0]["run_attempt"] = 2
    handed.save()
    _move_main(handed)
    _pass_fix_and_hand_off(handed)
    assert "7" not in _train(workflow)["repairing"]
    assert _train(workflow)["waiting"] == ["7", "8"]

    done = workflow.run_script("tick")

    _assert_held_in_place(workflow, done)
    _assert_refreshed(workflow, failed, handed.base)
