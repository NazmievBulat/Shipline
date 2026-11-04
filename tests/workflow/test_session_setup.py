"""A worktree's setup uses the same checkout/installation lookup as other programs."""

import json
from pathlib import Path

import pytest
from workflow_harness import Workflow


def _configure(workflow: Workflow, **settings: str) -> None:
    path = workflow.repo / "shipline.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **settings}))


def _program(workflow: Workflow, path: Path, name: str) -> None:
    workflow.fake(name, '#!/bin/bash\necho "@TOOL@ $* in $PWD" >> "@STATE@/calls.log"\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(workflow.bin / name)


@pytest.mark.parametrize("prefix", ["@install/", ""])
@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
@pytest.mark.parametrize("external", [False, True], ids=["checkout-config", "installation-config"])
def test_an_installed_setup_runs_in_the_new_worktree(
    workflow: Workflow, prefix: str, hunt: bool, external: bool
) -> None:
    workflow.add_pull_request(7)
    installation = workflow.bin / "installation with spaces"
    _program(workflow, installation / "setup-worktree", "installed-setup")
    # @install/ must ignore even an existing main-checkout program of the same name.
    if prefix:
        _program(workflow, workflow.repo / "setup-worktree", "checkout-setup")
    _configure(workflow, install_folder=str(installation), worktree_setup=f"{prefix}setup-worktree")
    env = {}
    if external:
        path = installation / "shipline.json"
        config = json.loads((workflow.repo / "shipline.json").read_text())
        config.pop("install_folder")
        path.write_text(json.dumps(config))
        env["SHIPLINE_CONFIG"] = str(path)

    arguments = ("hunt", "one") if hunt else ("worker", "7")
    done = workflow.run("start", *arguments, env=env)

    assert done.returncode == 0, done.stderr
    worktree = workflow.hunt_worktree("one") if hunt else workflow.worktree(7)
    arguments = "--no-push" if hunt else ""
    assert workflow.calls("installed-setup") == [f"installed-setup {arguments} in {worktree}"]
    assert workflow.calls("checkout-setup") == []


def test_a_main_checkout_setup_takes_precedence_and_runs_in_the_new_worktree(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    name = "tools/set up 'worktree'"
    installation = workflow.bin / "installation"
    _program(workflow, installation / name, "installed-setup")
    # Created after the branch: it is available only in the main checkout.
    _program(workflow, workflow.repo / name, "checkout-setup")
    _configure(workflow, install_folder=str(installation), worktree_setup=name)

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert workflow.calls("checkout-setup") == [f"checkout-setup  in {workflow.worktree(7)}"]
    assert workflow.calls("installed-setup") == []


@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
@pytest.mark.parametrize("name", ["tools/missing-setup", "scripts/setup-worktree"])
def test_a_missing_explicit_setup_is_reported_once_in_output_and_the_wake_log(
    workflow: Workflow, hunt: bool, name: str
) -> None:
    workflow.add_pull_request(7)
    if name == "scripts/setup-worktree":
        (workflow.repo / name).unlink()
    _configure(workflow, worktree_setup=name)

    done = workflow.run("start", "hunt", "one") if hunt else workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    message = f"worktree setup {name} is not executable; setup skipped"
    assert (done.stdout + done.stderr).count(message) == 1
    log = workflow.root / "home/.local/state/workflow/wake.log"
    assert log.read_text().count(message) == 1
    assert workflow.calls("worktree-setup") == []


def test_an_absent_unconfigured_default_stays_quiet(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    (workflow.repo / "scripts/setup-worktree").unlink()

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    assert "setup skipped" not in done.stdout + done.stderr
    assert not (workflow.root / "home/.local/state/workflow/wake.log").exists()
    assert workflow.calls("worktree-setup") == []


def test_a_configured_setup_without_execute_permission_is_reported(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    program = workflow.repo / "tools/setup-worktree"
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text("#!/bin/bash\nexit 0\n")
    program.chmod(0o644)
    _configure(workflow, worktree_setup="tools/setup-worktree")

    done = workflow.run("start", "worker", "7")

    assert done.returncode == 0, done.stderr
    message = "worktree setup tools/setup-worktree is not executable; setup skipped"
    assert (done.stdout + done.stderr).count(message) == 1
    log = workflow.root / "home/.local/state/workflow/wake.log"
    assert log.read_text().count(message) == 1


@pytest.mark.parametrize("hunt", [False, True], ids=["worker", "hunt"])
def test_a_failed_installed_setup_stops_the_start(workflow: Workflow, hunt: bool) -> None:
    workflow.add_pull_request(7)
    installation = workflow.bin / "installation"
    _program(workflow, installation / "setup-worktree", "installed-setup")
    workflow.fake("installed-setup", "#!/bin/bash\necho 'setup failed' >&2\nexit 19\n")
    _configure(workflow, install_folder=str(installation), worktree_setup="@install/setup-worktree")

    done = workflow.run("start", "hunt", "one") if hunt else workflow.run("start", "worker", "7")

    assert done.returncode == 19, done.stderr
    assert "setup failed" in done.stderr
    assert not workflow.is_running("hunt-one" if hunt else "ticket-7")
