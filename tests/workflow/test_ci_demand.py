"""The tick writes the CI demand on its way: the jobs the coming pushes will start."""

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow

NOW = 1792828200
AT = {"WORKFLOW_NOW": str(NOW), "TZ": "UTC"}
API = "repos/example-org/example-app/actions"

# Answers the runs of a branch from `runs.json` (branch: runs) and each attempt's jobs from
# `jobs.json` ("<run>-<attempt>": jobs); every call goes to the log.
_FAKE_GH = """#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

state = Path("@STATE@")
arguments = sys.argv[1:]
path = arguments[-1]
api = "repos/example-org/example-app/actions"
jobs_path = re.fullmatch(api + r"/runs/(\\d+)/attempts/(\\d+)/jobs\\?per_page=100", path)
runs_path = (urlsplit(path).path == api + "/runs"
             and parse_qs(urlsplit(path).query).get("per_page") == ["30"])
if not (len(arguments) == 2 and arguments[0] == "api" and (jobs_path or runs_path)):
    os.execv("@TICK_GH@", ["gh", *arguments])
with open(state / "calls.log", "a") as log:
    log.write("gh " + " ".join(arguments) + "\\n")
if (state / "fail-gh").exists():
    sys.exit(1)
if (state / "fail-path").exists() and (state / "fail-path").read_text() in path:
    sys.exit(1)
if (state / "bad-path").exists() and (state / "bad-path").read_text() in path:
    print((state / "bad-answer").read_text())
    sys.exit(0)
if found := jobs_path:
    jobs = json.loads((state / "jobs.json").read_text())
    print(json.dumps({"jobs": jobs.get(f"{found[1]}-{found[2]}", [])}))
elif urlsplit(path).path == api + "/runs":
    asked = parse_qs(urlsplit(path).query)
    assert asked["per_page"] == ["30"], asked
    runs = json.loads((state / "runs.json").read_text()).get(asked["branch"][0], [])
    print(json.dumps({"total_count": len(runs), "workflow_runs": runs}))
else:
    print(f"gh: could not answer {arguments}", file=sys.stderr)
    sys.exit(1)
"""

ORDINARY = "example-app-home"
DOCKER = "example-app-home-docker"


def _run(number: int, workflow: str, *, status: str = "completed", attempt: int = 1) -> dict:
    return {
        "id": number,
        "run_attempt": attempt,
        "status": status,
        "path": f".github/workflows/{workflow}",
        "created_at": f"2026-10-24T0{number % 10}:00:00Z",
    }


def _job(label: str, conclusion: str | None = "success") -> dict[str, Any]:
    return {"labels": ["self-hosted", label], "conclusion": conclusion}


class Demand:
    """One test's GitHub answers and state, and the forecast `ci_demand.py` writes from them."""

    def __init__(self, workflow: Workflow) -> None:
        self.workflow = workflow
        workflow.fake("gh-tick", (workflow.bin / "gh").read_text())
        workflow.fake("gh", _FAKE_GH.replace("@TICK_GH@", str(workflow.bin / "gh-tick")))
        self.runs: dict[str, list[dict]] = {}
        self.jobs: dict[str, list[dict]] = {}
        self.pulls: dict[str, dict[str, Any]] = {}
        self.notes = workflow.repo / ".local" / "workflow"
        self.notes.mkdir(parents=True, exist_ok=True)

    def pull(
        self,
        number: int,
        *,
        draft: bool = True,
        checks: list | None = None,
        runs: list[tuple[dict, list[dict]]] = (),
        labels: tuple[str, ...] = ("workflow",),
    ) -> None:
        branch = f"ticket/change-{number}"
        self.pulls[str(number)] = {
            "number": number,
            "state": "OPEN",
            "isDraft": draft,
            "headRefName": branch,
            "statusCheckRollup": checks or [],
            "labels": [{"name": label} for label in labels],
        }
        self.runs[branch] = [run for run, _ in runs]
        for run, jobs in runs:
            # A push starts the first attempt: the jobs of later ones are not read.
            self.jobs[f"{run['id']}-1"] = jobs
            for attempt in range(2, run["run_attempt"] + 1):
                self.jobs[f"{run['id']}-{attempt}"] = [_job(ORDINARY, "skipped")]

    def cloud(self, number: int, status: str) -> None:
        (self.notes / f"cloud-{number}").write_text(json.dumps({"status": status}))

    def train(self, value: dict[str, Any]) -> None:
        (self.notes / "merge-train.json").write_text(json.dumps(value))

    def prepare(self) -> Path:
        state = self.workflow.state
        (state / "runs.json").write_text(json.dumps(self.runs))
        (state / "jobs.json").write_text(json.dumps(self.jobs))
        snapshot = self.workflow.root / "pulls.json"
        snapshot.write_text(json.dumps(self.pulls))
        return snapshot

    def write(self, sessions: str = "", *, code: int = 0) -> dict[str, Any] | None:
        snapshot = self.prepare()
        done = self.workflow.run_python(
            "ci_demand.py", "write", str(self.workflow.repo), str(snapshot), stdin=sessions, env=AT
        )
        assert done.returncode == code, done.stdout + done.stderr
        self.printed = done.stdout
        self.error = done.stderr
        path = self.forecast
        return json.loads(path.read_text()) if path.exists() else None

    @property
    def forecast(self) -> Path:
        return self.workflow.root / "home/.local/state/workflow/ci-demand.json"


@pytest.fixture
def demand(workflow: Workflow) -> Demand:
    return Demand(workflow)


def _ci(run: int, ordinary: int = 3, docker: int = 0) -> tuple[dict, list[dict]]:
    return _run(run, "ci.yml"), [_job(ORDINARY)] * ordinary + [_job(DOCKER)] * docker


def test_every_push_on_its_way_counts_its_last_runs_jobs(demand: Demand) -> None:
    # 7 works in the cloud, 8 has a busy worker, 9 a busy reviewer, 10 is ready and unchecked.
    demand.cloud(7, "working")
    demand.pull(7, runs=[_ci(71, 2)])
    demand.pull(8, runs=[_ci(81, 3)])
    demand.pull(9, draft=False, checks=[{"name": "CI result"}], runs=[_ci(91, 1)])
    images = (_run(102, "images.yml"), [_job(DOCKER), _job(DOCKER), _job(DOCKER, "skipped")])
    demand.pull(10, draft=False, runs=[_ci(101, 4), images])
    sessions = "ticket-8  busy  100 MB held  0 MB in swap\nreview-9  busy  memory unknown\n"

    forecast = demand.write(sessions)

    assert forecast == {
        "at": NOW,
        "within_minutes": 30,
        "ordinary": 2 + 3 + 1 + 4,
        "docker": 2,
        "pulls": {"cloud": [7], "busy": [8, 9], "ready": [10], "prefixes": []},
    }
    assert demand.printed == "about 10 ordinary and 2 Docker jobs within 30 minutes\n"


def test_only_working_cloud_workers_busy_sessions_and_unchecked_ready_pulls_count(
    demand: Demand,
) -> None:
    for number, status in ((7, "finished"), (8, "asking"), (9, "blocked"), (10, "starting")):
        demand.cloud(number, status)
        demand.pull(number, runs=[_ci(number * 10)])
    demand.pull(11, runs=[_ci(110)])
    demand.pull(12, draft=False, checks=[{"name": "CI: what to run"}], runs=[_ci(120)])
    demand.pull(13, runs=[_ci(130)])
    # Ready and unchecked, but no ticket of the workflow: no push of it is on its way.
    demand.pull(14, draft=False, runs=[_ci(140)], labels=())
    sessions = (
        "ticket-11  idle  100 MB held\nreview-13  limit  0 MB held  session limit  resets 4pm\n"
    )

    forecast = demand.write(sessions + "hunt-x  busy  0 MB held\naudit-11  busy  0 MB held\n")

    assert forecast is not None
    assert (forecast["ordinary"], forecast["docker"]) == (0, 0)
    assert forecast["pulls"] == {"cloud": [], "busy": [], "ready": [], "prefixes": []}


def test_a_pull_request_counted_twice_pushes_once(demand: Demand) -> None:
    demand.cloud(7, "working")
    demand.pull(7, draft=False, runs=[_ci(71, 2)])

    forecast = demand.write("ticket-7  busy  0 MB held\nreview-7  busy  0 MB held\n")

    assert forecast is not None
    assert forecast["ordinary"] == 2
    assert forecast["pulls"] == {"cloud": [7], "busy": [], "ready": [], "prefixes": []}


def test_the_merge_line_counts_the_prefixes_still_to_build(demand: Demand) -> None:
    # A window of 3 with one prefix running: the next two eligible members after it, in the
    # line's order.
    for number in (11, 12, 13, 14, 15, 16):
        demand.pull(number, draft=False, checks=[{"name": "CI result"}], runs=[_ci(number * 10)])
    demand.train(
        {
            "waiting": ["11", "12", "13", "14", "15", "16"],
            "blocked": {"12": "red"},
            "suspended": {},
            "conflicts": {"14": "conflict"},
            "unavailable": [],
            "priorities": {"16": "high"},
            "fixes": [],
            "prefixes": {"base": "a" * 40, "merged": 0, "members": [{"number": "11"}]},
            "window": 3,
        }
    )

    forecast = demand.write()

    assert forecast is not None
    # The line extends after its last running prefix (11), never ahead of it: 16 waits.
    assert forecast["pulls"]["prefixes"] == [13, 15]
    assert forecast["ordinary"] == 6


@pytest.mark.parametrize(
    ("train", "expected"),
    [
        # A line starts only with two waiting, or a front whose failed prefix passed again.
        ({"waiting": ["11"]}, []),
        ({"waiting": ["11"], "proven": {"11": {"branch": "b"}}}, [11]),
        ({"waiting": ["11", "12"]}, [11, 12]),
        # A blocked front starts nothing; a pending member is passed over.
        ({"waiting": ["11", "12"], "blocked": {"11": "red"}, "refreshable": ["11"]}, []),
        ({"waiting": ["11", "12", "13"], "pending": ["12"]}, [11, 13]),
    ],
)
def test_the_line_starts_where_the_gate_starts_it(
    demand: Demand, train: dict[str, Any], expected: list[int]
) -> None:
    for number in (11, 12, 13):
        demand.pull(number, draft=False, checks=[{"name": "CI result"}], runs=[_ci(number * 10)])
    demand.train({"window": 4, **train})

    forecast = demand.write()

    assert forecast is not None
    assert forecast["pulls"]["prefixes"] == expected


def test_a_full_window_builds_no_prefix(demand: Demand) -> None:
    demand.pull(11, draft=False, checks=[{"name": "CI result"}], runs=[_ci(110)])
    demand.pull(12, draft=False, checks=[{"name": "CI result"}], runs=[_ci(120)])
    members = [{"number": "11"}, {"number": "12"}]
    demand.train({"waiting": ["11", "12"], "prefixes": {"members": members}, "window": 2})

    forecast = demand.write()

    assert forecast is not None
    assert forecast["pulls"]["prefixes"] == []


def test_each_workflow_counts_its_newest_run_that_ran_a_job(demand: Demand) -> None:
    # The newest image run skipped every job (a draft push); the one before it built images.
    skipped = (_run(203, "images.yml"), [_job(DOCKER, "skipped")])
    built = (_run(202, "images.yml"), [_job(DOCKER), _job(DOCKER)])
    older_ci = _ci(200, 9)
    running = (_run(201, "ci.yml", status="in_progress"), [_job(ORDINARY), _job(ORDINARY, None)])
    hosted = (_run(204, "other.yml"), [{"labels": ["ubuntu-latest"], "conclusion": "success"}])
    demand.cloud(7, "working")
    demand.pull(7, runs=[skipped, built, older_ci, running, hosted])

    forecast = demand.write()

    assert forecast is not None
    assert (forecast["ordinary"], forecast["docker"]) == (2, 2)


def test_a_re_run_counts_the_jobs_its_push_started(demand: Demand) -> None:
    # The newest CI run had its failed job run again: one push still starts all four jobs.
    demand.cloud(7, "working")
    demand.pull(7, runs=[(_run(71, "ci.yml", attempt=2), [_job(ORDINARY)] * 4)])

    forecast = demand.write()

    assert forecast is not None
    assert forecast["ordinary"] == 4
    assert any("/runs/71/attempts/1/jobs" in call for call in demand.workflow.calls("gh api"))


def test_a_kept_count_that_is_not_one_is_read_again(demand: Demand) -> None:
    demand.cloud(7, "working")
    demand.pull(7, runs=[_ci(71, 2)])
    kept = demand.forecast.with_name("ci-demand-runs.json")
    kept.parent.mkdir(parents=True)
    kept.write_text(json.dumps({"71": "broken", "72-1": [1, 2, True]}))

    forecast = demand.write()

    assert forecast is not None
    assert forecast["ordinary"] == 2
    assert json.loads(kept.read_text()) == {"71": [2, 0, True]}


def test_a_pull_request_without_runs_counts_as_the_others_on_average(demand: Demand) -> None:
    demand.cloud(7, "working")
    demand.pull(7, runs=[_ci(71, 2, 1)])
    demand.cloud(8, "working")
    demand.pull(8, runs=[_ci(81, 3)])
    demand.cloud(9, "working")
    demand.pull(9)

    forecast = demand.write()

    assert forecast is not None
    # 2.5 ordinary and 0.5 Docker jobs, rounded up.
    assert (forecast["ordinary"], forecast["docker"]) == (2 + 3 + 3, 1 + 1)


def test_a_completed_runs_jobs_are_read_once(demand: Demand) -> None:
    demand.cloud(7, "working")
    running = (_run(72, "e2e.yml", status="in_progress"), [_job(ORDINARY)])
    demand.pull(7, runs=[_ci(71, 2), running])
    demand.write()
    first = demand.workflow.calls("gh api")

    demand.write()

    again = demand.workflow.calls("gh api")[len(first) :]
    assert sum("/runs/71/" in call for call in first) == 1
    assert not any("/runs/71/" in call for call in again)
    assert sum("/runs/72/" in call for call in again) == 1


def test_no_runner_labels_no_forecast(demand: Demand) -> None:
    (demand.workflow.repo / "shipline.json").write_text("{}")
    demand.cloud(7, "working")
    demand.pull(7, runs=[_ci(71)])

    assert demand.write() is None
    assert demand.workflow.calls("gh") == []


def test_a_failed_read_keeps_the_last_forecast(demand: Demand) -> None:
    demand.cloud(7, "working")
    demand.pull(7, runs=[_ci(71, 2)])
    before = demand.write()
    (demand.workflow.state / "fail-gh").touch()
    demand.pull(7, runs=[_ci(73, 5)])

    original = demand.forecast.read_bytes()
    assert demand.write() == before
    assert demand.forecast.read_bytes() == original
    assert demand.printed == "CI demand: the run list could not be read; the last forecast is kept\n"


def test_a_failed_job_read_is_skipped_and_read_again_next_turn(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 9), _ci(70, 2)])
    demand.pull(8, draft=False, runs=[_ci(81, 3)])
    failed = demand.workflow.state / "fail-path"
    failed.write_text("/runs/71/")

    forecast = demand.write()

    assert forecast is not None
    assert forecast["ordinary"] == 5
    assert forecast["skipped_runs"] == 1
    assert "1 run skipped" in demand.printed
    kept = json.loads(demand.forecast.with_name("ci-demand-runs.json").read_text())
    assert "71" not in kept
    failed.unlink()
    forecast = demand.write()
    assert forecast["ordinary"] == 12
    assert "skipped_runs" not in forecast
    assert sum("/runs/71/" in call for call in demand.workflow.calls("gh api")) == 2


def test_no_readable_jobs_keeps_both_last_files(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 2)])
    demand.write()
    kept = demand.forecast.with_name("ci-demand-runs.json")
    before = demand.forecast.read_bytes(), kept.read_bytes()
    demand.pull(7, draft=False, runs=[_ci(72, 5)])
    (demand.workflow.state / "fail-path").write_text("/attempts/1/jobs")

    demand.write()

    assert (demand.forecast.read_bytes(), kept.read_bytes()) == before
    assert demand.printed == "CI demand: no runs could be read; the last forecast is kept\n"


def test_failed_runs_are_not_estimated_or_used_in_the_average(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 9)])
    demand.pull(8, draft=False, runs=[_ci(81, 3)])
    demand.pull(9, draft=False)
    (demand.workflow.state / "fail-path").write_text("/runs/71/")

    forecast = demand.write()

    assert forecast["ordinary"] == 6  # Three read, three estimated for the empty history.
    assert forecast["skipped_runs"] == 1


def test_a_partly_read_branch_does_not_feed_the_average(demand: Demand) -> None:
    # 7's newest run cannot be read and its older one ran no job: 7 counts nothing and must not
    # lower the average that 9, with no history, takes from the branches read whole.
    nothing_ran = (_run(71, "ci.yml"), [_job(ORDINARY, "skipped")])
    demand.pull(7, draft=False, runs=[_ci(72, 9), nothing_ran])
    demand.pull(8, draft=False, runs=[_ci(81, 4)])
    demand.pull(9, draft=False)
    (demand.workflow.state / "fail-path").write_text("/runs/72/")

    forecast = demand.write()

    assert forecast["ordinary"] == 8  # None for 7, four for 8, four estimated for 9.
    assert forecast["skipped_runs"] == 1

def test_an_unreadable_first_forecast_creates_neither_file(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 9)])
    (demand.workflow.state / "fail-path").write_text("/attempts/1/jobs")

    assert demand.write() is None
    assert not demand.forecast.with_name("ci-demand-runs.json").exists()


def test_a_later_run_list_failure_keeps_both_files(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 2)])
    demand.write()
    kept = demand.forecast.with_name("ci-demand-runs.json")
    before = demand.forecast.read_bytes(), kept.read_bytes()
    demand.pull(7, draft=False, runs=[_ci(72, 5)])
    demand.pull(8, draft=False, runs=[_ci(81, 3)])
    (demand.workflow.state / "fail-path").write_text("branch=ticket%2Fchange-8")

    demand.write()

    assert (demand.forecast.read_bytes(), kept.read_bytes()) == before


@pytest.mark.parametrize(
    "answer",
    ["not JSON", "null", '{}', '{"workflow_runs": null}', '{"workflow_runs": [{}]}'],
)
def test_an_unusable_run_list_keeps_the_last_forecast(demand: Demand, answer: str) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 2)])
    demand.write()
    before = demand.forecast.read_bytes()
    (demand.workflow.state / "bad-path").write_text("/runs?")
    (demand.workflow.state / "bad-answer").write_text(answer)

    demand.write()

    assert demand.forecast.read_bytes() == before
    assert "the last forecast is kept" in demand.printed


@pytest.mark.parametrize(
    "answer",
    [
        "not JSON", "null", '{}', '{"jobs": null}', '{"jobs": [null]}',
        '{"jobs": [{"labels": [null]}]}',
    ],
)
def test_an_unusable_jobs_answer_skips_only_its_run(demand: Demand, answer: str) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 9), _ci(70, 2)])
    (demand.workflow.state / "bad-path").write_text("/runs/71/")
    (demand.workflow.state / "bad-answer").write_text(answer)

    forecast = demand.write()

    assert forecast["ordinary"] == 2
    assert forecast["skipped_runs"] == 1


@pytest.mark.parametrize("failure", ["jobs", "run-list", "all-jobs"])
def test_the_tick_survives_forecast_read_failures(demand: Demand, failure: str) -> None:
    workflow = demand.workflow
    workflow.add_worktree(8)
    workflow.running("ticket-8", held=100 * 1024 * 1024)
    workflow.screen("Working\n  ✻ Thinking…\n", "ticket-8")
    demand.pull(8, runs=[_ci(81, 9), _ci(80, 2)])
    # Match the branch supplied by the tick's real snapshot.
    demand.runs[workflow.branch(8)] = demand.runs.pop("ticket/change-8")
    demand.prepare()
    demand.forecast.parent.mkdir(parents=True)
    demand.forecast.write_text('{"at": 1, "ordinary": 4, "docker": 0}\n')
    before = demand.forecast.read_bytes()
    failed_path = {"jobs": "/runs/81/", "run-list": "/runs?", "all-jobs": "/attempts/1/jobs"}
    (workflow.state / "fail-path").write_text(failed_path[failure])

    done = workflow.run_script("tick", env=AT)

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.wake_log.exists()
    if failure == "jobs":
        forecast = json.loads(demand.forecast.read_text())
        assert forecast["ordinary"] == 2
        assert forecast["skipped_runs"] == 1
    else:
        assert demand.forecast.read_bytes() == before
        reason = "the run list could not be read" if failure == "run-list" else "no runs could be read"
        assert f"CI demand: {reason}; the last forecast is kept\n" in done.stdout


def test_a_paused_budget_still_returns_75_and_keeps_the_forecast(demand: Demand) -> None:
    demand.pull(7, draft=False, runs=[_ci(71, 2)])
    demand.write()
    before = demand.forecast.read_bytes()
    budget = demand.forecast.parent / "github-budget/budget.json"
    budget.write_text(json.dumps({"core": {"remaining": 0, "reset": 4_000_000_000}}))

    demand.write(code=75)

    assert demand.forecast.read_bytes() == before


@pytest.mark.parametrize("fault", ["TypeError", "KeyError"])
def test_a_programming_fault_still_fails_the_tick(demand: Demand, fault: str) -> None:
    script = demand.workflow.repo / "scripts/workflow/ci_demand.py"
    text = script.read_text()
    script.write_text(text.replace("runs = Runs()", f"raise {fault}('broken forecast')"))

    done = demand.workflow.run_script("tick", env=AT)

    assert done.returncode == 1
    assert "CI demand: 'broken forecast'" in done.stderr or "CI demand: broken forecast" in done.stderr
    assert demand.workflow.wake_log.exists()


def test_a_forecast_write_fault_still_fails_the_tick(demand: Demand) -> None:
    demand.forecast.mkdir(parents=True)

    done = demand.workflow.run_script("tick", env=AT)

    assert done.returncode == 1
    assert "CI demand:" in done.stderr
    assert demand.workflow.wake_log.exists()


def test_the_forecast_is_written_whole_for_its_owner_alone(demand: Demand) -> None:
    demand.write()

    mode = stat.S_IMODE(demand.forecast.stat().st_mode)
    assert mode == 0o600
    assert [
        path.name for path in demand.forecast.parent.iterdir() if path.name.startswith(".")
    ] == []


def test_the_tick_counts_a_session_busy_before_it(workflow: Workflow) -> None:
    workflow.add_worktree(8)
    workflow.running("ticket-8", held=100 * 1024 * 1024)
    workflow.screen("Working\n  ✻ Thinking…\n", "ticket-8")

    done = workflow.run_script("tick", env=AT)

    assert done.returncode == 0, done.stdout + done.stderr
    forecast = json.loads((workflow.root / "home/.local/state/workflow/ci-demand.json").read_text())
    assert forecast["pulls"]["busy"] == [8]


def test_the_tick_writes_the_forecast_each_turn(workflow: Workflow) -> None:
    # Ready, its checks not started: the tick starts its reviewer, which is busy.
    workflow.add_pull_request(7, draft=False)

    done = workflow.run_script("tick", env=AT)

    assert done.returncode == 0, done.stdout + done.stderr
    forecast = json.loads((workflow.root / "home/.local/state/workflow/ci-demand.json").read_text())
    assert forecast["at"] == NOW
    assert 7 in forecast["pulls"]["ready"] + forecast["pulls"]["busy"]
    assert "about 0 ordinary and 0 Docker jobs within 30 minutes" in done.stdout


def test_the_tick_goes_on_when_the_forecast_fails(workflow: Workflow) -> None:
    forecast = Path(workflow.repo / "scripts/workflow/ci_demand.py")
    forecast.write_text("import sys\nsys.exit(1)\n")

    done = workflow.run_script("tick", env=AT)

    assert done.returncode == 1
    # The wake, which comes after it, still ran.
    assert workflow.wake_log.exists()


BALANCE_LOG = "home/.local/state/workflow/runner-balance.log"
BALANCED = (
    "2026-10-24T07:48:00Z added an ordinary slot (3 to 4); queued ordinary 5 (oldest 6 min), "
    "docker 0; forecast none; load 2.10 of 6; memory 20.5 GB"
)


def test_the_tick_notes_the_balancers_last_change_once(workflow: Workflow) -> None:
    log = workflow.root / BALANCE_LOG
    log.parent.mkdir(parents=True)
    log.write_text("2026-10-24T07:30:00Z an older change\n" + BALANCED + "\n")
    told = workflow.repo / ".local/workflow/runner-balance.told"
    pending = workflow.repo / ".local/workflow/runner-balance.pending"

    assert workflow.run_script("tick", env=AT).returncode == 0
    [noted] = pending.iterdir()
    assert json.loads(noted.read_text()) == {"line": BALANCED}
    noted.unlink()
    assert workflow.run_script("tick", env=AT).returncode == 0

    assert not list(pending.iterdir())
    assert told.read_text() == BALANCED + "\n"


def test_a_balance_change_older_than_a_day_is_not_announced(workflow: Workflow) -> None:
    log = workflow.root / BALANCE_LOG
    log.parent.mkdir(parents=True)
    log.write_text("2026-10-22T07:30:00Z added an ordinary slot (3 to 4); queued ordinary 1\n")

    assert workflow.run_script("tick", env=AT).returncode == 0

    assert not (workflow.repo / ".local/workflow/runner-balance.pending").exists()


def test_the_wake_names_the_last_balance_change_once_as_a_change(workflow: Workflow) -> None:
    workflow.orchestrator("idle")
    first = {"WORKFLOW_NOW": "1791932400", "TZ": "UTC"}  # 13 October, 23:00
    later = {"WORKFLOW_NOW": "1791934200", "TZ": "UTC"}  # 23:30
    assert workflow.run_script("wake", env=first).returncode == 0
    assert workflow.run_script("wake", env=later).returncode == 0
    assert len(workflow.sent()) == 1  # nothing changed, nothing told
    pending = workflow.repo / ".local/workflow/runner-balance.pending"
    pending.mkdir(parents=True)
    (pending / "balance.json").write_text(json.dumps({"line": BALANCED}))

    done = workflow.run_script("wake", env={"WORKFLOW_NOW": "1791936000", "TZ": "UTC"})

    assert done.returncode == 0, done.stderr
    assert len(workflow.sent()) == 2
    assert f"Runner balance, last change: {BALANCED}." in workflow.sent()[-1]
    assert not list(pending.iterdir())


def test_a_spare_or_main_runners_job_is_no_slots(demand: Demand) -> None:
    labels = [ORDINARY, DOCKER, "example-app-home-main", "example-app-home-standby", "x-planner"]
    (demand.workflow.repo / "shipline.json").write_text(json.dumps({"runner": {"labels": labels}}))
    demand.cloud(7, "working")
    jobs = [_job(label) for label in labels]
    demand.pull(7, runs=[(_run(71, "ci.yml"), jobs)])

    forecast = demand.write()

    assert forecast is not None
    assert (forecast["ordinary"], forecast["docker"]) == (1, 1)


def test_a_paused_budget_does_not_fail_the_tick(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False, labels=())
    forecast = Path(workflow.repo / "scripts/workflow/ci_demand.py")
    forecast.write_text("import sys\nsys.exit(75)\n")

    done = workflow.run_script("tick", env=AT)

    assert done.returncode == 0, done.stdout + done.stderr
