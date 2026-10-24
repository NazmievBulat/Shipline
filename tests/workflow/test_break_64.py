"""Breaker tests for #64: a session selects the same config as the tick that started it.

The installation keeps no `shipline.json` in its repository: its config is the home file
`~/.config/shipline/<owner>-<name>/shipline.json`, named by no variable, with a known-differences
file beside it. The repository is named by `.github/runner/settings.sh`. Its commands are
programs of the repository given by relative path, so the checkout lookup must stay first.
Sessions run in worktrees under `.claude/worktrees/ticket-<n>`; the tick may start in either."""

import json
import subprocess
from pathlib import Path

import pytest
from workflow_harness import Workflow, install_shipline

_STAND_IN = Path(__file__).resolve().parent / "stand-ins/shipline.json"
_PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]); import workflow_config as w; c = w.current(); "
    "print(c.path); print(c.command('browser_plan')[0]); print(c.file(c.parity.known)); "
    "print(c.state_folder)"
)


def _session_environment(workflow: Workflow, name: str) -> dict[str, str]:
    lines = (workflow.state / "env" / name).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def _home_installation(workflow: Workflow) -> tuple[Path, Path]:
    """No checkout config; the home file names a repository program and a beside-file."""
    plan = workflow.repo / "scripts/e2e-plan.py"
    plan.write_text("print('main')\n")
    workflow.git("rm", "--quiet", "shipline.json")
    workflow.git("add", "scripts/e2e-plan.py")
    workflow.git("commit", "--quiet", "-m", "chore: no checkout config")
    workflow.git("push", "--quiet", "origin", "main")
    folder = Path(workflow.env["HOME"]) / ".config/shipline/example-org-example-app"
    folder.mkdir(parents=True)
    config = json.loads(_STAND_IN.read_text())
    config["commands"]["browser_plan"] = "scripts/e2e-plan.py"
    config["state_folder"] = ".local/home-state"
    config["parity"] = {"known": "parity.known"}
    path = folder / "shipline.json"
    path.write_text(json.dumps(config))
    (folder / "parity.known").write_text("")
    # A program of the same name beside the config: the checkout's must still win.
    (folder / "scripts").mkdir()
    (folder / "scripts/e2e-plan.py").write_text("print('installation')\n")
    return path, folder


def _probe(workflow: Workflow, cwd: Path, env: dict[str, str]) -> list[str]:
    done = subprocess.run(
        ["python3", "-c", _PROBE, str(workflow.script.parent)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def _expected(workflow: Workflow, path: Path, folder: Path) -> list[str]:
    return [
        str(path), str(workflow.repo / "scripts/e2e-plan.py"), str(folder / "parity.known"),
        ".local/home-state",
    ]


@pytest.mark.parametrize("started_in", ["main", "worktree"])
def test_a_session_of_the_home_file_installation_reads_the_home_file(
    workflow: Workflow, started_in: str,
) -> None:
    install_shipline(workflow)
    path, folder = _home_installation(workflow)
    cwd = workflow.repo if started_in == "main" else workflow.add_worktree(6)
    workflow.add_pull_request(7)

    done = workflow.run_script("session", "start", "worker", "7", cwd=cwd)

    assert done.returncode == 0, done.stderr
    worktree = workflow.worktree(7)
    assert (worktree / ".local/home-state").is_dir()
    env = _session_environment(workflow, "ticket-7")
    assert "SHIPLINE_CONFIG" not in env
    assert "XDG_CONFIG_HOME" not in env
    assert _probe(workflow, worktree, env) == _expected(workflow, path, folder)
    read = subprocess.run(
        ["bash", str(workflow.home / "bin/shipline"), "pre-train-checks"],
        cwd=worktree, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert read.returncode == 0, read.stderr


@pytest.mark.parametrize("spelling", ["absolute", "tilde"])
def test_a_session_keeps_the_home_file_the_orchestrator_names(
    workflow: Workflow, spelling: str,
) -> None:
    """Named explicitly, even as a unit file writes it (`~` unexpanded), and with a ticket's
    branch that carries a `shipline.json` of its own."""
    install_shipline(workflow)
    path, folder = _home_installation(workflow)
    workflow.add_pull_request(7)
    named = str(path) if spelling == "absolute" else "~/" + str(
        path.relative_to(Path(workflow.env["HOME"])))

    done = workflow.run("start", "worker", "7", env={"SHIPLINE_CONFIG": named})

    assert done.returncode == 0, done.stderr
    worktree = workflow.worktree(7)
    (worktree / "shipline.json").write_text(json.dumps({"state_folder": ".local/branch-state"}))
    env = _session_environment(workflow, "ticket-7")
    assert env["SHIPLINE_CONFIG"] == str(path)
    assert _probe(workflow, worktree, env) == _expected(workflow, path, folder)


@pytest.mark.parametrize("started_in", ["main", "worktree"])
def test_a_tick_of_the_home_file_installation_gives_its_session_the_home_file(
    workflow: Workflow, started_in: str,
) -> None:
    path, folder = _home_installation(workflow)
    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.add_pull_request(8)

    done = workflow.run_script(
        "tick", cwd=workflow.repo if started_in == "main" else worktree)

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-8")
    session_worktree = workflow.worktree(8)
    assert (session_worktree / ".local/home-state").is_dir()
    env = _session_environment(workflow, "ticket-8")
    assert "SHIPLINE_CONFIG" not in env
    assert "XDG_CONFIG_HOME" not in env
    assert _probe(workflow, session_worktree, env) == _expected(workflow, path, folder)


def test_two_starts_at_once_both_keep_the_named_file(workflow: Workflow) -> None:
    install_shipline(workflow)
    path, folder = _home_installation(workflow)
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    env = {**workflow.env, "SHIPLINE_CONFIG": str(path)}
    started = [
        subprocess.Popen(
            ["bash", str(workflow.script), "start", "worker", str(number)],
            cwd=workflow.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for number in (7, 8)
    ]
    for process in started:
        _, error = process.communicate(timeout=120)
        assert process.returncode == 0, error
    for number in (7, 8):
        session = _session_environment(workflow, f"ticket-{number}")
        assert session["SHIPLINE_CONFIG"] == str(path)
        assert _probe(workflow, workflow.worktree(number), session) == _expected(
            workflow, path, folder)


def test_a_literal_tilde_config_home_still_reaches_the_home_file(workflow: Workflow) -> None:
    """Low, not reached in use: the installation sets no XDG_CONFIG_HOME. The loader expands a
    leading `~`, and did before this change; the tick's new anchoring makes it a folder under
    the current one."""
    path, folder = _home_installation(workflow)
    worktree = workflow.add_worktree(7)
    workflow.running("ticket-7")
    workflow.add_pull_request(8)

    done = workflow.run_script("tick", env={"XDG_CONFIG_HOME": "~/.config"})

    assert done.returncode == 0, done.stderr
    assert (workflow.worktree(8) / ".local/home-state").is_dir()
    assert worktree.is_dir()
