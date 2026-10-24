"""Breaking tests for pull request 56: the tick skips sessions it stopped when it writes notes.

They replay the daily flow with neutral numbers: reviewers that said "review finished" are
stopped by the tick, cleanup removes their worktrees, and the same tick goes on to its screen
check and later loops. The tick must exit 0 for that, keep writing notes for sessions that still
run, and still fail on a real write error."""

import shlex
from pathlib import Path

import pytest
from workflow.test_checks_finished import _nudges, _waiting
from workflow_harness import Workflow

_NOW = 1_790_000_000
_BOX = "─" * 40 + "\n> \n" + "─" * 40 + "\n  ? for shortcuts\n"
_BUSY = "● Bash(pytest tests)\n  ⎿  Running… (1m)\n\n✻ Cogitating… (1m · esc to interrupt)\n\n"


def _finished_review(workflow: Workflow, number: int, state: str = "MERGED") -> Path:
    worktree = workflow.add_worktree(number, state=state, draft=False)
    name = f"review-{number}"
    workflow.running(name)
    notes = workflow.notes(number)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"{name}.idle").touch()
    workflow.screen("● Merged.\n\nreview finished\n" + _BOX, name)
    return worktree


def _busy_ticket(workflow: Workflow, number: int) -> None:
    workflow.add_worktree(number, draft=True)
    workflow.running(f"ticket-{number}")
    workflow.screen(_BUSY + _BOX, f"ticket-{number}")


def _state(workflow: Workflow) -> Path:
    return workflow.repo / ".local/workflow"


def test_two_reviewers_finished_in_one_tick_end_it_with_0(workflow: Workflow) -> None:
    """The everyday case: two reviewers merged their pull requests and said so."""
    first = _finished_review(workflow, 7)
    second = _finished_review(workflow, 8)
    _busy_ticket(workflow, 9)

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("review-7") and not workflow.is_running("review-8")
    assert not first.exists() and not second.exists()
    assert not (_state(workflow) / "review-7.screen-seen").exists()
    assert not (_state(workflow) / "review-8.screen-seen").exists()
    # The session that still runs keeps its screen note in the same tick.
    assert (_state(workflow) / "ticket-9.screen-seen").read_text().split()[1] == str(
        _NOW
    )


def test_reviewers_stopped_with_screen_notes_from_earlier_ticks(
    workflow: Workflow,
) -> None:
    """Earlier ticks left a `.screen-seen` for each reviewer before it finished."""
    _finished_review(workflow, 7)
    _finished_review(workflow, 8)
    _busy_ticket(workflow, 9)
    folder = _state(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("review-7", "review-8"):
        (folder / f"{name}.screen-seen").write_text(f"123 456/idle {_NOW - 600}\n")

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert (folder / "ticket-9.screen-seen").exists()


def test_a_stale_list_of_two_stopped_reviewers_ends_the_tick_with_0(
    workflow: Workflow,
) -> None:
    """`session list` read before the stops, as a list kept for the whole tick can be."""
    _finished_review(workflow, 7)
    _finished_review(workflow, 8)
    _busy_ticket(workflow, 9)
    real = workflow.script.with_name("session.real")
    workflow.script.rename(real)
    cached = workflow.state / "cached-sessions"
    workflow.script.write_text(
        "#!/bin/bash\n"
        "if [[ $1 == list ]]; then\n"
        f"  [[ -f {shlex.quote(str(cached))} ]] ||"
        f" bash {shlex.quote(str(real))} list >{shlex.quote(str(cached))}\n"
        f"  cat {shlex.quote(str(cached))}\n"
        "else\n"
        f'  exec bash {shlex.quote(str(real))} "$@"\n'
        "fi\n"
    )

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert not (_state(workflow) / "review-7.screen-seen").exists()
    assert (_state(workflow) / "ticket-9.screen-seen").exists()


def test_a_reviewer_on_a_stack_member_keeps_its_head_note(workflow: Workflow) -> None:
    """An open, ready stack member: the head note is written before the reviewer is stopped."""
    _finished_review(workflow, 7, state="OPEN")
    (workflow.state / "base-branch-7").write_text("ticket/six\n")
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert (_state(workflow) / "review-7.head").read_text().strip() == head
    assert not workflow.is_running("review-7")


@pytest.mark.parametrize("how", ["directory", "readonly-note"])
def test_a_note_that_cannot_be_written_for_a_running_session_still_fails(
    workflow: Workflow, how: str
) -> None:
    """A real write error, with the session still running, must still fail the tick."""
    _busy_ticket(workflow, 9)
    folder = _state(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    note = folder / "ticket-9.screen-seen"
    if how == "directory":
        # Any write to it fails with "Is a directory", whoever runs the tick.
        note.mkdir()
    else:
        note.write_text("stale 1\n")
        note.chmod(0o444)

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    if how == "readonly-note" and done.returncode == 0:
        pytest.skip("running as root: a read-only file is still writable")
    assert done.returncode == 1, done.stderr
    assert workflow.is_running("ticket-9")


def test_a_session_whose_tmux_ended_by_itself_is_skipped_without_a_note(
    workflow: Workflow,
) -> None:
    """A session that exits on its own between the list and the screen check."""
    _busy_ticket(workflow, 9)
    workflow.fake(
        "cksum",
        "#!/bin/bash\n"
        f"rm -f -- {shlex.quote(str(workflow.state / 'tmux' / 'ticket-9'))}\n"
        'exec /usr/bin/cksum "$@"\n',
    )

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert not (_state(workflow) / "ticket-9.screen-seen").exists()


def test_a_live_reviewer_woken_beside_a_stopped_one_is_woken_once(
    workflow: Workflow,
) -> None:
    """The mark of a nudge still lands for a reviewer that runs, so the next tick, which
    re-reads the same finished checks, does not nudge it again."""
    head = _waiting(workflow)
    _finished_review(workflow, 8)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert len(_nudges(workflow)) == 1
    assert (_state(workflow) / f"review-7.checks-nudged-{head}").exists()
    assert not workflow.is_running("review-8")
