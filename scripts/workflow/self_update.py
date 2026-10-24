"""The tick keeps the Shipline checkout these scripts are in current, and moves it back by itself
when a new version fails.

    self_update.py start <pid>               at the start of a tick: roll back, or update; exits
                                             10 when the checkout moved, so the tick runs again
                                             as the version now checked out
    self_update.py end <pid> <exit> [skipped]
                                             at its end: the tick's exit, counted on the version
                                             it started on; a skipped tick counts neither way
    self_update.py gate <exit> <stderr>      a run of the merge gate by the tick's merge driver

At most once every 10 minutes the tick fetches Shipline's `main`. When `main` moved and the CI
run of `.github/workflows/ci.yml` on that very commit passed, the checkout is moved there: only
a fast-forward of a clean checkout of `main`. A checkout with local changes (files changed, a
commit of its own, another branch) is left alone and named in the wake once.

A version is good once it ran 5 ticks in a row without failing; the last three good commits are
kept in `.local/shipline/versions`, newest first. A version that is not good yet and fails 3
times in a row, as a tick (a non-zero exit, or a tick that ended without its end) or as a crash
of the merge gate (a traceback, a timeout or an exit the driver does not define; a refusal and
a wait are answers), is moved back to the last good commit at the next tick. It is noted as bad
and not taken again until `main` moves past it, and the wake tells the orchestrator once:
`shipline rolled back: <bad sha> -> <good sha>, <reason>`.

`SHIPLINE_PIN=<sha>`, or a file `.local/shipline/pin`, stops updates and roll-backs; the wake
names the pin once. A checkout without a GitHub `origin`, without `bin/shipline`, or that is the
repository the tick works on is left alone, and no note is written in it.

Everything is kept in `.local/shipline` of the Shipline checkout, which its `.gitignore` leaves
out; the lines for the wake in `wake.pending`.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HOME = Path(__file__).resolve().parents[2]
NOTES = HOME / ".local" / "shipline"
FETCH_EVERY_SECONDS = 10 * 60
GOOD_AFTER_TICKS = 5
ROLL_BACK_AFTER = 3
KEEP_GOOD = 3
# The exit of `start` when the checkout moved.
MOVED = 10
CI_WORKFLOW = ".github/workflows/ci.yml"
# Seconds a local git command, the fetch, and the read of CI may take.
GIT_SECONDS = 60
FETCH_SECONDS = 120
CI_SECONDS = 30
# How many bad commits are remembered; `main` has long moved past the older ones.
KEEP_BAD = 20
# The gate's exits under `--drive` that are answers: merged, wait for a new run, wait.
GATE_ANSWERS = (0, 2, 3)
SHA = re.compile(r"[0-9a-f]{40}")
GITHUB = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")


def now() -> int:
    """The time now, in seconds. A test names another."""
    return int(os.environ.get("WORKFLOW_NOW") or time.time())


def git(*args: str, timeout: float = GIT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(HOME), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        check=False,
    )


def head() -> str:
    done = git("rev-parse", "--verify", "HEAD")
    sha = done.stdout.strip()
    if done.returncode or not SHA.fullmatch(sha):
        raise OSError(f"cannot read the commit of {HOME}")
    return sha


def github_repository() -> str | None:
    """The repository Shipline's `origin` names on GitHub, or None when the checkout is not one
    to update."""
    if not (HOME / "bin" / "shipline").is_file():
        return None
    url = git("config", "--get", "remote.origin.url").stdout.strip()
    found = GITHUB.fullmatch(url)
    if not found:
        return None
    # The repository the tick works on keeps itself current by its own merges.
    repository = Path(os.environ.get("SHIPLINE_REPO") or Path.cwd())
    common = ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    own = git(*common).stdout.strip()
    worked_on = subprocess.run(
        ["git", "-C", str(repository), *common],
        capture_output=True,
        text=True,
        timeout=GIT_SECONDS,
        check=False,
    ).stdout.strip()
    if own and Path(own).resolve() == Path(worked_on or "/nonexistent").resolve():
        return None
    return found[1]


@contextmanager
def locked(name: str = "lock", wait: bool = True) -> Iterator[bool]:
    """The notes' lock, held briefly; `update.lock` is held while GitHub is asked, and is not
    waited for."""
    NOTES.mkdir(parents=True, exist_ok=True)
    with (NOTES / name).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(text)
    temporary.replace(path)


def read_lines(name: str) -> list[str]:
    try:
        return [line for line in (NOTES / name).read_text().split() if SHA.fullmatch(line)]
    except FileNotFoundError:
        return []


def read_trial() -> dict[str, Any] | None:
    """The version on trial: its commit, the good commit to go back to, its ticks in a row
    without failing, and its failures in a row."""
    try:
        trial = json.loads((NOTES / "trial.json").read_text())
    except FileNotFoundError, ValueError:
        return None
    return trial if isinstance(trial, dict) and SHA.fullmatch(str(trial.get("sha"))) else None


def write_trial(trial: dict[str, Any] | None) -> None:
    if trial is None:
        (NOTES / "trial.json").unlink(missing_ok=True)
    else:
        write(NOTES / "trial.json", json.dumps(trial) + "\n")


def new_trial(sha: str) -> dict[str, Any]:
    good = next((version for version in read_lines("versions") if version != sha), None)
    return {"sha": sha, "good": good, "ticks": 0, "tick_failures": 0, "gate_failures": 0}


def tell(kind: str, line: str) -> None:
    """A line for the wake, told once."""
    write(NOTES / "wake.pending" / f"{now()}-{kind}-{os.getpid()}.json", json.dumps({"line": line}))


def tell_once(mark: str, value: str, kind: str, line: str) -> None:
    """Tells the line once while `value` holds."""
    path = NOTES / mark
    try:
        if path.read_text() == value:
            return
    except FileNotFoundError:
        pass
    tell(kind, line)
    write(path, value)


def pin() -> tuple[str, str] | None:
    """What pins the checkout, and the commit it names, when something does."""
    named = os.environ.get("SHIPLINE_PIN", "")
    if named:
        return "SHIPLINE_PIN", named
    try:
        return str(NOTES / "pin"), (NOTES / "pin").read_text().strip()
    except FileNotFoundError:
        return None


def note_pin() -> bool:
    pinned = pin()
    if pinned is None:
        (NOTES / "pin-noted").unlink(missing_ok=True)
        return False
    source, value = pinned
    # Only a commit's characters reach the wake.
    value = re.sub(r"[^0-9A-Za-z._-]", "", value)[:40]
    tell_once(
        "pin-noted",
        f"{source} {value}",
        "pinned",
        f"shipline pinned by {source}{f' ({value})' if value else ''}: not updated",
    )
    return True


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def record_tick(sha: str, passed: bool, reason: str) -> None:
    """A tick of `sha` ended; counted only while `sha` is on trial."""
    trial = read_trial()
    if trial is None or trial["sha"] != sha:
        return
    if passed:
        trial["ticks"] += 1
        trial["tick_failures"] = 0
        if trial["ticks"] >= GOOD_AFTER_TICKS:
            versions = [sha, *(version for version in read_lines("versions") if version != sha)]
            write(NOTES / "versions", "\n".join(versions[:KEEP_GOOD]) + "\n")
            write_trial(None)
            return
    else:
        trial["ticks"] = 0
        trial["tick_failures"] += 1
        trial["last_tick"] = reason
    write_trial(trial)


def reap(own: int) -> None:
    """A tick that ended without its end (killed, or its end never ran) failed."""
    runs = NOTES / "runs"
    if not runs.is_dir():
        return
    for run in runs.iterdir():
        if not run.name.isdigit() or int(run.name) == own or alive(int(run.name)):
            continue
        record_tick(run.read_text().strip(), False, "it ended without its end")
        run.unlink(missing_ok=True)


def clean() -> bool:
    """A checkout of `main` with no change of its own to a tracked file."""
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return branch.stdout.strip() == "main" and status.returncode == 0 and not status.stdout.strip()


def local_changes() -> None:
    tell_once(
        "local-changes-noted",
        "noted",
        "local-changes",
        f"shipline not updated: its checkout {HOME} has local changes",
    )


def roll_back(current: str) -> bool:
    """Moves a failing version on trial back to its good commit. True when it moved."""
    trial = read_trial()
    if trial is None or trial["sha"] != current or not trial.get("good"):
        return False
    if trial["tick_failures"] >= ROLL_BACK_AFTER:
        reason = f"the tick failed {ROLL_BACK_AFTER} times in a row (last: {trial['last_tick']})"
    elif trial["gate_failures"] >= ROLL_BACK_AFTER:
        reason = (
            f"the merge gate crashed {ROLL_BACK_AFTER} times in a row "
            f"(last: {trial['last_gate']})"
        )
    else:
        return False
    good = trial["good"]
    if not clean():
        local_changes()
        return False
    moved = git("reset", "--quiet", "--keep", good)
    if moved.returncode:
        print(f"cannot move Shipline back to {good}: {moved.stderr.strip()}", file=sys.stderr)
        return False
    bad = [current, *(sha for sha in read_lines("bad") if sha != current)]
    write(NOTES / "bad", "\n".join(bad[:KEEP_BAD]) + "\n")
    write_trial(None)
    tell("rolled-back", f"shipline rolled back: {current} -> {good}, {reason}")
    print(f"shipline rolled back: {current} -> {good}, {reason}", flush=True)
    return True


def ci_passed(repository: str, sha: str) -> bool:
    """Whether the CI run of that very commit of `main` passed. Unreadable is not passed."""
    from github_budget import Limited, call

    url = f"repos/{repository}/actions/runs?head_sha={sha}&event=push&per_page=100"
    try:
        done = call("tick", "api", url, timeout=CI_SECONDS)
    except (Limited, OSError, subprocess.TimeoutExpired) as error:
        print(f"cannot read Shipline's CI for {sha}: {error}", file=sys.stderr)
        return False
    try:
        runs = json.loads(done.stdout)["workflow_runs"] if done.returncode == 0 else None
    except ValueError, KeyError, TypeError:
        runs = None
    if not isinstance(runs, list):
        print(f"cannot read Shipline's CI for {sha}", file=sys.stderr)
        return False
    own = [
        run
        for run in runs
        if isinstance(run, dict) and run.get("path") == CI_WORKFLOW and run.get("head_sha") == sha
    ]
    if not own:
        return False
    latest = max(own, key=lambda run: (str(run.get("created_at")), int(run.get("id") or 0)))
    return latest.get("status") == "completed" and latest.get("conclusion") == "success"


def update(repository: str, current: str) -> bool:
    """Fetches `main` when it is due and fast-forwards to it once its CI passed. True when the
    checkout moved."""
    try:
        fetched = int((NOTES / "fetched").read_text())
    except FileNotFoundError, ValueError:
        fetched = 0
    if now() - fetched < FETCH_EVERY_SECONDS:
        return False
    write(NOTES / "fetched", f"{now()}\n")
    done = git(
        "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main",
        timeout=FETCH_SECONDS,
    )
    if done.returncode:
        print(f"cannot fetch Shipline's main: {done.stderr.strip()}", file=sys.stderr)
        return False
    target = git("rev-parse", "--verify", "refs/remotes/origin/main").stdout.strip()
    if not SHA.fullmatch(target) or target == current or target in read_lines("bad"):
        return False
    if not clean() or git("merge-base", "--is-ancestor", current, target).returncode:
        local_changes()
        return False
    (NOTES / "local-changes-noted").unlink(missing_ok=True)
    if not ci_passed(repository, target):
        return False
    with locked():
        if head() != current:
            return False
        moved = git("merge", "--quiet", "--ff-only", target)
        if moved.returncode:
            print(f"cannot move Shipline to {target}: {moved.stderr.strip()}", file=sys.stderr)
            local_changes()
            return False
        write_trial(new_trial(target))
    print(f"shipline updated: {current} -> {target}", flush=True)
    return True


def start(pid: int) -> int:
    repository = github_repository()
    if repository is None:
        return 0
    moved = False
    with locked():
        reap(pid)
        current = head()
        pinned = note_pin()
        if not pinned:
            moved = roll_back(current)
            current = head()
        trial = read_trial()
        if current not in read_lines("versions") and (trial is None or trial["sha"] != current):
            write_trial(new_trial(current))
    if not pinned and not moved:
        with locked("update.lock", wait=False) as mine:
            moved = mine and update(repository, current)
    with locked():
        write(NOTES / "runs" / str(pid), head() + "\n")
    return MOVED if moved else 0


def end(pid: int, code: int, skipped: bool) -> None:
    run = NOTES / "runs" / str(pid)
    if not run.is_file():
        return
    with locked():
        sha = run.read_text().strip()
        run.unlink(missing_ok=True)
        if not skipped:
            record_tick(sha, code == 0, f"exit {code}")


def gate_crash(code: int, stderr: str) -> str | None:
    """Why a run of the gate under `--drive` is a crash, or None when it gave an answer."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if code in GATE_ANSWERS:
        return None
    if code == 1 and "Traceback (most recent call last):" in stderr:
        return lines[-1]
    if code == 1:
        timed_out = [line for line in lines if re.search(r" timed out after [0-9.]+s$", line)]
        return timed_out[-1] if timed_out else None
    return f"exit {code}"


def gate_ran(code: int, stderr: str) -> None:
    """A run of the merge gate by the tick's merge driver, counted while a version is on trial."""
    if not (NOTES / "trial.json").exists():
        return
    crash = gate_crash(code, stderr)
    with locked():
        trial = read_trial()
        if trial is None or trial["sha"] != head():
            return
        if crash is None:
            trial["gate_failures"] = 0
        else:
            trial["gate_failures"] += 1
            trial["last_gate"] = crash
        write_trial(trial)


def main() -> int:
    arguments = sys.argv[1:]
    try:
        if len(arguments) == 2 and arguments[0] == "start" and arguments[1].isdigit():
            return start(int(arguments[1]))
        if (
            len(arguments) in (3, 4)
            and arguments[0] == "end"
            and arguments[1].isdigit()
            and re.fullmatch(r"-?[0-9]+", arguments[2])
            and arguments[3:] in ([], ["skipped"])
        ):
            end(int(arguments[1]), int(arguments[2]), arguments[3:] == ["skipped"])
            return 0
        if len(arguments) == 3 and arguments[0] == "gate" and re.fullmatch(r"-?[0-9]+", arguments[1]):
            gate_ran(int(arguments[1]), arguments[2])
            return 0
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"shipline self-update: {error}", file=sys.stderr)
        return 1
    print(
        "usage: self_update.py start <pid> | end <pid> <exit> [skipped] | gate <exit> <stderr>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
