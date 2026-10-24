"""CI runs every test file in exactly one of its shards, on GitHub's own runners, and its last job,
`CI`, passes only when every job that ran passed. It writes the merge gate's notes with
`ci_proof.py`, as the example CI of a repository using Shipline does (examples/ci.yml)."""

import json
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github/workflows/ci.yml"
_EXAMPLE = _ROOT / "examples/ci.yml"


def _job(path: Path, job: str) -> str:
    """The lines of one job of a workflow, up to the next job."""
    jobs = path.read_text().split("\njobs:\n", 1)[1]
    found = re.search(rf"^  {job}:\n.*?(?=^  [\w-]+:$|\Z)", jobs, re.M | re.S)
    assert found, job
    return found[0]


def _shards() -> dict[int, list[str]]:
    """The test files of each shard, as the matrix of ci.yml lists them."""
    shards: dict[int, list[str]] = {}
    shard = None
    for line in _CI.read_text().splitlines():
        if found := re.fullmatch(r"\s+- shard: (\d+)", line):
            shard = int(found[1])
            shards[shard] = []
        elif shard is not None and (
            found := re.fullmatch(r"\s+(tests/(?:workflow|tools)/\S+\.py)", line)
        ):
            shards[shard].append(found[1])
    return shards


def test_ci_runs_four_shards() -> None:
    assert sorted(_shards()) == [1, 2, 3, 4]
    assert all(_shards().values())


def _test_files(root: Path) -> list[str]:
    """The files pytest collects tests from under `tests/workflow` and `tests/tools`: its default
    `python_files`, in every folder below them too."""
    found = set()
    for folder in (root / "tests/workflow", root / "tests/tools"):
        found |= {*folder.rglob("test_*.py"), *folder.rglob("*_test.py")}
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


@pytest.mark.parametrize("path", [_CI, _EXAMPLE], ids=["own", "example"])
def test_ci_runs_only_on_github_s_own_runners(path: Path) -> None:
    runners = re.findall(r"^\s+runs-on: (.+)$", path.read_text(), re.M)

    assert runners and set(runners) == {"ubuntu-latest"}
    assert "self-hosted" not in path.read_text()


# The step that fails the summing-up job when any job that ran did not pass.
_EVERY_JOB_PASSED = """\
      - name: Every job that ran passed
        env:
          RESULTS: ${{ join(needs.*.result, ' ') }}
        run: |
          for result in $RESULTS; do
            case "$result" in
              success | skipped) ;;
              *) echo "a job ended: $result"; exit 1 ;;
            esac
          done
"""


@pytest.mark.parametrize(
    "path,job,name", [(_CI, "ci", "CI"), (_EXAMPLE, "result", "CI result")], ids=["own", "example"]
)
def test_the_summing_up_check_needs_every_job(path: Path, job: str, name: str) -> None:
    text = _job(path, job)

    assert re.search(rf"^    name: {name}$", text, re.M)
    assert re.search(r"^    needs: \[plan, tests\]$", text, re.M)
    assert re.search(r"^    if: always\(\)$", text, re.M)
    assert _EVERY_JOB_PASSED in text


@pytest.mark.parametrize("ended", ["success skipped", "success failure", "cancelled success"])
def test_the_summing_up_check_passes_only_when_every_job_that_ran_passed(ended: str) -> None:
    script = _EVERY_JOB_PASSED.split("run: |\n", 1)[1]
    done = subprocess.run(
        ["bash", "-c", "\n".join(line.removeprefix(" " * 10) for line in script.splitlines())],
        env={"PATH": "/usr/bin:/bin", "RESULTS": ended},
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == (0 if ended == "success skipped" else 1), done.stdout


@pytest.mark.parametrize("path", [_CI, _EXAMPLE], ids=["own", "example"])
def test_the_planner_writes_the_gate_s_notes_and_main_s_inheritance(path: Path) -> None:
    plan = _job(path, "plan")

    assert re.search(r'^    name: "CI: what to run"$', plan, re.M)
    assert "run: python3 scripts/workflow/ci_proof.py notes --whole server_tests\n" in plan
    assert "        if: github.event_name == 'push'\n" in plan
    assert "run: python3 scripts/workflow/ci_proof.py inherited ci\n" in plan
    assert "inherited: ${{ steps.inherited.outputs.inherited || '[]' }}" in plan
    # The only job that reads more than the repository, and only what main's inheritance reads.
    assert re.search(r"^    permissions:\n      contents: read\n(?:      #.*\n)*"
                     r"      actions: read\n      checks: read\n", plan, re.M)


@pytest.mark.parametrize("path", [_CI, _EXAMPLE], ids=["own", "example"])
def test_the_test_job_runs_the_config_s_command_unless_main_inherited_it(path: Path) -> None:
    tests = _job(path, "tests")

    assert re.search(r'^    name: "CI: server tests[" (]', tests, re.M)
    assert re.search(r"^    needs: plan$", tests, re.M)
    assert (
        "    if: >-\n"
        "      ${{ !contains(fromJSON(needs.plan.outputs.inherited), 'CI: server tests') }}\n"
        in tests
    )
    assert "python3 scripts/workflow/ci_proof.py test" in tests


def test_shipline_names_its_own_test_command() -> None:
    config = json.loads((_ROOT / "shipline.json").read_text())

    assert config["commands"]["test"] == "python -m pytest -rs"


def test_the_example_s_jobs_carry_the_config_s_default_names() -> None:
    text = _EXAMPLE.read_text()
    names = re.findall(r"^    name: (.+)$", text, re.M)

    assert names == ['"CI: what to run"', '"CI: server tests"', "CI result"]
    assert re.search(r"^  pull_request:\n  push:\n    branches: \[main\]\n", text, re.M)
    assert re.search(r"^  workflow_dispatch:$", text, re.M)
    assert "pull_request_target" not in text
    assert re.search(r"^permissions:\n  contents: read$", text, re.M)


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
