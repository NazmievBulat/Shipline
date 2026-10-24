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
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

state = Path("@STATE@")
arguments = sys.argv[1:]
with open(state / "calls.log", "a") as log:
    log.write("gh " + " ".join(arguments) + "\\n")
if (state / "fail-gh").exists():
    sys.exit(1)
path = arguments[-1]
api = "repos/example-org/example-app/actions"
if found := re.fullmatch(api + r"/runs/(\\d+)/attempts/(\\d+)/jobs\\?per_page=100", path):
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
        workflow.fake("gh", _FAKE_GH)
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
    ) -> None:
        branch = f"ticket/change-{number}"
        self.pulls[str(number)] = {
            "number": number,
            "state": "OPEN",
            "isDraft": draft,
            "headRefName": branch,
            "statusCheckRollup": checks or [],
        }
        self.runs[branch] = [run for run, _ in runs]
        for run, jobs in runs:
            self.jobs[f"{run['id']}-{run['run_attempt']}"] = jobs

    def cloud(self, number: int, status: str) -> None:
        (self.notes / f"cloud-{number}").write_text(json.dumps({"status": status}))

    def train(self, value: dict[str, Any]) -> None:
        (self.notes / "merge-train.json").write_text(json.dumps(value))

    def write(self, sessions: str = "", *, code: int = 0) -> dict[str, Any] | None:
        state = self.workflow.state
        (state / "runs.json").write_text(json.dumps(self.runs))
        (state / "jobs.json").write_text(json.dumps(self.jobs))
        snapshot = self.workflow.root / "pulls.json"
        snapshot.write_text(json.dumps(self.pulls))
        done = self.workflow.run_python(
            "ci_demand.py", "write", str(self.workflow.repo), str(snapshot), stdin=sessions, env=AT
        )
        assert done.returncode == code, done.stdout + done.stderr
        self.printed = done.stdout
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
    sessions = "ticket-8 busy 100 8G\nreview-9 busy 50 8G\n"

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
    sessions = "ticket-11 idle 100 8G\nreview-13 limit 0 8G  session limit  resets 4pm\n"

    forecast = demand.write(sessions + "hunt-x busy 0 8G\naudit-11 busy 0 8G\n")

    assert forecast is not None
    assert (forecast["ordinary"], forecast["docker"]) == (0, 0)
    assert forecast["pulls"] == {"cloud": [], "busy": [], "ready": [], "prefixes": []}


def test_a_pull_request_counted_twice_pushes_once(demand: Demand) -> None:
    demand.cloud(7, "working")
    demand.pull(7, draft=False, runs=[_ci(71, 2)])

    forecast = demand.write("ticket-7 busy 0 8G\nreview-7 busy 0 8G\n")

    assert forecast is not None
    assert forecast["ordinary"] == 2
    assert forecast["pulls"] == {"cloud": [7], "busy": [], "ready": [], "prefixes": []}


def test_the_merge_line_counts_the_prefixes_still_to_build(demand: Demand) -> None:
    # A window of 3 with one prefix running: the next two eligible members, in the line's order.
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
    assert forecast["pulls"]["prefixes"] == [16, 13]
    assert forecast["ordinary"] == 6


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

    assert demand.write(code=1) == before


def test_the_forecast_is_written_whole_for_its_owner_alone(demand: Demand) -> None:
    demand.write()

    mode = stat.S_IMODE(demand.forecast.stat().st_mode)
    assert mode == 0o600
    assert [path.name for path in demand.forecast.parent.iterdir() if path.name.startswith(".")] == []


def test_the_tick_writes_the_forecast_each_turn(workflow: Workflow) -> None:
    # Ready and without the label: no reviewer starts for it, and its checks have not started.
    workflow.add_pull_request(7, draft=False, labels=())

    done = workflow.run_script("tick", env=AT)

    assert done.returncode == 0, done.stdout + done.stderr
    forecast = json.loads((workflow.root / "home/.local/state/workflow/ci-demand.json").read_text())
    assert forecast["at"] == NOW
    assert forecast["pulls"]["ready"] == [7]
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
    pending = workflow.repo / ".local/workflow/runner-balance.pending"

    assert workflow.run_script("tick", env=AT).returncode == 0
    [noted] = pending.iterdir()
    assert json.loads(noted.read_text()) == {"line": BALANCED}
    noted.unlink()
    assert workflow.run_script("tick", env=AT).returncode == 0

    assert not list(pending.iterdir())


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
