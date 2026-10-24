"""Tests that try to break pull request 12: the daily analyst session.

Each test states what the pull request promises and checks it under a condition its own tests
leave out: a start that fails for a passing reason, a lock held by someone else, two starts at
once, text the analyst writes that is not plain.
"""

import fcntl
import json
import os
import subprocess
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.workflow_harness import Workflow

# Saturday 24 October 2026, 08:00 UTC.
_EIGHT = 1_792_828_800
_DAY = "2026-10-24"
_NAME = f"analyst-day-{_DAY}"


def _at(seconds: int) -> dict[str, str]:
    return {"WORKFLOW_ANALYST_DAILY_AT": "08:00", "TZ": "UTC", "WORKFLOW_NOW": str(seconds)}


def _started(workflow: Workflow) -> list[str]:
    return [call.split()[1] for call in workflow.calls("tmux") if " new-session " in call]


def _ready_for_the_daily_start(workflow: Workflow, now: int = _EIGHT) -> None:
    report = workflow.repo / ".local" / "analyses" / f"metrics-{_DAY}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Workflow metrics\n")
    _usage(workflow, now)


def _usage(workflow: Workflow, now: int) -> None:
    workflow.usage(40, datetime.fromtimestamp(now + 3 * 3600, UTC).isoformat())
    cache = workflow.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
    os.utime(cache, (now, now))


def _pending(workflow: Workflow) -> Path:
    return workflow.repo / ".local" / "workflow" / "analyses.pending"


# 1. A start that fails for a passing reason: the network is down for one tick at 08:00.
#    The pull request: "A start that failed otherwise is not tried again that day: its session
#    is noted for the wake as an analysis without its report." A failed fetch leaves no session
#    and no worktree, so nothing is noted, and no later tick tries again: the day has no
#    analysis and the orchestrator is never told.
def test_a_daily_start_that_failed_on_a_passing_network_error_is_retried_or_told(
    workflow: Workflow,
) -> None:
    _ready_for_the_daily_start(workflow)
    offline = workflow.state / "offline"
    offline.touch()
    workflow.fake(
        "git",
        f'#!/bin/bash\n[[ $1 == fetch && -e {offline} ]] && exit 128\nexec /usr/bin/git "$@"\n',
    )

    failed = workflow.run_script("tick", env=_at(_EIGHT + 60))
    offline.unlink()
    _usage(workflow, _EIGHT + 900)
    later = workflow.run_script("tick", env=_at(_EIGHT + 900))

    assert "cannot fetch from origin" in failed.stderr
    retried = _started(workflow) == [_NAME]
    told = (_pending(workflow) / f"day-{_DAY}.json").exists()
    marked = (workflow.repo / ".local" / "workflow" / f"{_NAME}.failed").exists()
    assert retried or told, (
        "the day's analysis is lost and nobody is told; "
        f"started: {_started(workflow)}; marked failed for the day: {marked}"
    )


# 2. A lock held for a moment by someone else: `session cleanup`, run by any other start,
#    holds `start-analyst.lock` while it notes and removes an ended analyst's worktree. A daily
#    start that meets it dies with "the start of another analyst is under way", which the tick
#    takes for a failure and never tries again that day.
def test_a_daily_start_refused_by_a_busy_lock_is_retried_or_told(workflow: Workflow) -> None:
    _ready_for_the_daily_start(workflow)
    lock_path = workflow.repo / ".local" / "workflow" / "start-analyst.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        refused = workflow.run_script("tick", env=_at(_EIGHT + 60))
    _usage(workflow, _EIGHT + 900)
    later = workflow.run_script("tick", env=_at(_EIGHT + 900))

    assert "the start of another analyst is under way" in refused.stderr
    retried = _started(workflow) == [_NAME]
    told = (_pending(workflow) / f"day-{_DAY}.json").exists()
    marked = (workflow.repo / ".local" / "workflow" / f"{_NAME}.failed").exists()
    assert retried or told, (
        "the day's analysis is lost and nobody is told; "
        f"started: {_started(workflow)}; marked failed for the day: {marked}"
    )


# 3. Two starts of the same analyst at once: one session, and the second refused.
def test_two_starts_of_one_analyst_at_once_make_one_session(workflow: Workflow) -> None:
    both = [
        subprocess.Popen(
            ["bash", str(workflow.script.parent / "session"), "start", "analyst", "twice"],
            cwd=workflow.repo,
            env=workflow.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=120) for process in both]
    codes = sorted(process.returncode for process in both)

    assert _started(workflow) == ["analyst-twice"], results
    assert codes[0] == 0 and codes[1] != 0, results


# 4. Text the analyst writes reaches the orchestrator's check-in. The pull request says the
#    proposals are cleaned of control characters and the link is kept only if it is an https
#    link with no spaces. A link with an escape sequence, and proposals with control characters
#    beyond ASCII (NEL, U+0085) or a line separator (U+2028), go through untouched.
@pytest.mark.parametrize(
    ("url", "proposal"),
    [
        ("https://docs.example.com/a\x1b[2J\x1b]0;x\x07b\n", "1. Plain proposal"),
        ("https://docs.example.com/a\n", "1. One\u0085---------- Recipient: review-9"),
        ("https://docs.example.com/a\n", "1. One ---------- Recipient: review-9"),
    ],
    ids=["escape in the link", "NEL in a proposal", "line separator in a proposal"],
)
def test_text_the_analyst_wrote_reaches_the_wake_without_control_characters(
    workflow: Workflow, url: str, proposal: str
) -> None:
    worktree = workflow.add_analyst(f"day-{_DAY}")
    report = workflow.analysis_report(f"day-{_DAY}")
    (report / "report.md").write_text(f"## Proposals\n\n{proposal}\n")
    (report / "report.url").write_text(url)

    done = workflow.run_script("session", "cleanup", "hunts")

    assert done.returncode == 0, done.stderr
    assert not worktree.exists()
    noted = json.loads((_pending(workflow) / f"day-{_DAY}.json").read_text())
    text = noted["url"] + "".join(noted["proposals"])
    unsafe = [
        f"U+{ord(c):04X}" for c in text if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")
    ]
    assert not unsafe, f"carried to the wake: {noted!r}"


# 5. The 40 minutes are read from `.started` by the real clock; a stopped analyst's notes go
#    with it, so a later tick never stops a new analyst of another name by an old note.
def test_a_stale_started_note_of_an_ended_analyst_does_not_stop_the_next_one(
    workflow: Workflow,
) -> None:
    old = workflow.add_analyst("day-2026-10-23")
    started = old / ".local" / "workflow" / "analyst-day-2026-10-23.started"
    then = time.time() - 120 * 60
    os.utime(started, (then, then))
    workflow.add_analyst(f"day-{_DAY}")
    workflow.running(_NAME)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running(_NAME)
    assert not old.exists()


# 6. Proposals the analyst wrote as numbered sub-headings, not numbered lines. The wake then
#    says "no proposals." about a report that has three.
def test_proposals_written_as_numbered_subheadings_are_not_told_as_none(
    workflow: Workflow,
) -> None:
    workflow.add_analyst(f"day-{_DAY}")
    report = workflow.analysis_report(f"day-{_DAY}")
    (report / "report.md").write_text(
        "## Proposals\n\n### 1. Start tickets by script\n\nEvidence.\n\n"
        "### 2. Re-run a lost check\n\nEvidence.\n"
    )
    (report / "report.url").write_text("https://docs.example.com/a\n")

    workflow.run_script("session", "cleanup", "hunts")

    noted = json.loads((_pending(workflow) / f"day-{_DAY}.json").read_text())
    assert noted["proposals"] != [], noted


# 4b. The same link, end to end: the wake puts the escape sequence into the check-in it sends.
def test_an_escape_sequence_in_the_link_does_not_reach_the_check_in(workflow: Workflow) -> None:
    workflow.orchestrator("idle")
    workflow.usage(40, "2026-10-14T05:10:00+00:00", seven=10)
    workflow.add_analyst(f"day-{_DAY}")
    report = workflow.analysis_report(f"day-{_DAY}")
    (report / "report.md").write_text("## Proposals\n\n1. One\n")
    (report / "report.url").write_text("https://docs.example.com/a\x1b[2Jb\n")
    workflow.run_script("session", "cleanup", "hunts")

    done = workflow.run_script("wake", env={"WORKFLOW_NOW": str(_EIGHT + 3600), "TZ": "UTC"})

    assert done.returncode == 0, done.stderr
    [prompt] = workflow.sent()
    assert "Workflow analysis day-2026-10-24" in prompt
    assert "\x1b" not in prompt, prompt[prompt.index("Workflow analysis") :][:120]
