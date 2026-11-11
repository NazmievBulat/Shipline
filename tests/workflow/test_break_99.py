"""Independent breaker cases for PR 99; external services are stand-ins."""

import fcntl
import json
import os
import re
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from workflow_harness import Workflow


NOON = int(datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp())
FOLDER = ".local/workflow"


def wake(flow: Workflow, seconds: int = 0, *args: str):
    return flow.run_script(
        "wake", *args, env={"WORKFLOW_NOW": str(NOON + seconds), "TZ": "UTC"},
    )


def mark_idle(flow: Workflow, number: int, when: int = NOON - 7200, folder: str = FOLDER):
    notes = flow.worktree(number) / folder
    notes.mkdir(parents=True, exist_ok=True)
    idle = notes / f"ticket-{number}.idle"
    idle.touch()
    os.utime(idle, ns=(when * 10**9, when * 10**9))
    return idle


def write_waits(flow: Workflow, values=None, folder: str = FOLDER):
    if values is None:
        values = {str(n): f"{n} waits: shares src/example-app.py with 7" for n in (9, 12)}
    target = flow.repo / folder / "start-status.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(values))
    return target


@pytest.fixture
def held(workflow: Workflow):
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.orchestrator("busy", name="example-app")
    workflow.usage(30, "2026-10-14T05:00:00+00:00", seven=20)
    mark_idle(workflow, 7)
    write_waits(workflow)
    return workflow


def test_multiple_holders_and_numeric_waiter_order(held: Workflow):
    held.add_worktree(8)
    held.running("ticket-8")
    mark_idle(held, 8, NOON - 10800)
    write_waits(held, {
        "21": "21 waits: shares src/example-app.py with 8",
        "9": "9 waits: shares src/example-app.py with 7",
        "12": "12 waits: shares src/example-app.py with 7",
        "18": "18 waits: shares src/example-app.py with 8",
    })
    assert wake(held).returncode == 0
    [message] = held.sent()
    first = "idle and holding: ticket-7 (120 min) holds 9, 12"
    second = "idle and holding: ticket-8 (180 min) holds 18, 21"
    assert first in message and second in message
    assert message.index(first) < message.index(second)
    for elapsed in (360, 1800, 3600):
        assert wake(held, elapsed).returncode == 0
    assert len(held.sent()) == 1


def test_threshold_overrides_a_recent_look(held: Workflow):
    mark_idle(held, 7, NOON - 3600)
    looked = held.wake_log.parent / "wake.looked"
    looked.parent.mkdir(parents=True, exist_ok=True)
    looked.write_text(str(NOON - 1))
    assert wake(held).returncode == 0
    assert "ticket-7 (60 min) holds 9, 12" in held.sent()[0]


def test_work_between_wakes_rearms_only_that_holder(held: Workflow):
    assert wake(held).returncode == 0
    assert held.notify({"hook_event_name": "UserPromptSubmit"}).returncode == 0
    assert not (held.notes(7) / "ticket-7.idle").exists()
    assert held.notify({
        "hook_event_name": "Stop", "last_assistant_message": "Example task paused.",
    }).returncode == 0
    mark_idle(held, 7, NOON + 60)
    assert wake(held, 3659).returncode == 0
    assert len(held.sent()) == 1
    assert wake(held, 3660).returncode == 0
    assert len(held.sent()) == 2


@pytest.mark.parametrize("marker", ["waiting", "job-wait", "checks-wait"])
def test_background_wait_stays_quiet_then_rearms(held: Workflow, marker: str):
    note = held.notes(7) / f"ticket-7.{marker}"
    note.touch()
    assert wake(held).returncode == 0
    assert held.sent() == []
    note.unlink()
    assert wake(held, 360).returncode == 0
    assert "idle and holding:" in held.sent()[0]


@pytest.mark.parametrize("case", ["missing", "truncated", "empty", "no-idle"])
def test_incomplete_state_does_not_consume_the_alert(held: Workflow, case: str):
    target = held.repo / FOLDER / "start-status.json"
    if case == "missing":
        target.unlink()
    elif case == "truncated":
        target.write_text('{"9":')
    elif case == "empty":
        target.write_text("{}")
    else:
        (held.notes(7) / "ticket-7.idle").unlink()
    assert wake(held).returncode == 0
    assert held.sent() == []
    write_waits(held)
    mark_idle(held, 7)
    assert wake(held, 360).returncode == 0
    assert "idle and holding:" in held.sent()[0]


def test_failed_delivery_and_partial_receipt_directory_retry(held: Workflow):
    receipts = held.repo / FOLDER / "wake-idle-holding.sent"
    receipts.mkdir()
    (held.state / "send-fails").touch()
    assert wake(held).returncode == 1
    assert list(receipts.iterdir()) == []
    (held.state / "send-fails").unlink()
    assert wake(held, 360).returncode == 0
    assert (receipts / "ticket-7").is_file()
    assert wake(held, 720).returncode == 0
    assert len(held.sent()) == 2  # Failed attempt and successful retry.


def test_two_calls_deliver_one_alert(held: Workflow):
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(wake, held, 0, "ticket-7") for _ in range(2)]
        results = [call.result() for call in calls]
    assert [result.returncode for result in results] == [0, 0]
    assert sum("idle and holding:" in message for message in held.sent()) == 1


def test_delivery_lock_timeout_preserves_next_attempt(held: Workflow):
    lock = held.repo / FOLDER / "wake-phone.lock"
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        started = time.monotonic()
        result = wake(held)
        elapsed = time.monotonic() - started
        assert result.returncode == 1
        assert 14 <= elapsed < 35
        assert held.sent() == []
    assert wake(held, 360).returncode == 0
    assert "idle and holding:" in held.sent()[0]


@pytest.mark.parametrize("change", ["waiters-cleared", "holder-exits", "background-job-started"])
def test_state_changes_while_delivery_lock_is_held(held: Workflow, change: str):
    lock = held.repo / FOLDER / "wake-phone.lock"
    with lock.open("w") as stream, ThreadPoolExecutor(max_workers=1) as pool:
        fcntl.flock(stream, fcntl.LOCK_EX)
        call = pool.submit(wake, held)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if held.wake_log.exists() and "every 20 minutes" in held.wake_log.read_text():
                break
            time.sleep(0.02)
        else:
            fcntl.flock(stream, fcntl.LOCK_UN)
            pytest.fail("Wake did not reach delivery before deadline")
        if change == "waiters-cleared":
            write_waits(held, {})
        elif change == "holder-exits":
            # A terminal/server exit does not run Shipline's session-stop cleanup.
            result = subprocess.run(
                ["tmux", "-L", "ticket-7", "kill-server"], env=held.env,
                capture_output=True, text=True, timeout=5,
            )
            assert result.returncode == 0
        else:
            (held.notes(7) / "ticket-7.job-wait").touch()
        fcntl.flock(stream, fcntl.LOCK_UN)
        assert call.result().returncode == 0
    notices = [line for message in held.sent()
               for line in re.findall(r"^idle and holding:.*$", message, re.MULTILINE)]
    assert notices == [], "Do not report a holder after its session or wait has ended"


def test_lost_check_and_late_partial_retry_share_delivery(held: Workflow):
    pending = held.repo / FOLDER / "lost-checks.pending"
    pending.mkdir()
    event = pending / "example.json"
    event.write_text(json.dumps({"check": "CI", "run": 123, "number": 9}))
    (held.state / "send-fails").touch()
    assert wake(held).returncode == 1
    assert event.exists()
    (held.state / "send-fails").unlink()
    assert wake(held, 5400).returncode == 0
    message = held.sent()[-1]
    assert "lost check: CI of run 123 on pull request 9" in message
    assert "ticket-7 (210 min) holds 9, 12" in message
    assert not event.exists()
    assert wake(held, 5760).returncode == 0
    assert len(held.sent()) == 2


def test_custom_state_folder_with_spaces(held: Workflow):
    folder = ".local/example state"
    config = held.repo / "shipline.json"
    values = json.loads(config.read_text())
    values["state_folder"] = folder
    config.write_text(json.dumps(values))
    mark_idle(held, 7, folder=folder)
    write_waits(held, folder=folder)
    (held.repo / folder / "orchestrator").write_text("example-app\n")
    assert wake(held).returncode == 0
    assert "idle and holding:" in held.sent()[0]
    assert (held.repo / folder / "wake-idle-holding.sent/ticket-7").is_file()


def test_repository_path_is_neither_executed_nor_sent(held: Workflow):
    write_waits(held, {"9": "9 waits: shares $(touch example-injected);`false` with 7"})
    assert wake(held).returncode == 0
    [message] = held.sent()
    assert "ticket-7 (120 min) holds 9" in message
    assert "example-injected" not in message
    assert not (held.repo / "example-injected").exists()
    receipts = held.repo / FOLDER / "wake-idle-holding.sent"
    assert stat.S_IMODE(receipts.stat().st_mode) == 0o700
    assert stat.S_IMODE((receipts / "ticket-7").stat().st_mode) == 0o600


def test_session_disappears_before_the_wake(held: Workflow):
    (held.state / "tmux/ticket-7").unlink()
    assert wake(held).returncode == 0
    assert held.sent() == []


def test_missing_registration_does_not_acknowledge_the_stretch(held: Workflow):
    registration = held.repo / FOLDER / "orchestrator"
    registration.unlink()
    assert wake(held).returncode == 0
    assert held.sent() == []
    assert not (held.repo / FOLDER / "wake-idle-holding.sent/ticket-7").exists()
    registration.write_text("example-app\n")
    assert wake(held, 360).returncode == 0
    assert "idle and holding:" in held.sent()[0]


def test_github_read_failure_does_not_hide_local_waits(held: Workflow):
    (held.state / "fail-gh-list").touch()
    assert wake(held).returncode == 0
    assert "idle and holding:" in held.sent()[0]
    assert "pull requests: could not be read" in held.sent()[0]


def test_sender_timeout_keeps_the_stretch_for_retry(held: Workflow):
    held.fake("timeout", '#!/bin/bash\nif [[ $1 == 90 ]]; then exit 124; fi\nexec /usr/bin/timeout "$@"\n')
    assert wake(held).returncode == 1
    assert not (held.repo / FOLDER / "wake-idle-holding.sent/ticket-7").exists()
    (held.bin / "timeout").unlink()
    assert wake(held, 360).returncode == 0
    assert "idle and holding:" in held.sent()[0]


def test_phone_failure_retry_new_stretch_and_session_handoff(held: Workflow):
    # Only the curl stand-in sees these deliberately invalid credentials.
    keys = held.root / "home/agents/.env"
    keys.parent.mkdir()
    keys.write_text("PUSHOVER_USER=example\nPUSHOVER_TOKEN=example\n")
    keys.chmod(0o600)
    (held.state / "agents.json").write_text("[]")
    (held.state / "pushover-fails").touch()
    assert wake(held).returncode == 1
    (held.state / "pushover-fails").unlink()
    assert wake(held, 360).returncode == 0
    count = len(held.pushes())
    assert wake(held, 720).returncode == 0
    assert len(held.pushes()) == count
    assert "ticket-7 (126 min) holds 9, 12" in held.pushes()[-1].message
    mark_idle(held, 7, NOON + 720)
    assert wake(held, 4320).returncode == 0
    assert len(held.pushes()) == count + 1
    held.orchestrator("busy", name="example-app")
    assert wake(held, 4680).returncode == 0
    assert "ticket-7 (66 min) holds 9, 12" in held.sent()[0]
    assert wake(held, 5040).returncode == 0
    assert len(held.sent()) == 1


def test_notify_waiting_flow_does_not_report_a_background_job(held: Workflow):
    assert held.notify({"hook_event_name": "UserPromptSubmit"}).returncode == 0
    assert held.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "ticket-7"}).returncode == 0
    assert held.notify({
        "hook_event_name": "Stop", "last_assistant_message": "Waiting for the example job.",
    }).returncode == 0
    assert (held.notes(7) / "ticket-7.job-wait").exists()
    assert wake(held).returncode == 0
    assert held.sent() == []


def test_shadow_observation_does_not_consume_delivery(held: Workflow):
    shadow = held.root / "shadow.log"
    result = held.run_script("wake", env={
        "WORKFLOW_NOW": str(NOON), "TZ": "UTC", "SHIPLINE_SHADOW": str(shadow),
    })
    assert result.returncode == 0
    assert "idle and holding:" in shadow.read_text()
    assert not (held.repo / FOLDER / "wake-idle-holding.sent/ticket-7").exists()
    assert wake(held).returncode == 0
    assert "idle and holding:" in held.sent()[0]
