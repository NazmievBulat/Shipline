"""An hour-only Claude limit gets its day from usage and survives the figures rolling over."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from workflow_harness import Workflow, instruction

_NEW_YORK = ZoneInfo("America/New_York")
_BOX = "\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n"
_WEEKLY_RESET = "2026-10-16T07:00:00+00:00"


def _at(day: int, hour: int, minute: int = 0) -> dict[str, str]:
    moment = datetime(2026, 10, day, hour, minute, tzinfo=_NEW_YORK)
    return {"WORKFLOW_NOW": str(int(moment.timestamp())), "TZ": "UTC"}


def _line(limit: str = "weekly limit", resets: str = "3am (America/New_York)") -> str:
    return f"You've hit your {limit} · resets {resets}"


def _stopped(workflow: Workflow, name: str = "ticket-7", line: str | None = None) -> str:
    workflow.add_worktree(7)
    workflow.running(name)
    message = line or _line()
    workflow.screen(f"  ⎿  {message}\n{_BOX}", name)
    return message


def _usage(workflow: Workflow) -> None:
    workflow.usage(40, "2026-10-16T05:00:00+00:00", seven=100, seven_resets=_WEEKLY_RESET)


def _resent(workflow: Workflow, name: str = "ticket-7") -> list[str]:
    return workflow.calls(f"tmux {name} send-keys -t ={name}: -l ")


def _trace_usage(workflow: Workflow) -> None:
    script = workflow.script.with_name("usage")
    script.write_text(
        script.read_text().replace(
            "#!/usr/bin/env bash\n",
            f'#!/usr/bin/env bash\necho usage >> "{workflow.state}/calls.log"\n',
            1,
        )
    )


@pytest.mark.parametrize(
    ("name", "task"),
    [("ticket-7", "Implement"), ("audit-7", "Audit"), ("review-7", "Review")],
)
def test_weekly_reset_is_noted_then_resent_once_and_told_to_the_wake(
    workflow: Workflow, name: str, task: str
) -> None:
    message = _stopped(workflow, name)
    _usage(workflow)
    workflow.orchestrator("idle")

    stopped = workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": message},
        session=name,
        env=_at(15, 23),
    )

    assert stopped.returncode == 0, stopped.stderr
    assert workflow.pushes() == []
    assert not workflow.missed_pushes.exists()
    note = workflow.notes(7) / f"{name}.limit"
    assert note.exists()
    assert int(note.read_text()) == int(_at(16, 3, 1)["WORKFLOW_NOW"])
    listed = workflow.run("list", env=_at(16, 2, 59))
    assert "usage limit  resets Oct 16, 2026, 7:01am (UTC)" in listed.stdout
    before = workflow.run_script("tick", env=_at(16, 3))
    assert before.returncode == 0, before.stderr
    assert _resent(workflow, name) == []

    # At the reset, the current figures already describe next week's window. The saved
    # moment, rather than a new usage lookup, must wake the stopped session.
    workflow.usage(10, "2026-10-16T12:00:00+00:00", seven=0, seven_resets="2026-10-23T07:00:00Z")
    after = workflow.run_script("tick", env=_at(16, 3, 1))
    again = workflow.run_script("tick", env=_at(16, 3, 4))

    assert after.returncode == 0, after.stderr
    assert again.returncode == 0, again.stderr
    assert _resent(workflow, name) == [
        f"tmux {name} send-keys -t ={name}: -l " + instruction(f"{task} pull request 7")
    ]
    assert any(f"Sent their instruction again: {name}." in prompt for prompt in workflow.sent())


def test_a_weekly_line_without_a_stop_hook_is_saved_before_the_reset(workflow: Workflow) -> None:
    _stopped(workflow)
    _usage(workflow)

    before = workflow.run_script("tick", env=_at(15, 23))
    assert before.returncode == 0, before.stderr
    assert _resent(workflow) == []
    assert (workflow.notes(7) / "ticket-7.limit").exists()

    # An unavailable usage service after the reset cannot lose a reset already read.
    (workflow.root / "home/.claude/MEMORY/STATE/usage-cache.json").unlink()
    after = workflow.run_script("tick", env=_at(16, 3, 1))
    again = workflow.run_script("tick", env=_at(16, 3, 4))

    assert after.returncode == 0, after.stderr
    assert again.returncode == 0, again.stderr
    assert len(_resent(workflow)) == 1


@pytest.mark.parametrize("via_hook", [True, False])
@pytest.mark.parametrize(
    ("seven", "reset"),
    [
        (100, "2026-10-23T07:00:00Z"),
        (0, "2026-10-23T07:00:00Z"),
        (0, _WEEKLY_RESET),
        (89, _WEEKLY_RESET),
    ],
)
def test_a_weekly_line_rejects_next_weeks_reset_and_a_window_below_the_stop_percentage(
    workflow: Workflow, via_hook: bool, seven: int, reset: str
) -> None:
    message = _stopped(workflow)
    workflow.usage(100, "2026-10-16T08:00:00Z", seven=seven, seven_resets=reset)
    now = _at(16, 3)
    now["WORKFLOW_NOW"] = str(int(now["WORKFLOW_NOW"]) + 30)
    if via_hook:
        stopped = workflow.notify(
            {"hook_event_name": "Stop", "last_assistant_message": message}, env=now
        )
        assert stopped.returncode == 0, stopped.stderr
        assert not (workflow.notes(7) / "ticket-7.limit").exists()
        assert not (workflow.notes(7) / "ticket-7.limit-line").exists()

    listed = workflow.run("list", env=now)
    tick = workflow.run_script("tick", env=now)

    assert listed.returncode == 0, listed.stderr
    assert "weekly limit  resets 3am (America/New_York)" in listed.stdout
    assert tick.returncode == 0, tick.stderr
    assert _resent(workflow) == []
    assert not (workflow.notes(7) / "ticket-7.limit").exists()
    assert not (workflow.notes(7) / "ticket-7.limit-line").exists()


@pytest.mark.parametrize(("seconds", "accepted"), [(0, True), (1, False)])
def test_an_hour_only_reset_is_at_most_a_day_and_an_hour_ahead(
    workflow: Workflow, seconds: int, accepted: bool
) -> None:
    _stopped(workflow)
    workflow.usage(
        40,
        "2026-10-16T08:00:00Z",
        seven=100,
        seven_resets=f"2026-10-17T07:00:0{seconds}Z",
    )

    listed = workflow.run("list", env=_at(16, 2))

    assert listed.returncode == 0, listed.stderr
    assert ("usage limit  resets" in listed.stdout) is accepted
    assert (workflow.notes(7) / "ticket-7.limit").exists() is accepted
    assert (workflow.notes(7) / "ticket-7.limit-line").exists() is accepted


def test_a_codex_bridge_stop_on_claudes_weekly_limit_is_noted_without_a_push(
    workflow: Workflow,
) -> None:
    message = _stopped(workflow)
    workflow.set_draft(7, draft=True)
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / "ticket-7.codex").touch()
    keys = workflow.root / "home" / "agents" / ".env"
    keys.parent.mkdir()
    keys.write_text("PUSHOVER_USER=testUser\nPUSHOVER_TOKEN=testToken\n")
    _usage(workflow)

    stopped = workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": message}, env=_at(15, 23)
    )

    assert stopped.returncode == 0, stopped.stderr
    assert workflow.pushes() == []
    assert not workflow.missed_pushes.exists()
    note = workflow.notes(7) / "ticket-7.limit"
    assert note.exists()
    assert int(note.read_text()) == int(_at(16, 3, 1)["WORKFLOW_NOW"])


def test_a_codex_bridge_stopped_on_claudes_weekly_limit_is_resent_once(workflow: Workflow) -> None:
    _stopped(workflow)
    workflow.running("codex-7")
    workflow.screen("• Waiting for the owner.\n\u203a \n", "codex-7")
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / "ticket-7.codex").touch()
    _usage(workflow)

    before = workflow.run_script("tick", env=_at(16, 2, 59))
    assert before.returncode == 0, before.stderr
    assert _resent(workflow) == []
    assert not workflow.calls("tmux codex-7 send-keys")

    after = workflow.run_script("tick", env=_at(16, 3, 1))
    again = workflow.run_script("tick", env=_at(16, 3, 4))

    assert after.returncode == 0, after.stderr
    assert again.returncode == 0, again.stderr
    assert _resent(workflow) == ["tmux ticket-7 send-keys -t =ticket-7: -l __WORKFLOW_CODEX_TURN__"]
    assert not workflow.calls("tmux codex-7 send-keys")


def test_a_slow_usage_lookup_leaves_the_session_waiting_within_three_seconds(
    workflow: Workflow,
) -> None:
    workflow.trace_timeouts()
    _stopped(workflow)
    workflow.script.with_name("usage").write_text(
        f'#!/bin/bash\necho usage >> "{workflow.state}/calls.log"\n'
        # A second TERM, to the group, is ignored, as by HANGING_CALL.
        'trap \'trap "" TERM; '
        f"echo interrupted-usage >> {workflow.state}/calls.log; exit 124' TERM\n"
        "/bin/sleep 600 & wait\n"
        f"echo seven_day 100 {int(_at(16, 3)['WORKFLOW_NOW'])}\n"
    )

    listed = workflow.run("list", env=_at(16, 2, 59))

    assert 0 < workflow.timeout_budget() <= 1.5
    assert workflow.calls("interrupted-usage") == ["interrupted-usage"]
    assert listed.returncode == 0, listed.stderr
    assert "weekly limit  resets 3am (America/New_York)" in listed.stdout
    assert workflow.calls("usage") == ["usage"]
    assert not (workflow.notes(7) / "ticket-7.limit").exists()
    assert not (workflow.notes(7) / "ticket-7.limit-line").exists()


@pytest.mark.parametrize("via_hook", [True, False])
def test_a_session_limit_never_reads_usage_or_uses_the_weekly_reset(
    workflow: Workflow, via_hook: bool
) -> None:
    message = _stopped(workflow, line=_line("session limit", "4:10pm (America/New_York)"))
    workflow.usage(100, "2026-10-16T20:10:00Z", seven=100, seven_resets=_WEEKLY_RESET)
    cache = workflow.root / "home/.claude/MEMORY/STATE/usage-cache.json"
    now = int(_at(16, 12)["WORKFLOW_NOW"])
    os.utime(cache, (now, now))
    _trace_usage(workflow)
    if via_hook:
        stopped = workflow.notify(
            {"hook_event_name": "Stop", "last_assistant_message": message}, env=_at(16, 12)
        )
        assert stopped.returncode == 0, stopped.stderr
        assert workflow.calls("usage") == []
        assert not (workflow.notes(7) / "ticket-7.limit").exists()
        assert not (workflow.notes(7) / "ticket-7.limit-line").exists()

    listed = workflow.run("list", env=_at(16, 12))
    assert listed.returncode == 0, listed.stderr
    assert "session limit  resets 4:10pm (America/New_York)" in listed.stdout
    assert workflow.calls("usage") == []
    assert not (workflow.notes(7) / "ticket-7.limit").exists()
    assert not (workflow.notes(7) / "ticket-7.limit-line").exists()

    for hour, minute in [(12, 0), (16, 9)]:
        before = workflow.run_script("tick", env=_at(16, hour, minute))
        assert before.returncode == 0, before.stderr
        assert _resent(workflow) == []

    after = workflow.run_script("tick", env=_at(16, 16, 10))
    again = workflow.run_script("tick", env=_at(16, 16, 13))

    assert after.returncode == 0, after.stderr
    assert again.returncode == 0, again.stderr
    assert _resent(workflow) == [
        "tmux ticket-7 send-keys -t =ticket-7: -l " + instruction("Implement pull request 7")
    ]
    assert not (workflow.notes(7) / "ticket-7.limit").exists()
    assert not (workflow.notes(7) / "ticket-7.limit-line").exists()


@pytest.mark.parametrize("via_hook", [True, False])
@pytest.mark.parametrize(
    ("figures", "resets"),
    [
        ("missing", "3am (America/New_York)"),
        ("unreadable", "3am (America/New_York)"),
        ("mismatch", "3am (America/New_York)"),
        ("valid", "3am (Nowhere/Atall)"),
        ("valid", "3am (../../etc/passwd)"),
        ("valid", "25pm (America/New_York)"),
        ("valid", "soon"),
    ],
)
def test_unreadable_or_disagreeing_resets_wait_as_before(
    workflow: Workflow, via_hook: bool, figures: str, resets: str
) -> None:
    message = _stopped(workflow, line=_line(resets=resets))
    if figures != "missing":
        weekly = {
            "unreadable": "not a date",
            "mismatch": "2026-10-16T10:00:00Z",
            "valid": _WEEKLY_RESET,
        }[figures]
        workflow.usage(100, "2026-10-16T07:00:00Z", seven=100, seven_resets=weekly)
    if via_hook:
        workflow.notify(
            {"hook_event_name": "Stop", "last_assistant_message": message}, env=_at(15, 23)
        )

    before = workflow.run_script("tick", env=_at(16, 2, 59))
    after = workflow.run_script("tick", env=_at(16, 6))

    assert before.returncode == 0, before.stderr
    assert after.returncode == 0, after.stderr
    assert _resent(workflow) == []
    assert not (workflow.notes(7) / "ticket-7.limit").exists()
    assert not (workflow.notes(7) / "ticket-7.limit-line").exists()


@pytest.mark.parametrize(
    ("line", "reset", "zone", "accepted"),
    [
        ("2am (America/New_York)", _WEEKLY_RESET, "UTC", True),
        ("4am (America/New_York)", _WEEKLY_RESET, "UTC", True),
        ("1:59am (America/New_York)", _WEEKLY_RESET, "UTC", False),
        ("4:01am (America/New_York)", _WEEKLY_RESET, "UTC", False),
        ("3am", _WEEKLY_RESET, "America/New_York", True),
        ("7am (UTC)", _WEEKLY_RESET, "America/New_York", True),
        # Clock comparison is circular, and hours with a leading zero are decimal.
        ("11:45pm (UTC)", "2026-10-16T00:15:00Z", "UTC", True),
        ("12:15am (UTC)", "2026-10-16T23:45:00Z", "UTC", True),
        ("08:05am (UTC)", "2026-10-16T08:05:00Z", "UTC", True),
    ],
)
def test_clock_agreement_uses_the_lines_zone_and_one_hour_tolerance(
    workflow: Workflow, line: str, reset: str, zone: str, accepted: bool
) -> None:
    _stopped(workflow, line=_line(resets=line))
    workflow.usage(40, "2026-10-16T05:00:00Z", seven=100, seven_resets=reset)

    done = workflow.run("list", env={**_at(15, 23), "TZ": zone})

    assert done.returncode == 0, done.stderr
    assert ("usage limit  resets" in done.stdout) is accepted


@pytest.mark.parametrize(
    ("limit", "five", "seven", "expected"),
    [
        ("Opus limit", 100, 40, "5:01am"),
        ("limit", 40, 100, "7:01am"),
        ("Opus limit", 100, 100, "7:01am"),
        ("Opus limit", 95, 90, None),
        ("weekly limit", 100, 90, "7:01am"),
        ("weekly limit", 100, 95, "7:01am"),
    ],
)
def test_other_limit_names_select_a_full_window_and_weekly_selects_seven_day(
    workflow: Workflow, limit: str, five: int, seven: int, expected: str | None
) -> None:
    _stopped(workflow, line=_line(limit, "6am (UTC)"))
    workflow.usage(five, "2026-10-16T05:00:00Z", seven=seven, seven_resets=_WEEKLY_RESET)

    done = workflow.run("list", env=_at(15, 23))

    assert done.returncode == 0, done.stderr
    if expected is None:
        assert "usage limit" not in done.stdout
    else:
        assert f"usage limit  resets Oct 16, 2026, {expected} (UTC)" in done.stdout


def test_a_new_stop_at_the_same_hour_next_week_is_another_reset(workflow: Workflow) -> None:
    message = _stopped(workflow)
    _usage(workflow)
    workflow.run_script("tick", env=_at(15, 23))
    workflow.run_script("tick", env=_at(16, 3, 1))
    assert len(_resent(workflow)) == 1

    workflow.notify({"hook_event_name": "UserPromptSubmit"})
    assert not (workflow.notes(7) / "ticket-7.limit").exists()
    assert not (workflow.notes(7) / "ticket-7.limit-line").exists()
    workflow.usage(40, "2026-10-23T05:00:00Z", seven=100, seven_resets="2026-10-23T07:00:00Z")
    # Keep the stand-in cache fresh at the simulated stop next week.
    cache = workflow.root / "home/.claude/MEMORY/STATE/usage-cache.json"
    now = int(_at(22, 23)["WORKFLOW_NOW"])
    os.utime(cache, (now, now))
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": message}, env=_at(22, 23))
    workflow.run_script("tick", env=_at(23, 3))
    assert len(_resent(workflow)) == 1
    workflow.run_script("tick", env=_at(23, 3, 1))
    workflow.run_script("tick", env=_at(23, 3, 4))
    assert len(_resent(workflow)) == 2


def test_a_changed_limit_line_does_not_reuse_the_old_reset(workflow: Workflow) -> None:
    _stopped(workflow)
    _usage(workflow)
    workflow.run("list", env=_at(15, 23))
    workflow.screen(f"  ⎿  {_line(resets='8am (UTC)')}\n{_BOX}", "ticket-7")
    workflow.usage(40, "2026-10-16T05:00:00Z", seven=100, seven_resets="2026-10-16T08:00:00Z")

    before = workflow.run_script("tick", env=_at(16, 3, 1))
    assert before.returncode == 0, before.stderr
    assert _resent(workflow) == []
    after = workflow.run_script("tick", env=_at(16, 4, 1))

    assert after.returncode == 0, after.stderr
    assert len(_resent(workflow)) == 1


def test_a_weekly_hunt_is_never_noted_or_resent(workflow: Workflow) -> None:
    workflow.add_hunt("weekly")
    workflow.running("hunt-weekly")
    workflow.screen(f"  ⎿  {_line()}\n{_BOX}", "hunt-weekly")
    _usage(workflow)
    workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": _line()},
        session="hunt-weekly",
        env=_at(15, 23),
    )

    done = workflow.run_script("tick", env=_at(16, 3, 1))

    assert done.returncode == 0, done.stderr
    assert _resent(workflow, "hunt-weekly") == []
    assert not (workflow.hunt_worktree("weekly") / ".local/workflow/hunt-weekly.limit").exists()


def test_a_new_stop_with_expired_usage_retries_instead_of_reusing_the_resent_mark(
    workflow: Workflow,
) -> None:
    message = _stopped(workflow)
    _usage(workflow)
    workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": message}, env=_at(16, 2, 59)
    )
    workflow.run_script("tick", env=_at(16, 3, 1))
    assert len(_resent(workflow)) == 1

    workflow.notify({"hook_event_name": "UserPromptSubmit"}, env=_at(16, 3, 1))
    workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": message}, env=_at(16, 3, 2)
    )

    note = workflow.notes(7) / "ticket-7.limit"
    assert int(note.read_text()) == int(_at(16, 3, 13)["WORKFLOW_NOW"])
    workflow.usage(10, "2026-10-16T12:00:00Z", seven=0, seven_resets="2026-10-23T07:00:00Z")
    before = workflow.run_script("tick", env=_at(16, 3, 12))
    assert before.returncode == 0, before.stderr
    assert len(_resent(workflow)) == 1
    after = workflow.run_script("tick", env=_at(16, 3, 13))
    again = workflow.run_script("tick", env=_at(16, 3, 16))
    assert after.returncode == 0, after.stderr
    assert again.returncode == 0, again.stderr
    assert len(_resent(workflow)) == 2


def test_a_late_weekly_stop_hook_does_not_recreate_a_removed_worktree(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    workflow.git("worktree", "remove", "--force", str(worktree))
    _usage(workflow)

    done = workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": _line()}, env=_at(15, 23)
    )

    assert done.returncode == 0, done.stderr
    assert not worktree.exists()


@pytest.mark.parametrize("readable", [True, False])
def test_a_codex_bridge_relaying_codexs_own_limit_words_gets_no_claude_reset(
    workflow: Workflow, readable: bool
) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / "ticket-7.codex").touch()
    if readable:
        workflow.usage(100, "2026-10-16T05:00:00+00:00", seven=100, seven_resets=_WEEKLY_RESET)

    # Codex's usage is never inferred from Claude's figures, nor given an invented 5 hours.
    stopped = workflow.notify(
        {
            "hook_event_name": "Stop",
            "last_assistant_message": "Your usage limit was reached, so I am stopping here.",
        },
        env=_at(15, 23),
    )

    assert stopped.returncode == 0, stopped.stderr
    assert workflow.pushes() == []
    assert not (workflow.notes(7) / "ticket-7.limit").exists()
