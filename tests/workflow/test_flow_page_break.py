"""Tries to break `flow-page` as an installation uses it every day: a long merge line read from a
phone, two CI machines (one sometimes off), the shared GitHub budget, sources half-written while
the tick runs, partial re-runs, two calls at once and a served folder."""

import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import test_flow_page as base
from workflow_harness import Workflow

NOW = base.NOW
AT = base.AT
HOUR = base.HOUR


def _iso(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def _pull(
    number: int,
    *,
    draft: bool,
    labels: list[str],
    created: float,
    events: list[tuple[str, float]] = (),
    rounds: list[tuple[str, float]] = (),
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"example-app change {number}",
        "isDraft": draft,
        "createdAt": _iso(created),
        "labels": {"nodes": [{"name": name} for name in labels]},
        "timelineItems": {
            "nodes": [
                {
                    "__typename": kind,
                    "createdAt": _iso(at),
                    "label": {"name": "workflow"},
                }
                if kind == "LabeledEvent"
                else {"__typename": kind, "createdAt": _iso(at)}
                for kind, at in events
            ]
        },
        "reviews": {
            "nodes": [
                {
                    "body": f"<!-- workflow-review round=1 result={result} -->\nFindings.",
                    "submittedAt": _iso(at),
                }
                for result, at in rounds
            ]
        },
    }


LINE = list(range(100, 125))  # 25 pull requests waiting in the merge line


def _daily(workflow: Workflow) -> Path:
    """A usual day: 25 pull requests in the merge line, the oldest passed 26 hours ago; 3 being
    built, 2 in review, 2 tickets waiting for a start."""
    folder = base._sources(workflow)
    nodes = []
    for index, number in enumerate(LINE):
        passed = NOW - (26 - index) * HOUR
        nodes.append(
            _pull(
                number,
                draft=False,
                labels=["workflow"],
                created=passed - 10 * HOUR,
                events=[
                    ("LabeledEvent", passed - 9 * HOUR),
                    ("ReadyForReviewEvent", passed - 2 * HOUR),
                ],
                rounds=[("passed", passed)],
            )
        )
    for number in (130, 131, 132):
        nodes.append(
            _pull(
                number,
                draft=True,
                labels=["workflow"],
                created=NOW - 5 * HOUR,
                events=[("LabeledEvent", NOW - 4 * HOUR)],
            )
        )
    for number in (140, 141):
        nodes.append(
            _pull(
                number,
                draft=False,
                labels=["workflow"],
                created=NOW - 6 * HOUR,
                events=[
                    ("LabeledEvent", NOW - 5 * HOUR),
                    ("ReadyForReviewEvent", NOW - HOUR),
                ],
            )
        )
    for number in (150, 151):
        nodes.append(_pull(number, draft=True, labels=[], created=NOW - 30 * HOUR))
    base._set_open(folder, nodes)
    train = workflow.repo / ".local" / "workflow" / "merge-train.json"
    train.write_text(
        json.dumps(
            {"waiting": [str(n) for n in LINE], "conflicts": {"103": "head-103"}}
        )
    )
    return folder


def _train(workflow: Workflow) -> Path:
    return workflow.repo / ".local" / "workflow" / "merge-train.json"


# The daily look from a phone.


def test_daily_view_names_the_merge_line_with_its_count_oldest_and_blocked(
    workflow: Workflow,
) -> None:
    _daily(workflow)

    numbers = base._pass(workflow)

    assert (
        numbers["bottleneck"]
        == "merge line: 25 pull requests waiting, oldest 26 h, 1 blocked"
    )
    counts = {stage: len(found["items"]) for stage, found in numbers["stages"].items()}
    assert counts == {
        "waiting for a start": 2,
        "being built": 3,
        "in review": 2,
        "merge line": 25,
    }
    page = base._site(workflow).read_text()
    assert "merge line: 25 pull requests waiting, oldest 26 h, 1 blocked" in page
    assert 'content="300"' in page and "<script" not in page


def test_train_file_missing_while_the_tick_runs_does_not_name_another_stage(
    workflow: Workflow,
) -> None:
    _daily(workflow)
    _train(workflow).unlink()

    numbers = base._pass(workflow)

    # 25 pull requests sit in the merge line; without the train the page must say the train was
    # not read, and must not say the bottleneck is review.
    assert numbers["sources"]["merge train"]["error"], numbers["sources"]["merge train"]
    assert not numbers["bottleneck"].startswith("in review"), numbers["bottleneck"]


def test_train_file_half_written_while_the_tick_runs_does_not_name_another_stage(
    workflow: Workflow,
) -> None:
    _daily(workflow)
    whole = _train(workflow).read_text()
    _train(workflow).write_text(whole[: len(whole) // 2])

    numbers = base._pass(workflow)

    assert numbers["sources"]["merge train"]["error"]
    assert not numbers["bottleneck"].startswith("in review"), numbers["bottleneck"]


def test_wake_log_half_written_and_journal_failing_still_write_the_page(
    workflow: Workflow,
) -> None:
    _daily(workflow)
    log = workflow.wake_log
    log.write_text(log.read_text() + "2026-10-24 07:4")
    workflow.fake(
        "journalctl", "#!/bin/bash\necho 'Failed to open journal' >&2\nexit 1\n"
    )

    numbers = base._pass(workflow)

    assert numbers["bottleneck"].startswith("merge line: 25")
    assert numbers["sources"]["journal"]["error"] == "journalctl exited 1"
    assert numbers["sources"]["wake log"]["error"] is None
    assert "not read: journalctl exited 1" in base._site(workflow).read_text()


def test_wake_log_missing_is_named_and_the_rest_is_written(workflow: Workflow) -> None:
    _daily(workflow)
    workflow.wake_log.unlink()

    numbers = base._pass(workflow)

    assert numbers["sources"]["wake log"]["error"] == "not found"
    assert numbers["bottleneck"].startswith("merge line: 25")


# Two CI machines, the second one sometimes off.

_HANGING_SSH = """#!/bin/bash
printf 'ssh %s\\n' "$*" >> "@STATE@/calls.log"
while [[ $1 != -- ]]; do shift; done
if [[ $2 == *@ci-2.example ]]; then exec /bin/sleep 600; fi
cat "@FIXTURES@/runner-1.txt"
"""


def test_second_machine_off_and_hanging_keeps_its_last_numbers_within_the_ssh_bound(
    workflow: Workflow,
) -> None:
    folder = _daily(workflow)
    (workflow.repo / "shipline.json").write_text(
        json.dumps(
            {
                "machines": [
                    {"name": "main"},
                    {"name": "ci-2", "address": "runner@ci-2.example"},
                ]
            }
        )
    )
    workflow.fake("ssh", _HANGING_SSH.replace("@FIXTURES@", str(folder)))
    kept = base._state(workflow) / "machines.json"
    kept.parent.mkdir(parents=True)
    earlier = {
        "machine": "ci-2",
        "load": 8.0,
        "processors": 16,
        "memory_total_kb": 30 * 2**20,
        "memory_available_kb": 12 * 2**20,
        "at": _iso(NOW - 2 * HOUR),
    }
    kept.write_text(json.dumps({"samples": [earlier]}))

    started = time.monotonic()
    numbers = base._pass(workflow)
    took = time.monotonic() - started

    assert took < 40, took
    machines = {machine["name"]: machine for machine in numbers["machines"]}
    assert machines["main"]["fresh"]
    assert (machines["ci-2"]["fresh"], machines["ci-2"]["load"]) == (False, 8.0)
    assert "last answer" in base._site(workflow).read_text()
    assert "ci-2.example" not in base._site(workflow).read_text()


# The shared GitHub budget at the installation's volume.


def _many_runs(folder: Path, count: int) -> None:
    runs = json.loads((folder / "runs.json").read_text())
    template = runs[0]
    every = []
    for index in range(count):
        created = NOW - 23 * HOUR + index * (23 * HOUR // count)
        every.append(
            {
                **template,
                "id": 10_000 + index,
                "created_at": _iso(created),
                "run_started_at": _iso(created + 60),
                "updated_at": _iso(created + 600),
            }
        )
    (folder / "runs.json").write_text(json.dumps(list(reversed(every))))


def test_github_calls_an_hour_stay_a_few_hundred_at_780_runs_a_day(
    workflow: Workflow,
) -> None:
    folder = _daily(workflow)
    _many_runs(folder, 780)

    def calls() -> int:
        return len(workflow.calls("gh "))

    hourly = []
    for _ in range(
        6
    ):  # the first hour after installing: 6 passes, every job not read yet
        base._pass(workflow)
    hourly.append(calls())
    for _ in range(14):
        base._pass(workflow)
    before = calls()
    for _ in range(6):  # an hour once every finished attempt's jobs are kept
        base._pass(workflow)
    hourly.append(calls() - before)

    print("calls an hour:", hourly)
    assert hourly[0] <= 350, hourly
    assert hourly[1] <= 100, hourly


# Re-run in part, and a check late.


def test_a_run_re_run_in_part_is_running_then_its_new_attempt_s_jobs_are_read(
    workflow: Workflow,
) -> None:
    folder = base._sources(workflow)
    base._pass(workflow)
    runs = json.loads((folder / "runs.json").read_text())
    first = runs[0]
    jobs = json.loads((folder / "jobs.json").read_text())
    assert f"{first['id']}-1" in jobs
    reads = len(
        [c for c in workflow.calls("gh api") if f"runs/{first['id']}/attempts" in c]
    )

    runs[0] = {**first, "run_attempt": 2, "status": "in_progress", "conclusion": None}
    (folder / "runs.json").write_text(json.dumps(runs))
    middle = base._pass(workflow)
    ci = {row["name"]: row for row in middle["ci"]["workflows"]}
    assert ci[first["name"]]["running"] >= 1

    runs[0] = {
        **first,
        "run_attempt": 2,
        "status": "completed",
        "conclusion": "success",
        "updated_at": _iso(NOW - 600),
    }
    (folder / "runs.json").write_text(json.dumps(runs))
    jobs[f"{first['id']}-2"] = [
        {
            **job,
            "created_at": _iso(NOW - 1800),
            "started_at": _iso(NOW - 1200),
            "completed_at": _iso(NOW - 900),
        }
        for job in jobs[f"{first['id']}-1"][:1]
    ]
    (folder / "jobs.json").write_text(json.dumps(jobs))
    base._pass(workflow)
    asked = [
        c for c in workflow.calls("gh api") if f"runs/{first['id']}/attempts" in c
    ][reads:]
    assert any("attempts/2/jobs" in c for c in asked), asked
    assert not (base._cache(workflow, "jobs") / f"{first['id']}-1.json").exists()
    assert (base._cache(workflow, "jobs") / f"{first['id']}-2.json").exists()


# Two calls at once, and the gate's lock.

_SLOW_GH_PREFIX = "import time\ntime.sleep(0.5)\n"


def test_two_passes_at_once_write_one_whole_page(workflow: Workflow) -> None:
    _daily(workflow)
    fake = workflow.bin / "gh"
    text = fake.read_text().replace(
        "import json\n", "import json\n" + _SLOW_GH_PREFIX, 1
    )
    fake.write_text(text)
    env = {**workflow.env, **AT}
    command = ["bash", str(workflow.script.parent / "flow-page")]
    both = [
        subprocess.Popen(
            command,
            cwd=workflow.repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=100) for process in both]

    assert [process.returncode for process in both] == [0, 0], outputs
    assert sum("still running" in out for out, _ in outputs) == 1, outputs
    page = base._site(workflow).read_text()
    assert page.endswith("</body></html>\n")
    assert len(workflow.calls("gh api graphql")) >= 2
    leftovers = [
        p.name for p in base._site(workflow).parent.iterdir() if p.name != "index.html"
    ]
    assert leftovers == []


def test_the_gate_holding_the_merge_lock_does_not_stall_a_pass(
    workflow: Workflow,
) -> None:
    import fcntl

    _daily(workflow)
    lock = workflow.repo / ".local" / "workflow" / "merge.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        numbers = base._pass(workflow)
        took = time.monotonic() - started

    assert took < 30, took
    assert numbers["bottleneck"].startswith("merge line: 25")


# What the served page shows, and what the server hands out.


def test_a_traceback_in_the_tick_s_journal_puts_no_home_path_on_the_served_page(
    workflow: Workflow,
) -> None:
    folder = base._sources(workflow)
    lines = []
    for index in range(20):
        moment = (NOW - 20 * HOUR + index * 600) * 10**6
        for message in (
            "Traceback (most recent call last):",
            '  File "/home/example-user/example-app/scripts/workflow/merge_gate.py", line 1190, in run',
            "OSError: [Errno 28] No space left on device",
        ):
            lines.append(
                json.dumps({"__REALTIME_TIMESTAMP": str(moment), "MESSAGE": message})
            )
    (folder / "journal.jsonl").write_text("\n".join(lines) + "\n")
    # No merges in the window: the whole day is one gap, with what the journal said.
    merged = folder / "merged-search.json"
    pages = json.loads(merged.read_text())
    pages[0]["data"]["search"]["nodes"] = []
    merged.write_text(json.dumps(pages))

    base._pass(workflow)

    page = base._site(workflow).read_text()
    assert "No space left on device" in page
    assert "/home/example-user" not in page


def test_the_server_hands_out_nothing_beside_the_page_and_serves_a_phone_beside_a_silent_client(
    workflow: Workflow,
) -> None:
    base._sources(workflow)
    base._pass(workflow)
    port = base._free_port("127.0.0.1")
    base._settings(workflow, f"flow_page_address=127.0.0.1\nflow_page_port={port}\n")
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
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        silent = socket.create_connection(("127.0.0.1", port), timeout=1)
        started = time.monotonic()
        status, page = base._get("127.0.0.1", port)
        assert status == 200 and "Workflow flow" in page
        assert time.monotonic() - started < 5
        for path in (
            "/%2e%2e/machines.json",
            "/..%2fmachines.json",
            "/%2e%2e/cache/",
            "//..//..//lock",
            "/../../github-budget/budget.json",
        ):
            assert base._get("127.0.0.1", port, path)[0] == 404, path
        silent.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
