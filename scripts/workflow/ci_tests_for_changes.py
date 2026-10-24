"""The tests CI will run for this checkout's change, and a cloud worker's push runs them first.

    shipline ci-tests-for-changes [base] [--run] [--extra-only] [--server-part "K of N"]

A repository's pre-push hook runs its fast checks only; its slower suites run in CI. A cloud
worker runs on a machine of its own, where those suites cost no local machine anything, so its
push runs them before it leaves, and its CI comes back clean.

The change is every tracked file that differs, committed or not, from where this branch left
`origin/main` (fetched first), or `base` when one is named: the tested main of CI, which tests
the merge of the branch into main. Each suite of `cloud_pre_push.suites` in `shipline.json` is
asked, by its `select` command given that base and `--files-from` a list of the changed files,
which of its tests CI will run. It prints one JSON line: `whole` (true for the whole suite),
the selected files under `tests` or `specs`, `total` and `reasons`. The answer is printed, and
is the same for a person and a cloud worker.

`--run` then runs the repository's push checks (`commands.push_checks`) and each suite that
has a selection: its `run` command with `{workers}` filled in and the selected files after it,
none for the whole suite. `--extra-only`, the form for the pre-push hook, runs the suites only,
and only when `SHIPLINE_CLOUD_WORKER=1`: any other push runs no more than it did before.

As root, every suite and probe runs as the unprivileged account `SHIPLINE_CLOUD_TEST_USER`
names (`runuser -u <account> --`); without one, nothing runs. A suite with a `probe` runs only
when its probe says a browser can start, and is otherwise left to CI. Each suite reports its
time and has LIMIT_SECONDS; past it, it gets a TERM, and a KILL GRACE_SECONDS later, and counts
as timed out. After a timeout of the whole server suite, `--server-part "K of N"` (or
`SHIPLINE_CLOUD_SERVER_PART`) runs only that part, by the server suite's `part` argument, as CI
parts it; CI runs the rest. A failed or timed-out suite refuses the push, once every suite ran.

Exits 0 when the push may go, 1 when it is refused, 2 on a wrong call.
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import workflow_config
from workflow_config import Suite

# How long one suite may run, and how long it has to stop after a TERM.
LIMIT_SECONDS = 25 * 60
GRACE_SECONDS = 60
# How long fetching main may take.
FETCH_SECONDS = 120
# How many of a refused probe's last lines are told.
PROBE_LINES = 3
SERVER = "server"
_PART = re.compile(r"([0-9]+) of ([0-9]+)")
_ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]*\$?")


class Refused(Exception):
    """The push is refused before any suite ran, for the reason given."""


@dataclass(frozen=True)
class Selection:
    whole: bool
    files: tuple[str, ...]
    total: int
    reasons: tuple[str, ...]

    def __str__(self) -> str:
        if self.whole:
            said = f"the whole suite ({self.total} files)"
        elif not self.files:
            said = "none"
        else:
            said = f"{len(self.files)} of {self.total} files: {' '.join(self.files)}"
        return "".join([said, *(f"\n  because: {'; '.join(self.reasons)}",) * bool(self.reasons)])


def _say(text: str) -> None:
    print(text, flush=True)


def _git(checkout: Path, *args: str, timeout: float | None = None) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Refused(f"git {args[0]} ran for more than {timeout} seconds") from None
    if done.returncode:
        raise Refused(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _duration(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}m {whole % 60}s"


def _part(given: str | None) -> tuple[int, int] | None:
    if given is None or given == "":
        return None
    found = _PART.fullmatch(given.strip())
    if not found or not 1 <= int(found[1]) <= int(found[2]):
        raise ValueError(f'a server part is "K of N", K from 1 to N, not {given!r}')
    return int(found[1]), int(found[2])


def _words(command: str, values: dict[str, str]) -> list[str]:
    words = shlex.split(command)
    for name, value in values.items():
        words = [word.replace("{" + name + "}", value) for word in words]
    return words


def _select(suite: Suite, checkout: Path, base: str, listing: Path) -> Selection:
    words = shlex.split(suite.select) + [base, "--files-from", str(listing)]
    done = subprocess.run(words, cwd=checkout, capture_output=True, text=True, check=False)
    failed = f"{suite.name}: the selection failed"
    if done.returncode:
        raise Refused(f"{failed} (exit {done.returncode}): {done.stderr.strip()}")
    try:
        answer = json.loads(done.stdout)
        files = answer.get("tests", answer.get("specs"))
        if not (
            isinstance(answer["whole"], bool)
            and isinstance(files, list)
            and all(isinstance(path, str) and path for path in files)
            and type(answer["total"]) is int
            and isinstance(answer["reasons"], list)
            and all(isinstance(reason, str) for reason in answer["reasons"])
        ):
            raise ValueError
    except (ValueError, KeyError, AttributeError, TypeError):
        raise Refused(f"{failed}: it printed {done.stdout.strip()!r}") from None
    return Selection(answer["whole"], tuple(files), answer["total"], tuple(answer["reasons"]))


def _as_tester(words: list[str], account: str | None) -> list[str]:
    return ["runuser", "-u", account, "--", *words] if account else words


def _stop(process: subprocess.Popen[bytes], grace: float) -> None:
    """TERM to the run and every process it started; KILL to those left after the grace."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        process.wait()


def _run(words: list[str], checkout: Path, limit: float, grace: float) -> tuple[str, float]:
    """Runs a suite, its output told as it comes: passed, failed or timed out, and its time."""
    started = time.monotonic()
    # A session of its own, so a stop reaches the processes the suite started.
    process = subprocess.Popen(
        words, cwd=checkout, stderr=subprocess.STDOUT, start_new_session=True
    )
    try:
        code = process.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        _stop(process, grace)
        return "timed out", time.monotonic() - started
    return ("passed" if code == 0 else "failed"), time.monotonic() - started


def _probe(suite: Suite, checkout: Path, account: str | None) -> str | None:
    """None when the suite's browser can start, else what the probe said."""
    assert suite.probe
    done = subprocess.run(
        _as_tester(shlex.split(suite.probe), account),
        cwd=checkout,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=LIMIT_SECONDS,
        check=False,
    )
    if done.returncode == 0:
        return None
    lines = done.stdout.strip().splitlines()[-PROBE_LINES:]
    return " / ".join(lines) or f"it exited {done.returncode}"


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shipline ci-tests-for-changes",
        description="The tests CI will run for this change; with --run, run them.",
    )
    parser.add_argument("base", nargs="?", help="where the change starts (default origin/main)")
    parser.add_argument("--run", action="store_true", help="run the push checks and suites")
    parser.add_argument(
        "--extra-only", action="store_true", help="with --run: the suites only, for a cloud push"
    )
    parser.add_argument("--server-part", metavar='"K of N"', help="one part of the server suite")
    arguments = parser.parse_args(argv)
    if arguments.extra_only and not arguments.run:
        parser.error("--extra-only goes with --run")
    try:
        arguments.part = _part(
            arguments.server_part
            if arguments.server_part is not None
            else os.environ.get("SHIPLINE_CLOUD_SERVER_PART")
        )
    except ValueError as error:
        parser.error(str(error))
    return arguments


def _checkout() -> Path:
    return Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())


def _base(checkout: Path, named: str | None) -> tuple[str, str]:
    """Where the change starts, and how to say it."""
    if named is None:
        _git(checkout, "fetch", "--quiet", "origin", "main", timeout=FETCH_SECONDS)
        named = "origin/main"
    base = _git(checkout, "merge-base", named, "HEAD").strip()
    return base, f"against {base[:12]}, where this branch left {named}"


def main(argv: list[str]) -> int:
    arguments = _arguments(argv)
    if arguments.extra_only and os.environ.get("SHIPLINE_CLOUD_WORKER") != "1":
        _say("ci-tests-for-changes: not a cloud worker, so the push runs no extra suite")
        return 0
    try:
        config = workflow_config.current()
        account = os.environ.get("SHIPLINE_CLOUD_TEST_USER") or None
        limit = float(os.environ.get("WORKFLOW_CLOUD_SUITE_SECONDS", LIMIT_SECONDS))
        grace = float(os.environ.get("WORKFLOW_CLOUD_SUITE_GRACE", GRACE_SECONDS))
        suites = config.cloud_pre_push.suites
        if arguments.part:
            server = next((suite for suite in suites if suite.name == SERVER), None)
            if server is None or server.part is None:
                raise Refused(
                    "a server part was named, but shipline.json names no "
                    f"cloud_pre_push.suites.{SERVER}.part"
                )
        if arguments.run:
            if int(os.environ.get("WORKFLOW_EUID", os.geteuid())) != 0:
                account = None
            elif account is None:
                raise Refused(
                    "running as root: name the unprivileged account the tests run as in "
                    "SHIPLINE_CLOUD_TEST_USER, so no test runs as root"
                )
            elif not _ACCOUNT.fullmatch(account):
                raise Refused(f"SHIPLINE_CLOUD_TEST_USER is no account name: {account!r}")
        checkout = _checkout()
        if arguments.run and not arguments.extra_only and config.commands.push_checks:
            _say("push checks: running")
            outcome, took = _run(
                shlex.split(config.commands.push_checks), checkout, float("inf"), grace
            )
            _say(f"push checks: {outcome} in {_duration(took)}")
            if outcome != "passed":
                raise Refused("the push checks failed")
        base, against = _base(checkout, arguments.base)
        changed = _git(checkout, "diff", "--name-only", "--no-renames", "-z", base).split("\0")
        changed = [path for path in changed if path]
        _say(f"ci-tests-for-changes: {against}, {len(changed)} changed files")
        with tempfile.NamedTemporaryFile("w", prefix="ci-tests-", suffix=".txt") as listing:
            listing.write("".join(f"{path}\n" for path in changed))
            listing.flush()
            selections = [
                (suite, _select(suite, checkout, base, Path(listing.name))) for suite in suites
            ]
        for suite, selection in selections:
            _say(f"{suite.name}: {selection}")
        if not arguments.run:
            return 0
        refused = []
        for suite, selection in selections:
            if not selection.whole and not selection.files:
                continue
            if suite.probe:
                error = _probe(suite, checkout, account)
                if error is not None:
                    who = account or "this user"
                    _say(f"{suite.name}: left to CI: no browser can start as {who}: {error}")
                    continue
            words = _words(suite.run, {"workers": str(suite.workers)})
            part = arguments.part if suite.name == SERVER else None
            if part:
                assert suite.part
                shard, shards = (str(number) for number in part)
                words += _words(suite.part, {"shards": shards, "shard": shard})
            if not selection.whole:
                words += selection.files
            _say(f"{suite.name}: running" + (f" as {account}" if account else ""))
            outcome, took = _run(_as_tester(words, account), checkout, limit, grace)
            if outcome == "timed out":
                _say(
                    f"{suite.name}: timed out after {_duration(took)}, "
                    f"the limit is {_duration(limit)}"
                )
            elif part:
                rest = "; CI runs the rest" if outcome == "passed" else ""
                _say(
                    f"{suite.name}: part {part[0]} of {part[1]} {outcome} "
                    f"in {_duration(took)}{rest}"
                )
            else:
                _say(f"{suite.name}: {outcome} in {_duration(took)}")
            if outcome != "passed":
                refused.append(f"{suite.name} {outcome}")
        if refused:
            raise Refused(", ".join(refused))
    except (Refused, workflow_config.ConfigError) as error:
        _say(f"the push is refused: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
