"""Bound workflow subprocesses and retry only explicitly identified GitHub reads."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import NoReturn

from push_slots import SLOT_FD_ENV, admission

COMMAND_TIMEOUT = 600
READ_TIMEOUT = 30
GIT_READ_TIMEOUT = 120
READ_RETRY_DELAYS = (1, 2, 4)


class CommandTimeout(Exception):
    """The command and its process group have been stopped."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        # What the command printed before it was stopped: standard output, then standard error.
        self.output = output


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
) -> tuple[str, str]:
    """Stops the command and its run; returns all it printed, standard output and error."""
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
    # Popen keeps what an earlier `communicate` read, so this is the whole output.
    return process.communicate()


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
        stdout, stderr = _stop(process, unwind=True, cleanup_timeout=2)
        raise CommandTimeout(
            f"{' '.join(args)} timed out after {timeout:g}s", (stdout or "") + (stderr or "")
        ) from None
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


def run_shown(*args: str, timeout: float | None, grace: float) -> int | None:
    """Runs a command whose output is this one's, and returns its exit code; None when it ran
    past `timeout`. It then gets a TERM, and `grace` seconds later a KILL, as does every process
    it started. An interrupt kills them at once, and nothing it started outlives it."""
    process: subprocess.Popen[str] | None = None
    try:
        with _defer_interrupts():
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        assert process is not None
        _stop(process, unwind=True, cleanup_timeout=grace)
        return None
    except BaseException:
        if process is not None:
            _stop(process)
        raise
    # A server it left in the background would hold its ports for the next run.
    _kill_run(process.pid, set())
    return code


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


def _function_answer(name: str, entry: dict, words: list[str]) -> int:
    path = Path(words[0])
    arguments = words[1:]
    # A separate interpreter has no application modules cached. Its own siblings must win
    # over Shipline's modules, even when both provide the same module name.
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(
        path.stem, path, loader=importlib.machinery.SourceFileLoader(path.stem, str(path))
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load Python module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    subcommand = "browser_plan" if name == "browser_plan" else arguments.pop(0)
    reads_input = (
        name == "browser_plan"
        or (name == "advisory_watch" and subcommand == "unreachable")
        or (name == "push_checkout" and subcommand == "refusal")
    )
    text = sys.stdin.buffer.read().decode("utf-8", "surrogateescape") if reads_input else ""
    if name == "browser_plan":
        if arguments:
            raise ValueError("browser_plan takes changed paths on standard input")
        call_args = [[
            path.encode("utf-8", "surrogateescape") for path in text.split("\0") if path
        ]]
    elif name == "advisory_watch" and subcommand == "unreachable":
        if arguments:
            raise ValueError("unreachable takes text on standard input")
        call_args = [text]
    elif name == "push_checkout":
        if len(arguments) != 1:
            raise ValueError(f"{subcommand} takes one checkout path")
        checkout = Path(arguments[0])
        call_args = [checkout] if subcommand == "install" else [text, checkout]
    else:
        call_args = arguments
    # Diagnostics printed by a module are not part of its returned answer.
    answer_output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(module)
        try:
            result = getattr(module, entry["functions"][subcommand])(*call_args)
        except Exception as error:
            # A renumber that cannot move some files raises with them in `paths`: they are
            # named on standard output and left as a conflict, as the documented form does.
            paths = getattr(error, "paths", None)
            if (
                name != "mechanical_conflicts"
                or subcommand != "renumber"
                or isinstance(paths, (str, bytes))
                or not isinstance(paths, (list, tuple, set, frozenset))
                or not paths
                or not all(isinstance(path, str) and path.strip() for path in paths)
            ):
                raise
            for path in paths:
                print(path, file=answer_output)
            print(error, file=sys.stderr)
            return 1
    if name == "browser_plan":
        if type(result) is not bool:
            raise ValueError("browser_plan must return a bool")
        print("needed" if result else "none")
    elif result is None or result is False:
        return 1 if name == "advisory_watch" and subcommand == "answers" else 0
    elif result is True:
        return 0
    elif isinstance(result, str):
        print(result)
        if name == "push_checkout" and subcommand == "install":
            # The gate reads a failure's standard error first, where diagnostics went.
            print(result, file=sys.stderr)
            return 1
    elif isinstance(result, Mapping):
        for old, new in result.items():
            print(f"{old} {new}")
    elif isinstance(result, list):
        for item in result:
            print(item)
    else:
        raise ValueError("function must return a list, mapping, string, bool or None")
    return 0


def _json_answer(name: str, entry: dict, words: list[str]) -> int:
    done = _run(*words, errors="surrogateescape", timeout=COMMAND_TIMEOUT)
    sys.stderr.write(done.stderr)
    if done.returncode:
        sys.stdout.write(done.stdout)
        return done.returncode
    answer = json.loads(done.stdout)
    if not isinstance(answer, dict):
        raise ValueError("selector must print a JSON object")
    if name == "browser_tests_for_changes":
        specs = answer[entry["field"]]
        if not isinstance(specs, list) or not all(isinstance(spec, str) for spec in specs):
            raise ValueError("specs field must be a list of paths")
        for spec in specs:
            print(spec)
    else:
        mapped = {key: answer[field] for key, field in entry["fields"].items()}
        if (
            type(mapped["whole"]) is not bool
            or type(mapped["total"]) is not int
            or mapped["total"] < 0
            or not all(
                isinstance(mapped[key], list)
                and all(isinstance(item, str) for item in mapped[key])
                for key in ("tests", "reasons")
            )
        ):
            raise ValueError(
                "selector fields must be whole: bool, tests/reasons: lists, total: nonnegative int"
            )
        print(json.dumps(mapped))
    return 0


def repository_answer(args: list[str]) -> int:
    name = args[0] if args else "unknown"
    entry = {}
    try:
        entry = json.loads(args[1])
        words = args[2:]

        def answer() -> int:
            if entry["form"] == "functions":
                return _function_answer(name, entry, words)
            if entry["form"] == "json":
                return _json_answer(name, entry, words)
            # The caller keeps its existing deadline: the gate bounds this whole adapter,
            # while a launcher or CI command must not gain a shorter command deadline.
            done = _run(*words, errors="surrogateescape", timeout=None)
            sys.stdout.write(done.stdout)
            sys.stderr.write(done.stderr)
            return done.returncode

        if not {"wait_exit", "wait_output"} & entry.keys():
            return answer()
        from workflow_config import command_waits, pass_fail_command

        # A multi-call command's diagnostics and selectors keep their documented answers.
        call = words
        if name in ("mechanical_conflicts", "advisory_watch", "push_checkout"):
            # For command lines, interpreter and script words precede the call. The config
            # passes their count separately, so literal arguments cannot select a call.
            call = words[entry["_words"]:]
        if not pass_fail_command(name, call):
            return answer()
        if entry["form"] == "functions":
            # A function can write through file descriptors or let subprocesses inherit
            # them. Capture its interpreter, keeping real streams (and fileno) available.
            # Only this parent classifies waits; the worker keeps the original answers.
            worker = {key: value for key, value in entry.items() if key not in (
                "wait_exit", "wait_output",
            )}
            done = _run(
                sys.executable, __file__, "--repository-command", name, json.dumps(worker),
                *words, errors="surrogateescape", timeout=None,
            )
            code, stdout, stderr = done.returncode, done.stdout, done.stderr
        else:
            captured_out, captured_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
                try:
                    code = answer()
                except Exception as error:
                    print(f"commands.{name}: {error}", file=sys.stderr)
                    code = 1
            stdout, stderr = captured_out.getvalue(), captured_err.getvalue()
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        if any(command_waits(entry, code, output) for output in (stdout, stderr)):
            return 75
        # With 75 removed from wait_exit, an actual 75 is an ordinary failure.
        return 1 if code == 75 else code
    except Exception as error:
        # Module errors are a failed answer, not a traceback or an implicit fallback.
        prefix = "" if entry.get("form") == "functions" else f"commands.{name}: "
        print(f"{prefix}{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    install_signal_handlers()
    if sys.argv[1:2] == ["--repository-command"]:
        sys.exit(repository_answer(sys.argv[2:]))
    try:
        result = github_read(*sys.argv[1:])
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    except (CommandTimeout, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
