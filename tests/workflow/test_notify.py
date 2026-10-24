"""`notify` rings the owner's phone when a workflow session is blocked on him, and only then."""

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest
from workflow_harness import HANGING_CALL, SESSION_LINK, Workflow

_TOKEN = "tokenOfTheApplication"
_USER = "keyOfTheOwner"


def _stop(message: str = "Which limit do you want: 500 or 1,000?") -> dict[str, object]:
    return {"hook_event_name": "Stop", "last_assistant_message": message}


_PROMPT: dict[str, object] = {"hook_event_name": "UserPromptSubmit", "user_message": "1,000"}
_PERMISSION: dict[str, object] = {
    "hook_event_name": "Notification",
    "notification_type": "permission_prompt",
    "message": "Claude needs your permission to use Bash",
}


@pytest.fixture
def session(workflow: Workflow) -> Workflow:
    """Pull request 7 is a draft, its worktree exists, its link is saved, the keys are there."""
    workflow.add_worktree(7)
    workflow.set_draft(7, draft=True)
    workflow.notes(7).mkdir(parents=True)
    for name in ("ticket-7", "audit-7"):
        (workflow.notes(7) / f"{name}.url").write_text(SESSION_LINK + "\n")
    keys = workflow.root / "home" / "agents" / ".env"
    keys.parent.mkdir()
    keys.write_text(f"OTHER_KEY=unrelated\nPUSHOVER_USER={_USER}\nPUSHOVER_TOKEN={_TOKEN}\n")
    return workflow


def _idle(workflow: Workflow, name: str = "ticket-7") -> Path:
    return workflow.notes(7) / f"{name}.idle"


def test_a_worker_that_stops_on_a_draft_rings_the_phone(session: Workflow) -> None:
    done = session.notify(_stop("Which limit do you want: 500 or 1,000?"))

    assert done.returncode == 0
    [push] = session.pushes()
    assert push.title == "ticket-7 needs you"
    assert push.message == "Which limit do you want: 500 or 1,000?"
    assert push.url == SESSION_LINK
    assert push.arguments[-1] == "https://api.pushover.net/1/messages.json"


def test_the_push_carries_the_keys_from_the_owners_file(session: Workflow) -> None:
    session.notify(_stop())

    [push] = session.pushes()
    assert f'"token={_TOKEN}"' in push.input
    assert f'"user={_USER}"' in push.input


def test_an_answer_that_is_an_error_counts_as_a_failed_push(session: Workflow) -> None:
    session.notify(_stop())

    [push] = session.pushes()
    assert "--fail" in push.arguments


def test_nothing_is_left_in_the_temporary_folder(session: Workflow) -> None:
    session.notify(_stop())

    assert len(session.pushes()) == 1
    assert list((session.root / "scratch").iterdir()) == []


def test_the_keys_are_not_shown_to_other_programs_or_printed(session: Workflow) -> None:
    done = session.notify(_stop())

    [push] = session.pushes()
    shown = "\n".join([*push.arguments, done.stdout, done.stderr])
    assert _TOKEN not in shown
    assert _USER not in shown


def test_the_body_is_the_first_300_characters_of_the_last_message(session: Workflow) -> None:
    session.notify(_stop("é" * 299 + "xyz?"))

    [push] = session.pushes()
    assert push.message == "é" * 299 + "x"


def test_a_message_of_several_lines_is_sent_whole(session: Workflow) -> None:
    message = 'Two options:\n\n1. "keep" the limit\n2. drop it & move on\\\nReply 1 or 2'

    session.notify(_stop(message))

    [push] = session.pushes()
    assert push.message == message


def test_a_worker_stop_without_a_message_is_idle_and_silent(session: Workflow) -> None:
    session.notify({"hook_event_name": "Stop"})

    assert session.pushes() == []
    assert _idle(session).exists()
    assert not _asking(session).exists()


@pytest.mark.parametrize(
    "message",
    ["worker finished", "Merged as one commit.\n\n**Worker finished.**\n"],
)
def test_a_worker_that_says_it_finished_does_not_ring(session: Workflow, message: str) -> None:
    session.set_draft(7, draft=False)

    done = session.notify(_stop(message))

    assert done.returncode == 0
    assert session.pushes() == []
    assert session.calls("gh") == []


@pytest.mark.parametrize(
    "message",
    [
        "Marked ready. The merge was refused: may I merge?",
        "9 conflicts with main. Which side do you keep?",
        'I will say "worker finished" once the checks pass.',
        "I cannot merge. The rule asks for:\n\n> Worker finished\n",
        "Merged.\n\nWorker finished.\n\nBut 9 conflicts with main. Which side do you keep?",
        "Audit finished.",
    ],
)
def test_a_worker_that_stops_after_marking_ready_without_saying_it_finished_rings(
    session: Workflow, message: str
) -> None:
    """Ready is not the end: the worker still merges, and can be blocked on the owner."""
    session.set_draft(7, draft=False)

    session.notify(_stop(message))

    [push] = session.pushes()
    assert push.title == "ticket-7 needs you"


def test_a_worker_on_a_draft_that_says_it_finished_is_idle_and_silent(session: Workflow) -> None:
    session.notify(_stop("Worker finished."))

    assert session.pushes() == []
    assert _idle(session).exists()
    assert not _asking(session).exists()


def test_an_audit_that_says_worker_finished_rings(session: Workflow) -> None:
    session.notify(_stop("Worker finished."), session="audit-7")

    assert len(session.pushes()) == 1


@pytest.mark.parametrize(
    "message",
    [
        "audit finished",
        "Audit finished.",
        "Merged as one commit and moved pull request 9 onto main.\n\n**Audit finished**\n",
    ],
)
def test_an_audit_that_says_it_finished_does_not_ring(session: Workflow, message: str) -> None:
    done = session.notify(_stop(message), session="audit-7")

    assert done.returncode == 0
    assert session.pushes() == []


@pytest.mark.parametrize(
    "message",
    [
        "Should the limit be 500 or 1,000? I recommend 1,000.",
        "The audit finished nothing yet: one question is open.",
        "audit not finished",
        'The rule is to end with this line:\n\n"audit finished"',
        "I have not said it yet:\n\n`audit finished`",
    ],
)
def test_an_audit_that_stops_on_anything_else_rings(session: Workflow, message: str) -> None:
    session.notify(_stop(message), session="audit-7")

    [push] = session.pushes()
    assert push.title == "audit-7 needs you"
    assert push.message == message
    assert push.url == SESSION_LINK


def test_an_audit_is_not_judged_by_its_pull_request(session: Workflow) -> None:
    session.set_draft(7, draft=False)

    session.notify(_stop("One question is open."), session="audit-7")

    assert len(session.pushes()) == 1
    assert session.calls("gh") == []


def test_a_permission_prompt_always_rings(session: Workflow) -> None:
    session.set_draft(7, draft=False)

    done = session.notify(_PERMISSION)

    assert done.returncode == 0
    [push] = session.pushes()
    assert push.title == "ticket-7 needs you"
    assert push.message == "Claude needs your permission to use Bash"
    assert push.url == SESSION_LINK


def test_a_notification_that_does_not_name_its_kind_rings(session: Workflow) -> None:
    session.notify({"hook_event_name": "Notification", "message": "Needs your attention"})

    [push] = session.pushes()
    assert push.message == "Needs your attention"


def test_a_permission_prompt_without_words_still_has_a_body(session: Workflow) -> None:
    session.notify({"hook_event_name": "Notification", "notification_type": "permission_prompt"})

    [push] = session.pushes()
    assert push.message == "The session stopped without a message."


def test_every_permission_prompt_rings_even_with_the_same_words(session: Workflow) -> None:
    session.notify(_PERMISSION)
    session.notify(_PERMISSION)

    assert len(session.pushes()) == 2


def test_a_session_waiting_for_a_permission_is_not_marked_idle(session: Workflow) -> None:
    session.notify(_PERMISSION)

    assert not _idle(session).exists()


def test_another_kind_of_notification_does_not_ring(session: Workflow) -> None:
    session.notify({**_PERMISSION, "notification_type": "idle_prompt"})

    assert session.pushes() == []


def test_the_same_last_message_twice_rings_once(session: Workflow) -> None:
    session.notify(_stop("Which limit?"))
    session.notify(_stop("Which limit?"))

    assert len(session.pushes()) == 1


def test_a_new_last_message_rings_again(session: Workflow) -> None:
    session.notify(_stop("Which limit?"))
    session.notify(_stop("And which wording?"))

    assert [push.message for push in session.pushes()] == ["Which limit?", "And which wording?"]


def test_the_same_question_after_the_owner_answered_rings_again(session: Workflow) -> None:
    session.notify(_stop("Which limit?"))
    session.notify(_PROMPT)
    session.notify(_stop("Which limit?"))

    assert len(session.pushes()) == 2


def test_a_turn_the_session_started_itself_does_not_make_it_ring_twice(session: Workflow) -> None:
    session.notify(_stop("Which limit?"))
    session.notify({**_PROMPT, "user_initiated": False, "source": "background_task_output"})
    session.notify(_stop("Which limit?"))

    assert len(session.pushes()) == 1


@pytest.mark.parametrize("name", [None, "", "orchestrator", "ticket-", "ticket-7; rm -rf x"])
def test_a_session_outside_the_workflow_never_rings(session: Workflow, name: str | None) -> None:
    for event in (_stop(), _PERMISSION, _PROMPT):
        done = session.notify(event, session=name)
        assert done.returncode == 0

    assert session.pushes() == []
    assert session.calls("gh") == []
    assert list(session.notes(7).glob("*.idle")) == []


def test_it_rings_when_github_cannot_be_reached(session: Workflow) -> None:
    (session.state / "draft" / "7").unlink()

    done = session.notify(_stop())

    assert done.returncode == 0
    assert len(session.pushes()) == 1


def test_it_rings_when_github_says_something_unexpected(session: Workflow) -> None:
    (session.state / "draft" / "7").write_text("")

    session.notify(_stop())

    assert len(session.pushes()) == 1


def test_it_rings_when_github_does_not_answer_in_time(session: Workflow) -> None:
    session.trace_timeouts()
    session.fake("gh", HANGING_CALL)

    done = session.notify(_stop(), env={"WORKFLOW_NOTIFY_SECONDS": "1"})

    assert 0 < session.timeout_budget() <= 8
    assert done.returncode == 0
    assert session.calls("hangs-gh") == ["hangs-gh"]
    assert session.calls("interrupted-gh") == ["interrupted-gh"]
    assert len(session.pushes()) == 1


def test_with_nobody_answering_it_still_ends_within_the_ten_seconds_a_hook_has(
    session: Workflow,
) -> None:
    session.trace_timeouts()
    session.fake("gh", HANGING_CALL)
    session.fake("curl", HANGING_CALL)

    done = session.notify(_stop())

    assert 0 < session.timeout_budget() <= 8
    assert done.returncode == 0
    assert session.calls("hangs-gh") == ["hangs-gh"]
    assert session.calls("interrupted-gh") == ["interrupted-gh"]
    assert session.calls("interrupted-curl") == ["interrupted-curl"]
    [line] = session.missed_pushes.read_text().splitlines()
    assert "ticket-7 needs you" in line


def _soon(happened: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 30
    while not happened() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert happened()


def test_a_hanging_call_cut_off_twice_is_written_down_once(session: Workflow) -> None:
    # GNU timeout sends TERM to the call, then to its whole process group. On a loaded runner
    # the second one can land while bash 5.2 still runs the call's trap, after its line and
    # before its exit, and the trap runs again: one call written down as two.
    # Here the trap waits after its line until it is released, so the second TERM lands there.
    held = HANGING_CALL.replace(
        "exit 124", "until [[ -e @STATE@/release ]]; do /bin/sleep 0.01; done; exit 124"
    )
    assert held != HANGING_CALL
    session.fake("gh", held)
    call = subprocess.Popen([session.bin / "gh"], env=session.env, start_new_session=True)
    try:
        # The trap is set before the call starts its sleep.
        _soon(lambda: bool(Path(f"/proc/{call.pid}/task/{call.pid}/children").read_text()))
        call.terminate()
        _soon(lambda: session.calls("interrupted-gh") != [])
        # The signal is pending once killpg returns, so bash takes it before it sees the release.
        os.killpg(call.pid, signal.SIGTERM)
        (session.state / "release").touch()

        assert call.wait(timeout=30) == 124
    finally:
        with suppress(ProcessLookupError):
            os.killpg(call.pid, signal.SIGKILL)
        call.wait(timeout=30)
    assert session.calls("interrupted-gh") == ["interrupted-gh"]


def test_a_failed_push_is_written_down_and_does_not_fail_the_session(session: Workflow) -> None:
    (session.state / "pushover-fails").touch()

    done = session.notify(_stop("Which limit?\nI recommend 1,000. Reply 1."))

    assert done.returncode == 0
    [line] = session.missed_pushes.read_text().splitlines()
    assert "ticket-7 needs you" in line
    assert "Which limit? I recommend 1,000." in line
    assert SESSION_LINK in line
    assert _TOKEN not in line
    assert _USER not in line


def test_a_push_that_failed_is_tried_again_at_the_next_stop(session: Workflow) -> None:
    (session.state / "pushover-fails").touch()
    session.notify(_stop("Which limit?"))
    (session.state / "pushover-fails").unlink()

    session.notify(_stop("Which limit?"))

    assert len(session.pushes()) == 2


def test_pushover_not_answering_in_time_is_a_failed_push(session: Workflow) -> None:
    session.trace_timeouts()
    session.fake("curl", HANGING_CALL)

    done = session.notify(_stop(), env={"WORKFLOW_NOTIFY_SECONDS": "1"})

    assert 0 < session.timeout_budget() <= 8
    assert done.returncode == 0
    assert session.calls("interrupted-curl") == ["interrupted-curl"]
    assert "ticket-7 needs you" in session.missed_pushes.read_text()


@pytest.mark.parametrize("keys", [None, "PUSHOVER_USER=onlyTheUser\n", "PUSHOVER_TOKEN=\n"])
def test_missing_keys_are_a_failed_push(session: Workflow, keys: str | None) -> None:
    file = session.root / "home" / "agents" / ".env"
    file.unlink()
    if keys is not None:
        file.write_text(keys)

    done = session.notify(_stop())

    assert done.returncode == 0
    assert session.pushes() == []
    assert "ticket-7 needs you" in session.missed_pushes.read_text()


def test_keys_written_in_quotes_are_read(session: Workflow) -> None:
    keys = session.root / "home" / "agents" / ".env"
    keys.write_text(f"PUSHOVER_USER=\"{_USER}\"\nexport PUSHOVER_TOKEN='{_TOKEN}'\n")

    session.notify(_stop())

    [push] = session.pushes()
    assert f'"token={_TOKEN}"' in push.input
    assert f'"user={_USER}"' in push.input


def test_the_keys_file_is_read_and_never_run(session: Workflow) -> None:
    keys = session.root / "home" / "agents" / ".env"
    keys.write_text(f"PUSHOVER_USER={_USER}\nPUSHOVER_TOKEN=$(touch {session.root}/ran)\n")

    session.notify(_stop())

    assert not (session.root / "ran").exists()
    assert session.pushes() == []


def test_a_push_without_a_saved_link_is_sent_without_one(session: Workflow) -> None:
    (session.notes(7) / "ticket-7.url").unlink()

    session.notify(_stop())

    [push] = session.pushes()
    assert push.url is None


def test_a_stop_marks_the_session_idle(session: Workflow) -> None:
    session.set_draft(7, draft=False)

    session.notify(_stop())

    assert _idle(session).exists()


def test_a_blocked_session_is_idle_too(session: Workflow) -> None:
    session.notify(_stop())

    assert _idle(session).exists()


def test_an_audit_has_an_idle_mark_of_its_own_in_the_same_worktree(session: Workflow) -> None:
    session.notify(_stop("audit finished"), session="audit-7")

    assert _idle(session, "audit-7").exists()
    assert not _idle(session).exists()


def test_a_prompt_ends_the_idle_mark_and_sends_nothing(session: Workflow) -> None:
    session.notify(_stop())
    pushes = len(session.pushes())
    asked = len(session.calls("gh"))

    done = session.notify(_PROMPT)

    assert done.returncode == 0
    assert not _idle(session).exists()
    assert len(session.pushes()) == pushes
    assert len(session.calls("gh")) == asked


def test_a_session_waiting_for_its_own_job_is_neither_idle_nor_blocked(session: Workflow) -> None:
    waiting = session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "ticket-7"})

    done = session.notify(_stop("Codex is reviewing; I continue when it reports."))

    assert waiting.returncode == 0
    assert done.returncode == 0
    assert session.pushes() == []
    assert not _idle(session).exists()
    assert session.calls("gh") == []


def test_waiting_covers_one_stop_only(session: Workflow) -> None:
    session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "ticket-7"})
    session.notify(_stop("Codex is reviewing; I continue when it reports."))

    session.notify(_stop("Codex found nothing. Which limit do you want?"))

    assert len(session.pushes()) == 1
    assert _idle(session).exists()


@pytest.mark.parametrize(
    ("name", "finished"),
    [
        ("review-7", "review finished"),
        ("ticket-7", "worker finished"),
        ("audit-7", "audit finished"),
        ("hunt-example", "hunt finished"),
        ("review-7", "review sent back"),
    ],
)
def test_finished_stop_overrides_an_earlier_wait(
    session: Workflow, name: str, finished: str
) -> None:
    if name.startswith("hunt-"):
        notes = session.add_hunt("example") / ".local" / "workflow"
    else:
        notes = session.notes(7)
    session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": name})

    done = session.notify(_stop(f"Work completed.\n\n**{finished}.**\n"), session=name)

    assert done.returncode == 0
    assert (notes / f"{name}.idle").exists()
    assert not (notes / f"{name}.waiting").exists()
    assert not (notes / f"{name}.checks-wait").exists()
    assert not (notes / f"{name}.job-wait").exists()
    assert not (notes / f"{name}.asking").exists()
    assert session.pushes() == []


@pytest.mark.parametrize(
    "message",
    [
        "Checks are still running.",
        "review sent back",
        "> review finished",
        "review finished\n\nWhich option do you want?",
        "worker finished",
    ],
)
def test_unfinished_reviewer_keeps_waiting(session: Workflow, message: str) -> None:
    session.set_draft(7, draft=False)
    session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})

    session.notify(_stop(message), session="review-7")

    assert not _idle(session, "review-7").exists()
    assert (session.notes(7) / "review-7.checks-wait").exists()
    assert not (session.notes(7) / "review-7.waiting").exists()
    assert session.pushes() == []


@pytest.mark.parametrize("name", ["ticket-7", "review-7"])
@pytest.mark.parametrize("then", ["prompt", "stop"])
def test_a_wait_notes_when_it_began_until_the_session_takes_another_turn(
    session: Workflow, name: str, then: str
) -> None:
    # The tick reads from it how long the session has waited for its own job.
    job_wait = session.notes(7) / f"{name}.job-wait"
    session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": name})
    session.notify(_stop("The full Python run is in the background."), session=name)
    assert job_wait.exists()
    assert not _idle(session, name).exists()

    if then == "prompt":
        session.notify(_PROMPT, session=name)
    else:
        session.notify(_stop("It finished. Which limit do you want?"), session=name)

    assert not job_wait.exists()


@pytest.mark.parametrize("name", ["ticket-7", "review-7"])
def test_a_usage_limit_stop_after_a_wait_is_the_limit_not_a_wait(
    session: Workflow, name: str
) -> None:
    # The tick sends a waiting session a status check, never one stopped on the limit.
    session.usage(100, "2026-10-14T05:10:00+00:00")
    session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": name})

    session.notify(_stop("Your usage limit was reached, so I am stopping here."), session=name)

    notes = session.notes(7)
    assert (notes / f"{name}.limit").exists()
    assert not (notes / f"{name}.job-wait").exists()
    assert not (notes / f"{name}.checks-wait").exists()
    assert session.pushes() == []


def test_waiting_does_not_outlast_the_next_prompt(session: Workflow) -> None:
    session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "ticket-7"})
    session.notify(_PROMPT)

    session.notify(_stop("Which limit do you want?"))

    assert len(session.pushes()) == 1


def test_waiting_outside_the_workflow_writes_nothing(session: Workflow) -> None:
    done = session.run_script("notify", "waiting")

    assert done.returncode == 0
    assert sorted(file.name for file in session.notes(7).iterdir()) == [
        "audit-7.url",
        "ticket-7.url",
    ]


def test_it_rings_for_a_session_whose_worktree_is_gone(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.set_draft(7, draft=True)
    keys = workflow.root / "home" / "agents" / ".env"
    keys.parent.mkdir()
    keys.write_text(f"PUSHOVER_USER={_USER}\nPUSHOVER_TOKEN={_TOKEN}\n")

    done = workflow.notify(_stop())

    assert done.returncode == 0
    [push] = workflow.pushes()
    assert push.url is None
    assert not workflow.worktree(7).exists()


@pytest.mark.parametrize("event", ["", "not json", "[]", '{"hook_event_name": "SessionEnd"}'])
def test_an_event_it_does_not_know_is_ignored_without_failing(
    session: Workflow, event: str
) -> None:
    done = session.notify(event)

    assert done.returncode == 0
    assert session.pushes() == []


def test_it_prints_nothing_because_a_hook_s_output_reaches_the_session(session: Workflow) -> None:
    noisy = "#!/bin/bash\necho 'said aloud'\necho 'an error' >&2\nexit 1\n"
    session.fake("gh", noisy)
    session.fake("curl", noisy)

    for event in (_stop(), _PROMPT, _PERMISSION):
        done = session.notify(event)
        assert done.stdout == ""
        assert done.stderr == ""
    waiting = session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "ticket-7"})
    assert waiting.stdout == ""
    assert waiting.stderr == ""


def _exported_name(workflow: Workflow, name: str) -> str:
    lines = (workflow.state / "env" / name).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)["WORKFLOW_SESSION"]


def test_an_audit_started_by_the_session_script_rings_under_the_name_it_was_given(
    session: Workflow,
) -> None:
    started = session.run("start", "audit", "7")
    name = _exported_name(session, "audit-7")

    session.notify(_stop("One question is open."), session=name)

    assert started.returncode == 0, started.stderr
    [push] = session.pushes()
    assert push.title == "audit-7 needs you"
    assert push.url == SESSION_LINK


def test_a_worker_started_by_the_session_script_rings_under_the_name_it_was_given(
    session: Workflow,
) -> None:
    session.run("start", "worker", "7")
    name = _exported_name(session, "ticket-7")

    session.notify(_stop(), session=name)

    [push] = session.pushes()
    assert push.title == "ticket-7 needs you"


def test_the_words_the_rules_tell_a_worker_to_end_with_do_not_ring(session: Workflow) -> None:
    rules = (Path(__file__).resolve().parents[2] / "scripts/workflow/RULES.md").read_text()
    said = re.search(r'\bsay "(worker [^"]+)" on a line of its own', rules)
    assert said is not None
    session.set_draft(7, draft=False)

    session.notify(_stop(f"Everything is merged.\n\n{said.group(1)}"))

    assert session.pushes() == []


def test_the_words_the_rules_tell_an_audit_to_end_with_do_not_ring(session: Workflow) -> None:
    rules = (Path(__file__).resolve().parents[2] / "scripts/workflow/RULES.md").read_text()
    said = re.search(r'\bsay "(audit [^"]+)" on a line of its own', rules)
    assert said is not None

    session.notify(_stop(f"Everything is merged.\n\n{said.group(1)}"), session="audit-7")

    assert session.pushes() == []


def test_a_long_message_repeated_rings_once(session: Workflow) -> None:
    long = "Which limit do you want? " * 20

    session.notify(_stop(long))
    session.notify(_stop(long))

    assert len(session.pushes()) == 1


def test_long_messages_that_begin_alike_each_ring(session: Workflow) -> None:
    beginning = "x" * 300

    session.notify(_stop(beginning + " Which limit?"))
    session.notify(_stop(beginning + " And which wording?"))

    assert len(session.pushes()) == 2


def test_a_failed_push_leaves_nothing_in_the_temporary_folder(session: Workflow) -> None:
    (session.state / "pushover-fails").touch()

    session.notify(_stop())

    assert session.missed_pushes.is_file()
    assert list((session.root / "scratch").iterdir()) == []


def test_it_works_whichever_folder_the_hook_is_called_from(session: Workflow) -> None:
    session.set_draft(7, draft=False)
    elsewhere = session.root / "elsewhere"
    elsewhere.mkdir()

    session.notify(_stop("Worker finished."), cwd=elsewhere)
    session.notify(_PERMISSION, cwd=elsewhere)

    assert _idle(session).exists()
    [push] = session.pushes()
    assert push.message == "Claude needs your permission to use Bash"
    assert push.url == SESSION_LINK


_BOTH = pytest.mark.parametrize("name", ["ticket-7", "audit-7"])


@_BOTH
def test_waiting_covers_the_next_stop_of_a_worker_and_of_an_audit(
    session: Workflow, name: str
) -> None:
    session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": name})

    session.notify(_stop("Codex is reviewing; I continue when it reports."), session=name)

    assert session.pushes() == []
    assert not _idle(session, name).exists()


@_BOTH
def test_a_worker_and_an_audit_each_ring_once_for_the_same_words(
    session: Workflow, name: str
) -> None:
    session.notify(_stop("Which limit?"), session=name)
    session.notify(_stop("Which limit?"), session=name)

    assert [push.title for push in session.pushes()] == [f"{name} needs you"]


@_BOTH
def test_a_prompt_ends_the_idle_mark_of_a_worker_and_of_an_audit(
    session: Workflow, name: str
) -> None:
    session.notify(_stop(), session=name)

    session.notify(_PROMPT, session=name)

    assert not _idle(session, name).exists()


@_BOTH
def test_a_permission_prompt_rings_for_a_worker_and_for_an_audit(
    session: Workflow, name: str
) -> None:
    session.notify(_PERMISSION, session=name)

    assert [push.title for push in session.pushes()] == [f"{name} needs you"]


@pytest.mark.parametrize(
    "started_by",
    [
        {"source": "background_task_output"},
        {"source": "auto_continue"},
        {"source": "quota_auto_resume"},
        {"system": True},
        {"is_user_input": False},
        {"user_initiated": False},
        {"source": "permission_request"},
        {"source": "model_switch"},
    ],
)
def test_a_turn_that_names_its_own_cause_is_not_the_owners_answer(
    session: Workflow, started_by: dict[str, object]
) -> None:
    session.notify(_stop("Which limit?"))
    session.notify({**_PROMPT, **started_by})
    session.notify(_stop("Which limit?"))

    assert len(session.pushes()) == 1


def test_a_prompt_the_owner_typed_is_his_answer(session: Workflow) -> None:
    session.notify(_stop("Which limit?"))
    session.notify({**_PROMPT, "user_initiated": True, "source": "user_input", "system": False})
    session.notify(_stop("Which limit?"))

    assert len(session.pushes()) == 2


@pytest.mark.parametrize("name", ["ticket-7", "review-7", "audit-7"])
def test_task_notification_preserves_owner_question(session: Workflow, name: str) -> None:
    session.notify(_stop("Which limit?"), session=name)
    notes = session.notes(7)
    # The same prompt handler owns these notes for Claude and the Codex bridge.
    markers = ["asking", "pushed", "owner-blocked", "interaction-blocked"]
    for marker in markers:
        path = notes / f"{name}.{marker}"
        if not path.exists():
            path.write_text("waiting for owner\n")
    before = {marker: (notes / f"{name}.{marker}").read_text() for marker in markers}
    event = json.loads((session.script.parent / "tests/await-turn-completed.json").read_text())

    assert session.notify(event, session=name).returncode == 0

    assert {marker: (notes / f"{name}.{marker}").read_text() for marker in markers} == before
    session.notify(_stop("Which limit?"), session=name)
    assert len(session.pushes()) == 1
    session.notify({**_PROMPT, "prompt": "1"}, session=name)
    assert all(not (notes / f"{name}.{marker}").exists() for marker in markers)


_QUESTION: dict[str, object] = {
    "hook_event_name": "PreToolUse",
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [
            {"question": "Which limit do you want?", "header": "Limit", "options": []},
            {"question": "And which wording?", "header": "Wording", "options": []},
        ]
    },
}


@_BOTH
def test_a_question_asked_inside_the_turn_rings(session: Workflow, name: str) -> None:
    session.set_draft(7, draft=False)

    done = session.notify(_QUESTION, session=name)

    assert done.returncode == 0
    assert done.stdout == ""
    [push] = session.pushes()
    assert push.title == f"{name} needs you"
    assert push.message == "Which limit do you want?\nAnd which wording?"
    assert push.url == SESSION_LINK
    assert not _idle(session, name).exists()


def test_the_same_question_asked_again_inside_a_turn_rings_again(session: Workflow) -> None:
    session.notify(_QUESTION)
    session.notify(_QUESTION)

    assert len(session.pushes()) == 2


def test_a_question_without_words_still_has_a_body(session: Workflow) -> None:
    session.notify({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"})

    [push] = session.pushes()
    assert push.message == "The session stopped without a message."


def test_a_long_question_is_cut_to_300_characters(session: Workflow) -> None:
    asked = {"questions": [{"question": "q" * 400}]}

    session.notify({**_QUESTION, "tool_input": asked})

    [push] = session.pushes()
    assert push.message == "q" * 300


def test_another_tool_does_not_ring(session: Workflow) -> None:
    session.notify({**_QUESTION, "tool_name": "Bash", "tool_input": {"command": "ls"}})

    assert session.pushes() == []


def test_a_question_outside_the_workflow_does_not_ring(session: Workflow) -> None:
    session.notify(_QUESTION, session=None)

    assert session.pushes() == []


def test_notify_and_the_tick_read_the_finishing_line_alike(workflow: Workflow) -> None:
    line = re.compile(r"^FINISHED_LINE='([^']*WORDS[^']*)'$", re.MULTILINE)
    scripts = workflow.script.parent

    in_notify = line.search((scripts / "notify").read_text())
    in_tick = line.search((scripts / "tick").read_text())

    assert in_notify is not None
    assert in_tick is not None
    assert in_notify.group(1) == in_tick.group(1)


def test_the_same_stop_after_a_question_asked_inside_the_turn_rings_again(
    session: Workflow,
) -> None:
    """The owner answered inside the question tool, so no prompt followed his answer."""
    session.notify(_stop("Which limit?"))
    session.notify({**_PROMPT, "source": "background_task_output"})
    session.notify(_QUESTION)

    session.notify(_stop("Which limit?"))

    assert [push.message for push in session.pushes()] == [
        "Which limit?",
        "Which limit do you want?\nAnd which wording?",
        "Which limit?",
    ]


def test_an_audit_that_goes_on_after_the_words_rings(session: Workflow) -> None:
    session.notify(
        _stop("Merged.\n\nAudit finished.\n\nWhich limit do you want?"), session="audit-7"
    )

    assert len(session.pushes()) == 1


def _asking(workflow: Workflow, name: str = "ticket-7") -> Path:
    return workflow.notes(7) / f"{name}.asking"


def _asked_at(workflow: Workflow, name: str = "ticket-7") -> str:
    return _asking(workflow, name).read_text().split()[0]


_AT_NINE = {"WORKFLOW_NOW": "1792000000"}
_AT_TEN = {"WORKFLOW_NOW": "1792003600"}


def test_a_stop_that_rings_notes_when_the_owner_was_asked(session: Workflow) -> None:
    """The check-in of `wake` names the session and how long it has waited."""
    session.notify(_stop("Reply 1 or 2"), session="review-7", env=_AT_NINE)

    assert _asked_at(session, "review-7") == "1792000000"


def test_the_question_is_noted_even_when_the_push_fails(session: Workflow) -> None:
    (session.root / "home" / "agents" / ".env").unlink()

    session.notify(_stop("Reply 1 or 2"), env=_AT_NINE)

    assert session.pushes() == []
    assert _asked_at(session) == "1792000000"


def test_a_session_waiting_for_its_own_job_notes_no_question(session: Workflow) -> None:
    session.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})

    session.notify(_stop("Codex is reviewing; I continue when it reports."), session="review-7")

    assert not _asking(session, "review-7").exists()


def test_a_finished_session_notes_no_question_and_drops_an_earlier_one(session: Workflow) -> None:
    session.notify(_stop("Reply 1 or 2"), session="review-7")
    session.notify({**_PROMPT, "source": "background_task_output"}, session="review-7")

    session.notify(_stop("Merged.\n\nreview finished"), session="review-7")

    assert not _asking(session, "review-7").exists()


def test_the_owners_answer_drops_the_question(session: Workflow) -> None:
    session.notify(_stop("Reply 1 or 2"), session="review-7")

    session.notify(_PROMPT, session="review-7")

    assert not _asking(session, "review-7").exists()


def test_the_same_question_after_the_sessions_own_job_keeps_its_time(session: Workflow) -> None:
    session.notify(_stop("Reply 1 or 2"), env=_AT_NINE)
    session.notify({**_PROMPT, "source": "background_task_output"})

    session.notify(_stop("Reply 1 or 2"), env=_AT_TEN)

    assert _asked_at(session) == "1792000000"


def test_another_question_after_the_sessions_own_job_is_a_new_one(session: Workflow) -> None:
    session.notify(_stop("Reply 1 or 2"), env=_AT_NINE)
    session.notify({**_PROMPT, "source": "background_task_output"})

    session.notify(_stop("The checks failed. Reply 3 or 4"), env=_AT_TEN)

    assert _asked_at(session) == "1792003600"


def test_a_codex_bridge_notes_the_question_codex_asked(session: Workflow) -> None:
    (session.notes(7) / "ticket-7.codex").touch()

    session.notify(_stop("Codex asks: which limit, 500 or 1,000?"), env=_AT_NINE)

    assert _asked_at(session) == "1792000000"


def test_a_session_stopped_on_the_usage_limit_notes_no_question(session: Workflow) -> None:
    session.usage(100, "2026-10-14T05:10:00+00:00")

    session.notify(_stop("Your usage limit was reached, so I am stopping here."))

    assert (session.notes(7) / "ticket-7.limit").exists()
    assert not _asking(session).exists()


def test_the_bridges_own_relay_prompt_is_no_answer_and_keeps_the_question(
    session: Workflow,
) -> None:
    (session.notes(7) / "ticket-7.codex").touch()
    session.notify(_stop("Codex asks: which limit, 500 or 1,000?"), env=_AT_NINE)
    session.notify({**_PROMPT, "prompt": "__WORKFLOW_CODEX_TURN__"})

    session.notify(_stop("Codex asks: which limit, 500 or 1,000?"), env=_AT_TEN)

    assert _asked_at(session) == "1792000000"


@pytest.mark.parametrize(
    "question",
    [
        'Reply "1"',
        "Reply `1` or `2`.",
        "Choose 1 or 2.",
        "Reply 1 or 2",
        "Please reply with your choice.",
        'Type "1"',
        'write "merge it"',
        'Reply "go on" or "stop"',
        'The merge of pull request 7 was refused, write "merge it" and I run it.',
        'The description change was refused, write "change it" and I run it.',
        "Which option do you prefer?",
        "**Which option do you prefer?**",
        "Reply “1”",
        "reply\nwith your choice.",
        # As Codex writes them, and as its bridge relays them.
        "Reply **1** or **2**.",
        'Implementation is paused. Reply **"1"** to approve.',
        "**Reply 1 or 2.**",
        '"Which option do you prefer?"',
        '"Reply 1 or 2."',
        "> Reply 1 or 2.",
        "```\nWhich option do you prefer?\n```",
    ],
)
@pytest.mark.parametrize("bridge", [False, True])
def test_a_draft_workers_last_paragraph_asks_the_owner(
    session: Workflow, question: str, bridge: bool
) -> None:
    if bridge:
        (session.notes(7) / "ticket-7.codex").touch()
    message = f"Tests passed.\n\n{question}\n  \n\n"

    session.notify(_stop(message), env=_AT_NINE)

    [push] = session.pushes()
    assert push.message == message.rstrip("\n")
    assert _idle(session).exists()
    assert _asked_at(session) == _AT_NINE["WORKFLOW_NOW"]


@pytest.mark.parametrize(
    "report",
    [
        "Unit/workflow coverage: adequate.",
        "The full run has reached 99% with no failures.",
        "Tests passed.\n\nI am pushing the branch now.\n \n",
        'Reply "1"\n\nThe owner answered; tests passed.',
        "Which option?\n\nThe owner answered; tests passed.",
        "Which option?\nThe owner answered; tests passed.",
        "The reply was recorded. I will write the tests.",
        "Tests passed. I will write `test_notify.py` next.",
        'Tests passed. I will write "test_notify.py" next.',
        "I will reply with a progress report.",
        "Next step: write `test_notify.py` and push.",
        "Coverage: type 1 remaining.",
        "",
        " \n\n ",
    ],
)
@pytest.mark.parametrize("bridge", [False, True])
def test_a_draft_workers_progress_report_is_idle_without_an_owner_question(
    session: Workflow, report: str, bridge: bool
) -> None:
    if bridge:
        (session.notes(7) / "ticket-7.codex").touch()
    # Even an automatic continuation must drop a previous question when it now reports.
    session.notify(_stop('Reply "1"'))
    session.notify({**_PROMPT, "source": "background_task_output"})
    before = len(session.pushes())

    session.notify(_stop(report))

    assert len(session.pushes()) == before
    assert _idle(session).exists()
    assert not _asking(session).exists()


@pytest.mark.parametrize("introduction", ["", "Codex's message, word for word:\n\n"])
def test_a_bridge_relaying_a_live_permission_panel_rings(
    session: Workflow, introduction: str
) -> None:
    (session.notes(7) / "ticket-7.codex").touch()
    panel = (
        "Would you like to run the following command?\n\n$ git push\n\n"
        "1. Yes, proceed (y)\n2. No (esc)\n\nPress enter to confirm or esc to cancel"
    )
    (session.notes(7) / "ticket-7.codex-approval-delivered").write_text(panel)

    session.notify(_stop(introduction + panel))

    assert len(session.pushes()) == 1


def test_a_report_after_a_permission_panel_is_silent(session: Workflow) -> None:
    (session.notes(7) / "ticket-7.codex").touch()
    (session.notes(7) / "ticket-7.codex-approval-delivered").write_text(
        "Would you like to run the following command?\n\n$ git push"
    )

    session.notify(_stop("Pushed the branch; tests passed."))

    assert session.pushes() == []


def test_a_worker_github_does_not_answer_for_rings_without_a_question(
    session: Workflow,
) -> None:
    """It may be ready, and then a stop without "worker finished" is blocked."""
    session.fake("gh", "#!/bin/bash\nexit 1\n")

    session.notify(_stop("Marked ready. Waiting for CI."))

    assert len(session.pushes()) == 1
