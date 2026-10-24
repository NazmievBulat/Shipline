"""Worker/reviewer handoffs and Codex completion use real git and fake external commands."""

import json

import pytest
from workflow_harness import SESSION_LINK, Workflow, instruction


def _idle(workflow: Workflow, name: str) -> None:
    workflow.notes(7).mkdir(parents=True, exist_ok=True)
    (workflow.notes(7) / f"{name}.idle").touch()


def test_reviewer_is_fresh_opus_at_the_effort_given(workflow: Workflow) -> None:
    workflow.add_pull_request(7)
    done = workflow.run("start", "review", "7", "low")
    assert done.returncode == 0, done.stderr
    command = (workflow.state / "args" / "review-7").read_text().splitlines()[-1]
    assert command == (
        "claude --effort low --model opus --remote-control review-7 "
        f'"{instruction("Review pull request 7")}"'
    )
    assert "resume" not in command
    assert "worker" not in command


@pytest.mark.parametrize("model", ["sonnet", "codex"])
def test_reviewer_refuses_other_models(workflow: Workflow, model: str) -> None:
    done = workflow.run("start", "review", "7", "high", model)
    assert done.returncode == 2
    assert workflow.calls("tmux") == []


@pytest.mark.parametrize("first", ["worker", "audit", "review"])
@pytest.mark.parametrize("second", ["worker", "audit", "review"])
def test_no_two_sessions_share_a_pull_request(workflow: Workflow, first: str, second: str) -> None:
    workflow.add_pull_request(7)
    assert workflow.run("start", first, "7").returncode == 0
    done = workflow.run("start", second, "7")
    assert done.returncode != 0
    assert "already running" in done.stderr


def test_tick_hands_finished_worker_to_reviewer(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("ticket-7")
    _idle(workflow, "ticket-7")
    workflow.screen("Worker finished.\n> \n", "ticket-7")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("ticket-7")
    assert workflow.is_running("review-7")
    merges = [c for c in workflow.calls("gh") if c.startswith("gh pr merge ") or "/merge" in c]
    assert merges == []


def test_sent_back_draft_gets_worker_and_next_reviewer_is_new(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen("Review sent back.\n> \n", "review-7")
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stderr
    assert not workflow.is_running("review-7")
    assert workflow.is_running("ticket-7")
    workflow.list_pull_request(7, draft=False)
    _idle(workflow, "ticket-7")
    workflow.screen("Worker finished.\n> \n", "ticket-7")
    (workflow.state / "screen" / "review-7").unlink()
    second = workflow.run_script("tick")
    assert second.returncode == 0, second.stderr
    assert workflow.is_running("review-7")
    assert not workflow.calls("tmux review-7 send-keys")


@pytest.mark.parametrize("draft", [False, True])
def test_reviewer_is_not_stopped_for_an_incomplete_handoff(workflow: Workflow, draft: bool) -> None:
    workflow.add_worktree(7, draft=draft)
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen("Review sent back.\nAnd a question.\n> \n", "review-7")
    workflow.run_script("tick")
    assert workflow.is_running("review-7")
    assert not workflow.is_running("ticket-7")


def test_send_back_without_restoring_draft_keeps_reviewer(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen("Review sent back.\n> \n", "review-7")
    workflow.run_script("tick")
    assert workflow.is_running("review-7")


def test_reviewed_stack_head_waits_but_new_head_gets_fresh_review(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    (workflow.state / "base-branch-7").write_text("ticket/parent")
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen("Review finished.\n> \n", "review-7")
    workflow.run_script("tick")
    workflow.run_script("tick")
    assert not workflow.is_running("review-7")
    new_head = workflow.push_from_elsewhere(7)
    (workflow.state / "screen" / "review-7").unlink()
    workflow.run_script("tick")
    assert workflow.is_running("review-7")
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == new_head


@pytest.mark.parametrize("restacked", [False, True], ids=["main-base", "stack-moved-to-main"])
def test_finished_main_reviewer_gets_fresh_review(workflow: Workflow, restacked: bool) -> None:
    workflow.add_worktree(7, draft=False)
    if restacked:
        (workflow.state / "base-branch-7").write_text("ticket/parent")
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen(f"Remote Control: {SESSION_LINK}\nReview finished.\n> \n", "review-7")
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stderr
    mark = workflow.repo / ".local/workflow/review-7.head"
    assert mark.exists() == restacked
    if restacked:
        assert not workflow.is_running("review-7")
        (workflow.state / "base-branch-7").write_text("main")
    (workflow.state / "screen" / "review-7").unlink()
    second = workflow.run_script("tick")
    assert second.returncode == 0, second.stderr
    assert workflow.is_running("review-7")
    assert not mark.exists()


def _rule_breaks(workflow: Workflow) -> list[dict[str, str]]:
    pending = workflow.repo / ".local/workflow/rule-breaks.pending"
    return [json.loads(path.read_text()) for path in sorted(pending.glob("*.json"))]


def test_a_main_reviewer_that_finished_without_a_handoff_is_noted_once(
    workflow: Workflow,
) -> None:
    """A passed review is handed to the tick; `review finished` alone leaves nothing to merge."""
    workflow.add_worktree(7, draft=False)
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen(
        f"Remote Control: {SESSION_LINK}\n● Passed.\n\n  review finished\n\n> \n", "review-7"
    )
    # The first stop fails, so the next tick reads the same words again.
    workflow.survives_kill("review-7")

    first = workflow.run_script("tick")
    assert first.returncode != 0
    assert _rule_breaks(workflow) == [{"name": "review-7", "number": "7", "head": head}]
    # The wake told the orchestrator in between: the same break is not noted again.
    folder = workflow.repo / ".local/workflow"
    (folder / "rule-breaks.pending").rename(folder / "rule-breaks.sent")
    (workflow.state / "survives-kill").unlink()
    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stderr
    assert _rule_breaks(workflow) == []
    # Like any ready pull request without a session, it gets a fresh reviewer.
    assert workflow.is_running("review-7")

    # That reviewer breaks the rule again, on a new head: a new break.
    new_head = workflow.push_from_elsewhere(7)
    _idle(workflow, "review-7")
    third = workflow.run_script("tick")

    assert third.returncode == 0, third.stderr
    assert _rule_breaks(workflow) == [{"name": "review-7", "number": "7", "head": new_head}]


def test_a_rule_break_github_cannot_confirm_keeps_the_reviewer_for_the_next_tick(
    workflow: Workflow,
) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen(
        f"Remote Control: {SESSION_LINK}\n● Passed.\n\n  review finished\n\n> \n", "review-7"
    )
    (workflow.state / "fail-gh-view-7").touch()

    failed = workflow.run_script("tick")

    assert failed.returncode != 0
    assert workflow.is_running("review-7")
    assert _rule_breaks(workflow) == []
    (workflow.state / "fail-gh-view-7").unlink()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stderr
    assert [event["name"] for event in _rule_breaks(workflow)] == ["review-7"]


@pytest.mark.parametrize(
    "pull", ["stacked", "merged", "merged-after-snapshot", "handed-back", "batch-merged"]
)
def test_a_reviewer_that_could_not_hand_off_is_no_rule_break(workflow: Workflow, pull: str) -> None:
    workflow.add_worktree(7, state="MERGED" if pull == "merged" else "OPEN", draft=False)
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    if pull == "stacked":
        (workflow.state / "base-branch-7").write_text("ticket/parent")
    elif pull == "merged-after-snapshot":
        (workflow.state / "pr-now").mkdir()
        (workflow.state / "pr-now/7").write_text(
            (workflow.state / "pr/7").read_text().replace("OPEN", "MERGED")
        )
    elif pull == "handed-back":
        # The tick handed the merge back after the handoff; the reviewer had already finished.
        (folder / "merge-review-7").write_text(json.dumps({"reason": "a check failed"}))
    elif pull == "batch-merged":
        (folder / "batch-merged-7").write_text(json.dumps({"by": "8"}))
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen("● Merged.\n\n  review finished\n\n> \n", "review-7")

    done = workflow.run_script("tick")

    assert _rule_breaks(workflow) == [], done.stdout + done.stderr
    assert "review-7 stopped" in done.stdout


def test_cleanup_keeps_reviewer_until_stack_work_finishes(workflow: Workflow) -> None:
    path = workflow.add_worktree(7, state="MERGED")
    workflow.running("review-7")
    workflow.run("cleanup")
    assert path.exists()
    workflow.run("stop", "review-7")
    workflow.run("cleanup")
    assert not path.exists()


def test_closed_pull_request_stops_reviewer(workflow: Workflow) -> None:
    workflow.add_worktree(7, state="CLOSED")
    workflow.running("review-7")
    workflow.run("cleanup")
    assert not workflow.is_running("review-7")


@pytest.mark.parametrize(
    ("draft", "message", "rings"),
    [
        (False, "Review finished.", False),
        (True, "Review sent back.", False),
        (False, "Review sent back.", True),
        (False, "Which option?", True),
        (False, "Worker finished.", True),
        (False, "Review finished.\nWhich option?", True),
    ],
)
def test_reviewer_rings_only_for_owner(
    workflow: Workflow, draft: bool, message: str, rings: bool
) -> None:
    workflow.add_worktree(7, draft=draft)
    keys = workflow.root / "home" / "agents" / ".env"
    keys.parent.mkdir()
    keys.write_text("PUSHOVER_USER=owner\nPUSHOVER_TOKEN=app\n")
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": message}, "review-7")
    assert bool(workflow.pushes()) == rings
    assert (workflow.notes(7) / "review-7.idle").exists()


def test_reviewer_limit_resends_its_own_instruction(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.running("review-7")
    workflow.screen("You've hit your session limit · resets 4pm (UTC)\n> \n", "review-7")
    env = {"WORKFLOW_NOW": "1792080000"}  # Oct 15 2026, 16:00 UTC
    workflow.run_script("tick", env=env)
    workflow.run_script("tick", env=env)
    calls = workflow.calls("tmux review-7 send-keys")
    assert len(calls) == 2  # literal sentence, then Enter, once per reset
    assert calls[0].endswith(instruction("Review pull request 7"))


def test_failed_github_draft_check_rings_for_reviewer(workflow: Workflow) -> None:
    workflow.add_worktree(7)
    (workflow.state / "draft" / "7").unlink()
    keys = workflow.root / "home" / "agents" / ".env"
    keys.parent.mkdir()
    keys.write_text("PUSHOVER_USER=owner\nPUSHOVER_TOKEN=app\n")
    workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": "Review sent back."}, "review-7"
    )
    assert len(workflow.pushes()) == 1


def test_head_read_failure_does_not_prevent_other_sessions(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    workflow.add_pull_request(8)
    (workflow.state / "pr" / "7").write_text("OPEN\n")
    done = workflow.run_script("tick")
    assert done.returncode != 0
    assert not workflow.is_running("review-7")
    assert workflow.is_running("ticket-8")


def test_reviewer_preserves_clean_unpushed_descendants(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "fix: not pushed", cwd=worktree)
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")
    assert workflow.git("log", "-1", "--format=%s", cwd=worktree) == "fix: not pushed"


def test_reviewer_refuses_uncommitted_changes(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    (worktree / "stray.txt").write_text("left by a worker\n")
    done = workflow.run("start", "review", "7")
    assert done.returncode != 0
    assert "uncommitted changes" in done.stderr
    assert not workflow.is_running("review-7")


@pytest.mark.parametrize("allowed", [None, "2", "3"])
def test_tick_requires_owner_allowance_for_third_marked_round(
    workflow: Workflow, allowed: str | None
) -> None:
    workflow.add_pull_request(7, draft=False)
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [
                {"body": "Unmarked comment"},
                {"body": "<!-- workflow-review round=1 result=sent-back -->\nFirst review"},
                {"body": "<!-- workflow-review round=2 result=sent-back -->\nSecond review"},
            ]
        )
    )
    if allowed:
        notes = workflow.repo / ".local" / "workflow"
        notes.mkdir(parents=True)
        (notes / "review-7.allow-round").write_text(allowed + "\n")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7") == (allowed == "3")
    if allowed != "3":
        assert "owner" in done.stdout and "round 3" in done.stdout


def test_review_history_failure_blocks_only_that_reviewer(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    workflow.add_pull_request(8)
    (workflow.state / "fail-gh-reviews").touch()
    done = workflow.run_script("tick")
    assert done.returncode != 0
    assert not workflow.is_running("review-7")
    assert workflow.is_running("ticket-8")


def test_passed_second_round_on_stack_does_not_gate_new_head(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    (workflow.state / "base-branch-7").write_text("ticket/parent")
    workflow.running("review-7")
    _idle(workflow, "review-7")
    workflow.screen("Review finished.\n> \n", "review-7")
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [
                {"body": "<!-- workflow-review round=1 result=sent-back -->\nFix this"},
                {"body": "<!-- workflow-review round=2 result=passed -->\nWaiting for base"},
            ]
        )
    )
    assert workflow.run_script("tick").returncode == 0
    workflow.push_from_elsewhere(7)
    (workflow.state / "screen" / "review-7").unlink()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")


def test_unmarked_review_does_not_block_first_reviewer(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    (workflow.state / "reviews-7.json").write_text(
        json.dumps([{"body": "Codex review: no issues"}])
    )
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")


def test_nontext_unmarked_body_is_skipped(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    (workflow.state / "reviews-7.json").write_text(json.dumps([{"body": 42}]))
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")


def test_legacy_markers_keep_their_existing_round_gate(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [
                {"body": "<!-- workflow-review round=1 -->\nFirst review"},
                {"body": "<!-- workflow-review round=2 -->\nSecond review"},
            ]
        )
    )
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert not workflow.is_running("review-7")
    assert "round 3" in done.stdout


def test_tick_reads_reviews_from_the_organization(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    # The tick also reads main's CI for the merge gate; only the reviews read is checked here.
    assert [call for call in workflow.calls("gh api --paginate") if "/pulls/" in call] == [
        "gh api --paginate repos/example-org/example-app/pulls/7/reviews"
    ]


def test_tick_keeps_an_unpushed_commit_for_the_next_reviewer(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    workflow.git("commit", "--quiet", "--allow-empty", "-m", "fix: local work", cwd=worktree)
    local = workflow.git("rev-parse", "HEAD", cwd=worktree)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == local
    record = json.loads((workflow.repo / ".local/workflow/review-7.local-work").read_text())
    assert record["local_head"] == local
    assert record["head"] == workflow.git("rev-parse", f"origin/{workflow.branch(7)}")


def test_dirty_start_is_attempted_and_reported_once(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    (worktree / "stray.txt").write_text("left by a worker")
    workflow.orchestrator(status="busy")
    original = workflow.script.with_name("session-real")
    workflow.script.rename(original)
    attempts = workflow.state / "start-attempts"
    workflow.script.write_text(
        f'#!/bin/bash\n[[ "$*" != "start review 7" ]] || echo attempt >> "{attempts}"\n'
        f'exec bash "{original}" "$@"\n'
    )
    workflow.script.chmod(0o755)
    for tick in range(3):
        workflow.run_script("tick", env={"WORKFLOW_NOW": str(1792497600 + tick * 360)})
    assert not workflow.is_running("review-7")
    assert sum("reviewer start refused" in message for message in workflow.sent()) == 1
    assert attempts.read_text().splitlines() == ["attempt"]
    assert (worktree / "stray.txt").read_text() == "left by a worker"


def test_round_allowance_is_delivered_once_and_cleared(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    workflow.orchestrator(status="busy")
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [{"body": f"<!-- workflow-review round={n} result=sent-back -->"} for n in (1, 2)]
        )
    )
    for tick in range(3):
        done = workflow.run_script("tick", env={"WORKFLOW_NOW": str(1792497600 + tick * 360)})
        assert done.returncode == 0, done.stderr
    assert (
        sum(
            "pull request 7: needs the owner's allowance for round 3" in message
            for message in workflow.sent()
        )
        == 1
    )
    notes = workflow.repo / ".local/workflow"
    (notes / "review-7.allow-round").write_text("3\n")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")
    assert not (notes / "review-7.allowance-notice").exists()


def test_reviewer_refuses_diverged_local_commits(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    workflow.git("commit", "--allow-empty", "-m", "fix: local", cwd=worktree)
    local = workflow.git("rev-parse", "HEAD", cwd=worktree)
    workflow.push_from_elsewhere(7)
    done = workflow.run("start", "review", "7")
    assert done.returncode != 0
    assert not workflow.is_running("review-7")
    assert workflow.git("rev-parse", "HEAD", cwd=worktree) == local


def test_interrupted_refusal_notice_is_recovered_without_another_start(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    (worktree / "stray.txt").write_text("left by a worker")
    workflow.run_script("tick", env={"WORKFLOW_NOW": "1792497200"})
    notes = workflow.repo / ".local/workflow"
    record = json.loads((notes / "review-7.start-refused").read_text())
    pending = notes / "merge-handbacks.pending" / record["event"]
    pending.unlink()  # interruption between persisting the refusal and queuing delivery
    workflow.orchestrator(status="busy")
    done = workflow.run_script("tick", env={"WORKFLOW_NOW": "1792497600"})
    assert done.returncode == 0, done.stderr
    assert sum("reviewer start refused" in message for message in workflow.sent()) == 1
    assert not workflow.is_running("review-7")


def test_head_change_replaces_an_undelivered_allowance_notice(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [{"body": f"<!-- workflow-review round={n} result=sent-back -->"} for n in (1, 2)]
        )
    )
    workflow.run_script("tick")
    notes = workflow.repo / ".local/workflow"
    old = json.loads((notes / "review-7.allowance-notice").read_text())
    head = workflow.push_from_elsewhere(7)
    workflow.run_script("tick")
    new = json.loads((notes / "review-7.allowance-notice").read_text())
    assert new["identity"] == [head, "3"]
    assert new["event"] != old["event"]
    assert not (notes / "merge-handbacks.pending" / old["event"]).exists()


@pytest.mark.parametrize("changed", ["published", "local"])
def test_head_change_permits_one_new_start_attempt(workflow: Workflow, changed: str) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    (worktree / "stray.txt").write_text("unfinished")
    workflow.run_script("tick")
    notes = workflow.repo / ".local/workflow"
    old = json.loads((notes / "review-7.start-refused").read_text())
    if changed == "published":
        workflow.push_from_elsewhere(7)
    else:
        workflow.git("commit", "--allow-empty", "-m", "fix: new local work", cwd=worktree)
    done = workflow.run_script("tick")
    assert done.returncode != 0  # one new attempt hits the remaining dirty file
    new = json.loads((notes / "review-7.start-refused").read_text())
    assert new["event"] != old["event"]
    assert not (notes / "merge-handbacks.pending" / old["event"]).exists()
    done = workflow.run_script("tick")
    assert done.returncode == 0  # unchanged refusal is suppressed again
    assert json.loads((notes / "review-7.start-refused").read_text()) == new


def test_written_allowance_clears_notice_even_with_a_running_reviewer(workflow: Workflow) -> None:
    workflow.add_pull_request(7, draft=False)
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [{"body": f"<!-- workflow-review round={n} result=sent-back -->"} for n in (1, 2)]
        )
    )
    workflow.run_script("tick")
    notes = workflow.repo / ".local/workflow"
    record = json.loads((notes / "review-7.allowance-notice").read_text())
    (notes / "review-7.allow-round").write_text("3\n")
    workflow.running("review-7")
    workflow.run_script("tick")
    assert not (notes / "review-7.allowance-notice").exists()
    assert not (notes / "merge-handbacks.pending" / record["event"]).exists()


def test_cleaning_a_dirty_worktree_allows_automatic_recovery(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    stray = worktree / "stray.txt"
    stray.write_text("unfinished")
    workflow.run_script("tick")
    stray.unlink()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")
    assert not (workflow.repo / ".local/workflow/review-7.start-refused").exists()


def test_manual_recovery_withdraws_the_pending_refusal_notice(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    stray = worktree / "stray.txt"
    stray.write_text("unfinished")
    workflow.run_script("tick")
    notes = workflow.repo / ".local/workflow"
    record = json.loads((notes / "review-7.start-refused").read_text())
    stray.unlink()
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")
    assert not (notes / "review-7.start-refused").exists()
    assert not (notes / "merge-handbacks.pending" / record["event"]).exists()


def test_removing_a_competing_worktree_allows_automatic_recovery(workflow: Workflow) -> None:
    worktree = workflow.add_worktree(7, draft=False)
    elsewhere = workflow.root / "elsewhere"
    workflow.git("worktree", "move", str(worktree), str(elsewhere))
    done = workflow.run_script("tick")
    assert done.returncode != 0
    assert not workflow.is_running("review-7")
    assert elsewhere.exists()
    workflow.git("worktree", "remove", str(elsewhere))
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.is_running("review-7")
