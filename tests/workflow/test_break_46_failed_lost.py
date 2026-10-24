"""Breaking tests for pull request 46: a failed pull request with a check GitHub lost wakes its
reviewer. Each replays a daily flow of an installation, or a failure it meets, with the
harness of `test_lost_check.py`."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from workflow.test_lost_check import (
    _REPO,
    _ci_run,
    _failed_beside_lost,
    _lose,
    _lost_notes,
    _origin,
    _red,
    _red_with_its_job_lost,
    _stamp,
    _waiting_reviewer,
)
from workflow.test_merge_when_current import GitHub, line_up, train_status
from workflow.test_merge_when_current import github as github_fixture

github = github_fixture


def _sent(github: GitHub) -> list[str]:
    return github.workflow.calls("tmux review-7 send-keys")


def _typed(github: GitHub) -> None:
    """GitHub names every rollup entry's type; the stand-in leaves it out of its own."""
    for check in github.pulls["7"]["statusCheckRollup"]:
        check.setdefault("__typename", "CheckRun")
    github.save()


def _show(github: GitHub) -> None:
    """What the tick's own read of the pull request shows: the same rollup."""
    github.save()
    (github.workflow.state / "rollup-7.json").write_text(
        json.dumps(github.pulls["7"]["statusCheckRollup"])
    )


def _waiting_reviewer_typed(github: GitHub) -> None:
    _typed(github)
    _waiting_reviewer(github)


def _finished_wake(github: GitHub, head: str) -> None:
    sent = _sent(github)
    assert len(sent) == 2, sent  # literal text and Enter, once
    assert f"The checks on {head} have finished" in sent[0]


# 1. The loss happens while the reviewer still reads the diff: it waits for its checks with a
#    check-wait note and has not called the gate, so it is no member of the line.
@pytest.mark.parametrize("shown", [_failed_beside_lost, _red_with_its_job_lost])
def test_a_reviewer_waiting_before_any_gate_call_is_woken_for_a_failed_run_with_a_lost_check(
    github: GitHub, shown: Any
) -> None:
    workflow = github.workflow
    head = github.head
    shown(github)
    _typed(github)
    _waiting_reviewer(github)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    _finished_wake(github, head)
    assert [(note["head"], note["check"]) for note in _lost_notes(github)] == [
        (head, "CI: server tests")
    ]
    assert _origin(github) == head


def test_control_the_same_reviewer_is_woken_once_github_shows_the_check_completed(
    github: GitHub,
) -> None:
    """The setup of the test above is sound: with the lost check shown completed, the same
    waiting reviewer gets its checks-finished wake."""
    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    github.pulls["7"]["statusCheckRollup"][-1].update(status="COMPLETED", conclusion="FAILURE")
    _typed(github)
    _waiting_reviewer(github)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    _finished_wake(github, head)


# 2. The tick runs every 3 minutes: the run ended 4 minutes before the first tick. The wake comes
#    on the first tick 10 minutes after the run ended, once.
def test_ticks_every_three_minutes_wake_once_when_the_check_is_ten_minutes_lost(
    github: GitHub,
) -> None:
    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    line_up(github, ["7"])
    _typed(github)
    _waiting_reviewer(github)
    woken = []
    for minutes in (4, 7, 10.5, 13, 16):
        run = _ci_run(github, head)
        run["updated_at"] = _stamp(minutes * 60)
        github.api[f"{_REPO}/actions/runs/20"] = run
        for check in github.pulls["7"]["statusCheckRollup"]:
            if check.get("status") == "IN_PROGRESS":
                check["startedAt"] = _stamp(minutes * 60 + 300)
        _show(github)
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        woken.append(len(_sent(github)) // 2)

    assert woken == [0, 0, 1, 1, 1]
    assert len(_lost_notes(github)) == 1


# 3. A runner restarted: two server-test shards of the CI run and a browser test part of
#    another run are lost, the CI result shows the failure.
def test_several_shards_lost_across_runs_wake_once_and_note_once(github: GitHub) -> None:
    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    _lose(github, name="CI: server tests (3 of 4)")
    _lose(github, run_id=40, name="Docker images: browser tests (2 of 3)")
    github.api[f"{_REPO}/actions/runs/40"] = {
        "id": 40,
        "status": "completed",
        "conclusion": "failure",
        "updated_at": _stamp(3600),
    }
    line_up(github, ["7"])
    _typed(github)
    _waiting_reviewer(github)
    _show(github)

    for _ in range(3):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr

    _finished_wake(github, head)
    assert len(_lost_notes(github)) == 1
    assert "7" in train_status(github)["blocked"]
    assert _origin(github) == head


# 4. A job listed as queued for hours: it never started, so it has no start time.
def test_a_shard_lost_while_queued_wakes_once(github: GitHub) -> None:
    workflow = github.workflow
    head = github.head
    _red(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    _lose(github)
    lost = github.pulls["7"]["statusCheckRollup"][-1]
    lost.update(status="QUEUED", startedAt=None)
    line_up(github, ["7"])
    _typed(github)
    _waiting_reviewer(github)
    _show(github)

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr

    _finished_wake(github, head)
    assert len(_lost_notes(github)) == 1


# 5. GitHub shows the lost check late, completed, after the wake: no second wake, the line
#    forgets the lost entry, the note stays one.
def test_a_lost_check_shown_late_after_the_wake_wakes_nobody_again(github: GitHub) -> None:
    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    line_up(github, ["7"])
    _typed(github)
    _waiting_reviewer(github)
    assert workflow.run_script("tick").returncode == 0
    _finished_wake(github, head)

    github.pulls["7"]["statusCheckRollup"][-1].update(status="COMPLETED", conclusion="FAILURE")
    _show(github)
    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    _finished_wake(github, head)
    assert train_status(github)["lost"] == {}
    assert len(_lost_notes(github)) == 1


# 6. Two ticks at once (a slow one still running when the next starts): one wake.
def test_two_ticks_at_once_wake_once(github: GitHub) -> None:
    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    line_up(github, ["7"])
    _typed(github)
    _waiting_reviewer(github)
    _show(github)

    script = str(workflow.script.parent / "tick")
    procs = [
        subprocess.Popen(
            ["bash", script],
            cwd=workflow.repo,
            env=workflow.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for proc in procs:
        proc.communicate(timeout=110)

    _finished_wake(github, head)
    assert len(_lost_notes(github)) == 1


# 7. GitHub times out on the read of the lost check's run: no wake, the tick goes on; the next
#    tick reads it and wakes once.
def test_a_timed_out_run_read_is_retried_by_the_next_tick(github: GitHub) -> None:
    workflow = github.workflow
    head = github.head
    _red(github)
    github.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    _lose(github, run_id=40, name="Docker images: browser tests (1 of 3)")
    github.api[f"{_REPO}/actions/runs/40"] = "HTTP 504: Gateway Timeout"
    line_up(github, ["7"])
    _typed(github)
    _waiting_reviewer(github)
    _show(github)

    first = workflow.run_script("tick")

    assert first.returncode in (0, 1), first.stdout + first.stderr
    assert _sent(github) == []
    assert _lost_notes(github) == []

    github.api[f"{_REPO}/actions/runs/40"] = {
        "id": 40,
        "status": "completed",
        "conclusion": "failure",
        "updated_at": _stamp(3600),
    }
    _show(github)
    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    _finished_wake(github, head)
    assert len(_lost_notes(github)) == 1


# 8. The lost-check note the orchestrator reads names only neutral facts and is not world
#    writable.
def test_the_note_holds_only_the_check_run_and_head(github: GitHub) -> None:
    workflow = github.workflow
    _failed_beside_lost(github)
    line_up(github, ["7"])
    _typed(github)
    _waiting_reviewer(github)
    assert workflow.run_script("tick").returncode == 0

    [note] = (workflow.repo / ".local/workflow/lost-checks.pending").glob("*.json")
    assert set(json.loads(note.read_text())) == {"number", "head", "check", "run", "conclusion"}
    assert not note.stat().st_mode & 0o002
    assert Path(note).name == f"7-{github.head}.json"


# 9. Re-run in part after the wake: the failed jobs run again on the same head. While they run
#    nobody is woken and the line holds no lost entry; the loss is not taken for a new one.
def test_a_partial_rerun_after_the_wake_wakes_nobody_while_it_runs(github: GitHub) -> None:
    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    line_up(github, ["7"])
    _waiting_reviewer_typed(github)
    assert workflow.run_script("tick").returncode == 0
    _finished_wake(github, head)

    run = _ci_run(github, head)
    run.update(status="in_progress", conclusion=None, updated_at=_stamp(30), run_attempt=2)
    github.api[f"{_REPO}/actions/runs/20"] = run
    github.pulls["7"]["statusCheckRollup"][0].update(
        status="IN_PROGRESS", conclusion=None, startedAt=_stamp(30)
    )
    _show(github)
    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    _finished_wake(github, head)
    assert train_status(github)["lost"] == {}
    assert len(_lost_notes(github)) == 1
    assert _origin(github) == head


# 10. A gate holds the merge lock across a whole tick (a slow refresh): the tick reads the last
#     saved line and fails nothing; once the lock is free the next tick wakes once.
def test_a_merge_lock_held_through_a_tick_delays_the_wake_to_the_next(github: GitHub) -> None:
    import fcntl

    workflow = github.workflow
    head = github.head
    _failed_beside_lost(github)
    line_up(github, ["7"])
    _waiting_reviewer_typed(github)
    lock = workflow.repo / ".local/workflow/merge.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    assert _sent(github) == []

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    _finished_wake(github, head)
    assert len(_lost_notes(github)) == 1
