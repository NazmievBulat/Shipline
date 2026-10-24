"""Merge safety for squash."""


import pytest

from workflow.merge_gate_fixtures import (
    GitHub,
    _REPO,
    _batch_call,
    _batch_setup,
    _batch_state,
    _finish_batch,
    _merges,
    _numbers,
    github,
)


@pytest.mark.parametrize("number", ["7", "9"])
def test_a_member_requiring_migration_compatibility_uses_the_single_path(
    github: GitHub, number: str
) -> None:
    _batch_setup(github, compatibility=number)
    job = {
        "id": 30000,
        "name": "CI: older version on new tables",
        "status": "completed",
        "conclusion": "success",
    }
    github.api[f"{_REPO}/actions/runs/{int(number) * 1000}/jobs?per_page=100"]["jobs"].append(job)
    done = _batch_call(github)
    assert done.returncode == (2 if number == "7" else 3), done.stdout + done.stderr
    if number == "7":
        assert _batch_state(github)["prefixes"] is None
        assert github.workflow.calls("gh workflow run") == []
    else:
        assert _numbers(github) == ["7", "8"]
        _finish_batch(github)
        assert _batch_call(github).returncode == 0
        assert github.pulls["9"]["state"] == "OPEN"
        github.workflow.git("checkout", "ticket/9")
        assert _batch_call(github, "9").returncode == 2
        head = github.workflow.git("rev-parse", "HEAD")
        base = github.workflow.git("rev-parse", "origin/main")
        github.pulls["9"]["headRefOid"] = head
        github.ci(base, head, run_id=3000)
        github.api[f"{_REPO}/actions/runs/3000/jobs?per_page=100"]["jobs"].append(job)
        done = _batch_call(github, "9")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _merges(github) == ["7", "8", "9"]


@pytest.mark.parametrize("refusals", [1, 3])
def test_a_refused_member_squash_is_retried_a_bounded_number_of_times(
    github: GitHub, refusals: int
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.refuse_merge = {"8": refusals}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    merges = _merges(github)
    if refusals == 1:
        assert merges == ["7", "8", "8", "9"]
        assert len(github.workflow.calls("sleep 10")) == 1
    else:
        assert merges == ["7", "8", "8", "8"]
        assert len(github.workflow.calls("sleep 10")) == 2
        assert "the squash of batch member 8 failed" in done.stdout
        assert github.pulls["8"]["state"] == "OPEN" and github.pulls["9"]["state"] == "OPEN"
        assert _batch_state(github)["prefixes"] is None
        assert _batch_state(github)["waiting"] == ["8", "9"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("red", "main is red; remaining members use the single fix rule"),
        ("pushed-over", "main did not match the prefix squash"),
        ("on-view-of-8", "main moved during the merge train"),
    ],
)
def test_main_changing_after_the_callers_squash_reports_that_merge(
    github: GitHub, change: str, message: str
) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.after_main = {"7": change}
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pull request 7 merged in a batch (merged in this call: 7)" in done.stdout
    assert message in done.stdout
    assert _merges(github) == ["7"]
    train = _batch_state(github)
    assert train["waiting"] == ["8", "9"]
    # Rebuilt on the main the gate cannot vouch for only once it is green again.
    assert train["prefixes"] is None or change == "on-view-of-8"


def test_main_moving_while_a_squash_is_refused_stops_without_retrying(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.refuse_merge = {"8": 1}
    github.refusal_moves_main = True
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "main moved before batch member 8 merged; it needs fresh tests" in done.stdout
    assert github.workflow.calls("sleep 10") == []
    assert _merges(github) == ["7", "8"]
    assert _batch_state(github)["prefixes"] is None


def test_main_moving_during_the_retry_wait_stops_before_the_next_squash(github: GitHub) -> None:
    _batch_setup(github)
    assert _batch_call(github).returncode == 3
    _finish_batch(github)
    github.refuse_merge = {"8": 1}
    origin = github.workflow.root / "origin.git"
    # Another push lands while the gate waits to retry.
    (github.workflow.state / "on-sleep").write_text(
        f'moved=$(git --git-dir "{origin}" commit-tree "main^{{tree}}" -p main -m push)\n'
        f'git --git-dir "{origin}" update-ref refs/heads/main "$moved"\n'
        f'rm "{github.workflow.state}/on-sleep"\n'
    )
    done = _batch_call(github)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "main moved before batch member 8 merged; it needs fresh tests" in done.stdout
    assert len(github.workflow.calls("sleep 10")) == 1
    assert _merges(github) == ["7", "8"]
