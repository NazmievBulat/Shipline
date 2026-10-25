"""One machine-wide merge gate. Only the standard library and git/gh are needed."""

import fcntl
import hashlib
import json
import os
import re
import shlex
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
from typing import Any, NoReturn
from urllib.parse import quote

import check_repair
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

# The files a repository keeps for the gate are in its own `scripts/workflow`, in its main
# checkout: the repository Shipline works on, not the checkout these scripts are in. Searched last,
# so none of them stands in for one of the scripts or for a module of Python's own.
REPOSITORY_FILES = workflow_config.current().main / "scripts" / "workflow"
sys.path.append(str(REPOSITORY_FILES))

# A repository may hold one helper there, and the gate uses it when it does: `run_proof` reads
# and writes the `Workflow-Proof` line with which main skips a job its squash proved. Without it
# the proof is Shipline's own (`ci_proof`), the one its example CI reads. A helper that is there
# but cannot start stops the gate. The repository's other parts are commands its `shipline.json`
# names (`repository_command`).
try:
    from run_proof import RECEIPT, passed_jobs
    from run_proof import WHOLE as WHOLE_REQUEST
except ModuleNotFoundError as error:
    if error.name != "run_proof":
        raise
    from ci_proof import RECEIPT, passed_jobs
    from ci_proof import WHOLE as WHOLE_REQUEST


# How many of its last lines a failed push is refused with when the repository words no refusal;
# a push that outlasted its deadline keeps twice as many.
REFUSAL_LINES = 10
TIMEOUT_REFUSAL_LINES = 20

CONFIG = workflow_config.current()
REPOSITORY = CONFIG.repository
API = CONFIG.api
HERE = Path(__file__).resolve().parent
# How a line names `notify waiting`: by the launcher's absolute path, which a session started
# before the switch, with neither on its PATH, runs as well; a copy of the scripts in a
# repository's tree, which has no launcher, by its path there.
LAUNCHER = HERE.parents[1] / "bin" / "shipline"
NOTIFY_WAITING = (
    f"{shlex.quote(str(LAUNCHER))} notify waiting"
    if LAUNCHER.is_file()
    else "scripts/workflow/notify waiting"
)
# The gate owns these branches: each holds one batch's test merge, never a pull request.
SCRATCH_PREFIX = CONFIG.merge.scratch_prefix
E2E_PLAN = CONFIG.checks.e2e_plan
CI_PLAN = CONFIG.checks.ci_plan
CI_RESULT = CONFIG.checks.ci_result
# The word or words the name of every check of the CI workflow begins with: those its planner
# and its result begin with both, "CI" by default.
CI_PREFIX = (
    " ".join(os.path.commonprefix([CI_PLAN.replace(":", " ").split(), CI_RESULT.split()]))
    or CI_RESULT
)
# How long after GitHub listed no CI run for a head whose CI jobs finished it is asked again.
UNLISTED_RETRY_SECONDS = 30
# Every check of the image workflow begins with this; its planner is IMAGES_PLAN.
IMAGES = CONFIG.checks.images
IMAGES_PLAN = CONFIG.checks.images_plan
# The titles of the planners' annotations.
NOTES = CONFIG.annotations
# The job that audits the dependencies, and the title of the notice it writes on main when only
# advisories failed it. With no notice named, an advisory on main makes main red as before.
DEPENDENCIES = CONFIG.checks.dependency_audit or "CI: dependencies"
ADVISORIES_ONLY = CONFIG.checks.advisory_annotation
# The workflow files, by the gate's own names for them.
WORKFLOW_FILES = {
    "ci": CONFIG.workflows.ci,
    "e2e": CONFIG.workflows.e2e,
    "images": CONFIG.workflows.images,
}
# What a dispatched whole-suite run is called in the gate's words, by its workflow.
WHOLE = {"e2e": "the whole browser suite", "ci": "every server test", "images": IMAGES}
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
MEMBER_MARKS = ("unavailable", "fixes", "refreshable", "restored")

# Deferred imports in the concern modules use this same entry point when run as a script.
if __name__ == "__main__":
    sys.modules["merge_gate"] = sys.modules[__name__]


from merge_github import (
    BudgetWaiting,
    MEMORY_SHORTAGE,
    PUSH_RETRY_SECONDS,
    PackagesWaiting,
    PushWaiting,
    Pushed,
    Refused,
    ServiceWaiting,
    TimedOut,
    Waiting,
    _api,
    _own_runs,
    _push_once,
    _read_pull,
    _unlisted_runs,
    api,
    command,
    git,
    nonblank_lines,
    push,
    push_failure,
    read_github,
    read_pull,
    read_run,
    repository_command,
    runs,
    said,
    short_of_memory,
    workflow_runs,
)
from merge_checks import (
    _PART,
    _cancel_entry,
    _ci_check,
    _ci_suites,
    _completed_check,
    _failed_rollup,
    _lender_cancelled_check,
    _passing_check,
    _passing_rollup,
    _planner_notes,
    _proves_whole,
    _seconds,
    cancel_inherited,
    cancelled_inherited,
    carried,
    checks,
    failed_checks,
    fix_evidence,
    inherited_green,
    job_notes,
    jobs,
    lender_replanning,
    lost_check,
    note_lost,
    own_checks,
    proves_advisory_fix,
    proves_main_fix,
    record_green,
    rerun_lost,
    server_tests_selected,
    strict_jobs,
    tested_merge,
    verdict,
    whole_server_tests,
    whole_suite,
    workflow_verdict,
)
from merge_health import (
    ADVISORIES_NAMED,
    Health,
    MERGING,
    PLAIN_LENGTH,
    SERVICE_DOWN,
    SERVICE_PENDING,
    STILL_ADVISORY,
    _NOT_PLAIN,
    _advisory_lines,
    _cancel,
    _failed_jobs,
    _named,
    _outage_in,
    _outage_text,
    _refresh_health,
    _rerun,
    _rerunnable,
    _retry_failed_main,
    _service_wait,
    audit_notice,
    batch_service_wait,
    main_health,
    note_service_down,
    note_service_up,
    publish,
    pull_service_wait,
    push_health,
    refresh_health,
    scheduled_failure,
    service_answers,
    service_down,
    state_of,
    unreachable_service,
    unreached_service,
    wake_log,
)
from merge_images import (
    MainImages,
    browser_needed,
    images_needed,
    inherited_image_jobs,
    main_image_runs,
    main_images_needed,
    path_filter,
    pull_request_paths,
)
from merge_squash import (
    _batch_squash,
    _merge_tree,
    _taken_migrations,
    changed_files,
    commit_list,
    squash_body,
    squash_proof,
)
from merge_scratch import (
    Prefix,
    _batch_ready,
    _batch_report,
    _batch_rerun_green,
    _batch_runs,
    _batch_service_answered,
    _batch_signal,
    _batch_wait,
    _dispatch_scratch,
    _drop_line,
    _drop_prefix,
    _extend_line,
    _first_invalid,
    _merge_prefixes,
    _move_prefixes,
    _prefix_failed,
    _prefix_scratch,
    _prefix_state,
    _proves_on,
    _push_scratch,
    _queue_recovery_pending,
    _queue_warning,
    _queued_prefix,
    _refill,
    _restore_prefixes,
    _retest,
    _run_line,
    _scratch_runs,
    _seen,
    _start_prefixes,
    _suite_of,
    _suite_state,
    _suite_workflows,
    _sweep_scratch,
    _sweeping,
    _whole_suite_run,
)
from merge_train import (
    OwnView,
    Train,
    _advance_line,
    _prefix_summary,
    _pull_run_token,
    _run_token,
    conflicting,
    publish_first,
    ready,
    train_status,
)


def __getattr__(name: str) -> Any:
    # These two names are reassigned by their owning module rather than copied on import.
    if name == "_paused":
        from merge_github import _paused

        return _paused
    if name == "_planning_reads":
        from merge_train import _planning_reads

        return _planning_reads
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main_checkout() -> Path:
    """The main checkout of the repository the gate works on."""
    return CONFIG.main


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
    # The repository's command that resolves the conflicts and moves the numbered files a
    # refresh settles itself; without one no conflict is mechanical and nothing is renumbered.
    rules = CONFIG.command("mechanical_conflicts") is not None
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
    if mechanical and not rules and (conflicts or conflicting(pull)):
        # Without the repository's rules no conflict is mechanical, whether git or only GitHub
        # sees it: the reviewer resolves it.
        where = f" in {', '.join(conflicts)}" if conflicts else ""
        raise Refused(
            f"non-mechanical conflict{where}: the repository resolves no conflict mechanically"
        )
    if mechanical and rules:
        # It resolves the merge in the worktree, stages it and names each file it resolved.
        done = repository_command("mechanical_conflicts", "resolve", head, base)
        assert done is not None
        if done.returncode:
            if conflicts:
                raise Refused(f"non-mechanical conflict in {', '.join(conflicts)}: {said(done)}")
            raise Refused(said(done))
        message = "merge main: mechanical conflict in " + ", ".join(nonblank_lines(done.stdout))
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
        and rules
        and not command("git", "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode
    ):
        # Git merges two files numbered alike under different names cleanly; the tool that
        # reads them in order then has two heads. The branch's new ones move past main's last,
        # staged, or wait as a conflict: the command then names the files it could not move.
        done = repository_command("mechanical_conflicts", "renumber", head, base)
        assert done is not None
        if done.returncode and (paths := nonblank_lines(done.stdout)):
            message = "merge main: conflict in " + ", ".join(paths)
            Path(git("rev-parse", "--git-path", "MERGE_MSG")).write_text(message + "\n")
            train.conflicts[number] = head
            train.save()
            raise Refused(
                f"main took the number of {', '.join(paths)} and it cannot be renumbered "
                f"mechanically ({done.stderr.strip()}); renumber the branch's numbered files "
                f"after main's last, commit with '{message}', push, then wait for CI"
            )
        if done.returncode:
            raise Refused(said(done))
        moved = [line.split() for line in nonblank_lines(done.stdout)]
        if any(len(pair) != 2 for pair in moved):
            raise Refused(f"could not read the files the repository renumbered: {done.stdout}")
        if moved:
            message = "merge main: renumber migration " + ", ".join(
                f"{Path(old).name} to {Path(new).name}" for old, new in moved
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
    clear_generated(Path.cwd())
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


def clear_generated(worktree: Path) -> None:
    if not CONFIG.refresh.clear_generated:
        return
    try:
        root = Path(git("-C", str(worktree), "rev-parse", "--show-toplevel"))
        CONFIG.refresh.clear(root)
    except (workflow_config.ConfigError, OSError) as error:
        raise Refused(str(error)) from error


def pre_train_checks(number: str, folder: Path, *, locked: bool = True) -> None:
    """A failed local inventory must neither join nor stay approved for a prefix.

    A repository that names no `pre_train_checks` command has none to fail. A caller that does
    not hold the merge lock (a handoff) takes it only to leave the line."""
    done = repository_command("pre_train_checks")
    if done is None:
        return
    print(done.stdout, end="", flush=True)
    print(done.stderr, end="", file=sys.stderr, flush=True)
    if done.returncode:
        with nullcontext() if locked else handoff_lock(folder, number):
            train = Train(folder)
            members = train.prefixes["members"] if train.prefixes else []
            for member in members:
                if member["number"] == number:
                    _drop_prefix(train, number)
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


def _usual_reviewer(folder: Path, number: str, head: str, why: str) -> NoReturn:
    """Leave the round to the usual reviewer: the tick stops this one and starts that one."""
    (folder / f"review-{number}.usual-reviewer").write_text(head + "\n")
    raise Refused(
        f"this round needs the usual reviewer: {why}; end with review finished, and the tick "
        "starts the usual reviewer"
    )


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
    from conflict_review import Scope, named_reviewer, passed

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
    # The cheaper reviewer `conflict_review` names was started for a small conflict: anything
    # beyond resolving it within its lines is the usual reviewer's.
    named = CONFIG.conflict_review if named_reviewer(folder, number) else None
    if named is not None and conflict is None:
        _usual_reviewer(
            folder, number, head, "the reviewer of a small conflict passes no full review"
        )
    if conflict:
        try:
            scope = Scope.read(folder, number)
            if (
                scope is None
                or scope.head != conflict[1]
                or passed(reviews, scope.head) is None
            ):
                raise ValueError("conflict round lacks its scope or earlier passed round")
            if named is not None:
                try:
                    lines, outside = scope.size(Path.cwd(), head)
                except (ValueError, subprocess.SubprocessError, CommandTimeout) as error:
                    _usual_reviewer(folder, number, head, str(error))
                if outside:
                    _usual_reviewer(
                        folder, number, head, f"the resolution changes {', '.join(outside)} too"
                    )
                if lines > named.max_changed_lines:
                    _usual_reviewer(
                        folder,
                        number,
                        head,
                        f"the conflict and its resolution take {lines} lines in "
                        f"{', '.join(scope.files)}, more than the {named.max_changed_lines} of "
                        "conflict_review.max_changed_lines",
                    )
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
                f"run {NOTIFY_WAITING} and end the turn; the tick wakes you",
                flush=True,
            )
            command("bash", str(HERE / "notify"), "waiting")
            return 3
        # A pending or red main still lets a stale pull request refresh, so its CI runs on the
        # current main; the merge itself waits for green, or for proof of a fix, below.
        if health.state not in (*MERGING, "pending", "red"):
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
            if server_tests_selected(parts) or (
                "server_whole" in CONFIG.checks.prefix_only
                and (NOTES.server_tests, "none") not in _planner_notes(parts, CI_PLAN)
            ):
                needed.append("ci")
            # A local conflict GitHub does not see names no changed files: it needs proof too.
            if local is None or browser_needed(base, local):
                own = [] if "browser_whole" in CONFIG.checks.prefix_only else runs("e2e", head, pull=True)[:1]
                if not (own and whole_suite(own[0], jobs(own[0]), tree)):
                    needed.append("e2e")
            if "images" in CONFIG.checks.prefix_only and images_needed(head, base, []):
                needed.append("images")
            if needed and local != tree:
                raise Refused(
                    "a local merge of this pull request differs from the merge CI tested; "
                    f"{' and '.join(WHOLE[workflow] for workflow in needed)} cannot be run on "
                    "the tested files"
                )
        # The first members of the line are tested at once, each as a prefix on a scratch
        # branch. A fresh first member merges by its own checks meanwhile, below; on a red
        # main, its fix does too, judged by its own proof.
        deferred_workflows = {CONFIG.checks.groups()[group].workflow for group in CONFIG.checks.prefix_only}
        lined = _run_line(train, base, health, folder, number,
                          merge_sha is not None and not deferred_workflows.intersection(needed))
        if lined is not None:
            return lined
        if merge_sha is None:
            return refresh_pull(pull, base, train, number)
        strict = strict_jobs(parts)
        passed_checks = {job["name"] for job in strict if job.get("conclusion") == "success"}
        images = [] if "images" in CONFIG.checks.prefix_only else runs("images", head, pull=True)
        if "images" not in CONFIG.checks.prefix_only and images_needed(head, base, pull["statusCheckRollup"]) and not images:
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
            image_notes = job_notes(image_plan)
            if not any(
                note.get("title") == NOTES.files_tested and note["message"].strip() == tree
                for note in image_notes
            ):
                return refresh_pull(pull, base, train, number)
        for check in own_checks(pull):
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
        evidence = {"ci": (ci[0], strict)}
        suite = train.suites.get(number, {})
        for workflow, run_id in suite.get("passed", {}).items():
            run = {"id": run_id}
            proven = strict_jobs(jobs(run)) if workflow == "ci" else jobs(run)
            evidence["ci_suite" if workflow == "ci" else workflow] = run, proven
            passed_checks |= {job["name"] for job in proven if job.get("conclusion") == "success"}
            if workflow == "images":
                images, image_jobs = [run], proven
            if workflow == "ci" and "server_whole" in CONFIG.checks.prefix_only:
                # Main may inherit each job proven on this tree, including the deferred suite.
                passed_names = {job["name"] for job in proven if job.get("conclusion") == "success"}
                strict = [job for job in strict if job["name"] not in passed_names] + proven
        # From here a selected "CI: server tests" stands for every server test: they passed on this
        # very tree, so it may prove the fix of a red main and be inherited by main.
        # Catch a main push outside cooperating sessions while the checks were being read.
        current_base, current_health = refresh_health(folder)
        # A red main merges only the fix: this pull request's CI on that same main ran and
        # passed every check failing there. A skipped check proves nothing.
        fixes = current_health.state == "red" and set(current_health.failing) <= passed_checks
        if current_health.state not in MERGING and not fixes:
            if current_health.state == "red":
                # Sleep until main or this head changes; a later arrival may be the fix.
                train.suspend(number, head, current_health.line)
            raise Waiting("merging stopped: " + current_health.line)
        if current_base != base:
            raise Waiting("main moved during the freshness check; run this gate again")
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
            receipt_ci = evidence.get("ci_suite", evidence["ci"])[0] if "server_whole" in CONFIG.checks.prefix_only else ci[0]
            record_green(folder, base, tree, receipt_ci, strict, images, image_jobs)
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
    if os.environ.get("SHIPLINE_SHADOW") and sys.argv[1:] != ["--train-status"]:
        print(
            "merge refused: a shadow run only reads the merge line (--train-status)",
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
    # Shared diagnosis imports this module. Keep the CLI's exception classes and cached
    # reads: loading a second copy would turn an expected wait into an uncaught error.
    sys.modules["merge_gate"] = sys.modules[__name__]
    sys.exit(main())
