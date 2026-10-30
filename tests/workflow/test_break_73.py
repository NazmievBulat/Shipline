"""Independent command-wait and merge-handoff probes for pull request 73.

Run this file alone. External services use the repository's neutral harness.
"""

import fcntl
import json
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from workflow.merge_gate_fixtures import GitHub, _REPO, _SUITE_RUNS, _check_runs, _ci_runs
from workflow.merge_gate_fixtures import github as github_fixture
from workflow.test_merge_driver import handed as handed_fixture
from workflow_harness import Workflow

github = github_fixture
handed = handed_fixture


def configure(workflow: Workflow, entry: dict | str | None) -> None:
    workflow.git("update-index", "--skip-worktree", "shipline.json")
    path = workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    commands = config.setdefault("commands", {})
    if entry is None:
        commands.pop("pre_train_checks", None)
    else:
        commands["pre_train_checks"] = entry
    path.write_text(json.dumps(config))


def check_command(workflow: Workflow, code: int, *, progress: str = "", stderr: bool = True,
                  diagnostic: str = "checks temporarily unavailable: deadline") -> None:
    workflow.fake(
        "example-checks",
        "#!/bin/bash\n"
        + f"printf '%s' {shlex.quote(progress)}\n"
        + f"printf '%s\\n' {shlex.quote(diagnostic)} {'>&2' if stderr else ''}\n"
        + f"exit {code}\n",
    )


def configured(workflow: Workflow) -> None:
    configure(workflow, {"run": "example-checks", "form": "exit",
                         "wait_output": "^checks temporarily unavailable"})


def folder(workflow: Workflow) -> Path:
    return workflow.repo / ".local/workflow"


def reviewers(workflow: Workflow) -> list[str]:
    return [call for call in workflow.calls("tmux") if " new-session " in call]


def pending(workflow: Workflow) -> None:
    assert (folder(workflow) / "merge-wanted-7").exists()
    train = json.loads((folder(workflow) / "merge-train.json").read_text())
    assert "7" in train["waiting"]
    assert "7" not in train["approved"]
    assert not reviewers(workflow)
    assert not (workflow.state / "merged").exists()


@pytest.mark.parametrize("progress,stderr", [
    ("", False), ("selection complete\n", True), ("checking selection...", True),
], ids=["stdout", "stderr-after-line", "stderr-after-progress"])
def test_deadline_then_pass_retries_without_another_review(
    handed: GitHub, progress: str, stderr: bool,
) -> None:
    workflow = handed.workflow
    configured(workflow)
    check_command(workflow, 1, progress=progress, stderr=stderr)
    before = (folder(workflow) / "merge-wanted-7").read_bytes()

    first = workflow.run_script("tick")

    assert "merge hand-back" not in first.stdout, first.stdout + first.stderr
    pending(workflow)
    assert (folder(workflow) / "merge-wanted-7").read_bytes() == before
    assert first.stdout.count("pre-train checks of 7 wait:") == 1
    record = folder(workflow) / "pre-train-wait-7.json"
    assert record.stat().st_mode & 0o077 == 0
    check_command(workflow, 0, diagnostic="checks passed")

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert not reviewers(workflow)
    assert not record.exists()


@pytest.mark.parametrize("second_code", [1, 75])
def test_two_waits_name_both_attempts_and_revoke_the_handoff(handed: GitHub, second_code: int) -> None:
    workflow = handed.workflow
    configured(workflow)
    check_command(workflow, 1, diagnostic="checks temporarily unavailable: attempt one")
    first = workflow.run_script("tick")
    assert "merge hand-back" not in first.stdout, first.stdout + first.stderr
    pending(workflow)
    check_command(workflow, second_code, diagnostic="checks temporarily unavailable: attempt two")

    second = workflow.run_script("tick")

    assert "merge hand-back 7" in second.stdout, second.stdout + second.stderr
    assert "attempt one" in second.stdout and "attempt two" in second.stdout
    assert not (folder(workflow) / "merge-wanted-7").exists()
    assert not (workflow.state / "merged").exists()
    assert len(reviewers(workflow)) == 1


@pytest.mark.parametrize("entry", ["example-checks", {"run": "example-checks", "form": "exit"}])
def test_default_temporary_exit_needs_no_wait_setting(handed: GitHub, entry: dict | str) -> None:
    workflow = handed.workflow
    configure(workflow, entry)
    check_command(workflow, 75, diagnostic="tool temporarily unavailable")
    tick = workflow.run_script("tick")
    assert "merge hand-back" not in tick.stdout, tick.stdout + tick.stderr
    pending(workflow)


@pytest.mark.parametrize("after_wait", [False, True])
def test_real_failure_is_immediate_even_after_a_wait(handed: GitHub, after_wait: bool) -> None:
    workflow = handed.workflow
    configured(workflow)
    if after_wait:
        check_command(workflow, 1)
        workflow.run_script("tick")
        pending(workflow)
    check_command(workflow, 1, diagnostic="inventory assertion failed")
    tick = workflow.run_script("tick")
    assert "merge hand-back 7" in tick.stdout, tick.stdout + tick.stderr
    assert "pre-train checks failed" in tick.stdout
    assert not (folder(workflow) / "merge-wanted-7").exists()
    assert len(reviewers(workflow)) == 1


def test_missing_setting_runs_no_extra_checks(handed: GitHub) -> None:
    workflow = handed.workflow
    configure(workflow, None)
    tick = workflow.run_script("tick")
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert not reviewers(workflow)


def test_named_missing_program_cannot_merge(handed: GitHub) -> None:
    workflow = handed.workflow
    configure(workflow, {"run": "example-missing-checks", "form": "exit",
                         "wait_output": "^checks temporarily unavailable"})
    tick = workflow.run_script("tick")
    assert "merge hand-back 7" in tick.stdout, tick.stdout + tick.stderr
    assert not (workflow.state / "merged").exists()
    assert not (folder(workflow) / "merge-wanted-7").exists()


def test_invalid_pattern_stops_before_running_checks(handed: GitHub) -> None:
    workflow = handed.workflow
    configure(workflow, {"run": "example-checks", "form": "exit", "wait_output": "["})
    workflow.fake("example-checks", '#!/bin/bash\ntouch "@STATE@/unexpected-check"\n')
    gate = workflow.run_script("merge-when-current", "--handoff", "7", cwd=workflow.worktree(7))
    assert gate.returncode != 0
    assert "commands.pre_train_checks.wait_output" in gate.stdout + gate.stderr
    assert not (workflow.state / "unexpected-check").exists()


def test_reviewer_handoff_records_wait_without_refusing(handed: GitHub) -> None:
    workflow = handed.workflow
    (folder(workflow) / "merge-wanted-7").unlink()
    configured(workflow)
    check_command(workflow, 1)
    handoff = workflow.run_script("merge-when-current", "--handoff", "7", cwd=workflow.worktree(7))
    assert handoff.returncode == 0, handoff.stdout + handoff.stderr
    assert "waiting for pre-train checks" in handoff.stdout
    pending(workflow)
    check_command(workflow, 0)
    tick = workflow.run_script("tick")
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert (workflow.state / "merged").read_text() == "7"


def test_busy_merge_lock_does_not_consume_a_wait_attempt(handed: GitHub) -> None:
    workflow = handed.workflow
    configured(workflow)
    workflow.fake("example-checks", '#!/bin/bash\necho called >> "@STATE@/checks-called"\n'
                  'echo "checks temporarily unavailable"\nexit 1\n')
    with (folder(workflow) / "merge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(
            ["bash", str(workflow.script.parent / "merge-when-current"), "--drive", "7"],
            cwd=workflow.worktree(7), env=workflow.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            time.sleep(0.3)
            assert process.poll() is None
            assert not (workflow.state / "checks-called").exists()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 3, stdout + stderr
    assert (workflow.state / "checks-called").read_text().splitlines() == ["called"]
    pending(workflow)


def test_two_simultaneous_handoffs_cannot_leave_approval_after_two_waits(handed: GitHub) -> None:
    workflow = handed.workflow
    configured(workflow)
    workflow.fake("example-checks", '#!/bin/bash\n/bin/sleep 0.2\n'
                  'echo "checks temporarily unavailable"\nexit 1\n')
    processes = []
    try:
        for _ in range(2):
            processes.append(subprocess.Popen(
                ["bash", str(workflow.script.parent / "merge-when-current"), "--handoff", "7"],
                cwd=workflow.worktree(7), env=workflow.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        outputs = [process.communicate(timeout=30) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    combined = "".join(out + err for out, err in outputs)
    assert any(process.returncode == 1 for process in processes), combined
    assert "could not finish twice" in combined
    assert not (folder(workflow) / "merge-wanted-7").exists()
    train = json.loads((folder(workflow) / "merge-train.json").read_text())
    assert "7" not in train["approved"]
    assert not (workflow.state / "merged").exists()


def gate_probe(workflow: Workflow, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-B", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g\n" + source,
         str(workflow.script.parent)],
        cwd=workflow.worktree(7), env={**workflow.env, "SHIPLINE_REPO": str(workflow.repo)},
        input="", capture_output=True, text=True, timeout=30, check=False,
    )


def test_new_head_has_its_own_wait_budget(handed: GitHub) -> None:
    workflow = handed.workflow
    configured(workflow)
    check_command(workflow, 1)
    first = workflow.run_script("tick")
    assert "merge hand-back" not in first.stdout, first.stdout + first.stderr
    pending(workflow)
    workflow.git("commit", "--allow-empty", "-m", "test: example head changes", cwd=workflow.worktree(7))
    new_head = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))

    retry = gate_probe(workflow, 'g.pre_train_checks("7", g.CONFIG.state(), locked=False)\n')

    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert "pre-train checks of 7 wait:" in retry.stdout
    record = json.loads((folder(workflow) / "pre-train-wait-7.json").read_text())
    assert record["head"] == new_head
    assert "second" not in record


def test_adapter_timeout_stops_children_and_releases_the_lock(handed: GitHub) -> None:
    workflow = handed.workflow
    configured(workflow)
    workflow.fake("example-checks", '#!/bin/bash\n'
                  '( /bin/sleep 0.8; touch "@STATE@/escaped-child" ) &\nwait\n')

    result = gate_probe(workflow, '''g.COMMAND_TIMEOUT = 0.1
try:
    with g.handoff_lock(g.CONFIG.state(), "7"):
        g.repository_command("pre_train_checks")
except g.TimedOut:
    print("bounded timeout")
''')

    assert result.returncode == 0, result.stdout + result.stderr
    assert "bounded timeout" in result.stdout
    time.sleep(1)
    assert not (workflow.state / "escaped-child").exists()
    with (folder(workflow) / "merge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_command_arguments_remain_literal_with_wait_options(handed: GitHub) -> None:
    workflow = handed.workflow
    configure(workflow, {"run": "example-checks", "form": "exit",
                         "args": ["$(touch injected-example)", "; exit 0", "two words"],
                         "wait_output": "^checks temporarily unavailable"})
    workflow.fake("example-checks", '#!/bin/bash\nprintf "%s\\n" "$@" > "@STATE@/literal-args"\n'
                  'echo "checks temporarily unavailable"\nexit 1\n')

    tick = workflow.run_script("tick")

    assert "merge hand-back" not in tick.stdout, tick.stdout + tick.stderr
    pending(workflow)
    assert (workflow.state / "literal-args").read_text().splitlines() == [
        "$(touch injected-example)", "; exit 0", "two words",
    ]
    assert not (workflow.worktree(7) / "injected-example").exists()


def test_timeout_retry_with_a_lost_github_check_preserves_authorization(handed: GitHub) -> None:
    workflow = handed.workflow
    configured(workflow)
    check_command(workflow, 1)
    workflow.run_script("tick")
    pending(workflow)
    check_command(workflow, 0)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    handed.api[_ci_runs(handed.head)]["workflow_runs"][0]["updated_at"] = old
    handed.pulls["7"]["statusCheckRollup"].append({
        "__typename": "CheckRun", "name": "CI result", "status": "IN_PROGRESS",
        "conclusion": None, "startedAt": old,
        "detailsUrl": "https://github.com/example-org/example-app/actions/runs/20/job/22",
    })
    handed.save()

    repair = workflow.run_script("tick")

    assert "merge hand-back" not in repair.stdout, repair.stdout + repair.stderr
    assert not reviewers(workflow)
    assert not (workflow.state / "merged").exists()
    repaired = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert repaired != handed.head
    assert json.loads((folder(workflow) / "merge-wanted-7").read_text())["head"] == repaired
    handed.head = repaired
    handed.pulls["7"]["headRefOid"] = repaired
    handed.pulls["7"]["statusCheckRollup"].pop()
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {repaired}\n")
    handed.ci(handed.base, repaired, run_id=50)
    handed.save()

    last = workflow.run_script("tick")

    assert last.returncode == 0, last.stdout + last.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert not reviewers(workflow)


@pytest.mark.parametrize("condition", ["missing", "running", "partial-rerun"])
def test_passing_retry_does_not_merge_before_github_evidence_returns(handed: GitHub, condition: str) -> None:
    workflow = handed.workflow
    configured(workflow)
    check_command(workflow, 1)
    workflow.run_script("tick")
    pending(workflow)
    check_command(workflow, 0)
    key = _ci_runs(handed.head)
    original = json.loads(json.dumps(handed.api))
    if condition == "missing":
        handed.api[key] = {"workflow_runs": []}
        handed.api[_SUITE_RUNS] = {"workflow_runs": []}
        _check_runs(handed, handed.head, ("CI result", "completed"))
    else:
        handed.api[key]["workflow_runs"][0].update(status="in_progress", conclusion=None)
        handed.pulls["7"]["statusCheckRollup"] = [
            {"name": "CI result", "status": "IN_PROGRESS", "conclusion": None},
        ]
        _check_runs(handed, handed.head, ("CI: server tests", "in_progress"), ("CI result", "in_progress"))
        if condition == "partial-rerun":
            handed.api[key]["workflow_runs"][0]["run_attempt"] = 2
            jobs_key = f"{_REPO}/actions/runs/20/jobs?per_page=100"
            handed.api[jobs_key]["jobs"][-1].update(status="in_progress", conclusion=None)
    handed.save()

    tick = workflow.run_script("tick")

    assert "merge hand-back" not in tick.stdout, tick.stdout + tick.stderr
    assert not (workflow.state / "merged").exists()
    assert (folder(workflow) / "merge-wanted-7").exists()
    assert not reviewers(workflow)
    handed.api = original
    handed.pulls["7"]["statusCheckRollup"] = [
        {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    handed.save()
    last = workflow.run_script("tick")
    assert last.returncode == 0, last.stdout + last.stderr
    assert (workflow.state / "merged").read_text() == "7"
