"""Independent break tests for PR 84; run this file only, without xdist."""

import concurrent.futures
import fcntl
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time

import pytest
from workflow_harness import SCRIPTS, Workflow


CAPTURE = (Path(__file__).parent / "fixtures/screens/codex-status.ansi").read_text()
FOOTER = CAPTURE.splitlines()[-1]
HEAD = "a" * 40


def read_box(screen: str, trim: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; codex_input "$(cat)" "$2"',
         "bash", str(SCRIPTS / "input-box"), str(trim).lower()],
        input=screen, text=True, capture_output=True, timeout=5,
    )


def screen(draft: str = "\x1b[2mAsk Codex to do anything\x1b[0m", footer: str = FOOTER) -> str:
    return f"Review complete.\n\n› {draft}\n\n{footer}\n"


@pytest.mark.parametrize("footer", [FOOTER, "  ? for shortcuts", "", FOOTER + "\n\n"])
@pytest.mark.parametrize("draft,expected", [
    ("\x1b[2mAsk Codex to do anything\x1b[0m", ""),
    ("Keep this draft", " Keep this draft\n"),
    ("First · second\n\n  Last line", " First · second\n  Last line\n"),
    ("\x1b[2mSuggestion\x1b[0mTyped", " Typed\n"),
    ("First\n\n  custom-model · ~/example-app · Draft",
     " First\n  custom-model · ~/example-app · Draft\n"),
])
def test_daily_drafts_and_footer_compatibility(footer: str, draft: str, expected: str) -> None:
    done = read_box(screen(draft, footer))
    assert done.returncode == 0, done.stderr
    assert done.stdout == expected


@pytest.mark.parametrize("colour", ["32", "38;5;114", "38;2;17;41;85", "38:2::17:41:85"])
def test_separately_styled_footer_and_uniform_owner_row(colour: str) -> None:
    fields = ("custom-model high", "../example-app", "Review changes")
    footer = "  " + " · ".join(f"\x1b[{colour}m{x}\x1b[39m" for x in fields)
    done = read_box(screen(footer=footer))
    assert done.returncode == 0 and done.stdout == "", done.stdout
    owner = f"  \x1b[{colour}m" + " · ".join(fields) + "\x1b[39m"
    done = read_box(screen("First\n\n" + owner, ""))
    assert done.stdout == " First\n  " + " · ".join(fields) + "\n"


def test_legacy_opt_out_retains_the_native_status_text() -> None:
    done = read_box(screen("Keep my draft"), trim=False)
    assert done.returncode == 0, done.stderr
    assert "Keep my draft" in done.stdout
    assert "Implement pull request 12" in done.stdout
    assert "~/example-app/.claude/worktrees/ticket-12" in done.stdout


def prepare(workflow: Workflow, draft: str = "\x1b[2mAsk Codex to do anything\x1b[0m") -> Path:
    workflow.add_worktree(7, draft=False)
    for name in ("review-7", "codex-7"):
        workflow.running(name)
    notes = workflow.notes(7)
    notes.mkdir(parents=True)
    for marker in ("codex", "checks-wait", "bridge-stopped"):
        (notes / f"review-7.{marker}").touch()
    turn = '{"last-assistant-message":"Waiting for checks","turn-id":"one"}'
    for suffix in ("codex-turn.json", "codex-delivered"):
        (notes / f"review-7.{suffix}").write_text(turn)
    workflow.screen(screen(draft), "codex-7")
    rule = "─" * 40
    workflow.screen(f"Done.\n{rule}\n❯ \n{rule}\n", "review-7")
    return notes


@pytest.mark.parametrize("event", ["checks-finished", "lost-check", "conflict-found", "batch-merged"])
@pytest.mark.parametrize("typed", [False, True])
def test_real_wake_events_preserve_owner_drafts(workflow: Workflow, event: str, typed: bool) -> None:
    notes = prepare(workflow, "Keep my draft" if typed else "\x1b[2mAsk Codex to do anything\x1b[0m")
    done = workflow.run(event, "review-7", HEAD)
    calls = workflow.calls("tmux codex-7 send-keys")
    if typed:
        assert done.returncode != 0
        assert not calls
        assert (notes / "review-7.checks-wait").exists()
    else:
        assert done.returncode == 0, done.stderr
        assert len(calls) == 2 and calls[-1].endswith(" Enter"), calls
        assert not (notes / "review-7.checks-wait").exists()
    assert not workflow.calls("tmux review-7 send-keys")


def test_lock_held_across_repeated_ticks_is_retryable(workflow: Workflow) -> None:
    notes = prepare(workflow)
    lock = workflow.repo / ".local/workflow/start-7.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        for _ in range(2):
            done = workflow.run("checks-finished", "review-7", HEAD)
            assert done.returncode != 0
            assert "another action" in done.stderr
            assert (notes / "review-7.checks-wait").exists()
        assert not workflow.calls("tmux codex-7 send-keys")
    done = workflow.run("checks-finished", "review-7", HEAD)
    assert done.returncode == 0, done.stderr
    assert len(workflow.calls("tmux codex-7 send-keys")) == 2


def test_two_checks_finished_calls_submit_once(workflow: Workflow) -> None:
    prepare(workflow)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _: workflow.run("checks-finished", "review-7", HEAD), range(2)))
    assert sum(run.returncode == 0 for run in runs) == 1, [r.stderr for r in runs]
    assert len(workflow.calls("tmux codex-7 send-keys")) == 2


@pytest.mark.parametrize("missing", ["codex-delivered", "codex-turn.json", "bridge-stopped"])
def test_missing_handoff_files_do_not_authorize_typing(workflow: Workflow, missing: str) -> None:
    notes = prepare(workflow)
    (notes / f"review-7.{missing}").unlink()
    done = workflow.run("checks-finished", "review-7", HEAD)
    assert done.returncode != 0
    assert not workflow.calls("tmux codex-7 send-keys")
    assert (notes / "review-7.checks-wait").exists()


@pytest.mark.parametrize("failure", ["missing", "timeout"])
def test_capture_failure_preserves_wait_for_retry(workflow: Workflow, failure: str) -> None:
    notes = prepare(workflow)
    tmux = workflow.bin / "tmux"
    original = tmux.read_text()
    injected = '  capture-pane)\n    [ "$name" != codex-7 ] || exit ' + ("124" if failure == "timeout" else "1") + "\n"
    tmux.write_text(original.replace("  capture-pane)\n", injected))
    done = workflow.run("checks-finished", "review-7", HEAD)
    assert done.returncode != 0
    assert not workflow.calls("tmux codex-7 send-keys")
    assert (notes / "review-7.checks-wait").exists()
    tmux.write_text(original)
    done = workflow.run("checks-finished", "review-7", HEAD)
    assert done.returncode == 0, done.stderr


def test_partial_delivery_retries_only_unchanged_automation(workflow: Workflow) -> None:
    notes = prepare(workflow)
    swallow = workflow.state / "swallow-enter-codex-7"
    swallow.write_text("2")
    first = workflow.run("checks-finished", "review-7", HEAD)
    assert first.returncode != 0
    assert (notes / "review-7.checks-wait").exists()
    assert (notes / "codex-7.typed").exists()
    swallow.unlink()
    second = workflow.run("checks-finished", "review-7", HEAD)
    assert second.returncode == 0, second.stderr
    calls = workflow.calls("tmux codex-7 send-keys")
    assert sum(" -l " in call for call in calls) == 1
    assert calls[-1].endswith(" Enter")
    assert not (notes / "codex-7.typed").exists()


def test_edited_automation_draft_is_never_submitted_on_retry(workflow: Workflow) -> None:
    notes = prepare(workflow)
    (workflow.state / "swallow-enter-codex-7").write_text("2")
    first = workflow.run("checks-finished", "review-7", HEAD)
    assert first.returncode != 0
    assert (notes / "codex-7.typed").exists()
    before = workflow.calls("tmux codex-7 send-keys")
    (workflow.state / "input-codex-7").write_text("Owner changed the pending instruction")
    second = workflow.run("checks-finished", "review-7", HEAD)
    assert second.returncode != 0
    assert workflow.calls("tmux codex-7 send-keys") == before
    assert (notes / "review-7.checks-wait").exists()


@pytest.mark.parametrize("blocker", ["owner-reply", "owner-blocked", "interaction-blocked"])
def test_owner_wait_is_not_bypassed_by_an_empty_native_box(workflow: Workflow, blocker: str) -> None:
    notes = prepare(workflow)
    (notes / f"review-7.{blocker}").touch()
    done = workflow.run("checks-finished", "review-7", HEAD)
    assert done.returncode != 0
    assert not workflow.calls("tmux codex-7 send-keys")
    assert (notes / "review-7.checks-wait").exists()


def test_bridge_draft_also_blocks_a_native_wake(workflow: Workflow) -> None:
    notes = prepare(workflow)
    rule = "─" * 40
    workflow.screen(f"Done.\n{rule}\n❯ Keep this reply\n{rule}\n", "review-7")
    done = workflow.run("checks-finished", "review-7", HEAD)
    assert done.returncode != 0
    assert not workflow.calls("tmux codex-7 send-keys")
    assert (notes / "review-7.checks-wait").exists()


def test_repeated_notification_after_delivery_does_not_type_twice(workflow: Workflow) -> None:
    prepare(workflow)
    first = workflow.run("checks-finished", "review-7", HEAD)
    assert first.returncode == 0, first.stderr
    before = workflow.calls("tmux codex-7 send-keys")
    second = workflow.run("checks-finished", "review-7", HEAD)
    assert second.returncode != 0
    assert workflow.calls("tmux codex-7 send-keys") == before


def test_status_fields_are_data_not_shell(workflow: Workflow) -> None:
    prepare(workflow)
    footer = FOOTER.replace("Implement pull request 12", '$(touch BREAKER_INJECTED); `touch BREAKER_INJECTED`')
    workflow.screen(screen(footer=footer), "codex-7")
    done = workflow.run("checks-finished", "review-7", HEAD)
    assert done.returncode == 0, done.stderr
    assert not (workflow.repo / "BREAKER_INJECTED").exists()
    assert not any("BREAKER_INJECTED" in call for call in workflow.calls("tmux codex-7 send-keys"))


@pytest.mark.parametrize("width", [80, 160, 320])
@pytest.mark.parametrize("draft,expected", [
    (None, ""),
    ("Keep this draft", " Keep this draft\n"),
    ("First · second\n\n  Final line", " First · second\n  Final line\n"),
])
def test_actual_tmux_capture_round_trip(tmp_path: Path, width: int, draft: str | None, expected: str) -> None:
    """Use tmux's actual SGR serialization and soft-wrap joining, not its stand-in."""
    tmux = shutil.which("tmux")
    assert tmux, "tmux is needed for this terminal test"
    # Keep the socket inside the clone and below Unix's socket-path length limit.
    socket = Path.cwd() / ".local" / f"s84-{os.getpid()}-{width}"
    socket.parent.mkdir(exist_ok=True)
    config = tmp_path / "tmux.conf"
    config.write_text("set -g status off\n")
    capture_file = tmp_path / "screen.ansi"
    contents = CAPTURE
    if draft is not None:
        contents = contents.replace("\x1b[2mAsk Codex to do anything\x1b[0m", draft)
    capture_file.write_text(contents.replace("\n", "\r\n"))
    command = "cat " + shlex.quote(str(capture_file)) + "; exec sleep 30"
    base = [tmux, "-S", str(socket), "-f", str(config)]
    env = {**os.environ, "TERM": "xterm-256color", "LC_ALL": "C.UTF-8"}
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(base + list(args), text=True, capture_output=True, env=env, timeout=5)
    try:
        made = run("new-session", "-d", "-s", "example-app", "-x", str(width), "-y", "24", command)
        assert made.returncode == 0, made.stderr
        for _ in range(100):
            captured = run("capture-pane", "-p", "-e", "-J", "-t", "example-app:")
            assert captured.returncode == 0, captured.stderr
            if "f2" in captured.stdout:
                break
            time.sleep(0.01)
        assert "f2" in captured.stdout, "terminal did not render the screen"
        done = read_box(captured.stdout)
        assert done.returncode == 0, done.stderr
        assert done.stdout == expected, repr(done.stdout)
    finally:
        if socket.exists():
            run("kill-server")
