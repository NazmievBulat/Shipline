"""A ticket's priority is read the same way by the merge gate (`priority.py`) and by the tick
and the wake (`priority.jq`)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_WORKFLOW = Path(__file__).resolve().parents[2] / "scripts/workflow"


def _load(name: str) -> Any:
    # Loaded from its file, so the scripts' folder is not put on the path of every other test.
    spec = importlib.util.spec_from_file_location(name, _WORKFLOW / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


# What priority.py reads its label names with.
_load("workflow_config")
priority = _load("priority")


def _jq(labels: list[str]) -> str:
    done = subprocess.run(
        ["jq", "-r", "-L", str(_WORKFLOW), 'include "priority"; priority_name'],
        input=json.dumps({"labels": [{"name": name} for name in labels]}),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.mark.parametrize(
    "labels,expected",
    [
        ([], "medium"),
        (["workflow"], "medium"),
        (["workflow", "priority: high"], "high"),
        (["priority: blocker"], "blocker"),
        (["priority: low"], "low"),
        (["priority: medium"], "medium"),
        # Two priority labels at once: the more urgent one counts.
        (["priority: low", "priority: blocker"], "blocker"),
        (["priority: medium", "priority: high"], "high"),
        # A label that only looks like one is no priority.
        (["priority: urgent", "priority:high", "Priority: high"], "medium"),
    ],
)
def test_the_gate_and_the_shell_scripts_read_the_same_priority(
    labels: list[str], expected: str
) -> None:
    assert priority.of([{"name": name} for name in labels]) == expected
    assert _jq(labels) == expected


def test_a_pull_request_without_labels_is_medium() -> None:
    assert priority.of(None) == "medium"
    assert priority.of([{"id": "no name"}]) == "medium"


def test_priorities_rank_most_urgent_first_and_an_unknown_one_as_medium() -> None:
    ranks = [priority.rank(name) for name in ("blocker", "high", "medium", "low")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4
    assert priority.rank("urgent") == priority.rank("medium")
