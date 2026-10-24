"""Bound workflow subprocesses and retry only explicitly identified GitHub reads."""

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import NoReturn

from push_slots import SLOT_FD_ENV, admission

COMMAND_TIMEOUT = 600
READ_TIMEOUT = 30
GIT_READ_TIMEOUT = 120
READ_RETRY_DELAYS = (1, 2, 4)


class CommandTimeout(Exception):
    """The command and its process group have been stopped."""


def _interrupted(signum: int, frame: FrameType | None) -> NoReturn:
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    # Exit by unwinding the active command, so a stopped session cannot leave a push
    # running in its separate process group after releasing the gate lock.
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(signum, _interrupted)


def _processes() -> dict[int, tuple[str, int, int, int]]:
    """Each live process: its state, parent, session and start time, from `/proc`."""
    found: dict[int, tuple[str, int, int, int]] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/stat", encoding="utf-8", errors="replace") as stat:
                # The name can hold spaces and brackets; the fields after it cannot.
                fields = stat.read().rpartition(")")[2].split()
            found[int(entry.name)] = (
                fields[0],
                int(fields[1]),
                int(fields[3]),
                int(fields[19]),
            )
        except OSError, IndexError, ValueError:
            continue
    return found


def _run_processes(leader: int, known: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """The live processes of a command started as session leader `leader`, as (pid, start).

    `test-run`'s `timeout` moves the tests into a process group of their own, which a kill of
    the command's group misses, but they stay in its session. A child that started a session of
    its own (Playwright's browsers) is found as a descendant of the command, or of a process
    already `known` to be part of the run, since its parent may be gone by the time it is killed.
    """
    try:
        processes = _processes()
    except OSError:
        return set()
    children: dict[int, list[int]] = {}
    for pid, (_state, parent, _session, _start) in processes.items():
        children.setdefault(parent, []).append(pid)
    # A known pid counts only while it has the same start time: it is not a reused number. The
    # command itself is its session's leader, found with the session.
    pending = [pid for pid, start in known if pid in processes and processes[pid][3] == start]
    pending += [
        pid for pid, (_state, _parent, session, _start) in processes.items() if session == leader
    ]
    members: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid not in members and pid in processes:
            members.add(pid)
            pending += children.get(pid, [])
    members.discard(os.getpid())
    return {(pid, processes[pid][3]) for pid in members if processes[pid][0] != "Z"}


def _kill_run(leader: int, known: set[tuple[int, int]]) -> None:
    # SIGKILL ends a process at once, but one may fork before it arrives: look again until a
    # look finds nothing left.
    for _ in range(100):
        found = _run_processes(leader, known)
        if not found:
            return
        known |= found
        for pid, _start in found:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.01)


@contextmanager
def _defer_interrupts() -> Iterator[None]:
    # Popen may have spawned the child without returning its handle yet. Deliver
    # Python's exit/interrupt handlers only after the caller can clean that child up.
    handlers: dict[int, Callable[[int, FrameType | None], object]] = {}
    pending: list[tuple[int, FrameType | None]] = []

    def defer(signum: int, frame: FrameType | None) -> None:
        pending.append((signum, frame))

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            handler = signal.getsignal(signum)
            if callable(handler):
                handlers[signum] = handler
                signal.signal(signum, defer)
        yield
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if pending:
            signum, frame = pending[0]
            handlers[signum](signum, frame)


def _stop(
    process: subprocess.Popen[str],
    *,
    unwind: bool = False,
    cleanup_timeout: float | None = None,
) -> None:
    # Taken before any signal: a stopped parent leaves its children to init, out of its tree.
    run = _run_processes(process.pid, set())
    if unwind:
        # A timed-out command can itself be a gate. Let its SIGTERM handler stop
        # its separately grouped active command before we escalate. A trusted nested
        # gate must finish that cleanup even when scheduling takes longer than two
        # seconds. Ordinary timed-out commands still get a bounded grace period.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=cleanup_timeout)
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # What is left of the run would hold the output pipes, and with them the caller's merge
    # lock and push slot.
    _kill_run(process.pid, run)
    process.communicate()


def _run(
    *args: str,
    timeout: float | None = COMMAND_TIMEOUT,
    unwind_on_interrupt: bool = False,
    errors: str = "strict",
    slot_fd: int | None = None,
    environment: dict[str, str] | None = None,
    github_slot_fd: int | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    # Hooks inherit the new group. Killing just git/gh leaves them able to mutate the
    # repository after the caller releases its merge lock. Never inherit that lock fd.
    process: subprocess.Popen[str] | None = None
    try:
        with _defer_interrupts():
            process = subprocess.Popen(
                args,
                # Without input the command reads what the caller's standard input holds.
                stdin=None if input is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors=errors,
                start_new_session=True,
                close_fds=True,
                pass_fds=tuple(fd for fd in (slot_fd, github_slot_fd) if fd is not None),
                env=environment
                if slot_fd is None
                else {**(environment or os.environ), SLOT_FD_ENV: str(slot_fd)},
            )
        stdout, stderr = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the entire group even when the leader has already exited and only a
        # child holds the output pipe. SIGKILL also stops hooks that ignore SIGTERM.
        assert process is not None
        _stop(process, unwind=True, cleanup_timeout=2)
        raise CommandTimeout(f"{' '.join(args)} timed out after {timeout:g}s") from None
    except BaseException:
        if process is not None:
            _stop(process, unwind=unwind_on_interrupt)
        raise
    finally:
        if process is not None:
            # Match Popen's context manager after cleanup, never before it: __exit__
            # waits for the child, which might be stopped or ignoring TERM.
            process.__exit__(None, None, None)
    assert process is not None
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def run(
    *args: str,
    timeout: float | None = COMMAND_TIMEOUT,
    unwind_on_interrupt: bool = False,
    errors: str = "strict",
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if args and args[0] == "gh":
        from github_budget import Limited, call, wait

        background = os.environ.get("WORKFLOW_BACKGROUND_GITHUB") == "1"
        while True:
            try:
                return call(
                    "tick-gate" if background else "sessions",
                    *args[1:],
                    timeout=timeout or COMMAND_TIMEOUT,
                    background=background,
                    execute=lambda command, environment, seconds, fd: _run(
                        *command,
                        timeout=seconds,
                        unwind_on_interrupt=unwind_on_interrupt,
                        errors=errors,
                        environment=environment,
                        github_slot_fd=fd,
                        input=input,
                    ),
                )
            except Limited:
                if background:
                    return subprocess.CompletedProcess(
                        args, 75, "", "GitHub rate limit budget low; waiting for reset"
                    )
                wait("sessions", background=False)
    # Admission precedes the command timer. The hook inherits this same slot, so it
    # cannot queue twice. Network work and all checks still share the normal deadline.
    is_push = "git" in args and "push" in args[args.index("git") + 1 :]
    if is_push:
        with admission() as fd:
            return _run(
                *args,
                timeout=timeout,
                unwind_on_interrupt=unwind_on_interrupt,
                errors=errors,
                slot_fd=fd,
                input=input,
            )
    return _run(
        *args,
        timeout=timeout,
        unwind_on_interrupt=unwind_on_interrupt,
        errors=errors,
        input=input,
    )


def transient(error: str) -> bool:
    # gh says "HTTP 502"; git over HTTPS says "The requested URL returned error: 502".
    # gh can also report its own HTTP timeout before our process deadline.
    return not re.search(r"(?:HTTP|returned error:) 4\d\d\b", error) and bool(
        re.search(
            r"(?:HTTP|returned error:) 5\d\d\b|timed? out|timeout|context deadline exceeded",
            error,
            re.IGNORECASE,
        )
    )


def read_with_retries(
    *args: str,
    timeout: float = READ_TIMEOUT,
    delays: tuple[float, ...] = READ_RETRY_DELAYS,
) -> subprocess.CompletedProcess[str]:
    for attempt in range(len(delays) + 1):
        try:
            done = run(*args, timeout=timeout)
        except CommandTimeout:
            if attempt == len(delays):
                raise
        else:
            if not done.returncode or not transient(done.stderr or done.stdout):
                return done
            if attempt == len(delays):
                return done
        time.sleep(delays[attempt])
    raise AssertionError("unreachable retry state")


def github_read(
    *args: str,
    timeout: float = READ_TIMEOUT,
    delays: tuple[float, ...] = READ_RETRY_DELAYS,
) -> subprocess.CompletedProcess[str]:
    return read_with_retries("gh", *args, timeout=timeout, delays=delays)


if __name__ == "__main__":
    install_signal_handlers()
    try:
        result = github_read(*sys.argv[1:])
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    except (CommandTimeout, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
