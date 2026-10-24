"""Breaking tests for pull request 35: an advisory that fails only main's dependency audit.

They replay daily uses with neutral names: a repository whose audit job covers a JavaScript
and a Python lock file, a new advisory found on main, the one-line lock bump that fixes it, an
advisory service that does not answer, and the wake that has to name all of it."""

import json
from pathlib import Path
from typing import Any

import pytest
from workflow.test_merge_when_current import (
    _REPO,
    GitHub,
    _advisories_notice,
    _advisory_config,
    _advisory_watch,
    _main_audit_failed,
    _pull_passed_over_mains_advisory,
    _train,
    _two_stale_pulls,
)
from workflow.test_merge_when_current import (
    github as github_fixture,
)

github = github_fixture

_AUDIT = "CI: audit"
_BOTH = [
    "GHSA-aaaa-bbbb-cccc in example-js-lib (prototype pollution)",
    "PYSEC-2026-1 in example-py-lib (request smuggling)",
]


def _folder(github: GitHub) -> Path:
    return github.workflow.repo / ".local/workflow"


def _main_runs(github: GitHub) -> tuple[dict[str, Any], dict[str, Any]]:
    run = github.api[
        f"{_REPO}/actions/workflows/ci.yml/runs?head_sha={github.base}&per_page=100"
    ]["workflow_runs"][0]
    listed = github.api[f"{_REPO}/actions/runs/10/jobs?per_page=100"]
    return run, listed


def _status(github: GitHub) -> str:
    github.save()
    done = github.workflow.run_script("main-status")
    assert done.returncode == 0, done.stderr
    return done.stdout


def _told(github: GitHub) -> str | None:
    """What the wake is to tell at any hour, then taken as told (the wake removes it)."""
    pending = _folder(github) / "main-status.pending"
    if not pending.exists():
        return None
    text = pending.read_text()
    pending.unlink()
    return text


def test_both_ecosystems_advisories_are_named_and_a_green_pull_request_merges(
    github: GitHub,
) -> None:
    """The daily case: main's audit failed on one JavaScript and one Python advisory alone."""
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice(_BOTH))
    _pull_passed_over_mains_advisory(github)

    done = github.run()

    assert done.returncode == 0, done.stdout + done.stderr
    assert (github.workflow.state / "merged").read_text() == "7"
    line = _status(github)
    assert line.startswith("main: advisory since"), line
    for advisory in _BOTH:
        assert advisory in line, line


def test_main_entering_advisory_while_its_run_goes_on_is_told(github: GitHub) -> None:
    """Green main, then a new run of it (a merge or a scheduled run) in which the audit fails
    on a new advisory first, while the slower jobs still run, then completes. The owner must be
    told that main is now advisory, as into red."""
    _advisory_config(github)
    assert _status(github).startswith("main: green")
    _told(github)
    _main_audit_failed(github, _advisories_notice(_BOTH))
    run, listed = _main_runs(github)
    failed = listed["jobs"]
    run.update(status="in_progress", conclusion=None)
    listed["jobs"] = [
        {**failed[0], "status": "queued", "conclusion": None},
        failed[1],
        {
            "id": 14,
            "name": "CI: server tests",
            "status": "in_progress",
            "conclusion": None,
        },
    ]
    assert _status(github).startswith("main: pending")
    _told(github)

    run.update(status="completed", conclusion="failure")
    listed["jobs"] = [
        *failed,
        {
            "id": 14,
            "name": "CI: server tests",
            "status": "completed",
            "conclusion": "success",
        },
    ]
    line = _status(github)

    assert line.startswith("main: advisory since"), line
    told = _told(github)
    assert told is not None and told.startswith("main: advisory"), (
        f"into advisory was not noted for the wake; main-status: {line!r}"
    )


def test_main_entering_red_the_same_way_is_told(github: GitHub) -> None:
    """The same path into red, for comparison: it is noted."""
    assert _status(github).startswith("main: green")
    _told(github)
    github.main("failure")
    run, listed = _main_runs(github)
    run.update(status="in_progress", conclusion=None)
    listed["jobs"][0].update(status="in_progress", conclusion=None)
    assert _status(github).startswith("main: red")
    told = _told(github)
    assert told is not None and told.startswith("main: red")


def test_main_leaving_advisory_after_the_fix_lands_is_told(github: GitHub) -> None:
    """Advisory main, told; then the lock bump lands: main's run on it goes on for a while,
    then passes. The owner must be told that main left the advisory state."""
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice(_BOTH))
    assert _status(github).startswith("main: advisory")
    assert _told(github) is not None

    github.main()
    run, listed = _main_runs(github)
    run.update(status="in_progress", conclusion=None)
    for job in listed["jobs"]:
        job.update(status="in_progress", conclusion=None)
    pending = _status(github)
    assert pending.startswith("main: pending"), pending
    seen = [_told(github)]

    github.main()
    line = _status(github)
    assert line.startswith("main: green"), line
    seen.append(_told(github))

    assert any(seen), f"leaving advisory was never noted for the wake: {seen!r}"


def test_main_leaving_red_the_same_way_is_told(github: GitHub) -> None:
    """The same path out of red, for comparison: it is noted."""
    github.main("failure")
    assert _status(github).startswith("main: red")
    _told(github)
    github.main()
    run, listed = _main_runs(github)
    run.update(status="in_progress", conclusion=None)
    for job in listed["jobs"]:
        job.update(status="in_progress", conclusion=None)
    assert _status(github).startswith("main: pending")
    assert _told(github) is not None


def _fix_and_another(github: GitHub, *, fix_tested_on_current_main: bool) -> None:
    """Pull request 7 joined first, a plain change; 8 is the lock bump, its audit clean. Main
    moved after 8's CI ran when `fix_tested_on_current_main` is false: on an advisory main the
    line keeps merging, so that is the usual case."""
    _advisory_config(github)
    old = github.base
    _two_stale_pulls(github)
    head = github.pulls["8"]["headRefOid"]
    github.ci(
        github.base,
        head,
        run_id=30,
        tested_base=None if fix_tested_on_current_main else old,
    )
    github.api[f"{_REPO}/actions/runs/30/jobs?per_page=100"]["jobs"].append(
        {"id": 39, "name": _AUDIT, "status": "completed", "conclusion": "success"}
    )
    github.api[f"{_REPO}/check-runs/39/annotations?per_page=100"] = []
    _main_audit_failed(github, _advisories_notice(_BOTH))
    folder = _folder(github)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "merge-train.json").write_text(json.dumps({"waiting": ["7", "8"]}))


def test_the_lock_bump_tested_on_the_current_main_goes_first(github: GitHub) -> None:
    _fix_and_another(github, fix_tested_on_current_main=True)

    assert _status(github).startswith("main: advisory")

    train = _train(github)
    assert train["first"] == "8"
    assert train["priorities"]["8"] == "blocker"


def test_the_lock_bump_still_goes_first_after_main_moved_under_it(
    github: GitHub,
) -> None:
    """On an advisory main other pull requests keep merging, so main moves while the lock
    bump's CI runs. It must still go first, as the fix of an advisory main."""
    _fix_and_another(github, fix_tested_on_current_main=False)

    assert _status(github).startswith("main: advisory")

    train = _train(github)
    assert train["first"] == "8", train
    assert train["priorities"]["8"] == "blocker", train


_OUTAGE = {
    "title": "Advisory service unreachable",
    "annotation_level": "failure",
    "message": "Refused because PyPI's advisory service could not be reached after 3 tries; "
    "nothing was found wrong with these packages.",
}


def test_an_advisory_service_outage_keeps_main_red_by_its_own_rule(
    github: GitHub,
) -> None:
    _advisory_config(github)
    _advisory_watch(github)
    _main_audit_failed(github, _OUTAGE)
    _pull_passed_over_mains_advisory(github)

    line = _status(github)
    done = github.run()

    assert line.startswith("main: red"), line
    assert f"{_AUDIT} could not reach PyPI's advisory service" in line, line
    assert done.returncode == 1
    assert not (github.workflow.state / "merged").exists()


@pytest.mark.parametrize("order", ["outage first", "advisories first"])
def test_an_outage_beside_an_advisory_is_not_advisories_alone(
    github: GitHub, order: str
) -> None:
    """One lock file found an advisory, the other's service did not answer: the Python lock
    file was not audited, so main is not failing on advisories alone."""
    _advisory_config(github)
    _advisory_watch(github)
    notes = [_OUTAGE, _advisories_notice(_BOTH[:1])]
    _main_audit_failed(github, *(notes if order == "outage first" else notes[::-1]))

    line = _status(github)

    assert not line.startswith("main: advisory"), line


def test_the_train_status_names_main_s_state_without_reading_github(
    github: GitHub,
) -> None:
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice(_BOTH))
    assert _status(github).startswith("main: advisory")

    assert _train(github)["main"] == "advisory"
    (_folder(github) / "main-status").write_text("")
    assert _train(github)["main"] in (None, "unknown"), _train(github)["main"]


def test_an_annotation_read_lost_to_github_makes_main_unknown_not_red(
    github: GitHub,
) -> None:
    _advisory_config(github)
    _main_audit_failed(github, _advisories_notice(_BOTH))
    del github.api[f"{_REPO}/check-runs/12/annotations?per_page=100"]

    line = _status(github)

    assert not line.startswith("main: advisory"), line


def test_a_lock_bump_labelled_blocker_goes_first_after_main_moved_under_it(
    github: GitHub,
) -> None:
    """For scope: with the blocker label of its own, the moved-under lock bump goes first."""
    _fix_and_another(github, fix_tested_on_current_main=False)
    github.pulls["8"]["labels"] = [{"name": "priority: blocker"}]

    assert _status(github).startswith("main: advisory")

    train = _train(github)
    assert train["first"] == "8", train
