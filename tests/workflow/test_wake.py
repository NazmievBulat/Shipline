"""`wake` gives the orchestrator session a check-in from outside it, and writes what it did."""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workflow_harness import Workflow

_NEW_YORK = ZoneInfo("America/New_York")


def _at(hour: int, minute: int = 0, day: int = 13) -> dict[str, str]:
    """The clock of the wake, read by the machine's clock in New York."""
    moment = datetime(2026, 10, day, hour, minute, tzinfo=_NEW_YORK)
    return {"WORKFLOW_NOW": str(int(moment.timestamp())), "TZ": "America/New_York"}


@pytest.fixture
def night(workflow: Workflow) -> Workflow:
    """Two sessions run, three pull requests are open, the usage is known."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.add_pull_request(9, draft=False)
    workflow.add_pull_request(12, labels=())
    workflow.usage(76, "2026-10-14T05:10:00+00:00", seven=41)
    return workflow


def _wake(
    workflow: Workflow, *woken: str, at: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return workflow.run_script("wake", *woken, env=at or _at(23, 0))


def test_at_night_an_idle_orchestrator_gets_a_check_in(night: Workflow) -> None:
    night.orchestrator("idle")

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    [prompt] = night.sent()
    assert "Recipient: Check-in" in prompt
    assert "Wake check-in, 23:00." in prompt
    assert "5-hour window 76% used, resets in 2h10m, weekly 41%" in prompt
    assert "Sessions: ticket-7 busy." in prompt
    assert "drafts 7; waiting tickets 12; ready medium 9 (checks passing). Log: ~/" in prompt
    assert "asking the owner" not in prompt
    assert "start nothing above 85% of the 5-hour window unless it resets within the hour" in prompt
    assert "never merge, never force-push, never edit code" in prompt


def test_a_merge_hand_back_is_told_once_at_any_hour_and_retained_on_failed_delivery(
    night: Workflow,
) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/merge-handbacks.pending"
    pending.mkdir(parents=True)
    event = pending / "one-event"
    event.write_text(json.dumps({"number": "9", "reason": "head changed after review"}))
    (night.state / "send-fails").touch()

    first = _wake(night, at=_at(12, 0))
    assert first.returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    second = _wake(night, at=_at(12, 5))
    third = _wake(night, at=_at(12, 10))

    assert second.returncode == third.returncode == 0
    assert "pull request 9: head changed after review" in night.sent()[-1]
    assert not event.exists()
    assert len(night.sent()) == 2  # one failed attempt, then one delivered message


def test_an_unreachable_advisory_service_is_told_once_at_any_hour(night: Workflow) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/advisory-service.pending"
    pending.write_text("npm's advisory service unreachable since 2026-10-22 02:46 UTC\n")
    (night.state / "send-fails").touch()

    assert _wake(night, at=_at(12, 0)).returncode == 1
    assert pending.exists()
    (night.state / "send-fails").unlink()
    second = _wake(night, at=_at(12, 5))
    third = _wake(night, at=_at(12, 10))

    assert second.returncode == third.returncode == 0
    assert (
        "Package audit: npm's advisory service unreachable since 2026-10-22 02:46 UTC; "
        "pushes wait for it, nothing was found wrong."
    ) in night.sent()[-1]
    assert not pending.exists()
    assert len(night.sent()) == 2  # one failed attempt, then one delivered message


def test_a_lost_check_is_told_once_at_any_hour_on_a_line_of_its_own(night: Workflow) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/lost-checks.pending"
    pending.mkdir(parents=True)
    event = pending / f"494-{'a' * 40}.json"
    event.write_text(
        json.dumps(
            {
                "number": "494",
                "head": "a" * 40,
                "check": "CI result",
                "run": 123,
                "conclusion": "success",
            }
        )
    )
    # A note the gate is still writing is not read.
    (pending / "tmpwriting").write_text("{")
    (night.state / "send-fails").touch()

    failed = _wake(night, at=_at(12, 0))
    assert failed.returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    first = _wake(night, at=_at(12, 5))
    second = _wake(night, at=_at(12, 10))

    assert first.returncode == second.returncode == 0, first.stderr
    [_, prompt] = night.sent()  # one failed attempt, then one delivered message
    assert "\nlost check: CI result of run 123 on pull request 494\n" in prompt
    assert not event.exists()
    assert (night.repo / ".local/workflow/lost-checks.sent" / event.name).exists()
    assert "  lost check: CI result of run 123 on pull request 494" in night.wake_log.read_text()


def test_a_rule_break_is_told_once_at_any_hour_on_a_line_of_its_own(night: Workflow) -> None:
    night.orchestrator("busy")
    pending = night.repo / ".local/workflow/rule-breaks.pending"
    pending.mkdir(parents=True)
    event = pending / f"review-9-{'b' * 40}.json"
    event.write_text(json.dumps({"name": "review-9", "number": "9", "head": "b" * 40}))
    # A note the tick is still writing is not read.
    (pending / f".review-9-{'c' * 40}.json.tmp").write_text("{")
    (night.state / "send-fails").touch()

    failed = _wake(night, at=_at(12, 0))
    assert failed.returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    first = _wake(night, at=_at(12, 5))
    second = _wake(night, at=_at(12, 10))

    assert first.returncode == second.returncode == 0, first.stderr
    [_, prompt] = night.sent()  # one failed attempt, then one delivered message
    line = "rule break: review-9 said review finished without a merge handoff"
    assert f"\n{line}\n" in prompt
    assert not event.exists()
    assert (night.repo / ".local/workflow/rule-breaks.sent" / event.name).exists()
    assert f"  {line}" in night.wake_log.read_text()


def test_the_message_goes_through_a_small_model_with_one_tool_and_no_settings(
    night: Workflow,
) -> None:
    night.orchestrator("idle")

    _wake(night)

    arguments = night.sent()[0].splitlines()
    assert arguments[0] == "-p"
    for pair in (
        ["--model", "claude-haiku-5-5"],
        ["--setting-sources", ""],
        ["--tools", "SendMessage"],
    ):
        at = arguments.index(pair[0])
        assert arguments[at : at + 2] == pair


def test_the_log_says_what_was_seen_and_what_was_done(night: Workflow) -> None:
    night.orchestrator("idle")

    _wake(night)

    log = night.wake_log.read_text().splitlines()
    assert log[0] == "2026-10-13 23:00:00  every 20 minutes"
    assert log[1] == (
        "  usage: 5-hour window 76% used, resets in 2h10m, weekly 41%;"
        " /tmp file slots: 10% used (100/1000);"
        " load 0.40 over 5 minutes, ceiling 6 on 4 processors; above it starts wait"
    )
    assert log[2] == (
        "  sessions: ticket-7 busy; pull requests: drafts 7; waiting tickets 12;"
        " ready medium 9 (checks passing); starts: allowed; tmp pressure: false"
    )
    assert log[3].startswith("  told Check-in: Wake check-in, 23:00.")


def test_nothing_new_is_not_told_twice(night: Workflow) -> None:
    night.orchestrator("idle")

    _wake(night, at=_at(23, 0))
    _wake(night, at=_at(23, 21))

    assert len(night.sent()) == 1
    assert night.wake_log.read_text().splitlines()[-1] == (
        "  nothing changed since the last check-in; Check-in not told"
    )


def test_the_check_in_groups_the_ready_pull_requests_by_priority(night: Workflow) -> None:
    night.orchestrator("idle")
    night.list_pull_request(7, draft=False, labels=("workflow", "priority:+low"))
    night.list_pull_request(9, draft=False, labels=("workflow", "priority:+high"))
    night.add_pull_request(13, draft=False, labels=("priority:+blocker",))
    night.add_pull_request(14, draft=False)

    _wake(night)

    [prompt] = night.sent()
    assert (
        "Open pull requests: waiting tickets 12; ready blocker 13 (checks passing);"
        " ready high 9 (checks passing); ready medium 14 (checks passing);"
        " ready low 7 (checks passing). Log: ~/"
    ) in prompt


def test_a_change_is_told_at_the_next_look(night: Workflow) -> None:
    night.orchestrator("idle")
    _wake(night, at=_at(23, 0))
    night.list_pull_request(7, draft=False)

    _wake(night, at=_at(23, 21))

    assert len(night.sent()) == 2
    assert "ready medium 7 (checks passing), 9 (checks passing)" in night.sent()[1]


def test_it_looks_every_20_minutes_and_not_at_each_tick(night: Workflow) -> None:
    night.orchestrator("idle")
    _wake(night, at=_at(23, 0))
    night.list_pull_request(7, draft=False)

    early = _wake(night, at=_at(23, 18))
    sent_early = len(night.sent())
    _wake(night, at=_at(23, 20))

    assert early.returncode == 0
    assert sent_early == 1
    assert len(night.sent()) == 2


@pytest.mark.parametrize(
    ("hour", "looks"), [(8, False), (12, False), (19, False), (20, True), (3, True)]
)
def test_it_looks_in_only_at_night(night: Workflow, hour: int, looks: bool) -> None:
    night.orchestrator("idle")

    done = _wake(night, at=_at(hour, 0))

    assert done.returncode == 0, done.stderr
    assert len(night.sent()) == int(looks)
    assert night.wake_log.exists() == looks


def test_a_busy_orchestrator_is_left_to_its_work(night: Workflow) -> None:
    night.orchestrator("busy")

    _wake(night)

    assert night.sent() == []
    assert night.wake_log.read_text().splitlines()[-1] == "  Check-in is busy; not told"


def test_after_a_reset_it_is_told_at_once_at_any_hour_and_even_when_busy(night: Workflow) -> None:
    night.orchestrator("busy")
    _wake(night, at=_at(9, 59))

    done = _wake(night, "ticket-7", "audit-9", at=_at(10, 0))

    assert done.returncode == 0, done.stderr
    [prompt] = night.sent()
    assert "Wake check-in, 10:00, after a reset." in prompt
    assert "Sent their instruction again: ticket-7 audit-9." in prompt
    log = night.wake_log.read_text()
    assert "2026-10-13 10:00:00  after a reset" in log
    assert "  the tick sent its instruction again to: ticket-7 audit-9" in log


def test_without_an_orchestrator_nobody_is_told_and_it_is_written_down(night: Workflow) -> None:
    done = _wake(night)

    assert done.returncode == 0, done.stderr
    assert night.sent() == []
    assert night.wake_log.read_text().splitlines()[-1] == (
        f"  orchestrator registration could not be read: "
        f"{night.repo}/.local/workflow/orchestrator; nobody was told"
    )


def test_a_message_that_was_not_delivered_fails_and_is_tried_at_the_next_look(
    night: Workflow,
) -> None:
    night.orchestrator("idle")
    (night.state / "send-fails").touch()

    failed = _wake(night, at=_at(23, 0))
    (night.state / "send-fails").unlink()
    again = _wake(night, at=_at(23, 20))

    assert failed.returncode == 1
    assert "could not be sent" in failed.stderr
    assert "  the check-in could not be sent to Check-in" in night.wake_log.read_text()
    assert again.returncode == 0, again.stderr
    assert len(night.sent()) == 2


def test_unreadable_usage_and_pull_requests_are_said_and_do_not_stop_the_check_in(
    workflow: Workflow,
) -> None:
    workflow.orchestrator("idle")
    (workflow.state / "fail-gh-list").touch()

    done = _wake(workflow)

    assert done.returncode == 0, done.stderr
    [prompt] = workflow.sent()
    assert "Usage: the usage could not be read; /tmp file slots: 10% used (100/1000);" in prompt
    assert "Sessions: none. Open pull requests: could not be read." in prompt


def test_it_starts_nothing_merges_nothing_and_types_into_no_session(night: Workflow) -> None:
    night.orchestrator("idle")

    _wake(night, "ticket-7")

    assert [call for call in night.calls("tmux") if " new-session " in call] == []
    assert [call for call in night.calls("tmux") if " send-keys " in call] == []
    assert [call for call in night.calls("gh") if " list " not in call] == []


def test_what_claude_prints_besides_the_answer_does_not_hide_a_delivery(night: Workflow) -> None:
    """Live, a warning on the error output made a delivered message count as not delivered."""
    night.orchestrator("idle")
    (night.state / "stderr-warning").touch()

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    assert len(night.sent()) == 1
    send_log = night.wake_log.with_name("wake.send.log").read_text()
    assert "Warning: a line that is not JSON" in send_log


def test_claude_is_given_no_standard_input(night: Workflow) -> None:
    night.orchestrator("idle")
    night.fake(
        "claude",
        (night.bin / "claude")
        .read_text()
        .replace(
            '[ "$1" = -p ] || exit 1', '[ "$1" = -p ] || exit 1\n[ ! -p /dev/stdin ] || exit 1'
        ),
    )

    done = _wake(night, "ticket-7")

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "answer",
    [
        # The tool refused: no such session.
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1",'
        '"name":"SendMessage"}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1",'
        '"is_error":true}]}}\n{"type":"result","is_error":false,"result":"sent"}',
        # The model said it sent the message without calling the tool.
        '{"type":"result","is_error":false,"result":"sent"}',
    ],
)
def test_a_message_the_tool_did_not_deliver_is_not_counted_as_told(
    night: Workflow, answer: str
) -> None:
    night.orchestrator("idle")
    (night.state / "answer").write_text(answer + "\n")
    night.fake(
        "claude",
        (night.bin / "claude")
        .read_text()
        .replace(
            '[ "$1" = -p ] || exit 1', '[ "$1" = -p ] || exit 1\ncat "@STATE@/answer"; exit 0'
        ),
    )

    failed = _wake(night, at=_at(23, 0))

    assert failed.returncode == 1
    assert "  the check-in could not be sent to Check-in" in night.wake_log.read_text()
    assert not night.wake_log.with_name("wake.told").exists()


def test_sessions_that_could_not_be_read_are_not_said_to_be_none(night: Workflow) -> None:
    night.orchestrator("idle")
    night.break_session("list")

    _wake(night)

    [prompt] = night.sent()
    assert "Sessions: could not be read." in prompt


def test_the_orchestrator_is_told_when_starting_becomes_allowed_again(night: Workflow) -> None:
    """Above 85% it waits to start anything; nothing else changes when the window resets."""
    night.orchestrator("idle")
    night.usage(90, "2026-10-14T05:10:00+00:00")
    _wake(night, at=_at(23, 0))
    night.usage(90, "2026-10-14T05:10:00+00:00")
    _wake(night, at=_at(23, 20))
    night.usage(3, "2026-10-14T09:40:00+00:00")

    _wake(night, at=_at(23, 40))

    assert len(night.sent()) == 2
    assert "starts: held" in night.wake_log.read_text()
    assert "starts: allowed" in night.wake_log.read_text().splitlines()[-2]


def test_the_tick_gives_a_check_in_at_night_and_still_starts_a_worker(night: Workflow) -> None:
    night.orchestrator("idle")
    night.add_pull_request(15)

    done = night.run_script("tick", env=_at(23, 0))

    assert done.returncode == 0, done.stderr
    assert len(night.sent()) == 1
    assert [call for call in night.calls("tmux ticket-15 new-session")] != []


def test_a_tick_whose_check_in_failed_fails_and_still_starts_a_worker(night: Workflow) -> None:
    night.orchestrator("idle")
    night.add_pull_request(15)
    (night.state / "send-fails").touch()

    done = night.run_script("tick", env=_at(23, 0))

    assert done.returncode == 1
    assert [call for call in night.calls("tmux ticket-15 new-session")] != []


def test_checks_that_turn_green_are_told(night: Workflow) -> None:
    """A ready pull request whose checks pass can be merged: that is news."""
    night.orchestrator("idle")
    (night.state / "checks").mkdir()
    (night.state / "checks" / "9").write_text("PENDING\n")
    _wake(night, at=_at(23, 0))
    (night.state / "checks" / "9").write_text("SUCCESS\n")

    _wake(night, at=_at(23, 20))

    assert "ready medium 9 (checks pending)" in night.sent()[0]
    assert "ready medium 9 (checks passing)" in night.sent()[1]


def test_a_failing_check_is_named(night: Workflow) -> None:
    night.orchestrator("idle")
    (night.state / "checks").mkdir()
    (night.state / "checks" / "9").write_text("FAILURE\n")

    _wake(night)

    assert "ready medium 9 (checks failing)" in night.sent()[0]


def test_a_reset_check_in_that_failed_is_tried_again_by_day(night: Workflow) -> None:
    """The tick passes the names once; the wake keeps them until the orchestrator is told."""
    night.orchestrator("idle")
    (night.state / "send-fails").touch()
    failed = _wake(night, "ticket-7", at=_at(10, 0))
    (night.state / "send-fails").unlink()

    soon = _wake(night, at=_at(10, 3))
    sent_soon = len(night.sent())
    again = _wake(night, at=_at(10, 6))
    after = _wake(night, at=_at(10, 12))

    assert failed.returncode == 1
    assert soon.returncode == 0
    assert sent_soon == 1
    assert again.returncode == 0, again.stderr
    assert len(night.sent()) == 2
    assert "Sent their instruction again: ticket-7." in night.sent()[1]
    assert after.returncode == 0
    assert len(night.sent()) == 2


def test_a_reset_check_in_waits_for_an_orchestrator_to_open(night: Workflow) -> None:
    _wake(night, "ticket-7", at=_at(10, 0))
    night.orchestrator("busy")

    _wake(night, "audit-9", at=_at(10, 3))

    [prompt] = night.sent()
    assert "Sent their instruction again: ticket-7 audit-9." in prompt


@pytest.mark.parametrize("name", ["Check-in", "Evening check-in"])
def test_only_the_registered_session_gets_the_check_in(night: Workflow, name: str) -> None:
    night.orchestrator("idle", name=name)
    agents = night.state / "agents.json"
    listed = json.loads(agents.read_text())
    listed.insert(
        0,
        {
            "name": "App build and testing",
            "kind": "interactive",
            "status": "idle",
            "cwd": str(night.repo),
        },
    )
    agents.write_text(json.dumps(listed))

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    [prompt] = night.sent()
    assert f"Recipient: {name}\n" in prompt
    assert "Recipient: App build and testing" not in prompt


@pytest.mark.parametrize("registration", [None, "", "Closed session\n"])
def test_without_a_registered_open_session_pending_messages_are_kept(
    night: Workflow, registration: str | None
) -> None:
    night.orchestrator("idle")
    path = night.repo / ".local" / "workflow" / "orchestrator"
    if registration is None:
        path.unlink()
    else:
        path.write_text(registration)

    done = _wake(night, "ticket-7", at=_at(10, 0))

    assert done.returncode == 0, done.stderr
    assert night.sent() == []
    assert night.wake_log.with_name("wake.pending").read_text() == "ticket-7\n"
    assert not night.wake_log.with_name("wake.told").exists()
    assert "nobody was told" in night.wake_log.read_text()
    assert "orchestrator" in night.wake_log.read_text().splitlines()[-1]
    path.write_text("Check-in\n")

    again = _wake(night, at=_at(10, 6))

    assert again.returncode == 0, again.stderr
    [prompt] = night.sent()
    assert "Recipient: Check-in\n" in prompt
    assert "Sent their instruction again: ticket-7." in prompt
    assert not night.wake_log.with_name("wake.pending").exists()


@pytest.mark.parametrize("kind,cwd", [("background", "main"), ("interactive", "worktree")])
def test_the_registered_name_must_be_interactive_in_the_main_checkout(
    night: Workflow, kind: str, cwd: str
) -> None:
    night.orchestrator("idle")
    agents = night.state / "agents.json"
    listed = json.loads(agents.read_text())
    listed[-1]["kind"] = kind
    listed[-1]["cwd"] = str(night.repo if cwd == "main" else night.worktree(7))
    agents.write_text(json.dumps(listed))

    done = _wake(night, "ticket-7")

    assert done.returncode == 0, done.stderr
    assert night.sent() == []
    assert night.wake_log.with_name("wake.pending").read_text() == "ticket-7\n"
    assert "named orchestrator (Check-in) is not open" in night.wake_log.read_text()


def _stop(workflow: Workflow, name: str, message: str, at: dict[str, str]) -> None:
    """The stop hook of a running session, as `notify` gets it."""
    done = workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": message}, session=name, env=at
    )
    assert done.returncode == 0, done.stderr


@pytest.fixture
def day(night: Workflow) -> Workflow:
    """A reviewer runs beside the worker, and the orchestrator is busy at its own work."""
    night.running("review-7")
    night.orchestrator("busy")
    return night


def test_a_question_to_the_owner_is_named_once_at_any_hour(day: Workflow) -> None:
    _stop(day, "review-7", "Merge it or send it back?\n\nReply 1 or 2", _at(10, 0))

    told = _wake(day, at=_at(10, 8))
    again = _wake(day, at=_at(10, 30))

    assert told.returncode == 0, told.stderr
    assert again.returncode == 0, again.stderr
    [prompt] = day.sent()
    assert "(checks passing).\nasking the owner: review-7 (8 min)\nLog: ~/" in prompt
    assert "  asking the owner: review-7 (8 min)" in day.wake_log.read_text()


def test_a_session_waiting_for_its_own_job_is_not_named(day: Workflow) -> None:
    day.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})
    _stop(day, "review-7", "Checks are running; I continue when they finish.", _at(10, 0))

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_finished_session_is_not_named(day: Workflow) -> None:
    _stop(day, "review-7", "Merged as one commit.\n\nreview finished", _at(10, 0))

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_question_that_was_answered_is_not_named(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    day.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"}, session="review-7")

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_new_question_of_the_same_session_is_named_again(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    _wake(day, at=_at(10, 3))
    day.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"}, session="review-7")
    _stop(day, "review-7", "The checks failed. Reply 3 or 4", _at(10, 20))

    _wake(day, at=_at(10, 26))

    assert len(day.sent()) == 2
    assert "\nasking the owner: review-7 (6 min)\n" in day.sent()[1]


def test_several_sessions_asking_are_named_on_one_line(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    _stop(day, "ticket-7", "Which limit do you want: 500 or 1,000?", _at(10, 7))

    _wake(day, at=_at(10, 15))

    [prompt] = day.sent()
    assert "\nasking the owner: review-7 (15 min), ticket-7 (8 min)\n" in prompt


def test_a_session_that_no_longer_runs_is_not_named(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    (day.state / "tmux" / "review-7").unlink()

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_question_whose_check_in_failed_is_tried_again(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    (day.state / "send-fails").touch()
    failed = _wake(day, at=_at(10, 3))
    (day.state / "send-fails").unlink()

    soon = _wake(day, at=_at(10, 6))
    sent_soon = len(day.sent())
    _wake(day, at=_at(10, 9))

    assert failed.returncode == 1
    assert soon.returncode == 0
    assert sent_soon == 1
    assert len(day.sent()) == 2
    assert "\nasking the owner: review-7 (9 min)\n" in day.sent()[1]


def test_a_question_already_told_is_still_named_in_a_later_check_in(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(22, 0))
    day.orchestrator("idle")
    _wake(day, at=_at(22, 1))
    day.list_pull_request(7, draft=False)

    _wake(day, at=_at(22, 21))

    assert len(day.sent()) == 2
    assert "\nasking the owner: review-7 (21 min)\n" in day.sent()[1]


def test_a_session_working_again_on_its_own_job_is_not_named(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    day.notify(
        {"hook_event_name": "UserPromptSubmit", "source": "background_task_output"},
        session="review-7",
    )

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_codex_bridge_whose_codex_asks_is_named(day: Workflow) -> None:
    (day.notes(7) / "ticket-7.codex").parent.mkdir(parents=True, exist_ok=True)
    (day.notes(7) / "ticket-7.codex").touch()
    _stop(day, "ticket-7", "Codex asks: which limit, 500 or 1,000?", _at(10, 0))

    _wake(day, at=_at(10, 4))

    [prompt] = day.sent()
    assert "\nasking the owner: ticket-7 (4 min)\n" in prompt


def test_a_question_of_a_session_that_ended_brings_no_check_in_by_day(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    (day.state / "tmux" / "review-7").unlink()
    day.orchestrator("idle")

    _wake(day, at=_at(10, 8))

    assert day.sent() == []


def test_a_question_told_before_its_own_job_ran_is_not_told_again(day: Workflow) -> None:
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 0))
    _wake(day, at=_at(10, 3))
    day.notify(
        {"hook_event_name": "UserPromptSubmit", "source": "background_task_output"},
        session="review-7",
    )
    _wake(day, "ticket-7", at=_at(10, 9))
    _stop(day, "review-7", "Reply 1 or 2", _at(10, 12))

    _wake(day, at=_at(10, 18))

    assert len(day.sent()) == 2
    assert "asking the owner" not in day.sent()[1]


def test_pull_request_read_retries_server_errors_before_check_in(night: Workflow) -> None:
    night.orchestrator("idle")
    night.fake("gh-real", (night.bin / "gh").read_text())
    counter = night.state / "list-attempts"
    night.fake(
        "gh",
        f'''#!/bin/bash
if [[ "$1 $2" == "pr list" ]]; then
  count=0
  [[ ! -f "{counter}" ]] || read -r count < "{counter}"
  count=$((count + 1))
  echo "$count" > "{counter}"
  if ((count <= 2)); then echo 'gh: Server Error (HTTP 502)' >&2; exit 1; fi
fi
exec "{night.bin / "gh-real"}" "$@"
''',
    )

    done = _wake(night)

    assert done.returncode == 0, done.stderr
    assert counter.read_text().strip() == "3"
    [prompt] = night.sent()
    assert "drafts 7; waiting tickets 12; ready medium 9 (checks passing)" in prompt


def _analysis(workflow: Workflow, url: str, proposals: list[str]) -> Path:
    pending = workflow.repo / ".local/workflow/analyses.pending"
    pending.mkdir(parents=True, exist_ok=True)
    event = pending / "day-2026-10-13.json"
    event.write_text(
        json.dumps(
            {
                "name": "day-2026-10-13",
                "report": ".local/analyses/day-2026-10-13/report.md",
                "url": url,
                "proposals": proposals,
            }
        )
    )
    # A note the tick is still writing is not read.
    (pending / ".day-2026-10-14.json").write_text("{")
    return event


def test_a_finished_analysis_is_told_once_by_day_with_its_link_and_proposals(
    night: Workflow,
) -> None:
    night.orchestrator("busy")
    event = _analysis(
        night,
        "https://docs.example.com/analysis-1",
        ["1. The tick starts tickets by itself", "2. A lost check is re-run by the gate"],
    )
    (night.state / "send-fails").touch()

    failed = _wake(night, at=_at(9, 0))
    assert failed.returncode == 1
    assert event.exists()
    (night.state / "send-fails").unlink()
    first = _wake(night, at=_at(9, 5))
    second = _wake(night, at=_at(9, 10))

    assert first.returncode == second.returncode == 0, first.stderr
    [_, prompt] = night.sent()  # one failed attempt, then one delivered message
    assert (
        " Workflow analysis day-2026-10-13 (report https://docs.example.com/analysis-1): "
        "1. The tick starts tickets by itself; 2. A lost check is re-run by the gate." in prompt
    )
    assert not event.exists()


def test_an_analysis_without_proposals_says_so(night: Workflow) -> None:
    night.orchestrator("idle")
    _analysis(night, "https://docs.example.com/analysis-2", [])

    done = _wake(night, at=_at(9, 0))

    assert done.returncode == 0, done.stderr
    assert (
        " Workflow analysis day-2026-10-13 (report https://docs.example.com/analysis-2): "
        "no proposals." in night.sent()[-1]
    )


def test_an_analysis_that_ended_without_its_report_is_told(night: Workflow) -> None:
    night.orchestrator("idle")
    _analysis(night, "", [])

    done = _wake(night, at=_at(9, 0))

    assert done.returncode == 0, done.stderr
    assert (
        " Workflow analysis day-2026-10-13 ended without publishing its report "
        "(.local/analyses/day-2026-10-13/report.md)." in night.sent()[-1]
    )
