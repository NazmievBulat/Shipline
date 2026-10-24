"""The workflow's daily numbers, and the fixed detectors that find its gaps in them.

    workflow-metrics [--days N] [--json]   the report of the last day and of the last N days
                                           (7), as Markdown or as JSON
    workflow-metrics --sample [--sessions -|unread]
                                           one sample of the machine and the sessions, added to
                                           ~/.local/state/workflow/machine.jsonl (kept 8 days);
                                           every tick takes one, from local reads only, and gives
                                           it the `session list` it read on standard input
    workflow-metrics --daily               the tick's daily run: the report to
                                           .local/analyses/metrics-<date>.md in the main
                                           checkout, and its findings to the wake

What it reads: the merged pull requests (GraphQL, in pages of 100), the repository's workflow
runs and their jobs (REST, through the shared GitHub budget, which revalidates what it read
before), the wake log, the notes the gate and the tick leave in the state folder, the hunts'
reports, and the machine's samples. It changes nothing on GitHub and nothing in the repository.

A gap has a fixed shape: a count over a threshold, a duration over a limit, a reason that repeats.
Each detector below is one such rule, its threshold in THRESHOLDS; each one that fires prints one
finding line with its evidence. What to do about a finding is not decided here.
"""

import argparse
import fcntl
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import workflow_config

# What counts as a gap. Each threshold changes only by a pull request of its own: this table is
# the record of what counts as a gap. The detectors read the last N days (7 unless --days).
THRESHOLDS: dict[str, float] = {
    # The merge line's share of a merged pull request's time, from its label to its merge, or
    # the merge line's 90th percentile, in hours.
    "merge_line_share": 0.40,
    "merge_line_p90_hours": 3,
    # The same hand-back reason, its numbers and heads aside, this many times.
    "hand_back_repeats": 5,
    # The same push refusal, lost check, flaky test (a head that failed, then passed on a
    # re-run) or cause of a red main, this many times. Nothing records a push the pre-push hook
    # refused: the report says "not recorded", and that detector never fires.
    "repeats": 3,
    # A question to the owner unanswered this long, in minutes.
    "question_minutes": 60,
    # A session idle for more than this share of its life.
    "idle_share": 0.5,
    # CI runs per merged pull request, and the share of the repository's runs cancelled.
    "ci_runs_per_merge": 1.5,
    "cancelled_share": 0.15,
    # A session kind using more than this many times the session-hours of the workers.
    "kind_hours_over_workers": 2,
    # One job taking more than this share of its run's summed job time (runs of two jobs or
    # more).
    "job_share": 0.60,
    # Main pending or red in more than this share of the wake's check-ins.
    "main_not_green_share": 0.20,
    # Swap-out above this many MB a second while more than this many GB are available.
    "swap_out_mb_per_second": 20,
    "swap_available_gb": 8,
    # System CPU above this share of all processors, averaged over an hour.
    "system_cpu_share": 0.25,
    # The load per processor, in percent, above this floor and more than this many times the CPU
    # pressure (`some avg60`), both averaged over an hour: load that comes from waiting
    # (swapping, blocked processes) rather than from CPU work.
    "load_floor_percent": 50,
    "load_over_pressure": 2,
    # Any one service or session, the tick included, above this many cores over an hour.
    "group_cores": 2,
}

STATE = Path.home() / ".local" / "state" / "workflow"
SAMPLES = STATE / "machine.jsonl"
KEEP_SAMPLES_SECONDS = 8 * 86400
# The file is written again only once its oldest sample is this old: once a day, not each tick.
PRUNE_AFTER_SECONDS = 9 * 86400
# Two samples further apart than this say nothing about what ran between them: the machine was
# off, or the tick did not run.
LONGEST_INTERVAL = 10 * 60
# An hour's average needs this much of the hour sampled.
HOUR = 3600
HOUR_COVERED = 45 * 60
# Completed runs' jobs never change: each is read from GitHub once and kept this long.
JOBS = STATE / "metrics-jobs"
KEEP_JOBS_SECONDS = 8 * 86400
LOCK = STATE / "metrics.lock"
# A daily run that failed is tried again by a later tick, not sooner than this.
TRIED = STATE / "metrics.tried"
RETRY_SECONDS = 30 * 60
# The day whose findings were noted for the wake: a run tried again that day notes none again.
NOTED = STATE / "metrics.noted"
WAKE_LOG = STATE / "wake.log"
ANALYSES = Path(".local") / "analyses"

PROC = Path(os.environ.get("WORKFLOW_PROC_ROOT") or "/proc")
CGROUP_ROOT = Path(os.environ.get("WORKFLOW_CGROUP_ROOT") or "/sys/fs/cgroup")
LOADAVG = Path(os.environ.get("WORKFLOW_LOADAVG_FILE") or PROC / "loadavg")
PAGE = resource.getpagesize()
HERE = Path(__file__).resolve().parent

# `session` starts each session's command in a scope of its own, `workflow-<name>-<pid>.scope`;
# the tick runs as the systemd user service `workflow-tick` (`install-timer`).
SESSION_SCOPE = re.compile(r"workflow-(?P<name>.+)-[0-9]+\.scope")
TICK_SERVICE = "workflow-tick.service"
ROUND = re.compile(r"<!-- workflow-review round=(\d+)(?: result=(sent-back|passed))? -->")
CHECK_IN = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  .+")
TICK_LINE = re.compile(r"(\d{9,}) (.+)")
# The wake's main report: green, pending, red or unknown; an advisory red is one that only an
# advisory service that could not be reached made red. The gate writes "red since unknown time"
# when it could not read when the run ended.
MAIN = re.compile(r"; (main: (?:green|pending|red|unknown).*?); starts: ")
RED = re.compile(r"main: red since (unknown time|\S+), (.+?) \([0-9a-f]{12}\)")
ADVISORY = "advisory service, and nothing was found wrong with main"
# A number or a head in a hand-back's reason: the same reason for every pull request.
NUMBERED = re.compile(r"\b[0-9a-f]*[0-9][0-9a-f]*\b")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
FINDINGS = re.compile(r"(\d+)\s+findings?\b|\bfindings?\W+(\d+)", re.IGNORECASE)
# What of a name read from GitHub may reach a finding, and so the orchestrator's message: words
# and plain marks, no quotes, backticks, angle brackets or new lines, at most this long.
NOT_PLAIN = re.compile(r"[^\w .,:;()\[\]/+!#&=%-]")
PLAIN_LENGTH = 80

MERGED = """query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title createdAt mergedAt body
        timelineItems(first: 100, itemTypes: [LABELED_EVENT, READY_FOR_REVIEW_EVENT]) {
          nodes {
            __typename
            ... on LabeledEvent { createdAt label { name } }
            ... on ReadyForReviewEvent { createdAt }
          }
        }
        reviews(first: 100) { nodes { body submittedAt } }
      }
    }
  }
}"""


def now() -> int:
    return int(os.environ.get("WORKFLOW_NOW") or time.time())


def iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def plain(text: str) -> str:
    return NOT_PLAIN.sub("?", re.sub(r"\s+", " ", text))[:PLAIN_LENGTH]


def percentile(values: list[float], percent: int) -> float:
    """Linear between the closest ranks, as `statistics.quantiles(method="inclusive")`."""
    data = sorted(values)
    whole, part = divmod(percent * (len(data) - 1), 100)
    if part == 0:
        return data[whole]
    return (data[whole] * (100 - part) + data[whole + 1] * part) / 100


def spread(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {key: percentile(values, at) for key, at in (("median", 50), ("p75", 75), ("p90", 90))}


# The sample.


def _fields(path: Path) -> dict[str, int]:
    """`name value` lines, as /proc/vmstat and a control group's cpu.stat hold them."""
    found: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            words = line.split()
            if len(words) == 2 and words[1].isdigit():
                found[words[0]] = int(words[1])
    except OSError:
        pass
    return found


def _available_kb() -> int | None:
    try:
        for line in (PROC / "meminfo").read_text().splitlines():
            words = line.split()
            if words[:1] == ["MemAvailable:"] and words[1].isdigit():
                return int(words[1])
    except OSError, IndexError:
        pass
    return None


def _cpu_pressure() -> float | None:
    try:
        for line in (PROC / "pressure" / "cpu").read_text().splitlines():
            words = line.split()
            if words[:1] == ["some"]:
                return float(dict(word.split("=", 1) for word in words[1:])["avg60"])
    except OSError, KeyError, ValueError:
        pass
    return None


def _load() -> float | None:
    try:
        return float(LOADAVG.read_text().split()[0])
    except OSError, IndexError, ValueError:
        return None


def _processors() -> int | None:
    # `nproc`, as `session` reads it: the processors this machine lets the sessions use.
    try:
        done = subprocess.run(["nproc"], capture_output=True, text=True, timeout=5, check=False)
        return int(done.stdout.strip()) if done.returncode == 0 else None
    except OSError, ValueError, subprocess.TimeoutExpired:
        return None


def _groups(prefix: str | None) -> dict[str, int]:
    """The CPU time, in microseconds, of each session's scope, of the tick's service and, when
    the runner pool names its prefix, of each of its services and containers, all named
    `<prefix>-runner…`. A group's time holds its children's: nothing below a matched group is
    read again."""
    found: dict[str, int] = {}
    pool = prefix and re.compile(rf"{re.escape(prefix)}-runner(?:-.+)?\.(?:service|scope)")
    for folder, children, _files in os.walk(CGROUP_ROOT):
        name = os.path.basename(folder)
        key = None
        if session := SESSION_SCOPE.fullmatch(name):
            key = session["name"]
        elif name == TICK_SERVICE:
            key = name.removesuffix(".service")
        elif pool and pool.fullmatch(name):
            key = name.rsplit(".", 1)[0]
        if key is None:
            continue
        children.clear()
        used = _fields(Path(folder) / "cpu.stat").get("usage_usec")
        if used is not None:
            found[key] = found.get(key, 0) + used
    return found


def _states(listed: str) -> dict[str, str]:
    return {
        words[0]: words[1]
        for words in (line.split() for line in listed.splitlines())
        if len(words) >= 2
    }


def _sessions() -> dict[str, str] | None:
    """Each running session's state, from `session list`; None when it cannot be read."""
    try:
        done = subprocess.run(
            ["bash", str(HERE / "session"), "list"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if done.returncode:
        return None
    return _states(done.stdout)


def _asking(config: workflow_config.Config) -> dict[str, int]:
    """The questions to the owner still open, as the wake reads them (`notify` notes them): a
    session idle with its question, and a cloud worker's question."""
    found: dict[str, int] = {}
    state = config.state_folder
    for note in sorted(config.main.glob(f".claude/worktrees/*/{state}/*.asking")):
        if note.with_suffix(".idle").exists():
            asked = _first_number(note)
            if asked is not None:
                found[note.stem] = asked
    for note in sorted(config.state().glob("cloud-*.asking")):
        if (note.parent / note.stem).is_file():
            asked = _first_number(note)
            if asked is not None:
                found[note.stem] = asked
    return found


def _first_number(path: Path) -> int | None:
    try:
        words = path.read_text().split()
    except OSError:
        return None
    return int(words[0]) if words and words[0].isdigit() else None


def sample(listed: str | None = None) -> int:
    """With `listed`, the sessions are what the caller listed: "-" reads them from standard
    input, "unread" says they could not be read."""
    config = workflow_config.current()
    vmstat = _fields(PROC / "vmstat")
    root = _fields(CGROUP_ROOT / "cpu.stat")
    record = {
        "at": now(),
        "processors": _processors(),
        "pswpin": vmstat.get("pswpin"),
        "pswpout": vmstat.get("pswpout"),
        "available_kb": _available_kb(),
        "cpu_pressure": _cpu_pressure(),
        "load": _load(),
        "cpu": (
            {"usage_usec": root["usage_usec"], "system_usec": root["system_usec"]}
            if {"usage_usec", "system_usec"} <= root.keys()
            else None
        ),
        # The runner pool names its services and containers by `runner.prefix`.
        "groups": _groups(config.runner.prefix),
        "sessions": (
            _sessions()
            if listed is None
            else None
            if listed == "unread"
            else _states(sys.stdin.read())
        ),
        "asking": _asking(config),
    }
    STATE.mkdir(parents=True, exist_ok=True)
    _prune(record["at"])
    descriptor = os.open(SAMPLES, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a") as samples:
        os.fchmod(descriptor, 0o600)
        samples.write(json.dumps(record, separators=(",", ":")) + "\n")
    return 0


def _prune(moment: int) -> None:
    """Samples older than 8 days are dropped, once the oldest is 9 days old."""
    try:
        with SAMPLES.open() as samples:
            first = samples.readline()
    except OSError:
        return
    try:
        if json.loads(first)["at"] >= moment - PRUNE_AFTER_SECONDS:
            return
    except ValueError, KeyError, TypeError:
        pass
    kept = [line for line in read_samples() if line["at"] >= moment - KEEP_SAMPLES_SECONDS]
    with tempfile.NamedTemporaryFile("w", dir=STATE, delete=False) as output:
        output.writelines(json.dumps(line, separators=(",", ":")) + "\n" for line in kept)
    os.chmod(output.name, 0o600)
    os.replace(output.name, SAMPLES)


def read_samples() -> list[dict[str, Any]]:
    found = []
    try:
        lines = SAMPLES.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("at"), (int, float)):
            found.append(record)
    return sorted(found, key=lambda record: record["at"])


# What the report reads.


@dataclass
class CheckIn:
    at: float
    told: bool = False
    usage: int | None = None
    sessions: int | None = None
    main: str | None = None


@dataclass
class Sources:
    pulls: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    check_ins: list[CheckIn]
    ticks: list[tuple[float, str]]
    hand_backs: list[tuple[float, str, str]]
    lost: list[tuple[float, str, str]]
    hunts: list[tuple[float, str, int | None]]
    samples: list[dict[str, Any]]
    wake_log: bool


def read_wake_log() -> tuple[list[CheckIn], list[tuple[float, str]], bool]:
    """The wake's check-ins, and the lines the tick writes, one event each."""
    try:
        lines = WAKE_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return [], [], False
    check_ins: list[CheckIn] = []
    ticks: list[tuple[float, str]] = []
    for line in lines:
        if found := CHECK_IN.fullmatch(line):
            # The machine's clock, as the wake writes it.
            moment = time.mktime(time.strptime(found[1], "%Y-%m-%d %H:%M:%S"))
            check_ins.append(CheckIn(moment))
        elif found := TICK_LINE.fullmatch(line):
            ticks.append((int(found[1]), found[2]))
        elif check_ins and line.startswith("  usage: "):
            if used := re.search(r"5-hour window (\d+)% used", line):
                check_ins[-1].usage = int(used[1])
        elif check_ins and line.startswith("  sessions: "):
            listed = line.removeprefix("  sessions: ").split("; pull requests: ", 1)[0]
            if listed == "none":
                check_ins[-1].sessions = 0
            elif listed != "could not be read":
                check_ins[-1].sessions = len(listed.split(", "))
            if main := MAIN.search(line):
                check_ins[-1].main = main[1]
        elif check_ins and line.startswith("  told "):
            check_ins[-1].told = True
    return check_ins, ticks, True


def main_state(text: str) -> tuple[str, list[str], str | None]:
    """A main report's state, the checks it names, and since when it has been red."""
    if red := RED.match(text):
        state = "advisory" if ADVISORY in text else "red"
        return state, red[2].split(", "), red[1]
    if pending := re.match(r"main: pending, (.+?) \([0-9a-f]{12}\)", text):
        return "pending", pending[1].split(", "), None
    if text.startswith("main: unknown"):
        return "unknown", [text.removeprefix("main: unknown, ")], None
    return text.split()[1].strip(","), [], None


def _notes(folder: Path, field: str) -> list[tuple[float, str, str]]:
    """The notes a folder holds, as (when written, pull request, the field)."""
    found = []
    for path in sorted(folder.glob("*")) if folder.is_dir() else []:
        try:
            note = json.loads(path.read_text())
            found.append((path.stat().st_mtime, str(note["number"]), str(note[field])))
        except OSError, ValueError, KeyError, TypeError:
            continue
    return found


def read_hunts(main: Path) -> list[tuple[float, str, int | None]]:
    """Each hunt's report, as (when written, its name, the count of findings near its top)."""
    found: list[tuple[float, str, int | None]] = []
    folder = main / ".local" / "hunts"
    for hunt in sorted(folder.iterdir()) if folder.is_dir() else []:
        if not hunt.is_dir():
            continue
        report = hunt / "report.md"
        try:
            top = report.read_text(errors="replace").splitlines()[:10]
            written = report.stat().st_mtime
        except OSError:
            # A hunt without its report, as of when its folder last changed.
            found.append((hunt.stat().st_mtime, hunt.name, None))
            continue
        count = next(
            (int(match[1] or match[2]) for line in top if (match := FINDINGS.search(line))), None
        )
        found.append((written, hunt.name, count))
    return found


def merged_pulls(since: float, until: float) -> list[dict[str, Any]]:
    from merge_gate import REPOSITORY, Refused, read_github

    words = f"repo:{REPOSITORY} is:pr is:merged base:main merged:{iso(since)}..{iso(until)}"
    pulls: list[dict[str, Any]] = []
    after = None
    while True:
        arguments = ["api", "graphql", "-f", f"query={MERGED}", "-f", f"q={words}"]
        if after:
            arguments += ["-f", f"after={after}"]
        done = read_github(*arguments)
        if done.returncode:
            raise Refused(f"the merged pull requests could not be read: {done.stderr.strip()}")
        page = json.loads(done.stdout)["data"]["search"]
        pulls.extend(node for node in page["nodes"] if node)
        if not page["pageInfo"]["hasNextPage"]:
            return pulls
        if len(pulls) >= 1000:
            raise ValueError("the window reaches GitHub's search limit; choose fewer days")
        after = page["pageInfo"]["endCursor"]


def _jobs(path: str, name: str) -> list[dict[str, Any]]:
    from merge_gate import api

    cached = JOBS / name
    try:
        return json.loads(cached.read_text())
    except OSError, ValueError:
        pass
    jobs = api(path, "jobs")
    JOBS.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=JOBS, delete=False) as output:
        json.dump(jobs, output)
    os.replace(output.name, cached)
    return jobs


def workflow_runs(since: float, until: float) -> list[dict[str, Any]]:
    """Every run of the repository created in the window, with the jobs of the completed ones,
    and of a re-run that passed, the jobs of the attempt before it."""
    from ci_minutes import discover
    from merge_gate import API

    for old in JOBS.glob("*.json") if JOBS.is_dir() else []:
        if old.stat().st_mtime < time.time() - KEEP_JOBS_SECONDS:
            old.unlink(missing_ok=True)
    runs = [
        run
        for run in discover(f"{API}/actions/runs", iso(since), iso(until))
        if since <= instant(run["created_at"]) < until
    ]
    for run in runs:
        if run.get("status") != "completed":
            continue
        attempt = run.get("run_attempt") or 1
        # By its attempt: a re-run started since the list was read has jobs of its own.
        run["jobs"] = _jobs(
            f"{API}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100",
            f"{run['id']}-{attempt}.json",
        )
        if attempt > 1 and run.get("conclusion") == "success":
            run["earlier_jobs"] = _jobs(
                f"{API}/actions/runs/{run['id']}/attempts/{attempt - 1}/jobs?per_page=100",
                f"{run['id']}-{attempt - 1}.json",
            )
    return sorted(runs, key=lambda run: run["id"])


def read_sources(since: float, until: float) -> Sources:
    config = workflow_config.current()
    check_ins, ticks, wake_log = read_wake_log()
    notes = config.state()
    return Sources(
        pulls=merged_pulls(since, until),
        runs=workflow_runs(since, until),
        check_ins=check_ins,
        ticks=ticks,
        hand_backs=_notes(notes / "merge-handbacks.pending", "reason")
        + _notes(notes / "merge-handbacks.sent", "reason"),
        lost=_notes(notes / "lost-checks.pending", "check")
        + _notes(notes / "lost-checks.sent", "check"),
        hunts=read_hunts(config.main),
        samples=read_samples(),
        wake_log=wake_log,
    )


# The numbers of one window.


@dataclass
class Window:
    since: float
    until: float
    days: int

    def holds(self, moment: float | None) -> bool:
        return moment is not None and self.since <= moment < self.until


def _stages(pull: dict[str, Any], label: str) -> dict[str, float] | None:
    """A merged pull request's stages in hours: build (its label to ready), review (ready to
    the passed round, any rework after a send-back included) and the merge line (the passed
    round to its merge)."""
    events = pull["timelineItems"]["nodes"]
    labelled = [
        instant(event["createdAt"])
        for event in events
        if event.get("__typename") == "LabeledEvent"
        and (event.get("label") or {}).get("name") == label
    ]
    ready = [
        instant(event["createdAt"])
        for event in events
        if event.get("__typename") == "ReadyForReviewEvent"
    ]
    # The first passed round after the last send-back: a round again after a conflict, while the
    # pull request waits in the merge line, is the merge line's time.
    rounds = sorted(
        (instant(review["submittedAt"]), found[2] == "passed")
        for review in pull["reviews"]["nodes"]
        if review.get("submittedAt") and (found := ROUND.match(review.get("body") or ""))
    )
    sent_back = [moment for moment, passed in rounds if not passed]
    passed = [
        moment for moment, passed in rounds if passed and (not sent_back or moment > sent_back[-1])
    ]
    if not labelled or not passed:
        return None
    start = min(labelled)
    readied = max(min(ready) if ready else instant(pull["createdAt"]), start)
    round_passed = max(passed[0], readied)
    merged = max(instant(pull["mergedAt"]), round_passed)
    return {
        "build": (readied - start) / HOUR,
        "review": (round_passed - readied) / HOUR,
        "merge_line": (merged - round_passed) / HOUR,
        "total": (merged - start) / HOUR,
    }


def _weaker_tests(body: str) -> int | None:
    """The items of the "Weaker tests" sections of a description; None when it has none."""
    items = None
    level = None
    for line in body.splitlines():
        if heading := HEADING.match(line):
            if level is not None and len(heading[1]) <= level:
                level = None
            if heading[2].casefold().startswith(("weaker tests", "weaker-tests")):
                level = len(heading[1])
                items = items or 0
            continue
        if level is not None and ITEM.match(line):
            items = (items or 0) + 1
    return items


def _summary(check_ins: list[CheckIn]) -> str:
    """What the wake said during a gap: its check-ins, main's states, the usage, the sessions."""
    if not check_ins:
        return "no check-ins"
    states = Counter(main_state(check_in.main)[0] for check_in in check_ins if check_in.main)
    order = ("green", "red", "pending", "advisory", "unknown")
    said = [f"{len(check_ins)} check-ins"]
    if states:
        said.append(
            "main " + ", ".join(f"{state} {states[state]}" for state in order if states[state])
        )
    used = [check_in.usage for check_in in check_ins if check_in.usage is not None]
    if used:
        said.append(f"5-hour usage up to {max(used)}%")
    sessions = [check_in.sessions for check_in in check_ins if check_in.sessions is not None]
    if sessions:
        said.append(f"up to {max(sessions)} sessions")
    return "; ".join(said)


def merged_numbers(window: Window, sources: Sources, label: str) -> dict[str, Any]:
    pulls = sorted(
        (pull for pull in sources.pulls if window.holds(instant(pull["mergedAt"]))),
        key=lambda pull: instant(pull["mergedAt"]),
    )
    stages = {pull["number"]: found for pull in pulls if (found := _stages(pull, label))}
    totals = sum(found["total"] for found in stages.values())
    table: dict[str, Any] = {}
    for stage in ("build", "review", "merge_line", "total"):
        values = [found[stage] for found in stages.values()]
        table[stage] = spread(values)
        if table[stage] is not None:
            table[stage]["share"] = sum(values) / totals if totals else None
    moments = [instant(pull["mergedAt"]) for pull in pulls]
    # The gap since the last merge is still open at the window's end. The one before the first
    # merge is not known: the merge before it is outside the window.
    ends = [*moments[1:], window.until] if moments else []
    gaps = sorted(zip(moments, ends, strict=True), key=lambda gap: gap[1] - gap[0], reverse=True)[
        :3
    ]
    return {
        "numbers": [pull["number"] for pull in pulls],
        "per_hour": len(pulls) / ((window.until - window.since) / HOUR),
        "stages": table,
        "timed": sorted(stages),
        "gaps": [
            {
                "from": iso(start),
                "until": iso(end),
                "hours": (end - start) / HOUR,
                "open": end == window.until,
                "wake": _summary([c for c in sources.check_ins if start <= c.at < end]),
            }
            for start, end in gaps
        ],
    }


def _minutes(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (instant(end) - instant(start)) / 60


def run_numbers(window: Window, sources: Sources, merges: int) -> dict[str, Any]:
    from ci_minutes import summarize
    from merge_gate import REPOSITORY, WORKFLOW_FILES

    runs = [run for run in sources.runs if window.holds(instant(run["created_at"]))]
    completed = [run for run in runs if run.get("status") == "completed"]
    names = {
        f".github/workflows/{WORKFLOW_FILES['ci']}": "CI",
        f".github/workflows/{WORKFLOW_FILES['e2e']}": "E2E",
        f".github/workflows/{WORKFLOW_FILES['images']}": "Images",
    }

    def own(run: dict[str, Any]) -> bool:
        """One of the three workflows, from the repository's own branches: a fork's pull request
        runs its own copy of them under the same path, and names its run and its jobs."""
        head = (run.get("head_repository") or {}).get("full_name") or ""
        return run.get("path") in names and head.casefold() == REPOSITORY.casefold()

    workflows: dict[str, Any] = {}
    for name in ("CI", "E2E", "Images"):
        durations = [
            minutes
            for run in completed
            if names.get(run.get("path", "")) == name
            and (minutes := _minutes(run.get("run_started_at"), run.get("updated_at"))) is not None
        ]
        workflows[name] = (
            {
                "runs": len(durations),
                "median_minutes": percentile(durations, 50),
                "p90_minutes": percentile(durations, 90),
            }
            if durations
            else None
        )
    slowest = None
    shares = []
    # Only the three workflows Shipline knows, of its own branches: another one's names are
    # anybody's.
    for run in filter(own, completed):
        # Jobs that ran: a skipped job takes no time, and a clock can run backwards.
        timed = [
            (minutes, job["name"])
            for job in run.get("jobs", [])
            if (minutes := _minutes(job.get("started_at"), job.get("completed_at"))) is not None
            and minutes > 0
        ]
        for minutes, job in timed:
            if slowest is None or minutes > slowest["minutes"]:
                slowest = {
                    "workflow": plain(run["name"]),
                    "job": plain(job),
                    "run": run["id"],
                    "minutes": minutes,
                }
        if len(timed) >= 2:
            longest, job = max(timed)
            shares.append(
                {
                    "job": f"{run['name']}: {job}",
                    "run": run["id"],
                    "share": longest / sum(m for m, _ in timed),
                }
            )
    ci_runs = sum(
        1
        for run in runs
        if names.get(run.get("path", "")) == "CI" and run.get("event") == "pull_request"
    )
    cancelled = [run["id"] for run in completed if run.get("conclusion") == "cancelled"]
    flaky: Counter[str] = Counter()
    for run in filter(own, completed):
        for job in run.get("earlier_jobs", []):
            if job.get("conclusion") == "failure":
                flaky[f"{run['name']}: {job['name']}"] += 1
    minutes = summarize(completed, iso(window.since), iso(window.until))
    scratch = workflow_config.current().merge.scratch_prefix
    return {
        "workflows": workflows,
        "slowest_job": slowest,
        "ci_runs": ci_runs,
        "ci_runs_per_merge": ci_runs / merges if merges else None,
        "completed": len(completed),
        "cancelled": len(cancelled),
        "cancelled_runs": cancelled,
        "cancelled_share": len(cancelled) / len(completed) if completed else None,
        "runner_minutes": minutes["runner_minutes"],
        "categories_minutes": minutes["categories_minutes"],
        "job_shares": [share for share in shares if share["share"] > THRESHOLDS["job_share"]],
        "flaky": dict(flaky),
        "failed_scratch_runs": sum(
            1
            for run in completed
            if (run.get("head_branch") or "").startswith(scratch)
            and run.get("conclusion") == "failure"
        ),
    }


def main_numbers(window: Window, sources: Sources) -> dict[str, Any]:
    known = [check_in for check_in in sources.check_ins if check_in.main]
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for check_in in known:
        if not window.holds(check_in.at):
            continue
        state, names, _ = main_state(check_in.main or "")
        states[state] += 1
        reasons.update(names)
    # A red episode lasts from its "since" to the first check-in that no longer names it.
    episodes = []
    current = None
    for check_in in known:
        state, names, since = main_state(check_in.main or "")
        key = (state, since) if state in ("red", "advisory") else None
        if current and key != current["key"]:
            current["until"] = check_in.at
            current = None
        if key and current is None:
            cause = ", ".join(names) + (" (advisory)" if state == "advisory" else "")
            try:
                start = instant(since) if since else check_in.at
            except ValueError:
                # "unknown time": the gate could not read when its run ended.
                start = check_in.at
            current = {
                "key": key,
                "start": start,
                "cause": cause,
                "until": None,
            }
            episodes.append(current)
    check_ins = sum(states.values())
    return {
        "check_ins": check_ins,
        "share": {state: count / check_ins for state, count in states.items()},
        "reasons": dict(reasons),
        "red_episodes": [
            {
                "since": iso(episode["start"]),
                "hours": ((episode["until"] or window.until) - episode["start"]) / HOUR,
                "ongoing": episode["until"] is None,
                "cause": episode["cause"],
            }
            for episode in episodes
            if episode["start"] < window.until
            and (episode["until"] is None or episode["until"] > window.since)
        ],
    }


def turn_numbers(window: Window, sources: Sources, merges: int) -> dict[str, Any]:
    ticks = [line for moment, line in sources.ticks if window.holds(moment)]
    counted = {
        "nudges": sum(line.endswith(("; reviewer nudged", "; session nudged")) for line in ticks),
        "status_checks": sum(line.endswith("; status check sent") for line in ticks),
        "finish_messages": sum(line.endswith("; told to finish") for line in ticks),
        "wakes": sum(c.told for c in sources.check_ins if window.holds(c.at)),
    }
    hand_backs = Counter(
        NUMBERED.sub("#", reason)
        for moment, _, reason in sources.hand_backs
        if window.holds(moment)
    )
    counted["hand_backs"] = sum(hand_backs.values())
    return {
        **counted,
        "hand_backs": dict(hand_backs),
        "per_merge": {key: value / merges if merges else None for key, value in counted.items()},
    }


def _intervals(
    samples: list[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], dict[str, Any], float]]:
    for before, after in zip(samples, samples[1:], strict=False):
        elapsed = after["at"] - before["at"]
        if 0 < elapsed <= LONGEST_INTERVAL:
            yield before, after, elapsed


def session_numbers(every_sample: list[dict[str, Any]]) -> dict[str, Any]:
    # A tick whose `session list` failed knows nothing of the sessions: its sample is no change.
    samples = [record for record in every_sample if record.get("sessions") is not None]
    # Seconds, summed before they are hours.
    lived: Counter[str] = Counter()
    waited: Counter[str] = Counter()
    for before, after, elapsed in _intervals(samples):
        then, later = before.get("sessions") or {}, after.get("sessions") or {}
        for name, state in then.items():
            if name in later:
                lived[name] += elapsed
                waited[name] += elapsed if state == "idle" else 0
    seen: dict[str, list[float]] = {}
    stops = 0
    for index, record in enumerate(samples):
        sessions = record.get("sessions") or {}
        for name in sessions:
            seen.setdefault(name, []).append(record["at"])
        earlier = (samples[index - 1].get("sessions") or {}) if index else {}
        stops += (
            sum(
                1
                for name, state in sessions.items()
                if state == "limit" and earlier.get(name) != "limit"
            )
            if index
            else 0
        )
    kinds: dict[str, Any] = {}
    for name in sorted(lived):
        kind = kinds.setdefault(name.split("-", 1)[0], {"names": [], "lived": 0, "idle": 0})
        kind["names"].append(name)
        kind["lived"] += lived[name]
        kind["idle"] += waited[name]
    asked: dict[tuple[str, int], float] = {}
    # The questions are read from their notes, not from `session list`: every sample has them.
    for record in every_sample:
        for name, moment in (record.get("asking") or {}).items():
            asked[name, moment] = max(asked.get((name, moment), 0), record["at"])
    return {
        "kinds": {
            kind: {
                "sessions": len(found["names"]),
                "median_lifetime_hours": percentile(
                    [(max(seen[name]) - min(seen[name])) / HOUR for name in found["names"]], 50
                ),
                "hours": found["lived"] / HOUR,
                "idle_share": found["idle"] / found["lived"],
            }
            for kind, found in sorted(kinds.items())
        },
        "idle": {
            name: {"hours": lived[name] / HOUR, "idle_share": waited[name] / lived[name]}
            for name in sorted(lived)
        },
        "questions": [
            {"session": name, "waited_minutes": round((last - moment) / 60)}
            for (name, moment), last in sorted(asked.items())
        ],
        "limit_stops": stops,
    }


def _hours(
    series: list[tuple[float, float, tuple[float, ...]]],
) -> Iterator[tuple[float, tuple[float, ...]]]:
    """The averages of the hour that ends at each interval's end, where enough of it was
    sampled. The series holds (end, length, values) by end."""
    start = 0
    covered = 0.0
    sums = [0.0] * (len(series[0][2]) if series else 0)
    for end, length, values in series:
        covered += length
        sums = [total + value * length for total, value in zip(sums, values, strict=True)]
        while series[start][0] - series[start][1] < end - HOUR - 1e-6:
            old_length, old_values = series[start][1], series[start][2]
            covered -= old_length
            sums = [
                total - value * old_length for total, value in zip(sums, old_values, strict=True)
            ]
            start += 1
        if covered >= HOUR_COVERED:
            yield end, tuple(total / covered for total in sums)


def _peak(
    series: list[tuple[float, float, tuple[float, ...]]], which: int = 0
) -> tuple[float, float] | None:
    hours = list(_hours(series))
    if not hours:
        return None
    end, values = max(hours, key=lambda hour: hour[1][which])
    return values[which], end


def machine_numbers(samples: list[dict[str, Any]]) -> dict[str, Any]:
    swaps = []
    swaps_in = []
    system = []
    load = []
    groups: dict[str, list[tuple[float, float, tuple[float, ...]]]] = {}
    for before, after, elapsed in _intervals(samples):
        if before.get("pswpout") is not None and after.get("pswpout") is not None:
            pages = after["pswpout"] - before["pswpout"]
            if pages >= 0:
                swaps.append(
                    (after["at"], pages * PAGE / elapsed / 2**20, after.get("available_kb"))
                )
        if before.get("pswpin") is not None and after.get("pswpin") is not None:
            pages = after["pswpin"] - before["pswpin"]
            if pages >= 0:
                swaps_in.append((after["at"], pages * PAGE / elapsed / 2**20))
        processors = after.get("processors")
        if processors and before.get("cpu") and after.get("cpu"):
            used = after["cpu"]["system_usec"] - before["cpu"]["system_usec"]
            if used >= 0:
                system.append((after["at"], elapsed, (used / (elapsed * 1e6 * processors),)))
        if processors and after.get("load") is not None and after.get("cpu_pressure") is not None:
            load.append(
                (after["at"], elapsed, (after["load"] / processors * 100, after["cpu_pressure"]))
            )
        for name, used in (after.get("groups") or {}).items():
            earlier = (before.get("groups") or {}).get(name)
            if earlier is not None and used >= earlier:
                groups.setdefault(name, []).append(
                    (after["at"], elapsed, ((used - earlier) / (elapsed * 1e6),))
                )
    found: dict[str, Any] = {}
    if swaps:
        worst = max(swaps, key=lambda swap: swap[1])
        found["swap_out_mb_per_second"] = {
            "max": worst[1],
            "at": iso(worst[0]),
            "available_gb": worst[2] / 2**20 if worst[2] is not None else None,
        }
        found["swap_out_with_memory"] = [
            {"at": iso(at), "mb_per_second": rate, "available_gb": kb / 2**20}
            for at, rate, kb in swaps
            if rate > THRESHOLDS["swap_out_mb_per_second"]
            and kb is not None
            and kb / 2**20 > THRESHOLDS["swap_available_gb"]
        ]
    if swaps_in:
        worst_in = max(swaps_in, key=lambda swap: swap[1])
        found["swap_in_mb_per_second"] = {"max": worst_in[1], "at": iso(worst_in[0])}
    available = [
        record["available_kb"] for record in samples if record.get("available_kb") is not None
    ]
    if available:
        found["available_gb"] = {
            "min": min(available) / 2**20,
            "median": percentile(available, 50) / 2**20,
        }
    if peak := _peak(system):
        found["system_cpu_share"] = {"max_hour": peak[0], "hour_to": iso(peak[1])}
    if load:
        load_peak, pressure_peak = _peak(load, 0), _peak(load, 1)
        if load_peak and pressure_peak:
            found["load_percent"] = {"max_hour": load_peak[0], "hour_to": iso(load_peak[1])}
            found["cpu_pressure"] = {"max_hour": pressure_peak[0], "hour_to": iso(pressure_peak[1])}
        waiting = [
            (end, values)
            for end, values in _hours(load)
            if values[0] > THRESHOLDS["load_floor_percent"]
            and values[0] > THRESHOLDS["load_over_pressure"] * values[1]
        ]
        if waiting:
            ratio = THRESHOLDS["load_over_pressure"]
            end, values = max(waiting, key=lambda hour: hour[1][0] - ratio * hour[1][1])
            found["load_from_waiting"] = {
                "load_percent": values[0],
                "cpu_pressure": values[1],
                "hour_to": iso(end),
            }
    found["groups"] = {}
    for name, series in sorted(groups.items()):
        if peak := _peak(series):
            total = sum(length for _, length, _ in series)
            found["groups"][name] = {
                "max_hour_cores": peak[0],
                "hour_to": iso(peak[1]),
                "average_cores": sum(length * values[0] for _, length, values in series) / total,
            }
    return found


def quality_numbers(window: Window, sources: Sources) -> tuple[dict[str, Any], list[str]]:
    pulls = [pull for pull in sources.pulls if window.holds(instant(pull["mergedAt"]))]
    reviewed = 0
    sent_back = 0
    weaker = 0
    without = []
    for pull in sorted(pulls, key=lambda pull: pull["number"]):
        rounds = [
            found
            for review in pull["reviews"]["nodes"]
            if (found := ROUND.match(review.get("body") or ""))
        ]
        if rounds:
            reviewed += 1
            sent_back += sum(found[2] != "passed" for found in rounds)
        items = _weaker_tests(pull.get("body") or "")
        if items is None:
            without.append(str(pull["number"]))
        weaker += items or 0
    hunts = 0
    missing = []
    for written, name, count in sources.hunts:
        if not window.holds(written):
            continue
        if count is None:
            missing.append(name)
        else:
            hunts += count
    not_found = []
    if without:
        not_found.append("weaker tests section: " + ", ".join(without))
    not_found.extend(f"hunt report: {name}" for name in missing)
    return {
        "send_back_rounds_per_review": sent_back / reviewed if reviewed else None,
        "weaker_tests_items": weaker,
        "hunt_findings": hunts,
        "reverts": sorted(
            pull["number"]
            for pull in pulls
            if re.match(r"revert\b", pull.get("title") or "", re.IGNORECASE)
        ),
        "lost_checks": dict(
            Counter(check for moment, _, check in sources.lost if window.holds(moment))
        ),
        "push_refusals": "not recorded",
    }, not_found


def window_numbers(window: Window, sources: Sources) -> dict[str, Any]:
    label = workflow_config.current().labels.workflow
    merged = merged_numbers(window, sources, label)
    merges = len(merged["numbers"])
    runs = run_numbers(window, sources, merges)
    samples = [record for record in sources.samples if window.since <= record["at"] <= window.until]
    quality, not_found = quality_numbers(window, sources)
    quality["failed_scratch_runs"] = runs.pop("failed_scratch_runs")
    quality["flaky"] = runs.pop("flaky")
    if not sources.wake_log:
        not_found.append("wake log")
    if not samples:
        not_found.append("machine samples")
    return {
        "since": iso(window.since),
        "until": iso(window.until),
        "days": window.days,
        "merged": merged,
        "runs": runs,
        "main": main_numbers(window, sources),
        "turns": turn_numbers(window, sources, merges),
        "sessions": session_numbers(samples),
        "machine": machine_numbers(samples),
        "quality": quality,
        "not_found": not_found,
    }


# The detectors, in the order of THRESHOLDS.


def not_green(share: dict[str, float]) -> float:
    """The share of check-ins with main pending or red; unknown is neither."""
    return round(sum(share.get(state, 0) for state in ("pending", "red", "advisory")), 9)


def _numbers(values: Any) -> str:
    return ", ".join(str(value) for value in sorted(values, key=lambda value: int(value)))


def detect(numbers: dict[str, Any], sources: Sources, window: Window) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    over = f"over {window.days} day{'s' * (window.days != 1)}"
    within = f"in {window.days} day{'s' * (window.days != 1)}"

    def fire(detector: str, line: str) -> None:
        findings.append({"detector": detector, "line": line})

    merged = numbers["merged"]
    line_stage = merged["stages"]["merge_line"]
    if line_stage and (
        (line_stage["share"] or 0) > THRESHOLDS["merge_line_share"]
        or line_stage["p90"] > THRESHOLDS["merge_line_p90_hours"]
    ):
        fire(
            "merge_line",
            f"merge line {line_stage['share']:.0%} of the total time, 90th percentile"
            f" {line_stage['p90']:.1f} h, {over} (pull requests {_numbers(merged['timed'])})",
        )

    reasons: dict[str, list[tuple[float, str, str]]] = {}
    for note in sources.hand_backs:
        if window.holds(note[0]):
            reasons.setdefault(NUMBERED.sub("#", note[2]), []).append(note)
    for notes in reasons.values():
        if len(notes) >= THRESHOLDS["hand_back_repeats"]:
            latest = max(notes)[2]
            fire(
                "hand_back_reason",
                f"hand-back reason repeated {len(notes)} times {within}: {plain(latest)}"
                f" (pull requests {_numbers({number for _, number, _ in notes})})",
            )

    checks: dict[str, set[str]] = {}
    for moment, number, check in sources.lost:
        if window.holds(moment):
            checks.setdefault(check, set()).add(number)
    for check, count in numbers["quality"]["lost_checks"].items():
        if count >= THRESHOLDS["repeats"]:
            fire(
                "lost_check",
                f"lost check repeated {count} times {within}: {plain(check)}"
                f" (pull requests {_numbers(checks[check])})",
            )
    for job, count in numbers["quality"]["flaky"].items():
        if count >= THRESHOLDS["repeats"]:
            fire("flaky_test", f"flaky test repeated {count} times {within}: {plain(job)}")
    causes: dict[str, list[str]] = {}
    for episode in numbers["main"]["red_episodes"]:
        causes.setdefault(episode["cause"], []).append(episode["since"])
    for cause, since in causes.items():
        if len(since) >= THRESHOLDS["repeats"]:
            fire(
                "red_main_cause",
                f"red main cause repeated {len(since)} times {within}: {plain(cause)}"
                f" (since {', '.join(since)})",
            )

    sessions = numbers["sessions"]
    waited = [
        question
        for question in sessions["questions"]
        if question["waited_minutes"] > THRESHOLDS["question_minutes"]
    ]
    if waited:
        fire(
            "question_wait",
            f"questions to the owner waited over {THRESHOLDS['question_minutes']:.0f} min: "
            + ", ".join(f"{q['session']} ({q['waited_minutes']} min)" for q in waited),
        )
    idle = {
        name: found
        for name, found in sessions["idle"].items()
        if found["idle_share"] > THRESHOLDS["idle_share"]
    }
    if idle:
        fire(
            "idle_sessions",
            f"sessions idle more than {THRESHOLDS['idle_share']:.0%} of their life {over}: "
            + ", ".join(
                f"{name} ({found['idle_share']:.0%} of {found['hours']:.1f} h)"
                for name, found in idle.items()
            ),
        )

    runs = numbers["runs"]
    if (runs["ci_runs_per_merge"] or 0) > THRESHOLDS["ci_runs_per_merge"]:
        fire(
            "ci_runs_per_merge",
            f"CI runs per merged pull request {runs['ci_runs_per_merge']:.2f} {over}"
            f" ({runs['ci_runs']} runs, {len(merged['numbers'])} merges)",
        )
    if (runs["cancelled_share"] or 0) > THRESHOLDS["cancelled_share"]:
        fire(
            "cancelled_share",
            f"cancelled runs {runs['cancelled_share']:.0%} {over}"
            f" ({runs['cancelled']} of {runs['completed']} runs:"
            f" {_numbers(runs['cancelled_runs'])})",
        )

    # Without local workers (built in the cloud) there is nothing to compare with.
    workers = sessions["kinds"].get("ticket", {}).get("hours", 0.0)
    for kind, found in sessions["kinds"].items():
        if (
            workers
            and kind != "ticket"
            and found["hours"] > THRESHOLDS["kind_hours_over_workers"] * workers
        ):
            fire(
                "session_kind_hours",
                f"session kind {kind} used {found['hours']:.1f} session-hours {over},"
                f" {found['hours'] / workers:.1f} times the workers' {workers:.1f}",
            )

    shares: dict[str, list[dict[str, Any]]] = {}
    for share in runs["job_shares"]:
        shares.setdefault(share["job"], []).append(share)
    for job, found in shares.items():
        worst = max(share["share"] for share in found)
        which = (
            f"{worst:.0%} of run {found[0]['run']}"
            if len(found) == 1
            else f"up to {worst:.0%}, runs {_numbers([share['run'] for share in found])}"
        )
        fire(
            "job_share",
            f"one job over {THRESHOLDS['job_share']:.0%} of its run's time: {plain(job)} ({which})",
        )

    main = numbers["main"]
    share = not_green(main["share"])
    if main["check_ins"] and share > THRESHOLDS["main_not_green_share"]:
        fire(
            "main_not_green",
            f"main pending or red in {share:.0%} of {main['check_ins']} check-ins {over}",
        )

    machine = numbers["machine"]
    if swapped := machine.get("swap_out_with_memory"):
        worst = max(swapped, key=lambda swap: swap["mb_per_second"])
        fire(
            "swap_out",
            f"swap-out {worst['mb_per_second']:.0f} MB a second while"
            f" {worst['available_gb']:.1f} GB of memory was available, at {worst['at']}"
            f" ({len(swapped)} samples {over})",
        )
    system = machine.get("system_cpu_share")
    if system and system["max_hour"] > THRESHOLDS["system_cpu_share"]:
        fire(
            "system_cpu",
            f"system CPU {system['max_hour']:.0%} of the whole for the hour to {system['hour_to']}",
        )
    if waiting := machine.get("load_from_waiting"):
        fire(
            "load_over_pressure",
            f"load {waiting['load_percent']:.0f}% of the processors against CPU pressure"
            f" {waiting['cpu_pressure']:.0f}% for the hour to {waiting['hour_to']}:"
            " load that comes from waiting (swapping, blocked processes)",
        )
    for name, group in machine["groups"].items():
        if group["max_hour_cores"] > THRESHOLDS["group_cores"]:
            fire(
                "group_cores",
                f"{plain(name)} used {group['max_hour_cores']:.1f} cores on average for the hour to"
                f" {group['hour_to']}",
            )
    return findings


def measure(days: int, until: float) -> dict[str, Any]:
    long = Window(until - days * 86400, until, days)
    day = Window(until - 86400, until, 1)
    sources = read_sources(long.since, until)
    numbers = window_numbers(long, sources)
    return {
        "last_day": window_numbers(day, sources),
        "last_days": numbers,
        "findings": detect(numbers, sources, long),
    }


# The report.


def _hours_text(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f} h"


def _number(value: float | None, form: str = "{:.2f}") -> str:
    return "–" if value is None else form.format(value)


def _stage(stage: str, key: str) -> Any:
    return lambda window: _hours_text((window["merged"]["stages"][stage] or {}).get(key))


def _workflow(name: str) -> Any:
    def text(window: dict[str, Any]) -> str:
        found = window["runs"]["workflows"][name]
        if not found:
            return "–"
        return (
            f"{found['runs']} runs, median {found['median_minutes']:.1f} min,"
            f" 90th {found['p90_minutes']:.1f} min"
        )

    return text


def _kind_rows(windows: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    kinds = sorted({kind for window in windows for kind in window["sessions"]["kinds"]})
    rows = []
    for kind in kinds:

        def text(window: dict[str, Any], kind: str = kind) -> str:
            found = window["sessions"]["kinds"].get(kind)
            if not found:
                return "–"
            return (
                f"{found['sessions']} sessions, median life {found['median_lifetime_hours']:.1f} h,"
                f" {found['hours']:.1f} session-hours, {found['idle_share']:.0%} idle"
            )

        rows.append((f"{kind} sessions", text))
    return rows


def _table(
    title: str, rows: list[tuple[str, Any]], windows: list[dict[str, Any]], days: int
) -> list[str]:
    lines = [f"## {title}", "", f"| | last day | last {days} days |", "| --- | --- | --- |"]
    for label, value in rows:
        lines.append(f"| {label} | " + " | ".join(value(window) for window in windows) + " |")
    return [*lines, ""]


def _counts(found: dict[str, int]) -> str:
    return (
        "; ".join(
            f"{key} {count}" for key, count in sorted(found.items(), key=lambda item: -item[1])
        )
        or "none"
    )


def markdown(measured: dict[str, Any]) -> str:
    day, long = measured["last_day"], measured["last_days"]
    windows = [day, long]
    days = long["days"]
    lines = [
        f"# Workflow metrics, {long['until'][:10]}",
        "",
        f"From {long['since']} to {long['until']}: the last day and the last {days} days.",
        "",
        "## Findings",
        "",
    ]
    lines += [f"- {finding['line']}" for finding in measured["findings"]] or ["None."]
    lines.append("")
    lines += _table(
        "Merged pull requests",
        [
            ("merged", lambda w: str(len(w["merged"]["numbers"]))),
            ("merges per hour", lambda w: _number(w["merged"]["per_hour"])),
            *(
                (f"{name}, {key}", _stage(stage, key))
                for stage, name in (
                    ("build", "build"),
                    ("review", "review"),
                    ("merge_line", "merge line"),
                    ("total", "total"),
                )
                for key in ("median", "p75", "p90")
            ),
            *(
                (
                    f"{name}, share",
                    lambda w, stage=stage: _number(
                        (w["merged"]["stages"][stage] or {}).get("share"), "{:.0%}"
                    ),
                )
                for stage, name in (
                    ("build", "build"),
                    ("review", "review"),
                    ("merge_line", "merge line"),
                )
            ),
        ],
        windows,
        days,
    )
    lines += ["## Longest gaps on main", ""]
    lines += [
        f"- {gap['from']} to {gap['until']}{' and on' if gap['open'] else ''},"
        f" {gap['hours']:.1f} h: {gap['wake']}"
        for gap in long["merged"]["gaps"]
    ] or ["None."]
    lines.append("")
    lines += _table(
        "Runs",
        [
            *((name, _workflow(name)) for name in ("CI", "E2E", "Images")),
            (
                "slowest job",
                lambda w: "–"
                if not w["runs"]["slowest_job"]
                else "{workflow}: {job}, run {run}, {minutes:.1f} min".format(
                    **w["runs"]["slowest_job"]
                ),
            ),
            ("CI runs per merged pull request", lambda w: _number(w["runs"]["ci_runs_per_merge"])),
            (
                "cancelled",
                lambda w: f"{w['runs']['cancelled']} of {w['runs']['completed']}"
                f" ({_number(w['runs']['cancelled_share'], '{:.0%}')})",
            ),
            ("runner minutes", lambda w: f"{w['runs']['runner_minutes']:.0f}"),
        ],
        windows,
        days,
    )
    lines += _table(
        "Main",
        [
            ("check-ins", lambda w: str(w["main"]["check_ins"])),
            *(
                (state, lambda w, state=state: _number(w["main"]["share"].get(state, 0), "{:.0%}"))
                for state in ("green", "pending", "red", "advisory")
            ),
            ("reasons", lambda w: _counts(w["main"]["reasons"])),
            (
                "red episodes",
                lambda w: "; ".join(
                    f"{red['cause']} since {red['since']}, {red['hours']:.1f} h"
                    + (" and on" if red["ongoing"] else "")
                    for red in w["main"]["red_episodes"]
                )
                or "none",
            ),
        ],
        windows,
        days,
    )
    lines += _table(
        "Model turns",
        [
            *(
                (
                    label,
                    lambda w, key=key: f"{w['turns'][key]}"
                    f" ({_number(w['turns']['per_merge'][key])} per merge)",
                )
                for key, label in (
                    ("nudges", "reviewer nudges"),
                    ("status_checks", "status checks"),
                    ("finish_messages", "finish messages"),
                    ("wakes", "orchestrator check-ins"),
                )
            ),
            ("hand-backs", lambda w: _counts(w["turns"]["hand_backs"])),
        ],
        windows,
        days,
    )
    lines += _table(
        "Sessions",
        [
            *_kind_rows(windows),
            (
                "questions to the owner",
                lambda w: ", ".join(
                    f"{q['session']} {q['waited_minutes']} min" for q in w["sessions"]["questions"]
                )
                or "none",
            ),
            ("usage-limit stops", lambda w: str(w["sessions"]["limit_stops"])),
        ],
        windows,
        days,
    )

    def machine(key: str, form: str, field: str = "max_hour") -> Any:
        return lambda w: _number((w["machine"].get(key) or {}).get(field), form)

    def group(name: str) -> Any:
        return lambda w: _number(
            (w["machine"]["groups"].get(name) or {}).get("max_hour_cores"), "{:.1f} cores"
        )

    names = sorted({name for window in windows for name in window["machine"]["groups"]})
    lines += _table(
        "Machine",
        [
            ("swap-in, highest", machine("swap_in_mb_per_second", "{:.1f} MB/s", "max")),
            ("swap-out, highest", machine("swap_out_mb_per_second", "{:.1f} MB/s", "max")),
            ("available memory, lowest", machine("available_gb", "{:.1f} GB", "min")),
            ("system CPU, highest hour", machine("system_cpu_share", "{:.0%}")),
            ("load per processor, highest hour", machine("load_percent", "{:.0f}%")),
            ("CPU pressure, highest hour", machine("cpu_pressure", "{:.0f}%")),
            *((f"{name}, highest hour", group(name)) for name in names),
        ],
        windows,
        days,
    )
    lines += _table(
        "Quality",
        [
            (
                "send-back rounds per review",
                lambda w: _number(w["quality"]["send_back_rounds_per_review"]),
            ),
            ("failed scratch runs", lambda w: str(w["quality"]["failed_scratch_runs"])),
            ("flaky tests", lambda w: _counts(w["quality"]["flaky"])),
            ("lost checks", lambda w: _counts(w["quality"]["lost_checks"])),
            ("push refusals", lambda w: w["quality"]["push_refusals"]),
            ("weaker-tests items", lambda w: str(w["quality"]["weaker_tests_items"])),
            ("hunt findings", lambda w: str(w["quality"]["hunt_findings"])),
            ("reverts", lambda w: ", ".join(str(n) for n in w["quality"]["reverts"]) or "none"),
        ],
        windows,
        days,
    )
    missing = sorted(set(day["not_found"]) | set(long["not_found"]))
    if missing:
        lines += [f"Not found: {'; '.join(missing)}.", ""]
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as output:
        output.write(text)
    os.chmod(output.name, 0o644)
    os.replace(output.name, path)


def daily() -> int:
    """The report of the day and its findings for the wake, once a day. The tick starts it
    detached; a second run leaves while one runs, and one that failed is tried again by a later
    tick, half an hour on."""
    os.environ["WORKFLOW_BACKGROUND_GITHUB"] = "1"
    config = workflow_config.current()
    moment = now()
    date = datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%d")
    relative = ANALYSES / f"metrics-{date}.md"
    STATE.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("the daily metrics run before this one is still running")
            return 0
        if (config.main / relative).exists():
            return 0
        try:
            tried_date, tried_at = TRIED.read_text().split()
            if tried_date == date and moment - int(tried_at) < RETRY_SECONDS:
                return 0
        except OSError, ValueError:
            pass
        TRIED.write_text(f"{date} {moment}\n")
        from merge_gate import Refused

        try:
            measured = measure(7, moment)
        except (Refused, KeyError, TypeError, ValueError, OSError) as error:
            print(f"the daily metrics could not be computed: {error}", file=sys.stderr)
            return 1
        lines = [finding["line"] for finding in measured["findings"]]
        try:
            noted = NOTED.read_text().strip() == date
        except OSError:
            noted = False
        if lines and not noted:
            # Before the report: a report written is a day done, and its findings must not be lost.
            _write(
                config.state() / "metrics-findings.pending" / f"{date}.json",
                json.dumps({"report": str(relative), "lines": lines}),
            )
            # The wake may tell them before a report that fails is tried again.
            NOTED.write_text(f"{date}\n")
        _write(config.main / relative, markdown(measured))
        TRIED.unlink(missing_ok=True)
        print(f"wrote {relative}: {len(lines)} findings")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="workflow-metrics",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=7, help="the longer window, in days (7)")
    parser.add_argument("--json", action="store_true", help="the numbers as JSON")
    parser.add_argument("--sample", action="store_true", help="add one sample of the machine")
    parser.add_argument(
        "--sessions",
        choices=("-", "unread"),
        help="with --sample: the sessions on standard input, or none could be read",
    )
    parser.add_argument("--daily", action="store_true", help="the tick's daily run")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")
    try:
        if args.sample:
            return sample(args.sessions)
        if args.daily:
            return daily()
        from merge_gate import Refused

        try:
            measured = measure(args.days, now())
        except (Refused, KeyError, TypeError, ValueError) as error:
            print(f"the metrics could not be computed: {error}", file=sys.stderr)
            return 1
    except workflow_config.ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(measured, indent=2))
    else:
        print(markdown(measured), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
