"""Tests that try to break the repository's configured commands (pull request 8) the way an
installation uses them every day, and the way they fail."""

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from workflow.test_commands import (  # pyright: ignore[reportPrivateUsage]
    _browser_needed,
    _changes,
    _configure,
    _gate,
    _pre_train_checks,
    _stand_in,
    workflow_config,
)
from workflow_harness import Workflow, install_shipline


def _tool(workflow: Workflow, path: str, text: str) -> None:
    """A script of the repository's own in its main checkout, committed, so a later change's
    diff does not name it."""
    file = workflow.repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    workflow.git("add", path)
    workflow.git("commit", "--quiet", "-m", "chore: a repository tool")


# A browser plan written in Python, as the planner the gate imported before was, that fails.
_CRASHES = {
    "a module it imports is gone": "import example_missing_module\n",
    "an unexpected path breaks it": (
        "import sys\n"
        "paths = sys.stdin.buffer.read().split(b'\\0')[:-1]\n"
        "raise KeyError(paths[0])\n"
    ),
    "it stops with a message": "import sys\nsys.exit('cannot read the plan of screens')\n",
}


@pytest.mark.parametrize("crash", list(_CRASHES))
def test_a_browser_plan_that_crashes_never_skips_the_browser_tests(
    workflow: Workflow, crash: str
) -> None:
    """Before, the gate imported the planner, and a planner that raised stopped the gate. A
    crashed plan must not read as 'no browser tests needed', or a change to the application
    merges without the whole browser suite."""
    _tool(workflow, "tools/shipline/browser-plan.py", _CRASHES[crash])
    _configure(workflow, browser_plan="python3 -B tools/shipline/browser-plan.py")
    _changes(workflow, "app.txt")

    assert _browser_needed(workflow) == "needed"


def test_pre_train_checks_written_in_python_that_crash_refuse(workflow: Workflow) -> None:
    _tool(workflow, "tools/shipline/pre-train-checks.py", "import example_missing_module\n")
    _configure(workflow, pre_train_checks="python3 -B tools/shipline/pre-train-checks.py")

    done = _pre_train_checks(workflow, workflow.add_worktree(7))

    assert "refused: pre-train checks failed" in done.stdout, done.stderr


def test_pre_train_checks_that_hang_are_stopped_and_refuse(workflow: Workflow) -> None:
    """A check that hangs must not hold the merge lock past the gate's bound, and what it
    started must go with it."""
    _tool(
        workflow,
        "tools/shipline/pre-train-checks.sh",
        "/bin/sleep 300 &\necho $! > \"$1\"\nwait\n",
    )
    child = workflow.state / "hung-child"
    _configure(
        workflow, pre_train_checks=f"bash tools/shipline/pre-train-checks.sh {child}"
    )
    started = time.monotonic()

    done = _gate(
        workflow,
        """
        g.COMMAND_TIMEOUT = 2
        folder = g.main_checkout() / g.CONFIG.state_folder
        folder.mkdir(parents=True, exist_ok=True)
        try:
            g.pre_train_checks("7", folder)
        except g.Refused as error:
            print("refused:", error)
        else:
            print("passed")
        """,
    )

    assert time.monotonic() - started < 30
    assert done.stdout.startswith("refused:"), (done.stdout, done.stderr)
    pid = child.read_text().strip()
    time.sleep(0.5)
    # Gone, or only a zombie no one has reaped yet.
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"


def _selector(workflow: Workflow, path: str, kind: str) -> None:
    """A selection in the main checkout that writes down its arguments and folder, and prints
    JSON as a selection for CI does."""
    _tool(
        workflow,
        path,
        "import json, os, sys\n"
        f"print(json.dumps({{'kind': {kind!r}, 'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'from': os.path.dirname(os.path.abspath(__file__))}))\n",
    )


def test_a_session_runs_both_selections_with_today_s_arguments(workflow: Workflow) -> None:
    """From a session's worktree: the browser selection with a base (capped locally) and with
    `--ci <base>`, the server selection with `--plan <base>` and with a base alone. The program
    is the main checkout's; the folder is the worktree; the output passes through unchanged."""
    install_shipline(workflow)
    _selector(workflow, "tools/shipline/browser_select.py", "browser")
    _selector(workflow, "tools/shipline/server_select.py", "server")
    _configure(
        workflow,
        browser_tests_for_changes="python3 -B tools/shipline/browser_select.py",
        server_tests_for_changes="python3 -B tools/shipline/server_select.py",
    )
    worktree = workflow.add_worktree(7)
    # The pull request's own copy of a selection never runs.
    (worktree / "tools/shipline").mkdir(parents=True, exist_ok=True)
    (worktree / "tools/shipline/browser_select.py").write_text("print('the branch copy')\n")

    for name, args in (
        ("browser-tests-for-changes", ["origin/main"]),
        ("browser-tests-for-changes", ["--ci", "origin/main"]),
        ("server-tests-for-changes", ["--plan", "origin/main"]),
        ("server-tests-for-changes", ["origin/main"]),
    ):
        done = workflow.shipline(
            name, *args, cwd=worktree, env={"SHIPLINE_REPO": str(workflow.repo)}
        )
        assert done.returncode == 0, done.stderr
        said = json.loads(done.stdout)
        assert said["args"] == args
        assert Path(said["cwd"]).resolve() == worktree.resolve()
        assert Path(said["from"]).resolve() == (workflow.repo / "tools/shipline").resolve()


def test_many_sessions_select_at_once(workflow: Workflow) -> None:
    """Five to fifteen sessions run the selection locally at the same moment."""
    install_shipline(workflow)
    _selector(workflow, "tools/shipline/browser_select.py", "browser")
    _configure(workflow, browser_tests_for_changes="python3 -B tools/shipline/browser_select.py")
    worktrees = [workflow.add_worktree(number) for number in range(1, 16)]

    def select(worktree: Path) -> tuple[Path, subprocess.CompletedProcess[str]]:
        return worktree, workflow.shipline(
            "browser-tests-for-changes",
            f"origin/{worktree.name}",
            cwd=worktree,
            env={"SHIPLINE_REPO": str(workflow.repo)},
        )

    with ThreadPoolExecutor(15) as pool:
        for worktree, done in pool.map(select, worktrees):
            assert done.returncode == 0, done.stderr
            said = json.loads(done.stdout)
            assert said["args"] == [f"origin/{worktree.name}"]
            assert Path(said["cwd"]).resolve() == worktree.resolve()


def test_a_selection_that_fails_through_shipline_is_a_failure(workflow: Workflow) -> None:
    """A configured selection that fails must not read as 'nothing selected'."""
    install_shipline(workflow)
    _tool(workflow, "tools/shipline/browser_select.py", "import example_missing_module\n")
    _configure(workflow, browser_tests_for_changes="python3 -B tools/shipline/browser_select.py")

    done = workflow.shipline("browser-tests-for-changes", "origin/main")

    assert done.returncode != 0


def test_a_conflict_command_that_crashes_while_renumbering_refuses(workflow: Workflow) -> None:
    """Two calls at once aside, a crash in the conflict rules leaves the merge to a person."""
    _tool(workflow, "tools/shipline/conflicts.py", "import example_missing_module\n")
    _configure(workflow, mechanical_conflicts="python3 -B tools/shipline/conflicts.py")

    done = _gate(
        workflow,
        """
        try:
            print(g._taken_migrations("HEAD", "HEAD"))
        except g.Refused as error:
            print("refused:", error)
        """,
    )

    assert done.stdout.startswith("refused: could not compare numbered files"), done.stderr


def test_an_advisory_watch_that_crashes_reads_no_outage(workflow: Workflow) -> None:
    """A failed push the watch cannot read is refused as it is, never waits as an outage."""
    _tool(workflow, "tools/shipline/advisory.py", "import example_missing_module\n")
    _configure(workflow, advisory_watch="python3 -B tools/shipline/advisory.py")

    done = _gate(
        workflow,
        """
        print(g.unreachable_service("npm ERR! audit endpoint returned an error"))
        print(g.service_answers("npm"))
        """,
    )

    assert done.stdout.split() == ["None", "False"], done.stderr


def test_an_interpreter_option_with_a_folder_never_lets_the_worktree_supply_the_script(
    tmp_path: Path,
) -> None:
    """`node --import ./register.mjs tools/shipline/hunt-browser.mjs`: the script Node runs is
    the last word. Taking the option's value for it leaves the script to the folder the command
    runs in, a pull request's worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "--initial-branch=main"],
        ["-c", "user.name=Tester", "-c", "user.email=tester@example.com", "commit",
         "--quiet", "--allow-empty", "-m", "chore: start"],
    ):  # fmt: skip
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    (repo / "shipline.json").write_text(
        json.dumps(
            {
                "commands": {
                    "hunt_browser": "node --import ./register.mjs tools/shipline/hunt-browser.mjs"
                }
            }
        )
    )

    words = workflow_config.load(repo).command("hunt_browser")

    assert words is not None
    assert words[-1] == str(repo / "tools/shipline/hunt-browser.mjs")
