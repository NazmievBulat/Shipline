"""Neutral endpoint fixtures shared only by the independent PR 80 break tests."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from workflow_harness import Workflow

NOW = 1793833200
ENV = {"WORKFLOW_NOW": str(NOW), "TZ": "UTC"}


def dated(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def unused() -> dict[str, object]:
    return {
        "five_hour": {"utilization": 0.0, "resets_at": None},
        "seven_day": {"utilization": 32.0, "resets_at": dated(NOW + 172800)},
    }


def answer(workflow: Workflow, payload: object, source: str = "cache") -> Path:
    cache = workflow.state / "usage-cache.json"
    workflow.env.update(ENV)
    workflow.env["WORKFLOW_USAGE_CACHE"] = str(cache)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    if source == "cache":
        cache.write_text(text)
        os.utime(cache, (NOW, NOW))
    else:
        cache.unlink(missing_ok=True)
        credentials = workflow.root / "home/.claude/.credentials.json"
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "example-test-credential"},
        }))
        credentials.chmod(0o600)
        (workflow.state / "usage-answer").write_text(text)
        workflow.fake("curl", '''#!/bin/bash
printf '%s\\n' "$@" > '@STATE@/usage-arguments'
cat > '@STATE@/usage-input'
cat '@STATE@/usage-answer'
''')
    return cache


def notes(workflow: Workflow) -> Path:
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def candidate(workflow: Workflow, number: int = 7) -> None:
    workflow.add_pull_request(number, labels=())
    (workflow.state / f"body-{number}").write_text(
        "Maintain example-app.\n\n## Files\n\n- src/example_app/app.py\n"
    )
    (notes(workflow) / "room").write_text(f"{NOW - 900} {NOW - 180}\n")


def labelled(workflow: Workflow) -> list[str]:
    path = workflow.state / "labelled"
    return path.read_text().split() if path.exists() else []


def tick(workflow: Workflow, moment: int = NOW, **extra: str):
    return workflow.run_script("tick", env={"WORKFLOW_NOW": str(moment), **extra})
