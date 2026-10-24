"""Shared budget and conditional reads for the host's background GitHub clients.

Keep gh's stdout and exit status intact. HTTP debugging stays in memory: only counts and
rate-limit headers are persisted, never credentials, request URLs or request bodies.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

RESERVE = 500
# Only the hourly budgets keep a reserve. Small per-minute resources (search: 30) stop
# background work only when they are exhausted.
HOURLY = {"core", "graphql"}
MAX_IN_FLIGHT = 2
MIN_SPACING = 0.1
MAX_SPACING = 0.3
SLOT_ENV = "WORKFLOW_GITHUB_SLOT_FD"
# The runner programs' own identity (github_identity.py). Its budget and secondary limit are
# GitHub's for that identity alone, so its state and call slots are kept apart from the login's.
RUNNER = "runner"
_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


class Limited(Exception):
    """A background client must leave the remaining budget for interactive work."""


class Unusable(Exception):
    """The runner identity is there but gives no token; the reason is already logged."""


def folder() -> Path:
    path = Path.home() / ".local/state/workflow/github-budget"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextmanager
def locked() -> Iterator[Path]:
    path = folder()
    with (path / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except OSError, ValueError:
        return {}


def save(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        json.dump(value, output)
    temporary.replace(path)


def _prefix(identity: str | None) -> str:
    if identity not in (None, RUNNER):
        raise ValueError(identity)
    return "" if identity is None else identity + "-"


def _effective(identity: str | None) -> str | None:
    """The identity a call really uses: a machine without one uses the login."""
    if identity is None:
        return None
    _prefix(identity)
    # Beside this file in the runner install and in scripts/workflow, never in the sessions' copy.
    import github_identity

    return identity if github_identity.configured() else None


def _runner_token(program: str) -> str | None:
    import github_identity

    marker = folder() / (
        "login-instead-" + hashlib.sha256(program.encode()).hexdigest()
    )
    fault = folder() / (
        "identity-fault-" + hashlib.sha256(program.encode()).hexdigest()
    )
    try:
        token = github_identity.token()
    except github_identity.IdentityError as error:
        message = f"{program}: the runners' GitHub identity gives no token: {error}"
        # Said once for each fault, not on every poll; the call fails each time. Printed
        # here, as the budget pause is: a caller in this process may not show gh's stderr.
        if read(fault).get("message") != message:
            print(message, file=sys.stderr, flush=True)
            save(fault, {"message": message})
        raise Unusable() from None
    fault.unlink(missing_ok=True)
    if token is None:
        if not marker.exists():
            print(
                f"{program}: no GitHub identity of its own in {github_identity.folder()}; "
                "using the gh login (runner/README.md says how to give it one)",
                file=sys.stderr,
                flush=True,
            )
            marker.touch()
    else:
        marker.unlink(missing_ok=True)
    return token


def delay(
    program: str, *, background: bool = True, identity: str | None = None
) -> float:
    now = time.time()
    identity = _effective(identity)
    with locked() as path:
        state = read(path / (_prefix(identity) + "budget.json"))
        until = max(
            [0.0]
            + [
                float(row["reset"]) + 1
                for resource, row in state.items()
                if row["remaining"]
                < (RESERVE if background and resource in HOURLY else 1)
                and row["reset"] >= now
            ]
        )
        if until <= now:
            return 0
        marker = path / (
            "logged-"
            + hashlib.sha256((_prefix(identity) + program).encode()).hexdigest()
        )
        if read(marker).get("until") != until:
            print(
                f"{program}: GitHub budget low; requests pause until "
                f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(until))}",
                file=sys.stderr,
                flush=True,
            )
            save(marker, {"until": until})
        return until - now


def wait(program: str, *, background: bool = True, identity: str | None = None) -> None:
    while seconds := delay(program, background=background, identity=identity):
        time.sleep(min(seconds, 60))


def poll_delay(seconds: float) -> float:
    return random.uniform(seconds * 0.8, seconds * 1.2)


def raw_gh() -> str:
    given = os.environ.get("WORKFLOW_GITHUB_RAW_GH")
    candidates = (
        [given] if given else [str(Path(part) / "gh") for part in os.get_exec_path()]
    )
    skipped = False
    for name in candidates:
        if not name or not os.access(name, os.X_OK):
            continue
        path = Path(name).resolve()
        if path.name == "github-gh.sh" or (
            path.name == "gh" and path.parent.name == "gh-bin"
        ):
            skipped = True
            recorded = path.with_name("github-real-gh")
            if recorded.exists():
                return recorded.read_text().strip()
            continue
        return name if given or skipped else "gh"
    return shutil.which("gh", path="/usr/local/bin:/usr/bin:/bin") or "/usr/bin/gh"


@contextmanager
def admitted(arguments: tuple[str, ...], identity: str | None = None) -> Iterator[int]:
    path = folder()
    prefix = _prefix(identity)
    slots = [path / f"{prefix}call-{number}.lock" for number in range(MAX_IN_FLIGHT)]
    inherited = os.environ.get(SLOT_ENV, "")
    borrowed = False
    if inherited.isdigit():
        try:
            stat = os.fstat(int(inherited))
            if any(
                slot.exists()
                and (slot.stat().st_dev, slot.stat().st_ino)
                == (stat.st_dev, stat.st_ino)
                for slot in slots
            ):
                borrowed = True
        except OSError:
            pass
    if borrowed:
        yield int(inherited)
        return
    while True:
        for slot in random.sample(slots, len(slots)):
            lock = slot.open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                continue
            try:
                with (path / f"{prefix}starts.lock").open("a") as starts:
                    fcntl.flock(starts, fcntl.LOCK_EX)
                    previous = read(path / f"{prefix}started.json").get("at", 0)
                    mutating = arguments[:2] in (
                        ("pr", "merge"),
                        ("pr", "create"),
                        ("workflow", "run"),
                        ("run", "rerun"),
                    ) or (
                        arguments
                        and arguments[0] == "api"
                        and _cache_key(arguments) is None
                    )
                    spacing = (
                        random.uniform(1, 1.5)
                        if mutating
                        else random.uniform(MIN_SPACING, MAX_SPACING)
                    )
                    time.sleep(max(0, previous + spacing - time.time()))
                    save(path / f"{prefix}started.json", {"at": time.time()})
                yield lock.fileno()
            finally:
                lock.close()
            return
        time.sleep(random.uniform(0.05, 0.15))


def _trace(stderr: str) -> tuple[list[dict[str, str]], str]:
    responses: list[dict[str, str]] = []
    diagnostic: list[str] = []
    debugging = False
    current: dict[str, str] | None = None
    body = False
    for line in stderr.splitlines(keepends=True):
        line = _ANSI.sub(b"", line.encode("utf-8", "surrogateescape")).decode(
            "utf-8", "surrogateescape"
        )
        if line.startswith("* Request at "):
            debugging = True
            current = None
            body = False
        elif line.startswith("< HTTP/"):
            current = {"status": line.split()[2], "body": ""}
            responses.append(current)
        elif line.startswith("< ") and ":" in line and responses:
            key, value = line[2:].split(":", 1)
            responses[-1][key.lower()] = value.strip()
        elif line.startswith("* Request took "):
            debugging = False
        elif debugging and current is not None and not line.startswith((">", "*")):
            if body:
                current["body"] += line
            elif not line.strip():
                body = True
        elif not debugging:
            diagnostic.append(line)
    return responses, "".join(diagnostic)


def _partial_page(arguments: tuple[str, ...], response: dict[str, str]) -> bool:
    if "--paginate" not in arguments:
        return True
    # A full last page may gain a next page without changing its body/ETag (e.g.
    # a reviews array grows from exactly 30 to 31). Only a strictly partial page
    # proves this cached response represents the complete collection.
    match = re.search(r"[?&]per_page=([0-9]+)", " ".join(arguments))
    size = int(match[1]) if match else 30
    try:
        value = json.loads(response["body"])
    except KeyError, ValueError:
        return False
    if isinstance(value, list):
        return len(value) < size
    if isinstance(value, dict):
        arrays = [row for row in value.values() if isinstance(row, list)]
        return len(arrays) == 1 and len(arrays[0]) < size
    return False


def _cache_key(arguments: tuple[str, ...], identity: str | None = None) -> str | None:
    # gh defaults to POST when fields or input are present. Never cache a mutation,
    # a GraphQL query, or a caller's own conditional/cache policy.
    if not arguments or arguments[0] != "api":
        return None
    if any(
        arg in {"--field", "--raw-field", "--input", "--cache", "--header"}
        or arg.startswith(
            (
                "-f",
                "-F",
                "-X",
                "-H",
                "--field=",
                "--raw-field=",
                "--input=",
                "--method=",
                "--header=",
                "--cache=",
            )
        )
        for arg in arguments
    ):
        return None
    if "-X" in arguments or "--method" in arguments or "graphql" in arguments:
        return None
    # The runner identity's own token changes hourly; its name keeps its answers apart.
    who = [
        os.environ.get(key, "")
        for key in ("GH_HOST", "GH_REPO", "GH_TOKEN", "GITHUB_TOKEN")
    ] + ([identity] if identity else [])
    return hashlib.sha256(json.dumps([arguments, who]).encode()).hexdigest()


def _watch(
    program: str,
    arguments: tuple[str, ...],
    timeout: float | None,
    background: bool,
    execute: Any,
    identity: str | None = None,
) -> subprocess.CompletedProcess[str]:
    position = _watch_position(arguments)
    assert position is not None
    run = arguments[position : position + 2] == ("run", "watch")
    interval = 3.0 if run else 10.0
    poll = list(arguments[:position])
    poll.extend(("run", "view") if run else ("pr", "checks"))
    iterator = iter(arguments[position + 2 :])
    for argument in iterator:
        if argument.split("=", 1)[0] in {"--watch", "--fail-fast", "--exit-status"}:
            continue
        if argument in {"--interval", "-i"}:
            interval = float(next(iterator))
        elif argument.startswith(("--interval=", "-i=")):
            interval = float(argument.split("=", 1)[1])
        elif argument.startswith("-i") and len(argument) > 2:
            interval = float(argument[2:])
        else:
            poll.append(argument)
    if run:
        poll.extend(["--json", "status,conclusion"])
    while True:
        done = call(
            program,
            *poll,
            timeout=timeout,
            background=background,
            execute=execute,
            identity=identity,
        )
        if run:
            if done.returncode:
                return done
            value = json.loads(done.stdout)
            pending = value["status"] != "completed"
            failed = value["conclusion"] != "success"
        else:
            # gh emits tab-separated buckets without a terminal. Exit 1 can mean
            # both failed and pending checks; --fail-fast alone stops that watch.
            pending = done.returncode == 8 or any(
                line.split("\t")[1:2] == ["pending"]
                for line in done.stdout.splitlines()
            )
            failed = any(
                line.split("\t")[1:2] == ["fail"] for line in done.stdout.splitlines()
            ) or (done.returncode == 1 and not pending)
        if not pending or (
            failed and any(a in arguments for a in ("--fail-fast", "--fail-fast=true"))
        ):
            code = (
                int(failed)
                if run
                and any(a in arguments for a in ("--exit-status", "--exit-status=true"))
                else (0 if run else done.returncode)
            )
            return subprocess.CompletedProcess(
                arguments, code, done.stdout, done.stderr
            )
        # No admission lock is held during a watch's polling sleep. Every next
        # poll rechecks the shared cooldown and competes for a slot normally.
        time.sleep(poll_delay(max(1, interval)))


def _watch_position(arguments: tuple[str, ...]) -> int | None:
    if "--help" in arguments or "-h" in arguments:
        return None
    for position in range(len(arguments) - 1):
        pair = arguments[position : position + 2]
        if pair == ("run", "watch") or (
            pair == ("pr", "checks")
            and any(a in arguments for a in ("--watch", "--watch=true"))
        ):
            return position
    return None


def call(
    program: str,
    *arguments: str,
    timeout: float | None = 30,
    background: bool = True,
    execute: Callable[
        [list[str], dict[str, str], float | None, int], subprocess.CompletedProcess[str]
    ]
    | None = None,
    identity: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run gh. With identity=RUNNER it carries the runner programs' own token, when this
    machine has one (github_identity.py); without one it uses the login."""
    if _watch_position(arguments) is not None:
        return _watch(program, arguments, timeout, background, execute, identity)
    token = None
    if identity is not None:
        _prefix(identity)
        try:
            token = _runner_token(program)
        except Unusable:
            return subprocess.CompletedProcess([program, *arguments], 1, "", "")
        identity = identity if token else None
    if not background:
        wait(program, background=False, identity=identity)
    if delay(program, background=background, identity=identity):
        raise Limited()
    key = None if execute is _terminal_execute else _cache_key(arguments, identity)
    cached = read(folder() / key) if key else {}
    command = [raw_gh(), *arguments]
    if cached.get("etag"):
        command.extend(["-H", "If-None-Match: " + cached["etag"]])
    with admitted(arguments, identity) as slot:
        if delay(program, background=background, identity=identity):
            raise Limited()
        environment = {**os.environ, "GH_DEBUG": "api", SLOT_ENV: str(slot)}
        if token:
            # gh prefers GH_TOKEN to its login; GITHUB_TOKEN must not stand in for it.
            environment.pop("GITHUB_TOKEN", None)
            environment["GH_TOKEN"] = token
        if execute is None:
            raw = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=timeout,
                env=environment,
                pass_fds=(slot,),
            )
            done = subprocess.CompletedProcess(
                command,
                raw.returncode,
                raw.stdout.decode("utf-8", "surrogateescape"),
                raw.stderr.decode("utf-8", "surrogateescape"),
            )
        else:
            done = execute(command, environment, timeout, slot)
    responses, diagnostic = _trace(done.stderr or "")
    now = time.time()
    with locked() as path:
        budget = path / (_prefix(identity) + "budget.json")
        state = read(budget)
        for response in responses:
            try:
                row = {
                    "remaining": int(response["x-ratelimit-remaining"]),
                    "reset": int(response["x-ratelimit-reset"]),
                }
                resource = response["x-ratelimit-resource"]
            except KeyError, ValueError:
                continue
            old = state.get(resource, {})
            # Concurrent responses may arrive out of order. Keep the lowest remaining
            # count for a window, and never restore an older window over a newer one.
            if row["reset"] >= old.get("reset", 0):
                if row["reset"] == old.get("reset"):
                    row["remaining"] = min(row["remaining"], old["remaining"])
                state[resource] = row
        throttled = next(
            (
                row
                for row in responses
                if row["status"] == "429"
                or (
                    row["status"] == "403"
                    and (
                        "retry-after" in row
                        or "rate limit" in (row.get("body", "") + diagnostic).lower()
                    )
                )
            ),
            None,
        )
        if throttled or (done.returncode and "rate limit" in diagnostic.lower()):
            seconds = 60
            if throttled:
                try:
                    seconds = max(seconds, int(throttled.get("retry-after", "60")))
                except ValueError:
                    pass
            state["secondary"] = {
                "remaining": 0,
                "reset": max(
                    state.get("secondary", {}).get("reset", 0), int(now + seconds)
                ),
            }
        save(budget, state)
        log = path / "requests.jsonl"
        if log.exists() and log.stat().st_size > 10_000_000:
            log.replace(path / "requests.previous.jsonl")
        with log.open("a") as counts:
            counts.write(
                json.dumps(
                    {
                        "at": now,
                        "program": program + (f" ({identity})" if identity else ""),
                        "requests": len(responses),
                        "unchanged": sum(row["status"] == "304" for row in responses),
                        "core": sum(
                            row.get("x-ratelimit-resource") == "core"
                            and row["status"] != "304"
                            for row in responses
                        ),
                        "graphql": sum(
                            row.get("x-ratelimit-resource") == "graphql"
                            for row in responses
                        ),
                    }
                )
                + "\n"
            )
        if key:
            response = responses[0] if len(responses) == 1 else {}
            if response.get("status") == "304" and "stdout" in cached:
                return subprocess.CompletedProcess(command, 0, cached["stdout"], "")
            # A cached single page can be revalidated as a whole. Never revalidate
            # only page one of a previously paginated collection.
            if (
                done.returncode == 0
                and response.get("status") == "200"
                and response.get("etag")
                and 'rel="next"' not in response.get("link", "")
                and _partial_page(arguments, response)
            ):
                save(path / key, {"etag": response["etag"], "stdout": done.stdout})
            else:
                (path / key).unlink(missing_ok=True)
    if throttled:
        delay(program, background=background, identity=identity)
    return subprocess.CompletedProcess(
        command, done.returncode, done.stdout, diagnostic
    )


def report(seconds: float) -> None:
    totals: dict[str, Counter[str]] = {}
    since = time.time() - seconds
    with locked() as path:
        for log in path.glob("requests*.jsonl"):
            for line in log.read_text().splitlines():
                row = json.loads(line)
                if row["at"] >= since:
                    total = totals.setdefault(row["program"], Counter())
                    total.update(
                        {
                            key: row[key]
                            for key in ("requests", "unchanged", "core", "graphql")
                        }
                    )
    print("program\tHTTP requests\t304\tREST non-304\tGraphQL requests")
    for program, total in sorted(totals.items()):
        print(
            program,
            *(total[key] for key in ("requests", "unchanged", "core", "graphql")),
            sep="\t",
        )


def _terminal_execute(
    command: list[str], environment: dict[str, str], timeout: float | None, slot: int
) -> subprocess.CompletedProcess[str]:
    # Keep gh's stdin/stdout attached to the caller's terminal, including its
    # interactive prompts. A separate stderr PTY retains terminal detection while
    # letting us remove HTTP debug material before showing diagnostics.
    master, slave = pty.openpty()
    trace = bytearray()
    pending = b""
    debugging = False
    try:
        with subprocess.Popen(
            command, env=environment, stderr=slave, pass_fds=(slot,)
        ) as child:
            os.close(slave)
            slave = -1
            while True:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                trace.extend(chunk)
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    clean = _ANSI.sub(b"", line)
                    if clean.startswith(b"* Request at "):
                        debugging = True
                    elif clean.startswith(b"* Request took "):
                        debugging = False
                    elif not debugging:
                        sys.stderr.buffer.write(line + b"\n")
                        sys.stderr.buffer.flush()
                # Interactive prompts may have no newline. Debug lines always
                # start with '*', so retain an incomplete possible debug prefix.
                if (
                    pending
                    and not debugging
                    and not _ANSI.sub(b"", pending).startswith(b"*")
                    and not pending.endswith(b"\x1b")
                    and not re.search(rb"\x1b\[[0-?]*[ -/]*$", pending)
                ):
                    sys.stderr.buffer.write(pending)
                    sys.stderr.buffer.flush()
                    pending = b""
            code = child.wait(timeout=timeout)
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
    return subprocess.CompletedProcess(
        command, code, "", trace.decode("utf-8", "surrogateescape")
    )


def main() -> None:
    operation, *rest = sys.argv[1:]
    if operation == "raw":
        print(raw_gh())
        return
    if operation == "foreground":
        terminal = (
            sys.stdin.isatty()
            and sys.stdout.isatty()
            and _watch_position(tuple(rest)) is None
        )
        while True:
            try:
                done = call(
                    "sessions",
                    *rest,
                    timeout=None,
                    background=False,
                    execute=_terminal_execute if terminal else None,
                )
                sys.stdout.buffer.write(done.stdout.encode("utf-8", "surrogateescape"))
                if not terminal:
                    sys.stderr.write(done.stderr)
                sys.exit(done.returncode)
            except Limited:
                wait("sessions", background=False)
    identity = None
    if rest[:1] == ["--identity"]:
        identity, rest = rest[1], rest[2:]
    program, *arguments = rest
    if operation == "report":
        report(float(program))
    elif operation == "jitter":
        print(poll_delay(float(program)))
    elif operation == "wait":
        wait(program, identity=identity)
    elif operation == "ready":
        sys.exit(75 if delay(program, identity=identity) else 0)
    elif operation == "call":
        try:
            done = call(program, *arguments, identity=identity)
        except Limited:
            sys.exit(75)
        sys.stdout.buffer.write(done.stdout.encode("utf-8", "surrogateescape"))
        sys.stderr.write(done.stderr)
        sys.exit(done.returncode)
    else:
        raise ValueError(operation)


if __name__ == "__main__":
    main()
