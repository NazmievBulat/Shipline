"""Independent retry, interruption, concurrency and private-state probes for PR 89."""

import importlib
import json
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/workflow"))
    repair = importlib.import_module("check_repair")
    health = importlib.import_module("merge_health")
    github = importlib.import_module("merge_github")
    calls, wakes = [], []

    def command(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    gate = SimpleNamespace(
        CI_PLAN="CI: what to run", E2E_PLAN="Browser: what to run",
        IMAGES_PLAN="Images: what to run", REPOSITORY="example-org/example-app",
        API="repos/example-org/example-app",
        CONFIG=SimpleNamespace(runner=SimpleNamespace(labels=["example-runner-pool"])),
        _failed_jobs=lambda jobs: [j for j in jobs if j["conclusion"] in
                                  ("failure", "cancelled", "timed_out")],
        _rerunnable=health._rerunnable, command=command, wake_log=wakes.append,
        BudgetWaiting=github.BudgetWaiting, Waiting=github.Waiting,
        Refused=github.Refused, transient=lambda error: "timeout" in error,
    )
    monkeypatch.setitem(sys.modules, "merge_gate", gate)
    run = {"id": 20, "run_attempt": 1, "status": "completed", "conclusion": "failure"}
    jobs = [{"id": 21, "name": "CI: server tests", "status": "completed",
             "conclusion": "failure", "labels": ["example-runner-pool"]}]
    evidence = {"job": "CI: server tests", "runner": "example-runner",
                "form": "unexpected EOF".lower(), "line": "unexpected EOF"}

    def diagnosis(attempt=1, items=None):
        return repair.Diagnosis(
            "infrastructure", "its runners failed it",
            runs=[({**run, "run_attempt": attempt}, deepcopy(jobs))],
            downloads={20: deepcopy([evidence] if items is None else items)},
        )

    return SimpleNamespace(repair=repair, gate=gate, folder=tmp_path / "state",
                           calls=calls, wakes=wakes, evidence=evidence, diagnosis=diagnosis,
                           github=github)


def test_one_retry_then_same_runner_waits_and_sent_receipt_suppresses_notice(setup):
    s = setup
    assert s.repair.rerun(s.folder, s.diagnosis())
    assert len(s.calls) == 1
    assert stat.S_IMODE(s.repair.marker(s.folder, 20).stat().st_mode) == 0o600
    for _ in range(3):
        assert "cache" in s.repair.rerun(s.folder, s.diagnosis(2))
    assert len(s.calls) == 1
    pending = s.folder / "runner-downloads.pending/20.json"
    assert json.loads(pending.read_text())["line"].count("example-runner") == 1
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    sent = s.folder / "runner-downloads.sent/20.json"
    sent.parent.mkdir()
    pending.rename(sent)
    assert s.repair.rerun(s.folder, s.diagnosis(2))
    assert not pending.exists()


@pytest.mark.parametrize("change", ["runner", "job", "form", "no evidence"])
def test_changed_failure_is_not_held_as_a_cache_problem(setup, change):
    s = setup
    assert s.repair.rerun(s.folder, s.diagnosis())
    items = [deepcopy(s.evidence)]
    if change == "no evidence":
        items = []
    else:
        items[0][change] = "example-other"
    assert s.repair.rerun(s.folder, s.diagnosis(2, items)) is None
    assert not (s.folder / "runner-downloads.pending/20.json").exists()
    assert len(s.calls) == 1


def test_partial_rerun_with_another_job_recovered_retains_matching_failure(setup):
    s = setup
    other = {**s.evidence, "job": "CI: browser tests", "runner": "example-runner-2"}
    assert s.repair.rerun(s.folder, s.diagnosis(items=[s.evidence, other]))
    assert s.repair.rerun(s.folder, s.diagnosis(2))
    assert (s.folder / "runner-downloads.pending/20.json").exists()


def test_two_calls_while_first_request_is_held_send_only_once(setup):
    s = setup
    entered, release = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        s.calls.append(args)
        entered.set()
        assert release.wait(5)
        return subprocess.CompletedProcess(args, 0, "", "")

    s.gate.command = held
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(s.repair.rerun, s.folder, s.diagnosis())
        try:
            assert entered.wait(5)
            second = pool.submit(s.repair.rerun, s.folder, s.diagnosis())
            assert "requested" in second.result(timeout=2)
        finally:
            release.set()
        assert first.result(timeout=5)
    assert len(s.calls) == 1


def test_timeout_claim_keeps_evidence_and_recovers_when_attempt_two_appears(setup):
    s = setup

    def timed_out(*args, **kwargs):
        s.calls.append(args)
        raise s.github.TimedOut("example timeout", "", 1)

    s.gate.command = timed_out
    with pytest.raises(s.github.TimedOut):
        s.repair.rerun(s.folder, s.diagnosis())
    saved = json.loads(s.repair.marker(s.folder, 20).read_text())
    assert saved["state"] == "claim" and saved["downloads"]
    assert s.repair.retry_requested(s.folder, {"id": 20, "run_attempt": 1})
    assert s.repair.rerun(s.folder, s.diagnosis(2))
    assert len(s.calls) == 1


def test_budget_refusal_consumes_no_retry(setup):
    s = setup

    def refused(*args, **kwargs):
        raise s.github.BudgetWaiting("example budget pause")

    s.gate.command = refused
    with pytest.raises(s.github.BudgetWaiting):
        s.repair.rerun(s.folder, s.diagnosis())
    assert not s.repair.marker(s.folder, 20).exists()


@pytest.mark.parametrize("saved", ["", "{", "[]", '{"downloads":null}',
                                  '{"state":"requested","downloads":[{}]}'])
def test_missing_or_partial_claim_never_creates_a_false_runner_notice(setup, saved):
    s = setup
    s.folder.mkdir()
    s.repair.marker(s.folder, 20).write_text(saved)
    assert s.repair.rerun(s.folder, s.diagnosis(2)) is None
    assert not (s.folder / "runner-downloads.pending/20.json").exists()
    assert s.calls == []


def test_hosted_or_unknown_runner_does_not_gain_retry_eligibility(setup):
    s = setup
    diagnosis = s.diagnosis()
    diagnosis.runs[0][1][0]["labels"] = []
    assert s.repair.rerun(s.folder, diagnosis) is None
    assert s.calls == []


@pytest.mark.parametrize("failure", ["expired", "timeout"])
def test_missing_or_timed_out_log_does_not_invent_download_evidence(setup, failure):
    s = setup
    s.gate.api = lambda *args: []
    s.gate.read_github = lambda *args: subprocess.CompletedProcess(args, 1, "", failure)
    if failure == "timeout":
        with pytest.raises(s.github.Waiting):
            s.repair.diagnose_runs("a" * 40, s.diagnosis().runs)
    else:
        result = s.repair.diagnose_runs("a" * 40, s.diagnosis().runs)
        assert result.kind == "judgment"
        assert result.downloads == {}
