"""Where each job of the example workflows runs (examples/ci.yml, examples/e2e.yml), when the
repository turns main's runners on (`vars.CI_MAIN_RUNNER == 'on'`): a push to main on main's
runners; the merge gate's first run of a prefix in the merge line's lane on them, but only when
its planner found one free there (`ci_proof.py main-lane`), so a prefix never waits on main's
runners nor ahead of a push to main; every other run, and every run with the switch off, on the
ordinary choice, GitHub's own runners in the examples. Each `runs-on` and condition is read from
the file and worked out as GitHub would (`github_expression.py`)."""

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
from github_expression import ExpressionError, evaluate, value

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = [_ROOT / "examples/ci.yml", _ROOT / "examples/e2e.yml"]
# Loaded from their files, so the scripts' folder is not put on the path of every other test.
_SPEC = importlib.util.spec_from_file_location(
    "workflow_config", _ROOT / "scripts/workflow/workflow_config.py"
)
assert _SPEC and _SPEC.loader
workflow_config = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workflow_config)

HOSTED = "ubuntu-latest"
# The examples show the default scratch prefix, and the default label of the example config's
# runner prefix, which has no default itself.
PREFIX = workflow_config.DEFAULTS["merge"]["scratch_prefix"]
_EXAMPLE_RUNNER = json.loads((_ROOT / "examples/shipline.json").read_text())["runner"]
_POOL = workflow_config.Runner(
    labels=(), identity_folder="", prefix=_EXAMPLE_RUNNER["prefix"], label=_EXAMPLE_RUNNER["label"]
)
MAIN = ["self-hosted", _POOL.main_label]
LANE = ["self-hosted", _POOL.main_batch_label]


def _folded(text: str) -> str:
    if not text.startswith(">-"):
        return text
    return " ".join(part.strip() for part in text.splitlines()[1:])


def _runs_on(path: Path) -> dict[str, str]:
    """Each job's `runs-on`, a folded value joined into one line."""
    jobs = path.read_text().split("\njobs:\n", 1)[1]
    found: dict[str, str] = {}
    for job in re.finditer(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:$|\Z)", jobs, re.M | re.S):
        line = re.search(r"^    runs-on: (>-\n(?:      .*\n)+|[^>\n].*)", job[2], re.M)
        assert line, f"{path.name}: job {job[1]} names no runner"
        found[job[1]] = _folded(line[1])
    return found


def _lane_step(path: Path) -> str:
    """The condition of the planner's step that looks for main's lane."""
    plan = path.read_text().split("\n  plan:\n", 1)[1].split("\n\n  ", 1)[0]
    step = re.search(
        r"^      - name: [^\n]*\n(?:        (?!run:).*\n)*?"
        r"        if: (>-\n(?:          .*\n)+|.*\n)"
        r'(?:        (?!run:).*\n)*        run: python3 "?\S*scripts/workflow/ci_proof\.py"? main-lane\n',
        plan,
        re.M,
    )
    assert step, f"{path.name}: no planner step looks for main's lane"
    return _folded(step[1].rstrip("\n"))


def _github(
    event: str, ref: str, switch: str | None, attempt: int = 1, lane: str | None = None
) -> dict[str, Any]:
    return {
        # GitHub gives the attempt as text.
        "github": {"event_name": event, "ref": ref, "run_attempt": str(attempt)},
        "vars": {} if switch is None else {"CI_MAIN_RUNNER": switch},
        "needs": {"plan": {"outputs": {} if lane is None else {"main_lane": lane}}},
    }


_BATCH = f"refs/heads/{PREFIX}0123456789abcdef0123456789abcdef"
_DISPATCH = "workflow_dispatch"

# The jobs that never take the merge line's lane: the planner, which looks for it, and CI's
# summing-up job, which would ask for it long after the planner looked.
_OFF_THE_LANE = ("plan", "result")

# (event, ref, the switch, the attempt, the planner's `main_lane`, where the planner and the
# summing-up job run, where every other job runs)
_CASES = {
    "a merge-line dispatch, main's lane free": (_DISPATCH, _BATCH, "on", 1, "true", HOSTED, LANE),
    # Main's runners offline, busy, or held for a push to main: the ordinary choice.
    "a merge-line dispatch, main's lane not free": (
        _DISPATCH, _BATCH, "on", 1, "false", HOSTED, HOSTED
    ),
    # A re-run, of the whole run or of its failed jobs, keeps the planner's first answer: it
    # takes the ordinary choice, so no attempt but the first can wait on main's runners.
    "a merge-line re-run": (_DISPATCH, _BATCH, "on", 2, "true", HOSTED, HOSTED),
    "a merge-line dispatch, main's runners off": (
        _DISPATCH, _BATCH, "off", 1, "true", HOSTED, HOSTED
    ),
    "a merge-line dispatch, no switch": (_DISPATCH, _BATCH, None, 1, "true", HOSTED, HOSTED),
    "a push to main": ("push", "refs/heads/main", "on", 1, None, MAIN, MAIN),
    "a push to main run again": ("push", "refs/heads/main", "on", 2, None, MAIN, MAIN),
    "a push to main, main's runners off": (
        "push", "refs/heads/main", None, 1, None, HOSTED, HOSTED
    ),
    "a pull request": ("pull_request", "refs/pull/7/merge", "on", 1, None, HOSTED, HOSTED),
    "a manual dispatch on main": (_DISPATCH, "refs/heads/main", "on", 1, None, HOSTED, HOSTED),
    "a push to a scratch branch": ("push", _BATCH, "on", 1, None, HOSTED, HOSTED),
}


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.name)
@pytest.mark.parametrize("case", _CASES)
def test_each_job_runs_where_its_run_belongs(path: Path, case: str) -> None:
    event, ref, switch, attempt, lane, planner, others = _CASES[case]
    jobs = _runs_on(path)

    assert "plan" in jobs and len(jobs) > 1
    context = _github(event, ref, switch, attempt, lane)
    for job, runner in jobs.items():
        assert value(runner, context) == (planner if job in _OFF_THE_LANE else others), job


# (event, ref, the switch, the attempt, whether the planner looks for main's lane)
_LOOKS = {
    "the gate's first dispatch": (_DISPATCH, _BATCH, "on", 1, True),
    "its re-run": (_DISPATCH, _BATCH, "on", 2, False),
    "the switch off": (_DISPATCH, _BATCH, "off", 1, False),
    "no switch": (_DISPATCH, _BATCH, None, 1, False),
    "a pull request": ("pull_request", "refs/pull/7/merge", "on", 1, False),
    "a push to main": ("push", "refs/heads/main", "on", 1, False),
    "a push to a scratch branch": ("push", _BATCH, "on", 1, False),
    "a manual dispatch on another branch": (
        _DISPATCH, "refs/heads/feature/example", "on", 1, False
    ),
    # Only the gate's own branches: a tag, or a branch that only holds the prefix, is not one.
    "a dispatch on a tag of the prefix's name": (
        _DISPATCH, f"refs/tags/{PREFIX}0123", "on", 1, False
    ),
    "a dispatch on a branch holding the prefix": (
        _DISPATCH, f"refs/heads/old/{PREFIX}0123", "on", 1, False
    ),
}


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.name)
@pytest.mark.parametrize("case", _LOOKS)
def test_the_planner_looks_for_main_s_lane_only_on_the_gate_s_first_dispatch(
    path: Path, case: str
) -> None:
    event, ref, switch, attempt, looks = _LOOKS[case]

    assert bool(evaluate(_lane_step(path), _github(event, ref, switch, attempt))) is looks


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.name)
def test_github_reads_the_scratch_prefix_without_its_case(path: Path) -> None:
    """So the examples tell a repository to keep every case of it to the gate's identity."""
    ref = "refs/heads/" + _BATCH.removeprefix("refs/heads/").upper()

    assert evaluate(_lane_step(path), _github(_DISPATCH, ref, "on"))
    assert "in any case" in path.read_text()


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.name)
def test_a_prefix_falls_back_to_the_repository_s_own_choice(path: Path) -> None:
    """A repository with a pool puts its own choice where the examples say GitHub's runners:
    with the lane not free, on a re-run, or with the switch off, a prefix's job takes it."""
    own = "example-app-home"
    for job, runner in _runs_on(path).items():
        assert runner.count(f"'{HOSTED}'") == 1, job
        copied = runner.replace(f"'{HOSTED}'", f"'{own}'")
        for attempt, lane, switch in ((1, "false", "on"), (2, "true", "on"), (1, "true", "off")):
            assert value(copied, _github(_DISPATCH, _BATCH, switch, attempt, lane)) == own, job


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.name)
def test_main_and_the_merge_line_ask_for_labels_of_their_own(path: Path) -> None:
    """Main keeps its place: no prefix job asks for main's label, so a push to main never waits
    behind one; the lender takes the lane off main's idle runners while a push to main waits."""
    text = path.read_text()
    named = {
        job: re.findall(r'"self-hosted", "([^"]+)"', runner)
        for job, runner in _runs_on(path).items()
    }

    assert _EXAMPLE_RUNNER["label"] == f"{_EXAMPLE_RUNNER['prefix']}-home"
    assert f"'refs/heads/{PREFIX}'" in text
    for job in _OFF_THE_LANE:
        assert named.pop(job, [MAIN[1]]) == [MAIN[1]], job
    assert named and all(labels == [MAIN[1], LANE[1]] for labels in named.values())
    assert LANE[1] in _EXAMPLE_RUNNER["labels"]


def test_the_rules_say_where_the_gate_s_scratch_runs_run() -> None:
    rules = (_ROOT / "scripts/workflow/RULES.md").read_text()
    prefixes = next(part for part in rules.split("\n\n") if "each as a prefix" in part)

    assert re.search(
        r"first run of a prefix takes main's runners when the\s+repository has them and one is"
        r"\s+free",
        prefixes,
    )


@pytest.mark.parametrize(
    "expression,context,expected",
    [
        ("vars.SWITCH == 'on' && 'a' || 'b'", {"vars": {"SWITCH": "ON"}}, "a"),
        ("vars.SWITCH == 'on' && 'a' || 'b'", {"vars": {}}, "b"),
        ("startsWith(github.ref, 'refs/heads/x-')", {"github": {"ref": "refs/heads/X-1"}}, True),
        ("!(1 == 2) && fromJSON('[\"a\", \"b\"]')", {}, ["a", "b"]),
        ("'it''s'", {}, "it's"),
        ("github['event_name'] != 'push'", {"github": {"event_name": "push"}}, False),
        # GitHub reads a property's name without its case.
        ("github.EVENT_NAME", {"github": {"event_name": "push"}}, "push"),
        ("vars.ci_main_runner", {"vars": {"CI_MAIN_RUNNER": "on"}}, "on"),
        # Two kinds of value are compared as numbers.
        ("vars.SWITCH == 'on'", {"vars": {}}, False),
        ("'1' == 1", {}, True),
        ("null == 0", {}, True),
        ("null == ''", {}, True),
        ("'on' == 0", {}, False),
        ("true == 1", {}, True),
        # `<` binds tighter than `==`: 2 == true, as numbers 2 == 1.
        ("2 == 1 < 3", {}, False),
        ("!(2 != 1 < 3)", {}, False),
    ],
)
def test_expressions_are_worked_out_as_github_does(
    expression: str, context: dict[str, Any], expected: Any
) -> None:
    assert evaluate(expression, context) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "github.ref ~ 'x'",
        "unknown(1)",
        "'a' &&",
        "nothing.x",
        "startsWith('a')",
        "contains('a', 'a',)",
    ],
)
def test_an_expression_it_does_not_know_is_refused(expression: str) -> None:
    with pytest.raises(ExpressionError):
        evaluate(expression, {"github": {}})


def test_a_value_with_text_around_its_expression_is_refused() -> None:
    with pytest.raises(ExpressionError):
        value("runner-${{ github.ref }}", {"github": {}})
    assert value("ubuntu-latest", {}) == HOSTED
