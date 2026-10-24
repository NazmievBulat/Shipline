"""The tick keeps waits moving: it keeps a waiting session's cache alive, tells a
worker that is ready but not finished to finish, nudges a stuck session and names it in the
wake, and names an idle session that no rule covers."""

import json
import os
import shlex
from pathlib import Path

import pytest
from workflow_harness import Workflow

_NOW = 1_790_000_000
_BOX = "─" * 40 + "\n> \n" + "─" * 40 + "\n  ? for shortcuts\n"
_STATUS = (
    "Status check: if what you were waiting for is done, carry on; if not, run "
    "`scripts/workflow/notify waiting` and end your turn."
)
_FINISH = "Pull request 7 is ready, but you have not finished."
_STILL = (
    "The command has produced nothing for 45 minutes. Check whether it is still running and go on."
)
_LOOP = "That loop finds its own command line and never ends. Check the process by its number."
_SCREENS = Path(__file__).parent / "fixtures" / "screens"


def _captured(name: str) -> str:
    """A screen captured from a session, kept under `fixtures/screens`."""
    return (_SCREENS / f"{name}.txt").read_text()


def _tick(workflow: Workflow, at: int) -> None:
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(at)})
    assert done.returncode == 0, done.stderr


def _written(path: Path, at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n")
    os.utime(path, (at, at))


def _typed(workflow: Workflow, name: str, words: str) -> list[str]:
    return workflow.calls(f"tmux {name} send-keys -t ={name}: -l {words}")


def _session(workflow: Workflow, name: str = "ticket-7", *, draft: bool = True) -> Path:
    """A running Claude session of pull request 7. Returns the prefix of its notes."""
    workflow.add_worktree(7, draft=draft)
    workflow.running(name)
    workflow.screen("● Waiting for the full Python run in the background.\n" + _BOX, name)
    return workflow.notes(7) / name


def _waiting(workflow: Workflow, name: str = "ticket-7", since: int = _NOW - 3000) -> Path:
    """A session that ended its turn with `notify waiting` at `since`."""
    notes = _session(workflow, name)
    _written(notes.with_name(f"{name}.job-wait"), since)
    return notes


# 1. Cache keep-alive.


@pytest.mark.parametrize("name", ["ticket-7", "review-7"])
def test_a_session_waiting_50_minutes_gets_one_status_check_and_it_is_logged(
    workflow: Workflow, name: str
) -> None:
    _waiting(workflow, name, since=_NOW - 2999)
    _tick(workflow, _NOW)
    assert _typed(workflow, name, "Status") == []

    _tick(workflow, _NOW + 1)
    _tick(workflow, _NOW + 600)

    sent = _typed(workflow, name, "Status")
    assert len(sent) == 1
    assert sent[0].endswith(_STATUS)
    assert workflow.calls(f"tmux {name} send-keys")[-1].endswith(" Enter")
    assert f"{name} idle 50 min waiting for its own job or turn; status check sent" in (
        workflow.wake_log.read_text()
    )


def test_a_status_check_typed_but_not_sent_is_sent_at_the_next_tick(workflow: Workflow) -> None:
    notes = _waiting(workflow)
    # The last tick typed it, and the session did not take the Enter.
    notes.with_name("ticket-7.typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)

    _tick(workflow, _NOW)

    assert workflow.calls("tmux ticket-7 send-keys")[-1].endswith(" Enter")
    assert (workflow.state / "input-ticket-7").read_text() == ""
    assert "status check sent" in workflow.wake_log.read_text()


@pytest.mark.parametrize("kind", ["status", "finish"])
def test_owner_text_over_a_stale_note_of_the_tick_is_left_alone(
    workflow: Workflow, kind: str
) -> None:
    if kind == "status":
        notes = _waiting(workflow)
    else:
        notes = _ready_and_idle(workflow, idle_since=_NOW - 3600)
        _tick(workflow, _NOW - 900)
    notes.with_name("ticket-7.typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text("merge it")

    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 180)

    assert workflow.calls("tmux ticket-7 send-keys") == []
    assert (workflow.state / "input-ticket-7").read_text() == "merge it"


def test_a_status_check_that_could_not_be_typed_is_tried_again(workflow: Workflow) -> None:
    _waiting(workflow)
    (workflow.state / "send-keys-fails").touch()
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})
    assert done.returncode != 0
    assert "ticket-7 status check failed" in workflow.wake_log.read_text()

    (workflow.state / "send-keys-fails").unlink()
    _tick(workflow, _NOW + 180)
    _tick(workflow, _NOW + 360)

    # The failed attempt and the one that went.
    assert len(_typed(workflow, "ticket-7", "Status")) == 2
    assert workflow.calls("tmux ticket-7 send-keys")[-1].endswith(" Enter")
    assert "status check sent" in workflow.wake_log.read_text()


def test_a_new_wait_after_the_answer_is_a_new_stretch(workflow: Workflow) -> None:
    notes = _waiting(workflow)
    _tick(workflow, _NOW)
    # The session answered and ended its turn waiting again, ten minutes later.
    _written(notes.with_name("ticket-7.job-wait"), _NOW + 600)
    _tick(workflow, _NOW + 600 + 2999)
    assert len(_typed(workflow, "ticket-7", "Status")) == 1

    _tick(workflow, _NOW + 600 + 3000)

    assert len(_typed(workflow, "ticket-7", "Status")) == 2


def test_a_reviewer_waiting_for_its_checks_from_before_this_note_is_kept_alive(
    workflow: Workflow,
) -> None:
    notes = _session(workflow, "review-7")
    _written(notes.with_name("review-7.checks-wait"), _NOW - 3000)

    _tick(workflow, _NOW)

    assert len(_typed(workflow, "review-7", "Status")) == 1


def test_a_reviewer_idle_for_its_turn_in_the_merge_train_is_kept_alive(
    workflow: Workflow,
) -> None:
    notes = _session(workflow, "review-7")
    _written(notes.with_name("review-7.idle"), _NOW - 3000)
    (workflow.script.parent / "merge-when-current").write_text(
        '#!/bin/bash\necho \'{"first":"5","waiting":["5","7"],"blocked":[],"turn":1}\'\n'
    )

    _tick(workflow, _NOW)

    assert len(_typed(workflow, "review-7", "Status")) == 1


def test_a_reviewer_woken_for_its_checks_gets_no_status_check_in_the_same_tick(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("review-7")
    workflow.screen("● The checks are still running; I am waiting for them.\n" + _BOX, "review-7")
    notes = workflow.notes(7) / "review-7"
    _written(notes.with_name("review-7.started"), _NOW - 7200)
    _written(notes.with_name("review-7.checks-wait"), _NOW - 3000)
    (workflow.state / "rollup-7.json").write_text(
        json.dumps(
            [
                {"__typename": "CheckRun", "name": name, "status": "COMPLETED"}
                | {"conclusion": "SUCCESS"}
                for name in ("CI result", "Docker images: what to run")
            ]
        )
    )

    _tick(workflow, _NOW)

    assert len(_typed(workflow, "review-7", "The checks on ")) == 1
    assert _typed(workflow, "review-7", "Status") == []


def test_an_idle_session_waiting_for_nothing_named_is_not_kept_alive(
    workflow: Workflow,
) -> None:
    notes = _session(workflow)
    _written(notes.with_name("ticket-7.idle"), _NOW - 3000)

    _tick(workflow, _NOW)

    assert _typed(workflow, "ticket-7", "Status") == []


@pytest.mark.parametrize(
    "case",
    ["owner-blocked", "asking", "question on screen", "finished", "limit", "typed", "codex"],
)
def test_a_session_that_must_not_be_disturbed_gets_no_status_check(
    workflow: Workflow, case: str
) -> None:
    name = "review-7" if case == "owner-blocked" else "ticket-7"
    notes = _waiting(workflow, name)
    if case in ("owner-blocked", "asking", "codex"):
        _written(notes.with_name(f"{name}.{case}"), _NOW)
    elif case == "question on screen":
        workflow.screen("● Should the limit be 3 or 5? Reply 1 or 2.\n" + _BOX, name)
    elif case == "finished":
        workflow.screen("● All done.\n\nworker finished\n" + _BOX, name)
    elif case == "limit":
        workflow.screen(
            "● Waiting.\n  ⎿  You've hit your session limit · resets 4pm\n" + _BOX, name
        )
    elif case == "typed":
        (workflow.state / f"input-{name}").write_text("merge it")

    _tick(workflow, _NOW)

    assert _typed(workflow, name, "Status") == []


def test_audits_and_hunts_are_not_kept_alive(workflow: Workflow) -> None:
    _waiting(workflow, "audit-7")
    hunt = workflow.add_hunt("plan-1")
    workflow.running("hunt-plan-1")
    workflow.screen("● Waiting.\n" + _BOX, "hunt-plan-1")
    _written(hunt / ".local" / "workflow" / "hunt-plan-1.job-wait", _NOW - 3000)

    _tick(workflow, _NOW)

    assert _typed(workflow, "audit-7", "Status") == []
    assert _typed(workflow, "hunt-plan-1", "Status") == []


# 2. Ready without finishing.


def _ready_and_idle(workflow: Workflow, idle_since: int) -> Path:
    notes = _session(workflow, draft=False)
    workflow.screen("● Marked ready. The reviewer takes it from here.\n" + _BOX, "ticket-7")
    _written(notes.with_name("ticket-7.idle"), idle_since)
    # A stop on a ready pull request rings the phone, so these notes are always there.
    _written(notes.with_name("ticket-7.asking"), idle_since)
    _written(notes.with_name("ticket-7.pushed"), idle_since)
    return notes


def test_a_worker_idle_15_minutes_on_a_ready_pull_request_is_told_once_to_finish(
    workflow: Workflow,
) -> None:
    _ready_and_idle(workflow, idle_since=_NOW - 3600)
    # Ready is counted from the first tick that saw it ready.
    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 899)
    assert _typed(workflow, "ticket-7", "Pull") == []

    _tick(workflow, _NOW + 900)
    _tick(workflow, _NOW + 1800)

    sent = _typed(workflow, "ticket-7", "Pull")
    assert len(sent) == 1
    assert _FINISH in sent[0]
    assert sent[0].endswith("then say `worker finished` on a line of its own.")
    assert "ticket-7 idle on ready pull request 7 without worker finished; told to finish" in (
        workflow.wake_log.read_text()
    )


def test_ready_for_long_but_idle_only_briefly_waits_for_15_idle_minutes(
    workflow: Workflow,
) -> None:
    notes = _ready_and_idle(workflow, idle_since=_NOW)
    _tick(workflow, _NOW - 3600)
    _written(notes.with_name("ticket-7.idle"), _NOW)

    _tick(workflow, _NOW + 899)
    assert _typed(workflow, "ticket-7", "Pull") == []
    _tick(workflow, _NOW + 900)
    assert len(_typed(workflow, "ticket-7", "Pull")) == 1


@pytest.mark.parametrize("case", ["question on screen", "draft", "busy", "codex"])
def test_a_ready_worker_that_asks_works_or_is_codex_is_not_told(
    workflow: Workflow, case: str
) -> None:
    notes = _ready_and_idle(workflow, idle_since=_NOW - 3600)
    if case == "question on screen":
        workflow.screen(
            "● Shall I also open the follow-up ticket? Reply 1 for yes, 2 for no.\n" + _BOX,
            "ticket-7",
        )
    elif case == "draft":
        workflow.list_pull_request(7, draft=True)
    elif case == "busy":
        notes.with_name("ticket-7.idle").unlink()
    elif case == "codex":
        _written(notes.with_name("ticket-7.codex"), _NOW)

    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 900)

    assert _typed(workflow, "ticket-7", "Pull") == []


@pytest.mark.parametrize(
    "asked",
    [
        "Please reply with your choice.",
        "Reply **1** to open the follow-up ticket, **2** to leave it.",
        "Choose 1 or 2.",
        'Write "open it" and I open the follow-up ticket.',
        "Which one should I open?**",
    ],
)
def test_a_ready_worker_asking_without_a_question_mark_at_the_end_is_not_told(
    workflow: Workflow, asked: str
) -> None:
    # The same requests `notify` reads as a question to the owner on a stop.
    _ready_and_idle(workflow, idle_since=_NOW - 3600)
    workflow.screen(f"● The follow-up ticket is drafted.\n\n  {asked}\n" + _BOX, "ticket-7")

    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 900)

    assert _typed(workflow, "ticket-7", "Pull") == []


def test_ready_again_after_a_send_back_is_a_new_stretch(workflow: Workflow) -> None:
    _ready_and_idle(workflow, idle_since=_NOW - 3600)
    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 900)
    assert len(_typed(workflow, "ticket-7", "Pull")) == 1
    # Sent back to draft, then ready again.
    workflow.list_pull_request(7, draft=True)
    _tick(workflow, _NOW + 1200)
    workflow.list_pull_request(7, draft=False)
    _tick(workflow, _NOW + 1500)
    _tick(workflow, _NOW + 1500 + 899)
    assert len(_typed(workflow, "ticket-7", "Pull")) == 1

    _tick(workflow, _NOW + 1500 + 900)

    assert len(_typed(workflow, "ticket-7", "Pull")) == 2


def test_a_reviewer_idle_on_a_ready_pull_request_is_not_told_to_finish(
    workflow: Workflow,
) -> None:
    notes = _session(workflow, "review-7", draft=False)
    _written(notes.with_name("review-7.idle"), _NOW - 3600)

    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 900)

    assert _typed(workflow, "review-7", "Pull") == []


def test_a_worker_that_said_worker_finished_is_stopped_not_told(workflow: Workflow) -> None:
    _ready_and_idle(workflow, idle_since=_NOW - 3600)
    workflow.screen("● Ready.\n\nworker finished\n" + _BOX, "ticket-7")

    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 900)

    assert not workflow.is_running("ticket-7")
    assert _typed(workflow, "ticket-7", "Pull") == []


# 3. A busy screen that stops changing.


def _tool_shell(command: str) -> list[str]:
    return [
        "/bin/bash",
        "-c",
        "source /home/tester/.claude/shell-snapshots/snapshot-bash-1-abc.sh 2>/dev/null || true"
        f" && eval {shlex.quote(command)} < /dev/null && pwd -P >| /tmp/claude-a589-cwd",
    ]


def _runs(workflow: Workflow, name: str, command: str, pid: int = 6000) -> None:
    """The session's Bash tool runs `command`, in the session's own control group. A higher
    `pid` is a command started later."""
    group = workflow.cgroup_root / f"user.slice/app.slice/tmux-spawn-{name}.scope"
    folder = workflow.root / "proc" / str(pid)
    folder.mkdir(parents=True)
    (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in _tool_shell(command)) + b"\0")
    with (group / "cgroup.procs").open("a") as procs:
        procs.write(f"{pid}\n")


def _busy_screen(
    clock: str,
    body: str = "● Bash(pytest tests)\n  ⎿  Running… (",
    spinner: str = "✻ Cogitating",
    status: str = "  ? for shortcuts",
) -> str:
    box = _BOX.replace("  ? for shortcuts", status)
    return f"{body}{clock})\n\n{spinner}… ({clock} · ↓ 2.1k tokens · esc to interrupt)\n\n{box}"


def _pending(workflow: Workflow) -> list[str]:
    folder = workflow.repo / ".local" / "workflow" / "stuck.pending"
    if not folder.exists():
        return []
    return sorted(json.loads(event.read_text())["line"] for event in folder.iterdir())


def _keys(workflow: Workflow, name: str) -> list[str]:
    """What was sent to the session, a word each: `Escape`, `Enter` or the typed text."""
    prefix = f"tmux {name} send-keys -t ={name}: "
    return [
        call.removeprefix(prefix).removeprefix("-l ")
        for call in workflow.calls(f"tmux {name} send-keys")
    ]


def _still(workflow: Workflow, name: str = "ticket-7") -> None:
    """A busy session whose screen has not changed since the first of two captures."""
    _session(workflow, name)
    _runs(workflow, name, "pytest tests\n  -x")
    workflow.screen(_captured("busy-early"), name)
    _tick(workflow, _NOW)
    # Only the clocks, the spinner and the status line under the box moved.
    workflow.screen(_captured("busy-later"), name)


@pytest.mark.parametrize("name", ["ticket-7", "review-7"])
def test_a_busy_screen_unchanged_for_45_minutes_is_nudged_once_and_named(
    workflow: Workflow, name: str
) -> None:
    _still(workflow, name)
    _tick(workflow, _NOW + 2699)
    assert _pending(workflow) == []
    assert _keys(workflow, name) == []

    _tick(workflow, _NOW + 2700)
    _tick(workflow, _NOW + 3600)

    # Escape first, then the fixed text into the empty box, then the key that sends it.
    assert _keys(workflow, name) == ["Escape", _STILL, "Enter"]
    line = f"stuck? {name} (45 min, running: pytest tests); nudge sent"
    assert _pending(workflow) == [line]
    assert line in workflow.wake_log.read_text()


def test_a_still_screen_is_nudged_again_on_a_new_head(workflow: Workflow) -> None:
    _still(workflow)
    _tick(workflow, _NOW + 2700)
    assert _keys(workflow, "ticket-7").count(_STILL) == 1
    # The interrupted session drew nothing new, but it committed.
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "fix: x", cwd=workflow.worktree(7))

    _tick(workflow, _NOW + 2880)

    assert _keys(workflow, "ticket-7").count(_STILL) == 2


def test_a_still_screen_with_unsent_text_is_named_and_not_touched(workflow: Workflow) -> None:
    _still(workflow)
    (workflow.state / "input-ticket-7").write_text("also run the browser tests")

    _tick(workflow, _NOW + 2700)

    assert _keys(workflow, "ticket-7") == []
    assert (workflow.state / "input-ticket-7").read_text() == "also run the browser tests"
    assert _pending(workflow) == ["stuck? ticket-7 (45 min, running: pytest tests)"]


def test_a_still_screen_asking_the_owner_is_named_and_not_touched(workflow: Workflow) -> None:
    _session(workflow)
    workflow.screen(_captured("busy-asks-owner"), "ticket-7")
    _tick(workflow, _NOW)

    _tick(workflow, _NOW + 2700)

    assert _keys(workflow, "ticket-7") == []
    assert _pending(workflow) == ["stuck? ticket-7 (45 min, running: no command)"]


def test_text_that_escape_puts_back_in_the_box_is_not_sent(workflow: Workflow) -> None:
    _still(workflow)
    # A message the owner queued while the command ran comes back into the box.
    (workflow.state / "input-after-escape-ticket-7").write_text("and then the docs")

    _tick(workflow, _NOW + 2700)

    assert _keys(workflow, "ticket-7") == ["Escape"]
    assert (workflow.state / "input-ticket-7").read_text() == "and then the docs"
    assert _pending(workflow) == [
        "stuck? ticket-7 (45 min, running: pytest tests); interrupted, text left in its box"
    ]


def test_a_message_of_the_tick_left_in_the_box_is_not_sent_for_a_nudge(
    workflow: Workflow,
) -> None:
    _still(workflow)
    # An older message of the tick's own that did not go.
    typed = workflow.notes(7) / "ticket-7.typed"
    _written(typed, _NOW)
    typed.write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)

    _tick(workflow, _NOW + 2700)

    assert _keys(workflow, "ticket-7") == []
    assert _pending(workflow) == ["stuck? ticket-7 (45 min, running: pytest tests)"]


def test_a_session_that_turns_busy_starts_its_45_minutes_then(workflow: Workflow) -> None:
    notes = _session(workflow)
    workflow.screen(_captured("busy-early"), "ticket-7")
    _written(notes.with_name("ticket-7.idle"), _NOW)
    _tick(workflow, _NOW)
    # Its next prompt took away the idle note; the screen above the box looks the same.
    notes.with_name("ticket-7.idle").unlink()

    _tick(workflow, _NOW + 2700)
    assert _keys(workflow, "ticket-7") == []
    _tick(workflow, _NOW + 2700 + 2700)

    assert _keys(workflow, "ticket-7") == ["Escape", _STILL, "Enter"]


def test_a_nudge_that_could_not_be_typed_is_named_as_failed(workflow: Workflow) -> None:
    _still(workflow)
    (workflow.state / "send-keys-fails").touch()

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 2700)})

    assert done.returncode != 0
    assert _pending(workflow) == ["stuck? ticket-7 (45 min, running: pytest tests); nudge failed"]
    assert "ticket-7 stuck nudge failed" in workflow.wake_log.read_text()


def test_the_tick_notes_when_a_screen_last_changed_for_session_list(workflow: Workflow) -> None:
    _still(workflow)
    _tick(workflow, _NOW + 600)

    listed = workflow.run("list")

    assert listed.returncode == 0, listed.stderr
    # The first capture: the second differs only in what the sum leaves out.
    assert listed.stdout.splitlines() == [
        "ticket-7  busy  0 MB held  0 MB in swap  screen changed Sep 21, 2026, 2:13pm (UTC)"
    ]


def test_a_screen_that_changes_starts_the_45_minutes_again(workflow: Workflow) -> None:
    _session(workflow)
    workflow.screen(_busy_screen("1m"), "ticket-7")
    _tick(workflow, _NOW)
    workflow.screen(
        _busy_screen("30m", body="● 120 passed\n● Bash(npm run test:web)\n  ⎿  Running… ("),
        "ticket-7",
    )
    _tick(workflow, _NOW + 1800)

    _tick(workflow, _NOW + 2700)
    assert _pending(workflow) == []
    _tick(workflow, _NOW + 1800 + 2700)
    assert _pending(workflow) == ["stuck? ticket-7 (45 min, running: no command); nudge sent"]


@pytest.mark.parametrize("note", ["idle", "job-wait", "checks-wait"])
def test_a_session_that_ended_its_turn_is_not_called_stuck(workflow: Workflow, note: str) -> None:
    notes = _session(workflow, "review-7")
    _written(notes.with_name(f"review-7.{note}"), _NOW)
    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 2700)

    assert _pending(workflow) == []


def test_a_permission_dialog_is_waiting_for_the_owner_not_stuck(workflow: Workflow) -> None:
    _session(workflow)
    dialog = (
        "● Bash(git push origin ticket/change-7)\n\n"
        "╭──────────────────────────────╮\n"
        "│ Bash command                 │\n"
        "│ Do you want to proceed?      │\n"
        "│ \u276f 1. Yes                     │\n"
        "│   2. No                      │\n"
        "╰──────────────────────────────╯\n"
    )
    workflow.screen(dialog, "ticket-7")
    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 3600)

    assert _pending(workflow) == []
    assert _keys(workflow, "ticket-7") == []


# 4. A wait loop that finds itself.


def test_a_wait_loop_that_finds_its_own_shell_is_nudged_once_and_named(
    workflow: Workflow,
) -> None:
    _session(workflow)
    workflow.screen(_captured("busy-loop"), "ticket-7")
    _runs(workflow, "ticket-7", 'while pgrep -f "codex exec" >/dev/null; do sleep 30; done')

    _tick(workflow, _NOW)
    # Interrupted, the session drew its answer; the loop is still the one that ran.
    workflow.screen(_captured("interrupted"), "ticket-7")
    _tick(workflow, _NOW + 180)

    assert _keys(workflow, "ticket-7") == ["Escape", _LOOP, "Enter"]
    assert _pending(workflow) == [
        'wait loop finds itself? ticket-7 (running: while pgrep -f "codex exec" >/dev/null; '
        "do sleep 30; done); nudge sent"
    ]


def test_a_wait_loop_left_in_the_background_does_not_interrupt_other_work(
    workflow: Workflow,
) -> None:
    _session(workflow)
    _runs(workflow, "ticket-7", 'while pgrep -f "codex exec" >/dev/null; do sleep 30; done')
    _runs(workflow, "ticket-7", "pytest tests", pid=6001)

    _tick(workflow, _NOW)

    assert _keys(workflow, "ticket-7") == []
    assert _pending(workflow) == [
        'wait loop finds itself? ticket-7 (running: while pgrep -f "codex exec" >/dev/null; '
        "do sleep 30; done)"
    ]


@pytest.mark.parametrize("case", ["typed", "permission", "asks owner", "idle", "codex"])
def test_a_wait_loop_in_a_session_that_must_not_be_disturbed_is_only_named(
    workflow: Workflow, case: str
) -> None:
    notes = _session(workflow)
    workflow.screen(_captured("busy-loop"), "ticket-7")
    _runs(workflow, "ticket-7", 'while pgrep -f "codex exec" >/dev/null; do sleep 30; done')
    if case == "typed":
        (workflow.state / "input-ticket-7").write_text("merge it")
    elif case == "permission":
        workflow.screen(_captured("permission-dialog"), "ticket-7")
    elif case == "asks owner":
        workflow.screen(_captured("busy-asks-owner"), "ticket-7")
    elif case == "idle":
        # A loop the session left running in the background after its turn.
        _written(notes.with_name("ticket-7.idle"), _NOW)
    else:
        _written(notes.with_name("ticket-7.codex"), _NOW)

    _tick(workflow, _NOW)

    assert _keys(workflow, "ticket-7") == []
    if case != "codex":
        assert _pending(workflow) == [
            'wait loop finds itself? ticket-7 (running: while pgrep -f "codex exec" >/dev/null; '
            "do sleep 30; done)"
        ]


def test_a_wait_loop_that_can_end_is_not_named(workflow: Workflow) -> None:
    _session(workflow)
    _runs(workflow, "ticket-7", 'while pgrep -f "[c]odex exec" >/dev/null; do sleep 30; done')

    _tick(workflow, _NOW)

    assert _pending(workflow) == []


# 5. An idle session that no rule covers.


def _idle(workflow: Workflow, since: int, name: str = "ticket-7") -> Path:
    notes = _session(workflow, name)
    workflow.screen(_captured("idle-no-rule"), name)
    _written(notes.with_name(f"{name}.idle"), since)
    return notes


@pytest.mark.parametrize("name", ["ticket-7", "review-7"])
def test_an_idle_session_no_rule_covers_is_named_once_after_50_minutes(
    workflow: Workflow, name: str
) -> None:
    _idle(workflow, _NOW - 2999, name)
    _tick(workflow, _NOW)
    assert _pending(workflow) == []

    _tick(workflow, _NOW + 1)
    _tick(workflow, _NOW + 600)

    line = f"idle, no rule: {name} (50 min, last line: The pull request stays a draft for now.)"
    assert _pending(workflow) == [line]
    assert line in workflow.wake_log.read_text()
    # The tick types nothing it has no rule for.
    assert _keys(workflow, name) == []


def test_an_idle_session_no_rule_covers_is_named_again_on_a_new_head(workflow: Workflow) -> None:
    _idle(workflow, _NOW - 3000)
    _tick(workflow, _NOW)
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "fix: x", cwd=workflow.worktree(7))

    _tick(workflow, _NOW + 180)

    assert len(_pending(workflow)) == 2


@pytest.mark.parametrize(
    "case", ["job-wait", "checks-wait", "asking", "question on screen", "typed", "finished"]
)
def test_an_idle_session_a_rule_covers_or_that_waits_for_the_owner_is_not_named(
    workflow: Workflow, case: str
) -> None:
    notes = _idle(workflow, _NOW - 3000)
    if case in ("job-wait", "checks-wait", "asking"):
        _written(notes.with_name(f"ticket-7.{case}"), _NOW - 3000)
    elif case == "question on screen":
        workflow.screen("● Should the limit be 3 or 5? Reply 1 or 2.\n" + _BOX, "ticket-7")
    elif case == "typed":
        (workflow.state / "input-ticket-7").write_text("merge it")
    else:
        workflow.screen("● All done.\n\nworker finished\n" + _BOX, "ticket-7")

    _tick(workflow, _NOW)

    assert [line for line in _pending(workflow) if line.startswith("idle, no rule")] == []


def test_a_worker_idle_on_a_ready_pull_request_is_told_to_finish_not_named(
    workflow: Workflow,
) -> None:
    _ready_and_idle(workflow, idle_since=_NOW - 3600)
    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 900)

    assert _pending(workflow) == []


# The wake tells the orchestrator.


def test_the_wake_names_a_stuck_session_once_at_any_hour_and_busy(workflow: Workflow) -> None:
    folder = workflow.repo / ".local" / "workflow" / "stuck.pending"
    folder.mkdir(parents=True)
    line = "stuck? ticket-7 (45 min, running: pytest tests)"
    (folder / "ticket-7-stuck-1.json").write_text(json.dumps({"line": line}))
    workflow.orchestrator(status="busy")
    # Noon: outside the night's check-ins.
    noon = 1_790_078_400

    first = workflow.run_script("wake", env={"WORKFLOW_NOW": str(noon), "TZ": "UTC"})
    second = workflow.run_script("wake", env={"WORKFLOW_NOW": str(noon + 600), "TZ": "UTC"})

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    sent = workflow.sent()
    assert len(sent) == 1
    assert f"\n{line}\n" in sent[0]
    assert list(folder.iterdir()) == []
    assert (workflow.repo / ".local/workflow/stuck.sent/ticket-7-stuck-1.json").exists()


def test_a_stuck_line_the_wake_could_not_send_stays_until_it_is_sent(workflow: Workflow) -> None:
    folder = workflow.repo / ".local" / "workflow" / "stuck.pending"
    folder.mkdir(parents=True)
    line = "wait loop finds itself? ticket-7 (running: while pgrep -f codex; do sleep 30; done)"
    (folder / "ticket-7-loop-1.json").write_text(json.dumps({"line": line}))
    workflow.orchestrator()
    (workflow.state / "send-fails").touch()
    noon = 1_790_078_400
    workflow.run_script("wake", env={"WORKFLOW_NOW": str(noon), "TZ": "UTC"})
    assert [event.name for event in folder.iterdir()] == ["ticket-7-loop-1.json"]

    (workflow.state / "send-fails").unlink()
    done = workflow.run_script("wake", env={"WORKFLOW_NOW": str(noon + 300), "TZ": "UTC"})

    assert done.returncode == 0, done.stderr
    assert f"\n{line}\n" in workflow.sent()[-1]
    assert list(folder.iterdir()) == []
