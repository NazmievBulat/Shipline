"""Independent delivery and safety probes for PR 90; run this file alone.

Use the existing external-service stand-ins, but exercise only these new tests.
No real model, GitHub mutation, or existing test is run by this file.
"""

import fcntl
import json
import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from workflow_harness import Workflow, make_workflow


NOW = 1_790_000_000
STATUS = (
    "Status check: if what you were waiting for is done, carry on; if not, run "
    "`scripts/workflow/notify waiting` and end your turn."
)
BOX = "─" * 72 + "\n> \n" + "─" * 72 + "\n"


@pytest.fixture
def flow(tmp_path: Path) -> Workflow:
    programs = tmp_path / "bin"
    programs.mkdir()
    return make_workflow(tmp_path, programs)


def prepare(
    flow: Workflow, name: str = "review-7", *, wait: bool = False, draft: bool = True
) -> Path:
    flow.add_worktree(7, draft=draft)
    flow.running(name)
    flow.screen("● Waiting for checks.\n" + BOX, name)
    notes = flow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    prefix = notes / name
    prefix.with_suffix(".typed").write_text(STATUS)
    (flow.state / f"input-{name}").write_text(STATUS)
    if wait:
        mark = prefix.with_suffix(".job-wait")
        mark.touch()
        os.utime(mark, (NOW - 3600, NOW - 3600))
    return prefix


def tick(flow: Workflow, at: int = NOW) -> subprocess.CompletedProcess[str]:
    return flow.run_script("tick", env={"WORKFLOW_NOW": str(at)})


def keys(flow: Workflow, name: str = "review-7") -> list[str]:
    prefix = f"tmux {name} send-keys -t ={name}: "
    return [call.removeprefix(prefix) for call in flow.calls(prefix)]


def retry_note(flow: Workflow, name: str = "review-7") -> Path:
    return flow.repo / ".local/workflow" / f"{name}.status-retry"


@pytest.mark.parametrize("name", ["ticket-7", "review-7"])
@pytest.mark.parametrize("wait", [False, True])
def test_later_tick_sends_exact_status_without_retyping(flow: Workflow, name: str, wait: bool):
    prefix = prepare(flow, name, wait=wait)
    result = tick(flow)
    assert result.returncode == 0, result.stderr
    assert keys(flow, name) == ["Enter"]
    assert not prefix.with_suffix(".typed").exists()
    assert not retry_note(flow, name).exists()


def test_two_initial_enters_can_fail_and_hook_can_remove_all_waits(flow: Workflow):
    prefix = prepare(flow, "ticket-7", wait=True)
    prefix.with_suffix(".typed").unlink()
    (flow.state / "input-ticket-7").write_text("")
    (flow.state / "swallow-enter-ticket-7").write_text("2")
    wait = shlex.quote(str(prefix.with_suffix(".job-wait")))
    (flow.state / "on-sleep").write_text(f"rm -f {wait}\n")
    first = tick(flow)
    assert first.returncode == 1
    assert keys(flow, "ticket-7") == ["-l " + STATUS, "Enter", "Enter"]
    assert not prefix.with_suffix(".job-wait").exists()
    second = tick(flow, NOW + 180)
    assert second.returncode == 0, second.stderr
    assert keys(flow, "ticket-7") == ["-l " + STATUS, "Enter", "Enter", "Enter"]


def test_restarts_keep_three_attempt_bound_and_one_alert(flow: Workflow):
    prepare(flow)
    (flow.state / "swallow-enter-review-7").write_text("100")
    results = [tick(flow, NOW + 180 * i).returncode for i in range(6)]
    assert results == [1, 1, 1, 0, 0, 0]
    assert keys(flow) == ["Enter"] * 3
    assert retry_note(flow).read_text().split()[1:] == ["3", "1"]
    alert = "stuck? review-7 (status message not taken; its text stays in the box)"
    assert sum(line.split(" ", 1)[-1] == alert
               for line in flow.wake_log.read_text().splitlines()) == 1


@pytest.mark.parametrize("text", [
    "Owner draft", STATUS + " and keep the draft", STATUS + "\n  Owner continuation",
    "An earlier automation instruction", "$(touch example-marker); `false`",
])
def test_changed_input_is_never_sent_or_interrupted(flow: Workflow, text: str):
    prepare(flow, wait=True)
    (flow.state / "input-review-7").write_text(text)
    for i in range(2):
        result = tick(flow, NOW + 3600 * i)
        assert result.returncode == 0, result.stderr
    assert keys(flow) == []
    assert (flow.state / "input-review-7").read_text() == text
    assert not retry_note(flow).exists()
    assert not (flow.repo / "example-marker").exists()


@pytest.mark.parametrize("guard", ["asking", "owner-blocked", "interaction-blocked", "codex", "permission"])
def test_owner_and_permission_guards_block_recovery(flow: Workflow, guard: str):
    prefix = prepare(flow)
    if guard == "permission":
        flow.screen("Do you want to run this command?\n" + BOX, "review-7")
    else:
        prefix.with_suffix("." + guard).touch()
    result = tick(flow)
    assert result.returncode == 0, result.stderr
    assert keys(flow) == []


def test_input_edited_during_paste_settle_is_preserved(flow: Workflow):
    prefix = prepare(flow)
    source = flow.state / "owner-edit"
    source.write_text(STATUS + " plus an owner edit")
    target = flow.state / "input-review-7"
    (flow.state / "on-sleep").write_text(
        f"cp {shlex.quote(str(source))} {shlex.quote(str(target))}\n"
    )
    result = tick(flow)
    assert result.returncode == 0, result.stderr
    assert keys(flow) == []
    assert prefix.with_suffix(".typed").exists()
    assert target.read_text() == STATUS + " plus an owner edit"


def test_missing_ownership_note_does_not_claim_owner_input(flow: Workflow):
    prefix = prepare(flow)
    prefix.with_suffix(".typed").unlink()
    result = tick(flow)
    assert result.returncode == 0, result.stderr
    assert keys(flow) == []


def test_a_held_tick_lock_never_touches_input_and_release_recovers(flow: Workflow):
    prepare(flow)
    with (flow.repo / ".git/shipline-tick.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = tick(flow)
        assert result.returncode == 0
        assert "still running" in result.stdout
        assert keys(flow) == []
        assert not retry_note(flow).exists()
    result = tick(flow, NOW + 180)
    assert result.returncode == 0, result.stderr
    assert keys(flow) == ["Enter"]


def test_overlapping_ticks_spend_only_one_attempt(flow: Workflow):
    prepare(flow)
    (flow.state / "swallow-enter-review-7").write_text("100")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tick(flow), range(2)))
    assert sorted(result.returncode for result in results) == [0, 1]
    assert keys(flow) == ["Enter"]
    assert retry_note(flow).read_text().split()[1:] == ["1", "0"]


def test_interrupted_enter_keeps_its_attempt_on_the_next_tick(flow: Workflow):
    prepare(flow)
    # Interrupt the session helper after the retry count has been persisted, at
    # the same paste-settle boundary at which a process can time out or stop.
    marker = flow.state / "interrupt-once"
    (flow.state / "on-sleep").write_text(
        f"if [[ ! -e {shlex.quote(str(marker))} ]]; then "
        f"touch {shlex.quote(str(marker))}; kill -TERM \"$PPID\"; fi\n"
    )
    first = tick(flow)
    assert first.returncode == 1
    assert retry_note(flow).read_text().split()[1:] == ["1", "0"]
    (flow.state / "on-sleep").unlink()
    (flow.state / "swallow-enter-review-7").write_text("100")
    for i in range(1, 5):
        tick(flow, NOW + i * 180)
    assert keys(flow) == ["Enter", "Enter"]
    assert retry_note(flow).read_text().split()[1:] == ["3", "1"]


@pytest.mark.parametrize("check_state", ["missing", "pending", "complete", "partial-rerun"])
def test_check_changes_cannot_bypass_recovery_bound(flow: Workflow, check_state: str):
    prefix = prepare(flow, draft=False)
    for suffix in ("started", "checks-wait"):
        mark = prefix.with_suffix("." + suffix)
        mark.touch()
        os.utime(mark, (NOW - 7200, NOW - 7200))
    (flow.state / "swallow-enter-review-7").write_text("100")
    checks = []
    if check_state != "missing":
        for index, name in enumerate(["CI result", "Docker images: what to run"]):
            status = "COMPLETED" if check_state == "complete" or (
                check_state == "partial-rerun" and index == 0
            ) else "IN_PROGRESS"
            checks.append({"__typename": "CheckRun", "name": name,
                           "status": status, "conclusion": "SUCCESS" if status == "COMPLETED" else ""})
    (flow.state / "rollup-7.json").write_text(json.dumps(checks))
    for i in range(5):
        tick(flow, NOW + 180 * i)
    assert keys(flow) == ["Enter"] * 3
    assert retry_note(flow).read_text().split()[1:] == ["3", "1"]
    assert not list((flow.repo / ".local/workflow").glob("review-7.checks-nudged-*"))
    if check_state == "complete":
        (flow.state / "input-review-7").write_text("")
        (flow.state / "swallow-enter-review-7").unlink()
        result = tick(flow, NOW + 1080)
        assert result.returncode == 0, result.stderr
        assert len(list((flow.repo / ".local/workflow").glob("review-7.checks-nudged-*"))) == 1


def test_empty_box_releases_exhausted_status_route(flow: Workflow):
    prepare(flow)
    (flow.state / "swallow-enter-review-7").write_text("100")
    for i in range(4):
        tick(flow, NOW + 180 * i)
    (flow.state / "input-review-7").write_text("")
    result = flow.run("status-pending", "review-7")
    assert result.returncode == 3
    assert keys(flow) == ["Enter"] * 3


def test_a_completed_run_with_a_lost_check_cannot_bypass_status_retries(flow: Workflow):
    prefix = prepare(flow, draft=False)
    for suffix in ("started", "checks-wait"):
        prefix.with_suffix("." + suffix).touch()
    checks = [
        {"__typename": "CheckRun", "name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "CI: server tests", "status": "IN_PROGRESS",
         "startedAt": "2026-09-21T08:00:00Z", "conclusion": None,
         "detailsUrl": "https://github.com/example-org/example-app/actions/runs/81/job/82"},
    ]
    (flow.state / "rollup-7.json").write_text(json.dumps(checks))
    (flow.state / "run-81.json").write_text(json.dumps({
        "id": 81, "status": "completed", "conclusion": "success",
        "updated_at": "2026-09-21T08:10:00Z",
    }))
    gh = flow.bin / "gh"
    flow.fake("gh", gh.read_text().replace(
        "snapshot_fields=",
        'if [[ $1 == api && $2 == */actions/runs/81 ]]; then '
        'cat "@STATE@/run-81.json"; exit 0; fi\nsnapshot_fields=', 1,
    ))
    head = flow.git("rev-parse", "HEAD", cwd=flow.worktree(7))
    diagnosed = flow.run_python(
        "tick_lost_check.py", str(flow.repo / ".local/workflow"), "7",
        stdin=json.dumps({"headRefOid": head, "statusCheckRollup": checks}),
        env={"WORKFLOW_NOW": str(NOW)},
    )
    assert diagnosed.returncode == 0, diagnosed.stderr
    (flow.state / "swallow-enter-review-7").write_text("100")
    for i in range(5):
        tick(flow, NOW + i * 180)
    assert keys(flow) == ["Enter"] * 3
    assert retry_note(flow).read_text().split()[1:] == ["3", "1"]
    assert not list((flow.repo / ".local/workflow").glob("review-7.lost-nudged-*"))


@pytest.mark.parametrize("restored", [STATUS, "Owner draft restored by Escape"])
def test_escape_restored_text_uses_status_recovery_only_for_owned_text(flow: Workflow, restored: str):
    prepare(flow, "ticket-7")
    (flow.state / "input-ticket-7").write_text("")
    flow.screen("● A command is still running.\n" + BOX, "ticket-7")
    (flow.state / "input-after-escape-ticket-7").write_text(restored)
    assert tick(flow).returncode == 0
    assert tick(flow, NOW + 2700).returncode == 0
    assert keys(flow, "ticket-7") == ["Escape"]
    assert tick(flow, NOW + 2880).returncode == 0
    assert keys(flow, "ticket-7") == (["Escape", "Enter"] if restored == STATUS else ["Escape"])


@pytest.mark.parametrize("rules", [0, 1, 2])
def test_usage_reset_recovery_keeps_its_bound_during_github_pause(flow: Workflow, rules: int):
    prefix = prepare(flow, "ticket-7")
    (flow.state / "swallow-enter-ticket-7").write_text("100")
    now = 1_792_119_720
    prefix.with_suffix(".idle").touch()
    prefix.with_suffix(".limit").write_text(str(now - 120) + "\n")
    limit_box = BOX if rules == 2 else ("─" * 72 + "\n" if rules else "") + "> \n"
    flow.screen("  ⎿  You've hit your session limit · resets 3am (UTC)\n" + limit_box, "ticket-7")
    budget = Path(flow.env["HOME"]) / ".local/state/workflow/github-budget"
    budget.mkdir(parents=True)
    (budget / "budget.json").write_text(json.dumps({
        "core": {"remaining": 499, "reset": int(time.time()) + 3600}
    }))
    for i in range(5):
        tick(flow, now + 180 * i)
    assert keys(flow, "ticket-7") == ["Enter"] * 3
    assert retry_note(flow, "ticket-7").read_text().split()[1:] == ["3", "1"]


@pytest.mark.parametrize("failure", ["budget-pause", "listing-failure"])
def test_local_status_recovery_continues_when_github_is_unavailable(flow: Workflow, failure: str):
    prepare(flow)
    if failure == "budget-pause":
        budget = Path(flow.env["HOME"]) / ".local/state/workflow/github-budget"
        budget.mkdir(parents=True)
        (budget / "budget.json").write_text(json.dumps({
            "core": {"remaining": 499, "reset": int(time.time()) + 3600}
        }))
    else:
        (flow.state / "fail-gh-list").touch()
    results = [tick(flow, NOW + 180 * i).returncode for i in range(3)]
    assert flow.run("would-retry-status", "review-7").returncode == 0
    assert keys(flow) == ["Enter"], (
        f"GitHub {failure}: three ticks returned {results}; "
        f"expected local recovery Enter, got {keys(flow)}"
    )
