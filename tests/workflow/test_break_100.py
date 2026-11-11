"""Independent adversarial cases for setup-action HTTP failures; GitHub is a stand-in."""

import importlib
import fcntl
import json
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest
from workflow.test_merge_driver import (
    _RUNS, _fail, _folder, _github_answers_repairs, _handbacks, _note, _started, handed,
)
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import SCRIPTS

github = github_fixture
sys.path.insert(0, str(SCRIPTS))
repair = importlib.import_module("check_repair")
health = importlib.import_module("merge_health")

HEAD = "a" * 40
PHRASE = "Failed to run: Error: Unexpected HTTP response: "


@pytest.fixture
def installation(monkeypatch):
    """Exercise real classification, eligibility and durable claims without network calls."""
    gate = ModuleType("merge_gate")
    gate.REPOSITORY = "example-org/example-app"
    gate.API = "repos/" + gate.REPOSITORY
    gate.CI_PLAN = "CI: what to run"
    gate.E2E_PLAN = "E2E: what to run"
    gate.IMAGES_PLAN = "Images: what to run"
    gate.CONFIG = SimpleNamespace(runner=SimpleNamespace(labels=["example-app-runner"]))
    gate._failed_jobs = health._failed_jobs
    gate._rerunnable = health._rerunnable
    gate.Waiting = type("Waiting", (Exception,), {})
    gate.Refused = type("Refused", (Exception,), {})
    gate.BudgetWaiting = type("BudgetWaiting", (gate.Waiting,), {})
    gate.transient = lambda message: "503" in message or "timeout" in message
    gate.calls = []
    gate.notices = []
    gate.log = "##[error]" + PHRASE + "504\n"
    gate.annotations = []
    gate.read_github = lambda *args: subprocess.CompletedProcess(args, 0, gate.log, "")
    gate.api = lambda *args: gate.annotations
    gate.wake_log = gate.notices.append

    def command(*args, **kwargs):
        gate.calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    gate.command = command
    monkeypatch.setitem(sys.modules, "merge_gate", gate)
    run = {"id": 20, "head_sha": HEAD, "status": "completed",
           "conclusion": "failure", "run_attempt": 1}
    jobs = [{"id": 23, "name": "CI: server tests", "status": "completed",
             "conclusion": "failure", "labels": ["example-app-runner"]}]
    return gate, run, jobs


@pytest.mark.parametrize("status", range(500, 600))
def test_every_setup_server_status_is_retryable(installation, tmp_path, status):
    gate, run, jobs = installation
    gate.log = ("CI: server tests\tSet up Go\t2026-11-01T12:00:00Z "
                f"\x1b[31m##[error]{PHRASE}{status}, Error: Unexpected HTTP response: {status}\x1b[0m\n")
    diagnosis = repair.diagnose_runs(HEAD, [(run, jobs)])
    assert diagnosis.kind == "infrastructure"
    assert repair.rerun(tmp_path, diagnosis)
    assert gate.calls == [("gh", "run", "rerun", "20", "-R", gate.REPOSITORY, "--failed")]
    assert stat.S_IMODE(repair.marker(tmp_path, 20).stat().st_mode) == 0o600


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 499, 5000])
def test_non_server_status_does_not_buy_a_retry(installation, status):
    gate, run, jobs = installation
    gate.log = "##[error]" + PHRASE + str(status)
    diagnosis = repair.diagnose_runs(HEAD, [(run, jobs)])
    assert diagnosis.kind == "own"
    assert gate.calls == []


def test_a_named_failing_test_wins_over_setup_error_words(installation):
    gate, run, jobs = installation
    gate.log += "FAILED tests/test_example.py::test_download - AssertionError\n"
    diagnosis = repair.diagnose_runs(HEAD, [(run, jobs)])
    assert diagnosis.kind == "own"
    assert diagnosis.repair.test == "tests/test_example.py::test_download"


def test_application_http_error_is_not_a_setup_error(installation):
    gate, run, jobs = installation
    gate.log = "GET /example returned 504 Gateway Timeout\nProcess completed with exit code 1.\n"
    assert repair.diagnose_runs(HEAD, [(run, jobs)]).kind == "own"


@pytest.mark.parametrize("failure", ["missing", "transient", "annotation"])
def test_unreadable_log_or_annotation_fallback(installation, failure):
    gate, run, jobs = installation
    error = "HTTP 503" if failure == "transient" else "log not found"
    gate.read_github = lambda *args: subprocess.CompletedProcess(args, 1, "", error)
    if failure == "annotation":
        gate.annotations = [{"message": PHRASE + "504"}]
        assert repair.diagnose_runs(HEAD, [(run, jobs)]).kind == "infrastructure"
    elif failure == "transient":
        with pytest.raises(gate.Waiting):
            repair.diagnose_runs(HEAD, [(run, jobs)])
    else:
        assert repair.diagnose_runs(HEAD, [(run, jobs)]).kind == "judgment"
    assert not gate.calls


@pytest.mark.parametrize("evidence", ["missing labels", "missing attempt", "second attempt", "planner"])
def test_missing_or_ineligible_retry_evidence_stays_safe(installation, tmp_path, evidence):
    gate, run, jobs = installation
    if evidence == "missing labels":
        jobs[0].pop("labels")
    elif evidence == "missing attempt":
        run.pop("run_attempt")
    elif evidence == "second attempt":
        run["run_attempt"] = 2
    else:
        jobs[0]["name"] = gate.CI_PLAN
    diagnosis = repair.diagnose_runs(HEAD, [(run, jobs)])
    assert repair.rerun(tmp_path, diagnosis) is None
    assert not gate.calls


def test_partial_rerun_waits_for_all_jobs(installation):
    gate, run, jobs = installation
    run["run_attempt"] = 2
    jobs.append({**jobs[0], "id": 24, "status": "in_progress", "conclusion": None})
    with pytest.raises(gate.Waiting):
        repair.diagnose_runs(HEAD, [(run, jobs)])
    assert not gate.calls


def test_two_callers_and_restart_spend_only_one_retry(installation, tmp_path):
    gate, run, jobs = installation
    entered = threading.Event()
    release = threading.Event()

    def slow_command(*args, **kwargs):
        gate.calls.append(args)
        entered.set()
        assert release.wait(5)
        return subprocess.CompletedProcess(args, 0, "", "")

    gate.command = slow_command
    diagnosis = repair.diagnose_runs(HEAD, [(run, jobs)])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(repair.rerun, tmp_path, diagnosis)
        try:
            assert entered.wait(5)
            assert repair.retry_requested(tmp_path, run)
            second = pool.submit(repair.rerun, tmp_path, diagnosis)
            assert "requested" in second.result(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=5)
    # A later process reconstructs diagnosis from the same GitHub snapshot.
    assert repair.rerun(tmp_path, repair.diagnose_runs(HEAD, [(run, jobs)]))
    assert len(gate.calls) == 1
    run["run_attempt"] = 2
    repeated = repair.diagnose_runs(HEAD, [(run, jobs)])
    assert repair.rerun(tmp_path, repeated) is None
    assert "its runners failed it again in run 20" == repeated.reason
    assert len(gate.calls) == 1


def test_timeout_after_sending_keeps_the_claim(installation, tmp_path):
    gate, run, jobs = installation

    def lost_response(*args, **kwargs):
        gate.calls.append(args)
        raise gate.Waiting("request timeout")

    gate.command = lost_response
    diagnosis = repair.diagnose_runs(HEAD, [(run, jobs)])
    with pytest.raises(gate.Waiting):
        repair.rerun(tmp_path, diagnosis)
    assert repair.retry_requested(tmp_path, run)
    assert "requested" in repair.rerun(tmp_path, diagnosis)
    assert len(gate.calls) == 1


def test_setup_log_is_not_copied_into_retry_state_or_notice(installation, tmp_path):
    gate, run, jobs = installation
    gate.log += "opaque-example-credential $(touch example-marker) `false`\n"
    diagnosis = repair.diagnose_runs(HEAD, [(run, jobs)])
    assert repair.rerun(tmp_path, diagnosis)
    saved = repair.marker(tmp_path, 20).read_text()
    assert "opaque-example-credential" not in saved + "\n".join(gate.notices)
    assert gate.calls == [("gh", "run", "rerun", "20", "-R", gate.REPOSITORY, "--failed")]


def _tick(handed):
    done = handed.workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def _setup_failure(handed):
    _github_answers_repairs(handed.workflow)
    _fail(handed, log="CI: server tests\tSet up Go\t2026-11-01T12:00:00Z "
          f"##[error]{PHRASE}504, Error: Unexpected HTTP response: 504\n")
    _tick(handed)
    assert len(handed.workflow.calls("gh run rerun")) == 1
    assert _note(handed.workflow).exists()


def _finish_retry(handed, passed, rollup=None):
    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    run.update(run_attempt=2, status="completed", conclusion="success" if passed else "failure")
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    for job in jobs:
        job.update(status="completed", conclusion="success")
    if not passed:
        jobs[-1]["conclusion"] = "failure"
        jobs[1]["conclusion"] = "failure"
    handed.pulls["7"]["statusCheckRollup"][0].update(
        status="COMPLETED", conclusion=rollup or ("SUCCESS" if passed else "FAILURE"))
    handed.save()


@pytest.mark.parametrize("passed", [True, False], ids=["same-head-green", "repeat-504"])
def test_real_tick_preserves_head_or_hands_back_to_fresh_review(handed, passed):
    original = handed.head
    _setup_failure(handed)
    _tick(handed)
    assert _started(handed.workflow) == []
    _finish_retry(handed, passed)
    _tick(handed)
    assert len(handed.workflow.calls("gh run rerun")) == 1
    assert handed.head == original
    assert not handed.workflow.calls("gh pr ready")
    if passed:
        assert (handed.workflow.state / "merged").read_text() == "7"
        assert _started(handed.workflow) == []
    else:
        assert not (handed.workflow.state / "merged").exists()
        assert _started(handed.workflow) == ["review-7"]
        assert "its runners failed it again in run 20" in handed.workflow.wake_log.read_text()


def test_a_successful_retry_with_late_rollup_keeps_review_authorization(handed):
    _setup_failure(handed)
    _finish_retry(handed, True, rollup="FAILURE")
    _tick(handed)
    if not _note(handed.workflow).exists():
        reason = "; ".join(_handbacks(handed.workflow))
        pytest.fail("successful retry lost its passed-review handoff: " + reason)
    assert _started(handed.workflow) == []
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    handed.save()
    _tick(handed)
    assert (handed.workflow.state / "merged").read_text() == "7"


def test_real_tick_waits_for_partial_rerun_and_old_attempt_metadata(handed):
    _setup_failure(handed)
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    for job in jobs:
        job.update(conclusion="success")
    handed.save()
    # Job results arrive before the workflow-run attempt increments.
    _tick(handed)
    assert _note(handed.workflow).exists()
    assert _started(handed.workflow) == []
    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    run.update(run_attempt=2, status="in_progress", conclusion=None)
    jobs[-1].update(status="in_progress", conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    handed.save()
    _tick(handed)
    assert _note(handed.workflow).exists()
    assert _started(handed.workflow) == []
    assert len(handed.workflow.calls("gh run rerun")) == 1
    _finish_retry(handed, True)
    _tick(handed)
    assert (handed.workflow.state / "merged").read_text() == "7"


def test_held_merge_lock_does_not_consume_a_second_retry(handed):
    _setup_failure(handed)
    with (_folder(handed.workflow) / "merge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=1) as pool:
            tick = pool.submit(_tick, handed)
            try:
                with pytest.raises(TimeoutError):
                    tick.result(timeout=5)
                assert _note(handed.workflow).exists()
                assert len(handed.workflow.calls("gh run rerun")) == 1
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            tick.result(timeout=30)
    assert _note(handed.workflow).exists()
    assert _started(handed.workflow) == []
    assert json.loads((_folder(handed.workflow) / "check-rerun-20.json").read_text())["state"] == "requested"
