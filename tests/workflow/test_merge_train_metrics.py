"""Measure the same GitHub time window before and after rollout."""

import json

import pytest
from workflow_harness import Workflow


@pytest.mark.parametrize("merges", [0, 2])
def test_metrics_count_only_runs_and_merges_inside_the_window(
    workflow: Workflow,
    merges: int,
) -> None:
    workflow.fake(
        "gh",
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path
state = Path("@STATE@")
if sys.argv[1:3] == ["pr", "list"]:
    print((state / "merges.json").read_text())
else:
    print(json.dumps({"workflow_runs": [
        {"id": 1, "created_at": "2026-10-17T01:00:00Z"},
        {"id": 2, "created_at": "2026-10-17T02:00:00Z"},
        {"id": 3, "created_at": "2026-10-17T03:00:00Z"}
    ]}))
""",
    )
    (workflow.state / "merges.json").write_text(
        json.dumps(
            [{"number": number, "mergedAt": "2026-10-17T02:30:00Z"} for number in range(merges)]
            + [{"number": 99, "mergedAt": "2026-10-17T03:00:00Z"}]
        )
    )
    done = workflow.run_script(
        "merge-train-metrics", "2026-10-17T01:00:00Z", "2026-10-17T03:00:00Z"
    )
    assert done.returncode == 0, done.stderr
    measured = json.loads(done.stdout)
    assert measured["ci_runs"] == 2
    assert measured["merges"] == merges
    assert measured["merges_per_hour"] == merges / 2
    assert measured["ci_runs_per_merge"] == (1 if merges else None)
