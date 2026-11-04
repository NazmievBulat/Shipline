"""Capacity delivery while GitHub observations are incomplete or arrive late."""

import json
import shlex

import pytest
from break_77_support import enters, stopped_session, tick
from workflow_harness import Workflow


@pytest.mark.parametrize("status", ["lost", "partial-rerun", "late-success"])
def test_check_rollup_changes_do_not_duplicate_or_postpone_capacity_delivery(
    workflow: Workflow, status: str
):
    name, target, notes = stopped_session(workflow, "review")
    (notes / f"{name}.started").touch()
    # The bridge is relaying the capacity stop, rather than waiting for checks;
    # native retry and the check-observation pass must not send competing prompts.
    workflow.screen("● Selected model is at capacity.\n────\n❯ \n────\n", name)
    rollup = workflow.state / "rollup-7.json"
    running = {
        "__typename": "CheckRun", "name": "CI result", "status": "IN_PROGRESS",
        "conclusion": None, "startedAt": "2026-10-01T10:00:00Z",
        "detailsUrl": "https://github.com/example-org/example-app/actions/runs/20/job/27",
    }
    rollup.write_text(json.dumps([running]))
    assert tick(workflow, 0).returncode == 0
    if status == "partial-rerun":
        checks = [{"__typename": "CheckRun", "name": "CI: server tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    elif status == "late-success":
        checks = [{**running, "status": "COMPLETED", "conclusion": "SUCCESS"}]
    else:
        checks = [running]
        gh = workflow.bin / "gh"
        completed = json.dumps({
            "id": 20, "status": "completed", "conclusion": "success",
            "updated_at": "2026-10-01T10:05:00Z",
        })
        handler = (
            'if [[ "$1 $2" == "api repos/example-org/example-app/actions/runs/20" ]]; then\n'
            f"  printf '%s\\n' {shlex.quote(completed)}; exit 0\nfi\n"
        )
        gh.write_text(gh.read_text().replace("#!/bin/bash\n", "#!/bin/bash\n" + handler, 1))
    rollup.write_text(json.dumps(checks))
    assert tick(workflow, 300).returncode == 0
    assert enters(workflow, target) == 1
    assert tick(workflow, 480).returncode == 0
    assert enters(workflow, target) == 1
    assert not workflow.calls(f"tmux {name} send-keys")


def test_github_timeout_response_still_runs_local_capacity_duties(workflow: Workflow):
    _, target, _ = stopped_session(workflow)
    gh = workflow.bin / "gh"
    source = gh.read_text()
    # Model the bounded GitHub command returning its timeout status.
    condition = '  [ ! -e "' + str(workflow.state / "fail-gh-list") + '" ] || exit 1'
    assert condition in source
    gh.write_text(source.replace(condition, condition[:-1] + "124"))
    (workflow.state / "fail-gh-list").touch()
    tick(workflow, 0)
    tick(workflow, 300)
    actual = enters(workflow, target)
    assert actual == 1, f"GitHub timeout blocked local retry; retries = {actual}"


def test_custom_note_folder_and_missing_receipt_start_a_fresh_wait(workflow: Workflow):
    name, target, notes = stopped_session(workflow)
    config = workflow.repo / "shipline.json"
    settings = json.loads(config.read_text())
    settings["state_folder"] = ".local/example state"
    config.write_text(json.dumps(settings))
    custom = workflow.worktree(7) / settings["state_folder"]
    custom.mkdir(parents=True)
    (notes / f"{name}.codex").rename(custom / f"{name}.codex")
    assert not (custom / f"{name}.capacity").exists()
    assert tick(workflow, 0).returncode == 0
    assert tick(workflow, 299).returncode == 0
    assert enters(workflow, target) == 0
    assert tick(workflow, 300).returncode == 0
    assert enters(workflow, target) == 1
    assert (custom / f"{name}.capacity").read_text().split()[3] == "1"


@pytest.mark.parametrize("contents", ["", "incomplete\n", "3 not-a-time - 1 0\n"])
def test_interrupted_receipt_write_does_not_type_immediately(workflow: Workflow, contents: str):
    name, target, notes = stopped_session(workflow)
    (notes / f"{name}.capacity").write_text(contents)
    assert tick(workflow, 0).returncode == 0
    assert enters(workflow, target) == 0
    assert tick(workflow, 299).returncode == 0
    assert enters(workflow, target) == 0
    assert tick(workflow, 300).returncode == 0
    assert enters(workflow, target) == 1
