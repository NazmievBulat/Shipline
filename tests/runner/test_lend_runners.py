"""The lender (`runner/lend_runners.py`) lends one of main's idle runners to pull request jobs only
while neither main nor the merge gate's run of a prefix has a job queued or running for main's
runners: the merge line's scratch runs are main's own (examples/ci.yml, examples/e2e.yml). A
dispatched run counts by what its jobs ask for, main's label, whatever its branch is called."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
# The lender imports the budget beside it once installed; here it is in scripts/workflow.
sys.path.insert(0, str(_ROOT / "scripts/workflow"))
_SPEC = importlib.util.spec_from_file_location("lend_runners", _ROOT / "runner/lend_runners.py")
assert _SPEC and _SPEC.loader
lend_runners = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lend_runners)

_REPOSITORY = "example-owner/example-app"
_API = f"repos/{_REPOSITORY}"
_PREFIX = "workflow/merge-batch-"
_BATCH = f"{_PREFIX}0123456789abcdef0123456789abcdef"
LABELS = lend_runners.Labels(
    home="example-app-home", first="example-app-home-first", main="example-app-home-main"
)
_DOCKER = "example-app-home-docker"
_E2E, _IMAGES = ".github/workflows/e2e.yml", ".github/workflows/images.yml"


def _runner(identity: int, name: str, *labels: str) -> dict[str, Any]:
    return {
        "id": identity,
        "name": name,
        "status": "online",
        "busy": False,
        "labels": [{"name": label} for label in ["self-hosted", *labels]],
    }


def _run(
    identity: int,
    event: str,
    branch: str,
    path: str = ".github/workflows/ci.yml",
    status: str = "in_progress",
) -> Any:
    return {
        "id": identity,
        "event": event,
        "head_branch": branch,
        "path": path,
        "status": status,
        "head_sha": "1" * 40,
    }


def _job(identity: int, status: str, *labels: str) -> dict[str, Any]:
    return {"id": identity, "name": "CI: server tests", "status": status, "labels": list(labels)}


class GitHub:
    """Answers the lender's reads from a table, and writes down every change it asks for."""

    def __init__(self, runs: dict[str, list[dict[str, Any]]], jobs: dict[int, Any]) -> None:
        self.runs = runs
        self.jobs = jobs
        self.changes: list[tuple[str, ...]] = []
        self.runners = [
            # The first of main's runners is lent already, idle; the second is not.
            _runner(1, "home-main", LABELS.main, LABELS.home, LABELS.first),
            _runner(2, "home-main-2", LABELS.main),
        ]

    def __call__(self, *arguments: str) -> Any:
        if arguments[0] == "-X":
            self.changes.append(arguments)
            return None
        path = arguments[0]
        if path.startswith(f"{_API}/actions/runners?"):
            return {"runners": self.runners}
        if path.startswith(f"{_API}/actions/runs?"):
            query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
            # Newest first, as GitHub lists them.
            found = [
                run
                for run in sorted(self.runs.get(query["event"], []), key=lambda run: -run["id"])
                if ("branch" not in query or run["head_branch"] == query["branch"])
                and ("status" not in query or run["status"] == query["status"])
            ]
            return {"workflow_runs": found[: int(query["per_page"])]}
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": self.jobs[int(path.split("/")[-2])]}
        raise AssertionError(f"unexpected read {arguments}")


def _round(github: GitHub, tmp_path: Path) -> list[tuple[str, ...]]:
    first = lend_runners.First(
        github,
        _REPOSITORY,
        tmp_path / "train-first.json",
        tmp_path / "replanned.json",
        pause=lambda seconds: None,
    )
    lend_runners.round_once(
        github,
        _REPOSITORY,
        LABELS,
        1,
        first,
        tmp_path / "first.json",
        "home-main",
    )
    return github.changes


_TAKEN_BACK = [
    ("-X", "DELETE", f"{_API}/actions/runners/1/labels/{LABELS.home}"),
    ("-X", "DELETE", f"{_API}/actions/runners/1/labels/{LABELS.first}"),
]

# (runs by event, jobs by run, what the lender changes)
_QUEUES = {
    "nothing under way": ({}, {}, []),
    "a merge-line job queued": (
        {"workflow_dispatch": [_run(10, "workflow_dispatch", _BATCH)]},
        {10: [_job(1, "queued", LABELS.main)]},
        _TAKEN_BACK,
    ),
    "a merge-line run queued": (
        {"workflow_dispatch": [_run(10, "workflow_dispatch", _BATCH, status="queued")]},
        {10: [_job(1, "queued", LABELS.main)]},
        _TAKEN_BACK,
    ),
    # A repository that names another scratch prefix: its runs ask for main's label all the same.
    "a merge-line job queued on another prefix": (
        {"workflow_dispatch": [_run(10, "workflow_dispatch", "shipline/prefix-0123")]},
        {10: [_job(1, "queued", LABELS.main)]},
        _TAKEN_BACK,
    ),
    "a merge-line job running": (
        {"workflow_dispatch": [_run(10, "workflow_dispatch", _BATCH)]},
        {10: [_job(1, "in_progress", LABELS.main)]},
        _TAKEN_BACK,
    ),
    "a merge-line browser job queued": (
        {"workflow_dispatch": [_run(10, "workflow_dispatch", _BATCH, _E2E)]},
        {10: [_job(1, "completed", LABELS.main), _job(2, "queued", LABELS.main)]},
        _TAKEN_BACK,
    ),
    "a push to main queued": (
        {"push": [_run(11, "push", "main")]},
        {11: [_job(1, "queued", LABELS.main)]},
        _TAKEN_BACK,
    ),
    "a push to main running": (
        {"push": [_run(11, "push", "main")]},
        {11: [_job(1, "in_progress", LABELS.main)]},
        _TAKEN_BACK,
    ),
    # The image check's Docker jobs never ask for main's runners.
    "a merge-line Docker job queued": (
        {"workflow_dispatch": [_run(10, "workflow_dispatch", _BATCH, _IMAGES)]},
        {10: [_job(1, "queued", _DOCKER)]},
        [],
    ),
    "a merge-line run whose jobs are done": (
        {"workflow_dispatch": [_run(10, "workflow_dispatch", _BATCH)]},
        {10: [_job(1, "completed", LABELS.main)]},
        [],
    ),
    # A dispatch on any other branch runs on GitHub's own runners, and is none of main's.
    "a dispatch on another branch queued": (
        {"workflow_dispatch": [_run(10, "workflow_dispatch", "feature/example")]},
        {10: [_job(1, "queued", "ubuntu-latest")]},
        [],
    ),
}


@pytest.mark.parametrize("queue", _QUEUES)
def test_main_s_runner_is_lent_only_while_main_and_the_merge_line_need_none(
    tmp_path: Path, queue: str
) -> None:
    runs, jobs, expected = _QUEUES[queue]

    assert _round(GitHub(runs, jobs), tmp_path) == expected


def test_an_unlent_runner_is_not_lent_while_a_merge_line_job_is_queued(tmp_path: Path) -> None:
    github = GitHub(
        {"workflow_dispatch": [_run(10, "workflow_dispatch", _BATCH)]},
        {10: [_job(1, "queued", LABELS.main)]},
    )
    github.runners = [_runner(1, "home-main", LABELS.main), _runner(2, "home-main-2", LABELS.main)]

    assert _round(github, tmp_path) == []

    github.jobs[10] = [_job(1, "completed", LABELS.main)]
    assert _round(github, tmp_path) == [
        ("-X", "POST", f"{_API}/actions/runners/1/labels", "-f", f"labels[]={LABELS.home}"),
        ("-X", "POST", f"{_API}/actions/runners/1/labels", "-f", f"labels[]={LABELS.first}"),
    ]
    published = json.loads((tmp_path / "first.json").read_text())
    assert (published["free"], published["first_free"]) == (1, 1)


def test_a_merge_line_run_behind_many_finished_dispatches_still_counts(tmp_path: Path) -> None:
    """GitHub lists the newest runs first: finished ones never push an active one off the page."""
    finished = [
        _run(100 + n, "workflow_dispatch", "feature/example", status="completed")
        for n in range(30)
    ]
    github = GitHub(
        {"workflow_dispatch": [_run(10, "workflow_dispatch", _BATCH), *finished]},
        {10: [_job(1, "queued", LABELS.main)]},
    )

    assert _round(github, tmp_path) == _TAKEN_BACK
