"""Breaking tests for pull request 49: a draft runs no CI and its CI starts when it is marked
ready. Each test replays one GitHub event against the jobs of `.github/workflows/ci.yml`. A small
evaluator of GitHub's expressions decides which jobs run, how the `CI` result job ends, and which
runs share a concurrency group.

The tree under test is this checkout, or the folder `SHIPLINE_TREE` names (for example a merge
of the pull request with main)."""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(os.environ.get("SHIPLINE_TREE") or Path(__file__).resolve().parents[2])
CI = ROOT / ".github/workflows/ci.yml"
EXAMPLE = ROOT / "examples/ci.yml"
RULES = ROOT / "scripts/workflow/RULES.md"


# --- GitHub's expressions, as far as ci.yml uses them ------------------------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>==|!=|&&|\|\||!|\(|\)|,|\.|\*)"
    r"|(?P<name>[A-Za-z_][\w-]*))"
)


def _tokens(text: str) -> list[tuple[str, str]]:
    found, at = [], 0
    text = text.strip()
    while at < len(text):
        match = _TOKEN.match(text, at)
        if not match or match.end() == at:
            raise ValueError(f"cannot read {text[at:]!r}")
        kind = match.lastgroup or ""
        found.append((kind, match[kind]))
        at = match.end()
    return found


def _loose(value: Any) -> Any:
    """GitHub's loose equality: null, booleans and numbers are compared as numbers."""
    if value is None or value is False:
        return 0
    if value is True:
        return 1
    if isinstance(value, str):
        return value.lower()
    return value


def _truthy(value: Any) -> bool:
    return value not in (None, False, 0, "", -0.0)


class _Expression:
    def __init__(self, text: str, context: dict[str, Any]) -> None:
        self.tokens = _tokens(text)
        self.at = 0
        self.context = context

    def peek(self) -> str | None:
        return self.tokens[self.at][1] if self.at < len(self.tokens) else None

    def take(self, expected: str | None = None) -> tuple[str, str]:
        token = self.tokens[self.at]
        if expected is not None and token[1] != expected:
            raise ValueError(f"expected {expected!r}, got {token[1]!r}")
        self.at += 1
        return token

    def value(self) -> Any:
        result = self.either()
        if self.at != len(self.tokens):
            raise ValueError(f"left over: {self.tokens[self.at:]}")
        return result

    def either(self) -> Any:
        left = self.both()
        while self.peek() == "||":
            self.take()
            right = self.both()
            left = left if _truthy(left) else right
        return left

    def both(self) -> Any:
        left = self.compare()
        while self.peek() == "&&":
            self.take()
            right = self.compare()
            left = right if _truthy(left) else left
        return left

    def compare(self) -> Any:
        left = self.unary()
        while self.peek() in ("==", "!="):
            op = self.take()[1]
            right = self.unary()
            same = _loose(left) == _loose(right)
            left = same if op == "==" else not same
        return left

    def unary(self) -> Any:
        if self.peek() == "!":
            self.take()
            return not _truthy(self.unary())
        return self.primary()

    def primary(self) -> Any:
        kind, text = self.take()
        if text == "(":
            inner = self.either()
            self.take(")")
            return inner
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text)
        if text in ("true", "false"):
            return text == "true"
        if text == "null":
            return None
        if self.peek() == "(":
            self.take("(")
            args = []
            while self.peek() != ")":
                args.append(self.either())
                if self.peek() == ",":
                    self.take(",")
            self.take(")")
            return self.call(text, args)
        value: Any = self.context.get(text)
        while self.peek() == ".":
            self.take(".")
            key = self.take()[1]
            if key == "*":
                value = list(value.values()) if isinstance(value, dict) else []
                if self.peek() == ".":
                    self.take(".")
                    key = self.take()[1]
                    value = [item.get(key) for item in value]
            else:
                value = value.get(key) if isinstance(value, dict) else None
        return value

    def call(self, name: str, args: list[Any]) -> Any:
        status = self.context["__status__"]
        if name == "always":
            return True
        if name == "success":
            return status == "success"
        if name == "failure":
            return status == "failure"
        if name == "cancelled":
            return False
        if name == "contains":
            haystack, needle = args
            if isinstance(haystack, list):
                return any(_loose(item) == _loose(needle) for item in haystack)
            return str(needle).lower() in str(haystack).lower()
        if name == "fromJSON":
            return json.loads(args[0])
        if name == "join":
            return " ".join(str(item) for item in args[0])
        raise ValueError(f"unknown function {name}")


def evaluate(text: str, context: dict[str, Any]) -> Any:
    text = text.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return _Expression(text, context).value()


def _uses_status(text: str) -> bool:
    return bool(re.search(r"\b(always|success|failure|cancelled)\(", text))


# --- one run of ci.yml ------------------------------------------------------------------------


def workflow(path: Path = CI) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    # YAML 1.1 reads the key `on` as true.
    loaded["on"] = loaded.pop(True, loaded.get("on"))
    return loaded


def pull_event(action: str, *, draft: bool, number: int = 7) -> dict[str, Any]:
    return {
        "event_name": "pull_request",
        "ref": f"refs/pull/{number}/merge",
        "sha": "merge-sha",
        "event": {
            "action": action,
            "number": number,
            "pull_request": {"number": number, "draft": draft, "head": {"sha": "head-sha"}},
        },
    }


def push_event() -> dict[str, Any]:
    return {"event_name": "push", "ref": "refs/heads/main", "sha": "main-sha", "event": {}}


def merge_group_event() -> dict[str, Any]:
    return {
        "event_name": "merge_group",
        "ref": "refs/heads/gh-readonly-queue/main/pr-7-abc",
        "sha": "queue-sha",
        "event": {"merge_group": {"head_sha": "queue-sha"}},
    }


def triggered(github: dict[str, Any], flow: dict[str, Any] | None = None) -> bool:
    """Whether GitHub starts ci.yml for the event at all."""
    on = (flow or workflow())["on"]
    if isinstance(on, list):
        on = {name: None for name in on}
    if github["event_name"] not in on:
        return False
    if github["event_name"] == "pull_request":
        types = (on["pull_request"] or {}).get("types", ["opened", "synchronize", "reopened"])
        return github["event"]["action"] in types
    if github["event_name"] == "push":
        return github["ref"].removeprefix("refs/heads/") in (on["push"] or {}).get("branches", [])
    return True


def run(
    github: dict[str, Any],
    outcomes: dict[str, str] | None = None,
    flow: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Each job's result in one run of ci.yml: `skipped` when its condition is false, else the
    outcome given for it (default `success`). The result job `ci` runs its own step."""
    flow = flow or workflow()
    outcomes = outcomes or {}
    jobs: dict[str, Any] = flow["jobs"]
    results: dict[str, str] = {}
    outputs: dict[str, dict[str, str]] = {}
    pending = list(jobs)
    while pending:
        for key in list(pending):
            job = jobs[key]
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            if any(need not in results for need in needs):
                continue
            pending.remove(key)
            needed = {need: {"result": results[need], "outputs": outputs[need]} for need in needs}
            upstream = [results[need] for need in needs]
            status = "failure" if any(r in ("failure", "cancelled") for r in upstream) else "success"
            condition = str(job.get("if", "success()"))
            if not _uses_status(condition):
                condition = f"success() && ({condition.strip().removeprefix('${{').removesuffix('}}')})"
            context = {"github": github, "needs": needed, "__status__": status}
            # A job whose needs were skipped is skipped unless its condition asks for a status.
            if any(r == "skipped" for r in upstream) and not _uses_status(str(job.get("if", ""))):
                results[key] = "skipped"
            elif not _truthy(evaluate(condition, context)):
                results[key] = "skipped"
            elif key == "ci":
                results[key] = _ci_step(job, needed)
            else:
                results[key] = outcomes.get(key, "success")
            outputs[key] = {"inherited": "[]"} if results[key] == "success" else {}
    return results


def _ci_step(job: dict[str, Any], needed: dict[str, Any]) -> str:
    """The `CI` job's own step, run in bash with the results of the jobs it needs."""
    step = job["steps"][0]
    env = {
        name: str(evaluate(value, {"needs": needed, "__status__": "success"}))
        for name, value in (step.get("env") or {}).items()
    }
    script = re.sub(
        r"\$\{\{\s*needs\.(\w[\w-]*)\.result\s*\}\}",
        lambda found: needed[found[1]]["result"],
        step["run"],
    )
    done = subprocess.run(["bash", "-c", script], env={**os.environ, **env}, capture_output=True)
    return "success" if done.returncode == 0 else "failure"


def concurrency_group(github: dict[str, Any], flow: dict[str, Any] | None = None) -> str:
    group = (flow or workflow())["concurrency"]["group"]
    return re.sub(
        r"\$\{\{(.*?)\}\}",
        lambda found: str(evaluate(found[1], {"github": github, "__status__": "success"})),
        group,
    )


# --- the daily uses ---------------------------------------------------------------------------


def test_the_evaluator_reads_ci_yml_as_main_ran_it_before() -> None:
    """Control: a ready pull request's push runs every job and `CI` passes; a failed shard fails
    `CI`. If this fails, the evaluator, not the pull request, is wrong."""
    github = pull_event("synchronize", draft=False)

    assert triggered(github)
    results = run(github)
    assert set(results.values()) == {"success"}
    assert run(github, {"tests": "failure"})["ci"] == "failure"
    assert run(github, {"tests": "cancelled"})["ci"] == "failure"


def test_a_ticket_opened_as_a_draft_takes_no_runner() -> None:
    """The orchestrator opens every ticket as an empty draft pull request."""
    github = pull_event("opened", draft=True)

    assert triggered(github)
    assert set(run(github).values()) == {"skipped"}


@pytest.mark.parametrize("action", ["synchronize", "reopened"])
def test_a_builder_s_push_to_its_draft_takes_no_runner(action: str) -> None:
    """Thirty and more builders push to their drafts all day: no job of a draft's run may take one
    of the repository's concurrent runners, the planner neither."""
    results = run(pull_event(action, draft=True))

    assert {job for job, result in results.items() if result != "skipped"} == set()


def test_a_draft_s_run_never_reports_a_green_ci() -> None:
    results = run(pull_event("synchronize", draft=True))

    assert results["ci"] != "success"


def test_marking_ready_without_a_push_runs_everything() -> None:
    """The head was pushed while a draft; the pull request is marked ready with no new push. Its
    CI must start then and test that head."""
    github = pull_event("ready_for_review", draft=False)

    assert triggered(github)
    results = run(github)
    assert set(results.values()) == {"success"}
    assert run(github, {"tests": "failure"})["ci"] == "failure"


def test_a_push_to_a_ready_pull_request_runs_everything() -> None:
    results = run(pull_event("synchronize", draft=False))

    assert set(results.values()) == {"success"}


@pytest.mark.parametrize("make", [push_event, merge_group_event], ids=["main", "merge-queue"])
def test_main_and_the_merge_queue_run_every_job(make: Any) -> None:
    github = make()

    assert triggered(github)
    results = run(github)
    assert set(results.values()) == {"success"}
    assert run(github, {"tests": "failure"})["ci"] == "failure"


def test_an_edit_label_or_review_request_starts_no_run() -> None:
    for action in ("edited", "labeled", "review_requested", "converted_to_draft"):
        assert not triggered(pull_event(action, draft=action == "converted_to_draft"))


# --- the failures that use meets --------------------------------------------------------------


def test_a_partial_re_run_of_the_ready_run_still_tests() -> None:
    """A shard of the ready run failed and only the failed jobs are re-run: GitHub re-runs with the
    event the run started with, `ready_for_review` with `draft: false`, so the shard runs."""
    github = pull_event("ready_for_review", draft=False)

    assert run(github, {"tests": "failure"})["ci"] == "failure"
    assert run(github, {"tests": "success"})["ci"] == "success"


def test_re_running_the_draft_s_run_after_ready_never_tests() -> None:
    """If the ready run is lost, re-running the draft's own run replays `draft: true` and tests
    nothing. Shipline's head is then left with no CI result at all, never a false green. This test
    pins that it is not a false green."""
    results = run(pull_event("synchronize", draft=True))

    assert results["ci"] != "success"
    assert results["tests"] == "skipped"


def test_a_last_draft_push_and_ready_share_one_cancelling_group() -> None:
    """A builder pushes its last commit to the draft and marks it ready a moment later. Both runs
    are in one concurrency group that cancels the older run. Only the run with `draft: false` tests
    anything, so it must not be the one cancelled. This pins the shared group; the order GitHub
    queues the two runs in decides the rest (see the report)."""
    draft = pull_event("synchronize", draft=True)
    ready = pull_event("ready_for_review", draft=False)
    cancels = workflow()["concurrency"]["cancel-in-progress"]

    assert concurrency_group(draft) == concurrency_group(ready)
    assert evaluate(str(cancels), {"github": draft, "__status__": "success"}) is True
    assert set(run(draft).values()) == {"skipped"}


def test_a_missing_draft_field_does_not_skip_a_pull_request() -> None:
    """An event without the field (a run that has no pull request) is never filtered."""
    github = pull_event("synchronize", draft=False)
    del github["event"]["pull_request"]["draft"]

    assert run(github)["ci"] == "success"


# --- the rule and the example a repository copies ---------------------------------------------


def test_the_example_ci_a_repository_copies_does_what_the_rule_says() -> None:
    """`RULES.md` is the rules of every repository that uses Shipline, and it now says a draft runs
    no CI. A repository copying `examples/ci.yml` must then get that behaviour."""
    if not EXAMPLE.exists():
        pytest.skip("this tree has no examples/ci.yml")
    if "A draft pull request runs no CI" not in RULES.read_text():
        pytest.skip("RULES.md makes no claim about drafts")
    flow = workflow(EXAMPLE)

    assert triggered(pull_event("ready_for_review", draft=False), flow)
    results = run(pull_event("synchronize", draft=True), flow=flow)
    assert {job for job, result in results.items() if result != "skipped"} == set()
