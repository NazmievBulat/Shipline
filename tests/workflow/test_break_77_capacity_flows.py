"""Only breaker-authored cases; run this file independently of the existing suite."""

import fcntl
from concurrent.futures import ThreadPoolExecutor

import pytest
from break_77_support import CONTINUE, STOPPED, enters, stopped_session, tick
from workflow_harness import Workflow


@pytest.mark.parametrize("kind", ["ticket", "review"])
def test_daily_stop_recovers_with_persistent_bounded_backoff(workflow: Workflow, kind: str):
    name, target, notes = stopped_session(workflow, kind)
    workflow.orchestrator(status="busy")
    for offset in (0, 299):
        assert tick(workflow, offset).returncode == 0
        assert enters(workflow, target) == 0
    assert tick(workflow, 300).returncode == 0
    assert enters(workflow, target) == 1
    assert CONTINUE in workflow.calls(f"tmux {target} send-keys")[0]
    assert (notes / f"{name}.capacity").read_text().split()[0] == "1"
    for start, wait, expected in ((301, 900, 2), (1202, 1800, 3), (3003, 1800, 4)):
        workflow.screen(f"• Completed another attempt at {start}\n" + STOPPED, target)
        assert tick(workflow, start).returncode == 0
        assert tick(workflow, start + wait - 1).returncode == 0
        assert enters(workflow, target) == expected - 1
        assert tick(workflow, start + wait).returncode == 0
        assert enters(workflow, target) == expected
        assert tick(workflow, start + wait).returncode == 0
        assert enters(workflow, target) == expected
    alerts = "\n".join(workflow.sent())
    assert alerts.count(f"codex at capacity: {name} (15 min)") == 1
    assert alerts.count(f"codex at capacity: {name} (30 min)") == 2
    assert workflow.pushes() == []


def test_held_action_lock_defers_without_consuming_retry(workflow: Workflow):
    name, target, notes = stopped_session(workflow)
    assert tick(workflow, 0).returncode == 0
    lock = workflow.repo / ".local/workflow/start-7.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        done = tick(workflow, 300)
        assert done.returncode == 1
        assert "another action for pull request 7 is under way" in done.stderr
        assert enters(workflow, target) == 0
        assert (notes / f"{name}.capacity").read_text().split()[3] == "0"
    assert tick(workflow, 480).returncode == 0
    assert enters(workflow, target) == 1


def test_two_timer_calls_submit_only_once(workflow: Workflow):
    _, target, _ = stopped_session(workflow)
    assert tick(workflow, 0).returncode == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tick(workflow, 300), range(2)))
    assert all(result.returncode == 0 for result in results)
    assert enters(workflow, target) == 1


@pytest.mark.parametrize("block", ["owner-reply", "owner-blocked", "bridge-draft", "native-draft"])
def test_owner_input_arriving_during_backoff_is_preserved(workflow: Workflow, block: str):
    name, target, notes = stopped_session(workflow)
    assert tick(workflow, 0).returncode == 0
    draft = "Please keep this unsent draft"
    if block == "bridge-draft":
        workflow.screen("────\n❯ " + draft + "\n────\n", name)
    elif block == "native-draft":
        (workflow.state / f"input-{target}").write_text(draft)
    else:
        (notes / f"{name}.{block}").write_text(draft)
    assert tick(workflow, 300).returncode == 0
    assert not workflow.calls(f"tmux {target} send-keys")
    if block == "native-draft":
        assert (workflow.state / f"input-{target}").read_text() == draft


def test_failed_enter_recovers_without_pasting_twice(workflow: Workflow):
    _, target, _ = stopped_session(workflow)
    assert tick(workflow, 0).returncode == 0
    (workflow.state / f"swallow-enter-{target}").write_text("2\n")
    done = tick(workflow, 300)
    assert done.returncode == 1
    assert "after two Enter attempts" in done.stderr
    assert tick(workflow, 480).returncode == 0
    assert sum(" -l " in call for call in workflow.calls(f"tmux {target} send-keys")) == 1
    assert enters(workflow, target) == 3
    assert tick(workflow, 660).returncode == 0
    assert enters(workflow, target) == 3


def test_github_unavailable_does_not_hold_local_retry(workflow: Workflow):
    _, target, _ = stopped_session(workflow)
    (workflow.state / "fail-gh-list").touch()
    tick(workflow, 0)
    tick(workflow, 300)
    actual = enters(workflow, target)
    assert actual == 1, f"GitHub outage blocked local retry; retries = {actual}"


def test_shadow_cannot_consume_a_due_retry(workflow: Workflow):
    name, target, notes = stopped_session(workflow)
    assert tick(workflow, 0).returncode == 0
    receipt = notes / f"{name}.capacity"
    before = receipt.read_bytes()
    assert tick(workflow, 300, "--shadow").returncode == 0
    assert receipt.read_bytes() == before
    assert enters(workflow, target) == 0
    assert tick(workflow, 300).returncode == 0
    assert enters(workflow, target) == 1


def test_real_noninteractive_hunter_is_not_typed_into(workflow: Workflow):
    # The existing hunter launcher uses codex exec, which has no input prompt.
    _, target, _ = stopped_session(workflow, "hunt")
    workflow.screen("Codex\nReading example-app\n" + STOPPED.split("›")[0], target)
    assert tick(workflow, 0).returncode == 0
    assert tick(workflow, 300).returncode == 0
    assert not workflow.calls(f"tmux {target} send-keys")


@pytest.mark.parametrize("kind", ["ticket", "review"])
def test_capacity_callback_leaves_no_quota_or_owner_alarm(workflow: Workflow, kind: str):
    name, _, notes = stopped_session(workflow, kind)
    done = workflow.notify(
        {"hook_event_name": "Stop", "last_assistant_message": STOPPED.splitlines()[0]},
        session=name,
    )
    assert done.returncode == 0
    assert workflow.pushes() == []
    assert not workflow.missed_pushes.exists()
    assert all(not (notes / f"{name}.{suffix}").exists() for suffix in ("asking", "limit", "owner-blocked"))


def test_unreadable_native_capture_is_distinct_from_recovery(workflow: Workflow):
    _, target, _ = stopped_session(workflow)
    # Keep this failure local to the native capture: no network or real tmux is used.
    tmux = workflow.bin / "tmux"
    tmux.write_text(tmux.read_text().replace(
        "  capture-pane)\n",
        '  capture-pane)\n    if [ "$name" = codex-7 ]; then exit 124; fi\n',
    ))
    done = workflow.run("capacity-stop", "ticket-7")
    assert done.returncode == 2
    assert not workflow.calls(f"tmux {target} send-keys")
