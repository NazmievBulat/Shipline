"""Independent daily-flow and recovery checks for pre-train pass reuse."""

import json
import os
import time

import pytest
from workflow.break_109_helpers import call, checks, configure, folder, receipt
from workflow.merge_gate_fixtures import GitHub
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import Workflow

github = github_fixture
handed = handed_fixture


def test_repeated_calls_reuse_an_old_pass_without_a_command_or_fetch(workflow: Workflow) -> None:
    configure(workflow)
    checks(workflow)
    worktree = workflow.add_worktree(7)
    assert "outcome:passed" in call(workflow, worktree)
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(receipt(workflow), (old, old))
    # Reuse must not silently fetch or start the expensive command.
    for _ in range(6):
        output = call(workflow, worktree, '''
            original_git = g.git
            def git(*args):
                assert args[0] != "fetch", "a receipt hit fetched main"
                return original_git(*args)
            g.git = git
        ''')
        assert ", reused" in output and "outcome:passed" in output
    assert workflow.calls("example-checks") == ["example-checks"]
    saved = json.loads(receipt(workflow).read_text())
    assert set(saved) == {"head", "main", "command"}
    assert saved["head"] == workflow.git("rev-parse", "HEAD", cwd=worktree)
    assert saved["main"] == workflow.git("rev-parse", "origin/main")
    assert len(saved["command"]) == 64
    for path in (receipt(workflow), folder(workflow) / "pre-train-checks-7.lock"):
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("change", ["head", "main", "arguments", "adapter", "disabled"])
def test_each_input_change_forces_a_fresh_check(workflow: Workflow, change: str) -> None:
    configure(workflow)
    checks(workflow, "git rev-parse origin/main > '@STATE@/tested-main'\n")
    worktree = workflow.add_worktree(7)
    call(workflow, worktree)
    old = json.loads(receipt(workflow).read_text())
    if change == "head":
        workflow.git("commit", "--allow-empty", "-m", "test: changed head", cwd=worktree)
    elif change == "main":
        workflow.git("commit", "--allow-empty", "-m", "test: changed main")
        workflow.git("push", "origin", "main")
        workflow.git("update-ref", "refs/remotes/origin/main", old["main"])
    elif change == "arguments":
        configure(workflow, "example-checks --slow")
    elif change == "adapter":
        configure(workflow, {"run": "example-checks --fast", "form": "exit", "wait_exit": [42]})
    else:
        configure(workflow, None)
        assert "outcome:passed" in call(workflow, worktree)
        assert not receipt(workflow).exists()
        configure(workflow)
    assert ", reused" not in call(workflow, worktree)
    assert ", reused" in call(workflow, worktree)
    assert len(workflow.calls("example-checks")) == 2
    assert (workflow.state / "tested-main").read_text().strip() == workflow.git("rev-parse", "main")


@pytest.mark.parametrize("state", ["missing", "truncated", "in-flight", "wait", "machine"])
def test_incomplete_or_failed_state_never_reuses_a_pass(workflow: Workflow, state: str) -> None:
    configure(workflow)
    checks(workflow)
    worktree = workflow.add_worktree(7)
    call(workflow, worktree)
    if state == "missing":
        receipt(workflow).unlink()
    elif state == "truncated":
        receipt(workflow).write_text('{"head":')
    elif state == "in-flight":
        receipt(workflow).write_text(json.dumps({"event": "interrupted-example-check"}))
    elif state == "wait":
        (folder(workflow) / "pre-train-wait-7.json").write_text(json.dumps({
            "head": workflow.git("rev-parse", "HEAD", cwd=worktree),
            "outcome": "exit 75: try later", "event": "example-event",
        }))
    else:
        (folder(workflow) / "machine-errors").write_text(json.dumps({
            "line": "Machine error: example failure", "event": "example-event",
            "tick": "example-failed-tick", "failed_ticks": [],
        }))
    before = "" if state != "machine" else '''
        import atexit, os
        os.environ["WORKFLOW_MACHINE_ERROR_TICK"] = "example-recovery-tick"
        atexit.register(g.finish_machine_error_tick, folder, "example-recovery-tick")
    '''
    assert ", reused" not in call(workflow, worktree, before)
    assert len(workflow.calls("example-checks")) == 2
    assert not (folder(workflow) / "pre-train-wait-7.json").exists()
    assert ", reused" in call(workflow, worktree)


@pytest.mark.parametrize("failure", ["wait", "hard failure", "missing command", "timeout"])
def test_failed_execution_cannot_resurrect_an_older_pass(workflow: Workflow, failure: str) -> None:
    configure(workflow)
    checks(workflow)
    worktree = workflow.add_worktree(7)
    call(workflow, worktree)
    workflow.git("commit", "--allow-empty", "-m", "test: retry changed head", cwd=worktree)
    before = ""
    if failure == "wait":
        checks(workflow, "echo 'try later'\nexit 75\n")
    elif failure == "hard failure":
        checks(workflow, "echo 'inventory failed'\nexit 1\n")
    elif failure == "missing command":
        (workflow.bin / "example-checks").unlink()
    else:
        checks(workflow, "exec /bin/sleep 30\n")
        before = "g.COMMAND_TIMEOUT = 0.15\n"
    output = call(workflow, worktree, before)
    assert "outcome:wait" in output if failure == "wait" else "outcome:refused" in output
    workflow.git("reset", "--hard", "HEAD^", cwd=worktree)
    checks(workflow)
    assert ", reused" not in call(workflow, worktree)
    assert ", reused" in call(workflow, worktree)


@pytest.mark.parametrize("remote_state", ["queued", "missing", "late", "partial rerun"])
def test_waiting_driver_retains_the_pass_across_check_changes(handed: GitHub, remote_state: str) -> None:
    workflow = handed.workflow
    configure(workflow)
    checks(workflow)
    for cycle in range(3):
        if remote_state == "missing":
            handed.pulls["7"]["statusCheckRollup"] = []
            handed.api.clear()
        else:
            handed.pulls["7"]["statusCheckRollup"] = [
                {"name": "CI result", "status": "QUEUED", "conclusion": None},
            ]
            if remote_state == "late" and cycle:
                handed.pulls["7"]["statusCheckRollup"].append(
                    {"name": "CI: example", "status": "COMPLETED", "conclusion": "SUCCESS"})
            if remote_state == "partial rerun":
                handed.pulls["7"]["statusCheckRollup"].append(
                    {"name": "CI: example", "status": "IN_PROGRESS" if cycle else "QUEUED",
                     "conclusion": None})
        handed.save()
        done = workflow.run_script("merge-when-current", "--drive", "7", cwd=workflow.worktree(7))
        assert done.returncode in (2, 3), done.stdout + done.stderr
        if cycle:
            assert ", reused" in done.stdout, done.stdout + done.stderr
        assert not (workflow.state / "merged").exists()
    assert len(workflow.calls("example-checks")) == 1
