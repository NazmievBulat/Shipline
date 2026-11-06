"""Independent breaker cases for local machine waits and their recovery, PR 88.

Only this file is run. Existing fixtures provide isolated repositories and GitHub
stand-ins; no existing test is invoked. All diagnostics use invented example values.
"""

import fcntl
import json
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from workflow.test_merge_driver import (
    _folder,
    _note,
    _stale_refresh,
    _started,
    _train,
)
from workflow.test_workflow_commands import gate, github, handed


OWNERSHIP = (
    "OSError: The temporary directory /tmp/pytest-of-example is not owned by "
    "the current user. Fix this and try again."
)
ERRORS = [
    OWNERSHIP,
    "OSError: [Errno 28] No space left on device",
    "OSError: [Errno 24] Too many open files",
    "OSError: [Errno 12] Cannot allocate memory",
    "OSError: [Errno 30] Read-only file system",
]


def _precheck(workflow: Any, output: str, code: int) -> None:
    workflow.fake(
        "pre-train-checks",
        "#!/bin/bash\nprintf '%s\\n' " + shlex.quote(output) + f" >&2\nexit {code}\n",
    )


def _tick(workflow: Any) -> subprocess.CompletedProcess[str]:
    done = workflow.run_script("tick")
    assert done.returncode == 0, "tick failed unexpectedly"
    return done


@pytest.mark.parametrize("error", ERRORS)
def test_breaker_recognizes_required_diagnostics(gate: Any, tmp_path: Path, error: str) -> None:
    assert gate.machine_error("starting checks\n" + error, tmp_path), "machine error missed"


@pytest.mark.parametrize("form", ["python", "shell", "coreutils"])
def test_breaker_permission_scope(gate: Any, tmp_path: Path, form: str) -> None:
    worktree = tmp_path / "example-app"
    worktree.mkdir()
    outside = tmp_path / "example cache"
    outside.mkdir()

    def diagnostic(path: Path) -> str:
        return {
            "python": f"PermissionError: [Errno 13] Permission denied: '{path}'",
            "shell": f"bash: '{path}': Permission denied",
            "coreutils": f"mkdir: cannot create directory '{path}': Permission denied",
        }[form]

    assert gate.machine_error(diagnostic(outside), worktree)
    assert gate.machine_error(diagnostic(worktree / "cache"), worktree) is None
    (worktree / "linked-cache").symlink_to(outside, target_is_directory=True)
    assert gate.machine_error(diagnostic(worktree / "linked-cache"), worktree)


@pytest.mark.parametrize("code", [1, 75, 124])
def test_breaker_repeated_ticks_preserve_authorization_then_recover(handed: Any, code: int) -> None:
    workflow = handed.workflow
    workflow.orchestrator("busy")
    authorization = _note(workflow).read_bytes()
    _precheck(workflow, OWNERSHIP, code)
    for _ in range(3):
        _tick(workflow)
        assert _note(workflow).read_bytes() == authorization, "machine wait lost authorization"
        assert _train(workflow)["waiting"] == ["7"]
        assert not _started(workflow), "machine wait started a fresh reviewer"
        wait = json.loads((_folder(workflow) / "pre-train-wait-7.json").read_text())
        assert wait.get("machine") and "second" not in wait
    assert "\n".join(workflow.sent()).count("Machine error:") == 1
    _precheck(workflow, "all checks passed", 0)
    _tick(workflow)
    assert (workflow.state / "merged").read_text() == "7"
    assert not _started(workflow)
    assert not (_folder(workflow) / "machine-errors").exists()


@pytest.mark.parametrize("code", [1, 75])
def test_breaker_named_test_and_unrelated_failure_revoke_authorization(handed: Any, code: int) -> None:
    workflow = handed.workflow
    _precheck(
        workflow,
        OWNERSHIP + "\n\x1b[31mFAILED\x1b[0m tests/test_example.py::test_example - AssertionError",
        code,
    )
    _tick(workflow)
    assert not _note(workflow).exists(), "a named test failure retained authorization"
    assert _started(workflow) == ["review-7"]
    assert not (_folder(workflow) / "machine-errors").exists()


def test_breaker_unrelated_failure_hands_back(handed: Any) -> None:
    workflow = handed.workflow
    _precheck(workflow, "inventory is stale", 1)
    _tick(workflow)
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]


def test_breaker_missing_command_is_optional(handed: Any) -> None:
    workflow = handed.workflow
    path = workflow.repo / "shipline.json"
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    config = json.loads(path.read_text())
    config["commands"].pop("pre_train_checks")
    path.write_text(json.dumps(config))
    _tick(workflow)
    assert (workflow.state / "merged").read_text() == "7"


def test_breaker_refresh_push_retries_without_using_failure_budget(handed: Any) -> None:
    workflow = handed.workflow
    _stale_refresh(handed)
    workflow.fake(
        "git",
        '#!/bin/bash\nif [[ $1 == push ]]; then\n'
        "echo 'OSError: Too many open files' >&2\nexit 1\nfi\n"
        'exec /usr/bin/git "$@"\n',
    )
    authorization = _note(workflow).read_bytes()
    for _ in range(4):
        _tick(workflow)
        assert _note(workflow).read_bytes() == authorization
        assert not _started(workflow)
        assert not (_folder(workflow) / "merge-refresh-7.json").exists()
        assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == handed.head
    workflow.fake("git", '#!/bin/bash\nexec /usr/bin/git "$@"\n')
    _tick(workflow)
    assert json.loads(_note(workflow).read_text())["head"] != handed.head


def test_breaker_timeout_with_retained_diagnostic_waits(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: Any, **kwargs: Any) -> None:
        raise gate.TimedOut("example command timed out", OWNERSHIP, 1)

    monkeypatch.setattr(gate, "repository_command", timeout)
    monkeypatch.setattr(gate, "git", lambda *args: "a" * 40)
    assert gate.pre_train_checks("7", tmp_path)
    assert json.loads((tmp_path / "pre-train-wait-7.json").read_text())["machine"]


def test_breaker_machine_lock_serializes_overlapping_notices(
    gate: Any, tmp_path: Path,
) -> None:
    lock_path = tmp_path / "machine-errors.lock"
    with lock_path.open("w") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(gate.note_machine_result, tmp_path, ERRORS[1]) for _ in range(2)]
            try:
                for future in futures:
                    with pytest.raises(FutureTimeout):
                        future.result(timeout=0.1)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            for future in futures:
                future.result(timeout=5)
    notices = list((tmp_path / "stuck.pending").glob("*.json"))
    assert len(notices) == 1
    for path in [lock_path, tmp_path / "machine-errors", *notices]:
        assert path.stat().st_mode & 0o077 == 0, "machine state has public permissions"


def test_breaker_concurrent_handoff_cannot_erase_driver_failure(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A timer tick and a reviewer's direct handoff have separate environments.
    # The tick lock excludes another tick, but not a concurrent handoff. Each
    # operation takes the real machine lock; this interleaving is deterministic.
    monkeypatch.setenv("WORKFLOW_MACHINE_ERROR_TICK", "example-tick-a")
    gate.note_machine_result(tmp_path, ERRORS[1])
    monkeypatch.delenv("WORKFLOW_MACHINE_ERROR_TICK")
    gate.note_machine_result(tmp_path, ERRORS[1])
    monkeypatch.setenv("WORKFLOW_MACHINE_ERROR_TICK", "example-tick-a")
    gate.note_machine_result(tmp_path, None)  # another member passes in tick A
    gate.finish_machine_error_tick(tmp_path, "example-tick-a")
    assert (tmp_path / "machine-errors").exists(), "tick A erased an outage it observed itself"
    monkeypatch.delenv("WORKFLOW_MACHINE_ERROR_TICK")
    gate.note_machine_result(tmp_path, ERRORS[1])
    assert len(list((tmp_path / "stuck.pending").glob("*.json"))) == 1, "ongoing outage notified twice"


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_breaker_partial_ci_rerun_after_machine_wait_keeps_authorization(handed: Any, status: str) -> None:
    workflow = handed.workflow
    authorization = _note(workflow).read_bytes()
    _precheck(workflow, OWNERSHIP, 1)
    _tick(workflow)
    _precheck(workflow, "checks passed", 0)
    original_api = json.loads(json.dumps(handed.api))
    original_pulls = json.loads(json.dumps(handed.pulls))
    for key, data in handed.api.items():
        if "head_sha=" + handed.head in key and isinstance(data, dict):
            for run in data.get("workflow_runs", []):
                run.update(run_attempt=2, status=status, conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    handed.save()
    for _ in range(2):
        _tick(workflow)
        assert _note(workflow).read_bytes() == authorization
        assert not (workflow.state / "merged").exists()
        assert not _started(workflow)
    handed.api = original_api
    handed.pulls = original_pulls
    handed.save()
    _tick(workflow)
    assert (workflow.state / "merged").read_text() == "7"


def test_breaker_lost_check_after_machine_wait_refreshes_without_review(handed: Any) -> None:
    workflow = handed.workflow
    _precheck(workflow, OWNERSHIP, 1)
    _tick(workflow)
    _precheck(workflow, "checks passed", 0)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    key = (
        "repos/example-org/example-app/actions/workflows/ci.yml/runs"
        f"?head_sha={handed.head}&event=pull_request&per_page=100"
    )
    handed.api[key]["workflow_runs"][0]["updated_at"] = old
    handed.pulls["7"]["statusCheckRollup"].append({
        "__typename": "CheckRun", "name": "CI result", "status": "IN_PROGRESS",
        "conclusion": None, "startedAt": old,
        "detailsUrl": "https://github.com/example-org/example-app/actions/runs/20/job/22",
    })
    handed.save()
    _tick(workflow)
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != handed.head, "lost check was not refreshed"
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    assert not _started(workflow)
    assert not (workflow.state / "merged").exists()
    handed.head = refreshed
    handed.pulls["7"]["headRefOid"] = refreshed
    handed.pulls["7"]["statusCheckRollup"].pop()
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {refreshed}\n")
    handed.ci(handed.base, refreshed, run_id=50)
    handed.save()
    _tick(workflow)
    assert (workflow.state / "merged").read_text() == "7"
    assert not _started(workflow)


def test_breaker_clean_tick_rearms_but_partial_retry_does_not(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "machine-errors"
    for tick, error in [("outage", ERRORS[1]), ("partial", None), ("partial", ERRORS[1])]:
        monkeypatch.setenv("WORKFLOW_MACHINE_ERROR_TICK", tick)
        gate.note_machine_result(tmp_path, error)
    gate.finish_machine_error_tick(tmp_path, "partial")
    assert marker.exists()
    monkeypatch.setenv("WORKFLOW_MACHINE_ERROR_TICK", "recovered")
    gate.note_machine_result(tmp_path, None)
    gate.finish_machine_error_tick(tmp_path, "recovered")
    assert not marker.exists()
    gate.note_machine_result(tmp_path, ERRORS[1])
    assert len(list((tmp_path / "stuck.pending").glob("*.json"))) == 2


def test_breaker_diagnostic_payload_is_not_forwarded_to_wake(
    gate: Any, tmp_path: Path,
) -> None:
    # The value is invented, not a real credential. Commands can print sensitive
    # context on the same line as the recognized machine error.
    payload = "credential=example-sensitive-value"
    diagnostic = "OSError: No space left on device; " + payload
    error = gate.machine_error(diagnostic, tmp_path)
    assert error is not None
    gate.note_machine_result(tmp_path, error)
    notice = next((tmp_path / "stuck.pending").glob("*.json"))
    public_line = json.loads(notice.read_text())["line"]
    assert payload not in public_line, "unfiltered command context queued for outbound wake"


def test_breaker_first_error_line_and_remedy_survive_delivery_retry(handed: Any) -> None:
    workflow = handed.workflow
    workflow.orchestrator("busy")
    _precheck(workflow, "starting checks\n" + OWNERSHIP, 1)
    (workflow.state / "send-fails").touch()
    assert workflow.run_script("tick").returncode == 1
    pending = list((_folder(workflow) / "stuck.pending").glob("machine-errors-*.json"))
    assert len(pending) == 1
    (workflow.state / "send-fails").unlink()
    # Delivery retries have an existing cooldown; advance the injected clock.
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(int(time.time()) + 300)})
    assert done.returncode == 0
    assert not pending[0].exists()
    assert "give the temporary folder back to the user, or remove it" in workflow.sent()[-1]


@pytest.mark.parametrize("kind", ["machine", "named-test"])
def test_breaker_checkout_classification_precedes_exit_75(
    gate: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    output = ERRORS[4]
    if kind == "named-test":
        output += "\nFAILED tests/test_example.py::test_example - AssertionError"
    config = SimpleNamespace(command=lambda name: ("example-checkout",), state_folder="state")
    monkeypatch.setattr(gate, "CONFIG", config)
    monkeypatch.setattr(gate, "main_checkout", lambda: tmp_path)
    monkeypatch.setattr(
        gate, "command", lambda *args, **kwargs: subprocess.CompletedProcess(args, 75, output, ""),
    )
    if kind == "machine":
        with pytest.raises(gate.MachineWaiting):
            gate.repository_command("push_checkout", "install", str(tmp_path))
        assert (tmp_path / "state" / "machine-errors").exists()
    else:
        try:
            done = gate.repository_command("push_checkout", "install", str(tmp_path))
        except gate.Waiting:
            pytest.fail("named failing test became an indefinite checkout wait", pytrace=False)
        assert done.returncode == 75
