"""Breaking tests for pull request 15: the tick answers a question marked `Owner only: no`.

Each test replays a day of a repository that uses the workflow, with neutral names: the owner
follows many sessions from a phone and is away for hours, the tick runs every 3 minutes, the
wake gives the orchestrator a check-in every 20 minutes at night and the owner reads in the
morning what was answered for him.
"""

import fcntl
import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workflow_harness import Workflow

_ZONE = ZoneInfo("America/New_York")
_BOX = "─" * 40 + "\n> \n" + "─" * 40 + "\n  ? for shortcuts\n"
_TICK = 180


def _at(hour: int, minute: int = 0, day: int = 13) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=_ZONE).timestamp())


def _env(at: int) -> dict[str, str]:
    return {"WORKFLOW_NOW": str(at), "TZ": "America/New_York"}


def _design_question(owner_only: str | None = "no", reply: str = "Reply 1 or 2.") -> str:
    line = "" if owner_only is None else f"Owner only: {owner_only}\n"
    return (
        "Should the example-app list page load 50 rows or all of them?\n\n"
        "Recommendation: 1, 50 rows with a next page, as the other lists do.\n"
        f"1. 50 rows\n2. All rows\n{line}{reply}"
    )


def _ask(workflow: Workflow, name: str, message: str, at: int) -> None:
    number = int(name.split("-")[1])
    if not workflow.worktree(number).exists():
        workflow.add_worktree(number)
        workflow.set_draft(number, draft=True)
    workflow.running(name)
    workflow.screen("● " + message.replace("\n", "\n  ") + "\n" + _BOX, name)
    done = workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": message},
        session=name,
        env={"WORKFLOW_NOW": str(at)},
    )
    assert done.returncode == 0, done.stderr


def _tick(workflow: Workflow, at: int) -> subprocess.CompletedProcess[str]:
    return workflow.run_script("tick", env=_env(at))


def _replies(workflow: Workflow, name: str) -> list[str]:
    return [
        call.split(" -l ", 1)[1] for call in workflow.calls(f"tmux {name} send-keys -t ={name}: -l ")
    ]


def _pending(workflow: Workflow) -> Path:
    return workflow.repo / ".local" / "workflow" / "answered-for-owner.pending"


# 1. The daily flow: a design question asked at night is answered after 15 minutes, once, and the
#    owner finds it in the morning check-in, not in the night ones.


@pytest.mark.timeout(900)
def test_a_night_question_is_answered_once_and_listed_only_in_the_morning(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(9, draft=False)
    workflow.usage(40, "2026-10-14T05:10:00+00:00", seven=20)
    workflow.orchestrator("idle")
    asked = _at(23, 0)
    _ask(workflow, "ticket-7", _design_question(), asked)

    # The tick every 3 minutes until 00:00.
    for at in range(asked + _TICK, asked + 3600, _TICK):
        done = _tick(workflow, at)
        assert done.returncode == 0, done.stderr
        if _replies(workflow, "ticket-7") and (workflow.notes(7) / "ticket-7.question").exists():
            # The typed reply reached the session as a prompt, as when the owner types.
            workflow.notify(
                {"hook_event_name": "UserPromptSubmit", "prompt": "1"}, session="ticket-7"
            )

    assert _replies(workflow, "ticket-7") == ["1"]
    first = asked + 5 * _TICK  # 15 minutes
    [event] = sorted(_pending(workflow).glob("*.json"))
    assert json.loads(event.read_text())["at"] == first

    # The wake's night check-ins every 20 minutes keep it for the morning.
    for at in range(_at(23, 20), _at(7, 41, day=14), 20 * 60):
        done = workflow.run_script("wake", env=_env(at))
        assert done.returncode == 0, done.stderr
    assert all("answered for the owner" not in sent for sent in workflow.sent())
    night_sent = len(workflow.sent())

    for at in (_at(8, 0, day=14), _at(8, 5, day=14), _at(8, 20, day=14)):
        assert workflow.run_script("wake", env=_env(at)).returncode == 0
    morning = workflow.sent()[night_sent:]
    told = [sent for sent in morning if "answered for the owner" in sent]
    assert len(told) == 1
    assert '\nanswered for the owner: ticket-7, "1" (23:15)\n' in told[0]


# 2. A question that is the owner's alone (money, privacy, legal text) waits all night.


def test_a_question_about_money_marked_yes_waits_all_night(workflow: Workflow) -> None:
    message = (
        "Should example-app move to the paid plan of its mail service?\n\n"
        "Recommendation: 1, the free plan covers this month.\n"
        "1. Stay on the free plan\n2. Pay for the next plan\nOwner only: yes\nReply 1 or 2."
    )
    asked = _at(22, 0)
    _ask(workflow, "ticket-7", message, asked)

    for at in (asked + 900, asked + 1080, asked + 3600, asked + 10 * 3600):
        assert _tick(workflow, at).returncode == 0

    assert _replies(workflow, "ticket-7") == []
    assert not _pending(workflow).exists() or not list(_pending(workflow).iterdir())


# 3. One stop that asks two questions, the first the owner's alone and the second not. The tick
#    must not type the second one's reply while the first, his alone, is open.


@pytest.mark.parametrize("separator", ["\n", "\n\n"], ids=["one paragraph", "two paragraphs"])
def test_two_questions_in_one_stop_the_first_kept_for_the_owner_get_no_reply(
    workflow: Workflow, separator: str
) -> None:
    message = (
        "Two questions before I go on.\n\n"
        "A. Should example-app keep the visitors' addresses for 30 days or 90 days?\n"
        "Recommendation: 1, 30 days.\n"
        "1. 30 days\n2. 90 days\n"
        "Owner only: yes\n"
        "Reply 1 or 2."
        f"{separator}"
        "B. Should the list page load 50 rows or all of them?\n"
        "Recommendation: 1, 50 rows.\n"
        "1. 50 rows\n2. All rows\n"
        "Owner only: no\n"
        "Reply 1 or 2."
    )
    asked = _at(23, 0)
    _ask(workflow, "ticket-7", message, asked)

    for at in (asked + 720, asked + 900, asked + 1080, asked + 3600):
        assert _tick(workflow, at).returncode == 0

    # The session reads "1" as the owner's word on both, the privacy question included.
    assert _replies(workflow, "ticket-7") == []


# 4. The owner's own half-typed reply on the phone is never overwritten nor sent, tick after tick.


def test_the_owners_half_typed_reply_stays_in_the_box_all_night(workflow: Workflow) -> None:
    asked = _at(23, 0)
    _ask(workflow, "ticket-7", _design_question(), asked)
    (workflow.state / "input-ticket-7").write_text("2, but keep")

    for at in (asked + 900, asked + 1080, asked + 1260, asked + 3600, asked + 7200):
        assert _tick(workflow, at).returncode == 0

    assert workflow.calls("tmux ticket-7 send-keys") == []
    assert (workflow.state / "input-ticket-7").read_text() == "2, but keep"
    assert not _pending(workflow).exists() or not list(_pending(workflow).iterdir())


# 5. Many sessions at once: each is handled on its own, and one that cannot be answered does not
#    hold back the others.


@pytest.mark.timeout(900)
def test_many_sessions_each_get_what_their_question_says(workflow: Workflow) -> None:
    asked = _at(23, 0)
    money = _design_question("yes")
    _ask(workflow, "ticket-3", _design_question(), asked)
    _ask(workflow, "ticket-4", money, asked)
    _ask(workflow, "ticket-5", _design_question(None), asked)
    _ask(workflow, "review-6", _design_question(reply='Write "merge it" or "wait".'), asked)
    _ask(workflow, "ticket-8", _design_question(), asked)
    (workflow.state / "input-ticket-8").write_text("let me th")
    _ask(workflow, "audit-11", _design_question(reply="Reply **2** (recommended) or **1**."), asked)
    # The owner answered this one from the phone before the wait was over.
    _ask(workflow, "ticket-12", _design_question(), asked)
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "2"}, session="ticket-12")

    for at in (asked + 720, asked + 900, asked + 1080):
        assert _tick(workflow, at).returncode == 0

    assert _replies(workflow, "ticket-3") == ["1"]
    assert _replies(workflow, "ticket-4") == []
    assert _replies(workflow, "ticket-5") == []
    assert _replies(workflow, "review-6") == ["merge it"]
    assert _replies(workflow, "ticket-8") == []
    assert (workflow.state / "input-ticket-8").read_text() == "let me th"
    assert _replies(workflow, "audit-11") == ["2"]
    assert _replies(workflow, "ticket-12") == []
    names = sorted(json.loads(e.read_text())["name"] for e in _pending(workflow).glob("*.json"))
    assert names == ["audit-11", "review-6", "ticket-3"]
    lines = sorted(
        json.loads(e.read_text())["line"]
        for e in (workflow.repo / ".local/workflow/stuck.pending").glob("*.json")
    )
    assert lines == ["owner question without the Owner only line: ticket-5"]


# 6. A tick that holds its lock too long: the next tick passes over everything, and the one after
#    the lock is free answers once.


def test_a_tick_holding_its_lock_too_long_delays_the_answer_but_never_doubles_it(
    workflow: Workflow,
) -> None:
    asked = _at(23, 0)
    _ask(workflow, "ticket-7", _design_question(), asked)
    common = Path(workflow.git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())

    with open(common / "shipline-tick.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for at in (asked + 900, asked + 1080):
            done = _tick(workflow, at)
            assert done.returncode == 0
            assert "still running" in done.stdout
        assert _replies(workflow, "ticket-7") == []

    for at in (asked + 1260, asked + 1440, asked + 1620):
        assert _tick(workflow, at).returncode == 0
    assert _replies(workflow, "ticket-7") == ["1"]


# 7. A reviewer's question: the reply counts as the owner's, so the reviewer is no longer blocked
#    on him and the wake no longer names it as asking.


def test_a_reviewers_answered_question_no_longer_blocks_it(workflow: Workflow) -> None:
    asked = _at(14, 0)
    workflow.add_pull_request(7, draft=False)
    _ask(workflow, "review-7", _design_question(), asked)
    notes = workflow.notes(7)
    assert (notes / "review-7.owner-blocked").exists()

    assert _tick(workflow, asked + 900).returncode == 0
    assert _replies(workflow, "review-7") == ["1"]
    # What Claude Code's prompt hook sees for text typed into the box.
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "1"}, session="review-7")

    assert not (notes / "review-7.owner-blocked").exists()
    assert not (notes / "review-7.asking").exists()
    assert not (notes / "review-7.question").exists()


# 8. An answer the wake could not deliver in the day because the orchestrator was busy is not lost
#    at 20:00: it waits for the morning.


@pytest.mark.timeout(900)
def test_an_evening_answer_not_delivered_by_20_is_told_in_the_morning(workflow: Workflow) -> None:
    workflow.add_pull_request(9, draft=False)
    workflow.usage(40, "2026-10-14T05:10:00+00:00", seven=20)
    asked = _at(19, 30)
    _ask(workflow, "ticket-7", _design_question(), asked)
    assert _tick(workflow, asked + 900).returncode == 0
    assert _replies(workflow, "ticket-7") == ["1"]
    (workflow.state / "send-fails").touch()
    workflow.orchestrator("idle")
    workflow.run_script("wake", env=_env(_at(19, 50)))
    (workflow.state / "send-fails").unlink()
    tried = len(workflow.sent())

    for at in range(_at(20, 0), _at(7, 41, day=14), 20 * 60):
        workflow.run_script("wake", env=_env(at))
    assert all("answered for the owner" not in sent for sent in workflow.sent()[tried:])

    workflow.run_script("wake", env=_env(_at(8, 0, day=14)))
    assert '\nanswered for the owner: ticket-7, "1" (19:45)\n' in workflow.sent()[-1]


# 9. How a session writes its reply words in use. Each must give the recommended reply or none.


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("Reply 1 (recommended) or 2.", "1"),
        ("Reply **1** to take the recommendation, **2** for all rows.", "1"),
        ("Reply `1` or `2`.", "1"),
        ("Reply with 1 or 2.", "1"),
        ("Reply: 1 or 2.", None),
        # Not read as a question at all (`asks_owner`): it waits for the owner.
        ("Answer 1 or 2.", None),
        ('Type "merge it" to merge.', "merge it"),
    ],
)
def test_the_reply_words_sessions_write(workflow: Workflow, reply: str, expected: str | None) -> None:
    asked = _at(23, 0)
    _ask(workflow, "ticket-7", _design_question(reply=reply), asked)

    assert _tick(workflow, asked + 900).returncode == 0

    assert _replies(workflow, "ticket-7") == ([] if expected is None else [expected])


# 10. The line placed above the choices, not just before the reply words (against the rule): a
#     choice's own words must not be taken for the reply.


def test_the_line_above_the_choices_does_not_answer_with_a_choices_words(
    workflow: Workflow,
) -> None:
    message = (
        "How should the example-app import be tested?\n\n"
        "Owner only: no\n"
        "Recommendation: 1.\n"
        "1. Add a test per file format\n"
        "2. Write 2 end-to-end tests only\n"
        "Reply 1 or 2."
    )
    asked = _at(23, 0)
    _ask(workflow, "ticket-7", message, asked)

    assert _tick(workflow, asked + 900).returncode == 0

    # "Write 2" in the second choice is not the recommended reply.
    assert _replies(workflow, "ticket-7") in ([], ["1"])
