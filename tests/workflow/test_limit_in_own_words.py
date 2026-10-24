"""A session that wrote its own closing message about the usage limit is stopped on the limit.

One night no screen showed Claude's line "You've hit your session limit":
each worker wrote a message of its own and stopped, the stop hook marked it idle, and nothing
woke it. The two messages below are of that kind.
"""

import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workflow_harness import SESSION_LINK, Workflow, instruction

_TICKET_94 = (
    "Your usage limit was reached, so I am stopping here. The ticket is built and committed in "
    "the worktree ticket-94, but nothing is pushed and the review has not run.\n\n"
    "  Done\n"
    "  - Data per user: every route reads and writes only the signed-in user's records.\n"
    "  Left\n"
    "  - The test coverage reviewer and the Codex review, and fixes for what they find.\n\n"
    "  Pull request 94 stays a draft."
)
_TICKET_99 = (
    "The usage limit was reached, so I am stopping here with pull request 99 about two thirds "
    "built and still a draft. Everything committed is pushed (head b875d2ce); the push hook ran "
    "the whole server suite: 4,723 passed, 26 skipped.\n\n"
    "  Not done\n"
    "  - Browser tests: none written, and the suite was not run."
)
_NEW_YORK = ZoneInfo("America/New_York")
_BOX = "\n" + "─" * 40 + "\n\u276f \n" + "─" * 40 + "\n  ▰ USE: 5H: 100% 35m │ WK: 41% 2d\n"
# The window that stopped them reset at 21:10 New York time, 01:10 UTC.
_RESET = "2026-10-14T01:10:00.318040+00:00"
_RESET_SECONDS = int(datetime(2026, 10, 14, 1, 10, tzinfo=UTC).timestamp())


def _stop(message: str) -> dict[str, object]:
    return {"hook_event_name": "Stop", "last_assistant_message": message}


def _at(hour: int, minute: int = 0, day: int = 13) -> dict[str, str]:
    moment = datetime(2026, 10, day, hour, minute, tzinfo=_NEW_YORK)
    return {"WORKFLOW_NOW": str(int(moment.timestamp())), "TZ": "UTC"}


def _screen(message: str) -> str:
    """The end of the screen, as `tmux capture-pane` printed it that night."""
    shown = message.replace("\n", "\n  ")
    worked = "✻ Worked for 24m 31s · done 8:35 PM"
    return f"> {instruction('Implement pull request 7')}\n\n● {shown}\n\n{worked}\n{_BOX}"


@pytest.fixture
def session(workflow: Workflow) -> Workflow:
    workflow.add_worktree(7)
    workflow.set_draft(7, draft=True)
    workflow.notes(7).mkdir(parents=True)
    (workflow.notes(7) / "ticket-7.url").write_text(SESSION_LINK + "\n")
    keys = workflow.root / "home" / "agents" / ".env"
    keys.parent.mkdir()
    keys.write_text("PUSHOVER_USER=keyOfTheOwner\nPUSHOVER_TOKEN=tokenOfTheApplication\n")
    return workflow


def _limit_note(workflow: Workflow, name: str = "ticket-7") -> Path:
    return workflow.notes(7) / f"{name}.limit"


@pytest.mark.parametrize("message", [_TICKET_94, _TICKET_99])
def test_the_stop_hook_notes_the_reset_and_does_not_ring(session: Workflow, message: str) -> None:
    session.usage(100, _RESET)

    done = session.notify(_stop(message), env=_at(20, 35))

    assert done.returncode == 0
    assert session.pushes() == []
    assert (session.notes(7) / "ticket-7.idle").exists()
    # A minute after the reset: sent while the window is still full, it would stop again.
    assert _limit_note(session).read_text() == f"{_RESET_SECONDS + 60}\n"


@pytest.mark.parametrize(
    "message",
    [
        # A limit of the application, and a question to the owner.
        "The test hit the attempt limit, so I stopped it. Which limit do you want: 10 or 20?",
        # The limit in a later sentence: the session is reporting, not stopping on it.
        "Which wording do you want? Last night the usage limit was reached and I stopped. "
        "Reply 1 or 2.",
        # A limit reached, without a stop.
        "The usage limit was reached once tonight. Which limit do you want: 10 or 20?",
    ],
)
def test_a_session_that_only_mentions_a_limit_rings_as_before(
    session: Workflow, message: str
) -> None:
    session.usage(100, _RESET)

    session.notify(_stop(message), env=_at(20, 35))

    assert len(session.pushes()) == 1
    assert not _limit_note(session).exists()


def test_the_words_alone_are_not_enough_when_the_usage_is_far_from_the_limit(
    session: Workflow,
) -> None:
    """Codex has a limit of its own, and a session may stop on that one."""
    # Ready, a worker rings for any stop but "worker finished", so the ring shows the stop was
    # not taken as the usage limit. On a draft this report would be silent either way.
    session.set_draft(7, draft=False)
    session.usage(40, _RESET, seven=50)

    session.notify(_stop(_TICKET_94), env=_at(20, 35))

    assert len(session.pushes()) == 1
    assert not _limit_note(session).exists()


def test_a_full_weekly_window_gives_the_weekly_reset(session: Workflow) -> None:
    session.usage(60, _RESET, seven=100, seven_resets="2026-10-16T07:00:00+00:00")

    session.notify(_stop(_TICKET_94), env=_at(20, 35))

    weekly = int(datetime(2026, 10, 16, 7, 1, tzinfo=UTC).timestamp())
    assert _limit_note(session).read_text() == f"{weekly}\n"


def test_without_the_usage_the_reset_is_taken_5_hours_on(session: Workflow) -> None:
    now = _at(20, 35)

    session.notify(_stop(_TICKET_99), env=now)

    assert session.pushes() == []
    expected = int(now["WORKFLOW_NOW"]) + 5 * 3600 + 60
    assert int(_limit_note(session).read_text()) == expected


def test_the_next_prompt_forgets_the_limit(session: Workflow) -> None:
    session.usage(100, _RESET)
    session.notify(_stop(_TICKET_94), env=_at(20, 35))

    session.notify({"hook_event_name": "UserPromptSubmit", "source": "user_input"})

    assert not _limit_note(session).exists()


def test_a_hunt_is_not_noted(workflow: Workflow) -> None:
    workflow.add_hunt("night-1")
    workflow.usage(100, _RESET)

    workflow.notify(_stop(_TICKET_94), session="hunt-night-1", env=_at(20, 35))

    assert not (
        workflow.hunt_worktree("night-1") / ".local" / "workflow" / "hunt-night-1.limit"
    ).exists()


@pytest.mark.parametrize("message", [_TICKET_94, _TICKET_99])
def test_session_list_says_limit_and_when_it_resets(session: Workflow, message: str) -> None:
    session.usage(100, _RESET)
    session.notify(_stop(message), env=_at(20, 35))
    session.running("ticket-7")
    session.screen(_screen(message), "ticket-7")

    done = session.run("list")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "ticket-7  limit  0 MB held  0 MB in swap  usage limit  resets Oct 14, 2026, 1:11am (UTC)"
    ]


def test_a_note_without_the_idle_mark_is_not_a_stop(session: Workflow) -> None:
    """The session was given a prompt; the hook that forgets the note may not have run yet."""
    _limit_note(session).write_text(f"{_RESET_SECONDS}\n")
    session.running("ticket-7")

    done = session.run("list")

    assert done.stdout.splitlines() == ["ticket-7  busy  0 MB held  0 MB in swap"]


def test_the_tick_sends_the_instruction_again_once_after_the_reset(session: Workflow) -> None:
    session.usage(100, _RESET)
    session.notify(_stop(_TICKET_94), env=_at(20, 35))
    session.running("ticket-7")
    session.screen(_screen(_TICKET_94), "ticket-7")
    typed = "tmux ticket-7 send-keys -t =ticket-7: -l "

    before = session.run_script("tick", env=_at(21, 9))
    sent_before = len(session.calls(typed))
    after = [session.run_script("tick", env=_at(21, minute)) for minute in (11, 14, 17)]

    assert before.returncode == 0, before.stderr
    assert sent_before == 0
    assert [done.returncode for done in after] == [0, 0, 0], [done.stderr for done in after]
    assert session.calls(typed) == [typed + instruction("Implement pull request 7")]


def test_the_tick_tells_the_orchestrator_whom_it_woke(session: Workflow) -> None:
    session.usage(100, _RESET)
    session.notify(_stop(_TICKET_94), env=_at(20, 35))
    session.running("ticket-7")
    session.screen(_screen(_TICKET_94), "ticket-7")
    session.orchestrator("idle")

    done = session.run_script("tick", env=_at(21, 11))

    assert done.returncode == 0, done.stderr
    [prompt] = session.sent()
    assert "Recipient: Check-in" in prompt
    assert "after a reset" in prompt
    assert "Sent their instruction again: ticket-7." in prompt
    assert "the tick sent its instruction again to: ticket-7" in session.wake_log.read_text()


@pytest.mark.parametrize(
    "message",
    [
        "I hit the usage limit, so I am stopping here.",
        "Stopping here: the usage limit was reached.",
        "**Usage limit reached.** I am stopping.",
        "The 5-hour limit was hit, so I'm stopping here with the tests half written.",
    ],
)
def test_other_ways_of_saying_it_are_noted_too(session: Workflow, message: str) -> None:
    session.usage(100, _RESET)

    session.notify(_stop(message), env=_at(20, 35))

    assert session.pushes() == []
    assert _limit_note(session).exists()


def test_another_limit_reached_and_a_test_stopped_is_a_question_that_rings(
    session: Workflow,
) -> None:
    """Found by the review: at 95% this matched the first, looser rule."""
    session.usage(95, _RESET)

    session.notify(
        _stop("The import attempt limit was reached, so I stopped the test. Raise the limit?"),
        env=_at(20, 35),
    )

    assert len(session.pushes()) == 1
    assert not _limit_note(session).exists()


def test_a_weekly_window_fuller_than_the_5_hour_one_gives_its_reset(session: Workflow) -> None:
    session.usage(50, _RESET, seven=95, seven_resets="2026-10-16T07:00:00+00:00")

    session.notify(_stop(_TICKET_94), env=_at(20, 35))

    weekly = int(datetime(2026, 10, 16, 7, 1, tzinfo=UTC).timestamp())
    assert _limit_note(session).read_text() == f"{weekly}\n"


def test_an_audit_is_noted_in_the_worktree_of_its_pull_request(session: Workflow) -> None:
    session.usage(100, _RESET)

    session.notify(_stop(_TICKET_94), session="audit-7", env=_at(20, 35))

    assert _limit_note(session, "audit-7").read_text() == f"{_RESET_SECONDS + 60}\n"
    assert session.pushes() == []


def test_a_reset_that_has_passed_is_tried_again_10_minutes_on(session: Workflow) -> None:
    """The figures can be minutes old: they still name the reset the session was woken after."""
    session.usage(100, _RESET)
    now = _at(21, 12)

    session.notify(_stop(_TICKET_94), env=now)

    assert int(_limit_note(session).read_text()) == int(now["WORKFLOW_NOW"]) + 600 + 60


def test_a_session_that_stops_again_after_its_resend_is_woken_again(session: Workflow) -> None:
    """Woken at 21:11, it stops at 21:12 on figures that still say 21:10: it is not parked."""
    session.usage(100, _RESET)
    session.notify(_stop(_TICKET_94), env=_at(20, 35))
    session.running("ticket-7")
    session.screen(_screen(_TICKET_94), "ticket-7")
    typed = "tmux ticket-7 send-keys -t =ticket-7: -l "
    session.run_script("tick", env=_at(21, 11))
    session.notify({"hook_event_name": "UserPromptSubmit", "source": "user_input"})
    session.notify(_stop(_TICKET_94), env=_at(21, 12))

    early = session.run_script("tick", env=_at(21, 20))
    sent_early = len(session.calls(typed))
    session.run_script("tick", env=_at(21, 23))

    assert early.returncode == 0, early.stderr
    assert sent_early == 1
    assert len(session.calls(typed)) == 2


def test_the_tick_reads_the_reset_on_a_machine_clock_that_is_not_utc(session: Workflow) -> None:
    session.usage(100, _RESET)
    session.notify(_stop(_TICKET_94), env=_at(20, 35))
    session.running("ticket-7")
    session.screen(_screen(_TICKET_94), "ticket-7")
    typed = "tmux ticket-7 send-keys -t =ticket-7: -l "
    new_york = {"TZ": "America/New_York"}

    session.run_script("tick", env={**_at(21, 10), **new_york})
    before = len(session.calls(typed))
    session.run_script("tick", env={**_at(21, 11), **new_york})

    assert before == 0
    assert len(session.calls(typed)) == 1


# The stand-in announces readiness, then waits for the controller. Without a timeout wrapper it
# completes immediately: a broken hook fails without hanging. It sets no trap on TERM: bash runs a
# trap only between commands, so a TERM that lands just before `read` blocks would leave it
# waiting for a release that never comes. Unhandled, TERM ends it wherever it is.
_CONTROLLED_CALL = """#!/bin/bash
name=@TOOL@
echo started-$name >> "@STATE@/calls.log"
if [[ -n ${WORKFLOW_READY_FD:-} ]]; then
  echo "ready $name" >&"$WORKFLOW_READY_FD"
  read -r release <&"$WORKFLOW_RELEASE_FD"
fi
echo completed-$name >> "@STATE@/calls.log"
"""


def test_the_hook_ends_within_its_10_seconds_when_everything_answers_slowly(
    session: Workflow,
) -> None:
    """Each deadline expires once its call is ready, rather than racing runner scheduling.

    Usage completes at 50%, then GitHub and Pushover are interrupted. The sum of the
    requested deadlines proves the hook's budget; 30-second waits only catch deadlocks.
    The stand-in for `timeout` ends the call itself, as the real one does when its deadline
    passes: the real one, sent its alarm this early, can exit before it knows its child.
    ControlledTimeouts serves asyncio scopes, so subprocesses need a pipe handshake instead.
    """
    session.fake(
        "timeout",
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import os
            import select
            import signal
            import subprocess
            import sys

            with open("@STATE@/calls.log", "a") as log:
                log.write(f"timeout {sys.argv[1]}\\n")
            assert float(sys.argv[1]) > 0, "Deadline must be enabled"
            ready, announce = os.pipe()
            release, proceed = os.pipe()
            child = subprocess.Popen(
                sys.argv[2:],
                env={**os.environ, "WORKFLOW_READY_FD": str(announce),
                     "WORKFLOW_RELEASE_FD": str(release)},
                pass_fds=(announce, release), start_new_session=True,
            )
            os.close(announce)
            os.close(release)
            try:
                assert select.select([ready], [], [], 30)[0], "Stand-in never became ready"
                word, name = os.read(ready, 100).decode().split()
                assert word == "ready", word
                if name == "usage":
                    os.write(proceed, b"continue\\n")
                    sys.exit(child.wait(timeout=30))
                # As GNU timeout does: TERM to the call's whole group, and 124 once it ended.
                os.killpg(child.pid, signal.SIGTERM)
                if child.wait(timeout=30) == -signal.SIGTERM:
                    with open("@STATE@/calls.log", "a") as log:
                        log.write(f"interrupted-{name}\\n")
                sys.exit(124)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                os.close(ready)
                os.close(proceed)
            """
        ),
    )
    usage = session.script.with_name("usage")
    usage.write_text(
        _CONTROLLED_CALL.replace("@TOOL@", "usage").replace("@STATE@", str(session.state))
        + f"echo five_hour 50 {_RESET_SECONDS}\n"
    )
    for tool in ("gh", "curl"):
        session.fake(tool, _CONTROLLED_CALL)

    done = session.notify(_stop(_TICKET_94), env=_at(20, 35))

    assert len(session.calls("timeout ")) == 3
    assert 0 < session.timeout_budget() <= 9.5
    assert done.returncode == 0
    assert session.calls("started-") == ["started-usage", "started-gh", "started-curl"]
    assert session.calls("completed-") == ["completed-usage"]
    assert session.calls("interrupted-") == ["interrupted-gh", "interrupted-curl"]
    assert session.missed_pushes.exists()
    assert not _limit_note(session).exists()
