"""Tries to break #29: the reconcile cache must never hide an availability change."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from workflow.test_merge_when_current import (
    GitHub,
    _refreshed_first,
    _two_stale_pulls,
    _waiting_reviewers,
)
from workflow.test_merge_when_current import github as github_fixture
from workflow.test_train_reads import _context

github = github_fixture


def _line(github: GitHub) -> dict[str, str]:
    """Two members, 7 ahead of 8, both reviewers waiting; the tick's context."""
    second = _two_stale_pulls(github)
    workflow = github.workflow
    _waiting_reviewers(github)
    assert github.run().returncode == 2
    assert workflow.run_script("merge-when-current", "8", cwd=second).returncode == 3
    _refreshed_first(github)
    env = _context(github)
    Path(env["WORKFLOW_SESSION_SNAPSHOT"]).write_text(workflow.run("list").stdout)
    return env


def _limited(snapshot: str, name: str) -> str:
    """The snapshot with `name` stopped on the usage limit."""
    lines = []
    for line in snapshot.splitlines():
        fields = line.split()
        if fields and fields[0] == name:
            fields[1] = "limit"
            line = " ".join(fields) + " usage limit"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _status(github: GitHub, env: dict[str, str]) -> str:
    done = github.workflow.run_script("merge-when-current", "--train-status", env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _during_scan(github: GitHub, action: str) -> Path:
    """Runs `action` once, at the scan's first poll of runs: after 7's availability was read."""
    workflow = github.workflow
    armed = workflow.state / "armed"
    armed.touch()
    workflow.fake("gh-real", (workflow.bin / "gh").read_text())
    workflow.fake(
        "gh",
        f'#!/bin/bash\nif [[ "$*" == *actions/runs* && -e "{armed}" ]]; then\n'
        f'  rm "{armed}"; {action}\nfi\nexec "{workflow.bin / "gh-real"}" "$@"\n',
    )
    return armed


def _cached_then_uncached(github: GitHub, env: dict[str, str]) -> tuple[str, str]:
    again = _status(github, env)
    (Path(env["WORKFLOW_TRAIN_TICK"]) / "reconciled.json").unlink(missing_ok=True)
    return again, _status(github, env)


@pytest.mark.parametrize("change", ["stopped", "resent"])
def test_a_reviewer_stopped_or_resent_during_the_scan(
    github: GitHub, change: str
) -> None:
    # The tick's own session start or a usage-limit stop rewrites the session snapshot.
    env = _line(github)
    snapshot = Path(env["WORKFLOW_SESSION_SNAPSHOT"])
    running = snapshot.read_text()
    limited = _limited(running, "review-7")
    assert limited != running
    if change == "resent":
        snapshot.write_text(limited)
    replacement = github.workflow.state / "replacement"
    replacement.write_text(limited if change == "stopped" else running)
    armed = _during_scan(github, f'cp "{replacement}" "{snapshot}"')

    _status(github, env)
    assert not armed.exists()
    again, uncached = _cached_then_uncached(github, env)

    assert json.loads(uncached)["first"] == ("8" if change == "stopped" else "7")
    assert again == uncached


@pytest.mark.parametrize("change", ["created", "cleared"])
def test_an_interaction_note_changed_during_the_scan(
    github: GitHub, change: str
) -> None:
    env = _line(github)
    note = github.workflow.notes(7) / "review-7.interaction-blocked"
    if change == "cleared":
        note.touch()
    action = f'rm -f "{note}"' if change == "cleared" else f'touch "{note}"'
    armed = _during_scan(github, action)

    _status(github, env)
    assert not armed.exists()
    again, uncached = _cached_then_uncached(github, env)

    assert json.loads(uncached)["first"] == ("8" if change == "created" else "7")
    assert again == uncached


@pytest.mark.parametrize("comeback", ["resent", "answered"])
def test_a_passed_over_member_is_served_at_its_old_place_at_the_next_pass(
    github: GitHub, comeback: str
) -> None:
    # Within one tick: passed over, its reviewer comes back, the next pass serves it first.
    env = _line(github)
    snapshot = Path(env["WORKFLOW_SESSION_SNAPSHOT"])
    running = snapshot.read_text()
    note = github.workflow.notes(7) / "review-7.owner-blocked"
    if comeback == "resent":
        snapshot.write_text(_limited(running, "review-7"))
    else:
        note.touch()
    passed = json.loads(_status(github, env))
    assert passed["first"] == "8"
    assert passed["waiting"][:2] == ["7", "8"]
    # The same inputs reuse the pass.
    assert json.loads(_status(github, env))["first"] == "8"

    if comeback == "resent":
        snapshot.write_text(running)
    else:
        note.unlink()
    served = json.loads(_status(github, env))
    assert served["first"] == "7"
    assert served["waiting"][:2] == ["7", "8"]


@pytest.mark.parametrize("change", ["created", "cleared"])
def test_a_note_changed_between_the_saved_key_and_the_last_comparison(
    github: GitHub, tmp_path: Path, change: str
) -> None:
    """The window the PR's own test does not reach: after the scan, while the key is taken."""
    env = _line(github)
    note = github.workflow.notes(7) / "review-7.owner-blocked"
    if change == "cleared":
        note.touch()
    hook = tmp_path / "hook"
    hook.mkdir()
    armed = tmp_path / "armed"
    armed.touch()
    scripts = Path(__file__).resolve().parents[2] / "scripts/workflow"
    # Each signature digest has the train file, main and the pulls first; the second one in
    # a reconcile that missed the cache is the saved key. The note changes right after it.
    (hook / "sitecustomize.py").write_text(
        f"""
import os, sys
from pathlib import Path
sys.path.insert(0, {str(scripts)!r})
import train_reads
_digest = train_reads.digest
_count = [0]
def digest(value):
    result = _digest(value)
    if isinstance(value, list) and len(value) == 6 and isinstance(value[2], dict):
        _count[0] += 1
        armed = Path({str(armed)!r})
        if _count[0] == 2 and armed.exists():
            armed.unlink()
            note = Path({str(note)!r})
            note.unlink() if {change == "cleared"!r} else note.touch()
    return result
train_reads.digest = digest
"""
    )
    hooked = {**env, "PYTHONPATH": str(hook)}

    _status(github, hooked)
    assert not armed.exists()
    again, uncached = _cached_then_uncached(github, env)

    assert json.loads(uncached)["first"] == ("8" if change == "created" else "7")
    assert again == uncached


def test_unchanged_inputs_reuse_the_pass_without_github(github: GitHub) -> None:
    env = _line(github)
    first = _status(github, env)
    before = github.workflow.calls("gh")
    for _ in range(3):
        assert _status(github, env) == first
    assert github.workflow.calls("gh") == before


def test_two_status_calls_at_once_while_a_note_changes(github: GitHub) -> None:
    env = _line(github)
    note = github.workflow.notes(7) / "review-7.owner-blocked"
    armed = _during_scan(github, f'sleep 1; touch "{note}"')
    with ThreadPoolExecutor(2) as pool:
        calls = [
            pool.submit(
                github.workflow.run_script,
                "merge-when-current",
                "--train-status",
                env=env,
            )
            for _ in range(2)
        ]
        done = [call.result() for call in calls]
    assert all(call.returncode == 0 for call in done), [call.stderr for call in done]
    assert not armed.exists()
    again, uncached = _cached_then_uncached(github, env)

    assert json.loads(uncached)["first"] == "8"
    assert again == uncached
