"""Independent cancellation-window probes for PR 81; run this file alone."""

import fcntl
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest

from workflow.merge_gate_fixtures import (
    GitHub,
    _REPO,
    _main_ci_run,
    _main_jobs,
    _receipt,
    _squash_with_pending_main,
    gate,
    github,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _status(github: GitHub, *, tick: bool = False) -> str:
    done = github.workflow.run_script("main-status", *(["--tick-main-status"] if tick else []))
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _listed(github: GitHub) -> Path:
    _squash_with_pending_main(github)
    assert _status(github, tick=True).startswith("main: green")
    path = github.workflow.repo / ".local/workflow/main-tree-green.json"
    assert _receipt(github)["cancelled_inherited"]
    return path


def test_daily_cancellation_progress_and_late_failure(github: GitHub) -> None:
    path = _listed(github)
    # Cancellation is asynchronous: first one shard, then the summary, then the run.
    for summary, running in [(None, True), ("failure", True), ("failure", False)]:
        _main_ci_run(github).update(
            status="in_progress" if running else "completed",
            conclusion=None if running else "cancelled",
        )
        _main_jobs(
            github,
            ("CI result", "in_progress" if summary is None else "completed", summary),
            ("CI: server tests (1 of 3)", "completed", "cancelled"),
            ("CI: server tests (2 of 3)", "completed", "success"),
            ("CI: server tests (3 of 3)", "in_progress" if running else "completed",
             None if running else "cancelled"),
        )
        assert "inherited from CI run 20" in _status(github, tick=True)
        assert _status(github).startswith("main: green")
    assert len(github.workflow.calls("gh run cancel")) == 1
    assert path.stat().st_mode & 0o077 == 0
    # A failed part can arrive after the earlier read of the cancellation.
    _main_jobs(
        github,
        ("CI result", "completed", "failure"),
        ("CI: server tests (1 of 3)", "completed", "cancelled"),
        ("CI: server tests (2 of 3)", "completed", "success"),
        ("CI: server tests (3 of 3)", "completed", "failure"),
    )
    assert _status(github).startswith("main: red")


@pytest.mark.parametrize("mismatch", ["run", "attempt", "tree", "head"])
def test_receipt_is_bound_to_the_exact_run_attempt_and_tree(
    github: GitHub, mismatch: str
) -> None:
    path = _listed(github)
    receipt = _receipt(github)
    if mismatch in ("run", "attempt"):
        receipt["cancelled_inherited"][0][mismatch] += 1
    elif mismatch == "tree":
        receipt["cancelled_inherited"][0]["tree"] = "0" * 40
    elif mismatch == "head":
        receipt["main"] = "0" * 40
    path.write_text(json.dumps(receipt))
    _main_jobs(github, ("CI: web", "completed", "cancelled"))
    assert _status(github).startswith("main: red")


def test_a_foreign_branch_run_cannot_change_main_health(github: GitHub) -> None:
    _listed(github)
    _main_ci_run(github)["head_branch"] = "ticket/example"
    _main_jobs(github, ("CI: web", "completed", "cancelled"))
    # Main's run filter excludes this result; the validated squash receipt remains usable.
    assert _status(github).startswith("main: green")


@pytest.mark.parametrize("receipt_state", ["missing", "truncated", "old"])
def test_missing_or_old_receipt_cannot_excuse_cancelled_jobs(
    github: GitHub, receipt_state: str
) -> None:
    path = _listed(github)
    receipt = _receipt(github)
    if receipt_state == "missing":
        path.unlink()
    elif receipt_state == "truncated":
        path.write_text('{"main":')
    else:
        receipt.pop("cancelled_inherited")
        path.write_text(json.dumps(receipt))
    _main_jobs(github, ("CI: web", "completed", "cancelled"))
    assert _status(github).startswith("main: red")


@pytest.mark.parametrize("conclusion", [None, "failure", "cancelled"])
def test_partial_rerun_does_not_reuse_the_cancelled_attempt(
    github: GitHub, conclusion: str | None
) -> None:
    _listed(github)
    _main_ci_run(github).update(run_attempt=2, status="in_progress", conclusion=None)
    _main_jobs(
        github,
        ("CI result", "in_progress", None),
        ("CI: server tests (1 of 2)", "completed", "success"),
        ("CI: server tests (2 of 2)", "completed" if conclusion else "in_progress", conclusion),
    )
    assert _status(github, tick=True).startswith("main: green" if conclusion is None else "main: red")
    assert len(github.workflow.calls("gh run cancel")) == 1
    assert _receipt(github)["cancelled_inherited"][0]["attempt"] == 1


def test_missing_part_and_summary_do_not_hide_a_late_real_failure(github: GitHub) -> None:
    _listed(github)
    _main_jobs(github, ("CI: server tests (1 of 2)", "completed", "cancelled"))
    assert _status(github).startswith("main: green")
    _main_jobs(
        github,
        ("CI: server tests (1 of 2)", "completed", "cancelled"),
        ("CI: server tests (2 of 2)", "completed", "timed_out"),
    )
    assert _status(github).startswith("main: red")


def test_two_ticks_cancel_once_and_leave_a_complete_private_receipt(github: GitHub) -> None:
    _squash_with_pending_main(github)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_status, github, tick=True) for _ in range(2)]
        assert all(future.result(timeout=45).startswith("main: green") for future in futures)
    receipt = _receipt(github)
    assert receipt["cancelled_inherited"] == [
        {"run": 10, "attempt": 1, "tree": receipt["tree"]}
    ]
    assert len(github.workflow.calls("gh run cancel")) == 1
    _main_jobs(github, ("CI: web", "completed", "cancelled"))
    assert _status(github).startswith("main: green")


def test_busy_merge_lock_keeps_tick_responsive_without_cancelling(github: GitHub) -> None:
    _squash_with_pending_main(github)
    path = github.workflow.repo / ".local/workflow/merge.lock"
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        assert _status(github, tick=True).startswith("main: green")
        assert time.monotonic() - started < 15
        assert not github.workflow.calls("gh run cancel")
    assert _status(github, tick=True).startswith("main: green")
    assert len(github.workflow.calls("gh run cancel")) == 1


def test_github_outage_after_receipt_is_written_remains_retryable(github: GitHub) -> None:
    _squash_with_pending_main(github)
    github.cancel_unavailable = [10]
    github.save()
    assert _status(github, tick=True).startswith("main: green")
    assert _receipt(github)["cancelled_inherited"]
    assert len(github.workflow.calls("gh run cancel")) == 1
    assert _status(github, tick=True).startswith("main: green")
    assert len(github.workflow.calls("gh run cancel")) == 2
    _main_jobs(github, ("CI: web", "completed", "cancelled"))
    assert _status(github).startswith("main: green")


def test_unreadable_jobs_never_inherit_green(github: GitHub) -> None:
    _listed(github)
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"] = None
    github.save()
    assert _status(github).startswith("main: unknown")


def test_failure_on_a_later_jobs_page_is_not_hidden(github: GitHub) -> None:
    _listed(github)
    github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"] = {"_pages": [
        {"jobs": [{"id": 100 + index, "name": f"CI: server tests ({index} of 101)",
                   "status": "completed", "conclusion": "cancelled"}
                  for index in range(1, 101)]},
        {"jobs": [{"id": 201, "name": "CI: server tests (101 of 101)",
                   "status": "completed", "conclusion": "failure"}]},
    ]}
    github.save()
    assert _status(github).startswith("main: red")


@pytest.mark.parametrize("failure", ["failure", "timed_out", "action_required", "startup_failure", "stale", "neutral"])
@pytest.mark.parametrize("listed", [False, True])
def test_real_failure_wins_in_every_shard_order(
    gate: Any, monkeypatch: pytest.MonkeyPatch, failure: str, listed: bool
) -> None:
    monkeypatch.setattr(gate, "cancelled_inherited", lambda run: listed)
    monkeypatch.setattr(gate, "lender_replanning", lambda *args, **kwargs: False)
    run = {"id": 10, "status": "in_progress", "conclusion": None}
    for order in permutations(["cancelled", "success", failure]):
        parts = [
            {"name": f"CI: server tests ({index} of 3)", "status": "completed", "conclusion": value}
            for index, value in enumerate(order, 1)
        ]
        grouped = gate.checks(parts)
        assert grouped[0]["conclusion"] == failure
        assert gate.verdict(run, grouped) == ("red", ["CI: server tests"])


@pytest.mark.parametrize("fault", ["timeout", "budget"])
def test_cancel_timeout_or_budget_wait_preserves_receipt_then_retries(
    gate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    run = {"id": 10, "head_sha": "a" * 40, "status": "in_progress", "run_attempt": 1}
    receipt = {"tree": "b" * 40, "ci_run": 20}
    calls = []

    def command(*args: str, **kwargs: Any) -> Any:
        calls.append(args)
        saved = json.loads((tmp_path / "main-tree-green.json").read_text())
        assert saved["cancelled_inherited"] == [{"run": 10, "attempt": 1, "tree": "b" * 40}]
        if len(calls) == 1:
            if fault == "timeout":
                raise gate.TimedOut("command timed out", "", 1)
            raise gate.BudgetWaiting("budget unavailable")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(gate, "command", command)
    with pytest.raises(gate.Waiting):
        gate.cancel_inherited("a" * 40, receipt, [run], tmp_path)
    assert not (tmp_path / "cancel-10").exists()
    gate.cancel_inherited("a" * 40, receipt, [run], tmp_path)
    assert len(calls) == 2
    assert (tmp_path / "main-tree-green.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("age,attempt,head,expected", [
    (0, 1, "a", "pending"), (179, 1, "a", "pending"),
    (181, 1, "a", "red"), (-1, 1, "a", "red"),
    (0, 2, "a", "red"), (0, 1, "b", "red"),
])
def test_lender_window_uses_real_note_and_keeps_time_head_attempt_bounds(
    gate: Any, monkeypatch: pytest.MonkeyPatch,
    age: int, attempt: int, head: str, expected: str,
) -> None:
    monkeypatch.setattr(gate, "cancelled_inherited", lambda run: False)
    path = Path.home() / ".local/state/ci-overflow/replanned.json"
    path.parent.mkdir(parents=True)
    now = time.time()
    path.write_text(json.dumps({"10": {
        "state": "cancelled", "updated_at": now - age, "head": "a" * 40,
        "attempt": 1, "jobs": [101],
    }}))
    monkeypatch.setattr(time, "time", lambda: now)
    run = {"id": 10, "head_sha": head * 40, "run_attempt": attempt,
           "status": "in_progress", "conclusion": None}
    parts = [{"name": "CI: web", "status": "completed", "conclusion": "cancelled"}]
    assert gate.verdict(run, parts)[0] == expected
    parts.append({"name": "CI: server tests", "status": "completed", "conclusion": "failure"})
    assert gate.verdict(run, parts)[0] == "red"


def test_configured_nonstandard_check_names_keep_real_failures(
    gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "CONFIG", replace(
        gate.CONFIG, checks=replace(gate.CONFIG.checks, server_tests="Application tests")
    ))
    monkeypatch.setattr(gate, "cancelled_inherited", lambda run: True)
    monkeypatch.setattr(gate, "lender_replanning", lambda *args, **kwargs: False)
    parts = [{"name": f"Application tests ({index} of 2)", "status": "completed",
              "conclusion": result}
             for index, result in enumerate(["cancelled", "failure"], 1)]
    assert gate.verdict(
        {"id": 10, "status": "in_progress", "conclusion": None}, gate.checks(parts)
    ) == ("red", ["Application tests"])


def test_a_lost_rollup_check_is_found_after_its_cancelled_run_finishes(
    gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "lender_replanning", lambda *args, **kwargs: False)
    monkeypatch.setenv("WORKFLOW_NOW", "1800000000")
    pull = {"headRefOid": "a" * 40, "statusCheckRollup": [{
        "__typename": "CheckRun", "name": "CI result", "status": "IN_PROGRESS",
        "conclusion": None, "startedAt": "2026-01-01T00:00:00Z",
        "detailsUrl": "https://github.com/example-org/example-app/actions/runs/10/job/101",
    }]}
    finished = {"id": 10, "status": "completed", "conclusion": "cancelled",
                "updated_at": "2026-01-01T01:00:00Z"}
    assert gate.lost_check(pull, [finished]) == {
        "check": "CI result", "run": 10, "conclusion": "cancelled",
    }
