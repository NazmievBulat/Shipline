"""What a repository's CI writes for the merge gate, and what main's CI inherits from a squash's
proof. Only the standard library, and git and gh, are needed.

The gate merges only on what the latest green run proves it tested, read from the annotations
of the run's planner job (`checks.ci_plan`, `checks.e2e_plan`), titled as `annotations.*` names:

- `merge_tested`: the commit the run tested. On a pull request it is GitHub's merge of the
  pull request into main, whose two parents the gate compares with main and the head.
- `files_tested`: that commit's tree.
- one per suite (`server_tests`, `web_suite`, `browser_suite`): `whole` when every test of it
  ran, `selected: N of M <unit>` when N of its M ran, `none` when the change needs none of it.

    ci_proof.py notes [--whole SUITE] [--none SUITE] [--selected SUITE N M UNIT] ...

prints them as workflow commands, which GitHub turns into the job's annotations.

A squash the gate makes carries one `Workflow-Proof: {json}` line (`squash_proof` in
`merge_gate.py`): main's parent and tree, and the runs that tested that tree with the jobs that
passed there. On a push to main,

    ci_proof.py inherited WORKFLOW

names each job main's run inherits from it, one notice each, and hands their names on as the
step output `inherited`, a JSON list, so those jobs skip themselves. Nothing is inherited unless
everything checks out; whatever cannot be read means every job runs.

    ci_proof.py test [ARGUMENT ...]

runs the repository's own test command (`commands.test`) from the root of the checkout it is
called in, with the job's arguments after it.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import workflow_config

# The squash's proof line, and the line of a refresh commit that asks CI for every suite whole.
RECEIPT = "Workflow-Proof: "
WHOLE = "Workflow-Whole-Suites: true"
# The title of the notice that names an inherited job and its run.
INHERITED = "Inherited job"
SUITES = ("server_tests", "web_suite", "browser_suite")
# The job each suite's note speaks for, by the gate's names (merge_gate.py): it is inherited
# only from a run that ran its suite whole.
SUITE_JOBS = {
    "CI: server tests": "server_tests",
    "CI: web": "web_suite",
    "playwright": "browser_suite",
}
# The workflows a proof may name, by the gate's own names for them.
PROVEN = ("ci", "e2e")
READ_TIMEOUT = 30
_SHA = re.compile(r"[0-9a-f]{40}")


class Unreadable(Exception):
    """GitHub could not be read, or answered with something else than expected."""


def passed_jobs(parts: list[dict[str, Any]]) -> set[str]:
    """The jobs that passed in a run, a job run in parts once each of its parts did."""
    grouped: dict[str, list[tuple[re.Match[str] | None, dict[str, Any]]]] = {}
    for job in parts:
        part = re.fullmatch(r"(.+) \((\d+) of (\d+)\)", job["name"])
        grouped.setdefault(part[1] if part else job["name"], []).append((part, job))
    return {
        name
        for name, group in grouped.items()
        if all(job.get("conclusion") == "success" for _, job in group)
        and sorted((int(part[2]), int(part[3])) for part, _ in group if part)
        in ([], [(number, len(group)) for number in range(1, len(group) + 1)])
    }


def whole_requested(message: str) -> bool:
    """Whether a commit message asks CI to run every suite whole: a line that is WHOLE."""
    return WHOLE in message.splitlines()


def _escaped(text: str, *, in_property: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if in_property else text


def notice(title: str, message: str) -> str:
    """The workflow command that makes a notice titled `title` of the job's check."""
    return f"::notice title={_escaped(title, in_property=True)}::{_escaped(message)}"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, timeout=READ_TIMEOUT
    ).stdout.strip()


def notes(suites: dict[str, str]) -> list[str]:
    """The notes of the commit checked out here, with each suite's state."""
    titles = workflow_config.current().annotations
    return [
        notice(titles.merge_tested, _git("rev-parse", "HEAD")),
        notice(titles.files_tested, _git("rev-parse", "HEAD^{tree}")),
        *(notice(getattr(titles, suite), state) for suite, state in suites.items()),
    ]


def receipt(message: str) -> dict[str, Any] | None:
    """The proof a commit message carries: its one RECEIPT line, read whole. None when it has
    none or several, or one that is not a proof."""
    lines = [line for line in message.splitlines() if line.startswith(RECEIPT)]
    if len(lines) != 1:
        return None
    try:
        proof = json.loads(lines[0].removeprefix(RECEIPT))
    except (ValueError, RecursionError):
        return None

    def source(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("workflow"), str)
            and type(value.get("run")) is int
            and value["run"] > 0
            and isinstance(value.get("jobs"), list)
            and all(isinstance(job, str) for job in value["jobs"])
        )

    if (
        isinstance(proof, dict)
        and all(
            isinstance(proof.get(key), str) and _SHA.fullmatch(proof[key])
            for key in ("base", "tree")
        )
        and isinstance(proof.get("sources"), list)
        and all(source(value) for value in proof["sources"])
    ):
        return proof
    return None


def read(path: str, key: str | None = None) -> Any:
    """One GitHub read through `gh api`, every page of it; `key` names the list of a paged
    object, such as a run's `jobs`."""
    try:
        done = subprocess.run(
            ["gh", "api", "--paginate", path],
            capture_output=True,
            text=True,
            check=False,
            timeout=READ_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unreadable(f"gh api {path}: {error}") from error
    if done.returncode:
        raise Unreadable(f"gh api {path}: {done.stderr.strip() or done.stdout.strip()}")
    # gh prints one JSON document per page.
    raw, pages, decoder = done.stdout.strip(), [], json.JSONDecoder()
    try:
        while raw:
            page, end = decoder.raw_decode(raw)
            pages.append(page)
            raw = raw[end:].lstrip()
    except (ValueError, RecursionError) as error:
        raise Unreadable(f"gh api {path}: {error}") from error
    if key is not None:
        return [item for page in pages for item in page[key]]
    if pages and all(isinstance(page, list) for page in pages):
        return [item for page in pages for item in page]
    if len(pages) != 1:
        raise Unreadable(f"gh api {path}: {len(pages)} pages")
    return pages[0]


def _source_jobs(source: dict[str, Any], tree: str, api: Callable[..., Any]) -> dict[str, int]:
    """The jobs a run named by a proof passed on `tree`: none unless its notes prove that tree.

    A run of a fork, whose workflow the fork may have changed, never counts, nor do CI notes of
    a merge the run did not test: its own head, or GitHub's merge of its head into main."""
    config = workflow_config.current()
    titles, workflow = config.annotations, source["workflow"]
    run = api(f"{config.api}/actions/runs/{source['run']}")
    if (
        run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("path") != f".github/workflows/{getattr(config.workflows, workflow)}"
        or run["head_repository"]["full_name"] != config.repository
    ):
        return {}
    parts = api(f"{config.api}/actions/runs/{source['run']}/jobs?per_page=100", "jobs")
    plan = config.checks.ci_plan if workflow == "ci" else config.checks.e2e_plan
    planner = next(job for job in parts if job["name"] == plan)
    noted = {
        (note.get("title"), note["message"].strip())
        for note in api(f"{config.api}/check-runs/{planner['id']}/annotations?per_page=100")
    }
    if workflow == "ci":
        merges = [message for title, message in noted if title == titles.merge_tested]
        if len(merges) != 1 or not _SHA.fullmatch(merges[0]):
            return {}
        merge = api(f"{config.api}/git/commits/{merges[0]}")
        if merge["tree"]["sha"] != tree or run["head_sha"] not in (
            merges[0],
            merge["parents"][-1]["sha"],
        ):
            return {}
    elif not {(titles.files_tested, tree), (titles.browser_suite, "whole")} <= noted:
        return {}
    passed = passed_jobs(parts)
    return {
        job: source["run"]
        for job in source["jobs"]
        if job in passed
        and (job not in SUITE_JOBS or (getattr(titles, SUITE_JOBS[job]), "whole") in noted)
    }


def inherited(sha: str, workflow: str, api: Callable[..., Any] = read) -> dict[str, int]:
    """The jobs of `workflow` that main's commit `sha` inherits from its proof, with the run
    each passed in. Nothing when the proof does not match the commit or cannot be read."""
    if workflow not in PROVEN:
        return {}
    config = workflow_config.current()
    try:
        commit = api(f"{config.api}/git/commits/{sha}")
        proof = receipt(commit["message"])
        if (
            proof is None
            or commit["tree"]["sha"] != proof["tree"]
            or [parent["sha"] for parent in commit["parents"]] != [proof["base"]]
        ):
            return {}
        found: dict[str, int] = {}
        for source in proof["sources"]:
            if source["workflow"] == workflow:
                found.update(_source_jobs(source, proof["tree"], api))
        return found
    except (
        Unreadable,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        AttributeError,
        StopIteration,
    ) as error:
        print(f"nothing is inherited, every job runs: {error!r}", file=sys.stderr)
        return {}


def _count(text: str) -> int:
    if not re.fullmatch(r"[0-9]+", text):
        raise argparse.ArgumentTypeError(f"not a count: {text!r}")
    return int(text)


def _main_notes(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ci_proof.py notes")
    parser.add_argument("--whole", action="append", default=[], choices=SUITES)
    parser.add_argument("--none", action="append", default=[], choices=SUITES)
    parser.add_argument(
        "--selected", action="append", default=[], nargs=4, metavar=("SUITE", "N", "M", "UNIT")
    )
    given = parser.parse_args(args)
    suites: dict[str, str] = {}

    def state(suite: str, value: str) -> None:
        if suite not in SUITES:
            parser.error(f"unknown suite {suite!r}")
        if suite in suites:
            parser.error(f"suite {suite} is noted twice")
        suites[suite] = value

    for suite in given.whole:
        state(suite, "whole")
    for suite in given.none:
        state(suite, "none")
    for suite, ran, of, unit in given.selected:
        try:
            ran, of = _count(ran), _count(of)
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
        if ran > of or not re.fullmatch(r"[A-Za-z][\w -]*", unit):
            parser.error(f"cannot note {ran} of {of} {unit!r}")
        state(suite, f"selected: {ran} of {of} {unit}")
    # A suite in the order the gate reads them, whichever flag named it.
    print("\n".join(notes({suite: suites[suite] for suite in SUITES if suite in suites})))
    return 0


def _main_inherited(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ci_proof.py inherited")
    parser.add_argument("workflow")
    given = parser.parse_args(args)
    found = inherited(os.environ.get("GITHUB_SHA") or _git("rev-parse", "HEAD"), given.workflow)
    for job, run in sorted(found.items()):
        print(notice(INHERITED, f"{job}: run {run}"))
    line = "inherited=" + json.dumps(sorted(found)) + "\n"
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as handed:
            handed.write(line)
    else:
        sys.stdout.write(line)
    return 0


def _main_test(args: list[str]) -> int:
    config = workflow_config.current()
    if config.commands.test is None:
        print(
            f"{config.path} names no commands.test, "
            "the command that runs this repository's tests",
            file=sys.stderr,
        )
        return 1
    # The command line is the repository's own, as its file names it; the job's arguments
    # follow it as words of their own, so it must end where arguments may follow.
    return subprocess.run(
        ["bash", "-c", config.commands.test + ' "$@"', "test", *args],
        cwd=_git("rev-parse", "--show-toplevel"),
        check=False,
    ).returncode


def main() -> int:
    commands = {"notes": _main_notes, "inherited": _main_inherited, "test": _main_test}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print("usage: ci_proof.py notes|inherited|test ...", file=sys.stderr)
        return 2
    try:
        return commands[sys.argv[1]](sys.argv[2:])
    except workflow_config.ConfigError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
