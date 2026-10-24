"""Tests that try to break pull request 22: notes read from an earlier attempt of a re-run.

`merge_gate.job_notes` borrows the notes of an earlier attempt when a passed job of a later
attempt has none. These tests check where that reading is wrong: a job that really ran in the
later attempt and wrote no notes, an earlier copy that is older than another, a failed copy, a
job still running, and a read of an earlier attempt that GitHub could not answer.
"""

import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from workflow.test_merge_when_current import (
    GitHub,
    _two_stale_pulls,
    train_status,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture

_REPO = "repos/example-org/example-app"
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "workflow"


@pytest.fixture
def gate() -> Iterator[Any]:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        yield importlib.import_module("merge_gate")
    finally:
        sys.path.remove(str(_SCRIPTS))


def _stub(gate: Any, monkeypatch: pytest.MonkeyPatch, replies: dict[str, Any]) -> list[str]:
    calls: list[str] = []

    def api(path: str, key: str | None = None, *, paginate: bool = True) -> Any:
        calls.append(path)
        reply = replies[path]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(gate, "api", api)
    return calls


def _notes(gate: Any, job_id: int) -> str:
    return f"{gate.API}/check-runs/{job_id}/annotations?per_page=100"


def _attempt(gate: Any, run: int, attempt: int) -> str:
    return f"{gate.API}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100"


def _job(job_id: int, attempt: int, conclusion: str | None = "success", **more: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": "CI: dependencies",
        "run_id": 30,
        "run_attempt": attempt,
        "head_sha": "a" * 40,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        **more,
    }


# 1. A full re-run: every job ran again in attempt 2. The dependency audit passed clean there
#    and wrote no notes, as a clean audit does. Its passing over main's advisory in attempt 1
#    is older evidence and must not be read as attempt 2's.


def test_a_clean_audit_that_ran_in_a_full_rerun_is_not_read_as_its_older_advisory(
    gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _job(539, 2, started_at="2026-10-24T10:00:00Z")
    first = _job(39, 1, started_at="2026-10-24T08:00:00Z")
    _stub(
        gate,
        monkeypatch,
        {
            _notes(gate, 539): [],
            _attempt(gate, 30, 1): [first],
            _notes(gate, 39): [{"title": "Advisory on main", "message": "GHSA-1 is on main too"}],
        },
    )
    assert gate.strict_jobs([latest])[0]["conclusion"] == "success"


def test_a_fix_whose_full_rerun_passed_its_audit_clean_takes_the_red_main_turn(
    github: GitHub,
) -> None:
    """Main is red on "CI: dependencies". Pull request 8's first attempt passed over an
    advisory main had then; its full re-run, on the current main, passed every job and its
    audit clean. It is the fix of the red main, as it would be with no earlier attempt."""
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(github.base, head, run_id=30)
    jobs = f"{_REPO}/actions/runs/30/jobs?per_page=100"
    first = json.loads(json.dumps(github.api[jobs]["jobs"]))
    first.append({"id": 39, "name": "CI: dependencies", "status": "completed", "conclusion": "success"})
    for job in first:
        job.update(run_id=30, run_attempt=1, head_sha=head, started_at="2026-10-24T08:00:00Z")
    github.api[f"{_REPO}/actions/runs/30/attempts/1/jobs?per_page=100"] = {"jobs": first}
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = [
        {"title": "Advisory on main", "message": "GHSA-1 in vite is on main too"}
    ]
    # Attempt 2 ran every job again: new IDs, the planner's own notes, a clean audit.
    latest = github.api[jobs]["jobs"]
    latest.append({"id": 39, "name": "CI: dependencies", "status": "completed", "conclusion": "success"})
    planner_notes = github.api[f"{_REPO}/check-runs/31/annotations?per_page=100"]
    for job in latest:
        job["id"] = 500 + job["id"]
        job.update(run_id=30, run_attempt=2, head_sha=head, started_at="2026-10-24T10:00:00Z")
    github.api[f"{_REPO}/check-runs/531/annotations?per_page=100"] = planner_notes
    github.api[f"{_REPO}/check-runs/532/annotations?per_page=100"] = []
    github.api[f"{_REPO}/check-runs/539/annotations?per_page=100"] = []
    runs = f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"
    github.api[runs]["workflow_runs"][0]["run_attempt"] = 2
    github.main("failure")
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"] = [
        {"id": 11, "name": "CI result", "status": "completed", "conclusion": "failure"},
        {"id": 12, "name": "CI: dependencies", "status": "completed", "conclusion": "failure"},
    ]
    github.save()
    folder = github.workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))
    assert github.workflow.run_script("main-status").returncode == 0
    assert train_status(github)["first"] == "8"


# 2. Two re-runs: the newest earlier copy that has notes is the one read, not the first.


def test_the_newest_earlier_copy_with_notes_wins(gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    planner = {**_job(731, 3), "name": "CI: what to run"}
    _stub(
        gate,
        monkeypatch,
        {
            _notes(gate, 731): [],
            _attempt(gate, 30, 2): [{**_job(631, 2), "name": "CI: what to run"}],
            _notes(gate, 631): [{"title": "Merge tested", "message": "second"}],
            _attempt(gate, 30, 1): [{**_job(31, 1), "name": "CI: what to run"}],
            _notes(gate, 31): [{"title": "Merge tested", "message": "first"}],
        },
    )
    assert gate.job_notes(planner) == [{"title": "Merge tested", "message": "second"}]


# 3. A copy that failed lends nothing; a job still running borrows nothing.


def test_a_failed_earlier_copy_lends_no_notes(gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        gate,
        monkeypatch,
        {
            _notes(gate, 539): [],
            _attempt(gate, 30, 1): [_job(39, 1, "failure")],
            _notes(gate, 39): [{"title": "Advisory on main", "message": "x"}],
        },
    )
    assert gate.job_notes(_job(539, 2)) == []


def test_a_job_still_running_reads_no_earlier_attempt(
    gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(gate, monkeypatch, {_notes(gate, 539): []})
    assert gate.job_notes(_job(539, 2, None)) == []
    assert calls == [_notes(gate, 539)]


# 4. GitHub cannot answer the read of an earlier attempt: the gate waits, it does not decide.


def test_an_unreadable_earlier_attempt_makes_the_gate_wait(
    gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(
        gate,
        monkeypatch,
        {
            _notes(gate, 539): [],
            _attempt(gate, 30, 1): gate.Waiting("gh api failed: HTTP 502"),
        },
    )
    with pytest.raises(gate.Waiting):
        gate.job_notes(_job(539, 2))
