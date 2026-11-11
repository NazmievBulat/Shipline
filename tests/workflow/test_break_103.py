"""Independent PR 103 break tests; external services and panes are stand-ins.

Run this file alone. The tests drive the shipped commands, not builder test helpers.
"""

import fcntl
import json
import os
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from workflow_harness import Workflow, install_shipline


NOW = 1_800_000_000
BOX = "─" * 40 + "\n> \n" + "─" * 40 + "\n"
STATUS = (
    "Status check: if what you were waiting for is done, carry on; if not, run "
    "`scripts/workflow/notify waiting` and end your turn."
)


def stamp(path: Path, at: int, text: str = "") -> None:
    path.write_text(text)
    os.utime(path, (at, at))


def note(workflow: Workflow, suffix: str) -> Path:
    return workflow.notes(7) / f"ticket-7.{suffix}"


def prepare(workflow: Workflow, *, age: int = 3000) -> None:
    workflow.add_worktree(7)
    (workflow.repo / ".local/workflow").mkdir(parents=True, exist_ok=True)
    workflow.notes(7).mkdir(parents=True, exist_ok=True)
    workflow.running("ticket-7")
    workflow.running("codex-7")
    note(workflow, "codex").touch()
    note(workflow, "bridge-stopped").touch()
    turn = json.dumps({"turn-id": "example-wait", "last-assistant-message": "Waiting for the job."})
    note(workflow, "codex-turn.json").write_text(turn)
    note(workflow, "codex-delivered").write_text(turn)
    stamp(note(workflow, "job-wait"), NOW - age)
    workflow.screen("● Waiting for the job.\n" + BOX, "ticket-7")
    workflow.screen("• Waiting for the job.\n\n› \n\n  ? for shortcuts\n", "codex-7")


def tick(workflow: Workflow, at: int = NOW, *args: str):
    return workflow.run_script("tick", *args, env={"WORKFLOW_NOW": str(at), "TZ": "UTC"})


def keys(workflow: Workflow) -> list[str]:
    prefix = "tmux codex-7 send-keys -t =codex-7: "
    return [line.removeprefix(prefix).removeprefix("-l ")
            for line in workflow.calls(prefix)]


def no_owner_answer(workflow: Workflow) -> None:
    assert not note(workflow, "owner-reply").exists()
    assert not note(workflow, "codex-answered").exists()


@pytest.mark.parametrize("outcome", ["completed", "failed", "scope-missing"])
def test_ended_job_resumes_once_then_a_new_wait_gets_a_new_status(workflow, outcome):
    prepare(workflow, age=2999)
    # A scope and its exit state must not be required for the status question.
    if outcome != "scope-missing":
        scope = workflow.cgroup_root / "example-job.scope"
        scope.mkdir()
        (scope / "cgroup.events").write_text("populated 0\n")
        (workflow.state / "job-result").write_text(outcome)
    assert tick(workflow).returncode == 0
    assert keys(workflow) == []
    assert tick(workflow, NOW + 1).returncode == 0
    assert tick(workflow, NOW + 180).returncode == 0
    assert keys(workflow) == [STATUS, "Enter"]
    no_owner_answer(workflow)
    assert note(workflow, "codex-followed-up").read_bytes() == note(workflow, "codex-turn.json").read_bytes()
    assert not note(workflow, "job-wait").exists()
    stamp(note(workflow, "job-wait"), NOW + 180)
    assert tick(workflow, NOW + 3180).returncode == 0
    assert keys(workflow) == [STATUS, "Enter", STATUS, "Enter"]


def test_real_wait_and_relay_hooks_leave_an_idle_worker_that_can_resume(workflow):
    prepare(workflow)
    note(workflow, "job-wait").unlink()
    assert workflow.run_script("notify", "waiting", env={"WORKFLOW_SESSION": "ticket-7"}).returncode == 0
    assert workflow.notify({"hook_event_name": "UserPromptSubmit", "prompt": "__WORKFLOW_CODEX_TURN__"}).returncode == 0
    assert workflow.notify({"hook_event_name": "Stop", "last_assistant_message": "Waiting for the job."}).returncode == 0
    stamp(note(workflow, "job-wait"), NOW - 3000)
    listed = workflow.run("list")
    assert "ticket-7  idle" in listed.stdout
    assert tick(workflow).returncode == 0
    assert keys(workflow) == [STATUS, "Enter"]
    no_owner_answer(workflow)


@pytest.mark.parametrize("missing", ["codex-turn.json", "codex-delivered", "bridge-stopped"])
def test_missing_relay_receipt_never_sends_and_recovers_when_restored(workflow, missing):
    prepare(workflow)
    saved = note(workflow, missing).read_bytes()
    note(workflow, missing).unlink()
    assert tick(workflow).returncode == 0
    assert keys(workflow) == []
    note(workflow, missing).write_bytes(saved)
    assert tick(workflow, NOW + 180).returncode == 0
    assert keys(workflow) == [STATUS, "Enter"]


@pytest.mark.parametrize("block", ["asking", "owner-reply", "native-draft", "relay-draft", "working", "permission"])
def test_block_arriving_after_paste_survives_retry(workflow, block):
    prepare(workflow)
    if block == "asking":
        action = f"touch {shlex.quote(str(note(workflow, 'asking')))}"
    elif block == "owner-reply":
        action = f"printf '%s' 'Owner reply' > {shlex.quote(str(note(workflow, 'owner-reply')))}"
    elif block in ("native-draft", "relay-draft"):
        target = "codex-7" if block == "native-draft" else "ticket-7"
        action = f"printf '%s' 'Owner draft' > {shlex.quote(str(workflow.state / f'input-{target}'))}"
    else:
        screen = ("• Working (5s • esc to interrupt)\n\n› \n" if block == "working"
                  else "› \nPress enter to confirm or esc to cancel\n")
        action = f"printf '%s' {shlex.quote(screen)} > {shlex.quote(str(workflow.state / 'screen/codex-7'))}"
    typed = shlex.quote(str(workflow.state / "input-codex-7"))
    hook = workflow.state / "on-sleep"
    hook.write_text(f"if [[ -s {typed} ]]; then {action}; fi\n")
    tick(workflow)
    hook.unlink()
    tick(workflow, NOW + 180)
    assert keys(workflow) == [STATUS]
    assert note(workflow, "job-wait").exists()
    assert not note(workflow, "codex-followed-up").exists()
    if block == "owner-reply":
        assert note(workflow, "owner-reply").read_text() == "Owner reply"
    else:
        no_owner_answer(workflow)


def test_lock_held_by_another_action_preserves_wait_then_recovers(workflow):
    prepare(workflow)
    lock = workflow.repo / ".local/workflow/start-7.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        done = workflow.run("status-check", "ticket-7")
        assert done.returncode != 0
        assert time.monotonic() - started < 5
        assert keys(workflow) == []
        assert note(workflow, "job-wait").exists()
    assert tick(workflow, NOW + 180).returncode == 0
    assert keys(workflow) == [STATUS, "Enter"]


def test_two_simultaneous_status_calls_submit_at_most_once(workflow):
    prepare(workflow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(workflow.run, "status-check", "ticket-7") for _ in range(2)]
        results = [call.result() for call in calls]
    assert any(done.returncode == 0 for done in results)
    assert keys(workflow) == [STATUS, "Enter"]
    no_owner_answer(workflow)


@pytest.mark.parametrize("rollup", ["missing", "late", "partial-rerun"])
def test_check_state_does_not_strand_a_waiting_worker(workflow, rollup):
    prepare(workflow)
    checks = [] if rollup == "missing" else [
        {"__typename": "CheckRun", "name": "CI result", "status": "IN_PROGRESS", "conclusion": None},
    ]
    if rollup == "partial-rerun":
        checks.append({"__typename": "CheckRun", "name": "CI: tests", "status": "COMPLETED", "conclusion": "SUCCESS"})
    (workflow.state / "rollup-7.json").write_text(json.dumps(checks))
    assert tick(workflow).returncode == 0
    assert keys(workflow) == [STATUS, "Enter"]
    assert workflow.calls("tmux ticket-7 send-keys") == []
    no_owner_answer(workflow)


def test_interrupted_first_delivery_recovers_with_github_unavailable(workflow):
    prepare(workflow)
    (workflow.state / "swallow-enter-codex-7").write_text("2")
    first = tick(workflow)
    assert first.returncode != 0
    assert note(workflow, "job-wait").exists()
    (workflow.state / "fail-gh-list").touch()
    tick(workflow, NOW + 180)
    assert keys(workflow) == [STATUS, "Enter", "Enter", "Enter"]
    assert not note(workflow, "job-wait").exists()
    no_owner_answer(workflow)


def test_stale_wait_never_marks_a_running_native_turn_idle(workflow):
    prepare(workflow)
    note(workflow, "idle").touch()
    workflow.screen("• Working (5s • esc to interrupt)\n\n› \n", "codex-7")
    assert "ticket-7  busy" in workflow.run("list").stdout
    assert tick(workflow).returncode == 0
    assert keys(workflow) == []


def test_installed_commands_use_the_registered_state_folder(workflow):
    prepare(workflow)
    install_shipline(workflow)
    done = tick(workflow)
    assert done.returncode == 0
    installed_status = STATUS.replace("scripts/workflow/notify", f"{workflow.home}/bin/shipline notify")
    matches = keys(workflow) == [installed_status, "Enter"]
    assert matches, "the installed launcher must be used for the waiting instruction"
    no_owner_answer(workflow)


@pytest.mark.parametrize("blocked", ["native-draft", "permission", "unrelayed-turn"])
def test_status_shadow_matches_real_refusal_of_a_blocked_native_wait(workflow, blocked):
    prepare(workflow)
    assert tick(workflow, NOW, "--shadow").returncode == 0
    log = workflow.repo / ".local/shipline/shadow.log"
    reported = "status check sent" in log.read_text()
    assert reported, "control shadow must reach the waiting status rule"
    log.write_text("")
    if blocked == "native-draft":
        (workflow.state / "input-codex-7").write_text("Owner draft")
    elif blocked == "permission":
        workflow.screen("› \nPress enter to confirm or esc to cancel\n", "codex-7")
    else:
        note(workflow, "codex-turn.json").write_text(json.dumps({
            "turn-id": "example-question", "last-assistant-message": "Which limit should I use?",
        }))
    assert tick(workflow).returncode == 0
    assert keys(workflow) == [], "the real tick must refuse this blocked delivery"
    done = tick(workflow, NOW + 180, "--shadow")
    assert done.returncode == 0
    reported = "status check sent" in log.read_text()
    assert not reported, "shadow claims delivery that the real tick refuses"
    assert keys(workflow) == []
