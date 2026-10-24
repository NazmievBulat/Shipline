"""Counts new PR CI runs and merges in a half-open UTC time window."""

import json
import sys
from datetime import datetime
from urllib.parse import urlencode

from merge_gate import API, REPOSITORY, WORKFLOW_FILES, Refused, api, command


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return parsed


def measure(since: str, until: str) -> dict[str, object]:
    start, end = instant(since), instant(until)
    hours = (end - start).total_seconds() / 3600
    if hours <= 0:
        raise ValueError("the end must be after the start")
    query = urlencode({"event": "pull_request", "created": f"{since}..{until}", "per_page": 100})
    runs = api(f"{API}/actions/workflows/{WORKFLOW_FILES['ci']}/runs?{query}", "workflow_runs")
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
            f"merged:{since}..{until}",
            "--json",
            "number,mergedAt",
        ).stdout
    )
    # GitHub search is inclusive at both ends; use [start, end) for adjacent windows.
    ci_runs = len({run["id"] for run in runs if start <= instant(run["created_at"]) < end})
    merged = [pull["number"] for pull in pulls if start <= instant(pull["mergedAt"]) < end]
    if len(pulls) >= 1000 or len(runs) >= 1000:
        raise ValueError("the window reaches GitHub's search limit; choose a smaller window")
    return {
        "since": since,
        "until": until,
        "hours": hours,
        "ci_runs": ci_runs,
        "merges": len(merged),
        "merged_prs": merged,
        "merges_per_hour": len(merged) / hours,
        "ci_runs_per_merge": ci_runs / len(merged) if merged else None,
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: merge-train-metrics SINCE UNTIL (ISO timestamps with timezone)", file=sys.stderr
        )
        return 1
    try:
        print(json.dumps(measure(sys.argv[1], sys.argv[2]), indent=2))
        return 0
    except (Refused, KeyError, TypeError, ValueError) as error:
        print(f"measurement refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
