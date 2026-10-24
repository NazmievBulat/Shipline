"""CI runs every test file in exactly one of its shards, on GitHub's own runners, and its last job,
`CI`, passes only when every job that ran passed. It writes the merge gate's notes with
`ci_proof.py`, as the example CI of a repository using Shipline does (examples/ci.yml). A draft
pull request runs none of the jobs of either."""

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github/workflows/ci.yml"
_EXAMPLE = _ROOT / "examples/ci.yml"
_E2E = _ROOT / "examples/e2e.yml"
_SCRIPTS = _ROOT / "scripts/workflow"

# A pull request's run while it is a draft; main's push and the merge queue have no pull request.
_NOT_A_DRAFT = "github.event_name != 'pull_request' || github.event.pull_request.draft == false"


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
            found := re.fullmatch(r"\s+(tests/(?:workflow|runner)/\S+\.py)", line)
        ):
            shards[shard].append(found[1])
    return shards


def test_ci_runs_four_shards() -> None:
    assert sorted(_shards()) == [1, 2, 3, 4]
    assert all(_shards().values())


def _test_files(root: Path) -> list[str]:
    """The files pytest collects tests from under `tests/workflow` and `tests/runner`: its
    default `python_files`, in every folder below them too."""
    found = {
        path
        for folder in (root / "tests/workflow", root / "tests/runner")
        for pattern in ("test_*.py", "*_test.py")
        for path in folder.rglob(pattern)
    }
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


def test_a_test_file_of_the_runner_pool_is_counted(tmp_path: Path) -> None:
    runner = tmp_path / "tests/runner"
    runner.mkdir(parents=True)
    (runner / "test_names.py").touch()
    (tmp_path / "tests/workflow").mkdir()
    (tmp_path / "tests/workflow/test_tick.py").touch()

    assert _test_files(tmp_path) == ["tests/runner/test_names.py", "tests/workflow/test_tick.py"]


def test_pytest_collects_the_runner_pool_s_tests() -> None:
    text = (_ROOT / "pyproject.toml").read_text()

    assert re.search(r'^testpaths = \["tests/workflow", "tests/runner"\]$', text, re.M)


def test_ci_runs_only_on_github_s_own_runners() -> None:
    """Shipline's own; the examples' main runners are a repository's own choice
    (test_ci_runner_choice.py)."""
    runners = re.findall(r"^\s+runs-on: (.+)$", _CI.read_text(), re.M)

    assert runners and set(runners) == {"ubuntu-latest"}
    assert "self-hosted" not in _CI.read_text()


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
    assert _condition(text) == f"always() && ({_NOT_A_DRAFT})"
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


_NOT_INHERITED = "!contains(fromJSON(needs.plan.outputs.inherited), 'CI: server tests')"


@pytest.mark.parametrize("path", [_CI, _EXAMPLE], ids=["own", "example"])
def test_the_test_job_runs_the_config_s_command_unless_main_inherited_it(path: Path) -> None:
    tests = _job(path, "tests")

    assert re.search(r'^    name: "CI: server tests[" (]', tests, re.M)
    assert re.search(r"^    needs: plan$", tests, re.M)
    assert _condition(tests) == f"${{{{ ({_NOT_A_DRAFT}) && {_NOT_INHERITED} }}}}"
    assert "python3 scripts/workflow/ci_proof.py test" in tests


def test_shipline_names_its_own_test_command() -> None:
    config = json.loads((_ROOT / "shipline.json").read_text())

    assert config["commands"]["test"] == "python -m pytest -rs"


def test_the_example_s_jobs_carry_the_config_s_default_names() -> None:
    text = _EXAMPLE.read_text()
    names = re.findall(r"^    name: (.+)$", text, re.M)

    assert names == ['"CI: what to run"', '"CI: server tests"', "CI result"]
    assert re.search(
        r"^  pull_request:\n    types: \[opened, synchronize, reopened, ready_for_review\]\n"
        r"  push:\n    branches: \[main\]\n",
        text,
        re.M,
    )
    assert re.search(r"^  workflow_dispatch:$", text, re.M)
    assert "pull_request_target" not in text
    assert re.search(r"^permissions:\n  contents: read$", text, re.M)


def test_the_example_e2e_s_jobs_carry_the_names_the_gate_reads() -> None:
    text = _E2E.read_text()
    names = re.findall(r"^    name: (.+)$", text, re.M)

    assert names == ['"E2E: what to run"', "playwright"]
    assert re.search(
        r"^  pull_request:\n    types: \[opened, synchronize, reopened, ready_for_review\]\n"
        r"  push:\n    branches: \[main\]\n",
        text,
        re.M,
    )
    assert re.search(r"^  workflow_dispatch:$", text, re.M)
    assert "pull_request_target" not in text
    assert re.search(r"^permissions:\n  contents: read$", text, re.M)


def test_the_example_e2e_planner_notes_the_whole_browser_suite_and_main_s_inheritance() -> None:
    plan = _job(_E2E, "plan")

    assert "run: python3 scripts/workflow/ci_proof.py notes --whole browser_suite\n" in plan
    assert "        if: github.event_name == 'push'\n" in plan
    assert "run: python3 scripts/workflow/ci_proof.py inherited e2e\n" in plan
    assert "inherited: ${{ steps.inherited.outputs.inherited || '[]' }}" in plan
    assert re.search(r"^    permissions:\n      contents: read\n(?:      #.*\n)*"
                     r"      actions: read\n      checks: read\n", plan, re.M)


def test_the_example_e2e_runs_the_browser_command_unless_main_inherited_it() -> None:
    browser = _job(_E2E, "playwright")

    assert re.search(r"^    needs: plan$", browser, re.M)
    assert "!contains(fromJSON(needs.plan.outputs.inherited), 'playwright') }}" in browser
    assert "run: python3 scripts/workflow/ci_proof.py browser\n" in browser
    assert "persist-credentials: false" in browser


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


def _condition(job: str) -> str | None:
    """The condition a job runs under (its job-level `if`), a folded one on one line."""
    found = re.search(r"^    if: (?:>-\n((?:      .*\n)+)|(.+)$)", job, re.M)
    if not found:
        return None
    return found[2] or " ".join(line.strip() for line in found[1].splitlines())


def _jobs(path: Path) -> list[str]:
    jobs = path.read_text().split("\njobs:\n", 1)[1]
    return re.findall(r"^  ([\w-]+):$", jobs, re.M)


def test_a_folded_condition_is_read_on_one_line() -> None:
    job = "  tests:\n    if: >-\n      ${{ one\n      && two }}\n    runs-on: ubuntu-latest\n"

    assert _condition(job) == "${{ one && two }}"
    assert _condition("  ci:\n    if: always()\n") == "always()"
    assert _condition("  ci:\n    steps:\n      - if: never\n") is None


@pytest.mark.parametrize(
    "path,result", [(_CI, "ci"), (_EXAMPLE, "result")], ids=["own", "example"]
)
def test_every_job_is_skipped_while_the_pull_request_is_a_draft(path: Path, result: str) -> None:
    """A draft's run ends at once and takes no runner for the tests; marking the pull request
    ready (`ready_for_review`) starts its run on the current head."""
    jobs = {job: _condition(_job(path, job)) for job in _jobs(path)}

    assert sorted(jobs) == sorted(["plan", "tests", result])
    assert jobs["plan"] == _NOT_A_DRAFT
    assert all(condition and f"({_NOT_A_DRAFT})" in condition for job, condition in jobs.items()
               if job != "plan")


def test_every_e2e_job_is_skipped_while_the_pull_request_is_a_draft() -> None:
    """The example E2E, as the example CI: a draft takes no runner for its browser tests, and a
    draft's run never cancels a ready one."""
    jobs = {job: _condition(_job(_E2E, job)) for job in _jobs(_E2E)}
    group = re.search(r"^concurrency:\n  group: (.+)\n", _E2E.read_text(), re.M)

    assert sorted(jobs) == ["plan", "playwright"]
    assert jobs["plan"] == _NOT_A_DRAFT
    assert jobs["playwright"] == (
        f"${{{{ ({_NOT_A_DRAFT}) && !contains(fromJSON(needs.plan.outputs.inherited), "
        "'playwright') }}"
    )
    assert group and group[1] == (
        "e2e-${{ github.event_name }}-${{ github.event_name == 'pull_request'"
        " && format('{0}-draft-{1}', github.ref, github.event.pull_request.draft)"
        " || github.sha }}"
    )


def test_main_s_push_and_the_merge_queue_are_not_filtered() -> None:
    """Only a pull request has a draft: the condition lets every other run through, and main's
    push and the merge queue keep their triggers as they were."""
    triggers = _CI.read_text().split("\njobs:")[0]

    assert re.search(r"^  push:\n    branches: \[main\]\n  merge_group:\n\n", triggers, re.M)


@pytest.mark.parametrize("path", [_CI, _EXAMPLE], ids=["own", "example"])
def test_a_draft_s_run_never_cancels_a_ready_run(path: Path) -> None:
    """A last push to a draft and its marking ready come moments apart: were the draft's run
    queued second in the same group, it would cancel the ready run and leave the head with no CI.
    A draft's runs have a group of their own; a newer push still cancels an older run."""
    text = path.read_text()
    group = re.search(r"^concurrency:\n  group: (.+)\n  cancel-in-progress: (.+)$", text, re.M)

    assert group
    assert group[1] == (
        "ci-${{ github.event_name }}-${{ github.event_name == 'pull_request'"
        " && format('{0}-draft-{1}', github.ref, github.event.pull_request.draft)"
        " || github.sha }}"
    )
    assert group[2] == "${{ github.event_name == 'pull_request' }}"


@pytest.fixture(scope="module")
def gate() -> Any:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return importlib.import_module("merge_gate")
    finally:
        sys.path.remove(str(_SCRIPTS))


def _verdict(gate: Any, run: str, jobs: str, result: str) -> str:
    """The merge gate's verdict of a completed pull request run, its result job by the name the
    gate requires."""
    parts = [{"id": 2, "name": "CI: what to run", "status": "completed", "conclusion": jobs}]
    parts += [
        {"id": 3, "name": f"CI: server tests ({shard} of 4)", "status": "completed",
         "conclusion": jobs}
        for shard in range(1, 5)
    ]
    parts.append({"id": 4, "name": gate.CI_RESULT, "status": "completed", "conclusion": result})
    state, _ = gate.verdict(
        {"id": 1, "status": "completed", "conclusion": run, "head_sha": "head"}, parts
    )
    return state


def test_a_ready_pull_request_s_passed_run_is_a_pass_to_the_merge_gate(gate: Any) -> None:
    assert _verdict(gate, "success", "success", "success") == "green"


@pytest.mark.parametrize("run", ["skipped", "success"])
def test_a_draft_s_skipped_run_is_never_a_pass_to_the_merge_gate(gate: Any, run: str) -> None:
    """Every job of a draft's run is skipped, the result job too: its head has no green CI
    result, however GitHub concludes the run itself."""
    assert _verdict(gate, run, "skipped", "skipped") != "green"


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
