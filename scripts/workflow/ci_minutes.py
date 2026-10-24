"""Measure runner job minutes for CI, E2E and Images in a UTC time window.

Use the same 24-hour window length before and after rollout. Minutes are summed job time,
not workflow wall time, clipped to [since, until). Running jobs end at until. The category
columns overlap: for example cancelled image runs are also PR image minutes.
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from merge_gate import API, REPOSITORY, SCRATCH_PREFIX, WORKFLOW_FILES, api, command
from merge_train_metrics import instant


def prefix_disposition(run: dict[str, Any], until: str) -> str:
    """A prefix is discarded only once its member closed on a different tested tree.

    Open members stay unresolved: a green prefix still waiting is not waste. Single-PR whole
    suite scratch runs are counted in scratch minutes but are not speculative prefixes.
    """
    commit = api(f"{API}/git/commits/{run['head_sha']}")
    match = re.fullmatch(r"chore: test merge train pull request (\d+)", commit["message"].strip())
    if not match:
        return "single"
    pull = api(f"{API}/pulls/{match[1]}")
    closed = pull.get("closed_at")
    if not closed or instant(closed) >= instant(until):
        return "unresolved"
    if not pull.get("merged_at"):
        return "discarded"
    squash = api(f"{API}/git/commits/{pull['merge_commit_sha']}")
    return "used" if squash["tree"]["sha"] == commit["tree"]["sha"] else "discarded"


def summarize(runs: list[dict[str, Any]], since: str, until: str) -> dict[str, Any]:
    start, end = instant(since), instant(until)
    if end <= start:
        raise ValueError("until must be later than since")
    groups: dict[tuple[str, str, str], float] = defaultdict(float)
    categories: dict[str, float] = dict.fromkeys(
        (
            "pr_images",
            "cancelled",
            "pr_ci_failed",
            "scratch",
            "main",
            "cancelled_scratch",
            "discarded_prefix",
            "unresolved_prefix",
        ),
        0.0,
    )
    for run in runs:
        # A run of no branch (some scheduled and dynamic runs) has a null head branch.
        branch = run.get("head_branch") or ""
        kind = (
            "scratch"
            if branch.startswith(SCRATCH_PREFIX)
            else "main"
            if branch == "main" and run["event"] != "pull_request"
            else "pr"
            if run["event"] == "pull_request"
            else "other"
        )
        minutes = 0.0
        for job in run["jobs"]:
            if not job.get("runner_name") or not job.get("started_at"):
                continue
            first = max(start, instant(job["started_at"]))
            last = min(end, instant(job["completed_at"]) if job.get("completed_at") else end)
            minutes += max(0, (last - first).total_seconds()) / 60
        conclusion = run.get("conclusion") or "running"
        groups[run["name"], kind, conclusion] += minutes
        for category, selected in {
            "pr_images": kind == "pr" and run["name"] == "Images",
            "cancelled": conclusion == "cancelled",
            "pr_ci_failed": kind == "pr" and run["name"] == "CI" and conclusion == "failure",
            "discarded_prefix": run.get("prefix_disposition") == "discarded",
            "unresolved_prefix": run.get("prefix_disposition") == "unresolved",
            "scratch": kind == "scratch",
            "main": kind == "main",
            "cancelled_scratch": kind == "scratch" and conclusion == "cancelled",
        }.items():
            if selected:
                categories[category] += minutes
    return {
        "since": since,
        "until": until,
        "runner_minutes": round(sum(groups.values()), 2),
        "categories_minutes": {key: round(value, 2) for key, value in categories.items()},
        "groups": [
            {
                "workflow": workflow,
                "kind": kind,
                "conclusion": conclusion,
                "minutes": round(minutes, 2),
            }
            for (workflow, kind, conclusion), minutes in sorted(groups.items())
        ],
    }


def discover(path: str, first: str, last: str) -> list[dict[str, Any]]:
    """The runs of `path` (one workflow's, or the repository's) created from `first` to `last`.

    One API read per page: a large --paginate call can exceed the gate reader's timeout even
    while every individual request succeeds. The search cap is checked on page one, before
    fetching hundreds of runs only to split the interval again; an interval at the cap is split
    instead of losing runs.
    """
    filters: dict[str, str | int] = {"created": f"{first}..{last}", "per_page": 100}
    found = api(f"{path}?{urlencode(filters)}", paginate=False)
    total = found.get("total_count", len(found["workflow_runs"]))
    if total < 1000:
        runs = found["workflow_runs"]
        for page in range(2, (total + 99) // 100 + 1):
            runs.extend(
                api(f"{path}?{urlencode({**filters, 'page': page})}", paginate=False)[
                    "workflow_runs"
                ]
            )
        return runs
    left, right = instant(first), instant(last)
    if (right - left).total_seconds() < 2:
        raise ValueError("too many runs in a second for GitHub to report")
    middle = (left + (right - left) / 2).isoformat(timespec="seconds")
    found = discover(path, first, middle) + discover(path, middle, last)
    return list({run["id"]: run for run in found}.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("since")
    parser.add_argument("until")
    parser.add_argument(
        "--cache", type=Path, help="Cache completed runs' jobs for repeatable reports"
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    start, end = instant(args.since), instant(args.until)
    if end <= start:
        parser.error("until must be later than since")
    measured = []
    # Reruns retain their original creation date. Scan creation history, then discard runs
    # not updated in this window. Split at GitHub's cap instead of losing old reruns.
    created = instant(api(API)["created_at"])

    dispositions: dict[str, str] = {}
    for workflow in ("ci", "e2e", "images"):
        path = f"{API}/actions/workflows/{WORKFLOW_FILES[workflow]}/runs"
        runs = discover(path, created.isoformat(), args.until)
        for run in runs:
            if instant(run["updated_at"]) < start:
                continue
            if run.get("head_branch", "").startswith(SCRATCH_PREFIX):
                head = run["head_sha"]
                if head not in dispositions:
                    dispositions[head] = prefix_disposition(run, args.until)
                run["prefix_disposition"] = dispositions[head]
            for attempt in range(1, run["run_attempt"] + 1):
                path = args.cache / f"{run['id']}-{attempt}.json" if args.cache else None
                if path and path.exists():
                    jobs = json.loads(path.read_text())
                else:
                    jobs = api(
                        f"{API}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100",
                        "jobs",
                    )
                    if path and run["status"] == "completed":
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(jobs))
                # Earlier attempts' conclusion is their own, not the final rerun's.
                failed = any(job.get("conclusion") == "failure" for job in jobs)
                cancelled = any(job.get("conclusion") == "cancelled" for job in jobs)
                conclusion = (
                    ("failure" if failed else "cancelled" if cancelled else "success")
                    if attempt < run["run_attempt"]
                    else run.get("conclusion")
                )
                measured.append({**run, "conclusion": conclusion, "jobs": jobs})
    result = summarize(measured, args.since, args.until)
    pulls = json.loads(
        command(
            "gh",
            "pr",
            "list",
            "-R",
            REPOSITORY,
            "--state",
            "merged",
            "--limit",
            "1000",
            "--search",
            f"merged:{args.since}..{args.until}",
            "--json",
            "number,mergedAt",
        ).stdout
    )
    if len(pulls) >= 1000:
        raise ValueError("GitHub merge search limit reached")
    merged = [pull["number"] for pull in pulls if start <= instant(pull["mergedAt"]) < end]
    result.update(
        merged_prs=merged,
        merges=len(merged),
        merges_per_hour=len(merged) / ((end - start).total_seconds() / 3600),
    )
    encoded = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
