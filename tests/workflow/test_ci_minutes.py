"""Measure job time, including cancelled and overlapping matrix jobs, in the actual script."""

import json
import subprocess
import sys

from workflow_harness import Workflow


def test_ci_minutes_clips_jobs_and_keeps_overlapping_categories(workflow: Workflow) -> None:
    script = workflow.repo / "measure.py"
    script.write_text("""import json
import sys

sys.path.insert(0, "scripts/workflow")
from ci_minutes import summarize

runs = [
    {
        "name": "Images",
        "event": "pull_request",
        "head_branch": "ticket/seven",
        "conclusion": "cancelled",
        "jobs": [
            {
                "runner_name": "home",
                "started_at": "2026-10-23T00:55:00Z",
                "completed_at": "2026-10-23T01:05:00Z",
            },
            {
                "runner_name": "home",
                "started_at": "2026-10-23T01:00:00Z",
                "completed_at": "2026-10-23T01:06:00Z",
            },
            {"runner_name": "", "started_at": "2026-10-23T01:00:00Z", "completed_at": None},
        ],
    },
    {
        "name": "CI",
        "event": "workflow_dispatch",
        "head_branch": "workflow/merge-batch-123",
        "conclusion": None,
        "jobs": [
            {"runner_name": "home", "started_at": "2026-10-23T01:05:00Z", "completed_at": None}
        ],
    },
]
print(json.dumps(summarize(runs, "2026-10-23T01:00:00Z", "2026-10-23T01:10:00Z")))
""")
    done = subprocess.run(
        [sys.executable, str(script)],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["runner_minutes"] == 16
    assert report["categories_minutes"]["pr_images"] == 11
    assert report["categories_minutes"]["cancelled"] == 11
    assert report["categories_minutes"]["scratch"] == 5


def test_ci_minutes_cli_counts_every_attempt_and_merge_in_the_window(workflow: Workflow) -> None:
    workflow.fake(
        "gh",
        """#!/usr/bin/env python3
import json
import sys

path = sys.argv[-1]
if sys.argv[1:3] == ["pr", "list"]:
    print(json.dumps([{"number": 7, "mergedAt": "2026-10-23T01:05:00Z"}]))
elif path == "repos/NazmievBulat/Shipline":
    print('{"created_at": "2026-01-01T00:00:00Z"}')
elif "workflows/ci.yml/runs" in path:
    assert "2026-01-01" in path  # Old reruns remain discoverable.
    print(
        json.dumps(
            {
                "workflow_runs": [
                    {
                        "id": 1,
                        "name": "CI",
                        "event": "pull_request",
                        "head_branch": "ticket/seven",
                        "status": "completed",
                        "conclusion": "success",
                        "updated_at": "2026-10-23T01:10:00Z",
                        "run_attempt": 2,
                        "created_at": "2026-01-02T00:00:00Z",
                    }
                ]
            }
        )
    )
elif "workflows/" in path:
    print('{"workflow_runs": []}')
else:
    attempt = 1 if "/attempts/1/" in path else 2
    print(
        json.dumps(
            {
                "jobs": [
                    {
                        "runner_name": "home",
                        "started_at": "2026-10-23T01:00:00Z",
                        "completed_at": "2026-10-23T01:04:00Z"
                        if attempt == 1
                        else "2026-10-23T01:06:00Z",
                        "conclusion": "failure" if attempt == 1 else "success",
                    }
                ]
            }
        )
    )
""",
    )
    wrapper = workflow.repo / "scripts/workflow/measure-minutes"
    wrapper.write_text('#!/bin/bash\nexec python3 "$(dirname "$0")/ci_minutes.py" "$@"\n')
    done = workflow.run_script("measure-minutes", "2026-10-23T01:00:00Z", "2026-10-24T01:00:00Z")
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["runner_minutes"] == 10
    assert result["categories_minutes"]["pr_ci_failed"] == 4
    assert result["merges"] == 1
    assert result["merges_per_hour"] == 1 / 24


def test_prefix_minutes_separate_discarded_and_still_waiting_work(workflow: Workflow) -> None:
    script = workflow.repo / "prefixes.py"
    script.write_text("""import sys

sys.path.insert(0, "scripts/workflow")
import ci_minutes

answers = {
    "/git/commits/prefix": {
        "message": "chore: test merge train pull request 7",
        "tree": {"sha": "tested"},
    },
    "/pulls/7": {
        "closed_at": "2026-10-23T01:00:00Z",
        "merged_at": "2026-10-23T01:00:00Z",
        "merge_commit_sha": "squash",
    },
    "/git/commits/squash": {"tree": {"sha": "different"}},
}
ci_minutes.api = lambda path: answers[path.removeprefix(ci_minutes.API)]
assert ci_minutes.prefix_disposition({"head_sha": "prefix"}, "2026-10-24T00:00:00Z") == "discarded"
answers["/git/commits/squash"]["tree"]["sha"] = "tested"
assert ci_minutes.prefix_disposition({"head_sha": "prefix"}, "2026-10-24T00:00:00Z") == "used"
answers["/pulls/7"]["closed_at"] = None
assert ci_minutes.prefix_disposition({"head_sha": "prefix"}, "2026-10-24T00:00:00Z") == "unresolved"
""")
    done = subprocess.run(
        [sys.executable, str(script)],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
