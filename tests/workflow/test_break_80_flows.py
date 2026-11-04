"""Independent PR 80 tests: actual start gates, wake, and limit recovery."""

import os
from datetime import UTC, datetime

import pytest
from workflow.break_80_support import NOW, answer, candidate, dated, labelled, tick, unused
from workflow_harness import Workflow


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("reader", ["ticket", "cloud", "analyst"])
@pytest.mark.parametrize("state", ["unused", "missing", "non-numeric", "ceiling", "held", "soon"])
def test_start_gates_agree_on_room_and_keep_existing_thresholds(
    workflow: Workflow, source: str, reader: str, state: str,
) -> None:
    payload = unused()
    if state == "missing":
        payload.pop("five_hour")
    elif state == "non-numeric":
        payload["five_hour"] = {"utilization": "0", "resets_at": None}
    elif state in {"ceiling", "held", "soon"}:
        payload["five_hour"] = {
            "utilization": 85.0 if state == "ceiling" else 86.0,
            "resets_at": dated(NOW + (3600 if state == "soon" else 3601)),
        }
    answer(workflow, payload, source)
    extra = {}
    if reader == "ticket":
        candidate(workflow)
    elif reader == "cloud":
        workflow.add_pull_request(7)
        extra["WORKFLOW_WORKER_PROGRAM"] = "cloud"
    else:
        report = workflow.repo / ".local/analyses/metrics-2026-11-04.md"
        report.parent.mkdir(parents=True)
        report.write_text("# Workflow metrics\n")
        extra["WORKFLOW_ANALYST_DAILY_AT"] = "00:00"

    done = tick(workflow, **extra)

    assert done.returncode == 0, done.stdout + done.stderr
    starts = state in {"unused", "ceiling", "soon"}
    if reader == "ticket":
        assert labelled(workflow) == (["7"] if starts else [])
    else:
        prefix = "tmux cloud-7 new-session" if reader == "cloud" else "tmux analyst-day-2026-11-04 new-session"
        assert bool(workflow.calls(prefix)) == starts, done.stdout
    if state in {"missing", "non-numeric"}:
        assert "5-hour usage window could not be read" in done.stdout


@pytest.mark.parametrize("source", ["cache", "live"])
def test_ticket_selected_after_reset_launches_once_on_later_ticks(workflow: Workflow, source: str) -> None:
    answer(workflow, unused(), source)
    candidate(workflow)
    for moment in (NOW, NOW + 180, NOW + 360):
        done = tick(workflow, moment)
        assert done.returncode == 0, done.stdout + done.stderr
    assert labelled(workflow) == ["7"]
    assert len(workflow.calls("tmux ticket-7 new-session")) == 1


@pytest.mark.parametrize("source", ["cache", "live"])
def test_night_check_in_describes_unused_then_active_window(workflow: Workflow, source: str) -> None:
    workflow.orchestrator("idle")
    payload = unused()
    answer(workflow, payload, source)
    first = workflow.run_script("wake", env={"TZ": "UTC"})
    assert first.returncode == 0, first.stderr
    assert "5-hour window 0% used, not started" in workflow.wake_log.read_text()
    assert "starts: allowed" in workflow.wake_log.read_text()
    assert "5-hour window 0% used, not started" in workflow.sent()[0]

    payload["five_hour"] = {"utilization": 2.0, "resets_at": dated(NOW + 18000)}
    cache = answer(workflow, payload, source)
    if source == "cache":
        os.utime(cache, (NOW + 1200, NOW + 1200))
    later = workflow.run_script("wake", "ticket-7", env={"WORKFLOW_NOW": str(NOW + 1200)})
    assert later.returncode == 0, later.stderr
    assert "5-hour window 2% used, resets in 4h40m" in workflow.sent()[-1]


@pytest.mark.parametrize("source", ["cache", "live"])
@pytest.mark.parametrize("message", [
    "The usage limit was reached, so I am stopping here.",
    "You've hit your weekly limit · resets 2am (UTC)",
])
def test_unused_five_hour_window_keeps_full_weekly_reset(
    workflow: Workflow, source: str, message: str,
) -> None:
    workflow.add_worktree(7)
    workflow.notes(7).mkdir(parents=True)
    reset = NOW + 10800
    assert datetime.fromtimestamp(reset, UTC).hour == 2
    payload = unused()
    payload["seven_day"] = {"utilization": 100.0, "resets_at": dated(reset)}
    answer(workflow, payload, source)

    done = workflow.notify({"hook_event_name": "Stop", "last_assistant_message": message})

    assert done.returncode == 0, done.stderr
    assert int((workflow.notes(7) / "ticket-7.limit").read_text()) == reset + 60
    assert workflow.pushes() == []


@pytest.mark.parametrize("source", ["cache", "live"])
def test_new_unused_window_does_not_park_an_ordinary_stop_for_five_hours(
    workflow: Workflow, source: str,
) -> None:
    workflow.add_worktree(7)
    workflow.notes(7).mkdir(parents=True)
    answer(workflow, unused(), source)
    done = workflow.notify({
        "hook_event_name": "Stop",
        "last_assistant_message": "The usage limit was reached, so I am stopping here.",
    })
    assert done.returncode == 0, done.stderr
    assert not (workflow.notes(7) / "ticket-7.limit").exists()


def test_saved_limit_reset_is_retried_once_after_window_becomes_unused(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    folder = workflow.notes(7)
    folder.mkdir(parents=True)
    (folder / "ticket-7.limit").write_text(f"{NOW - 60}\n")
    (folder / "ticket-7.idle").touch()
    workflow.running("ticket-7")
    answer(workflow, unused())
    for moment in (NOW, NOW + 180):
        done = tick(workflow, moment)
        assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux ticket-7 send-keys -t =ticket-7: -l ")) == 1
