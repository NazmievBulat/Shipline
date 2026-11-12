"""Independent replay and failure tests for saved-question recovery.

External services are stand-ins; the workflow scripts are real.
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
import workflow_harness as harness


NOW = 1_790_000_000
SAVED_AT = NOW - 26 * 3600
QUESTION = (
    "The example-app setting needs approval.\n\n"
    "Owner only: yes\n"
    'Reply "1" to keep the recommended setting.\n\n'
    "Waiting for approval before continuing."
)


@pytest.fixture
def replay(tmp_path, programs_folder, monkeypatch):
    # Use the checkout's commit identity for throwaway Git commits too. Suppress
    # the harness's synthetic session URL; no session URL is needed in this replay.
    real_run = subprocess.run
    author = real_run(
        ["git", "log", "-1", "--format=%an%x00%ae"],
        text=True, capture_output=True, check=True,
    ).stdout.strip().split("\0")
    identity = dict(zip(("name", "email"), author))

    def run(args, *positional, **kwargs):
        if args and args[0] == "git" and kwargs.get("env") is not None:
            env = dict(kwargs["env"])
            for role in ("AUTHOR", "COMMITTER"):
                env[f"GIT_{role}_NAME"] = identity["name"]
                env[f"GIT_{role}_EMAIL"] = identity["email"]
            kwargs["env"] = env
        return real_run(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(harness, "SESSION_LINK", "")
    made = harness.make_workflow(tmp_path, programs_folder)
    made.orchestrator("busy")
    keys = made.root / "home/agents/.env"
    keys.parent.mkdir()
    keys.write_text("PUSHOVER_USER=exampleUser\nPUSHOVER_TOKEN=exampleToken\n")
    return made


def seed(replay, name="ticket-7", *, codex=False, message=QUESTION):
    replay.add_worktree(7, draft=name != "review-7")
    replay.running(name)
    replay.screen("● Waiting for approval.\n> \n", name)
    notes = replay.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / f"{name}.idle").touch()
    saved = notes / f"{name}.stop-message"
    if codex:
        (notes / f"{name}.codex").touch()
        replay.running("codex-7")
        replay.screen("• Waiting for approval.\n› \n", "codex-7")
        saved = notes / f"{name}.codex-turn.json"
        saved.write_text(json.dumps({"last-assistant-message": message, "turn-id": "example-turn"}))
    else:
        saved.write_text(message + "\n")
    os.utime(saved, (SAVED_AT, SAVED_AT))
    return notes, saved


def tick(replay, at=NOW):
    return replay.run_script("tick", env={"WORKFLOW_NOW": str(at)})


def recheck(replay, **extra):
    return replay.run_script(
        "notify", "recheck", env={"WORKFLOW_SESSION": "ticket-7", **extra}
    )


@pytest.mark.parametrize("name,codex", [("ticket-7", False), ("review-7", False), ("ticket-7", True)])
def test_old_question_rings_once_and_preserves_full_wait(replay, name, codex):
    notes, _ = seed(replay, name, codex=codex)
    for at in (NOW, NOW + 180):
        done = tick(replay, at)
        assert done.returncode == 0, done.stderr
    assert (notes / f"{name}.asking").read_text().split()[0] == str(SAVED_AT)
    assert [push.message for push in replay.pushes()] == [QUESTION[-900:] if codex else QUESTION[:300]]
    assert f"asking the owner: {name} (1560 min)" in replay.wake_log.read_text()
    if name == "review-7":
        assert (notes / f"{name}.owner-blocked").exists()


@pytest.mark.parametrize("case", ["progress", "finished", "busy", "limit", "asking", "missing", "answered", "followed-up"])
def test_unrelated_or_ineligible_turns_stay_silent(replay, case):
    notes, saved = seed(replay, codex=case in ("answered", "followed-up"))
    if case == "progress":
        saved.write_text("The example-app checks passed. Continuing next.\n")
    elif case == "finished":
        saved.write_text(QUESTION + "\n\nWorker finished.\n")
    elif case == "busy":
        (notes / "ticket-7.idle").unlink()
    elif case == "limit":
        (notes / "ticket-7.limit").write_text(str(NOW + 3600))
    elif case == "asking":
        (notes / "ticket-7.asking").write_text(f"{SAVED_AT} existing\n")
    elif case == "missing":
        saved.unlink()
    else:
        (notes / f"ticket-7.codex-{case}").write_bytes(saved.read_bytes())
    done = tick(replay)
    assert done.returncode == 0, done.stderr
    assert replay.pushes() == []
    if case != "asking":
        assert not (notes / "ticket-7.asking").exists()


def test_detector_upgrade_revisits_only_once_per_function(replay):
    notes, saved = seed(replay)
    notify = replay.script.with_name("notify")
    original = notify.read_text()
    counted = original.replace("asks_owner() {\n", 'asks_owner() {\n  echo tested >> "$notes/$name.break-tests"\n')
    notify.write_text(counted.replace("asks_owner() {\n", "asks_owner() {\n  return 1\n"))
    # A stop on the older installation leaves a real saved message and no question.
    replay.notify({"hook_event_name": "Stop", "last_assistant_message": QUESTION})
    assert not (notes / "ticket-7.asking").exists()
    os.utime(saved, (SAVED_AT, SAVED_AT))
    notify.write_text(counted)
    for _ in range(2):
        assert tick(replay).returncode == 0
    assert (notes / "ticket-7.break-tests").read_text().splitlines() == ["tested"]
    assert len(replay.pushes()) == 1
    assert (notes / "ticket-7.asking").read_text().split()[0] == str(SAVED_AT)


def test_recovery_runs_even_when_github_listing_is_unavailable(replay):
    notes, _ = seed(replay)
    (replay.state / "fail-gh-list").touch()
    tick(replay)
    assert (notes / "ticket-7.asking").exists(), "GitHub listing failure skipped local question recovery"
    assert len(replay.pushes()) == 1


def test_missing_and_late_check_results_do_not_repeat_a_ring(replay):
    notes, _ = seed(replay)
    (replay.state / "fail-gh-checks").touch()
    tick(replay)
    (replay.state / "fail-gh-checks").unlink()
    (replay.state / "rollup-7.json").write_text("[]")
    tick(replay, NOW + 180)
    assert len(replay.pushes()) == 1
    assert (notes / "ticket-7.asking").exists()


def test_two_rechecks_and_repeated_stop_share_one_receipt(replay):
    notes, _ = seed(replay)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(recheck, replay) for _ in range(2)]
        assert all(call.result(timeout=30).returncode == 0 for call in calls)
    asking = (notes / "ticket-7.asking").read_text()
    for _ in range(2):
        replay.notify({"hook_event_name": "Stop", "last_assistant_message": QUESTION})
    assert len(replay.pushes()) == 1
    assert (notes / "ticket-7.asking").read_text() == asking


def test_long_held_lock_defers_a_real_stop_to_next_tick(replay):
    notes, saved = seed(replay)
    saved.unlink()
    (notes / "ticket-7.idle").unlink()
    started = time.monotonic()
    with (notes / "ticket-7.notify.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        replay.notify(
            {"hook_event_name": "Stop", "last_assistant_message": QUESTION, "unneeded": "discard"},
            env={"WORKFLOW_NOW": str(SAVED_AT)},
        )
        assert time.monotonic() - started < 4
        pending = notes / "ticket-7.stop-pending.json"
        assert pending.stat().st_mode & 0o077 == 0
        assert set(json.loads(pending.read_text())["event"]) == {"hook_event_name", "last_assistant_message"}
        assert recheck(replay).returncode == 0
        assert replay.pushes() == []
    assert tick(replay).returncode == 0
    assert not pending.exists()
    assert saved.stat().st_mode & 0o077 == 0
    assert (notes / "ticket-7.question-tested").stat().st_mode & 0o077 == 0
    assert (notes / "ticket-7.asking").read_text().split()[0] == str(SAVED_AT)
    assert len(replay.pushes()) == 1


def test_partial_recovery_recreates_metadata_without_another_ring(replay):
    notes, _ = seed(replay)
    assert recheck(replay).returncode == 0
    (notes / "ticket-7.asking").unlink()
    (notes / "ticket-7.question").unlink()
    assert recheck(replay).returncode == 0
    assert (notes / "ticket-7.asking").exists()
    assert json.loads((notes / "ticket-7.question").read_text())["owner_only"] == "yes"
    assert len(replay.pushes()) == 1


@pytest.mark.parametrize("failure", ["rejected", "timeout", "missing-keys"])
@pytest.mark.parametrize("name,codex", [("ticket-7", False), ("review-7", False), ("ticket-7", True)])
def test_tick_retries_an_undelivered_recovered_question(replay, failure, name, codex):
    notes, _ = seed(replay, name, codex=codex)
    original_curl = (replay.bin / "curl").read_text()
    keys = replay.root / "home/agents/.env"
    key_values = keys.read_text()
    if failure == "rejected":
        (replay.state / "pushover-fails").touch()
    elif failure == "timeout":
        replay.fake("curl", "#!/bin/bash\nexec /bin/sleep 10\n")
    else:
        keys.unlink()
    done = replay.run_script("tick", env={"WORKFLOW_NOW": str(NOW), "WORKFLOW_NOTIFY_SECONDS": "0.2"})
    assert done.returncode == 0, done.stderr
    asking = (notes / f"{name}.asking").read_text()
    assert asking.split()[0] == str(SAVED_AT)
    assert not (notes / f"{name}.pushed").exists()
    failed_attempts = len(replay.pushes())
    # Service/configuration recovers; no new model turn or Stop will arrive.
    replay.fake("curl", original_curl)
    (replay.state / "pushover-fails").unlink(missing_ok=True)
    keys.write_text(key_values)
    for at in (NOW + 180, NOW + 360):
        assert tick(replay, at).returncode == 0
    assert (notes / f"{name}.pushed").exists(), "recovered question never delivered after temporary push failure"
    assert (notes / f"{name}.asking").read_text() == asking
    assert len(replay.pushes()) == failed_attempts + 1


def test_shell_syntax_in_saved_words_is_only_data(replay):
    marker = replay.state / "unexpected-execution"
    message = QUESTION + f'\n\n$(touch {shlex.quote(str(marker))}) `touch {shlex.quote(str(marker))}`'
    seed(replay, message=message)
    assert recheck(replay).returncode == 0
    assert not marker.exists()
    assert replay.pushes()[0].message == message[:300]


def test_negative_classification_is_cached_until_function_changes(replay):
    progress = "The example-app setting is pending."
    notes, _ = seed(replay, message=progress)
    notify = replay.script.with_name("notify")
    counted = notify.read_text().replace(
        "asks_owner() {\n", 'asks_owner() {\n  echo tested >> "$notes/$name.break-tests"\n'
    )
    notify.write_text(counted)
    for _ in range(2):
        assert recheck(replay).returncode == 0
    assert (notes / "ticket-7.break-tests").read_text().splitlines() == ["tested"]
    assert replay.pushes() == []
    notify.write_text(counted + "\n# Unrelated script edit.\n")
    assert recheck(replay).returncode == 0
    assert (notes / "ticket-7.break-tests").read_text().splitlines() == ["tested"]
    notify.write_text(counted.replace(
        "asks_owner() {\n", f'asks_owner() {{\n  if [[ $1 == {shlex.quote(progress)} ]]; then\n'
        '    echo tested >> "$notes/$name.break-tests"; return 0\n  fi\n'
    ))
    assert recheck(replay).returncode == 0
    assert (notes / "ticket-7.break-tests").read_text().splitlines() == ["tested", "tested"]
    assert len(replay.pushes()) == 1


def test_shadow_tick_does_not_recover_or_send(replay):
    notes, saved = seed(replay)
    snapshot = saved.read_bytes()
    done = replay.run_script("tick", "--shadow", env={"WORKFLOW_NOW": str(NOW)})
    assert done.returncode == 0, done.stderr
    assert saved.read_bytes() == snapshot
    for suffix in ("asking", "pushed", "question", "question-tested", "notify.lock"):
        assert not (notes / f"ticket-7.{suffix}").exists()
    assert replay.pushes() == []


@pytest.mark.parametrize("codex", [False, True])
def test_failed_delivery_survives_a_detector_update_without_reclassification(replay, codex):
    notes, _ = seed(replay, codex=codex)
    (replay.state / "pushover-fails").touch()
    assert recheck(replay).returncode == 0
    asking = (notes / "ticket-7.asking").read_text()
    receipt = (notes / "ticket-7.question-tested").read_text()
    assert not (notes / "ticket-7.pushed").exists()
    notify = replay.script.with_name("notify")
    notify.write_text(notify.read_text().replace(
        "asks_owner() {\n", 'asks_owner() {\n  echo tested >> "$notes/$name.retry-tests"; return 1\n'
    ))
    (replay.state / "pushover-fails").unlink()
    for _ in range(2):
        assert recheck(replay).returncode == 0
    assert (notes / "ticket-7.pushed").exists()
    assert (notes / "ticket-7.asking").read_text() == asking
    assert (notes / "ticket-7.question-tested").read_text() == receipt
    assert not (notes / "ticket-7.retry-tests").exists()
    assert len(replay.pushes()) == 2


@pytest.mark.parametrize("case", ["prompt", "replacement", "answered", "followed-up", "running", "limit"])
def test_failed_delivery_is_not_retried_for_an_obsolete_or_ineligible_turn(replay, case):
    notes, saved = seed(replay, codex=True)
    (replay.state / "pushover-fails").touch()
    assert recheck(replay).returncode == 0
    assert not (notes / "ticket-7.pushed").exists()
    (replay.state / "pushover-fails").unlink()
    if case == "prompt":
        replay.notify({"hook_event_name": "UserPromptSubmit", "prompt": "Use 1."})
    elif case == "replacement":
        saved.write_text(json.dumps({"last-assistant-message": QUESTION, "turn-id": "later-turn"}))
    elif case in ("answered", "followed-up"):
        (notes / f"ticket-7.codex-{case}").write_bytes(saved.read_bytes())
    elif case == "running":
        (notes / "ticket-7.idle").unlink()
    else:
        (notes / "ticket-7.limit").write_text(str(NOW + 3600))
    for _ in range(2):
        assert recheck(replay).returncode == 0
    assert not (notes / "ticket-7.pushed").exists()
    assert len(replay.pushes()) == 1
