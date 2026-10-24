"""The previous UTC day's avoided PR runs, priced by their merged scratch proof's job time.

The CI planner writes `Prefix-only groups` after its existing selectors, once per PR run.
Only groups named there are counted; a draft, a path-filtered group and a repeated read of a
run count nothing. A run's reruns keep its ID and count once. Nothing is reported until all
evidence can be read, so a temporary GitHub failure is retried by the wake.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import ci_proof
import workflow_config


def day(now: float) -> str:
    return (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=1)).date().isoformat()


def report(date: str, read: Any = ci_proof.read) -> str:
    config = workflow_config.current()
    groups = config.checks.prefix_only
    if not groups:
        return ""
    totals = dict.fromkeys(groups, 0.0)
    query = urlencode({"q": f"repo:{config.repository} is:pr is:merged merged:{date}", "per_page": 100})
    # Read the search count first; GitHub returns no more than 1000 search matches.
    search = read(f"search/issues?{query}", paginate=False)
    if search.get("incomplete_results") or search["total_count"] >= 1000:
        raise ValueError("merged pull request search is incomplete")
    pulls = search["items"]
    for page in range(2, (search["total_count"] + 99) // 100 + 1):
        page_data = read(f"search/issues?{query}&page={page}", paginate=False)
        if page_data.get("incomplete_results"):
            raise ValueError("merged pull request search is incomplete")
        pulls.extend(page_data["items"])
    for item in pulls:
        pull = read(f"{config.api}/pulls/{item['number']}")
        if not pull.get("merged_at", "").startswith(date) or pull["base"]["ref"] != "main":
            continue
        squash = read(f"{config.api}/git/commits/{pull['merge_commit_sha']}")
        proof = ci_proof.receipt(squash["message"])
        if (proof is None or squash["tree"]["sha"] != proof["tree"]
                or [p["sha"] for p in squash["parents"]] != [proof["base"]]):
            continue
        costs: dict[str, float] = {}
        for source in proof["sources"]:
            candidates = [name for name in groups
                          if config.checks.groups()[name].workflow == source["workflow"]]
            if not candidates:
                continue
            run = read(f"{config.api}/actions/runs/{source['run']}")
            if (run.get("event") != "workflow_dispatch"
                    or not (run.get("head_branch") or "").startswith("workflow/merge-batch-")):
                continue
            verified = ci_proof._source_jobs(source, proof["tree"], read)
            parts = read(f"{config.api}/actions/runs/{source['run']}/jobs?per_page=100", "jobs")
            for name in candidates:
                group = config.checks.groups()[name]
                names = [job for job in verified if group.contains(job)
                         and job != config.checks.images_plan]
                if not names:
                    continue
                minutes = 0.0
                for job in parts:
                    if (not group.contains(job["name"]) or job["name"] == config.checks.images_plan
                            or job.get("conclusion") != "success" or not job.get("runner_name")):
                        continue
                    start = datetime.fromisoformat(job["started_at"])
                    end = datetime.fromisoformat(job["completed_at"])
                    minutes += max(0, (end - start).total_seconds()) / 60
                costs[name] = minutes
        if not costs:
            continue
        filters = urlencode({"branch": pull["head"]["ref"], "event": "pull_request",
                             "created": f"{pull['created_at']}..{pull['merged_at']}", "per_page": 100})
        runs = read(f"{config.api}/actions/workflows/{config.workflows.ci}/runs?{filters}", "workflow_runs")
        if len(runs) >= 1000:
            raise ValueError("pull request run search limit reached")
        counted: set[int] = set()
        for run in runs:
            if (run["id"] in counted or run.get("event") != "pull_request"
                    or run.get("head_repository", {}).get("full_name") != config.repository
                    or run.get("head_branch") != pull["head"]["ref"]
                    or not pull["created_at"] <= run["created_at"] <= pull["merged_at"]
                    or (run.get("pull_requests") and not any(
                        p["number"] == item["number"] for p in run["pull_requests"]))):
                continue
            counted.add(run["id"])
            parts = read(f"{config.api}/actions/runs/{run['id']}/jobs?per_page=100", "jobs")
            planner = next((job for job in parts if job["name"] == config.checks.ci_plan
                            and job.get("conclusion") == "success"), None)
            if planner is None:
                continue
            notes = ci_proof.planner_notes(planner, read)
            deferred = [note["message"] for note in notes if note.get("title") == ci_proof.DEFERRED]
            if len(deferred) != 1:
                continue
            names = json.loads(deferred[0])
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ValueError("invalid prefix-only groups note")
            for name in set(names) & costs.keys():
                totals[name] += costs[name]
    detail = "; ".join(f"{name} {minutes:.2f}" for name, minutes in totals.items())
    return f"Prefix-only runner minutes saved ({date} UTC): {detail}; total {sum(totals.values()):.2f}."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", type=float, default=float(os.environ.get("WORKFLOW_NOW") or datetime.now().timestamp()))
    args = parser.parse_args()
    try:
        line = report(day(args.now))
    except (ci_proof.Unreadable, workflow_config.ConfigError, ValueError, KeyError, TypeError,
            IndexError, StopIteration, AttributeError, OSError) as error:
        print(f"prefix-only savings could not be read: {error}", file=sys.stderr)
        return 1
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
