"""`tick` does, every few minutes, the part of the workflow that has one right answer."""

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from workflow_harness import Workflow, instruction

_WORKER = instruction("Implement pull request 7")

_MB = 1024 * 1024
_SAID_FINISHED = "● Merged as one commit, and 9 is based on main now.\n\n  Audit finished.\n\n> \n"
_WORKER_SAID_FINISHED = (
    "● Merged as one commit, and 9 is based on main now.\n\n  Worker finished.\n\n> \n"
)


def _started(workflow: Workflow) -> list[str]:
    return [call.split()[1] for call in workflow.calls("tmux") if " new-session " in call]


def _idle(workflow: Workflow, name: str) -> None:
    notes = workflow.worktree(int(name.split("-")[1])) / ".local" / "workflow"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"{name}.idle").touch()


def _reclaimed(group: Path) -> str:
    return (group / "memory.reclaim").read_text()


def test_a_draft_without_a_worker_gets_one(workflow: Workflow) -> None:
    workflow.add_pull_request(7)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert (workflow.state / "args" / "ticket-7").read_text().splitlines()[-1] == (
        f'claude --effort medium --remote-control ticket-7 "{_WORKER}"'
    )


def test_every_draft_without_a_worker_gets_one(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.add_pull_request(12)

    workflow.run_script("tick")

    assert sorted(_started(workflow)) == ["ticket-12", "ticket-7"]


def test_a_draft_with_a_worker_is_left_alone(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == []
    assert workflow.is_running("ticket-7")


def test_a_draft_that_an_audit_is_working_in_gets_no_worker(workflow: Workflow) -> None:
    """An audit can turn its pull request back into a draft; the worktree is still its own."""
    workflow.add_worktree(7)
    workflow.running("audit-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == []


def test_a_pull_request_that_is_ready_gets_an_independent_reviewer(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == ["review-7"]
    assert workflow.worktree(7).exists()


def test_a_draft_without_the_label_gets_nothing_started(workflow: Workflow) -> None:
    workflow.add_pull_request(7, labels=())
    workflow.add_pull_request(8, labels=("audited", "bug"))

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == []


def test_a_worker_that_merged_and_said_worker_finished_is_stopped_and_its_worktree_removed(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(_WORKER_SAID_FINISHED, "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert not worktree.exists()
    assert _started(workflow) == []
    assert "ticket-7 stopped" in done.stdout
    assert "ticket-7: removed, its pull request is merged" in done.stdout


@pytest.mark.parametrize("idle", [False, True], ids=["busy", "idle with a question"])
def test_a_worker_that_merged_and_has_not_said_worker_finished_is_left_running(
    workflow: Workflow, idle: bool
) -> None:
    """After its merge a worker still moves the pull requests that were built on its own."""
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running("ticket-7")
    if idle:
        _idle(workflow, "ticket-7")
    workflow.screen("● 9 conflicts with main. Which side do you keep?\n> \n", "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert worktree.is_dir()
    assert "ticket-7: kept, its worker is still running" in done.stdout


def test_the_worktree_of_a_merged_pull_request_without_a_session_is_removed(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not worktree.exists()
    assert "ticket-7: removed, its pull request is merged" in done.stdout


def test_an_idle_session_that_holds_more_than_300_megabytes_is_squeezed(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7)
    group = workflow.running("ticket-7", held=301 * _MB)
    _idle(workflow, "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _reclaimed(group) == str(301 * _MB)


@pytest.mark.parametrize(
    ("held", "idle"),
    [(300 * _MB, True), (5000 * _MB, False)],
    ids=["idle with 300 MB", "busy with 5 GB"],
)
def test_a_small_or_a_busy_session_is_not_squeezed(
    workflow: Workflow, held: int, idle: bool
) -> None:
    workflow.add_worktree(7)
    group = workflow.running("ticket-7", held=held)
    if idle:
        _idle(workflow, "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _reclaimed(group) == ""


def test_a_session_whose_memory_cannot_be_read_is_not_squeezed(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    group = workflow.running("ticket-7", held=5000 * _MB)
    _idle(workflow, "ticket-7")
    (group / "memory.current").unlink()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _reclaimed(group) == ""


def test_an_idle_audit_that_said_audit_finished_is_stopped(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    workflow.running("audit-7")
    _idle(workflow, "audit-7")
    workflow.screen(_SAID_FINISHED, "audit-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("audit-7")
    assert worktree.is_dir()
    assert "audit-7 stopped" in done.stdout


@pytest.mark.parametrize(
    "line",
    [
        "Audit finished.",
        "audit finished",
        "● Audit finished",
        "  AUDIT FINISHED!  ",
        "**Audit finished.**",
    ],
)
def test_the_words_count_on_a_line_of_their_own_however_they_are_dressed(
    workflow: Workflow, line: str
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("audit-7")
    _idle(workflow, "audit-7")
    workflow.screen(f"● Labelled and merged.\n\n{line}\n\n> \n", "audit-7")

    workflow.run_script("tick")

    assert not workflow.is_running("audit-7")


def test_the_worktree_of_a_merged_pull_request_goes_in_the_tick_that_ends_its_audit(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running("audit-7")
    _idle(workflow, "audit-7")
    workflow.screen(_SAID_FINISHED, "audit-7")

    workflow.run_script("tick")

    assert not workflow.is_running("audit-7")
    assert not worktree.exists()


@pytest.mark.parametrize(
    "screen",
    [
        "● Codex found nothing. Labelling it now.\n> \n",
        '● Read AGENTS.md\n  9. Say "audit finished" and stop.\n\n● Which limit do you want?\n> \n',
        "● I will write `audit finished` when the checks pass.\n> \n",
        "● The audit finished check has not passed. Which limit do you want?\n> \n",
        "● Audit finished except for one question: which limit do you want?\n> \n",
        '● The last line will be:\n  "audit finished"\n> \n',
        "● The last line will be:\n  'audit finished'\n> \n",
        "● The last line will be:\n  “audit finished”\n> \n",
        "● Rule 9: say “audit finished”, then stop.\n> \n",
        "● The worker's audit finished with two findings open; asking the owner.\n> \n",
        "● I will say audit finished once the checks pass.\n> \n",
        "> Audit pull request 7. Say audit finished when done.\n",
        "● The checks of the\n  preaudit finished\n> \n",
        "● Потом скажу audit finished\n> \n",
        "● 7: audit finished\n> \n",
        "● I cannot finish. The rule asks for:\n\n  > Audit finished\n\n",
        "● Labelled and merged.\n\n───────\n> audit finished\n───────\n",
    ],
    ids=[
        "said nothing of it",
        "quoted the rule",
        "named the words in code marks",
        "used the words inside a sentence",
        "began a sentence with the words",
        "quoted the words on a line of their own",
        "quoted them with single marks on a line of their own",
        "quoted them with curly marks on a line of their own",
        "quoted the rule with curly marks",
        "spoke of the worker's review",
        "said what it will say",
        "was told the words in its prompt",
        "another word ends in audit",
        "words in another alphabet stand before it",
        "a number stands before it",
        "quoted the words as a block quote",
        "the owner typed the words",
    ],
)
def test_an_idle_audit_that_did_not_say_it_is_left_running(workflow: Workflow, screen: str) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("audit-7")
    _idle(workflow, "audit-7")
    workflow.screen(screen, "audit-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("audit-7")


def test_an_audit_that_is_still_busy_is_left_running_whatever_its_screen_says(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("audit-7")
    workflow.screen(_SAID_FINISHED, "audit-7")

    workflow.run_script("tick")

    assert workflow.is_running("audit-7")


def test_the_worker_of_a_draft_is_never_stopped_for_what_its_screen_says(
    workflow: Workflow,
) -> None:
    """A draft without a session gets a worker, so stopping this one would start it again."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(_WORKER_SAID_FINISHED, "ticket-7")

    done = workflow.run_script("tick")

    assert workflow.is_running("ticket-7")
    assert _started(workflow) == []
    assert "stopped" not in done.stdout


@pytest.mark.parametrize(
    ("name", "screen"),
    [("ticket-7", _SAID_FINISHED), ("audit-7", _WORKER_SAID_FINISHED)],
    ids=["a worker says audit finished", "an audit says worker finished"],
)
def test_a_session_is_not_stopped_for_the_words_of_the_other_kind(
    workflow: Workflow, name: str, screen: str
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running(name)
    _idle(workflow, name)
    workflow.screen(screen, name)

    workflow.run_script("tick")

    assert workflow.is_running(name)


def test_a_second_tick_exits_while_the_first_holds_the_lock(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    lock = workflow.repo / ".git" / "shipline-tick.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["the tick before this one is still running"]
    assert workflow.calls() == []
    assert not workflow.is_running("ticket-7")


def test_the_lock_is_free_again_after_a_tick(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.run_script("tick")
    workflow.add_pull_request(8)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == ["ticket-7", "ticket-8"]


@pytest.mark.parametrize("draft", [True, False], ids=["worker", "reviewer"])
def test_sessions_start_by_priority_then_the_older_ticket(workflow: Workflow, draft: bool) -> None:
    workflow.add_pull_request(7, draft=draft, labels=("workflow", "priority:+low"))
    workflow.add_pull_request(8, draft=draft)
    workflow.add_pull_request(9, draft=draft, labels=("workflow", "priority:+high"))
    workflow.add_pull_request(10, draft=draft, labels=("workflow", "priority:+blocker"))
    workflow.add_pull_request(11, draft=draft, labels=("workflow", "priority:+medium"))

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    kind = "ticket" if draft else "review"
    assert _started(workflow) == [f"{kind}-{n}" for n in (10, 9, 8, 11, 7)]


@pytest.mark.parametrize("draft", [True, False], ids=["worker", "reviewer"])
def test_a_start_that_waits_for_memory_keeps_the_later_starts_waiting(
    workflow: Workflow, draft: bool
) -> None:
    """Otherwise memory freed in the meantime would go to a lower priority first."""
    workflow.add_pull_request(7, draft=draft)
    workflow.add_pull_request(8, draft=draft, labels=("workflow", "priority:+high"))
    workflow.pressure_file.write_text("some avg60=11.00\n")
    workflow.fake("sleep", '#!/bin/bash\necho "sleep $*" >> "@STATE@/calls.log"\nexit 91\n')

    workflow.run_script("tick")

    assert _started(workflow) == []
    # The high one was prepared and waits for memory; the medium one was not tried at all.
    assert workflow.worktree(8).exists()
    assert not workflow.worktree(7).exists()

    workflow.pressure_file.write_text("some avg60=0.00\n")
    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    kind = "ticket" if draft else "review"
    assert _started(workflow) == [f"{kind}-8", f"{kind}-7"]


@pytest.mark.parametrize("draft", [True, False], ids=["worker", "reviewer"])
@pytest.mark.parametrize("shortage", ["swap", "pressure", "load"])
def test_memory_shortage_defers_starts_and_later_ticks_still_clean_up(
    workflow: Workflow, draft: bool, shortage: str
) -> None:
    workflow.add_pull_request(8, draft=draft)
    if shortage == "swap":
        workflow.swaps_file.write_text(
            "Filename Type Size Used Priority\n/swapfile file 1000 501 10\n"
        )
    elif shortage == "pressure":
        workflow.pressure_file.write_text("some avg60=11.00\n")
    else:
        workflow.loadavg_file.write_text("1.00 9.00 1.00 1/100 1234\n")
    # Fail a wait immediately: the unfixed infinite loop must not hang this test.
    workflow.fake("sleep", '#!/bin/bash\necho "sleep $*" >> "@STATE@/calls.log"\nexit 91\n')

    workflow.run_script("tick")

    assert workflow.calls("sleep") == []
    assert _started(workflow) == []

    # Work that becomes eligible after the first tick must still be maintained,
    # while the same pending start remains blocked by unchanged memory readings.
    worktree = workflow.add_worktree(7, state="MERGED", draft=False)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(_WORKER_SAID_FINISHED, "ticket-7")
    workflow.run_script("tick")

    assert not workflow.is_running("ticket-7")
    assert not worktree.exists()
    assert _started(workflow) == []
    assert workflow.calls("sleep") == []

    workflow.pressure_file.write_text("some avg60=0.00\n")
    workflow.swaps_file.write_text("Filename Type Size Used Priority\n/swapfile file 1000 500 10\n")
    workflow.loadavg_file.write_text("0.40 0.40 0.40 1/100 1234\n")
    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == ["ticket-8" if draft else "review-8"]


def test_a_tick_does_nothing_when_github_cannot_be_asked(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    group = workflow.running("ticket-7", held=5000 * _MB)
    _idle(workflow, "ticket-7")
    workflow.add_pull_request(8)
    (workflow.state / "fail-gh-list").touch()

    done = workflow.run_script("tick")

    assert done.returncode != 0
    assert "cannot list the pull requests" in done.stderr
    assert _started(workflow) == []
    assert _reclaimed(group) == ""
    assert worktree.is_dir()


def test_a_start_that_fails_does_not_keep_the_next_draft_waiting(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    workflow.screen("Do you trust the files in this folder?\n", "ticket-7")
    (workflow.state / "dies-at-once").write_text("ticket-7\n")

    done = workflow.run_script("tick")

    assert done.returncode != 0
    assert "ticket-7 printed no session link" in done.stderr
    assert workflow.is_running("ticket-8")


def test_a_tick_can_be_run_from_a_worktree(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.add_pull_request(8)

    done = workflow.run_script("tick", cwd=worktree)

    assert done.returncode == 0, done.stderr
    assert workflow.worktree(8).is_dir()
    # One lock for the repository: in the git common directory, never in a worktree.
    assert (workflow.repo / ".git" / "shipline-tick.lock").exists()
    assert not list(worktree.rglob("*tick.lock"))


_MARKS_8_READY_DURING_SETUP = """#!/bin/bash
echo "worktree-setup $* in $PWD" >> "@STATE@/calls.log"
echo "false workflow" > "@STATE@/listing/8"
"""

_TAKES_THE_LABEL_OFF_8_DURING_SETUP = """#!/bin/bash
echo "worktree-setup $* in $PWD" >> "@STATE@/calls.log"
echo "true" > "@STATE@/listing/8"
"""


@pytest.mark.parametrize(
    "setup",
    [_MARKS_8_READY_DURING_SETUP, _TAKES_THE_LABEL_OFF_8_DURING_SETUP],
    ids=["marked ready", "label taken off"],
)
def test_a_draft_that_stopped_being_one_while_another_started_gets_no_worker(
    workflow: Workflow, setup: str
) -> None:
    """A start can take minutes; the list read at the beginning of the tick is old by then."""
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    workflow.fake("worktree-setup", setup)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == ["ticket-7"]
    assert not workflow.worktree(8).exists()


def test_no_worker_starts_when_github_stops_answering_between_two_starts(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    workflow.fake(
        "worktree-setup",
        '#!/bin/bash\necho "worktree-setup $* in $PWD" >> "@STATE@/calls.log"\ntouch "@STATE@/fail-gh-list"\n',
    )

    done = workflow.run_script("tick")

    assert done.returncode != 0
    assert _started(workflow) == ["ticket-7"]


@pytest.mark.parametrize(("running", "draft"), [(70, 7), (7, 70)])
def test_a_session_of_another_number_that_starts_the_same_does_not_count(
    workflow: Workflow, running: int, draft: int
) -> None:
    workflow.add_worktree(running)
    workflow.running(f"ticket-{running}")
    workflow.add_pull_request(draft)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == [f"ticket-{draft}"]


def test_a_draft_whose_audit_just_ended_gets_its_worker_in_the_same_tick(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7)
    workflow.running("audit-7")
    _idle(workflow, "audit-7")
    workflow.screen(_SAID_FINISHED, "audit-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("audit-7")
    assert _started(workflow) == ["ticket-7"]


def test_a_session_that_was_stopped_is_not_squeezed_afterwards(workflow: Workflow) -> None:
    workflow.add_worktree(7, state="MERGED")
    group = workflow.running("ticket-7", held=900 * _MB)
    _idle(workflow, "ticket-7")
    workflow.screen(_WORKER_SAID_FINISHED, "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert _reclaimed(group) == ""


@pytest.mark.parametrize("command", ["list", "stop", "cleanup", "squeeze"])
def test_a_step_that_fails_is_reported_and_the_steps_after_it_still_run(
    workflow: Workflow, command: str
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("audit-7")
    _idle(workflow, "audit-7")
    workflow.screen(_SAID_FINISHED, "audit-7")
    workflow.add_worktree(9)
    workflow.running("ticket-9", held=900 * _MB)
    _idle(workflow, "ticket-9")
    workflow.add_pull_request(8)
    workflow.break_session(command)

    done = workflow.run_script("tick")

    assert done.returncode != 0
    assert f"session {command} failed" in done.stderr
    assert "ticket-8" in _started(workflow)


def test_a_draft_is_matched_by_its_whole_number_when_github_is_asked_again(
    workflow: Workflow,
) -> None:
    """Pull request 17 is still a draft; that must not pass for 7, which is ready by then."""
    # 17 is more urgent, so it starts first and 7 is asked about after its start.
    workflow.add_pull_request(17, labels=("workflow", "priority:+high"))
    workflow.add_pull_request(7)
    workflow.fake(
        "worktree-setup",
        '#!/bin/bash\necho "worktree-setup $* in $PWD" >> "@STATE@/calls.log"\n'
        'echo "false workflow" > "@STATE@/listing/7"\n',
    )

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == ["ticket-17"]


def test_memory_is_freed_before_a_start_that_may_wait_for_it(workflow: Workflow) -> None:
    workflow.add_worktree(9)
    group = workflow.running("ticket-9", held=900 * _MB)
    _idle(workflow, "ticket-9")
    workflow.add_pull_request(8)
    seen = workflow.state / "reclaimed-when-the-start-began"
    workflow.fake(
        "worktree-setup",
        '#!/bin/bash\necho "worktree-setup $* in $PWD" >> "@STATE@/calls.log"\n'
        f'[ -e "{seen}" ] || cp "{group}/memory.reclaim" "{seen}"\n',
    )

    workflow.run_script("tick")

    assert seen.read_text() == str(900 * _MB)


@pytest.mark.parametrize("call", [1, 2], ids=["before the cleanup", "after the cleanup"])
def test_a_list_of_sessions_that_fails_once_is_reported(workflow: Workflow, call: int) -> None:
    workflow.add_worktree(9, draft=False)
    workflow.running("ticket-9")
    workflow.break_session("list", only_call=call)

    done = workflow.run_script("tick")

    assert done.returncode != 0
    assert "session list failed" in done.stderr


def test_an_idle_worker_that_said_worker_finished_is_stopped_while_its_pull_request_waits(
    workflow: Workflow,
) -> None:
    """Its base is not `main`, so it marked the pull request ready and did not merge it."""
    worktree = workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen("● Ready, its base is not main.\n\n  Worker finished.\n> \n", "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert worktree.is_dir()
    assert "ticket-7 stopped" in done.stdout
    assert _started(workflow) == ["review-7"]


@pytest.mark.parametrize(
    "line",
    ["Worker finished.", "worker finished", "● Worker finished", "**Worker finished.**"],
)
def test_worker_finished_counts_on_a_line_of_its_own_however_it_is_dressed(
    workflow: Workflow, line: str
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(f"● Merged.\n\n{line}\n\n> \n", "ticket-7")

    workflow.run_script("tick")

    assert not workflow.is_running("ticket-7")


@pytest.mark.parametrize(
    "screen",
    [
        "● Marked ready. Waiting for the checks on GitHub.\n> \n",
        "● The checks failed on the browser tests. Which limit do you want?\n> \n",
        '● Read AGENTS.md\n  Say "worker finished" on a line of its own and stop.\n> \n',
        "● I will say worker finished once the checks pass.\n> \n",
        '● The last line will be:\n  "worker finished"\n> \n',
        "● The coworker finished\n> \n",
        "● I cannot merge. The rule asks for:\n\n  > Worker finished\n\n",
        "● Marked ready.\n\n───────\n> worker finished\n───────\n",
    ],
    ids=[
        "waits for the checks",
        "asks the owner",
        "quoted the rule",
        "said what it will say",
        "quoted the words on a line of their own",
        "another word ends in worker",
        "quoted the words as a block quote",
        "the owner typed the words",
    ],
)
def test_an_idle_worker_whose_pull_request_is_ready_is_left_running_until_it_says_so(
    workflow: Workflow, screen: str
) -> None:
    """Ready is not the end: the worker still waits for the checks and merges."""
    workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(screen, "ticket-7")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")


def test_a_busy_worker_is_left_running_whatever_its_screen_says(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    workflow.screen(_WORKER_SAID_FINISHED, "ticket-7")

    workflow.run_script("tick")

    assert workflow.is_running("ticket-7")


def test_an_idle_worker_whose_pull_request_is_a_draft_is_left_running(
    workflow: Workflow,
) -> None:
    """It waits for the owner or for a job of its own; being idle ends nothing."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.add_pull_request(17, draft=False)

    done = workflow.run_script("tick")

    assert workflow.calls("tmux ticket-7 kill-server") == []
    assert "stopped" not in done.stdout
    assert _started(workflow) == ["review-17"]


def test_an_idle_worker_outside_the_workflow_is_left_running(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.list_pull_request(7, draft=False, labels=())
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")

    workflow.run_script("tick")

    assert workflow.is_running("ticket-7")


def test_a_worker_outside_the_workflow_that_said_worker_finished_is_stopped(
    workflow: Workflow,
) -> None:
    """The owner started it by hand; it is not a draft the tick would start again."""
    workflow.add_worktree(7, draft=False)
    workflow.list_pull_request(7, draft=False, labels=())
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(_WORKER_SAID_FINISHED, "ticket-7")

    workflow.run_script("tick")

    assert not workflow.is_running("ticket-7")


def test_an_idle_audit_is_not_stopped_because_its_pull_request_is_ready(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("audit-7")
    _idle(workflow, "audit-7")
    workflow.screen("● Which limit do you want?\n> \n", "audit-7")

    workflow.run_script("tick")

    assert workflow.is_running("audit-7")


@pytest.mark.parametrize("other", [17, 70])
def test_a_worker_that_finished_is_stopped_whatever_draft_has_a_number_like_its_own(
    workflow: Workflow, other: int
) -> None:
    """Pull request 17 or 70 being a draft must not pass for 7 being one."""
    workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(_WORKER_SAID_FINISHED, "ticket-7")
    workflow.add_worktree(other)
    workflow.running(f"ticket-{other}")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert workflow.is_running(f"ticket-{other}")


@pytest.mark.parametrize(
    ("name", "words"), [("ticket-7", "Worker finished."), ("audit-7", "Audit finished.")]
)
def test_words_that_scrolled_off_the_screen_do_not_count(
    workflow: Workflow, name: str, words: str
) -> None:
    """A session that was given more work after it finished is asking the owner again."""
    workflow.add_worktree(7, draft=False)
    workflow.running(name)
    _idle(workflow, name)
    workflow.scrollback(f"● Merged.\n\n  {words}\n\n", name)
    workflow.screen("  form.py\n  Which side do you keep?\n> \n", name)

    workflow.run_script("tick")

    assert workflow.is_running(name)


@pytest.mark.parametrize(
    ("name", "words"), [("ticket-7", "Worker finished."), ("audit-7", "Audit finished.")]
)
@pytest.mark.parametrize(
    "later",
    [
        "● 9 conflicts with main. Which side do you keep?\n",
        "\b● Which side do you keep?\n",
        "> also move 9\n\n● Bash(git merge origin/main)\n  ⎿ CONFLICT\n\n● Which side?\n",
    ],
    ids=["a new reply", "a new reply on the next line", "a new instruction, a command and a reply"],
)
def test_words_that_a_newer_reply_follows_do_not_count(
    workflow: Workflow, name: str, words: str, later: str
) -> None:
    """Given more work after it finished, the session still shows the old line above."""
    workflow.add_worktree(7, draft=False)
    workflow.running(name)
    _idle(workflow, name)
    # A backspace at the start of `later` stands for no empty line between the two.
    gap = "\n" if later.startswith("\b") else "\n\n"
    workflow.screen(f"● Merged.\n\n  {words}{gap}{later.removeprefix(chr(8))}\n> \n", name)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running(name)


def test_the_words_said_again_after_more_work_count(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(
        "● Merged.\n\n  Worker finished.\n\n> also move 9\n\n● 9 is based on main now.\n\n"
        "  Worker finished.\n\n───────\n> \n───────\n  ? for shortcuts\n",
        "ticket-7",
    )

    workflow.run_script("tick")

    assert not workflow.is_running("ticket-7")


_PRINTS_THE_SCREEN_AND_FAILS = """#!/bin/bash
if [ "$3" = capture-pane ]; then
  cat "@STATE@/screen/$2"
  exit 1
fi
exec "$(dirname "$0")/tmux.real" "$@"
"""


def test_a_screen_that_could_not_be_read_ends_no_session(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen(_WORKER_SAID_FINISHED, "ticket-7")
    (workflow.bin / "tmux").rename(workflow.bin / "tmux.real")
    workflow.fake("tmux", _PRINTS_THE_SCREEN_AND_FAILS)

    workflow.run_script("tick")

    assert workflow.is_running("ticket-7")


_REAL_FOOTER = (
    "✻ Worked for 2m 14s\n"
    "  ⎿  Tip: press ctrl+o to expand\n\n"
    "────────────────────\n\u276f \n────────────────────\n"
    "  ⏵⏵ auto mode on · PR #7 · ← for agents\n\n"
    "  ● main\n  ◯ pr-test-analyzer  Done\n"
)


@pytest.mark.parametrize(
    ("name", "words"), [("ticket-7", "Worker finished."), ("audit-7", "Audit finished.")]
)
def test_the_words_count_above_what_claude_draws_under_a_reply(
    workflow: Workflow, name: str, words: str
) -> None:
    """The list of agents under the box has a bullet of its own; it is not a newer reply."""
    workflow.add_worktree(7, draft=False)
    workflow.running(name)
    _idle(workflow, name)
    workflow.screen(f"● Merged as one commit.\n\n  {words}\n\n{_REAL_FOOTER}", name)

    workflow.run_script("tick")

    assert not workflow.is_running(name)


@pytest.mark.parametrize(
    ("name", "words"), [("ticket-7", "Worker finished."), ("audit-7", "Audit finished.")]
)
@pytest.mark.parametrize(
    "more",
    ["  But 9 conflicts with main. Which side do you keep?\n", "\n  - 9: which side?\n"],
    ids=["text on the next line", "a list after an empty line"],
)
def test_words_that_the_same_reply_goes_on_after_do_not_count(
    workflow: Workflow, name: str, words: str, more: str
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running(name)
    _idle(workflow, name)
    workflow.screen(f"● Merged as one commit.\n\n  {words}\n{more}\n{_REAL_FOOTER}", name)

    workflow.run_script("tick")

    assert workflow.is_running(name)


@pytest.mark.parametrize(
    ("name", "words"), [("ticket-7", "Worker finished."), ("audit-7", "Audit finished.")]
)
def test_words_that_an_instruction_follows_do_not_count(
    workflow: Workflow, name: str, words: str
) -> None:
    """Claude shows what the owner sent after the prompt mark, from the left edge."""
    workflow.add_worktree(7, draft=False)
    workflow.running(name)
    _idle(workflow, name)
    workflow.screen(f"● Merged.\n\n  {words}\n\n\u276f also move 9\n\n{_REAL_FOOTER}", name)

    workflow.run_script("tick")

    assert workflow.is_running(name)


# Saturday 24 October 2026, 08:00 UTC; the day after is a Sunday.
_EIGHT = 1_792_828_800
_DAILY = {"WORKFLOW_ANALYST_DAILY_AT": "08:00", "TZ": "UTC"}
_REPORT = (
    "# Workflow analysis\n\nWhat the week showed.\n\n## Proposals\n\n"
    "1. The tick starts tickets by itself: one start a day was by hand.\n"
    "   Evidence: wake.log, 14 starts.\n"
    "2. A lost check is re-run by the gate.\n\n## Evidence\n\n1. Not a proposal.\n"
)


def _at(seconds: int) -> dict[str, str]:
    return {**_DAILY, "WORKFLOW_NOW": str(seconds)}


def _metrics_report(workflow: Workflow, day: str) -> None:
    report = workflow.repo / ".local" / "analyses" / f"metrics-{day}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Workflow metrics\n")


def _usage(workflow: Workflow, five: int, resets_in: int, now: int = _EIGHT) -> None:
    """The figures the status line keeps, written as fresh at the tick's time."""
    from datetime import UTC, datetime

    workflow.usage(five, datetime.fromtimestamp(now + resets_in, UTC).isoformat())
    cache = workflow.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
    os.utime(cache, (now, now))


def _analyst_words(workflow: Workflow, name: str) -> list[str]:
    command = (workflow.state / "args" / name).read_text().splitlines()[-1]
    read = subprocess.run(
        ["bash", "-c", f'claude() {{ printf "%s\\n" "$@"; }}\n{command}'],
        capture_output=True,
        text=True,
        check=True,
    )
    return read.stdout.splitlines()


def test_the_tick_starts_one_analyst_a_day_from_0800_utc_after_the_metrics_run(
    workflow: Workflow,
) -> None:
    _metrics_report(workflow, "2026-10-24")
    _usage(workflow, 40, 3 * 3600)

    before = workflow.run_script("tick", env=_at(_EIGHT - 60))
    done = workflow.run_script("tick", env=_at(_EIGHT + 60))
    again = workflow.run_script("tick", env=_at(_EIGHT + 240))

    assert before.returncode == done.returncode == again.returncode == 0, done.stderr
    assert _started(workflow) == ["analyst-day-2026-10-24"]
    assert _analyst_words(workflow, "analyst-day-2026-10-24")[-1] == (
        "Analyse the workflow over the last day, as analyst-day-2026-10-24, "
        "by the section Analyse the workflow of AGENTS.md in this worktree"
    )
    assert workflow.analysis_report("day-2026-10-24").is_dir()


def test_the_tick_starts_no_analyst_before_the_days_metrics_report(workflow: Workflow) -> None:
    _metrics_report(workflow, "2026-10-23")
    _usage(workflow, 40, 3 * 3600)

    done = workflow.run_script("tick", env=_at(_EIGHT + 60))

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == []


def test_on_sunday_the_analyst_reads_seven_days(workflow: Workflow) -> None:
    sunday = _EIGHT + 86400
    _metrics_report(workflow, "2026-10-25")
    _usage(workflow, 40, 3 * 3600, now=sunday)

    done = workflow.run_script("tick", env=_at(sunday + 60))

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == ["analyst-week-2026-10-25"]
    assert _analyst_words(workflow, "analyst-week-2026-10-25")[-1].startswith(
        "Analyse the workflow over the last 7 days, as analyst-week-2026-10-25, "
    )


@pytest.mark.parametrize(
    ("five", "resets_in", "starts"),
    [(86, 2 * 3600, False), (90, 3000, True), (85, 4 * 3600, True)],
    ids=["above 85% for over an hour", "above 85% but resets within the hour", "at 85%"],
)
def test_the_analyst_waits_while_the_usage_window_holds_starts(
    workflow: Workflow, five: int, resets_in: int, starts: bool
) -> None:
    _metrics_report(workflow, "2026-10-24")
    _usage(workflow, five, resets_in)

    done = workflow.run_script("tick", env=_at(_EIGHT + 60))

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == (["analyst-day-2026-10-24"] if starts else [])
    if not starts:
        assert "the daily analyst waits: the 5-hour usage window is 86% used" in done.stdout
        assert not workflow.analysis_report("day-2026-10-24").exists()


def test_the_analyst_waits_while_the_usage_cannot_be_read(workflow: Workflow) -> None:
    _metrics_report(workflow, "2026-10-24")

    done = workflow.run_script("tick", env=_at(_EIGHT + 60))

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == []
    assert "the daily analyst waits: the 5-hour usage window could not be read" in done.stdout


def test_an_analyst_held_by_the_usage_starts_at_a_later_tick(workflow: Workflow) -> None:
    _metrics_report(workflow, "2026-10-24")
    _usage(workflow, 95, 2 * 3600)
    workflow.run_script("tick", env=_at(_EIGHT + 60))
    _usage(workflow, 10, 4 * 3600, now=_EIGHT + 2 * 3600)

    done = workflow.run_script("tick", env=_at(_EIGHT + 2 * 3600))

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == ["analyst-day-2026-10-24"]


_ANALYST_SAID_FINISHED = "● Report published.\n\n  analysis finished\n\n> \n"


@pytest.mark.parametrize("idle", [True, False], ids=["idle", "listed busy"])
def test_an_analyst_that_said_analysis_finished_is_stopped_and_its_report_told(
    workflow: Workflow, idle: bool
) -> None:
    worktree = workflow.add_analyst("day-2026-10-24")
    workflow.running("analyst-day-2026-10-24")
    if idle:
        (worktree / ".local" / "workflow" / "analyst-day-2026-10-24.idle").touch()
    workflow.screen(_ANALYST_SAID_FINISHED, "analyst-day-2026-10-24")
    report = workflow.analysis_report("day-2026-10-24")
    (report / "report.md").write_text(_REPORT)
    (report / "report.url").write_text("https://docs.example.com/analysis-1\n")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("analyst-day-2026-10-24")
    assert not worktree.exists()
    assert (report / "report.md").read_text() == _REPORT
    pending = workflow.repo / ".local" / "workflow" / "analyses.pending"
    assert json.loads((pending / "day-2026-10-24.json").read_text()) == {
        "name": "day-2026-10-24",
        "report": ".local/analyses/day-2026-10-24/report.md",
        "url": "https://docs.example.com/analysis-1",
        "proposals": [
            "1. The tick starts tickets by itself: one start a day was by hand",
            "2. A lost check is re-run by the gate",
        ],
    }


@pytest.mark.parametrize(
    "screen",
    [
        "● Writing the report; I end with analysis finished when it is published.\n\n> \n",
        "● analysis finished\n\n● Bash(ls .local/analyses)\n\n> \n",
    ],
    ids=["in a sentence", "followed by more work"],
)
def test_an_analyst_that_has_not_finished_is_left_running(workflow: Workflow, screen: str) -> None:
    workflow.add_analyst("day-2026-10-24")
    workflow.running("analyst-day-2026-10-24")
    workflow.screen(screen, "analyst-day-2026-10-24")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("analyst-day-2026-10-24")
    assert not (workflow.repo / ".local" / "workflow" / "analyses.pending").exists()


@pytest.mark.parametrize(("minutes", "stopped"), [(39, False), (41, True)])
def test_an_analyst_is_stopped_40_minutes_after_its_start(
    workflow: Workflow, minutes: int, stopped: bool
) -> None:
    worktree = workflow.add_analyst("day-2026-10-24")
    workflow.running("analyst-day-2026-10-24")
    started = worktree / ".local" / "workflow" / "analyst-day-2026-10-24.started"
    then = time.time() - minutes * 60
    os.utime(started, (then, then))

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("analyst-day-2026-10-24") is not stopped
    pending = workflow.repo / ".local" / "workflow" / "analyses.pending" / "day-2026-10-24.json"
    if stopped:
        assert "analyst-day-2026-10-24 is over its time" in done.stdout
        # It ended without its report: the wake says so.
        assert json.loads(pending.read_text()) == {
            "name": "day-2026-10-24",
            "report": ".local/analyses/day-2026-10-24/report.md",
            "url": "",
            "proposals": [],
        }
    else:
        assert not pending.exists()


def test_an_analyst_is_stopped_on_its_time_while_github_cannot_be_asked(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_analyst("day-2026-10-24")
    workflow.running("analyst-day-2026-10-24")
    started = worktree / ".local" / "workflow" / "analyst-day-2026-10-24.started"
    then = time.time() - 41 * 60
    os.utime(started, (then, then))
    (workflow.state / "fail-gh-list").touch()

    workflow.run_script("tick")

    assert not workflow.is_running("analyst-day-2026-10-24")


def test_at_most_three_proposals_are_noted_each_cut_short_and_a_link_only_if_it_is_one(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_analyst("day-2026-10-24")
    workflow.running("analyst-day-2026-10-24")
    workflow.screen(_ANALYST_SAID_FINISHED, "analyst-day-2026-10-24")
    report = workflow.analysis_report("day-2026-10-24")
    long = "1. " + "x" * 400
    (report / "report.md").write_text(f"## Proposals\n\n{long}\n2. Two\x07.\n3) Three\n4. Four\n")
    (report / "report.url").write_text("javascript:alert(1) https://docs.example.com/x\n")

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert not worktree.exists()
    pending = workflow.repo / ".local" / "workflow" / "analyses.pending" / "day-2026-10-24.json"
    noted = json.loads(pending.read_text())
    assert noted["url"] == ""
    assert noted["proposals"] == [long[:300], "2. Two", "3) Three"]


def test_an_analyst_that_ended_by_itself_is_noted_once_as_its_worktree_goes(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_analyst("day-2026-10-24")
    report = workflow.analysis_report("day-2026-10-24")
    (report / "report.url").write_text("https://docs.example.com/analysis-3\n")
    pending = workflow.repo / ".local" / "workflow" / "analyses.pending" / "day-2026-10-24.json"

    done = workflow.run_script("tick")
    told = json.loads(pending.read_text())
    pending.unlink()  # the wake told it
    again = workflow.run_script("tick")

    assert done.returncode == again.returncode == 0, done.stderr
    assert not worktree.exists()
    assert told["url"] == "https://docs.example.com/analysis-3"
    assert not pending.exists()


def test_an_analyst_whose_worktree_cannot_be_removed_is_noted_once(workflow: Workflow) -> None:
    worktree = workflow.add_analyst("day-2026-10-24")
    pending = workflow.repo / ".local" / "workflow" / "analyses.pending" / "day-2026-10-24.json"
    workflow.fake(
        "git", '#!/bin/bash\n[ "$1 $2" != \'worktree remove\' ] || exit 1\nexec /usr/bin/git "$@"\n'
    )

    workflow.run_script("tick")
    assert pending.exists()
    pending.unlink()  # the wake told it
    workflow.run_script("tick")

    assert worktree.exists()
    assert not pending.exists()


def test_an_analyst_start_that_failed_is_not_tried_again_that_day(workflow: Workflow) -> None:
    _metrics_report(workflow, "2026-10-24")
    _usage(workflow, 40, 3 * 3600)
    workflow.screen("Do you trust the files in this folder?\n")

    failed = workflow.run_script("tick", env=_at(_EIGHT + 60))
    again = workflow.run_script("tick", env=_at(_EIGHT + 600))

    assert failed.returncode != 0
    assert again.returncode == 0, again.stderr
    assert _started(workflow) == ["analyst-day-2026-10-24"]
    pending = workflow.repo / ".local" / "workflow" / "analyses.pending" / "day-2026-10-24.json"
    assert json.loads(pending.read_text())["url"] == ""


def test_the_analyst_waits_five_minutes_after_another_start(workflow: Workflow) -> None:
    _metrics_report(workflow, "2026-10-24")
    _usage(workflow, 40, 3 * 3600)
    workflow.add_pull_request(7)

    done = workflow.run_script("tick", env=_at(_EIGHT + 60))

    assert done.returncode == 0, done.stderr
    assert _started(workflow) == ["ticket-7"]
    assert "the daily analyst waits: a session started in the last 5 minutes" in done.stdout
    _usage(workflow, 40, 3 * 3600, now=_EIGHT + 420)
    later = workflow.run_script("tick", env=_at(_EIGHT + 420))
    assert later.returncode == 0, later.stderr
    assert _started(workflow) == ["ticket-7", "analyst-day-2026-10-24"]


def test_an_analyst_start_that_waits_for_memory_is_left_for_a_later_tick(
    workflow: Workflow,
) -> None:
    _metrics_report(workflow, "2026-10-24")
    _usage(workflow, 40, 3 * 3600)
    workflow.pressure_file.write_text("some avg10=30.00 avg60=10.01 avg300=1.00 total=1\n")

    held = workflow.run_script("tick", env=_at(_EIGHT + 60))

    assert held.returncode == 0, held.stderr
    assert _started(workflow) == []
    assert not workflow.analysis_report("day-2026-10-24").exists()
    assert not workflow.analyst_worktree("day-2026-10-24").exists()
    workflow.pressure_file.write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
    started = workflow.run_script("tick", env=_at(_EIGHT + 240))
    assert started.returncode == 0, started.stderr
    assert _started(workflow) == ["analyst-day-2026-10-24"]
