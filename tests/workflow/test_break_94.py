"""Independent break cases for the unread-usage alarm; outside services are stand-ins."""

import fcntl
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from workflow_harness import Workflow
from workflow.test_ticket_starts import _draft, _room_since


START = 1_800_014_400


class Alarm:
    def __init__(self, workflow: Workflow):
        self.w = workflow
        self.notes = workflow.repo / ".local/workflow"
        self.notes.mkdir(parents=True, exist_ok=True)
        workflow.orchestrator("busy")
        for number in (7, 8, 9):
            _draft(workflow, number, "README.md", body="## Files\n\nREADME.md\n")
        self.cache = workflow.state / "usage.json"
        self.cache.write_text("{}")
        workflow.env["WORKFLOW_USAGE_CACHE"] = str(self.cache)
        self.keys = workflow.root / "home/agents/.env"
        self.keys.parent.mkdir(exist_ok=True)
        self.keys.write_text("PUSHOVER_USER=exampleuser\nPUSHOVER_TOKEN=exampletoken\n")

    def pick(self, minute: int):
        moment = START + minute * 60
        # Replay a status line that refreshes its file on each tick, on the same clock.
        if self.cache.exists():
            os.utime(self.cache, (moment, moment))
        _room_since(self.w, moment)
        snapshot = self.w.root / "snapshot.json"
        for script, args in (
            ("tick_snapshot.py", ["create", str(snapshot)]),
            ("ticket_starts.py", ["pick", str(self.w.repo), str(snapshot)]),
        ):
            done = self.w.run_python(script, *args, env={"WORKFLOW_NOW": str(moment)})
            assert done.returncode == 0, "picker failed"

    def wake(self, minute: int, **extra):
        return self.w.run_script("wake", env={
            "WORKFLOW_NOW": str(START + minute * 60), "TZ": "UTC", **extra,
        })

    def readable(self, percent=20):
        self.cache.write_text(json.dumps({"five_hour": {
            "utilization": percent, "resets_at": None,
        }}))

    def lines(self):
        return [line for message in self.w.sent() for line in message.splitlines()
                if line.startswith("starts held:") or line == "starts resumed"]


@pytest.fixture
def alarm(workflow):
    return Alarm(workflow)


@pytest.mark.parametrize("zone", ["UTC", "Etc/GMT+12"])
def test_thresholds_and_daily_delivery_are_once_per_stretch(alarm, zone):
    alarm.w.env["TZ"] = zone
    for minute in (0, 59, 60):
        alarm.pick(minute)
        assert alarm.wake(minute, TZ=zone).returncode == 0
    assert alarm.lines() == []
    for minute in (61, 64, 119, 120, 123, 180, 1440):
        alarm.pick(minute)
        assert alarm.wake(minute, TZ=zone).returncode == 0
    assert alarm.lines() == ["starts held: usage unread for 1 h, 3 tickets waiting"]
    assert [push.message for push in alarm.w.pushes()] == [
        "starts held: usage unread for 2 h, 3 tickets waiting"
    ]
    alarm.readable()
    for minute in (1443, 1446):
        alarm.pick(minute)
        assert alarm.wake(minute).returncode == 0
    assert alarm.lines()[-1] == "starts resumed"
    assert alarm.lines().count("starts resumed") == 1


@pytest.mark.parametrize("reason", ["load", "memory", "plan", "empty", "readable ceiling"])
def test_other_holds_are_quiet(alarm, reason):
    if reason == "load":
        alarm.w.loadavg_file.write_text("9 9 9 1/100 1234\n")
    elif reason == "memory":
        alarm.w.pressure_file.write_text(
            "some avg10=99.00 avg60=99.00 avg300=99.00 total=1\n"
        )
    elif reason == "plan":
        (alarm.notes / "start-plan").write_text("hold all\n")
    elif reason == "empty":
        for number in (7, 8, 9):
            alarm.w.set_pull_request(number, "CLOSED", alarm.w.git("rev-parse", "HEAD"))
    else:
        alarm.readable(95)
    for minute in (0, 61, 120, 1440):
        alarm.pick(minute)
        assert alarm.wake(minute).returncode == 0
    assert alarm.lines() == []
    assert alarm.w.pushes() == []


def test_missing_usage_file_still_raises_alarm(alarm):
    alarm.cache.unlink()
    alarm.pick(0)
    alarm.pick(120)
    assert alarm.wake(120).returncode == 0
    assert len(alarm.lines()) == 1
    assert len(alarm.w.pushes()) == 1


def test_held_phone_lock_defers_without_losing_alarm(alarm):
    alarm.pick(0)
    alarm.pick(120)
    with (alarm.notes / "ticket-starts-phone.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert alarm.wake(120).returncode == 0
        assert alarm.w.pushes() == []
        assert len(alarm.lines()) == 1
        assert list((alarm.notes / "ticket-starts-phone.pending").glob("*.json"))
    assert alarm.wake(123).returncode == 0
    assert alarm.wake(126).returncode == 0
    assert len(alarm.w.pushes()) == 1


def test_parallel_phone_delivery_is_once(alarm):
    alarm.pick(0)
    alarm.pick(61)
    assert alarm.wake(61).returncode == 0
    alarm.pick(120)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(alarm.wake, [120, 120]))
    assert all(done.returncode == 0 for done in results)
    assert len(alarm.w.pushes()) == 1
    assert len(alarm.lines()) == 1


def test_partial_rerun_does_not_repeat_successful_phone_send(alarm):
    alarm.pick(0)
    alarm.pick(120)
    (alarm.w.state / "send-fails").touch()
    assert alarm.wake(120).returncode == 1
    assert len(alarm.w.pushes()) == 1
    (alarm.w.state / "send-fails").unlink()
    assert alarm.wake(123).returncode == 0
    assert len(alarm.lines()) == 1  # The stand-in records failed delivery attempts too.
    assert alarm.wake(126).returncode == 0
    assert len(alarm.lines()) == 2
    assert len(alarm.w.pushes()) == 1


def test_missing_notification_settings_retry_after_repair(alarm):
    alarm.keys.unlink()
    alarm.pick(0)
    alarm.pick(120)
    assert alarm.wake(120).returncode == 1
    assert len(alarm.lines()) == 1
    alarm.keys.write_text("PUSHOVER_USER=exampleuser\nPUSHOVER_TOKEN=exampletoken\n")
    assert alarm.wake(123).returncode == 0
    assert len(alarm.w.pushes()) == 1


def test_phone_timeout_leaves_retry_and_does_not_hide_orchestrator(alarm):
    alarm.pick(0)
    alarm.pick(120)
    original = (alarm.w.bin / "curl").read_text()
    alarm.w.fake("curl", "#!/bin/bash\nexec /bin/sleep 30\n")
    assert alarm.wake(120, WORKFLOW_NOTIFY_SECONDS="1").returncode == 1
    assert len(alarm.lines()) == 1
    assert list((alarm.notes / "ticket-starts-phone.pending").glob("*.json"))
    alarm.w.fake("curl", original)
    assert alarm.wake(123).returncode == 0
    assert len(alarm.w.pushes()) == 1


def test_github_read_failure_cannot_silence_alarm(alarm):
    alarm.pick(0)
    alarm.pick(120)
    (alarm.w.state / "fail-gh-list").touch()
    assert alarm.wake(120).returncode == 0
    assert len(alarm.lines()) == 1
    assert len(alarm.w.pushes()) == 1


@pytest.mark.parametrize("check_state", ["lost", "late", "partial rerun"])
def test_check_notices_cannot_hide_alarm(alarm, check_state):
    alarm.pick(0)
    alarm.pick(120)
    pending = alarm.notes / "lost-checks.pending"
    pending.mkdir()
    (pending / "example.json").write_text(json.dumps({
        "check": "CI result", "run": 123, "number": 7,
    }))
    checks = {"lost": [], "late": [{"status": "IN_PROGRESS"}],
              "partial rerun": [{"conclusion": "SUCCESS"}, {"status": "QUEUED"}]}
    response = alarm.w.state / "check-response.json"
    response.write_text(json.dumps([{
        "number": 7, "isDraft": False, "labels": [],
        "statusCheckRollup": checks[check_state],
    }]))
    (alarm.w.state / "usual-gh").write_text((alarm.w.bin / "gh").read_text())
    alarm.w.fake("gh", '''#!/bin/bash
if [[ "$1 $2" == "pr list" && " $* " == *" --limit 200 "* ]]; then
  jq -r "${@: -1}" '@STATE@/check-response.json'
  exit $?
fi
exec bash '@STATE@/usual-gh' "$@"
''')
    assert alarm.wake(120).returncode == 0
    assert len(alarm.lines()) == 1
    assert len(alarm.w.pushes()) == 1
    assert "lost check: CI result of run 123 on pull request 7" in alarm.w.sent()[-1]
    assert not list(pending.glob("*.json"))


def test_another_hold_ends_the_stretch_and_rearms_a_fresh_clock(alarm):
    alarm.pick(0)
    alarm.pick(61)
    assert alarm.wake(61).returncode == 0
    (alarm.notes / "start-plan").write_text("hold all\n")
    alarm.pick(67)
    assert alarm.wake(67).returncode == 0
    assert alarm.lines().count("starts resumed") == 1
    (alarm.notes / "start-plan").unlink()
    alarm.pick(70)
    alarm.pick(129)
    assert alarm.wake(129).returncode == 0
    assert sum(line.startswith("starts held:") for line in alarm.lines()) == 1
    alarm.pick(131)
    assert alarm.wake(131).returncode == 0
    assert sum(line.startswith("starts held:") for line in alarm.lines()) == 2
    assert alarm.w.pushes() == []


@pytest.mark.parametrize("delivered", [False, True])
def test_flag_save_interruption_preserves_alert_and_resume(alarm, delivered):
    alarm.pick(0)
    record = alarm.notes / "ticket-starts-usage.json"
    before = record.read_text()
    alarm.pick(120)
    record.write_text(before)
    if delivered:
        assert alarm.wake(120).returncode == 0
    alarm.readable()
    alarm.pick(126)
    assert alarm.wake(126).returncode == 0
    assert sum(line.startswith("starts held:") for line in alarm.lines()) == 1
    assert alarm.lines().count("starts resumed") == 1
    assert alarm.wake(132).returncode == 0
    assert len(alarm.w.pushes()) == 1


@pytest.mark.parametrize("bad_record", ["{", "[]", '{"since": true}', "null"])
def test_invalid_hold_record_recovers_with_new_clock(alarm, bad_record):
    (alarm.notes / "ticket-starts-usage.json").write_text(bad_record)
    alarm.pick(0)
    assert alarm.wake(59).returncode == 0
    assert alarm.lines() == []
    alarm.pick(61)
    assert alarm.wake(61).returncode == 0
    assert len(alarm.lines()) == 1


def test_state_and_event_payloads_are_private_under_loose_umask(alarm):
    previous = os.umask(0)
    try:
        alarm.pick(0)
        alarm.pick(120)
        assert alarm.wake(120).returncode == 0
    finally:
        os.umask(previous)
    records = [alarm.notes / "ticket-starts-usage.json"]
    for kind in ("alert", "phone"):
        records.extend((alarm.notes / f"ticket-starts-{kind}.sent").glob("*.json"))
    assert len(records) == 3
    assert all(path.stat().st_mode & 0o077 == 0 for path in records)


def test_shadow_does_not_acknowledge_or_ring(alarm):
    alarm.pick(0)
    alarm.pick(120)
    assert alarm.wake(120, SHIPLINE_SHADOW=str(alarm.w.root / "shadow.log")).returncode == 0
    assert alarm.lines() == []
    assert alarm.w.pushes() == []
    assert len(list((alarm.notes / "ticket-starts-alert.pending").glob("*.json"))) == 1
    assert alarm.wake(120).returncode == 0
    assert len(alarm.lines()) == 1
    assert len(alarm.w.pushes()) == 1
