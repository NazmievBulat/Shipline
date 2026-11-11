"""PR 103: abrupt interruption, timeout and configured-path probes. Run alone."""

import json
import shlex
from datetime import UTC, datetime

import pytest

from workflow.test_break_103 import NOW, STATUS, keys, no_owner_answer, note, prepare, tick


@pytest.mark.parametrize("failure", ["timeout", "listing-failure"])
def test_an_owned_status_draft_recovers_without_a_github_listing(workflow, failure):
    prepare(workflow)
    (workflow.state / "swallow-enter-codex-7").write_text("2")
    assert tick(workflow).returncode != 0
    if failure == "timeout":
        (workflow.bin / "gh").rename(workflow.bin / "gh.saved")
        workflow.fake("gh", '#!/bin/bash\nif [[ "$1 $2" == "pr list" ]]; then exit 124; fi\nexec "$0.saved" "$@"\n')
    else:
        (workflow.state / "fail-gh-list").touch()
    tick(workflow, NOW + 180)
    assert keys(workflow) == [STATUS, "Enter", "Enter", "Enter"]
    no_owner_answer(workflow)


def test_killed_sender_recovers_even_without_restored_wait_notes(workflow):
    prepare(workflow)
    # Kill the sending shell after its paste, so its EXIT trap cannot restore the wait.
    hook = workflow.state / "on-sleep"
    typed = shlex.quote(str(workflow.state / "input-codex-7"))
    hook.write_text(f'if [[ -s {typed} ]]; then kill -KILL "$PPID"; fi\n')
    done = workflow.run("status-check", "ticket-7")
    assert done.returncode != 0
    assert keys(workflow) == [STATUS]
    assert not note(workflow, "job-wait").exists()
    hook.unlink()
    tick(workflow, NOW + 180)
    assert keys(workflow) == [STATUS, "Enter"]
    no_owner_answer(workflow)


def test_missing_explicit_configuration_leaves_the_wait_and_input_untouched(workflow):
    prepare(workflow)
    config = workflow.root / "missing-config.json"
    done = workflow.run("status-check", "ticket-7", env={"SHIPLINE_CONFIG": str(config)})
    assert done.returncode != 0
    assert keys(workflow) == []
    assert note(workflow, "job-wait").exists()
    assert tick(workflow, NOW + 180).returncode == 0
    assert keys(workflow) == [STATUS, "Enter"]


def test_configured_state_path_is_literal_and_does_not_execute_shell_text(workflow):
    prepare(workflow)
    folder = ".local/example state/$(touch injected)"
    config = workflow.repo / "shipline.json"
    data = json.loads(config.read_text())
    data["state_folder"] = folder
    config.write_text(json.dumps(data))
    old_main = workflow.repo / ".local/workflow"
    if old_main.exists():
        target = workflow.repo / folder
        target.parent.mkdir(parents=True, exist_ok=True)
        old_main.rename(target)
    target = workflow.worktree(7) / folder
    target.parent.mkdir(parents=True, exist_ok=True)
    workflow.notes(7).rename(target)
    # Use the session command so unrelated fixture paths do not enter this probe.
    done = workflow.run("status-check", "ticket-7")
    assert done.returncode == 0
    assert keys(workflow) == [STATUS, "Enter"]
    assert not (workflow.repo / "injected").exists()
    assert not (workflow.worktree(7) / "injected").exists()
    assert not (target / "ticket-7.owner-reply").exists()
    assert not (target / "ticket-7.codex-answered").exists()


@pytest.mark.parametrize("wait_age", [2999, 3000])
def test_wake_reports_the_ended_native_wait_as_idle(workflow, wait_age):
    prepare(workflow, age=wait_age)
    workflow.orchestrator("idle")
    done = workflow.run_script("wake", "ticket-7", env={"WORKFLOW_NOW": str(NOW), "TZ": "UTC"})
    assert done.returncode == 0
    assert "Sessions: ticket-7 idle." in workflow.sent()[-1]
    assert keys(workflow) == []


@pytest.mark.parametrize("exhausted", [False, True])
def test_quota_reset_does_not_reset_the_status_retry_budget(workflow, exhausted):
    prepare(workflow)
    typed = workflow.notes(7) / "codex-7.typed"
    typed.write_text(STATUS)
    (workflow.state / "input-codex-7").write_text(STATUS)
    if exhausted:
        (workflow.state / "swallow-enter-codex-7").write_text("100")
        for i in range(3):
            assert tick(workflow, NOW + i * 180).returncode != 0
    workflow.screen("■ You've hit your usage limit. Try again at 2027-01-16 08:00 UTC.\n› \n", "codex-7")
    reset = int(datetime(2027, 1, 16, 8, tzinfo=UTC).timestamp())
    before = ["Enter"] * 3 if exhausted else []
    tick(workflow, reset - 60)
    assert keys(workflow) == before
    tick(workflow, reset)
    tick(workflow, reset + 180)
    assert keys(workflow) == (["Enter"] * 3 if exhausted else ["Enter"])
    assert typed.exists() == exhausted
    no_owner_answer(workflow)
