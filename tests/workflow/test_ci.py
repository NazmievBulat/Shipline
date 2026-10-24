"""CI runs every test file in exactly one of its shards, on GitHub's own runners, and its last job,
`CI`, passes only when every shard passed. A draft pull request runs none of its jobs."""

import importlib
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github/workflows/ci.yml"
_SCRIPTS = _ROOT / "scripts/workflow"

# A pull request's run while it is a draft; main's push and the merge queue have no pull request.
_NOT_A_DRAFT = "github.event_name != 'pull_request' || github.event.pull_request.draft == false"


def _shards() -> dict[int, list[str]]:
    """The test files of each shard, as the matrix of ci.yml lists them."""
    shards: dict[int, list[str]] = {}
    shard = None
    for line in _CI.read_text().splitlines():
        if found := re.fullmatch(r"\s+- shard: (\d+)", line):
            shard = int(found[1])
            shards[shard] = []
        elif shard is not None and (found := re.fullmatch(r"\s+(tests/workflow/\S+\.py)", line)):
            shards[shard].append(found[1])
    return shards


def test_ci_runs_four_shards() -> None:
    assert sorted(_shards()) == [1, 2, 3, 4]
    assert all(_shards().values())


def _test_files(root: Path) -> list[str]:
    """The files pytest collects tests from under `tests/workflow`: its default `python_files`,
    in every folder below it too."""
    folder = root / "tests/workflow"
    found = {*folder.rglob("test_*.py"), *folder.rglob("*_test.py")}
    return sorted(str(path.relative_to(root)) for path in found)


def test_every_test_file_runs_in_exactly_one_shard() -> None:
    listed = [path for files in _shards().values() for path in files]

    assert sorted(listed) == _test_files(_ROOT)


def test_a_test_file_in_a_folder_below_is_counted(tmp_path: Path) -> None:
    below = tmp_path / "tests/workflow/below"
    below.mkdir(parents=True)
    (below / "test_nested.py").touch()
    (below / "nested_test.py").touch()

    assert _test_files(tmp_path) == [
        "tests/workflow/below/nested_test.py",
        "tests/workflow/below/test_nested.py",
    ]


def test_ci_runs_only_on_github_s_own_runners() -> None:
    runners = re.findall(r"^\s+runs-on: (.+)$", _CI.read_text(), re.M)

    assert runners and set(runners) == {"ubuntu-latest"}
    assert "self-hosted" not in _CI.read_text()


def test_the_check_the_merge_queue_requires_needs_every_shard() -> None:
    text = _CI.read_text()
    job = text[text.index("\n  ci:\n") :]

    assert re.search(r"^    name: CI$", job, re.M)
    assert re.search(r"^    needs: tests$", job, re.M)
    assert _jobs()["ci"] == f"always() && ({_NOT_A_DRAFT})"
    assert 'test "${{ needs.tests.result }}" = success' in job


def test_ci_runs_for_pull_requests_main_and_the_merge_queue() -> None:
    triggers = _CI.read_text().split("\njobs:")[0]

    assert re.search(
        r"^  pull_request:\n    types: \[opened, synchronize, reopened, ready_for_review\]$",
        triggers,
        re.M,
    )
    assert re.search(r"^  push:\n    branches: \[main\]$", triggers, re.M)
    assert re.search(r"^  merge_group:$", triggers, re.M)
    assert "pull_request_target" not in triggers
    assert re.search(r"^permissions:\n  contents: read$", _CI.read_text(), re.M)


def _jobs() -> dict[str, str | None]:
    """Each job of ci.yml by its key, with the condition it runs under (its job-level `if`)."""
    text = _CI.read_text().split("\njobs:\n", 1)[1]
    found: dict[str, str | None] = {}
    for key, body in re.findall(r"^  (\w[\w-]*):\n((?:(?:    .*)?\n)*)", text + "\n", re.M):
        condition = re.search(r"^    if: (.+)$", body, re.M)
        found[key] = condition[1] if condition else None
    return found


def test_ci_has_the_shards_and_the_check_the_merge_queue_requires() -> None:
    assert sorted(_jobs()) == ["ci", "tests"]


def test_every_job_is_skipped_while_the_pull_request_is_a_draft() -> None:
    """A draft's run ends at once and takes no runner for the tests; marking the pull request
    ready (`ready_for_review`) starts its run on the current head."""
    jobs = _jobs()

    assert jobs["tests"] == _NOT_A_DRAFT
    assert jobs["ci"] == f"always() && ({_NOT_A_DRAFT})"
    assert all(condition and _NOT_A_DRAFT in condition for condition in jobs.values())


def test_main_s_push_and_the_merge_queue_are_not_filtered() -> None:
    """Only a pull request has a draft: the condition lets every other run through, and main's
    push and the merge queue keep their triggers as they were."""
    triggers = _CI.read_text().split("\njobs:")[0]

    assert _NOT_A_DRAFT.startswith("github.event_name != 'pull_request' || ")
    assert re.search(r"^  push:\n    branches: \[main\]\n  merge_group:\n\n", triggers, re.M)


@pytest.fixture(scope="module")
def verdict() -> Any:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("merge_gate").verdict
    finally:
        sys.path.remove(str(_SCRIPTS))


@pytest.mark.parametrize("conclusion", ["skipped", "success"])
def test_a_draft_s_skipped_run_is_never_a_pass_to_the_merge_gate(
    verdict: Any, conclusion: str
) -> None:
    """Every job of a draft's run is skipped, `CI` too: its head has no green CI result, however
    GitHub concludes the run itself."""
    run = {"id": 1, "status": "completed", "conclusion": conclusion, "head_sha": "head"}
    parts = [
        {"id": 2, "name": "tests", "status": "completed", "conclusion": "skipped"},
        {"id": 3, "name": "CI", "status": "completed", "conclusion": "skipped"},
    ]

    state, _ = verdict(run, parts)

    assert state != "green"


def test_the_tests_use_a_tmux_that_leaves_each_pane_where_it_was_started() -> None:
    """A tmux built with systemd support moves each new pane into a scope of its own, racing the
    scope a session starts its command in; the session's memory limit is then lost."""
    text = _CI.read_text()

    assert "./configure --prefix=\"$HOME/.local/tmux\" --disable-systemd" in text
    assert 'echo "$HOME/.local/tmux/bin" >> "$GITHUB_PATH"' in text
    assert 'if ldd "$(command -v tmux)" | grep -q libsystemd; then' in text


def test_the_rules_tell_a_worker_that_a_draft_runs_no_ci() -> None:
    rules = " ".join((_SCRIPTS / "RULES.md").read_text().split())
    worker = rules[rules.index('## "Implement pull request N"') : rules.index("### Codex workers")]

    assert "A draft pull request runs no CI" in worker
    assert "run the tests in the session before marking ready" in worker
