"""A repository plugs its own checks into Shipline by commands named in `shipline.json`.

Each command is a stand-in on the test's `PATH` that writes down its arguments, its working
folder and what it read, and answers as the test says. Each is tried named and left out.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from workflow_harness import Workflow

_WORKFLOW = Path(__file__).resolve().parents[2] / "scripts/workflow"
# Loaded from its file, so the scripts' folder is not put on the path of every other test.
_SPEC = importlib.util.spec_from_file_location("workflow_config", _WORKFLOW / "workflow_config.py")
assert _SPEC and _SPEC.loader
workflow_config = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workflow_config)

_COMMANDS = (
    "browser_tests_for_changes",
    "server_tests_for_changes",
    "pre_train_checks",
    "mechanical_conflicts",
    "browser_plan",
    "hunt_app",
    "hunt_browser",
    "hunt_cursor",
    "advisory_watch",
    "push_checkout",
)


def _configure(workflow: Workflow, **commands: str | None) -> None:
    """Names `commands` in the main checkout's `shipline.json`; None leaves one out."""
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    named = config.setdefault("commands", {})
    for name, line in commands.items():
        if line is None:
            named.pop(name, None)
        else:
            named[name] = line
    path.write_text(json.dumps(config))


def _stand_in(workflow: Workflow, program: str, answer: str = "", status: int = 0) -> None:
    """A command on the `PATH` that writes down how it was called, prints `answer` and exits
    with `status`. What it read on standard input is kept in `<state>/<program>.input`."""
    workflow.fake(
        program,
        "#!/bin/bash\n"
        f'printf "%s\\n" "{program} $* (in $PWD)" >> "@STATE@/calls.log"\n'
        f'cat > "@STATE@/{program}.input"\n'
        f"printf %s {json.dumps(answer)}\n"
        f"exit {status}\n",
    )


def _read(workflow: Workflow, program: str) -> str:
    return (workflow.state / f"{program}.input").read_text()


# The file


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "--initial-branch=main"],
        ["remote", "add", "origin", "https://github.com/example-owner/example-app.git"],
        ["-c", "user.name=Tester", "-c", "user.email=tester@example.com", "commit",
         "--quiet", "--allow-empty", "-m", "chore: start"],
    ):  # fmt: skip
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    return repo


def test_without_commands_none_is_named(tmp_path: Path) -> None:
    config = workflow_config.load(_repository(tmp_path))

    assert [config.command(name) for name in _COMMANDS] == [None] * len(_COMMANDS)
    assert workflow_config.as_dict(config)["commands"] == dict.fromkeys(_COMMANDS)


def test_a_command_s_program_is_found_in_the_main_checkout_from_a_worktree(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(
        json.dumps(
            {
                "commands": {
                    "pre_train_checks": "tools/shipline/pre-train-checks --fast",
                    "browser_plan": "python3 -B 'tools/shipline/browser plan.py'",
                }
            }
        )
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(worktree)],
        cwd=repo,
        capture_output=True,
        check=True,
    )

    config = workflow_config.load(worktree)

    assert config.command("pre_train_checks") == [
        str(repo / "tools/shipline/pre-train-checks"),
        "--fast",
    ]
    # A program named without a folder is found on the PATH; the words after it stay as written.
    assert config.command("browser_plan") == ["python3", "-B", "tools/shipline/browser plan.py"]
    assert config.command("hunt_app") is None
    assert workflow_config.as_dict(config)["commands"]["pre_train_checks"] == (
        "tools/shipline/pre-train-checks --fast"
    )


@pytest.mark.parametrize(
    ("line", "refusal"),
    [
        ("/opt/checks", "must be a path inside the checkout"),
        ("../checks", "must be a path inside the checkout"),
        ("tools/'checks", "cannot be split into words"),
        ("   ", "must be a name"),
        (["tools/checks"], "must be a name"),
    ],
)
def test_a_command_that_cannot_be_run_from_the_checkout_is_refused(
    tmp_path: Path, line: object, refusal: str
) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(json.dumps({"commands": {"pre_train_checks": line}}))

    with pytest.raises(workflow_config.ConfigError, match=f"commands.pre_train_checks {refusal}"):
        workflow_config.load(repo)


def test_an_unknown_command_is_refused(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(json.dumps({"commands": {"pre_train_check": "x"}}))

    with pytest.raises(workflow_config.ConfigError, match="unknown key commands.pre_train_check"):
        workflow_config.load(repo)


def test_bash_reads_each_command_as_its_words(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "shipline.json").write_text(
        json.dumps({"commands": {"hunt_app": "tools/hunt app --port '8 0' $(touch nope)"}})
    )

    done = subprocess.run(
        [
            "bash",
            "-c",
            'eval "$(python3 "$1" shell "$2")" || exit 1; '
            'printf "%s\\n" "${#SHIPLINE_COMMAND_HUNT_APP[@]}" "${SHIPLINE_COMMAND_HUNT_APP[@]}" '
            '"${#SHIPLINE_COMMAND_PRE_TRAIN_CHECKS[@]}"',
            "bash",
            str(_WORKFLOW / "workflow_config.py"),
            str(repo),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert done.stdout.splitlines() == [
        "6",
        str(repo / "tools/hunt"),
        "app",
        "--port",
        "8 0",
        "$(touch",
        "nope)",
        "0",
    ]
    assert not (repo / "nope").exists()
