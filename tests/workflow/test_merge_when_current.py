"""Merge safety with real Git histories and a stand-in for GitHub."""

import errno
import fcntl
import importlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from stand_in_server import StandInServer
from workflow.test_ci_proof import annotations
from workflow_harness import Workflow

from workflow.merge_gate_fixtures import (
    ADVISORY,
    GitHub,
    MEMORY_SHORTAGES,
    ONE_AT_A_TIME_RUNS,
    SELECTED,
    UNREACHABLE,
    _ADVISORIES_ONLY,
    _ADVISORY_LINES,
    _AUDIT,
    _DEPENDENCIES,
    _GH,
    _MAIN_IMAGES,
    _MAIN_UNREACHABLE,
    _OLDER,
    _PYTHON,
    _REPO,
    _SUITE_RUNS,
    _advisories_notice,
    _advisory_config,
    _advisory_watch,
    _after_the_checks,
    _attempt_job,
    _attempt_path,
    _attempt_times,
    _batch_call,
    _batch_setup,
    _batch_state,
    _blocked_first,
    _branch_runs,
    _budget,
    _cancelled,
    _cancelled_by_github,
    _changes,
    _check_runs,
    _check_runs_path,
    _ci_runs,
    _dependencies_red,
    _dropped_nine,
    _finish_batch,
    _finish_prefix,
    _green_rerun_on_old_main,
    _green_second,
    _hand_off,
    _handing_off,
    _held,
    _hot,
    _images,
    _labelled,
    _line,
    _locked_calls,
    _main_audit_failed,
    _main_ci_run,
    _main_image_run,
    _main_jobs,
    _main_red_in_one_part,
    _main_red_on_an_advisory,
    _members_changing,
    _merges,
    _notes_api,
    _notes_by_ci_proof,
    _notes_path,
    _numbers,
    _parents,
    _pr_job,
    _pr_parts,
    _prefix_only,
    _prefixes,
    _pull_passed_over_mains_advisory,
    _pull_skipped_older,
    _push_without_images,
    _queue_config,
    _queued_prefix_job,
    _receipt,
    _red_ci,
    _refreshed_first,
    _refusal,
    _rerun_failed_jobs,
    _restored_before_eight,
    _scheduled_images,
    _scratch,
    _scratch_branches,
    _selected_server_tests,
    _server_suite_dependencies_red,
    _squash_with_pending_main,
    _stale,
    _suite,
    _tick_context,
    _train,
    _two_stale_pulls,
    _unlisted_line,
    _waiting,
    _waiting_drops,
    _waiting_reviewers,
    advisory_service_answers,
    batch_dependencies_red,
    blocked_first,
    gate,
    github,
    hook_runs,
    hook_runs_and_waits,
    line_up,
    pull_dependencies_red,
    refreshed_first,
    rerun_calls,
    short_of_memory,
    train_status,
    two_stale_pulls,
    waiting_reviewers,
)


def test_a_pull_request_tested_against_older_main_is_refreshed_not_merged(github: GitHub) -> None:
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert not (workflow.state / "merged").exists()
    assert workflow.git("rev-parse", "HEAD^2") == github.base
    assert workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[
        0
    ] == workflow.git("rev-parse", "HEAD")
    assert "CI requested" in done.stdout


def test_a_stale_pull_request_is_refreshed_while_main_ci_is_still_running(
    github: GitHub,
) -> None:
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    [main_run] = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"]
    main_run.update(status="in_progress", conclusion=None)
    github.ci(github.base, github.head, tested_base=old)

    done = github.run()

    assert done.returncode == 2, done.stdout + done.stderr
    assert "main: pending" in done.stdout
    assert workflow.git("rev-parse", "HEAD^2") == github.base
    assert not (workflow.state / "merged").exists()


def test_a_fresh_pull_request_waits_for_main_ci_that_is_still_running(github: GitHub) -> None:
    [main_run] = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"]
    main_run.update(status="in_progress", conclusion=None)

    done = github.run()

    assert done.returncode == 1
    assert "merging stopped: main: pending" in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_fresh_pull_request_merges_with_its_head_pinned(github: GitHub) -> None:
    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert any(
        f"--squash --match-head-commit {github.head}" in call
        for call in github.workflow.calls("gh pr merge")
    )


def test_a_repository_without_an_image_or_browser_workflow_merges_without_their_checks(
    github: GitHub,
) -> None:
    """A repository set up from nothing keeps only its CI workflow: GitHub lists no runs of a
    workflow it does not hold, and no change needs its checks."""
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("rm", "--quiet", ".github/workflows/images.yml")
    workflow.git("commit", "--quiet", "-m", "ci: no images")
    workflow.git("push", "--quiet", "origin", "main")
    github.base = workflow.git("rev-parse", "main")
    workflow.git("checkout", "ticket/seven")
    workflow.git("merge", "--quiet", "--no-edit", "main")
    github.head = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "--quiet", "origin", "HEAD:ticket/seven")
    github.pulls["7"]["headRefOid"] = github.head
    github.main()
    github.ci(github.base, github.head)
    github.missing_workflows = ["images.yml", "e2e.yml"]

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (workflow.state / "merged").read_text() == "7"


def test_a_repository_without_its_ci_workflow_never_merges(github: GitHub) -> None:
    github.missing_workflows = ["ci.yml"]

    done = github.run()

    assert done.returncode != 0
    assert "HTTP 404" in done.stdout + done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_fix_that_passed_every_check_failing_on_red_main_merges(github: GitHub) -> None:
    github.main("failure")
    _pr_job(github, "CI: web", "success")

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert "passed every check failing on main: CI result, CI: web" in done.stdout


def test_a_conflict_keeps_the_required_merge_message_until_the_session_resolves_it(
    github: GitHub,
) -> None:
    workflow = github.workflow
    shared = workflow.repo / "shared.txt"
    shared.write_text("the pull request\n")
    workflow.git("commit", "-am", "fix: branch edit")
    github.head = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "HEAD:ticket/seven")
    workflow.git("checkout", "main")
    shared.write_text("main changed it\n")
    workflow.git("commit", "-am", "fix: main edit")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.pulls["7"].update(
        headRefOid=github.head, mergeable="CONFLICTING", mergeStateStatus="DIRTY"
    )
    github.main()

    refused = github.run()

    assert refused.returncode == 1
    assert "resolve the conflicts in shared.txt" in refused.stderr
    assert not (workflow.state / "merged").exists()
    shared.write_text("the resolved edit\n")
    workflow.git("add", "shared.txt")
    workflow.git("commit", "--no-edit")
    assert workflow.git("log", "-1", "--format=%s") == "merge main: conflict in shared.txt"
    workflow.git("push", "origin", "HEAD:ticket/seven")
    github.head = workflow.git("rev-parse", "HEAD")
    github.pulls["7"].update(
        headRefOid=github.head, mergeable="MERGEABLE", mergeStateStatus="CLEAN"
    )
    github.ci(github.base, github.head)
    assert github.run().returncode == 0


def test_two_racing_merges_wait_and_the_second_rechecks_main(github: GitHub) -> None:
    workflow = github.workflow
    workflow.running("review-8")
    second_worktree = workflow.root / "second"
    workflow.git("worktree", "add", "-b", "ticket/eight", str(second_worktree), github.base)
    workflow.git("commit", "--allow-empty", "-m", "fix: eight", cwd=second_worktree)
    second_head = workflow.git("rev-parse", "HEAD", cwd=second_worktree)
    workflow.git("push", "origin", "HEAD:ticket/eight", cwd=second_worktree)
    github.pulls["8"] = {
        **deepcopy(github.pulls["7"]),
        "headRefOid": second_head,
        "headRefName": "ticket/eight",
    }
    github.ci(github.base, second_head, run_id=30)
    old = github.base
    github.base = workflow.git(
        "commit-tree",
        workflow.git("rev-parse", f"{old}^{{tree}}"),
        "-p",
        old,
        "-m",
        "fix: seven merged",
    )
    github.next_main = github.base
    workflow.git("push", "origin", f"{github.base}:refs/heads/prepared-merge")
    github.main()
    github.save()
    entered = workflow.state / "merge-entered"
    release = workflow.state / "merge-continue"
    os.mkfifo(entered)
    os.mkfifo(release)
    (workflow.state / "block-first-merge").touch()
    entered_fd = os.open(entered, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    processes: list[subprocess.Popen[str]] = []
    try:
        first = subprocess.Popen(
            ["bash", str(workflow.script.with_name("merge-when-current")), "7"],
            cwd=workflow.repo,
            env=workflow.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(first)
        assert select.select([entered_fd], [], [], 15)[0], "first merge never reached GitHub"
        assert os.read(entered_fd, 100) == b"entered\n"
        second = subprocess.Popen(
            ["bash", str(workflow.script.with_name("merge-when-current")), "8"],
            cwd=second_worktree,
            env=workflow.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(second)
        assert second.stdout is not None
        assert select.select([second.stdout], [], [], 15)[0]
        assert second.stdout.readline().strip() == "waiting for the merge lock"
        assert not workflow.calls("gh pr view 8")
        os.write(release_fd, b"x")
        first_output = first.communicate(timeout=15)
        second_output = second.communicate(timeout=15)
        assert first.returncode == 0, first_output
        assert second.returncode == 2, second_output
        assert (workflow.state / "merged").read_text() == "7"
        assert workflow.git("rev-parse", "HEAD^2", cwd=second_worktree) == github.base
        assert "CI requested" in second_output[0]
        assert len(workflow.calls("gh pr merge")) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        os.close(entered_fd)
        os.close(release_fd)


def test_a_handoff_into_a_long_line_reads_no_other_member_under_the_merge_lock(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _handing_off(github)
    line = [str(number) for number in range(8, 58)]
    for number in line:
        github.pulls[number] = {
            **deepcopy(github.pulls["7"]),
            "headRefOid": f"{int(number):040x}",
            "headRefName": f"ticket/{number}",
        }
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": line}))

    done = _hand_off(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (folder / "merge-wanted-7").exists()
    assert _waiting(workflow) == [*line, "7"]
    # Nothing is asked of GitHub's API under the lock, and the line's members are not read at all.
    assert _locked_calls(workflow) == []
    assert workflow.calls("gh pr view 7 ")[-1:] and len(_held(workflow)) == 1
    others = [github.pulls[number]["headRefOid"] for number in line]
    assert not [
        call
        for call in workflow.calls("gh")
        if call.startswith(tuple(f"gh pr view {number} " for number in line))
        or any(head in call for head in others)
    ]
    assert re.search(
        r"^[0-9]+ handoff 7 held the merge lock for [0-9]+\.[0-9] s$",
        workflow.wake_log.read_text(),
        re.MULTILINE,
    )


def test_another_gate_call_gets_the_merge_lock_while_a_handoff_reads_github(
    github: GitHub,
) -> None:
    workflow = github.workflow
    worktree = _handing_off(github)
    entered = workflow.state / "call-entered"
    release = workflow.state / "call-continue"
    os.mkfifo(entered)
    os.mkfifo(release)
    # GitHub is slow to answer the handoff's read of its reviews.
    (workflow.state / "pause-call").write_text("pulls/7/reviews")
    entered_fd = os.open(entered, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    handoff = subprocess.Popen(
        ["bash", str(workflow.script.with_name("merge-when-current")), "--handoff", "7"],
        cwd=worktree,
        env=workflow.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([entered_fd], [], [], 15)[0], "the handoff never read its reviews"
        assert os.read(entered_fd, 100) == b"entered\n"
        # Another gate call waiting for the lock gets it while the handoff waits on GitHub.
        got = None
        started = time.monotonic()
        with (workflow.repo / ".local/workflow/merge.lock").open("w") as lock:
            while got is None and time.monotonic() - started < 5:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = time.monotonic() - started
                except BlockingIOError:
                    time.sleep(0.05)
        os.write(release_fd, b"x")
        out, err = handoff.communicate(timeout=60)
    finally:
        if handoff.poll() is None:
            handoff.kill()
            handoff.communicate()
        os.close(entered_fd)
        os.close(release_fd)
    assert got is not None, "the handoff held the merge lock while it read GitHub"
    assert handoff.returncode == 0, out + err
    assert (workflow.repo / ".local/workflow/merge-wanted-7").exists()


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"headRefOid": "f" * 40}, "the pull request head changed during the handoff"),
        ({"headRefName": "ticket/other"}, "the pull request head changed during the handoff"),
        ({"isDraft": True}, "only a ready, non-conflicting pull request can be handed off"),
        ({"mergeable": "CONFLICTING"}, "only a ready, non-conflicting pull request"),
        (
            {
                "statusCheckRollup": [
                    {"name": "CI result", "status": "COMPLETED", "conclusion": "FAILURE"}
                ]
            },
            "only a ready, non-conflicting pull request",
        ),
    ],
)
def test_a_pull_request_that_changed_before_the_lock_was_taken_is_refused(
    github: GitHub, change: dict[str, Any], refusal: str
) -> None:
    workflow = github.workflow
    _handing_off(github)
    github.after_view = {"7": change}

    done = _hand_off(github)

    assert done.returncode == 1 and refusal in done.stderr, done.stdout + done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert "7" not in _waiting(workflow)
    # The refusal comes from the read just before the lock: the lock is never taken.
    assert _locked_calls(workflow) == [] and _held(workflow) == []


@pytest.mark.parametrize(
    ("case", "refusal"),
    [
        ("draft", "only a ready, non-conflicting pull request can be handed off"),
        ("conflicting", "only a ready, non-conflicting pull request can be handed off"),
        ("failed", "only a ready, non-conflicting pull request can be handed off"),
        ("unreviewed", "the latest independent round must have passed on the current head"),
        ("sent back", "the latest independent round must have passed on the current head"),
        ("unclean", "handoff requires the clean published pull request worktree"),
        ("branch", "handoff requires the pull request branch"),
        ("pre-train checks", "pre-train checks failed"),
        ("blocked", "this pull request failed checks; fix it before rejoining the merge train"),
        ("whole suite", "a whole suite run on this pull request's tested merge failed"),
        ("prefix", "the ci run of this pull request's prefix (workflow/merge-batch-b) failed"),
    ],
)
def test_each_handoff_refusal_still_fires(github: GitHub, case: str, refusal: str) -> None:
    workflow = github.workflow
    worktree = _handing_off(github)
    folder = workflow.repo / ".local/workflow"
    pull = github.pulls["7"]
    if case == "draft":
        pull["isDraft"] = True
    elif case == "conflicting":
        pull["mergeable"] = "CONFLICTING"
    elif case == "failed":
        pull["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    elif case == "unreviewed":
        github.api[f"{_REPO}/pulls/7/reviews"] = []
    elif case == "sent back":
        github.api[f"{_REPO}/pulls/7/reviews"][0]["body"] = (
            "<!-- workflow-review round=1 result=sent-back -->\nNot yet."
        )
    elif case == "unclean":
        (worktree / "uncommitted.txt").write_text("not published\n")
    elif case == "branch":
        workflow.git("checkout", "--detach", cwd=worktree)
    elif case == "pre-train checks":
        workflow.fake("pre-train-checks", "#!/bin/bash\necho 'inventory is stale' >&2\nexit 1\n")
        # An earlier handoff of it waits in the line: it leaves the line, and its note goes.
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7"]}))
        (folder / "merge-wanted-7").write_text(
            json.dumps({"head": github.head, "round": 1, "branch": "ticket/seven"})
        )
    elif case == "blocked":
        # It failed in the line before, on this same head, and its run is still red.
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "merge-train.json").write_text(
            json.dumps({"waiting": ["7"], "blocked": {"7": github.head}})
        )
        _red_ci(github)
    elif case == "whole suite":
        # The whole suite of its tested merge failed, on this same head.
        suite = {
            "member": github.head,
            "head": "a" * 40,
            "branch": "workflow/merge-batch-a",
            "workflows": ["ci"],
            "requested": ["ci"],
        }
        github.ci_dispatched("a" * 40, suite["branch"], run_id=40, conclusion="failure")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "merge-train.json").write_text(
            json.dumps(
                {"waiting": ["7"], "blocked": {"7": github.head}, "suites": {"7": suite}}
            )
        )
    else:
        # Its prefix's CI run failed on this same head, and no re-run of it passed since.
        batch = {"head": "b" * 40, "branch": "workflow/merge-batch-b", "workflows": ["ci"]}
        github.ci_dispatched("b" * 40, batch["branch"], run_id=41, conclusion="failure")
        failure = {"pull": "20:1", "batch": batch, "workflow": "ci", "run": "41:1"}
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "merge-train.json").write_text(
            json.dumps(
                {
                    "waiting": ["7"],
                    "blocked": {"7": github.head},
                    "batch_failed": {"7": failure},
                }
            )
        )

    done = _hand_off(github)

    assert done.returncode == 1 and refusal in done.stderr, done.stdout + done.stderr
    assert not (folder / "merge-wanted-7").exists()
    if case in ("blocked", "whole suite", "prefix"):
        # It keeps its place, passed over until a green re-run, as before.
        train = json.loads((folder / "merge-train.json").read_text())
        assert train["waiting"] == ["7"] and train["blocked"] == {"7": github.head}
    else:
        assert "7" not in _waiting(workflow)


def test_a_handoff_whose_place_the_tick_changed_meanwhile_is_judged_again(
    github: GitHub,
) -> None:
    workflow = github.workflow
    _handing_off(github)
    _red_ci(github)
    # While the handoff judged its place, the tick blocked it for its red run.
    github.line_after_view = {"7": [None, {"waiting": ["7"], "blocked": {"7": github.head}}]}

    done = _hand_off(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "this pull request failed checks" in done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    train = json.loads((workflow.repo / ".local/workflow/merge-train.json").read_text())
    assert train["waiting"] == ["7"] and train["blocked"] == {"7": github.head}
    assert _locked_calls(workflow) == [] and len(_held(workflow)) == 2


def test_a_handoff_whose_place_keeps_changing_waits(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    github.line_after_view = {
        "7": [None, *({"waiting": [], "approved": {"7": f"{n:040x}"}} for n in range(3))]
    }

    done = _hand_off(github)

    assert done.returncode == 1 and "merge waiting" in done.stderr, done.stdout + done.stderr
    assert "kept changing" in done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert _locked_calls(workflow) == [] and len(_held(workflow)) == 3


def test_a_newly_joined_member_waits_for_the_tick_to_read_its_run(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    github.pulls["8"] = {**deepcopy(github.pulls["7"]), "headRefOid": "8" * 40}
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    # 8 waits, green on current main; 7 joins with a higher priority.
    (folder / "merge-train.json").write_text(
        json.dumps({"waiting": ["8"], "passing": ["8"], "priorities": {"8": "low"}})
    )
    github.pulls["7"]["labels"] = [{"name": "priority: high"}]

    done = _hand_off(github)

    assert done.returncode == 0, done.stdout + done.stderr
    train = json.loads((folder / "merge-train.json").read_text())
    assert train["waiting"] == ["8", "7"] and train["pending"] == ["7"]
    assert train["turn"] == 0


def test_a_re_run_another_call_claimed_is_not_requested_again(github: GitHub) -> None:
    workflow = github.workflow
    folder = workflow.repo / ".local/workflow"
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "pull-rerun-20-1"
    # Another call claimed this re-run a moment ago.
    marker.write_text("automatic retry requested for failed jobs of run 20 of pull request 7\n")
    before = marker.read_text()

    done = subprocess.run(
        [
            "python3",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import merge_gate as g; "
            "from pathlib import Path; "
            "print(g._rerun({'id': 20}, Path(sys.argv[2]), Path(sys.argv[3]), 'now', "
            "name='run 20 of pull request 7'))",
            str(workflow.script.parent),
            str(folder),
            str(marker),
        ],
        cwd=workflow.repo,
        env=workflow.env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "re-run of run 20 of pull request 7 is requested" in done.stdout
    assert rerun_calls(workflow) == []
    assert marker.read_text() == before


def test_a_head_pushed_after_the_last_read_is_refused_under_the_lock(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    # Another push lands right after the read just before the lock.
    github.push_after_view = {"7": [False, True]}

    done = _hand_off(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "the pull request head changed during the handoff; review it again" in done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert "7" not in _waiting(workflow)
    assert _locked_calls(workflow) == [] and len(_held(workflow)) == 1


def test_a_stalled_head_check_lets_the_merge_lock_go_within_seconds(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    real = shutil.which("git", path=workflow.env["PATH"])
    # GitHub stops answering git after the checks: its branch's head cannot be read.
    workflow.fake(
        "git",
        "#!/bin/bash\n"
        f'if [ -e "{workflow.state}/stall" ] && [ "$1" = ls-remote ] '
        '&& [[ " $* " == *" refs/heads/ticket/seven "* ]]; then exec /bin/sleep 600; fi\n'
        f'exec "{real}" "$@"\n',
    )
    _after_the_checks(workflow, f": > '{workflow.state}/stall'")

    done = _hand_off(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "could not confirm the head; try again" in done.stderr
    assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
    assert "7" not in _waiting(workflow)
    # It waited out the check's own time limit, and no more.
    held = _held(workflow)
    assert len(held) == 1 and 9 < held[0] < 15, held


def test_a_budget_spent_after_the_checks_does_not_hold_the_merge_lock(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    # The line's other work spends GitHub's budget while the handoff runs its checks; it resets
    # 10 s later.
    budget = _budget(workflow)
    _after_the_checks(
        workflow,
        f"""echo '{{"core": {{"remaining": 0, "reset": '$(( $(date +%s) + 10 ))'}}}}' """
        f"""> '{budget}/budget.json'""",
    )
    started = time.monotonic()

    done = _hand_off(github)

    assert done.returncode == 0, done.stdout + done.stderr
    # It waited for the budget, before the lock.
    assert "GitHub budget low" in done.stderr and time.monotonic() - started > 8
    held = _held(workflow)
    assert len(held) == 1 and held[0] < 5, held


def test_busy_gh_call_slots_do_not_hold_the_merge_lock(github: GitHub) -> None:
    workflow = github.workflow
    _handing_off(github)
    budget = _budget(workflow)
    holder = workflow.state / "slot-holder.py"
    holder.write_text(
        "import fcntl, sys, time\n"
        "from pathlib import Path\n"
        "folder = Path(sys.argv[1])\n"
        "locks = [(folder / f'call-{n}.lock').open('a') for n in range(2)]\n"
        "for lock in locks:\n"
        "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "Path(sys.argv[2]).touch()\n"
        "time.sleep(10)\n"
    )
    ready = workflow.state / "slots-taken"
    # Two other calls, the tick reading the line's members, hold both gh slots for 10 s.
    _after_the_checks(
        workflow,
        f"python3 '{holder}' '{budget}' '{ready}' </dev/null >/dev/null 2>&1 &\n"
        f"while [ ! -e '{ready}' ]; do /bin/sleep 0.05; done",
    )
    started = time.monotonic()

    done = _hand_off(github)

    assert done.returncode == 0, done.stdout + done.stderr
    # It waited for a slot, before the lock.
    assert time.monotonic() - started > 8
    held = _held(workflow)
    assert len(held) == 1 and held[0] < 5, held


@pytest.mark.parametrize("meanwhile", ["nothing", "draft"])
def test_a_read_made_old_by_a_long_wait_for_the_lock_is_made_again(
    github: GitHub, meanwhile: str
) -> None:
    workflow = github.workflow
    _handing_off(github)
    holder = workflow.state / "lock-holder.py"
    # Another gate holds the merge lock for 7 s, from just after the handoff's checks. Once the
    # handoff has read its pull request before the lock, it may turn draft on GitHub.
    holder.write_text(
        "import fcntl, json, sys, time\n"
        "from pathlib import Path\n"
        "state, lock, draft = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == 'draft'\n"
        "held = lock.open('w')\n"
        "fcntl.flock(held, fcntl.LOCK_EX)\n"
        "(state / 'lock-taken').touch()\n"
        "started = time.monotonic()\n"
        "while draft and (state / 'calls.log').read_text().count('gh pr view 7 ') < 2:\n"
        "    time.sleep(0.05)\n"
        "if draft:\n"
        "    data = json.loads((state / 'github.json').read_text())\n"
        "    data['pulls']['7']['isDraft'] = True\n"
        "    (state / 'github.json').write_text(json.dumps(data))\n"
        "time.sleep(max(0, 7 - (time.monotonic() - started)))\n"
    )
    lock = workflow.repo / ".local/workflow/merge.lock"
    _after_the_checks(
        workflow,
        f"python3 '{holder}' '{workflow.state}' '{lock}' '{meanwhile}' "
        "</dev/null >/dev/null 2>&1 &\n"
        f"while [ ! -e '{workflow.state}/lock-taken' ]; do /bin/sleep 0.05; done",
    )

    done = _hand_off(github)

    # Its pull request is read again once the lock is free, and judged by that read.
    assert len(workflow.calls("gh pr view 7 ")) == 3
    if meanwhile == "draft":
        assert done.returncode == 1, done.stdout + done.stderr
        assert "only a ready, non-conflicting pull request can be handed off" in done.stderr
        assert not (workflow.repo / ".local/workflow/merge-wanted-7").exists()
        assert "7" not in _waiting(workflow)
    else:
        assert done.returncode == 0, done.stdout + done.stderr
        assert (workflow.repo / ".local/workflow/merge-wanted-7").exists()
        assert _waiting(workflow) == ["7"]
    assert all(held < 5 for held in _held(workflow))
