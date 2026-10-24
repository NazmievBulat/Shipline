"""Cloud workers launch with a TTY and report once through ordinary workflow channels."""

import fcntl
import json
import os
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


def _comment(identity: int, text: str) -> dict[str, object]:
    return {
        "id": identity,
        "body": f"{_MARKER}\n{text}",
        "created_at": "2026-10-19T18:00:00Z",
        "html_url": f"https://github.com/NazmievBulat/Shipline/pull/7#issuecomment-{identity}",
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
