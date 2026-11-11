"""Repairing a failed handoff preserves its place and retries only safe runner failures."""

import json

import pytest
from workflow.test_merge_driver import (
    _COMPOSE_DOWNLOAD,
    _DOWNLOAD_LINE,
    _FAILED_TEST,
    _RUNS,
    _behind,
    _fail,
    _fix_and_hand_off,
    _folder,
    _github_answers_repairs,
    _handbacks,
    _note,
    _pass_fix_and_hand_off,
    _push_fix,
    _repair_sentence,
    _started,
    _train,
    handed,
)
from workflow.test_merge_when_current import GitHub
from workflow.test_merge_when_current import github as github_fixture
from workflow_harness import SCRIPTS, session_arguments

github = github_fixture


def test_a_failing_test_goes_to_a_medium_worker_naming_the_run_and_keeps_the_line_place(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _behind(handed)
    failed = handed.head
    _fail(handed)

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    assert workflow.calls("gh pr ready") == ["gh pr ready 7 -R example-org/example-app --undo"]
    assert not _note(workflow).exists()
    assert _started(workflow) == []
    assert "7" not in _train(workflow)["approved"]
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert _train(workflow)["blocked"]["7"] == failed

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert _started(workflow) == ["ticket-7"]
    assert session_arguments(workflow.state, "ticket-7")[-1] == (
        f'claude --effort medium --remote-control ticket-7 "{_repair_sentence()}"'
    )
    assert _train(workflow)["waiting"] == ["7", "8"]
    workflow.run("stop", "ticket-7")
    _fix_and_hand_off(handed)
    workflow.run("stop", "review-7")

    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["blocked"]
    # While the fix's CI runs, the handoff waits in its earlier place.
    jobs = handed.api["repos/example-org/example-app/actions/runs/50/jobs?per_page=100"]["jobs"]
    jobs[-1].update(status="in_progress", conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    handed.save()

    third = workflow.run_script("tick")

    assert third.returncode == 0, third.stdout + third.stderr
    assert _note(workflow).exists()
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["blocked"]
    jobs[-1].update(status="completed", conclusion="success")
    handed.pulls["7"]["statusCheckRollup"][0].update(status="COMPLETED", conclusion="SUCCESS")
    handed.save()

    third = workflow.run_script("tick")

    assert third.returncode == 0, third.stdout + third.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert _started(workflow) == ["ticket-7"]
    assert not (_folder(workflow) / "check-repair-7.json").exists()

@pytest.mark.parametrize("repair_left", [False, True], ids=["repair removed", "repair left behind"])
def test_a_repaired_fix_tested_on_older_main_keeps_its_place_and_refreshes(
    handed: GitHub, repair_left: bool
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _behind(handed)
    failed = handed.head
    _fail(handed)

    first = workflow.run_script("tick")

    assert first.returncode == 0, first.stdout + first.stderr
    repair = _folder(workflow) / "check-repair-7.json"
    assert json.loads(repair.read_text())["head"] == failed
    assert _train(workflow)["blocked"] == {"7": failed}
    assert _train(workflow)["repairing"] == {"7": failed}
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert not _note(workflow).exists()

    second = workflow.run_script("tick")

    assert second.returncode == 0, second.stdout + second.stderr
    assert _started(workflow) == ["ticket-7"]
    assert _train(workflow)["waiting"] == ["7", "8"]
    workflow.run("stop", "ticket-7")
    old = handed.base
    _push_fix(handed)
    fixed = handed.head
    # CI passed on the fix and the old main, before main moved and the reviewer handed off.
    workflow.git("commit", "--allow-empty", "-m", "fix: main advanced")
    handed.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    handed.main()
    handed.save()
    if not repair_left:
        repair.unlink()

    _pass_fix_and_hand_off(handed)

    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["blocked"]
    assert "7" not in _train(workflow)["repairing"]
    assert _train(workflow)["approved"]["7"] == fixed
    assert json.loads(_note(workflow).read_text())["head"] == fixed
    assert repair.exists() == repair_left
    assert workflow.git("merge-base", fixed, handed.base) == old

    refreshed_tick = workflow.run_script("tick")

    assert refreshed_tick.returncode == 0, refreshed_tick.stdout + refreshed_tick.stderr
    assert "merge hand-back 7" not in refreshed_tick.stdout
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != fixed
    assert workflow.git("show", "-s", "--format=%P", refreshed) == f"{fixed} {handed.base}"
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == refreshed
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    assert _train(workflow)["approved"]["7"] == refreshed
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert "7" not in _train(workflow)["blocked"]
    assert not repair.exists()
    assert not (workflow.state / "merged").exists()
    assert not list(_folder(workflow).glob("merge-review-*"))
    assert _started(workflow) == ["ticket-7"]

    handed.head = refreshed
    handed.pulls["7"]["headRefOid"] = refreshed
    (workflow.state / "pr/7").write_text(f"OPEN ticket/seven {refreshed}\n")
    handed.ci(handed.base, refreshed, run_id=60)
    handed.save()

    merged_tick = workflow.run_script("tick")

    assert merged_tick.returncode == 0, merged_tick.stdout + merged_tick.stderr
    assert "merge hand-back 7" not in merged_tick.stdout
    assert (workflow.state / "merged").read_text() == "7"
    assert _train(workflow)["waiting"] == ["8"]
    assert any("--match-head-commit " + refreshed in call for call in workflow.calls("gh pr merge"))
    assert _started(workflow) == ["ticket-7"]
    assert not list(_folder(workflow).glob("merge-review-*"))

def test_a_repaired_fix_on_older_main_is_still_handed_back_when_its_own_test_fails(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _behind(handed)
    _fail(handed)
    first = workflow.run_script("tick")
    assert first.returncode == 0, first.stdout + first.stderr
    _push_fix(handed)
    workflow.git("commit", "--allow-empty", "-m", "fix: main advanced")
    handed.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    handed.main()
    handed.save()
    _pass_fix_and_hand_off(handed)
    _fail(handed, run_id=50)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert _train(workflow)["blocked"]["7"] == handed.head
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]
    assert "pull request checks failed again after a worker's fix" in done.stdout
    assert not workflow.calls("gh pr merge")
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0] == handed.head

@pytest.mark.parametrize("rerun", ["fails again", "passes"])
@pytest.mark.parametrize(
    "failure",
    [
        "runner shut down", "timed out", "layer download",
        "setup HTTP 500", "setup HTTP 502", "setup HTTP 504", "setup HTTP 599",
    ],
)
def test_a_runner_error_is_rerun_once_then_handed_back_to_a_reviewer(
    handed: GitHub, failure: str, rerun: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    if failure == "timed out":
        _fail(handed, conclusion="timed_out", log=None)
    elif failure == "layer download":
        _fail(handed, log="failed to solve: failed to compute cache key: short read: unexpected EOF\n")
    elif failure.startswith("setup HTTP "):
        status = failure.rsplit(" ", 1)[1]
        _fail(
            handed,
            log=(
                "CI: server tests\tSet up Go\t##[error]Failed to run: Error: "
                f"Unexpected HTTP response: {status}, Error: Unexpected HTTP response: {status}\n"
            ),
        )
    else:
        _fail(handed, log="##[error]The runner has received a shutdown signal.\n")

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        # Re-run once; until GitHub shows the new attempt, the request is not sent again.
        assert workflow.calls("gh run rerun") == [
            "gh run rerun 20 -R example-org/example-app --failed"
        ]
        assert _note(workflow).exists()
        assert _started(workflow) == []

    # GitHub shows the new attempt running: the handoff waits for it.
    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    run.update(run_attempt=2, status="in_progress", conclusion=None)
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    for job in jobs:
        job.update(status="in_progress", conclusion=None)
    handed.pulls["7"]["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion=None)
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _note(workflow).exists()
    assert _started(workflow) == []
    passed = rerun == "passes"
    run.update(status="completed", conclusion="success" if passed else "failure")
    for job in jobs:
        job.update(status="completed", conclusion="success")
    if not passed:
        jobs[-1]["conclusion"] = "timed_out" if failure == "timed out" else "failure"
        jobs[1]["conclusion"] = "failure"
    handed.pulls["7"]["statusCheckRollup"][0].update(
        status="COMPLETED", conclusion="SUCCESS" if passed else "FAILURE"
    )
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh run rerun")) == 1
    assert workflow.calls("gh pr ready") == []
    if passed:
        assert (workflow.state / "merged").read_text() == "7"
        assert _started(workflow) == []
    else:
        assert not _note(workflow).exists()
        assert _started(workflow) == ["review-7"]
        assert "its runners failed it again in run 20" in workflow.wake_log.read_text()
        assert any("failed it again" in reason for reason in _handbacks(workflow))

def test_a_runner_error_on_runners_the_gate_may_not_rerun_on_is_handed_back(
    handed: GitHub,
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(
        handed,
        labels=("ubuntu-latest",),
        log="The hosted runner lost communication with the server.\n",
    )

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == []
    assert _started(workflow) == ["review-7"]
    assert workflow.calls("gh pr ready") == []

@pytest.mark.parametrize("between", ["no tick", "a tick that saw only skipped checks"])
def test_a_second_failing_test_in_a_row_goes_to_a_reviewer_not_another_worker(
    handed: GitHub, between: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    _fix_and_hand_off(handed)
    workflow.run("stop", "review-7")
    if between != "no tick":
        # The fix's checks have not started: only a skipped check from its draft shows.
        handed.pulls["7"]["statusCheckRollup"] = [
            {"name": "Docker images: what to run", "status": "COMPLETED", "conclusion": "SKIPPED"}
        ]
        run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
        run.update(status="queued", conclusion=None)
        handed.save()
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _note(workflow).exists()
        handed.pulls["7"]["statusCheckRollup"] = [
            {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ]
        run.update(status="completed", conclusion="success")
        handed.save()
    _fail(handed, run_id=50)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh pr ready")) == 1
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]
    assert not (_folder(workflow) / "check-repair-7.json").exists()
    assert (
        "pull request checks failed again after a worker's fix: job CI: server tests of run 50, "
        "first failing test tests/test_dates.py::test_parses_a_leap_day"
    ) in _handbacks(workflow)

def test_a_failing_test_beside_a_cancelled_job_still_goes_to_a_worker(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed)
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    # Fail-fast cancelled another part when the test failed.
    jobs.insert(
        0,
        {"id": 19, "name": "CI: web", "status": "completed", "conclusion": "cancelled"},
    )
    handed.api["repos/example-org/example-app/check-runs/19/annotations?per_page=100"] = []
    (workflow.state / "job-logs/19").write_text("The operation was canceled.\n")
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh pr ready") == ["gh pr ready 7 -R example-org/example-app --undo"]
    assert _started(workflow) == []
    assert _handbacks(workflow) == [
        "pull request checks failed on its own test: a worker fixes job CI: server tests of "
        "run 20, first failing test tests/test_dates.py::test_parses_a_leap_day"
    ]

def test_a_failed_job_with_nothing_to_read_goes_to_a_reviewer(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=None)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh pr ready") == []
    assert _started(workflow) == ["review-7"]

@pytest.mark.parametrize(
    ("log", "test"),
    [
        (
            "\x1b[31mFAIL\x1b[39m src/dates.test.ts > parses a leap day\n",
            "src/dates.test.ts > parses a leap day",
        ),
        (
            "  1) [chromium] › e2e/plan.spec.ts:12:5 › opens the plan\n",
            "[chromium] > e2e/plan.spec.ts:12:5 > opens the plan",
        ),
        (
            'FAILED tests/test_quote.py::test_reads["$(id)",`id`\\] - assert\n',
            "tests/test_quote.py::test_reads[ (id) id ]",
        ),
        ("Error: Process completed with exit code 1.\n", "not reported"),
        (
            "##[error]Failed to run: Error: Unexpected HTTP response: 404, "
            "Error: Unexpected HTTP response: 404\n",
            "not reported",
        ),
        (
            "##[error]Failed to run: Error: Unexpected HTTP response: 5000\n",
            "not reported",
        ),
        (
            "FAILED tests/test_dates.py::test_totals[two items] - assert 3 == 4\n",
            "tests/test_dates.py::test_totals[two items]",
        ),
        (
            # The server under test answered 500: no runner of GitHub's failed it.
            '"GET /health HTTP/1.1" 500 Internal Server Error\n',
            "not reported",
        ),
    ],
)
def test_the_worker_sentence_names_the_first_failing_test_in_safe_words(
    handed: GitHub, log: str, test: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=log)

    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr

    assert session_arguments(workflow.state, "ticket-7")[-1] == (
        f'claude --effort medium --remote-control ticket-7 "{_repair_sentence(test=test)}"'
    )

@pytest.mark.parametrize("failure", ["runner lost", "timed out"])
def test_a_runner_error_beside_a_part_fail_fast_cancelled_is_rerun_once(
    handed: GitHub, failure: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    if failure == "timed out":
        _fail(handed, conclusion="timed_out", log=None)
    else:
        _fail(handed, log="The self-hosted runner: home-1 lost communication with the server.\n")
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    # Fail-fast cancelled the other part of the matrix when the runner failed.
    jobs.insert(
        0,
        {
            "id": 19,
            "name": "CI: server tests (2 of 2)",
            "status": "completed",
            "conclusion": "cancelled",
            "labels": ["example-app-home"],
        },
    )
    handed.api["repos/example-org/example-app/check-runs/19/annotations?per_page=100"] = []
    (workflow.state / "job-logs/19").write_text("##[error]The operation was canceled.\n")
    handed.save()

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == ["gh run rerun 20 -R example-org/example-app --failed"]
    assert workflow.calls("gh pr ready") == []
    assert _started(workflow) == []
    assert _note(workflow).exists()

def test_a_failing_test_without_the_label_keeps_its_audit_path(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    workflow.list_pull_request(7, draft=False, labels=())
    handed.pulls["7"]["labels"] = []
    _fail(handed)

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh pr ready") == []
    assert not _note(workflow).exists()
    assert not (_folder(workflow) / "check-repair-7.json").exists()
    assert (_folder(workflow) / "merge-review-7").exists()
    assert _started(workflow) == []

@pytest.mark.parametrize("workflow_name", ["ci", "images", "e2e"])
@pytest.mark.parametrize("run_status", ["in_progress", "completed"])
def test_transport_retry_waits_for_the_entire_run_and_keeps_the_handoff(
    handed: GitHub, workflow_name: str, run_status: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _behind(handed)
    _fail(handed, log="#13 ERROR: failed to solve: unexpected EOF while pulling a layer\n")
    path = _RUNS.format(head=handed.head)
    run = handed.api[path]["workflow_runs"][0]
    if workflow_name != "ci":
        handed.api[path] = {"workflow_runs": []}
        handed.api[path.replace("/ci.yml/", f"/{workflow_name}.yml/")] = {"workflow_runs": [run]}
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    jobs[-1]["name"] = "Docker images: image tests"
    jobs[0].update(status="in_progress", conclusion=None)
    run.update(status=run_status, conclusion="failure" if run_status == "completed" else None)
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == []
    assert _note(workflow).exists()
    assert _train(workflow)["waiting"] == ["7", "8"]
    jobs[0].update(status="completed", conclusion="success")
    run.update(status="completed", conclusion="failure")
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == ["gh run rerun 20 -R example-org/example-app --failed"]
    assert _note(workflow).exists()
    assert _train(workflow)["waiting"] == ["7", "8"]
    assert _started(workflow) == []

@pytest.mark.parametrize("log", [
    "failed to solve: short read: unexpected EOF\n" + _FAILED_TEST,
    "server under test: connection reset by peer\n",
    "server under test: i/o timeout\n",
    "server under test: unexpected EOF\n",
    "unexpected EOF from server under test\n",
    "failed to solve: failed to compute cache key: file not found\n",
    "#1 [internal] load metadata for example-app\n#2 server under test: unexpected EOF\n",
])
def test_transport_words_do_not_override_a_test_failure(handed: GitHub, log: str) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=log)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == []
    assert not _note(workflow).exists()
    assert workflow.calls("gh pr ready") == ["gh pr ready 7 -R example-org/example-app --undo"]

@pytest.mark.parametrize("log", [
    "failed to solve: failed to compute cache key: short read: unexpected EOF\n",
    "ERROR: unexpected EOF while exporting a manifest\n",
    "ERROR: short read while resolving an image layer\n",
    "registry pull: connection reset by peer\n",
    "registry: i/o timeout\n",
    "pull-through cache: TLS handshake timeout\n",
    "failed to solve: connection reset by peer\n",
])
def test_registry_transport_errors_are_retried_through_the_handoff(
    handed: GitHub, log: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=log)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == ["gh run rerun 20 -R example-org/example-app --failed"]
    assert _note(workflow).exists()
    assert _started(workflow) == []

def test_github_still_running_refusal_does_not_consume_the_retry(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log="failed to solve: short read: unexpected EOF\n")
    refused = workflow.state / "fail-rerun"
    refused.write_text("This workflow is already running")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _note(workflow).exists()
    assert _started(workflow) == []
    assert not (_folder(workflow) / "check-rerun-20.json").exists()
    refused.unlink()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh run rerun")) == 2
    assert _note(workflow).exists()
    assert json.loads((_folder(workflow) / "check-rerun-20.json").read_text())["state"] == "requested"

def test_a_reported_test_wins_even_when_its_job_times_out(handed: GitHub) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, conclusion="timed_out", log="failed to solve: unexpected EOF\n" + _FAILED_TEST)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == []
    assert not _note(workflow).exists()
    assert workflow.calls("gh pr ready") == ["gh pr ready 7 -R example-org/example-app --undo"]

@pytest.mark.parametrize("rollup", ["FAILURE", "SUCCESS"])
def test_successful_retry_jobs_wait_for_the_run_metadata_before_handing_back(
    handed: GitHub, rollup: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log="failed to solve: short read: unexpected EOF\n")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    for job in jobs:
        job["conclusion"] = "success"
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = rollup
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _note(workflow).exists()
    assert _started(workflow) == []
    assert len(workflow.calls("gh run rerun")) == 1
    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    run.update(run_attempt=2, conclusion="success")
    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").exists(), done.stdout + done.stderr + str(_train(workflow))
    assert (workflow.state / "merged").read_text() == "7"
    assert len(workflow.calls("gh run rerun")) == 1

@pytest.mark.parametrize("claim_state", ["requested", "claim"])
def test_a_successful_retry_with_late_rollup_keeps_review_authorization(
    handed: GitHub, claim_state: str
) -> None:
    workflow = handed.workflow
    original = handed.head
    _github_answers_repairs(workflow)
    _fail(handed, log=(
        "CI: server tests\tSet up Go\t2026-11-01T12:00:00Z "
        "##[error]Failed to run: Error: Unexpected HTTP response: 504, "
        "Error: Unexpected HTTP response: 504\n"
    ))
    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh run rerun")) == 1
    assert _note(workflow).exists()

    claim = _folder(workflow) / "check-rerun-20.json"
    saved = json.loads(claim.read_text())
    saved["state"] = claim_state  # A restart may leave the pre-request claim.
    claim.write_text(json.dumps(saved))
    run.update(run_attempt=2, status="completed", conclusion="success")
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    for job in jobs:
        job.update(status="completed", conclusion="success")
    handed.save()  # The PR check summary still reports the previous failure.
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        if not _note(workflow).exists():
            reason = "; ".join(_handbacks(workflow))
            pytest.fail("successful retry lost its passed-review handoff: " + reason)
        assert _started(workflow) == []
        assert not (workflow.state / "merged").exists()
        assert not workflow.calls("gh pr ready")
        assert len(workflow.calls("gh run rerun")) == 1
        assert handed.head == original

    handed.pulls["7"]["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"
    assert _started(workflow) == []
    assert len(workflow.calls("gh run rerun")) == 1


@pytest.mark.parametrize("check", [
    {"__typename": "StatusContext", "context": "example-app-policy", "state": "FAILURE"},
    {"__typename": "CheckRun", "name": "example-app-policy", "status": "COMPLETED",
     "conclusion": "FAILURE"},
    {"__typename": "CheckRun", "name": "CI result", "status": "COMPLETED",
     "conclusion": "FAILURE",
     "detailsUrl": "https://github.com/example-org/example-app/actions/runs/30/job/32"},
], ids=["status-context", "unrelated-job", "same-name-other-run"])
def test_a_successful_retry_does_not_hide_an_unrelated_failed_check(
    handed: GitHub, check: dict
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log="##[error]Failed to run: Error: Unexpected HTTP response: 504\n")
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _note(workflow).exists()
    assert len(workflow.calls("gh run rerun")) == 1

    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    run.update(run_attempt=2, status="completed", conclusion="success")
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    for job in jobs:
        job.update(status="completed", conclusion="success")
    handed.pulls["7"]["statusCheckRollup"].append(check)
    handed.save()  # The retry's old failure and an unrelated real failure coexist.

    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not _note(workflow).exists()
    assert _started(workflow) == ["review-7"]
    assert not (workflow.state / "merged").exists()
    assert not workflow.calls("gh pr ready")
    assert len(workflow.calls("gh run rerun")) == 1


@pytest.mark.parametrize("error", [
    _DOWNLOAD_LINE, "unexpected EOF", "failed to copy: httpReadSeeker: unexpected status",
    "toomanyrequests: download rate limit exceeded",
])
@pytest.mark.parametrize("command", ["docker compose up -d database", "docker pull example-app"])
def test_image_download_before_test_errors_is_retried(
    handed: GitHub, error: str, command: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    log = _COMPOSE_DOWNLOAD.replace("docker compose up -d database", command).replace(
        _DOWNLOAD_LINE, error
    ) + _FAILED_TEST
    _fail(handed, log=log)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh run rerun")) == 1
    assert _note(workflow).exists()
    assert workflow.calls("gh pr ready") == []
    assert _started(workflow) == []

@pytest.mark.parametrize("error", [
    _DOWNLOAD_LINE, "unexpected EOF", "failed to copy: httpReadSeeker", "toomanyrequests",
])
def test_image_download_words_in_tests_still_go_to_a_worker(handed: GitHub, error: str) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    log = (
        "CI: server tests\tStart services\t2026-11-04T10:00:00Z ##[group]Run docker compose up -d database\n"
        "CI: server tests\tStart services\t2026-11-04T10:00:01Z database Started\n"
        "CI: server tests\tTests\t2026-11-04T10:00:02Z ##[group]Run pytest\n"
        f"CI: server tests\tTests\t2026-11-04T10:00:03Z server under test: {error}\n"
    ) + _FAILED_TEST
    _fail(handed, log=log)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == []
    assert not _note(workflow).exists()
    assert len(workflow.calls("gh pr ready")) == 1

@pytest.mark.parametrize("second", [
    "same", "another job recovered", "interrupted claim", "runner not reported",
    "another runner", "another error", "own test",
])
def test_repeated_image_download_waits_only_for_the_same_runner_and_failure(
    handed: GitHub, second: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=_COMPOSE_DOWNLOAD)
    jobs = handed.api["repos/example-org/example-app/actions/runs/20/jobs?per_page=100"]["jobs"]
    jobs[-1]["runner_name"] = "example-runner"
    if second == "another job recovered":
        jobs.insert(-1, {**jobs[-1], "id": 98, "name": "CI: browser tests"})
        handed.api["repos/example-org/example-app/check-runs/98/annotations?per_page=100"] = []
        (workflow.state / "job-logs/98").write_text(_COMPOSE_DOWNLOAD)
    handed.save()
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _note(workflow).exists()
    if second == "interrupted claim":
        claim = _folder(workflow) / "check-rerun-20.json"
        saved = json.loads(claim.read_text())
        saved["state"] = "claim"
        claim.write_text(json.dumps(saved))
    run = handed.api[_RUNS.format(head=handed.head)]["workflow_runs"][0]
    run["run_attempt"] = 2
    if second == "another job recovered":
        jobs[-2]["conclusion"] = "success"
    jobs[-1]["id"] = 99  # A re-run assigns new job IDs.
    handed.api["repos/example-org/example-app/check-runs/99/annotations?per_page=100"] = []
    log = _COMPOSE_DOWNLOAD + _FAILED_TEST
    if second == "another runner":
        jobs[-1]["runner_name"] = "example-runner-2"
    elif second == "runner not reported":
        jobs[-1].pop("runner_name")
    elif second == "another error":
        log = _COMPOSE_DOWNLOAD.replace(_DOWNLOAD_LINE, "toomanyrequests") + _FAILED_TEST
    elif second == "own test":
        log = _FAILED_TEST
    (workflow.state / "job-logs/99").write_text(log)
    handed.save()
    for _ in range(2):
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh run rerun")) == 1
    if second in ("same", "another job recovered", "interrupted claim"):
        assert _note(workflow).exists()
        assert workflow.calls("gh pr ready") == []
        assert _started(workflow) == []
        pending = list((_folder(workflow) / "runner-downloads.pending").glob("*.json"))
        assert len(pending) == 1
        line = json.loads(pending[0].read_text())["line"]
        assert "example-runner" in line and _DOWNLOAD_LINE in line
        assert not list((_folder(workflow) / "merge-handbacks.pending").glob("*"))
        sent = _folder(workflow) / "runner-downloads.sent" / pending[0].name
        sent.parent.mkdir()
        pending[0].rename(sent)
        done = workflow.run_script("tick")
        assert done.returncode == 0, done.stdout + done.stderr
        assert not list(pending[0].parent.glob("*.json"))
        assert _note(workflow).exists()
    else:
        assert not _note(workflow).exists()
        assert not list((_folder(workflow) / "runner-downloads.pending").glob("*.json"))
        assert bool(workflow.calls("gh pr ready")) == (second == "own test")

@pytest.mark.parametrize("log", [
    "##[group]Run docker compose up -d database\npytest\n##[endgroup]\n" + _DOWNLOAD_LINE + "\n" + _FAILED_TEST,
    "Run docker pull example-app\nunexpected EOF\n" + _FAILED_TEST,
    "Run docker pull playwright\nunexpected EOF\n" + _FAILED_TEST,
    'Run docker compose -f "example app.yml" --profile=tests up -d database\nunexpected EOF\n' + _FAILED_TEST,
    # GitHub prints both pulls once, before either executes. Completing the first
    # must leave the second download in context.
    "##[group]Run docker pull example-app\ndocker pull example-database\n##[endgroup]\n"
    "Status: Image is up to date for example-app\nUsing default tag: latest\nunexpected EOF\n",
    "##[group]Run docker compose \\\n  --file example.yml \\\n  up -d database\n##[endgroup]\n"
    "database Pulling\nunexpected EOF\n",
    "##[group]Run docker compose up --build -d database\n##[endgroup]\n"
    "#2 [internal] load metadata for example-database\n"
    "#2 ERROR: failed to copy: httpReadSeeker\n",
    "##[group]Run docker compose up -d database cache\ndocker pull example-app\n##[endgroup]\n"
    "Container database Started\nContainer cache Started\nunexpected EOF\n",
    # Shell removes backslash-newline without inserting a space; retain indentation.
    "CI: server tests\tStart services\t2026-11-04T10:00:00Z ##[group]Run docker compose\\\n"
    "CI: server tests\tStart services\t2026-11-04T10:00:00Z   --file example.yml\\\n"
    "CI: server tests\tStart services\t2026-11-04T10:00:00Z   up -d database\n"
    "CI: server tests\tStart services\t2026-11-04T10:00:00Z ##[endgroup]\n"
    "CI: server tests\tStart services\t2026-11-04T10:00:01Z unexpected EOF\n",
])
def test_download_order_uses_execution_output_after_the_command_preamble(
    handed: GitHub, log: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=log)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(workflow.calls("gh run rerun")) == 1
    assert _note(workflow).exists()

@pytest.mark.parametrize("log", [
    _FAILED_TEST + _COMPOSE_DOWNLOAD,
    "Run pytest\n" + _COMPOSE_DOWNLOAD + _FAILED_TEST,
    "Run docker compose up -d database\ndatabase Started\nRun pytest\n" + _DOWNLOAD_LINE + "\n" + _FAILED_TEST,
    "Run docker build .\nfailed to copy: httpReadSeeker\n" + _FAILED_TEST,
    "Run docker compose up database\nexample-app | unexpected EOF\n" + _FAILED_TEST,
    "Run docker compose up database\nexample-app | toomanyrequests\n" + _FAILED_TEST,
    "Run docker compose up -d database\nContainer example-app Started\n" + _DOWNLOAD_LINE + "\n" + _FAILED_TEST,
    "Run docker pull example-app\nStatus: Image is up to date for example-app\nunexpected EOF\n" + _FAILED_TEST,
    "Run docker compose up database\nexample-app | " + _FAILED_TEST + _COMPOSE_DOWNLOAD,
    _COMPOSE_DOWNLOAD + "CI: server tests\tStart services\t2026-11-04T10:00:03Z Container example-app Started\n" + _FAILED_TEST,
    "Run docker pull example-app\nunexpected EOF\nStatus: Downloaded newer image for example-app\n" + _FAILED_TEST,
    "##[group]Run docker compose up -d database\necho toomanyrequests\n##[endgroup]\n" + _FAILED_TEST,
    "Run docker compose run --rm tests pytest -k pull\nunexpected EOF\n" + _FAILED_TEST,
    "Run docker compose exec example-app npm test -- --grep up\ntoomanyrequests\n" + _FAILED_TEST,
    "##[group]Run set -e\npytest -q\ndocker pull example-app\n##[endgroup]\nunexpected EOF\n" + _FAILED_TEST,
    "##[group]Run set -e\nnpm test\ndocker compose up database\n##[endgroup]\ntoomanyrequests\n" + _FAILED_TEST,
    "Run docker compose --unknown example-app up\nunexpected EOF\n" + _FAILED_TEST,
    # A Compose build's RUN output is application code, even before services start.
    "##[group]Run docker compose up --build -d example-app\n##[endgroup]\n"
    "#7 [example-app 3/3] RUN python build.py\n#7 0.123 RuntimeError: unexpected EOF\n"
    '#7 ERROR: process "/bin/sh -c python build.py" did not complete successfully: exit code: 1\n',
    # Download context from one vertex must never authorize another vertex's RUN.
    "Run docker compose up --build -d example-app\n"
    "#2 [internal] load metadata for example-app\n#2 DONE 0.1s\n"
    "#7 [example-app 3/3] RUN python build.py\n#7 0.123 RuntimeError: unexpected EOF\n",
    # Once both planned pulls finish, unrelated EOF output is no download evidence.
    "##[group]Run docker pull example-app\ndocker pull example-database\n##[endgroup]\n"
    "Status: Image is up to date for example-app\n"
    "Status: Downloaded newer image for example-database\nunexpected EOF\n" + _FAILED_TEST,
    "##[group]Run pytest \\\n  -q\ndocker compose \\\n  up -d database\n##[endgroup]\n"
    "unexpected EOF\n" + _FAILED_TEST,
])
def test_download_context_does_not_override_tests_that_already_started(
    handed: GitHub, log: str
) -> None:
    workflow = handed.workflow
    _github_answers_repairs(workflow)
    _fail(handed, log=log)
    done = workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    assert workflow.calls("gh run rerun") == []
    assert not _note(workflow).exists()
    assert len(workflow.calls("gh pr ready")) == 1

@pytest.mark.parametrize("error", ["unexpected EOF", "toomanyrequests", "failed to copy: httpReadSeeker"])
def test_dockerfile_command_summaries_are_not_download_evidence(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from check_repair import image_download

    # The existing transport fallback may retry this once. It must never record
    # download evidence that could hold the handoff waiting for a cache repair.
    log = (
        "Run docker compose up --build -d example-app\n"
        "#7 [example-app 3/3] RUN echo " + error + " && exit 1\n"
        "#7 0.123 " + error + "\n"
        'failed to solve: process "/bin/sh -c echo ' + error
        + ' && exit 1" did not complete successfully: exit code: 1\n'
    )
    assert image_download(log) is None
