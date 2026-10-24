"""Tries to break `decide` as an orchestrator uses it every day, and with the failures it meets:
a lock held too long, two calls at once, a GitHub call lost or late, a missing file."""

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_decisions import (
    _EVENING,
    _GH,
    _decide,
    _labelled,
    _lines,
    _records,
    _ticket,
    _usage,
)
from workflow_harness import Workflow

_CODEX = "Worker: Codex (a bug fix with no new UI)"


@pytest.fixture
def team(workflow: Workflow) -> Workflow:
    """As in test_decisions: a registered orchestrator, room in the usage window, body edits."""
    workflow.orchestrator("idle", name="orchestrator-1")
    _usage(workflow, 40)
    usual = workflow.bin / "gh"
    (workflow.state / "gh-usual").write_text(usual.read_text())
    workflow.fake("gh", _GH)
    return workflow


def _answer(team: Workflow, number: int, question: str, reply: str, meaning: str, **kw):
    words = ["answer", str(number), "--area", kw.pop("area", "product")]
    words += ["--question", question, "--meaning", meaning]
    if "owner" in kw:
        words += ["--owner", kw.pop("owner")]
    else:
        words += ["--reply", reply]
    return _decide(team, *words, **kw)


def _common(team: Workflow) -> Path:
    return Path(team.git("rev-parse", "--path-format=absolute", "--git-common-dir"))


def _popen_decide(team: Workflow, *words: str, at: int) -> subprocess.Popen:
    """`decide` in a process group of its own, so a test can end all of it."""
    return subprocess.Popen(
        ["bash", str(team.script.parent / "decide"), *words],
        cwd=team.repo,
        env={**team.env, "WORKFLOW_NOW": str(at)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


# 1. The daily flows.


def test_a_delegated_answer_lands_in_the_description_and_survives_a_restart(
    team: Workflow,
) -> None:
    """An answer under the standing delegation goes to the description, not chat alone, and a
    new orchestrator session after a restart still finds it, with its reply word and ticket."""
    _ticket(team, 8, "docs/two")
    _ticket(team, 9, "docs/three")
    done = _answer(
        team, 8, "which of the two layouts", "2", "the compact one", at=_EVENING
    )
    assert done.returncode == 0, done.stderr
    # The orchestrator restarts under another name.
    team.orchestrator("idle", name="orchestrator-2")
    later = _answer(
        team, 9, "keep the old flag", "no", "the flag goes", at=_EVENING + 3600
    )
    assert later.returncode == 0, later.stderr

    body = (team.state / "body-8").read_text()
    assert '"2", the compact one.' in body.split("## Answers given for the owner", 1)[1]
    delegated = [
        (r["number"], r["details"]["reply"], r["session"])
        for r in _records(team)
        if r["kind"] == "answer"
        and r["authority"] == "delegated"
        and r["status"] == "done"
    ]
    assert delegated == [(8, "2", "orchestrator-1"), (9, "no", "orchestrator-2")]


def test_a_hand_written_codex_line_is_kept_once_and_removed_by_a_later_choice(
    team: Workflow,
) -> None:
    """Descriptions already carry the hand-written line; a recorded choice never doubles it, and
    `claude` takes it away so the next worker is Claude's."""
    team.add_pull_request(7, labels=("workflow", "priority:+medium"))
    (team.state / "body-7").write_text(
        f"Fix the crash.\r\n\r\n{_CODEX}  \r\n\r\nAnother note.\r\n"
    )

    assert _decide(team, "program", "7", "codex").returncode == 0
    body = (team.state / "body-7").read_text()
    assert [line.strip() for line in body.splitlines()].count(_CODEX) == 1
    assert "Another note." in body.splitlines()

    assert _decide(team, "program", "7", "claude").returncode == 0
    body = (team.state / "body-7").read_text()
    assert _CODEX not in body
    assert "Another note." in body.splitlines()

    ticked = team.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "claude"})
    assert ticked.returncode == 0, ticked.stderr
    assert team.is_running("ticket-7")
    assert not team.is_running("codex-7")


def test_a_recorded_claude_choice_on_a_codex_built_ticket_routes_its_next_worker(
    team: Workflow,
) -> None:
    """The record says `claude for 7`, done. The next worker must then be Claude's, or the
    decision must be refused: record and act may not drift apart."""
    team.add_pull_request(7, labels=("workflow", "priority:+medium"))
    (team.repo / ".local/workflow").mkdir(parents=True, exist_ok=True)
    (team.repo / ".local/workflow/codex-built-7").touch()

    done = _decide(team, "program", "7", "claude")
    ticked = team.run_script("tick", env={"WORKFLOW_WORKER_PROGRAM": "claude"})

    assert ticked.returncode == 0, ticked.stderr
    assert done.returncode == 0, done.stderr
    assert _records(team)[-1]["choice"] == "claude for 7"
    assert not team.is_running("codex-7"), (
        "recorded claude for 7, the tick started Codex"
    )
    assert team.is_running("ticket-7")


def test_an_allowance_reads_every_page_and_counts_only_round_markers_at_the_start(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two", draft=False, labels=("workflow", "priority:+medium"))
    pages = [
        [
            {"body": "<!-- workflow-review round=1 result=sent-back --> one"},
            {"body": "Codex review: looks fine"},
        ],
        [
            {"body": "<!-- workflow-review round=2 result=sent-back --> two"},
            {
                "body": "quoting an old one: <!-- workflow-review round=7 result=passed -->"
            },
        ],
    ]
    (team.state / "reviews-8.json").write_text(
        "".join(json.dumps(page) for page in pages)
    )
    allow = team.repo / ".local/workflow/review-8.allow-round"

    wrong = _decide(
        team, "allowance", "8", "--round", "2", "--owner", "go on, one more round"
    )
    assert wrong.returncode == 1
    assert not allow.exists()
    done = _decide(
        team, "allowance", "8", "--round", "3", "--owner", "go on, one more round"
    )
    assert done.returncode == 0, done.stderr
    assert allow.read_text() == "3\n"
    assert stat.S_IMODE(allow.stat().st_mode) == 0o600


def test_an_allowance_for_a_pull_request_that_needs_none_is_refused(
    team: Workflow,
) -> None:
    """An allowance written for the wrong pull request: 9 had one round sent back and needs no
    allowance, so the call is most likely meant for another one."""
    _ticket(team, 9, "docs/three", draft=False, labels=("workflow", "priority:+medium"))
    (team.state / "reviews-9.json").write_text(
        json.dumps([{"body": "<!-- workflow-review round=1 result=sent-back --> one"}])
    )

    done = _decide(
        team, "allowance", "9", "--round", "2", "--owner", "one more round for it"
    )

    assert done.returncode == 1, (
        "an allowance for a pull request that needs none was written"
    )
    assert not (team.repo / ".local/workflow/review-9.allow-round").exists()


def test_owner_words_left_empty_are_not_the_owners_word(team: Workflow) -> None:
    """`--owner ""`, as when a variable the orchestrator meant to fill was empty."""
    _ticket(team, 8, "docs/two", draft=False, labels=("workflow", "priority:+medium"))
    (team.state / "reviews-8.json").write_text(
        json.dumps(
            [
                {"body": "<!-- workflow-review round=1 result=sent-back --> one"},
                {"body": "<!-- workflow-review round=2 result=sent-back --> two"},
            ]
        )
    )
    _ticket(team, 9, "docs/three", extra="\n\nStart: owner only")

    allowance = _decide(team, "allowance", "8", "--round", "3", "--owner", "")
    start = _decide(team, "start", "9", "--owner", " ")

    assert allowance.returncode == 1, "empty words were taken as the owner's word"
    assert start.returncode == 1, "blank words were taken as the owner's word"
    assert _labelled(team) == []


def test_a_start_plan_typo_cannot_happen_through_decide(team: Workflow) -> None:
    """A hold after a ticket that is not open is refused; the line `decide` writes is one the
    pick understands, and the pick names it as the reason the ticket waits."""
    _ticket(team, 7, "scripts/workflow/tick", labels=("workflow", "priority:+medium"))
    _ticket(team, 8, "scripts/workflow")
    (team.repo / ".local/workflow").mkdir(parents=True, exist_ok=True)
    (team.repo / ".local/workflow/start-plan").write_text(
        "hold 12\r\n609 after 605\r\n"
    )

    typo = _decide(team, "hold", "8", "--after", "77")
    assert typo.returncode == 1
    assert "77 is not an open pull request" in typo.stderr
    assert (
        team.repo / ".local/workflow/start-plan"
    ).read_bytes() == b"hold 12\r\n609 after 605\r\n"

    assert _decide(team, "hold", "8", "--after", "7").returncode == 0
    sys.path.insert(0, str(team.script.parent))
    import ticket_starts

    _, held, after, unclear = ticket_starts.read_plan(
        team.repo / ".local/workflow/start-plan"
    )
    assert unclear == []
    assert after["8"] == {"7"}
    assert held == {"12"}
    assert (
        stat.S_IMODE((team.repo / ".local/workflow/start-plan").stat().st_mode) == 0o600
    )


def test_a_label_never_lands_on_the_wrong_pull_request(team: Workflow) -> None:
    _ticket(team, 7, "docs/one", draft=False, labels=("priority:+medium",))
    _ticket(team, 8, "docs/two", labels=("workflow", "priority:+medium"))
    _ticket(team, 9, "docs/three", labels=())

    for number in ("7", "8", "9", "404"):
        refused = _decide(team, "start", number)
        assert refused.returncode == 1, (number, refused.stderr)
    for number in ("7", "8", "9"):
        refused = _decide(team, "start", number, "--owner", "start it now")
        assert refused.returncode == 1, (number, refused.stderr)
    assert _labelled(team) == []


def test_owner_words_in_another_language_round_trip_without_a_utf8_locale(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two")
    env_c = {"LANG": "C", "LC_ALL": "C", "WORKFLOW_NOW": str(_EVENING)}
    done = team.run_script(
        "decide",
        "answer",
        "8",
        "--area",
        "money",
        "--question",
        "the monthly cap",
        "--meaning",
        "twenty",
        "--owner",
        "да, двадцать — не больше",
        env=env_c,
    )

    assert done.returncode == 0, done.stderr
    body = (team.state / "body-8").read_bytes().decode("utf-8")
    assert '"да, двадцать — не больше", twenty.' in body
    assert _records(team)[-1]["owner_words"] == "да, двадцать — не больше"


def test_words_with_shell_characters_are_written_as_text(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    marker = team.root / "ran"
    nasty = f'$(touch {marker}) `touch {marker}` ; touch {marker} " -->'

    done = _answer(team, 8, nasty, nasty, "kept as text", at=_EVENING)
    bad_session = _decide(
        team,
        "restart",
        "8",
        "--session",
        "ticket-8;touch x",
        "--judgment",
        "j",
        "--message",
        "m",
    )

    assert done.returncode == 0, done.stderr
    assert not marker.exists()
    assert nasty in (team.state / "body-8").read_text()
    assert bad_session.returncode == 1


def test_the_files_decide_writes_are_the_owners_alone_whatever_the_umask(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two")
    _ticket(team, 7, "docs/one", labels=("workflow", "priority:+medium"))
    old = os.umask(0)
    try:
        assert _decide(team, "program", "8", "codex").returncode == 0
        assert _answer(team, 7, "q", "r", "m", at=_EVENING).returncode == 0
    finally:
        os.umask(old)
    notes = team.repo / ".local/workflow"
    for name in ("decisions.jsonl", "worker-program-8"):
        assert stat.S_IMODE((notes / name).stat().st_mode) == 0o600, name
    leftovers = [p.name for p in notes.iterdir() if p.name.startswith(".body-")]
    assert leftovers == []


# 2. The failures it meets.


def test_two_answers_at_once_on_one_description_both_land(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    first = _popen_decide(
        team, "answer", "8", "--area", "product", "--question", "first question",
        "--reply", "1", "--meaning", "the first", at=_EVENING,
    )  # fmt: skip
    second = _popen_decide(
        team, "answer", "8", "--area", "product", "--question", "second question",
        "--reply", "2", "--meaning", "the second", at=_EVENING,
    )  # fmt: skip
    for call in (first, second):
        _, err = call.communicate(timeout=60)
        assert call.returncode == 0, err

    block = (
        (team.state / "body-8")
        .read_text()
        .split("## Answers given for the owner", 1)[1]
    )
    assert "first question" in block and "second question" in block


def test_two_starts_of_one_ticket_at_once_label_it_once(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    calls = [_popen_decide(team, "start", "8", at=_EVENING) for _ in range(2)]
    codes = sorted(call.wait(timeout=60) for call in calls)

    assert codes == [0, 1]
    assert _labelled(team) == ["8"]
    assert sorted(r["status"] for r in _records(team)) == ["done", "refused"]


def test_a_decide_and_the_ticks_pick_at_once_label_once(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    call = _popen_decide(team, "start", "8", at=_EVENING)
    ticked = team.run_script("tick")
    call.wait(timeout=60)

    assert ticked.returncode == 0, ticked.stderr
    assert _labelled(team) == ["8"]


def test_a_lock_held_too_long_fails_the_decision_cleanly_and_records_nothing(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two")
    with (_common(team) / "shipline-decide.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        here = team.script.parent
        code = (
            f"import sys; sys.path.insert(0, {str(here)!r}); import decisions;"
            "decisions.LOCK_SECONDS = 2; sys.exit(decisions.main(sys.argv[1:]))"
        )
        done = subprocess.run(
            [sys.executable, "-c", code, "start", "8"],
            cwd=team.repo,
            env={**team.env, "WORKFLOW_NOW": str(_EVENING)},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert done.returncode != 0
    assert "holds the lock of decide" in done.stderr
    assert _lines(team) == []
    assert _labelled(team) == []


def test_the_tick_leaves_its_starts_to_the_next_while_a_decision_holds_the_lock(
    team: Workflow,
) -> None:
    _ticket(team, 8, "docs/two")
    with (_common(team) / "shipline-decide.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        ticked = subprocess.run(
            ["bash", str(team.script.parent / "tick")],
            cwd=team.repo,
            env=team.env,
            capture_output=True,
            text=True,
            timeout=110,
            check=False,
        )
    assert ticked.returncode == 0, ticked.stderr
    assert time.monotonic() - started >= 59
    assert "a decision holds the lock of decide" in ticked.stdout
    assert _labelled(team) == []


def test_a_label_github_applied_but_reported_lost_is_not_told_as_refused(
    team: Workflow,
) -> None:
    """The label went on, the answer was lost (a timeout). The orchestrator calls again."""
    _ticket(team, 8, "docs/two")
    usual = (team.state / "gh-usual").read_text()
    lost = usual.replace(
        'echo "$number" >> "@STATE@/labelled"\n  exit 0'.replace(
            "@STATE@", str(team.state)
        ),
        (
            'echo "$number" >> "@STATE@/labelled"\n'
            '  [ ! -e "@STATE@/lose-once" ] || { rm "@STATE@/lose-once"; exit 1; }\n  exit 0'
        ).replace("@STATE@", str(team.state)),
    )
    assert lost != usual
    (team.state / "gh-usual").write_text(lost)
    (team.state / "lose-once").touch()

    first = _decide(team, "start", "8", at=_EVENING)
    assert first.returncode == 3
    again = _decide(team, "start", "8", at=_EVENING + 60)

    assert _labelled(team) == ["8"]
    starts = [line["status"] for line in _lines(team) if line["kind"] == "start"]
    # The interrupted one is finished as done; the repeated call is refused: already labelled.
    assert starts[:2] == ["pending", "done"]
    assert again.returncode == 1
    # What the next check-in says of ticket 8.
    later = {"WORKFLOW_NOW": str(_EVENING + 1300), "TZ": "America/New_York"}
    assert team.run_script("wake", env=later).returncode == 0
    [prompt] = team.sent()
    # It must not say only that the start of 8 was refused, while 8 did start.
    assert "start of 8 refused" not in prompt, prompt


def test_an_answer_github_applied_but_reported_lost_lands_once(team: Workflow) -> None:
    _ticket(team, 8, "docs/two")
    gh = (team.bin / "gh").read_text()
    (team.bin / "gh").write_text(
        gh.replace(
            'echo "$number" >> "' + str(team.state) + '/patched"\n  exit 0',
            'echo "$number" >> "'
            + str(team.state)
            + '/patched"\n  [ ! -e "'
            + str(team.state)
            + '/lose-once" ] || { rm "'
            + str(team.state)
            + '/lose-once"; exit 1; }\n  exit 0',
        )
    )
    (team.state / "lose-once").touch()

    assert _answer(team, 8, "the cap", "ten", "ten a day", at=_EVENING).returncode == 3
    again = _answer(team, 8, "the cap", "ten", "ten a day", at=_EVENING + 60)

    body = (team.state / "body-8").read_text()
    assert body.count('the cap: "ten"') == 1
    assert again.returncode == 1  # already in the description, said so


def test_a_hung_network_call_inside_decide_holds_the_lock_without_end(
    team: Workflow,
) -> None:
    """`decide start` fetches the heads of the tickets under its lock. A fetch that stalls has no
    time limit, so the lock stays held and every tick leaves its starts."""
    _ticket(team, 8, "docs/two")
    real = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True, check=False
    )
    team.fake(
        "git",
        "#!/bin/bash\n"
        'for word in "$@"; do [ "$word" = fetch ] && { touch "@STATE@/fetch-hung"; /usr/bin/sleep 300; }; done\n'
        f'exec {real.stdout.strip()} "$@"\n',
    )
    call = _popen_decide(team, "start", "8", at=_EVENING)
    started = time.monotonic()
    try:
        call.wait(timeout=25)
        finished = True
        print(
            "decide ended",
            call.returncode,
            round(time.monotonic() - started),
            call.stderr.read(),
        )
    except subprocess.TimeoutExpired:
        finished = False
    finally:
        os.killpg(call.pid, signal.SIGKILL)
    assert (team.state / "fetch-hung").exists()
    assert finished, (
        "decide still held its lock 25 s into a stalled fetch, with no time limit"
    )
