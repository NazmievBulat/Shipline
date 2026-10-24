"""Breaking tests for pull request 38: a handoff whose green re-run tested an older main is
kept for its refresh, not handed back. Each test replays a daily flow of the tick: main moves
often, several pull requests wait in the line, and a handed-off member's failed run is re-run
green on the main it first tested."""

import json
import shutil
import textwrap
import threading
from copy import deepcopy

from workflow.test_merge_driver import (
    _folder,
    _handoff,
    _note,
    _publish,
    _started,
    handed,  # noqa: F401  (fixture)
)
from workflow.test_merge_when_current import (
    UNREACHABLE,
    GitHub,
    advisory_service_answers,
    github,  # noqa: F401  (fixture)
    pull_dependencies_red,
    rerun_calls,
)

_REPO = "repos/example-org/example-app"
_OTHER = "8" * 40


def _runs(head: str) -> str:
    return f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=100"


def _train(github: GitHub) -> dict:
    return json.loads((_folder(github.workflow) / "merge-train.json").read_text())


def _advisory_helper(github: GitHub) -> None:
    """The repository's advisory helper beside the scripts: the audit's note names npm, and
    npm's service answers again now."""
    (github.workflow.script.parent / "advisory_service.py").write_text(
        textwrap.dedent(
            f"""\
            ANNOTATION = "Advisory service unreachable"
            def unreachable(output):
                return "npm" if {UNREACHABLE!r} in output else None
            def note_down(folder, service):
                (folder / "advisory-service-down").write_text(service)
            def note_up(folder):
                (folder / "advisory-service-down").unlink(missing_ok=True)
            def down(folder):
                return None
            def answers(service):
                return True
            """
        )
    )
    advisory_service_answers(github.workflow, answers=True)


def _main_moves(github: GitHub) -> str:
    """Main takes another merge, green. Returns the main before it."""
    workflow = github.workflow
    old = github.base
    workflow.git("checkout", "main")
    workflow.git("commit", "--allow-empty", "-m", "fix: another merge lands")
    github.base = workflow.git("rev-parse", "HEAD")
    workflow.git("push", "origin", "main")
    github.main()
    github.save()
    return old


def _runs_of(github: GitHub) -> dict:
    return github.api[_runs(github.head)]["workflow_runs"][0]


def _green_rerun_on(github: GitHub, tested_base: str) -> None:
    """The failed run's second attempt passed, on the main it first tested."""
    github.ci(github.base, github.head, tested_base=tested_base)
    _runs_of(github)["run_attempt"] = 2
    github.pulls["7"]["statusCheckRollup"] = [
        {"name": "CI result", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    github.save()


def _another_first(github: GitHub) -> None:
    """Pull request 8 waits ahead of 7 with its own reviewer, its checks still running: 7 is
    not first for as long as 8 stays."""
    workflow = github.workflow
    github.pulls["8"] = {
        **deepcopy(github.pulls["7"]),
        "headRefName": "ticket/eight",
        "headRefOid": _OTHER,
        "statusCheckRollup": [{"name": "CI result", "status": "IN_PROGRESS", "conclusion": None}],
    }
    github.api[_runs(_OTHER)] = {
        "workflow_runs": [
            {
                "id": 80,
                "head_sha": _OTHER,
                "event": "pull_request",
                "status": "in_progress",
                "conclusion": None,
            }
        ]
    }
    github.api[f"{_REPO}/actions/runs/80/jobs?per_page=100"] = {
        "jobs": [{"id": 81, "name": "CI result", "status": "in_progress", "conclusion": None}]
    }
    workflow.list_pull_request(8, draft=False)
    (workflow.state / "pr" / "8").write_text(f"OPEN ticket/eight {_OTHER}\n")
    # A stopped session's process folder stays behind; the next session takes its number.
    stale = workflow.root / "proc" / str(4000 + len(list((workflow.state / "tmux").iterdir())))
    shutil.rmtree(stale, ignore_errors=True)
    workflow.running("review-8")
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "merge-train.json"
    train = json.loads(path.read_text()) if path.exists() else {}
    train["waiting"] = ["8", *[n for n in train.get("waiting", []) if n != "8"]]
    path.write_text(json.dumps(train))
    github.save()


def _first_leaves(github: GitHub) -> None:
    github.pulls["8"]["state"] = "MERGED"
    (github.workflow.state / "pr" / "8").write_text(f"MERGED ticket/eight {_OTHER}\n")
    github.save()


def _tick(github: GitHub) -> str:
    done = github.workflow.run_script("tick")
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


def _kept(github: GitHub, output: str) -> None:
    workflow = github.workflow
    assert "merge hand-back 7" not in output, output
    assert _note(workflow).exists(), output
    assert not (_folder(workflow) / "merge-review-7").exists()
    assert "review-7" not in _started(workflow)


def _waits_unrefreshed(github: GitHub, output: str) -> None:
    _kept(github, output)
    workflow = github.workflow
    assert workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7)) == github.head, output
    assert json.loads(_note(workflow).read_text())["head"] == github.head


def _refreshed_onto(github: GitHub, base: str) -> None:
    workflow = github.workflow
    refreshed = workflow.git("rev-parse", "HEAD", cwd=workflow.worktree(7))
    assert refreshed != github.head
    assert workflow.git("rev-parse", "HEAD^2", cwd=workflow.worktree(7)) == base
    assert json.loads(_note(workflow).read_text())["head"] == refreshed
    assert not (workflow.state / "merged").exists()


def _tick_rerun_red(handed: GitHub) -> str:
    """Daily flow: a handed-off run fails only because the audit could not reach its advisory
    service, and the tick re-runs its failed jobs once. Returns the main the run tested."""
    old = _main_moves(handed)
    pull_dependencies_red(handed, annotation=UNREACHABLE)
    _advisory_helper(handed)
    _another_first(handed)

    _waits_unrefreshed(handed, _tick(handed))
    assert len(rerun_calls(handed.workflow)) == 1
    return old


def test_the_ticks_own_rerun_green_on_an_older_main_keeps_its_place_behind_another(
    handed: GitHub,
) -> None:
    old = _tick_rerun_red(handed)
    _green_rerun_on(handed, old)

    for _ in range(3):
        _waits_unrefreshed(handed, _tick(handed))
    train = _train(handed)
    assert train["blocked"] == {"7": handed.head} and train["refreshable"] == ["7"], train
    assert train["waiting"] == ["8", "7"]
    assert len(rerun_calls(handed.workflow)) == 1

    _first_leaves(handed)
    _kept(handed, _tick(handed))
    _refreshed_onto(handed, handed.base)


def test_a_partial_rerun_still_running_keeps_the_handoff_behind_another(handed: GitHub) -> None:
    """The tick's re-run of the failed jobs is running: GitHub shows the run in progress."""
    old = _tick_rerun_red(handed)
    _green_rerun_on(handed, old)
    _runs_of(handed).update(status="in_progress", conclusion=None)
    handed.api[f"{_REPO}/actions/runs/20/jobs?per_page=100"]["jobs"][1].update(
        status="in_progress", conclusion=None
    )
    handed.pulls["7"]["statusCheckRollup"] = [
        {"name": "CI result", "status": "IN_PROGRESS", "conclusion": None}
    ]
    handed.save()

    for _ in range(2):
        _waits_unrefreshed(handed, _tick(handed))

    _green_rerun_on(handed, old)
    for _ in range(2):
        _waits_unrefreshed(handed, _tick(handed))
    _first_leaves(handed)
    _kept(handed, _tick(handed))
    _refreshed_onto(handed, handed.base)


def _handoff_while_blocked(github: GitHub) -> None:
    """Daily flow before the tick: the run failed, the line blocked the head, the failed job
    was re-run green on the main it first tested, and the reviewer passes and hands off from
    the kept worktree. Pull request 8 waits ahead of it."""
    workflow = github.workflow
    workflow.git("checkout", "main")
    workflow.git("worktree", "add", str(workflow.worktree(7)), "ticket/seven")
    workflow.list_pull_request(7, draft=False)
    (workflow.state / "pr" / "7").write_text(f"OPEN ticket/seven {github.head}\n")
    workflow.git("checkout", "ticket/seven", cwd=workflow.worktree(7))
    github.pulls["7"]["labels"] = [{"name": "workflow"}]
    old = _main_moves(github)
    folder = _folder(workflow)
    folder.mkdir(parents=True, exist_ok=True)
    # As the line left the red first attempt: waiting, blocked on this head.
    (folder / "merge-train.json").write_text(
        json.dumps({"waiting": ["7"], "blocked": {"7": github.head}})
    )
    _another_first(github)
    _green_rerun_on(github, old)
    _publish(github)
    _handoff(github)
    workflow.run("stop", "review-7")
    github.batch_mode = True
    github.driver_mode = True
    github.save()
    train = _train(github)
    assert train["blocked"] == {"7": github.head} and train["refreshable"] == ["7"], train


def test_a_handoff_made_while_blocked_on_a_green_rerun_keeps_its_place_behind_another(
    github: GitHub,
) -> None:
    _handoff_while_blocked(github)

    for _ in range(3):
        _waits_unrefreshed(github, _tick(github))

    _first_leaves(github)
    _kept(github, _tick(github))
    _refreshed_onto(github, github.base)


def _pull_runs_unreadable(workflow) -> None:
    """GitHub drops the connection when a pull request's CI runs are read, while the flag file
    exists; everything else answers."""
    (workflow.bin / "gh").rename(workflow.bin / "gh-stand-in")
    workflow.fake(
        "gh",
        """#!/bin/bash
if [[ "$1" == api && "$*" == *"ci.yml/runs?"*"event=pull_request"* &&
      -e "@STATE@/github-down" ]]; then
  echo 'Get "https://api.github.com/repos": read tcp: connection reset by peer' >&2
  exit 1
fi
exec "$(dirname "$0")/gh-stand-in" "$@"
""",
    )


def test_one_dropped_github_read_does_not_hand_back_a_refreshable_handoff(github: GitHub) -> None:
    """GitHub drops one tick's read of the line's runs, in a tick after main moved: nothing
    about the pull request changed, so the handoff waiting only for its refresh is still kept
    when GitHub answers again."""
    workflow = github.workflow
    _handoff_while_blocked(github)
    _waits_unrefreshed(github, _tick(github))
    _pull_runs_unreadable(workflow)
    (workflow.state / "github-down").touch()
    # Another merge lands, so this tick reads the line's runs from GitHub again.
    _main_moves(github)

    _waits_unrefreshed(github, _tick(github))
    (workflow.state / "github-down").unlink()
    # What the line saved from the dropped read, shown with a failure below.
    saved = {key: _train(github).get(key) for key in ("blocked", "refreshable")}

    _waits_unrefreshed(github, _tick(github) + f"\nline after the dropped read: {saved}")
    _first_leaves(github)
    _kept(github, _tick(github))
    _refreshed_onto(github, github.base)


def test_a_tick_and_a_second_handoff_call_at_once_keep_the_member(github: GitHub) -> None:
    """The reviewer's handoff is repeated while a tick runs: the line still holds the member
    once, refreshable, and the next tick keeps it."""
    workflow = github.workflow
    _handoff_while_blocked(github)
    outputs: list[str] = []

    def tick() -> None:
        done = workflow.run_script("tick")
        outputs.append(done.stdout + done.stderr)

    running = threading.Thread(target=tick)
    running.start()
    again = workflow.run_script("merge-when-current", "--handoff", "7", cwd=workflow.worktree(7))
    running.join(timeout=300)

    _kept(github, "".join(outputs) + again.stdout + again.stderr)
    train = _train(github)
    assert train["waiting"].count("7") == 1, train
    _waits_unrefreshed(github, _tick(github))


def test_the_budget_reserve_reached_mid_line_does_not_hand_back_a_refreshable_handoff(
    github: GitHub,
) -> None:
    """The shared GitHub budget reaches its reserve while the line reads its members, after
    main moved: the tick holds back, and the handoff waiting only for its refresh is kept once
    the budget resets."""
    from pathlib import Path

    from workflow.test_merge_driver import _low_budget_after

    workflow = github.workflow
    _handoff_while_blocked(github)
    _waits_unrefreshed(github, _tick(github))
    _main_moves(github)
    # The reserve is reached by the read of pull request 8's runs, just before 7's.
    _low_budget_after(workflow, f"api --paginate {_runs(_OTHER)}")

    _waits_unrefreshed(github, _tick(github))
    assert (workflow.state / "reserve-reached").exists()
    budget = Path(workflow.env["HOME"]) / ".local/state/workflow/github-budget/budget.json"
    budget.write_text(json.dumps({"core": {"remaining": 499, "reset": 0}}))
    saved = {key: _train(github).get(key) for key in ("blocked", "refreshable")}

    _waits_unrefreshed(github, _tick(github) + f"\nline after the reserve: {saved}")
    _first_leaves(github)
    _kept(github, _tick(github))
    _refreshed_onto(github, github.base)
