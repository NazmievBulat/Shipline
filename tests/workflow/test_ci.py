"""CI runs every test file in exactly one of its shards, on GitHub's own runners, and its last job,
`CI`, passes only when every shard passed."""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github/workflows/ci.yml"


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
    assert re.search(r"^    if: always\(\)$", job, re.M)
    assert 'test "${{ needs.tests.result }}" = success' in job


def test_ci_runs_for_pull_requests_main_and_the_merge_queue() -> None:
    triggers = _CI.read_text().split("\njobs:")[0]

    assert re.search(r"^  pull_request:$", triggers, re.M)
    assert re.search(r"^  push:\n    branches: \[main\]$", triggers, re.M)
    assert re.search(r"^  merge_group:$", triggers, re.M)
    assert "pull_request_target" not in triggers
    assert re.search(r"^permissions:\n  contents: read$", _CI.read_text(), re.M)


def test_the_tests_use_a_tmux_that_leaves_each_pane_where_it_was_started() -> None:
    """A tmux built with systemd support moves each new pane into a scope of its own, racing the
    scope a session starts its command in; the session's memory limit is then lost."""
    text = _CI.read_text()

    assert "./configure --prefix=\"$HOME/.local/tmux\" --disable-systemd" in text
    assert 'echo "$HOME/.local/tmux/bin" >> "$GITHUB_PATH"' in text
    assert 'if ldd "$(command -v tmux)" | grep -q libsystemd; then' in text
