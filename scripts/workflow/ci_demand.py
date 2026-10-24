"""The CI demand on its way, for the runner balancer (`runner/balance_runners.py`).

    ci_demand.py write <main> <snapshot>    the tick, each turn, with `session list` on stdin

The pushes coming within half an hour are those of:

- the working cloud workers: `cloud-N` records whose status is `working`;
- the busy local workers and reviewers: `ticket-N` and `review-N` that `session list` shows busy;
- the ready pull requests labelled for the workflow whose checks have not started: no check
  at all on their head;
- the merge line's prefixes still to be built: the window minus the running prefixes, taken
  from the next eligible members in the order the line serves them, where the gate would
  start or extend its line.

A pull request counted by more than one of the first three pushes once. Each push starts the
jobs its pull request's last runs started: for each of the CI, E2E and image workflows, the
newest run of its branch that ran a job, the jobs of its first attempt (the one a push starts)
counted by the runner label they asked for
(the labels of `runner.labels` in `shipline.json`: one ending `-docker` is a Docker runner's,
one ending `-main`, `-planner` or `-standby` is neither, any other an ordinary slot's). A skipped job
counts for nothing, so Docker jobs count only when the pull request changes the images. A pull
request with no such run counts as the others do on average, rounded up.

The forecast goes to `~/.local/state/workflow/ci-demand.json`, written whole, mode 600:

    {"at": <seconds>, "within_minutes": 30, "ordinary": N, "docker": M,
     "pulls": {"cloud": [...], "busy": [...], "ready": [...], "prefixes": [...]}}

and the tick prints "about N ordinary and M Docker jobs within 30 minutes". A completed run's
counts are kept beside it (`ci-demand-runs.json`) and its jobs are not read again. Without
runner labels there is no pool and no forecast. A read that fails leaves the last forecast as it
was: the balancer ignores one older than 10 minutes.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import priority
import workflow_config
from github_budget import Limited, call
from merge_gate import WINDOW_CEILING

WITHIN_MINUTES = 30
# The runs of a branch read at once, newest first: enough for each workflow's last few.
RUNS_PER_PAGE = 30
# A workflow's runs looked at, newest first, for one that ran a job.
RUNS_LOOKED_AT = 3
STATE = Path.home() / ".local/state/workflow"
FORECAST = STATE / "ci-demand.json"
RUN_COUNTS = STATE / "ci-demand-runs.json"
# A `session list` line: the name, two spaces, the state.
BUSY = re.compile(r"(?:ticket|review)-([0-9]+)\s+busy\b")

CONFIG = workflow_config.current()

Counts = tuple[int, int]


def now() -> int:
    return int(os.environ.get("WORKFLOW_NOW") or time.time())


def kind(label: str) -> str | None:
    """The kind of runner a pool label asks for: "ordinary", "docker" or None."""
    if label not in CONFIG.runner.labels or label.endswith(("-main", "-planner", "-standby")):
        return None
    return "docker" if label.endswith("-docker") else "ordinary"


def _api(path: str) -> Any:
    done = call("tick", "api", path)
    done.check_returncode()
    return json.loads(done.stdout)


class Runs:
    """The job counts of each pull request's last runs, a completed run's read only once."""

    def __init__(self) -> None:
        try:
            kept = json.loads(RUN_COUNTS.read_text())
        except OSError, ValueError:
            kept = {}
        self.kept: dict[str, list[Any]] = kept if isinstance(kept, dict) else {}
        self.used: dict[str, list[Any]] = {}

    def _counts(self, run: dict[str, Any]) -> tuple[int, int, bool]:
        """Ordinary and Docker jobs the run's push started, its first attempt (a re-run's attempt
        holds only the jobs run again), and whether it ran any job at all."""
        key = str(int(run["id"]))
        kept = self.kept.get(key)
        if (
            isinstance(kept, list)
            and len(kept) == 3
            and all(isinstance(number, int) and number >= 0 for number in kept[:2])
            and isinstance(kept[2], bool)
        ):
            ordinary, docker, ran = kept
        else:
            jobs = _api(f"{CONFIG.api}/actions/runs/{key}/attempts/1/jobs?per_page=100")["jobs"]
            started = [job for job in jobs if job.get("conclusion") != "skipped"]
            kinds = [
                next((found for label in job.get("labels") or [] if (found := kind(label))), None)
                for job in started
            ]
            ordinary, docker, ran = kinds.count("ordinary"), kinds.count("docker"), bool(started)
        if run.get("status") == "completed":
            self.used[key] = [ordinary, docker, ran]
        return ordinary, docker, ran

    def push(self, branch: str) -> Counts | None:
        """The jobs one push of the branch starts, or None when no run of it ran a job."""
        query = urlencode({"branch": branch, "per_page": RUNS_PER_PAGE})
        runs = _api(f"{CONFIG.api}/actions/runs?{query}")["workflow_runs"]
        runs.sort(key=lambda run: (run.get("created_at") or "", run["id"]), reverse=True)
        found = None
        for workflow in vars(CONFIG.workflows).values():
            own = [run for run in runs if _workflow_file(run) == workflow][:RUNS_LOOKED_AT]
            for run in own:
                ordinary, docker, ran = self._counts(run)
                if ran:
                    found = (
                        (found or (0, 0))[0] + ordinary,
                        (found or (0, 0))[1] + docker,
                    )
                    break
        return found

    def keep(self) -> None:
        _save(RUN_COUNTS, self.used)


def _workflow_file(run: dict[str, Any]) -> str:
    return str(run.get("path") or "").split("@")[0].rsplit("/", 1)[-1]


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".", delete=False) as output:
        json.dump(value, output)
    os.chmod(output.name, 0o600)
    os.replace(output.name, path)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def working_cloud(notes: Path) -> list[int]:
    return sorted(
        int(path.name.removeprefix("cloud-"))
        for path in notes.glob("cloud-*")
        if re.fullmatch(r"cloud-[0-9]+", path.name) and _read(path).get("status") == "working"
    )


def busy(sessions: str) -> list[int]:
    return sorted({int(found[1]) for line in sessions.splitlines() if (found := BUSY.match(line))})


def unchecked_ready(pulls: dict[str, dict[str, Any]]) -> list[int]:
    """The workflow's ready tickets with no check on their head yet."""
    return sorted(
        int(number)
        for number, pull in pulls.items()
        if pull.get("state") == "OPEN"
        and pull.get("isDraft") is False
        and not pull.get("statusCheckRollup")
        and any(
            isinstance(label, dict) and label.get("name") == CONFIG.labels.workflow
            for label in pull.get("labels") or []
        )
    )


def prefixes_to_build(notes: Path) -> list[int]:
    """The members whose prefixes the line builds next: the window minus the running ones, as
    the gate extends its line (`_extend_line` of `merge_gate.py`): nothing behind a blocked
    front, and a new line only with two waiting or a front whose failed prefix passed again."""
    train = _read(notes / "merge-train.json")
    if not train:
        return []
    members = {
        str(member.get("number"))
        for member in (train.get("prefixes") or {}).get("members") or []
        if isinstance(member, dict)
    }
    window = min(max(int(train.get("window", WINDOW_CEILING)), 1), WINDOW_CEILING)
    blocked = set(train.get("blocked", {}))
    away = {*train.get("suspended", {}), *train.get("conflicts", {}), *train.get("unavailable", [])}
    queue = [str(number) for number in train.get("waiting", [])]
    fixes = set(train.get("fixes", []))
    priorities = train.get("priorities", {})
    line = sorted(
        queue,
        key=lambda number: (
            (0, 0)
            if number in fixes
            else (1, priority.rank(priorities.get(number, priority.UNLABELLED)))
        ),
    )
    refreshable = set(train.get("refreshable", []))
    front = next(
        (
            number
            for number in line
            if number not in away and (number not in blocked or number in refreshable)
        ),
        None,
    )
    if (
        front is None
        or front in blocked
        or (not members and len(queue) < 2 and front not in train.get("proven", {}))
    ):
        return []
    held = away | blocked | set(train.get("pending", []))
    eligible = [number for number in line if number not in members and number not in held]
    return [int(number) for number in eligible[: max(window - len(members), 0)]]


def write(main: Path, snapshot: Path, sessions: str) -> int:
    if not any(kind(label) for label in CONFIG.runner.labels):
        return 0
    notes = CONFIG.state(main)
    pulls = json.loads(snapshot.read_text())
    cloud = [number for number in working_cloud(notes) if str(number) in pulls]
    sessions_busy = [n for n in busy(sessions) if str(n) in pulls and n not in cloud]
    ready = [n for n in unchecked_ready(pulls) if n not in cloud and n not in sessions_busy]
    prefixes = [number for number in prefixes_to_build(notes) if str(number) in pulls]
    runs = Runs()
    pushes = [
        runs.push(pulls[str(number)]["headRefName"])
        for number in (*cloud, *sessions_busy, *ready, *prefixes)
    ]
    known = [counts for counts in pushes if counts is not None]
    average = (
        (
            math.ceil(sum(counts[0] for counts in known) / len(known)),
            math.ceil(sum(counts[1] for counts in known) / len(known)),
        )
        if known
        else (0, 0)
    )
    counted = [average if counts is None else counts for counts in pushes]
    forecast = {
        "at": now(),
        "within_minutes": WITHIN_MINUTES,
        "ordinary": sum(counts[0] for counts in counted),
        "docker": sum(counts[1] for counts in counted),
        "pulls": {"cloud": cloud, "busy": sessions_busy, "ready": ready, "prefixes": prefixes},
    }
    _save(FORECAST, forecast)
    runs.keep()
    print(
        f"about {forecast['ordinary']} ordinary and {forecast['docker']} Docker jobs "
        f"within {WITHIN_MINUTES} minutes"
    )
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "write":
            sys.exit(write(Path(sys.argv[2]), Path(sys.argv[3]), sys.stdin.read()))
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        sys.exit(2)
    except Limited:
        sys.exit(75)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f"CI demand: {error}", file=sys.stderr)
        sys.exit(1)
