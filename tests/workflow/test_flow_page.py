"""`flow-page` writes one static page: which stage holds the work now, and where its time goes."""

import fcntl
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from workflow_harness import Workflow

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "flow_page"
# 24 October 2026, 07:50 UTC.
NOW = 1792828200
AT = {"WORKFLOW_NOW": str(NOW), "TZ": "UTC"}
HOUR = 3600

# Answers what the page reads from GitHub with the fixtures in @FIXTURES@: the open pull requests,
# the search for the merged ones and their timelines, the day's runs in pages and each attempt's
# jobs. Every call is logged, one line each.
_FAKE_GH = """#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

fixtures = Path("@FIXTURES@")
arguments = sys.argv[1:]
with open("@STATE@/calls.log", "a") as log:
    log.write("gh " + " ".join(arguments).replace("\\n", " ") + "\\n")
if arguments[:2] == ["api", "graphql"]:
    query = next(word for word in arguments if word.startswith("query="))
    fields = dict(word.split("=", 1) for word in arguments[2:] if "=" in word)
    if "pullRequests(states: OPEN" in query:
        assert (fields["owner"], fields["name"]) == ("example-org", "example-app"), fields
        print(json.dumps(json.loads((fixtures / "open.json").read_text())[0]))
    elif "search(" in query:
        words = "repo:example-org/example-app is:pr is:merged base:main merged:"
        assert fields["q"].startswith(words), fields
        print(json.dumps(json.loads((fixtures / "merged-search.json").read_text())[0]))
    else:
        if (fixtures / "fail-timelines").exists():
            print("HTTP 502: Bad Gateway", file=sys.stderr)
            sys.exit(1)
        merged = json.loads((fixtures / "merged.json").read_text())
        asked = re.findall(r"p(\\d+): pullRequest\\(number: (\\d+)\\)", query)
        print(json.dumps({"data": {"repository": {
            f"p{alias}": merged[number] for alias, number in asked}}}))
    sys.exit(0)
path = arguments[-1]
runs = "repos/example-org/example-app/actions/runs"
asked = parse_qs(urlsplit(path).query)
if found := re.fullmatch(runs + r"/(\\d+)/attempts/(\\d+)/jobs", urlsplit(path).path):
    jobs = json.loads((fixtures / "jobs.json").read_text()).get(f"{found[1]}-{found[2]}", [])
    print(json.dumps({"total_count": len(jobs), "jobs": jobs}))
elif urlsplit(path).path == runs:
    assert asked["created"] and asked["per_page"] == ["100"], asked
    every = json.loads((fixtures / "runs.json").read_text())
    page = int(asked.get("page", ["1"])[0])
    listed = every[(page - 1) * 100 : page * 100]
    print(json.dumps({"total_count": len(every), "workflow_runs": listed}))
else:
    print(f"gh: could not answer {arguments}", file=sys.stderr)
    sys.exit(1)
"""

# The tick's journal: the fixture's lines from --since to --until, as `-o json` prints them.
_FAKE_JOURNALCTL = """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

arguments = sys.argv[1:]
with open("@STATE@/calls.log", "a") as log:
    log.write("journalctl " + " ".join(arguments) + "\\n")
since = int(arguments[arguments.index("--since") + 1].lstrip("@"))
until = int(arguments[arguments.index("--until") + 1].lstrip("@"))
for line in Path("@FIXTURES@/journal.jsonl").read_text().splitlines():
    if since <= int(json.loads(line)["__REALTIME_TIMESTAMP"]) // 10**6 < until:
        print(line)
"""

# Another machine: runner-1 answers with the fixture, any other address cannot be reached.
_FAKE_SSH = """#!/bin/bash
printf 'ssh' >> "@STATE@/calls.log"
printf ' %s' "$@" >> "@STATE@/calls.log"
echo >> "@STATE@/calls.log"
while [[ $1 != -- ]]; do shift; done
if [[ $2 == runner-1.example ]]; then
  cat "@FIXTURES@/runner-1.txt"
else
  echo "ssh: connect to host $2 port 22: Connection timed out" >&2
  exit 255
fi
"""


def _fixtures(workflow: Workflow) -> Path:
    """The fixtures, copied so that a test can change them."""
    folder = workflow.root / "fixtures"
    if not folder.exists():
        shutil.copytree(FIXTURES, folder)
    return folder


def _sources(workflow: Workflow) -> Path:
    folder = _fixtures(workflow)
    for tool, text in (("gh", _FAKE_GH), ("journalctl", _FAKE_JOURNALCTL), ("ssh", _FAKE_SSH)):
        workflow.fake(tool, text.replace("@FIXTURES@", str(folder)))
    train = workflow.repo / ".local" / "workflow" / "merge-train.json"
    train.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / "merge-train.json", train)
    workflow.wake_log.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / "wake.log", workflow.wake_log)
    for name in ("loadavg", "meminfo"):
        shutil.copy2(folder / "proc" / name, workflow.root / "proc" / name)
    return folder


def _state(workflow: Workflow) -> Path:
    return workflow.root / "home" / ".local" / "state" / "workflow" / "flow-page"


def _cache(workflow: Workflow, kind: str, repository: str = "example-org/example-app") -> Path:
    """What the page keeps of one repository: each repository on the machine has its own."""
    return _state(workflow) / "cache" / repository / kind


def _site(workflow: Workflow) -> Path:
    return _state(workflow) / "site" / "index.html"


def _pass(workflow: Workflow, *args: str) -> dict[str, Any]:
    done = workflow.run_script("flow-page", "--json", *args, env=AT)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _open(folder: Path) -> list[dict[str, Any]]:
    return json.loads((folder / "open.json").read_text())[0]["data"]["repository"]["pullRequests"][
        "nodes"
    ]


def _set_open(folder: Path, nodes: list[dict[str, Any]]) -> None:
    pages = json.loads((folder / "open.json").read_text())
    pages[0]["data"]["repository"]["pullRequests"]["nodes"] = nodes
    (folder / "open.json").write_text(json.dumps(pages))


def _empty_line(workflow: Workflow, folder: Path) -> None:
    """No merge line: the train is empty and its members are gone."""
    (workflow.repo / ".local" / "workflow" / "merge-train.json").write_text('{"waiting": []}')
    _set_open(
        folder, [node for node in _open(folder) if node["number"] not in (44, 45, 46, 48, 51)]
    )


# The bottleneck and the work in each stage.


def test_the_bottleneck_is_the_stage_holding_the_most_pull_requests_with_its_oldest_and_blocked(
    workflow: Workflow,
) -> None:
    _sources(workflow)

    numbers = _pass(workflow)

    assert numbers["bottleneck"] == "merge line: 5 pull requests waiting, oldest 30 h, 4 blocked"
    page = _site(workflow).read_text()
    # One line, at the top, before any section.
    assert page.index("merge line: 5 pull requests waiting, oldest 30 h, 4 blocked") < page.index(
        "<h2"
    )


def test_each_stage_holds_its_pull_requests_with_their_ages_and_priorities(
    workflow: Workflow,
) -> None:
    _sources(workflow)

    stages = _pass(workflow)["stages"]

    def items(stage: str) -> list[tuple[int, float, str, str | None]]:
        return [
            (item["number"], item["hours"], item["priority"], item["blocked"])
            for item in stages[stage]["items"]
        ]

    # A draft without the label waits for a start; its age is from its opening.
    assert items("waiting for a start") == [(40, 96, "high", None)]
    # A labelled draft is being built: from its label, or from its last return to draft.
    assert items("being built") == [(41, 3, "medium", None), (42, 2, "low", None)]
    # Ready and labelled: in review from its last ready. A ready one without the label is not
    # the workflow's.
    assert items("in review") == [(43, 1, "medium", None)]
    # The merge line in the gate's own order, each from its passed review as the daily numbers
    # take it (the first pass after the last send-back: a round passed again while it waits is
    # the line's time), blocked as the gate reads the line's front.
    assert items("merge line") == [
        (44, 3, "high", None),
        (46, 30, "medium", "failed checks"),
        (45, 18, "medium", "conflicting"),
        (48, 1, "medium", "no reviewer that can act"),
        (51, 0.5, "medium", "suspended"),
    ]


def test_tickets_waiting_for_a_start_are_shown_but_never_the_bottleneck(
    workflow: Workflow,
) -> None:
    folder = _sources(workflow)
    _empty_line(workflow, folder)
    waiting = _open(folder)[0]
    _set_open(folder, _open(folder) + [{**waiting, "number": 60 + number} for number in range(5)])

    numbers = _pass(workflow)

    assert len(numbers["stages"]["waiting for a start"]["items"]) == 6
    assert numbers["bottleneck"] == "being built: 2 pull requests waiting, oldest 3 h"


def test_on_a_tie_the_stage_whose_oldest_pull_request_is_older_is_the_bottleneck(
    workflow: Workflow,
) -> None:
    folder = _sources(workflow)
    _empty_line(workflow, folder)
    in_review = next(node for node in _open(folder) if node["number"] == 43)
    older = {
        **in_review,
        "number": 49,
        "timelineItems": {
            "nodes": [{"__typename": "ReadyForReviewEvent", "createdAt": "2026-10-23T21:50:00Z"}]
        },
    }
    _set_open(folder, _open(folder) + [older])

    numbers = _pass(workflow)

    # Two being built (oldest 3 h) and two in review (oldest 10 h).
    assert numbers["bottleneck"] == "in review: 2 pull requests waiting, oldest 10 h"


def test_with_nothing_in_flight_the_top_line_says_so(workflow: Workflow) -> None:
    folder = _sources(workflow)
    _empty_line(workflow, folder)
    _set_open(folder, [])

    assert _pass(workflow)["bottleneck"] == "no pull request is being built, reviewed or merged"


# The merged pull requests.


def test_the_merged_pull_requests_give_each_stage_s_median_and_90th_percentile(
    workflow: Workflow,
) -> None:
    _sources(workflow)

    merged = _pass(workflow)["merged"]

    assert merged["numbers"] == [30, 31, 32, 33]
    # The stages of the daily numbers (`workflow_metrics._stages`), and before them the wait
    # from opening to the first label; nearest rank. 33 was never labelled, so it has none; 32
    # was ready before its label, so its build ends at the label; 31's review ends at the first
    # pass after its send-back, and its round passed again counts to the merge line.
    assert merged["stages"] == {
        "waiting for a start": {"count": 3, "median": 1, "p90": 2},
        "being built": {"count": 3, "median": 2, "p90": 4},
        "in review": {"count": 3, "median": 3, "p90": 4},
        "merge line": {"count": 3, "median": 4, "p90": 4},
    }
    assert merged["sent_back_share"] == 0.25


def test_the_merged_timelines_are_read_once_and_pruned_to_the_window(workflow: Workflow) -> None:
    _sources(workflow)
    cache = _cache(workflow, "merged")
    cache.mkdir(parents=True)
    # Merged before the window: no longer in the search.
    (cache / "29.json").write_text(json.dumps({"number": 29}))
    # Another repository's, kept by its own pass.
    other = _cache(workflow, "merged", "example-org/other-app")
    other.mkdir(parents=True)
    (other / "12.json").write_text(json.dumps({"number": 12}))

    first = _pass(workflow)
    asked = [call for call in workflow.calls("gh api graphql") if "pullRequest(number:" in call]
    assert len(asked) == 1
    assert all(f"pullRequest(number: {number})" in asked[0] for number in (30, 31, 32, 33))
    assert sorted(path.name for path in cache.iterdir()) == [f"{n}.json" for n in (30, 31, 32, 33)]
    assert (other / "12.json").exists()

    second = _pass(workflow)

    asked = [call for call in workflow.calls("gh api graphql") if "pullRequest(number:" in call]
    assert len(asked) == 1
    assert second["merged"] == first["merged"]


def test_timelines_that_cannot_be_read_leave_the_rest_of_the_merged_ones_on_the_page(
    workflow: Workflow,
) -> None:
    folder = _sources(workflow)
    cache = _cache(workflow, "merged")
    cache.mkdir(parents=True)
    kept = json.loads((folder / "merged.json").read_text())["30"]
    (cache / "30.json").write_text(json.dumps(kept))
    (folder / "fail-timelines").touch()

    numbers = _pass(workflow)

    assert numbers["merged"]["numbers"] == [30]
    error = numbers["sources"]["merged pull requests"]["error"]
    assert error.startswith("3 timelines not read: GitHub: HTTP 502"), error
    assert (cache / "30.json").exists()


def test_github_is_read_once_for_the_open_ones_once_for_the_merged_and_the_runs_in_pages(
    workflow: Workflow,
) -> None:
    folder = _sources(workflow)
    runs = json.loads((folder / "runs.json").read_text())
    extra = [{**runs[4], "id": 300 + number} for number in range(150)]
    (folder / "runs.json").write_text(json.dumps(runs + extra))

    _pass(workflow)

    graphql = workflow.calls("gh api graphql")
    assert len([call for call in graphql if "pullRequests(states: OPEN" in call]) == 1
    assert len([call for call in graphql if "search(" in call]) == 1
    listed = [call for call in workflow.calls("gh api repos/") if "/actions/runs?" in call]
    assert len(listed) == 2
    assert "created=2026-10-23T07%3A50%3A00Z..2026-10-24T07%3A50%3A00Z" in listed[0]


# The merge line.


def test_the_merge_line_counts_merges_an_hour_and_each_gap_over_an_hour_with_what_was_said(
    workflow: Workflow,
) -> None:
    _sources(workflow)

    line = _pass(workflow)["line"]

    hours = line["per_hour"]
    assert len(hours) == 24
    assert hours[0]["from"] == "2026-10-23T07:50:00Z"
    assert [index for index, hour in enumerate(hours) if hour["merges"]] == [5, 12, 21, 22]
    gaps = [
        (gap["from"], gap["until"], round(gap["hours"], 2), gap["open"]) for gap in line["gaps"]
    ]
    # 05:00 to 06:00 is an hour, not over it.
    assert gaps == [
        ("2026-10-23T07:50:00Z", "2026-10-23T13:00:00Z", 5.17, False),
        ("2026-10-23T13:00:00Z", "2026-10-23T20:00:00Z", 7.0, False),
        ("2026-10-23T20:00:00Z", "2026-10-24T05:00:00Z", 9.0, False),
        ("2026-10-24T06:00:00Z", "2026-10-24T07:50:00Z", 1.83, True),
    ]
    first, second, third, last = line["gaps"]
    # The first gap starts at the window's edge: the merge before it is not read.
    assert first["edge"] and not third["edge"]
    assert first["journal"] == {
        "merge gate: waiting: main is red, # checks failed": 2,
        "Started workflow-tick.service - Workflow tick.": 1,
    }
    assert first["wake"] == {"review-# checks finished on #; reviewer nudged": 1}
    assert first["check_ins"] == "1 check-ins; main red 1; 5-hour usage up to 20%; up to 1 sessions"
    assert second["journal"] == {"Started workflow-tick.service - Workflow tick.": 1}
    assert second["wake"] == {} and second["check_ins"] == "no check-ins"
    assert third["journal"] == {
        "merge gate: waiting: the first member's prefix runs": 2,
        "Started workflow-tick.service - Workflow tick.": 1,
    }
    assert third["wake"] == {
        "review-# merge conflict on #; reviewer nudged": 2,
        "ticket-# idle # min waiting for its own job or turn; status check sent": 1,
    }
    # What the journal says reaches the page as text, never as markup.
    assert last["journal"] == {"merge gate: waiting: <b>no reviewer</b>": 1}
    page = _site(workflow).read_text()
    assert "1 × merge gate: waiting: &lt;b&gt;no reviewer&lt;/b&gt;" in page
    assert "<b>no reviewer" not in page
    assert workflow.calls("journalctl") == [
        f"journalctl --user --unit workflow-tick.service --since @{NOW - 24 * HOUR}"
        f" --until @{NOW} --output json --output-fields MESSAGE --no-pager --quiet"
    ]


# CI.


def test_ci_per_workflow_and_per_job_slowest_first(workflow: Workflow) -> None:
    _sources(workflow)

    ci = _pass(workflow)["ci"]

    # The fork's run and the run created before the window are not counted; a running one is
    # counted as running.
    assert [
        (w["name"], w["runs"], w["running"], w["median_minutes"], w["failures"], w["cancelled"])
        for w in ci["workflows"]
    ] == [("E2E", 1, 0, 30, 0, 0), ("CI", 3, 1, 12, 1, 1)]
    e2e, ci_workflow = ci["workflows"]
    # A skipped job takes no time and waits for no runner.
    assert [job["name"] for job in e2e["jobs"]] == ["browser"]
    assert (e2e["wait_median"], e2e["wait_p90"]) == (10, 10)
    assert (ci_workflow["wait_median"], ci_workflow["wait_p90"]) == (2, 5)
    assert [
        (
            j["name"],
            j["median_minutes"],
            j["wait_median"],
            j["wait_p90"],
            j["failures"],
            j["cancelled"],
        )
        for j in ci_workflow["jobs"]
    ] == [
        ("tests (1 of 2)", 8, 2, 2, 1, 1),
        ("tests (2 of 2)", 6, 3, 5, 0, 1),
        ("CI", 1, 0, 0, 1, 0),
    ]
    assert ci["unread_jobs"] == 0
    assert "img src" not in _site(workflow).read_text()


def test_at_most_40_job_reads_a_pass_and_a_finished_attempt_s_jobs_are_read_once(
    workflow: Workflow,
) -> None:
    folder = _sources(workflow)
    runs = json.loads((folder / "runs.json").read_text())
    many = [{**runs[0], "id": 400 + number} for number in range(45)]
    (folder / "runs.json").write_text(json.dumps(many))
    cache = _cache(workflow, "jobs")
    cache.mkdir(parents=True)
    # An attempt of a run no longer in the window.
    (cache / "99-1.json").write_text("[]")

    def job_reads() -> int:
        return len([call for call in workflow.calls("gh api repos/") if "/jobs?" in call])

    first = _pass(workflow)
    assert job_reads() == 40
    assert first["ci"]["unread_jobs"] == 5
    assert "jobs of 5 runs are not read yet" in _site(workflow).read_text()

    second = _pass(workflow)
    assert job_reads() == 45
    assert second["ci"]["unread_jobs"] == 0

    _pass(workflow)
    assert job_reads() == 45
    assert not (cache / "99-1.json").exists()
    assert len(list(cache.iterdir())) == 45


# The machines.


def _machines(workflow: Workflow) -> None:
    (workflow.repo / "shipline.json").write_text(
        json.dumps(
            {
                "machines": [
                    {"name": "this machine"},
                    {"name": "runner-1", "address": "runner-1.example"},
                    {"name": "runner-2", "address": "runner-2.example"},
                ]
            }
        )
    )


def test_each_machine_is_sampled_and_one_that_misses_a_read_keeps_its_last_numbers(
    workflow: Workflow,
) -> None:
    _sources(workflow)
    _machines(workflow)
    kept = _state(workflow) / "machines.json"
    kept.parent.mkdir(parents=True)
    earlier = {
        "machine": "runner-2",
        "load": 4.0,
        "processors": 8,
        "memory_total_kb": 32 * 2**20,
        "memory_available_kb": 20 * 2**20,
    }
    kept.write_text(
        json.dumps(
            {
                "samples": [
                    {**earlier, "at": "2026-10-22T05:00:00Z"},
                    {**earlier, "at": "2026-10-24T05:00:00Z"},
                ]
            }
        )
    )

    machines = {machine["name"]: machine for machine in _pass(workflow)["machines"]}

    assert list(machines) == ["this machine", "runner-1", "runner-2"]
    this = machines["this machine"]
    assert (this["at"], this["fresh"], this["load"], this["processors"]) == (
        "2026-10-24T07:50:00Z",
        True,
        2.0,
        4,
    )
    assert (this["available_gb"], this["total_gb"]) == (6.0, 16.0)
    assert (machines["runner-1"]["load"], machines["runner-1"]["processors"]) == (6.0, 8)
    assert machines["runner-1"]["available_gb"] == 4.0
    stale = machines["runner-2"]
    assert (stale["at"], stale["fresh"], stale["load"]) == ("2026-10-24T05:00:00Z", False, 4.0)
    # Over the day: the load against the processors and the memory available, hour by hour.
    assert stale["hours"][21] == {
        "from": "2026-10-24T04:50:00Z",
        "load_share": 0.5,
        "available_gb": 20.0,
    }
    assert this["hours"][23]["load_share"] == 0.5
    assert this["hours"][0] == {
        "from": "2026-10-23T07:50:00Z",
        "load_share": None,
        "available_gb": None,
    }
    # Kept for the window only.
    samples = json.loads(kept.read_text())["samples"]
    assert sorted((s["machine"], s["at"]) for s in samples) == [
        ("runner-1", "2026-10-24T07:50:00Z"),
        ("runner-2", "2026-10-24T05:00:00Z"),
        ("this machine", "2026-10-24T07:50:00Z"),
    ]
    assert oct(kept.stat().st_mode & 0o777) == "0o600"
    assert "last answer 10-24 05:00 UTC" in _site(workflow).read_text()
    command = "cat /proc/loadavg; nproc; grep -E '^Mem(Total|Available):' /proc/meminfo"
    assert workflow.calls("ssh") == [
        f"ssh -o BatchMode=yes -o ConnectTimeout=5 -- runner-1.example {command}",
        f"ssh -o BatchMode=yes -o ConnectTimeout=5 -- runner-2.example {command}",
    ]


def test_without_machines_named_only_this_machine_is_sampled(workflow: Workflow) -> None:
    _sources(workflow)

    machines = _pass(workflow)["machines"]

    assert [machine["name"] for machine in machines] == ["this machine"]
    assert workflow.calls("ssh") == []


# The page.


def test_the_page_is_static_reloads_every_5_minutes_and_names_each_source_and_its_time(
    workflow: Workflow,
) -> None:
    _sources(workflow)

    _pass(workflow)

    page = _site(workflow).read_text()
    assert '<meta http-equiv="refresh" content="300">' in page
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert "<script" not in page.lower() and "javascript:" not in page.lower()
    # A title from GitHub is text, never markup.
    assert "feat: &lt;b&gt;forty-three&lt;/b&gt; &amp; more" in page
    assert "<b>forty-three</b>" not in page
    for source in (
        "GitHub, open pull requests",
        "merge-train.json",
        "GitHub, merged pull requests",
        "the tick&#x27;s journal",
        "wake.log",
        "GitHub Actions",
        "machines.json",
    ):
        assert f"{source}, read 07:50 UTC" in page, source
    # The line's count with how many are blocked there.
    assert ">merge line: 5, 4 blocked</h3>" in page
    for heading in ("Work now", "Merged in 24 hours", "Merge line", "CI", "Machines"):
        assert f">{heading}</h2>" in page
    assert oct(_site(workflow).stat().st_mode & 0o777) == "0o644"


def test_out_names_another_folder_for_the_page(workflow: Workflow) -> None:
    _sources(workflow)
    elsewhere = workflow.root / "elsewhere"

    _pass(workflow, "--out", str(elsewhere))

    assert "merge line: 5 pull requests" in (elsewhere / "index.html").read_text()
    assert not _site(workflow).exists()


def test_below_the_reserve_github_is_not_read_and_the_rest_of_the_page_is(
    workflow: Workflow,
) -> None:
    _sources(workflow)
    budget = workflow.root / "home" / ".local" / "state" / "workflow" / "github-budget"
    budget.mkdir(parents=True)
    reset = int(time.time()) + HOUR
    (budget / "budget.json").write_text(
        json.dumps(
            {
                "graphql": {"remaining": 100, "reset": reset},
                "core": {"remaining": 100, "reset": reset},
            }
        )
    )

    numbers = _pass(workflow)

    assert workflow.calls("gh") == []
    # Without the open pull requests no stage can be judged the bottleneck.
    assert numbers["bottleneck"] == (
        "bottleneck unknown: the open pull requests were not read (GitHub budget low: paused)"
    )
    assert numbers["sources"]["open pull requests"]["error"] == "GitHub budget low: paused"
    assert numbers["sources"]["runs"]["error"] == "GitHub budget low: paused"
    page = _site(workflow).read_text()
    assert "GitHub budget low: paused" in page
    # The line is the train's alone, without ages; the journal, the wake log and the machines
    # are read as ever.
    assert [item["number"] for item in numbers["stages"]["merge line"]["items"]] == [
        44,
        46,
        45,
        48,
        51,
    ]
    assert numbers["machines"][0]["fresh"]
    assert workflow.calls("journalctl")


def test_a_train_file_that_cannot_be_read_is_named_without_its_path(workflow: Workflow) -> None:
    _sources(workflow)
    train = workflow.repo / ".local" / "workflow" / "merge-train.json"
    train.unlink()
    train.mkdir()

    numbers = _pass(workflow)

    assert numbers["sources"]["merge train"]["error"] == "cannot be read: Is a directory"
    assert numbers["bottleneck"] == (
        "bottleneck unknown: the merge train was not read (cannot be read: Is a directory)"
    )
    assert str(workflow.root) not in _site(workflow).read_text()


def test_a_train_file_gone_while_the_tick_runs_is_no_empty_line(workflow: Workflow) -> None:
    _sources(workflow)
    (workflow.repo / ".local" / "workflow" / "merge-train.json").unlink()

    numbers = _pass(workflow)

    # Read as an empty line, the line's members would count as in review.
    assert numbers["sources"]["merge train"]["error"] == "not found"
    assert numbers["bottleneck"] == "bottleneck unknown: the merge train was not read (not found)"
    assert "bottleneck unknown: the merge train was not read" in _site(workflow).read_text()


def test_a_train_file_half_written_gives_no_bottleneck(workflow: Workflow) -> None:
    _sources(workflow)
    train = workflow.repo / ".local" / "workflow" / "merge-train.json"
    whole = train.read_text()
    train.write_text(whole[: len(whole) // 2])

    numbers = _pass(workflow)

    assert numbers["sources"]["merge train"]["error"] == "cannot be read (JSONDecodeError)"
    assert numbers["bottleneck"].startswith("bottleneck unknown: the merge train was not read")


def test_a_path_in_the_journal_keeps_only_its_last_part(workflow: Workflow) -> None:
    folder = _sources(workflow)
    frame = '  File "/home/example-user/example-app/scripts/workflow/merge_gate.py", line 7, in run'
    with (folder / "journal.jsonl").open("a") as journal:
        journal.write(
            json.dumps({"__REALTIME_TIMESTAMP": "1792749660000000", "MESSAGE": frame}) + "\n"
        )

    numbers = _pass(workflow)

    assert numbers["line"]["gaps"][0]["journal"]['File ".../merge_gate.py", line #, in run'] == 1
    page = _site(workflow).read_text()
    assert "merge_gate.py" in page and "/home/example-user" not in page


def test_a_pass_leaves_while_another_one_runs(workflow: Workflow) -> None:
    _sources(workflow)
    _state(workflow).mkdir(parents=True)
    with (_state(workflow) / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        done = workflow.run_script("flow-page", env=AT)

    assert done.returncode == 0, done.stderr
    assert "still running" in done.stdout
    assert workflow.calls("gh") == []
    assert not _site(workflow).exists()


# The server.


def _settings(workflow: Workflow, text: str) -> None:
    settings = workflow.repo / ".github" / "runner" / "settings.sh"
    settings.write_text(settings.read_text() + text)


def _free_port(address: str) -> int:
    with socket.socket() as probe:
        probe.bind((address, 0))
        return probe.getsockname()[1]


def _get(address: str, port: int, path: str = "/") -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://{address}:{port}{path}", timeout=5) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""


def test_the_server_binds_only_to_the_configured_address(workflow: Workflow) -> None:
    _sources(workflow)
    _pass(workflow)
    port = _free_port("127.0.0.2")
    _settings(workflow, f"flow_page_address=127.0.0.2\nflow_page_port={port}\n")
    server = subprocess.Popen(
        ["bash", str(workflow.script.parent / "flow-page"), "--serve", str(port)],
        cwd=workflow.repo,
        env={**workflow.env, **AT},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.2", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        status, page = _get("127.0.0.2", port)
        assert status == 200
        assert "merge line: 5 pull requests" in page
        assert _get("127.0.0.2", port, "/index.html")[0] == 200
        # Nothing but the page's folder, and no listing of it.
        assert _get("127.0.0.2", port, "/../machines.json")[0] == 404
        (_site(workflow).parent / "notes").mkdir()
        assert _get("127.0.0.2", port, "/notes/")[0] == 404
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
    finally:
        server.terminate()
        server.communicate(timeout=10)


def test_without_settings_nothing_is_served(workflow: Workflow) -> None:
    done = workflow.run_script("flow-page", "--serve", str(_free_port("127.0.0.1")))

    assert done.returncode == 1
    assert "nothing is served" in done.stderr


@pytest.mark.parametrize(
    "settings,printed",
    [
        ("", ""),
        ("flow_page_port=8800\n", "127.0.0.1 8800\n"),
        ("flow_page_address=192.0.2.10\n", "192.0.2.10 8765\n"),
        ("flow_page_address=192.0.2.10\nflow_page_port=8800\n", "192.0.2.10 8800\n"),
    ],
)
def test_the_local_settings_name_where_the_page_is_served(
    workflow: Workflow, settings: str, printed: str
) -> None:
    _settings(workflow, settings)

    done = workflow.run_script("flow-page", "--settings")

    assert done.returncode == 0, done.stderr
    assert done.stdout == printed


@pytest.mark.parametrize(
    "settings",
    [
        "flow_page_address=0.0.0.0\n",
        "flow_page_address=::\n",
        "flow_page_address=example-host\n",
        "flow_page_port=80000\n",
        "flow_page_port='8800; touch nope'\n",
    ],
)
def test_an_address_for_everyone_or_that_is_no_address_is_refused(
    workflow: Workflow, settings: str
) -> None:
    _settings(workflow, settings)

    for args in (("--settings",), ("--serve", "8800")):
        done = workflow.run_script("flow-page", *args)
        assert done.returncode == 1
        assert "flow_page_" in done.stderr
    assert not (workflow.repo / "nope").exists()


# The timer.


def _units(workflow: Workflow) -> Path:
    return Path(workflow.env["HOME"]) / ".config" / "systemd" / "user"


def test_the_timer_runs_the_page_every_10_minutes_and_serves_nothing_without_settings(
    workflow: Workflow,
) -> None:
    done = workflow.run_script("install-flow-page-timer")

    assert done.returncode == 0, done.stderr
    service = (_units(workflow) / "flow-page.service").read_text().splitlines()
    assert "Type=oneshot" in service
    assert f"WorkingDirectory={workflow.repo}" in service
    assert f'ExecStart="{workflow.repo}/scripts/workflow/flow-page"' in service
    assert f'Environment="PATH={workflow.env["PATH"]}"' in service
    timer = (_units(workflow) / "flow-page.timer").read_text().splitlines()
    assert "OnCalendar=*:0/10" in timer
    assert "WantedBy=timers.target" in timer
    assert not (_units(workflow) / "flow-page-serve.service").exists()
    assert workflow.calls("systemctl") == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now flow-page.timer",
    ]


def test_with_settings_a_service_serves_the_page_on_their_port(workflow: Workflow) -> None:
    _settings(workflow, "flow_page_address=192.0.2.10\nflow_page_port=8800\n")

    done = workflow.run_script("install-flow-page-timer")

    assert done.returncode == 0, done.stderr
    serve = (_units(workflow) / "flow-page-serve.service").read_text().splitlines()
    assert f'ExecStart="{workflow.repo}/scripts/workflow/flow-page" --serve 8800' in serve
    assert "Restart=always" in serve and "RestartSec=30" in serve
    assert "WantedBy=default.target" in serve
    assert workflow.calls("systemctl") == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now flow-page.timer",
        "systemctl --user enable flow-page-serve.service",
        "systemctl --user restart flow-page-serve.service",
    ]


def test_settings_taken_away_stop_and_remove_the_server(workflow: Workflow) -> None:
    _settings(workflow, "flow_page_port=8800\n")
    workflow.run_script("install-flow-page-timer")
    settings = workflow.repo / ".github" / "runner" / "settings.sh"
    settings.write_text(settings.read_text().replace("flow_page_port=8800\n", ""))
    (workflow.state / "calls.log").unlink()

    done = workflow.run_script("install-flow-page-timer")

    assert done.returncode == 0, done.stderr
    assert not (_units(workflow) / "flow-page-serve.service").exists()
    assert workflow.calls("systemctl") == [
        "systemctl --user disable --now flow-page-serve.service",
        "systemctl --user daemon-reload",
        "systemctl --user enable --now flow-page.timer",
    ]


def test_the_page_s_state_is_kept_from_other_users(workflow: Workflow) -> None:
    _sources(workflow)

    _pass(workflow)

    for path in (_state(workflow), _cache(workflow, "merged"), _cache(workflow, "jobs")):
        assert oct(path.stat().st_mode & 0o777) == "0o700", path
    for path in (*_cache(workflow, "merged").iterdir(), *_cache(workflow, "jobs").iterdir()):
        assert oct(path.stat().st_mode & 0o777) == "0o600", path
    assert os.access(_site(workflow), os.R_OK)
