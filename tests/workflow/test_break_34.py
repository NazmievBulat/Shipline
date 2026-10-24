"""Breaking tests for the lost-check wake before the merge gate (pull request 34).

Each replays a daily flow of a reviewer or audit that waits for its checks before it joined the
merge line, while GitHub loses one of them, and checks the tick wakes it once per head.
"""

import concurrent.futures
import json
import subprocess
import sys
from typing import Any

import pytest
from workflow.test_lost_check import (
    _lose,
    _lost_notes,
    _run,
    _run_reads,
    _stamp,
    _waiting_audit,
    _waiting_reviewer,
    _wake_log,
)
from workflow.test_merge_when_current import GitHub, train_status
from workflow.test_merge_when_current import github as github_fixture

github = github_fixture

_REPO = "example-org/example-app"


def _check(name: str, conclusion: str = "SUCCESS", **extra: Any) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "startedAt": _stamp(4000),
        "detailsUrl": f"https://github.com/{_REPO}/actions/runs/20/job/30",
        **extra,
    }


def _shards(github: GitHub, *, failed: int | None = None) -> None:
    """The server tests in four shards beside the CI result; one may have failed."""
    rollup = github.pulls["7"]["statusCheckRollup"]
    for shard in range(1, 5):
        rollup.append(
            _check(f"CI: server tests ({shard})", "FAILURE" if shard == failed else "SUCCESS")
        )
    github.save()


def _queued_after_restart(github: GitHub, name: str, started: Any, run_id: int = 20) -> None:
    """A job of a completed run still listed as queued: its runner restarted."""
    github.pulls["7"]["statusCheckRollup"].append(
        {
            "__typename": "CheckRun",
            "name": name,
            "status": "QUEUED",
            "conclusion": None,
            "startedAt": started,
            "detailsUrl": f"https://github.com/{_REPO}/actions/runs/{run_id}/job/{run_id + 9}",
        }
    )
    github.save()


def _told(github: GitHub) -> list[str]:
    return [
        line
        for prompt in github.workflow.sent()
        for line in prompt.splitlines()
        if line.startswith("lost check:")
    ]


def _woken(github: GitHub, name: str = "review-7") -> list[str]:
    return github.workflow.calls(f"tmux {name} send-keys")


@pytest.mark.parametrize("started", [None, "0001-01-01T00:00:00Z"])
def test_a_shard_queued_for_hours_after_a_runner_restart_wakes_the_reviewer_once(
    github: GitHub, started: Any
) -> None:
    """Daily: one server-test shard stays queued; the tick runs every 3 minutes."""
    workflow = github.workflow
    workflow.orchestrator("busy")
    _shards(github)
    _queued_after_restart(github, "CI: server tests (5)", started)
    _run(github, finished_ago=2 * 3600)
    _waiting_reviewer(github)

    runs = [workflow.run_script("tick") for _ in range(3)]

    assert [run.returncode for run in runs] == [0, 0, 0], runs[0].stdout + runs[0].stderr
    sent = _woken(github)
    assert len(sent) == 2, sent
    assert "GitHub lost a check on " + github.head in sent[0]
    assert _told(github) == ["lost check: CI: server tests (5) of run 20 on pull request 7"]
    # Read once, by the first tick; later ticks see the mark and read nothing.
    assert _run_reads(github) == 1


def test_a_browser_test_part_lost_in_its_own_run_wakes_the_reviewer(github: GitHub) -> None:
    """Daily: a browser test part of the E2E run is lost; CI itself finished green."""
    workflow = github.workflow
    workflow.orchestrator("busy")
    _shards(github)
    _lose(github, run_id=30, name="E2E: part 2")
    _run(github, 30)
    _waiting_reviewer(github)

    done = workflow.run_script("tick")
    workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(_woken(github)) == 2
    assert _told(github) == ["lost check: E2E: part 2 of run 30 on pull request 7"]
    assert _run_reads(github, 30) == 1
    assert _run_reads(github, 20) == 0


def test_a_shard_and_a_browser_part_lost_in_two_runs_wake_once(github: GitHub) -> None:
    workflow = github.workflow
    _lose(github, run_id=20, name="CI: server tests (2)")
    _lose(github, run_id=30, name="E2E: part 1")
    _run(github, 20)
    _run(github, 30)
    _waiting_reviewer(github)

    done = workflow.run_script("tick")
    workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(_woken(github)) == 2
    assert (_run_reads(github, 20), _run_reads(github, 30)) == (1, 1)
    assert len(_lost_notes(github)) + len(_told(github)) == 1


def test_a_check_lost_while_the_reviewer_reads_the_diff_wakes_it_once_it_waits(
    github: GitHub,
) -> None:
    """Daily: the loss comes before the reviewer waits; nothing is read until it does."""
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)
    wait = workflow.notes(7) / "review-7.checks-wait"
    wait.unlink()  # still reading the diff, no round published, no wait yet

    reading = workflow.run_script("tick")

    assert reading.returncode == 0, reading.stdout + reading.stderr
    assert _woken(github) == []
    assert _run_reads(github) == 0

    wait.touch()  # it ran `notify waiting`
    waiting = [workflow.run_script("tick") for _ in range(2)]

    assert [run.returncode for run in waiting] == [0, 0]
    assert len(_woken(github)) == 2
    assert f"review-7 lost check on {github.head}; reviewer nudged" in _wake_log(github)
    assert train_status(github)["waiting"] == []


def test_a_lost_shard_beside_a_failed_shard_wakes_the_reviewer(github: GitHub) -> None:
    """Daily: a shard failed for real and the runner of another restarted, losing it. The
    checks are as finished as they will ever be: the failure decides. A reviewer that is never
    woken sits idle until a person looks."""
    workflow = github.workflow
    _shards(github, failed=3)
    _lose(github, name="CI: server tests (4)")
    _run(github, conclusion="failure")
    _waiting_reviewer(github)

    runs = [workflow.run_script("tick") for _ in range(3)]

    assert [run.returncode for run in runs] == [0, 0, 0], runs[0].stdout + runs[0].stderr
    assert len(_woken(github)) == 2, (
        "the reviewer was never woken: "
        + repr(sorted(path.name for path in workflow.repo.glob(".local/workflow/review-7.*")))
    )


@pytest.mark.xfail(
    strict=True,
    reason="not counted: an audit outside the line is not woken for a failed check alone "
    "either (the pull request's Choices), so this is out of its scope",
)
def test_a_lost_audit_shard_beside_a_failed_one_wakes_the_audit(github: GitHub) -> None:
    """The same for an audit, which the tick never nudges after 50 idle minutes either."""
    workflow = github.workflow
    _shards(github, failed=1)
    _lose(github, name="CI: server tests (2)")
    _run(github, conclusion="failure")
    _waiting_audit(github)

    runs = [workflow.run_script("tick") for _ in range(3)]

    assert [run.returncode for run in runs] == [0, 0, 0], runs[0].stdout + runs[0].stderr
    assert len(_woken(github, "audit-7")) == 2, "the audit was never woken"


def test_a_shard_re_run_in_part_and_lost_wakes_the_reviewer(github: GitHub) -> None:
    """A failed shard was re-run alone; its new job is lost. While the re-run runs, nobody is
    woken; once the run completed 10 minutes ago, the reviewer is."""
    workflow = github.workflow
    _shards(github)
    _lose(github, name="CI: server tests (3)", finished_ago=1800)
    # The re-run attempt is still running: the run itself is in progress again.
    _run(github, status="in_progress", conclusion=None, finished_ago=60)
    _waiting_reviewer(github)

    running = workflow.run_script("tick")

    assert running.returncode == 0, running.stdout + running.stderr
    assert _woken(github) == []

    _run(github, finished_ago=1200)  # the re-run attempt completed 20 minutes ago
    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(_woken(github)) == 2
    # One read per tick: the cache of the first tick is gone.
    assert _run_reads(github) == 2


def test_an_unreadable_run_fails_one_tick_and_the_next_wakes(github: GitHub) -> None:
    """GitHub answers badly once (an outage): the tick fails, the next one wakes once."""
    workflow = github.workflow
    _lose(github)
    _run(github)
    good = github.api[f"repos/{_REPO}/actions/runs/20"]
    github.api[f"repos/{_REPO}/actions/runs/20"] = None
    github.save()
    _waiting_reviewer(github)

    failed = workflow.run_script("tick")
    github.api[f"repos/{_REPO}/actions/runs/20"] = good
    github.save()
    later = [workflow.run_script("tick") for _ in range(2)]

    assert failed.returncode == 1
    assert [run.returncode for run in later] == [0, 0], later[0].stdout + later[0].stderr
    assert len(_woken(github)) == 2
    assert _run_reads(github) == 2


def test_two_ticks_at_once_wake_the_reviewer_once(github: GitHub) -> None:
    workflow = github.workflow
    workflow.orchestrator("busy")
    _lose(github)
    _run(github)
    _waiting_reviewer(github)

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        both = list(pool.map(lambda _: workflow.run_script("tick"), range(2)))
    workflow.run_script("tick")

    assert 0 in [run.returncode for run in both], both[0].stderr + both[1].stderr
    assert len(_woken(github)) == 2
    assert len(_told(github)) == 1


def test_a_reviewer_woken_then_calling_the_gate_tells_the_orchestrator_once(
    github: GitHub,
) -> None:
    """After the wake the reviewer calls the gate on the same head: still one line."""
    workflow = github.workflow
    workflow.orchestrator("busy")
    _lose(github)
    _run(github)
    _waiting_reviewer(github)

    assert workflow.run_script("tick").returncode == 0
    github.run()  # the gate, as the wake's message asks
    workflow.run_script("tick")

    assert _told(github) == ["lost check: CI: server tests of run 20 on pull request 7"]


def test_the_helper_outside_a_tick_with_no_state_folder_yet(github: GitHub) -> None:
    """A missing state folder: the note is still written, and the run is read."""
    workflow = github.workflow
    _lose(github)
    _run(github)
    folder = workflow.root / "fresh" / "state"

    done = subprocess.run(
        ["python3", "-B", str(workflow.script.parent / "tick_lost_check.py"), str(folder), "7"],
        input=json.dumps(github.pulls["7"]),
        env={**workflow.env, "WORKFLOW_TRAIN_TICK": ""},
        capture_output=True,
        text=True,
        cwd=workflow.repo,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    [note] = (folder / "lost-checks.pending").iterdir()
    assert json.loads(note.read_text())["check"] == "CI: server tests"
    assert _run_reads(github) == 1


def test_the_cache_file_of_the_tick_is_not_world_writable(github: GitHub) -> None:
    """The tick's own folder holds the cache; it is made by mktemp, readable by its owner only."""
    workflow = github.workflow
    _lose(github)
    _run(github)
    _waiting_reviewer(github)
    probe = workflow.root / "probe"
    workflow.fake(
        "python3",
        "#!/usr/bin/env bash\n"
        f'if [[ $2 == */tick_lost_check.py ]]; then "{sys.executable}" "$@"; '
        f'code=$?; stat -c "%a %n" "$WORKFLOW_TRAIN_TICK" "$WORKFLOW_TRAIN_TICK"/lost-check-runs.json'
        f' >>"{probe}"; exit $code; fi\n'
        f'exec {sys.executable} "$@"\n',
    )

    done = workflow.run_script("tick")

    assert done.returncode == 0, done.stdout + done.stderr
    folder, cache = probe.read_text().splitlines()[:2]
    assert folder.split()[0] == "700", folder
    assert not int(cache.split()[0][-1]) & 2, cache
