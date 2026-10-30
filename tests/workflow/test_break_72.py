"""Independent hand-back break tests for pull request 72.

Run this file alone. External services are the harness stand-ins; Git is real.
"""

import fcntl
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from workflow.test_merge_driver import handed as handed_fixture
from workflow.test_merge_when_current import GitHub, github as github_fixture

github = github_fixture
handed = handed_fixture


def _folder(case):
    return case.workflow.repo / ".local/workflow"


def _request(case, number=7):
    return _folder(case) / f"merge-review-{number}"


def _starts(case):
    return [line.split()[1] for line in case.workflow.calls("tmux") if " new-session " in line]


def _tick(case):
    done = case.workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _block_next_round(case):
    case.api["repos/example-org/example-app/pulls/7/reviews"] = [
        {"body": f"<!-- workflow-review round={n} result=sent-back -->", "commit_id": case.head}
        for n in (1, 2)
    ]
    case.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    case.save()


@pytest.mark.parametrize("check_state", ["success", "late", "missing", "partial-rerun"])
def test_recovered_or_unread_checks_do_not_repeat_a_saved_failure(handed: GitHub, check_state):
    _block_next_round(handed)
    first = _tick(handed)
    saved = json.loads(_request(handed).read_text())
    assert first.count("merge hand-back 7:") == 1
    assert first.count("hand-back 7 waits: the owner's allowance for round 3") == 1
    checks = handed.pulls["7"]["statusCheckRollup"]
    checks[0]["conclusion"] = "SUCCESS"
    if check_state == "late":
        checks[0].update(status="IN_PROGRESS", conclusion=None)
    elif check_state == "missing":
        checks.clear()
    elif check_state == "partial-rerun":
        checks.append({"__typename": "CheckRun", "name": "CI: part two",
                       "status": "IN_PROGRESS", "conclusion": None})
    handed.save()
    for _ in range(3):
        out = _tick(handed)
        assert "merge hand-back 7:" not in out
        assert "hand-back 7 waits:" not in out
        assert json.loads(_request(handed).read_text()) == saved
    assert _starts(handed) == []
    assert not (handed.workflow.state / "merged").exists()


def test_allowance_memory_load_and_returning_cause_are_reported_once(handed: GitHub):
    _block_next_round(handed)
    _tick(handed)
    flow = handed.workflow
    (_folder(handed) / "review-7.allow-round").write_text("3\n")
    flow.pressure_file.write_text("some avg60=12.00\n")
    flow.fake("sleep", "#!/bin/bash\nexit 98\n")
    for cause, pressure, load in [
        ("memory is short", "12.00", "0.50"),
        ("the processors are overloaded", "0.00", "9.00"),
        ("memory is short", "12.00", "0.50"),
    ]:
        flow.pressure_file.write_text(f"some avg60={pressure}\n")
        flow.loadavg_file.write_text(f"0.50 {load} 0.50 1/100 1000\n")
        assert f"hand-back 7 waits: {cause}\n" in _tick(handed)
        assert "hand-back 7 waits:" not in _tick(handed)
    flow.pressure_file.write_text("some avg60=0.00\n")
    flow.loadavg_file.write_text("0.50 0.50 0.50 1/100 1000\n")
    _tick(handed)
    assert _starts(handed) == ["review-7"]
    assert not _request(handed).exists()


@pytest.mark.parametrize("residue", ["note", "approval-and-repair", "refresh-and-head"])
def test_interrupted_revocation_keeps_event_reason_and_wait(handed: GitHub, residue):
    _block_next_round(handed)
    _tick(handed)
    request = json.loads(_request(handed).read_text())
    folder = _folder(handed)
    note = folder / "merge-wanted-7"
    if residue == "note":
        note.write_text(json.dumps({"head": handed.head, "branch": "ticket/seven", "round": 2}))
    elif residue == "approval-and-repair":
        path = folder / "merge-train.json"
        train = json.loads(path.read_text())
        train["approved"]["7"] = handed.head
        train["repairing"]["7"] = handed.head
        path.write_text(json.dumps(train))
    else:
        (folder / "merge-refresh-7.json").write_text("{}")
        (folder / "review-7.head").write_text(handed.head)
    recovered = _tick(handed)
    assert recovered.count(f"merge hand-back 7: {request['reason']}\n") == 1
    assert "hand-back 7 waits:" not in recovered
    assert json.loads(_request(handed).read_text()) == request
    assert not note.exists()
    train = json.loads((folder / "merge-train.json").read_text())
    assert "7" not in train["approved"] and "7" not in train["repairing"]
    assert not (folder / "merge-refresh-7.json").exists()
    assert not (folder / "review-7.head").exists()
    assert "merge hand-back 7:" not in _tick(handed)


def test_concurrent_ticks_do_not_double_announce_or_double_start(handed: GitHub):
    _block_next_round(handed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: handed.workflow.run_script("tick"), range(2)))
    assert all(done.returncode == 0 for done in results), [d.stderr for d in results]
    out = "".join(done.stdout for done in results)
    assert out.count("merge hand-back 7:") == 1
    assert out.count("hand-back 7 waits:") == 1
    assert _starts(handed) == []


def test_two_wait_reporters_preserve_request_and_emit_one_line(handed: GitHub):
    _block_next_round(handed)
    _tick(handed)
    before = json.loads(_request(handed).read_text())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: handed.workflow.run_python("merge_driver.py", "--wait-hand-back", "7"),
            range(2),
        ))
    assert all(done.returncode == 0 for done in results), [d.stderr for d in results]
    assert sum(d.stdout.count("hand-back 7 waits: its turn in the order of starts")
               for d in results) == 1
    after = json.loads(_request(handed).read_text())
    assert after.pop("wait") == "its turn in the order of starts"
    before.pop("wait")
    assert after == before
    assert _request(handed).stat().st_mode & 0o077 == 0


def _wait_process(case):
    flow = case.workflow
    return subprocess.Popen(
        ["python3", "-B", str(flow.script.parent / "merge_driver.py"), "--wait-hand-back", "7"],
        cwd=flow.repo, env=flow.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )


def test_lock_contention_and_interrupted_wait_leave_retryable_state(handed: GitHub):
    _block_next_round(handed)
    _tick(handed)
    before = _request(handed).read_text()
    with (_folder(handed) / "merge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = _wait_process(handed)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                child.communicate(timeout=0.4)
            assert _request(handed).read_text() == before
        finally:
            child.terminate()
            child.communicate(timeout=5)
    retry = handed.workflow.run_python("merge_driver.py", "--wait-hand-back", "7")
    assert retry.returncode == 0, retry.stderr
    assert retry.stdout.count("hand-back 7 waits:") == 1
    again = handed.workflow.run_python("merge_driver.py", "--wait-hand-back", "7")
    assert again.returncode == 0 and again.stdout == ""


def test_removed_request_is_not_recreated_by_wait_reporter(handed: GitHub):
    _block_next_round(handed)
    _tick(handed)
    _request(handed).unlink()
    done = handed.workflow.run_python("merge_driver.py", "--wait-hand-back", "7")
    assert done.returncode == 0 and done.stdout == "", done.stderr
    assert not _request(handed).exists()


def test_waiting_behind_another_pull_recovers_after_memory_returns(handed: GitHub):
    _block_next_round(handed)
    _tick(handed)
    flow = handed.workflow
    head = flow.add_pull_request(8, draft=False, labels=("workflow", "priority:+high"))
    handed.pulls["8"] = deepcopy(handed.pulls["7"])
    handed.pulls["8"].update(headRefName=flow.branch(8), headRefOid=head,
                             labels=[{"name": "workflow"}, {"name": "priority: high"}])
    handed.api["repos/example-org/example-app/pulls/8/reviews"] = []
    _request(handed, 8).write_text(json.dumps({"event": "b" * 32, "reason": "checks need review"}))
    handed.save()
    flow.pressure_file.write_text("some avg60=12.00\n")
    assert "hand-back 7 waits: its turn in the order of starts\n" in _tick(handed)
    assert "hand-back 7 waits:" not in _tick(handed)
    flow.pressure_file.write_text("some avg60=0.00\n")
    assert "hand-back 7 waits: the owner's allowance for round 3\n" in _tick(handed)
    assert _starts(handed) == ["review-8"]
    assert _request(handed).exists()
    assert "hand-back 7 waits:" not in _tick(handed)


def test_varying_setup_progress_does_not_change_resource_cause(handed: GitHub):
    _block_next_round(handed)
    _tick(handed)
    flow = handed.workflow
    (_folder(handed) / "review-7.allow-round").write_text("3\n")
    flow.pressure_file.write_text("some avg60=12.00\n")
    for n in (1, 2, 3):
        flow.fake("worktree-setup", f'#!/bin/bash\nprintf "progress {n}\\r"\n')
        out = _tick(handed)
        assert ("hand-back 7 waits: memory is short\n" in out) == (n == 1)
        assert "progress" not in json.loads(_request(handed).read_text())["wait"]


def test_wrong_or_empty_allowance_stays_quiet_until_exact_round(handed: GitHub):
    _block_next_round(handed)
    _tick(handed)
    allowance = _folder(handed) / "review-7.allow-round"
    for value in ("", "2\n", "4\n"):
        allowance.write_text(value)
        assert "hand-back 7 waits:" not in _tick(handed)
        assert _starts(handed) == []
    allowance.write_text("3\n")
    _tick(handed)
    assert _starts(handed) == ["review-7"]


def test_transient_setup_failure_is_retried_without_repeating_wait(handed: GitHub):
    _block_next_round(handed)
    _tick(handed)
    (_folder(handed) / "review-7.allow-round").write_text("3\n")
    flow = handed.workflow
    flow.fake("worktree-setup", '#!/bin/bash\necho "setup temporarily unavailable" >&2\nexit 1\n')
    first = _tick(handed)
    assert "hand-back 7 waits: reviewer start refused: setup temporarily unavailable" in first
    second = _tick(handed)
    assert "hand-back 7 waits:" not in second
    assert _request(handed).exists()
    flow.fake("worktree-setup", "#!/bin/bash\nexit 0\n")
    _tick(handed)
    assert _starts(handed) == ["review-7"]
    assert not _request(handed).exists()
