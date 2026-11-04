"""Independent startup probes for PR 83; outside calls use the workflow stand-ins.

Run this file alone. Programs and configuration are created only in disposable repos;
no production program, prompt, or PR file is changed by these probes.
"""

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading

import pytest
from workflow_harness import Workflow, install_shipline


def configure(workflow: Workflow, **settings: str) -> Path:
    path = workflow.repo / "shipline.json"
    given = json.loads(path.read_text())
    path.write_text(json.dumps({**given, **settings}))
    return path


def setup(workflow: Workflow, body: str = "", *, name: str = "setup-worktree") -> Path:
    installation = workflow.bin / "example-app installation"
    program = installation / name
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text(
        '#!/bin/bash\nset -eu\nprintf "%s|%s\\n" "$PWD" "$*" >> '
        + f"'{workflow.state}/setup-calls'\n"
        + body
    )
    program.chmod(0o755)
    configure(workflow, install_folder=str(installation), worktree_setup=f"@install/{name}")
    return program


def calls(workflow: Workflow) -> list[str]:
    path = workflow.state / "setup-calls"
    return path.read_text().splitlines() if path.exists() else []


def start(workflow: Workflow, hunt: bool, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return workflow.run("start", *(('hunt', 'one') if hunt else ('worker', '7')), **kwargs)


def tree(workflow: Workflow, hunt: bool) -> Path:
    return workflow.hunt_worktree("one") if hunt else workflow.worktree(7)


@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
@pytest.mark.parametrize("prefix", ["@install/", ""])
def test_separate_shipline_checkout_and_relative_config_folder(
    workflow: Workflow, hunt: bool, prefix: str
) -> None:
    """Installed launcher, auto-discovered external config, relative XDG folder."""
    install_shipline(workflow)
    workflow.add_pull_request(7)
    program = setup(workflow)
    original = workflow.repo / "shipline.json"
    given = json.loads(original.read_text())
    given.pop("install_folder")
    given["worktree_setup"] = prefix + program.name
    config_folder = workflow.repo / "example-config/shipline/example-org-example-app"
    config_folder.mkdir(parents=True)
    (config_folder / "shipline.json").write_text(json.dumps(given))
    (config_folder / program.name).symlink_to(program)
    original.unlink()

    done = start(workflow, hunt, env={"XDG_CONFIG_HOME": "example-config"})

    assert done.returncode == 0, done.stderr
    assert calls(workflow) == [f"{tree(workflow, hunt)}|{'--no-push' if hunt else ''}"]


@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
@pytest.mark.parametrize("code", [19, 124], ids=["failed", "dependency-timeout"])
def test_partial_setup_is_retryable(
    workflow: Workflow, hunt: bool, code: int
) -> None:
    """Retry repairs a kept worker tree or prepares the hunt's replacement tree."""
    workflow.add_pull_request(7)
    repair = workflow.state / "setup-can-finish"
    failure = 'timeout 0.1 /bin/sleep 10' if code == 124 else f'exit {code}'
    setup(workflow, 'mkdir -p .local\ntouch .local/partial-setup\n'
          f'if [[ ! -f "{repair}" ]]; then\n'
          f'  echo "example-app setup interrupted" >&2\n  {failure}\nfi\n'
          'touch .local/setup-complete\n')

    first = start(workflow, hunt)
    assert first.returncode == code, first.stderr
    assert not workflow.is_running("hunt-one" if hunt else "ticket-7")
    worktree = tree(workflow, hunt)
    marker = worktree / ".local/partial-setup"
    original_inode = marker.stat().st_ino
    repair.touch()
    second = start(workflow, hunt)

    assert second.returncode == 0, second.stderr
    if not hunt:
        assert marker.stat().st_ino == original_inode
    assert (worktree / ".local/setup-complete").exists()
    assert len(calls(workflow)) == 2


@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
def test_held_start_lock_refuses_without_running_setup(workflow: Workflow, hunt: bool) -> None:
    workflow.add_pull_request(7)
    setup(workflow)
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ("start-hunt.lock" if hunt else "start-7.lock")
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        done = start(workflow, hunt)
    assert done.returncode != 0
    assert "under way" in done.stderr
    assert calls(workflow) == []
    assert start(workflow, hunt).returncode == 0


@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
def test_overlapping_starts_do_not_run_setup_twice(workflow: Workflow, hunt: bool) -> None:
    workflow.add_pull_request(7)
    ready = workflow.state / "setup-entered"
    release = workflow.state / "setup-release"
    os.mkfifo(ready)
    os.mkfifo(release)
    setup(workflow, f"printf entered > '{ready}'\nread -r word < '{release}'\n")
    arguments = ["start", *(('hunt', 'one') if hunt else ('worker', '7'))]
    process = subprocess.Popen(
        ["bash", str(workflow.script), *arguments], cwd=workflow.repo,
        env=workflow.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    # A daemon reader makes waiting for setup observable without a fixed sleep.
    entered = threading.Event()

    def receive() -> None:
        with ready.open() as stream:
            assert stream.read() == "entered"
        entered.set()

    threading.Thread(target=receive, daemon=True).start()
    try:
        assert entered.wait(15), "first start did not reach setup"
        second = start(workflow, hunt)
        assert second.returncode != 0
        assert "under way" in second.stderr
        with release.open("w") as stream:
            stream.write("continue\n")
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert len(calls(workflow)) == 1
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)


@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
def test_missing_program_warns_once_per_retry_and_keeps_log_private(
    workflow: Workflow, hunt: bool
) -> None:
    workflow.add_pull_request(7)
    program = setup(workflow)
    program.unlink()
    message = "worktree setup @install/setup-worktree is not executable; setup skipped"
    for _ in range(2):
        done = start(workflow, hunt)
        assert done.returncode == 0, done.stderr
        assert (done.stdout + done.stderr).count(message) == 1
        stopped = workflow.run("stop", "hunt-one" if hunt else "ticket-7")
        assert stopped.returncode == 0, stopped.stderr
        if hunt:
            # Reuse the hunt name; its ended worktree is cleaned up by the next call.
            shutil.rmtree(workflow.hunt_report("one"))
    log = workflow.root / "home/.local/state/workflow/wake.log"
    assert log.read_text().count(message) == 2
    assert log.stat().st_mode & 0o077 == 0
    assert calls(workflow) == []


def test_main_setup_with_shell_metacharacters_is_only_a_filename(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    program = setup(workflow)
    relative = "tools/setup 'quoted' $(touch INJECTED); command"
    checkout = workflow.repo / relative
    checkout.parent.mkdir()
    checkout.symlink_to(program)
    configure(workflow, worktree_setup=relative)

    done = start(workflow, False)

    assert done.returncode == 0, done.stderr
    assert calls(workflow) == [f"{workflow.worktree(7)}|"]
    assert not (workflow.repo / "INJECTED").exists()
    assert not (workflow.worktree(7) / "INJECTED").exists()


def test_pr_worktree_cannot_replace_main_setup(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7)
    program = setup(workflow)
    checkout = workflow.repo / "tools/example-setup"
    checkout.parent.mkdir()
    checkout.symlink_to(program)
    configure(workflow, worktree_setup="tools/example-setup")
    replacement = worktree / "tools/example-setup"
    replacement.parent.mkdir()
    replacement.write_text("#!/bin/bash\ntouch PR_SETUP_EXECUTED\n")
    replacement.chmod(0o755)

    done = start(workflow, False)

    assert done.returncode == 0, done.stderr
    assert calls(workflow) == [f"{worktree}|"]
    assert not (worktree / "PR_SETUP_EXECUTED").exists()


def test_lost_github_pr_read_does_not_run_setup_then_retry_succeeds(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    setup(workflow)
    unavailable = workflow.state / "fail-gh-view-7"
    unavailable.touch()
    first = start(workflow, False)
    assert first.returncode != 0
    assert "cannot read pull request 7" in first.stderr
    assert not calls(workflow)
    unavailable.unlink()
    second = start(workflow, False)
    assert second.returncode == 0, second.stderr
    assert calls(workflow) == [f"{workflow.worktree(7)}|"]


def test_missing_installation_setting_is_a_clear_config_refusal(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    configure(workflow, worktree_setup="@install/setup-worktree")

    done = start(workflow, False)

    assert done.returncode != 0
    assert "install_folder" in done.stderr
    assert not workflow.is_running("ticket-7")
    assert calls(workflow) == []


@pytest.mark.parametrize("role", ["worker", "audit", "review"])
def test_kept_worktree_setup_precedes_each_local_role(workflow: Workflow, role: str) -> None:
    worktree = workflow.add_worktree(7)
    setup(workflow, 'mkdir -p .local\ntouch .local/setup-complete\n')
    # Kept local notes are outside the tracked tree, including for reviewers.
    notes = worktree / ".local/example-notes"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("keep this work\n")

    done = workflow.run("start", role, "7")

    assert done.returncode == 0, done.stderr
    assert calls(workflow) == [f"{worktree}|"]
    assert (worktree / ".local/setup-complete").exists()
    assert notes.read_text() == "keep this work\n"


@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
def test_implicit_default_can_come_from_installation_or_remain_absent(
    workflow: Workflow, hunt: bool
) -> None:
    workflow.add_pull_request(7)
    program = setup(workflow, name="scripts/setup-worktree")
    path = workflow.repo / "shipline.json"
    given = json.loads(path.read_text())
    given.pop("worktree_setup")
    path.write_text(json.dumps(given))
    (workflow.repo / "scripts/setup-worktree").unlink()

    first = start(workflow, hunt)

    assert first.returncode == 0, first.stderr
    assert calls(workflow) == [f"{tree(workflow, hunt)}|{'--no-push' if hunt else ''}"]
    workflow.run("stop", "hunt-one" if hunt else "ticket-7")
    if hunt:
        shutil.rmtree(workflow.hunt_report("one"))
    program.unlink()
    second = start(workflow, hunt)
    assert second.returncode == 0, second.stderr
    assert "setup skipped" not in second.stdout + second.stderr
    assert not (workflow.root / "home/.local/state/workflow/wake.log").exists()
    assert len(calls(workflow)) == 1


@pytest.mark.parametrize("checks", [
    [],
    [{"name": "CI", "status": "QUEUED", "conclusion": None}],
    [{"name": "CI: server tests (1 of 2)", "status": "COMPLETED", "conclusion": "SUCCESS"},
     {"name": "CI: server tests (2 of 2)", "status": "IN_PROGRESS", "conclusion": None}],
], ids=["lost", "late", "partial-rerun"])
def test_check_rollup_does_not_skip_local_setup(workflow: Workflow, checks: list[dict]) -> None:
    workflow.add_pull_request(7)
    setup(workflow)
    (workflow.state / "rollup-7.json").write_text(json.dumps(checks))

    done = start(workflow, False)

    assert done.returncode == 0, done.stderr
    assert calls(workflow) == [f"{workflow.worktree(7)}|"]


def test_configured_installed_program_without_execute_permission_warns(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    program = setup(workflow)
    program.chmod(0o644)

    done = start(workflow, False)

    assert done.returncode == 0, done.stderr
    message = "worktree setup @install/setup-worktree is not executable; setup skipped"
    assert (done.stdout + done.stderr).count(message) == 1
    log = workflow.root / "home/.local/state/workflow/wake.log"
    assert log.read_text().count(message) == 1
    assert not calls(workflow)
