"""Cloud workers launch with a TTY and report once through ordinary workflow channels."""

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from workflow_harness import Workflow

_URL = "https://claude.ai/code/session_01Cloud?from=cli&m=0"
_NOW = 1792432800
_ENV = {"WORKFLOW_NOW": str(_NOW), "TZ": "UTC"}
_MARKER = "<!-- cloud-worker -->"


def _record(workflow: Workflow, number: int = 7) -> Path:
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True, exist_ok=True)
    record = notes / f"cloud-{number}"
    record.write_text(
        json.dumps(
            {
                "url": _URL,
                "started_at": _NOW - 600,
                "status": "working",
                "last_comment_id": 0,
                "last_comment_at": None,
            }
        )
    )
    return record


def _comment(identity: int, text: str, at: int = _NOW) -> dict[str, object]:
    return {
        "id": identity,
        "body": f"{_MARKER}\n{text}",
        "created_at": datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "html_url": f"https://github.com/example-org/example-app/pull/7#issuecomment-{identity}",
        # The worker posts from the owner's account, which may write.
        "user": {"login": "example-owner"},
    }


def _comments(workflow: Workflow, *comments: dict[str, object], number: int = 7) -> None:
    (workflow.state / f"comments-{number}.json").write_text(json.dumps([list(comments)]))


def _keys(workflow: Workflow) -> None:
    keys = workflow.root / "home/agents/.env"
    keys.parent.mkdir(parents=True)
    keys.write_text("PUSHOVER_TOKEN=testtoken\nPUSHOVER_USER=testuser\n")


@pytest.mark.parametrize("level", [(), ("high",)])
def test_cloud_start_records_the_printed_address_without_preparing_a_local_worktree(
    workflow: Workflow, level: tuple[str, ...]
) -> None:
    workflow.add_pull_request(7)
    _comments(workflow, _comment(8, "worker finished"))

    done = workflow.run("start", "worker", "7", *level, "cloud", env=_ENV)

    assert done.returncode == 0, done.stderr
    record = json.loads((workflow.repo / ".local/workflow/cloud-7").read_text())
    assert record["url"] == _URL
    assert record["last_comment_id"] == 8  # older sessions' comments are not replayed
    assert record["started_at"] == _NOW
    assert "cloud-7 started: " + _URL in done.stdout
    assert not workflow.worktree(7).exists()
    assert not workflow.is_running("cloud-7")
    assert workflow.calls("worktree-setup") == []
    arguments = (workflow.state / "cloud-args").read_bytes().decode().split("\0")[:-1]
    assert arguments[:3] == ["--effort", "high" if level else "medium", "--cloud"]
    assert len(arguments) == 4
    assert arguments[3].startswith("Implement pull request 7 by the rules of AGENTS.md")
    assert _MARKER in arguments[3]
    assert "separate unprivileged user" in arguments[3]
    assert "Push only to the pull request's branch" in arguments[3]
    assert "never skip or weaken the pre-push hook" in arguments[3]
    assert "start, first push, any blocker with the exact error" in arguments[3]
    assert "Owner only: yes or Owner only: no just before those words" in arguments[3]
    assert (
        "Never end your turn to wait for an answer: poll the pull request's comments every "
        "three minutes instead." in arguments[3]
    )


def test_the_cloud_worker_is_told_to_push_as_one_so_its_push_runs_cis_tests(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    _comments(workflow, _comment(8, "worker finished"))

    done = workflow.run("start", "worker", "7", "cloud", env=_ENV)

    assert done.returncode == 0, done.stderr
    sentence = (workflow.state / "cloud-args").read_bytes().decode().split("\0")[3]
    assert (
        "Export SHIPLINE_CLOUD_WORKER=1 in every shell before pushing: the pre-push hook then "
        "also runs the tests CI will run for this change, as "
        "shipline ci-tests-for-changes shows them; run each push in the background and wait "
        "for it, since each suite may take up to 25 minutes."
    ) in sentence
    assert (
        "When this container runs as root, also export SHIPLINE_CLOUD_TEST_USER naming the "
        "unprivileged user the tests run as, give that user the checkout with chown -R, and "
        "mark the checkout safe for git with git config --global --add safe.directory, as root "
        "and as that user."
    ) in sentence
    assert (
        "Only after the server suite timed out in a push, export "
        'SHIPLINE_CLOUD_SERVER_PART="1 of 4" (or another part) to run that part as CI parts '
        "it, and unset it once it is not needed."
    ) in sentence


def test_start_and_list_use_the_same_per_ticket_lock_as_local_workers(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True)
    with (notes / "start-7.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = workflow.run("start", "worker", "7", "cloud")
    assert done.returncode != 0
    assert not (notes / "cloud-7").exists()


@pytest.mark.parametrize("model", [(), ("medium", "codex"), ("cloud",)])
def test_a_cloud_record_prevents_another_start(workflow: Workflow, model: tuple[str, ...]) -> None:
    workflow.add_pull_request(7)
    record = _record(workflow)

    done = workflow.run("start", "worker", "7", *model)

    assert done.returncode != 0
    assert "cloud-7 is already recorded" in done.stderr
    assert record.exists()
    assert not workflow.is_running("ticket-7")


@pytest.mark.parametrize("failure", ["fail-cloud", "cloud-no-address"])
def test_a_failed_or_ambiguous_launch_keeps_its_reservation(
    workflow: Workflow, failure: str
) -> None:
    workflow.add_pull_request(7)
    (workflow.state / failure).touch()

    done = workflow.run("start", "worker", "7", "cloud")

    assert done.returncode != 0
    assert (workflow.repo / ".local/workflow/cloud-7").exists()
    assert "reservation kept" in done.stderr
    assert not workflow.worktree(7).exists()


@pytest.mark.parametrize("draft", [True, False])
def test_tick_starts_nothing_local_while_a_cloud_record_exists(
    workflow: Workflow, draft: bool
) -> None:
    workflow.add_pull_request(7, draft=draft)
    _record(workflow)

    done = workflow.run_script("tick", env=_ENV)

    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("review-7")


def test_list_shows_the_address_and_age_of_the_last_comment(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    _record(workflow)
    _comments(workflow, _comment(4, "Reading the task."))
    workflow.run_script("tick", env=_ENV)

    done = workflow.run("list", env={**_ENV, "WORKFLOW_NOW": str(_NOW + 300)})

    assert done.returncode == 0, done.stderr
    assert f"cloud-7  working  {_URL}  last comment: 5 min ago" in done.stdout


def test_list_names_a_record_it_cannot_read_and_nothing_without_records(
    workflow: Workflow,
) -> None:
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True)
    (notes / "cloud-events.pending").write_text("not a record\n")
    workflow.running("review-8")

    alone = workflow.run("list", env=_ENV)
    (notes / "cloud-9").write_text("{not json")
    (notes / "cloud-12").symlink_to(notes / "gone")
    named = workflow.run("list", env=_ENV)

    assert alone.returncode == named.returncode == 0, alone.stderr + named.stderr
    assert alone.stdout == "review-8  busy  0 MB held  0 MB in swap\n"
    assert named.stdout.splitlines()[1:] == [
        "cloud-12  unreadable  cloud record needs attention",
        "cloud-9  unreadable  cloud record needs attention",
    ]


def test_list_fails_when_reading_the_records_fails(workflow: Workflow) -> None:
    """The tick and the wake read a failed list as unread sessions, not as no cloud worker."""
    _record(workflow)
    # A module the reader imports, which the tests' copy holds compiled.
    module = workflow.script.with_name("github_budget.py")
    status = module.stat()
    text = module.read_text()
    assert text.count("MAX_SPACING = 0.0\n") == 1
    # Same size and same time: only the text tells the compiled copy is out of date.
    module.write_text(text.replace("MAX_SPACING = 0.0\n", "MAX_SPACING = 1/0\n"))
    os.utime(module, ns=(status.st_atime_ns, status.st_mtime_ns))

    done = workflow.run("list", env=_ENV)

    assert done.returncode == 1
    assert "ZeroDivisionError" in done.stderr


@pytest.mark.parametrize("labelled", [True, False])
def test_a_question_rings_once_and_is_named_in_the_wake_at_any_hour(
    workflow: Workflow, labelled: bool
) -> None:
    workflow.add_pull_request(7, labels=("workflow",) if labelled else ())
    _record(workflow)
    _keys(workflow)
    workflow.orchestrator("busy")
    _comments(workflow, _comment(4, 'Choose the recommended option? Reply "1".'))

    first = workflow.run_script("tick", env=_ENV)
    second = workflow.run_script("tick", env={**_ENV, "WORKFLOW_NOW": str(_NOW + 600)})

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    [push] = workflow.pushes()
    assert push.title == "cloud-7 needs you"
    assert push.url == _URL
    assert "recommended option" in push.message
    [wake] = workflow.sent()
    assert "asking the owner: cloud-7 (0 min)" in wake


def test_two_questions_in_one_second_are_each_relayed_once(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    _record(workflow)
    _keys(workflow)
    workflow.orchestrator("busy")
    _comments(workflow, _comment(4, "First question?"))
    workflow.run_script("tick", env=_ENV)
    _comments(workflow, _comment(4, "First question?"), _comment(5, "Second question?"))

    workflow.run_script("tick", env={**_ENV, "WORKFLOW_NOW": str(_NOW + 600)})

    assert len(workflow.pushes()) == len(workflow.sent()) == 2


@pytest.mark.parametrize("last", ['"1" or "2"', '"merge it"', "1 or 2", "**Which option?**"])
def test_the_phone_rings_for_the_owners_permitted_reply_formats(
    workflow: Workflow, last: str
) -> None:
    workflow.add_pull_request(7, labels=())
    _record(workflow)
    _keys(workflow)
    _comments(workflow, _comment(4, f"Choose the recommended option, or stop.\n\n{last}"))

    done = workflow.run_script("tick", env=_ENV)

    assert done.returncode == 0, done.stderr
    [push] = workflow.pushes()
    assert last in push.message
    assert (workflow.repo / ".local/workflow/cloud-7.asking").exists()


def test_failed_phone_delivery_is_retried_without_replaying_the_comment(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    record = _record(workflow)
    _keys(workflow)
    _comments(workflow, _comment(4, "Which option?"))
    (workflow.state / "pushover-fails").touch()
    workflow.run_script("tick", env=_ENV)
    assert json.loads(record.read_text())["last_comment_id"] == 4
    attempts = len(workflow.pushes())
    (workflow.state / "pushover-fails").unlink()

    workflow.run_script("tick", env=_ENV)
    workflow.run_script("tick", env=_ENV)

    assert len(workflow.pushes()) == attempts + 1


def test_a_blocker_is_told_once_and_retained_until_delivery(workflow: Workflow) -> None:
    workflow.add_pull_request(7, labels=())
    _record(workflow)
    workflow.orchestrator("busy")
    _comments(workflow, _comment(4, "Blocked: network policy denies semgrep.dev."))
    (workflow.state / "send-fails").touch()
    workflow.run_script("tick", env=_ENV)
    pending = workflow.repo / ".local/workflow/cloud-events.pending/cloud-7-4"
    assert pending.exists()
    (workflow.state / "send-fails").unlink()

    workflow.run_script("tick", env={**_ENV, "WORKFLOW_NOW": str(_NOW + 600)})
    workflow.run_script("tick", env={**_ENV, "WORKFLOW_NOW": str(_NOW + 1200)})

    assert not pending.exists()
    assert len(workflow.sent()) == 2  # failed delivery, then delivered
    assert "cloud-7: <!-- cloud-worker -->\nBlocked: network policy" in workflow.sent()[-1]
    assert workflow.pushes() == []


def test_finished_requires_ready_then_allows_the_normal_reviewer(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    record = _record(workflow)
    _comments(workflow, _comment(4, "worker finished"))

    first = workflow.run_script("tick", env=_ENV)

    assert first.returncode == 0, first.stderr
    assert record.exists()
    assert not workflow.is_running("ticket-7")
    workflow.add_pull_request(7, draft=False)
    second = workflow.run_script("tick", env=_ENV)

    assert second.returncode == 0, second.stderr
    assert not record.exists()
    assert workflow.is_running("review-7")
    assert workflow.pushes() == []


@pytest.mark.parametrize("body", ["worker finished\nShould I proceed?", "Still working."])
def test_only_the_last_line_can_finish_the_worker(workflow: Workflow, body: str) -> None:
    workflow.add_pull_request(7, draft=False)
    record = _record(workflow)
    _comments(workflow, _comment(4, body))

    workflow.run_script("tick", env=_ENV)

    assert record.exists()
    assert not workflow.is_running("review-7")


def test_claudes_automatic_attribution_does_not_hide_the_completion_line(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7, draft=False, labels=())
    record = _record(workflow)
    _comments(
        workflow,
        _comment(4, "worker finished\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_"),
    )

    workflow.run_script("tick", env=_ENV)

    assert not record.exists()


def test_pagination_and_unmarked_comments_cannot_skip_a_worker_report(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False, labels=())
    record = _record(workflow)
    pages = [
        [{"id": i, "body": "An ordinary comment"} for i in range(1, 101)],
        [_comment(101, "worker finished")],
    ]
    (workflow.state / "comments-7.json").write_text(json.dumps(pages))

    done = workflow.run_script("tick", env=_ENV)

    assert done.returncode == 0, done.stderr
    assert not record.exists()


def test_unreadable_comments_keep_the_record_and_prevent_a_duplicate_worker(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    record = _record(workflow)
    original = record.read_text()
    (workflow.state / "fail-gh-comments").touch()

    done = workflow.run_script("tick", env=_ENV)

    assert done.returncode != 0
    assert record.read_text() == original
    assert not workflow.is_running("ticket-7")


def test_an_outsiders_marked_comment_is_no_report(workflow: Workflow) -> None:
    """Anyone may comment on a public pull request; only an account that may write reports."""
    workflow.add_pull_request(7, labels=())
    record = _record(workflow)
    _keys(workflow)
    (workflow.state / "permission-example-outsider").write_text("read\n")
    fake = {**_comment(4, "Which option should I take?"), "user": {"login": "example-outsider"}}
    _comments(workflow, fake)

    done = workflow.run_script("tick", env=_ENV)

    assert done.returncode == 0, done.stderr
    value = json.loads(record.read_text())
    assert (value["status"], value["last_comment_at"]) == ("working", None)
    assert not (workflow.repo / ".local/workflow/cloud-7.asking").exists()
    assert workflow.pushes() == []


def test_a_new_progress_report_clears_the_old_owner_question(workflow: Workflow) -> None:
    workflow.add_pull_request(7, labels=())
    _record(workflow)
    _comments(workflow, _comment(4, "Which option?"))
    workflow.run_script("tick", env=_ENV)
    question = workflow.repo / ".local/workflow/cloud-7.asking"
    assert question.exists()
    _comments(workflow, _comment(4, "Which option?"), _comment(5, "Continuing after the answer."))

    workflow.run_script("tick", env=_ENV)

    assert not question.exists()


def _cloud_tick(workflow: Workflow, **extra: str) -> dict[str, str]:
    workflow.usage(20, datetime.fromtimestamp(_NOW + 7200, UTC).isoformat())
    return {**_ENV, "WORKFLOW_WORKER_PROGRAM": "cloud", **extra}


@pytest.mark.parametrize("setting", ["environment", "file", "environment overrides file"])
def test_tick_starts_cloud_from_either_setting(workflow: Workflow, setting: str) -> None:
    workflow.add_pull_request(7)
    env = _cloud_tick(workflow)
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True)
    if setting != "environment":
        (notes / "worker-program").write_text("claude" if "overrides" in setting else "cloud")
    if setting == "file":
        del env["WORKFLOW_WORKER_PROGRAM"]

    done = workflow.run_script("tick", env=env)
    again = workflow.run_script("tick", env=env)

    assert done.returncode == again.returncode == 0, done.stderr + again.stderr
    assert json.loads((notes / "cloud-7").read_text())["url"] == _URL
    assert (notes / "cloud-built-7").exists()
    assert len(workflow.calls("claude --effort medium")) == 1
    assert not workflow.worktree(7).exists()
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")


@pytest.mark.parametrize("scope", ["screen diff", "screen task", "unclear"])
@pytest.mark.parametrize("sent_back", [False, True])
def test_cloud_screen_exclusions_apply_to_new_and_sent_back_tickets(
    workflow: Workflow, scope: str, sent_back: bool
) -> None:
    workflow.add_pull_request(7)
    env = _cloud_tick(workflow)
    if scope == "screen diff":
        (workflow.state / "diff-7").write_text(
            "diff --git a/web/screen.tsx b/web/screen.tsx\n"
            "@@ -1 +1 @@\n-<button>Old</button>\n+<button>New</button>\n"
        )
    else:
        (workflow.state / "body-7").write_text(
            "Change the screen layout" if scope == "screen task" else "Fix this"
        )
    if sent_back:
        notes = workflow.repo / ".local/workflow"
        notes.mkdir(parents=True)
        (notes / "cloud-built-7").touch()
    done = workflow.run_script("tick", env=env)

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("ticket-7")
    assert not workflow.calls("tmux cloud-7 new-session")
    assert not workflow.is_running("codex-7")


@pytest.mark.parametrize("program", ["claude", "codex", "cloud"])
def test_sent_back_cloud_ticket_keeps_cloud_after_completion(
    workflow: Workflow, program: str
) -> None:
    workflow.add_pull_request(7)
    env = _cloud_tick(workflow)
    assert workflow.run_script("tick", env=env).returncode == 0
    _comments(workflow, _comment(4, "worker finished"))
    workflow.add_pull_request(7, draft=False, labels=())
    assert workflow.run_script("tick", env=env).returncode == 0
    assert not (workflow.repo / ".local/workflow/cloud-7").exists()
    workflow.add_pull_request(7)
    (workflow.state / "reviews-7.json").write_text(
        '[{"body":"<!-- workflow-review round=1 result=sent-back --> Fix the backend"}]'
    )
    done = workflow.run_script("tick", env={**env, "WORKFLOW_WORKER_PROGRAM": program})

    assert done.returncode == 0, done.stderr
    assert len(workflow.calls("claude --effort medium")) == 2
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")


@pytest.mark.parametrize(
    ("percent", "seconds", "allowed"),
    [
        (85, 7200, True),
        (86, 3601, False),
        (99, 3600, True),
        (99, 600, True),
    ],
)
def test_cloud_usage_boundaries(
    workflow: Workflow, percent: int, seconds: int, allowed: bool
) -> None:
    workflow.add_pull_request(7)
    env = _cloud_tick(workflow)
    workflow.usage(percent, datetime.fromtimestamp(_NOW + seconds, UTC).isoformat())

    done = workflow.run_script("tick", env=env)

    assert done.returncode == 0, done.stderr
    assert bool(workflow.calls("tmux cloud-7 new-session")) is allowed
    assert not workflow.is_running("ticket-7")


def test_unread_usage_waits_for_a_later_tick(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    done = workflow.run_script("tick", env={**_ENV, "WORKFLOW_WORKER_PROGRAM": "cloud"})
    assert done.returncode == 0, done.stderr
    assert not workflow.calls("tmux cloud-7 new-session")
    assert not (workflow.repo / ".local/workflow/cloud-7").exists()

    done = workflow.run_script("tick", env=_cloud_tick(workflow))
    assert done.returncode == 0, done.stderr
    assert len(workflow.calls("tmux cloud-7 new-session")) == 1


def test_cloud_start_ignores_local_memory_and_load(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.pressure_file.write_text("some avg10=99.00 avg60=99.00 avg300=99.00 total=0\n")
    workflow.loadavg_file.write_text("1000 1000 1000 1/100 1234\n")

    done = workflow.run_script("tick", env=_cloud_tick(workflow))

    assert done.returncode == 0, done.stderr
    assert len(workflow.calls("tmux cloud-7 new-session")) == 1


@pytest.mark.parametrize("failure", ["refuse-launch", "refuse-scope"])
def test_refused_launch_retries_under_the_same_reservation_and_reports_once(
    workflow: Workflow,
    failure: str,
) -> None:
    workflow.add_pull_request(7)
    env = _cloud_tick(workflow)
    refusal = workflow.state / f"{failure}-cloud-7"
    refusal.touch()
    workflow.orchestrator("busy")

    first = workflow.run_script("tick", env=env)
    record = workflow.repo / ".local/workflow/cloud-7"
    assert first.returncode != 0
    assert json.loads(record.read_text())["status"] == (
        "retryable" if failure == "refuse-launch" else "starting"
    )
    assert not workflow.calls("claude --effort medium")
    env = {**env, "WORKFLOW_NOW": str(_NOW + 600)}
    second = workflow.run_script("tick", env=env)
    assert second.returncode != 0
    [wake] = [message for message in workflow.sent() if "cloud-7: launch refused" in message]
    assert "cloud-7: launch refused" in wake
    refusal.unlink()

    third = workflow.run_script("tick", env=env)
    fourth = workflow.run_script("tick", env=env)

    assert third.returncode == fourth.returncode == 0, third.stderr + fourth.stderr
    assert json.loads(record.read_text())["url"] == _URL
    assert len(workflow.calls("claude --effort medium")) == 1
    assert sum("cloud-7: launch refused" in message for message in workflow.sent()) == 1
    assert not workflow.worktree(7).exists()


@pytest.mark.parametrize("failure", ["fail-cloud", "cloud-no-address"])
@pytest.mark.parametrize("source", ["saved output", "pane"])
def test_ambiguous_launch_only_recovers_its_address_without_a_second_session(
    workflow: Workflow, failure: str, source: str
) -> None:
    workflow.add_pull_request(7)
    env = _cloud_tick(workflow)
    (workflow.state / failure).touch()
    workflow.orchestrator("busy")
    assert workflow.run_script("tick", env=env).returncode != 0
    for turn in range(1, 3):
        done = workflow.run_script("tick", env={**env, "WORKFLOW_NOW": str(_NOW + turn * 600)})
        assert done.returncode == 0, done.stderr
    [wake] = [message for message in workflow.sent() if "cloud-7: no View address" in message]
    assert "no View address; reservation kept" in wake
    assert len(workflow.calls("claude --effort medium")) == 1
    assert len(workflow.calls("tmux cloud-7 new-session")) == 1
    if source == "saved output":
        (workflow.repo / ".local/workflow/cloud-7.launch").write_text(f"View: {_URL}\n")
    else:
        workflow.screen(f"View: {_URL}\n", "cloud-7")

    done = workflow.run_script("tick", env=env)

    assert done.returncode == 0, done.stderr
    assert json.loads((workflow.repo / ".local/workflow/cloud-7").read_text())["url"] == _URL
    assert (workflow.repo / ".local/workflow/cloud-built-7").exists()
    assert len(workflow.calls("claude --effort medium")) == 1


def test_ready_ticket_gets_local_reviewer_even_with_cloud_default(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    done = workflow.run_script("tick", env=_cloud_tick(workflow))
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")
    assert not workflow.calls("tmux cloud-7 new-session")


def test_cloud_default_preserves_a_codex_fix_round(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True)
    (notes / "codex-built-7").touch()

    done = workflow.run_script("tick", env=_cloud_tick(workflow))

    assert done.returncode == 0, done.stderr
    assert workflow.is_running("codex-7")
    assert workflow.is_running("ticket-7")
    assert not workflow.calls("tmux cloud-7 new-session")


def test_cloud_start_can_pass_a_local_start_waiting_for_memory(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    (workflow.state / "body-7").write_text("Change the screen layout")
    workflow.pressure_file.write_text("some avg10=99.00 avg60=99.00 avg300=99.00 total=0\n")

    done = workflow.run_script("tick", env=_cloud_tick(workflow))

    assert not workflow.is_running("ticket-7")
    assert len(workflow.calls("tmux cloud-8 new-session")) == 1, done.stderr


@pytest.mark.parametrize("draft", [True, False], ids=["worker", "reviewer"])
def test_held_local_starts_ask_github_nothing_more(workflow: Workflow, draft: bool) -> None:
    """Without a cloud start to make, a start waiting for memory ends the starts as before."""
    for number in (7, 8, 9):
        workflow.add_pull_request(number, draft=draft)
    workflow.pressure_file.write_text("some avg60=11.00\n")
    workflow.fake("sleep", '#!/bin/bash\necho "sleep $*" >> "@STATE@/calls.log"\nexit 91\n')

    workflow.run_script("tick", env=_ENV)

    assert len(workflow.calls("gh pr list --label workflow")) == 1


def _program(workflow: Workflow, number: int, text: str) -> None:
    """The orchestrator's record of one ticket's worker program."""
    notes = workflow.repo / ".local/workflow"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"worker-program-{number}").write_text(text)


@pytest.mark.parametrize("default", ["claude", "codex"])
def test_a_ticket_recorded_cloud_passes_a_start_held_for_memory(
    workflow: Workflow, default: str
) -> None:
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    (workflow.state / "body-7").write_text("Change the screen layout")
    _program(workflow, 8, "cloud\n")
    workflow.pressure_file.write_text("some avg10=99.00 avg60=99.00 avg300=99.00 total=0\n")

    done = workflow.run_script("tick", env=_cloud_tick(workflow, WORKFLOW_WORKER_PROGRAM=default))

    assert not workflow.is_running("ticket-7")
    assert len(workflow.calls("tmux cloud-8 new-session")) == 1, done.stderr
    assert not workflow.is_running("ticket-8")


@pytest.mark.parametrize("recorded", ["claude", "codex"])
def test_a_ticket_recorded_local_waits_behind_a_held_start_asking_github_nothing(
    workflow: Workflow, recorded: str
) -> None:
    workflow.add_pull_request(7)
    workflow.add_pull_request(8)
    (workflow.state / "body-7").write_text("Change the screen layout")
    _program(workflow, 8, recorded)
    workflow.pressure_file.write_text("some avg10=99.00 avg60=99.00 avg300=99.00 total=0\n")

    workflow.run_script("tick", env=_cloud_tick(workflow))

    assert len(workflow.calls("gh pr list --label workflow")) == 1
    assert not [call for call in workflow.calls("gh pr") if re.search(r"\b8\b", call)]
    assert not workflow.calls("tmux cloud-8 new-session")
    assert not workflow.is_running("ticket-8")
    assert not workflow.is_running("codex-8")


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("recorded", ["claude", "codex"])
def test_a_reservation_keeps_the_ticket_in_the_cloud_whatever_its_record(
    workflow: Workflow, recorded: str, held: bool
) -> None:
    if held:
        workflow.add_pull_request(6)
        (workflow.state / "body-6").write_text("Change the screen layout")
        workflow.pressure_file.write_text("some avg10=99.00 avg60=99.00 avg300=99.00 total=0\n")
    workflow.add_pull_request(7)
    record = _record(workflow)
    value = json.loads(record.read_text())
    value.update(url="", status="retryable")
    record.write_text(json.dumps(value))
    _program(workflow, 7, recorded)

    done = workflow.run_script("tick", env=_cloud_tick(workflow, WORKFLOW_WORKER_PROGRAM="claude"))

    assert len(workflow.calls("tmux cloud-7 new-session")) == 1, done.stderr
    assert json.loads(record.read_text())["url"] == _URL
    assert not workflow.is_running("ticket-7")
    assert not workflow.is_running("codex-7")


def test_retry_also_waits_for_usage_and_never_falls_back_locally(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    env = _cloud_tick(workflow)
    refusal = workflow.state / "refuse-launch-cloud-7"
    refusal.touch()
    assert workflow.run_script("tick", env=env).returncode != 0
    refusal.unlink()
    workflow.usage(99, datetime.fromtimestamp(_NOW + 7200, UTC).isoformat())

    done = workflow.run_script("tick", env={**env, "WORKFLOW_WORKER_PROGRAM": "claude"})

    assert done.returncode == 0, done.stderr
    assert not workflow.calls("claude --effort medium")
    assert not workflow.is_running("ticket-7")
    record = workflow.repo / ".local/workflow/cloud-7"
    assert json.loads(record.read_text())["status"] == "retryable"


def test_address_recovery_does_not_touch_a_manual_start_holding_the_lock(
    workflow: Workflow,
) -> None:
    workflow.add_pull_request(7)
    record = _record(workflow)
    value = json.loads(record.read_text())
    value.update(url="", status="starting")
    record.write_text(json.dumps(value))
    workflow.screen(f"View: {_URL}\n", "cloud-7")
    with (record.parent / "start-7.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = workflow.run_script("tick", env=_cloud_tick(workflow))
    assert done.returncode == 0, done.stderr
    assert json.loads(record.read_text())["url"] == ""
    assert not workflow.calls("tmux cloud-7 capture-pane")
    assert not workflow.calls("tmux cloud-7 new-session")

    done = workflow.run_script("tick", env=_cloud_tick(workflow))
    assert done.returncode == 0, done.stderr
    assert json.loads(record.read_text())["url"] == _URL


def test_existing_cloud_worker_keeps_provenance_for_its_fix_round(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False, labels=())
    record = _record(workflow)
    _comments(workflow, _comment(4, "worker finished"))
    env = _cloud_tick(workflow, WORKFLOW_WORKER_PROGRAM="claude")
    done = workflow.run_script("tick", env=env)
    assert done.returncode == 0, done.stderr
    assert not record.exists()
    assert (record.parent / "cloud-built-7").exists()
    workflow.add_pull_request(7)

    done = workflow.run_script("tick", env=env)

    assert done.returncode == 0, done.stderr
    assert len(workflow.calls("tmux cloud-7 new-session")) == 1
    assert not workflow.is_running("ticket-7")


_TMUX = shutil.which("tmux")
_STATUS = (
    "Status check: nothing has been pushed or reported for 30 minutes. If your question was "
    "answered, carry on; if something blocks you, report it as a question and carry on with "
    "what it does not block; never end your turn to wait."
)
_ANSWERED = (
    "Your question on pull request 7 was answered in its comments. Read the answer and carry on."
)
# A cloud session as `claude --cloud <address>` shows it: Claude's input box, under a reply.
# Each line typed into the box is recorded in `typed`. An `attach-*` file changes what it
# shows: an expired session, no box at all, a usage limit, or words already in the box.
_ATTACHED_CLAUDE = r"""#!/bin/bash
if [[ $# == 2 && $1 == --cloud ]]; then
  echo "$2" >> "@STATE@/attached"
  if [ -e "@STATE@/attach-expired" ]; then echo "This session has expired."; exit 1; fi
  if [ -e "@STATE@/attach-no-box" ]; then echo "Connecting..."; exec /bin/sleep 600; fi
  if [ -e "@STATE@/attach-deaf" ]; then
    trap '' HUP
    echo $$ > "@STATE@/deaf.pid"
    exec /bin/sleep 600
  fi
  draft=""
  [ ! -f "@STATE@/attach-draft" ] || draft=$(cat "@STATE@/attach-draft")
  rule=$(printf '─%.0s' {1..60})
  draw() {
    printf '\033[H\033[2J● Working on the ticket.\n'
    [ ! -e "@STATE@/attach-limit" ] ||
      printf "  ⎿  You've hit your session limit · resets 4pm (UTC)\n"
    # The box has room for the text to wrap; the cursor waits after the prompt.
    printf '\n%s\n> %s\0337\n\n\n%s\n  ? for shortcuts\0338' "$rule" "$draft" "$rule"
  }
  draw
  while IFS= read -r line; do
    printf '%s\n' "$line" >> "@STATE@/typed"
    draft=""
    draw
  done
  exit 0
fi
exec "@STATE@/../claude-harness" "$@"
"""


@pytest.fixture
def attachable(workflow: Workflow) -> Iterator[Workflow]:
    """The real `tmux`, and a `claude` that attaches to a cloud session and records the typing."""
    if _TMUX is None:
        pytest.skip("tmux is not installed")  # noqa: TID251
    # A socket path may be about 100 characters at most, so it cannot live in pytest's folder.
    sockets = Path(tempfile.mkdtemp(prefix="wf-"))
    (workflow.bin / "tmux").unlink()
    (workflow.bin / "claude").rename(workflow.root / "claude-harness")
    workflow.fake("claude", _ATTACHED_CLAUDE)
    workflow.env["TMUX_TMPDIR"] = str(sockets)
    workflow.orchestrator("busy")
    try:
        yield workflow
    finally:
        subprocess.run(
            [_TMUX, "-L", "wake-cloud-7", "kill-server"],
            env=workflow.env,
            capture_output=True,
            check=False,
        )
        shutil.rmtree(sockets, ignore_errors=True)


def _tick_at(workflow: Workflow, minutes: int, **env: str) -> None:
    done = workflow.run_script(
        "tick", env={**_ENV, "WORKFLOW_NOW": str(_NOW + minutes * 60), **env}
    )
    assert done.returncode == 0, done.stderr


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _wake_log(workflow: Workflow) -> list[str]:
    """The tick's lines of its wakes of cloud-7, among the lines of the wake's own passes."""
    return [
        line
        for line in _lines(workflow.wake_log)
        if re.fullmatch(r"[0-9]+ (woke cloud-7 .*|cloud-7 wake failed: .*)", line)
    ]


def _stuck(workflow: Workflow) -> list[str]:
    return [message for message in workflow.sent() if "stuck? cloud-7" in message]


def _pane_is_gone(workflow: Workflow) -> bool:
    assert _TMUX is not None
    asked = subprocess.run(
        [_TMUX, "-L", "wake-cloud-7", "has-session", "-t", "=wake-cloud-7"],
        env=workflow.env,
        capture_output=True,
        check=False,
    )
    return asked.returncode != 0


def test_a_quiet_cloud_worker_is_woken_once_and_then_named_once(attachable: Workflow) -> None:
    attachable.add_pull_request(7)
    _record(attachable)

    for minutes in (0, 29):
        _tick_at(attachable, minutes)
    assert not (attachable.state / "attached").exists()

    _tick_at(attachable, 30)

    assert _lines(attachable.state / "attached") == [_URL]
    assert _lines(attachable.state / "typed") == [_STATUS]
    assert _wake_log(attachable) == [f"{_NOW + 1800} woke cloud-7 (30 min quiet)"]
    assert _pane_is_gone(attachable)
    for minutes in (40, 59):
        _tick_at(attachable, minutes)
    assert _lines(attachable.state / "typed") == [_STATUS]
    assert _stuck(attachable) == []

    for minutes in (60, 70, 100):
        _tick_at(attachable, minutes)

    assert _lines(attachable.state / "typed") == [_STATUS]
    [stuck] = _stuck(attachable)
    assert "stuck? cloud-7 (60 min, woken once)" in stuck


@pytest.mark.parametrize("news", ["push", "report"])
def test_a_push_or_a_report_begins_a_new_quiet_stretch(attachable: Workflow, news: str) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _tick_at(attachable, 0)
    if news == "push":
        attachable.push_from_elsewhere(7)
    else:
        _comments(attachable, _comment(4, "First push.", at=_NOW + 1200))
    _tick_at(attachable, 20)

    _tick_at(attachable, 30)
    assert not (attachable.state / "attached").exists()
    _tick_at(attachable, 50)

    assert _lines(attachable.state / "typed") == [_STATUS]


def _answer(identity: int, body: str, login: str, minutes: int) -> dict[str, object]:
    at = datetime.fromtimestamp(_NOW + minutes * 60, UTC)
    return {
        "id": identity,
        "body": body,
        "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user": {"login": login},
    }


_ANSWERS = {
    "orchestrator": ("<!-- shipline-orchestrator -->\nTake the first option.", "write"),
    "writer": ("Take the first option.", "write"),
    "reader": ("Take the first option.", "read"),
    "outsider as orchestrator": ("<!-- shipline-orchestrator -->\nTake the first option.", "read"),
    "another role": ("<!-- shipline-review result=passed -->\nLooks right.", "write"),
    # GitHub's "Quote reply" copies the question, the connector's line under it included.
    "quote reply": (
        "> Which option should I take?\n> \n> ---\n"
        "> _Generated by [Claude Code](https://claude.ai/code)_\n\nThe first.",
        "write",
    ),
    "a command": ("@codex review\n\nCheck the security.", "write"),
    "the worker unmarked": (
        "Pushed.\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_",
        "write",
    ),
    "permission unread": ("Take the first option.", "unread"),
}


@pytest.mark.parametrize("answerer", list(_ANSWERS))
def test_an_open_question_holds_the_wake_until_its_answer_then_wakes_at_once(
    attachable: Workflow, answerer: str
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    question = _comment(4, "Which option should I take?")
    _comments(attachable, question)
    for minutes in (0, 30, 61, 95):
        _tick_at(attachable, minutes)
    assert not (attachable.state / "attached").exists()
    assert _stuck(attachable) == []
    body, permission = _ANSWERS[answerer]
    if permission == "unread":
        (attachable.state / "fail-gh-permission").touch()
    else:
        (attachable.state / "permission-example-account").write_text(permission + "\n")
    _comments(attachable, question, _answer(5, body, "example-account", 96))

    _tick_at(attachable, 97)
    _tick_at(attachable, 100)

    if answerer not in ("orchestrator", "writer", "quote reply"):
        assert not (attachable.state / "attached").exists()
        assert (attachable.repo / ".local/workflow/cloud-7.asking").exists()
        return
    assert _lines(attachable.state / "typed") == [_ANSWERED]
    assert _wake_log(attachable) == [f"{_NOW + 97 * 60} woke cloud-7 (question answered)"]
    assert not (attachable.repo / ".local/workflow/cloud-7.asking").exists()
    assert _pane_is_gone(attachable)
    # The answer's wake is its stretch's one wake.
    _tick_at(attachable, 127)
    _tick_at(attachable, 140)
    assert _lines(attachable.state / "typed") == [_ANSWERED]
    [stuck] = _stuck(attachable)
    assert "stuck? cloud-7 (31 min, woken once)" in stuck


def test_a_question_asked_again_waits_for_its_own_answer(attachable: Workflow) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    (attachable.state / "permission-example-account").write_text("write\n")
    first = [
        _comment(4, "Which option should I take?"),
        _answer(5, "The first.", "example-account", 1),
    ]
    _comments(attachable, *first)
    _tick_at(attachable, 2)
    assert _lines(attachable.state / "typed") == [_ANSWERED]
    again = [*first, _comment(6, "And which name?", at=_NOW + 300)]
    _comments(attachable, *again)

    for minutes in (6, 40, 80):
        _tick_at(attachable, minutes)
    assert _lines(attachable.state / "typed") == [_ANSWERED]
    _comments(attachable, *again, _answer(7, "The short one.", "example-account", 81))
    _tick_at(attachable, 82)

    assert _lines(attachable.state / "typed") == [_ANSWERED, _ANSWERED]


def test_unread_reports_hold_the_wake_for_that_tick(attachable: Workflow) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _tick_at(attachable, 0)
    (attachable.state / "fail-gh-comments").touch()
    done = attachable.run_script("tick", env={**_ENV, "WORKFLOW_NOW": str(_NOW + 1800)})
    assert done.returncode != 0
    assert not (attachable.state / "attached").exists()
    (attachable.state / "fail-gh-comments").unlink()

    _tick_at(attachable, 31)

    assert _lines(attachable.state / "typed") == [_STATUS]


def test_a_pane_an_interrupted_tick_left_is_replaced(attachable: Workflow) -> None:
    assert _TMUX is not None
    attachable.add_pull_request(7)
    _record(attachable)
    _tick_at(attachable, 0)
    subprocess.run(
        [_TMUX, "-L", "wake-cloud-7", "new-session", "-d", "-s", "wake-cloud-7", "/bin/sleep 600"],
        env=attachable.env,
        check=True,
    )

    _tick_at(attachable, 30)

    assert _lines(attachable.state / "typed") == [_STATUS]
    assert _pane_is_gone(attachable)


@pytest.mark.parametrize("case", ["ready", "removed"])
def test_a_ready_pull_request_or_a_removed_record_is_never_woken(
    attachable: Workflow, case: str
) -> None:
    attachable.add_pull_request(7, draft=case != "ready")
    record = _record(attachable)
    _tick_at(attachable, 0)
    if case == "removed":
        record.unlink()

    for minutes in (31, 61, 95):
        _tick_at(attachable, minutes)

    assert not (attachable.state / "attached").exists()
    assert _wake_log(attachable) == []
    assert _stuck(attachable) == []


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("no-box", "no input box within 2 s"),
        ("expired", "the attach ended before its input box appeared"),
        ("limit", "a usage limit is shown"),
        ("draft", "its input box holds text that was not sent; nothing was typed"),
    ],
)
def test_a_failed_attach_is_logged_once_never_retried_and_leaves_no_pane(
    attachable: Workflow, failure: str, reason: str
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    if failure == "draft":
        (attachable.state / "attach-draft").write_text("words of the owner")
    else:
        (attachable.state / f"attach-{failure}").touch()
    seconds = {"WORKFLOW_CLOUD_ATTACH_SECONDS": "2"}

    for minutes in (0, 30, 33, 36, 45, 60, 70):
        _tick_at(attachable, minutes, **seconds)

    assert _lines(attachable.state / "attached") == [_URL]
    assert not (attachable.state / "typed").exists()
    assert _wake_log(attachable) == [f"{_NOW + 1800} cloud-7 wake failed: {reason}"]
    assert _pane_is_gone(attachable)
    [stuck] = _stuck(attachable)
    assert "stuck? cloud-7 (60 min, its wake failed)" in stuck


@pytest.mark.parametrize(("minutes", "woken_at"), [(0, None), (45, 45)])
def test_the_configured_wait_moves_or_turns_off_the_wake(
    attachable: Workflow, minutes: int, woken_at: int | None
) -> None:
    config = attachable.repo / "shipline.json"
    config.write_text(
        json.dumps({**json.loads(config.read_text()), "cloud_wake_after_minutes": minutes})
    )
    attachable.add_pull_request(7)
    _record(attachable)

    for at in (0, 30, 44, 45, 61, 95):
        _tick_at(attachable, at)

    if woken_at is None:
        assert not (attachable.state / "attached").exists()
        assert _stuck(attachable) == []
        return
    assert _wake_log(attachable)[0] == f"{_NOW + woken_at * 60} woke cloud-7 (45 min quiet)"
    assert _lines(attachable.state / "typed") == [_STATUS.replace("30 minutes", "45 minutes")]


def _alive(pid: int) -> bool:
    """Whether the process runs: one ended but not yet reaped (a zombie) does not."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError, IndexError:
        return False
    return state != "Z"


def test_a_client_that_ignores_the_hang_up_does_not_outlive_its_pane(
    attachable: Workflow,
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    (attachable.state / "attach-deaf").touch()
    _tick_at(attachable, 0)

    _tick_at(attachable, 30, WORKFLOW_CLOUD_ATTACH_SECONDS="1")

    pid = int((attachable.state / "deaf.pid").read_text())
    try:
        assert _pane_is_gone(attachable)
        assert not _alive(pid), "the attached client outlived its pane"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_wake_ended_mid_attach_still_removes_its_pane(attachable: Workflow) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _tick_at(attachable, 0)
    (attachable.state / "attach-no-box").touch()
    tick = subprocess.Popen(
        ["bash", str(attachable.script.parent / "tick")],
        cwd=attachable.repo,
        env={**attachable.env, **_ENV, "WORKFLOW_NOW": str(_NOW + 1800)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while _pane_is_gone(attachable) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _pane_is_gone(attachable), "the wake never started its pane"

    subprocess.run(
        ["pkill", "-TERM", "-f", f"cloud_workers.py wake {attachable.repo} "], check=False
    )
    tick.wait(timeout=60)

    assert _pane_is_gone(attachable)


def test_the_owners_quote_reply_of_a_signed_question_wakes_the_worker_then_names_it_once(
    attachable: Workflow,
) -> None:
    """The breaker's case: the owner answers from the phone with "Quote reply", which copies the
    question and the connector's line under it. One quiet stretch after the wake, the worker
    is named once, as after any wake."""
    attachable.add_pull_request(7)
    _record(attachable)
    signed = "\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_"
    question = _comment(4, f"Which option should I take? Reply `first` or `second`.{signed}")
    _comments(attachable, question)
    _tick_at(attachable, 3)
    quoted = (
        "> Which option should I take? Reply `first` or `second`.\n> \n> ---\n"
        "> _Generated by [Claude Code](https://claude.ai/code)_\n\nfirst"
    )
    _comments(attachable, question, _answer(5, quoted, "example-owner", 10))

    for minutes in (12, 42, 72, 102):
        _tick_at(attachable, minutes)

    assert _lines(attachable.state / "typed") == [_ANSWERED]
    assert not (attachable.repo / ".local/workflow/cloud-7.asking").exists()
    assert len(_stuck(attachable)) == 1


_ORCHESTRATOR = "<!-- shipline-orchestrator -->"
_QUESTION = (
    "How should a failed upload be retried?\n\n"
    "1. Retry once after a minute (recommended)\n"
    "2. Retry three times\n"
    "3. Never retry\n\n"
    "Owner only: {owner}\n"
    "Reply 1, 2 or 3."
)


def _question(
    workflow: Workflow, owner: str = "no", text: str = _QUESTION, login: str = "example-owner"
) -> dict[str, object]:
    """The worker's question, from an account that may write to the repository."""
    (workflow.state / "permission-example-owner").write_text("write\n")
    comment = _comment(4, text.format(owner=owner))
    comment["user"] = {"login": login}
    return comment


def _answers(workflow: Workflow) -> list[str]:
    """The bodies of the comments the tick posted on pull request 7."""
    pages = json.loads((workflow.state / "comments-7.json").read_text())
    return [
        str(comment["body"])
        for page in pages
        for comment in page
        if str(comment["body"]).startswith(f"{_ORCHESTRATOR} answered for the owner")
    ]


def _answered_in_log(workflow: Workflow) -> list[str]:
    """The tick's lines of its answers, among the lines of the wake's own passes."""
    return [
        line
        for line in _lines(workflow.wake_log)
        if re.fullmatch(r"[0-9]+ answered for the owner: .*", line)
    ]


def _set_config(workflow: Workflow, **values: int) -> None:
    config = workflow.repo / "shipline.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), **values}))


def test_a_question_not_owner_only_is_answered_once_after_15_minutes_then_woken(
    attachable: Workflow,
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(attachable, _question(attachable))

    _tick_at(attachable, 14)
    assert _answers(attachable) == []
    assert not (attachable.state / "typed").exists()

    _tick_at(attachable, 15)
    _tick_at(attachable, 25)

    assert _answers(attachable) == [
        f"{_ORCHESTRATOR} answered for the owner: 1\n\n> 1. Retry once after a minute (recommended)"
    ]
    assert _answered_in_log(attachable) == [f'{_NOW + 900} answered for the owner: cloud-7, "1"']
    assert _lines(attachable.state / "typed") == [_ANSWERED]
    assert _wake_log(attachable) == [f"{_NOW + 900} woke cloud-7 (question answered)"]
    # Answered, it no longer waits for the owner, and the check-in lists it for him to overrule.
    assert not (attachable.repo / ".local/workflow/cloud-7.asking").exists()
    [told] = [message for message in attachable.sent() if "answered for the owner" in message]
    assert 'answered for the owner: cloud-7, "1" (18:15)' in told


@pytest.mark.parametrize(
    ("text", "reply", "quoted"),
    [
        (
            "Rename the flag?\n\n"
            '"keep it": keep the old name\n"rename it": use the new name\n\n'
            'Owner only: no\nReply "keep it" or "rename it".',
            "keep it",
            '\n\n> "keep it": keep the old name',
        ),
        (
            "Which order?\n\n- **2** Newest first\n- **1** Oldest first\n\n"
            "**Owner only:** no\nReply 2 or 1.",
            "2",
            "\n\n> - **2** Newest first",
        ),
        (
            "Done so far:\n1. Added the parser\n2. Tests green\n\nWhich order?\n"
            "1. Oldest first\n2. Newest first\n\nOwner only: no\nReply 1 or 2.",
            "1",
            "\n\n> 1. Oldest first",
        ),
        ('Rename the flag?\n\nOwner only: no\nReply "go ahead" or "wait".', "go ahead", ""),
    ],
    ids=["quoted words", "in bold", "a numbered list above the choices", "no choice line"],
)
def test_the_answer_is_the_first_reply_word_after_the_line(
    attachable: Workflow, text: str, reply: str, quoted: str
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(attachable, _question(attachable, text=text))

    _tick_at(attachable, 15)

    assert _answers(attachable) == [f"{_ORCHESTRATOR} answered for the owner: {reply}{quoted}"]
    assert _answered_in_log(attachable) == [
        f'{_NOW + 900} answered for the owner: cloud-7, "{reply}"'
    ]
    # The wake's check-in reads it, and moves it once told.
    [answer] = (attachable.repo / ".local/workflow").glob("answered-for-owner.*/*.json")
    assert json.loads(answer.read_text()) == {"name": "cloud-7", "reply": reply, "at": _NOW + 900}


@pytest.mark.parametrize(
    ("owner", "text", "named"),
    [
        ("yes", _QUESTION, None),
        ("missing", _QUESTION.replace("Owner only: {owner}\n", ""), "without the Owner only line"),
        ("no", _QUESTION.replace("Reply 1, 2 or 3.", "1, 2 or 3?"), "without a reply word"),
        ("no", _QUESTION.replace("Reply 1, 2 or 3.", 'Reply "/clear".'), "without a reply word"),
    ],
    ids=["owner only", "no line", "no reply word", "a command"],
)
def test_a_question_kept_for_the_owner_or_unclear_is_left(
    attachable: Workflow, owner: str, text: str, named: str | None
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(attachable, _question(attachable, owner=owner, text=text))

    for minutes in (15, 60, 95):
        _tick_at(attachable, minutes)

    assert _answers(attachable) == []
    assert _answered_in_log(attachable) == []
    assert not (attachable.state / "attached").exists()
    told = [message for message in attachable.sent() if "owner question" in message]
    if named is None:
        assert told == []
    else:
        [message] = told
        assert f"owner question {named}: cloud-7" in message


@pytest.mark.parametrize("answerer", ["owner", "orchestrator"])
def test_an_earlier_answer_wins(attachable: Workflow, answerer: str) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    answer = {
        "id": 5,
        "body": "1" if answerer == "owner" else f"{_ORCHESTRATOR}\n2",
        "created_at": "2026-10-19T18:05:00Z",
        "user": {"login": "example-owner"},
    }
    _comments(attachable, _question(attachable), answer)

    _tick_at(attachable, 15)

    assert _answers(attachable) == []
    assert _answered_in_log(attachable) == []


@pytest.mark.parametrize("late", ["owner", "worker"])
def test_an_answer_or_a_report_that_comes_while_the_tick_reads_wins(
    attachable: Workflow, late: str
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    question = _question(attachable)
    _comments(attachable, question)
    body = "2" if late == "owner" else f"{_MARKER}\nAnother question?"
    arrival = {
        "id": 5,
        "body": body,
        "created_at": "2026-10-19T18:14:00Z",
        "user": question["user"],
    }
    # The owner's reply, or a new report, lands after the tick's first read of the comments.
    real = attachable.bin / "gh"
    real.rename(attachable.bin / "gh-real")
    attachable.fake(
        "gh",
        "#!/bin/bash\n"
        'if [[ "$*" == *"/comments?per_page=100"* ]]; then\n'
        '  read_once="@STATE@/read-once"\n'
        '  if [ -e "$read_once" ]; then\n'
        f"    echo {json.dumps(json.dumps([[question, arrival]]))} > @STATE@/comments-7.json\n"
        "  fi\n"
        '  touch "$read_once"\n'
        "fi\n"
        'exec "$(dirname "$0")/gh-real" "$@"\n',
    )

    _tick_at(attachable, 15)

    assert _answers(attachable) == []


def test_a_question_from_an_account_that_may_not_write_is_never_answered(
    attachable: Workflow,
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    (attachable.state / "permission-passer-by").write_text("read\n")
    _comments(attachable, _question(attachable, login="passer-by"))

    _tick_at(attachable, 60)

    assert _answers(attachable) == []


def test_a_failed_answer_is_tried_again_and_posted_once(attachable: Workflow) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(attachable, _question(attachable))
    (attachable.state / "fail-gh-comment").touch()

    failed = attachable.run_script("tick", env={**_ENV, "WORKFLOW_NOW": str(_NOW + 900)})
    assert failed.returncode != 0
    assert _answers(attachable) == []
    (attachable.state / "fail-gh-comment").unlink()
    _tick_at(attachable, 18)
    _tick_at(attachable, 21)

    assert len(_answers(attachable)) == 1
    assert len(_answered_in_log(attachable)) == 1


@pytest.mark.parametrize(("minutes", "answered_at"), [(0, None), (40, 40)])
def test_the_configured_wait_moves_or_turns_off_the_answer(
    attachable: Workflow, minutes: int, answered_at: int | None
) -> None:
    _set_config(attachable, answer_for_owner_after_minutes=minutes)
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(attachable, _question(attachable))

    for at in (15, 39, 40, 600):
        _tick_at(attachable, at)

    if answered_at is None:
        assert _answers(attachable) == []
        assert not (attachable.state / "attached").exists()
        return
    assert len(_answers(attachable)) == 1
    assert _answered_in_log(attachable) == [
        f'{_NOW + answered_at * 60} answered for the owner: cloud-7, "1"'
    ]


def test_the_rules_give_a_cloud_workers_question_the_shape_the_tick_answers() -> None:
    rules = (Path(__file__).resolve().parents[2] / "scripts/workflow/RULES.md").read_text()
    cloud = " ".join(rules.split("### Cloud workers", 1)[1].split("\n## ", 1)[0].split())
    assert "the line `Owner only: yes` or `Owner only: no`" in cloud
    assert f"`{_ORCHESTRATOR} answered for the owner: <reply word>`" in cloud


def test_the_same_question_asked_again_after_the_answer_waits_for_the_owner(
    attachable: Workflow,
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    question = _question(attachable)
    _comments(attachable, question)
    _tick_at(attachable, 15)
    assert len(_answers(attachable)) == 1
    pages = json.loads((attachable.state / "comments-7.json").read_text())
    again = {**question, "id": 9, "created_at": "2026-10-19T18:30:00Z"}
    (attachable.state / "comments-7.json").write_text(json.dumps([pages[0] + [again]]))

    _tick_at(attachable, 31)
    _tick_at(attachable, 60)

    assert len(_answers(attachable)) == 1
    assert len(_answered_in_log(attachable)) == 1


def test_the_notes_are_named_as_the_ticks_and_a_signed_question_is_read(
    attachable: Workflow,
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    signed = _QUESTION + "\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_"
    _comments(attachable, _question(attachable, text=signed.replace("Owner only: {owner}\n", "")))

    _tick_at(attachable, 15)

    notes = attachable.repo / ".local/workflow"
    [note] = notes.glob("stuck.*/*.json")
    assert note.name == f"cloud-7-owner-question-{_NOW}.json"
    assert json.loads(note.read_text()) == {
        "line": "owner question without the Owner only line: cloud-7"
    }
    assert (
        f"{_NOW + 900} cloud-7 asks the owner without the Owner only line; it waits for him"
        in _lines(attachable.wake_log)
    )

    _comments(attachable, _question(attachable, text=signed))
    (attachable.state / "comments-7.json").write_text(
        (attachable.state / "comments-7.json").read_text().replace('"id": 4', '"id": 6')
    )
    _tick_at(attachable, 40)

    [answer] = notes.glob("answered-for-owner.*/*.json")
    assert answer.name == f"{_NOW + 2400}-cloud-7.json"


def test_zero_minutes_also_leaves_a_question_without_the_line_unnamed(
    attachable: Workflow,
) -> None:
    _set_config(attachable, answer_for_owner_after_minutes=0)
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(
        attachable, _question(attachable, text=_QUESTION.replace("Owner only: {owner}\n", ""))
    )

    _tick_at(attachable, 60)

    assert [message for message in attachable.sent() if "owner question" in message] == []


# Questions the stop hook (`notify`, `question_of`) and the cloud relay must read alike.
_SHAPES = [
    _QUESTION.format(owner="no"),
    _QUESTION.format(owner="yes"),
    _QUESTION.replace("Owner only: {owner}\n", ""),
    'Pick?\n**Owner only:** no\nWrite "merge it" to merge.',
    'Pick?\n`Owner only: no`\nReply "/clear".',
    'Pick?\nOwner only: no\nReply "' + "x" * 61 + '".',
    'Pick?\nOwner only: no\nReply "' + "x" * 60 + '".',
    "Pick?\nOwner only: no\nReply 12abc or 3.",
    "Pick?\nOwner only: no\nReply “é ok”.",
    "Pick?\nOwner only: no Reply 1.",
    'Pick?\nOwner only: no\nReply "a\x9bb".',
    "Pick?\nOwner only:\x1fno\nReply 1.",
    "Pick?\r\nOwner only: no\r\nReply 1.",
    "Pick?\nOwner only: yes\nOwner only: no\nReply 2.",
    "Pick?\nOwner only: no\nReply 1.\nThanks.",
    "Pick?\nOwner only: no\nReply 1.\n\n  \n",
    "Pick?\nReply 1.\nOwner only: no",
]


def test_a_cloud_question_is_read_as_the_stop_hook_reads_a_local_one() -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts/workflow"
    hook = subprocess.run(
        [
            "bash",
            "-c",
            'source <(sed -n "/^question_of()/,/^}/p" "$1"); shift; '
            'for message; do question_of "$message" "1 2"; done',
            "read",
            str(scripts / "notify"),
            *_SHAPES,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    relay = subprocess.run(
        [
            "python3",
            "-c",
            "import json, sys, cloud_workers\n"
            "for message in json.load(sys.stdin):\n"
            "    owner, reply, _ = cloud_workers._question(message)\n"
            "    print(json.dumps([owner, reply]))\n",
        ],
        input=json.dumps(_SHAPES),
        cwd=scripts,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    read = [json.loads(line) for line in hook.stdout.splitlines()]
    expected = [[note["owner_only"], note.get("reply", "")] for note in read]
    assert [json.loads(line) for line in relay.stdout.splitlines()] == expected


def test_an_answer_posted_but_not_confirmed_is_noted_once_at_the_next_tick(
    attachable: Workflow,
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(attachable, _question(attachable))
    (attachable.state / "lose-gh-comment-answer").touch()

    lost = attachable.run_script("tick", env={**_ENV, "WORKFLOW_NOW": str(_NOW + 900)})
    assert lost.returncode != 0
    (attachable.state / "lose-gh-comment-answer").unlink()
    _tick_at(attachable, 18)
    _tick_at(attachable, 21)

    assert len(_answers(attachable)) == 1
    assert _answered_in_log(attachable) == [f'{_NOW + 1080} answered for the owner: cloud-7, "1"']
    [answer] = (attachable.repo / ".local/workflow").glob("answered-for-owner.*/*.json")
    assert json.loads(answer.read_text())["reply"] == "1"


@pytest.mark.parametrize(
    "text",
    [
        "Which order?\n\n1. Oldest first\n2. Newest first (Recommended)\n\nOwner only: no\n"
        "Reply 1 or 2.",
        "Which order?\n\n1. Oldest first\n2. Newest first\n\nI recommend 2.\n\nOwner only: no\n"
        "Answer 1 or 2.",
        "Which order?\n\nRecommended: 2\n1. Oldest first\n2. Newest first\n\nOwner only: no\n"
        "Reply 1 or 2.",
    ],
    ids=["marked on a choice", "in a sentence", "above the list"],
)
def test_a_recommendation_other_than_the_first_reply_word_waits_and_is_named_once(
    attachable: Workflow, text: str
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(attachable, _question(attachable, text=text))

    for minutes in (15, 30, 60):
        _tick_at(attachable, minutes)

    assert _answers(attachable) == []
    told = [message for message in attachable.sent() if "owner question" in message]
    assert len(told) == 1
    assert (
        "owner question with a recommendation other than its first reply word: cloud-7"
        in told[0]
    )


@pytest.mark.parametrize(
    "text",
    [
        "Which order?\n\nRecommended: 1\n1. Oldest first\n2. Newest first\n\nOwner only: no\n"
        "Reply 1 or 2.",
        "Which order?\n\n1. Oldest first\n2. Newest first (not recommended)\n\nOwner only: no\n"
        "Reply 1 or 2.",
    ],
    ids=["agrees", "a choice not recommended"],
)
def test_a_recommendation_that_agrees_with_the_first_reply_word_is_answered(
    attachable: Workflow, text: str
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    _comments(attachable, _question(attachable, text=text))

    _tick_at(attachable, 15)

    assert [answer.split("\n", 1)[0] for answer in _answers(attachable)] == [
        f"{_ORCHESTRATOR} answered for the owner: 1"
    ]


@pytest.mark.parametrize(
    "ending", ["Reply: 1 or 2", "Choose 1 or 2.", "Reply A or B.", "Reply **1** or **2**."]
)
def test_common_closing_lines_ask_the_owner(workflow: Workflow, ending: str) -> None:
    workflow.add_pull_request(7, labels=())
    _record(workflow)
    _keys(workflow)
    _comments(
        workflow, _comment(4, f"Which order?\n\n1. Oldest first\n2. Newest first\n\n{ending}")
    )

    done = workflow.run_script("tick", env=_ENV)

    assert done.returncode == 0, done.stderr
    assert (workflow.repo / ".local/workflow/cloud-7.asking").exists()
    [push] = workflow.pushes()
    assert push.title == "cloud-7 needs you"


def test_the_same_question_asked_again_after_the_owner_answered_waits(
    attachable: Workflow,
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    question = _question(attachable)
    answer = {
        "id": 5,
        "body": "3",
        "created_at": "2026-10-19T18:05:00Z",
        "user": {"login": "example-owner"},
    }
    _comments(attachable, question, answer)
    _tick_at(attachable, 6)
    again = {**question, "id": 9, "created_at": "2026-10-19T18:20:00Z"}
    _comments(attachable, question, answer, again)

    for minutes in (21, 36, 60):
        _tick_at(attachable, minutes)

    assert _answers(attachable) == []


@pytest.mark.parametrize(
    "ending",
    [
        "Owner only: no\nRecommended: 2\nReply 1 or 2.",
        "Owner only: no\nReply 1 or 2 (recommended: 2).",
        "Owner only: no\nAnswer 1 or 2. I recommend 2.",
        "Owner only: no\nReply 1 (not recommended) or 2 (recommended).",
    ],
    ids=["below the line", "on the reply line", "after the reply words", "beside another"],
)
def test_a_recommendation_below_the_owner_only_line_is_not_overruled(
    attachable: Workflow, ending: str
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    text = f"Which order?\n\n1. Oldest first\n2. Newest first\n\n{ending}"
    _comments(attachable, _question(attachable, text=text))

    for minutes in (15, 30, 60):
        _tick_at(attachable, minutes)

    assert _answers(attachable) == []
    told = [message for message in attachable.sent() if "owner question" in message]
    assert len(told) == 1
    assert (
        "owner question with a recommendation other than its first reply word: cloud-7"
        in told[0]
    )


@pytest.mark.parametrize(
    ("ending", "reply"),
    [
        ("Owner only: no\nRecommended: 1\nAnswer 1 or 2.", "1"),
        ("Owner only: no\nAnswer 1 or 2. Recommended: 1", "1"),
        ("Owner only: no\nReply **2** (recommended) or **1**.", "2"),
    ],
    ids=["below the line", "after the reply words", "beside the first reply word"],
)
def test_a_recommendation_of_the_first_reply_word_below_the_line_is_answered(
    attachable: Workflow, ending: str, reply: str
) -> None:
    attachable.add_pull_request(7)
    _record(attachable)
    text = f"Which order?\n\n1. Oldest first\n2. Newest first\n\n{ending}"
    _comments(attachable, _question(attachable, text=text))

    _tick_at(attachable, 15)

    assert [answer.split("\n", 1)[0] for answer in _answers(attachable)] == [
        f"{_ORCHESTRATOR} answered for the owner: {reply}"
    ]


def test_a_question_ending_with_its_recommendation_asks_the_owner(workflow: Workflow) -> None:
    workflow.add_pull_request(7, labels=())
    _record(workflow)
    _keys(workflow)
    _comments(
        workflow,
        _comment(
            4,
            "Which order?\n\n1. Oldest first\n2. Newest first\n\nOwner only: no\n"
            "Answer 1 or 2\nRecommended: 1",
        ),
    )

    done = workflow.run_script("tick", env=_ENV)

    assert done.returncode == 0, done.stderr
    assert (workflow.repo / ".local/workflow/cloud-7.asking").exists()
    [push] = workflow.pushes()
    assert push.title == "cloud-7 needs you"
