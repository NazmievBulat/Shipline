"""One machine-wide merge gate. Only the standard library and git/gh are needed."""

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import priority
import train_reads
import workflow_config
from merge_handoff import Handoff, authorized
from workflow_commands import (
    COMMAND_TIMEOUT,
    GIT_READ_TIMEOUT,
    READ_RETRY_DELAYS,
    READ_TIMEOUT,
    CommandTimeout,
    github_read,
    install_signal_handlers,
    read_with_retries,
    transient,
)
from workflow_commands import (
    run as run_command,
)

# A repository may hold three helpers beside these scripts, and the gate uses them when it does:
# `advisory_service` tells a scan that could not reach a package advisory service, which is a
# wait; `pushing_checkout` installs the pushing checkout's packages before its hook and words a
# hook's refusal; `run_proof` reads and writes the `Workflow-Proof` line with which main skips a
# job its squash proved. Without them a failed push is refused with its last lines, nothing is
# installed first, and the proof is Shipline's own (`ci_proof`), the one its example CI reads. A
# helper that is there but cannot start stops the gate.
try:
    import advisory_service
except ModuleNotFoundError as error:
    if error.name != "advisory_service":
        raise
    advisory_service = None
try:
    import pushing_checkout
except ModuleNotFoundError as error:
    if error.name != "pushing_checkout":
        raise
    pushing_checkout = None
try:
    from run_proof import RECEIPT, passed_jobs
    from run_proof import WHOLE as WHOLE_REQUEST
except ModuleNotFoundError as error:
    if error.name != "run_proof":
        raise
    from ci_proof import RECEIPT, passed_jobs
    from ci_proof import WHOLE as WHOLE_REQUEST


def mechanical_conflicts() -> ModuleType | None:
    """The repository's rules for the conflicts and migration numbers a refresh settles itself,
    `mechanical_conflicts` beside the scripts, read only when a refresh needs them. Without
    them no conflict is mechanical and there are no migrations to renumber."""
    try:
        import mechanical_conflicts as rules
    except ModuleNotFoundError as error:
        if error.name != "mechanical_conflicts":
            raise
        return None
    return rules


# How many of its last lines a failed push is refused with, when the repository words no refusal.
REFUSAL_LINES = 10

CONFIG = workflow_config.current()
REPOSITORY = CONFIG.repository
API = CONFIG.api
HERE = Path(__file__).resolve().parent
# The gate owns these branches: each holds one batch's test merge, never a pull request.
SCRATCH_PREFIX = "workflow/merge-batch-"
E2E_PLAN = CONFIG.checks.e2e_plan
CI_PLAN = CONFIG.checks.ci_plan
CI_RESULT = CONFIG.checks.ci_result
# Every check of the image workflow begins with this; its planner is IMAGES_PLAN.
IMAGES = CONFIG.checks.images
IMAGES_PLAN = CONFIG.checks.images_plan
# The titles of the planners' annotations.
NOTES = CONFIG.annotations
# The workflow files, by the gate's own names for them.
WORKFLOW_FILES = {
    "ci": CONFIG.workflows.ci,
    "e2e": CONFIG.workflows.e2e,
    "images": CONFIG.workflows.images,
}
# What a dispatched whole-suite run is called in the gate's words, by its workflow.
WHOLE = {"e2e": "the whole browser suite", "ci": "every server test"}
SQUASH_ATTEMPTS = 3
SQUASH_RETRY_SECONDS = 10
# A scratch push that fails waits for the next call; a hook that keeps refusing for a reason
# other than the memory shortage, or a push that keeps stalling, is a refusal.
BATCH_PUSH_ATTEMPTS = 3
# How many members of the merge train are tested at once, at most, each as a cumulative prefix:
# main and every member ahead of it. The one place to change when the runners can take more.
WINDOW_CEILING = 4
# A check GitHub still lists as queued or running this long after its run completed was lost:
# GitHub missed its end, and no wait brings it.
LOST_AFTER_SECONDS = 10 * 60
# How long the gate remembers that it asked GitHub to cancel a run.
CANCEL_CLAIM_SECONDS = 24 * 60 * 60
# How long after a scratch branch was dropped the sweep still looks for the runs dispatched on
# it that GitHub has not listed yet: GitHub accepts a dispatch before it lists the run.
DROPPED_RUNS_SECONDS = 30 * 60
# How many times a handoff judges its own place again when the line changed it meanwhile.
HANDOFF_ATTEMPTS = 3
# How long a handoff's one check of its head, under the merge lock, may take.
HEAD_CHECK_TIMEOUT = 10
# A handoff's last read of its pull request still stands this long after it; a longer wait for
# the merge lock reads it again, at most LOCK_WAITS times.
FRESH_READ_SECONDS = 5
LOCK_WAITS = 10
# Each member's own entries in the saved line, by the Train attribute that holds them.
MEMBER_ENTRIES = (
    "blocked",
    "suspended",
    "conflicts",
    "approved",
    "proven",
    "batch_failed",
    "suites",
    "proofs",
    "priorities",
    "reruns",
    "lost",
)
MEMBER_MARKS = ("unavailable", "fixes", "refreshable")

# Set only while computing train order, never while verifying a merge.
_planning_reads: train_reads.Reads | None = None


class Refused(Exception):
    """Missing evidence is a refusal, never permission to merge."""


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
    *args: str, check: bool = True, errors: str = "strict"
) -> subprocess.CompletedProcess[str]:
    # A nested gate waits for the merge lock, then runs its own bounded commands, so its
    # whole run has no deadline of its own: one would kill a healthy push behind the lock.
    nested = str(HERE / "merge-when-current") in args
    try:
        done = run_command(
            *args,
            timeout=None if nested else COMMAND_TIMEOUT,
            unwind_on_interrupt=nested,
            errors=errors,
        )
    except CommandTimeout as error:
        raise Refused(str(error)) from error
    if done.returncode == 75 and os.environ.get("WORKFLOW_BACKGROUND_GITHUB") == "1":
        raise BudgetWaiting(done.stderr.strip())
    if check and done.returncode:
        raise Refused(f"{' '.join(args)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done


def read_github(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return github_read(*args, timeout=READ_TIMEOUT, delays=READ_RETRY_DELAYS)
    except CommandTimeout as error:
        raise Waiting(str(error)) from error


def git(*args: str) -> str:
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
    return any(words in output for words in MEMORY_SHORTAGE)


@dataclass
class Pushed:
    returncode: int
    # Standard output and error in the order written: git passes the hook's own output on
    # standard output, and its steps' failures are there.
    output: str
    # The hook's step that failed and its last lines, when the push failed.
    refusal: str


def push(*args: str, where: Path | None = None) -> Pushed:
    """`git push` with its hook on the posix backend, once more a minute after a memory shortage.

    The hook of a branch made before `.husky/pre-push` set the backend itself runs too, so the
    gate sets it for every push it makes. The hook runs with the pushing checkout's packages,
    installed first when its lockfiles changed since their last install. A failed install, or
    a scan that could not reach an advisory service after its own tries, is a wait."""
    checkout = pushing_checkout.top(where) if pushing_checkout else where or Path.cwd()
    failed = pushing_checkout.install_if_changed(checkout) if pushing_checkout else ""
    if failed:
        raise PackagesWaiting(failed)
    git_args = ("git", *(("-C", str(where)) if where else ()), "push", *args)
    merged = ("env", "EIO_BACKEND=posix", "sh", "-c", 'exec "$@" 2>&1', "push", *git_args)
    done = command(*merged, check=False)
    if done.returncode and short_of_memory(done.stdout):
        command("sleep", str(PUSH_RETRY_SECONDS))
        done = command(*merged, check=False)
    output = done.stdout
    pushed = Pushed(
        done.returncode,
        output,
        push_failure(output, checkout) if done.returncode else "",
    )
    if advisory_service is None:
        return pushed
    folder = main_checkout() / CONFIG.state_folder
    service = advisory_service.unreachable(output) if pushed.returncode else None
    if service:
        folder.mkdir(parents=True, exist_ok=True)
        advisory_service.note_down(folder, service)
        raise ServiceWaiting(
            f"the pre-push scan could not reach {service}'s advisory service; "
            "nothing was found wrong with the branch"
        )
    if not pushed.returncode:
        advisory_service.note_up(folder)
    return pushed


def push_failure(output: str, checkout: Path) -> str:
    """What a failed push says, led by the scan's memory error when it ran short.

    The first line is all the wake and a hand-back show, so a shortage is named there, before
    the hook's step that failed and its last lines."""
    if pushing_checkout:
        refusal = pushing_checkout.refusal(output, checkout)
    else:
        refusal = "\n".join([line for line in output.splitlines() if line.strip()][-REFUSAL_LINES:])
    shortage = next(
        (line.strip() for line in output.splitlines() if short_of_memory(line)),
        None,
    )
    if shortage is None:
        return refusal
    return f"the security scan was short of locked memory twice, a minute apart: {shortage}\n{refusal}"


def api(path: str, key: str | None = None, *, paginate: bool = True) -> Any:
    if _planning_reads is not None:
        return _planning_reads.api(path, lambda: _api(path, key, paginate=paginate))
    return _api(path, key, paginate=paginate)


def _api(path: str, key: str | None = None, *, paginate: bool = True) -> Any:
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


def runs(
    workflow: str, head: str, *, pull: bool = False, scheduled: bool = False
) -> list[dict[str, Any]]:
    # Main is judged by the runs of its pushes (and manual reruns). A scheduled run of an
    # unchanged head is kept apart: it may report a failure, never hold or close the gate.
    event = "&event=pull_request" if pull else ""
    found = api(
        f"{API}/actions/workflows/{WORKFLOW_FILES[workflow]}/runs"
        f"?head_sha={head}{event}&per_page=100",
        "workflow_runs",
    )
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


class MainImages:
    """Main's image runs, newest first, read a page at a time and only as far as needed."""

    def __init__(self) -> None:
        self.found: list[dict[str, Any]] = []
        self.page = 0
        self.last = False

    def __iter__(self) -> Iterator[dict[str, Any]]:
        index = 0
        while True:
            if index < len(self.found):
                yield self.found[index]
                index += 1
                continue
            if self.last:
                return
            # Nightly runs fill pages too: a missing page must never forget an older failure.
            self.page += 1
            runs_page = api(
                f"{API}/actions/workflows/{WORKFLOW_FILES['images']}/runs"
                f"?branch=main&per_page=100&page={self.page}",
                "workflow_runs",
                paginate=False,
            )
            self.last = len(runs_page) < 100
            self.found += [
                run
                for run in runs_page
                if run.get("head_branch") == "main"
                and run["event"] not in ("pull_request", "schedule")
            ]


def main_image_runs(head: str, recent: MainImages) -> list[dict[str, Any]]:
    # A push that changes nothing in the images gets no image run, and must not hide the failure
    # of an earlier one: main's image state is its latest completed image run, whichever push of
    # main it was for, plus any newer one still running. The head's own latest run always counts.
    own = runs("images", head)[:1]
    ancestors = {head: True}
    found: list[dict[str, Any]] = []
    for run in recent:
        commit = run["head_sha"]
        if commit not in ancestors:
            ancestors[commit] = not command(
                "git", "merge-base", "--is-ancestor", commit, head, check=False
            ).returncode
        if not ancestors[commit]:
            continue
        found.append(run)
        # A re-run keeps its place: its first attempt was the latest completed one, and an
        # older failure it replaced must not come back while it runs again.
        if run["status"] == "completed" or run.get("run_attempt", 1) > 1:
            break
    seen = {run["id"] for run in own}
    return own + [run for run in found if run["id"] not in seen]


def jobs(run: dict[str, Any]) -> list[dict[str, Any]]:
    return checks(api(f"{API}/actions/runs/{run['id']}/jobs?per_page=100", "jobs"))


# A CI job that runs in parts is named with its part by GitHub, "CI: server tests (1 of 2)", and with
# its name alone when it was skipped (.github/workflows/ci.yml).
_PART = re.compile(r"(CI: [^()]+) \((\d+) of (\d+)\)")


def checks(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The jobs of a CI run as the checks they are: the parts of one job are one check.

    It passed only when every part passed, failed when one failed, and is running while one
    runs or while a part is missing. Then a pull request that skipped it is judged by main's
    whole check, and a fix of a red main must pass every part, however the tests were dealt to
    the parts.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in parts:
        matched = _PART.fullmatch(job.get("name", ""))
        grouped.setdefault(matched[1] if matched else job.get("name", ""), []).append(job)
    merged: list[dict[str, Any]] = []
    for name, group in grouped.items():
        if len(group) == 1 and group[0].get("name") == name:
            merged.append(group[0])
            continue
        conclusions = [job.get("conclusion") for job in group]
        failed = [c for c in conclusions if c not in (None, "success", "skipped")]
        dealt = sorted(
            (int(matched[3]), int(matched[2]))
            for matched in (_PART.fullmatch(job.get("name", "")) for job in group)
            if matched
        )
        whole = dealt == [(len(group), number) for number in range(1, len(group) + 1)]
        merged.append(
            {
                **group[0],
                "name": name,
                "status": "completed"
                if whole and all(job.get("status") == "completed" for job in group)
                else "in_progress",
                "conclusion": failed[0]
                if failed
                else conclusions[0]
                if whole and len(set(conclusions)) == 1
                else None,
                # Where every part ran: a retry at home must not reach a part that ran away.
                "labels": sorted(
                    set.intersection(*(set(job.get("labels") or []) for job in group))
                ),
            }
        )
    return merged


def verdict(
    run: dict[str, Any],
    parts: list[dict[str, Any]],
    *,
    images: bool = False,
    plan: str | None = None,
) -> tuple[str, list[str]]:
    label = plan or ("Images" if images else CI_RESULT)
    plan = plan or (IMAGES_PLAN if images else CI_RESULT)
    required = [job for job in parts if job["name"] == plan]
    failed = [
        job["name"] for job in parts if job.get("conclusion") not in (None, "success", "skipped")
    ]
    settled = (None, "success", "skipped", "cancelled")
    if (
        run.get("conclusion") == "cancelled"
        and all(job.get("conclusion") in settled for job in parts)
        and lender_replanning(
            str(run["id"]), head=run.get("head_sha"), attempt=run.get("run_attempt", 1)
        )
    ):
        return "pending", [plan]
    if (
        run.get("conclusion") == "cancelled"
        # The summary job runs even in a cancelled run, and fails for the jobs cancelled under it.
        and all(job.get("conclusion") in settled or job["name"] == plan for job in parts)
        and cancelled_inherited(run)
    ):
        # The gate cancelled it, still running, once main inherited its result: that result
        # stands for it.
        return "pending", [plan]
    if failed or run.get("conclusion") not in (None, "success"):
        return "red", failed or [label]
    if (
        run["status"] != "completed"
        or not parts
        or not required
        or any(job["status"] != "completed" for job in parts)
        or any(job.get("conclusion") != "success" for job in required)
        or run.get("conclusion") != "success"
    ):
        return "pending", [plan]
    return "green", []


def workflow_verdict(
    workflow: str, run: dict[str, Any], parts: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    return verdict(
        run, parts, images=workflow == "images", plan=E2E_PLAN if workflow == "e2e" else None
    )


@dataclass
class Health:
    state: str
    line: str
    failing: tuple[str, ...] = ()
    # A failed scheduled run of main's head: reported in the wake, never a reason to refuse.
    scheduled: str = ""
    # The tick's own retry of a failed main run: reported in the wake whenever the line changes.
    retry: str = ""


def scheduled_failure(head: str) -> str:
    latest = runs("images", head, scheduled=True)[:1]
    for run in latest:
        state, names = verdict(run, jobs(run), images=True)
        if state == "red":
            return f"scheduled {IMAGES} run {run['id']} failed: {', '.join(names)}"
    return ""


def main_health(
    head: str, retry_folder: Path | None = None, *, retry: bool = False
) -> Health:
    health = push_health(head, retry_folder, retry=retry)
    note = scheduled_failure(head)
    if note:
        health.scheduled = note
        health.line += f"; {note}"
    return health


def _failed_jobs(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [job for job in parts if job.get("conclusion") not in (None, "success", "skipped")]


def unreached_service(parts: list[dict[str, Any]]) -> str | None:
    """The advisory service whose absence alone failed a CI run, from the audit's annotation:
    "CI: dependencies" is the only failed job besides "CI result", which follows it."""
    if advisory_service is None:
        return None
    failed = _failed_jobs(parts)
    if {job["name"] for job in failed} - {CI_RESULT} != {"CI: dependencies"}:
        return None
    job = next(job for job in failed if job["name"] == "CI: dependencies")
    try:
        notes = api(f"{API}/check-runs/{job['id']}/annotations?per_page=100")
    except Waiting:
        # Unread is not unannotated: a pull request's handoff waits for the next read.
        raise
    except Refused:
        return None
    for note in notes:
        if note.get("title") == advisory_service.ANNOTATION:
            return advisory_service.unreachable(note.get("message") or "")
    return None


def _retry_failed_main(
    run: dict[str, Any],
    parts: list[dict[str, Any]],
    folder: Path,
    *,
    retry: bool,
    service: str | None = None,
) -> str:
    marker = folder / f"main-rerun-{run['id']}"
    if marker.exists():
        earlier = marker.read_text().strip()
        if service is None:
            return earlier
        # Its one retry was used, perhaps while the service was still down: once more when
        # the service answers again.
        recovered = folder / f"main-rerun-{run['id']}-recovered"
        if recovered.exists():
            return f"{earlier}; {recovered.read_text().strip()}"
        if not retry or not _rerunnable(run, parts, first=False):
            return earlier
        # A sent first retry is a second attempt: until GitHub shows it, the run it shows is the
        # failed first one, perhaps already running again.
        attempt = run.get("run_attempt")
        if not earlier.startswith("automatic retry of") and not (
            isinstance(attempt, int) and attempt >= 2
        ):
            return earlier
        if not advisory_service.answers(service):
            return (
                f"{earlier}; main run {run['id']} is re-run once {service}'s advisory "
                "service answers"
            )
        return f"{earlier}; " + _rerun(
            run, folder, recovered, f"after {service}'s advisory service answered again"
        )
    if not retry or not _rerunnable(run, parts, first=True):
        return ""
    return _rerun(run, folder, marker, "once")


def _rerunnable(run: dict[str, Any], parts: list[dict[str, Any]], *, first: bool) -> bool:
    failed = _failed_jobs(parts)
    # Missing runner/attempt evidence never authorizes spending hosted minutes. The tick
    # holds its machine-wide lock; the marker also covers GitHub's delay updating attempts.
    home = set(CONFIG.runner.labels)
    attempt = run.get("run_attempt")
    return not (
        run.get("status") != "completed"
        or (attempt != 1 if first else not isinstance(attempt, int))
        or not failed
        # Retrying the CI planner can choose hosted destinations for skipped dependents.
        or any(job.get("name") == CI_PLAN for job in failed)
        or any(job.get("status") != "completed" for job in parts)
        or any(not home.intersection(job.get("labels") or []) for job in failed)
    )


def _rerun(
    run: dict[str, Any], folder: Path, marker: Path, when: str, *, name: str = ""
) -> str:
    """Re-run the failed jobs of `run`: main's, or, with `name`, a pull request's run."""
    if not name:
        name = f"main run {run['id']}"
        current = git("ls-remote", "--exit-code", "origin", "refs/heads/main").split()
        if not current or current[0] != run.get("head_sha"):
            return f"automatic retry of {name} deferred: main moved"
    folder.mkdir(parents=True, exist_ok=True)
    note = f"automatic retry requested for failed jobs of {name} {when}"
    # Claim before contacting GitHub: an interrupted or ambiguous request must not repeat. Of two
    # calls at once (a handoff judges its own run outside the merge lock), only one sends it.
    try:
        with marker.open("x") as claim:
            claim.write(note + "\n")
    except FileExistsError:
        return f"the re-run of {name} is requested; GitHub has not shown it yet"
    try:
        done = command(
            "gh", "run", "rerun", str(run["id"]), "-R", REPOSITORY, "--failed", check=False
        )
    except BudgetWaiting:
        # The request was never sent, so this must remain eligible after reset.
        # All ambiguous failures and interruptions retain the original claim.
        marker.unlink()
        raise
    if done.returncode:
        note = f"automatic retry of {name} refused: {done.stderr.strip() or done.stdout.strip()}"
    else:
        note = f"automatically re-ran failed jobs of {name} {when}"
    marker.write_text(note + "\n")
    wake_log(note)
    return note


def wake_log(line: str) -> None:
    log = Path.home() / ".local/state/workflow/wake.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as output:
        output.write(f"{int(time.time())} {line}\n")


def _cancel(found: list[dict[str, Any]], folder: Path, whose: str) -> None:
    """Cancel each run of `found` still queued or running, so that it holds no runner ahead of
    live work, each once. A cancel GitHub refuses (the run already finishing) goes to the wake
    log once and stops nothing. One held back by the shared budget, or lost to a GitHub outage
    or a stall, is a wait: the caller stops, and it is tried again at the next call."""
    for old in folder.glob("cancel-*"):
        # A cancelled run has long ended by then.
        if time.time() - old.stat().st_mtime > CANCEL_CLAIM_SECONDS:
            old.unlink(missing_ok=True)
    for run in found:
        if run.get("status") == "completed":
            continue
        claim = folder / f"cancel-{run['id']}"
        # Claim before contacting GitHub: a run still finishing after its cancel, which GitHub
        # lists as running for a while, is not cancelled again.
        try:
            with claim.open("x") as output:
                output.write(f"cancel requested for run {run['id']} of {whose}\n")
        except FileExistsError:
            continue
        try:
            done = command("gh", "run", "cancel", str(run["id"]), "-R", REPOSITORY, check=False)
            error = (done.stderr.strip() or done.stdout.strip()) if done.returncode else ""
        except BudgetWaiting:
            claim.unlink()
            raise
        except Refused as failure:
            error = str(failure)
        if error and transient(error):
            claim.unlink()
            raise Waiting(f"could not cancel run {run['id']} of {whose} yet: {error}")
        if error:
            note = f"could not cancel run {run['id']} of {whose}: {error}"
            claim.write_text(note + "\n")
            wake_log(note)


def pull_service_wait(
    pull: dict[str, Any], folder: Path, number: str, *, retry: bool = False
) -> str | None:
    """Why a pull request whose checks failed only waits, or None when they failed for it.

    Its latest CI run failed only in "CI: dependencies" (and "CI result", which follows it),
    because the package audit could not reach an advisory service: nothing was found wrong.
    With `retry` (the tick, or the gate under its lock), its failed jobs are re-run once that
    service answers again, under
    main's home-runner rules, once for each failed attempt. When the re-run shows a failure
    again, or nothing can re-run it, the failure is the pull request's, as any other.
    """
    if {check.get("name") for check in _failed_rollup(pull)} - {
        "CI: dependencies",
        CI_RESULT,
    }:
        return None
    ci = runs("ci", pull["headRefOid"], pull=True)
    if not ci:
        return None
    return _service_wait(
        ci[0],
        jobs(ci[0]),
        folder,
        f"run {ci[0]['id']} of pull request {number}",
        "pull-rerun",
        # A blocked member's reviewer is not woken when the service answers: only a handoff,
        # which the tick drives, is re-run without another call.
        f"by the tick once it is handed off: publish the passed round, run merge-when-current "
        f"--handoff {number} and end with review finished",
        retry=retry,
    )


def batch_service_wait(
    run: dict[str, Any], parts: list[dict[str, Any]], folder: Path
) -> str | None:
    """Why a batch's red CI run only waits, or None when it failed for its members.

    The same audit runs in a batch's CI, so a run red only because it could not reach an
    advisory service is neither split nor blamed on a member: it waits, its failed jobs are
    re-run once the service answers, and that re-run is judged as any batch run.
    """
    return _service_wait(
        run,
        parts,
        folder,
        f"batch run {run['id']}",
        "batch-rerun",
        "by the gate call at the front of the train",
        retry=True,
    )


def _service_wait(
    run: dict[str, Any],
    parts: list[dict[str, Any]],
    folder: Path,
    name: str,
    prefix: str,
    who: str,
    *,
    retry: bool,
) -> str | None:
    attempt = run.get("run_attempt", 1)
    sent = sorted(folder.glob(f"{prefix}-{run['id']}-*"))
    if verdict(run, parts)[0] != "red":
        # A failed rollup with a run that is not red is GitHub still showing the attempt the
        # re-run replaced; a re-run that passed is the train's to judge, as any green re-run.
        return f"the re-run of {name} is running" if sent else None
    if (
        run.get("status") != "completed" or any(job.get("status") != "completed" for job in parts)
    ) and (sent or unreached_service(parts)):
        # An attempt is judged once it has finished: its other jobs may still run, and GitHub
        # may list a queued re-run with the failed jobs of the attempt before it.
        return f"{name} is still running after its audit could not reach an advisory service"
    marker = folder / f"{prefix}-{run['id']}-{attempt}"
    if marker in sent:
        if " refused: " in marker.read_text():
            return None
        return f"the re-run of {name} is requested; GitHub has not shown it yet"
    if sent:
        return None
    service = unreached_service(parts)
    if service is None or not _rerunnable(run, parts, first=False):
        return None
    waits = f"{name} could not reach {service}'s advisory service, and nothing was found wrong"
    later = f"it is re-run once the service answers, {who}"
    if not retry or not advisory_service.answers(service):
        return f"{waits}; {later}"
    return _rerun(
        run, folder, marker, f"after {service}'s advisory service answered again", name=name
    )


def push_health(
    head: str, retry_folder: Path | None = None, *, retry: bool = False
) -> Health:
    ci = runs("ci", head)
    recent = MainImages()
    images = main_image_runs(head, recent)
    assessments: list[tuple[str, list[str], dict[str, Any]]] = []
    retry_notes: list[str] = []
    unreached = ""
    if ci:
        parts = jobs(ci[0])
        assessments.append((*verdict(ci[0], parts), ci[0]))
        if assessments[-1][0] == "red":
            try:
                service = unreached_service(parts)
            except Waiting:
                # Diagnostic only: main stays red as it is, and is not re-run for the service.
                service = None
            if service:
                unreached = (
                    f"; CI: dependencies could not reach {service}'s advisory service, "
                    "and nothing was found wrong with main"
                )
            if retry_folder is not None:
                retry_notes.append(
                    _retry_failed_main(ci[0], parts, retry_folder, retry=retry, service=service)
                )
    else:
        assessments.append(("pending", [f"{CI_RESULT} has not started"], {}))
    for run in images:
        parts = jobs(run)
        # Main image pushes share a cancelling concurrency group. A newer run replaces
        # a cancellation, but cannot erase a job that actually failed before cancellation.
        superseded = run.get("conclusion") == "cancelled" and any(
            newer["id"] > run["id"] for newer in images
        )
        if superseded and all(
            job.get("conclusion") in (None, "success", "skipped", "cancelled") for job in parts
        ):
            assessments.append(("pending", [IMAGES_PLAN], run))
        else:
            assessments.append((*verdict(run, parts, images=True), run))
        # Even with a failed job, re-running a superseded cancellation could cancel
        # the replacement run. Leave that run to establish main's image result.
        if retry_folder is not None and assessments[-1][0] == "red" and not superseded:
            retry_notes.append(_retry_failed_main(run, parts, retry_folder, retry=retry))
    if (
        not any(run["head_sha"] == head for run in images)
        and ci
        and assessments[0][0] == "green"
        and main_images_needed(head, ci)
    ):
        assessments.append(("pending", [f"{IMAGES} have not started"], {}))
    failed = [(names, run) for state, names, run in assessments if state == "red"]
    if failed:
        since = min(run.get("updated_at", "unknown time") for _, run in failed)
        names = ", ".join(name for checks, _ in failed for name in checks)
        # Main is named as faultless only when CI's dependencies job was all that failed.
        line = f"main: red since {since}, {names} ({head[:12]})"
        line += unreached if len(failed) == 1 else ""
        earlier = [
            f"{IMAGES} run {run['id']} of {run['head_sha'][:12]}"
            for _, run in failed
            if run.get("head_sha", head) != head
        ]
        line += f"; failed in {', '.join(earlier)}" if earlier else ""
        retry_line = "; ".join(note for note in retry_notes if note)
        line += f"; {retry_line}" if retry_line else ""
        # This is diagnostic only: failure to read the history must keep the red gate closed.
        try:
            history = api(
                f"{API}/actions/workflows/{WORKFLOW_FILES['ci']}/runs"
                "?branch=main&event=push&status=success&per_page=100",
                "workflow_runs",
            )
            green = None
            seen: set[str] = set()
            for run in sorted(history, key=lambda item: item["id"], reverse=True):
                commit = run["head_sha"]
                if commit in seen or commit == head:
                    continue
                seen.add(commit)
                if command(
                    "git", "merge-base", "--is-ancestor", commit, head, check=False
                ).returncode:
                    continue
                previous_ci = runs("ci", commit)
                if (
                    not previous_ci
                    or verdict(previous_ci[0], jobs(previous_ci[0]))[0] != "green"
                ):
                    continue
                previous_images = main_image_runs(commit, recent)
                if any(
                    verdict(run, jobs(run), images=True)[0] != "green" for run in previous_images
                ):
                    continue
                if not any(
                    run["head_sha"] == commit for run in previous_images
                ) and main_images_needed(commit, previous_ci):
                    continue
                green = commit
                break
            if green:
                commits = git("log", "--reverse", "--format=%h %s", f"{green}..{head}").replace(
                    "\n", "; "
                )
                line += f"; commits since last green main {green[:12]}: {commits}"
            else:
                line += "; last green main not found in available CI history"
        except (Refused, KeyError, TypeError, ValueError):
            line += "; commits since last green main could not be read"
        return Health(
            "red", line, tuple(name for checks, _ in failed for name in checks), retry=retry_line
        )
    # Only pending checks may borrow a result. Main's own failures above always win.
    inherited = inherited_green(head)
    sources: list[str] = []
    # Main's own CI runs whose result it inherited: under the lock they are cancelled, below.
    redundant: list[dict[str, Any]] = []
    if inherited:
        ci_inherited = False
        unproven = inherited.get("ci_unproven")
        if (
            assessments[0][0] == "pending"
            and isinstance(unproven, list)
            and all(isinstance(name, str) for name in unproven)
        ):
            # A pull request runs only the CI jobs its files select; main runs every job.
            # Each job the pull request did not pass needs main's own success first. The gate
            # merges nothing while main waits for one, so no later push cancels that run.
            passed = {
                job["name"]
                for job in (jobs(ci[0]) if ci else [])
                if job["status"] == "completed" and job.get("conclusion") == "success"
            }
            waiting = [name for name in unproven if name not in passed]
            if waiting:
                assessments[0] = ("pending", waiting, assessments[0][2])
            else:
                assessments[0] = ("green", [], assessments[0][2])
                sources.append(f"CI run {inherited['ci_run']}")
                ci_inherited = True
                redundant += ci
        own_images = any(run["head_sha"] == head for run in images)
        if inherited.get("images_run") and inherited.get("images_complete") is True:
            # Every image job passed on this very tree, which holds every earlier push of main
            # too, so it stands in for the head's run and for earlier runs still going.
            if not own_images or any(state == "pending" for state, _, _ in assessments[1:]):
                assessments = [
                    assessments[0],
                    *(
                        ("green", [], run) if state == "pending" else (state, names, run)
                        for state, names, run in assessments[1:]
                    ),
                ]
                sources.append(f"{IMAGES} run {inherited['images_run']}")
        elif ci_inherited and not own_images and main_images_needed(head, ci):
            # Main asks this after its own green CI above; an inherited CI asks it here. An
            # earlier push's image run in the list does not stand in for the head's own.
            assessments.append(("pending", [f"{IMAGES} have not started"], {}))
        if retry and retry_folder is not None and redundant:
            try:
                cancel_inherited(head, inherited, redundant, retry_folder)
            except Waiting:
                # Never sent, or lost to an outage: the next tick under the lock cancels them.
                pass
            except (Refused, OSError) as error:
                wake_log(f"could not cancel main's inherited runs of {head[:12]}: {error}")
    pending = [name for state, names, _ in assessments if state != "green" for name in names]
    if pending:
        return Health("pending", f"main: pending, {', '.join(pending)} ({head[:12]})")
    source = (
        f"; inherited from {', '.join(sources)} on identical tree {inherited['tree']}"
        if sources
        else ""
    )
    return Health("green", f"main: green ({head[:12]}){source}")


def main_checkout() -> Path:
    return Path(
        git("-C", str(HERE), "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).parent


def inherited_green(head: str) -> dict[str, Any] | None:
    """The gate alone writes this receipt, after observing the actual squash on main."""
    path = main_checkout() / CONFIG.state_folder / "main-tree-green.json"
    try:
        receipt = json.loads(path.read_text())
        if (
            receipt["main"] == head
            and receipt["tree"] == git("rev-parse", f"{head}^{{tree}}")
            and isinstance(receipt["ci_run"], int)
            and receipt["ci_run"] > 0
        ):
            return receipt
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _cancel_entry(run: dict[str, Any], tree: str) -> dict[str, Any]:
    # A re-run is another attempt: only the attempt the gate cancelled reads as inherited.
    return {"run": run["id"], "attempt": run.get("run_attempt", 1), "tree": tree}


def cancelled_inherited(run: dict[str, Any]) -> bool:
    """Whether the gate cancelled this attempt of a run of main because main inherited its
    result on this very tree. Any other cancelled run of main is red."""
    if run.get("head_branch") != "main" or not run.get("head_sha"):
        return False
    receipt = inherited_green(run["head_sha"])
    listed = receipt.get("cancelled_inherited") if receipt else None
    return isinstance(listed, list) and _cancel_entry(run, receipt["tree"]) in listed


def cancel_inherited(
    head: str, receipt: dict[str, Any], ci: list[dict[str, Any]], folder: Path
) -> None:
    """Cancel main's CI runs of `head` still queued or running whose result main inherited. Each
    is listed in the receipt first, with the tree it belongs to, so its cancellation never
    turns main red; the runs of another tree leave the list.

    Only CI: main is judged by the CI runs of its head alone, but by its latest image run on any
    earlier push too, where a cancelled one would decide main once a later receipt replaced its
    entry. A re-run, the gate's own retry of a failed main included, is never cancelled: its
    result decides main."""
    found = [
        run
        for run in ci
        if run["head_sha"] == head
        and run.get("status") != "completed"
        and run.get("run_attempt", 1) == 1
    ]
    if not found:
        return
    tree = receipt["tree"]
    listed = [
        entry
        for entry in receipt.get("cancelled_inherited") or []
        if isinstance(entry, dict) and entry.get("tree") == tree
    ]
    listed += [_cancel_entry(run, tree) for run in found if _cancel_entry(run, tree) not in listed]
    if listed != receipt.get("cancelled_inherited"):
        with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as output:
            json.dump({**receipt, "cancelled_inherited": listed}, output)
            temporary = output.name
        os.replace(temporary, folder / "main-tree-green.json")
    _cancel(found, folder, f"main {head[:12]}, inherited on identical tree {tree}")


def strict_jobs(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A pull request's "CI: dependencies" passes over an advisory that main has too, with an
    "Advisory on main" warning (scripts/audit-against-base.py). That pass is not main's check:
    it neither makes the pull request the fix of a red main nor stands in for main's own run."""
    strict: list[dict[str, Any]] = []
    for job in parts:
        if job["name"] == "CI: dependencies" and job.get("conclusion") == "success":
            notes = api(f"{API}/check-runs/{job['id']}/annotations?per_page=100")
            if any(note.get("title") == "Advisory on main" for note in notes):
                job = {**job, "conclusion": "passed over an advisory on main"}
        strict.append(job)
    return strict


def record_green(
    folder: Path,
    base: str,
    tree: str,
    ci: dict[str, Any],
    ci_jobs: list[dict[str, Any]],
    images: list[dict[str, Any]],
    image_jobs: list[dict[str, Any]],
) -> None:
    # GitHub's squash may differ, or somebody may push main outside the lock. Either case
    # loses this optimization. A receipt is scoped to the observed commit, never future trees.
    git("fetch", "origin", "main")
    head = git("rev-parse", "origin/main")
    if (
        git("show", "-s", "--format=%P", head) != base
        or git("rev-parse", f"{head}^{{tree}}") != tree
    ):
        print("main did not match the tested squash; waiting for main's own checks", flush=True)
        return
    images_complete = bool(image_jobs) and all(
        job.get("conclusion") == "success" for job in image_jobs
    )
    # Skipped by the pull request's path filters: main's own run must pass these itself.
    unproven = sorted(job["name"] for job in ci_jobs if job.get("conclusion") != "success")
    with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as output:
        json.dump(
            {
                "main": head,
                "tree": tree,
                "ci_run": ci["id"],
                "ci_passed": sorted(
                    job["name"] for job in ci_jobs if job.get("conclusion") == "success"
                ),
                "ci_unproven": unproven,
                "images_run": images[0]["id"] if images else None,
                # Main runs release tests that ordinary PRs skip. A skipped PR job cannot
                # supply the green result for those checks on main.
                "images_complete": images_complete,
            },
            output,
        )
        temporary = output.name
    os.replace(temporary, folder / "main-tree-green.json")
    source = (
        f"CI run {ci['id']}"
        + (f" and {IMAGES} run {images[0]['id']}" if images_complete else "")
        + (f"; main must pass {', '.join(unproven)} itself" if unproven else "")
    )
    print(
        f"main {head[:12]} recorded {source} for inheritance on identical tree {tree}", flush=True
    )


def publish(folder: Path, health: Health) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "main-status"
    old = target.read_text() if target.exists() else ""
    line = health.line + "\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as output:
        output.write(line)
        temporary = output.name
    os.replace(temporary, target)
    # Every merge passes through pending to green; only a red or unreadable main, and the
    # change out of it, is worth waking the orchestrator for.
    before, after = (
        re.split(r"[ ,]", text.removeprefix("main: "), maxsplit=1)[0] for text in (old, line)
    )
    if (
        (before != after and {"red", "unknown"} & {before, after})
        or (health.scheduled and health.scheduled not in old)
        or (health.retry and line != old)
    ):
        (folder / "main-status.pending").write_text(line)


def refresh_health(folder: Path, *, retry: bool = False) -> tuple[str, Health]:
    if retry:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "merge.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Keep the tick responsive while a gate is refreshing or merging a PR.
                return _refresh_health(folder)
            # Main is fetched only after acquiring the gate's lock, held through reruns.
            return _refresh_health(folder, retry=True)
    return _refresh_health(folder)


def _refresh_health(folder: Path, *, retry: bool = False) -> tuple[str, Health]:
    # The tick asks a service a push found unreachable, so that its next outage is told too.
    if (
        retry
        and advisory_service
        and (note := advisory_service.down(folder))
        and advisory_service.answers(note["service"])
    ):
        advisory_service.note_up(folder)
    try:
        git("fetch", "origin", "main")
        head = git("rev-parse", "origin/main")
        health = main_health(head, folder, retry=retry)
    except (Refused, KeyError, TypeError, ValueError) as error:
        head = ""
        health = Health("unknown", f"main: unknown, {error}")
    publish(folder, health)
    if context := train_reads.tick():
        train_reads.save(context / "main.json", {"head": head})
    return head, health


def main_images_needed(head: str, ci: list[dict[str, Any]]) -> bool:
    # CI records the whole push range, which may contain several commits. A later manual CI
    # run has no push payload, so consult the original push run for the same main head.
    push = next((run for run in ci if run["event"] == "push"), None)
    before = None
    if push:
        for job in jobs(push):
            if job["name"] != CI_PLAN:
                continue
            notes = api(f"{API}/check-runs/{job['id']}/annotations?per_page=100")
            before = next(
                (note["message"].strip() for note in notes if note.get("title") == "Push base"),
                None,
            )
    # Before the notice existed, main's merges were single squashed commits.
    before = before or git("rev-parse", f"{head}^")
    if not re.fullmatch(r"[0-9a-f]{40}", before):
        raise Refused("main CI did not record a valid push base")
    if before != "0" * 40 and command("git", "cat-file", "-e", before, check=False).returncode:
        git("fetch", "origin", before)
    return images_needed(head, before, [], workflow_ref=head)


def images_needed(
    head: str, base: str, rollup: list[dict[str, Any]], *, workflow_ref: str | None = None
) -> bool:
    if any(check.get("name", "").startswith(IMAGES) for check in rollup):
        return True
    # Use the actual workflow's path filter, without a second maintained list of image inputs.
    workflow = git("show", f"{workflow_ref or base}:.github/workflows/{WORKFLOW_FILES['images']}")
    matches = path_filter(pull_request_paths(workflow))
    # NUL delimiters preserve filenames that Git would otherwise quote or split across lines;
    # the last NUL ends the list, and the empty entry after it is no file.
    paths = (
        command("git", "ls-tree", "-r", "--name-only", "-z", head, errors="surrogateescape")
        if base == "0" * 40
        else command("git", "diff", "--name-only", "-z", f"{base}...{head}", errors="surrogateescape")
    ).stdout.split("\0")[:-1]
    return any(matches(path) for path in paths)


def pull_request_paths(workflow: str) -> list[str]:
    """The `paths:` of a workflow's `pull_request:` trigger, each entry as YAML reads it.

    Each entry is a double-quoted string on a line of its own, a comment after it allowed.
    Any other line in the list is refused, never skipped: a pattern the gate did not read
    could start a run the gate never waits for.
    """
    patterns: list[str] = []
    trigger = listed = False
    for line in workflow.splitlines():
        if re.fullmatch(r"\s*(?:#.*)?", line):
            continue
        if not line.startswith("   "):
            trigger = re.fullmatch(r"  pull_request:\s*(?:#.*)?", line) is not None
            listed = False
        elif trigger and not line.startswith("     "):
            # An anchor (`paths: &name`) lets the push trigger share the same list.
            listed = re.fullmatch(r"    paths:\s*(?:&[^\s#]+\s*)?(?:#.*)?", line) is not None
        elif listed:
            entry = re.fullmatch(r'      - "((?:[^"\\]|\\.)+)"\s*(?:#.*)?', line)
            try:
                # Only the escapes YAML shares with JSON are read; any other is refused.
                if entry:
                    patterns.append(json.loads(f'"{entry[1]}"'))
                    continue
            except json.JSONDecodeError:
                pass
            raise Refused(f"could not read the Images workflow path filter: {line.strip()}")
    if not patterns:
        raise Refused("could not read the Images workflow path filter")
    return patterns


def path_filter(patterns: list[str]) -> Callable[[str], bool]:
    """Whether a changed file starts the workflow, by GitHub's filter pattern rules.

    The patterns are read in order and the last one that matches a file decides: a pattern
    starting with `!` takes the file back, so negated patterns alone match nothing.
    """
    rules = [
        (pattern.startswith("!"), _filter_pattern(pattern.removeprefix("!")))
        for pattern in patterns
    ]

    def matches(path: str) -> bool:
        started = False
        for negated, rule in rules:
            if rule.fullmatch(path):
                started = not negated
        return started

    return matches


def _filter_pattern(pattern: str) -> re.Pattern[str]:
    """One filter pattern as a regular expression. `*` is any characters but `/`, `**` is any
    characters, and `**/` where a folder's name starts is zero or more folders (inside a name,
    `**` is any characters and the `/` stays). `?` and `+` make what stands before them
    zero or one, or one or more, of it. `[...]` is one character of its set, with `-` ranges.
    A backslash makes the next character itself, and every other character is itself.

    GitHub does not document a `?` or `+` with nothing before it, or a `[` that no `]` closes
    (nor an empty `[]`): each of those is read as the character itself.
    """
    # Each part is what one piece of the pattern matches and how often it may stand: at least
    # `fewest` (0 or 1) times, and at most once or, with `many`, any number of times. Repeating
    # a part only changes those two, so no repetition ever nests inside another.
    parts: list[tuple[str, int, bool]] = []
    at = 0
    while at < len(pattern):
        char = pattern[at]
        if pattern.startswith("**/", at) and pattern[at - 1 : at] in ("", "/"):
            # Zero or more folders: repeating it, however, is still that.
            parts.append(("(?:.*/)?", 1, False))
            at += 3
            continue
        if pattern.startswith("**", at):
            parts.append((".", 0, True))
            at += 2
            continue
        at += 1
        if char == "*":
            parts.append(("[^/]", 0, True))
        elif char in "?+" and parts:
            expression, fewest, many = parts[-1]
            if expression != "(?:.*/)?":
                parts[-1] = (expression, 0, many) if char == "?" else (expression, fewest, True)
        elif char == "\\" and at < len(pattern):
            parts.append((re.escape(pattern[at]), 1, False))
            at += 1
        elif char == "[" and (found := _filter_set(pattern, at)):
            expression, at = found
            parts.append((expression, 1, False))
        else:
            parts.append((re.escape(char), 1, False))
    times = {(1, False): "", (0, False): "?", (1, True): "+", (0, True): "*"}
    return re.compile(
        "".join(expression + times[fewest, many] for expression, fewest, many in parts),
        flags=re.DOTALL,
    )


def _filter_set(pattern: str, at: int) -> tuple[str, int] | None:
    """The set that starts at `at`, just after a `[`, as one character of a regular expression,
    and where the pattern goes on after its `]`; None when no `]` closes a set of at least one
    character. A backslash makes the next character itself. A `-` between two characters of
    a-z, of A-Z or of 0-9 is the range from the first to the second, as GitHub allows ranges
    only there; any other `-`, and a range that runs backwards, is the characters themselves.
    """
    chars: list[tuple[str, bool]] = []
    while at < len(pattern) and (pattern[at] != "]" or not chars):
        if pattern[at] == "]":
            return None
        escaped = pattern[at] == "\\" and at + 1 < len(pattern)
        if escaped:
            at += 1
        chars.append((pattern[at], escaped))
        at += 1
    if at == len(pattern):
        return None
    members: list[str] = []
    index = 0
    while index < len(chars):
        char = chars[index][0]
        last = chars[index + 2][0] if index + 2 < len(chars) else ""
        ranged = any(first <= char <= last <= final for first, final in ("az", "AZ", "09"))
        if chars[index + 1 : index + 2] == [("-", False)] and ranged:
            members.append(f"{re.escape(char)}-{re.escape(last)}")
            index += 3
        else:
            members.append(re.escape(char))
            index += 1
    return f"[{''.join(members)}]", at + 1


def browser_needed(base: str, merge: str) -> bool:
    """Whether a merge needs browser tests at all, by E2E's own rule (scripts/e2e-plan.py).

    The gate reads the exact difference from main, so a merge that changes nothing needs
    none; the planner runs on an empty diff only because it cannot tell one from a misread.
    A repository that holds no planner has no browser suite, so no change needs one.
    """
    planner = HERE.parent / "e2e-plan.py"
    if not planner.exists():
        return False
    changed = command(
        "git", "diff", "--no-renames", "--name-only", "-z", base, merge, errors="surrogateescape"
    ).stdout.split("\0")[:-1]
    if not changed:
        return False
    found = importlib.util.spec_from_file_location("e2e_plan", planner)
    if found is None or found.loader is None:
        raise Refused("could not read the E2E planner")
    plan = importlib.util.module_from_spec(found)
    found.loader.exec_module(plan)
    return plan.browser_needed([path.encode(errors="surrogateescape") for path in changed])


def whole_suite(run: dict[str, Any], parts: list[dict[str, Any]], tree: str) -> bool:
    """A green E2E run that ran every browser test on exactly this tree.

    E2E's planner notes "Browser suite" and "Files tested" (.github/workflows/e2e.yml). Only
    "whole" says that every spec ran: a pull request's run of the specs its change reaches
    says "selected: ...", and a run from before the note existed says nothing.
    """
    if workflow_verdict("e2e", run, parts)[0] != "green" or not any(
        job["name"] == "playwright" and job.get("conclusion") == "success" for job in parts
    ):
        return False
    planner = next(job for job in parts if job["name"] == E2E_PLAN)
    notes = {
        (note.get("title"), note["message"].strip())
        for note in api(f"{API}/check-runs/{planner['id']}/annotations?per_page=100")
    }
    return {(NOTES.files_tested, tree), (NOTES.browser_suite, "whole")} <= notes


def inherited_image_jobs(
    parts: list[dict[str, Any]], tree: str, branch: str
) -> list[dict[str, Any]]:
    notes = _planner_notes(parts, IMAGES_PLAN)
    sources = [message for title, message in notes if title == "Inherited image run"]
    if not sources:
        return parts
    if len(sources) != 1 or not sources[0].isdigit():
        raise Refused("invalid inherited image run")
    run = api(f"{API}/actions/runs/{sources[0]}")
    if (
        run.get("event") != "pull_request"
        or run.get("head_branch") != branch
        or run.get("path") != f".github/workflows/{WORKFLOW_FILES['images']}"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise Refused("inherited image run did not pass on this branch")
    original = jobs(run)
    if (NOTES.files_tested, tree) not in _planner_notes(original, IMAGES_PLAN):
        raise Refused("inherited image run tested other files")
    proven = passed_jobs(original)
    effective = []
    for job in parts:
        if job.get("conclusion") == "skipped":
            group = [
                old
                for old in original
                if old["name"] == job["name"] or old["name"].startswith(job["name"] + " (")
            ]
            if group and job["name"] in proven:
                effective.extend(group)
                continue
            if job["name"] in (f"{IMAGES}: browser tests", f"{IMAGES}: image tests"):
                raise Refused("inherited image run did not pass every required part")
        effective.append(job)
    return effective


def _planner_notes(parts: list[dict[str, Any]], plan: str) -> set[tuple[Any, str]]:
    planner = next(job for job in parts if job["name"] == plan)
    return {
        (note.get("title"), note["message"].strip())
        for note in api(f"{API}/check-runs/{planner['id']}/annotations?per_page=100")
    }


def server_tests_selected(parts: list[dict[str, Any]]) -> bool:
    """Whether a pull request's CI ran fewer than every server test.

    CI's planner notes "Server tests" (.github/workflows/ci.yml): "whole" when every server test
    ran, "none" when the change needs no Python job (main runs it, and passes it itself before
    it is inherited), and "selected: ..." for the test files the change reaches. A run from
    before the note existed says nothing, which proves nothing either.
    """
    notes = _planner_notes(parts, CI_PLAN)
    return not {(NOTES.server_tests, "whole"), (NOTES.server_tests, "none")} & notes


def whole_server_tests(run: dict[str, Any], parts: list[dict[str, Any]], head: str) -> bool:
    """A green CI run that ran every server test on exactly this commit, run in parts or not."""
    if verdict(run, parts)[0] != "green" or not any(
        job["name"] == "CI: server tests" and job.get("conclusion") == "success"
        for job in checks(parts)
    ):
        return False
    return {(NOTES.merge_tested, head), (NOTES.server_tests, "whole")} <= _planner_notes(
        parts, CI_PLAN
    )


def _proves_whole(
    workflow: str, run: dict[str, Any], parts: list[dict[str, Any]], record: dict[str, Any]
) -> bool:
    if workflow == "ci":
        return whole_server_tests(run, parts, record["head"])
    return whole_suite(run, parts, record["tree"])


def tested_merge(parts: list[dict[str, Any]], base: str, head: str) -> str | None:
    for job in parts:
        if job["name"] != CI_PLAN:
            continue
        notes = api(f"{API}/check-runs/{job['id']}/annotations?per_page=100")
        for note in notes:
            if note.get("title") != NOTES.merge_tested:
                continue
            merge = note["message"].strip()
            if not re.fullmatch(r"[0-9a-f]{40}", merge):
                continue
            commit = api(f"{API}/git/commits/{merge}")
            if [parent["sha"] for parent in commit["parents"]] == [base, head]:
                return merge
    return None


def refresh_pull(
    pull: dict[str, Any],
    base: str,
    train: "Train",
    number: str,
    *,
    mechanical: bool = False,
) -> int:
    head = pull["headRefOid"]
    if git("rev-parse", "HEAD") != head or not git("branch", "--show-current"):
        raise Refused(
            "CI is stale; run this gate from the pull request's clean branch worktree to refresh it"
        )
    if git("status", "--porcelain"):
        raise Refused("CI is stale; the pull request worktree has uncommitted changes")
    # A refreshed green rerun is no longer a failure: its new head, or the resolution of a
    # conflict, waits for CI in the saved place like any other refresh. A fix joins the back.
    unblocked = train.blocked.pop(number, None) is not None
    train.refreshable.discard(number)
    merged = command("git", "merge", "--no-ff", "--no-commit", base, check=False)
    message = "chore: test against current main"
    rules = mechanical_conflicts()
    if mechanical:
        conflicts = git("diff", "--name-only", "--diff-filter=U").splitlines()
        if conflicts:
            Path(git("rev-parse", "--git-path", "MERGE_MSG")).write_text(
                "merge main: conflict in " + ", ".join(conflicts) + "\n"
            )
        # A failed merge without MERGE_HEAD is an operational error, not a resolution.
        if (
            merged.returncode
            and command("git", "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode
        ):
            raise Refused(merged.stderr.strip() or merged.stdout.strip())
    if mechanical and rules is None and (conflicts or conflicting(pull)):
        # Without the repository's rules no conflict is mechanical, whether git or only GitHub
        # sees it: the reviewer resolves it.
        where = f" in {', '.join(conflicts)}" if conflicts else ""
        raise Refused(
            f"non-mechanical conflict{where}: the repository resolves no conflict mechanically"
        )
    if mechanical and rules is not None:
        try:
            resolved = rules.resolve(head, base)
        except rules.MechanicalConflict as error:
            if conflicts:
                raise Refused(f"non-mechanical conflict in {', '.join(conflicts)}: {error}") from error
            raise Refused(str(error)) from error
        message = "merge main: mechanical conflict in " + ", ".join(resolved)
        merged.returncode = 0
    if merged.returncode:
        conflicts = git("diff", "--name-only", "--diff-filter=U").splitlines()
        if conflicts:
            message = "merge main: conflict in " + ", ".join(conflicts)
            Path(git("rev-parse", "--git-path", "MERGE_MSG")).write_text(message + "\n")
            train.conflicts[number] = head
            train.save()
            raise Refused(
                f"resolve the conflicts in {', '.join(conflicts)}, commit with '{message}', "
                "push, then wait for CI"
            )
        raise Refused(merged.stderr.strip() or merged.stdout.strip())
    if (
        not mechanical
        and rules is not None
        and not command("git", "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode
    ):
        # Git merges two migrations numbered alike in different files cleanly; the migration
        # tool then has two heads. The branch's new ones move past main's last, or wait as a
        # conflict.
        try:
            moved = rules.renumber_migrations(head, base)
        except rules.MigrationCollision as error:
            message = "merge main: conflict in " + ", ".join(error.paths)
            Path(git("rev-parse", "--git-path", "MERGE_MSG")).write_text(message + "\n")
            train.conflicts[number] = head
            train.save()
            raise Refused(
                f"main took the number of {', '.join(error.paths)} and it cannot be renumbered "
                f"mechanically ({error}); renumber the branch's migrations after main's last, "
                f"commit with '{message}', push, then wait for CI"
            ) from error
        except rules.MechanicalConflict as error:
            raise Refused(str(error)) from error
        if moved:
            message = "merge main: renumber migration " + ", ".join(
                f"{Path(old).name} to {Path(new).name}" for old, new in moved.items()
            )
    # An older run without provenance cannot prove freshness. If main is already contained,
    # an empty commit requests a new pull_request event rather than replaying the old event.
    if WHOLE_REQUEST and Handoff.read(train.path.parent, number) is not None:
        message += "\n\n" + WHOLE_REQUEST
    _commit_and_push(pull, train, number, message, save=unblocked)
    print(
        f"CI requested on current main {base[:12]}; "
        "wait for the new run, then call this gate again",
        flush=True,
    )
    return 2


def _commit_and_push(
    pull: dict[str, Any], train: "Train", number: str, message: str, *, save: bool = False
) -> None:
    head = pull["headRefOid"]
    git("commit", "--allow-empty", "-m", message)
    try:
        pushed = push("origin", f"HEAD:{pull['headRefName']}")
    except PushWaiting:
        # The worktree was clean on the published head: the next call merges main again.
        git("reset", "--keep", head)
        raise
    if pushed.returncode:
        raise Refused(f"could not push the refreshed branch: {pushed.refusal}")
    if save:
        train.save()
    note = Handoff.read(train.path.parent, number)
    if note is not None:
        # Only this gate may advance the authorization through a mechanical refresh.
        # A crash before this write conservatively hands the new head to a reviewer.
        if note.head != head:
            raise Refused("the merge handoff changed during refresh")
        note.head = git("rev-parse", "HEAD")
        note.save(train.path.parent, number)
        train.approved[number] = note.head
        train.save()


def _seconds(stamp: Any) -> float | None:
    try:
        moment = datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError, OverflowError, OSError:
        return None
    # GitHub writes 0001-01-01 for a check that never started.
    return moment if moment > 0 else None


def lost_check(
    pull: dict[str, Any],
    known: list[dict[str, Any]] | None = None,
    read: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """A check the rollup lists as queued or running whose workflow run completed at least
    LOST_AFTER_SECONDS ago, when every check not passing is lost so: a check still running for
    real may yet fail, and is waited for first. Each read run costs a request from the shared
    budget, so a check that started less than that ago ends the search: its run cannot have
    completed so long ago. `read` reads a run by its id; the tick passes one that keeps each
    answer for the rest of the tick.
    """
    now = float(os.environ.get("WORKFLOW_NOW") or time.time())
    seen = {str(run["id"]): run for run in known or []}
    # Every check that cannot be lost is ruled out before any run is read.
    waiting = []
    for check in pull["statusCheckRollup"]:
        if _passing_check(check):
            continue
        if check.get("__typename") == "StatusContext" or check.get("status") == "COMPLETED":
            return None
        match = re.search(r"/actions/runs/(\d+)(?:/|$)", check.get("detailsUrl") or "")
        started = _seconds(check.get("startedAt"))
        if not match or (started is not None and now - started < LOST_AFTER_SECONDS):
            return None
        waiting.append((check, match[1]))
    found = []
    for check, run_id in waiting:
        if run_id not in seen:
            seen[run_id] = (read or read_run)(run_id)
        run = seen[run_id]
        finished = _seconds(run.get("updated_at"))
        if (
            run.get("status") != "completed"
            or finished is None
            or now - finished < LOST_AFTER_SECONDS
        ):
            return None
        found.append(
            {
                "check": check.get("name", "unknown"),
                "run": int(run_id),
                "conclusion": run.get("conclusion"),
            }
        )
    # A failed run's check is named first: it is a failure, never pushed for.
    return min(found, key=lambda lost: lost["conclusion"] == "success", default=None)


def read_run(run: str) -> dict[str, Any]:
    return api(f"{API}/actions/runs/{run}", paginate=False)


def note_lost(folder: Path, number: str, head: str, lost: dict[str, Any]) -> None:
    """Tell the orchestrator through the wake, once per head."""
    name = f"{number}-{head}.json"
    pending = folder / "lost-checks.pending"
    if (pending / name).exists() or (folder / "lost-checks.sent" / name).exists():
        return
    try:
        pending.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=pending, delete=False) as output:
            json.dump({"number": number, "head": head, **lost}, output)
        os.replace(output.name, pending / name)
    except OSError as error:
        print(f"could not note the lost check for the wake: {error}", flush=True)


def rerun_lost(pull: dict[str, Any], train: "Train", number: str, lost: dict[str, Any]) -> int:
    """One empty commit gets GitHub a new check list; only one per head, so a check lost again
    on the commit pushed for it is reported to the orchestrator and not pushed for."""
    head = pull["headRefOid"]
    what = f"GitHub lost {lost['check']} of run {lost['run']}"
    note_lost(train.path.parent, number, head, lost)
    if lost["conclusion"] != "success":
        raise Refused(
            f"{what}, which ended {lost['conclusion']}: treat it as a failed check; "
            "the orchestrator has been told"
        )
    if train.reruns.get(number) == head:
        raise Refused(
            f"{what} again, on the commit this gate pushed to run CI again; "
            "it is not pushed for twice, and the orchestrator has been told"
        )
    if git("rev-parse", "HEAD") == train.reruns.get(number):
        raise Waiting("CI was requested again; GitHub does not show the pushed head yet")
    if git("rev-parse", "HEAD") != head or not git("branch", "--show-current"):
        raise Refused(
            f"{what}; run this gate from the pull request's clean branch worktree to run CI again"
        )
    if git("status", "--porcelain"):
        raise Refused(f"{what}; the pull request worktree has uncommitted changes")
    _commit_and_push(pull, train, number, f"ci: run CI again, {what}")
    train.reruns[number] = git("rev-parse", "HEAD")
    train.save()
    print(
        f"{what} after its run finished; an empty commit was pushed to run CI again: "
        "wait for the new run, then call this gate again",
        flush=True,
    )
    return 2


def read_pull(number: str) -> dict[str, Any]:
    if _planning_reads is not None:
        return train_reads.pull(number, _read_pull)
    return _read_pull(number)


def _read_pull(number: str) -> dict[str, Any]:
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


def lender_replanning(
    run_id: str,
    *,
    head: str | None = None,
    attempt: int | None = None,
    job_id: int | None = None,
) -> bool:
    """Only the lender's recorded cancellation is pending, never a successful check.

    Bound the exception to the cancelled attempt and a recent cancellation. An abandoned
    rerun or a failed new attempt must still reach the ordinary failure handling.
    """
    try:
        notes = json.loads((Path.home() / ".local/state/ci-overflow/replanned.json").read_text())
        note = notes[run_id]
        return (
            note["state"] in ("cancelled", "rerun")
            and 0 <= time.time() - note["updated_at"] <= 180
            and (head is None or note["head"] == head)
            and (attempt is None or note["attempt"] == attempt)
            and (job_id is None or job_id in note["jobs"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _lender_cancelled_check(check: dict[str, Any], head: str | None) -> bool:
    match = re.search(r"/actions/runs/(\d+)/job/(\d+)(?:/|$)", check.get("detailsUrl", ""))
    return bool(
        check.get("conclusion") == "CANCELLED"
        and match
        and lender_replanning(match[1], head=head, job_id=int(match[2]))
    )


def _failed_rollup(pull: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        check
        for check in pull["statusCheckRollup"]
        if (
            check.get("conclusion")
            in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE")
            and not _lender_cancelled_check(check, pull.get("headRefOid"))
        )
        or check.get("state") in ("FAILURE", "ERROR")
    ]


def failed_checks(pull: dict[str, Any]) -> bool:
    return bool(_failed_rollup(pull))


def ready(pull: dict[str, Any]) -> bool:
    return pull["state"] == "OPEN" and not pull["isDraft"] and pull["baseRefName"] == "main"


def conflicting(pull: dict[str, Any]) -> bool:
    return pull["mergeable"] == "CONFLICTING" or pull["mergeStateStatus"] == "DIRTY"


def fix_evidence(pull: dict[str, Any], base: str, health: Health) -> str | None:
    """What a judgment of `proves_main_fix` rests on, or None when its CI has not passed.

    The tick reconciles every 3 minutes and each proof costs several GitHub requests from
    the budget every session shares, so a judgment is kept until this evidence changes.
    """
    if not any(
        check.get("name") == CI_RESULT and check.get("conclusion") == "SUCCESS"
        for check in pull["statusCheckRollup"]
    ):
        return None
    checks = sorted(
        json.dumps(check, sort_keys=True, default=str) for check in pull["statusCheckRollup"]
    )
    return hashlib.sha256(
        json.dumps([pull["headRefOid"], base, sorted(health.failing), checks]).encode()
    ).hexdigest()


def proves_main_fix(pull: dict[str, Any], base: str, health: Health) -> bool:
    """Prioritize only fresh green evidence; the gate still verifies before merging."""
    try:
        if failed_checks(pull):
            return False
        head = pull["headRefOid"]
        ci = runs("ci", head, pull=True)
        if not ci:
            return False
        parts = jobs(ci[0])
        if verdict(ci[0], parts)[0] != "green":
            return False
        merge = tested_merge(parts, base, head)
        if merge is None:
            return False
        # Judge it as the gate does: a pass over main's own advisory is not the fix.
        passed = {job["name"] for job in strict_jobs(parts) if job.get("conclusion") == "success"}
        if set(health.failing) <= passed:
            return True
        # Image failures need evidence for the very same tree as the fresh CI merge.
        images = runs("images", head, pull=True)
        if not images:
            return False
        image_jobs = jobs(images[0])
        if verdict(images[0], image_jobs, images=True)[0] != "green":
            return False
        tree = api(f"{API}/git/commits/{merge}")["tree"]["sha"]
        plan = next(job for job in image_jobs if job["name"] == IMAGES_PLAN)
        notes = api(f"{API}/check-runs/{plan['id']}/annotations?per_page=100")
        if not any(
            note.get("title") == NOTES.files_tested and note["message"].strip() == tree
            for note in notes
        ):
            return False
        passed |= {job["name"] for job in image_jobs if job.get("conclusion") == "success"}
        return set(health.failing) <= passed
    except Waiting:
        raise
    except Refused, KeyError, TypeError, ValueError, IndexError, StopIteration:
        return False


def publish_first(number: str | None, batch: dict[str, Any] | None) -> None:
    """Tell the runners' lender which pull request is first, and the branch its batch is tested
    on: their CI and browser jobs get a runner before other pull requests' jobs
    (.github/runner/lend_runners.py). The train is never held up for this note."""
    path = Path.home() / ".local/state/workflow/train-first.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
                json.dump({"number": number, "batch": batch["branch"] if batch else None}, output)
            os.replace(output.name, path)
            # Preserve the lender heartbeat: the gate cannot claim the service is alive.
            # Publishing the identity here makes a batch visible before its dispatch.
            planner = Path.home() / ".local/state/ci-overflow/home/first.json"
            if planner.exists():
                state = json.loads(planner.read_text())
                if not isinstance(state, dict):
                    raise ValueError("planner note is not an object")
                state.update(number=number, batch=batch["branch"] if batch else None)
                with tempfile.NamedTemporaryFile(mode="w", dir=planner.parent, delete=False) as output:
                    json.dump(state, output)
                os.chmod(output.name, 0o644)
                os.replace(output.name, planner)
    except (OSError, ValueError, TypeError) as error:
        print(
            f"could not note the train's first member for the runners: {error}",
            flush=True,
        )


class Train:
    """Persistent line, read and written only while holding merge.lock.

    Calling the gate is the reviewer's declaration that review has passed. Waiting
    entries never push. A fresh green rerun restores a failed head's earlier place.
    `queue` keeps the order of joining; `line()` is the order served, by priority first.
    """

    def __init__(self, folder: Path) -> None:
        self.path = folder / "merge-train.json"
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.queue: list[str] = data.get("waiting", [])
        self.blocked: dict[str, str] = data.get("blocked", {})
        self.suspended: dict[str, str] = data.get("suspended", {})
        self.conflicts: dict[str, str] = data.get("conflicts", {})
        self.unavailable: list[str] = data.get("unavailable", [])
        self.pending: list[str] = data.get("pending", [])
        self.passing: list[str] = data.get("passing", [])
        self.turn: int = data.get("turn", 0)
        self.refreshable: set[str] = set(data.get("refreshable", []))
        self.approved: dict[str, str] = data.get("approved", {})
        # The first members of the line, each tested on main and the members ahead of it, on a
        # scratch branch of its own (_run_line). A batch or probes saved before it are dropped,
        # and the sweep deletes their branch.
        self.prefixes: dict[str, Any] | None = data.get("prefixes")
        # How many members are tested at once: one more after each merge, half after a failed
        # prefix, from 1 to WINDOW_CEILING.
        self.window: int = min(max(data.get("window", WINDOW_CEILING), 1), WINDOW_CEILING)
        # A failed prefix whose rerun passed on what main is now, with its member's head: that
        # member's next prefix is this run, not a new one.
        self.proven: dict[str, dict[str, Any]] = data.get("proven", {})
        self.batch_failed: dict[str, dict[str, Any]] = data.get("batch_failed", {})
        # A pull request merging alone whose own E2E or CI run was a selection: the whole
        # browser suite's or every server test's run of its tested merge, on a scratch branch
        # (_whole_suite_run). "workflows" names which; a record without it is the browser's.
        self.suites: dict[str, dict[str, Any]] = data.get("suites", {})
        # How each one's run stood at the last reconcile; not saved.
        self.suite_states: dict[str, str | None] = {}
        self.fixes: list[str] = data.get("fixes", [])
        self.proofs: dict[str, list[Any]] = data.get("proofs", {})
        # Each member's priority as its labels said at the last reconcile.
        self.priorities: dict[str, str] = data.get("priorities", {})
        # The head each member's lost-check push made: a check lost on it is not pushed for again.
        self.reruns: dict[str, str] = data.get("reruns", {})
        # Each member's lost check at the last reconcile, with its head, for the tick's wake.
        self.lost: dict[str, dict[str, Any]] = data.get("lost", {})
        # The last scratch run that fell short in a way a new scratch branch retests: its tested
        # tree and why (_retest). Cleared once a scratch run proves its tree.
        self.retest: dict[str, str] | None = data.get("retest")
        # Scratch branches whose records left the line, with the workflows dispatched on them and
        # when: the sweep cancels their runs until it has seen one of each (_sweep_scratch).
        self.dropped: list[dict[str, Any]] = [
            entry
            for entry in data.get("dropped") or []
            if isinstance(entry, dict)
            and isinstance(entry.get("branch"), str)
            and isinstance(entry.get("requested"), list)
            and isinstance(entry.get("dropped"), (int, float))
        ]
        self.scratch = self.scratch_records()
        self.first = self.front()

    def scratch_records(self) -> dict[str, dict[str, Any]]:
        """Every scratch branch the line still names, with its record."""
        records = [
            *(
                member["scratch"]
                for member in (self.prefixes["members"] if self.prefixes else [])
                if member["scratch"]
            ),
            *(failure["batch"] for failure in self.batch_failed.values()),
            *self.proven.values(),
            *self.suites.values(),
        ]
        return {
            record["branch"]: record
            for record in records
            if isinstance(record, dict) and isinstance(record.get("branch"), str)
        }

    def line(self) -> list[str]:
        """The waiting members in the order they are served: the fix of a red main first,
        then by priority, then by the order of joining within one priority."""
        return sorted(
            self.queue,
            # Proven fixes share the first place, keeping their order of joining.
            key=lambda number: (
                (0, 0)
                if number in self.fixes
                else (1, priority.rank(self.priorities.get(number, priority.UNLABELLED)))
            ),
        )

    def front(self) -> str | None:
        eligible = [
            number
            for number in self.line()
            if number not in self.suspended
            and number not in self.conflicts
            and number not in self.unavailable
            and (number not in self.blocked or number in self.refreshable)
        ]
        if eligible and eligible[0] in self.pending:
            return next((number for number in eligible if number in self.passing), eligible[0])
        return eligible[0] if eligible else None

    def save(self) -> None:
        first = self.front()
        if first != self.first:
            self.turn += 1
            self.first = first
        # A record that left the line may have runs GitHub does not list yet.
        current = self.scratch_records()
        watched = {entry["branch"] for entry in self.dropped}
        for branch, record in self.scratch.items():
            # A dispatch sent but never answered may have reached GitHub too.
            sent = sorted({*record.get("requested", []), *record.get("dispatching", [])})
            if branch not in current and branch not in watched and sent:
                self.dropped.append(
                    {"branch": branch, "requested": sent, "dropped": int(time.time())}
                )
        self.scratch = current
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as output:
            json.dump(
                {
                    "waiting": self.queue,
                    "fixes": self.fixes,
                    "proofs": self.proofs,
                    "priorities": self.priorities,
                    "blocked": self.blocked,
                    "suspended": self.suspended,
                    "conflicts": self.conflicts,
                    "unavailable": self.unavailable,
                    "pending": self.pending,
                    "passing": self.passing,
                    "refreshable": sorted(self.refreshable),
                    "turn": self.turn,
                    "approved": self.approved,
                    "prefixes": self.prefixes,
                    "window": self.window,
                    "proven": self.proven,
                    "batch_failed": self.batch_failed,
                    "suites": self.suites,
                    "reruns": self.reruns,
                    "lost": self.lost,
                    "retest": self.retest,
                    "dropped": self.dropped,
                },
                output,
            )
            temporary = output.name
        os.replace(temporary, self.path)
        publish_first(first, self.front_scratch(first))

    def front_scratch(self, first: str | None) -> dict[str, Any] | None:
        """The scratch branch whose runs decide the first member's turn: its prefix, or the
        whole suite of its tested merge."""
        members = self.prefixes["members"] if self.prefixes else []
        if members and members[0]["number"] == first and members[0]["scratch"]:
            return members[0]["scratch"]
        return self.suites.get(first or "")

    def merged_one(self) -> None:
        self.window = min(self.window + 1, WINDOW_CEILING)

    def failed_prefix(self) -> None:
        self.window = max(self.window // 2, 1)

    def reconcile(self, known: dict[str, Any] | None = None, acting: str | None = None) -> None:
        global _planning_reads
        context = train_reads.tick()
        if context is None:
            self._reconcile(known, acting)
            return
        pulls = train_reads.planning_pulls(context)
        # Preserve bulk-only fields (number/body) when the gate supplies its fresh view.
        for number, pull in (known or {}).items():
            pulls[number] = {**pulls.get(number, {}), **pull}
        train_reads.save(context / "planning-pulls.json", pulls)
        health = self.path.parent / "main-status"
        base = train_reads.load(context / "main.json").get("head")
        if not base:
            base = git("ls-remote", "origin", "refs/heads/main").split()[0]
            train_reads.save(context / "main.json", {"head": base})

        def inputs(queue: list[str]) -> list[Any]:
            # Local authorizations and owner questions can change while the tick runs.
            notes = []
            for number in queue:
                root = CONFIG.state(
                    CONFIG.checkout_of(self.path.parent) / ".claude/worktrees" / f"ticket-{number}"
                )
                paths = [
                    *root.glob("*.owner-blocked"),
                    *root.glob("*.interaction-blocked"),
                    *root.glob("*.pushed"),
                ]
                paths += [
                    Handoff.path(self.path.parent, number),
                    self.path.parent / f"merge-review-{number}",
                ]
                notes += [(str(path), path.stat().st_mtime_ns) for path in paths if path.exists()]
            return [
                health.read_text() if health.exists() else "",
                sorted(notes),
                (context / "sessions").read_text(),
            ]

        def signature() -> str:
            return train_reads.digest(
                [self.path.read_text() if self.path.exists() else "", base, pulls]
                + inputs(self.queue)
            )

        previous = train_reads.load(context / "reconciled.json")
        delegated = acting is None or Handoff.path(self.path.parent, acting).exists()
        if delegated and previous.get("signature") == signature():
            self.suite_states = previous["suite_states"]
            return
        # The members the scan reads, blocked ones included.
        queue = list(dict.fromkeys([*self.queue, *self.blocked]))
        seen = inputs(queue)
        reads = train_reads.Reads(self.path.parent, base)
        _planning_reads = reads
        try:
            self._reconcile(known, acting)
            _batch_signal(self)
            reads.finish()
        finally:
            _planning_reads = None
        # A note created or cleared while the scan ran may not be what it read: keep the
        # result only for the inputs it saw, so the next reconcile computes it again. The key
        # is taken first: a note changed after it leaves the key on the old note, a miss.
        key = signature()
        if inputs(queue) != seen:
            return
        train_reads.save(
            context / "reconciled.json",
            {
                "signature": key,
                "at": time.time(),
                "suite_states": self.suite_states,
            },
        )

    def _reconcile(self, known: dict[str, Any] | None = None, acting: str | None = None) -> None:
        remaining: list[str] = []
        pulls: dict[str, Any] = {}
        suite_states: dict[str, str | None] = {}
        lost: dict[str, dict[str, Any]] = {}
        marked, self.refreshable = self.refreshable, set()
        # Use the same session state and owner-question notes as the tick. A background
        # job leaves its session running and must not lose its turn merely for waiting.
        sessions = {
            fields[0]: fields[1]
            for line in command("bash", str(HERE / "session"), "list").stdout.splitlines()
            if len(fields := line.split()) >= 2
        }
        self.unavailable = []
        health_file = self.path.parent / "main-status"
        health = health_file.read_text().strip() if health_file.exists() else ""
        # Older state files removed failed entries from waiting altogether.
        for number in dict.fromkeys([*self.queue, *self.blocked]):
            pull = known[number] if known and number in known else read_pull(number)
            pulls[number] = pull
            if not ready(pull):
                self.blocked.pop(number, None)
                continue
            if number in self.blocked and self.blocked[number] != pull["headRefOid"]:
                # A fix on a new head joins at the back when its reviewer calls the gate.
                # Keep its audit eligible for a checks-finished wake until that claim.
                continue
            if self.conflicts.get(number) != pull["headRefOid"]:
                self.conflicts.pop(number, None)
            notes = CONFIG.state(
                CONFIG.checkout_of(self.path.parent) / ".claude" / "worktrees" / f"ticket-{number}"
            )
            # The session running the gate is acting now, whatever note an answered permission
            # dialog or a wake by its own job left behind.
            delegated = (
                Handoff.path(self.path.parent, number).exists()
                or (self.path.parent / f"merge-review-{number}").exists()
            )
            available = (
                authorized(self.path.parent, number, pull["headRefOid"])
                if delegated
                else number == acting
            )
            for kind in ("review", "audit"):
                name = f"{kind}-{number}"
                if (
                    not delegated
                    and name in sessions
                    and sessions[name] != "limit"
                    and not any(
                        (notes / f"{name}.{suffix}").exists()
                        for suffix in ("owner-blocked", "interaction-blocked", "pushed")
                    )
                ):
                    available = True
            # GitHub can detect a conflict before any gate attempts a local merge. Keep
            # its queue slot, but let the next available pull proceed while it is resolved.
            if not available or conflicting(pull):
                self.unavailable.append(number)
            if self.suspended.get(number) != f"{pull['headRefOid']}:{health}":
                self.suspended.pop(number, None)
            suite = self.suites.get(number)
            try:
                suite_states[number] = (
                    _suite_state(suite) if suite and suite["member"] == pull["headRefOid"] else None
                )
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence neither blocks it nor lets another take its turn.
                suite_states[number] = None
            if failed_checks(pull):
                self.blocked[number] = pull["headRefOid"]
            elif suite_states[number] == "red" and self.blocked.get(number) == pull["headRefOid"]:
                # The gate blocked it when it told its session that a whole suite failed
                # (_whole_suite_run). Until then it keeps its turn, so the finished run
                # changes the turn and the tick wakes that session. A passing rerun lifts it.
                pass
            elif number != acting or number in self.blocked:
                # The rollup alone cannot prove that a rerun passed or tested current main.
                # Missing evidence keeps this entry blocked without holding up other pulls.
                try:
                    ci = runs("ci", pull["headRefOid"], pull=True)
                    parts = jobs(ci[0]) if ci else []
                    state = verdict(ci[0], parts)[0] if ci else "pending"
                    # A red CI run is a failure already, whatever its checks still show.
                    if state == "green" and not _passing_rollup(pull):
                        found = lost_check(pull, ci[:1])
                        if found:
                            lost[number] = {"head": pull["headRefOid"], **found}
                            note_lost(self.path.parent, number, pull["headRefOid"], found)
                    if state == "red":
                        self.blocked[number] = pull["headRefOid"]
                    elif state == "green" and number in self.blocked:
                        # Its own green run cannot clear a failure its prefix run found. A
                        # new run or attempt of that prefix, or a fix, is needed first.
                        failure = self.batch_failed.get(number)
                        if failure and failure["pull"] == _pull_run_token(pull["headRefOid"]):
                            if not _batch_rerun_green(failure):
                                remaining.append(number)
                                continue
                            base = git("ls-remote", "origin", "refs/heads/main").split()[0]
                            if _proves_on(failure["batch"], base):
                                # The rerun tested it on what main is now. It gets its earlier
                                # slot back, and that run is its next prefix.
                                self.blocked.pop(number)
                                if "under" in failure["batch"]:
                                    self.proven[number] = {
                                        **failure["batch"],
                                        "member": pull["headRefOid"],
                                    }
                            else:
                                self.refreshable.add(number)
                            self.batch_failed.pop(number)
                            remaining.append(number)
                            continue
                        self.batch_failed.pop(number, None)
                        base = git("ls-remote", "origin", "refs/heads/main").split()[0]
                        if tested_merge(parts, base, pull["headRefOid"]):
                            self.blocked.pop(number)
                        else:
                            # Still blocked, but its turn may refresh the stale green run.
                            self.refreshable.add(number)
                except Refused, KeyError, TypeError, ValueError, IndexError:
                    # An unread run changes nothing: a green re-run on this same head keeps
                    # its mark, so its turn still refreshes it.
                    if number in marked and self.blocked.get(number) == pull["headRefOid"]:
                        self.refreshable.add(number)
            remaining.append(number)
        self.queue = remaining
        # A label changed while waiting moves its pull request at this turn.
        self.priorities = {number: priority.of(pulls[number].get("labels")) for number in remaining}
        self.fixes = []
        proofs: dict[str, list[Any]] = {}
        if health.startswith("main: red"):
            try:
                base = git("ls-remote", "origin", "refs/heads/main").split()[0]
                current = main_health(base, self.path.parent)
                if current.state == "red":
                    for number in remaining:
                        if number in self.unavailable or number in self.conflicts:
                            continue
                        evidence = fix_evidence(pulls[number], base, current)
                        if evidence is None:
                            continue
                        judged = self.proofs.get(number)
                        if judged and judged[0] == evidence:
                            proven = judged[1]
                        else:
                            try:
                                proven = proves_main_fix(pulls[number], base, current)
                            except Waiting:
                                # An unreadable GitHub proves nothing; judge it again next time.
                                continue
                        proofs[number] = [evidence, proven]
                        if proven:
                            self.fixes.append(number)
                            self.suspended.pop(number, None)
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Missing health or proof must never promote a candidate.
                self.fixes = []
        self.proofs = proofs
        self.suspended = {
            number: token for number, token in self.suspended.items() if number in remaining
        }
        # A member that left the line, or pushed a new head, cannot reuse its isolated run.
        self.batch_failed = {
            number: failure
            for number, failure in self.batch_failed.items()
            if number in remaining and number in self.blocked
        }
        self.suites = {
            number: suite
            for number, suite in self.suites.items()
            if number in remaining and pulls[number]["headRefOid"] == suite["member"]
        }
        self.suite_states = suite_states
        # The acting member's lost check is judged by its own gate call.
        self.lost = {
            number: found
            for number, found in {**self.lost, **lost}.items()
            if number in remaining
            and found["head"] == pulls[number]["headRefOid"]
            and (number in lost or number == acting)
        }
        self.reruns = {number: head for number, head in self.reruns.items() if number in remaining}
        self.proven = {
            number: scratch
            for number, scratch in self.proven.items()
            if number in remaining and pulls[number]["headRefOid"] == scratch["member"]
        }
        self._pending_turns(pulls, suite_states)
        self.save()

    def _pending_turns(self, pulls: dict[str, Any], suite_states: dict[str, str | None]) -> None:
        # Pending entries keep their FIFO slots. Only a green run tested on current main
        # can take their turn; stale green entries still wait for the usual refresh path.
        self.pending = []
        self.passing = []
        if len(self.queue) < 2:
            return
        base = git("ls-remote", "origin", "refs/heads/main").split()[0]
        # A saved prefix proves its member's tree on this main, even after the squash of the
        # member ahead of it moved main.
        members = (
            self.prefixes["members"] if self.prefixes and self.prefixes["base"] == base else []
        )
        batched = {member["number"] for member in members}
        green: dict[str, list[dict[str, Any]]] = {}
        line = self.line()
        for number in line:
            if (
                number in self.blocked
                or number in self.suspended
                or number in self.conflicts
                or number in self.unavailable
            ):
                continue
            pull = pulls[number]
            head = pull["headRefOid"]
            if suite_states.get(number) == "pending":
                # A whole suite of its tested merge still runs, as its own checks would.
                self.pending.append(number)
                continue
            try:
                ci = runs("ci", head, pull=True)
                parts = jobs(ci[0]) if ci else []
                state = verdict(ci[0], parts)[0] if ci else "pending"
                if state == "pending" or not _passing_rollup(pull):
                    self.pending.append(number)
                    continue
                if state != "green":
                    continue
                images = runs("images", head, pull=True)
                if images:
                    state = verdict(images[0], jobs(images[0]), images=True)[0]
                elif images_needed(head, base, pull["statusCheckRollup"]):
                    state = "pending"
                if state == "pending":
                    self.pending.append(number)
                elif state == "green" and (
                    pull["mergeable"] == "MERGEABLE"
                    or (
                        number in batched
                        and self.prefixes
                        and self.prefixes["merged"]
                        and pull["mergeable"] == "UNKNOWN"
                    )
                ):
                    green[number] = parts
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence never grants a later member a turn.
                continue
        # A prefix holds the members ahead of it: behind a pending one it cannot merge first.
        ahead = []
        for member in members:
            if member["number"] in self.pending:
                break
            ahead.append(member["number"])
        batched = set(ahead)
        if self.pending:
            # Only a member behind a pending one takes its turn. A green member ahead of
            # every pending one is first anyway and keeps its single path.
            behind = line[line.index(self.pending[0]) + 1 :]
            for number, parts in green.items():
                if number not in behind:
                    continue
                try:
                    if number in batched or tested_merge(parts, base, pulls[number]["headRefOid"]):
                        self.passing.append(number)
                except Refused, KeyError, TypeError, ValueError, IndexError:
                    continue

    def suspend(self, number: str, head: str, health: str) -> None:
        self.suspended[number] = f"{head}:{health}"
        self.save()

    def fail(self, number: str, head: str) -> None:
        self.blocked[number] = head
        if number not in self.queue:
            self.queue.append(number)
        self.refreshable.discard(number)
        self.save()

    def remove(self, number: str) -> None:
        self.queue = [entry for entry in self.queue if entry != number]
        self.blocked.pop(number, None)
        self.refreshable.discard(number)
        self.suspended.pop(number, None)
        self.conflicts.pop(number, None)
        self.approved.pop(number, None)
        self.batch_failed.pop(number, None)
        self.suites.pop(number, None)
        self.reruns.pop(number, None)
        self.lost.pop(number, None)
        self.proven.pop(number, None)
        self.save()

    def claim(self, number: str, pull: dict[str, Any]) -> bool:
        self.approved[number] = pull["headRefOid"]
        self.reconcile({number: pull}, number)
        if failed_checks(pull):
            self.fail(number, pull["headRefOid"])
        if self.blocked.get(number) == pull["headRefOid"] and number not in self.refreshable:
            # It keeps its place, passed over, as any failed member until a green re-run.
            try:
                waits = pull_service_wait(pull, self.path.parent, number, retry=True)
            except Waiting:
                raise
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence of the service proves no wait: refused as before.
                waits = None
            if waits:
                raise Waiting(waits)
            if self.suite_states.get(number) == "red" and not failed_checks(pull):
                raise Refused(
                    "a whole suite run on this pull request's tested merge failed; treat it as "
                    "a failed check: fix and push, or rerun that run and call this gate again"
                )
            failure = self.batch_failed.get(number)
            if failure and not failed_checks(pull):
                raise Refused(
                    f"the {failure['workflow']} run of this pull request's prefix "
                    f"({failure['batch']['branch']}) failed on main and the members ahead of it; "
                    "treat it as a failed check: fix and push, or rerun that run and call this "
                    "gate again"
                )
            raise Refused(
                "this pull request failed checks; fix it before rejoining the merge train, "
                "or, for a flaky failure, call this gate again once a re-run is green"
            )
        if self.blocked.get(number) != pull["headRefOid"]:
            self.blocked.pop(number, None)
        if number not in self.queue:
            self.queue.append(number)
            self.reconcile({number: pull}, number)
        self.save()
        return self.first == number

    def member(self, number: str) -> dict[str, Any]:
        """What the line holds of one member alone."""
        return {
            "queued": number in self.queue,
            **{name: getattr(self, name).get(number) for name in MEMBER_ENTRIES},
            **{name: number in getattr(self, name) for name in MEMBER_MARKS},
        }

    def admit(self, view: "OwnView") -> None:
        """Take a member's own part from its view, keeping every other member as saved."""
        number = view.number
        if view.left:
            self.queue = [entry for entry in self.queue if entry != number]
        if number in view.queue and number not in self.queue:
            self.queue.append(number)
            # Whether its checks still run is the tick's to read with the others': until then
            # it does not take the turn from a member whose run is green.
            if number not in self.pending:
                self.pending.append(number)
        for name in MEMBER_ENTRIES:
            entries, judged = getattr(self, name), getattr(view, name)
            entries.pop(number, None)
            if number in judged:
                entries[number] = judged[number]
        self.unavailable = [entry for entry in self.unavailable if entry != number]
        self.fixes = [entry for entry in self.fixes if entry != number]
        self.refreshable.discard(number)
        self.unavailable += [number] if number in view.unavailable else []
        self.fixes += [number] if number in view.fixes else []
        self.refreshable |= {number} & view.refreshable
        self.save()


class OwnView(Train):
    """One member's part of the line, judged as `claim` judges it, apart from the other members.

    A handoff judges its own pull request with it before it takes the merge lock: it reads the
    saved line without the lock, reads GitHub only about this member and main, and saves
    nothing. The line takes its result under the lock (Train.admit); the other members' order is
    left to the turn of the line, whose merge driver reads them.
    """

    def __init__(self, folder: Path, number: str) -> None:
        super().__init__(folder)
        self.number = number
        # Whether the member left the queue while judged: it joins again at the back.
        self.left = False
        # Whether the line's judgment of the member finished, so its result can be taken.
        self.judged = False
        self.queue = [entry for entry in self.queue if entry == number]
        for name in MEMBER_ENTRIES:
            entries = getattr(self, name)
            setattr(self, name, {number: entries[number]} if number in entries else {})
        self.unavailable = [entry for entry in self.unavailable if entry == number]
        self.fixes = [entry for entry in self.fixes if entry == number]
        self.refreshable &= {number}
        self.pending = []
        self.passing = []
        self.first = self.front()

    def reconcile(self, known: dict[str, Any] | None = None, acting: str | None = None) -> None:
        self.judged = False
        self._reconcile(known, acting)
        if self.number not in self.queue:
            self.left = True
        self.judged = True

    def save(self) -> None:
        self.first = self.front()


def _run_token(run: dict[str, Any]) -> str:
    return f"{run['id']}:{run.get('run_attempt', 1)}"


def _pull_run_token(head: str) -> str:
    return "/".join(
        _run_token(run)
        for workflow in ("ci", "images")
        for run in runs(workflow, head, pull=True)[:1]
    )


def _passing_check(check: dict[str, Any]) -> bool:
    if check.get("__typename") == "StatusContext":
        return check.get("state") == "SUCCESS"
    return check.get("status") == "COMPLETED" and check.get("conclusion") in (
        "SUCCESS",
        "SKIPPED",
        "NEUTRAL",
    )


def _passing_rollup(pull: dict[str, Any]) -> bool:
    return all(_passing_check(check) for check in pull["statusCheckRollup"])


def _batch_ready(pull: dict[str, Any], base: str, *, settling: bool = False) -> bool:
    # Right after an earlier member's squash moves main, GitHub reports UNKNOWN until it
    # recomputes. The caller then proves a clean merge with its own prefix-tree check.
    mergeable = ("MERGEABLE", "UNKNOWN") if settling else ("MERGEABLE",)
    if (
        not ready(pull)
        or conflicting(pull)
        or pull["mergeable"] not in mergeable
        or not _passing_rollup(pull)
    ):
        return False
    head = pull["headRefOid"]
    if command("git", "cat-file", "-e", head, check=False).returncode:
        git("fetch", "origin", head)
    ci = runs("ci", head, pull=True)
    parts = jobs(ci[0]) if ci else []
    if not ci or verdict(ci[0], parts)[0] != "green":
        return False
    # The older-version job also passes when no migration changed. Only an actual
    # compatibility check depends on this member's own base; keep it on the single
    # path instead of comparing the old application of a combined prefix.
    if any(
        job["name"] == "CI: older version on new tables" and job.get("conclusion") != "skipped"
        for job in parts
    ) and git(
        "diff", "--name-only", f"{base}...{head}", "--",
        "migrations/versions",
    ):
        return False
    images = runs("images", head, pull=True)
    if images_needed(head, base, pull["statusCheckRollup"]) and not images:
        return False
    return not images or verdict(images[0], jobs(images[0]), images=True)[0] == "green"


def _merge_tree(base: str, head: str) -> str | None:
    if command("git", "cat-file", "-e", head, check=False).returncode:
        git("fetch", "origin", head)
    done = command("git", "merge-tree", "--write-tree", base, head, check=False)
    if done.returncode == 1:
        return None
    if done.returncode:
        raise Refused(f"could not build batch tree: {done.stderr.strip()}")
    tree = done.stdout.splitlines()[0]
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise Refused("git did not return a batch tree")
    return tree


def _taken_migrations(head: str, base: str) -> list[str]:
    rules = mechanical_conflicts()
    if rules is None:
        return []
    try:
        return rules.taken_migrations(head, base)
    except rules.MechanicalConflict as error:
        raise Refused(f"could not compare migration numbers: {error}") from error


def _batch_runs(batch: dict[str, Any], workflow: str) -> list[dict[str, Any]]:
    return sorted(
        (
            run
            for run in api(
                f"{API}/actions/workflows/{WORKFLOW_FILES[workflow]}/runs?head_sha={batch['head']}"
                "&event=workflow_dispatch&per_page=100",
                "workflow_runs",
            )
            if run["head_sha"] == batch["head"]
            and run["head_branch"] == batch["branch"]
            and run["event"] == "workflow_dispatch"
        ),
        key=lambda run: run["id"],
        reverse=True,
    )[:1]


def _suite_workflows(suite: dict[str, Any]) -> list[str]:
    return suite.get("workflows", ["e2e"])


def _suite_state(suite: dict[str, Any]) -> str | None:
    """How a tested merge's dispatched whole suites stand: None before they were requested."""
    if not suite["requested"]:
        return None
    states = []
    for workflow in _suite_workflows(suite):
        found = _batch_runs(suite, workflow)
        states.append(
            workflow_verdict(workflow, found[0], jobs(found[0]))[0] if found else "pending"
        )
    return "red" if "red" in states else "pending" if "pending" in states else "green"


def _batch_signal(train: Train) -> None:
    """Let the tick wake the front again when its scratch checks finish, on unchanged PR heads.

    Every prefix's runs are noted for --train-status; only the first member's, whose runs decide
    its turn, change the turn."""
    members = train.prefixes["members"] if train.prefixes else []
    scratch = [
        (member["scratch"], index == 0)
        for index, member in enumerate(members)
        if member["scratch"]
    ]
    if train.first in train.suites:
        scratch.append((train.suites[train.first], True))
    noted = changed = False
    for record, first in scratch:
        signal = [
            [
                workflow,
                run["id"],
                run.get("run_attempt", 1),
                run["status"],
                run.get("conclusion"),
            ]
            for workflow in _suite_workflows(record)
            for run in _batch_runs(record, workflow)
        ]
        if _batch_service_answered(record, train.path.parent):
            # Its red run does not change when the service answers again: only this does.
            signal.append(["advisory service answers"])
        if signal != record.get("signal"):
            record["signal"] = signal
            noted = True
            changed |= first
    if changed:
        train.turn += 1
    if noted:
        train.save()


def _batch_service_answered(batch: dict[str, Any], folder: Path) -> bool:
    """Whether a batch, or a whole suite's run, whose CI waits for an advisory service may
    re-run now: the service answers again, or its re-run was already sent (kept true, so that
    sending it is no wake)."""
    if "ci" not in _suite_workflows(batch):
        return False
    for run in _batch_runs(batch, "ci"):
        if run.get("status") != "completed" or run.get("conclusion") != "failure":
            return False
        if any(folder.glob(f"batch-rerun-{run['id']}-*")):
            return True
        try:
            service = unreached_service(jobs(run))
        except Refused:
            # Unread now is read on a later tick; the gate call decides the wait itself.
            return False
        return service is not None and advisory_service.answers(service)
    return False


def _batch_rerun_green(failure: dict[str, Any]) -> bool:
    batch = failure["batch"]
    retried = _batch_runs(batch, failure["workflow"])
    if not retried or _run_token(retried[0]) == failure["run"]:
        return False
    return all(
        found and workflow_verdict(workflow, found[0], jobs(found[0]))[0] == "green"
        for workflow in batch["workflows"]
        for found in [_batch_runs(batch, workflow)]
    )


def _batch_wait(message: str) -> int:
    print(message, flush=True)
    command("bash", str(HERE / "notify"), "waiting")
    return 3


def _retest(train: Train, tree: str, reason: str) -> NoReturn:
    """A scratch run that fell short without failing waits for a retest on a new scratch branch.

    The caller has dropped the scratch record, so the next call tests the same merge again and
    the hand-offs stay. The same shortfall twice in a row on one tree is a real fault: a refusal.
    """
    if train.retest == {"tree": tree, "reason": reason}:
        raise Refused(f"{reason}, twice in a row on the same tested files")
    train.retest = {"tree": tree, "reason": reason}
    # A reviewer waiting in the line is woken once per turn; this one needs its next call.
    train.turn += 1
    train.save()
    raise Waiting(f"{reason}; retesting next call")


def squash_proof(
    base: str,
    tree: str,
    evidence: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> str:
    """The `Workflow-Proof` line of a squash, or none where the repository writes no proof."""
    if RECEIPT is None:
        return ""
    sources = []
    for key, (run, parts) in evidence.items():
        workflow = key.split("_", 1)[0]
        if workflow not in ("ci", "e2e"):
            continue
        passed = sorted(passed_jobs(parts))
        if workflow == "ci":
            # The web job can pass with only lint, or without its story tests. Main needs both.
            if (NOTES.web_suite, "whole") not in _planner_notes(parts, CI_PLAN):
                passed = [name for name in passed if name != "CI: web"]
        sources.append({"workflow": workflow, "run": run["id"], "jobs": passed})
    return RECEIPT + json.dumps(
        {"base": base, "tree": tree, "sources": sources}, separators=(",", ":")
    )


def commit_list(number: str) -> str:
    """GitHub's own squash body, the branch's commit messages, which a body file replaces.

    Main's history keeps them; when they cannot be read, the squash carries the proof alone."""
    try:
        done = read_github("pr", "view", number, "-R", REPOSITORY, "--json", "commits")
        commits = json.loads(done.stdout)["commits"] if not done.returncode else []
        return "\n\n".join(
            "* " + "\n\n".join(part for part in (c["messageHeadline"], c["messageBody"]) if part)
            for c in commits
        )
    except (Waiting, ValueError, KeyError, TypeError) as error:
        print(f"could not read the commits of pull request {number}: {error}", flush=True)
        return ""


@contextmanager
def squash_body(number: str, proof: str) -> Iterator[str]:
    listed = commit_list(number)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt") as body:
        body.write("\n\n".join(part for part in (listed, proof) if part) + "\n")
        body.flush()
        yield body.name


def _batch_squash(number: str, head: str, base: str, proof: str) -> str | None:
    """Squash one member: None once GitHub shows it merged, otherwise why it did not."""
    reason = ""
    moved = f"main moved before batch member {number} merged; it needs fresh tests"
    for attempt in range(1, SQUASH_ATTEMPTS + 1):
        # A retry follows a wait, during which main may have moved to an untested tree.
        if attempt > 1 and git("ls-remote", "origin", "refs/heads/main").split()[0] != base:
            return moved
        with squash_body(number, proof) as body:
            done = command(
                "gh",
                "pr",
                "merge",
                number,
                "-R",
                REPOSITORY,
                "--body-file",
                body,
                "--squash",
                "--match-head-commit",
                head,
                check=False,
            )
        if not done.returncode or read_pull(number)["state"] == "MERGED":
            return None
        reason = done.stderr.strip() or done.stdout.strip()
        print(f"squash of {number} refused (attempt {attempt}): {reason}", flush=True)
        if git("ls-remote", "origin", "refs/heads/main").split()[0] != base:
            return moved
        if attempt < SQUASH_ATTEMPTS:
            # GitHub refuses a merge for a short while after its base moved.
            command("sleep", str(SQUASH_RETRY_SECONDS))
    return f"the squash of batch member {number} failed: {reason}"


def _batch_report(train: Train, merged: list[str], acting: str, message: str) -> int:
    others = [number for number in merged if number != acting]
    print(
        f"merge train: pull request {acting} merged in a batch "
        f"(merged in this call: {', '.join(merged)}); {message}",
        flush=True,
    )
    if others:
        print(
            "the tick tells the sessions of "
            + ", ".join(others)
            + " that their pull requests merged, and they do their own after-merge duties; "
            f"do the after-merge duties of {acting} only",
            flush=True,
        )
    if train.queue:
        print("still in the line: " + ", ".join(train.line()), flush=True)
    return 0


def _sweep_scratch(folder: Path) -> None:
    """Delete scratch branches that no saved batch, isolated failure awaiting a rerun or whole
    browser suite run names, each once its runs still queued or running are cancelled. A branch
    whose runs cannot be read for now, or whose cancel is lost to an outage, stops the sweep and
    is kept, with those after it, for the next; one that keeps failing is kept and passed over.

    GitHub lists a dispatched run only some time after it accepted the dispatch: the runs of a
    dropped branch are looked for, and cancelled, until one of each workflow dispatched on it
    has been seen, or DROPPED_RUNS_SECONDS after the drop, when the gate gives up and says so."""
    try:
        train = Train(folder)
        before = json.dumps(train.dropped, sort_keys=True)
        kept = set(train.scratch_records())
        listed = git("ls-remote", "origin", f"refs/heads/{SCRATCH_PREFIX}*")
        branches = [line.split()[1].removeprefix("refs/heads/") for line in listed.splitlines()]
    except (Refused, OSError, KeyError, IndexError, TypeError, ValueError) as error:
        print(f"could not delete old batch branches: {error}", flush=True)
        return
    watched = {entry["branch"]: entry for entry in train.dropped}
    try:
        for branch in branches:
            if branch in kept:
                continue
            try:
                # A dropped prefix's runs would hold runners ahead of the live prefixes' runs.
                found = _scratch_runs(branch)
                _cancel(found, folder, f"scratch branch {branch}")
                if branch in watched:
                    _seen(watched[branch], found)
                deleted = push("origin", "--delete", f"refs/heads/{branch}")
                if deleted.returncode:
                    raise Refused(deleted.refusal)
            except Waiting:
                raise
            except Refused as error:
                print(f"could not delete old batch branch {branch}: {error}", flush=True)
        for entry in list(train.dropped):
            branch = entry["branch"]
            try:
                if branch in kept:
                    train.dropped.remove(entry)
                    continue
                if branch not in branches:
                    # Past the deadline too: one last look before giving up.
                    found = _scratch_runs(branch)
                    _cancel(found, folder, f"scratch branch {branch}")
                    _seen(entry, found)
                missing = sorted(set(entry["requested"]) - set(entry.get("seen", [])))
                if not missing:
                    train.dropped.remove(entry)
                elif time.time() - entry["dropped"] > DROPPED_RUNS_SECONDS:
                    wake_log(
                        f"gave up looking for the {', '.join(missing)} run of dropped scratch "
                        f"branch {branch}: GitHub listed none within "
                        f"{DROPPED_RUNS_SECONDS // 60} minutes"
                    )
                    train.dropped.remove(entry)
            except Waiting:
                raise
            except Refused as error:
                print(f"could not look for the runs of {branch}: {error}", flush=True)
    except (Refused, OSError, KeyError, IndexError, TypeError, ValueError) as error:
        print(f"could not delete old batch branches: {error}", flush=True)
    finally:
        if json.dumps(train.dropped, sort_keys=True) != before:
            try:
                train.save()
            except OSError as error:
                print(f"could not save the dropped scratch branches: {error}", flush=True)


def _seen(entry: dict[str, Any], found: list[dict[str, Any]]) -> None:
    """Note in a dropped branch's entry each dispatched workflow a run of which GitHub listed:
    each was cancelled, or had finished."""
    files = {
        WORKFLOW_FILES[workflow]: workflow
        for workflow in entry["requested"]
        if workflow in WORKFLOW_FILES
    }
    seen = set(entry.get("seen", []))
    seen |= {
        files[name]
        for run in found
        if run.get("event") == "workflow_dispatch"
        and (name := str(run.get("path", "")).rsplit("/", 1)[-1]) in files
    }
    entry["seen"] = sorted(seen)


def _scratch_runs(branch: str) -> list[dict[str, Any]]:
    """Every run of a scratch branch the gate made, across every workflow, read fresh from GitHub.

    A branch of another name in the gate's namespace, and a pull request's own run, are never
    the gate's to cancel."""
    if not re.fullmatch(rf"{re.escape(SCRATCH_PREFIX)}[0-9a-f]{{32}}", branch):
        return []
    return [
        run
        for run in _api(f"{API}/actions/runs?branch={branch}&per_page=100", "workflow_runs")
        if run.get("head_branch") == branch and run.get("event") != "pull_request"
    ]


@contextmanager
def _sweeping(folder: Path) -> Generator[None]:
    try:
        yield
    finally:
        _sweep_scratch(folder)


def _push_scratch(
    train: Train, record: dict[str, Any], whose: str, drop: Callable[[], None]
) -> None:
    """Push a batch's, or a whole suite's, test merge to its scratch branch once."""
    if record["pushed"]:
        return
    failure: str | None = None
    output = ""
    try:
        pushed = push("origin", f"{record['head']}:refs/heads/{record['branch']}")
        if pushed.returncode:
            failure = pushed.refusal
            output = pushed.output
    except Waiting:
        raise
    except Refused as error:
        # A push that outlasted its deadline failed like any other.
        failure = str(error)
    if failure is not None:
        reason = f"could not push {whose} scratch branch: " + failure
        # The locked memory returns when another session's scan ends, so a shortage
        # never ends the batch; a hook refusing for another reason, or a stall, does.
        if not short_of_memory(output):
            record["push_failures"] = record.get("push_failures", 0) + 1
            if record["push_failures"] >= BATCH_PUSH_ATTEMPTS:
                drop()
                raise Refused(f"{reason} ({BATCH_PUSH_ATTEMPTS} calls in a row)")
        train.save()
        # The members are unchanged and still approved: the batch is pushed next call.
        raise Waiting(reason)
    record["pushed"] = True
    train.save()


def _dispatch_scratch(train: Train, record: dict[str, Any], workflow: str) -> None:
    if workflow in record["requested"]:
        return
    if _batch_runs(record, workflow):
        record["requested"].append(workflow)
        train.save()
        return
    # Save only an accepted dispatch. If interrupted around the request, the
    # lookup above recovers an observable run; a never-sent request can retry.
    # Until GitHub answers, the workflow is noted as being dispatched: dropped meanwhile, the
    # record's branch is still watched for its run (_sweep_scratch).
    record["dispatching"] = sorted({*record.get("dispatching", []), workflow})
    train.save()
    dispatched = command(
        "gh",
        "workflow",
        "run",
        WORKFLOW_FILES[workflow],
        "-R",
        REPOSITORY,
        "--ref",
        record["branch"],
        check=False,
    )
    record["dispatching"].remove(workflow)
    if dispatched.returncode:
        train.save()
        raise Refused(f"batch {workflow} dispatch failed: {dispatched.stderr.strip()}")
    record["requested"].append(workflow)
    train.save()


def _proves_on(scratch: dict[str, Any], base: str) -> bool:
    """Whether a green rerun of a prefix tested its member on what main is now: on the tree the
    prefix put it on. A batch saved before prefixes names the main it was tested on instead."""
    if "under" not in scratch:
        return scratch.get("base") == base
    if command("git", "cat-file", "-e", base, check=False).returncode:
        git("fetch", "origin", "main")
    return git("rev-parse", f"{base}^{{tree}}") == scratch["under"]


def _prefix_scratch(commit: str, base: str, under: str) -> dict[str, Any]:
    """A prefix's scratch branch, before it is pushed: `commit` is main and the members ahead,
    merged with this member; `under`, the tree it was put on."""
    workflows = ["ci"]
    if images_needed(commit, base, []):
        workflows.append("images")
    # Each member's own E2E run may have been a selection; a prefix runs the whole suite.
    if browser_needed(base, commit):
        workflows.append("e2e")
    return {
        "head": commit,
        "under": under,
        "branch": f"{SCRATCH_PREFIX}{uuid.uuid4().hex}",
        "workflows": workflows,
        "requested": [],
        "pushed": False,
    }


def _extend_line(train: Train, base: str, fresh: bool) -> None:
    """Add the next members of the line to the prefixes, up to the window, each merged onto the
    prefix ahead of it.

    With `fresh`, the front's own CI tested its merge with this main: its prefix needs no run,
    and it merges by its own checks on the single path. A front whose failed prefix passed when
    run again on this very main has that run as its prefix. A new line starts only where it tests
    a member that would otherwise wait: two members, a stale front while the window is one, or
    that passed rerun.
    """
    record = train.prefixes or {"base": base, "merged": 0, "members": []}
    members = record["members"]
    first = train.front()
    starting = not members
    if (
        first is None
        or first in train.blocked
        or len(members) >= train.window
        or (starting and len(train.queue) < 2 and first not in train.proven)
    ):
        return
    line = train.line()
    current = members[-1]["commit"] if members else base
    added: list[dict[str, Any]] = []
    reused: dict[str, Any] | None = None
    for number in line[line.index(members[-1]["number"] if members else first) + bool(members) :]:
        if len(members) + len(added) >= train.window:
            break
        if (
            number in train.blocked
            or number in train.suspended
            or number in train.conflicts
            or number in train.unavailable
            or number in train.pending
        ):
            continue
        pull = read_pull(number)
        head = pull["headRefOid"]
        if train.approved.get(number) != head:
            # An old queue entry or an unreviewed new head cannot authorize a squash.
            break
        if not _batch_ready(pull, base, settling=bool(record["merged"])):
            break
        tree = _merge_tree(current, head)
        if tree is not None and _taken_migrations(head, current):
            # Its own refresh renumbers a migration whose number main or an earlier member
            # took; merged here as it is, its prefix run would only find two migration heads.
            tree = None
        if tree is None:
            # A conflict with a member ahead keeps this pull request's place for its own turn.
            break
        under = git("rev-parse", f"{current}^{{tree}}")
        proven = train.proven.get(number)
        if (
            starting
            and not added
            and proven
            and proven["member"] == head
            and proven["under"] == under
            and git("rev-parse", f"{proven['head']}^{{tree}}") == tree
        ):
            # Its failed prefix passed when it was run again, on this very main.
            reused = proven
            current = proven["head"]
            scratch: dict[str, Any] | None = {
                key: value for key, value in proven.items() if key != "member"
            }
        else:
            current = git(
                "commit-tree",
                tree,
                "-p",
                current,
                "-p",
                head,
                "-m",
                f"chore: test merge train pull request {number}",
            )
            own = fresh and starting and not added and number == first
            scratch = None if own else _prefix_scratch(current, base, under)
        added.append(
            {"number": number, "head": head, "tree": tree, "commit": current, "scratch": scratch}
        )
    if starting and not (
        added
        and added[0]["number"] == first
        and (
            len(added) >= 2
            or (train.window == 1 and added[0]["scratch"])
            or reused is not None
        )
    ):
        return
    if added:
        if reused is not None:
            train.proven.pop(added[0]["number"], None)
        members.extend(added)
        train.prefixes = record
        train.save()


def _drop_line(train: Train, keep: int = 0) -> None:
    """Forget the prefixes from the `keep`th member on. Each holds every member ahead of it, so
    those behind a dropped one go with it; the sweep deletes their branches."""
    if train.prefixes is not None:
        del train.prefixes["members"][keep:]
        if not train.prefixes["members"]:
            train.prefixes = None
    train.save()


def _first_invalid(train: Train, record: dict[str, Any]) -> int:
    """The index of the first member whose prefix no longer stands, or how many there are.

    A prefix stands while its member waits in the line at its approved head, eligible, in the
    order the line serves them; the first must be the front."""
    line = train.line()
    present = []
    for member in record["members"]:
        if member["number"] not in line:
            break
        present.append(member["number"])
    served = sorted(present, key=line.index)
    for index, member in enumerate(record["members"]):
        number = member["number"]
        if (
            index >= len(present)
            or served[index] != number
            or (index == 0 and number != train.front())
            or train.approved.get(number) != member["head"]
            or number in train.blocked
            or number in train.suspended
            or number in train.conflicts
            or number in train.unavailable
        ):
            return index
        pull = read_pull(number)
        if pull["headRefOid"] != member["head"] or not ready(pull):
            return index
    return len(record["members"])


def _start_prefixes(train: Train) -> None:
    """Push each prefix to its scratch branch and dispatch its runs, each once.

    Only the first member's prefix decides this call: a later one that cannot start yet is
    tried again at the next call, and never keeps a member ahead of it from merging."""
    members = train.prefixes["members"] if train.prefixes else []
    for index, member in enumerate(list(members)):
        scratch = member["scratch"]
        if scratch is None:
            continue
        try:
            _push_scratch(
                train,
                scratch,
                f"pull request {member['number']}'s prefix",
                lambda index=index: _drop_line(train, index),
            )
            for workflow in scratch["workflows"]:
                _dispatch_scratch(train, scratch, workflow)
        except Refused as error:
            if index == 0:
                raise
            print(f"the prefix of pull request {member['number']} starts later: {error}", flush=True)
            return


def _refill(train: Train, base: str) -> None:
    """After a merge, the window has room: the next members' prefixes start on the new main.
    A new line starts only on a green main, as at a gate call. The merge is done; a prefix that
    cannot start yet starts at the next call."""
    try:
        if train.prefixes is not None and train.prefixes["base"] != base:
            _drop_line(train)
        if train.prefixes is None:
            current, health = refresh_health(train.path.parent)
            if current != base or health.state != "green":
                return
        _extend_line(train, base, False)
        _start_prefixes(train)
    except (Refused, KeyError, TypeError, ValueError, IndexError) as error:
        print(f"the next prefixes start at the next call: {error}", flush=True)


@dataclass
class Prefix:
    """How a member's prefix run stands: "green", "pending", "red" or "short" (passed without
    proving its tested files), with what to say and, when green, its runs."""

    state: str
    message: str
    evidence: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]
    workflow: str = ""
    run: dict[str, Any] | None = None


def _prefix_state(member: dict[str, Any], folder: Path) -> Prefix:
    scratch, number, tree = member["scratch"], member["number"], member["tree"]
    whose = f"pull request {number}'s prefix"
    evidence: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    pending: list[str] = []
    short: list[str] = []
    for workflow in scratch["workflows"]:
        found = _batch_runs(scratch, workflow)
        if not found:
            pending.append(f"waiting for {workflow} of {whose} to start")
            continue
        parts = jobs(found[0])
        state, failing = workflow_verdict(workflow, found[0], parts)
        if state == "red" and workflow == "ci":
            try:
                waits = batch_service_wait(found[0], parts, folder)
            except Waiting:
                raise
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence of the service proves no wait: a failure as before.
                waits = None
            if waits:
                pending.append(waits)
                continue
        if state == "red":
            return Prefix(
                "red",
                f"{workflow} run {found[0]['id']} of {whose} failed ({', '.join(failing)})",
                evidence,
                workflow,
                found[0],
            )
        if state != "green":
            pending.append(f"waiting for {workflow} checks of {whose}")
            continue
        # A manual run is every server test and the whole browser suite; the notes say so,
        # whatever a later CI plans, and they name the tested files.
        if not (
            whole_server_tests(found[0], parts, scratch["head"])
            if workflow == "ci"
            else whole_suite(found[0], parts, tree)
            if workflow == "e2e"
            else (NOTES.files_tested, tree) in _planner_notes(parts, IMAGES_PLAN)
        ):
            short.append(
                f"{whose} e2e did not prove the whole browser suite on its tested files"
                if workflow == "e2e"
                else f"{whose} {workflow} did not prove its tested files"
            )
        evidence[workflow] = found[0], parts
    if pending:
        return Prefix("pending", pending[0], evidence)
    remote = git("ls-remote", "origin", f"refs/heads/{scratch['branch']}").split()
    if not remote or remote[0] != scratch["head"]:
        short.append("the gate's scratch branch changed")
    if short:
        return Prefix("short", short[0], evidence)
    return Prefix("green", "", evidence)


def _run_line(
    train: Train, base: str, health: Health, folder: Path, acting: str, fresh: bool
) -> int | None:
    """Test the first members of the line at once, each as a prefix: main and every member
    ahead of it, on a scratch branch of its own. A member merges once its prefix passed and
    every member ahead of it merged. None leaves the call to the single path."""
    if train.prefixes is not None and train.prefixes["base"] != base:
        print("main moved outside the merge train; its prefixes are tested again", flush=True)
        _drop_line(train)
    if health.state == "red":
        # The fix of a red main merges by its own evidence, on the single path.
        if train.prefixes is not None:
            _drop_line(train)
        return None
    record = train.prefixes
    if health.state != "green" and not (record and record["merged"] and health.state == "pending"):
        if record is None:
            return None
        raise Waiting("merging stopped: " + health.line)
    if record is not None:
        valid = _first_invalid(train, record)
        if valid < len(record["members"]):
            print(
                f"merge train: the prefix of pull request {record['members'][valid]['number']} "
                "and those behind it are tested again",
                flush=True,
            )
            _drop_line(train, valid)
    _extend_line(train, base, fresh)
    if train.prefixes is None:
        return None
    _start_prefixes(train)
    merged: list[str] = []

    def stop(message: str, *, then: str = "wait") -> int | None:
        """End this call. Once the caller's own pull request merged, report that merge."""
        if acting in merged:
            return _batch_report(train, merged, acting, message)
        if then == "single":
            print(message, flush=True)
            return None
        if then == "refuse":
            raise Refused(message)
        return _batch_wait(message)

    try:
        return _merge_prefixes(train, folder, acting, merged, stop)
    except (Refused, KeyError, TypeError, ValueError, IndexError) as error:
        if acting in merged:
            return _batch_report(train, merged, acting, f"the merge train stopped: {error}")
        raise


def _prefix_failed(
    train: Train,
    member: dict[str, Any],
    found: Prefix,
    acting: str,
    stop: Callable[..., int | None],
) -> int | None:
    """Block the first member whose prefix run failed, as a failed check: every member ahead of
    it merged, so the failure is its own. The prefixes behind it hold its change and are built
    again from the new front, without it, and dispatched."""
    number, base = member["number"], train.prefixes["base"] if train.prefixes else ""
    assert found.run is not None
    train.batch_failed[number] = {
        "pull": _pull_run_token(member["head"]),
        "batch": member["scratch"],
        "workflow": found.workflow,
        "run": _run_token(found.run),
    }
    _drop_line(train)
    train.failed_prefix()
    train.fail(number, member["head"])
    # Its own checks stay green and unchanged, so no checks wake would come.
    train.turn += 1
    train.save()
    try:
        _extend_line(train, base, False)
        _start_prefixes(train)
    except (Refused, KeyError, TypeError, ValueError, IndexError) as error:
        print(f"the prefixes behind it start at the next call: {error}", flush=True)
    message = f"{found.message} on main and the members ahead of it"
    if number == acting:
        raise Refused(
            f"{message}; treat it as a failed check: fix and push, or rerun that run and call "
            "this gate again"
        )
    return stop(f"merge train: {message}; it is blocked as a failed check")


def _merge_prefixes(
    train: Train,
    folder: Path,
    acting: str,
    merged: list[str],
    stop: Callable[..., int | None],
) -> int | None:
    while train.prefixes is not None:
        record = train.prefixes
        member = record["members"][0]
        number, head, tree = member["number"], member["head"], member["tree"]
        if member["scratch"] is None:
            # Its own CI tested its merge with this main: it merges by its own checks.
            if number == acting:
                return None
            return stop(f"merge train: pull request {number} merges by its own checks first")
        found = _prefix_state(member, folder)
        if found.state == "pending":
            if merged:
                _refill(train, record["base"])
            return stop(f"merge train: {found.message}")
        if found.state == "red":
            return _prefix_failed(train, member, found, acting, stop)
        if found.state == "green" and train.retest:
            train.retest = None
            train.save()
        if found.state == "short":
            # Retested on a new scratch branch; the prefixes behind it hold the same commit.
            member["scratch"] = {
                **{key: member["scratch"][key] for key in ("head", "under", "workflows")},
                "branch": f"{SCRATCH_PREFIX}{uuid.uuid4().hex}",
                "requested": [],
                "pushed": False,
            }
            train.save()
            try:
                _retest(train, tree, found.message)
            except Refused as error:
                if acting in merged:
                    return stop(str(error))
                raise
        train.reconcile(acting=acting)
        pull = read_pull(number)
        current_base, current_health = refresh_health(folder)
        if current_base != record["base"]:
            _drop_line(train)
            return stop(
                "main moved during the merge train; the prefixes are tested again",
                then="refuse",
            )
        if current_health.state != "green" and (
            not record["merged"] or current_health.state != "pending"
        ):
            if current_health.state == "red":
                _drop_line(train)
                return stop(
                    "main is red; remaining members use the single fix rule",
                    then="single",
                )
            if acting in merged:
                return stop("merging stopped: " + current_health.line)
            raise Waiting("merging stopped: " + current_health.line)
        if (
            number != train.front()
            or train.approved.get(number) != head
            or pull["headRefOid"] != head
            or not _batch_ready(pull, current_base, settling=bool(record["merged"]))
        ):
            _drop_line(train)
            train.turn += 1
            train.save()
            return stop(f"merge train: prefix member {number} is no longer ready")
        if _merge_tree(current_base, head) != tree:
            _drop_line(train)
            return stop(
                f"the squash of {number} differs from its prefix; the members return to their "
                "own single gate path",
                then="single",
            )
        failure = _batch_squash(
            number,
            head,
            current_base,
            squash_proof(
                current_base,
                tree,
                {
                    workflow: (run, strict_jobs(parts) if workflow == "ci" else parts)
                    for workflow, (run, parts) in found.evidence.items()
                },
            ),
        )
        if failure:
            _drop_line(train)
            return stop(failure, then="refuse")
        # Recorded before anything else can fail. Another member's reviewer or audit ended
        # its turn waiting in the train; the tick reads this note and tells it to finish by
        # the after-merge rules. For the caller it keeps the merge known when a later step
        # of this call fails.
        merged.append(number)
        note = folder / f"batch-merged-{number}"
        note.write_text(json.dumps({"by": acting, "head": head}) + "\n")
        git("fetch", "origin", "main")
        actual = git("rev-parse", "origin/main")
        if context := train_reads.tick():
            train_reads.save(context / "main.json", {"head": actual})
        note.write_text(json.dumps({"by": acting, "head": head, "squash": actual}) + "\n")
        if (
            git("show", "-s", "--format=%P", actual) != current_base
            or git("rev-parse", f"{actual}^{{tree}}") != tree
        ):
            train.prefixes = None
            train.remove(number)
            return stop(
                "main did not match the prefix squash; waiting for main's own checks",
                then="refuse",
            )
        record["base"] = actual
        record["merged"] += 1
        record["members"].pop(0)
        if not record["members"]:
            train.prefixes = None
        train.merged_one()
        train.remove(number)
        ci, parts = found.evidence["ci"]
        image = found.evidence.get("images")
        try:
            # Main is exactly the tree this member's prefix tested.
            record_green(
                folder,
                current_base,
                tree,
                ci,
                strict_jobs(parts),
                [image[0]] if image else [],
                image[1] if image else [],
            )
        except (Refused, OSError) as error:
            print(f"could not record inherited prefix green: {error}", flush=True)
    if acting not in merged:
        # Unreachable while the caller is the first member; never claim its merge.
        return _batch_wait("merge train: prefixes merged " + ", ".join(merged))
    _refill(train, git("rev-parse", "origin/main"))
    return _batch_report(train, merged, acting, "every tested member merged")


def _advance_line(train: Train, number: str, base: str) -> None:
    """The single path squashed the first member: the prefixes behind it stand on the new main
    when main is exactly that squash, and their window has room for the next members.

    Its own CI may have skipped a job main always runs: the next squash waits for main's own
    checks as after any single merge, so this squash does not count among the line's merges."""
    train.merged_one()
    train.save()
    record = train.prefixes
    git("fetch", "origin", "main")
    actual = git("rev-parse", "origin/main")
    if context := train_reads.tick():
        train_reads.save(context / "main.json", {"head": actual})
    if record is not None:
        if (
            record["base"] != base
            or record["members"][0]["number"] != number
            or git("show", "-s", "--format=%P", actual) != base
            or git("rev-parse", f"{actual}^{{tree}}") != record["members"][0]["tree"]
        ):
            _drop_line(train)
        else:
            record["members"].pop(0)
            record["base"] = actual
            if not record["members"]:
                train.prefixes = None
            train.save()
    _refill(train, actual)


def _suite_of(train: Train, number: str, base: str, head: str, tree: str) -> dict[str, Any] | None:
    suite = train.suites.get(number)
    if suite and [suite["base"], suite["member"], suite["tree"]] == [base, head, tree]:
        return suite
    return None


def _whole_suite_run(
    train: Train, number: str, base: str, head: str, tree: str, needed: list[str]
) -> int | None:
    """Run every browser test, every server test, or both, on the tested merge of a pull request
    that merges alone.

    Its own E2E and CI runs may have tested only the specs and the server test files its change
    reaches. As for a batch, the gate pushes a commit of the tested files to a scratch branch
    and dispatches the `needed` workflows there; a manual run is the whole suite. None once
    each of those runs passed on this very tree.
    """
    suite = _suite_of(train, number, base, head, tree)
    if suite is None:
        suite = {
            "base": base,
            "member": head,
            "tree": tree,
            "head": git(
                "commit-tree",
                tree,
                "-p",
                base,
                "-p",
                head,
                "-m",
                f"chore: whole suite for pull request {number}",
            ),
            "branch": f"{SCRATCH_PREFIX}{uuid.uuid4().hex}",
            "workflows": [],
            "requested": [],
            "pushed": False,
        }
        train.suites[number] = suite
    # A record saved before it named its workflows ran the browser suite.
    suite["workflows"] = sorted(set(_suite_workflows(suite)) | set(needed))
    train.save()

    def drop() -> None:
        train.suites.pop(number, None)
        train.save()

    _push_scratch(train, suite, "the whole suite's", drop)
    for workflow in suite["workflows"]:
        _dispatch_scratch(train, suite, workflow)
    for workflow in suite["workflows"]:
        found = _batch_runs(suite, workflow)
        if not found:
            return _batch_wait(f"merge train: waiting for {WHOLE[workflow]} to start")
        parts = jobs(found[0])
        state, failing = workflow_verdict(workflow, found[0], parts)
        if state == "red" and workflow == "ci":
            # The same package audit as a batch's: a run red only because it could not reach
            # an advisory service waits, and is re-run once the service answers.
            try:
                waits = _service_wait(
                    found[0],
                    parts,
                    train.path.parent,
                    f"run {found[0]['id']} of {WHOLE[workflow]}",
                    "batch-rerun",
                    "by the gate call at the front of the train",
                    retry=True,
                )
            except Waiting:
                raise
            except Refused, KeyError, TypeError, ValueError, IndexError:
                # Unreadable evidence of the service proves no wait: refused as before.
                waits = None
            if waits:
                return _batch_wait(f"merge train: {waits}")
        if state == "red":
            # Blocked like a failed check, and kept: a rerun of this same run, once green,
            # restores its place and lets the next call merge.
            train.fail(number, head)
            raise Refused(
                f"{WHOLE[workflow]}, run {found[0]['id']}, failed on this pull request's "
                f"tested merge ({', '.join(failing)}); treat it as a failed check: fix and push, "
                "or rerun that run and call this gate again"
            )
        if state != "green":
            return _batch_wait(f"merge train: waiting for {WHOLE[workflow]} on the tested merge")
        suite.setdefault("passed", {})[workflow] = found[0]["id"]
        train.save()
        if not _proves_whole(workflow, found[0], parts, suite):
            drop()
            _retest(
                train,
                tree,
                f"the run of {WHOLE[workflow]} did not prove its tested files",
            )
    if train.retest:
        train.retest = None
        train.save()
    return None


def _prefix_summary(scratch: dict[str, Any] | None) -> str:
    """A prefix's state in a word, from its runs as last read: "own checks" for a first member
    that merges by its own CI, then "not pushed", "starting", "running", "failed" or "passed"."""
    if scratch is None:
        return "own checks"
    if not scratch["pushed"]:
        return "not pushed"
    seen = {run[0]: run for run in scratch.get("signal", []) if len(run) == 5}
    if any(run[3] == "completed" and run[4] != "success" for run in seen.values()):
        return "failed"
    if set(scratch["workflows"]) - set(seen):
        return "starting"
    if all(run[3] == "completed" for run in seen.values()):
        return "passed"
    return "running"


def train_status(folder: Path) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "merge.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            # A gate holds the lock, for a refresh through its push hooks. The tick reads the
            # last saved line, written whole, and a later tick reconciles it.
            locked = False
        train = Train(folder)
        if locked:
            train.reconcile()
            if train_reads.tick() is None:
                _batch_signal(train)
        print(
            json.dumps(
                {
                    "first": train.first,
                    # Keep failed entries' slots internally, but let the tick deliver their
                    # ordinary checks-finished wake instead of waiting for a train turn.
                    "waiting": [
                        number
                        for number in train.line()
                        if number not in train.blocked or number in train.refreshable
                    ],
                    "blocked": list(train.blocked),
                    # The priority each is served at: a proven fix of red main is a blocker.
                    "priorities": {
                        number: "blocker"
                        if number in train.fixes
                        else train.priorities.get(number, priority.UNLABELLED)
                        for number in train.line()
                    },
                    "turn": train.turn,
                    # A check GitHub lost on a member's head: the tick wakes its reviewer.
                    "lost": train.lost,
                    # How many members are tested at once, and each one's prefix with its runs
                    # as last read: a first member that merges by its own checks has none.
                    "window": train.window,
                    "prefixes": [
                        {
                            "number": member["number"],
                            "branch": member["scratch"]["branch"] if member["scratch"] else None,
                            "state": _prefix_summary(member["scratch"]),
                            "runs": [
                                {
                                    "workflow": run[0],
                                    "id": run[1],
                                    "status": run[3],
                                    "conclusion": run[4],
                                }
                                for run in (member["scratch"] or {}).get("signal", [])
                                if len(run) == 5
                            ],
                        }
                        for member in (train.prefixes["members"] if train.prefixes else [])
                    ],
                }
            )
        )
    return 0


def pre_train_checks(number: str, folder: Path, *, locked: bool = True) -> None:
    """A failed local inventory must neither join nor stay approved for a prefix.

    A repository that holds no `pre-train-checks` beside the gate has none to fail. A caller
    that does not hold the merge lock (a handoff) takes it only to leave the line."""
    checks = HERE / "pre-train-checks"
    if not checks.exists():
        return
    done = command("bash", str(checks), check=False)
    print(done.stdout, end="", flush=True)
    print(done.stderr, end="", file=sys.stderr, flush=True)
    if done.returncode:
        with nullcontext() if locked else handoff_lock(folder, number):
            train = Train(folder)
            members = train.prefixes["members"] if train.prefixes else []
            for index, member in enumerate(members):
                if member["number"] == number:
                    _drop_line(train, index)
                    break
            train.remove(number)
            Handoff.path(folder, number).unlink(missing_ok=True)
        raise Refused("pre-train checks failed; fix the inventory before joining the merge train")


@contextmanager
def handoff_lock(folder: Path, number: str) -> Iterator[None]:
    """The merge lock, held by a handoff: how long it held it goes to the wake log."""
    with (folder / "merge.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        taken = time.monotonic()
        try:
            yield
        finally:
            held = time.monotonic() - taken
            try:
                wake_log(f"handoff {number} held the merge lock for {held:.1f} s")
            except OSError as error:
                print(f"could not log the handoff's hold of the merge lock: {error}", flush=True)


def _confirm_head(branch: str, head: str) -> None:
    """Under the merge lock: the branch's head on GitHub is still `head`. One try of git, bounded
    by HEAD_CHECK_TIMEOUT, with no gh call: it waits for neither GitHub's budget nor a call slot."""
    ref = f"refs/heads/{branch}"
    try:
        done = run_command("git", "ls-remote", "--heads", "origin", ref, timeout=HEAD_CHECK_TIMEOUT)
    except CommandTimeout:
        raise Refused("could not confirm the head; try again") from None
    if done.returncode:
        raise Refused("could not confirm the head; try again")
    # A pattern matches the end of a ref's name: only the branch's own line counts.
    if [line.split() for line in done.stdout.splitlines() if line.endswith(f"\t{ref}")] != [
        [head, ref]
    ]:
        raise Refused("the pull request head changed during the handoff; review it again")


def _failing(pull: dict[str, Any]) -> set[str]:
    return {check.get("name", check.get("context", "unknown")) for check in _failed_rollup(pull)}


def handoff(number: str, folder: Path) -> int:
    """A published passed round is required before the reviewer can leave.

    Its own pull request is checked, and its place in the line judged, before the merge lock is
    taken. Under the lock the handoff only confirms its head with git, joins the line and saves
    its note: the other members' order is the tick's to read, at the turn of the line.
    """
    folder.mkdir(parents=True, exist_ok=True)
    pull = read_pull(number)
    head = pull["headRefOid"]
    # Checks failed only for an unreachable advisory service: the tick waits and re-runs.
    if (
        not ready(pull)
        or conflicting(pull)
        or (failed_checks(pull) and not pull_service_wait(pull, folder, number))
    ):
        raise Refused("only a ready, non-conflicting pull request can be handed off")
    if git("rev-parse", "HEAD") != head or git("status", "--porcelain"):
        raise Refused("handoff requires the clean published pull request worktree")
    if git("branch", "--show-current") != pull["headRefName"]:
        raise Refused("handoff requires the pull request branch")
    reviews = api(f"{API}/pulls/{number}/reviews")
    rounds = []
    for review in reviews:
        match = re.match(
            r"^<!-- workflow-review round=([0-9]+)(?: result=(sent-back|passed))? -->",
            review.get("body") or "",
        )
        if match:
            rounds.append((int(match[1]), match[2], review.get("commit_id")))
    latest = max(enumerate(rounds), key=lambda r: (r[1][0], r[0]))[1] if rounds else None
    if latest is None or latest[1:] != ("passed", head):
        raise Refused("the latest independent round must have passed on the current head")
    from conflict_review import Scope, passed

    current = next(
        review
        for review in reversed(reviews)
        if review.get("commit_id") == head
        and (review.get("body") or "").startswith(
            f"<!-- workflow-review round={latest[0]} result=passed -->"
        )
    )
    conflict = re.search(
        r"^<!-- workflow-conflict extends=([0-9a-f]{40}) -->$",
        current["body"],
        re.MULTILINE,
    )
    if "<!-- workflow-conflict" in current["body"] and conflict is None:
        raise Refused("conflict round needs full review: invalid conflict marker")
    if conflict:
        try:
            scope = Scope.read(folder, number)
            if (
                scope is None
                or scope.head != conflict[1]
                or passed(reviews, scope.head) is None
            ):
                raise ValueError("conflict round lacks its scope or earlier passed round")
            scope.validate(Path.cwd(), head)
        except (ValueError, OSError, TypeError) as error:
            raise Refused(f"conflict round needs full review: {error}") from error
    pre_train_checks(number, folder, locked=False)
    note = Handoff(head, max(r[0] for r in rounds), pull["headRefName"])
    checked = pull
    attempts = waits = 0
    view: OwnView | None = None
    while True:
        if view is None:
            if attempts == HANDOFF_ATTEMPTS:
                raise Waiting(
                    "the merge line kept changing this pull request's place; run the handoff again"
                )
            attempts += 1
            if pull is not checked:
                # A check failed since the first read: judged as the first read was.
                if failed_checks(pull) and not pull_service_wait(pull, folder, number):
                    raise Refused("only a ready, non-conflicting pull request can be handed off")
                checked = pull
            view = OwnView(folder, number)
            before = view.member(number)
            outcome: Refused | None = None
            try:
                view.claim(number, pull)
            except Refused as error:
                if not view.judged:
                    raise
                outcome = error
        # Read again just before the lock: GitHub's budget, its call slots and its retries are
        # waited for here, never while the lock is held.
        fresh = read_pull(number)
        if (fresh["headRefOid"], fresh["headRefName"]) != (head, pull["headRefName"]):
            raise Refused("the pull request head changed during the handoff; review it again")
        if not ready(fresh) or conflicting(fresh):
            raise Refused("only a ready, non-conflicting pull request can be handed off")
        if _failing(fresh) - _failing(pull):
            # A check failed meanwhile: judged again, as the first read was.
            pull, view = fresh, None
            continue
        read = time.monotonic()
        with handoff_lock(folder, number):
            if time.monotonic() - read > FRESH_READ_SECONDS:
                # Another holder kept the lock long enough for that read to be old: let it go
                # and read again.
                waits += 1
                if waits > LOCK_WAITS:
                    raise Waiting("the merge lock stayed busy; run the handoff again")
                continue
            _confirm_head(pull["headRefName"], head)
            train = Train(folder)
            if train.member(number) != before:
                # The tick or a gate changed its place meanwhile: judge it again.
                pull, view = fresh, None
                continue
            train.admit(view)
            # Queued and passed over in its place until the tick's re-run is green; a wait
            # before it was queued is no handoff.
            if outcome is not None and not (isinstance(outcome, Waiting) and number in train.queue):
                raise outcome
            note.save(folder, number)
        print(f"pull request {number} handed to the tick after round {note.round}")
        return 0


def merge(number: str, folder: Path, *, delegated: bool = False) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    # The sweep leaves first, while the lock is still held.
    with (folder / "merge.lock").open("w") as lock, _sweeping(folder):
        print("waiting for the merge lock", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        pull = read_pull(number)
        note = folder / f"batch-merged-{number}"
        if pull["state"] == "MERGED" and note.exists():
            by = json.loads(note.read_text())["by"]
            call = (
                "an earlier gate call of its own"
                if by == number
                else f"the gate call of pull request {by}"
            )
            print(
                f"merge train: pull request {number} already merged in a batch, through "
                f"{call}. Do not call the gate again; do its after-merge duties and finish.",
                flush=True,
            )
            return 0
        if delegated:
            if not authorized(folder, number, pull["headRefOid"]):
                raise Refused("the head changed since the passed review handoff")
            if git("rev-parse", "HEAD") != pull["headRefOid"] or git("status", "--porcelain"):
                raise Refused("the handed-off worktree is no longer clean at the reviewed head")
            if git("branch", "--show-current") != pull["headRefName"]:
                raise Refused("the handed-off worktree is on a different branch")
        if not ready(pull):
            raise Refused("only an open, ready pull request based on main can merge")
        pre_train_checks(number, folder)
        # A direct retry can observe recovery before the next tick. Reconcile suspension
        # against freshly read main health, rather than the previous gate's report.
        base, health = refresh_health(folder)
        print(health.line, flush=True)
        train = Train(folder)
        claimed = train.claim(number, pull)
        # A conflicting caller still needs the existing refresh path to merge main in,
        # even though reconciliation lets another pull request take the train turn.
        if not claimed and not conflicting(pull):
            reason = f"behind {train.first}" if train.first else "for main to change"
            print(
                f"merge train: pull request {number} waits {reason}; "
                "run scripts/workflow/notify waiting and end the turn; the tick wakes you",
                flush=True,
            )
            command("bash", str(HERE / "notify"), "waiting")
            return 3
        # A pending or red main still lets a stale pull request refresh, so its CI runs on the
        # current main; the merge itself waits for green, or for proof of a fix, below.
        if health.state not in ("green", "pending", "red"):
            # An unreadable main stops merging until it can be read; it needs no judgment.
            raise Waiting("merging stopped: " + health.line)
        head = pull["headRefOid"]
        if command("git", "cat-file", "-e", head, check=False).returncode:
            git("fetch", "origin", head)
        if conflicting(pull):
            return refresh_pull(pull, base, train, number, mechanical=delegated)
        if pull["mergeable"] != "MERGEABLE":
            raise Waiting("GitHub has not determined whether this pull request is mergeable")
        ci = runs("ci", head, pull=True)
        if not ci:
            raise Waiting(f"{CI_RESULT} has not started for the current pull request head")
        parts = jobs(ci[0])
        ci_state = verdict(ci[0], parts)[0]
        if ci_state != "green":
            if ci_state == "red":
                train.fail(number, head)
                waits = pull_service_wait(pull, folder, number, retry=True)
                if waits:
                    raise Waiting(waits)
                raise Refused("the latest CI run for this pull request failed")
            raise Waiting("the latest CI run for this pull request is not green")
        merge_sha = tested_merge(parts, base, head)
        tree = api(f"{API}/git/commits/{merge_sha}")["tree"]["sha"] if merge_sha else ""
        # Its own E2E and CI runs may have tested only the specs and the server tests its
        # change reaches: before it merges, the whole suite of each runs on the tested merge,
        # alone or in a batch. The local merge of the same commits names the changed files, and
        # is the commit a scratch branch would test.
        needed: list[str] = []
        if merge_sha:
            local = _merge_tree(base, head)
            if server_tests_selected(parts):
                needed.append("ci")
            # A local conflict GitHub does not see names no changed files: it needs proof too.
            if local is None or browser_needed(base, local):
                own = runs("e2e", head, pull=True)[:1]
                if not (own and whole_suite(own[0], jobs(own[0]), tree)):
                    needed.append("e2e")
            if needed and local != tree:
                raise Refused(
                    "a local merge of this pull request differs from the merge CI tested; "
                    f"{' and '.join(WHOLE[workflow] for workflow in needed)} cannot be run on "
                    "the tested files"
                )
        # The first members of the line are tested at once, each as a prefix on a scratch
        # branch. A fresh first member merges by its own checks meanwhile, below; on a red
        # main, its fix does too, judged by its own proof.
        lined = _run_line(train, base, health, folder, number, merge_sha is not None)
        if lined is not None:
            return lined
        if merge_sha is None:
            return refresh_pull(pull, base, train, number)
        strict = strict_jobs(parts)
        passed_checks = {job["name"] for job in strict if job.get("conclusion") == "success"}
        images = runs("images", head, pull=True)
        if images_needed(head, base, pull["statusCheckRollup"]) and not images:
            raise Waiting(f"{IMAGES} checks have not started for this pull request head")
        image_jobs: list[dict[str, Any]] = []
        if images:
            image_jobs = jobs(images[0])
            image_state = verdict(images[0], image_jobs, images=True)[0]
            if image_state != "green":
                if image_state == "red":
                    train.fail(number, head)
                    raise Refused(f"the latest {IMAGES} run for this pull request failed")
                raise Waiting(f"the latest {IMAGES} run for this pull request is not green")
            image_jobs = inherited_image_jobs(image_jobs, tree, pull["headRefName"])
            # Images record the checked-out tree already. Match it to the tested CI merge.
            passed_checks |= {
                job["name"] for job in image_jobs if job.get("conclusion") == "success"
            }
            image_plan = next(
                job for job in image_jobs if job["name"] == IMAGES_PLAN
            )
            image_notes = api(f"{API}/check-runs/{image_plan['id']}/annotations?per_page=100")
            if not any(
                note.get("title") == NOTES.files_tested and note["message"].strip() == tree
                for note in image_notes
            ):
                return refresh_pull(pull, base, train, number)
        for check in pull["statusCheckRollup"]:
            if not _passing_check(check):
                if failed_checks(pull):
                    raise Refused(
                        f"check is not passing: {check.get('name', check.get('context', 'unknown'))}"
                    )
                lost = lost_check(pull, [ci[0], *images])
                if lost:
                    return rerun_lost(pull, train, number, lost)
                raise Waiting(
                    f"check is not passing: {check.get('name', check.get('context', 'unknown'))}"
                )
        if needed:
            waited = _whole_suite_run(train, number, base, head, tree, needed)
            if waited is not None:
                return waited
        # From here a selected "CI: server tests" stands for every server test: they passed on this
        # very tree, so it may prove the fix of a red main and be inherited by main.
        # Catch a main push outside cooperating sessions while the checks were being read.
        current_base, current_health = refresh_health(folder)
        # A red main merges only the fix: this pull request's CI on that same main ran and
        # passed every check failing there. A skipped check proves nothing.
        fixes = current_health.state == "red" and set(current_health.failing) <= passed_checks
        if current_health.state != "green" and not fixes:
            if current_health.state == "red":
                # Sleep until main or this head changes; a later arrival may be the fix.
                train.suspend(number, head, current_health.line)
            raise Waiting("merging stopped: " + current_health.line)
        if current_base != base:
            raise Waiting("main moved during the freshness check; run this gate again")
        evidence = {"ci": (ci[0], strict)}
        suite = train.suites.get(number, {})
        for workflow, run_id in suite.get("passed", {}).items():
            run = {"id": run_id}
            evidence["ci_suite" if workflow == "ci" else workflow] = (
                run,
                strict_jobs(jobs(run)) if workflow == "ci" else jobs(run),
            )
        if "e2e" not in evidence and browser_needed(base, tree):
            own = runs("e2e", head, pull=True)[:1]
            if own and whole_suite(own[0], jobs(own[0]), tree):
                evidence["e2e"] = own[0], jobs(own[0])
        with squash_body(number, squash_proof(base, tree, evidence)) as body:
            done = command(
                "gh",
                "pr",
                "merge",
                number,
                "-R",
                REPOSITORY,
                "--body-file",
                body,
                "--squash",
                "--match-head-commit",
                head,
            )
        train.remove(number)
        try:
            record_green(folder, base, tree, ci[0], strict, images, image_jobs)
        except (Refused, OSError) as error:
            # The squash succeeded. Losing a receipt only makes the next turn wait.
            print(f"could not record inherited green: {error}", flush=True)
        try:
            _advance_line(train, number, base)
        except (Refused, OSError, KeyError, IndexError) as error:
            # The squash succeeded. The prefixes behind it are tested again at the next call.
            print(f"could not move the prefixes onto the new main: {error}", flush=True)
            _drop_line(train)
        if fixes:
            print(
                "main is red; this pull request passed every check failing on main: "
                + ", ".join(current_health.failing),
                flush=True,
            )
        print(done.stdout.strip(), flush=True)
        return 0


def main() -> int:
    install_signal_handlers()
    delegated = len(sys.argv) == 3 and sys.argv[1] == "--drive" and sys.argv[2].isdigit()
    handing_off = len(sys.argv) == 3 and sys.argv[1] == "--handoff" and sys.argv[2].isdigit()
    if not (delegated or handing_off) and (
        len(sys.argv) != 2
        or (
            sys.argv[1] not in ("--main-status", "--tick-main-status", "--train-status")
            and not sys.argv[1].isdigit()
        )
    ):
        print(
            "usage: merge-when-current N | --handoff N | --drive N | --main-status | --train-status",
            file=sys.stderr,
        )
        return 1
    try:
        folder = main_checkout() / CONFIG.state_folder
        if handing_off:
            return handoff(sys.argv[2], folder)
        if delegated:
            return merge(sys.argv[2], folder, delegated=True)
        if sys.argv[1] in ("--main-status", "--tick-main-status"):
            _, health = refresh_health(folder, retry=sys.argv[1] == "--tick-main-status")
            print(health.line)
            return 0
        if sys.argv[1] == "--train-status":
            return train_status(folder)
        return merge(sys.argv[1], folder)
    except PushWaiting as error:
        # Nothing wakes a reviewer when the service answers or the packages install; the tick
        # retries a handoff's push.
        later = (
            "the tick tries the push again on a later turn"
            if delegated
            else "it is tried again by the tick once it is handed off: publish the passed "
            f"round, run merge-when-current --handoff {sys.argv[1]} and end with review finished"
        )
        print(f"merge waiting: {error}; {later}", file=sys.stderr)
        return 3 if delegated else 1
    except Waiting as error:
        print(f"merge waiting: {error}", file=sys.stderr)
        return 3 if delegated else 1
    except (Refused, KeyError, TypeError, ValueError, StopIteration, OSError) as error:
        print(f"merge refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
