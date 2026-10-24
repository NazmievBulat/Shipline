"""Where the workflow's time goes, and which stage is the bottleneck now: one static page.

    flow-page [--out FOLDER] [--json]   one pass: reads every source and writes FOLDER/index.html
                                        (~/.local/state/workflow/flow-page/site); --json prints
                                        the numbers too
    flow-page --serve PORT [--out FOLDER]
                                        serves the page's folder on PORT, bound to the address
                                        the machine's local settings name (127.0.0.1 if none)
    flow-page --settings                prints "<address> <port>" when the local settings name
                                        where the page is served, nothing when they do not

The local settings are `.github/runner/settings.sh` of the main checkout, read as Bash reads
them: `flow_page_address` (an IP address of this machine, never one for every address) and
`flow_page_port`. Naming either serves the page; the other then takes its default, 127.0.0.1 or
8765. `install-flow-page-timer` runs a pass every 10 minutes and, when they are named, serves it.

What a pass reads: GitHub through the shared budget as the background client `flow-page`, so it
reads nothing while the budget is below its reserve (one GraphQL read for the open pull requests
with their timelines, one search for the ones merged in the last 24 hours, the timelines of
those not read before, the day's runs in pages and the jobs of at most 40 finished run attempts
not read before); the merge line as the gate saved it (`merge-train.json`); the tick's journal;
the wake log; and one sample of each machine `shipline.json` names, kept in `machines.json` for
24 hours. Merged timelines and finished attempts' jobs never change: each is read once and kept
while it is in the window. No model, and nothing is written to GitHub. A source that cannot be
read is named on the page with why; the rest of the page is written all the same.
"""

import argparse
import fcntl
import http.server
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections import Counter
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import priority
import workflow_config
from github_budget import Limited, call
from workflow_metrics import (
    NUMBERED,
    PROC,
    ROUND,
    STATE,
    CheckIn,
    _minutes,
    _stages,
    _summary,
    instant,
    iso,
    now,
    plain,
    read_wake_log,
)

FOLDER = STATE / "flow-page"
SITE = FOLDER / "site"
# Merged pull requests' timelines and finished run attempts' jobs, each read once, in
# `cache/<owner>/<name>/`: every repository on the machine shares the state folder.
CACHE = FOLDER / "cache"
MACHINES = FOLDER / "machines.json"
LOCK = FOLDER / "lock"
HOUR = 3600
WINDOW = 24 * HOUR
# Job reads a pass makes at most; the rest wait for the next pass.
JOB_READS = 40
CLIENT = "flow-page"
GITHUB_SECONDS = 30
JOURNAL_SECONDS = 30
SAMPLE_SECONDS = 20
SETTINGS_SECONDS = 5
DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8765
# The messages of each kind a gap names, the most frequent first. A message is counted with its
# numbers and heads aside (`NUMBERED`), a head of letters alone too.
GAP_MESSAGES = 10
HEAD = re.compile(r"\b[0-9a-f]{7,40}\b")
# An absolute path in a message (a traceback's frame) keeps only its last part: the page is served.
ABSOLUTE = re.compile(r"(?<![\w./:])/(?:[\w.@+-]+/)+")
# What of a line read from GitHub, the journal or the wake log reaches the page: one line, no
# control characters, at most this long. The page escapes it.
WORDS_LENGTH = 120
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
TITLE_LENGTH = 100

STAGES = ("waiting for a start", "being built", "in review", "merge line")
# Tickets waiting for a start wait for the owner's word: never the bottleneck.
WORKING = STAGES[1:]
# The tick, as `install-timer` installs it.
TICK_UNIT = "workflow-tick.service"
# One sample of a machine: its load, its processors and its memory.
SAMPLE = "cat /proc/loadavg; nproc; grep -E '^Mem(Total|Available):' /proc/meminfo"
# The same on this machine, of the `/proc` the metrics read.
LOCAL_SAMPLE = 'cat "$1/loadavg"; nproc; grep -E \'^Mem(Total|Available):\' "$1/meminfo"'
THIS_MACHINE = workflow_config.Machine("this machine")

SOURCES = {
    "open pull requests": "GitHub, open pull requests",
    "merge train": "merge-train.json",
    "merged pull requests": "GitHub, merged pull requests",
    "journal": "the tick's journal",
    "wake log": "wake.log",
    "runs": "GitHub Actions",
    "machines": "machines.json",
}

OPEN = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title isDraft createdAt
        labels(first: 50) { nodes { name } }
        timelineItems(
          last: 100
          itemTypes: [LABELED_EVENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT]
        ) {
          nodes {
            __typename
            ... on LabeledEvent { createdAt label { name } }
            ... on ReadyForReviewEvent { createdAt }
            ... on ConvertToDraftEvent { createdAt }
          }
        }
        reviews(last: 100) { nodes { body submittedAt } }
      }
    }
  }
}"""

SEARCH = """query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number mergedAt } }
  }
}"""

MERGED_FIELDS = """fragment Merged on PullRequest {
  number createdAt mergedAt
  timelineItems(first: 100, itemTypes: [LABELED_EVENT, READY_FOR_REVIEW_EVENT]) {
    nodes {
      __typename
      ... on LabeledEvent { createdAt label { name } }
      ... on ReadyForReviewEvent { createdAt }
    }
  }
  reviews(first: 100) { nodes { body submittedAt } }
}"""
# Timelines asked in one read at most.
TIMELINES_A_READ = 50


class Unread(Exception):
    """A source could not be read; its message says why, in plain words."""


class SettingsError(Exception):
    """The local settings name where to serve the page in a way that cannot be used."""


# Reading.


def _private(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def _save(path: Path, value: Any, mode: int = 0o600) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as output:
        json.dump(value, output)
    os.chmod(output.name, mode)
    os.replace(output.name, path)


def _github(*arguments: str) -> str:
    try:
        done = call(CLIENT, *arguments, timeout=GITHUB_SECONDS)
    except Limited:
        raise Unread("GitHub budget low: paused") from None
    except subprocess.TimeoutExpired:
        raise Unread(f"GitHub did not answer in {GITHUB_SECONDS} s") from None
    except OSError as error:
        raise Unread(f"gh cannot be run: {error.strerror}") from None
    if done.returncode:
        raise Unread("GitHub: " + plain(done.stderr.strip() or done.stdout.strip()))
    return done.stdout


def _graphql(query: str, **fields: str) -> dict[str, Any]:
    arguments = ["api", "graphql", "-f", f"query={query}"]
    for key, value in fields.items():
        arguments += ["-f", f"{key}={value}"]
    try:
        answer = json.loads(_github(*arguments))
    except ValueError:
        raise Unread("GitHub answered with no JSON") from None
    if answer.get("errors") or not isinstance(answer.get("data"), dict):
        raise Unread("GitHub: the query was refused")
    return answer["data"]


def open_pulls(repository: str) -> list[dict[str, Any]]:
    owner, name = repository.split("/")
    pulls: list[dict[str, Any]] = []
    after: dict[str, str] = {}
    while True:
        page = _graphql(OPEN, owner=owner, name=name, **after)["repository"]["pullRequests"]
        pulls.extend(node for node in page["nodes"] if node)
        if not page["pageInfo"]["hasNextPage"]:
            return pulls
        after = {"after": page["pageInfo"]["endCursor"]}


def _cache(repository: str, kind: str) -> Path:
    for folder in (FOLDER, CACHE, CACHE / repository.split("/")[0]):
        _private(folder)
    return _private(CACHE / repository / kind)


def merged_pulls(
    repository: str, since: float, until: float
) -> tuple[list[dict[str, Any]], str | None]:
    """The pull requests merged in the window, with their timelines: those not read before are
    read, and those no longer in the window are dropped. Returns why some timelines were not
    read, when they were not: the others are counted all the same."""
    words = f"repo:{repository} is:pr is:merged base:main merged:{iso(since)}..{iso(until)}"
    merged = _cache(repository, "merged")
    numbers: list[int] = []
    after: dict[str, str] = {}
    while True:
        page = _graphql(SEARCH, q=words, **after)["search"]
        numbers += [int(node["number"]) for node in page["nodes"] if node]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = {"after": page["pageInfo"]["endCursor"]}
    found: dict[int, dict[str, Any]] = {}
    for number in numbers:
        try:
            found[number] = json.loads((merged / f"{number}.json").read_text())
        except OSError, ValueError:
            pass
    missing = [number for number in numbers if number not in found]
    owner, name = repository.split("/")
    error = None
    for start in range(0, len(missing), TIMELINES_A_READ):
        asked = missing[start : start + TIMELINES_A_READ]
        query = (
            "query($owner: String!, $name: String!) {\n  repository(owner: $owner, name: $name) {\n"
            + "".join(f"    p{n}: pullRequest(number: {n}) {{ ...Merged }}\n" for n in asked)
            + "  }\n}\n"
            + MERGED_FIELDS
        )
        try:
            nodes = _graphql(query, owner=owner, name=name)["repository"]
        except Unread as refused:
            error = str(refused)
            continue
        for number in asked:
            node = nodes.get(f"p{number}")
            if node and node.get("mergedAt"):
                found[number] = node
                _save(merged / f"{number}.json", node)
    kept = {f"{number}.json" for number in numbers}
    for path in merged.iterdir():
        if path.name not in kept:
            path.unlink(missing_ok=True)
    pulls = sorted(found.values(), key=lambda pull: instant(pull["mergedAt"]))
    if error:
        error = f"{len(numbers) - len(found)} timelines not read: {error}"
    return pulls, error


def workflow_runs(repository: str, since: float, until: float) -> tuple[list[dict[str, Any]], bool]:
    """The runs of the repository's own branches created in the window, in pages of 100, and
    whether GitHub's limit of 1,000 cut the list short."""
    path = f"repos/{repository}/actions/runs"
    created = f"{iso(since)}..{iso(until)}"
    listed: list[dict[str, Any]] = []
    total = 0
    page = 1
    while True:
        query = urlencode({"created": created, "per_page": 100, "page": page})
        try:
            answer = json.loads(_github("api", f"{path}?{query}"))
        except ValueError:
            raise Unread("GitHub answered with no JSON") from None
        total = answer.get("total_count", 0)
        found = answer.get("workflow_runs") or []
        listed += found
        if not found or len(listed) >= min(total, 1000):
            break
        page += 1

    def own(run: dict[str, Any]) -> bool:
        # A fork's pull request runs its own copy of the workflows and names its runs and jobs.
        head = (run.get("head_repository") or {}).get("full_name") or ""
        return head.casefold() == repository.casefold()

    runs = [
        run
        for run in {run["id"]: run for run in listed}.values()
        if own(run) and since <= instant(run["created_at"]) < until
    ]
    return sorted(runs, key=lambda run: run["id"]), total > 1000


def _attempt_jobs(
    repository: str, run: dict[str, Any], reads: int
) -> tuple[list[dict[str, Any]] | None, int]:
    """Every job of a run's attempt, in pages of 100, in at most `reads` reads: None when they
    were not all read. Returns the reads it made too."""
    path = f"repos/{repository}/actions/runs/{run['id']}/attempts/{run.get('run_attempt') or 1}"
    jobs: list[dict[str, Any]] = []
    for page in range(1, reads + 1):
        try:
            answer = json.loads(
                _github("api", f"{path}/jobs?{urlencode({'per_page': 100, 'page': page})}")
            )
        except ValueError:
            raise Unread("GitHub answered with no JSON") from None
        found = answer.get("jobs") or []
        jobs += found
        if not found or len(jobs) >= answer.get("total_count", len(jobs)):
            return jobs, page
    return None, reads


def read_jobs(repository: str, runs: list[dict[str, Any]]) -> tuple[int, str | None]:
    """The jobs of each finished run attempt, from what was read before or from GitHub, at most
    JOB_READS reads a pass, the newest first. Returns how many runs' jobs are not read yet, and
    why reading stopped when GitHub refused."""
    folder = _cache(repository, "jobs")
    finished = [run for run in runs if run.get("status") == "completed"]
    keys = {run["id"]: f"{run['id']}-{run.get('run_attempt') or 1}.json" for run in finished}
    reads = 0
    unread = 0
    error = None
    for run in sorted(finished, key=lambda run: run["created_at"], reverse=True):
        cached = folder / keys[run["id"]]
        try:
            run["jobs"] = json.loads(cached.read_text())
            continue
        except OSError, ValueError:
            pass
        jobs = None
        if error is None:
            try:
                jobs, used = _attempt_jobs(repository, run, JOB_READS - reads)
                reads += used
            except Unread as refused:
                error = str(refused)
        if jobs is None:
            unread += 1
            continue
        run["jobs"] = jobs
        _save(cached, jobs)
    kept = set(keys.values())
    for path in folder.iterdir():
        if path.name not in kept:
            path.unlink(missing_ok=True)
    return unread, error


def read_train(config: workflow_config.Config) -> Any:
    """The merge line as the gate last saved it, read without its lock: the gate writes it whole.
    A missing file is not an empty line (`Train` reads it as one): it may be gone a moment while
    the tick runs."""
    from merge_gate import Train

    if not (config.state() / "merge-train.json").exists():
        raise Unread("not found")
    return Train(config.state())


def blocked_reason(train: Any, number: str) -> str | None:
    """Why the gate passes over a member at the line's front (`Train.front`)."""
    if number in train.suspended:
        return "suspended"
    if number in train.conflicts:
        return "conflicting"
    if number in train.unavailable:
        return "no reviewer that can act"
    if number in train.blocked and number not in train.refreshable:
        return "failed checks"
    return None


def read_journal(since: int, until: int) -> list[tuple[float, str]]:
    """What the tick wrote to its journal in the window, one message a line."""
    try:
        done = subprocess.run(
            [
                "journalctl",
                "--user",
                "--unit",
                TICK_UNIT,
                "--since",
                f"@{since}",
                "--until",
                f"@{until}",
                "--output",
                "json",
                "--output-fields",
                "MESSAGE",
                "--no-pager",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=JOURNAL_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        raise Unread("journalctl is not installed") from None
    except subprocess.TimeoutExpired:
        raise Unread(f"journalctl did not answer in {JOURNAL_SECONDS} s") from None
    if done.returncode:
        raise Unread(f"journalctl exited {done.returncode}")
    found = []
    for line in done.stdout.splitlines():
        try:
            entry = json.loads(line)
            moment = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
            message = entry["MESSAGE"]
        except ValueError, KeyError, TypeError:
            continue
        if isinstance(message, list):
            # The journal gives a message that is not text as its bytes.
            try:
                message = bytes(message).decode(errors="replace")
            except ValueError, TypeError:
                continue
        if isinstance(message, str):
            found.append((moment, message))
    return found


def _parse_sample(output: str) -> dict[str, Any] | None:
    lines = output.splitlines()
    try:
        load = float(lines[0].split()[0])
        processors = int(lines[1].strip())
        memory = {
            words[0].rstrip(":"): int(words[1])
            for words in (line.split() for line in lines[2:])
            if len(words) >= 2
        }
        return {
            "load": load,
            "processors": processors,
            "memory_total_kb": memory["MemTotal"],
            "memory_available_kb": memory["MemAvailable"],
        }
    except IndexError, KeyError, ValueError:
        return None


def sample_machine(machine: workflow_config.Machine) -> dict[str, Any] | None:
    if machine.address is None:
        command = ["sh", "-c", LOCAL_SAMPLE, "sample", str(PROC)]
    else:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "--",
            machine.address,
            SAMPLE,
        ]
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SAMPLE_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return _parse_sample(done.stdout) if done.returncode == 0 else None


def read_machines(config: workflow_config.Config, moment: int) -> list[dict[str, Any]]:
    """One sample of each machine added to those of the window, which are kept."""
    machines = config.machines or (THIS_MACHINE,)
    try:
        samples = json.loads(MACHINES.read_text())["samples"]
    except OSError, ValueError, KeyError, TypeError:
        samples = []
    kept = [
        sample
        for sample in samples
        if isinstance(sample, dict)
        and isinstance(sample.get("machine"), str)
        and isinstance(sample.get("at"), str)
        and _moment(sample["at"]) is not None
        and _moment(sample["at"]) >= moment - WINDOW
    ]
    for machine in machines:
        found = sample_machine(machine)
        if found:
            kept.append({"at": iso(moment), "machine": machine.name, **found})
    _private(FOLDER)
    _save(MACHINES, {"samples": kept})
    return kept


def _moment(text: str) -> float | None:
    try:
        return instant(text)
    except ValueError:
        return None


# Computing.


def nearest_rank(values: list[float], percent: int) -> float:
    data = sorted(values)
    return data[max(1, math.ceil(percent * len(data) / 100)) - 1]


def _spread(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "median": None, "p90": None}
    return {
        "count": len(values),
        "median": round(nearest_rank(values, 50), 2),
        "p90": round(nearest_rank(values, 90), 2),
    }


def _events(pull: dict[str, Any], kind: str, label: str | None = None) -> list[float]:
    return [
        instant(event["createdAt"])
        for event in (pull.get("timelineItems") or {}).get("nodes") or []
        if event
        and event.get("__typename") == kind
        and event.get("createdAt")
        and (label is None or (event.get("label") or {}).get("name") == label)
    ]


def _rounds(pull: dict[str, Any], result: str) -> list[float]:
    return [
        instant(review["submittedAt"])
        for review in (pull.get("reviews") or {}).get("nodes") or []
        if review
        and review.get("submittedAt")
        and (found := ROUND.match(review.get("body") or ""))
        and found[2] == result
    ]


def words(text: str, length: int = WORDS_LENGTH) -> str:
    return CONTROL.sub("?", re.sub(r"\s+", " ", text).strip())[:length]


def _title(pull: dict[str, Any]) -> str | None:
    title = pull.get("title")
    return words(title, TITLE_LENGTH) if isinstance(title, str) else None


def stage_of(pull: dict[str, Any], label: str, line: set[str]) -> tuple[str, float] | None:
    """A pull request's stage now and when it entered it."""
    created = instant(pull["createdAt"])
    ready = _events(pull, "ReadyForReviewEvent")
    if str(pull["number"]) in line:
        return "merge line", _line_entered(pull) or max(ready or [created])
    labelled = label in {node.get("name") for node in (pull.get("labels") or {}).get("nodes") or []}
    if not labelled:
        return ("waiting for a start", created) if pull.get("isDraft") else None
    if pull.get("isDraft"):
        entered = _events(pull, "LabeledEvent", label) + _events(pull, "ConvertToDraftEvent")
        return "being built", max(entered or [created])
    return "in review", max(ready or [created])


def _line_entered(pull: dict[str, Any]) -> float | None:
    """When a pull request joined the merge line, as the daily numbers take it
    (`workflow_metrics._stages`): its first passed round after the last send-back."""
    sent_back = _rounds(pull, "sent-back")
    return min(
        (moment for moment in _rounds(pull, "passed") if moment > max(sent_back, default=0)),
        default=None,
    )


def _age(hours: float | None) -> str:
    if hours is None:
        return "age unknown"
    return f"{round(hours * 60)} min" if hours < 1 else f"{hours:.0f} h"


def work_now(
    pulls: list[dict[str, Any]] | None, train: Any, label: str, moment: int
) -> dict[str, Any]:
    """The pull requests in each stage now: the merge line in the gate's order, the others the
    oldest first. Without the open pull requests, the line is the train's alone."""
    order = [str(number) for number in train.line()] if train is not None else []
    stages: dict[str, list[dict[str, Any]]] = {stage: [] for stage in STAGES}
    if pulls is None:
        stages["merge line"] = [
            {
                "number": int(number),
                "title": None,
                "hours": None,
                "priority": train.priorities.get(number, priority.UNLABELLED),
                "blocked": blocked_reason(train, number),
            }
            for number in order
        ]
        return stages
    for pull in pulls:
        found = stage_of(pull, label, set(order))
        if found is None:
            continue
        stage, entered = found
        number = str(pull["number"])
        stages[stage].append(
            {
                "number": pull["number"],
                "title": _title(pull),
                "hours": round((moment - entered) / HOUR, 2),
                "priority": priority.of((pull.get("labels") or {}).get("nodes")),
                "blocked": blocked_reason(train, number) if stage == "merge line" else None,
            }
        )
    for stage in STAGES[:-1]:
        stages[stage].sort(key=lambda item: -item["hours"])
    stages["merge line"].sort(key=lambda item: order.index(str(item["number"])))
    return stages


def bottleneck(stages: dict[str, list[dict[str, Any]]], unread: str | None = None) -> str:
    """The stage, among being built, in review and the merge line, that holds the most pull
    requests, the one with the older oldest on a tie. Without the open pull requests or the
    merge train no stage can be judged: `unread` says which was not read and why."""
    if unread is not None:
        return f"bottleneck unknown: {unread}"
    candidates = [
        (len(items), max((item["hours"] or 0) for item in items), stage)
        for stage, items in stages.items()
        if stage in WORKING and items
    ]
    if not candidates:
        return "no pull request is being built, reviewed or merged"
    count, _, stage = max(candidates, key=lambda candidate: candidate[:2])
    items = stages[stage]
    ages = [item["hours"] for item in items if item["hours"] is not None]
    line = f"{stage}: {count} pull request{'s' * (count != 1)} waiting"
    if ages:
        line += f", oldest {_age(max(ages))}"
    blocked = sum(1 for item in items if item["blocked"])
    if blocked:
        line += f", {blocked} blocked"
    return line


def merged_numbers(pulls: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Each stage's hours: opened to the first label, then the daily numbers' own stages
    (`workflow_metrics._stages`: build, review, merge line), which count only a pull request
    that was labelled and passed its review; and the share sent back at least once."""
    hours: dict[str, list[float]] = {stage: [] for stage in STAGES}
    sent_back = 0
    for pull in pulls:
        labelled = _events(pull, "LabeledEvent", label)
        if labelled:
            hours["waiting for a start"].append(
                max(0.0, min(labelled) - instant(pull["createdAt"])) / HOUR
            )
        if stages := _stages(pull, label):
            for stage, key in zip(STAGES[1:], ("build", "review", "merge_line"), strict=True):
                hours[stage].append(stages[key])
        sent_back += bool(_rounds(pull, "sent-back"))
    return {
        "numbers": [pull["number"] for pull in pulls],
        "stages": {stage: _spread(values) for stage, values in hours.items()},
        "sent_back_share": sent_back / len(pulls) if pulls else None,
    }


def _said(lines: list[tuple[float, str]], start: float, end: float) -> dict[str, int]:
    counted = Counter(
        words(ABSOLUTE.sub(".../", HEAD.sub("#", NUMBERED.sub("#", text))))
        for moment, text in lines
        if start <= moment < end
    )
    return dict(counted.most_common(GAP_MESSAGES))


def line_numbers(
    pulls: list[dict[str, Any]] | None,
    journal: list[tuple[float, str]] | None,
    check_ins: list[CheckIn] | None,
    ticks: list[tuple[float, str]] | None,
    since: float,
    until: float,
) -> dict[str, Any]:
    """Merges an hour, and every stretch of over an hour without one with what the journal and
    the wake log said meanwhile. The first stretch starts at the window's edge."""
    if pulls is None:
        return {"per_hour": None, "gaps": None}
    moments = sorted(
        moment for pull in pulls if since <= (moment := instant(pull["mergedAt"])) <= until
    )
    per_hour = [0] * 24
    for moment in moments:
        per_hour[min(23, int((moment - since) // HOUR))] += 1
    bounds = [since, *moments, until]
    gaps = []
    for index, (start, end) in enumerate(zip(bounds, bounds[1:], strict=False)):
        if end - start <= HOUR:
            continue
        gaps.append(
            {
                "from": iso(start),
                "until": iso(end),
                "hours": (end - start) / HOUR,
                "edge": index == 0,
                "open": index == len(bounds) - 2,
                "journal": None if journal is None else _said(journal, start, end),
                "wake": None if ticks is None else _said(ticks, start, end),
                "check_ins": None
                if check_ins is None
                else _summary([c for c in check_ins if start <= c.at < end]),
            }
        )
    return {
        "per_hour": [
            {"from": iso(since + hour * HOUR), "merges": count}
            for hour, count in enumerate(per_hour)
        ],
        "gaps": gaps,
    }


def _ci_row(
    name: str, durations: list[float], waits: list[float], conclusions: list[str]
) -> dict[str, Any]:
    middle = _spread(durations)
    wait = _spread(waits)
    return {
        "name": name,
        "median_minutes": middle["median"],
        "wait_median": wait["median"],
        "wait_p90": wait["p90"],
        "failures": conclusions.count("failure"),
        "cancelled": conclusions.count("cancelled"),
    }


def _slowest(row: dict[str, Any]) -> tuple[bool, float]:
    return row["median_minutes"] is None, -(row["median_minutes"] or 0)


def ci_numbers(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each workflow and each of its jobs, the slowest first: minutes, the wait for a runner,
    failures and cancellations."""
    workflows: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        workflows.setdefault(words(run.get("name") or "?"), []).append(run)
    rows = []
    for name, found in workflows.items():
        finished = [run for run in found if run.get("status") == "completed"]
        jobs: dict[str, dict[str, list[Any]]] = {}
        for run in finished:
            for job in run.get("jobs") or []:
                # A skipped job takes no time and waits for no runner.
                if job.get("conclusion") == "skipped":
                    continue
                into = jobs.setdefault(
                    words(job.get("name") or "?"),
                    {"durations": [], "waits": [], "conclusions": []},
                )
                into["conclusions"].append(job.get("conclusion") or "")
                ran = _minutes(job.get("started_at"), job.get("completed_at"))
                if ran is not None and ran >= 0:
                    into["durations"].append(ran)
                waited = _minutes(job.get("created_at"), job.get("started_at"))
                if waited is not None and waited >= 0:
                    into["waits"].append(waited)
        durations = [
            minutes
            for run in finished
            if (minutes := _minutes(run.get("run_started_at"), run.get("updated_at"))) is not None
        ]
        row = _ci_row(
            name,
            durations,
            [wait for job in jobs.values() for wait in job["waits"]],
            [run.get("conclusion") or "" for run in finished],
        )
        row["runs"] = len(finished)
        row["running"] = len(found) - len(finished)
        row["jobs"] = sorted(
            (
                {**_ci_row(job, **values), "runs": len(values["conclusions"])}
                for job, values in jobs.items()
            ),
            key=_slowest,
        )
        rows.append(row)
    return sorted(rows, key=_slowest)


def machine_numbers(
    config: workflow_config.Config, samples: list[dict[str, Any]], moment: int
) -> list[dict[str, Any]]:
    since = moment - WINDOW
    rows = []
    for machine in config.machines or (THIS_MACHINE,):
        own = sorted(
            (sample for sample in samples if sample["machine"] == machine.name),
            key=lambda sample: sample["at"],
        )
        hours: list[list[dict[str, Any]]] = [[] for _ in range(24)]
        for sample in own:
            hours[min(23, max(0, int((instant(sample["at"]) - since) // HOUR)))].append(sample)
        last = own[-1] if own else {}

        def share(sample: dict[str, Any]) -> float | None:
            if not sample.get("processors"):
                return None
            return sample["load"] / sample["processors"]

        rows.append(
            {
                "name": machine.name,
                "at": last.get("at"),
                "fresh": last.get("at") == iso(moment),
                "load": last.get("load"),
                "processors": last.get("processors"),
                "total_gb": _gb(last.get("memory_total_kb")),
                "available_gb": _gb(last.get("memory_available_kb")),
                "hours": [
                    {
                        "from": iso(since + index * HOUR),
                        "load_share": round(sum(shares) / len(shares), 3)
                        if (shares := [s for sample in hour if (s := share(sample)) is not None])
                        else None,
                        "available_gb": _gb(
                            min((sample["memory_available_kb"] for sample in hour), default=None)
                        ),
                    }
                    for index, hour in enumerate(hours)
                ],
            }
        )
    return rows


def _gb(kb: float | None) -> float | None:
    return None if kb is None else round(kb / 2**20, 2)


# The pass.


def _source(read: dict[str, Any], name: str, error: str | None = None) -> None:
    """A source as read now: the time it was read, and why it was not, when it was not."""
    read[name] = {"source": SOURCES[name], "read_at": iso(now()), "error": error}


def _cannot(error: Exception) -> str:
    """Why a file cannot be read, without its path: the page is served."""
    if isinstance(error, OSError) and error.strerror:
        return f"cannot be read: {error.strerror}"
    return f"cannot be read ({type(error).__name__})"


def measure(config: workflow_config.Config, moment: int) -> dict[str, Any]:
    since, until = moment - WINDOW, moment
    label = config.labels.workflow
    repository = config.repository
    read: dict[str, Any] = {}

    def attempt(name: str, reader: Any, *args: Any) -> Any:
        try:
            found = reader(*args)
        except Unread as error:
            _source(read, name, str(error))
            return None
        _source(read, name)
        return found

    pulls = attempt("open pull requests", open_pulls, repository)
    try:
        train = read_train(config)
        _source(read, "merge train")
    except Unread as error:
        train = None
        _source(read, "merge train", str(error))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError) as error:
        train = None
        _source(read, "merge train", _cannot(error))
    merged = attempt("merged pull requests", merged_pulls, repository, since, until)
    if merged:
        merged, partly = merged
        if partly:
            _source(read, "merged pull requests", partly)
    journal = attempt("journal", read_journal, since, until)
    check_ins, ticks, found = read_wake_log()
    _source(read, "wake log", None if found else "not found")
    listed = attempt("runs", workflow_runs, repository, since, until)
    runs, capped = listed if listed else ([], False)
    unread = 0
    if listed:
        unread, error = read_jobs(repository, runs)
        if error:
            _source(read, "runs", f"jobs: {error}")
    samples = read_machines(config, moment)
    _source(read, "machines")
    stages = work_now(pulls, train, label, moment)
    unknown = None
    if pulls is None:
        unknown = f"the open pull requests were not read ({read['open pull requests']['error']})"
    elif train is None:
        unknown = f"the merge train was not read ({read['merge train']['error']})"
    return {
        "now": iso(moment),
        "bottleneck": bottleneck(stages, unknown),
        "stages": {stage: {"items": items} for stage, items in stages.items()},
        "merged": merged_numbers(merged, label) if merged is not None else None,
        "line": line_numbers(
            merged,
            journal,
            check_ins if found else None,
            ticks if found else None,
            since,
            until,
        ),
        "ci": {"workflows": ci_numbers(runs), "unread_jobs": unread, "capped": capped}
        if listed
        else None,
        "machines": machine_numbers(config, samples, moment),
        "sources": read,
    }


def write_page(folder: Path, text: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=folder, delete=False, suffix=".tmp") as output:
        output.write(text)
    os.chmod(output.name, 0o644)
    os.replace(output.name, folder / "index.html")


def one_pass(folder: Path, printed: bool) -> int:
    import flow_page_html

    config = workflow_config.current()
    _private(FOLDER)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("the flow page's pass before this one is still running")
            return 0
        numbers = measure(config, now())
        write_page(folder, flow_page_html.render(numbers))
    if printed:
        print(json.dumps(numbers, indent=2))
    return 0


# Serving.


def served() -> tuple[str, int] | None:
    """Where the local settings say the page is served, or None when they name nowhere."""
    path = workflow_config.main_checkout() / workflow_config.RUNNER_SETTINGS
    if not path.is_file():
        return None
    try:
        done = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1" >/dev/null 2>&1; printf "%s\\n%s\\n" "${flow_page_address-}" '
                '"${flow_page_port-}"',
                "settings",
                str(path),
            ],
            env={key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ},
            capture_output=True,
            text=True,
            timeout=SETTINGS_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SettingsError(
            f"{workflow_config.RUNNER_SETTINGS} ran for more than {SETTINGS_SECONDS} seconds"
        ) from None
    address, port = (done.stdout.split("\n") + ["", ""])[:2]
    if not address and not port:
        return None
    if address:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise SettingsError(
                f"flow_page_address must be an IP address of this machine, not {plain(address)}"
            ) from None
        if parsed.is_unspecified:
            raise SettingsError(
                f"flow_page_address must name one address, not every one: {plain(address)}"
            )
    if port and not (re.fullmatch(r"[0-9]{1,5}", port) and 1 <= int(port) <= 65535):
        raise SettingsError(f"flow_page_port must be a port, 1 to 65535, not {plain(port)}")
    return address or DEFAULT_ADDRESS, int(port) if port else DEFAULT_PORT


class _Folder(http.server.SimpleHTTPRequestHandler):
    """The page's folder and nothing else: no listing, no log of who asked."""

    # A client that sends nothing is let go.
    timeout = 30

    def list_directory(self, path: Any) -> None:
        self.send_error(404)
        return None

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def serve(port: int, folder: Path) -> int:
    where = served()
    if where is None:
        raise SettingsError(
            f"{workflow_config.RUNNER_SETTINGS} names no flow_page_address or flow_page_port:"
            " nothing is served"
        )
    address = where[0]
    folder.mkdir(parents=True, exist_ok=True)
    _Server.address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with _Server((address, port), partial(_Folder, directory=str(folder))) as server:
        print(f"serving the flow page on {address} port {port}", flush=True)
        server.serve_forever()
    return 0


def _port(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,5}", text) or not 1 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError(f"a port is 1 to 65535, not {text}")
    return int(text)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="flow-page", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, help="the folder of the page")
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON too")
    parser.add_argument(
        "--serve", type=_port, metavar="PORT", help="serve the page's folder on PORT"
    )
    parser.add_argument("--settings", action="store_true", help="print where it is served")
    args = parser.parse_args()
    folder = args.out or SITE
    try:
        if args.settings:
            where = served()
            if where:
                print(*where)
            return 0
        if args.serve is not None:
            return serve(args.serve, folder)
        return one_pass(folder, args.json)
    except (workflow_config.ConfigError, SettingsError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
