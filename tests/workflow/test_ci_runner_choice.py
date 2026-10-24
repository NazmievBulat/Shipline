"""Where each job of the example workflows runs (examples/ci.yml, examples/e2e.yml): the merge
gate's scratch runs and a push to main on main's runners when the repository turns them on
(`vars.CI_MAIN_RUNNER == 'on'`), so a prefix never waits behind pull requests' own CI; every
other run on GitHub's own runners. Each job's `runs-on` is read from the file and worked out as
GitHub would (`github_expression.py`)."""

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
# The examples show the default scratch prefix and main machine, and the example config's
# runner prefix, which has no default.
PREFIX = workflow_config.DEFAULTS["merge"]["scratch_prefix"]
_EXAMPLE_RUNNER = json.loads((_ROOT / "examples/shipline.json").read_text())["runner"]
MAIN = [
    "self-hosted",
    workflow_config.Runner(
        labels=(),
        identity_folder="",
        prefix=_EXAMPLE_RUNNER["prefix"],
        main_machine=workflow_config.DEFAULTS["runner"]["main_machine"],
    ).main_label,
]


def _runs_on(path: Path) -> dict[str, str]:
    """Each job's `runs-on`, a folded value joined into one line."""
    jobs = path.read_text().split("\njobs:\n", 1)[1]
    found: dict[str, str] = {}
    for job in re.finditer(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:$|\Z)", jobs, re.M | re.S):
        line = re.search(r"^    runs-on: (>-\n(?:      .*\n)+|[^>\n].*)", job[2], re.M)
        assert line, f"{path.name}: job {job[1]} names no runner"
        text = line[1]
        if text.startswith(">-"):
            text = " ".join(part.strip() for part in text.splitlines()[1:])
        found[job[1]] = text
    return found


def _github(event: str, ref: str, switch: str | None) -> dict[str, Any]:
    return {
        "github": {"event_name": event, "ref": ref, "head_ref": ""},
        "vars": {} if switch is None else {"CI_MAIN_RUNNER": switch},
    }


_BATCH = f"refs/heads/{PREFIX}0123456789abcdef0123456789abcdef"

# (event, ref, the switch, where every job runs)
_CASES = {
    "a merge-line dispatch": ("workflow_dispatch", _BATCH, "on", MAIN),
    "a push to main": ("push", "refs/heads/main", "on", MAIN),
    "a pull request": ("pull_request", "refs/pull/7/merge", "on", HOSTED),
    "a manual dispatch on another branch": (
        "workflow_dispatch",
        "refs/heads/feature/example",
        "on",
        HOSTED,
    ),
    "a manual dispatch on main": ("workflow_dispatch", "refs/heads/main", "on", HOSTED),
    "a merge-line dispatch, main's runners off": ("workflow_dispatch", _BATCH, "off", HOSTED),
    "a merge-line dispatch, no switch": ("workflow_dispatch", _BATCH, None, HOSTED),
    "a push to main, main's runners off": ("push", "refs/heads/main", None, HOSTED),
    # Only the gate's own branches: a tag, or a branch that only holds the prefix, is not one.
    "a dispatch on a tag of the prefix's name": (
        "workflow_dispatch",
        f"refs/tags/{PREFIX}0123",
        "on",
        HOSTED,
    ),
    "a dispatch on a branch holding the prefix": (
        "workflow_dispatch",
        f"refs/heads/old/{PREFIX}0123",
        "on",
        HOSTED,
    ),
    # A push to a scratch branch is not the gate's dispatch.
    "a push to a scratch branch": ("push", _BATCH, "on", HOSTED),
}


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.name)
@pytest.mark.parametrize("case", _CASES)
def test_each_job_runs_where_its_run_belongs(path: Path, case: str) -> None:
    event, ref, switch, expected = _CASES[case]
    jobs = _runs_on(path)

    assert jobs
    for job, runner in jobs.items():
        assert value(runner, _github(event, ref, switch)) == expected, job


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.name)
def test_a_pull_request_from_a_branch_of_the_prefix_s_name_stays_hosted(path: Path) -> None:
    """A fork may name its branch anything: only the gate's dispatch reaches main's runners."""
    context = _github("pull_request", "refs/pull/7/merge", "on")
    context["github"]["head_ref"] = f"{PREFIX}0123456789abcdef0123456789abcdef"

    for job, runner in _runs_on(path).items():
        assert value(runner, context) == HOSTED, job


@pytest.mark.parametrize("path", _EXAMPLES, ids=lambda path: path.name)
def test_the_examples_name_the_config_s_default_prefix_and_label(path: Path) -> None:
    text = path.read_text()

    assert _EXAMPLE_RUNNER["main_machine"] == workflow_config.DEFAULTS["runner"]["main_machine"]

    assert f"'refs/heads/{PREFIX}'" in text
    assert re.findall(r'"self-hosted", "([^"]+)"', text) == [MAIN[1]] * len(_runs_on(path))


def test_the_rules_say_where_the_gate_s_scratch_runs_run() -> None:
    rules = (_ROOT / "scripts/workflow/RULES.md").read_text()
    prefixes = next(part for part in rules.split("\n\n") if "each as a prefix" in part)

    assert re.search(r"scratch runs use main's runners\s+when the repository has them", prefixes)


@pytest.mark.parametrize(
    "expression,context,expected",
    [
        ("vars.SWITCH == 'on' && 'a' || 'b'", {"vars": {"SWITCH": "ON"}}, "a"),
        ("vars.SWITCH == 'on' && 'a' || 'b'", {"vars": {}}, "b"),
        ("startsWith(github.ref, 'refs/heads/x-')", {"github": {"ref": "refs/heads/X-1"}}, True),
        ("!(1 == 2) && fromJSON('[\"a\", \"b\"]')", {}, ["a", "b"]),
        ("'it''s'", {}, "it's"),
        ("github['event_name'] != 'push'", {"github": {"event_name": "push"}}, False),
    ],
)
def test_expressions_are_worked_out_as_github_does(
    expression: str, context: dict[str, Any], expected: Any
) -> None:
    assert evaluate(expression, context) == expected


@pytest.mark.parametrize("expression", ["github.ref ~ 'x'", "unknown(1)", "'a' &&", "nothing.x"])
def test_an_expression_it_does_not_know_is_refused(expression: str) -> None:
    with pytest.raises(ExpressionError):
        evaluate(expression, {"github": {}})


def test_a_value_with_text_around_its_expression_is_refused() -> None:
    with pytest.raises(ExpressionError):
        value("runner-${{ github.ref }}", {"github": {}})
    assert value("ubuntu-latest", {}) == HOSTED
