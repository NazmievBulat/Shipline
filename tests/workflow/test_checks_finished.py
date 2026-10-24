"""A stopped check watch wakes once, without submitting an owner's answer."""

import json
import os

import pytest
from workflow_harness import Workflow

_BOX = "\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n"
_WAIT = "The checks are still running; I am waiting for them."


# The session started at _STARTED; its own round was published after that.
_STARTED = 1_790_000_000
_AFTER_START = "2026-09-21T15:00:00Z"
_BEFORE_START = "2026-09-21T10:00:00Z"


def _review(workflow: Workflow, head: str, submitted_at: str = _AFTER_START) -> None:
    (workflow.state / "reviews-7.json").write_text(
        json.dumps(
            [
                {
                    "body": "<!-- workflow-review round=1 result=passed -->",
                    "commit_id": head,
                    "submitted_at": submitted_at,
                }
            ]
        )
    )


def _checks(workflow: Workflow, status: str = "COMPLETED", conclusion: str = "SUCCESS") -> None:
    (workflow.state / "rollup-7.json").write_text(
        json.dumps(
            [
                {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}
                for name in ("CI result", "Docker images: what to run", "E2E")
            ]
        )
    )


def _waiting(workflow: Workflow) -> str:
    workflow.add_worktree(7, draft=False)
    workflow.running("review-7")
    workflow.notes(7).mkdir(parents=True)
    started = workflow.notes(7) / "review-7.started"
    started.touch()
    os.utime(started, (_STARTED, _STARTED))
    (workflow.notes(7) / "review-7.checks-wait").touch()
    workflow.screen("● " + _WAIT + "\n" + _BOX, "review-7")
    head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    _review(workflow, head)
    _checks(workflow)
    return head


def _nudges(workflow: Workflow) -> list[str]:
    return workflow.calls("tmux review-7 send-keys -t =review-7: -l The checks on ")


def test_finished_checks_wake_an_idle_reviewer_once_and_log_it(workflow: Workflow) -> None:
    head = _waiting(workflow)

    first = workflow.run_script("tick")
    second = workflow.run_script("tick")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert len(_nudges(workflow)) == 1
    assert _nudges(workflow)[0].endswith(
        f"The checks on {head} have finished. Read them once and go on by the rules: "
        "merge when they all passed, fix or send back when one failed."
    )
    assert workflow.calls("tmux review-7 send-keys")[-1].endswith(" Enter")
    assert head in workflow.wake_log.read_text()
    assert "review-7" in workflow.wake_log.read_text()
    merges = [c for c in workflow.calls("gh") if c.startswith("gh pr merge ") or "/merge" in c]
    assert merges == []


@pytest.mark.parametrize("review", ["[]", '[{"body":"Unmarked review"}]'])
def test_wait_note_wakes_before_a_round_is_published_once_per_head(
    workflow: Workflow, review: str
) -> None:
    head = _waiting(workflow)
    (workflow.state / "reviews-7.json").write_text(review)
    # This is the empty prompt Claude Code draws, including its no-break space.
    workflow.screen("● " + _WAIT + _BOX.replace("> \n", "\u276f\u00a0\n"), "review-7")
    _checks(workflow, status="IN_PROGRESS", conclusion="")
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []

    _checks(workflow)
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stderr
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1
    assert head in workflow.wake_log.read_text()
    assert not any("/pulls/7/reviews" in call for call in workflow.calls("gh api"))


def test_a_reviewer_without_a_wait_note_needs_its_published_round(workflow: Workflow) -> None:
    head = _waiting(workflow)
    (workflow.notes(7) / "review-7.checks-wait").unlink()
    (workflow.notes(7) / "review-7.idle").touch()
    (workflow.state / "reviews-7.json").write_text("[]")
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []

    _review(workflow, head)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS", "WAITING", "PENDING"])
def test_unfinished_checks_are_left_alone(workflow: Workflow, status: str) -> None:
    _waiting(workflow)
    _checks(workflow, status=status, conclusion="")
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []


def test_failed_checks_also_wake_the_reviewer(workflow: Workflow) -> None:
    _waiting(workflow)
    _checks(workflow, conclusion="FAILURE")
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


def test_a_fix_pushed_after_the_round_gets_one_new_nudge(workflow: Workflow) -> None:
    _waiting(workflow)
    assert workflow.run_script("tick").returncode == 0
    # Pushed from a clone, so the tick must fetch the head before it can read its ancestry.
    workflow.push_from_elsewhere(7)
    assert workflow.run_script("tick").returncode == 0
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 2


def test_a_merge_of_main_after_the_round_gets_one_new_nudge(workflow: Workflow) -> None:
    _waiting(workflow)
    assert workflow.run_script("tick").returncode == 0
    path = workflow.worktree(7)
    tree = workflow.git("rev-parse", "origin/main^{tree}", cwd=path)
    moved = workflow.git("commit-tree", tree, "-p", "origin/main", "-m", "main moved", cwd=path)
    workflow.git("push", "--quiet", "origin", f"{moved}:refs/heads/main", cwd=path)
    workflow.git("merge", "--quiet", "--no-ff", "-m", "merge main", moved, cwd=path)
    workflow.git("push", "--quiet", "origin", workflow.branch(7), cwd=path)
    workflow.set_pull_request(7, "OPEN", workflow.git("rev-parse", "HEAD", cwd=path))
    assert workflow.run_script("tick").returncode == 0
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 2


def test_a_round_published_before_the_session_started_is_not_its_own(
    workflow: Workflow,
) -> None:
    head = _waiting(workflow)
    (workflow.notes(7) / "review-7.checks-wait").unlink()
    (workflow.notes(7) / "review-7.idle").touch()
    _review(workflow, head, submitted_at=_BEFORE_START)
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []


def test_a_round_on_a_commit_outside_the_branch_does_not_count(workflow: Workflow) -> None:
    head = _waiting(workflow)
    path = workflow.worktree(7)
    tree = workflow.git("rev-parse", "HEAD^{tree}", cwd=path)
    rewritten = workflow.git("commit-tree", tree, "-p", head, "-m", "rewritten", cwd=path)
    (workflow.notes(7) / "review-7.checks-wait").unlink()
    (workflow.notes(7) / "review-7.idle").touch()
    _review(workflow, rewritten)
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []


def test_a_reviewer_started_without_a_start_note_is_not_eligible(workflow: Workflow) -> None:
    _waiting(workflow)
    (workflow.notes(7) / "review-7.started").unlink()
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []


@pytest.mark.parametrize(
    "message",
    [
        "Two rounds were sent back. Allow another round?",
        "Recommend option 1; alternatively option 2. Write 1 or 2.",
        'The merge was refused. Write "merge it" and I will run it.',
    ],
)
def test_an_owner_block_is_never_nudged_even_if_the_push_failed(
    workflow: Workflow, message: str
) -> None:
    _waiting(workflow)
    (workflow.state / "reviews-7.json").write_text("[]")
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": message}, "review-7")
    # Deliberately leave a stale check-wait screen: the hook's owner block wins.
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []


def test_notify_waiting_keeps_a_reviewer_eligible_without_ringing(workflow: Workflow) -> None:
    _waiting(workflow)
    (workflow.notes(7) / "review-7.checks-wait").unlink()
    workflow.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": _WAIT}, "review-7")
    assert (workflow.notes(7) / "review-7.checks-wait").exists()
    assert not (workflow.notes(7) / "review-7.idle").exists()
    assert workflow.pushes() == []
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


def test_a_reviewer_waiting_for_its_own_job_is_not_squeezed(workflow: Workflow) -> None:
    _waiting(workflow)
    (workflow.notes(7) / "review-7.checks-wait").unlink()
    group = workflow.running("review-7", held=4000 * 1024 * 1024)
    workflow.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": _WAIT}, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert (group / "memory.reclaim").read_text() == ""
    assert len(_nudges(workflow)) == 1


def test_a_new_turn_ends_the_wait_for_checks(workflow: Workflow) -> None:
    _waiting(workflow)
    workflow.notify(
        {"hook_event_name": "UserPromptSubmit", "source": "background_task_output"}, "review-7"
    )
    assert not (workflow.notes(7) / "review-7.checks-wait").exists()
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []


def test_a_plain_wait_that_names_the_owner_is_still_nudged(workflow: Workflow) -> None:
    _waiting(workflow)
    message = "Waiting for the checks; the tick wakes me, nothing for the owner to do."
    workflow.screen("● " + message + _BOX, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


def test_text_in_the_input_box_is_preserved_and_can_be_retried(workflow: Workflow) -> None:
    _waiting(workflow)
    workflow.screen(_WAIT + _BOX.replace("> \n", "> wait, do something else\n"), "review-7")
    workflow.run_script("tick")
    assert workflow.calls("tmux review-7 send-keys") == []
    workflow.screen(_WAIT + _BOX, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


@pytest.mark.parametrize("reason", ["draft", "busy", "no-checks", "api-error"])
def test_ineligible_sessions_get_no_nudge(workflow: Workflow, reason: str) -> None:
    _waiting(workflow)
    if reason == "draft":
        workflow.list_pull_request(7, draft=True)
    elif reason == "busy":
        (workflow.notes(7) / "review-7.checks-wait").unlink()
    elif reason == "no-checks":
        (workflow.state / "rollup-7.json").write_text("[]")
    else:
        (workflow.state / "fail-gh-checks").touch()
    workflow.run_script("tick")
    assert _nudges(workflow) == []


@pytest.mark.parametrize("state", ["PENDING", "SUCCESS", "FAILURE", "ERROR"])
def test_status_contexts_must_also_be_finished(workflow: Workflow, state: str) -> None:
    _waiting(workflow)
    checks = workflow.state / "rollup-7.json"
    checks.write_text(
        json.dumps(
            [*json.loads(checks.read_text()), {"__typename": "StatusContext", "state": state}]
        )
    )
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == (0 if state == "PENDING" else 1)


def test_a_failed_send_is_retried(workflow: Workflow) -> None:
    _waiting(workflow)
    (workflow.state / "send-keys-fails").touch()
    assert workflow.run_script("tick").returncode != 0
    (workflow.state / "send-keys-fails").unlink()
    assert workflow.run_script("tick").returncode == 0
    assert workflow.run_script("tick").returncode == 0
    # One failed literal send, then one successful literal send and Enter.
    assert len(workflow.calls("tmux review-7 send-keys")) == 3


@pytest.mark.parametrize("reason", ["no-ci-result", "draft-images", "one-job-running"])
def test_a_partial_or_draft_rollup_does_not_consume_the_nudge(
    workflow: Workflow, reason: str
) -> None:
    _waiting(workflow)
    checks = workflow.state / "rollup-7.json"
    rollup = json.loads(checks.read_text())
    if reason == "no-ci-result":
        rollup = rollup[1:]
    elif reason == "draft-images":
        rollup[1]["conclusion"] = "SKIPPED"
    else:
        rollup[2]["status"] = "IN_PROGRESS"
    checks.write_text(json.dumps(rollup))
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []
    _checks(workflow)
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


@pytest.mark.parametrize("mode", ["checks-wait", "idle-reviewer", "train-turn"])
@pytest.mark.parametrize("conclusion", ["SUCCESS", "FAILURE"])
def test_ready_planner_supersedes_skipped_draft_planner_once(
    workflow: Workflow, mode: str, conclusion: str
) -> None:
    _waiting(workflow)
    if mode == "idle-reviewer":
        (workflow.notes(7) / "review-7.checks-wait").unlink()
        (workflow.notes(7) / "review-7.idle").touch()
    elif mode == "train-turn":
        (workflow.script.parent / "merge-when-current").write_text(
            '#!/bin/bash\necho \'{"first":"7","waiting":["7"],"blocked":[],"turn":1}\'\n'
        )
    checks = workflow.state / "rollup-7.json"
    rollup = json.loads(checks.read_text())
    draft_planner = {**rollup[1], "conclusion": "SKIPPED"}
    checks.write_text(json.dumps([draft_planner, rollup[0]]))
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []
    # GitHub retains the skipped draft checks beside the ready run on the same head.
    rollup[1]["conclusion"] = conclusion
    checks.write_text(json.dumps([draft_planner, *rollup]))
    first = workflow.run_script("tick")
    second = workflow.run_script("tick")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert len(_nudges(workflow)) == 1
    if mode == "train-turn":
        assert list((workflow.repo / ".local/workflow").glob("review-7.train-nudged-*"))


def test_only_an_owner_reply_clears_an_owner_block(workflow: Workflow) -> None:
    _waiting(workflow)
    workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": 'Write "merge it".'}, "review-7"
    )
    workflow.notify(
        {"hook_event_name": "UserPromptSubmit", "source": "background_task_output"}, "review-7"
    )
    workflow.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": _WAIT}, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []
    workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "merge it"}, "review-7")
    workflow.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": _WAIT}, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


@pytest.mark.parametrize(
    "message",
    [
        "Two rounds were sent back. Allow another round?",
        'The merge was refused. Write "merge it" and I will run it.',
        "Recommend option 1; alternatively option 2. Write 1 or 2.",
    ],
)
def test_owner_questions_from_older_hooks_are_left_alone(workflow: Workflow, message: str) -> None:
    _waiting(workflow)
    workflow.screen("● " + message + _BOX, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []


def test_an_old_question_does_not_hide_the_latest_wait(workflow: Workflow) -> None:
    _waiting(workflow)
    workflow.screen("● Which option?\n> 1\n● " + _WAIT + _BOX, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


@pytest.mark.parametrize("interaction", ["permission", "question-tool"])
def test_a_resolved_interaction_does_not_leave_an_owner_block(
    workflow: Workflow, interaction: str
) -> None:
    _waiting(workflow)
    event: dict[str, object]
    if interaction == "permission":
        event = {
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission",
        }
    else:
        event = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"}
    workflow.notify(event, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []
    # Approval/an answer continues the existing turn: no UserPromptSubmit hook runs.
    workflow.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "review-7"})
    workflow.notify({"hook_event_name": "Stop", "last_assistant_message": _WAIT}, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


def test_an_older_horizontal_rule_does_not_hide_the_latest_wait(workflow: Workflow) -> None:
    _waiting(workflow)
    workflow.screen("● Which option?\n" + "─" * 40 + "\n> 1\n● " + _WAIT + _BOX, "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1


def test_a_review_start_notes_its_time_after_forgetting_old_notes(workflow: Workflow) -> None:
    workflow.add_worktree(7, draft=False)
    workflow.notes(7).mkdir(parents=True)
    started = workflow.notes(7) / "review-7.started"
    started.touch()
    os.utime(started, (_STARTED, _STARTED))
    (workflow.notes(7) / "review-7.checks-wait").touch()
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    assert started.stat().st_mtime > _STARTED
    assert not (workflow.notes(7) / "review-7.checks-wait").exists()


def test_a_fresh_reviewer_on_an_already_nudged_head_gets_its_own_nudge(
    workflow: Workflow,
) -> None:
    head = _waiting(workflow)
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 1
    (workflow.state / "tmux" / "review-7").unlink()
    (workflow.state / "screen" / "review-7").unlink()
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stderr
    (workflow.notes(7) / "review-7.checks-wait").touch()
    workflow.screen("● " + _WAIT + "\n" + _BOX, "review-7")
    _review(workflow, head, submitted_at="2099-01-01T00:00:00Z")
    assert workflow.run_script("tick").returncode == 0
    assert len(_nudges(workflow)) == 2


def test_a_screen_without_an_input_box_is_not_typed_into(workflow: Workflow) -> None:
    head = _waiting(workflow)
    workflow.screen("● " + _WAIT + "\n> half typed\n", "review-7")
    assert workflow.run_script("tick").returncode == 0
    assert workflow.run("checks-finished", "review-7", head).returncode != 0
    assert workflow.calls("tmux review-7 send-keys") == []


@pytest.mark.parametrize("line", ["review finished", "review sent back"])
def test_a_finished_reviewer_is_not_nudged(workflow: Workflow, line: str) -> None:
    _waiting(workflow)
    workflow.screen("● Done.\n\n" + line + "\n" + _BOX, "review-7")
    workflow.run_script("tick")
    assert _nudges(workflow) == []


@pytest.mark.parametrize("blocked", ["owner-blocked", "interaction-blocked", "pushed", "limit"])
def test_a_wait_note_without_a_round_does_not_override_blocks(
    workflow: Workflow, blocked: str
) -> None:
    _waiting(workflow)
    (workflow.state / "reviews-7.json").write_text("[]")
    if blocked == "limit":
        workflow.screen("You've hit your session limit · resets 4pm\n" + _BOX, "review-7")
    else:
        (workflow.notes(7) / f"review-7.{blocked}").touch()
    assert workflow.run_script("tick").returncode == 0
    assert _nudges(workflow) == []


@pytest.mark.parametrize(
    "text",
    [
        "\u276f\u00a0wait",
        "\u276f\u2003\u043e\u0442\u0432\u0435\u0442",
        "\u276f\u00a0\n\u2009do 9 first",
    ],
)
def test_unicode_spaces_do_not_hide_unsent_input(workflow: Workflow, text: str) -> None:
    head = _waiting(workflow)
    workflow.screen(_WAIT + _BOX.replace("> \n", text + "\n"), "review-7")
    done = workflow.run("checks-finished", "review-7", head)
    assert done.returncode != 0
    assert "text in its box" in done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []


@pytest.mark.parametrize("space", ["\u00a0", "\u1680", "\u2003", "\u202f", "\u205f", "\u3000"])
def test_unicode_space_only_input_allows_a_check_nudge(workflow: Workflow, space: str) -> None:
    head = _waiting(workflow)
    workflow.screen(_WAIT + _BOX.replace("> \n", f"\u276f{space}\n \t{space}\n"), "review-7")
    done = workflow.run("checks-finished", "review-7", head, env={"LC_ALL": "C"})
    assert done.returncode == 0, done.stderr
    assert len(_nudges(workflow)) == 1


@pytest.mark.parametrize("swallowed", [1, 2])
def test_swallowed_enter_is_retried_and_failure_is_logged(
    workflow: Workflow, swallowed: int
) -> None:
    head = _waiting(workflow)
    (workflow.state / "swallow-enter-review-7").write_text(str(swallowed))
    done = workflow.run_script("tick")
    assert len(workflow.calls("tmux review-7 send-keys -t =review-7: Enter")) == 2
    note = workflow.notes(7) / "review-7.typed"
    if swallowed == 1:
        assert done.returncode == 0, done.stderr
        assert not note.exists()
        assert head in workflow.wake_log.read_text()
    else:
        assert done.returncode != 0
        assert "still has automation text" in workflow.wake_log.read_text()
        assert note.read_text().startswith(f"The checks on {head}")
        assert not list((workflow.repo / ".local/workflow").glob("review-7.checks-nudged-*"))


def test_next_tick_sends_its_previous_text_without_typing_again(workflow: Workflow) -> None:
    head = _waiting(workflow)
    prompt = (
        f"The checks on {head} have finished. Read them once and go on by the rules: "
        "merge when they all passed, fix or send back when one failed."
    )
    (workflow.notes(7) / "review-7.typed").write_text(prompt)
    workflow.screen(_WAIT + _BOX.replace("> \n", "> " + prompt + "\n"), "review-7")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stderr
    assert _nudges(workflow) == []
    assert len(workflow.calls("tmux review-7 send-keys -t =review-7: Enter")) == 1
    assert not (workflow.notes(7) / "review-7.typed").exists()


def test_recorded_automation_does_not_authorize_other_text(workflow: Workflow) -> None:
    _waiting(workflow)
    (workflow.notes(7) / "review-7.typed").write_text("previous automation text")
    workflow.screen(_WAIT + _BOX.replace("> \n", "> owner's unfinished reply\n"), "review-7")
    workflow.run_script("tick")
    assert workflow.calls("tmux review-7 send-keys") == []


@pytest.mark.parametrize("appears", [True, False])
def test_enter_waits_for_the_text_to_appear(workflow: Workflow, appears: bool) -> None:
    head = _waiting(workflow)
    (workflow.state / "hide-input-review-7").touch()
    if appears:
        pending = workflow.state / "pending-input-review-7"
        visible = workflow.state / "input-review-7"
        (workflow.state / "on-sleep").write_text(
            f'if [ -f "{pending}" ]; then mv "{pending}" "{visible}"; fi'
        )
    done = workflow.run("checks-finished", "review-7", head)
    enters = workflow.calls("tmux review-7 send-keys -t =review-7: Enter")
    if appears:
        assert done.returncode == 0, done.stderr
        assert len(enters) == 1
        calls = workflow.calls("")
        literal = _nudges(workflow)[0]
        assert "sleep 0.1" in calls[calls.index(literal) : calls.index(enters[0])]
    else:
        assert done.returncode != 0
        assert "did not appear" in done.stderr
        assert enters == []
        assert (workflow.notes(7) / "review-7.typed").exists()


@pytest.mark.parametrize("drawing", ["wrapped", "partial", "wrapped-again"])
def test_rendering_does_not_prevent_delivery(workflow: Workflow, drawing: str) -> None:
    head = _waiting(workflow)
    tmux = workflow.bin / "tmux"
    script = tmux.read_text()
    if drawing.startswith("wrapped"):
        # Claude pads each drawn row to the box's width, and `capture-pane -J` keeps it.
        script = script.replace(
            'lines[rules[-2] + 1:rules[-1]] = ["> " + text]',
            "import textwrap\n    lines[rules[-2] + 1:rules[-1]] = [row.ljust(78) for row in "
            'textwrap.wrap("> " + text, width=78, subsequent_indent="  ") or ["> "]]',
        )
    else:
        complete = workflow.state / "draw-complete"
        script = script.replace(
            "text = Path(sys.argv[2]).read_text()",
            "text = Path(sys.argv[2]).read_text()\n"
            f'if not Path("{complete}").exists(): text = text[:20]',
        )
        (workflow.state / "on-sleep").write_text(f'touch "{complete}"')
    tmux.write_text(script)
    if drawing == "wrapped-again":
        prompt = (
            f"The checks on {head} have finished. Read them once and go on by the rules: "
            "merge when they all passed, fix or send back when one failed."
        )
        (workflow.notes(7) / "review-7.typed").write_text(prompt)
        (workflow.state / "input-review-7").write_text(prompt)
    done = workflow.run("checks-finished", "review-7", head)
    assert done.returncode == 0, done.stderr
    assert len(workflow.calls("tmux review-7 send-keys -t =review-7: Enter")) == 1
    assert not (workflow.notes(7) / "review-7.typed").exists()


def test_owner_edits_during_paste_settling_are_not_submitted(workflow: Workflow) -> None:
    head = _waiting(workflow)
    visible = workflow.state / "input-review-7"
    (workflow.state / "on-sleep").write_text(f'echo "owner edited the input" > "{visible}"')
    done = workflow.run("checks-finished", "review-7", head)
    assert done.returncode != 0
    assert "input changed before Enter" in done.stderr
    assert workflow.calls("tmux review-7 send-keys -t =review-7: Enter") == []


@pytest.mark.parametrize("state", ["CONFLICTING UNKNOWN", "UNKNOWN DIRTY"])
def test_conflict_wakes_an_idle_reviewer_without_checks_or_a_round_once(
    workflow: Workflow, state: str
) -> None:
    head = _waiting(workflow)
    (workflow.state / "mergeability-7").write_text(state)
    (workflow.state / "rollup-7.json").write_text("[]")
    (workflow.state / "reviews-7.json").write_text("[]")
    (workflow.notes(7) / "review-7.checks-wait").unlink()
    (workflow.notes(7) / "review-7.idle").touch()
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    wakes = workflow.calls("tmux review-7 send-keys")
    assert len(wakes) == 2
    assert f"GitHub reports a merge conflict on {head}" in wakes[0]
    assert "merge main: conflict in <files>" in wakes[0]
    assert "merge conflict" in workflow.wake_log.read_text()


@pytest.mark.parametrize(
    "block", ["owner-blocked", "interaction-blocked", "pushed", "limit", "typed", "question"]
)
def test_conflict_wake_respects_owner_and_session_blocks(workflow: Workflow, block: str) -> None:
    head = _waiting(workflow)
    (workflow.state / "mergeability-7").write_text("CONFLICTING DIRTY")
    (workflow.state / "rollup-7.json").write_text("[]")
    if block == "limit":
        workflow.screen("You've hit your session limit\n" + _BOX, "review-7")
    elif block == "typed":
        workflow.screen("● Waiting.\n" + _BOX.replace("> \n", "> unfinished reply\n"), "review-7")
    elif block == "question":
        workflow.screen("● Which resolution should I use?\n" + _BOX, "review-7")
    else:
        (workflow.notes(7) / f"review-7.{block}").touch()
    done = workflow.run_script("tick")
    assert done.returncode == (1 if block == "typed" else 0), done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert not (workflow.repo / f".local/workflow/review-7.conflict-nudged-{head}").exists()
    # Once unsent input is cleared the failed delivery is retried, not lost.
    if block == "typed":
        workflow.screen("● Waiting.\n" + _BOX, "review-7")
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert len(workflow.calls("tmux review-7 send-keys")) == 2


def test_stacked_conflict_does_not_receive_an_instruction_to_merge_main(workflow: Workflow) -> None:
    head = _waiting(workflow)
    (workflow.state / "mergeability-7").write_text("CONFLICTING DIRTY")
    (workflow.state / "base-branch-7").write_text("ticket/parent")
    (workflow.state / "rollup-7.json").write_text("[]")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
    assert not (workflow.repo / f".local/workflow/review-7.conflict-nudged-{head}").exists()
    # Retargeting after the parent merges must still receive this head's conflict wake.
    (workflow.state / "base-branch-7").write_text("main")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux review-7 send-keys")) == 2


def test_a_fresh_reviewer_gets_a_conflict_wake_for_the_same_head(workflow: Workflow) -> None:
    head = _waiting(workflow)
    (workflow.state / "mergeability-7").write_text("CONFLICTING DIRTY")
    (workflow.state / "rollup-7.json").write_text("[]")
    assert workflow.run_script("tick").returncode == 0
    assert len(workflow.calls("tmux review-7 send-keys")) == 2
    (workflow.state / "tmux/review-7").unlink()
    (workflow.state / "screen/review-7").unlink()
    done = workflow.run("start", "review", "7")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (workflow.repo / f".local/workflow/review-7.conflict-nudged-{head}").exists()
    (workflow.notes(7) / "review-7.checks-wait").touch()
    workflow.screen("● " + _WAIT + "\n" + _BOX, "review-7")
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("tmux review-7 send-keys")) == 4


@pytest.mark.parametrize("kind", ["review", "audit"])
def test_a_member_merged_in_a_batch_is_told_once_to_finish(workflow: Workflow, kind: str) -> None:
    head = _waiting(workflow)
    name = f"{kind}-7"
    if kind == "audit":
        workflow.running(name)
        (workflow.notes(7) / "audit-7.checks-wait").touch()
        workflow.screen("● " + _WAIT + "\n" + _BOX, name)
        (workflow.state / "tmux/review-7").unlink()
    # Another member's gate call squashed it; it is no longer open or in the train.
    workflow.set_pull_request(7, "MERGED", head)
    squash = "a" * 40
    (workflow.repo / ".local/workflow").mkdir(parents=True, exist_ok=True)
    (workflow.repo / ".local/workflow/batch-merged-7").write_text(
        json.dumps({"by": "6", "squash": squash})
    )
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    wakes = workflow.calls(f"tmux {name} send-keys -t ={name}: -l Pull request 7 was merged")
    assert len(wakes) == 1
    finished = "review finished" if kind == "review" else "audit finished"
    assert wakes[0].endswith(
        f"Pull request 7 was merged by the merge train, in a batch (commit {squash}). Do not call "
        f"the gate again. Do its after-merge duties by the rules, then end with `{finished}`."
    )
    assert f"{name} merged in a batch as {squash}" in workflow.wake_log.read_text()


def test_a_batch_merged_member_waiting_for_the_owner_is_left_alone(workflow: Workflow) -> None:
    head = _waiting(workflow)
    workflow.set_pull_request(7, "MERGED", head)
    (workflow.notes(7) / "review-7.owner-blocked").touch()
    (workflow.repo / ".local/workflow").mkdir(parents=True, exist_ok=True)
    (workflow.repo / ".local/workflow/batch-merged-7").write_text(
        json.dumps({"by": "6", "squash": "a" * 40})
    )
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("tmux review-7 send-keys") == []
