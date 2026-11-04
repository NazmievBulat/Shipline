"""Independent incident replays for PR 85; run this file alone.

All GitHub calls below use local stand-ins and real disposable Git histories.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow


@pytest.fixture
def gate():
    from workflow.merge_gate_fixtures import gate as fixture

    yield from fixture.__wrapped__()


@pytest.fixture
def github(workflow):
    from workflow.merge_gate_fixtures import github as fixture

    yield from fixture.__wrapped__(workflow)


@pytest.fixture
def handed(github):
    from workflow.test_merge_driver import handed as fixture

    return fixture.__wrapped__(github)


ERRORS = [
    "non-200 OK status code: 502 Bad Gateway body: \"<html>502 Bad Gateway</html>\"",
    "* Request at now\n> POST /graphql HTTP/1.1\n< HTTP/2.0 503 Service Unavailable\n"
    "non-200 OK status code: 503 Service Unavailable",
    "remote: Internal Server Error",
    "remote: Internal Server Error\n"
    " ! [remote rejected] HEAD -> ticket/example-app (Internal Server Error)\n"
    "error: failed to push some refs",
    " ! [remote rejected] HEAD -> ticket/example-app (Service Unavailable)",
    " ! [remote rejected] HEAD -> ticket/example-app (Bad Gateway)",
]
NOTICE = "GitHub server errors: merges and pushes wait"


def folder(workflow: Workflow) -> Path:
    return workflow.repo / ".local/workflow"


def train(workflow: Workflow) -> dict[str, Any]:
    return json.loads((folder(workflow) / "merge-train.json").read_text())


def note(workflow: Workflow, number: int = 7) -> Path:
    return folder(workflow) / f"merge-wanted-{number}"


def drive(workflow: Workflow):
    return workflow.run_script("merge-when-current", "--drive", "7", cwd=workflow.worktree(7))


def keep_authorization(workflow: Workflow, head: str) -> None:
    assert note(workflow).exists(), "the outage revoked the passed review handoff"
    assert train(workflow)["approved"].get("7") == head, "the outage revoked train approval"
    assert not list(folder(workflow).glob("merge-review-*")), "the outage requested a reviewer"


def fail_merges(workflow: Workflow, *, completed=False, unreadable=False, changed=False,
                hang=False, only="", error=ERRORS[0]) -> None:
    """A response can be lost before or after the server accepts the write."""
    workflow.fake("gh-break85-delegate", (workflow.bin / "gh").read_text())
    mode = {"completed": completed, "unreadable": unreadable, "changed": changed,
            "hang": hang, "only": only, "error": error}
    (workflow.state / "break85-mode.json").write_text(json.dumps(mode))
    workflow.fake("gh", f"#!{sys.executable}\n" + r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

state = Path("@STATE@")
mode = json.loads((state / "break85-mode.json").read_text())
args = sys.argv[1:]
delegate = str(Path(sys.argv[0]).with_name("gh-break85-delegate"))
if args[:2] == ["pr", "merge"] and (not mode["only"] or args[2] == mode["only"]):
    with (state / "break85-writes").open("a") as log:
        log.write(args[2] + "\n")
    if mode["completed"]:
        done = subprocess.run([delegate, *args], check=False, timeout=10)
        if done.returncode:
            sys.exit(done.returncode)
    if mode["changed"]:
        path = state / "github.json"
        data = json.loads(path.read_text())
        data["pulls"][args[2]]["headRefOid"] = "f" * 40
        path.write_text(json.dumps(data))
    (state / "break85-after-write").touch()
    if mode["hang"]:
        time.sleep(30)
    print(mode["error"], file=sys.stderr)
    sys.exit(1)
if args[:2] == ["pr", "view"] and (state / "break85-after-write").exists():
    with (state / "break85-readbacks").open("a") as log:
        log.write(args[2] + "\n")
    if mode["unreadable"]:
        sys.exit("non-200 OK status code: 503 Service Unavailable")
os.execv(delegate, [delegate, *args])
''')


def restore_merges(workflow: Workflow) -> None:
    workflow.fake("gh", (workflow.bin / "gh-break85-delegate").read_text())


@pytest.mark.parametrize("error", ERRORS)
def test_incident_formats_and_mixed_client_failures(gate, error):
    assert gate.transient(error)
    assert gate.github_server_error(error)
    assert gate._transient_refresh_push(error)
    for client in ("< HTTP/2 401 Unauthorized", "HTTP 403", "returned error: 404",
                   "non-200 OK status code: 422 Unprocessable Entity"):
        for mixed in (client + "\n" + error, error + "\n" + client):
            assert not gate.transient(mixed)
            assert not gate.github_server_error(mixed)
            assert not gate._transient_refresh_push(mixed)


@pytest.mark.parametrize("rejection", ["[rejected] HEAD -> ticket/example-app (non-fast-forward)",
    "[remote rejected] HEAD -> ticket/example-app (pre-receive hook declined)",
    "[remote rejected] HEAD -> ticket/example-app (protected branch)"])
def test_server_preamble_does_not_override_a_real_branch_rejection(gate, rejection):
    assert not gate._transient_refresh_push("remote: Internal Server Error\n" + rejection)


def test_merge_outage_repeats_without_losing_review_then_recovers(handed):
    workflow = handed.workflow
    workflow.orchestrator("idle")
    fail_merges(workflow)
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stderr
        keep_authorization(workflow, handed.head)
        assert workflow.run_script("wake").returncode == 0
    assert sum(message.count(NOTICE) for message in workflow.sent()) == 1
    assert (workflow.state / "break85-readbacks").exists()
    restore_merges(workflow)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert not note(workflow).exists()
    assert not (folder(workflow) / "github-server-errors").exists()


@pytest.mark.parametrize("changed", [False, True])
def test_accepted_merge_requires_readback_of_the_expected_head(handed, changed):
    workflow = handed.workflow
    fail_merges(workflow, completed=True, changed=changed)
    done = drive(workflow)
    assert done.returncode == (3 if changed else 0), done.stderr
    assert (workflow.state / "break85-readbacks").read_text().splitlines()[0] == "7"
    if changed:
        keep_authorization(workflow, handed.head)
    else:
        assert "7" not in train(workflow)["waiting"]
        assert (folder(workflow) / "main-tree-green.json").exists()


@pytest.mark.parametrize("completed", [False, True])
def test_failed_readback_preserves_handoff_and_next_tick_reconciles(handed, completed):
    workflow = handed.workflow
    fail_merges(workflow, completed=completed, unreadable=True)
    done = drive(workflow)
    assert done.returncode == 3, done.stderr
    keep_authorization(workflow, handed.head)
    restore_merges(workflow)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert not note(workflow).exists()
    assert not list(folder(workflow).glob("merge-review-*"))


def test_two_callers_behind_a_busy_merge_lock_keep_one_notice(handed):
    workflow = handed.workflow
    fail_merges(workflow)
    with (folder(workflow) / "merge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(drive, workflow) for _ in range(2)]
            try:
                time.sleep(6)
                assert not (workflow.state / "break85-writes").exists()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            outcomes = [call.result(timeout=40) for call in calls]
    assert [done.returncode for done in outcomes] == [3, 3]
    keep_authorization(workflow, handed.head)
    pending = list((folder(workflow) / "stuck.pending").glob("*.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text()) == {"line": NOTICE}
    assert all(path.stat().st_mode & 0o077 == 0 for path in
               [*pending, folder(workflow) / "github-server-errors"])


@pytest.mark.parametrize("accepted", [False, True])
def test_refresh_response_loss_reuses_the_saved_commit_and_review(handed, accepted):
    from workflow.test_merge_driver import _drop_refresh_pushes, _stale_refresh

    workflow = handed.workflow
    _stale_refresh(handed)
    _drop_refresh_pushes(workflow, [ERRORS[-1]], published=accepted)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    keep_authorization(workflow, handed.head)
    saved = json.loads((folder(workflow) / "merge-refresh-7.json").read_text())
    commit = saved["commit"]
    assert saved["failures"] == 1
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == commit
    keep_authorization(workflow, commit)
    assert not (folder(workflow) / "merge-refresh-7.json").exists()


@pytest.mark.parametrize("member", ["7", "8"])
def test_a_tested_batch_keeps_handoffs_when_a_squash_gets_502(github, member):
    from workflow.test_merge_driver import _handed_batch

    _handed_batch(github)
    workflow = github.workflow
    assert workflow.run_script("tick").returncode == 0
    github.finish_batch()
    github.save()
    fail_merges(workflow, only=member)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert note(workflow, int(member)).exists(), (
        "a GitHub 502 during batch squash revoked the reviewed handoff\n"
        + "\n".join(line for line in done.stdout.splitlines()
                    if "hand-back" in line or "refused:" in line))
    assert not list(folder(workflow).glob("merge-review-*"))
    assert (folder(workflow) / "github-server-errors").exists(), (
        "a GitHub 502 during batch squash did not queue the outage notice")


def test_successful_batch_clears_the_marker_so_a_later_outage_can_be_reported(github):
    from workflow.test_merge_driver import _handed_batch

    _handed_batch(github)
    workflow = github.workflow
    assert workflow.run_script("tick").returncode == 0
    github.finish_batch()
    github.save()
    marker = folder(workflow) / "github-server-errors"
    marker.write_text(json.dumps({"line": NOTICE}))
    marker.chmod(0o600)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert not list(folder(workflow).glob("merge-wanted-*"))
    assert not marker.exists(), "successful batch merges left the outage marker set"


@pytest.mark.parametrize("outage", [False, True])
def test_lost_check_recovery_push_keeps_the_review_during_server_failure(handed, outage):
    from workflow.test_merge_driver import _drop_refresh_pushes

    workflow = handed.workflow
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    runs = ("repos/example-org/example-app/actions/workflows/ci.yml/runs"
            f"?head_sha={handed.head}&event=pull_request&per_page=100")
    handed.api[runs]["workflow_runs"][0]["updated_at"] = old
    handed.pulls["7"]["statusCheckRollup"].append({
        "__typename": "CheckRun", "name": "CI result", "status": "IN_PROGRESS",
        "conclusion": None, "startedAt": old,
        "detailsUrl": "https://github.com/example-org/example-app/actions/runs/20/job/22",
    })
    handed.save()
    if outage:
        _drop_refresh_pushes(workflow, [ERRORS[-1]])
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    expected = handed.head if outage else workflow.git("rev-parse", "HEAD",
                                                      cwd=workflow.worktree(7))
    assert note(workflow).exists(), (
        "a server error on the lost-check recovery push revoked the passed handoff\n"
        + "\n".join(line for line in done.stdout.splitlines() if "hand-back" in line))
    keep_authorization(workflow, expected)
    assert not (workflow.state / "merged").exists()


def test_partial_ci_rerun_and_late_metadata_still_wait_after_an_outage(handed):
    workflow = handed.workflow
    fail_merges(workflow)
    assert drive(workflow).returncode == 3
    key = ("repos/example-org/example-app/actions/workflows/ci.yml/runs"
           f"?head_sha={handed.head}&event=pull_request&per_page=100")
    run = handed.api[key]["workflow_runs"][0]
    run.update(run_attempt=2, status="in_progress", conclusion=None)
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    jobs[-1].update(status="in_progress", conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    keep_authorization(workflow, handed.head)
    jobs[-1].update(status="completed", conclusion="success")
    handed.pulls["7"]["statusCheckRollup"][0].update(status="COMPLETED", conclusion="SUCCESS")
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    keep_authorization(workflow, handed.head)
    assert (workflow.state / "break85-writes").read_text().splitlines() == ["7"]
    run.update(status="completed", conclusion="success")
    handed.save()
    restore_merges(workflow)
    assert workflow.run_script("tick").returncode == 0
    assert (workflow.state / "merged").read_text() == "7"


def test_new_notice_folders_are_private_and_a_missing_marker_can_be_rebuilt(gate, tmp_path):
    state = tmp_path / "state"
    gate.note_github_server_errors(state)
    pending = state / "stuck.pending/github-server-errors.json"
    marker = state / "github-server-errors"
    assert json.loads(pending.read_text()) == {"line": NOTICE}
    assert pending.stat().st_mode & 0o077 == 0
    assert marker.stat().st_mode & 0o077 == 0
    marker.unlink()
    gate.note_github_server_errors(state)
    assert json.loads(marker.read_text()) == {"line": NOTICE}
    assert len(list(pending.parent.iterdir())) == 1


def test_wake_delivery_failure_does_not_lose_the_outage_notice(handed):
    workflow = handed.workflow
    fail_merges(workflow)
    assert drive(workflow).returncode == 3
    assert workflow.run_script("wake").returncode == 0
    pending = folder(workflow) / "stuck.pending/github-server-errors.json"
    assert pending.exists()
    workflow.orchestrator("idle")
    later = str(int(time.time()) + 301)
    assert workflow.run_script("wake", env={"WORKFLOW_NOW": later}).returncode == 0
    assert not pending.exists()
    assert sum(message.count(NOTICE) for message in workflow.sent()) == 1


def test_error_body_is_not_executed_or_copied_into_the_outage_notice(handed):
    workflow = handed.workflow
    fail_merges(workflow, error=ERRORS[0] + "\n$(touch break85-executed); example-app-canary")
    done = drive(workflow)
    assert done.returncode == 3, done.stderr
    assert not (workflow.worktree(7) / "break85-executed").exists()
    pending = folder(workflow) / "stuck.pending/github-server-errors.json"
    assert json.loads(pending.read_text()) == {"line": NOTICE}


def test_a_removed_handoff_cannot_authorize_a_retry(handed):
    workflow = handed.workflow
    fail_merges(workflow)
    assert drive(workflow).returncode == 3
    note(workflow).unlink()
    done = drive(workflow)
    assert done.returncode == 1
    assert (workflow.state / "break85-writes").read_text().splitlines() == ["7"]


def test_merge_deadline_keeps_handoff_and_reads_back_the_write(handed):
    workflow = handed.workflow
    fail_merges(workflow, hang=True)
    bootstrap = '''import sys
import merge_gate as gate
original = gate.run_command
def bounded(*args, **kwargs):
    if args[:3] == ("gh", "pr", "merge"):
        kwargs["timeout"] = 0.75
    return original(*args, **kwargs)
gate.run_command = bounded
sys.argv = ["merge-when-current", "--drive", "7"]
sys.exit(gate.main())
'''
    done = subprocess.run(
        [sys.executable, "-c", bootstrap], cwd=workflow.worktree(7),
        env={**workflow.env, "PYTHONPATH": str(workflow.repo / "scripts/workflow")},
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert (workflow.state / "break85-after-write").exists(), "the merge call never started"
    assert done.returncode == 3, "a merge deadline became a refusal: " + done.stderr
    assert (workflow.state / "break85-readbacks").exists()
    keep_authorization(workflow, handed.head)
