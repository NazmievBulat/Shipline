"""Main health, advisory outages and health publication."""

import fcntl
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import train_reads


# The gate's note of a package advisory service a push found unreachable, kept until a push
# succeeds or the service answers again, and the line the wake tells once an outage.
SERVICE_DOWN = "advisory-service-down"
SERVICE_PENDING = "advisory-service.pending"


def unreachable_service(text: str) -> str | None:
    """The advisory service the repository's `advisory_watch` reads in `text` as unreachable;
    None when it names none, or when the repository has no advisory watch."""
    from merge_gate import nonblank_lines, repository_command, said

    done = repository_command("advisory_watch", "unreachable", input=text)
    if done is None:
        return None
    if done.returncode:
        # Unread is no outage: the failure is told, and the push is refused as it is.
        print(
            f"the repository's advisory_watch could not read an output: {said(done)}",
            file=sys.stderr,
            flush=True,
        )
        return None
    return next(iter(nonblank_lines(done.stdout)), "").strip() or None


def service_answers(service: str) -> bool:
    """Whether an advisory service answers again, by the repository's `advisory_watch`."""
    from merge_gate import repository_command

    done = repository_command("advisory_watch", "answers", service)
    return done is not None and not done.returncode


def service_down(folder: Path) -> dict[str, str] | None:
    """The outage noted in `folder`; a note that cannot be read counts as none."""
    from merge_gate import SERVICE_DOWN

    try:
        note = json.loads((folder / SERVICE_DOWN).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(note, dict) and isinstance(note.get("service"), str) and note["service"]:
        return note
    return None


def note_service_down(folder: Path, service: str) -> None:
    """Notes the first push of an outage, and the line that tells it once; a later push of the
    same outage keeps the first note."""
    from merge_gate import SERVICE_DOWN, SERVICE_PENDING, service_down

    if service_down(folder) is not None:
        return
    folder.mkdir(parents=True, exist_ok=True)
    since = time.strftime(
        "%Y-%m-%d %H:%M UTC", time.gmtime(float(os.environ.get("WORKFLOW_NOW") or time.time()))
    )
    for name, text in (
        (SERVICE_DOWN, json.dumps({"service": service, "since": since}) + "\n"),
        (SERVICE_PENDING, f"{service}'s advisory service unreachable since {since}\n"),
    ):
        with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as output:
            output.write(text)
        os.replace(output.name, folder / name)


def note_service_up(folder: Path) -> None:
    from merge_gate import SERVICE_DOWN

    (folder / SERVICE_DOWN).unlink(missing_ok=True)


# The states of main in which the line merges: on an advisory main the audit alone failed, for
# advisories only, and the update that fixes them is a pull request like any other.
MERGING = ("green", "advisory")


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
    from merge_gate import IMAGES, jobs, runs, verdict

    latest = runs("images", head, scheduled=True)[:1]
    for run in latest:
        state, names = verdict(run, jobs(run), images=True)
        if state == "red":
            return f"scheduled {IMAGES} run {run['id']} failed: {', '.join(names)}"
    return ""


def main_health(
    head: str, retry_folder: Path | None = None, *, retry: bool = False
) -> Health:
    from merge_gate import push_health, scheduled_failure

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
    the dependency audit is the only failed job besides "CI result", which follows it."""
    from merge_gate import (
        API,
        CI_RESULT,
        CONFIG,
        DEPENDENCIES,
        Refused,
        Waiting,
        _failed_jobs,
        _outage_in,
        api,
    )

    if CONFIG.command("advisory_watch") is None:
        return None
    failed = _failed_jobs(parts)
    if {job["name"] for job in failed} - {CI_RESULT} != {DEPENDENCIES}:
        return None
    job = next(job for job in failed if job["name"] == DEPENDENCIES)
    try:
        notes = api(f"{API}/check-runs/{job['id']}/annotations?per_page=100")
    except Waiting:
        # Unread is not unannotated: a pull request's handoff waits for the next read.
        raise
    except Refused:
        return None
    return _outage_in(notes)


def _outage_in(notes: list[dict[str, Any]]) -> str | None:
    """The advisory service the audit's annotations name as unreachable, by the repository's
    `advisory_watch`."""
    from merge_gate import _outage_text, unreachable_service

    text = _outage_text(notes)
    return unreachable_service(text) if text.strip() else None


def _outage_text(notes: list[dict[str, Any]]) -> str:
    """What the repository's `advisory_watch` reads of the audit's annotations. The advisories
    notice is left out: published advisories' own words neither fake an outage nor hide one."""
    from merge_gate import ADVISORIES_ONLY

    return "".join(
        f"{note.get('message') or ''}\n"
        for note in notes
        if ADVISORIES_ONLY is None or note.get("title") != ADVISORIES_ONLY
    )


# What of an advisory's line may reach main's line, and so the orchestrator's message: words and
# plain marks, no quotes, backticks, angle brackets, semicolons or new lines, at most this long;
# and at most this many advisories, the rest counted.
_NOT_PLAIN = re.compile(r"[^\w .,:()\[\]/+!#&=%@-]")
PLAIN_LENGTH = 80
ADVISORIES_NAMED = 5


def audit_notice(parts: list[dict[str, Any]]) -> tuple[list[str], bool, str | None]:
    """The advisories the audit's notice names on a failed run of main, or none; whether a lock
    file was left unaudited beside them; and the advisory service that did not answer, when the
    repository's `advisory_watch` names one.

    The dependency audit is the only failed job besides "CI result", which follows it, and its
    notice lists the advisories: the audit writes it only when it audited everything, every tool
    answering, and found nothing wrong but advisories. Without the notice, a lock file it could
    not audit or a tool that failed, the failure is main's as any other; so it is with an
    outage beside the notice, which is not advisories alone."""
    from merge_gate import (
        ADVISORIES_ONLY,
        API,
        CI_RESULT,
        DEPENDENCIES,
        _advisory_lines,
        _failed_jobs,
        _outage_text,
        api,
        nonblank_lines,
        repository_command,
        said,
    )

    if ADVISORIES_ONLY is None:
        return [], False, None
    failed = _failed_jobs(parts)
    if {job["name"] for job in failed} - {CI_RESULT} != {DEPENDENCIES}:
        return [], False, None
    job = next(job for job in failed if job["name"] == DEPENDENCIES)
    notes = api(f"{API}/check-runs/{job['id']}/annotations?per_page=100")
    advisories = _advisory_lines(notes)
    # Asked only beside a notice: a run without one is red whatever else it says.
    text = _outage_text(notes)
    if not advisories or not text.strip():
        return advisories, False, None
    done = repository_command("advisory_watch", "unreachable", input=text)
    if done is None:
        return advisories, False, None
    if done.returncode:
        # A watch that could not read the annotations proves no lock file was audited whole.
        print(
            f"the repository's advisory_watch could not read main's audit: {said(done)}",
            file=sys.stderr,
            flush=True,
        )
        return advisories, True, None
    service = next(iter(nonblank_lines(done.stdout)), "").strip() or None
    return advisories, service is not None, service


def _advisory_lines(notes: list[dict[str, Any]]) -> list[str]:
    from merge_gate import ADVISORIES_ONLY, PLAIN_LENGTH, _NOT_PLAIN

    for note in notes:
        if note.get("title") != ADVISORIES_ONLY:
            continue
        # A JSON list of "<advisory> in <package> (<summary>)". The runner undoes the escapes of
        # the workflow command that wrote it; a message that still holds them is read undone.
        message = note.get("message") or ""
        for text in (
            message,
            message.replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%"),
        ):
            try:
                lines = json.loads(text)
            except ValueError:
                continue
            if isinstance(lines, list) and all(isinstance(line, str) and line for line in lines):
                # Published advisories' own words reach the orchestrator only as plain words.
                return [_NOT_PLAIN.sub("?", " ".join(line.split()))[:PLAIN_LENGTH] for line in lines]
            return []
        return []
    return []


def _named(advisories: list[str]) -> str:
    from merge_gate import ADVISORIES_NAMED

    named = ", ".join(advisories[:ADVISORIES_NAMED])
    if len(advisories) > ADVISORIES_NAMED:
        named += f" and {len(advisories) - ADVISORIES_NAMED} more"
    return named


def _retry_failed_main(
    run: dict[str, Any],
    parts: list[dict[str, Any]],
    folder: Path,
    *,
    retry: bool,
    service: str | None = None,
) -> str:
    from merge_gate import _rerun, _rerunnable, service_answers

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
        if not service_answers(service):
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
    from merge_gate import CI_PLAN, CONFIG, _failed_jobs

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
    from merge_gate import BudgetWaiting, REPOSITORY, command, git, wake_log

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


def _cancel(found: list[dict[str, Any]], folder: Path, whose: str, *, report: bool = False) -> None:
    """Cancel each run of `found` still queued or running, so that it holds no runner ahead of
    live work, each once. A cancel GitHub refuses (the run already finishing) goes to the wake
    log once and stops nothing. One held back by the shared budget, or lost to a GitHub outage
    or a stall, is a wait: the caller stops, and it is tried again at the next call."""
    from merge_gate import (
        BudgetWaiting,
        CANCEL_CLAIM_SECONDS,
        REPOSITORY,
        Refused,
        Waiting,
        command,
        transient,
        wake_log,
    )

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
            if report:
                print(note, flush=True)
        elif report:
            print(f"cancel requested for run {run['id']} of {whose}", flush=True)


def pull_service_wait(
    pull: dict[str, Any], folder: Path, number: str, *, retry: bool = False
) -> str | None:
    """Why a pull request whose checks failed only waits, or None when they failed for it.

    Its latest CI run failed only in the dependency audit (and "CI result", which follows it),
    because the package audit could not reach an advisory service: nothing was found wrong.
    With `retry` (the tick, or the gate under its lock), its failed jobs are re-run once that
    service answers again, under
    main's home-runner rules, once for each failed attempt. When the re-run shows a failure
    again, or nothing can re-run it, the failure is the pull request's, as any other.
    """
    from merge_gate import (
        CI_RESULT,
        DEPENDENCIES,
        _failed_rollup,
        _service_wait,
        jobs,
        runs,
    )

    if {check.get("name") for check in _failed_rollup(pull)} - {DEPENDENCIES, CI_RESULT}:
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
    from merge_gate import _service_wait

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
    from merge_gate import _rerun, _rerunnable, service_answers, unreached_service, verdict

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
    if not retry or not service_answers(service):
        return f"{waits}; {later}"
    return _rerun(
        run, folder, marker, f"after {service}'s advisory service answered again", name=name
    )


def push_health(
    head: str, retry_folder: Path | None = None, *, retry: bool = False
) -> Health:
    from merge_gate import (
        API,
        CI_RESULT,
        DEPENDENCIES,
        Health,
        IMAGES,
        IMAGES_PLAN,
        MainImages,
        Refused,
        WORKFLOW_FILES,
        Waiting,
        _named,
        _retry_failed_main,
        api,
        audit_notice,
        cancel_inherited,
        command,
        git,
        inherited_green,
        jobs,
        main_image_runs,
        main_images_needed,
        runs,
        unreached_service,
        verdict,
        wake_log,
    )

    ci = runs("ci", head)
    recent = MainImages()
    images = main_image_runs(head, recent)
    assessments: list[tuple[str, list[str], dict[str, Any]]] = []
    retry_notes: list[str] = []
    unreached = ""
    advisories: list[str] = []
    ci_parts: list[dict[str, Any]] = []
    if ci:
        parts = ci_parts = jobs(ci[0])
        assessments.append((*verdict(ci[0], parts), ci[0]))
        if assessments[-1][0] == "red":
            try:
                advisories, unaudited, service = audit_notice(parts)
            except Waiting:
                # GitHub could not be read: main is unknown until it can be.
                raise
            except Refused:
                # An unread notice is no advisory: main stays red.
                advisories, unaudited, service = [], False, None
            try:
                service = service if advisories else unreached_service(parts)
            except Waiting:
                # Diagnostic only: main stays red as it is, and is not re-run for the service.
                service = None
            if service:
                unreached = f"; {DEPENDENCIES} could not reach {service}'s advisory service, " + (
                    f"and found advisories: {_named(advisories)}"
                    if advisories
                    else "and nothing was found wrong with main"
                )
            if unaudited:
                # Red by the outage's own rule, and re-run as for it.
                advisories = []
            # A re-run finds the same advisories again.
            if retry_folder is not None and not advisories:
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
        if advisories and len(failed) == 1:
            named = _named(advisories)
            # Main's other checks must pass first: until they have, main is pending.
            running = [
                *(job["name"] for job in ci_parts if job.get("status") != "completed"),
                *(name for state, checks, _ in assessments if state == "pending" for name in checks),
            ]
            if running or ci[0].get("status") != "completed":
                return Health(
                    "pending",
                    f"main: pending, {', '.join(running or [CI_RESULT])} ({head[:12]}); "
                    f"{DEPENDENCIES} failed on advisories only: {named}",
                )
            return Health(
                "advisory",
                f"main: advisory since {since}, {names} ({head[:12]}): {named}; "
                "merging goes on, the update that fixes it goes first",
                tuple(failed[0][0]),
            )
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


# How a pending main line says that main was advisory before it and stays so: each merge on an
# advisory main passes through pending, and main is advisory until it is green, red or unknown.
STILL_ADVISORY = "; still advisory"


def publish(folder: Path, health: Health) -> None:
    from merge_gate import STILL_ADVISORY, state_of

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "main-status"
    old = target.read_text() if target.exists() else ""
    # Kept in the line itself, replaced whole, so two writers at once never leave it behind.
    before = state_of(old)
    if before == "pending" and old.rstrip("\n").endswith(STILL_ADVISORY):
        before = "advisory"
    after = health.state
    line = health.line
    if after == "pending" and before == "advisory":
        after = "advisory"
        line += STILL_ADVISORY
    line += "\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as output:
        output.write(line)
        temporary = output.name
    os.replace(temporary, target)
    # Every merge passes through pending to green; only a red, advisory or unreadable main, and
    # the change out of it, is worth waking the orchestrator for.
    if (
        (before != after and {"red", "advisory", "unknown"} & {before, after})
        or (health.scheduled and health.scheduled not in old)
        or (health.retry and line != old)
    ):
        (folder / "main-status.pending").write_text(line)


def state_of(line: str) -> str:
    """The state a published main line names: green, pending, red, advisory or unknown."""
    return re.split(r"[ ,:]", line.removeprefix("main: "), maxsplit=1)[0]


def refresh_health(folder: Path, *, retry: bool = False) -> tuple[str, Health]:
    from merge_gate import _refresh_health

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
    from merge_gate import (
        Health,
        Refused,
        git,
        main_health,
        note_service_up,
        publish,
        service_answers,
        service_down,
    )

    if retry and (note := service_down(folder)) and service_answers(note["service"]):
        note_service_up(folder)
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
