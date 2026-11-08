"""Eight-shard failure and rerun replays against the real grouping and proof code."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE, HEAD, TREE, SQUASH, MERGE = (digit * 40 for digit in "abcde")
API = "repos/example-org/example-app"
SUITE = "CI: server tests"
OK = {"status": "completed", "conclusion": "success"}


@pytest.fixture(scope="module")
def modules(tmp_path_factory):
    config = tmp_path_factory.mktemp("config") / "shipline.json"
    config.write_text(json.dumps({"repository": "example-org/example-app", "checks": {"ci_result": "CI"}}))
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(ROOT / "scripts/workflow"))
        patch.setenv("SHIPLINE_CONFIG", str(config))
        patch.setenv("SHIPLINE_REPO", str(ROOT))
        workflow_config = importlib.import_module("workflow_config")
        workflow_config.current.cache_clear()
        proof = importlib.import_module("ci_proof")
        gate = importlib.import_module("merge_gate")
        # Hosted runners have no lender. Keep its machine-specific state out of these replays.
        patch.setattr(gate, "lender_replanning", lambda *args, **kwargs: False)
        patch.setattr(gate, "cancelled_inherited", lambda *args, **kwargs: False)
        yield gate, proof
        workflow_config.current.cache_clear()


def parts():
    return [
        {"id": 1, "name": "CI: what to run", **OK},
        *({"id": n + 10, "name": f"{SUITE} ({n} of 8)", "labels": ["ubuntu-latest"], **OK}
          for n in range(1, 9)),
        {"id": 2, "name": "CI", **OK},
    ]


def corrupt(jobs, shard, fault):
    target = jobs[shard]
    if fault == "missing":
        jobs.remove(target)
    elif fault == "duplicate":
        jobs.append({**target, "id": 100 + shard})
    elif fault in ("queued", "in_progress"):
        target.update(status=fault, conclusion=None)
    else:
        target.update(conclusion=fault)


def run():
    return {"id": 20, "head_sha": HEAD, "run_attempt": 1, **OK}


@pytest.mark.parametrize("shard", range(1, 9))
@pytest.mark.parametrize("fault", ["missing", "duplicate", "queued", "in_progress",
                                   "failure", "cancelled", "timed_out"])
def test_no_single_lost_late_failed_or_repeated_shard_can_make_gate_green(modules, shard, fault):
    gate, proof = modules
    jobs = parts()
    corrupt(jobs, shard, fault)
    assert SUITE not in proof.passed_jobs(jobs)
    grouped = gate.checks(jobs)
    state, _ = gate.verdict(run(), grouped)
    assert state == ("red" if fault in ("failure", "cancelled", "timed_out") else "pending")


def proof_api(jobs):
    receipt = {"base": BASE, "tree": TREE, "sources": [
        {"workflow": "ci", "run": 20, "jobs": [SUITE, "CI"]}
    ]}
    data = {
        f"{API}/git/commits/{SQUASH}": {
            "message": "ci: example\n\nWorkflow-Proof: " + json.dumps(receipt),
            "tree": {"sha": TREE}, "parents": [{"sha": BASE}],
        },
        f"{API}/actions/runs/20": {
            **run(), "path": ".github/workflows/ci.yml",
            "head_repository": {"full_name": "example-org/example-app"},
        },
        f"{API}/actions/runs/20/jobs?per_page=100": {"jobs": jobs},
        f"{API}/check-runs/1/annotations?per_page=100": [
            {"title": "Merge tested", "message": MERGE},
            {"title": "Server tests", "message": "whole"},
        ],
        f"{API}/git/commits/{MERGE}": {
            "tree": {"sha": TREE}, "parents": [{"sha": BASE}, {"sha": HEAD}],
        },
    }

    def read(path, key=None):
        answer = data[path]
        return deepcopy(answer[key] if key else answer)

    return data, read


@pytest.mark.parametrize("shard", range(1, 9))
@pytest.mark.parametrize("fault", ["missing", "duplicate", "queued", "in_progress",
                                   "failure", "cancelled", "timed_out", "skipped"])
def test_main_cannot_inherit_incomplete_or_unsuccessful_eight_shard_proof(modules, shard, fault):
    _, proof = modules
    jobs = parts()
    corrupt(jobs, shard, fault)
    _, read = proof_api(jobs)
    inherited = proof.inherited(SQUASH, "ci", read)
    assert SUITE not in inherited
    assert inherited["CI"] == 20


def test_all_eight_pass_and_two_readers_observe_same_proof_without_mutation(modules):
    gate, proof = modules
    data, read = proof_api(parts())
    before = deepcopy(data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: proof.inherited(SQUASH, "ci", read), range(2)))
    assert answers == [{SUITE: 20, "CI": 20}] * 2
    assert data == before
    assert gate.verdict(run(), gate.checks(parts()))[0] == "green"


@pytest.mark.parametrize("lost", [False, True])
def test_failed_job_rerun_uses_carried_planner_notes_and_requires_eighth_shard(modules, lost):
    gate, proof = modules
    jobs = parts()
    data, read = proof_api(jobs)
    notes = data.pop(f"{API}/check-runs/1/annotations?per_page=100")
    previous = {
        **jobs[0], "head_sha": HEAD, "run_id": 20, "run_attempt": 1,
        "created_at": "2026-01-01T00:00:00Z", "started_at": "2026-01-01T00:00:01Z",
        "completed_at": "2026-01-01T00:00:02Z",
    }
    jobs[0].update(previous, id=101, run_attempt=2, created_at="2026-01-01T00:02:00Z")
    data[f"{API}/check-runs/101/annotations?per_page=100"] = []
    data[f"{API}/check-runs/1/annotations?per_page=100"] = notes
    data[f"{API}/actions/runs/20/attempts/1/jobs?per_page=100"] = {"jobs": [previous]}
    data[f"{API}/actions/runs/20"]["run_attempt"] = 2
    if lost:
        corrupt(jobs, 8, "missing")
    inherited = proof.inherited(SQUASH, "ci", read)
    assert (SUITE in inherited) is not lost
    assert gate.verdict({**run(), "run_attempt": 2}, gate.checks(jobs))[0] == ("pending" if lost else "green")


@pytest.mark.parametrize("fault", ["jobs_unreadable", "notes_unreadable", "fork", "wrong_tree", "no_receipt"])
def test_unreadable_or_untrusted_proof_runs_every_job(modules, fault):
    _, proof = modules
    data, read = proof_api(parts())
    if fault == "fork":
        data[f"{API}/actions/runs/20"]["head_repository"]["full_name"] = "example-fork/example-app"
    elif fault == "wrong_tree":
        data[f"{API}/git/commits/{MERGE}"]["tree"]["sha"] = "f" * 40
    elif fault == "no_receipt":
        data[f"{API}/git/commits/{SQUASH}"]["message"] = "ci: example"

    def flaky(path, key=None):
        if (fault == "jobs_unreadable" and "/jobs?" in path) or (
            fault == "notes_unreadable" and "/annotations?" in path
        ):
            raise proof.Unreadable("example unavailable")
        return read(path, key)

    assert proof.inherited(SQUASH, "ci", flaky) == {}


def test_timed_out_github_read_becomes_unreadable(modules, monkeypatch):
    _, proof = modules

    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(proof.subprocess, "run", timeout)
    with pytest.raises(proof.Unreadable):
        proof.read(f"{API}/actions/runs/20/jobs?per_page=100", "jobs")


@pytest.mark.parametrize("shard", range(1, 9))
@pytest.mark.parametrize("finished_ago,detected", [(599, False), (601, True)])
def test_stale_rollup_detects_lost_check_only_after_grace_period(
    modules, monkeypatch, shard, finished_ago, detected,
):
    from datetime import datetime, UTC

    gate, _ = modules
    now = datetime(2026, 1, 1, 1, tzinfo=UTC).timestamp()
    monkeypatch.setenv("WORKFLOW_NOW", str(now))
    rollup = [
        {"__typename": "CheckRun", "name": part["name"],
         "status": "COMPLETED", "conclusion": "SUCCESS"}
        for part in parts()
    ]
    rollup[shard].update(
        status="IN_PROGRESS", conclusion=None,
        startedAt="2026-01-01T00:00:00Z",
        detailsUrl="https://github.com/example-org/example-app/actions/runs/20/job/30",
    )
    ended = {**run(), "updated_at": datetime.fromtimestamp(now - finished_ago, UTC).isoformat()}
    lost = gate.lost_check({"statusCheckRollup": rollup}, known=[ended])
    assert lost == ({"check": f"{SUITE} ({shard} of 8)", "run": 20, "conclusion": "success"}
                    if detected else None)
