"""Independent phone-fallback break tests for PR 98; external services are stand-ins."""

import fcntl
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from workflow_harness import Workflow


START = int(datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def installation(workflow: Workflow) -> Workflow:
    workflow.add_worktree(7)
    workflow.running("review-7")
    workflow.orchestrator("busy", name="example-orchestrator")
    (workflow.state / "agents.json").write_text("[]")
    keys = workflow.root / "home/agents/.env"
    keys.parent.mkdir(parents=True)
    keys.write_text("PUSHOVER_USER=ExampleUser\nPUSHOVER_TOKEN=ExampleToken\n")
    keys.chmod(0o600)
    return workflow


def wake(installation, minute=0, *names, extra=None):
    return installation.run_script(
        "wake", *names,
        env={"WORKFLOW_NOW": str(START + minute * 60), "TZ": "UTC", **(extra or {})},
    )


def queue(installation, kind, name, payload):
    folder = installation.repo / ".local/workflow" / (kind + ".pending")
    folder.mkdir(parents=True, exist_ok=True)
    event = folder / (name + ".json")
    event.write_text(json.dumps(payload))
    return event


def question(installation, minute=0):
    notes = installation.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "review-7.idle").touch()
    (notes / "review-7.asking").write_text(f"{START + minute * 60}\n")


def messages(installation):
    return [push.message for push in installation.pushes()]


def test_long_closure_restart_and_new_question(installation):
    question(installation)
    for minute in (6, 12, 18, 360, 720, 1440, 2160, 2880):
        assert wake(installation, minute).returncode == 0
    assert messages(installation) == [
        "Orchestrator is not open.\nasking the owner: review-7 (6 min)"
    ]
    # Each invocation is a new process; no process memory can deduplicate these deliveries.
    question(installation, 2881)
    assert wake(installation, 2886).returncode == 0
    assert len(messages(installation)) == 2
    installation.orchestrator("busy", name="example-orchestrator")
    assert wake(installation, 2892).returncode == 0
    assert "asking the owner: review-7 (11 min)" in installation.sent()[-1]
    assert len(messages(installation)) == 2


def test_night_summaries_remain_quiet_and_open_delivery_works(installation):
    for minute in (600, 621, 642):
        assert wake(installation, minute).returncode == 0
    assert messages(installation) == []
    installation.orchestrator("idle", name="example-orchestrator")
    assert wake(installation, 663).returncode == 0
    assert len(installation.sent()) == 1
    assert messages(installation) == []


def test_lost_check_retry_and_partial_rerun_keep_event_identity(installation):
    payload = {"number": 7, "check": "CI", "run": 123}
    event = queue(installation, "lost-checks", "7-example-head", payload)
    assert wake(installation).returncode == 0
    for minute in (6, 12):
        replacement = event.with_suffix(".new")
        replacement.write_text(json.dumps(payload))
        replacement.replace(event)
        assert wake(installation, minute).returncode == 0
    assert len(messages(installation)) == 1
    queue(installation, "lost-checks", "8-example-head", {**payload, "number": 8, "run": 124})
    assert wake(installation, 18).returncode == 0
    assert len(messages(installation)) == 2
    installation.orchestrator("busy", name="example-orchestrator")
    assert wake(installation, 24).returncode == 0
    assert not event.exists()
    assert "run 123 on pull request 7" in installation.sent()[-1]
    assert "run 124 on pull request 8" in installation.sent()[-1]
    assert len(messages(installation)) == 2


def test_partial_phone_failure_retries_only_the_unconfirmed_notice(installation):
    queue(installation, "stuck", "a", {"line": "stuck? review-7 (45 min)"})
    queue(installation, "stuck", "b", {"line": "idle, no rule: ticket-8 (50 min)"})
    original = (installation.bin / "curl").read_text()
    installation.fake("curl", original + '\n[[ ${call##*/} != 1 ]]\n')
    assert wake(installation).returncode == 1
    assert len(messages(installation)) == 2
    installation.fake("curl", original)
    for minute in (6, 12):
        assert wake(installation, minute).returncode == 0
    assert len(messages(installation)) == 3
    assert messages(installation)[1] == messages(installation)[2]
    assert not messages(installation)[2].startswith("Orchestrator")


def test_missing_phone_keys_do_not_consume_question(installation):
    question(installation)
    keys = installation.root / "home/agents/.env"
    saved = keys.read_text()
    keys.unlink()
    assert wake(installation).returncode == 1
    assert messages(installation) == []
    keys.write_text(saved)
    assert wake(installation, 6).returncode == 0
    assert len(messages(installation)) == 1
    assert messages(installation)[0].startswith("Orchestrator is not open.")


@pytest.mark.parametrize("registration", [None, ""])
def test_missing_or_empty_registration_remains_quiet(installation, registration):
    question(installation)
    file = installation.repo / ".local/workflow/orchestrator"
    if registration is None:
        file.unlink()
    else:
        file.write_text(registration)
    assert wake(installation).returncode == 0
    assert messages(installation) == []


def test_two_concurrent_callers_share_question_and_queue_receipts(installation):
    question(installation)
    queue(installation, "lost-checks", "7-example", {"number": 7, "check": "CI", "run": 123})
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(wake, installation, 0, "review-7") for _ in range(2)]
        results = [call.result() for call in calls]
    assert [result.returncode for result in results] == [0, 0]
    assert len(messages(installation)) == 3
    assert sum(message.startswith("Orchestrator is not open.") for message in messages(installation)) == 1


def test_contended_phone_lock_expires_without_acknowledgement(installation):
    question(installation)
    notes = installation.repo / ".local/workflow"
    with (notes / "wake-phone.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = time.monotonic()
        result = wake(installation)
        elapsed = time.monotonic() - before
        assert result.returncode == 1
        assert 14 <= elapsed < 25
        assert messages(installation) == []
    assert wake(installation, 6).returncode == 0
    assert len(messages(installation)) == 1


def test_session_list_timeout_uses_phone_and_is_bounded(installation):
    question(installation)
    installation.fake("timeout", '''#!/bin/bash
if [[ $2 == claude && $3 == agents ]]; then
  [[ $1 == 15 ]] || exit 2
  echo timed-out >> "@STATE@/timeouts"
  exit 124
fi
exec /usr/bin/timeout "$@"
''')
    assert wake(installation).returncode == 0
    assert (installation.state / "timeouts").exists()
    assert len(messages(installation)) == 1
    assert wake(installation, 6).returncode == 0
    assert len(messages(installation)) == 1


def test_phone_timeout_retries_without_marking_delivery(installation):
    question(installation)
    installation.fake("timeout", '''#!/bin/bash
if [[ $2 == curl ]]; then
  [[ $1 == 4 ]] || exit 2
  exit 124
fi
exec /usr/bin/timeout "$@"
''')
    assert wake(installation).returncode == 1
    assert messages(installation) == []
    (installation.bin / "timeout").unlink()
    assert wake(installation, 6).returncode == 0
    assert len(messages(installation)) == 1


def test_shadow_does_not_take_receipt_or_ring(installation):
    question(installation)
    shadow = installation.root / "shadow.log"
    assert wake(installation, extra={"SHIPLINE_SHADOW": str(shadow)}).returncode == 0
    assert messages(installation) == []
    assert not (installation.repo / ".local/workflow/wake-phone.sent").exists()
    assert wake(installation).returncode == 0
    assert len(messages(installation)) == 1


def test_notice_text_is_literal_and_receipts_are_private(installation):
    marker = installation.root / "unexpected-command"
    line = 'stuck? review-7; $(touch "' + str(marker) + '") `false`'
    queue(installation, "stuck", "example", {"line": line + "\nMore detail"})
    assert wake(installation).returncode == 0
    assert messages(installation) == ["Orchestrator is not open.\n" + line]
    assert not marker.exists()
    notes = installation.repo / ".local/workflow"
    for name in ("wake-phone.sent", "wake-phone.closed", "wake-phone.lock"):
        assert (notes / name).stat().st_mode & 0o077 == 0
    receipts = list((notes / "wake-phone.sent").iterdir())
    assert len(receipts) == 1
    assert receipts[0].stat().st_mode & 0o077 == 0
    assert len(receipts[0].name) == 64
    assert receipts[0].read_bytes() == b""
    log = installation.wake_log.read_text()
    assert "ExampleToken" not in log
    assert "ExampleUser" not in log


def test_new_tmp_pressure_after_a_quiet_daytime_recovery_rings_again(installation):
    def pressure(used):
        installation.fake("df", "#!/bin/bash\n"
                          "echo 'Filesystem Inodes IUsed IFree IUse% Mounted on'\n"
                          f"echo 'tmpfs 1000 {used} {1000 - used} {used // 10}% /tmp'\n")

    pressure(900)
    assert wake(installation).returncode == 0
    assert len(messages(installation)) == 1
    pressure(100)
    assert wake(installation, 6).returncode == 0
    pressure(900)
    assert wake(installation, 12).returncode == 0
    rings = len(messages(installation))
    assert rings == 2, "a new pressure stretch must ring after recovery"


def test_session_delivery_while_another_wake_waits_does_not_ring_stale_event(installation):
    event = queue(installation, "stuck", "example", {"line": "stuck? review-7 (45 min)"})
    notes = installation.repo / ".local/workflow"
    # A real flock lets the second wake read its snapshot before delivery finishes.
    with (notes / "wake-phone.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(wake, installation, 0, "review-7")
            looked = installation.wake_log.with_name("wake.looked")
            deadline = time.monotonic() + 10
            while not installation.wake_log.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert looked.exists()
            # Simulate the other delivery's atomic queue retirement and reset acknowledgement.
            sent = notes / "stuck.sent"
            sent.mkdir()
            event.replace(sent / event.name)
            installation.wake_log.with_name("wake.pending").unlink()
            fcntl.flock(lock, fcntl.LOCK_UN)
            assert pending.result().returncode == 0
    assert messages(installation) == []


def test_github_read_failure_cannot_hide_a_queued_lost_check(installation):
    queue(installation, "lost-checks", "7-example", {"number": 7, "check": "CI", "run": 123})
    (installation.state / "fail-gh-list").touch()
    assert wake(installation).returncode == 0
    assert len(messages(installation)) == 1
    assert "lost check: CI of run 123" in messages(installation)[0]


def test_same_second_cloud_questions_have_distinct_comment_receipts(installation):
    notes = installation.repo / ".local/workflow"
    (notes / "cloud-8").write_text("{}")
    asking = notes / "cloud-8.asking"
    asking.write_text(f"{START} 101\n")
    assert wake(installation).returncode == 0
    asking.write_text(f"{START} 102\n")
    assert wake(installation, 6).returncode == 0
    assert wake(installation, 12).returncode == 0
    assert len(messages(installation)) == 2


@pytest.mark.parametrize("late", [False, True])
def test_usage_alarm_partial_producer_retry_and_resumption(installation, late):
    def produce(minute, waiting):
        source = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ticket_starts import note_usage_hold
note_usage_hold(Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
'''
        done = subprocess.run(
            ["python3", "-c", source, str(installation.script.parent),
             str(installation.repo / ".local/workflow"), str(START + minute * 60), str(waiting)],
            cwd=installation.repo, env=installation.env, text=True,
            capture_output=True, timeout=10,
        )
        assert done.returncode == 0, done.stderr

    produce(0, 2)
    if not late:
        produce(61, 2)
        assert wake(installation, 61).returncode == 0
    produce(120, 2)
    # A partial rerun lost the producer's flags but retained its published events.
    record = installation.repo / ".local/workflow/ticket-starts-usage.json"
    saved = json.loads(record.read_text())
    record.write_text(json.dumps({**saved, "told": False, "phone": False}))
    produce(123, 2)
    assert wake(installation, 123).returncode == 0
    assert len(messages(installation)) == (1 if late else 2)
    produce(129, 0)
    assert wake(installation, 129).returncode == 0
    assert messages(installation)[-1] == "starts resumed"
    assert len(messages(installation)) == (2 if late else 3)
