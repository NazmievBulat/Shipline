"""Every orchestrator decision is a record of a fixed shape, checked and acted on by `decide`."""

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from workflow_harness import Workflow

# 13 October 2026, 21:00 in New York: the wake looks in at night by the machine's clock.
_EVENING = int(datetime(2026, 10, 13, 21, 0, tzinfo=ZoneInfo("America/New_York")).timestamp())

_RULES = Path(__file__).resolve().parents[2] / "scripts/workflow/RULES.md"

# The description edit the stand-in `gh` does not know: it keeps the new body as `body-<n>`
# and counts the edits in `patched`. Every other call goes to the usual stand-in.
_GH = """#!/bin/bash
if [[ "$1 $2 $3 $5" == "api -X PATCH -F" && $4 == repos/*/pulls/* && $6 == body=@* ]]; then
  echo "gh $*" >> "@STATE@/calls.log"
  [ ! -e "@STATE@/fail-gh-patch" ] || exit 1
  number=${4##*/}
  cp "${6#body=@}" "@STATE@/body-$number"
  echo "$number" >> "@STATE@/patched"
  exit 0
fi
exec bash "@STATE@/gh-usual" "$@"
"""


@pytest.fixture
def team(workflow: Workflow) -> Workflow:
    """A registered orchestrator, a usage window that allows starts, and the edit of a body."""
    workflow.orchestrator("idle", name="orchestrator-1")
    _usage(workflow, 40)
    usual = workflow.bin / "gh"
    (workflow.state / "gh-usual").write_text(usual.read_text())
    workflow.fake("gh", _GH)
    return workflow


def _usage(workflow: Workflow, percent: int, resets_in: int = 7200) -> None:
    resets = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + resets_in))
    workflow.usage(percent, resets)


def _ticket(
    workflow: Workflow,
    number: int,
    files: str,
    *,
    labels: tuple[str, ...] = ("priority:+medium",),
    draft: bool = True,
    extra: str = "",
) -> None:
    """A ticket whose description names its files under a `Files` heading."""
    workflow.add_pull_request(number, labels=labels, draft=draft)
    lines = "\n".join(f"- `{path}`" for path in files.split())
    (workflow.state / f"body-{number}").write_text(
        f"Change the workflow scripts.{extra}\n\n## Files\n\n{lines}\n"
    )


def _decide(workflow: Workflow, *words: str, at: int | None = None):
    return workflow.run_script("decide", *words, env={"WORKFLOW_NOW": str(at or int(time.time()))})


def _lines(workflow: Workflow) -> list[dict]:
    path = workflow.repo / ".local/workflow/decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _records(workflow: Workflow) -> list[dict]:
    """Each decision as its last line says it is, in the order the decisions were made."""
    last: dict[str, dict] = {}
    for line in _lines(workflow):
        last[line["id"]] = line
    return list(last.values())


def _labelled(workflow: Workflow) -> list[str]:
    path = workflow.state / "labelled"
    return path.read_text().split() if path.exists() else []


def _plan(workflow: Workflow) -> str:
    path = workflow.repo / ".local/workflow/start-plan"
    return path.read_text() if path.exists() else ""


def test_a_record_has_the_fixed_shape_and_names_a_heading_of_the_rules(
    team: Workflow,
) -> None:
    _ticket(team, 8, "scripts/workflow/notify")

    done = _decide(team, "start", "8", "--evidence", "next in the order, nothing shares its files")

    assert done.returncode == 0, done.stderr
    assert _labelled(team) == ["8"]
    [record] = _records(team)
    assert re.fullmatch(r"start-8-\d{8}T\d{6}Z-[0-9a-f]{6}", record["id"])
    assert record["status"] == "done"
    assert record["kind"] == "start"
    assert record["number"] == 8
    assert record["choice"] == "start 8"
    assert record["authority"] == "delegated"
    assert record["owner_words"] is None
    assert record["session"] == "orchestrator-1"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["at"])
    assert "next in the order, nothing shares its files" in record["evidence"]
    headings = {
        line.lstrip("#").strip() for line in _RULES.read_text().splitlines() if line.startswith("#")
    }
    assert record["rule"] in headings
    # What it printed is the record's line, for the orchestrator to quote.
    assert json.loads(done.stdout.strip().splitlines()[-1]) == record
    # A pending line came first, before the label.
    assert [line["status"] for line in _lines(team)] == ["pending", "done"]


def test_a_start_on_overlapping_files_is_refused_without_a_hold_and_recorded_with_one(
    team: Workflow,
) -> None:
    _ticket(team, 7, "scripts/workflow/tick", labels=("workflow", "priority:+medium"))
    _ticket(team, 8, "scripts/workflow")

    refused = _decide(team, "start", "8")

    assert refused.returncode == 1
    assert "shares scripts/workflow/tick with 7" in refused.stderr
    assert _labelled(team) == []
    [record] = _records(team)
    assert record["status"] == "refused"
    assert "shares scripts/workflow/tick with 7" in record["reason"]

    held = _decide(team, "start", "8", "--after", "7")

    assert held.returncode == 0, held.stderr
    assert _labelled(team) == []
    hold = _records(team)[-1]
    assert hold["kind"] == "hold"
    assert hold["status"] == "done"
    assert hold["details"]["after"] == 7
    assert hold["details"]["shared"] == ["scripts/workflow/tick"]
    assert re.search(rf"^8 after 7 +# {hold['id']}", _plan(team), re.MULTILINE)


def test_an_out_of_order_start_is_refused(team: Workflow) -> None:
    _ticket(team, 7, "docs/one")
    _ticket(team, 8, "docs/two", labels=("priority:+high",))
    _ticket(team, 9, "docs/three")

    refused = _decide(team, "start", "9")

    assert refused.returncode == 1
    assert "out of order: 8 goes first" in refused.stderr
    assert _labelled(team) == []
    assert _decide(team, "start", "8").returncode == 0
    assert _labelled(team) == ["8"]


def test_a_ticket_without_a_priority_label_is_refused(team: Workflow) -> None:
    _ticket(team, 8, "docs/two", labels=())

    refused = _decide(team, "start", "8")

    assert refused.returncode == 1
    assert "no priority label" in refused.stderr


def test_the_owners_word_lifts_order_overlap_and_his_own_mark_but_not_the_usage_window(
    team: Workflow,
) -> None:
    _ticket(team, 7, "scripts/workflow/tick", labels=("workflow", "priority:+medium"))
    _ticket(team, 8, "docs/first", labels=("priority:+high",))
    _ticket(team, 9, "scripts/workflow", extra="\n\nStart: owner only")

    assert "needs the owner's word" in _decide(team, "start", "9").stderr
    _usage(team, 90)
    held = _decide(team, "start", "9", "--owner", "start 9 now")
    assert held.returncode == 1
    assert "the 5-hour usage window is 90% used" in held.stderr
    _usage(team, 40)

    done = _decide(team, "start", "9", "--owner", "start 9 now")

    assert done.returncode == 0, done.stderr
    assert _labelled(team) == ["9"]
    record = _records(team)[-1]
    assert record["authority"] == "owner"
    assert record["owner_words"] == "start 9 now"


def test_an_answer_in_an_owner_only_area_is_refused_unless_marked_as_his_word(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two")
    question = (
        "--question",
        "the cost of a run",
        "--meaning",
        "a run may cost a little",
    )

    for area in ("money", "privacy", "legal", "model-text"):
        refused = _decide(team, "answer", "8", "--area", area, "--reply", "yes", *question)
        assert refused.returncode == 1
        assert "only the owner answers" in refused.stderr
    assert not (team.state / "patched").exists()

    done = _decide(team, "answer", "8", "--area", "money", "--owner", "fine, go", *question)

    assert done.returncode == 0, done.stderr
    record = _records(team)[-1]
    assert record["authority"] == "owner"
    assert record["owner_words"] == "fine, go"
    assert [r["status"] for r in _records(team)] == ["refused"] * 4 + ["done"]
    unknown = _decide(team, "answer", "8", "--area", "weather", "--reply", "yes", *question)
    assert unknown.returncode == 1
    assert "not a subject area" in unknown.stderr


def test_an_answer_lands_in_the_description_block(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    moment = int(time.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1)))

    done = _decide(
        team,
        "answer",
        "8",
        "--area",
        "product",
        "--question",
        "where the page is served",
        "--reply",
        "1",
        "--meaning",
        "on the private network only",
        at=moment,
    )

    assert done.returncode == 0, done.stderr
    body = (team.state / "body-8").read_text()
    assert body.startswith("Change the workflow scripts.")
    assert "## Files" in body
    block = body.split("## Answers given for the owner", 1)[1]
    entry = '- 2026-10-24: where the page is served: "1", on the private network only.'
    assert entry in block.splitlines()

    second = _decide(
        team,
        "answer",
        "8",
        "--area",
        "product",
        "--question",
        "how long a run keeps",
        "--reply",
        "a week",
        "--meaning",
        "kept seven days",
        at=moment,
    )

    assert second.returncode == 0, second.stderr
    body = (team.state / "body-8").read_text()
    assert body.count("## Answers given for the owner") == 1
    lines = body.split("## Answers given for the owner", 1)[1].strip().splitlines()
    assert lines == [
        entry,
        '- 2026-10-24: how long a run keeps: "a week", kept seven days.',
    ]


def test_an_allowance_for_a_round_other_than_the_next_is_refused(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two", draft=False, labels=("workflow", "priority:+medium"))
    (team.state / "reviews-8.json").write_text(
        json.dumps(
            [
                {"body": "<!-- workflow-review round=1 result=sent-back --> one"},
                {"body": "<!-- workflow-review round=2 result=sent-back --> two"},
            ]
        )
    )
    allow = team.repo / ".local/workflow/review-8.allow-round"

    for words in (("--round", "2"), ("--round", "4")):
        refused = _decide(team, "allowance", "8", *words, "--owner", "one more round")
        assert refused.returncode == 1
        assert "the next independent round is 3" in refused.stderr
    without = _decide(team, "allowance", "8", "--round", "3")
    assert without.returncode != 0
    assert not allow.exists()

    done = _decide(team, "allowance", "8", "--round", "3", "--owner", "one more round")

    assert done.returncode == 0, done.stderr
    assert allow.read_text().strip() == "3"
    assert _records(team)[-1]["owner_words"] == "one more round"


def test_a_hand_back_records_the_judgment_the_session_and_the_message(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two", draft=False, labels=("workflow", "priority:+medium"))
    team.running("review-8")

    done = _decide(
        team,
        "hand-back",
        "8",
        "--session",
        "review-8",
        "--judgment",
        "the conflict is mechanical",
        "--message",
        "resolve the conflict in the lock file and push",
    )

    assert done.returncode == 0, done.stderr
    record = _records(team)[-1]
    assert record["kind"] == "hand-back"
    assert record["details"] == {
        "session": "review-8",
        "judgment": "the conflict is mechanical",
        "message": "resolve the conflict in the lock file and push",
    }
    assert json.loads(done.stdout.strip().splitlines()[-1])["id"] == record["id"]
    missing = _decide(
        team,
        "hand-back",
        "8",
        "--session",
        "review-9",
        "--judgment",
        "x",
        "--message",
        "y",
    )
    assert missing.returncode == 1


def test_a_recorded_program_choice_routes_the_next_worker(team: Workflow) -> None:
    team.add_pull_request(7, labels=("workflow", "priority:+medium"))

    done = _decide(team, "program", "7", "codex")

    assert done.returncode == 0, done.stderr
    assert (team.repo / ".local/workflow/worker-program-7").read_text().strip() == "codex"
    body = (team.state / "body-7").read_text()
    assert "Worker: Codex (a bug fix with no new UI)" in body.splitlines()

    ticked = team.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "claude"})

    assert ticked.returncode == 0, ticked.stderr
    assert team.is_running("codex-7")

    back = _decide(team, "program", "7", "claude")
    assert back.returncode == 0, back.stderr
    assert "Worker: Codex" not in (team.state / "body-7").read_text()


def test_an_interrupted_decision_is_finished_once(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    (team.state / "fail-gh-label").touch()

    first = _decide(team, "start", "8")

    assert first.returncode == 3
    assert [line["status"] for line in _lines(team)] == ["pending"]
    (team.state / "fail-gh-label").unlink()
    _ticket(team, 9, "docs/three")

    # The next call, of any kind, finishes it first: checked again, then labelled.
    again = _decide(team, "hold", "9", "--after", "8", "--owner", "9 after 8")
    _decide(
        team,
        "restart",
        "9",
        "--session",
        "ticket-9",
        "--judgment",
        "x",
        "--message",
        "y",
    )

    assert again.returncode == 0, again.stderr
    assert _labelled(team) == ["8"]
    assert [line["status"] for line in _lines(team) if line["kind"] == "start"] == [
        "pending",
        "done",
    ]


def test_an_interrupted_answer_keeps_edits_made_meanwhile_and_repairs_only_its_own(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two")
    moment = int(time.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1)))
    (team.state / "fail-gh-patch").touch()
    words = (
        "answer",
        "8",
        "--area",
        "product",
        "--question",
        "where the page is served",
        "--reply",
        "1",
        "--meaning",
        "on the private network only",
    )

    assert _decide(team, *words, at=moment).returncode == 3
    (team.state / "fail-gh-patch").unlink()
    # Meanwhile the worker edits the description, and a cut-off copy of the entry is there.
    body = (team.state / "body-8").read_text()
    (team.state / "body-8").write_text(
        body + "\nA note of the worker.\n\n## Answers given for the owner\n\n"
        '- 2026-10-23: earlier: "x", kept.\n- 2026-10-24: where the page is served: "1'
    )
    # A line of the record file cut off mid-write by the same interruption.
    with (team.repo / ".local/workflow/decisions.jsonl").open("a") as file:
        file.write('{"id": "start-9-')

    done = _decide(
        team,
        "restart",
        "8",
        "--session",
        "ticket-8",
        "--judgment",
        "x",
        "--message",
        "y",
    )

    assert done.returncode == 0, done.stderr
    body = (team.state / "body-8").read_text()
    assert "A note of the worker." in body
    assert body.count("where the page is served") == 1
    assert body.rstrip().splitlines()[-2:] == [
        '- 2026-10-23: earlier: "x", kept.',
        '- 2026-10-24: where the page is served: "1", on the private network only.',
    ]
    assert [line["status"] for line in _lines(team) if line["kind"] == "answer"] == [
        "pending",
        "done",
    ]


def test_the_tick_skips_a_ticket_with_an_unfinished_decision(team: Workflow) -> None:
    team.add_pull_request(7, labels=("workflow", "priority:+medium"))
    (team.state / "fail-gh-patch").touch()
    assert _decide(team, "program", "7", "codex").returncode == 3

    ticked = team.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "claude"})

    assert ticked.returncode == 0, ticked.stderr
    assert not team.is_running("ticket-7")
    assert not team.is_running("codex-7")


def test_a_hold_ends_when_its_ticket_closes(team: Workflow) -> None:
    _ticket(team, 7, "scripts/workflow/tick", labels=("workflow", "priority:+medium"))
    _ticket(team, 8, "scripts/workflow")
    (team.repo / ".local/workflow").mkdir(parents=True, exist_ok=True)
    (team.repo / ".local/workflow/start-plan").write_text("hold 12  # the owner's rule\n")
    moment = int(time.time())
    assert _decide(team, "hold", "8", "--after", "7", at=moment).returncode == 0

    team.set_pull_request(7, "MERGED", team.git("rev-parse", "origin/main"))
    ticked = team.run_script("tick", env={"WORKFLOW_NOW": str(moment + 600)})

    assert ticked.returncode == 0, ticked.stderr
    hold = _records(team)[-1]
    assert hold["status"] == "ended"
    assert hold["ended_because"] == "7 closed"
    assert hold["ended_at"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment + 600))
    assert _plan(team) == "hold 12  # the owner's rule\n"


def test_the_wake_lists_held_tickets_answers_and_refused_starts_from_the_records(
    team: Workflow,
) -> None:
    _ticket(team, 7, "scripts/workflow/tick", labels=("workflow", "priority:+medium"))
    _ticket(team, 8, "scripts/workflow")
    moment = _EVENING
    _decide(team, "start", "8", at=moment)
    _decide(team, "start", "8", "--after", "7", at=moment)
    _decide(
        team,
        "answer",
        "7",
        "--area",
        "product",
        "--question",
        "the limit",
        "--reply",
        "ten",
        "--meaning",
        "ten a day",
        at=moment,
    )
    later = {"WORKFLOW_NOW": str(moment + 7800), "TZ": "America/New_York"}

    done = team.run_script("wake", env=later)

    assert done.returncode == 0, done.stderr
    [prompt] = team.sent()
    assert "8 held after 7: shares scripts/workflow/tick (2h10m)" in prompt
    assert 'answered 7 (product): the limit: "ten"' in prompt
    assert "start of 8 refused: shares scripts/workflow/tick with 7" in prompt

    # Told once: the next check-in names the hold again, the answer and the refusal not.
    (team.root / "home/.local/state/workflow/wake.told").unlink()
    again = team.run_script(
        "wake",
        env={"WORKFLOW_NOW": str(moment + 7800 + 1300), "TZ": "America/New_York"},
    )
    assert again.returncode == 0, again.stderr
    assert len(team.sent()) == 2
    assert "8 held after 7" in team.sent()[-1]
    assert "answered 7" not in team.sent()[-1]
    assert "refused" not in team.sent()[-1]


def test_a_check_in_not_delivered_keeps_its_answers_for_the_next(
    team: Workflow,
) -> None:
    _ticket(team, 7, "docs/one")
    moment = _EVENING
    _decide(
        team,
        "answer",
        "7",
        "--area",
        "product",
        "--question",
        "the limit",
        "--reply",
        "ten",
        "--meaning",
        "ten a day",
        at=moment,
    )
    (team.state / "send-fails").touch()
    night = {"TZ": "America/New_York"}
    assert team.run_script("wake", env={"WORKFLOW_NOW": str(moment + 60), **night}).returncode == 1
    (team.state / "send-fails").unlink()

    done = team.run_script("wake", env={"WORKFLOW_NOW": str(moment + 1300), **night})

    assert done.returncode == 0, done.stderr
    assert 'answered 7 (product): the limit: "ten"' in team.sent()[-1]


def test_the_metrics_count_episodes_within_their_bounds(team: Workflow) -> None:
    _ticket(team, 7, "scripts/workflow/tick", labels=("workflow", "priority:+medium"))
    _ticket(team, 8, "scripts/workflow")
    _ticket(team, 9, "scripts/workflow/wake")
    start = 1_790_000_000
    hour = 3600
    _decide(team, "start", "8", at=start)
    _decide(team, "hold", "8", "--after", "7", at=start)
    # The first hold ends when 7 closes, two hours on; the tick of a moment later sees it.
    team.set_pull_request(7, "CLOSED", team.git("rev-parse", "origin/main"))
    _decide(
        team,
        "restart",
        "9",
        "--session",
        "ticket-9",
        "--judgment",
        "x",
        "--message",
        "y",
        at=start + 2 * hour,
    )
    _ticket(team, 10, "scripts/workflow/wake", labels=("workflow", "priority:+medium"))
    _decide(team, "hold", "9", "--after", "10", at=start + 3 * hour)
    _decide(
        team,
        "answer",
        "9",
        "--area",
        "product",
        "--question",
        "q",
        "--reply",
        "r",
        "--meaning",
        "m",
        at=start + 3 * hour,
    )

    def metrics(since: int, until: int) -> dict:
        done = team.run_script(
            "decide",
            "--metrics",
            str(since),
            str(until),
            env={"WORKFLOW_NOW": str(start + 9 * hour)},
        )
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    whole = metrics(start - hour, start + 5 * hour)
    assert whole["kinds"]["start"] == {"done": 0, "refused": 1}
    assert whole["kinds"]["hold"] == {"done": 2, "refused": 0}
    assert whole["kinds"]["answer"] == {"done": 1, "refused": 0}
    # One episode of two hours, ended; one of three, open at five hours, counted to the bound.
    assert sorted(episode["hours"] for episode in whole["holds"]) == [2.0, 2.0]
    assert whole["answers"] == {"9": 1}

    late = metrics(start + hour, start + 4 * hour)
    assert sorted((e["number"], e["hours"]) for e in late["holds"]) == [
        (8, 1.0),
        (9, 1.0),
    ]
    assert late["kinds"]["start"] == {"done": 0, "refused": 0}

    after = metrics(start + 6 * hour, start + 7 * hour)
    assert [(e["number"], e["hours"]) for e in after["holds"]] == [(9, 1.0)]
    assert after["answers"] == {}


def test_the_daily_report_includes_the_decisions(workflow: Workflow) -> None:
    from test_workflow_metrics import AT, NOW, _github, _notes

    _github(workflow)
    _notes(workflow)
    hour = 3600
    record = {
        "rule": "For the orchestrator session",
        "evidence": [],
        "authority": "delegated",
        "owner_words": None,
        "session": "orchestrator-1",
    }

    def line(id: str, status: str, kind: str, number: int, at: int, **more: object) -> str:
        moment = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
        fields = {
            "id": id,
            "status": status,
            "kind": kind,
            "number": number,
            "at": moment,
        }
        return json.dumps({**fields, "choice": kind, **record, "details": {}, **more})

    folder = workflow.repo / ".local/workflow"
    (folder / "decisions.jsonl").write_text(
        "\n".join(
            [
                line(
                    "start-8-a",
                    "refused",
                    "start",
                    8,
                    NOW - 2 * hour,
                    reason="shares x with 7",
                ),
                line("hold-8-b", "done", "hold", 8, NOW - 30 * hour, details={"after": 7}),
                line(
                    "hold-8-b",
                    "ended",
                    "hold",
                    8,
                    NOW - 30 * hour,
                    details={"after": 7},
                    ended_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 27 * hour)),
                    ended_because="7 closed",
                ),
                line("answer-9-c", "done", "answer", 9, NOW - 3 * hour),
            ]
        )
        + "\n"
    )

    done = workflow.run_script("workflow-metrics", "--json", env=AT)

    assert done.returncode == 0, done.stderr
    measured = json.loads(done.stdout)
    day, week = measured["last_day"]["decisions"], measured["last_days"]["decisions"]
    assert day["kinds"]["start"] == {"done": 0, "refused": 1}
    assert day["holds"] == []
    assert week["kinds"]["hold"] == {"done": 1, "refused": 0}
    assert [(e["number"], e["hours"]) for e in week["holds"]] == [(8, 3.0)]
    assert week["answers"] == {"9": 1}

    report = workflow.run_script("workflow-metrics", env=AT)

    assert report.returncode == 0, report.stderr
    assert "## Decisions" in report.stdout
    assert "| start | 0 done, 1 refused | 0 done, 1 refused |" in report.stdout
    assert "| answers per ticket | 9: 1 | 9: 1 |" in report.stdout
    assert "| hold episodes | none | 8 after 7, 3.0 h |" in report.stdout


def test_an_interrupted_decision_on_a_pull_request_closed_meanwhile_is_closed_as_refused(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two")
    _ticket(team, 9, "docs/three")
    (team.state / "fail-gh-patch").touch()
    words = ("--question", "q", "--reply", "r", "--meaning", "m")
    assert _decide(team, "answer", "8", "--area", "product", *words).returncode == 3
    (team.state / "fail-gh-patch").unlink()
    team.set_pull_request(8, "CLOSED", team.git("rev-parse", "origin/main"))

    first = _decide(
        team, "restart", "9", "--session", "ticket-9", "--judgment", "x", "--message", "y"
    )
    second = _decide(
        team, "restart", "9", "--session", "ticket-9", "--judgment", "x", "--message", "y"
    )

    assert first.returncode == second.returncode == 0, first.stderr
    [answer] = [record for record in _records(team) if record["kind"] == "answer"]
    assert answer["status"] == "refused"
    assert answer["reason"] == "checked again: 8 is not an open pull request"


def test_a_delegated_start_obeys_hold_all_and_never_labels_a_running_ticket(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two")
    _ticket(team, 9, "docs/three")
    team.running("ticket-9")
    (team.repo / ".local/workflow").mkdir(parents=True, exist_ok=True)
    plan = team.repo / ".local/workflow/start-plan"
    plan.write_text("hold all\n")

    held = _decide(team, "start", "8")
    plan.unlink()
    running = _decide(team, "start", "9")

    assert held.returncode == running.returncode == 1
    assert "hold all in start-plan" in held.stderr
    assert "9 runs already" in running.stderr
    plan.write_text("hold all\n")
    assert _decide(team, "start", "8", "--owner", "start 8").returncode == 0
    assert _labelled(team) == ["8"]


def test_a_hold_ends_when_its_own_ticket_starts(team: Workflow) -> None:
    _ticket(team, 7, "scripts/workflow/tick", labels=("workflow", "priority:+medium"))
    _ticket(team, 8, "scripts/workflow")
    assert _decide(team, "hold", "8", "--after", "7").returncode == 0

    assert _decide(team, "start", "8", "--owner", "start 8 now").returncode == 0
    _decide(team, "restart", "7", "--session", "ticket-7", "--judgment", "x", "--message", "y")

    [hold] = [record for record in _records(team) if record["kind"] == "hold"]
    assert hold["status"] == "ended"
    assert hold["ended_because"] == "8 started"
    assert "8 after 7" not in _plan(team)


def test_an_answer_whose_reply_is_not_the_owners_words_is_refused(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")

    done = _decide(
        team,
        "answer",
        "8",
        "--area",
        "product",
        "--question",
        "q",
        "--meaning",
        "m",
        "--reply",
        "yes",
        "--owner",
        "no",
    )

    assert done.returncode == 1
    assert "--reply differs from --owner" in done.stderr


def test_the_metrics_read_iso_times(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    moment = 1_790_000_000
    _decide(
        team,
        "restart",
        "8",
        "--session",
        "ticket-8",
        "--judgment",
        "x",
        "--message",
        "y",
        at=moment,
    )

    inside = team.run_script(
        "decide",
        "--metrics",
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment - 60)),
        time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(moment + 60)),
    )
    before = team.run_script("decide", "--metrics", "2020-01-01T00:00:00", "2020-01-02T00:00:00")

    assert inside.returncode == before.returncode == 0, inside.stderr
    assert json.loads(inside.stdout)["kinds"]["restart"] == {"done": 1, "refused": 0}
    assert json.loads(before.stdout)["kinds"]["restart"] == {"done": 0, "refused": 0}


def test_a_recorded_codex_choice_on_a_cloud_built_ticket_starts_codex(team: Workflow) -> None:
    team.add_pull_request(7, labels=("workflow", "priority:+medium"))
    (team.repo / ".local/workflow").mkdir(parents=True, exist_ok=True)
    (team.repo / ".local/workflow/cloud-built-7").touch()

    assert _decide(team, "program", "7", "codex").returncode == 0
    ticked = team.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "claude"})

    assert ticked.returncode == 0, ticked.stderr
    assert team.is_running("codex-7")
    assert not (team.repo / ".local/workflow/cloud-7").exists()


def test_an_allowance_needs_two_rounds_sent_back(team: Workflow) -> None:
    _ticket(team, 8, "docs/two", draft=False, labels=("workflow", "priority:+medium"))
    (team.state / "reviews-8.json").write_text(
        json.dumps(
            [
                {"body": "<!-- workflow-review round=1 result=sent-back --> one"},
                {"body": "<!-- workflow-review round=2 result=passed --> two"},
            ]
        )
    )

    done = _decide(team, "allowance", "8", "--round", "3", "--owner", "one more round")

    assert done.returncode == 1
    assert "1 round sent back: an allowance is needed only after two" in done.stderr
    assert not (team.repo / ".local/workflow/review-8.allow-round").exists()


def test_a_start_again_of_a_started_ticket_is_told_as_already_started(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    assert _decide(team, "start", "8", at=_EVENING).returncode == 0

    again = _decide(team, "start", "8", at=_EVENING + 60)

    assert again.returncode == 1
    assert "already started: 8 has the label workflow" in again.stderr
    assert "refused" not in again.stderr
    later = {"WORKFLOW_NOW": str(_EVENING + 1300), "TZ": "America/New_York"}
    assert team.run_script("wake", env=later).returncode == 0
    [prompt] = team.sent()
    assert "8 already started" in prompt
    assert "start of 8 refused" not in prompt


def test_a_stalled_fetch_ends_the_decision_as_files_not_known(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    real = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True, check=True
    ).stdout.strip()
    team.fake(
        "git",
        "#!/bin/bash\n"
        'for word in "$@"; do [ "$word" = fetch ] && { echo x >> "@STATE@/fetches"; '
        "exec /usr/bin/sleep 300; }; done\n"
        f'exec {real} "$@"\n',
    )
    started = time.monotonic()

    done = _decide(team, "start", "8", at=_EVENING)

    assert time.monotonic() - started < 25
    assert done.returncode == 1
    assert "its files are not known yet" in done.stderr
    # One fetch timed out: the call tried no other.
    assert (team.state / "fetches").read_text().count("x") == 1


def test_owner_words_left_blank_are_no_word_of_his(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")

    done = _decide(
        team, "answer", "8", "--area", "money", "--question", "q", "--meaning", "m", "--owner", " "
    )

    assert done.returncode == 1
    assert "only the owner answers" in done.stderr


def test_a_start_again_of_a_started_ticket_marked_ready_is_told_as_already_started(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two", draft=False, labels=("workflow", "priority:+medium"))

    again = _decide(team, "start", "8")

    assert again.returncode == 1
    assert "already started: 8 has the label workflow" in again.stderr
    assert _records(team)[-1]["details"] == {"already_started": True}
