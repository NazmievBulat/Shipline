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
`merge_gate.py`): main's parent and tree, and the runs that proved its jobs. CI and E2E prove
the identical tree; images may prove unchanged image inputs across neutral differences.
On a push to main,

    ci_proof.py inherited WORKFLOW

names each job main's run inherits from it, one notice each, and hands their names on as the
step output `inherited`, a JSON list, so those jobs skip themselves. Nothing is inherited unless
everything checks out; whatever cannot be read means every job runs.

    ci_proof.py test [ARGUMENT ...]
    ci_proof.py browser [ARGUMENT ...]

runs the repository's own test command (`commands.test`), or its browser command
(`commands.browser`), from the root of the checkout it is called in, with the job's arguments
after it.

    ci_proof.py main-lane

hands on, as the step output `main_lane`, whether the merge gate's first run of a prefix takes
the merge line's lane on main's runners: only when the runner pool's lender noted, at most
LANE_SECONDS ago, that one of main's runners is online and idle with it, and this planner could
claim one of the free runners the note counts. The pool's planner reads that note; on GitHub's
own runners there is none, and the prefix takes the ordinary choice.

    ci_proof.py prefix-only [--needed GROUP] ...

hands workflows the configured groups to defer on pull request events, after their existing
path selectors have named the groups needed. It notes those deferrals in the CI planner for
the wake's daily savings report. Scratch dispatches and main defer nothing.
"""

import argparse
import base64
import json
import os
import re
import subprocess
from contextlib import chdir
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import workflow_config
from merge_images import path_filter, pull_request_paths

# The squash's proof line, and the line of a refresh commit that asks CI for every suite whole.
RECEIPT = "Workflow-Proof: "
WHOLE = "Workflow-Whole-Suites: true"
# The title of the notice that names an inherited job and its run.
INHERITED = "Inherited job"
SUITES = ("server_tests", "web_suite", "browser_suite")
# The workflows a proof may name, by the gate's own names for them.
PROVEN = ("ci", "e2e", "images")
DEFERRED = "Prefix-only groups"
READ_TIMEOUT = 30
# The lender's note to the pool's planner, in its home (runner/lend_runners.py), and how old it
# may be: the lender writes it every round, a few seconds apart.
LANE_NOTE = ".local/state/ci-overflow/home/first.json"
LANE_SECONDS = 60
# Where the planners claim the free runners a note counts, one file each, so planners that read
# the same note take no more than it names free; and how long a claim is kept.
LANE_LEDGER = ".local/state/ci-overflow/ledger"
LANE_CLAIM = "main-lane-"
LANE_CLAIM_SECONDS = 600
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


def read(path: str, key: str | None = None, *, paginate: bool = True) -> Any:
    """One GitHub read through `gh api`, every page of it; `key` names the list of a paged
    object, such as a run's `jobs`."""
    try:
        done = subprocess.run(
            ["gh", "api", *(["--paginate"] if paginate else []), path],
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


def image_tree_difference(before: str, after: str, api: Callable[..., Any]) -> list[str] | None:
    """Image-neutral differences, or None when a tree or the filter cannot prove reuse.

    Recursive Git trees include deletions, modes and both sides of renames without the
    compare API's file limit. A truncated tree proves nothing.
    """
    if before == after:
        return []
    config = workflow_config.current()

    def files(tree: str) -> dict[str, tuple[str, str, str]]:
        listing = api(f"{config.api}/git/trees/{tree}?recursive=1")
        if listing.get("truncated") is not False:
            raise Unreadable("image inheritance needs complete trees")
        return {entry["path"]: (entry["type"], entry["mode"], entry["sha"])
                for entry in listing["tree"] if entry["type"] != "tree"}

    old, new = files(before), files(after)
    changed = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
    workflow = f".github/workflows/{config.workflows.images}"
    if workflow not in old or old.get(workflow) != new.get(workflow):
        return None
    blob = api(f"{config.api}/git/blobs/{old[workflow][2]}")
    if blob.get("encoding") != "base64":
        return None
    text = base64.b64decode("".join(blob["content"].split()), validate=True).decode("utf-8")
    matches = path_filter(pull_request_paths(text, error=Unreadable))
    return None if any(matches(path) for path in changed) else changed


def _source_jobs(source: dict[str, Any], tree: str, api: Callable[..., Any]) -> dict[str, int]:
    """The jobs a proof's run passed for `tree`: its exact files or unchanged image inputs.

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
    plan = {"ci": config.checks.ci_plan, "e2e": config.checks.e2e_plan,
            "images": config.checks.images_plan}[workflow]
    planner = next(job for job in parts if job["name"] == plan)
    noted = {
        (note.get("title"), note["message"].strip())
        for note in planner_notes(planner, api)
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
    elif workflow == "e2e" and not {(titles.files_tested, tree), (titles.browser_suite, "whole")} <= noted:
        return {}
    elif workflow == "images":
        trees = [message for title, message in noted if title == titles.files_tested]
        if (len(trees) != 1 or not _SHA.fullmatch(trees[0])
                or image_tree_difference(trees[0], tree, api) is None):
            return {}
    passed = passed_jobs(parts)
    # A suite job is inherited only from a run that ran its configured suite whole.
    suite_jobs = {
        config.checks.server_tests: "server_tests",
        config.checks.web: "web_suite",
        "playwright": "browser_suite",
    }
    return {
        job: source["run"]
        for job in source["jobs"]
        if job in passed
        and (job not in suite_jobs or (getattr(titles, suite_jobs[job]), "whole") in noted)
    }


def planner_notes(planner: dict[str, Any], api: Callable[..., Any] = read) -> list[dict[str, Any]]:
    """A failed-jobs rerun carries a successful planner, whose notes belong to its old copy."""
    config = workflow_config.current()
    notes = api(f"{config.api}/check-runs/{planner['id']}/annotations?per_page=100")
    started, created = planner.get("started_at"), planner.get("created_at")
    if (notes or planner.get("conclusion") != "success" or not started or not created
            or datetime.fromisoformat(started) >= datetime.fromisoformat(created)):
        return notes
    for attempt in range(planner.get("run_attempt", 1) - 1, 0, -1):
        copies = api(f"{config.api}/actions/runs/{planner['run_id']}/attempts/{attempt}/jobs?per_page=100", "jobs")
        for old in copies:
            if (old["name"] == planner["name"] and old.get("head_sha") == planner.get("head_sha")
                    and old.get("conclusion") == "success"
                    and old.get("started_at") == started
                    and old.get("completed_at") == planner.get("completed_at")
                    and old.get("created_at") and datetime.fromisoformat(old["created_at"]) <= datetime.fromisoformat(started)):
                return api(f"{config.api}/check-runs/{old['id']}/annotations?per_page=100")
    return []


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


# What each command's tests are called when the file names no command for them.
_TESTS = {"test": "tests", "browser": "browser tests"}


def _run_command(name: str, args: list[str]) -> int:
    config = workflow_config.current()
    entry = getattr(config.commands, name)
    if entry is None:
        print(
            f"{config.path} names no commands.{name}, "
            f"the command that runs this repository's {_TESTS[name]}",
            file=sys.stderr,
        )
        return 1
    # The command line is the repository's own, as its file names it; the job's arguments
    # follow it as words of their own, so it must end where arguments may follow.
    line = entry["run"] if isinstance(entry, dict) else entry
    leading = entry.get("args", []) if isinstance(entry, dict) else []
    if isinstance(entry, dict) and {"wait_exit", "wait_output"} & entry.keys():
        from workflow_commands import repository_answer

        with chdir(_git("rev-parse", "--show-toplevel")):
            return repository_answer([
                name, json.dumps(entry), "bash", "-c", line + ' "$@"', name, *leading, *args,
            ])
    return subprocess.run(
        ["bash", "-c", line + ' "$@"', name, *leading, *args],
        cwd=_git("rev-parse", "--show-toplevel"),
        check=False,
    ).returncode


def main_lane(home: Path, now: datetime | None = None) -> bool:
    """Whether the lender noted, at most LANE_SECONDS ago, that one of main's runners is online
    and idle with the merge line's lane, and this planner claimed one of the free runners the
    note counts. A missing, old or unreadable note, or a claim that cannot be made, says no."""
    try:
        note = json.loads((home / LANE_NOTE).read_text())
        free, written = note["batch_free"], note["updated_at"]
        age = (now or datetime.now(UTC)) - datetime.fromisoformat(written)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"main's lane is not taken: {error!r}", file=sys.stderr)
        return False
    if type(free) is not int or free < 1 or not 0 <= age.total_seconds() <= LANE_SECONDS:
        return False
    return _claim(home / LANE_LEDGER, re.sub(r"[^0-9A-Za-z]", "", str(written)), free)


def _claim(ledger: Path, lender_round: str, free: int) -> bool:
    """Claim one of the `free` runners of the lender's round `lender_round`, first come first
    served."""
    try:
        ledger.mkdir(parents=True, exist_ok=True)
        for old in ledger.glob(f"{LANE_CLAIM}*"):
            if datetime.now(UTC).timestamp() - old.stat().st_mtime > LANE_CLAIM_SECONDS:
                old.unlink(missing_ok=True)
        for slot in range(free):
            try:
                (ledger / f"{LANE_CLAIM}{lender_round}-{slot}").open("x").close()
                return True
            except FileExistsError:
                continue
    except OSError as error:
        print(f"main's lane is not taken: {error!r}", file=sys.stderr)
    return False


def _main_lane(args: list[str]) -> int:
    argparse.ArgumentParser(prog="ci_proof.py main-lane").parse_args(args)
    line = f"main_lane={'true' if main_lane(Path.home()) else 'false'}\n"
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as handed:
            handed.write(line)
    else:
        sys.stdout.write(line)
    return 0


def _main_prefix_only(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ci_proof.py prefix-only")
    config = workflow_config.current()
    parser.add_argument("--needed", action="append", default=[], choices=config.checks.groups())
    given = parser.parse_args(args)
    groups = [group for group in config.checks.prefix_only if group in given.needed]
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        groups = []
    print(notice(DEFERRED, json.dumps(groups)))
    line = "prefix_only=" + json.dumps(groups) + "\n"
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as handed:
            handed.write(line)
    else:
        sys.stdout.write(line)
    return 0


def main() -> int:
    commands = {
        "notes": _main_notes,
        "inherited": _main_inherited,
        "test": lambda args: _run_command("test", args),
        "browser": lambda args: _run_command("browser", args),
        "main-lane": _main_lane,
        "prefix-only": _main_prefix_only,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print("usage: ci_proof.py notes|inherited|test|browser|main-lane|prefix-only ...", file=sys.stderr)
        return 2
    try:
        return commands[sys.argv[1]](sys.argv[2:])
    except workflow_config.ConfigError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
