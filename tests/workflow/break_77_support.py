"""Independent capacity-stop fixtures for the PR 77 breaker tests."""

from pathlib import Path

from workflow_harness import Workflow

EPOCH = 1_792_080_000
ERROR = "■ Selected model is at capacity. Please try a different model."
CONTINUE = "The model was at capacity. Continue where you stopped."
STOPPED = ERROR + "\n\n› \n  ? for shortcuts  90% context left\n"


def stopped_session(workflow: Workflow, kind: str = "ticket") -> tuple[str, str, Path]:
    if kind == "hunt":
        worktree = workflow.add_hunt("example-app")
        (workflow.hunt_report("example-app") / "codex.log").touch()
        name = target = "hunt-example-app"
        notes = worktree / ".local/workflow"
    else:
        workflow.add_worktree(7, draft=kind == "ticket")
        name, target = f"{kind}-7", "codex-7"
        notes = workflow.notes(7)
        notes.mkdir(parents=True, exist_ok=True)
        (notes / f"{name}.codex").touch()
        workflow.running(target)
    workflow.running(name)
    workflow.screen(STOPPED, target)
    return name, target, notes


def tick(workflow: Workflow, offset: int, *arguments: str):
    return workflow.run_script(
        "tick", *arguments, env={"WORKFLOW_NOW": str(EPOCH + offset)}
    )


def enters(workflow: Workflow, target: str) -> int:
    return sum(call.endswith(" Enter") for call in workflow.calls(f"tmux {target} send-keys"))
