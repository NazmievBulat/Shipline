"""Breaking tests for the merge line's runner choice (examples/ci.yml, examples/e2e.yml), as an
installation with a runner pool uses it: its own workflows plan on a bare `home-planner` label,
pick `<prefix>-home-main` for a push to main when `CI_MAIN_RUNNER` is `on`, else the planner's
`large`/`small` output on a first attempt, else `<prefix>-home`, and copy the examples' choice
in front of their own.

Each job's `runs-on` is read from the example files and worked out with tests/github_expression.py.
"""

import heapq
import re
from pathlib import Path
from typing import Any

import pytest
from github_expression import evaluate, value

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = [_ROOT / "examples/ci.yml", _ROOT / "examples/e2e.yml"]

HOSTED = "ubuntu-latest"
MAIN = ["self-hosted", "example-app-home-main"]
BATCH = "refs/heads/workflow/merge-batch-0123456789abcdef0123456789abcdef"
# An installation's own choice, as the planner and its first attempt give it; the example's
# `ubuntu-latest` is what it puts its own choice in place of.
OWN = (
    "(vars.CI_MAIN_RUNNER == 'on' && github.event_name == 'push' && github.ref == 'refs/heads/main'"
    " && 'example-app-home-main' || github.run_attempt == 1 && needs.plan.outputs.runner"
    " || 'example-app-home')"
)
OWN_PLANNER = "'home-planner'"


def _runs_on(path: Path) -> dict[str, str]:
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


def _context(
    event: str, ref: str, switch: str | None, attempt: int = 1, planned: str = "example-app-large"
) -> dict[str, Any]:
    return {
        "github": {"event_name": event, "ref": ref, "run_attempt": str(attempt)},
        "vars": {} if switch is None else {"CI_MAIN_RUNNER": switch},
        "needs": {"plan": {"outputs": {"runner": planned}}},
    }


def _installed(expression: str, job: str) -> str:
    """The example's choice copied into an installation's workflow: its own choice where the
    example falls back to GitHub's runners."""
    own = OWN_PLANNER if job == "plan" else OWN
    assert expression.count(f"'{HOSTED}'") == 1, expression
    return expression.replace(f"'{HOSTED}'", own)


def _jobs() -> list[tuple[str, str, str]]:
    return [(path.name, job, text) for path in _EXAMPLES for job, text in _runs_on(path).items()]


_IDS = [f"{name}:{job}" for name, job, _ in _jobs()]


@pytest.mark.parametrize("name,job,text", _jobs(), ids=_IDS)
def test_with_the_switch_off_the_copied_choice_changes_nothing(
    name: str, job: str, text: str
) -> None:
    """Survives: with `CI_MAIN_RUNNER` off or unset the copied choice is the installation's own."""
    own = OWN_PLANNER if job == "plan" else OWN
    installed = _installed(text, job)
    for switch in (None, "off", ""):
        for event, ref in (
            ("push", "refs/heads/main"),
            ("pull_request", "refs/pull/7/merge"),
            ("workflow_dispatch", BATCH),
        ):
            for attempt in (1, 2):
                context = _context(event, ref, switch, attempt)
                assert value(installed, context) == evaluate(own, context), (
                    event,
                    switch,
                    attempt,
                )


@pytest.mark.parametrize("name,job,text", _jobs(), ids=_IDS)
def test_with_the_switch_on_the_copied_choice_sends_a_scratch_run_to_main(
    name: str, job: str, text: str
) -> None:
    """Survives: a first attempt of the gate's dispatch, and a push to main, go to main's runners
    in the installation's copy too; a pull request keeps the planner's choice."""
    installed = _installed(text, job)
    assert value(installed, _context("workflow_dispatch", BATCH, "on")) == MAIN
    assert value(installed, _context("push", "refs/heads/main", "on")) == MAIN
    expected = "home-planner" if job == "plan" else "example-app-large"
    assert value(installed, _context("pull_request", "refs/pull/7/merge", "on")) == expected


@pytest.mark.parametrize("name,job,text", _jobs(), ids=_IDS)
def test_a_scratch_run_whose_main_runners_are_offline_can_still_finish(
    name: str, job: str, text: str
) -> None:
    """Main's runners are offline or busy: the gate's prefix run must not wait for them forever.
    Its next attempt (a re-run of the run, or of its stuck jobs) must be able to take the
    ordinary label, as every other re-run in the installation does. Every attempt here asks for
    main's label again, so with main's runners offline no attempt ever starts."""
    labels = {
        attempt: value(text, _context("workflow_dispatch", BATCH, "on", attempt))
        for attempt in (1, 2, 3)
    }
    installed = {
        attempt: value(_installed(text, job), _context("workflow_dispatch", BATCH, "on", attempt))
        for attempt in (1, 2, 3)
    }

    assert labels[1] == MAIN
    assert any(label != MAIN for label in labels.values()), (
        f"{name}:{job} asks for main's runners on every attempt: {labels}"
    )
    assert any(label != MAIN for label in installed.values()), (
        f"{name}:{job}, as an installation copies it, asks for main's runners on every attempt: "
        f"{installed}"
    )


def _assign(
    jobs: list[tuple[int, str, frozenset[str], int]], runners: list[frozenset[str]]
) -> dict[str, int]:
    """When each job starts, as GitHub hands queued jobs to idle self-hosted runners: the oldest
    queued job first, to an idle runner carrying every label it asks for. A job is (queued at,
    name, labels, minutes)."""
    free = [(0, index) for index in range(len(runners))]
    heapq.heapify(free)
    waiting = sorted(jobs)
    started: dict[str, int] = {}
    while waiting:
        at, index = heapq.heappop(free)
        ready = [job for job in waiting if job[0] <= at and job[2] <= runners[index]]
        if not ready:
            later = [job[0] for job in waiting if job[0] > at and job[2] <= runners[index]]
            if not later:
                continue
            heapq.heappush(free, (min(later), index))
            continue
        job = ready[0]
        waiting.remove(job)
        started[job[1]] = at
        heapq.heappush(free, (at + job[3], index))
    return started


def test_a_push_to_main_goes_before_the_merge_line_on_main_s_runners() -> None:
    """A wave: the gate tests four prefixes, CI and E2E each, on main's two runners; a merge
    lands a minute later and its push to main plans its run. Main's push must go first: it
    starts on the next runner that frees, ahead of prefix jobs still queued. Both ask for the
    very same labels and nothing else (the lender) tells them apart, so main waits behind every
    prefix job queued before it."""
    ci, e2e = (_runs_on(path) for path in _EXAMPLES)
    scratch = {
        job: frozenset(value(text, _context("workflow_dispatch", BATCH, "on")))
        for job, text in [*ci.items(), *e2e.items()]
    }
    main = frozenset(value(ci["plan"], _context("push", "refs/heads/main", "on")))
    jobs: list[tuple[int, str, frozenset[str], int]] = []
    for prefix in range(4):
        jobs += [(0, f"prefix {prefix} {job}", labels, 20) for job, labels in scratch.items()]
    jobs.append((1, "main CI: what to run", main, 2))
    pool = [frozenset(MAIN), frozenset(MAIN)]

    started = _assign(jobs, pool)

    first_free = min(at for name, at in started.items() if at > 0)
    assert started["main CI: what to run"] <= first_free, (
        f"main's push starts at minute {started['main CI: what to run']}, "
        f"a runner was free at minute {first_free}; "
        f"{sum(1 for name, at in started.items() if 0 < at < started['main CI: what to run'])} "
        "prefix jobs went first"
    )
