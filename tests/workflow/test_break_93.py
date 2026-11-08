"""Independent break cases for PR 93. Run this file alone; all GitHub calls are stand-ins.

Real Git repositories exercise movement and rollback. CI responses exercise failed,
missing, late and partially re-run checks without contacting GitHub.
"""

import fcntl
import importlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "workflow"
sys.path.insert(0, str(SCRIPTS))
update = importlib.import_module("self_update")
budget = importlib.import_module("github_budget")
SLUG = "example-org/example-app"
AUTHOR = "Bulat Nazmiev"
EMAIL = "82903523+NazmievBulat@users.noreply.github.com"
START = 1_800_000_000


class Installation:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.now = START
        self.calls = []
        self.runs = {}
        self.jobs = []
        self.annotations = {}
        self.log = ""
        self.rerun_error = None
        self.read_error = None
        self.before_read = None
        self.guard = threading.Lock()
        self.dev = root / "dev"
        self.home = root / "checkout"
        self.notes = self.home / ".local" / "shipline"
        self.dev.mkdir()
        self.home.mkdir()
        (root / "empty-config").touch()
        monkeypatch.setenv("HOME", str(root))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(root / "empty-config"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        for kind in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv(f"GIT_{kind}_NAME", AUTHOR)
            monkeypatch.setenv(f"GIT_{kind}_EMAIL", EMAIL)
        self.git(root, "init", "--bare", "--initial-branch=main", "upstream.git")
        for folder in (self.dev, self.home):
            self.git(folder, "init", "--initial-branch=main")
            self.git(folder, "config", "user.name", AUTHOR)
            self.git(folder, "config", "user.email", EMAIL)
            self.git(folder, "remote", "add", "origin", "../upstream.git")
        (self.dev / ".gitignore").write_text(".local/\n")
        (self.dev / "VERSION").write_text("initial\n")
        self.git(self.dev, "add", ".")
        self.git(self.dev, "commit", "-m", "test: initial version")
        self.git(self.dev, "push", "origin", "main")
        self.git(self.home, "fetch", "origin", "main")
        self.git(self.home, "reset", "--hard", "origin/main")
        self.initial = self.head
        self.notes.mkdir(parents=True)
        (self.notes / "versions").write_text(self.initial + "\n")
        original_git = update.git

        def local_git(*args, **kwargs):
            kwargs.setdefault("folder", self.home)
            return original_git(*args, **kwargs)

        monkeypatch.setattr(update, "HOME", self.home)
        monkeypatch.setattr(update, "NOTES", self.notes)
        monkeypatch.setattr(update, "git", local_git)
        monkeypatch.setattr(update, "now", lambda: self.now)
        monkeypatch.setattr(update, "github_repository", lambda: SLUG)
        # Process identity and installation discovery are outside the change under test.
        monkeypatch.setattr(update, "started_at", lambda pid: "example-tick")
        monkeypatch.setattr(update, "tick_file_changed", lambda pid: False)
        monkeypatch.setattr(update, "register_locks", lambda: None)
        monkeypatch.setattr(budget, "call", self.call)

    def git(self, folder, *args):
        done = subprocess.run(["git", *args], cwd=folder, capture_output=True,
                              text=True, timeout=10, check=False)
        assert done.returncode == 0, "test repository setup failed"
        return done.stdout.strip()

    @property
    def head(self):
        return self.git(self.home, "rev-parse", "HEAD")

    def land(self, conclusion="success"):
        self.git(self.dev, "commit", "--allow-empty", "-m", "test: next version")
        self.git(self.dev, "push", "origin", "main")
        sha = self.git(self.dev, "rev-parse", "HEAD")
        if conclusion is not None:
            self.runs[sha] = [{
                "id": 100 + len(self.runs), "head_sha": sha, "head_branch": "main",
                "path": ".github/workflows/ci.yml", "event": "push", "run_attempt": 1,
                "status": "completed" if conclusion != "in_progress" else "in_progress",
                "conclusion": None if conclusion == "in_progress" else conclusion,
                "created_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            }]
        return sha

    def tick(self, pid=101):
        result = update.start(pid, False)
        update.end(pid, 0, False, False)
        return result

    def later(self, seconds=601):
        self.now += seconds

    def notices(self):
        return [json.loads(path.read_text())["line"]
                for path in sorted((self.notes / "wake.pending").glob("*.json"))]

    def call(self, program, *args, timeout=None, **kwargs):
        assert timeout == 30, "CI requests must have a timeout"
        with self.guard:
            self.calls.append(args)
        if args[:2] == ("run", "rerun"):
            if self.rerun_error is not None:
                raise self.rerun_error
            return subprocess.CompletedProcess(args, 0, "", "")
        if self.before_read:
            self.before_read(args)
        if self.read_error is not None:
            raise self.read_error
        if args[0] == "api" and "head_sha=" in args[1]:
            sha = args[1].split("head_sha=", 1)[1].split("&", 1)[0]
            value = {"workflow_runs": self.runs.get(sha, [])}
        elif args[0] == "api" and "/jobs?" in args[1]:
            assert args[-2:] == ("--paginate", "--slurp")
            value = self.jobs if self.jobs and isinstance(self.jobs[0], list) else [self.jobs]
            value = [{"jobs": page} for page in value]
        elif args[0] == "api" and "/annotations?" in args[1]:
            assert args[-2:] == ("--paginate", "--slurp")
            job_id = int(args[1].split("check-runs/")[1].split("/")[0])
            value = self.annotations.get(job_id, [[]])
        elif args[:2] == ("run", "view"):
            return subprocess.CompletedProcess(args, 0, self.log, "")
        else:
            raise AssertionError("unexpected GitHub operation")
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    @property
    def retries(self):
        return sum(args[:2] == ("run", "rerun") for args in self.calls)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    return Installation(tmp_path, monkeypatch)


def job(conclusion="cancelled", *, step="cancelled", job_id=201, name="CI: server tests (1 of 4)"):
    return {"id": job_id, "name": name, "status": "completed", "conclusion": conclusion,
            "steps": [{"name": "Tests", "conclusion": step}]}


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "in_progress", None])
def test_daily_fallback_then_late_head_success(installation, conclusion):
    app = installation
    app.land()
    eligible = app.land()
    head = app.land(conclusion)
    assert app.tick() == update.MOVED
    assert app.head == eligible
    assert json.loads((app.notes / "trial.json").read_text())["good"] == app.initial
    if conclusion is None:
        app.runs[head] = [{**app.runs[eligible][0], "id": 501, "head_sha": head}]
    else:
        app.runs[head][0].update(status="completed", conclusion="success", run_attempt=2)
    app.later()
    assert app.tick() == update.MOVED
    assert app.head == head


def test_missing_ci_waits_one_hour_from_observation_and_reports_once(installation):
    app = installation
    head = app.land(None)
    assert app.tick() == 0
    app.later(3600)
    assert app.tick() == 0
    assert app.notices() == []
    app.later()
    assert app.tick() == 0
    expected = f"shipline update waits: {head} CI missing, installed {app.initial}"
    assert app.notices() == [expected]
    app.later(7200)
    assert app.tick() == 0
    assert app.notices() == [expected]


def test_old_failed_head_notice_names_actual_fallback(installation):
    app = installation
    eligible = app.land()
    head = app.land("failure")
    app.later(3601)
    assert app.tick() == update.MOVED
    assert app.notices() == [f"shipline update waits: {head} CI failure, installed {eligible}"]


def test_safe_ancestor_is_still_taken_after_rollback_and_failed_ci_rerun(installation):
    app = installation
    eligible = app.land()
    bad = app.land()
    assert app.tick() == update.MOVED
    assert app.head == bad
    for _ in range(3):
        update.record_tick(bad, "example crash")
    assert app.tick() == update.MOVED
    assert app.head == app.initial
    assert bad in (app.notes / "bad").read_text()
    # CI is re-run while diagnosing the version; the new attempt loses its runner.
    # The fetched head now has no latest passed CI, but its older fix still does.
    app.runs[bad][0].update(run_attempt=2, conclusion="cancelled")
    app.later()
    assert app.tick() == update.MOVED, "eligible passed ancestor is skipped when main head is known bad"
    assert app.head == eligible


def test_fallback_stays_on_first_parent_and_excludes_bad_ancestors(installation):
    app = installation
    eligible = app.land()
    bad = app.land()
    (app.notes / "bad").write_text(bad + "\n")
    app.git(app.dev, "checkout", "-b", "example-topic")
    app.git(app.dev, "commit", "--allow-empty", "-m", "test: side branch")
    side = app.git(app.dev, "rev-parse", "HEAD")
    app.runs[side] = [{**app.runs[eligible][0], "head_sha": side}]
    app.git(app.dev, "checkout", "main")
    app.land("failure")
    app.git(app.dev, "merge", "--no-ff", "example-topic", "-m", "test: merge side")
    app.git(app.dev, "push", "origin", "main")
    assert app.tick() == update.MOVED
    assert app.head == eligible
    assert not any(side in " ".join(args) for args in app.calls)


@pytest.mark.parametrize("conclusion", ["cancelled", "failure", "timed_out"])
def test_runner_failure_retries_once_with_private_claim(installation, conclusion):
    app = installation
    sha = app.land(conclusion)
    app.jobs = [job(conclusion), job("failure", step="failure", job_id=202, name="CI")]
    app.log = "The runner has lost communication."
    for _ in range(3):
        assert app.tick() == 0
        app.later()
    assert app.retries == 1
    claim = app.notes / f"ci-rerun-{app.runs[sha][0]['id']}"
    assert claim.stat().st_mode & 0o777 == 0o600


def test_failed_test_on_second_jobs_page_prevents_retry(installation):
    app = installation
    app.land("failure")
    app.jobs = [[job()], [job("failure", step="failure", job_id=202)]]
    assert app.tick() == 0
    assert app.retries == 0


def test_failed_test_on_second_annotations_page_prevents_retry(installation):
    app = installation
    app.land("cancelled")
    app.jobs = [job()]
    app.annotations[201] = [[], [{"message": "FAILED tests/test_example.py::test_example"}]]
    assert app.tick() == 0
    assert app.retries == 0


def test_cancelled_log_with_failed_test_prevents_retry(installation):
    app = installation
    app.land("cancelled")
    app.jobs = [job()]
    app.log = "FAILED tests/test_example.py::test_example - AssertionError"
    assert app.tick() == 0
    assert app.retries == 0


@pytest.mark.parametrize("metadata", ["missing", "invalid-json"])
def test_missing_or_damaged_timing_state_uses_first_observation(installation, metadata):
    app = installation
    app.land(None)
    if metadata == "invalid-json":
        (app.notes / "update-head.json").write_text("{interrupted\n")
    assert app.tick() == 0
    app.later(3601)
    assert app.tick() == 0
    assert len(app.notices()) == 1


def test_latest_in_progress_ci_does_not_reuse_older_green_run(installation):
    app = installation
    eligible = app.land()
    head = app.land()
    older = app.runs[head][0]
    app.runs[head].append({**older, "id": older["id"] + 1,
                          "status": "in_progress", "conclusion": None})
    assert app.tick() == update.MOVED
    assert app.head == eligible


def test_partial_rerun_is_not_requested_again_and_late_success_updates(installation):
    app = installation
    sha = app.land("failure")
    app.runs[sha][0]["run_attempt"] = 2
    app.jobs = [job()]
    assert app.tick() == 0
    assert app.retries == 0
    app.runs[sha][0]["conclusion"] = "success"
    app.later()
    assert app.tick() == update.MOVED
    assert app.head == sha


@pytest.mark.parametrize("error", [budget.Limited(), subprocess.TimeoutExpired("gh", 30), OSError("unavailable")],
                         ids=["budget", "timeout", "unavailable"])
def test_unreadable_github_leaves_checkout_and_tick_intact(installation, error):
    app = installation
    app.land()
    app.read_error = error
    assert app.tick() == 0
    assert app.head == app.initial


@pytest.mark.parametrize("error, count", [(budget.Limited(), 2), (subprocess.TimeoutExpired("gh", 30), 1),
                                         (OSError("unavailable"), 1)],
                         ids=["budget", "ambiguous-timeout", "ambiguous-os-error"])
def test_retry_budget_and_ambiguous_request_outcomes(installation, error, count):
    app = installation
    app.land("cancelled")
    app.jobs = [job()]
    app.rerun_error = error
    for _ in range(2):
        assert app.tick() == 0
        app.later()
    assert app.retries == count


def test_two_retry_calls_claim_once(installation):
    app = installation
    sha = app.land("cancelled")
    app.jobs = [job()]
    barrier = threading.Barrier(2)
    app.before_read = lambda args: barrier.wait(timeout=5) if "/jobs?" in args[1] else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update.retry_ci, SLUG, app.runs[sha][0]) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert app.retries == 1


def test_long_held_update_lock_does_not_wait_or_move(installation):
    app = installation
    app.land()
    with (app.notes / "update.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert app.tick() == 0
        assert app.head == app.initial
    assert app.tick() == update.MOVED


def test_tick_arriving_during_ci_read_prevents_movement(installation):
    app = installation
    app.land()
    entered, release = threading.Event(), threading.Event()

    def pause(args):
        entered.set()
        assert release.wait(5)

    app.before_read = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(update.start, 101, False)
        assert entered.wait(5)
        try:
            assert update.start(102, False) == 0
        finally:
            release.set()
        assert first.result(timeout=10) == 0
    assert app.head == app.initial
    update.end(101, 0, False, False)
    update.end(102, 0, False, False)


def test_local_edit_blocks_fallback(installation):
    app = installation
    app.land()
    app.land("failure")
    (app.home / "VERSION").write_text("example local edit\n")
    assert app.tick() == 0
    assert app.head == app.initial
    assert app.notices() == ["shipline not updated: its checkout has local changes"]


@pytest.mark.parametrize("field, value", [("event", "pull_request"), ("head_branch", "example-topic"),
                                          ("path", ".github/workflows/example.yml"), ("head_sha", "a" * 40)])
def test_unrelated_green_run_cannot_authorize_fallback(installation, field, value):
    app = installation
    sha = app.land()
    app.runs[sha][0][field] = value
    app.land("failure")
    assert app.tick() == 0
    assert app.head == app.initial


def test_untrusted_conclusion_does_not_reach_wake_or_execute(installation):
    app = installation
    sha = app.land("failure")
    app.runs[sha][0]["conclusion"] = "$(touch example-injected); example diagnostic"
    app.later(3601)
    assert app.tick() == 0
    assert app.notices() == [f"shipline update waits: {sha} CI unknown, installed {app.initial}"]
    assert not (app.home / "example-injected").exists()


def test_check_in_reads_installation_commit_with_inherited_git_environment(installation):
    app = installation
    # Execute the exact read block from wake, with the surrounding services omitted.
    wake = (SCRIPTS / "wake").read_text()
    begin = wake.index("installed=$(\n")
    end = wake.index('\nseen="sessions:', begin)
    block = wake[begin:end]
    environment = {**os.environ, "GIT_DIR": str(app.dev / ".git"), "GIT_WORK_TREE": str(app.dev)}
    app.land()
    (app.home / "scripts" / "workflow").mkdir(parents=True)
    done = subprocess.run(["bash", "-c", 'here=$1\n' + block + '\nprintf "%s" "$installed"',
                           "bash", str(app.home / "scripts" / "workflow")],
                          env=environment, capture_output=True, text=True, timeout=10)
    expected = app.git(app.home, "rev-parse", "--short", "HEAD")
    assert done.returncode == 0
    assert done.stdout == expected
    assert 'shipline: $installed.' in wake
