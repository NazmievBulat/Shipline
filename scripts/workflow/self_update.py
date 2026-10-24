"""The tick keeps the Shipline checkout these scripts are in current, and moves it back by itself
when a new version crashes.

    self_update.py start <pid> [moved]       at the start of a tick: roll back, or update; exits
                                             10 when the checkout moved, so the tick runs again
                                             as the version now checked out, which moves nothing
                                             more in that tick (`moved`); 11 when this checkout
                                             is not one to keep current
    self_update.py watch <pid>               passes the tick's standard error through, and notes
                                             a traceback or an error of the shell in it
    self_update.py end <pid> <exit> [skipped] [watched]
                                             at its end: the tick's exit, counted on the version
                                             it started on; a skipped tick counts neither way
    self_update.py gate <exit> <stderr>      a run of the merge gate by the tick's merge driver

At most once every 10 minutes the tick fetches Shipline's `main`. When `main` moved and the CI
run of `.github/workflows/ci.yml` on that very commit passed, the checkout is moved there: only
a fast-forward of a clean checkout of `main`. A checkout with local changes (files changed, a
commit of its own, another branch) is left alone and named in the wake once.

Only a crash counts against a version; a failure the tick or the gate handles and reports (a
step that failed, GitHub out of reach, a refusal, a wait) is routine. A tick crashed when it
exited other than 0 or 1 (an error of the shell, 127 or 2), when a traceback or an error of the
shell showed in its standard error, or when it ended without its end; a tick stopped by a signal
counts neither way. The merge gate run by the tick's merge driver crashed when it raised, timed
out in one of its own commands, or exited other than the driver defines.

A version is good once it ran 5 ticks in a row without crashing, with no crash of the gate
since its last good run; the last three good commits are kept in `.local/shipline/versions`,
newest first. A version that is not good yet and crashes 3 times in a row, as a tick or as the
gate, is moved back to the last good commit, or to the commit it was taken from when none is
good yet, at the next tick. It is noted as bad and not taken again until `main` moves past it,
and the wake tells the orchestrator once: `shipline rolled back: <bad sha> -> <good sha>,
<reason>`.

Nothing moves while a tick runs from the checkout or holds its lock, or a gate call holds the
merge lock, in any repository whose tick ran from it: the next tick does it. Each tick notes its
start before anything is fetched. `SHIPLINE_PIN=<sha>`, or a file `.local/shipline/pin`, stops
updates and roll-backs; the wake names the pin once. A checkout without a GitHub `origin`,
without `bin/shipline`, that is not a git checkout of its own, or that is the repository the
tick works on is left alone, and no note is written in it.

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
# The exits of `start` when the checkout moved, and when it is not one to keep current.
MOVED = 10
NOT_KEPT = 11
CI_WORKFLOW = ".github/workflows/ci.yml"
# Seconds a local git command, the fetch, and the read of CI may take.
GIT_SECONDS = 60
FETCH_SECONDS = 120
CI_SECONDS = 30
# How long the end of a tick waits for the watch to read what the tick printed.
DRAINED_SECONDS = 5
# How many bad commits are remembered; `main` has long moved past the older ones.
KEEP_BAD = 20
# How much of a crash's line a roll-back's reason carries.
REASON_CHARACTERS = 200
# The tick's own exits: done, and done with a failure it reported. A stop by a signal (HUP,
# INT, TERM) counts neither way.
TICK_EXITS = (0, 1)
STOPPED = (129, 130, 143)
# What git is told by the caller's environment about another repository.
GIT_PLACES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)
# The gate's exits under `--drive` that are answers: merged, wait for a new run, wait.
GATE_ANSWERS = (0, 2, 3)
# A line of standard error that tells a crash: an error of the shell, or a script not found.
SHELL_ERROR = re.compile(
    r".*: line [0-9]+: .*(unbound variable|syntax error.*|command not found|bad substitution)"
    r"|.*can't open file .*|bash: .*: No such file or directory"
)
TRACEBACK = "Traceback (most recent call last):"
SHA = re.compile(r"[0-9a-f]{40}")
GITHUB = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")


def now() -> int:
    """The time now, in seconds. A test names another."""
    return int(os.environ.get("WORKFLOW_NOW") or time.time())


def git(
    *args: str, timeout: float = GIT_SECONDS, folder: Path = HOME
) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if key not in GIT_PLACES}
    return subprocess.run(
        ["git", "-C", str(folder), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**environment, "GIT_TERMINAL_PROMPT": "0"},
        check=False,
    )


def head() -> str:
    done = git("rev-parse", "--verify", "HEAD")
    sha = done.stdout.strip()
    if done.returncode or not SHA.fullmatch(sha):
        raise OSError(f"cannot read the commit of {HOME}")
    return sha


def worked_on() -> Path:
    """The repository the tick works on: `SHIPLINE_REPO`, else the current folder."""
    return Path(os.environ.get("SHIPLINE_REPO") or Path.cwd())


def common_dir(folder: Path) -> Path | None:
    found = git("rev-parse", "--path-format=absolute", "--git-common-dir", folder=folder)
    return Path(found.stdout.strip()).resolve() if found.returncode == 0 else None


def github_repository() -> str | None:
    """The repository Shipline's `origin` names on GitHub, or None when the checkout is not one
    to update."""
    if not (HOME / "bin" / "shipline").is_file():
        return None
    # A copy inside another repository would move that repository.
    top = git("rev-parse", "--show-toplevel").stdout.strip()
    if not top or Path(top).resolve() != HOME:
        return None
    url = git("config", "--get", "remote.origin.url").stdout.strip()
    found = GITHUB.fullmatch(url)
    if not found:
        return None
    # The repository the tick works on keeps itself current by its own merges.
    if common_dir(HOME) == common_dir(worked_on()):
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


def one_line(text: str) -> str:
    """One short line of printable characters, for the wake."""
    text = "".join(character for character in text.strip() if character.isprintable())
    return text if len(text) <= REASON_CHARACTERS else text[: REASON_CHARACTERS - 3] + "..."


def read_trial() -> dict[str, Any] | None:
    """The version on trial: its commit, the good commit to go back to, its ticks in a row
    without crashing, and its crashes in a row as a tick and as the gate."""
    try:
        trial = json.loads((NOTES / "trial.json").read_text())
    except FileNotFoundError, ValueError:
        return None
    if not isinstance(trial, dict) or not SHA.fullmatch(str(trial.get("sha"))):
        return None
    # A trial an older version wrote may lack a count.
    for count in ("ticks", "tick_failures", "gate_failures"):
        if not isinstance(trial.get(count), int):
            trial[count] = 0
    if not SHA.fullmatch(str(trial.get("good"))):
        trial["good"] = None
    return trial


def write_trial(trial: dict[str, Any] | None) -> None:
    if trial is None:
        (NOTES / "trial.json").unlink(missing_ok=True)
    else:
        write(NOTES / "trial.json", json.dumps(trial) + "\n")


def new_trial(sha: str, came_from: str | None = None) -> dict[str, Any]:
    """`sha` on trial. It goes back to the last good commit, or to the one it came from when
    none is good yet."""
    good = next((version for version in read_lines("versions") if version != sha), came_from)
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
        return ".local/shipline/pin", (NOTES / "pin").read_text().strip()
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


def started_at(pid: int) -> str:
    """When the process started, in clock ticks since boot; empty when it is gone or cannot
    be read."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as stat:
            # The name can hold spaces and brackets; the fields after it cannot.
            return stat.read().rpartition(")")[2].split()[19]
    except OSError, IndexError:
        return ""


def run_note(pid: int, suffix: str = "") -> Path:
    return NOTES / "runs" / f"{pid}{suffix}"


def forget_run(pid: int) -> None:
    for suffix in ("", ".crash", ".drained"):
        run_note(pid, suffix).unlink(missing_ok=True)


def runs() -> Iterator[tuple[int, str, bool]]:
    """Each tick that noted its start: its pid, commit, and whether that tick still runs (its
    pid is alive and started when the note says)."""
    folder = NOTES / "runs"
    if not folder.is_dir():
        return
    for run in folder.iterdir():
        if not run.name.isdigit():
            continue
        try:
            sha, started = (run.read_text().split() + ["", ""])[:2]
        except FileNotFoundError:
            continue
        yield int(run.name), sha, bool(started) and started_at(int(run.name)) == started


def record_tick(sha: str, crash: str | None) -> None:
    """A tick of `sha` ended, crashed or not; counted only while `sha` is on trial."""
    trial = read_trial()
    if trial is None or trial["sha"] != sha:
        return
    if crash is None:
        trial["ticks"] += 1
        trial["tick_failures"] = 0
        # A crash of the gate holds it back until the gate runs again without one.
        if trial["ticks"] >= GOOD_AFTER_TICKS and not trial["gate_failures"]:
            versions = [sha, *(version for version in read_lines("versions") if version != sha)]
            write(NOTES / "versions", "\n".join(versions[:KEEP_GOOD]) + "\n")
            write_trial(None)
            return
    else:
        trial["ticks"] = 0
        trial["tick_failures"] += 1
        trial["last_tick"] = one_line(crash)
    write_trial(trial)


def reap(own: int) -> None:
    """A tick that ended without its end (killed outright, the machine went down) crashed."""
    for pid, sha, running in list(runs()):
        if pid != own and not running:
            record_tick(sha, "it ended without its end")
            forget_run(pid)


def register_locks() -> None:
    """Notes the locks a tick and a gate call of the repository the tick works on hold: no move
    is made while one of them is held."""
    repository = worked_on()
    common = common_dir(repository)
    if common is None:
        return
    main = common.parent if common.name == ".git" else repository.resolve()
    state = ".local/workflow"
    try:
        given = json.loads((main / "shipline.json").read_text()).get("state_folder", state)
        if isinstance(given, str) and given and not given.startswith("/") and ".." not in given:
            state = given
    except OSError, ValueError, AttributeError:
        pass
    known = (NOTES / "locks").read_text().splitlines() if (NOTES / "locks").exists() else []
    wanted = [str(common / "shipline-tick.lock"), str(main / state / "merge.lock")]
    wanted = [path for path in wanted if "\n" not in path and path not in known]
    if wanted:
        write(NOTES / "locks", "".join(f"{path}\n" for path in [*known, *wanted]))


def locks_held() -> bool:
    """Whether a tick or a gate call holds its lock now; asked without waiting."""
    try:
        paths = (NOTES / "locks").read_text().splitlines()
    except FileNotFoundError:
        return False
    for path in paths:
        try:
            with open(path) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
        except OSError:
            continue
    return False


def busy(own: int) -> bool:
    """Whether another tick runs from the checkout, or a tick or a gate call holds its lock."""
    return any(running for pid, _, running in runs() if pid != own) or locks_held()


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
        "shipline not updated: its checkout has local changes",
    )


def roll_back(current: str) -> bool:
    """Moves a crashing version on trial back to its good commit. True when it moved."""
    trial = read_trial()
    if trial is None or trial["sha"] != current or not trial.get("good"):
        return False
    if trial["tick_failures"] >= ROLL_BACK_AFTER:
        reason = (
            f"the tick crashed {ROLL_BACK_AFTER} times in a row (last: {trial['last_tick']})"
        )
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
        listed = json.loads(done.stdout)["workflow_runs"] if done.returncode == 0 else None
    except ValueError, KeyError, TypeError:
        listed = None
    if not isinstance(listed, list):
        print(f"cannot read Shipline's CI for {sha}", file=sys.stderr)
        return False
    own = [
        run
        for run in listed
        if isinstance(run, dict) and run.get("path") == CI_WORKFLOW and run.get("head_sha") == sha
    ]
    if not own:
        return False
    latest = max(own, key=lambda run: (str(run.get("created_at")), int(run.get("id") or 0)))
    return latest.get("status") == "completed" and latest.get("conclusion") == "success"


def update(repository: str, current: str, pid: int) -> bool:
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
    # Asked again under the lock every tick takes to note its start: a tick that started while
    # this one fetched is seen.
    with locked():
        if head() != current or busy(pid):
            return False
        moved = git("merge", "--quiet", "--ff-only", target)
        if moved.returncode:
            print(f"cannot move Shipline to {target}: {moved.stderr.strip()}", file=sys.stderr)
            local_changes()
            return False
        write_trial(new_trial(target, current))
        write(run_note(pid), f"{target} {started_at(pid)}\n")
    print(f"shipline updated: {current} -> {target}", flush=True)
    return True


def start(pid: int, moved_already: bool) -> int:
    repository = github_repository()
    if repository is None:
        return NOT_KEPT
    moved = False
    with locked():
        # Noted before anything is fetched: a move made meanwhile sees this tick.
        write(run_note(pid), f"{head()} {started_at(pid)}\n")
        register_locks()
        reap(pid)
        # A tick that runs from the checkout would run the rest of its steps from another
        # version, and so would a gate call.
        held = moved_already or busy(pid)
        current = head()
        pinned = note_pin()
        if not pinned and not held:
            moved = roll_back(current)
            current = head()
            write(run_note(pid), f"{current} {started_at(pid)}\n")
        trial = read_trial()
        if current not in read_lines("versions") and (trial is None or trial["sha"] != current):
            write_trial(new_trial(current))
    if not pinned and not held and not moved:
        with locked("update.lock", wait=False) as mine:
            moved = mine and update(repository, current, pid)
    return MOVED if moved else 0


def watch(pid: int) -> None:
    """Passes the tick's standard error through to this one's standard output, all of it, and
    notes the first crash it shows: a traceback (by the exception it ends with) or an error of
    the shell. The line `shipline-tick-ended <pid>` the tick's end prints is not passed: it
    says everything before it was read."""
    end_line = f"shipline-tick-ended {pid}\n".encode()
    noted = run_note(pid).exists()
    crash: str | None = None
    in_traceback = False

    # Reading goes on whatever fails here: a watch that left would break the tick's writes.
    def passed(data: bytes) -> None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except OSError:
            pass

    def note(suffix: str, text: str) -> None:
        try:
            write(run_note(pid, suffix), text)
        except OSError:
            pass

    for raw in sys.stdin.buffer:
        if raw.endswith(end_line):
            before = raw[: -len(end_line)]
            if before:
                passed(before + b"\n")
            if noted:
                note(".drained", "")
            continue
        passed(raw)
        if not noted or crash is not None:
            continue
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if line.startswith(TRACEBACK):
            in_traceback = True
        elif in_traceback and line and not line[0].isspace():
            crash = line
        elif SHELL_ERROR.fullmatch(line):
            crash = line
        if crash is not None:
            note(".crash", one_line(crash) + "\n")


def end(pid: int, code: int, skipped: bool, watched: bool) -> None:
    if not run_note(pid).is_file():
        return
    if watched:
        # The watch reads what the tick printed last a moment after it.
        deadline = time.monotonic() + DRAINED_SECONDS
        while not run_note(pid, ".drained").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    try:
        noted = run_note(pid, ".crash").read_text().strip()
    except FileNotFoundError:
        noted = ""
    with locked():
        sha = (run_note(pid).read_text().split() or [""])[0]
        forget_run(pid)
        if skipped or code in STOPPED:
            return
        if code not in TICK_EXITS:
            record_tick(sha, noted or f"exit {code}")
        else:
            record_tick(sha, noted or None)


def gate_crash(code: int, stderr: str) -> str | None:
    """Why a run of the gate under `--drive` is a crash, or None when it gave an answer.

    Exit 1 is a refusal when the gate said so; what a pull request's own checks printed rides
    on that line's message, and is not read. A refusal is a crash only when one of the gate's
    own commands timed out, which the line ends with. Exit 1 without a refusal is an exception
    the gate did not catch."""
    if code in GATE_ANSWERS:
        return None
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    answer = next(
        (line for line in lines if line.startswith(("merge refused: ", "merge waiting: "))), None
    )
    if code == 1 and answer is not None:
        return answer if re.search(r" timed out after [0-9.]+s$", answer) else None
    if code == 1 and lines:
        return lines[-1]
    return f"exit {code}"


def gate_ran(code: int, stderr: str) -> None:
    """A run of the merge gate by the tick's merge driver, counted while a version is on trial.
    A crash also sets the ticks without one back to 0."""
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
            trial["ticks"] = 0
            trial["gate_failures"] += 1
            trial["last_gate"] = one_line(crash)
        write_trial(trial)


def main() -> int:
    arguments = sys.argv[1:]
    try:
        if (
            len(arguments) in (2, 3)
            and arguments[0] == "start"
            and arguments[1].isdigit()
            and arguments[2:] in ([], ["moved"])
        ):
            return start(int(arguments[1]), arguments[2:] == ["moved"])
        if len(arguments) == 2 and arguments[0] == "watch" and arguments[1].isdigit():
            watch(int(arguments[1]))
            return 0
        if (
            len(arguments) >= 3
            and arguments[0] == "end"
            and arguments[1].isdigit()
            and re.fullmatch(r"-?[0-9]+", arguments[2])
            and set(arguments[3:]) <= {"skipped", "watched"}
        ):
            end(
                int(arguments[1]),
                int(arguments[2]),
                "skipped" in arguments[3:],
                "watched" in arguments[3:],
            )
            return 0
        if len(arguments) == 3 and arguments[0] == "gate" and re.fullmatch(r"-?[0-9]+", arguments[1]):
            gate_ran(int(arguments[1]), arguments[2])
            return 0
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"shipline self-update: {error}", file=sys.stderr)
        return 1
    print(
        "usage: self_update.py start <pid> [moved] | watch <pid> | "
        "end <pid> <exit> [skipped] [watched] | gate <exit> <stderr>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
