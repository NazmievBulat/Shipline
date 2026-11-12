"""Queue recovery regressions from PR 105; all GitHub calls use stand-ins."""

import fcntl
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from workflow.merge_gate_fixtures import github as github_fixture

github = github_fixture
API = "repos/example-org/example-app"
STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")


def stamp(age):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))


def queued(github, number=120, workflow="ci", age=16200):
    run = {
        "id": number, "head_sha": github.head, "head_branch": "ticket/seven",
        "event": "pull_request", "status": "queued", "conclusion": None,
        "path": f".github/workflows/{workflow}.yml", "run_attempt": 1,
        "created_at": stamp(age),
    }
    for status in STATUSES:
        github.api.setdefault(f"{API}/actions/runs?status={status}&per_page=100", {"workflow_runs": []})
    github.api[f"{API}/actions/runs?status=queued&per_page=100"]["workflow_runs"].append(run)
    github.api[f"{API}/actions/runs/{number}"] = run
    github.api[f"{API}/actions/runs/{number}/cancel"] = {}
    github.api[f"{API}/actions/runs/{number}/jobs?per_page=100"] = {"jobs": []}
    return run


def folder(github):
    return github.workflow.repo / ".local/workflow"


def recover(github, **env):
    github.save()
    snapshot = github.workflow.state / "breaker-pulls.json"
    snapshot.write_text(json.dumps(github.pulls))
    return github.workflow.run_python("tick_queued_runs.py", str(folder(github)), str(snapshot), env=env)


def good(done):
    assert done.returncode == 0, done.stdout + done.stderr


def state(github):
    return json.loads((folder(github) / "never-started-runs.json").read_text())


def save_state(github, data):
    (folder(github) / "never-started-runs.json").write_text(json.dumps(data))


def cancellations(github, run):
    return github.workflow.calls(f"gh api -X POST {API}/actions/runs/{run['id']}/cancel")


def worktree(github):
    github.workflow.git("checkout", "main")
    target = github.workflow.worktree(7)
    target.parent.mkdir(parents=True, exist_ok=True)
    github.workflow.git("worktree", "add", str(target), "ticket/seven")
    return target


def origin_head(github):
    return github.workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0]


def retry(github, run, *, materialize_jobs=True):
    good(recover(github))
    run.update(status="completed", conclusion="cancelled")
    good(recover(github))
    run.update(status="queued", conclusion=None, run_attempt=2)
    if materialize_jobs:
        github.api[f"{API}/actions/runs/{run['id']}/jobs?per_page=100"] = {"jobs": [{
            "id": 121, "run_attempt": 2, "status": "queued", "runner_id": 0,
            "started_at": None, "created_at": stamp(1800),
        }]}


@pytest.mark.parametrize("suite", ["ci", "e2e"])
def test_idle_outage_run_is_cancelled_via_runs_api_and_retried_once(github, suite):
    run = queued(github, workflow=suite)
    for _ in range(3):
        good(recover(github))
    assert len(cancellations(github, run)) == 1
    run.update(status="completed", conclusion="cancelled")
    for _ in range(3):
        good(recover(github))
    assert len(github.workflow.calls("gh run rerun 120 ")) == 1
    assert not github.workflow.calls("gh run cancel")


@pytest.mark.parametrize("jobs", [True, False])
def test_retry_ages_independently_then_pushes_one_empty_commit(github, jobs):
    target = worktree(github)
    run = queued(github)
    retry(github, run, materialize_jobs=jobs)
    if not jobs:
        good(recover(github))
        assert origin_head(github) == github.head
        data = state(github)
        data["runs"]["120"]["observed"]["2"] -= 1800
        save_state(github, data)
    good(recover(github))
    after = origin_head(github)
    assert after != github.head
    assert github.workflow.git("rev-parse", f"{after}^") == github.head
    assert github.workflow.git("rev-parse", f"{after}^{{tree}}") == github.workflow.git("rev-parse", f"{github.head}^{{tree}}")
    assert github.workflow.git("log", "-1", "--format=%s", cwd=target) == "ci: run CI again, GitHub never started CI of run 120"
    good(recover(github))
    assert origin_head(github) == after


def test_two_retries_without_jobs_do_not_deadlock_each_other(github):
    worktree(github)
    first = queued(github)
    second = queued(github, number=130, workflow="e2e", age=14400)
    good(recover(github))
    assert len(cancellations(github, first)) == 1
    assert not cancellations(github, second)
    first.update(status="completed", conclusion="cancelled")
    good(recover(github))
    assert len(cancellations(github, second)) == 1
    first.update(status="queued", conclusion=None, run_attempt=2)
    second.update(status="completed", conclusion="cancelled")
    good(recover(github))
    second.update(status="queued", conclusion=None, run_attempt=2)
    good(recover(github))
    data = state(github)
    data["runs"]["120"]["observed"]["2"] -= 1800
    data["runs"]["130"]["observed"]["2"] -= 1500
    save_state(github, data)
    # Both retries are overdue, with CI observed first. Neither has any jobs.
    for _ in range(3):
        good(recover(github))
    after = origin_head(github)
    before = github.head
    assert after != before, "two overdue retries with no jobs block each other's empty-commit recovery"


@pytest.mark.parametrize("status", STATUSES)
def test_busy_or_older_repository_work_prevents_cancellation(github, status):
    run = queued(github)
    other = {**run, "id": 110, "head_branch": "ticket/other", "created_at": stamp(18000), "status": status}
    github.api[f"{API}/actions/runs?status={status}&per_page=100"]["workflow_runs"].append(other)
    good(recover(github))
    assert not cancellations(github, run)


@pytest.mark.parametrize("job", [
    {"status": "in_progress", "runner_id": 9},
    {"status": "completed", "conclusion": "success"},
    {"status": "queued", "started_at": "2026-01-01T00:00:00Z"},
    {"status": "queued", "runner_id": 9},
    {"status": "queued", "run_attempt": 2},
])
def test_any_started_assigned_or_mismatched_matrix_part_prevents_cancellation(github, job):
    run = queued(github)
    github.api[f"{API}/actions/runs/120/jobs?per_page=100"] = {"jobs": [
        {"id": 121, "status": "queued", "created_at": stamp(1800)}, {"id": 122, **job},
    ]}
    good(recover(github))
    assert not cancellations(github, run)


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_main_only_gets_one_notice_and_is_never_cancelled(github, event):
    run = queued(github)
    run.update(head_branch="main", head_sha=github.base, event=event)
    for _ in range(3):
        good(recover(github))
    assert not cancellations(github, run)
    assert not github.workflow.calls("gh run rerun")
    assert len(list((folder(github) / "never-started.pending").glob("*.json"))) == 1


def test_lock_contention_returns_without_any_github_call(github):
    queued(github)
    folder(github).mkdir(parents=True, exist_ok=True)
    before = len(github.workflow.calls("gh"))
    with (folder(github) / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        good(recover(github))
    assert len(github.workflow.calls("gh")) == before
    good(recover(github))
    assert len(github.workflow.calls("gh api -X POST")) == 1


def test_simultaneous_ticks_claim_only_one_cancellation(github):
    run = queued(github)
    github.save()
    snapshot = github.workflow.state / "breaker-pulls.json"
    snapshot.write_text(json.dumps(github.pulls))
    def call():
        return github.workflow.run_python("tick_queued_runs.py", str(folder(github)), str(snapshot))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(), range(2)))
    for done in results:
        good(done)
    assert len(cancellations(github, run)) == 1


@pytest.mark.parametrize("failure", ["HTTP 502: Bad Gateway", "context deadline exceeded"])
def test_cancel_transport_failure_can_recover_on_the_next_tick(github, failure):
    run = queued(github)
    github.api[f"{API}/actions/runs/120/cancel"] = failure
    assert recover(github).returncode == 1
    github.api[f"{API}/actions/runs/120/cancel"] = {}
    good(recover(github))
    assert len(cancellations(github, run)) == 2


def test_unreadable_capacity_does_not_authorize_cancellation(github):
    run = queued(github)
    github.api[f"{API}/actions/runs?status=in_progress&per_page=100"] = "HTTP 403: Forbidden"
    assert recover(github).returncode == 1
    assert not cancellations(github, run)


@pytest.mark.parametrize("missing", ["worktree", "dirty worktree"])
def test_refresh_refuses_missing_or_dirty_worktree_without_pushing(github, missing):
    if missing == "dirty worktree":
        (worktree(github) / "shared.txt").write_text("uncommitted\n")
    run = queued(github)
    retry(github, run)
    good(recover(github))
    assert origin_head(github) == github.head
    notes = [json.loads(path.read_text()) for path in (folder(github) / "never-started.pending").glob("*.json")]
    assert any("worktree" in note["line"] for note in notes)


def test_shadow_tick_does_not_read_or_mutate_recovery(github):
    queued(github)
    before = len(github.workflow.calls("gh"))
    good(recover(github, SHIPLINE_SHADOW="1"))
    assert len(github.workflow.calls("gh")) == before
    assert not (folder(github) / "never-started-runs.json").exists()


def test_recovery_state_and_notices_are_owner_only(github):
    queued(github)
    good(recover(github))
    for path in [folder(github) / "never-started-runs.json", *(folder(github) / "never-started.pending").glob("*.json")]:
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("stage", ["cancel", "refresh"])
def test_a_run_that_starts_during_capacity_reads_is_left_alone(github, stage):
    worktree(github)
    run = queued(github)
    if stage == "refresh":
        retry(github, run)
    source = github.workflow.bin / "gh.py"
    text = source.read_text()
    anchor = '    data = json.loads((state / "github.json").read_text())\n'
    hook = '''    if args[-1:] == ["repos/example-org/example-app/actions/runs?status=requested&per_page=100"]:
        current = data["api"]["repos/example-org/example-app/actions/runs/120"]
        current.update(status="in_progress", conclusion=None)
        data["api"]["repos/example-org/example-app/actions/runs/120/jobs?per_page=100"] = {"jobs": [{
            "id": 121, "run_attempt": current["run_attempt"], "status": "in_progress",
            "runner_id": 9, "started_at": current["created_at"], "created_at": current["created_at"],
        }]}
        (state / "github.json").write_text(json.dumps(data))
'''
    assert anchor in text
    source.write_text(text.replace(anchor, anchor + hook))
    before = len(cancellations(github, run))
    good(recover(github))
    assert len(cancellations(github, run)) == before
    after = origin_head(github)
    original = github.head
    assert after == original, "recovery pushed a replacement commit after the retry had started"


def test_a_transient_refresh_push_failure_recovers_without_a_review_handoff(github):
    target = worktree(github)
    run = queued(github)
    retry(github, run)
    github.workflow.fake("git", '''#!/bin/bash
if [[ $1 == push && -e "@STATE@/breaker-push-unavailable" ]]; then
  echo 'fatal: unable to access remote: HTTP 503' >&2
  exit 1
fi
exec /usr/bin/git "$@"
''')
    unavailable = github.workflow.state / "breaker-push-unavailable"
    unavailable.touch()
    good(recover(github))
    assert origin_head(github) == github.head
    assert github.workflow.git("rev-parse", "HEAD", cwd=target) != github.head
    unavailable.unlink()
    for _ in range(3):
        good(recover(github))
    after = origin_head(github)
    before = github.head
    assert after != before, "the recovered remote never receives the locally created recovery commit"


def test_sent_notices_are_not_created_again_after_restart(github):
    run = queued(github)
    good(recover(github))
    pending = folder(github) / "never-started.pending"
    sent = folder(github) / "never-started.sent"
    sent.mkdir()
    [notice] = list(pending.glob("*.json"))
    notice.rename(sent / notice.name)
    for _ in range(3):
        good(recover(github))
    assert not list(pending.glob("*.json"))
    assert len(list(sent.glob("*.json"))) == 1
    assert len(cancellations(github, run)) == 1


def test_custom_workflow_state_path_and_limit_are_used_literally(github):
    config = github.workflow.repo / "shipline.json"
    data = json.loads(config.read_text())
    relative = ".local/example state/$(touch injected)"
    data.update(state_folder=relative, workflows={"ci": "build.yml", "e2e": "browser.yml"})
    data["merge_train"] = {"queued_limit_minutes": 10}
    config.write_text(json.dumps(data))
    run = queued(github, age=660)
    run["path"] = ".github/workflows/build.yml"
    github.save()
    snapshot = github.workflow.state / "breaker-pulls.json"
    snapshot.write_text(json.dumps(github.pulls))
    target = github.workflow.repo / relative
    good(github.workflow.run_python("tick_queued_runs.py", str(target), str(snapshot)))
    assert len(cancellations(github, run)) == 1
    assert (target / "never-started-runs.json").exists()
    assert not (github.workflow.repo / "injected").exists()


def test_an_explicit_missing_configuration_does_not_mutate_github(github):
    run = queued(github)
    done = recover(github, SHIPLINE_CONFIG=str(github.workflow.repo / "missing.json"))
    assert done.returncode != 0
    assert not cancellations(github, run)
    assert not (folder(github) / "never-started-runs.json").exists()


@pytest.mark.parametrize("main", [False, True])
def test_recovery_notices_count_once_in_lost_check_metrics_after_delivery(github, main):
    run = queued(github)
    if main:
        run.update(head_branch="main", head_sha=github.base, event="push")
    good(recover(github))
    pending = folder(github) / "never-started.pending"
    sent = folder(github) / "never-started.sent"
    sent.mkdir()
    [notice] = list(pending.glob("*.json"))
    notice.rename(sent / notice.name)
    good(recover(github))
    # Exercise the actual durable-note collector and quality counter, with the
    # unrelated GitHub history empty. This is not a metrics-suite rerun.
    code = '''import json, time
import workflow_metrics as metrics
metrics.merged_pulls = lambda *args: []
metrics.workflow_runs = lambda *args: []
now = time.time()
sources = metrics.read_sources(now - 86400, now + 1)
quality, _ = metrics.quality_numbers(metrics.Window(now - 86400, now + 1, 1), sources)
print(json.dumps(quality["lost_checks"]))
'''
    done = subprocess.run(
        ["python3", "-B", "-c", code], cwd=github.workflow.repo,
        env={**github.workflow.env, "PYTHONPATH": str(github.workflow.script.parent)},
        capture_output=True, text=True, timeout=30, check=False,
    )
    good(done)
    assert json.loads(done.stdout) == {"CI (never started)": 1}
