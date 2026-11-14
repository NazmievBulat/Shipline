"""Small process helpers for the independent pre-train receipt breaker tests."""

import json
import subprocess
import textwrap
from pathlib import Path

from workflow_harness import Workflow


def configure(workflow: Workflow, command: str | dict | None = "example-checks --fast") -> None:
    path = workflow.repo / "shipline.json"
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    data = json.loads(path.read_text())
    commands = data.setdefault("commands", {})
    if command is None:
        commands.pop("pre_train_checks", None)
    else:
        commands["pre_train_checks"] = command
    path.write_text(json.dumps(data))


def checks(workflow: Workflow, body: str = "exit 0\n") -> None:
    workflow.fake("example-checks", "#!/bin/bash\n"
                  "echo example-checks >> '@STATE@/calls.log'\n" + body)


def folder(workflow: Workflow) -> Path:
    return workflow.repo / ".local/workflow"


def receipt(workflow: Workflow) -> Path:
    return folder(workflow) / "pre-train-pass-7.json"


def arguments(workflow: Workflow, before: str = "") -> list[str]:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g\n"
        "folder = g.CONFIG.state()\nfolder.mkdir(parents=True, exist_ok=True)\n"
        + textwrap.dedent(before)
        + '\ntry:\n    result = g.pre_train_checks("7", folder, locked=False)\n'
        'except g.Refused as error:\n    print("outcome:refused", error, flush=True)\n'
        'else:\n    print("outcome:wait" if result else "outcome:passed", flush=True)\n'
    )
    return ["python3", "-B", "-c", code, str(workflow.script.parent)]


def start(workflow: Workflow, worktree: Path, before: str = "") -> subprocess.Popen[str]:
    return subprocess.Popen(
        arguments(workflow, before), cwd=worktree,
        env={**workflow.env, "SHIPLINE_REPO": str(workflow.repo)},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def call(workflow: Workflow, worktree: Path, before: str = "") -> str:
    process = start(workflow, worktree, before)
    try:
        out, err = process.communicate(timeout=15)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    assert process.returncode == 0, out + err
    return out + err
