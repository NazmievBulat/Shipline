"""Breaking tests for pull request 55: a restored member ahead of prefixes already built on main.

Each test replays a daily flow of the merge line with neutral names: a member whose prefix
failed gets a green rerun and its place back while the prefix of a member behind it, built on
the same main, is running or done. Gate calls repeat as the tick makes them.
"""

import json
import threading
from typing import Any

import pytest
from workflow.test_merge_when_current import (
    _REPO,
    SELECTED,
    GitHub,
    _batch_call,
    _batch_setup,
    _batch_state,
    _branch_runs,
    _cancelled,
    _finish_prefix,
    _merges,
    _prefixes,
    _receipt,
    _scratch,
    _scratch_branches,
    _train,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture


def _runs_key(scratch: dict[str, Any], workflow: str) -> str:
    return (
        f"{_REPO}/actions/workflows/{workflow}.yml/runs"
        f"?head_sha={scratch['head']}&event=workflow_dispatch&per_page=100"
    )


def _dispatches(github: GitHub, branch: str) -> int:
    return len([call for call in github.workflow.calls("gh workflow run") if branch in call])


def _restore_with_images_running(
    github: GitHub,
    *,
    count: int = 3,
    window: int | None = None,
    old_state: bool = True,
    short: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """7 is ahead of 8. 7's prefix fails; 8's prefix (main + 8) then has CI and E2E green
    while its image checks still run. 7's failed run is rerun green on the same main. With
    `short`, the rerun did not prove its tested files, so 7 needs a new prefix. With two
    members and a window of two, the failure halves the window to one and 8's prefix is
    main + 8 alone."""
    _batch_setup(github, count=count, images=True, images_member=8)
    if window is not None:
        path = github.workflow.repo / ".local/workflow/merge-train.json"
        saved = _batch_state(github)
        saved["window"] = window
        path.write_text(json.dumps(saved))
    assert _batch_call(github).returncode == 3
    isolated = _prefixes(github)[0]
    assert isolated["number"] == "7"
    _finish_prefix(github, "7", conclusion="failure")
    assert _batch_call(github).returncode == 1
    kept = next(member for member in _prefixes(github) if member["number"] == "8")
    assert "images" in kept["scratch"]["workflows"], kept
    _finish_prefix(github, "8", run_id=1100)
    # Only the image checks still run, on their own runners.
    github.api[_runs_key(kept["scratch"], "images")]["workflow_runs"][0].update(
        status="in_progress", conclusion=None
    )
    if old_state:
        # The state file as the gate wrote it before this change.
        path = github.workflow.repo / ".local/workflow/merge-train.json"
        saved = _batch_state(github)
        saved.pop("restored", None)
        for member in saved["prefixes"]["members"]:
            member.pop("ahead", None)
            member.pop("kept", None)
        path.write_text(json.dumps(saved))
    _finish_prefix(github, "7", scratch=isolated["scratch"], tree=isolated["tree"], run_id=100)
    for workflow in isolated["scratch"]["workflows"]:
        github.api[_runs_key(isolated["scratch"], workflow)]["workflow_runs"][0]["run_attempt"] = 2
    if short:
        github.api[f"{_REPO}/check-runs/101/annotations?per_page=100"][1]["message"] = SELECTED
    return isolated, kept


# The line as it stands: three members, or two with a window of two (main + 8 alone after 7 fails).
SHAPES = pytest.mark.parametrize(
    "shape", [{"count": 3}, {"count": 2, "window": 2}], ids=["three", "two-window-one"]
)


def _finish_images(github: GitHub, scratch: dict[str, Any]) -> None:
    run = github.api[_runs_key(scratch, "images")]["workflow_runs"][0]
    run.update(status="completed", conclusion="success")
    # The harness serializes API responses between calls; both reads name this attempt.
    github.api[f"{_REPO}/actions/runs/{run['id']}"] = dict(run)


def _tick_round(github: GitHub) -> list[Any]:
    """One tick: every handed-off member's gate call, in queue order."""
    results = []
    for number in [entry for entry in ("7", "8", "9", "10") if entry in github.pulls]:
        if github.pulls[number].get("state") in ("CLOSED", "MERGED"):
            continue
        if number in _merges(github):
            continue
        results.append(_batch_call(github, number))
    return results


@SHAPES
def test_a_prefix_with_images_running_is_kept_and_merges_when_they_pass(
    github: GitHub, shape: dict[str, int]
) -> None:
    """A daily flow: 8's prefix of main + 8 waits only for its image checks; 7 is restored
    ahead of it with a proof shortfall. 8's prefix is not thrown away, its image runs are not
    cancelled or dispatched again, and 8 merges on main + 8 as soon as they pass."""
    _, kept = _restore_with_images_running(github, **shape)
    branch = kept["scratch"]["branch"]
    outputs = ""
    for _ in range(4):
        for done in _tick_round(github):
            outputs += done.stdout + done.stderr
            assert done.returncode in (1, 3), done.stdout + done.stderr
        assert _scratch(github, "8")["branch"] == branch
        assert branch in _scratch_branches(github)
        assert not _merges(github)
    assert "the prefix of pull request 8 and those" not in outputs
    assert "did not prove its tested files" in outputs
    assert _dispatches(github, branch) == len(kept["scratch"]["workflows"])
    assert not [run for run, _ in _cancelled(github).items() if run.startswith("11")]
    # No prefix behind 7 was rebuilt to hold it on the old main.
    for member in _prefixes(github):
        if member["number"] != "7":
            assert "7" not in member["ahead"], member
        if member["number"] == "8":
            assert member["commit"] == kept["commit"]
    _finish_images(github, kept["scratch"])
    for done in _tick_round(github):
        assert done.returncode in (0, 1, 3), done.stdout + done.stderr
    assert _merges(github)[:1] == ["8"]
    receipt = _receipt(github)
    assert receipt["tree"] == kept["tree"]


@SHAPES
def test_the_restored_member_merges_after_the_kept_one_on_the_new_main(
    github: GitHub, shape: dict[str, int]
) -> None:
    _, kept = _restore_with_images_running(github, **shape)
    for _ in range(2):
        _tick_round(github)
    _finish_images(github, kept["scratch"])
    _tick_round(github)
    assert _merges(github) == ["8"]
    restored = _scratch(github, "7")
    if shape["count"] == 2:
        # Alone in the line, 7 takes its single path on the new main: a refresh, not a wait.
        assert restored is None
        done = _batch_call(github, "7")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "CI requested on current main" in done.stdout
        return
    assert restored is not None
    base = github.workflow.git("rev-parse", "origin/main")
    assert restored["under"] == github.workflow.git("rev-parse", f"{base}^{{tree}}")
    for _ in range(2):
        _tick_round(github)
        assert _merges(github) == ["8"]
    _finish_prefix(github, "7", run_id=3100)
    _tick_round(github)
    assert _merges(github) == ["8", "7"]


@SHAPES
def test_the_restored_prefix_does_not_take_a_runner_slot_from_kept_work(
    github: GitHub, shape: dict[str, int]
) -> None:
    """After a failure the window halves: with two members it is one, so the restored member's
    retest must wait for the kept prefix rather than add image jobs beside it."""
    _restore_with_images_running(github, **shape)
    for _ in range(3):
        _tick_round(github)
        saved = _batch_state(github)
        started = [
            member["number"]
            for member in saved["prefixes"]["members"]
            if member["scratch"] is None or member["scratch"]["pushed"]
        ]
        assert len(started) <= saved["window"], (started, saved["window"])


@SHAPES
def test_a_kept_prefix_whose_member_pushed_is_dropped_and_its_live_runs_cancelled(
    github: GitHub,
    shape: dict[str, int],
) -> None:
    _, kept = _restore_with_images_running(github, **shape)
    _tick_round(github)
    branch = kept["scratch"]["branch"]
    _branch_runs(
        github,
        branch,
        (1121, "in_progress", branch, "workflow_dispatch", "images"),
        (1101, "completed"),
    )
    restored = _scratch(github, "7")
    if restored and restored["pushed"]:
        _branch_runs(github, restored["branch"], (3101, "in_progress"))
    github.pulls["8"]["headRefOid"] = github.pulls["7"]["headRefOid"]
    done = _batch_call(github, "7")
    assert done.returncode in (1, 2, 3), done.stdout + done.stderr
    assert branch not in _scratch_branches(github)
    cancelled = _cancelled(github)
    assert "1121" in cancelled and "1101" not in cancelled
    assert "3101" not in cancelled
    assert done.stdout.count("cancel requested for run 1121") == 1


def test_the_restored_members_runs_on_the_old_main_are_cancelled_when_the_kept_one_merges(
    github: GitHub,
) -> None:
    """A wider window: 7's own prefix starts beside 8's. Once 8 merges, 7's run on the old main
    proves nothing; its live runs must not hold the image runners."""
    _, kept = _restore_with_images_running(github, count=3)
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    saved = _batch_state(github)
    saved["window"] = 4
    path.write_text(json.dumps(saved))
    for _ in range(2):
        _tick_round(github)
    restored = _scratch(github, "7")
    assert restored is not None and restored["pushed"], _batch_state(github)
    _branch_runs(
        github,
        restored["branch"],
        (3121, "in_progress", restored["branch"], "workflow_dispatch", "images"),
        (3101, "queued"),
    )
    _finish_images(github, kept["scratch"])
    _tick_round(github)
    assert _merges(github)[:1] == ["8"]
    assert restored["branch"] not in _scratch_branches(github)
    cancelled = _cancelled(github)
    assert {"3121", "3101"} <= set(cancelled)
    assert all(not branch.startswith("ticket/") for runs in cancelled.values() for branch in runs)


@SHAPES
def test_a_state_file_written_before_the_change_mid_restore_is_read(
    github: GitHub, shape: dict[str, int]
) -> None:
    """The gate restored 7 under the old code: the state names it in `proven` only, with
    prefixes saved without dependencies or a kept mark."""
    _restore_with_images_running(github, **shape)
    github.save()
    status = _train(github)
    assert status["first"] in ("7", "8")
    path = github.workflow.repo / ".local/workflow/merge-train.json"
    saved = _batch_state(github)
    saved.pop("restored", None)
    for member in saved["prefixes"]["members"]:
        member.pop("ahead", None)
        member.pop("kept", None)
    path.write_text(json.dumps(saved))
    done = _batch_call(github, "8")
    assert done.returncode in (1, 3), done.stdout + done.stderr
    assert "Traceback" not in done.stderr
    assert _scratch(github, "8") is not None


@SHAPES
def test_two_gate_calls_at_once_dispatch_the_restored_prefix_once(
    github: GitHub, shape: dict[str, int]
) -> None:
    _restore_with_images_running(github, **shape, short=False)
    github.save()
    results: list[Any] = []

    def call(number: str) -> None:
        results.append(github.workflow.run_script("merge-when-current", number))

    threads = [threading.Thread(target=call, args=(number,)) for number in ("7", "8")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for done in results:
        assert "Traceback" not in done.stderr, done.stderr
    saved = json.loads((github.workflow.state / "github.json").read_text())
    github.api, github.pulls = saved["api"], saved["pulls"]
    branches = [m["scratch"]["branch"] for m in _prefixes(github) if m["scratch"]]
    for branch in branches:
        assert _dispatches(github, branch) <= 3


@pytest.mark.parametrize("passed", [False, True], ids=["running", "passed"])
def test_a_proven_rerun_with_kept_work_beside_it_merges_without_a_new_run(
    github: GitHub, passed: bool
) -> None:
    """The rerun proved its files: 7 merges on its rerun at once, as the front."""
    _, kept = _restore_with_images_running(github, short=False)
    if passed:
        _finish_images(github, kept["scratch"])
    _tick_round(github)
    assert _merges(github)[:1] == ["7"]
    assert len([c for c in github.workflow.calls("gh workflow run") if "--ref" in c]) >= 1


@pytest.mark.parametrize("count", [6, 8])
def test_the_restored_front_is_not_overtaken_by_every_member_behind_it(
    github: GitHub, count: int
) -> None:
    """A busy line: 7 is restored ahead of five members. Every prefix takes the same time
    (two ticks) from its push to its result, as image checks on a fixed pool of runners do.
    7 is the front; it must merge before the line behind it has drained, not be retested
    after every later member's merge."""
    _restore_with_images_running(github, count=count)
    pushed_at: dict[str, int] = {}
    finished: set[str] = set()
    restarts: list[str] = []
    for now in range(40):
        for member in _prefixes(github):
            scratch = member["scratch"]
            if scratch and scratch["pushed"]:
                pushed_at.setdefault(scratch["branch"], now)
                if member["number"] == "7" and scratch["branch"] not in restarts:
                    restarts.append(scratch["branch"])
        for member in _prefixes(github):
            scratch = member["scratch"]
            if (
                scratch
                and scratch["branch"] in pushed_at
                and now - pushed_at[scratch["branch"]] >= 2
                and scratch["branch"] not in finished
            ):
                finished.add(scratch["branch"])
                _finish_prefix(github, member["number"], run_id=10_000 + len(finished) * 100)
        _tick_round(github)
        if "7" in _merges(github):
            break
    order = _merges(github)
    assert "7" in order, (order, restarts)
    # 8 and 9 were kept at the restoration and may go first; no member started after it may.
    assert order.index("7") <= 2, f"merge order {order}; prefixes of 7 started: {len(restarts)}"


@pytest.mark.parametrize("count", [4, 6])
def test_after_the_kept_prefixes_merge_the_restored_front_keeps_a_prefix(
    github: GitHub, count: int
) -> None:
    """7 is restored ahead of the kept prefixes of 8 and 9, and both pass. The call that
    merges them moves main twice; 7's own prefix on the old main proves nothing now. The gate
    must test 7 again on the new main, ahead of the members behind it, rather than fill the
    window with new prefixes that leave the front out."""
    _restore_with_images_running(github, count=count)
    _tick_round(github)
    _finish_prefix(github, "8", run_id=20_000)
    _finish_prefix(github, "9", run_id=21_000)
    _tick_round(github)
    assert _merges(github) == ["8", "9"]
    members = _prefixes(github)
    numbers = [m["number"] for m in members]
    without_front = [
        m["number"]
        for m in members
        if m["number"] != "7" and "7" not in m["ahead"] and m["scratch"]["pushed"]
    ]
    assert "7" in numbers or not without_front, (
        f"prefixes {numbers} started on the new main without the restored front 7"
    )


def test_a_kept_prefix_whose_images_fail_then_pass_on_a_partial_rerun(github: GitHub) -> None:
    """8's kept prefix fails one image job; the failed jobs are re-run and pass. The restored
    7 must not wait on it: it takes the freed slot and merges. 8 then follows the rules for a
    failed member (its rerun tested an older main once 7 merged), reported by its own call."""
    _, kept = _restore_with_images_running(github, count=3)
    _tick_round(github)
    run = github.api[_runs_key(kept["scratch"], "images")]["workflow_runs"][0]
    run.update(status="completed", conclusion="failure")
    jobs = github.api[f"{_REPO}/actions/runs/{run['id']}/jobs?per_page=100"]["jobs"]
    jobs[1]["conclusion"] = "failure"
    outputs = "".join(done.stdout + done.stderr for done in _tick_round(github))
    assert "Traceback" not in outputs
    assert _batch_state(github)["blocked"].get("8") == github.pulls["8"]["headRefOid"]
    run.update(conclusion="success", run_attempt=2)
    jobs[1]["conclusion"] = "success"
    for round_number in range(8):
        outputs = "".join(done.stdout + done.stderr for done in _tick_round(github))
        assert "Traceback" not in outputs, outputs
        for index, member in enumerate(_prefixes(github)):
            scratch = member["scratch"]
            if scratch and scratch["pushed"] and scratch["head"] != kept["scratch"]["head"]:
                _finish_prefix(
                    github, member["number"], run_id=40_000 + round_number * 1000 + index * 100
                )
        if "7" in _merges(github):
            break
    assert _merges(github)[:1] == ["7"], _merges(github)
    done = _batch_call(github, "8")
    assert "Traceback" not in done.stderr
    assert done.returncode in (1, 2, 3), done.stdout + done.stderr
