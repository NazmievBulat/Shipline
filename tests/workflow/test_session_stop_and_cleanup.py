"""`session stop` ends a session; `session cleanup` removes what a merged pull request left."""

import subprocess
from pathlib import Path

from workflow_harness import Workflow


def _branches(workflow: Workflow) -> list[str]:
    return workflow.git("branch", "--format=%(refname:short)").splitlines()


def test_stop_ends_the_session_and_keeps_the_worktree(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.running("audit-7")

    done = workflow.run("stop", "ticket-7")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert workflow.is_running("audit-7")
    assert worktree.is_dir()


def test_stop_forgets_the_link_and_the_idle_mark_of_the_ended_session(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")
    notes = worktree / ".local" / "workflow"
    notes.mkdir(parents=True)
    ended = ("ticket-7.url", "ticket-7.idle", "ticket-7.waiting", "ticket-7.pushed")
    for name in (*ended, "audit-7.url", "audit-7.pushed"):
        (notes / name).touch()

    workflow.run("stop", "ticket-7")

    assert sorted(path.name for path in notes.iterdir()) == ["audit-7.pushed", "audit-7.url"]


def test_stop_forgets_the_notes_of_an_ended_audit(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    workflow.running("audit-7")
    notes = worktree / ".local" / "workflow"
    notes.mkdir(parents=True)
    ended = ("audit-7.url", "audit-7.idle", "audit-7.waiting", "audit-7.pushed")
    for name in (*ended, "ticket-7.url", "ticket-7.pushed"):
        (notes / name).touch()

    workflow.run("stop", "audit-7")

    assert sorted(path.name for path in notes.iterdir()) == ["ticket-7.pushed", "ticket-7.url"]


def test_stopping_a_session_that_does_not_run_says_so(workflow: Workflow) -> None:
    workflow.socket("ticket-7")

    done = workflow.run("stop", "ticket-7")

    assert done.returncode == 0
    assert done.stdout.splitlines() == ["ticket-7 is not running"]
    assert workflow.calls("tmux ticket-7 kill-server") == []


def _notes_of_ticket_7(workflow: Workflow, *names: str) -> Path:
    notes = workflow.add_worktree(7) / ".local" / "workflow"
    notes.mkdir(parents=True)
    for name in names:
        (notes / name).touch()
    return notes


def test_stop_fails_and_keeps_the_notes_of_a_session_that_still_runs(workflow: Workflow) -> None:
    """The tick reads the idle mark of a session that runs; losing it hides its completion."""
    notes = _notes_of_ticket_7(workflow, "ticket-7.url", "ticket-7.idle")
    workflow.running("ticket-7")
    workflow.survives_kill("ticket-7")

    done = workflow.run("stop", "ticket-7")

    assert done.returncode == 1
    assert "stopped" not in done.stdout
    assert done.stderr.splitlines() == ["ticket-7 is still running"]
    assert workflow.is_running("ticket-7")
    assert sorted(path.name for path in notes.iterdir()) == ["ticket-7.idle", "ticket-7.url"]


def test_stop_fails_when_the_codex_worker_beside_the_bridge_still_runs(
    workflow: Workflow,
) -> None:
    notes = _notes_of_ticket_7(workflow, "ticket-7.idle", "ticket-7.codex")
    workflow.running("ticket-7")
    workflow.running("codex-7")
    workflow.survives_kill("codex-7")

    done = workflow.run("stop", "ticket-7")

    assert done.returncode == 1
    assert "stopped" not in done.stdout
    assert done.stderr.splitlines() == ["codex-7 is still running"]
    assert workflow.is_running("codex-7")
    assert workflow.is_running("ticket-7")
    assert sorted(path.name for path in notes.iterdir()) == ["ticket-7.codex", "ticket-7.idle"]


def test_stop_counts_an_already_ended_bridge_as_stopped(workflow: Workflow) -> None:
    """Its `kill-server` fails, as the real one does when no server runs."""
    notes = _notes_of_ticket_7(workflow, "ticket-7.idle", "ticket-7.codex")
    workflow.running("codex-7")

    done = workflow.run("stop", "ticket-7")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["ticket-7 stopped"]
    assert not workflow.is_running("codex-7")
    assert list(notes.iterdir()) == []


def test_cleanup_keeps_a_closed_worktree_whose_session_still_runs_and_goes_on(
    workflow: Workflow,
) -> None:
    closed = workflow.add_worktree(7, state="CLOSED")
    notes = closed / ".local" / "workflow"
    notes.mkdir(parents=True)
    (notes / "ticket-7.idle").touch()
    workflow.running("ticket-7")
    workflow.survives_kill("ticket-7")
    merged = workflow.add_worktree(8, state="MERGED")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "ticket-7: kept, ticket-7 is still running",
        "ticket-8: removed, its pull request is merged",
    ]
    assert (notes / "ticket-7.idle").exists()
    assert not merged.exists()


def test_cleanup_removes_the_worktree_and_branch_of_a_merged_pull_request(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    (worktree / ".local").mkdir()
    (worktree / ".local" / "ignored.txt").write_text("only ignored files\n")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert not worktree.exists()
    assert _branches(workflow) == ["main"]
    assert "ticket-7" in done.stdout


def test_cleanup_waits_for_a_worker_that_still_runs_after_its_merge(workflow: Workflow) -> None:
    """After merging, a worker still moves the pull requests that were based on its own."""
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running("ticket-7")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert worktree.is_dir()
    assert workflow.branch(7) in _branches(workflow)
    assert "ticket-7: kept, its worker is still running" in done.stdout


def test_cleanup_removes_a_merged_worktree_once_its_worker_has_ended(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running("ticket-7")
    workflow.running("ticket-8")
    workflow.run("cleanup")

    workflow.run("stop", "ticket-7")
    workflow.run("cleanup")

    assert not worktree.exists()
    assert workflow.is_running("ticket-8")


def test_cleanup_waits_for_a_audit_that_still_runs_after_its_merge(workflow: Workflow) -> None:
    """After merging, an audit still moves the pull requests that were based on this one."""
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running("audit-7")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("audit-7")
    assert worktree.is_dir()
    assert workflow.branch(7) in _branches(workflow)
    assert "ticket-7: kept, its audit is still running" in done.stdout


def test_cleanup_removes_a_merged_worktree_once_its_audit_has_ended(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running("audit-7")
    workflow.run("cleanup")

    workflow.run("stop", "audit-7")
    workflow.run("cleanup")

    assert not worktree.exists()


def test_cleanup_keeps_a_merged_worktree_with_an_untracked_file(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    (worktree / "notes.txt").write_text("not committed\n")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert (worktree / "notes.txt").is_file()
    assert workflow.branch(7) in _branches(workflow)
    assert "ticket-7: kept, it has uncommitted changes" in done.stdout


def test_cleanup_keeps_a_merged_worktree_with_a_changed_file(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    (worktree / ".gitignore").write_text("changed\n")

    done = workflow.run("cleanup")

    assert worktree.is_dir()
    assert "ticket-7: kept, it has uncommitted changes" in done.stdout


def test_cleanup_keeps_a_merged_worktree_whose_head_github_never_saw(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "fix: not pushed", cwd=worktree)

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert worktree.is_dir()
    assert workflow.branch(7) in _branches(workflow)
    assert "ticket-7: kept, its head is not the commit GitHub recorded" in done.stdout


def test_cleanup_leaves_the_worker_of_a_merged_worktree_with_an_untracked_file_running(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    (worktree / "notes.txt").write_text("not committed\n")
    workflow.running("ticket-7")

    done = workflow.run("cleanup")

    assert workflow.is_running("ticket-7")
    assert (worktree / "notes.txt").is_file()
    assert "ticket-7: kept, its worker is still running" in done.stdout


def test_cleanup_stops_the_sessions_of_a_closed_pull_request_and_keeps_its_worktree(
    workflow: Workflow,
) -> None:
    worktree = workflow.add_worktree(7, state="CLOSED")
    workflow.running("ticket-7")
    workflow.running("audit-7")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("audit-7")
    assert worktree.is_dir()
    assert workflow.branch(7) in _branches(workflow)
    assert "ticket-7: kept, its pull request was closed without merging" in done.stdout


def test_cleanup_leaves_an_open_pull_request_alone(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert worktree.is_dir()
    assert done.stdout == ""


def test_cleanup_never_touches_a_worktree_not_named_after_a_ticket(workflow: Workflow) -> None:
    workflow.add_pull_request(7, state="MERGED")
    workflow.git("fetch", "--quiet", "origin", workflow.branch(7))
    kept: list[Path] = []
    for name in ("pr-7", "landing-7", "ticket-7-old", "ticket-x"):
        path = workflow.repo / ".claude" / "worktrees" / name
        workflow.git("worktree", "add", "--quiet", "--detach", str(path), "FETCH_HEAD")
        kept.append(path)
    outside = workflow.root / "elsewhere" / "ticket-7"
    workflow.git("worktree", "add", "--quiet", "--detach", str(outside), "FETCH_HEAD")

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert all(path.is_dir() for path in [*kept, outside])
    assert workflow.calls("gh") == []


def test_cleanup_keeps_a_worktree_whose_pull_request_cannot_be_read(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.running("ticket-7")
    (workflow.state / "pr" / "7").unlink()

    done = workflow.run("cleanup")

    assert done.returncode == 0
    assert worktree.is_dir()
    assert workflow.is_running("ticket-7")
    assert "cannot read pull request 7" in done.stderr


def test_cleanup_keeps_a_merged_worktree_that_git_will_not_remove(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, state="MERGED")
    workflow.add_worktree(8, state="MERGED")
    workflow.git("worktree", "lock", str(worktree))

    done = workflow.run("cleanup")

    assert done.returncode == 0, done.stderr
    assert worktree.is_dir()
    assert workflow.branch(7) in _branches(workflow)
    assert "ticket-7: kept, git could not remove it" in done.stdout
    assert not workflow.worktree(8).exists()


def test_a_worker_can_call_the_script_from_its_own_worktree(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    group = workflow.running("ticket-7")
    (worktree / ".local" / "workflow").mkdir(parents=True)
    (worktree / ".local" / "workflow" / "ticket-7.idle").touch()
    script = str(worktree / "scripts" / "workflow" / "session")

    protect = subprocess.run(
        ["bash", script, "protect", "ticket-7"], cwd=worktree, env=workflow.env, check=False
    )
    listed = subprocess.run(
        ["bash", script, "list"],
        cwd=worktree,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert protect.returncode == 0
    assert (group / "memory.low").read_text().strip() == str(4 * 1024**3)
    assert listed.stdout.splitlines() == ["ticket-7  idle  0 MB held  0 MB in swap"]


def test_cleanup_called_from_a_worktree_finds_the_others(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    finished = workflow.add_worktree(8, state="MERGED")
    script = str(worktree / "scripts" / "workflow" / "session")

    done = subprocess.run(["bash", script, "cleanup"], cwd=worktree, env=workflow.env, check=False)

    assert done.returncode == 0
    assert not finished.exists()
    assert worktree.is_dir()
