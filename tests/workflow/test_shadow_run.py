"""`shipline tick --shadow` takes the tick's decisions against a repository and changes nothing:
it writes each one to `.local/shipline/shadow.log`, in the words the real tick logs it in, and
`tools/compare-shadow` lists every decision a shadow log and the real tick's logs differ on."""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from workflow.test_lost_check import _lose, _run, _waiting_reviewer
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import Workflow, install_shipline

github = github_fixture

_ROOT = Path(__file__).resolve().parents[2]
_COMPARE = _ROOT / "tools" / "compare-shadow"
_MB = 1024 * 1024
# 2026-09-21 17:00:00 UTC, the clock of every tick here.
_NOW = 1_790_010_000
_CLOCK = {"WORKFLOW_NOW": str(_NOW), "TZ": "UTC"}
# Reviewer 10's session started then; its own round was published after it.
_STARTED = 1_790_000_000
_BOX = "\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n"
_FINISHED = "● The audit left nothing open.\n\n  Audit finished.\n\n> \n"
_WAITING = "● The checks are still running; I am waiting for them." + _BOX
_LIMIT = (
    "> Implement pull request 12\n"
    "  ⎿  You've hit your session limit · resets Sep 21, 2026, 4pm (UTC)\n" + _BOX
)

# The stand-ins of a shadow tick refuse what would change something. A refused call is written
# down; the test then finds none.
_READS_ONLY_GH = """#!/bin/bash
reads='^(pr (list|view|diff|checks)|label list|run (list|view)|repo view) '
if [[ ! "$1 $2 " =~ $reads ]]; then
  method=GET
  for word in "$@"; do
    case $word in -X | --method) next=1; continue ;; esac
    [[ -z ${next:-} ]] || { method=$word; next=""; }
    case $word in -f | -F | --field | --raw-field | --input) method=POST ;; esac
  done
  if [[ $1 != api || $method != GET ]]; then
    echo "gh $*" >> "@STATE@/refused.log"
    exit 1
  fi
fi
exec "@BIN@/gh.reads" "$@"
"""
_READS_ONLY_TMUX = """#!/bin/bash
case $3 in
  has-session | capture-pane | list-panes | display-message) exec "@BIN@/tmux.reads" "$@" ;;
esac
echo "tmux $*" >> "@STATE@/refused.log"
exit 1
"""
_REFUSED = """#!/bin/bash
echo "@TOOL@ $*" >> "@STATE@/refused.log"
exit 1
"""
# `claude` may list the sessions; a message is sent by `claude -p`.
_LISTING_CLAUDE = """#!/bin/bash
[ "$*" = "agents --json" ] && exec "@BIN@/claude.reads" "$@"
echo "claude $*" >> "@STATE@/refused.log"
exit 1
"""


def _written(path: Path, at: float, text: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (at, at))


def _scenario(workflow: Workflow) -> str:
    """One tick's worth of decisions, each by its own pull request. Returns reviewer 10's head.

    7, a labelled draft, gets a worker, and 8, ready, a reviewer. The audit of 9 said it
    finished and is stopped, and its draft gets a worker in the same tick. A hunt is over its
    time. Reviewer 10 waits for checks that finished. Worker 11 is idle and holds 400 MB. The
    usage limit stopped worker 12 until an hour ago. Worker 13 has waited for its own job for
    51 minutes. The orchestrator is open and idle.
    """
    workflow.add_pull_request(7)
    workflow.add_pull_request(8, draft=False)

    workflow.add_worktree(9)
    workflow.running("audit-9")
    _written(workflow.notes(9) / "audit-9.idle", _NOW)
    workflow.screen(_FINISHED, "audit-9")

    hunt = workflow.add_hunt("x")
    workflow.running("hunt-x")
    _written(hunt / ".local" / "workflow" / "hunt-x.started", time.time() - 2 * 3600)

    workflow.add_worktree(10, draft=False)
    workflow.running("review-10")
    _written(workflow.notes(10) / "review-10.started", _STARTED)
    _written(workflow.notes(10) / "review-10.checks-wait", _STARTED)
    workflow.screen(_WAITING, "review-10")
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(10))
    (workflow.state / "reviews-10.json").write_text(
        json.dumps(
            [
                {
                    "body": "<!-- workflow-review round=1 result=passed -->",
                    "commit_id": head,
                    "submitted_at": "2026-09-21T15:00:00Z",
                }
            ]
        )
    )
    (workflow.state / "rollup-10.json").write_text(
        json.dumps(
            [
                {"__typename": "CheckRun", "name": name, "status": "COMPLETED"}
                | {"conclusion": "SUCCESS"}
                for name in ("CI result", "Docker images: what to run", "E2E")
            ]
        )
    )

    workflow.add_worktree(11)
    workflow.running("ticket-11", held=400 * _MB)
    _written(workflow.notes(11) / "ticket-11.idle", _NOW)

    workflow.add_worktree(12)
    workflow.running("ticket-12")
    workflow.screen(_LIMIT, "ticket-12")

    workflow.add_worktree(13)
    workflow.running("ticket-13")
    workflow.screen("● Waiting for the full Python run in the background." + _BOX, "ticket-13")
    _written(workflow.notes(13) / "ticket-13.job-wait", _NOW - 51 * 60)

    workflow.orchestrator()
    return head


def _shadow_log(workflow: Workflow) -> Path:
    return workflow.repo / ".local" / "shipline" / "shadow.log"


def _decisions(log: Path) -> list[str]:
    """The decisions of a shadow log, without their time: its tick lines, the run marks and the
    train status left out."""
    lines = []
    for line in log.read_text().splitlines():
        stamp, _, text = line.partition(" ")
        if stamp.isdigit() and text != "shadow tick" and not text.startswith("train status: "):
            lines.append(text)
    return lines


def _strict(workflow: Workflow) -> None:
    """Stand-ins that answer reads as before and refuse everything else."""
    for tool in ("gh", "tmux", "claude"):
        os.replace(workflow.bin / tool, workflow.bin / f"{tool}.reads")
    reads = {"gh": _READS_ONLY_GH, "tmux": _READS_ONLY_TMUX, "claude": _LISTING_CLAUDE}
    for tool, text in reads.items():
        workflow.fake(tool, text.replace("@BIN@", str(workflow.bin)))
    for tool in ("curl", "systemd-run", "systemctl", "codex", "worktree-setup"):
        workflow.fake(tool, _REFUSED)


def _digest(*roots: Path, leave_out: tuple[Path, ...] = ()) -> dict[str, str]:
    """Every file below the roots, and every folder, with what it holds."""
    found: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if any(path == out or out in path.parents for out in leave_out):
                continue
            if path.is_symlink():
                found[str(path)] = "link " + os.readlink(path)
            elif path.is_file():
                found[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif path.is_dir():
                found[str(path)] = "folder"
    return found


def _journal(*outputs: str, at: int = _NOW) -> str:
    """What `journalctl -o short-unix` shows of a tick's output."""
    lines = [line for output in outputs for line in output.splitlines() if line.strip()]
    return "".join(f"{at}.250000 example-host workflow-tick[42]: {line}\n" for line in lines)


def _compare(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(_COMPARE), *args],
        cwd=cwd,
        env={**os.environ, "TZ": "UTC"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# The shadow tick.


def test_a_shadow_tick_changes_nothing(workflow: Workflow) -> None:
    _scenario(workflow)
    home = install_shipline(workflow)
    _strict(workflow)
    origin = workflow.git("ls-remote", "origin")
    worktrees = workflow.git("worktree", "list", "--porcelain")
    home_state = workflow.root / "home" / ".local" / "state" / "workflow"
    # The shared GitHub budget counts the shadow's own reads, which are real calls to GitHub.
    budget = home_state / "github-budget"
    budget.mkdir(parents=True)
    roots = (workflow.repo, workflow.cgroup_root, workflow.root / "home", workflow.root / "tmp")
    leave_out = (workflow.repo / ".git", _shadow_log(workflow).parent, budget)
    before = _digest(*roots, leave_out=leave_out)

    done = workflow.shipline("tick", "--shadow", env=_CLOCK)

    assert done.returncode == 0, done.stderr
    assert not (workflow.state / "refused.log").exists(), (
        workflow.state / "refused.log"
    ).read_text()
    assert _digest(*roots, leave_out=leave_out) == before
    assert workflow.git("ls-remote", "origin") == origin
    assert workflow.git("worktree", "list", "--porcelain") == worktrees
    assert all(workflow.is_running(name) for name in ("audit-9", "hunt-x", "ticket-11"))
    # What it would have done is in the shadow log, and nowhere else.
    assert "review-10 checks finished on " in _shadow_log(workflow).read_text()
    assert done.stdout == ""
    assert home.is_dir()


def test_a_shadow_tick_logs_the_decisions_the_real_tick_takes(workflow: Workflow) -> None:
    head = _scenario(workflow)

    shadow = workflow.run_script("tick", "--shadow", env=_CLOCK)
    real = workflow.run_script("tick", env=_CLOCK)

    assert shadow.returncode == 0, shadow.stderr
    assert real.returncode == 0, real.stderr
    decisions = _decisions(_shadow_log(workflow))
    assert decisions == [
        "hunt-x is over its time",
        "hunt-x stopped",
        "audit-9 stopped",
        "ticket-11 squeezed",
        "ticket-12 was sent its instruction again",
        f"review-10 checks finished on {head}; reviewer nudged",
        "ticket-13 idle 51 min waiting for its own job or turn; status check sent",
        "ticket-7 started",
        "review-8 started",
        "ticket-9 started",
    ]
    log = _shadow_log(workflow).read_text()
    assert "  the tick sent its instruction again to: ticket-12\n" in log
    assert "  told Check-in: Wake check-in, 17:00, after a reset." in log
    # The real tick did each of them.
    assert workflow.is_running("ticket-7") and workflow.is_running("review-8")
    assert not workflow.is_running("audit-9") and not workflow.is_running("hunt-x")
    journal = workflow.root / "journal"
    journal.write_text(_journal(real.stdout, real.stderr))

    compared = _compare(
        "--shadow",
        str(_shadow_log(workflow)),
        "--wake",
        str(workflow.wake_log),
        "--journal",
        str(journal),
    )

    assert compared.returncode == 0, compared.stdout + compared.stderr
    assert compared.stdout.splitlines()[-1] == (
        "11 decisions compared from 2026-09-21 17:00:00 to 2026-09-21 17:00:00: none differs"
    )


def test_a_lost_check_is_noted_for_the_shadow_s_wake_and_not_in_the_live_state(
    github: GitHub,
) -> None:
    # A reviewer waits for its checks before it joined the line, and GitHub still lists one as
    # running an hour after its run ended: the tick notes it for the wake, once per head.
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)
    workflow.orchestrator()
    state = workflow.repo / ".local" / "workflow"
    told = "lost check: CI: server tests of run 20 on pull request 7"
    before = _digest(state)

    shadow = workflow.run_script("tick", "--shadow")

    assert shadow.returncode == 0, shadow.stdout + shadow.stderr
    assert _digest(state) == before
    log = _shadow_log(workflow).read_text()
    assert "review-7 lost check on " in log
    assert f"\n  {told}\n" in log
    once = log.count(told)

    real = workflow.run_script("tick")

    assert real.returncode == 0, real.stdout + real.stderr
    assert told in workflow.wake_log.read_text()
    # A nudge to try again: the real tick nudges, and its wake tells no lost check it told.
    for mark in state.glob("review-7.lost-nudged-*"):
        mark.unlink()
    again = workflow.run_script("tick", "--shadow")

    assert again.returncode == 0, again.stdout + again.stderr
    log = _shadow_log(workflow).read_text()
    assert log.count("review-7 lost check on ") == 2
    assert log.count(told) == once


@pytest.mark.parametrize(("five", "starts"), [(40, True), (90, False)], ids=["room", "held"])
def test_the_daily_analyst_starts_in_a_shadow_tick_as_in_the_real_one(
    workflow: Workflow, five: int, starts: bool
) -> None:
    eight = 1_792_828_800  # 2026-10-24 08:00 UTC
    clock = {"WORKFLOW_ANALYST_DAILY_AT": "08:00", "TZ": "UTC", "WORKFLOW_NOW": str(eight + 60)}
    report = workflow.repo / ".local" / "analyses" / "metrics-2026-10-24.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Workflow metrics\n")
    workflow.usage(five, datetime.fromtimestamp(eight + 3 * 3600, UTC).isoformat())
    cache = workflow.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
    os.utime(cache, (eight, eight))
    # Main's report as the tick before left it: unchanged, it wakes nobody at this hour.
    folder = workflow.repo / ".local" / "workflow"
    folder.mkdir(parents=True)
    (folder / "main-status").write_text(
        "main: unknown, list indices must be integers or slices, not str\n"
    )

    shadow = workflow.run_script("tick", "--shadow", env=clock)
    assert not (workflow.repo / ".local" / "analyses" / "day-2026-10-24").exists()
    real = workflow.run_script("tick", env=clock)

    assert shadow.returncode == real.returncode == 0, shadow.stderr + real.stderr
    assert workflow.is_running("analyst-day-2026-10-24") is starts
    assert _decisions(_shadow_log(workflow)) == [
        "analyst-day-2026-10-24 started"
        if starts
        else "the daily analyst waits: the 5-hour usage window is 90% used"
    ]
    journal = workflow.root / "journal"
    journal.write_text(_journal(real.stdout, real.stderr, at=eight + 60))
    compared = _compare(
        *("--shadow", str(_shadow_log(workflow)), "--wake", str(workflow.wake_log)),
        *("--journal", str(journal)),
    )
    assert compared.returncode == 0, compared.stdout


def test_a_start_that_would_wait_for_memory_is_logged_and_holds_the_later_ones(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    workflow.pressure_file.write_text(
        "some avg10=60.00 avg60=60.00 avg300=60.00 total=22247292\n"
        "full avg10=60.00 avg60=60.00 avg300=60.00 total=21634252\n"
    )

    shadow = workflow.run_script("tick", "--shadow", env=_CLOCK)
    real = workflow.run_script("tick", env=_CLOCK)

    # A start that waits fails the tick, which tries it again; so does the shadow tick.
    assert shadow.returncode == real.returncode == 1, shadow.stderr
    held = [line for line in real.stderr.splitlines() if "the next tick tries" in line]
    assert held and _decisions(_shadow_log(workflow)) == held
    assert not workflow.is_running("ticket-7") and not workflow.is_running("ticket-8")


def _handed_back(workflow: Workflow, sent_back: int) -> None:
    """Pull request 6, ready, whose merge the driver handed back to a fresh reviewer."""
    workflow.add_pull_request(6, draft=False)
    folder = workflow.repo / ".local" / "workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-review-6").write_text('{"reason": "conflicts with main"}\n')
    rounds = [
        {"body": f"<!-- workflow-review round={round} result=sent-back -->"}
        for round in range(1, sent_back + 1)
    ]
    (workflow.state / "reviews-6.json").write_text(json.dumps(rounds))


@pytest.mark.parametrize(
    ("sent_back", "allowed", "started"),
    [(1, False, True), (2, False, False), (2, True, True)],
    ids=["second round", "third round unallowed", "third round allowed"],
)
def test_a_hand_back_s_reviewer_starts_as_the_merge_driver_would_start_it(
    workflow: Workflow, sent_back: int, allowed: bool, started: bool
) -> None:
    _handed_back(workflow, sent_back)
    if allowed:
        (workflow.repo / ".local" / "workflow" / "review-6.allow-round").write_text("3\n")
    workflow.add_pull_request(7)

    done = workflow.run_script("tick", "--shadow", env=_CLOCK)

    assert done.returncode == 0, done.stderr
    assert _decisions(_shadow_log(workflow)) == [
        *(["review-6 started"] if started else []),
        "ticket-7 started",
    ]


def test_a_hand_back_whose_pull_request_has_a_session_gets_no_reviewer(
    workflow: Workflow,
) -> None:
    _handed_back(workflow, 0)
    workflow.running("ticket-6")

    done = workflow.run_script("tick", "--shadow", env=_CLOCK)

    assert done.returncode == 0, done.stderr
    assert _decisions(_shadow_log(workflow)) == []


def test_the_sessions_of_a_pull_request_closed_without_merging_are_gone_as_cleanup_leaves_them(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(9, state="CLOSED")
    workflow.running("ticket-9")
    workflow.screen("● Waiting for the full Python run in the background." + _BOX, "ticket-9")
    _written(workflow.notes(9) / "ticket-9.job-wait", _NOW - 51 * 60)

    shadow = workflow.run_script("tick", "--shadow", env=_CLOCK)
    real = workflow.run_script("tick", env=_CLOCK)

    assert shadow.returncode == 0, shadow.stderr
    assert real.returncode == 0, real.stderr
    assert not workflow.is_running("ticket-9")
    assert _decisions(_shadow_log(workflow)) == []
    assert not workflow.wake_log.exists() or "ticket-9" not in workflow.wake_log.read_text()


# The real tick reads its clock later than the shadow tick: what comes due in between.


def _settled(workflow: Workflow) -> None:
    """Main's report as the installation's ticks before left it: unchanged, it wakes nobody."""
    folder = workflow.repo / ".local" / "workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main-status").write_text(
        "main: unknown, list indices must be integers or slices, not str\n"
    )


def _job_waiting(workflow: Workflow, since: int) -> None:
    workflow.add_worktree(13)
    workflow.running("ticket-13")
    workflow.screen("● Waiting for the full Python run in the background." + _BOX, "ticket-13")
    _written(workflow.notes(13) / "ticket-13.job-wait", since)


def _ticks(workflow: Workflow, *moments: int, late: int = 20, env: dict[str, str] | None = None):
    """A shadow tick at each moment, and the real tick `late` seconds after it."""
    for at in moments:
        clock = {"TZ": "UTC", **(env or {})}
        shadow = workflow.run_script("tick", "--shadow", env={**clock, "WORKFLOW_NOW": str(at)})
        real = workflow.run_script("tick", env={**clock, "WORKFLOW_NOW": str(at + late)})
        assert shadow.returncode == real.returncode == 0, shadow.stderr + real.stderr


def test_a_status_check_due_between_the_shadow_and_the_real_tick_is_a_timing_difference(
    workflow: Workflow,
) -> None:
    # 49 min 50 s when the shadow tick reads the clock, 50 min 10 s when the real one does.
    _settled(workflow)
    _job_waiting(workflow, _NOW - 50 * 60 + 10)

    _ticks(workflow, _NOW, _NOW + 180)

    line = "ticket-13 idle 50 min waiting for its own job or turn; status check sent"
    assert line in workflow.wake_log.read_text()
    assert f"{_NOW} due within 180 s: {line}" in _shadow_log(workflow).read_text()
    logs = ("--shadow", str(_shadow_log(workflow)), "--wake", str(workflow.wake_log))
    compared = _compare(*logs)
    listed = _compare(*logs, "--timing")
    assert compared.returncode == 0, compared.stdout
    assert "ticket-13" not in compared.stdout
    assert compared.stdout.splitlines()[-1].endswith(
        ": none differs; 1 came due between a shadow tick and the real one"
    )
    assert listed.returncode == 0, listed.stdout
    assert listed.stdout.splitlines()[-3:-1] == [
        "2026-09-21 17:00:20 ticket-13, timing",
        f"  real:   {line.replace('50 min', 'N min')}",
    ]


def test_a_decision_only_the_real_tick_took_that_its_shadow_did_not_note_is_still_a_difference(
    tmp_path: Path,
) -> None:
    line = "ticket-13 idle 50 min waiting for its own job or turn; status check sent"
    shadow = (
        f"{_NOW} shadow tick\n"
        f"{_NOW} due within 180 s: {line.replace('ticket-13', 'ticket-14')}\n"
        f"{_NOW + 180} shadow tick\n"
        f"{_NOW + 180} due within 180 s: {line}\n"
    )
    # The real tick after the first shadow tick sent it: the note of the second does not count.
    wake = f"{_NOW + 90} {line}\n"

    done = _compare(*_logs(tmp_path, shadow, wake, "\n"))

    assert done.returncode == 1
    assert done.stdout.splitlines()[:3] == [
        "2026-09-21 17:01:30 ticket-13",
        "  shadow: (nothing)",
        "  real:   ticket-13 idle N min waiting for its own job or turn; status check sent",
    ]


def test_a_night_check_in_due_between_the_shadow_and_the_real_tick_is_no_difference(
    workflow: Workflow,
) -> None:
    night = _NOW + 6 * 3600  # 23:00 UTC: the wake looks every 20 minutes
    workflow.add_worktree(11)
    workflow.running("ticket-11")
    workflow.screen("● Working on the change." + _BOX, "ticket-11")
    workflow.orchestrator()
    settled = workflow.run_script("tick", env={"TZ": "UTC", "WORKFLOW_NOW": str(night - 900)})
    assert settled.returncode == 0, settled.stderr
    # The wake last looked 19 min 25 s before the shadow tick reads the clock.
    looked = workflow.root / "home" / ".local" / "state" / "workflow" / "wake.looked"
    looked.write_text(f"{night - 1165}\n")

    _ticks(workflow, night, night + 180)

    assert "  due within 180 s: every 20 minutes\n" in _shadow_log(workflow).read_text()
    compared = _compare("--shadow", str(_shadow_log(workflow)), "--wake", str(workflow.wake_log))
    assert compared.returncode == 0, compared.stdout
    assert compared.stdout.splitlines()[-1].endswith(
        "came due between a shadow tick and the real one"
    )


def test_a_reset_due_between_the_two_ticks_and_its_check_in_are_timing_differences(
    workflow: Workflow,
) -> None:
    _settled(workflow)
    workflow.add_worktree(12)
    workflow.running("ticket-12")
    workflow.screen(_LIMIT.replace("4pm", "5:00pm"), "ticket-12")
    workflow.orchestrator()

    # The limit resets at 17:00:00; the shadow tick reads 16:59:40, the real one 17:00:00.
    _ticks(workflow, _NOW - 20)

    assert "due within 180 s: ticket-12 was sent its instruction again" in (
        _shadow_log(workflow).read_text()
    )
    journal = workflow.root / "journal"
    journal.write_text(_journal("ticket-12 was sent its instruction again", at=_NOW))
    compared = _compare(
        *("--shadow", str(_shadow_log(workflow)), "--wake", str(workflow.wake_log)),
        *("--journal", str(journal)),
    )
    assert compared.returncode == 0, compared.stdout
    assert compared.stdout.splitlines()[-1].endswith(
        "none differs; 3 came due between a shadow tick and the real one"
    )


def test_a_stuck_session_due_between_the_two_ticks_and_its_check_in_are_timing_differences(
    tmp_path: Path,
) -> None:
    stuck = "stuck? ticket-14 (45 min, running: pytest tests)"
    shadow = f"{_NOW} shadow tick\n{_NOW} due within 180 s: {stuck}\n"
    wake = (
        f"{_NOW + 30} {stuck}\n"
        "2026-09-21 17:00:30  every 20 minutes\n"
        f"  {stuck}\n"
        "  told Check-in: Wake check-in, 17:00.\n"
    )

    done = _compare(*_logs(tmp_path, shadow, wake, "\n"))

    assert done.returncode == 0, done.stdout
    assert done.stdout.endswith(
        ": none differs; 3 came due between a shadow tick and the real one\n"
    )


# A shadow tick never holds the real one back.


def test_a_shadow_tick_stops_at_its_bound_while_the_real_tick_runs_on_time(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    # GitHub answers a shadow tick's list of pull requests after a minute.
    os.replace(workflow.bin / "gh", workflow.bin / "gh.reads")
    workflow.fake(
        "gh",
        "#!/bin/bash\n"
        '[[ -z ${SHIPLINE_SHADOW:-} || "$1 $2" != "pr list" ]] || /bin/sleep 60\n'
        f'exec "{workflow.bin}/gh.reads" "$@"\n',
    )
    env = {**workflow.env, **_CLOCK, "WORKFLOW_SHADOW_SECONDS": "2"}
    began = time.monotonic()
    shadow = subprocess.Popen(
        ["bash", str(workflow.script.with_name("tick")), "--shadow"],
        cwd=workflow.repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    real = workflow.run_script("tick", env={**_CLOCK, "WORKFLOW_NOW": str(_NOW + 20)})
    shadow.communicate(timeout=30)

    assert time.monotonic() - began < 20
    assert shadow.returncode == 124
    assert real.returncode == 0, real.stderr
    assert workflow.is_running("ticket-7")
    log = _shadow_log(workflow).read_text()
    assert log.splitlines()[-1] == f"{_NOW} shadow tick cut short after 2 s"
    journal = workflow.root / "journal"
    journal.write_text(_journal(real.stdout, at=_NOW + 20))
    compared = _compare(
        *("--shadow", str(_shadow_log(workflow)), "--wake", str(workflow.wake_log)),
        *("--journal", str(journal)),
    )
    assert compared.returncode == 0, compared.stdout
    assert re.search(
        r": none differs; \d+ beside a shadow tick cut short not compared$", compared.stdout
    )


def test_a_cut_tick_s_own_decisions_and_the_real_ones_beside_it_are_not_compared(
    tmp_path: Path,
) -> None:
    line = "ticket-13 idle 50 min waiting for its own job or turn; status check sent"
    shadow = (
        f"{_NOW} shadow tick\n"
        f"{_NOW} review-10 checks finished on {'a' * 40}; reviewer nudged\n"
        f"{_NOW + 180} shadow tick\n{_NOW + 180} {line}\n"
        f"{_NOW + 180} shadow tick cut short after 70 s\n"
    )
    # The real tick before the cut one took the same as its shadow tick; the one after the cut
    # tick took more than the shadow logged before it was stopped.
    wake = (
        f"{_NOW + 95} review-10 checks finished on {'a' * 40}; reviewer nudged\n"
        f"{_NOW + 260} {line}\n"
        f"{_NOW + 260} stuck? ticket-14 (45 min, running: pytest tests)\n"
    )

    done = _compare(*_logs(tmp_path, shadow, wake, "\n"))

    assert done.returncode == 0, done.stdout
    assert done.stdout.endswith(": none differs; 3 beside a shadow tick cut short not compared\n")
    assert done.stdout.startswith("1 decisions compared")


def test_a_real_tick_after_a_shadow_tick_that_did_not_run_is_counted_not_compared(
    tmp_path: Path,
) -> None:
    shadow = f"{_NOW} shadow tick\n{_NOW + 360} shadow tick\n"
    wake = f"{_NOW + 270} review-10 checks finished on {'a' * 40}; reviewer nudged\n"

    done = _compare(*_logs(tmp_path, shadow, wake, "\n"))

    assert done.returncode == 0, done.stdout
    assert done.stdout.endswith(": none differs; 1 with no shadow tick beside them not compared\n")


def test_a_check_in_with_another_line_of_its_own_is_no_timing_difference(tmp_path: Path) -> None:
    stuck = "stuck? ticket-14 (45 min, running: pytest tests)"
    shadow = f"{_NOW} shadow tick\n{_NOW} due within 180 s: {stuck}\n"
    wake = (
        f"{_NOW + 80} {stuck}\n"
        "2026-09-21 17:01:20  every 20 minutes\n"
        f"  {stuck}\n"
        "  lost check: CI result of run 123 on pull request 9\n"
        "  told Check-in: Wake check-in, 17:01.\n"
    )

    done = _compare(*_logs(tmp_path, shadow, wake, "\n"))

    assert done.returncode == 1
    assert "  real:   wake: lost check: CI result of run 123 on pull request 9" in done.stdout
    assert done.stdout.endswith(": 3 differ; 1 came due between a shadow tick and the real one\n")


def test_a_check_in_after_a_reset_that_came_due_is_timing_without_the_journal(
    tmp_path: Path,
) -> None:
    shadow = (
        f"{_NOW} shadow tick\n{_NOW} due within 180 s: ticket-12 was sent its instruction again\n"
    )
    wake = (
        "2026-09-21 17:01:20  after a reset\n"
        "  the tick sent its instruction again to: ticket-12\n"
        "  told Check-in: Wake check-in, 17:01, after a reset.\n"
    )

    done = _compare(*_logs(tmp_path, shadow, wake))

    assert done.returncode == 0, done.stdout
    assert done.stdout.endswith(
        ": none differs; 2 came due between a shadow tick and the real one\n"
    )


def test_a_night_check_in_the_morning_ends_before_the_real_wake_is_timing(
    workflow: Workflow,
) -> None:
    morning = _NOW + 15 * 3600 - 75  # 07:58:45 UTC, the shadow tick before the 08:00 tick
    _settled(workflow)
    workflow.orchestrator()
    looked = workflow.root / "home" / ".local" / "state" / "workflow" / "wake.looked"
    looked.parent.mkdir(parents=True, exist_ok=True)
    looked.write_text(f"{morning - 1500}\n")

    _ticks(workflow, morning, late=80)

    assert "  ends within 180 s: every 20 minutes\n" in _shadow_log(workflow).read_text()
    assert not workflow.wake_log.exists() or "every 20 minutes" not in workflow.wake_log.read_text()
    compared = _compare("--shadow", str(_shadow_log(workflow)), "--wake", str(workflow.wake_log))
    assert compared.returncode == 0, compared.stdout
    assert "came due between a shadow tick and the real one" in compared.stdout


def test_the_daily_analyst_due_at_eight_is_a_timing_difference(workflow: Workflow) -> None:
    eight = 1_792_828_800  # 2026-10-24 08:00 UTC
    report = workflow.repo / ".local" / "analyses" / "metrics-2026-10-24.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Workflow metrics\n")
    workflow.usage(40, datetime.fromtimestamp(eight + 3 * 3600, UTC).isoformat())
    cache = workflow.root / "home" / ".claude" / "MEMORY" / "STATE" / "usage-cache.json"
    os.utime(cache, (eight, eight))
    _settled(workflow)
    daily = {"WORKFLOW_ANALYST_DAILY_AT": "08:00"}

    shadow = workflow.run_script(
        "tick", "--shadow", env={**daily, "TZ": "UTC", "WORKFLOW_NOW": str(eight - 75)}
    )
    real = workflow.run_script("tick", env={**daily, "TZ": "UTC", "WORKFLOW_NOW": str(eight + 10)})

    assert shadow.returncode == real.returncode == 0, shadow.stderr + real.stderr
    assert workflow.is_running("analyst-day-2026-10-24")
    assert f"{eight - 75} due within 180 s: analyst-day-2026-10-24 started" in (
        _shadow_log(workflow).read_text()
    )
    journal = workflow.root / "journal"
    journal.write_text(_journal(real.stdout, at=eight + 10))
    compared = _compare(
        *("--shadow", str(_shadow_log(workflow)), "--wake", str(workflow.wake_log)),
        *("--journal", str(journal)),
    )
    listed = _compare(
        *("--shadow", str(_shadow_log(workflow)), "--wake", str(workflow.wake_log)),
        *("--journal", str(journal), "--timing"),
    )
    assert compared.returncode == 0, compared.stdout
    assert "  real:   analyst-day-2026-10-24 started" in listed.stdout, listed.stdout
    # So did the shadow wake's night look, which the morning ends before the real wake looks.
    assert "  shadow: wake (every 20 minutes): orchestrator registration" in listed.stdout
    assert compared.stdout.endswith("2 came due between a shadow tick and the real one\n")


def test_the_shadow_tick_s_bound_ends_it_before_the_real_tick_starts() -> None:
    readme = (_ROOT / "scripts" / "workflow" / "README.md").read_text()
    tick = (_ROOT / "scripts" / "workflow" / "tick").read_text()

    # The timer fires at :45 of the minute before each real tick, 75 seconds ahead of it.
    assert re.search(r"OnCalendar=\*:1/3:45\n", readme)
    bound = re.search(r"^SHADOW_SECONDS=\$\{WORKFLOW_SHADOW_SECONDS:-(\d+)\}$", tick, re.M)
    assert bound and int(bound[1]) + 5 <= 75
    assert "TimeoutStartSec=75" in readme
    assert "every `Environment=` line" in readme


def test_the_bounded_shadow_tick_is_shipline_s_own_whatever_folder_it_starts_in(
    workflow: Workflow,
) -> None:
    home = install_shipline(workflow)
    # The repository keeps a tick of its own, from before shadow ticks: run, it would act.
    own = workflow.repo / "scripts" / "workflow" / "tick"
    own.parent.mkdir(parents=True)
    own.write_text(f'#!/bin/bash\ntouch "{workflow.state}/the-repository-s-own-tick-ran"\n')

    done = subprocess.run(
        ["bash", "scripts/workflow/tick", "--shadow"],
        cwd=home,
        env={**workflow.env, **_CLOCK, "SHIPLINE_REPO": str(workflow.repo)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert not (workflow.state / "the-repository-s-own-tick-ran").exists()
    assert _shadow_log(workflow).read_text().startswith(f"{_NOW} shadow tick\n")


def test_a_shadow_tick_takes_no_clock_but_a_time(workflow: Workflow) -> None:
    done = workflow.run_script("tick", "--shadow", env={"SHIPLINE_SHADOW_BOUND": "$(touch x)"})

    assert done.returncode == 2
    assert done.stderr == "SHIPLINE_SHADOW_BOUND is no time\n"
    assert not (workflow.repo / "x").exists()


def test_the_readme_runs_the_shadow_tick_on_a_timer_of_its_own_and_bounds_it() -> None:
    readme = (_ROOT / "scripts" / "workflow" / "README.md").read_text()

    assert "ExecStartPre" not in readme
    assert (
        "ExecStart=<path of the Shipline checkout>/bin/shipline --repo <checkout> tick --shadow"
        in readme
    )
    assert "TimeoutStartSec=75" in readme
    # 75 seconds before each of the tick's (`OnCalendar=*:0/3` in install-timer).
    assert "OnCalendar=*:1/3:45" in readme
    assert "OnCalendar=*:0/3" in (_ROOT / "scripts" / "workflow" / "install-timer").read_text()


def test_each_shadow_tick_marks_its_run_and_the_train_status_when_it_changes(
    workflow: Workflow,
) -> None:
    for at in (_NOW, _NOW + 180):
        done = workflow.run_script("tick", "--shadow", env={**_CLOCK, "WORKFLOW_NOW": str(at)})
        assert done.returncode == 0, done.stderr

    lines = _shadow_log(workflow).read_text().splitlines()
    assert lines[0] == f"{_NOW} shadow tick"
    assert lines[1].startswith(f'{_NOW} train status: {{"first": null, "waiting": []')
    assert lines[2:] == [f"{_NOW + 180} shadow tick"]


def test_the_shadow_log_is_kept_from_other_users(workflow: Workflow) -> None:
    done = workflow.run_script("tick", "--shadow", env=_CLOCK)

    assert done.returncode == 0, done.stderr
    assert _shadow_log(workflow).stat().st_mode & 0o077 == 0
    assert _shadow_log(workflow).parent.stat().st_mode & 0o077 == 0


def test_a_shadow_tick_runs_while_the_real_tick_holds_its_lock(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    with (workflow.repo / ".git" / "shipline-tick.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        done = workflow.run_script("tick", "--shadow", env=_CLOCK)

    assert done.returncode == 0, done.stderr
    assert "ticket-7 started" in _decisions(_shadow_log(workflow))


def test_a_second_shadow_tick_exits_while_the_first_runs(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    with (workflow.repo / ".git" / "shipline-shadow.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        done = workflow.run_script("tick", "--shadow", env=_CLOCK)

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["the shadow tick before this one is still running"]
    assert workflow.calls() == []
    assert not _shadow_log(workflow).exists()


def test_the_tick_takes_no_other_argument(workflow: Workflow) -> None:
    done = workflow.run_script("tick", "--shadows")

    assert done.returncode == 2
    assert done.stderr == "usage: tick [--shadow]\n"
    assert workflow.calls() == []


def test_shipline_names_the_shadow_tick(workflow: Workflow) -> None:
    install_shipline(workflow)

    done = workflow.shipline("tick", "--shadows")

    assert done.returncode == 2
    assert done.stderr == "usage: tick [--shadow]\n"
    assert "shipline tick --shadow" in (workflow.home / "bin" / "shipline").read_text()


# What a shadow run refuses by itself, whoever calls it.


@pytest.mark.parametrize(
    "command",
    [
        ["stop", "ticket-7"],
        ["start", "worker", "7"],
        ["resend", "ticket-7"],
        ["squeeze", "ticket-7"],
        ["cleanup"],
        ["checks-finished", "review-7", "0" * 40],
    ],
)
def test_a_shadow_run_starts_stops_and_types_into_no_session(
    workflow: Workflow, command: list[str]
) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7", held=400 * _MB)
    log = _shadow_log(workflow)

    done = workflow.run(*command, env={"SHIPLINE_SHADOW": str(log)})

    assert done.returncode == 1
    assert done.stderr == f"a shadow run does not run session {command[0]}\n"
    assert workflow.is_running("ticket-7")
    assert workflow.calls("tmux") == [] and workflow.calls("gh") == []


def test_a_shadow_run_still_lists_the_sessions(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("ticket-7")

    done = workflow.run("list", env={"SHIPLINE_SHADOW": str(_shadow_log(workflow))})

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("ticket-7  busy")


def test_a_shadow_run_reads_a_limit_s_day_as_the_real_tick_does_and_keeps_no_note(
    workflow: Workflow,
) -> None:
    """An hour-only weekly limit gets its day from the usage figures; the real listing notes it."""
    workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.screen(
        "  ⎿  You've hit your weekly limit · resets 3am (America/New_York)" + _BOX, "ticket-7"
    )
    workflow.usage(
        40, "2026-10-16T05:00:00+00:00", seven=100, seven_resets="2026-10-16T07:00:00+00:00"
    )
    clock = {"WORKFLOW_NOW": "1792119600", "TZ": "UTC"}
    before = _digest(workflow.notes(7))

    shadow = workflow.run("list", env={**clock, "SHIPLINE_SHADOW": str(_shadow_log(workflow))})

    assert shadow.returncode == 0, shadow.stderr
    assert _digest(workflow.notes(7)) == before
    real = workflow.run("list", env=clock)
    assert shadow.stdout == real.stdout
    assert "usage limit  resets Oct 16, 2026, 7:01am (UTC)" in real.stdout
    assert (workflow.notes(7) / "ticket-7.limit-line").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["pr", "merge", "7", "--squash"],
        ["pr", "ready", "7"],
        ["api", "-X", "POST", "repos/example-org/example-app/issues/7/labels"],
        ["api", "repos/example-org/example-app/issues/7/comments", "-f", "body=hello"],
        ["api", "graphql", "-f", "query=mutation { closePullRequest }"],
        ["api", "graphql", "--input", "body.json"],
        ["label", "create", "workflow"],
    ],
)
def test_a_shadow_run_sends_github_no_write(workflow: Workflow, arguments: list[str]) -> None:
    called = subprocess.run(
        ["python3", str(workflow.script.with_name("github_budget.py")), "call", "tick", *arguments],
        cwd=workflow.repo,
        env={**workflow.env, "SHIPLINE_SHADOW": str(_shadow_log(workflow))},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert called.returncode == 1
    assert called.stderr == f"a shadow run sends GitHub no write: gh {' '.join(arguments)}\n"
    assert workflow.calls("gh") == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["pr", "view", "7", "--json", "state,headRefName,headRefOid", "--jq", "."],
        ["api", "--paginate", "repos/example-org/example-app/pulls/7/reviews"],
    ],
)
def test_a_shadow_run_still_reads_github(workflow: Workflow, arguments: list[str]) -> None:
    workflow.add_pull_request(7)

    called = subprocess.run(
        ["python3", str(workflow.script.with_name("github_budget.py")), "call", "tick", *arguments],
        cwd=workflow.repo,
        env={**workflow.env, "SHIPLINE_SHADOW": str(_shadow_log(workflow))},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert called.returncode == 0, called.stderr
    assert workflow.calls("gh") == [f"gh {' '.join(arguments)}"]


# The gate's read path.


def _train(workflow: Workflow) -> Path:
    folder = workflow.repo / ".local" / "workflow"
    folder.mkdir(parents=True, exist_ok=True)
    line = {
        "waiting": ["8", "7"],
        "blocked": {"8": "checks failed"},
        "priorities": {"7": "high", "8": "medium"},
        "turn": 4,
    }
    (folder / "merge-train.json").write_text(json.dumps(line))
    return folder


def test_a_shadow_run_reads_the_saved_merge_line_without_taking_the_lock(
    workflow: Workflow,
) -> None:
    folder = _train(workflow)
    saved = (folder / "merge-train.json").read_text()
    with (folder / "merge.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)

        done = workflow.run_script(
            "merge-when-current",
            "--train-status",
            env={"SHIPLINE_SHADOW": str(_shadow_log(workflow))},
        )

    assert done.returncode == 0, done.stderr
    status = json.loads(done.stdout)
    assert (status["first"], status["waiting"], status["blocked"], status["turn"]) == (
        "7",
        ["7"],
        ["8"],
        4,
    )
    assert (folder / "merge-train.json").read_text() == saved
    assert sorted(path.name for path in folder.iterdir()) == ["merge-train.json", "merge.lock"]
    assert workflow.calls() == []


@pytest.mark.parametrize(
    "arguments",
    [["7"], ["--handoff", "7"], ["--drive", "7"], ["--main-status"], ["--tick-main-status"]],
)
def test_a_shadow_run_merges_nothing_and_refreshes_no_report(
    workflow: Workflow, arguments: list[str]
) -> None:
    folder = _train(workflow)
    before = _digest(folder)

    done = workflow.run_script(
        "merge-when-current", *arguments, env={"SHIPLINE_SHADOW": str(_shadow_log(workflow))}
    )

    assert done.returncode == 1
    assert done.stderr == (
        "merge refused: a shadow run only reads the merge line (--train-status)\n"
    )
    assert _digest(folder) == before
    assert workflow.calls() == []


# Comparing a shadow log with the real tick's logs.


def _logs(tmp_path: Path, shadow: str, wake: str = "", journal: str = "") -> list[str]:
    paths = {"shadow": shadow, "wake": wake, "journal": journal}
    arguments = []
    for name, text in paths.items():
        if name == "journal" and not text:
            continue
        (tmp_path / name).write_text(text)
        arguments += [f"--{name}", str(tmp_path / name)]
    return arguments


def test_the_same_decisions_within_the_window_do_not_differ(tmp_path: Path) -> None:
    shadow = (
        f"{_NOW} shadow tick\n"
        f"{_NOW} review-10 checks finished on {'a' * 40}; reviewer nudged\n"
        f"{_NOW} ticket-7 started\n"
        f"{_NOW + 180} shadow tick\n"
    )
    wake = f"{_NOW + 20} review-10 checks finished on {'a' * 40}; reviewer nudged\n"
    journal = f"{_NOW + 95}.5 example-host bash[7]: ticket-7 started: https://example.com/s\n"

    done = _compare(*_logs(tmp_path, shadow, wake, journal))

    assert done.returncode == 0, done.stdout
    assert done.stdout == (
        "2 decisions compared from 2026-09-21 17:00:00 to 2026-09-21 17:03:00: none differs\n"
    )


def test_a_decision_only_one_side_took_is_listed_with_the_other_side_empty(
    tmp_path: Path,
) -> None:
    shadow = f"{_NOW} shadow tick\n{_NOW} audit-9 stopped\n"
    journal = f"{_NOW + 3}.0 example-host bash[7]: ticket-7 started: https://example.com/s\n"

    done = _compare(*_logs(tmp_path, shadow, "", journal))

    assert done.returncode == 1
    assert done.stdout == (
        "2026-09-21 17:00:00 audit-9\n"
        "  shadow: audit-9 stopped\n"
        "  real:   (nothing)\n"
        "2026-09-21 17:00:03 ticket-7\n"
        "  shadow: (nothing)\n"
        "  real:   ticket-7 started\n"
        "2 decisions compared from 2026-09-21 17:00:00 to 2026-09-21 17:00:00: 2 differ\n"
    )


def test_two_decisions_on_one_session_that_differ_show_both_versions(tmp_path: Path) -> None:
    shadow = f"{_NOW} shadow tick\n{_NOW} review-10 stopped\n"
    wake = f"{_NOW + 4} review-10 lost check on {'b' * 40}; reviewer nudged\n"

    done = _compare(*_logs(tmp_path, shadow, wake, "\n"))

    assert done.returncode == 1
    assert done.stdout.splitlines()[:3] == [
        "2026-09-21 17:00:00 review-10",
        "  shadow: review-10 stopped",
        f"  real:   review-10 lost check on {'b' * 40}; reviewer nudged",
    ]


def test_a_decision_outside_the_window_is_another_one(tmp_path: Path) -> None:
    shadow = f"{_NOW} shadow tick\n{_NOW} ticket-13 finish message failed: no box\n"
    wake = f"{_NOW + 400} ticket-13 finish message failed: no box\n"

    narrow = _compare(*_logs(tmp_path, shadow, wake), "--until", str(_NOW + 600))
    wide = _compare(*_logs(tmp_path, shadow, wake), "--window", "500")

    assert narrow.returncode == 1
    assert wide.returncode == 0, wide.stdout


def test_figures_that_move_between_two_ticks_are_left_out(tmp_path: Path) -> None:
    shadow = (
        f"{_NOW} shadow tick\n"
        f"{_NOW} ticket-13 idle 51 min waiting for its own job or turn; status check sent\n"
        f"{_NOW} stuck? ticket-14 (45 min, running: pytest tests)\n"
        f"{_NOW} ticket-11 squeezed\n"
    )
    wake = (
        f"{_NOW + 60} ticket-13 idle 52 min waiting for its own job or turn; status check sent\n"
        f"{_NOW + 60} stuck? ticket-14 (46 min, running: pytest tests)\n"
    )
    journal = f"{_NOW + 1}.0 example-host bash[7]: ticket-11 squeezed: 120 MB held\n"

    done = _compare(*_logs(tmp_path, shadow, wake, journal))

    assert done.returncode == 0, done.stdout


def test_the_wake_s_check_ins_are_compared_by_whom_they_told_and_their_own_lines(
    tmp_path: Path,
) -> None:
    def check_in(told: str, waited: int) -> str:
        return (
            "2026-09-21 17:00:00  after a reset\n"
            "  usage: 5-hour window 12% used, resets in 2h0m; load 0.5\n"
            "  sessions: ticket-12 busy; pull requests: none; starts: allowed;"
            " tmp pressure: false\n"
            "  the tick sent its instruction again to: ticket-12\n"
            f"  asking the owner: review-15 ({waited} min)\n"
            f"  {told}\n"
        )

    shadow = f"{_NOW} shadow tick\n" + check_in("told Check-in: Wake check-in, 17:00.", 8)
    wake = check_in("told Check-in: Wake check-in, 17:00, a longer message.", 9)
    other = check_in("Check-in is busy; not told", 8)

    same = _compare(*_logs(tmp_path, shadow, wake, "\n"))
    differs = _compare(*_logs(tmp_path, shadow, other, "\n"))

    assert same.returncode == 0, same.stdout
    assert same.stdout.startswith("3 decisions compared")
    assert differs.returncode == 1
    assert differs.stdout.splitlines()[:3] == [
        "2026-09-21 17:00:00 wake",
        "  shadow: wake (after a reset): told Check-in",
        "  real:   wake (after a reset): Check-in is busy; not told",
    ]


def test_what_only_the_real_installation_logs_is_not_compared(tmp_path: Path) -> None:
    shadow = f"{_NOW} shadow tick\n{_NOW} train status: {{}}\n"
    wake = (
        f"{_NOW} ticket start: started 611 (priority high)\n"
        f"{_NOW} handoff 7 held the merge lock for 0.4 s\n"
        f"{_NOW} automatically re-ran failed jobs of CI\n"
    )
    journal = (
        f"{_NOW}.1 example-host bash[7]: ticket-7: removed, its pull request is merged\n"
        f"{_NOW}.1 example-host bash[7]: the orchestrator (Check-in) was given a check-in\n"
    )

    done = _compare(*_logs(tmp_path, shadow, wake, journal))

    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("0 decisions compared")


def test_without_the_journal_the_session_commands_are_not_compared(tmp_path: Path) -> None:
    shadow = f"{_NOW} shadow tick\n{_NOW} audit-9 stopped\n{_NOW} ticket-7 started\n"

    done = _compare(*_logs(tmp_path, shadow))

    assert done.returncode == 0, done.stdout
    assert done.stdout == (
        "the lines of the session commands (stopped, started, squeezed, sent again, bridged, "
        "start held) are not compared: no --journal\n"
        "0 decisions compared from 2026-09-21 17:00:00 to 2026-09-21 17:00:00: none differs\n"
    )


def test_only_the_hours_of_the_shadow_run_are_compared(tmp_path: Path) -> None:
    shadow = f"{_NOW} shadow tick\n{_NOW + 1790} shadow tick\n{_NOW + 3600} shadow tick\n"
    wake = (
        f"{_NOW - 600} review-10 checks finished on {'a' * 40}; reviewer nudged\n"
        f"{_NOW + 1800} review-11 checks finished on {'a' * 40}; reviewer nudged\n"
        f"{_NOW + 9000} review-12 checks finished on {'a' * 40}; reviewer nudged\n"
    )

    whole = _compare(*_logs(tmp_path, shadow, wake, "\n"))
    named = _compare(*_logs(tmp_path, shadow, wake, "\n"), "--from", "2026-09-21T17:40:00")

    assert whole.returncode == 1
    assert [line for line in whole.stdout.splitlines() if not line.startswith(" ")] == [
        "2026-09-21 17:30:00 review-11",
        "1 decisions compared from 2026-09-21 17:00:00 to 2026-09-21 18:00:00: 1 differ",
    ]
    assert named.returncode == 0, named.stdout


def test_a_journal_line_counts_from_the_start_of_its_service(tmp_path: Path) -> None:
    """A start prints its line once its session is up, minutes into the tick."""
    shadow = f"{_NOW} shadow tick\n{_NOW} ticket-7 started\n{_NOW + 890} shadow tick\n"
    journal = (
        f"{_NOW + 75}.0 example-host systemd[1]: Starting workflow-tick.service - Tick...\n"
        f"{_NOW + 400}.0 example-host bash[7]: ticket-7 started: https://example.com/s\n"
        f"{_NOW + 410}.0 example-host systemd[1]: Finished workflow-tick.service - Workflow tick.\n"
        f"{_NOW + 900}.0 example-host bash[8]: ticket-8 started: https://example.com/t\n"
    )

    done = _compare(*_logs(tmp_path, shadow, "", journal))

    assert done.returncode == 1
    assert [line for line in done.stdout.splitlines() if not line.startswith(" ")] == [
        "2026-09-21 17:15:00 ticket-8",
        "2 decisions compared from 2026-09-21 17:00:00 to 2026-09-21 17:14:50: 1 differ",
    ]


def test_a_real_tick_without_a_shadow_tick_beside_it_is_not_compared(tmp_path: Path) -> None:
    shadow = f"{_NOW} shadow tick\n{_NOW + 360} shadow tick\n"
    wake = f"{_NOW + 180} review-10 checks finished on {'a' * 40}; reviewer nudged\n"

    done = _compare(*_logs(tmp_path, shadow, wake, "\n"), "--window", "60")

    assert done.returncode == 0, done.stdout
    assert done.stdout.startswith("0 decisions compared")


def test_the_load_of_a_held_start_and_an_orchestrator_s_spaced_name_compare(
    tmp_path: Path,
) -> None:
    held = (
        "the processors are overloaded: load {} over 5 minutes, ceiling 6 (1.5 times 4 processors)"
    )
    shadow = (
        f"{_NOW} shadow tick\n{_NOW} "
        + held.format("7.10")
        + "; the next tick tries this start again\n"
        "2026-09-21 17:00:00  after a reset\n  told Check in: Wake check-in, 17:00.\n"
    )
    wake = "2026-09-21 17:00:01  after a reset\n  Check in is busy; not told\n"
    journal = (
        f"{_NOW + 1}.0 example-host bash[7]: "
        + held.format("7.25")
        + "; the next tick tries this start again\n"
    )

    done = _compare(*_logs(tmp_path, shadow, wake, journal))

    assert done.returncode == 1
    assert done.stdout.splitlines()[:3] == [
        "2026-09-21 17:00:00 wake",
        "  shadow: wake (after a reset): told Check in",
        "  real:   wake (after a reset): Check in is busy; not told",
    ]
    assert done.stdout.splitlines()[-1].endswith(": 1 differ")


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (["--window", "soon"], "--window: soon is not a number of seconds"),
        (["--from", "yesterday"], "--from: yesterday is not a time"),
        (["--shadow", "/nonexistent/shadow.log"], "cannot read /nonexistent/shadow.log"),
    ],
)
def test_a_wrong_argument_is_named(tmp_path: Path, arguments: list[str], error: str) -> None:
    done = _compare(*_logs(tmp_path, f"{_NOW} shadow tick\n"), *arguments)

    assert done.returncode == 2
    assert done.stderr.splitlines()[-1].endswith(error)


def test_a_shadow_log_without_a_run_names_no_hours(tmp_path: Path) -> None:
    done = _compare(*_logs(tmp_path, ""))

    assert done.returncode == 2
    assert (
        done.stderr
        == "the shadow log holds no shadow tick: name the hours with --from and --until\n"
    )
