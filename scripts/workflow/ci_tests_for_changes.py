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
none for the whole suite. They run where CI tests: in place when this branch holds the main it
was compared with, else in a temporary worktree of HEAD merged with that main (`_tree`); a
branch that conflicts with it is refused. `--extra-only`, the form for the pre-push hook, runs
the suites only, and only when `SHIPLINE_CLOUD_WORKER=1`: any other push runs no more than it
did before.

As root, every suite and probe runs as the unprivileged account `SHIPLINE_CLOUD_TEST_USER`
names (`runuser -u <account> --`); without one, or when the account cannot write the checkout
or git does not trust it there, nothing runs. A suite with a `probe` runs only when its probe
says a browser can start, and is otherwise left to CI. Each suite reports its time and has
LIMIT_SECONDS; past it, it gets a TERM, and a KILL GRACE_SECONDS later, and counts as timed
out. After a timeout of the server suite, `--server-part "K of N"` (or
`SHIPLINE_CLOUD_SERVER_PART`) runs only that part of its selection, by the server suite's
`part` argument with the selected files after it, as CI parts it; CI runs the rest. A failed
or timed-out suite refuses the push, once every suite ran.

Exits 0 when the push may go, 1 when it is refused, 2 on a wrong call.
"""

import argparse
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import workflow_config
from workflow_commands import CommandTimeout, install_signal_handlers, run, run_shown
from workflow_config import Suite

# How long one suite may run, and how long it has to stop after a TERM.
LIMIT_SECONDS = 25 * 60
GRACE_SECONDS = 60
# How long fetching main, a selection and a browser probe may take.
FETCH_SECONDS = 120
SELECT_SECONDS = 300
PROBE_SECONDS = 120
# How many of a refused probe's last lines are told.
PROBE_LINES = 3
SERVER = "server"
_PART = re.compile(r"([0-9]+) of ([0-9]+)")
_ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]*\$?")


class Refused(Exception):
    """The push is refused, for the reason given."""


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
        if self.reasons:
            said += f"\n  because: {'; '.join(self.reasons)}"
        return said


def _say(text: str) -> None:
    print(text, flush=True)


def _git(*args: str, timeout: float | None = None) -> str:
    try:
        done = subprocess.run(
            ["git", *args],
            stdin=subprocess.DEVNULL,
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
    if not given:
        return None
    found = _PART.fullmatch(given.strip())
    if not found or not 1 <= int(found[1]) <= int(found[2]):
        raise ValueError(f'a server part is "K of N", K from 1 to N, not {given!r}')
    return int(found[1]), int(found[2])


def _filled(words: list[str], values: dict[str, str]) -> list[str]:
    """The words with each `{name}` in them filled in."""
    for name, value in values.items():
        words = [word.replace("{" + name + "}", value) for word in words]
    return words


def _select(suite: Suite, base: str, listing: Path) -> Selection:
    failed = f"{suite.name}: the selection failed"
    try:
        done = run(
            *workflow_config.current().words(suite.select),
            base,
            "--files-from",
            str(listing),
            timeout=SELECT_SECONDS,
            errors="replace",
        )
    except (CommandTimeout, OSError) as error:
        raise Refused(f"{failed}: {error}") from None
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
    except ValueError, KeyError, AttributeError, TypeError:
        raise Refused(f"{failed}: it printed {done.stdout.strip()!r}") from None
    return Selection(answer["whole"], tuple(files), answer["total"], tuple(answer["reasons"]))


def _as_tester(words: list[str], account: str | None) -> list[str]:
    return ["runuser", "-u", account, "--", *words] if account else words


def _run(words: list[str], limit: float | None, grace: float) -> tuple[str, float]:
    """Runs a suite, its output told as it comes: passed, failed or timed out, and its time."""
    started = time.monotonic()
    try:
        code = run_shown(*words, timeout=limit, grace=grace)
    except OSError as error:
        _say(f"cannot start {shlex.join(words)}: {error}")
        return "failed", time.monotonic() - started
    outcome = "timed out" if code is None else "passed" if code == 0 else "failed"
    return outcome, time.monotonic() - started


def _probe(suite: Suite, account: str | None) -> str | None:
    """None when the suite's browser can start, else what the probe said."""
    assert suite.probe
    seconds = float(os.environ.get("WORKFLOW_CLOUD_PROBE_SECONDS", PROBE_SECONDS))
    try:
        done = run(
            *_as_tester(workflow_config.current().words(suite.probe), account),
            timeout=seconds,
            errors="replace",
        )
    except CommandTimeout:
        return f"the probe did not answer in {_duration(seconds)}"
    except OSError as error:
        return str(error)
    if done.returncode == 0:
        return None
    lines = (done.stdout + done.stderr).strip().splitlines()[-PROBE_LINES:]
    return " / ".join(lines) or f"it exited {done.returncode}"


def _parser() -> argparse.ArgumentParser:
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
    return parser


def _base(named: str | None) -> tuple[str, str, str]:
    """Where the change starts, how to say it, and the main it is tested with."""
    if named is None:
        # Named in full: a single-branch clone's fetch would not update `origin/main`.
        _git(
            "fetch",
            "--quiet",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
            timeout=FETCH_SECONDS,
        )
        named = "origin/main"
    base = _git("merge-base", named, "HEAD").strip()
    return base, f"against {base[:12]}, where this branch left {named}", named


def _account(run_tests: bool) -> str | None:
    """The account the suites run as: None for the user this runs as, which is not root."""
    if not run_tests or int(os.environ.get("WORKFLOW_EUID", os.geteuid())) != 0:
        return None
    account = os.environ.get("SHIPLINE_CLOUD_TEST_USER") or None
    if account is None:
        raise Refused(
            "running as root: name the unprivileged account the tests run as in "
            "SHIPLINE_CLOUD_TEST_USER, so no test runs as root"
        )
    try:
        if not _ACCOUNT.fullmatch(account) or pwd.getpwnam(account).pw_uid == 0:
            raise KeyError
    except KeyError:
        raise Refused(
            f"SHIPLINE_CLOUD_TEST_USER names no unprivileged account: {account!r}"
        ) from None
    return account


def _usable(account: str) -> None:
    """Refuses before any test when the account cannot write the checkout, or git does not
    trust it as the account: every suite would fail for that, not for the change."""
    top = Path.cwd()
    checks = (["test", "-w", str(top)], ["git", "-C", str(top), "rev-parse", "--show-toplevel"])
    for check in checks:
        try:
            done = run(*_as_tester(check, account), timeout=FETCH_SECONDS, errors="replace")
        except CommandTimeout, OSError:
            done = None
        if done is None or done.returncode:
            raise Refused(
                f"{account} cannot write {top}, or git does not trust it as {account}: give it "
                f"the checkout with chown -R {account} {top} and mark it safe for git with "
                f"git config --global --add safe.directory {top}, as root and as {account}"
            )


def _as(account: str | None, *words: str) -> subprocess.CompletedProcess[str]:
    """Runs a git step of the merged tree as the account the suites run as."""
    try:
        return run(*_as_tester(list(words), account), timeout=FETCH_SECONDS, errors="replace")
    except (CommandTimeout, OSError) as error:
        raise Refused(f"{' '.join(words[:2])} failed: {error}") from None


@contextmanager
def _tree(main: str, account: str | None, setup: str) -> Iterator[str | None]:
    """Where the suites run, as CI tests: here when this branch holds `main`, else a worktree of
    HEAD merged with it, set up as a session's worktree is and removed after, whatever happens.
    Yields None for here, else how to say it."""
    held = subprocess.run(
        ["git", "merge-base", "--is-ancestor", main, "HEAD"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if held.returncode == 0:
        yield None
        return
    here = Path.cwd()
    folder = Path(tempfile.mkdtemp(prefix="ci-tests-merged-"))
    try:
        if account:
            entry = pwd.getpwnam(account)
            os.chown(folder, entry.pw_uid, entry.pw_gid)
        added = _as(account, "git", "worktree", "add", "--quiet", "--detach", str(folder), "HEAD")
        if added.returncode:
            raise Refused(f"cannot make a worktree to merge {main} in: {added.stderr.strip()}")
        # Not committed: a merge commit would need a name, and nothing keeps it.
        merged = _as(account, "git", "-C", str(folder), "merge", "--quiet", "--no-commit",
                     "--no-ff", main)  # fmt: skip
        if merged.returncode:
            conflicted = _as(account, "git", "-C", str(folder), "diff", "--name-only",
                             "--diff-filter=U").stdout.split()  # fmt: skip
            if conflicted:
                raise Refused(
                    f"this branch conflicts with {main} in {' '.join(conflicted)}: "
                    "merge it into the branch, then push"
                )
            raise Refused(f"cannot merge {main} in: {merged.stderr.strip()}")
        os.chdir(folder)
        if os.access(setup, os.X_OK) and Path(setup).is_file():
            code = run_shown(*_as_tester([f"./{setup}"], account), timeout=LIMIT_SECONDS, grace=0)
            if code != 0:
                raise Refused(f"the set-up of the merged tree, {setup}, failed")
        yield f"this branch merged with {main}"
    finally:
        os.chdir(here)
        for words in (
            _as_tester(["git", "worktree", "remove", "--force", str(folder)], account),
            ["rm", "-rf", "--", str(folder)],
            ["git", "worktree", "prune"],
        ):
            with suppress(subprocess.TimeoutExpired):
                subprocess.run(
                    words,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=FETCH_SECONDS,
                    check=False,
                )


def main(argv: list[str]) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.extra_only and not arguments.run:
        parser.error("--extra-only goes with --run")
    if arguments.extra_only and os.environ.get("SHIPLINE_CLOUD_WORKER") != "1":
        _say("ci-tests-for-changes: not a cloud worker, so the push runs no extra suite")
        return 0
    try:
        part = _part(
            arguments.server_part
            if arguments.server_part is not None
            else os.environ.get("SHIPLINE_CLOUD_SERVER_PART")
        )
        limit = float(os.environ.get("WORKFLOW_CLOUD_SUITE_SECONDS", LIMIT_SECONDS))
        grace = float(os.environ.get("WORKFLOW_CLOUD_SUITE_GRACE", GRACE_SECONDS))
    except ValueError as error:
        parser.error(str(error))
    # A stopped push stops its suites with it.
    install_signal_handlers()
    # Nothing it starts reads the push's input: git's list of refs in the hook, or a person's keys.
    os.dup2(os.open(os.devnull, os.O_RDONLY), sys.stdin.fileno())
    try:
        config = workflow_config.current()
        suites = config.cloud_pre_push.suites
        if part and not any(suite.name == SERVER and suite.part for suite in suites):
            raise Refused(
                "a server part was named, but shipline.json names no "
                f"cloud_pre_push.suites.{SERVER}.part"
            )
        account = _account(arguments.run)
        os.chdir(_git("rev-parse", "--show-toplevel").strip())
        if account:
            _usable(account)
        push_checks = config.command("push_checks")
        if arguments.run and not arguments.extra_only and push_checks:
            _say("push checks: running")
            outcome, took = _run(push_checks, None, grace)
            _say(f"push checks: {outcome} in {_duration(took)}")
            if outcome != "passed":
                raise Refused("the push checks failed")
        base, against, main = _base(arguments.base)
        changed = _git("diff", "--name-only", "--no-renames", "-z", base).split("\0")
        changed = [path for path in changed if path]
        _say(f"ci-tests-for-changes: {against}, {len(changed)} changed files")
        with tempfile.NamedTemporaryFile("w", prefix="ci-tests-", suffix=".txt") as listing:
            listing.write("".join(f"{path}\n" for path in changed))
            listing.flush()
            selections = [(suite, _select(suite, base, Path(listing.name))) for suite in suites]
        for suite, selection in selections:
            _say(f"{suite.name}: {selection}")
        if not arguments.run:
            return 0
        if not any(selection.whole or selection.files for _, selection in selections):
            return 0
        with _tree(main, account, config.worktree_setup) as merged:
            refused = _suites(selections, config, part, account, merged, limit, grace)
        if refused:
            raise Refused(", ".join(refused))
    except (Refused, workflow_config.ConfigError) as error:
        _say(f"the push is refused: {error}")
        return 1
    return 0


def _suites(
    selections: list[tuple[Suite, Selection]],
    config: workflow_config.Config,
    part: tuple[int, int] | None,
    account: str | None,
    merged: str | None,
    limit: float,
    grace: float,
) -> list[str]:
    """Runs each suite that has a selection, in the current folder; what failed."""
    refused = []
    for suite, selection in selections:
        if not selection.whole and not selection.files:
            continue
        if suite.probe:
            error = _probe(suite, account)
            if error is not None:
                who = account or "this user"
                _say(f"{suite.name}: left to CI: no browser can start as {who}: {error}")
                continue
        words = _filled(config.words(suite.run), {"workers": str(suite.workers)})
        # A part of whatever the server suite selected, the selected files after the part.
        shard = part if suite.name == SERVER else None
        if shard:
            assert suite.part
            words += _filled(
                shlex.split(suite.part), {"shards": str(shard[1]), "shard": str(shard[0])}
            )
        if not selection.whole:
            words += selection.files
        _say(
            f"{suite.name}: running"
            + (f" on {merged}" if merged else "")
            + (f" as {account}" if account else "")
        )
        outcome, took = _run(_as_tester(words, account), limit, grace)
        if outcome == "timed out":
            _say(
                f"{suite.name}: timed out after {_duration(took)}, the limit is {_duration(limit)}"
            )
        elif shard:
            rest = "; CI runs the rest" if outcome == "passed" else ""
            _say(
                f"{suite.name}: part {shard[0]} of {shard[1]} {outcome} in {_duration(took)}{rest}"
            )
        else:
            _say(f"{suite.name}: {outcome} in {_duration(took)}")
        if outcome != "passed":
            refused.append(f"{suite.name} {outcome}")
    return refused


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
