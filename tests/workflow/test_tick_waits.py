"""The tick keeps waits moving: it keeps a waiting session's cache alive, tells a
worker that is ready but not finished to finish, nudges a stuck session and names it in the
wake, and names an idle session that no rule covers."""

import json
import os
import shlex
import time
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
    workflow.notes(7).mkdir(parents=True, exist_ok=True)
    return workflow.notes(7) / name


def _waiting(workflow: Workflow, name: str = "ticket-7", since: int = _NOW - 3000) -> Path:
    """A session that ended its turn with `notify waiting` at `since`."""
    notes = _session(workflow, name)
    _written(notes.with_name(f"{name}.job-wait"), since)
    return notes


# 1. Cache keep-alive.


def _codex_waiting(workflow: Workflow, since: int = _NOW - 3000) -> Path:
    notes = _waiting(workflow, since=since)
    notes.with_suffix(".codex").touch()
    notes.with_suffix(".bridge-stopped").touch()
    notes.with_suffix(".codex-turn.json").write_text(json.dumps({
        "last-assistant-message": "Waiting for the background job.", "turn-id": "wait-1",
    }))
    workflow.running("codex-7")
    workflow.screen("• Waiting for the background job.\n\n› \n\n  ? for shortcuts\n", "codex-7")
    return notes


def test_codex_gets_one_native_status_per_wait_without_an_owner_answer(workflow: Workflow) -> None:
    notes = _codex_waiting(workflow, since=_NOW - 2999)
    _tick(workflow, _NOW)
    assert _typed(workflow, "codex-7", "Status") == []

    _tick(workflow, _NOW + 1)
    _tick(workflow, _NOW + 600)

    assert len(_typed(workflow, "codex-7", "Status")) == 1
    assert _keys(workflow, "codex-7") == [_STATUS, "Enter"]
    assert workflow.calls("tmux ticket-7 send-keys") == []
    assert not notes.with_suffix(".owner-reply").exists()
    assert not notes.with_suffix(".codex-answered").exists()
    assert notes.with_suffix(".codex-followed-up").read_bytes() == notes.with_suffix(".codex-turn.json").read_bytes()
    assert not notes.with_suffix(".job-wait").exists()

    # A later notify waiting stop begins a new stretch.
    _written(notes.with_suffix(".job-wait"), _NOW + 600)
    _tick(workflow, _NOW + 3599)
    assert len(_typed(workflow, "codex-7", "Status")) == 1
    _tick(workflow, _NOW + 3600)
    assert len(_typed(workflow, "codex-7", "Status")) == 2


@pytest.mark.parametrize("case", [
    "asking", "owner-blocked", "interaction-blocked", "owner-reply",
    "running", "native input", "bridge input", "permission", "limit", "question on screen",
])
def test_codex_status_leaves_questions_turns_and_input_alone(workflow: Workflow, case: str) -> None:
    notes = _codex_waiting(workflow)
    if case in ("asking", "owner-blocked", "interaction-blocked", "owner-reply"):
        notes.with_suffix(f".{case}").write_text("pending")
    elif case == "running":
        workflow.screen("• Working (1m • esc to interrupt)\n\n› \n\n  ? for shortcuts\n", "codex-7")
    elif case in ("native input", "bridge input"):
        target = "codex-7" if case == "native input" else "ticket-7"
        (workflow.state / f"input-{target}").write_text("Owner draft")
    elif case == "permission":
        workflow.screen("› \nPress enter to confirm or esc to cancel\n", "codex-7")
    elif case == "limit":
        workflow.screen("■ You've hit your usage limit.\n› \n", "codex-7")
    else:
        workflow.screen("● Should the limit be 3 or 5? Reply 1 or 2.\n" + _BOX, "ticket-7")

    _tick(workflow, _NOW)

    assert workflow.calls("tmux codex-7 send-keys") == []
    assert workflow.calls("tmux ticket-7 send-keys") == []
    assert notes.with_suffix(".job-wait").exists()
    assert not notes.with_suffix(".codex-answered").exists()


@pytest.mark.parametrize("running", [False, True])
def test_codex_wait_state_follows_the_native_turn(workflow: Workflow, running: bool) -> None:
    _codex_waiting(workflow)
    if running:
        workflow.screen("• Working (1m • esc to interrupt)\n\n› \n", "codex-7")
    listed = workflow.run("list")
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.startswith("ticket-7  " + ("busy" if running else "idle"))


def test_codex_status_recovery_is_bounded_and_does_not_answer(workflow: Workflow) -> None:
    notes = _codex_waiting(workflow)
    (workflow.state / "swallow-enter-codex-7").write_text("100")
    for n in range(6):
        workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 180 * n)})

    assert _keys(workflow, "codex-7") == [_STATUS] + ["Enter"] * 5
    assert notes.with_suffix(".job-wait").exists()
    assert not notes.with_suffix(".codex-answered").exists()
    assert _pending(workflow) == ["stuck? ticket-7 (status message not taken; its text stays in the box)"]


def test_codex_status_recovers_after_enter_was_not_taken(workflow: Workflow) -> None:
    notes = _codex_waiting(workflow)
    (workflow.state / "swallow-enter-codex-7").write_text("2")
    first = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})
    assert first.returncode != 0
    assert notes.with_suffix(".job-wait").exists()

    _tick(workflow, _NOW + 180)
    _tick(workflow, _NOW + 360)

    assert _keys(workflow, "codex-7") == [_STATUS, "Enter", "Enter", "Enter"]
    assert not notes.with_suffix(".job-wait").exists()
    assert not notes.with_suffix(".codex-answered").exists()
    assert not notes.with_suffix(".owner-reply").exists()


@pytest.mark.parametrize("exhausted", [False, True])
def test_codex_status_spanning_a_quota_reset_keeps_its_retry_budget(
    workflow: Workflow, exhausted: bool,
) -> None:
    notes = _codex_waiting(workflow)
    (workflow.notes(7) / "codex-7.typed").write_text(_STATUS)
    (workflow.state / "input-codex-7").write_text(_STATUS)
    if exhausted:
        (workflow.state / "swallow-enter-codex-7").write_text("100")
        for n in range(3):
            done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 180 * n)})
            assert done.returncode != 0
    workflow.screen(
        "■ You've hit your usage limit. Try again at 2026-10-15 16:00 UTC.\n› \n",
        "codex-7",
    )
    reset = 1_792_080_000
    before = ["Enter"] * 3 if exhausted else []
    _tick(workflow, reset - 60)
    assert _keys(workflow, "codex-7") == before

    _tick(workflow, reset)
    _tick(workflow, reset + 180)

    assert _keys(workflow, "codex-7") == (["Enter"] * 3 if exhausted else ["Enter"])
    assert (workflow.notes(7) / "codex-7.typed").exists() == exhausted
    assert not notes.with_suffix(".codex-answered").exists()
    assert not notes.with_suffix(".owner-reply").exists()
    if exhausted:
        assert _pending(workflow) == ["stuck? ticket-7 (status message not taken; its text stays in the box)"]


@pytest.mark.parametrize("change", ["native input", "bridge input", "asking", "running"])
def test_codex_status_rechecks_before_enter(workflow: Workflow, change: str) -> None:
    notes = _codex_waiting(workflow)
    if change in ("native input", "bridge input"):
        target = "codex-7" if change == "native input" else "ticket-7"
        action = f"printf '%s' 'Owner draft' > {shlex.quote(str(workflow.state / f'input-{target}'))}"
    elif change == "asking":
        action = f"touch {shlex.quote(str(notes.with_suffix('.asking')))}"
    else:
        screen = shlex.quote(str(workflow.state / "screen/codex-7"))
        action = f"printf '• Working (1m • esc to interrupt)\\n\\n› \\n' > {screen}"
    # Only change the state after the status has actually been pasted.
    typed = shlex.quote(str(workflow.state / "input-codex-7"))
    (workflow.state / "on-sleep").write_text(f"if [[ -s {typed} ]]; then {action}; fi\n")

    workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    assert _keys(workflow, "codex-7") == [_STATUS]
    assert notes.with_suffix(".job-wait").exists()
    assert not notes.with_suffix(".codex-answered").exists()


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


@pytest.mark.parametrize("name", ["ticket-7", "review-7"])
def test_own_status_without_wait_notes_is_sent_on_the_next_tick(
    workflow: Workflow, name: str
) -> None:
    notes = _session(workflow, name)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / f"input-{name}").write_text(_STATUS)

    _tick(workflow, _NOW)

    assert _keys(workflow, name) == ["Enter"]
    assert not notes.with_suffix(".typed").exists()
    assert _pending(workflow) == []


def _github_unavailable(workflow: Workflow, failure: str) -> Path:
    if failure == "budget-pause":
        budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget"
        budget.mkdir(parents=True, exist_ok=True)
        marker = budget / "budget.json"
        marker.write_text(json.dumps({
            "core": {"remaining": 499, "reset": int(time.time()) + 3600}
        }))
    else:
        marker = workflow.state / "fail-gh-list"
        marker.touch()
    return marker


@pytest.mark.parametrize("failure", ["budget-pause", "listing-failure"])
@pytest.mark.parametrize("name", ["ticket-7", "review-7"])
def test_status_recovery_needs_no_github(workflow: Workflow, failure: str, name: str) -> None:
    notes = _session(workflow, name)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / f"input-{name}").write_text(_STATUS)
    _github_unavailable(workflow, failure)

    for n in range(3):
        done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 180 * n)})
        assert done.returncode == (0 if failure == "budget-pause" else 1), done.stderr

    assert _keys(workflow, name) == ["Enter"]
    assert not notes.with_suffix(".typed").exists()


@pytest.mark.parametrize("failure", ["budget-pause", "listing-failure"])
@pytest.mark.parametrize("restore", [False, True])
def test_local_status_recovery_shares_its_bound_and_alert(
    workflow: Workflow, failure: str, restore: bool
) -> None:
    notes = _session(workflow, "review-7")
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-review-7").write_text(_STATUS)
    (workflow.state / "swallow-enter-review-7").write_text("100")
    marker = _github_unavailable(workflow, failure)

    for n in range(7):
        if restore and n == 2:
            marker.unlink()
        workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 180 * n)})
        assert _keys(workflow, "review-7") == ["Enter"] * min(n + 1, 3)

    line = "stuck? review-7 (status message not taken; its text stays in the box)"
    assert _pending(workflow) == [line]
    assert [row for row in workflow.wake_log.read_text().splitlines()
            if row.split(" ", 1)[-1] == line] == [f"{_NOW + 540} {line}"]


@pytest.mark.parametrize("failure", ["budget-pause", "listing-failure"])
@pytest.mark.parametrize("text", ["Owner draft", _STATUS + " more", _STATUS + "\n  Owner addition"])
def test_local_status_recovery_preserves_changed_input(
    workflow: Workflow, failure: str, text: str
) -> None:
    notes = _session(workflow, "review-7")
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-review-7").write_text(text)
    _github_unavailable(workflow, failure)

    for n in range(2):
        workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 180 * n)})

    assert _keys(workflow, "review-7") == []
    assert (workflow.state / "input-review-7").read_text() == text


@pytest.mark.parametrize("wait_note", [False, True])
def test_unsent_status_has_three_single_enter_retries_and_is_named_once(
    workflow: Workflow, wait_note: bool
) -> None:
    notes = _waiting(workflow) if wait_note else _session(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)
    (workflow.state / "swallow-enter-ticket-7").write_text("100")

    codes = [workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 180 * n)}).returncode
             for n in range(7)]

    assert codes == [1, 1, 1, 0, 0, 0, 0]
    assert _keys(workflow, "ticket-7") == ["Enter"] * 3
    line = "stuck? ticket-7 (status message not taken; its text stays in the box)"
    assert _pending(workflow) == [line]
    assert [row for row in workflow.wake_log.read_text().splitlines()
            if row.split(" ", 1)[-1] == line] == [f"{_NOW + 540} {line}"]


@pytest.mark.parametrize("text", ["Owner draft", _STATUS + " more", "A different tick message"])
def test_status_recovery_never_touches_changed_text(workflow: Workflow, text: str) -> None:
    notes = _waiting(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(text)

    _tick(workflow, _NOW)
    _tick(workflow, _NOW + 3600)

    assert _keys(workflow, "ticket-7") == []
    assert (workflow.state / "input-ticket-7").read_text() == text


def test_status_recovery_does_not_send_another_recorded_message(workflow: Workflow) -> None:
    notes = _waiting(workflow)
    notes.with_suffix(".typed").write_text("A different tick message")
    (workflow.state / "input-ticket-7").write_text("A different tick message")

    _tick(workflow, _NOW)

    assert _keys(workflow, "ticket-7") == []


def test_failed_status_is_recovered_even_when_submit_removed_the_wait_notes(
    workflow: Workflow,
) -> None:
    notes = _waiting(workflow)
    _tick(workflow, _NOW - 2700)
    (workflow.state / "swallow-enter-ticket-7").write_text("2")
    (workflow.state / "on-sleep").write_text(
        f"if [[ -e {shlex.quote(str(notes.with_suffix('.typed')))} ]]; then "
        f"rm -f {shlex.quote(str(notes.with_suffix('.job-wait')))}; fi\n"
    )

    first = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})
    assert first.returncode == 1
    assert not notes.with_suffix(".job-wait").exists()
    assert _pending(workflow) == []
    _tick(workflow, _NOW + 180)

    assert _keys(workflow, "ticket-7") == [_STATUS, "Enter", "Enter", "Enter"]
    assert not notes.with_suffix(".typed").exists()


def test_shadow_status_recovery_presses_nothing_and_keeps_retry_notes(
    workflow: Workflow,
) -> None:
    notes = _session(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)

    done = workflow.run_script("tick", "--shadow", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert _keys(workflow, "ticket-7") == []
    assert notes.with_suffix(".typed").read_text() == _STATUS
    assert not (workflow.repo / ".local/workflow/ticket-7.status-retry").exists()


def test_a_new_status_after_delivery_gets_a_fresh_retry_budget(workflow: Workflow) -> None:
    notes = _session(workflow)
    for at in (_NOW, _NOW + 180):
        notes.with_suffix(".typed").write_text(_STATUS)
        (workflow.state / "input-ticket-7").write_text(_STATUS)
        _tick(workflow, at)

    assert _keys(workflow, "ticket-7") == ["Enter", "Enter"]


def test_a_fast_answer_to_recovery_does_not_mark_its_new_wait_kept_alive(
    workflow: Workflow,
) -> None:
    notes = _waiting(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)
    waiting = shlex.quote(str(notes.with_suffix(".job-wait")))
    (workflow.state / "on-sleep").write_text(f"touch -d @{_NOW} {waiting}\n")

    _tick(workflow, _NOW)
    (workflow.state / "on-sleep").unlink()
    _tick(workflow, _NOW + 3000)

    assert _keys(workflow, "ticket-7") == ["Enter", _STATUS, "Enter"]


def _status_limit_screen(rules: int) -> str:
    box = _BOX if rules == 2 else ("─" * 40 + "\n" if rules else "") + "> \n"
    return "  ⎿  You've hit your session limit · resets 4:10pm (America/New_York)\n" + box


@pytest.mark.parametrize("rules", [0, 1, 2])
@pytest.mark.parametrize("exhausted", [False, True])
def test_a_usage_reset_keeps_the_same_status_retry_bound(
    workflow: Workflow, exhausted: bool, rules: int
) -> None:
    from test_tick_limit import _at

    now = int(_at(16, 10)["WORKFLOW_NOW"])
    notes = _session(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)
    (workflow.state / "swallow-enter-ticket-7").write_text("100")
    if exhausted:
        for n in range(3):
            done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(now - 1800 + 180 * n)})
            assert done.returncode == 1
    _written(notes.with_suffix(".idle"), now - 600)
    notes.with_suffix(".limit").write_text(f"{now}\n")
    workflow.screen(_status_limit_screen(rules), "ticket-7")
    _tick(workflow, now - 60)
    assert _keys(workflow, "ticket-7") == (["Enter"] * 3 if exhausted else [])

    codes = [workflow.run_script("tick", env={"WORKFLOW_NOW": str(now + 180 * n)}).returncode
             for n in range(7)]

    assert codes == ([0] * 7 if exhausted else [1, 1, 1, 0, 0, 0, 0])
    assert _keys(workflow, "ticket-7") == ["Enter"] * 3
    assert _pending(workflow) == [
        "stuck? ticket-7 (status message not taken; its text stays in the box)"
    ]


@pytest.mark.parametrize("rules", [0, 1, 2])
@pytest.mark.parametrize("shadow", [False, True])
def test_status_delivery_after_reset_is_single_and_shadow_presses_nothing(
    workflow: Workflow, shadow: bool, rules: int
) -> None:
    from test_tick_limit import _at

    notes = _session(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)
    now = int(_at(16, 10)["WORKFLOW_NOW"])
    _written(notes.with_suffix(".idle"), now - 600)
    notes.with_suffix(".limit").write_text(f"{now}\n")
    workflow.screen(_status_limit_screen(rules), "ticket-7")

    for minute in (10, 13):
        done = workflow.run_script("tick", *(["--shadow"] if shadow else []), env=_at(16, minute))
        assert done.returncode == 0, done.stderr

    assert _keys(workflow, "ticket-7") == ([] if shadow else ["Enter"])
    assert notes.with_suffix(".typed").exists() == shadow


@pytest.mark.parametrize("rules", [0, 1, 2])
@pytest.mark.parametrize("text", ["Owner draft", _STATUS + " more", _STATUS + "\n  Owner addition"])
def test_reset_status_recovery_keeps_owner_continuations(
    workflow: Workflow, rules: int, text: str
) -> None:
    from test_tick_limit import _at

    notes = _session(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    _written(notes.with_suffix(".idle"), _NOW)
    notes.with_suffix(".limit").write_text(_at(16, 10)["WORKFLOW_NOW"] + "\n")
    (workflow.state / "input-ticket-7").write_text(text)
    workflow.screen(_status_limit_screen(rules), "ticket-7")

    done = workflow.run_script("tick", env=_at(16, 10))

    assert done.returncode == 0, done.stderr
    assert _keys(workflow, "ticket-7") == []
    assert (workflow.state / "input-ticket-7").read_text() == text


@pytest.mark.parametrize("rules", [0, 1, 2])
def test_reset_status_recovery_keeps_unnoted_owner_questions(workflow: Workflow, rules: int) -> None:
    from test_tick_limit import _at

    notes = _session(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    _written(notes.with_suffix(".idle"), _NOW)
    notes.with_suffix(".limit").write_text(_at(16, 10)["WORKFLOW_NOW"] + "\n")
    (workflow.state / "input-ticket-7").write_text(_STATUS)
    workflow.screen("● Reply 1 or 2.\n" + _status_limit_screen(rules), "ticket-7")

    done = workflow.run_script("tick", env=_at(16, 10))

    assert done.returncode == 0, done.stderr
    assert _keys(workflow, "ticket-7") == []


def test_escape_restoring_own_status_is_recovered_on_the_next_tick(workflow: Workflow) -> None:
    _still(workflow)
    (workflow.notes(7) / "ticket-7.typed").write_text(_STATUS)
    (workflow.state / "input-after-escape-ticket-7").write_text(_STATUS)

    _tick(workflow, _NOW + 2700)
    assert _keys(workflow, "ticket-7") == ["Escape"]
    assert _pending(workflow) == []
    _tick(workflow, _NOW + 2880)

    assert _keys(workflow, "ticket-7") == ["Escape", "Enter"]
    assert _pending(workflow) == []


@pytest.mark.parametrize("case", ["asking", "owner-blocked", "interaction-blocked", "codex", "permission"])
def test_status_recovery_keeps_existing_safety_guards(workflow: Workflow, case: str) -> None:
    notes = _session(workflow)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)
    if case == "permission":
        workflow.screen("Do you want to run this command?\n" + _BOX, "ticket-7")
    else:
        notes.with_suffix(f".{case}").touch()

    _tick(workflow, _NOW)

    assert _keys(workflow, "ticket-7") == []


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


def test_a_checks_wake_cannot_bypass_the_status_retry_path(workflow: Workflow) -> None:
    notes = _session(workflow, "review-7", draft=False)
    _written(notes.with_suffix(".started"), _NOW - 7200)
    _written(notes.with_suffix(".checks-wait"), _NOW - 3000)
    notes.with_suffix(".typed").write_text(_STATUS)
    (workflow.state / "input-review-7").write_text(_STATUS)
    (workflow.state / "swallow-enter-review-7").write_text("100")
    (workflow.state / "rollup-7.json").write_text(json.dumps([
        {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
        for name in ("CI result", "Docker images: what to run")
    ]))

    for n in range(6):
        workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 180 * n)})

    assert _keys(workflow, "review-7") == ["Enter"] * 3
    assert not list((workflow.repo / ".local/workflow").glob("review-7.checks-nudged-*"))
    assert _pending(workflow) == [
        "stuck? review-7 (status message not taken; its text stays in the box)"
    ]

    # The owner submits it by hand after the bound. A stale .typed note must not
    # block the checks wake once the input is empty.
    (workflow.state / "input-review-7").write_text("")
    (workflow.state / "swallow-enter-review-7").unlink()
    _tick(workflow, _NOW + 1080)

    assert len(_typed(workflow, "review-7", "The checks on ")) == 1
    assert len(list((workflow.repo / ".local/workflow").glob("review-7.checks-nudged-*"))) == 1


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
    _tick(workflow, _NOW + 2880)

    assert _keys(workflow, "ticket-7") == ["Escape"]
    assert (workflow.state / "input-ticket-7").read_text() == "and then the docs"
    assert _pending(workflow) == [
        "stuck? ticket-7 (45 min, running: pytest tests); interrupted, text left in its box"
    ]


@pytest.mark.parametrize("queued", ["queued", "no-break space", "plain spinner frame", "none"])
def test_escape_is_never_pressed_on_a_message_queued_under_the_spinner(
    workflow: Workflow, queued: str
) -> None:
    screen = _captured("busy-queued")
    if queued == "no-break space":
        screen = screen.replace("❯ what is", "❯\u00a0what is")
    elif queued == "plain spinner frame":
        screen = screen.replace("✶ Pondering", "* Pondering")
    elif queued == "none":
        # Only the prompt already sent, in the history above the reply, has the mark.
        screen = screen.replace("\x1b[48;5;237m❯ what is going on here?\x1b[0m\n", "")
    _session(workflow)
    _runs(workflow, "ticket-7", "pytest tests")
    workflow.screen(screen, "ticket-7")
    _tick(workflow, _NOW)

    _tick(workflow, _NOW + 2700)

    if queued != "none":
        assert _keys(workflow, "ticket-7") == []
        assert _pending(workflow) == ["stuck? ticket-7 (45 min, running: pytest tests)"]
    else:
        assert _keys(workflow, "ticket-7") == ["Escape", _STILL, "Enter"]


def test_a_nudge_whose_enter_was_not_taken_is_sent_again_without_escape(
    workflow: Workflow,
) -> None:
    _still(workflow)
    (workflow.state / "screen-after-escape-ticket-7").write_text(_captured("interrupted"))
    (workflow.state / "swallow-enter-ticket-7").write_text("2")
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 2700)})
    assert done.returncode != 0
    assert (workflow.state / "input-ticket-7").read_text() == _STILL

    _tick(workflow, _NOW + 2880)
    _tick(workflow, _NOW + 3060)

    assert _keys(workflow, "ticket-7") == ["Escape", _STILL, "Enter", "Enter", "Enter"]
    assert (workflow.state / "input-ticket-7").read_text() == ""
    assert "ticket-7 still nudge sent again after its Enter was not taken" in (
        workflow.wake_log.read_text()
    )


def test_a_nudge_is_sent_again_under_a_history_prompt_of_an_earlier_turn(
    workflow: Workflow,
) -> None:
    _still(workflow)
    # Interrupted: no spinner now, an earlier turn's time and the prompt sent after it above.
    (workflow.state / "screen-after-escape-ticket-7").write_text(
        "● Done with the first part.\n\n✻ Worked for 3m 2s\n\n❯ run the tests and fix what fails\n\n"
        + _captured("interrupted")
    )
    (workflow.state / "swallow-enter-ticket-7").write_text("2")
    workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 2700)})

    _tick(workflow, _NOW + 2880)

    assert (workflow.state / "input-ticket-7").read_text() == ""
    assert _keys(workflow, "ticket-7").count("Escape") == 1


def test_a_nudge_that_is_never_taken_is_tried_three_more_times_then_named(
    workflow: Workflow,
) -> None:
    _still(workflow)
    (workflow.state / "screen-after-escape-ticket-7").write_text(_captured("interrupted"))
    (workflow.state / "swallow-enter-ticket-7").write_text("100")
    codes = [
        workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW + 2700 + 180 * n)}).returncode
        for n in range(7)
    ]

    # The first try and three more fail; then the wake is told and the tick is quiet again.
    assert codes == [1, 1, 1, 1, 0, 0, 0]
    assert _keys(workflow, "ticket-7").count("Escape") == 1
    assert _keys(workflow, "ticket-7").count("Enter") == 8
    assert "stuck? ticket-7 (nudge not taken; its text stays in the box)" in _pending(workflow)


def test_a_status_left_in_the_box_is_sent_before_a_nudge(
    workflow: Workflow,
) -> None:
    _still(workflow)
    # An older message of the tick's own that did not go.
    typed = workflow.notes(7) / "ticket-7.typed"
    _written(typed, _NOW)
    typed.write_text(_STATUS)
    (workflow.state / "input-ticket-7").write_text(_STATUS)

    _tick(workflow, _NOW + 2700)

    assert _keys(workflow, "ticket-7") == ["Enter"]
    assert _pending(workflow) == []


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


@pytest.mark.parametrize("name", ["ticket-7", "review-7", "audit-7"])
def test_a_session_stopped_and_cleaned_up_is_skipped_even_in_a_stale_list(
    workflow: Workflow, name: str
) -> None:
    worktree = workflow.add_worktree(7, state="MERGED", draft=False)
    workflow.running(name)
    notes = workflow.notes(7)
    notes.mkdir(parents=True)
    (notes / f"{name}.idle").touch()
    words = {"ticket-7": "worker", "review-7": "review", "audit-7": "audit"}[name]
    workflow.screen(f"● All done.\n\n{words} finished\n" + _BOX, name)
    # Keep the observation from before stop/cleanup, as an in-memory caller can do.
    real = workflow.script.with_name("session.real")
    workflow.script.rename(real)
    workflow.script.write_text(
        '#!/bin/bash\n'
        'if [[ $1 == list ]]; then\n'
        f'  cached={shlex.quote(str(workflow.state / "cached-sessions"))}\n'
        f'  [[ -f $cached ]] || bash {shlex.quote(str(real))} list >"$cached"\n'
        '  cat "$cached"\n'
        'else\n'
        f'  exec bash {shlex.quote(str(real))} "$@"\n'
        'fi\n'
    )

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running(name)
    assert not worktree.exists()
    assert not (workflow.repo / ".local/workflow" / f"{name}.screen-seen").exists()
    assert workflow.calls(f"tmux {name} send-keys") == []


def test_notes_removed_during_a_screen_check_are_not_recreated_by_later_loops(
    workflow: Workflow,
) -> None:
    _session(workflow, draft=False)
    folder = workflow.repo / ".local/workflow"
    workflow.fake(
        "cksum",
        f'#!/bin/bash\nrm -rf -- {shlex.quote(str(folder))}\nexec /usr/bin/cksum "$@"\n',
    )

    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})

    assert done.returncode == 0, done.stderr
    assert "No such file or directory" not in done.stderr
    assert not list(folder.glob("ticket-7.*"))
    assert workflow.calls("tmux ticket-7 send-keys") == []


def test_a_state_folder_that_exists_but_cannot_be_written_still_fails_the_tick(
    workflow: Workflow,
) -> None:
    _session(workflow)
    folder = workflow.repo / ".local/workflow"
    workflow.fake(
        "cksum",
        f'#!/bin/bash\nchmod 555 -- {shlex.quote(str(folder))}\nexec /usr/bin/cksum "$@"\n',
    )

    try:
        done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(_NOW)})
    finally:
        folder.chmod(0o755)

    assert done.returncode == 1
    assert "ticket-7.screen-seen: Permission denied" in done.stderr
    assert not (folder / "ticket-7.screen-seen").exists()


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
    _tick(workflow, _NOW + 180)
    # Not before its screen has stood still for 5 minutes.
    assert _keys(workflow, "ticket-7") == []
    _tick(workflow, _NOW + 300)
    # Interrupted, the session drew its answer; the loop is still the one that ran.
    workflow.screen(_captured("interrupted"), "ticket-7")
    _tick(workflow, _NOW + 480)

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


def test_a_wait_loop_on_a_screen_that_moves_is_named_when_the_turn_ends(
    workflow: Workflow,
) -> None:
    notes = _session(workflow)
    _runs(workflow, "ticket-7", 'while pgrep -f "codex exec" >/dev/null; do sleep 30; done')
    for at, step in ((_NOW, "one"), (_NOW + 180, "two"), (_NOW + 360, "three")):
        # The session edits files while the loop runs in the background.
        workflow.screen(f"● Update(scripts/{step}.py)\n  ⎿  Updated\n" + _BOX, "ticket-7")
        _tick(workflow, at)
    assert _pending(workflow) == []
    _written(notes.with_name("ticket-7.idle"), _NOW + 400)

    _tick(workflow, _NOW + 540)

    assert _keys(workflow, "ticket-7") == []
    assert _pending(workflow) == [
        'wait loop finds itself? ticket-7 (running: while pgrep -f "codex exec" >/dev/null; '
        "do sleep 30; done)"
    ]


def test_a_wait_loop_on_a_screen_that_keeps_moving_is_named_after_15_minutes(
    workflow: Workflow,
) -> None:
    _session(workflow)
    _runs(workflow, "ticket-7", 'while pgrep -f "codex exec" >/dev/null; do sleep 30; done')
    for n in range(7):
        workflow.screen(f"● Bash(while pgrep …)\n  ⎿  +{n} lines\n" + _BOX, "ticket-7")
        _tick(workflow, _NOW + 180 * n)
        assert _pending(workflow) == [] or n * 180 >= 900

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


def test_the_idle_line_leaves_out_the_lines_claude_draws_after_a_turn(
    workflow: Workflow,
) -> None:
    _idle(workflow, _NOW - 3000)
    workflow.screen(
        "● The pull request stays a draft for now.\n\n✻ Worked for 3m 2s\n\n" + _BOX, "ticket-7"
    )

    _tick(workflow, _NOW)

    assert _pending(workflow) == [
        "idle, no rule: ticket-7 (50 min, last line: The pull request stays a draft for now.)"
    ]


@pytest.mark.parametrize("words", ["review finished", "review sent back", "worker finished"])
@pytest.mark.parametrize(("left", "right"), [("", "│"), ("│ ", " │"), ("╭ │ ", " │ ╯")])
def test_the_idle_last_line_shows_words_inside_screen_borders(
    workflow: Workflow, words: str, left: str, right: str
) -> None:
    _idle(workflow, _NOW - 3000)
    message = f"The session has not said {words} yet."
    screen = f"● {message}\n\n✻ Worked for 3m 2s\n\n"
    screen = "\n".join(f"{left}{line:<60}{right}" for line in screen.splitlines()) + "\n"
    workflow.screen(screen + _BOX, "ticket-7")

    _tick(workflow, _NOW)

    assert _pending(workflow) == [f"idle, no rule: ticket-7 (50 min, last line: {message})"]


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
