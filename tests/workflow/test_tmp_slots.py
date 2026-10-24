"""Finished work and passing tests must release their temporary file slots."""

import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from workflow_harness import Workflow


def _scratch(workflow: Workflow, worktree: Path) -> Path:
    project = re.sub(r"[^a-zA-Z0-9]", "-", str(worktree))
    scratch = workflow.root / "claude-tmp" / project / "session-id" / "scratchpad"
    scratch.mkdir(parents=True)
    (scratch / "file").write_text("temporary work\n")
    return scratch


def test_cleanup_removes_only_the_removed_worktrees_scratch(workflow: Workflow) -> None:
    ended = workflow.add_worktree(7, state="MERGED")
    running = workflow.add_worktree(8, state="MERGED")
    workflow.running("ticket-8")
    gone = _scratch(workflow, ended)
    kept = _scratch(workflow, running)
    unrelated = _scratch(workflow, workflow.repo)

    done = workflow.run(
        "cleanup", env={"WORKFLOW_CLAUDE_TMP_ROOT": str(workflow.root / "claude-tmp")}
    )

    assert done.returncode == 0, done.stderr
    assert not ended.exists()
    assert not gone.exists()
    assert kept.is_dir()
    assert unrelated.is_dir()


@pytest.mark.parametrize("session", ["ticket-7", "audit-7", "review-7", "codex-7"])
def test_cleanup_keeps_scratch_while_any_session_runs(workflow: Workflow, session: str) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running(session)
    scratch = _scratch(workflow, worktree)

    done = workflow.run(
        "cleanup", env={"WORKFLOW_CLAUDE_TMP_ROOT": str(workflow.root / "claude-tmp")}
    )

    assert done.returncode == 0, done.stderr
    assert worktree.is_dir()
    assert scratch.is_dir()


@pytest.mark.parametrize("reason", ["locked", "changed", "starting", "closed", "open"])
def test_cleanup_keeps_scratch_when_the_worktree_cannot_be_removed(
    workflow: Workflow, reason: str
) -> None:
    state = {"open": "OPEN", "closed": "CLOSED"}.get(reason, "MERGED")
    worktree = workflow.add_worktree(7, state=state)
    scratch = _scratch(workflow, worktree)
    if reason == "locked":
        workflow.git("worktree", "lock", str(worktree))
    if reason == "changed":
        (worktree / "notes").touch()
    locks = workflow.repo / ".local" / "workflow"
    locks.mkdir(parents=True)
    with (locks / "start-7.lock").open("w") as lock:
        if reason == "starting":
            fcntl.flock(lock, fcntl.LOCK_EX)
        done = workflow.run(
            "cleanup", env={"WORKFLOW_CLAUDE_TMP_ROOT": str(workflow.root / "claude-tmp")}
        )

    assert done.returncode == 0, done.stderr
    assert worktree.is_dir()
    assert scratch.is_dir()


@pytest.mark.parametrize("running", [False, True])
def test_hunt_cleanup_releases_only_an_ended_hunts_scratch(
    workflow: Workflow, running: bool
) -> None:
    worktree = workflow.add_hunt("phone")
    scratch = _scratch(workflow, worktree)
    if running:
        workflow.running("hunt-phone")

    done = workflow.run(
        "cleanup", "hunts", env={"WORKFLOW_CLAUDE_TMP_ROOT": str(workflow.root / "claude-tmp")}
    )

    assert done.returncode == 0, done.stderr
    assert scratch.exists() == running


def test_cleanup_does_not_follow_a_project_symlink(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    scratch = _scratch(workflow, workflow.repo)
    project = re.sub(r"[^a-zA-Z0-9]", "-", str(worktree))
    (workflow.root / "claude-tmp" / project).symlink_to(scratch.parent.parent)

    done = workflow.run(
        "cleanup", env={"WORKFLOW_CLAUDE_TMP_ROOT": str(workflow.root / "claude-tmp")}
    )

    assert done.returncode == 0, done.stderr
    assert not worktree.exists()
    assert (scratch / "file").read_text() == "temporary work\n"


@pytest.mark.parametrize("percent", [79, 80, 81, 100])
def test_wake_reports_file_slots_and_warns_only_above_eighty(
    workflow: Workflow, percent: int
) -> None:
    workflow.orchestrator("idle")
    workflow.fake(
        "df",
        '#!/bin/bash\n[ "$*" = "-i /tmp" ] || exit 1\n'
        f"printf 'Filesystem Inodes IUsed IFree IUse%% Mounted on\\ntmpfs 1000 {percent * 10} "
        f"{1000 - percent * 10} {percent}%% /tmp\\n'\n",
    )

    done = workflow.run_script("wake", env={"WORKFLOW_NOW": "1791932400", "TZ": "UTC"})

    assert done.returncode == 0, done.stderr
    [prompt] = workflow.sent()
    assert f"/tmp file slots: {percent}% used ({percent * 10}/1000)" in prompt
    assert ("WARNING" in prompt) == (percent > 80)
    assert f"/tmp file slots: {percent}% used" in workflow.wake_log.read_text()


def test_file_slot_warning_reaches_a_busy_orchestrator_by_day_and_is_not_repeated(
    workflow: Workflow,
) -> None:
    workflow.orchestrator("busy")
    workflow.fake(
        "df",
        "#!/bin/bash\necho 'Filesystem Inodes IUsed IFree IUse% Mounted on'\n"
        "echo 'tmpfs 1000 810 190 81% /tmp'\n",
    )

    for moment in ("1791885600", "1791885960"):
        done = workflow.run_script("wake", env={"WORKFLOW_NOW": moment, "TZ": "UTC"})
        assert done.returncode == 0, done.stderr

    [prompt] = workflow.sent()
    assert "WARNING" in prompt
    assert "/tmp file slots: 81% used (810/1000)" in prompt


def test_a_told_file_slot_warning_does_not_wake_a_busy_orchestrator_on_each_change(
    workflow: Workflow,
) -> None:
    workflow.orchestrator("busy")
    workflow.fake(
        "df",
        "#!/bin/bash\necho 'Filesystem Inodes IUsed IFree IUse% Mounted on'\n"
        "echo 'tmpfs 1000 810 190 81% /tmp'\n",
    )

    for number, moment in enumerate(("1791885600", "1791885960", "1791886320")):
        if number:
            workflow.running(f"ticket-{number}")
        done = workflow.run_script("wake", env={"WORKFLOW_NOW": moment, "TZ": "UTC"})
        assert done.returncode == 0, done.stderr

    assert len(workflow.sent()) == 1


@pytest.mark.parametrize("answer", ["exit 1", "echo 'unreadable'", "echo 'tmpfs 0 0 0 - /tmp'"])
def test_unreadable_file_slots_do_not_break_the_check_in(workflow: Workflow, answer: str) -> None:
    workflow.orchestrator("idle")
    workflow.fake("df", f"#!/bin/bash\n{answer}\n")

    done = workflow.run_script("wake", env={"WORKFLOW_NOW": "1791932400", "TZ": "UTC"})

    assert done.returncode == 0, done.stderr
    [prompt] = workflow.sent()
    assert "/tmp file slots: could not be read" in prompt
    assert "WARNING" not in prompt


def test_passing_tests_release_explicit_basetemp_trees(tmp_path: Path) -> None:
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "def test_pass(tmp_path):\n    (tmp_path / 'temporary-file').write_text('work')\n"
    )
    base = tmp_path / "explicit-base"

    done = _pytest(sample, "--basetemp", str(base))

    assert done.returncode == 0, done.stdout + done.stderr
    assert not list(base.rglob("temporary-file"))


def test_default_pytest_temp_keeps_only_two_failed_runs(tmp_path: Path) -> None:
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "def test_fail(tmp_path):\n"
        "    (tmp_path / 'failure-evidence').write_text('work')\n"
        "    assert False\n"
    )
    temp = tmp_path / "pytest-temp"
    temp.mkdir()
    for _ in range(3):
        done = _pytest(sample, env={"PYTEST_DEBUG_TEMPROOT": str(temp)})
        assert done.returncode == 1, done.stdout + done.stderr

    runs = [path for path in temp.rglob("pytest-*") if path.name[7:].isdigit()]
    assert len(runs) == 2
    assert all(list(path.rglob("failure-evidence")) for path in runs)


def _pytest(
    sample: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    config = Path(__file__).resolve().parents[2] / "pyproject.toml"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(config),
            "--noconftest",
            "-q",
            str(sample),
            *args,
        ],
        env={**os.environ, **(env or {}), "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        check=False,
    )
