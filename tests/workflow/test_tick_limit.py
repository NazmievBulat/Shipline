"""`tick` and a session the usage limit stopped: squeezed, and woken once after the reset."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from workflow_harness import Workflow, instruction

_WORKER = instruction("Implement pull request 7")
_MB = 1024 * 1024
_NEW_YORK = ZoneInfo("America/New_York")
_BOX = "\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n"


def _stopped(resets: str, limit: str = "session limit") -> str:
    return f"> {_WORKER}\n  ⎿  You've hit your {limit} · resets {resets}\n{_BOX}"


def _at(
    hour: int,
    minute: int = 0,
    day: int = 13,
    zone: ZoneInfo = _NEW_YORK,
    month: int = 10,
    year: int = 2026,
) -> dict[str, str]:
    """The clock of the tick: this time on a day of October 2026, unless another is named."""
    moment = datetime(year, month, day, hour, minute, tzinfo=zone)
    return {"WORKFLOW_NOW": str(int(moment.timestamp())), "TZ": "UTC"}


def _resent(workflow: Workflow, name: str = "ticket-7") -> int:
    return len(workflow.calls(f"tmux {name} send-keys -t ={name}: -l "))


def _limit_stopped(
    workflow: Workflow, resets: str, name: str = "ticket-7", limit: str = "session limit"
) -> None:
    workflow.add_worktree(7)
    workflow.running(name)
    workflow.screen(_stopped(resets, limit), name)


def test_a_session_stopped_on_the_limit_is_left_alone_before_the_reset(
    workflow: Workflow,
) -> None:
    _limit_stopped(workflow, "4:10pm (America/New_York)")

    done = workflow.run_script("tick", env=_at(16, 9))

    assert done.returncode == 0, done.stderr
    assert _resent(workflow) == 0
    assert workflow.is_running("ticket-7")
    assert [call for call in workflow.calls("tmux") if " new-session " in call] == []


def test_after_the_reset_the_session_is_sent_its_instruction_again_once(
    workflow: Workflow,
) -> None:
    _limit_stopped(workflow, "4:10pm (America/New_York)")

    first = workflow.run_script("tick", env=_at(16, 10))
    second = workflow.run_script("tick", env=_at(16, 13))

    assert first.returncode == 0, first.stderr
    assert "ticket-7 was sent its instruction again" in first.stdout
    assert second.returncode == 0, second.stderr
    assert _resent(workflow) == 1
    assert workflow.calls("tmux ticket-7 send-keys")[0].endswith(f"-l {_WORKER}")


def test_a_limit_line_with_advice_under_it_is_answered_once_after_the_reset(
    workflow: Workflow,
) -> None:
    """The screen a review session stayed on all night."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(
        f"> {_WORKER}\n"
        "  ⎿  You've hit your session limit · resets 10:30pm (America/New_York)\n"
        "     /upgrade or /usage-credits to finish what you're working on.\n"
        "✻ Baked for 0s · done 9:33 PM\n"
        f"{_BOX}",
        "ticket-7",
    )

    before = workflow.run_script("tick", env=_at(22, 29))
    first = workflow.run_script("tick", env=_at(22, 30))
    second = workflow.run_script("tick", env=_at(22, 33))

    assert before.returncode == 0, before.stderr
    assert first.returncode == 0, first.stderr
    assert "ticket-7 was sent its instruction again" in first.stdout
    assert second.returncode == 0, second.stderr
    assert _resent(workflow) == 1


def test_a_limit_met_again_with_another_time_is_answered_again(workflow: Workflow) -> None:
    _limit_stopped(workflow, "4:10pm (America/New_York)")
    workflow.run_script("tick", env=_at(16, 10))
    workflow.screen(_stopped("9:10pm (America/New_York)"), "ticket-7")

    early = workflow.run_script("tick", env=_at(16, 13))
    sent_early = _resent(workflow)
    workflow.run_script("tick", env=_at(21, 11))

    assert early.returncode == 0, early.stderr
    assert sent_early == 1
    assert _resent(workflow) == 2


@pytest.mark.parametrize(
    ("resets", "now", "sent"),
    [
        # The reset is at most 5 hours after the stop: a time further ahead was earlier today.
        ("4pm (America/New_York)", _at(11, 0), 0),
        ("4pm (America/New_York)", _at(10, 59), 1),
        ("4pm (America/New_York)", _at(16, 0), 1),
        # Over midnight: stopped at 23:00 with a reset at 00:30.
        ("12:30am (America/New_York)", _at(23, 30), 0),
        ("12:30am (America/New_York)", _at(0, 30, day=14), 1),
        ("11pm (America/New_York)", _at(1, 0, day=14), 1),
        ("12pm (America/New_York)", _at(11, 59), 0),
        ("12pm (America/New_York)", _at(12, 0), 1),
        # The time zone on the line is the one that counts, not the machine's.
        ("10pm (Europe/Berlin)", _at(15, 59), 0),
        ("10pm (Europe/Berlin)", _at(16, 0), 1),
        # With its year, as a reset in the next year is printed.
        ("Jan 2, 2027, 1am (America/New_York)", _at(1, 0, day=15), 0),
        ("Oct 15, 2026, 1am (America/New_York)", _at(0, 59, day=15), 0),
        ("Oct 15, 2026, 1am (America/New_York)", _at(1, 0, day=15), 1),
        ("Oct 15, 2025, 1am (America/New_York)", _at(1, 0, day=13), 1),
        # Without its year, read in December for January.
        ("Jan 1, 1am (America/New_York)", _at(0, 0, day=31, month=12), 0),
        ("Jan 1, 1am (America/New_York)", _at(1, 0, day=1, month=1, year=2027), 1),
        # Written in other ways.
        ("Oct 15 1am (America/New_York)", _at(1, 0, day=15), 1),
        ("Oct 15, 1am", {**_at(1, 0, day=15), "TZ": "America/New_York"}, 1),
        ("4pm (UTC)", _at(11, 59), 0),
        ("4pm (UTC)", _at(12, 0), 1),
        # The night the clocks go forward in New York: 11:30pm to 5am is four and a half hours.
        ("5am (America/New_York)", _at(23, 30, day=7, month=3), 0),
        ("5am (America/New_York)", _at(5, 0, day=8, month=3), 1),
        # The night they go back: 11:30pm to 4am is five and a half hours, so 4am has passed.
        ("4am (America/New_York)", _at(23, 30, day=31), 1),
        ("3:30am (America/New_York)", _at(23, 30, day=31), 0),
        # The day after they went forward, 5pm is 22 hours back from 4pm, not 23.
        ("5pm (America/New_York)", _at(11, 0, day=9, month=3), 1),
        # A line that names the day: the weekly limit.
        ("Oct 15, 1am (America/New_York)", _at(0, 59, day=15), 0),
        ("Oct 15, 1am (America/New_York)", _at(23, 0, day=14), 0),
        ("Oct 15, 1am (America/New_York)", _at(1, 0, day=15), 1),
        # A time that cannot be read is never taken as passed.
        ("…", _at(16, 0), 0),
        ("soon", _at(16, 0), 0),
        ("tomorrow", _at(16, 0), 0),
        ("25pm (America/New_York)", _at(16, 0), 0),
        ("4pm (Nowhere/Atall)", _at(17, 0), 0),
        ("4pm (../../etc/passwd)", _at(17, 0), 0),
    ],
)
def test_the_reset_has_passed_only_by_the_clock_of_the_line(
    workflow: Workflow, resets: str, now: dict[str, str], sent: int
) -> None:
    _limit_stopped(workflow, resets)

    done = workflow.run_script("tick", env=now)

    assert done.returncode == 0, done.stderr
    assert _resent(workflow) == sent


@pytest.mark.parametrize("limit", ["weekly limit", "Opus limit", "limit"])
def test_an_hour_without_a_day_is_read_only_for_the_session_limit(
    workflow: Workflow, limit: str
) -> None:
    """At 4am a weekly reset at 4pm is 12 hours ahead, not 12 hours past."""
    _limit_stopped(workflow, "4pm (America/New_York)", limit=limit)

    early = workflow.run_script("tick", env=_at(4, 0))
    late = workflow.run_script("tick", env=_at(16, 1))

    assert early.returncode == 0, early.stderr
    assert late.returncode == 0, late.stderr
    assert _resent(workflow) == 0


def test_another_limit_is_answered_when_its_line_names_the_day(workflow: Workflow) -> None:
    _limit_stopped(workflow, "Oct 15, 1am (America/New_York)", limit="weekly limit")

    workflow.run_script("tick", env=_at(0, 59, day=15))
    before = _resent(workflow)
    workflow.run_script("tick", env=_at(1, 0, day=15))
    workflow.run_script("tick", env=_at(1, 3, day=15))

    assert before == 0
    assert _resent(workflow) == 1


def test_the_same_hour_on_the_next_day_is_another_reset(workflow: Workflow) -> None:
    """The session went on, and a day later met a limit that resets at the same hour."""
    _limit_stopped(workflow, "4pm (America/New_York)")
    workflow.run_script("tick", env=_at(16, 1))

    workflow.run_script("tick", env=_at(10, 0, day=14))
    workflow.run_script("tick", env=_at(15, 59, day=14))
    before = _resent(workflow)
    workflow.run_script("tick", env=_at(16, 0, day=14))
    workflow.run_script("tick", env=_at(16, 3, day=14))

    assert before == 1
    assert _resent(workflow) == 2
    mark = workflow.notes(7) / "ticket-7.resent"
    assert mark.read_text() == _at(16, 0, day=14)["WORKFLOW_NOW"] + "\n"


def test_a_line_without_a_zone_is_read_by_the_clock_of_the_machine(workflow: Workflow) -> None:
    _limit_stopped(workflow, "4:10pm")
    berlin = ZoneInfo("Europe/Berlin")

    workflow.run_script("tick", env={**_at(16, 9, zone=berlin), "TZ": "Europe/Berlin"})
    before = _resent(workflow)
    workflow.run_script("tick", env={**_at(16, 10, zone=berlin), "TZ": "Europe/Berlin"})

    assert before == 0
    assert _resent(workflow) == 1


def test_a_line_without_a_time_is_never_answered(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(f"> {_WORKER}\n  ⎿  You've hit your session limit\n{_BOX}", "ticket-7")

    done = workflow.run_script("tick", env=_at(16, 0))

    assert done.returncode == 0, done.stderr
    assert _resent(workflow) == 0


def test_an_audit_is_answered_once_and_a_hunt_is_not(workflow: Workflow) -> None:
    _limit_stopped(workflow, "4pm (America/New_York)", "audit-7")
    workflow.list_pull_request(7, draft=False)
    workflow.add_hunt("phone-1")
    workflow.running("hunt-phone-1")
    workflow.screen(_stopped("4pm (America/New_York)"), "hunt-phone-1")

    done = workflow.run_script("tick", env=_at(16, 1))
    again = workflow.run_script("tick", env=_at(16, 4))

    assert done.returncode == 0, done.stderr
    assert again.returncode == 0, again.stderr
    assert _resent(workflow, "audit-7") == 1
    assert _resent(workflow, "hunt-phone-1") == 0


def test_a_session_stopped_on_the_limit_is_squeezed_like_an_idle_one(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7)
    group = workflow.running("ticket-7", held=900 * _MB)
    workflow.screen(_stopped("4:10pm (America/New_York)"), "ticket-7")

    done = workflow.run_script("tick", env=_at(15, 0))

    assert done.returncode == 0, done.stderr
    assert (group / "memory.reclaim").read_text() == str(900 * _MB)


def test_a_worker_stopped_on_the_limit_after_it_said_worker_finished_is_not_stopped(
    workflow: Workflow,
) -> None:
    """Its pull request is ready, the stop hook marked it idle, and the limit met it after."""
    workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    notes = workflow.notes(7)
    notes.mkdir(parents=True)
    (notes / "ticket-7.idle").touch()
    workflow.screen(
        "● Worker finished.\n  ⎿  You've hit your session limit · resets 4pm\n" + _BOX, "ticket-7"
    )

    done = workflow.run_script("tick", env=_at(15, 0))

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")


def test_the_draft_of_a_worker_stopped_on_the_limit_gets_no_second_worker(
    workflow: Workflow,
) -> None:
    _limit_stopped(workflow, "4pm (America/New_York)")

    done = workflow.run_script("tick", env=_at(15, 0))

    assert done.returncode == 0, done.stderr
    assert [call for call in workflow.calls("tmux") if " new-session " in call] == []


def test_a_resend_that_fails_is_reported_and_retried(workflow: Workflow) -> None:
    _limit_stopped(workflow, "4pm (America/New_York)")
    (workflow.state / "send-keys-fails").touch()

    first = workflow.run_script("tick", env=_at(16, 1))
    assert not (workflow.notes(7) / "ticket-7.resent").exists()
    (workflow.state / "send-keys-fails").unlink()
    second = workflow.run_script("tick", env=_at(16, 4))

    assert first.returncode == 1
    assert second.returncode == 0, second.stderr
    assert len(workflow.calls("tmux ticket-7 send-keys")) == 3


def test_a_session_with_an_unsent_text_in_its_box_is_reported_and_left(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    rule = "─" * 40
    workflow.screen(
        f"  ⎿  You've hit your session limit · resets 4pm (America/New_York)\n"
        f"{rule}\n> wait, do 9 first\n{rule}\n",
        "ticket-7",
    )

    first = workflow.run_script("tick", env=_at(16, 1))
    second = workflow.run_script("tick", env=_at(16, 4))

    assert first.returncode == 1
    assert "has text in its box" in first.stderr
    assert second.returncode == 1, second.stderr
    assert workflow.calls("tmux ticket-7 send-keys") == []


def test_the_resent_sessions_are_told_when_the_start_loop_cannot_list_again(
    workflow: Workflow,
) -> None:
    """The wake runs after the starts; a failed listing among them must not skip it."""
    _limit_stopped(workflow, "4pm (America/New_York)")
    workflow.add_pull_request(8)
    workflow.add_pull_request(9)
    workflow.orchestrator("idle")
    real = workflow.script.with_name("session-real")
    workflow.script.rename(real)
    workflow.script.write_text(
        "#!/bin/bash\n"
        f'[[ "$1 $2" != "start worker" ]] || touch "{workflow.state}/fail-gh-list"\n'
        f'exec bash "{real}" "$@"\n'
    )
    workflow.script.chmod(0o755)

    done = workflow.run_script("tick", env=_at(16, 1))

    assert done.returncode == 1
    assert "cannot list the pull requests" in done.stderr
    assert _resent(workflow) == 1
    assert any("Sent their instruction again: ticket-7." in prompt for prompt in workflow.sent())
