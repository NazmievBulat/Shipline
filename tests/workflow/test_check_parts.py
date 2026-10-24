"""A CI job run in parts is one check to the merge gate (scripts/workflow/merge_gate.py)."""

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "workflow"


@pytest.fixture(scope="module")
def checks() -> Any:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("merge_gate").checks
    finally:
        sys.path.remove(str(_SCRIPTS))


def _job(name: str, conclusion: str | None, labels: tuple[str, ...] = ("home",)) -> dict[str, Any]:
    status = "in_progress" if conclusion is None else "completed"
    return {"id": 1, "name": name, "status": status, "conclusion": conclusion, "labels": [*labels]}


def _parts(*conclusions: str | None) -> list[dict[str, Any]]:
    count = len(conclusions)
    return [
        _job(f"CI: server tests ({number} of {count})", conclusion)
        for number, conclusion in enumerate(conclusions, start=1)
    ]


@pytest.mark.parametrize(
    "parts,status,conclusion",
    [
        (_parts("success", "success"), "completed", "success"),
        (_parts("success"), "completed", "success"),
        # One part failed while the other runs: the check failed, and is still running.
        (_parts("failure", None), "in_progress", "failure"),
        (_parts("success", None), "in_progress", None),
        (_parts("success", "cancelled"), "completed", "cancelled"),
        (_parts("skipped", "skipped"), "completed", "skipped"),
        # Parts of one check never differ like this; when they do, it has not passed.
        (_parts("success", "skipped"), "completed", None),
        # A part missing, or parts of two ways of dealing: not passed, waited for.
        (_parts("success", "success")[:1], "in_progress", None),
        ([*_parts("success", "success"), *_parts("success")], "in_progress", None),
        ([_job("CI: server tests (1 of 2)", "success"), _job("CI: server tests (1 of 2)", "success")],
         "in_progress", None),
        # A failed part is a failure, beside whatever else passed. GitHub lists the jobs of a
        # run's latest attempt only, so a part that failed in an earlier one is not among them.
        ([*_parts("success", "success"), *_parts("failure")], "in_progress", "failure"),
    ],
)  # fmt: skip
def test_the_parts_of_a_job_are_one_check(
    checks: Any, parts: list[dict[str, Any]], status: str, conclusion: str | None
) -> None:
    (check,) = checks([*parts])

    assert check["name"] == "CI: server tests"
    assert (check["status"], check["conclusion"]) == (status, conclusion)


def test_other_jobs_are_left_as_they_are(checks: Any) -> None:
    jobs = [
        _job("CI: server tests", "skipped"),
        _job("CI result", "success"),
        # The image workflow's parts are its own: the gate reads them as before.
        _job("Docker images: browser tests (1 of 3)", "success"),
    ]

    assert checks(jobs) == jobs


def test_a_check_ran_at_home_only_when_every_part_did(checks: Any) -> None:
    """The gate retries a failed main run by itself only when what failed ran at home."""
    home = ("self-hosted", "example-app-home-main")
    parts = [
        _job("CI: server tests (1 of 2)", "success", home),
        _job("CI: server tests (2 of 2)", "failure", home),
    ]
    assert checks(parts)[0]["labels"] == sorted(home)

    parts[0]["labels"] = ["ubuntu-latest"]
    assert checks(parts)[0]["labels"] == []
