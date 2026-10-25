"""Breaking tests for pull request 66: CI demand forecasts across failed GitHub reads.

Each test replays the tick's CI demand step with neutral names while one GitHub read goes
wrong the way it does in use: a job read that fails once and works a minute later, a read
that hangs past its timeout, a secondary rate limit, a crashed gh, an empty answer, a
partly re-run run, and two forecasts written at once.
"""

import json
import threading

import pytest
from workflow.test_ci_demand import AT, ORDINARY, Demand, _ci, _job, _run
from workflow_harness import Workflow

# Sits in front of the forecast tests' own fake gh. A file `once-<text>` fails the next read
# whose path holds the text, once; `hang-<text>` sleeps past the call's timeout; `limit-<text>`
# answers as GitHub's secondary rate limit; `crash-<text>` dies by a signal; `empty-<text>`
# answers nothing with success.
_FLAKY = """#!/usr/bin/env python3
import os
import signal
import sys
import time
from pathlib import Path

state = Path("@STATE@")
path = sys.argv[-1]
for mark in sorted(state.glob("*-*")):
    kind, _, text = mark.name.partition("-")
    if kind not in ("once", "hang", "limit", "crash", "empty"):
        continue
    if text.replace("%", "/") not in path:
        continue
    with open(state / "flaky.log", "a") as log:
        log.write(kind + " " + path + "\\n")
    if kind == "once":
        mark.unlink()
        print("gh: HTTP 502: Bad Gateway", file=sys.stderr)
        sys.exit(1)
    if kind == "hang":
        time.sleep(45)
    if kind == "limit":
        print("gh: You have exceeded a secondary rate limit (HTTP 403)", file=sys.stderr)
        sys.exit(1)
    if kind == "crash":
        os.kill(os.getpid(), signal.SIGKILL)
    if kind == "empty":
        sys.exit(0)
os.execv("@DEMAND_GH@", ["gh", *sys.argv[1:]])
"""


@pytest.fixture
def demand(workflow: Workflow) -> Demand:
    demand = Demand(workflow)
    workflow.fake("gh-demand", (workflow.bin / "gh").read_text())
    workflow.fake("gh", _FLAKY.replace("@DEMAND_GH@", str(workflow.bin / "gh-demand")))
    return demand


def _mark(demand: Demand, kind: str, text: str) -> None:
    (demand.workflow.state / f"{kind}-{text.replace('/', '%')}").touch()


def _tick_with_one_busy_worker(demand: Demand, runs: list) -> None:
    workflow = demand.workflow
    workflow.add_worktree(8)
    workflow.running("ticket-8", held=100 * 1024 * 1024)
    workflow.screen("Working\n  ✻ Thinking…\n", "ticket-8")
    demand.pull(8, runs=runs)
    demand.runs[workflow.branch(8)] = demand.runs.pop("ticket/change-8")
    demand.prepare()


def test_a_job_read_failing_once_does_not_fail_the_tick_and_the_next_tick_is_whole(
    demand: Demand,
) -> None:
    # The daily flow: one worker busy, its last run still in progress, so its jobs are read
    # every tick. The first tick's job read fails once; a minute later it works.
    workflow = demand.workflow
    run = _run(81, "ci.yml", status="in_progress")
    _tick_with_one_busy_worker(demand, [(run, [_job(ORDINARY)] * 4)])
    demand.forecast.parent.mkdir(parents=True)
    demand.forecast.write_text('{"at": 1, "ordinary": 3, "docker": 0}\n')
    before = demand.forecast.read_bytes()
    _mark(demand, "once", "/runs/81/attempts/1/jobs")

    first = workflow.run_script("tick", env=AT)

    assert first.returncode == 0, first.stdout + first.stderr
    assert demand.forecast.read_bytes() == before
    assert "CI demand: no runs could be read; the last forecast is kept" in first.stdout
    assert "CI demand:" not in first.stderr

    second = workflow.run_script("tick", env={**AT, "WORKFLOW_NOW": str(int(AT["WORKFLOW_NOW"]) + 60)})

    assert second.returncode == 0, second.stdout + second.stderr
    forecast = json.loads(demand.forecast.read_text())
    assert forecast["ordinary"] == 4
    assert "skipped_runs" not in forecast
    assert "the last forecast is kept" not in second.stdout


def test_one_failed_job_read_among_several_pulls_writes_a_partial_forecast(
    demand: Demand,
) -> None:
    demand.pull(7, draft=False, runs=[(_run(71, "ci.yml", status="in_progress"), [_job(ORDINARY)] * 5)])
    demand.pull(8, draft=False, runs=[_ci(81, 3)])
    _mark(demand, "once", "/runs/71/attempts/1/jobs")

    forecast = demand.write()

    assert forecast["ordinary"] == 3
    assert forecast["skipped_runs"] == 1
    assert demand.printed.endswith("; 1 run skipped\n")
    assert demand.write()["ordinary"] == 8


@pytest.mark.parametrize("kind", ["limit", "crash", "empty"])
def test_a_misbehaving_job_read_never_exits_1(demand: Demand, kind: str) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 5)])
    demand.pull(8, draft=False, runs=[_ci(81, 3)])
    _mark(demand, kind, "/runs/71/attempts/1/jobs")

    snapshot = demand.prepare()
    done = demand.workflow.run_python(
        "ci_demand.py", "write", str(demand.workflow.repo), str(snapshot), env=AT
    )

    # A secondary limit pauses the budget: exit 75 for the rest, never a failed tick.
    assert done.returncode in (0, 75), done.stdout + done.stderr
    if done.returncode == 0:
        forecast = json.loads(demand.forecast.read_text())
        assert forecast["skipped_runs"] == 1


def test_a_job_read_past_its_timeout_skips_the_run(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 5)])
    demand.pull(8, draft=False, runs=[_ci(81, 3)])
    _mark(demand, "hang", "/runs/71/attempts/1/jobs")

    forecast = demand.write()

    assert forecast["ordinary"] == 3
    assert forecast["skipped_runs"] == 1


def test_a_run_list_failing_once_keeps_the_forecast_and_exits_0(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 2)])
    demand.write()
    before = demand.forecast.read_bytes()
    _mark(demand, "once", "/runs?branch=ticket")

    demand.write()

    assert demand.forecast.read_bytes() == before
    assert demand.printed == "CI demand: the run list could not be read; the last forecast is kept\n"


def test_a_partly_rerun_run_whose_first_attempt_cannot_be_read(demand: Demand) -> None:
    # Run 72 was re-run in part (attempt 2); its first attempt's jobs fail to read, so the
    # older run 71 stands in for the branch.
    demand.pull(
        7,
        draft=False,
        runs=[(_run(72, "ci.yml", attempt=2), [_job(ORDINARY)] * 6), _ci(71, 2)],
    )
    _mark(demand, "once", "/runs/72/attempts/1/jobs")

    forecast = demand.write()

    assert forecast["ordinary"] == 2
    assert forecast["skipped_runs"] == 1
    assert "/runs/72/attempts/2/" not in (demand.workflow.state / "calls.log").read_text()


def test_two_forecasts_at_once_with_failed_reads_leave_a_whole_file(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 5)])
    demand.pull(8, draft=False, runs=[_ci(81, 3)])
    demand.write()
    _mark(demand, "once", "/runs/71/attempts/1/jobs")
    snapshot = demand.prepare()
    results = []

    def one() -> None:
        results.append(
            demand.workflow.run_python(
                "ci_demand.py", "write", str(demand.workflow.repo), str(snapshot), env=AT
            )
        )

    threads = [threading.Thread(target=one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [done.returncode for done in results] == [0, 0], [d.stderr for d in results]
    forecast = json.loads(demand.forecast.read_text())
    assert forecast["ordinary"] in (3, 8)
    kept = json.loads(demand.forecast.with_name("ci-demand-runs.json").read_text())
    assert isinstance(kept, dict)


def test_a_skipped_newest_run_does_not_pull_the_average_down(demand: Demand) -> None:
    # Pull 7's newest run cannot be read and its older one ran no job; pull 8 reads 4 jobs and
    # pull 9 has no history, so it takes the average of the pulls actually read.
    demand.pull(
        7,
        draft=False,
        runs=[_ci(72, 9), (_run(71, "ci.yml"), [_job(ORDINARY, "skipped")])],
    )
    demand.pull(8, draft=False, runs=[_ci(81, 4)])
    demand.pull(9, draft=False)
    _mark(demand, "once", "/runs/72/attempts/1/jobs")

    forecast = demand.write()

    assert forecast["ordinary"] == 8  # 0 for 7, 4 for 8, 4 estimated for 9.
