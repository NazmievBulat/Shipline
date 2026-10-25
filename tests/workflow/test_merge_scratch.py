"""Merge safety for scratch."""

import json
from datetime import UTC, datetime

import pytest

from workflow.merge_gate_fixtures import (
    GitHub,
    MEMORY_SHORTAGES,
    ONE_AT_A_TIME_RUNS,
    SELECTED,
    UNREACHABLE,
    _OLDER,
    _PYTHON,
    _REPO,
    _batch_call,
    _batch_setup,
    _batch_state,
    _branch_runs,
    _cancelled,
    _changes,
    _dropped_nine,
    _finish_batch,
    _finish_prefix,
    _green_second,
    _images,
    _main_image_run,
    _main_jobs,
    _merges,
    _numbers,
    _parents,
    _pr_job,
    _pr_parts,
    _prefix_only,
    _prefixes,
    _queue_config,
    _queued_prefix_job,
    _receipt,
    _rerun_failed_jobs,
    _restored_before_eight,
    _scratch,
    _scratch_branches,
    _selected_server_tests,
    _server_suite_dependencies_red,
    _squash_with_pending_main,
    _suite,
    _train,
    _waiting_drops,
    advisory_service_answers,
    batch_dependencies_red,
    github,
    rerun_calls,
    short_of_memory,
)


def test_a_stale_member_merging_alone_starts_a_refresh_and_its_whole_suites(
    github: GitHub,
) -> None:
    tree = _selected_server_tests(github)
    old = github.base
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: main moved")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    github.ci(github.base, github.head, tested_base=old, server=SELECTED)
    github.e2e(github.head, tree, suite="selected: 0 of 48 spec files", run_id=900_020)
    published = {github.head}

    assert _batch_call(github).returncode == 2  # The refresh: its CI and E2E start again.
    github.head = workflow.git("rev-parse", "HEAD")
    published.add(workflow.git("ls-remote", "origin", "refs/heads/ticket/seven").split()[0])
    github.pulls["7"]["headRefOid"] = github.head
    tree = workflow.git("rev-parse", f"{github.head}^{{tree}}")
    github.ci(github.base, github.head, run_id=70, server=SELECTED)
    github.e2e(github.head, tree, suite="selected: 0 of 48 spec files", run_id=900_070)
    assert _batch_call(github).returncode == 3  # Every server test and the browser suite.
    suite = _suite(github)
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    refreshes = len(published) - 1
    assert 2 * refreshes + len(workflow.calls("gh workflow run")) == ONE_AT_A_TIME_RUNS


def test_a_line_starts_no_more_runs_per_merged_member_than_merging_one_at_a_time(
    github: GitHub,
) -> None:
    _batch_setup(github, count=4, selected=True)
    heads = {number: pull["headRefOid"] for number, pull in github.pulls.items()}
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9", "10"]
    # No pull request branch was pushed: nothing ran on one again.
    for number, head in heads.items():
        branch = github.pulls[number]["headRefName"]
        assert github.workflow.git("ls-remote", "origin", f"refs/heads/{branch}").split()[0] == head
    runs = len(github.workflow.calls("gh workflow run"))
    # A prefix's CI and E2E for each member.
    assert runs == 8
    assert runs / 4 <= ONE_AT_A_TIME_RUNS


def test_three_members_each_tested_on_the_ones_ahead_merge_in_order(github: GitHub) -> None:
    _batch_setup(github)
    heads = {number: pull["headRefOid"] for number, pull in github.pulls.items()}
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    members = _prefixes(github)
    assert _numbers(github) == ["7", "8", "9"]
    # Each prefix is main and the members ahead of it, merged with its own member.
    for index, member in enumerate(members):
        ahead = members[index - 1]["commit"] if index else github.base
        assert _parents(github, member["commit"]) == [ahead, member["head"]]
        assert member["scratch"]["head"] == member["commit"]
    assert sorted(_scratch_branches(github)) == sorted(m["scratch"]["branch"] for m in members)
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3
    assert github.workflow.git("rev-parse", "HEAD") == heads["7"]
    for number, head in heads.items():
        branch = github.pulls[number]["headRefName"]
        assert github.workflow.git("ls-remote", "origin", f"refs/heads/{branch}").split()[0] == head
    assert _batch_call(github).returncode == 3  # A restart does not dispatch twice.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    final = github.workflow.git("rev-parse", "origin/main^{tree}")
    assert final == members[-1]["tree"]
    receipt = json.loads(
        (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
    )
    # Main inherits the run of the prefix that tested exactly its tree: the third's.
    assert receipt["tree"] == final and receipt["ci_run"] == 2100
    assert _batch_state(github)["prefixes"] is None
    assert _scratch_branches(github) == []
    assert "merged in this call: 7, 8, 9" in done.stdout
    assert "the tick tells the sessions of 8, 9" in done.stdout


def test_four_members_merge_in_order_as_their_prefix_runs_pass(github: GitHub) -> None:
    _batch_setup(github, count=4)
    assert _batch_call(github).returncode == 3
    assert _numbers(github) == ["7", "8", "9", "10"]
    trees = {member["number"]: member["tree"] for member in _prefixes(github)}
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 4
    # A passed prefix behind one still running waits for the member ahead of it.
    _finish_prefix(github, "8", run_id=1100)
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert _merges(github) == []
    _finish_prefix(github, "7")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "merged in this call: 7, 8" in done.stdout
    assert _merges(github) == ["7", "8"]
    assert _numbers(github) == ["9", "10"]
    github.workflow.git("checkout", "ticket/9")
    _finish_prefix(github, "10", run_id=3100)
    assert _batch_call(github, "9").returncode == 3
    assert _merges(github) == ["7", "8"]
    _finish_prefix(github, "9", run_id=2100)
    done = _batch_call(github, "9")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9", "10"]
    assert github.workflow.git("rev-parse", "origin/main^{tree}") == trees["10"]
    # Nothing was tested twice.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 4
    assert _batch_state(github)["prefixes"] is None


def test_a_merge_pulls_the_next_member_into_the_window_on_the_last_prefix(
    github: GitHub,
) -> None:
    _batch_setup(github, count=5)
    assert _batch_call(github).returncode == 3
    assert _numbers(github) == ["7", "8", "9", "10"]
    _finish_prefix(github, "7")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7"]
    members = _prefixes(github)
    assert [member["number"] for member in members] == ["8", "9", "10", "11"]
    # 11 is merged onto the prefix of 10, and its runs start in the same call.
    assert _parents(github, members[3]["commit"]) == [
        members[2]["commit"],
        github.pulls["11"]["headRefOid"],
    ]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 5


def test_a_member_leaving_the_line_keeps_the_prefixes_ahead_of_it(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_prefix(github, "7")
    _finish_prefix(github, "8", run_id=1100)
    github.pulls["9"]["state"] = "CLOSED"
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8"]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3


def test_a_dropped_prefix_has_its_unfinished_runs_cancelled_before_its_branch_goes(
    github: GitHub,
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    dropped, kept = _scratch(github, "9"), _scratch(github, "7")
    assert dropped is not None and kept is not None
    _branch_runs(
        github,
        dropped["branch"],
        (901, "queued"),
        (902, "in_progress"),
        (903, "completed"),
        # Listed for its branch by mistake, these are never the gate's to cancel.
        (904, "in_progress", "ticket/9", "pull_request"),
        (905, "in_progress", "main", "push"),
    )
    _branch_runs(github, kept["branch"], (701, "in_progress"))
    github.pulls["9"]["state"] = "CLOSED"

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert _numbers(github) == ["7", "8"]
    cancelled = _cancelled(github)
    assert list(cancelled) == ["901", "902"]
    assert all(dropped["branch"] in branches for branches in cancelled.values())
    first = github.workflow.calls("gh run cancel")[0]
    assert first == "gh run cancel 901 -R example-org/example-app"
    assert dropped["branch"] not in _scratch_branches(github)
    assert kept["branch"] in _scratch_branches(github)


def test_every_prefix_dropped_at_once_has_its_runs_cancelled(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    old: dict[str, str] = {}
    for index, member in enumerate(_prefixes(github)):
        first = 800 + index * 10
        _branch_runs(
            github,
            member["scratch"]["branch"],
            (first, "queued"),
            (first + 1, "in_progress"),
            (first + 2, "waiting"),
            (first + 3, "completed"),
        )
        old |= {str(first + offset): member["scratch"]["branch"] for offset in range(3)}
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "chore: move main again")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert "main moved outside the merge train" in done.stdout
    cancelled = _cancelled(github)
    assert sorted(cancelled) == sorted(old)
    assert all(old[run] in branches for run, branches in cancelled.items())
    remaining = _scratch_branches(github)
    assert not set(old.values()) & set(remaining)
    assert sorted(remaining) == sorted(m["scratch"]["branch"] for m in _prefixes(github))


def test_a_scratch_cancel_lost_to_an_outage_is_tried_again(github: GitHub) -> None:
    dropped = _dropped_nine(github)
    _branch_runs(github, dropped["branch"], (901, "in_progress"))
    github.cancel_unavailable = [901]

    assert _batch_call(github).returncode == 3
    # The branch outlives the failed cancel, so the next sweep finds its run again.
    assert dropped["branch"] in _scratch_branches(github)
    log = github.workflow.wake_log
    assert not log.exists() or "901" not in log.read_text()
    assert _batch_call(github).returncode == 3

    assert len(github.workflow.calls("gh run cancel 901 ")) == 2
    assert dropped["branch"] not in _scratch_branches(github)


def test_a_run_github_lists_only_after_the_drop_is_still_cancelled(github: GitHub) -> None:
    # GitHub accepts a dispatch before it lists the run: a prefix dropped in that window has
    # its branch deleted while its run is still to come.
    dropped = _dropped_nine(github)
    assert "ci" in dropped["requested"], dropped
    branch = dropped["branch"]

    assert _batch_call(github).returncode == 3
    assert branch not in _scratch_branches(github)
    _branch_runs(github, branch, (901, "queued"))
    assert _batch_call(github).returncode == 3

    assert github.workflow.calls("gh run cancel 901 ")


def test_a_dropped_prefix_is_watched_until_each_dispatched_run_was_seen(github: GitHub) -> None:
    dropped = _dropped_nine(github)
    branch = dropped["branch"]
    assert _batch_call(github).returncode == 3
    assert [entry["branch"] for entry in _waiting_drops(github)] == [branch]
    # A run of each workflow it dispatched shows up: each is cancelled, or had finished.
    runs = [
        (900 + index, "completed" if index else "queued", branch, "workflow_dispatch", workflow)
        for index, workflow in enumerate(dropped["requested"])
    ]
    _branch_runs(github, branch, *runs)
    assert _batch_call(github).returncode == 3
    assert list(_cancelled(github)) == ["900"]
    assert _waiting_drops(github) == []
    assert _batch_call(github).returncode == 3
    assert list(_cancelled(github)) == ["900"]


def test_a_dropped_prefix_whose_runs_never_show_is_given_up_after_the_deadline(
    github: GitHub,
) -> None:
    dropped = _dropped_nine(github)
    branch = dropped["branch"]
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    # Still within its deadline, it stays watched.
    train["dropped"][0]["dropped"] -= 29 * 60
    path.write_text(json.dumps(train))
    assert _batch_call(github).returncode == 3
    assert [entry["branch"] for entry in _waiting_drops(github)] == [branch]
    train = _batch_state(github)
    train["dropped"][0]["dropped"] -= 2 * 60
    path.write_text(json.dumps(train))

    assert _batch_call(github).returncode == 3

    assert _waiting_drops(github) == []
    log = github.workflow.wake_log.read_text()
    notes = [line for line in log.splitlines() if branch in line]
    assert len(notes) == 1 and "gave up" in notes[0], log
    assert _batch_call(github).returncode == 3
    assert github.workflow.wake_log.read_text().count(branch) == 1
    assert github.workflow.calls("gh run cancel") == []


def test_past_the_deadline_the_gate_looks_once_more_before_it_gives_up(github: GitHub) -> None:
    # The next sweep may come long after the drop: by then GitHub lists the late runs.
    dropped = _dropped_nine(github)
    branch = dropped["branch"]
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    train["dropped"][0]["dropped"] -= 31 * 60
    path.write_text(json.dumps(train))
    runs = [
        (900 + index, "completed" if index else "queued", branch, "workflow_dispatch", workflow)
        for index, workflow in enumerate(dropped["requested"])
    ]
    _branch_runs(github, branch, *runs)

    assert _batch_call(github).returncode == 3

    assert list(_cancelled(github)) == ["900"]
    assert _waiting_drops(github) == []
    log = github.workflow.wake_log
    assert not log.exists() or "gave up" not in log.read_text()


def test_a_lasting_error_on_one_dropped_branch_does_not_hold_the_others(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    eight, nine = _scratch(github, "8"), _scratch(github, "9")
    assert eight is not None and nine is not None
    # 8 leaves the line: its prefix and 9's behind it are dropped.
    github.pulls["8"]["state"] = "CLOSED"
    unreadable = f"{_REPO}/actions/runs?branch={eight['branch']}&per_page=100"
    github.api[unreadable] = "HTTP 404: Not Found"
    _branch_runs(github, nine["branch"], (901, "queued"))

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert list(_cancelled(github)) == ["901"]
    assert nine["branch"] not in _scratch_branches(github)
    assert eight["branch"] in _scratch_branches(github)


def test_a_malformed_watch_list_never_changes_the_gates_result(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    train["dropped"] = [
        None,
        {"branch": 5},
        {"branch": "workflow/merge-batch-x", "requested": None},
    ]
    path.write_text(json.dumps(train))

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert _waiting_drops(github) == []


def test_a_dispatch_whose_answer_was_lost_is_watched_too(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    during = json.loads((github.workflow.state / "batch-state-during-dispatch").read_text())
    # Sent but not yet answered: it may have reached GitHub.
    assert during["prefixes"]["members"][0]["scratch"]["dispatching"] == ["ci"]
    assert _scratch(github, "7")["dispatching"] == []
    # 9's dispatch was sent, but the gate stopped before GitHub answered.
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    nine = train["prefixes"]["members"][2]["scratch"]
    nine.update(requested=[], dispatching=["ci"])
    path.write_text(json.dumps(train))
    github.pulls["9"]["state"] = "CLOSED"

    assert _batch_call(github).returncode == 3

    assert [(e["branch"], e["requested"]) for e in _waiting_drops(github)] == [
        (nine["branch"], ["ci"])
    ]


def test_a_rejected_dispatch_is_not_watched(github: GitHub) -> None:
    _batch_setup(github)
    rejected = github.workflow.state / "reject-batch-dispatch"
    rejected.touch()
    assert _batch_call(github).returncode == 1
    scratch = _scratch(github)
    assert scratch is not None and scratch["requested"] == [] and scratch["dispatching"] == []


def test_a_refused_cancel_is_logged_once_and_the_branch_still_goes(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    dropped = _scratch(github, "9")
    assert dropped is not None
    _branch_runs(github, dropped["branch"], (901, "in_progress"), (902, "in_progress"))
    github.refuse_cancel = [901]
    github.pulls["9"]["state"] = "CLOSED"

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert list(_cancelled(github)) == ["901", "902"]
    assert dropped["branch"] not in _scratch_branches(github)
    log = github.workflow.wake_log.read_text()
    notes = [line for line in log.splitlines() if "run 901" in line]
    assert len(notes) == 1 and "Cannot cancel a workflow run that is completed." in notes[0]
    assert "run 902" not in log
    assert _batch_call(github).returncode == 3
    assert github.workflow.wake_log.read_text().count("run 901") == 1


def test_a_later_prefix_that_cannot_start_never_holds_a_ready_front(github: GitHub) -> None:
    _batch_setup(github)
    # 7's own CI tested its merge with this main: it needs no prefix of its own.
    github.ci(github.base, github.head, run_id=7100)
    # The push of 8's prefix is short of locked memory, twice: it waits for the next call.
    short_of_memory(github.workflow, MEMORY_SHORTAGES[1], times=2)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "the prefix of pull request 8 starts later" in done.stdout
    assert _merges(github) == ["7"]


def test_after_a_single_merge_the_next_prefix_waits_for_a_job_main_runs_itself(
    github: GitHub,
) -> None:
    _batch_setup(github)
    # 7 merges by its own checks, which skipped a job that main always runs.
    github.ci(github.base, github.head, run_id=7100)
    github.api[f"{_REPO}/actions/runs/7100/jobs?per_page=100"]["jobs"].append(
        {"id": 7103, "name": _OLDER, "status": "completed", "conclusion": "skipped"}
    )
    assert _batch_call(github).returncode == 0
    assert _merges(github) == ["7"]
    assert _numbers(github) == ["8", "9"]
    _finish_batch(github)
    github.workflow.git("checkout", "ticket/8")
    done = _batch_call(github, "8")
    # The next squash would cancel main's own run of that job before its result is known.
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"merging stopped: main: pending, {_OLDER}" in done.stderr
    assert _merges(github) == ["7"]


def test_a_member_whose_prefix_failed_is_blocked_and_the_ones_behind_are_rebuilt(
    github: GitHub,
) -> None:
    _batch_setup(github, count=4)
    assert _batch_call(github).returncode == 3
    before = {member["number"]: member for member in _prefixes(github)}
    _finish_prefix(github, "7")
    _finish_prefix(github, "8", conclusion="failure", run_id=1100)
    # The prefix behind it holds its change and fails with it.
    _finish_prefix(github, "9", conclusion="failure", run_id=2100)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "merged in this call: 7" in done.stdout
    assert (
        "ci run 1100 of pull request 8's prefix failed (CI result) on main and the members "
        "ahead of it; it is blocked as a failed check"
    ) in done.stdout
    train = _batch_state(github)
    # Only the first failure is the member's own: 9 failed because 8 was in its prefix.
    assert train["blocked"] == {"8": github.pulls["8"]["headRefOid"]}
    assert train["window"] == 2
    rebuilt = _prefixes(github)
    assert [member["number"] for member in rebuilt] == ["9", "10"]
    main = github.workflow.git("rev-parse", "origin/main")
    assert _parents(github, rebuilt[0]["commit"]) == [main, github.pulls["9"]["headRefOid"]]
    assert rebuilt[0]["scratch"]["branch"] != before["9"]["scratch"]["branch"]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 6
    # The failed run's branch stays for a rerun; the dropped prefixes' branches are gone.
    branches = _scratch_branches(github)
    assert before["8"]["scratch"]["branch"] in branches
    assert before["9"]["scratch"]["branch"] not in branches
    assert before["10"]["scratch"]["branch"] not in branches
    github.save()
    assert _train(github)["first"] == "9"
    github.workflow.git("checkout", "ticket/9")
    _finish_batch(github, run_id=5000)
    done = _batch_call(github, "9")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "9", "10"]
    train = _batch_state(github)
    assert train["blocked"] == {"8": github.pulls["8"]["headRefOid"]}
    assert train["window"] == 4


def test_the_window_halves_on_each_failure_and_grows_by_one_per_merge(github: GitHub) -> None:
    _batch_setup(github, count=4)
    assert _batch_call(github).returncode == 3
    _finish_prefix(github, "7", conclusion="failure")
    assert _batch_call(github).returncode == 1
    assert _batch_state(github)["window"] == 2
    assert _numbers(github) == ["8", "9"]
    github.workflow.git("checkout", "ticket/8")
    _finish_prefix(github, "8", conclusion="failure", run_id=1100)
    assert _batch_call(github, "8").returncode == 1
    assert _batch_state(github)["window"] == 1
    # A window of one still tests a stale first member alone, on a scratch branch.
    assert _numbers(github) == ["9"]
    github.workflow.git("checkout", "ticket/9")
    _finish_prefix(github, "9", run_id=2100)
    done = _batch_call(github, "9")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _batch_state(github)["window"] == 2
    assert _merges(github) == ["9"]


def test_a_batch_takes_its_members_by_priority(github: GitHub) -> None:
    _batch_setup(github)
    github.pulls["9"]["labels"] = [{"name": "priority: high"}]
    github.pulls["7"]["labels"] = [{"name": "priority: low"}]
    github.workflow.git("checkout", "ticket/9")

    done = _batch_call(github, "9")

    assert done.returncode == 3, done.stdout + done.stderr
    assert _numbers(github) == ["9", "8", "7"]
    _finish_batch(github)
    done = _batch_call(github, "9")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["9", "8", "7"]


def test_a_priority_change_drops_the_prefixes_served_out_of_order(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    kept = _prefixes(github)[0]
    github.pulls["8"]["labels"] = [{"name": "priority: low"}]
    assert _batch_call(github).returncode == 3
    # 9 is now served before 8: the prefix of 8 is tested again behind 9.
    assert _numbers(github) == ["7", "9", "8"]
    assert _prefixes(github)[0] == kept


def test_a_member_conflicting_with_the_batch_waits_for_its_own_turn(github: GitHub) -> None:
    _batch_setup(github, conflict=True)
    assert _batch_call(github).returncode == 3
    assert _numbers(github) == ["7", "8"]
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _batch_state(github)["waiting"] == ["9"]
    assert github.pulls["9"]["state"] == "OPEN"
    assert _merges(github) == ["7", "8"]


def test_main_moved_from_outside_invalidates_every_prefix(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    previous = _prefixes(github)
    _finish_batch(github)
    workflow = github.workflow
    workflow.git("checkout", "main")
    (workflow.repo / "new-main.txt").write_text("a concurrent push\n")
    workflow.git("add", "new-main.txt")
    workflow.git("commit", "-m", "fix: concurrent main")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert _batch_state(github)["prefixes"]["base"] == github.base
    current = _prefixes(github)
    assert [m["number"] for m in current] == ["7", "8", "9"]
    for old, new in zip(previous, current, strict=True):
        assert new["commit"] != old["commit"]
        assert new["scratch"]["branch"] != old["scratch"]["branch"]
    assert github.workflow.calls("gh pr merge") == []
    _finish_batch(github, run_id=200)
    assert _batch_call(github).returncode == 0
    assert len(workflow.calls("gh workflow run ci.yml")) == 6


def test_batch_completion_changes_the_train_wakeup_once(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    github.save()
    before = _train(github)["turn"]
    # Only the first member's prefix decides its turn.
    _finish_prefix(github, "8", run_id=1100)
    github.save()
    assert _train(github)["turn"] == before
    _finish_prefix(github, "7")
    github.save()
    after = _train(github)["turn"]
    assert after > before
    assert _train(github)["turn"] == after


def test_train_status_shows_each_members_prefix_and_its_state(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    members = _prefixes(github)
    _finish_prefix(github, "7")
    _finish_prefix(github, "8", conclusion="failure", run_id=1100)
    github.save()
    status = _train(github)
    assert status["window"] == 4
    assert [
        (prefix["number"], prefix["branch"], prefix["state"]) for prefix in status["prefixes"]
    ] == [
        ("7", members[0]["scratch"]["branch"], "passed"),
        ("8", members[1]["scratch"]["branch"], "failed"),
        ("9", members[2]["scratch"]["branch"], "starting"),
    ]
    assert {run["workflow"]: run["id"] for run in status["prefixes"][1]["runs"]} == {
        "ci": 1100,
        "e2e": 1110,
        "images": members[1]["scratch"]["image_source"]["run"]["id"],
    }
    assert status["prefixes"][1]["image_source"]["run"] == members[1]["scratch"]["image_source"]["run"]["id"]


def test_the_line_never_tests_more_members_than_its_ceiling(github: GitHub) -> None:
    _batch_setup(github, count=5)
    assert _batch_call(github).returncode == 3
    assert _numbers(github) == ["7", "8", "9", "10"]
    _finish_batch(github)
    assert _batch_call(github).returncode == 0
    assert _batch_state(github)["waiting"] == ["11"]


@pytest.mark.parametrize("state", ["missing", "pending", "red", "other-tree"])
def test_batch_images_must_pass_on_the_scratch_tree(github: GitHub, state: str) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    assert scratch is not None
    # Later prefixes reuse 7's image proof while testing their own CI and browser suites.
    assert [m["scratch"]["workflows"] for m in _prefixes(github)] == [
        ["ci", "images", "e2e"], ["ci", "e2e"], ["ci", "e2e"]
    ]
    _finish_batch(github)
    image_path = (
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    if state == "missing":
        github.api.pop(image_path)
    elif state == "pending":
        github.api[image_path]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    elif state == "red":
        github.api[image_path]["workflow_runs"][0]["conclusion"] = "failure"
    else:
        github.api[f"{_REPO}/check-runs/111/annotations?per_page=100"][0]["message"] = "old-tree"
    done = _batch_call(github)
    assert done.returncode == (1 if state in ("other-tree", "red") else 3), (
        done.stdout + done.stderr
    )
    assert github.workflow.calls("gh pr merge") == []
    if state == "other-tree":
        # The same evidence would refuse every later call: the prefix is retested instead.
        assert (
            "merge waiting: pull request 7's prefix images did not prove its tested files"
            in done.stderr
        )
        retested = _scratch(github)
        assert retested is not None and retested["branch"] != scratch["branch"]
        assert retested["head"] == scratch["head"]
        assert _batch_call(github).returncode == 3
        assert retested["branch"] in _scratch_branches(github)
        assert scratch["branch"] not in _scratch_branches(github)
    if state != "red":
        _finish_batch(github)
        done = _batch_call(github)
        assert done.returncode == 0, done.stdout + done.stderr
        receipt = json.loads(
            (github.workflow.repo / ".local/workflow/main-tree-green.json").read_text()
        )
        assert receipt["images_run"] == 110 and receipt["images_complete"] is True


def test_neutral_prefixes_share_one_image_run_and_main_keeps_its_proof(github: GitHub) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    members = _prefixes(github)
    assert len(github.workflow.calls("gh workflow run images.yml")) == 1
    for index, number in enumerate(("7", "8", "9")):
        _finish_prefix(github, number, run_id=100 + 1000 * index)
        done = _batch_call(github, number)
        assert done.returncode == 0, done.stdout + done.stderr
        assert _merges(github) == ["7", "8", "9"][:index + 1]
        receipt = _receipt(github)
        assert receipt["images_run"] == 110 and receipt["images_complete"]
        assert receipt["images_tree"] == members[0]["tree"]
        assert receipt["images_differing_files"] == [f"change-{n}.txt" for n in range(8, 8 + index)]
        message = github.workflow.git("show", "-s", "--format=%B", "origin/main")
        proof = json.loads(next(line.removeprefix("Workflow-Proof: ")
                                for line in message.splitlines() if line.startswith("Workflow-Proof: ")))
        assert next(s["run"] for s in proof["sources"] if s["workflow"] == "images") == 110
        github.save()
        status = github.workflow.run_script("main-status")
        assert status.stdout.startswith("main: green"), status.stdout + status.stderr
        assert "Docker images run 110" in status.stdout
    assert len(github.workflow.calls("gh workflow run images.yml")) == 1


@pytest.mark.parametrize("initial", ["missing", "pending", "skipped", "delayed-notes"])
def test_neutral_prefix_after_an_own_check_merge_waits_for_main_images(
    github: GitHub, initial: str,
) -> None:
    _batch_setup(github, images=True)
    # The front merges by its own checks, without a scratch image run.
    github.ci(github.base, github.head, run_id=7000)
    _images(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7"]
    main = github.workflow.git("rev-parse", "origin/main")
    # No scratch dispatch can test images now: their inputs already match main.
    assert not github.workflow.calls("gh workflow run images.yml")
    _finish_batch(github, run_id=1100)
    if initial == "delayed-notes":
        # GitHub already lists a completed run, but its planner annotations arrive later.
        _main_image_run(github, 8100, main)
        github.api[f"{_REPO}/actions/runs/8100/jobs?per_page=100"]["jobs"][1]["name"] = "Docker images: image tests"
        github.api[f"{_REPO}/check-runs/8101/annotations?per_page=100"] = []
    elif initial != "missing":
        _main_image_run(github, 8100, main, status="in_progress", conclusion=None)
        if initial == "skipped":
            github.api[f"{_REPO}/actions/workflows/images.yml/runs?branch=main&per_page=100&page=1"]["workflow_runs"][0].update(status="completed", conclusion="success")
            github.api[f"{_REPO}/actions/runs/8100/jobs?per_page=100"]["jobs"][1].update(status="completed", conclusion="skipped")
    done = _batch_call(github, "8")
    assert done.returncode in (1, 3), done.stdout + done.stderr
    assert _merges(github) == ["7"]
    assert not github.workflow.calls("gh workflow run images.yml")
    github.save()
    status = _train(github)
    assert next(p for p in status["prefixes"] if p["number"] == "8")["state"] != "passed"
    before = status["turn"]
    run_id = 8100 if initial == "delayed-notes" else 8200
    if initial != "delayed-notes":
        _main_image_run(github, run_id, main)
        github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"]["jobs"][1]["name"] = "Docker images: image tests"
    github.api[f"{_REPO}/check-runs/{run_id + 1}/annotations?per_page=100"] = [
        {"title": "Files tested", "message": github.workflow.git("rev-parse", f"{main}^{{tree}}")}
    ]
    github.save()
    assert _train(github)["turn"] > before
    assert _scratch(github, "8")["image_source"]["run"]["id"] == run_id
    done = _batch_call(github, "8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert _receipt(github)["images_run"] == run_id
    assert not github.workflow.calls("gh workflow run images.yml")


def test_existing_image_receipts_still_supply_neutral_prefixes(github: GitHub) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    _finish_prefix(github, "7")
    assert _batch_call(github).returncode == 0
    folder = github.workflow.repo / ".local/workflow"
    receipt = _receipt(github)
    assert receipt["images_run"] == 110 and receipt["images_complete"]
    for key in ("images_source", "images_tree", "images_differing_files"):
        receipt.pop(key, None)
    (folder / "main-tree-green.json").write_text(json.dumps(receipt) + "\n")
    # A new window reads the receipt written before image source metadata existed.
    state = _batch_state(github)
    state["prefixes"] = None
    (folder / "merge-train.json").write_text(json.dumps(state) + "\n")
    done = _batch_call(github, "8")
    assert done.returncode == 3, done.stdout + done.stderr
    assert len(github.workflow.calls("gh workflow run images.yml")) == 1
    _finish_batch(github, run_id=4100)
    done = _batch_call(github, "8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert _receipt(github)["images_run"] == 110


def test_a_prefix_can_introduce_the_image_workflow_before_main_has_it(github: GitHub) -> None:
    workflow = github.workflow
    path = ".github/workflows/images.yml"
    images = workflow.repo / path
    definition = images.read_text()
    workflow.git("checkout", "main")
    images.unlink()
    workflow.git("add", path)
    workflow.git("commit", "-m", "chore: start without an image workflow")
    workflow.git("push", "origin", "main")
    github.base = workflow.git("rev-parse", "HEAD")
    github.main()
    workflow.git("checkout", "ticket/seven")
    workflow.git("merge", "--no-edit", "main")
    images.parent.mkdir(parents=True, exist_ok=True)
    images.write_text(definition)
    workflow.git("add", path)
    workflow.git("commit", "-m", "ci: introduce the image workflow")
    _batch_setup(github, count=2)
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?branch=main&per_page=100&page=1"] = {"workflow_runs": []}

    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert not github.workflow.calls("gh workflow run images.yml")
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8"]
    assert workflow.git("show", f"origin/main:{path}") == definition.strip()


def _skipped_main_image_push(github: GitHub, run_id: int, source: int | None = None) -> None:
    """A neutral main push whose image tests skip, optionally inheriting another run."""
    workflow = github.workflow
    previous = github.base
    workflow.git("checkout", "main")
    filename = f"main-neutral-{run_id}.txt"
    (workflow.repo / filename).write_text("image-neutral change\n")
    workflow.git("add", filename)
    tree = workflow.git("write-tree")
    message = "chore: neutral main push"
    if source is not None:
        message += "\n\nWorkflow-Proof: " + json.dumps({
            "base": previous, "tree": tree,
            "sources": [{"workflow": "images", "run": source,
                         "jobs": ["Docker images: image tests"]}],
        })
    workflow.git("commit", "-m", message)
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    workflow.git("checkout", "ticket/seven")
    github.main()
    _main_image_run(github, run_id, github.base)
    github.api[f"{_REPO}/actions/runs/{run_id}/jobs?per_page=100"]["jobs"][1].update(
        name="Docker images: image tests", conclusion="skipped",
    )
    github.api[f"{_REPO}/check-runs/{run_id + 1}/annotations?per_page=100"] = [
        {"title": "Files tested", "message": tree},
    ]


@pytest.mark.parametrize("kind", ["inherited", "neutral-push", "multi-hop", "scheduled"])
def test_neutral_line_after_main_skipped_or_inherited_its_image_tests(
    github: GitHub, kind: str,
) -> None:
    _batch_setup(github)
    source = 8_000_060
    if kind == "scheduled":
        github.api[f"{_REPO}/actions/runs/{source}"]["event"] = "schedule"
    _skipped_main_image_push(github, 8_000_070,
                            source if kind in ("inherited", "multi-hop") else None)
    if kind == "multi-hop":
        _skipped_main_image_push(github, 8_000_080, 8_000_070)
    # Main's latest image tests skipped, exactly as after a neutral or inherited push.
    github.api[f"{_REPO}/actions/runs/{8_000_080 if kind == 'multi-hop' else 8_000_070}/jobs?per_page=100"]["jobs"][1]["conclusion"] = "skipped"
    github.save()
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    for _ in range(4):
        _batch_call(github, next((n for n in ("7", "8", "9")
                                 if github.pulls[n]["state"] != "MERGED"), "9"))
    assert _merges(github) == ["7", "8", "9"]
    assert not github.workflow.calls("gh workflow run images.yml")
    assert _receipt(github)["images_run"] == source
    assert "main-neutral-8000070.txt" in _receipt(github)["images_differing_files"]


@pytest.mark.parametrize("state", ["failure", "pending"])
def test_an_inherited_main_image_source_must_really_pass(github: GitHub, state: str) -> None:
    _batch_setup(github)
    _skipped_main_image_push(github, 8_000_070, 8_000_060)
    run = github.api[f"{_REPO}/actions/runs/8000060"]
    run.update(status="in_progress" if state == "pending" else "completed",
               conclusion=None if state == "pending" else "failure")
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode in (1, 3), done.stdout + done.stderr
    assert not _merges(github)
    github.api[f"{_REPO}/actions/runs/8000060"].update(status="completed", conclusion="success")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


def test_a_neutral_own_check_merge_carries_mains_last_image_source(github: GitHub) -> None:
    _batch_setup(github)
    github.ci(github.base, github.head, run_id=7000)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7"]
    receipt = _receipt(github)
    assert receipt["images_run"] == 8_000_060 and receipt["images_complete"]
    assert receipt["images_differing_files"] == ["change-7.txt"]
    _finish_batch(github)
    done = _batch_call(github, "8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert _receipt(github)["images_run"] == 8_000_060


def test_main_follows_its_squash_image_proof_after_losing_the_receipt(github: GitHub) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    _finish_prefix(github, "7")
    assert _batch_call(github).returncode == 0
    assert _merges(github) == ["7"]
    folder = github.workflow.repo / ".local/workflow"
    (folder / "main-tree-green.json").unlink()
    github.base = github.workflow.git("rev-parse", "origin/main")
    github.main()
    _main_image_run(github, 8_000_070, github.base)
    github.api[f"{_REPO}/actions/runs/8000070/jobs?per_page=100"]["jobs"][1].update(
        name="Docker images: image tests", conclusion="skipped",
    )
    github.api[f"{_REPO}/check-runs/8000071/annotations?per_page=100"] = [
        {"title": "Files tested", "message": github.workflow.git("rev-parse", f"{github.base}^{{tree}}")},
    ]
    state = _batch_state(github)
    state["prefixes"] = None
    (folder / "merge-train.json").write_text(json.dumps(state))
    assert _batch_call(github, "8").returncode == 3
    _finish_batch(github, run_id=4100)
    done = _batch_call(github, "8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert _receipt(github)["images_run"] == 110
    assert len(github.workflow.calls("gh workflow run images.yml")) == 1


@pytest.mark.parametrize("broken", ["cycle", "workflow", "fork", "tree"])
def test_an_invalid_main_image_chain_cannot_supply_a_proof(github: GitHub, broken: str) -> None:
    _batch_setup(github)
    _skipped_main_image_push(github, 8_000_070,
                            8_000_070 if broken == "cycle" else 8_000_060)
    source = github.api[f"{_REPO}/actions/runs/8000060"]
    if broken == "workflow":
        source["path"] = ".github/workflows/ci.yml"
    elif broken == "fork":
        source["head_repository"] = {"full_name": "example-fork/example-app"}
    elif broken == "tree":
        github.api[f"{_REPO}/check-runs/8000061/annotations?per_page=100"][0]["message"] = "0" * 40
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode in (1, 3), done.stdout + done.stderr
    assert not _merges(github)


def test_a_neutral_prefix_without_any_main_image_result_waits_for_main(github: GitHub) -> None:
    _batch_setup(github)
    github.api[f"{_REPO}/actions/workflows/images.yml/runs?branch=main&per_page=100&page=1"] = {"workflow_runs": []}
    github.api.pop(f"{_REPO}/actions/runs/8000060")
    _skipped_main_image_push(github, 8_000_070)
    assert _batch_call(github).returncode == 3
    assert ["images" in m["scratch"]["workflows"] for m in _prefixes(github)] == [False, False, False]
    _finish_batch(github)
    assert _batch_call(github).returncode == 3
    assert _merges(github) == []
    assert not github.workflow.calls("gh workflow run images.yml")
    # Only an actual passed image run can release this line.
    _main_image_run(github, 8_000_080, github.base, event="schedule")
    github.api[f"{_REPO}/actions/runs/8000080/jobs?per_page=100"]["jobs"][1]["name"] = "Docker images: image tests"
    github.api[f"{_REPO}/check-runs/8000081/annotations?per_page=100"] = [
        {"title": "Files tested", "message": github.workflow.git("rev-parse", f"{github.base}^{{tree}}")},
    ]
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


@pytest.mark.parametrize("lost", ["failed", "missing"])
def test_a_lost_shared_result_after_its_member_merged_waits_for_main(github: GitHub, lost: str) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    source = _scratch(github)
    _finish_prefix(github, "7")
    assert _batch_call(github).returncode == 0
    path = (f"{_REPO}/actions/workflows/images.yml/runs?head_sha={source['head']}"
            "&event=workflow_dispatch&per_page=100")
    if lost == "failed":
        github.api[path]["workflow_runs"][0]["conclusion"] = "failure"
        github.api[f"{_REPO}/actions/runs/110"]["conclusion"] = "failure"
    else:
        github.api[path]["workflow_runs"] = []
        github.api.pop(f"{_REPO}/actions/runs/110")
    _finish_prefix(github, "8", run_id=1100)
    _finish_prefix(github, "9", run_id=2100)
    github.save()
    status = _train(github)
    assert next(prefix for prefix in status["prefixes"] if prefix["number"] == "8")["state"] != "passed"
    done = _batch_call(github, "8")
    assert done.returncode == 3, done.stdout + done.stderr
    assert _merges(github) == ["7"]
    assert "images" not in _scratch(github, "8")["workflows"]
    assert _batch_call(github, "8").returncode == 3
    main = github.workflow.git("rev-parse", "origin/main")
    _main_image_run(github, 8200, main)
    github.api[f"{_REPO}/actions/runs/8200/jobs?per_page=100"]["jobs"][1]["name"] = "Docker images: image tests"
    github.api[f"{_REPO}/check-runs/8201/annotations?per_page=100"] = [
        {"title": "Files tested", "message": github.workflow.git("rev-parse", f"{main}^{{tree}}")}
    ]
    done = _batch_call(github, "8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert _receipt(github)["images_run"] == 8200
    assert len(github.workflow.calls("gh workflow run images.yml")) == 1


def test_an_image_change_after_a_neutral_member_gets_its_own_run(github: GitHub) -> None:
    _batch_setup(github, images=True, images_member=8)
    assert _batch_call(github).returncode == 3
    assert ["images" in m["scratch"]["workflows"] for m in _prefixes(github)] == [False, True, False]
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert _receipt(github)["images_run"] == 1110


def test_a_failed_shared_image_run_releases_the_line_only_after_a_green_rerun(github: GitHub) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls("gh workflow run images.yml")) == 1
    original = _prefixes(github)
    _finish_batch(github)
    path = (f"{_REPO}/actions/workflows/images.yml/runs?head_sha={original[0]['commit']}"
            "&event=workflow_dispatch&per_page=100")
    github.api[path]["workflow_runs"][0]["conclusion"] = "failure"
    assert _batch_call(github).returncode == 1
    assert _merges(github) == []
    _finish_prefix(github, "7", scratch=original[0]["scratch"], tree=original[0]["tree"])
    github.api[path]["workflow_runs"][0]["run_attempt"] = 2
    github.save()
    assert _train(github)["first"] == "7"
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    for _ in range(3):
        _finish_batch(github, run_id=4100)
        number = next((n for n in ("8", "9") if github.pulls[n]["state"] != "MERGED"), None)
        if number is None:
            break
        done = _batch_call(github, number)
        assert done.returncode in (0, 3), done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


@pytest.mark.parametrize("change", ["unapproved-queue-entry", "changed-head"])
def test_a_batch_cannot_merge_a_member_without_approval_of_its_current_head(
    github: GitHub, change: str
) -> None:
    _batch_setup(github)
    if change == "changed-head":
        assert _batch_call(github).returncode == 3
        previous = _prefixes(github)
        _finish_batch(github)
        workflow = github.workflow
        workflow.git("checkout", "ticket/9")
        (workflow.repo / "change-9.txt").write_text("an unreviewed new change\n")
        workflow.git("add", "change-9.txt")
        workflow.git("commit", "-m", "fix: new head for nine")
        head = workflow.git("rev-parse", "HEAD")
        workflow.git("push", "origin", "ticket/9")
        workflow.git("checkout", "ticket/seven")
        github.pulls["9"]["headRefOid"] = head
        github.ci(github.base, head, run_id=90)
    else:
        path = github.workflow.repo / ".local/workflow/merge-train.json"
        train = _batch_state(github)
        train["approved"].pop("9")
        path.write_text(json.dumps(train))
        previous = None
    done = _batch_call(github)
    assert done.returncode == (0 if previous else 3), done.stdout + done.stderr
    if previous is None:
        assert _numbers(github) == ["7", "8"]
        assert github.workflow.calls("gh pr merge") == []
        _finish_batch(github, run_id=200)
        assert _batch_call(github).returncode == 0
    else:
        # The prefixes ahead of the changed member still stand; its own is gone.
        assert "merged in this call: 7, 8" in done.stdout
    assert github.pulls["9"]["state"] == "OPEN"
    assert _merges(github) == ["7", "8"]


def test_rejected_batch_dispatch_can_be_retried(github: GitHub) -> None:
    _batch_setup(github)
    rejected = github.workflow.state / "reject-batch-dispatch"
    rejected.touch()
    done = _batch_call(github)
    assert done.returncode == 1 and "dispatch failed" in done.stderr
    scratch = _scratch(github)
    assert scratch is not None and scratch["requested"] == []
    assert github.workflow.calls("gh pr merge") == []
    rejected.unlink()
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    assert _batch_call(github).returncode == 0
    # The rejected one, then one for each prefix.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 4


def test_restart_recovers_an_accepted_dispatch_before_its_state_was_saved(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    during = json.loads((github.workflow.state / "batch-state-during-dispatch").read_text())
    # The crash window before a request starts cannot claim an unsent dispatch succeeded.
    assert during["prefixes"]["members"][0]["scratch"]["requested"] == []
    _finish_batch(github)
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    path.write_text(json.dumps(during))
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3


@pytest.mark.parametrize("member", [0, 1])
def test_a_different_prefix_tree_returns_remaining_members_to_the_single_path(
    github: GitHub, member: int
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    train = _batch_state(github)
    train["prefixes"]["members"][member]["tree"] = github.workflow.git(
        "rev-parse", f"{github.base}^{{tree}}"
    )
    (github.workflow.repo / ".local/workflow/merge-train.json").write_text(json.dumps(train))
    _finish_batch(github)
    done = _batch_call(github)
    # Once the caller's own pull request merged, the call reports that merge, never a wait.
    assert done.returncode == (2 if member == 0 else 0), done.stdout + done.stderr
    assert _merges(github) == ([] if member == 0 else ["7"])
    if member == 1:
        assert "pull request 7 merged in a batch (merged in this call: 7)" in done.stdout
        assert "still in the line: 8, 9" in done.stdout
    assert _batch_state(github)["prefixes"] is None
    receipt = github.workflow.repo / ".local/workflow/main-tree-green.json"
    # Main inherits only the prefix run that tested exactly its tree: 7's.
    assert receipt.exists() is (member == 1)


@pytest.mark.parametrize("fixes", [False, True])
def test_main_turning_red_during_a_batch_uses_the_existing_single_fix_rule(
    github: GitHub, fixes: bool
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.main("failure")
    github.ci(github.base, github.head, run_id=3000)
    if fixes:
        github.api[f"{_REPO}/actions/runs/3000/jobs?per_page=100"]["jobs"].append(
            {"id": 3003, "name": "CI: web", "status": "completed", "conclusion": "success"}
        )
    done = _batch_call(github)
    assert done.returncode == (0 if fixes else 1), done.stdout + done.stderr
    train = _batch_state(github)
    # The fix merges alone; main inherits green from its run, and the others' prefixes start.
    assert _numbers(github) == (["8", "9"] if fixes else [])
    assert ("7" in train["suspended"]) is not fixes
    github.save()
    assert _train(github)["first"] == "8"
    assert len(github.workflow.calls("gh pr merge")) == int(fixes)


def test_an_image_rerun_can_restore_an_isolated_batch_failure_to_its_place(github: GitHub) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    assert scratch is not None
    _finish_batch(github)
    path = (
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    github.api[path]["workflow_runs"][0]["conclusion"] = "failure"
    assert _batch_call(github).returncode == 1
    github.save()
    assert _train(github)["first"] == "8"
    image = github.api[
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ]["workflow_runs"][0]
    image["run_attempt"] = 2
    github.save()
    assert _train(github)["first"] == "7"
    assert _batch_call(github).returncode == 2  # Refresh its stale green CI in its saved place.


def test_the_sweep_never_deletes_a_branch_that_only_ends_like_the_gate_s(github: GitHub) -> None:
    """`git ls-remote` lists a branch whose name only ends like the gate's pattern too: it is
    someone else's."""
    _batch_setup(github)
    others = ["ticket/x/refs/heads/workflow/merge-batch-" + "0" * 32]
    for branch in others:
        github.workflow.git("push", "--quiet", "origin", f"{github.base}:refs/heads/{branch}")
    assert _batch_call(github).returncode == 3
    _finish_batch(github)

    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    listed = github.workflow.git("ls-remote", "origin").split()
    assert [branch for branch in others if f"refs/heads/{branch}" in listed] == others
    # The gate's own scratch branches are gone.
    assert sorted(_scratch_branches(github)) == sorted(others)


def test_a_batch_saved_before_prefixes_is_dropped(github: GitHub) -> None:
    _batch_setup(github, count=4)
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    train = _batch_state(github)
    branch = "workflow/merge-batch-" + "0" * 32
    github.workflow.git("push", "origin", f"{github.base}:refs/heads/{branch}")
    train["batch"] = {
        "base": github.base,
        "head": github.base,
        "branch": branch,
        "members": [{"number": "7", "head": github.head, "tree": "0" * 40}],
        "cursor": 1,
        "workflows": ["ci"],
        "requested": ["ci"],
        "pushed": True,
    }
    train["probes"] = [["8"]]
    path.write_text(json.dumps(train))
    github.workflow.git("checkout", "ticket/10")
    done = _batch_call(github, "10")
    assert done.returncode == 3, done.stdout + done.stderr
    assert "batch" not in _batch_state(github) and "probes" not in _batch_state(github)
    assert branch not in _scratch_branches(github)
    assert github.workflow.calls("gh pr merge") == []


@pytest.mark.parametrize("moved", [False, True])
def test_a_green_scratch_rerun_restores_the_isolated_members_earlier_place(
    github: GitHub, moved: bool
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    isolated = _scratch(github)
    assert isolated is not None
    _finish_batch(github)
    # Only CI failed for the first member; its whole browser suite passed.
    _finish_prefix(github, "7", conclusion="failure")
    github.e2e(
        isolated["head"], _prefixes(github)[0]["tree"], run_id=110, branch=isolated["branch"]
    )
    assert _batch_call(github).returncode == 1
    github.save()
    assert _train(github)["first"] == "8"
    if moved:
        # Main's files changed: the rerun no longer tests 7 on what main is.
        github.workflow.git("checkout", "main")
        (github.workflow.repo / "new-main.txt").write_text("a concurrent push\n")
        github.workflow.git("add", "new-main.txt")
        github.workflow.git("commit", "-m", "chore: main moved again")
        github.base = github.workflow.git("rev-parse", "HEAD")
        github.workflow.git("push", "origin", "main")
        github.workflow.git("checkout", "ticket/seven")
        github.main()
    path = (
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={isolated['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    github.api[path]["workflow_runs"][0].update(conclusion="success", run_attempt=2)
    for job in github.api[f"{_REPO}/actions/runs/100/jobs?per_page=100"]["jobs"]:
        job["conclusion"] = "success"
    github.save()
    assert _train(github)["first"] == "7"
    done = _batch_call(github)
    # On the same main, the passed rerun is its prefix: it merges without another run.
    assert done.returncode == (2 if moved else 0), done.stdout + done.stderr
    assert _merges(github) == ([] if moved else ["7"])
    # 7, 8 and 9; then 8 and 9 without the failed member. Restoring 7 starts no replacement
    # runs on the old main. If 7 merges, its new main is where 8 and 9 are tested next.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 5
    if not moved:
        base = github.workflow.git("rev-parse", "origin/main")
        done = _batch_call(github, "8")
        assert done.returncode == 3, done.stdout + done.stderr
        assert len(github.workflow.calls("gh workflow run ci.yml")) == 7
        assert _scratch(github, "8")["under"] == github.workflow.git(
            "rev-parse", f"{base}^{{tree}}"
        )


@pytest.mark.parametrize("passed", [False, True], ids=["running", "passed"])
@pytest.mark.parametrize("reconciled", [False, True], ids=["failed-state", "proven-state"])
def test_restoration_keeps_an_existing_prefix_across_retests_and_gate_calls(
    github: GitHub, passed: bool, reconciled: bool
) -> None:
    kept = _restored_before_eight(github, passed=passed, reconciled=reconciled)
    branch = kept["scratch"]["branch"]
    done = _batch_call(github)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "did not prove its tested files" in done.stderr
    assert _scratch(github, "8")["branch"] == branch
    assert branch in _scratch_branches(github)

    # The replacement prefix of 7 starts beside the kept work; a later tick cannot forget it.
    for _ in range(3):
        github.save()
        status = _train(github)
        prefixes = {entry["number"]: entry for entry in status["prefixes"]}
        assert prefixes["8"]["kept"] is True
        assert prefixes["8"]["branch"] == branch
        assert prefixes["7"]["branch"] != branch
        number = status["first"]
        done = _batch_call(github, number)
        if passed:
            assert done.returncode == 0, done.stdout + done.stderr
            break
        assert done.returncode == 3, done.stdout + done.stderr
        assert _scratch(github, "8")["branch"] == branch
        saved = _batch_state(github)
        assert sum(
            member["scratch"] is None or member["scratch"]["pushed"]
            for member in saved["prefixes"]["members"]
        ) <= saved["window"]
    if not passed:
        _finish_prefix(github, "8", run_id=1100)
        github.save()
        assert _train(github)["first"] == "8"
        done = _batch_call(github, "8")
        assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["8"]
    receipt = _receipt(github)
    assert receipt["tree"] == kept["tree"]
    assert receipt["ci_run"] == 1100
    assert receipt["images_run"] == 1110 and receipt["images_complete"] is True
    # The kept branch was dispatched only once, and 7 now tests against the new main.
    assert len([call for call in github.workflow.calls("gh workflow run") if branch in call]) == len(
        kept["scratch"]["workflows"]
    )
    restored = next(member for member in _prefixes(github) if member["number"] == "7")
    base = github.workflow.git("rev-parse", "origin/main")
    assert restored["scratch"]["under"] == github.workflow.git("rev-parse", f"{base}^{{tree}}")
    _finish_prefix(github, "7", run_id=2100)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["8", "7"]


@pytest.mark.parametrize("reconciled", [False, True], ids=["failed-state", "proven-state"])
def test_a_member_joining_during_a_restored_retest_waits_for_the_restored_front(
    github: GitHub, reconciled: bool
) -> None:
    # Read the older state shape, both before and after its green rerun was recognized.
    kept = _restored_before_eight(github, passed=False, reconciled=reconciled, late=True)
    assert _batch_call(github).returncode == 1
    saved = _batch_state(github)
    saved["window"] = 2
    (github.workflow.repo / ".local/workflow/merge-train.json").write_text(json.dumps(saved))
    assert _batch_call(github).returncode == 3
    restored = _scratch(github, "7")
    assert restored and restored["pushed"]

    # 9 joins while 8 is kept and 7's replacement is running. Only the existing 8 may go first.
    github.pulls["9"]["isDraft"] = False
    saved = _batch_state(github)
    saved["window"] = 4
    (github.workflow.repo / ".local/workflow/merge-train.json").write_text(json.dumps(saved))
    done = _batch_call(github, "9")
    assert done.returncode == 3, done.stdout + done.stderr
    assert "9" in _batch_state(github)["waiting"]
    for member in _prefixes(github):
        if member["number"] == "9":
            assert "7" in member["ahead"], member
            assert not member.get("kept"), member
    assert _scratch(github, "8")["branch"] == kept["scratch"]["branch"]

    _finish_prefix(github, "8", run_id=1100)
    done = _batch_call(github, "8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["8"]
    replacement = _scratch(github, "7")
    assert replacement and replacement["pushed"]
    assert replacement["branch"] != restored["branch"]
    base = github.workflow.git("rev-parse", "origin/main")
    assert replacement["under"] == github.workflow.git("rev-parse", f"{base}^{{tree}}")

    for member in _prefixes(github):
        if member["number"] == "9":
            assert "7" in member["ahead"], member
            _finish_prefix(github, "9", run_id=4100)
    for _ in range(3):
        for number in ("7", "9"):
            done = _batch_call(github, number)
            assert done.returncode == 3, done.stdout + done.stderr
        assert _merges(github) == ["8"]
        assert _scratch(github, "7")["branch"] == replacement["branch"]
    _finish_prefix(github, "7", run_id=3100)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github)[:2] == ["8", "7"]


@pytest.mark.parametrize("change", ["head", "main", "draft"])
def test_a_kept_prefix_is_invalidated_when_its_inputs_change(github: GitHub, change: str) -> None:
    kept = _restored_before_eight(github, passed=False)
    assert _batch_call(github).returncode == 1
    if change == "head":
        github.pulls["8"]["headRefOid"] = github.pulls["9"]["headRefOid"]
    elif change == "draft":
        github.pulls["8"]["isDraft"] = True
    else:
        workflow = github.workflow
        workflow.git("checkout", "main")
        workflow.git("commit", "--allow-empty", "-m", "chore: main moved")
        github.base = workflow.git("rev-parse", "HEAD")
        workflow.git("push", "origin", "main")
        workflow.git("checkout", "ticket/seven")
        github.main()
    done = _batch_call(github)
    assert done.returncode in (1, 2, 3), done.stdout + done.stderr
    assert kept["scratch"]["branch"] not in _scratch_branches(github)
    assert _merges(github) == []


def test_replacing_a_short_prefix_cancels_only_its_own_live_runs_once(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    _finish_batch(github)
    github.api[f"{_REPO}/check-runs/101/annotations?per_page=100"][1]["message"] = SELECTED
    _branch_runs(
        github, scratch["branch"],
        (901, "queued"), (902, "in_progress"), (903, "completed"),
        (904, "in_progress", "main", "push"),
        (905, "in_progress", "ticket/7", "push"),
    )
    done = _batch_call(github)
    assert done.returncode == 1, done.stdout + done.stderr
    assert list(_cancelled(github)) == ["901", "902"]
    assert all(scratch["branch"] in branches for branches in _cancelled(github).values())
    assert done.stdout.count("cancel requested for run 901") == 1
    assert done.stdout.count("cancel requested for run 902") == 1
    assert scratch["branch"] not in _scratch_branches(github)
    done = _batch_call(github)
    assert "cancel requested for run 901" not in done.stdout
    assert len(github.workflow.calls("gh run cancel")) == 2


def test_an_old_scratch_name_is_cancelled_but_other_branches_are_untouched(github: GitHub) -> None:
    _batch_setup(github)
    branch = "workflow/merge-batch-legacy"
    other = "ticket/example-app"
    for name in (branch, other):
        github.workflow.git("push", "origin", f"{github.base}:refs/heads/{name}")
        _branch_runs(github, name, (901 if name == branch else 902, "in_progress"))
    _branch_runs(github, "main", (903, "in_progress"))
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert list(_cancelled(github)) == ["901"]
    assert branch not in _scratch_branches(github)
    assert github.workflow.git("ls-remote", "origin", f"refs/heads/{other}")


def test_planning_a_restored_prefix_does_not_rerun_its_failed_jobs(github: GitHub) -> None:
    _restored_before_eight(github, passed=False)
    assert _batch_call(github).returncode == 1
    # Room for the restored member's retest, while the kept prefix still runs.
    github.pulls["9"]["state"] = "CLOSED"
    assert _batch_call(github).returncode == 3
    _finish_prefix(github, "7", run_id=200)
    batch_dependencies_red(github, annotation=UNREACHABLE, run_id=200)
    advisory_service_answers(github.workflow, answers=True)
    before = github.workflow.calls("gh run rerun")
    _train(github)
    assert github.workflow.calls("gh run rerun") == before


def test_a_new_priority_front_still_invalidates_the_kept_line(github: GitHub) -> None:
    kept = _restored_before_eight(github, passed=False, count=4)
    assert _batch_call(github).returncode == 1
    github.pulls["10"]["labels"] = [{"name": "priority: blocker"}]
    github.workflow.git("checkout", "ticket/10")
    done = _batch_call(github, "10")
    assert done.returncode in (2, 3), done.stdout + done.stderr
    assert "waiting for the members ahead of the next prefix" not in done.stdout
    assert kept["scratch"]["branch"] not in _scratch_branches(github)
    assert _merges(github) == []


@pytest.mark.parametrize("descendant_passed", [False, True], ids=["pending", "passed"])
def test_a_failed_kept_root_frees_capacity_for_the_restored_prefix(
    github: GitHub, descendant_passed: bool
) -> None:
    _restored_before_eight(github, passed=False)
    assert _batch_call(github).returncode == 1
    for _ in range(2):
        assert _batch_call(github).returncode == 3
        assert _scratch(github, "7")["pushed"] is False
    _finish_prefix(github, "8", conclusion="failure", run_id=1100)
    if descendant_passed:
        _finish_prefix(github, "9", run_id=2100)
    github.save()
    # Status reads keep nominating the failed independent root, not its dependent result
    # or the restored member whose scratch branch cannot start until a slot is released.
    for _ in range(3):
        assert _train(github)["first"] == "8"
    done = _batch_call(github, "8")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "prefix failed" in done.stderr
    assert _batch_state(github)["blocked"]["8"] == github.pulls["8"]["headRefOid"]
    restored = _scratch(github, "7")
    assert restored["pushed"] is True
    assert _numbers(github) == ["7"]
    for _ in range(2):
        assert _batch_call(github).returncode == 3
        assert _scratch(github, "7")["branch"] == restored["branch"]
    _finish_prefix(github, "7", run_id=3100)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7"]


def test_the_acting_reviewer_keeps_its_availability_through_batch_squashes(github: GitHub) -> None:
    _batch_setup(github)
    notes = github.workflow.notes(7)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "review-7.owner-blocked").touch()
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


def test_the_scratch_branch_changing_drops_the_batch_for_a_retest(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    assert scratch is not None
    _finish_batch(github)
    github.workflow.git(
        "push", "--force", "origin", f"{github.base}:refs/heads/{scratch['branch']}"
    )
    done = _batch_call(github)
    assert done.returncode == 1
    assert "merge waiting: the gate's scratch branch changed; retesting next call" in done.stderr
    retested = _scratch(github)
    assert retested is not None and retested["branch"] != scratch["branch"]
    # The prefixes behind it are built on the same commit and stand.
    assert _numbers(github) == ["7", "8", "9"]
    assert scratch["branch"] not in _scratch_branches(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github, run_id=200)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    # A scratch run that proved the tree forgets the shortfall: a later one is a wait again.
    assert _batch_state(github)["retest"] is None


def test_a_different_shortfall_on_the_same_tree_is_still_a_wait(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    scratch = _scratch(github)
    assert scratch is not None
    _finish_batch(github)
    github.workflow.git(
        "push", "--force", "origin", f"{github.base}:refs/heads/{scratch['branch']}"
    )
    assert "merge waiting: the gate's scratch branch changed" in _batch_call(github).stderr
    assert _batch_call(github).returncode == 3
    retested = _scratch(github)
    assert retested is not None
    _finish_batch(github, run_id=200)
    github.e2e(
        retested["head"],
        _prefixes(github)[0]["tree"],
        suite="selected: 3 of 48 spec files",
        run_id=210,
        branch=retested["branch"],
    )

    done = _batch_call(github)

    # Not the same shortfall twice in a row: one more retest, not a refusal.
    assert done.returncode == 1
    assert (
        "merge waiting: pull request 7's prefix e2e did not prove the whole browser suite"
        in done.stderr
    )
    assert github.workflow.calls("gh pr merge") == []


def test_a_member_no_longer_ready_after_the_callers_squash_reports_that_merge(
    github: GitHub,
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_merge = {"7": {"8": {"isDraft": True}}}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "prefix member 8 is no longer ready" in done.stdout
    assert "pull request 7 merged in a batch (merged in this call: 7)" in done.stdout
    assert _merges(github) == ["7"]
    assert _batch_state(github)["prefixes"] is None


def test_unknown_mergeability_right_after_a_squash_keeps_the_batch(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    unknown = {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}
    github.after_merge = {"7": {"8": unknown, "9": unknown}, "8": {"9": unknown}}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 3


def test_a_conflict_shown_after_a_squash_still_stops_the_batch(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_merge = {"7": {"8": {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}}}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "prefix member 8 is no longer ready" in done.stdout
    assert _merges(github) == ["7"]


def test_members_merged_by_another_call_are_noted_for_their_own_sessions(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    assert _batch_call(github).returncode == 0
    folder = github.workflow.repo / ".local/workflow"
    squashes = github.workflow.git("log", "--format=%H", "-3", "origin/main").splitlines()
    for number, squash in (("7", squashes[2]), ("8", squashes[1]), ("9", squashes[0])):
        note = json.loads((folder / f"batch-merged-{number}").read_text())
        assert note == {"by": "7", "head": github.pulls[number]["headRefOid"], "squash": squash}
    github.workflow.git("checkout", "ticket/8")
    done = _batch_call(github, "8")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already merged in a batch, through the gate call of pull request 7" in done.stdout
    assert _merges(github) == ["7", "8", "9"]


def test_unknown_mergeability_keeps_a_member_out_of_a_new_batch(github: GitHub) -> None:
    _batch_setup(github)
    github.pulls["8"].update(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")
    done = _batch_call(github)
    # Only a squash this gate just made explains UNKNOWN; a new line waits for GitHub.
    assert done.returncode == 2, done.stdout + done.stderr
    assert _batch_state(github)["prefixes"] is None
    assert github.workflow.calls("gh workflow run") == []


def test_an_error_after_the_callers_squash_still_reports_and_keeps_that_merge(
    github: GitHub,
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_merge = {"7": {"8": {"fail_view": True}}}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pull request 7 merged in a batch (merged in this call: 7); the merge train stopped" in (
        done.stdout
    )
    note = json.loads((github.workflow.repo / ".local/workflow/batch-merged-7").read_text())
    assert note["by"] == "7"
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already merged in a batch, through an earlier gate call of its own" in done.stdout
    assert _merges(github) == ["7"]


def test_an_isolated_failure_is_reported_to_its_own_caller(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github, conclusion="failure")
    done = _batch_call(github)
    # Its own PR checks stay green, so a wait would never be woken: it is a finding now.
    assert done.returncode == 1, done.stdout + done.stderr
    assert (
        "ci run 100 of pull request 7's prefix failed (CI result) on main and the members ahead "
        "of it; treat it as a failed check"
    ) in done.stderr
    assert _batch_state(github)["blocked"] == {"7": github.head}
    # The prefixes behind it are built again without it, at once.
    assert _numbers(github) == ["8", "9"]
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 5
    done = _batch_call(github)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "the ci run of this pull request's prefix" in done.stderr


@pytest.mark.parametrize("departure", ["closed", "new-head"])
def test_a_departed_isolated_failure_no_longer_keeps_its_branch(
    github: GitHub, departure: str
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    isolated = _scratch(github)
    assert isolated is not None
    _finish_batch(github, conclusion="failure")
    assert _batch_call(github).returncode == 1
    assert isolated["branch"] in _scratch_branches(github)
    if departure == "closed":
        github.pulls["7"]["state"] = "CLOSED"
    else:
        github.pulls["7"]["headRefOid"] = github.base  # Any other head: its fix.
    github.workflow.git("checkout", "ticket/8")
    assert _batch_call(github, "8").returncode == 3
    train = _batch_state(github)
    assert train["batch_failed"] == {}
    assert isolated["branch"] not in _scratch_branches(github)


def test_a_failed_fetch_after_the_callers_squash_still_records_that_merge(
    github: GitHub,
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_main = {"7": "unreachable"}
    done = _batch_call(github)
    origin = github.workflow.root / "origin.git"
    (github.workflow.root / "origin.git.away").rename(origin)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pull request 7 merged in a batch (merged in this call: 7); the merge train stopped" in (
        done.stdout
    )
    note = json.loads((github.workflow.repo / ".local/workflow/batch-merged-7").read_text())
    assert note == {"by": "7", "head": github.head}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already merged in a batch, through an earlier gate call of its own" in done.stdout


@pytest.mark.parametrize("group,workflow_name,path", [
    ("images", "images", "docker/change.txt"),
    ("browser_whole", "e2e", "web/src/example.ts"),
    ("server_whole", "ci", "src/example_app/reader.py"),
])
@pytest.mark.parametrize("listed", [False, True])
def test_prefix_only_first_member_uses_scratch_proof_and_main_inherits(
    github: GitHub, group: str, workflow_name: str, path: str, listed: bool,
) -> None:
    tree = _changes(github, path)
    _prefix_only(github, [group] if listed else [])
    if group == "images" and not listed:
        _images(github)
    # Even an own whole-suite result cannot replace a configured group's scratch run.
    done = _batch_call(github)
    if not listed:
        assert done.returncode == 0, done.stdout + done.stderr
        assert not github.workflow.calls("gh workflow run")
        return
    assert done.returncode == 3, done.stdout + done.stderr
    scratch = _suite(github)
    assert scratch["workflows"] == [workflow_name]
    _finish_prefix(github, "7", scratch=scratch, tree=tree, run_id=800)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    squash = github.workflow.git("rev-parse", "origin/main")
    message = github.workflow.git("show", "-s", "--format=%B", squash)
    proof = json.loads(next(line.removeprefix("Workflow-Proof: ")
                            for line in message.splitlines() if line.startswith("Workflow-Proof: ")))
    assert {"workflow": workflow_name, "run": 800} in [
        {"workflow": source["workflow"], "run": source["run"]} for source in proof["sources"]
    ]
    assert proof["tree"] == tree
    receipt = json.loads((github.workflow.repo / ".local/workflow/main-tree-green.json").read_text())
    if group == "images":
        assert receipt["images_run"] == 800 and receipt["images_complete"]
        assert not github.workflow.calls(f"gh api --paginate {_REPO}/actions/workflows/images.yml/runs?head_sha={github.head}&event=pull_request")
    if group == "server_whole":
        assert "CI: server tests" in receipt["ci_passed"]


@pytest.mark.parametrize("groups", [[], ["images"], ["browser_whole"], ["server_whole"]])
def test_prefix_only_path_filters_need_no_group_run(github: GitHub, groups: list[str]) -> None:
    _prefix_only(github, groups)
    github.ci(github.base, github.head, server="none")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not github.workflow.calls("gh workflow run")


@pytest.mark.parametrize("group", ["images", "browser_whole", "server_whole"])
def test_prefix_only_line_waits_on_its_prefix_and_records_its_groups(github: GitHub, group: str) -> None:
    _batch_setup(github, count=2, images=group == "images", selected=True)
    _prefix_only(github, [group])
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert _numbers(github) == ["7", "8"]
    assert all(member["scratch"] for member in _prefixes(github))
    status = github.workflow.run_script("merge-when-current", "--train-status")
    assert json.loads(status.stdout)["prefix_only"] == [group]
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert all(pull["state"] == "MERGED" for pull in github.pulls.values())


@pytest.mark.parametrize("conclusion", ["failure", "in_progress", "wrong tree"])
def test_deferred_images_require_passing_proof_on_the_tested_tree(
    github: GitHub, conclusion: str,
) -> None:
    tree = _changes(github, "docker/change.txt")
    _prefix_only(github, ["images"])
    assert _batch_call(github).returncode == 3
    scratch = _suite(github)
    _finish_prefix(github, "7", scratch=scratch, tree=tree, run_id=800)
    if conclusion == "wrong tree":
        github.api[f"{_REPO}/check-runs/801/annotations?per_page=100"][0]["message"] = "0" * 40
    elif conclusion == "in_progress":
        github.api[f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}&event=workflow_dispatch&per_page=100"]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    else:
        github.api[f"{_REPO}/actions/runs/800/jobs?per_page=100"]["jobs"][1]["conclusion"] = "failure"
    done = _batch_call(github)
    assert done.returncode != 0
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("deferred", [False, True])
def test_an_image_prefix_cannot_pass_with_only_its_planner(github: GitHub, deferred: bool) -> None:
    _batch_setup(github, count=2, images=True, selected=True)
    _prefix_only(github, ["images"] if deferred else [])
    if not deferred:
        _images(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    scratch = _scratch(github)
    assert scratch is not None
    image_run = github.api[
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0]
    for job in github.api[f"{_REPO}/actions/runs/{image_run['id']}/jobs?per_page=100"]["jobs"]:
        if job["name"] != "Docker images: what to run":
            job["conclusion"] = "skipped"
    done = _batch_call(github)
    assert done.returncode != 0, done.stdout + done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_scratch_run_finishing_or_rerunning_rejudges_red_main_fix_priority(github: GitHub) -> None:
    tree = _changes(github, "docker/change.txt")
    _prefix_only(github, ["images"])
    _images(github, pull=False, conclusion="failure", release_conclusion="success")
    assert _batch_call(github).returncode == 3
    scratch = _suite(github)
    assert _batch_state(github)["proofs"]["7"][1] is False
    _finish_prefix(github, "7", scratch=scratch, tree=tree, run_id=800)
    github.save()
    assert _train(github)["priorities"]["7"] == "blocker"
    assert _batch_state(github)["proofs"]["7"][1] is True
    run = github.api[
        f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0]
    run.update(run_attempt=2, status="in_progress", conclusion=None)
    github.save()
    assert _train(github)["priorities"]["7"] == "medium"
    assert _batch_state(github)["proofs"]["7"][1] is False
    run.update(status="completed", conclusion="success")
    github.save()
    assert _train(github)["priorities"]["7"] == "blocker"
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "own",
    [
        "selected: 3 of 48 spec files",
        # Before the planner noted the kind of run, nothing says every spec ran.
        None,
        # The whole suite, but of another tree than the merge CI tested.
        "whole on another tree",
    ],
)
def test_a_pull_request_merges_only_after_the_whole_browser_suite_on_its_tested_merge(
    github: GitHub, own: str | None
) -> None:
    tree = _changes(github)
    run = 900_020
    if own == "whole on another tree":
        github.e2e(github.head, github.base, run_id=run)
    else:
        github.e2e(github.head, tree, suite=own or "", run_id=run)
        if own is None:
            github.api[f"{_REPO}/check-runs/{run + 1}/annotations?per_page=100"] = []

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert "waiting for the whole browser suite to start" in done.stdout
    assert not (github.workflow.state / "merged").exists()
    suite = _suite(github)
    assert _scratch_branches(github) == [suite["branch"]]
    # The scratch commit is the tested merge: main and the pull request, with CI's files.
    assert github.workflow.git("rev-parse", f"{suite['head']}^{{tree}}") == tree
    assert github.workflow.git("show", "-s", "--format=%P", suite["head"]).split() == [
        github.base,
        github.head,
    ]
    assert len(github.workflow.calls(f"gh workflow run e2e.yml -R {_REPO[6:]}")) == 1
    assert _batch_call(github).returncode == 3  # No second dispatch while it starts.
    assert len(github.workflow.calls("gh workflow run")) == 1

    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert "suites" not in _batch_state(github) or _batch_state(github)["suites"] == {}
    assert _scratch_branches(github) == []


def test_a_dispatched_run_that_was_not_the_whole_suite_never_merges(github: GitHub) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(
        suite["head"],
        tree,
        suite="selected: 3 of 48 spec files",
        run_id=950_000,
        branch=suite["branch"],
    )

    done = _batch_call(github)

    assert done.returncode == 1
    assert "merge waiting: the run of the whole browser suite did not prove its tested files" in (
        done.stderr
    )
    assert not (github.workflow.state / "merged").exists()
    assert _batch_state(github)["suites"] == {}
    # The retest dispatches a new scratch run of the same tested merge; the same shortfall
    # again is a refusal for a reviewer.
    assert _batch_call(github).returncode == 3
    retested = _suite(github)
    assert retested["branch"] != suite["branch"] and retested["tree"] == tree
    github.e2e(
        retested["head"],
        tree,
        suite="selected: 3 of 48 spec files",
        run_id=950_100,
        branch=retested["branch"],
    )

    done = _batch_call(github)

    assert done.returncode == 1
    assert (
        "merge refused: the run of the whole browser suite did not prove its tested files, "
        "twice in a row on the same tested files"
    ) in done.stderr
    assert not (github.workflow.state / "merged").exists()


def test_a_whole_suite_retest_that_passes_merges_and_forgets_the_shortfall(
    github: GitHub,
) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(
        suite["head"],
        tree,
        suite="selected: 3 of 48 spec files",
        run_id=950_000,
        branch=suite["branch"],
    )
    assert "merge waiting" in _batch_call(github).stderr
    assert _batch_state(github)["retest"]["tree"] == tree
    assert _batch_call(github).returncode == 3
    retested = _suite(github)
    github.e2e(retested["head"], tree, run_id=950_100, branch=retested["branch"])

    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert _batch_state(github)["retest"] is None


def test_the_same_shortfall_on_other_tested_files_is_still_a_wait(github: GitHub) -> None:
    reason = "the run of the whole browser suite did not prove its tested files"
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    # The last shortfall had the same reason on the tree of an older main.
    train = _batch_state(github)
    other = github.workflow.git("rev-parse", f"{github.base}^{{tree}}")
    assert other != tree
    train["retest"] = {"tree": other, "reason": reason}
    (github.workflow.repo / ".local/workflow/merge-train.json").write_text(json.dumps(train))
    github.e2e(
        suite["head"],
        tree,
        suite="selected: 3 of 48 spec files",
        run_id=950_000,
        branch=suite["branch"],
    )

    done = _batch_call(github)

    assert done.returncode == 1
    assert f"merge waiting: {reason}; retesting next call" in done.stderr
    assert _batch_state(github)["retest"] == {"tree": tree, "reason": reason}


def test_a_failed_whole_suite_refuses_until_its_rerun_passes(github: GitHub) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"], conclusion="failure")

    done = _batch_call(github)

    assert done.returncode == 1
    assert "run 950000, failed on this pull request's tested merge (playwright)" in done.stderr
    # Blocked from then on, as a failed check is, and told so again while it stays red.
    assert _train(github)["blocked"] == ["7"]
    again = _batch_call(github)
    assert again.returncode == 1
    assert "a whole suite run on this pull request's tested merge failed" in again.stderr
    assert not (github.workflow.state / "merged").exists()
    # The same run, rerun and green: no new dispatch, and the merge goes ahead.
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(github.workflow.calls("gh workflow run")) == 1


def test_a_pull_request_whose_own_run_was_the_whole_suite_merges_at_once(github: GitHub) -> None:
    _changes(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert github.workflow.calls("gh workflow run") == []


def test_an_own_whole_suite_whose_failed_jobs_alone_were_rerun_merges_at_once(
    github: GitHub,
) -> None:
    _changes(github)
    _rerun_failed_jobs(github, 900_020, github.head, "E2E: what to run")
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert github.workflow.calls("gh workflow run") == []


def test_a_workflow_only_change_needs_no_browser_run(github: GitHub) -> None:
    _changes(github, "scripts/workflow/notes.md")
    del github.api[
        f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ]
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert github.workflow.calls("gh workflow run") == []
    assert not [call for call in github.workflow.calls("gh api") if "e2e.yml" in call]


def test_a_batch_runs_the_whole_browser_suite_on_its_tested_merge(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    assert all(m["scratch"]["workflows"] == ["ci", "e2e"] for m in _prefixes(github))
    assert len(github.workflow.calls("gh workflow run e2e.yml")) == 3
    _finish_batch(github)
    batch = _scratch(github)
    assert batch is not None
    github.e2e(
        batch["head"],
        _prefixes(github)[0]["tree"],
        suite="selected: 3 of 48 spec files",
        run_id=110,
        branch=batch["branch"],
    )
    github.save()
    before = _train(github)["turn"]

    done = _batch_call(github)

    assert done.returncode == 1
    assert (
        "merge waiting: pull request 7's prefix e2e did not prove the whole browser suite"
        in done.stderr
    )
    assert github.workflow.calls("gh pr merge") == []
    # Its reviewer, which waits in the line, is woken once more for the retest.
    assert _train(github)["turn"] > before


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_a_finished_whole_suite_run_changes_the_train_wakeup_once(
    github: GitHub, conclusion: str
) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    github.save()
    before = _train(github)["turn"]
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"], conclusion=conclusion)
    github.save()
    train = _train(github)
    assert train["turn"] > before
    # A failed run leaves it first and waiting, so the tick wakes its reviewer to be told.
    assert train["first"] == "7" and train["waiting"] == ["7"] and train["blocked"] == []
    assert _train(github)["turn"] == train["turn"]


@pytest.mark.parametrize("suite", ["failure", "running"])
def test_a_whole_suite_that_failed_or_still_runs_lets_a_green_pull_request_go_first(
    github: GitHub, suite: str
) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    second = _green_second(github)
    record = _suite(github)
    github.e2e(
        record["head"],
        tree,
        run_id=950_000,
        branch=record["branch"],
        conclusion="failure" if suite == "failure" else "success",
    )
    if suite == "running":
        path = (
            f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={record['head']}"
            "&event=workflow_dispatch&per_page=100"
        )
        github.api[path]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    else:
        # Its reviewer, woken by the finished run, is told and the pull request is blocked.
        assert _batch_call(github).returncode == 1
    github.save()

    # Without the whole suite counted, 8 would wait behind 7.
    done = github.workflow.run_script("merge-when-current", "8", cwd=second)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "8"
    train = _train(github)
    # A blocked 7 is first again only once a rerun of its suite passes.
    assert train["first"] == (None if suite == "failure" else "7")
    assert train["blocked"] == (["7"] if suite == "failure" else [])


def test_a_local_merge_unlike_the_tested_one_is_refused_before_any_run(github: GitHub) -> None:
    _changes(github)
    merge = github.api[f"{_REPO}/check-runs/21/annotations?per_page=100"][0]["message"]
    other = github.workflow.git("rev-parse", f"{github.base}^{{tree}}")
    github.api[f"{_REPO}/git/commits/{merge}"]["tree"]["sha"] = other

    done = _batch_call(github)

    assert done.returncode == 1
    assert "a local merge of this pull request differs from the merge CI tested" in done.stderr
    assert github.workflow.calls("gh workflow run") == []
    assert not (github.workflow.state / "merged").exists()


def test_a_merge_that_changes_nothing_needs_no_browser_run(github: GitHub) -> None:
    # Pull request 7 of the fixture is an empty commit: its merge is main's own tree.
    del github.api[
        f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={github.head}"
        "&event=pull_request&per_page=100"
    ]
    done = github.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [call for call in github.workflow.calls("gh") if "e2e.yml" in call]


@pytest.mark.parametrize(
    "own",
    [
        "selected: 4 of 374 files",
        # Before the planner noted the kind of run, nothing says every server test ran.
        None,
    ],
)
def test_a_pull_request_merges_only_after_every_server_test_on_its_tested_merge(
    github: GitHub, own: str | None
) -> None:
    tree = _selected_server_tests(github, own)

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert "waiting for every server test to start" in done.stdout
    assert not (github.workflow.state / "merged").exists()
    suite = _suite(github)
    assert suite["workflows"] == ["ci", "e2e"]
    assert github.workflow.git("rev-parse", f"{suite['head']}^{{tree}}") == tree
    assert len(github.workflow.calls(f"gh workflow run ci.yml -R {_REPO[6:]}")) == 1
    assert _batch_call(github).returncode == 3  # No second dispatch while it starts.
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 1

    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert _scratch_branches(github) == []


@pytest.mark.parametrize(
    "proof",
    [
        {"server": "selected: 4 of 374 files"},
        # Green, but of another commit than the gate's tested merge.
        {"merge": "0" * 40},
        # Green, with no server test run at all.
        {"python": "skipped"},
    ],
)
def test_a_dispatched_ci_run_that_was_not_every_server_test_never_merges(
    github: GitHub, proof: dict[str, str]
) -> None:
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000, **proof)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])

    done = _batch_call(github)

    assert done.returncode == 1
    assert "the run of every server test did not prove its tested files" in done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert _batch_state(github)["suites"] == {}


def test_a_failed_run_of_every_server_test_refuses_until_its_rerun_passes(
    github: GitHub,
) -> None:
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000, conclusion="failure")

    done = _batch_call(github)

    assert done.returncode == 1
    assert (
        "every server test, run 960000, failed on this pull request's tested merge "
        "(CI: server tests, CI result)"
    ) in done.stderr
    assert _train(github)["blocked"] == ["7"]
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 1


def test_a_run_of_every_server_test_red_for_dependencies_without_the_service_note_refuses(
    github: GitHub,
) -> None:
    _server_suite_dependencies_red(github, annotation=None)
    advisory_service_answers(github.workflow, answers=True)

    done = _batch_call(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "every server test, run 960000, failed" in done.stderr
    assert _train(github)["blocked"] == ["7"]
    assert rerun_calls(github.workflow) == []


def test_the_fix_of_a_red_python_check_merges_only_after_every_server_test_passed(
    github: GitHub,
) -> None:
    github.main("failure")
    main_jobs = github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]["jobs"]
    main_jobs[1]["name"] = "CI: server tests"
    tree = _selected_server_tests(github)
    # Its own "CI: server tests" passed, on the test files its change reaches.
    _pr_job(github, "CI: server tests", "success")

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    assert not (github.workflow.state / "merged").exists()
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 1
    suite = _suite(github)
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    assert "passed every check failing on main: CI result, CI: server tests" in done.stdout


def test_a_browser_suite_record_saved_before_server_tests_keeps_its_run(github: GitHub) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    state = json.loads(path.read_text())
    record = state["suites"]["7"]
    assert record.pop("workflows") == ["e2e"]
    path.write_text(json.dumps(state))
    # Its CI is then known to have run a selection of the server tests.
    github.ci(github.base, github.head, server="selected: 4 of 374 files")
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)

    done = _batch_call(github)

    assert done.returncode == 3, done.stdout + done.stderr
    suite = _suite(github)
    assert [suite["branch"], suite["head"], suite["workflows"]] == [
        record["branch"],
        record["head"],
        ["ci", "e2e"],
    ]
    assert len(github.workflow.calls("gh workflow run e2e.yml")) == 1
    assert len(github.workflow.calls("gh workflow run ci.yml")) == 1
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    assert _batch_call(github).returncode == 3  # Every server test has not run yet.
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"


def test_a_browser_suite_saved_before_server_tests_still_runs_while_others_go_first(
    github: GitHub,
) -> None:
    tree = _changes(github)
    github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    state = json.loads(path.read_text())
    del state["suites"]["7"]["workflows"]
    path.write_text(json.dumps(state))
    second = _green_second(github)
    record = _suite(github)
    github.e2e(record["head"], tree, run_id=950_000, branch=record["branch"])
    runs = (
        f"{_REPO}/actions/workflows/e2e.yml/runs?head_sha={record['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    github.api[runs]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.save()

    # Read as the browser suite's record, its run still going lets 8 merge first.
    done = github.workflow.run_script("merge-when-current", "8", cwd=second)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "8"


def test_a_finished_run_of_every_server_test_changes_the_train_wakeup_once(
    github: GitHub,
) -> None:
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"])
    github.save()
    before = _train(github)["turn"]
    github.ci_dispatched(suite["head"], suite["branch"], run_id=960_000)
    github.save()
    train = _train(github)
    assert train["turn"] > before
    assert _train(github)["turn"] == train["turn"]


def test_a_run_of_every_server_test_still_going_lets_a_green_pull_request_go_first(
    github: GitHub,
) -> None:
    tree = _selected_server_tests(github)
    assert _batch_call(github).returncode == 3
    second = _green_second(github)
    record = _suite(github)
    github.e2e(record["head"], tree, run_id=950_000, branch=record["branch"])
    github.ci_dispatched(record["head"], record["branch"], run_id=960_000)
    path = (
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={record['head']}"
        "&event=workflow_dispatch&per_page=100"
    )
    github.api[path]["workflow_runs"][0].update(status="in_progress", conclusion=None)
    github.save()

    done = github.workflow.run_script("merge-when-current", "8", cwd=second)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "8"
    assert _train(github)["first"] == "7"


@pytest.mark.parametrize("own", ["whole", "none"])
def test_a_pull_request_whose_ci_ran_every_server_test_or_needed_none_merges_at_once(
    github: GitHub, own: str
) -> None:
    _changes(github)
    github.ci(github.base, github.head, server=own)

    done = _batch_call(github)

    assert done.returncode == 0, done.stdout + done.stderr
    assert github.workflow.calls("gh workflow run") == []


def test_a_batch_whose_ci_ran_a_selection_never_merges(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    batch = _scratch(github)
    assert batch is not None
    notes = f"{_REPO}/check-runs/101/annotations?per_page=100"
    assert github.api[notes][1] == {"title": "Server tests", "message": "whole"}
    github.api[notes][1]["message"] = "selected: 4 of 374 files"

    done = _batch_call(github)

    assert done.returncode == 1
    assert "pull request 7's prefix ci did not prove its tested files" in done.stderr
    assert github.workflow.calls("gh pr merge") == []
    assert batch["workflows"][0] == "ci"


@pytest.mark.parametrize("python", ["skipped", None])
def test_a_batch_whose_ci_did_not_pass_its_python_job_never_merges(
    github: GitHub, python: str | None
) -> None:
    # The same proof as a pull request merging alone (whole_server_tests): the note and a
    # passed "CI: server tests", here skipped or missing.
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    found = github.api[f"{_REPO}/actions/runs/100/jobs?per_page=100"]["jobs"]
    python_job = next(job for job in found if job["name"] == "CI: server tests")
    if python is None:
        found.remove(python_job)
    else:
        python_job["conclusion"] = python

    done = _batch_call(github)

    assert done.returncode == 1
    assert "pull request 7's prefix ci did not prove its tested files" in done.stderr
    assert github.workflow.calls("gh pr merge") == []


def test_a_prefix_whose_whole_suite_fails_blocks_its_member(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    batch = _scratch(github)
    assert batch is not None
    github.e2e(
        batch["head"],
        _prefixes(github)[0]["tree"],
        run_id=110,
        branch=batch["branch"],
        conclusion="failure",
    )

    done = _batch_call(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "e2e run 110 of pull request 7's prefix failed (playwright)" in done.stderr
    assert github.workflow.calls("gh pr merge") == []
    assert _batch_state(github)["blocked"] == {"7": github.head}
    assert _numbers(github) == ["8", "9"]


@pytest.mark.parametrize(
    "reason", ["no annotation", "another job failed too", "hosted", "unreadable annotation"]
)
def test_a_batch_red_for_more_than_the_audit_service_is_isolated(
    github: GitHub, reason: str
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    batch_dependencies_red(
        github, annotation=None if reason == "no annotation" else UNREACHABLE, reason=reason
    )
    if reason == "unreadable annotation":
        del github.api[f"{_REPO}/check-runs/103/annotations?per_page=100"]
    advisory_service_answers(github.workflow, answers=True)

    done = _batch_call(github)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "ci run 100 of pull request 7's prefix failed" in done.stderr
    assert _batch_state(github)["blocked"] == {"7": github.head}
    assert _numbers(github) == ["8", "9"]
    assert rerun_calls(github.workflow) == []


@pytest.mark.parametrize("count", [2, 3])
def test_a_complete_receipt_inherits_every_job_with_python_shards(
    github: GitHub, count: int
) -> None:
    _pr_parts(github, *(["success"] * count))
    jobs = ("CI: older version on new tables", "CI: web", "CI: lint")
    for index, name in enumerate(jobs):
        github.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"].append(
            {"id": 60 + index, "name": name, "status": "completed", "conclusion": "success"}
        )
    _squash_with_pending_main(github)
    receipt = _receipt(github)
    assert receipt["ci_unproven"] == []
    assert receipt["ci_passed"] == sorted(["CI result", "CI: what to run", _PYTHON, *jobs])
    for index, name in enumerate(receipt["ci_passed"]):
        if name == "CI: what to run":
            github.api[f"{_REPO}/check-runs/{100 + index}/annotations?per_page=100"] = []
    _main_jobs(github, *[(name, "in_progress", None) for name in receipt["ci_passed"]])
    status = github.workflow.run_script("main-status")
    assert status.stdout.startswith("main: green"), status.stdout
    assert "inherited from CI run 20" in status.stdout


def test_successful_older_version_noop_keeps_prefix_testing(github: GitHub) -> None:
    _batch_setup(github)
    for number in (7, 8, 9):
        github.api[f"{_REPO}/actions/runs/{number * 1000}/jobs?per_page=100"]["jobs"].append(
            {"id": 30000 + number, "name": _OLDER, "status": "completed", "conclusion": "success"}
        )
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert _numbers(github) == ["7", "8", "9"]
    _finish_batch(github)
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


@pytest.mark.parametrize("workflow_name", ["ci", "images", "e2e"])
@pytest.mark.parametrize("second", ["passes", "fails again"])
def test_prefix_transport_retry_keeps_the_prefix_until_the_second_result(
    github: GitHub, workflow_name: str, second: str
) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    scratch = _scratch(github)
    path = (f"{_REPO}/actions/workflows/{workflow_name}.yml/runs?head_sha={scratch['head']}"
            "&event=workflow_dispatch&per_page=100")
    run = github.api[path]["workflow_runs"][0]
    run.update(status="in_progress", conclusion=None, run_attempt=1)
    jobs = github.api[f"{_REPO}/actions/runs/{run['id']}/jobs?per_page=100"]["jobs"]
    job = next(j for j in jobs if j["name"] not in
               ("CI: what to run", "CI result", "Docker images: what to run", "E2E: what to run"))
    job.update(conclusion="failure", labels=["example-app-home-docker"])
    if workflow_name == "ci":
        # The grouped check copies the passing first part's ID; the failing log is the
        # second part's. Classifying the grouped check would miss the transport failure.
        job["name"] = "CI: server tests (2 of 2)"
        jobs.insert(jobs.index(job), {
            **job, "id": job["id"] + 100, "name": "CI: server tests (1 of 2)",
            "conclusion": "success",
        })
    logs = github.workflow.state / "job-logs"
    logs.mkdir(exist_ok=True)
    (logs / str(job["id"])).write_text(
        "#13 [stage 1/2] FROM example-app:latest\n"
        "#13 ERROR: short read: expected 28249323 bytes but got 0: unexpected EOF\n")
    github.api[f"{_REPO}/check-runs/{job['id']}/annotations?per_page=100"] = []
    for j in jobs:
        j.setdefault("labels", ["example-app-home-docker"])
    github.save()
    done = _batch_call(github)
    assert done.returncode == 3, done.stdout + done.stderr
    assert rerun_calls(github.workflow) == []
    assert _scratch(github)["branch"] == scratch["branch"]
    run = github.api[path]["workflow_runs"][0]
    run.update(status="completed", conclusion="failure")
    github.save()
    for _ in range(2):
        done = _batch_call(github)
        assert done.returncode == 3, done.stdout + done.stderr
        assert rerun_calls(github.workflow) == [
            f"gh run rerun {run['id']} -R example-org/example-app --failed"]
        assert _scratch(github)["branch"] == scratch["branch"]
        assert _batch_state(github)["waiting"] == ["7", "8", "9"]
        assert "7" not in _batch_state(github)["blocked"]
    run = github.api[path]["workflow_runs"][0]
    jobs = github.api[f"{_REPO}/actions/runs/{run['id']}/jobs?per_page=100"]["jobs"]
    failed_job_id = job["id"]
    job = next(j for j in jobs if j["id"] == failed_job_id)
    if second == "passes":
        # The jobs endpoint has advanced to the passing retry while the run list still
        # reports the failed first attempt. Keep the request and this exact prefix.
        job["conclusion"] = "success"
        done = _batch_call(github)
        assert done.returncode == 3, done.stdout + done.stderr
        assert _scratch(github)["branch"] == scratch["branch"]
        assert "7" not in _batch_state(github)["blocked"]
        run = github.api[path]["workflow_runs"][0]
    run.update(run_attempt=2, conclusion="success" if second == "passes" else "failure")
    github.save()
    done = _batch_call(github)
    assert done.returncode == (0 if second == "passes" else 1), done.stdout + done.stderr
    assert len(rerun_calls(github.workflow)) == 1
    assert ("7" in _batch_state(github)["blocked"]) == (second != "passes")
    if second != "passes":
        assert "failed again after its runner retry" in done.stderr
        assert f"its runners failed it again in run {run['id']}" in github.workflow.wake_log.read_text()


@pytest.mark.parametrize("second", ["passes", "fails again"])
@pytest.mark.parametrize("workflow_name", ["ci", "e2e"])
def test_whole_suite_transport_retry_keeps_the_scratch_run(
    github: GitHub, second: str, workflow_name: str
) -> None:
    if workflow_name == "ci":
        tree = _selected_server_tests(github)
    else:
        tree = _changes(github)
        github.e2e(github.head, tree, suite="selected: 3 of 48 spec files", run_id=900_020)
    assert _batch_call(github).returncode == 3
    suite = _suite(github)
    if workflow_name == "ci":
        github.e2e(suite["head"], tree, run_id=950_100, branch=suite["branch"])
        github.ci_dispatched(suite["head"], suite["branch"], run_id=950_000, conclusion="failure")
    else:
        github.e2e(suite["head"], tree, run_id=950_000, branch=suite["branch"], conclusion="failure")
    path = (f"{_REPO}/actions/workflows/{workflow_name}.yml/runs?head_sha={suite['head']}"
            "&event=workflow_dispatch&per_page=100")
    run = github.api[path]["workflow_runs"][0]
    run["run_attempt"] = 1
    jobs = github.api[f"{_REPO}/actions/runs/950000/jobs?per_page=100"]["jobs"]
    for job in jobs:
        job["labels"] = ["example-app-home-docker"]
    logs = github.workflow.state / "job-logs"
    logs.mkdir(exist_ok=True)
    (logs / "950002").write_text("failed to solve: registry: TLS handshake timeout\n")
    github.api[f"{_REPO}/check-runs/950002/annotations?per_page=100"] = []
    github.save()
    for _ in range(2):
        done = _batch_call(github)
        assert done.returncode == 3, done.stdout + done.stderr
        assert _suite(github)["branch"] == suite["branch"]
        assert "7" not in _batch_state(github)["blocked"]
        assert rerun_calls(github.workflow) == [
            "gh run rerun 950000 -R example-org/example-app --failed"]
    run = github.api[path]["workflow_runs"][0]
    jobs = github.api[f"{_REPO}/actions/runs/950000/jobs?per_page=100"]["jobs"]
    for job in jobs[1:]:
        job["conclusion"] = "success" if second == "passes" else "failure"
    if second == "passes":
        # Mixed attempt snapshots must not turn a successful retry into a hand-back.
        done = _batch_call(github)
        assert done.returncode == 3, done.stdout + done.stderr
        assert _suite(github)["branch"] == suite["branch"]
        assert "7" not in _batch_state(github)["blocked"]
        run = github.api[path]["workflow_runs"][0]
    run.update(run_attempt=2, conclusion="success" if second == "passes" else "failure")
    github.save()
    done = _batch_call(github)
    assert done.returncode == (0 if second == "passes" else 1), done.stdout + done.stderr
    assert len(rerun_calls(github.workflow)) == 1
    if second != "passes":
        assert "failed again after its runner retry" in done.stderr
        assert "its runners failed it again in run 950000" in github.workflow.wake_log.read_text()


def test_a_transient_diagnosis_read_keeps_the_prefix_waiting(github: GitHub) -> None:
    _batch_setup(github, images=True)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    scratch = _scratch(github)
    path = (f"{_REPO}/actions/workflows/images.yml/runs?head_sha={scratch['head']}"
            "&event=workflow_dispatch&per_page=100")
    run = github.api[path]["workflow_runs"][0]
    run.update(conclusion="failure", run_attempt=1)
    jobs = github.api[f"{_REPO}/actions/runs/{run['id']}/jobs?per_page=100"]["jobs"]
    jobs[-1].update(conclusion="failure", labels=["example-app-home-docker"])
    github.api[f"{_REPO}/check-runs/{jobs[-1]['id']}/annotations?per_page=100"] = "HTTP 502"
    done = _batch_call(github)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "merge waiting" in done.stderr
    assert "Traceback" not in done.stderr
    assert rerun_calls(github.workflow) == []
    assert _scratch(github)["branch"] == scratch["branch"]
    assert "7" not in _batch_state(github)["blocked"]


@pytest.mark.parametrize("suite", ["ci", "e2e"])
def test_a_scratch_main_lane_job_past_the_default_limit_is_cancelled_then_rerun_once(
    github: GitHub, suite: str,
) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    run = _queued_prefix_job(github, workflow=suite)
    identity = run["id"]
    assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls(f"gh run cancel {identity} ")) == 1
    assert not github.workflow.calls(f"gh run rerun {identity} ")
    assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls(f"gh run cancel {identity} ")) == 1
    # Cancellation finishes on GitHub between gate calls; a new process resumes the retry.
    scratch = _scratch(github)
    run = github.api[
        f"{_REPO}/actions/workflows/{suite}.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0]
    run.update(status="completed", conclusion="cancelled")
    assert _batch_call(github).returncode == 3
    assert github.workflow.calls(f"gh run rerun {identity} ") == [
        f"gh run rerun {identity} -R example-org/example-app"
    ]
    assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls(f"gh run rerun {identity} ")) == 1
    status = github.workflow.run_script("merge-when-current", "--train-status")
    assert status.returncode == 0, status.stderr
    prefix = json.loads(status.stdout)["prefixes"][0]
    assert prefix["queued_retries"][str(identity)]["state"] == "rerun requested"
    # A successful ordinary attempt still supplies the normal merge proof.
    _finish_batch(github)
    github.api[
        f"{_REPO}/actions/workflows/{suite}.yml/runs?head_sha={scratch['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0]["run_attempt"] = 2
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert _merges(github) == ["7", "8", "9"]


@pytest.mark.parametrize("minutes,limit", [(19, None), (21, 25)])
def test_a_scratch_job_under_the_queue_limit_is_left_alone(
    github: GitHub, minutes: int, limit: int | None,
) -> None:
    _queue_config(github, limit)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github, minutes=minutes)
    assert _batch_call(github).returncode == 3
    assert not github.workflow.calls("gh run cancel")
    assert not github.workflow.calls("gh run rerun")


@pytest.mark.parametrize("suite,label", [
    ("images", "example-app-home-docker"), ("ci", "example-app-home"),
    ("e2e", "home-planner"),
])
def test_a_long_wait_outside_the_main_batch_lane_is_not_cancelled(
    github: GitHub, suite: str, label: str,
) -> None:
    _queue_config(github)
    _batch_setup(github, images=suite == "images")
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github, minutes=87, workflow=suite, label=label)
    assert _batch_call(github).returncode == 3
    assert not github.workflow.calls("gh run cancel")
    assert not github.workflow.calls("gh run rerun")


def test_a_second_attempt_stuck_in_the_lane_is_reported_once_without_a_retry(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github, attempt=2)
    first = _batch_call(github)
    second = _batch_call(github)
    assert first.returncode == second.returncode == 3
    assert "run 100 attempt 2" in first.stdout
    assert "queued past 20 minutes" in first.stdout
    assert "queued past 20 minutes" not in second.stdout
    assert not github.workflow.calls("gh run cancel")
    assert not github.workflow.calls("gh run rerun")
    pending = github.workflow.repo / ".local/workflow/prefix-queues.pending"
    [event] = pending.glob("*.json")
    assert "run 100 attempt 2" in json.loads(event.read_text())["line"]


@pytest.mark.parametrize("branch,event", [("main", "push"), ("ticket/seven", "pull_request")])
def test_no_run_outside_the_scratch_prefix_is_cancelled_for_its_queue(
    github: GitHub, branch: str, event: str,
) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    run = _queued_prefix_job(github)
    run.update(head_branch=branch, event=event)
    assert _batch_call(github).returncode == 3
    assert not github.workflow.calls("gh run cancel")
    assert not github.workflow.calls("gh run rerun")


def test_a_retry_queued_on_ordinary_runners_is_reported_without_another_retry(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github, attempt=2, label="example-app-home")
    first = _batch_call(github)
    assert first.returncode == 3
    assert "run 100 attempt 2" in first.stdout
    assert len(github.workflow.calls("gh run cancel 100 ")) == 1
    assert not github.workflow.calls("gh run rerun")


def test_a_timestamp_missing_from_a_queued_job_starts_a_durable_observation(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github)
    part = github.api[f"{_REPO}/actions/runs/100/jobs?per_page=100"]["jobs"][0]
    part.pop("created_at")
    assert _batch_call(github).returncode == 3
    assert not github.workflow.calls("gh run cancel")
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    state = json.loads(path.read_text())
    observation = state["prefixes"]["members"][0]["scratch"]["queued_retries"]["100"]["jobs"]
    observation["1:102"] -= 21 * 60
    path.write_text(json.dumps(state))
    assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls("gh run cancel 100 ")) == 1


def test_one_queued_matrix_part_past_the_limit_retries_the_run(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github)
    parts = github.api[f"{_REPO}/actions/runs/100/jobs?per_page=100"]["jobs"]
    parts.insert(0, {**parts[0], "id": 103, "name": "CI: server tests (1 of 3)",
                     "created_at": datetime.now(UTC).isoformat()})
    parts.append({**parts[0], "id": 104, "name": "CI: server tests (3 of 3)",
                  "status": "completed", "conclusion": "success"})
    assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls("gh run cancel 100 ")) == 1


def test_a_cancel_lost_to_an_outage_is_retried_after_a_gate_restart(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github)
    github.cancel_unavailable = [100]
    _batch_call(github)
    assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls("gh run cancel 100 ")) == 2
    assert not github.workflow.calls("gh run rerun")


@pytest.mark.parametrize("error", [
    "HTTP 502: Bad Gateway", 'Post "https://api.github.com/x": context deadline exceeded',
])
def test_a_rerun_lost_to_an_outage_is_sent_again_while_the_first_attempt_stays_cancelled(
    github: GitHub, error: str,
) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github)
    assert _batch_call(github).returncode == 3
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={_scratch(github)['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0].update(status="completed", conclusion="cancelled")
    (github.workflow.state / "rerun-unavailable").write_text(error)
    assert "could not re-run scratch run 100 yet" in _batch_call(github).stderr
    assert len(github.workflow.calls("gh run rerun 100 ")) == 1
    assert not (github.workflow.repo / ".local/workflow/prefix-queues.pending").exists()
    for _ in range(3):
        assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls("gh run rerun 100 ")) == 2


def test_a_recovered_run_is_still_cancelled_when_its_prefix_is_dropped(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github, number="9")
    github.save()
    assert github.workflow.run_script("merge-when-current", "--train-status").returncode == 0
    assert len(github.workflow.calls("gh run cancel 100 ")) == 1
    dropped = _scratch(github, "9")
    assert dropped is not None
    github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={dropped['head']}"
        "&event=workflow_dispatch&per_page=100"
    ]["workflow_runs"][0].update(status="completed", conclusion="cancelled")
    github.save()
    assert github.workflow.run_script("merge-when-current", "--train-status").returncode == 0
    assert len(github.workflow.calls("gh run rerun 100 ")) == 1
    # Its second attempt, the same run, holds ordinary runners when the prefix is dropped.
    _branch_runs(github, dropped["branch"], (100, "in_progress"))
    github.pulls["9"]["state"] = "CLOSED"
    _batch_call(github)
    assert len(github.workflow.calls("gh run cancel 100 ")) == 2
    assert dropped["branch"] not in _scratch_branches(github)


def test_train_status_recovers_a_later_prefix_without_polluting_its_json(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github, number="8")
    github.save()
    done = github.workflow.run_script("merge-when-current", "--train-status")
    assert done.returncode == 0, done.stderr
    status = json.loads(done.stdout)
    assert status["prefixes"][1]["queued_retries"]["100"]["state"] == "cancel requested"
    assert len(github.workflow.calls("gh run cancel 100 ")) == 1


def test_a_stale_job_from_the_previous_attempt_does_not_report_a_new_wait(github: GitHub) -> None:
    _queue_config(github)
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github, attempt=2)
    github.api[f"{_REPO}/actions/runs/100/jobs?per_page=100"]["jobs"][0]["run_attempt"] = 1
    done = _batch_call(github)
    assert done.returncode == 3
    assert "queued past" not in done.stdout
    assert not (github.workflow.repo / ".local/workflow/prefix-queues.pending").exists()
    assert not github.workflow.calls("gh run cancel")


def test_a_custom_pool_label_and_unrelated_planner_label_do_not_change_recovery(github: GitHub) -> None:
    _queue_config(github)
    path = github.workflow.repo / "shipline.json"
    config = json.loads(path.read_text())
    config["runner"].update(label="example-pool", planner_label="home-planner")
    path.write_text(json.dumps(config))
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _queued_prefix_job(github, label="example-pool-main-batch")
    assert _batch_call(github).returncode == 3
    assert len(github.workflow.calls("gh run cancel 100 ")) == 1
