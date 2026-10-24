"""Breaking tests for pull request 13: the tick nudges a stuck session by rule.

Each test replays a day-to-day flow of an installation: a tick every 3 minutes, Claude workers
and reviewers in their own tmux servers, an owner who follows them from a phone and cannot type
into a pane, and the failures such an installation meets. Names and values are neutral.
"""

import json
import os
import shlex
from pathlib import Path

from workflow_harness import Workflow

_NOW = 1_790_000_000
_TICK = 180
_BOX = "─" * 40 + "\n❯ \n" + "─" * 40 + "\n  ? for shortcuts\n"
_STILL = (
    "The command has produced nothing for 45 minutes. Check whether it is still running and go on."
)
_LOOP = "That loop finds its own command line and never ends. Check the process by its number."
_STATUS = "Status check: if what you were waiting for is done, carry on;"

_TEST_RUN_EARLY = (
    "● Bash(python -m pytest -q tests)\n"
    "  ⎿  Running… (1m 12s)\n\n"
    "✻ Cogitating… (1m 12s · ↓ 3.4k tokens · esc to interrupt)\n\n" + _BOX
)
_TEST_RUN_LATER = (
    "● Bash(python -m pytest -q tests)\n"
    "  ⎿  Running… (47m 40s)\n\n"
    "✶ Pondering… (47m 40s · ↓ 3.4k tokens · esc to interrupt)\n\n"
    + _BOX.replace("  ? for shortcuts", "  14% context left")
)
_ANSWERED = (
    "● The run had ended; the results are in. Going on with the fix.\n\n"
    "● Read(scripts/example.py)\n  ⎿  Read 40 lines\n\n"
    "✻ Cogitating… (5s · esc to interrupt)\n\n" + _BOX
)


def _tick(workflow: Workflow, at: int) -> None:
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(at)})
    assert done.returncode == 0, done.stderr


def _ticks(workflow: Workflow, start: int, end: int, every: int = _TICK) -> None:
    """Ticks from `start` up to `end`. The timer runs every 3 minutes; a long stretch with
    nothing due is ticked more sparsely, to keep the test short."""
    for at in range(start, end + 1, every):
        _tick(workflow, at)


def _hours(workflow: Workflow, end: int) -> None:
    """The timer's ticks around the 45-minute mark, then every 15 minutes up to `end`."""
    _ticks(workflow, _NOW, _NOW + 360)
    _ticks(workflow, _NOW + 2520, _NOW + 3060)
    _ticks(workflow, _NOW + 3600, end, 900)


def _written(path: Path, at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n")
    os.utime(path, (at, at))


def _session(workflow: Workflow, name: str = "ticket-7", screen: str = _TEST_RUN_EARLY) -> Path:
    workflow.add_worktree(7)
    workflow.running(name)
    workflow.screen(screen, name)
    return workflow.notes(7) / name


def _tool_shell(command: str) -> list[str]:
    return [
        "/bin/bash",
        "-c",
        (
            "source /var/tmp/example/.claude/shell-snapshots/snapshot-bash-1-abc.sh 2>/dev/null"
            f" || true && eval {shlex.quote(command)} < /dev/null && pwd -P >| /tmp/claude-1-cwd"
        ),
    ]


def _runs(workflow: Workflow, name: str, command: str, pid: int = 6000) -> None:
    group = workflow.cgroup_root / f"user.slice/app.slice/tmux-spawn-{name}.scope"
    folder = workflow.root / "proc" / str(pid)
    folder.mkdir(parents=True)
    (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in _tool_shell(command)) + b"\0")
    with (group / "cgroup.procs").open("a") as procs:
        procs.write(f"{pid}\n")


def _keys(workflow: Workflow, name: str = "ticket-7") -> list[str]:
    prefix = f"tmux {name} send-keys -t ={name}: "
    return [
        call.removeprefix(prefix).removeprefix("-l ")
        for call in workflow.calls(f"tmux {name} send-keys")
    ]


def _pending(workflow: Workflow) -> list[str]:
    folder = workflow.repo / ".local" / "workflow" / "stuck.pending"
    if not folder.exists():
        return []
    return sorted(json.loads(event.read_text())["line"] for event in folder.iterdir())


def _box(workflow: Workflow, name: str = "ticket-7") -> str:
    typed = workflow.state / f"input-{name}"
    return typed.read_text() if typed.exists() else ""


# 1. A test run that prints nothing for long, ticked every 3 minutes.


def test_a_silent_test_run_is_nudged_once_per_stretch_at_the_timer_cadence(
    workflow: Workflow,
) -> None:
    _session(workflow)
    _runs(workflow, "ticket-7", "python -m pytest -q tests")
    _tick(workflow, _NOW)
    workflow.screen(_TEST_RUN_LATER, "ticket-7")
    # What the session draws once it took the nudge.
    (workflow.state / "screen-after-enter-ticket-7").write_text(_ANSWERED)

    _ticks(workflow, _NOW + 2520, _NOW + 3060)
    _ticks(workflow, _NOW + 3600, _NOW + 5400, 900)

    assert _keys(workflow) == ["Escape", _STILL, "Enter"]
    assert [line for line in _pending(workflow) if line.startswith("stuck?")] == [
        "stuck? ticket-7 (45 min, running: python -m pytest -q tests); nudge sent"
    ]


# 2. Boxes and dialogs the tick must never type into.


def test_a_permission_dialog_drawn_under_one_rule_is_never_pressed(
    workflow: Workflow,
) -> None:
    dialog = (
        "● Bash(git push origin ticket/change-7)\n\n" + "─" * 40 + "\n"
        " Bash command\n\n"
        "   git push origin ticket/change-7\n"
        "   Push the branch\n\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again for git push commands in example-app\n"
        "   3. No, and tell Claude what to do differently (esc)\n"
    )
    _session(workflow, screen=dialog)
    _runs(workflow, "ticket-7", "git push origin ticket/change-7")

    _hours(workflow, _NOW + 3 * 2700)

    assert _keys(workflow) == []


def test_a_question_tool_dialog_on_a_worker_is_never_pressed(
    workflow: Workflow,
) -> None:
    # A worker's question tool leaves no interaction note (only a reviewer's does).
    dialog = (
        "● I need one decision before going on.\n\n" + "─" * 40 + "\n"
        " ← ☐ Limit  ✔ Submit →\n\n"
        "Should the retry limit be 3 or 5?\n\n"
        "❯ 1. 3\n     Fewer retries\n"
        "  2. 5\n     More retries\n"
        "  3. Type something.\n" + "─" * 40 + "\n"
        "  4. Chat about this\n\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )
    _session(workflow, screen=dialog)

    _hours(workflow, _NOW + 3 * 2700)

    assert _keys(workflow) == []


def test_a_session_the_owner_interrupted_is_not_set_going_again(
    workflow: Workflow,
) -> None:
    # An interrupt runs no Stop hook: no idle note, so the session still lists as busy.
    interrupted = (
        "● Bash(python -m pytest -q tests)\n"
        "  ⎿  Interrupted · What should Claude do instead?\n\n" + _BOX
    )
    _session(workflow, screen=interrupted)

    _hours(workflow, _NOW + 3 * 2700)

    assert _keys(workflow) == []


# 3. What a nudge leaves behind when it goes wrong.


def test_a_nudge_whose_enter_was_swallowed_is_sent_at_a_later_tick(
    workflow: Workflow,
) -> None:
    """A fast Enter after typed text sometimes leaves it unsent. The tick has pressed Escape
    by then; its own message must not stay in the box of the session it stopped."""
    _session(workflow)
    _runs(workflow, "ticket-7", "python -m pytest -q tests")
    _tick(workflow, _NOW)
    workflow.screen(_TEST_RUN_LATER, "ticket-7")
    # Escape stopped the run; both Enters of the nudge are lost.
    (workflow.state / "screen-after-escape-ticket-7").write_text(
        "● Bash(python -m pytest -q tests)\n"
        "  ⎿  Interrupted · What should Claude do instead?\n\n" + _BOX
    )
    (workflow.state / "swallow-enter-ticket-7").write_text("2")
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 2700)})
    assert "nudge failed" in "\n".join(_pending(workflow)), done.stderr
    assert _box(workflow) == _STILL

    _ticks(workflow, _NOW + 2700 + _TICK, _NOW + 4 * 2700, 900)

    assert _box(workflow) == "", "the tick's own nudge stays unsent in the box for 3 hours"


def test_an_owner_message_escape_put_back_reaches_the_session(
    workflow: Workflow,
) -> None:
    """The owner, on the phone, asks a session that has not moved what is going on; the
    message queues behind the silent command. The tick's Escape puts it back into the box."""
    _session(workflow)
    _runs(workflow, "ticket-7", "python -m pytest -q tests")
    _tick(workflow, _NOW)
    workflow.screen(_TEST_RUN_LATER, "ticket-7")
    (workflow.state / "input-after-escape-ticket-7").write_text("what is going on here?")
    (workflow.state / "screen-after-escape-ticket-7").write_text(
        "● Bash(python -m pytest -q tests)\n"
        "  ⎿  Interrupted · What should Claude do instead?\n\n" + _BOX
    )

    _ticks(workflow, _NOW + 2700, _NOW + 4 * 2700, 900)

    assert _keys(workflow)[0] == "Escape"
    assert _box(workflow) == "", (
        "the owner's message stays unsent in a box the owner cannot reach; the session stays stopped"
    )


# 4. Wait loops.


def test_a_multi_line_wait_loop_that_finds_itself_is_nudged_once(
    workflow: Workflow,
) -> None:
    loop = 'while pgrep -f "gh run watch" >/dev/null; do\n  sleep 30\ndone\necho done'
    screen = (
        '● Bash(while pgrep -f "gh run watch" >/dev/null; do…)\n'
        "  ⎿  Running… (2m 1s)\n\n"
        "✻ Cogitating… (2m 1s · esc to interrupt)\n\n" + _BOX
    )
    _session(workflow, screen=screen)
    _runs(workflow, "ticket-7", loop)
    (workflow.state / "screen-after-enter-ticket-7").write_text(_ANSWERED)

    _ticks(workflow, _NOW, _NOW + 6 * _TICK)

    assert _keys(workflow) == ["Escape", _LOOP, "Enter"]


def test_a_background_wait_loop_does_not_interrupt_work_that_runs_no_command(
    workflow: Workflow,
) -> None:
    """The session started the loop in the background and goes on editing files: its Bash tool
    runs nothing else, so the loop is the command it started last."""
    editing = (
        '● Bash(while pgrep -f "codex exec" >/dev/null; do sleep 30; done)\n'
        "  ⎿  Running in the background (down arrow to manage)\n\n"
        "● Update(scripts/example.py)\n  ⎿  Updated scripts/example.py with 3 additions\n\n"
        "✻ Cogitating… (40s · esc to interrupt)\n\n" + _BOX
    )
    _session(workflow, screen=editing)
    _runs(
        workflow,
        "ticket-7",
        'while pgrep -f "codex exec" >/dev/null; do sleep 30; done',
    )

    _tick(workflow, _NOW)

    assert "Escape" not in _keys(workflow)


# 5. Idle sessions no rule covers.


def test_an_idle_worker_blocked_on_another_pull_request_is_named_once_and_never_woken(
    workflow: Workflow,
) -> None:
    notes = _session(
        workflow,
        screen="● This waits for pull request 12 to merge first; nothing else is left here.\n"
        + _BOX,
    )
    _written(notes.with_name("ticket-7.idle"), _NOW - 2900)

    _ticks(workflow, _NOW, _NOW + _TICK)
    _ticks(workflow, _NOW + 1800, _NOW + 4 * 3600, 1800)

    assert _keys(workflow) == []
    assert _pending(workflow) == [
        (
            "idle, no rule: ticket-7 (51 min, last line: This waits for pull request 12 to merge "
            "first; nothing else is left here.)"
        )
    ]


def test_a_waiting_session_gets_the_status_check_only(workflow: Workflow) -> None:
    notes = _session(
        workflow,
        screen="● Waiting for the checks of pull request 12 in the background.\n" + _BOX,
    )
    _written(notes.with_name("ticket-7.job-wait"), _NOW)

    _ticks(workflow, _NOW, _NOW + 2 * 3600, 900)

    keys = _keys(workflow)
    assert "Escape" not in keys
    assert sum(key.startswith(_STATUS) for key in keys) == 1
    assert _pending(workflow) == []


def test_the_idle_line_names_the_last_words_of_the_reply(workflow: Workflow) -> None:
    """After a turn Claude draws a line of its own under the reply, with the turn's time."""
    notes = _session(
        workflow,
        screen="● I'll wait for the checks on pull request 7 to finish.\n\n"
        "✻ Worked for 3m 2s\n\n" + _BOX,
    )
    _written(notes.with_name("ticket-7.idle"), _NOW - 3000)

    _tick(workflow, _NOW)

    assert _pending(workflow) == [
        (
            "idle, no rule: ticket-7 (50 min, last line: "
            "I'll wait for the checks on pull request 7 to finish.)"
        )
    ]


# 6. The hand check.


def test_session_list_shows_when_the_screen_last_changed(workflow: Workflow) -> None:
    _session(workflow)
    _tick(workflow, _NOW)
    workflow.screen(_TEST_RUN_LATER, "ticket-7")
    _tick(workflow, _NOW + 1200)

    listed = workflow.run("list")

    assert listed.returncode == 0, listed.stderr
    assert "screen changed Sep 21, 2026, 2:13pm (UTC)" in listed.stdout
