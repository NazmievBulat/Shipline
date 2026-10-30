"""Bounded commands, GitHub reads and retrying pushes."""

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import train_reads

# One pause per process for runs not yet listed by GitHub.
_paused = False


class Refused(Exception):
    """Missing evidence is a refusal, never permission to merge."""


class TimedOut(Refused):
    """A command outlasted its deadline and was stopped."""

    def __init__(self, message: str, output: str, timeout: float) -> None:
        super().__init__(message)
        # What it printed before it was stopped, and the deadline it outlasted.
        self.output = output
        self.timeout = timeout


class Waiting(Refused):
    """An expected wait which the tick may retry without a reviewer."""


class BudgetWaiting(Waiting):
    """The shared budget prevented a command; no request was sent."""


class PushWaiting(Waiting):
    """Nothing was pushed, and nothing was found wrong with the branch. A caller undoes a
    commit it made for the push."""


class ServiceWaiting(PushWaiting):
    """The pre-push scan could not reach a package advisory service."""


class PackagesWaiting(PushWaiting):
    """The pushing checkout's packages could not be brought to its lockfiles before its hook."""


def command(
    *args: str, check: bool = True, errors: str = "strict", input: str | None = None
) -> subprocess.CompletedProcess[str]:
    # A nested gate waits for the merge lock, then runs its own bounded commands, so its
    # whole run has no deadline of its own: one would kill a healthy push behind the lock.
    from merge_gate import (
        BudgetWaiting,
        COMMAND_TIMEOUT,
        CommandTimeout,
        HERE,
        Refused,
        TimedOut,
        run_command,
    )

    nested = str(HERE / "merge-when-current") in args
    try:
        done = run_command(
            *args,
            timeout=None if nested else COMMAND_TIMEOUT,
            unwind_on_interrupt=nested,
            errors=errors,
            input=input,
        )
    except CommandTimeout as error:
        raise TimedOut(str(error), error.output, COMMAND_TIMEOUT) from error
    if args[0] == "gh" and done.returncode == 75 and os.environ.get("WORKFLOW_BACKGROUND_GITHUB") == "1":
        raise BudgetWaiting(done.stderr.strip())
    if check and done.returncode:
        raise Refused(f"{' '.join(args)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done


def repository_command(
    name: str, *args: str, input: str | None = None, errors: str = "strict"
) -> subprocess.CompletedProcess[str] | None:
    """The repository's own command `name` (`shipline.json`), run with `args` in the current
    folder and bounded like every command; None when the repository names none. It reads
    `input`, and never the gate's own standard input. A command that is named but cannot start
    stops the gate's call."""
    from merge_gate import CONFIG, PackagesWaiting, Refused, Waiting, command
    from workflow_config import pass_fail_command

    words = CONFIG.command(name)
    if words is None:
        return None
    try:
        done = command(*words, *args, check=False, input=input or "", errors=errors)
    except OSError as error:
        raise Refused(
            f"cannot run the repository's {name} command, {shlex.join(words)}: {error}"
        ) from error
    if name != "pre_train_checks" and done.returncode == 75 and pass_fail_command(name, args):
        waiting = PackagesWaiting if name == "push_checkout" else Waiting
        raise waiting(f"{name} waits: {said(done)}")
    return done


def said(done: subprocess.CompletedProcess[str]) -> str:
    """Why a repository's command failed, in its own words."""
    return done.stderr.strip() or done.stdout.strip() or f"it exited {done.returncode}"


def nonblank_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def read_github(*args: str) -> subprocess.CompletedProcess[str]:
    from merge_gate import (
        CommandTimeout,
        READ_RETRY_DELAYS,
        READ_TIMEOUT,
        Waiting,
        github_read,
    )

    try:
        return github_read(*args, timeout=READ_TIMEOUT, delays=READ_RETRY_DELAYS)
    except CommandTimeout as error:
        raise Waiting(str(error)) from error


def git(*args: str) -> str:
    from merge_gate import (
        CommandTimeout,
        GIT_READ_TIMEOUT,
        READ_RETRY_DELAYS,
        Refused,
        Waiting,
        _planning_reads,
        command,
        read_with_retries,
        transient,
    )

    if _planning_reads is not None and args == (
        "ls-remote",
        "origin",
        "refs/heads/main",
    ):
        return _planning_reads.base + "\trefs/heads/main"
    if args[0] not in ("fetch", "ls-remote"):
        return command("git", *args).stdout.strip()
    # These read GitHub too: a server error or a stall is retried like a gh read.
    try:
        done = read_with_retries(
            "git", *args, timeout=GIT_READ_TIMEOUT, delays=READ_RETRY_DELAYS
        )
    except CommandTimeout as error:
        raise Waiting(str(error)) from error
    if done.returncode:
        error = done.stderr.strip() or done.stdout.strip()
        raise (Waiting if transient(error) else Refused)(f"git {' '.join(args)} failed: {error}")
    return done.stdout.strip()


# The pre-push security scan's words when the machine's shared locked memory runs short: its
# own retry note and Semgrep's error, which the scan itself knows by io_uring_queue_init.
# Nothing was found; the posix backend locks none.
MEMORY_SHORTAGE = ("io_uring_queue_init", "could not lock memory")
# Another session's scan that holds the locked memory has usually ended a minute later.
PUSH_RETRY_SECONDS = 60


def short_of_memory(output: str) -> bool:
    from merge_gate import MEMORY_SHORTAGE

    return any(words in output for words in MEMORY_SHORTAGE)


@dataclass
class Pushed:
    returncode: int
    # Standard output and error in the order written: git passes the hook's own output on
    # standard output, and its steps' failures are there.
    output: str
    # The hook's step that failed and its last lines, when the push failed.
    refusal: str
    # Whether the push outlasted its deadline; `output` is what it printed before it was stopped.
    timed_out: bool = False


def push(*args: str, where: Path | None = None) -> Pushed:
    """`git push` with its hook on the posix backend, once more a minute after a memory shortage.

    The hook of a branch made before `.husky/pre-push` set the backend itself runs too, so the
    gate sets it for every push it makes. The hook runs with the pushing checkout's packages,
    installed first when its lockfiles changed since their last install. A failed install, or
    a scan that could not reach an advisory service after its own tries, is a wait."""
    from merge_gate import (
        CONFIG,
        PUSH_RETRY_SECONDS,
        PackagesWaiting,
        Pushed,
        ServiceWaiting,
        _push_once,
        command,
        git,
        main_checkout,
        note_service_down,
        note_service_up,
        push_failure,
        repository_command,
        said,
        short_of_memory,
        unreachable_service,
    )

    checkout = where or Path.cwd()
    if CONFIG.command("push_checkout") is not None:
        checkout = Path(git("-C", str(checkout), "rev-parse", "--show-toplevel"))
        installed = repository_command("push_checkout", "install", str(checkout))
        if installed and installed.returncode:
            raise PackagesWaiting(said(installed))
    git_args = ("git", *(("-C", str(where)) if where else ()), "push", *args)
    merged = ("env", "EIO_BACKEND=posix", "sh", "-c", 'exec "$@" 2>&1', "push", *git_args)
    done, timeout = _push_once(merged)
    if done.returncode and timeout is None and short_of_memory(done.stdout):
        command("sleep", str(PUSH_RETRY_SECONDS))
        done, timeout = _push_once(merged)
    output = done.stdout
    pushed = Pushed(
        done.returncode,
        output,
        push_failure(output, checkout, timeout) if done.returncode else "",
        timeout is not None,
    )
    if CONFIG.command("advisory_watch") is None:
        return pushed
    folder = main_checkout() / CONFIG.state_folder
    # A scan stopped at the deadline never concluded that a service was out.
    service = (
        unreachable_service(output) if pushed.returncode and not pushed.timed_out else None
    )
    if service:
        note_service_down(folder, service)
        raise ServiceWaiting(
            f"the pre-push scan could not reach {service}'s advisory service; "
            "nothing was found wrong with the branch"
        )
    if not pushed.returncode:
        note_service_up(folder)
    return pushed


def _push_once(merged: tuple[str, ...]) -> tuple[subprocess.CompletedProcess[str], float | None]:
    """The push, and the deadline it outlasted, if it did: then it failed with what it printed."""
    from merge_gate import TimedOut, command

    try:
        return command(*merged, check=False), None
    except TimedOut as error:
        # 124, as `timeout` reports a command it stopped.
        return subprocess.CompletedProcess(merged, 124, error.output, ""), error.timeout


def push_failure(output: str, checkout: Path, timeout: float | None = None) -> str:
    """What a failed push says, led by the deadline it outlasted, or by the scan's memory error
    when it ran short.

    The first line is all the wake and a hand-back show, so a timeout or a shortage is named
    there, before the hook's step that failed, or was running, and its last lines."""
    from merge_gate import (
        REFUSAL_LINES,
        TIMEOUT_REFUSAL_LINES,
        nonblank_lines,
        repository_command,
        short_of_memory,
    )

    worded = repository_command("push_checkout", "refusal", str(checkout), input=output)
    if worded and not worded.returncode and worded.stdout.strip():
        refusal = worded.stdout.strip()
    else:
        lines = REFUSAL_LINES if timeout is None else TIMEOUT_REFUSAL_LINES
        refusal = "\n".join(nonblank_lines(output)[-lines:])
    if timeout is not None:
        return "\n".join(filter(None, [f"git push timed out after {timeout:g}s", refusal]))
    shortage = next(
        (line.strip() for line in output.splitlines() if short_of_memory(line)),
        None,
    )
    if shortage is None:
        return refusal
    return f"the security scan was short of locked memory twice, a minute apart: {shortage}\n{refusal}"


def api(path: str, key: str | None = None, *, paginate: bool = True, fresh: bool = False) -> Any:
    """GitHub's answer for `path`; `fresh` asks again what a reconciliation already read."""
    from merge_gate import _api, _planning_reads

    if _planning_reads is not None:
        return _planning_reads.api(path, lambda: _api(path, key, paginate=paginate), fresh=fresh)
    return _api(path, key, paginate=paginate)


def _api(path: str, key: str | None = None, *, paginate: bool = True) -> Any:
    from merge_gate import Refused, Waiting, read_github

    args = ("gh", "api", *(["--paginate"] if paginate else []), path)
    done = read_github(*args[1:])
    if done.returncode:
        error = done.stderr.strip() or done.stdout.strip()
        # A dropped connection, a GitHub server error or a rate limit says nothing about
        # the pull request: the tick retries instead of revoking a handoff.
        unreadable = re.search(
            r'HTTP (?:5\d\d|429)\b|rate limit|timed? out|timeout|context deadline exceeded|^(?:get|post|put|patch|delete) "https?://',
            error,
            re.IGNORECASE | re.MULTILINE,
        )
        raise (Waiting if unreadable else Refused)(f"{' '.join(args)} failed: {error}")
    # gh emits one JSON document per page. Do not silently lose jobs or annotations after 100.
    raw = done.stdout
    decoder = json.JSONDecoder()
    pages: list[Any] = []
    while raw.strip():
        page, end = decoder.raw_decode(raw.lstrip())
        pages.append(page)
        raw = raw.lstrip()[end:]
    if key is not None:
        return [item for page in pages for item in page[key]]
    if pages and isinstance(pages[0], list):
        return [item for page in pages for item in page]
    if len(pages) != 1:
        raise Refused(f"invalid GitHub response for {path}")
    return pages[0]


def workflow_runs(workflow: str, path: str, *, paginate: bool = True) -> list[dict[str, Any]]:
    """The runs of one workflow that GitHub lists at `path`. A repository may hold no image or
    browser workflow, as one set up with only its CI holds neither; GitHub answers HTTP 404 for
    it, and it has no runs. Whether their checks are needed is read from the repository itself
    (`images_needed`, `browser_needed`). A repository without its CI workflow stays an error."""
    from merge_gate import Refused, api

    try:
        return api(path, "workflow_runs", paginate=paginate)
    except Refused as error:
        if workflow in ("e2e", "images") and re.search(r"\bHTTP 404\b", str(error)):
            return []
        raise


def runs(
    workflow: str, head: str, *, pull: bool = False, scheduled: bool = False
) -> list[dict[str, Any]]:
    # Main is judged by the runs of its pushes (and manual reruns). A scheduled run of an
    # unchanged head is kept apart: it may report a failure, never hold or close the gate.
    from merge_gate import (
        API,
        Refused,
        WORKFLOW_FILES,
        Waiting,
        _own_runs,
        _unlisted_runs,
        workflow_runs,
    )

    event = "&event=pull_request" if pull else ""
    path = (
        f"{API}/actions/workflows/{WORKFLOW_FILES[workflow]}/runs"
        f"?head_sha={head}{event}&per_page=100"
    )
    listed = workflow_runs(workflow, path)
    found = _own_runs(listed, head, pull=pull, scheduled=scheduled)
    if listed or workflow != "ci":
        return found
    try:
        return _unlisted_runs(path, head, pull=pull, scheduled=scheduled)
    except Waiting:
        raise
    except Refused, KeyError, TypeError, ValueError:
        # Unread is not absent: the head has no CI result yet, and the line keeps its judgment.
        return []


def _unlisted_runs(path: str, head: str, *, pull: bool, scheduled: bool) -> list[dict[str, Any]]:
    """The CI runs of `head` that GitHub did not list, when its check runs show CI jobs finished.

    GitHub sometimes lists no CI run for such a head and lists it when asked again a little
    later. The run is found by the check suite of its jobs; when that lists none either, GitHub
    is asked again, after a pause the first time in a process. A run still not found is no CI
    result yet, a wait: the line keeps what it judged of the head before."""
    from merge_gate import (
        API,
        UNLISTED_RETRY_SECONDS,
        WORKFLOW_FILES,
        _ci_suites,
        _own_runs,
        api,
        command,
    )

    global _paused
    suites = _ci_suites(head)
    if not suites:
        return []
    ci_path = f".github/workflows/{WORKFLOW_FILES['ci']}"
    found = _own_runs(
        [
            run
            for suite in suites
            for run in api(
                f"{API}/actions/runs?check_suite_id={suite}&per_page=100", "workflow_runs"
            )
            if run.get("path", "").split("@")[0] == ci_path
        ],
        head,
        pull=pull,
        scheduled=scheduled,
    )
    if found:
        return found
    if not _paused:
        _paused = True
        command("sleep", str(UNLISTED_RETRY_SECONDS))
    return _own_runs(api(path, "workflow_runs", fresh=True), head, pull=pull, scheduled=scheduled)


def _own_runs(
    found: list[dict[str, Any]], head: str, *, pull: bool, scheduled: bool
) -> list[dict[str, Any]]:
    """The runs of `head` that judge it, newest first: its pull request's, or main's own."""
    return sorted(
        [
            run
            for run in found
            if run["head_sha"] == head
            and (
                run["event"] == "pull_request"
                if pull
                else run.get("head_branch") == "main"
                and (
                    run["event"] == "schedule"
                    if scheduled
                    else run["event"] not in ("pull_request", "schedule")
                )
            )
        ],
        key=lambda run: run["id"],
        reverse=True,
    )


def read_run(run: str) -> dict[str, Any]:
    from merge_gate import API, api

    return api(f"{API}/actions/runs/{run}", paginate=False)


def read_pull(number: str) -> dict[str, Any]:
    from merge_gate import _planning_reads, _read_pull

    if _planning_reads is not None:
        return train_reads.pull(number, _read_pull)
    return _read_pull(number)


def _read_pull(number: str) -> dict[str, Any]:
    from merge_gate import REPOSITORY, Waiting, read_github

    done = read_github(
        "pr",
        "view",
        number,
        "-R",
        REPOSITORY,
        "--json",
        "state,isDraft,baseRefName,headRefName,headRefOid,mergeable,mergeStateStatus,statusCheckRollup,labels",
    )
    if done.returncode:
        # Nothing about the pull request is known, so nothing needs judgment yet: the
        # tick retries a handed-off pull request instead of revoking its handoff.
        error = done.stderr.strip() or done.stdout.strip()
        raise Waiting(f"cannot read pull request {number}: {error}")
    return json.loads(done.stdout)
