"""Breaking tests for one cloud choice in the tick (pull request 28).

Each replays a daily use of the start loop: the default program switched during the day,
sent-back tickets kept on their program by their fix-round markers, starts held for memory
for many ticks, and the cloud's usage window.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from workflow_harness import Workflow

_NOW = 1792432800
_HELD = "some avg10=99.00 avg60=99.00 avg300=99.00 total=0\n"
_BACKEND = "diff --git a/backend/tool.py b/backend/tool.py\n@@ -1 +1 @@\n-old\n+new\n"
_NO_PROGRAM = "worker-program-8 names no worker program (cloud, claude or codex); read as no record"


def _env(
    workflow: Workflow,
    default: str,
    percent: int = 20,
    resets: int = 7200,
    now: int = _NOW,
):
    workflow.usage(percent, datetime.fromtimestamp(now + resets, UTC).isoformat())
    return {"WORKFLOW_NOW": str(now), "TZ": "UTC", "WORKFLOW_WORKER_PROGRAM": default}


def _notes(workflow: Workflow) -> Path:
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True, exist_ok=True)
    return notes


def _held_ahead(workflow: Workflow) -> None:
    """Ticket 7 is a screen change: local Claude only, and held for memory."""
    workflow.add_pull_request(7)
    (workflow.state / "body-7").write_text("Change the screen layout")
    workflow.pressure_file.write_text(_HELD)


def _asked_about_8(workflow: Workflow) -> list[str]:
    return [call for call in workflow.calls("gh pr") if re.search(r"\b8\b", call)]


def _nothing_started_for_8(workflow: Workflow) -> None:
    assert not workflow.calls("tmux cloud-8 new-session")
    assert not workflow.is_running("ticket-8")
    assert not workflow.is_running("codex-8")


# The owner switches the machine-wide program during the day. A sent-back ticket keeps its
# program by its marker, without a record, whether or not a start ahead of it waits for memory.


@pytest.mark.parametrize("default", ["claude", "codex", "cloud"])
def test_a_cloud_built_fix_round_passes_a_held_start_under_any_default(
    workflow: Workflow, default: str
) -> None:
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    (_notes(workflow) / "cloud-built-8").touch()

    done = workflow.run_script("tick", env=_env(workflow, default))

    assert not workflow.is_running("ticket-7")
    assert len(workflow.calls("tmux cloud-8 new-session")) == 1, done.stderr
    assert not workflow.is_running("ticket-8")
    assert not workflow.is_running("codex-8")


def test_a_codex_fix_round_under_the_cloud_default_waits_asking_github_nothing(
    workflow: Workflow,
) -> None:
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    (_notes(workflow) / "codex-built-8").touch()

    workflow.run_script("tick", env=_env(workflow, "cloud"))

    assert not _asked_about_8(workflow)
    _nothing_started_for_8(workflow)


def test_a_switch_mid_hold_never_moves_a_fix_round_to_another_program(
    workflow: Workflow,
) -> None:
    """Held for memory over many ticks while the default goes cloud, claude, codex, cloud."""
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    (_notes(workflow) / "codex-built-8").touch()

    for turn, default in enumerate(["cloud", "claude", "codex", "cloud"]):
        workflow.run_script("tick", env=_env(workflow, default, now=_NOW + turn * 180))

    _nothing_started_for_8(workflow)
    # Memory frees: the fix round goes to Codex, under the cloud default still.
    workflow.pressure_file.write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    (workflow.state / "diff-8").write_text(_BACKEND)
    workflow.run_script("tick", env=_env(workflow, "cloud", now=_NOW + 900))
    assert workflow.is_running("codex-8")
    assert not workflow.calls("tmux cloud-8 new-session")


# A ticket's own record holds across a switch of the default.


@pytest.mark.parametrize("default", ["claude", "codex", "cloud"])
def test_a_ticket_recorded_codex_with_its_marker_never_goes_to_the_cloud_when_held(
    workflow: Workflow, default: str
) -> None:
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    notes = _notes(workflow)
    (notes / "codex-built-8").touch()
    (notes / "worker-program-8").write_text("codex\n")

    workflow.run_script("tick", env=_env(workflow, default))

    assert not _asked_about_8(workflow)
    _nothing_started_for_8(workflow)


def test_a_record_written_with_windows_line_ends_is_still_read(
    workflow: Workflow,
) -> None:
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    (_notes(workflow) / "worker-program-8").write_text("cloud\r\n")

    done = workflow.run_script("tick", env=_env(workflow, "claude"))

    assert len(workflow.calls("tmux cloud-8 new-session")) == 1, done.stderr
    assert not workflow.is_running("ticket-8")


def test_a_junk_record_falls_back_to_the_marker_and_is_named_once(
    workflow: Workflow,
) -> None:
    """A record that names nothing must not lose a cloud fix round to local Claude."""
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    notes = _notes(workflow)
    (notes / "cloud-built-8").touch()
    (notes / "worker-program-8").write_text("Cloud\n")

    done = workflow.run_script("tick", env=_env(workflow, "claude"))
    again = workflow.run_script("tick", env=_env(workflow, "claude", now=_NOW + 180))

    assert len(workflow.calls("tmux cloud-8 new-session")) == 1, (
        done.stderr + again.stderr
    )
    assert not workflow.is_running("ticket-8")
    named = sorted(notes.glob("stuck.*/worker-program-8-*.json"))
    assert [json.loads(note.read_text())["line"] for note in named] == [_NO_PROGRAM]
    log = workflow.wake_log.read_text() if workflow.wake_log.exists() else ""
    assert (
        sum(
            line.endswith(_NO_PROGRAM) and "could not" not in line
            for line in log.splitlines()
        )
        >= 1
    )


def test_a_record_that_is_a_folder_does_not_stop_the_tick(workflow: Workflow) -> None:
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    (_notes(workflow) / "worker-program-8").mkdir()

    done = workflow.run_script("tick", env=_env(workflow, "cloud"))

    assert len(workflow.calls("tmux cloud-8 new-session")) == 1, done.stderr
    assert "Is a directory" not in done.stderr


# The usage window gates cloud starts, held memory or not: none above 85% unless the window
# resets within the hour, and never a fall back to a local start.


def test_a_recorded_cloud_ticket_above_the_usage_ceiling_waits_and_never_goes_local(
    workflow: Workflow,
) -> None:
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    (_notes(workflow) / "worker-program-8").write_text("cloud\n")
    (workflow.state / "diff-8").write_text(_BACKEND)

    for turn in range(3):
        workflow.run_script(
            "tick",
            env=_env(
                workflow, "claude", percent=90, resets=7200, now=_NOW + turn * 180
            ),
        )
    _nothing_started_for_8(workflow)
    # Memory frees while the window is still full: still no local start of a cloud ticket.
    workflow.pressure_file.write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    workflow.run_script(
        "tick", env=_env(workflow, "claude", percent=90, now=_NOW + 900)
    )
    _nothing_started_for_8(workflow)


def test_a_recorded_cloud_ticket_starts_when_the_full_window_resets_within_the_hour(
    workflow: Workflow,
) -> None:
    _held_ahead(workflow)
    workflow.add_pull_request(8)
    (_notes(workflow) / "worker-program-8").write_text("cloud\n")

    done = workflow.run_script(
        "tick", env=_env(workflow, "claude", percent=95, resets=1800)
    )

    assert len(workflow.calls("tmux cloud-8 new-session")) == 1, done.stderr
    assert not workflow.is_running("ticket-8")


# Two cloud tickets behind one held start: at most one tick's worth of starts each, no second
# session for the same ticket on the next pass.


def test_two_recorded_cloud_tickets_behind_a_held_start_each_start_once(
    workflow: Workflow,
) -> None:
    _held_ahead(workflow)
    for number in (8, 9):
        workflow.add_pull_request(number)
        (_notes(workflow) / f"worker-program-{number}").write_text("cloud\n")

    for turn in range(3):
        workflow.run_script("tick", env=_env(workflow, "codex", now=_NOW + turn * 180))

    assert len(workflow.calls("tmux cloud-8 new-session")) == 1
    assert len(workflow.calls("tmux cloud-9 new-session")) == 1
    assert not workflow.is_running("ticket-7")


def test_two_ticks_at_once_start_a_recorded_cloud_ticket_once(
    workflow: Workflow,
) -> None:
    """Two ticks fired together, a start held for memory: one cloud session, not two."""
    from concurrent.futures import ThreadPoolExecutor

    _held_ahead(workflow)
    workflow.add_pull_request(8)
    (_notes(workflow) / "worker-program-8").write_text("cloud\n")
    env = _env(workflow, "claude")

    with ThreadPoolExecutor(2) as pool:
        runs = list(pool.map(lambda _: workflow.run_script("tick", env=env), range(2)))

    assert len(workflow.calls("tmux cloud-8 new-session")) == 1, [
        run.stderr for run in runs
    ]
    assert not workflow.is_running("ticket-8")
