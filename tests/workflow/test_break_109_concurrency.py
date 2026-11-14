"""Independent process races, interrupted runs and lock recovery for pass receipts."""

import fcntl
import json
import subprocess
import time

import pytest
from workflow.break_109_helpers import call, checks, configure, folder, receipt, start
from workflow_harness import Workflow


def wait_for(path, process) -> None:
    deadline = time.monotonic() + 8
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), "worker did not reach its barrier"


def finish(process) -> str:
    out, err = process.communicate(timeout=10)
    assert process.returncode == 0, out + err
    return out + err


def stop(process) -> None:
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


def controlled_checks(workflow: Workflow) -> None:
    checks(workflow, '''
        role=${EXAMPLE_CHECK_ROLE:-next}
        if [[ "$role" != next ]]; then
          touch "@STATE@/entered-$role"
          while [[ ! -e "@STATE@/release-$role" ]]; do /bin/sleep 0.01; done
          status=$(cat "@STATE@/status-$role")
          echo "example $role result"
          exit "$status"
        fi
    ''')


@pytest.mark.parametrize("older_status,newer_status", [(0, 0), (0, 75), (0, 1), (75, 0), (1, 0)])
def test_overlapping_calls_never_turn_a_failure_into_a_cached_pass(
    workflow: Workflow, older_status: int, newer_status: int,
) -> None:
    configure(workflow)
    controlled_checks(workflow)
    worktree = workflow.add_worktree(7)
    for role, status in (("older", older_status), ("newer", newer_status)):
        (workflow.state / f"status-{role}").write_text(str(status))
    older = start(workflow, worktree, 'import os\nos.environ["EXAMPLE_CHECK_ROLE"] = "older"\n')
    newer = None
    try:
        wait_for(workflow.state / "entered-older", older)
        newer = start(workflow, worktree, 'import os\nos.environ["EXAMPLE_CHECK_ROLE"] = "newer"\n')
        wait_for(workflow.state / "entered-newer", newer)
        (workflow.state / "release-newer").touch()
        newer_output = finish(newer)
        (workflow.state / "release-older").touch()
        older_output = finish(older)
        assert ", reused" not in newer_output + older_output
        if older_status == newer_status == 0:
            assert ", reused" in call(workflow, worktree)
            assert len(workflow.calls("example-checks")) == 2
        else:
            assert not receipt(workflow).exists()
            assert ", reused" not in call(workflow, worktree)
            assert ", reused" in call(workflow, worktree)
            assert len(workflow.calls("example-checks")) == 3
    finally:
        (workflow.state / "release-older").touch()
        (workflow.state / "release-newer").touch()
        stop(older)
        if newer is not None:
            stop(newer)


def test_main_moving_during_checks_invalidates_the_next_call(workflow: Workflow) -> None:
    configure(workflow)
    controlled_checks(workflow)
    worktree = workflow.add_worktree(7)
    (workflow.state / "status-older").write_text("0")
    process = start(workflow, worktree, 'import os\nos.environ["EXAMPLE_CHECK_ROLE"] = "older"\n')
    try:
        wait_for(workflow.state / "entered-older", process)
        tested_main = workflow.git("rev-parse", "origin/main")
        workflow.git("commit", "--allow-empty", "-m", "test: main advances during checks")
        workflow.git("push", "origin", "main")
        # Keep this run's tracking ref on the main it actually tested.
        workflow.git("update-ref", "refs/remotes/origin/main", tested_main)
        (workflow.state / "release-older").touch()
        assert "outcome:passed" in finish(process)
        assert json.loads(receipt(workflow).read_text())["main"] == tested_main
        assert ", reused" not in call(workflow, worktree)
        assert ", reused" in call(workflow, worktree)
        assert len(workflow.calls("example-checks")) == 2
    finally:
        (workflow.state / "release-older").touch()
        stop(process)


def test_interrupted_check_cannot_leave_the_previous_pass_reusable(workflow: Workflow) -> None:
    configure(workflow)
    checks(workflow)
    worktree = workflow.add_worktree(7)
    call(workflow, worktree)
    workflow.git("commit", "--allow-empty", "-m", "test: checks interrupted on new head", cwd=worktree)
    # Stop immediately after invalidation, before executing the command. This is also
    # the durable state left by a killed gate during the command or its fetch.
    output = call(workflow, worktree, '''
        original_write = g.write_json
        def write(path, value):
            original_write(path, value)
            if path.name == "pre-train-pass-7.json" and "event" in value:
                raise SystemExit(0)
        g.write_json = write
    ''')
    assert not output
    assert set(json.loads(receipt(workflow).read_text())) == {"event"}
    workflow.git("reset", "--hard", "HEAD^", cwd=worktree)
    assert ", reused" not in call(workflow, worktree)
    assert ", reused" in call(workflow, worktree)
    assert len(workflow.calls("example-checks")) == 2


@pytest.mark.parametrize("which_lock", ["receipt", "merge"])
def test_a_busy_lock_recovers_without_rerunning_a_valid_pass(workflow: Workflow, which_lock: str) -> None:
    configure(workflow)
    checks(workflow)
    worktree = workflow.add_worktree(7)
    call(workflow, worktree)
    name = "pre-train-checks-7.lock" if which_lock == "receipt" else "merge.lock"
    with (folder(workflow) / name).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = start(workflow, worktree)
        try:
            if which_lock == "receipt":
                # Hold it far longer than its ordinary read/write critical section.
                with pytest.raises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.5)
                fcntl.flock(lock, fcntl.LOCK_UN)
            assert ", reused" in finish(process)
            assert len(workflow.calls("example-checks")) == 1
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            stop(process)


def test_a_timed_out_main_read_stops_reuse_but_does_not_forge_a_pass(workflow: Workflow) -> None:
    configure(workflow)
    checks(workflow)
    worktree = workflow.add_worktree(7)
    call(workflow, worktree)
    saved = receipt(workflow).read_text()
    output = call(workflow, worktree, '''
        original_read = g.read_with_retries
        def read(*args, **kwargs):
            if args[:2] == ("git", "ls-remote"):
                assert kwargs["timeout"] > 0
                raise g.CommandTimeout("example read timed out", "")
            return original_read(*args, **kwargs)
        g.read_with_retries = read
    ''')
    assert "outcome:refused" in output and ", reused" not in output
    assert receipt(workflow).read_text() == saved
    assert len(workflow.calls("example-checks")) == 1


def test_receipt_has_no_command_text_and_arguments_are_never_shell_code(workflow: Workflow) -> None:
    configure(workflow, "example-checks '--token=example-secret' '$(touch example-injected)' ';' ")
    checks(workflow)
    worktree = workflow.add_worktree(7)
    assert "outcome:passed" in call(workflow, worktree)
    output = call(workflow, worktree)
    saved = receipt(workflow).read_text()
    assert "example-secret" not in saved + output
    assert "touch" not in saved + output
    assert not (worktree / "example-injected").exists()
    assert ", reused" in output
