"""The previous UTC day's avoided PR runs, priced by their merged scratch proof's job time.

The CI planner writes `Prefix-only groups` after its existing selectors, once per PR run.
Only groups named there are counted; a draft, a path-filtered group and a repeated read of a
run count nothing. A run's reruns keep its ID and count once. Nothing is reported until all
evidence can be read, so a temporary GitHub failure is retried by the wake.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import ci_proof
import workflow_config


def day(now: float) -> str:
    return (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=1)).date().isoformat()


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        json.dump(value, output)
    os.replace(output.name, path)


def report(date: str, read: Any = ci_proof.read, *, progress: Path | None = None) -> str:
    config = workflow_config.current()
    groups = config.checks.prefix_only
    if not groups:
        return ""
    # Keep only identifiers, costs and completed totals, never API payloads. A killed read
    # resumes the current PR's run; even a failed search keeps its date across midnight.
    identity = json.loads(json.dumps([config.repository, groups, vars(config.checks),
                                    vars(config.workflows), vars(config.annotations)]))
    state: dict[str, Any] = {"identity": identity, "date": date, "done": {}}
    if progress is not None:
        if progress.exists():
            previous = json.loads(progress.read_text())
            if previous.get("identity") == identity and previous.get("date") == date:
                state = previous
        _save(progress, state)
    if "pulls" not in state:
        query = urlencode({"q": f"repo:{config.repository} is:pr is:merged merged:{date}", "per_page": 100})
        search = read(f"search/issues?{query}", paginate=False)
        if search.get("incomplete_results") or search["total_count"] >= 1000:
            raise ValueError("merged pull request search is incomplete")
        pulls = search["items"]
        for page in range(2, (search["total_count"] + 99) // 100 + 1):
            page_data = read(f"search/issues?{query}&page={page}", paginate=False)
            if page_data.get("incomplete_results"):
                raise ValueError("merged pull request search is incomplete")
            pulls.extend(page_data["items"])
        numbers = {item["number"] for item in pulls}
        if len(numbers) != search["total_count"]:
            raise ValueError("merged pull request search is incomplete")
        state["pulls"] = sorted(numbers)
        if progress is not None:
            _save(progress, state)
    for number in state["pulls"]:
        if str(number) in state["done"]:
            continue
        partial = state.setdefault("current", {"number": number})
        if partial["number"] != number:
            raise ValueError("savings checkpoint names another pull request")
        def checkpoint() -> None:
            if progress is not None:
                _save(progress, state)
        state["done"][str(number)] = _pull_totals(date, number, config, read, partial, checkpoint)
        del state["current"]
        checkpoint()
    totals = {name: sum(result.get(name, 0.0) for result in state["done"].values()) for name in groups}
    detail = "; ".join(f"{name} {minutes:.2f}" for name, minutes in totals.items())
    return f"Prefix-only runner minutes saved ({date} UTC): {detail}; total {sum(totals.values()):.2f}."


def _pull_plan(date: str, number: int, config: workflow_config.Config, read: Any) -> tuple[dict[str, float], list[int]]:
    groups = config.checks.prefix_only
    pull = read(f"{config.api}/pulls/{number}")
    if not pull.get("merged_at", "").startswith(date) or pull["base"]["ref"] != "main":
        return {}, []
    squash = read(f"{config.api}/git/commits/{pull['merge_commit_sha']}")
    proof = ci_proof.receipt(squash["message"])
    if (proof is None or squash["tree"]["sha"] != proof["tree"]
            or [p["sha"] for p in squash["parents"]] != [proof["base"]]):
        return {}, []
    costs: dict[str, float] = {}
    for source in proof["sources"]:
        candidates = [name for name in groups
                      if config.checks.groups()[name].workflow == source["workflow"]]
        if not candidates:
            continue
        # The accepted squash proof already records whole jobs on this tree. Pricing does
        # not re-run the gate's proof validation: fetch durations once, and count only jobs
        # both named in the receipt and still successful (including every matrix part).
        parts = read(f"{config.api}/actions/runs/{source['run']}/jobs?per_page=100", "jobs")
        verified = set(source["jobs"]) & ci_proof.passed_jobs(parts)
        for name in candidates:
            group = config.checks.groups()[name]
            names = [job for job in verified if group.contains(job)
                     and job != config.checks.images_plan]
            if not names:
                continue
            minutes = 0.0
            for job in parts:
                job_name = re.sub(r" \(\d+ of \d+\)$", "", job["name"])
                if (job_name not in names or job["name"] == config.checks.images_plan
                        or job.get("conclusion") != "success" or not job.get("runner_name")):
                    continue
                start = datetime.fromisoformat(job["started_at"])
                end = datetime.fromisoformat(job["completed_at"])
                minutes += max(0, (end - start).total_seconds()) / 60
            costs[name] = minutes
    if not costs:
        return {}, []
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
                    p["number"] == number for p in run["pull_requests"]))):
            continue
        counted.add(run["id"])
    return costs, sorted(counted)


def _pull_totals(date: str, number: int, config: workflow_config.Config, read: Any,
                 partial: dict[str, Any], checkpoint: Any) -> dict[str, float]:
    if "runs" not in partial:
        costs, runs = _pull_plan(date, number, config, read)
        partial.update(costs=costs, runs=runs, next=0,
                       totals=dict.fromkeys(config.checks.prefix_only, 0.0))
        checkpoint()
    for index in range(partial["next"], len(partial["runs"])):
        run_id = partial["runs"][index]
        parts = read(f"{config.api}/actions/runs/{run_id}/jobs?per_page=100", "jobs")
        planner = next((job for job in parts if job["name"] == config.checks.ci_plan
                        and job.get("conclusion") == "success"), None)
        if planner is not None:
            _count_run(planner, partial, read)
        partial["next"] = index + 1
        checkpoint()
    return partial["totals"]


def _count_run(planner: dict[str, Any], partial: dict[str, Any], read: Any) -> None:
    notes = ci_proof.planner_notes(planner, read)
    deferred = [note["message"] for note in notes if note.get("title") == ci_proof.DEFERRED]
    if len(deferred) != 1:
        return
    names = json.loads(deferred[0])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("invalid prefix-only groups note")
    for name in set(names) & partial["costs"].keys():
        partial["totals"][name] += partial["costs"][name]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", type=float, default=float(os.environ.get("WORKFLOW_NOW") or datetime.now().timestamp()))
    parser.add_argument("--date", type=lambda value: datetime.strptime(value, "%Y-%m-%d").date().isoformat())
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args()
    try:
        line = report(args.date or day(args.now), progress=args.progress)
    except (ci_proof.Unreadable, workflow_config.ConfigError, ValueError, KeyError, TypeError,
            IndexError, StopIteration, AttributeError, OSError) as error:
        print(f"prefix-only savings could not be read: {error}", file=sys.stderr)
        return 1
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
