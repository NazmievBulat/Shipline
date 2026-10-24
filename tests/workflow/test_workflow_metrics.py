"""`workflow-metrics` computes the daily numbers and fires the fixed gap detectors."""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "workflow_metrics"
# 24 October 2026, 07:50 UTC: the time of the daily run.
NOW = 1792828200
AT = {"WORKFLOW_NOW": str(NOW), "TZ": "UTC"}
HOUR = 3600
DAY = 24 * HOUR

# Answers what the metrics read from GitHub with the fixtures: the merged pull requests in
# pages of 100, the repository's runs and each run's jobs.
_FAKE_GH = """#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

fixtures = Path("@FIXTURES@")
arguments = sys.argv[1:]
with open("@STATE@/calls.log", "a") as log:
    log.write("gh " + " ".join(arguments) + "\\n")
if arguments[:2] == ["api", "graphql"]:
    query = next(word for word in arguments if word.startswith("query="))
    words = next(word for word in arguments if word.startswith("q="))
    assert "first: 100" in query and "is:merged" in words, arguments
    after = [word for word in arguments if word.startswith("after=")]
    pages = json.loads((fixtures / "pulls.json").read_text())
    print(json.dumps(pages[1] if after == ["after=page-2"] else pages[0]))
    sys.exit(0)
path = arguments[-1]
jobs = json.loads((fixtures / "jobs.json").read_text())
runs = "repos/example-org/example-app/actions/runs"
if found := re.fullmatch(runs + r"/(\\d+)/attempts/(\\d+)/jobs\\?per_page=100", path):
    print(json.dumps({"jobs": jobs[f"{found[1]}-{found[2]}"]}))
elif found := re.fullmatch(runs + r"/(\\d+)/jobs\\?per_page=100", path):
    print(json.dumps({"jobs": jobs[found[1]]}))
elif urlsplit(path).path == runs:
    asked = parse_qs(urlsplit(path).query)
    assert asked["created"] and asked["per_page"] == ["100"], asked
    runs = json.loads((fixtures / "runs.json").read_text())
    print(json.dumps({"total_count": len(runs), "workflow_runs": runs}))
else:
    print(f"gh: could not answer {arguments}", file=sys.stderr)
    sys.exit(1)
"""


def _github(workflow: Workflow) -> None:
    workflow.fake("gh", _FAKE_GH.replace("@FIXTURES@", str(FIXTURES)))


def _note(path: Path, value: dict[str, Any], at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    os.utime(path, (at, at))


def _notes(workflow: Workflow) -> None:
    """The wake log, the hand-backs, the lost checks and the hunts of the fixture's week."""
    log = workflow.wake_log
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text((FIXTURES / "wake.log").read_text())
    notes = workflow.repo / ".local" / "workflow"
    for number, head in enumerate(("1a2b3c4d5e6f", "0f1e2d3c4b5a", "abcdef012345", "a1", "b2")):
        reason = f"the gate refused {20 + number}: CI result failed on {head}"
        _note(
            notes / "merge-handbacks.sent" / f"{number:032x}",
            {"number": str(20 + number), "reason": reason},
            NOW - DAY - number * HOUR,
        )
    _note(
        notes / "merge-handbacks.pending" / f"{9:032x}",
        {"number": "22", "reason": "main is red"},
        NOW - HOUR,
    )
    for number in (21, 22, 23):
        _note(
            notes / "lost-checks.sent" / f"{number}-{'a' * 40}.json",
            {"number": str(number), "head": "a" * 40, "check": "CI result", "run": 7},
            NOW - 2 * DAY,
        )
    # A week and a day ago: outside the window.
    _note(
        notes / "lost-checks.sent" / f"19-{'b' * 40}.json",
        {"number": "19", "head": "b" * 40, "check": "CI result", "run": 6},
        NOW - 8 * DAY,
    )
    report = workflow.hunt_report("plan-phone-2") / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Hunt plan-phone-2\n\nFindings: 3\n\n## One\n\nThe 4 steps.\n")
    os.utime(report, (NOW - DAY, NOW - DAY))
    workflow.hunt_report("plan-phone-3").mkdir(parents=True)


def _measure(workflow: Workflow, *args: str) -> dict[str, Any]:
    done = workflow.run_script("workflow-metrics", "--json", *args, env=AT)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _detectors(measured: dict[str, Any]) -> list[str]:
    return sorted(finding["detector"] for finding in measured["findings"])


def test_the_fixtures_give_the_expected_table_and_fire_only_their_detectors(
    workflow: Workflow,
) -> None:
    _github(workflow)
    _notes(workflow)

    measured = _measure(workflow)

    week, day = measured["last_days"], measured["last_day"]
    assert (week["days"], day["days"]) == (7, 1)
    assert week["since"] == "2026-10-17T07:50:00Z"
    assert week["until"] == day["until"] == "2026-10-24T07:50:00Z"

    merged = week["merged"]
    assert merged["numbers"] == [21, 22, 23]
    assert day["merged"]["numbers"] == [22, 23]
    assert merged["per_hour"] == pytest.approx(3 / 168)
    stages = merged["stages"]
    assert stages["build"]["median"] == 1
    assert stages["review"]["median"] == 3
    assert stages["merge_line"]["median"] == 7
    assert stages["merge_line"]["p75"] == 12
    assert stages["merge_line"]["p90"] == 15
    assert stages["total"]["median"] == 11
    assert stages["merge_line"]["share"] == pytest.approx(29 / 39.5)
    assert stages["build"]["share"] == pytest.approx(3.5 / 39.5)
    [longest, second] = merged["gaps"]
    assert (longest["from"], longest["until"], longest["hours"]) == (
        "2026-10-22T18:00:00Z",
        "2026-10-23T20:00:00Z",
        26,
    )
    assert longest["wake"] == (
        "10 check-ins; main green 5, red 3, pending 1, advisory 1;"
        " 5-hour usage up to 91%; up to 3 sessions"
    )
    assert second["hours"] == 10.5
    assert second["wake"] == "no check-ins"

    runs = week["runs"]
    assert runs["workflows"]["CI"] == {"runs": 7, "median_minutes": 10, "p90_minutes": 10.8}
    assert runs["workflows"]["E2E"] == {"runs": 1, "median_minutes": 20, "p90_minutes": 20}
    assert runs["workflows"]["Images"] == {"runs": 1, "median_minutes": 5, "p90_minutes": 5}
    assert runs["slowest_job"] == {"workflow": "E2E", "job": "e2e (1)", "run": 201, "minutes": 14}
    assert runs["ci_runs"] == 6
    assert runs["ci_runs_per_merge"] == 2
    assert (runs["cancelled"], runs["completed"]) == (2, 10)
    assert runs["cancelled_share"] == pytest.approx(0.2)
    assert runs["runner_minutes"] == 101
    assert day["runs"]["ci_runs"] == 4
    assert day["runs"]["cancelled_share"] == pytest.approx(0.25)

    main = week["main"]
    assert main["check_ins"] == 10
    assert main["share"] == {"green": 0.5, "red": 0.3, "pending": 0.1, "advisory": 0.1}
    assert main["reasons"] == {"CI result": 4, "CI: dependencies": 1}
    assert [(red["since"], red["hours"], red["cause"]) for red in main["red_episodes"]] == [
        ("2026-10-23T01:30:00Z", 1.5, "CI result"),
        ("2026-10-23T03:30:00Z", 1.5, "CI result"),
        ("2026-10-23T05:40:00Z", pytest.approx(4 / 3), "CI result"),
        ("2026-10-23T08:30:00Z", 1.5, "CI: dependencies (advisory)"),
    ]
    assert day["main"]["check_ins"] == 3

    turns = week["turns"]
    assert {key: turns[key] for key in ("nudges", "status_checks", "finish_messages", "wakes")} == {
        "nudges": 4,
        "status_checks": 1,
        "finish_messages": 1,
        "wakes": 4,
    }
    assert turns["per_merge"]["nudges"] == pytest.approx(4 / 3)
    assert turns["hand_backs"] == {
        "the gate refused #: CI result failed on #": 5,
        "main is red": 1,
    }
    assert day["turns"]["nudges"] == 1
    assert day["turns"]["wakes"] == 1

    quality = week["quality"]
    assert quality["send_back_rounds_per_review"] == pytest.approx(1 / 3)
    assert quality["failed_scratch_runs"] == 1
    assert quality["flaky"] == {"CI: tests (1 of 2)": 1}
    assert quality["lost_checks"] == {"CI result": 3}
    assert quality["weaker_tests_items"] == 2
    assert quality["hunt_findings"] == 3
    assert quality["reverts"] == [23]
    assert quality["push_refusals"] == "not recorded"
    assert "hunt report: plan-phone-3" in week["not_found"]
    assert "weaker tests section: 22, 23" in week["not_found"]
    assert "machine samples" in week["not_found"]

    assert _detectors(measured) == [
        "cancelled_share",
        "ci_runs_per_merge",
        "hand_back_reason",
        "job_share",
        "lost_check",
        "main_not_green",
        "merge_line",
        "red_main_cause",
    ]
    lines = [finding["line"] for finding in measured["findings"]]
    assert "merge line 73% of the total time, 90th percentile 15.0 h, over 7 days" in lines[0]
    assert "pull requests 21, 22, 23" in lines[0]
    assert (
        "hand-back reason repeated 5 times in 7 days: the gate refused 20: CI result failed"
        " on 1a2b3c4d5e6f (pull requests 20, 21, 22, 23, 24)"
    ) in lines
    assert "lost check repeated 3 times in 7 days: CI result (pull requests 21, 22, 23)" in lines
    assert (
        "red main cause repeated 3 times in 7 days: CI result"
        " (since 2026-10-23T01:30:00Z, 2026-10-23T03:30:00Z, 2026-10-23T05:40:00Z)"
    ) in lines
    assert "CI runs per merged pull request 2.00 over 7 days (6 runs, 3 merges)" in lines
    assert "cancelled runs 20% over 7 days (2 of 10 runs: 103, 301)" in lines
    assert "one job over 60% of its run's time: CI: tests (1 of 1) (80% of run 101)" in lines
    assert "main pending or red in 50% of 10 check-ins over 7 days" in lines


def test_the_window_is_named_in_days(workflow: Workflow) -> None:
    _github(workflow)
    _notes(workflow)

    measured = _measure(workflow, "--days", "2")

    assert measured["last_days"]["days"] == 2
    assert measured["last_days"]["since"] == "2026-10-22T07:50:00Z"
    assert measured["last_days"]["runs"]["workflows"]["CI"]["runs"] == 7
    assert measured["last_days"]["main"]["check_ins"] == 10
    assert "merge line" in measured["findings"][0]["line"]
    assert "over 2 days" in measured["findings"][0]["line"]


def _sample(
    at: int,
    *,
    swapped_pages: int = 0,
    available_kb: int = 4 * 1024 * 1024,
    system_usec: int = 0,
    usage_usec: int = 0,
    load: float = 0.5,
    pressure: float = 1.0,
    groups: dict[str, int] | None = None,
    sessions: dict[str, str] | None = None,
    asking: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "at": at,
        "processors": 4,
        "pswpin": 0,
        "pswpout": swapped_pages,
        "available_kb": available_kb,
        "cpu_pressure": pressure,
        "load": load,
        "cpu": {"usage_usec": usage_usec, "system_usec": system_usec},
        "groups": groups or {},
        "sessions": sessions or {},
        "asking": asking or {},
    }


def _write_samples(workflow: Workflow, samples: list[dict[str, Any]]) -> None:
    path = workflow.root / "home" / ".local" / "state" / "workflow" / "machine.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(sample) + "\n" for sample in samples))


def _empty_github(workflow: Workflow) -> None:
    workflow.fake(
        "gh",
        """#!/usr/bin/env python3
import json
import sys
if sys.argv[1:3] == ["api", "graphql"]:
    print(json.dumps({"data": {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": []}}}))
else:
    print(json.dumps({"total_count": 0, "workflow_runs": []}))
""",
    )


def test_a_busy_machine_fires_the_machine_and_session_detectors(workflow: Workflow) -> None:
    _empty_github(workflow)
    start = NOW - DAY
    samples = []
    asked = start - 10 * 60
    for step in range(41):
        at = start + step * 180
        busy = {"ticket-7": "limit" if step == 20 else "busy"}
        reviews = {f"review-{number}": "idle" for number in (9, 10, 11)}
        samples.append(
            _sample(
                at,
                # One interval swaps 1,000,000 pages out while 10 GB are available.
                swapped_pages=1_000_000 if step >= 30 else 0,
                available_kb=10 * 1024 * 1024,
                # 30% of four processors in system time, 2.5 cores for the worker.
                system_usec=step * int(0.3 * 180e6 * 4),
                usage_usec=step * int(0.6 * 180e6 * 4),
                load=3.0,
                pressure=10.0,
                groups={"ticket-7": step * int(2.5 * 180e6), "workflow-tick": step * 1000},
                sessions={**busy, **reviews},
                asking={"review-9": asked},
            )
        )
    _write_samples(workflow, samples)

    measured = _measure(workflow)

    week = measured["last_days"]
    sessions = week["sessions"]
    assert sessions["kinds"]["ticket"]["hours"] == 2
    assert sessions["kinds"]["review"]["hours"] == 6
    assert sessions["kinds"]["review"]["sessions"] == 3
    assert sessions["kinds"]["review"]["idle_share"] == 1
    assert sessions["kinds"]["ticket"]["idle_share"] == 0
    assert sessions["kinds"]["review"]["median_lifetime_hours"] == 2
    assert sessions["questions"] == [{"session": "review-9", "waited_minutes": 130}]
    assert sessions["limit_stops"] == 1
    machine = week["machine"]
    assert machine["swap_out_mb_per_second"]["max"] == pytest.approx(1_000_000 * 4096 / 180 / 2**20)
    assert machine["system_cpu_share"]["max_hour"] == pytest.approx(0.3)
    assert machine["groups"]["ticket-7"]["max_hour_cores"] == pytest.approx(2.5)
    assert machine["groups"]["workflow-tick"]["max_hour_cores"] < 0.01
    assert machine["load_percent"]["max_hour"] == 75
    assert machine["cpu_pressure"]["max_hour"] == 10

    assert _detectors(measured) == [
        "group_cores",
        "idle_sessions",
        "load_over_pressure",
        "question_wait",
        "session_kind_hours",
        "swap_out",
        "system_cpu",
    ]
    lines = [finding["line"] for finding in measured["findings"]]
    assert "questions to the owner waited over 60 min: review-9 (130 min)" in lines
    assert (
        "sessions idle more than half their life over 7 days:"
        " review-10 (100% of 2.0 h), review-11 (100% of 2.0 h), review-9 (100% of 2.0 h)"
    ) in lines
    assert (
        "session kind review used 6.0 session-hours over 7 days, 3.0 times the workers' 2.0"
    ) in lines
    assert any(line.startswith("swap-out 22 MB a second while 10.0 GB") for line in lines)
    assert any(line.startswith("system CPU 30% of the whole for the hour to") for line in lines)
    assert any(
        line.startswith("load 75% of the processors against CPU pressure 10% for the hour to")
        and line.endswith("load that comes from waiting (swapping, blocked processes)")
        for line in lines
    )
    assert any(
        line.startswith("ticket-7 used 2.5 cores on average for the hour to") for line in lines
    )


def test_a_quiet_machine_fires_nothing(workflow: Workflow) -> None:
    _empty_github(workflow)
    _write_samples(
        workflow,
        [
            _sample(
                NOW - 2 * HOUR + step * 180,
                system_usec=step * int(0.05 * 180e6 * 4),
                load=0.5,
                pressure=10.0,
                groups={"ticket-7": step * int(0.5 * 180e6)},
                sessions={"ticket-7": "busy", "review-9": "idle" if step < 10 else "busy"},
            )
            for step in range(41)
        ],
    )

    measured = _measure(workflow)

    assert measured["findings"] == []
    assert measured["last_days"]["sessions"]["kinds"]["review"]["idle_share"] == 0.25


def test_a_gap_between_samples_is_not_counted(workflow: Workflow) -> None:
    """The machine was off: the interval says nothing about what ran."""
    _empty_github(workflow)
    _write_samples(
        workflow,
        [
            _sample(NOW - 5 * HOUR, groups={"ticket-7": 0}, sessions={"ticket-7": "idle"}),
            _sample(
                NOW - HOUR, groups={"ticket-7": int(4 * 3600e6)}, sessions={"ticket-7": "idle"}
            ),
        ],
    )

    measured = _measure(workflow)

    assert measured["findings"] == []
    assert measured["last_days"]["sessions"]["kinds"] == {}


def _proc(workflow: Workflow) -> None:
    for name in ("vmstat", "meminfo", "loadavg", "pressure/cpu"):
        target = workflow.root / "proc" / name
        target.write_text((FIXTURES / "proc" / name).read_text())
    workflow.loadavg_file.write_text((FIXTURES / "proc" / "loadavg").read_text())
    (workflow.cgroup_root / "cpu.stat").write_text(
        "usage_usec 900000000\nuser_usec 700000000\nsystem_usec 200000000\n"
    )


def test_a_sample_reads_proc_the_control_groups_and_the_sessions(workflow: Workflow) -> None:
    _proc(workflow)
    workflow.add_worktree(7)
    scope = "user.slice/user@1000.service/app.slice/workflow-ticket-7-4242.scope"
    group = workflow.running("ticket-7", scope=scope)
    (group / "cpu.stat").write_text("usage_usec 5000\nuser_usec 4000\nsystem_usec 1000\n")
    # A test run inside the session's scope belongs to the session.
    (group / "run-1").mkdir()
    (group / "run-1" / "cpu.stat").write_text("usage_usec 4000\n")
    tick = workflow.cgroup_root / "user.slice/user@1000.service/app.slice/workflow-tick.service"
    tick.mkdir(parents=True)
    (tick / "cpu.stat").write_text("usage_usec 777\n")
    other = workflow.cgroup_root / "user.slice/user@1000.service/app.slice/other.service"
    other.mkdir(parents=True)
    (other / "cpu.stat").write_text("usage_usec 1\n")
    notes = workflow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "ticket-7.idle").touch()
    (notes / "ticket-7.asking").write_text(f"{NOW - 600} 0\n")
    machine = workflow.root / "home" / ".local" / "state" / "workflow" / "machine.jsonl"
    machine.parent.mkdir(parents=True, exist_ok=True)
    # Nine days old: kept 8 days.
    old = _sample(NOW - 9 * DAY)
    kept = _sample(NOW - 7 * DAY)
    machine.write_text(json.dumps(old) + "\n" + json.dumps(kept) + "\n")

    done = workflow.run_script("workflow-metrics", "--sample", env=AT)

    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    [first, sample] = [json.loads(line) for line in machine.read_text().splitlines()]
    assert first == kept
    assert sample == {
        "at": NOW,
        "processors": 4,
        "pswpin": 1200,
        "pswpout": 3400,
        "available_kb": 12582912,
        "cpu_pressure": 2.25,
        "load": 1.25,
        "cpu": {"usage_usec": 900000000, "system_usec": 200000000},
        "groups": {"ticket-7": 5000, "workflow-tick": 777},
        "sessions": {"ticket-7": "idle"},
        "asking": {"ticket-7": NOW - 600},
    }
    assert machine.stat().st_mode & 0o077 == 0
    assert not [call for call in workflow.calls("gh ")]


def test_a_sample_without_proc_figures_still_records_the_sessions(workflow: Workflow) -> None:
    done = workflow.run_script("workflow-metrics", "--sample", env=AT)

    assert done.returncode == 0, done.stderr
    machine = workflow.root / "home" / ".local" / "state" / "workflow" / "machine.jsonl"
    [sample] = [json.loads(line) for line in machine.read_text().splitlines()]
    assert sample["at"] == NOW
    assert sample["pswpout"] is None
    assert sample["cpu"] is None
    assert sample["sessions"] == {}


def _daily(workflow: Workflow, now: int = NOW) -> subprocess.CompletedProcess[str]:
    return workflow.run_script(
        "workflow-metrics", "--daily", env={"WORKFLOW_NOW": str(now), "TZ": "UTC"}
    )


def test_the_daily_run_writes_the_report_and_the_findings_for_the_wake(workflow: Workflow) -> None:
    _github(workflow)
    _notes(workflow)

    done = _daily(workflow)

    assert done.returncode == 0, done.stderr
    report = workflow.repo / ".local" / "analyses" / "metrics-2026-10-24.md"
    text = report.read_text()
    assert text.startswith("# Workflow metrics, 2026-10-24\n")
    assert "| merge line, median | 12.0 h | 7.0 h |" in text
    assert "## Findings\n\n- merge line 73% of the total time" in text
    assert "- main pending or red in 50% of 10 check-ins over 7 days\n" in text
    assert "Not found: " in text
    [pending] = (workflow.repo / ".local" / "workflow" / "metrics-findings.pending").iterdir()
    noted = json.loads(pending.read_text())
    assert noted["report"] == ".local/analyses/metrics-2026-10-24.md"
    assert len(noted["lines"]) == 8
    assert noted["lines"][0].startswith("merge line 73% of the total time")


def test_the_daily_run_runs_once_a_day(workflow: Workflow) -> None:
    _github(workflow)
    _notes(workflow)
    assert _daily(workflow).returncode == 0
    calls = len(workflow.calls("gh "))
    pending = workflow.repo / ".local" / "workflow" / "metrics-findings.pending"
    for path in pending.iterdir():
        path.unlink()

    done = _daily(workflow, NOW + 4 * HOUR)

    assert done.returncode == 0, done.stderr
    assert len(workflow.calls("gh ")) == calls
    assert not list(pending.iterdir())


def test_a_daily_run_that_failed_is_tried_again_after_half_an_hour(workflow: Workflow) -> None:
    workflow.fake("gh", "#!/bin/bash\necho \"gh $*\" >> '@STATE@/calls.log'\nexit 1\n")
    _notes(workflow)

    failed = _daily(workflow)
    again_soon = _daily(workflow, NOW + 20 * 60)
    tries = len(workflow.calls("gh "))
    _github(workflow)
    later = _daily(workflow, NOW + 40 * 60)

    assert failed.returncode == 1
    assert again_soon.returncode == 0
    assert tries == 1
    assert later.returncode == 0, later.stderr
    assert (workflow.repo / ".local" / "analyses" / "metrics-2026-10-24.md").exists()


def test_a_second_daily_run_leaves_while_the_first_runs(workflow: Workflow) -> None:
    import fcntl

    _github(workflow)
    lock = workflow.root / "home" / ".local" / "state" / "workflow" / "metrics.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        done = _daily(workflow)

    assert done.returncode == 0, done.stderr
    assert "still running" in done.stdout
    assert not (workflow.repo / ".local" / "analyses").exists()


def test_a_day_without_findings_leaves_nothing_for_the_wake(workflow: Workflow) -> None:
    _empty_github(workflow)

    done = _daily(workflow)

    assert done.returncode == 0, done.stderr
    report = workflow.repo / ".local" / "analyses" / "metrics-2026-10-24.md"
    assert "## Findings\n\nNone.\n" in report.read_text()
    assert not (workflow.repo / ".local" / "workflow" / "metrics-findings.pending").exists()


def test_the_report_prints_without_writing_anything(workflow: Workflow) -> None:
    _github(workflow)
    _notes(workflow)

    done = workflow.run_script("workflow-metrics", env=AT)

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("# Workflow metrics, 2026-10-24\n")
    assert not (workflow.repo / ".local" / "analyses").exists()
    assert not (workflow.repo / ".local" / "workflow" / "metrics-findings.pending").exists()


def test_the_wake_carries_the_findings_once(workflow: Workflow) -> None:
    pending = workflow.repo / ".local" / "workflow" / "metrics-findings.pending"
    pending.mkdir(parents=True)
    (pending / "2026-10-24.json").write_text(
        json.dumps(
            {
                "report": ".local/analyses/metrics-2026-10-24.md",
                "lines": ["cancelled runs 20% over 7 days (2 of 10 runs: 103, 301)", "main red"],
            }
        )
    )
    workflow.orchestrator("idle")
    night = {"WORKFLOW_NOW": "1791932400", "TZ": "UTC"}  # 13 October, 23:00

    done = workflow.run_script("wake", env=night)
    again = workflow.run_script("wake", env={"WORKFLOW_NOW": "1791934200", "TZ": "UTC"})

    assert done.returncode == 0, done.stderr
    assert again.returncode == 0, again.stderr
    [prompt] = workflow.sent()
    assert (
        "Workflow metrics findings (report .local/analyses/metrics-2026-10-24.md):"
        " cancelled runs 20% over 7 days (2 of 10 runs: 103, 301); main red."
    ) in prompt
    assert not list(pending.iterdir())


def test_findings_alone_are_a_change_for_the_check_in_but_wait_for_the_next_look(
    workflow: Workflow,
) -> None:
    workflow.orchestrator("idle")
    first = {"WORKFLOW_NOW": "1791932400", "TZ": "UTC"}  # 13 October, 23:00
    assert workflow.run_script("wake", env=first).returncode == 0
    pending = workflow.repo / ".local" / "workflow" / "metrics-findings.pending"
    pending.mkdir(parents=True)
    (pending / "2026-10-14.json").write_text(
        json.dumps({"report": ".local/analyses/metrics-2026-10-14.md", "lines": ["main red"]})
    )
    by_day = {"WORKFLOW_NOW": str(1791932400 + 9 * HOUR), "TZ": "UTC"}  # 08:00

    day = workflow.run_script("wake", env=by_day)
    # The next night, 20:00.
    night = workflow.run_script(
        "wake", env={"WORKFLOW_NOW": str(1791932400 + 21 * HOUR), "TZ": "UTC"}
    )

    assert day.returncode == 0, day.stderr
    assert night.returncode == 0, night.stderr
    assert len(workflow.sent()) == 2
    assert (
        "Workflow metrics findings (report .local/analyses/metrics-2026-10-14.md): main red."
        in (workflow.sent()[-1])
    )


def _github_beside_the_tick(workflow: Workflow) -> None:
    """The tick's own questions go to the harness's `gh`; the metrics' to the fixtures."""
    (workflow.bin / "gh").rename(workflow.bin / "gh-tick")
    _github(workflow)
    (workflow.bin / "gh").rename(workflow.bin / "gh-metrics")
    workflow.fake(
        "gh",
        '#!/bin/bash\ncase "$*" in\n'
        '  "api graphql "* | *actions/runs*) exec "$(dirname "$0")/gh-metrics" "$@" ;;\n'
        'esac\nexec "$(dirname "$0")/gh-tick" "$@"\n',
    )


def _wait_for(path: Path) -> bool:
    for _ in range(200):
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def test_every_tick_samples_the_machine(workflow: Workflow) -> None:
    _proc(workflow)

    done = workflow.run_script("tick", env={**AT, "WORKFLOW_NOW": str(NOW - HOUR)})

    assert done.returncode == 0, done.stderr
    machine = workflow.root / "home" / ".local" / "state" / "workflow" / "machine.jsonl"
    [sample] = [json.loads(line) for line in machine.read_text().splitlines()]
    assert sample["at"] == NOW - HOUR
    assert sample["load"] == 1.25


def test_the_tick_starts_the_daily_run_from_0750_utc(workflow: Workflow) -> None:
    _github_beside_the_tick(workflow)
    _notes(workflow)
    report = workflow.repo / ".local" / "analyses" / "metrics-2026-10-24.md"
    daily = {"WORKFLOW_METRICS_DAILY_AT": "07:50", "TZ": "UTC"}

    before = workflow.run_script("tick", env={**daily, "WORKFLOW_NOW": str(NOW - 600)})
    time.sleep(1)
    assert before.returncode == 0, before.stderr
    assert not report.exists()

    done = workflow.run_script("tick", env={**daily, "WORKFLOW_NOW": str(NOW + 60)})

    assert done.returncode == 0, done.stderr
    assert _wait_for(report)
    pending = workflow.repo / ".local" / "workflow" / "metrics-findings.pending"
    assert _wait_for(pending / "2026-10-24.json")
    workflow.orchestrator("idle")
    night = {"WORKFLOW_NOW": str(NOW + 13 * HOUR), "TZ": "UTC"}  # 20:50
    assert workflow.run_script("wake", env=night).returncode == 0
    assert (
        "Workflow metrics findings (report .local/analyses/metrics-2026-10-24.md): merge line"
        in (workflow.sent()[-1])
    )


def test_the_tick_leaves_the_daily_run_alone_when_it_is_switched_off(workflow: Workflow) -> None:
    _github_beside_the_tick(workflow)

    done = workflow.run_script("tick", env={**AT, "WORKFLOW_NOW": str(NOW + 60)})
    time.sleep(1)

    assert done.returncode == 0, done.stderr
    assert not (workflow.repo / ".local" / "analyses").exists()
    assert not [call for call in workflow.calls("gh ") if "graphql" in call]
